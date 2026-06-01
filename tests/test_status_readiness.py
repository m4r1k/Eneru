"""v5.5: readiness capability matrix.

Drives the per-runtime ``/ready`` decision: every required capability must
be achievable or readiness reports 503 (strict — Eneru is defense
technology; any broken contract surfaces loudly at every health probe).

Required capabilities are derived from config. Achievability depends on:
- ``nut_polling``: NUT connection state + last update time
- ``local_*``: native install → host binary on PATH; container → loopback
  delegate ``remote_health`` is HEALTHY
- ``remote_server_shutdown[name]``: that target's ``remote_health`` is
  HEALTHY (or UNKNOWN — probes treated as advisory)
"""

import time
from unittest.mock import MagicMock, patch

import pytest

from eneru import (
    Config, UPSConfig, UPSGroupConfig, TriggersConfig,
    VMConfig, ContainersConfig, FilesystemsConfig, UnmountConfig,
    RemoteServerConfig, LocalShutdownConfig, MonitorState,
    RedundancyGroupConfig,
)
from eneru.status import (
    readiness,
    _capability_achievable,
    _required_capabilities,
    _loopback_runtime_summary,
    _remote_server_summary,
    config_summary,
    remote_health_for_monitor,
    live_remote_health,
    query_events,
    query_history,
    redundancy_group_statuses,
)


def _make_source(*, config, snapshot):
    """Build a monitor-like source for readiness()."""
    source = MagicMock()
    source.config = config
    source._monitors = None  # single-UPS source
    monitor = MagicMock()
    monitor.config = config
    monitor._remote_health_manager = None
    monitor._remote_health_path = None
    monitor.state.snapshot.return_value = snapshot
    # iter_monitors falls back to [source] when source._monitors is None.
    source.state.snapshot.return_value = snapshot
    return source


def _ok_snapshot():
    snap = MagicMock()
    snap.connection_state = "OK"
    snap.last_update_time = 1234567890.0
    return snap


def _failed_snapshot():
    snap = MagicMock()
    snap.connection_state = "FAILED"
    snap.last_update_time = 0
    return snap


def _health_snapshot(*, status="OL", trigger_active=False):
    state = MonitorState()
    state.latest_status = status
    state.latest_battery_charge = "100"
    state.latest_runtime = "3600"
    state.latest_update_time = time.time()
    state.connection_state = "OK"
    state.trigger_active = trigger_active
    state.trigger_reason = "test trigger" if trigger_active else ""
    return state.snapshot()


def _bare_metal_config():
    return Config(
        ups_groups=[UPSGroupConfig(
            ups=UPSConfig(name="UPS@host"),
            is_local=True,
            virtual_machines=VMConfig(enabled=False),
            containers=ContainersConfig(enabled=False),
            filesystems=FilesystemsConfig(
                sync_enabled=True,
                unmount=UnmountConfig(enabled=False),
            ),
        )],
        local_shutdown=LocalShutdownConfig(enabled=False, trigger_on="none"),
    )


class TestRequiredCapabilities:
    """Pure config → capability list translation."""

    @pytest.mark.unit
    def test_minimal_remote_only_only_requires_nut_polling(self):
        caps = _required_capabilities(_bare_metal_config())
        assert caps == ["nut_polling"]

    @pytest.mark.unit
    def test_local_capabilities_added_when_configured(self):
        config = Config(
            ups_groups=[UPSGroupConfig(
                ups=UPSConfig(name="UPS@host"),
                is_local=True,
                virtual_machines=VMConfig(enabled=True),
                containers=ContainersConfig(enabled=True),
                filesystems=FilesystemsConfig(
                    sync_enabled=True,
                    unmount=UnmountConfig(enabled=True),
                ),
            )],
            local_shutdown=LocalShutdownConfig(enabled=True, trigger_on="any"),
        )
        caps = _required_capabilities(config)
        assert "local_vm_teardown" in caps
        assert "local_container_teardown" in caps
        assert "local_filesystem_unmount" in caps
        assert "local_host_poweroff" in caps

    @pytest.mark.unit
    def test_remote_server_capabilities_per_entry(self):
        config = Config(
            ups_groups=[UPSGroupConfig(
                ups=UPSConfig(name="UPS@host"),
                is_local=True,
                remote_servers=[
                    RemoteServerConfig(name="nas", enabled=True, host="10.0.0.1", user="root"),
                    RemoteServerConfig(name="db", enabled=True, host="10.0.0.2", user="root"),
                ],
            )],
            local_shutdown=LocalShutdownConfig(enabled=False, trigger_on="none"),
        )
        caps = _required_capabilities(config)
        assert "remote_server_shutdown[nas]" in caps
        assert "remote_server_shutdown[db]" in caps

    @pytest.mark.unit
    def test_duplicate_remote_names_are_scoped_by_group(self):
        config = Config(
            ups_groups=[
                UPSGroupConfig(
                    ups=UPSConfig(name="UPS-A@host", display_name="rack-a"),
                    remote_servers=[
                        RemoteServerConfig(
                            name="nas", enabled=True, host="10.0.0.1", user="root"
                        ),
                    ],
                ),
                UPSGroupConfig(
                    ups=UPSConfig(name="UPS-B@host", display_name="rack-b"),
                    remote_servers=[
                        RemoteServerConfig(
                            name="nas", enabled=True, host="10.0.0.2", user="root"
                        ),
                    ],
                ),
            ],
            local_shutdown=LocalShutdownConfig(enabled=False, trigger_on="none"),
        )
        caps = _required_capabilities(config)
        assert "remote_server_shutdown[rack-a/nas]" in caps
        assert "remote_server_shutdown[rack-b/nas]" in caps
        assert "remote_server_shutdown[nas]" not in caps

    @pytest.mark.unit
    def test_loopback_entries_not_listed_as_remote_targets(self):
        """The loopback is the host-poweroff transport, not a separately
        scored remote target."""
        config = Config(
            ups_groups=[UPSGroupConfig(
                ups=UPSConfig(name="UPS@host"),
                is_local=True,
                remote_servers=[
                    RemoteServerConfig(
                        name="host-loopback", enabled=True,
                        host="127.0.0.1", user="root",
                        is_host_loopback=True,
                    ),
                ],
            )],
            local_shutdown=LocalShutdownConfig(enabled=True, trigger_on="any"),
        )
        caps = _required_capabilities(config)
        assert "remote_server_shutdown[host-loopback]" not in caps
        assert "local_host_poweroff" in caps


class TestReadinessNativeInstall:
    """Bare-metal install: capability achievability depends on binary presence."""

    @pytest.mark.unit
    def test_ready_when_nut_ok_and_no_local_capabilities(self):
        config = _bare_metal_config()
        source = _make_source(config=config, snapshot=_ok_snapshot())
        with patch("eneru.status._runtime_context_label",
                   return_value="systemd service"):
            payload = readiness(source)
        assert payload["ready"] is True
        assert payload["runtime"]["container"] is False

    @pytest.mark.unit
    def test_not_ready_when_nut_failed(self):
        config = _bare_metal_config()
        source = _make_source(config=config, snapshot=_failed_snapshot())
        with patch("eneru.status._runtime_context_label",
                   return_value="systemd service"):
            payload = readiness(source)
        assert payload["ready"] is False
        assert any("nut_polling" in r for r in payload["reasons"])

    @pytest.mark.unit
    def test_not_ready_when_local_vm_configured_but_virsh_missing(self):
        config = Config(
            ups_groups=[UPSGroupConfig(
                ups=UPSConfig(name="UPS@host"),
                is_local=True,
                virtual_machines=VMConfig(enabled=True),
            )],
            local_shutdown=LocalShutdownConfig(enabled=False, trigger_on="none"),
        )
        source = _make_source(config=config, snapshot=_ok_snapshot())
        with patch("eneru.status._runtime_context_label",
                   return_value="systemd service"), \
             patch("eneru.status.command_exists", return_value=False):
            payload = readiness(source)
        assert payload["ready"] is False
        assert any("local_vm_teardown" in r for r in payload["reasons"])

    @pytest.mark.unit
    def test_coordinator_scores_full_config_not_last_monitor_only(self):
        full_config = Config(
            ups_groups=[
                UPSGroupConfig(
                    ups=UPSConfig(name="UPS-A@host"),
                    is_local=True,
                    virtual_machines=VMConfig(enabled=True),
                ),
                UPSGroupConfig(
                    ups=UPSConfig(name="UPS-B@host"),
                    is_local=False,
                ),
            ],
            local_shutdown=LocalShutdownConfig(enabled=False, trigger_on="none"),
        )
        monitor_a = MagicMock()
        monitor_a.config = Config(
            ups_groups=[full_config.ups_groups[0]],
            local_shutdown=full_config.local_shutdown,
        )
        monitor_a.state.snapshot.return_value = _ok_snapshot()
        monitor_b = MagicMock()
        monitor_b.config = Config(
            ups_groups=[full_config.ups_groups[1]],
            local_shutdown=full_config.local_shutdown,
        )
        monitor_b.state.snapshot.return_value = _ok_snapshot()
        source = MagicMock()
        source.config = full_config
        source._monitors = [monitor_a, monitor_b]

        with patch("eneru.status._runtime_context_label",
                   return_value="systemd service"), \
             patch("eneru.status.command_exists", return_value=False):
            payload = readiness(source)

        assert payload["ready"] is False
        assert any("local_vm_teardown" in r for r in payload["reasons"])
        vm_cap = next(
            c for c in payload["capabilities"] if c["id"] == "local_vm_teardown"
        )
        assert vm_cap["achievable"] is False
        assert "virsh" in vm_cap["reason"]

    @pytest.mark.unit
    def test_ready_when_local_vm_configured_and_virsh_present(self):
        config = Config(
            ups_groups=[UPSGroupConfig(
                ups=UPSConfig(name="UPS@host"),
                is_local=True,
                virtual_machines=VMConfig(enabled=True),
            )],
            local_shutdown=LocalShutdownConfig(enabled=False, trigger_on="none"),
        )
        source = _make_source(config=config, snapshot=_ok_snapshot())
        with patch("eneru.status._runtime_context_label",
                   return_value="systemd service"), \
             patch("eneru.status.command_exists", return_value=True):
            payload = readiness(source)
        assert payload["ready"] is True

    @pytest.mark.unit
    def test_local_poweroff_uses_configured_command_binary(self):
        """Readiness should check /sbin/halt when that is the configured command."""
        config = Config(
            ups_groups=[UPSGroupConfig(
                ups=UPSConfig(name="UPS@host"),
                is_local=True,
            )],
            local_shutdown=LocalShutdownConfig(
                enabled=True,
                command="/sbin/halt -p",
                trigger_on="any",
            ),
        )
        source = _make_source(config=config, snapshot=_ok_snapshot())

        def exists(binary):
            return binary == "/sbin/halt"

        with patch("eneru.status._runtime_context_label",
                   return_value="systemd service"), \
             patch("eneru.status.command_exists", side_effect=exists):
            payload = readiness(source)

        assert payload["ready"] is True

    @pytest.mark.unit
    def test_local_poweroff_skips_leading_sudo_when_checking_binary(self):
        config = Config(
            ups_groups=[UPSGroupConfig(
                ups=UPSConfig(name="UPS@host"),
                is_local=True,
            )],
            local_shutdown=LocalShutdownConfig(
                enabled=True,
                command="sudo -n /sbin/halt -p",
                trigger_on="any",
            ),
        )
        source = _make_source(config=config, snapshot=_ok_snapshot())

        def exists(binary):
            return binary == "/sbin/halt"

        with patch("eneru.status._runtime_context_label",
                   return_value="systemd service"), \
             patch("eneru.status.command_exists", side_effect=exists):
            payload = readiness(source)

        assert payload["ready"] is True

    @pytest.mark.unit
    def test_malformed_local_shutdown_command_is_not_ready(self):
        with patch("eneru.status.command_exists") as exists:
            achievable, reason = _capability_achievable(
                "local_host_poweroff",
                runtime_label="systemd service",
                nut_ready=True,
                loopback_status=None,
                remote_health_by_target={},
                local_shutdown_command="'unterminated",
            )

        assert achievable is False
        assert "invalid local shutdown command" in reason
        exists.assert_not_called()


class TestRedundancyGroupStatus:
    """API/dashboard redundancy rollups must mirror evaluator quorum policy."""

    @pytest.mark.unit
    def test_healthy_count_uses_effective_group_health(self):
        config = Config(
            ups_groups=[
                UPSGroupConfig(ups=UPSConfig(name="UPS-A@host")),
                UPSGroupConfig(ups=UPSConfig(name="UPS-B@host")),
            ],
            redundancy_groups=[
                RedundancyGroupConfig(
                    name="rack",
                    ups_sources=["UPS-A@host", "UPS-B@host"],
                    min_healthy=2,
                    degraded_counts_as="healthy",
                    unknown_counts_as="critical",
                ),
            ],
        )
        monitor_a = MagicMock()
        monitor_a.config = Config(ups_groups=[config.ups_groups[0]])
        monitor_a.state.snapshot.return_value = _health_snapshot(status="OB DISCHRG")
        monitor_b = MagicMock()
        monitor_b.config = Config(ups_groups=[config.ups_groups[1]])
        monitor_b.state.snapshot.return_value = _health_snapshot(status="OL")
        source = MagicMock()
        source._monitors = [monitor_a, monitor_b]
        source._redundancy_remote_health_managers = []

        rows = redundancy_group_statuses(source, config)

        assert rows[0]["healthyCount"] == 2
        assert rows[0]["quorumLost"] is False
        assert rows[0]["members"][0]["health"] == "degraded"
        assert rows[0]["members"][0]["effectiveHealth"] == "healthy"


class TestReadinessContainerWithLoopback:
    """Container with loopback: local_* achievability = loopback HEALTHY."""

    def _container_config(self):
        return Config(
            ups_groups=[UPSGroupConfig(
                ups=UPSConfig(name="UPS@host"),
                is_local=True,
                virtual_machines=VMConfig(enabled=True),
                containers=ContainersConfig(enabled=True),
                filesystems=FilesystemsConfig(
                    sync_enabled=True,
                    unmount=UnmountConfig(enabled=True),
                ),
                remote_servers=[
                    RemoteServerConfig(
                        name="host-loopback", enabled=True,
                        host="127.0.0.1", user="root",
                        is_host_loopback=True,
                    ),
                ],
            )],
            local_shutdown=LocalShutdownConfig(enabled=True, trigger_on="any"),
        )

    @pytest.mark.unit
    def test_ready_when_loopback_healthy(self):
        config = self._container_config()
        source = _make_source(config=config, snapshot=_ok_snapshot())
        with patch("eneru.status._runtime_context_label",
                   return_value="container (Docker)"), \
             patch("eneru.status.live_remote_health",
                   return_value=[{
                       "server": "host-loopback", "host": "127.0.0.1",
                       "status": "HEALTHY", "is_host_loopback": True,
                       "last_checked_at": 1.0, "last_error": "",
                   }]):
            payload = readiness(source)
        assert payload["ready"] is True
        assert payload["runtime"]["container"] is True
        assert payload["runtime"]["loopbackDelegate"]["status"] == "HEALTHY"

    @pytest.mark.unit
    def test_not_ready_when_loopback_failed(self):
        """Strict 503: even though NUT is fine, the broken loopback means the
        local shutdown contract cannot be honored."""
        config = self._container_config()
        source = _make_source(config=config, snapshot=_ok_snapshot())
        with patch("eneru.status._runtime_context_label",
                   return_value="container (Docker)"), \
             patch("eneru.status.live_remote_health",
                   return_value=[{
                       "server": "host-loopback", "host": "127.0.0.1",
                       "status": "FAILED", "is_host_loopback": True,
                       "last_checked_at": 1.0,
                       "last_error": "host identity mismatch",
                   }]):
            payload = readiness(source)
        assert payload["ready"] is False
        # All four local capabilities should be flagged as unachievable.
        local_caps = [c for c in payload["capabilities"] if c["id"].startswith("local_")]
        assert all(c["achievable"] is False for c in local_caps)
        assert any("FAILED" in c["reason"] for c in local_caps)

    @pytest.mark.unit
    def test_strict_503_with_remote_target_healthy_but_loopback_failed(self):
        """Federico's confirmed strict semantics: partial functionality still
        reports 503 because the shutdown contract is incomplete."""
        config = self._container_config()
        # Add a healthy remote target alongside the failed loopback.
        config.ups_groups[0].remote_servers.append(
            RemoteServerConfig(name="nas", enabled=True, host="10.0.0.5", user="root")
        )
        source = _make_source(config=config, snapshot=_ok_snapshot())
        with patch("eneru.status._runtime_context_label",
                   return_value="container (Docker)"), \
             patch("eneru.status.live_remote_health",
                   return_value=[
                       {"server": "host-loopback", "host": "127.0.0.1",
                        "status": "FAILED", "is_host_loopback": True,
                        "last_checked_at": 1.0, "last_error": "ssh refused"},
                       {"server": "nas", "host": "10.0.0.5",
                        "status": "HEALTHY", "is_host_loopback": False,
                        "last_checked_at": 1.0, "last_error": ""},
                   ]):
            payload = readiness(source)
        assert payload["ready"] is False  # strict, even though nas is HEALTHY
        nas_cap = next(
            c for c in payload["capabilities"]
            if c["id"] == "remote_server_shutdown[nas]"
        )
        assert nas_cap["achievable"] is True

    @pytest.mark.unit
    @pytest.mark.parametrize("health_status", ["UNKNOWN", "DISABLED"])
    def test_regular_remote_unknown_or_disabled_is_still_achievable(
        self, health_status
    ):
        achievable, reason = _capability_achievable(
            "remote_server_shutdown[nas]",
            runtime_label="container (Docker)",
            nut_ready=True,
            loopback_status="HEALTHY",
            remote_health_by_target={"nas": health_status},
        )

        assert achievable is True
        assert reason == ""

    @pytest.mark.unit
    def test_regular_remote_failed_blocks_readiness(self):
        achievable, reason = _capability_achievable(
            "remote_server_shutdown[nas]",
            runtime_label="container (Docker)",
            nut_ready=True,
            loopback_status="HEALTHY",
            remote_health_by_target={"nas": "FAILED"},
        )

        assert achievable is False
        assert "nas" in reason

    @pytest.mark.unit
    def test_duplicate_remote_health_rows_do_not_mask_each_other(self):
        config = Config(
            ups_groups=[
                UPSGroupConfig(
                    ups=UPSConfig(name="UPS-A@host", display_name="rack-a"),
                    remote_servers=[
                        RemoteServerConfig(
                            name="nas", enabled=True, host="10.0.0.1", user="root"
                        ),
                    ],
                ),
                UPSGroupConfig(
                    ups=UPSConfig(name="UPS-B@host", display_name="rack-b"),
                    remote_servers=[
                        RemoteServerConfig(
                            name="nas", enabled=True, host="10.0.0.2", user="root"
                        ),
                    ],
                ),
            ],
            local_shutdown=LocalShutdownConfig(enabled=False, trigger_on="none"),
        )
        source = _make_source(config=config, snapshot=_ok_snapshot())
        with patch("eneru.status._runtime_context_label",
                   return_value="container (Docker)"), \
             patch("eneru.status.live_remote_health",
                   return_value=[
                       {"group": "rack-a", "server": "nas", "host": "10.0.0.1",
                        "status": "FAILED", "is_host_loopback": False},
                       {"group": "rack-b", "server": "nas", "host": "10.0.0.2",
                        "status": "HEALTHY", "is_host_loopback": False},
                       {"group": "rack-a", "server": "host-loopback", "host": "127.0.0.1",
                        "status": "HEALTHY", "is_host_loopback": True},
                   ]):
            payload = readiness(source)

        assert payload["ready"] is False
        rack_a = next(
            c for c in payload["capabilities"]
            if c["id"] == "remote_server_shutdown[rack-a/nas]"
        )
        rack_b = next(
            c for c in payload["capabilities"]
            if c["id"] == "remote_server_shutdown[rack-b/nas]"
        )
        assert rack_a["achievable"] is False
        assert rack_b["achievable"] is True


class TestReadinessContainerNoLoopback:
    """Container with local capabilities but no loopback configured."""

    @pytest.mark.unit
    def test_local_capabilities_not_achievable(self):
        """No loopback in container → all local_* report unachievable with
        a doc-pointer hint."""
        config = Config(
            ups_groups=[UPSGroupConfig(
                ups=UPSConfig(name="UPS@host"),
                is_local=True,
                virtual_machines=VMConfig(enabled=True),
            )],
            local_shutdown=LocalShutdownConfig(enabled=True, trigger_on="any"),
        )
        source = _make_source(config=config, snapshot=_ok_snapshot())
        with patch("eneru.status._runtime_context_label",
                   return_value="container (Kubernetes)"), \
             patch("eneru.status.live_remote_health", return_value=[]):
            payload = readiness(source)
        assert payload["ready"] is False
        vm_cap = next(
            c for c in payload["capabilities"] if c["id"] == "local_vm_teardown"
        )
        assert vm_cap["achievable"] is False
        assert "install-comparison.md" in vm_cap["reason"]


class TestReadinessLoopbackRuntimePayload:
    """The runtime.loopbackDelegate.* block in /ready."""

    @pytest.mark.unit
    def test_configured_false_when_no_loopback(self):
        config = _bare_metal_config()
        source = _make_source(config=config, snapshot=_ok_snapshot())
        with patch("eneru.status._runtime_context_label",
                   return_value="systemd service"):
            payload = readiness(source)
        assert payload["runtime"]["loopbackDelegate"] == {"configured": False}

    @pytest.mark.unit
    def test_configured_true_with_host_user(self):
        config = Config(
            ups_groups=[UPSGroupConfig(
                ups=UPSConfig(name="UPS@host"),
                is_local=True,
                virtual_machines=VMConfig(enabled=True),
                remote_servers=[
                    RemoteServerConfig(
                        name="host-loopback", enabled=True,
                        host="127.0.0.1", user="root",
                        is_host_loopback=True,
                    ),
                ],
            )],
            local_shutdown=LocalShutdownConfig(enabled=True, trigger_on="any"),
        )
        source = _make_source(config=config, snapshot=_ok_snapshot())
        with patch("eneru.status._runtime_context_label",
                   return_value="container (Docker)"), \
             patch("eneru.status.live_remote_health", return_value=[]):
            payload = readiness(source)
        delegate = payload["runtime"]["loopbackDelegate"]
        assert delegate["configured"] is True
        assert delegate["host"] == "127.0.0.1"
        assert delegate["user"] == "root"

    @pytest.mark.unit
    def test_disabled_loopback_is_not_reported_as_configured(self):
        config = Config(
            ups_groups=[UPSGroupConfig(
                ups=UPSConfig(name="UPS@host"),
                is_local=True,
                remote_servers=[
                    RemoteServerConfig(
                        name="host-loopback", enabled=False,
                        host="127.0.0.1", user="root",
                        is_host_loopback=True,
                    ),
                ],
            )],
            local_shutdown=LocalShutdownConfig(enabled=True, trigger_on="any"),
        )
        source = _make_source(config=config, snapshot=_ok_snapshot())
        with patch("eneru.status._runtime_context_label",
                   return_value="container (Docker)"), \
             patch("eneru.status.live_remote_health", return_value=[]):
            payload = readiness(source)

        assert payload["runtime"]["loopbackDelegate"] == {"configured": False}


class TestRequiredCapabilitiesRedundancy:
    """Redundancy groups contribute capabilities the same way ups_groups do."""

    @pytest.mark.unit
    def test_redundancy_group_adds_local_capabilities(self):
        config = Config(
            ups_groups=[UPSGroupConfig(
                ups=UPSConfig(name="UPS@host"),
                is_local=False,
            )],
            redundancy_groups=[RedundancyGroupConfig(
                name="rack",
                ups_sources=["UPS@host"],
                is_local=True,
                virtual_machines=VMConfig(enabled=True),
                containers=ContainersConfig(enabled=True),
                filesystems=FilesystemsConfig(
                    sync_enabled=True,
                    unmount=UnmountConfig(enabled=True),
                ),
            )],
            local_shutdown=LocalShutdownConfig(enabled=False, trigger_on="none"),
        )
        caps = _required_capabilities(config)
        assert "local_vm_teardown" in caps
        assert "local_container_teardown" in caps
        assert "local_filesystem_unmount" in caps

    @pytest.mark.unit
    def test_non_local_redundancy_group_skips_local_capability_loop(self):
        """Mirror the ups_groups guard: ``is_local=False`` skips the
        local_* capabilities even when VMs / containers are enabled."""
        config = Config(
            ups_groups=[UPSGroupConfig(
                ups=UPSConfig(name="UPS@host"),
                is_local=False,
            )],
            redundancy_groups=[RedundancyGroupConfig(
                name="rack",
                ups_sources=["UPS@host"],
                is_local=False,
                virtual_machines=VMConfig(enabled=True),
            )],
            local_shutdown=LocalShutdownConfig(enabled=False, trigger_on="none"),
        )
        caps = _required_capabilities(config)
        assert "local_vm_teardown" not in caps

    @pytest.mark.unit
    def test_redundancy_group_remote_servers_are_capabilities(self):
        config = Config(
            ups_groups=[UPSGroupConfig(
                ups=UPSConfig(name="UPS@host"),
                is_local=False,
            )],
            redundancy_groups=[RedundancyGroupConfig(
                name="rack-a",
                ups_sources=["UPS@host"],
                is_local=True,
                remote_servers=[
                    RemoteServerConfig(
                        name="nas", enabled=True,
                        host="10.0.0.1", user="root",
                    ),
                ],
            )],
            local_shutdown=LocalShutdownConfig(enabled=False, trigger_on="none"),
        )
        caps = _required_capabilities(config)
        assert "remote_server_shutdown[nas]" in caps


class TestCapabilityAchievableEmptyCommand:
    """An empty local_shutdown command means no binary check is required."""

    @pytest.mark.unit
    def test_empty_command_is_achievable_with_no_binary_check(self):
        with patch("eneru.status.command_exists") as exists:
            achievable, reason = _capability_achievable(
                "local_host_poweroff",
                runtime_label="systemd service",
                nut_ready=True,
                loopback_status=None,
                remote_health_by_target={},
                local_shutdown_command="",
            )
        assert achievable is True
        assert reason == ""
        exists.assert_not_called()


class TestCapabilityAchievableSudoParsing:
    """CodeRabbit #1: the local_host_poweroff candidate scanner must look
    through every sudo option form, not just `sudo -n`."""

    @pytest.mark.unit
    def test_sudo_dash_u_user_picks_real_binary(self):
        """`sudo -u root shutdown -h now` must score `shutdown`, not `-u`."""
        with patch("eneru.status.command_exists") as exists:
            exists.return_value = True
            achievable, _ = _capability_achievable(
                "local_host_poweroff",
                runtime_label="systemd service",
                nut_ready=True,
                loopback_status=None,
                remote_health_by_target={},
                local_shutdown_command="sudo -u root shutdown -h now",
            )
        assert achievable is True
        # The exists() check must have been called with `shutdown`, not
        # with `-u` (the old code's misclassification).
        called_with = {c.args[0] for c in exists.call_args_list}
        assert "shutdown" in called_with
        assert "-u" not in called_with

    @pytest.mark.unit
    def test_sudo_dash_n_still_works(self):
        """Sanity: the original `sudo -n shutdown` form still resolves
        to `shutdown`."""
        with patch("eneru.status.command_exists") as exists:
            exists.return_value = True
            achievable, _ = _capability_achievable(
                "local_host_poweroff",
                runtime_label="systemd service",
                nut_ready=True,
                loopback_status=None,
                remote_health_by_target={},
                local_shutdown_command="sudo -n shutdown -h now",
            )
        assert achievable is True
        assert "shutdown" in {c.args[0] for c in exists.call_args_list}

    @pytest.mark.unit
    def test_sudo_double_dash_terminator_stops_flag_scan(self):
        """`sudo -- shutdown` must treat `--` as the end of flags."""
        with patch("eneru.status.command_exists") as exists:
            exists.return_value = True
            _capability_achievable(
                "local_host_poweroff",
                runtime_label="systemd service",
                nut_ready=True,
                loopback_status=None,
                remote_health_by_target={},
                local_shutdown_command="sudo -- shutdown -h now",
            )
        assert "shutdown" in {c.args[0] for c in exists.call_args_list}


class TestLoopbackRuntimeSummaryRedundancy:
    """A loopback configured on a redundancy group must surface in /ready."""

    @pytest.mark.unit
    def test_redundancy_group_loopback_is_reported_as_configured(self):
        config = Config(
            ups_groups=[UPSGroupConfig(
                ups=UPSConfig(name="UPS@host"),
                is_local=False,
            )],
            redundancy_groups=[RedundancyGroupConfig(
                name="rack",
                ups_sources=["UPS@host"],
                is_local=True,
                remote_servers=[
                    RemoteServerConfig(
                        name="host-loopback", enabled=True,
                        host="172.17.0.1", user="root",
                        is_host_loopback=True,
                    ),
                ],
            )],
        )
        summary = _loopback_runtime_summary(config, loopback_row=None)
        assert summary["configured"] is True
        assert summary["host"] == "172.17.0.1"
        assert summary["user"] == "root"


class TestRemoteServerSummary:
    """The sanitized server summary feeds config_summary()."""

    @pytest.mark.unit
    def test_summary_uses_name_when_set(self):
        server = RemoteServerConfig(
            name="nas", enabled=True, host="10.0.0.5", user="root",
        )
        out = _remote_server_summary(server)
        assert out["name"] == "nas"
        assert out["host"] == "10.0.0.5"
        assert out["user"] == "root"
        assert out["enabled"] is True
        assert out["isHostLoopback"] is False

    @pytest.mark.unit
    def test_summary_falls_back_to_host_when_name_blank(self):
        server = RemoteServerConfig(
            name="", enabled=True, host="10.0.0.7", user="root",
        )
        out = _remote_server_summary(server)
        # Falls back to host when name is empty (line 491).
        assert out["name"] == "10.0.0.7"

    @pytest.mark.unit
    def test_config_summary_includes_remote_servers(self):
        config = Config(
            ups_groups=[UPSGroupConfig(
                ups=UPSConfig(name="UPS@host"),
                is_local=True,
                remote_servers=[
                    RemoteServerConfig(
                        name="nas", enabled=True,
                        host="10.0.0.1", user="root",
                    ),
                ],
            )],
        )
        out = config_summary(config)
        assert out["ups"][0]["remoteServers"][0]["name"] == "nas"


class TestRemoteHealthForMonitor:
    """Live-manager snapshots win over the on-disk sidecar."""

    @pytest.mark.unit
    def test_live_manager_snapshot_returned_when_present(self):
        monitor = MagicMock()
        monitor._remote_health_manager.snapshot.return_value = [
            {"server": "nas", "status": "HEALTHY"}
        ]
        # Sidecar path is set but should not be used.
        monitor._remote_health_path = "/should/not/be/read"
        rows = remote_health_for_monitor(monitor)
        assert rows == [{"server": "nas", "status": "HEALTHY"}]


class TestLiveRemoteHealthRedundancyManagers:
    """Redundancy-group managers held on the coordinator must be merged in."""

    @pytest.mark.unit
    def test_redundancy_manager_rows_are_included(self):
        config = _bare_metal_config()
        source = MagicMock()
        source._remote_health_manager = None
        source._monitors = []
        rm = MagicMock()
        rm.snapshot.return_value = [
            {"server": "nas", "status": "HEALTHY", "group": "rack-a"},
        ]
        source._redundancy_remote_health_managers = [rm]
        rows = live_remote_health(source, config)
        assert rows == [
            {"server": "nas", "status": "HEALTHY", "group": "rack-a"},
        ]


class TestQueryEventsAndHistoryEdgeCases:
    """Defensive edge cases in query_events and query_history."""

    @pytest.mark.unit
    def test_query_events_skips_groups_without_a_db(self, tmp_path):
        """An unopened DB returns None from open_readonly and is skipped."""
        config = Config(
            ups_groups=[
                UPSGroupConfig(ups=UPSConfig(name="UPS1@h")),
                UPSGroupConfig(ups=UPSConfig(name="UPS2@h")),
            ],
        )
        # Both DBs absent — open_readonly returns None for each → empty list.
        rows = query_events(config, limit=10, verbosity=2)
        assert rows == []

    @pytest.mark.unit
    def test_query_events_swallows_connection_close_errors(self):
        """A close() exception in the cleanup must not propagate."""
        config = Config(
            ups_groups=[UPSGroupConfig(ups=UPSConfig(name="UPS@h"))],
        )
        bad_conn = MagicMock()
        bad_conn.close.side_effect = RuntimeError("close failed")
        with patch("eneru.status.StatsStore.open_readonly",
                   return_value=bad_conn), \
             patch("eneru.status.StatsStore.from_connection") as from_conn:
            store = MagicMock()
            store.query_recent_events.return_value = []
            from_conn.return_value = store
            # Should NOT raise.
            rows = query_events(config, limit=5, verbosity=2)
        assert rows == []
        bad_conn.close.assert_called_once()

    @pytest.mark.unit
    def test_query_history_swallows_connection_close_errors(self):
        config = Config(
            ups_groups=[UPSGroupConfig(ups=UPSConfig(name="UPS@h"))],
        )
        bad_conn = MagicMock()
        bad_conn.close.side_effect = RuntimeError("close failed")
        with patch("eneru.status.StatsStore.open_readonly",
                   return_value=bad_conn), \
             patch("eneru.status.StatsStore.from_connection") as from_conn:
            store = MagicMock()
            store.query_range.return_value = [(100, 50.0), (200, 60.0)]
            from_conn.return_value = store
            rows = query_history(config, "UPS@h", "charge", 0, 1000)
        assert rows == [{"ts": 100, "value": 50.0}, {"ts": 200, "value": 60.0}]
        bad_conn.close.assert_called_once()


# ====================================================================
# Helpers and read-through paths exercised by API / metrics / TUI
# ====================================================================


class TestStatusHelpers:
    """Pure-logic helpers shared across the API and TUI."""

    @pytest.mark.unit
    def test_sanitize_name_replaces_unsafe_characters(self):
        from eneru.status import sanitize_name
        assert sanitize_name("UPS@host:3493") == "UPS-host-3493"
        assert sanitize_name("a/b") == "a-b"

    @pytest.mark.unit
    def test_stats_db_path_uses_default_for_single_ups(self, tmp_path):
        from eneru.status import stats_db_path_for_group
        from eneru import StatsConfig
        config = Config(
            ups_groups=[UPSGroupConfig(ups=UPSConfig(name="UPS@h"))],
            statistics=StatsConfig(db_directory=str(tmp_path)),
        )
        assert stats_db_path_for_group(
            config, config.ups_groups[0]
        ) == tmp_path / "default.db"

    @pytest.mark.unit
    def test_stats_db_path_uses_sanitized_name_for_multi_ups(self, tmp_path):
        from eneru.status import stats_db_path_for_group
        from eneru import StatsConfig
        config = Config(
            ups_groups=[
                UPSGroupConfig(ups=UPSConfig(name="UPS1@h:3493")),
                UPSGroupConfig(ups=UPSConfig(name="UPS2@h:3493")),
            ],
            statistics=StatsConfig(db_directory=str(tmp_path)),
        )
        assert stats_db_path_for_group(
            config, config.ups_groups[0]
        ) == tmp_path / "UPS1-h-3493.db"

    @pytest.mark.unit
    def test_state_file_path_appends_suffix_in_multi_ups(self, tmp_path):
        from eneru.status import state_file_path_for_group
        from eneru import LoggingConfig
        config = Config(
            ups_groups=[
                UPSGroupConfig(ups=UPSConfig(name="UPS1@h")),
                UPSGroupConfig(ups=UPSConfig(name="UPS2@h")),
            ],
            logging=LoggingConfig(state_file=str(tmp_path / "state")),
        )
        from pathlib import Path
        assert state_file_path_for_group(
            config, config.ups_groups[0]
        ) == Path(str(tmp_path / "state") + ".UPS1-h")

    @pytest.mark.unit
    def test_state_file_path_bare_in_single_ups(self, tmp_path):
        from eneru.status import state_file_path_for_group
        from eneru import LoggingConfig
        from pathlib import Path
        config = Config(
            ups_groups=[UPSGroupConfig(ups=UPSConfig(name="UPS@h"))],
            logging=LoggingConfig(state_file=str(tmp_path / "state")),
        )
        assert state_file_path_for_group(
            config, config.ups_groups[0]
        ) == Path(str(tmp_path / "state"))

    @pytest.mark.unit
    def test_redundancy_state_file_path(self, tmp_path):
        from eneru.status import redundancy_state_file_path
        from eneru import LoggingConfig
        from pathlib import Path
        config = Config(
            logging=LoggingConfig(state_file=str(tmp_path / "state")),
        )
        assert redundancy_state_file_path(
            config, "rack-a"
        ) == Path(str(tmp_path / "state") + ".redundancy-rack-a")


class TestMonitorStatus:
    """``monitor_status`` shapes one monitor's snapshot into the API payload."""

    def _build_monitor(self):
        config = Config(
            ups_groups=[UPSGroupConfig(
                ups=UPSConfig(
                    name="UPS@host",
                    display_name="Rack A",
                ),
                is_local=True,
            )],
        )
        monitor = MagicMock()
        monitor.config = config
        snap = MagicMock()
        snap.status = "OL"
        snap.battery_charge = 87.0
        snap.runtime = 1800
        snap.load = 12
        snap.input_voltage = 230.0
        snap.output_voltage = 230.0
        snap.battery_voltage = 13.5
        snap.ups_temperature = 25
        snap.input_frequency = 50.0
        snap.output_frequency = 50.0
        snap.voltage_state = "NORMAL"
        snap.avr_state = "INACTIVE"
        snap.bypass_state = "INACTIVE"
        snap.overload_state = "INACTIVE"
        snap.nominal_voltage = 230
        snap.voltage_warning_low = 200
        snap.voltage_warning_high = 250
        snap.depletion_rate = 0.0
        snap.time_on_battery = 0
        snap.last_update_time = 1234.0
        snap.connection_state = "OK"
        snap.trigger_active = False
        snap.trigger_reason = ""
        snap.stale_data_count = 0
        monitor.state.snapshot.return_value = snap
        monitor._remote_health_manager = None
        monitor._remote_health_path = None
        return monitor

    @pytest.mark.unit
    def test_returns_serializable_payload_with_label_and_display_name(self):
        from eneru.status import monitor_status
        out = monitor_status(self._build_monitor())
        assert out["name"] == "UPS@host"
        assert out["label"] == "Rack A"
        assert out["displayName"] == "Rack A"
        assert out["isLocal"] is True
        assert out["powerQuality"]["inputVoltage"] == 230.0
        assert out["remoteHealth"] == []


class TestCollectStatusAndRedundancy:
    """``collect_status`` + ``redundancy_group_statuses`` API entrypoints."""

    @pytest.mark.unit
    def test_redundancy_group_statuses_returns_empty_when_no_config(self):
        from eneru.status import redundancy_group_statuses
        assert redundancy_group_statuses(MagicMock(), None) == []

    @pytest.mark.unit
    def test_redundancy_group_statuses_uses_live_manager_when_present(
        self, tmp_path
    ):
        from eneru.status import redundancy_group_statuses
        from eneru import LoggingConfig
        config = Config(
            redundancy_groups=[RedundancyGroupConfig(
                name="rack-a",
                ups_sources=["UPS@h"],
                min_healthy=1,
                is_local=True,
            )],
            logging=LoggingConfig(state_file=str(tmp_path / "state")),
        )
        manager = MagicMock()
        manager.group_label = "redundancy:rack-a"
        manager.snapshot.return_value = [
            {"server": "nas", "status": "HEALTHY"},
        ]
        source = MagicMock()
        source._redundancy_remote_health_managers = [manager]
        rows = redundancy_group_statuses(source, config)
        assert len(rows) == 1
        assert rows[0]["name"] == "rack-a"
        assert rows[0]["upsSources"] == ["UPS@h"]
        assert rows[0]["minHealthy"] == 1
        assert rows[0]["remoteHealth"] == [
            {"server": "nas", "status": "HEALTHY"},
        ]

    @pytest.mark.unit
    def test_collect_status_includes_runtime_block(self):
        """When a config is attached, the payload exposes the v5.5
        runtime + loopbackDelegate block."""
        from eneru.status import collect_status

        config = Config(
            ups_groups=[UPSGroupConfig(
                ups=UPSConfig(name="UPS@h"),
                is_local=True,
            )],
        )
        monitor = MagicMock()
        monitor.config = config
        snap = MagicMock()
        # MagicMock auto-vivs all snapshot attributes — only override the
        # few the runtime block depends on so the payload stays sane.
        snap.connection_state = "OK"
        snap.last_update_time = 1.0
        monitor.state.snapshot.return_value = snap
        monitor._remote_health_manager = None
        monitor._remote_health_path = None
        source = MagicMock()
        source.config = config
        source._monitors = None
        source._remote_health_manager = None
        source._redundancy_remote_health_managers = []
        source.state.snapshot.return_value = snap

        with patch("eneru.status.iter_monitors", return_value=[monitor]), \
             patch("eneru.status._runtime_context_label",
                   return_value="systemd service"):
            payload = collect_status(source)

        assert "runtime" in payload
        assert payload["runtime"]["context"] == "systemd service"
        assert payload["runtime"]["loopbackDelegate"] == {"configured": False}
        assert payload["ups"][0]["name"] == "UPS@h"

    @pytest.mark.unit
    def test_collect_status_omits_runtime_when_no_config(self):
        """A source without a ``config`` attribute (defensive) gets no
        runtime block — the function must still return a payload."""
        from eneru.status import collect_status
        source = MagicMock(spec=["state", "_monitors"])
        source._monitors = []
        payload = collect_status(source)
        assert "runtime" not in payload
        assert payload["ups"] == []


class TestRemoteHealthSidecarFallback:
    """When no live manager exists, ``remote_health_for_monitor`` falls back
    to the on-disk sidecar."""

    @pytest.mark.unit
    def test_sidecar_path_is_read_when_no_manager(self, tmp_path):
        from eneru.status import remote_health_for_monitor
        sidecar = tmp_path / "state.remote_health.json"
        monitor = MagicMock()
        monitor._remote_health_manager = None
        monitor._remote_health_path = sidecar
        with patch("eneru.status.read_remote_health_sidecar",
                   return_value=[{"server": "nas", "status": "HEALTHY"}]
                   ) as reader:
            rows = remote_health_for_monitor(monitor)
        reader.assert_called_once_with(sidecar)
        assert rows == [{"server": "nas", "status": "HEALTHY"}]

    @pytest.mark.unit
    def test_returns_empty_when_neither_manager_nor_sidecar(self):
        from eneru.status import remote_health_for_monitor
        monitor = MagicMock()
        monitor._remote_health_manager = None
        monitor._remote_health_path = None
        assert remote_health_for_monitor(monitor) == []


class TestRemoteHealthForConfig:
    """``remote_health_for_config`` reads sidecars for both group types."""

    @pytest.mark.unit
    def test_reads_sidecars_for_ups_and_redundancy_groups(self, tmp_path):
        from eneru.status import remote_health_for_config
        from eneru import LoggingConfig
        config = Config(
            ups_groups=[UPSGroupConfig(ups=UPSConfig(name="UPS@h"))],
            redundancy_groups=[RedundancyGroupConfig(
                name="rack-a", ups_sources=["UPS@h"],
            )],
            logging=LoggingConfig(state_file=str(tmp_path / "state")),
        )
        with patch("eneru.status.read_remote_health_sidecar",
                   side_effect=[
                       [{"server": "ups-side"}],
                       [{"server": "redundancy-side"}],
                   ]) as reader:
            rows = remote_health_for_config(config)
        assert reader.call_count == 2
        assert rows == [
            {"server": "ups-side"},
            {"server": "redundancy-side"},
        ]


class TestLiveRemoteHealthFallback:
    """When no live managers fire, fall back to disk sidecars."""

    @pytest.mark.unit
    def test_falls_back_to_sidecar_when_no_managers(self):
        config = _bare_metal_config()
        source = MagicMock(spec=[])  # no _remote_health_manager / _monitors / ...
        with patch("eneru.status.remote_health_for_config",
                   return_value=[{"server": "from-disk"}]) as fallback:
            rows = live_remote_health(source, config)
        fallback.assert_called_once_with(config)
        assert rows == [{"server": "from-disk"}]

    @pytest.mark.unit
    def test_uses_own_manager_when_source_has_one(self):
        """Single-UPS source: the source itself owns the manager."""
        config = _bare_metal_config()
        source = MagicMock()
        source._monitors = []
        source._redundancy_remote_health_managers = []
        source._remote_health_manager.snapshot.return_value = [
            {"server": "live", "status": "HEALTHY"},
        ]
        rows = live_remote_health(source, config)
        assert rows == [{"server": "live", "status": "HEALTHY"}]

    @pytest.mark.unit
    def test_aggregates_per_monitor_managers_in_multi_ups(self):
        config = Config(
            ups_groups=[
                UPSGroupConfig(ups=UPSConfig(name="UPS1@h")),
                UPSGroupConfig(ups=UPSConfig(name="UPS2@h")),
            ],
        )
        source = MagicMock()
        source._remote_health_manager = None
        source._redundancy_remote_health_managers = []
        m1 = MagicMock()
        m1._remote_health_manager.snapshot.return_value = [{"server": "a"}]
        m2 = MagicMock()
        m2._remote_health_manager.snapshot.return_value = [{"server": "b"}]
        source._monitors = [m1, m2]
        rows = live_remote_health(source, config)
        assert rows == [{"server": "a"}, {"server": "b"}]


class TestQueryEventsAndHistoryHappyPath:
    """Happy-path coverage for query_events + query_history."""

    @pytest.mark.unit
    def test_query_events_returns_sorted_dicts_from_each_db(self):
        config = Config(
            ups_groups=[UPSGroupConfig(
                ups=UPSConfig(name="UPS@h", display_name="Rack"),
            )],
        )
        good_conn = MagicMock()
        with patch("eneru.status.StatsStore.open_readonly",
                   return_value=good_conn), \
             patch("eneru.status.StatsStore.from_connection") as from_conn:
            store = MagicMock()
            # v5: query_recent_events(include_id=True) yields (id, ts, type, detail).
            store.query_recent_events.return_value = [
                (2, 200, "POWER_RESTORED", "back"),
                (1, 100, "ON_BATTERY", "outage"),
            ]
            from_conn.return_value = store
            rows = query_events(config, limit=10, verbosity=0)
        # Sorted ascending by ts.
        assert [r["ts"] for r in rows] == [100, 200]
        assert rows[0]["eventType"] == "ON_BATTERY"
        assert rows[0]["label"] == "Rack"
        assert rows[0]["ups"] == "UPS@h"
        # Source-qualified identity is exposed for the API/dashboard.
        assert rows[0]["id"] == 1
        assert rows[0]["source"] == "UPS-h"

    @pytest.mark.unit
    def test_query_history_unknown_metric_returns_none(self):
        config = Config(
            ups_groups=[UPSGroupConfig(ups=UPSConfig(name="UPS@h"))],
        )
        assert query_history(config, "UPS@h", "bogus", 0, 1000) is None

    @pytest.mark.unit
    def test_query_history_unknown_ups_returns_none(self):
        config = Config(
            ups_groups=[UPSGroupConfig(ups=UPSConfig(name="UPS@h"))],
        )
        assert query_history(config, "other-ups", "charge", 0, 1000) is None

    @pytest.mark.unit
    def test_query_history_no_db_returns_empty_list(self):
        """Distinct from unknown UPS/metric: an absent DB returns []."""
        config = Config(
            ups_groups=[UPSGroupConfig(ups=UPSConfig(name="UPS@h"))],
        )
        with patch("eneru.status.StatsStore.open_readonly",
                   return_value=None):
            assert query_history(config, "UPS@h", "charge", 0, 1000) == []


class TestFindStatus:
    """``find_status`` looks up a single UPS row in a /status payload."""

    @pytest.mark.unit
    def test_find_by_raw_name(self):
        from eneru.status import find_status
        payload = {"ups": [{"name": "UPS@h"}, {"name": "Other@h"}]}
        assert find_status(payload, "UPS@h") == {"name": "UPS@h"}

    @pytest.mark.unit
    def test_find_by_sanitized_name(self):
        from eneru.status import find_status
        payload = {"ups": [{"name": "UPS@h:3493"}]}
        assert find_status(payload, "UPS-h-3493") == {"name": "UPS@h:3493"}

    @pytest.mark.unit
    def test_missing_returns_none(self):
        from eneru.status import find_status
        assert find_status({"ups": []}, "missing") is None
