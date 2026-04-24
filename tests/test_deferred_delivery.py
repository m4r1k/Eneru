"""Tests for ``src/eneru/deferred_delivery.py`` — the v5.2.1 systemd-run
based deferred-stop notification mechanism."""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from eneru.deferred_delivery import (
    DEFAULT_DEFER_SECS,
    _detect_systemd_stop_intent,
    _eager_send,
    _eneru_invocation_args,
    _running_under_systemd,
    deliver_pending_stop,
    schedule_deferred_stop_or_eager_send,
)
from eneru.stats import StatsStore


# ==============================================================================
# _eneru_invocation_args
# ==============================================================================

class TestEneruInvocationArgs:

    @pytest.mark.unit
    def test_prefers_deb_rpm_wrapper_when_present(self):
        """When /opt/ups-monitor/eneru.py exists, use it (no PYTHONPATH
        dependency). The deb/rpm wrapper sets sys.path explicitly."""
        with patch("eneru.deferred_delivery.os.path.exists",
                   return_value=True):
            args = _eneru_invocation_args()
        assert args[1] == "/opt/ups-monitor/eneru.py"

    @pytest.mark.unit
    def test_falls_back_to_python_dash_m_for_pip_installs(self):
        """No deb/rpm wrapper → use `python -m eneru` (works for pip
        and uv-venv installs)."""
        with patch("eneru.deferred_delivery.os.path.exists",
                   return_value=False):
            args = _eneru_invocation_args()
        assert args[1:] == ["-m", "eneru"]


# ==============================================================================
# _running_under_systemd
# ==============================================================================

class TestRunningUnderSystemd:

    @pytest.mark.unit
    def test_true_when_invocation_id_set(self):
        with patch.dict("os.environ", {"INVOCATION_ID": "abc123"}, clear=False):
            assert _running_under_systemd() is True

    @pytest.mark.unit
    def test_false_when_invocation_id_unset(self):
        env = {k: v for k, v in __import__("os").environ.items()
               if k != "INVOCATION_ID"}
        with patch.dict("os.environ", env, clear=True):
            assert _running_under_systemd() is False

    @pytest.mark.unit
    def test_false_when_invocation_id_empty(self):
        with patch.dict("os.environ", {"INVOCATION_ID": ""}, clear=False):
            assert _running_under_systemd() is False


# ==============================================================================
# _detect_systemd_stop_intent
# ==============================================================================

class TestDetectSystemdStopIntent:

    @pytest.mark.unit
    def test_returns_true_for_stop_job(self):
        result = MagicMock(returncode=0, stdout=b"Job=42:stop\n")
        with patch("eneru.deferred_delivery.subprocess.run",
                   return_value=result):
            assert _detect_systemd_stop_intent() is True

    @pytest.mark.unit
    def test_returns_false_for_restart_job(self):
        result = MagicMock(returncode=0, stdout=b"Job=42:restart\n")
        with patch("eneru.deferred_delivery.subprocess.run",
                   return_value=result):
            assert _detect_systemd_stop_intent() is False

    @pytest.mark.unit
    def test_returns_false_for_start_job(self):
        result = MagicMock(returncode=0, stdout=b"Job=42:start\n")
        with patch("eneru.deferred_delivery.subprocess.run",
                   return_value=result):
            assert _detect_systemd_stop_intent() is False

    @pytest.mark.unit
    def test_returns_false_for_no_job_queued(self):
        result = MagicMock(returncode=0, stdout=b"Job=\n")
        with patch("eneru.deferred_delivery.subprocess.run",
                   return_value=result):
            assert _detect_systemd_stop_intent() is False

    @pytest.mark.unit
    def test_returns_false_when_systemctl_missing(self):
        with patch("eneru.deferred_delivery.subprocess.run",
                   side_effect=FileNotFoundError):
            assert _detect_systemd_stop_intent() is False

    @pytest.mark.unit
    def test_returns_false_when_query_times_out(self):
        with patch("eneru.deferred_delivery.subprocess.run",
                   side_effect=subprocess.TimeoutExpired(
                       cmd="systemctl", timeout=2)):
            assert _detect_systemd_stop_intent() is False

    @pytest.mark.unit
    def test_returns_false_on_nonzero_exit(self):
        result = MagicMock(returncode=1, stdout=b"")
        with patch("eneru.deferred_delivery.subprocess.run",
                   return_value=result):
            assert _detect_systemd_stop_intent() is False

    @pytest.mark.unit
    def test_case_insensitive_on_job_type(self):
        result = MagicMock(returncode=0, stdout=b"Job=42:STOP\n")
        with patch("eneru.deferred_delivery.subprocess.run",
                   return_value=result):
            assert _detect_systemd_stop_intent() is True


# ==============================================================================
# schedule_deferred_stop_or_eager_send
# ==============================================================================

class TestScheduleDeferred:

    @pytest.fixture(autouse=True)
    def _under_systemd_no_stop_intent(self):
        """All TestScheduleDeferred tests assume the timer-scheduling
        path: under systemd AND not a `systemctl stop` (which is the
        v5.2.1 instant-eager short-circuit). Tests that exercise the
        short-circuit paths live in TestScheduleShortCircuits below."""
        with patch.dict("os.environ", {"INVOCATION_ID": "test-invocation"}), \
             patch("eneru.deferred_delivery._detect_systemd_stop_intent",
                   return_value=False):
            yield

    def _stub_run_success(self, cmd, **kwargs):
        result = MagicMock()
        result.returncode = 0
        result.stderr = b""
        return result

    @pytest.mark.unit
    def test_invokes_systemd_run_with_correct_args(self, tmp_path):
        log = MagicMock()
        worker = MagicMock()
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["timeout"] = kwargs.get("timeout")
            return self._stub_run_success(cmd, **kwargs)

        with patch("eneru.deferred_delivery.subprocess.run",
                   side_effect=fake_run):
            schedule_deferred_stop_or_eager_send(
                notification_id=42,
                db_path=tmp_path / "ups.db",
                config_path="/etc/ups-monitor/config.yaml",
                body="🛑 Stopped",
                notify_type="warning",
                worker=worker,
                log_fn=log,
                delay_secs=15,
            )

        cmd = captured["cmd"]
        assert cmd[0] == "systemd-run"
        assert "--on-active=15s" in cmd
        assert any("eneru-deliver-stop-42" in p for p in cmd)
        assert "_deliver-stop" in cmd
        assert "--notification-id" in cmd
        assert "42" in cmd
        assert "--db-path" in cmd
        assert str(tmp_path / "ups.db") in cmd
        assert "--config" in cmd
        assert "/etc/ups-monitor/config.yaml" in cmd
        # Eager-send fallback should NOT have fired.
        worker._send_via_apprise.assert_not_called()
        # Log should mention scheduling.
        assert any("scheduled" in c.args[0].lower()
                   for c in log.call_args_list)

    @pytest.mark.unit
    def test_uses_default_delay_when_unspecified(self, tmp_path):
        log = MagicMock()
        worker = MagicMock()
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return self._stub_run_success(cmd, **kwargs)

        with patch("eneru.deferred_delivery.subprocess.run",
                   side_effect=fake_run):
            schedule_deferred_stop_or_eager_send(
                notification_id=1,
                db_path=tmp_path / "ups.db",
                config_path="/etc/cfg.yaml",
                body="x",
                notify_type="warning",
                worker=worker,
                log_fn=log,
            )
        assert f"--on-active={DEFAULT_DEFER_SECS}s" in captured["cmd"]

    @pytest.mark.unit
    def test_falls_back_when_systemd_run_missing(self, tmp_path):
        """No systemd-run on PATH → eager Apprise delivery."""
        log = MagicMock()
        worker = MagicMock()
        worker._send_via_apprise.return_value = True

        with patch("eneru.deferred_delivery.subprocess.run",
                   side_effect=FileNotFoundError("systemd-run")), \
             patch("eneru.stats.StatsStore.open"), \
             patch("eneru.stats.StatsStore.mark_notification_sent"), \
             patch("eneru.stats.StatsStore.close"):
            schedule_deferred_stop_or_eager_send(
                notification_id=7,
                db_path=tmp_path / "ups.db",
                config_path="/etc/cfg.yaml",
                body="🛑 Stopped",
                notify_type="warning",
                worker=worker,
                log_fn=log,
            )
        worker._send_via_apprise.assert_called_once_with("🛑 Stopped", "warning")
        assert any("falling back" in c.args[0].lower()
                   for c in log.call_args_list)

    @pytest.mark.unit
    def test_falls_back_when_systemd_run_returns_nonzero(self, tmp_path):
        log = MagicMock()
        worker = MagicMock()
        worker._send_via_apprise.return_value = True

        result = MagicMock(returncode=1, stderr=b"some systemd error")
        with patch("eneru.deferred_delivery.subprocess.run",
                   return_value=result), \
             patch("eneru.stats.StatsStore.open"), \
             patch("eneru.stats.StatsStore.mark_notification_sent"), \
             patch("eneru.stats.StatsStore.close"):
            schedule_deferred_stop_or_eager_send(
                notification_id=7,
                db_path=tmp_path / "ups.db",
                config_path="/etc/cfg.yaml",
                body="x", notify_type="warning",
                worker=worker, log_fn=log,
            )
        worker._send_via_apprise.assert_called_once()

    @pytest.mark.unit
    def test_falls_back_when_systemd_run_times_out(self, tmp_path):
        log = MagicMock()
        worker = MagicMock()
        worker._send_via_apprise.return_value = True

        with patch("eneru.deferred_delivery.subprocess.run",
                   side_effect=subprocess.TimeoutExpired(cmd="systemd-run",
                                                         timeout=5)), \
             patch("eneru.stats.StatsStore.open"), \
             patch("eneru.stats.StatsStore.mark_notification_sent"), \
             patch("eneru.stats.StatsStore.close"):
            schedule_deferred_stop_or_eager_send(
                notification_id=7,
                db_path=tmp_path / "ups.db",
                config_path="/etc/cfg.yaml",
                body="x", notify_type="warning",
                worker=worker, log_fn=log,
            )
        worker._send_via_apprise.assert_called_once()

    @pytest.mark.unit
    def test_falls_back_to_eager_when_no_config_path(self, tmp_path):
        """No config_path → can't spawn out-of-process re-loader → eager
        delivery via the worker's Apprise instance."""
        log = MagicMock()
        worker = MagicMock()
        worker._send_via_apprise.return_value = True

        with patch("eneru.deferred_delivery.subprocess.run") as run, \
             patch("eneru.stats.StatsStore.open"), \
             patch("eneru.stats.StatsStore.mark_notification_sent"), \
             patch("eneru.stats.StatsStore.close"):
            schedule_deferred_stop_or_eager_send(
                notification_id=1,
                db_path=tmp_path / "ups.db",
                config_path=None,
                body="x", notify_type="warning",
                worker=worker, log_fn=log,
            )
        run.assert_not_called()
        worker._send_via_apprise.assert_called_once()


# ==============================================================================
# v5.2.1 short-circuit paths in schedule_deferred_stop_or_eager_send
# ==============================================================================

class TestScheduleShortCircuits:
    """The instant-eager paths added in v5.2.1 to drop the 15-second
    notification latency on `systemctl stop` and in non-systemd
    contexts (containers, K8s, manual `eneru run`)."""

    @pytest.mark.unit
    def test_short_circuit_when_not_under_systemd(self, tmp_path):
        """No INVOCATION_ID env var → eager send, no systemd-run call,
        no Job query. This is the K8s / Docker / foreground path."""
        log = MagicMock()
        worker = MagicMock()
        worker._send_via_apprise.return_value = True
        # Drop INVOCATION_ID from env to simulate non-systemd context.
        env = {k: v for k, v in __import__("os").environ.items()
               if k != "INVOCATION_ID"}
        with patch.dict("os.environ", env, clear=True), \
             patch("eneru.deferred_delivery.subprocess.run") as run, \
             patch("eneru.stats.StatsStore.open"), \
             patch("eneru.stats.StatsStore.mark_notification_sent"), \
             patch("eneru.stats.StatsStore.close"):
            schedule_deferred_stop_or_eager_send(
                notification_id=1,
                db_path=tmp_path / "ups.db",
                config_path="/etc/cfg.yaml",
                body="🛑 stop", notify_type="warning",
                worker=worker, log_fn=log,
            )
        run.assert_not_called()
        worker._send_via_apprise.assert_called_once_with("🛑 stop", "warning")
        assert any("Not running under systemd" in c.args[0]
                   for c in log.call_args_list)

    @pytest.mark.unit
    def test_short_circuit_on_systemctl_stop_intent(self, tmp_path):
        """systemd Job=stop detected → eager send, no systemd-run timer.
        This is the instant-Stopped UX win for `systemctl stop eneru`."""
        log = MagicMock()
        worker = MagicMock()
        worker._send_via_apprise.return_value = True
        with patch.dict("os.environ",
                        {"INVOCATION_ID": "test-invocation"}), \
             patch("eneru.deferred_delivery._detect_systemd_stop_intent",
                   return_value=True), \
             patch("eneru.deferred_delivery.subprocess.run") as run, \
             patch("eneru.stats.StatsStore.open"), \
             patch("eneru.stats.StatsStore.mark_notification_sent"), \
             patch("eneru.stats.StatsStore.close"):
            schedule_deferred_stop_or_eager_send(
                notification_id=1,
                db_path=tmp_path / "ups.db",
                config_path="/etc/cfg.yaml",
                body="🛑 stop", notify_type="warning",
                worker=worker, log_fn=log,
            )
        # subprocess.run is mocked but _detect_systemd_stop_intent
        # is also mocked, so subprocess.run shouldn't have been
        # called (the systemd-run path is what would call it).
        run.assert_not_called()
        worker._send_via_apprise.assert_called_once_with("🛑 stop", "warning")
        assert any("systemctl stop detected" in c.args[0]
                   for c in log.call_args_list)


# ==============================================================================
# _eager_send
# ==============================================================================

class TestEagerSend:

    @pytest.mark.unit
    def test_marks_sent_on_apprise_success(self, tmp_path):
        """Worker.send_via_apprise returns True → row gets marked sent."""
        worker = MagicMock()
        worker._send_via_apprise.return_value = True
        db_path = tmp_path / "ups.db"
        # Pre-create a row via real StatsStore so mark_notification_sent
        # has something to update.
        store = StatsStore(db_path)
        store.open()
        try:
            row_id = store.enqueue_notification(
                body="x", notify_type="warning",
                category="lifecycle", ts=1000,
            )
        finally:
            store.close()

        log = MagicMock()
        _eager_send(
            notification_id=row_id, db_path=db_path,
            body="🛑 stop", notify_type="warning",
            worker=worker, log_fn=log,
        )
        # Re-open and verify status.
        store = StatsStore(db_path)
        store.open()
        try:
            row = store._conn.execute(
                "SELECT status FROM notifications WHERE id = ?",
                (row_id,),
            ).fetchone()
        finally:
            store.close()
        assert row[0] == "sent"

    @pytest.mark.unit
    def test_leaves_pending_on_apprise_failure(self, tmp_path):
        worker = MagicMock()
        worker._send_via_apprise.return_value = False
        db_path = tmp_path / "ups.db"
        store = StatsStore(db_path)
        store.open()
        try:
            row_id = store.enqueue_notification(
                body="x", notify_type="warning",
                category="lifecycle", ts=1000,
            )
        finally:
            store.close()

        log = MagicMock()
        _eager_send(
            notification_id=row_id, db_path=db_path,
            body="🛑 stop", notify_type="warning",
            worker=worker, log_fn=log,
        )
        store = StatsStore(db_path)
        store.open()
        try:
            row = store._conn.execute(
                "SELECT status FROM notifications WHERE id = ?",
                (row_id,),
            ).fetchone()
        finally:
            store.close()
        assert row[0] == "pending"

    @pytest.mark.unit
    def test_no_op_when_worker_is_none(self, tmp_path):
        log = MagicMock()
        # Should not raise.
        _eager_send(
            notification_id=1, db_path=tmp_path / "x.db",
            body="x", notify_type="warning",
            worker=None, log_fn=log,
        )
        assert any("No worker" in c.args[0] for c in log.call_args_list)


# ==============================================================================
# deliver_pending_stop (CLI entry point body)
# ==============================================================================

class TestDeliverPendingStop:

    def _make_pending_row(self, db_path, body="🛑 Stopped", status="pending"):
        store = StatsStore(db_path)
        store.open()
        try:
            row_id = store.enqueue_notification(
                body=body, notify_type="warning",
                category="lifecycle", ts=1000,
            )
            if status != "pending":
                store.cancel_notification(row_id, status)
            return row_id
        finally:
            store.close()

    def _make_config(self, urls=None):
        from eneru.config import Config, NotificationsConfig
        cfg = Config()
        cfg.notifications = NotificationsConfig()
        cfg.notifications.urls = urls or ["json://192.0.2.1/x"]
        return cfg

    @pytest.mark.unit
    def test_returns_zero_when_db_missing(self, tmp_path):
        cfg = self._make_config()
        rc = deliver_pending_stop(
            notification_id=42,
            db_path=tmp_path / "does-not-exist.db",
            config=cfg,
        )
        assert rc == 0

    @pytest.mark.unit
    def test_skips_when_row_is_already_cancelled(self, tmp_path):
        """Next daemon's classifier already superseded the row →
        timer is a no-op (single Restarted will be visible to the user)."""
        db_path = tmp_path / "ups.db"
        # Use cancel_notification's reason argument to drop the row to
        # status='cancelled' as the classifier would.
        row_id = self._make_pending_row(db_path, status="superseded")
        cfg = self._make_config()
        with patch("eneru.notifications.NotificationWorker") as Worker:
            worker = Worker.return_value
            worker.start.return_value = True
            rc = deliver_pending_stop(
                notification_id=row_id, db_path=db_path, config=cfg,
            )
        assert rc == 0
        # Worker should NOT have been used.
        worker._send_via_apprise.assert_not_called()

    @pytest.mark.unit
    def test_delivers_when_row_is_pending(self, tmp_path):
        """No replacement daemon came up — row is still pending → ship
        via Apprise and mark sent."""
        db_path = tmp_path / "ups.db"
        row_id = self._make_pending_row(db_path)
        cfg = self._make_config()
        with patch("eneru.notifications.NotificationWorker") as Worker:
            worker = Worker.return_value
            worker.start.return_value = True
            worker._send_via_apprise.return_value = True
            rc = deliver_pending_stop(
                notification_id=row_id, db_path=db_path, config=cfg,
            )
        assert rc == 0
        worker._send_via_apprise.assert_called_once_with("🛑 Stopped", "warning")
        worker.stop.assert_called_once()
        # Verify row is now sent.
        store = StatsStore(db_path)
        store.open()
        try:
            row = store._conn.execute(
                "SELECT status FROM notifications WHERE id = ?",
                (row_id,),
            ).fetchone()
        finally:
            store.close()
        assert row[0] == "sent"

    @pytest.mark.unit
    def test_returns_zero_when_row_is_purged(self, tmp_path):
        """Row was purged (e.g., max_age_days TTL) between scheduling
        and timer fire → silent no-op."""
        db_path = tmp_path / "ups.db"
        # Open store so the DB file exists, but DON'T insert a row.
        StatsStore(db_path).open()
        cfg = self._make_config()
        rc = deliver_pending_stop(
            notification_id=999, db_path=db_path, config=cfg,
        )
        assert rc == 0
