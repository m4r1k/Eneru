"""Tests for VMShutdownMixin (libvirt VM shutdown phase).

The mixin is exercised indirectly by integration tests, but the per-branch
behavior (no virsh, no running VMs, dry-run, graceful shutdown, force destroy
on timeout) is most reliably covered with focused unit tests against a
patched ``eneru.shutdown.vms.run_command`` / ``command_exists``.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

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
         patch("eneru.shutdown.vms.run_command", side_effect=call_outputs) as mock_run, \
         patch("eneru.shutdown.vms.time.sleep"):
        monitor._shutdown_vms()

    # Assert the actual virsh command sequence — without this, the test
    # passed even when the wrong command (or no command at all) was
    # issued during shutdown.
    issued = [c.args[0] for c in mock_run.call_args_list]
    assert issued[0] == ["virsh", "list", "--name", "--state-running"]
    # Per-VM shutdowns are issued in the order virsh list returned them.
    assert ["virsh", "shutdown", "vm1"] in issued
    assert ["virsh", "shutdown", "vm2"] in issued
    # No `destroy` should fire when graceful shutdown completes cleanly.
    assert not any(c[:2] == ["virsh", "destroy"] for c in issued)


@pytest.mark.unit
def test_shutdown_vms_logs_shutdown_stdout(tmp_path: Path) -> None:
    """virsh shutdown stdout is relayed to the operator log."""
    monitor = _make_vm_monitor(tmp_path, max_wait=10)
    log = []
    monitor._log_message = log.append
    call_outputs = [
        (0, "vm1\n", ""),              # initial list
        (0, "Domain vm1 is being shutdown\n", ""),
        (0, "", ""),                   # poll: stopped
    ]

    with patch("eneru.shutdown.vms.command_exists", return_value=True), \
         patch("eneru.shutdown.vms.run_command", side_effect=call_outputs), \
         patch("eneru.shutdown.vms.time.sleep"):
        monitor._shutdown_vms()

    assert any("Domain vm1 is being shutdown" in line for line in log)


@pytest.mark.unit
def test_shutdown_vms_retry_poll_failure_keeps_prior_remaining(
    tmp_path: Path,
) -> None:
    """A failed wait-loop poll must not make stuck VMs look stopped."""
    monitor = _make_vm_monitor(tmp_path, max_wait=1)
    log = []
    monitor._log_message = log.append

    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["virsh", "list", "--name"]:
            calls = getattr(fake_run, "calls", 0)
            fake_run.calls = calls + 1
            if calls == 0:
                return (0, "vm1\n", "")
            return (1, "", "libvirt down")
        return (0, "", "")

    with patch("eneru.shutdown.vms.command_exists", return_value=True), \
         patch("eneru.shutdown.vms.run_command", side_effect=fake_run) as mock_run, \
         patch("eneru.shutdown.vms.time.monotonic",
               side_effect=[0.0, 0.0, 0.0, 1.1, 1.1]), \
         patch("eneru.shutdown.vms.time.sleep"):
        monitor._shutdown_vms()

    assert any("keeping prior remaining VMs (1)" in line for line in log)
    destroy_calls = [
        c.args[0] for c in mock_run.call_args_list
        if c.args[0][:2] == ["virsh", "destroy"]
    ]
    assert destroy_calls == [["virsh", "destroy", "vm1"]]


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


@pytest.mark.unit
def test_shutdown_vms_reports_failed_rc(tmp_path):
    """ISS-040: failed virsh shutdown AND destroy both log ⚠️ with the rc."""
    monitor = _make_vm_monitor(tmp_path, max_wait=5)
    logs = []
    monitor._log_message = logs.append

    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["virsh", "list", "--name"]:
            return (0, "vm1\n", "")
        if cmd[:2] == ["virsh", "shutdown"]:
            return (1, "", "shutdown refused")
        if cmd[:2] == ["virsh", "destroy"]:
            return (1, "", "domain not found")
        return (0, "", "")

    with patch("eneru.shutdown.vms.command_exists", return_value=True), \
         patch("eneru.shutdown.vms.run_command", side_effect=fake_run), \
         patch("eneru.shutdown.vms.time.sleep"):
        monitor._shutdown_vms()

    assert any("virsh shutdown vm1 returned rc=1" in m for m in logs), logs
    assert any("virsh destroy vm1 returned rc=1" in m for m in logs), logs
    # ISS-040 follow-up: the phase summary must not claim ✅ when a
    # force-destroy failed -- that VM may still be running.
    assert not any("All VMs shutdown complete" in m for m in logs), logs
    assert any("1 VM(s) possibly still running" in m for m in logs), logs


@pytest.mark.unit
def test_deadline_just_after_stop_reples_and_skips_force_destroy(tmp_path):
    """F-025: the wait loop can exit on the deadline the instant AFTER the VMs
    stopped. A final re-poll must confirm they're down and skip force-destroy +
    the false 'possibly still running' warning."""
    monitor = _make_vm_monitor(tmp_path, max_wait=10)
    logs = []
    monitor._log_message = logs.append

    list_calls = {"n": 0}

    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["virsh", "list", "--name"]:
            list_calls["n"] += 1
            # call 1 (initial) + call 2 (in-loop poll): vm1 still running;
            # call 3 (final re-poll): vm1 has since stopped.
            return (0, "" if list_calls["n"] >= 3 else "vm1\n", "")
        if cmd[:2] == ["virsh", "shutdown"]:
            return (0, "", "")
        if cmd[:2] == ["virsh", "destroy"]:
            return (0, "", "")  # should never be reached
        return (0, "", "")

    # Drive exactly one loop iteration, then the deadline expires with the poll
    # still showing vm1 -> exercises the final re-poll path.
    monotonic_seq = iter([0, 1, 1, 1, 11, 11, 11])

    with patch("eneru.shutdown.vms.command_exists", return_value=True), \
         patch("eneru.shutdown.vms.run_command", side_effect=fake_run), \
         patch("eneru.shutdown.vms.time.sleep"), \
         patch("eneru.shutdown.vms.time.monotonic",
               side_effect=lambda: next(monotonic_seq)):
        monitor._shutdown_vms()

    assert any("confirmed on final poll" in m for m in logs), logs
    assert not any("Force destroying" in m for m in logs), logs
    assert not any("possibly still running" in m for m in logs), logs
    assert any("All VMs shutdown complete" in m for m in logs), logs


@pytest.mark.unit
def test_shutdown_vms_summary_success_when_destroy_succeeds(tmp_path):
    """A destroy that succeeds after a failed graceful shutdown still ends
    with the ✅ summary (graceful-path rc failures are covered by the
    destroy pass)."""
    monitor = _make_vm_monitor(tmp_path, max_wait=5)
    logs = []
    monitor._log_message = logs.append

    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["virsh", "list", "--name"]:
            return (0, "vm1\n", "")
        if cmd[:2] == ["virsh", "shutdown"]:
            return (1, "", "shutdown refused")
        if cmd[:2] == ["virsh", "destroy"]:
            return (0, "", "")
        return (0, "", "")

    with patch("eneru.shutdown.vms.command_exists", return_value=True), \
         patch("eneru.shutdown.vms.run_command", side_effect=fake_run), \
         patch("eneru.shutdown.vms.time.sleep"):
        monitor._shutdown_vms()

    assert any("All VMs shutdown complete" in m for m in logs), logs
    assert not any("possibly still running" in m for m in logs), logs
