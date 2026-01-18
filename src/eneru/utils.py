"""Utility functions for Eneru."""

import subprocess
import os
from typing import Any, List, Tuple


def is_numeric(value: Any) -> bool:
    """Check if a value is numeric (int or float)."""
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return True
    if isinstance(value, str):
        try:
            float(value)
            return True
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
        return result.returncode, result.stdout, result.stderr
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
    """Format seconds into a human-readable string."""
    if not is_numeric(seconds):
        return "N/A"
    seconds = int(float(seconds))
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
