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
    return patch("eneru.cli._detect_runtime_context", return_value=runtime)


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
        monitor._shutdown_remote_servers = lambda: calls.append("remote")
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
             patch("eneru.monitor.os.sync") as sync_mock:
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
             patch("eneru.monitor.run_command") as run_cmd, \
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
        with patch("eneru.cli._detect_runtime_context",
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
        with patch("eneru.cli._detect_runtime_context",
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
        with patch("eneru.cli._detect_runtime_context",
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

        with patch("eneru.cli._detect_runtime_context",
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
        with patch("eneru.cli._detect_runtime_context",
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
        with patch("eneru.cli._detect_runtime_context",
                   return_value="container (Docker)"):
            _inject_delegated_actions(config)
