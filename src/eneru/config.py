"""Configuration classes and loader for Eneru."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Dict, Any, List

from eneru.version import __version__

# Optional import for YAML
try:
    import yaml
    YAML_AVAILABLE = True
except ImportError:
    YAML_AVAILABLE = False


# ==============================================================================
# CONFIGURATION CLASSES
# ==============================================================================

@dataclass
class DepletionConfig:
    """Battery depletion tracking configuration."""
    window: int = 300
    critical_rate: float = 15.0
    grace_period: int = 90


@dataclass
class ExtendedTimeConfig:
    """Extended time on battery configuration."""
    enabled: bool = True
    threshold: int = 900


@dataclass
class TriggersConfig:
    """Shutdown triggers configuration."""
    low_battery_threshold: int = 20
    critical_runtime_threshold: int = 600
    depletion: DepletionConfig = field(default_factory=DepletionConfig)
    extended_time: ExtendedTimeConfig = field(default_factory=ExtendedTimeConfig)


@dataclass
class UPSConfig:
    """UPS connection configuration."""
    name: str = "UPS@localhost"
    check_interval: int = 1
    max_stale_data_tolerance: int = 3


@dataclass
class LoggingConfig:
    """Logging configuration."""
    file: Optional[str] = "/var/log/ups-monitor.log"
    state_file: str = "/var/run/ups-monitor.state"
    battery_history_file: str = "/var/run/ups-battery-history"
    shutdown_flag_file: str = "/var/run/ups-shutdown-scheduled"


@dataclass
class NotificationsConfig:
    """Notifications configuration using Apprise."""
    enabled: bool = False
    urls: List[str] = field(default_factory=list)
    title: Optional[str] = None  # None = no title sent
    avatar_url: Optional[str] = None
    timeout: int = 10
    retry_interval: int = 5  # Seconds between retry attempts for failed notifications


@dataclass
class VMConfig:
    """Virtual machine shutdown configuration."""
    enabled: bool = False
    max_wait: int = 30


@dataclass
class ComposeFileConfig:
    """Configuration for a single compose file."""
    path: str = ""
    stop_timeout: Optional[int] = None  # None = use global timeout


@dataclass
class ContainersConfig:
    """Container runtime shutdown configuration."""
    enabled: bool = False
    runtime: str = "auto"  # "auto", "docker", or "podman"
    stop_timeout: int = 60
    compose_files: List[ComposeFileConfig] = field(default_factory=list)
    shutdown_all_remaining_containers: bool = True
    include_user_containers: bool = False


@dataclass
class UnmountConfig:
    """Unmount configuration."""
    enabled: bool = False
    timeout: int = 15
    mounts: List[Dict[str, str]] = field(default_factory=list)


@dataclass
class FilesystemsConfig:
    """Filesystem operations configuration."""
    sync_enabled: bool = True
    unmount: UnmountConfig = field(default_factory=UnmountConfig)


@dataclass
class RemoteCommandConfig:
    """Configuration for a single remote pre-shutdown command."""
    action: Optional[str] = None  # predefined action name
    command: Optional[str] = None  # custom command
    timeout: Optional[int] = None  # per-command timeout (None = use server default)
    path: Optional[str] = None  # for stop_compose action


@dataclass
class RemoteServerConfig:
    """Remote server shutdown configuration."""
    name: str = ""
    enabled: bool = False
    host: str = ""
    user: str = ""
    connect_timeout: int = 10
    command_timeout: int = 30
    shutdown_command: str = "sudo shutdown -h now"
    ssh_options: List[str] = field(default_factory=list)
    pre_shutdown_commands: List[RemoteCommandConfig] = field(default_factory=list)
    parallel: bool = True  # If False, server is shutdown sequentially before parallel batch


@dataclass
class LocalShutdownConfig:
    """Local shutdown configuration."""
    enabled: bool = True
    command: str = "shutdown -h now"
    message: str = "UPS battery critical - emergency shutdown"


@dataclass
class BehaviorConfig:
    """Behavior configuration."""
    dry_run: bool = False


@dataclass
class Config:
    """Main configuration container."""
    ups: UPSConfig = field(default_factory=UPSConfig)
    triggers: TriggersConfig = field(default_factory=TriggersConfig)
    behavior: BehaviorConfig = field(default_factory=BehaviorConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    notifications: NotificationsConfig = field(default_factory=NotificationsConfig)
    virtual_machines: VMConfig = field(default_factory=VMConfig)
    containers: ContainersConfig = field(default_factory=ContainersConfig)
    filesystems: FilesystemsConfig = field(default_factory=FilesystemsConfig)
    remote_servers: List[RemoteServerConfig] = field(default_factory=list)
    local_shutdown: LocalShutdownConfig = field(default_factory=LocalShutdownConfig)

    # Notification types mapped to colors/severity
    NOTIFY_FAILURE: str = "failure"
    NOTIFY_WARNING: str = "warning"
    NOTIFY_SUCCESS: str = "success"
    NOTIFY_INFO: str = "info"


# ==============================================================================
# CONFIGURATION LOADER
# ==============================================================================

class ConfigLoader:
    """Loads and validates configuration from YAML file."""

    DEFAULT_CONFIG_PATHS = [
        Path("/etc/ups-monitor/config.yaml"),
        Path("/etc/ups-monitor/config.yml"),
        Path("./config.yaml"),
        Path("./config.yml"),
    ]

    @classmethod
    def load(cls, config_path: Optional[str] = None) -> Config:
        """Load configuration from file or use defaults."""
        config = Config()

        if not YAML_AVAILABLE:
            print("Warning: PyYAML not installed. Using default configuration.")
            print("Install with: pip install pyyaml")
            return config

        # Find config file
        if config_path:
            path = Path(config_path)
            if not path.exists():
                print(f"Warning: Config file not found: {path}")
                print("Using default configuration.")
                return config
        else:
            path = None
            for default_path in cls.DEFAULT_CONFIG_PATHS:
                if default_path.exists():
                    path = default_path
                    break

            if path is None:
                print("No config file found. Using default configuration.")
                return config

        # Load YAML
        try:
            with open(path, 'r') as f:
                data = yaml.safe_load(f) or {}
        except Exception as e:
            print(f"Error reading config file {path}: {e}")
            print("Using default configuration.")
            return config

        # Parse configuration sections
        config = cls._parse_config(data)
        print(f"Configuration loaded from: {path}")
        return config

    @classmethod
    def _convert_discord_webhook_to_apprise(cls, webhook_url: str) -> str:
        """Convert Discord webhook URL to Apprise format."""
        if webhook_url.startswith("https://discord.com/api/webhooks/"):
            parts = webhook_url.replace("https://discord.com/api/webhooks/", "").split("/")
            if len(parts) >= 2:
                webhook_id = parts[0]
                webhook_token = parts[1]
                return f"discord://{webhook_id}/{webhook_token}/"
        return webhook_url

    @classmethod
    def _append_avatar_to_url(cls, url: str, avatar_url: str) -> str:
        """Append avatar_url parameter to notification URLs that support it."""
        if not avatar_url:
            return url

        # Services that support avatar_url parameter
        avatar_supported_schemes = [
            'discord://',
            'slack://',
            'mattermost://',
            'guilded://',
            'zulip://',
        ]

        url_lower = url.lower()
        for scheme in avatar_supported_schemes:
            if url_lower.startswith(scheme):
                # Check if URL already has parameters
                separator = '&' if '?' in url else '?'
                # URL encode the avatar URL
                from urllib.parse import quote
                encoded_avatar = quote(avatar_url, safe='')
                return f"{url}{separator}avatar_url={encoded_avatar}"

        return url

    @classmethod
    def _parse_config(cls, data: Dict[str, Any]) -> Config:
        """Parse configuration dictionary into Config object."""
        config = Config()

        # UPS Configuration
        if 'ups' in data:
            ups_data = data['ups']
            config.ups = UPSConfig(
                name=ups_data.get('name', config.ups.name),
                check_interval=ups_data.get('check_interval', config.ups.check_interval),
                max_stale_data_tolerance=ups_data.get('max_stale_data_tolerance',
                                                      config.ups.max_stale_data_tolerance),
            )

        # Triggers Configuration
        if 'triggers' in data:
            triggers_data = data['triggers']
            depletion_data = triggers_data.get('depletion', {})
            extended_data = triggers_data.get('extended_time', {})

            config.triggers = TriggersConfig(
                low_battery_threshold=triggers_data.get('low_battery_threshold',
                                                        config.triggers.low_battery_threshold),
                critical_runtime_threshold=triggers_data.get('critical_runtime_threshold',
                                                             config.triggers.critical_runtime_threshold),
                depletion=DepletionConfig(
                    window=depletion_data.get('window', config.triggers.depletion.window),
                    critical_rate=depletion_data.get('critical_rate',
                                                     config.triggers.depletion.critical_rate),
                    grace_period=depletion_data.get('grace_period',
                                                    config.triggers.depletion.grace_period),
                ),
                extended_time=ExtendedTimeConfig(
                    enabled=extended_data.get('enabled', config.triggers.extended_time.enabled),
                    threshold=extended_data.get('threshold', config.triggers.extended_time.threshold),
                ),
            )

        # Behavior Configuration
        if 'behavior' in data:
            behavior_data = data['behavior']
            config.behavior = BehaviorConfig(
                dry_run=behavior_data.get('dry_run', config.behavior.dry_run),
            )

        # Logging Configuration
        if 'logging' in data:
            logging_data = data['logging']
            config.logging = LoggingConfig(
                file=logging_data.get('file', config.logging.file),
                state_file=logging_data.get('state_file', config.logging.state_file),
                battery_history_file=logging_data.get('battery_history_file',
                                                      config.logging.battery_history_file),
                shutdown_flag_file=logging_data.get('shutdown_flag_file',
                                                    config.logging.shutdown_flag_file),
            )

        # Notifications Configuration
        # Support both new 'notifications' format and legacy 'discord' format
        notif_urls = []
        notif_title = None
        avatar_url = None
        notif_timeout = 10
        notif_retry_interval = 5

        if 'notifications' in data:
            notif_data = data['notifications']

            # Get configuration options
            notif_title = notif_data.get('title')
            avatar_url = notif_data.get('avatar_url')
            notif_timeout = notif_data.get('timeout', 10)
            notif_retry_interval = notif_data.get('retry_interval', 5)

            # New Apprise-style configuration
            if 'urls' in notif_data:
                for url in notif_data.get('urls', []):
                    notif_urls.append(cls._append_avatar_to_url(url, avatar_url))

            # Legacy Discord configuration within notifications
            if 'discord' in notif_data:
                discord_data = notif_data['discord']
                webhook_url = discord_data.get('webhook_url', '')
                if webhook_url:
                    apprise_url = cls._convert_discord_webhook_to_apprise(webhook_url)
                    apprise_url = cls._append_avatar_to_url(apprise_url, avatar_url)
                    if apprise_url not in notif_urls:
                        notif_urls.insert(0, apprise_url)
                notif_timeout = discord_data.get('timeout', notif_timeout)

        # Top-level legacy Discord configuration (backwards compatibility)
        if 'discord' in data and 'notifications' not in data:
            discord_data = data['discord']
            webhook_url = discord_data.get('webhook_url', '')
            if webhook_url:
                apprise_url = cls._convert_discord_webhook_to_apprise(webhook_url)
                apprise_url = cls._append_avatar_to_url(apprise_url, avatar_url)
                if apprise_url not in notif_urls:
                    notif_urls.insert(0, apprise_url)
                notif_timeout = discord_data.get('timeout', notif_timeout)

        config.notifications = NotificationsConfig(
            enabled=len(notif_urls) > 0,
            urls=notif_urls,
            title=notif_title,
            avatar_url=avatar_url,
            timeout=notif_timeout,
            retry_interval=notif_retry_interval,
        )

        # Virtual Machines Configuration
        if 'virtual_machines' in data:
            vm_data = data['virtual_machines']
            config.virtual_machines = VMConfig(
                enabled=vm_data.get('enabled', False),
                max_wait=vm_data.get('max_wait', 30),
            )

        # Containers Configuration (supports both 'containers' and legacy 'docker')
        containers_data = data.get('containers', data.get('docker', {}))
        if containers_data:
            # Parse compose_files - normalize both string and dict formats
            compose_files_raw = containers_data.get('compose_files') or []
            compose_files = []
            for cf in compose_files_raw:
                if isinstance(cf, str):
                    compose_files.append(ComposeFileConfig(path=cf))
                elif isinstance(cf, dict):
                    compose_files.append(ComposeFileConfig(
                        path=cf.get('path', ''),
                        stop_timeout=cf.get('stop_timeout'),
                    ))

            # Handle legacy 'docker' section format
            if 'docker' in data and 'containers' not in data:
                # Legacy format: docker.enabled, docker.stop_timeout
                config.containers = ContainersConfig(
                    enabled=containers_data.get('enabled', False),
                    runtime="docker",  # Legacy config assumes docker
                    stop_timeout=containers_data.get('stop_timeout', 60),
                    compose_files=compose_files,
                    shutdown_all_remaining_containers=containers_data.get(
                        'shutdown_all_remaining_containers', True),
                    include_user_containers=False,
                )
            else:
                # New format: containers section
                config.containers = ContainersConfig(
                    enabled=containers_data.get('enabled', False),
                    runtime=containers_data.get('runtime', 'auto'),
                    stop_timeout=containers_data.get('stop_timeout', 60),
                    compose_files=compose_files,
                    shutdown_all_remaining_containers=containers_data.get(
                        'shutdown_all_remaining_containers', True),
                    include_user_containers=containers_data.get('include_user_containers', False),
                )

        # Filesystems Configuration
        if 'filesystems' in data:
            fs_data = data['filesystems']
            unmount_data = fs_data.get('unmount', {})
            mounts_raw = unmount_data.get('mounts', [])

            # Normalize mounts to list of dicts
            mounts = []
            for mount in mounts_raw:
                if isinstance(mount, str):
                    mounts.append({'path': mount, 'options': ''})
                elif isinstance(mount, dict):
                    mounts.append({
                        'path': mount.get('path', ''),
                        'options': mount.get('options', ''),
                    })

            config.filesystems = FilesystemsConfig(
                sync_enabled=fs_data.get('sync_enabled', True),
                unmount=UnmountConfig(
                    enabled=unmount_data.get('enabled', False),
                    timeout=unmount_data.get('timeout', 15),
                    mounts=mounts,
                ),
            )

        # Remote Servers Configuration
        if 'remote_servers' in data:
            servers = []
            for server_data in data['remote_servers']:
                # Parse pre_shutdown_commands
                pre_cmds_raw = server_data.get('pre_shutdown_commands') or []
                pre_cmds = []
                for cmd_data in pre_cmds_raw:
                    if isinstance(cmd_data, dict):
                        pre_cmds.append(RemoteCommandConfig(
                            action=cmd_data.get('action'),
                            command=cmd_data.get('command'),
                            timeout=cmd_data.get('timeout'),
                            path=cmd_data.get('path'),
                        ))

                servers.append(RemoteServerConfig(
                    name=server_data.get('name', ''),
                    enabled=server_data.get('enabled', False),
                    host=server_data.get('host', ''),
                    user=server_data.get('user', ''),
                    connect_timeout=server_data.get('connect_timeout', 10),
                    command_timeout=server_data.get('command_timeout', 30),
                    shutdown_command=server_data.get('shutdown_command', 'sudo shutdown -h now'),
                    ssh_options=server_data.get('ssh_options', []),
                    pre_shutdown_commands=pre_cmds,
                    parallel=server_data.get('parallel', True),
                ))
            config.remote_servers = servers

        # Local Shutdown Configuration
        if 'local_shutdown' in data:
            local_data = data['local_shutdown']
            config.local_shutdown = LocalShutdownConfig(
                enabled=local_data.get('enabled', True),
                command=local_data.get('command', 'shutdown -h now'),
                message=local_data.get('message', 'UPS battery critical - emergency shutdown'),
            )

        return config

    @classmethod
    def validate_config(cls, config: Config, raw_data: Optional[Dict[str, Any]] = None) -> List[str]:
        """Validate configuration and return list of warnings/info messages."""
        # Import here to avoid circular imports
        from eneru.notifications import APPRISE_AVAILABLE

        messages = []

        # Check Apprise availability
        if config.notifications.enabled and not APPRISE_AVAILABLE:
            messages.append(
                "WARNING: Notifications enabled but apprise package not installed. "
                "Notifications will be disabled. Install with: pip install apprise"
            )

        # Check for legacy Discord configuration (webhook_url in discord section)
        has_legacy_discord = False
        if raw_data:
            # Check for legacy discord.webhook_url in notifications section
            if 'notifications' in raw_data:
                notif_data = raw_data['notifications']
                if 'discord' in notif_data and notif_data['discord'].get('webhook_url'):
                    has_legacy_discord = True
            # Check for top-level legacy discord section
            if 'discord' in raw_data and 'notifications' not in raw_data:
                if raw_data['discord'].get('webhook_url'):
                    has_legacy_discord = True

        if has_legacy_discord:
            messages.append(
                "INFO: Legacy Discord webhook_url detected. Using Apprise for notifications. "
                "Consider migrating to the 'notifications.urls' format."
            )

        return messages
