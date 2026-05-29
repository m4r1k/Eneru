"""Tests for the rc2 gap-closure work: per-group nut_control, events-table
audit, subsystem live-reload hooks, and do_DELETE."""

from io import BytesIO
from unittest.mock import MagicMock

import pytest

from eneru import api as apimod
from eneru import reload as reloadmod
from eneru.api import EneruAPIHandler, SessionManager
from eneru.config import Config, ConfigLoader, NutControlConfig
from eneru.monitor import UPSGroupMonitor
from eneru.multi_ups import MultiUPSCoordinator


def _load(tmp_path, text, name="c.yaml"):
    p = tmp_path / name
    p.write_text(text)
    return ConfigLoader.load(str(p))


# ----- per-group nut_control (#2) -----

@pytest.mark.unit
def test_per_group_nut_control_parsed(tmp_path):
    cfg = _load(tmp_path,
                "ups:\n  - name: U1@hostA\n    nut_control:\n"
                "      username: a\n      allowed_commands: [beeper.toggle]\n"
                "  - name: U2@hostB\n")
    g1 = next(g for g in cfg.ups_groups if g.ups.name == "U1@hostA")
    g2 = next(g for g in cfg.ups_groups if g.ups.name == "U2@hostB")
    assert g1.nut_control is not None
    assert g1.nut_control.username == "a"
    assert g1.nut_control.allowed_commands == ["beeper.toggle"]
    assert g2.nut_control is None


@pytest.mark.unit
def test_effective_nut_control_uses_group_override(tmp_path):
    cfg = _load(tmp_path,
                "api:\n  auth:\n    enabled: true\n"
                "nut_control:\n  enabled: true\n  username: glob\n"
                "  password: gpw\n  allowed_commands: [beeper.toggle]\n"
                "ups:\n  - name: U1@hostA\n    nut_control:\n"
                "      username: groupuser\n      allowed_commands: [test.battery.start]\n"
                "  - name: U2@hostB\n")
    h = object.__new__(EneruAPIHandler)
    h.api_config = cfg
    # group with override -> merged (enabled from global, creds/allowlist from group)
    nc1 = h._effective_nut_control("U1@hostA")
    assert nc1.enabled is True
    assert nc1.username == "groupuser"
    assert nc1.allowed_commands == ["test.battery.start"]
    assert nc1.password == "gpw"  # falls back to global
    # group without override -> global
    nc2 = h._effective_nut_control("U2@hostB")
    assert nc2.username == "glob"
    assert nc2.allowed_commands == ["beeper.toggle"]


# ----- events-table audit (#3) -----

@pytest.mark.unit
def test_audit_writes_to_events_via_source():
    h = object.__new__(EneruAPIHandler)
    h.api_log = None
    source = MagicMock()
    h.api_source = source
    h._audit({"username": "alice", "kind": "user"}, "command",
             "UPS@h:beeper.toggle", "ok")
    source.record_control_event.assert_called_once()
    args = source.record_control_event.call_args[0]
    assert args[0] == "UPS@h"            # ups parsed from target
    assert args[1] == "CONTROL_COMMAND"  # mapped event type
    assert "alice" in args[2] and "ok" in args[2]


@pytest.mark.unit
def test_audit_source_without_hook_is_noop():
    h = object.__new__(EneruAPIHandler)
    h.api_log = None
    h.api_source = object()  # no record_control_event
    h._audit({"username": "a", "kind": "user"}, "config", "reload", "ok")  # no raise


@pytest.mark.unit
def test_monitor_record_control_event(tmp_path):
    mon = object.__new__(UPSGroupMonitor)
    mon._stats_store = MagicMock()
    mon.record_control_event("UPS@h", "CONTROL_COMMAND", "alice did x")
    mon._stats_store.log_event.assert_called_once_with("CONTROL_COMMAND", "alice did x")


@pytest.mark.unit
def test_coordinator_record_control_event_routes_to_ups(tmp_path):
    coord = object.__new__(MultiUPSCoordinator)
    m1 = object.__new__(UPSGroupMonitor)
    m1.config = _load(tmp_path, "ups:\n  - name: U1@h\n", "m1.yaml")
    m1._stats_store = MagicMock()
    m2 = object.__new__(UPSGroupMonitor)
    m2.config = _load(tmp_path, "ups:\n  - name: U2@h\n", "m2.yaml")
    m2._stats_store = MagicMock()
    coord._monitors = [m1, m2]
    coord.record_control_event("U2@h", "CONTROL_VARIABLE", "set")
    m2._stats_store.log_event.assert_called_once()
    m1._stats_store.log_event.assert_not_called()


# ----- subsystem live-reload hooks (#1) -----

@pytest.mark.unit
def test_reload_classifies_subsystems(tmp_path):
    live = _load(tmp_path, "ups:\n  name: U@h\nstatistics:\n  retention:\n    raw_hours: 24\n", "a.yaml")
    new = _load(tmp_path, "ups:\n  name: U@h\nstatistics:\n  retention:\n    raw_hours: 48\n", "b.yaml")
    rep = reloadmod.apply_reload(live, [live], new)
    assert "statistics" in rep["applied"]
    assert "statistics" in rep["subsystems"]


@pytest.mark.unit
def test_reload_mqtt_remote_health_are_restart_required(tmp_path):
    live = _load(tmp_path, "ups:\n  name: U@h\nmqtt:\n  enabled: false\n", "a.yaml")
    new = _load(tmp_path, "ups:\n  name: U@h\nmqtt:\n  enabled: true\n  broker: 'mqtt://h:1883'\n", "b.yaml")
    rep = reloadmod.apply_reload(live, [live], new)
    assert "mqtt" in rep["restartRequired"]
    assert "mqtt" not in rep["subsystems"]


@pytest.mark.unit
def test_stats_store_apply_reload(tmp_path):
    from eneru.stats import StatsStore
    store = StatsStore(tmp_path / "s.db")
    cfg = _load(tmp_path, "ups:\n  name: U@h\nstatistics:\n  retention:\n"
                          "    raw_hours: 72\n    agg_5min_days: 60\n    agg_hourly_days: 100\n")
    assert store.apply_reload(cfg.statistics) is True
    assert store.retention_raw_hours == 72
    assert store.retention_5min_days == 60
    assert store.retention_hourly_days == 100


@pytest.mark.unit
def test_notifications_apply_reload_paths(tmp_path, monkeypatch):
    from eneru import notifications as notif
    cfg_on = _load(tmp_path, "notifications:\n  enabled: true\n  urls: ['json://localhost']\n")
    cfg_off = _load(tmp_path, "notifications:\n  enabled: false\n")
    w = notif.NotificationWorker(cfg_on)
    # running worker: rebuild apprise targets (atomic swap)
    w._initialized = True
    assert w.apply_reload(cfg_on) is True
    assert w._apprise_instance is not None
    # disabled: drop apprise
    assert w.apply_reload(cfg_off) is True
    assert w._apprise_instance is None
    # was disabled, now enabled -> delegates to start()
    w._initialized = False
    monkeypatch.setattr(w, "start", lambda: True)
    assert w.apply_reload(cfg_on) is True


@pytest.mark.unit
def test_monitor_apply_subsystem_reload(tmp_path):
    mon = object.__new__(UPSGroupMonitor)
    mon.config = _load(tmp_path, "ups:\n  name: U@h\n")
    mon._notification_worker = MagicMock()
    mon._stats_store = MagicMock()
    logs = []
    mon._log_message = logs.append
    mon._apply_subsystem_reload(["notifications", "statistics"])
    mon._notification_worker.apply_reload.assert_called_once()
    mon._stats_store.apply_reload.assert_called_once()


@pytest.mark.unit
def test_monitor_apply_subsystem_reload_swallows_errors(tmp_path):
    mon = object.__new__(UPSGroupMonitor)
    mon.config = _load(tmp_path, "ups:\n  name: U@h\n")
    mon._notification_worker = MagicMock()
    mon._notification_worker.apply_reload.side_effect = RuntimeError("boom")
    mon._stats_store = None
    logs = []
    mon._log_message = logs.append
    mon._apply_subsystem_reload(["notifications"])  # must not raise
    assert any("reload failed" in line for line in logs)


@pytest.mark.unit
def test_coordinator_apply_subsystem_reload(tmp_path):
    coord = object.__new__(MultiUPSCoordinator)
    coord.config = _load(tmp_path, "ups:\n  - name: U1@h\n")
    coord._notification_worker = MagicMock()
    mon = object.__new__(UPSGroupMonitor)
    mon._stats_store = MagicMock()
    coord._monitors = [mon]
    coord._log = lambda m: None
    coord._apply_subsystem_reload(["notifications", "statistics"])
    coord._notification_worker.apply_reload.assert_called_once()
    mon._stats_store.apply_reload.assert_called_once()


# ----- do_DELETE (#6) -----

@pytest.mark.unit
def test_do_delete_returns_405(minimal_config):
    h = object.__new__(EneruAPIHandler)
    h.api_config = minimal_config
    headers = []
    h.send_response = lambda s: headers.append(("status", s))
    h.send_header = lambda k, v: None
    h.end_headers = lambda: None
    h.wfile = BytesIO()
    h.do_DELETE()
    assert ("status", 405) in headers
