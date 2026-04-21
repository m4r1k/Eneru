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

# Voltage-warning derivation: the EN 50160 / IEC 60038 ±10% envelope
# is the operator-relevant "grid quality" band. NUT's transfer points
# are honored only when they're TIGHTER than ±10% (managed UPSes with
# narrow transfer config); otherwise we clamp to ±10% so wide-range
# UPS firmware defaults don't make our warnings useless.
GRID_QUALITY_DEVIATION_PCT = 0.10  # ±10% from nominal = warning band
TRANSFER_BUFFER_V = 5.0            # buffer when using NUT transfer points

# Severity bypass: deviations above this threshold skip the
# voltage_hysteresis_seconds dwell and notify immediately. Mild
# deviations (in the 10-15% band) usually represent grid noise that
# settles within seconds; severe deviations indicate real grid trouble
# (utility fault, generator instability, site wiring) that the
# operator wants to know about NOW, not 30s from now.
VOLTAGE_SEVERE_DEVIATION_PCT = 0.15  # >±15% from nominal = severe


def _snap_to_standard_grid(value: float) -> float:
    """Return the nearest STANDARD_GRIDS entry within tolerance, else value."""
    if value <= 0:
        return value
    nearest = min(STANDARD_GRIDS, key=lambda g: abs(g - value))
    return float(nearest) if abs(nearest - value) <= GRID_SNAP_TOLERANCE else float(value)


def _derive_warning_low(nominal: float, low_transfer) -> float:
    """Pick the warning-low threshold: the *tighter* (higher) of ±10% and (transfer + buffer).

    Tighter = warns earlier = better grid-quality signal. NUT's
    transfer point is honored only when:
      - it's numeric,
      - it's BELOW the nominal (a low-transfer at or above nominal is
        nonsense -- means the UPS would switch on perfectly normal mains),
      - and it's within ±25% of the expected ±10% line.

    The "below nominal" guard catches the bug where a NUT driver
    reports a high value (e.g., 250 on a 230V grid) in the low-transfer
    field; without the guard we'd compute warning_low = 255V and then
    230V mains would falsely fire BROWNOUT_DETECTED. With the guard
    the bogus value is ignored and we fall back to ±10%.

    Rounded to one decimal so the log line and notification text stay
    clean (avoids 253.00000000000003 from float multiplication).
    """
    pct_band = nominal * (1 - GRID_QUALITY_DEVIATION_PCT)
    candidates = [pct_band]
    if is_numeric(low_transfer):
        lt = float(low_transfer)
        if lt < nominal and abs(lt - pct_band) <= nominal * 0.25:
            candidates.append(lt + TRANSFER_BUFFER_V)
    return round(max(candidates), 1)


def _derive_warning_high(nominal: float, high_transfer) -> float:
    """Pick the warning-high threshold: the *tighter* (lower) of ±10% and (transfer - buffer).

    Symmetric guard to ``_derive_warning_low``: high transfer must be
    ABOVE nominal to be plausible. A NUT driver reporting 200 on a 230V
    grid in the high-transfer field would otherwise compute a warning
    of 195V, well below the 207V ±10% threshold -- forcing the warning
    band wider rather than tighter, which defeats the whole clamp.
    """
    pct_band = nominal * (1 + GRID_QUALITY_DEVIATION_PCT)
    candidates = [pct_band]
    if is_numeric(high_transfer):
        ht = float(high_transfer)
        if ht > nominal and abs(ht - pct_band) <= nominal * 0.25:
            candidates.append(ht - TRANSFER_BUFFER_V)
    return round(min(candidates), 1)


class VoltageMonitorMixin:
    """Mixin: voltage thresholds, AVR, bypass, and overload monitoring."""

    def _initialize_voltage_thresholds(self):
        """Initialize voltage thresholds dynamically from UPS data.

        Two-stage process (issue #27):

        1. **Startup snap.** Read ``input.voltage.nominal`` from NUT and
           snap it to the nearest standard grid voltage. Derive
           ``warning_low`` / ``warning_high`` as the *tighter* of the
           ±10% grid-quality band and (NUT ``input.transfer.{low,high}``
           ± buffer) when those are sensible -- never wider than ±10%.
        2. **Observed-range cross-check** (runs each poll until done; see
           ``_check_voltage_autodetect``). If the median of the first
           ~10 observed ``input.voltage`` readings disagrees with NUT's
           nominal by more than ``AUTODETECT_DISCREPANCY_V`` volts, snap
           to the standard grid nearest the observed median and emit a
           ``VOLTAGE_AUTODETECT_MISMATCH`` event so the operator knows
           Eneru second-guessed NUT.

        Eneru's framing has shifted from pure shutdown orchestration to
        grid-quality reporting. Wide UPS firmware transfer points
        (typical APC default: 170 / 280 on 230V) made the previous
        warnings useless -- they fired only ~5V before the UPS itself
        switched to battery. The clamp to ±10% ensures BROWNOUT and
        OVER_VOLTAGE warnings serve the operator's grid-quality
        question, while the UPS-switch points remain available
        separately for notification context.

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

        # Stash the raw transfer values for notification context; they're
        # informational only and never used to gate any decision.
        self.state.ups_transfer_low = (
            float(low_transfer) if is_numeric(low_transfer) else None
        )
        self.state.ups_transfer_high = (
            float(high_transfer) if is_numeric(high_transfer) else None
        )

        nom = self.state.nominal_voltage
        self.state.voltage_warning_low = _derive_warning_low(nom, low_transfer)
        self.state.voltage_warning_high = _derive_warning_high(nom, high_transfer)

        self._log_message(
            f"📊 Voltage Monitoring Active.\n"
            f"   Nominal: {nom}V ({origin}).\n"
            f"   Grid-quality warnings: {self.state.voltage_warning_low}V"
            f" / {self.state.voltage_warning_high}V"
            f" (±{int(GRID_QUALITY_DEVIATION_PCT * 100)}% nominal,"
            f" EN 50160 envelope)."
        )
        if self.state.ups_transfer_low is not None or self.state.ups_transfer_high is not None:
            lo = (f"{self.state.ups_transfer_low}V"
                  if self.state.ups_transfer_low is not None else "?")
            hi = (f"{self.state.ups_transfer_high}V"
                  if self.state.ups_transfer_high is not None else "?")
            self._log_message(
                f"   UPS battery-switch points: {lo} / {hi}"
                f" (from NUT input.transfer.{{low,high}})."
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
            # Reapply the tighter-of clamp against the cached transfer
            # values from startup -- a re-snap shouldn't widen the
            # thresholds beyond ±10% of the new nominal.
            self.state.voltage_warning_low = _derive_warning_low(
                new_nominal, self.state.ups_transfer_low,
            )
            self.state.voltage_warning_high = _derive_warning_high(
                new_nominal, self.state.ups_transfer_high,
            )
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
        """Check for voltage quality issues, with severity-aware hysteresis.

        Mild deviations (10-15% from nominal) go through the
        ``voltage_hysteresis_seconds`` dwell so flap from neighbour
        appliances doesn't spam notifications. Severe deviations
        (>±15%) bypass the dwell and notify immediately -- those signal
        real grid trouble where the operator wants to know now.
        """
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
        nominal = self.state.nominal_voltage
        deviation_pct = (
            abs(voltage - nominal) / nominal if nominal > 0 else 0.0
        )
        is_severe = deviation_pct > VOLTAGE_SEVERE_DEVIATION_PCT

        if voltage < self.state.voltage_warning_low:
            target = "LOW"
            event = "BROWNOUT_DETECTED"
            threshold = self.state.voltage_warning_low
            detail = self._format_voltage_detail(
                "low", voltage, threshold, nominal, deviation_pct, is_severe,
            )
        elif voltage > self.state.voltage_warning_high:
            target = "HIGH"
            event = "OVER_VOLTAGE_DETECTED"
            threshold = self.state.voltage_warning_high
            detail = self._format_voltage_detail(
                "high", voltage, threshold, nominal, deviation_pct, is_severe,
            )
        else:
            target = "NORMAL"
            event = None
            threshold = 0.0
            detail = ""

        # State log line is sacred -- always written immediately on
        # transition. The notification path is gated by the hysteresis
        # logic in _maybe_notify_voltage_pending unless severe.
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
            self._set_voltage_pending(
                target, voltage, threshold, is_severe=is_severe,
            )

        # Re-evaluate the pending notification each poll regardless of
        # whether the state changed -- the hysteresis fires when the
        # dwell time elapses, not on a state transition.
        self._maybe_notify_voltage_pending()

    # ----- helpers (B2: notification hysteresis + severity bypass) -----

    def _hysteresis_seconds(self) -> int:
        """Return the configured dwell, defaulting to 0 if unset."""
        try:
            return max(0, int(self.config.notifications.voltage_hysteresis_seconds))
        except (AttributeError, TypeError, ValueError):
            return 0

    def _format_voltage_detail(self, direction: str, voltage: float,
                               threshold: float, nominal: float,
                               deviation_pct: float, is_severe: bool,
                               annotation: str = "") -> str:
        """Build the detail string used by both the log line and the notification.

        Structure is always:
          ``<head>. [<annotation>] [<ups_switch_context>]``

        Mild events get "% deviation + threshold + UPS-switch context"
        (operator can see this is a grid-quality issue, not an imminent
        UPS reaction). Severe events get a "(severe, X.X% ...)" tag and
        an "approaching UPS battery-switch threshold" callout when the
        UPS is likely to react soon. ``annotation`` (e.g.
        ``"Persisted 30s."`` or ``"Notifying immediately."``) is
        injected between head and tail when supplied.
        """
        relative = "below" if direction == "low" else "above"
        pct_str = f"{deviation_pct * 100:.1f}%"

        if is_severe:
            head = (f"(severe, {pct_str} {relative} nominal): "
                    f"input voltage {voltage}V.")
        else:
            head = (f"input voltage {voltage}V is {pct_str} {relative} "
                    f"{int(nominal)}V nominal "
                    f"(warning threshold {threshold}V).")

        # UPS-switch context -- only when NUT exposes the matching
        # transfer point. For severe events, frame as "battery may
        # engage shortly"; for mild, frame as "this is a grid quality
        # issue, not an imminent power loss".
        # NOTE: do NOT imply EN 50160 considers the UPS switch point
        # acceptable. EN 50160 caps at nominal × 1.1 (e.g., 253V on
        # 230V), well below typical UPS switch points (e.g., 280V).
        # The two are independent thresholds with different purposes.
        ups_switch = (self.state.ups_transfer_low if direction == "low"
                      else self.state.ups_transfer_high)
        if ups_switch is not None:
            if is_severe:
                tail = (f"Approaching UPS battery-switch threshold "
                        f"({ups_switch}V) -- battery may engage shortly.")
            else:
                tail = (f"UPS will not switch to battery until "
                        f"{ups_switch}V (firmware setting); this is "
                        f"a grid-quality issue (outside the EN 50160 "
                        f"±10% envelope), not an imminent power loss.")
        else:
            tail = ""

        parts = [head]
        if annotation:
            parts.append(annotation)
        if tail:
            parts.append(tail)
        return " ".join(parts)

    def _set_voltage_pending(self, target: str, voltage: float,
                             threshold: float, *, is_severe: bool = False):
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
        self.state.voltage_pending_severe = is_severe
        # Severe deviations bypass the dwell (notify on this same poll);
        # hysteresis = 0 also fires immediately (legacy behavior).
        if is_severe or self._hysteresis_seconds() == 0:
            self._maybe_notify_voltage_pending()

    def _clear_voltage_pending(self):
        self.state.voltage_pending_state = ""
        self.state.voltage_pending_since = 0.0
        self.state.voltage_pending_voltage = 0.0
        self.state.voltage_pending_threshold = 0.0
        self.state.voltage_pending_notified = False
        self.state.voltage_pending_severe = False

    def _maybe_notify_voltage_pending(self):
        """Fire the deferred notification when the dwell time elapses (or immediately if severe)."""
        target = self.state.voltage_pending_state
        if not target or self.state.voltage_pending_notified:
            return
        elapsed = time.time() - self.state.voltage_pending_since
        is_severe = self.state.voltage_pending_severe
        # Severe events skip the dwell entirely; otherwise honor it.
        if not is_severe and elapsed < self._hysteresis_seconds():
            return
        v = self.state.voltage_pending_voltage
        t = self.state.voltage_pending_threshold
        nominal = self.state.nominal_voltage
        deviation_pct = abs(v - nominal) / nominal if nominal > 0 else 0.0
        elapsed_int = int(round(elapsed))
        direction = "low" if target == "LOW" else "high"
        event = ("BROWNOUT_DETECTED" if target == "LOW"
                 else "OVER_VOLTAGE_DETECTED")

        annotation = ("Notifying immediately (bypassed hysteresis)."
                      if is_severe
                      else f"Persisted {elapsed_int}s.")
        detail = self._format_voltage_detail(
            direction, v, t, nominal, deviation_pct, is_severe,
            annotation=annotation,
        )
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
