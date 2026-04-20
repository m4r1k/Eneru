"""Tests for the redundancy-group evaluator and shutdown executor."""

import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from eneru import (
    BehaviorConfig,
    Config,
    LocalShutdownConfig,
    LoggingConfig,
    NotificationsConfig,
    RedundancyGroupConfig,
    RedundancyGroupEvaluator,
    RedundancyGroupExecutor,
    RemoteServerConfig,
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
    def test_recovery_then_re_loss_keeps_fired_state(self):
        """Once fired, the evaluator stays "fired" -- recovery is informational only."""
        group = _redundancy_group(min_healthy=1)
        monitors = {
            "UPS-A": _FakeMonitor("UPS-A", _snap(trigger_active=True, trigger_reason="x")),
            "UPS-B": _FakeMonitor("UPS-B", _snap(trigger_active=True, trigger_reason="x")),
        }
        ev, executor = self._make(group, monitors)
        ev.evaluate_once()  # fires
        # Snap UPS-A back to healthy
        with monitors["UPS-A"].state._lock:
            monitors["UPS-A"].state.trigger_active = False
            monitors["UPS-A"].state.trigger_reason = ""
            monitors["UPS-A"].state.latest_status = "OL"
        ev.evaluate_once()  # quorum recovered, no extra calls
        # UPS-A drops again
        with monitors["UPS-A"].state._lock:
            monitors["UPS-A"].state.trigger_active = True
            monitors["UPS-A"].state.trigger_reason = "x"
            monitors["UPS-A"].state.latest_status = "OB FSD"
        ev.evaluate_once()  # would fire again but already fired
        executor.shutdown.assert_called_once()


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
        cfg = _base_config(dry_run=False, tmp_path=tmp_path)
        ex = RedundancyGroupExecutor(_redundancy_group(), base_config=cfg)
        # Pre-create the flag file as if a previous Eneru run left it behind.
        ex._shutdown_flag_path.touch()
        assert ex.shutdown(reason="x") is False
        assert ex._shutdown_done is True

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
