"""Voltage / AVR / bypass / overload health monitoring.

Translates raw NUT status flags and ``input.voltage`` readings into
power-event log lines (``BROWNOUT_DETECTED``, ``OVER_VOLTAGE_DETECTED``,
``AVR_BOOST_ACTIVE`` etc.) and tracks per-state transitions on
``self.state``.
"""

from eneru.utils import is_numeric


class VoltageMonitorMixin:
    """Mixin: voltage thresholds, AVR, bypass, and overload monitoring."""

    def _initialize_voltage_thresholds(self):
        """Initialize voltage thresholds dynamically from UPS data."""
        nominal = self._get_ups_var("input.voltage.nominal")
        low_transfer = self._get_ups_var("input.transfer.low")
        high_transfer = self._get_ups_var("input.transfer.high")

        if is_numeric(nominal):
            self.state.nominal_voltage = float(nominal)
        else:
            self.state.nominal_voltage = 230.0

        if is_numeric(low_transfer):
            self.state.voltage_warning_low = float(low_transfer) + 5
        else:
            self.state.voltage_warning_low = self.state.nominal_voltage * 0.9

        if is_numeric(high_transfer):
            self.state.voltage_warning_high = float(high_transfer) - 5
        else:
            self.state.voltage_warning_high = self.state.nominal_voltage * 1.1

        self._log_message(
            f"📊 Voltage Monitoring Active. Nominal: {self.state.nominal_voltage}V. "
            f"Low Warning: {self.state.voltage_warning_low}V. "
            f"High Warning: {self.state.voltage_warning_high}V."
        )

    def _check_voltage_issues(self, ups_status: str, input_voltage: str):
        """Check for voltage quality issues."""
        if "OL" not in ups_status:
            if "OB" in ups_status or "FSD" in ups_status:
                self.state.voltage_state = "NORMAL"
            return

        if not is_numeric(input_voltage):
            return

        voltage = float(input_voltage)

        if voltage < self.state.voltage_warning_low:
            if self.state.voltage_state != "LOW":
                self._log_power_event(
                    "BROWNOUT_DETECTED",
                    f"Voltage is low: {voltage}V (Threshold: {self.state.voltage_warning_low}V)"
                )
                self.state.voltage_state = "LOW"
        elif voltage > self.state.voltage_warning_high:
            if self.state.voltage_state != "HIGH":
                self._log_power_event(
                    "OVER_VOLTAGE_DETECTED",
                    f"Voltage is high: {voltage}V (Threshold: {self.state.voltage_warning_high}V)"
                )
                self.state.voltage_state = "HIGH"
        elif self.state.voltage_state != "NORMAL":
            self._log_power_event(
                "VOLTAGE_NORMALIZED",
                f"Voltage returned to normal: {voltage}V. Previous state: {self.state.voltage_state}"
            )
            self.state.voltage_state = "NORMAL"

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
