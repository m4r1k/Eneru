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
    collect_group_data,
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
        events = ["10:00:00  POWER EVENT: 🔋 battery low"]
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


class TestHumanStatus:
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


class TestStatusColor:
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
        # priority_only=False — this test exercises sort/interleave for
        # arbitrary event types, not the priority filter itself.
        _seed_events(config, config.ups_groups[0],
                     [(now - 100, "A", ""), (now - 20, "C", "")])
        _seed_events(config, config.ups_groups[1],
                     [(now - 60, "B", "")])
        lines = query_events_for_display(config, priority_only=False)
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
        # priority_only=False — testing the row cap, not the type filter.
        lines = query_events_for_display(
            config, max_events=3, priority_only=False,
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
    def test_priority_only_default_filters_chatter(self, tmp_path):
        """Default ``priority_only=True`` keeps the panel focused on
        daemon-lifecycle and power transitions even when the events
        table is full of low-priority chatter."""
        from eneru.tui import query_events_for_display
        import time as _time
        config = _events_config(tmp_path)
        now = int(_time.time())
        events = []
        # 30 chatter rows that should be filtered.
        for i in range(30):
            events.append(
                (now - 100 + i, "VOLTAGE_FLAP_SUPPRESSED", f"flap {i}")
            )
        # 2 priority rows that must survive the filter.
        events.append((now - 50, "DAEMON_START", "v1"))
        events.append((now - 5, "POWER_RESTORED", "Outage 12s"))
        _seed_events(config, config.ups_groups[0], events)
        lines = query_events_for_display(config, max_events=8)
        # Both priority rows present, no VOLTAGE_FLAP_SUPPRESSED lines.
        assert any("DAEMON_START" in line for line in lines)
        assert any("POWER_RESTORED" in line for line in lines)
        assert all("VOLTAGE_FLAP_SUPPRESSED" not in line for line in lines)

    @pytest.mark.unit
    def test_verbose_includes_low_priority(self, tmp_path):
        """``priority_only=False`` (set by ``--verbose`` / ``<V>``)
        widens the filter to all event types."""
        from eneru.tui import query_events_for_display
        import time as _time
        config = _events_config(tmp_path)
        now = int(_time.time())
        _seed_events(config, config.ups_groups[0], [
            (now - 100, "VOLTAGE_FLAP_SUPPRESSED", "flap"),
            (now - 50, "DAEMON_START", "v1"),
        ])
        lines = query_events_for_display(config, priority_only=False)
        assert len(lines) == 2
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
        # Seed 600 priority events -- comfortably above the new default cap of 30.
        _seed_events(config, config.ups_groups[0], [
            (now - 600 + i, "DAEMON_START", f"row-{i}")
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

        lines = query_events_for_display(config, max_events=20)
        # All 3 power-event rows MUST survive the cap.
        assert any("ON_BATTERY: real outage" in l for l in lines), (
            f"ON_BATTERY pushed off by daemon noise -- tiered trim regressed. "
            f"Got: {lines}"
        )
        assert any("POWER_RESTORED: outage 10m" in l for l in lines)
        assert any("EMERGENCY_SHUTDOWN_INITIATED" in l for l in lines)
        # Result respects the cap.
        assert len(lines) == 20
        # Remaining 17 slots are most-recent daemon rows.
        daemon_lines = [l for l in lines if "DAEMON_RESTARTED" in l]
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
        assert all("ON_BATTERY" in l for l in lines)
        assert any("outage-49" in l for l in lines)  # latest power event
        assert any("outage-40" in l for l in lines)  # 10th-latest


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
        out = _sanitize_event_detail("📦 **Eneru Upgraded** v5.2.0 → v5.2.1")
        assert "**" not in out
        assert "📦 Eneru Upgraded v5.2.0 → v5.2.1" == out

    @pytest.mark.unit
    def test_collapses_embedded_newline(self):
        """Embedded `\\n` mid-string used to make `curses.addstr` jump to
        a new row before the gold background fill completed, leaving
        cells unpainted (the 'broken colors' visible on the
        'Service is back online' continuation row)."""
        from eneru.tui import _sanitize_event_detail
        out = _sanitize_event_detail(
            "📦 **Eneru Upgraded** v5.2.0 → v5.2.1\n"
            "Service is back online with the new version."
        )
        assert "\n" not in out
        assert "**" not in out
        assert "📦 Eneru Upgraded v5.2.0 → v5.2.1" in out
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
            detail=("📦 **Eneru Upgraded** v5.2.0 → v5.2.1\n"
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
            "2026-04-20 10:00:00 - ⚡ POWER EVENT: ON_BATTERY - 85%\n"
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
        assert "(no events)" in out

    @pytest.mark.unit
    def test_snapshot_path_honours_verbose_flag(self, tmp_path, capsys):
        """5.2.2 (cubic.dev / CodeRabbit P1): the non-events-only branch
        of run_once also reads events for its tail block. Pre-fix it
        used the default ``priority_only=True`` and silently ignored
        ``--verbose``, so chatter never surfaced even when the user
        explicitly asked for it."""
        from eneru.tui import run_once
        import time as _time
        config = _events_config(tmp_path)
        now = int(_time.time())
        _seed_events(config, config.ups_groups[0], [
            (now - 60, "VOLTAGE_FLAP_SUPPRESSED", "low-prio chatter"),
            (now - 30, "DAEMON_START", "high-prio"),
        ])
        # Default snapshot path: chatter hidden.
        run_once(config, events_only=False)
        out_default = capsys.readouterr().out
        assert "DAEMON_START" in out_default
        assert "VOLTAGE_FLAP_SUPPRESSED" not in out_default
        # --verbose: chatter must surface.
        run_once(config, events_only=False, verbose=True)
        out_verbose = capsys.readouterr().out
        assert "DAEMON_START" in out_verbose
        assert "VOLTAGE_FLAP_SUPPRESSED" in out_verbose, (
            "snapshot path must honour --verbose; pre-5.2.2 the events "
            "tail in the snapshot block silently ignored the flag"
        )

    @pytest.mark.unit
    def test_snapshot_path_no_time_window_for_events(self, tmp_path, capsys):
        """5.2.2: events have no time window -- ``--time`` only affects
        graphs. The snapshot tail must surface old DAEMON rows even
        though they fall outside any ``--time`` choice."""
        from eneru.tui import run_once
        import time as _time
        config = _events_config(tmp_path)
        now = int(_time.time())
        old_ts = now - 90 * 86400  # 3 months ago
        _seed_events(config, config.ups_groups[0], [
            (old_ts, "DAEMON_START", "ancient"),
            (now - 60, "DAEMON_START", "recent"),
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
    def test_events_only_length_caps_output(self, tmp_path, capsys):
        """``--length N`` (events_only=True path) caps output to N rows."""
        from eneru.tui import run_once
        import time as _time
        config = _events_config(tmp_path)
        now = int(_time.time())
        _seed_events(config, config.ups_groups[0], [
            (now - 100 + i, "DAEMON_START", f"row-{i}") for i in range(50)
        ])
        run_once(config, events_only=True, length=5)
        out = capsys.readouterr().out
        # Should see exactly 5 lines -- the most-recent 5 DAEMON_START rows.
        lines = [l for l in out.splitlines() if "DAEMON_START" in l]
        assert len(lines) == 5
        assert "row-49" in lines[-1]
        assert "row-45" in lines[0]

    @pytest.mark.unit
    def test_events_only_length_zero_means_no_cap(self, tmp_path, capsys):
        """``--length 0`` returns every priority row."""
        from eneru.tui import run_once
        import time as _time
        config = _events_config(tmp_path)
        now = int(_time.time())
        _seed_events(config, config.ups_groups[0], [
            (now - 100 + i, "DAEMON_START", f"row-{i}") for i in range(40)
        ])
        run_once(config, events_only=True, length=0)
        out = capsys.readouterr().out
        lines = [l for l in out.splitlines() if "DAEMON_START" in l]
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
        evt = "⚡⚡⚡ POWER EVENT: ON_BATTERY - " + "🔋" * 10
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
