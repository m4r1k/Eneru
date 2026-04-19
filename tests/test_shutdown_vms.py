"""Tests for VMShutdownMixin (libvirt VM shutdown phase).

The mixin is exercised indirectly by integration tests, but the per-branch
behavior (no virsh, no running VMs, dry-run, graceful shutdown, force destroy
on timeout) is most reliably covered with focused unit tests against a
patched ``eneru.shutdown.vms.run_command`` / ``command_exists``.
"""

import pytest
from unittest.mock import MagicMock, patch

from eneru import (
    Config,
    UPSGroupConfig,
    UPSConfig,
    VMConfig,
    BehaviorConfig,
    LoggingConfig,
    LocalShutdownConfig,
    UPSGroupMonitor,
    MonitorState,
)


def _make_vm_monitor(tmp_path, *, vm_enabled=True, max_wait=15, dry_run=False):
    config = Config(
        ups_groups=[UPSGroupConfig(
            ups=UPSConfig(name="UPS1@host"),
            virtual_machines=VMConfig(enabled=vm_enabled, max_wait=max_wait),
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
def test_shutdown_vms_disabled_no_op(tmp_path):
    """When config.virtual_machines.enabled is False, returns immediately."""
    monitor = _make_vm_monitor(tmp_path, vm_enabled=False)
    with patch("eneru.shutdown.vms.command_exists") as mock_exists, \
         patch("eneru.shutdown.vms.run_command") as mock_run:
        monitor._shutdown_vms()
    mock_exists.assert_not_called()
    mock_run.assert_not_called()


@pytest.mark.unit
def test_shutdown_vms_no_virsh_command(tmp_path):
    """Missing virsh exits early with an info log and no run_command calls."""
    monitor = _make_vm_monitor(tmp_path)
    with patch("eneru.shutdown.vms.command_exists", return_value=False), \
         patch("eneru.shutdown.vms.run_command") as mock_run:
        monitor._shutdown_vms()
    mock_run.assert_not_called()


@pytest.mark.unit
def test_shutdown_vms_list_failure(tmp_path):
    """A failed virsh list logs a warning and returns without further calls."""
    monitor = _make_vm_monitor(tmp_path)
    with patch("eneru.shutdown.vms.command_exists", return_value=True), \
         patch("eneru.shutdown.vms.run_command", return_value=(1, "", "err")) as mock_run:
        monitor._shutdown_vms()
    # Only the initial 'virsh list' is called; no shutdown attempts
    assert mock_run.call_count == 1


@pytest.mark.unit
def test_shutdown_vms_no_running_vms(tmp_path):
    """Empty running-VM list exits before any shutdown attempt."""
    monitor = _make_vm_monitor(tmp_path)
    with patch("eneru.shutdown.vms.command_exists", return_value=True), \
         patch("eneru.shutdown.vms.run_command", return_value=(0, "", "")) as mock_run:
        monitor._shutdown_vms()
    assert mock_run.call_count == 1  # Only the list call


@pytest.mark.unit
def test_shutdown_vms_dry_run_skips_real_calls(tmp_path):
    """Dry-run lists VMs but does not invoke virsh shutdown / destroy."""
    monitor = _make_vm_monitor(tmp_path, dry_run=True)
    with patch("eneru.shutdown.vms.command_exists", return_value=True), \
         patch("eneru.shutdown.vms.run_command", return_value=(0, "vm1\nvm2\n", "")) as mock_run:
        monitor._shutdown_vms()
    # Only the initial 'virsh list' runs in dry-run; shutdown/destroy are skipped.
    assert mock_run.call_count == 1


@pytest.mark.unit
def test_shutdown_vms_graceful_shutdown_completes_first_poll(tmp_path):
    """Graceful shutdown when all VMs stop before max_wait elapses."""
    monitor = _make_vm_monitor(tmp_path, max_wait=10)

    # First call: list two VMs. Per-VM shutdowns return (0,"",""). Then poll
    # returns no running VMs (loop exits immediately).
    call_outputs = [
        (0, "vm1\nvm2\n", ""),    # initial list
        (0, "", ""),                # virsh shutdown vm1
        (0, "", ""),                # virsh shutdown vm2
        (0, "", ""),                # poll: no VMs running
    ]
    with patch("eneru.shutdown.vms.command_exists", return_value=True), \
         patch("eneru.shutdown.vms.run_command", side_effect=call_outputs), \
         patch("eneru.shutdown.vms.time.sleep"):
        monitor._shutdown_vms()


@pytest.mark.unit
def test_shutdown_vms_force_destroys_after_timeout(tmp_path):
    """VMs that don't stop within max_wait are force-destroyed."""
    monitor = _make_vm_monitor(tmp_path, max_wait=5)

    # Sequence: list -> shutdown vm1 -> shutdown vm2 -> poll (still running)
    # repeats until timeout, then destroy vm1 + vm2.
    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["virsh", "list", "--name"]:
            return (0, "vm1\nvm2\n", "")
        return (0, "", "")

    with patch("eneru.shutdown.vms.command_exists", return_value=True), \
         patch("eneru.shutdown.vms.run_command", side_effect=fake_run) as mock_run, \
         patch("eneru.shutdown.vms.time.sleep"):
        monitor._shutdown_vms()

    # Confirm at least one virsh destroy was issued for each stuck VM
    destroy_calls = [c for c in mock_run.call_args_list if c.args[0][:2] == ["virsh", "destroy"]]
    destroyed = {c.args[0][2] for c in destroy_calls}
    assert destroyed == {"vm1", "vm2"}
