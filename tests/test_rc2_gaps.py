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
def test_audit_ups_name_with_colon_split_on_last_colon():
    h = object.__new__(EneruAPIHandler)
    h.api_log = None
    source = MagicMock()
    h.api_source = source
    # NUT name itself contains a colon (host:port) — must split on the LAST colon.
    h._audit({"username": "a", "kind": "user"}, "command",
             "ups@host:3493:beeper.toggle", "ok")
    assert source.record_control_event.call_args[0][0] == "ups@host:3493"


@pytest.mark.unit
def test_audit_scrubs_control_characters():
    h = object.__new__(EneruAPIHandler)
    logs = []
    h.api_log = logs.append
    source = MagicMock()
    h.api_source = source
    h._audit({"username": "a\nINJECT", "kind": "user"}, "command",
             "UPS@h:cmd\nFORGED", "ok")
    assert "\n" not in logs[0]
    assert "\n" not in source.record_control_event.call_args[0][2]


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


@pytest.mark.unit
def test_monitor_delete_events(tmp_path):
    mon = object.__new__(UPSGroupMonitor)
    mon._stats_store = MagicMock()
    mon._stats_store.delete_events.return_value = 3
    assert mon.delete_events("UPS@h", [(1, 100, "A")]) == 3
    mon._stats_store.delete_events.assert_called_once_with([(1, 100, "A")])
    # No store -> None so the API answers 503.
    mon._stats_store = None
    assert mon.delete_events("UPS@h", [(1, 100, "A")]) is None


@pytest.mark.unit
def test_coordinator_delete_events_routes_to_ups(tmp_path):
    coord = object.__new__(MultiUPSCoordinator)
    m1 = object.__new__(UPSGroupMonitor)
    m1.config = _load(tmp_path, "ups:\n  - name: U1@h\n", "m1.yaml")
    m1._stats_store = MagicMock()
    m2 = object.__new__(UPSGroupMonitor)
    m2.config = _load(tmp_path, "ups:\n  - name: U2@h\n", "m2.yaml")
    m2._stats_store = MagicMock()
    m2._stats_store.delete_events.return_value = 1
    coord._monitors = [m1, m2]
    assert coord.delete_events("U2@h", [(9, 5, "X")]) == 1
    m2._stats_store.delete_events.assert_called_once_with([(9, 5, "X")])
    m1._stats_store.delete_events.assert_not_called()
    # Unknown UPS -> None (no first-store fallback for a destructive op).
    assert coord.delete_events("Ghost@h", [(9, 5, "X")]) is None
    # Matched UPS but no open store -> None.
    m2._stats_store = None
    assert coord.delete_events("U2@h", [(9, 5, "X")]) is None


# ----- subsystem live-reload hooks (#1) -----

@pytest.mark.unit
def test_reload_classifies_subsystems(tmp_path):
    live = _load(tmp_path, "ups:\n  name: U@h\nstatistics:\n  retention:\n    raw_hours: 24\n", "a.yaml")
    new = _load(tmp_path, "ups:\n  name: U@h\nstatistics:\n  retention:\n    raw_hours: 48\n", "b.yaml")
    rep = reloadmod.apply_reload(live, [live], new)
    assert "statistics" in rep["applied"]
    assert "statistics" in rep["subsystems"]


@pytest.mark.unit
@pytest.mark.parametrize("section,frag", [
    ("mqtt", "mqtt:\n  enabled: true\n  broker: 'mqtt://h:1883'\n"),
    ("notifications", "notifications:\n  enabled: true\n  urls: ['json://h']\n"),
])
def test_reload_worker_subsystems_apply_live(tmp_path, section, frag):
    live = _load(tmp_path, "ups:\n  name: U@h\n", "a.yaml")
    new = _load(tmp_path, "ups:\n  name: U@h\n" + frag, "b.yaml")
    rep = reloadmod.apply_reload(live, [live], new)
    assert section in rep["applied"]
    assert section in rep["subsystems"]
    assert section not in rep["restartRequired"]


@pytest.mark.unit
def test_reload_statistics_db_dir_is_restart_required(tmp_path):
    # Only retention is live; a db_directory change needs a restart.
    live = _load(tmp_path, "ups:\n  name: U@h\nstatistics:\n  db_directory: /a\n", "a.yaml")
    new = _load(tmp_path, "ups:\n  name: U@h\nstatistics:\n  db_directory: /b\n", "b.yaml")
    rep = reloadmod.apply_reload(live, [live], new)
    assert "statistics" in rep["restartRequired"]
    assert "statistics" not in rep["subsystems"]


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
def test_monitor_apply_subsystem_reload(tmp_path):
    mon = object.__new__(UPSGroupMonitor)
    mon.config = _load(tmp_path, "ups:\n  name: U@h\n")
    mon._stats_store = MagicMock()
    mon._log_message = lambda m: None
    mon._apply_subsystem_reload(["statistics"])
    mon._stats_store.apply_reload.assert_called_once()


@pytest.mark.unit
def test_monitor_apply_subsystem_reload_swallows_errors(tmp_path):
    mon = object.__new__(UPSGroupMonitor)
    mon.config = _load(tmp_path, "ups:\n  name: U@h\n")
    mon._stats_store = MagicMock()
    mon._stats_store.apply_reload.side_effect = RuntimeError("boom")
    logs = []
    mon._log_message = logs.append
    mon._apply_subsystem_reload(["statistics"])  # must not raise
    assert any("reload failed" in line for line in logs)


@pytest.mark.unit
def test_coordinator_apply_subsystem_reload(tmp_path):
    coord = object.__new__(MultiUPSCoordinator)
    coord.config = _load(tmp_path, "ups:\n  - name: U1@h\n")
    mon = object.__new__(UPSGroupMonitor)
    mon._stats_store = MagicMock()
    coord._monitors = [mon]
    coord._log = lambda m: None
    coord._apply_subsystem_reload(["statistics"])
    mon._stats_store.apply_reload.assert_called_once()


# ----- do_DELETE (#6) -----

@pytest.mark.unit
def test_do_delete_unknown_path_returns_404(minimal_config):
    # DELETE is now a real verb (event deletion); an unknown DELETE path is a
    # 404, not the old blanket 405.
    from unittest.mock import MagicMock
    h = object.__new__(EneruAPIHandler)
    h.api_config = minimal_config
    h.api_source = MagicMock()
    h.path = "/api/v1/nope"
    headers = []
    h.send_response = lambda s: headers.append(("status", s))
    h.send_header = lambda k, v: None
    h.end_headers = lambda: None
    h.wfile = BytesIO()
    h.do_DELETE()
    assert ("status", 404) in headers
