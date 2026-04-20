"""Eneru - Intelligent UPS Monitoring & Shutdown Orchestration for NUT."""

from eneru.version import __version__
from eneru.config import (
    Config,
    UPSConfig,
    UPSGroupConfig,
    RedundancyGroupConfig,
    TriggersConfig,
    DepletionConfig,
    ExtendedTimeConfig,
    ConnectionLossGracePeriodConfig,
    BehaviorConfig,
    LoggingConfig,
    NotificationsConfig,
    VMConfig,
    ContainersConfig,
    ComposeFileConfig,
    FilesystemsConfig,
    UnmountConfig,
    RemoteServerConfig,
    RemoteCommandConfig,
    LocalShutdownConfig,
    ConfigLoader,
    YAML_AVAILABLE,
)
from eneru.state import MonitorState
from eneru.logger import UPSLogger, TimezoneFormatter
from eneru.notifications import NotificationWorker, APPRISE_AVAILABLE
from eneru.utils import run_command, command_exists, is_numeric, format_seconds
from eneru.actions import REMOTE_ACTIONS
from eneru.monitor import UPSGroupMonitor
from eneru.multi_ups import MultiUPSCoordinator
from eneru.cli import main

__all__ = [
    "__version__",
    # Configuration classes
    "Config",
    "UPSConfig",
    "UPSGroupConfig",
    "RedundancyGroupConfig",
    "TriggersConfig",
    "DepletionConfig",
    "ExtendedTimeConfig",
    "ConnectionLossGracePeriodConfig",
    "BehaviorConfig",
    "LoggingConfig",
    "NotificationsConfig",
    "VMConfig",
    "ContainersConfig",
    "ComposeFileConfig",
    "FilesystemsConfig",
    "UnmountConfig",
    "RemoteServerConfig",
    "RemoteCommandConfig",
    "LocalShutdownConfig",
    # State and loader
    "MonitorState",
    "ConfigLoader",
    # Core classes
    "UPSGroupMonitor",
    "MultiUPSCoordinator",
    "NotificationWorker",
    # Logger classes
    "UPSLogger",
    "TimezoneFormatter",
    # Functions
    "main",
    "run_command",
    "command_exists",
    "is_numeric",
    "format_seconds",
    "REMOTE_ACTIONS",
    # Availability flags
    "YAML_AVAILABLE",
    "APPRISE_AVAILABLE",
]
