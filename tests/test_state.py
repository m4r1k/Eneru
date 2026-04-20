"""Tests for state tracking and transitions."""

import pytest
import threading
import time
from unittest.mock import patch, MagicMock

from eneru import (
    UPSGroupMonitor,
    MonitorState,
)
from eneru.state import HealthSnapshot


class TestStateTransitions:
    """Test state transition handling."""

    @pytest.fixture
    def monitor(self, minimal_config, tmp_path):
        """Create a monitor for testing."""
        minimal_config.logging.battery_history_file = str(tmp_path / "battery-history")
        minimal_config.logging.shutdown_flag_file = str(tmp_path / "shutdown-flag")
        minimal_config.logging.state_file = str(tmp_path / "state")
        monitor = UPSGroupMonitor(minimal_config)
        monitor.state = MonitorState()
        monitor.logger = MagicMock()
        monitor._notification_worker = MagicMock()
        return monitor

    @pytest.mark.unit
    def test_transition_to_on_battery(self, monitor):
        """Test transition from online to on battery."""
        monitor.state.previous_status = "OL CHRG"

        ups_data = {
            "ups.status": "OB DISCHRG",
            "battery.charge": "100",
            "battery.runtime": "1800",
            "ups.load": "25",
        }

        with patch.object(monitor, "_log_power_event") as mock_log:
            with patch("eneru.monitor.run_command", return_value=(0, "", "")):
                monitor._handle_on_battery(ups_data)

                mock_log.assert_called_once()
                call_args = mock_log.call_args
                assert call_args[0][0] == "ON_BATTERY"

    @pytest.mark.unit
    def test_transition_to_online(self, monitor):
        """Test transition from on battery to online."""
        monitor.state.previous_status = "OB DISCHRG"
        monitor.state.on_battery_start_time = int(time.time()) - 120  # 2 minutes ago

        ups_data = {
            "ups.status": "OL CHRG",
            "battery.charge": "85",
            "input.voltage": "230.5",
        }

        with patch.object(monitor, "_log_power_event") as mock_log:
            with patch("eneru.monitor.run_command", return_value=(0, "", "")):
                monitor._handle_on_line(ups_data)

                mock_log.assert_called_once()
                call_args = mock_log.call_args
                assert call_args[0][0] == "POWER_RESTORED"
                assert "2m" in call_args[0][1]  # Outage duration

    @pytest.mark.unit
    def test_on_battery_start_time_set(self, monitor):
        """Test that on_battery_start_time is set on transition."""
        monitor.state.previous_status = "OL CHRG"
        monitor.state.on_battery_start_time = 0

        ups_data = {
            "ups.status": "OB DISCHRG",
            "battery.charge": "100",
            "battery.runtime": "1800",
            "ups.load": "25",
        }

        before_time = int(time.time())

        with patch.object(monitor, "_log_power_event"):
            with patch("eneru.monitor.run_command", return_value=(0, "", "")):
                monitor._handle_on_battery(ups_data)

        after_time = int(time.time())

        assert before_time <= monitor.state.on_battery_start_time <= after_time

    @pytest.mark.unit
    def test_battery_history_cleared_on_power_restore(self, monitor):
        """Test that battery history is cleared when power is restored."""
        monitor.state.previous_status = "OB DISCHRG"
        monitor.state.on_battery_start_time = int(time.time()) - 60
        monitor.state.battery_history.append((int(time.time()), 95))
        monitor.state.battery_history.append((int(time.time()), 90))

        ups_data = {
            "ups.status": "OL CHRG",
            "battery.charge": "85",
            "input.voltage": "230.5",
        }

        with patch.object(monitor, "_log_power_event"):
            with patch("eneru.monitor.run_command", return_value=(0, "", "")):
                monitor._handle_on_line(ups_data)

        assert len(monitor.state.battery_history) == 0
        assert monitor.state.on_battery_start_time == 0


class TestVoltageStateTracking:
    """Test voltage state tracking."""

    @pytest.fixture
    def monitor(self, minimal_config, tmp_path):
        """Create a monitor for testing."""
        minimal_config.logging.shutdown_flag_file = str(tmp_path / "shutdown-flag")
        monitor = UPSGroupMonitor(minimal_config)
        monitor.state = MonitorState()
        monitor.state.voltage_warning_low = 200.0
        monitor.state.voltage_warning_high = 250.0
        monitor.logger = MagicMock()
        monitor._notification_worker = MagicMock()
        return monitor

    @pytest.mark.unit
    def test_brownout_detection(self, monitor):
        """Test brownout (low voltage) detection."""
        monitor.state.voltage_state = "NORMAL"

        with patch.object(monitor, "_log_power_event") as mock_log:
            monitor._check_voltage_issues("OL", "190")  # Below 200V threshold

            mock_log.assert_called_once()
            assert mock_log.call_args[0][0] == "BROWNOUT_DETECTED"
            assert monitor.state.voltage_state == "LOW"

    @pytest.mark.unit
    def test_over_voltage_detection(self, monitor):
        """Test over-voltage detection."""
        monitor.state.voltage_state = "NORMAL"

        with patch.object(monitor, "_log_power_event") as mock_log:
            monitor._check_voltage_issues("OL", "260")  # Above 250V threshold

            mock_log.assert_called_once()
            assert mock_log.call_args[0][0] == "OVER_VOLTAGE_DETECTED"
            assert monitor.state.voltage_state == "HIGH"

    @pytest.mark.unit
    def test_voltage_normalized(self, monitor):
        """Test voltage normalization detection."""
        monitor.state.voltage_state = "LOW"

        with patch.object(monitor, "_log_power_event") as mock_log:
            monitor._check_voltage_issues("OL", "225")  # Normal voltage

            mock_log.assert_called_once()
            assert mock_log.call_args[0][0] == "VOLTAGE_NORMALIZED"
            assert monitor.state.voltage_state == "NORMAL"

    @pytest.mark.unit
    def test_no_voltage_check_on_battery(self, monitor):
        """Test that voltage is not checked when on battery."""
        monitor.state.voltage_state = "NORMAL"

        with patch.object(monitor, "_log_power_event") as mock_log:
            # On battery status - input voltage doesn't matter
            monitor._check_voltage_issues("OB DISCHRG", "0")

            mock_log.assert_not_called()


class TestAVRStateTracking:
    """Test AVR (Automatic Voltage Regulation) state tracking."""

    @pytest.fixture
    def monitor(self, minimal_config, tmp_path):
        """Create a monitor for testing."""
        minimal_config.logging.shutdown_flag_file = str(tmp_path / "shutdown-flag")
        monitor = UPSGroupMonitor(minimal_config)
        monitor.state = MonitorState()
        monitor.logger = MagicMock()
        monitor._notification_worker = MagicMock()
        return monitor

    @pytest.mark.unit
    def test_avr_boost_detection(self, monitor):
        """Test AVR boost mode detection."""
        monitor.state.avr_state = "INACTIVE"

        with patch.object(monitor, "_log_power_event") as mock_log:
            monitor._check_avr_status("OL BOOST", "210")

            mock_log.assert_called_once()
            assert mock_log.call_args[0][0] == "AVR_BOOST_ACTIVE"
            assert monitor.state.avr_state == "BOOST"

    @pytest.mark.unit
    def test_avr_trim_detection(self, monitor):
        """Test AVR trim mode detection."""
        monitor.state.avr_state = "INACTIVE"

        with patch.object(monitor, "_log_power_event") as mock_log:
            monitor._check_avr_status("OL TRIM", "245")

            mock_log.assert_called_once()
            assert mock_log.call_args[0][0] == "AVR_TRIM_ACTIVE"
            assert monitor.state.avr_state == "TRIM"

    @pytest.mark.unit
    def test_avr_inactive(self, monitor):
        """Test AVR returning to inactive."""
        monitor.state.avr_state = "BOOST"

        with patch.object(monitor, "_log_power_event") as mock_log:
            monitor._check_avr_status("OL", "230")

            mock_log.assert_called_once()
            assert mock_log.call_args[0][0] == "AVR_INACTIVE"
            assert monitor.state.avr_state == "INACTIVE"


class TestBypassStateTracking:
    """Test bypass mode state tracking."""

    @pytest.fixture
    def monitor(self, minimal_config, tmp_path):
        """Create a monitor for testing."""
        minimal_config.logging.shutdown_flag_file = str(tmp_path / "shutdown-flag")
        monitor = UPSGroupMonitor(minimal_config)
        monitor.state = MonitorState()
        monitor.logger = MagicMock()
        monitor._notification_worker = MagicMock()
        return monitor

    @pytest.mark.unit
    def test_bypass_active_detection(self, monitor):
        """Test bypass mode detection."""
        monitor.state.bypass_state = "INACTIVE"

        with patch.object(monitor, "_log_power_event") as mock_log:
            monitor._check_bypass_status("BYPASS")

            mock_log.assert_called_once()
            assert mock_log.call_args[0][0] == "BYPASS_MODE_ACTIVE"
            assert monitor.state.bypass_state == "ACTIVE"

    @pytest.mark.unit
    def test_bypass_inactive(self, monitor):
        """Test bypass mode returning to inactive."""
        monitor.state.bypass_state = "ACTIVE"

        with patch.object(monitor, "_log_power_event") as mock_log:
            monitor._check_bypass_status("OL")

            mock_log.assert_called_once()
            assert mock_log.call_args[0][0] == "BYPASS_MODE_INACTIVE"
            assert monitor.state.bypass_state == "INACTIVE"


class TestOverloadStateTracking:
    """Test overload state tracking."""

    @pytest.fixture
    def monitor(self, minimal_config, tmp_path):
        """Create a monitor for testing."""
        minimal_config.logging.shutdown_flag_file = str(tmp_path / "shutdown-flag")
        monitor = UPSGroupMonitor(minimal_config)
        monitor.state = MonitorState()
        monitor.logger = MagicMock()
        monitor._notification_worker = MagicMock()
        return monitor

    @pytest.mark.unit
    def test_overload_detection(self, monitor):
        """Test overload detection."""
        monitor.state.overload_state = "INACTIVE"

        with patch.object(monitor, "_log_power_event") as mock_log:
            monitor._check_overload_status("OL OVER", "95")

            mock_log.assert_called_once()
            assert mock_log.call_args[0][0] == "OVERLOAD_ACTIVE"
            assert monitor.state.overload_state == "ACTIVE"

    @pytest.mark.unit
    def test_overload_resolved(self, monitor):
        """Test overload resolution detection."""
        monitor.state.overload_state = "ACTIVE"

        with patch.object(monitor, "_log_power_event") as mock_log:
            monitor._check_overload_status("OL", "50")

            mock_log.assert_called_once()
            assert mock_log.call_args[0][0] == "OVERLOAD_RESOLVED"
            assert monitor.state.overload_state == "INACTIVE"


class TestMonitorStateSnapshot:
    """Tests for the snapshot/lock infrastructure used by redundancy groups."""

    @pytest.mark.unit
    def test_lock_attribute_exists(self):
        """``MonitorState`` exposes a non-reentrant Lock under ``_lock``."""
        state = MonitorState()
        # threading.Lock() returns the lock factory; verify it is acquirable.
        assert hasattr(state, "_lock")
        assert state._lock.acquire(blocking=False) is True
        state._lock.release()

    @pytest.mark.unit
    def test_snapshot_defaults(self):
        """A freshly created state snapshots to documented zero defaults."""
        state = MonitorState()
        snap = state.snapshot()
        assert isinstance(snap, HealthSnapshot)
        assert snap.status == ""
        assert snap.battery_charge == ""
        assert snap.runtime == ""
        assert snap.load == ""
        assert snap.depletion_rate == 0.0
        assert snap.time_on_battery == 0
        assert snap.last_update_time == 0.0
        assert snap.connection_state == "OK"
        assert snap.trigger_active is False
        assert snap.trigger_reason == ""

    @pytest.mark.unit
    def test_snapshot_reflects_writes(self):
        """``snapshot()`` returns the latest values written under the lock."""
        state = MonitorState()
        with state._lock:
            state.latest_status = "OB DISCHRG"
            state.latest_battery_charge = "57"
            state.latest_runtime = "920"
            state.latest_load = "42"
            state.latest_depletion_rate = 3.25
            state.latest_time_on_battery = 180
            state.latest_update_time = 1700000000.0
            state.connection_state = "GRACE_PERIOD"
            state.trigger_active = True
            state.trigger_reason = "battery 5% below threshold"

        snap = state.snapshot()
        assert snap.status == "OB DISCHRG"
        assert snap.battery_charge == "57"
        assert snap.runtime == "920"
        assert snap.load == "42"
        assert snap.depletion_rate == 3.25
        assert snap.time_on_battery == 180
        assert snap.last_update_time == 1700000000.0
        assert snap.connection_state == "GRACE_PERIOD"
        assert snap.trigger_active is True
        assert snap.trigger_reason == "battery 5% below threshold"

    @pytest.mark.unit
    def test_snapshot_concurrent_writes_observe_consistent_pairs(self):
        """A reader thread never observes a torn (mid-update) snapshot.

        The writer flips ``latest_status`` and ``trigger_reason`` together
        between two paired sets ("A"/"reason-A", "B"/"reason-B"). The reader
        must always see one matched pair, never a mix.
        """
        state = MonitorState()
        state.latest_status = "A"
        state.trigger_reason = "reason-A"

        stop_flag = threading.Event()

        def writer():
            cycle = 0
            while not stop_flag.is_set():
                with state._lock:
                    if cycle % 2 == 0:
                        state.latest_status = "A"
                        state.trigger_reason = "reason-A"
                    else:
                        state.latest_status = "B"
                        state.trigger_reason = "reason-B"
                cycle += 1

        observed_mixed = []

        def reader():
            for _ in range(2000):
                snap = state.snapshot()
                pair = (snap.status, snap.trigger_reason)
                if pair not in (("A", "reason-A"), ("B", "reason-B")):
                    observed_mixed.append(pair)

        wt = threading.Thread(target=writer)
        rt = threading.Thread(target=reader)
        wt.start()
        rt.start()
        rt.join()
        stop_flag.set()
        wt.join()

        assert observed_mixed == []

    @pytest.mark.unit
    def test_dataclass_equality_unaffected_by_lock(self):
        """Two fresh states compare equal even though each owns its own lock."""
        a = MonitorState()
        b = MonitorState()
        # Distinct lock instances...
        assert a._lock is not b._lock
        # ...but dataclass equality ignores `_lock` (compare=False).
        assert a == b

    @pytest.mark.unit
    def test_dataclass_repr_omits_lock(self):
        """``repr(MonitorState())`` does not surface the lock object."""
        text = repr(MonitorState())
        assert "_lock" not in text
        assert "Lock" not in text

    @pytest.mark.unit
    def test_snapshot_holds_lock_only_briefly(self):
        """``snapshot()`` releases the lock before returning."""
        state = MonitorState()
        snap = state.snapshot()
        # If snapshot() somehow held the lock past return, this would block.
        acquired = state._lock.acquire(blocking=False)
        assert acquired is True
        state._lock.release()
        assert isinstance(snap, HealthSnapshot)

    @pytest.mark.unit
    def test_default_state_round_trip(self):
        """The new fields don't break existing default-state assumptions."""
        state = MonitorState()
        # Existing fields remain at their documented defaults
        assert state.previous_status == ""
        assert state.connection_state == "OK"
        assert state.voltage_state == "NORMAL"
        # New snapshot/trigger fields default to inert values
        assert state.latest_status == ""
        assert state.latest_depletion_rate == 0.0
        assert state.trigger_active is False
        assert state.trigger_reason == ""
