"""Tests for voltage health monitoring (issue #27, issue #4).

Covers:

- B1: smart grid auto-detection (`STANDARD_GRIDS`, `_snap_to_standard_grid`,
  observed-range cross-check that re-snaps the nominal when NUT lies).
- B2: notification hysteresis (log on transition immediately, defer
  notification until the dwell elapses, suppress flap on revert).
- v5.1.2 (#4): voltage_sensitivity preset (tight/normal/loose), single
  percentage-band threshold formula, one-time migration warning when
  v5.1.1's narrower transfer-derived band would have been tighter.

The mixin's host needs `self.config`, `self.state`, `self._log_message`,
`self._log_power_event`, `self._send_notification`, `self._get_ups_var`.
We build a minimal host so each test isolates one slice of behaviour.
"""

import time
import pytest

from eneru import (
    Config, NotificationsConfig, UPSGroupConfig, UPSConfig,
    BehaviorConfig, LoggingConfig, LocalShutdownConfig,
    TriggersConfig,
)
from eneru.state import MonitorState
from eneru.health.voltage import (
    VoltageMonitorMixin,
    STANDARD_GRIDS,
    GRID_SNAP_TOLERANCE,
    DEFAULT_GRID_QUALITY_DEVIATION_PCT,
    VOLTAGE_SEVERE_DEVIATION_PCT,
    AUTODETECT_OBSERVATION_COUNT,
    _snap_to_standard_grid,
    _derive_warning_low,
    _derive_warning_high,
    _legacy_warning_low,
    _legacy_warning_high,
    _resolve_sensitivity_pct,
)


class _TestHost(VoltageMonitorMixin):
    """Minimal host that satisfies the mixin's interface contract.

    Captures every log line, power event, and notification so tests can
    assert on them without importing curses, the notification worker,
    or any of the heavier monitor machinery.
    """

    def __init__(self, ups_vars=None, hysteresis: int = 0,
                 voltage_sensitivity: str = "normal",
                 voltage_sensitivity_explicit: bool = False):
        # `hysteresis=0` makes notifications fire immediately on the
        # poll the state transitions; tests that want to exercise the
        # dwell pass an explicit value (typically 30 to mirror the
        # production default, or 1 with a monkeypatched clock for
        # deterministic timing). The migration warning fires at init
        # time, not on a state transition, so the hysteresis dwell
        # does not affect it -- migration-warning tests can leave
        # hysteresis at the 0 default.
        self.config = type("_C", (), {})()
        self.config.notifications = NotificationsConfig(
            voltage_hysteresis_seconds=hysteresis,
        )
        self.config.triggers = TriggersConfig(
            voltage_sensitivity=voltage_sensitivity,
            voltage_sensitivity_explicit=voltage_sensitivity_explicit,
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
    def test_chris_repro_120v_106_127_no_false_alarm_at_122v(self):
        """Issue #4: US 120V grid + APC firmware (106/127) on a hot grid.

        v5.1.1 chose the transfer-derived candidates 111/122. At 120V
        grid with the utility running at 122.4V (well within the
        EN 50160 envelope), the operator got a constant flood of
        OVER_VOLTAGE_DETECTED + VOLTAGE_FLAP_SUPPRESSED noise.

        v5.1.2 returns 108/132 on default `normal` (10%) sensitivity --
        no false alarm at 122.4V.
        """
        h = _TestHost(ups_vars={
            "input.voltage.nominal": "120",
            "input.transfer.low": "106",
            "input.transfer.high": "127",
        })
        h._initialize_voltage_thresholds()
        assert h.state.voltage_warning_low == pytest.approx(108.0)
        assert h.state.voltage_warning_high == pytest.approx(132.0)

        # 122.4V on the wire: well inside the new band.
        h._check_voltage_issues("OL", "122.4")
        assert h.state.voltage_state == "NORMAL"
        assert not any(ev[0] == "OVER_VOLTAGE_DETECTED"
                       for ev in h.power_events)

        # Drop to 107V: just below 108V threshold -> brownout.
        h._check_voltage_issues("OL", "107")
        assert h.state.voltage_state == "LOW"
        assert any(ev[0] == "BROWNOUT_DETECTED" for ev in h.power_events)

    @pytest.mark.unit
    def test_init_ignores_transfer_bands_for_warnings(self):
        # v5.1.2 always uses the percentage band; transfer values are
        # informational only. Verify even values that v5.1.1 would have
        # honoured (narrow, in-range transfer points) no longer compute
        # the threshold.
        h = _TestHost(ups_vars={
            "input.voltage.nominal": "120",
            "input.transfer.low": "110",
            "input.transfer.high": "130",
        })
        h._initialize_voltage_thresholds()
        assert h.state.voltage_warning_low == pytest.approx(108.0)
        assert h.state.voltage_warning_high == pytest.approx(132.0)
        # Transfer values still cached for notification context.
        assert h.state.ups_transfer_low == 110.0
        assert h.state.ups_transfer_high == 130.0


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
# v5.1.2 (#4): single percentage-band threshold formula
# ===========================================================================

class TestThresholdFormula:
    """v5.1.2 collapsed the v5.1.1 dual-candidate "tighter of percentage
    or NUT transfer ± buffer" logic into a single percentage-band
    formula. Transfer values stay informational only -- they're cached
    on the state for notification context but no longer compute the
    warning thresholds."""

    @pytest.mark.unit
    @pytest.mark.parametrize("nominal, expected_low, expected_high", [
        (100.0, 90.0, 110.0),
        (110.0, 99.0, 121.0),
        (115.0, 103.5, 126.5),
        (120.0, 108.0, 132.0),
        (127.0, 114.3, 139.7),
        (200.0, 180.0, 220.0),
        (208.0, 187.2, 228.8),
        (220.0, 198.0, 242.0),
        (230.0, 207.0, 253.0),
        (240.0, 216.0, 264.0),
    ])
    def test_normal_preset_at_each_standard_grid(self, nominal, expected_low, expected_high):
        # ±10% from every STANDARD_GRIDS nominal.
        assert _derive_warning_low(nominal, 0.10) == expected_low
        assert _derive_warning_high(nominal, 0.10) == expected_high

    @pytest.mark.unit
    @pytest.mark.parametrize("preset, pct", [
        ("tight", 0.05),
        ("normal", 0.10),
        ("loose", 0.15),
    ])
    def test_resolve_sensitivity_pct(self, preset, pct):
        assert _resolve_sensitivity_pct(preset) == pct

    @pytest.mark.unit
    def test_resolve_sensitivity_unknown_falls_back_to_default(self):
        # Schema validation should reject unknown values at config load,
        # but the mixin defends against direct programmatic use.
        assert _resolve_sensitivity_pct("bogus") == DEFAULT_GRID_QUALITY_DEVIATION_PCT

    @pytest.mark.unit
    @pytest.mark.parametrize("nominal, preset, low, high", [
        # Chris's case (issue #4): every preset.
        (120.0, "tight",  114.0, 126.0),
        (120.0, "normal", 108.0, 132.0),
        (120.0, "loose",  102.0, 138.0),
        # 230V EU grid: every preset.
        (230.0, "tight",  218.5, 241.5),
        (230.0, "normal", 207.0, 253.0),
        (230.0, "loose",  195.5, 264.5),
    ])
    def test_init_applies_preset(self, nominal, preset, low, high):
        h = _TestHost(
            ups_vars={"input.voltage.nominal": str(nominal)},
            voltage_sensitivity=preset,
            voltage_sensitivity_explicit=True,
        )
        h._initialize_voltage_thresholds()
        assert h.state.voltage_warning_low == low
        assert h.state.voltage_warning_high == high
        assert h.state.voltage_deviation_pct == _resolve_sensitivity_pct(preset)

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
        # Warnings = ±10% of nominal regardless of transfer values.
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
        # New v5.1.2 framing: explicit sensitivity preset.
        assert "sensitivity=normal" in joined

    @pytest.mark.unit
    def test_startup_log_omits_ups_switch_line_when_silent(self):
        h = _TestHost(ups_vars={"input.voltage.nominal": "230"})
        h._initialize_voltage_thresholds()
        joined = "\n".join(h.logs)
        assert "Grid-quality warnings:" in joined
        assert "UPS battery-switch points:" not in joined


# ===========================================================================
# v5.1.2 (#4): one-time migration warning when band widens
# ===========================================================================

class TestMigrationWarning:
    """v5.1.1 -> v5.1.2 widens the warning band on UPSes whose firmware
    transfer points were tighter than ±10% of nominal. The daemon emits
    a one-line startup warning so an upgrading operator notices and can
    set ``voltage_sensitivity: tight`` to restore the prior tighter
    behaviour. The warning is suppressed once the operator picks any
    explicit preset."""

    @staticmethod
    def _migration_warning(host) -> str:
        for line in host.logs:
            if "Voltage warning band changed from v5.1.1" in line:
                return line
        return ""

    @pytest.mark.unit
    def test_warning_fires_when_narrow_firmware_widens_band(self):
        # Managed UPS, 230V, transfer 215/245. v5.1.1 would have produced
        # 220/240; v5.1.2 default produces 207/253 -- wider on both sides.
        h = _TestHost(ups_vars={
            "input.voltage.nominal": "230",
            "input.transfer.low": "215",
            "input.transfer.high": "245",
        })
        h._initialize_voltage_thresholds()
        msg = self._migration_warning(h)
        assert msg, "expected migration warning to fire on narrow-firmware UPS"
        # Per-side delta is reported honestly with widened/tightened verbs.
        assert "low 220.0V→207.0V (widened)" in msg
        assert "high 240.0V→253.0V (widened)" in msg
        # Final band is named explicitly.
        assert "(207.0V/253.0V)" in msg
        # Both migration paths are surfaced (tighten OR acknowledge).
        assert "voltage_sensitivity: tight" in msg
        assert "voltage_sensitivity: normal" in msg

    @pytest.mark.unit
    def test_warning_text_per_side_delta_chris(self):
        # Chris's case: both sides widen relative to nominal=120 (low
        # 111->108 moves further below nominal, high 122->132 moves
        # further above). Per-side delta verbs must reflect that.
        h = _TestHost(ups_vars={
            "input.voltage.nominal": "120",
            "input.transfer.low": "106",
            "input.transfer.high": "127",
        })
        h._initialize_voltage_thresholds()
        msg = self._migration_warning(h)
        assert "low 111.0V→108.0V (widened)" in msg
        assert "high 122.0V→132.0V (widened)" in msg

    @pytest.mark.unit
    def test_warning_text_one_sided_for_partial_transfer_data(self):
        # NUT reports only transfer.low (high silent). v5.1.1 would
        # still have produced 220 on low (215+5) but ±10% on high (253);
        # v5.1.2 produces 207/253. Only the low side moved -> the
        # per-side delta lists ONLY the low side.
        h = _TestHost(ups_vars={
            "input.voltage.nominal": "230",
            "input.transfer.low": "215",
        })
        h._initialize_voltage_thresholds()
        msg = self._migration_warning(h)
        assert msg, "expected migration warning when one side widens"
        assert "low 220.0V→207.0V (widened)" in msg
        # High side unchanged -> NOT mentioned.
        assert "high " not in msg

    @pytest.mark.unit
    def test_warning_fires_for_chris_repro_asymmetric_band(self):
        # Chris's case is asymmetric: v5.1.1 produced 111/122, v5.1.2
        # default produces 108/132 -- low side TIGHTENED (108 < 111)
        # while high side WIDENED (132 > 122). The migration-warning
        # gate fires when EITHER side moves in the tightening direction
        # under v5.1.1 (legacy_low > new_low OR legacy_high < new_high),
        # so the warning surfaces here -- 111 > 108 satisfies the
        # low-side branch. That's the right behaviour: any one-sided
        # change of the threshold is worth surfacing once so the
        # operator notices the drift before relying on the new numbers.
        h = _TestHost(ups_vars={
            "input.voltage.nominal": "120",
            "input.transfer.low": "106",
            "input.transfer.high": "127",
        })
        h._initialize_voltage_thresholds()
        msg = self._migration_warning(h)
        assert msg, "asymmetric band change must still surface the warning"

    @pytest.mark.unit
    def test_warning_suppressed_for_wide_firmware_unchanged_case(self):
        # APC default-wide on 230V (170/280): v5.1.1 ±10% won (207/253),
        # v5.1.2 also produces 207/253. No change -> no warning.
        h = _TestHost(ups_vars={
            "input.voltage.nominal": "230",
            "input.transfer.low": "170",
            "input.transfer.high": "280",
        })
        h._initialize_voltage_thresholds()
        assert self._migration_warning(h) == ""

    @pytest.mark.unit
    def test_warning_suppressed_when_no_transfer_points(self):
        # NUT silent on transfer points -> v5.1.1 already used ±10% on
        # both sides -> no change in v5.1.2 -> no warning.
        h = _TestHost(ups_vars={"input.voltage.nominal": "230"})
        h._initialize_voltage_thresholds()
        assert self._migration_warning(h) == ""

    @pytest.mark.unit
    def test_warning_suppressed_when_sensitivity_explicit(self):
        # Even if the band widened, an operator who has set the preset
        # explicitly has made the call -> no nag.
        h = _TestHost(
            ups_vars={
                "input.voltage.nominal": "230",
                "input.transfer.low": "215",
                "input.transfer.high": "245",
            },
            voltage_sensitivity="normal",
            voltage_sensitivity_explicit=True,
        )
        h._initialize_voltage_thresholds()
        assert self._migration_warning(h) == ""


# ===========================================================================
# v5.1.2 (#4): legacy-band recompute helpers used by the migration warning
# ===========================================================================

class TestLegacyHelpers:
    """The v5.1.1 dual-candidate logic survives only inside
    `_legacy_warning_low` / `_legacy_warning_high` so the migration
    warning can compare new vs old. These tests pin the legacy maths
    so any future refactor that touches them surfaces the impact on
    the migration-warning gate."""

    @pytest.mark.unit
    def test_legacy_chris_120_106_127(self):
        # The exact numbers from the bug report.
        assert _legacy_warning_low(120, 106) == 111.0
        assert _legacy_warning_high(120, 127) == 122.0

    @pytest.mark.unit
    def test_legacy_wide_firmware_clamps_to_ten_percent(self):
        # APC 170/280 on 230V: ±10% wins on both sides.
        assert _legacy_warning_low(230, 170) == 207.0
        assert _legacy_warning_high(230, 280) == 253.0

    @pytest.mark.unit
    def test_legacy_no_transfer_uses_ten_percent(self):
        assert _legacy_warning_low(230, None) == 207.0
        assert _legacy_warning_high(230, None) == 253.0

    @pytest.mark.unit
    def test_legacy_rejects_buffered_candidate_crossing_nominal(self):
        # Cubic P1 guard from v5.1.0: lt + 5 < nominal must hold.
        assert _legacy_warning_low(230, 226) == 207.0   # 226+5=231 > 230 -> reject
        assert _legacy_warning_high(230, 234) == 253.0  # 234-5=229 < 230 -> reject

    @pytest.mark.unit
    def test_autodetect_resnap_applies_percentage_band(self):
        # NUT says 230, transfer 170/280. Real grid is 120 -- the
        # autodetect re-snaps to 120V. New thresholds = ±10% of the
        # snapped nominal (the per-UPS sensitivity preset is preserved
        # across the re-snap).
        h = _TestHost(ups_vars={
            "input.voltage.nominal": "230",
            "input.transfer.low": "170",
            "input.transfer.high": "280",
        })
        h._initialize_voltage_thresholds()
        assert h.state.ups_transfer_low == 170.0
        for v in [118, 119, 120, 120, 121, 122, 119, 120, 121, 120]:
            h._check_voltage_autodetect(str(v))
        assert h.state.voltage_autodetect_done
        assert h.state.nominal_voltage == 120.0
        assert h.state.voltage_warning_low == 108.0
        assert h.state.voltage_warning_high == 132.0

    @pytest.mark.unit
    def test_autodetect_resnap_preserves_per_ups_sensitivity(self):
        # If the operator chose `tight` for this UPS, a re-snap to a
        # different nominal must keep tight (5%), not silently revert
        # to normal (10%).
        h = _TestHost(
            ups_vars={"input.voltage.nominal": "230"},
            voltage_sensitivity="tight",
            voltage_sensitivity_explicit=True,
        )
        h._initialize_voltage_thresholds()
        for v in [118, 119, 120, 120, 121, 122, 119, 120, 121, 120]:
            h._check_voltage_autodetect(str(v))
        assert h.state.nominal_voltage == 120.0
        # tight = 5% of 120 = 6V
        assert h.state.voltage_warning_low == 114.0
        assert h.state.voltage_warning_high == 126.0


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
# F1 (5.1.1): severity escalation within the same LOW/HIGH state must
# refresh `voltage_pending_severe` so the immediate-notify bypass fires.
# ===========================================================================

class TestVoltageSeverityEscalation:
    """A brownout that worsens past the severe threshold AFTER the LOW
    state was already pending must lift the pending record's severe
    flag (and refresh the recorded voltage / threshold) so the next
    `_maybe_notify_voltage_pending` fires immediately. Without this,
    operators would wait the full hysteresis window for an alert that
    should have been immediate."""

    @pytest.mark.unit
    def test_mild_then_severe_escalates_to_immediate_notify(self):
        h = _TestHost(ups_vars={"input.voltage.nominal": "230"}, hysteresis=30)
        h._initialize_voltage_thresholds()

        # Poll 1: mild brownout (200V, 13.0% below 230V). Pending state
        # is opened but no notification — dwell in effect.
        h._check_voltage_issues("OL", "200")
        assert h.state.voltage_state == "LOW"
        assert h.state.voltage_pending_state == "LOW"
        assert h.state.voltage_pending_severe is False
        assert h.notifications == []
        assert h.state.voltage_pending_voltage == 200.0

        # Poll 2: deviation worsens past the 15% severe threshold
        # (180V, 21.7% below). State is unchanged (still LOW), so the
        # state-transition branch in _check_voltage_issues doesn't fire.
        # The escalation branch must lift `voltage_pending_severe` and
        # refresh the recorded voltage / threshold; then
        # _maybe_notify_voltage_pending fires immediately.
        h._check_voltage_issues("OL", "180")
        assert h.state.voltage_state == "LOW"  # unchanged
        assert h.state.voltage_pending_severe is True
        assert h.state.voltage_pending_voltage == 180.0
        assert h.notifications, "severity escalation must fire immediate notify"
        body, _ = h.notifications[0]
        assert "BROWNOUT_DETECTED" in body
        assert "(severe," in body
        assert "Notifying immediately" in body

    @pytest.mark.unit
    def test_escalation_does_not_fire_after_already_notified(self):
        # Once a notification has been dispatched, the escalation branch
        # must NOT re-mark the record severe (would be harmless but
        # would muddy the state machine). Reuse the severe-bypass path
        # to consume the notification, then push a more severe reading.
        h = _TestHost(ups_vars={"input.voltage.nominal": "230"}, hysteresis=30)
        h._initialize_voltage_thresholds()
        h._check_voltage_issues("OL", "180")  # severe immediately
        assert h.state.voltage_pending_notified is True
        notif_count = len(h.notifications)

        # Worsen further; pending_notified gate must keep us silent.
        h._check_voltage_issues("OL", "150")
        assert h.state.voltage_pending_notified is True
        assert len(h.notifications) == notif_count, (
            "no second notification should fire while pending is already notified"
        )

    @pytest.mark.unit
    def test_high_side_escalation_also_refreshes(self):
        # Symmetry check on the OVER_VOLTAGE path.
        h = _TestHost(ups_vars={"input.voltage.nominal": "230"}, hysteresis=30)
        h._initialize_voltage_thresholds()
        h._check_voltage_issues("OL", "260")  # mild HIGH (~13%)
        assert h.state.voltage_pending_state == "HIGH"
        assert h.state.voltage_pending_severe is False
        assert h.notifications == []

        h._check_voltage_issues("OL", "280")  # 21.7% — severe
        assert h.state.voltage_pending_severe is True
        assert h.notifications
        body, _ = h.notifications[0]
        assert "OVER_VOLTAGE_DETECTED" in body
        assert "(severe," in body


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
        assert "configured ±10% nominal band" in body

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
    def test_mild_overvoltage_frames_band_as_configured(self):
        # v5.1.2 dropped the hardcoded "EN 50160 ±10%" wording in
        # favour of the actual configured percentage. With the warning
        # band now operator-tunable, naming the percentage and calling
        # it "configured" is more accurate than citing the standard --
        # which only matches the band on `normal` (10%).
        h = _TestHost(ups_vars={
            "input.voltage.nominal": "230",
            "input.transfer.high": "280",
        }, hysteresis=0)
        h._initialize_voltage_thresholds()
        h._check_voltage_issues("OL", "256")  # 11.3% above, mild
        body, _ = h.notifications[0]
        assert "outside the configured ±10% nominal band" in body
        assert "UPS will not switch to battery until 280.0V" in body
        # The legacy "EN 50160 envelope" framing is gone.
        assert "EN 50160" not in body

    @pytest.mark.unit
    def test_mild_overvoltage_band_text_reflects_tight_preset(self):
        h = _TestHost(
            ups_vars={
                "input.voltage.nominal": "230",
                "input.transfer.high": "280",
            },
            hysteresis=0,
            voltage_sensitivity="tight",
            voltage_sensitivity_explicit=True,
        )
        h._initialize_voltage_thresholds()
        # 245V is +6.5% on 230V -- just past tight (5%) warning threshold.
        h._check_voltage_issues("OL", "245")
        body, _ = h.notifications[0]
        assert "outside the configured ±5% nominal band" in body
