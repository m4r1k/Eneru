"""Tests for remote pre-shutdown command templating and execution."""

import threading
import time

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
from eneru import utils as eneru_utils
from eneru.shutdown.remote import (
    REMOTE_PATH_PREFIX,
    RemoteShutdownMixin,
    RemoteShutdownResult,
    loopback_poweroff_sent,
    with_sudo,
)


class TestLoopbackPoweroffSent:
    """ISS-005: the shared predicate the monitor and redundancy loopback paths
    both use to decide whether the delegated host poweroff was delivered. Unlike
    RemoteShutdownResult.success it ignores Phase-A drain failures."""

    @pytest.mark.unit
    def test_true_when_poweroff_delivered(self):
        result = RemoteShutdownResult(
            server="host-loopback", host="127.0.0.1", shutdown_sent=True,
        )
        assert loopback_poweroff_sent(result) is True

    @pytest.mark.unit
    def test_true_even_when_phase_a_crashed_or_errored(self):
        """The poweroff went out; a Phase-A drain crash/error must NOT flip the
        predicate to False (that is the exact monitor/redundancy divergence)."""
        crashed = RemoteShutdownResult(
            server="host-loopback", host="127.0.0.1",
            shutdown_sent=True, crashed=True, error="drain crashed",
        )
        assert crashed.success is False          # success is stricter
        assert loopback_poweroff_sent(crashed) is True

    @pytest.mark.unit
    def test_false_when_poweroff_not_sent(self):
        result = RemoteShutdownResult(
            server="host-loopback", host="127.0.0.1",
            shutdown_sent=False, error="ssh failed",
        )
        assert loopback_poweroff_sent(result) is False

    @pytest.mark.unit
    def test_false_when_timed_out(self):
        result = RemoteShutdownResult(
            server="host-loopback", host="127.0.0.1",
            shutdown_sent=True, timed_out=True,
        )
        assert loopback_poweroff_sent(result) is False

    @pytest.mark.unit
    def test_redundancy_uses_the_shared_helper(self):
        """Convergence guard: redundancy must call the shared module helper, not
        re-introduce a private copy that can drift from the monitor path."""
        import eneru.redundancy as redundancy
        assert redundancy.loopback_poweroff_sent is loopback_poweroff_sent
        assert not hasattr(
            redundancy.RedundancyGroupExecutor, "_loopback_poweroff_sent"
        )


class TestSelectLoopbackResults:
    """ISS-013: the loopback-result selector shared by the monitor and
    redundancy delegated-shutdown paths — previously a verbatim inline
    list-comprehension copy-pasted in both."""

    @staticmethod
    def _server(**kw):
        from types import SimpleNamespace
        base = dict(enabled=True, is_host_loopback=True,
                    name=None, host="127.0.0.1")
        base.update(kw)
        return SimpleNamespace(**base)

    @pytest.mark.unit
    def test_matches_enabled_loopback_by_name_host_pair(self):
        from eneru.shutdown.remote import select_loopback_results
        srv = self._server(name="lo", host="127.0.0.1")
        r_match = RemoteShutdownResult(
            server="lo", host="127.0.0.1", shutdown_sent=True)
        r_other = RemoteShutdownResult(
            server="other", host="10.0.0.9", shutdown_sent=True)
        assert select_loopback_results([srv], [r_match, r_other]) == [r_match]

    @pytest.mark.unit
    def test_falls_back_to_host_when_name_unset(self):
        from eneru.shutdown.remote import select_loopback_results
        srv = self._server(name=None, host="127.0.0.1")
        r = RemoteShutdownResult(
            server="127.0.0.1", host="127.0.0.1", shutdown_sent=True)
        assert select_loopback_results([srv], [r]) == [r]

    @pytest.mark.unit
    def test_excludes_disabled_and_non_loopback_servers(self):
        from eneru.shutdown.remote import select_loopback_results
        disabled = self._server(name="lo", host="127.0.0.1", enabled=False)
        not_lo = self._server(name="lo", host="127.0.0.1",
                              is_host_loopback=False)
        r = RemoteShutdownResult(
            server="lo", host="127.0.0.1", shutdown_sent=True)
        assert select_loopback_results([disabled, not_lo], [r]) == []

    @pytest.mark.unit
    def test_monitor_and_redundancy_use_the_shared_helper(self):
        import eneru.monitor as monitor
        import eneru.redundancy as redundancy
        from eneru.shutdown.remote import select_loopback_results
        assert monitor.select_loopback_results is select_loopback_results
        assert redundancy.select_loopback_results is select_loopback_results


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
    def test_custom_command_use_sudo_prefix_is_idempotent(self, remote_monitor):
        """6.2: use_sudo also runs custom pre-shutdown commands via sudo -n,
        leaving commands that already start with sudo untouched."""
        server = RemoteServerConfig(
            name="NAS", enabled=True, host="10.0.0.2", user="admin",
            use_sudo=True, command_timeout=30,
            pre_shutdown_commands=[
                RemoteCommandConfig(command="systemctl stop app"),
                RemoteCommandConfig(command="sudo -n systemctl stop db"),
            ],
        )
        with patch.object(remote_monitor, "_run_remote_command",
                          return_value=(True, "")) as mock_run:
            remote_monitor._execute_remote_pre_shutdown(server)
        sent = [c[0][1] for c in mock_run.call_args_list]
        assert sent == ["sudo -n systemctl stop app", "sudo -n systemctl stop db"]

        server.use_sudo = False
        with patch.object(remote_monitor, "_run_remote_command",
                          return_value=(True, "")) as mock_run:
            remote_monitor._execute_remote_pre_shutdown(server)
        assert mock_run.call_args_list[0][0][1] == "systemctl stop app"

    @pytest.mark.unit
    def test_custom_command_log_shows_the_sudoed_command(self, remote_monitor):
        """F-138/R6: the step log names the command that is really sent."""
        server = RemoteServerConfig(
            name="NAS", enabled=True, host="10.0.0.2", user="admin",
            use_sudo=True, command_timeout=30,
            pre_shutdown_commands=[RemoteCommandConfig(command="systemctl stop app")],
        )
        with patch.object(remote_monitor, "_run_remote_command",
                          return_value=(True, "")):
            remote_monitor._execute_remote_pre_shutdown(server)
        log_text = "\n".join(str(c) for c in remote_monitor.logger.log.call_args_list)
        assert "[1/1] sudo -n systemctl stop app (timeout: 30s)" in log_text

    @pytest.mark.unit
    @pytest.mark.parametrize("dry_run", [False, True])
    def test_loopback_poweroff_honors_use_sudo(self, remote_monitor, dry_run):
        """F-123: the documented non-root `eneru-loopback` host user needs the
        final poweroff sent through sudo, on the loopback path too."""
        remote_monitor.config.behavior.dry_run = dry_run
        server = RemoteServerConfig(
            name="host-loopback", enabled=True, host="127.0.0.1",
            user="eneru-loopback", use_sudo=True, is_host_loopback=True,
            shutdown_command="shutdown -h now",
        )
        result = RemoteShutdownResult(server="host-loopback", host="127.0.0.1")
        with patch.object(remote_monitor, "_run_remote_command",
                          return_value=(True, "")) as mock_run:
            remote_monitor._shutdown_loopback_command(server, result)
        log_text = "\n".join(str(c) for c in remote_monitor.logger.log.call_args_list)
        if dry_run:
            mock_run.assert_not_called()
            assert ("[DRY-RUN] Would send command 'sudo -n shutdown -h now' "
                    "to eneru-loopback@127.0.0.1") in log_text
        else:
            assert mock_run.call_args[0][1] == "sudo -n shutdown -h now"
            assert mock_run.call_args.kwargs["is_final_shutdown"] is True
        assert result.shutdown_sent is True

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
    def test_slow_pre_phase_still_attempts_final_shutdown(self, remote_monitor):
        """H8: a slow pre-shutdown phase that exhausts its reserved slice must
        NOT starve or skip the final poweroff. As long as the FULL phase
        deadline isn't blown, the shutdown command is still attempted."""
        from eneru.shutdown.remote import RemotePreShutdownResult
        server = RemoteServerConfig(
            name="slow", enabled=True, host="10.0.0.3", user="root",
            command_timeout=30,
            pre_shutdown_commands=[
                RemoteCommandConfig(command="sleep 999", timeout=1),
            ],
        )
        # Pre-phase reports timed_out (used up its reserved budget); the full
        # deadline is far away (monotonic pinned to 0, deadline 100).
        pre_result = RemotePreShutdownResult(attempted=1, failed=1, timed_out=True)
        with patch.object(remote_monitor, "_execute_remote_pre_shutdown",
                          return_value=pre_result), \
             patch.object(remote_monitor, "_run_remote_command",
                          return_value=(True, "")) as mock_run, \
             patch("eneru.shutdown.remote.time.monotonic", return_value=0.0):
            result = remote_monitor._shutdown_remote_server(server, deadline=100.0)

        assert mock_run.call_count == 1          # poweroff attempted
        assert result.shutdown_sent is True

    @pytest.mark.unit
    def test_poweroff_reserve_survives_hung_pre_commands(self, remote_monitor):
        """F-127 / H8, end to end with a fake clock: every hung pre-command
        eats the whole time it is given. The final poweroff's slice
        (command_timeout + SSH buffer = 60s of the 100s phase) must still be
        left when the pre-phase is cut off, so the poweroff runs. Without the
        reserve, the first hung step would burn all 100s and the host would
        never be told to power off."""
        clock = {"now": 0.0}
        sent = []

        def fake_run_command(cmd, timeout=None, **_kw):
            sent.append((cmd[-1], timeout))
            clock["now"] += timeout            # hangs until its timeout
            return 124, "", ""

        server = RemoteServerConfig(
            name="slow", enabled=True, host="10.0.0.3", user="root",
            command_timeout=30,
            pre_shutdown_commands=[
                RemoteCommandConfig(command="sleep 999", timeout=60),
                RemoteCommandConfig(command="sleep 998", timeout=60),
            ],
        )
        with patch("eneru.shutdown.remote.run_command", side_effect=fake_run_command), \
             patch("eneru.shutdown.remote.time.monotonic",
                   side_effect=lambda: clock["now"]):
            result = remote_monitor._shutdown_remote_server(server, deadline=100.0)

        # Pre-phase: capped at the 40s left before the reserve, then stopped.
        assert sent[0] == (REMOTE_PATH_PREFIX + "sleep 999", 40)
        assert result.pre_commands.timed_out is True
        # The poweroff still ran, with the full reserved slice.
        assert sent[-1] == (REMOTE_PATH_PREFIX + "sudo shutdown -h now", 60)
        assert len(sent) == 2
        assert result.timed_out is False

    @pytest.mark.unit
    def test_full_deadline_blown_skips_final_shutdown(self, remote_monitor):
        """If the FULL phase deadline is already exceeded, the poweroff is
        skipped -- the phase is genuinely out of time."""
        from eneru.shutdown.remote import RemotePreShutdownResult
        server = RemoteServerConfig(
            name="slow", enabled=True, host="10.0.0.3", user="root",
            command_timeout=30,
            pre_shutdown_commands=[
                RemoteCommandConfig(command="sleep 999", timeout=1),
            ],
        )
        pre_result = RemotePreShutdownResult(attempted=1, failed=0, timed_out=False)
        with patch.object(remote_monitor, "_execute_remote_pre_shutdown",
                          return_value=pre_result), \
             patch.object(remote_monitor, "_run_remote_command",
                          return_value=(True, "")) as mock_run, \
             patch("eneru.shutdown.remote.time.monotonic", return_value=200.0):
            result = remote_monitor._shutdown_remote_server(server, deadline=100.0)

        assert mock_run.call_count == 0          # poweroff skipped (out of time)
        assert result.shutdown_sent is False
        assert result.timed_out is True

    @pytest.mark.unit
    def test_pre_shutdown_skips_when_deadline_already_exceeded(self, remote_monitor):
        """Pre-shutdown commands are skipped (timed_out) when the phase deadline
        is already blown on entry -- nothing runs."""
        server = RemoteServerConfig(
            name="s", enabled=True, host="10.0.0.3", user="root",
            pre_shutdown_commands=[RemoteCommandConfig(command="echo hi", timeout=5)],
        )
        with patch.object(remote_monitor, "_remote_deadline_exceeded",
                          return_value=True), \
             patch.object(remote_monitor, "_run_remote_command") as mock_run:
            result = remote_monitor._execute_remote_pre_shutdown(
                server, collect_result=True, deadline=10.0)
        assert result.timed_out is True
        mock_run.assert_not_called()

    @pytest.mark.unit
    def test_pre_shutdown_stops_when_deadline_exceeded_midway(self, remote_monitor):
        """Once the deadline passes after a pre-command, the rest are skipped."""
        server = RemoteServerConfig(
            name="s", enabled=True, host="10.0.0.3", user="root",
            pre_shutdown_commands=[
                RemoteCommandConfig(command="echo a", timeout=5),
                RemoteCommandConfig(command="echo b", timeout=5),
            ],
        )
        with patch.object(remote_monitor, "_remote_deadline_exceeded",
                          side_effect=[False, True]), \
             patch.object(remote_monitor, "_run_remote_command",
                          return_value=(True, "")) as mock_run:
            result = remote_monitor._execute_remote_pre_shutdown(
                server, collect_result=True, deadline=10.0)
        assert result.timed_out is True
        assert mock_run.call_count == 1          # only the first command ran

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
    def test_regular_remote_unmount_uses_command_mounts(self, remote_monitor):
        """Regular remote servers must provide mounts on the command itself."""
        server = RemoteServerConfig(
            name="Storage",
            enabled=True,
            host="192.168.1.90",
            user="root",
            command_timeout=30,
            pre_shutdown_commands=[
                RemoteCommandConfig(
                    action="unmount_filesystems",
                    timeout=20,
                    mounts=[
                        {"path": "/mnt/media", "options": ""},
                        {"path": "/mnt/backup disk", "options": "-l"},
                    ],
                ),
            ],
        )

        with patch.object(remote_monitor, "_run_remote_command",
                          return_value=(True, "")) as mock_run:
            remote_monitor._execute_remote_pre_shutdown(server)

        command = mock_run.call_args.args[1]
        assert "/mnt/media" in command
        assert "/mnt/backup disk" in command
        assert "umount -l" in command

    @pytest.mark.unit
    def test_regular_remote_unmount_without_mounts_is_skipped(self, remote_monitor):
        server = RemoteServerConfig(
            name="Storage",
            enabled=True,
            host="192.168.1.90",
            user="root",
            pre_shutdown_commands=[
                RemoteCommandConfig(action="unmount_filesystems"),
            ],
        )

        with patch.object(remote_monitor, "_run_remote_command") as mock_run:
            result = remote_monitor._execute_remote_pre_shutdown(
                server, collect_result=True,
            )

        mock_run.assert_not_called()
        assert result.failed == 1

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


class TestLoopbackShutdownOrdering:
    """v5.5 loopback ordering invariant: an ``is_host_loopback: true``
    delegate ALWAYS brackets the non-loopback remotes.

    Pre-actions (sync, unmount, stop local VMs/containers) run BEFORE
    any peer remote so the local drain finishes while peer remotes are
    still alive. Host poweroff runs AFTER every peer so the eneru host
    outlives every remote it might have depended on. The loopback's
    ``shutdown_order`` field is ignored at execution time.

    Regression for the v5.5.0-rc7 test on 2026-05-18 where NAS
    shutdown was sent first and the host NFS unmount of NAS mounts
    hung 32s afterward.
    """

    @pytest.fixture
    def remote_monitor(self, minimal_config, tmp_path):
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

    @staticmethod
    def _record_calls(monitor):
        """Patch _run_remote_command to record (host, command) in order."""
        calls: list[tuple[str, str]] = []

        def fake_run(server, command, timeout, description, **kwargs):
            calls.append((server.host, command))
            return (True, "")

        return calls, patch.object(monitor, "_run_remote_command", side_effect=fake_run)

    @pytest.mark.unit
    def test_loopback_pre_runs_before_regular_remote(self, remote_monitor):
        """Loopback pre_shutdown_commands MUST run before a peer remote's
        shutdown_command. The rc7 bug had this reversed."""
        loopback = RemoteServerConfig(
            name="host-loopback",
            enabled=True,
            host="127.0.0.1",
            user="root",
            is_host_loopback=True,
            shutdown_command="shutdown -h now",
            pre_shutdown_commands=[
                RemoteCommandConfig(command="echo drain-local"),
            ],
        )
        nas = RemoteServerConfig(
            name="NAS",
            enabled=True,
            host="10.0.0.10",
            user="root",
            shutdown_order=1,
            shutdown_command="poweroff",
        )
        remote_monitor.config.ups_groups[0].remote_servers = [loopback, nas]

        calls, run_patch = self._record_calls(remote_monitor)
        with run_patch:
            remote_monitor._shutdown_remote_servers()

        # Expected ordering: loopback pre-action, NAS shutdown, loopback shutdown
        hosts = [host for host, _ in calls]
        assert hosts == ["127.0.0.1", "10.0.0.10", "127.0.0.1"]

        # And the loopback's drain comes from its pre_shutdown_commands,
        # not its final shutdown_command.
        assert calls[0][1] == "echo drain-local"
        assert calls[1][1] == "poweroff"
        assert calls[2][1] == "shutdown -h now"

    @pytest.mark.unit
    def test_loopback_pre_runs_before_lower_ordered_regular(self, remote_monitor):
        """Even when a regular remote has a very low shutdown_order
        (e.g. -1, which would sort first under the v5.4 algorithm),
        the loopback pre-actions still run first."""
        loopback = RemoteServerConfig(
            name="host-loopback",
            enabled=True,
            host="127.0.0.1",
            user="root",
            is_host_loopback=True,
            shutdown_command="shutdown -h now",
            pre_shutdown_commands=[
                RemoteCommandConfig(command="echo drain"),
            ],
        )
        # parallel=False on a single regular yields a unique negative order
        # via compute_effective_order — the very pattern that produced
        # the rc7 misordering when the loopback had order=999.
        nas = RemoteServerConfig(
            name="NAS",
            enabled=True,
            host="10.0.0.10",
            user="root",
            parallel=False,
            shutdown_command="poweroff",
        )
        remote_monitor.config.ups_groups[0].remote_servers = [loopback, nas]

        calls, run_patch = self._record_calls(remote_monitor)
        with run_patch:
            remote_monitor._shutdown_remote_servers()

        hosts = [host for host, _ in calls]
        assert hosts == ["127.0.0.1", "10.0.0.10", "127.0.0.1"]

    @pytest.mark.unit
    def test_loopback_shutdown_order_field_is_ignored(self, remote_monitor):
        """A loopback entry with shutdown_order=1 (would normally run
        first) still has its poweroff deferred to the end."""
        loopback = RemoteServerConfig(
            name="host-loopback",
            enabled=True,
            host="127.0.0.1",
            user="root",
            is_host_loopback=True,
            shutdown_order=1,  # explicitly low; runtime must ignore
            shutdown_command="shutdown -h now",
            pre_shutdown_commands=[
                RemoteCommandConfig(command="echo drain"),
            ],
        )
        nas = RemoteServerConfig(
            name="NAS",
            enabled=True,
            host="10.0.0.10",
            user="root",
            shutdown_order=50,  # explicitly higher than the loopback
            shutdown_command="poweroff",
        )
        remote_monitor.config.ups_groups[0].remote_servers = [loopback, nas]

        calls, run_patch = self._record_calls(remote_monitor)
        with run_patch:
            remote_monitor._shutdown_remote_servers()

        hosts = [host for host, _ in calls]
        # Loopback pre first, NAS shutdown second, loopback poweroff last —
        # NOT loopback-then-NAS as the shutdown_order field would suggest.
        assert hosts == ["127.0.0.1", "10.0.0.10", "127.0.0.1"]

    @pytest.mark.unit
    def test_remote_only_setup_unchanged_by_partition(self, remote_monitor):
        """Remote-only configs (no loopback anywhere) MUST execute exactly
        as in v5.4: phases derived from shutdown_order, ascending."""
        nas = RemoteServerConfig(
            name="NAS",
            enabled=True,
            host="10.0.0.10",
            user="root",
            shutdown_order=5,
            shutdown_command="poweroff",
        )
        backup = RemoteServerConfig(
            name="Backup",
            enabled=True,
            host="10.0.0.11",
            user="root",
            shutdown_order=10,
            shutdown_command="poweroff",
        )
        remote_monitor.config.ups_groups[0].remote_servers = [nas, backup]

        calls, run_patch = self._record_calls(remote_monitor)
        with run_patch:
            remote_monitor._shutdown_remote_servers()

        hosts = [host for host, _ in calls]
        # order=5 before order=10, both shutdown_commands only (no pre).
        assert hosts == ["10.0.0.10", "10.0.0.11"]

    @pytest.mark.unit
    def test_loopback_without_pre_skips_phase_a(self, remote_monitor):
        """A loopback with no pre_shutdown_commands skips Phase A
        entirely — only the host poweroff runs in Phase C."""
        loopback = RemoteServerConfig(
            name="host-loopback",
            enabled=True,
            host="127.0.0.1",
            user="root",
            is_host_loopback=True,
            shutdown_command="shutdown -h now",
            pre_shutdown_commands=[],
        )
        nas = RemoteServerConfig(
            name="NAS",
            enabled=True,
            host="10.0.0.10",
            user="root",
            shutdown_command="poweroff",
        )
        remote_monitor.config.ups_groups[0].remote_servers = [loopback, nas]

        calls, run_patch = self._record_calls(remote_monitor)
        with run_patch:
            remote_monitor._shutdown_remote_servers()

        hosts = [host for host, _ in calls]
        assert hosts == ["10.0.0.10", "127.0.0.1"]

    @pytest.mark.unit
    def test_loopback_result_aggregates_pre_and_post(self, remote_monitor):
        """The single loopback RemoteShutdownResult must reflect BOTH
        the pre-actions outcome and the final shutdown command outcome."""
        loopback = RemoteServerConfig(
            name="host-loopback",
            enabled=True,
            host="127.0.0.1",
            user="root",
            is_host_loopback=True,
            shutdown_command="shutdown -h now",
            pre_shutdown_commands=[
                RemoteCommandConfig(command="echo drain"),
            ],
        )
        remote_monitor.config.ups_groups[0].remote_servers = [loopback]

        _calls, run_patch = self._record_calls(remote_monitor)
        with run_patch:
            results = remote_monitor._shutdown_remote_servers()

        assert len(results) == 1
        lb_result = results[0]
        assert lb_result.server == "host-loopback"
        assert lb_result.shutdown_sent is True
        assert lb_result.pre_commands.attempted == 1
        assert lb_result.pre_commands.failed == 0
        assert lb_result.success is True

    @pytest.mark.unit
    def test_loopback_dry_run_marks_result_and_skips_real_ssh(
        self, remote_monitor
    ):
        """Dry-run path on the loopback: result.dry_run is True,
        result.shutdown_sent is True, no real SSH command issued."""
        remote_monitor.config.behavior.dry_run = True
        loopback = RemoteServerConfig(
            name="host-loopback",
            enabled=True,
            host="127.0.0.1",
            user="root",
            is_host_loopback=True,
            shutdown_command="shutdown -h now",
        )
        remote_monitor.config.ups_groups[0].remote_servers = [loopback]

        with patch.object(remote_monitor, "_run_remote_command") as mock_run:
            results = remote_monitor._shutdown_remote_servers()

        assert len(results) == 1
        lb_result = results[0]
        assert lb_result.dry_run is True
        assert lb_result.shutdown_sent is True
        # No real SSH command — dry-run short-circuits before _run_remote_command.
        mock_run.assert_not_called()

    @pytest.mark.unit
    def test_loopback_shutdown_command_failure_records_error(
        self, remote_monitor
    ):
        """When the host poweroff SSH call fails, result.error captures
        the SSH error and result.success is False — the per-server
        notification path fires (✅  for start, ❌  for failure)."""
        loopback = RemoteServerConfig(
            name="host-loopback",
            enabled=True,
            host="127.0.0.1",
            user="root",
            is_host_loopback=True,
            shutdown_command="shutdown -h now",
        )
        remote_monitor.config.ups_groups[0].remote_servers = [loopback]

        with patch.object(remote_monitor, "_run_remote_command",
                          return_value=(False, "ssh: connection refused")):
            results = remote_monitor._shutdown_remote_servers()

        assert len(results) == 1
        lb_result = results[0]
        assert lb_result.shutdown_sent is False
        assert "connection refused" in lb_result.error
        assert lb_result.success is False

    @pytest.mark.unit
    def test_loopback_phase_a_exception_does_not_skip_phase_c(
        self, remote_monitor
    ):
        """A Python exception during Phase A (pre-actions) MUST NOT
        abort the orchestration before Phase C — the host poweroff
        still has to fire. Regression for the local reviewer's P1
        finding: pre-fix, Phase A was unguarded and a bubbling
        AttributeError would skip the entire host poweroff."""
        loopback = RemoteServerConfig(
            name="host-loopback",
            enabled=True,
            host="127.0.0.1",
            user="root",
            is_host_loopback=True,
            shutdown_command="shutdown -h now",
            pre_shutdown_commands=[
                RemoteCommandConfig(command="echo drain"),
            ],
        )
        remote_monitor.config.ups_groups[0].remote_servers = [loopback]
        remote_monitor._shutdown_progress.start("test outage")
        phase_a_snapshots = []

        def fail_pre_actions(*_args, **_kwargs):
            phase_a_snapshots.append(
                remote_monitor._shutdown_progress.snapshot())
            raise AttributeError("simulated crash")

        # Phase A raises mid-run; Phase C still has to send shutdown.
        with patch.object(remote_monitor, "_execute_remote_pre_shutdown",
                          side_effect=fail_pre_actions), \
             patch.object(remote_monitor, "_run_remote_command",
                          return_value=(True, "")) as mock_run:
            results = remote_monitor._shutdown_remote_servers()

        live_row = phase_a_snapshots[0]["remotes"][0]
        assert live_row["state"] == "running"
        assert live_row["finishedAt"] is None
        # Phase C ran — the shutdown_command was issued.
        assert mock_run.call_count == 1
        assert mock_run.call_args.args[1] == "shutdown -h now"
        # Result reflects: crashed in pre, but shutdown_sent in post.
        lb_result = results[0]
        assert lb_result.crashed is True
        assert "simulated crash" in lb_result.pre_commands.error
        assert lb_result.shutdown_sent is True
        final_row = remote_monitor._shutdown_progress.snapshot()["remotes"][0]
        assert final_row["startedAt"] == live_row["startedAt"]
        assert final_row["state"] == "failed"
        assert final_row["outcome"] == "command-sent"

    @pytest.mark.unit
    def test_peer_orchestration_exception_still_powers_off_the_host(
        self, remote_monitor
    ):
        """F-118: a crash in Phase B (regular remotes) must not skip Phase C.
        The loopback poweroff is still sent, its progress row is finished,
        and the unfinished regulars come back as crashed rows next to the
        loopback's result (callers need its shutdown_sent)."""
        loopback = RemoteServerConfig(
            name="host-loopback", enabled=True, host="127.0.0.1",
            user="root", is_host_loopback=True,
            shutdown_command="shutdown -h now",
        )
        nas = RemoteServerConfig(
            name="NAS", enabled=True, host="10.0.0.10", user="root",
            shutdown_command="poweroff",
        )
        remote_monitor.config.ups_groups[0].remote_servers = [loopback, nas]
        remote_monitor._shutdown_progress.start("test outage")

        with patch.object(
                remote_monitor, "_shutdown_servers_parallel",
                side_effect=RuntimeError("thread setup failed")), \
                patch.object(remote_monitor, "_run_remote_command",
                             return_value=(True, "")) as run:
            results = remote_monitor._shutdown_remote_servers()

        sent = [c.args[1] for c in run.call_args_list]
        assert sent == ["shutdown -h now"]  # Phase C still ran
        by_name = {r.server: r for r in results}
        assert by_name["host-loopback"].shutdown_sent is True
        assert by_name["NAS"].crashed is True
        assert by_name["NAS"].success is False
        assert "thread setup failed" in by_name["NAS"].error
        row = remote_monitor._shutdown_progress.snapshot()["remotes"][0]
        assert row["finishedAt"] is not None
        assert row["outcome"] == "command-sent"

    @pytest.mark.unit
    def test_phase_b_crash_keeps_results_of_finished_phases(
        self, remote_monitor
    ):
        """Only servers of the crashed (and later) phases become crashed
        rows; an earlier phase's real result is kept as is."""
        first = RemoteServerConfig(
            name="first", enabled=True, host="10.0.0.1", user="root",
            shutdown_order=1)
        second = RemoteServerConfig(
            name="second", enabled=True, host="10.0.0.2", user="root",
            shutdown_order=2)
        remote_monitor.config.ups_groups[0].remote_servers = [first, second]
        done = RemoteShutdownResult(
            server="first", host="10.0.0.1", shutdown_sent=True)
        with patch.object(
                remote_monitor, "_shutdown_servers_parallel",
                side_effect=[[done], RuntimeError("boom")]):
            results = remote_monitor._shutdown_remote_servers()
        assert results[0] is done
        assert [r.server for r in results] == ["first", "second"]
        assert results[1].crashed is True and not results[1].completed

    @pytest.mark.unit
    def test_loopback_phase_c_exception_does_not_skip_other_loopbacks(
        self, remote_monitor
    ):
        """K8s multi-pod corner case: two loopback delegates, the first
        crashes in Phase C — the second loopback's poweroff still has
        to fire."""
        lb1 = RemoteServerConfig(
            name="lb1", enabled=True, host="127.0.0.1", user="root",
            is_host_loopback=True, shutdown_command="shutdown -h now",
        )
        lb2 = RemoteServerConfig(
            name="lb2", enabled=True, host="127.0.0.2", user="root",
            is_host_loopback=True, shutdown_command="shutdown -h now",
        )
        remote_monitor.config.ups_groups[0].remote_servers = [lb1, lb2]

        call_log: list[str] = []

        def fake_loopback_cmd(server, result):
            call_log.append(server.host)
            if server is lb1:
                raise RuntimeError("simulated lb1 crash")
            result.shutdown_sent = True

        with patch.object(remote_monitor, "_shutdown_loopback_command",
                          side_effect=fake_loopback_cmd):
            results = remote_monitor._shutdown_remote_servers()

        # Both poweroff attempts were made; lb2 succeeded despite lb1's crash.
        assert call_log == ["127.0.0.1", "127.0.0.2"]
        by_host = {r.host: r for r in results}
        assert by_host["127.0.0.1"].crashed is True
        assert "lb1 crash" in by_host["127.0.0.1"].error
        assert by_host["127.0.0.2"].shutdown_sent is True

    @pytest.mark.unit
    def test_loopback_poweroff_fires_per_server_notification(
        self, remote_monitor
    ):
        """v5.5 symmetry with regular remotes: the loopback poweroff
        sends a 'Remote Shutdown Starting' notification BEFORE the SSH
        call, so the notification reaches Discord/Slack/etc. even if
        the host goes down a second later."""
        loopback = RemoteServerConfig(
            name="host-loopback",
            enabled=True,
            host="127.0.0.1",
            user="root",
            is_host_loopback=True,
            shutdown_command="shutdown -h now",
        )
        remote_monitor.config.ups_groups[0].remote_servers = [loopback]

        with patch.object(remote_monitor, "_send_notification") as mock_notify, \
             patch.object(remote_monitor, "_run_remote_command",
                          return_value=(True, "")):
            remote_monitor._shutdown_remote_servers()

        # The starting notification fires (NOTIFY_INFO category="shutdown").
        starts = [
            c for c in mock_notify.call_args_list
            if "Remote Shutdown Starting" in c.args[0]
        ]
        assert len(starts) == 1
        assert "host-loopback" in starts[0].args[0]

    @pytest.mark.unit
    def test_loopback_only_no_pre_no_phase_header(self, remote_monitor):
        """A single loopback with no pre_shutdown_commands and no peer
        remotes produces num_phases==1; the Phase C header is skipped
        but the shutdown_command still runs."""
        loopback = RemoteServerConfig(
            name="host-loopback", enabled=True, host="127.0.0.1", user="root",
            is_host_loopback=True, shutdown_command="shutdown -h now",
        )
        remote_monitor.config.ups_groups[0].remote_servers = [loopback]

        calls, run_patch = self._record_calls(remote_monitor)
        with run_patch:
            results = remote_monitor._shutdown_remote_servers()

        assert [host for host, _ in calls] == ["127.0.0.1"]
        assert results[0].shutdown_sent is True
        log_text = "\n".join(
            str(c) for c in remote_monitor.logger.log.call_args_list
        )
        # Single phase → use the "1 remote server" header, not the
        # "(loopback poweroff)" Phase header.
        assert "Shutting down 1 remote server" in log_text
        assert "(loopback poweroff)" not in log_text

    @pytest.mark.unit
    def test_no_enabled_servers_returns_empty_list_without_logging(
        self, remote_monitor
    ):
        """Empty remote_servers list short-circuits with `return []`."""
        remote_monitor.config.ups_groups[0].remote_servers = []
        results = remote_monitor._shutdown_remote_servers()
        assert results == []
        # No header log either — the function returned before any log.
        for call_obj in remote_monitor.logger.log.call_args_list:
            assert "Shutting down" not in str(call_obj)

    @pytest.mark.unit
    def test_multiple_loopbacks_skip_partial_pre_loop(self, remote_monitor):
        """Two loopbacks where only one has pre_shutdown_commands: the
        Phase A loop must `continue` past the one without pre and only
        execute the one that has them, then run Phase C for both."""
        lb_with_pre = RemoteServerConfig(
            name="lb-with-pre", enabled=True, host="127.0.0.1", user="root",
            is_host_loopback=True, shutdown_command="shutdown -h now",
            pre_shutdown_commands=[RemoteCommandConfig(command="echo drain")],
        )
        lb_without_pre = RemoteServerConfig(
            name="lb-no-pre", enabled=True, host="127.0.0.2", user="root",
            is_host_loopback=True, shutdown_command="shutdown -h now",
        )
        remote_monitor.config.ups_groups[0].remote_servers = [
            lb_with_pre, lb_without_pre,
        ]

        calls, run_patch = self._record_calls(remote_monitor)
        with run_patch:
            results = remote_monitor._shutdown_remote_servers()

        # Phase A: only lb-with-pre executes; lb-no-pre is `continue`-d.
        pre_phase_calls = [host for host, cmd in calls if cmd == "echo drain"]
        assert pre_phase_calls == ["127.0.0.1"]
        # Phase C: both poweroff.
        shutdown_calls = [host for host, cmd in calls if cmd == "shutdown -h now"]
        assert shutdown_calls == ["127.0.0.1", "127.0.0.2"]
        assert len(results) == 2

    @pytest.mark.unit
    def test_header_grammar_singular_vs_plural(self, remote_monitor):
        """Header log uses 'server' singular for one remote and 'servers'
        plural for multiple — no more '1 remote server(s)' parenthesis-S."""
        loopback = RemoteServerConfig(
            name="host-loopback", enabled=True, host="127.0.0.1", user="root",
            is_host_loopback=True, shutdown_command="shutdown -h now",
            pre_shutdown_commands=[RemoteCommandConfig(command="echo drain")],
        )
        nas = RemoteServerConfig(
            name="NAS", enabled=True, host="10.0.0.10", user="root",
            shutdown_command="poweroff",
        )
        remote_monitor.config.ups_groups[0].remote_servers = [loopback, nas]

        _calls, run_patch = self._record_calls(remote_monitor)
        with run_patch:
            remote_monitor._shutdown_remote_servers()

        log_text = "\n".join(
            str(c) for c in remote_monitor.logger.log.call_args_list
        )
        # 2 servers → "Shutting down 2 remote servers in N phases..."
        assert "2 remote servers in" in log_text
        assert "remote server(s)" not in log_text


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
            # The remote command is the final single argv element, now carrying
            # the PATH augmentation prefix (see REMOTE_PATH_PREFIX). The
            # original command must still be present intact.
            assert call_args[-1] == REMOTE_PATH_PREFIX + "echo test"
            assert call_args[-1].endswith("echo test")
            assert "/usr/syno/sbin" in call_args[-1]

    @pytest.mark.unit
    def test_synology_bare_command_gets_path_augmentation(self, ssh_monitor):
        """Regression (DS1821 outage, Eneru 6.1.6): a bare-name shutdown
        command like ``synoshutdown -s`` over SSH previously failed with
        ``sudo: synoshutdown: command not found`` because the non-interactive
        SSH shell has a minimal PATH lacking ``/usr/syno/sbin``. The command
        string sent over ssh must now carry the PATH augmentation so bare
        names resolve — through sudo — exactly like an interactive login."""
        ssh_monitor._notification_worker = MagicMock()
        ssh_monitor.config.behavior.dry_run = False
        server = RemoteServerConfig(
            name="Synology",
            host="192.168.1.60",
            user="admin",
            use_sudo=True,
            shutdown_command="synoshutdown -s",
        )

        with patch("eneru.shutdown.remote.run_command") as mock_run:
            mock_run.return_value = (0, "", "")

            result = ssh_monitor._shutdown_remote_server(server)

            assert result.shutdown_sent is True
            sent_command = mock_run.call_args[0][0][-1]
            # PATH prefix present, /usr/syno/sbin on PATH, sudo -n applied,
            # and the original bare command preserved at the tail.
            assert sent_command == (
                REMOTE_PATH_PREFIX + "sudo -n synoshutdown -s"
            )
            assert "/usr/syno/sbin" in sent_command
            assert sent_command.endswith("sudo -n synoshutdown -s")

    @pytest.mark.unit
    def test_remote_path_is_always_augmented(self, ssh_monitor):
        """Privileged bare commands resolve without a per-server switch."""
        server = RemoteServerConfig(host="192.168.1.50", user="root")
        assert not hasattr(server, "augment_remote_path")
        with patch("eneru.shutdown.remote.run_command") as mock_run:
            mock_run.return_value = (0, "", "")
            ssh_monitor._run_remote_command(server, "echo hi", 30, "test")
            assert mock_run.call_args[0][0][-1] == REMOTE_PATH_PREFIX + "echo hi"

    @pytest.mark.unit
    def test_final_shutdown_ssh_teardown_255_is_sent_unconfirmed(self, ssh_monitor):
        """F-077: the remote accepted poweroff and sshd died before returning
        status → ssh exits 255 with a transport-teardown stderr. For the FINAL
        shutdown command this is "sent (unconfirmed)", not a failure."""
        server = RemoteServerConfig(host="192.168.1.50", user="root")
        with patch("eneru.shutdown.remote.run_command") as mock_run:
            mock_run.return_value = (
                255, "", "Connection to 192.168.1.50 closed by remote host.")
            success, note = ssh_monitor._run_remote_command(
                server, "poweroff", 30, "shutdown", is_final_shutdown=True)
            assert success is True
            assert note == "SSH transport ended (result unknown)"

    @pytest.mark.unit
    def test_final_shutdown_255_permission_denied_stays_failed(self, ssh_monitor):
        """F-077: a 255 that is NOT a transport teardown (e.g. auth failure)
        stays a failure even on the final shutdown command."""
        server = RemoteServerConfig(host="192.168.1.50", user="root")
        with patch("eneru.shutdown.remote.run_command") as mock_run:
            mock_run.return_value = (255, "", "Permission denied (publickey).")
            success, err = ssh_monitor._run_remote_command(
                server, "poweroff", 30, "shutdown", is_final_shutdown=True)
            assert success is False
            assert "Permission denied" in err

    @pytest.mark.unit
    def test_non_final_255_teardown_stays_failed(self, ssh_monitor):
        """F-077: a mid-sequence (pre_shutdown) command that drops the SSH
        transport really did fail — the leniency is scoped to the final
        poweroff only (is_final_shutdown defaults False)."""
        server = RemoteServerConfig(host="192.168.1.50", user="root")
        with patch("eneru.shutdown.remote.run_command") as mock_run:
            mock_run.return_value = (255, "", "Broken pipe")
            success, err = ssh_monitor._run_remote_command(
                server, "stop_vms", 30, "pre")
            assert success is False

    @pytest.mark.unit
    def test_final_shutdown_teardown_sets_shutdown_sent(self, ssh_monitor):
        """F-077 end-to-end: via _shutdown_remote_server, a 255-teardown on the
        poweroff marks shutdown_sent=True (so the loopback-delegate path writes
        the completion marker) without firing a false failure."""
        ssh_monitor._notification_worker = MagicMock()
        ssh_monitor.config.behavior.dry_run = False
        server = RemoteServerConfig(host="192.168.1.50", user="root",
                                    shutdown_command="poweroff")
        with patch("eneru.shutdown.remote.run_command") as mock_run:
            mock_run.return_value = (255, "", "Connection reset by peer")
            result = ssh_monitor._shutdown_remote_server(server)
            assert result.shutdown_sent is True
            assert not result.error

    @pytest.mark.unit
    def test_is_ssh_transport_teardown_signatures(self):
        """F-077: the teardown detector matches the known transport-drop
        phrasings (fixed + OpenSSH's variable-host form) and nothing else."""
        from eneru.shutdown.remote import _is_ssh_transport_teardown
        assert _is_ssh_transport_teardown("Connection to h closed by remote host")
        assert _is_ssh_transport_teardown("client_loop: send disconnect: Broken pipe")
        assert _is_ssh_transport_teardown("Connection reset by peer")
        assert _is_ssh_transport_teardown("Connection to 10.0.0.1 closed.")
        assert not _is_ssh_transport_teardown("Permission denied (publickey).")
        assert not _is_ssh_transport_teardown("")

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
    def test_run_remote_command_container_uses_ssh_mount_known_hosts(
        self, ssh_monitor, monkeypatch
    ):
        """Shutdown SSH commands use the same container known_hosts default."""
        monkeypatch.delenv(eneru_utils.KNOWN_HOSTS_ENV, raising=False)
        monkeypatch.setattr(eneru_utils, "running_in_container", lambda: True)
        server = RemoteServerConfig(
            name="Test",
            host="192.168.1.50",
            user="admin",
        )

        with patch("eneru.shutdown.remote.run_command") as mock_run:
            mock_run.return_value = (0, "", "")

            ssh_monitor._run_remote_command(server, "echo test", 30, "test")

            call_args = mock_run.call_args[0][0]
            assert "UserKnownHostsFile=/var/lib/eneru/ssh/known_hosts" in call_args

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


class TestParallelShutdownResilience:
    """Deadline-based parallel join + worker-crash accounting."""

    @pytest.fixture
    def remote_monitor(self, minimal_config, tmp_path):
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
    def test_parallel_join_does_not_hang_on_stuck_worker(self, remote_monitor):
        """Behavioural-gap 1: a worker that blocks past the phase deadline is
        NOT waited on forever -- the deadline-based join caps the total wait,
        synthesises a timed-out result for the missing thread, and logs the
        'still in progress' warning. Zeroing the SSH overhead buffer + the
        per-server timeouts makes ``max_timeout`` (hence the join deadline) ~0,
        so the stuck worker is observed still-alive immediately."""
        release = threading.Event()

        server = RemoteServerConfig(
            name="hang", enabled=True, host="10.0.0.9", user="root",
            command_timeout=0, connect_timeout=0, shutdown_safety_margin=0,
        )

        def blocking_worker(server, *, deadline=None):
            # Block until the test releases us (5s safety net so a stray
            # daemon thread can't wedge the run if something regresses).
            release.wait(timeout=5)
            return RemoteShutdownResult(
                server=server.name, host=server.host, shutdown_sent=True)

        with patch("eneru.shutdown.remote._SSH_OVERHEAD_BUFFER", 0), \
             patch.object(remote_monitor, "_shutdown_remote_server",
                          side_effect=blocking_worker):
            try:
                results = remote_monitor._shutdown_servers_parallel([server])
            finally:
                release.set()  # let the daemon worker exit cleanly

        assert len(results) == 1
        stuck = results[0]
        assert stuck.timed_out is True
        assert stuck.completed is False
        assert "timed out" in stuck.error

        log_text = "\n".join(
            str(c) for c in remote_monitor.logger.log.call_args_list)
        assert "still in progress" in log_text

    @pytest.mark.unit
    def test_worker_crash_flags_result_and_counts_in_summary(self, remote_monitor):
        """Behavioural-gap 4: a worker that RAISES (not an SSH failure the callee
        catches, but a bubbling exception) is caught in the thread wrapper, its
        result is flagged ``crashed=True``, and the phase summary counts it."""
        server = RemoteServerConfig(
            name="boom", enabled=True, host="10.0.0.4", user="root")
        remote_monitor.config.ups_groups[0].remote_servers = [server]

        def crashing_worker(server, *, deadline=None):
            raise RuntimeError("kaboom")

        with patch.object(remote_monitor, "_shutdown_remote_server",
                          side_effect=crashing_worker):
            results = remote_monitor._shutdown_remote_servers()

        assert len(results) == 1
        assert results[0].crashed is True

        log_text = "\n".join(
            str(c) for c in remote_monitor.logger.log.call_args_list)
        assert "1 crashed" in log_text          # phase summary line
        assert "kaboom" in log_text             # the thread-crash trace line


class TestRemoteProgressTracking:
    """Progress tracking is optional: executors without a tracker no-op."""

    @pytest.mark.unit
    def test_tracking_without_tracker_is_noop(self):
        from eneru.config import RemoteServerConfig
        from eneru.shutdown.remote import RemoteShutdownMixin, RemoteShutdownResult

        mixin = RemoteShutdownMixin()
        server = RemoteServerConfig(name="nas", host="10.0.0.2", user="root")

        assert mixin._track_remote_start(server) is None
        mixin._track_remote_finish(
            RemoteShutdownResult(server="nas", host="10.0.0.2"), None)


class TestRemoteCommandOutputCapture:
    """The final shutdown command's exit code and output reach the result
    so authenticated dashboard readers can inspect the server's response."""

    @pytest.fixture
    def live_monitor(self, minimal_config, tmp_path):
        minimal_config.logging.state_file = str(tmp_path / "state")
        minimal_config.logging.battery_history_file = str(tmp_path / "history")
        minimal_config.logging.shutdown_flag_file = str(tmp_path / "flag")
        minimal_config.logging.file = None
        minimal_config.behavior.dry_run = False
        monitor = UPSGroupMonitor(minimal_config)
        monitor.state = MonitorState()
        monitor.logger = MagicMock()
        monitor._notification_worker = MagicMock()
        monitor._send_notification = MagicMock()
        return monitor

    @staticmethod
    def _server(**kwargs):
        from eneru.config import RemoteServerConfig

        defaults = dict(name="nas", host="10.0.0.2", user="root",
                        enabled=True, shutdown_command="poweroff")
        defaults.update(kwargs)
        return RemoteServerConfig(**defaults)

    @pytest.mark.unit
    def test_run_remote_command_fills_capture(self, live_monitor):
        capture = {}
        with patch("eneru.shutdown.remote.run_command",
                   return_value=(3, "partial\n", "boom\n")):
            ok, msg = live_monitor._run_remote_command(
                self._server(), "poweroff", 10, "shutdown", capture=capture)
        assert (ok, msg) == (False, "boom")
        assert capture == {"exit_code": 3, "stdout": "partial\n",
                           "stderr": "boom\n"}

    @pytest.mark.unit
    def test_capture_untouched_when_command_never_runs(self, live_monitor):
        capture = {}
        with patch("eneru.shutdown.remote.run_command") as runner:
            ok, _ = live_monitor._run_remote_command(
                self._server(), "poweroff", 10, "shutdown",
                deadline=time.monotonic() - 1, capture=capture)
        runner.assert_not_called()
        assert ok is False and capture == {}

    @pytest.mark.unit
    @pytest.mark.parametrize(("rc", "stdout", "stderr", "sent", "response"), [
        (0, "Powering off\n", "", True, "Powering off"),
        (1, "", "Permission denied\n", False, "Permission denied"),
        (2, "out\n", "err\n", False, "out\nerr"),
    ])
    def test_regular_remote_records_exit_code_and_response(
            self, live_monitor, rc, stdout, stderr, sent, response):
        with patch("eneru.shutdown.remote.run_command",
                   return_value=(rc, stdout, stderr)):
            result = live_monitor._shutdown_remote_server(self._server())
        assert result.shutdown_sent is sent
        assert result.exit_code == rc
        assert result.response == response

    @pytest.mark.unit
    def test_loopback_poweroff_records_exit_code_and_response(
            self, live_monitor):
        from eneru.shutdown.remote import RemoteShutdownResult

        server = self._server(name="host", host="127.0.0.1",
                              is_host_loopback=True)
        result = RemoteShutdownResult(server="host", host="127.0.0.1")
        with patch("eneru.shutdown.remote.run_command",
                   return_value=(1, "", "sudo: a password is required\n")):
            live_monitor._shutdown_loopback_command(server, result)
        assert result.exit_code == 1
        assert result.response == "sudo: a password is required"

    @pytest.mark.unit
    def test_dry_run_leaves_exit_code_unset(self, live_monitor):
        live_monitor.config.behavior.dry_run = True
        with patch("eneru.shutdown.remote.run_command") as runner:
            result = live_monitor._shutdown_remote_server(self._server())
        runner.assert_not_called()
        assert result.exit_code is None and result.response == ""


class TestRemoteWorkerStartFailure:
    """A worker thread that can't start (resource exhaustion) is recorded as
    crashed; other workers still run and the loopback poweroff still fires."""

    @pytest.fixture
    def live_monitor(self, minimal_config, tmp_path):
        minimal_config.logging.state_file = str(tmp_path / "state")
        minimal_config.logging.battery_history_file = str(tmp_path / "history")
        minimal_config.logging.shutdown_flag_file = str(tmp_path / "flag")
        minimal_config.logging.file = None
        minimal_config.behavior.dry_run = False
        monitor = UPSGroupMonitor(minimal_config)
        monitor.state = MonitorState()
        monitor.logger = MagicMock()
        monitor._notification_worker = MagicMock()
        monitor._send_notification = MagicMock()
        return monitor

    @pytest.mark.unit
    def test_start_failure_is_recorded_and_phase_c_still_runs(self, live_monitor):
        from eneru.config import RemoteServerConfig
        import threading as real_threading

        nas = RemoteServerConfig(name="nas", host="10.0.0.2", user="root",
                                 enabled=True, shutdown_command="poweroff")
        web = RemoteServerConfig(name="web", host="10.0.0.3", user="root",
                                 enabled=True, shutdown_command="poweroff")
        host = RemoteServerConfig(name="host", host="127.0.0.1", user="root",
                                  enabled=True, is_host_loopback=True,
                                  shutdown_command="poweroff")
        live_monitor.config.ups_groups[0].remote_servers = [nas, web, host]
        sent = []

        class FlakyThread(real_threading.Thread):
            def start(self):
                if self.name.endswith("nas"):
                    raise RuntimeError("can't start new thread")
                super().start()

        def fake_run(server, command, timeout, desc, **kwargs):
            sent.append(server.host)
            return True, ""

        with patch("eneru.shutdown.remote.threading.Thread", FlakyThread), \
             patch.object(live_monitor, "_run_remote_command", side_effect=fake_run):
            results = live_monitor._shutdown_remote_servers()

        by_host = {r.host: r for r in results}
        assert by_host["10.0.0.2"].crashed is True
        assert "can't start new thread" in by_host["10.0.0.2"].error
        assert by_host["10.0.0.3"].shutdown_sent is True
        # The host poweroff (Phase C) still ran, after the peer that started.
        assert by_host["127.0.0.1"].shutdown_sent is True
        assert sent.index("10.0.0.3") < sent.index("127.0.0.1")


class TestGroupAReleaseReview:
    """6.2.0 release review, fix group A (F-095, F-098, F-104, F-118)."""

    @pytest.fixture
    def remote_monitor(self, minimal_config, tmp_path):
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

    @staticmethod
    def _shutdown_argv(monitor, server, command="poweroff"):
        with patch("eneru.shutdown.remote.run_command",
                   return_value=(0, "", "")) as run:
            ok, _ = monitor._run_remote_command(server, command, 30, "t")
        assert ok
        return run.call_args[0][0]

    @pytest.mark.unit
    @pytest.mark.parametrize("opts", [
        ["-i", "/k"],
        ["-p", "2222"],
        ["-o Port=2222"],
        ["StrictHostKeyChecking=no"],
        ["-J", "jump"],
        ["-i", "/k", "-p", "2222", "-o StrictHostKeyChecking=no",
         "-o UserKnownHostsFile=/dev/null"],
    ])
    def test_shutdown_and_probe_build_the_same_ssh_argv(self, remote_monitor, opts):
        """F-095: `["-i", "/k"]` used to become `-i -o /k …` on the shutdown
        path (the key path parsed as the host) while the probe was right."""
        from eneru.remote_health import build_ssh_probe_command
        server = RemoteServerConfig(name="s", enabled=True, host="h",
                                    user="u", ssh_options=list(opts))
        shutdown = self._shutdown_argv(remote_monitor, server, "poweroff")
        probe = build_ssh_probe_command(server, "PROBE")
        assert shutdown[:-1] == probe[:-1]
        assert shutdown[-1] == REMOTE_PATH_PREFIX + "poweroff"
        # The option's value stays glued to its flag.
        for flag in ("-i", "-p", "-J"):
            if flag in opts:
                assert shutdown[shutdown.index(flag) + 1] == \
                    opts[opts.index(flag) + 1]

    @pytest.mark.unit
    @pytest.mark.parametrize("item,flag,value", [
        ("-i /root/.ssh/nas_key", "-i", "/root/.ssh/nas_key"),
        ("-l admin", "-l", "admin"),
        ("-J jump.example", "-J", "jump.example"),
        ("-F /etc/eneru/ssh_config", "-F", "/etc/eneru/ssh_config"),
        ("-p 2222", "-p", "2222"),
    ])
    def test_flag_and_value_in_one_item_split_the_same_everywhere(
            self, remote_monitor, item, flag, value):
        """R2-02: `"-i /root/.ssh/key"` as ONE list item must reach ssh as
        two argv elements on the shutdown path, the remote-health probe and
        `config check` alike (ssh would otherwise read " /root/..." as the
        key path and fail BatchMode auth)."""
        from eneru import config_check as cc
        from eneru.config import Config
        from eneru.remote_health import run_remote_probe
        server = RemoteServerConfig(name="s", enabled=True, host="h",
                                    user="u", ssh_options=[item],
                                    shutdown_command="sudo shutdown -h now")
        shutdown = self._shutdown_argv(remote_monitor, server, "poweroff")
        with patch("eneru.remote_health.run_command",
                   return_value=(0, "", "")) as run:
            assert run_remote_probe(server, "PROBE")[0]
        health = run.call_args[0][0]
        with patch("eneru.remote_health.run_command",
                   return_value=(0, "", "")), \
                patch.object(cc, "command_exists", return_value=True), \
                patch.object(cc, "_run", return_value=(0, "", "")) as cc_run:
            cc.probe_remote(Config(), server)
        check = cc_run.call_args[0][0]
        for argv in (shutdown, health, check):
            assert item not in argv
            assert argv[argv.index(flag) + 1] == value
        assert shutdown[:-1] == health[:-1] == check[:-1]

    @pytest.mark.unit
    def test_destination_follows_double_dash(self, remote_monitor):
        """F-104: `--` ends ssh option parsing before user@host."""
        server = RemoteServerConfig(name="s", enabled=True, host="h", user="u")
        argv = self._shutdown_argv(remote_monitor, server)
        assert argv[argv.index("--") + 1] == "u@h"

    @pytest.mark.unit
    def test_dangling_ssh_option_fails_the_step_not_the_sequence(self, remote_monitor):
        server = RemoteServerConfig(name="s", enabled=True, host="h", user="u",
                                    ssh_options=["-i"])
        capture = {}
        with patch("eneru.shutdown.remote.run_command") as run:
            ok, err = remote_monitor._run_remote_command(
                server, "poweroff", 30, "t", capture=capture)
        assert not ok and "dangling" in err
        run.assert_not_called()
        assert capture == {}  # ssh never ran

    @pytest.mark.unit
    def test_step_use_sudo_overrides_the_server(self, remote_monitor):
        """F-098: a step's own use_sudo wins; unset inherits the server."""
        server = RemoteServerConfig(
            name="s", enabled=True, host="h", user="deploy", use_sudo=True,
            command_timeout=30,
            pre_shutdown_commands=[
                RemoteCommandConfig(command="systemctl --user stop app",
                                    use_sudo=False),
                RemoteCommandConfig(command="systemctl stop db"),
                RemoteCommandConfig(action="stop_vms", use_sudo=False),
            ],
        )
        with patch.object(remote_monitor, "_run_remote_command",
                          return_value=(True, "")) as run:
            remote_monitor._execute_remote_pre_shutdown(server)
        sent = [c.args[1] for c in run.call_args_list]
        assert sent[0] == "systemctl --user stop app"
        assert sent[1] == "sudo -n systemctl stop db"
        assert "sudo -n virsh" not in sent[2] and "virsh list" in sent[2]

        server.use_sudo = False
        server.pre_shutdown_commands = [
            RemoteCommandConfig(command="systemctl stop db", use_sudo=True)]
        with patch.object(remote_monitor, "_run_remote_command",
                          return_value=(True, "")) as run:
            remote_monitor._execute_remote_pre_shutdown(server)
        assert run.call_args.args[1] == "sudo -n systemctl stop db"


class TestWithSudoHelper:
    """F-124: one sudo-prefix rule shared by the runtime and config check."""

    @pytest.mark.unit
    @pytest.mark.parametrize("command,expected", [
        ("shutdown -h now", "sudo -n shutdown -h now"),
        ("  shutdown -h now", "sudo -n   shutdown -h now"),
        ("sudo shutdown -h now", "sudo shutdown -h now"),
        ("  sudo -n poweroff", "  sudo -n poweroff"),
        ("sudo\tshutdown -h now", "sudo\tshutdown -h now"),
        ("/usr/bin/sudo shutdown -h now", "/usr/bin/sudo shutdown -h now"),
        ("sudo", "sudo"),
        ("sudoedit /etc/x", "sudo -n sudoedit /etc/x"),
        ("", "sudo -n "),
    ])
    def test_prefix_rule(self, command, expected):
        assert with_sudo(command, True) == expected
        assert with_sudo(command, False) == command
        # The mixin's staticmethod is a thin alias of the shared helper.
        assert RemoteShutdownMixin._with_sudo(command, True) == expected
