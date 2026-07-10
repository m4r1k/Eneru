"""v5.5: shutdown sequence in container loopback delegation mode.

When Eneru runs inside a container with a configured is_host_loopback
delegate, the in-process local phases (VMs, containers, sync, unmount,
host poweroff) are skipped and the equivalent work is sent over SSH to
the host as part of the loopback's pre_shutdown_commands + shutdown_command
(handled by the existing _shutdown_remote_servers path).

These tests pin the rewired sequence: what's skipped, what still runs,
what events fire.
"""

from unittest.mock import MagicMock, patch

import pytest

from eneru import (
    Config, UPSConfig, UPSGroupConfig, TriggersConfig, DepletionConfig,
    ExtendedTimeConfig, BehaviorConfig, LoggingConfig, NotificationsConfig,
    VMConfig, ContainersConfig, FilesystemsConfig, UnmountConfig,
    RemoteServerConfig, LocalShutdownConfig, MonitorState,
)
from eneru.monitor import UPSGroupMonitor
from eneru.shutdown.remote import RemotePreShutdownResult, RemoteShutdownResult


def _make_delegated_monitor(
    tmp_path, *,
    loopback: bool = True,
    runtime: str = "container (Docker)",
    is_local: bool = True,
    local_shutdown_enabled: bool = True,
    wall: bool = False,
    dry_run: bool = True,
):
    """Build a monitor representing the container+loopback scenario.

    Defaults to dry_run=True so the shutdown sequence is safe to invoke
    end-to-end without actually doing anything irreversible.
    """
    remote_servers = []
    if loopback:
        remote_servers.append(RemoteServerConfig(
            name="host-loopback",
            enabled=True,
            host="127.0.0.1",
            user="root",
            shutdown_command="shutdown -h now",
            is_host_loopback=True,
        ))

    config = Config(
        ups_groups=[UPSGroupConfig(
            ups=UPSConfig(name="TestUPS@localhost"),
            triggers=TriggersConfig(low_battery_threshold=20),
            virtual_machines=VMConfig(enabled=True),
            containers=ContainersConfig(enabled=True),
            filesystems=FilesystemsConfig(
                sync_enabled=True,
                unmount=UnmountConfig(enabled=True),
            ),
            remote_servers=remote_servers,
            is_local=is_local,
        )],
        behavior=BehaviorConfig(dry_run=dry_run),
        logging=LoggingConfig(
            shutdown_flag_file=str(tmp_path / "shutdown-flag"),
            state_file=str(tmp_path / "state"),
            battery_history_file=str(tmp_path / "history"),
        ),
        local_shutdown=LocalShutdownConfig(
            enabled=local_shutdown_enabled,
            wall=wall,
        ),
    )
    monitor = UPSGroupMonitor(config)
    monitor.state = MonitorState()
    monitor.logger = MagicMock()
    monitor._notification_worker = MagicMock()
    # Patch runtime detection at the import site used by the property.
    monitor._test_runtime = runtime
    return monitor


def _patch_runtime(runtime: str):
    """Patch _detect_runtime_context everywhere monitor.py imports it from."""
    return patch("eneru.runtime._detect_runtime_context", return_value=runtime)


class TestUsesLoopbackDelegate:
    """The property that drives every other decision in the sequence."""

    @pytest.mark.unit
    def test_true_for_container_plus_loopback_plus_local(self, tmp_path):
        monitor = _make_delegated_monitor(tmp_path)
        with _patch_runtime("container (Docker)"):
            assert monitor._uses_loopback_delegate is True

    @pytest.mark.unit
    def test_false_when_no_loopback(self, tmp_path):
        monitor = _make_delegated_monitor(tmp_path, loopback=False)
        with _patch_runtime("container (Docker)"):
            assert monitor._uses_loopback_delegate is False

    @pytest.mark.unit
    def test_false_when_loopback_disabled(self, tmp_path):
        monitor = _make_delegated_monitor(tmp_path)
        monitor.config.remote_servers[0].enabled = False
        with _patch_runtime("container (Docker)"):
            assert monitor._uses_loopback_delegate is False

    @pytest.mark.unit
    def test_false_on_bare_metal(self, tmp_path):
        """Bare-metal install with a loopback entry shouldn't delegate —
        config validation should have prevented that case anyway."""
        monitor = _make_delegated_monitor(tmp_path)
        with _patch_runtime("systemd service"):
            assert monitor._uses_loopback_delegate is False

    @pytest.mark.unit
    def test_false_when_not_local(self, tmp_path):
        monitor = _make_delegated_monitor(tmp_path, is_local=False)
        with _patch_runtime("container (Docker)"):
            assert monitor._uses_loopback_delegate is False

    @pytest.mark.unit
    def test_true_for_kubernetes_with_explicit_loopback(self, tmp_path):
        """K8s discourages local-host ownership, but if a user explicitly
        opts in with a loopback, delegation kicks in normally."""
        monitor = _make_delegated_monitor(tmp_path)
        with _patch_runtime("container (Kubernetes)"):
            assert monitor._uses_loopback_delegate is True


class TestDelegatedShutdownSequence:
    """End-to-end shutdown in delegated mode."""

    def _spy_phases(self, monitor):
        """Replace in-process shutdown phases with call-tracking spies."""
        calls = []
        monitor._shutdown_vms = lambda: calls.append("vms")
        monitor._shutdown_containers = lambda: calls.append("containers")
        monitor._sync_filesystems = lambda: calls.append("sync")
        monitor._unmount_filesystems = lambda: calls.append("unmount")
        def remote_spy():
            calls.append("remote")
            return [
                RemoteShutdownResult(
                    server="host-loopback",
                    host="127.0.0.1",
                    shutdown_sent=True,
                    dry_run=monitor.config.behavior.dry_run,
                )
            ]
        monitor._shutdown_remote_servers = remote_spy
        return calls

    @pytest.mark.unit
    def test_skips_in_process_local_phases(self, tmp_path):
        """Delegated mode skips VMs/containers/sync/unmount inside the container."""
        monitor = _make_delegated_monitor(tmp_path)
        calls = self._spy_phases(monitor)
        with _patch_runtime("container (Docker)"):
            monitor._execute_shutdown_sequence()
        assert "vms" not in calls
        assert "containers" not in calls
        assert "sync" not in calls
        assert "unmount" not in calls

    @pytest.mark.unit
    def test_pre_command_failure_surfaces_partial_drain_warning(self, tmp_path):
        """cubic P2: an ordinary pre-shutdown command that exits non-zero
        increments pre_commands.failed WITHOUT setting crashed/error, so
        RemoteShutdownResult.success stays True. The delegated path must still
        warn about the partial drain (and still complete, since the poweroff
        was delivered) rather than silently logging SEQUENCE COMPLETE."""
        monitor = _make_delegated_monitor(tmp_path)
        logs = []
        monitor._log_message = lambda m: logs.append(m)
        monitor._shutdown_vms = lambda: None
        monitor._shutdown_containers = lambda: None
        monitor._sync_filesystems = lambda: None
        monitor._unmount_filesystems = lambda: None

        def remote_spy():
            r = RemoteShutdownResult(
                server="host-loopback", host="127.0.0.1",
                shutdown_sent=True, dry_run=monitor.config.behavior.dry_run,
                pre_commands=RemotePreShutdownResult(failed=1),
            )
            assert r.success is True  # the exact case cubic flagged
            return [r]
        monitor._shutdown_remote_servers = remote_spy

        with _patch_runtime("container (Docker)"):
            monitor._execute_shutdown_sequence()

        assert any(
            "partially failed" in m and "pre-shutdown command" in m
            for m in logs
        ), logs

    @pytest.mark.unit
    def test_still_runs_remote_servers_phase(self, tmp_path):
        """Remote shutdown phase ALWAYS runs — that's where the loopback
        delegate executes the host-side actions."""
        monitor = _make_delegated_monitor(tmp_path)
        calls = self._spy_phases(monitor)
        with _patch_runtime("container (Docker)"):
            monitor._execute_shutdown_sequence()
        assert "remote" in calls

    @pytest.mark.unit
    def test_skips_final_inline_os_sync(self, tmp_path):
        """The final inline os.sync() is skipped — only host-side sync (via
        the loopback's sync action) is meaningful for the host's filesystems."""
        monitor = _make_delegated_monitor(tmp_path)
        self._spy_phases(monitor)
        with _patch_runtime("container (Docker)"), \
             patch.object(monitor, "_bounded_sync") as sync_mock:
            monitor._execute_shutdown_sequence()
            sync_mock.assert_not_called()

    @pytest.mark.unit
    def test_skips_final_inline_local_shutdown_command(self, tmp_path):
        """The inline `shutdown -h now` is skipped — the loopback's
        shutdown_command (already sent via SSH) is what actually powers off."""
        monitor = _make_delegated_monitor(tmp_path, dry_run=False)
        self._spy_phases(monitor)
        with _patch_runtime("container (Docker)"), \
             patch("eneru.monitor.run_command") as run_cmd, \
             patch("eneru.monitor.write_shutdown_marker"):
            monitor._execute_shutdown_sequence()
            # No call to `shutdown -h now` — that argv would contain "shutdown"
            for call in run_cmd.call_args_list:
                argv = call.args[0]
                assert "shutdown" not in argv[0], (
                    f"unexpected inline poweroff call: {argv}"
                )

    @pytest.mark.unit
    def test_suppresses_wall_in_delegated_mode(self, tmp_path):
        """wall() reaches nobody from inside a container; suppress it."""
        monitor = _make_delegated_monitor(tmp_path, wall=True, dry_run=False)
        self._spy_phases(monitor)
        with _patch_runtime("container (Docker)"), \
             patch("eneru.monitor.run_command") as run_cmd, \
             patch("eneru.monitor.write_shutdown_marker"):
            monitor._execute_shutdown_sequence()
            for call in run_cmd.call_args_list:
                argv = call.args[0]
                assert argv[0] != "wall", "wall must not fire in delegated mode"

    @pytest.mark.unit
    def test_native_mode_still_runs_wall_when_configured(self, tmp_path):
        """Regression: bare-metal with wall=true still calls wall."""
        monitor = _make_delegated_monitor(
            tmp_path, loopback=False, wall=True, dry_run=False,
        )
        self._spy_phases(monitor)
        with _patch_runtime("systemd service"), \
             patch("eneru.monitor.run_command",
                   return_value=(0, "", "")) as run_cmd, \
             patch("eneru.monitor.write_shutdown_marker"):
            monitor._execute_shutdown_sequence()
            wall_calls = [
                c for c in run_cmd.call_args_list if c.args[0][0] == "wall"
            ]
            assert len(wall_calls) == 1

    @pytest.mark.unit
    def test_emits_delegated_shutdown_initiated_event(self, tmp_path):
        """DELEGATED_SHUTDOWN_INITIATED fires so dashboards/SIEM can
        distinguish container-mediated from native shutdowns."""
        monitor = _make_delegated_monitor(tmp_path)
        self._spy_phases(monitor)
        event_log = MagicMock()
        monitor._stats_store.log_event = event_log
        with _patch_runtime("container (Docker)"):
            monitor._execute_shutdown_sequence()
        events_seen = [c.args[0] for c in event_log.call_args_list]
        assert "DELEGATED_SHUTDOWN_INITIATED" in events_seen

    @pytest.mark.unit
    def test_native_mode_does_not_emit_delegated_event(self, tmp_path):
        """Regression: bare-metal shutdowns don't carry the delegated tag."""
        monitor = _make_delegated_monitor(tmp_path, loopback=False)
        self._spy_phases(monitor)
        event_log = MagicMock()
        monitor._stats_store.log_event = event_log
        with _patch_runtime("systemd service"):
            monitor._execute_shutdown_sequence()
        events_seen = [c.args[0] for c in event_log.call_args_list]
        assert "DELEGATED_SHUTDOWN_INITIATED" not in events_seen

    @pytest.mark.unit
    def test_native_mode_still_runs_in_process_phases(self, tmp_path):
        """Regression: bare-metal install does NOT skip the in-process phases."""
        monitor = _make_delegated_monitor(tmp_path, loopback=False)
        calls = self._spy_phases(monitor)
        with _patch_runtime("systemd service"):
            monitor._execute_shutdown_sequence()
        assert calls == ["vms", "containers", "sync", "unmount", "remote"]

    @pytest.mark.unit
    def test_check_dependencies_silent_about_host_binaries_when_delegating(
        self, tmp_path,
    ):
        """rc1 → rc2 fix: in delegated mode, _check_dependencies must not
        warn about missing host binaries (virsh, docker, podman) — they
        live on the HOST. The previous behavior emitted misleading
        WARNING lines AND flipped enabled-flags off, defeating the
        delegated path."""
        monitor = _make_delegated_monitor(tmp_path)

        def fake_exists(cmd):
            # upsc is always required (NUT polling — unrelated to delegation).
            # ssh is always required (we need to reach the loopback).
            # Every other binary (virsh, docker, podman, etc.) is "not found"
            # so we can prove the dep check stays silent about them in
            # delegated mode.
            return cmd in ("upsc", "ssh")

        with _patch_runtime("container (Docker)"), \
             patch("eneru.monitor.command_exists", side_effect=fake_exists):
            try:
                monitor._check_dependencies()
            except SystemExit:
                pytest.fail("Delegated mode must not exit on missing host binaries")

        # The enabled-flags must STAY enabled — the loopback path
        # delegates them; flipping them off would break the rendered
        # shutdown sequence.
        assert monitor.config.virtual_machines.enabled is True
        assert monitor.config.containers.enabled is True

        # And no per-binary warning should have been emitted.
        log_calls = [c.args[0] for c in monitor.logger.log.call_args_list] if hasattr(monitor.logger, 'log') else []
        # Use _log_message side effect — collected via the monitor's logger mock.
        # Most tests in this file accept either logger.log() or self._log_message
        # being a real method calling logger.info/.log; the asserts above on
        # enabled-flags are the load-bearing checks. Verify ad-hoc:
        joined = "\n".join(str(c) for c in log_calls)
        assert "virsh' not found" not in joined
        assert "No container runtime found" not in joined

    @pytest.mark.unit
    def test_check_dependencies_native_mode_still_warns(self, tmp_path):
        """Regression: bare-metal install MUST still warn + disable flags
        when host binaries are missing (the existing v5.4 behavior)."""
        monitor = _make_delegated_monitor(tmp_path, loopback=False)

        def fake_exists(cmd):
            # virsh missing, everything else present.
            return cmd != "virsh"

        with _patch_runtime("systemd service"), \
             patch("eneru.monitor.command_exists", side_effect=fake_exists):
            try:
                monitor._check_dependencies()
            except SystemExit:
                pass  # Don't care for this assertion — focus is on VMs flag.

        assert monitor.config.virtual_machines.enabled is False

    @pytest.mark.unit
    def test_shutdown_sequence_complete_event_still_fires_when_delegated(self, tmp_path):
        """SHUTDOWN_SEQUENCE_COMPLETE describes Eneru's own state; must still
        fire when delegating so the events table stays consistent."""
        monitor = _make_delegated_monitor(tmp_path)
        self._spy_phases(monitor)
        event_log = MagicMock()
        monitor._stats_store.log_event = event_log
        with _patch_runtime("container (Docker)"):
            monitor._execute_shutdown_sequence()
        events_seen = [c.args[0] for c in event_log.call_args_list]
        assert "SHUTDOWN_SEQUENCE_COMPLETE" in events_seen

    @pytest.mark.unit
    def test_exit_after_shutdown_exits_delegated_success(self, tmp_path):
        """A successful loopback poweroff honors one-shot daemon mode."""
        monitor = _make_delegated_monitor(tmp_path, dry_run=False)
        monitor._exit_after_shutdown = True
        self._spy_phases(monitor)
        monitor._cleanup_and_exit = MagicMock()

        with _patch_runtime("container (Docker)"), \
             patch("eneru.monitor.write_shutdown_marker"):
            monitor._execute_shutdown_sequence()

        monitor._cleanup_and_exit.assert_called_once_with(None, None)

    @pytest.mark.unit
    def test_missing_ssh_is_fatal_when_delegating(self, tmp_path):
        monitor = _make_delegated_monitor(tmp_path)

        def fake_exists(cmd):
            return cmd != "ssh"

        with _patch_runtime("container (Docker)"), \
             patch("eneru.monitor.command_exists", side_effect=fake_exists):
            # ISS-006: RuntimeError (not SystemExit) so coordinator mode surfaces it.
            with pytest.raises(RuntimeError) as exc_info:
                monitor._check_dependencies()

        assert "ssh" in str(exc_info.value)

    @pytest.mark.unit
    def test_delegated_shutdown_failure_does_not_mark_sequence_complete(self, tmp_path):
        monitor = _make_delegated_monitor(tmp_path, dry_run=False)
        calls = []
        monitor._shutdown_vms = lambda: calls.append("vms")
        monitor._shutdown_containers = lambda: calls.append("containers")
        monitor._sync_filesystems = lambda: calls.append("sync")
        monitor._unmount_filesystems = lambda: calls.append("unmount")
        monitor._shutdown_remote_servers = lambda: [
            RemoteShutdownResult(
                server="host-loopback",
                host="127.0.0.1",
                shutdown_sent=False,
                error="ssh failed",
            )
        ]
        event_log = MagicMock()
        monitor._stats_store.log_event = event_log

        with _patch_runtime("container (Docker)"), \
             patch("eneru.monitor.run_command") as run_cmd, \
             patch("eneru.monitor.write_shutdown_marker") as marker:
            monitor._execute_shutdown_sequence()

        run_cmd.assert_not_called()
        marker.assert_not_called()
        events_seen = [c.args[0] for c in event_log.call_args_list]
        assert "SHUTDOWN_SEQUENCE_COMPLETE" not in events_seen

    @pytest.mark.unit
    def test_delegated_poweroff_sent_with_drain_failure_marks_complete(
        self, tmp_path,
    ):
        """ISS-005: a Phase-A drain crash (success=False because crashed/error)
        while the Phase-C poweroff WAS delivered (shutdown_sent=True) must be
        treated as a COMPLETE sequence — write the marker, log
        SHUTDOWN_SEQUENCE_COMPLETE, warn about the partial drain — not reported
        as a failed poweroff. Before the fix, monitor used all(r.success) and
        misclassified this delivered poweroff as incomplete."""
        monitor = _make_delegated_monitor(tmp_path, dry_run=False)
        monitor._shutdown_remote_servers = lambda: [
            RemoteShutdownResult(
                server="host-loopback",
                host="127.0.0.1",
                shutdown_sent=True,   # Phase-C poweroff delivered
                crashed=True,         # but a Phase-A pre-action crashed
                error="pre-action drain crashed",
            )
        ]
        event_log = MagicMock()
        monitor._stats_store.log_event = event_log
        monitor._log_message = MagicMock()
        monitor._send_notification = MagicMock()

        with _patch_runtime("container (Docker)"), \
             patch("eneru.monitor.write_shutdown_marker") as marker:
            monitor._execute_shutdown_sequence()

        # Sequence recorded complete + recovery marker written.
        marker.assert_called_once()
        events_seen = [c.args[0] for c in event_log.call_args_list]
        assert "SHUTDOWN_SEQUENCE_COMPLETE" in events_seen
        # Partial drain surfaced as a warning, not a failure.
        assert any(
            "partially failed" in str(call.args[0]).lower()
            for call in monitor._log_message.call_args_list
        )
        bodies = [call.args[0] for call in monitor._send_notification.call_args_list]
        assert not any("Incomplete" in body for body in bodies)
        assert any("Complete" in body for body in bodies)

    @pytest.mark.unit
    def test_delegated_shutdown_failure_clears_shutdown_flag(self, tmp_path):
        """CodeRabbit #3: when the delegated host poweroff fails, the
        re-entry flag must be cleared so subsequent triggers aren't
        suppressed (the container stays up and may need to retry)."""
        monitor = _make_delegated_monitor(tmp_path, dry_run=False)
        monitor._shutdown_remote_servers = lambda: [
            RemoteShutdownResult(
                server="host-loopback",
                host="127.0.0.1",
                shutdown_sent=False,
                error="ssh failed",
            )
        ]
        # _trigger_immediate_shutdown touches the flag before calling
        # _execute_shutdown_sequence; simulate that pre-existing state.
        monitor._shutdown_flag_path.touch()
        assert monitor._shutdown_flag_path.exists()

        with _patch_runtime("container (Docker)"), \
             patch("eneru.monitor.run_command"), \
             patch("eneru.monitor.write_shutdown_marker"):
            monitor._execute_shutdown_sequence()

        # Flag must be gone so the next trigger can run.
        assert not monitor._shutdown_flag_path.exists()


class TestInjectDelegatedActions:
    """v5.5: _inject_delegated_actions translates local config into
    loopback pre_shutdown_commands at startup."""

    def _config_with_loopback_and_caps(self, tmp_path):
        from eneru import ConfigLoader
        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "ups:\n"
            "  name: 'TestUPS@localhost'\n"
            "local_shutdown:\n"
            "  enabled: true\n"
            "virtual_machines:\n"
            "  enabled: true\n"
            "containers:\n"
            "  enabled: true\n"
            "  compose_files:\n"
            "    - path: /etc/docker/compose/app.yml\n"
            "filesystems:\n"
            "  sync_enabled: true\n"
            "remote_servers:\n"
            "  - name: host-loopback\n"
            "    enabled: true\n"
            "    host: 127.0.0.1\n"
            "    user: root\n"
            "    is_host_loopback: true\n"
        )
        return ConfigLoader.load(str(config_file))

    @pytest.mark.unit
    def test_prepends_vms_compose_containers_sync(self, tmp_path):
        from eneru.cli import _inject_delegated_actions, _find_host_loopback

        config = self._config_with_loopback_and_caps(tmp_path)
        with patch("eneru.runtime._detect_runtime_context",
                   return_value="container (Docker)"):
            _inject_delegated_actions(config)

        _owner, server = _find_host_loopback(config)
        actions = [(c.action, c.path) for c in server.pre_shutdown_commands]
        # Order matches the in-process sequence: VMs → compose → containers → sync.
        assert actions == [
            ("stop_vms", None),
            ("stop_compose", "/etc/docker/compose/app.yml"),
            ("stop_containers", None),
            ("sync", None),
        ]

    @pytest.mark.unit
    def test_prepends_unmount_filesystems_when_configured(self, tmp_path):
        """v5.5 Commit 2: filesystem unmount delegation closes the parity gap."""
        from eneru import ConfigLoader
        from eneru.cli import _inject_delegated_actions, _find_host_loopback

        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "ups:\n"
            "  name: 'TestUPS@localhost'\n"
            "filesystems:\n"
            "  sync_enabled: true\n"
            "  unmount:\n"
            "    enabled: true\n"
            "    mounts:\n"
            "      - path: /mnt/data\n"
            "        options: '-l'\n"
            "remote_servers:\n"
            "  - name: host-loopback\n"
            "    enabled: true\n"
            "    host: 127.0.0.1\n"
            "    user: root\n"
            "    is_host_loopback: true\n"
        )
        config = ConfigLoader.load(str(config_file))
        with patch("eneru.runtime._detect_runtime_context",
                   return_value="container (Docker)"):
            _inject_delegated_actions(config)

        _owner, server = _find_host_loopback(config)
        action_names = [c.action for c in server.pre_shutdown_commands]
        # sync runs first (flushes caches), then unmount (releases the mount).
        assert "sync" in action_names
        assert "unmount_filesystems" in action_names
        assert action_names.index("sync") < action_names.index("unmount_filesystems")

    @pytest.mark.unit
    def test_skips_unmount_when_no_mounts_configured(self, tmp_path):
        """unmount.enabled=true with empty mounts list = nothing to do.
        Don't synthesize a no-op action."""
        from eneru import ConfigLoader
        from eneru.cli import _inject_delegated_actions, _find_host_loopback

        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "ups:\n"
            "  name: 'TestUPS@localhost'\n"
            "filesystems:\n"
            "  unmount:\n"
            "    enabled: true\n"
            "remote_servers:\n"
            "  - name: host-loopback\n"
            "    enabled: true\n"
            "    host: 127.0.0.1\n"
            "    user: root\n"
            "    is_host_loopback: true\n"
        )
        config = ConfigLoader.load(str(config_file))
        with patch("eneru.runtime._detect_runtime_context",
                   return_value="container (Docker)"):
            _inject_delegated_actions(config)

        _owner, server = _find_host_loopback(config)
        action_names = [c.action for c in server.pre_shutdown_commands]
        assert "unmount_filesystems" not in action_names

    @pytest.mark.unit
    def test_preserves_existing_user_pre_shutdown_commands(self, tmp_path):
        """User-defined pre_shutdown_commands must survive; generated actions
        prepend (do-the-work first, then user-extras)."""
        from eneru import ConfigLoader, RemoteCommandConfig
        from eneru.cli import _inject_delegated_actions, _find_host_loopback

        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "ups:\n"
            "  name: 'TestUPS@localhost'\n"
            "virtual_machines:\n"
            "  enabled: true\n"
            "remote_servers:\n"
            "  - name: host-loopback\n"
            "    enabled: true\n"
            "    host: 127.0.0.1\n"
            "    user: root\n"
            "    is_host_loopback: true\n"
            "    pre_shutdown_commands:\n"
            "      - command: '/usr/local/bin/notify-pager'\n"
        )
        config = ConfigLoader.load(str(config_file))

        with patch("eneru.runtime._detect_runtime_context",
                   return_value="container (Docker)"):
            _inject_delegated_actions(config)

        _owner, server = _find_host_loopback(config)
        actions = [
            (c.action, c.command) for c in server.pre_shutdown_commands
        ]
        assert actions[0] == ("stop_vms", None)
        # User custom command preserved at the end.
        assert (None, "/usr/local/bin/notify-pager") in actions

    @pytest.mark.unit
    def test_no_op_on_bare_metal(self, tmp_path):
        """Bare-metal install: no SSH-to-self injection, even if a loopback
        somehow appears in config (validation would reject it anyway)."""
        from eneru.cli import _inject_delegated_actions, _find_host_loopback

        config = self._config_with_loopback_and_caps(tmp_path)
        with patch("eneru.runtime._detect_runtime_context",
                   return_value="systemd service"):
            _inject_delegated_actions(config)

        _owner, server = _find_host_loopback(config)
        assert server.pre_shutdown_commands == []

    @pytest.mark.unit
    def test_no_op_when_no_loopback(self, tmp_path):
        """No loopback configured → nothing to inject into."""
        from eneru import ConfigLoader
        from eneru.cli import _inject_delegated_actions

        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "ups:\n"
            "  name: 'TestUPS@localhost'\n"
            "virtual_machines:\n"
            "  enabled: true\n"
        )
        config = ConfigLoader.load(str(config_file))
        # Should not raise; just no-op.
        with patch("eneru.runtime._detect_runtime_context",
                   return_value="container (Docker)"):
            _inject_delegated_actions(config)

    @pytest.mark.unit
    def test_disabled_loopback_is_ignored_for_injection(self, tmp_path):
        """A disabled loopback must not satisfy the delegation contract."""
        from eneru import ConfigLoader
        from eneru.cli import _inject_delegated_actions

        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "ups:\n"
            "  name: 'TestUPS@localhost'\n"
            "virtual_machines:\n"
            "  enabled: true\n"
            "remote_servers:\n"
            "  - name: host-loopback\n"
            "    enabled: false\n"
            "    host: 127.0.0.1\n"
            "    user: root\n"
            "    is_host_loopback: true\n"
        )
        config = ConfigLoader.load(str(config_file))
        with patch("eneru.runtime._detect_runtime_context",
                   return_value="container (Docker)"):
            _inject_delegated_actions(config)

        assert config.remote_servers[0].pre_shutdown_commands == []

    @pytest.mark.unit
    def test_explicit_false_blocks_synthesis(self, tmp_path):
        """is_host_loopback: false means the operator explicitly opted out."""
        from eneru import ConfigLoader
        from eneru.cli import _synthesize_loopback_if_needed

        config_file = tmp_path / "config.yaml"
        config_file.write_text(
            "ups:\n"
            "  name: 'TestUPS@localhost'\n"
            "virtual_machines:\n"
            "  enabled: true\n"
            "remote_servers:\n"
            "  - name: not-loopback\n"
            "    enabled: true\n"
            "    host: 127.0.0.1\n"
            "    user: root\n"
            "    is_host_loopback: false\n"
        )
        config = ConfigLoader.load(str(config_file))
        with patch("eneru.runtime._detect_runtime_context",
                   return_value="container (Docker)"):
            _synthesize_loopback_if_needed(config, strict_key_check=False)

        assert [s.name for s in config.remote_servers] == ["not-loopback"]


# ==============================================================================
# DEFENSIVE-BRANCH COVERAGE FOR DELEGATED REAL-RUN AND NATIVE FINAL-SYNC
# ==============================================================================


class TestDelegatedRealRunPath:
    """Covers the dry_run=False branches of the delegated shutdown
    sequence — sending the summary notification, flushing the
    notification worker, and writing the shutdown marker."""

    def _spy_phases_with_success(self, monitor):
        """Spy phases that return a successful loopback result."""
        calls = []
        monitor._shutdown_vms = lambda: calls.append("vms")
        monitor._shutdown_containers = lambda: calls.append("containers")
        monitor._sync_filesystems = lambda: calls.append("sync")
        monitor._unmount_filesystems = lambda: calls.append("unmount")

        def remote_spy():
            calls.append("remote")
            return [
                RemoteShutdownResult(
                    server="host-loopback",
                    host="127.0.0.1",
                    shutdown_sent=True,  # success=True
                )
            ]
        monitor._shutdown_remote_servers = remote_spy
        return calls

    @pytest.mark.unit
    def test_real_run_sends_summary_and_writes_marker(self, tmp_path):
        monitor = _make_delegated_monitor(tmp_path, dry_run=False)
        self._spy_phases_with_success(monitor)
        monitor._stats_store = MagicMock()
        monitor._send_notification = MagicMock()

        with _patch_runtime("container (Docker)"), \
             patch("eneru.monitor.run_command") as run_cmd, \
             patch("eneru.monitor.write_shutdown_marker") as marker:
            monitor._execute_shutdown_sequence()

        # Summary notification fires (success path, line 1204-1209).
        bodies = [c.args[0] for c in monitor._send_notification.call_args_list]
        assert any("Shutdown Sequence Complete" in b for b in bodies), bodies
        # Worker flush happens at line 1210-1211.
        monitor._notification_worker.flush.assert_called_once_with(timeout=5)
        # Marker written at line 1213-1217.
        marker.assert_called_once()
        # No inline shutdown command — the loopback already sent it.
        for call in run_cmd.call_args_list:
            assert "shutdown" not in call.args[0][0]


class TestNativeFinalSyncBranch:
    """Native (non-delegated) shutdown logs the `💾  Final filesystem sync...`
    line and either runs `os.sync()` or prints the dry-run preview."""

    @pytest.mark.unit
    def test_final_sync_logs_dry_run_preview(self, tmp_path):
        monitor = _make_delegated_monitor(
            tmp_path, loopback=False, dry_run=True,
        )
        # Stub all the phase methods so we only exercise the orchestration.
        monitor._shutdown_vms = MagicMock()
        monitor._shutdown_containers = MagicMock()
        monitor._sync_filesystems = MagicMock()
        monitor._unmount_filesystems = MagicMock()
        monitor._shutdown_remote_servers = MagicMock(return_value=[])
        monitor._stats_store = MagicMock()
        log = []
        original_log = monitor._log_message
        monitor._log_message = lambda msg, **kw: log.append(msg)

        with _patch_runtime("systemd service"), \
             patch.object(monitor, "_bounded_sync") as sync_mock:
            monitor._execute_shutdown_sequence()

        assert any("Final filesystem sync" in m for m in log), log
        # Dry-run: no actual sync call.
        sync_mock.assert_not_called()
        assert any("[DRY-RUN] Would perform final sync" in m for m in log), log

    @pytest.mark.unit
    def test_final_sync_invokes_os_sync_in_real_mode(self, tmp_path):
        monitor = _make_delegated_monitor(
            tmp_path,
            loopback=False,
            dry_run=False,
            local_shutdown_enabled=False,  # skip the local-shutdown branch
        )
        monitor._shutdown_vms = MagicMock()
        monitor._shutdown_containers = MagicMock()
        monitor._sync_filesystems = MagicMock()
        monitor._unmount_filesystems = MagicMock()
        monitor._shutdown_remote_servers = MagicMock(return_value=[])
        monitor._stats_store = MagicMock()
        monitor._send_notification = MagicMock()
        log = []
        monitor._log_message = lambda msg, **kw: log.append(msg)

        with _patch_runtime("systemd service"), \
             patch.object(monitor, "_bounded_sync") as sync_mock:
            monitor._execute_shutdown_sequence()

        # H6: the final sync now runs via the bounded `sync` subprocess helper.
        sync_mock.assert_called_once()
        assert any("Final filesystem sync" in m for m in log), log
