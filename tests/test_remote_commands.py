"""Tests for remote pre-shutdown command templating and execution."""

import pytest
from unittest.mock import patch, MagicMock, call

from eneru import (
    UPSGroupMonitor,
    Config,
    RemoteServerConfig,
    RemoteCommandConfig,
    MonitorState,
    REMOTE_ACTIONS,
    FilesystemsConfig,
    UnmountConfig,
)
from eneru.shutdown.remote import RemoteShutdownResult


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
        # qm requires root; sudo is mandatory so non-root SSH users can drive it.
        assert "sudo qm" in template

    @pytest.mark.unit
    def test_stop_proxmox_cts_template_exists(self):
        """Test that stop_proxmox_cts action template exists."""
        assert "stop_proxmox_cts" in REMOTE_ACTIONS
        template = REMOTE_ACTIONS["stop_proxmox_cts"]
        assert "pct" in template
        assert "shutdown" in template
        # pct requires root; sudo is mandatory so non-root SSH users can drive it.
        assert "sudo pct" in template

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
        """Test that timeout placeholder is correctly substituted.

        v5.5: templates take additional placeholders (skip_ids,
        umount_targets, wait_interval) — use the centralized
        render_action() helper so all required kwargs get defaults.
        """
        from eneru.actions import render_action
        result = render_action("stop_containers", timeout=60)
        assert "t=60" in result
        assert "{timeout}" not in result
        assert "{skip_ids}" not in result

    @pytest.mark.unit
    def test_path_substitution_in_compose(self):
        """stop_compose path is shell-quoted by render_action."""
        from eneru.actions import render_action
        result = render_action(
            "stop_compose",
            timeout=30,
            path="/opt/app/docker compose.yml",
        )
        assert "'/opt/app/docker compose.yml'" in result
        assert "{path}" not in result
        assert "t=30" in result

    @pytest.mark.unit
    def test_stop_xcpng_vms_binds_uuid_via_xargs_placeholder(self):
        """The xe template must bind UUIDs into uuid= via xargs -I {},
        not pass them positionally — `xe` parses arguments as key=value
        pairs and silently ignores positional UUIDs."""
        template = REMOTE_ACTIONS["stop_xcpng_vms"]
        rendered = template.format(timeout=120)
        # Both the graceful and force passes must bind UUID via xargs -I {}.
        assert "xargs -r -I {} xe vm-shutdown uuid={}" in rendered
        # Force pass uses xe's key=value form, not --force.
        assert "force=true" in rendered
        assert "--force" not in rendered
        # The bug pattern (uuid= with no following bound value) must not appear.
        assert "uuid= " not in rendered
        assert "uuid=\n" not in rendered
        assert "uuid=2" not in rendered  # not joined with the next arg either

    @pytest.mark.unit
    def test_stop_compose_template_quotes_path_variable_at_use_sites(self):
        """stop_compose quotes "$path" when invoking compose so spaces survive."""
        template = REMOTE_ACTIONS["stop_compose"]
        assert "path={path}" in template
        assert '-f "$path" ps -q' in template
        assert '-f "$path" down -t "$t"' in template


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

        monitor = UPSGroupMonitor(minimal_config)
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
    def test_execute_pre_shutdown_with_use_sudo(self, remote_monitor):
        """use_sudo should render delegated write actions through sudo -n."""
        server = RemoteServerConfig(
            name="Loopback",
            enabled=True,
            host="127.0.0.1",
            user="eneru-loopback",
            use_sudo=True,
            command_timeout=30,
            pre_shutdown_commands=[
                RemoteCommandConfig(action="stop_vms", timeout=60),
            ],
        )

        with patch.object(remote_monitor, "_run_remote_command", return_value=(True, "")) as mock_run:
            remote_monitor._execute_remote_pre_shutdown(server)

        command = mock_run.call_args[0][1]
        assert "sudo -n virsh" in command

    @pytest.mark.unit
    def test_shutdown_command_use_sudo_prefix_is_idempotent(self, remote_monitor):
        """Final shutdown command gets sudo -n unless it already starts with sudo."""
        server = RemoteServerConfig(
            name="Loopback",
            enabled=True,
            host="127.0.0.1",
            user="eneru-loopback",
            use_sudo=True,
            shutdown_command="shutdown -h now",
        )

        with patch.object(remote_monitor, "_run_remote_command", return_value=(True, "")) as mock_run:
            remote_monitor._shutdown_remote_server(server)

        assert mock_run.call_args[0][1] == "sudo -n shutdown -h now"

        server.shutdown_command = "sudo shutdown -h now"
        with patch.object(remote_monitor, "_run_remote_command", return_value=(True, "")) as mock_run:
            remote_monitor._shutdown_remote_server(server)
        assert mock_run.call_args[0][1] == "sudo shutdown -h now"

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
    def test_execute_pre_shutdown_collects_best_effort_failures(self, remote_monitor):
        server = RemoteServerConfig(
            name="Test Server",
            enabled=True,
            host="192.168.1.50",
            user="root",
            command_timeout=30,
            pre_shutdown_commands=[
                RemoteCommandConfig(command="systemctl stop app"),
                RemoteCommandConfig(action="unknown_action"),
            ],
        )

        with patch.object(remote_monitor, "_run_remote_command", return_value=(False, "boom")):
            result = remote_monitor._execute_remote_pre_shutdown(
                server, collect_result=True,
            )

        assert result.attempted == 1
        assert result.failed == 2

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
    def test_stop_compose_without_path_does_not_render_template(self, remote_monitor):
        """5.1.1 fix: the path-presence check now runs BEFORE the
        REMOTE_ACTIONS template is fetched/rendered with shlex.quote("").
        Validate by patching .format on the template string and asserting
        it was never called when path is missing — proves the precondition
        is authoritative rather than a dead-code warning after rendering.
        """
        from eneru import REMOTE_ACTIONS
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
        with patch.dict(
            REMOTE_ACTIONS,
            {"stop_compose": MagicMock(wraps=REMOTE_ACTIONS["stop_compose"])},
        ) as patched:
            with patch.object(remote_monitor, "_run_remote_command") as mock_run:
                remote_monitor._execute_remote_pre_shutdown(server)
            # The template's .format must not have been invoked at all.
            patched["stop_compose"].format.assert_not_called()
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
    def test_stop_compose_path_is_shell_quoted(self, remote_monitor):
        """A path containing shell metacharacters must be shlex-quoted by
        _execute_remote_pre_shutdown before it lands in the rendered command,
        so $(), backticks, and ${...} cannot expand on the remote host."""
        import shlex

        malicious_path = "/tmp/$(rm -rf /)/docker-compose.yml"
        server = RemoteServerConfig(
            name="Test Server",
            enabled=True,
            host="192.168.1.50",
            user="root",
            command_timeout=30,
            pre_shutdown_commands=[
                RemoteCommandConfig(
                    action="stop_compose",
                    path=malicious_path,
                    timeout=30,
                ),
            ],
        )

        with patch.object(remote_monitor, "_run_remote_command") as mock_run:
            mock_run.return_value = (True, "")
            remote_monitor._execute_remote_pre_shutdown(server)

        rendered = mock_run.call_args[0][1]
        # The path must appear in its shlex-quoted form so the remote shell
        # treats it as a literal string.
        assert shlex.quote(malicious_path) in rendered

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

    @pytest.mark.unit
    def test_shutdown_remote_server_returns_structured_failure(self, remote_monitor):
        server = RemoteServerConfig(
            name="Test Server",
            enabled=True,
            host="192.168.1.50",
            user="root",
            command_timeout=30,
        )

        with patch.object(remote_monitor, "_run_remote_command",
                          return_value=(False, "permission denied")):
            result = remote_monitor._shutdown_remote_server(server)

        assert isinstance(result, RemoteShutdownResult)
        assert result.success is False
        assert result.shutdown_sent is False
        assert "permission denied" in result.error

    @pytest.mark.unit
    def test_remote_shutdown_summary_counts_failures(self, remote_monitor):
        servers = [
            RemoteServerConfig(name="ok", enabled=True, host="10.0.0.1", user="root"),
            RemoteServerConfig(name="bad", enabled=True, host="10.0.0.2", user="root"),
        ]
        remote_monitor.config.ups_groups[0].remote_servers = servers

        def fake_shutdown(server):
            return RemoteShutdownResult(
                server=server.name,
                host=server.host,
                shutdown_sent=(server.name == "ok"),
                error="" if server.name == "ok" else "refused",
            )

        with patch.object(remote_monitor, "_shutdown_remote_server",
                          side_effect=fake_shutdown):
            remote_monitor._shutdown_remote_servers()

        log_text = "\n".join(str(c) for c in remote_monitor.logger.log.call_args_list)
        assert "1/2 succeeded" in log_text
        assert "1 failed" in log_text

    @pytest.mark.unit
    def test_pre_shutdown_deadline_skips_late_final_shutdown(self, remote_monitor):
        server = RemoteServerConfig(
            name="slow",
            enabled=True,
            host="10.0.0.3",
            user="root",
            command_timeout=30,
            pre_shutdown_commands=[
                RemoteCommandConfig(command="sleep 999", timeout=1),
                RemoteCommandConfig(command="sleep 999", timeout=1),
            ],
        )

        with patch("eneru.shutdown.remote.time.monotonic", side_effect=[0.0, 11.0]):
            with patch.object(remote_monitor, "_run_remote_command",
                              return_value=(False, "timed out")) as mock_run:
                result = remote_monitor._shutdown_remote_server(server, deadline=10.0)

        assert result.timed_out is True
        assert result.shutdown_sent is False
        assert result.pre_commands.timed_out is True
        assert mock_run.call_count == 1
        assert mock_run.call_args.args[3] == "sleep 999"

    @pytest.mark.unit
    def test_loopback_pre_shutdown_supplies_skip_ids_and_umount_targets(
        self, remote_monitor
    ):
        """Loopback actions get host-side context rendered into templates."""
        remote_monitor.config.ups_groups[0].filesystems = FilesystemsConfig(
            sync_enabled=True,
            unmount=UnmountConfig(
                enabled=True,
                mounts=["/mnt/media", {"path": "/mnt/usb disk", "options": "-l"}],
            ),
        )
        server = RemoteServerConfig(
            name="host-loopback",
            enabled=True,
            host="127.0.0.1",
            user="root",
            is_host_loopback=True,
            pre_shutdown_commands=[
                RemoteCommandConfig(action="stop_containers"),
                RemoteCommandConfig(action="unmount_filesystems"),
            ],
        )

        with patch.object(remote_monitor, "_current_container_ids",
                          return_value={"def456", "abc123"}), \
             patch.object(remote_monitor, "_run_remote_command",
                          return_value=(True, "")) as mock_run:
            remote_monitor._execute_remote_pre_shutdown(server)

        stop_cmd = mock_run.call_args_list[0].args[1]
        umount_cmd = mock_run.call_args_list[1].args[1]
        assert 'skip="abc123,def456"' in stop_cmd
        assert "/mnt/media" in umount_cmd
        assert "/mnt/usb disk" in umount_cmd

    @pytest.mark.unit
    def test_loopback_helpers_fail_closed_to_empty_context(self, remote_monitor):
        """Helper failures should remove optional context, not abort shutdown."""
        with patch.object(remote_monitor, "_current_container_ids",
                          side_effect=RuntimeError("docker unavailable")):
            assert remote_monitor._loopback_skip_ids() == set()

        remote_monitor.config.ups_groups[0].filesystems = FilesystemsConfig(
            sync_enabled=True,
            unmount=UnmountConfig(enabled=False, mounts=["/mnt/media"]),
        )
        assert remote_monitor._loopback_umount_targets() == []


class TestRunRemoteCommand:
    """Test the _run_remote_command helper method."""

    @pytest.fixture
    def ssh_monitor(self, minimal_config, tmp_path):
        """Create a monitor for SSH testing."""
        minimal_config.logging.state_file = str(tmp_path / "state")
        minimal_config.logging.battery_history_file = str(tmp_path / "history")
        minimal_config.logging.shutdown_flag_file = str(tmp_path / "flag")
        minimal_config.logging.file = None

        monitor = UPSGroupMonitor(minimal_config)
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

        with patch("eneru.shutdown.remote.run_command") as mock_run:
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
    def test_run_remote_command_uses_ssh_key_path(self, ssh_monitor):
        """ssh_key_path maps to OpenSSH -i without requiring ssh_options."""
        server = RemoteServerConfig(
            name="Test",
            host="192.168.1.50",
            user="admin",
            ssh_key_path="/var/lib/eneru/ssh/id_ups_shutdown",
        )

        with patch("eneru.shutdown.remote.run_command") as mock_run:
            mock_run.return_value = (0, "", "")

            ssh_monitor._run_remote_command(server, "echo test", 30, "test")

            call_args = mock_run.call_args[0][0]
            assert call_args[0:3] == [
                "ssh",
                "-i",
                "/var/lib/eneru/ssh/id_ups_shutdown",
            ]

    @pytest.mark.unit
    def test_run_remote_command_success(self, ssh_monitor):
        """Test successful remote command execution."""
        server = RemoteServerConfig(host="192.168.1.50", user="root")

        with patch("eneru.shutdown.remote.run_command") as mock_run:
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

        with patch("eneru.shutdown.remote.run_command") as mock_run:
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

        with patch("eneru.shutdown.remote.run_command") as mock_run:
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

        with patch("eneru.shutdown.remote.run_command") as mock_run:
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

        with patch("eneru.shutdown.remote.run_command") as mock_run:
            mock_run.return_value = (0, "", "")

            ssh_monitor._run_remote_command(server, "test", 30, "test")

            # run_command should be called with timeout + 30 buffer
            call_kwargs = mock_run.call_args[1]
            assert call_kwargs["timeout"] == 60  # 30 + 30 buffer

    @pytest.mark.unit
    def test_run_remote_command_deadline_already_expired(self, ssh_monitor):
        """Expired phase deadlines fail before opening a new SSH command."""
        server = RemoteServerConfig(host="192.168.1.50", user="root")

        with patch("eneru.shutdown.remote.time.monotonic", return_value=20.0), \
             patch("eneru.shutdown.remote.run_command") as mock_run:
            success, error = ssh_monitor._run_remote_command(
                server, "shutdown -h now", 30, "shutdown", deadline=10.0,
            )

        assert success is False
        assert error == "remote shutdown deadline exceeded"
        mock_run.assert_not_called()

    @pytest.mark.unit
    def test_run_remote_command_reports_deadline_capped_timeout(self, ssh_monitor):
        """Timeout messages should show when the phase deadline shortened them."""
        server = RemoteServerConfig(host="192.168.1.50", user="root")

        with patch("eneru.shutdown.remote.time.monotonic", return_value=9.1), \
             patch("eneru.shutdown.remote.run_command",
                   return_value=(124, "", "timed out")) as mock_run:
            success, error = ssh_monitor._run_remote_command(
                server, "sync", 30, "sync", deadline=11.0,
            )

        assert success is False
        assert "capped by phase deadline" in error
        assert mock_run.call_args.kwargs["timeout"] == 1
