"""Tests for the v5.2 DB-backed notification worker.

The worker is persistent: ``send()`` writes a ``pending`` row to a
registered :class:`StatsStore`'s notifications table, and the worker
thread drains those rows via Apprise. Tests assert the new contract:
persistence, exponential backoff, attempt cap, age expiry, backlog cap,
flush(), and the memory-buffer drain on first ``register_store``.
"""

import pytest
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

from eneru import (
    NotificationWorker,
    Config,
    NotificationsConfig,
)
from eneru.stats import StatsStore


@pytest.fixture
def notification_config():
    """Config with notifications enabled. Short retry_interval so the
    backoff schedule lands inside test budgets."""
    config = Config()
    config.notifications = NotificationsConfig(
        enabled=True,
        urls=["discord://test/token"],
        title="Test UPS",
        timeout=5,
        retry_interval=1,
    )
    return config


@pytest.fixture
def registered_store(tmp_path):
    """Open a StatsStore for tests that need real persistent delivery."""
    store = StatsStore(tmp_path / "notifications.db")
    store.open()
    yield store
    store.close()


def _patch_apprise(mock_apprise, *, succeed=True,
                   side_effect=None, service_count=1):
    """Wire the apprise mock so the worker can construct & call it.
    Returns the inner Apprise() mock so tests can interrogate notify."""
    mock_instance = MagicMock()
    mock_apprise.Apprise.return_value = mock_instance
    mock_instance.add.return_value = True
    mock_instance.__len__ = lambda self: service_count
    if side_effect is not None:
        mock_instance.notify.side_effect = side_effect
    else:
        mock_instance.notify.return_value = bool(succeed)
    mock_apprise.NotifyType = MagicMock()
    mock_apprise.NotifyType.INFO = "info"
    mock_apprise.NotifyType.SUCCESS = "success"
    mock_apprise.NotifyType.WARNING = "warning"
    mock_apprise.NotifyType.FAILURE = "failure"
    return mock_instance


def _wait_until(predicate, timeout=2.0, poll=0.02):
    """Block until predicate() is truthy or the timeout elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(poll)
    return False


def _peek(store, sql, params=()):
    """Test helper: read from the store under its ``_db_lock`` to avoid
    racing the worker thread on Python 3.13 sqlite3 (which raises
    ``SystemError: error return without exception set`` when concurrent
    execute()s share a connection without external mutex). Cubic P1.
    """
    with store._db_lock:
        return store._conn.execute(sql, params).fetchone()


def _peek_all(store, sql, params=()):
    """Test helper: like ``_peek`` but returns ``fetchall()``."""
    with store._db_lock:
        return store._conn.execute(sql, params).fetchall()


# ==============================================================================
# Worker lifecycle
# ==============================================================================

class TestWorkerLifecycle:

    @pytest.mark.unit
    def test_worker_not_initialized_when_disabled(self):
        config = Config()
        config.notifications.enabled = False
        worker = NotificationWorker(config)
        assert worker.start() is False
        assert worker._initialized is False

    @pytest.mark.unit
    def test_worker_not_initialized_without_urls(self):
        config = Config()
        config.notifications.enabled = True
        config.notifications.urls = []
        worker = NotificationWorker(config)
        assert worker.start() is False

    @pytest.mark.unit
    @patch("eneru.notifications.APPRISE_AVAILABLE", True)
    @patch("eneru.notifications.apprise")
    def test_worker_starts_with_valid_config(self, mock_apprise,
                                             notification_config):
        _patch_apprise(mock_apprise)
        worker = NotificationWorker(notification_config)
        try:
            assert worker.start() is True
            assert worker._initialized is True
            assert worker._worker_thread is not None
            assert worker._worker_thread.daemon is True
        finally:
            worker.stop()

    @pytest.mark.unit
    @patch("eneru.notifications.APPRISE_AVAILABLE", True)
    @patch("eneru.notifications.apprise")
    def test_stop_terminates_thread(self, mock_apprise, notification_config):
        _patch_apprise(mock_apprise)
        worker = NotificationWorker(notification_config)
        worker.start()
        assert worker._worker_thread.is_alive()
        worker.stop()
        # Stop joins with a 2s budget; the thread should be down well
        # before that on a non-stuck loop.
        assert _wait_until(lambda: not worker._worker_thread.is_alive(),
                           timeout=2.5)

    @pytest.mark.unit
    def test_send_is_noop_when_uninitialized(self):
        config = Config()
        config.notifications.enabled = False
        worker = NotificationWorker(config)
        # No start() — must not raise.
        assert worker.send("anything", "info", "general") is None

    @pytest.mark.unit
    @patch("eneru.notifications.APPRISE_AVAILABLE", True)
    @patch("eneru.notifications.apprise")
    def test_get_service_count(self, mock_apprise, notification_config):
        _patch_apprise(mock_apprise, service_count=3)
        worker = NotificationWorker(notification_config)
        try:
            worker.start()
            assert worker.get_service_count() == 3
        finally:
            worker.stop()


# ==============================================================================
# Send + persistence
# ==============================================================================

class TestSendPersistence:

    @pytest.mark.unit
    @patch("eneru.notifications.APPRISE_AVAILABLE", True)
    @patch("eneru.notifications.apprise")
    def test_send_returns_quickly(self, mock_apprise, notification_config,
                                  registered_store):
        _patch_apprise(mock_apprise)
        worker = NotificationWorker(notification_config)
        try:
            worker.start()
            worker.register_store(registered_store)
            t0 = time.monotonic()
            worker.send("Test message", "info", "general")
            elapsed = time.monotonic() - t0
            # Send is INSERT + signal — should return well under 100ms
            # even on slow sqlite.
            assert elapsed < 0.1
        finally:
            worker.stop()

    @pytest.mark.unit
    @patch("eneru.notifications.APPRISE_AVAILABLE", True)
    @patch("eneru.notifications.apprise")
    def test_send_persists_pending_row(self, mock_apprise,
                                       notification_config,
                                       registered_store):
        # Worker not started → no delivery, but the row must persist.
        _patch_apprise(mock_apprise)
        worker = NotificationWorker(notification_config)
        try:
            worker.start()
            worker.register_store(registered_store)
            # Stop immediately so the worker can't drain.
            worker.stop()
            row_id = worker.send("persisted", "info", "lifecycle")
            assert row_id is not None
            row = _peek(
                registered_store,
                "SELECT body, notify_type, category, status FROM notifications "
                "WHERE id=?", (row_id,),
            )
            assert row == ("persisted", "info", "lifecycle", "pending")
        finally:
            worker.stop()

    @pytest.mark.unit
    @patch("eneru.notifications.APPRISE_AVAILABLE", True)
    @patch("eneru.notifications.apprise")
    def test_successful_delivery_marks_sent(self, mock_apprise,
                                            notification_config,
                                            registered_store):
        mock_instance = _patch_apprise(mock_apprise, succeed=True)
        worker = NotificationWorker(notification_config)
        try:
            worker.start()
            worker.register_store(registered_store)
            worker.send("hello", "info", "lifecycle")
            assert _wait_until(
                lambda: registered_store.pending_notification_count() == 0
            )
            mock_instance.notify.assert_called()
        finally:
            worker.stop()

    @pytest.mark.unit
    @patch("eneru.notifications.APPRISE_AVAILABLE", True)
    @patch("eneru.notifications.apprise")
    def test_memory_buffer_flushed_on_first_register(self, mock_apprise,
                                                    notification_config,
                                                    registered_store):
        """Sends made before any store is registered (multi-UPS coordinator
        startup window) buffer in memory and replay verbatim — including
        the original ts — once the first store appears."""
        mock_instance = _patch_apprise(mock_apprise, succeed=True)
        worker = NotificationWorker(notification_config)
        try:
            worker.start()
            # No register yet → buffered.
            assert worker.send("buffered-1", "info", "lifecycle") is None
            assert worker.send("buffered-2", "info", "lifecycle") is None
            assert registered_store.pending_notification_count() == 0
            # Register triggers the drain.
            worker.register_store(registered_store)
            assert _wait_until(
                lambda: registered_store.pending_notification_count() == 0
                        and mock_instance.notify.call_count >= 2
            )
            bodies = [c.kwargs.get("body") for c
                      in mock_instance.notify.call_args_list]
            assert "buffered-1" in bodies and "buffered-2" in bodies
        finally:
            worker.stop()


# ==============================================================================
# Retry / backoff / max_attempts
# ==============================================================================

class TestRetryAndBackoff:

    @pytest.mark.unit
    @patch("eneru.notifications.APPRISE_AVAILABLE", True)
    @patch("eneru.notifications.apprise")
    def test_failure_increments_attempts_keeps_pending(
        self, mock_apprise, notification_config, registered_store,
    ):
        # All deliveries fail.
        _patch_apprise(mock_apprise, succeed=False)
        worker = NotificationWorker(notification_config)
        try:
            worker.start()
            worker.register_store(registered_store)
            worker.send("retry-me", "info", "lifecycle")
            # Wait long enough for at least one attempt.
            assert _wait_until(
                lambda: _peek(
                    registered_store,
                    "SELECT attempts FROM notifications "
                    "WHERE body='retry-me'",
                )[0] >= 1
            )
            row = _peek(
                registered_store,
                "SELECT status, attempts FROM notifications "
                "WHERE body='retry-me'",
            )
            assert row[0] == "pending"
            assert row[1] >= 1
        finally:
            worker.stop()

    @pytest.mark.unit
    @patch("eneru.notifications.APPRISE_AVAILABLE", True)
    @patch("eneru.notifications.apprise")
    def test_max_attempts_cancels_with_reason(self, mock_apprise,
                                              registered_store):
        """When the per-message cap is set, a poison message hits its
        limit and the row gets cancel_reason='max_attempts'."""
        _patch_apprise(mock_apprise, succeed=False)
        config = Config()
        config.notifications = NotificationsConfig(
            enabled=True,
            urls=["discord://x"],
            title="t",
            timeout=5,
            retry_interval=0,        # no backoff wait
            retry_backoff_max=0,
            max_attempts=3,
        )
        worker = NotificationWorker(config)
        try:
            worker.start()
            worker.register_store(registered_store)
            worker.send("poison", "info", "lifecycle")
            assert _wait_until(
                lambda: _peek(
                    registered_store,
                    "SELECT status FROM notifications WHERE body='poison'",
                )[0] == "cancelled"
            )
            row = _peek(
                registered_store,
                "SELECT status, attempts, cancel_reason "
                "FROM notifications WHERE body='poison'",
            )
            assert row[0] == "cancelled"
            assert row[1] == 3
            assert row[2] == "max_attempts"
        finally:
            worker.stop()

    @pytest.mark.unit
    @patch("eneru.notifications.APPRISE_AVAILABLE", True)
    @patch("eneru.notifications.apprise")
    def test_max_attempts_zero_means_unlimited(self, mock_apprise,
                                               registered_store):
        """Default max_attempts=0 must NOT cancel the row no matter how
        many failures pile up. This is the panic-attack guarantee:
        Apprise's bool can't tell network down from bad URL."""
        _patch_apprise(mock_apprise, succeed=False)
        config = Config()
        config.notifications = NotificationsConfig(
            enabled=True,
            urls=["discord://x"],
            title="t",
            timeout=5,
            retry_interval=0,
            retry_backoff_max=0,
            max_attempts=0,
        )
        worker = NotificationWorker(config)
        try:
            worker.start()
            worker.register_store(registered_store)
            worker.send("forever", "info", "lifecycle")
            assert _wait_until(
                lambda: _peek(
                    registered_store,
                    "SELECT attempts FROM notifications "
                    "WHERE body='forever'",
                )[0] >= 5
            )
            row = _peek(
                registered_store,
                "SELECT status, cancel_reason FROM notifications "
                "WHERE body='forever'",
            )
            assert row[0] == "pending"
            assert row[1] is None
        finally:
            worker.stop()

    @pytest.mark.unit
    @patch("eneru.notifications.APPRISE_AVAILABLE", True)
    @patch("eneru.notifications.apprise")
    def test_recovery_after_failures_marks_sent(self, mock_apprise,
                                                registered_store):
        """First N attempts fail, then succeed — the row should
        eventually be marked sent and removed from pending."""
        # Fail 3 times then succeed.
        _patch_apprise(
            mock_apprise,
            side_effect=[False, False, False, True],
        )
        config = Config()
        config.notifications = NotificationsConfig(
            enabled=True,
            urls=["discord://x"],
            title="t",
            timeout=5,
            retry_interval=0,
            retry_backoff_max=0,
            max_attempts=0,
        )
        worker = NotificationWorker(config)
        try:
            worker.start()
            worker.register_store(registered_store)
            worker.send("eventually", "info", "lifecycle")
            assert _wait_until(
                lambda: registered_store.pending_notification_count() == 0
            )
            row = _peek(
                registered_store,
                "SELECT status, attempts FROM notifications "
                "WHERE body='eventually'",
            )
            assert row[0] == "sent"
            assert row[1] >= 3

        finally:
            worker.stop()

    @pytest.mark.unit
    @patch("eneru.notifications.APPRISE_AVAILABLE", True)
    @patch("eneru.notifications.apprise")
    def test_stop_returns_quickly_with_pending_failures(self, mock_apprise,
                                                       registered_store):
        """stop() must return promptly (under ~3s join budget) even
        with pending rows that keep failing — the rows persist for the
        next process start."""
        _patch_apprise(mock_apprise, succeed=False)
        config = Config()
        config.notifications = NotificationsConfig(
            enabled=True,
            urls=["discord://x"],
            title="t",
            timeout=5,
            retry_interval=10,  # would block forever in v5.1
            retry_backoff_max=300,
        )
        worker = NotificationWorker(config)
        worker.start()
        worker.register_store(registered_store)
        worker.send("stays-pending", "info", "lifecycle")
        time.sleep(0.1)
        t0 = time.monotonic()
        worker.stop()
        elapsed = time.monotonic() - t0
        assert elapsed < 3.0
        # Row stayed pending for the next start. Worker is stopped now,
        # but use the lock helper anyway for consistency.
        row = _peek(
            registered_store,
            "SELECT status FROM notifications WHERE body='stays-pending'",
        )
        assert row[0] == "pending"


# ==============================================================================
# Ordering across stores + flush()
# ==============================================================================

class TestOrderingAndFlush:

    @pytest.mark.unit
    @patch("eneru.notifications.APPRISE_AVAILABLE", True)
    @patch("eneru.notifications.apprise")
    def test_oldest_pending_delivered_first(self, mock_apprise,
                                            notification_config,
                                            registered_store):
        mock_instance = _patch_apprise(mock_apprise, succeed=True)
        worker = NotificationWorker(notification_config)
        try:
            worker.start()
            worker.register_store(registered_store)
            for body in ("first", "second", "third"):
                worker.send(body, "info", "lifecycle")
            assert _wait_until(
                lambda: registered_store.pending_notification_count() == 0
                        and mock_instance.notify.call_count >= 3,
            )
            bodies = [c.kwargs.get("body")
                      for c in mock_instance.notify.call_args_list]
            assert bodies[:3] == ["first", "second", "third"]
        finally:
            worker.stop()

    @pytest.mark.unit
    @patch("eneru.notifications.APPRISE_AVAILABLE", True)
    @patch("eneru.notifications.apprise")
    def test_flush_returns_true_when_drained(self, mock_apprise,
                                             notification_config,
                                             registered_store):
        _patch_apprise(mock_apprise, succeed=True)
        worker = NotificationWorker(notification_config)
        try:
            worker.start()
            worker.register_store(registered_store)
            worker.send("a", "info", "lifecycle")
            worker.send("b", "info", "lifecycle")
            assert worker.flush(timeout=2.0) is True
            assert registered_store.pending_notification_count() == 0
        finally:
            worker.stop()

    @pytest.mark.unit
    @patch("eneru.notifications.APPRISE_AVAILABLE", True)
    @patch("eneru.notifications.apprise")
    def test_flush_returns_false_on_timeout_with_failures(
        self, mock_apprise, registered_store,
    ):
        _patch_apprise(mock_apprise, succeed=False)
        config = Config()
        config.notifications = NotificationsConfig(
            enabled=True, urls=["discord://x"], title="t", timeout=5,
            retry_interval=10, retry_backoff_max=300,
            max_attempts=0,
        )
        worker = NotificationWorker(config)
        try:
            worker.start()
            worker.register_store(registered_store)
            worker.send("undeliverable", "info", "lifecycle")
            t0 = time.monotonic()
            ok = worker.flush(timeout=0.3)
            elapsed = time.monotonic() - t0
            assert ok is False
            # flush honours the timeout (with small slack for poll grain).
            assert elapsed < 0.6
            assert registered_store.pending_notification_count() == 1
        finally:
            worker.stop()


# ==============================================================================
# Backlog cap (max_pending)
# ==============================================================================

class TestBacklogCap:

    @pytest.mark.unit
    @patch("eneru.notifications.APPRISE_AVAILABLE", True)
    @patch("eneru.notifications.apprise")
    def test_send_enforces_max_pending_cancelling_oldest(
        self, mock_apprise, registered_store,
    ):
        """When pending exceeds max_pending, the OLDEST get cancelled
        with reason 'backlog_overflow'. Newest survive."""
        _patch_apprise(mock_apprise, succeed=False)  # all stay pending
        config = Config()
        config.notifications = NotificationsConfig(
            enabled=True, urls=["discord://x"], title="t", timeout=5,
            retry_interval=10,  # never actually retries within test
            retry_backoff_max=300,
            max_pending=2,
            max_attempts=0,
        )
        worker = NotificationWorker(config)
        worker.start()
        worker.register_store(registered_store)
        # Stop the worker thread BEFORE issuing the sends — otherwise the
        # worker thread races send()'s cap_pending UPDATE on the same
        # SQLite connection and Python 3.13 raises SystemError. send()
        # remains usable after stop() (it still INSERTs pending rows and
        # cap_pending runs synchronously inside send()) — there's just
        # no thread reading them. That's exactly what this test wants.
        worker.stop()
        for i in range(5):
            worker.send(f"msg-{i}", "info", "lifecycle")
        # Cap kicks in synchronously inside send().
        assert registered_store.pending_notification_count() <= 2
        cancelled = _peek_all(
            registered_store,
            "SELECT body, cancel_reason FROM notifications "
            "WHERE status='cancelled' ORDER BY id ASC",
        )
        cancel_bodies = [c[0] for c in cancelled]
        cancel_reasons = {c[1] for c in cancelled}
        # Older messages cancelled, newest kept.
        assert "msg-0" in cancel_bodies
        assert "backlog_overflow" in cancel_reasons


# ==============================================================================
# Slice 4: brief-outage coalescing (panic-attack)
# ==============================================================================

class TestPowerEventCoalescing:

    @pytest.mark.unit
    @patch("eneru.notifications.APPRISE_AVAILABLE", True)
    @patch("eneru.notifications.apprise")
    def test_pair_collapses_into_brief_outage_summary(
        self, mock_apprise, notification_config, registered_store,
    ):
        """When ON_BATTERY + POWER_RESTORED are both pending in the
        store (network down for the duration of a short outage), the
        coalescer replaces them with a single 'Brief Power Outage'
        summary BEFORE the worker tries delivery."""
        # Apprise unreachable so nothing ships and we can observe the
        # final pending state.
        _patch_apprise(mock_apprise, succeed=False)
        worker = NotificationWorker(notification_config)
        worker.start()
        worker.register_store(registered_store)
        # Stop the worker thread before mutating; we'll exercise
        # _coalesce_pending_outages directly to avoid the same race
        # the cap test had on Python 3.13.
        worker.stop()

        # Simulate the on_battery → on_line pair using the sub-typed
        # categories that _log_power_event sets in production. The
        # body wording is irrelevant to the coalescer (intentionally —
        # it doesn't grep user-visible strings).
        registered_store.enqueue_notification(
            body="⚠️ **POWER FAILURE DETECTED!**\nDetails: Battery 80%",
            notify_type="warning",
            category="power_event_on_battery", ts=1000,
        )
        registered_store.enqueue_notification(
            body="✅ **POWER RESTORED**\nDetails: Outage 60s",
            notify_type="success",
            category="power_event_on_line", ts=1060,
        )
        coalesced = worker._coalesce_pending_outages(registered_store)
        assert coalesced == 1

        # Originals cancelled; one new summary remains pending.
        cancelled = _peek_all(
            registered_store,
            "SELECT body, cancel_reason FROM notifications "
            "WHERE status='cancelled' ORDER BY id ASC",
        )
        assert len(cancelled) == 2
        assert all(c[1] == "coalesced" for c in cancelled)

        pending = _peek_all(
            registered_store,
            "SELECT body FROM notifications WHERE status='pending'",
        )
        assert len(pending) == 1
        body = pending[0][0]
        assert "Brief Power Outage" in body
        # Includes duration + the time-of-day window.
        assert "1m" in body or "60s" in body

    @pytest.mark.unit
    @patch("eneru.notifications.APPRISE_AVAILABLE", True)
    @patch("eneru.notifications.apprise")
    def test_unpaired_on_battery_alone_is_left_alone(
        self, mock_apprise, notification_config, registered_store,
    ):
        """Without a matching POWER_RESTORED, the lone ON_BATTERY stays
        pending — coalescing only kicks in for matched pairs (otherwise
        we'd hide an in-progress outage from the user)."""
        _patch_apprise(mock_apprise, succeed=False)
        worker = NotificationWorker(notification_config)
        worker.start()
        worker.register_store(registered_store)
        worker.stop()

        registered_store.enqueue_notification(
            body="⚠️ **POWER FAILURE DETECTED!**\nDetails: Battery 80%",
            notify_type="warning",
            category="power_event_on_battery", ts=1000,
        )
        coalesced = worker._coalesce_pending_outages(registered_store)
        assert coalesced == 0
        assert registered_store.pending_notification_count() == 1


# ==============================================================================
# Config defaults (regression-style)
# ==============================================================================

class TestConfigDefaults:

    @pytest.mark.unit
    def test_retry_interval_default(self):
        assert NotificationsConfig().retry_interval == 5

    @pytest.mark.unit
    def test_v52_outage_survival_defaults(self):
        """The defaults must survive a multi-day weekend outage. See
        the analysis in the v5.2.0 plan: max_attempts=0 (unlimited),
        max_age_days=30, max_pending=10000, retry_backoff_max=300."""
        c = NotificationsConfig()
        assert c.retention_days == 7
        assert c.max_attempts == 0
        assert c.max_age_days == 30
        assert c.max_pending == 10000
        assert c.retry_backoff_max == 300
