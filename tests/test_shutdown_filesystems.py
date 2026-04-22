"""Tests for FilesystemShutdownMixin (sync + unmount phase)."""

import pytest
from unittest.mock import MagicMock, patch

from eneru import (
    Config,
    UPSGroupConfig,
    UPSConfig,
    FilesystemsConfig,
    UnmountConfig,
    BehaviorConfig,
    LoggingConfig,
    LocalShutdownConfig,
    UPSGroupMonitor,
    MonitorState,
)


def _make_fs_monitor(tmp_path, *, sync_enabled=True, unmount_enabled=True, mounts=None, dry_run=False):
    if mounts is None:
        mounts = [{"path": "/mnt/data", "options": ""}]
    config = Config(
        ups_groups=[UPSGroupConfig(
            ups=UPSConfig(name="UPS1@host"),
            filesystems=FilesystemsConfig(
                sync_enabled=sync_enabled,
                unmount=UnmountConfig(enabled=unmount_enabled, timeout=10, mounts=mounts),
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
    return monitor


@pytest.mark.unit
def test_sync_filesystems_disabled_no_op(tmp_path):
    """sync_enabled=False returns without invoking os.sync."""
    monitor = _make_fs_monitor(tmp_path, sync_enabled=False)
    with patch("eneru.shutdown.filesystems.os.sync") as mock_sync, \
         patch("eneru.shutdown.filesystems.time.sleep"):
        monitor._sync_filesystems()
    mock_sync.assert_not_called()


@pytest.mark.unit
def test_sync_filesystems_dry_run_skips_os_sync(tmp_path):
    """Dry-run logs the action but never calls os.sync()."""
    monitor = _make_fs_monitor(tmp_path, dry_run=True)
    with patch("eneru.shutdown.filesystems.os.sync") as mock_sync:
        monitor._sync_filesystems()
    mock_sync.assert_not_called()


@pytest.mark.unit
def test_sync_filesystems_real_calls_os_sync_then_sleeps(tmp_path):
    """Real path: os.sync() + 2-second sleep for storage controller flush."""
    monitor = _make_fs_monitor(tmp_path)
    with patch("eneru.shutdown.filesystems.os.sync") as mock_sync, \
         patch("eneru.shutdown.filesystems.time.sleep") as mock_sleep:
        monitor._sync_filesystems()
    mock_sync.assert_called_once()
    mock_sleep.assert_called_once_with(2)


@pytest.mark.unit
def test_unmount_filesystems_disabled_no_op(tmp_path):
    """unmount.enabled=False returns immediately."""
    monitor = _make_fs_monitor(tmp_path, unmount_enabled=False)
    with patch("eneru.shutdown.filesystems.run_command") as mock_run:
        monitor._unmount_filesystems()
    mock_run.assert_not_called()


@pytest.mark.unit
def test_unmount_filesystems_no_mounts_no_op(tmp_path):
    """No configured mounts means no umount calls."""
    monitor = _make_fs_monitor(tmp_path, mounts=[])
    with patch("eneru.shutdown.filesystems.run_command") as mock_run:
        monitor._unmount_filesystems()
    mock_run.assert_not_called()


@pytest.mark.unit
def test_unmount_filesystems_skips_mount_with_no_path(tmp_path):
    """Mounts missing the 'path' key are silently skipped."""
    monitor = _make_fs_monitor(tmp_path, mounts=[{"path": "", "options": ""}])
    with patch("eneru.shutdown.filesystems.run_command") as mock_run:
        monitor._unmount_filesystems()
    mock_run.assert_not_called()


@pytest.mark.unit
def test_unmount_filesystems_dry_run_skips_real_call(tmp_path):
    """Dry-run logs the umount but never invokes run_command."""
    monitor = _make_fs_monitor(tmp_path, dry_run=True)
    with patch("eneru.shutdown.filesystems.run_command") as mock_run:
        monitor._unmount_filesystems()
    mock_run.assert_not_called()


@pytest.mark.unit
def test_unmount_filesystems_success(tmp_path):
    """Successful umount logs success and stops."""
    monitor = _make_fs_monitor(tmp_path)
    with patch("eneru.shutdown.filesystems.run_command", return_value=(0, "", "")) as mock_run:
        monitor._unmount_filesystems()
    # One call: the umount itself
    assert mock_run.call_count == 1
    cmd = mock_run.call_args.args[0]
    assert cmd[0] == "umount"
    assert "/mnt/data" in cmd


@pytest.mark.unit
def test_unmount_filesystems_includes_options(tmp_path):
    """Mount options are appended to the umount command."""
    monitor = _make_fs_monitor(tmp_path, mounts=[{"path": "/mnt/data", "options": "-l"}])
    with patch("eneru.shutdown.filesystems.run_command", return_value=(0, "", "")) as mock_run:
        monitor._unmount_filesystems()
    cmd = mock_run.call_args.args[0]
    assert cmd == ["umount", "-l", "/mnt/data"]


@pytest.mark.unit
def test_unmount_filesystems_multi_flag_options_split(tmp_path):
    """Multi-flag option strings like "-l -f" must be split into
    separate argv entries; the previous cmd.append(options) was passing
    them as one argument and umount rejected the literal as unknown."""
    monitor = _make_fs_monitor(
        tmp_path, mounts=[{"path": "/mnt/data", "options": "-l -f"}],
    )
    with patch("eneru.shutdown.filesystems.run_command", return_value=(0, "", "")) as mock_run:
        monitor._unmount_filesystems()
    cmd = mock_run.call_args.args[0]
    assert cmd == ["umount", "-l", "-f", "/mnt/data"]


@pytest.mark.unit
def test_unmount_filesystems_malformed_options_skip_mount(tmp_path, capsys):
    """A malformed options string (unclosed quote) used to crash the
    shutdown sequence with ValueError out of shlex.split. Cubic P1:
    catch and skip the offending mount instead of propagating."""
    monitor = _make_fs_monitor(
        tmp_path,
        mounts=[
            {"path": "/mnt/bad", "options": '"unclosed'},
            {"path": "/mnt/good", "options": "-l"},
        ],
    )
    monitor.logger = MagicMock()
    with patch("eneru.shutdown.filesystems.run_command", return_value=(0, "", "")) as mock_run:
        # Must not raise.
        monitor._unmount_filesystems()
    # Only the good mount reached run_command; the bad one was skipped.
    cmds = [c.args[0] for c in mock_run.call_args_list]
    assert ["umount", "-l", "/mnt/good"] in cmds
    assert all(c[1] != "/mnt/bad" for c in cmds), \
        "malformed-options mount must NOT reach umount"
    # And the failure was logged with the offending mount path.
    log_lines = [str(c) for c in monitor.logger.log.call_args_list]
    assert any("/mnt/bad" in line and "Invalid umount options" in line
               for line in log_lines)


@pytest.mark.unit
def test_unmount_filesystems_timeout_proceeds(tmp_path):
    """umount returning 124 (timeout) is logged but does not raise."""
    monitor = _make_fs_monitor(tmp_path)
    with patch("eneru.shutdown.filesystems.run_command", return_value=(124, "", "")):
        monitor._unmount_filesystems()  # Must not raise


@pytest.mark.unit
def test_unmount_filesystems_failure_checks_mountpoint(tmp_path):
    """On non-timeout failure, mountpoint is checked before logging an error."""
    monitor = _make_fs_monitor(tmp_path)

    def fake_run(cmd, **kwargs):
        if cmd[0] == "umount":
            return (1, "", "device busy")
        # mountpoint -q
        return (0, "", "")  # mounted, real failure

    with patch("eneru.shutdown.filesystems.run_command", side_effect=fake_run) as mock_run:
        monitor._unmount_filesystems()
    # umount + mountpoint check
    assert mock_run.call_count == 2


@pytest.mark.unit
def test_unmount_filesystems_failure_when_already_unmounted(tmp_path):
    """Non-zero umount + mountpoint failure means it was already unmounted."""
    monitor = _make_fs_monitor(tmp_path)

    def fake_run(cmd, **kwargs):
        if cmd[0] == "umount":
            return (1, "", "not mounted")
        return (1, "", "")  # mountpoint says not mounted

    with patch("eneru.shutdown.filesystems.run_command", side_effect=fake_run):
        monitor._unmount_filesystems()  # Must not raise


@pytest.mark.unit
def test_unmount_filesystems_multiple_mounts_independent(tmp_path):
    """Each configured mount is unmounted independently."""
    monitor = _make_fs_monitor(
        tmp_path,
        mounts=[
            {"path": "/mnt/a", "options": ""},
            {"path": "/mnt/b", "options": ""},
        ],
    )
    with patch("eneru.shutdown.filesystems.run_command", return_value=(0, "", "")) as mock_run:
        monitor._unmount_filesystems()
    targets = [c.args[0][-1] for c in mock_run.call_args_list]
    assert "/mnt/a" in targets
    assert "/mnt/b" in targets
