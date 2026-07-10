"""Unit tests for the shared periodic scheduler (src/eneru/scheduler.py).

Calendar math uses an injected UTC tz so the assertions are independent of
the machine timezone.
"""

from datetime import datetime, timezone

import pytest

from eneru.scheduler import (
    Schedule,
    parse_hhmm,
    parse_weekday,
)

UTC = timezone.utc


def _epoch(y, mo, d, h=0, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=UTC).timestamp()


# --------------------------------------------------------------------------
# parsers
# --------------------------------------------------------------------------

class TestParsers:
    @pytest.mark.unit
    def test_parse_hhmm_valid(self):
        assert parse_hhmm("08:30") == (8, 30)
        assert parse_hhmm(" 0:00 ") == (0, 0)
        assert parse_hhmm("23:59") == (23, 59)

    @pytest.mark.unit
    @pytest.mark.parametrize("bad", ["8", "8:99", "24:00", "x:y", "8:30:00", ""])
    def test_parse_hhmm_invalid(self, bad):
        with pytest.raises(ValueError):
            parse_hhmm(bad)

    @pytest.mark.unit
    def test_parse_weekday_names_and_ints(self):
        assert parse_weekday("Monday") == 0
        assert parse_weekday("sunday") == 6
        assert parse_weekday(3) == 3

    @pytest.mark.unit
    @pytest.mark.parametrize("bad", ["funday", 7, -1, True])
    def test_parse_weekday_invalid(self, bad):
        with pytest.raises(ValueError):
            parse_weekday(bad)


# --------------------------------------------------------------------------
# Schedule constructors
# --------------------------------------------------------------------------

class TestScheduleConstructors:
    @pytest.mark.unit
    def test_interval_constructor(self):
        s = Schedule.interval(3600)
        assert s.kind == "interval" and s.interval_seconds == 3600
        assert s.fire_on_first is True

    @pytest.mark.unit
    @pytest.mark.parametrize("bad", [0, -5, None])
    def test_interval_rejects_nonpositive(self, bad):
        with pytest.raises(ValueError):
            Schedule.interval(bad)

    @pytest.mark.unit
    def test_interval_preserves_fractional_seconds(self):
        # int() truncation used to make interval(0.5) -> 0 (permanently due) and
        # interval(1.9) -> 1.
        assert Schedule.interval(0.5).interval_seconds == 0.5
        assert Schedule.interval(1.9).interval_seconds == 1.9

    @pytest.mark.unit
    def test_calendar_constructors_default_no_fire_on_first(self):
        assert Schedule.daily("08:00").fire_on_first is False
        assert Schedule.weekly("monday", "08:00").fire_on_first is False
        assert Schedule.monthly(1, "08:00").fire_on_first is False

    @pytest.mark.unit
    def test_weekly_parses_weekday(self):
        assert Schedule.weekly("Friday", "06:00").weekday == 4

    @pytest.mark.unit
    @pytest.mark.parametrize("bad", [0, 32, -1])
    def test_monthly_rejects_bad_day(self, bad):
        with pytest.raises(ValueError):
            Schedule.monthly(bad, "08:00")


# --------------------------------------------------------------------------
# interval due logic
# --------------------------------------------------------------------------

class TestIntervalDue:
    @pytest.mark.unit
    def test_fires_on_first_when_enabled(self):
        assert Schedule.interval(60, fire_on_first=True).due(1000.0, None) is True

    @pytest.mark.unit
    def test_seeds_not_fires_on_first_when_disabled(self):
        assert Schedule.interval(60, fire_on_first=False).due(1000.0, None) is False

    @pytest.mark.unit
    def test_due_after_interval_elapsed(self):
        s = Schedule.interval(60)
        assert s.due(1060.0, 1000.0) is True
        assert s.due(1059.0, 1000.0) is False

    @pytest.mark.unit
    def test_next_run_interval(self):
        s = Schedule.interval(60)
        assert s.next_run(1000.0, 1000.0) == 1060.0      # not yet due
        assert s.next_run(1100.0, 1000.0) == 1100.0      # already due -> now


# --------------------------------------------------------------------------
# calendar due logic (UTC)
# --------------------------------------------------------------------------

class TestDailyDue:
    @pytest.mark.unit
    def test_last_occurrence_today_vs_yesterday(self):
        s = Schedule.daily("08:00")
        # 2026-06-01 09:00 -> last occurrence is today 08:00
        assert s.last_occurrence(_epoch(2026, 6, 1, 9, 0), UTC) == _epoch(2026, 6, 1, 8, 0)
        # 2026-06-01 07:00 -> last occurrence is yesterday 08:00
        assert s.last_occurrence(_epoch(2026, 6, 1, 7, 0), UTC) == _epoch(2026, 5, 31, 8, 0)

    @pytest.mark.unit
    def test_due_when_last_run_before_todays_occurrence(self):
        s = Schedule.daily("08:00")
        now = _epoch(2026, 6, 1, 9, 0)
        assert s.due(now, _epoch(2026, 5, 31, 8, 5), UTC) is True   # ran yesterday
        assert s.due(now, _epoch(2026, 6, 1, 8, 5), UTC) is False   # already ran today

    @pytest.mark.unit
    def test_not_due_on_first_sight_by_default(self):
        s = Schedule.daily("08:00")
        assert s.due(_epoch(2026, 6, 1, 9, 0), None, UTC) is False

    @pytest.mark.unit
    def test_next_run_daily(self):
        s = Schedule.daily("08:00")
        now = _epoch(2026, 6, 1, 9, 0)
        # already ran today -> next is tomorrow 08:00
        assert s.next_run(now, _epoch(2026, 6, 1, 8, 5), UTC) == _epoch(2026, 6, 2, 8, 0)


class TestWeeklyDue:
    @pytest.mark.unit
    def test_last_occurrence_steps_back_to_weekday(self):
        # 2026-06-01 is a Monday. weekly Wednesday 06:00.
        s = Schedule.weekly("wednesday", "06:00")
        now = _epoch(2026, 6, 1, 9, 0)  # Monday
        # most recent Wednesday before Monday = 2026-05-27
        assert s.last_occurrence(now, UTC) == _epoch(2026, 5, 27, 6, 0)

    @pytest.mark.unit
    def test_due_after_a_week(self):
        s = Schedule.weekly("monday", "06:00")
        now = _epoch(2026, 6, 8, 7, 0)  # Monday 07:00
        assert s.due(now, _epoch(2026, 6, 1, 6, 5), UTC) is True   # last week
        assert s.due(now, _epoch(2026, 6, 8, 6, 5), UTC) is False  # already this Monday

    @pytest.mark.unit
    def test_next_run_weekly(self):
        s = Schedule.weekly("monday", "06:00")
        now = _epoch(2026, 6, 8, 7, 0)
        assert s.next_run(now, _epoch(2026, 6, 8, 6, 5), UTC) == _epoch(2026, 6, 15, 6, 0)

    @pytest.mark.unit
    def test_same_weekday_before_time_steps_back_a_week(self):
        # now = Monday 05:00, schedule = Monday 06:00 -> not reached today, so
        # the most recent occurrence is the previous Monday.
        s = Schedule.weekly("monday", "06:00")
        now = _epoch(2026, 6, 8, 5, 0)  # Monday 05:00
        assert s.last_occurrence(now, UTC) == _epoch(2026, 6, 1, 6, 0)


class TestIntervalOccurrenceEdges:
    @pytest.mark.unit
    def test_interval_last_occurrence_raises(self):
        with pytest.raises(ValueError):
            Schedule.interval(60).last_occurrence(1000.0, UTC)

    @pytest.mark.unit
    def test_interval_next_occurrence(self):
        assert Schedule.interval(60).next_occurrence(1000.0, UTC) == 1060.0


class TestMonthlyDue:
    @pytest.mark.unit
    def test_last_occurrence_this_vs_prev_month(self):
        s = Schedule.monthly(15, "00:00")
        assert s.last_occurrence(_epoch(2026, 6, 20), UTC) == _epoch(2026, 6, 15)
        assert s.last_occurrence(_epoch(2026, 6, 10), UTC) == _epoch(2026, 5, 15)

    @pytest.mark.unit
    def test_day_clamped_to_month_length(self):
        # day 31 in February clamps to the 28th (2026 is not a leap year)
        s = Schedule.monthly(31, "00:00")
        assert s.last_occurrence(_epoch(2026, 2, 28, 12), UTC) == _epoch(2026, 2, 28)

    @pytest.mark.unit
    def test_due_after_a_month(self):
        s = Schedule.monthly(1, "00:00")
        now = _epoch(2026, 6, 2)
        assert s.due(now, _epoch(2026, 5, 1, 1), UTC) is True
        assert s.due(now, _epoch(2026, 6, 1, 1), UTC) is False

    @pytest.mark.unit
    def test_next_run_monthly_wraps_year(self):
        s = Schedule.monthly(1, "00:00")
        now = _epoch(2026, 12, 5)
        assert s.next_run(now, _epoch(2026, 12, 1, 1), UTC) == _epoch(2027, 1, 1)



# ------------------------------------------------------------------
# DST correctness (F-070 spring-forward, F-083 fall-back)
# ------------------------------------------------------------------

try:
    from zoneinfo import ZoneInfo
    BERLIN = ZoneInfo("Europe/Berlin")
except Exception:  # pragma: no cover - tzdata missing on exotic platforms
    BERLIN = None

needs_tz = pytest.mark.skipif(BERLIN is None, reason="zoneinfo tz unavailable")


def _berlin(y, mo, d, h, mi, fold=0):
    return datetime(y, mo, d, h, mi, tzinfo=BERLIN, fold=fold).timestamp()


class TestDSTSpringForward:
    """F-070: Europe/Berlin 2026-03-29 02:00→03:00 skips the 02:xx wall hour.
    A daily 02:30 schedule used to map (fold=0) to an epoch in the FUTURE,
    violating 'at or before now' and refiring on every tick (verified 361
    duplicate fires in 30 min). The epoch-space wrap-around fixes it."""

    @needs_tz
    @pytest.mark.unit
    def test_last_occurrence_never_in_future_through_the_gap(self):
        s = Schedule.daily("02:30")
        # Sweep 01:55 → 04:00 across the gap in 5s ticks: the invariant
        # "last_occurrence(now) <= now" must hold on every tick.
        start = _berlin(2026, 3, 29, 1, 55)
        for i in range(int((2 * 3600 + 300) / 5)):
            now = start + i * 5
            assert s.last_occurrence(now, BERLIN) <= now

    @needs_tz
    @pytest.mark.unit
    def test_daily_in_gap_fires_exactly_once(self):
        """The verified refire storm: tick a daily 02:30 job every 5s across
        the whole spring-forward night. It must fire exactly once."""
        s = Schedule.daily("02:30")
        last_run = _berlin(2026, 3, 28, 2, 30) + 1  # ran normally yesterday
        fires = 0
        now = _berlin(2026, 3, 29, 1, 0)
        end = _berlin(2026, 3, 29, 5, 0)
        while now < end:
            if s.due(now, last_run, BERLIN):
                fires += 1
                last_run = now  # owner stamps last-run on fire
            now += 5
        assert fires == 1

    @needs_tz
    @pytest.mark.unit
    def test_weekly_in_gap_fires_exactly_once(self):
        # 2026-03-29 is a Sunday; weekly Sunday 02:30 hits the gap head-on.
        s = Schedule.weekly("sunday", "02:30")
        last_run = _berlin(2026, 3, 22, 2, 31)  # ran last Sunday
        fires = 0
        now = _berlin(2026, 3, 29, 1, 0)
        end = _berlin(2026, 3, 29, 5, 0)
        while now < end:
            if s.due(now, last_run, BERLIN):
                fires += 1
                last_run = now
            now += 5
        assert fires == 1


class TestDSTFallBack:
    """F-083: Europe/Berlin 2026-10-25 03:00→02:00 replays the 02:xx wall
    hour, so the SAME daily 02:30 slot exists at two epochs an hour apart.
    The wall-identity dedupe must stop the second fire."""

    @needs_tz
    @pytest.mark.unit
    def test_daily_runs_once_across_repeated_hour(self):
        s = Schedule.daily("02:30")
        first = _berlin(2026, 10, 25, 2, 30, fold=0)   # CEST 02:30
        second = _berlin(2026, 10, 25, 2, 30, fold=1)  # CET 02:30, 1h later
        assert second - first == 3600  # sanity: the hour really repeats

        # Job fired at the first 02:30…
        assert s.due(first, _berlin(2026, 10, 24, 2, 31), BERLIN) is True
        last_run = first
        # …the second 02:30 must NOT refire (same wall slot).
        assert s.due(second, last_run, BERLIN) is False
        assert s.due(second + 300, last_run, BERLIN) is False
        # Next day's occurrence still fires.
        assert s.due(_berlin(2026, 10, 26, 2, 30), last_run, BERLIN) is True

    @needs_tz
    @pytest.mark.unit
    def test_weekly_runs_once_across_repeated_hour(self):
        # 2026-10-25 is a Sunday.
        s = Schedule.weekly("sunday", "02:30")
        first = _berlin(2026, 10, 25, 2, 30, fold=0)
        second = _berlin(2026, 10, 25, 2, 30, fold=1)
        last_run = first
        assert s.due(second, last_run, BERLIN) is False
        assert s.due(_berlin(2026, 11, 1, 2, 30), last_run, BERLIN) is True

    @needs_tz
    @pytest.mark.unit
    def test_monthly_runs_once_across_repeated_hour(self):
        s = Schedule.monthly(25, "02:30")
        first = _berlin(2026, 10, 25, 2, 30, fold=0)
        second = _berlin(2026, 10, 25, 2, 30, fold=1)
        last_run = first
        assert s.due(second, last_run, BERLIN) is False
        assert s.due(_berlin(2026, 11, 25, 2, 30), last_run, BERLIN) is True


class TestDedupeKeepsSameDayBaselineFiring:
    """Regression guard for the F-083 dedupe: a baseline seeded EARLIER the
    same local day (fire_on_first=False seeds last_run=now at first sight)
    must NOT suppress that day's occurrence — the covering occurrence of the
    baseline is yesterday's, so the wall identities differ."""

    @pytest.mark.unit
    def test_baseline_seeded_before_todays_occurrence_still_fires(self):
        s = Schedule.daily("08:00")
        baseline = _epoch(2026, 6, 1, 1, 0)   # daemon started 01:00
        assert s.due(_epoch(2026, 6, 1, 8, 0), baseline, UTC) is True

    @pytest.mark.unit
    def test_weekly_baseline_midweek_still_fires(self):
        s = Schedule.weekly(0, "06:00")  # Monday
        baseline = _epoch(2026, 6, 5, 12, 0)  # seeded Friday
        assert s.due(_epoch(2026, 6, 8, 6, 0), baseline, UTC) is True
