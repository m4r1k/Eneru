"""Tests for shutdown trigger logic."""

import pytest
import time
from unittest.mock import patch, MagicMock

from ups_monitor import (
    UPSMonitor,
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
        monitor = UPSMonitor(minimal_config)
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
            monitor.state.on_battery_start_time = int(time.time()) - 10
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
            monitor.state.on_battery_start_time = int(time.time()) - 10
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
            monitor.state.on_battery_start_time = int(time.time()) - 10
            monitor._handle_on_battery(ups_data)

            mock_shutdown.assert_called_once()
            call_args = mock_shutdown.call_args[0][0]
            assert "Runtime" in call_args

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
        monitor = UPSMonitor(minimal_config)
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
        monitor = UPSMonitor(minimal_config)
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
            monitor.state.on_battery_start_time = int(time.time()) - 10
            monitor._handle_on_battery(ups_data)

            # Should trigger on low battery, not runtime
            mock_shutdown.assert_called_once()
            call_args = mock_shutdown.call_args[0][0]
            assert "15%" in call_args  # Low battery message
            assert "Runtime" not in call_args
