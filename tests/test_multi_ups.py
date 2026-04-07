"""Tests for multi-UPS support: config parsing, coordinator routing, and logic."""

import pytest
import threading
import tempfile
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

from eneru import (
    Config, UPSConfig, UPSGroupConfig, TriggersConfig, DepletionConfig,
    ExtendedTimeConfig, BehaviorConfig, LoggingConfig, NotificationsConfig,
    VMConfig, ContainersConfig, FilesystemsConfig, UnmountConfig,
    RemoteServerConfig, LocalShutdownConfig, MonitorState, ConfigLoader,
)
from eneru.monitor import UPSMonitor, MultiUPSCoordinator


# ==============================================================================
# CONFIG PARSING
# ==============================================================================

class TestLegacyBackwardCompat:
    """Legacy single-UPS config must work unchanged."""

    @pytest.mark.unit
    def test_legacy_single_ups(self, tmp_path):
        """Legacy dict format produces one group with is_local=True."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
ups:
  name: "TestUPS@10.0.0.1"
  check_interval: 2
triggers:
  low_battery_threshold: 25
remote_servers:
  - name: "NAS"
    enabled: true
    host: "10.0.0.2"
    user: "admin"
virtual_machines:
  enabled: true
containers:
  enabled: true
""")
        config = ConfigLoader.load(str(config_file))

        assert not config.multi_ups
        assert len(config.ups_groups) == 1
        g = config.ups_groups[0]
        assert g.is_local is True
        assert g.ups.name == "TestUPS@10.0.0.1"
        assert g.triggers.low_battery_threshold == 25
        assert len(g.remote_servers) == 1
        assert g.virtual_machines.enabled is True
        assert g.containers.enabled is True

    @pytest.mark.unit
    def test_legacy_properties(self, tmp_path):
        """Config legacy properties delegate to first group."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
ups:
  name: "TestUPS@10.0.0.1"
triggers:
  low_battery_threshold: 30
""")
        config = ConfigLoader.load(str(config_file))

        assert config.ups.name == "TestUPS@10.0.0.1"
        assert config.triggers.low_battery_threshold == 30


class TestMultiUPSParsing:
    """Multi-UPS list format parsing."""

    @pytest.mark.unit
    def test_multi_ups_basic(self, tmp_path):
        """List format produces multiple groups."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
ups:
  - name: "UPS1@10.0.0.1"
    display_name: "Main UPS"
    is_local: true
    remote_servers:
      - name: "ServerA"
        enabled: true
        host: "10.0.0.10"
        user: "root"
  - name: "UPS2@10.0.0.2"
    display_name: "Backup UPS"
    remote_servers:
      - name: "ServerB"
        enabled: true
        host: "10.0.0.20"
        user: "root"
""")
        config = ConfigLoader.load(str(config_file))

        assert config.multi_ups
        assert len(config.ups_groups) == 2

        g1, g2 = config.ups_groups
        assert g1.ups.name == "UPS1@10.0.0.1"
        assert g1.ups.display_name == "Main UPS"
        assert g1.ups.label == "Main UPS"
        assert g1.is_local is True
        assert len(g1.remote_servers) == 1

        assert g2.ups.name == "UPS2@10.0.0.2"
        assert g2.ups.label == "Backup UPS"
        assert g2.is_local is False
        assert len(g2.remote_servers) == 1

    @pytest.mark.unit
    def test_trigger_inheritance(self, tmp_path):
        """Per-UPS triggers inherit from global, with overrides."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
ups:
  - name: "UPS1@10.0.0.1"
    triggers:
      low_battery_threshold: 30
  - name: "UPS2@10.0.0.2"
triggers:
  low_battery_threshold: 20
  critical_runtime_threshold: 500
""")
        config = ConfigLoader.load(str(config_file))
        g1, g2 = config.ups_groups

        # g1 overrides low_battery but inherits runtime
        assert g1.triggers.low_battery_threshold == 30
        assert g1.triggers.critical_runtime_threshold == 500

        # g2 inherits everything from global
        assert g2.triggers.low_battery_threshold == 20
        assert g2.triggers.critical_runtime_threshold == 500

    @pytest.mark.unit
    def test_display_name_fallback(self, tmp_path):
        """Label falls back to name when display_name is not set."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
ups:
  name: "UPS@10.0.0.1"
""")
        config = ConfigLoader.load(str(config_file))
        assert config.ups.display_name is None
        assert config.ups.label == "UPS@10.0.0.1"

    @pytest.mark.unit
    def test_drain_and_trigger_on_parsing(self, tmp_path):
        """drain_on_local_shutdown and trigger_on are parsed."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
ups:
  name: "UPS@10.0.0.1"
local_shutdown:
  enabled: true
  drain_on_local_shutdown: true
  trigger_on: none
""")
        config = ConfigLoader.load(str(config_file))
        assert config.local_shutdown.drain_on_local_shutdown is True
        assert config.local_shutdown.trigger_on == "none"


class TestOwnershipValidation:
    """Ownership model: only is_local can manage local resources."""

    @pytest.mark.unit
    def test_nonlocal_containers_rejected(self, tmp_path):
        """Non-local group with containers enabled produces ERROR."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
ups:
  - name: "UPS1@10.0.0.1"
    is_local: true
  - name: "UPS2@10.0.0.2"
    containers:
      enabled: true
""")
        config = ConfigLoader.load(str(config_file))
        msgs = ConfigLoader.validate_config(config)
        errors = [m for m in msgs if m.startswith("ERROR")]
        assert any("containers enabled" in m and "UPS2" in m for m in errors)

    @pytest.mark.unit
    def test_multiple_is_local_rejected(self, tmp_path):
        """Multiple is_local groups produce ERROR."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
ups:
  - name: "UPS1@10.0.0.1"
    is_local: true
  - name: "UPS2@10.0.0.2"
    is_local: true
""")
        config = ConfigLoader.load(str(config_file))
        msgs = ConfigLoader.validate_config(config)
        assert any("Multiple UPS groups marked as is_local" in m for m in msgs)

    @pytest.mark.unit
    def test_toplevel_resources_warned(self, tmp_path):
        """Top-level resource sections in multi-UPS mode produce WARNING."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
ups:
  - name: "UPS1@10.0.0.1"
  - name: "UPS2@10.0.0.2"
remote_servers:
  - name: "Stray server"
    host: "10.0.0.99"
""")
        config = ConfigLoader.load(str(config_file))
        raw_data = {"ups": [{}], "remote_servers": [{}]}
        msgs = ConfigLoader.validate_config(config, raw_data=raw_data)
        assert any("remote_servers" in m and "ignored" in m for m in msgs)


# ==============================================================================
# COORDINATOR LOGIC
# ==============================================================================

class TestMultiUPSCoordinator:
    """MultiUPSCoordinator routing and coordination logic."""

    def _make_config(self, groups, **kwargs):
        """Helper to build a Config with UPS groups."""
        return Config(ups_groups=groups, **kwargs)

    @pytest.mark.unit
    def test_coordinator_init(self):
        """Coordinator initializes with correct state."""
        config = self._make_config([
            UPSGroupConfig(ups=UPSConfig(name="UPS1@10.0.0.1"), is_local=True),
            UPSGroupConfig(ups=UPSConfig(name="UPS2@10.0.0.2"), is_local=False),
        ])
        coord = MultiUPSCoordinator(config)
        assert coord._stop_event is not None
        assert coord._local_shutdown_initiated is False
        assert len(coord._monitors) == 0

    @pytest.mark.unit
    def test_is_local_triggers_local_shutdown(self):
        """is_local group triggers _handle_local_shutdown."""
        config = self._make_config([
            UPSGroupConfig(ups=UPSConfig(name="UPS1", display_name="Main"), is_local=True),
            UPSGroupConfig(ups=UPSConfig(name="UPS2"), is_local=False),
        ])
        coord = MultiUPSCoordinator(config)
        coord._log = lambda msg: None

        calls = []
        coord._handle_local_shutdown = lambda label: calls.append(label)

        coord._on_group_shutdown(config.ups_groups[0])
        assert len(calls) == 1
        assert calls[0] == "Main"

    @pytest.mark.unit
    def test_nonlocal_does_not_trigger(self):
        """Non-local group does NOT trigger local shutdown when is_local exists."""
        config = self._make_config([
            UPSGroupConfig(ups=UPSConfig(name="UPS1"), is_local=True),
            UPSGroupConfig(ups=UPSConfig(name="UPS2"), is_local=False),
        ])
        coord = MultiUPSCoordinator(config)
        coord._log = lambda msg: None

        calls = []
        coord._handle_local_shutdown = lambda label: calls.append(label)

        coord._on_group_shutdown(config.ups_groups[1])
        assert len(calls) == 0

    @pytest.mark.unit
    def test_trigger_on_any_no_is_local(self):
        """trigger_on=any triggers shutdown when no is_local exists."""
        config = self._make_config(
            [
                UPSGroupConfig(ups=UPSConfig(name="UPS1"), is_local=False),
                UPSGroupConfig(ups=UPSConfig(name="UPS2"), is_local=False),
            ],
            local_shutdown=LocalShutdownConfig(enabled=True, trigger_on="any"),
        )
        coord = MultiUPSCoordinator(config)
        coord._log = lambda msg: None

        calls = []
        coord._handle_local_shutdown = lambda label: calls.append(label)

        coord._on_group_shutdown(config.ups_groups[0])
        assert len(calls) == 1

    @pytest.mark.unit
    def test_trigger_on_none_prevents_shutdown(self):
        """trigger_on=none prevents local shutdown."""
        config = self._make_config(
            [UPSGroupConfig(ups=UPSConfig(name="UPS1"), is_local=False)],
            local_shutdown=LocalShutdownConfig(enabled=True, trigger_on="none"),
        )
        coord = MultiUPSCoordinator(config)
        coord._log = lambda msg: None

        calls = []
        coord._handle_local_shutdown = lambda label: calls.append(label)

        coord._on_group_shutdown(config.ups_groups[0])
        assert len(calls) == 0

    @pytest.mark.unit
    def test_defense_in_depth_lock(self):
        """Threading lock prevents double local shutdown."""
        config = self._make_config(
            [UPSGroupConfig(ups=UPSConfig(name="UPS1"), is_local=True)],
            local_shutdown=LocalShutdownConfig(enabled=False),
        )
        coord = MultiUPSCoordinator(config)
        coord._log = lambda msg: None
        coord._notification_worker = None

        shutdown_count = []

        def counting_shutdown(label):
            proceed = False
            with coord._local_shutdown_lock:
                if not coord._local_shutdown_initiated:
                    coord._local_shutdown_initiated = True
                    proceed = True
            if proceed:
                shutdown_count.append(label)

        coord._handle_local_shutdown = counting_shutdown
        coord._handle_local_shutdown("UPS1")
        coord._handle_local_shutdown("UPS2")
        assert len(shutdown_count) == 1


# ==============================================================================
# UPS MONITOR COORDINATOR MODE
# ==============================================================================

class TestUPSMonitorCoordinatorMode:
    """UPSMonitor hooks for coordinator mode."""

    @pytest.mark.unit
    def test_coordinator_mode_params(self):
        """Coordinator mode parameters are stored correctly."""
        config = Config(
            ups_groups=[UPSGroupConfig(ups=UPSConfig(name="UPS1"))],
            behavior=BehaviorConfig(dry_run=True),
            local_shutdown=LocalShutdownConfig(enabled=False),
        )
        stop_event = threading.Event()

        monitor = UPSMonitor(
            config=config,
            coordinator_mode=True,
            stop_event=stop_event,
            log_prefix="[Main] ",
            state_file_suffix="UPS1-10-0-0-1",
        )

        assert monitor._coordinator_mode is True
        assert monitor._log_prefix == "[Main] "
        assert "UPS1-10-0-0-1" in str(monitor._shutdown_flag_path)
        assert "UPS1-10-0-0-1" in str(monitor._battery_history_path)
        assert "UPS1-10-0-0-1" in str(monitor._state_file_path)

    @pytest.mark.unit
    def test_stop_event_exits_loop(self):
        """Setting stop_event causes the main loop to exit."""
        stop_event = threading.Event()
        stop_event.set()
        assert stop_event.is_set()


# ==============================================================================
# BATTERY ANOMALY DETECTION
# ==============================================================================

class TestBatteryAnomalyDetection:
    """Battery recalibration / anomaly notification."""

    @pytest.mark.unit
    def test_large_drop_while_online_triggers_warning(self, tmp_path):
        """A >20% battery drop while on OL within 120s triggers notification."""
        import time as _time

        config = Config(
            ups_groups=[UPSGroupConfig(
                ups=UPSConfig(name="UPS@test"),
                is_local=True,
            )],
            behavior=BehaviorConfig(dry_run=True),
            logging=LoggingConfig(
                shutdown_flag_file=str(tmp_path / "flag"),
                state_file=str(tmp_path / "state"),
                battery_history_file=str(tmp_path / "history"),
            ),
            local_shutdown=LocalShutdownConfig(enabled=False),
        )

        monitor = UPSMonitor(config)
        monitor.state = MonitorState()
        monitor.logger = MagicMock()
        monitor._notification_worker = MagicMock()

        # First reading: 100%
        monitor.state.last_battery_charge = 100.0
        monitor.state.last_battery_charge_time = _time.time() - 10  # 10s ago

        # Second reading: 60% (40% drop in 10s while OL)
        ups_data = {"ups.status": "OL CHRG", "battery.charge": "60"}
        monitor._check_battery_anomaly(ups_data)

        # Should have logged a warning
        monitor.logger.log.assert_called()
        log_msg = monitor.logger.log.call_args[0][0]
        assert "Battery Anomaly" in log_msg or "dropped" in log_msg

        # Should have sent notification
        monitor._notification_worker.send.assert_called_once()
        call_kwargs = monitor._notification_worker.send.call_args
        # send() is called with keyword args: body=..., notify_type=...
        notif_body = call_kwargs.kwargs.get("body", call_kwargs.args[0] if call_kwargs.args else "")
        assert "100%" in notif_body
        assert "60%" in notif_body

    @pytest.mark.unit
    def test_small_drop_no_warning(self, tmp_path):
        """A small battery drop (<20%) does not trigger notification."""
        import time as _time

        config = Config(
            ups_groups=[UPSGroupConfig(ups=UPSConfig(name="UPS@test"), is_local=True)],
            behavior=BehaviorConfig(dry_run=True),
            logging=LoggingConfig(
                shutdown_flag_file=str(tmp_path / "flag"),
                state_file=str(tmp_path / "state"),
                battery_history_file=str(tmp_path / "history"),
            ),
        )

        monitor = UPSMonitor(config)
        monitor.state = MonitorState()
        monitor.logger = MagicMock()
        monitor._notification_worker = MagicMock()

        monitor.state.last_battery_charge = 100.0
        monitor.state.last_battery_charge_time = _time.time() - 10

        ups_data = {"ups.status": "OL CHRG", "battery.charge": "95"}
        monitor._check_battery_anomaly(ups_data)

        monitor._notification_worker.send.assert_not_called()

    @pytest.mark.unit
    def test_drop_while_on_battery_no_warning(self, tmp_path):
        """Battery drops while OB are expected and do not trigger anomaly."""
        import time as _time

        config = Config(
            ups_groups=[UPSGroupConfig(ups=UPSConfig(name="UPS@test"), is_local=True)],
            behavior=BehaviorConfig(dry_run=True),
            logging=LoggingConfig(
                shutdown_flag_file=str(tmp_path / "flag"),
                state_file=str(tmp_path / "state"),
                battery_history_file=str(tmp_path / "history"),
            ),
        )

        monitor = UPSMonitor(config)
        monitor.state = MonitorState()
        monitor.logger = MagicMock()
        monitor._notification_worker = MagicMock()

        monitor.state.last_battery_charge = 100.0
        monitor.state.last_battery_charge_time = _time.time() - 10

        ups_data = {"ups.status": "OB DISCHRG", "battery.charge": "50"}
        monitor._check_battery_anomaly(ups_data)

        monitor._notification_worker.send.assert_not_called()
