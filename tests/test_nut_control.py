"""Unit tests for the v6.0 NUT control wrappers (src/eneru/nut_control.py)."""

import pytest

from eneru import nut_control as nc


@pytest.mark.unit
def test_parse_command_list_skips_header_and_junk():
    text = (
        "Instant commands supported on UPS [dummy]:\n"
        "  beeper.toggle - Toggle the beeper\n"
        "  test.battery.start - Start a battery test\n"
        "noise line without separator\n"
    )
    assert nc._parse_command_list(text) == ["beeper.toggle", "test.battery.start"]


@pytest.mark.unit
def test_parse_variable_list_blocks():
    text = (
        "[input.transfer.low]\nLow transfer\nType: STRING\nValue: 196\n"
        "[input.transfer.high]\nHigh transfer\nType: STRING\nValue: 264\n"
    )
    out = nc._parse_variable_list(text)
    assert out == [
        {"name": "input.transfer.low", "type": "STRING", "value": "196"},
        {"name": "input.transfer.high", "type": "STRING", "value": "264"},
    ]


@pytest.mark.unit
def test_parse_variable_list_ignores_orphan_lines():
    # Lines before any [var] header must not crash or attach anywhere.
    assert nc._parse_variable_list("Value: stray\nType: STRING\n") == []


@pytest.mark.unit
def test_creds_args():
    assert nc._creds_args("u", "p") == ["-u", "u", "-p", "p"]
    assert nc._creds_args("", "") == []


@pytest.mark.unit
def test_list_commands_success(monkeypatch):
    monkeypatch.setattr(nc, "run_command",
                        lambda cmd, timeout=10: (0, "  beeper.toggle - x\n", ""))
    ok, commands, err = nc.list_commands("UPS@h")
    assert ok and commands == ["beeper.toggle"] and err == ""


@pytest.mark.unit
def test_list_commands_failure(monkeypatch):
    monkeypatch.setattr(nc, "run_command",
                        lambda cmd, timeout=10: (1, "", "driver not connected"))
    ok, commands, err = nc.list_commands("UPS@h")
    assert ok is False and commands == [] and "driver not connected" in err


@pytest.mark.unit
def test_run_instant_command_builds_argv(monkeypatch):
    captured = {}

    def fake(cmd, timeout=10):
        captured["cmd"] = cmd
        return 0, "done", ""

    monkeypatch.setattr(nc, "run_command", fake)
    ok, out, err = nc.run_instant_command("UPS@h", "beeper.toggle", "adm", "pw")
    assert ok and out == "done"
    assert captured["cmd"] == ["upscmd", "-u", "adm", "-p", "pw",
                               "UPS@h", "beeper.toggle"]


@pytest.mark.unit
def test_run_instant_command_failure(monkeypatch):
    monkeypatch.setattr(nc, "run_command",
                        lambda cmd, timeout=10: (1, "", "access denied"))
    ok, out, err = nc.run_instant_command("UPS@h", "x", "u", "p")
    assert ok is False and "access denied" in err


@pytest.mark.unit
def test_list_variables_success_and_failure(monkeypatch):
    monkeypatch.setattr(nc, "run_command", lambda cmd, timeout=10: (
        0, "[input.transfer.low]\nType: STRING\nValue: 196\n", ""))
    ok, variables, _ = nc.list_variables("UPS@h")
    assert ok and variables[0]["name"] == "input.transfer.low"

    monkeypatch.setattr(nc, "run_command", lambda cmd, timeout=10: (1, "", "boom"))
    ok, variables, err = nc.list_variables("UPS@h")
    assert ok is False and variables == [] and "boom" in err


@pytest.mark.unit
def test_set_variable_builds_argv(monkeypatch):
    captured = {}

    def fake(cmd, timeout=10):
        captured["cmd"] = cmd
        return 0, "", ""

    monkeypatch.setattr(nc, "run_command", fake)
    ok, _, _ = nc.set_variable("UPS@h", "input.transfer.low", "200", "adm", "pw")
    assert ok
    assert captured["cmd"] == ["upsrw", "-s", "input.transfer.low=200",
                               "-u", "adm", "-p", "pw", "UPS@h"]


@pytest.mark.unit
def test_set_variable_failure(monkeypatch):
    monkeypatch.setattr(nc, "run_command",
                        lambda cmd, timeout=10: (1, "", "out of range"))
    ok, _, err = nc.set_variable("UPS@h", "v", "x", "u", "p")
    assert ok is False and "out of range" in err
