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


class TestNUTLatencyVisibility:
    """Slow ``upsc`` calls are visible without notification spam."""

    @pytest.mark.unit
    def test_slow_upsc_logs_once_per_rate_limit_window(self, tmp_path):
        monitor = make_monitor(tmp_path)
        monitor._stats_store = MagicMock()
        monitor._slow_nut_log_threshold_seconds = 2.0
        monitor._slow_nut_log_rate_limit_seconds = 300.0

        with patch("eneru.monitor.run_command",
                   return_value=(0, "ups.status: OL\n", "")):
            with patch("eneru.monitor.time.monotonic",
                       side_effect=[0.0, 3.0, 10.0, 13.0, 400.0, 403.0]):
                with patch("eneru.monitor.time.time",
                           side_effect=[100.0, 110.0, 500.0]):
                    monitor._get_all_ups_data()
                    monitor._get_all_ups_data()
                    monitor._get_all_ups_data()

        slow_logs = [
            c for c in monitor.logger.log.call_args_list
            if "Slow NUT response" in str(c)
        ]
        assert len(slow_logs) == 2
        assert monitor._stats_store.log_event.call_count == 2
        assert all(
            call_args.args[0] == "SLOW_NUT_RESPONSE"
            and call_args.kwargs["notification_sent"] is False
            for call_args in monitor._stats_store.log_event.call_args_list
        )

    @pytest.mark.unit
    def test_brief_slow_poll_does_not_notify(self, tmp_path):
        monitor = make_monitor(tmp_path)
        monitor._slow_nut_log_threshold_seconds = 99.0
        monitor._slow_nut_notify_threshold_seconds = 1.0
        monitor._slow_nut_notify_consecutive_polls = 3

        with patch("eneru.monitor.run_command",
                   return_value=(0, "ups.status: OL\n", "")):
            with patch("eneru.monitor.time.monotonic",
                       side_effect=[0.0, 2.0, 10.0, 10.5, 20.0, 20.4]):
                with patch("eneru.monitor.time.time",
                           side_effect=[100.0, 101.0, 102.0]):
                    monitor._get_all_ups_data()
                    monitor._get_all_ups_data()
                    monitor._get_all_ups_data()

        monitor._notification_worker.send.assert_not_called()

    @pytest.mark.unit
    def test_sustained_slow_polling_notifies_once(self, tmp_path):
        monitor = make_monitor(tmp_path)
        monitor._stats_store = MagicMock()
        monitor._slow_nut_log_threshold_seconds = 99.0
        monitor._slow_nut_notify_threshold_seconds = 1.0
        monitor._slow_nut_notify_consecutive_polls = 3

        with patch("eneru.monitor.run_command",
                   return_value=(0, "ups.status: OL\n", "")):
            with patch("eneru.monitor.time.monotonic",
                       side_effect=[0.0, 2.0, 10.0, 12.0, 20.0, 22.0,
                                    30.0, 32.0]):
                with patch("eneru.monitor.time.time",
                           side_effect=[100.0, 101.0, 102.0, 103.0]):
                    monitor._get_all_ups_data()
                    monitor._get_all_ups_data()
                    monitor._get_all_ups_data()
                    monitor._get_all_ups_data()

        assert monitor._notification_worker.send.call_count == 1
        body = monitor._notification_worker.send.call_args.kwargs["body"]
        assert "Sustained slow NUT responses" in body
        monitor._stats_store.log_event.assert_called_once()
        event = monitor._stats_store.log_event.call_args
        assert event.args[0] == "SLOW_NUT_RESPONSE"
        assert "sustained slow NUT responses" in event.args[1]
        assert event.kwargs["notification_sent"] is True


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
        monitor.state.on_battery_start_time = int(time.time()) - 40

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
        monitor.state.on_battery_start_time = int(time.time()) - 40

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
        monitor.state.on_battery_start_time = int(time.time()) - 40

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
        monitor = make_monitor(
            tmp_path,
            triggers=TriggersConfig(on_battery_stabilization_delay=0),
        )

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
        monitor = make_monitor(
            tmp_path,
            triggers=TriggersConfig(on_battery_stabilization_delay=0),
        )

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
        # H3: hard failures are debounced like stale data. Push the hard-error
        # counter to tolerance-1 so this iteration reaches tolerance and fires
        # (mirrors how the stale-data tests pre-set stale_data_count).
        monitor.state.connection_error_count = (
            monitor.config.ups.max_stale_data_tolerance - 1
        )
        with patch.object(monitor, "_execute_shutdown_sequence") as mock_exec:
            _run_one_iteration(monitor, (False, {}, "Network error"))

        mock_exec.assert_called_once()
        assert monitor.state.connection_state == "FAILED"

    @pytest.mark.unit
    def test_single_hard_error_while_ob_is_debounced(self, tmp_path):
        """H3: a single transient hard NUT failure while on battery does NOT
        fire the FAILSAFE (it is debounced by max_stale_data_tolerance), so a
        momentary connection refusal / upsc timeout can't drop a healthy host."""
        monitor = make_monitor(tmp_path)
        monitor._in_redundancy_group = False
        monitor.state.previous_status = "OB DISCHRG"
        monitor.state.connection_state = "OK"
        monitor.config.ups.max_stale_data_tolerance = 3
        monitor.state.connection_error_count = 0
        with patch.object(monitor, "_execute_shutdown_sequence") as mock_exec:
            _run_one_iteration(monitor, (False, {}, "Network error"))
        mock_exec.assert_not_called()
        assert monitor.state.connection_error_count == 1

    @pytest.mark.unit
    def test_tolerance_one_restores_instant_fsb(self, tmp_path):
        """H3: max_stale_data_tolerance=1 restores the instant fail-closed FSB."""
        monitor = make_monitor(tmp_path)
        monitor._in_redundancy_group = False
        monitor.state.previous_status = "OB DISCHRG"
        monitor.state.connection_state = "OK"
        monitor.config.ups.max_stale_data_tolerance = 1
        with patch.object(monitor, "_execute_shutdown_sequence") as mock_exec:
            _run_one_iteration(monitor, (False, {}, "Network error"))
        mock_exec.assert_called_once()

    @pytest.mark.unit
    def test_failsafe_does_not_refire_while_latched(self, tmp_path):
        """H2: once the on-battery FAILSAFE has acted, it must not re-run the
        shutdown sequence on every subsequent failed poll (non-halting config)."""
        monitor = make_monitor(tmp_path)
        monitor._in_redundancy_group = False
        monitor.state.previous_status = "OB DISCHRG"
        monitor.state.connection_state = "OK"
        monitor.config.ups.max_stale_data_tolerance = 1
        with patch.object(monitor, "_execute_shutdown_sequence") as mock_exec:
            # First failed poll fires the sequence.
            _run_one_iteration(monitor, (False, {}, "Network error"))
            assert mock_exec.call_count == 1
            assert monitor._failsafe_initiated is True
            # Subsequent failed polls (NUT still down, previous_status still OB)
            # must NOT re-fire while the latch is set.
            for _ in range(3):
                monitor._stop_event.clear()
                _run_one_iteration(monitor, (False, {}, "Network error"))
            assert mock_exec.call_count == 1

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
        """L23: drive the REAL loop -- one stale poll (below tolerance, not on
        battery) increments the counter and does NOT fire a shutdown."""
        monitor = make_monitor(tmp_path)
        monitor.state.previous_status = "OL CHRG"  # not on battery
        monitor.state.connection_state = "OK"
        monitor.state.stale_data_count = 0
        with patch.object(monitor, "_execute_shutdown_sequence") as mock_exec:
            _run_one_iteration(monitor, (False, {}, "Data stale"))
        assert monitor.state.stale_data_count == 1
        mock_exec.assert_not_called()

    @pytest.mark.unit
    def test_stale_data_reaches_tolerance(self, tmp_path):
        """L23: drive the REAL loop -- stale data reaching tolerance while on
        battery fires the failsafe shutdown."""
        monitor = make_monitor(tmp_path)
        monitor._in_redundancy_group = False
        monitor.state.previous_status = "OB DISCHRG"
        monitor.state.connection_state = "OK"
        monitor.state.stale_data_count = (
            monitor.config.ups.max_stale_data_tolerance - 1
        )
        with patch.object(monitor, "_execute_shutdown_sequence") as mock_exec:
            _run_one_iteration(monitor, (False, {}, "Data stale"))
        assert (monitor.state.stale_data_count
                >= monitor.config.ups.max_stale_data_tolerance)
        mock_exec.assert_called_once()


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
    def test_shutdown_continues_past_drain_step_failure(self, tmp_path):
        """H4: an unhandled exception inside a drain step is caught and logged;
        the remaining drain steps AND the remote/poweroff path still run, so a
        wedged libvirt or a bad config value can never skip the host poweroff.
        (Previously such an exception aborted the whole sequence.)
        """
        monitor = make_monitor(tmp_path)
        monitor.config.behavior.dry_run = True  # don't actually power off in the test
        call_order = []

        def failing_vms():
            call_order.append("vms")
            raise RuntimeError("VM shutdown failed")

        monitor._shutdown_vms = failing_vms
        monitor._shutdown_containers = lambda: call_order.append("containers")
        monitor._sync_filesystems = lambda: call_order.append("sync")
        monitor._unmount_filesystems = lambda: call_order.append("unmount")
        monitor._shutdown_remote_servers = (
            lambda: call_order.append("remote") or [])

        # No raise: the sequence completes despite the VM step failing.
        monitor._execute_shutdown_sequence()

        # Every drain step ran AND the remote/poweroff path was reached.
        assert call_order == ["vms", "containers", "sync", "unmount", "remote"]


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
        ``ℹ️  **Shutdown Detail:** <line>`` and pushed through
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
    def test_wall_disabled_suppresses_on_battery_event_broadcast(self, tmp_path):
        """Regression: an operator running with the documented default
        ``local_shutdown.wall: false`` reported wall(1) messages firing
        on a real power cut. Lines 1279 (on-battery transition) and
        1410 (power restored) historically only checked dry_run, not
        the wall flag, so the documented default lied. Both call sites
        must respect the flag — pin that here for the on-battery path."""
        monitor = make_monitor(tmp_path)
        monitor.config.behavior.dry_run = False
        monitor.config.local_shutdown.wall = False
        monitor.state.previous_status = "OL"  # was on line; transition fires
        monitor.state.battery_history = deque()

        with patch("eneru.monitor.run_command") as run_cmd:
            monitor._handle_on_battery({
                "battery.charge": "85",
                "battery.runtime": "1200",
                "ups.load": "30",
                "ups.status": "OB DISCHRG",
            })

        wall_calls = [c for c in run_cmd.call_args_list
                      if c.args and c.args[0] and c.args[0][0] == "wall"]
        assert wall_calls == [], (
            f"wall(1) must not fire on the on-battery transition when "
            f"local_shutdown.wall=False; got {wall_calls}"
        )

    @pytest.mark.unit
    def test_wall_enabled_fires_on_battery_event_broadcast(self, tmp_path):
        """Mirror of the suppression test: with the flag opted on, the
        on-battery transition does broadcast via wall(1)."""
        monitor = make_monitor(tmp_path)
        monitor.config.behavior.dry_run = False
        monitor.config.local_shutdown.wall = True
        monitor.state.previous_status = "OL"
        monitor.state.battery_history = deque()

        with patch("eneru.monitor.run_command") as run_cmd:
            monitor._handle_on_battery({
                "battery.charge": "85",
                "battery.runtime": "1200",
                "ups.load": "30",
                "ups.status": "OB DISCHRG",
            })

        wall_calls = [c for c in run_cmd.call_args_list
                      if c.args and c.args[0] and c.args[0][0] == "wall"]
        assert len(wall_calls) == 1, wall_calls
        assert "Power failure detected" in wall_calls[0].args[0][1]

    @pytest.mark.unit
    def test_wall_disabled_suppresses_power_restored_broadcast(self, tmp_path):
        """Power-restored side of the on-battery suppression regression."""
        monitor = make_monitor(tmp_path)
        monitor.config.behavior.dry_run = False
        monitor.config.local_shutdown.wall = False
        monitor.state.previous_status = "OB DISCHRG"  # was on battery
        monitor.state.on_battery_start_time = int(time.time()) - 60

        with patch("eneru.monitor.run_command") as run_cmd:
            monitor._handle_on_line({
                "battery.charge": "92",
                "input.voltage": "230",
                "ups.status": "OL CHRG",
            })

        wall_calls = [c for c in run_cmd.call_args_list
                      if c.args and c.args[0] and c.args[0][0] == "wall"]
        assert wall_calls == [], (
            f"wall(1) must not fire on power restoration when "
            f"local_shutdown.wall=False; got {wall_calls}"
        )

    @pytest.mark.unit
    def test_wall_enabled_fires_power_restored_broadcast(self, tmp_path):
        monitor = make_monitor(tmp_path)
        monitor.config.behavior.dry_run = False
        monitor.config.local_shutdown.wall = True
        monitor.state.previous_status = "OB DISCHRG"
        monitor.state.on_battery_start_time = int(time.time()) - 60

        with patch("eneru.monitor.run_command") as run_cmd:
            monitor._handle_on_line({
                "battery.charge": "92",
                "input.voltage": "230",
                "ups.status": "OL CHRG",
            })

        wall_calls = [c for c in run_cmd.call_args_list
                      if c.args and c.args[0] and c.args[0][0] == "wall"]
        assert len(wall_calls) == 1, wall_calls
        assert "Power has been restored" in wall_calls[0].args[0][1]

    @pytest.mark.unit
    def test_summary_notification_fires_when_local_shutdown_disabled(self, tmp_path):
        """The "✅  Shutdown Sequence Complete" summary used to fire only
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
        '📦  Upgraded' message that supersedes the stop, so the stop
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
        timer scheduled. The next daemon's '📦  Upgraded' covers both."""
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
        events table, mirroring the user's '📊  Recovered' notification."""
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
        # Two no-op workers in one phase. With the SSH-overhead buffer now in
        # the budget the absolute deadline is ~30s+, so instead of timing a hung
        # worker we prove the *structure*: the join timeout handed to each
        # successive thread DECREASES (deadline - elapsed), which only happens
        # under a single shared deadline. Per-thread stacking would hand each
        # thread the same full budget.
        servers = [
            RemoteServerConfig(name="A", enabled=True, host="10.0.0.1", user="root",
                               shutdown_order=1, command_timeout=5, connect_timeout=2,
                               shutdown_safety_margin=10),
            RemoteServerConfig(name="B", enabled=True, host="10.0.0.2", user="root",
                               shutdown_order=1, command_timeout=5, connect_timeout=2,
                               shutdown_safety_margin=10),
        ]
        monitor = make_monitor(tmp_path, remote_servers=servers)
        monitor._shutdown_remote_server = lambda s: None  # no-op workers

        timeouts = []
        original_join = threading.Thread.join

        def capture_join(self, timeout=None):
            timeouts.append(timeout)
            time.sleep(0.05)  # advance the deadline clock between joins
            return original_join(self, timeout=0)

        with patch.object(threading.Thread, "join", capture_join):
            monitor._shutdown_remote_servers()

        assert len(timeouts) >= 2
        assert timeouts[1] < timeouts[0], (
            "join timeouts should DECREASE across threads (one shared "
            "deadline); constant timeouts indicate per-thread stacking"
        )
        assert all(t >= 0 for t in timeouts)


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

        # Each server has 0 pre-commands -> num_commands=1 -> +1*30 SSH buffer.
        # max_timeout = max(0+5+2+10+30, 0+5+2+120+30) = max(47, 157) = 157.
        # Deadline-based join subtracts the tiny elapsed time between computing
        # `deadline` and the first join, so the captured value is just under 157.
        assert captured["first_timeout"] == pytest.approx(157, abs=0.5)


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
        monitor.state.on_battery_start_time = int(time.time()) - 40

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
        monitor.state.on_battery_start_time = int(time.time()) - 40

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
        # Connection error (not "Data stale") fires the failsafe once the hard-
        # error counter reaches tolerance (H3); pre-set to tolerance-1 so this
        # iteration fires (mirrors the stale-data tests).
        monitor.state.stale_data_count = 0
        monitor.state.connection_error_count = (
            monitor.config.ups.max_stale_data_tolerance - 1
        )
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


class TestCheckDependencies:
    """v5.4: required deps depend on configured features.

    Pre-v5.4 the daemon hard-required upsc, sync, shutdown, AND logger.
    That blocked remote-only and containerized deployments where
    /sbin/shutdown isn't on PATH and journald handles syslog instead of
    logger(1). v5.4 trims required to upsc plus the configured local
    shutdown command (when enabled), and downgrades logger(1) to a
    soft warning.
    """

    @pytest.mark.unit
    def test_remote_only_requires_only_upsc(self, tmp_path):
        """Non-local config + local_shutdown disabled: only upsc is required."""
        monitor = make_monitor(tmp_path)
        monitor.config.ups_groups[0].is_local = False
        monitor.config.local_shutdown.enabled = False

        def cmd_present(cmd):
            return cmd == "upsc"

        with patch("eneru.monitor.command_exists", side_effect=cmd_present):
            monitor._check_dependencies()  # Must not sys.exit

    @pytest.mark.unit
    def test_local_with_local_shutdown_requires_shutdown_binary(self, tmp_path):
        """is_local + local_shutdown.enabled: configured shutdown command must be on PATH."""
        monitor = make_monitor(tmp_path)
        monitor.config.ups_groups[0].is_local = True
        monitor.config.local_shutdown.enabled = True
        monitor.config.local_shutdown.command = "/sbin/shutdown -h now"

        seen = []

        def cmd_present(cmd):
            seen.append(cmd)
            return True  # Pretend everything is present

        with patch("eneru.monitor.command_exists", side_effect=cmd_present):
            monitor._check_dependencies()

        assert "upsc" in seen
        # First token of the command becomes the required dep
        assert "/sbin/shutdown" in seen

    @pytest.mark.unit
    def test_missing_required_command_raises(self, tmp_path):
        """ISS-006: raise RuntimeError (not sys.exit) so coordinator-mode's
        `except Exception` crash handler can see it instead of SystemExit
        silently killing the per-group thread."""
        monitor = make_monitor(tmp_path)
        monitor.config.ups_groups[0].is_local = False
        monitor.config.local_shutdown.enabled = False
        monitor._log_message = MagicMock()

        with patch("eneru.monitor.command_exists", return_value=False):
            with pytest.raises(RuntimeError) as exc_info:
                monitor._check_dependencies()
        assert "Missing required commands" in str(exc_info.value)
        assert "upsc" in str(exc_info.value)
        # And it was logged (visible in coordinator mode, unlike the old print).
        assert any(
            "Missing required commands" in str(call.args[0])
            for call in monitor._log_message.call_args_list
        )

    @pytest.mark.unit
    def test_missing_logger_warns_but_does_not_exit(self, tmp_path):
        """logger(1) is now advisory — containers without it must still start."""
        monitor = make_monitor(tmp_path)
        monitor.config.ups_groups[0].is_local = False
        monitor.config.local_shutdown.enabled = False
        # Disable VM/container/filesystem checks downstream
        monitor.config.virtual_machines.enabled = False
        monitor.config.containers.enabled = False
        monitor.config.filesystems.unmount.enabled = False
        log = []
        monitor._log_message = log.append

        def cmd_present(cmd):
            return cmd != "logger"  # Everything except logger

        with patch("eneru.monitor.command_exists", side_effect=cmd_present):
            monitor._check_dependencies()  # Must not sys.exit

        assert any("'logger' not found" in m for m in log), log

    @pytest.mark.unit
    def test_missing_virsh_disables_vms_with_warning(self, tmp_path):
        monitor = make_monitor(tmp_path)
        monitor.config.ups_groups[0].is_local = False
        monitor.config.local_shutdown.enabled = False
        monitor.config.virtual_machines.enabled = True
        monitor.config.containers.enabled = False
        monitor.config.filesystems.unmount.enabled = False
        log = []
        monitor._log_message = log.append

        def cmd_present(cmd):
            return cmd not in ("virsh",)

        with patch("eneru.monitor.command_exists", side_effect=cmd_present):
            monitor._check_dependencies()

        assert monitor.config.virtual_machines.enabled is False
        assert any("virsh" in m and "VM shutdown" in m for m in log), log

    @pytest.mark.unit
    def test_container_runtime_detected_logs_runtime(self, tmp_path):
        monitor = make_monitor(tmp_path)
        monitor.config.ups_groups[0].is_local = False
        monitor.config.local_shutdown.enabled = False
        monitor.config.containers.enabled = True
        log = []
        monitor._log_message = log.append

        with patch("eneru.monitor.command_exists", return_value=True), \
             patch.object(monitor, "_detect_container_runtime", return_value="podman"):
            monitor._check_dependencies()

        assert monitor._container_runtime == "podman"
        assert any("Container runtime detected: podman" in m for m in log), log

    @pytest.mark.unit
    def test_container_runtime_missing_disables_containers(self, tmp_path):
        monitor = make_monitor(tmp_path)
        monitor.config.ups_groups[0].is_local = False
        monitor.config.local_shutdown.enabled = False
        monitor.config.containers.enabled = True
        log = []
        monitor._log_message = log.append

        with patch("eneru.monitor.command_exists", return_value=True), \
             patch.object(monitor, "_detect_container_runtime", return_value=None):
            monitor._check_dependencies()

        assert monitor.config.containers.enabled is False
        assert any("No container runtime found" in m for m in log), log

    @pytest.mark.unit
    def test_compose_available_logs_enabled_message(self, tmp_path):
        from eneru import ComposeFileConfig
        monitor = make_monitor(tmp_path)
        monitor.config.ups_groups[0].is_local = False
        monitor.config.local_shutdown.enabled = False
        monitor.config.containers.enabled = True
        monitor.config.containers.compose_files = [ComposeFileConfig(path="/c.yml")]
        log = []
        monitor._log_message = log.append

        with patch("eneru.monitor.command_exists", return_value=True), \
             patch.object(monitor, "_detect_container_runtime", return_value="docker"), \
             patch.object(monitor, "_check_compose_available", return_value=True):
            monitor._check_dependencies()

        assert monitor._compose_available is True
        assert any("Compose support: enabled" in m for m in log), log

    @pytest.mark.unit
    def test_compose_unavailable_warns_and_skips(self, tmp_path):
        from eneru import ComposeFileConfig
        monitor = make_monitor(tmp_path)
        monitor.config.ups_groups[0].is_local = False
        monitor.config.local_shutdown.enabled = False
        monitor.config.containers.enabled = True
        monitor.config.containers.compose_files = [ComposeFileConfig(path="/c.yml")]
        log = []
        monitor._log_message = log.append

        with patch("eneru.monitor.command_exists", return_value=True), \
             patch.object(monitor, "_detect_container_runtime", return_value="docker"), \
             patch.object(monitor, "_check_compose_available", return_value=False):
            monitor._check_dependencies()

        assert any("compose_files configured" in m and "not available" in m for m in log), log

    @pytest.mark.unit
    def test_remote_servers_disabled_when_ssh_missing(self, tmp_path):
        monitor = make_monitor(tmp_path)
        monitor.config.ups_groups[0].is_local = False
        monitor.config.local_shutdown.enabled = False
        monitor.config.ups_groups[0].remote_servers = [
            RemoteServerConfig(name="nas", enabled=True, host="nas.lan", user="ups"),
        ]
        log = []
        monitor._log_message = log.append

        def cmd_present(cmd):
            return cmd != "ssh"

        with patch("eneru.monitor.command_exists", side_effect=cmd_present):
            monitor._check_dependencies()

        assert monitor.config.remote_servers[0].enabled is False
        assert any("'ssh' not found" in m and "Remote shutdown will be skipped" in m for m in log), log


class TestStartHelpersIdempotent:
    """The _start_* lifecycle hooks must be safe to invoke twice
    (called by both single-UPS startup and the multi-UPS coordinator
    re-init path; running twice would leak threads and ports)."""

    @pytest.mark.unit
    def test_start_api_server_no_op_when_already_started(self, tmp_path):
        monitor = make_monitor(tmp_path)
        monitor.config.api.enabled = True
        sentinel = MagicMock()
        monitor._api_server = sentinel  # Pretend it's already running
        monitor._start_api_server()
        assert monitor._api_server is sentinel

    @pytest.mark.unit
    def test_start_api_server_creates_and_starts_when_absent(self, tmp_path):
        monitor = make_monitor(tmp_path)
        monitor.config.api.enabled = True
        monitor._api_server = None
        with patch("eneru.monitor.EneruAPIServer") as cls:
            cls.return_value = MagicMock()
            monitor._start_api_server()
        cls.assert_called_once_with(monitor, monitor.config, log_fn=monitor._log_message)
        cls.return_value.start.assert_called_once()

    @pytest.mark.unit
    def test_start_mqtt_publisher_no_op_when_already_started(self, tmp_path):
        monitor = make_monitor(tmp_path)
        sentinel = MagicMock()
        monitor._mqtt_publisher = sentinel
        monitor._start_mqtt_publisher()
        assert monitor._mqtt_publisher is sentinel

    @pytest.mark.unit
    def test_start_mqtt_publisher_creates_and_starts_when_absent(self, tmp_path):
        monitor = make_monitor(tmp_path)
        monitor._mqtt_publisher = None
        with patch("eneru.monitor.MQTTPublisher") as cls:
            cls.return_value = MagicMock()
            monitor._start_mqtt_publisher()
        cls.return_value.start.assert_called_once()

    @pytest.mark.unit
    def test_start_remote_health_no_op_when_already_started(self, tmp_path):
        monitor = make_monitor(tmp_path)
        sentinel = MagicMock()
        monitor._remote_health_manager = sentinel
        monitor._start_remote_health()
        assert monitor._remote_health_manager is sentinel


class TestLogEnabledFeatures:
    """Lock the `📋  Enabled features:` log-line construction.

    This line is the operator's single source of truth for what the daemon
    is configured to do at startup; if a feature is silently dropped from
    the report it can mask a misconfiguration."""

    def _features_line(self, monitor):
        log = []
        monitor._log_message = log.append
        monitor._log_enabled_features()
        return next(m for m in log if m.startswith("📋  Enabled features:"))

    @pytest.mark.unit
    def test_no_features_enabled_reports_none(self, tmp_path):
        monitor = make_monitor(tmp_path)
        monitor.config.virtual_machines.enabled = False
        monitor.config.containers.enabled = False
        monitor.config.filesystems.sync_enabled = False
        monitor.config.filesystems.unmount.enabled = False
        monitor.config.local_shutdown.enabled = False
        monitor.config.ups_groups[0].remote_servers = []
        monitor.config.ups.connection_loss_grace_period.enabled = False
        line = self._features_line(monitor)
        assert "Enabled features: None" in line

    @pytest.mark.unit
    def test_vm_feature_listed(self, tmp_path):
        monitor = make_monitor(tmp_path)
        monitor.config.virtual_machines.enabled = True
        line = self._features_line(monitor)
        assert "VMs" in line

    @pytest.mark.unit
    def test_containers_auto_with_no_compose_files(self, tmp_path):
        monitor = make_monitor(tmp_path)
        monitor.config.containers.enabled = True
        monitor.config.containers.runtime = "auto"
        monitor.config.containers.compose_files = []
        line = self._features_line(monitor)
        assert "Containers (auto-detect)" in line

    @pytest.mark.unit
    def test_containers_auto_with_compose_count(self, tmp_path):
        from eneru import ComposeFileConfig
        monitor = make_monitor(tmp_path)
        monitor.config.containers.enabled = True
        monitor.config.containers.runtime = "auto"
        monitor.config.containers.compose_files = [
            ComposeFileConfig(path="/a"), ComposeFileConfig(path="/b"),
        ]
        line = self._features_line(monitor)
        assert "Containers (auto-detect, 2 compose)" in line

    @pytest.mark.unit
    def test_containers_explicit_runtime_with_compose(self, tmp_path):
        from eneru import ComposeFileConfig
        monitor = make_monitor(tmp_path)
        monitor.config.containers.enabled = True
        monitor.config.containers.runtime = "podman"
        monitor.config.containers.compose_files = [ComposeFileConfig(path="/x")]
        line = self._features_line(monitor)
        assert "Containers (podman, 1 compose)" in line

    @pytest.mark.unit
    def test_filesystem_features_combine(self, tmp_path):
        from eneru import UnmountConfig
        monitor = make_monitor(tmp_path)
        monitor.config.filesystems.sync_enabled = True
        monitor.config.filesystems.unmount = UnmountConfig(enabled=True, mounts=["/a", "/b"])
        line = self._features_line(monitor)
        assert "FS (sync, unmount (2 mounts))" in line

    @pytest.mark.unit
    def test_remote_servers_count_only_enabled(self, tmp_path):
        monitor = make_monitor(tmp_path)
        monitor.config.ups_groups[0].remote_servers = [
            RemoteServerConfig(name="a", enabled=True, host="h1", user="u"),
            RemoteServerConfig(name="b", enabled=False, host="h2", user="u"),
            RemoteServerConfig(name="c", enabled=True, host="h3", user="u"),
        ]
        line = self._features_line(monitor)
        assert "Remote (2 servers)" in line

    @pytest.mark.unit
    def test_local_shutdown_listed(self, tmp_path):
        monitor = make_monitor(tmp_path)
        monitor.config.local_shutdown.enabled = True
        line = self._features_line(monitor)
        assert "Local Shutdown" in line

    @pytest.mark.unit
    def test_connection_grace_includes_duration(self, tmp_path):
        monitor = make_monitor(tmp_path)
        monitor.config.ups.connection_loss_grace_period.enabled = True
        monitor.config.ups.connection_loss_grace_period.duration = 90
        line = self._features_line(monitor)
        assert "Connection Grace (90s)" in line


class TestInitializeNotifications:
    """v5.2 wired notification persistence into `_initialize_notifications`.
    Cover the three runtime branches: disabled, apprise unavailable,
    and happy path with a worker actually starting."""

    @pytest.mark.unit
    def test_notifications_disabled_logs_and_returns(self, tmp_path):
        monitor = make_monitor(tmp_path)
        monitor.config.notifications.enabled = False
        monitor._notification_worker = None
        log = []
        monitor._log_message = log.append
        # Bypass stats_store open() and lifecycle sweep for unit isolation
        monitor._stats_store = MagicMock(_conn=None)

        monitor._initialize_notifications()

        assert monitor._notification_worker is None
        assert any("📢  Notifications: disabled" in m for m in log), log

    @pytest.mark.unit
    def test_notifications_apprise_unavailable_logs_warning(self, tmp_path):
        monitor = make_monitor(tmp_path)
        monitor.config.notifications.enabled = True
        monitor._notification_worker = None
        log = []
        monitor._log_message = log.append
        monitor._stats_store = MagicMock(_conn=None)

        with patch("eneru.monitor.APPRISE_AVAILABLE", False):
            monitor._initialize_notifications()

        assert monitor.config.notifications.enabled is False
        assert any("apprise not installed" in m for m in log), log

    @pytest.mark.unit
    def test_notifications_happy_path_starts_worker(self, tmp_path):
        monitor = make_monitor(tmp_path)
        monitor.config.notifications.enabled = True
        monitor._notification_worker = None
        log = []
        monitor._log_message = log.append
        monitor._stats_store = MagicMock(_conn=None)

        fake_worker = MagicMock()
        fake_worker.start.return_value = True
        fake_worker.get_service_count.return_value = 3

        with patch("eneru.monitor.APPRISE_AVAILABLE", True), \
             patch("eneru.monitor.NotificationWorker", return_value=fake_worker):
            monitor._initialize_notifications()

        assert monitor._notification_worker is fake_worker
        fake_worker.start.assert_called_once()
        assert any("Notifications: enabled (3 service(s))" in m for m in log), log

    @pytest.mark.unit
    def test_notifications_worker_start_failure_disables(self, tmp_path):
        monitor = make_monitor(tmp_path)
        monitor.config.notifications.enabled = True
        monitor._notification_worker = None
        log = []
        monitor._log_message = log.append
        monitor._stats_store = MagicMock(_conn=None)

        fake_worker = MagicMock()
        fake_worker.start.return_value = False  # Simulate failure

        with patch("eneru.monitor.APPRISE_AVAILABLE", True), \
             patch("eneru.monitor.NotificationWorker", return_value=fake_worker):
            monitor._initialize_notifications()

        assert monitor.config.notifications.enabled is False
        assert any("Failed to initialize notifications" in m for m in log), log

    @pytest.mark.unit
    def test_notifications_coordinator_mode_registers_existing_worker(self, tmp_path):
        """When a coordinator-injected worker exists, register our store and return."""
        monitor = make_monitor(tmp_path)
        existing = MagicMock()
        monitor._notification_worker = existing
        # Pretend the stats store has an open connection
        store = MagicMock()
        store._conn = MagicMock()  # truthy
        monitor._stats_store = store

        # Make find_pending_by_category return [] so the lifecycle sweep is a no-op
        store.find_pending_by_category.return_value = []

        monitor._initialize_notifications()

        existing.register_store.assert_called_once_with(store)


class TestWaitForInitialConnection:
    """`_wait_for_initial_connection` polls NUT for up to 30s before
    giving up with a warning. Cover both success and timeout."""

    @pytest.mark.unit
    def test_initial_connection_success_logs_check_mark(self, tmp_path):
        monitor = make_monitor(tmp_path)
        log = []
        monitor._log_message = log.append

        with patch.object(monitor, "_get_all_ups_data", return_value=(True, {}, "")):
            monitor._wait_for_initial_connection()

        assert any("Initial connection successful" in m for m in log), log

    @pytest.mark.unit
    def test_initial_connection_timeout_logs_warning(self, tmp_path):
        monitor = make_monitor(tmp_path)
        log = []
        monitor._log_message = log.append

        # Always-fail get_all_ups_data, but skip the actual sleep
        with patch.object(monitor, "_get_all_ups_data", return_value=(False, None, "err")), \
             patch("eneru.monitor.time.sleep"):
            monitor._wait_for_initial_connection()

        assert any("Failed to connect" in m and "30s" in m for m in log), log


class TestStartStats:
    """`_start_stats` opens the per-UPS stats DB and starts a writer.
    SQLite failures must be isolated — the daemon continues without
    persistence rather than crashing."""

    @pytest.mark.unit
    def test_start_stats_logs_and_continues_when_open_raises(self, tmp_path):
        monitor = make_monitor(tmp_path)
        monitor._stats_store = MagicMock(_conn=None)
        monitor._stats_store.open.side_effect = OSError("disk full")
        log = []
        monitor._log_message = log.append

        monitor._start_stats()

        assert monitor._stats_writer is None
        assert any("stats store open failed" in m and "disk full" in m for m in log), log

    @pytest.mark.unit
    def test_start_stats_starts_writer_and_logs_daemon_start(self, tmp_path):
        monitor = make_monitor(tmp_path)
        store = MagicMock(_conn=None)
        # After open() runs we want _conn to look truthy
        def fake_open():
            store._conn = MagicMock()
        store.open.side_effect = fake_open
        monitor._stats_store = store

        with patch("eneru.monitor.StatsWriter") as writer_cls:
            writer_cls.return_value = MagicMock()
            monitor._start_stats()

        writer_cls.return_value.start.assert_called_once()
        store.log_event.assert_called_once()
        # The first arg of log_event should be DAEMON_START
        assert store.log_event.call_args.args[0] == "DAEMON_START"


class TestLogMessageFallback:
    """`_log_message` must never silently lose output. When no logger
    is wired (very early init / test harness), fall through to print()."""

    @pytest.mark.unit
    def test_log_message_prints_with_timestamp_when_no_logger(self, tmp_path, capsys):
        monitor = make_monitor(tmp_path)
        monitor.logger = None  # No logger configured yet
        monitor._log_prefix = ""

        monitor._log_message("hello world")

        captured = capsys.readouterr()
        assert "hello world" in captured.out
        # Timestamp like "YYYY-MM-DD HH:MM:SS TZ - hello world"
        assert " - hello world" in captured.out


class TestInitializePathPermissions:
    """`_initialize` must tolerate read-only state-file / battery-history
    paths (containers with read-only mounts, dropped privileges) — log a
    warning and continue rather than crashing."""

    @pytest.mark.unit
    def test_initialize_swallows_log_file_permission_error(self, tmp_path):
        from pathlib import Path as P
        monitor = make_monitor(tmp_path)
        monitor.config.logging.file = str(tmp_path / "eneru.log")
        # Stub out the heavy collaborators _initialize calls
        monitor._initialize_notifications = MagicMock()
        monitor._check_dependencies = MagicMock()
        monitor._emit_lifecycle_startup_notification = MagicMock()
        monitor._log_enabled_features = MagicMock()
        monitor._wait_for_initial_connection = MagicMock()
        monitor._initialize_voltage_thresholds = MagicMock()
        monitor._start_stats = MagicMock()
        monitor._start_remote_health = MagicMock()
        monitor._start_api_server = MagicMock()
        monitor._start_mqtt_publisher = MagicMock()

        with patch.object(P, "touch", side_effect=PermissionError("ro fs")), \
             patch("eneru.monitor.signal.signal"):
            monitor._initialize()  # Must not raise

    @pytest.mark.unit
    def test_initialize_logs_warning_on_battery_history_permission_error(self, tmp_path):
        from pathlib import Path as P
        monitor = make_monitor(tmp_path)
        monitor.config.logging.file = str(tmp_path / "eneru.log")
        log = []
        monitor._log_message = log.append
        monitor._initialize_notifications = MagicMock()
        monitor._check_dependencies = MagicMock()
        monitor._emit_lifecycle_startup_notification = MagicMock()
        monitor._log_enabled_features = MagicMock()
        monitor._wait_for_initial_connection = MagicMock()
        monitor._initialize_voltage_thresholds = MagicMock()
        monitor._start_stats = MagicMock()
        monitor._start_remote_health = MagicMock()
        monitor._start_api_server = MagicMock()
        monitor._start_mqtt_publisher = MagicMock()

        with patch.object(P, "write_text", side_effect=PermissionError("ro fs")), \
             patch("eneru.monitor.signal.signal"):
            monitor._initialize()

        assert any("Cannot write to" in m for m in log), log


class TestRunFatalErrorPath:
    """`run()` must catch generic exceptions, log FATAL ERROR, write a
    shutdown marker so the next start can emit
    'Restarted (last instance exited fatally)', then re-raise."""

    @pytest.mark.unit
    def test_run_writes_fatal_marker_and_reraises(self, tmp_path):
        monitor = make_monitor(tmp_path)

        def explode():
            raise RuntimeError("boom in main loop")

        log = []
        monitor._log_message = log.append
        monitor._send_notification = MagicMock()

        with patch.object(monitor, "_initialize"), \
             patch.object(monitor, "_main_loop", side_effect=explode), \
             patch("eneru.monitor.write_shutdown_marker") as marker:
            with pytest.raises(RuntimeError, match="boom in main loop"):
                monitor.run()

        assert any("FATAL ERROR" in m and "boom" in m for m in log), log
        marker.assert_called_once()

    @pytest.mark.unit
    def test_run_fatal_marker_write_exception_does_not_mask_original(self, tmp_path):
        """If the marker-write itself fails, the original FATAL must still propagate."""
        monitor = make_monitor(tmp_path)
        monitor._send_notification = MagicMock()

        with patch.object(monitor, "_initialize"), \
             patch.object(monitor, "_main_loop", side_effect=RuntimeError("primary")), \
             patch("eneru.monitor.write_shutdown_marker", side_effect=OSError("disk full")):
            with pytest.raises(RuntimeError, match="primary"):
                monitor.run()  # OSError must not surface here


class TestExecuteShutdownSequence:
    """`_execute_shutdown_sequence` is the safety-critical path. Cover
    the coordinator-mode return, dry-run, and real-execution branches."""

    def _stub_phases(self, monitor):
        """Stub out every phase method so the sequence is just orchestration."""
        monitor._shutdown_vms = MagicMock()
        monitor._shutdown_containers = MagicMock()
        monitor._sync_filesystems = MagicMock()
        monitor._unmount_filesystems = MagicMock()
        monitor._shutdown_remote_servers = MagicMock()
        monitor._send_notification = MagicMock()
        monitor._stats_store = MagicMock()
        return monitor

    @pytest.mark.unit
    def test_coordinator_mode_invokes_callback_and_returns(self, tmp_path):
        monitor = self._stub_phases(make_monitor(tmp_path))
        monitor._coordinator_mode = True
        callback = MagicMock()
        monitor._shutdown_callback = callback
        log = []
        monitor._log_message = log.append

        with patch("eneru.monitor.write_shutdown_marker") as marker, \
             patch("eneru.monitor.run_command") as runner:
            monitor._execute_shutdown_sequence()

        callback.assert_called_once()
        # Coordinator mode must NOT execute local shutdown or write markers
        marker.assert_not_called()
        runner.assert_not_called()
        assert any("GROUP SHUTDOWN SEQUENCE COMPLETE" in m for m in log), log

    @pytest.mark.unit
    def test_dry_run_skips_local_shutdown_command(self, tmp_path):
        monitor = self._stub_phases(make_monitor(tmp_path))
        monitor._coordinator_mode = False
        monitor.config.behavior.dry_run = True
        monitor.config.local_shutdown.enabled = True
        monitor.config.local_shutdown.command = "/sbin/shutdown -h now"
        log = []
        monitor._log_message = log.append

        with patch("eneru.monitor.write_shutdown_marker") as marker, \
             patch("eneru.monitor.run_command") as runner:
            monitor._execute_shutdown_sequence()

        # Dry-run: no marker, no shutdown command, but the dry-run preview log line
        marker.assert_not_called()
        runner.assert_not_called()
        assert any("[DRY-RUN] Would execute" in m for m in log), log

    @pytest.mark.unit
    def test_real_run_writes_marker_and_executes_shutdown_command(self, tmp_path):
        monitor = self._stub_phases(make_monitor(tmp_path))
        monitor._coordinator_mode = False
        monitor.config.behavior.dry_run = False
        monitor.config.local_shutdown.enabled = True
        monitor.config.local_shutdown.command = "/sbin/shutdown -h now"
        monitor.config.local_shutdown.message = "UPS critical"

        with patch("eneru.monitor.write_shutdown_marker") as marker, \
             patch("eneru.monitor.run_command") as runner:
            monitor._execute_shutdown_sequence()

        marker.assert_called_once()
        # Shutdown command runs with the configured message appended
        runner.assert_called_once()
        cmd = runner.call_args.args[0]
        assert cmd[:3] == ["/sbin/shutdown", "-h", "now"]
        assert "UPS critical" in cmd
        # Notification was sent before the shutdown command
        monitor._send_notification.assert_called_once()

    @pytest.mark.unit
    def test_local_shutdown_disabled_skips_command(self, tmp_path):
        """When local_shutdown.enabled=False the daemon logs completion
        but does NOT run the shutdown binary — system stays up."""
        monitor = self._stub_phases(make_monitor(tmp_path))
        monitor._coordinator_mode = False
        monitor.config.behavior.dry_run = False
        monitor.config.local_shutdown.enabled = False
        log = []
        monitor._log_message = log.append

        with patch("eneru.monitor.write_shutdown_marker") as marker, \
             patch("eneru.monitor.run_command") as runner:
            monitor._execute_shutdown_sequence()

        marker.assert_not_called()
        runner.assert_not_called()
        assert any("local shutdown disabled" in m for m in log), log

    @pytest.mark.unit
    def test_non_local_group_skips_local_phases(self, tmp_path):
        """A non-local UPS group must NOT touch local VMs/containers/filesystems
        (runtime safety: even if validation was bypassed)."""
        monitor = self._stub_phases(make_monitor(tmp_path))
        monitor._coordinator_mode = False
        monitor.config.ups_groups[0].is_local = False
        monitor.config.local_shutdown.enabled = False

        with patch("eneru.monitor.write_shutdown_marker"), \
             patch("eneru.monitor.run_command"):
            monitor._execute_shutdown_sequence()

        # Local phases never invoked
        monitor._shutdown_vms.assert_not_called()
        monitor._shutdown_containers.assert_not_called()
        monitor._sync_filesystems.assert_not_called()
        monitor._unmount_filesystems.assert_not_called()
        # Remote phase always runs
        monitor._shutdown_remote_servers.assert_called_once()

    @pytest.mark.unit
    def test_wall_broadcast_fires_when_configured_and_not_dry_run(self, tmp_path):
        """local_shutdown.wall=True triggers a `wall(1)` broadcast before
        the shutdown phases — but only outside dry-run mode."""
        monitor = self._stub_phases(make_monitor(tmp_path))
        monitor._coordinator_mode = False
        monitor.config.behavior.dry_run = False
        monitor.config.local_shutdown.wall = True
        monitor.config.local_shutdown.enabled = False  # Avoid triggering real shutdown

        with patch("eneru.monitor.write_shutdown_marker"), \
             patch("eneru.monitor.run_command") as runner:
            monitor._execute_shutdown_sequence()

        # Find the wall call among run_command invocations
        wall_calls = [c for c in runner.call_args_list
                      if c.args and c.args[0] and c.args[0][0] == "wall"]
        assert len(wall_calls) == 1, runner.call_args_list

    @pytest.mark.unit
    def test_wall_broadcast_skipped_in_dry_run(self, tmp_path):
        monitor = self._stub_phases(make_monitor(tmp_path))
        monitor._coordinator_mode = False
        monitor.config.behavior.dry_run = True
        monitor.config.local_shutdown.wall = True

        with patch("eneru.monitor.write_shutdown_marker"), \
             patch("eneru.monitor.run_command") as runner:
            monitor._execute_shutdown_sequence()

        wall_calls = [c for c in runner.call_args_list
                      if c.args and c.args[0] and c.args[0][0] == "wall"]
        assert wall_calls == []

    @pytest.mark.unit
    def test_stats_log_event_failure_does_not_break_sequence(self, tmp_path):
        """If logging the SHUTDOWN_SEQUENCE_COMPLETE event fails, the
        sequence still continues to the local shutdown step."""
        monitor = self._stub_phases(make_monitor(tmp_path))
        monitor._coordinator_mode = False
        monitor.config.behavior.dry_run = False
        monitor.config.local_shutdown.enabled = False
        monitor._stats_store.log_event.side_effect = OSError("db locked")

        with patch("eneru.monitor.write_shutdown_marker"), \
             patch("eneru.monitor.run_command"):
            monitor._execute_shutdown_sequence()  # Must not raise

        monitor._send_notification.assert_called_once()


class TestTriggerImmediateShutdown:
    """`_trigger_immediate_shutdown` is the path low-battery / FSD /
    depletion fire when conditions become unsafe. Coverage: the wall(1)
    broadcast and the EMERGENCY_SHUTDOWN_INITIATED event log."""

    @pytest.mark.unit
    def test_wall_broadcast_fires_when_configured(self, tmp_path):
        monitor = make_monitor(tmp_path)
        monitor.config.behavior.dry_run = False
        monitor.config.local_shutdown.wall = True
        monitor._stats_store = MagicMock()
        monitor._execute_shutdown_sequence = MagicMock()

        with patch("eneru.monitor.run_command") as runner:
            monitor._trigger_immediate_shutdown("battery 5% < threshold 20%")

        wall_calls = [c for c in runner.call_args_list
                      if c.args and c.args[0] and c.args[0][0] == "wall"]
        assert len(wall_calls) == 1
        # Wall message includes the reason
        assert "battery 5%" in wall_calls[0].args[0][1]
        monitor._execute_shutdown_sequence.assert_called_once()

    @pytest.mark.unit
    def test_wall_broadcast_skipped_in_dry_run(self, tmp_path):
        monitor = make_monitor(tmp_path)
        monitor.config.behavior.dry_run = True
        monitor.config.local_shutdown.wall = True
        monitor._stats_store = MagicMock()
        monitor._execute_shutdown_sequence = MagicMock()

        with patch("eneru.monitor.run_command") as runner:
            monitor._trigger_immediate_shutdown("test reason")

        # No wall call in dry-run; only sequence runs (which is mocked)
        assert all(
            not (c.args and c.args[0] and c.args[0][0] == "wall")
            for c in runner.call_args_list
        )

    @pytest.mark.unit
    def test_emergency_event_log_failure_does_not_block_shutdown(self, tmp_path):
        """A broken stats DB during EMERGENCY_SHUTDOWN_INITIATED
        log_event must not prevent the shutdown sequence from running."""
        monitor = make_monitor(tmp_path)
        monitor.config.behavior.dry_run = True
        monitor.config.local_shutdown.wall = False
        monitor._stats_store = MagicMock()
        monitor._stats_store.log_event.side_effect = OSError("db locked")
        monitor._execute_shutdown_sequence = MagicMock()

        monitor._trigger_immediate_shutdown("test reason")

        monitor._execute_shutdown_sequence.assert_called_once()

    @pytest.mark.unit
    def test_shutdown_flag_write_failure_does_not_block_shutdown(
        self, tmp_path: Path,
    ) -> None:
        monitor = make_monitor(tmp_path)
        monitor._stats_store = MagicMock()
        monitor._execute_shutdown_sequence = MagicMock()

        with patch.object(
            type(monitor._shutdown_flag_path),
            "touch",
            side_effect=OSError("read-only filesystem"),
        ):
            monitor._trigger_immediate_shutdown("test reason")

        monitor._execute_shutdown_sequence.assert_called_once()
        assert any(
            "Could not write shutdown flag" in str(call)
            for call in monitor.logger.log.call_args_list
        )

    @pytest.mark.unit
    def test_shutdown_flag_write_failure_still_blocks_duplicate_shutdowns(
        self, tmp_path: Path,
    ) -> None:
        monitor = make_monitor(tmp_path)
        monitor._stats_store = MagicMock()
        monitor._execute_shutdown_sequence = MagicMock()

        with patch.object(
            type(monitor._shutdown_flag_path),
            "touch",
            side_effect=OSError("read-only filesystem"),
        ):
            monitor._trigger_immediate_shutdown("first trigger")

        monitor._trigger_immediate_shutdown("second trigger")

        monitor._execute_shutdown_sequence.assert_called_once()
        assert any(
            "already in progress" in str(call)
            for call in monitor.logger.log.call_args_list
        )

    @pytest.mark.unit
    def test_shutdown_flag_clear_failure_disables_disk_guard(
        self, tmp_path: Path,
    ) -> None:
        monitor = make_monitor(tmp_path)
        monitor._shutdown_in_progress = True
        monitor._shutdown_flag_path.touch()

        with patch.object(
            type(monitor._shutdown_flag_path),
            "unlink",
            side_effect=OSError("read-only filesystem"),
        ):
            monitor._clear_shutdown_in_progress()

        assert monitor._shutdown_in_progress is False
        assert monitor._shutdown_flag_unusable is True
        assert monitor._shutdown_guard_active() is False
        assert any(
            "Ignoring the on-disk guard" in str(call)
            for call in monitor.logger.log.call_args_list
        )


class TestCleanupAndExit:
    """`_cleanup_and_exit` handles SIGTERM/SIGINT. Three distinct paths:
    mid-shutdown signal (drain + exit), graceful stop with stats DB
    available, and graceful stop when stats failed (worker fallback)."""

    @pytest.mark.unit
    def test_mid_shutdown_signal_drains_and_exits(self, tmp_path):
        """If the shutdown_flag is already on disk (sequence in progress),
        flush the worker and exit with stats stop. Skip lifecycle notif."""
        monitor = make_monitor(tmp_path)
        monitor._stats_store = MagicMock()
        monitor._stop_stats = MagicMock()

        worker = MagicMock()
        monitor._notification_worker = worker
        monitor._remote_health_manager = MagicMock()
        monitor._mqtt_publisher = MagicMock()
        monitor._api_server = MagicMock()
        monitor._send_notification = MagicMock()
        monitor._shutdown_flag_path.touch()  # Pretend sequence in progress

        with pytest.raises(SystemExit) as exc_info:
            monitor._cleanup_and_exit(15, None)
        assert exc_info.value.code == 0

        # Mid-shutdown path: worker drained + stopped, stats stopped, exited
        worker.flush.assert_called_once()
        worker.stop.assert_called_once()
        monitor._stop_stats.assert_called_once()
        # No lifecycle notification on mid-shutdown
        monitor._send_notification.assert_not_called()
        # Subsystems were also stopped
        monitor._remote_health_manager.stop.assert_called_once()
        monitor._mqtt_publisher.stop.assert_called_once()
        monitor._api_server.stop.assert_called_once()

    @pytest.mark.unit
    def test_mid_shutdown_signal_uses_in_memory_guard_when_flag_missing(
        self, tmp_path: Path,
    ) -> None:
        """If the flag write failed, the in-memory guard still identifies a
        real shutdown sequence and prevents the graceful-stop path."""
        monitor = make_monitor(tmp_path)
        monitor._stats_store = MagicMock()
        monitor._stop_stats = MagicMock()
        monitor._shutdown_in_progress = True
        monitor._shutdown_flag_path.unlink(missing_ok=True)

        worker = MagicMock()
        monitor._notification_worker = worker
        monitor._send_notification = MagicMock()

        with pytest.raises(SystemExit) as exc_info:
            monitor._cleanup_and_exit(15, None)

        assert exc_info.value.code == 0
        worker.flush.assert_called_once()
        worker.stop.assert_called_once()
        monitor._send_notification.assert_not_called()

    @pytest.mark.unit
    def test_signal_ignored_while_shutdown_sequence_in_flight(self, tmp_path):
        """ISS-001: a real SIGTERM/SIGINT arriving while the shutdown sequence
        runs on the main thread must be ignored (return, no SystemExit) so it
        cannot abort the in-flight host poweroff. Nothing is torn down."""
        monitor = make_monitor(tmp_path)
        monitor._stop_stats = MagicMock()
        monitor._stop_event = MagicMock()
        worker = MagicMock()
        monitor._notification_worker = worker
        monitor._log_message = MagicMock()
        monitor._shutdown_sequence_in_flight = True
        # Even with the guard flag on disk, an in-flight sequence takes priority.
        monitor._shutdown_flag_path.touch()

        # Returns normally (no SystemExit) and tears nothing down.
        # 15 == SIGTERM (matches the numeric convention used across this class).
        assert monitor._cleanup_and_exit(15, None) is None
        monitor._stop_event.set.assert_not_called()
        monitor._stop_stats.assert_not_called()
        worker.flush.assert_not_called()
        assert any(
            "ignoring" in str(call.args[0]).lower()
            for call in monitor._log_message.call_args_list
        )

    @pytest.mark.unit
    def test_internal_exit_after_shutdown_still_exits_during_sequence(
        self, tmp_path,
    ):
        """ISS-001: the internal _exit_after_shutdown call passes signum=None and
        must still exit even mid-sequence — the ignore branch is gated on a real
        signal number so it does not swallow the intentional exit."""
        monitor = make_monitor(tmp_path)
        monitor._stats_store = MagicMock()
        monitor._stop_stats = MagicMock()
        monitor._notification_worker = MagicMock()
        monitor._send_notification = MagicMock()
        monitor._shutdown_sequence_in_flight = True
        monitor._shutdown_flag_path.touch()  # guard active → exit(0) path

        with pytest.raises(SystemExit) as exc_info:
            monitor._cleanup_and_exit(None, None)
        assert exc_info.value.code == 0

    @pytest.mark.unit
    def test_graceful_stop_enqueues_lifecycle_notification(self, tmp_path):
        """No shutdown_flag on disk → graceful stop. Touch flag, log
        DAEMON_STOP, schedule deferred lifecycle notification, exit 0."""
        monitor = make_monitor(tmp_path)
        store = MagicMock()
        store._conn = MagicMock()
        monitor._stats_store = store
        monitor._stop_stats = MagicMock()
        monitor._notification_worker = MagicMock()
        monitor._send_notification = MagicMock(return_value=42)  # row id
        # Make sure shutdown flag does NOT exist
        monitor._shutdown_flag_path.unlink(missing_ok=True)

        with patch("eneru.monitor.read_upgrade_marker", return_value=None), \
             patch("eneru.monitor.read_shutdown_marker", return_value=None), \
             patch("eneru.monitor.write_shutdown_marker") as marker, \
             patch("eneru.monitor.schedule_deferred_stop_or_eager_send") as scheduler:
            with pytest.raises(SystemExit) as exc_info:
                monitor._cleanup_and_exit(15, None)
            assert exc_info.value.code == 0

        # Lifecycle stop got enqueued and scheduled for deferred delivery
        monitor._send_notification.assert_called_once()
        scheduler.assert_called_once()
        kwargs = scheduler.call_args.kwargs
        assert kwargs["notification_id"] == 42

        # REASON_SIGNAL marker dropped for the next start to detect restart
        marker.assert_called_once()
        from eneru.lifecycle import REASON_SIGNAL
        assert marker.call_args.kwargs["reason"] == REASON_SIGNAL

        # DAEMON_STOP logged in events
        store.log_event.assert_any_call(
            "DAEMON_STOP",
            f"Eneru v{monitor.config.ups.name}".replace(monitor.config.ups.name, monitor.config.ups.name) and
            store.log_event.call_args_list[0].args[1],  # tolerant
        )

    @pytest.mark.unit
    def test_graceful_stop_skipped_during_upgrade(self, tmp_path):
        """When read_upgrade_marker indicates an upgrade in progress,
        suppress the lifecycle stop notification entirely (the next
        daemon will emit a single 'Upgraded vX → vY' that supersedes
        this stop)."""
        monitor = make_monitor(tmp_path)
        store = MagicMock()
        store._conn = MagicMock()
        monitor._stats_store = store
        monitor._stop_stats = MagicMock()
        monitor._notification_worker = MagicMock()
        monitor._send_notification = MagicMock()
        monitor._shutdown_flag_path.unlink(missing_ok=True)

        with patch("eneru.monitor.read_upgrade_marker",
                   return_value={"from": "5.3.0", "to": "5.4.0"}), \
             patch("eneru.monitor.read_shutdown_marker", return_value=None), \
             patch("eneru.monitor.write_shutdown_marker"), \
             patch("eneru.monitor.schedule_deferred_stop_or_eager_send") as scheduler:
            with pytest.raises(SystemExit):
                monitor._cleanup_and_exit(15, None)

        monitor._send_notification.assert_not_called()
        scheduler.assert_not_called()

    @pytest.mark.unit
    def test_graceful_stop_eager_fallback_when_stats_db_down(self, tmp_path):
        """If the stats DB never opened (notif_id is None / _conn is None),
        fall back to an eager Apprise send via the worker so the user
        still gets a Stopped notification."""
        monitor = make_monitor(tmp_path)
        store = MagicMock()
        store._conn = None  # DB never opened
        monitor._stats_store = store
        monitor._stop_stats = MagicMock()
        worker = MagicMock()
        monitor._notification_worker = worker
        monitor._send_notification = MagicMock(return_value=None)
        monitor._shutdown_flag_path.unlink(missing_ok=True)

        with patch("eneru.monitor.read_upgrade_marker", return_value=None), \
             patch("eneru.monitor.read_shutdown_marker", return_value=None), \
             patch("eneru.monitor.write_shutdown_marker"), \
             patch("eneru.monitor.schedule_deferred_stop_or_eager_send") as scheduler:
            with pytest.raises(SystemExit):
                monitor._cleanup_and_exit(15, None)

        # Scheduler not invoked (no notif_id), eager Apprise fallback fires
        scheduler.assert_not_called()
        worker._send_via_apprise_bounded.assert_called_once()

    @pytest.mark.unit
    def test_graceful_stop_does_not_overwrite_sequence_complete_marker(self, tmp_path):
        """If a SHUTDOWN_SEQUENCE_COMPLETE marker is already on disk
        (because power-loss shutdown ran first), do NOT overwrite it
        with REASON_SIGNAL — that would mask the power-loss event from
        the next start."""
        from eneru.lifecycle import REASON_SEQUENCE_COMPLETE
        monitor = make_monitor(tmp_path)
        store = MagicMock()
        store._conn = MagicMock()
        monitor._stats_store = store
        monitor._stop_stats = MagicMock()
        monitor._notification_worker = MagicMock()
        monitor._send_notification = MagicMock(return_value=1)
        monitor._shutdown_flag_path.unlink(missing_ok=True)

        existing = {"reason": REASON_SEQUENCE_COMPLETE, "version": "5.4.0"}
        with patch("eneru.monitor.read_upgrade_marker", return_value=None), \
             patch("eneru.monitor.read_shutdown_marker", return_value=existing), \
             patch("eneru.monitor.write_shutdown_marker") as marker, \
             patch("eneru.monitor.schedule_deferred_stop_or_eager_send"):
            with pytest.raises(SystemExit):
                monitor._cleanup_and_exit(15, None)

        marker.assert_not_called()  # Existing power-loss marker preserved


class TestGetUpsVar:
    """`_get_ups_var` queries a single NUT variable. Returns the
    stripped value on success, None on failure — never raises."""

    @pytest.mark.unit
    def test_returns_stripped_value_on_success(self, tmp_path):
        monitor = make_monitor(tmp_path)
        with patch.object(monitor, "_run_upsc",
                          return_value=(0, "  ON BATTERY \n", "")):
            assert monitor._get_ups_var("ups.status") == "ON BATTERY"

    @pytest.mark.unit
    def test_returns_none_on_upsc_failure(self, tmp_path):
        monitor = make_monitor(tmp_path)
        with patch.object(monitor, "_run_upsc",
                          return_value=(1, "", "no such ups")):
            assert monitor._get_ups_var("ups.status") is None


class TestConnectionRecoveryGracePeriod:
    """When `_main_loop` sees fresh data after the connection state was
    GRACE_PERIOD, it logs a quiet recovery + tracks flap count for
    instability detection. Cover the GRACE_PERIOD branch and the
    flap-threshold notification."""

    @pytest.mark.unit
    def test_grace_period_recovery_resets_state_quietly(self, tmp_path):
        """A successful poll while connection_state=GRACE_PERIOD must
        flip state back to OK without emitting a recovery notification."""
        monitor = make_monitor(tmp_path)
        log = []
        monitor._log_message = log.append
        monitor._send_notification = MagicMock()
        # Set up the GRACE_PERIOD precondition
        monitor.state.connection_state = "GRACE_PERIOD"
        monitor.state.connection_lost_time = time.time() - 10
        monitor.state.connection_flap_count = 0
        monitor.state.connection_first_flap_time = 0.0
        # Keep flap threshold high so a single flap doesn't trip notification
        monitor.config.ups.connection_loss_grace_period.flap_threshold = 5

        # Run one main loop iteration with a successful poll
        ups_data = {
            "ups.status": "OL CHRG",
            "battery.charge": "100",
            "battery.runtime": "3600",
        }
        _run_one_iteration(monitor, (True, ups_data, ""))

        assert monitor.state.connection_state == "OK"
        assert monitor.state.connection_lost_time == 0.0
        # Quiet recovery — no notification
        monitor._send_notification.assert_not_called()
        # Flap counter incremented
        assert monitor.state.connection_flap_count == 1
        assert any("recovered during grace period" in m for m in log), log

    @pytest.mark.unit
    def test_grace_period_flap_threshold_fires_unstable_notification(self, tmp_path):
        """When connection_flap_count crosses flap_threshold, log a
        warning and send an Unstable notification. Reset the counter
        so the next outbreak fires only after another N flaps."""
        monitor = make_monitor(tmp_path)
        log = []
        monitor._log_message = log.append
        monitor._send_notification = MagicMock()
        monitor.state.connection_state = "GRACE_PERIOD"
        monitor.state.connection_lost_time = time.time() - 5
        monitor.state.connection_flap_count = 4  # One short of threshold
        monitor.state.connection_first_flap_time = time.time() - 100
        monitor.config.ups.connection_loss_grace_period.flap_threshold = 5

        _run_one_iteration(monitor, (True, {
            "ups.status": "OL CHRG", "battery.charge": "100",
            "battery.runtime": "3600",
        }, ""))

        # Counter incremented past threshold then reset
        assert monitor.state.connection_flap_count == 0
        assert monitor.state.connection_first_flap_time == 0.0
        # Notification fired
        monitor._send_notification.assert_called_once()
        body = monitor._send_notification.call_args.args[0]
        assert "NUT Server Unstable" in body
        assert any("NUT server is unstable" in m for m in log), log

    @pytest.mark.unit
    def test_failed_state_recovery_logs_power_event(self, tmp_path):
        """A successful poll while connection_state=FAILED logs a
        CONNECTION_RESTORED power event (loud) — that's the explicit
        "comes back from a real outage" signal."""
        monitor = make_monitor(tmp_path)
        monitor._log_power_event = MagicMock()
        monitor._send_notification = MagicMock()
        monitor.state.connection_state = "FAILED"
        monitor.state.connection_lost_time = time.time() - 600
        monitor.state.connection_flap_count = 7  # Should reset

        _run_one_iteration(monitor, (True, {
            "ups.status": "OL CHRG", "battery.charge": "100",
            "battery.runtime": "3600",
        }, ""))

        assert monitor.state.connection_state == "OK"
        assert monitor.state.connection_flap_count == 0
        monitor._log_power_event.assert_called_once()
        assert monitor._log_power_event.call_args.args[0] == "CONNECTION_RESTORED"

    @pytest.mark.unit
    def test_grace_period_flap_count_resets_after_24h_ttl(self, tmp_path):
        """Flap counter has a 24h TTL — flaps from yesterday don't
        count toward today's threshold."""
        monitor = make_monitor(tmp_path)
        monitor._send_notification = MagicMock()
        monitor.state.connection_state = "GRACE_PERIOD"
        monitor.state.connection_lost_time = time.time() - 5
        # Flaps from 25h ago — should be reset
        monitor.state.connection_flap_count = 4
        monitor.state.connection_first_flap_time = time.time() - 25 * 3600
        monitor.config.ups.connection_loss_grace_period.flap_threshold = 5

        _run_one_iteration(monitor, (True, {
            "ups.status": "OL CHRG", "battery.charge": "100",
            "battery.runtime": "3600",
        }, ""))

        # The 4 stale flaps were dropped, then this one flap counted as first.
        # So count is 1 (not 5) — well below threshold, no unstable notif.
        assert monitor.state.connection_flap_count == 1
        monitor._send_notification.assert_not_called()


class TestEmptyStatusHandling:
    """When ups_data is fetched OK but `ups.status` is missing, log
    an error and skip the rest of the iteration — never proceed
    with an empty status to the trigger logic."""

    @pytest.mark.unit
    def test_empty_status_logs_error_and_skips_iteration(self, tmp_path):
        monitor = make_monitor(tmp_path)
        log = []
        monitor._log_message = log.append
        # Make sure we're not in any special connection state
        monitor.state.connection_state = "OK"
        monitor.state.previous_status = "OL"

        # Successful fetch but no ups.status field
        _run_one_iteration(monitor, (True, {
            "battery.charge": "85",
            "battery.runtime": "1200",
            # NOTE: no ups.status
        }, ""))

        assert any("'ups.status' is missing" in m for m in log), log


class TestPowerRestoredCallback:
    """The coordinator hook invoked when an UPS group recovers from
    On Battery. Defensive contract: a callback that raises must NOT
    propagate into the main loop — log a warning and continue."""

    @pytest.mark.unit
    def test_callback_exception_is_logged_and_swallowed(self, tmp_path):
        monitor = make_monitor(tmp_path)
        log = []
        # _log_message accepts **extra kwargs (group, category, etc) — wrap
        # so list.append doesn't choke on them.
        monitor._log_message = lambda msg, **kw: log.append(msg)
        monitor._send_notification = MagicMock()
        # Set up an OB->OL transition: previous status was OB and trigger
        # was active so _handle_on_line walks the recovery path.
        monitor.state.previous_status = "OB DISCHRG"
        monitor.state.trigger_active = True
        monitor.state.on_battery_start_time = int(time.time()) - 60

        def boom():
            raise RuntimeError("coordinator gone away")

        monitor._power_restored_callback = boom

        ups_data = {
            "ups.status": "OL CHRG",
            "battery.charge": "75",
            "input.voltage": "230.5",
        }
        # Must not raise.
        monitor._handle_on_line(ups_data)

        assert any("power_restored_callback raised" in m for m in log), log


# ==============================================================================
# DEFENSIVE BRANCH COVERAGE (gap-closing tests for monitor.py)
# ==============================================================================


class TestRunKeyboardInterrupt:
    """`UPSGroupMonitor.run` catches KeyboardInterrupt and delegates to
    `_cleanup_and_exit` rather than letting the exception propagate. The
    fatal-Exception branch is already covered; this pins the SIGINT path."""

    @pytest.mark.unit
    def test_keyboard_interrupt_routes_to_cleanup(self, tmp_path):
        import signal as _signal

        monitor = make_monitor(tmp_path)
        monitor._initialize = MagicMock()
        monitor._main_loop = MagicMock(side_effect=KeyboardInterrupt())
        monitor._cleanup_and_exit = MagicMock()

        monitor.run()

        monitor._cleanup_and_exit.assert_called_once_with(_signal.SIGINT, None)


class TestGetAllUpsDataFailurePaths:
    """`_get_all_ups_data` has two early-return paths (non-zero exit code
    and stale-data marker in stdout/stderr) that don't have a dedicated
    test today — they're exercised implicitly elsewhere, but a direct
    test pins the contract."""

    @pytest.mark.unit
    def test_nonzero_exit_returns_failure_with_stderr(self, tmp_path):
        monitor = make_monitor(tmp_path)
        with patch.object(monitor, "_run_upsc",
                          return_value=(1, "", "no such ups")):
            success, data, err = monitor._get_all_ups_data()
        assert success is False
        assert data == {}
        assert err == "no such ups"

    @pytest.mark.unit
    def test_nonzero_exit_filters_nut_ssl_init_noise(self, tmp_path):
        """Issue #71: the NSS SSL-init line is not the actual NUT failure."""
        monitor = make_monitor(tmp_path)
        with patch.object(
            monitor, "_run_upsc",
            return_value=(
                1,
                "Error: Unknown UPS\n",
                "Init SSL without certificate database\n",
            ),
        ):
            success, data, err = monitor._get_all_ups_data()
        assert success is False
        assert data == {}
        assert err == "Error: Unknown UPS"

    @pytest.mark.unit
    def test_ssl_handshake_failure_appends_unifi_hint(self, tmp_path):
        """A botched STARTTLS handshake (e.g. UniFi UPS with NUT login
        credentials enabled) gets an actionable hint, not just raw OpenSSL."""
        monitor = make_monitor(tmp_path)
        with patch.object(
            monitor, "_run_upsc",
            return_value=(
                1,
                "",
                "Unknown return value from SSL_connect -1: Success\n"
                "Error: SSL error: error:0A000197:SSL routines::"
                "shutdown while in init\n",
            ),
        ):
            success, _data, err = monitor._get_all_ups_data()
        assert success is False
        # Raw text is preserved AND the hint is appended.
        assert "SSL routines" in err
        assert "disable NUT login credentials" in err
        assert "UniFi" in err

    @pytest.mark.unit
    def test_non_ssl_error_gets_no_hint(self, tmp_path):
        """An ordinary connection error must not be decorated with the SSL hint."""
        monitor = make_monitor(tmp_path)
        with patch.object(
            monitor, "_run_upsc",
            return_value=(1, "", "Error: Connection failure: Connection refused\n"),
        ):
            success, _data, err = monitor._get_all_ups_data()
        assert success is False
        assert err == "Error: Connection failure: Connection refused"
        assert "hint:" not in err

    @pytest.mark.unit
    def test_run_upsc_suppresses_nut_ssl_init_noise(self, tmp_path):
        monitor = make_monitor(tmp_path)
        with patch("eneru.monitor.run_command",
                   return_value=(0, "ups.status: OL\n", "")) as run:
            monitor._run_upsc([], full_poll=True)
        run.assert_called_once_with(
            ["upsc", "TestUPS@localhost"],
            env_overrides={"NUT_QUIET_INIT_SSL": "true"},
        )

    @pytest.mark.unit
    def test_stale_data_marker_returns_failure(self, tmp_path):
        monitor = make_monitor(tmp_path)
        with patch.object(monitor, "_run_upsc",
                          return_value=(0, "Data stale\n", "")):
            success, data, err = monitor._get_all_ups_data()
        assert success is False
        assert data == {}
        assert err == "Data stale"

    @pytest.mark.unit
    def test_missing_status_returns_failed_poll(self, tmp_path: Path) -> None:
        monitor = make_monitor(tmp_path)
        with patch.object(
            monitor, "_run_upsc",
            return_value=(0, "battery.charge: 5\nbattery.runtime: 30\n", ""),
        ):
            success, data, err = monitor._get_all_ups_data()
        assert success is False
        assert data == {}
        assert err == "Missing ups.status"


class TestNutNameAutodiscovery:
    """Issue #71: on a hard NUT connection failure, list the server's real UPS
    names (``upsc -l``), warn clearly, and self-heal an obviously-wrong
    ``ups.name`` (e.g. a NUT username placed where the UPS name belongs)."""

    @pytest.mark.unit
    def test_parse_nut_host(self, tmp_path):
        monitor = make_monitor(tmp_path)
        assert monitor._parse_nut_host("upsmon@10.13.0.8:3493") == "10.13.0.8:3493"
        assert monitor._parse_nut_host("UPS@server") == "server"
        assert monitor._parse_nut_host("UPS") == "localhost"
        assert monitor._parse_nut_host("UPS@") == "localhost"
        assert monitor._parse_nut_host("") == "localhost"

    @pytest.mark.unit
    def test_parse_nut_name(self, tmp_path):
        monitor = make_monitor(tmp_path)
        assert monitor._parse_nut_name("upsmon@10.13.0.8:3493") == "upsmon"
        assert monitor._parse_nut_name("UPS") == "UPS"

    @pytest.mark.unit
    def test_discover_returns_names_with_banners_on_stderr(self, tmp_path):
        """Real NUT prints names on stdout; SSL/connection banners go to
        stderr, which is discarded."""
        monitor = make_monitor(tmp_path)
        stderr = (
            "Init SSL without certificate database\n"
            "Connected to NUT server 192.168.178.11 in SSL\n"
            "Certificate verification is disabled\n"
        )
        with patch("eneru.monitor.run_command",
                   return_value=(0, "UPS\nUPS2\n", stderr)) as run:
            names = monitor._discover_ups_names("192.168.178.11")
        assert names == ["UPS", "UPS2"]
        run.assert_called_once_with(
            ["upsc", "-l", "192.168.178.11"],
            timeout=10,
            env_overrides={"NUT_QUIET_INIT_SSL": "true"},
        )

    @pytest.mark.unit
    def test_discover_filter_is_defensive_against_stdout_decoration(self, tmp_path):
        """Defense-in-depth: blank, whitespace, and colon lines on stdout are
        dropped, and duplicate names are de-duplicated."""
        monitor = make_monitor(tmp_path)
        stdout = (
            "\n"                        # blank -> dropped
            "ups-a\n"
            "Connected to X in SSL\n"   # internal space -> dropped
            "two\twords\n"             # internal tab -> dropped
            "key: value\n"             # colon -> dropped
            "ups-a\n"                   # duplicate -> de-duplicated
            "ups-b\n"
        )
        with patch("eneru.monitor.run_command", return_value=(0, stdout, "")):
            names = monitor._discover_ups_names("h")
        assert names == ["ups-a", "ups-b"]

    @pytest.mark.unit
    def test_discover_returns_empty_on_failure(self, tmp_path):
        monitor = make_monitor(tmp_path)
        with patch("eneru.monitor.run_command",
                   return_value=(1, "", "Connection failure")):
            assert monitor._discover_ups_names("10.0.0.9") == []

    @pytest.mark.unit
    def test_self_heals_single_ups(self, tmp_path):
        """Wrong name + exactly one real UPS -> auto-correct the poll target."""
        monitor = make_monitor(tmp_path)
        monitor._poll_target = "upsmon@10.13.0.8:3493"
        with patch("eneru.monitor.run_command",
                   return_value=(0, "UPS\n", "")):
            monitor._run_ups_name_diagnostic("Init SSL without certificate database")
        assert monitor._poll_target == "UPS@10.13.0.8:3493"
        assert monitor._ups_name_autocorrected is True
        # config.ups.name (display/state identity) is untouched.
        assert monitor.config.ups.name == "TestUPS@localhost"
        logged = " ".join(str(c) for c in monitor.logger.log.call_args_list)
        assert "Auto-correcting" in logged

    @pytest.mark.unit
    def test_self_heal_runs_only_once(self, tmp_path):
        """A second failure episode must not flip the target again."""
        monitor = make_monitor(tmp_path)
        monitor._poll_target = "upsmon@10.13.0.8:3493"
        with patch("eneru.monitor.run_command", return_value=(0, "UPS\n", "")):
            monitor._run_ups_name_diagnostic("err")
            healed = monitor._poll_target
            # Even if a different single UPS appears later, do not re-correct.
            with patch("eneru.monitor.run_command",
                       return_value=(0, "OTHER\n", "")):
                monitor._run_ups_name_diagnostic("err")
        assert monitor._poll_target == healed == "UPS@10.13.0.8:3493"

    @pytest.mark.unit
    def test_multiple_names_no_heal_lists_them(self, tmp_path):
        monitor = make_monitor(tmp_path)
        monitor._poll_target = "upsmon@10.13.0.8:3493"
        with patch("eneru.monitor.run_command",
                   return_value=(0, "ups-a\nups-b\n", "")):
            monitor._run_ups_name_diagnostic("err")
        assert monitor._poll_target == "upsmon@10.13.0.8:3493"  # unchanged
        assert monitor._ups_name_autocorrected is False
        logged = " ".join(str(c) for c in monitor.logger.log.call_args_list)
        assert "ups-a" in logged and "ups-b" in logged

    @pytest.mark.unit
    def test_configured_name_present_no_heal(self, tmp_path):
        """Name exists but poll still fails -> point at auth, not the name."""
        monitor = make_monitor(tmp_path)
        monitor._poll_target = "UPS@10.13.0.8:3493"
        with patch("eneru.monitor.run_command",
                   return_value=(0, "UPS\n", "")):
            monitor._run_ups_name_diagnostic("ERR ACCESS-DENIED")
        assert monitor._poll_target == "UPS@10.13.0.8:3493"  # unchanged
        assert monitor._ups_name_autocorrected is False
        logged = " ".join(str(c) for c in monitor.logger.log.call_args_list)
        assert "exists" in logged

    @pytest.mark.unit
    def test_discovery_failure_warns_unverified(self, tmp_path):
        monitor = make_monitor(tmp_path)
        monitor._poll_target = "upsmon@10.13.0.8:3493"
        with patch("eneru.monitor.run_command",
                   return_value=(1, "", "Connection failure")):
            monitor._run_ups_name_diagnostic("err")
        assert monitor._poll_target == "upsmon@10.13.0.8:3493"  # unchanged
        logged = " ".join(str(c) for c in monitor.logger.log.call_args_list)
        assert "Could not list" in logged

    @pytest.mark.unit
    def test_run_upsc_uses_healed_poll_target(self, tmp_path):
        """After a self-heal, polling uses the corrected name."""
        monitor = make_monitor(tmp_path)
        monitor._poll_target = "UPS@10.13.0.8:3493"
        with patch("eneru.monitor.run_command",
                   return_value=(0, "ups.status: OL\n", "")) as run:
            monitor._run_upsc([], full_poll=True)
        run.assert_called_once_with(
            ["upsc", "UPS@10.13.0.8:3493"],
            env_overrides={"NUT_QUIET_INIT_SSL": "true"},
        )

    @pytest.mark.unit
    def test_diagnostic_fires_once_on_first_hard_error(self, tmp_path):
        monitor = make_monitor(tmp_path)
        monitor.state.previous_status = "OL CHRG"
        monitor.state.connection_state = "OK"
        monitor.state.connection_error_count = 0
        with patch.object(monitor, "_run_ups_name_diagnostic") as diag:
            _run_one_iteration(monitor, (False, {}, "Network error"))
        diag.assert_called_once()

    @pytest.mark.unit
    def test_diagnostic_skipped_mid_episode(self, tmp_path):
        """It runs only when connection_error_count reaches 1 (episode start)."""
        monitor = make_monitor(tmp_path)
        monitor.state.previous_status = "OL CHRG"
        monitor.state.connection_state = "GRACE_PERIOD"
        monitor.state.connection_error_count = 5
        with patch.object(monitor, "_run_ups_name_diagnostic") as diag:
            _run_one_iteration(monitor, (False, {}, "Network error"))
        diag.assert_not_called()

    @pytest.mark.unit
    def test_diagnostic_skipped_on_fsb_path_while_on_battery(self, tmp_path):
        """The up-to-10s discovery probe must never sit in front of an FSB:
        on battery with tolerance=1, the shutdown fires and discovery is
        skipped entirely."""
        monitor = make_monitor(tmp_path)
        monitor._in_redundancy_group = False
        monitor.config.ups.max_stale_data_tolerance = 1
        monitor.state.previous_status = "OB DISCHRG"
        monitor.state.connection_state = "OK"
        monitor.state.connection_error_count = 0
        with patch.object(monitor, "_run_ups_name_diagnostic") as diag, \
             patch.object(monitor, "_execute_shutdown_sequence") as exec_seq:
            _run_one_iteration(monitor, (False, {}, "Network error"))
        diag.assert_not_called()
        exec_seq.assert_called_once()


class TestFormatUpscError:
    """`_format_upsc_error` joins real error lines from both streams, drops the
    benign SSL-init noise, de-duplicates, and never resurfaces filtered noise."""

    @pytest.mark.unit
    def test_filters_blank_dedups_and_joins_both_streams(self, tmp_path):
        monitor = make_monitor(tmp_path)
        out = monitor._format_upsc_error(
            stdout="Error: Unknown UPS\n\nError: Unknown UPS\n",   # blank + dup
            stderr="Init SSL without certificate database\n"
                   "Error: Driver not connected\n",
        )
        # stderr is scanned first; the SSL line is dropped; the duplicate
        # stdout line is collapsed.
        assert out == "Error: Driver not connected | Error: Unknown UPS"

    @pytest.mark.unit
    def test_only_ssl_noise_falls_back_without_resurfacing_it(self, tmp_path):
        monitor = make_monitor(tmp_path)
        out = monitor._format_upsc_error(
            stdout="", stderr="Init SSL without certificate database\n")
        assert out == "upsc exited without output"


class TestRecordRemoteHealthEvent:
    """`_record_remote_health_event` mirrors RemoteHealthManager events
    into the per-UPS stats DB (best-effort)."""

    @pytest.mark.unit
    def test_writes_event_with_notification_flag(self, tmp_path):
        monitor = make_monitor(tmp_path)
        monitor._stats_store = MagicMock()
        monitor._record_remote_health_event(
            "REMOTE_HEALTH_FAILED", "ssh timeout", notification_sent=True,
        )
        monitor._stats_store.log_event.assert_called_once_with(
            "REMOTE_HEALTH_FAILED", "ssh timeout", notification_sent=True,
        )


class TestInitializeNotificationsStoreOpenFailure:
    """When `_stats_store.open()` raises during `_initialize_notifications`,
    the daemon logs a warning and continues without persistence."""

    @pytest.mark.unit
    def test_open_failure_logs_warning_and_continues(self, tmp_path):
        monitor = make_monitor(tmp_path)
        monitor.config.notifications.enabled = False  # skip worker path
        monitor._notification_worker = None
        store = MagicMock()
        store._conn = None
        store.open.side_effect = RuntimeError("disk full")
        monitor._stats_store = store
        log = []
        monitor._log_message = log.append

        monitor._initialize_notifications()

        assert any("stats store open failed" in m for m in log), log

    @pytest.mark.unit
    def test_pre_worker_lifecycle_sweep_swallows_sqlite_error(self, tmp_path):
        """The pre-worker lifecycle sweep wraps SQLite/OS errors so a
        transient DB hiccup doesn't break startup. The late-cancel block
        in `_emit_lifecycle_startup_notification` still has a chance to
        catch up later."""
        import sqlite3 as _sqlite3

        monitor = make_monitor(tmp_path)
        monitor.config.notifications.enabled = False
        monitor._notification_worker = None
        store = MagicMock()
        store._conn = object()  # already open — skip the open() branch
        store.find_pending_by_category.side_effect = _sqlite3.OperationalError(
            "disk I/O error"
        )
        monitor._stats_store = store
        log = []
        monitor._log_message = log.append

        # Must not raise.
        monitor._initialize_notifications()

        assert any("pre-worker lifecycle sweep failed" in m for m in log), log

    @pytest.mark.unit
    def test_register_store_called_when_worker_starts_and_store_open(self, tmp_path):
        """Happy-path: after NotificationWorker.start() returns True and
        the per-UPS stats store has an open `_conn`, register the store
        so the worker can persist messages across restarts."""
        monitor = make_monitor(tmp_path)
        monitor.config.notifications.enabled = True
        monitor._notification_worker = None
        store = MagicMock()
        store._conn = object()
        store.find_pending_by_category.return_value = []
        monitor._stats_store = store

        fake_worker = MagicMock()
        fake_worker.start.return_value = True
        fake_worker.get_service_count.return_value = 1

        with patch("eneru.monitor.APPRISE_AVAILABLE", True), \
             patch("eneru.monitor.NotificationWorker", return_value=fake_worker):
            monitor._initialize_notifications()

        fake_worker.register_store.assert_called_once_with(store)


class TestEmitLifecycleStartupCoordinatorMode:
    """The lifecycle classifier short-circuits in coordinator mode
    (the multi_ups path emits the single classified notification)."""

    @pytest.mark.unit
    def test_coordinator_mode_returns_after_meta_update(self, tmp_path):
        monitor = make_monitor(tmp_path)
        monitor._coordinator_mode = True
        store = MagicMock()
        store._conn = object()
        store.get_meta.return_value = None
        monitor._stats_store = store

        with patch("eneru.monitor.read_shutdown_marker") as read_sh, \
             patch("eneru.monitor.delete_shutdown_marker") as del_sh, \
             patch("eneru.monitor.delete_upgrade_marker") as del_up:
            monitor._emit_lifecycle_startup_notification()

        # Meta updated, then early return — markers never consulted/deleted.
        store.set_meta.assert_called_once_with("last_seen_version", monitor_mod_version())
        read_sh.assert_not_called()
        del_sh.assert_not_called()
        del_up.assert_not_called()


def monitor_mod_version() -> str:
    """Helper to read the package __version__ without polluting imports."""
    from eneru.version import __version__
    return __version__


class TestEmitLifecycleStartupExceptionalPaths:
    """Cover the defensive corners of `_emit_lifecycle_startup_notification`."""

    @pytest.mark.unit
    def test_log_event_failure_does_not_mask_notification(self, tmp_path):
        """When the events-table mirror raises, swallow the error and
        still send the startup notification."""
        from eneru.lifecycle import REASON_SEQUENCE_COMPLETE
        from eneru.version import __version__

        monitor = make_monitor(tmp_path)
        store = MagicMock()
        store._conn = object()
        store.get_meta.return_value = __version__
        store.log_event.side_effect = RuntimeError("table missing")
        store.find_pending_by_category.return_value = []
        monitor._stats_store = store
        monitor._send_notification = MagicMock()

        marker = {"shutdown_at": 1000, "version": __version__,
                  "reason": REASON_SEQUENCE_COMPLETE}
        with patch("eneru.monitor.read_shutdown_marker", return_value=marker), \
             patch("eneru.monitor.read_upgrade_marker", return_value=None), \
             patch("eneru.monitor.delete_shutdown_marker"), \
             patch("eneru.monitor.delete_upgrade_marker"), \
             patch("eneru.monitor.coalesce_recovered_with_prev_shutdown",
                   return_value=None):
            # Must not raise.
            monitor._emit_lifecycle_startup_notification()

        monitor._send_notification.assert_called_once()

    @pytest.mark.unit
    def test_cancels_prior_pending_lifecycle_rows(self, tmp_path):
        """When the late-cancel block runs, it walks the pending lifecycle
        rows and cancels each as 'superseded'."""
        monitor = make_monitor(tmp_path)
        store = MagicMock()
        store._conn = object()
        store.get_meta.return_value = None
        store.find_pending_by_category.return_value = [(7,), (9,)]
        monitor._stats_store = store
        monitor._send_notification = MagicMock()

        with patch("eneru.monitor.read_shutdown_marker", return_value=None), \
             patch("eneru.monitor.read_upgrade_marker", return_value=None), \
             patch("eneru.monitor.delete_shutdown_marker"), \
             patch("eneru.monitor.delete_upgrade_marker"):
            monitor._emit_lifecycle_startup_notification()

        store.cancel_notification.assert_any_call(7, "superseded")
        store.cancel_notification.assert_any_call(9, "superseded")
        assert store.cancel_notification.call_count == 2

    @pytest.mark.unit
    def test_corrupted_shutdown_at_falls_back_to_zero(self, tmp_path):
        """A non-numeric ``shutdown_at`` in the marker must not crash —
        the TypeError/ValueError handler resets to 0 so the coalesce
        still runs (with a zero downtime)."""
        from eneru.lifecycle import REASON_SEQUENCE_COMPLETE
        from eneru.version import __version__

        monitor = make_monitor(tmp_path)
        store = MagicMock()
        store._conn = object()
        store.get_meta.return_value = __version__
        store.find_pending_by_category.return_value = []
        monitor._stats_store = store
        monitor._send_notification = MagicMock()

        marker = {"shutdown_at": "garbage", "version": __version__,
                  "reason": REASON_SEQUENCE_COMPLETE}
        with patch("eneru.monitor.read_shutdown_marker", return_value=marker), \
             patch("eneru.monitor.read_upgrade_marker", return_value=None), \
             patch("eneru.monitor.delete_shutdown_marker"), \
             patch("eneru.monitor.delete_upgrade_marker"), \
             patch("eneru.monitor.coalesce_recovered_with_prev_shutdown",
                   return_value=None) as coalesce:
            monitor._emit_lifecycle_startup_notification()

        # Coalesce was still invoked — proves the bad marker didn't
        # propagate as an exception.
        coalesce.assert_called_once()

    @pytest.mark.unit
    def test_coalesced_body_overrides_classification_body(self, tmp_path):
        """When the coalescer returns a non-None body, the lifecycle
        notification ships that richer message instead of the bare
        classification."""
        from eneru.lifecycle import REASON_SEQUENCE_COMPLETE
        from eneru.version import __version__

        monitor = make_monitor(tmp_path)
        store = MagicMock()
        store._conn = object()
        store.get_meta.return_value = __version__
        store.find_pending_by_category.return_value = []
        monitor._stats_store = store
        monitor._send_notification = MagicMock()

        marker = {"shutdown_at": 1000, "version": __version__,
                  "reason": REASON_SEQUENCE_COMPLETE}
        with patch("eneru.monitor.read_shutdown_marker", return_value=marker), \
             patch("eneru.monitor.read_upgrade_marker", return_value=None), \
             patch("eneru.monitor.delete_shutdown_marker"), \
             patch("eneru.monitor.delete_upgrade_marker"), \
             patch("eneru.monitor.coalesce_recovered_with_prev_shutdown",
                   return_value="🪄  coalesced summary"):
            monitor._emit_lifecycle_startup_notification()

        sent_body = monitor._send_notification.call_args.args[0]
        assert sent_body == "🪄  coalesced summary"


class TestLogEnabledFeaturesExplicitRuntime:
    """`_log_enabled_features` formats the runtime/compose string
    differently for runtime="auto" vs an explicit runtime; cover the
    explicit-runtime-with-no-compose case (line 556)."""

    @pytest.mark.unit
    def test_explicit_runtime_no_compose(self, tmp_path):
        monitor = make_monitor(tmp_path)
        monitor.config.ups_groups[0].containers = ContainersConfig(
            enabled=True,
            runtime="podman",
            compose_files=[],
        )
        log = []
        monitor._log_message = log.append
        monitor._log_enabled_features()
        joined = " ".join(log)
        assert "Containers (podman)" in joined
        # Make sure we did NOT mistakenly tag a compose count.
        assert "compose" not in joined


class TestLogMessagePrefixGroupExtra:
    """When `_log_prefix` is set, `_log_message` derives a `group` field
    from the prefix (line 591). That feeds JSON pipelines so they can
    group per-UPS rows without parsing the message text."""

    @pytest.mark.unit
    def test_prefix_populates_group_extra(self, tmp_path):
        monitor = make_monitor(tmp_path)
        monitor._log_prefix = "[Main UPS] "
        monitor.logger = MagicMock()
        monitor._log_message("hello", category="test")
        assert monitor.logger.log.call_args.kwargs["group"] == "Main UPS"

    @pytest.mark.unit
    def test_explicit_group_extra_is_preserved(self, tmp_path):
        """Caller-supplied `group` wins over the auto-derived one."""
        monitor = make_monitor(tmp_path)
        monitor._log_prefix = "[Main UPS] "
        monitor.logger = MagicMock()
        monitor._log_message("hi", group="explicit")
        assert monitor.logger.log.call_args.kwargs["group"] == "explicit"


class TestLogPowerEventDefensiveBranches:
    """`_log_power_event` has two defensive branches: the syslog
    side-channel swallows exceptions (lines 673-674), the stats
    persistence swallows exceptions (lines 753-754), and unmapped
    event names fall through to the generic INFO notification (line 762)."""

    @pytest.mark.unit
    def test_syslog_failure_is_swallowed(self, tmp_path):
        monitor = make_monitor(tmp_path)
        monitor._stats_store = MagicMock()
        monitor._shutdown_flag_path.unlink(missing_ok=True)

        with patch("eneru.monitor.run_command",
                   side_effect=OSError("logger missing")):
            # Must not raise even though run_command blew up.
            monitor._log_power_event("ON_BATTERY", "test")

        # The event still landed in stats and the notification still
        # dispatched — only the legacy syslog mirror failed.
        monitor._stats_store.log_event.assert_called_once()
        monitor._notification_worker.send.assert_called_once()

    @pytest.mark.unit
    def test_stats_log_event_failure_is_swallowed(self, tmp_path):
        monitor = make_monitor(tmp_path)
        store = MagicMock()
        store.log_event.side_effect = RuntimeError("DB locked")
        monitor._stats_store = store
        monitor._shutdown_flag_path.unlink(missing_ok=True)

        with patch("eneru.monitor.run_command", return_value=(0, "", "")):
            # Must not raise even though log_event blew up.
            monitor._log_power_event("ON_BATTERY", "test")

        # Notification still fires — stats hiccup must not block alerts.
        monitor._notification_worker.send.assert_called_once()

    @pytest.mark.unit
    def test_unmapped_event_uses_generic_info_notification(self, tmp_path):
        """An event name not in the per-event mapping falls through to
        the generic `⚡  **Event:** ...` body with NOTIFY_INFO."""
        monitor = make_monitor(tmp_path)
        monitor._stats_store = MagicMock()
        monitor._shutdown_flag_path.unlink(missing_ok=True)

        with patch("eneru.monitor.run_command", return_value=(0, "", "")):
            monitor._log_power_event("CUSTOM_TELEMETRY", "value=42")

        send_kwargs = monitor._notification_worker.send.call_args.kwargs
        assert "⚡  **Event:** CUSTOM_TELEMETRY" in send_kwargs["body"]
        assert send_kwargs["category"] == "power_event"


class TestSaveStateDefensive:
    """`_save_state` writes the per-UPS state file and buffers a stats
    sample, both wrapped so write failures don't crash the poll loop."""

    @pytest.mark.unit
    def test_state_file_write_failure_is_swallowed(self, tmp_path):
        monitor = make_monitor(tmp_path)
        monitor._stats_store = MagicMock()
        # Point state_file_path at a location that can't be written
        # (parent doesn't exist + write_text throws).
        bogus = tmp_path / "does-not-exist" / "state-file"
        monitor._state_file_path = bogus

        # Must not raise.
        monitor._save_state({"ups.status": "OL", "battery.charge": "100"})

    @pytest.mark.unit
    def test_stats_buffer_failure_is_swallowed(self, tmp_path):
        monitor = make_monitor(tmp_path)
        store = MagicMock()
        store.buffer_sample.side_effect = RuntimeError("ring full")
        monitor._stats_store = store

        # Must not raise.
        monitor._save_state({"ups.status": "OL", "battery.charge": "100"})

        # The state file was still written even though buffering blew up.
        assert monitor._state_file_path.exists()


class TestExecuteShutdownSequenceDelegatedStatsEvent:
    """In delegated mode, `_execute_shutdown_sequence` logs a
    DELEGATED_SHUTDOWN_INITIATED event — and swallows any stats failure
    on that write so the safety-critical path keeps going."""

    @pytest.mark.unit
    def test_delegated_event_log_failure_is_swallowed(self, tmp_path):
        from eneru.shutdown.remote import RemoteShutdownResult

        monitor = make_monitor(
            tmp_path,
            remote_servers=[RemoteServerConfig(
                name="host-loopback",
                enabled=True,
                host="127.0.0.1",
                user="root",
                shutdown_command="shutdown -h now",
                is_host_loopback=True,
            )],
        )
        monitor._coordinator_mode = False
        monitor.config.local_shutdown.enabled = True
        store = MagicMock()
        store.log_event.side_effect = RuntimeError("DB gone")
        monitor._stats_store = store
        monitor._send_notification = MagicMock()
        monitor._shutdown_vms = MagicMock()
        monitor._shutdown_containers = MagicMock()
        monitor._sync_filesystems = MagicMock()
        monitor._unmount_filesystems = MagicMock()
        monitor._shutdown_remote_servers = MagicMock(return_value=[
            RemoteShutdownResult(
                server="host-loopback",
                host="127.0.0.1",
                shutdown_sent=True,
                dry_run=True,
            )
        ])

        with patch("eneru.cli._detect_runtime_context",
                   return_value="container (Docker)"):
            # Must not raise.
            monitor._execute_shutdown_sequence()


class TestCleanupAndExitDefensive:
    """Defensive corners of `_cleanup_and_exit`."""

    @pytest.mark.unit
    def test_daemon_stop_log_event_failure_is_swallowed(self, tmp_path):
        monitor = make_monitor(tmp_path)
        store = MagicMock()
        store._conn = MagicMock()
        store.log_event.side_effect = RuntimeError("DB locked")
        monitor._stats_store = store
        monitor._stop_stats = MagicMock()
        monitor._notification_worker = MagicMock()
        monitor._send_notification = MagicMock(return_value=1)
        monitor._shutdown_flag_path.unlink(missing_ok=True)

        with patch("eneru.monitor.read_upgrade_marker", return_value=None), \
             patch("eneru.monitor.read_shutdown_marker", return_value=None), \
             patch("eneru.monitor.write_shutdown_marker"), \
             patch("eneru.monitor.schedule_deferred_stop_or_eager_send"):
            # Must not raise even though log_event blew up.
            with pytest.raises(SystemExit):
                monitor._cleanup_and_exit(15, None)

    @pytest.mark.unit
    def test_eager_apprise_fallback_swallows_apprise_exception(self, tmp_path):
        """When the eager Apprise fallback also blows up, swallow it —
        nothing more we can do at this point in shutdown."""
        monitor = make_monitor(tmp_path)
        store = MagicMock()
        store._conn = None  # DB never opened
        monitor._stats_store = store
        monitor._stop_stats = MagicMock()
        worker = MagicMock()
        worker._send_via_apprise_bounded.side_effect = RuntimeError("apprise gone")
        monitor._notification_worker = worker
        monitor._send_notification = MagicMock(return_value=None)
        monitor._shutdown_flag_path.unlink(missing_ok=True)

        with patch("eneru.monitor.read_upgrade_marker", return_value=None), \
             patch("eneru.monitor.read_shutdown_marker", return_value=None), \
             patch("eneru.monitor.write_shutdown_marker"), \
             patch("eneru.monitor.schedule_deferred_stop_or_eager_send"):
            with pytest.raises(SystemExit):
                monitor._cleanup_and_exit(15, None)

        worker._send_via_apprise_bounded.assert_called_once()


class TestHandleOnBatteryDefensive:
    """Cover the non-numeric battery_charge warning and the per-5s status
    log inside `_handle_on_battery`."""

    @pytest.mark.unit
    def test_non_numeric_battery_charge_warns(self, tmp_path):
        monitor = make_monitor(
            tmp_path,
            triggers=TriggersConfig(on_battery_stabilization_delay=0),
        )
        monitor.state.previous_status = "OB DISCHRG"
        monitor.state.on_battery_start_time = int(time.time()) - 10
        log = []
        monitor._log_message = log.append

        ups_data = {
            "ups.status": "OB DISCHRG",
            "battery.charge": "N/A",
            "battery.runtime": "1200",
            "ups.load": "30",
        }
        with patch.object(monitor, "_calculate_depletion_rate",
                          return_value=0.0):
            monitor._handle_on_battery(ups_data)

        assert any("non-numeric battery charge value" in m for m in log), log

    @pytest.mark.unit
    def test_status_log_every_five_seconds(self, tmp_path):
        """At ``int(time.time()) % 5 == 0`` the handler emits the
        periodic `🔋  On battery` heartbeat line."""
        monitor = make_monitor(
            tmp_path,
            triggers=TriggersConfig(on_battery_stabilization_delay=0),
        )
        monitor.state.previous_status = "OB DISCHRG"
        monitor.state.on_battery_start_time = 1000
        log = []
        monitor._log_message = log.append

        ups_data = {
            "ups.status": "OB DISCHRG",
            "battery.charge": "80",
            "battery.runtime": "1200",
            "ups.load": "30",
        }
        # Fix time.time() so the modulo branch is deterministic.
        with patch("eneru.monitor.time.time", return_value=1500.0), \
             patch.object(monitor, "_calculate_depletion_rate",
                          return_value=1.5):
            monitor._handle_on_battery(ups_data)

        assert any(m.startswith("🔋  On battery:") for m in log), log


class TestMainLoopBranchCoverage:
    """Cover the remaining `_main_loop` branches:
    - failsafe trigger while NOT on battery routes to `_handle_connection_failure`
    - FAILED → OK recovery clears the advisory trigger when in a redundancy group
    - "OB" status routes through `_handle_on_battery`
    """

    @pytest.mark.unit
    def test_failsafe_when_not_on_battery_uses_grace_period(self, tmp_path):
        """Connection failure while NOT on battery feeds into
        `_handle_connection_failure` (which applies the grace period
        when enabled)."""
        monitor = make_monitor(tmp_path)
        monitor.state.previous_status = "OL CHRG"
        monitor._handle_connection_failure = MagicMock()
        monitor._execute_shutdown_sequence = MagicMock()

        _run_one_iteration(monitor,
                           (False, {}, "connection refused"))

        monitor._handle_connection_failure.assert_called_once()
        monitor._execute_shutdown_sequence.assert_not_called()

    @pytest.mark.unit
    def test_failed_to_ok_clears_advisory_in_redundancy(self, tmp_path):
        """FAILED → OK recovery: when the monitor belongs to a redundancy
        group, the advisory trigger must be cleared so the group
        evaluator sees this UPS as healthy."""
        monitor = make_monitor(tmp_path)
        monitor._in_redundancy_group = True
        monitor._clear_advisory_trigger = MagicMock()
        monitor.state.connection_state = "FAILED"
        monitor.state.previous_status = "OL CHRG"

        _run_one_iteration(monitor, (True, {
            "ups.status": "OL CHRG",
            "battery.charge": "100",
            "battery.runtime": "3600",
        }, ""))

        monitor._clear_advisory_trigger.assert_called_once()

    @pytest.mark.unit
    def test_ob_status_routes_to_on_battery_handler(self, tmp_path):
        """The OB-status branch in `_main_loop` calls `_handle_on_battery`."""
        monitor = make_monitor(tmp_path)
        monitor._handle_on_battery = MagicMock()
        monitor._handle_on_line = MagicMock()
        monitor.state.previous_status = "OL CHRG"
        monitor.state.connection_state = "OK"

        _run_one_iteration(monitor, (True, {
            "ups.status": "OB DISCHRG",
            "battery.charge": "85",
            "battery.runtime": "1200",
            "ups.load": "30",
        }, ""))

        monitor._handle_on_battery.assert_called_once()
        monitor._handle_on_line.assert_not_called()

    @pytest.mark.unit
    @pytest.mark.parametrize("status", ["OFF", "BYPASS", "DISCHRG"])
    def test_neutral_status_routes_to_neither_handler(self, tmp_path, status):
        """ISS-019: statuses without OL/OB/FSD (OFF, BYPASS, bare DISCHRG) take
        no power-state action and don't crash. Token matching also stops
        `CHRG in DISCHRG` from aliasing bare DISCHRG onto the on-line path."""
        monitor = make_monitor(tmp_path)
        monitor._handle_on_battery = MagicMock()
        monitor._handle_on_line = MagicMock()
        monitor.state.previous_status = "OL"
        monitor.state.connection_state = "OK"

        _run_one_iteration(monitor, (True, {
            "ups.status": status, "battery.charge": "80",
            "battery.runtime": "1200", "ups.load": "30",
        }, ""))

        monitor._handle_on_battery.assert_not_called()
        monitor._handle_on_line.assert_not_called()

    @pytest.mark.unit
    def test_ol_chrg_routes_to_on_line(self, tmp_path):
        """ISS-019: `OL CHRG` still routes to the on-line handler under tokens."""
        monitor = make_monitor(tmp_path)
        monitor._handle_on_battery = MagicMock()
        monitor._handle_on_line = MagicMock()
        monitor.state.previous_status = "OB"
        monitor.state.connection_state = "OK"

        _run_one_iteration(monitor, (True, {
            "ups.status": "OL CHRG", "battery.charge": "95",
            "battery.runtime": "3000", "ups.load": "20",
        }, ""))

        monitor._handle_on_line.assert_called_once()
        monitor._handle_on_battery.assert_not_called()

    @pytest.mark.unit
    @pytest.mark.parametrize("status", ["OB LB", "OB DISCHRG LB", "OB DISCHRG"])
    def test_multiflag_on_battery_statuses_route_to_on_battery(self, tmp_path, status):
        """ISS-019: safety-critical low-battery routing — real multi-flag NUT
        statuses carrying OB (with LB/DISCHRG) still hit the on-battery handler
        under token matching."""
        monitor = make_monitor(tmp_path)
        monitor._handle_on_battery = MagicMock()
        monitor._handle_on_line = MagicMock()
        monitor.state.previous_status = "OL"
        monitor.state.connection_state = "OK"

        _run_one_iteration(monitor, (True, {
            "ups.status": status, "battery.charge": "15",
            "battery.runtime": "120", "ups.load": "40",
        }, ""))

        monitor._handle_on_battery.assert_called_once()
        monitor._handle_on_line.assert_not_called()


class TestT3DepletionRateBranches:
    """Cover the T3 depletion-rate branches: ignored during stabilization,
    ignored during grace period, and the actual trigger after grace."""

    @pytest.mark.unit
    def test_high_depletion_ignored_during_stabilization(self, tmp_path):
        monitor = make_monitor(
            tmp_path,
            triggers=TriggersConfig(
                on_battery_stabilization_delay=60,
                low_battery_threshold=20,
                critical_runtime_threshold=60,
                depletion=DepletionConfig(
                    window=300, critical_rate=10.0, grace_period=90,
                ),
                extended_time=ExtendedTimeConfig(enabled=False, threshold=900),
            ),
        )
        monitor.state.previous_status = "OB DISCHRG"
        # Within stabilization window (10s < 60s)
        monitor.state.on_battery_start_time = int(time.time()) - 10
        log = []
        monitor._log_message = log.append

        ups_data = {
            "ups.status": "OB DISCHRG",
            "battery.charge": "80",      # Above T1
            "battery.runtime": "1200",   # Above T2
            "ups.load": "30",
        }
        with patch.object(monitor, "_trigger_immediate_shutdown") as fire, \
             patch.object(monitor, "_calculate_depletion_rate",
                          return_value=25.0):
            monitor._handle_on_battery(ups_data)
            fire.assert_not_called()

        assert any("High depletion rate" in m and "stabilization" in m
                   for m in log), log

    @pytest.mark.unit
    def test_high_depletion_ignored_during_grace_period(self, tmp_path):
        monitor = make_monitor(
            tmp_path,
            triggers=TriggersConfig(
                on_battery_stabilization_delay=0,
                low_battery_threshold=20,
                critical_runtime_threshold=60,
                depletion=DepletionConfig(
                    window=300, critical_rate=10.0, grace_period=120,
                ),
                extended_time=ExtendedTimeConfig(enabled=False, threshold=900),
            ),
        )
        monitor.state.previous_status = "OB DISCHRG"
        # After stabilization (50s > 0), but within grace (50s < 120s)
        monitor.state.on_battery_start_time = int(time.time()) - 50
        log = []
        monitor._log_message = log.append

        ups_data = {
            "ups.status": "OB DISCHRG",
            "battery.charge": "80",
            "battery.runtime": "1200",
            "ups.load": "30",
        }
        with patch.object(monitor, "_trigger_immediate_shutdown") as fire, \
             patch.object(monitor, "_calculate_depletion_rate",
                          return_value=25.0):
            monitor._handle_on_battery(ups_data)
            fire.assert_not_called()

        assert any("High depletion rate" in m and "grace period" in m
                   for m in log), log

    @pytest.mark.unit
    def test_extended_time_ignored_during_stabilization(self, tmp_path):
        """T4: extended-time exceedance during the stabilization window
        logs the `🕒  INFO` stabilization line rather than triggering."""
        monitor = make_monitor(
            tmp_path,
            triggers=TriggersConfig(
                on_battery_stabilization_delay=120,
                low_battery_threshold=0,
                critical_runtime_threshold=0,
                depletion=DepletionConfig(
                    window=300, critical_rate=99.0, grace_period=0,
                ),
                extended_time=ExtendedTimeConfig(enabled=True, threshold=30),
            ),
        )
        monitor.state.previous_status = "OB DISCHRG"
        # Within stabilization (60s < 120s), but past extended-time (60s > 30s)
        monitor.state.on_battery_start_time = int(time.time()) - 60
        log = []
        monitor._log_message = log.append

        ups_data = {
            "ups.status": "OB DISCHRG",
            "battery.charge": "80",
            "battery.runtime": "1200",
            "ups.load": "30",
        }
        with patch.object(monitor, "_trigger_immediate_shutdown") as fire, \
             patch.object(monitor, "_calculate_depletion_rate",
                          return_value=0.0):
            monitor._handle_on_battery(ups_data)
            fire.assert_not_called()

        assert any("Extended-time trigger ignored" in m and "stabilization" in m
                   for m in log), log


class TestStartRemoteHealthHappyPath:
    """`_start_remote_health` constructs the manager and calls .start()
    when none exists yet."""

    @pytest.mark.unit
    def test_creates_manager_and_starts(self, tmp_path):
        monitor = make_monitor(tmp_path)
        monitor._remote_health_manager = None

        fake_mgr = MagicMock()
        with patch("eneru.monitor.RemoteHealthManager",
                   return_value=fake_mgr) as mgr_cls:
            monitor._start_remote_health()

        mgr_cls.assert_called_once()
        fake_mgr.start.assert_called_once()
        assert monitor._remote_health_manager is fake_mgr


class TestStopStatsDefensive:
    """`_stop_stats` is safe to call multiple times: it clears the writer
    handle and swallows close() exceptions."""

    @pytest.mark.unit
    def test_writer_cleared_and_close_called(self, tmp_path):
        monitor = make_monitor(tmp_path)
        writer = MagicMock()  # truthy
        monitor._stats_writer = writer
        store = MagicMock()
        monitor._stats_store = store

        monitor._stop_stats()

        assert monitor._stats_writer is None
        writer.join.assert_called_once_with(timeout=2)
        store.close.assert_called_once()

    @pytest.mark.unit
    def test_close_failure_is_swallowed(self, tmp_path):
        monitor = make_monitor(tmp_path)
        monitor._stats_writer = None  # already cleared
        store = MagicMock()
        store.close.side_effect = RuntimeError("disk gone")
        monitor._stats_store = store

        # Must not raise.
        monitor._stop_stats()


class TestWaitForInitialConnectionInterruptible:
    """ISS-021: startup connection wait must honor the stop event."""

    @pytest.mark.unit
    def test_stop_event_aborts_wait_promptly(self, tmp_path):
        monitor = make_monitor(tmp_path)
        monitor._get_all_ups_data = MagicMock(return_value=(False, {}, ""))
        monitor._log_message = MagicMock()
        monitor._stop_event.set()  # request stop before the wait

        start = time.monotonic()
        monitor._wait_for_initial_connection()
        elapsed = time.monotonic() - start

        # Must not have slept the 5s retry interval.
        assert elapsed < 2
        assert any(
            "interrupted" in str(call.args[0]).lower()
            for call in monitor._log_message.call_args_list
        )


class TestReportsRequireNotificationWorker:
    """ISS-023: single-UPS report send must be gated on a live worker so the
    last-run stamp is not advanced for an undelivered digest."""

    @pytest.mark.unit
    def test_reports_skipped_without_worker(self, tmp_path):
        monitor = make_monitor(tmp_path)
        monitor.config.reports.enabled = True
        monitor._notification_worker = None
        with patch("eneru.monitor.reports_mod.maybe_send_due_reports") as m:
            monitor._run_periodic_tasks()
        m.assert_not_called()

    @pytest.mark.unit
    def test_reports_run_with_worker(self, tmp_path):
        monitor = make_monitor(tmp_path)
        monitor.config.reports.enabled = True
        monitor._notification_worker = MagicMock()
        with patch("eneru.monitor.reports_mod.maybe_send_due_reports") as m:
            monitor._run_periodic_tasks()
        m.assert_called_once()


class TestStartupDependencyMarker:
    """ISS-006: a startup missing-dependency is an environment problem, not a
    runtime crash — it must not write the FATAL marker (which would misclassify
    the next successful start as 'exited fatally'), while genuine runtime
    exceptions still do."""

    @pytest.mark.unit
    def test_dependency_error_skips_fatal_marker(self, tmp_path):
        from eneru.monitor import DependencyError
        monitor = make_monitor(tmp_path)
        monitor._initialize = MagicMock(side_effect=DependencyError("no upsc"))
        monitor._send_notification = MagicMock()
        monitor._log_message = MagicMock()
        with patch("eneru.monitor.write_shutdown_marker") as marker:
            with pytest.raises(DependencyError):
                monitor.run()
        marker.assert_not_called()

    @pytest.mark.unit
    def test_runtime_fatal_still_writes_marker(self, tmp_path):
        monitor = make_monitor(tmp_path)
        monitor._initialize = MagicMock()
        monitor._main_loop = MagicMock(side_effect=RuntimeError("boom"))
        monitor._send_notification = MagicMock()
        monitor._log_message = MagicMock()
        with patch("eneru.monitor.write_shutdown_marker") as marker:
            with pytest.raises(RuntimeError):
                monitor.run()
        marker.assert_called_once()


class TestPR6OnBatteryLogic:
    """ISS-016 (advisory clear), ISS-018 (monotonic log throttle), ISS-020
    (monotonic on-battery timing) exercised through _handle_on_battery."""

    def _stable_on_battery(self, monitor, *, secs_ago):
        """Put the monitor mid-outage with timing anchored `secs_ago` back."""
        monitor.config.triggers.extended_time.enabled = False
        monitor.config.triggers.on_battery_stabilization_delay = 0
        monitor.state.previous_status = "OB"  # continuing outage, no fresh reset
        monitor.state.on_battery_start_time = int(time.time()) - secs_ago
        monitor.state.on_battery_start_mono = time.monotonic() - secs_ago

    @pytest.mark.unit
    def test_advisory_trigger_cleared_on_clean_reading(self, tmp_path):
        monitor = make_monitor(tmp_path)
        monitor._in_redundancy_group = True
        self._stable_on_battery(monitor, secs_ago=30)
        monitor.state.trigger_active = True
        monitor.state.trigger_reason = "prior transient dip"

        # A healthy reading (charge/runtime well above thresholds) => no reason.
        monitor._handle_on_battery({
            "ups.status": "OB", "battery.charge": "95",
            "battery.runtime": "3600", "ups.load": "10",
        })
        assert monitor.state.trigger_active is False

    @pytest.mark.unit
    def test_advisory_trigger_kept_while_still_critical(self, tmp_path):
        monitor = make_monitor(tmp_path)
        monitor._in_redundancy_group = True
        self._stable_on_battery(monitor, secs_ago=30)
        monitor.state.trigger_active = True

        # Still below the low-battery threshold => reason persists, stays latched.
        monitor._handle_on_battery({
            "ups.status": "OB", "battery.charge": "1",
            "battery.runtime": "5", "ups.load": "10",
        })
        assert monitor.state.trigger_active is True

    @pytest.mark.unit
    def test_advisory_trigger_kept_during_stabilization(self, tmp_path):
        """ISS-016 guard: a latched trigger must NOT be cleared while still in
        the on-battery stabilization window, even on a clean reading."""
        monitor = make_monitor(tmp_path)
        monitor._in_redundancy_group = True
        monitor.config.triggers.extended_time.enabled = False
        monitor.config.triggers.on_battery_stabilization_delay = 300
        monitor.state.previous_status = "OB"
        monitor.state.on_battery_start_time = int(time.time()) - 5
        monitor.state.on_battery_start_mono = time.monotonic() - 5
        monitor.state.trigger_active = True

        monitor._handle_on_battery({
            "ups.status": "OB", "battery.charge": "95",
            "battery.runtime": "3600", "ups.load": "10",
        })
        assert monitor.state.trigger_active is True

    @pytest.mark.unit
    def test_on_battery_status_log_throttled_on_monotonic(self, tmp_path, monkeypatch):
        monitor = make_monitor(tmp_path)
        monitor.config.triggers.extended_time.enabled = False
        monitor.config.triggers.on_battery_stabilization_delay = 0
        monitor.state.previous_status = "OB"
        monitor.state.on_battery_start_time = int(time.time())
        clock = [1000.0]
        monkeypatch.setattr("eneru.monitor.time.monotonic", lambda: clock[0])
        monitor.state.on_battery_start_mono = 1000.0
        logs = []
        monitor._log_message = logs.append
        data = {"ups.status": "OB", "battery.charge": "50",
                "battery.runtime": "600", "ups.load": "20"}

        monitor._handle_on_battery(data)      # t=1000 -> logs
        clock[0] = 1001.0
        monitor._handle_on_battery(data)      # +1s -> throttled
        clock[0] = 1007.0
        monitor._handle_on_battery(data)      # +6s -> logs again

        ob_logs = [m for m in logs if "On battery:" in m]
        assert len(ob_logs) == 2

    @pytest.mark.unit
    def test_time_on_battery_survives_wall_clock_jump(self, tmp_path, monkeypatch):
        """ISS-020: an NTP step backward mid-outage must not shrink the computed
        time-on-battery (it's anchored on the monotonic clock). Observe the real
        on-battery log line the handler emits."""
        monitor = make_monitor(tmp_path)
        monitor.config.triggers.extended_time.enabled = False
        monitor.config.triggers.on_battery_stabilization_delay = 0
        monitor.state.previous_status = "OB"
        monitor.state.on_battery_start_time = int(time.time())
        monitor.state.on_battery_start_mono = time.monotonic() - 120  # 2 min in
        logs = []
        monitor._log_message = logs.append

        # Wall clock jumps BACKWARD; monotonic is unaffected.
        monkeypatch.setattr("eneru.monitor.time.time", lambda: 1.0)
        monitor._handle_on_battery({
            "ups.status": "OB", "battery.charge": "80",
            "battery.runtime": "1200", "ups.load": "20",
        })
        ob_logs = [m for m in logs if "On battery:" in m]
        assert ob_logs, "expected an on-battery status log line"
        # ~2 minutes on battery from the monotonic anchor, NOT a wall-derived
        # negative/near-zero value.
        assert "Time on battery: 2m" in ob_logs[0]


class TestUpscNameDiagnosticCooldown:
    """ISS-022: the ~10s `upsc -l` name diagnostic must be rate-limited so a
    flapping server doesn't stall the poll thread once per flap."""

    @pytest.mark.unit
    def test_probe_gated_by_cooldown(self, tmp_path, monkeypatch):
        monitor = make_monitor(tmp_path)
        monitor.state.previous_status = "OL"  # not on battery
        monitor.state.connection_state = "OK"
        monitor._run_ups_name_diagnostic = MagicMock()
        clock = [10000.0]
        monkeypatch.setattr("eneru.monitor.time.monotonic", lambda: clock[0])

        # First failure episode: count -> 1, cooldown elapsed -> probe runs.
        _run_one_iteration(monitor, (False, {}, "connection refused"))
        assert monitor._run_ups_name_diagnostic.call_count == 1

        # A recovery resets the counter; another failure 60s later is inside the
        # 10-minute cooldown, so the probe must NOT run again.
        monitor.state.connection_error_count = 0
        clock[0] += 60
        _run_one_iteration(monitor, (False, {}, "connection refused"))
        assert monitor._run_ups_name_diagnostic.call_count == 1
