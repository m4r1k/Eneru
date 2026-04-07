"""Curses-based TUI dashboard for Eneru (eneru monitor).

Reads UPS data from daemon state files -- no direct NUT polling.
Two-panel layout:
  - Top panel (gray background, white text): UPS config/status
  - Bottom panel (yellow/gold background, black text): event logs + key hints
"""

import curses
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, List

from eneru.version import __version__
from eneru.config import Config, UPSGroupConfig


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
    """Read recent power events from the log file tail."""
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

    if config.multi_ups:
        sanitized = name.replace("@", "-").replace(":", "-").replace("/", "-")
        state_path = Path(config.logging.state_file + f".{sanitized}")
    else:
        state_path = Path(config.logging.state_file)

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

def safe_addstr(win, y: int, x: int, text: str, attr: int = 0):
    """Write string to window, clipping to avoid curses errors."""
    max_y, max_x = win.getmaxyx()
    if y < 0 or y >= max_y or x >= max_x:
        return
    available = max_x - x - 1
    if available <= 0:
        return
    try:
        win.addnstr(y, x, text, available, attr)
    except curses.error:
        pass


def fill_row(win, y: int, attr: int):
    """Fill an entire row with a background color, edge to edge."""
    max_y, max_x = win.getmaxyx()
    if y < 0 or y >= max_y:
        return
    try:
        win.addnstr(y, 0, " " * (max_x - 1), max_x - 1, attr)
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
                       events: List[str], show_more: bool):
    """Render the logs panel with yellow/gold background, edge to edge."""
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
            # Truncate to fit screen. Conservative for emoji double-width.
            max_text = width - 6
            display = f"   {event}"
            if len(display) > max_text:
                display = display[:max_text - 2] + ".."
            safe_addstr(win, y, 0, display, gold_attr)
            y += 1

    # Key hints at the bottom of the gold panel
    hint_y = y_end - 1
    x = 2
    safe_addstr(win, hint_y, x, " <Q> ", key_attr)
    x += 5
    safe_addstr(win, hint_y, x, " Quit   ", dim_attr)
    x += 8
    safe_addstr(win, hint_y, x, " <R> ", key_attr)
    x += 5
    safe_addstr(win, hint_y, x, " Refresh   ", dim_attr)
    x += 11
    safe_addstr(win, hint_y, x, " <M> ", key_attr)
    x += 5
    safe_addstr(win, hint_y, x, " More logs", dim_attr)


# ==============================================================================
# MAIN TUI LOOP
# ==============================================================================

def run_tui(config: Config, interval: int = 5):
    """Run the curses TUI dashboard."""
    def _main(stdscr):
        init_colors()
        curses.curs_set(0)
        stdscr.timeout(interval * 1000)
        stdscr.bkgd(' ', curses.color_pair(C_BORDER))

        show_more = False
        max_log_events = 8

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

            # Black spacer row between panels
            spacer = config_end
            fill_row(stdscr, spacer, curses.color_pair(C_BORDER))

            # Logs panel fills the rest (after spacer)
            logs_start = config_end + 1
            logs_end = height

            # Collect data
            groups_data = [collect_group_data(g, config) for g in config.ups_groups]
            log_events = parse_log_events(
                config.logging.file or "",
                max_events=50 if show_more else max_log_events,
            )

            # Render panels edge-to-edge
            render_config_panel(stdscr, config_start, config_end, width,
                                groups_data)
            render_logs_panel(stdscr, logs_start, logs_end, width,
                              log_events, show_more)

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

    curses.wrapper(_main)


# ==============================================================================
# --once MODE (no curses, stdout)
# ==============================================================================

def run_once(config: Config):
    """Print a status snapshot to stdout and exit."""
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

    log_path = config.logging.file or ""
    events = parse_log_events(log_path, max_events=10)
    if events:
        print()
        print("Recent Events:")
        for event in events:
            print(f"  {event}")
