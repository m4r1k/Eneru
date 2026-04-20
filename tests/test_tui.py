"""Tests for TUI dashboard (eneru monitor)."""

import pytest
import tempfile
import os
import time
from pathlib import Path
from io import StringIO
from unittest.mock import patch

from eneru import Config, UPSConfig, UPSGroupConfig, LoggingConfig
from eneru.tui import (
    parse_state_file,
    parse_log_events,
    human_status,
    status_color,
    collect_group_data,
    run_once,
    C_STATUS_OK, C_STATUS_OB, C_STATUS_CRIT, C_STATUS_UNK,
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
        path.write_text(
            "ups.status=OL CHRG\n"
            f"battery.charge={charge}\n"
            "battery.runtime=1800\n"
            "ups.load=30\n"
            f"input.voltage={voltage}\n"
            "output.voltage=230\n"
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
        lines = query_events_for_display(config, time_range_seconds=3600)
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
        lines = query_events_for_display(config, time_range_seconds=3600)
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
        _seed_events(config, config.ups_groups[0],
                     [(now - 100, "A", ""), (now - 20, "C", "")])
        _seed_events(config, config.ups_groups[1],
                     [(now - 60, "B", "")])
        lines = query_events_for_display(config, time_range_seconds=3600)
        # Order: A (UPS1), B (UPS2), C (UPS1)
        assert "[UPS1@h] A" in lines[0]
        assert "[UPS2@h] B" in lines[1]
        assert "[UPS1@h] C" in lines[2]

    @pytest.mark.unit
    def test_outside_time_window_excluded(self, tmp_path):
        from eneru.tui import query_events_for_display
        import time as _time
        config = _events_config(tmp_path)
        now = int(_time.time())
        _seed_events(config, config.ups_groups[0], [
            (now - 7200, "OLD", "two hours ago"),
            (now - 60, "RECENT", "one minute ago"),
        ])
        # 1-hour window -> only RECENT included.
        lines = query_events_for_display(config, time_range_seconds=3600)
        assert len(lines) == 1
        assert "RECENT" in lines[0]

    @pytest.mark.unit
    def test_max_events_caps_results(self, tmp_path):
        from eneru.tui import query_events_for_display
        import time as _time
        config = _events_config(tmp_path)
        now = int(_time.time())
        _seed_events(config, config.ups_groups[0], [
            (now - i, f"EVT{i}", "") for i in range(1, 11)
        ])
        lines = query_events_for_display(
            config, time_range_seconds=3600, max_events=3,
        )
        # Most recent 3 events.
        assert len(lines) == 3


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
