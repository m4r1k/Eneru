"""Shared scheduling primitives: pure due-time / next-occurrence logic.

Each schedule is either an interval ("every N seconds") or a wall-clock
calendar time (e.g. "daily at 08:00", "monthly on the 1st"). The daemon
compares a schedule against a persisted last-run timestamp (kept in the
stats ``meta`` table, not process memory, so an infrequent job like a
monthly self-test still fires on the right day across restarts -- a
``time.monotonic`` timer would silently reset on every restart and never
reach 30 days).

Everything here is pure and unit-testable: the calendar math takes an
injectable ``tz`` (tests pass UTC for determinism; the runtime passes
``None`` = the daemon's local time).

ISS-057: an unused ``PeriodicScheduler`` runtime owner used to live here.
The daemon drives its periodic work directly (see ``reports``/battery-health
wiring), so the dead class and its tests were removed.
"""

import calendar as _calendar
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from typing import Optional, Tuple, Union

__all__ = ["Schedule", "parse_hhmm", "parse_weekday"]

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
