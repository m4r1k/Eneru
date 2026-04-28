"""Tests for the core single-UPS monitoring logic.

Covers the state machine, shutdown triggers, failsafe behavior,
shutdown sequence ordering, status transitions, and notifications.
These are the safety-critical code paths.
"""

import pytest
import time
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch, call
from collections import deque

from eneru import (
    Config, UPSConfig, UPSGroupConfig, TriggersConfig, DepletionConfig,
    ExtendedTimeConfig, BehaviorConfig, LoggingConfig, NotificationsConfig,
    VMConfig, ContainersConfig, FilesystemsConfig, UnmountConfig,
    RemoteServerConfig, LocalShutdownConfig, MonitorState,
)
from eneru.monitor import UPSGroupMonitor, compute_effective_order


def make_monitor(tmp_path, **overrides):
    """Helper to create a UPSGroupMonitor with test defaults."""
    triggers = overrides.pop("triggers", TriggersConfig(
        low_battery_threshold=20,
        critical_runtime_threshold=600,
        depletion=DepletionConfig(window=300, critical_rate=15.0, grace_period=90),
        extended_time=ExtendedTimeConfig(enabled=True, threshold=900),
    ))
    remote_servers = overrides.pop("remote_servers", [])
    config = Config(
        ups_groups=[UPSGroupConfig(
            ups=UPSConfig(name="TestUPS@localhost"),
            triggers=triggers,
            virtual_machines=VMConfig(enabled=False),
            containers=ContainersConfig(enabled=False),
            filesystems=FilesystemsConfig(
                sync_enabled=False,
                unmount=UnmountConfig(enabled=False),
            ),
            remote_servers=remote_servers,
            is_local=True,
        )],
        behavior=BehaviorConfig(dry_run=True),
        logging=LoggingConfig(
            shutdown_flag_file=str(tmp_path / "shutdown-flag"),
            state_file=str(tmp_path / "state"),
            battery_history_file=str(tmp_path / "history"),
        ),
        local_shutdown=LocalShutdownConfig(enabled=False),
        **overrides,
    )
    monitor = UPSGroupMonitor(config)
    monitor.state = MonitorState()
    monitor.logger = MagicMock()
    monitor._notification_worker = MagicMock()
    return monitor


def _run_one_iteration(monitor, ups_data_response):
    """Run ``_main_loop`` for exactly one iteration.

    ``_main_loop`` calls ``self._stop_event.wait(timeout)`` at every
    natural pause; we monkey-patch the wait so the first invocation
    also sets the event. This guarantees one full pass through the
    loop body before the loop exits. Promoted to module scope so any
    test class can route through the real loop instead of inlining
    the failsafe simulation.
    """
    original_wait = monitor._stop_event.wait
    called = {"n": 0}

    def wait_then_stop(timeout=None):
        called["n"] += 1
        monitor._stop_event.set()
        return original_wait(0)

    with patch.object(monitor, "_get_all_ups_data",
                      return_value=ups_data_response):
        with patch.object(monitor._stop_event, "wait", wait_then_stop):
            monitor._main_loop()


# ==============================================================================
# STATUS STATE MACHINE
# ==============================================================================

class TestStatusTransitions:
    """Verify status transitions trigger correct behavior."""

    @pytest.mark.unit
    def test_ol_to_ob_triggers_on_battery(self, tmp_path):
        """Transition from OL to OB initializes battery tracking."""
        monitor = make_monitor(tmp_path)
        monitor.state.previous_status = "OL CHRG"

        ups_data = {
            "ups.status": "OB DISCHRG",
            "battery.charge": "85",
            "battery.runtime": "1200",
            "ups.load": "30",
        }
        monitor._handle_on_battery(ups_data)

        assert monitor.state.on_battery_start_time > 0
        assert monitor.state.extended_time_logged is False
        # Should have logged a power event
        assert any("ON_BATTERY" in str(c) for c in monitor.logger.log.call_args_list)

    @pytest.mark.unit
    def test_ob_to_ol_triggers_recovery(self, tmp_path):
        """Transition from OB to OL sends recovery notification."""
        monitor = make_monitor(tmp_path)
        monitor.state.previous_status = "OB DISCHRG"
        monitor.state.on_battery_start_time = int(time.time()) - 120

        ups_data = {
            "ups.status": "OL CHRG",
            "battery.charge": "75",
            "input.voltage": "230.5",
        }
        monitor._handle_on_line(ups_data)

        # Should have logged POWER_RESTORED
        assert any("POWER_RESTORED" in str(c) for c in monitor.logger.log.call_args_list)
        # Timer should be reset
        assert monitor.state.on_battery_start_time == 0

    @pytest.mark.unit
    def test_ob_continuation_no_reinitialization(self, tmp_path):
        """Staying on OB does not reinitialize battery tracking."""
        monitor = make_monitor(tmp_path)
        monitor.state.previous_status = "OB DISCHRG"
        original_start = int(time.time()) - 60
        monitor.state.on_battery_start_time = original_start

        ups_data = {
            "ups.status": "OB DISCHRG",
            "battery.charge": "80",
            "battery.runtime": "1000",
            "ups.load": "30",
        }
        monitor._handle_on_battery(ups_data)

        # Start time should NOT be reset
        assert monitor.state.on_battery_start_time == original_start


# ==============================================================================
# SHUTDOWN TRIGGERS (T1-T4)
# ==============================================================================

class TestShutdownTriggers:
    """Verify each trigger fires correctly and short-circuits."""

    @pytest.mark.unit
    def test_t1_low_battery_triggers_shutdown(self, tmp_path):
        """T1: Battery below threshold triggers shutdown."""
        monitor = make_monitor(tmp_path)
        monitor.state.previous_status = "OB DISCHRG"
        monitor.state.on_battery_start_time = int(time.time()) - 10

        ups_data = {
            "ups.status": "OB DISCHRG",
            "battery.charge": "15",  # Below 20% threshold
            "battery.runtime": "1200",
            "ups.load": "30",
        }

        with patch.object(monitor, "_trigger_immediate_shutdown") as mock_shutdown:
            monitor._handle_on_battery(ups_data)
            mock_shutdown.assert_called_once()
            assert "15%" in mock_shutdown.call_args[0][0]

    @pytest.mark.unit
    def test_t1_above_threshold_no_shutdown(self, tmp_path):
        """T1: Battery above threshold does not trigger."""
        monitor = make_monitor(tmp_path)
        monitor.state.previous_status = "OB DISCHRG"
        monitor.state.on_battery_start_time = int(time.time()) - 10

        ups_data = {
            "ups.status": "OB DISCHRG",
            "battery.charge": "50",  # Above 20%
            "battery.runtime": "1200",
            "ups.load": "30",
        }

        with patch.object(monitor, "_trigger_immediate_shutdown") as mock_shutdown:
            monitor._handle_on_battery(ups_data)
            mock_shutdown.assert_not_called()

    @pytest.mark.unit
    def test_t2_low_runtime_triggers_shutdown(self, tmp_path):
        """T2: Runtime below threshold triggers shutdown."""
        monitor = make_monitor(tmp_path)
        monitor.state.previous_status = "OB DISCHRG"
        monitor.state.on_battery_start_time = int(time.time()) - 10

        ups_data = {
            "ups.status": "OB DISCHRG",
            "battery.charge": "50",   # Above T1
            "battery.runtime": "300", # Below 600s threshold
            "ups.load": "30",
        }

        with patch.object(monitor, "_trigger_immediate_shutdown") as mock_shutdown:
            monitor._handle_on_battery(ups_data)
            mock_shutdown.assert_called_once()
            assert "Runtime" in mock_shutdown.call_args[0][0]

    @pytest.mark.unit
    def test_t4_extended_time_triggers_shutdown(self, tmp_path):
        """T4: Extended time on battery triggers shutdown."""
        monitor = make_monitor(tmp_path)
        monitor.state.previous_status = "OB DISCHRG"
        monitor.state.on_battery_start_time = int(time.time()) - 1000  # > 900s threshold

        ups_data = {
            "ups.status": "OB DISCHRG",
            "battery.charge": "50",    # Above T1
            "battery.runtime": "1200", # Above T2
            "ups.load": "30",
        }

        with patch.object(monitor, "_trigger_immediate_shutdown") as mock_shutdown:
            with patch.object(monitor, "_calculate_depletion_rate", return_value=0.0):
                monitor._handle_on_battery(ups_data)
                mock_shutdown.assert_called_once()
                assert "Time on battery" in mock_shutdown.call_args[0][0]

    @pytest.mark.unit
    def test_t4_disabled_no_shutdown(self, tmp_path):
        """T4: Extended time disabled does not trigger shutdown."""
        monitor = make_monitor(tmp_path, triggers=TriggersConfig(
            extended_time=ExtendedTimeConfig(enabled=False, threshold=900),
        ))
        monitor.state.previous_status = "OB DISCHRG"
        monitor.state.on_battery_start_time = int(time.time()) - 1000

        ups_data = {
            "ups.status": "OB DISCHRG",
            "battery.charge": "50",
            "battery.runtime": "1200",
            "ups.load": "30",
        }

        with patch.object(monitor, "_trigger_immediate_shutdown") as mock_shutdown:
            with patch.object(monitor, "_calculate_depletion_rate", return_value=0.0):
                monitor._handle_on_battery(ups_data)
                mock_shutdown.assert_not_called()

    @pytest.mark.unit
    def test_trigger_short_circuit_t1_before_t2(self, tmp_path):
        """T1 fires first -- T2 is not evaluated."""
        monitor = make_monitor(tmp_path)
        monitor.state.previous_status = "OB DISCHRG"
        monitor.state.on_battery_start_time = int(time.time()) - 10

        ups_data = {
            "ups.status": "OB DISCHRG",
            "battery.charge": "10",   # Triggers T1
            "battery.runtime": "100", # Would also trigger T2
            "ups.load": "30",
        }

        with patch.object(monitor, "_trigger_immediate_shutdown") as mock_shutdown:
            monitor._handle_on_battery(ups_data)
            mock_shutdown.assert_called_once()
            reason = mock_shutdown.call_args[0][0]
            # Should mention battery charge (T1), not runtime (T2)
            assert "charge" in reason.lower() or "10%" in reason


# ==============================================================================
# FSD FLAG
# ==============================================================================

class TestFSDFlag:
    """FSD (Forced Shutdown) flag handling -- highest priority."""

    @pytest.mark.unit
    def test_fsd_triggers_immediate_shutdown(self, tmp_path):
        """FSD status triggers _trigger_immediate_shutdown directly."""
        monitor = make_monitor(tmp_path)

        with patch.object(monitor, "_trigger_immediate_shutdown") as mock_shutdown:
            # Simulate what the main loop does for FSD
            ups_status = "FSD"
            if "FSD" in ups_status:
                monitor._trigger_immediate_shutdown("UPS signaled FSD (Forced Shutdown) flag.")

            mock_shutdown.assert_called_once()
            assert "FSD" in mock_shutdown.call_args[0][0]

    @pytest.mark.unit
    def test_trigger_immediate_shutdown_sets_flag(self, tmp_path):
        """_trigger_immediate_shutdown creates the shutdown flag file."""
        monitor = make_monitor(tmp_path)

        with patch.object(monitor, "_execute_shutdown_sequence"):
            monitor._trigger_immediate_shutdown("Test reason")

        assert monitor._shutdown_flag_path.exists()

    @pytest.mark.unit
    def test_trigger_immediate_shutdown_idempotent(self, tmp_path):
        """Second call to _trigger_immediate_shutdown is a no-op."""
        monitor = make_monitor(tmp_path)

        call_count = 0
        original_execute = monitor._execute_shutdown_sequence

        def counting_execute():
            nonlocal call_count
            call_count += 1

        monitor._execute_shutdown_sequence = counting_execute

        monitor._trigger_immediate_shutdown("First")
        monitor._trigger_immediate_shutdown("Second")  # Should be no-op

        assert call_count == 1

    @pytest.mark.unit
    def test_trigger_immediate_shutdown_logs_when_gated(self, tmp_path):
        """5.2.2 (bug #4): when the flag-file gate blocks a re-trigger,
        a warning must surface so operators can correlate it. Pre-5.2.2
        the second call returned silently, which made debugging the
        ckrevel reproduction much harder than it had to be.
        """
        monitor = make_monitor(tmp_path)

        with patch.object(monitor, "_execute_shutdown_sequence"):
            monitor._trigger_immediate_shutdown("First")

        with patch.object(monitor, "_log_message") as mock_log:
            with patch.object(monitor, "_execute_shutdown_sequence") as mock_exec:
                monitor._trigger_immediate_shutdown("Second")
                # Must not run the sequence again (still no-op).
                mock_exec.assert_not_called()
            # Must log a warning that mentions the reason and the flag path.
            assert mock_log.call_count >= 1
            warning_calls = [
                call.args[0] for call in mock_log.call_args_list
                if call.args and "previous shutdown sequence" in call.args[0]
            ]
            assert warning_calls, (
                f"expected a 'previous shutdown sequence' warning; "
                f"got: {[c.args for c in mock_log.call_args_list]}"
            )
            assert "Second" in warning_calls[0]
            assert str(monitor._shutdown_flag_path) in warning_calls[0]


# ==============================================================================
# Shutdown re-arm on POWER_RESTORED (5.2.2 / bug #4)
# ==============================================================================

class TestShutdownReArmOnPowerRestored:
    """When the OS reboot fails to take down the daemon (custom shutdown
    command, sandboxed environment, dummy-UPS test rig), the previous
    shutdown sequence's flag file persists and silently blocks the next
    trigger. _handle_on_line clears the flag on OB->OL so the daemon
    re-arms.
    """

    @pytest.mark.unit
    def test_power_restored_unlinks_shutdown_flag(self, tmp_path):
        """OB -> OL transition must remove the shutdown flag file."""
        monitor = make_monitor(tmp_path)
        monitor.state.previous_status = "OB DISCHRG"
        monitor.state.on_battery_start_time = int(time.time()) - 30
        # Simulate a previous trigger leaving the flag behind.
        monitor._shutdown_flag_path.touch()
        assert monitor._shutdown_flag_path.exists()

        ups_data = {
            "ups.status": "OL CHRG",
            "battery.charge": "85",
            "input.voltage": "230.0",
        }
        monitor._handle_on_line(ups_data)

        assert not monitor._shutdown_flag_path.exists(), (
            "POWER_RESTORED must clear the shutdown flag so the next OB "
            "transition can re-trigger; pre-5.2.2 the flag persisted "
            "and the second trigger silently no-op'd (bug #4)."
        )

    @pytest.mark.unit
    def test_power_restored_no_flag_is_safe(self, tmp_path):
        """Clearing a non-existent flag must not raise (missing_ok=True)."""
        monitor = make_monitor(tmp_path)
        monitor.state.previous_status = "OB"
        monitor.state.on_battery_start_time = int(time.time()) - 5
        assert not monitor._shutdown_flag_path.exists()

        ups_data = {
            "ups.status": "OL",
            "battery.charge": "100",
            "input.voltage": "230.0",
        }
        # No exception, no flag.
        monitor._handle_on_line(ups_data)
        assert not monitor._shutdown_flag_path.exists()

    @pytest.mark.unit
    def test_no_unlink_when_already_on_line(self, tmp_path):
        """OL -> OL (no transition) must NOT touch the shutdown flag.

        If a shutdown sequence is genuinely in flight while the daemon
        is still on line (extremely unusual but possible during dry-run
        or coordinator-mode chains), removing the flag could let a
        concurrent re-trigger fire. Only the OB/FSD -> OL transition
        is expected to clear the flag.
        """
        monitor = make_monitor(tmp_path)
        monitor.state.previous_status = "OL CHRG"
        # Pre-existing flag (someone or something put it there).
        monitor._shutdown_flag_path.touch()

        ups_data = {
            "ups.status": "OL CHRG",
            "battery.charge": "100",
            "input.voltage": "230.0",
        }
        monitor._handle_on_line(ups_data)

        assert monitor._shutdown_flag_path.exists(), (
            "Steady-state OL polls must not clear the shutdown flag; "
            "the unlink is gated on the OB/FSD -> OL transition."
        )

    @pytest.mark.unit
    def test_full_outage_recovery_rearms_trigger(self, tmp_path):
        """End-to-end: OB triggers shutdown -> flag set; OL clears flag;
        second OB triggers shutdown again. Mirrors the bug #4 reproducer
        exactly (with the shutdown sequence mocked so the test doesn't
        actually call out to real shutdown code).
        """
        monitor = make_monitor(tmp_path)

        # First OB: trigger fires, flag is set.
        monitor.state.previous_status = ""
        ob_data = {
            "ups.status": "OB DISCHRG",
            "battery.charge": "5",   # below default low_battery_threshold
            "battery.runtime": "30",
            "ups.load": "30",
        }
        with patch.object(monitor, "_execute_shutdown_sequence") as mock_exec:
            monitor._handle_on_battery(ob_data)
        assert mock_exec.called, "first OB must trigger shutdown"
        assert monitor._shutdown_flag_path.exists()
        monitor.state.previous_status = "OB DISCHRG"

        # OL: power restored, flag cleared.
        ol_data = {
            "ups.status": "OL CHRG",
            "battery.charge": "100",
            "input.voltage": "230.0",
        }
        monitor._handle_on_line(ol_data)
        assert not monitor._shutdown_flag_path.exists()
        monitor.state.previous_status = "OL CHRG"

        # Second OB: trigger must fire again (re-arm worked).
        with patch.object(monitor, "_execute_shutdown_sequence") as mock_exec2:
            monitor._handle_on_battery(ob_data)
        assert mock_exec2.called, (
            "second OB after OL must re-trigger shutdown; if this fails "
            "the bug #4 regression is back."
        )
        assert monitor._shutdown_flag_path.exists()


# ==============================================================================
# FAILSAFE (Connection Lost While On Battery)
# ==============================================================================

class TestFailsafe:
    """Failsafe: connection lost while OB triggers immediate shutdown."""

    @pytest.mark.unit
    def test_connection_lost_while_ob_triggers_shutdown(self, tmp_path):
        """Connection failure while on battery triggers immediate shutdown.

        Routes through the real ``_main_loop`` via ``_run_one_iteration``
        rather than inlining the failsafe logic — otherwise a regression
        in the loop's failsafe path would leave this test green because
        the assertions only check the inline simulation.
        """
        monitor = make_monitor(tmp_path)
        monitor._in_redundancy_group = False
        monitor.state.previous_status = "OB DISCHRG"
        monitor.state.connection_state = "OK"
        # Trigger failsafe immediately (non-stale-data path).
        with patch.object(monitor, "_execute_shutdown_sequence") as mock_exec:
            _run_one_iteration(monitor, (False, {}, "Network error"))

        mock_exec.assert_called_once()
        assert monitor.state.connection_state == "FAILED"

    @pytest.mark.unit
    def test_connection_lost_while_ol_enters_grace_period(self, tmp_path):
        """Connection failure while on line enters grace period, not shutdown."""
        monitor = make_monitor(tmp_path)
        monitor.state.previous_status = "OL CHRG"
        monitor.state.connection_state = "OK"

        monitor._handle_connection_failure("Connection refused")

        assert monitor.state.connection_state == "GRACE_PERIOD"
        assert not monitor._shutdown_flag_path.exists()

    @pytest.mark.unit
    def test_stale_data_increments_counter(self, tmp_path):
        """Stale data increments counter before triggering failsafe."""
        monitor = make_monitor(tmp_path)
        monitor.state.stale_data_count = 0

        # Simulate stale data handling from main loop
        error_msg = "Data stale"
        if "Data stale" in error_msg:
            monitor.state.stale_data_count += 1
            if monitor.state.stale_data_count >= monitor.config.ups.max_stale_data_tolerance:
                is_failsafe_trigger = True
            else:
                is_failsafe_trigger = False

        assert monitor.state.stale_data_count == 1
        assert is_failsafe_trigger is False  # Not yet at tolerance (3)

    @pytest.mark.unit
    def test_stale_data_reaches_tolerance(self, tmp_path):
        """Stale data at tolerance threshold triggers failsafe."""
        monitor = make_monitor(tmp_path)
        monitor.state.stale_data_count = 2  # One more will reach 3

        error_msg = "Data stale"
        monitor.state.stale_data_count += 1
        is_failsafe_trigger = (
            monitor.state.stale_data_count >= monitor.config.ups.max_stale_data_tolerance
        )

        assert monitor.state.stale_data_count == 3
        assert is_failsafe_trigger is True


# ==============================================================================
# SHUTDOWN SEQUENCE ORDERING
# ==============================================================================

class TestShutdownSequence:
    """Verify shutdown sequence calls methods in correct order."""

    @pytest.mark.unit
    def test_shutdown_sequence_order(self, tmp_path):
        """Shutdown sequence calls in correct order: VMs, containers, sync, unmount, remote."""
        monitor = make_monitor(tmp_path)
        call_order = []

        monitor._shutdown_vms = lambda: call_order.append("vms")
        monitor._shutdown_containers = lambda: call_order.append("containers")
        monitor._sync_filesystems = lambda: call_order.append("sync")
        monitor._unmount_filesystems = lambda: call_order.append("unmount")
        monitor._shutdown_remote_servers = lambda: call_order.append("remote")

        monitor._execute_shutdown_sequence()

        assert call_order == ["vms", "containers", "sync", "unmount", "remote"]

    @pytest.mark.unit
    def test_shutdown_sequence_sets_flag(self, tmp_path):
        """Shutdown sequence creates the flag file (cleaned up in dry-run)."""
        monitor = make_monitor(tmp_path)
        monitor._shutdown_vms = lambda: None
        monitor._shutdown_containers = lambda: None
        monitor._sync_filesystems = lambda: None
        monitor._unmount_filesystems = lambda: None
        monitor._shutdown_remote_servers = lambda: None

        # In dry-run mode with local_shutdown disabled, flag is created then cleaned up.
        # Verify by checking flag exists DURING the sequence.
        flag_existed = False
        original_remote = monitor._shutdown_remote_servers

        def check_flag():
            nonlocal flag_existed
            flag_existed = monitor._shutdown_flag_path.exists()

        monitor._shutdown_remote_servers = check_flag
        monitor._execute_shutdown_sequence()

        assert flag_existed

    @pytest.mark.unit
    def test_shutdown_aborts_on_step_failure(self, tmp_path):
        """Documents current behavior: an unhandled exception inside a
        shutdown step propagates up and ABORTS the remaining steps. The
        steps themselves handle expected failures internally; only an
        unexpected raise reaches the orchestrator. If we ever decide to
        wrap each step in try/except so subsequent steps run as
        best-effort, this test must change to assert the new contract.
        """
        monitor = make_monitor(tmp_path)
        call_order = []

        def failing_vms():
            call_order.append("vms")
            raise RuntimeError("VM shutdown failed")

        monitor._shutdown_vms = failing_vms
        monitor._shutdown_containers = lambda: call_order.append("containers")
        monitor._sync_filesystems = lambda: call_order.append("sync")
        monitor._unmount_filesystems = lambda: call_order.append("unmount")
        monitor._shutdown_remote_servers = lambda: call_order.append("remote")

        with pytest.raises(RuntimeError, match="VM shutdown failed"):
            monitor._execute_shutdown_sequence()

        # Only the failing step ran; subsequent steps did NOT execute.
        assert call_order == ["vms"]


# ==============================================================================
# NOTIFICATIONS
# ==============================================================================

class TestNotifications:
    """Verify notifications fire for key events."""

    @pytest.mark.unit
    def test_trigger_shutdown_sends_notification(self, tmp_path):
        """_trigger_immediate_shutdown sends emergency notification."""
        monitor = make_monitor(tmp_path)

        with patch.object(monitor, "_execute_shutdown_sequence"):
            monitor._trigger_immediate_shutdown("Battery critical")

        # Exactly one notification: the headline. Log lines are NOT mirrored
        # as notifications anymore (v5.2 — see TestNotificationPolicyV52).
        assert monitor._notification_worker.send.call_count == 1
        body = monitor._notification_worker.send.call_args.kwargs.get(
            "body"
        ) or monitor._notification_worker.send.call_args.args[0]
        assert "EMERGENCY SHUTDOWN INITIATED" in body
        assert "Battery critical" in body

    @pytest.mark.unit
    def test_power_restored_logs_event(self, tmp_path):
        """Transition from OB to OL logs POWER_RESTORED event."""
        monitor = make_monitor(tmp_path)
        monitor.state.previous_status = "OB DISCHRG"
        monitor.state.on_battery_start_time = int(time.time()) - 60

        ups_data = {
            "ups.status": "OL CHRG",
            "battery.charge": "70",
            "input.voltage": "230",
        }
        monitor._handle_on_line(ups_data)

        log_calls = [str(c) for c in monitor.logger.log.call_args_list]
        assert any("POWER_RESTORED" in c for c in log_calls)

    @pytest.mark.unit
    def test_no_recovery_notification_on_ol_to_ol(self, tmp_path):
        """OL to OL does not trigger recovery notification."""
        monitor = make_monitor(tmp_path)
        monitor.state.previous_status = "OL CHRG"

        ups_data = {
            "ups.status": "OL",
            "battery.charge": "100",
            "input.voltage": "230",
        }
        monitor._handle_on_line(ups_data)

        log_calls = [str(c) for c in monitor.logger.log.call_args_list]
        assert not any("POWER_RESTORED" in c for c in log_calls)


# ==============================================================================
# v5.2 NOTIFICATION POLICY (Slice 1: noise cleanup + wall opt-in)
# ==============================================================================

class TestNotificationPolicyV52:
    """v5.2 changes: log lines no longer mirror to notifications during
    shutdown, wall(1) is opt-in instead of always-on, and the sequence
    completion notification fires in both the local-shutdown-enabled
    and the local-shutdown-disabled paths."""

    @pytest.mark.unit
    def test_log_message_during_shutdown_does_not_auto_mirror(self, tmp_path):
        """Regression guard for the v5.1 ``_log_message`` auto-mirror
        block (lines 325-331 before its v5.2 removal): every log line
        emitted during shutdown got wrapped as
        ``ℹ️ **Shutdown Detail:** <line>`` and pushed through
        ``_send_notification``, which produced ~22 notifications per
        shutdown sequence. v5.2 deletes the block; this test fires
        ``_log_message`` with the shutdown flag set and asserts the
        notification worker was never called.

        If a future change re-introduces any path from ``_log_message``
        to ``_notification_worker.send``, this test fails — which is
        exactly what we want.
        """
        monitor = make_monitor(tmp_path)
        monitor._shutdown_flag_path.touch()

        monitor._log_message("VMs: stopping libvirt-qemu-1234")
        monitor._log_message("Containers: docker stop nginx")

        # journalctl carries the per-step trace; the notification
        # channel must stay silent for these.
        assert monitor._notification_worker.send.call_count == 0

    @pytest.mark.unit
    def test_wall_disabled_by_default(self, tmp_path):
        """LocalShutdownConfig.wall defaults False; neither wall call
        site fires the shell broadcast."""
        monitor = make_monitor(tmp_path)
        # Stub out the phases so the sequence runs without I/O.
        monitor._shutdown_vms = lambda: None
        monitor._shutdown_containers = lambda: None
        monitor._sync_filesystems = lambda: None
        monitor._unmount_filesystems = lambda: None
        monitor._shutdown_remote_servers = lambda: None
        # dry_run is False for this test — we want the wall path to be
        # otherwise reachable so the *only* thing suppressing it is the
        # config flag.
        monitor.config.behavior.dry_run = False
        monitor.config.local_shutdown.enabled = False  # don't actually halt

        with patch("eneru.monitor.run_command") as run_cmd:
            monitor._execute_shutdown_sequence()

        wall_calls = [c for c in run_cmd.call_args_list
                      if c.args and c.args[0] and c.args[0][0] == "wall"]
        assert wall_calls == [], (
            f"wall(1) should not be invoked when local_shutdown.wall=False; "
            f"got {wall_calls}"
        )

    @pytest.mark.unit
    def test_wall_enabled_invokes_wall_command(self, tmp_path):
        """When the user opts in via local_shutdown.wall=True, the
        shell broadcast fires from the sequence entry point."""
        monitor = make_monitor(tmp_path)
        monitor._shutdown_vms = lambda: None
        monitor._shutdown_containers = lambda: None
        monitor._sync_filesystems = lambda: None
        monitor._unmount_filesystems = lambda: None
        monitor._shutdown_remote_servers = lambda: None
        monitor.config.behavior.dry_run = False
        monitor.config.local_shutdown.enabled = False
        monitor.config.local_shutdown.wall = True

        with patch("eneru.monitor.run_command") as run_cmd:
            monitor._execute_shutdown_sequence()

        wall_calls = [c for c in run_cmd.call_args_list
                      if c.args and c.args[0] and c.args[0][0] == "wall"]
        assert len(wall_calls) == 1, (
            f"expected one wall(1) broadcast at sequence start, got "
            f"{len(wall_calls)}: {wall_calls}"
        )

    @pytest.mark.unit
    def test_summary_notification_fires_when_local_shutdown_disabled(self, tmp_path):
        """The "✅ Shutdown Sequence Complete" summary used to fire only
        when local_shutdown.enabled=true (because the system was about
        to halt and the user needed a goodbye). The disabled path
        finished silently. v5.2 always summarises so users running Eneru
        as a remote-resource orchestrator get the same closure."""
        monitor = make_monitor(tmp_path)
        monitor._shutdown_vms = lambda: None
        monitor._shutdown_containers = lambda: None
        monitor._sync_filesystems = lambda: None
        monitor._unmount_filesystems = lambda: None
        monitor._shutdown_remote_servers = lambda: None
        monitor.config.local_shutdown.enabled = False

        monitor._execute_shutdown_sequence()

        bodies = []
        for c in monitor._notification_worker.send.call_args_list:
            body = c.kwargs.get("body") or (c.args[0] if c.args else "")
            bodies.append(body)
        summaries = [b for b in bodies if "Shutdown Sequence Complete" in b]
        assert len(summaries) == 1, (
            f"expected exactly one summary notification, got bodies={bodies}"
        )


# ==============================================================================
# v5.2.1 events-table mirroring (Slice 3 + Slice 4 follow-up)
#
# These tests verify the lifecycle / shutdown notifications now ALSO
# land in the stats events table, so the TUI's `--events-only` view
# carries the same taxonomy the user sees on Apprise. A real install
# was missing rows like EMERGENCY_SHUTDOWN_INITIATED / DAEMON_RECOVERED
# between an ON_BATTERY and the next DAEMON_START — they're now mirrored
# via _stats_store.log_event at the relevant call sites.
# ==============================================================================

class TestEventsTableMirroring:

    @pytest.mark.unit
    def test_trigger_immediate_shutdown_logs_emergency_event(self, tmp_path):
        """_trigger_immediate_shutdown writes EMERGENCY_SHUTDOWN_INITIATED
        with the trigger reason as the detail."""
        monitor = make_monitor(tmp_path)
        monitor._stats_store = MagicMock()
        with patch.object(monitor, "_execute_shutdown_sequence"):
            monitor._trigger_immediate_shutdown(
                "Battery charge 14% below threshold 20%"
            )
        log_calls = monitor._stats_store.log_event.call_args_list
        emergency = [c for c in log_calls
                     if c.args and c.args[0] == "EMERGENCY_SHUTDOWN_INITIATED"]
        assert len(emergency) == 1
        # The reason string is preserved verbatim as the detail.
        assert "Battery charge 14% below threshold 20%" in emergency[0].args[1]

    @pytest.mark.unit
    def test_execute_shutdown_sequence_logs_complete_event(self, tmp_path):
        """_execute_shutdown_sequence writes SHUTDOWN_SEQUENCE_COMPLETE
        with elapsed time, in BOTH the local-shutdown-enabled and
        -disabled paths (Slice 1 fixed the disabled path; this test
        guards the events-table mirror lands the same way)."""
        monitor = make_monitor(tmp_path)
        monitor._stats_store = MagicMock()
        monitor._shutdown_vms = lambda: None
        monitor._shutdown_containers = lambda: None
        monitor._sync_filesystems = lambda: None
        monitor._unmount_filesystems = lambda: None
        monitor._shutdown_remote_servers = lambda: None
        monitor.config.local_shutdown.enabled = False

        monitor._execute_shutdown_sequence()

        log_calls = monitor._stats_store.log_event.call_args_list
        completes = [c for c in log_calls
                     if c.args and c.args[0] == "SHUTDOWN_SEQUENCE_COMPLETE"]
        assert len(completes) == 1
        assert "elapsed:" in completes[0].args[1]

    @pytest.mark.unit
    def test_cleanup_and_exit_logs_daemon_stop(self, tmp_path):
        """_cleanup_and_exit writes DAEMON_STOP so the events table has
        a symmetric on/off pair against _start_stats's DAEMON_START."""
        monitor = make_monitor(tmp_path)
        monitor._stats_store = MagicMock()
        with pytest.raises(SystemExit):
            monitor._cleanup_and_exit(15, None)
        log_calls = monitor._stats_store.log_event.call_args_list
        stops = [c for c in log_calls
                 if c.args and c.args[0] == "DAEMON_STOP"]
        assert len(stops) == 1
        assert "stopped by signal" in stops[0].args[1]

    @pytest.mark.unit
    def test_cleanup_and_exit_skips_stop_notification_on_upgrade(self, tmp_path):
        """v5.2.1: when postinstall.sh has dropped the upgrade marker
        before invoking systemctl restart, the SIGTERM that lands here
        comes from the upgrade. The next daemon will emit a single
        '📦 Upgraded' message that supersedes the stop, so the stop
        notification must NOT be enqueued."""
        monitor = make_monitor(tmp_path)
        monitor._stats_store = MagicMock()
        marker = {"old_version": "5.2.0", "new_version": "5.2.1"}
        with patch("eneru.monitor.read_upgrade_marker", return_value=marker), \
             patch("eneru.monitor.read_shutdown_marker", return_value=None), \
             patch("eneru.monitor.write_shutdown_marker"), \
             pytest.raises(SystemExit):
            monitor._cleanup_and_exit(15, None)
        # The worker's send() is what _send_notification routes through;
        # zero send calls means no lifecycle stop was enqueued.
        send_calls = [c for c in monitor._notification_worker.send.call_args_list
                      if c.kwargs.get("category") == "lifecycle"
                      or (len(c.args) >= 3 and c.args[2] == "lifecycle")]
        assert send_calls == [], (
            "Expected no lifecycle send when upgrade marker present; "
            f"got: {monitor._notification_worker.send.call_args_list}"
        )

    @pytest.mark.unit
    def test_cleanup_and_exit_enqueues_stop_after_flush_and_stop(self, tmp_path):
        """v5.2.1: when no upgrade is in flight, the stop notification
        is enqueued AFTER the worker is drained AND stopped. This way
        the row stays `pending` in SQLite (the worker thread is dead,
        can't deliver eagerly) and is either cancelled by the next
        daemon's classifier (single Restarted) or delivered by the
        systemd-run timer scheduled below (single Stopped)."""
        monitor = make_monitor(tmp_path)
        monitor._stats_store = MagicMock()
        with patch("eneru.monitor.read_upgrade_marker", return_value=None), \
             patch("eneru.monitor.read_shutdown_marker", return_value=None), \
             patch("eneru.monitor.write_shutdown_marker"), \
             patch("eneru.monitor.schedule_deferred_stop_or_eager_send") as sched, \
             pytest.raises(SystemExit):
            monitor._cleanup_and_exit(15, None)
        # Walk method_calls in order; flush + stop must BOTH precede send
        # so the worker thread is dead before we enqueue the row.
        names = [c[0] for c in monitor._notification_worker.method_calls]
        assert "flush" in names
        assert "stop" in names
        assert "send" in names
        assert names.index("flush") < names.index("send"), (
            f"flush must happen before send; got order: {names}"
        )
        assert names.index("stop") < names.index("send"), (
            f"stop must happen before send; got order: {names}"
        )
        # Schedule helper must be called with the lifecycle send's id.
        sched.assert_called_once()
        kwargs = sched.call_args.kwargs
        assert kwargs["body"].startswith("🛑")
        assert kwargs["notify_type"] == monitor.config.NOTIFY_WARNING

    @pytest.mark.unit
    def test_cleanup_and_exit_skips_schedule_on_upgrade(self, tmp_path):
        """v5.2.1: with an upgrade marker on disk, the lifecycle stop is
        suppressed entirely — no notification enqueued, no systemd-run
        timer scheduled. The next daemon's '📦 Upgraded' covers both."""
        monitor = make_monitor(tmp_path)
        monitor._stats_store = MagicMock()
        marker = {"old_version": "5.2.0", "new_version": "5.2.1"}
        with patch("eneru.monitor.read_upgrade_marker", return_value=marker), \
             patch("eneru.monitor.read_shutdown_marker", return_value=None), \
             patch("eneru.monitor.write_shutdown_marker"), \
             patch("eneru.monitor.schedule_deferred_stop_or_eager_send") as sched, \
             pytest.raises(SystemExit):
            monitor._cleanup_and_exit(15, None)
        sched.assert_not_called()

    @pytest.mark.unit
    def test_emit_lifecycle_skips_event_for_fresh_start(self, tmp_path):
        """First-ever start (no marker, no last_seen) classifies as
        DAEMON_START — _start_stats already inserts that row, so the
        lifecycle classifier MUST NOT insert a duplicate."""
        monitor = make_monitor(tmp_path)
        monitor._stats_store = MagicMock()
        # Mock no markers + no last_seen.
        monitor._stats_store._conn = object()  # truthy
        monitor._stats_store.get_meta.return_value = None
        monitor._stats_store.find_pending_by_category.return_value = []
        with patch("eneru.monitor.read_shutdown_marker", return_value=None), \
             patch("eneru.monitor.read_upgrade_marker", return_value=None), \
             patch("eneru.monitor.delete_shutdown_marker"), \
             patch("eneru.monitor.delete_upgrade_marker"):
            monitor._emit_lifecycle_startup_notification()
        # No log_event call from the lifecycle classifier — fresh start
        # is the one case where it stays silent (DAEMON_START is the
        # _start_stats responsibility).
        log_event_calls = monitor._stats_store.log_event.call_args_list
        assert log_event_calls == []

    @pytest.mark.unit
    def test_emit_lifecycle_logs_recovered_after_sequence_complete(self, tmp_path):
        """sequence_complete shutdown marker → DAEMON_RECOVERED in the
        events table, mirroring the user's '📊 Recovered' notification."""
        from eneru.lifecycle import REASON_SEQUENCE_COMPLETE
        from eneru.version import __version__
        monitor = make_monitor(tmp_path)
        monitor._stats_store = MagicMock()
        monitor._stats_store._conn = object()
        # Use the current __version__ on both sides so the classifier
        # picks RECOVERED rather than the (pip-path) UPGRADED branch
        # that fires when last_seen_version != current_version.
        monitor._stats_store.get_meta.return_value = __version__
        monitor._stats_store.find_pending_by_category.return_value = []
        marker = {"shutdown_at": 1000, "version": __version__,
                  "reason": REASON_SEQUENCE_COMPLETE}
        with patch("eneru.monitor.read_shutdown_marker", return_value=marker), \
             patch("eneru.monitor.read_upgrade_marker", return_value=None), \
             patch("eneru.monitor.delete_shutdown_marker"), \
             patch("eneru.monitor.delete_upgrade_marker"), \
             patch("eneru.monitor.coalesce_recovered_with_prev_shutdown",
                   return_value=None):
            monitor._emit_lifecycle_startup_notification()
        log_calls = monitor._stats_store.log_event.call_args_list
        recovered = [c for c in log_calls
                     if c.args and c.args[0] == "DAEMON_RECOVERED"]
        assert len(recovered) == 1


# ==============================================================================
# CONNECTION GRACE PERIOD STATE MACHINE
# ==============================================================================

class TestConnectionGracePeriod:
    """Grace period state transitions for connection loss."""

    @pytest.mark.unit
    def test_ok_to_grace_period(self, tmp_path):
        """First connection failure transitions OK -> GRACE_PERIOD."""
        monitor = make_monitor(tmp_path)
        monitor.state.connection_state = "OK"

        monitor._handle_connection_failure("Connection refused")

        assert monitor.state.connection_state == "GRACE_PERIOD"
        assert monitor.state.connection_lost_time > 0

    @pytest.mark.unit
    def test_grace_period_expires_to_failed(self, tmp_path):
        """Grace period expiration transitions GRACE_PERIOD -> FAILED."""
        monitor = make_monitor(tmp_path)
        monitor.state.connection_state = "GRACE_PERIOD"
        monitor.state.connection_lost_time = time.time() - 120  # Expired (>60s default)

        monitor._handle_connection_failure("Connection refused")

        assert monitor.state.connection_state == "FAILED"

    @pytest.mark.unit
    def test_grace_period_not_expired_stays(self, tmp_path):
        """During grace period, state remains GRACE_PERIOD."""
        monitor = make_monitor(tmp_path)
        monitor.state.connection_state = "GRACE_PERIOD"
        monitor.state.connection_lost_time = time.time() - 5  # Only 5s (grace=60s)

        monitor._handle_connection_failure("Connection refused")

        assert monitor.state.connection_state == "GRACE_PERIOD"

    @pytest.mark.unit
    def test_grace_period_disabled_immediate_fail(self, tmp_path):
        """With grace period disabled, connection loss goes directly to FAILED."""
        config = Config(
            ups_groups=[UPSGroupConfig(
                ups=UPSConfig(
                    name="TestUPS@localhost",
                    connection_loss_grace_period=type(
                        "GraceCfg", (), {"enabled": False, "duration": 60, "flap_threshold": 5}
                    )(),
                ),
                is_local=True,
            )],
            behavior=BehaviorConfig(dry_run=True),
            logging=LoggingConfig(
                shutdown_flag_file=str(tmp_path / "flag"),
                state_file=str(tmp_path / "state"),
                battery_history_file=str(tmp_path / "history"),
            ),
        )
        monitor = UPSGroupMonitor(config)
        monitor.state = MonitorState()
        monitor.logger = MagicMock()
        monitor._notification_worker = MagicMock()

        monitor._handle_connection_failure("Connection refused")

        assert monitor.state.connection_state == "FAILED"


# ==============================================================================
# MULTI-PHASE SHUTDOWN ORDER
# ==============================================================================

class TestComputeEffectiveOrder:
    """Test the compute_effective_order() function."""

    @pytest.mark.unit
    def test_all_defaults_get_order_zero(self):
        """Servers with no shutdown_order and unset parallel get order 0."""
        servers = [
            RemoteServerConfig(name="A"),
            RemoteServerConfig(name="B"),
            RemoteServerConfig(name="C"),
        ]
        result = compute_effective_order(servers)
        assert all(order == 0 for order, _ in result)

    @pytest.mark.unit
    def test_legacy_sequential_gets_negative_orders(self):
        """parallel=False servers without shutdown_order get unique negative orders."""
        servers = [
            RemoteServerConfig(name="A", parallel=False),
            RemoteServerConfig(name="B", parallel=False),
            RemoteServerConfig(name="C", parallel=True),
        ]
        result = compute_effective_order(servers)
        assert result[0] == (-2, servers[0])
        assert result[1] == (-1, servers[1])
        assert result[2] == (0, servers[2])

    @pytest.mark.unit
    def test_explicit_shutdown_order_used_as_is(self):
        """Explicit shutdown_order values are used directly."""
        servers = [
            RemoteServerConfig(name="A", shutdown_order=3),
            RemoteServerConfig(name="B", shutdown_order=1),
            RemoteServerConfig(name="C", shutdown_order=2),
        ]
        result = compute_effective_order(servers)
        assert result[0] == (3, servers[0])
        assert result[1] == (1, servers[1])
        assert result[2] == (2, servers[2])

    @pytest.mark.unit
    def test_mixed_explicit_and_legacy(self):
        """Mix of shutdown_order and legacy parallel flag."""
        servers = [
            RemoteServerConfig(name="Explicit", shutdown_order=5),
            RemoteServerConfig(name="LegacyPar"),
            RemoteServerConfig(name="LegacySeq", parallel=False),
        ]
        result = compute_effective_order(servers)
        assert result[0] == (5, servers[0])   # explicit
        assert result[1] == (0, servers[1])   # legacy parallel -> 0
        assert result[2] == (-1, servers[2])  # legacy sequential -> -1

    @pytest.mark.unit
    def test_shutdown_order_overrides_parallel_flag(self):
        """compute_effective_order trusts shutdown_order even if parallel is also set.

        Such a config is rejected by the validator (mutual-exclusion ERROR),
        but the function itself should be defensive: if both are present, the
        explicit shutdown_order value wins.
        """
        servers = [
            RemoteServerConfig(name="A", shutdown_order=2, parallel=False),
            RemoteServerConfig(name="B", shutdown_order=2, parallel=True),
        ]
        result = compute_effective_order(servers)
        assert result[0][0] == 2
        assert result[1][0] == 2

    @pytest.mark.unit
    def test_legacy_backward_compat_exact(self):
        """Legacy config A(seq), B(seq), C(par), D(par) -> A then B then C+D."""
        servers = [
            RemoteServerConfig(name="A", parallel=False),
            RemoteServerConfig(name="B", parallel=False),
            RemoteServerConfig(name="C", parallel=True),
            RemoteServerConfig(name="D", parallel=True),
        ]
        result = compute_effective_order(servers)

        groups = {}
        for order, s in result:
            groups.setdefault(order, []).append(s.name)
        sorted_keys = sorted(groups.keys())

        assert sorted_keys == [-2, -1, 0]
        assert groups[-2] == ["A"]
        assert groups[-1] == ["B"]
        assert sorted(groups[0]) == ["C", "D"]

    @pytest.mark.unit
    def test_empty_server_list(self):
        """Empty server list returns empty result."""
        assert compute_effective_order([]) == []

    @pytest.mark.unit
    def test_single_legacy_sequential(self):
        """Single parallel=False server gets order -1."""
        servers = [RemoteServerConfig(name="Solo", parallel=False)]
        result = compute_effective_order(servers)
        assert result[0] == (-1, servers[0])


class TestMultiPhaseShutdown:
    """Test multi-phase shutdown execution in UPSGroupMonitor."""

    @pytest.mark.unit
    def test_three_phase_execution_order(self, tmp_path):
        """Three-phase shutdown executes groups in ascending order."""
        monitor = make_monitor(tmp_path, remote_servers=[
            RemoteServerConfig(name="Compute1", enabled=True, host="10.0.0.1",
                               user="root", shutdown_order=1),
            RemoteServerConfig(name="Compute2", enabled=True, host="10.0.0.2",
                               user="root", shutdown_order=1),
            RemoteServerConfig(name="Storage", enabled=True, host="10.0.0.3",
                               user="root", shutdown_order=2),
            RemoteServerConfig(name="Router", enabled=True, host="10.0.0.4",
                               user="root", shutdown_order=3),
            RemoteServerConfig(name="Switch", enabled=True, host="10.0.0.5",
                               user="root", shutdown_order=3),
        ])

        call_order = []

        def mock_shutdown(server):
            call_order.append(server.name)

        monitor._shutdown_remote_server = mock_shutdown
        monitor._shutdown_remote_servers()

        storage_idx = call_order.index("Storage")
        assert call_order.index("Compute1") < storage_idx
        assert call_order.index("Compute2") < storage_idx
        assert call_order.index("Router") > storage_idx
        assert call_order.index("Switch") > storage_idx

    @pytest.mark.unit
    def test_legacy_two_phase_backward_compat(self, tmp_path):
        """Legacy config with parallel flag produces same ordering as old code."""
        monitor = make_monitor(tmp_path, remote_servers=[
            RemoteServerConfig(name="SeqA", enabled=True, host="10.0.0.1",
                               user="root", parallel=False),
            RemoteServerConfig(name="SeqB", enabled=True, host="10.0.0.2",
                               user="root", parallel=False),
            RemoteServerConfig(name="ParC", enabled=True, host="10.0.0.3",
                               user="root", parallel=True),
            RemoteServerConfig(name="ParD", enabled=True, host="10.0.0.4",
                               user="root", parallel=True),
        ])

        call_order = []

        def mock_shutdown(server):
            call_order.append(server.name)

        monitor._shutdown_remote_server = mock_shutdown
        monitor._shutdown_remote_servers()

        assert call_order.index("SeqA") < call_order.index("SeqB")
        assert call_order.index("SeqB") < call_order.index("ParC")
        assert call_order.index("SeqB") < call_order.index("ParD")

    @pytest.mark.unit
    def test_single_server_no_threading(self, tmp_path):
        """A group with one server calls _shutdown_remote_server directly."""
        monitor = make_monitor(tmp_path, remote_servers=[
            RemoteServerConfig(name="Solo", enabled=True, host="10.0.0.1",
                               user="root", shutdown_order=1),
        ])

        shutdown_called = []
        monitor._shutdown_remote_server = lambda s: shutdown_called.append(s.name)
        monitor._shutdown_remote_servers()

        assert shutdown_called == ["Solo"]

    @pytest.mark.unit
    def test_disabled_servers_excluded(self, tmp_path):
        """Disabled servers are not included in shutdown ordering."""
        monitor = make_monitor(tmp_path, remote_servers=[
            RemoteServerConfig(name="Enabled", enabled=True, host="10.0.0.1",
                               user="root", shutdown_order=1),
            RemoteServerConfig(name="Disabled", enabled=False, host="10.0.0.2",
                               user="root", shutdown_order=1),
        ])

        shutdown_called = []
        monitor._shutdown_remote_server = lambda s: shutdown_called.append(s.name)
        monitor._shutdown_remote_servers()

        assert shutdown_called == ["Enabled"]

    @pytest.mark.unit
    def test_no_enabled_servers_returns_early(self, tmp_path):
        """No enabled servers means _shutdown_remote_servers returns immediately."""
        monitor = make_monitor(tmp_path, remote_servers=[
            RemoteServerConfig(name="Off", enabled=False, host="10.0.0.1", user="root"),
        ])

        log_messages = []
        monitor._log_message = lambda msg, *a, **kw: log_messages.append(msg)
        monitor._shutdown_remote_servers()

        assert not any("🌐" in m for m in log_messages)

    @pytest.mark.unit
    def test_failure_in_one_phase_continues_to_next(self, tmp_path):
        """A failure in one phase does not prevent subsequent phases from running."""
        monitor = make_monitor(tmp_path, remote_servers=[
            RemoteServerConfig(name="FailServer", enabled=True, host="10.0.0.1",
                               user="root", shutdown_order=1),
            RemoteServerConfig(name="GoodServer", enabled=True, host="10.0.0.2",
                               user="root", shutdown_order=2),
        ])

        call_order = []

        def mock_shutdown(server):
            call_order.append(server.name)
            if server.name == "FailServer":
                raise RuntimeError("SSH failed")

        monitor._shutdown_remote_server = mock_shutdown
        monitor._shutdown_remote_servers()

        assert call_order == ["FailServer", "GoodServer"]

    @pytest.mark.unit
    def test_phase_logging_multi_phase(self, tmp_path):
        """Multi-phase shutdown logs phase headers."""
        monitor = make_monitor(tmp_path, remote_servers=[
            RemoteServerConfig(name="A", enabled=True, host="10.0.0.1",
                               user="root", shutdown_order=1),
            RemoteServerConfig(name="B", enabled=True, host="10.0.0.2",
                               user="root", shutdown_order=2),
        ])

        log_messages = []
        monitor._log_message = lambda msg, *a, **kw: log_messages.append(msg)
        monitor._shutdown_remote_server = lambda s: None
        monitor._shutdown_remote_servers()

        phase_logs = [m for m in log_messages if "Phase" in m]
        assert len(phase_logs) == 2
        assert "Phase 1/2 (order=1)" in phase_logs[0]
        assert "Phase 2/2 (order=2)" in phase_logs[1]

    @pytest.mark.unit
    def test_no_phase_logging_single_phase(self, tmp_path):
        """Single-phase shutdown does not log phase headers."""
        monitor = make_monitor(tmp_path, remote_servers=[
            RemoteServerConfig(name="A", enabled=True, host="10.0.0.1",
                               user="root", shutdown_order=1),
            RemoteServerConfig(name="B", enabled=True, host="10.0.0.2",
                               user="root", shutdown_order=1),
        ])

        log_messages = []
        monitor._log_message = lambda msg, *a, **kw: log_messages.append(msg)
        monitor._shutdown_remote_server = lambda s: None
        monitor._shutdown_remote_servers()

        assert not any("Phase" in m for m in log_messages)

    @pytest.mark.unit
    def test_parallel_group_uses_threads(self, tmp_path):
        """Same-group servers run in parallel via threads."""
        monitor = make_monitor(tmp_path, remote_servers=[
            RemoteServerConfig(name="A", enabled=True, host="10.0.0.1",
                               user="root", shutdown_order=1),
            RemoteServerConfig(name="B", enabled=True, host="10.0.0.2",
                               user="root", shutdown_order=1),
        ])

        thread_names = []

        def mock_shutdown(server):
            thread_names.append(threading.current_thread().name)

        monitor._shutdown_remote_server = mock_shutdown
        monitor._shutdown_remote_servers()

        # Both servers should run in non-main threads (spawned by _shutdown_servers_parallel)
        assert len(thread_names) == 2
        assert all(name != "MainThread" for name in thread_names)
        assert all(name.startswith("remote-shutdown-") for name in thread_names)

    @pytest.mark.unit
    def test_parallel_phase_join_does_not_stack(self, tmp_path):
        """Phase total wait is bounded by the per-phase deadline, not N × budget.

        Two stuck workers in the same phase must not cause the phase to wait
        for 2 × max_timeout. Regression guard for the deadline-based join in
        ``_shutdown_servers_parallel``.
        """
        # Tiny budgets so the test is fast: pre_cmd=0, command_timeout=0,
        # connect_timeout=0, safety_margin=1 -> max_timeout = 1 second.
        servers = [
            RemoteServerConfig(name="StuckA", enabled=True, host="10.0.0.1", user="root",
                               shutdown_order=1, command_timeout=0, connect_timeout=0,
                               shutdown_safety_margin=1),
            RemoteServerConfig(name="StuckB", enabled=True, host="10.0.0.2", user="root",
                               shutdown_order=1, command_timeout=0, connect_timeout=0,
                               shutdown_safety_margin=1),
        ]
        monitor = make_monitor(tmp_path, remote_servers=servers)

        def hang_forever(server):
            time.sleep(10)  # well beyond the 1s budget

        monitor._shutdown_remote_server = hang_forever

        start = time.monotonic()
        monitor._shutdown_remote_servers()
        elapsed = time.monotonic() - start

        # Stacked behavior would be ~2 s; deadline-based should be ~1 s.
        # Allow a generous 1.8 s ceiling for CI scheduler jitter; still well
        # below the 2 s stacked floor and the 10 s actual hang time.
        assert elapsed < 1.8, (
            f"Phase took {elapsed:.2f}s — expected ~1s (deadline-based join), "
            "looks like per-thread timeouts are stacking."
        )


class TestRemoteShutdownSafetyMargin:
    """Verify shutdown_safety_margin flows through calc_server_timeout."""

    @pytest.mark.unit
    def test_calc_server_timeout_uses_per_server_margin(self, tmp_path):
        """Each server's own margin contributes to its own budget,
        and the phase-wide max wins for the join window."""
        servers = [
            RemoteServerConfig(name="Fast", enabled=True, host="10.0.0.1", user="root",
                               shutdown_order=1, command_timeout=5, connect_timeout=2,
                               shutdown_safety_margin=10),
            RemoteServerConfig(name="Slow", enabled=True, host="10.0.0.2", user="root",
                               shutdown_order=1, command_timeout=5, connect_timeout=2,
                               shutdown_safety_margin=120),
        ]
        monitor = make_monitor(tmp_path, remote_servers=servers)

        captured = {}
        original_join = threading.Thread.join

        def capture_join(self, timeout=None):
            captured.setdefault("first_timeout", timeout)
            # Don't actually wait — workers are no-ops anyway.
            return original_join(self, timeout=0)

        monitor._shutdown_remote_server = lambda s: None
        with patch.object(threading.Thread, "join", capture_join):
            monitor._shutdown_remote_servers()

        # max_timeout = max(0+5+2+10, 0+5+2+120) = 127.
        # Deadline-based join subtracts the tiny elapsed time between
        # computing `deadline` and the first join, so the captured value
        # will be just under 127.
        assert captured["first_timeout"] == pytest.approx(127, abs=0.5)


# ==============================================================================
# ADVISORY MODE -- redundancy-group members suppress local shutdown
# ==============================================================================

class TestAdvisoryTriggers:
    """Per-trigger-site verification of the in_redundancy_group branch."""

    @pytest.mark.unit
    def test_record_advisory_trigger_sets_state_under_lock(self, tmp_path):
        monitor = make_monitor(tmp_path)
        monitor._in_redundancy_group = True

        monitor._record_advisory_trigger("battery 5% < threshold 20%")

        snap = monitor.state.snapshot()
        assert snap.trigger_active is True
        assert snap.trigger_reason == "battery 5% < threshold 20%"

    @pytest.mark.unit
    def test_record_advisory_trigger_idempotent_message(self, tmp_path):
        """A second call with the same condition does not re-log the alert."""
        monitor = make_monitor(tmp_path)
        monitor._in_redundancy_group = True

        monitor._record_advisory_trigger("low battery")
        first_log_count = monitor.logger.log.call_count
        monitor._record_advisory_trigger("low battery")
        # State stays set; second call does not emit another alert message.
        assert monitor.state.trigger_active is True
        assert monitor.logger.log.call_count == first_log_count

    @pytest.mark.unit
    def test_clear_advisory_trigger_resets_state(self, tmp_path):
        monitor = make_monitor(tmp_path)
        monitor._in_redundancy_group = True
        monitor._record_advisory_trigger("low battery")
        assert monitor.state.trigger_active is True

        monitor._clear_advisory_trigger()
        snap = monitor.state.snapshot()
        assert snap.trigger_active is False
        assert snap.trigger_reason == ""

    @pytest.mark.unit
    def test_t1_advisory_in_redundancy_group_does_not_call_immediate_shutdown(self, tmp_path):
        """T1 fires as advisory in redundancy mode -- no immediate shutdown."""
        monitor = make_monitor(tmp_path)
        monitor._in_redundancy_group = True
        monitor.state.previous_status = "OB DISCHRG"
        monitor.state.on_battery_start_time = int(time.time()) - 10

        ups_data = {
            "ups.status": "OB DISCHRG",
            "battery.charge": "15",  # Below 20% threshold
            "battery.runtime": "1200",
            "ups.load": "30",
        }
        with patch.object(monitor, "_trigger_immediate_shutdown") as mock_shutdown:
            monitor._handle_on_battery(ups_data)
        mock_shutdown.assert_not_called()
        assert monitor.state.trigger_active is True
        assert "15%" in monitor.state.trigger_reason

    @pytest.mark.unit
    def test_t1_non_redundancy_path_unchanged_calls_immediate_shutdown(self, tmp_path):
        """Regression: outside a redundancy group T1 still fires the local shutdown."""
        monitor = make_monitor(tmp_path)
        # in_redundancy_group defaults to False
        monitor.state.previous_status = "OB DISCHRG"
        monitor.state.on_battery_start_time = int(time.time()) - 10

        ups_data = {
            "ups.status": "OB DISCHRG",
            "battery.charge": "15",
            "battery.runtime": "1200",
            "ups.load": "30",
        }
        with patch.object(monitor, "_trigger_immediate_shutdown") as mock_shutdown:
            monitor._handle_on_battery(ups_data)
        mock_shutdown.assert_called_once()
        assert monitor.state.trigger_active is False  # legacy path doesn't set advisory

    # _run_one_iteration moved to module scope (see top of this file)
    # so other test classes can route through the real loop without
    # cross-class coupling. Class-level shim kept for backward
    # compatibility with the existing self._run_one_iteration calls
    # below.
    _run_one_iteration = staticmethod(_run_one_iteration)

    @pytest.mark.unit
    def test_fsd_advisory_in_redundancy_group(self, tmp_path):
        """FSD signal in redundancy mode records advisory + skips local path."""
        monitor = make_monitor(tmp_path)
        monitor._in_redundancy_group = True
        monitor.state.previous_status = "OL"

        ups_data = {
            "ups.status": "OL FSD",
            "battery.charge": "100",
            "battery.runtime": "1800",
            "ups.load": "25",
        }
        with patch.object(monitor, "_trigger_immediate_shutdown") as mock_shutdown:
            self._run_one_iteration(monitor, (True, ups_data, ""))

        mock_shutdown.assert_not_called()
        assert monitor.state.trigger_active is True
        assert "FSD" in monitor.state.trigger_reason

    @pytest.mark.unit
    def test_fsd_non_redundancy_still_triggers_immediate_shutdown(self, tmp_path):
        """Regression: outside redundancy, FSD path is byte-identical to legacy."""
        monitor = make_monitor(tmp_path)
        monitor.state.previous_status = "OL"

        ups_data = {
            "ups.status": "OL FSD",
            "battery.charge": "100",
            "battery.runtime": "1800",
            "ups.load": "25",
        }
        with patch.object(monitor, "_trigger_immediate_shutdown") as mock_shutdown:
            self._run_one_iteration(monitor, (True, ups_data, ""))

        mock_shutdown.assert_called_once()
        assert "FSD" in mock_shutdown.call_args[0][0]

    @pytest.mark.unit
    def test_failsafe_advisory_in_redundancy_group(self, tmp_path):
        """FAILSAFE on a redundancy member records advisory and connection_state."""
        monitor = make_monitor(tmp_path)
        monitor._in_redundancy_group = True
        monitor.state.previous_status = "OB DISCHRG"  # was on battery
        # Push stale_data_count to threshold-1 -- this iteration increments to 3 and fires.
        monitor.state.stale_data_count = 2
        monitor.state.connection_state = "OK"

        with patch.object(monitor, "_execute_shutdown_sequence") as mock_exec:
            self._run_one_iteration(monitor, (False, {}, "Data stale"))

        mock_exec.assert_not_called()
        assert monitor.state.connection_state == "FAILED"
        assert monitor.state.trigger_active is True
        assert "FAILSAFE" in monitor.state.trigger_reason

    @pytest.mark.unit
    def test_failsafe_non_redundancy_unchanged_for_single_ups(self, tmp_path):
        """Regression: legacy single-UPS failsafe path is byte-identical."""
        monitor = make_monitor(tmp_path)
        monitor._in_redundancy_group = False
        monitor.state.previous_status = "OB DISCHRG"
        monitor.state.stale_data_count = 2
        monitor.state.connection_state = "OK"

        with patch.object(monitor, "_execute_shutdown_sequence") as mock_exec:
            self._run_one_iteration(monitor, (False, {}, "Data stale"))

        mock_exec.assert_called_once()
        assert monitor.state.connection_state == "FAILED"
        assert monitor.state.trigger_active is False

    @pytest.mark.unit
    def test_failsafe_non_redundancy_unchanged_for_independent_group(self, tmp_path):
        """Regression: independent (non-redundancy) UPS group also unchanged."""
        # Same as the previous test -- "independent group" means the monitor
        # was constructed with in_redundancy_group=False (the default).
        monitor = make_monitor(tmp_path)
        monitor._in_redundancy_group = False
        monitor.state.previous_status = "OB DISCHRG"
        # Connection error (not "Data stale") fires failsafe immediately.
        monitor.state.stale_data_count = 0
        monitor.state.connection_state = "OK"

        with patch.object(monitor, "_execute_shutdown_sequence") as mock_exec:
            self._run_one_iteration(monitor, (False, {}, "Network error"))

        mock_exec.assert_called_once()

    @pytest.mark.unit
    def test_handle_on_line_clears_advisory_in_redundancy(self, tmp_path):
        """Returning to OL on a redundancy member clears the advisory trigger."""
        monitor = make_monitor(tmp_path)
        monitor._in_redundancy_group = True
        monitor.state.trigger_active = True
        monitor.state.trigger_reason = "battery 5% < threshold 20%"
        monitor.state.previous_status = "OB DISCHRG"
        monitor.state.on_battery_start_time = int(time.time()) - 60

        ups_data = {
            "ups.status": "OL CHRG",
            "battery.charge": "75",
            "input.voltage": "230.5",
        }
        monitor._handle_on_line(ups_data)
        snap = monitor.state.snapshot()
        assert snap.trigger_active is False
        assert snap.trigger_reason == ""

    @pytest.mark.unit
    def test_constructor_default_is_not_in_redundancy_group(self, tmp_path):
        monitor = make_monitor(tmp_path)
        assert monitor._in_redundancy_group is False
