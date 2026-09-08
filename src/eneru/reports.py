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
from datetime import datetime, timedelta
from statistics import median
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


def _outage_summary(events: List[tuple], end_ts: int) -> tuple:
    """Return ``(count, total_seconds, open_count)`` for OB/OL pairs."""
    starts: List[int] = []
    total = 0
    count = 0
    for ts, event_type, _detail in events:
        if event_type == "ON_BATTERY":
            starts.append(int(ts))
            count += 1
        elif event_type == "POWER_RESTORED" and starts:
            total += max(0, int(ts) - starts.pop(0))
    total += sum(max(0, int(end_ts) - started) for started in starts)
    return count, total, len(starts)


def _fmt_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, remainder = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m" + (f" {remainder}s" if remainder else "")
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h" + (f" {minutes}m" if minutes else "")


def _period_label(sources: Dict) -> str:
    start = datetime.fromtimestamp(sources.get("period_start", 0))
    end = datetime.fromtimestamp(sources.get("period_end", 0))
    if start.date() == end.date():
        return start.strftime("%d %b %Y").lstrip("0")
    if start.year == end.year and start.month == end.month:
        return f"{start.day}-{end.day} {end:%b %Y}"
    if start.year == end.year:
        return f"{start.day} {start:%b}-{end.day} {end:%b %Y}"
    return f"{start:%d %b %Y}-{end:%d %b %Y}"


def _summary_lines(sources: Dict, include: List[str], *, width: int = 0) -> List[str]:
    """Render one compact UPS summary in at most two content lines."""
    label = str(sources.get("ups_label") or sources.get("ups_name") or "UPS")
    prefix = f"{label:<{width}}" if width else label
    primary: List[str] = []
    secondary: List[str] = []

    if "energy" in include:
        e = sources.get("energy") or {}
        kwh = _fmt_kwh(e.get("periodKwh"))
        if e.get("estimated") and e.get("periodKwh") is not None:
            kwh = "~" + kwh
        primary.append(kwh)
        if e.get("periodCostFormatted"):
            primary.append(e["periodCostFormatted"])

    if "battery_health" in include:
        bh = sources.get("battery_health")
        if bh and bh.get("score") is not None:
            primary.append(f"🔋 {bh['score']:.0f}")
            conf = (bh.get("detail") or {}).get("confidence")
            if conf is None:
                conf = bh.get("confidence", 0) or 0
            if conf < 1:
                primary.append(f"confidence {conf:.0%}")
        else:
            primary.append("🔋 unknown")

    if "events" in include:
        events = sources.get("events") or []
        count, duration, open_count = _outage_summary(
            events, sources.get(
                "period_end_exclusive",
                sources.get("period_end", int(time.time())),
            ))
        if count:
            text = f"{count} outage{'s' if count != 1 else ''} ({_fmt_duration(duration)})"
            if open_count:
                text += ", ongoing"
            secondary.append(text)
        else:
            secondary.append("no outages")

    if "self_tests" in include:
        tests = sources.get("self_tests") or []
        counts: Dict[str, int] = {}
        for test in tests:
            result = str(test.get("result_enum") or "unknown")
            counts[result] = counts.get(result, 0) + 1
        if counts:
            parts = [f"{count} test{'s' if count != 1 else ''} {result}"
                     for result, count in sorted(counts.items())]
            secondary.append(", ".join(parts))

    if "uptime" in include:
        up = sources.get("uptime") or {}
        starts = up.get("restarts", 0)
        secondary.append(
            "no restarts" if not starts
            else f"{starts} restart{'s' if starts != 1 else ''}")

    lines = [prefix + ("  " + " · ".join(primary) if primary else "")]
    if secondary:
        lines.append(" " * (width + 2 if width else 2) + " · ".join(secondary))
    return lines


def _csv_safe(value) -> str:
    """Neutralize CSV/formula injection (ISS-063).

    A spreadsheet interprets a cell that begins with ``= + - @`` (or a
    leading tab / CR that resolves to one) as a formula, so an attacker-
    controlled UPS name or event detail like ``=HYPERLINK(...)`` would
    execute on open. Prefix such cells with a single quote, which Excel /
    LibreOffice render as a literal leading character."""
    text = "" if value is None else str(value)
    if text and text[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + text
    return text


def _events_csv(*source_dicts: Dict) -> str:
    buf = io.StringIO()
    writer = _csv.writer(buf)
    writer.writerow(["ups", "timestamp", "event_type", "detail"])
    for sources in source_dicts:
        ups = sources.get("ups_name", "UPS")
        for ts, etype, detail in (sources.get("events") or []):
            writer.writerow([
                _csv_safe(ups),
                datetime.fromtimestamp(ts).isoformat(),
                _csv_safe(etype),
                _csv_safe(detail or ""),
            ])
    return buf.getvalue()


def build_report(period: str, sources: Dict, *, include: List[str],
                 fmt: str = "text") -> Dict:
    """Assemble a single-UPS report from pre-fetched ``sources``.

    Returns ``{"subject", "body", "csv"}`` (``csv`` is ``None`` unless
    ``fmt == "csv"``). ``include`` selects sections (events / battery_health /
    self_tests / energy / uptime).
    """
    ups = sources.get("ups_label") or sources.get("ups_name", "UPS")
    lines = [f"📊 {period.title()} report · {_period_label(sources)}", ""]
    lines += _summary_lines(sources, include)
    if ("energy" in include
            and (sources.get("energy") or {}).get("estimated")):
        lines += ["", "~ estimated from UPS load"]
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
    period_text = _period_label(per_ups_sources[0]) if per_ups_sources else ""
    lines = [f"📊 {period.title()} report · {period_text} · {n} UPS", ""]
    width = max((len(str(s.get("ups_label") or s.get("ups_name") or "UPS"))
                 for s in per_ups_sources), default=0)
    for sources in per_ups_sources:
        lines += _summary_lines(sources, include, width=width)
        lines.append("")
    if "energy" in include and per_ups_sources:
        energy = [(s.get("energy") or {}) for s in per_ups_sources]
        if all(e.get("periodKwh") is not None for e in energy):
            total_kwh = sum(e["periodKwh"] for e in energy)
            total = _fmt_kwh(total_kwh)
            if any(e.get("estimated") for e in energy):
                total = "~" + total
            costs = [e.get("periodCost") for e in energy]
            if all(cost is not None for cost in costs):
                first = energy[0]
                total_cost = energy_mod.format_cost(
                    sum(costs), first.get("currency", "USD"),
                    first.get("costFormat"))
                total += f" · {total_cost}"
            lines.append(f"Total  {total}")
    if ("energy" in include
            and any((s.get("energy") or {}).get("estimated")
                    for s in per_ups_sources)):
        lines += ["", "~ estimated from UPS load"]
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
                          period: str, now: float,
                          ups_label: Optional[str] = None) -> Dict:
    """Fetch the report sources for one UPS/store over the period window."""
    start = _period_start(period, now)
    sources: Dict = {"ups_name": ups_name, "ups_label": ups_label or ups_name}

    event_start = start
    event_end = int(now)
    period_end_exclusive = event_end
    if period == "daily":
        # A digest sent at 08:00 should summarize yesterday's complete day,
        # not midnight->08:00 and then permanently lose the other 16 hours.
        # Subtract a calendar day before converting to epoch so 23/25-hour DST
        # days retain the correct local-midnight boundaries.
        now_dt = datetime.fromtimestamp(now)
        current_midnight = datetime(now_dt.year, now_dt.month, now_dt.day)
        previous_midnight = current_midnight - timedelta(days=1)
        event_start = int(previous_midnight.timestamp())
        period_end_exclusive = int(current_midnight.timestamp())
        event_end = period_end_exclusive - 1
    sources["period_start"] = event_start
    sources["period_end"] = event_end
    sources["period_end_exclusive"] = period_end_exclusive

    events = store.query_events(event_start, event_end) if store else []
    if store:
        previous_power = store.latest_power_event_before(event_start)
        if previous_power and previous_power[1] == "ON_BATTERY":
            # Count only the portion of a carry-in outage inside this report.
            events.insert(0, (event_start, "ON_BATTERY", previous_power[2]))
    # The rendered section is labelled "Power events", so keep it to the real
    # power-event set — lifecycle/diagnostic rows (DAEMON_START, etc.) would
    # otherwise be miscounted under that heading and skew the digest. The full
    # `events` list is still used below for the DAEMON_START uptime math.
    from eneru.status import POWER_EVENT_TYPES
    sources["events"] = [e for e in events if e[1] in POWER_EVENT_TYPES]

    # Count classified restarts, not every daemon start. A cold boot or first
    # installation is a start, but it is not an operator-requested restart.
    restart_types = {"DAEMON_RESTARTED", "DAEMON_RESTARTED_AFTER_FATAL"}
    in_window_restarts = [
        ts for ts, event_type, _ in events if event_type in restart_types]
    # Resolve "running since" from ALL retained history (events are sparse and
    # retention-capped), so a daemon up longer than a year still reports its real
    # start instead of "unknown". ISS-038: a targeted MAX(ts) query instead of
    # loading every retained event row just to take a max().
    since = store.latest_event_ts("DAEMON_START") if store else None
    sources["uptime"] = {
        "restarts": len(in_window_restarts),
        "since": since,
    }

    sources["self_tests"] = (
        store.query_self_tests(event_start, event_end) if store else [])

    # Battery health is a current condition, so use the latest retained row
    # even when it predates the report's accounting window.
    sources["battery_health"] = (
        store.latest_battery_health() if store else None)

    # Energy follows the report window. A weekly digest therefore reports the
    # week it summarizes rather than unrelated today/month status figures.
    if store and getattr(energy_config, "enabled", True):
        samples = store.power_samples(event_start, event_end)
        energy_boundary = event_end + 1 if period == "daily" else event_end
        sample_dts = [
            nxt[0] - current[0]
            for current, nxt in zip(samples, samples[1:])
            if nxt[0] - current[0] > 0
        ]
        expected_interval = median(sample_dts) if sample_dts else None
        if (expected_interval is not None
                and samples[-1][0] < energy_boundary):
            # integrate_kwh consumes intervals between samples. Add the window
            # boundary so the final retained bucket contributes up to the end.
            # Pass the cadence inferred from real samples so this synthetic row
            # cannot make a stale final gap look normal.
            samples.append((energy_boundary, None, None, None))
        result = energy_mod.integrate_kwh(
            samples,
            expected_interval_s=expected_interval,
            nominal_fallback=getattr(energy_config, "nominal_power", None))
        cost = energy_mod.compute_cost(result.kwh, energy_config.cost_per_kwh)
        currency = (energy_config.currency or "USD").upper()
        sources["energy"] = {
            "periodKwh": result.kwh,
            "periodCost": cost,
            "periodCostFormatted": (
                energy_mod.format_cost(cost, currency, energy_config.cost_format)
                if cost is not None else None),
            "currency": currency,
            "costFormat": energy_config.cost_format,
            "estimated": result.estimated,
            "partial": result.partial,
        }
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
        # F-081: build before stamping so gather/render failures retry next tick
        # instead of silently burning the whole report period.
        sources = gather_report_sources(
            store, ups_name, config.energy, period=period, now=now,
            ups_label=config.ups.label)
        content = build_report(period, sources, include=reports.include,
                               fmt=reports.format)
        # F-028: stamp the dedup key BEFORE enqueuing, and only send if the stamp
        # committed. ELI5: cross the chore off the whiteboard first, THEN do it --
        # if the marker is dry (the meta write fails, silently swallowed by the
        # store) you notice before sending, so you never send the same digest
        # every tick until the DB recovers. The enqueue below hands the message to
        # the PERSISTENT notification queue (durable, retries on its own), so
        # committing the stamp first does not risk losing the report.
        if not store.set_meta(key, str(int(now))):
            continue  # stamp failed -> retry next tick, nothing sent (no dupe)
        try:
            notification_id = enqueue(
                _compose_message(content), "info", "report",
            )
        except Exception:
            # The queue rejected the handoff: restore the previous cadence so
            # the caller's isolation guard can retry on the next tick.
            if raw is not None:
                store.set_meta(key, str(raw))
            raise
        if notification_id is None:
            # The worker could not persist the row. Put the cadence back so
            # the next scheduler tick retries instead of losing this digest.
            if raw is not None:
                store.set_meta(key, str(raw))
            continue
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

    ``units`` is ``[(ups_name, ups_label, store, energy_config), ...]``;
    ``meta_store`` is
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
        # F-081: gather/render first so a transient source failure does not burn
        # the report period before there is a message ready to enqueue.
        per_ups = [
            gather_report_sources(store, ups_name, energy_cfg,
                                  period=period, now=now,
                                  ups_label=ups_label)
            for ups_name, ups_label, store, energy_cfg in units
        ]
        content = build_aggregate_report(period, per_ups,
                                         include=reports.include,
                                         fmt=reports.format)
        # F-028: stamp the dedup key first and only send if it committed (see
        # maybe_send_due_reports). A silently-failed meta write must not let the
        # daemon re-send the same digest every tick; the enqueue persists to the
        # durable notification queue, so stamping first can't lose the report.
        if not meta_store.set_meta(key, str(int(now))):
            continue  # stamp failed -> retry next tick, nothing sent (no dupe)
        try:
            notification_id = enqueue(
                _compose_message(content), "info", "report",
            )
        except Exception:
            if raw is not None:
                meta_store.set_meta(key, str(raw))
            raise
        if notification_id is None:
            if raw is not None:
                meta_store.set_meta(key, str(raw))
            continue
        sent.append(period)
    return sent
