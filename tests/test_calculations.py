"""Tests for calculation functions."""

import pytest
import time
from collections import deque

from eneru import (
    is_numeric,
    format_seconds,
    UPSMonitor,
    Config,
    MonitorState,
)


class TestIsNumeric:
    """Test the is_numeric helper function."""

    @pytest.mark.unit
    def test_integer_is_numeric(self):
        """Test that integers are numeric."""
        assert is_numeric(42) is True
        assert is_numeric(0) is True
        assert is_numeric(-10) is True

    @pytest.mark.unit
    def test_float_is_numeric(self):
        """Test that floats are numeric."""
        assert is_numeric(3.14) is True
        assert is_numeric(0.0) is True
        assert is_numeric(-2.5) is True

    @pytest.mark.unit
    def test_numeric_strings(self):
        """Test that numeric strings are recognized."""
        assert is_numeric("42") is True
        assert is_numeric("3.14") is True
        assert is_numeric("-10") is True
        assert is_numeric("0") is True
        assert is_numeric("100.5") is True

    @pytest.mark.unit
    def test_non_numeric_strings(self):
        """Test that non-numeric strings are rejected."""
        assert is_numeric("hello") is False
        assert is_numeric("") is False
        assert is_numeric("12abc") is False
        assert is_numeric("N/A") is False

    @pytest.mark.unit
    def test_none_is_not_numeric(self):
        """Test that None is not numeric."""
        assert is_numeric(None) is False

    @pytest.mark.unit
    def test_other_types_not_numeric(self):
        """Test that other types are not numeric."""
        assert is_numeric([1, 2, 3]) is False
        assert is_numeric({"a": 1}) is False
        assert is_numeric(object()) is False


class TestFormatSeconds:
    """Test the format_seconds helper function."""

    @pytest.mark.unit
    def test_format_seconds_only(self):
        """Test formatting seconds less than a minute."""
        assert format_seconds(0) == "0s"
        assert format_seconds(1) == "1s"
        assert format_seconds(30) == "30s"
        assert format_seconds(59) == "59s"

    @pytest.mark.unit
    def test_format_minutes_and_seconds(self):
        """Test formatting minutes and seconds."""
        assert format_seconds(60) == "1m 0s"
        assert format_seconds(90) == "1m 30s"
        assert format_seconds(125) == "2m 5s"
        assert format_seconds(3599) == "59m 59s"

    @pytest.mark.unit
    def test_format_hours_and_minutes(self):
        """Test formatting hours and minutes."""
        assert format_seconds(3600) == "1h 0m"
        assert format_seconds(3660) == "1h 1m"
        assert format_seconds(7200) == "2h 0m"
        assert format_seconds(7320) == "2h 2m"

    @pytest.mark.unit
    def test_format_string_input(self):
        """Test formatting with string input."""
        assert format_seconds("120") == "2m 0s"
        assert format_seconds("3600") == "1h 0m"

    @pytest.mark.unit
    def test_format_float_input(self):
        """Test formatting with float input."""
        assert format_seconds(90.5) == "1m 30s"
        assert format_seconds(3661.9) == "1h 1m"

    @pytest.mark.unit
    def test_format_non_numeric_returns_na(self):
        """Test that non-numeric input returns N/A."""
        assert format_seconds("N/A") == "N/A"
        assert format_seconds(None) == "N/A"
        assert format_seconds("invalid") == "N/A"


class TestDepletionRateCalculation:
    """Test battery depletion rate calculation."""

    @pytest.fixture
    def monitor_with_history(self, minimal_config, tmp_path):
        """Create a monitor with battery history."""
        minimal_config.logging.battery_history_file = str(tmp_path / "battery-history")
        monitor = UPSMonitor(minimal_config)
        monitor.state = MonitorState()
        return monitor

    @pytest.mark.unit
    def test_no_depletion_with_few_samples(self, monitor_with_history):
        """Test that depletion is 0 with insufficient samples."""
        # Add only 10 samples (need 30)
        current_time = int(time.time())
        for i in range(10):
            monitor_with_history.state.battery_history.append(
                (current_time - (10 - i), 100 - i)
            )

        rate = monitor_with_history._calculate_depletion_rate("90")
        assert rate == 0.0

    @pytest.mark.unit
    def test_depletion_calculation_with_enough_samples(self, monitor_with_history):
        """Test depletion calculation with sufficient samples."""
        current_time = int(time.time())

        # Add 60 samples over 60 seconds, battery dropping from 100 to 94
        # That's 6% over 60 seconds = 6%/minute
        for i in range(60):
            battery = 100 - (i * 0.1)  # 0.1% per second = 6%/minute
            monitor_with_history.state.battery_history.append(
                (current_time - (60 - i), battery)
            )

        rate = monitor_with_history._calculate_depletion_rate("94")

        # Should be approximately 6%/min (allowing for rounding)
        assert 5.5 <= rate <= 6.5

    @pytest.mark.unit
    def test_depletion_with_stable_battery(self, monitor_with_history):
        """Test depletion is near zero with stable battery."""
        current_time = int(time.time())

        # Add 60 samples with constant battery
        for i in range(60):
            monitor_with_history.state.battery_history.append(
                (current_time - (60 - i), 100)
            )

        rate = monitor_with_history._calculate_depletion_rate("100")
        assert rate == 0.0

    @pytest.mark.unit
    def test_depletion_with_non_numeric_battery(self, monitor_with_history):
        """Test depletion returns 0 with non-numeric battery."""
        rate = monitor_with_history._calculate_depletion_rate("N/A")
        assert rate == 0.0

    @pytest.mark.unit
    def test_old_samples_are_pruned(self, monitor_with_history):
        """Test that samples outside the window are removed."""
        current_time = int(time.time())
        window = monitor_with_history.config.triggers.depletion.window

        # Add old samples (outside window)
        for i in range(10):
            monitor_with_history.state.battery_history.append(
                (current_time - window - 100 + i, 50)
            )

        # Add current samples
        for i in range(40):
            monitor_with_history.state.battery_history.append(
                (current_time - 40 + i, 100)
            )

        monitor_with_history._calculate_depletion_rate("100")

        # Old samples should be pruned
        oldest_time = monitor_with_history.state.battery_history[0][0]
        assert oldest_time >= current_time - window
