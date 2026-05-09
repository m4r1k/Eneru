"""Tests for advisory remote SSH healthchecks."""

import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from eneru import Config, RemoteServerConfig
from eneru.remote_health import (
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
def test_probe_safety_rejects_obvious_shutdown_commands():
    assert is_safe_probe_command("true")
    assert not is_safe_probe_command("sudo shutdown -h now")
    assert not is_safe_probe_command("systemctl poweroff")
    assert not is_safe_probe_command("docker stop app")
    assert not is_safe_probe_command("virsh shutdown vm01")


@pytest.mark.unit
def test_remote_health_failure_then_recovery(tmp_path, remote_server):
    config = Config()
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
