"""Shared test fixtures and configuration."""

import pytest
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch
from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional
from collections import deque

# Add src directory to path for eneru package imports
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
# Add tests directory to path for test_constants
sys.path.insert(0, str(Path(__file__).parent))

from test_constants import TEST_DISCORD_APPRISE_URL

from eneru import (
    Config,
    UPSConfig,
    UPSGroupConfig,
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
from eneru import config as eneru_config_module
from eneru import stats as eneru_stats_module


@pytest.fixture(autouse=True)
def isolate_stats_db_directory(request, tmp_path, monkeypatch):
    """Redirect every test's StatsConfig.db_directory default and any
    direct StatsStore(db_path=...) call to a per-test tmp_path, so no
    test leaks SQLite files into the real /var/lib/eneru.

    Tests that specifically need to verify the unmodified production
    default (e.g. asserting StatsConfig().db_directory == "/var/lib/
    eneru") can opt out via @pytest.mark.no_stats_isolation.

    Two layers of defense are required when the fixture is active:

    1. ``StatsConfig.__init__`` — the dataclass-generated ``__init__``
       captures the literal default ``"/var/lib/eneru"`` at class
       decoration time, so monkeypatching the class attribute is a
       no-op for new instances. We replace ``__init__`` with a
       wrapper that substitutes the isolated path when the caller
       didn't supply one, regardless of how the dataclass was
       generated.

    2. ``StatsStore.__init__`` — direct ``StatsStore(Path("/var/lib/
       eneru/foo.db"))`` calls (e.g. via TUI helpers) bypass
       StatsConfig entirely. Redirect any ``db_path`` whose parent is
       ``/var/lib/eneru`` into the isolated dir so it lands in
       tmp_path instead.
    """
    if request.node.get_closest_marker("no_stats_isolation"):
        yield None
        return

    isolated = tmp_path / "stats"
    isolated.mkdir(parents=True, exist_ok=True)
    isolated_str = str(isolated)
    real_dir = Path("/var/lib/eneru")

    # Layer 1: StatsConfig default.
    original_cfg_init = eneru_config_module.StatsConfig.__init__

    def patched_cfg_init(self, db_directory=isolated_str, **kw):
        return original_cfg_init(self, db_directory=db_directory, **kw)

    monkeypatch.setattr(
        eneru_config_module.StatsConfig, "__init__", patched_cfg_init,
    )

    # Layer 2: direct StatsStore instantiation.
    original_store_init = eneru_stats_module.StatsStore.__init__

    def patched_store_init(self, db_path, *args, **kw):
        try:
            p = Path(db_path)
        except TypeError:
            return original_store_init(self, db_path, *args, **kw)
        if p.parent == real_dir:
            p = isolated / p.name
        return original_store_init(self, p, *args, **kw)

    monkeypatch.setattr(
        eneru_stats_module.StatsStore, "__init__", patched_store_init,
    )

    yield isolated


@pytest.fixture
def default_config() -> Config:
    """Create a default configuration for testing."""
    return Config(ups_groups=[UPSGroupConfig()])


@pytest.fixture
def minimal_config() -> Config:
    """Create a minimal configuration."""
    return Config(
        ups_groups=[UPSGroupConfig(
            ups=UPSConfig(name="TestUPS@localhost"),
            virtual_machines=VMConfig(enabled=False),
            containers=ContainersConfig(enabled=False),
            filesystems=FilesystemsConfig(unmount=UnmountConfig(enabled=False)),
            is_local=True,
        )],
        behavior=BehaviorConfig(dry_run=True),
        notifications=NotificationsConfig(enabled=False),
        local_shutdown=LocalShutdownConfig(enabled=False),
    )


@pytest.fixture
def full_config() -> Config:
    """Create a fully-configured configuration for testing."""
    return Config(
        ups_groups=[UPSGroupConfig(
            ups=UPSConfig(
                name="UPS@192.168.1.100",
                check_interval=1,
                max_stale_data_tolerance=3,
            ),
            triggers=TriggersConfig(
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
            ),
            virtual_machines=VMConfig(enabled=True, max_wait=30),
            containers=ContainersConfig(
                enabled=True,
                runtime="auto",
                stop_timeout=60,
            ),
            filesystems=FilesystemsConfig(
                sync_enabled=True,
                unmount=UnmountConfig(
                    enabled=True,
                    timeout=15,
                    mounts=[
                        {"path": "/mnt/test1", "options": ""},
                        {"path": "/mnt/test2", "options": "-l"},
                    ],
                ),
            ),
            remote_servers=[
                RemoteServerConfig(
                    name="Test Server",
                    enabled=True,
                    host="192.168.1.50",
                    user="admin",
                    shutdown_command="sudo shutdown -h now",
                ),
            ],
            is_local=True,
        )],
        behavior=BehaviorConfig(dry_run=True),
        notifications=NotificationsConfig(
            enabled=True,
            urls=[TEST_DISCORD_APPRISE_URL],
            title="Test UPS",
            timeout=10,
        ),
        local_shutdown=LocalShutdownConfig(
            enabled=True,
            command="shutdown -h now",
            message="Test shutdown",
        ),
    )


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
    with patch("eneru.monitor.run_command") as mock:
        mock.return_value = (0, "", "")
        yield mock


@pytest.fixture
def patch_run_command_everywhere():
    """Patch ``run_command`` in every module that imported it under its
    own name. ``from eneru.utils import run_command`` binds the symbol
    at import time, so ``patch("eneru.utils.run_command")`` is a no-op
    for already-imported modules — tests that go through a shutdown
    mixin (vms/containers/filesystems/remote) must patch each binding
    explicitly or the mixin's call will hit real ``virsh``/``umount``/
    ``ssh`` despite the test's intent.

    Yields a dict mapping the module path → MagicMock so a test can
    assert against any specific binding. Each mock returns
    ``(0, "", "")`` by default; override per-test as needed.
    """
    targets = [
        "eneru.monitor.run_command",
        "eneru.multi_ups.run_command",
        "eneru.shutdown.vms.run_command",
        "eneru.shutdown.containers.run_command",
        "eneru.shutdown.filesystems.run_command",
        "eneru.shutdown.remote.run_command",
    ]
    patchers = [patch(t) for t in targets]
    mocks = {t: p.start() for t, p in zip(targets, patchers)}
    for m in mocks.values():
        m.return_value = (0, "", "")
    try:
        yield mocks
    finally:
        for p in patchers:
            p.stop()


@pytest.fixture
def mock_apprise():
    """Mock the Apprise library."""
    with patch("eneru.notifications.apprise") as mock:
        mock_instance = MagicMock()
        mock.Apprise.return_value = mock_instance
        mock_instance.add.return_value = True
        mock_instance.notify.return_value = True
        mock_instance.__len__ = lambda self: 1
        yield mock
