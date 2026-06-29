"""Unit tests for the pure battery-health scoring + replacement prediction
(src/eneru/health/prediction.py)."""

import pytest

from eneru.health import prediction as P

DAY = 86400.0


# --------------------------------------------------------------------------
# individual term scores
# --------------------------------------------------------------------------

class TestTermScores:
    @pytest.mark.unit
    def test_runtime_score_ratio_and_clamp(self):
        assert P.runtime_score(900, 1800) == pytest.approx(50.0)
        assert P.runtime_score(3600, 1800) == 100.0   # clamped
        assert P.runtime_score(0, 1800) == 0.0

    @pytest.mark.unit
    def test_runtime_score_unavailable(self):
        assert P.runtime_score(900, None) is None
        assert P.runtime_score(None, 1800) is None
        assert P.runtime_score(900, 0) is None

    @pytest.mark.unit
    def test_capacity_score_flat_is_full(self):
        hist = [(0.0, 1800.0), (DAY, 1800.0), (2 * DAY, 1800.0)]
        assert P.capacity_score(hist, 1800) == 100.0

    @pytest.mark.unit
    def test_capacity_score_declining_is_lower(self):
        # runtime dropping 1800 -> 1700 -> 1600 over 2 days
        hist = [(0.0, 1800.0), (DAY, 1700.0), (2 * DAY, 1600.0)]
        s = P.capacity_score(hist, 1800)
        assert s is not None and 0.0 <= s < 100.0

    @pytest.mark.unit
    def test_capacity_score_unavailable(self):
        assert P.capacity_score([(0.0, 1800.0)], 1800) is None  # <2 points
        assert P.capacity_score([(0.0, 1800.0), (DAY, 1700.0)], None) is None

    @pytest.mark.unit
    def test_capacity_score_zero_x_variance_is_full(self):
        # two points at the same ts -> slope undefined -> treated as flat (100)
        assert P.capacity_score([(5.0, 1800.0), (5.0, 1700.0)], 1800) == 100.0

    @pytest.mark.unit
    def test_self_test_score(self):
        assert P.self_test_score("passed") == 100.0
        assert P.self_test_score("failed") == 0.0
        for x in ("running", "unknown", "unsupported", None, ""):
            assert P.self_test_score(x) is None

    @pytest.mark.unit
    def test_anomaly_score_always_available(self):
        assert P.anomaly_score(0) == 100.0
        assert P.anomaly_score(2) == 50.0
        assert P.anomaly_score(10) == 0.0  # clamped

    @pytest.mark.unit
    def test_age_score(self):
        now = P.datetime(2026, 6, 1).timestamp()
        # installed 1 year ago, 5y expected -> ~80
        s = P.age_score("2025-06-01", 5.0, now)
        assert s == pytest.approx(80.0, abs=1.0)

    @pytest.mark.unit
    def test_age_score_unavailable_and_bad_date(self):
        assert P.age_score(None, 5.0, 1000.0) is None
        assert P.age_score("not-a-date", 5.0, 1000.0) is None
        assert P.age_score("2025-06-01", 0, 1000.0) is None

    @pytest.mark.unit
    def test_age_score_future_install_clamped(self):
        now = P.datetime(2026, 6, 1).timestamp()
        assert P.age_score("2030-01-01", 5.0, now) == 100.0  # negative age -> full


# --------------------------------------------------------------------------
# composite
# --------------------------------------------------------------------------

class TestComposite:
    @pytest.mark.unit
    def test_unknown_when_only_anomaly_available(self):
        # Only the always-available anomaly term (weight 0.15) -> below
        # MIN_CONFIDENCE -> score is None (unknown), NOT a confident 100.
        terms = {"capacity": None, "runtime": None, "self_test": None,
                 "anomaly": 100.0, "age": None}
        score, conf, avail = P.composite_score(terms)
        assert score is None
        assert conf == pytest.approx(0.15)
        assert avail == ["anomaly"]

    @pytest.mark.unit
    def test_weighted_average_over_available_terms(self):
        terms = {"capacity": None, "runtime": 80.0, "self_test": 100.0,
                 "anomaly": 100.0, "age": None}
        score, conf, avail = P.composite_score(terms)
        # weights: runtime .25, self_test .20, anomaly .15 -> total avail .60
        expected = (80 * .25 + 100 * .20 + 100 * .15) / .60
        assert score == pytest.approx(expected)
        assert conf == pytest.approx(0.60)
        assert avail == ["anomaly", "runtime", "self_test"]

    @pytest.mark.unit
    def test_all_none_is_unknown(self):
        terms = {k: None for k in P.TERM_WEIGHTS}
        score, conf, avail = P.composite_score(terms)
        assert score is None and conf == 0.0 and avail == []


# --------------------------------------------------------------------------
# least squares
# --------------------------------------------------------------------------

class TestLeastSquares:
    @pytest.mark.unit
    def test_slope_known(self):
        # y = 2x + 1
        assert P.least_squares_slope([(0, 1), (1, 3), (2, 5)]) == pytest.approx(2.0)

    @pytest.mark.unit
    def test_slope_insufficient_or_flat_x(self):
        assert P.least_squares_slope([(1, 1)]) is None
        assert P.least_squares_slope([(5, 1), (5, 9)]) is None  # zero x variance


# --------------------------------------------------------------------------
# replacement prediction
# --------------------------------------------------------------------------

class TestPredictReplacement:
    @pytest.mark.unit
    def test_insufficient_history(self):
        r = P.predict_replacement([(0.0, 90.0)], threshold_score=50,
                                  horizon_days=90, min_history_days=14, now=DAY)
        assert r["due"] is False and "insufficient" in r["reason"]

    @pytest.mark.unit
    def test_insufficient_span(self):
        hist = [(0.0, 90.0), (DAY, 89.0)]  # only 1 day span
        r = P.predict_replacement(hist, threshold_score=50, horizon_days=90,
                                  min_history_days=14, now=DAY)
        assert r["due"] is False and "span" in r["reason"]

    @pytest.mark.unit
    def test_already_below_threshold_is_due(self):
        hist = [(0.0, 60.0), (20 * DAY, 45.0)]
        r = P.predict_replacement(hist, threshold_score=50, horizon_days=90,
                                  min_history_days=14, now=20 * DAY)
        assert r["due"] is True and r["days_remaining"] == 0.0

    @pytest.mark.unit
    def test_already_below_with_thin_history_still_due(self):
        # A failed battery must fire even with a single sub-threshold point: the
        # already-below check now runs BEFORE the history-length/span guards.
        r = P.predict_replacement([(0.0, 30.0)], threshold_score=50,
                                  horizon_days=90, min_history_days=14, now=DAY)
        assert r["due"] is True and "already below" in r["reason"]

    @pytest.mark.unit
    def test_flat_or_improving_not_due(self):
        hist = [(0.0, 80.0), (30 * DAY, 82.0)]
        r = P.predict_replacement(hist, threshold_score=50, horizon_days=90,
                                  min_history_days=14, now=30 * DAY)
        assert r["due"] is False and r["reason"] == "flat or improving"

    @pytest.mark.unit
    def test_declining_within_horizon_is_due(self):
        # 90 -> 70 over 30 days = -20/30d; from 70 to 50 threshold = 30 more days
        hist = [(0.0, 90.0), (30 * DAY, 70.0)]
        r = P.predict_replacement(hist, threshold_score=50, horizon_days=90,
                                  min_history_days=14, now=30 * DAY)
        assert r["due"] is True
        assert r["days_remaining"] == pytest.approx(30.0, abs=1.0)

    @pytest.mark.unit
    def test_projected_crossing_already_past_is_due(self):
        # declining history, but `now` is far past the last sample so the
        # projected crossing is already behind us -> due.
        hist = [(0.0, 90.0), (30 * DAY, 70.0)]
        r = P.predict_replacement(hist, threshold_score=50, horizon_days=90,
                                  min_history_days=14, now=200 * DAY)
        assert r["due"] is True and r["days_remaining"] == 0.0

    @pytest.mark.unit
    def test_declining_beyond_horizon_not_due(self):
        # very slow decline -> crossing far beyond a 30-day horizon
        hist = [(0.0, 90.0), (60 * DAY, 88.0)]
        r = P.predict_replacement(hist, threshold_score=50, horizon_days=30,
                                  min_history_days=14, now=60 * DAY)
        assert r["due"] is False
        assert r["days_remaining"] > 30
