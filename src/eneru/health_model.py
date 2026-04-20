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


def assess_health(
    snapshot,
    triggers=None,
    check_interval: int = 1,
    *,
    now: Optional[float] = None,
) -> UPSHealth:
    """Classify a UPS snapshot into a :class:`UPSHealth` tier.

    Pure function -- no side effects, no thread state.

    Order of evaluation (first match wins):

    1. **UNKNOWN** -- the snapshot is unusable:
       - no successful poll has happened yet (``last_update_time == 0``); or
       - the last poll is older than ``5 * check_interval`` seconds; or
       - the per-UPS monitor reports ``connection_state == "FAILED"``.
    2. **CRITICAL** -- the UPS is signalling shutdown intent:
       - the monitor's advisory ``trigger_active`` flag is set; or
       - the UPS is in ``FSD`` (Forced Shutdown Drain).
    3. **DEGRADED** -- the UPS is healthy but in a warning state:
       - on battery (``OB`` in status) without an active trigger; or
       - connection in the loss-grace window.
    4. **HEALTHY** -- everything looks fine.

    Args:
        snapshot: ``HealthSnapshot`` from ``MonitorState.snapshot()``.
        triggers: ``TriggersConfig`` for the member UPS. Reserved for
            future extensions; the monitor itself owns threshold
            evaluation, so the evaluator trusts ``snapshot.trigger_active``.
        check_interval: The member's ``ups.check_interval`` in seconds.
            Used to compute the stale-snapshot threshold.
        now: Optional ``time.time()`` override -- only used by tests.
    """
    del triggers  # Reserved for future use.

    current_time = now if now is not None else time.time()
    interval = max(1, int(check_interval) if check_interval else 1)

    # 1. UNKNOWN
    if snapshot.last_update_time == 0:
        return UPSHealth.UNKNOWN
    if (current_time - snapshot.last_update_time) > (STALE_INTERVAL_MULTIPLIER * interval):
        return UPSHealth.UNKNOWN
    if snapshot.connection_state == "FAILED":
        return UPSHealth.UNKNOWN

    # 2. CRITICAL
    if snapshot.trigger_active:
        return UPSHealth.CRITICAL
    if "FSD" in snapshot.status:
        return UPSHealth.CRITICAL

    # 3. DEGRADED
    if "OB" in snapshot.status:
        return UPSHealth.DEGRADED
    if snapshot.connection_state == "GRACE_PERIOD":
        return UPSHealth.DEGRADED

    # 4. HEALTHY
    return UPSHealth.HEALTHY
