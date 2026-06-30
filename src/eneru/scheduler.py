"""Shared periodic scheduler: pure due-time logic + a small runtime owner.

Think of a kitchen timer board. Each job has a schedule (every N seconds,
or a wall-clock time like "daily at 08:00") and a last-run sticky note in
the stats ``meta`` table. On each :meth:`PeriodicScheduler.tick` the owner
reads the sticky note, asks the pure :class:`Schedule` "are we past due?",
runs the job if so, and writes a fresh note. Because last-run lives in
``meta`` (not in process memory), an infrequent job like a monthly
self-test still fires on the right day even if the daemon restarted in
between -- a ``time.monotonic`` timer would silently reset on every
restart and never reach 30 days.

Wiring (see ``src/eneru/AGENTS.md`` "Periodic scheduling"):

- **Per-UPS jobs** (battery-health update, self-test issue/poll) register
  on a per-monitor :class:`PeriodicScheduler` and tick from the end of
  ``UPSGroupMonitor._main_loop`` (failure-isolated, before the sleep).
- **Daemon-wide jobs** (periodic reports -- one digest, not N copies)
  register on a single owner ticked by the ``MultiUPSCoordinator`` loop in
  multi-UPS mode, or by the single monitor in single-UPS mode.

Everything in this module is pure/threadless and unit-testable: the
calendar math takes an injectable ``tz`` (tests pass UTC for determinism;
the runtime passes ``None`` = the daemon's local time), and the owner
takes ``get_meta`` / ``set_meta`` callables instead of touching SQLite
directly.
"""

import calendar as _calendar
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from typing import Callable, Dict, List, Optional, Tuple, Union

__all__ = ["PeriodicScheduler", "Schedule", "parse_hhmm", "parse_weekday"]

_WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}


def parse_hhmm(value: str) -> Tuple[int, int]:
    """Parse a ``"HH:MM"`` time-of-day into ``(hour, minute)``."""
    parts = str(value).strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"invalid time-of-day {value!r} (want HH:MM)")
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        raise ValueError(f"invalid time-of-day {value!r} (want HH:MM)")
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"time-of-day out of range: {value!r}")
    return hour, minute


def parse_weekday(value: Union[int, str]) -> int:
    """Parse a weekday name (or 0-6 int, Monday=0) into 0-6."""
    if isinstance(value, bool):  # bool is an int subclass; reject explicitly
        raise ValueError(f"invalid weekday {value!r}")
    if isinstance(value, int):
        if 0 <= value <= 6:
            return value
        raise ValueError(f"weekday out of range: {value!r}")
    key = str(value).strip().lower()
    if key in _WEEKDAYS:
        return _WEEKDAYS[key]
    raise ValueError(f"invalid weekday {value!r}")


def _dt(now: float, tz: Optional[tzinfo]) -> datetime:
    """Local (tz=None) or tz-aware datetime for an epoch second."""
    return datetime.fromtimestamp(now, tz)


@dataclass
class Schedule:
    """A job's cadence. Build via the constructors, not the raw fields.

    ``fire_on_first`` controls the very first tick when there is no
    last-run note yet: intervals default to firing immediately (good for a
    "compute battery health now, then hourly" job), while calendar
    schedules default to *not* firing on startup (so a daily report does
    not blast out on every restart -- the first sight just seeds the
    baseline and the first real fire is the next scheduled occurrence).
    """

    kind: str  # "interval" | "daily" | "weekly" | "monthly"
    interval_seconds: Optional[float] = None
    hour: int = 0
    minute: int = 0
    weekday: Optional[int] = None  # 0=Mon .. 6=Sun
    day: Optional[int] = None      # day of month, 1-31 (clamped to month length)
    fire_on_first: bool = True

    # --- constructors ---

    @classmethod
    def interval(cls, seconds: float, *, fire_on_first: bool = True) -> "Schedule":
        if seconds is None or seconds <= 0:
            raise ValueError("interval seconds must be > 0")
        # Keep sub-second / fractional intervals intact — int() truncation turned
        # interval(0.5) into 0 (permanently "due") and interval(1.9) into 1.
        return cls("interval", interval_seconds=float(seconds),
                   fire_on_first=fire_on_first)

    @classmethod
    def daily(cls, hhmm: str, *, fire_on_first: bool = False) -> "Schedule":
        hour, minute = parse_hhmm(hhmm)
        return cls("daily", hour=hour, minute=minute, fire_on_first=fire_on_first)

    @classmethod
    def weekly(cls, weekday: Union[int, str], hhmm: str, *,
               fire_on_first: bool = False) -> "Schedule":
        hour, minute = parse_hhmm(hhmm)
        return cls("weekly", weekday=parse_weekday(weekday),
                   hour=hour, minute=minute, fire_on_first=fire_on_first)

    @classmethod
    def monthly(cls, day: int, hhmm: str, *,
                fire_on_first: bool = False) -> "Schedule":
        hour, minute = parse_hhmm(hhmm)
        dom = int(day)
        if not (1 <= dom <= 31):
            raise ValueError(f"day-of-month out of range: {day!r}")
        return cls("monthly", day=dom, hour=hour, minute=minute,
                   fire_on_first=fire_on_first)

    # --- occurrence math (calendar kinds only) ---

    def last_occurrence(self, now: float, tz: Optional[tzinfo] = None) -> float:
        """Epoch of the most recent scheduled instant at or before ``now``."""
        d = _dt(now, tz)
        if self.kind == "daily":
            cand = d.replace(hour=self.hour, minute=self.minute,
                             second=0, microsecond=0)
            if cand > d:
                cand -= timedelta(days=1)
            return cand.timestamp()
        if self.kind == "weekly":
            cand = d.replace(hour=self.hour, minute=self.minute,
                             second=0, microsecond=0)
            cand -= timedelta(days=(d.weekday() - self.weekday) % 7)
            if cand > d:
                cand -= timedelta(days=7)
            return cand.timestamp()
        if self.kind == "monthly":
            cand = self._month_instant(d.year, d.month, d, tz)
            if cand > now:
                year, month = (d.year - 1, 12) if d.month == 1 \
                    else (d.year, d.month - 1)
                cand = self._month_instant(year, month, d, tz)
            return cand
        raise ValueError(f"last_occurrence undefined for kind {self.kind!r}")

    def _month_instant(self, year: int, month: int,
                       ref: datetime, tz: Optional[tzinfo]) -> float:
        dom = min(self.day, _calendar.monthrange(year, month)[1])
        return ref.replace(year=year, month=month, day=dom, hour=self.hour,
                           minute=self.minute, second=0,
                           microsecond=0).timestamp()

    def next_occurrence(self, now: float, tz: Optional[tzinfo] = None) -> float:
        """Epoch of the next scheduled instant strictly after ``now``."""
        if self.kind == "interval":
            return now + float(self.interval_seconds)
        last = self.last_occurrence(now, tz)
        d = _dt(last, tz)
        if self.kind == "daily":
            return (d + timedelta(days=1)).timestamp()
        if self.kind == "weekly":
            return (d + timedelta(days=7)).timestamp()
        # monthly: advance one calendar month, clamping the day
        year, month = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
        return self._month_instant(year, month, d, tz)

    # --- due check ---

    def due(self, now: float, last_run: Optional[float],
            tz: Optional[tzinfo] = None) -> bool:
        """Is the job due at ``now`` given its ``last_run`` epoch (or None)?"""
        if self.kind == "interval":
            if last_run is None:
                return self.fire_on_first
            return (now - last_run) >= self.interval_seconds
        # calendar kinds
        if last_run is None:
            return self.fire_on_first
        return last_run < self.last_occurrence(now, tz)

    def next_run(self, now: float, last_run: Optional[float],
                 tz: Optional[tzinfo] = None) -> float:
        """Epoch when the job will next fire (``now`` if already due)."""
        if self.due(now, last_run, tz):
            return now
        if self.kind == "interval":
            base = last_run if last_run is not None else now
            return base + float(self.interval_seconds)
        return self.next_occurrence(now, tz)


@dataclass
class _Job:
    name: str
    schedule: Schedule
    run: Callable[[float], None]


class PeriodicScheduler:
    """Runs registered jobs when due, persisting last-run to ``meta``.

    Threadless: the owner (a monitor or the coordinator) calls
    :meth:`tick` from its existing loop. ``get_meta`` / ``set_meta`` are
    the stats-store accessors; passing them in keeps this class free of
    SQLite and trivially unit-testable. Each job is failure-isolated -- a
    raising job is logged and never breaks the tick or its siblings -- and
    last-run is stamped *before* the job body runs, so a job that throws is
    not re-attempted until its next occurrence (no retry storm, and no
    re-issuing a self-test command every second).
    """

    def __init__(self, *, meta_prefix: str = "sched:",
                 tz: Optional[tzinfo] = None,
                 log: Optional[Callable[[str], None]] = None) -> None:
        self._jobs: List[_Job] = []
        self._meta_prefix = meta_prefix
        self._tz = tz
        self._log = log or (lambda _m: None)

    def register(self, name: str, schedule: Schedule,
                 run: Callable[[float], None]) -> None:
        self._jobs.append(_Job(name, schedule, run))

    def clear(self) -> None:
        """Drop all registered jobs (used by apply_reload before re-register)."""
        self._jobs = []

    @property
    def job_names(self) -> List[str]:
        return [j.name for j in self._jobs]

    def _read_last(self, get_meta: Callable[[str], Optional[str]],
                   name: str) -> Optional[float]:
        # NOTE: a get_meta() failure deliberately propagates to tick(), which
        # skips the job for this tick. Swallowing it here and returning None
        # would make tick() take the no-baseline path: it reseeds a calendar
        # job (silently skipping a due run) or fires an interval job early.
        raw = get_meta(self._meta_prefix + name)
        if raw in (None, ""):
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    def tick(self, now: float,
             get_meta: Callable[[str], Optional[str]],
             set_meta: Callable[[str, str], None]) -> List[str]:
        """Run every due job once. Returns the names of the jobs that ran."""
        ran: List[str] = []
        for job in self._jobs:
            key = self._meta_prefix + job.name
            try:
                last = self._read_last(get_meta, job.name)
            except Exception as exc:  # state read failed -> skip, don't reseed
                self._log(
                    f"⚠️  scheduler state read failed for '{job.name}': {exc}")
                continue
            try:
                if job.schedule.due(now, last, self._tz):
                    # Stamp + record as fired first, so a job whose body
                    # raises is logged but not re-attempted every tick.
                    # Persist the full float (Schedule.interval keeps fractional
                    # seconds); truncating with int() makes last_run earlier than
                    # the real fire time, so interval jobs can become due too
                    # early on the next tick or after a restart.
                    set_meta(key, repr(now))
                    ran.append(job.name)
                    job.run(now)
                elif last is None:
                    # Seed the baseline so the first real fire is the next
                    # occurrence, not an immediate one on every restart.
                    set_meta(key, repr(now))
            except Exception as exc:  # failure isolation
                self._log(f"⚠️  scheduler job '{job.name}' failed: {exc}")
        return ran

    def next_runs(self, now: float,
                  get_meta: Callable[[str], Optional[str]]) -> Dict[str, float]:
        """Map each job name to its next-fire epoch (for status/API)."""
        out: Dict[str, float] = {}
        for job in self._jobs:
            last = self._read_last(get_meta, job.name)
            out[job.name] = job.schedule.next_run(now, last, self._tz)
        return out
