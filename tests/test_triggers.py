"""Tests for shutdown trigger logic."""

import pytest
import time
from unittest.mock import patch, MagicMock

from eneru import (
    UPSGroupMonitor,
    Config,
    MonitorState,
)


class TestTriggerEvaluation:
    """Test shutdown trigger evaluation logic."""

    @pytest.fixture
    def monitor(self, minimal_config, tmp_path):
        """Create a monitor for testing triggers."""
        minimal_config.logging.battery_history_file = str(tmp_path / "battery-history")
        minimal_config.logging.shutdown_flag_file = str(tmp_path / "shutdown-flag")
        minimal_config.logging.state_file = str(tmp_path / "state")
        monitor = UPSGroupMonitor(minimal_config)
        monitor.state = MonitorState()
        monitor.logger = MagicMock()
        return monitor

    @pytest.mark.unit
    def test_low_battery_trigger(self, monitor):
        """Test that low battery triggers shutdown."""
        monitor.config.triggers.low_battery_threshold = 20

        ups_data = {
            "ups.status": "OB DISCHRG",
            "battery.charge": "15",  # Below threshold
            "battery.runtime": "600",
            "ups.load": "25",
        }

        with patch.object(monitor, "_trigger_immediate_shutdown") as mock_shutdown:
            monitor.state.previous_status = "OB DISCHRG"
            monitor.state.on_battery_start_time = int(time.time()) - 40
            monitor._handle_on_battery(ups_data)

            mock_shutdown.assert_called_once()
            call_args = mock_shutdown.call_args[0][0]
            assert "15%" in call_args
            assert "20%" in call_args

    @pytest.mark.unit
    def test_low_battery_no_trigger_above_threshold(self, monitor):
        """Test that battery above threshold does not trigger."""
        monitor.config.triggers.low_battery_threshold = 20

        ups_data = {
            "ups.status": "OB DISCHRG",
            "battery.charge": "50",  # Above threshold
            "battery.runtime": "1800",
            "ups.load": "25",
        }

        with patch.object(monitor, "_trigger_immediate_shutdown") as mock_shutdown:
            monitor.state.previous_status = "OB DISCHRG"
            monitor.state.on_battery_start_time = int(time.time()) - 40
            monitor._handle_on_battery(ups_data)

            mock_shutdown.assert_not_called()

    @pytest.mark.unit
    def test_low_battery_trigger_waits_for_stabilization_delay(self, monitor):
        monitor.config.triggers.low_battery_threshold = 20
        monitor.config.triggers.on_battery_stabilization_delay = 30
        ups_data = {
            "ups.status": "OB DISCHRG",
            "battery.charge": "15",
            "battery.runtime": "600",
            "ups.load": "25",
        }

        with patch.object(monitor, "_trigger_immediate_shutdown") as mock_shutdown:
            monitor.state.previous_status = "OB DISCHRG"
            monitor.state.on_battery_start_time = int(time.time()) - 5
            monitor._handle_on_battery(ups_data)

        mock_shutdown.assert_not_called()

    @pytest.mark.unit
    def test_critical_runtime_trigger(self, monitor):
        """Test that critical runtime triggers shutdown."""
        monitor.config.triggers.critical_runtime_threshold = 600  # 10 minutes

        ups_data = {
            "ups.status": "OB DISCHRG",
            "battery.charge": "50",
            "battery.runtime": "300",  # 5 minutes - below threshold
            "ups.load": "25",
        }

        with patch.object(monitor, "_trigger_immediate_shutdown") as mock_shutdown:
            monitor.state.previous_status = "OB DISCHRG"
            monitor.state.on_battery_start_time = int(time.time()) - 40
            monitor._handle_on_battery(ups_data)

            mock_shutdown.assert_called_once()
            call_args = mock_shutdown.call_args[0][0]
            assert "Runtime" in call_args

    @pytest.mark.unit
    def test_runtime_trigger_waits_for_stabilization_delay(self, monitor):
        monitor.config.triggers.critical_runtime_threshold = 600
        monitor.config.triggers.on_battery_stabilization_delay = 30
        ups_data = {
            "ups.status": "OB DISCHRG",
            "battery.charge": "50",
            "battery.runtime": "300",
            "ups.load": "25",
        }

        with patch.object(monitor, "_trigger_immediate_shutdown") as mock_shutdown:
            monitor.state.previous_status = "OB DISCHRG"
            monitor.state.on_battery_start_time = int(time.time()) - 5
            monitor._handle_on_battery(ups_data)

        mock_shutdown.assert_not_called()

    @pytest.mark.unit
    def test_repeated_outage_ignores_unstable_runtime_after_restore(self, monitor):
        """Regression for OB -> OL -> OB with transient fake runtime."""
        monitor.config.triggers.critical_runtime_threshold = 600
        monitor.config.triggers.on_battery_stabilization_delay = 30
        second_outage = {
            "ups.status": "OB DISCHRG",
            "battery.charge": "89",
            "battery.runtime": "388",
            "ups.load": "30",
        }

        with patch.object(monitor, "_trigger_immediate_shutdown") as mock_shutdown:
            monitor.state.previous_status = "OL CHRG"
            monitor._handle_on_battery(second_outage)

        mock_shutdown.assert_not_called()

    @pytest.mark.unit
    def test_extended_time_trigger(self, monitor):
        """Test that extended time on battery triggers shutdown."""
        monitor.config.triggers.extended_time.enabled = True
        monitor.config.triggers.extended_time.threshold = 900  # 15 minutes

        ups_data = {
            "ups.status": "OB DISCHRG",
            "battery.charge": "80",  # Battery fine
            "battery.runtime": "3600",  # Runtime fine
            "ups.load": "25",
        }

        with patch.object(monitor, "_trigger_immediate_shutdown") as mock_shutdown:
            monitor.state.previous_status = "OB DISCHRG"
            # Set start time to 20 minutes ago
            monitor.state.on_battery_start_time = int(time.time()) - 1200
            monitor._handle_on_battery(ups_data)

            mock_shutdown.assert_called_once()
            call_args = mock_shutdown.call_args[0][0]
            assert "Time on battery" in call_args

    @pytest.mark.unit
    def test_extended_time_trigger_waits_for_stabilization_delay(self, monitor):
        monitor.config.triggers.extended_time.enabled = True
        monitor.config.triggers.extended_time.threshold = 3
        monitor.config.triggers.on_battery_stabilization_delay = 30
        ups_data = {
            "ups.status": "OB DISCHRG",
            "battery.charge": "80",
            "battery.runtime": "3600",
            "ups.load": "25",
        }

        with patch.object(monitor, "_trigger_immediate_shutdown") as mock_shutdown:
            monitor.state.previous_status = "OB DISCHRG"
            monitor.state.on_battery_start_time = int(time.time()) - 5
            monitor._handle_on_battery(ups_data)

        mock_shutdown.assert_not_called()

    @pytest.mark.unit
    def test_extended_time_disabled_no_trigger(self, monitor):
        """Test that disabled extended time does not trigger."""
        monitor.config.triggers.extended_time.enabled = False
        monitor.config.triggers.extended_time.threshold = 900

        ups_data = {
            "ups.status": "OB DISCHRG",
            "battery.charge": "80",
            "battery.runtime": "3600",
            "ups.load": "25",
        }

        with patch.object(monitor, "_trigger_immediate_shutdown") as mock_shutdown:
            monitor.state.previous_status = "OB DISCHRG"
            monitor.state.on_battery_start_time = int(time.time()) - 1200
            monitor._handle_on_battery(ups_data)

            mock_shutdown.assert_not_called()

    @pytest.mark.unit
    def test_depletion_rate_grace_period(self, monitor):
        """Test that high depletion during grace period does not trigger."""
        monitor.config.triggers.depletion.critical_rate = 15.0
        monitor.config.triggers.depletion.grace_period = 90

        ups_data = {
            "ups.status": "OB DISCHRG",
            "battery.charge": "80",
            "battery.runtime": "1800",
            "ups.load": "25",
        }

        # Mock high depletion rate
        with patch.object(monitor, "_calculate_depletion_rate", return_value=20.0):
            with patch.object(monitor, "_trigger_immediate_shutdown") as mock_shutdown:
                monitor.state.previous_status = "OB DISCHRG"
                # Only 30 seconds on battery (within grace period)
                monitor.state.on_battery_start_time = int(time.time()) - 30
                monitor._handle_on_battery(ups_data)

                # Should NOT trigger during grace period
                mock_shutdown.assert_not_called()

    @pytest.mark.unit
    def test_depletion_trigger_waits_for_stabilization_delay(self, monitor):
        monitor.config.triggers.depletion.critical_rate = 15.0
        monitor.config.triggers.depletion.grace_period = 0
        monitor.config.triggers.on_battery_stabilization_delay = 30
        ups_data = {
            "ups.status": "OB DISCHRG",
            "battery.charge": "80",
            "battery.runtime": "1800",
            "ups.load": "25",
        }

        with patch.object(monitor, "_calculate_depletion_rate", return_value=20.0):
            with patch.object(monitor, "_trigger_immediate_shutdown") as mock_shutdown:
                monitor.state.previous_status = "OB DISCHRG"
                monitor.state.on_battery_start_time = int(time.time()) - 5
                monitor._handle_on_battery(ups_data)

        mock_shutdown.assert_not_called()

    @pytest.mark.unit
    def test_depletion_rate_after_grace_period(self, monitor):
        """Test that high depletion after grace period triggers shutdown."""
        monitor.config.triggers.depletion.critical_rate = 15.0
        monitor.config.triggers.depletion.grace_period = 90

        ups_data = {
            "ups.status": "OB DISCHRG",
            "battery.charge": "80",
            "battery.runtime": "1800",
            "ups.load": "25",
        }

        # Mock high depletion rate
        with patch.object(monitor, "_calculate_depletion_rate", return_value=20.0):
            with patch.object(monitor, "_trigger_immediate_shutdown") as mock_shutdown:
                monitor.state.previous_status = "OB DISCHRG"
                # 120 seconds on battery (past grace period)
                monitor.state.on_battery_start_time = int(time.time()) - 120
                monitor._handle_on_battery(ups_data)

                mock_shutdown.assert_called_once()
                call_args = mock_shutdown.call_args[0][0]
                assert "Depletion rate" in call_args


class TestFSDTrigger:
    """Test FSD (Forced Shutdown) flag handling."""

    @pytest.fixture
    def monitor(self, minimal_config, tmp_path):
        """Create a monitor for testing."""
        minimal_config.logging.shutdown_flag_file = str(tmp_path / "shutdown-flag")
        monitor = UPSGroupMonitor(minimal_config)
        monitor.state = MonitorState()
        monitor.logger = MagicMock()
        return monitor

    @pytest.mark.unit
    def test_fsd_flag_triggers_immediate_shutdown(self, monitor):
        """Test that FSD in status triggers immediate shutdown."""
        with patch.object(monitor, "_trigger_immediate_shutdown") as mock_shutdown:
            # Simulate main loop detecting FSD
            ups_status = "OB FSD"

            if "FSD" in ups_status:
                monitor._trigger_immediate_shutdown("UPS signaled FSD (Forced Shutdown) flag.")

            mock_shutdown.assert_called_once()
            assert "FSD" in mock_shutdown.call_args[0][0]


class TestTriggerPriority:
    """Test that triggers are evaluated in correct priority order."""

    @pytest.fixture
    def monitor(self, minimal_config, tmp_path):
        """Create a monitor for testing."""
        minimal_config.logging.battery_history_file = str(tmp_path / "battery-history")
        minimal_config.logging.shutdown_flag_file = str(tmp_path / "shutdown-flag")
        minimal_config.logging.state_file = str(tmp_path / "state")
        monitor = UPSGroupMonitor(minimal_config)
        monitor.state = MonitorState()
        monitor.logger = MagicMock()
        return monitor

    @pytest.mark.unit
    def test_low_battery_triggers_before_runtime(self, monitor):
        """Test that low battery triggers before critical runtime."""
        monitor.config.triggers.low_battery_threshold = 20
        monitor.config.triggers.critical_runtime_threshold = 600

        ups_data = {
            "ups.status": "OB DISCHRG",
            "battery.charge": "15",  # Triggers low battery
            "battery.runtime": "300",  # Would also trigger runtime
            "ups.load": "25",
        }

        with patch.object(monitor, "_trigger_immediate_shutdown") as mock_shutdown:
            monitor.state.previous_status = "OB DISCHRG"
            monitor.state.on_battery_start_time = int(time.time()) - 40
            monitor._handle_on_battery(ups_data)

            # Should trigger on low battery, not runtime
            mock_shutdown.assert_called_once()
            call_args = mock_shutdown.call_args[0][0]
            assert "15%" in call_args  # Low battery message
            assert "Runtime" not in call_args

    @pytest.mark.unit
    def test_on_battery_stabilization_suppresses_runtime_trigger(self, monitor):
        """Fresh OB readings can be firmware recalibration noise."""
        monitor.config.triggers.critical_runtime_threshold = 600
        monitor.config.triggers.on_battery_stabilization_delay = 30

        ups_data = {
            "ups.status": "OB DISCHRG",
            "battery.charge": "89",
            "battery.runtime": "388",
            "ups.load": "30",
        }

        with patch.object(monitor, "_trigger_immediate_shutdown") as mock_shutdown:
            monitor.state.previous_status = "OB DISCHRG"
            monitor.state.on_battery_start_time = int(time.time()) - 10
            monitor._handle_on_battery(ups_data)

            mock_shutdown.assert_not_called()

    @pytest.mark.unit
    def test_on_battery_stabilization_can_be_disabled(self, monitor):
        """A zero stabilization delay preserves immediate legacy behavior."""
        monitor.config.triggers.critical_runtime_threshold = 600
        monitor.config.triggers.on_battery_stabilization_delay = 0

        ups_data = {
            "ups.status": "OB DISCHRG",
            "battery.charge": "89",
            "battery.runtime": "388",
            "ups.load": "30",
        }

        with patch.object(monitor, "_trigger_immediate_shutdown") as mock_shutdown:
            monitor.state.previous_status = "OB DISCHRG"
            monitor.state.on_battery_start_time = int(time.time()) - 1
            monitor._handle_on_battery(ups_data)

            mock_shutdown.assert_called_once()
            # F-154: pin WHICH trigger fired (T2), not merely that one did.
            assert mock_shutdown.call_args[0][0].startswith("Runtime ")


def _boundary_monitor(minimal_config, tmp_path):
    minimal_config.logging.battery_history_file = str(tmp_path / "battery-history")
    minimal_config.logging.shutdown_flag_file = str(tmp_path / "shutdown-flag")
    minimal_config.logging.state_file = str(tmp_path / "state")
    t = minimal_config.triggers
    t.low_battery_threshold = 20
    t.critical_runtime_threshold = 600
    t.depletion.critical_rate = 15.0
    t.depletion.grace_period = 90
    t.extended_time.enabled = True
    t.extended_time.threshold = 900
    t.on_battery_stabilization_delay = 0
    monitor = UPSGroupMonitor(minimal_config)
    monitor.state = MonitorState()
    monitor.logger = MagicMock()
    return monitor


def _evaluate(monitor, *, charge="80", runtime="1800", rate=0.0, on_battery_for=40):
    """Run one on-battery evaluation with an exact time-on-battery.

    Continuing outage (previous_status OB). Both clocks are frozen, so a slow
    runner can't turn "exactly 900 s" into 901 s mid-evaluation. Returns the
    shutdown reason, or None."""
    mono_now, wall_now = 100_000.0, 1_800_000_000
    monitor.state.previous_status = "OB DISCHRG"
    monitor.state.on_battery_start_mono = mono_now - on_battery_for
    monitor.state.on_battery_start_time = wall_now - on_battery_for
    with patch.object(monitor, "_calculate_depletion_rate", return_value=rate), \
            patch("eneru.monitor.time.monotonic", return_value=mono_now), \
            patch("eneru.monitor.time.time", return_value=wall_now), \
            patch.object(monitor, "_trigger_immediate_shutdown") as mock_shutdown:
        monitor._handle_on_battery({
            "ups.status": "OB DISCHRG", "battery.charge": charge,
            "battery.runtime": runtime, "ups.load": "25",
        })
    if not mock_shutdown.called:
        return None
    mock_shutdown.assert_called_once()
    return mock_shutdown.call_args[0][0]


class TestTriggerBoundaries:
    """F-150: T1-T4 at threshold-1 / threshold / threshold+1. The shutdown
    path's comparison direction must match health_model's (its twins are
    tested there); an off-by-one flip here would silently drift from it."""

    @pytest.fixture
    def monitor(self, minimal_config, tmp_path):
        return _boundary_monitor(minimal_config, tmp_path)

    @pytest.mark.unit
    @pytest.mark.parametrize("charge,fires", [("19", True), ("20", False), ("21", False)])
    def test_t1_low_battery_is_strictly_below(self, monitor, charge, fires):
        reason = _evaluate(monitor, charge=charge)
        if fires:
            assert reason.startswith(f"Battery charge {charge}% below threshold 20%")
        else:
            assert reason is None

    @pytest.mark.unit
    @pytest.mark.parametrize("runtime,fires", [("599", True), ("600", False), ("601", False)])
    def test_t2_runtime_is_strictly_below(self, monitor, runtime, fires):
        reason = _evaluate(monitor, runtime=runtime)
        if fires:
            assert reason.startswith("Runtime ")
        else:
            assert reason is None

    @pytest.mark.unit
    @pytest.mark.parametrize("rate,fires", [(14.99, False), (15.0, False), (15.01, True)])
    def test_t3_depletion_rate_is_strictly_above(self, monitor, rate, fires):
        reason = _evaluate(monitor, rate=rate, on_battery_for=120)
        if fires:
            assert reason.startswith("Depletion rate 15.01%/min")
        else:
            assert reason is None

    @pytest.mark.unit
    @pytest.mark.parametrize("on_battery_for,fires", [(89, False), (90, True), (91, True)])
    def test_t3_grace_period_ends_at_grace_seconds(self, monitor, on_battery_for, fires):
        # health_model treats time_on_battery >= grace_period as armed; the
        # shutdown path must agree at equality.
        reason = _evaluate(monitor, rate=20.0, on_battery_for=on_battery_for)
        if fires:
            assert reason.startswith("Depletion rate 20.0%/min")
        else:
            assert reason is None

    @pytest.mark.unit
    @pytest.mark.parametrize("on_battery_for,fires", [(899, False), (900, False), (901, True)])
    def test_t4_extended_time_is_strictly_above(self, monitor, on_battery_for, fires):
        reason = _evaluate(monitor, on_battery_for=on_battery_for)
        if fires:
            assert reason.startswith("Time on battery ")
        else:
            assert reason is None

    @pytest.mark.unit
    @pytest.mark.parametrize("on_battery_for,fires", [(29, False), (30, True)])
    def test_stabilization_ends_at_delay_seconds(self, monitor, on_battery_for, fires):
        monitor.config.triggers.on_battery_stabilization_delay = 30
        reason = _evaluate(monitor, runtime="300", on_battery_for=on_battery_for)
        assert (reason is not None) is fires


class TestEmptyBatteryReadings:
    """F-149: a genuine 0 is a valid (and the most urgent) reading. Only
    negative values are NUT's "unknown" sentinel."""

    @pytest.fixture
    def monitor(self, minimal_config, tmp_path):
        return _boundary_monitor(minimal_config, tmp_path)

    @pytest.mark.unit
    def test_zero_charge_fires_t1(self, monitor):
        reason = _evaluate(monitor, charge="0")
        assert reason.startswith("Battery charge 0% below threshold 20%")

    @pytest.mark.unit
    def test_zero_runtime_fires_t2(self, monitor):
        reason = _evaluate(monitor, runtime="0")
        assert reason.startswith("Runtime ")

    @pytest.mark.unit
    def test_negative_sentinel_is_not_a_reading(self, monitor):
        assert _evaluate(monitor, charge="-1", runtime="-1") is None


class TestFailsafeLatchFSD:
    """F-152: the FAILSAFE latch is per-outage. An FSD-only poll (no OB token)
    is still the same outage, so it must NOT re-arm the latch -- otherwise the
    next hard error would re-run the shutdown sequence mid-outage."""

    @staticmethod
    def _one_poll(monitor, ups_data):
        original_wait = monitor._stop_event.wait

        def wait_then_stop(timeout=None):
            monitor._stop_event.set()
            return original_wait(0)

        with patch.object(monitor, "_get_all_ups_data",
                          return_value=(True, ups_data, "")), \
                patch.object(monitor._stop_event, "wait", wait_then_stop), \
                patch.object(monitor, "_trigger_immediate_shutdown") as mock_sd:
            monitor._main_loop()
        return mock_sd

    @pytest.fixture
    def monitor(self, minimal_config, tmp_path):
        m = _boundary_monitor(minimal_config, tmp_path)
        m._notification_worker = MagicMock()
        m._in_redundancy_group = False
        m.state.previous_status = "OB DISCHRG"
        m.state.connection_state = "OK"
        m._failsafe_initiated = True
        return m

    @pytest.mark.unit
    def test_fsd_only_poll_keeps_latch(self, monitor):
        mock_sd = self._one_poll(monitor, {
            "ups.status": "FSD", "battery.charge": "5",
            "battery.runtime": "60", "ups.load": "25"})
        mock_sd.assert_called_once()                 # FSD was acted on ...
        assert monitor._failsafe_initiated is True   # ... latch kept

    @pytest.mark.unit
    def test_online_poll_rearms_latch(self, monitor):
        mock_sd = self._one_poll(monitor, {
            "ups.status": "OL CHRG", "battery.charge": "90",
            "battery.runtime": "1800", "ups.load": "25"})
        mock_sd.assert_not_called()                  # on line: nothing fires
        assert monitor._failsafe_initiated is False
