"""Tests for ContainerShutdownMixin (Docker/Podman + compose phase)."""

import pytest
from unittest.mock import MagicMock, patch

from eneru import (
    Config,
    UPSGroupConfig,
    UPSConfig,
    ContainersConfig,
    ComposeFileConfig,
    BehaviorConfig,
    LoggingConfig,
    LocalShutdownConfig,
    UPSGroupMonitor,
    MonitorState,
)


def _make_container_monitor(
    tmp_path,
    *,
    enabled=True,
    runtime="auto",
    container_runtime="docker",
    compose_available=True,
    compose_files=None,
    shutdown_all_remaining=True,
    include_user_containers=False,
    stop_timeout=30,
    dry_run=False,
):
    if compose_files is None:
        compose_files = []
    config = Config(
        ups_groups=[UPSGroupConfig(
            ups=UPSConfig(name="UPS1@host"),
            containers=ContainersConfig(
                enabled=enabled,
                runtime=runtime,
                stop_timeout=stop_timeout,
                shutdown_all_remaining_containers=shutdown_all_remaining,
                include_user_containers=include_user_containers,
                compose_files=compose_files,
            ),
            is_local=True,
        )],
        behavior=BehaviorConfig(dry_run=dry_run),
        logging=LoggingConfig(
            shutdown_flag_file=str(tmp_path / "flag"),
            state_file=str(tmp_path / "state"),
            battery_history_file=str(tmp_path / "history"),
        ),
        local_shutdown=LocalShutdownConfig(enabled=False),
    )
    monitor = UPSGroupMonitor(config)
    monitor.state = MonitorState()
    monitor.logger = MagicMock()
    monitor._notification_worker = None
    monitor._container_runtime = container_runtime
    monitor._compose_available = compose_available
    return monitor


# ----------------------------------------------------------------------
# _detect_container_runtime
# ----------------------------------------------------------------------


@pytest.mark.unit
def test_detect_runtime_docker_explicit(tmp_path):
    """runtime='docker' returns 'docker' when docker is on PATH."""
    monitor = _make_container_monitor(tmp_path, runtime="docker")
    with patch("eneru.shutdown.containers.command_exists", return_value=True):
        assert monitor._detect_container_runtime() == "docker"


@pytest.mark.unit
def test_detect_runtime_docker_missing(tmp_path):
    """runtime='docker' returns None and warns when docker is not on PATH."""
    monitor = _make_container_monitor(tmp_path, runtime="docker")
    with patch("eneru.shutdown.containers.command_exists", return_value=False):
        assert monitor._detect_container_runtime() is None


@pytest.mark.unit
def test_detect_runtime_podman_explicit(tmp_path):
    """runtime='podman' returns 'podman' when podman is on PATH."""
    monitor = _make_container_monitor(tmp_path, runtime="podman")
    with patch("eneru.shutdown.containers.command_exists", return_value=True):
        assert monitor._detect_container_runtime() == "podman"


@pytest.mark.unit
def test_detect_runtime_podman_missing(tmp_path):
    """runtime='podman' returns None when podman is missing."""
    monitor = _make_container_monitor(tmp_path, runtime="podman")
    with patch("eneru.shutdown.containers.command_exists", return_value=False):
        assert monitor._detect_container_runtime() is None


@pytest.mark.unit
def test_detect_runtime_auto_prefers_podman(tmp_path):
    """runtime='auto' picks podman first when both are present."""
    monitor = _make_container_monitor(tmp_path, runtime="auto")
    with patch("eneru.shutdown.containers.command_exists", return_value=True):
        assert monitor._detect_container_runtime() == "podman"


@pytest.mark.unit
def test_detect_runtime_auto_falls_back_to_docker(tmp_path):
    """runtime='auto' falls back to docker when podman is not present."""
    monitor = _make_container_monitor(tmp_path, runtime="auto")
    with patch(
        "eneru.shutdown.containers.command_exists",
        side_effect=lambda cmd: cmd == "docker",
    ):
        assert monitor._detect_container_runtime() == "docker"


@pytest.mark.unit
def test_detect_runtime_auto_neither_present(tmp_path):
    """runtime='auto' returns None when no runtime is present."""
    monitor = _make_container_monitor(tmp_path, runtime="auto")
    with patch("eneru.shutdown.containers.command_exists", return_value=False):
        assert monitor._detect_container_runtime() is None


@pytest.mark.unit
def test_detect_runtime_unknown_value(tmp_path):
    """An unrecognized runtime value warns and returns None."""
    monitor = _make_container_monitor(tmp_path, runtime="containerd")
    assert monitor._detect_container_runtime() is None


# ----------------------------------------------------------------------
# _check_compose_available
# ----------------------------------------------------------------------


@pytest.mark.unit
def test_compose_available_no_runtime(tmp_path):
    """Without _container_runtime, compose check returns False."""
    monitor = _make_container_monitor(tmp_path)
    monitor._container_runtime = None
    assert monitor._check_compose_available() is False


@pytest.mark.unit
def test_compose_available_runtime_supports_compose(tmp_path):
    """Compose availability is True when 'docker compose version' returns 0."""
    monitor = _make_container_monitor(tmp_path)
    with patch("eneru.shutdown.containers.run_command", return_value=(0, "v2", "")):
        assert monitor._check_compose_available() is True


@pytest.mark.unit
def test_compose_available_runtime_lacks_compose(tmp_path):
    """Compose availability is False when the subcommand is not present."""
    monitor = _make_container_monitor(tmp_path)
    with patch("eneru.shutdown.containers.run_command", return_value=(127, "", "not found")):
        assert monitor._check_compose_available() is False


# ----------------------------------------------------------------------
# _shutdown_containers (top-level orchestration)
# ----------------------------------------------------------------------


@pytest.mark.unit
def test_shutdown_containers_disabled_no_op(tmp_path):
    """containers.enabled=False short-circuits the entire phase."""
    monitor = _make_container_monitor(tmp_path, enabled=False)
    with patch("eneru.shutdown.containers.run_command") as mock_run:
        monitor._shutdown_containers()
    mock_run.assert_not_called()


@pytest.mark.unit
def test_shutdown_containers_no_runtime_no_op(tmp_path):
    """When no container runtime was detected, the phase is a no-op."""
    monitor = _make_container_monitor(tmp_path, container_runtime=None)
    with patch("eneru.shutdown.containers.run_command") as mock_run:
        monitor._shutdown_containers()
    mock_run.assert_not_called()


@pytest.mark.unit
def test_shutdown_containers_skip_remaining_when_disabled(tmp_path):
    """shutdown_all_remaining_containers=False skips the 'docker ps -q' phase."""
    monitor = _make_container_monitor(
        tmp_path,
        compose_available=False,  # also skip compose phase
        shutdown_all_remaining=False,
    )
    with patch("eneru.shutdown.containers.run_command") as mock_run:
        monitor._shutdown_containers()
    mock_run.assert_not_called()


@pytest.mark.unit
def test_shutdown_containers_no_running_containers(tmp_path):
    """Empty container list logs an info message and exits cleanly."""
    monitor = _make_container_monitor(tmp_path, compose_available=False)
    with patch("eneru.shutdown.containers.run_command", return_value=(0, "", "")) as mock_run:
        monitor._shutdown_containers()
    # docker ps -q only
    assert mock_run.call_count == 1


@pytest.mark.unit
def test_shutdown_containers_dry_run_lists_names(tmp_path):
    """In dry-run, the container list is fetched twice (-q and --format names) but never stopped."""
    monitor = _make_container_monitor(tmp_path, compose_available=False, dry_run=True)

    def fake_run(cmd, **kwargs):
        if cmd[-1] == "-q":
            return (0, "abc123\ndef456\n", "")
        if "{{.Names}}" in cmd:
            return (0, "web\nworker\n", "")
        # No 'docker stop' should ever be reached in dry-run
        raise AssertionError(f"Unexpected stop call in dry-run: {cmd}")

    with patch("eneru.shutdown.containers.run_command", side_effect=fake_run):
        monitor._shutdown_containers()


@pytest.mark.unit
def test_shutdown_containers_real_stop_call(tmp_path):
    """Real (non-dry-run) path issues docker stop with timeout and IDs."""
    monitor = _make_container_monitor(tmp_path, compose_available=False, stop_timeout=20)

    def fake_run(cmd, **kwargs):
        if cmd[-1] == "-q":
            return (0, "abc123\ndef456\n", "")
        return (0, "", "")

    with patch("eneru.shutdown.containers.run_command", side_effect=fake_run) as mock_run:
        monitor._shutdown_containers()

    stop_calls = [c for c in mock_run.call_args_list if c.args[0][:2] == ["docker", "stop"]]
    assert len(stop_calls) == 1
    cmd = stop_calls[0].args[0]
    assert "-t" in cmd and "20" in cmd
    assert "abc123" in cmd and "def456" in cmd


@pytest.mark.unit
def test_shutdown_containers_ps_failure_logs_warning(tmp_path):
    """A failing 'docker ps' logs a warning and exits the phase."""
    monitor = _make_container_monitor(tmp_path, compose_available=False)
    with patch("eneru.shutdown.containers.run_command", return_value=(1, "", "err")) as mock_run:
        monitor._shutdown_containers()
    # Only the failing ps -q call
    assert mock_run.call_count == 1


# ----------------------------------------------------------------------
# _shutdown_compose_stacks
# ----------------------------------------------------------------------


@pytest.mark.unit
def test_compose_stacks_skipped_when_unavailable(tmp_path):
    """compose subcommand unavailable: phase is a no-op."""
    monitor = _make_container_monitor(tmp_path, compose_available=False)
    with patch("eneru.shutdown.containers.run_command") as mock_run:
        monitor._shutdown_compose_stacks()
    mock_run.assert_not_called()


@pytest.mark.unit
def test_compose_stacks_skipped_when_no_files(tmp_path):
    """compose_available=True but no compose_files: phase is a no-op."""
    monitor = _make_container_monitor(tmp_path)
    with patch("eneru.shutdown.containers.run_command") as mock_run:
        monitor._shutdown_compose_stacks()
    mock_run.assert_not_called()


@pytest.mark.unit
def test_compose_stacks_skips_missing_file(tmp_path):
    """A configured compose file that doesn't exist on disk is skipped with a warning."""
    monitor = _make_container_monitor(
        tmp_path,
        compose_files=[ComposeFileConfig(path=str(tmp_path / "missing.yml"))],
    )
    with patch("eneru.shutdown.containers.run_command") as mock_run:
        monitor._shutdown_compose_stacks()
    mock_run.assert_not_called()


@pytest.mark.unit
def test_compose_stacks_dry_run(tmp_path):
    """Dry-run logs the compose down but doesn't invoke it."""
    cf = tmp_path / "compose.yml"
    cf.write_text("services: {}\n")
    monitor = _make_container_monitor(
        tmp_path,
        compose_files=[ComposeFileConfig(path=str(cf))],
        dry_run=True,
    )
    with patch("eneru.shutdown.containers.run_command") as mock_run:
        monitor._shutdown_compose_stacks()
    mock_run.assert_not_called()


@pytest.mark.unit
def test_compose_stacks_success(tmp_path):
    """Successful 'docker compose down' returns 0 and the stack is logged as stopped."""
    cf = tmp_path / "compose.yml"
    cf.write_text("services: {}\n")
    monitor = _make_container_monitor(
        tmp_path,
        compose_files=[ComposeFileConfig(path=str(cf), stop_timeout=15)],
    )
    with patch("eneru.shutdown.containers.run_command", return_value=(0, "", "")) as mock_run:
        monitor._shutdown_compose_stacks()
    mock_run.assert_called_once()
    cmd = mock_run.call_args.args[0]
    assert cmd == ["docker", "compose", "-f", str(cf), "down"]


@pytest.mark.unit
def test_compose_stacks_per_file_timeout_overrides_global(tmp_path):
    """Per-file stop_timeout takes precedence over global containers.stop_timeout."""
    cf = tmp_path / "compose.yml"
    cf.write_text("services: {}\n")
    monitor = _make_container_monitor(
        tmp_path,
        compose_files=[ComposeFileConfig(path=str(cf), stop_timeout=99)],
        stop_timeout=30,
    )
    with patch("eneru.shutdown.containers.run_command", return_value=(0, "", "")) as mock_run:
        monitor._shutdown_compose_stacks()
    # run_command kwargs should reflect per-file timeout + 30 buffer
    assert mock_run.call_args.kwargs["timeout"] == 129


@pytest.mark.unit
def test_compose_stacks_global_timeout_used_when_per_file_none(tmp_path):
    """When per-file stop_timeout is None, global stop_timeout is used."""
    cf = tmp_path / "compose.yml"
    cf.write_text("services: {}\n")
    monitor = _make_container_monitor(
        tmp_path,
        compose_files=[ComposeFileConfig(path=str(cf), stop_timeout=None)],
        stop_timeout=45,
    )
    with patch("eneru.shutdown.containers.run_command", return_value=(0, "", "")) as mock_run:
        monitor._shutdown_compose_stacks()
    assert mock_run.call_args.kwargs["timeout"] == 75  # 45 + 30 buffer


@pytest.mark.unit
def test_compose_stacks_timeout_exit_code_is_logged(tmp_path):
    """Compose down returning 124 (timeout) is treated as best-effort, not a hard failure."""
    cf = tmp_path / "compose.yml"
    cf.write_text("services: {}\n")
    monitor = _make_container_monitor(
        tmp_path,
        compose_files=[ComposeFileConfig(path=str(cf))],
    )
    with patch("eneru.shutdown.containers.run_command", return_value=(124, "", "")):
        monitor._shutdown_compose_stacks()  # Must not raise
