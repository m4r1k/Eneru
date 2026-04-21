"""Tests for voltage health monitoring (issue #27).

Covers:

- B1: smart grid auto-detection (`STANDARD_GRIDS`, `_snap_to_standard_grid`,
  observed-range cross-check that re-snaps the nominal when NUT lies).
- B2: notification hysteresis (log on transition immediately, defer
  notification until the dwell elapses, suppress flap on revert).

The mixin's host needs `self.config`, `self.state`, `self._log_message`,
`self._log_power_event`, `self._send_notification`, `self._get_ups_var`.
We build a minimal host so each test isolates one slice of behaviour.
"""

import time
import pytest

from eneru import (
    Config, NotificationsConfig, UPSGroupConfig, UPSConfig,
    BehaviorConfig, LoggingConfig, LocalShutdownConfig,
)
from eneru.state import MonitorState
from eneru.health.voltage import (
    VoltageMonitorMixin,
    STANDARD_GRIDS,
    GRID_SNAP_TOLERANCE,
    GRID_QUALITY_DEVIATION_PCT,
    VOLTAGE_SEVERE_DEVIATION_PCT,
    TRANSFER_BUFFER_V,
    AUTODETECT_OBSERVATION_COUNT,
    _snap_to_standard_grid,
    _derive_warning_low,
    _derive_warning_high,
)


class _TestHost(VoltageMonitorMixin):
    """Minimal host that satisfies the mixin's interface contract.

    Captures every log line, power event, and notification so tests can
    assert on them without importing curses, the notification worker,
    or any of the heavier monitor machinery.
    """

    def __init__(self, ups_vars=None, hysteresis: int = 0):
        self.config = type("_C", (), {})()
        self.config.notifications = NotificationsConfig(
            voltage_hysteresis_seconds=hysteresis,
        )
        self.config.NOTIFY_WARNING = "warning"
        self.config.NOTIFY_FAILURE = "failure"
        self.config.NOTIFY_SUCCESS = "success"
        self.config.NOTIFY_INFO = "info"
        self.state = MonitorState()
        self.logs: list = []
        self.power_events: list = []
        self.notifications: list = []
        self._ups_vars = dict(ups_vars or {})

    def _get_ups_var(self, name):
        return self._ups_vars.get(name)

    def _log_message(self, msg):
        self.logs.append(msg)

    def _log_power_event(self, event, details, *, suppress_notification=False):
        self.power_events.append((event, details, suppress_notification))

    def _send_notification(self, body, notify_type="info", blocking=False):
        self.notifications.append((body, notify_type))


# ===========================================================================
# B1: STANDARD_GRIDS + _snap_to_standard_grid
# ===========================================================================

class TestSnapToStandardGrid:

    @pytest.mark.unit
    @pytest.mark.parametrize("raw, expected", [
        # Within tolerance of a standard -> snap.
        (118.0, 120.0),
        (122.5, 120.0),
        (110.0, 110.0),
        (228.0, 230.0),
        (244.0, 240.0),
        (212.0, 208.0),
        (115.0, 115.0),
        (127.0, 127.0),
        # Far from any standard -> keep the raw reading.
        (50.0, 50.0),
        (175.0, 175.0),
        (300.0, 300.0),
    ])
    def test_snap_table(self, raw, expected):
        assert _snap_to_standard_grid(raw) == expected

    @pytest.mark.unit
    def test_snap_zero_or_negative_returns_input(self):
        # Defensive: don't try to snap garbage.
        assert _snap_to_standard_grid(0.0) == 0.0
        assert _snap_to_standard_grid(-12.0) == -12.0

    @pytest.mark.unit
    def test_standard_grids_are_sorted(self):
        # Documentation contract for the constant.
        assert list(STANDARD_GRIDS) == sorted(STANDARD_GRIDS)


# ===========================================================================
# B1: _initialize_voltage_thresholds + _check_voltage_autodetect
# ===========================================================================

class TestInitializeVoltageThresholds:

    @pytest.mark.unit
    def test_us_grid_misreport_snaps_to_120_at_init(self):
        """NUT firmware bug: reports 230V on a US 120V UPS.
        On its own, 230 is a valid standard, so init keeps 230 and
        relies on _check_voltage_autodetect to fix it once we have
        observations. Confirms the init path is unchanged for the
        common case + the snap is invoked."""
        h = _TestHost(ups_vars={"input.voltage.nominal": "230"})
        h._initialize_voltage_thresholds()
        assert h.state.nominal_voltage == 230.0
        # Init log line surfaces the source.
        assert any("NUT=230" in m for m in h.logs)

    @pytest.mark.unit
    def test_init_snaps_close_value_to_standard(self):
        # Reading of 121 is within tolerance of 120 -> snap.
        h = _TestHost(ups_vars={"input.voltage.nominal": "121.5"})
        h._initialize_voltage_thresholds()
        assert h.state.nominal_voltage == 120.0
        # Thresholds derive from the snapped value (±10%).
        assert h.state.voltage_warning_low == pytest.approx(108.0)
        assert h.state.voltage_warning_high == pytest.approx(132.0)

    @pytest.mark.unit
    def test_init_falls_back_to_230_when_nominal_missing(self):
        h = _TestHost(ups_vars={})
        h._initialize_voltage_thresholds()
        assert h.state.nominal_voltage == 230.0
        # Log line names the fallback explicitly.
        assert any("default" in m for m in h.logs)

    @pytest.mark.unit
    def test_init_uses_transfer_bands_when_tighter_than_ten_percent(self):
        # Narrow transfer band (managed UPS): transfer.low=110, transfer.high=130
        # on 120V nominal. transfer-derived candidates 115 / 125 are tighter
        # than ±10% (108 / 132), so they win the clamp.
        h = _TestHost(ups_vars={
            "input.voltage.nominal": "120",
            "input.transfer.low": "110",
            "input.transfer.high": "130",
        })
        h._initialize_voltage_thresholds()
        assert h.state.voltage_warning_low == 115.0   # 110 + 5
        assert h.state.voltage_warning_high == 125.0  # 130 - 5

    @pytest.mark.unit
    def test_init_ignores_transfer_bands_when_inconsistent(self):
        # Some UPS firmwares report 230V transfer bands on 120V grids.
        # Don't apply them if they're way off the snapped nominal.
        h = _TestHost(ups_vars={
            "input.voltage.nominal": "120",
            "input.transfer.low": "200",
            "input.transfer.high": "260",
        })
        h._initialize_voltage_thresholds()
        # Falls back to ±10% of nominal.
        assert h.state.voltage_warning_low == pytest.approx(108.0)
        assert h.state.voltage_warning_high == pytest.approx(132.0)


class TestVoltageAutodetect:

    @pytest.mark.unit
    def test_autodetect_no_observations_keeps_initial_thresholds(self):
        h = _TestHost(ups_vars={"input.voltage.nominal": "230"})
        h._initialize_voltage_thresholds()
        # Feed only a few observations -> autodetect not yet finalized.
        for _ in range(AUTODETECT_OBSERVATION_COUNT - 1):
            h._check_voltage_autodetect("230.0")
        assert not h.state.voltage_autodetect_done
        assert h.state.nominal_voltage == 230.0

    @pytest.mark.unit
    def test_autodetect_resnaps_when_us_grid_observed(self):
        """The headline issue #27 case: NUT says 230, real grid is 120."""
        h = _TestHost(ups_vars={"input.voltage.nominal": "230"})
        h._initialize_voltage_thresholds()
        # 10 observations all near 120V.
        for v in [118, 119, 120, 120, 121, 122, 119, 120, 121, 120]:
            h._check_voltage_autodetect(str(v))
        assert h.state.voltage_autodetect_done
        assert h.state.nominal_voltage == 120.0
        assert h.state.voltage_warning_low == pytest.approx(108.0)
        assert h.state.voltage_warning_high == pytest.approx(132.0)
        # Fired the auditable event so operators know we second-guessed NUT.
        assert any(ev[0] == "VOLTAGE_AUTODETECT_MISMATCH" for ev in h.power_events)
        # Also a human log line with the median + window.
        assert any("re-snap" in m for m in h.logs)

    @pytest.mark.unit
    def test_autodetect_silent_for_clean_eu_grid(self):
        """NUT says 230, real grid is 230 -> no event, no re-snap."""
        h = _TestHost(ups_vars={"input.voltage.nominal": "230"})
        h._initialize_voltage_thresholds()
        for v in [228, 230, 231, 229, 230, 232, 228, 230, 231, 230]:
            h._check_voltage_autodetect(str(v))
        assert h.state.voltage_autodetect_done
        assert h.state.nominal_voltage == 230.0
        # No mismatch event fired.
        assert not any(ev[0] == "VOLTAGE_AUTODETECT_MISMATCH"
                       for ev in h.power_events)

    @pytest.mark.unit
    def test_autodetect_runs_only_once(self):
        h = _TestHost(ups_vars={"input.voltage.nominal": "230"})
        h._initialize_voltage_thresholds()
        for v in [120] * 10:
            h._check_voltage_autodetect(str(v))
        events_after_first = list(h.power_events)
        # Subsequent calls (even with wildly different values) are no-ops.
        for v in [50] * 10:
            h._check_voltage_autodetect(str(v))
        assert h.power_events == events_after_first

    @pytest.mark.unit
    def test_autodetect_rejects_garbage_readings(self):
        # NUT drivers occasionally emit 0 or impossibly-high values
        # (especially while OB). Don't let those pollute the median.
        h = _TestHost(ups_vars={"input.voltage.nominal": "230"})
        h._initialize_voltage_thresholds()
        for v in ["0", "999", "not-a-number", "-5", ""]:
            h._check_voltage_autodetect(v)
        assert len(h.state.voltage_observed) == 0


# ===========================================================================
# B2: notification hysteresis
# ===========================================================================

class TestVoltageHysteresis:

    @pytest.mark.unit
    def test_hysteresis_zero_fires_immediately(self):
        # Default-disabled (=0) preserves legacy behaviour: notification
        # fires on the same poll the state transitions to HIGH.
        h = _TestHost(ups_vars={"input.voltage.nominal": "230"}, hysteresis=0)
        h._initialize_voltage_thresholds()
        h._check_voltage_issues("OL", "260")
        assert h.state.voltage_state == "HIGH"
        # Notification dispatched (via _send_notification, not _log_power_event).
        assert any("VOLTAGE ISSUE" in body for body, _ in h.notifications)

    @pytest.mark.unit
    def test_hysteresis_short_flap_suppresses_notification(self):
        # 30s dwell, but we revert after 1s -> no notification, plus a
        # VOLTAGE_FLAP_SUPPRESSED event for visibility.
        h = _TestHost(ups_vars={"input.voltage.nominal": "230"}, hysteresis=30)
        h._initialize_voltage_thresholds()

        h._check_voltage_issues("OL", "260")  # transitions to HIGH
        assert h.state.voltage_state == "HIGH"
        assert h.notifications == []  # not yet notified
        assert h.state.voltage_pending_state == "HIGH"

        h._check_voltage_issues("OL", "230")  # back to NORMAL within dwell
        assert h.state.voltage_state == "NORMAL"
        assert h.notifications == []  # still not notified
        # The flap was recorded as its own event.
        assert any(ev[0] == "VOLTAGE_FLAP_SUPPRESSED" for ev in h.power_events)
        # And the pending state is cleared.
        assert h.state.voltage_pending_state == ""

    @pytest.mark.unit
    def test_hysteresis_sustained_event_eventually_notifies(self, monkeypatch):
        # Use a tiny dwell (1 s) to keep the test fast, then advance time
        # via monkeypatch so we don't actually sleep.
        h = _TestHost(ups_vars={"input.voltage.nominal": "230"}, hysteresis=1)
        h._initialize_voltage_thresholds()

        t0 = time.time()
        clock = [t0]
        monkeypatch.setattr("eneru.health.voltage.time.time",
                            lambda: clock[0])

        h._check_voltage_issues("OL", "260")  # transition; pending opened
        assert h.state.voltage_pending_state == "HIGH"
        assert h.notifications == []

        # Advance past the dwell -- next poll must fire the notification.
        clock[0] = t0 + 2.0
        h._check_voltage_issues("OL", "260")
        assert h.notifications, "expected the deferred notification to fire"
        body, _ = h.notifications[0]
        assert "OVER_VOLTAGE" in body or "VOLTAGE ISSUE" in body
        assert "Persisted" in body  # the dwell-annotation is present

        # Same poll cycle running again must NOT double-fire.
        n_before = len(h.notifications)
        h._check_voltage_issues("OL", "260")
        assert len(h.notifications) == n_before

    @pytest.mark.unit
    def test_voltage_normalized_on_OB_clears_pending(self):
        # Going on-battery resets voltage_state to NORMAL and must
        # NOT leave a stale pending notification queued.
        h = _TestHost(ups_vars={"input.voltage.nominal": "230"}, hysteresis=30)
        h._initialize_voltage_thresholds()
        h._check_voltage_issues("OL", "260")
        assert h.state.voltage_pending_state == "HIGH"
        h._check_voltage_issues("OB DISCHRG", "0")
        assert h.state.voltage_state == "NORMAL"
        assert h.state.voltage_pending_state == ""

    @pytest.mark.unit
    def test_brownout_path_also_hysteresis_gated(self):
        # Symmetry check: BROWNOUT goes through the same dwell logic.
        # Use 200V (13% under nominal -- below the warning threshold of
        # 207V but mild enough to NOT trigger the rc9 severity bypass at
        # ±15%). 180V would be severe and notify immediately.
        h = _TestHost(ups_vars={"input.voltage.nominal": "230"}, hysteresis=30)
        h._initialize_voltage_thresholds()
        h._check_voltage_issues("OL", "200")  # below 207V (= 230*0.9), mild
        assert h.state.voltage_state == "LOW"
        assert h.notifications == []
        # And the immediate log/event fires (with suppress_notification=True).
        assert any(ev[0] == "BROWNOUT_DETECTED" and ev[2]
                   for ev in h.power_events)


# ===========================================================================
# rc9: threshold clamp -- tighter of (±10% nominal) and (transfer ± buffer)
# ===========================================================================

class TestThresholdClamp:
    """Item 1 (rc9): voltage warnings clamp to the tighter of the EN 50160
    ±10% envelope and the NUT transfer points ± 5V buffer. Wide UPS
    firmware defaults can no longer produce useless warnings."""

    @pytest.mark.unit
    def test_wide_transfer_clamped_to_ten_percent(self):
        # APC default-wide on 230V: transfer 170/280 (24%/22% from nominal).
        # The transfer-derived candidates (175/275) are MUCH wider than ±10%
        # (207/253). The clamp picks the tighter -- ±10% wins.
        assert _derive_warning_low(230, 170) == 207.0
        assert _derive_warning_high(230, 280) == 253.0

    @pytest.mark.unit
    def test_narrow_transfer_uses_transfer_based(self):
        # Managed UPS configured tight: transfer 215/245 on 230V. Transfer-
        # derived (220/240) is tighter than ±10% (207/253), so it wins.
        assert _derive_warning_low(230, 215) == 220.0
        assert _derive_warning_high(230, 245) == 240.0

    @pytest.mark.unit
    def test_no_transfer_uses_ten_percent_fallback(self):
        # No NUT transfer info -- ±10% is the only candidate.
        assert _derive_warning_low(230, None) == 207.0
        assert _derive_warning_high(230, None) == 253.0

    @pytest.mark.unit
    def test_garbage_transfer_falls_back_to_ten_percent(self):
        # Wildly wrong transfer (e.g., NUT firmware bug reporting EU
        # values on a US grid): ignored by the ±25% sanity check, falls
        # back to ±10%.
        assert _derive_warning_low(120, 250) == 108.0   # 250 way off |250-108|=142 > 30
        assert _derive_warning_high(120, 50) == 132.0   # 50 way off |50-132|=82 > 30

    @pytest.mark.unit
    def test_low_transfer_at_or_above_nominal_is_rejected(self):
        # Regression for CodeRabbit major: NUT reporting low_transfer
        # >= nominal is nonsense (UPS would switch on perfectly normal
        # mains). Without the guard, low_transfer=250 on 230V passes
        # the ±25% sanity (|250-207|=43 ≤ 57.5) and would yield
        # warning_low=255, making 230V mains read as a brownout.
        assert _derive_warning_low(230, 250) == 207.0   # rejected, fall back to ±10%
        assert _derive_warning_low(230, 230) == 207.0   # equal also rejected
        assert _derive_warning_low(230, 231) == 207.0   # just-above also rejected

    @pytest.mark.unit
    def test_high_transfer_at_or_below_nominal_is_rejected(self):
        # Symmetric guard: high_transfer <= nominal is nonsense.
        # 200 on 230V would otherwise yield warning_high = 195V,
        # below the 207V low warning -- impossible band.
        assert _derive_warning_high(230, 200) == 253.0   # rejected
        assert _derive_warning_high(230, 230) == 253.0   # equal also rejected
        assert _derive_warning_high(230, 229) == 253.0   # just-below also rejected

    @pytest.mark.unit
    def test_low_transfer_buffered_candidate_above_nominal_is_rejected(self):
        # Cubic P1 regression on PR #29: a low_transfer value can sit
        # below nominal (passing the first guard) but the buffered
        # candidate `lt + 5` can land above nominal -- which would make
        # warning_low > nominal and trigger BROWNOUT on every normal
        # poll. transfer.low=226 on 230V → candidate=231 > 230 → reject,
        # fall back to ±10%.
        assert _derive_warning_low(230, 226) == 207.0   # 226+5=231 > 230
        assert _derive_warning_low(230, 225) == 207.0   # 225+5=230, not strictly < nominal
        # 224 + 5 = 229, strictly below 230 -- accepted.
        assert _derive_warning_low(230, 224) == 229.0

    @pytest.mark.unit
    def test_high_transfer_buffered_candidate_below_nominal_is_rejected(self):
        # Symmetric Cubic P1: high_transfer above nominal but
        # ht - buffer falls below. transfer.high=234 on 230V → candidate
        # = 229 < 230 → reject. transfer.high=235 → candidate=230, not
        # strictly > nominal → reject. transfer.high=236 → candidate=231
        # → accepted.
        assert _derive_warning_high(230, 234) == 253.0   # 234-5=229
        assert _derive_warning_high(230, 235) == 253.0   # 235-5=230
        assert _derive_warning_high(230, 236) == 231.0   # 236-5=231 ✓

    @pytest.mark.unit
    def test_state_records_ups_transfer_points(self):
        h = _TestHost(ups_vars={
            "input.voltage.nominal": "230",
            "input.transfer.low": "170",
            "input.transfer.high": "280",
        })
        h._initialize_voltage_thresholds()
        # Raw transfer values stashed verbatim for notification context.
        assert h.state.ups_transfer_low == 170.0
        assert h.state.ups_transfer_high == 280.0
        # Warnings clamped to ±10%, NOT 175/275.
        assert h.state.voltage_warning_low == 207.0
        assert h.state.voltage_warning_high == 253.0

    @pytest.mark.unit
    def test_state_transfer_is_none_when_nut_silent(self):
        h = _TestHost(ups_vars={"input.voltage.nominal": "230"})
        h._initialize_voltage_thresholds()
        assert h.state.ups_transfer_low is None
        assert h.state.ups_transfer_high is None

    @pytest.mark.unit
    def test_startup_log_includes_grid_quality_and_ups_switch_lines(self):
        h = _TestHost(ups_vars={
            "input.voltage.nominal": "230",
            "input.transfer.low": "170",
            "input.transfer.high": "280",
        })
        h._initialize_voltage_thresholds()
        joined = "\n".join(h.logs)
        assert "Grid-quality warnings: 207.0V / 253.0V" in joined
        assert "UPS battery-switch points: 170.0V / 280.0V" in joined
        assert "EN 50160 envelope" in joined

    @pytest.mark.unit
    def test_startup_log_omits_ups_switch_line_when_silent(self):
        h = _TestHost(ups_vars={"input.voltage.nominal": "230"})
        h._initialize_voltage_thresholds()
        joined = "\n".join(h.logs)
        assert "Grid-quality warnings:" in joined
        assert "UPS battery-switch points:" not in joined

    @pytest.mark.unit
    def test_autodetect_resnap_applies_clamp(self):
        # NUT says 230, transfer 170/280 (wide). Real grid is 120 -- the
        # autodetect re-snaps to 120V. New thresholds must use the SAME
        # tighter-of clamp (not raw 0.9/1.1), and the cached transfer
        # values are too wide for 120V to be applied (|170-108|=62 > 30).
        h = _TestHost(ups_vars={
            "input.voltage.nominal": "230",
            "input.transfer.low": "170",
            "input.transfer.high": "280",
        })
        h._initialize_voltage_thresholds()
        # Confirm initial transfer caches.
        assert h.state.ups_transfer_low == 170.0
        # Feed observations near 120V.
        for v in [118, 119, 120, 120, 121, 122, 119, 120, 121, 120]:
            h._check_voltage_autodetect(str(v))
        assert h.state.voltage_autodetect_done
        assert h.state.nominal_voltage == 120.0
        # Transfer points were valid for 230V but are >25% off for 120V,
        # so the clamp ignores them and uses ±10% of the new nominal.
        assert h.state.voltage_warning_low == 108.0
        assert h.state.voltage_warning_high == 132.0


# ===========================================================================
# rc9: severity-aware notification bypass
# ===========================================================================

class TestSeverityBypass:
    """Item 2 (rc9): deviations >±15% bypass the voltage_hysteresis_seconds
    dwell and notify immediately. Mild deviations (10-15%) still go through
    the dwell so neighbour-appliance flap doesn't spam."""

    @pytest.mark.unit
    def test_mild_deviation_uses_hysteresis(self):
        # 200V on 230V = 13.0% below -- inside the 10-15% mild band.
        # Hysteresis applies: no immediate notification.
        h = _TestHost(ups_vars={"input.voltage.nominal": "230"}, hysteresis=30)
        h._initialize_voltage_thresholds()
        h._check_voltage_issues("OL", "200")
        assert h.state.voltage_state == "LOW"
        assert h.state.voltage_pending_state == "LOW"
        assert h.state.voltage_pending_severe is False
        assert h.notifications == []

    @pytest.mark.unit
    def test_severe_deviation_bypasses_hysteresis(self):
        # 180V on 230V = 21.7% below -- severe. Notification fires
        # immediately on the first poll past the threshold, even with
        # a 30s dwell configured.
        h = _TestHost(ups_vars={"input.voltage.nominal": "230"}, hysteresis=30)
        h._initialize_voltage_thresholds()
        h._check_voltage_issues("OL", "180")
        assert h.state.voltage_state == "LOW"
        assert h.state.voltage_pending_severe is True
        assert h.notifications, "severe deviation must notify immediately"
        body, _ = h.notifications[0]
        assert "BROWNOUT_DETECTED" in body
        assert "(severe," in body

    @pytest.mark.unit
    def test_severity_threshold_is_15_percent(self):
        # Exactly +15.0% (264.5V on 230V) is NOT severe -- threshold is `>`.
        # +15.1% (264.73V) IS severe.
        h_mild = _TestHost(ups_vars={"input.voltage.nominal": "230"}, hysteresis=30)
        h_mild._initialize_voltage_thresholds()
        h_mild._check_voltage_issues("OL", "264.5")
        assert h_mild.state.voltage_pending_severe is False
        assert h_mild.notifications == []

        h_severe = _TestHost(ups_vars={"input.voltage.nominal": "230"}, hysteresis=30)
        h_severe._initialize_voltage_thresholds()
        h_severe._check_voltage_issues("OL", "265")  # ~15.2%
        assert h_severe.state.voltage_pending_severe is True
        assert h_severe.notifications  # immediate

    @pytest.mark.unit
    def test_severe_overvoltage_also_bypasses(self):
        # Symmetry check on the high side.
        h = _TestHost(ups_vars={"input.voltage.nominal": "230"}, hysteresis=30)
        h._initialize_voltage_thresholds()
        h._check_voltage_issues("OL", "280")  # 21.7% above
        assert h.state.voltage_state == "HIGH"
        assert h.state.voltage_pending_severe is True
        assert h.notifications
        body, _ = h.notifications[0]
        assert "OVER_VOLTAGE_DETECTED" in body
        assert "(severe," in body

    @pytest.mark.unit
    def test_severe_notification_marks_immediate_dispatch(self):
        h = _TestHost(ups_vars={"input.voltage.nominal": "230"}, hysteresis=30)
        h._initialize_voltage_thresholds()
        h._check_voltage_issues("OL", "180")
        body, _ = h.notifications[0]
        assert "Notifying immediately" in body
        assert "bypassed hysteresis" in body


# ===========================================================================
# rc9: notification text -- grid-quality framing + UPS-switch context
# ===========================================================================

class TestNotificationText:
    """Item 3 (rc9): notifications carry % deviation, threshold, and a
    UPS-switch-context line so the operator understands whether this is
    a quality issue or an imminent UPS reaction."""

    @pytest.mark.unit
    def test_mild_brownout_includes_ups_switch_context(self):
        # Force immediate dispatch via hysteresis=0.
        h = _TestHost(ups_vars={
            "input.voltage.nominal": "230",
            "input.transfer.low": "170",
            "input.transfer.high": "280",
        }, hysteresis=0)
        h._initialize_voltage_thresholds()
        h._check_voltage_issues("OL", "200")  # 13% below, mild
        assert h.notifications
        body, _ = h.notifications[0]
        assert "13.0% below" in body
        assert "230V nominal" in body
        assert "warning threshold 207.0V" in body
        assert "UPS will not switch to battery until 170.0V" in body
        assert "grid-quality issue" in body

    @pytest.mark.unit
    def test_severe_brownout_includes_severity_tag_and_warns_about_ups(self):
        h = _TestHost(ups_vars={
            "input.voltage.nominal": "230",
            "input.transfer.low": "170",
        }, hysteresis=30)
        h._initialize_voltage_thresholds()
        h._check_voltage_issues("OL", "180")  # 21.7%, severe
        body, _ = h.notifications[0]
        assert "(severe, 21.7% below nominal)" in body
        assert "Approaching UPS battery-switch threshold (170.0V)" in body

    @pytest.mark.unit
    def test_message_omits_ups_context_when_transfer_unknown(self):
        h = _TestHost(ups_vars={"input.voltage.nominal": "230"}, hysteresis=0)
        h._initialize_voltage_thresholds()
        h._check_voltage_issues("OL", "200")
        body, _ = h.notifications[0]
        assert "UPS will not switch" not in body
        assert "Approaching UPS" not in body
        # But the % deviation framing is still present.
        assert "13.0% below" in body

    @pytest.mark.unit
    def test_mild_overvoltage_explains_en50160_envelope_correctly(self):
        # Regression for CodeRabbit major: the previous "EN 50160
        # considers up to that level acceptable" wording was wrong --
        # it implied EN 50160 accepts the UPS switch threshold (e.g.,
        # 280V), but EN 50160 actually caps at nominal × 1.1 (253V on
        # 230V). The corrected message frames the warning as outside
        # the EN 50160 ±10% envelope without putting words into the
        # standard's mouth about higher voltages.
        h = _TestHost(ups_vars={
            "input.voltage.nominal": "230",
            "input.transfer.high": "280",
        }, hysteresis=0)
        h._initialize_voltage_thresholds()
        h._check_voltage_issues("OL", "256")  # 11.3% above, mild
        body, _ = h.notifications[0]
        # Corrected wording: warning is OUTSIDE the EN 50160 envelope.
        assert "outside the EN 50160" in body
        assert "UPS will not switch to battery until 280.0V" in body
        # The buggy wording must NOT appear.
        assert "EN 50160 considers up to that level acceptable" not in body
