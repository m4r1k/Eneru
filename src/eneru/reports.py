"""Periodic summary reports (v6.1).

Assembles a daily/weekly/monthly digest -- power events, battery health,
energy, and uptime -- and delivers it as an INFO notification tagged
``category="report"``. ``category`` is the notification queue's coalescing
concept, NOT the ``notifications.suppress`` mechanism (which validates power
*event* names); reports are gated solely by ``reports.enabled`` + the
per-period toggles.

``build_report`` is pure (takes pre-fetched sources) so it is fully
unit-testable; ``gather_report_sources`` does the StatsStore reads and
``maybe_send_due_reports`` is the scheduler-driven entry point the monitor /
coordinator call once per loop.
"""

import csv as _csv
import io
import time
from datetime import datetime
from typing import Callable, Dict, List, Optional

from eneru import energy as energy_mod
from eneru.scheduler import Schedule

__all__ = [
    "PERIOD_WINDOW_SECONDS",
    "build_report",
    "gather_report_sources",
    "schedule_for_period",
    "maybe_send_due_reports",
]

PERIOD_WINDOW_SECONDS = {
    "daily": 24 * 3600,
    "weekly": 7 * 86400,
    "monthly": 30 * 86400,
}


def schedule_for_period(period: str, reports_config) -> Schedule:
    """Build the Schedule for one report period from the reports config."""
    t = reports_config.time
    if period == "daily":
        return Schedule.daily(t, fire_on_first=False)
    if period == "weekly":
        return Schedule.weekly(reports_config.weekly_day, t, fire_on_first=False)
    if period == "monthly":
        return Schedule.monthly(reports_config.monthly_day, t, fire_on_first=False)
    raise ValueError(f"unknown report period {period!r}")


def _fmt_kwh(value: Optional[float]) -> str:
    return f"{value:.3f} kWh" if value is not None else "unknown"


def build_report(period: str, sources: Dict, *, include: List[str],
                 fmt: str = "text") -> Dict:
    """Assemble a report from pre-fetched ``sources``.

    Returns ``{"subject", "body", "csv"}`` (``csv`` is ``None`` unless
    ``fmt == "csv"``). ``include`` selects sections (events / battery_health /
    energy / uptime).
    """
    ups = sources.get("ups_name", "UPS")
    lines = [f"📊 Eneru {period} report — {ups}", ""]

    if "energy" in include:
        e = sources.get("energy") or {}
        lines.append("Energy:")
        lines.append(f"  Today: {_fmt_kwh(e.get('todayKwh'))}"
                     + (f"  ({e['todayCostFormatted']})"
                        if e.get("todayCostFormatted") else ""))
        lines.append(f"  Month: {_fmt_kwh(e.get('monthKwh'))}"
                     + (f"  ({e['monthCostFormatted']})"
                        if e.get("monthCostFormatted") else ""))
        if e.get("estimated"):
            lines.append("  (estimated — UPS does not report real power)")
        lines.append("")

    if "battery_health" in include:
        bh = sources.get("battery_health")
        lines.append("Battery health:")
        if bh and bh.get("score") is not None:
            lines.append(f"  Score: {bh['score']:.0f}/100"
                         f" (confidence {bh.get('confidence', 0):.0%})")
        else:
            lines.append("  Score: unknown (insufficient telemetry)")
        lines.append("")

    if "events" in include:
        events = sources.get("events") or []
        counts: Dict[str, int] = {}
        for _ts, etype, _detail in events:
            counts[etype] = counts.get(etype, 0) + 1
        lines.append(f"Power events ({len(events)} total):")
        if counts:
            for etype in sorted(counts):
                lines.append(f"  {etype}: {counts[etype]}")
        else:
            lines.append("  none")
        lines.append("")

    if "uptime" in include:
        up = sources.get("uptime") or {}
        starts = up.get("daemon_starts", 0)
        since = up.get("since")
        since_txt = (datetime.fromtimestamp(since).isoformat(timespec="minutes")
                     if since else "unknown")
        lines.append("Uptime:")
        lines.append(f"  Daemon starts in window: {starts}")
        lines.append(f"  Running since: {since_txt}")
        lines.append("")

    body = "\n".join(lines).rstrip() + "\n"

    csv_text = None
    if fmt == "csv":
        buf = io.StringIO()
        writer = _csv.writer(buf)
        writer.writerow(["timestamp", "event_type", "detail"])
        for ts, etype, detail in (sources.get("events") or []):
            writer.writerow([datetime.fromtimestamp(ts).isoformat(),
                             etype, detail or ""])
        csv_text = buf.getvalue()

    return {"subject": f"Eneru {period} report — {ups}",
            "body": body, "csv": csv_text}


def gather_report_sources(store, ups_name: str, energy_config, *,
                          period: str, now: float,
                          expected_interval_s: float = 1.0) -> Dict:
    """Fetch the report sources for one UPS/store over the period window."""
    window = PERIOD_WINDOW_SECONDS.get(period, 24 * 3600)
    start = int(now - window)
    sources: Dict = {"ups_name": ups_name}

    events = store.query_events(start, int(now)) if store else []
    sources["events"] = events

    # uptime: count DAEMON_START events in the window; "since" = the most recent.
    daemon_starts = [ts for ts, etype, _ in events if etype == "DAEMON_START"]
    sources["uptime"] = {
        "daemon_starts": len(daemon_starts),
        "since": max(daemon_starts) if daemon_starts else None,
    }

    # battery health: the most recent stored row.
    bh_rows = store.query_battery_health(start, int(now)) if store else []
    sources["battery_health"] = bh_rows[-1] if bh_rows else None

    # energy: today + month windows.
    if store and getattr(energy_config, "enabled", True):
        today_start = int(now - 24 * 3600)
        month_start = int(now - 30 * 86400)
        today = store.power_samples(today_start, int(now))
        month = store.power_samples(month_start, int(now))
        sources["energy"] = energy_mod.summarize(
            today, month,
            cost_per_kwh=energy_config.cost_per_kwh,
            currency=energy_config.currency,
            cost_format=energy_config.cost_format,
            expected_interval_s=expected_interval_s)
    else:
        sources["energy"] = {}
    return sources


def maybe_send_due_reports(config, store, ups_name: str,
                           enqueue: Callable[[str, str, str], object], *,
                           now: Optional[float] = None,
                           expected_interval_s: float = 1.0,
                           tz=None) -> List[str]:
    """Send any due periodic reports. Returns the periods sent.

    Stateless across calls: due-ness is decided from ``meta``
    (``last_report_sent_<period>``) so a restart never double-sends, and the
    first sight of a period seeds the baseline (no blast on startup).
    """
    reports = config.reports
    if not reports.enabled or store is None:
        return []
    if now is None:
        now = time.time()
    sent: List[str] = []
    for period, enabled in (("daily", reports.daily),
                            ("weekly", reports.weekly),
                            ("monthly", reports.monthly)):
        if not enabled:
            continue
        try:
            sched = schedule_for_period(period, reports)
        except ValueError:
            continue
        key = f"last_report_sent_{period}"
        raw = store.get_meta(key)
        try:
            last = float(raw) if raw else None
        except (TypeError, ValueError):
            last = None
        if not sched.due(now, last, tz):
            if last is None:
                store.set_meta(key, str(int(now)))  # seed baseline, don't send
            continue
        store.set_meta(key, str(int(now)))  # stamp before send (no retry storm)
        sources = gather_report_sources(
            store, ups_name, config.energy, period=period, now=now,
            expected_interval_s=expected_interval_s)
        content = build_report(period, sources, include=reports.include,
                               fmt=reports.format)
        enqueue(content["body"], "info", "report")
        sent.append(period)
    return sent
