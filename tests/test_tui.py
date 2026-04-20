"""Tests for TUI dashboard (eneru monitor)."""

import pytest
import tempfile
import os
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
