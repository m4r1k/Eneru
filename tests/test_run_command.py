"""Tests for run_command and command_exists helper functions."""

import pytest
from unittest.mock import mock_open, patch

from eneru import run_command, command_exists
from eneru import utils as eneru_utils


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
    def test_none_timeout_falls_back_to_default(self):
        """H7: a None timeout must NOT mean 'wait forever' -- it falls back to
        the default bound so a config slip (unmount.timeout:) can't hang."""
        from unittest.mock import patch
        with patch("eneru.utils.subprocess.run") as mock_run:
            mock_run.return_value = type(
                "R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
            run_command(["echo", "hi"], timeout=None)
        assert mock_run.call_args.kwargs["timeout"] == 30

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
    def test_command_with_env_overrides(self):
        """Callers can add process-local environment without mutating os.environ."""
        exit_code, stdout, stderr = run_command(
            ["sh", "-c", "echo $NUT_QUIET_INIT_SSL:$LC_NUMERIC"],
            env_overrides={"NUT_QUIET_INIT_SSL": "true"},
        )

        assert exit_code == 0
        assert "true:C" in stdout
        assert stderr == ""

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

    @pytest.mark.unit
    def test_programming_errors_propagate(self):
        """ISS-056: TypeError/ValueError signal a caller bug, not a command
        runtime failure, so they must propagate instead of being masked as
        exit 1."""
        for exc in (TypeError("bad cmd"), ValueError("bad arg")):
            with patch("subprocess.run", side_effect=exc):
                with pytest.raises(type(exc)):
                    run_command(["any", "command"])


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
    def test_command_exists_uses_shutil_which(self):
        """F-031: command_exists resolves via shutil.which (a PATH walk), not a
        `which` subprocess."""
        with patch("eneru.utils.shutil.which", return_value="/usr/bin/test") as mock_which:
            result = command_exists("test_cmd")

            mock_which.assert_called_once_with("test_cmd")
            assert result is True

    @pytest.mark.unit
    def test_command_not_exists_which_fails(self):
        """command_exists returns False when shutil.which finds nothing."""
        with patch("eneru.utils.shutil.which", return_value=None):
            result = command_exists("missing_cmd")

            assert result is False


class TestStatusHasToken:
    """F-051: whitespace-separated TOKEN membership, not substring matching."""

    @pytest.mark.unit
    def test_matches_whole_token(self):
        assert eneru_utils.status_has_token("OB LB", "OB") is True
        assert eneru_utils.status_has_token("OB LB", "LB") is True
        assert eneru_utils.status_has_token("OL CHRG", "OL") is True

    @pytest.mark.unit
    def test_does_not_match_substring(self):
        # "CHRG" must not match inside "DISCHRG"; a contrived value like "NOTOB"
        # must not match "OB".
        assert eneru_utils.status_has_token("OL DISCHRG", "CHRG") is False
        assert eneru_utils.status_has_token("NOTOB", "OB") is False
        assert eneru_utils.status_has_token("FSDX", "FSD") is False

    @pytest.mark.unit
    def test_tolerates_none_and_empty(self):
        assert eneru_utils.status_has_token(None, "OB") is False
        assert eneru_utils.status_has_token("", "OB") is False


class TestRuntimeSshOptions:
    """Test runtime-dependent SSH defaults."""

    @pytest.mark.unit
    def test_running_in_container_detects_container_files(self, monkeypatch):
        monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
        monkeypatch.delenv("container", raising=False)
        with patch(
            "eneru.utils.os.path.exists",
            side_effect=lambda path: path == "/.dockerenv",
        ):
            assert eneru_utils.running_in_container() is True

    @pytest.mark.unit
    def test_running_in_container_detects_kubernetes_env(self, monkeypatch):
        monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
        with patch("eneru.utils.os.path.exists", return_value=False):
            assert eneru_utils.running_in_container() is True

    @pytest.mark.unit
    def test_running_in_container_detects_cgroup_marker(self, monkeypatch):
        monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
        monkeypatch.delenv("container", raising=False)
        with patch("eneru.utils.os.path.exists", return_value=False), patch(
            "builtins.open", mock_open(read_data="0::/kubepods.slice/pod123")
        ):
            assert eneru_utils.running_in_container() is True

    @pytest.mark.unit
    def test_running_in_container_returns_false_without_markers(self, monkeypatch):
        monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
        monkeypatch.delenv("container", raising=False)
        with patch("eneru.utils.os.path.exists", return_value=False), patch(
            "builtins.open", side_effect=OSError("missing cgroup")
        ):
            assert eneru_utils.running_in_container() is False

    @pytest.mark.unit
    @pytest.mark.parametrize("ssh_options", [
        ["UserKnownHostsFile=/tmp/known_hosts"],
        ["-o UserKnownHostsFile=/tmp/known_hosts"],
        ["-o", "UserKnownHostsFile=/tmp/known_hosts"],
    ])
    def test_ssh_option_configured_detects_user_known_hosts_file(self, ssh_options):
        assert eneru_utils.ssh_option_configured(
            ssh_options, "UserKnownHostsFile"
        ) is True

    @pytest.mark.unit
    def test_ssh_option_configured_ignores_non_matching_entries(self):
        assert eneru_utils.ssh_option_configured(
            [42, "GlobalKnownHostsFile=/tmp/global"], "UserKnownHostsFile"
        ) is False

    @pytest.mark.unit
    def test_runtime_default_ssh_options_bare_metal_uses_openssh_default(
        self, monkeypatch
    ):
        monkeypatch.delenv(eneru_utils.KNOWN_HOSTS_ENV, raising=False)
        monkeypatch.setattr(eneru_utils, "running_in_container", lambda: False)

        assert eneru_utils.runtime_default_ssh_options([]) == []

    @pytest.mark.unit
    def test_runtime_default_ssh_options_container_uses_ssh_mount(
        self, monkeypatch
    ):
        monkeypatch.delenv(eneru_utils.KNOWN_HOSTS_ENV, raising=False)
        monkeypatch.setattr(eneru_utils, "running_in_container", lambda: True)

        assert eneru_utils.runtime_default_ssh_options([]) == [
            "UserKnownHostsFile=/var/lib/eneru/ssh/known_hosts"
        ]

    @pytest.mark.unit
    def test_runtime_default_ssh_options_env_overrides_runtime(
        self, monkeypatch
    ):
        monkeypatch.setenv(eneru_utils.KNOWN_HOSTS_ENV, "/var/lib/eneru/kh")
        monkeypatch.setattr(eneru_utils, "running_in_container", lambda: False)

        assert eneru_utils.runtime_default_ssh_options([]) == [
            "UserKnownHostsFile=/var/lib/eneru/kh"
        ]

    @pytest.mark.unit
    def test_runtime_default_ssh_options_preserves_explicit_file(
        self, monkeypatch
    ):
        monkeypatch.setenv(eneru_utils.KNOWN_HOSTS_ENV, "/var/lib/eneru/kh")
        monkeypatch.setattr(eneru_utils, "running_in_container", lambda: True)

        assert eneru_utils.runtime_default_ssh_options([
            "UserKnownHostsFile=/custom/known_hosts"
        ]) == []
