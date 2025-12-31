"""Tests for remote pre-shutdown command templating and execution."""

import pytest
from unittest.mock import patch, MagicMock, call

from ups_monitor import (
    UPSMonitor,
    Config,
    RemoteServerConfig,
    RemoteCommandConfig,
    MonitorState,
    REMOTE_ACTIONS,
)


class TestRemoteActionTemplates:
    """Test the predefined remote action templates."""

    @pytest.mark.unit
    def test_stop_containers_template_exists(self):
        """Test that stop_containers action template exists."""
        assert "stop_containers" in REMOTE_ACTIONS
        template = REMOTE_ACTIONS["stop_containers"]
        assert "docker" in template
        assert "podman" in template

    @pytest.mark.unit
    def test_stop_vms_template_exists(self):
        """Test that stop_vms action template exists."""
        assert "stop_vms" in REMOTE_ACTIONS
        template = REMOTE_ACTIONS["stop_vms"]
        assert "virsh" in template
        assert "shutdown" in template
        assert "destroy" in template

    @pytest.mark.unit
    def test_stop_proxmox_vms_template_exists(self):
        """Test that stop_proxmox_vms action template exists."""
        assert "stop_proxmox_vms" in REMOTE_ACTIONS
        template = REMOTE_ACTIONS["stop_proxmox_vms"]
        assert "qm" in template
        assert "shutdown" in template

    @pytest.mark.unit
    def test_stop_proxmox_cts_template_exists(self):
        """Test that stop_proxmox_cts action template exists."""
        assert "stop_proxmox_cts" in REMOTE_ACTIONS
        template = REMOTE_ACTIONS["stop_proxmox_cts"]
        assert "pct" in template
        assert "shutdown" in template

    @pytest.mark.unit
    def test_stop_xcpng_vms_template_exists(self):
        """Test that stop_xcpng_vms action template exists."""
        assert "stop_xcpng_vms" in REMOTE_ACTIONS
        template = REMOTE_ACTIONS["stop_xcpng_vms"]
        assert "xe" in template
        assert "vm-shutdown" in template

    @pytest.mark.unit
    def test_stop_esxi_vms_template_exists(self):
        """Test that stop_esxi_vms action template exists."""
        assert "stop_esxi_vms" in REMOTE_ACTIONS
        template = REMOTE_ACTIONS["stop_esxi_vms"]
        assert "vim-cmd" in template
        assert "power.shutdown" in template

    @pytest.mark.unit
    def test_stop_compose_template_exists(self):
        """Test that stop_compose action template exists."""
        assert "stop_compose" in REMOTE_ACTIONS
        template = REMOTE_ACTIONS["stop_compose"]
        assert "compose" in template
        assert "{path}" in template
        assert "{timeout}" in template

    @pytest.mark.unit
    def test_sync_template_exists(self):
        """Test that sync action template exists."""
        assert "sync" in REMOTE_ACTIONS
        template = REMOTE_ACTIONS["sync"]
        assert "sync" in template

    @pytest.mark.unit
    def test_timeout_placeholder_in_templates(self):
        """Test that timeout placeholder is used correctly in templates."""
        templates_with_timeout = [
            "stop_containers",
            "stop_vms",
            "stop_proxmox_vms",
            "stop_proxmox_cts",
            "stop_xcpng_vms",
            "stop_esxi_vms",
            "stop_compose",
        ]

        for action_name in templates_with_timeout:
            template = REMOTE_ACTIONS[action_name]
            assert "{timeout}" in template, f"{action_name} should have timeout placeholder"

    @pytest.mark.unit
    def test_timeout_substitution(self):
        """Test that timeout placeholder is correctly substituted."""
        template = REMOTE_ACTIONS["stop_containers"]
        result = template.format(timeout=60)
        assert "t=60" in result
        assert "{timeout}" not in result

    @pytest.mark.unit
    def test_path_substitution_in_compose(self):
        """Test that path placeholder is correctly substituted in stop_compose."""
        template = REMOTE_ACTIONS["stop_compose"]
        result = template.format(timeout=30, path="/opt/app/docker-compose.yml")
        assert "/opt/app/docker-compose.yml" in result
        assert "{path}" not in result
        assert "t=30" in result


class TestRemotePreShutdownExecution:
    """Test remote pre-shutdown command execution logic."""

    @pytest.fixture
    def remote_monitor(self, minimal_config, tmp_path):
        """Create a monitor configured for remote server testing."""
        minimal_config.logging.state_file = str(tmp_path / "state")
        minimal_config.logging.battery_history_file = str(tmp_path / "history")
        minimal_config.logging.shutdown_flag_file = str(tmp_path / "flag")
        minimal_config.logging.file = None
        minimal_config.behavior.dry_run = False

        monitor = UPSMonitor(minimal_config)
        monitor.state = MonitorState()
        monitor.logger = MagicMock()
        monitor._notification_worker = MagicMock()

        return monitor

    @pytest.mark.unit
    def test_execute_pre_shutdown_with_action(self, remote_monitor):
        """Test executing pre-shutdown with predefined action."""
        server = RemoteServerConfig(
            name="Test Server",
            enabled=True,
            host="192.168.1.50",
            user="root",
            command_timeout=30,
            pre_shutdown_commands=[
                RemoteCommandConfig(action="stop_containers", timeout=60),
            ],
        )

        with patch.object(remote_monitor, "_run_remote_command") as mock_run:
            mock_run.return_value = (True, "")

            result = remote_monitor._execute_remote_pre_shutdown(server)

            assert result is True
            mock_run.assert_called_once()

            # Check that the action was expanded into a command
            call_args = mock_run.call_args
            command = call_args[0][1]  # Second positional arg is the command
            assert "docker" in command or "podman" in command
            assert "t=60" in command  # Timeout substituted

    @pytest.mark.unit
    def test_execute_pre_shutdown_with_custom_command(self, remote_monitor):
        """Test executing pre-shutdown with custom command."""
        server = RemoteServerConfig(
            name="Test Server",
            enabled=True,
            host="192.168.1.50",
            user="root",
            command_timeout=30,
            pre_shutdown_commands=[
                RemoteCommandConfig(command="systemctl stop my-service", timeout=15),
            ],
        )

        with patch.object(remote_monitor, "_run_remote_command") as mock_run:
            mock_run.return_value = (True, "")

            result = remote_monitor._execute_remote_pre_shutdown(server)

            assert result is True
            mock_run.assert_called_once()

            call_args = mock_run.call_args
            command = call_args[0][1]
            assert command == "systemctl stop my-service"

    @pytest.mark.unit
    def test_execute_pre_shutdown_with_stop_compose(self, remote_monitor):
        """Test executing pre-shutdown with stop_compose action."""
        server = RemoteServerConfig(
            name="Test Server",
            enabled=True,
            host="192.168.1.50",
            user="root",
            command_timeout=30,
            pre_shutdown_commands=[
                RemoteCommandConfig(
                    action="stop_compose",
                    path="/opt/myapp/docker-compose.yml",
                    timeout=120
                ),
            ],
        )

        with patch.object(remote_monitor, "_run_remote_command") as mock_run:
            mock_run.return_value = (True, "")

            result = remote_monitor._execute_remote_pre_shutdown(server)

            assert result is True
            mock_run.assert_called_once()

            call_args = mock_run.call_args
            command = call_args[0][1]
            assert "/opt/myapp/docker-compose.yml" in command
            assert "compose" in command

    @pytest.mark.unit
    def test_execute_pre_shutdown_stop_compose_without_path_skipped(self, remote_monitor):
        """Test that stop_compose without path is skipped."""
        server = RemoteServerConfig(
            name="Test Server",
            enabled=True,
            host="192.168.1.50",
            user="root",
            command_timeout=30,
            pre_shutdown_commands=[
                RemoteCommandConfig(action="stop_compose"),  # No path!
            ],
        )

        with patch.object(remote_monitor, "_run_remote_command") as mock_run:
            result = remote_monitor._execute_remote_pre_shutdown(server)

            assert result is True
            # Command should NOT be called since path is missing
            mock_run.assert_not_called()

    @pytest.mark.unit
    def test_execute_pre_shutdown_unknown_action_skipped(self, remote_monitor):
        """Test that unknown action is skipped."""
        server = RemoteServerConfig(
            name="Test Server",
            enabled=True,
            host="192.168.1.50",
            user="root",
            command_timeout=30,
            pre_shutdown_commands=[
                RemoteCommandConfig(action="unknown_action_xyz"),
            ],
        )

        with patch.object(remote_monitor, "_run_remote_command") as mock_run:
            result = remote_monitor._execute_remote_pre_shutdown(server)

            assert result is True
            mock_run.assert_not_called()

    @pytest.mark.unit
    def test_execute_pre_shutdown_uses_server_default_timeout(self, remote_monitor):
        """Test that command uses server's default timeout when not specified."""
        server = RemoteServerConfig(
            name="Test Server",
            enabled=True,
            host="192.168.1.50",
            user="root",
            command_timeout=45,  # Server default
            pre_shutdown_commands=[
                RemoteCommandConfig(action="sync"),  # No timeout specified
            ],
        )

        with patch.object(remote_monitor, "_run_remote_command") as mock_run:
            mock_run.return_value = (True, "")

            remote_monitor._execute_remote_pre_shutdown(server)

            call_args = mock_run.call_args
            timeout = call_args[0][2]  # Third positional arg is timeout
            assert timeout == 45

    @pytest.mark.unit
    def test_execute_pre_shutdown_uses_command_timeout(self, remote_monitor):
        """Test that command uses its own timeout when specified."""
        server = RemoteServerConfig(
            name="Test Server",
            enabled=True,
            host="192.168.1.50",
            user="root",
            command_timeout=30,  # Server default
            pre_shutdown_commands=[
                RemoteCommandConfig(action="sync", timeout=10),  # Custom timeout
            ],
        )

        with patch.object(remote_monitor, "_run_remote_command") as mock_run:
            mock_run.return_value = (True, "")

            remote_monitor._execute_remote_pre_shutdown(server)

            call_args = mock_run.call_args
            timeout = call_args[0][2]
            assert timeout == 10

    @pytest.mark.unit
    def test_execute_pre_shutdown_multiple_commands_in_order(self, remote_monitor):
        """Test that multiple pre-shutdown commands execute in order."""
        server = RemoteServerConfig(
            name="Test Server",
            enabled=True,
            host="192.168.1.50",
            user="root",
            command_timeout=30,
            pre_shutdown_commands=[
                RemoteCommandConfig(action="stop_containers", timeout=60),
                RemoteCommandConfig(command="systemctl stop nginx"),
                RemoteCommandConfig(action="sync"),
            ],
        )

        with patch.object(remote_monitor, "_run_remote_command") as mock_run:
            mock_run.return_value = (True, "")

            remote_monitor._execute_remote_pre_shutdown(server)

            assert mock_run.call_count == 3

            # Check order of calls
            calls = mock_run.call_args_list
            assert "docker" in calls[0][0][1] or "podman" in calls[0][0][1]
            assert "nginx" in calls[1][0][1]
            assert "sync" in calls[2][0][1]

    @pytest.mark.unit
    def test_execute_pre_shutdown_continues_on_failure(self, remote_monitor):
        """Test that pre-shutdown continues even if a command fails."""
        server = RemoteServerConfig(
            name="Test Server",
            enabled=True,
            host="192.168.1.50",
            user="root",
            command_timeout=30,
            pre_shutdown_commands=[
                RemoteCommandConfig(command="failing-command"),
                RemoteCommandConfig(action="sync"),  # Should still run
            ],
        )

        with patch.object(remote_monitor, "_run_remote_command") as mock_run:
            # First command fails, second succeeds
            mock_run.side_effect = [(False, "command failed"), (True, "")]

            result = remote_monitor._execute_remote_pre_shutdown(server)

            assert result is True  # Should still return True (best effort)
            assert mock_run.call_count == 2  # Both commands attempted

    @pytest.mark.unit
    def test_execute_pre_shutdown_empty_list(self, remote_monitor):
        """Test that empty pre_shutdown_commands list returns True."""
        server = RemoteServerConfig(
            name="Test Server",
            enabled=True,
            host="192.168.1.50",
            user="root",
            pre_shutdown_commands=[],
        )

        with patch.object(remote_monitor, "_run_remote_command") as mock_run:
            result = remote_monitor._execute_remote_pre_shutdown(server)

            assert result is True
            mock_run.assert_not_called()

    @pytest.mark.unit
    def test_execute_pre_shutdown_no_action_or_command_skipped(self, remote_monitor):
        """Test that command config without action or command is skipped."""
        server = RemoteServerConfig(
            name="Test Server",
            enabled=True,
            host="192.168.1.50",
            user="root",
            command_timeout=30,
            pre_shutdown_commands=[
                RemoteCommandConfig(),  # No action or command
            ],
        )

        with patch.object(remote_monitor, "_run_remote_command") as mock_run:
            result = remote_monitor._execute_remote_pre_shutdown(server)

            assert result is True
            mock_run.assert_not_called()

    @pytest.mark.unit
    def test_dry_run_skips_remote_commands(self, remote_monitor):
        """Test that dry-run mode logs but doesn't execute remote commands."""
        remote_monitor.config.behavior.dry_run = True

        server = RemoteServerConfig(
            name="Test Server",
            enabled=True,
            host="192.168.1.50",
            user="root",
            command_timeout=30,
            pre_shutdown_commands=[
                RemoteCommandConfig(action="stop_containers"),
            ],
        )

        with patch.object(remote_monitor, "_run_remote_command") as mock_run:
            remote_monitor._execute_remote_pre_shutdown(server)

            # In dry-run mode, _run_remote_command should NOT be called
            mock_run.assert_not_called()

        # Check that DRY-RUN was logged
        log_calls = [str(c) for c in remote_monitor.logger.log.call_args_list]
        assert any("DRY-RUN" in c for c in log_calls)


class TestRunRemoteCommand:
    """Test the _run_remote_command helper method."""

    @pytest.fixture
    def ssh_monitor(self, minimal_config, tmp_path):
        """Create a monitor for SSH testing."""
        minimal_config.logging.state_file = str(tmp_path / "state")
        minimal_config.logging.battery_history_file = str(tmp_path / "history")
        minimal_config.logging.shutdown_flag_file = str(tmp_path / "flag")
        minimal_config.logging.file = None

        monitor = UPSMonitor(minimal_config)
        monitor.state = MonitorState()
        monitor.logger = MagicMock()

        return monitor

    @pytest.mark.unit
    def test_run_remote_command_builds_ssh_command(self, ssh_monitor):
        """Test that SSH command is built correctly."""
        server = RemoteServerConfig(
            name="Test",
            host="192.168.1.50",
            user="admin",
            connect_timeout=10,
            ssh_options=["-o StrictHostKeyChecking=no"],
        )

        with patch("ups_monitor.run_command") as mock_run:
            mock_run.return_value = (0, "", "")

            ssh_monitor._run_remote_command(server, "echo test", 30, "test")

            mock_run.assert_called_once()
            call_args = mock_run.call_args[0][0]
            call_str = " ".join(call_args)

            assert call_args[0] == "ssh"
            assert "-o" in call_args
            assert "StrictHostKeyChecking=no" in call_str
            assert "ConnectTimeout=10" in call_str
            assert "BatchMode=yes" in call_str
            assert "admin@192.168.1.50" in call_args
            assert "echo test" in call_args

    @pytest.mark.unit
    def test_run_remote_command_success(self, ssh_monitor):
        """Test successful remote command execution."""
        server = RemoteServerConfig(host="192.168.1.50", user="root")

        with patch("ups_monitor.run_command") as mock_run:
            mock_run.return_value = (0, "output", "")

            success, error = ssh_monitor._run_remote_command(
                server, "echo test", 30, "test"
            )

            assert success is True
            assert error == ""

    @pytest.mark.unit
    def test_run_remote_command_failure(self, ssh_monitor):
        """Test failed remote command execution."""
        server = RemoteServerConfig(host="192.168.1.50", user="root")

        with patch("ups_monitor.run_command") as mock_run:
            mock_run.return_value = (1, "", "permission denied")

            success, error = ssh_monitor._run_remote_command(
                server, "sudo command", 30, "test"
            )

            assert success is False
            assert "permission denied" in error

    @pytest.mark.unit
    def test_run_remote_command_timeout(self, ssh_monitor):
        """Test remote command timeout."""
        server = RemoteServerConfig(host="192.168.1.50", user="root")

        with patch("ups_monitor.run_command") as mock_run:
            mock_run.return_value = (124, "", "timed out")

            success, error = ssh_monitor._run_remote_command(
                server, "long-command", 30, "test"
            )

            assert success is False
            assert "timed out" in error

    @pytest.mark.unit
    def test_run_remote_command_with_multiple_ssh_options(self, ssh_monitor):
        """Test SSH command with multiple options."""
        server = RemoteServerConfig(
            host="192.168.1.50",
            user="root",
            connect_timeout=5,
            ssh_options=[
                "-o StrictHostKeyChecking=no",
                "-o UserKnownHostsFile=/dev/null",
                "-o LogLevel=ERROR",
            ],
        )

        with patch("ups_monitor.run_command") as mock_run:
            mock_run.return_value = (0, "", "")

            ssh_monitor._run_remote_command(server, "test", 30, "test")

            call_args = mock_run.call_args[0][0]
            call_str = " ".join(call_args)

            assert "StrictHostKeyChecking=no" in call_str
            assert "UserKnownHostsFile=/dev/null" in call_str
            assert "LogLevel=ERROR" in call_str

    @pytest.mark.unit
    def test_run_remote_command_timeout_with_buffer(self, ssh_monitor):
        """Test that timeout passed to run_command includes buffer."""
        server = RemoteServerConfig(host="192.168.1.50", user="root")

        with patch("ups_monitor.run_command") as mock_run:
            mock_run.return_value = (0, "", "")

            ssh_monitor._run_remote_command(server, "test", 30, "test")

            # run_command should be called with timeout + 30 buffer
            call_kwargs = mock_run.call_args[1]
            assert call_kwargs["timeout"] == 60  # 30 + 30 buffer
