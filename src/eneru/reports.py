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
    "build_aggregate_report",
    "build_report",
    "gather_report_sources",
    "maybe_send_due_reports",
    "maybe_send_due_reports_multi",
    "schedule_for_period",
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


def _section_lines(sources: Dict, include: List[str], *, indent: str = "  ") -> List[str]:
    """Render the per-UPS report sections (no title) for one sources dict."""
    lines: List[str] = []
    if "energy" in include:
        e = sources.get("energy") or {}
        lines.append("Energy:")
        lines.append(f"{indent}Today: {_fmt_kwh(e.get('todayKwh'))}"
                     + (f"  ({e['todayCostFormatted']})"
                        if e.get("todayCostFormatted") else ""))
        lines.append(f"{indent}Month: {_fmt_kwh(e.get('monthKwh'))}"
                     + (f"  ({e['monthCostFormatted']})"
                        if e.get("monthCostFormatted") else ""))
        if e.get("estimated"):
            lines.append(f"{indent}(estimated — UPS does not report real power)")
        lines.append("")

    if "battery_health" in include:
        bh = sources.get("battery_health")
        lines.append("Battery health:")
        if bh and bh.get("score") is not None:
            lines.append(f"{indent}Score: {bh['score']:.0f}/100"
                         f" (confidence {bh.get('confidence', 0):.0%})")
        else:
            lines.append(f"{indent}Score: unknown (insufficient telemetry)")
        lines.append("")

    if "events" in include:
        events = sources.get("events") or []
        counts: Dict[str, int] = {}
        for _ts, etype, _detail in events:
            counts[etype] = counts.get(etype, 0) + 1
        lines.append(f"Power events ({len(events)} total):")
        if counts:
            for etype in sorted(counts):
                lines.append(f"{indent}{etype}: {counts[etype]}")
        else:
            lines.append(f"{indent}none")
        lines.append("")

    if "uptime" in include:
        up = sources.get("uptime") or {}
        starts = up.get("daemon_starts", 0)
        since = up.get("since")
        since_txt = (datetime.fromtimestamp(since).isoformat(timespec="minutes")
                     if since else "unknown")
        lines.append("Uptime:")
        lines.append(f"{indent}Daemon starts in window: {starts}")
        lines.append(f"{indent}Running since: {since_txt}")
        lines.append("")
    return lines


def _events_csv(*source_dicts: Dict) -> str:
    buf = io.StringIO()
    writer = _csv.writer(buf)
    writer.writerow(["ups", "timestamp", "event_type", "detail"])
    for sources in source_dicts:
        ups = sources.get("ups_name", "UPS")
        for ts, etype, detail in (sources.get("events") or []):
            writer.writerow([ups, datetime.fromtimestamp(ts).isoformat(),
                             etype, detail or ""])
    return buf.getvalue()


def build_report(period: str, sources: Dict, *, include: List[str],
                 fmt: str = "text") -> Dict:
    """Assemble a single-UPS report from pre-fetched ``sources``.

    Returns ``{"subject", "body", "csv"}`` (``csv`` is ``None`` unless
    ``fmt == "csv"``). ``include`` selects sections (events / battery_health /
    energy / uptime).
    """
    ups = sources.get("ups_name", "UPS")
    lines = [f"📊 Eneru {period} report — {ups}", ""]
    lines += _section_lines(sources, include)
    body = "\n".join(lines).rstrip() + "\n"
    csv_text = _events_csv(sources) if fmt == "csv" else None
    return {"subject": f"Eneru {period} report — {ups}",
            "body": body, "csv": csv_text}


def build_aggregate_report(period: str, per_ups_sources: List[Dict], *,
                           include: List[str], fmt: str = "text") -> Dict:
    """Assemble ONE daemon-wide report with a section per UPS (multi-UPS mode).

    ``per_ups_sources`` is a list of ``gather_report_sources`` dicts, one per
    UPS. The body carries a per-UPS block so the digest truly covers the whole
    fleet rather than just the first monitor.
    """
    n = len(per_ups_sources)
    lines = [f"📊 Eneru {period} report — {n} UPS", ""]
    for sources in per_ups_sources:
        ups = sources.get("ups_name", "UPS")
        lines.append(f"━━ {ups} ━━")
        lines += _section_lines(sources, include)
    body = "\n".join(lines).rstrip() + "\n"
    csv_text = _events_csv(*per_ups_sources) if fmt == "csv" else None
    return {"subject": f"Eneru {period} report — {n} UPS",
            "body": body, "csv": csv_text}


def _period_start(period: str, now: float) -> int:
    """Calendar-aware start for a report period (local time).

    ``daily`` = since local midnight, ``monthly`` = since the 1st — so the
    report's own period matches the calendar boundary a reader expects (and the
    energy windows / status.py) rather than a rolling 24h/30d. ``weekly`` has no
    clean calendar anchor, so it stays a 7-day rolling window.
    """
    now_dt = datetime.fromtimestamp(now)
    if period == "daily":
        return int(datetime(now_dt.year, now_dt.month, now_dt.day).timestamp())
    if period == "monthly":
        return int(datetime(now_dt.year, now_dt.month, 1).timestamp())
    return int(now - PERIOD_WINDOW_SECONDS.get(period, 24 * 3600))


def gather_report_sources(store, ups_name: str, energy_config, *,
                          period: str, now: float) -> Dict:
    """Fetch the report sources for one UPS/store over the period window."""
    start = _period_start(period, now)
    sources: Dict = {"ups_name": ups_name}

    events = store.query_events(start, int(now)) if store else []
    sources["events"] = events

    # uptime: count DAEMON_START events in the window, but resolve "since" from a
    # WIDE lookback so a long-lived daemon (no restart in the report window) still
    # reports its real start time instead of "unknown".
    in_window_starts = [ts for ts, etype, _ in events if etype == "DAEMON_START"]
    wide = (store.query_events(int(now - 365 * 86400), int(now)) if store else [])
    all_starts = [ts for ts, etype, _ in wide if etype == "DAEMON_START"]
    sources["uptime"] = {
        "daemon_starts": len(in_window_starts),
        "since": max(all_starts) if all_starts else None,
    }

    # battery health: the most recent stored row.
    bh_rows = store.query_battery_health(start, int(now)) if store else []
    sources["battery_health"] = bh_rows[-1] if bh_rows else None

    # energy: today + month windows. CALENDAR boundaries (local time) — "today"
    # = since local midnight, "month" = since the 1st — mirroring status.py.
    # Cost is only meaningful against a fixed boundary; a rolling 24h/30d isn't
    # what an electricity bill measures.
    if store and getattr(energy_config, "enabled", True):
        now_dt = datetime.fromtimestamp(now)
        today_start = int(datetime(now_dt.year, now_dt.month, now_dt.day).timestamp())
        month_start = int(datetime(now_dt.year, now_dt.month, 1).timestamp())
        today = store.power_samples(today_start, int(now))
        month = store.power_samples(month_start, int(now))
        sources["energy"] = energy_mod.summarize(
            today, month,
            cost_per_kwh=energy_config.cost_per_kwh,
            currency=energy_config.currency,
            cost_format=energy_config.cost_format,
            nominal_fallback=getattr(energy_config, "nominal_power", None))
    else:
        sources["energy"] = {}
    return sources


def maybe_send_due_reports(config, store, ups_name: str,
                           enqueue: Callable[[str, str, str], object], *,
                           now: Optional[float] = None,
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
        sources = gather_report_sources(
            store, ups_name, config.energy, period=period, now=now)
        content = build_report(period, sources, include=reports.include,
                               fmt=reports.format)
        enqueue(_compose_message(content), "info", "report")
        # Stamp AFTER handing the message to the (persistent) notification queue,
        # so a transient enqueue failure (which raises) is retried next tick
        # instead of permanently dropping the period's report.
        store.set_meta(key, str(int(now)))
        sent.append(period)
    return sent


def _compose_message(content: Dict) -> str:
    """Body for delivery. Honors `format: csv` by appending the machine-readable
    CSV block under the human summary (the notification channel is text-only)."""
    message = content["body"]
    if content.get("csv"):
        message = message + "\n\n--- CSV ---\n" + content["csv"]
    return message


def maybe_send_due_reports_multi(config, units, meta_store,
                                 enqueue: Callable[[str, str, str], object], *,
                                 now: Optional[float] = None,
                                 tz=None) -> List[str]:
    """Daemon-wide multi-UPS reports: ONE digest per period covering every UPS.

    ``units`` is ``[(ups_name, store, energy_config), ...]``; ``meta_store`` is
    where the ``last_report_sent_<period>`` dedup keys live (a single deterministic
    place so the daemon never double-sends). Mirrors ``maybe_send_due_reports``
    but aggregates per-UPS sections into one body.
    """
    reports = config.reports
    if not reports.enabled or meta_store is None or not units:
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
        raw = meta_store.get_meta(key)
        try:
            last = float(raw) if raw else None
        except (TypeError, ValueError):
            last = None
        if not sched.due(now, last, tz):
            if last is None:
                meta_store.set_meta(key, str(int(now)))  # seed baseline
            continue
        per_ups = [
            gather_report_sources(store, ups_name, energy_cfg,
                                  period=period, now=now)
            for ups_name, store, energy_cfg in units
        ]
        content = build_aggregate_report(period, per_ups,
                                         include=reports.include,
                                         fmt=reports.format)
        enqueue(_compose_message(content), "info", "report")
        meta_store.set_meta(key, str(int(now)))  # stamp after enqueue
        sent.append(period)
    return sent
