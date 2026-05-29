"""Unit tests for v6.0 config hot-reload (src/eneru/reload.py + wiring)."""

import pytest

from eneru import reload as reloadmod
from eneru.config import Config, ConfigLoader
from eneru.monitor import UPSGroupMonitor
from eneru.multi_ups import MultiUPSCoordinator


def _write(path, text):
    path.write_text(text)
    return str(path)


def _load(path):
    return ConfigLoader.load(path)


# ----- load_and_validate -----

@pytest.mark.unit
def test_load_and_validate_missing_path():
    cfg, errors = reloadmod.load_and_validate(None)
    assert cfg is None and errors


@pytest.mark.unit
def test_load_and_validate_bad_yaml(tmp_path):
    p = _write(tmp_path / "c.yaml", "ups: [unclosed\n")
    cfg, errors = reloadmod.load_and_validate(p)
    assert cfg is None and any("cannot read" in e for e in errors)


@pytest.mark.unit
def test_load_and_validate_non_mapping(tmp_path):
    p = _write(tmp_path / "c.yaml", "- just\n- a list\n")
    cfg, errors = reloadmod.load_and_validate(p)
    assert cfg is None and any("mapping" in e for e in errors)


@pytest.mark.unit
def test_load_and_validate_validation_error(tmp_path):
    # fail-closed: nut_control without auth
    p = _write(tmp_path / "c.yaml",
               "ups:\n  name: U@h\nnut_control:\n  enabled: true\n")
    cfg, errors = reloadmod.load_and_validate(p)
    assert cfg is None
    assert any("nut_control.enabled requires api.auth.enabled" in e for e in errors)


@pytest.mark.unit
def test_load_and_validate_success(tmp_path):
    p = _write(tmp_path / "c.yaml", "ups:\n  name: U@h\n")
    cfg, errors = reloadmod.load_and_validate(p)
    assert cfg is not None and errors == []


@pytest.mark.unit
def test_load_and_validate_malformed_section_type(tmp_path):
    # `triggers: 5` makes _parse_config raise; must surface as a reload error,
    # not propagate into the signal handler.
    p = _write(tmp_path / "c.yaml", "ups:\n  name: U@h\ntriggers: 5\n")
    cfg, errors = reloadmod.load_and_validate(p)
    assert cfg is None and errors


@pytest.mark.unit
def test_format_report_variants():
    assert reloadmod.format_report(
        {"reloaded": False, "errors": ["boom"]})[0].startswith("⚠️")
    assert any("no changes" in line for line in reloadmod.format_report(
        {"reloaded": True, "applied": [], "restartRequired": []}))
    lines = reloadmod.format_report(
        {"reloaded": True, "applied": ["triggers:U@h"], "restartRequired": ["api"]})
    assert any("applied live" in s for s in lines)
    assert any("restart" in s for s in lines)


# ----- apply_reload -----

@pytest.mark.unit
def test_apply_reload_safe_top_section(tmp_path):
    live = _load(_write(tmp_path / "a.yaml", "ups:\n  name: U@h\nbehavior:\n  dry_run: false\n"))
    new = _load(_write(tmp_path / "b.yaml", "ups:\n  name: U@h\nbehavior:\n  dry_run: true\n"))
    report = reloadmod.apply_reload(live, [live], new)
    assert "behavior" in report["applied"]
    assert live.behavior.dry_run is True


@pytest.mark.unit
def test_apply_reload_triggers_swapped_live(tmp_path):
    live = _load(_write(tmp_path / "a.yaml",
                        "ups:\n  name: U@h\ntriggers:\n  low_battery_threshold: 20\n"))
    new = _load(_write(tmp_path / "b.yaml",
                       "ups:\n  name: U@h\ntriggers:\n  low_battery_threshold: 35\n"))
    report = reloadmod.apply_reload(live, [live], new)
    assert "triggers:U@h" in report["applied"]
    assert live.triggers.low_battery_threshold == 35


@pytest.mark.unit
def test_apply_reload_restart_required_section(tmp_path):
    live = _load(_write(tmp_path / "a.yaml", "ups:\n  name: U@h\napi:\n  port: 9191\n"))
    new = _load(_write(tmp_path / "b.yaml", "ups:\n  name: U@h\napi:\n  port: 9999\n"))
    report = reloadmod.apply_reload(live, [live], new)
    assert "api" in report["restartRequired"]
    assert live.api.port == 9191  # unchanged (restart required)


@pytest.mark.unit
def test_apply_reload_topology_change(tmp_path):
    live = _load(_write(tmp_path / "a.yaml", "ups:\n  - name: U1@h\n  - name: U2@h\n"))
    new = _load(_write(tmp_path / "b.yaml", "ups:\n  - name: U1@h\n"))
    report = reloadmod.apply_reload(live, [live], new)
    assert "ups_groups" in report["restartRequired"]


@pytest.mark.unit
def test_load_and_validate_empty_file(tmp_path):
    p = _write(tmp_path / "c.yaml", "")  # YAML -> None -> {}
    cfg, errors = reloadmod.load_and_validate(p)
    assert cfg is not None and errors == []


@pytest.mark.unit
def test_apply_reload_redundancy_change_restart(tmp_path):
    base = ("ups:\n  - name: U1@h\n  - name: U2@h\n"
            "redundancy_groups:\n  - name: rg\n    ups_sources: [U1@h, U2@h]\n"
            "    min_healthy: {mh}\n")
    live = _load(_write(tmp_path / "a.yaml", base.format(mh=1)))
    new = _load(_write(tmp_path / "b.yaml", base.format(mh=2)))
    report = reloadmod.apply_reload(live, [live], new)
    assert "redundancy_groups" in report["restartRequired"]


@pytest.mark.unit
def test_apply_reload_group_non_trigger_change_restart(tmp_path):
    live = _load(_write(tmp_path / "a.yaml",
                        "ups:\n  - name: U@h\n    virtual_machines:\n      enabled: false\n"))
    new = _load(_write(tmp_path / "b.yaml",
                       "ups:\n  - name: U@h\n    virtual_machines:\n      enabled: true\n"))
    report = reloadmod.apply_reload(live, [live], new)
    assert "ups_groups:U@h" in report["restartRequired"]


@pytest.mark.unit
def test_apply_reload_dedups_trigger_tag_across_configs(tmp_path):
    primary = _load(_write(tmp_path / "a.yaml",
                           "ups:\n  name: U@h\ntriggers:\n  low_battery_threshold: 20\n"))
    monitor_cfg = _load(_write(tmp_path / "m.yaml",
                               "ups:\n  name: U@h\ntriggers:\n  low_battery_threshold: 20\n"))
    new = _load(_write(tmp_path / "b.yaml",
                       "ups:\n  name: U@h\ntriggers:\n  low_battery_threshold: 60\n"))
    report = reloadmod.apply_reload(primary, [monitor_cfg], new)
    assert report["applied"].count("triggers:U@h") == 1  # deduped


@pytest.mark.unit
def test_apply_reload_no_change(tmp_path):
    text = "ups:\n  name: U@h\n"
    live = _load(_write(tmp_path / "a.yaml", text))
    new = _load(_write(tmp_path / "b.yaml", text))
    report = reloadmod.apply_reload(live, [live], new)
    assert report["applied"] == [] and report["restartRequired"] == []


@pytest.mark.unit
def test_apply_reload_multi_config_propagation(tmp_path):
    # Two separate Config objects (coordinator + one monitor) must both update.
    primary = _load(_write(tmp_path / "a.yaml", "ups:\n  name: U@h\nbehavior:\n  dry_run: false\n"))
    monitor_cfg = _load(_write(tmp_path / "m.yaml", "ups:\n  name: U@h\nbehavior:\n  dry_run: false\n"))
    new = _load(_write(tmp_path / "b.yaml", "ups:\n  name: U@h\nbehavior:\n  dry_run: true\n"))
    reloadmod.apply_reload(primary, [monitor_cfg], new)
    assert primary.behavior.dry_run is True
    assert monitor_cfg.behavior.dry_run is True


# ----- perform_reload -----

@pytest.mark.unit
def test_perform_reload_rejects_bad_config(tmp_path):
    live = _load(_write(tmp_path / "a.yaml", "ups:\n  name: U@h\n"))
    p = _write(tmp_path / "a.yaml", "ups: [bad\n")
    report = reloadmod.perform_reload(live, [live], p)
    assert report["reloaded"] is False and report["errors"]


@pytest.mark.unit
def test_perform_reload_applies_good_config(tmp_path):
    p = tmp_path / "a.yaml"
    live = _load(_write(p, "ups:\n  name: U@h\ntriggers:\n  low_battery_threshold: 20\n"))
    _write(p, "ups:\n  name: U@h\ntriggers:\n  low_battery_threshold: 40\n")
    report = reloadmod.perform_reload(live, [live], str(p))
    assert report["reloaded"] is True
    assert live.triggers.low_battery_threshold == 40


# ----- monitor / coordinator wiring -----

@pytest.mark.unit
def test_monitor_reload_config_and_logging(tmp_path):
    p = tmp_path / "a.yaml"
    cfg = _load(_write(p, "ups:\n  name: U@h\ntriggers:\n  low_battery_threshold: 20\n"))
    _write(p, "ups:\n  name: U@h\ntriggers:\n  low_battery_threshold: 50\n")
    mon = object.__new__(UPSGroupMonitor)
    mon.config = cfg
    logs = []
    mon._log_message = logs.append
    report = mon.reload_config()
    assert report["reloaded"] and cfg.triggers.low_battery_threshold == 50
    assert any("reloaded" in line for line in logs)


@pytest.mark.unit
def test_monitor_handle_sighup_invokes_reload(tmp_path):
    p = tmp_path / "a.yaml"
    cfg = _load(_write(p, "ups:\n  name: U@h\ntriggers:\n  low_battery_threshold: 20\n"))
    mon = object.__new__(UPSGroupMonitor)
    mon.config = cfg
    logs = []
    mon._log_message = logs.append
    _write(p, "ups:\n  name: U@h\ntriggers:\n  low_battery_threshold: 33\n")
    mon._handle_sighup(1, None)
    assert any("SIGHUP" in line for line in logs)
    # The handler actually performed the reload, not just logged.
    assert cfg.triggers.low_battery_threshold == 33


@pytest.mark.unit
def test_monitor_reload_report_logs_failure(tmp_path):
    mon = object.__new__(UPSGroupMonitor)
    logs = []
    mon._log_message = logs.append
    mon._log_reload_report({"reloaded": False, "errors": ["boom"],
                            "applied": [], "restartRequired": []})
    assert any("failed" in line for line in logs)
    assert any("boom" in line for line in logs)


@pytest.mark.unit
def test_monitor_reload_report_logs_restart_required(tmp_path):
    mon = object.__new__(UPSGroupMonitor)
    logs = []
    mon._log_message = logs.append
    mon._log_reload_report({"reloaded": True, "errors": [],
                            "applied": [], "restartRequired": ["api"]})
    assert any("restart" in line for line in logs)


@pytest.mark.unit
def test_coordinator_reload_config(tmp_path):
    p = tmp_path / "a.yaml"
    cfg = _load(_write(p, "ups:\n  - name: U1@h\n    triggers:\n      low_battery_threshold: 20\n"))
    coord = object.__new__(MultiUPSCoordinator)
    coord.config = cfg
    # The monitor holds a DISTINCT Config object (as it does at runtime), so the
    # test fails if reload_config() stops propagating to per-monitor configs.
    monitor = object.__new__(UPSGroupMonitor)
    monitor.config = _load(_write(
        tmp_path / "mon.yaml",
        "ups:\n  - name: U1@h\n    triggers:\n      low_battery_threshold: 20\n"))
    coord._monitors = [monitor]
    logs = []
    coord._log = logs.append
    _write(p, "ups:\n  - name: U1@h\n    triggers:\n      low_battery_threshold: 45\n")
    report = coord.reload_config()
    assert report["reloaded"]
    assert coord.config.ups_groups[0].triggers.low_battery_threshold == 45
    assert monitor.config.ups_groups[0].triggers.low_battery_threshold == 45
    assert any("reloaded" in line for line in logs)
