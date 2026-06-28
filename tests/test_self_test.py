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
        ("No test initiated", "unsupported"),
        ("not supported", "unsupported"),
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
        monkeypatch.setattr(self_test.nutctl, "list_commands",
                            lambda ups, timeout=10: (True, ["test.battery.start",
                                                            "beeper.toggle"], ""))
        assert self_test.discover_self_test_command(
            "U@h", "test.battery.start") == "test.battery.start"

    @pytest.mark.unit
    def test_none_when_not_exposed(self, monkeypatch):
        monkeypatch.setattr(self_test.nutctl, "list_commands",
                            lambda ups, timeout=10: (True, ["beeper.toggle"], ""))
        assert self_test.discover_self_test_command(
            "U@h", "test.battery.start") is None

    @pytest.mark.unit
    def test_none_when_list_fails(self, monkeypatch):
        monkeypatch.setattr(self_test.nutctl, "list_commands",
                            lambda ups, timeout=10: (False, [], "upscmd error"))
        assert self_test.discover_self_test_command(
            "U@h", "test.battery.start") is None


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
