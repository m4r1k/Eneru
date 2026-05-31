"""UPS control via NUT ``upscmd`` / ``upsrw`` (v6.0).

Thin wrappers over the NUT client CLIs — the same shell-out model Eneru already
uses for ``upsc`` (see ``monitor._run_upsc``). We deliberately do not reimplement
the NUT wire protocol: ``nut-client`` ships ``upscmd``/``upsrw`` on every target.

Security model (enforced by the API layer, not here):
- These functions only run when ``nut_control.enabled`` AND ``api.auth.enabled``
  (a config-validation invariant), so control is never reachable unauthenticated.
- Command/variable names are allowlisted by the caller before reaching here.

Credentials are passed on the argv (``-u``/``-p``), which is visible in ``ps`` —
an accepted tradeoff (a local user who can read ``ps`` can already do worse), and
identical to how operators run these CLIs by hand.
"""

import re
from typing import Dict, List, Optional, Tuple

from eneru.utils import run_command


def _creds_args(username: str, password: str) -> List[str]:
    args: List[str] = []
    if username:
        args += ["-u", username]
    if password:
        args += ["-p", password]
    return args


def list_commands(ups_name: str, *, timeout: int = 10) -> Tuple[bool, List[str], str]:
    """Return the instant commands a UPS supports (``upscmd -l``).

    Listing needs no credentials. Returns ``(ok, commands, error)``.
    """
    code, out, err = run_command(["upscmd", "-l", ups_name], timeout=timeout)
    if code != 0:
        return False, [], (err.strip() or out.strip() or f"upscmd exited {code}")
    return True, _parse_command_list(out), ""


def _parse_command_list(text: str) -> List[str]:
    """Extract command names from ``upscmd -l`` output.

    Lines look like ``  beeper.toggle - Toggle the UPS beeper``; the header line
    ``Instant commands supported on UPS ...:`` has no `` - `` token.
    """
    commands: List[str] = []
    for line in text.splitlines():
        line = line.strip()
        if " - " not in line:
            continue
        name = line.split(" - ", 1)[0].strip()
        # A NUT instant command is dotted lowercase tokens (e.g. test.battery.start).
        if name and re.fullmatch(r"[a-z0-9.+-]+", name):
            commands.append(name)
    return commands


def run_instant_command(
    ups_name: str, command: str, username: str, password: str,
    *, timeout: int = 10,
) -> Tuple[bool, str, str]:
    """Run an instant command (``upscmd -u … -p … ups command``).

    Returns ``(ok, output, error)``.
    """
    cmd = ["upscmd"] + _creds_args(username, password) + [ups_name, command]
    code, out, err = run_command(cmd, timeout=timeout)
    if code != 0:
        return False, out.strip(), (err.strip() or f"upscmd exited {code}")
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
    """Write a UPS variable (``upsrw -s var=value -u … -p … ups``).

    Returns ``(ok, output, error)``.

    L10 (evaluated, tunable): a write that actually changes UPS state (e.g. a
    battery-calibration variable) can take longer to acknowledge than a read, so
    a slow SET may be reported failed here while NUT still applies it. The bound
    is the operator-tunable ``nut_control.timeout`` (passed as ``timeout``);
    raise it for slow devices rather than special-casing SET.
    """
    cmd = (["upsrw", "-s", f"{variable}={value}"]
           + _creds_args(username, password) + [ups_name])
    code, out, err = run_command(cmd, timeout=timeout)
    if code != 0:
        return False, out.strip(), (err.strip() or f"upsrw exited {code}")
    return True, out.strip(), ""
