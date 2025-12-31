"""Tests for state tracking and transitions."""

import pytest
import time
from unittest.mock import patch, MagicMock

from eneru import (
    UPSMonitor,
    MonitorState,
)


class TestStateTransitions:
    """Test state transition handling."""

    @pytest.fixture
    def monitor(self, minimal_config, tmp_path):
        """Create a monitor for testing."""
        minimal_config.logging.battery_history_file = str(tmp_path / "battery-history")
        minimal_config.logging.shutdown_flag_file = str(tmp_path / "shutdown-flag")
        minimal_config.logging.state_file = str(tmp_path / "state")
        monitor = UPSMonitor(minimal_config)
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
        monitor = UPSMonitor(minimal_config)
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
        monitor = UPSMonitor(minimal_config)
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
        monitor = UPSMonitor(minimal_config)
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
        monitor = UPSMonitor(minimal_config)
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
