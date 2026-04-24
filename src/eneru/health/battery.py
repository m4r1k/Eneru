"""Battery depletion-rate tracking and anomaly detection.

Owns the rolling battery-history file (``self._battery_history_path``) used
to compute depletion rate over a configurable window, plus sustained-reading
confirmation of charge anomalies that survive 3 consecutive polls (filters
firmware jitter from APC / CyberPower / UniFi UPS units).
"""

import time
from collections import deque
from typing import Dict

from eneru.utils import is_numeric


class BatteryMonitorMixin:
    """Mixin: battery depletion-rate calculation and anomaly detection."""

    def _calculate_depletion_rate(self, current_battery: str) -> float:
        """Calculate battery depletion rate based on history."""
        current_time = int(time.time())

        if not is_numeric(current_battery):
            return 0.0

        current_battery_float = float(current_battery)
        cutoff_time = current_time - self.config.triggers.depletion.window

        self.state.battery_history = deque(
            [(ts, bat) for ts, bat in self.state.battery_history if ts >= cutoff_time],
            maxlen=1000
        )
        self.state.battery_history.append((current_time, current_battery_float))

        try:
            # with_name(name + '.tmp') preserves the per-UPS suffix on the
            # path (e.g. 'ups-battery-history.ups1') so concurrent writers
            # in multi-UPS mode never share a temp file. with_suffix('.tmp')
            # would replace the per-UPS suffix and race on the rename.
            temp_file = self._battery_history_path.with_name(
                self._battery_history_path.name + '.tmp'
            )
            with open(temp_file, 'w') as f:
                for ts, bat in self.state.battery_history:
                    f.write(f"{ts}:{bat}\n")
            temp_file.replace(self._battery_history_path)
        except Exception as exc:
            # Persisting battery history is best-effort; the in-memory deque
            # is the source of truth and a single failed write doesn't break
            # depletion calculations. Log so silent disk errors are visible.
            self._log_message(
                f"⚠️ Battery history persist failed: {exc}"
            )

        if len(self.state.battery_history) < 30:
            return 0.0

        oldest_time, oldest_battery = self.state.battery_history[0]
        time_diff = current_time - oldest_time

        if time_diff > 0:
            battery_diff = oldest_battery - current_battery_float
            rate = (battery_diff / time_diff) * 60
            return round(rate, 2)

        return 0.0

    def _check_battery_anomaly(self, ups_data: Dict[str, str]):
        """Detect abnormal battery charge changes while on line power.

        Catches firmware recalibrations, battery aging events, or hardware
        issues that cause sudden charge drops (e.g., 100% -> 60% in seconds)
        while the UPS is on line power and not discharging.

        Uses sustained-reading confirmation: an anomalous drop must persist
        across 3 consecutive polls before firing.  This filters out transient
        firmware jitter that some UPS units (notably APC, CyberPower, and
        Ubiquiti UniFi UPS) exhibit after an OB -> OL transition, where the first
        few readings may report a wildly incorrect charge that self-corrects
        within a couple of seconds.
        """
        ups_status = ups_data.get('ups.status', '')
        battery_charge_str = ups_data.get('battery.charge', '')

        if not is_numeric(battery_charge_str):
            return

        current_charge = float(battery_charge_str)
        current_time = time.time()

        # Only track anomalies while on line power (OL/CHRG)
        if "OB" in ups_status:
            # On battery -- reset tracking, drops are expected
            self.state.last_battery_charge = current_charge
            self.state.last_battery_charge_time = current_time
            self.state.pending_anomaly_charge = -1.0
            self.state.pending_anomaly_count = 0
            return

        prev_charge = self.state.last_battery_charge
        prev_time = self.state.last_battery_charge_time

        # Update tracking
        self.state.last_battery_charge = current_charge
        self.state.last_battery_charge_time = current_time

        # Skip if not yet initialized
        if prev_charge < 0:
            return

        # Check for significant drop while online
        drop = prev_charge - current_charge
        elapsed = current_time - prev_time if prev_time > 0 else 0

        # Threshold: >20% drop within 120 seconds while on line power
        if drop > 20 and elapsed < 120:
            # First detection -- record as pending, wait for confirmation
            self.state.pending_anomaly_charge = current_charge
            self.state.pending_anomaly_prev_charge = prev_charge
            self.state.pending_anomaly_time = current_time
            self.state.pending_anomaly_count = 1
            return

        # Check if a pending anomaly is being confirmed across polls
        if self.state.pending_anomaly_charge >= 0:
            # Charge recovered -- transient jitter, discard the anomaly
            if current_charge > self.state.pending_anomaly_charge + 10:
                self.state.pending_anomaly_charge = -1.0
                self.state.pending_anomaly_count = 0
                return

            # Still low -- increment confirmation counter
            self.state.pending_anomaly_count += 1

            # Need 3 consecutive polls to confirm (filters firmware jitter)
            if self.state.pending_anomaly_count < 3:
                return

            # Re-validate the drop magnitude before notifying. Without this
            # check the confirmation can fire after the charge has crept
            # back up to within a few % of the original (drop < 20),
            # producing a false-alarm "battery dropped X%" message that
            # contradicts the current reading.
            anomaly_prev = self.state.pending_anomaly_prev_charge
            anomaly_drop = anomaly_prev - current_charge
            if anomaly_drop <= 20:
                self.state.pending_anomaly_charge = -1.0
                self.state.pending_anomaly_count = 0
                return

            # Confirmed anomaly (sustained across 3 polls)
            anomaly_elapsed = current_time - self.state.pending_anomaly_time
            self.state.pending_anomaly_charge = -1.0
            self.state.pending_anomaly_count = 0

            self._log_message(
                f"⚠️ WARNING: Battery charge dropped from {anomaly_prev:.0f}% to "
                f"{current_charge:.0f}% ({anomaly_drop:.0f}% drop) while on line power. "
                f"Possible firmware recalibration, battery aging, or hardware issue."
            )
            self._send_notification(
                f"⚠️ **Battery Anomaly Detected**\n"
                f"Charge dropped from {anomaly_prev:.0f}% to {current_charge:.0f}% "
                f"({anomaly_drop:.0f}% drop in {anomaly_elapsed:.0f}s) while on line power.\n"
                f"Possible causes: firmware recalibration, battery aging, or hardware issue.",
                self.config.NOTIFY_WARNING,
                category="health",
            )
