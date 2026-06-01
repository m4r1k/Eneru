"""Unit tests for the v6.0 NUT control wrappers (src/eneru/nut_control.py)."""

import pytest

from eneru import nut_control as nc


@pytest.mark.unit
def test_parse_command_list_skips_header_and_junk():
    text = (
        "Instant commands supported on UPS [dummy]:\n"
        "  beeper.toggle - Toggle the beeper\n"
        "  test.battery.start - Start a battery test\n"
        "  bad name - ignored\n"
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
    assert nc._creds_args("u", "p") == ["-u", "u"]
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
def test_run_instant_command_does_not_put_password_in_argv(monkeypatch):
    captured = {}

    def fake(cmd, password, timeout=10):
        captured["cmd"] = cmd
        captured["password"] = password
        return 0, "done", ""

    monkeypatch.setattr(nc, "_run_auth_command", fake)
    ok, out, err = nc.run_instant_command("UPS@h", "beeper.toggle", "adm", "pw")
    assert ok and out == "done"
    assert captured["cmd"] == ["upscmd", "-u", "adm", "UPS@h", "beeper.toggle"]
    assert "pw" not in captured["cmd"]
    assert captured["password"] == "pw"


@pytest.mark.unit
def test_run_instant_command_failure(monkeypatch):
    monkeypatch.setattr(nc, "_run_auth_command",
                        lambda cmd, password, timeout=10: (1, "", "access denied"))
    ok, out, err = nc.run_instant_command("UPS@h", "x", "u", "p")
    assert ok is False and "access denied" in err


@pytest.mark.unit
def test_run_instant_command_failure_uses_pty_output_when_stderr_empty(monkeypatch):
    monkeypatch.setattr(nc, "_run_auth_command", lambda cmd, password, timeout=10: (
        1, "Unexpected response from upsd: ERR CMD-NOT-SUPPORTED", ""))
    ok, out, err = nc.run_instant_command("UPS@h", "x", "u", "p")
    assert ok is False
    assert out == "Unexpected response from upsd: ERR CMD-NOT-SUPPORTED"
    assert "CMD-NOT-SUPPORTED" in err


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
def test_set_variable_does_not_put_password_in_argv(monkeypatch):
    captured = {}

    def fake(cmd, password, timeout=10):
        captured["cmd"] = cmd
        captured["password"] = password
        return 0, "", ""

    monkeypatch.setattr(nc, "_run_auth_command", fake)
    ok, _, _ = nc.set_variable("UPS@h", "input.transfer.low", "200", "adm", "pw")
    assert ok
    assert captured["cmd"] == ["upsrw", "-s", "input.transfer.low=200",
                               "-u", "adm", "UPS@h"]
    assert "pw" not in captured["cmd"]
    assert captured["password"] == "pw"


@pytest.mark.unit
def test_set_variable_failure(monkeypatch):
    monkeypatch.setattr(nc, "_run_auth_command",
                        lambda cmd, password, timeout=10: (1, "", "out of range"))
    ok, _, err = nc.set_variable("UPS@h", "v", "x", "u", "p")
    assert ok is False and "out of range" in err


@pytest.mark.unit
def test_set_variable_failure_uses_pty_output_when_stderr_empty(monkeypatch):
    monkeypatch.setattr(nc, "_run_auth_command", lambda cmd, password, timeout=10: (
        1, "Unexpected response from upsd: ERR ACCESS-DENIED", ""))
    ok, out, err = nc.set_variable("UPS@h", "v", "x", "u", "p")
    assert ok is False
    assert out == "Unexpected response from upsd: ERR ACCESS-DENIED"
    assert "ACCESS-DENIED" in err


@pytest.mark.unit
def test_run_auth_command_without_password_uses_regular_runner(monkeypatch):
    captured = {}

    def fake(cmd, timeout=10):
        captured["cmd"] = cmd
        captured["timeout"] = timeout
        return 0, "ok", ""

    monkeypatch.setattr(nc, "run_command", fake)
    code, out, err = nc._run_auth_command(["upscmd", "UPS@h", "cmd"], "", timeout=7)
    assert (code, out, err) == (0, "ok", "")
    assert captured == {"cmd": ["upscmd", "UPS@h", "cmd"], "timeout": 7}


@pytest.mark.unit
def test_run_auth_command_rejects_upscmd_username_without_password():
    code, out, err = nc._run_auth_command(
        ["upscmd", "-u", "adm", "UPS@h", "cmd"], "", timeout=1)

    assert (code, out) == (2, "")
    assert "username requires password" in err


@pytest.mark.unit
def test_run_auth_command_accepts_upsrw_without_username(monkeypatch):
    captured = []

    def fake(cmd, timeout=10):
        captured.append(cmd)
        return 0, "ok", ""

    monkeypatch.setattr(nc, "run_command", fake)

    assert nc._run_auth_command(
        ["upsrw", "-s", "input.transfer.low=200", "UPS@h"], "", timeout=1
    ) == (0, "ok", "")
    assert captured == [
        ["upsrw", "-s", "input.transfer.low=200", "UPS@h"],
    ]


@pytest.mark.unit
def test_run_auth_command_rejects_upsrw_username_without_password():
    code, out, err = nc._run_auth_command(
        ["upsrw", "-s", "input.transfer.low=200", "-u", "adm", "UPS@h"],
        "",
        timeout=1,
    )

    assert (code, out) == (2, "")
    assert "username requires password" in err


@pytest.mark.unit
@pytest.mark.parametrize(
    ("cmd", "message"),
    [
        (["upscmd", "", "beeper.toggle"], "empty"),
        (["upscmd", "-u", "", "UPS@h", "beeper.toggle"], "empty"),
        (["upscmd", "-u", "adm", "UPS;id", "beeper.toggle"], "unsupported"),
        (["upscmd", "-u", "adm", "UPS@h", ""], "empty"),
        (["upsrw", "-s", "", "UPS@h"], "empty"),
        (["upsrw", "-s", "input.transfer.low=200", ""], "empty"),
        (["upsrw", "-s", "", "-u", "adm", "UPS@h"], "empty"),
        (["upsrw", "-s", "input.transfer.low=200", "-u", "", "UPS@h"], "empty"),
        (["upsrw", "-s", "input.transfer.low=200", "-u", "adm", ""], "empty"),
    ],
)
def test_validated_auth_command_rejects_each_data_position(cmd, message):
    safe_cmd, err = nc._validated_auth_command_argv(cmd)

    assert safe_cmd is None
    assert message in err


@pytest.mark.unit
def test_validate_auth_command_wrapper_reports_bool_and_error():
    ok, err = nc._validate_auth_command_argv(
        ["upscmd", "-u", "adm", "UPS@h", "beeper.toggle"])
    assert ok is True
    assert err == ""

    ok, err = nc._validate_auth_command_argv(["upscmd", "UPS@h"])
    assert ok is False
    assert "invalid upscmd argument shape" in err


@pytest.mark.unit
def test_run_auth_command_rejects_unexpected_binary():
    code, out, err = nc._run_auth_command(["sh", "-c", "id"], "", timeout=1)
    assert code == 2
    assert out == ""
    assert "unsupported NUT control binary" in err


@pytest.mark.unit
def test_run_auth_command_rejects_option_like_data_arg():
    code, out, err = nc._run_auth_command(
        ["upscmd", "UPS@h", "--help"], "pw", timeout=1)
    assert code == 2
    assert out == ""
    assert "looks like an option" in err


@pytest.mark.unit
def test_run_auth_command_rejects_allowed_option_in_data_position():
    code, out, err = nc._run_auth_command(
        ["upscmd", "UPS@h", "-u"], "pw", timeout=1)
    assert code == 2
    assert out == ""
    assert "looks like an option" in err


@pytest.mark.unit
def test_run_auth_command_rejects_bad_upsrw_shape():
    code, out, err = nc._run_auth_command(
        ["upsrw", "-u", "adm", "-s", "input.transfer.low=200", "UPS@h"],
        "pw",
        timeout=1,
    )
    assert code == 2
    assert out == ""
    assert "invalid upsrw argument shape" in err


@pytest.mark.unit
def test_run_auth_command_rejects_bad_upscmd_shape():
    code, out, err = nc._run_auth_command(
        ["upscmd", "-u", "adm", "UPS@h"], "pw", timeout=1)
    assert code == 2
    assert out == ""
    assert "invalid upscmd argument shape" in err


@pytest.mark.unit
@pytest.mark.parametrize("cmd", [
    ["upscmd", "UPS@h", "beeper.toggle"],
    ["upsrw", "-s", "input.transfer.low=200", "UPS@h"],
])
def test_run_auth_command_rejects_password_without_username(monkeypatch, cmd):
    monkeypatch.setattr(nc.pty, "openpty", lambda: (_ for _ in ()).throw(
        AssertionError("PTY must not open without a username")))

    code, out, err = nc._run_auth_command(cmd, "pw", timeout=1)

    assert (code, out) == (2, "")
    assert "password requires username" in err


@pytest.mark.unit
def test_run_auth_command_rejects_shell_metacharacters():
    code, out, err = nc._run_auth_command(
        ["upscmd", "UPS@h", "beeper.toggle;id"], "pw", timeout=1)
    assert code == 2
    assert out == ""
    assert "unsupported characters" in err


@pytest.mark.unit
@pytest.mark.parametrize("bad", ["", "bad\x00arg"])
def test_run_auth_command_rejects_empty_and_nul_args(bad):
    code, out, err = nc._run_auth_command(["upscmd", "UPS@h", bad], "", timeout=1)
    assert code == 2
    assert out == ""
    assert err


@pytest.mark.unit
def test_run_auth_command_with_password_drives_pty_prompt(monkeypatch):
    from types import SimpleNamespace

    captured = {}
    closed = []
    writes = []

    class FakeProc:
        def __init__(self):
            self.polls = [None, 0]

        def poll(self):
            return self.polls.pop(0) if self.polls else 0

        def kill(self):
            raise AssertionError("successful command must not be killed")

    def fake_popen(cmd, stdin, stdout, stderr, close_fds):
        captured.update({
            "cmd": cmd, "stdin": stdin, "stdout": stdout,
            "stderr": stderr, "close_fds": close_fds,
        })
        return FakeProc()

    reads = iter([b"Password: ", b"OK\n", OSError("eof")])

    def fake_read(fd, size):
        item = next(reads)
        if isinstance(item, OSError):
            raise item
        return item

    monkeypatch.setattr(nc.pty, "openpty", lambda: (10, 11))
    monkeypatch.setattr(nc.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(nc.os, "close", closed.append)
    monkeypatch.setattr(nc.os, "read", fake_read)
    monkeypatch.setattr(nc.os, "write", lambda fd, data: writes.append((fd, data)))
    monkeypatch.setattr(nc.select, "select", lambda r, w, x, t: (r, [], []))
    monkeypatch.setattr(nc, "time", SimpleNamespace(monotonic=lambda: 0.0))

    code, out, err = nc._run_auth_command(
        ["upscmd", "-u", "adm", "UPS@h", "cmd"], "secret", timeout=1)

    assert (code, err) == (0, "")
    assert "Password:" in out and "OK" in out
    assert captured == {
        "cmd": ["upscmd", "-u", "adm", "UPS@h", "cmd"],
        "stdin": 11,
        "stdout": 11,
        "stderr": 11,
        "close_fds": True,
    }
    assert writes == [(10, b"secret\n")]
    assert closed == [11, 10]


@pytest.mark.unit
def test_run_auth_command_does_not_answer_non_prompt_password_text(monkeypatch):
    from types import SimpleNamespace

    writes = []

    class FakeProc:
        def poll(self):
            return 1

        def kill(self):
            raise AssertionError("completed command must not be killed")

    reads = iter([b"ERR PASSWORD-REQUIRED\n", OSError("eof")])

    def fake_read(fd, size):
        item = next(reads)
        if isinstance(item, OSError):
            raise item
        return item

    monkeypatch.setattr(nc.pty, "openpty", lambda: (10, 11))
    monkeypatch.setattr(nc.subprocess, "Popen", lambda *a, **k: FakeProc())
    monkeypatch.setattr(nc.os, "close", lambda fd: None)
    monkeypatch.setattr(nc.os, "read", fake_read)
    monkeypatch.setattr(nc.os, "write", lambda fd, data: writes.append((fd, data)))
    monkeypatch.setattr(nc.select, "select", lambda r, w, x, t: (r, [], []))
    monkeypatch.setattr(nc, "time", SimpleNamespace(monotonic=lambda: 0.0))

    code, out, err = nc._run_auth_command(
        ["upscmd", "-u", "adm", "UPS@h", "cmd"], "secret", timeout=1)

    assert (code, err) == (1, "")
    assert "PASSWORD-REQUIRED" in out
    assert writes == []


@pytest.mark.unit
@pytest.mark.parametrize(
    ("cmd", "expected"),
    [
        (
            ["upscmd", "-u", "adm", "UPS@h", "beeper.toggle"],
            ["upscmd", "-u", "adm", "UPS@h", "beeper.toggle"],
        ),
        (
            ["upsrw", "-s", "input.transfer.low=200", "-u", "adm", "UPS@h"],
            ["upsrw", "-s", "input.transfer.low=200", "-u", "adm", "UPS@h"],
        ),
    ],
)
def test_run_auth_command_with_password_spawns_each_fixed_shape(
    monkeypatch, cmd, expected,
):
    from types import SimpleNamespace

    captured = {}

    class FakeProc:
        def poll(self):
            return 0

        def kill(self):
            raise AssertionError("completed command must not be killed")

    def fake_popen(cmd, stdin, stdout, stderr, close_fds):
        captured.update({
            "cmd": cmd, "stdin": stdin, "stdout": stdout,
            "stderr": stderr, "close_fds": close_fds,
        })
        return FakeProc()

    monkeypatch.setattr(nc.pty, "openpty", lambda: (10, 11))
    monkeypatch.setattr(nc.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(nc.os, "close", lambda fd: None)
    monkeypatch.setattr(nc.os, "read", lambda fd, size: b"")
    monkeypatch.setattr(nc.select, "select", lambda r, w, x, t: ([], [], []))
    monkeypatch.setattr(nc, "time", SimpleNamespace(monotonic=lambda: 0.0))

    code, out, err = nc._run_auth_command(cmd, "secret", timeout=1)

    assert (code, out, err) == (0, "", "")
    assert captured == {
        "cmd": expected,
        "stdin": 11,
        "stdout": 11,
        "stderr": 11,
        "close_fds": True,
    }


@pytest.mark.unit
def test_run_auth_command_timeout_kills_process_and_closes_fds(monkeypatch):
    from types import SimpleNamespace

    closed = []

    class FakeProc:
        def __init__(self):
            self.killed = False
            self.waited = False

        def poll(self):
            return None

        def kill(self):
            self.killed = True

        def wait(self, timeout=None):
            self.waited = True

    proc = FakeProc()
    calls = {"count": 0}

    def fake_monotonic():
        calls["count"] += 1
        return 0.0 if calls["count"] == 1 else 2.0

    monkeypatch.setattr(nc.pty, "openpty", lambda: (10, 11))
    monkeypatch.setattr(nc.subprocess, "Popen", lambda *a, **k: proc)
    monkeypatch.setattr(nc.os, "close", closed.append)
    monkeypatch.setattr(nc, "time", SimpleNamespace(monotonic=fake_monotonic))

    code, out, err = nc._run_auth_command(
        ["upscmd", "-u", "adm", "UPS@h", "cmd"], "secret", timeout=1)

    assert (code, out, err) == (124, "", "Command timed out")
    assert proc.killed is True
    assert proc.waited is True
    assert closed == [11, 10]


@pytest.mark.unit
def test_run_auth_command_timeout_swallows_kill_errors(monkeypatch):
    from types import SimpleNamespace

    class FakeProc:
        def poll(self):
            return None

        def kill(self):
            raise RuntimeError("already gone")

    calls = {"count": 0}

    def fake_monotonic():
        calls["count"] += 1
        return 0.0 if calls["count"] == 1 else 2.0

    monkeypatch.setattr(nc.pty, "openpty", lambda: (10, 11))
    monkeypatch.setattr(nc.subprocess, "Popen", lambda *a, **k: FakeProc())
    monkeypatch.setattr(nc.os, "close", lambda fd: None)
    monkeypatch.setattr(nc, "time", SimpleNamespace(monotonic=fake_monotonic))

    code, out, err = nc._run_auth_command(
        ["upscmd", "-u", "adm", "UPS@h", "cmd"], "secret", timeout=1)

    assert (code, out, err) == (124, "", "Command timed out")


@pytest.mark.unit
def test_run_auth_command_read_error_and_close_error_are_swallowed(monkeypatch):
    from types import SimpleNamespace

    class FakeProc:
        def __init__(self):
            self.polls = [0]

        def poll(self):
            return self.polls.pop(0) if self.polls else 0

        def kill(self):
            raise AssertionError("completed command must not be killed")

    reads = iter([OSError("eio"), b""])
    closed = []

    def fake_read(fd, size):
        item = next(reads)
        if isinstance(item, OSError):
            raise item
        return item

    def fake_close(fd):
        if fd == 10:
            raise OSError("close failed")
        closed.append(fd)

    monkeypatch.setattr(nc.pty, "openpty", lambda: (10, 11))
    monkeypatch.setattr(nc.subprocess, "Popen", lambda *a, **k: FakeProc())
    monkeypatch.setattr(nc.os, "close", fake_close)
    monkeypatch.setattr(nc.os, "read", fake_read)
    monkeypatch.setattr(nc.select, "select", lambda r, w, x, t: (r, [], []))
    monkeypatch.setattr(nc, "time", SimpleNamespace(monotonic=lambda: 0.0))

    code, out, err = nc._run_auth_command(
        ["upscmd", "-u", "adm", "UPS@h", "cmd"], "secret")

    assert (code, out, err) == (0, "", "")
    assert closed == [11]


@pytest.mark.unit
def test_run_auth_command_no_readable_fd_still_drains_final_output(monkeypatch):
    from types import SimpleNamespace

    class FakeProc:
        def poll(self):
            return 0

        def kill(self):
            raise AssertionError("completed command must not be killed")

    reads = iter([b"final output\n", b""])

    monkeypatch.setattr(nc.pty, "openpty", lambda: (10, 11))
    monkeypatch.setattr(nc.subprocess, "Popen", lambda *a, **k: FakeProc())
    monkeypatch.setattr(nc.os, "close", lambda fd: None)
    monkeypatch.setattr(nc.os, "read", lambda fd, size: next(reads))
    monkeypatch.setattr(nc.select, "select", lambda r, w, x, t: ([], [], []))
    monkeypatch.setattr(nc, "time", SimpleNamespace(monotonic=lambda: 0.0))

    code, out, err = nc._run_auth_command(
        ["upscmd", "-u", "adm", "UPS@h", "cmd"], "secret")

    assert (code, out, err) == (0, "final output\n", "")


@pytest.mark.unit
def test_run_auth_command_file_not_found_closes_fds(monkeypatch):
    closed = []

    def missing(*args, **kwargs):
        raise FileNotFoundError("upscmd missing")

    monkeypatch.setattr(nc.pty, "openpty", lambda: (10, 11))
    monkeypatch.setattr(nc.subprocess, "Popen", missing)
    monkeypatch.setattr(nc.os, "close", closed.append)

    code, out, err = nc._run_auth_command(
        ["upscmd", "-u", "adm", "UPS@h", "cmd"], "secret")

    assert code == 127
    assert out == ""
    assert "upscmd missing" in err
    assert closed == [10, 11]


@pytest.mark.unit
def test_run_auth_command_generic_exception_returns_output(monkeypatch):
    monkeypatch.setattr(nc.pty, "openpty", lambda: (_ for _ in ()).throw(
        RuntimeError("pty gone")))

    code, out, err = nc._run_auth_command(
        ["upscmd", "-u", "adm", "UPS@h", "cmd"], "secret")

    assert code == 1
    assert out == ""
    assert "pty gone" in err
