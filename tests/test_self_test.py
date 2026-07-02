"""Unit tests for scheduled self-test logic (src/eneru/self_test.py).

nut_control I/O is mocked (the dummy driver has no INSTCMD; real-hardware
coverage is the maintainer's)."""

import pytest

from eneru import self_test
from eneru.config import NutControlConfig
from eneru.stats import StatsStore


@pytest.fixture
def store(tmp_path):
    s = StatsStore(tmp_path / "st.db")
    s.open()
    yield s
    s.close()


def _nc(**kw):
    return NutControlConfig(**{"enabled": True, "username": "u", "password": "p",
                              "allowed_commands": ["test.battery.start"],
                              "timeout": 5, **kw})


# --------------------------------------------------------------------------
# normalize_result
# --------------------------------------------------------------------------

class TestNormalize:
    @pytest.mark.unit
    @pytest.mark.parametrize("raw,expected", [
        ("Done and passed", "passed"),
        ("OK", "passed"),
        ("done", "passed"),
        ("Battery test failed", "failed"),
        ("In progress", "running"),
        ("test pending", "running"),
        # "no test initiated" = nothing has run yet -> unknown (NOT unsupported).
        ("No test initiated", "unknown"),
        ("Done: No test initiated", "unknown"),
        ("not supported", "unsupported"),
        ("Self-test unsupported", "unsupported"),
        ("", "unknown"),
        (None, "unknown"),
        ("weird vendor string", "unknown"),
    ])
    def test_normalize(self, raw, expected):
        assert self_test.normalize_result(raw) == expected


# --------------------------------------------------------------------------
# discover
# --------------------------------------------------------------------------

class TestDiscover:
    @pytest.mark.unit
    def test_returns_command_when_exposed(self, monkeypatch):
        monkeypatch.setattr(
            self_test.nutctl, "list_commands",
            lambda ups, username="", password="", timeout=10:
                (True, ["test.battery.start", "beeper.toggle"], ""))
        assert self_test.discover_self_test_command(
            "U@h", "test.battery.start") == "test.battery.start"

    @pytest.mark.unit
    def test_none_when_not_exposed(self, monkeypatch):
        monkeypatch.setattr(
            self_test.nutctl, "list_commands",
            lambda ups, username="", password="", timeout=10:
                (True, ["beeper.toggle"], ""))
        assert self_test.discover_self_test_command(
            "U@h", "test.battery.start") is None

    @pytest.mark.unit
    def test_list_failure_raises_unavailable(self, monkeypatch):
        # A transient upscmd -l failure must be distinguishable from "command
        # genuinely not exposed" (None), so callers can retry instead of skipping
        # a whole 30-day cadence.
        monkeypatch.setattr(
            self_test.nutctl, "list_commands",
            lambda ups, username="", password="", timeout=10:
                (False, [], "upscmd error"))
        with pytest.raises(self_test.SelfTestUnavailable):
            self_test.discover_self_test_command("U@h", "test.battery.start")

    @pytest.mark.unit
    def test_forwards_credentials_to_list(self, monkeypatch):
        # Regression: some upsd only list commands to a logged-in client, so
        # discovery MUST forward nut_control credentials to `upscmd -l`.
        captured = {}

        def fake_list(ups, username="", password="", timeout=10):
            captured.update(ups=ups, username=username, password=password,
                            timeout=timeout)
            return True, ["test.battery.start"], ""

        monkeypatch.setattr(self_test.nutctl, "list_commands", fake_list)
        self_test.discover_self_test_command(
            "U@h", "test.battery.start",
            username="mon-user", password="mon-pass", timeout=7)  # noqa: S106
        assert captured == {"ups": "U@h", "username": "mon-user",
                            "password": "mon-pass", "timeout": 7}


# --------------------------------------------------------------------------
# list_supported_commands / test_command_candidates (v6.1.4 "did you mean")
# --------------------------------------------------------------------------

class TestCandidates:
    @pytest.mark.unit
    def test_list_supported_returns_full_list(self, monkeypatch):
        monkeypatch.setattr(
            self_test.nutctl, "list_commands",
            lambda ups, username="", password="", timeout=10:
                (True, ["beeper.toggle", "test.battery.start.quick"], ""))
        assert self_test.list_supported_commands("U@h") == [
            "beeper.toggle", "test.battery.start.quick"]

    @pytest.mark.unit
    def test_list_supported_raises_on_transient_failure(self, monkeypatch):
        monkeypatch.setattr(
            self_test.nutctl, "list_commands",
            lambda ups, username="", password="", timeout=10: (False, [], "boom"))
        with pytest.raises(self_test.SelfTestUnavailable):
            self_test.list_supported_commands("U@h")

    @pytest.mark.unit
    def test_candidates_are_startable_tests_only(self):
        # APC-style list: quick/deep are candidates; stop (ends a test) and
        # non-test commands are not.
        cmds = ["beeper.enable", "load.off", "test.battery.start.deep",
                "test.battery.start.quick", "test.battery.stop"]
        assert self_test.test_command_candidates(cmds) == [
            "test.battery.start.deep", "test.battery.start.quick"]

    @pytest.mark.unit
    def test_candidates_empty_when_no_tests(self):
        assert self_test.test_command_candidates(["beeper.toggle"]) == []
        assert self_test.test_command_candidates(None) == []


# --------------------------------------------------------------------------
# self_test_control (v6.1.2 narrow permission)
# --------------------------------------------------------------------------

class _ST:
    def __init__(self, enabled):
        self.enabled = enabled


class TestSelfTestControl:
    @pytest.mark.unit
    def test_self_test_enabled_auto_allows_command(self):
        # nut_control off + command NOT allowlisted, but self_test on -> permitted,
        # and the effective nut_control has the command added to its allowlist.
        nc = _nc(enabled=False, allowed_commands=[])
        permitted, eff = self_test.self_test_control(
            nc, _ST(True), "test.battery.start")
        assert permitted is True
        assert "test.battery.start" in eff.allowed_commands
        # The general control surface is untouched: original object unchanged.
        assert nc.allowed_commands == []

    @pytest.mark.unit
    def test_self_test_disabled_falls_back_to_general_allowlist(self):
        # self_test off: permitted only when the GENERAL control surface allows it.
        nc = _nc(enabled=True, allowed_commands=["test.battery.start"])
        permitted, eff = self_test.self_test_control(
            nc, _ST(False), "test.battery.start")
        assert permitted is True
        assert eff is nc  # unchanged

    @pytest.mark.unit
    def test_disabled_and_not_allowlisted_is_denied(self):
        nc = _nc(enabled=False, allowed_commands=[])
        permitted, _eff = self_test.self_test_control(
            nc, _ST(False), "test.battery.start")
        assert permitted is False

    @pytest.mark.unit
    def test_general_control_off_denies_even_if_allowlisted(self):
        # allowlisted but nut_control disabled + self_test disabled -> denied.
        nc = _nc(enabled=False, allowed_commands=["test.battery.start"])
        permitted, _eff = self_test.self_test_control(
            nc, _ST(False), "test.battery.start")
        assert permitted is False


# --------------------------------------------------------------------------
# issue
# --------------------------------------------------------------------------

class TestIssue:
    @pytest.mark.unit
    def test_rejects_non_allowlisted_without_running(self, store, monkeypatch):
        ran = []
        monkeypatch.setattr(self_test.nutctl, "run_instant_command",
                            lambda *a, **k: ran.append(a) or (True, "", ""))
        r = self_test.issue_self_test("U@h", "test.battery.start",
                                      _nc(allowed_commands=[]), store)
        assert r["ok"] is False and "allowed_commands" in r["error"]
        assert ran == []                  # never executed
        assert store.latest_self_test() is None  # no row recorded

    @pytest.mark.unit
    def test_success_records_running_row(self, store, monkeypatch):
        monkeypatch.setattr(self_test.nutctl, "run_instant_command",
                            lambda *a, **k: (True, "Initiating test", ""))
        r = self_test.issue_self_test("U@h", "test.battery.start", _nc(), store,
                                      source="api")
        assert r["ok"] is True and r["test_id"] is not None
        latest = store.latest_self_test()
        assert latest["result_enum"] == "running"
        assert latest["source"] == "api"

    @pytest.mark.unit
    def test_failure_marks_row_failed(self, store, monkeypatch):
        monkeypatch.setattr(self_test.nutctl, "run_instant_command",
                            lambda *a, **k: (False, "", "access denied"))
        r = self_test.issue_self_test("U@h", "test.battery.start", _nc(), store)
        assert r["ok"] is False
        assert store.latest_self_test()["result_enum"] == "failed"

    @pytest.mark.unit
    def test_passes_creds_to_nut_control(self, store, monkeypatch):
        captured = {}

        def fake_run(ups, cmd, user, pw, *, timeout=10):
            captured.update(ups=ups, cmd=cmd, user=user, pw=pw, timeout=timeout)
            return True, "", ""

        monkeypatch.setattr(self_test.nutctl, "run_instant_command", fake_run)
        self_test.issue_self_test("U@h", "test.battery.start",
                                  _nc(username="bob", password="s3cret",  # noqa: S106
                                      timeout=9), store)
        assert captured == {"ups": "U@h", "cmd": "test.battery.start",
                            "user": "bob", "pw": "s3cret", "timeout": 9}


# --------------------------------------------------------------------------
# record result
# --------------------------------------------------------------------------

class TestRecordResult:
    @pytest.mark.unit
    def test_record_updates_row(self, store):
        tid = store.record_self_test("test.battery.start", "scheduler")
        enum = self_test.record_self_test_result(
            store, tid, "Done and passed", "2026-06-28")
        assert enum == "passed"
        latest = store.latest_self_test()
        assert latest["result_enum"] == "passed"
        assert latest["result_raw"] == "Done and passed"
        assert latest["result_date"] == "2026-06-28"

    @pytest.mark.unit
    def test_record_no_store_safe(self):
        assert self_test.record_self_test_result(None, None, "OK", None) == "passed"


# --------------------------------------------------------------------------
# schedule parsing
# --------------------------------------------------------------------------

class TestParseSchedule:
    @pytest.mark.unit
    def test_calendar_schedules(self):
        assert self_test.parse_schedule("daily", "03:00").kind == "daily"
        assert self_test.parse_schedule("weekly", "03:00").kind == "weekly"
        assert self_test.parse_schedule("monthly", "03:00").kind == "monthly"

    @pytest.mark.unit
    def test_calendar_never_fires_on_first(self):
        assert self_test.parse_schedule("monthly", "03:00").fire_on_first is False

    @pytest.mark.unit
    @pytest.mark.parametrize("spec,seconds", [
        ("every 30d", 30 * 86400),
        ("every 12h", 12 * 3600),
        ("every 90m", 90 * 60),
    ])
    def test_interval_schedules(self, spec, seconds):
        s = self_test.parse_schedule(spec, "03:00")
        assert s.kind == "interval" and s.interval_seconds == seconds
        assert s.fire_on_first is False

    @pytest.mark.unit
    @pytest.mark.parametrize("bad", ["hourly", "every", "every 0d", "every 5x",
                                     "every abcd"])
    def test_invalid_schedule_raises(self, bad):
        with pytest.raises(ValueError):
            self_test.parse_schedule(bad, "03:00")
