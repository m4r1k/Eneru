"""Health-model primitives for redundancy-group evaluation.

Lives in its own module (``health_model.py`` -- not ``health.py``) to
avoid colliding with the existing :mod:`eneru.health` mixin package.

A :class:`UPSHealth` value is the *contribution* a single UPS makes to
its redundancy group. The group evaluator sums those contributions
according to the group's ``degraded_counts_as`` / ``unknown_counts_as``
policies and decides whether quorum is lost.

This module is deliberately pure: no I/O, no threading, no file system.
The same function backs unit tests, the live evaluator, and the TUI.
"""

import time
from enum import Enum
from typing import Optional

from eneru.config import TriggersConfig
from eneru.state import HealthSnapshot


class UPSHealth(str, Enum):
    """Per-UPS contribution to a redundancy group's quorum count.

    Inheriting from ``str`` keeps values JSON-serialisable and
    log-friendly without losing enum semantics.
    """
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    CRITICAL = "critical"
    UNKNOWN = "unknown"


# Snapshots older than this many ``check_interval``s are treated as UNKNOWN.
# 5 polls is the documented "stale snapshot" threshold from the Phase 2 spec.
STALE_INTERVAL_MULTIPLIER = 5

# ISS-017: SINGLE source of truth for the connection-retry cadence (seconds
# between failed polls). monitor.py imports this as CONNECTION_RETRY_WAIT_SECONDS
# for its `_stop_event.wait(...)`, and the pre-grace stale window below is sized
# off it. ``max_stale_data_tolerance`` is a poll COUNT, so the window must be
# tolerance * (seconds-per-retry) + interval -- not tolerance * a dimensionless
# constant compared against seconds (which silently assumed 1s/retry).
RETRY_WAIT_SECONDS = 5


def assess_health(
    snapshot: HealthSnapshot,
    triggers: Optional[TriggersConfig] = None,
    check_interval: int = 1,
    *,
    max_stale_data_tolerance: int = 3,
    connection_grace_enabled: bool = False,
    connection_grace_duration: int = 60,
    now: Optional[float] = None,
) -> UPSHealth:
    """Classify a UPS snapshot into a :class:`UPSHealth` tier.

    Pure function -- no side effects, no thread state.

    Order of evaluation (first match wins):

    1. **UNKNOWN** -- the snapshot is unusable:
       - no successful poll has happened yet (``last_update_time == 0``); or
       - the per-UPS monitor reports ``connection_state == "FAILED"``; or
       - the last poll is older than ``5 * check_interval`` seconds and
         the monitor is not actively handling a transient stale/lost poll.
    2. **CRITICAL** -- the UPS is signalling shutdown intent:
       - the monitor's advisory ``trigger_active`` flag is set; or
       - the UPS is in ``FSD`` (Forced Shutdown Drain).
    3. **DEGRADED** -- the UPS is healthy but in a warning state:
       - on battery (``OB`` in status) without an active trigger; or
       - connection in the loss-grace window; or
       - stale/lost NUT data is being retried after at least one good poll.
    4. **HEALTHY** -- everything looks fine.

    Args:
        snapshot: ``HealthSnapshot`` from ``MonitorState.snapshot()``.
        triggers: ``TriggersConfig`` for the evaluator's policy layer.
            Per-UPS monitors still publish ``snapshot.trigger_active``, but
            redundancy groups may override thresholds without mutating the
            member monitor's own config.
        check_interval: The member's ``ups.check_interval`` in seconds.
            Used to compute the stale-snapshot threshold.
        max_stale_data_tolerance: Consecutive stale polls tolerated before
            monitor.py enters connection grace. Used only to bound the
            pre-grace stale retry window so a dead monitor cannot stay
            degraded forever.
        connection_grace_enabled: Whether the member monitor is configured
            to tolerate connection loss before marking it failed.
        connection_grace_duration: Configured connection-loss grace duration
            in seconds.
        now: Optional ``time.time()`` override -- only used by tests.
    """
    current_time = now if now is not None else time.time()
    interval = max(1, int(check_interval) if check_interval else 1)

    # 1. UNKNOWN
    if snapshot.last_update_time == 0:
        return UPSHealth.UNKNOWN
    if snapshot.connection_state == "FAILED":
        return UPSHealth.UNKNOWN

    # Runtime NUT visibility loss should line up with monitor.py's
    # connection-loss grace. A member that had a good poll before the
    # flap contributes DEGRADED while stale/lost data is still being
    # retried; only FAILED after grace expiry becomes UNKNOWN.
    age = current_time - snapshot.last_update_time
    try:
        tolerance = max(1, int(max_stale_data_tolerance))
    except (TypeError, ValueError):
        tolerance = 3
    try:
        grace_duration = max(0, int(connection_grace_duration))
    except (TypeError, ValueError):
        grace_duration = 60
    grace_window = grace_duration if connection_grace_enabled else 0
    stale_threshold = STALE_INTERVAL_MULTIPLIER * interval
    # tolerance polls, each followed by a RETRY_WAIT_SECONDS sleep, plus one
    # interval of slack — expressed in seconds to match ``age``.
    pre_grace_stale_window = max(
        stale_threshold,
        (tolerance * RETRY_WAIT_SECONDS) + interval,
    )
    stale_count = getattr(snapshot, "stale_data_count", 0)
    transient_stale_retry = (
        stale_count > 0
        and age <= pre_grace_stale_window
    )
    if snapshot.connection_state == "GRACE_PERIOD":
        lost_at = getattr(snapshot, "connection_lost_time", 0.0)
        if lost_at:
            grace_age = current_time - lost_at
        else:
            # Back-compat path: a snapshot in GRACE_PERIOD with no
            # ``connection_lost_time`` predates that field. Approximate the
            # in-grace duration from the snapshot's age past the stale
            # threshold. Allowed to go negative on purpose -- a fresh
            # GRACE_PERIOD snapshot then trivially compares below
            # ``grace_window`` (even when grace_window == 0) and stays
            # DEGRADED, deferring to the monitor's own grace timer.
            grace_age = age - stale_threshold
        if grace_age >= grace_window:
            return UPSHealth.UNKNOWN
        return UPSHealth.DEGRADED

    # A slow in-flight upsc call leaves the last published snapshot as OK.
    # Give it the same bounded visibility grace as an explicit failure; a
    # monitor that remains stale past that window is treated as UNKNOWN.
    in_flight_grace = (
        snapshot.connection_state == "OK"
        and age > stale_threshold
        and grace_window
        and age <= stale_threshold + grace_window
    )
    transient_visibility_loss = (
        in_flight_grace
        or transient_stale_retry
    )
    if age > stale_threshold and not transient_visibility_loss:
        return UPSHealth.UNKNOWN

    # 2. CRITICAL
    if snapshot.trigger_active:
        return UPSHealth.CRITICAL
    if "FSD" in snapshot.status:
        return UPSHealth.CRITICAL
    # Group-local thresholds mirror monitor.py's T1-T4 checks, which run
    # inside the on-battery handler. A recovering OL UPS may still have low
    # charge; that should not by itself exhaust redundancy quorum.
    if triggers is not None and "OB" in snapshot.status:
        stabilization_delay = getattr(
            triggers, "on_battery_stabilization_delay", 0
        )
        try:
            stabilization_delay = max(0, int(stabilization_delay))
        except (TypeError, ValueError):
            stabilization_delay = 0
        if snapshot.time_on_battery < stabilization_delay:
            return UPSHealth.DEGRADED
        try:
            battery = int(float(snapshot.battery_charge))
        except (TypeError, ValueError):
            battery = None
        try:
            runtime = int(float(snapshot.runtime))
        except (TypeError, ValueError):
            runtime = None
        if (
            battery is not None
            and battery < getattr(triggers, "low_battery_threshold", 20)
        ):
            return UPSHealth.CRITICAL
        if (
            runtime is not None
            and runtime < getattr(triggers, "critical_runtime_threshold", 600)
        ):
            return UPSHealth.CRITICAL
        depletion = getattr(triggers, "depletion", None)
        if depletion is not None:
            rate = getattr(snapshot, "depletion_rate", 0.0) or 0.0
            if (
                rate > getattr(depletion, "critical_rate", 15.0)
                and snapshot.time_on_battery >= getattr(depletion, "grace_period", 90)
            ):
                return UPSHealth.CRITICAL
        extended_time = getattr(triggers, "extended_time", None)
        if (
            extended_time is not None
            and getattr(extended_time, "enabled", True)
            and snapshot.time_on_battery > getattr(extended_time, "threshold", 900)
        ):
            return UPSHealth.CRITICAL
    # 3. DEGRADED
    if "OB" in snapshot.status:
        return UPSHealth.DEGRADED
    if transient_visibility_loss:
        return UPSHealth.DEGRADED

    # 4. HEALTHY
    return UPSHealth.HEALTHY
