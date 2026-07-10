"""Unit tests for v6.0 config hot-reload (src/eneru/reload.py + wiring)."""

import pytest
import threading
from unittest.mock import MagicMock

from eneru import reload as reloadmod
from eneru.config import Config, ConfigLoader
from eneru.monitor import UPSGroupMonitor
from eneru.multi_ups import MultiUPSCoordinator
from eneru.state import MonitorState


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
    # fail-closed: nut_control without auth. db_path points at a nonexistent file
    # so effective-auth is deterministically inactive (no stray users).
    missing = tmp_path / "nope.db"
    p = _write(tmp_path / "c.yaml",
               "ups:\n  name: U@h\nnut_control:\n  enabled: true\n"
               f"api:\n  auth:\n    db_path: '{missing}'\n")
    cfg, errors = reloadmod.load_and_validate(p)
    assert cfg is None
    assert any("nut_control.enabled requires API authentication" in e
               for e in errors)


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
def test_apply_reload_per_ups_v61_overrides_live(tmp_path):
    # Per-UPS battery_health / self_test overrides are read live by the v6.1
    # resolvers each tick, so a reload must apply them IN PLACE — not punt the
    # whole group to restart-required (the original B1a gap CodeRabbit flagged).
    base = (
        "api:\n  auth:\n    enabled: true\n"
        "nut_control:\n  enabled: true\n  allowed_commands: [test.battery.start]\n"
        "ups:\n"
        "  - name: U1@h\n"
        "    battery_health:\n      expected_life_years: {y}\n"
        "    self_test:\n      schedule: {sch}\n      command: test.battery.start\n"
        "  - name: U2@h\n"
    )
    live = _load(_write(tmp_path / "a.yaml", base.format(y=5, sch="monthly")))
    new = _load(_write(tmp_path / "b.yaml", base.format(y=3, sch="weekly")))
    report = reloadmod.apply_reload(live, [live], new)
    assert "battery_health:U1@h" in report["applied"]
    assert "self_test:U1@h" in report["applied"]
    # The whole group must NOT be punted to restart-required for a live field.
    assert "ups_groups:U1@h" not in report["restartRequired"]
    g1 = next(g for g in live.ups_groups if g.ups.name == "U1@h")
    assert g1.battery_health.expected_life_years == 3
    assert g1.self_test.schedule == "weekly"


@pytest.mark.unit
def test_apply_reload_per_ups_other_field_still_restart(tmp_path):
    # A per-UPS change OUTSIDE the live set (e.g. virtual_machines) is still
    # restart-required even alongside a live battery_health change.
    base = ("ups:\n  - name: U@h\n"
            "    battery_health:\n      expected_life_years: {y}\n"
            "    virtual_machines:\n      enabled: {vm}\n")
    live = _load(_write(tmp_path / "a.yaml", base.format(y=5, vm="false")))
    new = _load(_write(tmp_path / "b.yaml", base.format(y=3, vm="true")))
    report = reloadmod.apply_reload(live, [live], new)
    assert "battery_health:U@h" in report["applied"]
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


@pytest.mark.unit
def test_apply_reload_subsystem_sections_live(tmp_path):
    live = _load(_write(tmp_path / "a.yaml", """
ups:
  name: U@h
notifications:
  enabled: false
mqtt:
  enabled: false
remote_health:
  interval: 3600
"""))
    new = _load(_write(tmp_path / "b.yaml", """
ups:
  name: U@h
notifications:
  enabled: true
  urls: ["json://localhost"]
mqtt:
  enabled: true
  broker: "mqtt://127.0.0.1:1883"
remote_health:
  interval: 120
"""))
    report = reloadmod.apply_reload(live, [live], new)
    assert set(["notifications", "mqtt", "remote_health"]).issubset(report["applied"])
    assert set(["notifications", "mqtt", "remote_health"]).issubset(report["subsystems"])
    assert not set(["notifications", "mqtt", "remote_health"]).intersection(
        report["restartRequired"]
    )
    assert live.notifications.enabled is True
    assert live.mqtt.enabled is True
    assert live.remote_health.interval == 120


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


@pytest.mark.unit
def test_reload_propagates_to_redundancy_executor(tmp_path):
    """ISS-004: a live behavior.dry_run toggle must reach the redundancy
    executor's private Config, not just the per-UPS monitors."""
    from eneru.redundancy import RedundancyGroupExecutor
    body = ("behavior:\n  dry_run: true\n"
            "ups:\n  - name: U1@h\n  - name: U2@h\n"
            "redundancy_groups:\n  - name: rg\n    ups_sources: [U1@h, U2@h]\n")
    p = tmp_path / "a.yaml"
    cfg = _load(_write(p, body))
    coord = object.__new__(MultiUPSCoordinator)
    coord.config = cfg
    coord._monitors = []
    coord._log = lambda *_: None
    executor = RedundancyGroupExecutor(cfg.redundancy_groups[0], base_config=cfg)
    coord._redundancy_executors = {"rg": executor}
    assert executor.config.behavior.dry_run is True

    _write(p, body.replace("dry_run: true", "dry_run: false"))
    report = coord.reload_config()

    assert report["reloaded"]
    assert coord.config.behavior.dry_run is False
    # The executor must see the new value AND share the coordinator's object.
    assert executor.config.behavior.dry_run is False
    assert executor.config.behavior is coord.config.behavior
    # The full contract of _repoint_executor_configs: every shared section is
    # re-pointed to the coordinator's current object, not just behavior.
    assert executor.config.notifications is coord.config.notifications
    assert executor.config.local_shutdown is coord.config.local_shutdown
    assert executor.config.logging is coord.config.logging


@pytest.mark.unit
def test_perform_reload_is_serialized(tmp_path, monkeypatch):
    """ISS-027: concurrent perform_reload calls (SIGHUP + API) must not overlap
    their apply_reload bodies."""
    import time
    p = tmp_path / "a.yaml"
    _write(p, "ups:\n  - name: U1@h\n")
    primary = _load(str(p))

    counters = {"cur": 0, "max": 0}
    guard = threading.Lock()
    real_apply = reloadmod.apply_reload

    def slow_apply(*args, **kwargs):
        with guard:
            counters["cur"] += 1
            counters["max"] = max(counters["max"], counters["cur"])
        try:
            time.sleep(0.05)
            return real_apply(*args, **kwargs)
        finally:
            with guard:
                counters["cur"] -= 1

    monkeypatch.setattr(reloadmod, "apply_reload", slow_apply)
    threads = [
        threading.Thread(
            target=lambda: reloadmod.perform_reload(primary, [], str(p))
        )
        for _ in range(4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # The lock must have kept every apply_reload body strictly non-overlapping.
    assert counters["max"] == 1


@pytest.mark.unit
def test_monitor_apply_subsystem_reload_bounces_workers():
    mon = object.__new__(UPSGroupMonitor)
    mon._coordinator_mode = False
    mon.config = Config()
    mon._stats_store = MagicMock()
    mon._log_message = MagicMock()
    mon._reload_notification_worker = MagicMock()
    mon._reload_remote_health = MagicMock()
    mon._reload_mqtt_publisher = MagicMock()
    mon._apply_subsystem_reload([
        "statistics", "notifications", "remote_health", "mqtt",
    ])
    mon._stats_store.apply_reload.assert_called_once_with(mon.config.statistics)
    mon._reload_notification_worker.assert_called_once()
    mon._reload_remote_health.assert_called_once()
    mon._reload_mqtt_publisher.assert_called_once()


@pytest.mark.unit
def test_coordinator_apply_subsystem_reload_bounces_workers():
    coord = object.__new__(MultiUPSCoordinator)
    coord.config = Config()
    coord._monitors = []
    coord._reload_notification_worker = MagicMock()
    coord._reload_remote_health = MagicMock()
    coord._reload_mqtt_publisher = MagicMock()
    coord._apply_subsystem_reload(["notifications", "remote_health", "mqtt"])
    coord._reload_notification_worker.assert_called_once()
    coord._reload_remote_health.assert_called_once()
    coord._reload_mqtt_publisher.assert_called_once()


@pytest.mark.unit
def test_remote_health_stop_does_not_set_daemon_stop_event(tmp_path):
    from eneru.remote_health import RemoteHealthManager

    daemon_stop = threading.Event()
    mgr = RemoteHealthManager(
        config=Config(),
        group_label="U@h",
        servers=[],
        sidecar_path=tmp_path / "rh.json",
        stop_event=daemon_stop,
        log_fn=lambda _msg: None,
    )
    mgr.stop()
    assert not daemon_stop.is_set()


@pytest.mark.unit
def test_monitor_reload_notification_worker_restarts_and_registers(monkeypatch):
    import eneru.monitor as monitormod

    mon = object.__new__(UPSGroupMonitor)
    mon.config = Config()
    mon.config.notifications.enabled = True
    mon._notification_worker = MagicMock()
    mon._stats_store = MagicMock()
    mon._stats_store._conn = object()
    mon._log_message = MagicMock()
    mon.logger = object()  # shared structured logger
    worker = MagicMock()
    worker.start.return_value = True
    worker.get_service_count.return_value = 2
    monkeypatch.setattr(monitormod, "APPRISE_AVAILABLE", True)
    worker_cls = MagicMock(return_value=worker)
    monkeypatch.setattr(monitormod, "NotificationWorker", worker_cls)

    old = mon._notification_worker
    mon._reload_notification_worker()

    old.stop.assert_called_once()
    worker.start.assert_called_once()
    worker.register_store.assert_called_once_with(mon._stats_store)
    assert mon._notification_worker is worker
    # Reload must forward the shared logger (else warnings fall back to print).
    assert worker_cls.call_args.kwargs.get("logger") is mon.logger


@pytest.mark.unit
def test_monitor_reload_notification_worker_warns_when_apprise_missing(monkeypatch):
    """Reload with notifications on but apprise absent must log the uv-pip
    install hint and leave the worker unset (covers the APPRISE_AVAILABLE=False
    branch of _reload_notification_worker)."""
    import eneru.monitor as monitormod

    mon = object.__new__(UPSGroupMonitor)
    mon.config = Config()
    mon.config.notifications.enabled = True
    mon._notification_worker = MagicMock()
    mon._stats_store = MagicMock()
    mon._log_message = MagicMock()
    monkeypatch.setattr(monitormod, "APPRISE_AVAILABLE", False)

    old = mon._notification_worker
    mon._reload_notification_worker()

    old.stop.assert_called_once()
    assert mon._notification_worker is None
    logged = " ".join(str(c.args[0]) for c in mon._log_message.call_args_list)
    assert "apprise not installed" in logged
    assert "uv pip install apprise" in logged


@pytest.mark.unit
def test_monitor_reload_remote_health_and_mqtt_bounce_workers():
    mon = object.__new__(UPSGroupMonitor)
    mon._remote_health_manager = MagicMock()
    mon._mqtt_publisher = MagicMock()
    mon._start_remote_health = MagicMock()
    mon._start_mqtt_publisher = MagicMock()
    mon._log_message = MagicMock()

    old_remote = mon._remote_health_manager
    old_mqtt = mon._mqtt_publisher
    mon._reload_remote_health()
    mon._reload_mqtt_publisher()

    old_remote.stop.assert_called_once()
    old_mqtt.stop.assert_called_once()
    mon._start_remote_health.assert_called_once()
    mon._start_mqtt_publisher.assert_called_once()


@pytest.mark.unit
def test_coordinator_reload_notification_worker_rewires_children(monkeypatch):
    import eneru.multi_ups as multi_mod

    coord = object.__new__(MultiUPSCoordinator)
    coord.config = Config()
    coord.config.notifications.enabled = True
    coord._notification_worker = MagicMock()
    coord._log = MagicMock()
    coord._logger = object()  # shared structured logger
    monitor = MagicMock()
    monitor._stats_store = MagicMock()
    monitor._stats_store._conn = object()
    executor = MagicMock()
    coord._monitors = [monitor]
    coord._redundancy_executors = {"rg": executor}
    worker = MagicMock()
    worker.start.return_value = True
    worker.get_service_count.return_value = 1
    monkeypatch.setattr(multi_mod, "APPRISE_AVAILABLE", True)
    worker_cls = MagicMock(return_value=worker)
    monkeypatch.setattr(multi_mod, "NotificationWorker", worker_cls)

    old = coord._notification_worker
    coord._reload_notification_worker()

    old.stop.assert_called_once()
    worker.register_store.assert_called_once_with(monitor._stats_store)
    assert coord._notification_worker is worker
    assert monitor._notification_worker is worker
    assert executor._notification_worker is worker
    # Reload must forward the shared logger (else warnings fall back to print).
    assert worker_cls.call_args.kwargs.get("logger") is coord._logger


@pytest.mark.unit
def test_coordinator_reload_remote_health_and_mqtt_bounce_workers():
    coord = object.__new__(MultiUPSCoordinator)
    coord.config = Config()
    coord.config.redundancy_groups = []
    coord._log = MagicMock()
    monitor = MagicMock()
    manager = MagicMock()
    coord._monitors = [monitor]
    coord._redundancy_remote_health_managers = [manager]
    coord._mqtt_publisher = MagicMock()
    coord._start_mqtt_publisher = MagicMock()

    old_mqtt = coord._mqtt_publisher
    coord._reload_remote_health()
    coord._reload_mqtt_publisher()

    monitor._reload_remote_health.assert_called_once()
    manager.stop.assert_called_once()
    assert coord._redundancy_remote_health_managers == []
    old_mqtt.stop.assert_called_once()
    coord._start_mqtt_publisher.assert_called_once()


# ----- reload bucket completeness (regression meta-test) -----

@pytest.mark.unit
def test_every_config_section_has_a_reload_bucket():
    """Regression guard: every top-level Config dataclass section must be
    classified into exactly one reload bucket (SAFE / SUBSYSTEM / RESTART) OR be
    one of the explicitly-handled topology sections. A future field added without
    a reload bucket would silently never apply (or never be restart-flagged), so
    this fails the moment that happens — forcing the author to pick a bucket.
    """
    import dataclasses

    # Non-config Config fields: the source path + the NOTIFY_* severity
    # constants are not user-tunable sections and have no reload semantics.
    non_config_fields = {
        "config_path",
        "NOTIFY_FAILURE", "NOTIFY_WARNING", "NOTIFY_SUCCESS", "NOTIFY_INFO",
    }
    # Topology sections are handled explicitly in apply_reload (diffed by name /
    # by value), not via the section-name bucket tuples.
    topology_sections = {"ups_groups", "redundancy_groups"}

    buckets = {
        "SAFE": set(reloadmod.SAFE_TOP_SECTIONS),
        "SUBSYSTEM": set(reloadmod.SUBSYSTEM_SECTIONS),
        "RESTART": set(reloadmod.RESTART_TOP_SECTIONS),
    }

    section_fields = [
        f.name for f in dataclasses.fields(Config)
        if f.name not in non_config_fields and f.name not in topology_sections
    ]
    assert section_fields, "expected some classifiable Config sections"

    for name in section_fields:
        memberships = [b for b, names in buckets.items() if name in names]
        assert len(memberships) == 1, (
            f"Config section {name!r} must belong to exactly ONE reload bucket "
            f"(SAFE/SUBSYSTEM/RESTART) or be an explicit topology section; "
            f"found in: {memberships or 'NONE'}. Add it to a bucket in "
            f"reload.py (or to topology_sections here if it is handled "
            f"specially).")


@pytest.mark.unit
def test_reload_buckets_are_mutually_exclusive_and_no_unknown_names():
    """The three bucket tuples must not overlap, and must not reference a name
    that is not an actual Config field (a typo'd bucket entry would silently do
    nothing)."""
    import dataclasses

    safe = set(reloadmod.SAFE_TOP_SECTIONS)
    sub = set(reloadmod.SUBSYSTEM_SECTIONS)
    restart = set(reloadmod.RESTART_TOP_SECTIONS)
    assert safe.isdisjoint(sub)
    assert safe.isdisjoint(restart)
    assert sub.isdisjoint(restart)

    real_fields = {f.name for f in dataclasses.fields(Config)}
    for name in safe | sub | restart:
        assert name in real_fields, f"reload bucket references unknown field {name!r}"


# ----- F-009: startup runtime synthesis mirrored onto the fresh parse -----

@pytest.mark.unit
def test_reload_preserves_cli_dry_run(tmp_path):
    """F-009: --dry-run is a CLI override recorded on the running config; a
    SIGHUP reload of a file that says dry_run: false must NOT disarm the
    rehearsal daemon (live-safety reversal)."""
    import argparse
    from eneru.cli import _apply_run_overrides

    p = tmp_path / "a.yaml"
    live = _load(_write(p, "ups:\n  name: U@h\nbehavior:\n  dry_run: false\n"))
    _apply_run_overrides(live, argparse.Namespace(dry_run=True))
    assert live.behavior.dry_run is True

    report = reloadmod.perform_reload(live, [live], str(p))

    assert report["reloaded"]
    assert live.behavior.dry_run is True        # still a rehearsal daemon
    assert "behavior" not in report["applied"]  # and no false diff either


@pytest.mark.unit
def test_reload_preserves_cli_api_overrides(tmp_path):
    """F-009: --api/--api-bind/--api-port overrides must be re-applied to the
    fresh parse, or every reload falsely reports 'api' restart-required."""
    import argparse
    from eneru.cli import _apply_run_overrides

    p = tmp_path / "a.yaml"
    live = _load(_write(p, "ups:\n  name: U@h\n"))
    _apply_run_overrides(live, argparse.Namespace(
        dry_run=False, api=True, api_bind="0.0.0.0", api_port=9999))
    assert live.api.enabled is True and live.api.port == 9999

    report = reloadmod.perform_reload(live, [live], str(p))

    assert report["reloaded"]
    assert "api" not in report["restartRequired"]
    assert live.api.enabled is True and live.api.port == 9999


@pytest.mark.unit
def test_reload_loopback_config_no_false_restart(tmp_path, monkeypatch):
    """F-009: a containerized loopback-delegate config must reload clean.
    The RUNNING config carries a synthesized is_host_loopback entry plus
    generated pre-shutdown commands that are not in the YAML; the fresh
    parse must receive the same synthesis, or every reload falsely flags
    ups_groups restart-required."""
    monkeypatch.setattr("eneru.runtime._detect_runtime_context",
                        lambda: "container (Docker)")
    from eneru.cli import _prepare_runtime_config

    body = ("ups:\n  name: U@h\n"
            "containers:\n  enabled: true\n"
            "local_shutdown:\n  enabled: true\n")
    p = tmp_path / "a.yaml"
    live = _load(_write(p, body))
    _prepare_runtime_config(live, strict_key_check=False)
    # Sanity: the loopback delegate WAS synthesized onto the running config.
    assert any(s.is_host_loopback for s in live.remote_servers)

    report = reloadmod.perform_reload(live, [live], str(p))

    assert report["reloaded"]
    assert not any(r.startswith("ups_groups")
                   for r in report["restartRequired"]), report


@pytest.mark.unit
def test_reload_container_logging_rewrite_no_false_restart(tmp_path,
                                                           monkeypatch):
    """F-009: inside a container the legacy logging.* defaults are rewritten
    to /var/{log,run}/eneru/ at startup; the fresh parse must get the same
    rewrite, or every reload falsely reports 'logging' restart-required."""
    monkeypatch.setattr("eneru.runtime._detect_runtime_context",
                        lambda: "container (Docker)")
    from eneru.cli import _rewrite_legacy_paths_for_container

    p = tmp_path / "a.yaml"
    live = _load(_write(p, "ups:\n  name: U@h\n"))  # logging keeps defaults
    _rewrite_legacy_paths_for_container(live)
    assert live.logging.file == "/var/log/eneru/ups-monitor.log"

    report = reloadmod.perform_reload(live, [live], str(p))

    assert report["reloaded"]
    assert "logging" not in report["restartRequired"]


@pytest.mark.unit
def test_reload_synthesis_error_reported_not_raised(tmp_path, monkeypatch):
    """F-009 defensive branch: an exception inside the synthesis mirror is
    reported as a reload error (daemon keeps its old config), never raised
    into the signal handler."""
    p = tmp_path / "a.yaml"
    live = _load(_write(p, "ups:\n  name: U@h\n"))

    def boom(cfg, primary):
        raise RuntimeError("synthesis exploded")

    monkeypatch.setattr(reloadmod, "_mirror_startup_synthesis", boom)
    report = reloadmod.perform_reload(live, [live], str(p))
    assert report["reloaded"] is False
    assert any("synthesis exploded" in e for e in report["errors"])


@pytest.mark.unit
def test_reload_during_on_battery_preserves_outage_state(tmp_path):
    """Behavioural-gap 6: a config hot-reload that lands DURING an outage must
    swap only config -- it must not reset the on-battery clock or clear a
    latched advisory trigger held in MonitorState. Outage state lives on the
    monitor's ``state`` object, which the safe-swap path never touches."""
    live = _load(_write(
        tmp_path / "live.yaml",
        "ups:\n  name: U@h\ntriggers:\n  low_battery_threshold: 20\n"))
    live.logging.state_file = str(tmp_path / "state")
    live.logging.battery_history_file = str(tmp_path / "history")
    live.logging.shutdown_flag_file = str(tmp_path / "flag")
    live.logging.file = None

    monitor = UPSGroupMonitor(live)
    monitor.state = MonitorState()
    # Simulate an in-progress outage with a latched advisory trigger.
    monitor.state.on_battery_start_time = 1_700_000_000
    monitor.state.on_battery_start_mono = 123.456
    monitor.state.trigger_active = True
    monitor.state.trigger_reason = "critical runtime"

    new = _load(_write(
        tmp_path / "new.yaml",
        "ups:\n  name: U@h\ntriggers:\n  low_battery_threshold: 35\n"))

    report = reloadmod.apply_reload(monitor.config, [monitor.config], new)

    # Positive control: the reload genuinely took effect (triggers swapped live).
    assert "triggers:U@h" in report["applied"]
    assert monitor.config.triggers.low_battery_threshold == 35

    # The outage / latch state SURVIVES the config swap.
    assert monitor.state.on_battery_start_time == 1_700_000_000
    assert monitor.state.on_battery_start_mono == 123.456
    assert monitor.state.trigger_active is True
    assert monitor.state.trigger_reason == "critical runtime"
