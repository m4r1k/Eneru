"""Shared test fixtures and configuration."""

import pytest
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch
from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional
from collections import deque

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from ups_monitor import (
    Config,
    UPSConfig,
    TriggersConfig,
    DepletionConfig,
    ExtendedTimeConfig,
    BehaviorConfig,
    LoggingConfig,
    NotificationsConfig,
    VMConfig,
    ContainersConfig,
    FilesystemsConfig,
    UnmountConfig,
    RemoteServerConfig,
    LocalShutdownConfig,
    MonitorState,
    ConfigLoader,
)


@pytest.fixture
def default_config() -> Config:
    """Create a default configuration for testing."""
    return Config()


@pytest.fixture
def minimal_config() -> Config:
    """Create a minimal configuration."""
    config = Config()
    config.ups.name = "TestUPS@localhost"
    config.behavior.dry_run = True
    config.notifications.enabled = False
    config.virtual_machines.enabled = False
    config.containers.enabled = False
    config.filesystems.unmount.enabled = False
    config.local_shutdown.enabled = False
    return config


@pytest.fixture
def full_config() -> Config:
    """Create a fully-configured configuration for testing."""
    config = Config()
    config.ups = UPSConfig(
        name="UPS@192.168.1.100",
        check_interval=1,
        max_stale_data_tolerance=3,
    )
    config.triggers = TriggersConfig(
        low_battery_threshold=20,
        critical_runtime_threshold=600,
        depletion=DepletionConfig(
            window=300,
            critical_rate=15.0,
            grace_period=90,
        ),
        extended_time=ExtendedTimeConfig(
            enabled=True,
            threshold=900,
        ),
    )
    config.behavior = BehaviorConfig(dry_run=True)
    config.notifications = NotificationsConfig(
        enabled=True,
        urls=["discord://test/test"],
        title="Test UPS",
        timeout=10,
    )
    config.virtual_machines = VMConfig(enabled=True, max_wait=30)
    config.containers = ContainersConfig(
        enabled=True,
        runtime="auto",
        stop_timeout=60,
    )
    config.filesystems = FilesystemsConfig(
        sync_enabled=True,
        unmount=UnmountConfig(
            enabled=True,
            timeout=15,
            mounts=[
                {"path": "/mnt/test1", "options": ""},
                {"path": "/mnt/test2", "options": "-l"},
            ],
        ),
    )
    config.remote_servers = [
        RemoteServerConfig(
            name="Test Server",
            enabled=True,
            host="192.168.1.50",
            user="admin",
            shutdown_command="sudo shutdown -h now",
        ),
    ]
    config.local_shutdown = LocalShutdownConfig(
        enabled=True,
        command="shutdown -h now",
        message="Test shutdown",
    )
    return config


@pytest.fixture
def monitor_state() -> MonitorState:
    """Create a fresh monitor state."""
    return MonitorState()


@pytest.fixture
def temp_config_file(tmp_path) -> Path:
    """Create a temporary config file."""
    config_file = tmp_path / "config.yaml"
    return config_file


@pytest.fixture
def sample_ups_data() -> Dict[str, str]:
    """Sample UPS data as returned by upsc."""
    return {
        "ups.status": "OL CHRG",
        "battery.charge": "100",
        "battery.runtime": "1800",
        "ups.load": "25",
        "input.voltage": "230.5",
        "output.voltage": "230.0",
        "input.voltage.nominal": "230",
        "input.transfer.low": "170",
        "input.transfer.high": "280",
    }


@pytest.fixture
def sample_ups_data_on_battery() -> Dict[str, str]:
    """Sample UPS data when on battery."""
    return {
        "ups.status": "OB DISCHRG",
        "battery.charge": "85",
        "battery.runtime": "1200",
        "ups.load": "30",
        "input.voltage": "0.0",
        "output.voltage": "230.0",
    }


@pytest.fixture
def mock_run_command():
    """Mock the run_command function."""
    with patch("ups_monitor.run_command") as mock:
        mock.return_value = (0, "", "")
        yield mock


@pytest.fixture
def mock_apprise():
    """Mock the Apprise library."""
    with patch("ups_monitor.apprise") as mock:
        mock_instance = MagicMock()
        mock.Apprise.return_value = mock_instance
        mock_instance.add.return_value = True
        mock_instance.notify.return_value = True
        mock_instance.__len__ = lambda self: 1
        yield mock
