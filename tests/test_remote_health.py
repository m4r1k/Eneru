"""Tests for advisory remote SSH healthchecks."""

import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from eneru import Config, RemoteServerConfig
from eneru.remote_health import (
    REMOTE_HEALTH_DEGRADED,
    REMOTE_HEALTH_DISABLED,
    REMOTE_HEALTH_FAILED,
    REMOTE_HEALTH_HEALTHY,
    RemoteHealthManager,
    build_ssh_probe_command,
    is_safe_probe_command,
    run_loopback_identity_probe,
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
def test_probe_command_builder_uses_ssh_key_path(remote_server):
    remote_server.ssh_key_path = "/var/lib/eneru/ssh/id_ups_shutdown"

    cmd = build_ssh_probe_command(remote_server, "true")

    assert cmd[0:3] == ["ssh", "-i", "/var/lib/eneru/ssh/id_ups_shutdown"]


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
@pytest.mark.parametrize("probe", [
    "true; shutdown -h now",
    "true && halt",
    "echo ok | xargs reboot",
    "echo $(whoami)",
    "echo `id`",
    "true > /tmp/x",
    "true < /etc/passwd",
    "true\nshutdown",
    "(reboot)",
])
def test_probe_safety_rejects_shell_metacharacters(probe):
    """Even commands whose prefix is benign get rejected if they chain."""
    assert not is_safe_probe_command(probe)


@pytest.mark.unit
@pytest.mark.parametrize("probe", [
    "true",
    "uname -a",
    "hostname",
    "echo ok",
    "/bin/true",
    "true ",  # trailing whitespace stripped
])
def test_probe_safety_accepts_harmless_commands(probe):
    assert is_safe_probe_command(probe)


@pytest.mark.unit
@pytest.mark.parametrize("probe", ["", None, "   "])
def test_probe_safety_rejects_empty(probe):
    assert not is_safe_probe_command(probe)


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
def test_remote_health_recovery_notification_only_after_failed(tmp_path, remote_server):
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
        assert manager.check_once()[0]["status"] == REMOTE_HEALTH_DEGRADED
    with patch("eneru.remote_health.run_remote_probe",
               return_value=(True, "", 8)):
        assert manager.check_once()[0]["status"] == REMOTE_HEALTH_HEALTHY

    assert notifications == []


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
def test_remote_health_records_only_status_transitions(tmp_path, remote_server):
    config = Config()
    config.remote_health.enabled = True
    config.remote_health.failure_threshold = 1
    events = []
    manager = RemoteHealthManager(
        config=config,
        group_label="Rack",
        servers=[remote_server],
        sidecar_path=tmp_path / "state.remote-health.json",
        stop_event=threading.Event(),
        log_fn=lambda msg: None,
        notify_fn=lambda body, typ: None,
        event_fn=lambda etype, detail, notified: events.append(
            (etype, detail, notified)
        ),
    )

    with patch("eneru.remote_health.run_remote_probe",
               return_value=(False, "refused", 12)):
        manager.check_once()
        manager.check_once()
    with patch("eneru.remote_health.run_remote_probe",
               return_value=(True, "", 8)):
        manager.check_once()

    assert [event[0] for event in events] == [
        "REMOTE_HEALTH_FAILED",
        "REMOTE_HEALTH_HEALTHY",
    ]
    assert events[0][2] is True
    assert events[1][2] is True
    assert "UNKNOWN -> FAILED" in events[0][1]
    assert "FAILED -> HEALTHY" in events[1][1]


@pytest.mark.unit
def test_remote_health_does_not_record_initial_healthy_baseline(tmp_path, remote_server):
    config = Config()
    config.remote_health.enabled = True
    events = []
    manager = RemoteHealthManager(
        config=config,
        group_label="Rack",
        servers=[remote_server],
        sidecar_path=tmp_path / "state.remote-health.json",
        stop_event=threading.Event(),
        log_fn=lambda msg: None,
        event_fn=lambda etype, detail, notified: events.append(
            (etype, detail, notified)
        ),
    )

    with patch("eneru.remote_health.run_remote_probe",
               return_value=(True, "", 8)):
        rows = manager.check_once()

    assert rows[0]["status"] == REMOTE_HEALTH_HEALTHY
    assert events == []


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


@pytest.mark.unit
def test_remote_health_start_creates_daemon_thread(tmp_path, remote_server,
                                                  monkeypatch):
    created = []
    config = Config()
    config.remote_health.enabled = True

    class FakeThread:
        def __init__(self, *, target, name, daemon):
            self.target = target
            self.name = name
            self.daemon = daemon
            self.started = False
            created.append(self)

        def start(self):
            self.started = True

    manager = RemoteHealthManager(
        config=config,
        group_label="Rack",
        servers=[remote_server],
        sidecar_path=tmp_path / "state.remote-health.json",
        stop_event=threading.Event(),
        log_fn=lambda msg: None,
    )
    monkeypatch.setattr("eneru.remote_health.threading.Thread", FakeThread)

    manager.start()
    manager.start()

    assert len(created) == 1
    assert created[0].target == manager._run_loop
    assert created[0].name == "remote-health-Rack"
    assert created[0].daemon is True
    assert created[0].started is True
    assert manager._thread is created[0]


@pytest.mark.unit
def test_remote_health_stop_keeps_alive_thread_reference(tmp_path,
                                                        remote_server):
    class AliveThread:
        def __init__(self):
            self.join_timeout = None

        def join(self, timeout=None):
            self.join_timeout = timeout

        def is_alive(self):
            return True

    config = Config()
    config.remote_health.enabled = True
    stop_event = threading.Event()
    manager = RemoteHealthManager(
        config=config,
        group_label="Rack",
        servers=[remote_server],
        sidecar_path=tmp_path / "state.remote-health.json",
        stop_event=stop_event,
        log_fn=lambda msg: None,
    )
    thread = AliveThread()
    manager._thread = thread

    manager.stop(timeout=7)

    assert stop_event.is_set()
    assert thread.join_timeout == 7
    assert manager._thread is thread


@pytest.mark.unit
def test_remote_health_run_loop_startup_check_then_waits(tmp_path,
                                                        remote_server):
    config = Config()
    config.remote_health.enabled = True
    config.remote_health.startup_check = True
    config.remote_health.interval = 1
    waits = []
    stop_event = threading.Event()

    def stop_after_first_wait(timeout):
        waits.append(timeout)
        return True

    stop_event.wait = stop_after_first_wait
    manager = RemoteHealthManager(
        config=config,
        group_label="Rack",
        servers=[remote_server],
        sidecar_path=tmp_path / "state.remote-health.json",
        stop_event=stop_event,
        log_fn=lambda msg: None,
    )

    with patch.object(manager, "check_once") as check_once:
        manager._run_loop()

    check_once.assert_called_once()
    assert waits == [60]


@pytest.mark.unit
def test_remote_health_run_loop_without_startup_check_runs_after_wait(
    tmp_path, remote_server,
):
    config = Config()
    config.remote_health.enabled = True
    config.remote_health.startup_check = False
    config.remote_health.interval = 75
    waits = []

    class StopAfterSecondWait:
        def wait(self, timeout):
            waits.append(timeout)
            return len(waits) >= 2

        def set(self):
            pass

    manager = RemoteHealthManager(
        config=config,
        group_label="Rack",
        servers=[remote_server],
        sidecar_path=tmp_path / "state.remote-health.json",
        stop_event=StopAfterSecondWait(),
        log_fn=lambda msg: None,
    )

    with patch.object(manager, "check_once") as check_once:
        manager._run_loop()

    check_once.assert_called_once()
    assert waits == [75, 75]


@pytest.mark.unit
def test_remote_health_stats_event_failure_logs_only_once(tmp_path,
                                                         remote_server):
    config = Config()
    config.remote_health.enabled = True
    config.remote_health.failure_threshold = 1
    logs = []
    manager = RemoteHealthManager(
        config=config,
        group_label="Rack",
        servers=[remote_server],
        sidecar_path=tmp_path / "state.remote-health.json",
        stop_event=threading.Event(),
        log_fn=logs.append,
        event_fn=lambda etype, detail, notified: (_ for _ in ()).throw(
            RuntimeError("db down")
        ),
    )

    with patch("eneru.remote_health.run_remote_probe",
               return_value=(False, "refused", 12)):
        manager.check_once()
        manager.check_once()

    assert len([m for m in logs if "stats event failed" in m]) == 1


# --- v5.5: host-loopback identity guard + louder failure notification ---


def _make_loopback_server(**overrides):
    base = dict(
        name="host-loopback",
        enabled=True,
        host="127.0.0.1",
        user="root",
        connect_timeout=3,
        is_host_loopback=True,
        host_identity_command="cat /etc/machine-id",
        expected_host_identity="abc123",
    )
    base.update(overrides)
    return RemoteServerConfig(**base)


@pytest.mark.unit
def test_loopback_status_tagged_in_snapshot(tmp_path):
    """RemoteHealthStatus surfaces is_host_loopback so API/TUI can render it."""
    config = Config()
    config.remote_health.enabled = True
    manager = RemoteHealthManager(
        config=config,
        group_label="Rack",
        servers=[_make_loopback_server()],
        sidecar_path=tmp_path / "state.remote-health.json",
        stop_event=threading.Event(),
        log_fn=lambda msg: None,
    )
    rows = manager.snapshot()
    assert rows[0]["is_host_loopback"] is True


@pytest.mark.unit
def test_loopback_auto_populates_expected_identity_from_etc_machine_id(tmp_path):
    """When config omits expected_host_identity, the manager fills it in
    from container-side /etc/machine-id at __init__."""
    config = Config()
    config.remote_health.enabled = True
    server = _make_loopback_server(expected_host_identity=None)

    with patch("eneru.remote_health._read_container_machine_id",
               return_value="aabbcc-host-id"):
        RemoteHealthManager(
            config=config,
            group_label="Rack",
            servers=[server],
            sidecar_path=tmp_path / "state.remote-health.json",
            stop_event=threading.Event(),
            log_fn=lambda msg: None,
        )
    assert server.expected_host_identity == "aabbcc-host-id"


@pytest.mark.unit
def test_loopback_explicit_expected_identity_not_overwritten(tmp_path):
    """If the operator set it explicitly, the auto-populate must not clobber."""
    config = Config()
    config.remote_health.enabled = True
    server = _make_loopback_server(expected_host_identity="operator-set-id")

    with patch("eneru.remote_health._read_container_machine_id",
               return_value="DIFFERENT"):
        RemoteHealthManager(
            config=config,
            group_label="Rack",
            servers=[server],
            sidecar_path=tmp_path / "state.remote-health.json",
            stop_event=threading.Event(),
            log_fn=lambda msg: None,
        )
    assert server.expected_host_identity == "operator-set-id"


@pytest.mark.unit
def test_loopback_identity_match_keeps_healthy(tmp_path):
    """Standard probe pass + identity match → HEALTHY."""
    config = Config()
    config.remote_health.enabled = True
    config.remote_health.failure_threshold = 1
    server = _make_loopback_server()

    manager = RemoteHealthManager(
        config=config,
        group_label="Rack",
        servers=[server],
        sidecar_path=tmp_path / "state.remote-health.json",
        stop_event=threading.Event(),
        log_fn=lambda msg: None,
    )
    with patch("eneru.remote_health.run_remote_probe",
               return_value=(True, "", 8)), \
         patch("eneru.remote_health.run_loopback_identity_probe",
               return_value=(True, "", 3)):
        rows = manager.check_once()
    assert rows[0]["status"] == REMOTE_HEALTH_HEALTHY


@pytest.mark.unit
def test_loopback_probe_runs_even_when_remote_health_disabled(tmp_path):
    """remote_health.enabled=false disables regular remotes, not loopback identity guard."""
    config = Config()
    config.remote_health.enabled = False
    config.remote_health.failure_threshold = 1
    server = _make_loopback_server()

    manager = RemoteHealthManager(
        config=config,
        group_label="Rack",
        servers=[server],
        sidecar_path=tmp_path / "state.remote-health.json",
        stop_event=threading.Event(),
        log_fn=lambda msg: None,
    )
    with patch("eneru.remote_health.run_remote_probe",
               return_value=(True, "", 8)) as probe_mock, \
         patch("eneru.remote_health.run_loopback_identity_probe",
               return_value=(True, "", 3)) as id_mock:
        rows = manager.check_once()

    assert rows[0]["status"] == REMOTE_HEALTH_HEALTHY
    probe_mock.assert_called_once()
    id_mock.assert_called_once()


@pytest.mark.unit
def test_regular_remote_stays_disabled_when_remote_health_disabled(tmp_path):
    config = Config()
    config.remote_health.enabled = False
    server = RemoteServerConfig(
        name="nas",
        enabled=True,
        host="nas.example",
        user="root",
    )
    manager = RemoteHealthManager(
        config=config,
        group_label="Rack",
        servers=[server],
        sidecar_path=tmp_path / "state.remote-health.json",
        stop_event=threading.Event(),
        log_fn=lambda msg: None,
    )
    with patch("eneru.remote_health.run_remote_probe") as probe_mock:
        rows = manager.check_once()

    assert rows[0]["status"] == REMOTE_HEALTH_DISABLED
    probe_mock.assert_not_called()


@pytest.mark.unit
def test_empty_machine_id_fails_with_setup_hint(tmp_path):
    """Missing host identity should point at machine-id setup, not limp through."""
    config = Config()
    config.remote_health.enabled = True
    config.remote_health.failure_threshold = 1
    server = _make_loopback_server(expected_host_identity=None)
    with patch("eneru.remote_health._read_container_machine_id", return_value=None):
        manager = RemoteHealthManager(
            config=config,
            group_label="Rack",
            servers=[server],
            sidecar_path=tmp_path / "state.remote-health.json",
            stop_event=threading.Event(),
            log_fn=lambda msg: None,
        )

    with patch("eneru.remote_health.run_remote_probe",
               return_value=(True, "", 8)):
        rows = manager.check_once()

    assert rows[0]["status"] == REMOTE_HEALTH_FAILED
    assert "systemd-machine-id-setup" in rows[0]["last_error"]


@pytest.mark.unit
def test_forced_command_shutdown_response_fails_with_command_hint(tmp_path):
    """authorized_keys command= rewrites probes; call that out directly."""
    server = _make_loopback_server(expected_host_identity="abc123")
    with patch("eneru.remote_health.run_command",
               return_value=(0, "Shutdown scheduled for Sun 2026-05-17\n", "")):
        ok, error, _ = run_loopback_identity_probe(server)

    assert ok is False
    assert "authorized_keys command=" in error


@pytest.mark.unit
def test_loopback_identity_mismatch_marks_failed_with_hint(tmp_path):
    """Probe passes but identity doesn't match → FAILED with bind-mount hint."""
    config = Config()
    config.remote_health.enabled = True
    config.remote_health.failure_threshold = 1
    server = _make_loopback_server()

    notifications = []
    manager = RemoteHealthManager(
        config=config,
        group_label="Rack",
        servers=[server],
        sidecar_path=tmp_path / "state.remote-health.json",
        stop_event=threading.Event(),
        log_fn=lambda msg: None,
        notify_fn=lambda body, typ: notifications.append((body, typ)),
    )
    mismatch_msg = (
        "host identity mismatch: probe returned 'wrong' but expected 'abc123'. "
        "Most likely cause: /etc/machine-id is NOT bind-mounted from the host."
    )
    with patch("eneru.remote_health.run_remote_probe",
               return_value=(True, "", 8)), \
         patch("eneru.remote_health.run_loopback_identity_probe",
               return_value=(False, mismatch_msg, 3)):
        rows = manager.check_once()
    assert rows[0]["status"] == REMOTE_HEALTH_FAILED
    assert "host identity mismatch" in rows[0]["last_error"]
    assert "bind-mount" in rows[0]["last_error"]
    # Louder notification used for loopback failures.
    assert notifications
    body, _ = notifications[0]
    assert "Host Loopback FAILED" in body
    assert "Under a real power outage" in body


@pytest.mark.unit
def test_loopback_skipped_identity_probe_when_main_probe_fails(tmp_path):
    """If SSH itself fails, don't run the identity probe — first failure wins."""
    config = Config()
    config.remote_health.enabled = True
    config.remote_health.failure_threshold = 1
    server = _make_loopback_server()

    manager = RemoteHealthManager(
        config=config,
        group_label="Rack",
        servers=[server],
        sidecar_path=tmp_path / "state.remote-health.json",
        stop_event=threading.Event(),
        log_fn=lambda msg: None,
    )
    with patch("eneru.remote_health.run_remote_probe",
               return_value=(False, "connection refused", 12)) as probe_mock, \
         patch("eneru.remote_health.run_loopback_identity_probe") as id_mock:
        manager.check_once()
        probe_mock.assert_called_once()
        id_mock.assert_not_called()


@pytest.mark.unit
def test_non_loopback_server_skips_identity_probe(tmp_path):
    """Regular remote_servers don't run identity probes — only loopback does."""
    config = Config()
    config.remote_health.enabled = True
    server = RemoteServerConfig(
        name="nas",
        enabled=True,
        host="nas.example",
        user="ups",
        connect_timeout=3,
        is_host_loopback=False,  # not a loopback
    )
    manager = RemoteHealthManager(
        config=config,
        group_label="Rack",
        servers=[server],
        sidecar_path=tmp_path / "state.remote-health.json",
        stop_event=threading.Event(),
        log_fn=lambda msg: None,
    )
    with patch("eneru.remote_health.run_remote_probe",
               return_value=(True, "", 8)), \
         patch("eneru.remote_health.run_loopback_identity_probe") as id_mock:
        manager.check_once()
        id_mock.assert_not_called()
