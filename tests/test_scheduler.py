"""Unit tests for the shared periodic scheduler (src/eneru/scheduler.py).

Calendar math uses an injected UTC tz so the assertions are independent of
the machine timezone.
"""

from datetime import datetime, timezone

import pytest

from eneru.scheduler import (
    PeriodicScheduler,
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


# --------------------------------------------------------------------------
# PeriodicScheduler runtime owner
# --------------------------------------------------------------------------

class _Meta:
    """In-memory meta store with get/set closures for the scheduler."""

    def __init__(self):
        self.data = {}

    def get(self, k):
        return self.data.get(k)

    def set(self, k, v):
        self.data[k] = v


class TestPeriodicScheduler:
    @pytest.mark.unit
    def test_interval_job_fires_and_persists(self):
        meta = _Meta()
        calls = []
        sched = PeriodicScheduler(tz=UTC)
        sched.register("health", Schedule.interval(60), lambda now: calls.append(now))
        ran = sched.tick(1000.0, meta.get, meta.set)
        assert ran == ["health"]
        assert calls == [1000.0]
        # The full run timestamp is persisted (not truncated to an int), so
        # compare the parsed float rather than locking to a string form.
        assert float(meta.data["sched:health"]) == 1000.0
        # not due 30s later
        assert sched.tick(1030.0, meta.get, meta.set) == []
        # due 60s later
        assert sched.tick(1060.0, meta.get, meta.set) == ["health"]

    @pytest.mark.unit
    def test_state_read_failure_skips_job_without_reseeding(self):
        # A get_meta() failure must NOT be read as "first run": that would
        # reseed a calendar job (skipping a due run) or fire an interval job
        # early. The job is skipped for this tick and its state left untouched.
        sets = []
        calls = []
        sched = PeriodicScheduler(tz=UTC)
        sched.register("health", Schedule.interval(60), lambda now: calls.append(now))

        def _boom(_k):
            raise RuntimeError("stats db gone")
        ran = sched.tick(1000.0, _boom, lambda k, v: sets.append((k, v)))
        assert ran == []          # not fired
        assert calls == []        # body never ran
        assert sets == []         # state not reseeded

    @pytest.mark.unit
    def test_calendar_job_seeds_then_fires_next_occurrence(self):
        meta = _Meta()
        calls = []
        sched = PeriodicScheduler(tz=UTC)
        sched.register("report", Schedule.daily("08:00"),
                       lambda now: calls.append(now))
        # First tick at 09:00 seeds baseline, does NOT fire.
        assert sched.tick(_epoch(2026, 6, 1, 9, 0), meta.get, meta.set) == []
        assert calls == []
        assert "sched:report" in meta.data
        # Next day after 08:00 -> fires.
        assert sched.tick(_epoch(2026, 6, 2, 8, 30), meta.get, meta.set) == ["report"]
        assert len(calls) == 1

    @pytest.mark.unit
    def test_stamp_before_run_prevents_retry_storm(self):
        meta = _Meta()
        sched = PeriodicScheduler(tz=UTC)

        def boom(now):
            raise RuntimeError("kaboom")

        sched.register("flaky", Schedule.interval(60), boom)
        # Job raises but is logged + last-run stamped, so it doesn't retry
        # until the next interval.
        assert sched.tick(1000.0, meta.get, meta.set) == ["flaky"]
        assert float(meta.data["sched:flaky"]) == 1000.0
        assert sched.tick(1030.0, meta.get, meta.set) == []  # not retried

    @pytest.mark.unit
    def test_failure_isolation_across_jobs(self):
        meta = _Meta()
        ok_calls = []
        sched = PeriodicScheduler(tz=UTC)
        sched.register("bad", Schedule.interval(60),
                       lambda now: (_ for _ in ()).throw(ValueError("x")))
        sched.register("good", Schedule.interval(60),
                       lambda now: ok_calls.append(now))
        sched.tick(1000.0, meta.get, meta.set)
        assert ok_calls == [1000.0]  # good ran despite bad raising

    @pytest.mark.unit
    def test_logs_on_job_failure(self):
        logs = []
        meta = _Meta()
        sched = PeriodicScheduler(tz=UTC, log=logs.append)
        sched.register("bad", Schedule.interval(60),
                       lambda now: (_ for _ in ()).throw(ValueError("x")))
        sched.tick(1000.0, meta.get, meta.set)
        assert any("bad" in m for m in logs)

    @pytest.mark.unit
    def test_corrupt_meta_value_treated_as_unrun(self):
        meta = _Meta()
        meta.data["sched:health"] = "not-a-number"
        calls = []
        sched = PeriodicScheduler(tz=UTC)
        sched.register("health", Schedule.interval(60), lambda now: calls.append(now))
        assert sched.tick(1000.0, meta.get, meta.set) == ["health"]  # fires (last=None)

    @pytest.mark.unit
    def test_clear_and_job_names(self):
        sched = PeriodicScheduler()
        sched.register("a", Schedule.interval(60), lambda now: None)
        sched.register("b", Schedule.interval(60), lambda now: None)
        assert sched.job_names == ["a", "b"]
        sched.clear()
        assert sched.job_names == []

    @pytest.mark.unit
    def test_next_runs(self):
        meta = _Meta()
        meta.data["sched:health"] = "1000"
        sched = PeriodicScheduler(tz=UTC)
        sched.register("health", Schedule.interval(60), lambda now: None)
        assert sched.next_runs(1000.0, meta.get) == {"health": 1060.0}
