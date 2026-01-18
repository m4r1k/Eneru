"""Notification worker for Eneru."""

import queue
import threading
from typing import Optional, Dict, Any

from eneru.config import Config

# Optional import for Apprise
try:
    import apprise
    APPRISE_AVAILABLE = True
except ImportError:
    apprise = None
    APPRISE_AVAILABLE = False


class NotificationWorker:
    """Non-blocking notification worker with persistent retry using a background thread.

    This worker ensures that notifications never block the main monitoring loop
    or shutdown sequence. The main thread queues notifications instantly and
    continues with critical operations. The worker thread persistently retries
    failed notifications until they succeed (or the process exits).

    Architecture:
    - Main thread: Queues notifications instantly (non-blocking)
    - Worker thread: Processes queue in FIFO order, retrying each message
      until successful before moving to the next one
    - Apprise handles parallel delivery to multiple backends

    This design ensures:
    1. Zero impact on shutdown operations (main thread never waits)
    2. Guaranteed delivery during transient network issues
    3. Order preservation (FIFO queue, no message skipping)
    4. No message loss during brief outages (e.g., 30-second power blip)
    """

    def __init__(self, config: Config):
        self.config = config
        self._queue: queue.Queue = queue.Queue()
        self._worker_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._apprise_instance: Optional[Any] = None
        self._initialized = False
        self._retry_count = 0  # Track retries for current message (for logging)

    def start(self) -> bool:
        """Initialize Apprise and start the background worker thread."""
        if not self.config.notifications.enabled:
            return False

        if not APPRISE_AVAILABLE:
            return False

        if not self.config.notifications.urls:
            return False

        # Initialize Apprise
        self._apprise_instance = apprise.Apprise()

        for url in self.config.notifications.urls:
            if not self._apprise_instance.add(url):
                print(f"Warning: Failed to add notification URL: {url}")

        if len(self._apprise_instance) == 0:
            print("Warning: No valid notification URLs configured")
            return False

        # Start background worker thread (daemon=True ensures it won't block shutdown)
        self._stop_event.clear()
        self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker_thread.start()
        self._initialized = True

        return True

    def stop(self):
        """Stop the background worker thread gracefully.

        Note: During shutdown, the worker will attempt to send any pending
        notifications. Messages that cannot be delivered before process exit
        will be lost, but this is acceptable since journalctl logs remain
        for forensics.
        """
        if self._worker_thread and self._worker_thread.is_alive():
            # Log pending notifications
            pending = self._queue.qsize()
            in_progress = 1 if self._retry_count > 0 else 0
            total_pending = pending + in_progress
            if total_pending > 0:
                retry_info = f" (current message: retry #{self._retry_count})" if in_progress else ""
                print(f"⚠️ Stopping notification worker with {total_pending} message(s) pending{retry_info}")

            self._stop_event.set()
            # Add sentinel to unblock the queue
            self._queue.put(None)
            # Don't wait too long - we might be shutting down
            self._worker_thread.join(timeout=2)

    def send(self, body: str, notify_type: str = "info", blocking: bool = False):
        """
        Queue a notification for sending.

        Args:
            body: Notification body
            notify_type: One of 'info', 'success', 'warning', 'failure'
            blocking: If True, wait for notification to be sent.
                      NOTE: This should only be used for test notifications,
                      never during shutdown sequences where network may be down.
        """
        if not self._initialized:
            return

        notification = {
            'title': self.config.notifications.title,  # Can be None
            'body': body,
            'notify_type': notify_type,
            'blocking_event': threading.Event() if blocking else None,
        }

        self._queue.put(notification)

        # If blocking, wait for the notification to be processed
        # This should ONLY be used for --test-notifications, never during shutdown
        if blocking and notification['blocking_event']:
            # For blocking calls, use a generous timeout that allows for retries
            max_wait = self.config.notifications.timeout * 3 + 10
            notification['blocking_event'].wait(timeout=max_wait)

    def _worker_loop(self):
        """Background worker that processes the notification queue with persistent retry."""
        while not self._stop_event.is_set():
            try:
                notification = self._queue.get(timeout=1)

                if notification is None:
                    # Sentinel value, exit loop
                    break

                self._send_with_retry(notification)

            except queue.Empty:
                continue
            except Exception:
                # Silently ignore errors - notifications should never crash the monitor
                pass

    def _send_with_retry(self, notification: Dict[str, Any]):
        """Send notification with persistent retry until success or stop signal."""
        self._retry_count = 0
        retry_interval = self.config.notifications.retry_interval

        while not self._stop_event.is_set():
            success = self._send_notification(notification)

            if success:
                self._retry_count = 0
                return

            # Failed - wait and retry
            self._retry_count += 1

            # Use stop_event.wait() instead of time.sleep() so we can interrupt quickly
            if self._stop_event.wait(timeout=retry_interval):
                # Stop was requested during wait
                break

        # If we exit the loop without success (stop requested), reset counter
        self._retry_count = 0
        # Signal completion even on failure (for blocking calls)
        if notification.get('blocking_event'):
            notification['blocking_event'].set()

    def _send_notification(self, notification: Dict[str, Any]) -> bool:
        """Actually send the notification via Apprise.

        Returns:
            True if notification was sent successfully, False otherwise.
        """
        if not self._apprise_instance:
            return False

        try:
            # Map notify_type string to Apprise NotifyType
            type_map = {
                "info": apprise.NotifyType.INFO,
                "success": apprise.NotifyType.SUCCESS,
                "warning": apprise.NotifyType.WARNING,
                "failure": apprise.NotifyType.FAILURE,
            }
            notify_type = type_map.get(notification['notify_type'], apprise.NotifyType.INFO)

            # Build notification parameters
            notify_kwargs = {
                'body': notification['body'],
                'notify_type': notify_type,
            }

            # Only add title if configured (not None/empty)
            if notification.get('title'):
                notify_kwargs['title'] = notification['title']

            # Apprise.notify() returns True if at least one notification succeeded
            success = self._apprise_instance.notify(**notify_kwargs)

            if success:
                # Signal completion for blocking calls
                if notification.get('blocking_event'):
                    notification['blocking_event'].set()

            return success

        except Exception:
            # Network error, DNS failure, etc. - will retry
            return False

    def get_service_count(self) -> int:
        """Return the number of configured notification services."""
        if self._apprise_instance:
            return len(self._apprise_instance)
        return 0

    def get_queue_size(self) -> int:
        """Return the number of pending notifications in the queue."""
        return self._queue.qsize()

    def get_retry_count(self) -> int:
        """Return the current retry count for the message being processed."""
        return self._retry_count
