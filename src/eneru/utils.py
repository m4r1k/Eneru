"""Utility functions for Eneru."""

import math
import subprocess
import os
from typing import Any, Dict, List, Optional, Tuple


CONTAINER_DEFAULT_KNOWN_HOSTS_FILE = "/var/lib/eneru/ssh/known_hosts"
KNOWN_HOSTS_ENV = "ENERU_SSH_KNOWN_HOSTS_FILE"


def redact_apprise_url(url: Any) -> str:
    """Return an Apprise/notification URL with credentials stripped to scheme.

    ISS-008/ISS-034: Apprise URLs embed webhook tokens/passwords
    (e.g. ``discord://id/token``). Never log or print them verbatim -- emit only
    ``scheme://***`` so the service type is still visible for debugging without
    leaking the secret. Shared by the notification worker and the CLI.
    """
    text = str(url)
    scheme = text.split("://", 1)[0] if "://" in text else "unknown"
    return f"{scheme}://***"


def is_numeric(value: Any) -> bool:
    """Check if a value is numeric (int or float).

    Rejects NaN and ±Inf — callers (UPS metrics, voltages, runtimes)
    expect a real comparable number, and `int(float("nan"))` raises
    while `float("inf")` propagates into bucket math as garbage.
    """
    if value is None:
        return False
    if isinstance(value, bool):
        # bool is a subtype of int — NUT/UPS data should never be a
        # bool, and treating True as 1 silently conceals upstream bugs.
        return False
    if isinstance(value, (int, float)):
        return math.isfinite(value)
    if isinstance(value, str):
        try:
            return math.isfinite(float(value))
        except (ValueError, TypeError):
            return False
    return False


def run_command(
    cmd: List[str],
    timeout: int = 30,
    capture_output: bool = True,
    env_overrides: Optional[Dict[str, str]] = None,
) -> Tuple[int, str, str]:
    """Run a shell command and return (exit_code, stdout, stderr)."""
    # A None timeout means "wait forever" to subprocess.run. No caller wants
    # that during a shutdown sequence: a config value that slipped through as
    # None (e.g. `unmount.timeout:` with no value) would otherwise let a busy
    # umount hang the drain phase indefinitely. Fall back to the default bound.
    if timeout is None:
        timeout = 30
    try:
        result = subprocess.run(
            cmd,
            capture_output=capture_output,
            text=True,
            timeout=timeout,
            env={**os.environ, 'LC_NUMERIC': 'C', **(env_overrides or {})}
        )
        # subprocess.run returns stdout/stderr=None when capture_output
        # is False; normalize to empty strings so callers can always
        # `.strip()` / index the values without a TypeError.
        return (
            result.returncode,
            result.stdout if result.stdout is not None else "",
            result.stderr if result.stderr is not None else "",
        )
    except subprocess.TimeoutExpired:
        return 124, "", "Command timed out"
    except FileNotFoundError:
        return 127, "", f"Command not found: {cmd[0]}"
    except Exception as e:
        return 1, "", str(e)


def running_in_container() -> bool:
    """Best-effort check for Docker/Podman/Kubernetes runtime context."""
    if os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv"):
        return True
    if os.environ.get("KUBERNETES_SERVICE_HOST"):
        return True
    if os.environ.get("container", "").strip():
        return True
    try:
        with open("/proc/1/cgroup", encoding="utf-8") as fh:
            cgroup_text = fh.read()
    except OSError:
        cgroup_text = ""
    return any(marker in cgroup_text for marker in (
        "docker", "kubepods", "containerd", "lxc",
    ))


def ssh_option_configured(ssh_options: List[str], option_name: str) -> bool:
    """Return True when an OpenSSH option is already set by the user."""
    needle = option_name.lower()
    pending_o = False
    for opt in ssh_options:
        if not isinstance(opt, str):
            continue
        value = opt.strip()
        lower = value.lower()
        if pending_o:
            if _ssh_option_matches(lower, needle):
                return True
            pending_o = False
            continue
        if lower == "-o":
            pending_o = True
            continue
        if lower.startswith("-o "):
            value = value.split(None, 1)[1]
            lower = value.lower()
        if _ssh_option_matches(lower, needle):
            return True
    return False


def _ssh_option_matches(value: str, option_name: str) -> bool:
    return (
        value == option_name
        or value.startswith(f"{option_name}=")
        or value.startswith(f"{option_name} ")
    )


def runtime_default_ssh_options(ssh_options: List[str]) -> List[str]:
    """Return SSH defaults that depend on the runtime environment.

    Bare-metal installs should use the running user's normal OpenSSH trust
    store. Containers keep Eneru's documented SSH mount contract instead:
    `/srv/eneru/ssh` on the host maps to `/var/lib/eneru/ssh` in the
    container, so accept-new host keys are written there unless an explicit
    override is configured.
    """
    known_hosts = os.environ.get(KNOWN_HOSTS_ENV, "").strip()
    if not known_hosts and running_in_container():
        known_hosts = CONTAINER_DEFAULT_KNOWN_HOSTS_FILE
    if known_hosts and not ssh_option_configured(ssh_options, "UserKnownHostsFile"):
        return [f"UserKnownHostsFile={known_hosts}"]
    return []


def command_exists(cmd: str) -> bool:
    """Check if a command exists in the system PATH."""
    exit_code, _, _ = run_command(["which", cmd])
    return exit_code == 0


def format_seconds(seconds: Any) -> str:
    """Format seconds into a human-readable string.

    Negative inputs are clamped to 0 — UPS runtime/uptime values are
    never negative semantically, but a misbehaving driver can briefly
    return one (e.g. clock-skew during a hot-swap), and "-1m 30s" in
    the TUI is more confusing than "0s".
    """
    if not is_numeric(seconds):
        return "N/A"
    seconds = max(0, int(float(seconds)))
    if seconds < 60:
        return f"{seconds}s"
    elif seconds < 3600:
        mins = seconds // 60
        secs = seconds % 60
        return f"{mins}m {secs}s"
    else:
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        return f"{hours}h {minutes}m"
