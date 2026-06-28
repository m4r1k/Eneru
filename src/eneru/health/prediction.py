"""Battery-health scoring + replacement prediction (v6.1, pure functions).

Kept pure and threadless so the heuristic is unit-testable in isolation; the
``BatteryMonitorMixin`` gathers the live inputs (config, state, stats) and
calls in here.

The cardinal rule (see src/eneru/AGENTS.md): **unknown is not healthy.** Every
term returns a 0-100 score *or* ``None`` when its input isn't available yet
(no install date, runtime not learned, never self-tested...). A missing term
never silently counts as full marks — the composite is a weighted average over
*available* terms only, and if too little of the weight is available the score
itself is ``None`` ("unknown"), never a confident high number from thin
telemetry.
"""

from datetime import datetime
from typing import Dict, List, Optional, Tuple

__all__ = [
    "TERM_WEIGHTS",
    "MIN_CONFIDENCE",
    "runtime_score",
    "capacity_score",
    "self_test_score",
    "anomaly_score",
    "age_score",
    "compute_terms",
    "composite_score",
    "least_squares_slope",
    "predict_replacement",
]

# Default per-term weights. Tunable later; the composite renormalizes over
# whichever terms are actually available.
TERM_WEIGHTS: Dict[str, float] = {
    "capacity": 0.30,
    "runtime": 0.25,
    "self_test": 0.20,
    "anomaly": 0.15,
    "age": 0.10,
}

# Report a composite score only when at least this fraction of total weight is
# available; below it the telemetry is too thin and the score is "unknown".
MIN_CONFIDENCE = 0.30


def _clamp(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, value))


def runtime_score(current_runtime_s: Optional[float],
                  nominal_runtime_s: Optional[float]) -> Optional[float]:
    """Current runtime-under-load vs the expected (nominal) full runtime.

    Unavailable until the nominal runtime is known (configured or learned).
    """
    if (nominal_runtime_s is None or nominal_runtime_s <= 0
            or current_runtime_s is None or current_runtime_s < 0):
        return None
    return _clamp(current_runtime_s / nominal_runtime_s * 100.0)


def capacity_score(runtime_history: List[Tuple[float, float]],
                   nominal_runtime_s: Optional[float]) -> Optional[float]:
    """Capacity degradation inferred from the runtime TREND, not instantaneous
    charge (which is state-of-charge, not capacity).

    ``runtime_history`` is ``[(ts, runtime_s)]``. A flat or rising trend scores
    100; a decline scores lower in proportion to how fast runtime is dropping
    relative to the nominal runtime. Unavailable with fewer than two points or
    no nominal to normalise against.
    """
    if (nominal_runtime_s is None or nominal_runtime_s <= 0
            or len(runtime_history) < 2):
        return None
    slope = least_squares_slope(runtime_history)  # runtime_s per second
    if slope is None or slope >= 0:
        return 100.0
    # Project the decline over 30 days; express the lost fraction of nominal.
    lost_over_30d = (-slope) * (30 * 86400)
    lost_fraction = lost_over_30d / nominal_runtime_s
    return _clamp(100.0 - lost_fraction * 100.0)


def self_test_score(result_enum: Optional[str]) -> Optional[float]:
    """Latest self-test result. Only passed/failed inform the score; running,
    unknown, unsupported, or never-tested are unavailable."""
    return {"passed": 100.0, "failed": 0.0}.get(result_enum or "")


def anomaly_score(anomaly_count: int, *, penalty: float = 25.0) -> float:
    """Confirmed-anomaly count. Always available (zero anomalies => 100)."""
    return _clamp(100.0 - max(0, anomaly_count) * penalty)


def age_score(battery_install_date: Optional[str],
              expected_life_years: float,
              now: float) -> Optional[float]:
    """Battery age vs expected life. Unavailable if the install date is unset
    or unparseable."""
    if not battery_install_date or expected_life_years is None \
            or expected_life_years <= 0:
        return None
    try:
        installed = datetime.strptime(
            str(battery_install_date).strip(), "%Y-%m-%d").timestamp()
    except (ValueError, TypeError):
        return None
    age_years = (now - installed) / (365.25 * 86400)
    if age_years < 0:
        age_years = 0.0
    return _clamp(100.0 * (1.0 - age_years / expected_life_years))


def compute_terms(*, current_runtime_s: Optional[float],
                  nominal_runtime_s: Optional[float],
                  runtime_history: List[Tuple[float, float]],
                  self_test_result: Optional[str],
                  anomaly_count: int,
                  battery_install_date: Optional[str],
                  expected_life_years: float,
                  now: float) -> Dict[str, Optional[float]]:
    """Compute all five term sub-scores (each 0-100 or None=unavailable)."""
    return {
        "capacity": capacity_score(runtime_history, nominal_runtime_s),
        "runtime": runtime_score(current_runtime_s, nominal_runtime_s),
        "self_test": self_test_score(self_test_result),
        "anomaly": anomaly_score(anomaly_count),
        "age": age_score(battery_install_date, expected_life_years, now),
    }


def composite_score(terms: Dict[str, Optional[float]],
                    weights: Optional[Dict[str, float]] = None
                    ) -> Tuple[Optional[float], float, List[str]]:
    """Weighted composite over available terms.

    Returns ``(score, confidence, available_terms)``. ``score`` is ``None``
    (unknown) when the available weight is below ``MIN_CONFIDENCE`` -- thin
    telemetry must never produce a confident high score. ``confidence`` is the
    fraction of total weight that was available.
    """
    weights = weights or TERM_WEIGHTS
    total_weight = sum(weights.get(name, 0.0) for name in terms)
    available = {n: v for n, v in terms.items() if v is not None}
    avail_weight = sum(weights.get(n, 0.0) for n in available)
    confidence = (avail_weight / total_weight) if total_weight > 0 else 0.0
    if not available or confidence < MIN_CONFIDENCE:
        return None, confidence, sorted(available)
    score = sum(v * weights.get(n, 0.0) for n, v in available.items()) / avail_weight
    return _clamp(score), confidence, sorted(available)


def least_squares_slope(points: List[Tuple[float, float]]) -> Optional[float]:
    """Ordinary least-squares slope dy/dx over ``[(x, y)]``. ``None`` if fewer
    than two points or x has zero variance."""
    n = len(points)
    if n < 2:
        return None
    sx = sum(x for x, _ in points)
    sy = sum(y for _, y in points)
    sxx = sum(x * x for x, _ in points)
    sxy = sum(x * y for x, y in points)
    denom = n * sxx - sx * sx
    if denom == 0:
        return None
    return (n * sxy - sx * sy) / denom


def predict_replacement(history: List[Tuple[float, float]], *,
                        threshold_score: float,
                        horizon_days: int,
                        min_history_days: int,
                        now: float) -> Dict:
    """Project when the health score crosses ``threshold_score``.

    ``history`` is ``[(ts, score)]`` (scores must be non-None). Returns a dict:
    ``{due, days_remaining, eta_ts, reason}``. ``due`` is True when the battery
    is already below threshold, or the declining trend projects a crossing
    within ``horizon_days``. A flat/improving trend, or too little history, is
    never due.
    """
    result = {"due": False, "days_remaining": None, "eta_ts": None,
              "reason": ""}
    if len(history) < 2:
        result["reason"] = "insufficient history"
        return result
    span_days = (history[-1][0] - history[0][0]) / 86400.0
    if span_days < min_history_days:
        result["reason"] = "insufficient history span"
        return result

    last_ts, last_score = history[-1]
    # Already at/below threshold -> due now.
    if last_score <= threshold_score:
        result.update(due=True, days_remaining=0.0, eta_ts=last_ts,
                      reason="already below threshold")
        return result

    slope = least_squares_slope(history)  # score per second
    if slope is None or slope >= 0:
        result["reason"] = "flat or improving"
        return result

    seconds_to_cross = (threshold_score - last_score) / slope  # both negative-ish
    eta_ts = last_ts + seconds_to_cross
    days_remaining = (eta_ts - now) / 86400.0
    if days_remaining <= 0:
        # Crossing already in the past relative to now -> due.
        result.update(due=True, days_remaining=0.0, eta_ts=eta_ts,
                      reason="projected crossing reached")
        return result
    result.update(days_remaining=days_remaining, eta_ts=eta_ts,
                  due=days_remaining <= horizon_days,
                  reason="within horizon" if days_remaining <= horizon_days
                  else "beyond horizon")
    return result
