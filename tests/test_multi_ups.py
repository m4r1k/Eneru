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
    def test_defense_in_depth_lock(self, tmp_path):
        """L24: the REAL _handle_local_shutdown guard prevents a double local
        shutdown -- a second call (e.g. a second UPS group tripping) returns
        before running the body. Drives the real method, not a copy of it."""
        config = self._make_config(
            [UPSGroupConfig(ups=UPSConfig(name="UPS1"), is_local=True)],
            logging=LoggingConfig(
                state_file=str(tmp_path / "state"),
                battery_history_file=str(tmp_path / "history"),
                shutdown_flag_file=str(tmp_path / "global-shutdown-flag"),
            ),
            local_shutdown=LocalShutdownConfig(enabled=False),
        )
        coord = MultiUPSCoordinator(config)
        logs = []
        coord._log = lambda msg: logs.append(msg)
        coord._notification_worker = None

        coord._handle_local_shutdown("UPS1")
        coord._handle_local_shutdown("UPS2")

        # The body's "triggered by" line fires once; the second call hit the
        # guard (proceed=False) and returned before logging anything.
        triggered = [m for m in logs if "Local shutdown triggered by" in m]
        assert triggered == ["🚨  Local shutdown triggered by UPS1"]

    @pytest.mark.unit
    def test_clear_local_shutdown_state_resets_lock_and_flag(self, tmp_path):
        """5.2.2 (bug #4 / multi-UPS): _clear_local_shutdown_state resets
        BOTH the in-memory ``_local_shutdown_initiated`` lock and the
        unsuffixed ``_global_shutdown_flag`` so the next outage on any
        group can re-trigger. Without this, the per-monitor unlink in
        ``_handle_on_line`` re-arms only the suffixed per-group flag --
        the second OB still hits the coordinator's stuck lock."""
        flag_path = tmp_path / "global-shutdown-flag"
        config = self._make_config(
            [UPSGroupConfig(ups=UPSConfig(name="UPS1"), is_local=True)],
            logging=LoggingConfig(
                state_file=str(tmp_path / "state"),
                battery_history_file=str(tmp_path / "history"),
                shutdown_flag_file=str(flag_path),
            ),
            local_shutdown=LocalShutdownConfig(enabled=False),
        )
        coord = MultiUPSCoordinator(config)

        # Simulate a prior shutdown sequence having set both pieces.
        coord._local_shutdown_initiated = True
        flag_path.touch()
        assert flag_path.exists()

        coord._clear_local_shutdown_state()

        assert coord._local_shutdown_initiated is False, (
            "in-memory lock must be reset on POWER_RESTORED so the next "
            "OB re-triggers (bug #4 multi-UPS path)"
        )
        assert not flag_path.exists(), (
            "global flag must be cleared on POWER_RESTORED"
        )

    @pytest.mark.unit
    def test_clear_local_shutdown_state_refuses_mid_flight(self, tmp_path):
        """M1: while a local shutdown is committed and running outside the lock,
        an unrelated group's recovery must NOT re-arm the guard -- otherwise a
        concurrent trigger could admit a SECOND poweroff. Once the sequence
        returns (in_flight cleared), recovery re-arms normally."""
        flag_path = tmp_path / "global-shutdown-flag"
        config = self._make_config(
            [UPSGroupConfig(ups=UPSConfig(name="UPS1"), is_local=True)],
            logging=LoggingConfig(
                state_file=str(tmp_path / "state"),
                battery_history_file=str(tmp_path / "history"),
                shutdown_flag_file=str(flag_path),
            ),
            local_shutdown=LocalShutdownConfig(enabled=False),
        )
        coord = MultiUPSCoordinator(config)
        coord._local_shutdown_initiated = True
        coord._local_shutdown_in_flight = True
        flag_path.touch()

        coord._clear_local_shutdown_state()
        assert coord._local_shutdown_initiated is True, "must not re-arm mid-flight"
        assert flag_path.exists(), "flag must survive a mid-flight clear attempt"

        # Sequence finished -> recovery may re-arm.
        coord._local_shutdown_in_flight = False
        coord._clear_local_shutdown_state()
        assert coord._local_shutdown_initiated is False
        assert not flag_path.exists()

    @pytest.mark.unit
    def test_clear_local_shutdown_state_idempotent(self, tmp_path):
        """Safe to call when nothing was in flight (steady-state OL or
        repeated OL transitions)."""
        flag_path = tmp_path / "global-shutdown-flag"
        config = self._make_config(
            [UPSGroupConfig(ups=UPSConfig(name="UPS1"), is_local=True)],
            logging=LoggingConfig(
                state_file=str(tmp_path / "state"),
                battery_history_file=str(tmp_path / "history"),
                shutdown_flag_file=str(flag_path),
            ),
            local_shutdown=LocalShutdownConfig(enabled=False),
        )
        coord = MultiUPSCoordinator(config)
        # Nothing set, no flag file.
        coord._clear_local_shutdown_state()  # must not raise
        coord._clear_local_shutdown_state()  # idempotent
        assert coord._local_shutdown_initiated is False
        assert not flag_path.exists()

    @pytest.mark.unit
    def test_coordinator_passes_power_restored_callback_to_monitors(self):
        """The coordinator MUST wire its ``_clear_local_shutdown_state``
        into each monitor as ``power_restored_callback`` -- otherwise
        the OB/FSD->OL transition can clear the per-group flag but the
        coordinator's own flag/lock stay set (multi-UPS bug #4)."""
        import inspect
        from eneru import multi_ups as mu_mod

        src = inspect.getsource(mu_mod.MultiUPSCoordinator._start_monitors)
        assert "power_restored_callback=self._clear_local_shutdown_state" in src, (
            "coordinator must pass power_restored_callback to UPSGroupMonitor; "
            "without it, multi-UPS deployments still hit bug #4 even after "
            "the per-group _handle_on_line clears its suffixed flag."
        )


# ==============================================================================
# DAEMON-WIDE REPORTS
# ==============================================================================

class TestCoordinatorReportSender:
    """The fleet-wide report sender must NOT borrow a per-UPS log prefix."""

    def _coord_with_monitors(self):
        config = Config(ups_groups=[
            UPSGroupConfig(ups=UPSConfig(name="UPS-A@h"), is_local=True),
            UPSGroupConfig(ups=UPSConfig(name="UPS-B@h"), is_local=False),
        ])
        coord = MultiUPSCoordinator(config)
        coord._log = lambda msg: None
        primary = MagicMock()
        primary._stats_store = object()
        # A real monitor would prefix the body with its group; assert we DON'T
        # route through it.
        primary._send_notification = MagicMock(
            side_effect=AssertionError("must not use per-UPS _send_notification"))
        coord._monitors = [primary, MagicMock()]
        coord._notification_worker = MagicMock()
        return coord, primary

    @pytest.mark.unit
    def test_send_report_notification_has_no_per_ups_prefix(self):
        coord, primary = self._coord_with_monitors()
        coord._send_report_notification("Fleet digest body", "info", "report")
        # Routed straight through the worker with the PRIMARY store, never the
        # prefixing per-monitor _send_notification.
        primary._send_notification.assert_not_called()
        coord._notification_worker.send.assert_called_once()
        kwargs = coord._notification_worker.send.call_args.kwargs
        assert kwargs["category"] == "report"
        assert kwargs["store"] is primary._stats_store
        # body carries no "[UPS-A]" prefix; @ is escaped to avoid mentions.
        assert kwargs["body"].startswith("Fleet digest body")

    @pytest.mark.unit
    def test_send_report_notification_escapes_at(self):
        coord, _ = self._coord_with_monitors()
        coord._send_report_notification("covers UPS-A@h and UPS-B@h", "info", "report")
        body = coord._notification_worker.send.call_args.kwargs["body"]
        assert "@\u200B" in body and "@h" not in body.replace("@\u200B", "")

    @pytest.mark.unit
    def test_send_report_notification_no_worker_is_safe(self):
        coord, _ = self._coord_with_monitors()
        coord._notification_worker = None
        coord._send_report_notification("x", "info", "report")   # must not raise

    @pytest.mark.unit
    def test_maybe_send_reports_uses_coordinator_sender(self):
        import inspect
        from eneru import multi_ups as mu_mod
        src = inspect.getsource(mu_mod.MultiUPSCoordinator._maybe_send_reports)
        assert "self._send_report_notification" in src
        # and the old prefixing path is gone
        assert "primary._send_notification" not in src

    def _coord_reports_enabled(self):
        from eneru.config import ReportsConfig
        config = Config(
            ups_groups=[UPSGroupConfig(ups=UPSConfig(name="UPS-A@h"),
                                       is_local=True)],
            reports=ReportsConfig(enabled=True, daily=True),
        )
        coord = MultiUPSCoordinator(config)
        coord._log = lambda msg: None
        # A real monitor's .config is the full Config (has .ups.name + .energy);
        # a plain MagicMock satisfies both attribute reads in the unit list comp.
        mon = MagicMock()
        mon._stats_store = object()
        coord._monitors = [mon]
        return coord

    @pytest.mark.unit
    def test_maybe_send_reports_skipped_when_no_worker(self, monkeypatch):
        # If the notification worker is unavailable, nothing can actually be
        # enqueued — so we must NOT call maybe_send_due_reports_multi (which would
        # stamp the cadence meta and burn the period). The next tick should retry.
        from eneru import reports as reports_mod
        coord = self._coord_reports_enabled()
        coord._notification_worker = None
        called = []
        monkeypatch.setattr(reports_mod, "maybe_send_due_reports_multi",
                            lambda *a, **k: called.append(True) or [])
        coord._maybe_send_reports()
        assert called == []

    @pytest.mark.unit
    def test_maybe_send_reports_runs_when_worker_present(self, monkeypatch):
        from eneru import reports as reports_mod
        coord = self._coord_reports_enabled()
        coord._notification_worker = MagicMock()
        called = []
        monkeypatch.setattr(reports_mod, "maybe_send_due_reports_multi",
                            lambda *a, **k: called.append(True) or [])
        coord._maybe_send_reports()
        assert called == [True]


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
    def test_drain_skips_current_thread_no_self_join_crash(self, tmp_path):
        """Regression (C1): _drain_all_groups runs ON a monitor thread whose
        own Thread object is in self._threads. Joining the current thread
        raises RuntimeError('cannot join current thread'), which previously
        unwound the whole sequence BEFORE the host poweroff -- a missed
        shutdown. The drain must skip itself and still drain peers."""
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
        # The bug trigger: the CURRENT thread is in _threads (real runtime
        # shape -- the firing group's poll thread drives the drain).
        coord._threads = [threading.current_thread()]

        # Pre-fix this raised RuntimeError; post-fix it returns cleanly.
        coord._drain_all_groups(timeout=1)

        # And the peer drain must still have happened.
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
    def test_flag_write_failure_does_not_skip_local_shutdown(self, tmp_path):
        config = _coord_config(
            tmp_path,
            behavior=BehaviorConfig(dry_run=False),
            local_shutdown=LocalShutdownConfig(
                enabled=True,
                command="/sbin/poweroff",
            ),
        )
        coord = MultiUPSCoordinator(config)
        logs = []
        coord._log = logs.append
        coord._notification_worker = None

        with patch.object(
            type(coord._global_shutdown_flag),
            "touch",
            side_effect=OSError("read-only filesystem"),
        ), patch("eneru.multi_ups.time.sleep"), \
             patch("eneru.multi_ups.run_command") as mock_run:
            coord._handle_local_shutdown("UPS1")

        mock_run.assert_called_once()
        assert mock_run.call_args.args[0][0] == "/sbin/poweroff"
        assert any("Could not write shutdown flag" in line for line in logs)

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
    def test_real_shutdown_empty_command_logs_and_skips_poweroff(self, tmp_path):
        """Programmatic configs can bypass validation; fail closed if the local
        shutdown command is blank instead of trying to exec an empty argv."""
        config = _coord_config(
            tmp_path,
            behavior=BehaviorConfig(dry_run=False),
            local_shutdown=LocalShutdownConfig(enabled=True, command="   "),
        )
        coord = MultiUPSCoordinator(config)
        logs = []
        coord._log = logs.append
        coord._notification_worker = None

        with patch("eneru.multi_ups.time.sleep"), \
             patch("eneru.multi_ups.run_command") as mock_run:
            coord._handle_local_shutdown("UPS1")

        mock_run.assert_not_called()
        assert any("local_shutdown.command is empty" in line for line in logs)

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

        # _drain_all_groups now joins twice per thread: a short window after
        # signaling stop_event so peer monitors finish their poll cycle,
        # then a final wait once the per-monitor shutdown sequences have run.
        assert live_thread.join.called
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
        # Empty store list so the v5.2.1 deferred-delivery branch is a
        # no-op (no first_store to schedule against).
        coord._notification_worker._stores = []
        coord._notification_worker._stores_lock = threading.RLock()
        coord._threads = []

        Path(config.logging.shutdown_flag_file).touch()

        with patch("eneru.multi_ups.schedule_deferred_stop_or_eager_send"), \
             patch("eneru.multi_ups.sys.exit") as mock_exit:
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

        # Deadline-based join (H10): each thread is joined once with a positive,
        # bounded timeout (<= the 5 s no-in-flight budget), not necessarily the
        # exact integer 5 -- it's `deadline - now`.
        for thread in threads:
            thread.join.assert_called_once()
            timeout = thread.join.call_args.kwargs["timeout"]
            assert 0 < timeout <= 5

    @pytest.mark.unit
    def test_handle_signal_enqueues_stop_after_flush_and_stop(self, tmp_path):
        """v5.2.1: coordinator enqueues the stop AFTER flush+stop on the
        worker so the row stays `pending` in SQLite, then schedules a
        systemd-run timer (or eager-fallback) to deliver it ~15s later
        unless the next daemon's classifier supersedes it first."""
        import signal as _signal

        config = _coord_config(tmp_path)
        coord = MultiUPSCoordinator(config)
        coord._log = lambda msg: None
        coord._notification_worker = MagicMock()
        coord._notification_worker._stores = []
        coord._notification_worker._stores_lock = threading.RLock()
        coord._threads = []

        with patch("eneru.multi_ups.read_upgrade_marker", return_value=None), \
             patch("eneru.multi_ups.read_shutdown_marker", return_value=None), \
             patch("eneru.multi_ups.write_shutdown_marker"), \
             patch("eneru.multi_ups.schedule_deferred_stop_or_eager_send"), \
             patch("eneru.multi_ups.sys.exit"):
            coord._handle_signal(_signal.SIGTERM, None)

        names = [c[0] for c in coord._notification_worker.method_calls]
        assert "flush" in names
        assert "stop" in names
        assert "send" in names
        assert names.index("flush") < names.index("send"), (
            f"flush must happen before send; got order: {names}"
        )
        assert names.index("stop") < names.index("send"), (
            f"stop must happen before send; got order: {names}"
        )

    @pytest.mark.unit
    def test_handle_signal_skips_stop_notification_on_upgrade(self, tmp_path):
        """v5.2.1: when an upgrade marker is on disk (postinstall.sh
        dropped it before systemctl restart), the coordinator skips the
        lifecycle stop entirely AND the deferred-delivery scheduler —
        the next daemon's '📦  Upgraded' message covers the transition."""
        import signal as _signal

        config = _coord_config(tmp_path)
        coord = MultiUPSCoordinator(config)
        coord._log = lambda msg: None
        coord._notification_worker = MagicMock()
        coord._notification_worker._stores = []
        coord._notification_worker._stores_lock = threading.RLock()
        coord._threads = []

        marker = {"old_version": "5.2.0", "new_version": "5.2.1"}
        with patch("eneru.multi_ups.read_upgrade_marker", return_value=marker), \
             patch("eneru.multi_ups.read_shutdown_marker", return_value=None), \
             patch("eneru.multi_ups.write_shutdown_marker"), \
             patch("eneru.multi_ups.schedule_deferred_stop_or_eager_send") as sched, \
             patch("eneru.multi_ups.sys.exit"):
            coord._handle_signal(_signal.SIGTERM, None)

        send_calls = coord._notification_worker.send.call_args_list
        assert send_calls == [], (
            "Expected no lifecycle send when upgrade marker present; "
            f"got: {send_calls}"
        )
        sched.assert_not_called()
        # flush + stop still happen (drains any non-lifecycle rows
        # like a sequence-complete summary that landed mid-shutdown).
        coord._notification_worker.flush.assert_called_once()
        coord._notification_worker.stop.assert_called_once()

    @pytest.mark.unit
    def test_initialize_cancels_prev_pending_lifecycle_rows(self, tmp_path):
        """v5.2.1: coordinator startup sweeps each per-UPS store and
        cancels any pending lifecycle row from the previous instance
        (the deferred 'Service Stopped' from _handle_signal). Without
        this, multi-UPS users still see two notifications on every
        restart — the single-UPS path does this inside
        UPSGroupMonitor._emit_lifecycle_startup_notification but that
        function early-returns in coordinator mode."""
        from eneru.stats import StatsStore

        config = _coord_config(tmp_path)
        # Point statistics at tmp_path so we can pre-populate a real
        # SQLite store with a pending lifecycle row, then verify the
        # coordinator's sweep cancels it.
        config.statistics.db_directory = str(tmp_path)
        coord = MultiUPSCoordinator(config)
        coord._log = lambda msg: None

        # Sanitize the same way the coordinator does to find the DB.
        ups_name = config.ups_groups[0].ups.name
        sanitized = (ups_name.replace("@", "-")
                     .replace(":", "-").replace("/", "-"))
        db_path = tmp_path / f"{sanitized}.db"

        # Pre-populate: create a store, insert a pending lifecycle row.
        store = StatsStore(db_path)
        store.open()
        try:
            row_id = store.enqueue_notification(
                body="🛑  Eneru Service Stopped\nMonitoring is now inactive.",
                notify_type="warning",
                category="lifecycle",
                ts=1000,
            )
            assert row_id is not None
            pending_before = store.find_pending_by_category("lifecycle")
            assert len(pending_before) == 1, (
                f"Expected 1 pending row, got {pending_before}"
            )
        finally:
            store.close()

        # Run the sweep.
        coord._cancel_prev_pending_lifecycle_rows(tmp_path)

        # Verify the row is now cancelled with reason='superseded'.
        store = StatsStore(db_path)
        store.open()
        try:
            pending_after = store.find_pending_by_category("lifecycle")
            assert pending_after == [], (
                "Expected zero pending lifecycle rows after sweep; "
                f"got: {pending_after}"
            )
            # Confirm the cancel_reason is 'superseded' specifically.
            row = store._conn.execute(
                "SELECT status, cancel_reason FROM notifications "
                "WHERE id = ?", (row_id,)
            ).fetchone()
            assert row == ("cancelled", "superseded")
        finally:
            store.close()

    @pytest.mark.unit
    def test_cancel_prev_pending_lifecycle_rows_skips_missing_db(self, tmp_path):
        """First-ever start has no per-UPS DB on disk yet; the sweep
        must silently skip and not raise."""
        config = _coord_config(tmp_path)
        config.statistics.db_directory = str(tmp_path)
        coord = MultiUPSCoordinator(config)
        coord._log = lambda msg: None
        # No DB created; nothing to sweep. Should be a no-op.
        coord._cancel_prev_pending_lifecycle_rows(tmp_path)
        # If we got here, the no-op succeeded.


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
    def test_start_monitors_starts_remote_health_for_redundancy_remotes(self, tmp_path):
        config = self._config_with_redundancy(tmp_path)
        config.redundancy_groups[0].remote_servers = [
            RemoteServerConfig(
                name="nas",
                enabled=True,
                host="10.0.0.10",
                user="root",
            ),
        ]
        coord = MultiUPSCoordinator(config)
        coord._log = lambda msg: None

        with patch("eneru.multi_ups.threading.Thread"), \
             patch("eneru.multi_ups.UPSGroupMonitor") as mock_monitor_cls, \
             patch("eneru.multi_ups.RedundancyGroupExecutor"), \
             patch("eneru.multi_ups.RedundancyGroupEvaluator") as mock_eval_cls, \
             patch("eneru.multi_ups.RemoteHealthManager") as mock_manager_cls:
            mock_monitor_cls.return_value = MagicMock()
            mock_eval_cls.return_value = MagicMock()
            mock_manager_cls.return_value = MagicMock()
            coord._start_monitors()

        assert mock_manager_cls.call_args.kwargs["group_label"] == "redundancy:rack-1"
        assert mock_manager_cls.call_args.kwargs["servers"][0].name == "nas"
        mock_manager_cls.return_value.start.assert_called_once()

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

    @pytest.mark.unit
    def test_start_monitors_clears_redundancy_executor_state_at_startup(self, tmp_path):
        """5.3.0 contract: the daemon owns the redundancy flag's
        lifecycle. The coordinator must call clear_shutdown_state() on
        every freshly constructed executor so a stale on-disk flag from
        a prior daemon instance can't silently block the next quorum
        loss (issue #4)."""
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

        # The executor mock's clear_shutdown_state must have been called
        # exactly once -- before the evaluator started polling.
        mock_executor_cls.return_value.clear_shutdown_state.assert_called_once_with(
            refuse_active_peer=True
        )

    @pytest.mark.unit
    @pytest.mark.parametrize("exc", [
        RuntimeError("active peer"),
        PermissionError("/var/run/ups-shutdown-redundancy-rack"),
        OSError("/var/run/ups-shutdown-redundancy-rack"),
    ])
    def test_start_monitors_exits_when_redundancy_flag_cannot_be_cleared(
        self, tmp_path, exc
    ):
        """A startup flag-cleanup failure is fatal: running with a
        possibly active peer would make the re-entry guard untrustworthy."""
        config = self._config_with_redundancy(tmp_path)
        coord = MultiUPSCoordinator(config)
        coord._log = MagicMock()

        with patch("eneru.multi_ups.threading.Thread"), \
             patch("eneru.multi_ups.UPSGroupMonitor") as mock_monitor_cls, \
             patch("eneru.multi_ups.RedundancyGroupExecutor") as mock_executor_cls:
            mock_monitor_cls.return_value = MagicMock()
            mock_executor_cls.return_value.clear_shutdown_state.side_effect = exc
            with pytest.raises(SystemExit):
                coord._start_monitors()

        assert any("FATAL ERROR" in call.args[0]
                   for call in coord._log.call_args_list)

    @pytest.mark.unit
    def test_handle_signal_clears_redundancy_executor_state(self, tmp_path):
        """5.3.0 contract: graceful exit clears redundancy flags too,
        not just the per-UPS coordinator flag."""
        coord = MultiUPSCoordinator(self._config_with_redundancy(tmp_path))
        coord._logger = MagicMock()
        ex_a, ex_b = MagicMock(), MagicMock()
        coord._redundancy_executors = {"rg-A": ex_a, "rg-B": ex_b}

        with pytest.raises(SystemExit):
            coord._handle_signal(15, None)

        ex_a.clear_shutdown_state.assert_called_once()
        ex_b.clear_shutdown_state.assert_called_once()

    @pytest.mark.unit
    def test_handle_signal_swallows_redundancy_clear_failures(self, tmp_path):
        """A flag-cleanup failure in one executor must NOT block exit
        nor stop other executors from being cleared."""
        coord = MultiUPSCoordinator(self._config_with_redundancy(tmp_path))
        coord._logger = MagicMock()
        bad = MagicMock()
        bad.clear_shutdown_state.side_effect = OSError("flag dir gone")
        good = MagicMock()
        coord._redundancy_executors = {"rg-bad": bad, "rg-good": good}

        with pytest.raises(SystemExit):
            coord._handle_signal(15, None)
        good.clear_shutdown_state.assert_called_once()


class TestCoordinatorRunHandlesKeyboardInterrupt:
    """`MultiUPSCoordinator.run()` must catch KeyboardInterrupt and
    route through _handle_signal so the per-group monitors are
    cleaned up — otherwise Ctrl-C leaks daemon threads."""

    @pytest.mark.unit
    def test_keyboard_interrupt_invokes_handle_signal(self, tmp_path):
        from eneru import ConfigLoader
        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
ups:
  - name: "UPS-A"
  - name: "UPS-B"
""")
        config = ConfigLoader.load(str(config_file))
        coord = MultiUPSCoordinator(config)

        # Force KeyboardInterrupt out of _start_monitors and assert that
        # _handle_signal is called rather than letting it propagate raw.
        with patch.object(coord, "_initialize"), \
             patch.object(coord, "_start_monitors", side_effect=KeyboardInterrupt), \
             patch.object(coord, "_handle_signal") as handler:
            coord.run()

        handler.assert_called_once()
        # First positional arg is signal.SIGINT
        import signal as _signal
        assert handler.call_args.args[0] == _signal.SIGINT


class TestCoordinatorReadLastSeenVersion:
    """`_read_last_seen_version_from_first_group` reads the meta table
    on the first group's per-UPS DB so the lifecycle classifier can
    spot version upgrades. SQLite, OS, and missing-row failures must
    return None (best-effort) — never crash coordinator startup."""

    @staticmethod
    def _coord(tmp_path):
        from eneru import ConfigLoader
        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
ups:
  - name: "UPS-A"
  - name: "UPS-B"
""")
        config = ConfigLoader.load(str(config_file))
        return MultiUPSCoordinator(config)

    @pytest.mark.unit
    def test_returns_none_when_no_groups(self, tmp_path):
        from eneru import Config
        coord = MultiUPSCoordinator(Config(ups_groups=[]))
        assert coord._read_last_seen_version_from_first_group(tmp_path) is None

    @pytest.mark.unit
    def test_returns_none_when_open_readonly_raises(self, tmp_path):
        coord = self._coord(tmp_path)
        with patch("eneru.multi_ups.StatsStore.open_readonly",
                   side_effect=OSError("perm denied")):
            assert coord._read_last_seen_version_from_first_group(tmp_path) is None

    @pytest.mark.unit
    def test_returns_none_when_db_does_not_exist(self, tmp_path):
        coord = self._coord(tmp_path)
        # open_readonly returns None when the DB file doesn't exist
        with patch("eneru.multi_ups.StatsStore.open_readonly", return_value=None):
            assert coord._read_last_seen_version_from_first_group(tmp_path) is None

    @pytest.mark.unit
    def test_returns_none_on_query_failure(self, tmp_path):
        coord = self._coord(tmp_path)
        bad_conn = MagicMock()
        bad_conn.execute.side_effect = Exception("schema corrupt")
        with patch("eneru.multi_ups.StatsStore.open_readonly", return_value=bad_conn):
            assert coord._read_last_seen_version_from_first_group(tmp_path) is None
        # finally block always closes
        bad_conn.close.assert_called_once()

    @pytest.mark.unit
    def test_returns_none_when_meta_row_absent(self, tmp_path):
        coord = self._coord(tmp_path)
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = None
        with patch("eneru.multi_ups.StatsStore.open_readonly", return_value=conn):
            assert coord._read_last_seen_version_from_first_group(tmp_path) is None

    @pytest.mark.unit
    def test_returns_string_when_meta_row_present(self, tmp_path):
        coord = self._coord(tmp_path)
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = ("5.4.0-rc2",)
        with patch("eneru.multi_ups.StatsStore.open_readonly", return_value=conn):
            assert coord._read_last_seen_version_from_first_group(tmp_path) == "5.4.0-rc2"

    @pytest.mark.unit
    def test_close_failure_swallowed_in_finally(self, tmp_path):
        coord = self._coord(tmp_path)
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = ("5.4.0",)
        conn.close.side_effect = OSError("already closed")
        with patch("eneru.multi_ups.StatsStore.open_readonly", return_value=conn):
            assert coord._read_last_seen_version_from_first_group(tmp_path) == "5.4.0"


class TestCoordinatorRedundancyStatsLogging:
    """`_record_redundancy_remote_health_event` mirrors a redundancy-group
    event into each member's per-UPS stats DB. A bad/closed store on
    one member must not suppress fan-out to the others."""

    @pytest.mark.unit
    def test_fanout_continues_when_one_member_store_raises(self, tmp_path):
        from eneru import ConfigLoader, RedundancyGroupConfig
        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
ups:
  - name: "UPS-A"
  - name: "UPS-B"
  - name: "UPS-C"
""")
        config = ConfigLoader.load(str(config_file))
        coord = MultiUPSCoordinator(config)

        log = []
        coord._log = log.append

        good_a = MagicMock()
        bad_b = MagicMock()
        bad_b.log_event.side_effect = OSError("db locked")
        good_c = MagicMock()

        m_a = MagicMock(_stats_store=good_a)
        m_b = MagicMock(_stats_store=bad_b)
        m_c = MagicMock(_stats_store=good_c)
        monitors_by_name = {"UPS-A": m_a, "UPS-B": m_b, "UPS-C": m_c}

        group = RedundancyGroupConfig(
            name="rack",
            ups_sources=["UPS-A", "UPS-B", "UPS-C"],
        )

        coord._record_redundancy_remote_health_event(
            group, monitors_by_name,
            event_type="REMOTE_HEALTH_HEALTHY", detail="recovered",
            notification_sent=True,
        )

        good_a.log_event.assert_called_once()
        good_c.log_event.assert_called_once()
        # The error from bad_b was logged as a warning, not raised
        assert any("failed to record redundancy remote-health" in m for m in log), log

    @pytest.mark.unit
    def test_handle_signal_stops_subsystems_and_threads(self, tmp_path):
        """SIGTERM/SIGINT must stop the redundancy remote-health managers,
        MQTT publisher, API server, and join all monitor/evaluator threads
        before exit."""
        from eneru import ConfigLoader
        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
ups:
  - name: "UPS-A"
  - name: "UPS-B"
        """)
        config = ConfigLoader.load(str(config_file))
        config.logging.shutdown_flag_file = str(tmp_path / "shutdown-flag")
        coord = MultiUPSCoordinator(config)
        coord._log = MagicMock()

        rh1 = MagicMock()
        rh2 = MagicMock()
        coord._redundancy_remote_health_managers = [rh1, rh2]
        coord._mqtt_publisher = MagicMock()
        coord._api_server = MagicMock()

        t1 = MagicMock()
        t2 = MagicMock()
        coord._threads = [t1, t2]
        coord._evaluator_threads = []
        mon = MagicMock()
        mon._shutdown_flag_path = tmp_path / "no-monitor-shutdown"
        coord._monitors = [mon]
        coord._notification_worker = None
        coord._stats_stores = []
        coord._stats_writers = []
        coord._stop_event = MagicMock()

        with patch("eneru.multi_ups.read_upgrade_marker", return_value=None), \
             patch("eneru.multi_ups.read_shutdown_marker", return_value=None), \
             patch("eneru.multi_ups.write_shutdown_marker"), \
             patch("eneru.multi_ups.schedule_deferred_stop_or_eager_send"), \
             pytest.raises(SystemExit):
            coord._handle_signal(15, None)

        rh1.stop.assert_called_once()
        rh2.stop.assert_called_once()
        coord._mqtt_publisher.stop.assert_called_once()
        coord._api_server.stop.assert_called_once()
        # Deadline-based join (H10): bounded positive timeout, not exactly 5.
        for thread in (t1, t2):
            thread.join.assert_called_once()
            assert 0 < thread.join.call_args.kwargs["timeout"] <= 5
        mon._stop_stats.assert_called_once()

    @pytest.mark.unit
    def test_handle_signal_logs_monitor_stats_close_failure(self, tmp_path):
        import signal as _signal

        config = _coord_config(tmp_path)
        coord = MultiUPSCoordinator(config)
        logs = []
        coord._log = logs.append
        coord._notification_worker = None
        coord._threads = []
        coord._evaluator_threads = []
        mon = MagicMock()
        mon._stop_stats.side_effect = RuntimeError("sqlite busy")
        coord._monitors = [mon]
        coord._redundancy_executors = {}

        with patch("eneru.multi_ups.read_upgrade_marker", return_value=None), \
             patch("eneru.multi_ups.read_shutdown_marker", return_value=None), \
             patch("eneru.multi_ups.write_shutdown_marker"), \
             patch("eneru.multi_ups.sys.exit"):
            coord._handle_signal(_signal.SIGTERM, None)

        assert any(
            "Failed to close monitor stats" in line and "sqlite busy" in line
            for line in logs
        )

    @pytest.mark.unit
    def test_handle_signal_skips_lifecycle_notif_during_upgrade(self, tmp_path):
        """An in-flight deb/rpm upgrade (read_upgrade_marker returns truthy)
        means the next daemon will emit 'Upgraded vX → vY' — suppress the
        lifecycle stop here so the user sees one combined message."""
        from eneru import ConfigLoader
        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
ups:
  - name: "UPS-A"
  - name: "UPS-B"
        """)
        config = ConfigLoader.load(str(config_file))
        config.logging.shutdown_flag_file = str(tmp_path / "shutdown-flag")
        coord = MultiUPSCoordinator(config)
        coord._log = MagicMock()
        coord._redundancy_remote_health_managers = []
        coord._mqtt_publisher = None
        coord._api_server = None
        coord._threads = []
        coord._evaluator_threads = []
        worker = MagicMock()
        coord._notification_worker = worker
        coord._stats_stores = []
        coord._stats_writers = []
        coord._stop_event = MagicMock()

        with patch("eneru.multi_ups.read_upgrade_marker",
                   return_value={"from": "5.3.0", "to": "5.4.0"}), \
             patch("eneru.multi_ups.read_shutdown_marker", return_value=None), \
             patch("eneru.multi_ups.write_shutdown_marker"), \
             patch("eneru.multi_ups.schedule_deferred_stop_or_eager_send") as scheduler, \
             pytest.raises(SystemExit):
            coord._handle_signal(15, None)

        scheduler.assert_not_called()


    @pytest.mark.unit
    def test_fanout_skips_members_without_a_store(self, tmp_path):
        """A monitor whose _stats_store is None (open() failed) is
        silently skipped — not a warning, just no fan-out for that one."""
        from eneru import ConfigLoader, RedundancyGroupConfig
        config_file = tmp_path / "config.yaml"
        config_file.write_text("""
ups:
  - name: "UPS-A"
  - name: "UPS-B"
""")
        config = ConfigLoader.load(str(config_file))
        coord = MultiUPSCoordinator(config)
        coord._log = MagicMock()

        good_a = MagicMock()
        m_a = MagicMock(_stats_store=good_a)
        m_b = MagicMock(_stats_store=None)
        monitors_by_name = {"UPS-A": m_a, "UPS-B": m_b}

        group = RedundancyGroupConfig(
            name="rack", ups_sources=["UPS-A", "UPS-B"],
        )

        coord._record_redundancy_remote_health_event(
            group, monitors_by_name,
            event_type="REMOTE_HEALTH_FAILED", detail="ssh failed",
            notification_sent=False,
        )

        good_a.log_event.assert_called_once()


# ==============================================================================
# DEFENSIVE-BRANCH COVERAGE FOR MULTI_UPS COORDINATOR
# ==============================================================================


class TestCoordinatorRunKeyboardInterrupt:
    """`MultiUPSCoordinator.run` catches KeyboardInterrupt and routes to
    `_handle_signal(SIGINT, None)` rather than propagating."""

    @pytest.mark.unit
    def test_keyboard_interrupt_during_initialize_calls_handle_signal(self, tmp_path):
        import signal as _signal

        config = _coord_config(tmp_path)
        coord = MultiUPSCoordinator(config)
        coord._initialize = MagicMock(side_effect=KeyboardInterrupt())
        coord._handle_signal = MagicMock()

        coord.run()

        coord._handle_signal.assert_called_once_with(_signal.SIGINT, None)


class TestCoordinatorInitializeLogPermission:
    """When the configured log-file path is unwritable, `_initialize` must
    swallow the PermissionError (line 99-100)."""

    @pytest.mark.unit
    def test_initialize_swallows_permission_error_on_log_touch(self, tmp_path):
        config = _coord_config(tmp_path)
        coord = MultiUPSCoordinator(config)

        original_touch = Path.touch

        def raising_touch(self, *args, **kwargs):
            # Raise only for the configured log file; let every other Path
            # (state, flag, stats DB) keep its real behavior so we don't
            # produce side-effects elsewhere in _initialize.
            if str(self) == config.logging.file:
                raise PermissionError("read-only")
            return original_touch(self, *args, **kwargs)

        with patch("eneru.multi_ups.signal.signal"), \
             patch.object(Path, "touch", new=raising_touch):
            # Must not raise.
            coord._initialize()


class TestCoordinatorCancelPrevPendingSweepFailures:
    """`_cancel_prev_pending_lifecycle_rows` logs and continues on SQLite
    and OSError failures, both for the main sweep and the finally-close."""

    @pytest.mark.unit
    def test_sqlite_error_during_sweep_is_logged_not_raised(self, tmp_path):
        from eneru import ConfigLoader
        from eneru.stats import StatsStore
        config_file = tmp_path / "config.yaml"
        config_file.write_text("ups:\n  - name: UPS-A\n")
        config = ConfigLoader.load(str(config_file))
        config.statistics.db_directory = str(tmp_path)
        coord = MultiUPSCoordinator(config)
        log = []
        coord._log = log.append

        # Pre-create the DB so the sweep loop reaches the store.open() path.
        sanitized = "UPS-A"
        db_path = tmp_path / f"{sanitized}.db"
        db_path.touch()

        # Stub StatsStore so open() succeeds but the query raises.
        bad_store = MagicMock()
        import sqlite3 as _sqlite3
        bad_store.find_pending_by_category.side_effect = _sqlite3.OperationalError(
            "db is locked"
        )
        with patch("eneru.multi_ups.StatsStore", return_value=bad_store):
            coord._cancel_prev_pending_lifecycle_rows(tmp_path)

        assert any("Lifecycle sweep skipped" in m for m in log), log
        # close() was still called in finally.
        bad_store.close.assert_called_once()

    @pytest.mark.unit
    def test_close_failure_in_finally_is_logged_not_raised(self, tmp_path):
        from eneru import ConfigLoader
        config_file = tmp_path / "config.yaml"
        config_file.write_text("ups:\n  - name: UPS-A\n")
        config = ConfigLoader.load(str(config_file))
        config.statistics.db_directory = str(tmp_path)
        coord = MultiUPSCoordinator(config)
        log = []
        coord._log = log.append

        sanitized = "UPS-A"
        db_path = tmp_path / f"{sanitized}.db"
        db_path.touch()

        good_store = MagicMock()
        good_store.find_pending_by_category.return_value = []
        good_store.close.side_effect = OSError("disk gone")
        with patch("eneru.multi_ups.StatsStore", return_value=good_store):
            coord._cancel_prev_pending_lifecycle_rows(tmp_path)

        assert any("Failed to close stats DB" in m for m in log), log


class TestCoordinatorRedundancyRemoteHealthDisabled:
    """`_start_redundancy_remote_health` removes the stale sidecar when
    all servers are disabled — and swallows OSError if the unlink fails."""

    def _config_with_redundancy(self, tmp_path):
        from eneru import RedundancyGroupConfig
        return _coord_config(
            tmp_path,
            ups_groups=[
                UPSGroupConfig(ups=UPSConfig(name="UPS1@h"), is_local=False),
                UPSGroupConfig(ups=UPSConfig(name="UPS2@h"), is_local=False),
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
    def test_unlink_oserror_is_swallowed(self, tmp_path):
        config = self._config_with_redundancy(tmp_path)
        coord = MultiUPSCoordinator(config)
        coord._log = MagicMock()

        with patch("eneru.multi_ups.remote_health_sidecar_path") as sidecar:
            stale_path = MagicMock()
            stale_path.unlink.side_effect = OSError("EACCES")
            sidecar.return_value = stale_path
            # Must not raise.
            coord._start_redundancy_remote_health(
                config.redundancy_groups[0], monitors_by_name={},
            )

    @pytest.mark.unit
    def test_notify_fn_routes_to_notification_worker(self, tmp_path):
        """When a notification worker exists, the redundancy
        remote-health notify_fn closure dispatches via worker.send."""
        config = self._config_with_redundancy(tmp_path)
        config.redundancy_groups[0].remote_servers = [
            RemoteServerConfig(
                name="nas", enabled=True, host="10.0.0.10", user="root",
            ),
        ]
        coord = MultiUPSCoordinator(config)
        coord._log = MagicMock()
        worker = MagicMock()
        coord._notification_worker = worker

        with patch("eneru.multi_ups.RemoteHealthManager") as mgr_cls:
            mgr_cls.return_value = MagicMock()
            coord._start_redundancy_remote_health(
                config.redundancy_groups[0], monitors_by_name={},
            )

        notify_fn = mgr_cls.call_args.kwargs["notify_fn"]
        notify_fn("alert body", "warning")
        worker.send.assert_called_once()
        # Category for redundancy remote-health alerts is "health".
        assert worker.send.call_args.kwargs["category"] == "health"
        worker.send.reset_mock()
        coord._notification_worker = None
        notify_fn("muted body", "warning")
        worker.send.assert_not_called()


class TestCoordinatorStartServersIdempotent:
    """The API server and MQTT publisher starters are idempotent — calling
    them again when one is already running is a no-op."""

    @pytest.mark.unit
    def test_start_api_server_no_op_when_already_started(self, tmp_path):
        coord = MultiUPSCoordinator(_coord_config(tmp_path))
        sentinel = MagicMock()
        coord._api_server = sentinel
        with patch("eneru.multi_ups.EneruAPIServer") as cls:
            coord._start_api_server()
        cls.assert_not_called()
        assert coord._api_server is sentinel

    @pytest.mark.unit
    def test_start_mqtt_publisher_no_op_when_already_started(self, tmp_path):
        coord = MultiUPSCoordinator(_coord_config(tmp_path))
        sentinel = MagicMock()
        coord._mqtt_publisher = sentinel
        with patch("eneru.multi_ups.MQTTPublisher") as cls:
            coord._start_mqtt_publisher()
        cls.assert_not_called()
        assert coord._mqtt_publisher is sentinel


class TestCoordinatorReloadNotificationWorker:
    """Notification reload should leave every monitor/executor with a coherent
    worker reference, including disabled and failed-reload branches."""

    @pytest.mark.unit
    def test_reload_notification_worker_disabled_clears_existing_refs(
        self, tmp_path: Path,
    ) -> None:
        coord = MultiUPSCoordinator(_coord_config(
            tmp_path, notifications=NotificationsConfig(enabled=False)))
        old_worker = MagicMock()
        coord._notification_worker = old_worker
        mon = MagicMock()
        executor = MagicMock()
        coord._monitors = [mon]
        coord._redundancy_executors = {"rg": executor}
        logs = []
        coord._log = logs.append

        coord._reload_notification_worker()

        old_worker.stop.assert_called_once()
        assert coord._notification_worker is None
        assert mon._notification_worker is None
        assert executor._notification_worker is None
        assert any("Notifications: disabled" in line for line in logs)

    @pytest.mark.unit
    def test_reload_notification_worker_logs_when_apprise_missing(
        self, tmp_path: Path,
    ) -> None:
        coord = MultiUPSCoordinator(_coord_config(
            tmp_path,
            notifications=NotificationsConfig(enabled=True, urls=["json://x"]),
        ))
        logs = []
        coord._log = logs.append

        with patch("eneru.multi_ups.APPRISE_AVAILABLE", False), \
             patch("eneru.multi_ups.NotificationWorker") as worker_cls:
            coord._reload_notification_worker()

        worker_cls.assert_not_called()
        assert coord._notification_worker is None
        assert any("apprise not installed" in line for line in logs)

    @pytest.mark.unit
    def test_reload_notification_worker_start_failure_clears_refs(
        self, tmp_path: Path,
    ) -> None:
        coord = MultiUPSCoordinator(_coord_config(
            tmp_path,
            notifications=NotificationsConfig(enabled=True, urls=["json://x"]),
        ))
        mon = MagicMock()
        executor = MagicMock()
        coord._monitors = [mon]
        coord._redundancy_executors = {"rg": executor}
        worker = MagicMock()
        worker.start.return_value = False
        logs = []
        coord._log = logs.append

        with patch("eneru.multi_ups.APPRISE_AVAILABLE", True), \
             patch("eneru.multi_ups.NotificationWorker", return_value=worker):
            coord._reload_notification_worker()

        assert coord._notification_worker is None
        assert mon._notification_worker is None
        assert executor._notification_worker is None
        assert any("Failed to reload notifications" in line for line in logs)

    @pytest.mark.unit
    def test_reload_notification_worker_registers_open_stores(
        self, tmp_path: Path,
    ) -> None:
        coord = MultiUPSCoordinator(_coord_config(
            tmp_path,
            notifications=NotificationsConfig(enabled=True, urls=["json://x"]),
        ))
        open_store = MagicMock()
        open_store._conn = object()
        closed_store = MagicMock()
        closed_store._conn = None
        mon_open = MagicMock(_stats_store=open_store)
        mon_closed = MagicMock(_stats_store=closed_store)
        executor = MagicMock()
        coord._monitors = [mon_open, mon_closed]
        coord._redundancy_executors = {"rg": executor}
        worker = MagicMock()
        worker.start.return_value = True
        worker.get_service_count.return_value = 1
        logs = []
        coord._log = logs.append

        with patch("eneru.multi_ups.APPRISE_AVAILABLE", True), \
             patch("eneru.multi_ups.NotificationWorker", return_value=worker):
            coord._reload_notification_worker()

        assert coord._notification_worker is worker
        assert mon_open._notification_worker is worker
        assert mon_closed._notification_worker is worker
        assert executor._notification_worker is worker
        worker.register_store.assert_called_once_with(open_store)
        assert any("Notifications reloaded" in line for line in logs)


class TestRunMonitorCrashStoreSelection:
    """`_run_monitor`'s crash-notify path passes `store=None` when the
    monitor's stats store never opened — protecting the worker from a
    half-initialized store (line 470)."""

    @pytest.mark.unit
    def test_unopened_store_normalized_to_none(self, tmp_path):
        coord = MultiUPSCoordinator(_coord_config(tmp_path))
        coord._log = MagicMock()
        worker = MagicMock()
        coord._notification_worker = worker

        monitor = MagicMock()
        # Store object exists but `_conn` is None — never opened.
        unopened_store = MagicMock()
        unopened_store._conn = None
        monitor._stats_store = unopened_store
        monitor.run.side_effect = RuntimeError("crash")

        group = MagicMock()
        group.ups.label = "UPS-X"

        coord._run_monitor(monitor, group)

        # Worker.send was called with store=None (unopened store filtered).
        assert worker.send.call_args.kwargs["store"] is None


class TestOnGroupShutdownEdgeCases:
    """`_on_group_shutdown` has three branches today: group=None early
    return, the local-shutdown handoff, and the non-local
    --exit-after-shutdown handoff."""

    @pytest.mark.unit
    def test_none_group_is_no_op(self, tmp_path):
        coord = MultiUPSCoordinator(_coord_config(tmp_path))
        coord._handle_local_shutdown = MagicMock()
        coord._on_group_shutdown(None)
        coord._handle_local_shutdown.assert_not_called()

    @pytest.mark.unit
    def test_nonlocal_with_exit_after_shutdown_sets_stop_event(self, tmp_path):
        """A non-local group whose shutdown completes triggers stop_event.set()
        when --exit-after-shutdown is enabled."""
        config = _coord_config(
            tmp_path,
            ups_groups=[
                UPSGroupConfig(ups=UPSConfig(name="UPS1@h"), is_local=False),
            ],
            local_shutdown=LocalShutdownConfig(enabled=False, trigger_on="local_only"),
        )
        coord = MultiUPSCoordinator(config, exit_after_shutdown=True)
        coord._log = MagicMock()
        coord._handle_local_shutdown = MagicMock()
        coord._stop_event = MagicMock()

        group = config.ups_groups[0]
        coord._on_group_shutdown(group)

        coord._handle_local_shutdown.assert_not_called()
        coord._stop_event.set.assert_called_once()


class TestHandleLocalShutdownLockReentry:
    """The defense-in-depth lock makes a second `_handle_local_shutdown`
    call a no-op when the first one is still in flight (line 538-539)."""

    @pytest.mark.unit
    def test_second_call_returns_after_lock_check(self, tmp_path):
        coord = MultiUPSCoordinator(_coord_config(tmp_path))
        coord._log = MagicMock()
        # Simulate the first call having already set the lock.
        coord._local_shutdown_initiated = True

        # The global flag should NOT be touched on the re-entry path.
        assert not coord._global_shutdown_flag.exists()
        coord._handle_local_shutdown("UPS-A")
        assert not coord._global_shutdown_flag.exists()


class TestHandleLocalShutdownDrainLog:
    """The drain branch logs `⏳  Draining...` before delegating to
    `_drain_all_groups` (lines 548-549)."""

    @pytest.mark.unit
    def test_drain_is_invoked_when_configured(self, tmp_path):
        config = _coord_config(tmp_path)
        config.local_shutdown.drain_on_local_shutdown = True
        coord = MultiUPSCoordinator(config)
        log = []
        coord._log = log.append
        coord._drain_all_groups = MagicMock()

        coord._handle_local_shutdown("UPS-A")

        coord._drain_all_groups.assert_called_once()
        assert any("Draining all UPS groups" in m for m in log), log


class TestWaitForCompletionKeyboardInterrupt:
    """`_wait_for_completion` catches KeyboardInterrupt from
    `self._stop_event.wait` and delegates to `_handle_signal`."""

    @pytest.mark.unit
    def test_keyboard_interrupt_routes_to_handle_signal(self, tmp_path):
        import signal as _signal

        coord = MultiUPSCoordinator(_coord_config(tmp_path))
        coord._handle_signal = MagicMock()

        # One alive thread so the wait() is actually entered, then KbI.
        alive_thread = MagicMock()
        alive_thread.is_alive.return_value = True
        coord._threads = [alive_thread]
        coord._evaluator_threads = []

        # Stop event: not set on the first check, then raises on wait().
        stop = MagicMock()
        stop.is_set.return_value = False
        stop.wait.side_effect = KeyboardInterrupt()
        coord._stop_event = stop

        coord._wait_for_completion()

        coord._handle_signal.assert_called_once_with(_signal.SIGINT, None)


class TestHandleSignalDeferredScheduling:
    """Cover the deferred-scheduling and eager-fallback branches of
    `_handle_signal` (lines 707-727)."""

    @pytest.mark.unit
    def test_schedule_called_when_store_and_notif_id_present(self, tmp_path):
        config = _coord_config(tmp_path)
        coord = MultiUPSCoordinator(config)
        coord._log = MagicMock()
        coord._threads = []
        coord._evaluator_threads = []
        coord._redundancy_remote_health_managers = []

        worker = MagicMock()
        worker.send.return_value = 42  # notif_id
        store = MagicMock()
        store.db_path = tmp_path / "fake.db"
        # The handler walks worker._stores under worker._stores_lock.
        worker._stores_lock = threading.Lock()
        worker._stores = [store]
        coord._notification_worker = worker

        with patch("eneru.multi_ups.read_upgrade_marker", return_value=None), \
             patch("eneru.multi_ups.read_shutdown_marker", return_value=None), \
             patch("eneru.multi_ups.write_shutdown_marker"), \
             patch("eneru.multi_ups.schedule_deferred_stop_or_eager_send") as sched, \
             pytest.raises(SystemExit):
            coord._handle_signal(15, None)

        sched.assert_called_once()
        kw = sched.call_args.kwargs
        assert kw["notification_id"] == 42
        assert kw["db_path"] == store.db_path

    @pytest.mark.unit
    def test_eager_apprise_fallback_swallows_exception(self, tmp_path):
        """When `_send_via_apprise_bounded` raises in the no-store fallback
        path, the handler swallows the exception."""
        config = _coord_config(tmp_path)
        coord = MultiUPSCoordinator(config)
        coord._log = MagicMock()
        coord._threads = []
        coord._evaluator_threads = []
        coord._redundancy_remote_health_managers = []

        worker = MagicMock()
        worker.send.return_value = None  # No stores registered → returns None
        worker._stores_lock = threading.Lock()
        worker._stores = []
        worker._send_via_apprise_bounded.side_effect = RuntimeError("apprise gone")
        coord._notification_worker = worker

        with patch("eneru.multi_ups.read_upgrade_marker", return_value=None), \
             patch("eneru.multi_ups.read_shutdown_marker", return_value=None), \
             patch("eneru.multi_ups.write_shutdown_marker"), \
             patch("eneru.multi_ups.schedule_deferred_stop_or_eager_send") as sched, \
             pytest.raises(SystemExit):
            coord._handle_signal(15, None)

        sched.assert_not_called()
        # Eager fallback was attempted (and its exception swallowed).
        worker._send_via_apprise_bounded.assert_called_once()


# ==============================================================================
# COVERAGE: H10 join-deadline, in-flight signal branch, control-event routing
# ==============================================================================

class TestCoordinatorShutdownJoinAndAudit:
    """Cover the rc10 additions: _shutdown_join_deadline, the in-flight signal
    branch, and record_control_event routing."""

    def _cfg(self, tmp_path, *, servers=None, drain=False):
        from types import SimpleNamespace  # noqa: F401 (kept local)
        return Config(
            ups_groups=[UPSGroupConfig(
                ups=UPSConfig(name="UPS1"), is_local=True,
                remote_servers=servers or [],
            )],
            logging=LoggingConfig(
                state_file=str(tmp_path / "state"),
                battery_history_file=str(tmp_path / "history"),
                shutdown_flag_file=str(tmp_path / "flag"),
            ),
            local_shutdown=LocalShutdownConfig(
                enabled=True, drain_on_local_shutdown=drain),
        )

    @pytest.mark.unit
    def test_shutdown_join_deadline_from_remote_budget(self, tmp_path):
        """H10: budget = max remote (cmd+connect+margin) + 120 drain headroom."""
        srv = RemoteServerConfig(
            name="nas", host="10.0.0.1", user="root",
            command_timeout=20, connect_timeout=10, shutdown_safety_margin=60)
        coord = MultiUPSCoordinator(self._cfg(tmp_path, servers=[srv]))
        assert coord._shutdown_join_deadline() == 20 + 10 + 60 + 120

    @pytest.mark.unit
    def test_shutdown_join_deadline_capped_and_floored(self, tmp_path):
        """H10: the deadline is clamped to [30, 600]."""
        big = RemoteServerConfig(
            name="nas", host="10.0.0.1", user="root",
            command_timeout=9999, connect_timeout=0, shutdown_safety_margin=0)
        coord = MultiUPSCoordinator(self._cfg(tmp_path, servers=[big], drain=True))
        assert coord._shutdown_join_deadline() == 600  # capped
        # No remote servers -> just the 120 headroom (above the 30 floor).
        coord2 = MultiUPSCoordinator(self._cfg(tmp_path))
        assert coord2._shutdown_join_deadline() == 120

    @pytest.mark.unit
    def test_shutdown_join_deadline_includes_redundancy_and_pre_commands(self, tmp_path):
        """cubic: the budget must count redundancy-group remotes AND pre-shutdown
        command runtime, not just per-UPS final commands."""
        from eneru import RedundancyGroupConfig, RemoteCommandConfig
        rg_srv = RemoteServerConfig(
            name="rnas", host="10.0.0.9", user="root",
            command_timeout=10, connect_timeout=5, shutdown_safety_margin=20,
            pre_shutdown_commands=[RemoteCommandConfig(command="echo x", timeout=40)],
        )
        config = Config(
            ups_groups=[UPSGroupConfig(ups=UPSConfig(name="UPS1"), is_local=True)],
            redundancy_groups=[RedundancyGroupConfig(
                name="rg", ups_sources=["UPS1"], remote_servers=[rg_srv])],
            logging=LoggingConfig(
                state_file=str(tmp_path / "s"),
                battery_history_file=str(tmp_path / "h"),
                shutdown_flag_file=str(tmp_path / "f")),
        )
        coord = MultiUPSCoordinator(config)
        # rg_srv: pre(40) + cmd(10) + connect(5) + margin(20) = 75; + 120 headroom.
        assert coord._shutdown_join_deadline() == 75 + 120

    @pytest.mark.unit
    def test_shutdown_join_deadline_ignores_non_int_timeout(self, tmp_path):
        """A server with a non-int timeout is skipped in the budget calc, not
        crashed (defensive int() guard)."""
        bad = RemoteServerConfig(
            name="bad", host="10.0.0.1", user="root",
            command_timeout="oops", connect_timeout=0, shutdown_safety_margin=0)
        coord = MultiUPSCoordinator(self._cfg(tmp_path, servers=[bad]))
        # bad server contributes nothing -> just the 120 drain headroom.
        assert coord._shutdown_join_deadline() == 120

    @pytest.mark.unit
    def test_clear_global_shutdown_flag_logs_unlink_errors(self, tmp_path):
        coord = MultiUPSCoordinator(self._cfg(tmp_path))
        logs = []
        coord._log = logs.append

        with patch.object(
            type(coord._global_shutdown_flag),
            "unlink",
            side_effect=OSError("read-only"),
        ):
            coord._clear_global_shutdown_flag("test")

        assert any("Could not clear global shutdown flag" in m for m in logs)

    @pytest.mark.unit
    def test_global_shutdown_guard_logs_exists_errors(self, tmp_path):
        coord = MultiUPSCoordinator(self._cfg(tmp_path))
        logs = []
        coord._log = logs.append

        with patch.object(
            type(coord._global_shutdown_flag),
            "exists",
            side_effect=OSError("permission denied"),
        ):
            assert coord._global_shutdown_guard_active() is False

        assert any("Could not inspect global shutdown flag" in m for m in logs)

    @pytest.mark.unit
    def test_monitor_shutdown_guard_uses_monitor_helper(self, tmp_path):
        coord = MultiUPSCoordinator(self._cfg(tmp_path))

        class MonitorWithGuard:
            def _shutdown_guard_active(self):
                return True

        assert coord._monitor_shutdown_guard_active(MonitorWithGuard()) is True

    @pytest.mark.unit
    def test_monitor_shutdown_guard_falls_back_after_helper_error(self, tmp_path):
        coord = MultiUPSCoordinator(self._cfg(tmp_path))
        logs = []
        coord._log = logs.append

        class MonitorWithBrokenGuard:
            def __init__(self):
                self._shutdown_in_progress = True

            def _shutdown_guard_active(self):
                raise RuntimeError("boom")

        assert coord._monitor_shutdown_guard_active(MonitorWithBrokenGuard()) is True
        assert any("Could not inspect monitor shutdown guard" in m for m in logs)

    @pytest.mark.unit
    def test_monitor_shutdown_guard_handles_missing_or_unreadable_flag(self, tmp_path):
        coord = MultiUPSCoordinator(self._cfg(tmp_path))
        logs = []
        coord._log = logs.append

        class MonitorWithoutFlag:
            _shutdown_in_progress = False

        assert coord._monitor_shutdown_guard_active(MonitorWithoutFlag()) is False

        mon = MonitorWithoutFlag()
        mon._shutdown_flag_path = tmp_path / "flag-ups"
        with patch.object(
            type(mon._shutdown_flag_path),
            "exists",
            side_effect=OSError("permission denied"),
        ):
            assert coord._monitor_shutdown_guard_active(mon) is False

        assert any("Could not inspect monitor shutdown flag" in m for m in logs)

    @pytest.mark.unit
    def test_inflight_recovery_defers_rearm(self, tmp_path):
        """cubic P1: a recovery that races the in-flight window does not drop the
        re-arm -- _clear_local_shutdown_state defers it, and the finally of
        _handle_local_shutdown applies it so the guard isn't left stuck after a
        non-halting shutdown."""
        config = Config(
            ups_groups=[UPSGroupConfig(ups=UPSConfig(name="UPS1"), is_local=True)],
            logging=LoggingConfig(
                state_file=str(tmp_path / "state"),
                battery_history_file=str(tmp_path / "history"),
                shutdown_flag_file=str(tmp_path / "flag"),
            ),
            local_shutdown=LocalShutdownConfig(
                enabled=False, drain_on_local_shutdown=True),
        )
        coord = MultiUPSCoordinator(config)
        coord._log = lambda m: None
        coord._notification_worker = None
        coord._monitors = []
        coord._threads = []

        seen = {}

        def fake_drain(timeout=120):
            # A concurrent OB->OL recovery fires while the shutdown is in flight.
            coord._clear_local_shutdown_state()
            seen["initiated_midflight"] = coord._local_shutdown_initiated
            seen["rearm_pending"] = coord._rearm_after_inflight

        coord._drain_all_groups = fake_drain
        coord._handle_local_shutdown("UPS1")

        # Mid-flight: the guard was NOT cleared (no second-poweroff window)...
        assert seen["initiated_midflight"] is True
        assert seen["rearm_pending"] is True
        # ...but the deferred re-arm fired in the finally, so we're not stuck.
        assert coord._local_shutdown_initiated is False
        assert coord._rearm_after_inflight is False

    @pytest.mark.unit
    def test_handle_signal_in_flight_waits_longer(self, tmp_path):
        """H10: with a shutdown already in flight, the signal handler logs the
        longer bounded wait instead of the brisk 5s exit."""
        coord = MultiUPSCoordinator(self._cfg(tmp_path))
        logs = []
        coord._log = lambda m: logs.append(m)
        coord._notification_worker = None
        coord._threads = []
        coord._evaluator_threads = []
        coord._global_shutdown_flag.touch()  # shutdown in flight
        with patch("eneru.multi_ups.read_upgrade_marker", return_value=None), \
             patch("eneru.multi_ups.read_shutdown_marker", return_value=None), \
             patch("eneru.multi_ups.write_shutdown_marker"), \
             patch("eneru.multi_ups.sys.exit"):
            coord._handle_signal(15, None)
        assert any("Shutdown sequence in progress" in m for m in logs), logs

    @pytest.mark.unit
    def test_handle_signal_in_memory_in_flight_waits_longer(self, tmp_path):
        """If the global flag write failed, the in-memory in-flight bit still
        protects the host poweroff thread from a short signal teardown."""
        coord = MultiUPSCoordinator(self._cfg(tmp_path))
        logs = []
        coord._log = lambda m: logs.append(m)
        coord._notification_worker = None
        coord._threads = []
        coord._evaluator_threads = []
        coord._global_shutdown_flag.unlink(missing_ok=True)
        with coord._local_shutdown_lock:
            coord._local_shutdown_in_flight = True
        with patch("eneru.multi_ups.read_upgrade_marker", return_value=None), \
             patch("eneru.multi_ups.read_shutdown_marker", return_value=None), \
             patch("eneru.multi_ups.write_shutdown_marker"), \
             patch("eneru.multi_ups.sys.exit"):
            coord._handle_signal(15, None)
        assert any("Shutdown sequence in progress" in m for m in logs), logs

    @pytest.mark.unit
    def test_handle_signal_reentrancy_guard(self, tmp_path):
        """L5: a second signal during teardown is ignored."""
        coord = MultiUPSCoordinator(self._cfg(tmp_path))
        coord._log = lambda m: None
        coord._notification_worker = None
        coord._threads = []
        coord._evaluator_threads = []
        coord._signal_handling = True  # pretend a handler is already running
        # Must return immediately without raising SystemExit.
        coord._handle_signal(15, None)

    @pytest.mark.unit
    def test_record_control_event_routes_to_matching_store(self, tmp_path):
        """Audit groundwork: a control event lands in the matching UPS's store."""
        from types import SimpleNamespace
        coord = MultiUPSCoordinator(self._cfg(tmp_path))
        mon = MagicMock()
        mon.config.ups_groups = [SimpleNamespace(ups=SimpleNamespace(name="UPS1"))]
        store = MagicMock()
        mon._stats_store = store
        coord._monitors = [mon]
        coord.record_control_event("UPS1", "CONTROL_COMMAND", "ran beeper.toggle")
        store.log_event.assert_called_once_with("CONTROL_COMMAND", "ran beeper.toggle")

    @pytest.mark.unit
    def test_record_control_event_falls_back_to_first_store(self, tmp_path):
        """Unknown/reload UPS name -> first monitor's store."""
        from types import SimpleNamespace
        coord = MultiUPSCoordinator(self._cfg(tmp_path))
        mon = MagicMock()
        mon.config.ups_groups = [SimpleNamespace(ups=SimpleNamespace(name="UPS1"))]
        store = MagicMock()
        mon._stats_store = store
        coord._monitors = [mon]
        coord.record_control_event("", "CONFIG_RELOAD", "reloaded")
        store.log_event.assert_called_once_with("CONFIG_RELOAD", "reloaded")
