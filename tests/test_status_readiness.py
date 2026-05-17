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

from unittest.mock import MagicMock, patch

import pytest

from eneru import (
    Config, UPSConfig, UPSGroupConfig, TriggersConfig,
    VMConfig, ContainersConfig, FilesystemsConfig, UnmountConfig,
    RemoteServerConfig, LocalShutdownConfig, MonitorState,
)
from eneru.status import readiness, _required_capabilities


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
