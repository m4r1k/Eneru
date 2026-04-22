"""Utility functions for Eneru."""

import subprocess
import os
from typing import Any, List, Tuple


def is_numeric(value: Any) -> bool:
    """Check if a value is numeric (int or float).

    Rejects NaN and ±Inf — callers (UPS metrics, voltages, runtimes)
    expect a real comparable number, and `int(float("nan"))` raises
    while `float("inf")` propagates into bucket math as garbage.
    """
    import math
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
    capture_output: bool = True
) -> Tuple[int, str, str]:
    """Run a shell command and return (exit_code, stdout, stderr)."""
    try:
        result = subprocess.run(
            cmd,
            capture_output=capture_output,
            text=True,
            timeout=timeout,
            env={**os.environ, 'LC_NUMERIC': 'C'}
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
