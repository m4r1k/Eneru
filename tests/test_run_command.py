"""Tests for run_command and command_exists helper functions."""

import pytest
from unittest.mock import patch, MagicMock
import subprocess

from ups_monitor import run_command, command_exists


class TestRunCommand:
    """Test the run_command helper function."""

    @pytest.mark.unit
    def test_successful_command(self):
        """Test successful command execution."""
        exit_code, stdout, stderr = run_command(["echo", "hello"])

        assert exit_code == 0
        assert "hello" in stdout
        assert stderr == ""

    @pytest.mark.unit
    def test_command_with_nonzero_exit(self):
        """Test command that returns non-zero exit code."""
        exit_code, stdout, stderr = run_command(["sh", "-c", "exit 42"])

        assert exit_code == 42

    @pytest.mark.unit
    def test_command_with_stderr_output(self):
        """Test command that writes to stderr."""
        exit_code, stdout, stderr = run_command(
            ["sh", "-c", "echo error >&2; exit 1"]
        )

        assert exit_code == 1
        assert "error" in stderr

    @pytest.mark.unit
    def test_command_timeout(self):
        """Test command that times out."""
        # Sleep for longer than the timeout
        exit_code, stdout, stderr = run_command(
            ["sleep", "10"],
            timeout=1
        )

        assert exit_code == 124
        assert "timed out" in stderr.lower()

    @pytest.mark.unit
    def test_command_not_found(self):
        """Test command that doesn't exist."""
        exit_code, stdout, stderr = run_command(
            ["nonexistent_command_xyz_123"]
        )

        assert exit_code == 127
        assert "not found" in stderr.lower()

    @pytest.mark.unit
    def test_command_with_arguments(self):
        """Test command with multiple arguments."""
        exit_code, stdout, stderr = run_command(
            ["sh", "-c", "echo $0 $1", "arg0", "arg1"]
        )

        assert exit_code == 0
        assert "arg0" in stdout
        assert "arg1" in stdout

    @pytest.mark.unit
    def test_command_output_capture(self):
        """Test that both stdout and stderr are captured correctly."""
        exit_code, stdout, stderr = run_command(
            ["sh", "-c", "echo stdout_msg; echo stderr_msg >&2"]
        )

        assert exit_code == 0
        assert "stdout_msg" in stdout
        assert "stderr_msg" in stderr

    @pytest.mark.unit
    def test_command_with_lc_numeric_env(self):
        """Test that LC_NUMERIC is set to C for consistent number formatting."""
        exit_code, stdout, stderr = run_command(
            ["sh", "-c", "echo $LC_NUMERIC"]
        )

        assert exit_code == 0
        assert "C" in stdout

    @pytest.mark.unit
    def test_default_timeout(self):
        """Test that default timeout is applied (command should complete quickly)."""
        # A quick command should work with default timeout
        exit_code, stdout, stderr = run_command(["echo", "quick"])

        assert exit_code == 0
        assert "quick" in stdout

    @pytest.mark.unit
    def test_generic_exception_handling(self):
        """Test handling of generic exceptions during command execution."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = Exception("Generic error")

            exit_code, stdout, stderr = run_command(["any", "command"])

            assert exit_code == 1
            assert stdout == ""
            assert "Generic error" in stderr


class TestCommandExists:
    """Test the command_exists helper function."""

    @pytest.mark.unit
    def test_existing_command(self):
        """Test detection of existing command."""
        # 'echo' should exist on all systems
        assert command_exists("echo") is True

    @pytest.mark.unit
    def test_nonexistent_command(self):
        """Test detection of non-existent command."""
        assert command_exists("nonexistent_command_xyz_123") is False

    @pytest.mark.unit
    def test_common_system_commands(self):
        """Test common system commands that should exist."""
        # These should exist on most Unix-like systems
        common_commands = ["sh", "ls", "cat"]
        for cmd in common_commands:
            assert command_exists(cmd) is True, f"Expected {cmd} to exist"

    @pytest.mark.unit
    def test_command_exists_uses_which(self):
        """Test that command_exists uses 'which' internally."""
        with patch("ups_monitor.run_command") as mock_run:
            mock_run.return_value = (0, "/usr/bin/test", "")

            result = command_exists("test_cmd")

            mock_run.assert_called_once_with(["which", "test_cmd"])
            assert result is True

    @pytest.mark.unit
    def test_command_not_exists_which_fails(self):
        """Test that command_exists returns False when which fails."""
        with patch("ups_monitor.run_command") as mock_run:
            mock_run.return_value = (1, "", "")

            result = command_exists("missing_cmd")

            assert result is False
