"""Monitor state tracking for Eneru."""

from dataclasses import dataclass, field
from collections import deque


@dataclass
class MonitorState:
    """Tracks the current state of the UPS monitor."""
    previous_status: str = ""
    on_battery_start_time: int = 0
    extended_time_logged: bool = False
    voltage_state: str = "NORMAL"
    avr_state: str = "INACTIVE"
    bypass_state: str = "INACTIVE"
    overload_state: str = "INACTIVE"
    connection_state: str = "OK"
    stale_data_count: int = 0
    voltage_warning_low: float = 0.0
    voltage_warning_high: float = 0.0
    nominal_voltage: float = 230.0
    battery_history: deque = field(default_factory=lambda: deque(maxlen=1000))
