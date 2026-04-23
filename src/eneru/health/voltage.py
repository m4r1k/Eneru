"""Voltage / AVR / bypass / overload health monitoring.

Translates raw NUT status flags and ``input.voltage`` readings into
power-event log lines (``BROWNOUT_DETECTED``, ``OVER_VOLTAGE_DETECTED``,
``AVR_BOOST_ACTIVE`` etc.) and tracks per-state transitions on
``self.state``.
"""

import statistics
import time

from eneru.config import VOLTAGE_SENSITIVITY_PRESETS
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

# Default percentage when the per-monitor sensitivity preset can't be
# resolved (config schema validation should normally prevent this).
# Matches the EN 50160 / IEC 60038 ±10% envelope.
DEFAULT_GRID_QUALITY_DEVIATION_PCT = 0.10

# v5.1.1 legacy constants -- retained ONLY for the migration warning
# computation. v5.1.2's threshold formula is a single percentage band;
# the legacy "tighter of percentage or (transfer ± 5V)" candidate is
# computed at startup purely so we can detect cases where the new band
# is wider than the old one would have been on this UPS, and prompt
# the operator to set ``voltage_sensitivity: tight`` if they want the
# pre-5.1.2 behaviour back.
#
# TODO(v5.2): drop these constants together with `_legacy_warning_low`
# / `_legacy_warning_high` and `_maybe_log_voltage_migration_warning`
# once the upgrade window is past (target: when v5.1.2 has been on
# every changelog for at least two minor releases). At that point
# operators upgrading directly from v5.1.1 are rare enough that the
# nag is more cost than benefit.
_LEGACY_GRID_QUALITY_DEVIATION_PCT = 0.10
_LEGACY_TRANSFER_BUFFER_V = 5.0

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


def _resolve_sensitivity_pct(sensitivity) -> float:
    """Map a ``voltage_sensitivity`` preset string to a deviation fraction.

    Falls back to the EN 50160 ±10% envelope on any unknown value --
    schema validation should reject typos at config load, so this is
    only a safety net for direct programmatic instantiation in tests
    or for the case where validation was skipped (e.g.,
    ``ConfigLoader.load_dict`` callers that bypass ``validate_config``).
    Non-string inputs (lists, dicts from a malformed YAML) hit the
    ``isinstance`` guard before the dict lookup so we never raise
    ``TypeError`` on the safety-critical voltage-init path.
    """
    if not isinstance(sensitivity, str):
        return DEFAULT_GRID_QUALITY_DEVIATION_PCT
    return VOLTAGE_SENSITIVITY_PRESETS.get(
        sensitivity, DEFAULT_GRID_QUALITY_DEVIATION_PCT,
    )


def _derive_warning_low(nominal: float, pct: float) -> float:
    """Warning-low threshold: ``nominal × (1 − pct)``, rounded to 1 decimal.

    Single-source formula -- the v5.1.1 dual-candidate "tighter of
    percentage or transfer ± buffer" logic was dropped because it
    conflated grid-quality reporting with "approaching firmware
    transfer point" and produced operator-confusing thresholds on
    narrow-firmware UPSes (issue #4).
    """
    return round(nominal * (1 - pct), 1)


def _derive_warning_high(nominal: float, pct: float) -> float:
    """Warning-high threshold: ``nominal × (1 + pct)``, rounded to 1 decimal."""
    return round(nominal * (1 + pct), 1)


def _legacy_warning_low(nominal: float, low_transfer) -> float:
    """Recompute the v5.1.1 warning_low for migration-warning comparison only."""
    pct_band = nominal * (1 - _LEGACY_GRID_QUALITY_DEVIATION_PCT)
    candidates = [pct_band]
    if is_numeric(low_transfer):
        lt = float(low_transfer)
        candidate = lt + _LEGACY_TRANSFER_BUFFER_V
        if (lt < nominal
                and candidate < nominal
                and abs(lt - pct_band) <= nominal * 0.25):
            candidates.append(candidate)
    return round(max(candidates), 1)


def _legacy_warning_high(nominal: float, high_transfer) -> float:
    """Recompute the v5.1.1 warning_high for migration-warning comparison only."""
    pct_band = nominal * (1 + _LEGACY_GRID_QUALITY_DEVIATION_PCT)
    candidates = [pct_band]
    if is_numeric(high_transfer):
        ht = float(high_transfer)
        candidate = ht - _LEGACY_TRANSFER_BUFFER_V
        if (ht > nominal
                and candidate > nominal
                and abs(ht - pct_band) <= nominal * 0.25):
            candidates.append(candidate)
    return round(min(candidates), 1)


class VoltageMonitorMixin:
    """Mixin: voltage thresholds, AVR, bypass, and overload monitoring."""

    def _initialize_voltage_thresholds(self):
        """Initialize voltage thresholds dynamically from UPS data.

        Two-stage process (issue #27):

        1. **Startup snap.** Read ``input.voltage.nominal`` from NUT and
           snap it to the nearest standard grid voltage. Derive
           ``warning_low`` / ``warning_high`` as ``nominal × (1 ∓ pct)``
           where ``pct`` comes from
           ``triggers.voltage_sensitivity`` (tight=5%, normal=10%,
           loose=15%; default normal = EN 50160 / IEC 60038 envelope).
        2. **Observed-range cross-check** (runs each poll until done; see
           ``_check_voltage_autodetect``). If the median of the first
           ~10 observed ``input.voltage`` readings disagrees with NUT's
           nominal by more than ``AUTODETECT_DISCREPANCY_V`` volts, snap
           to the standard grid nearest the observed median and emit a
           ``VOLTAGE_AUTODETECT_MISMATCH`` event so the operator knows
           Eneru second-guessed NUT.

        v5.1.2 dropped the v5.1.0/5.1.1 dual-candidate "tighter of ±10%
        or NUT transfer ± 5V" logic in favour of the single percentage
        formula above (issue #4). The UPS firmware transfer points
        remain available as informational context (printed alongside the
        warning band, quoted in ``BROWNOUT_DETECTED`` /
        ``OVER_VOLTAGE_DETECTED`` event detail) but no longer compute
        thresholds. On UPSes whose transfer points were narrower than
        the percentage band, the new formula widens the warning band;
        a one-line migration warning fires at startup unless
        ``voltage_sensitivity`` is explicitly set in YAML, so an
        upgrading operator notices and can opt back into a tighter
        preset.

        We intentionally do NOT expose any ``warning_low`` /
        ``warning_high`` / ``nominal_override`` config keys: a
        misconfiguration there would mask real over-voltage events that
        damage hardware. The bounded preset is the escape hatch.
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
        sensitivity = getattr(
            self.config.triggers, "voltage_sensitivity", "normal",
        )
        pct = _resolve_sensitivity_pct(sensitivity)
        self.state.voltage_deviation_pct = pct
        self.state.voltage_warning_low = _derive_warning_low(nom, pct)
        self.state.voltage_warning_high = _derive_warning_high(nom, pct)

        self._log_message(
            f"📊 Voltage Monitoring Active.\n"
            f"   Nominal: {nom}V ({origin}).\n"
            f"   Grid-quality warnings: {self.state.voltage_warning_low}V"
            f" / {self.state.voltage_warning_high}V"
            f" (±{int(pct * 100)}% nominal,"
            f" sensitivity={sensitivity})."
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

        self._maybe_log_voltage_migration_warning(
            nom, low_transfer, high_transfer, sensitivity,
        )

    def _maybe_log_voltage_migration_warning(
        self, nominal: float, low_transfer, high_transfer, sensitivity: str,
    ):
        """Warn at startup if v5.1.1 would have produced a tighter band on either side.

        Suppressed when ``voltage_sensitivity`` was set explicitly in
        YAML (the operator has already chosen). Fires only when the
        legacy candidate would have been tighter than the new
        percentage band on at least one side -- the wide-firmware case
        (where ±10% always won under v5.1.1) sees no warning. Drops
        in v5.2 once the upgrade is well past.
        """
        if getattr(self.config.triggers, "voltage_sensitivity_explicit", False):
            return
        new_low = self.state.voltage_warning_low
        new_high = self.state.voltage_warning_high
        legacy_low = _legacy_warning_low(nominal, low_transfer)
        legacy_high = _legacy_warning_high(nominal, high_transfer)
        # "Tighter" = closer to nominal: legacy_low > new_low (warning
        # fired sooner on the way down) or legacy_high < new_high
        # (warning fired sooner on the way up). If neither side moved
        # in the tightening direction, the upgrade didn't change the
        # band for this operator -- nothing to warn about.
        if legacy_low <= new_low and legacy_high >= new_high:
            return
        # Per-side delta in honest words. The asymmetric case (e.g.
        # Chris's 120V/106/127: 111->108 widens low, 122->132 widens
        # high; 215/245 on 230V: 220->207 widens low, 240->253 widens
        # high) gets an accurate "low ... ; high ..." breakdown.
        sides = []
        if legacy_low != new_low:
            verb = "widened" if new_low < legacy_low else "tightened"
            sides.append(f"low {legacy_low}V→{new_low}V ({verb})")
        if legacy_high != new_high:
            verb = "widened" if new_high > legacy_high else "tightened"
            sides.append(f"high {legacy_high}V→{new_high}V ({verb})")
        delta = "; ".join(sides) if sides else "(no change)"
        self._log_message(
            f"⚠️ Voltage warning band changed from v5.1.1 on this UPS: "
            f"{delta}. v5.1.2 dropped the tighter-of-percentage-or-transfer "
            f"clamp in favour of a single percentage-band formula (issue #4). "
            f"Current band is ±{int(self.state.voltage_deviation_pct * 100)}% "
            f"nominal ({new_low}V/{new_high}V). Set "
            f"'voltage_sensitivity: tight' under this UPS's triggers block "
            f"to restore a tighter band, or set "
            f"'voltage_sensitivity: normal' to acknowledge the new default "
            f"and silence this warning. See "
            f"https://eneru.readthedocs.io/latest/changelog/ for details."
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
            # Reapply the percentage band against the new nominal.
            # voltage_deviation_pct was set by _initialize_voltage_thresholds
            # from the per-UPS sensitivity preset; preserve it across the
            # re-snap so the operator's chosen sensitivity carries through.
            pct = self.state.voltage_deviation_pct
            self.state.voltage_warning_low = _derive_warning_low(new_nominal, pct)
            self.state.voltage_warning_high = _derive_warning_high(new_nominal, pct)
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
        else:
            # Severity escalation within the same state: a brownout that
            # was mild on the previous poll may now have crossed the
            # severe-deviation threshold. _set_voltage_pending only fires
            # on state transition, so without this update the pending
            # record keeps the original is_severe=False and the dwell
            # bypass for severe events never triggers.
            if (
                is_severe
                and self.state.voltage_pending_state in ("HIGH", "LOW")
                and not self.state.voltage_pending_severe
                and not self.state.voltage_pending_notified
            ):
                self.state.voltage_pending_severe = True
                self.state.voltage_pending_voltage = voltage
                self.state.voltage_pending_threshold = threshold

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
        # NOTE: keep wording independent of EN 50160 -- the warning
        # band is now operator-configurable via voltage_sensitivity,
        # so a "the spec says..." framing would mislead anyone running
        # `tight` (5%) or `loose` (15%).
        ups_switch = (self.state.ups_transfer_low if direction == "low"
                      else self.state.ups_transfer_high)
        if ups_switch is not None:
            if is_severe:
                tail = (f"Approaching UPS battery-switch threshold "
                        f"({ups_switch}V) -- battery may engage shortly.")
            else:
                pct = self.state.voltage_deviation_pct
                tail = (f"UPS will not switch to battery until "
                        f"{ups_switch}V (firmware setting); this is "
                        f"a grid-quality issue (outside the configured "
                        f"±{int(pct * 100)}% nominal band), not an "
                        f"imminent power loss.")
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
