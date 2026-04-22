"""Curses-based TUI dashboard for Eneru (eneru monitor).

Reads UPS data from daemon state files -- no direct NUT polling.
Two-panel layout:
  - Top panel (gray background, white text): UPS config/status
  - Bottom panel (yellow/gold background, black text): event logs + key hints
"""

import curses
import sys
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, List, Tuple

from eneru.version import __version__
from eneru.config import Config, UPSGroupConfig
from eneru.graph import BrailleGraph
from eneru.stats import StatsStore


# Cycle order for the G key + --graph flag.
GRAPH_MODES = ("off", "charge", "load", "voltage", "runtime")
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
# list. 24h is the operational sweet spot -- short enough that the list
# stays readable, long enough to cover an overnight power event.
EVENTS_TIME_WINDOW = 24 * 3600

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
    name = group.ups.name
    if config.multi_ups:
        sanitized = name.replace("@", "-").replace(":", "-").replace("/", "-")
    else:
        sanitized = "default"
    return Path(config.statistics.db_directory) / f"{sanitized}.db"


def state_file_path_for(group: UPSGroupConfig, config: Config) -> Path:
    """Return the per-UPS state file path the daemon writes every poll.

    Multi-UPS mode appends a sanitized suffix; single-UPS uses the bare
    path. Used by both ``collect_group_data`` and ``update_live_buffer``.
    """
    name = group.ups.name
    if config.multi_ups:
        sanitized = name.replace("@", "-").replace(":", "-").replace("/", "-")
        return Path(config.logging.state_file + f".{sanitized}")
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
            store = StatsStore(db_path)
            store._conn = conn
            try:
                sqlite_series = store.query_range(column, start, end)
            finally:
                store._conn = None
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


def parse_log_events(log_path: str, max_events: int = 8) -> List[str]:
    """Read recent power events from the log file tail (fallback path)."""
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
                if len(events) >= max_events:
                    break
        events.reverse()
        return events
    except Exception:
        return []


def _format_event_line(ts: int, label: str, event_type: str,
                       detail: str, multi_ups: bool) -> str:
    """Format one event row from the SQLite events table for display."""
    try:
        time_str = datetime.fromtimestamp(int(ts)).strftime("%H:%M:%S")
    except (TypeError, ValueError, OSError):
        time_str = "??:??:??"
    prefix = f"[{label}] " if multi_ups else ""
    if detail:
        return f"{time_str}  {prefix}{event_type}: {detail}"
    return f"{time_str}  {prefix}{event_type}"


def query_events_for_display(
    config: Config,
    time_range_seconds: int = 24 * 3600,
    *,
    max_events: int = 50,
) -> List[str]:
    """Pull recent events from each UPS's SQLite store, sorted by timestamp.

    Returns formatted display strings ready to drop into the TUI events
    panel. Returns an empty list when no per-UPS DB exists -- callers
    should fall back to ``parse_log_events`` in that case.
    """
    end = int(time.time())
    start = end - max(60, int(time_range_seconds))
    multi_ups = config.multi_ups
    rows: List[tuple] = []  # (ts, label, event_type, detail)
    any_db_seen = False

    for group in config.ups_groups:
        db_path = stats_db_path_for(group, config)
        conn = StatsStore.open_readonly(db_path)
        if conn is None:
            continue
        any_db_seen = True
        try:
            store = StatsStore(db_path)
            store._conn = conn
            try:
                events = store.query_events(start, end)
            finally:
                store._conn = None
            for ts, etype, detail in events:
                rows.append((int(ts), group.ups.label, etype, detail or ""))
        finally:
            try:
                conn.close()
            except Exception:
                pass

    if not any_db_seen:
        return []  # signal "no DB" so callers can fall back

    rows.sort(key=lambda r: r[0])
    rows = rows[-max_events:]
    return [_format_event_line(ts, label, etype, detail, multi_ups)
            for ts, label, etype, detail in rows]


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
C_STATUS_OK = 8      # black on green (highlighted badge)
C_STATUS_OB = 9      # white on red (on battery -- alert)
C_STATUS_CRIT = 10   # white on red (critical/shutdown imminent, blink)
C_STATUS_UNK = 11    # white on magenta (unknown/connection lost)

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
    else:
        gray_bg = curses.COLOR_BLACK
        gold_bg = curses.COLOR_YELLOW
        dim_on_gold = curses.COLOR_WHITE
        black_fg = curses.COLOR_BLACK

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


def human_status(status: str) -> str:
    """Convert NUT status codes to human-readable labels."""
    s = status.upper().strip()
    if "FSD" in s:
        return "FORCED SHUTDOWN"
    if "OB" in s and "LB" in s:
        return "ON BATTERY - LOW"
    if "OB" in s and "DISCHRG" in s:
        return "ON BATTERY - DISCHARGING"
    if "OB" in s:
        return "ON BATTERY"
    if "OL" in s and "CHRG" in s:
        return "ONLINE - CHARGING"
    if "OL" in s:
        return "ONLINE"
    if "CHRG" in s:
        return "CHARGING"
    if not s:
        return "UNKNOWN"
    return s


def status_color(status: str) -> int:
    """Return color pair ID for a UPS status string."""
    s = status.upper()
    if "FSD" in s or "LB" in s:
        return C_STATUS_CRIT
    if "OB" in s:
        if "DISCHRG" in s:
            return C_STATUS_CRIT
        return C_STATUS_OB
    if "OL" in s or "CHRG" in s:
        return C_STATUS_OK
    return C_STATUS_UNK


def status_attr(status: str) -> int:
    """Return curses attribute for a status badge."""
    sc = status_color(status)
    attr = curses.color_pair(sc) | curses.A_BOLD
    s = status.upper()
    if "OB" in s or "FSD" in s or "LB" in s:
        attr |= curses.A_BLINK
    return attr


# ==============================================================================
# DATA COLLECTION
# ==============================================================================

def collect_group_data(group: UPSGroupConfig, config: Config) -> Dict:
    """Collect display data for one UPS group."""
    label = group.ups.label
    name = group.ups.name

    state_path = state_file_path_for(group, config)
    state = parse_state_file(state_path)

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

    return {
        "label": label, "name": name, "is_local": group.is_local,
        "state": state,
        "resources": ", ".join(res_parts) if res_parts else "none",
    }


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


def render_config_panel(win, y_start: int, y_end: int, width: int,
                         groups_data: List[Dict]):
    """Render the config/status panel with gray background, edge to edge."""
    gray_attr = curses.color_pair(C_GRAY_BG)
    bold_attr = gray_attr | curses.A_BOLD

    # Fill entire panel with gray background
    for row in range(y_start, y_end):
        fill_row(win, row, gray_attr)

    y = y_start + 1  # top padding
    for i, data in enumerate(groups_data):
        if y >= y_end - 1:
            break

        state = data["state"]

        # Group name line
        header = f"   {data['label']}"
        if data["name"] != data["label"]:
            header += f"  ({data['name']})"
        if data["is_local"]:
            header += "  [is_local]"
        safe_addstr(win, y, 0, header, bold_attr)

        # Status badge (right-aligned, highlighted background)
        if state:
            status_str = state.get("STATUS", "?")
            status_label = f"  {human_status(status_str)}  "
            sa = status_attr(status_str)
            sx = max(0, width - len(status_label) - 3)
            safe_addstr(win, y, sx, status_label, sa)
        else:
            label = "  daemon not running  "
            sx = max(0, width - len(label) - 3)
            safe_addstr(win, y, sx, label,
                        curses.color_pair(C_STATUS_UNK) | curses.A_BOLD)
        y += 1
        if y >= y_end:
            break

        # Data line: values bold, labels regular
        if state:
            battery = state.get("BATTERY", "?")
            runtime = format_runtime(state.get("RUNTIME", "?"))
            load = state.get("LOAD", "?")
            input_v = state.get("INPUT_VOLTAGE", "?")
            output_v = state.get("OUTPUT_VOLTAGE", "?")
            line = (f"   Battery: {battery}% ({runtime})  Load: {load}%  "
                    f"Input: {input_v}V  Output: {output_v}V")
            safe_addstr(win, y, 0, line, bold_attr)
        else:
            safe_addstr(win, y, 0, "   No data available", gray_attr)
        y += 1
        if y >= y_end:
            break

        # Timestamp
        if state:
            ts = state.get("TIMESTAMP", "")
            safe_addstr(win, y, 0, f"   Last update: {ts}", gray_attr)
        y += 1
        if y >= y_end:
            break

        # Resources
        safe_addstr(win, y, 0, f"   Resources: {data['resources']}", gray_attr)
        y += 1

        # Spacing between groups
        if i < len(groups_data) - 1 and y < y_end:
            y += 1


def render_logs_panel(win, y_start: int, y_end: int, width: int,
                       events: List[str], show_more: bool,
                       *, graph_mode: str = "off", time_range: str = "1h",
                       ups_index: int = 0, ups_total: int = 1):
    """Render the logs panel with yellow/gold background, edge to edge.

    The bottom-row key hints reflect the *current* graph mode, time
    range, and (in multi-UPS) the active UPS index. Static hints would
    leave operators guessing what state the cycle keys are in.
    """
    gold_attr = curses.color_pair(C_GOLD_BG)
    gold_bold = gold_attr | curses.A_BOLD
    key_attr = curses.color_pair(C_GOLD_KEY) | curses.A_BOLD
    dim_attr = curses.color_pair(C_GOLD_DIM)

    # Fill entire panel with gold background
    for row in range(y_start, y_end):
        fill_row(win, row, gold_attr)

    y = y_start + 1  # top padding

    # Title (bold)
    safe_addstr(win, y, 0, "   Recent Events", gold_bold)
    y += 1

    if not events:
        safe_addstr(win, y, 0, "   (no recent events)", gold_attr)
        y += 1
    else:
        footer_lines = 2
        available = y_end - y - footer_lines
        if not show_more:
            display_events = events[-min(len(events), max(3, available)):] if available > 0 else []
        else:
            display_events = events[-max(1, available):]

        for event in display_events:
            if y >= y_end - footer_lines:
                break
            # Account for the right-edge gutter and the leading 3-space
            # indent. Use display-cell width (handles emoji + CJK) so
            # lines never spill past the panel edge.
            max_cells = max(0, width - 4)
            display = f"   {event}"
            if display_width(display) > max_cells:
                # Reserve 2 cells for the trailing ellipsis.
                display = truncate_to_width(display, max_cells - 2) + ".."
            # Pad to full row width with gold-bg spaces so the line
            # overwrites every cell of the row, not just where the text
            # ends. Mobile SSH clients often render emoji at a different
            # cell width than display_width predicts; without this pad,
            # any miscount leaves cells with stale or unpainted bg.
            pad_cells = max(0, width - display_width(display))
            safe_addstr(win, y, 0, display + (" " * pad_cells), gold_attr)
            y += 1

    # Key hints at the bottom of the gold panel.
    # G/T/U descriptions interpolate the current cycle state so an
    # operator can see at a glance "next press of T moves from 1h to 6h"
    # without having to remember the cycle order.
    hint_y = y_end - 1
    x = 2
    ups_descr = (f"UPS: {ups_index + 1}/{ups_total}" if ups_total > 1
                 else "UPS")
    hints = (
        ("<Q>", "Quit"),
        ("<R>", "Refresh"),
        ("<M>", "More logs"),
        ("<G>", f"Graph: {graph_mode}"),
        ("<T>", f"Time: {time_range}"),
        ("<U>", ups_descr),
    )
    for label, descr in hints:
        if x + len(label) + len(descr) + 6 > width:
            break  # ran out of horizontal space; skip remaining hints
        safe_addstr(win, hint_y, x, f" {label} ", key_attr)
        x += len(label) + 2
        safe_addstr(win, hint_y, x, f" {descr}   ", dim_attr)
        x += len(descr) + 4


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
    y_min = cfg_y_min if cfg_y_min is not None else obs_min
    y_max = cfg_y_max if cfg_y_max is not None else obs_max
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
        from eneru.utils import format_seconds
        footer = (f"   data: {format_seconds(actual_span)} "
                  f"of {format_seconds(seconds)} requested")
        safe_addstr(stdscr, y_end - 1, 0, footer, gray_attr)


def run_tui(config: Config, interval: int = 5):
    """Run the curses TUI dashboard."""
    def _main(stdscr):
        init_colors()
        curses.curs_set(0)
        stdscr.timeout(interval * 1000)
        stdscr.bkgd(' ', curses.color_pair(C_BORDER))

        show_more = False
        max_log_events = 8
        graph_mode = "off"           # G key cycles through GRAPH_MODES
        time_range = "1h"            # T key cycles through TIME_RANGES
        ups_index = 0                # U key cycles which UPS the graph shows

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

            # Calculate panel split: config panel gets what it needs,
            # logs panel gets the rest
            groups_needed = 0
            for group in config.ups_groups:
                groups_needed += 5  # 4 data lines + 1 spacing
            groups_needed = max(groups_needed - 1, 4)
            groups_needed += 2  # top + bottom padding

            # Config panel starts at row 1, ends before logs panel
            config_start = 1
            config_end = min(config_start + groups_needed, height - 8)
            config_end = max(config_end, 6)

            # Optional graph panel between config and logs (when graph_mode != off)
            graph_start = config_end + 1
            graph_end = graph_start
            if graph_mode != "off" and config.ups_groups:
                graph_end = graph_start + max(5, (height - config_end) // 2)
                graph_end = min(graph_end, height - 6)

            # Black spacer row between panels
            spacer = config_end
            fill_row(stdscr, spacer, curses.color_pair(C_BORDER))

            # Logs panel fills the rest (after spacer / graph)
            logs_start = (graph_end + 1) if graph_end > graph_start else config_end + 1
            logs_end = height

            # Collect data. Prefer the SQLite events tier; fall back to the
            # log-tail parser when no DB is present (single-UPS pip installs
            # without /var/lib/eneru, fresh installs before first poll, etc.).
            # Also push a fresh state-file snapshot into each group's live
            # buffer so the graph panel can blend SQLite + post-flush
            # samples (spec 2.13 -- bridges the 0-10s flush gap).
            groups_data = []
            for g in config.ups_groups:
                groups_data.append(collect_group_data(g, config))
                update_live_buffer(g, config)
            log_events = query_events_for_display(config, EVENTS_TIME_WINDOW,
                                                  max_events=50 if show_more else max_log_events)
            if not log_events:
                log_events = parse_log_events(
                    config.logging.file or "",
                    max_events=50 if show_more else max_log_events,
                )

            # Render panels edge-to-edge
            render_config_panel(stdscr, config_start, config_end, width,
                                groups_data)
            if graph_mode != "off" and graph_end > graph_start and config.ups_groups:
                ups_index = max(0, min(ups_index, len(config.ups_groups) - 1))
                render_graph_panel(
                    stdscr, graph_start, graph_end, width,
                    config, config.ups_groups[ups_index],
                    graph_mode, time_range,
                )
                # Spacer between graph and logs
                fill_row(stdscr, graph_end, curses.color_pair(C_BORDER))
            render_logs_panel(stdscr, logs_start, logs_end, width,
                              log_events, show_more,
                              graph_mode=graph_mode,
                              time_range=time_range,
                              ups_index=ups_index,
                              ups_total=len(config.ups_groups) or 1)

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
            elif key == ord('r'):
                continue
            elif key in (ord('m'), ord('M')):
                show_more = not show_more
            elif key in (ord('g'), ord('G')):
                graph_mode = cycle(GRAPH_MODES, graph_mode)
            elif key in (ord('t'), ord('T')):
                time_range = cycle(TIME_RANGES, time_range)
            elif key in (ord('u'), ord('U')):
                if config.ups_groups:
                    ups_index = (ups_index + 1) % len(config.ups_groups)

    curses.wrapper(_main)


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
    Used by ``run_once --graph`` and re-used by the curses panel.
    """
    seconds = TIME_RANGE_SECONDS.get(time_range, 3600)
    series = query_metric_series(config, group, metric, seconds)
    info = METRIC_INFO.get(metric)
    if info is None:
        return [f"(unknown metric: {metric})"]
    _, unit, y_min, y_max, _ = info
    # Reconstruct the axis label users got pre-v5.1.0 ("0-100%" /
    # "seconds" / "V") so existing --once output stays stable; the live
    # TUI uses richer labels (see render_graph_panel).
    if y_min is not None and y_max is not None:
        y_axis_label = f"{int(y_min)}-{int(y_max)}{unit}"
    else:
        y_axis_label = unit or "value"
    title = f"{metric} -- last {time_range}  ({y_axis_label})"
    if not series:
        return [
            title,
            "(no data)",
        ]
    values = [v for _, v in series]
    rows = BrailleGraph.plot(
        values,
        width=width,
        height=height,
        y_min=y_min,
        y_max=y_max,
        force_fallback=force_fallback,
    )
    return [title] + rows + [f"y-axis: {y_axis_label}"]


def run_once(config: Config, *, graph_metric: Optional[str] = None,
             time_range: str = "1h", events_only: bool = False):
    """Print a status snapshot to stdout and exit.

    With ``events_only=True`` the status / resource summary and graph
    block are skipped -- only the events list (from SQLite if available,
    otherwise the log tail) is printed. Useful for scripts and CI.
    """
    if events_only:
        seconds = TIME_RANGE_SECONDS.get(time_range, 24 * 3600)
        events = query_events_for_display(config, seconds, max_events=50)
        if not events:
            events = parse_log_events(config.logging.file or "", max_events=50)
        if events:
            for line in events:
                print(line)
        else:
            print("(no events)")
        return

    print(f"Eneru v{__version__}")
    group_count = len(config.ups_groups)
    if group_count > 1:
        print(f"Mode: multi-UPS ({group_count} groups)")
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()

    for i, group in enumerate(config.ups_groups):
        data = collect_group_data(group, config)
        header = data["label"]
        if data["name"] != data["label"]:
            header += f"  ({data['name']})"
        if data["is_local"]:
            header += "  [is_local]"

        state = data["state"]
        if state:
            header += f"  --  Status: {state.get('STATUS', '?')}"
        else:
            header += "  --  daemon not running"
        print(header)

        if state:
            battery = state.get("BATTERY", "?")
            runtime = format_runtime(state.get("RUNTIME", "?"))
            load = state.get("LOAD", "?")
            input_v = state.get("INPUT_VOLTAGE", "?")
            output_v = state.get("OUTPUT_VOLTAGE", "?")
            ts = state.get("TIMESTAMP", "?")
            print(f"  Battery: {battery}% ({runtime})  Load: {load}%  "
                  f"Input: {input_v}V  Output: {output_v}V")
            print(f"  Last update: {ts}")
        else:
            print("  No data available (daemon not running or no state file)")
        print(f"  Resources: {data['resources']}")
        if i < group_count - 1:
            print()

    seconds = TIME_RANGE_SECONDS.get(time_range, 24 * 3600)
    events = query_events_for_display(config, seconds, max_events=10)
    if not events:
        events = parse_log_events(config.logging.file or "", max_events=10)
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
