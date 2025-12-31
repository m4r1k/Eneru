"""Eneru - Intelligent UPS Monitoring & Shutdown Orchestration for NUT."""

from eneru.monitor import (
    __version__,
    # Configuration classes
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
    ComposeFileConfig,
    FilesystemsConfig,
    UnmountConfig,
    RemoteServerConfig,
    RemoteCommandConfig,
    LocalShutdownConfig,
    # State and loader
    MonitorState,
    ConfigLoader,
    # Core classes
    UPSMonitor,
    NotificationWorker,
    # Functions
    main,
    run_command,
    command_exists,
    is_numeric,
    format_seconds,
    REMOTE_ACTIONS,
    # Availability flags
    YAML_AVAILABLE,
    APPRISE_AVAILABLE,
)

__all__ = [
    "__version__",
    # Configuration classes
    "Config",
    "UPSConfig",
    "TriggersConfig",
    "DepletionConfig",
    "ExtendedTimeConfig",
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
    "UPSMonitor",
    "NotificationWorker",
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
