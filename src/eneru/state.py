"""Monitor state tracking for Eneru."""

import threading
import time
from collections import deque, namedtuple
from dataclasses import dataclass, field
from typing import Optional


# Frozen snapshot of the fields a redundancy-group evaluator needs to read.
# Returned by ``MonitorState.snapshot()`` under the state lock so the values
# are mutually consistent for a single read.
HealthSnapshot = namedtuple(
    "HealthSnapshot",
    [
        "status",              # latest ``ups.status`` (e.g. "OL", "OB DISCHRG")
        "battery_charge",      # latest ``battery.charge`` (string from upsc)
        "runtime",             # latest ``battery.runtime`` (string, seconds)
        "load",                # latest ``ups.load`` (string, percent)
        "depletion_rate",      # latest depletion rate (float, %/min)
        "time_on_battery",     # seconds since on_battery_start_time, 0 when OL
        "last_update_time",    # ``time.time()`` of the last successful poll
        "connection_state",    # "OK" / "GRACE_PERIOD" / "FAILED"
        "trigger_active",      # advisory shutdown trigger fired (redundancy mode)
        "trigger_reason",      # human-readable reason for the advisory trigger
    ],
)


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
    connection_lost_time: float = 0.0
    connection_flap_count: int = 0
    connection_first_flap_time: float = 0.0
    stale_data_count: int = 0
    voltage_warning_low: float = 0.0
    voltage_warning_high: float = 0.0
    nominal_voltage: float = 230.0
    # Resolved voltage_sensitivity preset as a deviation fraction
    # (0.05 / 0.10 / 0.15). Set once by _initialize_voltage_thresholds
    # and reused by the autodetect re-snap path so a per-UPS preset
    # carries through if NUT's nominal turns out to be wrong.
    voltage_deviation_pct: float = 0.10
    # UPS firmware's switch-to-battery thresholds, populated from
    # ``input.transfer.{low,high}`` at startup. Informational only --
    # used by voltage notification text to give the operator context
    # ("warning fired but UPS won't switch until X V"). None when NUT
    # doesn't report transfer points.
    ups_transfer_low: Optional[float] = None
    ups_transfer_high: Optional[float] = None
    battery_history: deque = field(default_factory=lambda: deque(maxlen=1000))
    # Battery anomaly detection (recalibration, sudden drops while online)
    last_battery_charge: float = -1.0  # -1 = not yet initialized
    last_battery_charge_time: float = 0.0
    # Sustained-reading confirmation: anomaly must persist across 3 consecutive
    # polls to filter out transient firmware jitter after OB→OL transitions
    # (known behavior on APC, CyberPower, and Ubiquiti UniFi UPS units).
    pending_anomaly_charge: float = -1.0  # -1 = no pending anomaly
    pending_anomaly_prev_charge: float = 0.0
    pending_anomaly_time: float = 0.0
    pending_anomaly_count: int = 0  # consecutive polls confirming the anomaly

    # Voltage notification hysteresis (#27 / B2). The state log line is
    # always written immediately on a NORMAL→HIGH/LOW transition; the
    # *notification* dispatch is deferred until the condition has held
    # for ``notifications.voltage_hysteresis_seconds``. If the condition
    # reverts inside the window, no notification is sent and a
    # VOLTAGE_FLAP_SUPPRESSED event is recorded.
    voltage_pending_state: str = ""    # "" / "LOW" / "HIGH" -- pending notify
    voltage_pending_since: float = 0.0
    voltage_pending_voltage: float = 0.0
    voltage_pending_threshold: float = 0.0
    voltage_pending_notified: bool = False  # already fired during this dwell
    # Severity-aware bypass (rc9): when True, the pending notification
    # skips the voltage_hysteresis_seconds dwell and dispatches on the
    # next _maybe_notify_voltage_pending tick -- triggered when the
    # deviation from nominal exceeds VOLTAGE_SEVERE_DEVIATION_PCT.
    voltage_pending_severe: bool = False

    # Voltage auto-detection (#27 / B1): a small rolling window of
    # observed input.voltage readings used to cross-check NUT's reported
    # input.voltage.nominal at startup. Fixed-size deque so we don't
    # leak memory; evaluated once after ~10 polls and never again.
    voltage_observed: deque = field(default_factory=lambda: deque(maxlen=10))
    voltage_autodetect_done: bool = False

    # ----- Snapshot fields published to redundancy-group evaluators -----
    # The poll cycle writes these atomically under ``_lock`` once per cycle so
    # external readers (RedundancyGroupEvaluator) can call ``snapshot()`` and
    # get a self-consistent view of the most recent UPS observation. Default
    # values match a "no observations yet" state.
    latest_status: str = ""
    latest_battery_charge: str = ""
    latest_runtime: str = ""
    latest_load: str = ""
    latest_depletion_rate: float = 0.0
    latest_time_on_battery: int = 0
    latest_update_time: float = 0.0
    # Set by the monitor's advisory-mode branch when this UPS belongs to a
    # redundancy group: instead of triggering a local shutdown the monitor
    # records the trigger here for the group evaluator to act on.
    trigger_active: bool = False
    trigger_reason: str = ""

    # Lock guarding the latest_* and trigger_* fields. Excluded from
    # ``__repr__`` and ``__eq__`` so existing behaviour (and test assertions
    # that compare or print states) is preserved verbatim.
    _lock: threading.Lock = field(
        default_factory=threading.Lock, repr=False, compare=False
    )

    def snapshot(self) -> HealthSnapshot:
        """Return a self-consistent snapshot of the live monitor signals.

        Always read via this helper from another thread; direct attribute
        access is not safe because the poll cycle updates several fields in
        quick succession.
        """
        with self._lock:
            return HealthSnapshot(
                status=self.latest_status,
                battery_charge=self.latest_battery_charge,
                runtime=self.latest_runtime,
                load=self.latest_load,
                depletion_rate=self.latest_depletion_rate,
                time_on_battery=self.latest_time_on_battery,
                last_update_time=self.latest_update_time,
                connection_state=self.connection_state,
                trigger_active=self.trigger_active,
                trigger_reason=self.trigger_reason,
            )
