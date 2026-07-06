"""Tests for calculation functions."""

import pytest
import time
from collections import deque

from eneru import (
    is_numeric,
    format_seconds,
    UPSGroupMonitor,
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
    def test_bool_is_not_numeric(self):
        """bool is an int subtype, but NUT/UPS data should never be a bool;
        treating True as 1 would silently conceal an upstream bug."""
        assert is_numeric(True) is False
        assert is_numeric(False) is False

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
        monitor = UPSGroupMonitor(minimal_config)
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

    @pytest.mark.unit
    def test_persist_failure_is_logged_but_not_raised(
        self, monitor_with_history, monkeypatch
    ):
        """Disk failures persisting history must log + continue, never raise."""
        logs = []
        monkeypatch.setattr(
            monitor_with_history, "_log_message", lambda msg: logs.append(msg)
        )
        # Make the temp file path point at a directory that doesn't exist so
        # `open(temp_file, 'w')` raises FileNotFoundError -- exercises the
        # broad except + best-effort log branch (battery.py lines 47-51).
        from pathlib import Path
        monitor_with_history._battery_history_path = Path(
            "/nonexistent/dir/does/not/exist/battery-history"
        )

        # Should not raise; returns 0.0 because fewer than 30 samples.
        rate = monitor_with_history._calculate_depletion_rate("80")
        assert rate == 0.0
        assert any("Battery history persist failed" in msg for msg in logs)

    @pytest.mark.unit
    def test_depletion_zero_when_all_samples_at_same_timestamp(
        self, monitor_with_history
    ):
        """time_diff==0 path returns 0.0 (battery.py line 66)."""
        # Inject >=30 samples that all share the *current* timestamp so that
        # after the function appends one more sample, oldest_time ==
        # current_time and time_diff == 0.
        # We rely on the function computing current_time = int(time.time())
        # and use a far-future timestamp that pruning will keep.
        future = int(time.time()) + 10_000
        for _ in range(50):
            monitor_with_history.state.battery_history.append((future, 100.0))

        # Patch time.time so int(time.time()) inside the function matches the
        # injected timestamp exactly.
        import eneru.health.battery as battery_mod
        original_time = battery_mod.time.time
        try:
            battery_mod.time.time = lambda: float(future)
            rate = monitor_with_history._calculate_depletion_rate("100")
        finally:
            battery_mod.time.time = original_time

        assert rate == 0.0


class TestBatteryAnomalyEdgeCases:
    """Edge cases in `_check_battery_anomaly` not covered elsewhere."""

    @pytest.fixture
    def monitor(self, minimal_config, tmp_path):
        minimal_config.logging.battery_history_file = str(
            tmp_path / "battery-history"
        )
        m = UPSGroupMonitor(minimal_config)
        m.state = MonitorState()
        return m

    @pytest.mark.unit
    def test_non_numeric_battery_charge_returns_early(self, monitor):
        """battery.charge that's not numeric short-circuits the check (line 86)."""
        ups_data = {"ups.status": "OL", "battery.charge": "N/A"}
        # last_battery_charge should remain at its initial -1.0 (untouched).
        monitor.state.last_battery_charge = -1.0
        monitor._check_battery_anomaly(ups_data)
        assert monitor.state.last_battery_charge == -1.0

    @pytest.mark.unit
    def test_confirmation_aborts_when_drop_recovers_below_threshold(
        self, monitor
    ):
        """At poll 3 confirmation, an anomaly_drop <= 20 must abort, not fire.

        Reproduces the false-alarm path the re-validation guard was added
        to fix: charge bounces partway back so when the 3rd poll lands the
        cumulative drop from the pending prev_charge is no longer >20.
        Covers battery.py lines 147-149.
        """
        import time as _time
        notifications = []
        monitor._send_notification = (
            lambda body, ntype=None, **kw: notifications.append(body)
        )

        # Baseline: 100% line power 10s ago, so a drop to 60% looks anomalous.
        monitor.state.last_battery_charge = 100.0
        monitor.state.last_battery_charge_time = _time.time() - 10

        # Poll 1: 60% (40% drop) -> pending
        monitor._check_battery_anomaly(
            {"ups.status": "OL", "battery.charge": "60"}
        )
        assert monitor.state.pending_anomaly_count == 1
        # Poll 2: 75% -- still pending (no >10% bounce above pending_charge of 60)
        # Wait: 75 > 60+10? 75 > 70 -> True, so this would clear the anomaly.
        # Use a charge between 60 and 70 to keep pending but reduce drop.
        # pending prev is 100, so drop = 100 - x. We want x such that
        # x > 60 (sustained-low not jitter-cleared), x < 60+10=70, drop = 100-x <= 20
        # 100 - x <= 20 -> x >= 80. But x < 70 -> contradiction.
        # Instead, set pending_charge low directly so the test focuses on the
        # re-validation branch only. Force-set state to mimic 2 polls done.
        monitor.state.pending_anomaly_charge = 60.0
        monitor.state.pending_anomaly_prev_charge = 100.0
        monitor.state.pending_anomaly_time = _time.time()
        monitor.state.pending_anomaly_count = 2
        # Update last_battery_charge so prev_charge in the function is 60.
        monitor.state.last_battery_charge = 60.0
        monitor.state.last_battery_charge_time = _time.time()

        # Poll 3: 85% -- not high enough to clear (60+10=70, 85>70 clears).
        # We need the "no-jitter clear, then re-validate fails" branch.
        # Drop on current poll: 60 -> 85, drop = -25 (no immediate-anomaly path).
        # Pending recovery check: 85 > 60+10 = 70 -> clears as jitter (line 127-130).
        # So to hit line 147 we need: pending stays (current_charge not > pending+10)
        # and final anomaly_drop = pending_prev - current_charge <= 20.
        # pending_prev = 100, so current_charge >= 80.
        # But current_charge must also be <= pending_charge+10 = 70 to skip recovery.
        # Contradiction unless we mutate pending_prev_charge to e.g. 75.
        monitor.state.pending_anomaly_prev_charge = 75.0  # prev was actually 75
        # Now: current_charge = 65 -> not > 60+10, stays pending; count=3;
        # anomaly_drop = 75 - 65 = 10 <= 20 -> aborts at line 146.
        monitor._check_battery_anomaly(
            {"ups.status": "OL", "battery.charge": "65"}
        )

        # No notification fired; pending cleared.
        assert notifications == []
        assert monitor.state.pending_anomaly_charge == -1.0
        assert monitor.state.pending_anomaly_count == 0
