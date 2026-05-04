"""Tests for the pure-function health model used by redundancy groups."""

import time

import pytest

from eneru import UPSHealth, assess_health
from eneru.state import HealthSnapshot


def _snap(**overrides):
    """Build a fully-specified ``HealthSnapshot`` with ``overrides`` applied."""
    defaults = dict(
        status="OL",
        battery_charge="100",
        runtime="1800",
        load="25",
        depletion_rate=0.0,
        time_on_battery=0,
        last_update_time=1_000_000.0,
        connection_state="OK",
        trigger_active=False,
        trigger_reason="",
        stale_data_count=0,
        connection_lost_time=0.0,
    )
    defaults.update(overrides)
    return HealthSnapshot(**defaults)


# ``now`` value used as the reference clock in every parametrized case.
NOW = 1_000_000.0


class TestAssessHealthBasicTiers:
    """Direct tier classification with explicit snapshots."""

    @pytest.mark.unit
    def test_healthy_baseline(self):
        snap = _snap()
        assert assess_health(snap, None, 1, now=NOW) == UPSHealth.HEALTHY

    @pytest.mark.unit
    def test_on_battery_no_trigger_is_degraded(self):
        snap = _snap(status="OB DISCHRG")
        assert assess_health(snap, None, 1, now=NOW) == UPSHealth.DEGRADED

    @pytest.mark.unit
    def test_on_battery_with_trigger_is_critical(self):
        snap = _snap(status="OB DISCHRG", trigger_active=True,
                     trigger_reason="battery 5% < threshold 20%")
        assert assess_health(snap, None, 1, now=NOW) == UPSHealth.CRITICAL

    @pytest.mark.unit
    def test_fsd_status_is_critical(self):
        snap = _snap(status="OB FSD")
        assert assess_health(snap, None, 1, now=NOW) == UPSHealth.CRITICAL

    @pytest.mark.unit
    def test_grace_period_is_degraded(self):
        snap = _snap(connection_state="GRACE_PERIOD")
        assert assess_health(snap, None, 1, now=NOW) == UPSHealth.DEGRADED

    @pytest.mark.unit
    def test_failed_connection_is_unknown(self):
        snap = _snap(connection_state="FAILED")
        assert assess_health(snap, None, 1, now=NOW) == UPSHealth.UNKNOWN

    @pytest.mark.unit
    def test_no_observations_is_unknown(self):
        snap = _snap(last_update_time=0.0)
        assert assess_health(snap, None, 1, now=NOW) == UPSHealth.UNKNOWN


class TestAssessHealthStaleness:
    """``5 * check_interval`` stale-snapshot rule."""

    @pytest.mark.unit
    def test_just_inside_stale_threshold_is_healthy(self):
        snap = _snap(last_update_time=NOW - 4.99)
        assert assess_health(snap, None, 1, now=NOW) == UPSHealth.HEALTHY

    @pytest.mark.unit
    def test_just_past_stale_threshold_is_unknown(self):
        snap = _snap(last_update_time=NOW - 5.01)
        assert assess_health(snap, None, 1, now=NOW) == UPSHealth.UNKNOWN

    @pytest.mark.unit
    def test_stale_snapshot_during_connection_grace_is_degraded(self):
        snap = _snap(
            last_update_time=NOW - 30,
            connection_state="GRACE_PERIOD",
            stale_data_count=3,
            connection_lost_time=NOW - 10,
        )
        assert assess_health(
            snap,
            None,
            1,
            connection_grace_enabled=True,
            now=NOW,
        ) == UPSHealth.DEGRADED

    @pytest.mark.unit
    def test_grace_period_past_duration_is_unknown(self):
        snap = _snap(
            last_update_time=NOW - 90,
            connection_state="GRACE_PERIOD",
            stale_data_count=3,
            connection_lost_time=NOW - 61,
        )
        assert assess_health(
            snap,
            None,
            1,
            connection_grace_enabled=True,
            now=NOW,
        ) == UPSHealth.UNKNOWN

    @pytest.mark.unit
    def test_grace_period_back_compat_no_lost_at_fresh_snapshot_is_degraded(self):
        # Back-compat: pre-field snapshots leave ``connection_lost_time`` at
        # 0.0. With a fresh ``last_update_time`` (age <= stale_threshold) the
        # clamped fallback must keep the member DEGRADED rather than flipping
        # UNKNOWN by accident.
        snap = _snap(
            last_update_time=NOW - 1,
            connection_state="GRACE_PERIOD",
            connection_lost_time=0.0,
        )
        assert assess_health(
            snap,
            None,
            1,
            connection_grace_enabled=True,
            now=NOW,
        ) == UPSHealth.DEGRADED

    @pytest.mark.unit
    def test_grace_period_back_compat_no_lost_at_past_grace_is_unknown(self):
        # Back-compat fallback still fails safe once the snapshot is old
        # enough that ``age - stale_threshold`` exceeds the grace window.
        snap = _snap(
            last_update_time=NOW - 90,
            connection_state="GRACE_PERIOD",
            connection_lost_time=0.0,
        )
        assert assess_health(
            snap,
            None,
            1,
            connection_grace_enabled=True,
            connection_grace_duration=60,
            now=NOW,
        ) == UPSHealth.UNKNOWN

    @pytest.mark.unit
    def test_zero_duration_grace_period_is_immediate_unknown(self):
        snap = _snap(
            last_update_time=NOW - 10,
            connection_state="GRACE_PERIOD",
            stale_data_count=3,
            connection_lost_time=NOW,
        )
        assert assess_health(
            snap,
            None,
            1,
            connection_grace_enabled=True,
            connection_grace_duration=0,
            now=NOW,
        ) == UPSHealth.UNKNOWN

    @pytest.mark.unit
    def test_in_flight_slow_poll_is_degraded_inside_grace(self):
        snap = _snap(last_update_time=NOW - 30, connection_state="OK")
        assert assess_health(
            snap,
            None,
            1,
            connection_grace_enabled=True,
            now=NOW,
        ) == UPSHealth.DEGRADED

    @pytest.mark.unit
    def test_in_flight_slow_poll_past_grace_is_unknown(self):
        snap = _snap(last_update_time=NOW - 70, connection_state="OK")
        assert assess_health(
            snap,
            None,
            1,
            connection_grace_enabled=True,
            now=NOW,
        ) == UPSHealth.UNKNOWN

    @pytest.mark.unit
    def test_in_flight_slow_poll_without_grace_is_unknown(self):
        snap = _snap(last_update_time=NOW - 30, connection_state="OK")
        assert assess_health(
            snap,
            None,
            1,
            connection_grace_enabled=False,
            now=NOW,
        ) == UPSHealth.UNKNOWN

    @pytest.mark.unit
    def test_transient_stale_data_after_good_poll_is_degraded(self):
        snap = _snap(last_update_time=NOW - 10, stale_data_count=1)
        assert assess_health(snap, None, 1, now=NOW) == UPSHealth.DEGRADED

    @pytest.mark.unit
    def test_stale_retry_past_pre_grace_window_is_unknown(self):
        snap = _snap(last_update_time=NOW - 30, stale_data_count=1)
        assert assess_health(snap, None, 1, now=NOW) == UPSHealth.UNKNOWN

    @pytest.mark.unit
    def test_no_observations_still_unknown_even_with_stale_count(self):
        snap = _snap(last_update_time=0, stale_data_count=2)
        assert assess_health(snap, None, 1, now=NOW) == UPSHealth.UNKNOWN

    @pytest.mark.unit
    def test_threshold_scales_with_check_interval(self):
        # 5s old, check_interval=5 → still inside the 25s window.
        snap = _snap(last_update_time=NOW - 5)
        assert assess_health(snap, None, 5, now=NOW) == UPSHealth.HEALTHY

    @pytest.mark.unit
    def test_threshold_scales_with_check_interval_negative_case(self):
        # 30s old, check_interval=5 → past 25s window → UNKNOWN.
        snap = _snap(last_update_time=NOW - 30)
        assert assess_health(snap, None, 5, now=NOW) == UPSHealth.UNKNOWN

    @pytest.mark.unit
    def test_zero_check_interval_falls_back_to_one(self):
        # Defensive: check_interval=0 must not divide-by-zero or be too tight.
        snap = _snap(last_update_time=NOW - 4)
        assert assess_health(snap, None, 0, now=NOW) == UPSHealth.HEALTHY

    @pytest.mark.unit
    def test_uses_real_clock_when_now_omitted(self):
        snap = _snap(last_update_time=time.time())
        assert assess_health(snap, None, 1) == UPSHealth.HEALTHY


class TestAssessHealthPriority:
    """First-match-wins ordering between the four tiers."""

    @pytest.mark.unit
    def test_failed_beats_trigger_active(self):
        # FAILED connection wins over CRITICAL signals.
        snap = _snap(connection_state="FAILED",
                     trigger_active=True, status="OB FSD")
        assert assess_health(snap, None, 1, now=NOW) == UPSHealth.UNKNOWN

    @pytest.mark.unit
    def test_stale_beats_trigger_active(self):
        snap = _snap(last_update_time=NOW - 100,
                     trigger_active=True, status="OB FSD")
        assert assess_health(snap, None, 1, now=NOW) == UPSHealth.UNKNOWN

    @pytest.mark.unit
    def test_trigger_active_beats_fsd(self):
        snap = _snap(trigger_active=True, status="OL")
        assert assess_health(snap, None, 1, now=NOW) == UPSHealth.CRITICAL

    @pytest.mark.unit
    def test_fsd_beats_ob(self):
        snap = _snap(status="OB FSD")
        assert assess_health(snap, None, 1, now=NOW) == UPSHealth.CRITICAL

    @pytest.mark.unit
    def test_ob_beats_grace_period(self):
        snap = _snap(status="OB DISCHRG", connection_state="GRACE_PERIOD")
        assert assess_health(snap, None, 1, now=NOW) == UPSHealth.DEGRADED


@pytest.mark.parametrize("status,expected", [
    ("OL", UPSHealth.HEALTHY),
    ("OL CHRG", UPSHealth.HEALTHY),
    ("OL CHRG BOOST", UPSHealth.HEALTHY),  # AVR not in snapshot path
    ("OB", UPSHealth.DEGRADED),
    ("OB DISCHRG", UPSHealth.DEGRADED),
    ("OB FSD", UPSHealth.CRITICAL),
    ("FSD", UPSHealth.CRITICAL),
    ("OL FSD", UPSHealth.CRITICAL),
])
@pytest.mark.unit
def test_status_string_classification(status, expected):
    assert assess_health(_snap(status=status), None, 1, now=NOW) == expected


@pytest.mark.parametrize("conn_state,expected", [
    ("OK", UPSHealth.HEALTHY),
    ("GRACE_PERIOD", UPSHealth.DEGRADED),
    ("FAILED", UPSHealth.UNKNOWN),
])
@pytest.mark.unit
def test_connection_state_classification(conn_state, expected):
    assert assess_health(
        _snap(connection_state=conn_state), None, 1, now=NOW
    ) == expected


class TestUPSHealthEnum:
    """Stable enum surface used by other modules."""

    @pytest.mark.unit
    def test_values_are_documented_strings(self):
        assert UPSHealth.HEALTHY.value == "healthy"
        assert UPSHealth.DEGRADED.value == "degraded"
        assert UPSHealth.CRITICAL.value == "critical"
        assert UPSHealth.UNKNOWN.value == "unknown"

    @pytest.mark.unit
    def test_enum_is_str_subclass(self):
        # Important for log-friendly comparisons (e.g. h == "healthy").
        assert isinstance(UPSHealth.HEALTHY, str)
        assert UPSHealth.HEALTHY == "healthy"

    @pytest.mark.unit
    def test_membership_is_complete(self):
        members = {h.value for h in UPSHealth}
        assert members == {"healthy", "degraded", "critical", "unknown"}
