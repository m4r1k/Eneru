"""Tests for TUI dashboard (eneru monitor)."""

import curses
import pytest
import tempfile
import os
import time
from pathlib import Path
from io import StringIO
from unittest.mock import patch, MagicMock

from eneru import Config, UPSConfig, UPSGroupConfig, LoggingConfig
from eneru.tui import (
    display_width,
    fill_row,
    parse_state_file,
    parse_log_events,
    human_status,
    status_color,
    status_attr,
    collect_group_data,
    init_colors,
    render_config_panel,
    render_logs_panel,
    run_once,
    C_STATUS_OK, C_STATUS_OB, C_STATUS_CRIT, C_STATUS_UNK,
)


class _FakeWin:
    """Minimal stand-in for a curses window that records writes per cell.

    Cells are stored as (char, attr); reads via ``cells[(y, x)]``.
    ``addnstr`` raises curses.error when it would write into the
    bottom-right cell (mirrors real curses behavior) so we can verify
    the workaround in fill_row actually fires.
    """

    def __init__(self, height: int, width: int):
        self.height = height
        self.width = width
        self.cells: dict = {}

    def getmaxyx(self):
        return (self.height, self.width)

    def addnstr(self, y, x, text, n, attr=0):
        if y < 0 or y >= self.height or x < 0:
            raise curses.error("out of bounds")
        # Real curses raises if the write would advance the cursor past
        # the bottom-right corner.
        end_x = x + min(len(text), n)
        if y == self.height - 1 and end_x >= self.width:
            raise curses.error("addnstr would advance past bottom-right")
        for i, ch in enumerate(text[:n]):
            if x + i >= self.width:
                break
            self.cells[(y, x + i)] = (ch, attr)

    def insch(self, y, x, ch, attr=0):
        if y < 0 or y >= self.height or x < 0 or x >= self.width:
            raise curses.error("insch out of bounds")
        self.cells[(y, x)] = (chr(ch) if isinstance(ch, int) else ch, attr)

    def chgat(self, *args, **kwargs):
        pass

    def attrs_in_row(self, y: int) -> set:
        return {self.cells.get((y, x), (None, None))[1]
                for x in range(self.width)}


class _FakeTuiScreen(_FakeWin):
    """Curses stdscr stand-in for exercising the interactive loop."""

    def __init__(self, height: int, width: int, keys):
        super().__init__(height, width)
        self.keys = list(keys)
        self.timeout_value = None
        self.background = None
        self.keypad_enabled = None
        self.refreshes = 0
        self.moves = []

    def erase(self):
        self.cells.clear()

    def timeout(self, value):
        self.timeout_value = value

    def bkgd(self, ch, attr=0):
        self.background = (ch, attr)

    def keypad(self, enabled):
        self.keypad_enabled = enabled

    def refresh(self):
        self.refreshes += 1

    def getch(self):
        if self.keys:
            return self.keys.pop(0)
        return ord("q")

    def move(self, y, x):
        self.moves.append((y, x))
        if y < 0 or y >= self.height or x < 0 or x >= self.width:
            raise curses.error("move out of bounds")


class TestFillRow:
    """Tests for the edge-to-edge background fill helper."""

    @pytest.mark.unit
    def test_fill_row_paints_every_column(self):
        """fill_row must paint columns 0..width-1 inclusive (no black strip).

        Regression: the previous implementation wrote ``max_x - 1`` chars
        and left the rightmost column unpainted, producing a thin dark
        vertical strip on the right edge of the gold events panel.
        """
        win = _FakeWin(height=20, width=80)
        attr = 0xAB  # arbitrary non-zero attr to detect "not painted"
        fill_row(win, y=5, attr=attr)
        for x in range(80):
            painted = win.cells.get((5, x))
            assert painted is not None, f"column {x} was not painted"
            assert painted[1] == attr, f"column {x} has wrong attr"

    @pytest.mark.unit
    def test_fill_row_handles_bottom_right_cell(self):
        """fill_row must not crash on the very last screen row."""
        win = _FakeWin(height=10, width=40)
        # Bottom-right cell would crash a naive addnstr.
        fill_row(win, y=9, attr=0x42)
        # And must still paint that last column via insch.
        assert win.cells.get((9, 39)) == (" ", 0x42)


class TestEventsTimescaleDecoupled:
    """Events panel pulls every event and ignores the graph timescale."""

    @pytest.mark.unit
    def test_events_query_passes_no_time_window(self):
        """``query_events_for_display`` must be called WITHOUT a
        time_range_seconds argument (or with None), so the panel always
        sees every event in the SQLite store. Pressing T to cycle the
        graph timescale must not feed a dynamic window in here.
        """
        import inspect
        import re as _re
        from eneru import tui as tui_mod

        src = inspect.getsource(tui_mod.run_tui)
        # The call must NOT pass TIME_RANGE_SECONDS or any positional
        # time-range argument; only `config` and the keyword `max_events`.
        assert "TIME_RANGE_SECONDS.get(time_range" not in src, (
            "run_tui re-introduces a dynamic time-range window for events; "
            "the panel must scan the full events table."
        )
        # Confirm the call site uses keyword max_events (no positional
        # second argument that could become a window).
        call_re = _re.compile(
            r"query_events_for_display\s*\(\s*config\s*,\s*max_events\s*=",
        )
        assert call_re.search(src), (
            "run_tui must call query_events_for_display(config, max_events=...) "
            "with no positional time-range argument."
        )


class TestKeypadEnabled:
    """5.1.1 (cubic P1): the curses TUI must enable keypad translation
    or arrow / page / home / end keys arrive as raw escape sequences and
    the leading ESC byte hits the quit branch instead of scrolling."""

    @pytest.mark.unit
    def test_run_tui_enables_keypad_mode(self):
        import inspect
        import re as _re
        from eneru import tui as tui_mod

        src = inspect.getsource(tui_mod.run_tui)
        # `stdscr.keypad(True)` must be called before the input loop
        # binds curses.KEY_* constants; without it those constants are
        # never delivered (curses returns the raw escape sequence).
        assert _re.search(r"stdscr\.keypad\s*\(\s*True\s*\)", src), (
            "run_tui must call stdscr.keypad(True) so KEY_UP/KEY_DOWN/"
            "KEY_PPAGE/KEY_NPAGE/KEY_HOME/KEY_END are delivered as the "
            "expected curses key codes."
        )


class TestGhosttyTerminfoFallback:
    """Ghostty can advertise xterm-ghostty before host terminfo is installed."""

    @pytest.mark.unit
    def test_xterm_ghostty_missing_terminfo_retries_with_xterm_256color(self):
        from eneru import tui as tui_mod

        config = Config()
        with patch.dict(os.environ, {"TERM": "xterm-ghostty"}):
            with patch.object(tui_mod.curses, "wrapper") as wrapper:
                wrapper.side_effect = [
                    curses.error("setupterm: could not find terminal"),
                    None,
                ]

                tui_mod.run_tui(config)

                assert wrapper.call_count == 2
                assert os.environ["TERM"] == "xterm-ghostty"

    @pytest.mark.unit
    def test_non_ghostty_curses_error_is_not_retried(self):
        from eneru import tui as tui_mod

        config = Config()
        with patch.dict(os.environ, {"TERM": "ansi"}):
            with patch.object(tui_mod.curses, "wrapper") as wrapper:
                wrapper.side_effect = curses.error(
                    "setupterm: could not find terminal"
                )

                with pytest.raises(curses.error):
                    tui_mod.run_tui(config)

                assert wrapper.call_count == 1


class TestRunTuiLoop:
    """Exercise the curses event loop without requiring a real terminal."""

    def _group_data(self, group, _config):
        return {
            "label": group.ups.label,
            "name": group.ups.name,
            "is_local": group.is_local,
            "state": {
                "STATUS": "OL",
                "BATTERY": "98",
                "RUNTIME": "3600",
                "LOAD": "12",
                "INPUT_VOLTAGE": "120.1",
                "OUTPUT_VOLTAGE": "120.0",
                "TIMESTAMP": "2026-05-15 12:00:00",
            },
            "resources": "VMs, containers",
            "remote_health_summary": "1 healthy",
        }

    @pytest.mark.unit
    def test_run_tui_handles_modes_navigation_and_scrolling(self):
        from eneru import tui as tui_mod

        config = Config(
            ups_groups=[
                UPSGroupConfig(
                    ups=UPSConfig(
                        name="ups-a@localhost",
                        display_name="Rack A",
                    ),
                    is_local=True,
                ),
                UPSGroupConfig(
                    ups=UPSConfig(
                        name="ups-b@localhost",
                        display_name="Rack B",
                    ),
                    is_local=False,
                ),
            ],
        )
        config.remote_health.enabled = True
        screen = _FakeTuiScreen(
            height=30,
            width=120,
            keys=[
                ord("g"),
                ord("t"),
                ord("u"),
                ord("v"),
                curses.KEY_UP,
                curses.KEY_NPAGE,
                curses.KEY_END,
                ord("m"),
                ord("r"),
                ord("q"),
            ],
        )
        events = [
            f"12:{i:02d}:00  POWER EVENT: battery event {i}"
            for i in range(40)
        ]

        def wrapper(callback):
            callback(screen)

        with patch.object(tui_mod.curses, "wrapper", side_effect=wrapper), \
             patch.object(tui_mod.curses, "COLORS", 256, create=True), \
             patch.object(tui_mod.curses, "start_color", lambda: None), \
             patch.object(tui_mod.curses, "init_pair", lambda *args: None), \
             patch.object(tui_mod.curses, "color_pair", lambda n: n), \
             patch.object(tui_mod.curses, "curs_set", lambda _value: None), \
             patch.object(tui_mod, "collect_group_data",
                          side_effect=self._group_data) as collect, \
             patch.object(tui_mod, "update_live_buffer") as update_buffer, \
             patch.object(tui_mod, "query_events_for_display",
                          return_value=events) as query_events, \
             patch.object(tui_mod, "render_graph_panel") as render_graph:
            tui_mod.run_tui(
                config,
                interval=2,
                initial_graph="charge",
                initial_time_range="bad-range",
                initial_ups_index=99,
                verbose=True,
            )

        assert screen.timeout_value == 2000
        assert screen.keypad_enabled is True
        assert screen.background == (" ", tui_mod.C_BORDER)
        assert screen.refreshes == 10
        assert screen.moves
        assert collect.call_count >= 2 * screen.refreshes
        assert update_buffer.call_count >= 2 * screen.refreshes
        assert query_events.call_args_list[0].kwargs["verbosity"] == (
            tui_mod.EVENTS_VERBOSITY_DIAGNOSTICS
        )
        assert any(
            call.kwargs["max_events"] == 500
            for call in query_events.call_args_list
        )
        assert any(
            call.args[7] == "1h"
            for call in render_graph.call_args_list
        )
        assert any(
            call.args[5].ups.name == "ups-b@localhost"
            for call in render_graph.call_args_list
        )

    @pytest.mark.unit
    def test_run_tui_handles_small_terminal_until_quit(self):
        from eneru import tui as tui_mod

        config = Config()
        screen = _FakeTuiScreen(
            height=8,
            width=40,
            keys=[ord("x"), ord("q")],
        )

        def wrapper(callback):
            callback(screen)

        with patch.object(tui_mod.curses, "wrapper", side_effect=wrapper), \
             patch.object(tui_mod.curses, "COLORS", 256, create=True), \
             patch.object(tui_mod.curses, "start_color", lambda: None), \
             patch.object(tui_mod.curses, "init_pair", lambda *args: None), \
             patch.object(tui_mod.curses, "color_pair", lambda n: n), \
             patch.object(tui_mod.curses, "curs_set", lambda _value: None):
            tui_mod.run_tui(config)

        row0 = "".join(
            screen.cells.get((0, x), (" ", 0))[0]
            for x in range(screen.width)
        )
        assert "Terminal too small" in row0
        assert screen.refreshes == 2


class TestRemoteHealthSummary:
    """Remote health summary for the TUI resource panel."""

    @pytest.mark.unit
    def test_summarize_remote_health_counts_statuses(self):
        from eneru.tui import summarize_remote_health

        summary = summarize_remote_health([
            {"status": "HEALTHY"},
            {"status": "HEALTHY"},
            {"status": "FAILED"},
        ])

        assert "2 healthy" in summary
        assert "1 failed" in summary


class TestEventsVerbosityNormalize:
    @pytest.mark.unit
    def test_bool_values_map_to_power_and_diagnostics(self):
        from eneru.tui import (
            EVENTS_VERBOSITY_DIAGNOSTICS,
            EVENTS_VERBOSITY_POWER,
            _events_verbosity,
        )

        assert _events_verbosity(False) == EVENTS_VERBOSITY_POWER
        assert _events_verbosity(True) == EVENTS_VERBOSITY_DIAGNOSTICS

    @pytest.mark.unit
    def test_invalid_values_fall_back_to_power_tier(self):
        from eneru.tui import EVENTS_VERBOSITY_POWER, _events_verbosity

        assert _events_verbosity(None) == EVENTS_VERBOSITY_POWER
        assert _events_verbosity("bad") == EVENTS_VERBOSITY_POWER


class TestInitColors:
    """Color setup should stay deterministic across rich and basic terminals."""

    @pytest.mark.unit
    def test_init_colors_uses_256_color_palette(self, monkeypatch):
        calls = []
        monkeypatch.setattr(curses, "COLORS", 256, raising=False)
        monkeypatch.setattr(curses, "start_color", lambda: None)
        monkeypatch.setattr(curses, "init_pair",
                            lambda *args: calls.append(args))

        init_colors()

        assert (3, curses.COLOR_WHITE, 243) in calls
        assert (5, 16, 178) in calls
        assert (7, 241, 178) in calls

    @pytest.mark.unit
    def test_init_colors_falls_back_for_basic_terminals(self, monkeypatch):
        calls = []
        monkeypatch.setattr(curses, "COLORS", 8, raising=False)
        monkeypatch.setattr(curses, "start_color", lambda: None)
        monkeypatch.setattr(curses, "init_pair",
                            lambda *args: calls.append(args))

        init_colors()

        assert (3, curses.COLOR_WHITE, curses.COLOR_BLACK) in calls
        assert (5, curses.COLOR_BLACK, curses.COLOR_YELLOW) in calls
        assert (7, curses.COLOR_WHITE, curses.COLOR_YELLOW) in calls


class TestRenderConfigPanel:
    """Config panel rendering should cover missing daemon data and sidecars."""

    def _row_text(self, win, y: int) -> str:
        return "".join(
            win.cells.get((y, x), (" ", 0))[0]
            for x in range(win.width)
        ).rstrip()

    @pytest.mark.unit
    def test_render_config_panel_handles_missing_state(self):
        win = _FakeWin(height=12, width=80)
        groups_data = [{
            "label": "Rack UPS",
            "name": "ups-a@localhost",
            "is_local": False,
            "state": {},
            "resources": "remote servers",
        }]

        with patch.object(curses, "color_pair", lambda n: n):
            render_config_panel(win, 0, 8, 80, groups_data)

        assert "Rack UPS" in self._row_text(win, 1)
        assert "daemon not running" in self._row_text(win, 1)
        assert "No data available" in self._row_text(win, 2)
        assert "Resources: remote servers" in self._row_text(win, 4)

    @pytest.mark.unit
    def test_render_config_panel_includes_local_marker_and_remote_health(self):
        win = _FakeWin(height=14, width=100)
        groups_data = [{
            "label": "Rack UPS",
            "name": "ups-a@localhost",
            "is_local": True,
            "state": {
                "STATUS": "OL",
                "BATTERY": "98",
                "RUNTIME": "3600",
                "LOAD": "12",
                "INPUT_VOLTAGE": "120.1",
                "OUTPUT_VOLTAGE": "120.0",
                "TIMESTAMP": "2026-05-15 12:00:00",
            },
            "resources": "VMs, containers",
            "remote_health_summary": "1 failed",
        }]

        with patch.object(curses, "color_pair", lambda n: n):
            render_config_panel(win, 0, 9, 100, groups_data)

        assert "[is_local]" in self._row_text(win, 1)
        assert "ONLINE" in self._row_text(win, 1)
        assert "Battery: 98% (1h 0m)" in self._row_text(win, 2)
        assert "Last update: 2026-05-15 12:00:00" in self._row_text(win, 3)
        assert "Remote health: 1 failed" in self._row_text(win, 5)


class TestEventsScrollAutoPromote:
    """5.1.1 (CodeRabbit): scrolling toward older history while in
    normal-cap mode (8 rows) used to be a silent no-op because the
    cap matched the visible window. ↑ / PgUp / Home now auto-promote
    show_more=True so scroll has somewhere to scroll to."""

    @pytest.mark.unit
    def test_arrow_keys_auto_promote_show_more(self):
        import inspect
        from eneru import tui as tui_mod

        src = inspect.getsource(tui_mod.run_tui)
        # The branch must group KEY_UP / KEY_PPAGE / KEY_HOME together
        # and assign show_more = True before performing the scroll math.
        # Match the structural pattern rather than exact whitespace so
        # a re-format doesn't break the test.
        assert "curses.KEY_UP" in src
        assert "curses.KEY_PPAGE" in src
        assert "curses.KEY_HOME" in src
        # show_more flips to True inside the auto-promote branch.
        idx_up = src.find("curses.KEY_UP, curses.KEY_PPAGE, curses.KEY_HOME")
        assert idx_up >= 0, (
            "auto-promote branch must group KEY_UP / KEY_PPAGE / KEY_HOME "
            "into a single elif so show_more flips before scrolling"
        )
        following = src[idx_up:idx_up + 800]
        assert "show_more = True" in following, (
            "auto-promote branch must set show_more = True before the "
            "scroll math runs"
        )


class TestGraphPanelHeader:
    """Item 3: graph panel renders a now/min/max stat header with units."""

    @pytest.mark.unit
    def test_render_graph_panel_writes_stat_header(self):
        """The row right under the title must show 'now: X{unit}  min: Y{unit}  max: Z{unit}'."""
        from eneru.tui import render_graph_panel
        from unittest.mock import MagicMock

        win = _FakeWin(height=20, width=120)
        # Build minimal config + group with a stub stats DB by mocking
        # the series query directly.
        cfg = MagicMock()
        cfg.statistics.db_directory = "/tmp"
        cfg.multi_ups = False
        group = MagicMock()
        group.ups.label = "TestUPS"
        group.ups.name = "TestUPS@localhost"

        with patch.object(curses, "color_pair", lambda n: n), \
             patch("eneru.tui.query_metric_series",
                   return_value=[(1000, 95.0), (1100, 98.0), (1200, 100.0)]):
            render_graph_panel(
                win, y_start=0, y_end=10, width=120,
                config=cfg, group=group,
                graph_mode="charge", time_range="1h",
            )

        # Reconstruct the stat row (y=1) from the recorded cells.
        row1 = "".join(
            win.cells.get((1, x), (" ", 0))[0] for x in range(120)
        )
        assert "now: 100%" in row1
        assert "min: 95%" in row1
        assert "max: 100%" in row1

    @pytest.mark.unit
    def test_render_graph_panel_voltage_uses_observed_bounds(self):
        """For voltage (no configured y_min/y_max), the stat header must
        reflect the actually observed range, not '0' or 'None'."""
        from eneru.tui import render_graph_panel
        from unittest.mock import MagicMock

        win = _FakeWin(height=20, width=120)
        cfg = MagicMock()
        cfg.statistics.db_directory = "/tmp"
        cfg.multi_ups = False
        group = MagicMock()
        group.ups.label = "TestUPS"
        group.ups.name = "TestUPS@localhost"

        with patch.object(curses, "color_pair", lambda n: n), \
             patch("eneru.tui.query_metric_series",
                   return_value=[(1000, 233.1), (1100, 234.5), (1200, 235.4)]):
            render_graph_panel(
                win, y_start=0, y_end=10, width=120,
                config=cfg, group=group,
                graph_mode="voltage", time_range="1h",
            )

        row1 = "".join(
            win.cells.get((1, x), (" ", 0))[0] for x in range(120)
        )
        assert "min: 233.1V" in row1
        assert "max: 235.4V" in row1
        assert "now: 235.4V" in row1

    @pytest.mark.unit
    def test_render_graph_panel_runtime_uses_human_format(self):
        """Runtime must show '45m 12s' style strings, not raw seconds."""
        from eneru.tui import render_graph_panel
        from unittest.mock import MagicMock

        win = _FakeWin(height=20, width=120)
        cfg = MagicMock()
        cfg.statistics.db_directory = "/tmp"
        cfg.multi_ups = False
        group = MagicMock()
        group.ups.label = "TestUPS"
        group.ups.name = "TestUPS@localhost"

        with patch.object(curses, "color_pair", lambda n: n), \
             patch("eneru.tui.query_metric_series",
                   return_value=[(1000, 1800.0), (1100, 2400.0), (1200, 2712.0)]):
            render_graph_panel(
                win, y_start=0, y_end=10, width=120,
                config=cfg, group=group,
                graph_mode="runtime", time_range="1h",
            )

        row1 = "".join(
            win.cells.get((1, x), (" ", 0))[0] for x in range(120)
        )
        # 2712s = 45m 12s; 1800s = 30m 0s
        assert "now: 45m 12s" in row1
        assert "min: 30m 0s" in row1


class TestEventsPanelRightEdge:
    """Tests for the events-panel right-edge artifact fix (item 2)."""

    @pytest.mark.unit
    def test_event_line_pads_to_full_width(self):
        """Every cell in an event row must be painted, even past the text.

        Regression: emoji and wide chars miscount in display_width vs.
        what the terminal actually renders, leaving stale cells visible
        on the right edge. Padding to full width with gold-bg spaces
        guarantees the row is fully repainted regardless of miscounts.
        """
        win = _FakeWin(height=20, width=80)
        # Short event with emoji -- display_width counts emoji as 2, so
        # the unpadded write would only cover ~40-50 cells.
        events = ["10:00:00  POWER EVENT: 🔋  battery low"]
        # curses.color_pair requires initscr(); mock it for headless tests.
        with patch.object(curses, "color_pair", lambda n: n):
            render_logs_panel(win, y_start=2, y_end=12, width=80,
                              events=events, show_more=False)
        # Find the row the event landed on (first row after the title
        # block). Per render_logs_panel: y_start + 1 (top pad) + 1 (title).
        event_row = 4
        # All 80 columns should be painted (some via fill_row, some via
        # the padded event write -- doesn't matter which, just no holes).
        for x in range(80):
            assert (event_row, x) in win.cells, (
                f"events row column {x} unpainted -- artifact would show here"
            )


class TestParseStateFile:
    """Tests for daemon state file parsing."""

    @pytest.mark.unit
    def test_valid_state_file(self, tmp_path):
        """Parse a valid state file."""
        state_file = tmp_path / "ups.state"
        state_file.write_text(
            "STATUS=OL CHRG\n"
            "BATTERY=100\n"
            "RUNTIME=1800\n"
            "LOAD=20\n"
            "INPUT_VOLTAGE=230.5\n"
            "OUTPUT_VOLTAGE=230.0\n"
            "TIMESTAMP=2026-04-07 15:00:00\n"
        )
        data = parse_state_file(state_file)
        assert data is not None
        assert data["STATUS"] == "OL CHRG"
        assert data["BATTERY"] == "100"
        assert data["RUNTIME"] == "1800"
        assert data["LOAD"] == "20"
        assert data["TIMESTAMP"] == "2026-04-07 15:00:00"

    @pytest.mark.unit
    def test_missing_file(self):
        """Missing file returns None."""
        assert parse_state_file(Path("/nonexistent/file")) is None

    @pytest.mark.unit
    def test_empty_file(self, tmp_path):
        """Empty file returns None."""
        state_file = tmp_path / "empty.state"
        state_file.write_text("")
        assert parse_state_file(state_file) is None


class TestParseLogEvents:
    """Tests for log event filtering."""

    @pytest.mark.unit
    def test_filters_power_events(self, tmp_path):
        """Only real power events pass the filter."""
        log_file = tmp_path / "test.log"
        log_file.write_text(
            "2026-04-07 10:00:00 - Normal log line\n"
            "2026-04-07 10:01:00 - POWER EVENT: CONNECTION_LOST\n"
            "2026-04-07 10:02:00 - Battery at 50%\n"
            "2026-04-07 10:03:00 - Enabled features: VMs, Containers\n"
            "2026-04-07 10:04:00 - Status changed: OL -> OB\n"
            "2026-04-07 10:05:00 - Checking initial connection\n"
            "2026-04-07 10:06:00 - Initial connection successful\n"
        )
        events = parse_log_events(str(log_file))
        assert len(events) == 2
        assert "POWER EVENT" in events[0]
        assert "Status changed" in events[1]

    @pytest.mark.unit
    def test_excludes_startup_noise(self, tmp_path):
        """Startup messages are excluded."""
        log_file = tmp_path / "test.log"
        log_file.write_text(
            "2026-04-07 10:00:00 - Eneru v5.0.0 starting - monitoring UPS\n"
            "2026-04-07 10:00:00 - Enabled features: VMs\n"
            "2026-04-07 10:00:00 - Checking initial connection\n"
            "2026-04-07 10:00:00 - Initial connection successful\n"
            "2026-04-07 10:00:00 - Eneru v5.0.0 Started\n"
        )
        events = parse_log_events(str(log_file))
        assert len(events) == 0

    @pytest.mark.unit
    def test_missing_log(self):
        """Missing log file returns empty list."""
        assert parse_log_events("/nonexistent/log") == []

    @pytest.mark.unit
    def test_max_events_limit(self, tmp_path):
        """Events are limited to max_events."""
        log_file = tmp_path / "test.log"
        lines = [f"2026-04-07 10:{i:02d}:00 - POWER EVENT: test {i}\n" for i in range(20)]
        log_file.write_text("".join(lines))
        events = parse_log_events(str(log_file), max_events=5)
        assert len(events) == 5


class TestHumanStatusPureLogic:
    """Tests for NUT status to human-readable conversion."""

    @pytest.mark.unit
    def test_ol_chrg(self):
        assert human_status("OL CHRG") == "ONLINE - CHARGING"

    @pytest.mark.unit
    def test_ol(self):
        assert human_status("OL") == "ONLINE"

    @pytest.mark.unit
    def test_ob(self):
        assert human_status("OB") == "ON BATTERY"

    @pytest.mark.unit
    def test_ob_dischrg(self):
        assert human_status("OB DISCHRG") == "ON BATTERY - DISCHARGING"

    @pytest.mark.unit
    def test_ob_lb(self):
        assert human_status("OB LB") == "ON BATTERY - LOW"

    @pytest.mark.unit
    def test_fsd(self):
        assert human_status("FSD") == "FORCED SHUTDOWN"

    @pytest.mark.unit
    def test_empty(self):
        assert human_status("") == "UNKNOWN"

    @pytest.mark.unit
    def test_unknown_passthrough(self):
        assert human_status("SOMETHING ELSE") == "SOMETHING ELSE"


class TestStatusColorPureLogic:
    """Tests for status color pair selection."""

    @pytest.mark.unit
    def test_ol_chrg_is_ok(self):
        assert status_color("OL CHRG") == C_STATUS_OK

    @pytest.mark.unit
    def test_ob_is_ob(self):
        assert status_color("OB") == C_STATUS_OB

    @pytest.mark.unit
    def test_ob_dischrg_is_critical(self):
        assert status_color("OB DISCHRG") == C_STATUS_CRIT

    @pytest.mark.unit
    def test_fsd_is_critical(self):
        assert status_color("FSD") == C_STATUS_CRIT

    @pytest.mark.unit
    def test_empty_is_unknown(self):
        assert status_color("") == C_STATUS_UNK


class TestRunOnce:
    """Tests for --once mode output."""

    @pytest.mark.unit
    def test_once_single_ups(self, tmp_path, capsys):
        """--once prints correct single-UPS snapshot."""
        state_file = tmp_path / "ups.state"
        state_file.write_text(
            "STATUS=OL CHRG\nBATTERY=100\nRUNTIME=1800\n"
            "LOAD=20\nINPUT_VOLTAGE=230.5\nOUTPUT_VOLTAGE=230.0\n"
            "TIMESTAMP=2026-04-07 15:00:00\n"
        )
        config = Config(
            ups_groups=[UPSGroupConfig(
                ups=UPSConfig(name="TestUPS@localhost"),
                is_local=True,
            )],
            logging=LoggingConfig(state_file=str(state_file)),
        )

        run_once(config)
        output = capsys.readouterr().out

        assert "Eneru v" in output
        assert "TestUPS@localhost" in output
        assert "is_local" in output
        assert "OL CHRG" in output
        assert "100%" in output

    @pytest.mark.unit
    def test_once_daemon_not_running(self, tmp_path, capsys):
        """--once shows 'daemon not running' when no state file."""
        config = Config(
            ups_groups=[UPSGroupConfig(
                ups=UPSConfig(name="TestUPS@localhost"),
                is_local=True,
            )],
            logging=LoggingConfig(state_file=str(tmp_path / "nonexistent")),
        )

        run_once(config)
        output = capsys.readouterr().out
        assert "daemon not running" in output

    @pytest.mark.unit
    def test_once_multi_ups(self, tmp_path, capsys):
        """--once shows multi-UPS mode header."""
        config = Config(
            ups_groups=[
                UPSGroupConfig(ups=UPSConfig(name="UPS1"), is_local=True),
                UPSGroupConfig(ups=UPSConfig(name="UPS2"), is_local=False),
            ],
            logging=LoggingConfig(state_file=str(tmp_path / "nonexistent")),
        )

        run_once(config)
        output = capsys.readouterr().out
        assert "multi-UPS" in output
        assert "2 groups" in output


# ===========================================================================
# Graph integration (Phase 2 -- TUI graphs)
# ===========================================================================

class TestTUIGraphCycle:
    """``cycle()`` helper used by the G/T/U keybindings."""

    @pytest.mark.unit
    def test_cycle_advances_one_step(self):
        from eneru.tui import cycle, GRAPH_MODES
        assert cycle(GRAPH_MODES, "off") == "charge"
        assert cycle(GRAPH_MODES, "charge") == "load"

    @pytest.mark.unit
    def test_cycle_wraps_around(self):
        from eneru.tui import cycle, GRAPH_MODES
        last = GRAPH_MODES[-1]
        assert cycle(GRAPH_MODES, last) == GRAPH_MODES[0]

    @pytest.mark.unit
    def test_cycle_unknown_value_resets_to_first(self):
        from eneru.tui import cycle, GRAPH_MODES
        assert cycle(GRAPH_MODES, "nonsense") == GRAPH_MODES[0]


class TestStatsDbPath:
    """Path computation must mirror MultiUPSCoordinator's sanitization."""

    @pytest.mark.unit
    def test_single_ups_uses_default_filename(self, tmp_path):
        from eneru.tui import stats_db_path_for
        from eneru import (
            Config, UPSConfig, UPSGroupConfig, StatsConfig,
        )
        config = Config(
            ups_groups=[UPSGroupConfig(ups=UPSConfig(name="UPS@host"))],
            statistics=StatsConfig(db_directory=str(tmp_path)),
        )
        path = stats_db_path_for(config.ups_groups[0], config)
        assert path == tmp_path / "default.db"

    @pytest.mark.unit
    def test_multi_ups_uses_sanitized_ups_name(self, tmp_path):
        from eneru.tui import stats_db_path_for
        from eneru import (
            Config, UPSConfig, UPSGroupConfig, StatsConfig,
        )
        config = Config(
            ups_groups=[
                UPSGroupConfig(ups=UPSConfig(name="UPS1@10.0.0.1:3493")),
                UPSGroupConfig(ups=UPSConfig(name="UPS2@10.0.0.2:3493")),
            ],
            statistics=StatsConfig(db_directory=str(tmp_path)),
        )
        path = stats_db_path_for(config.ups_groups[0], config)
        assert path == tmp_path / "UPS1-10.0.0.1-3493.db"


class TestLiveBufferBlending:
    """Spec 2.13: TUI blends SQLite history with a per-UPS live deque
    so the graph's right edge stays current between SQLite flushes."""

    def _config(self, tmp_path):
        from eneru import (
            Config, UPSConfig, UPSGroupConfig, StatsConfig,
            BehaviorConfig, LoggingConfig, NotificationsConfig,
            LocalShutdownConfig,
        )
        return Config(
            ups_groups=[UPSGroupConfig(
                ups=UPSConfig(name="TestUPS@localhost"),
                is_local=True,
            )],
            behavior=BehaviorConfig(dry_run=True),
            logging=LoggingConfig(
                state_file=str(tmp_path / "state"),
                battery_history_file=str(tmp_path / "history"),
                shutdown_flag_file=str(tmp_path / "flag"),
                file=None,
            ),
            notifications=NotificationsConfig(enabled=False),
            local_shutdown=LocalShutdownConfig(enabled=False),
            statistics=StatsConfig(db_directory=str(tmp_path)),
        )

    def _write_state_file(self, path: Path, charge: float, voltage: float):
        # Match UPSGroupMonitor._save_state's actual on-disk format:
        # uppercase KEY=value lines, NOT NUT's dotted lowercase.
        path.write_text(
            "STATUS=OL CHRG\n"
            f"BATTERY={charge}\n"
            "RUNTIME=1800\n"
            "LOAD=30\n"
            f"INPUT_VOLTAGE={voltage}\n"
            "OUTPUT_VOLTAGE=230\n"
        )

    @pytest.mark.unit
    def test_update_live_buffer_pushes_state_snapshot(self, tmp_path):
        from eneru.tui import (
            update_live_buffer, _live_buffers, clear_live_buffers,
            state_file_path_for, _buffer_key,
        )
        clear_live_buffers()
        config = self._config(tmp_path)
        group = config.ups_groups[0]
        self._write_state_file(state_file_path_for(group, config), 87.0, 231.5)

        update_live_buffer(group, config)
        buf = _live_buffers[_buffer_key(group, config)]
        assert len(buf) == 1
        ts, sample = buf[-1]
        assert sample["battery_charge"] == 87.0
        assert sample["input_voltage"] == 231.5

    @pytest.mark.unit
    def test_update_live_buffer_dedupes_within_same_second(self, tmp_path):
        from eneru.tui import (
            update_live_buffer, _live_buffers, clear_live_buffers,
            state_file_path_for, _buffer_key,
        )
        clear_live_buffers()
        config = self._config(tmp_path)
        group = config.ups_groups[0]
        sf = state_file_path_for(group, config)

        self._write_state_file(sf, 50.0, 230.0)
        update_live_buffer(group, config)
        # Same wall-clock second: second push must replace, not append.
        self._write_state_file(sf, 51.0, 230.0)
        update_live_buffer(group, config)

        buf = _live_buffers[_buffer_key(group, config)]
        assert len(buf) == 1
        assert buf[-1][1]["battery_charge"] == 51.0

    @pytest.mark.unit
    def test_update_live_buffer_no_state_file_is_noop(self, tmp_path):
        from eneru.tui import (
            update_live_buffer, _live_buffers, clear_live_buffers,
            _buffer_key,
        )
        clear_live_buffers()
        config = self._config(tmp_path)
        group = config.ups_groups[0]
        # No state file written.
        update_live_buffer(group, config)
        # No buffer created (or empty -- both acceptable).
        buf = _live_buffers.get(_buffer_key(group, config))
        assert buf is None or len(buf) == 0

    @pytest.mark.unit
    def test_query_metric_series_extends_sqlite_with_live_deque(
        self, tmp_path,
    ):
        from eneru import StatsStore
        from eneru.tui import (
            query_metric_series, stats_db_path_for, _live_buffer_for,
            clear_live_buffers,
        )
        clear_live_buffers()
        config = self._config(tmp_path)
        group = config.ups_groups[0]

        # SQLite tail at t-30s through t-21s (10 samples), then a gap.
        store = StatsStore(stats_db_path_for(group, config))
        store.open()
        try:
            now = int(time.time())
            for i in range(10):
                store.buffer_sample(
                    {"ups.status": "OL", "battery.charge": str(50 + i),
                     "battery.runtime": "1800", "ups.load": "30",
                     "input.voltage": "230", "output.voltage": "230"},
                    ts=now - 30 + i,
                )
            store.flush()
        finally:
            store.close()

        # Inject 3 live deque samples newer than the SQLite tail.
        buf = _live_buffer_for(group, config)
        for i in range(3):
            buf.append((now - 5 + i, {"battery_charge": 70.0 + i}))

        merged = query_metric_series(config, group, "charge", 60)
        # SQLite contributed 10, deque contributed 3 newer.
        assert len(merged) == 13
        # The deque samples must come last and be ordered.
        assert merged[-3:] == [
            (now - 5, 70.0), (now - 4, 71.0), (now - 3, 72.0),
        ]
        # SQLite block stays first and untouched.
        assert merged[0] == (now - 30, 50.0)

    @pytest.mark.unit
    def test_query_metric_series_dedupes_overlap_with_sqlite_tail(
        self, tmp_path,
    ):
        from eneru import StatsStore
        from eneru.tui import (
            query_metric_series, stats_db_path_for, _live_buffer_for,
            clear_live_buffers,
        )
        clear_live_buffers()
        config = self._config(tmp_path)
        group = config.ups_groups[0]
        store = StatsStore(stats_db_path_for(group, config))
        store.open()
        try:
            now = int(time.time())
            store.buffer_sample(
                {"ups.status": "OL", "battery.charge": "50",
                 "battery.runtime": "1800", "ups.load": "30",
                 "input.voltage": "230", "output.voltage": "230"},
                ts=now - 5,
            )
            store.flush()
        finally:
            store.close()

        # Buffer carries the same ts as the SQLite tail, plus 2 newer.
        buf = _live_buffer_for(group, config)
        buf.append((now - 5, {"battery_charge": 999.0}))   # duplicate ts
        buf.append((now - 3, {"battery_charge": 60.0}))
        buf.append((now - 1, {"battery_charge": 61.0}))

        merged = query_metric_series(config, group, "charge", 60)
        # 3 distinct timestamps, duplicate from buffer dropped.
        assert [ts for ts, _ in merged] == [now - 5, now - 3, now - 1]
        # SQLite value wins for the overlapping ts (50.0, not 999.0).
        assert merged[0] == (now - 5, 50.0)

    @pytest.mark.unit
    def test_live_buffers_are_per_ups(self, tmp_path):
        from eneru import (
            Config, UPSConfig, UPSGroupConfig, StatsConfig,
            BehaviorConfig, LoggingConfig, NotificationsConfig,
            LocalShutdownConfig,
        )
        from eneru.tui import (
            update_live_buffer, _live_buffers, clear_live_buffers,
            state_file_path_for, _buffer_key,
        )
        clear_live_buffers()
        config = Config(
            ups_groups=[
                UPSGroupConfig(ups=UPSConfig(name="UPS1@10.0.0.1:3493")),
                UPSGroupConfig(ups=UPSConfig(name="UPS2@10.0.0.2:3493")),
            ],
            behavior=BehaviorConfig(dry_run=True),
            logging=LoggingConfig(
                state_file=str(tmp_path / "state"),
                battery_history_file=str(tmp_path / "history"),
                shutdown_flag_file=str(tmp_path / "flag"),
                file=None,
            ),
            notifications=NotificationsConfig(enabled=False),
            local_shutdown=LocalShutdownConfig(enabled=False),
            statistics=StatsConfig(db_directory=str(tmp_path)),
        )

        g1, g2 = config.ups_groups[0], config.ups_groups[1]
        self._write_state_file(state_file_path_for(g1, config), 50.0, 230.0)
        self._write_state_file(state_file_path_for(g2, config), 75.0, 230.0)

        update_live_buffer(g1, config)
        update_live_buffer(g2, config)

        k1, k2 = _buffer_key(g1, config), _buffer_key(g2, config)
        assert k1 != k2
        assert _live_buffers[k1][-1][1]["battery_charge"] == 50.0
        assert _live_buffers[k2][-1][1]["battery_charge"] == 75.0


class TestDynamicFooter:
    """``render_logs_panel`` interpolates current cycle state into the
    G / T / U key hints (S2)."""

    def _stub_window(self):
        """Capture safe_addstr calls so we can assert footer content."""
        captured: list = []

        class _Win:
            def addstr(self, y, x, text, attr=0):
                captured.append((y, x, text, attr))
            def addnstr(self, y, x, text, n, attr=0):
                captured.append((y, x, text[:n], attr))
            def getmaxyx(self):
                return (24, 200)  # huge so all hints render

        return _Win(), captured

    @pytest.mark.unit
    def test_footer_shows_current_graph_mode_and_time_range(self):
        # render_logs_panel uses curses color pairs which require an
        # initialized terminal; patch curses to no-ops so this stays
        # a pure-Python test.
        from unittest.mock import patch
        with patch("eneru.tui.curses") as mc, \
             patch("eneru.tui.fill_row"), \
             patch("eneru.tui.safe_addstr") as sa:
            mc.color_pair.side_effect = lambda c: c
            mc.A_BOLD = 0
            from eneru.tui import render_logs_panel
            render_logs_panel(None, 0, 10, 200, ["evt1"], False,
                              graph_mode="charge", time_range="6h",
                              ups_index=0, ups_total=1)
        # safe_addstr was called for each hint label + descr.
        rendered = " ".join(call.args[3] for call in sa.call_args_list
                            if isinstance(call.args[3], str))
        assert "Graph: charge" in rendered
        assert "Time: 6h" in rendered
        # ups_total=1 means UPS hint stays generic.
        assert "UPS: " not in rendered

    @pytest.mark.unit
    def test_footer_shows_ups_index_when_multi_ups(self):
        from unittest.mock import patch
        with patch("eneru.tui.curses") as mc, \
             patch("eneru.tui.fill_row"), \
             patch("eneru.tui.safe_addstr") as sa:
            mc.color_pair.side_effect = lambda c: c
            mc.A_BOLD = 0
            from eneru.tui import render_logs_panel
            render_logs_panel(None, 0, 10, 200, ["evt1"], False,
                              graph_mode="off", time_range="1h",
                              ups_index=1, ups_total=3)
        rendered = " ".join(call.args[3] for call in sa.call_args_list
                            if isinstance(call.args[3], str))
        assert "UPS: 2/3" in rendered  # 1-indexed for humans

    @pytest.mark.unit
    def test_footer_truncates_when_terminal_too_narrow(self):
        # Narrow width => render_logs_panel must skip overflowing hints
        # rather than spilling past the right edge.
        from unittest.mock import patch
        with patch("eneru.tui.curses") as mc, \
             patch("eneru.tui.fill_row"), \
             patch("eneru.tui.safe_addstr") as sa:
            mc.color_pair.side_effect = lambda c: c
            mc.A_BOLD = 0
            from eneru.tui import render_logs_panel
            render_logs_panel(None, 0, 10, 30, [], False,
                              graph_mode="voltage", time_range="24h",
                              ups_index=0, ups_total=1)
        rendered_strs = [c.args[3] for c in sa.call_args_list
                         if isinstance(c.args[3], str)]
        rendered = " ".join(rendered_strs)
        # First hints (Q, R) make it; later ones may be skipped.
        assert "<Q>" in rendered
        # The combined width of all 6 full hints would exceed 30 cols,
        # so at least one of the right-most hints must be skipped.
        assert "<U>" not in rendered or "<T>" not in rendered


class TestRenderGraphText:
    """``render_graph_text`` is what ``run_once --graph`` prints."""

    def _config_with_db(self, tmp_path):
        from eneru import (
            Config, UPSConfig, UPSGroupConfig, StatsConfig,
            BehaviorConfig, LoggingConfig, NotificationsConfig,
            LocalShutdownConfig,
        )
        return Config(
            ups_groups=[UPSGroupConfig(
                ups=UPSConfig(name="TestUPS@localhost"),
                is_local=True,
            )],
            behavior=BehaviorConfig(dry_run=True),
            logging=LoggingConfig(
                state_file=str(tmp_path / "state"),
                battery_history_file=str(tmp_path / "history"),
                shutdown_flag_file=str(tmp_path / "flag"),
                file=None,
            ),
            notifications=NotificationsConfig(enabled=False),
            local_shutdown=LocalShutdownConfig(enabled=False),
            statistics=StatsConfig(db_directory=str(tmp_path)),
        )

    @pytest.mark.unit
    def test_render_graph_text_no_data_placeholder(self, tmp_path):
        from eneru.tui import render_graph_text
        config = self._config_with_db(tmp_path)
        # No DB file exists at all -- expect the "(no data)" placeholder.
        lines = render_graph_text(config, config.ups_groups[0],
                                  "charge", "1h")
        assert lines[0].startswith("charge -- last 1h")
        assert any("no data" in ln for ln in lines)

    @pytest.mark.unit
    def test_render_graph_text_with_real_samples(self, tmp_path):
        from eneru.tui import render_graph_text
        from eneru import StatsStore
        import time as _time
        config = self._config_with_db(tmp_path)
        # Seed the per-UPS DB with samples mirroring the daemon's path.
        from eneru.tui import stats_db_path_for
        store = StatsStore(stats_db_path_for(config.ups_groups[0], config))
        store.open()
        try:
            now = int(_time.time())
            for i in range(20):
                store.buffer_sample(
                    {"ups.status": "OL CHRG",
                     "battery.charge": str(50 + i),
                     "battery.runtime": "1800",
                     "ups.load": "30",
                     "input.voltage": "230",
                     "output.voltage": "230"},
                    ts=now - 10 + i,
                )
            store.flush()
        finally:
            store.close()

        lines = render_graph_text(config, config.ups_groups[0],
                                  "charge", "1h",
                                  width=40, height=4,
                                  force_fallback=True)
        # Header + 4 graph rows + y-axis label = 6 lines.
        assert len(lines) == 6
        # The graph rows must contain at least one non-blank cell.
        graph_rows = lines[1:5]
        assert any(any(c != " " for c in r) for r in graph_rows)

    @pytest.mark.unit
    def test_render_graph_text_unknown_metric(self, tmp_path):
        from eneru.tui import render_graph_text
        config = self._config_with_db(tmp_path)
        lines = render_graph_text(config, config.ups_groups[0],
                                  "nonsense", "1h")
        assert any("unknown metric" in ln for ln in lines)


class TestRunOnceGraphFlag:
    """``run_once`` accepts ``graph_metric`` and prints the graph block."""

    @pytest.mark.unit
    def test_run_once_with_graph_prints_block(self, tmp_path, capsys):
        from eneru.tui import run_once
        from eneru import (
            Config, UPSConfig, UPSGroupConfig, StatsConfig,
            LoggingConfig, NotificationsConfig, LocalShutdownConfig,
            BehaviorConfig,
        )
        config = Config(
            ups_groups=[UPSGroupConfig(ups=UPSConfig(name="TestUPS@localhost"))],
            behavior=BehaviorConfig(dry_run=True),
            logging=LoggingConfig(
                state_file=str(tmp_path / "state"),
                battery_history_file=str(tmp_path / "history"),
                shutdown_flag_file=str(tmp_path / "flag"),
                file=None,
            ),
            notifications=NotificationsConfig(enabled=False),
            local_shutdown=LocalShutdownConfig(enabled=False),
            statistics=StatsConfig(db_directory=str(tmp_path)),
        )
        run_once(config, graph_metric="charge", time_range="1h")
        out = capsys.readouterr().out
        assert "Graph: TestUPS@localhost" in out
        assert "charge -- last 1h" in out

    @pytest.mark.unit
    def test_run_once_without_graph_prints_no_graph_block(self, tmp_path, capsys):
        from eneru.tui import run_once
        from eneru import (
            Config, UPSConfig, UPSGroupConfig,
        )
        config = Config(ups_groups=[
            UPSGroupConfig(ups=UPSConfig(name="TestUPS@localhost")),
        ])
        run_once(config)
        out = capsys.readouterr().out
        assert "Graph:" not in out


# ===========================================================================
# Events panel sourced from SQLite (Phase 2 -- TUI events)
# ===========================================================================

def _events_config(tmp_path, ups_names=("TestUPS@localhost",)):
    from eneru import (
        Config, UPSConfig, UPSGroupConfig, StatsConfig,
        BehaviorConfig, LoggingConfig, NotificationsConfig,
        LocalShutdownConfig,
    )
    return Config(
        ups_groups=[UPSGroupConfig(ups=UPSConfig(name=n)) for n in ups_names],
        behavior=BehaviorConfig(dry_run=True),
        logging=LoggingConfig(
            state_file=str(tmp_path / "state"),
            battery_history_file=str(tmp_path / "history"),
            shutdown_flag_file=str(tmp_path / "flag"),
            file=str(tmp_path / "eneru.log"),
        ),
        notifications=NotificationsConfig(enabled=False),
        local_shutdown=LocalShutdownConfig(enabled=False),
        statistics=StatsConfig(db_directory=str(tmp_path)),
    )


def _seed_events(config, group, events):
    """Open the per-UPS DB the TUI would read and seed events."""
    from eneru import StatsStore
    from eneru.tui import stats_db_path_for
    store = StatsStore(stats_db_path_for(group, config))
    store.open()
    try:
        for ts, etype, detail in events:
            store.log_event(etype, detail, ts=ts)
    finally:
        store.close()


class TestQueryEventsForDisplay:

    @pytest.mark.unit
    def test_no_db_returns_empty(self, tmp_path):
        from eneru.tui import query_events_for_display
        config = _events_config(tmp_path)
        assert query_events_for_display(config) == []

    @pytest.mark.unit
    def test_uses_bounded_recent_query(self, tmp_path, monkeypatch):
        """F-045: the events pane must read the bounded query_recent_events
        (indexed LIMIT), never the full-table query_events(0, now)."""
        from eneru import StatsStore
        from eneru.tui import query_events_for_display, EVENTS_QUERY_LIMIT
        import time as _time
        config = _events_config(tmp_path)
        now = int(_time.time())
        _seed_events(config, config.ups_groups[0],
                     [(now - 60, "ON_BATTERY", "x")])

        recent_calls = []
        real_recent = StatsStore.query_recent_events

        def spy_recent(self, **kwargs):
            recent_calls.append(kwargs)
            return real_recent(self, **kwargs)

        def boom(self, *a, **k):
            raise AssertionError("full-table query_events must not be used")

        monkeypatch.setattr(StatsStore, "query_recent_events", spy_recent)
        monkeypatch.setattr(StatsStore, "query_events", boom)

        lines = query_events_for_display(config)
        assert lines and "ON_BATTERY: x" in lines[0]
        assert recent_calls, "query_recent_events was not called"
        assert recent_calls[0]["limit"] == EVENTS_QUERY_LIMIT
        assert "end_ts" in recent_calls[0]

    @pytest.mark.unit
    def test_single_ups_events_no_label_prefix(self, tmp_path):
        from eneru.tui import query_events_for_display
        import time as _time
        config = _events_config(tmp_path)
        now = int(_time.time())
        _seed_events(config, config.ups_groups[0], [
            (now - 60, "ON_BATTERY", "Battery: 85%"),
            (now - 30, "POWER_RESTORED", "Outage 30s"),
        ])
        lines = query_events_for_display(config)
        assert len(lines) == 2
        # Single-UPS configs do not prefix with [LABEL].
        assert "[" not in lines[0]
        assert "ON_BATTERY: Battery: 85%" in lines[0]
        assert "POWER_RESTORED: Outage 30s" in lines[1]

    @pytest.mark.unit
    def test_multi_ups_events_prefixed_with_label(self, tmp_path):
        from eneru.tui import query_events_for_display
        import time as _time
        config = _events_config(tmp_path,
                                ups_names=("UPS1@host1", "UPS2@host2"))
        now = int(_time.time())
        _seed_events(config, config.ups_groups[0],
                     [(now - 60, "ON_BATTERY", "ups1")])
        _seed_events(config, config.ups_groups[1],
                     [(now - 30, "ON_BATTERY", "ups2")])
        lines = query_events_for_display(config)
        assert len(lines) == 2
        # Sorted by ts ascending, prefixed with [label].
        assert "[UPS1@host1] ON_BATTERY: ups1" in lines[0]
        assert "[UPS2@host2] ON_BATTERY: ups2" in lines[1]

    @pytest.mark.unit
    def test_multi_ups_events_interleaved_by_timestamp(self, tmp_path):
        from eneru.tui import query_events_for_display
        import time as _time
        config = _events_config(tmp_path,
                                ups_names=("UPS1@h", "UPS2@h"))
        now = int(_time.time())
        # verbosity=1 — this test exercises sort/interleave for arbitrary
        # diagnostics, not the default Power-only filter itself.
        _seed_events(config, config.ups_groups[0],
                     [(now - 100, "A", ""), (now - 20, "C", "")])
        _seed_events(config, config.ups_groups[1],
                     [(now - 60, "B", "")])
        lines = query_events_for_display(config, verbosity=1)
        # Order: A (UPS1), B (UPS2), C (UPS1)
        assert "[UPS1@h] A" in lines[0]
        assert "[UPS2@h] B" in lines[1]
        assert "[UPS1@h] C" in lines[2]

    @pytest.mark.unit
    def test_max_events_caps_results(self, tmp_path):
        from eneru.tui import query_events_for_display
        import time as _time
        config = _events_config(tmp_path)
        now = int(_time.time())
        _seed_events(config, config.ups_groups[0], [
            (now - i, f"EVT{i}", "") for i in range(1, 11)
        ])
        # verbosity=1 — testing the row cap, not the type filter.
        lines = query_events_for_display(
            config, max_events=3, verbosity=1,
        )
        # Most recent 3 events.
        assert len(lines) == 3

    @pytest.mark.unit
    def test_event_line_includes_full_date(self, tmp_path):
        """Rows must include YYYY-MM-DD prefix so multi-day events are
        distinguishable in the TUI events panel (regression: TODO #1)."""
        from datetime import datetime
        from eneru.tui import query_events_for_display
        config = _events_config(tmp_path)
        # Pin a timestamp; assert the rendered prefix matches local-time
        # YYYY-MM-DD HH:MM:SS for the same instant.
        ts = 1_700_000_000  # 2023-11-14 22:13:20 UTC, varies by local tz
        expected_prefix = datetime.fromtimestamp(ts).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        _seed_events(config, config.ups_groups[0],
                     [(ts, "ON_BATTERY", "Battery: 90%")])
        lines = query_events_for_display(config)
        assert len(lines) == 1
        assert lines[0].startswith(expected_prefix), (
            f"expected line to start with {expected_prefix!r}, got {lines[0]!r}"
        )

    @pytest.mark.unit
    def test_event_line_uses_placeholder_for_bad_timestamp(self, tmp_path):
        """Malformed timestamps render as a same-width placeholder so the
        column alignment in the events panel doesn't shift."""
        from eneru.tui import _format_event_line
        line = _format_event_line(
            ts="not-a-number", label="UPS@h", event_type="X",
            detail="", multi_ups=False,
        )
        assert line.startswith("????-??-?? ??:??:??")

    @pytest.mark.unit
    def test_default_filters_to_power_events(self, tmp_path):
        """Default verbosity keeps the panel focused on power transitions."""
        from eneru.tui import query_events_for_display
        import time as _time
        config = _events_config(tmp_path)
        now = int(_time.time())
        events = []
        # 30 diagnostics rows that should be filtered.
        for i in range(30):
            events.append(
                (now - 100 + i, "VOLTAGE_FLAP_SUPPRESSED", f"flap {i}")
            )
        # Lifecycle and Power rows; only Power should survive the default filter.
        events.append((now - 50, "DAEMON_START", "v1"))
        events.append((now - 5, "POWER_RESTORED", "Outage 12s"))
        _seed_events(config, config.ups_groups[0], events)
        lines = query_events_for_display(config, max_events=8)
        assert all("DAEMON_START" not in line for line in lines)
        assert any("POWER_RESTORED" in line for line in lines)
        assert all("VOLTAGE_FLAP_SUPPRESSED" not in line for line in lines)

    @pytest.mark.unit
    def test_safety_critical_health_events_are_power_tier(self, tmp_path):
        """Default output includes safety-critical event names the daemon emits."""
        from eneru.tui import query_events_for_display
        import time as _time
        config = _events_config(tmp_path)
        now = int(_time.time())
        _seed_events(config, config.ups_groups[0], [
            (now - 30, "BYPASS_MODE_ACTIVE", "bypass"),
            (now - 20, "OVERLOAD_ACTIVE", "overload"),
            (now - 10, "BYPASS_MODE_INACTIVE", "resolved"),
        ])

        lines = query_events_for_display(config)
        assert any("BYPASS_MODE_ACTIVE" in line for line in lines)
        assert any("OVERLOAD_ACTIVE" in line for line in lines)
        assert all("BYPASS_MODE_INACTIVE" not in line for line in lines)

    @pytest.mark.unit
    def test_verbose_includes_diagnostics_not_lifecycle(self, tmp_path):
        """``-v`` adds Diagnostics while keeping Lifecycle hidden."""
        from eneru.tui import query_events_for_display
        import time as _time
        config = _events_config(tmp_path)
        now = int(_time.time())
        _seed_events(config, config.ups_groups[0], [
            (now - 100, "VOLTAGE_FLAP_SUPPRESSED", "flap"),
            (now - 50, "DAEMON_START", "v1"),
        ])
        lines = query_events_for_display(config, verbosity=1)
        assert len(lines) == 1
        assert any("VOLTAGE_FLAP_SUPPRESSED" in line for line in lines)
        assert all("DAEMON_START" not in line for line in lines)

    @pytest.mark.unit
    def test_slow_response_events_are_diagnostics(self, tmp_path):
        """Slow NUT/SSH response rows surface at Diagnostics verbosity."""
        from eneru.tui import query_events_for_display
        import time as _time
        config = _events_config(tmp_path)
        now = int(_time.time())
        _seed_events(config, config.ups_groups[0], [
            (now - 20, "SLOW_NUT_RESPONSE", "nut"),
            (now - 10, "REMOTE_SSH_SLOW_RESPONSE", "ssh"),
        ])

        default_lines = query_events_for_display(config)
        verbose_lines = query_events_for_display(config, verbosity=1)

        assert all("SLOW_NUT_RESPONSE" not in line for line in default_lines)
        assert all("REMOTE_SSH_SLOW_RESPONSE" not in line for line in default_lines)
        assert any("SLOW_NUT_RESPONSE" in line for line in verbose_lines)
        assert any("REMOTE_SSH_SLOW_RESPONSE" in line for line in verbose_lines)

    @pytest.mark.unit
    def test_double_verbose_includes_all_tiers(self, tmp_path):
        """``-vv`` includes Power, Diagnostics, and Lifecycle."""
        from eneru.tui import query_events_for_display
        import time as _time
        config = _events_config(tmp_path)
        now = int(_time.time())
        _seed_events(config, config.ups_groups[0], [
            (now - 100, "VOLTAGE_FLAP_SUPPRESSED", "flap"),
            (now - 50, "DAEMON_START", "v1"),
            (now - 25, "ON_BATTERY", "outage"),
        ])
        lines = query_events_for_display(config, verbosity=2)
        assert len(lines) == 3
        assert any("ON_BATTERY" in line for line in lines)
        assert any("VOLTAGE_FLAP_SUPPRESSED" in line for line in lines)
        assert any("DAEMON_START" in line for line in lines)

    @pytest.mark.unit
    def test_max_events_none_disables_cap(self, tmp_path):
        """``max_events=None`` returns every priority row in the events
        table -- the ``--length 0`` promise. The default cap of 30
        would otherwise silently drop older rows on long-running installs.
        """
        from eneru.tui import query_events_for_display
        import time as _time
        config = _events_config(tmp_path)
        now = int(_time.time())
        # Seed 600 power events -- comfortably above the default cap of 30.
        _seed_events(config, config.ups_groups[0], [
            (now - 600 + i, "ON_BATTERY", f"row-{i}")
            for i in range(600)
        ])
        # Default cap (EVENTS_MAX_ROWS_NORMAL=30) drops the oldest 570.
        capped = query_events_for_display(config)
        assert len(capped) == 30
        # max_events=None must surface every row.
        full = query_events_for_display(config, max_events=None)
        assert len(full) == 600, (
            f"max_events=None must disable the cap; got {len(full)} rows"
        )
        # Ordering preserved (ascending by timestamp).
        assert "row-0" in full[0]
        assert "row-599" in full[-1]

    @pytest.mark.unit
    def test_tiered_trim_preserves_power_events(self, tmp_path):
        """5.2.2 (real bug surfaced by maintainer's data): when the cap
        triggers, POWER_EVENTS must always survive even when the most-
        recent rows are all daemon-lifecycle. Pre-fix, 65 daemon rows
        could push 5 power-event rows off-screen at cap=20 since both
        tiers were treated as one undifferentiated 'priority' set.
        """
        from eneru.tui import query_events_for_display
        import time as _time
        config = _events_config(tmp_path)
        now = int(_time.time())
        # Mirror the maintainer's actual data shape: a sea of recent
        # DAEMON_RESTARTED + a handful of older ON_BATTERY / POWER_RESTORED.
        events = []
        for i in range(60):
            events.append((now - 60 + i, "DAEMON_RESTARTED", f"daemon-{i}"))
        events.append((now - 86400 * 5, "ON_BATTERY", "real outage"))
        events.append((now - 86400 * 5 + 600, "POWER_RESTORED", "outage 10m"))
        events.append((now - 86400 * 7, "EMERGENCY_SHUTDOWN_INITIATED", "low batt"))
        _seed_events(config, config.ups_groups[0], events)

        lines = query_events_for_display(config, max_events=20, verbosity=2)
        # All 3 power-event rows MUST survive the cap.
        assert any("ON_BATTERY: real outage" in line for line in lines), (
            f"ON_BATTERY pushed off by daemon noise -- tiered trim regressed. "
            f"Got: {lines}"
        )
        assert any("POWER_RESTORED: outage 10m" in line for line in lines)
        assert any("EMERGENCY_SHUTDOWN_INITIATED" in line for line in lines)
        # Result respects the cap.
        assert len(lines) == 20
        # Remaining 17 slots are most-recent daemon rows.
        daemon_lines = [line for line in lines if "DAEMON_RESTARTED" in line]
        assert len(daemon_lines) == 17

    @pytest.mark.unit
    def test_tiered_trim_pathological_more_power_than_cap(self, tmp_path):
        """Edge case: more power events than the cap. Take the most
        recent N -- still better than evicting them in favor of daemon
        events."""
        from eneru.tui import query_events_for_display
        import time as _time
        config = _events_config(tmp_path)
        now = int(_time.time())
        events = [
            (now - 1000 + i, "ON_BATTERY", f"outage-{i}") for i in range(50)
        ]
        events += [
            (now - 100 + i, "DAEMON_RESTARTED", f"daemon-{i}") for i in range(5)
        ]
        _seed_events(config, config.ups_groups[0], events)

        lines = query_events_for_display(config, max_events=10)
        # 10 most-recent ON_BATTERY rows; daemon entirely evicted.
        assert len(lines) == 10
        assert all("ON_BATTERY" in line for line in lines)
        assert any("outage-49" in line for line in lines)  # latest power event
        assert any("outage-40" in line for line in lines)  # 10th-latest

    @pytest.mark.unit
    def test_tiered_trim_diagnostics_outrank_lifecycle(self, tmp_path):
        """In -vv mode, diagnostics fill cap slots before lifecycle rows."""
        from eneru.tui import query_events_for_display
        import time as _time
        config = _events_config(tmp_path)
        now = int(_time.time())
        # 1 power event + 30 diagnostic rows + 4 lifecycle rows.
        # Cap = 7. Result must be: 1 power + 6 diagnostics, with lifecycle
        # evicted because it is the noisiest tier.
        events = [(now - 5000, "ON_BATTERY", "outage")]
        for i in range(30):
            events.append(
                (now - 1000 + i, "VOLTAGE_FLAP_SUPPRESSED", f"flap-{i}")
            )
        for i in range(4):
            events.append(
                (now - 100 + i * 10, "DAEMON_RESTARTED", f"d-{i}")
            )
        _seed_events(config, config.ups_groups[0], events)

        lines = query_events_for_display(
            config, max_events=7, verbosity=2,
        )
        assert len(lines) == 7
        assert sum("ON_BATTERY" in line for line in lines) == 1, (
            f"power event must survive: {lines}"
        )
        assert sum("VOLTAGE_FLAP_SUPPRESSED" in line for line in lines) == 6, (
            f"diagnostics must fill before lifecycle; got: {lines}"
        )
        assert sum("DAEMON_RESTARTED" in line for line in lines) == 0, (
            f"lifecycle should be evicted before diagnostics; got: {lines}"
        )

    @pytest.mark.unit
    def test_grouped_output_orders_sections_for_live_tui(self, tmp_path):
        """Live TUI groups enabled tiers as Power, Diagnostics, Lifecycle."""
        from eneru.tui import query_events_for_display
        import time as _time
        config = _events_config(tmp_path)
        now = int(_time.time())
        _seed_events(config, config.ups_groups[0], [
            (now - 30, "DAEMON_START", "start"),
            (now - 20, "VOLTAGE_FLAP_SUPPRESSED", "flap"),
            (now - 10, "ON_BATTERY", "outage"),
        ])

        lines = query_events_for_display(config, verbosity=2, grouped=True)
        assert lines[0] == "Power Events"
        assert any("ON_BATTERY" in line for line in lines[1:])
        assert lines.index("Diagnostics") < lines.index("Lifecycle")
        assert any("VOLTAGE_FLAP_SUPPRESSED" in line for line in lines)
        assert any("DAEMON_START" in line for line in lines)

    @pytest.mark.unit
    def test_grouped_cap_counts_headers_and_preserves_power(self, tmp_path):
        """Grouped live output must not add headers after the row cap."""
        from eneru.tui import query_events_for_display
        import time as _time
        config = _events_config(tmp_path)
        now = int(_time.time())
        events = [(now - 5000, "ON_BATTERY", "outage")]
        events.extend(
            (now - 100 + i, "VOLTAGE_FLAP_SUPPRESSED", f"diag-{i}")
            for i in range(20)
        )
        _seed_events(config, config.ups_groups[0], events)

        lines = query_events_for_display(
            config, max_events=5, verbosity=1, grouped=True,
        )
        assert len(lines) <= 5
        assert lines[0] == "Power Events"
        assert any("ON_BATTERY: outage" in line for line in lines)
        assert lines.count("Diagnostics") == 1
        assert any("diag-19" in line for line in lines)
        assert all("diag-0" not in line for line in lines)

    @pytest.mark.unit
    def test_crash_restart_events_are_diagnostics(self, tmp_path):
        """Crash/restart classifiers are diagnostics, not lifecycle."""
        from eneru.tui import query_events_for_display
        import time as _time
        config = _events_config(tmp_path)
        now = int(_time.time())
        _seed_events(config, config.ups_groups[0], [
            (now - 30, "DAEMON_RESTARTED_AFTER_FATAL", "fatal"),
            (now - 20, "DAEMON_AFTER_CRASH", "crash"),
            (now - 10, "DAEMON_START", "start"),
        ])

        default = query_events_for_display(config)
        verbose = query_events_for_display(config, verbosity=1)
        assert default == []
        assert any("DAEMON_RESTARTED_AFTER_FATAL" in line for line in verbose)
        assert any("DAEMON_AFTER_CRASH" in line for line in verbose)
        assert all("DAEMON_START" not in line for line in verbose)

    @pytest.mark.unit
    def test_grouped_length_one_keeps_power_event(self, tmp_path):
        """``max_events=1`` in grouped mode must still render the single
        most-recent Power event. The cap is too small to fit a section
        header *and* a row, so the header is dropped and the surviving
        row is rendered bare -- preserving the docstring contract that
        Power events are never evicted within the cap."""
        from eneru.tui import query_events_for_display
        import time as _time
        config = _events_config(tmp_path)
        now = int(_time.time())
        _seed_events(config, config.ups_groups[0], [
            (now - 30, "DAEMON_START", "boot"),
            (now - 20, "ON_BATTERY", "outage"),
            (now - 10, "DAEMON_RESTARTED", "restart"),
        ])

        out = query_events_for_display(
            config, max_events=1, verbosity=2, grouped=True,
        )
        assert len(out) == 1
        assert "ON_BATTERY" in out[0]
        # Bare row, no section header (no room for header + row at length=1).
        assert out[0] != "Power Events"

    @pytest.mark.unit
    def test_ungrouped_length_one_keeps_power_event(self, tmp_path):
        """Non-grouped path at length=1 returns the single most-recent
        Power event (regression guard alongside the grouped fallback)."""
        from eneru.tui import query_events_for_display
        import time as _time
        config = _events_config(tmp_path)
        now = int(_time.time())
        _seed_events(config, config.ups_groups[0], [
            (now - 20, "ON_BATTERY", "outage"),
            (now - 10, "DAEMON_START", "boot"),
        ])

        out = query_events_for_display(
            config, max_events=1, verbosity=2, grouped=False,
        )
        assert len(out) == 1
        assert "ON_BATTERY" in out[0]

    @pytest.mark.unit
    def test_grouped_length_one_falls_back_to_diagnostic_when_no_power(
            self, tmp_path):
        """At length=1 with no Power events, the grouped fallback must
        surface the most-recent Diagnostic survivor -- the second tier
        in the tier-priority order (Power -> Diagnostics -> Lifecycle)."""
        from eneru.tui import query_events_for_display
        import time as _time
        config = _events_config(tmp_path)
        now = int(_time.time())
        _seed_events(config, config.ups_groups[0], [
            (now - 30, "DAEMON_START", "boot"),
            (now - 20, "VOLTAGE_FLAP_SUPPRESSED", "flap"),
            (now - 10, "DAEMON_RESTARTED", "restart"),
        ])

        out = query_events_for_display(
            config, max_events=1, verbosity=2, grouped=True,
        )
        assert len(out) == 1
        assert "VOLTAGE_FLAP_SUPPRESSED" in out[0]


class TestRobustBounds:
    """``_robust_bounds`` keeps a single 0V outlier from squashing the
    voltage band into a one-row strip at the top of the chart."""

    @pytest.mark.unit
    def test_short_series_falls_back_to_min_max(self):
        from eneru.tui import _robust_bounds
        # < 20 samples -> percentile math is meaningless; use min/max.
        assert _robust_bounds([1.0, 2.0, 3.0]) == (1.0, 3.0)

    @pytest.mark.unit
    def test_single_outlier_clipped_from_bounds(self):
        """A single 0V dot among 99 normal voltage samples must not
        drag the lower bound down to zero. The 5th-percentile bound
        should stay close to the band of normal values."""
        from eneru.tui import _robust_bounds
        values = [230.0] * 99 + [0.0]
        lo, hi = _robust_bounds(values)
        assert lo > 200.0, f"expected lo > 200V, got {lo}"
        assert hi == 230.0

    @pytest.mark.unit
    def test_constant_series_falls_back_to_min_max(self):
        """When every sample is the same, percentiles collapse and the
        helper falls back to min/max so the renderer can still pad."""
        from eneru.tui import _robust_bounds
        assert _robust_bounds([42.0] * 50) == (42.0, 42.0)

    @pytest.mark.unit
    def test_normal_voltage_band_preserved(self):
        """Realistic 230V ± 5V series: percentile bounds stay tight to
        the meaningful range."""
        from eneru.tui import _robust_bounds
        import random
        random.seed(0)
        values = [230.0 + random.uniform(-5, 5) for _ in range(200)]
        lo, hi = _robust_bounds(values)
        assert 220.0 < lo < 230.0
        assert 230.0 < hi < 240.0

    @pytest.mark.unit
    def test_n_equals_20_excludes_extremes_both_sides(self):
        """Boundary test (CodeRabbit P1): for n=20 the helper must
        actually clip the top sample, not return it. The earlier
        ``int(n * 0.95)`` formulation produced sorted_vals[19] (the
        max itself), silently no-op'ing the helper at small n.
        """
        from eneru.tui import _robust_bounds
        # 18 samples at 230V plus one extreme on each side. Expected:
        # clip BOTH extremes -- lo and hi should both be 230.0.
        values = [0.0] + [230.0] * 18 + [999.0]
        lo, hi = _robust_bounds(values)
        assert lo == 230.0, (
            f"n=20 must clip the bottom outlier (was: {lo}); regression "
            "of the percentile off-by-one"
        )
        assert hi == 230.0, (
            f"n=20 must clip the top outlier (was: {hi}); the earlier "
            "int(n*0.95) returned sorted[19]=999, silently no-op'ing"
        )


class TestSanitizeEventDetail:
    """v5.2.1: lifecycle bodies stored in events.detail include
    `**markdown bold**` and embedded `\\n` (the same body that goes to
    Apprise where Discord/Slack render them natively). The TUI's curses
    panel can't, so the sanitizer flattens to one line and strips the
    asterisks. See screenshots in PR #35 for the broken-render case."""

    @pytest.mark.unit
    def test_strips_markdown_bold_markers(self):
        from eneru.tui import _sanitize_event_detail
        out = _sanitize_event_detail("📦  **Eneru Upgraded** v5.2.0 → v5.2.1")
        assert "**" not in out
        assert "📦  Eneru Upgraded v5.2.0 → v5.2.1" == out

    @pytest.mark.unit
    def test_collapses_embedded_newline(self):
        """Embedded `\\n` mid-string used to make `curses.addstr` jump to
        a new row before the gold background fill completed, leaving
        cells unpainted (the 'broken colors' visible on the
        'Service is back online' continuation row)."""
        from eneru.tui import _sanitize_event_detail
        out = _sanitize_event_detail(
            "📦  **Eneru Upgraded** v5.2.0 → v5.2.1\n"
            "Service is back online with the new version."
        )
        assert "\n" not in out
        assert "**" not in out
        assert "📦  Eneru Upgraded v5.2.0 → v5.2.1" in out
        assert "Service is back online with the new version." in out
        # Multi-line bodies join with " · " for visual separation.
        assert " · " in out

    @pytest.mark.unit
    def test_collapses_multiple_newlines(self):
        from eneru.tui import _sanitize_event_detail
        out = _sanitize_event_detail("a\nb\nc")
        assert out == "a · b · c"

    @pytest.mark.unit
    def test_strips_indentation_on_continuation_lines(self):
        """A continuation line indented for Apprise readability shouldn't
        leave a leading-space artifact in the joined one-liner."""
        from eneru.tui import _sanitize_event_detail
        out = _sanitize_event_detail("first\n   indented")
        assert out == "first · indented"

    @pytest.mark.unit
    def test_drops_empty_continuation_lines(self):
        from eneru.tui import _sanitize_event_detail
        out = _sanitize_event_detail("a\n\n\nb")
        assert out == "a · b"

    @pytest.mark.unit
    def test_passthrough_when_already_clean(self):
        from eneru.tui import _sanitize_event_detail
        assert _sanitize_event_detail("plain text 85%") == "plain text 85%"

    @pytest.mark.unit
    def test_empty_input(self):
        from eneru.tui import _sanitize_event_detail
        assert _sanitize_event_detail("") == ""
        assert _sanitize_event_detail(None) == ""

    @pytest.mark.unit
    def test_format_event_line_renders_sanitized_detail(self):
        """End-to-end: the rendered event line never contains `**` or
        embedded `\\n` for any of the v5.2 lifecycle bodies."""
        from eneru.tui import _format_event_line
        line = _format_event_line(
            ts=1700000000, label="UPS@h", event_type="DAEMON_UPGRADED",
            detail=("📦  **Eneru Upgraded** v5.2.0 → v5.2.1\n"
                    "Service is back online with the new version."),
            multi_ups=False,
        )
        assert "\n" not in line
        assert "**" not in line
        assert "DAEMON_UPGRADED:" in line
        assert "Service is back online" in line


class TestRunOnceEventsOnly:

    @pytest.mark.unit
    def test_events_only_prints_only_events(self, tmp_path, capsys):
        from eneru.tui import run_once
        import time as _time
        config = _events_config(tmp_path)
        now = int(_time.time())
        _seed_events(config, config.ups_groups[0], [
            (now - 60, "ON_BATTERY", "85%"),
        ])
        run_once(config, events_only=True)
        out = capsys.readouterr().out
        assert "ON_BATTERY: 85%" in out
        # Status / resources / graph header must NOT appear.
        assert "Eneru v" not in out
        assert "Status:" not in out
        assert "Graph:" not in out

    @pytest.mark.unit
    def test_events_only_no_db_falls_back_to_log(self, tmp_path, capsys):
        from eneru.tui import run_once
        config = _events_config(tmp_path)
        # Seed a log file with one matching line; no DB exists.
        log_path = Path(config.logging.file)
        log_path.write_text(
            "2026-04-20 10:00:00 - ⚡  POWER EVENT: ON_BATTERY - 85%\n"
        )
        run_once(config, events_only=True)
        out = capsys.readouterr().out
        assert "POWER EVENT: ON_BATTERY" in out

    @pytest.mark.unit
    def test_events_only_no_db_no_log_prints_placeholder(self, tmp_path, capsys):
        from eneru.tui import run_once
        config = _events_config(tmp_path)
        run_once(config, events_only=True)
        out = capsys.readouterr().out
        assert "(no power events recorded)" in out

    @pytest.mark.unit
    def test_events_only_db_with_no_power_does_not_fallback_to_log(
        self, tmp_path, capsys
    ):
        """A DB with only hidden tiers should say no power events, not parse logs."""
        from eneru.tui import run_once
        import time as _time
        config = _events_config(tmp_path)
        now = int(_time.time())
        _seed_events(config, config.ups_groups[0], [
            (now - 30, "VOLTAGE_FLAP_SUPPRESSED", "flap"),
        ])
        Path(config.logging.file).write_text(
            "2026-04-20 10:00:00 - ⚡  POWER EVENT: ON_BATTERY - stale log\n"
        )

        run_once(config, events_only=True)
        out = capsys.readouterr().out
        assert "(no power events recorded)" in out
        assert "stale log" not in out

    @pytest.mark.unit
    def test_events_only_flat_time_sorted_with_enabled_tiers(
        self, tmp_path, capsys
    ):
        """--once output remains flat and timestamp-sorted across enabled tiers."""
        from eneru.tui import run_once
        import time as _time
        config = _events_config(tmp_path)
        now = int(_time.time())
        _seed_events(config, config.ups_groups[0], [
            (now - 30, "DAEMON_START", "start"),
            (now - 20, "VOLTAGE_FLAP_SUPPRESSED", "flap"),
            (now - 10, "ON_BATTERY", "outage"),
        ])

        run_once(config, events_only=True, verbose=2)
        lines = capsys.readouterr().out.splitlines()
        assert all(line not in ("Power Events", "Diagnostics", "Lifecycle")
                   for line in lines)
        assert "DAEMON_START" in lines[0]
        assert "VOLTAGE_FLAP_SUPPRESSED" in lines[1]
        assert "ON_BATTERY" in lines[2]

    @pytest.mark.unit
    def test_snapshot_path_honours_verbose_level(self, tmp_path, capsys):
        """5.2.2 (cubic.dev / CodeRabbit P1): the non-events-only branch
        of run_once also reads events for its tail block. Pre-fix it used
        the default event query and silently ignored ``--verbose``, so
        diagnostics never surfaced even when the user explicitly asked for
        them."""
        from eneru.tui import run_once
        import time as _time
        config = _events_config(tmp_path)
        now = int(_time.time())
        _seed_events(config, config.ups_groups[0], [
            (now - 60, "VOLTAGE_FLAP_SUPPRESSED", "low-prio chatter"),
            (now - 30, "DAEMON_START", "high-prio"),
        ])
        # Default snapshot path: diagnostics and lifecycle hidden.
        run_once(config, events_only=False)
        out_default = capsys.readouterr().out
        assert "DAEMON_START" not in out_default
        assert "VOLTAGE_FLAP_SUPPRESSED" not in out_default
        # -v: diagnostics surface, lifecycle remains hidden.
        run_once(config, events_only=False, verbose=1)
        out_verbose = capsys.readouterr().out
        assert "DAEMON_START" not in out_verbose
        assert "VOLTAGE_FLAP_SUPPRESSED" in out_verbose, (
            "snapshot path must honour -v; the events tail in the snapshot "
            "block must include diagnostics"
        )
        # -vv: lifecycle joins the flat, timestamp-sorted tail.
        run_once(config, events_only=False, verbose=2)
        out_all = capsys.readouterr().out
        assert "DAEMON_START" in out_all

    @pytest.mark.unit
    def test_snapshot_path_no_time_window_for_events(self, tmp_path, capsys):
        """5.2.2: events have no time window -- ``--time`` only affects
        graphs. The snapshot tail must surface old power rows even
        though they fall outside any ``--time`` choice."""
        from eneru.tui import run_once
        import time as _time
        config = _events_config(tmp_path)
        now = int(_time.time())
        old_ts = now - 90 * 86400  # 3 months ago
        _seed_events(config, config.ups_groups[0], [
            (old_ts, "ON_BATTERY", "ancient"),
            (now - 60, "POWER_RESTORED", "recent"),
        ])
        run_once(config, events_only=False)
        out = capsys.readouterr().out
        # Both surface even though "ancient" is well outside any --time:
        # the snapshot tail caps at 10 rows but applies no time filter.
        assert "recent" in out
        assert "ancient" in out, (
            "snapshot path must show events regardless of --time; events "
            "have no time window in 5.2.2+"
        )

    @pytest.mark.unit
    def test_snapshot_path_honours_length(self, tmp_path, capsys):
        """``--once --length N`` must size the Recent Events block too."""
        from eneru.tui import run_once
        import time as _time
        config = _events_config(tmp_path)
        now = int(_time.time())
        _seed_events(config, config.ups_groups[0], [
            (now - 100 + i, "DAEMON_START", f"row-{i}") for i in range(20)
        ])

        run_once(config, events_only=False, verbose=2, length=15)
        out = capsys.readouterr().out
        lines = [line for line in out.splitlines() if "DAEMON_START" in line]
        assert len(lines) == 15
        assert "row-5" in lines[0]
        assert "row-19" in lines[-1]

    @pytest.mark.unit
    def test_events_only_length_caps_output(self, tmp_path, capsys):
        """``--length N`` (events_only=True path) caps output to N rows."""
        from eneru.tui import run_once
        import time as _time
        config = _events_config(tmp_path)
        now = int(_time.time())
        _seed_events(config, config.ups_groups[0], [
            (now - 100 + i, "ON_BATTERY", f"row-{i}") for i in range(50)
        ])
        run_once(config, events_only=True, length=5)
        out = capsys.readouterr().out
        # Should see exactly 5 lines -- the most-recent 5 ON_BATTERY rows.
        lines = [line for line in out.splitlines() if "ON_BATTERY" in line]
        assert len(lines) == 5
        assert "row-49" in lines[-1]
        assert "row-45" in lines[0]

    @pytest.mark.unit
    def test_events_only_length_zero_means_no_cap(self, tmp_path, capsys):
        """``--length 0`` returns every enabled row."""
        from eneru.tui import run_once
        import time as _time
        config = _events_config(tmp_path)
        now = int(_time.time())
        _seed_events(config, config.ups_groups[0], [
            (now - 100 + i, "ON_BATTERY", f"row-{i}") for i in range(40)
        ])
        run_once(config, events_only=True, length=0)
        out = capsys.readouterr().out
        lines = [line for line in out.splitlines() if "ON_BATTERY" in line]
        assert len(lines) == 40, f"length=0 must show all rows; got {len(lines)}"


class TestDisplayWidthAndTruncate:
    """The cell-aware width helpers behind the events panel overflow fix."""

    @pytest.mark.unit
    def test_ascii_width_equals_length(self):
        from eneru.tui import display_width
        assert display_width("hello world") == 11

    @pytest.mark.unit
    def test_emoji_counted_as_two_cells(self):
        from eneru.tui import display_width
        # The "⚡" emoji (U+26A1) and the broader emoji range are
        # double-width on most terminals.
        assert display_width("⚡") == 2
        assert display_width("a⚡b") == 4

    @pytest.mark.unit
    def test_cjk_counted_as_two_cells(self):
        from eneru.tui import display_width
        assert display_width("漢字") == 4

    @pytest.mark.unit
    def test_truncate_fits_when_short_enough(self):
        from eneru.tui import truncate_to_width
        assert truncate_to_width("hello", 10) == "hello"

    @pytest.mark.unit
    def test_truncate_clips_ascii(self):
        from eneru.tui import truncate_to_width
        assert truncate_to_width("0123456789", 5) == "01234"

    @pytest.mark.unit
    def test_truncate_clips_before_partial_emoji(self):
        from eneru.tui import truncate_to_width
        # 4 cells for "ab" + emoji; 3 cells leaves "ab" only because the
        # emoji (2 cells) wouldn't fit in the remaining 1 cell.
        assert truncate_to_width("ab⚡cd", 3) == "ab"

    @pytest.mark.unit
    def test_truncate_zero_max_returns_empty(self):
        from eneru.tui import truncate_to_width
        assert truncate_to_width("anything", 0) == ""

    @pytest.mark.unit
    def test_render_logs_panel_clips_emoji_lines(self):
        """Regression for the 'events overflow past the gold panel' bug.

        We use a fake window that records every addnstr() call and check
        that no painted line overflows the visible width when the events
        contain emojis.
        """
        from eneru.tui import render_logs_panel
        import curses as _c

        class FakeWin:
            def __init__(self, h, w):
                self._h = h
                self._w = w
                self.painted = []
            def getmaxyx(self):
                return (self._h, self._w)
            def addnstr(self, y, x, text, n, attr=0):
                self.painted.append((y, x, text[:n]))
            def insch(self, y, x, ch, attr=0):
                # fill_row uses insch to paint the rightmost cell so it
                # doesn't crash on the bottom-right corner.
                self.painted.append((y, x, chr(ch) if isinstance(ch, int) else ch))

        # 30-cell wide panel. Event has lots of emoji that would each
        # double the rendered width.
        win = FakeWin(20, 30)
        evt = "⚡⚡⚡  POWER EVENT: ON_BATTERY - " + "🔋" * 10
        # Stub out curses pair lookups so render_logs_panel does not crash.
        try:
            _c.color_pair  # noqa: B018
        except Exception:
            pass
        # render_logs_panel calls curses.color_pair(...) which requires
        # curses init -- patch it via module-level monkey.
        import eneru.tui as tui_mod
        with patch.object(tui_mod, "curses", _CursesStub()):
            render_logs_panel(win, 1, 19, 30, [evt], show_more=False)

        # Every painted text must fit in the 30-cell window.
        from eneru.tui import display_width
        for _y, _x, text in win.painted:
            assert display_width(text) <= 30


class _CursesStub:
    """Minimal curses stub used by the render-overflow regression test."""
    A_BOLD = 0
    error = type("error", (Exception,), {})

    @staticmethod
    def color_pair(_n):
        return 0


# ====================================================================
# Pure-logic helpers (no curses required)
# ====================================================================


class TestHumanStatus:
    """Translate NUT status flags into operator-friendly strings."""

    @pytest.mark.unit
    def test_fsd_takes_precedence(self):
        assert human_status("FSD OL CHRG") == "FORCED SHUTDOWN"

    @pytest.mark.unit
    def test_on_battery_low_takes_precedence_over_on_battery(self):
        assert human_status("OB LB") == "ON BATTERY - LOW"

    @pytest.mark.unit
    def test_on_battery_discharging(self):
        assert human_status("OB DISCHRG") == "ON BATTERY - DISCHARGING"

    @pytest.mark.unit
    def test_on_battery_alone(self):
        assert human_status("OB") == "ON BATTERY"

    @pytest.mark.unit
    def test_online_charging(self):
        assert human_status("OL CHRG") == "ONLINE - CHARGING"

    @pytest.mark.unit
    def test_online_alone(self):
        assert human_status("OL") == "ONLINE"

    @pytest.mark.unit
    def test_charging_without_online_marker(self):
        # Defensive: most NUT setups always include OL or OB; this is the
        # fallback when only CHRG is present.
        assert human_status("CHRG") == "CHARGING"

    @pytest.mark.unit
    def test_empty_status_is_unknown(self):
        assert human_status("") == "UNKNOWN"
        assert human_status("   ") == "UNKNOWN"

    @pytest.mark.unit
    def test_unrecognised_status_passes_through_uppercased(self):
        # No rule matched — return the raw upper-cased status so
        # operators can still see what the UPS reported.
        assert human_status("WTF") == "WTF"


class TestStatusColor:
    """Map status flags to color-pair IDs for the badge."""

    @pytest.mark.unit
    def test_fsd_is_critical(self):
        assert status_color("FSD") == C_STATUS_CRIT

    @pytest.mark.unit
    def test_low_battery_is_critical(self):
        assert status_color("OB LB") == C_STATUS_CRIT

    @pytest.mark.unit
    def test_on_battery_discharging_is_critical(self):
        assert status_color("OB DISCHRG") == C_STATUS_CRIT

    @pytest.mark.unit
    def test_on_battery_alone_is_warning(self):
        assert status_color("OB") == C_STATUS_OB

    @pytest.mark.unit
    def test_online_is_ok(self):
        assert status_color("OL") == C_STATUS_OK

    @pytest.mark.unit
    def test_charging_alone_is_ok(self):
        assert status_color("CHRG") == C_STATUS_OK

    @pytest.mark.unit
    def test_unknown_status_is_unknown_color(self):
        assert status_color("?") == C_STATUS_UNK


class TestStatusAttr:
    """`status_attr` adds A_BOLD always, and A_BLINK only for warning/critical
    states (OB, FSD, LB) — the operator's signal that the UPS needs
    attention even at a glance.

    `curses.color_pair()` requires initscr() and crashes outside a real
    curses session, so patch it to a no-op (the BLINK logic, not the
    color_pair lookup, is what's under test)."""

    @pytest.mark.unit
    def test_online_status_is_bold_only(self):
        with patch("eneru.tui.curses.color_pair", return_value=0):
            attr = status_attr("OL CHRG")
        assert attr & curses.A_BOLD
        assert not (attr & curses.A_BLINK)

    @pytest.mark.unit
    def test_on_battery_status_blinks(self):
        with patch("eneru.tui.curses.color_pair", return_value=0):
            attr = status_attr("OB DISCHRG")
        assert attr & curses.A_BOLD
        assert attr & curses.A_BLINK

    @pytest.mark.unit
    def test_fsd_status_blinks(self):
        with patch("eneru.tui.curses.color_pair", return_value=0):
            attr = status_attr("FSD")
        assert attr & curses.A_BLINK

    @pytest.mark.unit
    def test_low_battery_status_blinks(self):
        with patch("eneru.tui.curses.color_pair", return_value=0):
            attr = status_attr("OL LB")
        assert attr & curses.A_BLINK


class TestFormatRuntime:
    """`format_runtime` converts NUT runtime seconds to a human display."""

    @pytest.mark.unit
    def test_hours_and_minutes(self):
        from eneru.tui import format_runtime
        assert format_runtime("3700") == "1h 1m"  # 3700 = 1h 1m 40s, seconds dropped
        assert format_runtime("7200") == "2h 0m"

    @pytest.mark.unit
    def test_minutes_and_seconds(self):
        from eneru.tui import format_runtime
        assert format_runtime("125") == "2m 5s"
        assert format_runtime("60") == "1m 0s"

    @pytest.mark.unit
    def test_sub_minute_shows_seconds(self):
        from eneru.tui import format_runtime
        assert format_runtime("45") == "45s"
        assert format_runtime("0") == "0s"

    @pytest.mark.unit
    def test_invalid_input_returns_input_unchanged(self):
        """A non-numeric runtime (e.g. NUT didn't report it) falls
        back to the raw value so the operator at least sees what NUT
        did send."""
        from eneru.tui import format_runtime
        assert format_runtime("?") == "?"
        assert format_runtime("not-a-number") == "not-a-number"
        assert format_runtime("") == ""

    @pytest.mark.unit
    def test_floating_point_input_truncates(self):
        from eneru.tui import format_runtime
        # NUT can return decimals
        assert format_runtime("3661.7") == "1h 1m"


# ====================================================================
# Events-tier label helpers (run-once footer + placeholder text)
# ====================================================================


class TestEventsVerbosityLabel:
    """`_events_verbosity_label` is the short string in the live TUI
    footer telling the operator what tier they're seeing."""

    @pytest.mark.unit
    def test_power_label(self):
        from eneru.tui import _events_verbosity_label, EVENTS_VERBOSITY_POWER
        assert _events_verbosity_label(EVENTS_VERBOSITY_POWER) == "power"

    @pytest.mark.unit
    def test_diagnostics_label(self):
        from eneru.tui import _events_verbosity_label, EVENTS_VERBOSITY_DIAGNOSTICS
        assert _events_verbosity_label(EVENTS_VERBOSITY_DIAGNOSTICS) == "+diag"

    @pytest.mark.unit
    def test_all_label(self):
        from eneru.tui import _events_verbosity_label, EVENTS_VERBOSITY_ALL
        assert _events_verbosity_label(EVENTS_VERBOSITY_ALL) == "all"


class TestNoEventsMessage:
    """`_no_events_message` placeholder text varies by tier so the operator
    knows whether they're seeing a true gap or just narrow filtering."""

    @pytest.mark.unit
    def test_power_tier_message(self):
        from eneru.tui import _no_events_message, EVENTS_VERBOSITY_POWER
        assert _no_events_message(EVENTS_VERBOSITY_POWER) == "(no power events recorded)"

    @pytest.mark.unit
    def test_higher_tier_uses_generic_no_events(self):
        from eneru.tui import _no_events_message, EVENTS_VERBOSITY_DIAGNOSTICS
        assert _no_events_message(EVENTS_VERBOSITY_DIAGNOSTICS) == "(no events)"


class TestSummarizeRemoteHealth:
    """Already partially covered; add malformed-row defensive guards."""

    @pytest.mark.unit
    def test_empty_rows_returns_empty_string(self):
        from eneru.tui import summarize_remote_health
        assert summarize_remote_health([]) == ""

    @pytest.mark.unit
    def test_non_dict_rows_are_skipped(self):
        """A partially-written sidecar entry that isn't a mapping must
        not crash the summary — skip and continue."""
        from eneru.tui import summarize_remote_health
        rows = [
            "not-a-dict",  # malformed — must be skipped
            {"status": "healthy"},
            {"status": "failed"},
            42,  # also not a dict
            {"status": "healthy"},
        ]
        result = summarize_remote_health(rows)
        # Two healthy + one failed counted; malformed entries skipped
        assert "2 healthy" in result
        assert "1 failed" in result

    @pytest.mark.unit
    def test_status_field_default_is_unknown(self):
        from eneru.tui import summarize_remote_health
        rows = [{"server": "x"}, {"server": "y"}]  # no status field
        result = summarize_remote_health(rows)
        assert "2 unknown" in result

    @pytest.mark.unit
    def test_orders_statuses_by_severity_priority(self):
        """Order: healthy, degraded, failed, checking, unknown, disabled."""
        from eneru.tui import summarize_remote_health
        rows = [
            {"status": "disabled"},
            {"status": "healthy"},
            {"status": "failed"},
            {"status": "degraded"},
        ]
        result = summarize_remote_health(rows)
        # The output is comma-separated in the priority order
        assert result.index("1 healthy") < result.index("1 degraded")
        assert result.index("1 degraded") < result.index("1 failed")
        assert result.index("1 failed") < result.index("1 disabled")


# ====================================================================
# Coverage-gap fillers: edge cases for helpers, renderers, and run_tui
# ====================================================================


class TestFmtRuntimeSeconds:
    """``_fmt_runtime_seconds`` powers the runtime graph axis labels."""

    @pytest.mark.unit
    def test_hours_and_minutes(self):
        from eneru.tui import _fmt_runtime_seconds
        # 3700s -> 1h 1m (2700//60 = 1, so 3700%3600=100 -> 100//60=1)
        assert _fmt_runtime_seconds(3700) == "1h 1m"

    @pytest.mark.unit
    def test_minutes_and_seconds(self):
        from eneru.tui import _fmt_runtime_seconds
        assert _fmt_runtime_seconds(125) == "2m 5s"

    @pytest.mark.unit
    def test_invalid_input_returns_placeholder(self):
        from eneru.tui import _fmt_runtime_seconds
        # Non-numeric -> ValueError -> "?"
        assert _fmt_runtime_seconds("not-a-number") == "?"


class TestCoerceFloat:
    """``_coerce_float`` defends ``update_live_buffer`` against bad input."""

    @pytest.mark.unit
    def test_none_returns_none(self):
        from eneru.tui import _coerce_float
        assert _coerce_float(None) is None

    @pytest.mark.unit
    def test_empty_string_returns_none(self):
        from eneru.tui import _coerce_float
        assert _coerce_float("") is None

    @pytest.mark.unit
    def test_non_numeric_string_returns_none(self):
        from eneru.tui import _coerce_float
        assert _coerce_float("abc") is None


class TestUpdateLiveBufferAllNonFloat:
    """When every state-file value parses as non-numeric, no sample is pushed."""

    @pytest.mark.unit
    def test_state_file_with_only_strings_does_not_push_sample(self, tmp_path):
        from eneru.tui import (
            update_live_buffer, _live_buffers, clear_live_buffers,
            state_file_path_for, _buffer_key,
        )
        from eneru import (
            Config, UPSConfig, UPSGroupConfig, StatsConfig,
            LoggingConfig, BehaviorConfig, NotificationsConfig,
            LocalShutdownConfig,
        )
        clear_live_buffers()
        config = Config(
            ups_groups=[UPSGroupConfig(
                ups=UPSConfig(name="TestUPS@localhost"),
                is_local=True,
            )],
            behavior=BehaviorConfig(dry_run=True),
            logging=LoggingConfig(
                state_file=str(tmp_path / "state"),
                battery_history_file=str(tmp_path / "history"),
                shutdown_flag_file=str(tmp_path / "flag"),
                file=None,
            ),
            notifications=NotificationsConfig(enabled=False),
            local_shutdown=LocalShutdownConfig(enabled=False),
            statistics=StatsConfig(db_directory=str(tmp_path)),
        )
        group = config.ups_groups[0]
        # State file has KEY=value lines but all values parse as non-numeric,
        # so the sample stays empty and the helper returns without appending.
        state_file_path_for(group, config).write_text(
            "BATTERY=?\nRUNTIME=?\nLOAD=?\n"
            "INPUT_VOLTAGE=\nOUTPUT_VOLTAGE=?\n"
        )
        update_live_buffer(group, config)
        buf = _live_buffers.get(_buffer_key(group, config))
        assert buf is None or len(buf) == 0


class TestParseStateFileException:
    """Unexpected read errors return None rather than propagate."""

    @pytest.mark.unit
    def test_unreadable_path_returns_none(self, tmp_path):
        from eneru.tui import parse_state_file
        # Patch Path.read_text to raise so the except branch fires.
        bad = tmp_path / "state"
        bad.write_text("BATTERY=85\n")
        with patch("eneru.tui.Path.read_text",
                   side_effect=PermissionError("nope")):
            assert parse_state_file(bad) is None


class TestEventsDbAvailableCloseException:
    """A close() exception during the DB-availability probe must not crash."""

    @pytest.mark.unit
    def test_close_exception_does_not_propagate(self, tmp_path):
        from eneru.tui import events_db_available
        from eneru import Config, UPSConfig, UPSGroupConfig
        config = Config(
            ups_groups=[UPSGroupConfig(ups=UPSConfig(name="UPS@h"))],
        )
        bad_conn = MagicMock()
        bad_conn.close.side_effect = RuntimeError("close failed")
        with patch("eneru.tui.StatsStore.open_readonly",
                   return_value=bad_conn):
            assert events_db_available(config) is True
        bad_conn.close.assert_called_once()


class TestQueryEventsForDisplayCloseException:
    """Close errors during the events query are swallowed."""

    @pytest.mark.unit
    def test_close_exception_during_events_query_is_swallowed(self):
        from eneru.tui import query_events_for_display
        from eneru import Config, UPSConfig, UPSGroupConfig
        config = Config(
            ups_groups=[UPSGroupConfig(ups=UPSConfig(name="UPS@h"))],
        )
        bad_conn = MagicMock()
        bad_conn.close.side_effect = RuntimeError("close failed")
        # Patch the whole class so both ``open_readonly`` and the
        # constructor route through the same mock.
        fake_store = MagicMock()
        fake_store.query_events.return_value = []
        store_cls = MagicMock(return_value=fake_store)
        store_cls.open_readonly.return_value = bad_conn
        with patch("eneru.tui.StatsStore", store_cls):
            lines = query_events_for_display(config)
        assert lines == []
        bad_conn.close.assert_called_once()


class TestQueryMetricSeriesEdges:
    """Extra paths in the SQLite + deque blend helper."""

    def _config(self, tmp_path: Path):
        from eneru import (
            Config, UPSConfig, UPSGroupConfig, StatsConfig,
            BehaviorConfig, LoggingConfig, NotificationsConfig,
            LocalShutdownConfig,
        )
        return Config(
            ups_groups=[UPSGroupConfig(
                ups=UPSConfig(name="TestUPS@localhost"),
                is_local=True,
            )],
            behavior=BehaviorConfig(dry_run=True),
            logging=LoggingConfig(
                state_file=str(tmp_path / "state"),
                battery_history_file=str(tmp_path / "history"),
                shutdown_flag_file=str(tmp_path / "flag"),
                file=None,
            ),
            notifications=NotificationsConfig(enabled=False),
            local_shutdown=LocalShutdownConfig(enabled=False),
            statistics=StatsConfig(db_directory=str(tmp_path)),
        )

    @pytest.mark.unit
    def test_skips_deque_samples_missing_the_requested_column(self, tmp_path):
        """A deque entry without the column key must be silently skipped."""
        from eneru.tui import (
            query_metric_series, _live_buffer_for, clear_live_buffers,
        )
        clear_live_buffers()
        config = self._config(tmp_path)
        group = config.ups_groups[0]
        now = int(time.time())
        buf = _live_buffer_for(group, config)
        # First sample has no battery_charge → skipped (line 364).
        buf.append((now - 5, {"input_voltage": 230.0}))
        merged = query_metric_series(config, group, "charge", 60)
        assert merged == []

    @pytest.mark.unit
    def test_close_exception_during_metric_query_swallowed(self):
        """A close() exception on the readonly conn must not propagate."""
        from eneru.tui import query_metric_series
        from eneru import Config, UPSConfig, UPSGroupConfig
        config = Config(
            ups_groups=[UPSGroupConfig(ups=UPSConfig(name="UPS@h"))],
        )
        bad_conn = MagicMock()
        bad_conn.close.side_effect = RuntimeError("close failed")
        # The store wrapper just needs to return an empty series so we
        # exercise the close() branch deterministically. Patch the whole
        # class so both ``open_readonly`` and the constructor are
        # intercepted by the same mock.
        fake_store = MagicMock()
        fake_store.query_range.return_value = []
        store_cls = MagicMock(return_value=fake_store)
        # ISS-039: the wrapper is now built via StatsStore.from_connection.
        store_cls.from_connection.return_value = fake_store
        store_cls.open_readonly.return_value = bad_conn
        with patch("eneru.tui.StatsStore", store_cls):
            # No live buffer either → returns sqlite_series (empty).
            result = query_metric_series(
                config, config.ups_groups[0], "charge", 60,
            )
        assert result == []
        bad_conn.close.assert_called_once()


class TestRenderLogsPanelGroupedBreak:
    """Grouped output stops adding sections once the cap leaves <2 rows."""

    @pytest.mark.unit
    def test_grouped_break_when_only_one_row_remains(self, tmp_path):
        from eneru.tui import query_events_for_display
        config = _events_config(tmp_path)
        now = int(time.time())
        # 1 power + 1 lifecycle. Cap = 3: Power Events header + ON_BATTERY
        # row uses 2 rows; only 1 slot remains for Lifecycle — not enough
        # for a header + row, so the loop breaks.
        _seed_events(config, config.ups_groups[0], [
            (now - 60, "ON_BATTERY", "outage"),
            (now - 30, "DAEMON_START", "boot"),
        ])
        out = query_events_for_display(
            config, max_events=3, verbosity=2, grouped=True,
        )
        # Header + power row only; Lifecycle section never appears.
        assert "Power Events" in out
        assert any("ON_BATTERY" in line for line in out)
        assert "Lifecycle" not in out
        assert all("DAEMON_START" not in line for line in out)


class TestCollectGroupDataResources:
    """``collect_group_data`` summarises the local + remote resource surface."""

    @pytest.mark.unit
    def test_vms_containers_compose_and_remote_listed(self, tmp_path):
        from eneru import (
            Config, UPSConfig, UPSGroupConfig, VMConfig, ContainersConfig,
            FilesystemsConfig, UnmountConfig, RemoteServerConfig,
            StatsConfig, LoggingConfig, BehaviorConfig,
            NotificationsConfig, LocalShutdownConfig,
        )
        from eneru.tui import collect_group_data
        config = Config(
            ups_groups=[UPSGroupConfig(
                ups=UPSConfig(name="UPS@h"),
                is_local=True,
                virtual_machines=VMConfig(enabled=True),
                containers=ContainersConfig(
                    enabled=True,
                    compose_files=["a.yml", "b.yml"],
                ),
                filesystems=FilesystemsConfig(
                    sync_enabled=True,
                    unmount=UnmountConfig(enabled=False),
                ),
                remote_servers=[
                    RemoteServerConfig(
                        name="nas", enabled=True,
                        host="10.0.0.1", user="root",
                    ),
                ],
            )],
            behavior=BehaviorConfig(dry_run=True),
            logging=LoggingConfig(
                state_file=str(tmp_path / "state"),
                battery_history_file=str(tmp_path / "history"),
                shutdown_flag_file=str(tmp_path / "flag"),
                file=None,
            ),
            notifications=NotificationsConfig(enabled=False),
            local_shutdown=LocalShutdownConfig(enabled=False),
            statistics=StatsConfig(db_directory=str(tmp_path)),
        )
        data = collect_group_data(config.ups_groups[0], config)
        # Compose file count is reported as "<n> compose"; VMs listed too.
        assert "VMs" in data["resources"]
        assert "2 compose" in data["resources"]
        assert "1 remote server" in data["resources"]

    @pytest.mark.unit
    def test_containers_without_compose_says_containers(self, tmp_path):
        """ContainersConfig.enabled but no compose_files → "containers"."""
        from eneru import (
            Config, UPSConfig, UPSGroupConfig, ContainersConfig,
            StatsConfig, LoggingConfig, BehaviorConfig,
            NotificationsConfig, LocalShutdownConfig,
        )
        from eneru.tui import collect_group_data
        config = Config(
            ups_groups=[UPSGroupConfig(
                ups=UPSConfig(name="UPS@h"),
                is_local=True,
                containers=ContainersConfig(enabled=True, compose_files=[]),
            )],
            behavior=BehaviorConfig(dry_run=True),
            logging=LoggingConfig(
                state_file=str(tmp_path / "state"),
                battery_history_file=str(tmp_path / "history"),
                shutdown_flag_file=str(tmp_path / "flag"),
                file=None,
            ),
            notifications=NotificationsConfig(enabled=False),
            local_shutdown=LocalShutdownConfig(enabled=False),
            statistics=StatsConfig(db_directory=str(tmp_path)),
        )
        data = collect_group_data(config.ups_groups[0], config)
        assert "containers" in data["resources"]
        assert "compose" not in data["resources"]

    @pytest.mark.unit
    def test_multiple_remote_servers_use_plural(self, tmp_path):
        from eneru import (
            Config, UPSConfig, UPSGroupConfig, RemoteServerConfig,
            StatsConfig, LoggingConfig, BehaviorConfig,
            NotificationsConfig, LocalShutdownConfig,
        )
        from eneru.tui import collect_group_data
        config = Config(
            ups_groups=[UPSGroupConfig(
                ups=UPSConfig(name="UPS@h"),
                is_local=True,
                remote_servers=[
                    RemoteServerConfig(
                        name="nas-a", enabled=True,
                        host="10.0.0.1", user="root",
                    ),
                    RemoteServerConfig(
                        name="nas-b", enabled=True,
                        host="10.0.0.2", user="root",
                    ),
                ],
            )],
            behavior=BehaviorConfig(dry_run=True),
            logging=LoggingConfig(
                state_file=str(tmp_path / "state"),
                battery_history_file=str(tmp_path / "history"),
                shutdown_flag_file=str(tmp_path / "flag"),
                file=None,
            ),
            notifications=NotificationsConfig(enabled=False),
            local_shutdown=LocalShutdownConfig(enabled=False),
            statistics=StatsConfig(db_directory=str(tmp_path)),
        )
        data = collect_group_data(config.ups_groups[0], config)
        assert "2 remote servers" in data["resources"]


class TestDisplayWidthNul:
    """Width helper ignores the NUL byte (invisible terminal code point)."""

    @pytest.mark.unit
    def test_nul_does_not_count_as_a_cell(self):
        from eneru.tui import display_width
        assert display_width("ab\x00c") == 3


class TestSafeAddstrBoundaries:
    """``safe_addstr`` clips writes to keep curses from raising."""

    @pytest.mark.unit
    def test_out_of_range_y_is_a_no_op(self):
        from eneru.tui import safe_addstr
        win = _FakeWin(height=5, width=20)
        safe_addstr(win, y=99, x=0, text="hello", attr=0)
        assert win.cells == {}

    @pytest.mark.unit
    def test_zero_available_cells_is_a_no_op(self):
        """Writing at the rightmost column has zero cells available — skip."""
        from eneru.tui import safe_addstr
        win = _FakeWin(height=5, width=20)
        safe_addstr(win, y=0, x=19, text="x", attr=0)
        # x=19 leaves max_x - x - 1 = 0 cells available → no write.
        assert win.cells == {}

    @pytest.mark.unit
    def test_truncated_to_empty_is_a_no_op(self):
        """A multi-cell first char that doesn't fit yields an empty truncate."""
        from eneru.tui import safe_addstr
        win = _FakeWin(height=5, width=20)
        # Only 1 cell available; the emoji needs 2 cells → truncate empty.
        safe_addstr(win, y=0, x=18, text="🔋", attr=0)
        assert win.cells == {}

    @pytest.mark.unit
    def test_swallows_curses_error_from_underlying_win(self):
        """If addnstr raises, safe_addstr returns silently."""
        from eneru.tui import safe_addstr

        class BadWin:
            def getmaxyx(self):
                return (10, 40)
            def addnstr(self, *args, **kwargs):
                raise curses.error("simulated curses failure")

        # Must not raise.
        safe_addstr(BadWin(), y=1, x=1, text="hi", attr=0)


class TestFillRowBoundaries:
    """``fill_row`` defends against curses raising on edge writes."""

    @pytest.mark.unit
    def test_y_out_of_range_is_a_no_op(self):
        from eneru.tui import fill_row
        win = _FakeWin(height=5, width=20)
        fill_row(win, y=99, attr=0)
        assert win.cells == {}

    @pytest.mark.unit
    def test_zero_width_window_is_a_no_op(self):
        from eneru.tui import fill_row
        win = _FakeWin(height=5, width=0)
        fill_row(win, y=0, attr=0)
        assert win.cells == {}

    @pytest.mark.unit
    def test_addnstr_failure_does_not_propagate(self):
        """If addnstr raises, the helper continues into insch silently."""
        from eneru.tui import fill_row

        calls = []

        class BadWin:
            def getmaxyx(self):
                return (10, 40)
            def addnstr(self, *args, **kwargs):
                calls.append(("addnstr", args))
                raise curses.error("simulated")
            def insch(self, *args, **kwargs):
                calls.append(("insch", args))
                raise curses.error("simulated too")

        # Must not raise — both branches are wrapped in try/except.
        fill_row(BadWin(), y=0, attr=0)
        assert calls[0][0] == "addnstr"
        assert calls[-1][0] == "insch"


class TestRenderConfigPanelEarlyBreaks:
    """When y_end is small, the renderer should bail out instead of overflowing."""

    def _data(self):
        return [{
            "label": "Rack",
            "name": "Rack",  # equal to label → no extra "( )"
            "is_local": False,
            "state": {
                "STATUS": "OL",
                "BATTERY": "100",
                "RUNTIME": "1800",
                "LOAD": "20",
                "INPUT_VOLTAGE": "230",
                "OUTPUT_VOLTAGE": "230",
                "TIMESTAMP": "2026-05-15 12:00:00",
            },
            "resources": "VMs",
            "remote_health_summary": "1 healthy",
        }]

    @pytest.mark.unit
    def test_break_at_loop_start_when_y_end_is_two(self):
        """y_start=0, y_end=2 → y=1, loop condition y >= y_end-1 ⇒ break."""
        from eneru.tui import render_config_panel
        win = _FakeWin(height=4, width=80)
        with patch.object(curses, "color_pair", lambda n: n):
            render_config_panel(win, 0, 2, 80, self._data())
        # Nothing past the gray fill was painted — loop broke at line 913.
        assert not any(("Rack" in win.cells.get((y, 0), (" ", 0))[0])
                       for y in range(4))

    @pytest.mark.unit
    def test_break_after_each_y_increment(self):
        """A panel just one row taller hits the break after writing y+=1."""
        from eneru.tui import render_config_panel
        # Run the renderer with progressively larger y_end values so each
        # intermediate `if y >= y_end: break` (lines 939, 955, 963) is
        # exercised at least once.
        for y_end in (3, 4, 5):
            win = _FakeWin(height=10, width=80)
            with patch.object(curses, "color_pair", lambda n: n):
                # Must not raise even when the panel is too small to fit.
                render_config_panel(win, 0, y_end, 80, self._data())


class TestRenderLogsPanelNoAvailableSpace:
    """When the panel has no room for events the helper records nothing."""

    @pytest.mark.unit
    def test_zero_available_makes_display_events_empty(self):
        from eneru.tui import render_logs_panel
        win = _FakeWin(height=10, width=80)
        # y_start=0, y_end=2 leaves only the title row; no room for events
        # (available = y_end - y - footer_lines = 2 - 2 - 2 = -2 ≤ 0).
        with patch.object(curses, "color_pair", lambda n: n):
            render_logs_panel(
                win, y_start=0, y_end=2, width=80,
                events=["evt"], show_more=False,
            )
        # The single supplied event string must NOT appear anywhere in
        # the cell buffer — if available <= 0 the helper has to skip
        # event rows entirely (the title row is fine, but no "evt").
        painted = "".join(
            ch for (_y, _x), (ch, _attr) in win.cells.items()
        )
        assert "evt" not in painted


class TestRobustBoundsEmpty:
    """The defensive empty-list branch never crashes."""

    @pytest.mark.unit
    def test_empty_values_returns_zero_zero(self):
        from eneru.tui import _robust_bounds
        assert _robust_bounds([]) == (0.0, 0.0)


class TestRenderGraphPanelEdges:
    """``render_graph_panel`` defensive branches when inputs misbehave."""

    @pytest.mark.unit
    def test_one_row_panel_returns_immediately(self):
        from eneru.tui import render_graph_panel
        win = _FakeWin(height=10, width=80)
        cfg = MagicMock()
        cfg.statistics.db_directory = "/tmp"
        cfg.multi_ups = False
        group = MagicMock()
        group.ups.label = "UPS"
        group.ups.name = "UPS@h"
        with patch.object(curses, "color_pair", lambda n: n):
            render_graph_panel(
                win, y_start=0, y_end=1, width=80,
                config=cfg, group=group,
                graph_mode="charge", time_range="1h",
            )
        # panel_h == 1 → early return before any fill_row was issued.
        assert win.cells == {}

    @pytest.mark.unit
    def test_unknown_metric_shows_placeholder_and_returns(self):
        from eneru.tui import render_graph_panel
        win = _FakeWin(height=10, width=80)
        cfg = MagicMock()
        cfg.statistics.db_directory = "/tmp"
        cfg.multi_ups = False
        group = MagicMock()
        group.ups.label = "UPS"
        group.ups.name = "UPS@h"
        with patch.object(curses, "color_pair", lambda n: n):
            render_graph_panel(
                win, y_start=0, y_end=8, width=80,
                config=cfg, group=group,
                graph_mode="nonsense", time_range="1h",
            )
        row1 = "".join(
            win.cells.get((1, x), (" ", 0))[0] for x in range(80)
        )
        assert "(unknown metric)" in row1

    @pytest.mark.unit
    def test_no_data_shows_placeholder_and_returns(self):
        from eneru.tui import render_graph_panel
        win = _FakeWin(height=10, width=80)
        cfg = MagicMock()
        cfg.statistics.db_directory = "/tmp"
        cfg.multi_ups = False
        group = MagicMock()
        group.ups.label = "UPS"
        group.ups.name = "UPS@h"
        with patch.object(curses, "color_pair", lambda n: n), \
             patch("eneru.tui.query_metric_series", return_value=[]):
            render_graph_panel(
                win, y_start=0, y_end=8, width=80,
                config=cfg, group=group,
                graph_mode="charge", time_range="1h",
            )
        row1 = "".join(
            win.cells.get((1, x), (" ", 0))[0] for x in range(80)
        )
        assert "(no data yet)" in row1

    @pytest.mark.unit
    def test_flat_voltage_pads_min_and_max(self):
        """Constant voltage series → y_max == y_min → pad branch fires."""
        from eneru.tui import render_graph_panel
        win = _FakeWin(height=20, width=120)
        cfg = MagicMock()
        cfg.statistics.db_directory = "/tmp"
        cfg.multi_ups = False
        group = MagicMock()
        group.ups.label = "UPS"
        group.ups.name = "UPS@h"
        # All-same voltage so _robust_bounds returns equal hi/lo → pad.
        series = [(1000 + i, 230.0) for i in range(25)]
        with patch.object(curses, "color_pair", lambda n: n), \
             patch("eneru.tui.query_metric_series", return_value=series):
            # Must not raise; just renders the flat band with padding.
            render_graph_panel(
                win, y_start=0, y_end=10, width=120,
                config=cfg, group=group,
                graph_mode="voltage", time_range="1h",
            )


class TestRunTuiLogFallbackAndMove:
    """Coverage for the log-fallback events path and the move() guard."""

    def _group_data(self, group: UPSGroupConfig, _config: Config) -> dict:
        return {
            "label": group.ups.label,
            "name": group.ups.name,
            "is_local": False,
            "state": None,
            "resources": "none",
            "remote_health_summary": "",
        }

    @pytest.mark.unit
    def test_falls_back_to_parse_log_events_when_no_db(self, tmp_path):
        """No DB available → run_tui must call parse_log_events()."""
        from eneru import tui as tui_mod
        config = Config(
            ups_groups=[UPSGroupConfig(
                ups=UPSConfig(name="UPS@h"),
                is_local=False,
            )],
            logging=LoggingConfig(state_file=str(tmp_path / "state"),
                                  file=str(tmp_path / "log")),
        )
        screen = _FakeTuiScreen(height=30, width=120, keys=[ord("q")])

        def wrapper(callback):
            callback(screen)

        with patch.object(tui_mod.curses, "wrapper", side_effect=wrapper), \
             patch.object(tui_mod.curses, "COLORS", 256, create=True), \
             patch.object(tui_mod.curses, "start_color", lambda: None), \
             patch.object(tui_mod.curses, "init_pair", lambda *args: None), \
             patch.object(tui_mod.curses, "color_pair", lambda n: n), \
             patch.object(tui_mod.curses, "curs_set", lambda _value: None), \
             patch.object(tui_mod, "collect_group_data",
                          side_effect=self._group_data), \
             patch.object(tui_mod, "update_live_buffer"), \
             patch.object(tui_mod, "query_events_for_display",
                          return_value=[]), \
             patch.object(tui_mod, "events_db_available",
                          return_value=False), \
             patch.object(tui_mod, "parse_log_events",
                          return_value=["fallback row"]) as plog:
            tui_mod.run_tui(config)
        plog.assert_called()

    @pytest.mark.unit
    def test_move_curses_error_is_swallowed(self, tmp_path):
        """A move() that raises curses.error must not crash the loop."""
        from eneru import tui as tui_mod
        config = Config(
            ups_groups=[UPSGroupConfig(
                ups=UPSConfig(name="UPS@h"),
                is_local=False,
            )],
            logging=LoggingConfig(state_file=str(tmp_path / "state")),
        )

        class _MoveErrScreen(_FakeTuiScreen):
            def move(self, y, x):
                raise curses.error("bottom-right move blocked")

        screen = _MoveErrScreen(height=30, width=120, keys=[ord("q")])

        def wrapper(callback):
            callback(screen)

        with patch.object(tui_mod.curses, "wrapper", side_effect=wrapper), \
             patch.object(tui_mod.curses, "COLORS", 256, create=True), \
             patch.object(tui_mod.curses, "start_color", lambda: None), \
             patch.object(tui_mod.curses, "init_pair", lambda *args: None), \
             patch.object(tui_mod.curses, "color_pair", lambda n: n), \
             patch.object(tui_mod.curses, "curs_set", lambda _value: None), \
             patch.object(tui_mod, "collect_group_data",
                          side_effect=self._group_data), \
             patch.object(tui_mod, "update_live_buffer"), \
             patch.object(tui_mod, "query_events_for_display",
                          return_value=["x"]):
            # If the curses.error escaped, this would raise.
            tui_mod.run_tui(config)
        # Loop ran at least one refresh before quitting.
        assert screen.refreshes == 1

    @pytest.mark.unit
    def test_handles_page_up_home_and_down_arrow(self, tmp_path):
        """KEY_PPAGE / KEY_HOME / KEY_DOWN all reach their scroll handlers."""
        from eneru import tui as tui_mod
        config = Config(
            ups_groups=[UPSGroupConfig(
                ups=UPSConfig(name="UPS@h"),
                is_local=False,
            )],
            logging=LoggingConfig(state_file=str(tmp_path / "state")),
        )
        screen = _FakeTuiScreen(
            height=30, width=120,
            keys=[
                curses.KEY_PPAGE,
                curses.KEY_HOME,
                curses.KEY_DOWN,
                ord("q"),
            ],
        )

        def wrapper(callback):
            callback(screen)

        events = [f"event-{i}" for i in range(50)]
        with patch.object(tui_mod.curses, "wrapper", side_effect=wrapper), \
             patch.object(tui_mod.curses, "COLORS", 256, create=True), \
             patch.object(tui_mod.curses, "start_color", lambda: None), \
             patch.object(tui_mod.curses, "init_pair", lambda *args: None), \
             patch.object(tui_mod.curses, "color_pair", lambda n: n), \
             patch.object(tui_mod.curses, "curs_set", lambda _value: None), \
             patch.object(tui_mod, "collect_group_data",
                          side_effect=self._group_data), \
             patch.object(tui_mod, "update_live_buffer"), \
             patch.object(tui_mod, "query_events_for_display",
                          return_value=events):
            tui_mod.run_tui(config)
        # 4 keystrokes consumed → 4 refreshes.
        assert screen.refreshes == 4


class TestRenderGraphTextUnboundedAxis:
    """Voltage / runtime metrics have no configured y_min / y_max."""

    @pytest.mark.unit
    def test_voltage_axis_label_uses_unit_when_unbounded(self, tmp_path):
        from eneru.tui import render_graph_text
        from eneru import (
            Config, UPSConfig, UPSGroupConfig, StatsConfig,
            BehaviorConfig, LoggingConfig, NotificationsConfig,
            LocalShutdownConfig,
        )
        config = Config(
            ups_groups=[UPSGroupConfig(
                ups=UPSConfig(name="UPS@h"),
                is_local=True,
            )],
            behavior=BehaviorConfig(dry_run=True),
            logging=LoggingConfig(
                state_file=str(tmp_path / "state"),
                battery_history_file=str(tmp_path / "history"),
                shutdown_flag_file=str(tmp_path / "flag"),
                file=None,
            ),
            notifications=NotificationsConfig(enabled=False),
            local_shutdown=LocalShutdownConfig(enabled=False),
            statistics=StatsConfig(db_directory=str(tmp_path)),
        )
        # No DB → no data; we just need the header path to compute the
        # axis label from the unit alone (line 1513).
        lines = render_graph_text(
            config, config.ups_groups[0], "voltage", "1h",
        )
        # Header contains the bare unit when y_min/y_max are unbounded.
        assert "(V)" in lines[0]


class TestRunOnceDisplayName:
    """When display_name differs from name, --once prefixes the raw name."""

    @pytest.mark.unit
    def test_display_name_shows_alongside_raw_name(self, tmp_path, capsys):
        from eneru.tui import run_once
        config = Config(
            ups_groups=[UPSGroupConfig(
                ups=UPSConfig(
                    name="ups-a@host",
                    display_name="Rack A",
                ),
                is_local=True,
            )],
            logging=LoggingConfig(state_file=str(tmp_path / "nonexistent")),
        )
        run_once(config)
        out = capsys.readouterr().out
        # Header line: "Rack A  (ups-a@host)  [is_local]  --  daemon not running"
        assert "Rack A" in out
        assert "(ups-a@host)" in out


@pytest.mark.unit
def test_battery_health_alerts_are_power_tier():
    """v6.1 battery-health alerts must be Power-tier (visible at the default
    verbosity), not hidden in Diagnostics."""
    from eneru.tui import (POWER_EVENTS, _event_tier, _event_enabled,
                           EVENT_SECTION_POWER, EVENTS_VERBOSITY_POWER)
    for ev in ("BATTERY_HEALTH_CRITICAL", "BATTERY_HEALTH_WARNING",
               "BATTERY_REPLACEMENT_PREDICTED"):
        assert ev in POWER_EVENTS
        assert _event_tier(ev) == EVENT_SECTION_POWER
        assert _event_enabled(ev, EVENTS_VERBOSITY_POWER)
