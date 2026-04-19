"""Health-monitoring mixins for UPSGroupMonitor.

Each mixin owns one diagnostic concern:

* :class:`~eneru.health.voltage.VoltageMonitorMixin` - thresholds, AVR,
  bypass, overload
* :class:`~eneru.health.battery.BatteryMonitorMixin` - depletion-rate
  history and anomaly detection

Mixins assume the host class provides ``self.config``, ``self.state``,
``self._log_message``, ``self._send_notification``, ``self._log_power_event``,
``self._get_ups_var``, and (for battery) ``self._battery_history_path``.
"""
