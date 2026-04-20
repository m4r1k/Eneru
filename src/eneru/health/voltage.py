"""Voltage / AVR / bypass / overload health monitoring.

Translates raw NUT status flags and ``input.voltage`` readings into
power-event log lines (``BROWNOUT_DETECTED``, ``OVER_VOLTAGE_DETECTED``,
``AVR_BOOST_ACTIVE`` etc.) and tracks per-state transitions on
``self.state``.
"""

import statistics
import time

from eneru.utils import is_numeric


# Standard grid voltages, sorted ascending. ``input.voltage.nominal``
# from NUT is snapped to the nearest entry within ``GRID_SNAP_TOLERANCE``
# volts; otherwise the raw NUT value is kept. The set covers Japan
# (100), the Americas (110/115/120/127), and EU/UK/Asia (200/208/220/
# 230/240). Adding more is safe -- the snap is monotonic.
STANDARD_GRIDS = (100, 110, 115, 120, 127, 200, 208, 220, 230, 240)
GRID_SNAP_TOLERANCE = 15.0  # V

# How many observed input.voltage readings to collect before we
# cross-check NUT's reported nominal. Small enough that the validation
# completes within ~10 s of startup at 1 Hz polling, big enough to
# average out the kind of single-poll noise that comes from older NUT
# drivers.
AUTODETECT_OBSERVATION_COUNT = 10
# Discrepancy tolerance between NUT's nominal and the median of
# observed readings. A US 120 V grid + NUT-reports-230 will easily
# clear this; a clean EU 230 V grid + NUT-reports-230 with normal
# small fluctuations stays inside.
AUTODETECT_DISCREPANCY_V = 25.0


def _snap_to_standard_grid(value: float) -> float:
    """Return the nearest STANDARD_GRIDS entry within tolerance, else value."""
    if value <= 0:
        return value
    nearest = min(STANDARD_GRIDS, key=lambda g: abs(g - value))
    return float(nearest) if abs(nearest - value) <= GRID_SNAP_TOLERANCE else float(value)


class VoltageMonitorMixin:
    """Mixin: voltage thresholds, AVR, bypass, and overload monitoring."""

    def _initialize_voltage_thresholds(self):
        """Initialize voltage thresholds dynamically from UPS data.

        Two-stage process (issue #27):

        1. **Startup snap.** Read ``input.voltage.nominal`` from NUT and
           snap it to the nearest standard grid voltage. Derive
           ``warning_low`` / ``warning_high`` as ±10% (or from
           ``input.transfer.{low,high}`` if NUT exposes those AND they
           sit within sanity bounds of the snapped nominal).
        2. **Observed-range cross-check** (runs each poll until done; see
           ``_check_voltage_autodetect``). If the median of the first
           ~10 observed ``input.voltage`` readings disagrees with NUT's
           nominal by more than ``AUTODETECT_DISCREPANCY_V`` volts, snap
           to the standard grid nearest the observed median and emit a
           ``VOLTAGE_AUTODETECT_MISMATCH`` event so the operator knows
           Eneru second-guessed NUT.

        We intentionally do NOT expose any ``warning_low`` /
        ``warning_high`` / ``nominal_override`` config keys: a
        misconfiguration there would mask real over-voltage events that
        damage hardware. The auto-detect path solves the ``[FEATURE]
        Overvoltage alerts`` (#27) US-grid pain without that risk.
        """
        nominal_raw = self._get_ups_var("input.voltage.nominal")
        low_transfer = self._get_ups_var("input.transfer.low")
        high_transfer = self._get_ups_var("input.transfer.high")

        if is_numeric(nominal_raw):
            raw_nominal = float(nominal_raw)
            snapped = _snap_to_standard_grid(raw_nominal)
            self.state.nominal_voltage = snapped
            origin = (f"NUT={raw_nominal}, snapped" if snapped != raw_nominal
                      else f"NUT={raw_nominal}")
        else:
            self.state.nominal_voltage = 230.0
            origin = "NUT=missing, default"

        # Apply NUT's transfer bands only if they bracket the snapped
        # nominal sensibly (within ±25%); otherwise fall back to ±10%.
        nom = self.state.nominal_voltage
        if is_numeric(low_transfer) and abs(float(low_transfer) - nom * 0.9) <= nom * 0.25:
            self.state.voltage_warning_low = float(low_transfer) + 5
        else:
            self.state.voltage_warning_low = nom * 0.9

        if is_numeric(high_transfer) and abs(float(high_transfer) - nom * 1.1) <= nom * 0.25:
            self.state.voltage_warning_high = float(high_transfer) - 5
        else:
            self.state.voltage_warning_high = nom * 1.1

        self._log_message(
            f"📊 Voltage Monitoring Active. Nominal: {self.state.nominal_voltage}V "
            f"({origin}). Low Warning: {self.state.voltage_warning_low}V. "
            f"High Warning: {self.state.voltage_warning_high}V."
        )

    def _check_voltage_autodetect(self, input_voltage: str):
        """Once-per-startup observed-range cross-check (see issue #27).

        Pushes the latest ``input.voltage`` into a fixed-size rolling
        deque. When the deque fills, computes the median and -- if it
        disagrees with NUT's nominal by more than the tolerance --
        re-snaps the nominal + thresholds and emits a
        ``VOLTAGE_AUTODETECT_MISMATCH`` event. After that, the
        cross-check is permanently disabled for this monitor.
        """
        if self.state.voltage_autodetect_done:
            return
        if not is_numeric(input_voltage):
            return
        v = float(input_voltage)
        # Reject impossible readings (some NUT drivers emit 0 while OB).
        if v <= 0 or v > 600:
            return
        self.state.voltage_observed.append(v)
        if len(self.state.voltage_observed) < AUTODETECT_OBSERVATION_COUNT:
            return

        median = statistics.median(self.state.voltage_observed)
        old_nominal = self.state.nominal_voltage
        if abs(median - old_nominal) > AUTODETECT_DISCREPANCY_V:
            new_nominal = _snap_to_standard_grid(median)
            self.state.nominal_voltage = new_nominal
            self.state.voltage_warning_low = new_nominal * 0.9
            self.state.voltage_warning_high = new_nominal * 1.1
            self._log_message(
                f"📊 Voltage auto-detect re-snap: NUT={old_nominal}V "
                f"disagreed with observed median {median:.1f}V "
                f"(window={list(self.state.voltage_observed)}V). "
                f"Re-snapped to {new_nominal}V; "
                f"new thresholds {self.state.voltage_warning_low}V / "
                f"{self.state.voltage_warning_high}V."
            )
            self._log_power_event(
                "VOLTAGE_AUTODETECT_MISMATCH",
                f"NUT nominal={old_nominal}V, observed median={median:.1f}V, "
                f"re-snapped to {new_nominal}V"
            )
        self.state.voltage_autodetect_done = True

    def _check_voltage_issues(self, ups_status: str, input_voltage: str):
        """Check for voltage quality issues, with notification hysteresis."""
        # Cross-check NUT's reported nominal against observed reality.
        # Runs only until enough samples accumulate; cheap no-op after.
        self._check_voltage_autodetect(input_voltage)

        if "OL" not in ups_status:
            if "OB" in ups_status or "FSD" in ups_status:
                self.state.voltage_state = "NORMAL"
                self._clear_voltage_pending()
            return

        if not is_numeric(input_voltage):
            return

        voltage = float(input_voltage)

        if voltage < self.state.voltage_warning_low:
            target = "LOW"
            event = "BROWNOUT_DETECTED"
            threshold = self.state.voltage_warning_low
            detail = f"Voltage is low: {voltage}V (Threshold: {threshold}V)"
        elif voltage > self.state.voltage_warning_high:
            target = "HIGH"
            event = "OVER_VOLTAGE_DETECTED"
            threshold = self.state.voltage_warning_high
            detail = f"Voltage is high: {voltage}V (Threshold: {threshold}V)"
        else:
            target = "NORMAL"
            event = None
            threshold = 0.0
            detail = ""

        # State log line is sacred -- always written immediately on
        # transition. The notification path is gated by the hysteresis
        # logic in _maybe_notify_voltage_pending.
        if target != self.state.voltage_state:
            if target == "NORMAL":
                self._log_power_event(
                    "VOLTAGE_NORMALIZED",
                    f"Voltage returned to normal: {voltage}V. "
                    f"Previous state: {self.state.voltage_state}",
                    suppress_notification=True,
                )
                # Cancel any pending HIGH/LOW notify -- it's a flap.
                self._record_voltage_flap_if_pending(voltage)
            else:
                self._log_power_event(
                    event, detail,
                    suppress_notification=True,  # gated by hysteresis below
                )
            self.state.voltage_state = target
            self._set_voltage_pending(target, voltage, threshold)

        # Re-evaluate the pending notification each poll regardless of
        # whether the state changed -- the hysteresis fires when the
        # dwell time elapses, not on a state transition.
        self._maybe_notify_voltage_pending()

    # ----- helpers (B2: notification hysteresis) -----

    def _hysteresis_seconds(self) -> int:
        """Return the configured dwell, defaulting to 0 if unset."""
        try:
            return max(0, int(self.config.notifications.voltage_hysteresis_seconds))
        except (AttributeError, TypeError, ValueError):
            return 0

    def _set_voltage_pending(self, target: str, voltage: float, threshold: float):
        """Open a new pending notification window for a HIGH/LOW transition."""
        if target not in ("HIGH", "LOW"):
            self._clear_voltage_pending()
            return
        # Only reset the dwell start when the *direction* changes; if
        # the state oscillates LOW→NORMAL→LOW within the dwell, the
        # second LOW restarts the timer (it's a different excursion).
        self.state.voltage_pending_state = target
        self.state.voltage_pending_since = time.time()
        self.state.voltage_pending_voltage = voltage
        self.state.voltage_pending_threshold = threshold
        self.state.voltage_pending_notified = False
        # Hysteresis = 0 means "behave like the legacy code path":
        # the notification fires immediately on this same poll.
        if self._hysteresis_seconds() == 0:
            self._maybe_notify_voltage_pending()

    def _clear_voltage_pending(self):
        self.state.voltage_pending_state = ""
        self.state.voltage_pending_since = 0.0
        self.state.voltage_pending_voltage = 0.0
        self.state.voltage_pending_threshold = 0.0
        self.state.voltage_pending_notified = False

    def _maybe_notify_voltage_pending(self):
        """Fire the deferred notification when the dwell time elapses."""
        target = self.state.voltage_pending_state
        if not target or self.state.voltage_pending_notified:
            return
        elapsed = time.time() - self.state.voltage_pending_since
        if elapsed < self._hysteresis_seconds():
            return
        # Dwell elapsed and condition still holds -- send the real
        # notification with a `(persisted Ns)` annotation.
        v = self.state.voltage_pending_voltage
        t = self.state.voltage_pending_threshold
        elapsed_int = int(round(elapsed))
        if target == "LOW":
            event = "BROWNOUT_DETECTED"
            detail = (f"Voltage is low: {v}V (Threshold: {t}V) "
                      f"(persisted {elapsed_int}s)")
        else:
            event = "OVER_VOLTAGE_DETECTED"
            detail = (f"Voltage is high: {v}V (Threshold: {t}V) "
                      f"(persisted {elapsed_int}s)")
        # Notify only -- the BROWNOUT/OVER_VOLTAGE log+event row was
        # already written when we set the pending state, so don't
        # double-log. The notification dispatch itself is on the
        # safety-critical path (cannot be suppressed via
        # notifications.suppress -- enforced in config validation).
        self._send_voltage_notification(event, detail)
        self.state.voltage_pending_notified = True

    def _send_voltage_notification(self, event: str, detail: str):
        """Dispatch a single voltage notification (no extra log line)."""
        # Reuse the existing _log_power_event payload formatter by
        # calling _send_notification directly; we already wrote the log
        # row when the state transitioned, and we're explicitly
        # bypassing _log_power_event to avoid double-logging.
        body = f"⚠️ **VOLTAGE ISSUE:** {event}\nDetails: {detail}"
        try:
            self._send_notification(body, self.config.NOTIFY_WARNING)
        except Exception:
            pass

    def _record_voltage_flap_if_pending(self, voltage: float):
        """If the prior HIGH/LOW state never fired its notification, log a flap."""
        prior = self.state.voltage_pending_state
        if prior and not self.state.voltage_pending_notified:
            duration = int(round(time.time() - self.state.voltage_pending_since))
            peak = self.state.voltage_pending_voltage
            self._log_power_event(
                "VOLTAGE_FLAP_SUPPRESSED",
                f"state={prior} duration={duration}s peak={peak}V "
                f"(below hysteresis threshold; no notification sent)",
                suppress_notification=True,
            )
        self._clear_voltage_pending()

    def _check_avr_status(self, ups_status: str, input_voltage: str):
        """Check for Automatic Voltage Regulation activity."""
        voltage_str = f"{input_voltage}V" if is_numeric(input_voltage) else "N/A"

        if "BOOST" in ups_status:
            if self.state.avr_state != "BOOST":
                self._log_power_event(
                    "AVR_BOOST_ACTIVE",
                    f"Input voltage low ({voltage_str}). UPS is boosting output."
                )
                self.state.avr_state = "BOOST"
        elif "TRIM" in ups_status:
            if self.state.avr_state != "TRIM":
                self._log_power_event(
                    "AVR_TRIM_ACTIVE",
                    f"Input voltage high ({voltage_str}). UPS is trimming output."
                )
                self.state.avr_state = "TRIM"
        elif self.state.avr_state != "INACTIVE":
            self._log_power_event("AVR_INACTIVE", f"AVR is inactive. Input voltage: {voltage_str}.")
            self.state.avr_state = "INACTIVE"

    def _check_bypass_status(self, ups_status: str):
        """Check for bypass mode."""
        if "BYPASS" in ups_status:
            if self.state.bypass_state != "ACTIVE":
                self._log_power_event("BYPASS_MODE_ACTIVE", "UPS in bypass mode - no protection active!")
                self.state.bypass_state = "ACTIVE"
        elif self.state.bypass_state != "INACTIVE":
            self._log_power_event("BYPASS_MODE_INACTIVE", "UPS left bypass mode.")
            self.state.bypass_state = "INACTIVE"

    def _check_overload_status(self, ups_status: str, ups_load: str):
        """Check for overload condition."""
        if "OVER" in ups_status:
            if self.state.overload_state != "ACTIVE":
                self._log_power_event("OVERLOAD_ACTIVE", f"UPS overload detected! Load: {ups_load}%")
                self.state.overload_state = "ACTIVE"
        elif self.state.overload_state != "INACTIVE":
            reported_load = str(ups_load) if is_numeric(ups_load) else "N/A"
            self._log_power_event("OVERLOAD_RESOLVED", f"UPS overload resolved. Load: {reported_load}%")
            self.state.overload_state = "INACTIVE"
