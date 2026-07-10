"""Tests for the v5.2 DB-backed notification worker.

The worker is persistent: ``send()`` writes a ``pending`` row to a
registered :class:`StatsStore`'s notifications table, and the worker
thread drains those rows via Apprise. Tests assert the new contract:
persistence, exponential backoff, attempt cap, age expiry, backlog cap,
flush(), and the memory-buffer drain on first ``register_store``.
"""

import pytest
import threading
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


def _wait_until(predicate, timeout=5.0, poll=0.02):
    """Block until predicate() is truthy or the timeout elapses.

    ISS-054: default timeout widened from 2 s to 5 s for headroom on a
    loaded CI runner — this is already the robust bounded-poll pattern
    (returns as soon as the condition holds), so a larger cap only costs
    wall-clock on genuine failures, never on the happy path."""
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
    def test_send_via_apprise_bounded_returns_result(self):
        """M6: bounded eager send returns the underlying send result."""
        worker = NotificationWorker(Config())
        with patch.object(worker, "_send_via_apprise", return_value=True) as inner:
            assert worker._send_via_apprise_bounded("b", "info") is True
        inner.assert_called_once_with("b", "info")
        with patch.object(worker, "_send_via_apprise", return_value=False):
            assert worker._send_via_apprise_bounded("b", "info") is False

    @pytest.mark.unit
    def test_send_via_apprise_bounded_times_out(self):
        """M6: a hung eager send is abandoned after the timeout (returns False)
        so the SIGTERM handler isn't blocked; the row stays pending."""
        worker = NotificationWorker(Config())
        started = threading.Event()
        release = threading.Event()

        def _slow(body, notify_type):
            started.set()
            release.wait(5)
            return True

        with patch.object(worker, "_send_via_apprise", side_effect=_slow):
            result = worker._send_via_apprise_bounded("b", "info", timeout=0.05)
        assert result is False
        assert started.is_set()
        release.set()  # let the daemon thread unwind

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
    @patch("eneru.notifications.APPRISE_AVAILABLE", True)
    @patch("eneru.notifications.apprise")
    def test_add_failure_redacts_url(self, mock_apprise, capsys):
        """ISS-008: a failed add() must not echo the raw Apprise URL (it embeds
        webhook tokens); only the scheme is printed."""
        mock_instance = MagicMock()
        mock_apprise.Apprise.return_value = mock_instance
        mock_instance.add.return_value = False  # simulate a rejected URL
        mock_instance.__len__ = lambda self: 0
        config = Config()
        config.notifications = NotificationsConfig(
            enabled=True, urls=["discord://id/SUPERSECRETTOKEN"],
        )
        worker = NotificationWorker(config)
        assert worker.start() is False  # no valid URLs
        out = capsys.readouterr().out
        assert "discord://***" in out
        assert "SUPERSECRETTOKEN" not in out

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
    def test_enqueue_returns_none_falls_back_to_memory_buffer(
        self, mock_apprise, notification_config, registered_store
    ):
        """F-010: a registered-but-unopened store returns None from
        enqueue_notification. Instead of silently dropping the message,
        send() must buffer it in memory (lossless guarantee), warn once,
        and return None. A second failing send must NOT re-warn."""
        _patch_apprise(mock_apprise, succeed=True)
        worker = NotificationWorker(notification_config)
        try:
            worker.start()
            worker.stop()  # halt the drain so the buffer stays put
            worker.register_store(registered_store)
            # Simulate the store not being open: enqueue refuses the row.
            registered_store.enqueue_notification = MagicMock(
                return_value=None
            )
            # Capture warnings emitted via _warn.
            warnings = []
            worker._warn = warnings.append

            assert worker._memory_buffer == []
            assert worker.send("dropped-1", "info", "shutdown") is None
            assert len(worker._memory_buffer) == 1
            assert worker._memory_buffer[0][0] == "dropped-1"
            assert len(warnings) == 1
            assert "store not open" in warnings[0]
            assert worker._enqueue_failed_warned is True

            # Second failure buffers again but does NOT re-warn.
            assert worker.send("dropped-2", "info", "shutdown") is None
            assert len(worker._memory_buffer) == 2
            assert len(warnings) == 1
        finally:
            worker.stop()

    @pytest.mark.unit
    @patch("eneru.notifications.APPRISE_AVAILABLE", True)
    @patch("eneru.notifications.apprise")
    def test_worker_replays_memory_buffer_when_store_recovers(
        self, mock_apprise, notification_config, registered_store
    ):
        """cubic P1 (round 1): rows buffered because the store refused the
        insert must be replayed by the WORKER once the store recovers —
        register_store never fires again after startup, so without a worker
        replay path the buffer would sit in memory until process exit."""
        _patch_apprise(mock_apprise, succeed=True)
        worker = NotificationWorker(notification_config)
        try:
            worker.start()
            worker.stop()  # halt the drain thread; we drive replay directly
            worker.register_store(registered_store)
            real_enqueue = registered_store.enqueue_notification
            # Store "not open": sends land in the memory buffer.
            registered_store.enqueue_notification = MagicMock(
                return_value=None)
            worker._warn = lambda *_: None
            worker.send("buffered-1", "info", "shutdown")
            worker.send("buffered-2", "warning", "shutdown")
            assert len(worker._memory_buffer) == 2

            # Store still broken: replay attempts and re-buffers everything,
            # preserving age order.
            assert worker._replay_memory_buffer() == 2
            assert [row[0] for row in worker._memory_buffer] == [
                "buffered-1", "buffered-2"]

            # Store recovers: the worker-loop replay persists the backlog.
            registered_store.enqueue_notification = real_enqueue
            assert worker._replay_memory_buffer() == 2
            assert worker._memory_buffer == []
            assert registered_store.pending_notification_count() == 2
        finally:
            worker.stop()

    @pytest.mark.unit
    @patch("eneru.notifications.APPRISE_AVAILABLE", True)
    @patch("eneru.notifications.apprise")
    def test_replay_memory_buffer_noop_without_store_or_backlog(
        self, mock_apprise, notification_config, registered_store
    ):
        """Replay is a cheap no-op when there's nothing to do: no store
        registered (rows must stay buffered) or an empty buffer."""
        _patch_apprise(mock_apprise, succeed=True)
        worker = NotificationWorker(notification_config)
        try:
            worker.start()
            worker.stop()
            # No store yet: pre-store sends stay buffered.
            worker.send("pre-store", "info", "lifecycle")
            assert len(worker._memory_buffer) == 1
            assert worker._replay_memory_buffer() == 0
            assert len(worker._memory_buffer) == 1
            # Store registered (drains the buffer), then an empty buffer
            # replay is a no-op.
            worker.register_store(registered_store)
            assert worker._memory_buffer == []
            assert worker._replay_memory_buffer() == 0
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
# F-067: direct Apprise delivery of memory-buffered rows + reload handover
# ==============================================================================

class TestMemoryBufferDirectDelivery:
    """F-067: rows the store keeps refusing must ship straight through
    Apprise (drop from the buffer only on confirmed delivery), instead of
    sitting in memory until process exit."""

    @pytest.mark.unit
    @patch("eneru.notifications.APPRISE_AVAILABLE", True)
    @patch("eneru.notifications.apprise")
    def test_closed_store_message_actually_delivered(
        self, mock_apprise, notification_config, tmp_path
    ):
        """The report's regression case: a registered-but-CLOSED store
        (unwritable /var/lib/eneru) + a working Apprise endpoint → the
        message must actually be delivered, end-to-end via the worker
        thread, and leave the memory buffer."""
        mock_instance = _patch_apprise(mock_apprise, succeed=True)
        closed_store = StatsStore(tmp_path / "never-opened.db")  # no open()
        worker = NotificationWorker(notification_config)
        try:
            worker.start()
            worker.register_store(closed_store)
            worker._warn = lambda *_: None
            assert worker.send("lost-no-more", "failure", "shutdown") is None
            assert len(worker._memory_buffer) == 1
            assert _wait_until(
                lambda: mock_instance.notify.call_count >= 1
                and len(worker._memory_buffer) == 0
            )
            bodies = [c.kwargs.get("body")
                      for c in mock_instance.notify.call_args_list]
            assert "lost-no-more" in bodies
        finally:
            worker.stop()

    @pytest.mark.unit
    @patch("eneru.notifications.APPRISE_AVAILABLE", True)
    @patch("eneru.notifications.apprise")
    def test_flush_returns_early_once_buffer_delivers(
        self, mock_apprise, notification_config, tmp_path
    ):
        """F-067(c): flush() must not burn its full timeout when only
        memory-buffered rows remain and they get delivered directly."""
        _patch_apprise(mock_apprise, succeed=True)
        closed_store = StatsStore(tmp_path / "never-opened.db")
        worker = NotificationWorker(notification_config)
        try:
            worker.start()
            worker.register_store(closed_store)
            worker._warn = lambda *_: None
            worker.send("drain-me", "info", "shutdown")
            start = time.monotonic()
            assert worker.flush(timeout=10) is True
            assert time.monotonic() - start < 8  # returned early, not on timeout
            assert worker._memory_buffer == []
        finally:
            worker.stop()

    @pytest.mark.unit
    @patch("eneru.notifications.APPRISE_AVAILABLE", True)
    @patch("eneru.notifications.apprise")
    def test_no_store_grace_holds_then_delivers(
        self, mock_apprise, notification_config
    ):
        """With NO store registered, rows are held for the imminent
        register_store during the startup grace, then direct-delivered
        once the grace passes (no store is coming)."""
        mock_instance = _patch_apprise(mock_apprise, succeed=True)
        worker = NotificationWorker(notification_config)
        try:
            worker.start()
            worker.stop()  # halt the thread; drive the direct path by hand
            worker.send("held", "info", "lifecycle")
            worker._stop_event.clear()  # allow the hand-driven sweep to run

            # Inside the grace: held for register_store, nothing sent.
            worker._deliver_memory_buffer_direct()
            assert len(worker._memory_buffer) == 1
            mock_instance.notify.assert_not_called()

            # Past the grace: no store is coming → direct delivery.
            worker._start_mono = time.monotonic() - 60
            worker._deliver_memory_buffer_direct()
            assert worker._memory_buffer == []
            assert mock_instance.notify.call_count == 1
        finally:
            worker.stop()

    @pytest.mark.unit
    @patch("eneru.notifications.APPRISE_AVAILABLE", True)
    @patch("eneru.notifications.apprise")
    def test_direct_delivery_failure_backs_off(
        self, mock_apprise, notification_config, tmp_path
    ):
        """A refusing endpoint keeps the rows buffered and arms a backoff so
        the direct path doesn't hammer Apprise every 1s tick; a later
        success resets the backoff."""
        mock_instance = _patch_apprise(mock_apprise, succeed=False)
        closed_store = StatsStore(tmp_path / "never-opened.db")
        worker = NotificationWorker(notification_config)
        try:
            worker.start()
            worker.stop()
            worker.register_store(closed_store)
            worker._warn = lambda *_: None
            worker.send("stuck-1", "info", "shutdown")
            worker.send("stuck-2", "info", "shutdown")
            worker._stop_event.clear()

            worker._deliver_memory_buffer_direct()
            # Sweep stopped at the FIRST failure (one blocking call, not two).
            assert mock_instance.notify.call_count == 1
            assert len(worker._memory_buffer) == 2
            assert worker._buffer_direct_attempts == 1
            assert worker._buffer_direct_next_mono > time.monotonic()

            # Within the backoff window: no new attempt at all.
            worker._deliver_memory_buffer_direct()
            assert mock_instance.notify.call_count == 1

            # Backoff elapsed + endpoint recovered: both rows deliver,
            # backoff state resets.
            worker._buffer_direct_next_mono = 0.0
            mock_instance.notify.return_value = True
            worker._deliver_memory_buffer_direct()
            assert worker._memory_buffer == []
            assert worker._buffer_direct_attempts == 0
        finally:
            worker.stop()

    @pytest.mark.unit
    @patch("eneru.notifications.APPRISE_AVAILABLE", True)
    @patch("eneru.notifications.apprise")
    def test_direct_delivery_partial_failure_keeps_undelivered(
        self, mock_apprise, notification_config, tmp_path
    ):
        """First row delivers, second fails → only the delivered row leaves
        the buffer; age order of the remainder is preserved."""
        mock_instance = _patch_apprise(mock_apprise, succeed=True)
        mock_instance.notify.side_effect = [True, False]
        closed_store = StatsStore(tmp_path / "never-opened.db")
        worker = NotificationWorker(notification_config)
        try:
            worker.start()
            worker.stop()
            worker.register_store(closed_store)
            worker._warn = lambda *_: None
            worker.send("first", "info", "shutdown")
            worker.send("second", "info", "shutdown")
            worker._stop_event.clear()

            worker._deliver_memory_buffer_direct()
            assert [row[0] for row in worker._memory_buffer] == ["second"]
        finally:
            worker.stop()

    @pytest.mark.unit
    @patch("eneru.notifications.APPRISE_AVAILABLE", True)
    @patch("eneru.notifications.apprise")
    def test_direct_delivery_stops_on_stop_event(
        self, mock_apprise, notification_config, tmp_path
    ):
        """The sweep aborts promptly when the worker is being stopped."""
        mock_instance = _patch_apprise(mock_apprise, succeed=True)
        closed_store = StatsStore(tmp_path / "never-opened.db")
        worker = NotificationWorker(notification_config)
        try:
            worker.start()
            worker.stop()
            worker.register_store(closed_store)
            worker._warn = lambda *_: None
            worker.send("late", "info", "shutdown")

            # stop_event stays SET (from stop()) → sweep sends nothing.
            worker._deliver_memory_buffer_direct()
            mock_instance.notify.assert_not_called()
            assert len(worker._memory_buffer) == 1
        finally:
            worker.stop()

    @pytest.mark.unit
    @patch("eneru.notifications.APPRISE_AVAILABLE", True)
    @patch("eneru.notifications.apprise")
    def test_direct_delivery_noop_on_empty_buffer(
        self, mock_apprise, notification_config
    ):
        mock_instance = _patch_apprise(mock_apprise, succeed=True)
        worker = NotificationWorker(notification_config)
        try:
            worker.start()
            worker.stop()
            worker._stop_event.clear()
            worker._deliver_memory_buffer_direct()
            mock_instance.notify.assert_not_called()
        finally:
            worker.stop()

    @pytest.mark.unit
    @patch("eneru.notifications.APPRISE_AVAILABLE", True)
    @patch("eneru.notifications.apprise")
    def test_drain_and_adopt_memory_buffer(
        self, mock_apprise, notification_config
    ):
        """F-067(b): a reload bounce hands the old worker's buffer to the
        replacement — drain detaches, adopt prepends preserving age order."""
        _patch_apprise(mock_apprise, succeed=True)
        old = NotificationWorker(notification_config)
        old._memory_buffer = [("old-1", "info", "lifecycle", 100),
                              ("old-2", "info", "lifecycle", 200)]
        entries = old.drain_memory_buffer()
        assert [e[0] for e in entries] == ["old-1", "old-2"]
        assert old._memory_buffer == []

        new = NotificationWorker(notification_config)
        new._memory_buffer = [("new-1", "info", "lifecycle", 300)]
        new.adopt_memory_buffer(entries)
        assert [e[0] for e in new._memory_buffer] == ["old-1", "old-2", "new-1"]

        # No-op adoption paths.
        new.adopt_memory_buffer(None)
        new.adopt_memory_buffer([])
        assert len(new._memory_buffer) == 3

    @pytest.mark.unit
    @patch("eneru.notifications.APPRISE_AVAILABLE", True)
    @patch("eneru.notifications.apprise")
    def test_adopt_memory_buffer_respects_cap(
        self, mock_apprise, notification_config
    ):
        """Adoption enforces max_pending (oldest dropped) so a reload can't
        resurrect an unbounded backlog."""
        _patch_apprise(mock_apprise, succeed=True)
        notification_config.notifications.max_pending = 2
        worker = NotificationWorker(notification_config)
        worker._warn = lambda *_: None
        worker.adopt_memory_buffer([
            ("a", "info", "lifecycle", 1),
            ("b", "info", "lifecycle", 2),
            ("c", "info", "lifecycle", 3),
        ])
        assert [e[0] for e in worker._memory_buffer] == ["b", "c"]

    @pytest.mark.unit
    @patch("eneru.notifications.APPRISE_AVAILABLE", True)
    @patch("eneru.notifications.apprise")
    def test_stop_warns_about_buffered_rows(
        self, mock_apprise, notification_config
    ):
        """stop() must say how many memory-buffered rows are at risk instead
        of silently discarding them."""
        _patch_apprise(mock_apprise, succeed=True)
        worker = NotificationWorker(notification_config)
        try:
            worker.start()
            warnings = []
            worker._warn = warnings.append
            worker._memory_buffer = [("orphan", "info", "lifecycle", 1)]
            worker.stop()
            assert any("memory-buffered" in w for w in warnings)
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
# Backoff-map hygiene + bounded due scan (ISS-036)
# ==============================================================================

class TestBackoffMapHygiene:
    """ISS-036: the in-memory ``_backoff`` map must not leak entries for
    rows cancelled out-of-band, and the due-candidate scan must stay
    bounded without reintroducing head-of-queue starvation."""

    @pytest.mark.unit
    def test_prune_backoff_drops_entries_for_nonpending_rows(
        self, notification_config, registered_store,
    ):
        worker = NotificationWorker(notification_config)
        worker._stores = [registered_store]
        live_id = registered_store.enqueue_notification(
            "live", "info", "general")
        dead_id = registered_store.enqueue_notification(
            "dead", "info", "general")
        # Cancel one row out-of-band (as cap/coalesce/TTL would).
        registered_store.cancel_notification(dead_id, "coalesced")
        dbp = str(registered_store.db_path)
        worker._backoff = {
            (dbp, live_id): 1.0,               # still pending      -> keep
            (dbp, dead_id): 1.0,               # cancelled          -> drop
            (dbp, 999999): 1.0,                # never existed      -> drop
            # Key for a store we did NOT query this pass: conservatively
            # retained (we can't prove its row is gone).
            ("/gone/other.db", live_id): 1.0,
        }
        worker._prune_backoff([registered_store])
        assert (dbp, live_id) in worker._backoff
        assert (dbp, dead_id) not in worker._backoff
        assert (dbp, 999999) not in worker._backoff
        assert ("/gone/other.db", live_id) in worker._backoff

    @pytest.mark.unit
    def test_prune_backoff_keeps_entries_when_store_query_errors(
        self, notification_config, registered_store,
    ):
        """A transient ``pending_notification_ids`` failure (returns None)
        must NOT wipe that store's backoff timers — otherwise a DB hiccup
        would reset every message's exponential backoff to "retry now"."""
        worker = NotificationWorker(notification_config)
        worker._stores = [registered_store]
        registered_store.pending_notification_ids = lambda: None
        dbp = str(registered_store.db_path)
        worker._backoff = {(dbp, 1): 1.0, (dbp, 2): 1.0}
        worker._prune_backoff([registered_store])
        assert worker._backoff == {(dbp, 1): 1.0, (dbp, 2): 1.0}

    @pytest.mark.unit
    def test_prune_backoff_noop_when_empty(
        self, notification_config, registered_store,
    ):
        worker = NotificationWorker(notification_config)
        worker._stores = [registered_store]
        worker._backoff = {}
        worker._prune_backoff([registered_store])
        assert worker._backoff == {}

    @pytest.mark.unit
    def test_due_scan_cap_is_small_when_no_backoff(
        self, notification_config, registered_store,
    ):
        """Common case: with nothing backed off, the SELECT fetches only
        the ~50 floor rows instead of the full max_pending backlog."""
        worker = NotificationWorker(notification_config)
        worker._stores = [registered_store]
        for i in range(5):
            registered_store.enqueue_notification(f"m{i}", "info", "general")
        captured = {}
        real = registered_store.next_pending_notifications

        def spy(limit=10, offset=0):
            captured["limit"] = limit
            return real(limit=limit, offset=offset)

        registered_store.next_pending_notifications = spy
        worker._backoff = {}
        worker._next_due_candidate()
        assert captured["limit"] == 50

    @pytest.mark.unit
    def test_due_scan_grows_past_backed_off_head_no_starvation(
        self, notification_config, registered_store,
    ):
        """A cluster of backed-off head rows must not hide a newer due row
        (CR P1): the scan window grows by the number of suppressed rows."""
        worker = NotificationWorker(notification_config)
        worker._stores = [registered_store]
        ids = [
            registered_store.enqueue_notification(
                f"m{i}", "info", "general", ts=1000 + i)
            for i in range(60)
        ]
        dbp = str(registered_store.db_path)
        future = time.monotonic() + 10_000
        for rid in ids[:55]:
            worker._backoff[(dbp, rid)] = future
        cand = worker._next_due_candidate()
        assert cand is not None
        # Oldest DUE row is the 56th (first one not backed off).
        assert cand[2] == ids[55]


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
            body="⚠️  **POWER FAILURE DETECTED!**\nDetails: Battery 80%",
            notify_type="warning",
            category="power_event_on_battery", ts=1000,
        )
        registered_store.enqueue_notification(
            body="✅  **POWER RESTORED**\nDetails: Outage 60s",
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
            body="⚠️  **POWER FAILURE DETECTED!**\nDetails: Battery 80%",
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


# ==============================================================================
# v5.2.1 coverage gaps — error / fallback paths in notifications.py
# ==============================================================================

class TestErrorPaths:

    @pytest.mark.unit
    @patch("eneru.notifications.APPRISE_AVAILABLE", True)
    @patch("eneru.notifications.apprise")
    def test_memory_buffer_overflow_drops_oldest_and_warns(
        self, mock_apprise, capsys,
    ):
        """When sends arrive before any store registers and the buffer
        exceeds max_pending, the oldest get dropped and a one-shot
        warning prints. Without this cap a misconfigured daemon could
        OOM (CR P1 — already addressed; this test pins the behaviour)."""
        _patch_apprise(mock_apprise, succeed=True)
        config = Config()
        config.notifications = NotificationsConfig(
            enabled=True, urls=["discord://x"], title="t", timeout=5,
            max_pending=3,  # tiny cap to force overflow fast
        )
        worker = NotificationWorker(config)
        worker.start()
        try:
            # 5 sends with no store registered → all go to buffer; cap
            # at 3 means 2 oldest get dropped.
            for i in range(5):
                worker.send(f"msg-{i}", "info", "lifecycle")
            assert len(worker._memory_buffer) == 3
            # Newest survive; the 2 oldest were trimmed.
            bodies = [t[0] for t in worker._memory_buffer]
            assert bodies == ["msg-2", "msg-3", "msg-4"]
            # One-shot warning lands on stdout.
            captured = capsys.readouterr()
            assert "memory buffer exceeded" in captured.out
            # Subsequent overflows don't re-warn (one-shot).
            for i in range(5, 10):
                worker.send(f"msg-{i}", "info", "lifecycle")
            captured2 = capsys.readouterr()
            assert "memory buffer exceeded" not in captured2.out
        finally:
            worker.stop()

    @pytest.mark.unit
    @patch("eneru.notifications.APPRISE_AVAILABLE", True)
    @patch("eneru.notifications.apprise")
    def test_register_store_rebuffers_on_persistence_failure(
        self, mock_apprise, notification_config,
    ):
        """If a buffered row fails to persist on register_store (the
        store returns None for any reason), the row goes back into the
        memory buffer rather than getting silently dropped (CR P1)."""
        _patch_apprise(mock_apprise, succeed=True)
        worker = NotificationWorker(notification_config)
        worker.start()
        try:
            worker.send("buffered-1", "info", "lifecycle")
            worker.send("buffered-2", "info", "lifecycle")
            assert len(worker._memory_buffer) == 2
            # Stub store: enqueue_notification returns None (simulated
            # transient SQLite error). pending_notification_count
            # returns 0 so worker.stop()'s drain check doesn't trip on
            # the MagicMock's default truthy value.
            failing_store = MagicMock()
            failing_store.enqueue_notification.return_value = None
            failing_store.cap_pending_notifications.return_value = 0
            failing_store.pending_notification_count.return_value = 0
            worker.register_store(failing_store)
            # Both rows went back into the buffer because persistence
            # never succeeded.
            assert len(worker._memory_buffer) == 2
            bodies = [t[0] for t in worker._memory_buffer]
            assert "buffered-1" in bodies and "buffered-2" in bodies
        finally:
            worker.stop()

    @pytest.mark.unit
    @patch("eneru.notifications.APPRISE_AVAILABLE", True)
    @patch("eneru.notifications.apprise")
    def test_flush_returns_false_when_buffer_nonempty_no_store(
        self, mock_apprise, notification_config,
    ):
        """flush() must include the in-memory buffer in its drain check
        — without that, a shutdown that fires before any store
        registered would return True immediately and drop the buffered
        message (CR P1)."""
        _patch_apprise(mock_apprise, succeed=True)
        worker = NotificationWorker(notification_config)
        worker.start()
        try:
            worker.send("never-persisted", "info", "lifecycle")
            # No store registered; the row sits in memory.
            assert len(worker._memory_buffer) == 1
            # flush should report "not drained" because the buffer is
            # not empty even though no store has any pending.
            assert worker.flush(timeout=0.2) is False
        finally:
            worker.stop()


# ====================================================================
# NotificationWorker.start() — early-return paths
# ====================================================================


class TestNotificationWorkerStartEarlyReturns:
    """`start()` is the entry point for the worker thread. It returns
    False without starting if any precondition isn't met — verify each
    branch so a regression doesn't silently start a worker that can't
    actually deliver."""

    @pytest.mark.unit
    def test_start_returns_false_when_notifications_disabled(self, minimal_config):
        from eneru.notifications import NotificationWorker
        minimal_config.notifications.enabled = False
        worker = NotificationWorker(minimal_config)
        assert worker.start() is False
        assert worker._worker_thread is None

    @pytest.mark.unit
    def test_start_returns_false_when_apprise_unavailable(self, minimal_config):
        from eneru import notifications as notif_mod
        minimal_config.notifications.enabled = True
        minimal_config.notifications.urls = ["discord://fake/url"]
        worker = notif_mod.NotificationWorker(minimal_config)
        with patch.object(notif_mod, "APPRISE_AVAILABLE", False):
            assert worker.start() is False
        assert worker._worker_thread is None

    @pytest.mark.unit
    def test_start_returns_false_when_no_urls(self, minimal_config):
        from eneru.notifications import NotificationWorker
        minimal_config.notifications.enabled = True
        minimal_config.notifications.urls = []  # No URLs configured
        worker = NotificationWorker(minimal_config)
        assert worker.start() is False

    @pytest.mark.unit
    def test_start_returns_false_when_all_urls_invalid(self, minimal_config, capsys):
        """If apprise.add() rejects every URL, start() warns + returns False."""
        from eneru.notifications import NotificationWorker
        minimal_config.notifications.enabled = True
        minimal_config.notifications.urls = ["bogus://not-a-real-scheme/x"]
        worker = NotificationWorker(minimal_config)

        # L1: patch the whole `apprise` module object (like the sibling tests),
        # not its `.Apprise` attribute -- when the optional apprise extra isn't
        # installed `eneru.notifications.apprise` is None, and patching an
        # attribute of None raises AttributeError. This keeps the test hermetic.
        with patch("eneru.notifications.APPRISE_AVAILABLE", True), \
                patch("eneru.notifications.apprise") as mock_apprise:
            fake_apprise_instance = MagicMock()
            # add() returns False for every URL, len() returns 0
            fake_apprise_instance.add = MagicMock(return_value=False)
            fake_apprise_instance.__len__ = MagicMock(return_value=0)
            mock_apprise.Apprise.return_value = fake_apprise_instance
            assert worker.start() is False

        out = capsys.readouterr().out
        assert "Failed to add notification URL" in out or "No valid notification URLs" in out


# ====================================================================
# Defensive branches: register_store edge cases, drain/coalesce
# exception swallowing, apprise instance error paths, accessor returns
# ====================================================================


class TestRegisterStoreEdgeCases:
    """``register_store`` guards: None argument + duplicate registration."""

    @pytest.mark.unit
    def test_register_store_ignores_none(self, notification_config):
        """A ``None`` store must be a no-op (notifications.py line 172).

        Some startup paths call register_store from a try/except wrapper
        that may pass None when stats opening fails; the worker must
        accept that without bookkeeping the bogus entry.
        """
        worker = NotificationWorker(notification_config)
        worker.register_store(None)
        # Stores list still empty.
        with worker._stores_lock:
            assert worker._stores == []

    @pytest.mark.unit
    def test_register_store_ignores_duplicate(self, notification_config):
        """Registering the same store twice must not add a second entry
        (notifications.py line 175)."""
        worker = NotificationWorker(notification_config)
        store = MagicMock()
        store.enqueue_notification.return_value = 1
        store.cap_pending_notifications.return_value = 0
        store.pending_notification_count.return_value = 0
        worker.register_store(store)
        worker.register_store(store)
        with worker._stores_lock:
            assert worker._stores == [store]


class TestDrainAndCoalesceExceptionPaths:
    """The worker thread must never die: drain + coalesce errors are
    swallowed so future notifications still ship (notifications.py
    lines 330-333 and 359-360)."""

    @pytest.mark.unit
    @patch("eneru.notifications.APPRISE_AVAILABLE", True)
    @patch("eneru.notifications.apprise")
    def test_worker_loop_swallows_drain_exception(
        self, mock_apprise, notification_config,
    ):
        """If _drain_once raises, the outer loop catches it and continues
        (lines 330-333)."""
        _patch_apprise(mock_apprise, succeed=True)
        worker = NotificationWorker(notification_config)
        calls = {"n": 0}

        def boom():
            calls["n"] += 1
            if calls["n"] == 1:
                # Signal another iteration so the loop runs again quickly.
                worker._wakeup_event.set()
                raise RuntimeError("drain blew up")

        worker._drain_once = boom
        worker._maybe_prune = lambda: None
        worker.start()
        try:
            # Wake immediately so the first iteration runs without the
            # 1 s tick.
            worker._wakeup_event.set()
            assert _wait_until(lambda: calls["n"] >= 2, timeout=3.0), (
                "worker thread should have looped past the exception"
            )
        finally:
            worker.stop()

    @pytest.mark.unit
    @patch("eneru.notifications.APPRISE_AVAILABLE", True)
    @patch("eneru.notifications.apprise")
    def test_drain_continues_when_coalesce_raises(
        self, mock_apprise, notification_config,
    ):
        """_coalesce_pending_outages errors must not block delivery
        (lines 359-360)."""
        _patch_apprise(mock_apprise, succeed=True)
        worker = NotificationWorker(notification_config)
        # Replace coalesce with one that always raises so the except
        # clause is hit on every drain iteration.
        worker._coalesce_pending_outages = MagicMock(
            side_effect=RuntimeError("coalesce blew up"),
        )

        store = MagicMock()
        store.cap_pending_notifications.return_value = 0
        store.pending_notification_count.return_value = 0
        store.enqueue_notification.return_value = 1
        # No candidates due so the drain loop exits after the coalesce
        # exception is swallowed.
        worker._next_due_candidate = lambda: None
        worker.register_store(store)
        # Should not raise even though every iteration triggers the
        # coalesce exception.
        worker._drain_once()


class TestCoalesceLineIndexBranches:
    """Coalesce skipping logic when on_line rows are older than the
    earliest on_battery or when enqueue of the summary fails."""

    @pytest.mark.unit
    @patch("eneru.notifications.APPRISE_AVAILABLE", True)
    @patch("eneru.notifications.apprise")
    def test_coalesce_skips_older_on_line_rows(
        self, mock_apprise, notification_config,
    ):
        """An on_line row with ts < the earliest on_battery is skipped so
        we don't pair it with a later outage (notifications.py line 412).
        """
        _patch_apprise(mock_apprise, succeed=True)
        worker = NotificationWorker(notification_config)

        store = MagicMock()
        # OB at t=100; OL rows at t=50 (older, must be skipped) and
        # t=200 (the real partner).
        store.find_pending_by_category.side_effect = lambda cat: {
            "power_event_on_battery": [(1, 100, "body-ob", "info")],
            "power_event_on_line": [
                (2, 50, "body-old-ol", "info"),
                (3, 200, "body-ol", "info"),
            ],
        }[cat]
        store.enqueue_notification.return_value = 99

        pairs = worker._coalesce_pending_outages(store)
        assert pairs == 1
        # The summary was enqueued exactly once...
        assert store.enqueue_notification.call_count == 1
        # ...and only the genuine pair was cancelled (ids 1 and 3, not 2).
        cancelled_ids = sorted(
            c.args[0] for c in store.cancel_notification.call_args_list
        )
        assert cancelled_ids == [1, 3]

    @pytest.mark.unit
    @patch("eneru.notifications.APPRISE_AVAILABLE", True)
    @patch("eneru.notifications.apprise")
    def test_coalesce_breaks_when_on_line_exhausted(
        self, mock_apprise, notification_config,
    ):
        """Multiple on_battery rows with not enough on_line partners: we
        break out of the loop on the unpaired tail (line 414)."""
        _patch_apprise(mock_apprise, succeed=True)
        worker = NotificationWorker(notification_config)

        store = MagicMock()
        store.find_pending_by_category.side_effect = lambda cat: {
            "power_event_on_battery": [
                (1, 100, "body-ob1", "info"),
                (2, 300, "body-ob2", "info"),  # unpaired tail
            ],
            "power_event_on_line": [(3, 200, "body-ol", "info")],
        }[cat]
        store.enqueue_notification.return_value = 99

        # Should pair the first OB with the OL and stop; second OB has
        # no partner.
        pairs = worker._coalesce_pending_outages(store)
        assert pairs == 1

    @pytest.mark.unit
    @patch("eneru.notifications.APPRISE_AVAILABLE", True)
    @patch("eneru.notifications.apprise")
    def test_coalesce_skips_pair_when_summary_enqueue_fails(
        self, mock_apprise, notification_config,
    ):
        """If the summary row fails to persist, the OB/OL pair must NOT
        be cancelled — they ship individually (notifications.py lines
        436-437)."""
        _patch_apprise(mock_apprise, succeed=True)
        worker = NotificationWorker(notification_config)

        store = MagicMock()
        store.find_pending_by_category.side_effect = lambda cat: {
            "power_event_on_battery": [(1, 100, "body-ob", "info")],
            "power_event_on_line": [(2, 200, "body-ol", "info")],
        }[cat]
        # Enqueue returns None → simulated SQLite hiccup.
        store.enqueue_notification.return_value = None

        pairs = worker._coalesce_pending_outages(store)
        assert pairs == 0
        # Critical: no cancellations -- both originals stay pending.
        store.cancel_notification.assert_not_called()


class TestSendViaAppriseEdgeCases:

    @pytest.mark.unit
    def test_send_via_apprise_returns_false_without_instance(
        self, notification_config,
    ):
        """When the apprise instance hasn't been initialized (start()
        not called), _send_via_apprise must reject the message rather
        than crashing on None (notifications.py line 514)."""
        worker = NotificationWorker(notification_config)
        assert worker._apprise_instance is None
        assert worker._send_via_apprise("hi", "info") is False

    @pytest.mark.unit
    @patch("eneru.notifications.APPRISE_AVAILABLE", True)
    @patch("eneru.notifications.apprise")
    def test_send_via_apprise_swallows_notify_exception(
        self, mock_apprise, notification_config,
    ):
        """apprise.notify() raising (DNS, ssl, ...) must surface as False
        so the row stays pending and gets retried (lines 535-536)."""
        _patch_apprise(
            mock_apprise, side_effect=RuntimeError("network blew up"),
        )
        worker = NotificationWorker(notification_config)
        worker.start()
        try:
            assert worker._send_via_apprise("body", "info") is False
        finally:
            worker.stop()


class TestAccessorEdgeCases:

    @pytest.mark.unit
    def test_get_service_count_zero_without_instance(self, notification_config):
        """Without an Apprise instance, service count is 0
        (notifications.py line 566)."""
        worker = NotificationWorker(notification_config)
        assert worker._apprise_instance is None
        assert worker.get_service_count() == 0

    @pytest.mark.unit
    def test_get_pending_count_sums_across_stores(self, notification_config):
        """get_pending_count must sum pending across every registered
        store (notifications.py lines 572-573)."""
        worker = NotificationWorker(notification_config)
        a = MagicMock()
        a.pending_notification_count.return_value = 4
        a.enqueue_notification.return_value = 1
        a.cap_pending_notifications.return_value = 0
        b = MagicMock()
        b.pending_notification_count.return_value = 7
        b.enqueue_notification.return_value = 1
        b.cap_pending_notifications.return_value = 0
        worker.register_store(a)
        worker.register_store(b)
        assert worker.get_pending_count() == 11


@pytest.mark.unit
def test_module_records_apprise_available_flag_matches_import_state():
    """The ImportError fallback constants (lines 33-35) must keep the
    module importable when apprise isn't installed. We can't uninstall
    apprise in the test environment, but we can assert the constants
    exist and are consistent with the actual import."""
    import eneru.notifications as notif_mod
    if notif_mod.apprise is None:
        assert notif_mod.APPRISE_AVAILABLE is False
    else:
        assert notif_mod.APPRISE_AVAILABLE is True


@pytest.mark.unit
def test_worker_loop_survives_iteration_crash():
    """Behavioural-gap 8: an exception thrown during a single worker-loop
    iteration is swallowed and the loop keeps running. Losing the worker would
    silently disable ALL future notifications, so a crash must never escape."""
    worker = NotificationWorker(Config())
    # Make the per-iteration wait return instantly so the loop spins fast.
    worker._wakeup_event = MagicMock()
    worker._wakeup_event.wait.return_value = True

    calls = []

    def drain():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("iteration boom")   # first pass crashes
        worker._stop_event.set()                   # second pass ends the loop

    with patch.object(worker, "_drain_once", side_effect=drain), \
         patch.object(worker, "_maybe_prune"):
        worker._worker_loop()   # returns cleanly despite the first-pass crash

    # It ran again after the crash -> the worker did NOT die on the exception.
    assert len(calls) >= 2
