"""Tests for advisory remote SSH healthchecks."""

import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from eneru import Config, RemoteServerConfig
from eneru.remote_health import (
    REMOTE_HEALTH_DEGRADED,
    REMOTE_HEALTH_FAILED,
    REMOTE_HEALTH_HEALTHY,
    RemoteHealthManager,
    build_ssh_probe_command,
    is_safe_probe_command,
)


@pytest.fixture
def remote_server():
    return RemoteServerConfig(
        name="nas",
        enabled=True,
        host="nas.example",
        user="ups",
        connect_timeout=3,
        ssh_options=["-o StrictHostKeyChecking=yes"],
        shutdown_command="sudo shutdown -h now",
    )


@pytest.mark.unit
def test_probe_command_builder_uses_probe_not_shutdown(remote_server):
    cmd = build_ssh_probe_command(remote_server, "true")
    assert cmd[-1] == "true"
    assert "sudo shutdown -h now" not in cmd
    assert "BatchMode=yes" in cmd


@pytest.mark.unit
def test_probe_command_builder_preserves_ssh_option_arguments(remote_server):
    remote_server.ssh_options = [
        "-i",
        "/root/.ssh/id_ups_shutdown",
        "-p",
        "2222",
        "StrictHostKeyChecking=no",
    ]

    cmd = build_ssh_probe_command(remote_server, "true")

    assert cmd[:7] == [
        "ssh",
        "-i",
        "/root/.ssh/id_ups_shutdown",
        "-p",
        "2222",
        "-o",
        "StrictHostKeyChecking=no",
    ]


@pytest.mark.unit
def test_probe_safety_rejects_obvious_shutdown_commands():
    assert is_safe_probe_command("true")
    assert not is_safe_probe_command("sudo shutdown -h now")
    assert not is_safe_probe_command("systemctl poweroff")
    assert not is_safe_probe_command("docker stop app")
    assert not is_safe_probe_command("virsh shutdown vm01")


@pytest.mark.unit
def test_remote_health_failure_then_recovery(tmp_path, remote_server):
    config = Config()
    config.remote_health.enabled = True
    config.remote_health.failure_threshold = 1
    sidecar = tmp_path / "state.remote-health.json"
    logs = []
    notifications = []

    manager = RemoteHealthManager(
        config=config,
        group_label="Rack",
        servers=[remote_server],
        sidecar_path=sidecar,
        stop_event=threading.Event(),
        log_fn=logs.append,
        notify_fn=lambda body, typ: notifications.append((body, typ)),
    )

    with patch("eneru.remote_health.run_remote_probe",
               return_value=(False, "refused", 12)):
        rows = manager.check_once()
    assert rows[0]["status"] == REMOTE_HEALTH_FAILED
    assert rows[0]["consecutive_failures"] == 1
    assert "refused" in rows[0]["last_error"]
    assert sidecar.exists()

    with patch("eneru.remote_health.run_remote_probe",
               return_value=(True, "", 8)):
        rows = manager.check_once()
    assert rows[0]["status"] == REMOTE_HEALTH_HEALTHY
    assert rows[0]["consecutive_failures"] == 0
    assert any("Recovered" in body for body, _ in notifications)


@pytest.mark.unit
def test_remote_health_degrades_before_failure_threshold(tmp_path, remote_server):
    config = Config()
    config.remote_health.enabled = True
    config.remote_health.failure_threshold = 2
    notifications = []
    manager = RemoteHealthManager(
        config=config,
        group_label="Rack",
        servers=[remote_server],
        sidecar_path=tmp_path / "state.remote-health.json",
        stop_event=threading.Event(),
        log_fn=lambda msg: None,
        notify_fn=lambda body, typ: notifications.append((body, typ)),
    )

    with patch("eneru.remote_health.run_remote_probe",
               return_value=(False, "refused", 12)):
        rows = manager.check_once()
    assert rows[0]["status"] == REMOTE_HEALTH_DEGRADED
    assert notifications == []

    with patch("eneru.remote_health.run_remote_probe",
               return_value=(False, "refused", 12)):
        rows = manager.check_once()
    assert rows[0]["status"] == REMOTE_HEALTH_FAILED
    assert len(notifications) == 1


@pytest.mark.unit
def test_remote_health_suppresses_repeated_failure_notifications(tmp_path, remote_server):
    config = Config()
    config.remote_health.enabled = True
    config.remote_health.failure_threshold = 1
    notifications = []
    manager = RemoteHealthManager(
        config=config,
        group_label="Rack",
        servers=[remote_server],
        sidecar_path=tmp_path / "state.remote-health.json",
        stop_event=threading.Event(),
        log_fn=lambda msg: None,
        notify_fn=lambda body, typ: notifications.append((body, typ)),
    )

    with patch("eneru.remote_health.run_remote_probe",
               return_value=(False, "refused", 12)):
        manager.check_once()
        manager.check_once()

    assert len(notifications) == 1


@pytest.mark.unit
def test_remote_health_probe_command_is_validated_once(tmp_path, remote_server):
    config = Config()
    config.remote_health.enabled = True
    config.remote_health.probe_command = "true"
    manager = RemoteHealthManager(
        config=config,
        group_label="Rack",
        servers=[remote_server],
        sidecar_path=tmp_path / "state.remote-health.json",
        stop_event=threading.Event(),
        log_fn=lambda msg: None,
    )
    config.remote_health.probe_command = "sudo shutdown -h now"

    with patch("eneru.remote_health.run_remote_probe",
               return_value=(True, "", 3)) as probe:
        rows = manager.check_once()

    assert rows[0]["status"] == REMOTE_HEALTH_HEALTHY
    probe.assert_called_once_with(remote_server, "true")


@pytest.mark.unit
def test_remote_health_logs_sidecar_write_failure_once(tmp_path, remote_server):
    config = Config()
    config.remote_health.enabled = False
    logs = []
    sidecar_dir = tmp_path / "state.remote-health.json"
    sidecar_dir.mkdir()
    manager = RemoteHealthManager(
        config=config,
        group_label="Rack",
        servers=[remote_server],
        sidecar_path=sidecar_dir,
        stop_event=threading.Event(),
        log_fn=logs.append,
    )

    manager.check_once()
    manager.check_once()

    assert len(logs) == 1
    assert "Failed to write remote health sidecar" in logs[0]
