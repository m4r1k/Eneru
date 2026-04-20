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
    AUTODETECT_OBSERVATION_COUNT,
    _snap_to_standard_grid,
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
    def test_init_uses_transfer_bands_when_in_range(self):
        h = _TestHost(ups_vars={
            "input.voltage.nominal": "120",
            "input.transfer.low": "100",
            "input.transfer.high": "140",
        })
        h._initialize_voltage_thresholds()
        # 100+5 / 140-5 (NUT-derived).
        assert h.state.voltage_warning_low == 105.0
        assert h.state.voltage_warning_high == 135.0

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
        assert "persisted" in body  # the dwell-annotation is present

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
        h = _TestHost(ups_vars={"input.voltage.nominal": "230"}, hysteresis=30)
        h._initialize_voltage_thresholds()
        h._check_voltage_issues("OL", "180")  # below 207V (= 230*0.9)
        assert h.state.voltage_state == "LOW"
        assert h.notifications == []
        # And the immediate log/event fires (with suppress_notification=True).
        assert any(ev[0] == "BROWNOUT_DETECTED" and ev[2]
                   for ev in h.power_events)
