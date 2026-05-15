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
from eneru.shutdown.containers import (
    _container_id_tokens,
    _container_ids_from_mountinfo,
    _looks_like_container_id,
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
    # Default to "no self-detected container IDs" so tests are deterministic
    # even if pytest itself runs inside Docker/Podman. Self-detection-specific
    # tests override this attribute on the returned monitor.
    monitor._current_container_ids = lambda: set()
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
def test_shutdown_containers_dry_run_logs_filtered_id_list(tmp_path):
    """Dry-run preview lists IDs from the same filtered set the real run would
    stop — no second `ps --format` query and no chance of including a container
    that self-detection just excluded."""
    monitor = _make_container_monitor(tmp_path, compose_available=False, dry_run=True)
    log = []
    monitor._log_message = log.append

    def fake_run(cmd, **kwargs):
        if cmd[-1] == "-q":
            return (0, "abc123\ndef456\n", "")
        # No 'docker ps --format' second query, no 'docker stop' in dry-run.
        raise AssertionError(f"Unexpected call in dry-run: {cmd}")

    with patch("eneru.shutdown.containers.run_command", side_effect=fake_run):
        monitor._shutdown_containers()

    dry_lines = [m for m in log if "DRY-RUN" in m]
    assert len(dry_lines) == 1
    assert "abc123" in dry_lines[0] and "def456" in dry_lines[0]


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
def test_shutdown_containers_skips_current_container(tmp_path):
    """The remaining-container phase must not stop Eneru's own container."""
    monitor = _make_container_monitor(tmp_path, compose_available=False, stop_timeout=20)
    monitor._current_container_ids = lambda: {"abc123456789"}

    def fake_run(cmd, **kwargs):
        if cmd[-1] == "-q":
            return (0, "abc123456789\ndef456\n", "")
        return (0, "", "")

    with patch("eneru.shutdown.containers.run_command", side_effect=fake_run) as mock_run:
        monitor._shutdown_containers()

    stop_calls = [c for c in mock_run.call_args_list if c.args[0][:2] == ["docker", "stop"]]
    assert len(stop_calls) == 1
    cmd = stop_calls[0].args[0]
    assert "def456" in cmd
    assert "abc123456789" not in cmd


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
def test_compose_stacks_skips_file_that_contains_current_container(tmp_path):
    """Compose down is skipped when the project includes Eneru itself."""
    cf = tmp_path / "compose.yml"
    cf.write_text("services: {}\n")
    monitor = _make_container_monitor(
        tmp_path,
        compose_files=[ComposeFileConfig(path=str(cf))],
    )
    monitor._current_container_ids = lambda: {"abc123456789"}

    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["docker", "compose", "-f"] and cmd[-2:] == ["ps", "-q"]:
            return (0, "abc123456789\n", "")
        raise AssertionError(f"compose down should not be called: {cmd}")

    with patch("eneru.shutdown.containers.run_command", side_effect=fake_run):
        monitor._shutdown_compose_stacks()


@pytest.mark.unit
def test_container_id_tokens_extracts_systemd_scope_ids():
    """cgroup parsing handles Docker/containerd systemd scope names."""
    text = (
        "0::/system.slice/docker-"
        "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
        ".scope\n"
    )

    assert _container_id_tokens(text) == {
        "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    }


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


# ----------------------------------------------------------------------
# Self-container detection helpers
# ----------------------------------------------------------------------

_FULL_ID = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
_SHORT_ID = _FULL_ID[:12]


@pytest.mark.unit
def test_looks_like_container_id_short_boundary_passes():
    assert _looks_like_container_id(_SHORT_ID) is True


@pytest.mark.unit
def test_looks_like_container_id_full_boundary_passes():
    assert _looks_like_container_id(_FULL_ID) is True


@pytest.mark.unit
def test_looks_like_container_id_too_short_rejected():
    assert _looks_like_container_id(_FULL_ID[:11]) is False


@pytest.mark.unit
def test_looks_like_container_id_too_long_rejected():
    assert _looks_like_container_id(_FULL_ID + "0") is False


@pytest.mark.unit
def test_looks_like_container_id_non_hex_rejected():
    # 12 chars but contains 'g' which is not hex
    assert _looks_like_container_id("abcdefg01234") is False


@pytest.mark.unit
def test_container_id_tokens_cgroup_v1_docker_scope():
    """cgroup v1 line: numeric:subsystem:/system.slice/docker-<id>.scope."""
    text = f"12:perf_event:/system.slice/docker-{_FULL_ID}.scope\n"
    assert _container_id_tokens(text) == {_FULL_ID}


@pytest.mark.unit
def test_container_id_tokens_cgroup_v2_unified_bare_id():
    """cgroup v2 unified: 0::/system.slice/docker-<id>.scope."""
    text = f"0::/kubepods.slice/kubepods-burstable.slice/{_FULL_ID}\n"
    assert _container_id_tokens(text) == {_FULL_ID}


@pytest.mark.unit
def test_container_id_tokens_cri_containerd_scope():
    """K8s + containerd uses cri-containerd-<id>.scope."""
    text = f"0::/system.slice/cri-containerd-{_FULL_ID}.scope\n"
    assert _container_id_tokens(text) == {_FULL_ID}


@pytest.mark.unit
def test_container_id_tokens_no_match_for_bare_metal_paths():
    """Bare-metal hosts have no container IDs to extract."""
    text = (
        "12:devices:/user.slice/user-1000.slice/session-2.scope\n"
        "11:memory:/user.slice\n"
    )
    assert _container_id_tokens(text) == set()


@pytest.mark.unit
def test_is_current_container_short_id_matches_full_id(tmp_path):
    monitor = _make_container_monitor(tmp_path)
    assert monitor._is_current_container(_SHORT_ID, {_FULL_ID}) is True


@pytest.mark.unit
def test_is_current_container_full_id_matches_short_id(tmp_path):
    monitor = _make_container_monitor(tmp_path)
    assert monitor._is_current_container(_FULL_ID, {_SHORT_ID}) is True


@pytest.mark.unit
def test_is_current_container_disjoint_returns_false(tmp_path):
    monitor = _make_container_monitor(tmp_path)
    other = "fedcba9876543210" + _FULL_ID[16:]
    assert monitor._is_current_container(other, {_FULL_ID}) is False


@pytest.mark.unit
def test_is_current_container_empty_id_returns_false(tmp_path):
    monitor = _make_container_monitor(tmp_path)
    assert monitor._is_current_container("", {_FULL_ID}) is False


@pytest.mark.unit
def test_is_current_container_empty_current_ids_returns_false(tmp_path):
    """When self-detection found nothing, no container is "ours"."""
    monitor = _make_container_monitor(tmp_path)
    assert monitor._is_current_container(_FULL_ID, set()) is False


@pytest.mark.unit
def test_compose_stack_contains_self_returns_false_when_no_current_ids(tmp_path):
    """Bare-metal Eneru (no detected container) must not skip ANY compose file."""
    monitor = _make_container_monitor(tmp_path)
    # Shared fixture already stubs _current_container_ids to lambda: set()
    # — confirm the early-return short-circuits before run_command is touched.
    with patch("eneru.shutdown.containers.run_command",
               side_effect=AssertionError("must not be called")):
        assert monitor._compose_stack_contains_self("/some/file.yml") is False


@pytest.mark.unit
def test_compose_stack_contains_self_handles_compose_ps_failure(tmp_path):
    """If `compose ps -q` returns nonzero, treat as 'doesn't contain self'."""
    monitor = _make_container_monitor(tmp_path)
    monitor._current_container_ids = lambda: {_SHORT_ID}
    with patch("eneru.shutdown.containers.run_command",
               return_value=(1, "", "compose: error")):
        assert monitor._compose_stack_contains_self("/missing.yml") is False


@pytest.mark.unit
def test_current_container_ids_handles_missing_cgroup_files(tmp_path):
    """No /proc/self/cgroup (bare metal) → must not raise; hostname-only fallback."""
    monitor = _make_container_monitor(tmp_path)
    # Restore the real method (the shared fixture stubs it for determinism).
    del monitor._current_container_ids
    with patch("eneru.shutdown.containers.socket.gethostname", return_value="laptop"), \
         patch("pathlib.Path.read_text", side_effect=OSError):
        ids = monitor._current_container_ids()
    assert ids == set()  # Hostname "laptop" doesn't look like a container ID, no cgroup data


@pytest.mark.unit
def test_current_container_ids_picks_up_hostname_when_it_looks_like_id(tmp_path):
    """Docker sets hostname to the short container ID by default."""
    monitor = _make_container_monitor(tmp_path)
    # Restore the real method (the shared fixture stubs it for determinism).
    del monitor._current_container_ids
    with patch("eneru.shutdown.containers.socket.gethostname", return_value=_SHORT_ID), \
         patch("pathlib.Path.read_text", side_effect=OSError):
        ids = monitor._current_container_ids()
    assert ids == {_SHORT_ID}


# ----------------------------------------------------------------------
# Mountinfo-based container ID detection (cgroupns workaround)
# ----------------------------------------------------------------------


@pytest.mark.unit
def test_mountinfo_extracts_docker_container_id_from_etc_hostname():
    """Docker bind-mounts /etc/hostname from /var/lib/docker/containers/<id>/hostname."""
    text = (
        f"1234 1233 0:75 /docker/containers/{_FULL_ID}/hostname /etc/hostname "
        f"rw,nosuid,nodev,noexec,relatime - tmpfs tmpfs rw\n"
    )
    assert _container_ids_from_mountinfo(text) == {_FULL_ID}


@pytest.mark.unit
def test_mountinfo_extracts_podman_container_id():
    """Podman uses /var/lib/containers/storage/overlay-containers/<id>/userdata/..."""
    text = (
        f"567 566 0:80 /var/lib/containers/storage/overlay-containers/{_FULL_ID}/userdata/hostname "
        f"/etc/hostname rw,relatime - tmpfs tmpfs rw\n"
    )
    assert _container_ids_from_mountinfo(text) == {_FULL_ID}


@pytest.mark.unit
def test_mountinfo_handles_cgroupns_only_collapsed_view():
    """When cgroupns is enabled the host paths still appear in mountinfo."""
    text = (
        # Real-shape entry for /etc/resolv.conf bind-mount inside a Docker container
        f"431 430 0:73 /var/lib/docker/containers/{_FULL_ID}/resolv.conf "
        f"/etc/resolv.conf rw,nosuid,nodev,noexec,relatime master:80 - tmpfs tmpfs rw\n"
        # Also handles /etc/hosts
        f"432 430 0:74 /var/lib/docker/containers/{_FULL_ID}/hosts "
        f"/etc/hosts rw,nosuid,nodev,noexec,relatime master:81 - tmpfs tmpfs rw\n"
    )
    assert _container_ids_from_mountinfo(text) == {_FULL_ID}


@pytest.mark.unit
def test_mountinfo_no_container_paths_returns_empty():
    """Bare-metal mountinfo has no container-runtime paths."""
    text = (
        "23 28 0:21 / /sys rw,nosuid,nodev,noexec,relatime shared:7 - sysfs sysfs rw\n"
        "24 28 0:22 / /proc rw,nosuid,nodev,noexec,relatime shared:14 - proc proc rw\n"
    )
    assert _container_ids_from_mountinfo(text) == set()


@pytest.mark.unit
def test_mountinfo_ignores_unrelated_hex_paths():
    """Overlay layer hashes outside known container-runtime prefixes must not match."""
    # An overlay layer ID at /var/lib/docker/overlay2/<hex>/... is NOT a container ID.
    text = (
        f"500 0 0:90 /var/lib/docker/overlay2/{_FULL_ID}/diff /some/path rw - overlay overlay rw\n"
    )
    assert _container_ids_from_mountinfo(text) == set()
