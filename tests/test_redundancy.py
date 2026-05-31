"""Tests for the redundancy-group evaluator and shutdown executor."""

import os
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from eneru import (
    BehaviorConfig,
    Config,
    ExtendedTimeConfig,
    LocalShutdownConfig,
    LoggingConfig,
    NotificationsConfig,
    RedundancyGroupConfig,
    RedundancyGroupEvaluator,
    RedundancyGroupExecutor,
    RemoteServerConfig,
    TriggersConfig,
    UPSConfig,
    UPSGroupConfig,
    UPSHealth,
    VMConfig,
    ContainersConfig,
    FilesystemsConfig,
    UnmountConfig,
)
from eneru.state import HealthSnapshot, MonitorState
from eneru.health_model import assess_health


_IMPOSSIBLE_PID = 999_999_999


def _snap(**overrides):
    """Build a fresh ``HealthSnapshot``; ``last_update_time`` defaults to now
    so the evaluator's stale-snapshot rule does not flip these to UNKNOWN."""
    base = dict(
        status="OL",
        battery_charge="100",
        runtime="1800",
        load="25",
        depletion_rate=0.0,
        time_on_battery=0,
        last_update_time=time.time(),
        connection_state="OK",
        trigger_active=False,
        trigger_reason="",
        stale_data_count=0,
        connection_lost_time=0.0,
    )
    base.update(overrides)
    return HealthSnapshot(**base)


class _FakeMonitor:
    """Stand-in for a UPSGroupMonitor with just the surface the evaluator needs."""

    def __init__(self, ups_name: str, snap: HealthSnapshot, *, check_interval: int = 1):
        # Match the real shape used by RedundancyGroupEvaluator._effective_health.
        self.config = MagicMock()
        self.config.ups = MagicMock()
        self.config.ups.name = ups_name
        self.config.ups.check_interval = check_interval
        self.config.ups.max_stale_data_tolerance = 3
        self.config.ups.connection_loss_grace_period = MagicMock()
        self.config.ups.connection_loss_grace_period.enabled = True
        self.config.ups.connection_loss_grace_period.duration = 60
        self.config.triggers = MagicMock()
        self.state = MonitorState()
        # Pre-populate the snapshot fields under the lock so .snapshot() returns
        # the values we asked for.
        with self.state._lock:
            self.state.latest_status = snap.status
            self.state.latest_battery_charge = snap.battery_charge
            self.state.latest_runtime = snap.runtime
            self.state.latest_load = snap.load
            self.state.latest_depletion_rate = snap.depletion_rate
            self.state.latest_time_on_battery = snap.time_on_battery
            self.state.latest_update_time = snap.last_update_time
            self.state.connection_state = snap.connection_state
            self.state.trigger_active = snap.trigger_active
            self.state.trigger_reason = snap.trigger_reason
            self.state.stale_data_count = snap.stale_data_count
            self.state.connection_lost_time = snap.connection_lost_time


def _base_config(*, dry_run: bool = True, tmp_path: Path = None) -> Config:
    """Build a minimal base Config the executor can pull paths from."""
    log_dir = tmp_path or Path("/tmp")
    logging_cfg = LoggingConfig(
        file=None,
        state_file=str(log_dir / "test-state"),
        battery_history_file=str(log_dir / "test-battery"),
        shutdown_flag_file=str(log_dir / "test-flag"),
    )
    return Config(
        ups_groups=[UPSGroupConfig(ups=UPSConfig(name="placeholder@host"))],
        behavior=BehaviorConfig(dry_run=dry_run),
        logging=logging_cfg,
        notifications=NotificationsConfig(enabled=False),
        local_shutdown=LocalShutdownConfig(enabled=False, trigger_on="none"),
    )


def _redundancy_group(**kwargs) -> RedundancyGroupConfig:
    defaults = dict(
        name="rg",
        ups_sources=["UPS-A", "UPS-B"],
        min_healthy=1,
        degraded_counts_as="healthy",
        unknown_counts_as="critical",
        is_local=False,
    )
    defaults.update(kwargs)
    return RedundancyGroupConfig(**defaults)


# ===========================================================================
# Evaluator counting + policy
# ===========================================================================

class TestEvaluatorCounting:

    def _make_evaluator(self, group, monitors_by_name):
        executor = MagicMock()
        executor.shutdown.return_value = True
        return RedundancyGroupEvaluator(
            group, monitors_by_name, executor,
            stop_event=threading.Event(), logger=None,
        ), executor

    @pytest.mark.unit
    def test_two_healthy_keeps_quorum(self):
        group = _redundancy_group(min_healthy=1)
        monitors = {
            "UPS-A": _FakeMonitor("UPS-A", _snap()),
            "UPS-B": _FakeMonitor("UPS-B", _snap()),
        }
        ev, executor = self._make_evaluator(group, monitors)
        healthy, _ = ev.evaluate_once()
        assert healthy == 2
        executor.shutdown.assert_not_called()

    @pytest.mark.unit
    def test_group_specific_extended_time_trigger_drives_quorum(self):
        """Redundancy-group triggers are evaluated at the group layer.

        This avoids mutating member monitor configs, which matters when
        one UPS participates in multiple redundancy groups with different
        trigger thresholds.
        """
        group = _redundancy_group(
            min_healthy=1,
            triggers=TriggersConfig(
                extended_time=ExtendedTimeConfig(enabled=True, threshold=30),
            ),
        )
        monitors = {
            "UPS-A": _FakeMonitor(
                "UPS-A", _snap(status="OB", time_on_battery=31),
            ),
            "UPS-B": _FakeMonitor(
                "UPS-B", _snap(status="OB", time_on_battery=31),
            ),
        }
        ev, executor = self._make_evaluator(group, monitors)
        healthy, _ = ev.evaluate_once()
        assert healthy == 0
        executor.shutdown.assert_called_once()

    @pytest.mark.unit
    def test_group_specific_thresholds_do_not_fire_while_online(self):
        group = _redundancy_group(
            min_healthy=1,
            triggers=TriggersConfig(low_battery_threshold=90),
        )
        monitors = {
            "UPS-A": _FakeMonitor(
                "UPS-A", _snap(status="OL", battery_charge="50"),
            ),
            "UPS-B": _FakeMonitor(
                "UPS-B", _snap(status="OL", battery_charge="50"),
            ),
        }
        ev, executor = self._make_evaluator(group, monitors)
        healthy, _ = ev.evaluate_once()
        assert healthy == 2
        executor.shutdown.assert_not_called()

    @pytest.mark.unit
    def test_one_critical_one_healthy_keeps_quorum_min1(self):
        group = _redundancy_group(min_healthy=1)
        monitors = {
            "UPS-A": _FakeMonitor("UPS-A", _snap(trigger_active=True,
                                                 trigger_reason="low")),
            "UPS-B": _FakeMonitor("UPS-B", _snap()),
        }
        ev, executor = self._make_evaluator(group, monitors)
        healthy, per_ups = ev.evaluate_once()
        assert healthy == 1
        assert per_ups["UPS-A"] == UPSHealth.CRITICAL
        assert per_ups["UPS-B"] == UPSHealth.HEALTHY
        executor.shutdown.assert_not_called()

    @pytest.mark.unit
    def test_both_critical_loses_quorum_and_fires(self):
        group = _redundancy_group(min_healthy=1)
        monitors = {
            "UPS-A": _FakeMonitor("UPS-A", _snap(trigger_active=True,
                                                 trigger_reason="low")),
            "UPS-B": _FakeMonitor("UPS-B", _snap(trigger_active=True,
                                                 trigger_reason="low")),
        }
        ev, executor = self._make_evaluator(group, monitors)
        ev.evaluate_once()
        executor.shutdown.assert_called_once()
        reason = executor.shutdown.call_args[0][0]
        assert "healthy=0" in reason and "min_healthy=1" in reason

    @pytest.mark.unit
    def test_unknown_monitor_treated_as_unknown(self):
        # No FakeMonitor in the map → evaluator falls back to UNKNOWN.
        group = _redundancy_group(min_healthy=1, unknown_counts_as="critical")
        ev, executor = self._make_evaluator(group, {})
        healthy, per_ups = ev.evaluate_once()
        assert healthy == 0
        assert all(h == UPSHealth.UNKNOWN for h in per_ups.values())
        executor.shutdown.assert_called_once()

    @pytest.mark.unit
    def test_min_healthy_two_of_three_tolerates_one_failure(self):
        group = _redundancy_group(
            ups_sources=["A", "B", "C"], min_healthy=2,
        )
        monitors = {
            "A": _FakeMonitor("A", _snap(trigger_active=True,
                                          trigger_reason="x")),
            "B": _FakeMonitor("B", _snap()),
            "C": _FakeMonitor("C", _snap()),
        }
        ev, executor = self._make_evaluator(group, monitors)
        healthy, _ = ev.evaluate_once()
        assert healthy == 2
        executor.shutdown.assert_not_called()

    @pytest.mark.unit
    def test_min_healthy_two_of_three_fires_on_two_failures(self):
        group = _redundancy_group(
            ups_sources=["A", "B", "C"], min_healthy=2,
        )
        monitors = {
            "A": _FakeMonitor("A", _snap(trigger_active=True,
                                          trigger_reason="x")),
            "B": _FakeMonitor("B", _snap(connection_state="FAILED")),
            "C": _FakeMonitor("C", _snap()),
        }
        ev, executor = self._make_evaluator(group, monitors)
        healthy, _ = ev.evaluate_once()
        assert healthy == 1
        executor.shutdown.assert_called_once()

    @pytest.mark.unit
    def test_transient_stale_members_count_degraded_not_unknown(self):
        group = _redundancy_group(
            min_healthy=1,
            unknown_counts_as="critical",
            degraded_counts_as="healthy",
        )
        old = time.time() - 10
        monitors = {
            "UPS-A": _FakeMonitor("UPS-A", _snap(
                last_update_time=old,
                stale_data_count=1,
            )),
            "UPS-B": _FakeMonitor("UPS-B", _snap(
                last_update_time=old,
                stale_data_count=2,
            )),
        }
        ev, executor = self._make_evaluator(group, monitors)
        healthy, per_ups = ev.evaluate_once()
        assert healthy == 2
        assert per_ups == {
            "UPS-A": UPSHealth.DEGRADED,
            "UPS-B": UPSHealth.DEGRADED,
        }
        executor.shutdown.assert_not_called()

    @pytest.mark.unit
    def test_grace_period_members_count_degraded_until_grace_expires(self):
        group = _redundancy_group(
            min_healthy=1,
            unknown_counts_as="critical",
            degraded_counts_as="healthy",
        )
        old = time.time() - 30
        grace_started = time.time() - 10
        monitors = {
            "UPS-A": _FakeMonitor("UPS-A", _snap(
                last_update_time=old,
                connection_state="GRACE_PERIOD",
                stale_data_count=3,
                connection_lost_time=grace_started,
            )),
            "UPS-B": _FakeMonitor("UPS-B", _snap(
                last_update_time=old,
                connection_state="GRACE_PERIOD",
                stale_data_count=3,
                connection_lost_time=grace_started,
            )),
        }
        ev, executor = self._make_evaluator(group, monitors)
        healthy, per_ups = ev.evaluate_once()
        assert healthy == 2
        assert per_ups == {
            "UPS-A": UPSHealth.DEGRADED,
            "UPS-B": UPSHealth.DEGRADED,
        }
        executor.shutdown.assert_not_called()

    @pytest.mark.unit
    def test_grace_period_members_turn_unknown_after_grace_expiry(self):
        group = _redundancy_group(
            min_healthy=1,
            unknown_counts_as="critical",
            degraded_counts_as="healthy",
        )
        old = time.time() - 90
        grace_started = time.time() - 61
        monitors = {
            "UPS-A": _FakeMonitor("UPS-A", _snap(
                last_update_time=old,
                connection_state="GRACE_PERIOD",
                stale_data_count=3,
                connection_lost_time=grace_started,
            )),
            "UPS-B": _FakeMonitor("UPS-B", _snap(
                last_update_time=old,
                connection_state="GRACE_PERIOD",
                stale_data_count=3,
                connection_lost_time=grace_started,
            )),
        }
        ev, executor = self._make_evaluator(group, monitors)
        healthy, per_ups = ev.evaluate_once()
        assert healthy == 0
        assert per_ups == {
            "UPS-A": UPSHealth.UNKNOWN,
            "UPS-B": UPSHealth.UNKNOWN,
        }
        executor.shutdown.assert_called_once()

    @pytest.mark.unit
    def test_in_flight_slow_poll_counts_degraded_inside_grace_window(self):
        group = _redundancy_group(
            min_healthy=1,
            unknown_counts_as="critical",
            degraded_counts_as="healthy",
        )
        old = time.time() - 30
        monitors = {
            "UPS-A": _FakeMonitor("UPS-A", _snap(last_update_time=old)),
            "UPS-B": _FakeMonitor("UPS-B", _snap(last_update_time=old)),
        }
        ev, executor = self._make_evaluator(group, monitors)
        healthy, per_ups = ev.evaluate_once()
        assert healthy == 2
        assert per_ups == {
            "UPS-A": UPSHealth.DEGRADED,
            "UPS-B": UPSHealth.DEGRADED,
        }
        executor.shutdown.assert_not_called()

    @pytest.mark.unit
    def test_failed_after_grace_still_counts_unknown_and_fires(self):
        group = _redundancy_group(min_healthy=1, unknown_counts_as="critical")
        old = time.time() - 30
        monitors = {
            "UPS-A": _FakeMonitor("UPS-A", _snap(
                last_update_time=old,
                connection_state="FAILED",
                stale_data_count=3,
            )),
            "UPS-B": _FakeMonitor("UPS-B", _snap(
                last_update_time=old,
                connection_state="FAILED",
                stale_data_count=3,
            )),
        }
        ev, executor = self._make_evaluator(group, monitors)
        healthy, per_ups = ev.evaluate_once()
        assert healthy == 0
        assert per_ups == {
            "UPS-A": UPSHealth.UNKNOWN,
            "UPS-B": UPSHealth.UNKNOWN,
        }
        executor.shutdown.assert_called_once()


class TestEvaluatorPolicy:
    """``degraded_counts_as`` / ``unknown_counts_as`` translation."""

    def _drive(self, group, snaps_by_name):
        monitors = {n: _FakeMonitor(n, s) for n, s in snaps_by_name.items()}
        executor = MagicMock()
        executor.shutdown.return_value = True
        ev = RedundancyGroupEvaluator(
            group, monitors, executor,
            stop_event=threading.Event(), logger=None,
        )
        return ev.evaluate_once(), executor

    @pytest.mark.unit
    def test_degraded_counts_as_healthy_default(self):
        group = _redundancy_group(min_healthy=1, degraded_counts_as="healthy")
        (healthy, _), executor = self._drive(group, {
            "UPS-A": _snap(status="OB DISCHRG"),  # DEGRADED
            "UPS-B": _snap(status="OB DISCHRG"),  # DEGRADED
        })
        assert healthy == 2  # both DEGRADED count as healthy
        executor.shutdown.assert_not_called()

    @pytest.mark.unit
    def test_degraded_counts_as_critical_strict(self):
        group = _redundancy_group(min_healthy=1, degraded_counts_as="critical")
        (healthy, _), executor = self._drive(group, {
            "UPS-A": _snap(status="OB DISCHRG"),
            "UPS-B": _snap(status="OB DISCHRG"),
        })
        assert healthy == 0
        executor.shutdown.assert_called_once()

    @pytest.mark.unit
    def test_unknown_counts_as_critical_default(self):
        group = _redundancy_group(min_healthy=1, unknown_counts_as="critical")
        (healthy, _), executor = self._drive(group, {
            "UPS-A": _snap(connection_state="FAILED"),
            "UPS-B": _snap(connection_state="FAILED"),
        })
        assert healthy == 0
        executor.shutdown.assert_called_once()

    @pytest.mark.unit
    def test_unknown_counts_as_degraded_routes_via_degraded_policy(self):
        # unknown→degraded with degraded_counts_as=healthy → both count healthy.
        group = _redundancy_group(
            min_healthy=1,
            unknown_counts_as="degraded",
            degraded_counts_as="healthy",
        )
        (healthy, _), executor = self._drive(group, {
            "UPS-A": _snap(connection_state="FAILED"),
            "UPS-B": _snap(connection_state="FAILED"),
        })
        assert healthy == 2
        executor.shutdown.assert_not_called()

    @pytest.mark.unit
    def test_unknown_counts_as_healthy_risky(self):
        group = _redundancy_group(min_healthy=1, unknown_counts_as="healthy")
        (healthy, _), executor = self._drive(group, {
            "UPS-A": _snap(connection_state="FAILED"),
            "UPS-B": _snap(connection_state="FAILED"),
        })
        assert healthy == 2
        executor.shutdown.assert_not_called()


class TestEvaluatorIdempotency:

    def _make(self, group, monitors_by_name):
        executor = MagicMock()
        executor.shutdown.return_value = True
        ev = RedundancyGroupEvaluator(
            group, monitors_by_name, executor,
            stop_event=threading.Event(), logger=None,
        )
        return ev, executor

    @pytest.mark.unit
    def test_executor_called_once_even_with_repeated_quorum_loss(self):
        group = _redundancy_group(min_healthy=1)
        monitors = {
            "UPS-A": _FakeMonitor("UPS-A", _snap(trigger_active=True, trigger_reason="x")),
            "UPS-B": _FakeMonitor("UPS-B", _snap(trigger_active=True, trigger_reason="x")),
        }
        ev, executor = self._make(group, monitors)
        for _ in range(5):
            ev.evaluate_once()
        executor.shutdown.assert_called_once()

    @pytest.mark.unit
    def test_recovery_clears_executor_state_even_when_never_fired(self):
        """5.3.0 contract regression: when ``shutdown()`` is suppressed
        by a stale flag (CodeRabbit P1 #2), ``_fired`` never flips to
        True. Pre-fix, the recovery branch only cleared executor state
        when ``_fired`` was True, so the executor's ``_shutdown_done``
        stayed latched and silently blocked every subsequent quorum
        loss for the rest of the daemon's life. The recovery branch
        must always call ``clear_shutdown_state()``.
        """
        group = _redundancy_group(min_healthy=1)
        monitors = {
            "UPS-A": _FakeMonitor("UPS-A", _snap(trigger_active=True, trigger_reason="x")),
            "UPS-B": _FakeMonitor("UPS-B", _snap(trigger_active=True, trigger_reason="x")),
        }
        executor = MagicMock()
        executor.shutdown.return_value = False  # simulate stale-flag suppression
        ev = RedundancyGroupEvaluator(
            group, monitors, executor,
            stop_event=threading.Event(), logger=None,
        )
        ev.evaluate_once()  # quorum lost, shutdown suppressed
        assert ev._fired is False  # never flipped because shutdown returned False
        executor.clear_shutdown_state.assert_not_called()
        # Both back to healthy -- recovery must clear executor state
        # even though _fired is False.
        for name in ("UPS-A", "UPS-B"):
            with monitors[name].state._lock:
                monitors[name].state.trigger_active = False
                monitors[name].state.trigger_reason = ""
                monitors[name].state.latest_status = "OL"
        ev.evaluate_once()
        executor.clear_shutdown_state.assert_called_once()

    @pytest.mark.unit
    def test_quorum_recovery_re_arms_for_next_event(self):
        """5.3.0 contract: recovery re-arms the evaluator + executor.

        Pre-5.3.0 the evaluator stayed pinned at ``_fired = True`` after
        the first quorum loss, so subsequent quorum losses silently
        no-op'd. With the per-UPS bug-#4 analog now wired in, the
        evaluator clears its own ``_fired`` AND calls
        ``executor.clear_shutdown_state()`` on the lost->recovered
        transition.
        """
        group = _redundancy_group(min_healthy=1)
        monitors = {
            "UPS-A": _FakeMonitor("UPS-A", _snap(trigger_active=True, trigger_reason="x")),
            "UPS-B": _FakeMonitor("UPS-B", _snap(trigger_active=True, trigger_reason="x")),
        }
        ev, executor = self._make(group, monitors)
        ev.evaluate_once()  # fires once
        assert ev._fired is True
        # Snap both back to healthy -- quorum recovers
        for name in ("UPS-A", "UPS-B"):
            with monitors[name].state._lock:
                monitors[name].state.trigger_active = False
                monitors[name].state.trigger_reason = ""
                monitors[name].state.latest_status = "OL"
        ev.evaluate_once()  # recovery re-arms
        executor.clear_shutdown_state.assert_called_once()
        assert ev._fired is False
        # Both drop again -- second quorum loss must fire a new shutdown
        for name in ("UPS-A", "UPS-B"):
            with monitors[name].state._lock:
                monitors[name].state.trigger_active = True
                monitors[name].state.trigger_reason = "x"
                monitors[name].state.latest_status = "OB FSD"
        ev.evaluate_once()
        assert executor.shutdown.call_count == 2
        assert ev._fired is True


class TestEvaluatorThreadLifecycle:

    @pytest.mark.unit
    def test_evaluator_thread_starts_and_stops_cleanly(self):
        group = _redundancy_group(min_healthy=1)
        monitors = {
            "UPS-A": _FakeMonitor("UPS-A", _snap()),
            "UPS-B": _FakeMonitor("UPS-B", _snap()),
        }
        executor = MagicMock()
        executor.shutdown.return_value = True
        stop_event = threading.Event()
        ev = RedundancyGroupEvaluator(
            group, monitors, executor,
            stop_event=stop_event, logger=None, tick=0.05,
            startup_grace_seconds=0,  # bypass startup grace for fast tests
        )
        ev.start()
        time.sleep(0.15)  # let it tick a couple of times
        stop_event.set()
        ev.join(timeout=2)
        assert not ev.is_alive()
        executor.shutdown.assert_not_called()  # quorum was healthy

    @pytest.mark.unit
    def test_evaluator_swallows_exceptions_and_continues(self):
        """A monitor that raises during snapshot() doesn't kill the evaluator."""
        group = _redundancy_group(min_healthy=1)
        bad_monitor = _FakeMonitor("UPS-A", _snap())
        bad_monitor.state = MagicMock()
        bad_monitor.state.snapshot.side_effect = RuntimeError("boom")
        monitors = {"UPS-A": bad_monitor, "UPS-B": _FakeMonitor("UPS-B", _snap())}

        stop_event = threading.Event()
        ev = RedundancyGroupEvaluator(
            group, monitors, MagicMock(),
            stop_event=stop_event, logger=None, tick=0.05,
            startup_grace_seconds=0,
        )
        ev.start()
        time.sleep(0.15)
        # F3: assert the evaluator thread survived the exception storm
        # BEFORE we signal stop. A thread killed by the unhandled
        # RuntimeError would still leave `not ev.is_alive()` True after
        # join (a dead thread joins instantly), so the original test
        # would silently pass even if the swallowing contract broke.
        assert ev.is_alive()
        stop_event.set()
        ev.join(timeout=2)
        assert not ev.is_alive()

    @pytest.mark.unit
    def test_evaluator_startup_grace_default_uses_check_interval(self):
        """Default grace is ``5 * max(check_interval) + 5`` across members."""
        group = _redundancy_group(min_healthy=1)
        monitors = {
            "UPS-A": _FakeMonitor("UPS-A", _snap(), check_interval=2),
            "UPS-B": _FakeMonitor("UPS-B", _snap(), check_interval=4),
        }
        ev = RedundancyGroupEvaluator(
            group, monitors, MagicMock(),
            stop_event=threading.Event(), logger=None,
        )
        # max(2, 4) * 5 + 5 = 25.
        assert ev._startup_grace == pytest.approx(25.0)

    @pytest.mark.unit
    def test_evaluator_startup_grace_explicit_override(self):
        ev = RedundancyGroupEvaluator(
            _redundancy_group(),
            {}, MagicMock(),
            stop_event=threading.Event(), logger=None,
            startup_grace_seconds=42.0,
        )
        assert ev._startup_grace == 42.0

    @pytest.mark.unit
    def test_evaluator_startup_grace_prevents_spurious_unknown_fire(self):
        """Regression: at start of run() every member is UNKNOWN
        (last_update_time=0). With unknown_counts_as=critical the evaluator
        WOULD fire shutdown -- but the startup grace must hold it off long
        enough for monitors to publish their initial snapshots."""
        group = _redundancy_group(min_healthy=1, unknown_counts_as="critical")
        # Build monitors whose snapshots have last_update_time=0 (no poll yet).
        monitors = {
            "UPS-A": _FakeMonitor("UPS-A", _snap(last_update_time=0)),
            "UPS-B": _FakeMonitor("UPS-B", _snap(last_update_time=0)),
        }
        executor = MagicMock()
        executor.shutdown.return_value = True
        stop_event = threading.Event()
        ev = RedundancyGroupEvaluator(
            group, monitors, executor,
            stop_event=stop_event, logger=None, tick=0.05,
            startup_grace_seconds=0.5,  # short grace for the test
        )
        ev.start()
        # Inside the grace window: no evaluation yet -> shutdown not called.
        time.sleep(0.1)
        executor.shutdown.assert_not_called()
        # Before the grace expires, swap in healthy snapshots for both members.
        for m in monitors.values():
            with m.state._lock:
                m.state.latest_update_time = time.time()
                m.state.latest_status = "OL"
        # Wait past the grace + a couple of ticks.
        time.sleep(0.6)
        stop_event.set()
        ev.join(timeout=2)
        executor.shutdown.assert_not_called()

    @pytest.mark.unit
    def test_cold_start_holds_fire_until_members_report(self):
        """H1: present members that have NEVER reported (last_update_time==0)
        must not drop a powered rack while inside the readiness window, even
        with unknown_counts_as=critical and quorum numerically lost."""
        group = _redundancy_group(
            min_healthy=2, unknown_counts_as="critical",
            ups_sources=["A", "B", "C"],
        )
        monitors = {
            "A": _FakeMonitor("A", _snap()),                     # reported, healthy
            "B": _FakeMonitor("B", _snap(last_update_time=0)),   # never reported
            "C": _FakeMonitor("C", _snap(last_update_time=0)),   # never reported
        }
        executor = MagicMock()
        executor.shutdown.return_value = True
        ev = RedundancyGroupEvaluator(
            group, monitors, executor,
            stop_event=threading.Event(), logger=None,
            startup_grace_seconds=0,
        )
        # healthy_count=1 < min_healthy=2, but B/C never reported and we're in
        # the readiness window -> hold, no fire.
        ev.evaluate_once()
        executor.shutdown.assert_not_called()
        # Once B and C publish their first (healthy) snapshot, quorum holds.
        for n in ("B", "C"):
            with monitors[n].state._lock:
                monitors[n].state.latest_update_time = time.time()
                monitors[n].state.latest_status = "OL"
        ev.evaluate_once()
        executor.shutdown.assert_not_called()

    @pytest.mark.unit
    def test_cold_start_hold_expires_then_fires(self):
        """H1: once the readiness window elapses, a still-never-reported member
        counts as UNKNOWN->critical and a real quorum loss fires -- a genuinely
        dead UPS at boot is still protected."""
        group = _redundancy_group(
            min_healthy=2, unknown_counts_as="critical",
            ups_sources=["A", "B"],
        )
        monitors = {
            "A": _FakeMonitor("A", _snap()),                    # reported healthy
            "B": _FakeMonitor("B", _snap(last_update_time=0)),  # never reports
        }
        executor = MagicMock()
        executor.shutdown.return_value = True
        ev = RedundancyGroupEvaluator(
            group, monitors, executor,
            stop_event=threading.Event(), logger=None,
            startup_grace_seconds=0,
        )
        ev._readiness_window = 0  # force the readiness window to have elapsed
        ev.evaluate_once()
        executor.shutdown.assert_called_once()

    @pytest.mark.unit
    def test_readiness_window_excludes_disabled_grace(self):
        """cubic: a member whose connection grace is DISABLED must not extend the
        cold-start readiness window (a disabled grace doesn't delay FAILED)."""
        group = _redundancy_group(min_healthy=1)
        monitors = {
            "UPS-A": _FakeMonitor("UPS-A", _snap()),
            "UPS-B": _FakeMonitor("UPS-B", _snap()),
        }
        for m in monitors.values():
            m.config.ups.connection_loss_grace_period.enabled = False
        ev = RedundancyGroupEvaluator(
            group, monitors, MagicMock(),
            stop_event=threading.Event(), logger=None, startup_grace_seconds=10,
        )
        # base_grace excluded (disabled) -> window = startup_grace(10) + 0 + 10.
        assert ev._readiness_window == 20


# ===========================================================================
# Executor: synthetic config wiring + idempotency + dry-run cleanup
# ===========================================================================

class TestExecutorConstruction:

    @pytest.mark.unit
    def test_synthetic_config_wraps_group_resources(self, tmp_path):
        group = _redundancy_group(
            remote_servers=[RemoteServerConfig(
                name="srv", enabled=True, host="10.0.0.1", user="root",
            )],
        )
        ex = RedundancyGroupExecutor(
            group, base_config=_base_config(tmp_path=tmp_path),
        )
        assert ex.config.behavior.dry_run is True
        assert len(ex.config.remote_servers) == 1
        assert ex.config.remote_servers[0].host == "10.0.0.1"
        # Local resources mirror the group (defaults)
        assert ex.config.virtual_machines.enabled is False
        assert ex.config.containers.enabled is False

    @pytest.mark.unit
    def test_flag_path_uses_redundancy_namespace(self, tmp_path):
        group = _redundancy_group(name="rack-1")
        ex = RedundancyGroupExecutor(
            group, base_config=_base_config(tmp_path=tmp_path),
        )
        assert ex._shutdown_flag_path.name == "ups-shutdown-redundancy-rack-1"
        assert ex._shutdown_flag_path.parent == tmp_path

    @pytest.mark.unit
    def test_sanitization_replaces_at_and_colon_in_flag_name(self, tmp_path):
        group = _redundancy_group(name="rack@host:1/2")
        ex = RedundancyGroupExecutor(
            group, base_config=_base_config(tmp_path=tmp_path),
        )
        assert "rack-host-1-2" in ex._shutdown_flag_path.name


class TestExecutorShutdown:

    def _make(self, *, group=None, tmp_path: Path):
        group = group or _redundancy_group()
        return RedundancyGroupExecutor(
            group,
            base_config=_base_config(tmp_path=tmp_path),
            log_prefix="[test] ",
        ), group

    @pytest.mark.unit
    def test_dry_run_clears_flag_and_marks_completed(self, tmp_path):
        ex, _ = self._make(tmp_path=tmp_path)
        assert ex.shutdown(reason="quorum lost") is True
        # Flag is cleared in dry-run so reruns are possible
        assert not ex._shutdown_flag_path.exists()

    @pytest.mark.unit
    def test_idempotent_within_process(self, tmp_path):
        # Disable dry-run so the flag persists across calls.
        cfg = _base_config(dry_run=False, tmp_path=tmp_path)
        ex = RedundancyGroupExecutor(_redundancy_group(), base_config=cfg)
        assert ex.shutdown(reason="x") is True
        assert ex.shutdown(reason="x") is False  # second call is a no-op

    @pytest.mark.unit
    def test_idempotent_against_existing_flag_file(self, tmp_path):
        # Defense-in-depth: even though the 5.3.0 contract has the
        # coordinator clear the flag at startup, the executor itself
        # still refuses to re-fire if it observes a flag at first call.
        # This covers the contract-violation case (flag owned by another
        # user, /var/run remounted RO mid-run, manual touch).
        cfg = _base_config(dry_run=False, tmp_path=tmp_path)
        ex = RedundancyGroupExecutor(_redundancy_group(), base_config=cfg)
        # Pre-create the flag file as if a previous Eneru run left it behind.
        ex._shutdown_flag_path.touch()
        assert ex.shutdown(reason="x") is False
        assert ex._shutdown_done is True

    @pytest.mark.unit
    def test_stale_flag_emits_warning_log(self, tmp_path):
        """5.3.0: the silent no-op path now surfaces a warning so the
        operator sees what the pre-5.3.0 silent suppression hid."""
        cfg = _base_config(dry_run=False, tmp_path=tmp_path)
        ex = RedundancyGroupExecutor(_redundancy_group(name="rg-x"),
                                      base_config=cfg)
        ex.logger = MagicMock()
        ex._shutdown_flag_path.touch()  # simulate startup-cleanup bypassed
        assert ex.shutdown(reason="x") is False
        logged = " ".join(call.args[0] for call in ex.logger.log.call_args_list)
        assert "suppressed" in logged
        assert "rg-x" in logged
        assert "startup cleanup bypassed" in logged

    @pytest.mark.unit
    def test_clear_shutdown_state_unlinks_flag_and_resets_done(self, tmp_path):
        cfg = _base_config(dry_run=False, tmp_path=tmp_path)
        # Explicit is_local=False so this test stays correct even if the
        # _redundancy_group() default ever flips -- the local resource
        # mixins (_shutdown_vms, _unmount_filesystems) would otherwise
        # try to run for real with dry_run=False.
        ex = RedundancyGroupExecutor(_redundancy_group(is_local=False),
                                     base_config=cfg)
        # Fire a shutdown so the flag is on disk and _shutdown_done is True.
        assert ex.shutdown(reason="x") is True
        assert ex._shutdown_flag_path.exists()
        assert ex._shutdown_done is True
        # Clear: both must be reset.
        ex.clear_shutdown_state()
        assert not ex._shutdown_flag_path.exists()
        assert ex._shutdown_done is False
        # Re-fire is now possible.
        assert ex.shutdown(reason="y") is True

    @pytest.mark.unit
    def test_clear_shutdown_state_is_idempotent(self, tmp_path):
        cfg = _base_config(dry_run=False, tmp_path=tmp_path)
        ex = RedundancyGroupExecutor(_redundancy_group(), base_config=cfg)
        # No fire yet -- flag absent. Clear must not raise.
        ex.clear_shutdown_state()
        ex.clear_shutdown_state()
        assert not ex._shutdown_flag_path.exists()
        assert ex._shutdown_done is False

    @pytest.mark.unit
    def test_clear_shutdown_state_refuses_running_peer_pid(self, tmp_path):
        cfg = _base_config(dry_run=False, tmp_path=tmp_path)
        ex = RedundancyGroupExecutor(_redundancy_group(), base_config=cfg)
        ex._shutdown_flag_path.write_text(f"pid={_IMPOSSIBLE_PID}\n")

        with patch.object(ex, "_pid_is_running", return_value=True):
            with pytest.raises(RuntimeError, match=f"running PID {_IMPOSSIBLE_PID}"):
                ex.clear_shutdown_state(refuse_active_peer=True)

        assert ex._shutdown_flag_path.exists()

    @pytest.mark.unit
    def test_clear_shutdown_state_removes_stale_peer_pid(self, tmp_path):
        cfg = _base_config(dry_run=False, tmp_path=tmp_path)
        ex = RedundancyGroupExecutor(_redundancy_group(), base_config=cfg)
        ex._shutdown_flag_path.write_text(f"pid={_IMPOSSIBLE_PID}\n")

        with patch.object(ex, "_pid_is_running", return_value=False):
            ex.clear_shutdown_state(refuse_active_peer=True)

        assert not ex._shutdown_flag_path.exists()

    @pytest.mark.unit
    def test_clear_shutdown_state_removes_reused_pid_identity(self, tmp_path):
        cfg = _base_config(dry_run=False, tmp_path=tmp_path)
        ex = RedundancyGroupExecutor(_redundancy_group(), base_config=cfg)
        ex._shutdown_flag_path.write_text(
            f"pid={_IMPOSSIBLE_PID}\nstart_time=old\nboot_id=boot\n"
        )

        with patch("eneru.redundancy.os.kill", return_value=None), \
             patch.object(ex, "_read_proc_start_time", return_value="new"), \
             patch.object(ex, "_read_boot_id", return_value="boot"):
            ex.clear_shutdown_state(refuse_active_peer=True)

        assert not ex._shutdown_flag_path.exists()

    @pytest.mark.unit
    def test_clear_shutdown_state_ignores_invalid_pid_owner(self, tmp_path):
        cfg = _base_config(dry_run=False, tmp_path=tmp_path)
        ex = RedundancyGroupExecutor(_redundancy_group(), base_config=cfg)
        ex._shutdown_flag_path.write_text("pid=not-a-number\n")

        ex.clear_shutdown_state(refuse_active_peer=True)

        assert not ex._shutdown_flag_path.exists()

    @pytest.mark.unit
    def test_clear_shutdown_state_probes_flag_directory_access(self, tmp_path):
        cfg = _base_config(dry_run=False, tmp_path=tmp_path)
        ex = RedundancyGroupExecutor(_redundancy_group(), base_config=cfg)

        with patch("eneru.redundancy.os.open", side_effect=PermissionError("denied")):
            with pytest.raises(PermissionError, match="denied"):
                ex.clear_shutdown_state(refuse_active_peer=True)

    @pytest.mark.unit
    def test_read_shutdown_flag_pid_handles_missing_and_invalid_values(self, tmp_path):
        cfg = _base_config(dry_run=False, tmp_path=tmp_path)
        ex = RedundancyGroupExecutor(_redundancy_group(), base_config=cfg)

        assert ex._read_shutdown_flag_pid() is None
        ex._shutdown_flag_path.write_text("pid=not-a-number\n")
        assert ex._read_shutdown_flag_pid() is None
        ex._shutdown_flag_path.write_text("created_at=1\n")
        assert ex._read_shutdown_flag_pid() is None

    @pytest.mark.unit
    def test_pid_liveness_handles_missing_process_and_permission(self, tmp_path):
        cfg = _base_config(dry_run=False, tmp_path=tmp_path)
        ex = RedundancyGroupExecutor(_redundancy_group(), base_config=cfg)

        assert ex._pid_is_running(0) is False
        with patch("eneru.redundancy.os.kill", side_effect=ProcessLookupError):
            assert ex._pid_is_running(_IMPOSSIBLE_PID) is False
        with patch("eneru.redundancy.os.kill", side_effect=PermissionError):
            assert ex._pid_is_running(_IMPOSSIBLE_PID) is True

    @pytest.mark.unit
    def test_pid_liveness_rejects_mismatched_owner_identity(self, tmp_path):
        cfg = _base_config(dry_run=False, tmp_path=tmp_path)
        ex = RedundancyGroupExecutor(_redundancy_group(), base_config=cfg)

        with patch("eneru.redundancy.os.kill", return_value=None), \
             patch.object(ex, "_read_boot_id", return_value="current"):
            assert ex._pid_is_running(
                _IMPOSSIBLE_PID, boot_id="previous"
            ) is False

        with patch("eneru.redundancy.os.kill", return_value=None), \
             patch.object(ex, "_read_boot_id", return_value="boot"), \
             patch.object(ex, "_read_proc_start_time", return_value="new"):
            assert ex._pid_is_running(
                _IMPOSSIBLE_PID, start_time="old", boot_id="boot"
            ) is False

    @pytest.mark.unit
    def test_shutdown_flag_records_owner_identity(self, tmp_path):
        cfg = _base_config(dry_run=False, tmp_path=tmp_path)
        ex = RedundancyGroupExecutor(_redundancy_group(is_local=False),
                                     base_config=cfg)

        with patch.object(ex, "_read_proc_start_time", return_value="123"), \
             patch.object(ex, "_read_boot_id", return_value="boot"):
            assert ex.shutdown(reason="owner") is True

        content = ex._shutdown_flag_path.read_text()
        assert f"pid={os.getpid()}" in content
        assert "start_time=123" in content
        assert "boot_id=boot" in content

    @pytest.mark.unit
    def test_owner_identity_omits_unavailable_proc_fields(self, tmp_path):
        cfg = _base_config(dry_run=False, tmp_path=tmp_path)
        ex = RedundancyGroupExecutor(_redundancy_group(), base_config=cfg)

        with patch.object(ex, "_read_proc_start_time", return_value=None), \
             patch.object(ex, "_read_boot_id", return_value=None):
            assert ex._current_owner_identity() == {"pid": str(os.getpid())}

    @pytest.mark.unit
    def test_proc_start_time_parser_handles_missing_and_short_stat(self):
        assert RedundancyGroupExecutor._read_proc_start_time(_IMPOSSIBLE_PID) is None
        with patch("eneru.redundancy.Path.read_text", return_value="1 (x) S"):
            assert RedundancyGroupExecutor._read_proc_start_time(os.getpid()) is None

    @pytest.mark.unit
    def test_boot_id_reader_handles_unavailable_proc(self):
        with patch("eneru.redundancy.Path.read_text", side_effect=PermissionError):
            assert RedundancyGroupExecutor._read_boot_id() is None

    @pytest.mark.unit
    def test_atomic_flag_acquisition_allows_only_one_executor(self, tmp_path):
        cfg = _base_config(dry_run=False, tmp_path=tmp_path)
        group = _redundancy_group(is_local=False)
        ex_a = RedundancyGroupExecutor(group, base_config=cfg)
        ex_b = RedundancyGroupExecutor(group, base_config=cfg)

        assert ex_a.shutdown(reason="first") is True
        assert ex_b.shutdown(reason="second") is False
        assert ex_b._shutdown_done is True
        assert f"pid={os.getpid()}" in ex_a._shutdown_flag_path.read_text()

    @pytest.mark.unit
    def test_remote_shutdown_called_in_dry_run(self, tmp_path):
        group = _redundancy_group(
            remote_servers=[RemoteServerConfig(
                name="srv", enabled=True, host="10.0.0.1", user="root",
            )],
        )
        ex, _ = self._make(group=group, tmp_path=tmp_path)
        with pytest.MonkeyPatch.context() as mp:
            calls = []
            mp.setattr(ex, "_shutdown_remote_servers",
                       lambda: calls.append("remote"))
            ex.shutdown(reason="x")
        assert calls == ["remote"]

    @pytest.mark.unit
    def test_local_resources_skipped_when_not_local(self, tmp_path):
        group = _redundancy_group(
            is_local=False,
            virtual_machines=VMConfig(enabled=True),
        )
        ex, _ = self._make(group=group, tmp_path=tmp_path)
        with pytest.MonkeyPatch.context() as mp:
            vm_calls, ct_calls, fs_calls = [], [], []
            mp.setattr(ex, "_shutdown_vms",
                       lambda: vm_calls.append(1))
            mp.setattr(ex, "_shutdown_containers",
                       lambda: ct_calls.append(1))
            mp.setattr(ex, "_sync_filesystems",
                       lambda: fs_calls.append(1))
            mp.setattr(ex, "_unmount_filesystems",
                       lambda: fs_calls.append(2))
            mp.setattr(ex, "_shutdown_remote_servers", lambda: None)
            ex.shutdown(reason="x")
        assert vm_calls == [] and ct_calls == [] and fs_calls == []

    @pytest.mark.unit
    def test_local_resources_invoked_when_is_local(self, tmp_path):
        group = _redundancy_group(
            is_local=True,
            virtual_machines=VMConfig(enabled=True),
            containers=ContainersConfig(enabled=True),
            filesystems=FilesystemsConfig(
                sync_enabled=True,
                unmount=UnmountConfig(enabled=False),
            ),
        )
        ex, _ = self._make(group=group, tmp_path=tmp_path)
        with pytest.MonkeyPatch.context() as mp:
            calls = []
            mp.setattr(ex, "_shutdown_vms",
                       lambda: calls.append("vms"))
            mp.setattr(ex, "_shutdown_containers",
                       lambda: calls.append("containers"))
            mp.setattr(ex, "_sync_filesystems",
                       lambda: calls.append("sync"))
            mp.setattr(ex, "_unmount_filesystems",
                       lambda: calls.append("unmount"))
            mp.setattr(ex, "_shutdown_remote_servers",
                       lambda: calls.append("remote"))
            ex.shutdown(reason="x")
        assert calls == ["vms", "containers", "sync", "unmount", "remote"]

    @pytest.mark.unit
    def test_loopback_delegate_skips_local_phases_and_callback(self, tmp_path):
        """Containerized local redundancy groups delegate host work to SSH.

        The remote phase must still run because that is where the loopback
        executes generated VM/container/filesystem actions. The in-process
        local phases and local shutdown callback would duplicate host work.
        """
        callback = MagicMock()
        group = _redundancy_group(
            is_local=True,
            virtual_machines=VMConfig(enabled=True),
            containers=ContainersConfig(enabled=True),
            filesystems=FilesystemsConfig(
                sync_enabled=True,
                unmount=UnmountConfig(enabled=True),
            ),
            remote_servers=[RemoteServerConfig(
                name="host-loopback",
                enabled=True,
                host="127.0.0.1",
                user="root",
                is_host_loopback=True,
            )],
        )
        ex = RedundancyGroupExecutor(
            group,
            base_config=_base_config(tmp_path=tmp_path),
            local_shutdown_callback=callback,
        )

        with pytest.MonkeyPatch.context() as mp:
            calls = []
            mp.setattr("eneru.cli._detect_runtime_context",
                       lambda: "container (Docker)")
            mp.setattr(ex, "_shutdown_vms", lambda: calls.append("vms"))
            mp.setattr(ex, "_shutdown_containers",
                       lambda: calls.append("containers"))
            mp.setattr(ex, "_sync_filesystems", lambda: calls.append("sync"))
            mp.setattr(ex, "_unmount_filesystems",
                       lambda: calls.append("unmount"))
            mp.setattr(ex, "_shutdown_remote_servers",
                       lambda: calls.append("remote"))
            assert ex.shutdown(reason="x") is True

        assert calls == ["remote"]
        callback.assert_not_called()

    @pytest.mark.unit
    def test_logging_uses_prefix(self, tmp_path):
        ex, _ = self._make(tmp_path=tmp_path)
        ex.logger = MagicMock()
        ex._log_message("hello")
        ex.logger.log.assert_called_with("[test] hello")

    @pytest.mark.unit
    def test_send_notification_no_op_without_worker(self, tmp_path):
        ex, _ = self._make(tmp_path=tmp_path)
        # Should not raise even when the worker is None.
        ex._send_notification("body", "info")

    @pytest.mark.unit
    def test_send_notification_escapes_at_for_discord_safety(self, tmp_path):
        ex, _ = self._make(tmp_path=tmp_path)
        ex._notification_worker = MagicMock()
        ex._send_notification("UPS-A@host failed", "warning")
        body = ex._notification_worker.send.call_args.kwargs["body"]
        assert "UPS-A@\u200Bhost failed" in body  # zero-width space inserted


# ===========================================================================
# Cascade: a UPS in both an independent UPS group AND a redundancy group
# ===========================================================================

class TestCascade:

    @pytest.mark.unit
    def test_independent_group_path_unaffected_by_redundancy(self):
        """Regression: when a UPS appears in both tiers, the independent
        UPS group's monitor still triggers via _trigger_immediate_shutdown
        when not flagged as in_redundancy_group. The redundancy evaluator
        operates only on monitors that *were* flagged in_redundancy_group."""
        # Two monitors, both representing the SAME UPS. One is flagged
        # in_redundancy (the redundancy-tier observer); the other is not.
        snap_critical = _snap(trigger_active=True, trigger_reason="x")
        rg_monitor = _FakeMonitor("UPS-A", snap_critical)
        # Build a redundancy group that only sees the rg-flagged monitor.
        group = _redundancy_group(min_healthy=1, ups_sources=["UPS-A", "UPS-B"])
        executor = MagicMock()
        executor.shutdown.return_value = True
        ev = RedundancyGroupEvaluator(
            group,
            {"UPS-A": rg_monitor, "UPS-B": _FakeMonitor("UPS-B", _snap())},
            executor,
            stop_event=threading.Event(), logger=None,
        )
        healthy, per_ups = ev.evaluate_once()
        # UPS-B remains healthy → quorum holds → executor not called.
        assert healthy == 1
        assert per_ups["UPS-A"] == UPSHealth.CRITICAL
        executor.shutdown.assert_not_called()


# ===========================================================================
# Edge cases: mixed-state groups + executor notification content
# ===========================================================================

class TestThreeStateMix:
    """A 3-UPS group with HEALTHY + DEGRADED + CRITICAL members at once.

    The earlier policy tests cover all-of-one-state. These pin the
    interaction between mixed states and ``degraded_counts_as``.
    """

    def _drive(self, group):
        monitors = {
            "A": _FakeMonitor("A", _snap()),                                    # HEALTHY
            "B": _FakeMonitor("B", _snap(status="OB DISCHRG")),                 # DEGRADED
            "C": _FakeMonitor("C", _snap(trigger_active=True,
                                          trigger_reason="low")),               # CRITICAL
        }
        executor = MagicMock()
        executor.shutdown.return_value = True
        ev = RedundancyGroupEvaluator(
            group, monitors, executor,
            stop_event=threading.Event(), logger=None,
            startup_grace_seconds=0,
        )
        return ev.evaluate_once(), executor

    @pytest.mark.unit
    def test_mix_with_degraded_as_healthy_yields_two_healthy(self):
        """DEGRADED→healthy: A + B both count, C does not. healthy_count = 2."""
        group = _redundancy_group(
            ups_sources=["A", "B", "C"],
            min_healthy=2,
            degraded_counts_as="healthy",
        )
        (healthy, per_ups), executor = self._drive(group)
        assert healthy == 2
        assert per_ups["A"] == UPSHealth.HEALTHY
        assert per_ups["B"] == UPSHealth.DEGRADED
        assert per_ups["C"] == UPSHealth.CRITICAL
        executor.shutdown.assert_not_called()  # quorum holds at 2

    @pytest.mark.unit
    def test_mix_with_degraded_as_critical_drops_to_one_healthy(self):
        """DEGRADED→critical: only A counts. healthy_count = 1, fires shutdown."""
        group = _redundancy_group(
            ups_sources=["A", "B", "C"],
            min_healthy=2,
            degraded_counts_as="critical",
        )
        (healthy, _), executor = self._drive(group)
        assert healthy == 1
        executor.shutdown.assert_called_once()


class TestExecutorNotificationContent:
    """The shutdown notification body actually carries useful detail."""

    @pytest.mark.unit
    def test_notification_body_includes_name_reason_and_sources(self, tmp_path):
        group = _redundancy_group(
            name="rack-7",
            ups_sources=["UPS-X@h", "UPS-Y@h"],
        )
        worker = MagicMock()
        ex = RedundancyGroupExecutor(
            group, base_config=_base_config(tmp_path=tmp_path),
            notification_worker=worker,
        )
        ex.shutdown(reason="quorum lost: healthy=0 < min_healthy=1")
        # Worker.send was called at least once for the headline alert.
        assert worker.send.called
        body = worker.send.call_args.kwargs["body"]
        # Group name, reason, and both sources must appear in the body.
        # (The @-escape inserts a zero-width space after each "@".)
        assert "rack-7" in body
        assert "quorum lost" in body
        assert "UPS-X@\u200Bh" in body
        assert "UPS-Y@\u200Bh" in body


class TestLocalShutdownCallback:
    """5.1.1 fix: an is_local redundancy group must invoke the
    coordinator's _handle_local_shutdown after the remote-shutdown
    phase. Without this, quorum loss completed cleanly with the local
    host still running."""

    @pytest.mark.unit
    def test_callback_fires_on_is_local_quorum_loss(self, tmp_path):
        callback = MagicMock()
        group = _redundancy_group(name="rack-local", is_local=True)
        ex = RedundancyGroupExecutor(
            group, base_config=_base_config(tmp_path=tmp_path),
            local_shutdown_callback=callback,
        )
        ex.shutdown(reason="quorum lost")
        callback.assert_called_once()
        # The reason carries the redundancy group's name so the
        # coordinator's defense-in-depth lock log can attribute it.
        assert "rack-local" in callback.call_args[0][0]

    @pytest.mark.unit
    def test_callback_NOT_invoked_for_non_local_group(self, tmp_path):
        # is_local=False is the typical "managed remote rack" case;
        # the local poweroff must NEVER fire on this path even when
        # a callback is wired up.
        callback = MagicMock()
        group = _redundancy_group(name="rack-remote", is_local=False)
        ex = RedundancyGroupExecutor(
            group, base_config=_base_config(tmp_path=tmp_path),
            local_shutdown_callback=callback,
        )
        ex.shutdown(reason="quorum lost")
        callback.assert_not_called()

    @pytest.mark.unit
    def test_callback_optional_no_crash_when_unset(self, tmp_path):
        # Single-UPS-coordinator-less setups won't wire a callback.
        # The executor must not raise when local_shutdown_callback=None.
        group = _redundancy_group(name="standalone", is_local=True)
        ex = RedundancyGroupExecutor(
            group, base_config=_base_config(tmp_path=tmp_path),
            # No local_shutdown_callback supplied -> defaults to None.
        )
        # Must not raise.
        assert ex.shutdown(reason="quorum lost") is True

    @pytest.mark.unit
    def test_callback_skipped_when_remote_shutdown_raises(self, tmp_path):
        # The callback is positioned AFTER _shutdown_remote_servers
        # inside the try-block, so an exception in remote shutdown
        # short-circuits the callback. The coordinator's monitor-side
        # path can still trigger local shutdown via its own lock; the
        # redundancy callback is a redundant signal in that scenario.
        callback = MagicMock()
        group = _redundancy_group(name="rack-local", is_local=True)
        ex = RedundancyGroupExecutor(
            group, base_config=_base_config(tmp_path=tmp_path),
            local_shutdown_callback=callback,
        )
        with pytest.MonkeyPatch.context() as mp:
            def boom():
                raise RuntimeError("ssh dead")
            mp.setattr(ex, "_shutdown_remote_servers", boom)
            ex.shutdown(reason="quorum lost")
        # Exception was caught by the executor's try/except; the
        # callback was NOT invoked because the raise happened before
        # the callback line in the try-block.
        callback.assert_not_called()
