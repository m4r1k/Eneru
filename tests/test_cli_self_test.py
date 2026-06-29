"""Unit tests for the `eneru self-test run|status` CLI (src/eneru/cli.py, B6).

The daemon API and NUT I/O are mocked: the dummy driver has no INSTCMD, and
these tests exercise the CLI plumbing (target resolution, URL/token handling,
API vs --direct dispatch, status read-out), not real hardware.
"""

import argparse
from types import SimpleNamespace

import pytest
import yaml

from eneru import cli
from eneru.config import ConfigLoader


def _config(text):
    return ConfigLoader._parse_config(yaml.safe_load(text))


SINGLE = (
    "ups:\n  name: 'UPS@host'\n"
    "api:\n  enabled: true\n  bind: '127.0.0.1'\n  port: 9191\n"
    "nut_control:\n  enabled: true\n  username: u\n  password: p\n"
    "  allowed_commands: [test.battery.start]\n"
    "self_test:\n  command: test.battery.start\n"
)
MULTI = (
    "ups:\n  - name: 'UPS-A@h'\n  - name: 'UPS-B@h'\n"
)


def _args(**kw):
    base = dict(ups=None, config=None, direct=False, url=None, token=None, api_key=None)
    base.update(kw)
    return argparse.Namespace(**base)


class _FakeStore:
    """Minimal stand-in for an OPEN StatsStore (just needs ``close()``)."""
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

class TestFindGroup:
    @pytest.mark.unit
    def test_default_single_ups(self):
        cfg = _config(SINGLE)
        assert cli._self_test_find_group(cfg, None).ups.name == "UPS@host"

    @pytest.mark.unit
    def test_exact_and_sanitized_match(self):
        cfg = _config(MULTI)
        assert cli._self_test_find_group(cfg, "UPS-B@h").ups.name == "UPS-B@h"
        # sanitized form (@ -> -) resolves to the same group
        assert cli._self_test_find_group(cfg, "UPS-B-h").ups.name == "UPS-B@h"

    @pytest.mark.unit
    def test_missing_and_ambiguous_return_none(self):
        assert cli._self_test_find_group(_config(SINGLE), "nope") is None
        assert cli._self_test_find_group(_config(MULTI), None) is None


class TestApiBaseAndToken:
    @pytest.mark.unit
    def test_url_override_wins(self):
        cfg = _config(SINGLE)
        assert cli._self_test_api_base(cfg, _args(url="http://x:1/")) == "http://x:1"

    @pytest.mark.unit
    def test_wildcard_bind_becomes_loopback(self):
        cfg = _config("ups:\n  name: U@h\napi:\n  bind: '0.0.0.0'\n  port: 9000\n")
        assert cli._self_test_api_base(cfg, _args()) == "http://127.0.0.1:9000"

    @pytest.mark.unit
    def test_normal_bind(self):
        cfg = _config(SINGLE)
        assert cli._self_test_api_base(cfg, _args()) == "http://127.0.0.1:9191"

    @pytest.mark.unit
    def test_ipv6_bind_is_bracketed(self):
        cfg = _config("ups:\n  name: U@h\napi:\n  bind: '::1'\n  port: 9191\n")
        assert cli._self_test_api_base(cfg, _args()) == "http://[::1]:9191"

    @pytest.mark.unit
    def test_token_precedence(self, monkeypatch):
        monkeypatch.delenv("ENERU_API_TOKEN", raising=False)
        monkeypatch.delenv("ENERU_API_KEY", raising=False)
        assert cli._self_test_token(_args(token="t", api_key="k")) == "t"
        assert cli._self_test_token(_args(api_key="k")) == "k"
        monkeypatch.setenv("ENERU_API_TOKEN", "envtok")
        assert cli._self_test_token(_args()) == "envtok"


class TestErrorMessage:
    @pytest.mark.unit
    def test_shapes(self):
        assert cli._error_message({"error": {"message": "boom"}}) == "boom"
        assert cli._error_message({"error": "plain"}) == "plain"
        assert cli._error_message({"x": 1}) is None
        assert cli._error_message("not a dict") is None


# --------------------------------------------------------------------------
# run — API path
# --------------------------------------------------------------------------

class TestRunApi:
    @pytest.mark.unit
    def test_success(self, monkeypatch, capsys):
        monkeypatch.setattr(cli, "_load_config", lambda a: _config(SINGLE))
        seen = {}

        def fake_http(method, url, token=None, body=None, timeout=15):
            seen.update(method=method, url=url, token=token)
            return 202, {"status": "issued"}
        monkeypatch.setattr(cli, "_http_json", fake_http)
        cli._cmd_self_test_run(_args(token="tok"))
        out = capsys.readouterr().out
        assert "issued on UPS@host via the daemon API" in out
        assert seen["method"] == "POST"
        assert seen["url"].endswith("/api/v1/ups/UPS%40host/self-test")
        assert seen["token"] == "tok"

    @pytest.mark.unit
    def test_no_token_exits(self, monkeypatch):
        monkeypatch.setattr(cli, "_load_config", lambda a: _config(SINGLE))
        monkeypatch.delenv("ENERU_API_TOKEN", raising=False)
        monkeypatch.delenv("ENERU_API_KEY", raising=False)
        with pytest.raises(SystemExit) as e:
            cli._cmd_self_test_run(_args())
        assert e.value.code == 2

    @pytest.mark.unit
    def test_http_error_exits(self, monkeypatch, capsys):
        monkeypatch.setattr(cli, "_load_config", lambda a: _config(SINGLE))
        monkeypatch.setattr(cli, "_http_json",
                            lambda *a, **k: (403, {"error": {"message": "forbidden"}}))
        with pytest.raises(SystemExit) as e:
            cli._cmd_self_test_run(_args(token="tok"))
        assert e.value.code == 1
        assert "forbidden" in capsys.readouterr().out

    @pytest.mark.unit
    def test_unreachable_exits(self, monkeypatch, capsys):
        monkeypatch.setattr(cli, "_load_config", lambda a: _config(SINGLE))
        monkeypatch.setattr(cli, "_http_json",
                            lambda *a, **k: (0, {"error": {"message": "refused"}}))
        with pytest.raises(SystemExit) as e:
            cli._cmd_self_test_run(_args(token="tok"))
        assert e.value.code == 1
        assert "Could not reach" in capsys.readouterr().out

    @pytest.mark.unit
    def test_bad_ups_exits(self, monkeypatch):
        monkeypatch.setattr(cli, "_load_config", lambda a: _config(MULTI))
        with pytest.raises(SystemExit) as e:
            cli._cmd_self_test_run(_args(ups="ghost", token="t"))
        assert e.value.code == 2

    @pytest.mark.unit
    def test_ambiguous_no_ups_exits(self, monkeypatch, capsys):
        # Multi-UPS with no --ups -> can't pick one -> exit 2 with a hint.
        monkeypatch.setattr(cli, "_load_config", lambda a: _config(MULTI))
        with pytest.raises(SystemExit) as e:
            cli._cmd_self_test_run(_args())
        assert e.value.code == 2
        assert "specify which with --ups" in capsys.readouterr().out


# --------------------------------------------------------------------------
# run — --direct path
# --------------------------------------------------------------------------

class TestRunDirect:
    @pytest.mark.unit
    def test_success(self, monkeypatch, capsys):
        monkeypatch.setattr(cli, "_load_config", lambda a: _config(SINGLE))
        monkeypatch.setattr(cli, "_open_stats_store", lambda c, g: _FakeStore())
        from eneru import self_test as st
        monkeypatch.setattr(st, "discover_self_test_command",
                            lambda *a, **k: "test.battery.start")
        monkeypatch.setattr(st, "issue_self_test",
                            lambda *a, **k: {"ok": True, "test_id": 1, "error": ""})
        cli._cmd_self_test_run(_args(direct=True))
        assert "issued on UPS@host (command test.battery.start)" in capsys.readouterr().out

    @pytest.mark.unit
    def test_no_stats_db_refuses(self, monkeypatch, capsys):
        # --direct must NOT issue a test when the stats DB is unavailable: the
        # result could never be recorded/finalised, so it exits non-zero and
        # does not call issue_self_test.
        monkeypatch.setattr(cli, "_load_config", lambda a: _config(SINGLE))
        monkeypatch.setattr(cli, "_open_stats_store", lambda c, g: None)
        from eneru import self_test as st
        monkeypatch.setattr(st, "discover_self_test_command",
                            lambda *a, **k: "test.battery.start")
        called = {"issued": False}
        monkeypatch.setattr(
            st, "issue_self_test",
            lambda *a, **k: called.__setitem__("issued", True))
        with pytest.raises(SystemExit) as e:
            cli._cmd_self_test_run(_args(direct=True))
        assert e.value.code == 1
        assert called["issued"] is False
        assert "stats DB is unavailable" in capsys.readouterr().out

    @pytest.mark.unit
    def test_nut_control_disabled_exits(self, monkeypatch):
        cfg = _config("ups:\n  name: U@h\nself_test:\n  command: test.battery.start\n")
        monkeypatch.setattr(cli, "_load_config", lambda a: cfg)
        with pytest.raises(SystemExit) as e:
            cli._cmd_self_test_run(_args(direct=True))
        assert e.value.code == 2

    @pytest.mark.unit
    def test_command_not_allowlisted_exits(self, monkeypatch):
        cfg = _config(
            "ups:\n  name: U@h\n"
            "nut_control:\n  enabled: true\n  allowed_commands: [beeper.toggle]\n"
            "self_test:\n  command: test.battery.start\n")
        monkeypatch.setattr(cli, "_load_config", lambda a: cfg)
        with pytest.raises(SystemExit) as e:
            cli._cmd_self_test_run(_args(direct=True))
        assert e.value.code == 2

    @pytest.mark.unit
    def test_command_not_exposed_exits(self, monkeypatch):
        monkeypatch.setattr(cli, "_load_config", lambda a: _config(SINGLE))
        from eneru import self_test as st
        monkeypatch.setattr(st, "discover_self_test_command", lambda *a, **k: None)
        with pytest.raises(SystemExit) as e:
            cli._cmd_self_test_run(_args(direct=True))
        assert e.value.code == 1

    @pytest.mark.unit
    def test_direct_uses_per_ups_command(self, monkeypatch):
        cfg = _config(
            "api:\n  auth:\n    enabled: true\n"
            "nut_control:\n  enabled: true\n  allowed_commands: [test.battery.start.quick]\n"
            "self_test:\n  command: test.battery.start\n"
            "ups:\n  - name: U1@h\n    self_test:\n      command: test.battery.start.quick\n")
        monkeypatch.setattr(cli, "_load_config", lambda a: cfg)
        monkeypatch.setattr(cli, "_open_stats_store", lambda c, g: _FakeStore())
        from eneru import self_test as st
        seen = {}

        def _disc(name, cmd, **k):
            seen["cmd"] = cmd
            return cmd
        monkeypatch.setattr(st, "discover_self_test_command", _disc)
        monkeypatch.setattr(st, "issue_self_test",
                            lambda *a, **k: {"ok": True, "test_id": 1, "error": ""})
        cli._cmd_self_test_run(_args(ups="U1@h", direct=True))
        assert seen["cmd"] == "test.battery.start.quick"   # per-UPS override, not global

    @pytest.mark.unit
    def test_direct_per_ups_cannot_enable_when_global_off(self, monkeypatch):
        # A per-UPS nut_control block can NEVER enable control when the global
        # gate is off — --direct must force `enabled` from the GLOBAL config,
        # mirroring the API's _effective_nut_control. So this exits (code 2)
        # even though the per-UPS block says enabled: true.
        cfg = _config(
            "api:\n  auth:\n    enabled: true\n"
            "nut_control:\n  enabled: false\n"
            "  allowed_commands: [test.battery.start]\n"
            "self_test:\n  command: test.battery.start\n"
            "ups:\n  - name: U1@h\n"
            "    nut_control:\n      enabled: true\n"
            "      allowed_commands: [test.battery.start]\n")
        monkeypatch.setattr(cli, "_load_config", lambda a: cfg)
        called = {"discover": False}
        from eneru import self_test as st
        monkeypatch.setattr(
            st, "discover_self_test_command",
            lambda *a, **k: called.__setitem__("discover", True) or "x")
        with pytest.raises(SystemExit) as e:
            cli._cmd_self_test_run(_args(ups="U1@h", direct=True))
        assert e.value.code == 2
        assert called["discover"] is False   # never got past the enabled gate

    @pytest.mark.unit
    def test_direct_per_ups_override_inherits_global_enabled(self, monkeypatch):
        # The mirror case: global ON + a per-UPS override that does NOT set
        # enabled still issues (enabled forced from the global ON gate), and the
        # per-UPS allowlist/creds are used as-is.
        cfg = _config(
            "api:\n  auth:\n    enabled: true\n"
            "nut_control:\n  enabled: true\n  username: g\n  password: g\n"
            "  allowed_commands: [test.battery.start]\n"
            "self_test:\n  command: test.battery.start\n"
            "ups:\n  - name: U1@h\n"
            "    nut_control:\n      username: u1\n      password: p1\n"
            "      allowed_commands: [test.battery.start]\n")
        monkeypatch.setattr(cli, "_load_config", lambda a: cfg)
        monkeypatch.setattr(cli, "_open_stats_store", lambda c, g: _FakeStore())
        from eneru import self_test as st
        seen = {}
        monkeypatch.setattr(st, "discover_self_test_command",
                            lambda *a, **k: "test.battery.start")

        def _issue(name, cmd, nc, store, **k):
            seen["enabled"] = nc.enabled
            seen["username"] = nc.username
            return {"ok": True, "test_id": 1, "error": ""}
        monkeypatch.setattr(st, "issue_self_test", _issue)
        cli._cmd_self_test_run(_args(ups="U1@h", direct=True))
        assert seen["enabled"] is True          # forced from global ON
        assert seen["username"] == "u1"         # per-UPS creds used as-is

    @pytest.mark.unit
    def test_issue_failure_exits(self, monkeypatch, capsys):
        monkeypatch.setattr(cli, "_load_config", lambda a: _config(SINGLE))
        monkeypatch.setattr(cli, "_open_stats_store", lambda c, g: _FakeStore())
        from eneru import self_test as st
        monkeypatch.setattr(st, "discover_self_test_command",
                            lambda *a, **k: "test.battery.start")
        monkeypatch.setattr(st, "issue_self_test",
                            lambda *a, **k: {"ok": False, "test_id": None, "error": "nope"})
        with pytest.raises(SystemExit) as e:
            cli._cmd_self_test_run(_args(direct=True))
        assert e.value.code == 1
        assert "Self-test failed: nope" in capsys.readouterr().out


# --------------------------------------------------------------------------
# status
# --------------------------------------------------------------------------

class TestStatus:
    @pytest.mark.unit
    def test_prints_latest_row(self, monkeypatch, capsys):
        monkeypatch.setattr(cli, "_load_config", lambda a: _config(SINGLE))
        row = {"id": 3, "started_ts": 1_700_000_000, "command": "test.battery.start",
               "result_raw": "Done and passed", "result_enum": "passed",
               "result_date": "2026-06-28", "source": "cli"}
        monkeypatch.setattr(cli, "_open_stats_store",
                            lambda c, g: SimpleNamespace(latest_self_test=lambda: row,
                                                         close=lambda: None))
        cli._cmd_self_test_status(_args())
        out = capsys.readouterr().out
        assert "Latest self-test for UPS@host" in out
        assert "passed" in out and "Done and passed" in out
        assert "test.battery.start" in out and "source : cli" in out.lower()

    @pytest.mark.unit
    def test_no_row(self, monkeypatch, capsys):
        monkeypatch.setattr(cli, "_load_config", lambda a: _config(SINGLE))
        monkeypatch.setattr(cli, "_open_stats_store",
                            lambda c, g: SimpleNamespace(latest_self_test=lambda: None,
                                                         close=lambda: None))
        cli._cmd_self_test_status(_args())
        assert "No self-test on record" in capsys.readouterr().out

    @pytest.mark.unit
    def test_status_opens_real_store(self, tmp_path, monkeypatch, capsys):
        # Exercise the real _open_stats_store + stats_db_path_for_group path
        # (single-UPS -> default.db under db_directory); empty store -> no record.
        cfg = _config(f"ups:\n  name: 'U@h'\n"
                      f"statistics:\n  db_directory: '{tmp_path}'\n")
        monkeypatch.setattr(cli, "_load_config", lambda a: cfg)
        cli._cmd_self_test_status(_args())
        assert "No self-test on record" in capsys.readouterr().out

    @pytest.mark.unit
    def test_status_reads_seeded_row(self, tmp_path, monkeypatch, capsys):
        # Proves _open_stats_store actually open()s the DB: a pre-seeded row in
        # the same default.db must be read back (an unopened store no-ops -> None).
        from eneru.stats import StatsStore
        from eneru.status import stats_db_path_for_group
        cfg = _config(f"ups:\n  name: 'U@h'\n"
                      f"statistics:\n  db_directory: '{tmp_path}'\n")
        monkeypatch.setattr(cli, "_load_config", lambda a: cfg)
        db = stats_db_path_for_group(cfg, cfg.ups_groups[0])
        seed = StatsStore(db)
        seed.open()
        tid = seed.record_self_test("test.battery.start", "cli", result_enum="running")
        seed.update_self_test_result(tid, result_raw="Done and passed", result_enum="passed")
        seed.close()
        cli._cmd_self_test_status(_args())
        out = capsys.readouterr().out
        assert "Latest self-test for U@h" in out and "passed" in out

    @pytest.mark.unit
    def test_bad_ups(self, monkeypatch):
        monkeypatch.setattr(cli, "_load_config", lambda a: _config(MULTI))
        with pytest.raises(SystemExit) as e:
            cli._cmd_self_test_status(_args(ups="ghost"))
        assert e.value.code == 2


# --------------------------------------------------------------------------
# _http_json (stdlib client) — mock urllib
# --------------------------------------------------------------------------

class TestHttpJson:
    @pytest.mark.unit
    def test_ok(self, monkeypatch):
        import urllib.request

        class _Resp:
            status = 200
            def read(self): return b'{"ok": true}'
            def __enter__(self): return self
            def __exit__(self, *a): return False
        monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _Resp())
        status, data = cli._http_json("POST", "http://x/y", token="t", body={})
        assert status == 200 and data == {"ok": True}

    @pytest.mark.unit
    def test_http_error(self, monkeypatch):
        import urllib.error
        import urllib.request

        def boom(*a, **k):
            raise urllib.error.HTTPError("http://x", 500, "err", {},
                                         __import__("io").BytesIO(b'{"error":{"message":"x"}}'))
        monkeypatch.setattr(urllib.request, "urlopen", boom)
        status, data = cli._http_json("GET", "http://x/y")
        assert status == 500 and data["error"]["message"] == "x"

    @pytest.mark.unit
    def test_http_error_non_json_body(self, monkeypatch):
        import io
        import urllib.error
        import urllib.request

        def boom(*a, **k):
            raise urllib.error.HTTPError("http://x", 502, "bad gateway", {},
                                         io.BytesIO(b"<html>nope</html>"))
        monkeypatch.setattr(urllib.request, "urlopen", boom)
        status, data = cli._http_json("GET", "http://x/y")
        assert status == 502 and "nope" in data["error"]["message"]

    @pytest.mark.unit
    def test_url_error(self, monkeypatch):
        import urllib.error
        import urllib.request

        def boom(*a, **k):
            raise urllib.error.URLError("refused")
        monkeypatch.setattr(urllib.request, "urlopen", boom)
        status, data = cli._http_json("GET", "http://x/y")
        assert status == 0 and "refused" in data["error"]["message"]
