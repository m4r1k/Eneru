"""Tests for the v6.1 end-of-loop periodic orchestration on UPSGroupMonitor
(monitor.py: _run_periodic_tasks / _run_self_test_task / _enqueue_report and the
per-UPS self_test / nut_control resolvers).

These methods are time-gated and fully failure-isolated, so they are exercised
through a lightweight ``object.__new__`` harness with mocked NUT/selftest I/O
rather than a live daemon.
"""

import time

import pytest
import yaml

from eneru import monitor as monitor_mod
from eneru import self_test as selftest
from eneru.config import ConfigLoader, NutControlConfig
from eneru.monitor import UPSGroupMonitor
from eneru.state import MonitorState
from eneru.stats import StatsStore


def _cfg(text):
    return ConfigLoader._parse_config(yaml.safe_load(text))


@pytest.fixture
def store(tmp_path):
    s = StatsStore(tmp_path / "p.db")
    s.open()
    yield s
    s.close()


def _make_monitor(cfg, store=None, *, coordinator_mode=False):
    mon = object.__new__(UPSGroupMonitor)
    mon.config = cfg
    mon.state = MonitorState()
    mon._stats_store = store
    mon._coordinator_mode = coordinator_mode
    mon._notification_worker = None
    mon._last_health_update_mono = None
    mon._self_test_pending_id = None
    mon._self_test_poll_due_mono = None
    mon._self_test_retry_after_mono = None
    mon._poll_target = cfg.ups.name
    mon.logs = []
    mon.notifications = []
    mon._log_message = lambda m: mon.logs.append(m)
    mon._send_notification = (
        lambda body, ntype, category="general", **kwargs:
        mon.notifications.append((body, ntype, category, kwargs)) or 1)
    mon._get_ups_var = lambda var: None
    return mon


_ENABLED = (
    "api:\n  auth:\n    enabled: true\n"
    "nut_control:\n  enabled: true\n  allowed_commands: [test.battery.start]\n"
    "self_test:\n  enabled: true\n  schedule: monthly\n  command: test.battery.start\n"
    "  result_poll_after: 60\n"
    "ups:\n  name: U@h\n"
)


# --------------------------------------------------------------------------
# _run_periodic_tasks: time-gating + failure isolation
# --------------------------------------------------------------------------

class TestRunPeriodicTasks:
    @pytest.mark.unit
    def test_battery_health_runs_first_time_then_gates(self, store):
        cfg = _cfg("ups:\n  name: U@h\nbattery_health:\n  update_interval: 3600\n")
        mon = _make_monitor(cfg, store)
        calls = []
        mon._update_battery_health_periodic = lambda *a: calls.append(1)
        mon._run_periodic_tasks()
        assert calls == [1]                       # first sight runs
        assert mon._last_health_update_mono is not None
        mon._run_periodic_tasks()
        assert calls == [1]                       # within interval -> gated

    @pytest.mark.unit
    def test_battery_health_disabled_skips(self, store):
        cfg = _cfg("ups:\n  name: U@h\nbattery_health:\n  enabled: false\n")
        mon = _make_monitor(cfg, store)
        calls = []
        mon._update_battery_health_periodic = lambda *a: calls.append(1)
        mon._run_periodic_tasks()
        assert calls == []

    @pytest.mark.unit
    def test_battery_health_failure_is_isolated(self, store):
        cfg = _cfg("ups:\n  name: U@h\n")
        mon = _make_monitor(cfg, store)

        def boom(*a):
            raise RuntimeError("kaboom")
        mon._update_battery_health_periodic = boom
        # Must not raise — the poll loop can never be interrupted by this.
        mon._run_periodic_tasks()
        assert any("battery-health task failed" in m for m in mon.logs)

    @pytest.mark.unit
    def test_reports_sent_when_enabled_and_not_coordinator(self, store, monkeypatch):
        cfg = _cfg("ups:\n  name: U@h\n"
                   "reports:\n  enabled: true\n  daily: true\n")
        mon = _make_monitor(cfg, store)
        mon._update_battery_health_periodic = lambda *a: None
        mon._notification_worker = object()  # ISS-023: reports need a worker
        seen = {}

        def _fake_reports(c, s, name, enq, **k):
            seen["name"] = name
            return ["daily"]
        monkeypatch.setattr(monitor_mod.reports_mod, "maybe_send_due_reports",
                            _fake_reports)
        mon._run_periodic_tasks()
        assert seen["name"] == "U@h"

    @pytest.mark.unit
    def test_reports_skipped_in_coordinator_mode(self, store, monkeypatch):
        cfg = _cfg("ups:\n  name: U@h\nreports:\n  enabled: true\n  daily: true\n")
        mon = _make_monitor(cfg, store, coordinator_mode=True)
        mon._update_battery_health_periodic = lambda *a: None
        called = []
        monkeypatch.setattr(monitor_mod.reports_mod, "maybe_send_due_reports",
                            lambda *a, **k: called.append(1) or [])
        mon._run_periodic_tasks()
        assert called == []          # coordinator owns the single daemon-wide digest

    @pytest.mark.unit
    def test_reports_failure_is_isolated(self, store, monkeypatch):
        cfg = _cfg("ups:\n  name: U@h\nreports:\n  enabled: true\n  daily: true\n")
        mon = _make_monitor(cfg, store)
        mon._update_battery_health_periodic = lambda *a: None
        mon._notification_worker = object()  # ISS-023: reports need a worker

        def boom(*a, **k):
            raise RuntimeError("report-boom")
        monkeypatch.setattr(monitor_mod.reports_mod, "maybe_send_due_reports", boom)
        mon._run_periodic_tasks()
        assert any("reports task failed" in m for m in mon.logs)

    @pytest.mark.unit
    def test_self_test_failure_is_isolated(self, store, monkeypatch):
        cfg = _cfg(_ENABLED)
        mon = _make_monitor(cfg, store)
        mon._update_battery_health_periodic = lambda *a: None

        def boom():
            raise RuntimeError("st-boom")
        mon._run_self_test_task = boom
        mon._run_periodic_tasks()
        assert any("self-test task failed" in m for m in mon.logs)


# --------------------------------------------------------------------------
# _enqueue_report
# --------------------------------------------------------------------------

class TestEnqueueReport:
    @pytest.mark.unit
    def test_routes_to_send_notification(self):
        mon = _make_monitor(_cfg("ups:\n  name: U@h\n"))
        assert mon._enqueue_report("body", "info", "report") == 1
        assert mon.notifications == [(
            "body", "info", "report", {"require_persistent": True},
        )]


# --------------------------------------------------------------------------
# resolvers
# --------------------------------------------------------------------------

class TestResolvers:
    @pytest.mark.unit
    def test_self_test_per_ups_override(self):
        cfg = _cfg("self_test:\n  schedule: monthly\n  command: test.battery.start\n"
                   "ups:\n  - name: U1@h\n    self_test:\n      schedule: weekly\n")
        mon = _make_monitor(cfg)
        mon._poll_target = "U1@h"
        # config.ups.name is the first group's name in this multi-UPS config
        assert mon._resolve_self_test_config().schedule == "weekly"

    @pytest.mark.unit
    def test_self_test_global_fallback(self):
        cfg = _cfg("self_test:\n  schedule: daily\n"
                   "ups:\n  - name: U1@h\n")
        mon = _make_monitor(cfg)
        assert mon._resolve_self_test_config().schedule == "daily"

    @pytest.mark.unit
    def test_nut_control_per_ups_override(self):
        cfg = _cfg("nut_control:\n  enabled: false\n"
                   "ups:\n  - name: U1@h\n    nut_control:\n      enabled: true\n"
                   "      allowed_commands: [beeper.toggle]\n")
        mon = _make_monitor(cfg)
        resolved = mon._resolve_nut_control_config()
        assert resolved.enabled is True
        assert resolved.allowed_commands == ["beeper.toggle"]

    @pytest.mark.unit
    def test_nut_control_global_fallback(self):
        cfg = _cfg("nut_control:\n  enabled: true\n  allowed_commands: [x]\n"
                   "ups:\n  - name: U1@h\n")
        mon = _make_monitor(cfg)
        assert mon._resolve_nut_control_config().allowed_commands == ["x"]


# --------------------------------------------------------------------------
# _run_self_test_task
# --------------------------------------------------------------------------

class TestRunSelfTestTask:
    @pytest.mark.unit
    def test_disabled_returns_early(self, store):
        mon = _make_monitor(_cfg("ups:\n  name: U@h\n"), store)  # self_test off
        mon._run_self_test_task()
        assert mon.logs == []

    @pytest.mark.unit
    def test_issues_without_nut_control_enabled(self, store, monkeypatch):
        # v6.1.2: self_test is its own permission — enabling it grants exactly
        # its command even when nut_control is disabled. When DUE, it issues, and
        # the effective nut_control handed to issue_self_test has the command
        # auto-allowlisted.
        cfg = _cfg("api:\n  auth:\n    enabled: true\n"
                   "self_test:\n  enabled: true\n  command: test.battery.start\n"
                   "ups:\n  name: U@h\n")
        mon = _make_monitor(cfg, store)
        store.set_meta("self_test_last_run", "0")     # due now
        captured = {}
        monkeypatch.setattr(selftest, "discover_self_test_command",
                            lambda *a, **k: "test.battery.start")

        def _issue(ups, cmd, nc, s, source="scheduler"):
            captured["allowed"] = list(nc.allowed_commands)
            return {"ok": True, "test_id": 5, "error": ""}
        monkeypatch.setattr(selftest, "issue_self_test", _issue)
        mon._run_self_test_task()
        assert mon._self_test_pending_id == 5
        assert "test.battery.start" in captured["allowed"]

    @pytest.mark.unit
    def test_discovery_receives_nut_control_credentials(self, store, monkeypatch):
        # Regression: discovery must forward credentials so `upscmd -l` works on
        # an upsd that only lists commands to a logged-in client.
        cfg = _cfg("api:\n  auth:\n    enabled: true\n"
                   "nut_control:\n  enabled: true\n"
                   "  username: mon-user\n  password: mon-pass\n"
                   "  allowed_commands: [test.battery.start]\n"
                   "self_test:\n  enabled: true\n  command: test.battery.start\n"
                   "ups:\n  name: U@h\n")
        mon = _make_monitor(cfg, store)
        store.set_meta("self_test_last_run", "0")     # due now
        seen = {}

        def _discover(ups, command, *, username="", password="", timeout=10):
            seen.update(username=username, password=password)
            return command
        monkeypatch.setattr(selftest, "discover_self_test_command", _discover)
        monkeypatch.setattr(selftest, "issue_self_test",
                            lambda *a, **k: {"ok": True, "test_id": 1, "error": ""})
        mon._run_self_test_task()
        assert seen == {"username": "mon-user", "password": "mon-pass"}

    @pytest.mark.unit
    def test_first_sight_seeds_baseline(self, store):
        mon = _make_monitor(_cfg(_ENABLED), store)
        mon._run_self_test_task()
        # monthly + fire_on_first=False -> not due on first sight -> seed, no issue
        assert store.get_meta("self_test_last_run") is not None
        assert mon._self_test_pending_id is None

    @pytest.mark.unit
    def test_due_issues_and_arms_poll(self, store, monkeypatch):
        mon = _make_monitor(_cfg(_ENABLED), store)
        store.set_meta("self_test_last_run", "0")     # last run at epoch -> due now
        monkeypatch.setattr(selftest, "discover_self_test_command",
                            lambda *a, **k: "test.battery.start")
        monkeypatch.setattr(selftest, "issue_self_test",
                            lambda *a, **k: {"ok": True, "test_id": 7, "error": ""})
        mon._run_self_test_task()
        assert mon._self_test_pending_id == 7
        assert mon._self_test_poll_due_mono is not None

    @pytest.mark.unit
    def test_due_but_command_not_exposed(self, store, monkeypatch):
        mon = _make_monitor(_cfg(_ENABLED), store)
        store.set_meta("self_test_last_run", "0")
        monkeypatch.setattr(selftest, "discover_self_test_command", lambda *a, **k: None)
        mon._run_self_test_task()
        assert mon._self_test_pending_id is None
        assert any("not exposed" in m for m in mon.logs)

    @pytest.mark.unit
    def test_due_issue_failure_logs(self, store, monkeypatch):
        mon = _make_monitor(_cfg(_ENABLED), store)
        store.set_meta("self_test_last_run", "0")
        monkeypatch.setattr(selftest, "discover_self_test_command",
                            lambda *a, **k: "test.battery.start")
        monkeypatch.setattr(selftest, "issue_self_test",
                            lambda *a, **k: {"ok": False, "test_id": None, "error": "nope"})
        mon._run_self_test_task()
        assert mon._self_test_pending_id is None
        assert any("self-test issue failed" in m for m in mon.logs)

    @pytest.mark.unit
    def test_failed_issue_backs_off_before_retry(self, store, monkeypatch):
        # A failed issue doesn't burn the cadence (so it retries), but a backoff
        # gate stops it from re-attempting on the very next poll tick — otherwise
        # a persistently-broken config would spawn an upscmd every few seconds.
        mon = _make_monitor(_cfg(_ENABLED), store)
        store.set_meta("self_test_last_run", "0")
        calls = []
        monkeypatch.setattr(selftest, "discover_self_test_command",
                            lambda *a, **k: "test.battery.start")

        def _issue(*a, **k):
            calls.append(1)
            return {"ok": False, "test_id": None, "error": "nope"}
        monkeypatch.setattr(selftest, "issue_self_test", _issue)

        mon._run_self_test_task()                      # attempt 1 fails -> backoff
        assert len(calls) == 1
        assert mon._self_test_retry_after_mono is not None
        assert store.get_meta("self_test_last_run") == "0"   # cadence not burned
        mon._run_self_test_task()                      # within backoff -> gated
        assert len(calls) == 1
        mon._self_test_retry_after_mono = time.monotonic() - 1   # backoff elapsed
        mon._run_self_test_task()                      # retries
        assert len(calls) == 2

    @pytest.mark.unit
    def test_skips_when_poll_target_autocorrected(self, store, monkeypatch):
        # After NUT name autocorrect, the control surface stays on the
        # configured ups.name; a scheduled self-test must NOT fire an INSTCMD at
        # the discovered target. It skips (with backoff) until the config is fixed.
        mon = _make_monitor(_cfg(_ENABLED), store)
        mon._ups_name_autocorrected = True
        store.set_meta("self_test_last_run", "0")        # due
        monkeypatch.setattr(
            selftest, "discover_self_test_command",
            lambda *a, **k: pytest.fail("must not discover when autocorrected"))
        mon._run_self_test_task()
        assert mon._self_test_pending_id is None
        assert mon._self_test_retry_after_mono is not None
        assert any("auto-corrected" in m for m in mon.logs)

    @pytest.mark.unit
    def test_invalid_schedule_logs(self, store):
        cfg = _cfg("api:\n  auth:\n    enabled: true\n"
                   "nut_control:\n  enabled: true\n  allowed_commands: [test.battery.start]\n"
                   "self_test:\n  enabled: true\n  schedule: 'every banana'\n"
                   "  command: test.battery.start\n"
                   "ups:\n  name: U@h\n")
        mon = _make_monitor(cfg, store)
        mon._run_self_test_task()
        assert any("invalid self_test.schedule" in m for m in mon.logs)

    @pytest.mark.unit
    def test_pending_poll_finalizes_result(self, store, monkeypatch):
        mon = _make_monitor(_cfg(_ENABLED), store)
        mon._self_test_pending_id = 3
        mon._self_test_poll_due_mono = time.monotonic() - 1   # poll window elapsed
        mon._get_ups_var = lambda var: {"ups.test.result": "Done and passed",
                                        "ups.test.date": "2026-06-28"}.get(var)
        monkeypatch.setattr(selftest, "record_self_test_result",
                            lambda s, tid, raw, date: "passed")
        mon._run_self_test_task()
        assert mon._self_test_pending_id is None
        assert mon._self_test_poll_due_mono is None
        assert any("Self-test result: passed" in m for m in mon.logs)

    @pytest.mark.unit
    def test_pending_finalized_even_when_config_now_disabled(self, store, monkeypatch):
        # A config reload that DISABLES self_test must not orphan an already
        # issued/pending test: its result is finalised before the disabled
        # config is honored (otherwise the `running` row lives forever).
        mon = _make_monitor(_cfg("ups:\n  name: U@h\n"), store)  # self_test OFF
        mon._self_test_pending_id = 9
        mon._self_test_poll_due_mono = time.monotonic() - 1   # poll window elapsed
        store.set_meta(selftest.PENDING_ID_META, "9")
        store.set_meta(selftest.PENDING_DUE_TS_META, str(int(time.time()) - 1))
        mon._get_ups_var = lambda var: {"ups.test.result": "Done and passed",
                                        "ups.test.date": "2026-06-28"}.get(var)
        monkeypatch.setattr(selftest, "record_self_test_result",
                            lambda s, tid, raw, date: "passed")
        mon._run_self_test_task()
        assert mon._self_test_pending_id is None
        assert mon._self_test_poll_due_mono is None
        assert store.get_meta(selftest.PENDING_ID_META) == ""
        assert store.get_meta(selftest.PENDING_DUE_TS_META) == ""
        assert any("Self-test result: passed" in m for m in mon.logs)

    @pytest.mark.unit
    def test_store_none_skips_entirely(self):
        # No store -> can't dedup/record -> skip rather than fire every tick.
        mon = _make_monitor(_cfg(_ENABLED), None)
        mon._run_self_test_task()
        assert mon._self_test_pending_id is None

    @pytest.mark.unit
    def test_closed_store_skips_entirely(self, tmp_path):
        # A store that exists but is closed (get_meta/set_meta silently no-op)
        # must be treated as unavailable too — otherwise scheduled tests fire
        # without any state tracking. A closed StatsStore reports is_open=False.
        closed = StatsStore(tmp_path / "closed.db")
        closed.open()
        closed.close()
        assert closed.is_open is False
        mon = _make_monitor(_cfg(_ENABLED), closed)
        mon._run_self_test_task()
        assert mon._self_test_pending_id is None

    @pytest.mark.unit
    def test_discovery_unavailable_retries_without_consuming_cycle(self, store, monkeypatch):
        mon = _make_monitor(_cfg(_ENABLED), store)
        store.set_meta("self_test_last_run", "0")     # due now

        def _raise(*a, **k):
            raise selftest.SelfTestUnavailable("nut down")
        monkeypatch.setattr(selftest, "discover_self_test_command", _raise)
        mon._run_self_test_task()
        assert mon._self_test_pending_id is None
        assert any("discovery failed" in m for m in mon.logs)
        # last_run must NOT advance, so the next tick retries instead of waiting
        # a full (possibly 30-day) cadence.
        assert store.get_meta("self_test_last_run") == "0"

    @pytest.mark.unit
    def test_pending_poll_not_yet_due(self, store):
        mon = _make_monitor(_cfg(_ENABLED), store)
        mon._self_test_pending_id = 3
        mon._self_test_poll_due_mono = time.monotonic() + 1000   # not yet
        mon._run_self_test_task()
        assert mon._self_test_pending_id == 3      # still pending, nothing issued

    @pytest.mark.unit
    def test_issue_persists_pending_id_for_restart(self, store, monkeypatch):
        mon = _make_monitor(_cfg(_ENABLED), store)
        store.set_meta("self_test_last_run", "0")
        monkeypatch.setattr(selftest, "discover_self_test_command",
                            lambda *a, **k: "test.battery.start")
        monkeypatch.setattr(selftest, "issue_self_test",
                            lambda *a, **k: {"ok": True, "test_id": 9, "error": ""})
        mon._run_self_test_task()
        assert store.get_meta(selftest.PENDING_ID_META) == "9"
        assert store.get_meta(selftest.PENDING_DUE_TS_META) is not None

    @pytest.mark.unit
    def test_recovers_in_flight_test_after_restart(self, store, monkeypatch):
        # Simulate a restart: a pending id is in meta but the in-memory fields
        # are fresh (None). The next tick must adopt + finalize it.
        store.set_meta(selftest.PENDING_ID_META, "11")
        mon = _make_monitor(_cfg(_ENABLED), store)
        mon._get_ups_var = lambda var: {"ups.test.result": "Done and passed",
                                        "ups.test.date": "2026-06-28"}.get(var)
        recorded = {}

        def _rec(s, tid, raw, date):
            recorded["id"] = tid
            return "passed"
        monkeypatch.setattr(selftest, "record_self_test_result", _rec)
        mon._run_self_test_task()
        assert recorded["id"] == 11                # adopted the persisted id
        assert mon._self_test_pending_id is None   # finalized
        assert store.get_meta(selftest.PENDING_ID_META) == ""   # cleared
        assert store.get_meta(selftest.PENDING_DUE_TS_META) == ""

    @pytest.mark.unit
    def test_recovers_in_flight_test_but_waits_for_due_timestamp(self, store):
        store.set_meta(selftest.PENDING_ID_META, "12")
        store.set_meta(selftest.PENDING_DUE_TS_META, str(int(time.time()) + 600))
        mon = _make_monitor(_cfg(_ENABLED), store)
        mon._get_ups_var = lambda var: pytest.fail("poll should wait for due timestamp")
        mon._run_self_test_task()
        assert mon._self_test_pending_id == 12
        assert store.get_meta(selftest.PENDING_ID_META) == "12"

    @pytest.mark.unit
    def test_adopts_running_api_row_without_pending_meta(self, store, monkeypatch):
        # v6.1.4 API-created rows did not write self_test_pending_* meta. The
        # monitor should still pick up the newest running row and finish it.
        started = int(time.time()) - 120
        tid = store.record_self_test("test.battery.start", "api",
                                     started_ts=started)
        mon = _make_monitor(_cfg(_ENABLED), store)
        mon._get_ups_var = lambda var: {"ups.test.result": "Done and passed",
                                        "ups.test.date": "2026-07-03"}.get(var)
        recorded = {}
        original_record = selftest.record_self_test_result

        def _rec(s, test_id, raw, date):
            recorded["id"] = test_id
            return original_record(s, test_id, raw, date)
        monkeypatch.setattr(selftest, "record_self_test_result", _rec)
        mon._run_self_test_task()
        assert recorded["id"] == tid
        assert store.latest_self_test()["result_enum"] == "passed"
        assert store.get_meta(selftest.PENDING_ID_META) == ""

    @pytest.mark.unit
    def test_corrupt_pending_id_is_cleared(self, store):
        store.set_meta(selftest.PENDING_ID_META, "not-an-int")
        store.set_meta(selftest.PENDING_DUE_TS_META, str(int(time.time()) + 60))
        store.set_meta("self_test_last_run", str(int(time.time())))  # not due -> stop early
        mon = _make_monitor(_cfg(_ENABLED), store)
        mon._run_self_test_task()
        assert store.get_meta(selftest.PENDING_ID_META) == ""   # corrupt value scrubbed
        assert store.get_meta(selftest.PENDING_DUE_TS_META) == ""


# --------------------------------------------------------------------------
# _check_observed_self_test (v6.1.2 passive observation)
# --------------------------------------------------------------------------

class TestObservedSelfTest:
    @pytest.mark.unit
    def test_records_device_result(self, store):
        # A UPS that ran its own test (device schedule / manual) is recorded even
        # with self_test disabled — regardless of whether Eneru schedules tests.
        mon = _make_monitor(_cfg("ups:\n  name: U@h\n"), store)
        mon._check_observed_self_test(
            {"ups.test.result": "done and passed", "ups.test.date": "2026-06-02"})
        latest = store.latest_self_test()
        assert latest["result_enum"] == "passed"
        assert latest["result_date"] == "2026-06-02"
        assert latest["source"] == "device"
        assert latest["command"] == ""       # Eneru issued nothing
        assert any("Observed UPS self-test: passed" in m for m in mon.logs)

    @pytest.mark.unit
    def test_dedups_same_result(self, store):
        mon = _make_monitor(_cfg("ups:\n  name: U@h\n"), store)
        data = {"ups.test.result": "done and passed", "ups.test.date": "2026-06-02"}
        mon._check_observed_self_test(data)
        first = store.latest_self_test()["id"]
        mon._check_observed_self_test(data)               # unchanged -> no new row
        assert store.latest_self_test()["id"] == first

    @pytest.mark.unit
    def test_new_test_records_again(self, store):
        mon = _make_monitor(_cfg("ups:\n  name: U@h\n"), store)
        mon._check_observed_self_test(
            {"ups.test.result": "done and passed", "ups.test.date": "2026-06-02"})
        first = store.latest_self_test()["id"]
        mon._check_observed_self_test(
            {"ups.test.result": "done and passed", "ups.test.date": "2026-07-02"})
        latest = store.latest_self_test()
        assert latest["id"] != first
        assert latest["result_date"] == "2026-07-02"

    @pytest.mark.unit
    @pytest.mark.parametrize("raw", ["in progress", "No test initiated",
                                     "not supported"])
    def test_skips_non_terminal_results(self, store, raw):
        # running/unknown/unsupported are churny or one-time noise — only settled
        # pass/fail is persisted passively.
        mon = _make_monitor(_cfg("ups:\n  name: U@h\n"), store)
        mon._check_observed_self_test({"ups.test.result": raw})
        assert store.latest_self_test() is None

    @pytest.mark.unit
    def test_skips_when_no_result_or_failed_poll(self, store):
        mon = _make_monitor(_cfg("ups:\n  name: U@h\n"), store)
        mon._check_observed_self_test({"battery.charge": "100"})   # UPS reports no test
        mon._check_observed_self_test(None)                        # failed poll
        mon._check_observed_self_test({})                          # empty
        assert store.latest_self_test() is None

    @pytest.mark.unit
    def test_stands_down_while_eneru_test_pending(self, store):
        # The scheduled path owns the row for a test it issued; the observer must
        # not double-record the same result.
        mon = _make_monitor(_cfg("ups:\n  name: U@h\n"), store)
        mon._self_test_pending_id = 42
        mon._check_observed_self_test(
            {"ups.test.result": "done and passed", "ups.test.date": "2026-06-02"})
        assert store.latest_self_test() is None

    @pytest.mark.unit
    def test_records_without_date(self, store):
        # Some UPSes report a result but no ups.test.date.
        mon = _make_monitor(_cfg("ups:\n  name: U@h\n"), store)
        mon._check_observed_self_test({"ups.test.result": "Battery test failed"})
        latest = store.latest_self_test()
        assert latest["result_enum"] == "failed"
        assert latest["result_date"] is None

    @pytest.mark.unit
    def test_record_failure_does_not_fingerprint(self, store, monkeypatch):
        # If the row write fails (record_self_test -> None), the observed key
        # must NOT be stamped, so the next poll retries instead of silently
        # dropping a device result forever.
        mon = _make_monitor(_cfg("ups:\n  name: U@h\n"), store)
        monkeypatch.setattr(store, "record_self_test", lambda *a, **k: None)
        mon._check_observed_self_test(
            {"ups.test.result": "done and passed", "ups.test.date": "2026-06-02"})
        assert store.get_meta("self_test_observed_key") in (None, "")
        # Recovery: once the write succeeds, it records + fingerprints.
        monkeypatch.undo()
        mon._check_observed_self_test(
            {"ups.test.result": "done and passed", "ups.test.date": "2026-06-02"})
        assert store.latest_self_test()["result_enum"] == "passed"
        assert store.get_meta("self_test_observed_key") == "2026-06-02|done and passed"

    @pytest.mark.unit
    def test_no_store_is_safe(self):
        mon = _make_monitor(_cfg("ups:\n  name: U@h\n"), None)
        mon._check_observed_self_test({"ups.test.result": "done and passed"})  # no raise

    @pytest.mark.unit
    def test_observer_failure_isolated(self, store):
        cfg = _cfg("ups:\n  name: U@h\n")
        mon = _make_monitor(cfg, store)
        mon._update_battery_health_periodic = lambda *a: None

        def boom(_data):
            raise RuntimeError("obs-boom")
        mon._check_observed_self_test = boom
        mon._run_periodic_tasks({"ups.test.result": "done and passed"})
        assert any("self-test observer failed" in m for m in mon.logs)

    @pytest.mark.unit
    def test_scheduled_finalize_stamps_observed_key(self, store, monkeypatch):
        # After Eneru finalises its OWN test, the observer must not re-record the
        # same result: the finalise stamps the observer fingerprint.
        mon = _make_monitor(_cfg(_ENABLED), store)
        mon._self_test_pending_id = 3
        mon._self_test_poll_due_mono = time.monotonic() - 1
        mon._get_ups_var = lambda var: {"ups.test.result": "done and passed",
                                        "ups.test.date": "2026-06-02"}.get(var)
        monkeypatch.setattr(selftest, "record_self_test_result",
                            lambda s, tid, raw, date: "passed")
        mon._run_self_test_task()
        assert store.get_meta("self_test_observed_key") == "2026-06-02|done and passed"
        # A subsequent observation of that same result is a no-op.
        before = store.latest_self_test()
        mon._check_observed_self_test(
            {"ups.test.result": "done and passed", "ups.test.date": "2026-06-02"})
        assert store.latest_self_test() == before
