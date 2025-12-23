"""Tests for notification system."""

import pytest
import time
import threading
from unittest.mock import patch, MagicMock

from ups_monitor import (
    NotificationWorker,
    Config,
    NotificationsConfig,
)


# Module-level fixture for notification config
@pytest.fixture
def notification_config():
    """Create a config with notifications enabled."""
    config = Config()
    config.notifications = NotificationsConfig(
        enabled=True,
        urls=["discord://test/token"],
        title="Test UPS",
        timeout=5,
    )
    return config


class TestNotificationWorker:
    """Test the notification worker."""

    @pytest.mark.unit
    def test_worker_not_initialized_when_disabled(self):
        """Test that worker doesn't start when notifications disabled."""
        config = Config()
        config.notifications.enabled = False

        worker = NotificationWorker(config)
        result = worker.start()

        assert result is False
        assert worker._initialized is False

    @pytest.mark.unit
    def test_worker_not_initialized_without_urls(self):
        """Test that worker doesn't start without URLs."""
        config = Config()
        config.notifications.enabled = True
        config.notifications.urls = []

        worker = NotificationWorker(config)
        result = worker.start()

        assert result is False

    @pytest.mark.unit
    @patch("ups_monitor.APPRISE_AVAILABLE", True)
    @patch("ups_monitor.apprise")
    def test_worker_starts_with_valid_config(self, mock_apprise, notification_config):
        """Test that worker starts with valid configuration."""
        mock_instance = MagicMock()
        mock_apprise.Apprise.return_value = mock_instance
        mock_instance.add.return_value = True
        mock_instance.__len__ = lambda self: 1

        worker = NotificationWorker(notification_config)
        result = worker.start()

        assert result is True
        assert worker._initialized is True
        assert worker._worker_thread is not None
        assert worker._worker_thread.daemon is True

        worker.stop()

    @pytest.mark.unit
    @patch("ups_monitor.APPRISE_AVAILABLE", True)
    @patch("ups_monitor.apprise")
    def test_send_queues_notification(self, mock_apprise, notification_config):
        """Test that send() queues notification without blocking."""
        mock_instance = MagicMock()
        mock_apprise.Apprise.return_value = mock_instance
        mock_instance.add.return_value = True
        mock_instance.__len__ = lambda self: 1

        worker = NotificationWorker(notification_config)
        worker.start()

        start_time = time.time()
        worker.send("Test message", "info", blocking=False)
        elapsed = time.time() - start_time

        # Should return almost immediately (non-blocking)
        assert elapsed < 0.1

        worker.stop()

    @pytest.mark.unit
    @patch("ups_monitor.APPRISE_AVAILABLE", True)
    @patch("ups_monitor.apprise")
    def test_send_with_blocking(self, mock_apprise, notification_config):
        """Test that blocking send waits for completion."""
        mock_instance = MagicMock()
        mock_apprise.Apprise.return_value = mock_instance
        mock_instance.add.return_value = True
        mock_instance.__len__ = lambda self: 1
        mock_instance.notify.return_value = True

        # Need to mock NotifyType as well
        mock_apprise.NotifyType = MagicMock()
        mock_apprise.NotifyType.INFO = "info"
        mock_apprise.NotifyType.SUCCESS = "success"
        mock_apprise.NotifyType.WARNING = "warning"
        mock_apprise.NotifyType.FAILURE = "failure"

        worker = NotificationWorker(notification_config)
        worker.start()

        # Give worker time to start
        time.sleep(0.1)

        worker.send("Test message", "info", blocking=True)

        # Verify notification was processed
        mock_instance.notify.assert_called()

        worker.stop()

    @pytest.mark.unit
    @patch("ups_monitor.APPRISE_AVAILABLE", True)
    @patch("ups_monitor.apprise")
    def test_worker_stop_graceful(self, mock_apprise, notification_config):
        """Test that worker stops gracefully."""
        mock_instance = MagicMock()
        mock_apprise.Apprise.return_value = mock_instance
        mock_instance.add.return_value = True
        mock_instance.__len__ = lambda self: 1

        worker = NotificationWorker(notification_config)
        worker.start()

        assert worker._worker_thread.is_alive()

        worker.stop()

        # Thread should be stopped
        time.sleep(0.1)
        assert not worker._worker_thread.is_alive()

    @pytest.mark.unit
    def test_send_does_nothing_when_not_initialized(self):
        """Test that send() is a no-op when not initialized."""
        config = Config()
        config.notifications.enabled = False

        worker = NotificationWorker(config)
        # Don't call start()

        # Should not raise any errors
        worker.send("Test message", "info")

    @pytest.mark.unit
    @patch("ups_monitor.APPRISE_AVAILABLE", True)
    @patch("ups_monitor.apprise")
    def test_get_service_count(self, mock_apprise, notification_config):
        """Test getting service count."""
        mock_instance = MagicMock()
        mock_apprise.Apprise.return_value = mock_instance
        mock_instance.add.return_value = True
        mock_instance.__len__ = lambda self: 3

        worker = NotificationWorker(notification_config)
        worker.start()

        assert worker.get_service_count() == 3

        worker.stop()


class TestNotificationTypes:
    """Test notification type mapping."""

    @pytest.mark.unit
    @patch("ups_monitor.APPRISE_AVAILABLE", True)
    @patch("ups_monitor.apprise")
    def test_notify_type_mapping(self, mock_apprise):
        """Test that notify types are correctly mapped."""
        mock_instance = MagicMock()
        mock_apprise.Apprise.return_value = mock_instance
        mock_instance.add.return_value = True
        mock_instance.__len__ = lambda self: 1
        mock_instance.notify.return_value = True

        mock_apprise.NotifyType = MagicMock()
        mock_apprise.NotifyType.INFO = "info"
        mock_apprise.NotifyType.SUCCESS = "success"
        mock_apprise.NotifyType.WARNING = "warning"
        mock_apprise.NotifyType.FAILURE = "failure"

        # Create config inline instead of using fixture
        config = Config()
        config.notifications = NotificationsConfig(
            enabled=True,
            urls=["discord://test/token"],
            title="Test",
            timeout=5,
        )

        worker = NotificationWorker(config)
        worker.start()
        time.sleep(0.1)

        # Test each type
        for notify_type in ["info", "success", "warning", "failure"]:
            worker.send(f"Test {notify_type}", notify_type, blocking=True)

        worker.stop()

        # Verify notify was called for each type
        assert mock_instance.notify.call_count >= 4
