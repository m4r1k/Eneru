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
class ConnectionLossGracePeriodConfig:
    """Connection loss grace period configuration."""
    enabled: bool = True
    duration: int = 60
    flap_threshold: int = 5


@dataclass
class UPSConfig:
    """UPS connection configuration."""
    name: str = "UPS@localhost"
    display_name: Optional[str] = None  # Human-readable name for logs/notifications
    check_interval: int = 1
    max_stale_data_tolerance: int = 3
    connection_loss_grace_period: ConnectionLossGracePeriodConfig = field(
        default_factory=ConnectionLossGracePeriodConfig
    )

    @property
    def label(self) -> str:
        """Return display_name if set, otherwise name."""
        return self.display_name or self.name


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
    drain_on_local_shutdown: bool = False  # Drain all groups before local shutdown
    trigger_on: str = "any"  # "any" or "none" — when to trigger local shutdown in multi-UPS


@dataclass
class BehaviorConfig:
    """Behavior configuration."""
    dry_run: bool = False


@dataclass
class UPSGroupConfig:
    """A UPS and the resources it protects."""
    ups: UPSConfig = field(default_factory=UPSConfig)
    triggers: TriggersConfig = field(default_factory=TriggersConfig)
    remote_servers: List[RemoteServerConfig] = field(default_factory=list)
    virtual_machines: VMConfig = field(default_factory=VMConfig)
    containers: ContainersConfig = field(default_factory=ContainersConfig)
    filesystems: FilesystemsConfig = field(default_factory=FilesystemsConfig)
    is_local: bool = False  # Does this UPS power the Eneru host?

    @property
    def is_multi_ups(self) -> bool:
        """True if this group was created from a multi-UPS config."""
        return self._multi_ups

    def __post_init__(self):
        self._multi_ups = False


@dataclass
class Config:
    """Main configuration container."""
    ups_groups: List[UPSGroupConfig] = field(default_factory=list)
    behavior: BehaviorConfig = field(default_factory=BehaviorConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    notifications: NotificationsConfig = field(default_factory=NotificationsConfig)
    local_shutdown: LocalShutdownConfig = field(default_factory=LocalShutdownConfig)

    # Notification types mapped to colors/severity
    NOTIFY_FAILURE: str = "failure"
    NOTIFY_WARNING: str = "warning"
    NOTIFY_SUCCESS: str = "success"
    NOTIFY_INFO: str = "info"

    @property
    def multi_ups(self) -> bool:
        """True if multiple UPS groups are configured."""
        return len(self.ups_groups) > 1

    # --- Backward-compatible accessors for single-UPS code paths ---
    @property
    def ups(self) -> UPSConfig:
        """Legacy accessor: returns the first (or only) UPS config."""
        if self.ups_groups:
            return self.ups_groups[0].ups
        return UPSConfig()

    @property
    def triggers(self) -> TriggersConfig:
        """Legacy accessor: returns triggers from the first group."""
        if self.ups_groups:
            return self.ups_groups[0].triggers
        return TriggersConfig()

    @property
    def remote_servers(self) -> List[RemoteServerConfig]:
        """Legacy accessor: returns remote servers from the first group."""
        if self.ups_groups:
            return self.ups_groups[0].remote_servers
        return []

    @property
    def virtual_machines(self) -> VMConfig:
        """Legacy accessor: returns VM config from the first group."""
        if self.ups_groups:
            return self.ups_groups[0].virtual_machines
        return VMConfig()

    @property
    def containers(self) -> ContainersConfig:
        """Legacy accessor: returns container config from the first group."""
        if self.ups_groups:
            return self.ups_groups[0].containers
        return ContainersConfig()

    @property
    def filesystems(self) -> FilesystemsConfig:
        """Legacy accessor: returns filesystem config from the first group."""
        if self.ups_groups:
            return self.ups_groups[0].filesystems
        return FilesystemsConfig()


# ==============================================================================
# CONFIGURATION LOADER
# ==============================================================================

class ConfigLoader:
    """Loads and validates configuration from YAML file."""

    DEFAULT_CONFIG_PATHS = [
        Path("/etc/ups-monitor/config.yaml"),
        Path("/etc/ups-monitor/config.yml"),
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
    def _parse_ups_config(cls, ups_data: Dict[str, Any]) -> UPSConfig:
        """Parse a single UPS connection configuration."""
        defaults = UPSConfig()
        grace_data = ups_data.get('connection_loss_grace_period', {})
        return UPSConfig(
            name=ups_data.get('name', defaults.name),
            display_name=ups_data.get('display_name'),
            check_interval=ups_data.get('check_interval', defaults.check_interval),
            max_stale_data_tolerance=ups_data.get('max_stale_data_tolerance',
                                                  defaults.max_stale_data_tolerance),
            connection_loss_grace_period=ConnectionLossGracePeriodConfig(
                enabled=grace_data.get('enabled',
                                       defaults.connection_loss_grace_period.enabled),
                duration=grace_data.get('duration',
                                        defaults.connection_loss_grace_period.duration),
                flap_threshold=grace_data.get('flap_threshold',
                                               defaults.connection_loss_grace_period.flap_threshold),
            ),
        )

    @classmethod
    def _parse_triggers_config(cls, triggers_data: Dict[str, Any],
                                defaults: Optional[TriggersConfig] = None) -> TriggersConfig:
        """Parse triggers configuration, optionally inheriting from defaults."""
        if defaults is None:
            defaults = TriggersConfig()
        depletion_data = triggers_data.get('depletion', {})
        extended_data = triggers_data.get('extended_time', {})
        return TriggersConfig(
            low_battery_threshold=triggers_data.get('low_battery_threshold',
                                                    defaults.low_battery_threshold),
            critical_runtime_threshold=triggers_data.get('critical_runtime_threshold',
                                                         defaults.critical_runtime_threshold),
            depletion=DepletionConfig(
                window=depletion_data.get('window', defaults.depletion.window),
                critical_rate=depletion_data.get('critical_rate',
                                                 defaults.depletion.critical_rate),
                grace_period=depletion_data.get('grace_period',
                                                defaults.depletion.grace_period),
            ),
            extended_time=ExtendedTimeConfig(
                enabled=extended_data.get('enabled', defaults.extended_time.enabled),
                threshold=extended_data.get('threshold', defaults.extended_time.threshold),
            ),
        )

    @classmethod
    def _parse_remote_servers(cls, servers_data: list) -> List[RemoteServerConfig]:
        """Parse a list of remote server configurations."""
        servers = []
        for server_data in servers_data:
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
        return servers

    @classmethod
    def _parse_containers_config(cls, containers_data: Dict[str, Any],
                                  is_legacy_docker: bool = False) -> ContainersConfig:
        """Parse container runtime configuration."""
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

        if is_legacy_docker:
            return ContainersConfig(
                enabled=containers_data.get('enabled', False),
                runtime="docker",
                stop_timeout=containers_data.get('stop_timeout', 60),
                compose_files=compose_files,
                shutdown_all_remaining_containers=containers_data.get(
                    'shutdown_all_remaining_containers', True),
                include_user_containers=False,
            )
        return ContainersConfig(
            enabled=containers_data.get('enabled', False),
            runtime=containers_data.get('runtime', 'auto'),
            stop_timeout=containers_data.get('stop_timeout', 60),
            compose_files=compose_files,
            shutdown_all_remaining_containers=containers_data.get(
                'shutdown_all_remaining_containers', True),
            include_user_containers=containers_data.get('include_user_containers', False),
        )

    @classmethod
    def _parse_filesystems_config(cls, fs_data: Dict[str, Any]) -> FilesystemsConfig:
        """Parse filesystem operations configuration."""
        unmount_data = fs_data.get('unmount', {})
        mounts_raw = unmount_data.get('mounts', [])
        mounts = []
        for mount in mounts_raw:
            if isinstance(mount, str):
                mounts.append({'path': mount, 'options': ''})
            elif isinstance(mount, dict):
                mounts.append({
                    'path': mount.get('path', ''),
                    'options': mount.get('options', ''),
                })
        return FilesystemsConfig(
            sync_enabled=fs_data.get('sync_enabled', True),
            unmount=UnmountConfig(
                enabled=unmount_data.get('enabled', False),
                timeout=unmount_data.get('timeout', 15),
                mounts=mounts,
            ),
        )

    @classmethod
    def _parse_notifications(cls, data: Dict[str, Any]) -> NotificationsConfig:
        """Parse notifications configuration, supporting legacy Discord format."""
        notif_urls = []
        notif_title = None
        avatar_url = None
        notif_timeout = 10
        notif_retry_interval = 5

        if 'notifications' in data:
            notif_data = data['notifications']
            notif_title = notif_data.get('title')
            avatar_url = notif_data.get('avatar_url')
            notif_timeout = notif_data.get('timeout', 10)
            notif_retry_interval = notif_data.get('retry_interval', 5)

            if 'urls' in notif_data:
                for url in notif_data.get('urls', []):
                    notif_urls.append(cls._append_avatar_to_url(url, avatar_url))

            if 'discord' in notif_data:
                discord_data = notif_data['discord']
                webhook_url = discord_data.get('webhook_url', '')
                if webhook_url:
                    apprise_url = cls._convert_discord_webhook_to_apprise(webhook_url)
                    apprise_url = cls._append_avatar_to_url(apprise_url, avatar_url)
                    if apprise_url not in notif_urls:
                        notif_urls.insert(0, apprise_url)
                notif_timeout = discord_data.get('timeout', notif_timeout)

        if 'discord' in data and 'notifications' not in data:
            discord_data = data['discord']
            webhook_url = discord_data.get('webhook_url', '')
            if webhook_url:
                apprise_url = cls._convert_discord_webhook_to_apprise(webhook_url)
                apprise_url = cls._append_avatar_to_url(apprise_url, avatar_url)
                if apprise_url not in notif_urls:
                    notif_urls.insert(0, apprise_url)
                notif_timeout = discord_data.get('timeout', notif_timeout)

        return NotificationsConfig(
            enabled=len(notif_urls) > 0,
            urls=notif_urls,
            title=notif_title,
            avatar_url=avatar_url,
            timeout=notif_timeout,
            retry_interval=notif_retry_interval,
        )

    @classmethod
    def _parse_config(cls, data: Dict[str, Any]) -> Config:
        """Parse configuration dictionary into Config object.

        Supports two formats:
        - Legacy: ups is a dict with 'name' key, resources at top level
        - Multi-UPS: ups is a list, resources nested under each UPS entry
        """
        config = Config()

        # Parse global settings (shared across all UPS groups)
        if 'behavior' in data:
            config.behavior = BehaviorConfig(
                dry_run=data['behavior'].get('dry_run', False),
            )

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

        config.notifications = cls._parse_notifications(data)

        if 'local_shutdown' in data:
            local_data = data['local_shutdown']
            config.local_shutdown = LocalShutdownConfig(
                enabled=local_data.get('enabled', True),
                command=local_data.get('command', 'shutdown -h now'),
                message=local_data.get('message', 'UPS battery critical - emergency shutdown'),
                drain_on_local_shutdown=local_data.get('drain_on_local_shutdown', False),
                trigger_on=local_data.get('trigger_on', 'any'),
            )

        # Parse global triggers (used as defaults for per-UPS triggers)
        global_triggers = TriggersConfig()
        if 'triggers' in data:
            global_triggers = cls._parse_triggers_config(data['triggers'])

        # Detect legacy vs multi-UPS format
        ups_raw = data.get('ups', {})

        if isinstance(ups_raw, list):
            # --- Multi-UPS mode ---
            config.ups_groups = cls._parse_multi_ups(ups_raw, global_triggers)
        else:
            # --- Legacy single-UPS mode ---
            config.ups_groups = [cls._parse_legacy_ups(data, ups_raw, global_triggers)]

        return config

    @classmethod
    def _parse_legacy_ups(cls, data: Dict[str, Any], ups_data: Dict[str, Any],
                           global_triggers: TriggersConfig) -> UPSGroupConfig:
        """Parse legacy single-UPS format into a UPSGroupConfig."""
        ups_config = cls._parse_ups_config(ups_data) if ups_data else UPSConfig()

        # Parse top-level resources
        remote_servers = []
        if 'remote_servers' in data:
            remote_servers = cls._parse_remote_servers(data['remote_servers'])

        vm_config = VMConfig()
        if 'virtual_machines' in data:
            vm_data = data['virtual_machines']
            vm_config = VMConfig(
                enabled=vm_data.get('enabled', False),
                max_wait=vm_data.get('max_wait', 30),
            )

        containers_config = ContainersConfig()
        containers_data = data.get('containers', data.get('docker', {}))
        if containers_data:
            is_legacy_docker = 'docker' in data and 'containers' not in data
            containers_config = cls._parse_containers_config(containers_data, is_legacy_docker)

        fs_config = FilesystemsConfig()
        if 'filesystems' in data:
            fs_config = cls._parse_filesystems_config(data['filesystems'])

        group = UPSGroupConfig(
            ups=ups_config,
            triggers=global_triggers,
            remote_servers=remote_servers,
            virtual_machines=vm_config,
            containers=containers_config,
            filesystems=fs_config,
            is_local=True,  # Legacy single-UPS is always local
        )
        return group

    @classmethod
    def _parse_multi_ups(cls, ups_list: list,
                          global_triggers: TriggersConfig) -> List[UPSGroupConfig]:
        """Parse multi-UPS list format into UPSGroupConfig list."""
        groups = []
        for entry in ups_list:
            ups_config = cls._parse_ups_config(entry)

            # Per-UPS triggers inherit from global, override if specified
            if 'triggers' in entry:
                triggers = cls._parse_triggers_config(entry['triggers'], global_triggers)
            else:
                triggers = global_triggers

            is_local = entry.get('is_local', False)

            # Remote servers (allowed for all groups)
            remote_servers = []
            if 'remote_servers' in entry:
                remote_servers = cls._parse_remote_servers(entry['remote_servers'])

            # Local resources (only allowed if is_local)
            vm_config = VMConfig()
            if 'virtual_machines' in entry:
                vm_config = VMConfig(
                    enabled=entry['virtual_machines'].get('enabled', False),
                    max_wait=entry['virtual_machines'].get('max_wait', 30),
                )

            containers_config = ContainersConfig()
            if 'containers' in entry:
                containers_config = cls._parse_containers_config(entry['containers'])

            fs_config = FilesystemsConfig()
            if 'filesystems' in entry:
                fs_config = cls._parse_filesystems_config(entry['filesystems'])

            group = UPSGroupConfig(
                ups=ups_config,
                triggers=triggers,
                remote_servers=remote_servers,
                virtual_machines=vm_config,
                containers=containers_config,
                filesystems=fs_config,
                is_local=is_local,
            )
            group._multi_ups = True
            groups.append(group)

        return groups

    @classmethod
    def validate_config(cls, config: Config, raw_data: Optional[Dict[str, Any]] = None) -> List[str]:
        """Validate configuration and return list of warnings/info messages."""
        from eneru.notifications import APPRISE_AVAILABLE

        messages = []

        # Check Apprise availability
        if config.notifications.enabled and not APPRISE_AVAILABLE:
            messages.append(
                "WARNING: Notifications enabled but apprise package not installed. "
                "Notifications will be disabled. Install with: pip install apprise"
            )

        # Check for legacy Discord configuration
        has_legacy_discord = False
        if raw_data:
            if 'notifications' in raw_data:
                notif_data = raw_data['notifications']
                if 'discord' in notif_data and notif_data['discord'].get('webhook_url'):
                    has_legacy_discord = True
            if 'discord' in raw_data and 'notifications' not in raw_data:
                if raw_data['discord'].get('webhook_url'):
                    has_legacy_discord = True

        if has_legacy_discord:
            messages.append(
                "INFO: Legacy Discord webhook_url detected. Using Apprise for notifications. "
                "Consider migrating to the 'notifications.urls' format."
            )

        # Multi-UPS validation
        if config.multi_ups:
            # Check that at most one group is marked is_local
            local_groups = [g for g in config.ups_groups if g.is_local]
            if len(local_groups) > 1:
                local_names = [g.ups.label for g in local_groups]
                messages.append(
                    f"ERROR: Multiple UPS groups marked as is_local: {', '.join(local_names)}. "
                    "At most one UPS group can power the Eneru host."
                )

            # Check ownership: non-local groups must not have local resources
            for group in config.ups_groups:
                if group.is_local:
                    continue
                label = group.ups.label
                if group.virtual_machines.enabled:
                    messages.append(
                        f"ERROR: UPS group '{label}' has virtual_machines enabled but is not "
                        "marked is_local. Only the local UPS group can manage VMs."
                    )
                if group.containers.enabled:
                    messages.append(
                        f"ERROR: UPS group '{label}' has containers enabled but is not "
                        "marked is_local. Only the local UPS group can manage containers."
                    )
                if group.filesystems.unmount.enabled:
                    messages.append(
                        f"ERROR: UPS group '{label}' has filesystem unmount enabled but is not "
                        "marked is_local. Only the local UPS group can manage filesystems."
                    )

            # Warn about top-level resources in multi-UPS mode
            if raw_data:
                for key in ('remote_servers', 'virtual_machines', 'containers', 'filesystems'):
                    if key in raw_data:
                        messages.append(
                            f"WARNING: Top-level '{key}' section ignored in multi-UPS mode. "
                            f"Move resources under the appropriate UPS entry."
                        )

        return messages
