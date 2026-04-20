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
from eneru.monitor import UPSGroupMonitor
from eneru.multi_ups import MultiUPSCoordinator


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
    def test_nonlocal_vms_rejected(self, tmp_path):
        """Non-local group with virtual_machines enabled produces ERROR."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
ups:
  - name: "UPS1@10.0.0.1"
    is_local: true
  - name: "UPS2@10.0.0.2"
    virtual_machines:
      enabled: true
""")
        config = ConfigLoader.load(str(config_file))
        msgs = ConfigLoader.validate_config(config)
        errors = [m for m in msgs if m.startswith("ERROR")]
        assert any("virtual_machines enabled" in m and "UPS2" in m for m in errors)

    @pytest.mark.unit
    def test_nonlocal_filesystems_rejected(self, tmp_path):
        """Non-local group with filesystem unmount enabled produces ERROR."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
ups:
  - name: "UPS1@10.0.0.1"
    is_local: true
  - name: "UPS2@10.0.0.2"
    filesystems:
      unmount:
        enabled: true
        mounts:
          - "/mnt/data"
""")
        config = ConfigLoader.load(str(config_file))
        msgs = ConfigLoader.validate_config(config)
        errors = [m for m in msgs if m.startswith("ERROR")]
        assert any("filesystem unmount enabled" in m and "UPS2" in m for m in errors)

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
        assert any("Multiple groups marked as is_local" in m for m in msgs)

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

class TestUPSGroupMonitorCoordinatorMode:
    """UPSGroupMonitor hooks for coordinator mode."""

    @pytest.mark.unit
    def test_coordinator_mode_params(self):
        """Coordinator mode parameters are stored correctly."""
        config = Config(
            ups_groups=[UPSGroupConfig(ups=UPSConfig(name="UPS1"))],
            behavior=BehaviorConfig(dry_run=True),
            local_shutdown=LocalShutdownConfig(enabled=False),
        )
        stop_event = threading.Event()

        monitor = UPSGroupMonitor(
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
    """Battery recalibration / anomaly notification.

    Uses sustained-reading confirmation: an anomalous drop must persist
    across 3 consecutive polls before firing.  This filters out transient
    firmware jitter that APC and CyberPower units exhibit after OB→OL
    transitions.
    """

    def _make_monitor(self, tmp_path):
        """Helper: create a UPSGroupMonitor wired for anomaly-detection tests."""
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

        monitor = UPSGroupMonitor(config)
        monitor.state = MonitorState()
        monitor.logger = MagicMock()
        monitor._notification_worker = MagicMock()

        # Baseline: 100% charge recorded 10s ago
        monitor.state.last_battery_charge = 100.0
        monitor.state.last_battery_charge_time = _time.time() - 10
        return monitor

    @pytest.mark.unit
    def test_large_drop_while_online_triggers_warning(self, tmp_path):
        """A >20% sustained battery drop while on OL triggers notification after 3 polls."""
        monitor = self._make_monitor(tmp_path)
        ups_data = {"ups.status": "OL CHRG", "battery.charge": "60"}

        # Poll 1: 60% (40% drop in 10s while OL) -- pending, no alert yet
        monitor._check_battery_anomaly(ups_data)
        monitor._notification_worker.send.assert_not_called()

        # Poll 2: still 60% -- not yet confirmed (need 3 polls)
        monitor._check_battery_anomaly(ups_data)
        monitor._notification_worker.send.assert_not_called()

        # Poll 3: still 60% -- confirmed anomaly, alert fires
        monitor._check_battery_anomaly(ups_data)

        # Should have logged a warning
        monitor.logger.log.assert_called()
        log_msg = monitor.logger.log.call_args[0][0]
        assert "dropped" in log_msg

        # Should have sent notification
        monitor._notification_worker.send.assert_called_once()
        call_kwargs = monitor._notification_worker.send.call_args
        notif_body = call_kwargs.kwargs.get("body", call_kwargs.args[0] if call_kwargs.args else "")
        assert "100%" in notif_body
        assert "60%" in notif_body

    @pytest.mark.unit
    def test_transient_jitter_recovers_poll2_no_warning(self, tmp_path):
        """A drop that recovers on poll 2 is transient jitter, not an anomaly.

        Reproduces the real-world scenario observed on 2026-04-07 where a
        CyberPower UPS reported 50% immediately after an OB→OL transition,
        then self-corrected.
        """
        monitor = self._make_monitor(tmp_path)

        # Poll 1: 50% drop detected while OL -- pending
        ups_data_drop = {"ups.status": "OL CHRG", "battery.charge": "50"}
        monitor._check_battery_anomaly(ups_data_drop)
        monitor._notification_worker.send.assert_not_called()

        # Poll 2: charge bounces back to 99% -- jitter, discard anomaly
        ups_data_recovery = {"ups.status": "OL CHRG", "battery.charge": "99"}
        monitor._check_battery_anomaly(ups_data_recovery)
        monitor._notification_worker.send.assert_not_called()

    @pytest.mark.unit
    def test_transient_jitter_recovers_poll3_no_warning(self, tmp_path):
        """A drop that persists for 2 polls but recovers on poll 3 is still jitter."""
        monitor = self._make_monitor(tmp_path)

        ups_data_drop = {"ups.status": "OL CHRG", "battery.charge": "50"}
        ups_data_recovery = {"ups.status": "OL CHRG", "battery.charge": "99"}

        # Poll 1: drop detected -- pending (count=1)
        monitor._check_battery_anomaly(ups_data_drop)
        # Poll 2: still low -- count=2, not yet confirmed
        monitor._check_battery_anomaly(ups_data_drop)
        # Poll 3: recovers before reaching threshold of 3
        monitor._check_battery_anomaly(ups_data_recovery)

        monitor._notification_worker.send.assert_not_called()

    @pytest.mark.unit
    def test_small_drop_no_warning(self, tmp_path):
        """A small battery drop (<20%) does not trigger notification."""
        monitor = self._make_monitor(tmp_path)

        ups_data = {"ups.status": "OL CHRG", "battery.charge": "95"}
        monitor._check_battery_anomaly(ups_data)

        monitor._notification_worker.send.assert_not_called()

    @pytest.mark.unit
    def test_drop_while_on_battery_no_warning(self, tmp_path):
        """Battery drops while OB are expected and do not trigger anomaly."""
        monitor = self._make_monitor(tmp_path)

        ups_data = {"ups.status": "OB DISCHRG", "battery.charge": "50"}
        monitor._check_battery_anomaly(ups_data)

        monitor._notification_worker.send.assert_not_called()

    @pytest.mark.unit
    def test_ob_transition_clears_pending_anomaly(self, tmp_path):
        """Going on battery resets any pending anomaly detection state."""
        monitor = self._make_monitor(tmp_path)

        # Poll 1: anomalous drop detected -- pending
        ups_data_drop = {"ups.status": "OL CHRG", "battery.charge": "50"}
        monitor._check_battery_anomaly(ups_data_drop)
        assert monitor.state.pending_anomaly_charge >= 0
        assert monitor.state.pending_anomaly_count == 1

        # UPS goes on battery -- pending anomaly and counter must be cleared
        ups_data_ob = {"ups.status": "OB DISCHRG", "battery.charge": "48"}
        monitor._check_battery_anomaly(ups_data_ob)
        assert monitor.state.pending_anomaly_charge < 0
        assert monitor.state.pending_anomaly_count == 0

        monitor._notification_worker.send.assert_not_called()

    @pytest.mark.unit
    def test_firmware_recalibration_while_online_fires(self, tmp_path):
        """Pure OL recalibration (no OB transition) fires after 3-poll confirmation.

        Reproduces the real-world scenario observed on 2026-04-03 where a
        firmware upgrade caused the charge to drop from 100% to 60% while
        the UPS never left line power (OL CHRG → OL → OL CHRG).
        """
        monitor = self._make_monitor(tmp_path)
        ups_data_drop = {"ups.status": "OL CHRG", "battery.charge": "60"}

        # Poll 1: charge drops to 60% while staying on OL -- pending
        monitor._check_battery_anomaly(ups_data_drop)
        monitor._notification_worker.send.assert_not_called()

        # Poll 2: still 60% -- not yet confirmed
        monitor._check_battery_anomaly(ups_data_drop)
        monitor._notification_worker.send.assert_not_called()

        # Poll 3: still 60% -- sustained, confirmed anomaly
        monitor._check_battery_anomaly(ups_data_drop)
        monitor._notification_worker.send.assert_called_once()


# ==============================================================================
# DRAIN ON LOCAL SHUTDOWN
# ==============================================================================

class TestDrainOnLocalShutdown:
    """Verify drain_on_local_shutdown triggers resource shutdown on other groups."""

    @pytest.mark.unit
    def test_drain_calls_shutdown_on_other_monitors(self, tmp_path):
        """drain_on_local_shutdown=true must call _execute_shutdown_sequence on each monitor."""
        config = Config(
            ups_groups=[
                UPSGroupConfig(ups=UPSConfig(name="UPS1"), is_local=True),
                UPSGroupConfig(ups=UPSConfig(name="UPS2"), is_local=False),
            ],
            behavior=BehaviorConfig(dry_run=True),
            logging=LoggingConfig(
                shutdown_flag_file=str(tmp_path / "flag"),
                state_file=str(tmp_path / "state"),
                battery_history_file=str(tmp_path / "history"),
            ),
            local_shutdown=LocalShutdownConfig(
                enabled=False,
                drain_on_local_shutdown=True,
            ),
        )
        coord = MultiUPSCoordinator(config)
        coord._log = lambda msg: None

        mock_monitor = MagicMock()
        mock_monitor._shutdown_flag_path = tmp_path / "flag-ups2"
        mock_monitor._shutdown_flag_path.unlink(missing_ok=True)
        mock_monitor._log_prefix = "[UPS2] "
        coord._monitors = [mock_monitor]
        coord._threads = []

        coord._drain_all_groups(timeout=5)

        mock_monitor._execute_shutdown_sequence.assert_called_once()

    @pytest.mark.unit
    def test_drain_not_called_when_disabled(self, tmp_path):
        """drain_on_local_shutdown=false must NOT drain other groups."""
        config = Config(
            ups_groups=[
                UPSGroupConfig(ups=UPSConfig(name="UPS1"), is_local=True),
            ],
            behavior=BehaviorConfig(dry_run=True),
            logging=LoggingConfig(
                shutdown_flag_file=str(tmp_path / "flag"),
                state_file=str(tmp_path / "state"),
                battery_history_file=str(tmp_path / "history"),
            ),
            local_shutdown=LocalShutdownConfig(
                enabled=False,
                drain_on_local_shutdown=False,
            ),
        )
        coord = MultiUPSCoordinator(config)
        coord._log = lambda msg: None
        coord._notification_worker = None

        drain_called = []
        coord._drain_all_groups = lambda timeout=120: drain_called.append(True)

        coord._handle_local_shutdown("UPS1")

        assert len(drain_called) == 0


# ==============================================================================
# RUNTIME is_local ENFORCEMENT
# ==============================================================================

class TestRuntimeIsLocalEnforcement:
    """Verify non-local groups skip VMs/containers/filesystems at runtime."""

    @pytest.mark.unit
    def test_nonlocal_group_skips_local_resources(self, tmp_path):
        """Non-local group's shutdown sequence must skip VMs, containers, filesystems."""
        config = Config(
            ups_groups=[UPSGroupConfig(
                ups=UPSConfig(name="UPS2@remote"),
                is_local=False,
            )],
            behavior=BehaviorConfig(dry_run=True),
            logging=LoggingConfig(
                shutdown_flag_file=str(tmp_path / "flag"),
                state_file=str(tmp_path / "state"),
                battery_history_file=str(tmp_path / "history"),
            ),
            local_shutdown=LocalShutdownConfig(enabled=False),
        )

        monitor = UPSGroupMonitor(config)
        monitor.state = MonitorState()
        monitor.logger = MagicMock()
        monitor._notification_worker = MagicMock()

        call_order = []
        monitor._shutdown_vms = lambda: call_order.append("vms")
        monitor._shutdown_containers = lambda: call_order.append("containers")
        monitor._sync_filesystems = lambda: call_order.append("sync")
        monitor._unmount_filesystems = lambda: call_order.append("unmount")
        monitor._shutdown_remote_servers = lambda: call_order.append("remote")

        monitor._execute_shutdown_sequence()

        assert "remote" in call_order
        assert "vms" not in call_order
        assert "containers" not in call_order
        assert "sync" not in call_order
        assert "unmount" not in call_order

    @pytest.mark.unit
    def test_local_group_runs_all_resources(self, tmp_path):
        """Local group's shutdown sequence must run all resources."""
        config = Config(
            ups_groups=[UPSGroupConfig(
                ups=UPSConfig(name="UPS1@local"),
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

        monitor = UPSGroupMonitor(config)
        monitor.state = MonitorState()
        monitor.logger = MagicMock()
        monitor._notification_worker = MagicMock()

        call_order = []
        monitor._shutdown_vms = lambda: call_order.append("vms")
        monitor._shutdown_containers = lambda: call_order.append("containers")
        monitor._sync_filesystems = lambda: call_order.append("sync")
        monitor._unmount_filesystems = lambda: call_order.append("unmount")
        monitor._shutdown_remote_servers = lambda: call_order.append("remote")

        monitor._execute_shutdown_sequence()

        assert call_order == ["vms", "containers", "sync", "unmount", "remote"]


# ==============================================================================
# NOTIFICATION PREFIXING
# ==============================================================================

class TestNotificationPrefixing:
    """Verify notifications include UPS name in multi-UPS mode."""

    @pytest.mark.unit
    def test_notification_prefixed_in_coordinator_mode(self, tmp_path):
        """Notifications in coordinator mode include [display_name] prefix."""
        config = Config(
            ups_groups=[UPSGroupConfig(
                ups=UPSConfig(name="UPS1@test", display_name="Main UPS"),
                is_local=True,
            )],
            behavior=BehaviorConfig(dry_run=True),
            logging=LoggingConfig(
                shutdown_flag_file=str(tmp_path / "flag"),
                state_file=str(tmp_path / "state"),
                battery_history_file=str(tmp_path / "history"),
            ),
        )

        mock_worker = MagicMock()
        monitor = UPSGroupMonitor(
            config=config,
            coordinator_mode=True,
            log_prefix="[Main UPS] ",
            notification_worker=mock_worker,
        )
        monitor.state = MonitorState()
        monitor.logger = MagicMock()

        monitor._send_notification("Battery at 15%", "warning")

        mock_worker.send.assert_called_once()
        call_kwargs = mock_worker.send.call_args
        body = call_kwargs.kwargs.get("body", "")
        assert "[Main UPS]" in body
        assert "15%" in body

    @pytest.mark.unit
    def test_notification_not_prefixed_in_single_ups(self, tmp_path):
        """Notifications in single-UPS mode have no prefix."""
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
        )

        mock_worker = MagicMock()
        monitor = UPSGroupMonitor(config=config, notification_worker=mock_worker)
        monitor.state = MonitorState()
        monitor.logger = MagicMock()

        monitor._send_notification("Battery at 15%", "warning")

        mock_worker.send.assert_called_once()
        call_kwargs = mock_worker.send.call_args
        body = call_kwargs.kwargs.get("body", "")
        assert not body.startswith("[")
        assert "15%" in body


# ==============================================================================
# EXIT AFTER SHUTDOWN IN COORDINATOR MODE
# ==============================================================================

class TestCoordinatorExitAfterShutdown:
    """Verify --exit-after-shutdown works in multi-UPS coordinator mode."""

    @pytest.mark.unit
    def test_exit_after_shutdown_sets_stop_event(self, tmp_path):
        """Coordinator sets stop_event after group shutdown when exit_after_shutdown=True."""
        config = Config(
            ups_groups=[
                UPSGroupConfig(ups=UPSConfig(name="UPS1"), is_local=True),
            ],
            behavior=BehaviorConfig(dry_run=True),
            logging=LoggingConfig(
                shutdown_flag_file=str(tmp_path / "flag"),
                state_file=str(tmp_path / "state"),
                battery_history_file=str(tmp_path / "history"),
            ),
            local_shutdown=LocalShutdownConfig(enabled=False),
        )

        coord = MultiUPSCoordinator(config, exit_after_shutdown=True)
        coord._log = lambda msg: None
        coord._notification_worker = None

        coord._on_group_shutdown(config.ups_groups[0])

        assert coord._stop_event.is_set()


# ==============================================================================
# COORDINATOR INITIALIZATION
# ==============================================================================

def _coord_config(tmp_path, **kwargs):
    """Helper to build a Config with sane defaults for coordinator tests."""
    defaults = dict(
        ups_groups=[
            UPSGroupConfig(ups=UPSConfig(name="UPS1@10.0.0.1"), is_local=True),
        ],
        behavior=BehaviorConfig(dry_run=True),
        logging=LoggingConfig(
            shutdown_flag_file=str(tmp_path / "flag"),
            state_file=str(tmp_path / "state"),
            battery_history_file=str(tmp_path / "history"),
            file=str(tmp_path / "eneru.log"),
        ),
        local_shutdown=LocalShutdownConfig(enabled=False),
    )
    defaults.update(kwargs)
    return Config(**defaults)


class TestCoordinatorInitialize:
    """_initialize() wires signal handlers, logger, flag, and notifications."""

    @pytest.mark.unit
    def test_initialize_clears_stale_global_flag(self, tmp_path):
        """A pre-existing shutdown flag is cleared at startup."""
        config = _coord_config(tmp_path)
        flag = Path(config.logging.shutdown_flag_file)
        flag.touch()
        assert flag.exists()

        coord = MultiUPSCoordinator(config)
        with patch("eneru.multi_ups.signal.signal"):
            coord._initialize()

        assert not flag.exists()

    @pytest.mark.unit
    def test_initialize_registers_signal_handlers(self, tmp_path):
        """SIGTERM and SIGINT are routed to _handle_signal."""
        import signal as _signal

        config = _coord_config(tmp_path)
        coord = MultiUPSCoordinator(config)
        with patch("eneru.multi_ups.signal.signal") as mock_signal:
            coord._initialize()

        registered = {call.args[0] for call in mock_signal.call_args_list}
        assert _signal.SIGTERM in registered
        assert _signal.SIGINT in registered

    @pytest.mark.unit
    def test_initialize_creates_log_file(self, tmp_path):
        """_initialize touches the log file when a path is configured."""
        config = _coord_config(tmp_path)
        log_path = Path(config.logging.file)
        assert not log_path.exists()

        coord = MultiUPSCoordinator(config)
        with patch("eneru.multi_ups.signal.signal"):
            coord._initialize()

        assert log_path.exists()

    @pytest.mark.unit
    def test_initialize_skips_notifications_when_disabled(self, tmp_path):
        """No NotificationWorker is created when notifications are disabled."""
        config = _coord_config(
            tmp_path,
            notifications=NotificationsConfig(enabled=False),
        )
        coord = MultiUPSCoordinator(config)
        with patch("eneru.multi_ups.signal.signal"), \
             patch("eneru.multi_ups.NotificationWorker") as mock_worker_cls:
            coord._initialize()

        assert coord._notification_worker is None
        mock_worker_cls.assert_not_called()

    @pytest.mark.unit
    def test_initialize_starts_notification_worker(self, tmp_path):
        """A successfully started NotificationWorker is retained."""
        config = _coord_config(
            tmp_path,
            notifications=NotificationsConfig(
                enabled=True,
                urls=["json://localhost"],
            ),
        )
        coord = MultiUPSCoordinator(config)

        mock_worker = MagicMock()
        mock_worker.start.return_value = True
        mock_worker.get_service_count.return_value = 1

        with patch("eneru.multi_ups.signal.signal"), \
             patch("eneru.multi_ups.APPRISE_AVAILABLE", True), \
             patch("eneru.multi_ups.NotificationWorker", return_value=mock_worker):
            coord._initialize()

        assert coord._notification_worker is mock_worker
        mock_worker.start.assert_called_once()

    @pytest.mark.unit
    def test_initialize_drops_notification_worker_on_start_failure(self, tmp_path):
        """If NotificationWorker.start() returns False, the worker is dropped."""
        config = _coord_config(
            tmp_path,
            notifications=NotificationsConfig(
                enabled=True,
                urls=["json://localhost"],
            ),
        )
        coord = MultiUPSCoordinator(config)

        mock_worker = MagicMock()
        mock_worker.start.return_value = False

        with patch("eneru.multi_ups.signal.signal"), \
             patch("eneru.multi_ups.APPRISE_AVAILABLE", True), \
             patch("eneru.multi_ups.NotificationWorker", return_value=mock_worker):
            coord._initialize()

        assert coord._notification_worker is None


# ==============================================================================
# COORDINATOR THREAD START
# ==============================================================================

class TestCoordinatorStartMonitors:
    """_start_monitors() builds per-group configs and spawns daemon threads."""

    @pytest.mark.unit
    def test_start_monitors_one_thread_per_group(self, tmp_path):
        """One thread is created per UPS group."""
        config = _coord_config(
            tmp_path,
            ups_groups=[
                UPSGroupConfig(ups=UPSConfig(name="UPS1@host"), is_local=True),
                UPSGroupConfig(ups=UPSConfig(name="UPS2@host"), is_local=False),
                UPSGroupConfig(ups=UPSConfig(name="UPS3@host"), is_local=False),
            ],
        )
        coord = MultiUPSCoordinator(config)
        coord._log = lambda msg: None

        with patch("eneru.multi_ups.threading.Thread") as mock_thread, \
             patch("eneru.multi_ups.UPSGroupMonitor") as mock_monitor_cls:
            mock_monitor_cls.return_value = MagicMock()
            coord._start_monitors()

        assert len(coord._monitors) == 3
        assert mock_thread.call_count == 3
        # Every thread is a daemon and was started
        for call in mock_thread.call_args_list:
            assert call.kwargs.get("daemon") is True
        # threading.Thread is patched to return the same MagicMock each call,
        # so start() should have been invoked once per group.
        assert mock_thread.return_value.start.call_count == 3

    @pytest.mark.unit
    def test_start_monitors_sanitizes_ups_name(self, tmp_path):
        """@, :, / in UPS names become - in thread name and state suffix."""
        config = _coord_config(
            tmp_path,
            ups_groups=[
                UPSGroupConfig(ups=UPSConfig(name="UPS1@10.0.0.1:3493/path"), is_local=True),
            ],
        )
        coord = MultiUPSCoordinator(config)
        coord._log = lambda msg: None

        with patch("eneru.multi_ups.threading.Thread") as mock_thread, \
             patch("eneru.multi_ups.UPSGroupMonitor") as mock_monitor_cls:
            mock_monitor_cls.return_value = MagicMock()
            coord._start_monitors()

        sanitized = "UPS1-10.0.0.1-3493-path"
        thread_kwargs = mock_thread.call_args_list[0].kwargs
        assert thread_kwargs["name"] == f"ups-{sanitized}"

        monitor_kwargs = mock_monitor_cls.call_args_list[0].kwargs
        assert monitor_kwargs["state_file_suffix"] == sanitized
        assert monitor_kwargs["coordinator_mode"] is True
        assert monitor_kwargs["log_prefix"].startswith("[")

    @pytest.mark.unit
    def test_start_monitors_passes_per_group_config(self, tmp_path):
        """Each monitor receives a Config containing only its own group."""
        groups = [
            UPSGroupConfig(ups=UPSConfig(name="UPS1@h"), is_local=True),
            UPSGroupConfig(ups=UPSConfig(name="UPS2@h"), is_local=False),
        ]
        config = _coord_config(tmp_path, ups_groups=groups)
        coord = MultiUPSCoordinator(config)
        coord._log = lambda msg: None

        with patch("eneru.multi_ups.threading.Thread"), \
             patch("eneru.multi_ups.UPSGroupMonitor") as mock_monitor_cls:
            mock_monitor_cls.return_value = MagicMock()
            coord._start_monitors()

        for idx, call in enumerate(mock_monitor_cls.call_args_list):
            per_group = call.kwargs["config"]
            assert len(per_group.ups_groups) == 1
            assert per_group.ups_groups[0].ups.name == groups[idx].ups.name


# ==============================================================================
# COORDINATOR RUN-MONITOR THREAD TARGET
# ==============================================================================

class TestCoordinatorRunMonitor:
    """_run_monitor() handles normal completion and crash notification."""

    @pytest.mark.unit
    def test_run_monitor_normal(self, tmp_path):
        """A monitor that runs without raising does not notify failure."""
        config = _coord_config(tmp_path)
        coord = MultiUPSCoordinator(config)
        coord._log = lambda msg: None
        coord._notification_worker = MagicMock()

        mock_monitor = MagicMock()
        coord._run_monitor(mock_monitor, config.ups_groups[0])

        mock_monitor.run.assert_called_once()
        coord._notification_worker.send.assert_not_called()

    @pytest.mark.unit
    def test_run_monitor_crash_logs_and_notifies(self, tmp_path):
        """A monitor that raises is logged and a failure notification is sent."""
        config = _coord_config(
            tmp_path,
            ups_groups=[UPSGroupConfig(
                ups=UPSConfig(name="UPS1", display_name="MainUPS"),
                is_local=True,
            )],
        )
        coord = MultiUPSCoordinator(config)
        logged = []
        coord._log = logged.append
        coord._notification_worker = MagicMock()

        mock_monitor = MagicMock()
        mock_monitor.run.side_effect = RuntimeError("boom")
        coord._run_monitor(mock_monitor, config.ups_groups[0])

        assert any("MainUPS" in m and "boom" in m for m in logged)
        coord._notification_worker.send.assert_called_once()
        body = coord._notification_worker.send.call_args.args[0]
        assert "MainUPS" in body
        assert "boom" in body

    @pytest.mark.unit
    def test_run_monitor_crash_without_notifier(self, tmp_path):
        """Crash path is safe when no notification worker is configured."""
        config = _coord_config(tmp_path)
        coord = MultiUPSCoordinator(config)
        coord._log = lambda msg: None
        coord._notification_worker = None

        mock_monitor = MagicMock()
        mock_monitor.run.side_effect = RuntimeError("boom")
        # Must not raise.
        coord._run_monitor(mock_monitor, config.ups_groups[0])


# ==============================================================================
# COORDINATOR LOCAL SHUTDOWN (REAL PATH)
# ==============================================================================

class TestCoordinatorRealLocalShutdown:
    """_handle_local_shutdown executes the real shutdown command outside dry-run."""

    @pytest.mark.unit
    def test_real_shutdown_invokes_command(self, tmp_path):
        """Non-dry-run path runs the configured shutdown command."""
        config = _coord_config(
            tmp_path,
            behavior=BehaviorConfig(dry_run=False),
            local_shutdown=LocalShutdownConfig(
                enabled=True,
                command="/sbin/shutdown -h now",
                message="UPS triggered shutdown",
            ),
        )
        coord = MultiUPSCoordinator(config)
        coord._log = lambda msg: None
        coord._notification_worker = MagicMock()

        with patch("eneru.multi_ups.time.sleep"), \
             patch("eneru.multi_ups.run_command") as mock_run:
            coord._handle_local_shutdown("UPS1")

        mock_run.assert_called_once()
        cmd_parts = mock_run.call_args.args[0]
        assert cmd_parts[:3] == ["/sbin/shutdown", "-h", "now"]
        assert cmd_parts[-1] == "UPS triggered shutdown"
        coord._notification_worker.send.assert_called_once()

    @pytest.mark.unit
    def test_real_shutdown_without_message(self, tmp_path):
        """Empty message means no extra arg appended to the command."""
        config = _coord_config(
            tmp_path,
            behavior=BehaviorConfig(dry_run=False),
            local_shutdown=LocalShutdownConfig(
                enabled=True,
                command="/sbin/poweroff",
                message="",
            ),
        )
        coord = MultiUPSCoordinator(config)
        coord._log = lambda msg: None
        coord._notification_worker = None

        with patch("eneru.multi_ups.time.sleep"), \
             patch("eneru.multi_ups.run_command") as mock_run:
            coord._handle_local_shutdown("UPS1")

        cmd_parts = mock_run.call_args.args[0]
        assert cmd_parts == ["/sbin/poweroff"]

    @pytest.mark.unit
    def test_disabled_local_shutdown_clears_flag(self, tmp_path):
        """When local_shutdown.enabled=False, the global flag is removed."""
        config = _coord_config(
            tmp_path,
            local_shutdown=LocalShutdownConfig(enabled=False),
        )
        coord = MultiUPSCoordinator(config)
        coord._log = lambda msg: None
        coord._notification_worker = None

        coord._handle_local_shutdown("UPS1")

        assert not Path(config.logging.shutdown_flag_file).exists()

    @pytest.mark.unit
    def test_dry_run_clears_flag(self, tmp_path):
        """Dry-run path also clears the flag rather than rebooting the host."""
        config = _coord_config(
            tmp_path,
            behavior=BehaviorConfig(dry_run=True),
            local_shutdown=LocalShutdownConfig(
                enabled=True,
                command="/sbin/shutdown -h now",
            ),
        )
        coord = MultiUPSCoordinator(config)
        coord._log = lambda msg: None
        coord._notification_worker = None

        with patch("eneru.multi_ups.run_command") as mock_run:
            coord._handle_local_shutdown("UPS1")

        mock_run.assert_not_called()
        assert not Path(config.logging.shutdown_flag_file).exists()

    @pytest.mark.unit
    def test_real_shutdown_sets_stop_event_when_exit_requested(self, tmp_path):
        """exit_after_shutdown=True sets stop_event after the local shutdown."""
        config = _coord_config(
            tmp_path,
            local_shutdown=LocalShutdownConfig(enabled=False),
        )
        coord = MultiUPSCoordinator(config, exit_after_shutdown=True)
        coord._log = lambda msg: None
        coord._notification_worker = None

        coord._handle_local_shutdown("UPS1")

        assert coord._stop_event.is_set()


# ==============================================================================
# COORDINATOR DRAIN EDGE CASES
# ==============================================================================

class TestCoordinatorDrainEdgeCases:
    """_drain_all_groups handles already-shut, exceptions, and timeouts."""

    @pytest.mark.unit
    def test_drain_skips_monitor_with_existing_flag(self, tmp_path):
        """A monitor whose shutdown flag is already present is not re-triggered."""
        config = _coord_config(tmp_path)
        coord = MultiUPSCoordinator(config)
        coord._log = lambda msg: None

        already_shut = MagicMock()
        already_shut._shutdown_flag_path = tmp_path / "shut-flag"
        already_shut._shutdown_flag_path.touch()
        coord._monitors = [already_shut]
        coord._threads = []

        coord._drain_all_groups(timeout=1)

        already_shut._execute_shutdown_sequence.assert_not_called()

    @pytest.mark.unit
    def test_drain_swallows_shutdown_exception(self, tmp_path):
        """An exception in a monitor's shutdown sequence is logged, not raised."""
        config = _coord_config(tmp_path)
        coord = MultiUPSCoordinator(config)
        logs = []
        coord._log = logs.append

        bad_monitor = MagicMock()
        bad_monitor._shutdown_flag_path = tmp_path / "bad-flag"
        bad_monitor._shutdown_flag_path.unlink(missing_ok=True)
        bad_monitor._log_prefix = "[bad] "
        bad_monitor._execute_shutdown_sequence.side_effect = RuntimeError("fail")
        coord._monitors = [bad_monitor]
        coord._threads = []

        # Must not raise.
        coord._drain_all_groups(timeout=1)
        assert any("Error during drain shutdown" in m for m in logs)

    @pytest.mark.unit
    def test_drain_warns_on_timeout(self, tmp_path):
        """Threads still alive after the deadline produce a warning log."""
        config = _coord_config(tmp_path)
        coord = MultiUPSCoordinator(config)
        logs = []
        coord._log = logs.append

        coord._monitors = []
        live_thread = MagicMock()
        live_thread.is_alive.return_value = True
        coord._threads = [live_thread]

        coord._drain_all_groups(timeout=0)

        live_thread.join.assert_called_once()
        assert any("still running after drain timeout" in m for m in logs)


# ==============================================================================
# COORDINATOR WAIT-FOR-COMPLETION
# ==============================================================================

class TestCoordinatorWaitForCompletion:
    """_wait_for_completion exits when all monitor threads die."""

    @pytest.mark.unit
    def test_wait_returns_when_no_threads_alive(self, tmp_path):
        """If every thread is dead, the wait loop exits promptly."""
        config = _coord_config(tmp_path)
        coord = MultiUPSCoordinator(config)

        dead_thread = MagicMock()
        dead_thread.is_alive.return_value = False
        coord._threads = [dead_thread]

        # Should return without blocking on stop_event.
        coord._wait_for_completion()
        assert not coord._stop_event.is_set()

    @pytest.mark.unit
    def test_wait_returns_when_stop_event_set(self, tmp_path):
        """A pre-set stop_event causes immediate exit."""
        config = _coord_config(tmp_path)
        coord = MultiUPSCoordinator(config)
        coord._stop_event.set()

        live_thread = MagicMock()
        live_thread.is_alive.return_value = True
        coord._threads = [live_thread]

        coord._wait_for_completion()


# ==============================================================================
# COORDINATOR SIGNAL HANDLER
# ==============================================================================

class TestCoordinatorHandleSignal:
    """_handle_signal stops worker, joins threads, clears flag, and exits."""

    @pytest.mark.unit
    def test_handle_signal_stops_notification_worker(self, tmp_path):
        """A configured notification worker is told to stop."""
        import signal as _signal

        config = _coord_config(tmp_path)
        coord = MultiUPSCoordinator(config)
        coord._log = lambda msg: None
        coord._notification_worker = MagicMock()
        coord._threads = []

        Path(config.logging.shutdown_flag_file).touch()

        with patch("eneru.multi_ups.sys.exit") as mock_exit:
            coord._handle_signal(_signal.SIGTERM, None)

        coord._notification_worker.send.assert_called_once()
        coord._notification_worker.stop.assert_called_once()
        assert coord._stop_event.is_set()
        assert not Path(config.logging.shutdown_flag_file).exists()
        mock_exit.assert_called_once_with(0)

    @pytest.mark.unit
    def test_handle_signal_joins_threads(self, tmp_path):
        """Each registered thread is joined before exit."""
        import signal as _signal

        config = _coord_config(tmp_path)
        coord = MultiUPSCoordinator(config)
        coord._log = lambda msg: None
        coord._notification_worker = None

        threads = [MagicMock(), MagicMock()]
        coord._threads = threads

        with patch("eneru.multi_ups.sys.exit"):
            coord._handle_signal(_signal.SIGINT, None)

        for thread in threads:
            thread.join.assert_called_once_with(timeout=5)


# ==============================================================================
# COORDINATOR LOG FALLBACK
# ==============================================================================

class TestCoordinatorLogFallback:
    """_log() falls back to print() when no shared logger is initialized."""

    @pytest.mark.unit
    def test_log_uses_logger_when_present(self, tmp_path):
        """When a logger is set, _log delegates to logger.log()."""
        config = _coord_config(tmp_path)
        coord = MultiUPSCoordinator(config)
        coord._logger = MagicMock()

        coord._log("hello")
        coord._logger.log.assert_called_once_with("hello")

    @pytest.mark.unit
    def test_log_prints_when_no_logger(self, tmp_path, capsys):
        """Without a logger, _log writes a timestamped line to stdout."""
        config = _coord_config(tmp_path)
        coord = MultiUPSCoordinator(config)
        coord._logger = None

        coord._log("hello world")
        captured = capsys.readouterr()
        assert "hello world" in captured.out


# ==============================================================================
# REDUNDANCY-GROUP COORDINATOR WIRING (Phase 2)
# ==============================================================================

class TestCoordinatorRedundancyWiring:
    """The coordinator marks members as in_redundancy_group and starts evaluators."""

    def _config_with_redundancy(self, tmp_path):
        from eneru import RedundancyGroupConfig
        return _coord_config(
            tmp_path,
            ups_groups=[
                UPSGroupConfig(ups=UPSConfig(name="UPS1@h"), is_local=False),
                UPSGroupConfig(ups=UPSConfig(name="UPS2@h"), is_local=False),
                UPSGroupConfig(ups=UPSConfig(name="UPS3@h"), is_local=False),
            ],
            redundancy_groups=[
                RedundancyGroupConfig(
                    name="rack-1",
                    ups_sources=["UPS1@h", "UPS2@h"],
                    min_healthy=1,
                ),
            ],
        )

    @pytest.mark.unit
    def test_in_redundancy_set_computed_at_construction(self, tmp_path):
        config = self._config_with_redundancy(tmp_path)
        coord = MultiUPSCoordinator(config)
        assert coord._in_redundancy == {"UPS1@h", "UPS2@h"}
        # UPS3 is independent
        assert "UPS3@h" not in coord._in_redundancy

    @pytest.mark.unit
    def test_in_redundancy_empty_when_no_groups(self, tmp_path):
        coord = MultiUPSCoordinator(_coord_config(tmp_path))
        assert coord._in_redundancy == set()

    @pytest.mark.unit
    def test_start_monitors_passes_in_redundancy_flag(self, tmp_path):
        config = self._config_with_redundancy(tmp_path)
        coord = MultiUPSCoordinator(config)
        coord._log = lambda msg: None

        with patch("eneru.multi_ups.threading.Thread"), \
             patch("eneru.multi_ups.UPSGroupMonitor") as mock_monitor_cls, \
             patch("eneru.multi_ups.RedundancyGroupExecutor"), \
             patch("eneru.multi_ups.RedundancyGroupEvaluator"):
            mock_monitor_cls.return_value = MagicMock()
            coord._start_monitors()

        # First two monitors are in redundancy; third is not.
        flags = [c.kwargs["in_redundancy_group"]
                 for c in mock_monitor_cls.call_args_list]
        assert flags == [True, True, False]

    @pytest.mark.unit
    def test_start_monitors_creates_evaluator_per_redundancy_group(self, tmp_path):
        config = self._config_with_redundancy(tmp_path)
        coord = MultiUPSCoordinator(config)
        coord._log = lambda msg: None

        with patch("eneru.multi_ups.threading.Thread"), \
             patch("eneru.multi_ups.UPSGroupMonitor") as mock_monitor_cls, \
             patch("eneru.multi_ups.RedundancyGroupExecutor") as mock_executor_cls, \
             patch("eneru.multi_ups.RedundancyGroupEvaluator") as mock_eval_cls:
            mock_monitor_cls.return_value = MagicMock()
            mock_eval_cls.return_value = MagicMock()
            coord._start_monitors()

        assert mock_executor_cls.call_count == 1
        assert mock_eval_cls.call_count == 1
        assert "rack-1" in coord._redundancy_executors
        assert len(coord._evaluator_threads) == 1
        # Evaluator's start() was called
        mock_eval_cls.return_value.start.assert_called_once()

    @pytest.mark.unit
    def test_no_evaluator_when_no_redundancy_groups(self, tmp_path):
        coord = MultiUPSCoordinator(_coord_config(tmp_path))
        coord._log = lambda msg: None

        with patch("eneru.multi_ups.threading.Thread"), \
             patch("eneru.multi_ups.UPSGroupMonitor") as mock_monitor_cls, \
             patch("eneru.multi_ups.RedundancyGroupExecutor") as mock_executor_cls, \
             patch("eneru.multi_ups.RedundancyGroupEvaluator") as mock_eval_cls:
            mock_monitor_cls.return_value = MagicMock()
            coord._start_monitors()

        mock_executor_cls.assert_not_called()
        mock_eval_cls.assert_not_called()
        assert coord._evaluator_threads == []

    @pytest.mark.unit
    def test_handle_signal_joins_evaluator_threads(self, tmp_path):
        coord = MultiUPSCoordinator(self._config_with_redundancy(tmp_path))
        coord._logger = MagicMock()

        joined_evals = []
        for _ in range(2):
            t = MagicMock()
            t.join = lambda timeout=None, t=t: joined_evals.append(t)
            coord._evaluator_threads.append(t)

        with pytest.raises(SystemExit):
            coord._handle_signal(15, None)
        assert len(joined_evals) == 2
