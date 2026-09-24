"""Curses-based TUI dashboard for Eneru (eneru monitor).

Reads UPS data from daemon state files -- no direct NUT polling.
Two-panel layout:
  - Top panel (gray background, white text): UPS config/status
  - Bottom panel (yellow/gold background, black text): event logs + key hints
"""

import curses
import os
import re
import sys
import time
import unicodedata
from collections import deque, namedtuple
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Dict, List, Tuple

from eneru.version import __version__
from eneru.config import Config, UPSGroupConfig
from eneru.graph import BrailleGraph
from eneru.outlook import (
    read_self_test_failure_armed,
    redundancy_outlook_from_state_files,
    stale_after_seconds,
    state_file_outlook,
)
from eneru.remote_health import read_remote_health_sidecar, remote_health_sidecar_path
from eneru.stats import StatsStore
from eneru.status import sanitize_name, select_event_rows
from eneru.utils import (
    SEVERITY_CRIT,
    SEVERITY_OK,
    SEVERITY_WARN,
    format_age,
    format_seconds,
    humanize_event_type,
    is_numeric,
    status_summary,
)


# Cycle order for the G key + --graph flag.
GRAPH_MODES = ("off", "charge", "load", "voltage", "runtime")

# ----------------------------------------------------------------------
# IMPORTANT: TIME_RANGES / TIME_RANGE_SECONDS / the --time CLI flag /
# the <T> keystroke are GRAPH-ONLY in 5.2.2+. They scope the graph's
# X-axis window. They must NOT be used to filter the events panel.
#
# Events are sparse (one row per power transition or daemon lifecycle
# event, not per-poll), so a fixed time window made the panel silently
# empty for normal homelab usage. The events panel queries the full
# events table and trims by row count via --length / EVENTS_MAX_ROWS_*.
# Don't re-couple the two -- the original 5.2.0 coupling is exactly
# the bug that made the events panel silently fall back to log
# parsing. See query_events_for_display + run_once for the new wiring.
# ----------------------------------------------------------------------
TIME_RANGES = ("1h", "6h", "24h", "7d", "30d")
TIME_RANGE_SECONDS = {
    "1h": 3600,
    "6h": 6 * 3600,
    "24h": 24 * 3600,
    "7d": 7 * 86400,
    "30d": 30 * 86400,
}

# The events panel is intentionally decoupled from the graph timescale:
# pressing T to change the graph window must not shrink/grow the events
# list. The panel pulls every event from the SQLite store (the events
# table is one row per power event, not per poll, so even years of
# history stay tiny) and lets the operator scroll through with the
# arrow keys.
EVENTS_MAX_ROWS_NORMAL = 30  # default cap; matches CLI --length default
EVENTS_MAX_ROWS_MORE = 500   # <M> "More logs" toggle cap
# F-045: the events pane read the ENTIRE events table per UPS every 5s
# (query_events(0, now)) — 10^5+ rows under long retention. The pane shows at
# most EVENTS_MAX_ROWS_MORE lines, so bound the DB read to the newest N rows via
# the indexed query_recent_events (ORDER BY ts DESC, id DESC LIMIT). N sits well
# above the largest display cap so the three-tier trim still has headroom; the
# only trade-off is that a power event older than the newest N rows can scroll
# out of reach (the old full scan never evicted it) — acceptable for a live pane.
EVENTS_QUERY_LIMIT = 2000

# Three display tiers. The events table accumulates power transitions
# (operator's reason for opening the panel), diagnostics that help explain
# electrical / UPS behavior, and daemon lifecycle rows that are useful
# context but high-volume during testing or rapid restarts.
#
# POWER_EVENTS keep at least half of the row cap (all of it at the default
# verbosity) -- never crowded out by daemon noise -- while each enabled
# lower tier still gets seats (6.2, M5). DIAGNOSTIC_EVENTS are implicit: anything
# outside POWER_EVENTS and LIFECYCLE_EVENTS. They surface at ``-v`` /
# first ``<V>``. LIFECYCLE_EVENTS surface last at ``-vv`` / second
# ``<V>``.
#
# Without this tiering, a homelab user who hits a real outage 4 days ago
# and then iterates on the daemon today sees daemon rows instead of the
# actual ON_BATTERY rows. (Bug surfaced in 5.2.2 by the maintainer's own
# data: 65 daemon-lifecycle rows vs 5 power-event rows in the events table.)
POWER_EVENTS = frozenset({
    "ON_BATTERY",
    "POWER_RESTORED",
    "EMERGENCY_SHUTDOWN_INITIATED",
    "SHUTDOWN_SEQUENCE_COMPLETE",
    "VOLTAGE_LOW",
    "VOLTAGE_HIGH",
    "BROWNOUT_DETECTED",
    "OVER_VOLTAGE_DETECTED",
    "BYPASS_MODE_ACTIVE",
    "OVERLOAD_ACTIVE",
    "OVERLOAD_DETECTED",
    "BATTERY_LOW",
    "FSD_DETECTED",
    "CONNECTION_LOST",
    "CONNECTION_RESTORED",
    # v6.1 battery-health alerts — critical/actionable, must show by default
    # (they were landing in Diagnostics, hidden at the default verbosity).
    "BATTERY_HEALTH_CRITICAL",
    "BATTERY_HEALTH_WARNING",
    "BATTERY_REPLACEMENT_PREDICTED",
})

LIFECYCLE_EVENTS = frozenset({
    "DAEMON_START",
    "DAEMON_STOP",
    "DAEMON_RESTARTED",
    "DAEMON_UPGRADED",
    "DAEMON_RECOVERED",
})

# Union for callers / tests that grep for "is this row in the priority set"
# without caring about the tier split.
PRIORITY_EVENTS = POWER_EVENTS | LIFECYCLE_EVENTS

EVENTS_VERBOSITY_POWER = 0
EVENTS_VERBOSITY_DIAGNOSTICS = 1
EVENTS_VERBOSITY_ALL = 2

GHOSTTY_TERMS = frozenset({"ghostty", "xterm-ghostty"})
GHOSTTY_FALLBACK_TERM = "xterm-256color"

EVENT_SECTION_POWER = "Power Events"
EVENT_SECTION_DIAGNOSTICS = "Diagnostics"
EVENT_SECTION_LIFECYCLE = "Lifecycle"


def _events_verbosity(value) -> int:
    """Normalize count-style verbosity to the supported 0..2 display tiers."""
    if isinstance(value, bool):
        return (EVENTS_VERBOSITY_DIAGNOSTICS if value
                else EVENTS_VERBOSITY_POWER)
    try:
        return max(EVENTS_VERBOSITY_POWER, min(int(value), EVENTS_VERBOSITY_ALL))
    except (TypeError, ValueError):
        return EVENTS_VERBOSITY_POWER


def _missing_ghostty_terminfo(term: str, exc: curses.error) -> bool:
    """Return True when curses failed only because Ghostty terminfo is absent."""
    return (
        term in GHOSTTY_TERMS
        and "setupterm" in str(exc)
        and "could not find terminal" in str(exc)
    )


def _event_tier(event_type: str) -> str:
    """Return the user-facing display tier for an event type."""
    if event_type in POWER_EVENTS:
        return EVENT_SECTION_POWER
    if event_type in LIFECYCLE_EVENTS:
        return EVENT_SECTION_LIFECYCLE
    return EVENT_SECTION_DIAGNOSTICS


def _event_enabled(event_type: str, verbosity: int) -> bool:
    """Return whether an event type is visible at the current verbosity."""
    tier = _event_tier(event_type)
    if tier == EVENT_SECTION_POWER:
        return True
    if tier == EVENT_SECTION_DIAGNOSTICS:
        return verbosity >= EVENTS_VERBOSITY_DIAGNOSTICS
    return verbosity >= EVENTS_VERBOSITY_ALL


def _events_verbosity_label(verbosity: int) -> str:
    """Short label for the live TUI footer."""
    verbosity = _events_verbosity(verbosity)
    if verbosity == EVENTS_VERBOSITY_POWER:
        return "power"
    if verbosity == EVENTS_VERBOSITY_DIAGNOSTICS:
        return "+diag"
    return "all"


def _no_events_message(verbosity: int) -> str:
    """Placeholder text when the enabled tiers have no rows."""
    if _events_verbosity(verbosity) == EVENTS_VERBOSITY_POWER:
        return "(no power events recorded)"
    return "(no events)"

# Map graph modes to (column, unit_suffix, y_min, y_max, value_formatter).
# value_formatter takes a float and returns the user-facing display
# string for axis labels and the now/min/max header. None bounds
# auto-scale from the observed data.
def _fmt_int(v: float) -> str:
    return f"{int(round(v))}"

def _fmt_volts(v: float) -> str:
    return f"{v:.1f}"

def _fmt_runtime_seconds(v: float) -> str:
    # Reuses the same logic as format_runtime() but accepts a float
    # directly (format_runtime expects a string from the state file).
    try:
        rt = int(round(float(v)))
    except (TypeError, ValueError):
        return "?"
    if rt >= 3600:
        return f"{rt // 3600}h {(rt % 3600) // 60}m"
    if rt >= 60:
        return f"{rt // 60}m {rt % 60}s"
    return f"{rt}s"

METRIC_INFO = {
    "charge":  ("battery_charge",  "%",  0.0,   100.0,  _fmt_int),
    "load":    ("ups_load",        "%",  0.0,   100.0,  _fmt_int),
    "voltage": ("input_voltage",   "V",  None,  None,   _fmt_volts),
    "runtime": ("battery_runtime", "",   0.0,   None,   _fmt_runtime_seconds),
}


def stats_db_path_for(group: UPSGroupConfig, config: Config) -> Path:
    """Return the per-UPS stats DB path the daemon would write to.

    Mirrors the sanitisation in MultiUPSCoordinator and UPSGroupMonitor.
    """
    # ISS-039: reuse status.sanitize_name rather than re-implementing the chain.
    sanitized = sanitize_name(group.ups.name) if config.multi_ups else "default"
    return Path(config.statistics.db_directory) / f"{sanitized}.db"


def state_file_path_for(group: UPSGroupConfig, config: Config) -> Path:
    """Return the per-UPS state file path the daemon writes every poll.

    Multi-UPS mode appends a sanitized suffix; single-UPS uses the bare
    path. Used by both ``collect_group_data`` and ``update_live_buffer``.
    """
    # ISS-039: reuse status.sanitize_name rather than re-implementing the chain.
    if config.multi_ups:
        return Path(config.logging.state_file + f".{sanitize_name(group.ups.name)}")
    return Path(config.logging.state_file)


# ---- Live-sample blending (spec 2.13) ----
#
# The daemon's SQLite writer flushes every 10 s, but the state file is
# rewritten every poll cycle (~1 s). Without blending, the TUI graph's
# rightmost edge lags by up to 10 s while the live status panel stays
# current. ``update_live_buffer`` is called once per TUI refresh per
# group; it parses the same state file ``collect_group_data`` does and
# pushes the snapshot into a per-UPS deque. ``query_metric_series``
# then extends the SQLite result with any deque points newer than the
# last SQLite sample, deduped by timestamp.
_LIVE_BUFFER_MAXLEN = 60
_live_buffers: Dict[str, "deque[Tuple[int, Dict[str, float]]]"] = {}

# State-file keys (left) we promote into deque samples, mapped to the
# stats schema's column names (right). The daemon writes the state
# file as uppercase ``KEY=value`` lines (see UPSGroupMonitor._save_state),
# NOT in NUT's dotted lowercase form -- the keys here must match the
# state-file format or live blending receives zero samples and the
# rightmost edge of the graph lags behind the SQLite write cadence
# by ~10 s. Metrics whose values aren't persisted to the state file
# (battery voltage, temperature, frequencies) cannot be live-blended
# and are intentionally absent.
_STATE_FILE_TO_COLUMN: Dict[str, str] = {
    "BATTERY": "battery_charge",
    "RUNTIME": "battery_runtime",
    "LOAD": "ups_load",
    "INPUT_VOLTAGE": "input_voltage",
    "OUTPUT_VOLTAGE": "output_voltage",
}


def _buffer_key(group: UPSGroupConfig, config: Config) -> str:
    """Stable per-UPS key (matches the stats DB filename stem)."""
    return stats_db_path_for(group, config).stem


def _live_buffer_for(group: UPSGroupConfig, config: Config) -> deque:
    key = _buffer_key(group, config)
    buf = _live_buffers.get(key)
    if buf is None:
        buf = deque(maxlen=_LIVE_BUFFER_MAXLEN)
        _live_buffers[key] = buf
    return buf


def _coerce_float(value) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def update_live_buffer(group: UPSGroupConfig, config: Config) -> None:
    """Snapshot the state file into the per-UPS live deque.

    Idempotent within the same wall-clock second: if called twice in
    one second the latter call replaces the former (no duplicate
    timestamps make it into the deque).
    """
    data = parse_state_file(state_file_path_for(group, config))
    if not data:
        return
    sample: Dict[str, float] = {}
    for state_key, column in _STATE_FILE_TO_COLUMN.items():
        v = _coerce_float(data.get(state_key))
        if v is not None:
            sample[column] = v
    if not sample:
        return
    ts = int(time.time())
    buf = _live_buffer_for(group, config)
    if buf and buf[-1][0] == ts:
        buf[-1] = (ts, sample)
    else:
        buf.append((ts, sample))


def clear_live_buffers() -> None:
    """Drop all live buffers. Test helper -- the runtime never calls this."""
    _live_buffers.clear()


def query_metric_series(
    config: Config,
    group: UPSGroupConfig,
    metric: str,
    seconds: int,
) -> List[Tuple[int, float]]:
    """Return ``[(ts, value), ...]`` for a metric across a time window.

    Source order:
    1. The per-UPS SQLite stats DB (historical, possibly up to 10 s stale).
    2. The per-UPS live deque (real-time tail since the last SQLite flush).

    Returns an empty list if neither source has data for the metric --
    callers should render a "(no data)" placeholder.
    """
    info = METRIC_INFO.get(metric)
    if info is None:
        return []
    column = info[0]
    end = int(time.time())
    start = end - max(60, int(seconds))

    sqlite_series: List[Tuple[int, float]] = []
    db_path = stats_db_path_for(group, config)
    conn = StatsStore.open_readonly(db_path)
    if conn is not None:
        try:
            # ISS-039: wrap the caller-owned connection instead of poking
            # StatsStore._conn directly.
            store = StatsStore.from_connection(conn)
            sqlite_series = store.query_range(column, start, end)
        finally:
            try:
                conn.close()
            except Exception:
                pass

    # Blend with the live deque: anything newer than the last SQLite
    # sample (or any point in-window if SQLite is empty), deduped by ts.
    buf = _live_buffers.get(_buffer_key(group, config))
    if not buf:
        return sqlite_series
    sqlite_tail_ts = sqlite_series[-1][0] if sqlite_series else (start - 1)
    extra: List[Tuple[int, float]] = []
    seen_ts = {ts for ts, _ in sqlite_series}
    for ts, sample in buf:
        if ts <= sqlite_tail_ts or ts in seen_ts or ts < start or ts > end:
            continue
        v = sample.get(column)
        if v is None:
            continue
        extra.append((ts, float(v)))
        seen_ts.add(ts)
    if not extra:
        return sqlite_series
    extra.sort(key=lambda t: t[0])
    return sqlite_series + extra


# ==============================================================================
# STATE FILE PARSING
# ==============================================================================

def parse_state_file(path: Path) -> Optional[Dict[str, str]]:
    """Parse a daemon state file into a dict. Returns None if unreadable."""
    try:
        if not path.exists():
            return None
        text = path.read_text().strip()
        if not text:
            return None
        data = {}
        for line in text.splitlines():
            if "=" in line:
                key, _, value = line.partition("=")
                data[key.strip()] = value.strip()
        return data if data else None
    except Exception:
        return None


def parse_log_events(log_path: str,
                     max_events: Optional[int] = 8) -> List[str]:
    """Read recent power events from the log file tail (fallback path).

    ``max_events=None`` returns every matching event in the file, no
    cap. Used when the CLI passes ``--full-history`` and the SQLite
    DB isn't present so we fall back to the log tail.
    """
    INCLUDE = (
        "POWER EVENT", "Status changed", "SHUTDOWN", "shutdown",
        "CRITICAL", "FSD", "flap", "Flap", "On battery",
        "Power restored", "WARNING:",
    )
    EXCLUDE = (
        "Enabled features", "Checking initial connection",
        "Initial connection successful", "starting - monitoring",
        "Started", "Service stopped",
    )
    try:
        p = Path(log_path)
        if not p.exists():
            return []
        text = p.read_text()
        lines = text.strip().splitlines()
        events = []
        for line in reversed(lines):
            if any(ex in line for ex in EXCLUDE):
                continue
            if any(inc in line for inc in INCLUDE):
                events.append(line)
                if max_events is not None and len(events) >= max_events:
                    break
        events.reverse()
        return events
    except Exception:
        return []


def events_db_available(config: Config) -> bool:
    """Return True when at least one per-UPS events DB can be opened."""
    for group in config.ups_groups:
        conn = StatsStore.open_readonly(stats_db_path_for(group, config))
        if conn is None:
            continue
        try:
            return True
        finally:
            try:
                conn.close()
            except Exception:
                pass
    return False


def _sanitize_event_detail(detail: str) -> str:
    """Strip markdown formatting and collapse newlines so the detail
    fits on a single row of the events panel.

    The body strings written to ``events.detail`` come from the same
    text that goes to Apprise (Discord / Slack render ``**bold**`` and
    multi-line bodies natively). The TUI's curses surface can't:

    - Markdown bold ``**...**`` shows as literal asterisks in the
      panel — distracting and ugly.
    - An embedded ``\\n`` mid-string causes ``curses.addstr`` to advance
      to a new row before the gold background fill completes, leaving
      cells unpainted and exposing the underlying terminal default
      colors. Looks like artifacts / "broken colors" on the row.

    Sanitization is per-display only — the events table keeps the
    original body so notifications still render correctly on the
    Apprise side.
    """
    if not detail:
        return ""
    cleaned = detail.replace("**", "")
    # Collapse newlines (and surrounding indentation) into a single
    # visual separator so the line stays intact.
    parts = [p.strip() for p in cleaned.split("\n")]
    parts = [p for p in parts if p]
    return " · ".join(parts)


_SECONDS_RE = re.compile(r"\b(\d+) seconds\b")


def _is_decoration(ch: str) -> bool:
    """Emoji / pictograph / variation selector used as notification markup."""
    cp = ord(ch)
    if cp in (0xFE0F, 0xFE0E, 0x200D):
        return True
    return cp >= 0x2190 and unicodedata.category(ch) == "So"


def clean_event_detail(detail: str, ups_name: Optional[str] = None) -> str:
    """Readable one-line event detail for the TUI / ``--once`` (M6, M3).

    On top of :func:`_sanitize_event_detail` (no ``**`` / newlines):
    drops notification emoji ("📦  Eneru Upgraded"), drops the UPS name
    the ``[label]`` column already shows, and turns legacy
    "Runtime: 1490 seconds" rows into "24m 50s".
    """
    text = _sanitize_event_detail(detail)
    if not text:
        return ""
    text = "".join(ch for ch in text if not _is_decoration(ch))
    if ups_name:
        text = text.replace(f" {ups_name}", "")
    text = _SECONDS_RE.sub(lambda m: format_seconds(int(m.group(1))), text)
    return " ".join(text.split())


def _format_event_line(ts: int, label: str, event_type: str,
                       detail: str, multi_ups: bool, *,
                       ups_name: Optional[str] = None,
                       now: Optional[float] = None,
                       compact: bool = False,
                       raw: bool = False) -> str:
    """Format one event row: ``time  age  [UPS] Label: detail``.

    ``compact`` shortens the time to ``MM-DD HH:MM`` for narrow terminals.
    The age column ("14d ago") says at a glance how old "recent" is (M5).
    ``raw`` keeps the pre-6.2 script-friendly shape for ``--events-only``:
    ``YYYY-MM-DD HH:MM:SS  [UPS] EVENT_TYPE: detail`` (stable, no age).
    """
    if raw:
        try:
            stamp = datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError, OSError, OverflowError):
            stamp = "????-??-?? ??:??:??"
        prefix = f"[{label}] " if multi_ups else ""
        cleaned = _sanitize_event_detail(detail)
        if cleaned:
            return f"{stamp}  {prefix}{event_type}: {cleaned}"
        return f"{stamp}  {prefix}{event_type}"
    now = time.time() if now is None else now
    try:
        when = datetime.fromtimestamp(int(ts))
        time_str = when.strftime("%m-%d %H:%M" if compact else "%Y-%m-%d %H:%M:%S")
        age = format_age(now - int(ts))
    except (TypeError, ValueError, OSError, OverflowError):
        time_str = "??-?? ??:??" if compact else "????-??-?? ??:??:??"
        age = "?"
    prefix = f"[{label}] " if multi_ups else ""
    name = humanize_event_type(event_type)
    cleaned_detail = clean_event_detail(detail, ups_name)
    head = f"{time_str}  {age:>8}  {prefix}{name}"
    if cleaned_detail:
        return f"{head}: {cleaned_detail}"
    return head


def query_events_for_display(
    config: Config,
    *,
    max_events: Optional[int] = EVENTS_MAX_ROWS_NORMAL,
    verbosity: int = EVENTS_VERBOSITY_POWER,
    grouped: bool = False,
    compact: bool = False,
    raw: bool = False,
) -> List[str]:
    """Pull events from each UPS's SQLite store, sorted by timestamp.

    Always queries the full events table -- there is no time-range
    parameter. Events are sparse compared to graph samples, and a
    fixed time window made the panel silently empty for normal
    homelab usage (the daemon emits maybe one or two power events per
    week). Callers that want to *narrow* the result use ``max_events``;
    callers that want *all* of them pass ``max_events=None``.

    Returns formatted display strings ready to drop into the TUI events
    panel. Returns an empty list when no per-UPS DB exists -- callers
    should fall back to ``parse_log_events`` in that case.

    ``verbosity`` selects enabled tiers: ``0`` shows Power Events only,
    ``1`` adds Diagnostics, and ``2`` adds Lifecycle.

    ``max_events=None`` (or 0) disables the row cap entirely.

    Trim (M5, ``status.select_event_rows``): power events get half the cap
    (all of it when they are the only enabled tier), every other enabled
    tier present gets an equal slice of the rest, and spare seats go to the
    newest rows. Before 6.2 power events took every seat, so on an install
    with 30+ old outages ``-v`` / ``-vv`` looked like they did nothing.
    ``compact`` shortens timestamps for narrow terminals; ``raw`` keeps the
    script-friendly ``EVENT_TYPE: detail`` lines (``--once --events-only``).
    """
    verbosity = _events_verbosity(verbosity)
    multi_ups = config.multi_ups
    rows: List[tuple] = []  # (ts, label, event_type, detail, ups_name)
    any_db_seen = False

    for group in config.ups_groups:
        db_path = stats_db_path_for(group, config)
        conn = StatsStore.open_readonly(db_path)
        if conn is None:
            continue
        any_db_seen = True
        try:
            # ISS-039: wrap the caller-owned connection rather than poking
            # StatsStore._conn directly.
            store = StatsStore.from_connection(conn)
            # F-045: bound the read to the newest EVENTS_QUERY_LIMIT rows instead
            # of scanning the whole events table. query_recent_events returns the
            # same ascending (ts, event_type, detail) 3-tuples query_events did,
            # so the tier-trim loop below is unchanged.
            # cubic P1 (round 1): the bound only applies when a display cap
            # exists. ``max_events`` in (None, 0) is documented as "no cap"
            # (CLI --length 0), so that path keeps the full ascending scan —
            # otherwise "give me everything" silently topped out at 2000 rows
            # per UPS. A cap above the default bound widens the read so the
            # tier-trim below still has headroom.
            if max_events:
                events = store.query_recent_events(
                    end_ts=int(time.time()),
                    limit=max(EVENTS_QUERY_LIMIT, int(max_events)))
            else:
                events = store.query_events(0, int(time.time()))
            for ts, etype, detail in events:
                if not _event_enabled(etype, verbosity):
                    continue
                rows.append((int(ts), group.ups.label, etype, detail or "",
                             group.ups.name))
        finally:
            try:
                conn.close()
            except Exception:
                pass

    if not any_db_seen:
        return []  # signal "no DB" so callers can fall back

    rows.sort(key=lambda r: r[0])
    # ``max_events`` in (None, 0) means no cap (select_event_rows agrees).
    tier_names = {EVENT_SECTION_POWER: "power",
                  EVENT_SECTION_DIAGNOSTICS: "diagnostics",
                  EVENT_SECTION_LIFECYCLE: "lifecycle"}
    cap = max_events
    if grouped and max_events and max_events > 1:
        # Each section header takes a row of the panel too; reserve them
        # up front so the grouped trim below never cuts a whole tier.
        tiers = {_event_tier(r[2]) for r in rows}
        cap = max(1, max_events - len(tiers))
    rows = select_event_rows(
        rows, max_events=cap,
        tier_of=lambda r: tier_names[_event_tier(r[2])])
    now = time.time()

    def fmt(row) -> str:
        ts, label, etype, detail, ups_name = row
        return _format_event_line(ts, label, etype, detail, multi_ups,
                                  ups_name=ups_name, now=now, compact=compact,
                                  raw=raw)

    if not grouped:
        return [fmt(row) for row in rows]

    # Grouped mode renders one header per non-empty tier, so a section needs
    # at least 2 lines (1 header + 1 row) to display without an orphan header.
    # At max_events == 1 there is no room for both, so fall back to the single
    # most-recent row -- the tier trim above gives power the first seat, so
    # a power event still wins at length=1.
    if max_events == 1:
        return [fmt(row) for row in rows]

    grouped_lines: List[str] = []
    sections = (
        (EVENT_SECTION_POWER, lambda r: r[2] in POWER_EVENTS),
        (EVENT_SECTION_DIAGNOSTICS,
         lambda r: r[2] not in POWER_EVENTS and r[2] not in LIFECYCLE_EVENTS),
        (EVENT_SECTION_LIFECYCLE, lambda r: r[2] in LIFECYCLE_EVENTS),
    )
    for section, predicate in sections:
        section_rows = [r for r in rows if predicate(r)]
        if not section_rows:
            continue
        if max_events:
            remaining_lines = max_events - len(grouped_lines)
            # Headers consume live-panel rows too. Need >= 2 (1 header +
            # 1 row) to render this section without an orphan header.
            # The max_events == 1 degenerate case is handled above as a
            # fallback to the trimmed single row.
            if remaining_lines < 2:
                break
            section_rows = section_rows[-(remaining_lines - 1):]
        grouped_lines.append(section)
        grouped_lines.extend(fmt(row) for row in section_rows)
    return grouped_lines


# ==============================================================================
# COLOR SCHEME
# ==============================================================================

# Color pair IDs
C_BORDER = 1         # white pipes on black
C_HEADER = 2         # white on black (title bar inside border)
C_GRAY_BG = 3        # white text on gray background (config panel)
C_GRAY_DIM = 4       # dim text on gray background
C_GOLD_BG = 5        # black text on yellow/gold background (logs panel)
C_GOLD_KEY = 6       # bold black on yellow/gold (<Q>, <R>, <M>)
C_GOLD_DIM = 7       # dim/gray text on yellow/gold (key descriptions)
C_STATUS_OK = 8      # black on green: severity "ok"
C_STATUS_OB = 9      # white on red (kept for config_tui's error badge)
C_STATUS_CRIT = 10   # white on red: severity "crit"
C_STATUS_UNK = 11    # white on magenta (legacy; no longer used by monitor)
C_STATUS_WARN = 12   # black on amber: severity "warn"

# M1: one 3-level scale on every surface (ok = green, warn = amber,
# crit = red). The badge TEXT always carries the meaning too, so a
# monochrome terminal loses nothing but the colour.
_SEVERITY_PAIRS = {
    SEVERITY_OK: C_STATUS_OK,
    SEVERITY_WARN: C_STATUS_WARN,
    SEVERITY_CRIT: C_STATUS_CRIT,
}

def init_colors():
    """Initialize color scheme.

    Uses the standard xterm-256color palette for consistent rendering
    across terminals and SSH sessions. Falls back to basic 8 colors
    when 256 colors are not available.
    """
    curses.start_color()

    if curses.COLORS >= 256:
        gray_bg = 243        # #767676
        gold_bg = 178        # #D7AF00
        dim_on_gold = 241    # #626262 -- dim gray for key hint descriptions
        black_fg = 16        # true black
        amber_bg = 214       # #FFAF00 -- warn badge, distinct from the gold panel
    else:
        gray_bg = curses.COLOR_BLACK
        gold_bg = curses.COLOR_YELLOW
        dim_on_gold = curses.COLOR_WHITE
        black_fg = curses.COLOR_BLACK
        amber_bg = curses.COLOR_YELLOW

    curses.init_pair(C_BORDER, curses.COLOR_WHITE, curses.COLOR_BLACK)
    curses.init_pair(C_HEADER, curses.COLOR_WHITE, curses.COLOR_BLACK)
    curses.init_pair(C_GRAY_BG, curses.COLOR_WHITE, gray_bg)
    curses.init_pair(C_GRAY_DIM, curses.COLOR_WHITE, gray_bg)
    curses.init_pair(C_GOLD_BG, black_fg, gold_bg)
    curses.init_pair(C_GOLD_KEY, black_fg, gold_bg)
    curses.init_pair(C_GOLD_DIM, dim_on_gold, gold_bg)
    # Status badges: colored background with contrasting text
    curses.init_pair(C_STATUS_OK, black_fg, curses.COLOR_GREEN)
    curses.init_pair(C_STATUS_OB, curses.COLOR_WHITE, curses.COLOR_RED)
    curses.init_pair(C_STATUS_CRIT, curses.COLOR_WHITE, curses.COLOR_RED)
    curses.init_pair(C_STATUS_UNK, curses.COLOR_WHITE, curses.COLOR_MAGENTA)
    curses.init_pair(C_STATUS_WARN, black_fg, amber_bg)


def human_status(status: str, **context) -> str:
    """Short shared label for a NUT status (``utils.status_summary``).

    ``context`` passes through ``trigger_active`` / ``shutting_down`` /
    ``connection_state`` / ``stale`` so the TUI says exactly what the web
    dashboard and notifications say ("On mains", "On battery", ...).
    """
    return status_summary(status, **context)["label"]


def severity_color(severity: str) -> int:
    """Colour pair for an ``ok`` / ``warn`` / ``crit`` severity."""
    return _SEVERITY_PAIRS.get(severity, C_STATUS_WARN)


def status_color(status: str, **context) -> int:
    """Return the severity colour pair for a UPS status string."""
    return severity_color(status_summary(status, **context)["severity"])


def summary_attr(summary: Dict[str, Any]) -> int:
    """Badge attribute for a ``status_summary`` dict.

    Blink ONLY when the summary says so (shutdown triggered / shutting
    down). "On battery, 90% left" must not flash like "about to die".
    """
    attr = (curses.color_pair(severity_color(summary.get("severity")))
            | curses.A_BOLD)
    if summary.get("blink"):
        attr |= curses.A_BLINK
    return attr


def status_attr(status: str, **context) -> int:
    """Return curses attribute for a status badge."""
    return summary_attr(status_summary(status, **context))


# ==============================================================================
# DATA COLLECTION
# ==============================================================================

def state_epoch(state: Optional[Dict[str, str]],
                path: Optional[Path] = None) -> Tuple[Optional[float], str]:
    """When the daemon last wrote this state file, as ``(epoch, source)``.

    H5: ``TIMESTAMP`` is a naive daemon-local string (a container on UTC
    and a host on CEST disagree by two hours), so it is never shown. A 6.2
    daemon writes ``EPOCH``; an older daemon doesn't, and then the file's
    mtime is the same fact (the file is only rewritten on a good poll).
    Returns ``(None, "none")`` when neither is available.
    """
    if state and is_numeric(state.get("EPOCH")) and float(state["EPOCH"]) > 0:
        return float(state["EPOCH"]), "epoch"
    if state and path is not None:
        try:
            return Path(path).stat().st_mtime, "mtime"
        except OSError:
            pass
    return None, "none"


def missing_state_reason(path: Path) -> str:
    """Why no state could be read at ``path`` (M8): the TUI must not claim
    "daemon not running" when it simply can't see the daemon's files."""
    path = Path(path)
    if not path.parent.is_dir():
        return "no-dir"
    if path.exists():
        if not os.access(path, os.R_OK):
            return "unreadable"
        return "empty"
    return "missing"


def missing_state_lines(path: Any, reason: str) -> List[str]:
    """Plain-text explanation for a missing state file (M8)."""
    what = {
        "no-dir": f"State directory {Path(path).parent} does not exist here.",
        "unreadable": f"State file {path} is not readable by this user.",
        "empty": f"State file {path} is empty (daemon starting?).",
    }.get(reason, f"No state file at {path}.")
    return [
        what,
        "Daemon stopped, or running in a container? If so, set logging.state_file",
        "and statistics.db_directory to the host-side paths, or run the TUI in",
        "the container: docker exec <container> eneru monitor",
    ]


def _state_for_outlook(state: Optional[Dict[str, str]],
                       epoch: Optional[float]) -> Optional[Dict[str, str]]:
    """Copy of ``state`` with ``EPOCH`` filled from the mtime fallback."""
    if not state:
        return state
    out = dict(state)
    if epoch is not None and not is_numeric(out.get("EPOCH")):
        out["EPOCH"] = str(epoch)
    return out


def _self_test_armed(group: UPSGroupConfig, config: Config,
                     state: Dict[str, str]) -> bool:
    """T5 latch from the stats DB, read only while on battery (cheap)."""
    if "OB" not in str(state.get("STATUS", "")).split():
        return False
    conn = StatsStore.open_readonly(stats_db_path_for(group, config))
    if conn is None:
        return False
    try:
        return read_self_test_failure_armed(
            conn, state.get("ON_BATTERY_SINCE", ""),
            attributed=state.get("SELF_TEST_ATTRIBUTED") == "1")
    finally:
        try:
            conn.close()
        except Exception:
            pass


def group_outlook(group: UPSGroupConfig, config: Config,
                  state: Optional[Dict[str, str]], now: float) -> Optional[Dict]:
    """``outlook.state_file_outlook`` for the TUI; None if it can't be built.

    Display-only: a failure here must never take the dashboard down, so the
    renderer falls back to the bare status when this returns None.
    """
    try:
        armed = _self_test_armed(group, config, state) if state else False
        return state_file_outlook(config, group, state, now=now,
                                  self_test_failure_armed=armed)
    except Exception:
        return None


def collect_redundancy_data(config: Config, groups_data: List[Dict],
                            now: Optional[float] = None) -> List[Dict]:
    """Per redundancy group, the M9 outlook built from member state files.

    Reuses the states ``collect_group_data`` already parsed (with the mtime
    fallback for pre-6.2 daemons) so both panels judge the same snapshot.
    """
    now = time.time() if now is None else now
    states = {d["name"]: d.get("outlook_state") for d in groups_data}
    labels = {d["name"]: d["label"] for d in groups_data}
    out = []
    for rg in getattr(config, "redundancy_groups", None) or []:
        try:
            data = redundancy_outlook_from_state_files(
                config, rg, now=now, states=states)
        except Exception:
            data = {"name": rg.name, "error": True,
                    "minHealthy": rg.min_healthy}
        data["labels"] = {n: labels.get(n, n) for n in rg.ups_sources}
        data["total"] = len(rg.ups_sources)
        out.append(data)
    return out


def collect_group_data(group: UPSGroupConfig, config: Config,
                       now: Optional[float] = None) -> Dict:
    """Collect display data for one UPS group."""
    label = group.ups.label
    name = group.ups.name
    now = time.time() if now is None else now

    state_path = state_file_path_for(group, config)
    state = parse_state_file(state_path)
    epoch, epoch_source = state_epoch(state, state_path)
    outlook_state = _state_for_outlook(state, epoch)

    res_parts = []
    if group.is_local:
        if group.virtual_machines.enabled:
            res_parts.append("VMs")
        if group.containers.enabled:
            compose_n = len(group.containers.compose_files)
            if compose_n:
                res_parts.append(f"{compose_n} compose")
            else:
                res_parts.append("containers")
    server_n = len([s for s in group.remote_servers if s.enabled])
    if server_n:
        res_parts.append(f"{server_n} remote server{'s' if server_n != 1 else ''}")
    remote_health = read_remote_health_sidecar(
        remote_health_sidecar_path(state_path)
    )

    return {
        "label": label, "name": name, "is_local": group.is_local,
        "state": state,
        "state_path": str(state_path),
        "missing_reason": None if state else missing_state_reason(state_path),
        "epoch": epoch,
        "epoch_source": epoch_source,
        "outlook_state": outlook_state,
        "outlook": group_outlook(group, config, outlook_state, now),
        "resources": ", ".join(res_parts) if res_parts else "none",
        "remote_health": remote_health,
        "remote_health_summary": summarize_remote_health(remote_health),
    }


def summarize_remote_health(rows: List[Dict]) -> str:
    """Return a compact remote-health summary for TUI display."""
    if not rows:
        return ""
    counts: Dict[str, int] = {}
    for row in rows:
        # Defend against malformed sidecar content — a partially
        # written / hand-edited / older-schema entry that's not a
        # mapping must not crash the TUI rendering.
        if not isinstance(row, dict):
            continue
        status = str(row.get("status", "UNKNOWN")).lower()
        counts[status] = counts.get(status, 0) + 1
    order = ("healthy", "degraded", "failed", "checking", "unknown", "disabled")
    parts = [f"{counts[k]} {k}" for k in order if counts.get(k)]
    return ", ".join(parts)


def format_runtime(runtime: str) -> str:
    """Format runtime seconds into human-readable string."""
    try:
        rt_sec = int(float(runtime))
        if rt_sec >= 3600:
            return f"{rt_sec // 3600}h {(rt_sec % 3600) // 60}m"
        elif rt_sec >= 60:
            return f"{rt_sec // 60}m {rt_sec % 60}s"
        else:
            return f"{rt_sec}s"
    except (ValueError, TypeError):
        return runtime


# ==============================================================================
# VIEW MODEL (pure: data dicts in, text lines out)
# ==============================================================================
#
# ELI5: the kitchen prepares every plate (the lines below) before the waiter
# (curses) carries them out. Building text first means the interactive TUI and
# ``--once`` say exactly the same words, the panel can be sized to what it
# actually holds (no blank rows, L8), and tests can read the plates without a
# terminal.

# One display row of the status panel. ``style`` is a name ("bold", "plain",
# "warn", "crit"), mapped to a curses attribute only at paint time.
# ``priority``: 0 = must show, 1 = useful, 2 = dropped first on short
# terminals. ``badge``: optional right-aligned ``(text, severity, blink)``.
Line = namedtuple("Line", "text style priority badge")


def _line(text: str, style: str = "plain", priority: int = 1,
          badge: Optional[Tuple[str, str, bool]] = None) -> Line:
    return Line(text, style, priority, badge)


# Shutdown phases in executor order (shutdown.plan.PHASE_ORDER) -> short name.
PHASE_SHORT = {
    "vms": "VMs",
    "containers": "Containers",
    "filesystem-sync": "Sync",
    "filesystem-unmount": "Unmount",
    "remote": "Remotes",
    "final-sync": "Final sync",
    "local-poweroff": "Poweroff",
}
_TERMINAL_PHASE_STATES = frozenset({"succeeded", "failed", "timed-out", "skipped"})

# Short trigger names for the "Shutdown when:" chips (the full rule text is
# in the web Shutdown tab and ``eneru config check``).
_TRIGGER_SHORT = {
    "lowBattery": "charge",
    "criticalRuntime": "runtime",
    "depletionRate": "drain",
    "extendedTime": "on battery",
    "selfTestFailure": "failed self-test",
}

# Redundancy outlook state -> badge words (the colour repeats the severity).
_GROUP_BADGES = {
    "healthy": "OK",
    "at-risk": "AT RISK",
    "quorum-lost": "QUORUM LOST",
    "deferred": "DEFERRED",
    "shutting-down": "SHUTTING DOWN",
}


def clock_text(epoch: Optional[float], now: float) -> str:
    """Local wall-clock time of ``epoch``; adds the date when not today."""
    if epoch is None:
        return "?"
    try:
        when = datetime.fromtimestamp(float(epoch))
        today = datetime.fromtimestamp(float(now)).date()
    except (TypeError, ValueError, OSError, OverflowError):
        return "?"
    if when.date() == today:
        return when.strftime("%H:%M:%S")
    return when.strftime("%Y-%m-%d %H:%M:%S")


STALE_HINT = ("No new data: daemon stopped, NUT unreachable,"
              " or a different state file?")


def freshness_view(data: Dict, now: float) -> Dict[str, Any]:
    """Age + stale verdict for one UPS (H5), from EPOCH or the mtime.

    Returns ``{age, stale, stale_after, text}``; ``text`` is the
    "Updated 3s ago (23:46:50)" line, with "-- STALE (limit 30s)" appended
    once the daemon stopped rewriting the file.
    """
    epoch = data.get("epoch")
    fresh = (data.get("outlook") or {}).get("freshness") or {}
    stale_after = fresh.get("staleAfterSeconds") or stale_after_seconds(1)
    if epoch is None:
        return {"age": None, "stale": True, "stale_after": stale_after,
                "text": "Never updated"}
    age = max(0.0, now - float(epoch))
    stale = age > stale_after
    text = f"Updated {format_age(age)} ({clock_text(epoch, now)})"
    if stale:
        text += f" -- STALE (limit {format_seconds(stale_after)})"
    return {"age": age, "stale": stale, "stale_after": stale_after,
            "text": text}


def reading_parts(state: Dict[str, str]) -> List[str]:
    """``Battery: 100% (24m 52s)``, ``Load: 18%``, ... skipping empty
    readings (M11: no bare ``Output: V`` when the UPS doesn't report it)."""
    def ok(key: str) -> bool:
        return is_numeric(state.get(key))

    parts = []
    if ok("BATTERY"):
        text = f"Battery: {state['BATTERY']}%"
        if ok("RUNTIME"):
            text += f" ({format_runtime(state['RUNTIME'])})"
        parts.append(text)
    elif ok("RUNTIME"):
        parts.append(f"Runtime: {format_runtime(state['RUNTIME'])}")
    if ok("LOAD"):
        parts.append(f"Load: {state['LOAD']}%")
    if ok("INPUT_VOLTAGE"):
        parts.append(f"Input: {state['INPUT_VOLTAGE']}V")
    if ok("OUTPUT_VOLTAGE"):
        parts.append(f"Output: {state['OUTPUT_VOLTAGE']}V")
    return parts


def short_duration(seconds: Any) -> str:
    """Compact duration for tight lines: "45s", "1m 21s", "21m", "1h 5m".

    Seconds are dropped (rounded down) from 10 minutes up -- nobody plans
    around the seconds of a 20-minute countdown, and 80 columns are precious.
    """
    if not is_numeric(seconds):
        return "?"
    sec = max(0, int(round(float(seconds))))
    if sec < 60:
        return f"{sec}s"
    if sec < 600:
        return f"{sec // 60}m" + (f" {sec % 60}s" if sec % 60 else "")
    if sec < 3600:
        return f"{sec // 60}m"  # floor: never promise more time than left
    return f"{sec // 3600}h {(sec % 3600) // 60}m"


def trigger_chip(trigger: Dict[str, Any]) -> Optional[str]:
    """One "Shutdown when:" chip, or None for triggers not worth a chip.

    Examples: ``runtime < 5m: FIRED at 4m 40s``, ``charge < 20%: 45% (~20m)``,
    ``drain > 15%/min: 1.2%/min``, ``on battery > 20m: in 13m``. ``(~20m)``
    is the estimated time until that trigger fires.
    """
    tid = trigger.get("id")
    state = trigger.get("state")
    if tid not in _TRIGGER_SHORT or state in ("idle", "disabled", None):
        return None
    name = _TRIGGER_SHORT[tid]
    threshold = trigger.get("threshold")
    unit = trigger.get("unit") or ""
    if unit == "s":
        thr_text = short_duration(threshold)
    else:
        thr_text = f"{threshold:g}{unit}" if is_numeric(threshold) else "?"
    op = "<" if trigger.get("comparison") == "below" else ">"
    if tid == "selfTestFailure":
        cond = f"{name} + {thr_text} on battery"
    else:
        cond = f"{name} {op} {thr_text}"
    value = trigger.get("value")
    if tid in ("extendedTime", "selfTestFailure") or not is_numeric(value):
        now_text = ""
    elif unit == "s":
        now_text = short_duration(value)
    else:
        now_text = f"{value:g}{unit}"
    eta = trigger.get("etaSeconds")
    if state == "fired":
        tail = "FIRED" + (f" at {now_text}" if now_text else "")
    elif state == "held":
        tail = "met, held " + short_duration(eta or 0)
    elif state == "unknown":
        tail = "no reading"
    elif eta is not None:
        tail = (f"{now_text} (~{short_duration(eta)})" if now_text
                else f"in {short_duration(eta)}")
    else:
        tail = now_text or "ok"
    return f"{cond}: {tail}"


def outlook_headline(trig: Dict[str, Any]) -> str:
    """Short "what happens next" sentence (the contract ``summary`` is too
    long for 80 columns once "On battery for 7m 5s" leads it)."""
    nxt = trig.get("next")
    if nxt and nxt.get("state") == "fired":
        text = f"Shutdown condition met: {nxt['label'].lower()}"
    elif nxt and nxt.get("state") == "held":
        text = (f"{nxt['label']} met, waiting "
                f"{short_duration(nxt.get('etaSeconds') or 0)} (stabilizing)")
    elif nxt and nxt.get("etaSeconds") is not None:
        text = (f"Next trigger: {nxt['label'].lower()} in "
                f"~{short_duration(nxt['etaSeconds'])}")
    else:
        text = "No trigger is close"
    if trig.get("stabilizing") and not trig.get("firing") and not (
            nxt and nxt.get("state") == "held"):
        text += f"; stabilizing {short_duration(trig.get('stabilizationRemaining'))}"
    return text


def wrap_items(prefix: str, items: List[str], width: int,
               indent: str, sep: str = " · ") -> List[str]:
    """Greedy word-wrap of ``items`` after ``prefix`` into ``width`` cells.

    Continuation lines start with ``indent``. An item wider than a line is
    kept whole (the painter truncates it); nothing is silently dropped.
    """
    lines: List[str] = []
    current = prefix
    empty = True
    for item in items:
        candidate = current + ("" if empty else sep) + item
        if empty or display_width(candidate) <= width:
            current = candidate
            empty = False
            continue
        lines.append(current)
        current = indent + item
    if not empty:
        lines.append(current)
    return lines


PROGRESS_SILENT_AFTER = 300  # s without a progress write before we nag


def progress_silent(progress: Optional[Dict[str, Any]], now: float,
                    stale: bool) -> bool:
    """A "running" sidecar nobody has touched for 5 min while the state
    file is stale too: most likely a daemon that died mid-shutdown.

    The daemon may legitimately stop polling during a long phase, so the
    state file alone going stale is not enough -- both must be quiet.
    """
    if not stale or not isinstance(progress, dict):
        return False
    if progress.get("state") != "running":
        return False
    written = progress.get("writtenAt")
    return is_numeric(written) and now - float(written) > PROGRESS_SILENT_AFTER


def progress_lines(progress: Optional[Dict[str, Any]], now: float, *,
                   indent: str = "   ", stale: bool = False) -> List[Line]:
    """Shutdown progress from the §3.4 sidecar (item 7), or [] when idle.

    The daemon resets the sidecar to ``idle`` on its first good poll after a
    restart, so a "running" file from a previous run never shows as live.
    """
    if not isinstance(progress, dict):
        return []
    state = str(progress.get("state") or "idle")
    if state == "idle":
        return []
    phases = [p for p in (progress.get("phases") or []) if isinstance(p, dict)]
    lines: List[Line] = []
    if state == "running":
        idx = next((i for i, p in enumerate(phases)
                    if p.get("state") not in _TERMINAL_PHASE_STATES),
                   len(phases) - 1 if phases else 0)
        head = "SHUTDOWN IN PROGRESS"
        if phases:
            cur = phases[idx]
            head += (f": phase {idx + 1}/{len(phases)} "
                     f"{PHASE_SHORT.get(cur.get('id'), cur.get('id'))}")
            if cur.get("state") == "running" and is_numeric(cur.get("startedAt")):
                head += f" (running {format_seconds(now - float(cur['startedAt']))})"
        if is_numeric(progress.get("startedAt")):
            head += f", started {format_age(now - float(progress['startedAt']))}"
        lines.append(_line(indent + head, "crit", 0))
        if progress_silent(progress, now, stale):
            lines.append(_line(
                f"{indent}No progress for "
                f"{short_duration(now - float(progress['writtenAt']))}"
                ": is the daemon still running?", "warn", 0))
    else:
        head = f"Last shutdown run: {state}"
        if is_numeric(progress.get("finishedAt")):
            head += f" {format_age(now - float(progress['finishedAt']))}"
        lines.append(_line(indent + head,
                           "crit" if state in ("failed", "timed-out") else "bold",
                           0))
    if progress.get("reason"):
        lines.append(_line(f"{indent}Reason: {progress['reason']}", "plain", 1))
    buckets = (("succeeded", "Done"), ("running", "Running"),
               ("failed", "Failed"), ("timed-out", "Timed out"),
               ("pending", "To do"), ("skipped", "Skipped"))
    parts = []
    for key, word in buckets:
        names = [PHASE_SHORT.get(p.get("id"), str(p.get("id")))
                 for p in phases if p.get("state") == key]
        if names:
            parts.append(f"{word}: {', '.join(names)}")
    if parts:
        lines.append(_line(indent + "Phases -- " + " | ".join(parts), "plain", 0))
    remotes = [r for r in (progress.get("remotes") or []) if isinstance(r, dict)]
    if remotes:
        chips = [f"{r.get('server') or r.get('host')} {r.get('state')}"
                 + (f" ({r['error']})" if r.get("error") else "")
                 for r in remotes]
        lines.append(_line(indent + "Remotes -- " + ", ".join(chips), "plain", 1))
    return lines


def ups_block_lines(data: Dict, now: float, width: int = 120) -> List[Line]:
    """Every status-panel line for one UPS (header, readings, freshness,
    what happens next, shutdown progress, resources)."""
    state = data.get("state") or {}
    ol = data.get("outlook") or {}
    header = f"   {data['label']}"
    if data.get("name") != data["label"]:
        header += f"  ({data['name']})"
    role = ol.get("role")
    if role:
        header += f"  · {role['label']}"
    elif data.get("is_local"):
        header += "  · Powers this host"
    lines: List[Line] = []

    if not state:
        lines.append(_line(header, "bold", 0, ("NO DATA", SEVERITY_WARN, False)))
        path = data.get("state_path") or "(unknown path)"
        for i, text in enumerate(missing_state_lines(
                path, data.get("missing_reason") or "missing")):
            lines.append(_line("   " + text, "plain", 0 if i == 0 else 1))
    else:
        fresh = freshness_view(data, now)
        summary = ol.get("statusSummary") or status_summary(
            state.get("STATUS", ""), trigger_active=state.get("TRIGGER_ACTIVE") == "1",
            stale=fresh["stale"])
        trig = ol.get("triggerOutlook") or {}
        acts = bool(role and (role.get("hasShutdownActions")
                              or role.get("kind") == "redundancy-member"))
        if (trig.get("firing") and acts and not fresh["stale"]
                and not summary.get("blink")):
            # A trigger condition is met and this UPS really shuts something
            # down: say so (red, blinking) even before the daemon's progress
            # file flips to "running". A monitoring-only UPS never escalates
            # (H1): its outlook line says "Notification only".
            summary = status_summary(state.get("STATUS", ""), trigger_active=True)
        if fresh["stale"] and (not summary.get("blink") or progress_silent(
                ol.get("shutdownProgress"), now, True)):
            age = fresh["age"]
            badge_text = "STALE " + (format_seconds(age).split(" ")[0]
                                     if age is not None else "")
            badge = (badge_text.strip(), SEVERITY_WARN, False)
        else:
            badge = (str(summary.get("label", "?")).upper(),
                     summary.get("severity", SEVERITY_WARN),
                     bool(summary.get("blink")))
        lines.append(_line(header, "bold", 0, badge))
        readings = "  ".join(reading_parts(state)) or "No readings reported"
        raw = state.get("STATUS", "")
        status_bit = f"{human_status(raw)} ({raw})" if raw else "Status unknown"
        if fresh["stale"]:
            lines.append(_line(f"   Last known: {status_bit}  {readings}", "bold", 0))
            lines.append(_line("   " + fresh["text"], "warn", 0))
            lines.append(_line("   " + STALE_HINT, "plain", 1))
        else:
            lines.append(_line(f"   {readings}", "bold", 0))
            lines.append(_line("   " + fresh["text"], "plain", 1))
        lines.extend(outlook_lines(ol, state, width, stale=fresh["stale"]))
        lines.extend(progress_lines(ol.get("shutdownProgress"), now,
                                    stale=fresh["stale"]))

    lines.append(_line(f"   Resources: {data.get('resources', 'none')}", "plain", 2))
    if data.get("remote_health_summary"):
        summary_text = data["remote_health_summary"]
        bad = any(word in summary_text for word in ("failed", "degraded"))
        lines.append(_line(f"   Remote health: {summary_text}",
                           "warn" if bad else "plain", 1 if bad else 2))
    return lines


def outlook_lines(ol: Dict, state: Dict[str, str], width: int, *,
                  stale: bool = False) -> List[Line]:
    """H3: while on battery (or a trigger is latched), which trigger is
    next, how close each one is, and what firing does.

    With stale data the countdowns would be guesses built on an old
    reading, so only the action is shown (H5: never dress old data as live).
    """
    trig = ol.get("triggerOutlook") or {}
    advisory = state.get("TRIGGER_ACTIVE") == "1"
    if not trig.get("onBattery") and not trig.get("firing") and not advisory:
        return []
    action = (trig.get("action") or {}).get("label")
    if stale:
        lines = [_line("   On battery at the last update; countdowns paused"
                       " (data is stale)", "warn", 0)]
        if action:
            lines.append(_line(f"   If a trigger fires: {action}", "bold", 0))
        return lines
    firing = bool(trig.get("firing")) or advisory
    lines: List[Line] = []
    lead = ""
    if trig.get("onBattery"):
        if "TIME_ON_BATTERY" in state:
            lead = f"On battery {short_duration(trig.get('timeOnBattery', 0))} · "
        else:
            lead = "On battery · "
    lines.append(_line(f"   {lead}{outlook_headline(trig)}",
                       "crit" if firing else "warn", 0))
    if advisory and state.get("TRIGGER_REASON"):
        lines.append(_line(f"   Trigger: {state['TRIGGER_REASON']}", "crit", 0))
    chips = [c for c in (trigger_chip(t) for t in trig.get("triggers") or []) if c]
    if chips:
        for text in wrap_items("   Shutdown when: ", chips, max(20, width - 4),
                               "                  "):
            lines.append(_line(text, "plain", 1))
    if action:
        verb = "Trigger fired -> " if firing else "If a trigger fires: "
        lines.append(_line(f"   {verb}{action}", "crit" if firing else "bold", 0))
    return lines


def redundancy_block_lines(rg: Dict, now: float, width: int = 120) -> List[Line]:
    """M9: one redundancy group -- who is healthy, quorum, what happens."""
    name = rg.get("name", "?")
    if rg.get("error"):
        return [_line(f"   Redundancy group {name}: status unavailable", "warn", 0,
                      ("UNKNOWN", SEVERITY_WARN, False))]
    outlook = rg.get("outlook") or {}
    ostate = outlook.get("state", "healthy")
    badge = (_GROUP_BADGES.get(ostate, ostate.upper()),
             outlook.get("severity", SEVERITY_WARN), ostate == "shutting-down")
    head = (f"   Redundancy group {name}: {rg.get('healthyCount', 0)}/"
            f"{rg.get('total', 0)} healthy, need {rg.get('minHealthy', '?')}")
    style = {"crit": "crit", "warn": "warn"}.get(outlook.get("severity"), "plain")
    lines = [_line(head, "bold", 0, badge)]
    if outlook.get("label"):
        lines.append(_line(f"     {outlook['label']}", style, 0))
    labels = rg.get("labels") or {}
    failing = set(rg.get("failingMembers") or [])
    members = rg.get("members") or {}
    chips = []
    for member in labels or members:
        info = members.get(member) or {}
        reason = info.get("healthReason", "unknown")
        chip = f"{labels.get(member, member)}: {reason}"
        if member in failing:
            chip += " (counts as failed)"
        chips.append(chip)
    for text in wrap_items("     Members: ", chips, max(20, width - 4),
                           "              "):
        lines.append(_line(text, "plain", 0 if failing else 1))
    if outlook.get("action"):
        lines.append(_line(f"     Group shutdown: {outlook['action']}", "plain",
                           1 if ostate in ("quorum-lost", "at-risk") else 2))
    lines.extend(progress_lines(rg.get("shutdownProgress"), now, indent="     "))
    return lines


def config_panel_lines(groups_data: List[Dict], rg_data: Optional[List[Dict]],
                       width: int, now: Optional[float] = None) -> List[Line]:
    """All status-panel lines: every UPS, then every redundancy group."""
    now = time.time() if now is None else now
    lines: List[Line] = []
    # Blank separators are priority 1: on a short terminal the
    # "Resources" / "Remote health" rows (priority 2) go first, so blocks
    # stay visibly apart.
    for i, data in enumerate(groups_data):
        if i:
            lines.append(_line("", "plain", 1))
        lines.extend(ups_block_lines(data, now, width))
    for rg in rg_data or []:
        lines.append(_line("", "plain", 1))
        lines.extend(redundancy_block_lines(rg, now, width))
    return lines


def fit_lines(lines: List[Line], max_rows: int) -> List[Line]:
    """Drop the least important lines (highest priority number, last first)
    until ``lines`` fits ``max_rows``; hard-cut only if priority-0 lines
    alone overflow. Keeps a small SSH window showing what matters."""
    lines = list(lines)
    while lines and not lines[0].text:
        del lines[0]
    if max_rows <= 0:
        return []
    for level in (2, 1):
        # Text rows of this level go before blank separators of it.
        for blank in (False, True):
            while len(lines) > max_rows:
                idx = next((i for i in range(len(lines) - 1, -1, -1)
                            if lines[i].priority >= level
                            and (not lines[i].text) == blank), None)
                if idx is None:
                    break
                del lines[idx]
    return lines[:max_rows]


# ==============================================================================
# RENDERING HELPERS
# ==============================================================================

def display_width(text: str) -> int:
    """Approximate the on-screen *cell* width of ``text``.

    Conservative: any code point at or above U+1100 is treated as 2
    cells. Covers the common cases that broke the events panel (emoji,
    CJK), at the cost of occasionally over-truncating exotic glyphs.
    """
    width = 0
    for ch in text:
        cp = ord(ch)
        if cp >= 0x1100:
            width += 2
        elif cp == 0:
            # NUL is invisible; ignore.
            continue
        else:
            width += 1
    return width


def truncate_to_width(text: str, max_width: int) -> str:
    """Return the longest prefix of ``text`` whose display width <= max_width."""
    if max_width <= 0:
        return ""
    if display_width(text) <= max_width:
        return text
    out = []
    width = 0
    for ch in text:
        cw = 2 if ord(ch) >= 0x1100 else 1
        if width + cw > max_width:
            break
        out.append(ch)
        width += cw
    return "".join(out)


def safe_addstr(win, y: int, x: int, text: str, attr: int = 0):
    """Write string to window, clipping to display width to avoid overflow.

    Critically, this must clip by *cell* width (which is what curses
    actually paints), not character count. Emoji, CJK, and other
    double-width glyphs would otherwise spill past the right edge of
    the visible panel.
    """
    max_y, max_x = win.getmaxyx()
    if y < 0 or y >= max_y or x >= max_x:
        return
    available_cells = max_x - x - 1
    if available_cells <= 0:
        return
    truncated = truncate_to_width(text, available_cells)
    if not truncated:
        return
    try:
        # We've already truncated to fit; pass len() so curses doesn't
        # re-clip more aggressively than necessary.
        win.addnstr(y, x, truncated, len(truncated), attr)
    except curses.error:
        pass


def fill_row(win, y: int, attr: int):
    """Fill an entire row with a background color, edge to edge.

    Curses raises when writing the bottom-right cell (cursor would advance
    past the screen), so we paint the first ``max_x - 1`` cells with
    ``addnstr`` and the rightmost cell with ``insch`` -- the standard
    workaround that avoids the unpainted vertical strip on the right edge.
    """
    max_y, max_x = win.getmaxyx()
    if y < 0 or y >= max_y or max_x <= 0:
        return
    try:
        win.addnstr(y, 0, " " * (max_x - 1), max_x - 1, attr)
    except curses.error:
        pass
    try:
        win.insch(y, max_x - 1, ord(" "), attr)
    except curses.error:
        pass


# ==============================================================================
# PANEL RENDERING
# ==============================================================================

def render_header(win, y: int, width: int, group_count: int):
    """Render title bar: full-width, white bold on black."""
    attr = curses.color_pair(C_HEADER) | curses.A_BOLD
    fill_row(win, y, curses.color_pair(C_HEADER))
    text = f"  Eneru v{__version__}"
    if group_count > 1:
        text += f"    {group_count} UPS groups"
    text += f"    {datetime.now().strftime('%H:%M:%S')}"
    safe_addstr(win, y, 0, text, attr)


def _style_attr(style: str) -> int:
    """curses attribute for a view-model line style (on the gray panel)."""
    gray = curses.color_pair(C_GRAY_BG)
    if style == "bold":
        return gray | curses.A_BOLD
    if style == "warn":
        return curses.color_pair(C_STATUS_WARN) | curses.A_BOLD
    if style == "crit":
        return curses.color_pair(C_STATUS_CRIT) | curses.A_BOLD
    return gray


def _ellipsize(text: str, max_cells: int) -> str:
    """Cut ``text`` to ``max_cells`` with a trailing "…" when it overflows."""
    if max_cells <= 0:
        return ""
    if display_width(text) <= max_cells:
        return text
    return truncate_to_width(text, max_cells - 1) + "…"


def paint_line(win, y: int, width: int, line: Line) -> None:
    """Paint one view-model line: text on the left, badge on the right."""
    badge_text = f"  {line.badge[0]}  " if line.badge else ""
    text_room = width - 4
    if badge_text:
        text_room = max(8, width - display_width(badge_text) - 5)
    text = _ellipsize(line.text, text_room)
    if line.style in ("warn", "crit"):
        # Coloured strip only under the words; the indent stays gray.
        stripped = text.lstrip(" ")
        indent = len(text) - len(stripped)
        safe_addstr(win, y, indent, stripped, _style_attr(line.style))
    else:
        safe_addstr(win, y, 0, text, _style_attr(line.style))
    if badge_text:
        severity, blink = line.badge[1], line.badge[2]
        attr = summary_attr({"severity": severity, "blink": blink})
        sx = max(display_width(text) + 1, width - display_width(badge_text) - 3)
        safe_addstr(win, y, sx, badge_text, attr)


def render_config_panel(win, y_start: int, y_end: int, width: int,
                        groups_data: List[Dict],
                        rg_data: Optional[List[Dict]] = None,
                        now: Optional[float] = None) -> int:
    """Render the status panel (gray background, edge to edge).

    Lines come from :func:`config_panel_lines`; when the panel is shorter
    than the content, :func:`fit_lines` drops the least important rows
    first. Returns the number of content rows painted.
    """
    gray_attr = curses.color_pair(C_GRAY_BG)
    for row in range(y_start, y_end):
        fill_row(win, row, gray_attr)
    lines = fit_lines(config_panel_lines(groups_data, rg_data, width, now),
                      y_end - y_start - 1)
    for i, line in enumerate(lines):
        paint_line(win, y_start + 1 + i, width, line)
    return len(lines)


HELP_HINT = ("<?>", "Help")


def key_hints(*, graph_mode: str = "off", time_range: str = "1h",
              ups_index: int = 0, ups_total: int = 1,
              verbosity: int = EVENTS_VERBOSITY_POWER) -> List[Tuple[str, str]]:
    """Bottom-row key hints, most useful first, <?> Help last.

    G/T/U/V descriptions carry the current cycle state so an operator can
    see "graph is on charge, 1h" without remembering the cycle order.
    """
    ups_descr = (f"UPS: {ups_index + 1}/{ups_total}" if ups_total > 1
                 else "UPS")
    return [
        ("<Q>", "Quit"),
        ("<R>", "Refresh"),
        ("<M>", "More logs"),
        ("<↑↓>", "Scroll"),
        ("<G>", f"Graph: {graph_mode}"),
        ("<T>", f"Time: {time_range}"),
        ("<U>", ups_descr),
        ("<V>", f"Events: {_events_verbosity_label(verbosity)}"),
        HELP_HINT,
    ]


def _hint_width(hint: Tuple[str, str]) -> int:
    return len(hint[0]) + 2 + len(hint[1]) + 3


def layout_key_hints(hints: List[Tuple[str, str]], width: int,
                     max_rows: int = 2) -> List[List[Tuple[str, str]]]:
    """Pack hints into at most ``max_rows`` rows of ``width`` cells (M7).

    80 columns fit every hint on two rows. When even that overflows, the
    last row keeps ``<?> Help`` so the full key list is one press away --
    a key is never silently undiscoverable.
    """
    room = max(1, width - 3)
    rows: List[List[Tuple[str, str]]] = [[]]
    used = 0
    for hint in hints:
        w = _hint_width(hint)
        if rows[-1] and used + w > room:
            rows.append([])
            used = 0
        rows[-1].append(hint)
        used += w
    if len(rows) <= max_rows:
        return rows
    rows = rows[:max_rows]
    last = [h for h in rows[-1] if h != HELP_HINT]
    while last and sum(_hint_width(h) for h in last) + _hint_width(HELP_HINT) > room:
        last.pop()
    rows[-1] = last + [HELP_HINT]
    return rows


def help_lines(*, graph_mode: str = "off", time_range: str = "1h",
               ups_index: int = 0, ups_total: int = 1,
               verbosity: int = EVENTS_VERBOSITY_POWER) -> List[str]:
    """The <?> overlay: every key, what it does, and the current value."""
    ups_now = f"{ups_index + 1}/{ups_total}" if ups_total > 1 else "only one"
    return [
        "Keys (press any key to close)",
        "  Q / Esc     Quit",
        "  R           Refresh now and jump to the newest event",
        "  M           More logs: up to 500 events (scrolling turns it on)",
        "  Up / Down   Scroll events; PgUp/PgDn by 10; Home/End jump to the ends",
        f"  G           Graph: off > charge > load > voltage > runtime (now {graph_mode})",
        f"  T           Graph window: 1h > 6h > 24h > 7d > 30d (now {time_range})",
        f"  U           Which UPS the graph shows (now {ups_now})",
        "  V           Events: power > +diagnostics > all "
        f"(now {_events_verbosity_label(verbosity)})",
        "  ?           Show / hide this help",
        "Badges: green = OK, amber = warning, red = critical. Only",
        "  'SHUTDOWN TRIGGERED' and 'SHUTTING DOWN' blink.",
        "Events: newest at the bottom; the second column is how long ago.",
    ]


def render_logs_panel(win, y_start: int, y_end: int, width: int,
                      events: List[str], show_more: bool,
                      *, graph_mode: str = "off", time_range: str = "1h",
                      ups_index: int = 0, ups_total: int = 1,
                      scroll_offset: int = 0,
                      verbosity: int = EVENTS_VERBOSITY_POWER,
                      show_help: bool = False):
    """Render the logs panel with yellow/gold background, edge to edge.

    The bottom key hints reflect the *current* graph mode, time range, and
    (in multi-UPS) the active UPS index, wrapped onto two rows when one
    doesn't fit. ``show_help`` replaces the events with the key overlay.
    """
    gold_attr = curses.color_pair(C_GOLD_BG)
    gold_bold = gold_attr | curses.A_BOLD
    key_attr = curses.color_pair(C_GOLD_KEY) | curses.A_BOLD
    dim_attr = curses.color_pair(C_GOLD_DIM)

    # Fill entire panel with gold background
    for row in range(y_start, y_end):
        fill_row(win, row, gold_attr)

    state = dict(graph_mode=graph_mode, time_range=time_range,
                 ups_index=ups_index, ups_total=ups_total, verbosity=verbosity)
    hint_rows = layout_key_hints(key_hints(**state), width)
    footer_lines = 1 + len(hint_rows)
    max_cells = max(0, width - 4)

    y = y_start + 1  # top padding

    if show_help:
        for i, text in enumerate(help_lines(**state)):
            if y >= y_end - footer_lines:
                break
            safe_addstr(win, y, 0, _ellipsize("   " + text, max_cells),
                        gold_bold if i == 0 else gold_attr)
            y += 1
    else:
        title = "   Recent Events (newest at the bottom"
        title += ", scrolled)" if scroll_offset else ")"
        safe_addstr(win, y, 0, title, gold_bold)
        y += 1

    if show_help:
        events = []  # the overlay owns the panel body
    elif not events:
        safe_addstr(win, y, 0, f"   {_no_events_message(verbosity)}", gold_attr)
        y += 1
    if events:
        available = y_end - y - footer_lines
        if available <= 0:
            display_events = []
        else:
            visible = max(3, available) if not show_more else max(1, available)
            # scroll_offset = 0 anchors the bottom (most recent) on the
            # last visible row. Larger offsets reveal older events; clamp
            # so the user can never scroll past the oldest entry.
            max_offset = max(0, len(events) - visible)
            offset = max(0, min(scroll_offset, max_offset))
            end_idx = len(events) - offset
            start_idx = max(0, end_idx - visible)
            display_events = events[start_idx:end_idx]

        for event in display_events:
            if y >= y_end - footer_lines:
                break
            is_section = event in (
                EVENT_SECTION_POWER,
                EVENT_SECTION_DIAGNOSTICS,
                EVENT_SECTION_LIFECYCLE,
            )
            # Display-cell width (handles emoji + CJK) so lines never
            # spill past the panel edge; "…" marks a cut line.
            display = _ellipsize(f"   {event}", max_cells)
            # Pad to full row width with gold-bg spaces so the line
            # overwrites every cell of the row, not just where the text
            # ends. Mobile SSH clients often render emoji at a different
            # cell width than display_width predicts; without this pad,
            # any miscount leaves cells with stale or unpainted bg.
            pad_cells = max(0, width - display_width(display))
            attr = gold_bold if is_section else gold_attr
            safe_addstr(win, y, 0, display + (" " * pad_cells), attr)
            y += 1

    # Key hints at the bottom of the gold panel (one or two rows).
    for r, row in enumerate(hint_rows):
        hint_y = y_end - len(hint_rows) + r
        x = 2
        for label, descr in row:
            # Advance by characters: curses moves one cell per arrow glyph,
            # while display_width() deliberately over-counts them.
            safe_addstr(win, hint_y, x, f" {label} ", key_attr)
            x += len(label) + 2
            safe_addstr(win, hint_y, x, f" {descr}  ", dim_attr)
            x += len(descr) + 3


# ==============================================================================
# MAIN TUI LOOP
# ==============================================================================

def cycle(values: tuple, current: str) -> str:
    """Return the next value in ``values`` after ``current`` (wraps)."""
    try:
        idx = values.index(current)
    except ValueError:
        return values[0]
    return values[(idx + 1) % len(values)]


def _robust_bounds(values: List[float]) -> Tuple[float, float]:
    """5th/95th percentile of ``values``, with a min(values)/max(values)
    fallback for short series. Used by the graph panel to keep a single
    outlier (e.g. a stray 0V from a NUT poll race) from squashing the
    plotted band into a single row at the top of the chart.

    Symmetric drop-count formulation: clip ``int(n * 0.05)`` samples
    from each end of the sorted series. For ``n = 20`` that's one
    sample per side -- ``lo = sorted[1]``, ``hi = sorted[18]`` --
    cleanly excluding both extremes. The earlier ``int(n * 0.95)``
    formulation produced ``sorted[19]`` (the actual max) and silently
    no-op'd the helper at small n.

    When the percentile bounds collapse (every sample inside the 5..95
    band is identical), the caller's pad-around-flat logic handles the
    ``hi == lo`` case -- we deliberately do *not* fall back to
    min/max here, because that would re-introduce the very outlier the
    helper exists to clip.

    At ``n < 40`` only one outlier can be clipped per side; with two or
    more bottom-side outliers in a 20-sample series, one survives into
    the bound. Acceptable for our use case (the writer-side filter at
    ``stats._to_input_voltage`` has already dropped most phantom zeros).
    """
    if not values:
        return 0.0, 0.0  # caller guards but defensive: don't raise on min([])
    n = len(values)
    if n < 20:
        return min(values), max(values)
    sorted_vals = sorted(values)
    drop = int(n * 0.05)  # number of samples to clip from each end
    lo = sorted_vals[drop]
    hi = sorted_vals[n - 1 - drop]
    return lo, hi


def render_graph_panel(stdscr, y_start: int, y_end: int, width: int,
                       config: Config, group: UPSGroupConfig,
                       graph_mode: str, time_range: str):
    """Render the graph panel (bottom of the gray section) when active.

    Layout per panel (top to bottom):
      0: title       --  ``Graph: charge (1h)  --  TestUPS``
      1: stat header --  ``now: 100%   min: 98%   max: 100%``
      2..N-1: graph rows with a left ``Y-axis label`` gutter
      N: footer     --  ``data: 12h of 30d`` (only when sparse)
    """
    gray_attr = curses.color_pair(C_GRAY_BG)
    gray_bold = gray_attr | curses.A_BOLD
    panel_h = y_end - y_start
    if panel_h <= 1:
        return
    for row in range(y_start, y_end):
        fill_row(stdscr, row, gray_attr)

    title = f"   Graph: {graph_mode} ({time_range})  --  {group.ups.label}"
    safe_addstr(stdscr, y_start, 0, title, gray_bold)

    info = METRIC_INFO.get(graph_mode)
    if info is None:
        safe_addstr(stdscr, y_start + 1, 3, "(unknown metric)", gray_attr)
        return
    column, unit, cfg_y_min, cfg_y_max, fmt = info

    seconds = TIME_RANGE_SECONDS.get(time_range, 3600)
    series = query_metric_series(config, group, graph_mode, seconds)
    if not series:
        safe_addstr(stdscr, y_start + 1, 3, "(no data yet)", gray_attr)
        return

    values = [v for _, v in series]
    timestamps = [ts for ts, _ in series]
    end_ts = int(time.time())
    start_ts = end_ts - seconds

    # Y-axis bounds: prefer the metric's configured range (charge/load
    # are 0-100); fall back to observed range for unbounded metrics
    # (voltage/runtime). Without this, voltage with a 0.5V swing
    # autoscales to that swing AND we still want the display to read
    # the actual range, not "0-235".
    obs_min = min(values)
    obs_max = max(values)
    if cfg_y_min is None or cfg_y_max is None:
        # Unbounded metric (voltage / runtime). A single outlier -- e.g.
        # a phantom 0V sample left over from before the on-line zero
        # filter -- would otherwise drag obs_min to 0 and squash the
        # meaningful band into one row at the top of the chart. Use the
        # 5th/95th percentile when there's enough data to compute one;
        # outliers still plot (clipped) at the band edges thanks to the
        # norm-clamp in BrailleGraph.plot.
        scale_min, scale_max = _robust_bounds(values)
    else:
        scale_min, scale_max = obs_min, obs_max
    y_min = cfg_y_min if cfg_y_min is not None else scale_min
    y_max = cfg_y_max if cfg_y_max is not None else scale_max
    if y_max <= y_min:  # single sample or flat line
        pad = max(abs(y_min) * 0.05, 1.0)
        y_min -= pad
        y_max += pad

    # Stat header (row 1): now / min / max in human units.
    current = values[-1]
    stat_line = (f"   now: {fmt(current)}{unit}"
                 f"   min: {fmt(obs_min)}{unit}"
                 f"   max: {fmt(obs_max)}{unit}")
    safe_addstr(stdscr, y_start + 1, 0, stat_line, gray_attr)

    # Reserve rows for title (1), stat header (1), and an optional
    # footer when data is sparse. Graph itself takes the remainder.
    actual_span = max(0, (timestamps[-1] - timestamps[0]) if timestamps else 0)
    sparse = actual_span < int(seconds * 0.5) and actual_span > 0
    footer_rows = 1 if sparse else 0
    graph_top = y_start + 2
    graph_bot = y_end - footer_rows
    g_h = max(2, graph_bot - graph_top)

    # Y-axis label gutter. Width is computed from the actual labels we'd
    # produce so longer values like "235.4V" or "1h 30m" don't overflow
    # into the graph area. Each label is "<value><unit> <tick>" -- e.g.
    # "100% ┤" (6 cells) or "235.4V ┤" (8 cells). We add 1 cell of
    # left-margin so the labels aren't flush against column 0.
    tick = "┤" if BrailleGraph.supported() else "|"
    sample_labels = [f"{fmt(v)}{unit}" for v in (y_min, (y_min + y_max) / 2.0, y_max)]
    label_w = max(len(s) for s in sample_labels) + 3   # value + " " + tick + 1 margin
    g_w = max(10, width - label_w - 3)

    rows = BrailleGraph.plot(
        values, width=g_w, height=g_h,
        y_min=y_min, y_max=y_max,
        x_values=timestamps, x_min=start_ts, x_max=end_ts,
    )
    # Y-axis labels on top, middle, bottom rows of the graph. Labels
    # are right-aligned within the gutter so the graph itself starts at
    # a consistent column regardless of label length.
    def axis_label(value: float) -> str:
        return f"{fmt(value)}{unit} {tick}".rjust(label_w)

    if g_h >= 1:
        safe_addstr(stdscr, graph_top,             0, axis_label(y_max), gray_attr)
    if g_h >= 3:
        safe_addstr(stdscr, graph_top + g_h // 2,  0,
                    axis_label((y_min + y_max) / 2.0), gray_attr)
    if g_h >= 2:
        safe_addstr(stdscr, graph_top + g_h - 1,   0, axis_label(y_min), gray_attr)

    for i, line in enumerate(rows):
        safe_addstr(stdscr, graph_top + i, label_w, line, gray_attr)

    if sparse:
        footer = (f"   data: {format_seconds(actual_span)} "
                  f"of {format_seconds(seconds)} requested")
        safe_addstr(stdscr, y_end - 1, 0, footer, gray_attr)


def run_tui(config: Config, interval: int = 5, *,
            initial_graph: Optional[str] = None,
            initial_time_range: str = "1h",
            initial_ups_index: int = 0,
            verbose: int = EVENTS_VERBOSITY_POWER):
    """Run the curses TUI dashboard.

    Optional kwargs let the CLI pre-seed the cycle state so flags like
    ``--graph voltage --time 7d`` open the dashboard already showing the
    requested view, instead of starting on the default (graph off) and
    forcing the operator to press <G>/<T> to get there.

    ``verbose`` is a count-style event verbosity: 0 shows Power Events,
    1 adds Diagnostics, and 2 adds Lifecycle. The ``<V>`` key cycles this
    in-session. ``<?>`` (or ``h``) shows every key; any key closes it.
    """
    def _main(stdscr):
        init_colors()
        curses.curs_set(0)
        stdscr.timeout(interval * 1000)
        stdscr.bkgd(' ', curses.color_pair(C_BORDER))
        # Enable keypad translation so curses delivers KEY_UP / KEY_DOWN /
        # KEY_PPAGE / KEY_NPAGE / KEY_HOME / KEY_END for the events-panel
        # scroll bindings. Without this, arrow keys arrive as raw escape
        # sequences (ESC + [ + A) and the leading ESC byte (27) hits the
        # quit branch instead of scrolling.
        stdscr.keypad(True)

        show_more = False
        graph_mode = (
            initial_graph if initial_graph in GRAPH_MODES else "off"
        )
        time_range = (
            initial_time_range if initial_time_range in TIME_RANGES else "1h"
        )
        ups_index = max(
            0, min(int(initial_ups_index or 0), max(0, len(config.ups_groups) - 1))
        )
        events_verbosity = _events_verbosity(verbose)
        events_scroll = 0            # ↑/↓ scrolls events panel; 0 = bottom
        show_help = False            # <?> key overlay (M7)

        while True:
            stdscr.erase()
            height, width = stdscr.getmaxyx()

            if height < 10 or width < 50:
                safe_addstr(stdscr, 0, 0, "Terminal too small (min 50x10)",
                            curses.color_pair(C_STATUS_CRIT) | curses.A_BOLD)
                stdscr.refresh()
                key = stdscr.getch()
                if key in (ord('q'), ord('Q'), 27):
                    break
                continue

            # Header (row 0)
            render_header(stdscr, 0, width, len(config.ups_groups))

            # Collect data first so the status panel can be sized to what
            # it actually holds (L8: no blank filler rows). Prefer the
            # SQLite events tier; fall back to the log-tail parser when no
            # DB is present. Also push a fresh state-file snapshot into
            # each group's live buffer so the graph panel can blend SQLite
            # + post-flush samples (spec 2.13 -- bridges the 0-10s gap).
            now = time.time()
            groups_data = []
            for g in config.ups_groups:
                groups_data.append(collect_group_data(g, config, now))
                update_live_buffer(g, config)
            rg_data = collect_redundancy_data(config, groups_data, now)

            ups_total = len(config.ups_groups) or 1
            hint_state = dict(graph_mode=graph_mode, time_range=time_range,
                              ups_index=ups_index, ups_total=ups_total,
                              verbosity=events_verbosity)
            hint_rows = len(layout_key_hints(key_hints(**hint_state), width))
            # Logs keep at least: padding + title + 3 events + gap + hints.
            min_logs = 6 + hint_rows
            graph_on = graph_mode != "off" and bool(config.ups_groups)
            graph_min = 7 if graph_on else 0
            max_config_rows = max(
                3, height - 1 - 1 - min_logs - graph_min - 1)
            panel_lines = fit_lines(
                config_panel_lines(groups_data, rg_data, width, now),
                max_config_rows)

            # Config panel starts at row 1: top padding + lines (L8: no
            # filler rows; the black spacer below already separates panels).
            config_start = 1
            config_end = config_start + len(panel_lines) + 1

            # Optional graph panel between config and logs.
            graph_start = config_end + 1
            graph_end = graph_start
            if graph_on:
                graph_end = graph_start + max(5, (height - config_end) // 2)
                graph_end = min(graph_end, height - min_logs - 1)

            # Black spacer row between panels
            fill_row(stdscr, config_end, curses.color_pair(C_BORDER))

            # Logs panel fills the rest (after spacer / graph)
            logs_start = (graph_end + 1) if graph_end > graph_start else config_end + 1
            logs_end = height

            # Couple the data cap to the visible rows so the default view
            # shows the newest rows of every enabled tier; <M> still expands
            # to EVENTS_MAX_ROWS_MORE (500) for full scrollable history.
            visible_estimate = max(
                3, logs_end - logs_start - 3 - hint_rows  # title + pads + hints
            )
            if show_more:
                events_cap = EVENTS_MAX_ROWS_MORE
            else:
                events_cap = min(EVENTS_MAX_ROWS_NORMAL, visible_estimate)
            log_events = query_events_for_display(
                config, max_events=events_cap,
                verbosity=events_verbosity,
                grouped=True,
                compact=width < 100,
            )
            if not log_events and not events_db_available(config):
                log_events = parse_log_events(
                    config.logging.file or "",
                    max_events=events_cap,
                )

            # Render panels edge-to-edge
            render_config_panel(stdscr, config_start, config_end, width,
                                groups_data, rg_data, now)
            if graph_on and graph_end > graph_start:
                ups_index = max(0, min(ups_index, len(config.ups_groups) - 1))
                render_graph_panel(
                    stdscr, graph_start, graph_end, width,
                    config, config.ups_groups[ups_index],
                    graph_mode, time_range,
                )
                # Spacer between graph and logs
                fill_row(stdscr, graph_end, curses.color_pair(C_BORDER))
            # Bound the scroll offset to the current events list so a
            # refresh that shrinks the list doesn't strand the user on
            # an empty view.
            events_scroll = max(0, min(events_scroll, max(0, len(log_events) - 1)))
            if show_help:
                # The key overlay borrows the whole body so every line
                # fits even at 80x24 with the graph on.
                logs_start = 1
            render_logs_panel(stdscr, logs_start, logs_end, width,
                              log_events, show_more,
                              scroll_offset=events_scroll,
                              show_help=show_help,
                              **hint_state)

            # Move cursor to bottom-right to avoid visual artifacts
            try:
                stdscr.move(height - 1, width - 1)
            except curses.error:
                pass

            stdscr.refresh()

            # Handle input
            key = stdscr.getch()
            if key in (ord('q'), ord('Q'), 27):
                break
            if show_help:
                # Any key (not the refresh timeout) closes the overlay.
                if key != -1:
                    show_help = False
                continue
            if key in (ord('?'), ord('h'), ord('H')):
                show_help = True
                continue
            elif key == ord('r'):
                events_scroll = 0
                continue
            elif key in (ord('m'), ord('M')):
                show_more = not show_more
            elif key in (ord('v'), ord('V')):
                events_verbosity = (
                    events_verbosity + 1
                ) % (EVENTS_VERBOSITY_ALL + 1)
                events_scroll = 0  # reset so the new (longer/shorter) list anchors at bottom
            elif key in (ord('g'), ord('G')):
                graph_mode = cycle(GRAPH_MODES, graph_mode)
            elif key in (ord('t'), ord('T')):
                time_range = cycle(TIME_RANGES, time_range)
            elif key in (ord('u'), ord('U')):
                if config.ups_groups:
                    ups_index = (ups_index + 1) % len(config.ups_groups)
            elif key in (curses.KEY_UP, curses.KEY_PPAGE, curses.KEY_HOME):
                # First scroll auto-promotes to More mode so the cap is
                # the bigger 500-row window. Without this, normal mode's
                # 8-row cap means there's nothing to scroll back to and
                # the arrow keys are a silent no-op despite the visible
                # `<↑↓> Scroll` hint.
                if not show_more:
                    show_more = True
                if key == curses.KEY_UP:
                    # Scroll one event toward older history. The render
                    # function clamps to the current list size.
                    events_scroll += 1
                elif key == curses.KEY_PPAGE:    # PgUp
                    events_scroll += 10
                else:                            # KEY_HOME
                    # Jump to the oldest event currently in the list.
                    # render_logs_panel clamps so the value just needs
                    # to overshoot the visible window.
                    events_scroll = len(log_events)
            elif key == curses.KEY_DOWN:
                events_scroll = max(0, events_scroll - 1)
            elif key == curses.KEY_NPAGE:    # PgDn
                events_scroll = max(0, events_scroll - 10)
            elif key == curses.KEY_END:
                events_scroll = 0

    try:
        curses.wrapper(_main)
    except curses.error as exc:
        original_term = os.environ.get("TERM", "")
        if not _missing_ghostty_terminfo(original_term, exc):
            raise

        os.environ["TERM"] = GHOSTTY_FALLBACK_TERM
        try:
            curses.wrapper(_main)
        finally:
            if original_term:
                os.environ["TERM"] = original_term
            else:
                os.environ.pop("TERM", None)


# ==============================================================================
# --once MODE (no curses, stdout)
# ==============================================================================

def render_graph_text(
    config: Config,
    group: UPSGroupConfig,
    metric: str,
    time_range: str,
    *,
    width: int = 60,
    height: int = 6,
    force_fallback: bool = False,
) -> List[str]:
    """Render an ASCII / Braille graph for a metric to stdout-friendly lines.

    Always returns a non-empty list -- callers can print it directly.
    Used by ``run_once --graph``. The footer states the real y-axis scale
    (bottom and top value, M11) plus now/min/max, like the live panel.
    """
    seconds = TIME_RANGE_SECONDS.get(time_range, 3600)
    series = query_metric_series(config, group, metric, seconds)
    info = METRIC_INFO.get(metric)
    if info is None:
        return [f"(unknown metric: {metric})"]
    _, unit, cfg_min, cfg_max, fmt = info
    if cfg_min is not None and cfg_max is not None:
        y_axis_label = f"{int(cfg_min)}-{int(cfg_max)}{unit}"
    else:
        y_axis_label = f"{unit or 'seconds'}, auto-scaled"
    title = f"{metric} -- last {time_range}  ({y_axis_label})"
    if not series:
        return [
            title,
            "(no data)",
        ]
    values = [v for _, v in series]
    lo, hi = _robust_bounds(values)
    y_min = cfg_min if cfg_min is not None else lo
    y_max = cfg_max if cfg_max is not None else hi
    if y_max <= y_min:  # single sample or flat line
        pad = max(abs(y_min) * 0.05, 1.0)
        y_min -= pad
        y_max += pad
    rows = BrailleGraph.plot(
        values,
        width=width,
        height=height,
        y_min=y_min,
        y_max=y_max,
        force_fallback=force_fallback,
    )
    footer = (f"y-axis: {fmt(y_min)}{unit} (bottom) to {fmt(y_max)}{unit} (top)"
              f"  now: {fmt(values[-1])}{unit}  min: {fmt(min(values))}{unit}"
              f"  max: {fmt(max(values))}{unit}")
    return [title] + rows + [footer]


def _once_text(lines: List[Line], indent: str = "  ") -> List[str]:
    """View-model lines as plain text for ``--once`` (badge -> "-- BADGE")."""
    out = []
    for i, line in enumerate(lines):
        text = line.text
        if i == 0:
            text = text.strip()
            if line.badge:
                text += f"  --  {line.badge[0]}"
        elif text.startswith("   "):
            text = indent + text[3:]
        out.append(text.rstrip())
    return out


def run_once(config: Config, *, graph_metric: Optional[str] = None,
             time_range: str = "1h", events_only: bool = False,
             verbose: int = EVENTS_VERBOSITY_POWER,
             length: int = EVENTS_MAX_ROWS_NORMAL,
             width: int = 100):
    """Print a status snapshot to stdout and exit.

    With ``events_only=True`` the status / resource summary and graph
    block are skipped -- only the events list (from SQLite if available,
    otherwise the log tail) is printed, in the stable raw
    ``YYYY-MM-DD HH:MM:SS  [UPS] EVENT_TYPE: detail`` form. Useful for
    scripts and CI.

    ``time_range`` is **graph-only** -- it does not affect the events
    list. Events are sparse and a fixed window made the panel silently
    empty for normal homelab usage.

    ``verbose`` is a count-style event verbosity: 0 shows Power Events,
    1 adds Diagnostics, and 2 adds Lifecycle.

    ``length`` caps the events list (default :data:`EVENTS_MAX_ROWS_NORMAL`,
    set to 0 for no cap). Power events keep at least half of the cap;
    each other enabled tier gets a share of the rest.

    The status block uses the same view model as the live TUI, so both say
    the same words (status label, "Updated 3s ago", next trigger, shutdown
    progress, redundancy groups).
    """
    verbose = _events_verbosity(verbose)
    # length=0 means "no cap" -- pass None through to the query.
    events_cap = None if length == 0 else length

    if events_only:
        # Scripts parse this: keep the stable raw EVENT_TYPE lines.
        events = query_events_for_display(
            config, max_events=events_cap,
            verbosity=verbose, raw=True,
        )
        if not events and not events_db_available(config):
            events = parse_log_events(config.logging.file or "",
                                      max_events=events_cap)
        if events:
            for line in events:
                print(line)
        else:
            print(_no_events_message(verbose))
        return

    now = time.time()
    print(f"Eneru v{__version__}")
    group_count = len(config.ups_groups)
    if group_count > 1:
        print(f"Mode: multi-UPS ({group_count} groups)")
    print(f"Time: {datetime.fromtimestamp(now).strftime('%Y-%m-%d %H:%M:%S')}")
    print()

    groups_data = [collect_group_data(g, config, now) for g in config.ups_groups]
    for i, data in enumerate(groups_data):
        lines = ups_block_lines(data, now, width)
        state = data.get("state") or {}
        text = _once_text(lines)
        if state.get("STATUS") and lines and lines[0].badge:
            text[0] += f" ({state['STATUS']})"
        for row in text:
            print(row)
        if i < group_count - 1:
            print()

    for rg in collect_redundancy_data(config, groups_data, now):
        print()
        for row in _once_text(redundancy_block_lines(rg, now, width)):
            print(row)

    # Snapshot path: same flag semantics as the events-only branch above.
    # --verbose increments enabled tiers; --length caps the row count.
    snapshot_cap = events_cap
    events = query_events_for_display(
        config, max_events=snapshot_cap,
        verbosity=verbose,
    )
    if not events and not events_db_available(config):
        events = parse_log_events(config.logging.file or "",
                                  max_events=snapshot_cap)
    if events:
        print()
        print("Recent Events:")
        for event in events:
            print(f"  {event}")

    if graph_metric:
        for group in config.ups_groups:
            print()
            print(f"Graph: {group.ups.label}")
            for line in render_graph_text(config, group, graph_metric,
                                          time_range):
                print(line)
