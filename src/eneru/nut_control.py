"""UPS control via NUT ``upscmd`` / ``upsrw`` (v6.0).

Thin wrappers over the NUT client CLIs — the same shell-out model Eneru already
uses for ``upsc`` (see ``monitor._run_upsc``). We deliberately do not reimplement
the NUT wire protocol: ``nut-client`` ships ``upscmd``/``upsrw`` on every target.

Security model (enforced by the API layer, not here):
- These functions only run when ``nut_control.enabled`` AND ``api.auth.enabled``
  (a config-validation invariant), so control is never reachable unauthenticated.
- Command/variable names are allowlisted by the caller before reaching here.

Passwords are not passed on argv. ``upscmd``/``upsrw`` prompt for the password
when ``-p`` is omitted, so authenticated calls run behind a pseudo-terminal and
answer that prompt without exposing the reusable secret to ``ps``.
"""

import os
import pty
import re
import select
import subprocess
import termios
import threading
import time
from typing import Dict, List, Optional, Tuple

from eneru.utils import run_command

# Per-UPS lock serializing CONTROL commands (INSTCMD/SET/self-test) against one
# device. Lives here so every issuer shares one lock identity — the API control
# routes AND the scheduled self-test path in the monitor — so two of them can't
# race a command against the same UPS. Keyed by the real NUT name.
_ups_command_locks: Dict[str, "threading.Lock"] = {}
_ups_locks_guard = threading.Lock()


def command_lock(name: str) -> "threading.Lock":
    """Return the shared per-UPS control-command lock for ``name``."""
    with _ups_locks_guard:
        lock = _ups_command_locks.get(name)
        if lock is None:
            lock = threading.Lock()
            _ups_command_locks[name] = lock
        return lock


_AUTH_COMMAND_BINARIES = {"upscmd", "upsrw"}
_SAFE_AUTH_ARG = re.compile(r"\A[A-Za-z0-9 ._:@+%/,\-=\[\]]{1,256}\Z")
_PASSWORD_PROMPT_RE = re.compile(
    rb"password[^\r\n]{0,40}[:?]\s*\Z",
    re.IGNORECASE,
)


def _creds_args(username: str, password: str) -> List[str]:
    args: List[str] = []
    if username:
        args += ["-u", username]
    return args


def _safe_auth_data_arg(arg: object) -> Tuple[Optional[str], str]:
    """Return one validated argv data value, or an error message."""
    if not isinstance(arg, str) or not arg:
        return None, "empty NUT control argument"
    if "\x00" in arg:
        return None, "NUT control argument contains NUL"
    if arg.startswith("-"):
        return None, "NUT control argument looks like an option"
    if not _SAFE_AUTH_ARG.match(arg):
        return None, "NUT control argument contains unsupported characters"
    return arg, ""


def _validated_auth_command_argv(cmd: List[str]) -> Tuple[Optional[List[str]], str]:
    """Build a normalized NUT auth argv before it reaches ``subprocess``.

    The API already allowlists command and variable names, but this wrapper is
    the last gate before exec. Keeping the binary and option positions fixed
    makes the invariant visible to readers and static analysis.
    """
    if (not cmd or not isinstance(cmd[0], str)
            or cmd[0] not in _AUTH_COMMAND_BINARIES):
        return None, "unsupported NUT control binary"

    binary = cmd[0]
    args = cmd[1:]
    if binary == "upscmd":
        # Authenticated LIST: `upscmd -u user -l ups`. Some upsd setups (e.g.
        # UniFi's NUT) only return the instant-command list to a logged-in
        # client, so listing must be able to carry credentials just like an
        # INSTCMD. `-l` is a fixed literal we control (never validated as data).
        if len(args) == 4 and args[0] == "-u" and args[2] == "-l":
            username, error = _safe_auth_data_arg(args[1])
            if error:
                return None, error
            ups_name, error = _safe_auth_data_arg(args[3])
            if error:
                return None, error
            return ["upscmd", "-u", username, "-l", ups_name], ""
        if len(args) == 2:
            ups_name, error = _safe_auth_data_arg(args[0])
            if error:
                return None, error
            command, error = _safe_auth_data_arg(args[1])
            if error:
                return None, error
            return ["upscmd", ups_name, command], ""
        elif len(args) == 4 and args[0] == "-u":
            username, error = _safe_auth_data_arg(args[1])
            if error:
                return None, error
            ups_name, error = _safe_auth_data_arg(args[2])
            if error:
                return None, error
            command, error = _safe_auth_data_arg(args[3])
            if error:
                return None, error
            return ["upscmd", "-u", username, ups_name, command], ""
        else:
            return None, "invalid upscmd argument shape"
    elif binary == "upsrw":
        if len(args) == 3 and args[0] == "-s":
            assignment, error = _safe_auth_data_arg(args[1])
            if error:
                return None, error
            ups_name, error = _safe_auth_data_arg(args[2])
            if error:
                return None, error
            return ["upsrw", "-s", assignment, ups_name], ""
        elif len(args) == 5 and args[0] == "-s" and args[2] == "-u":
            assignment, error = _safe_auth_data_arg(args[1])
            if error:
                return None, error
            username, error = _safe_auth_data_arg(args[3])
            if error:
                return None, error
            ups_name, error = _safe_auth_data_arg(args[4])
            if error:
                return None, error
            return ["upsrw", "-s", assignment, "-u", username, ups_name], ""
        else:
            return None, "invalid upsrw argument shape"
    return None, "unsupported NUT control binary"


def _validate_auth_command_argv(cmd: List[str]) -> Tuple[bool, str]:
    """Validate a NUT auth argv shape."""
    safe_cmd, error = _validated_auth_command_argv(cmd)
    return safe_cmd is not None, error


def _auth_command_has_username(safe_cmd: List[str]) -> bool:
    """Return True when a normalized command includes ``-u username``."""
    if safe_cmd[0] == "upscmd":
        return len(safe_cmd) == 5 and safe_cmd[1] == "-u"
    if safe_cmd[0] == "upsrw":
        return len(safe_cmd) == 6 and safe_cmd[3] == "-u"
    return False


def _popen_validated_auth_command(
    safe_cmd: List[str],
    slave_fd: int,
) -> subprocess.Popen:
    """Spawn a previously normalized NUT auth command on a PTY.

    CodeQL treats a fully data-derived argv list as command-injection tainted,
    even after validation. Keep the executable and option positions as fixed
    literals at each spawn site; only validated data values are copied in.
    """
    if safe_cmd[0] == "upscmd":
        if len(safe_cmd) == 3:
            return subprocess.Popen(
                ["upscmd", safe_cmd[1], safe_cmd[2]],
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                close_fds=True,
            )
        if safe_cmd[3] == "-l":
            # Authenticated list: upscmd -u user -l ups (keep -l a literal).
            return subprocess.Popen(
                ["upscmd", "-u", safe_cmd[2], "-l", safe_cmd[4]],
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                close_fds=True,
            )
        return subprocess.Popen(
            ["upscmd", "-u", safe_cmd[2], safe_cmd[3], safe_cmd[4]],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True,
        )

    if len(safe_cmd) == 4:
        return subprocess.Popen(
            ["upsrw", "-s", safe_cmd[2], safe_cmd[3]],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True,
        )
    return subprocess.Popen(
        ["upsrw", "-s", safe_cmd[2], "-u", safe_cmd[4], safe_cmd[5]],
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        close_fds=True,
    )


def _kill_and_reap(proc: subprocess.Popen) -> None:
    """Best-effort terminate + wait so timed-out PTY children do not zombie."""
    try:
        proc.kill()
    except Exception:
        pass
    try:
        proc.wait(timeout=1)
    except Exception:
        pass


def _run_auth_command(
    cmd: List[str],
    password: str,
    *,
    timeout: int = 10,
) -> Tuple[int, str, str]:
    """Run a NUT auth command without putting the password in argv."""
    safe_cmd, error = _validated_auth_command_argv(cmd)
    if safe_cmd is None:
        return 2, "", error
    has_username = _auth_command_has_username(safe_cmd)
    if password and not has_username:
        return 2, "", "NUT control password requires username (-u)"
    if has_username and not password:
        return 2, "", "NUT control username requires password (-p)"
    if not password:
        return run_command(safe_cmd, timeout=timeout)

    master_fd: Optional[int] = None
    slave_fd: Optional[int] = None
    proc: Optional[subprocess.Popen] = None
    output = bytearray()
    password_sent = False
    deadline = time.monotonic() + max(1, int(timeout))

    try:
        master_fd, slave_fd = pty.openpty()
        # F-034: a fresh pty has ECHO on, so the password we later write to the
        # master to answer the prompt gets echoed straight back and captured
        # into `output` — leaking the secret into the returned buffer (and any
        # log of it). Turn echo off on the terminal before the child inherits
        # it. Fail closed if the terminal cannot disable echo: sending the
        # secret anyway could put it in captured output and later logs.
        try:
            attrs = termios.tcgetattr(slave_fd)
            attrs[3] &= ~termios.ECHO   # index 3 == lflags
            termios.tcsetattr(slave_fd, termios.TCSANOW, attrs)
        except (OSError, termios.error) as exc:
            return (
                1,
                "",
                "Could not disable PTY echo; refusing to send the NUT "
                f"control password: {exc}",
            )
        proc = _popen_validated_auth_command(safe_cmd, slave_fd)
        os.close(slave_fd)
        slave_fd = None

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _kill_and_reap(proc)
                proc = None
                text = output.decode("utf-8", errors="replace")
                return 124, text, "Command timed out"

            readable, _, _ = select.select(
                [master_fd], [], [], min(0.1, remaining)
            )
            if readable:
                try:
                    chunk = os.read(master_fd, 4096)
                except OSError:
                    chunk = b""
                if chunk:
                    output.extend(chunk)
                    prompt_tail = bytes(output[-256:])
                    if (not password_sent
                            and _PASSWORD_PROMPT_RE.search(prompt_tail)):
                        os.write(master_fd, (password + "\n").encode("utf-8"))
                        password_sent = True

            rc = proc.poll()
            if rc is not None:
                while True:
                    try:
                        chunk = os.read(master_fd, 4096)
                    except OSError:
                        break
                    if not chunk:
                        break
                    output.extend(chunk)
                text = output.decode("utf-8", errors="replace")
                return rc, text, ""
    except FileNotFoundError as exc:
        return 127, "", str(exc)
    except Exception as exc:
        text = output.decode("utf-8", errors="replace")
        return 1, text, str(exc)
    finally:
        if proc is not None and proc.poll() is None:
            _kill_and_reap(proc)
        for fd in (master_fd, slave_fd):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass


def list_commands(ups_name: str, *, username: str = "", password: str = "",
                  timeout: int = 10) -> Tuple[bool, List[str], str]:
    """Return the instant commands a UPS supports (``upscmd -l``).

    Returns ``(ok, commands, error)``. Pass ``username``/``password`` when the
    upsd requires a logged-in client to list commands — some setups (notably
    UniFi's NUT) return an EMPTY list to an anonymous ``upscmd -l``, which would
    otherwise look like "the UPS doesn't expose the command". When both are
    given the credentialed PTY path is used (password never on argv); otherwise
    it falls back to an anonymous listing (unchanged v6.0 behavior).
    """
    if username and password:
        code, out, err = _run_auth_command(
            ["upscmd", "-u", username, "-l", ups_name], password, timeout=timeout)
    else:
        code, out, err = run_command(["upscmd", "-l", ups_name], timeout=timeout)
    if code != 0:
        return False, [], (err.strip() or out.strip() or f"upscmd exited {code}")
    return True, _parse_command_list(out), ""


def _parse_command_list(text: str) -> List[str]:
    """Extract command names from ``upscmd -l`` output.

    Two formats occur in the wild:
      * standard NUT drivers: ``  beeper.toggle - Toggle the UPS beeper``
      * description-less (e.g. Ubiquiti/UniFi): bare ``test.battery.start``
    Take the FIRST whitespace-delimited token of each line — the command name in
    BOTH shapes — and keep it only if it looks like a NUT instant command (dotted
    lowercase). The header line ``Instant commands supported on UPS ...:`` and any
    blank lines fall out because their first token isn't a valid command name.
    (Earlier versions required a `` - `` separator and so returned NOTHING for the
    description-less format, making a supported command look unsupported.)
    """
    commands: List[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        # Standard "name - description": take the part before the separator.
        # Description-less: the WHOLE line must itself be the command name — so a
        # bare "test.battery.start" is accepted while prose ("noise line without
        # separator", the "Instant commands..." header) is rejected because it
        # isn't a single command token.
        name = line.split(" - ", 1)[0].strip() if " - " in line else line
        # A NUT instant command is dotted lowercase tokens (e.g. test.battery.start).
        if re.fullmatch(r"[a-z0-9.+-]+", name):
            commands.append(name)
    return commands


def command_allowed(command: str, allowed_commands) -> bool:
    """Is ``command`` on the control allowlist?

    The single membership check shared by the API control path AND the v6.1
    self-test scheduler, so a scheduled test can never be a back door around
    the v6.0 allowlist. An empty/unset allowlist denies everything.
    """
    return bool(command) and command in set(allowed_commands or [])


def run_instant_command(
    ups_name: str, command: str, username: str, password: str,
    *, timeout: int = 10,
) -> Tuple[bool, str, str]:
    """Run an instant command (``upscmd -u … ups command``).

    Returns ``(ok, output, error)``.
    """
    cmd = ["upscmd"] + _creds_args(username, password) + [ups_name, command]
    code, out, err = _run_auth_command(cmd, password, timeout=timeout)
    if code != 0:
        return False, out.strip(), (err.strip() or out.strip()
                                    or f"upscmd exited {code}")
    return True, out.strip(), ""


def list_variables(ups_name: str, *, timeout: int = 10) -> Tuple[bool, List[Dict], str]:
    """Return writable variables and their current values (``upsrw ups``).

    Returns ``(ok, variables, error)`` where each variable is
    ``{"name", "type", "value"}``.
    """
    code, out, err = run_command(["upsrw", ups_name], timeout=timeout)
    if code != 0:
        return False, [], (err.strip() or out.strip() or f"upsrw exited {code}")
    return True, _parse_variable_list(out), ""


def _parse_variable_list(text: str) -> List[Dict]:
    """Parse ``upsrw`` output into a list of ``{name, type, value}`` dicts.

    ``upsrw`` prints blocks like::

        [input.transfer.low]
        Low transfer voltage
        Type: STRING
        Value: 196
    """
    variables: List[Dict] = []
    current: Optional[Dict] = None
    for raw in text.splitlines():
        line = raw.strip()
        header = re.fullmatch(r"\[(.+)\]", line)
        if header:
            current = {"name": header.group(1), "type": "", "value": ""}
            variables.append(current)
            continue
        if current is None:
            continue
        if line.startswith("Type:"):
            current["type"] = line.split(":", 1)[1].strip()
        elif line.startswith("Value:"):
            current["value"] = line.split(":", 1)[1].strip()
    return variables


def set_variable(
    ups_name: str, variable: str, value: str, username: str, password: str,
    *, timeout: int = 10,
) -> Tuple[bool, str, str]:
    """Write a UPS variable (``upsrw -s var=value -u … ups``).

    Returns ``(ok, output, error)``.

    L10 (evaluated, tunable): a write that actually changes UPS state (e.g. a
    battery-calibration variable) can take longer to acknowledge than a read, so
    a slow SET may be reported failed here while NUT still applies it. The bound
    is the operator-tunable ``nut_control.timeout`` (passed as ``timeout``);
    raise it for slow devices rather than special-casing SET.
    """
    cmd = (["upsrw", "-s", f"{variable}={value}"]
           + _creds_args(username, password) + [ups_name])
    code, out, err = _run_auth_command(cmd, password, timeout=timeout)
    if code != 0:
        return False, out.strip(), (err.strip() or out.strip()
                                    or f"upsrw exited {code}")
    return True, out.strip(), ""
