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
    mon._last_health_update_mono = None
    mon._self_test_pending_id = None
    mon._self_test_poll_due_mono = None
    mon._poll_target = cfg.ups.name
    mon.logs = []
    mon.notifications = []
    mon._log_message = lambda m: mon.logs.append(m)
    mon._send_notification = (
        lambda body, ntype, category="general":
        mon.notifications.append((body, ntype, category)))
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
        seen = {}
        monkeypatch.setattr(monitor_mod.reports_mod, "maybe_send_due_reports",
                            lambda c, s, name, enq, **k: seen.setdefault("name", name) or ["daily"])
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
        mon._enqueue_report("body", "info", "report")
        assert mon.notifications == [("body", "info", "report")]


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
    def test_nut_control_disabled_is_defense_in_depth(self, store):
        # self_test enabled but nut_control disabled -> never issue (the per-UPS
        # override case validation can't catch).
        cfg = _cfg("self_test:\n  enabled: true\n  command: test.battery.start\n"
                   "ups:\n  name: U@h\n")
        mon = _make_monitor(cfg, store)
        mon._run_self_test_task()
        assert store.latest_self_test() is None

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
    def test_pending_poll_not_yet_due(self, store):
        mon = _make_monitor(_cfg(_ENABLED), store)
        mon._self_test_pending_id = 3
        mon._self_test_poll_due_mono = time.monotonic() + 1000   # not yet
        mon._run_self_test_task()
        assert mon._self_test_pending_id == 3      # still pending, nothing issued
