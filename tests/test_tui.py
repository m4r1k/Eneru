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
