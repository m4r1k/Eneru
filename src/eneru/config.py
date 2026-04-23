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
    # Voltage warning band as a fraction of input.voltage.nominal.
    # `tight` = ±5%, `normal` = ±10% (EN 50160), `loose` = ±15%.
    # Per-UPS-group so a clean PDU and a generator-fed leg in the same
    # daemon can use different thresholds. The voltage mixin maps this
    # to a percentage at startup; misconfiguration is rejected at load.
    voltage_sensitivity: str = "normal"
    # True iff `voltage_sensitivity` was explicitly present in the YAML
    # (vs. dataclass default). Gates the v5.1.1→v5.1.2 migration warning
    # so users who've already chosen a preset don't get a recurring nag.
    voltage_sensitivity_explicit: bool = False


VOLTAGE_SENSITIVITY_PRESETS: Dict[str, float] = {
    "tight": 0.05,
    "normal": 0.10,
    "loose": 0.15,
}


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
    # Per-event-type notification suppression. Logs always record these
    # events; only the notification dispatch is muted. See
    # SAFETY_CRITICAL_EVENTS in this module for the blocklist of names
    # that cannot be silenced.
    suppress: List[str] = field(default_factory=list)
    # Voltage-state notification debounce (seconds). A NORMAL→HIGH/LOW
    # transition is logged immediately; the notification is delayed for
    # this many seconds and only fires if the condition persists.
    # Default 30 s mutes 1-2 second NUT-driver flaps without weakening
    # the response to a real sustained event. 0 = immediate (legacy).
    voltage_hysteresis_seconds: int = 30


# Power events whose notifications cannot be suppressed via
# `notifications.suppress`. Allowing a user to silence these would
# defeat the safety contract: an unannounced sustained over-voltage,
# brownout, overload, bypass-active, or shutdown can damage hardware
# or data. ``voltage_hysteresis_seconds`` exists for tuning noise on
# transient flaps without losing the alert when it matters.
SAFETY_CRITICAL_EVENTS: frozenset = frozenset({
    "OVER_VOLTAGE_DETECTED",
    "BROWNOUT_DETECTED",
    "OVERLOAD_ACTIVE",
    "BYPASS_MODE_ACTIVE",
    "ON_BATTERY",
    "CONNECTION_LOST",
    # Shutdown-family events: any event name beginning with "SHUTDOWN"
    # is blocked dynamically in validation, not enumerated here.
})

# Event names that ARE allowed in notifications.suppress (anything not
# in this set is rejected to catch typos). Order is informational only.
SUPPRESSIBLE_EVENTS: frozenset = frozenset({
    "POWER_RESTORED",
    "VOLTAGE_NORMALIZED",
    "AVR_BOOST_ACTIVE",
    "AVR_TRIM_ACTIVE",
    "AVR_INACTIVE",
    "BYPASS_MODE_INACTIVE",
    "OVERLOAD_RESOLVED",
    "CONNECTION_RESTORED",
    "VOLTAGE_AUTODETECT_MISMATCH",
    "VOLTAGE_FLAP_SUPPRESSED",
})


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
    # Legacy ordering flag: None = unset (default behaves like True); True = run with the
    # parallel batch; False = run sequentially before the parallel batch. Mutually exclusive
    # with shutdown_order — setting both is a hard validation error.
    parallel: Optional[bool] = None
    shutdown_order: Optional[int] = None  # None = derive from parallel flag; >= 1 for explicit phase
    # Seconds added on top of (pre_shutdown_commands + command_timeout + connect_timeout)
    # when waiting for this server's parallel-shutdown thread to finish. Covers SSH session
    # setup, OS scheduling jitter, and the brief window between the remote shutdown command
    # starting and SSH closing the channel. Tune higher for servers with battery-backed RAID
    # or large flush windows; tune lower for fast-shutdown VMs. Zero opts out entirely.
    shutdown_safety_margin: int = 60


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
class StatsRetentionConfig:
    """Per-tier retention windows for the SQLite stats store."""
    raw_hours: int = 24
    agg_5min_days: int = 30
    agg_hourly_days: int = 1825


@dataclass
class StatsConfig:
    """Always-on per-UPS SQLite statistics store configuration."""
    db_directory: str = "/var/lib/eneru"
    retention: StatsRetentionConfig = field(default_factory=StatsRetentionConfig)


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
class RedundancyGroupConfig:
    """A redundancy group: shared resources protected by 2+ UPS sources.

    Mirrors :class:`UPSGroupConfig` so a group can own the same resource
    surface (remote servers, VMs, containers, filesystems). Resources in a
    redundancy group are shut down by the group evaluator only when fewer
    than ``min_healthy`` member UPSes still report a healthy snapshot.
    """
    name: str = ""
    ups_sources: List[str] = field(default_factory=list)
    # Quorum threshold: shutdown fires when healthy_count < min_healthy.
    # Default ``1`` matches a 2-UPS dual-PSU setup ("either UPS keeps us up").
    min_healthy: int = 1
    # How a DEGRADED member counts toward healthy_count.
    #   "healthy"  -- counted as healthy (default; tolerant of voltage warnings)
    #   "critical" -- counted as critical (strict; treats degraded as failed)
    degraded_counts_as: str = "healthy"
    # How an UNKNOWN member (stale snapshot, dropped NUT connection) counts.
    #   "critical" -- treated as failed (default; fail-safe)
    #   "degraded" -- counted via ``degraded_counts_as``
    #   "healthy"  -- counted as healthy (risky -- assumes best on missing data)
    unknown_counts_as: str = "critical"
    is_local: bool = False
    triggers: TriggersConfig = field(default_factory=TriggersConfig)
    remote_servers: List[RemoteServerConfig] = field(default_factory=list)
    virtual_machines: VMConfig = field(default_factory=VMConfig)
    containers: ContainersConfig = field(default_factory=ContainersConfig)
    filesystems: FilesystemsConfig = field(default_factory=FilesystemsConfig)


@dataclass
class Config:
    """Main configuration container."""
    ups_groups: List[UPSGroupConfig] = field(default_factory=list)
    redundancy_groups: List[RedundancyGroupConfig] = field(default_factory=list)
    behavior: BehaviorConfig = field(default_factory=BehaviorConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    notifications: NotificationsConfig = field(default_factory=NotificationsConfig)
    local_shutdown: LocalShutdownConfig = field(default_factory=LocalShutdownConfig)
    statistics: StatsConfig = field(default_factory=StatsConfig)

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
        # Distinguish "key absent" (use inherited default, leave explicit
        # flag alone) from "key present" (mark explicit so the migration
        # warning suppresses on the next daemon start). The explicit flag
        # propagates from defaults so a per-UPS block inherits a global
        # explicit choice unless it overrides.
        sensitivity_explicit = (
            'voltage_sensitivity' in triggers_data
            or defaults.voltage_sensitivity_explicit
        )
        sensitivity = triggers_data.get('voltage_sensitivity',
                                        defaults.voltage_sensitivity)
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
            voltage_sensitivity=sensitivity,
            voltage_sensitivity_explicit=sensitivity_explicit,
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
                parallel=server_data.get('parallel'),
                shutdown_order=server_data.get('shutdown_order'),
                shutdown_safety_margin=server_data.get('shutdown_safety_margin', 60),
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
        # Defaults match NotificationsConfig dataclass.
        notif_suppress: List[str] = []
        notif_voltage_hysteresis = 30

        if 'notifications' in data:
            notif_data = data['notifications']
            notif_title = notif_data.get('title')
            avatar_url = notif_data.get('avatar_url')
            notif_timeout = notif_data.get('timeout', 10)
            notif_retry_interval = notif_data.get('retry_interval', 5)
            notif_suppress = notif_data.get('suppress', notif_suppress)
            notif_voltage_hysteresis = notif_data.get(
                'voltage_hysteresis_seconds', notif_voltage_hysteresis,
            )

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
            suppress=notif_suppress,
            voltage_hysteresis_seconds=notif_voltage_hysteresis,
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

        # Statistics (always-on per-UPS SQLite store)
        if 'statistics' in data:
            stats_data = data.get('statistics') or {}
            retention_data = stats_data.get('retention') or {}
            defaults = StatsRetentionConfig()
            config.statistics = StatsConfig(
                db_directory=stats_data.get('db_directory', config.statistics.db_directory),
                retention=StatsRetentionConfig(
                    raw_hours=retention_data.get('raw_hours', defaults.raw_hours),
                    agg_5min_days=retention_data.get('agg_5min_days', defaults.agg_5min_days),
                    agg_hourly_days=retention_data.get('agg_hourly_days', defaults.agg_hourly_days),
                ),
            )

        # Detect legacy vs multi-UPS format
        ups_raw = data.get('ups', {})

        if isinstance(ups_raw, list):
            # --- Multi-UPS mode ---
            config.ups_groups = cls._parse_multi_ups(ups_raw, global_triggers)
        else:
            # --- Legacy single-UPS mode ---
            config.ups_groups = [cls._parse_legacy_ups(data, ups_raw, global_triggers)]

        # --- Redundancy groups (Phase 2) ---
        if 'redundancy_groups' in data:
            config.redundancy_groups = cls._parse_redundancy_groups(
                data['redundancy_groups'], global_triggers
            )

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
    def _parse_redundancy_groups(cls, groups_data: list,
                                  global_triggers: TriggersConfig) -> List[RedundancyGroupConfig]:
        """Parse the ``redundancy_groups`` YAML section into config objects."""
        groups: List[RedundancyGroupConfig] = []
        for entry in groups_data or []:
            if not isinstance(entry, dict):
                continue

            ups_sources_raw = entry.get('ups_sources', []) or []
            ups_sources = [str(s) for s in ups_sources_raw]

            # Per-group triggers inherit from global, overriding only fields
            # the user actually re-specifies.
            if 'triggers' in entry:
                triggers = cls._parse_triggers_config(entry['triggers'], global_triggers)
            else:
                triggers = global_triggers

            remote_servers: List[RemoteServerConfig] = []
            if 'remote_servers' in entry:
                remote_servers = cls._parse_remote_servers(entry['remote_servers'])

            vm_config = VMConfig()
            if 'virtual_machines' in entry:
                vm_data = entry['virtual_machines']
                vm_config = VMConfig(
                    enabled=vm_data.get('enabled', False),
                    max_wait=vm_data.get('max_wait', 30),
                )

            containers_config = ContainersConfig()
            if 'containers' in entry:
                containers_config = cls._parse_containers_config(entry['containers'])

            fs_config = FilesystemsConfig()
            if 'filesystems' in entry:
                fs_config = cls._parse_filesystems_config(entry['filesystems'])

            groups.append(RedundancyGroupConfig(
                name=str(entry.get('name', '')),
                ups_sources=ups_sources,
                min_healthy=entry.get('min_healthy', 1),
                degraded_counts_as=str(entry.get('degraded_counts_as', 'healthy')),
                unknown_counts_as=str(entry.get('unknown_counts_as', 'critical')),
                is_local=bool(entry.get('is_local', False)),
                triggers=triggers,
                remote_servers=remote_servers,
                virtual_machines=vm_config,
                containers=containers_config,
                filesystems=fs_config,
            ))

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

        # notifications.suppress: every entry must be a known suppressible
        # event name. Safety-critical events (over-voltage, brownout,
        # overload, bypass-active, on-battery, connection-lost, anything
        # starting with SHUTDOWN) are rejected -- silencing them would
        # hide hardware-damaging conditions.
        if config.notifications.suppress:
            suppress = config.notifications.suppress
            if not isinstance(suppress, list):
                messages.append(
                    "ERROR: notifications.suppress must be a list of "
                    "event-type strings."
                )
            else:
                blocked = []
                unknown = []
                for ev in suppress:
                    name = str(ev).strip().upper()
                    if name in SAFETY_CRITICAL_EVENTS or name.startswith("SHUTDOWN"):
                        blocked.append(name)
                    elif name not in SUPPRESSIBLE_EVENTS:
                        unknown.append(name)
                if blocked:
                    messages.append(
                        "ERROR: notifications.suppress cannot include "
                        f"safety-critical events: {sorted(set(blocked))}. "
                        "These exist to alert you to potential hardware "
                        "damage and cannot be muted. Use "
                        "notifications.voltage_hysteresis_seconds to "
                        "debounce transient voltage flaps instead."
                    )
                if unknown:
                    messages.append(
                        "ERROR: notifications.suppress contains unknown "
                        f"event names: {sorted(set(unknown))}. Valid "
                        f"options: {sorted(SUPPRESSIBLE_EVENTS)}"
                    )

        # voltage_sensitivity is a strict enum -- typos must error rather
        # than silently fall back to "normal", because "loose" vs "tight"
        # is a meaningful operator decision and we don't want a fat-fingered
        # value to mask it. The validator walks every UPS group (including
        # the single legacy entry, which `Config.triggers` aliases via
        # property -- no separate check needed for the legacy alias) and
        # every redundancy group's triggers block too, so a typo there
        # surfaces at config load instead of silently parsing as a string.
        for group in config.ups_groups:
            value = group.triggers.voltage_sensitivity
            if value not in VOLTAGE_SENSITIVITY_PRESETS:
                messages.append(
                    f"ERROR: invalid ups[{group.ups.label!r}]."
                    f"triggers.voltage_sensitivity {value!r}; "
                    f"expected one of {sorted(VOLTAGE_SENSITIVITY_PRESETS)}."
                )
        for rg in config.redundancy_groups:
            value = rg.triggers.voltage_sensitivity
            if value not in VOLTAGE_SENSITIVITY_PRESETS:
                label = rg.name or "(unnamed)"
                messages.append(
                    f"ERROR: invalid redundancy_groups[{label!r}]."
                    f"triggers.voltage_sensitivity {value!r}; "
                    f"expected one of {sorted(VOLTAGE_SENSITIVITY_PRESETS)}."
                )

        # voltage_hysteresis_seconds must be non-negative; absurdly long
        # values get a warning (delayed alerts may exceed shutdown timing).
        hys = config.notifications.voltage_hysteresis_seconds
        if not isinstance(hys, int) or hys < 0:
            messages.append(
                "ERROR: notifications.voltage_hysteresis_seconds must be a "
                f"non-negative integer (got {hys!r})."
            )
        elif hys > 600:
            messages.append(
                "WARNING: notifications.voltage_hysteresis_seconds > 600s "
                f"(got {hys}). A flap longer than ~10 minutes is no longer "
                "a flap; consider lowering this value."
            )

        # Multi-UPS validation
        if config.multi_ups:
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

        # is_local uniqueness (combined across UPS groups + redundancy groups)
        local_ups = [g.ups.label for g in config.ups_groups if g.is_local]
        local_redundancy = [g.name or "(unnamed)"
                            for g in config.redundancy_groups if g.is_local]
        all_local = local_ups + [f"redundancy:{n}" for n in local_redundancy]
        if len(all_local) > 1:
            messages.append(
                f"ERROR: Multiple groups marked as is_local: {', '.join(all_local)}. "
                "At most one group (UPS or redundancy) can power the Eneru host."
            )

        # --- Redundancy-group validation (Phase 2) ---
        if config.redundancy_groups:
            seen_names: Dict[str, int] = {}
            ups_known = {g.ups.name for g in config.ups_groups}
            ups_labels = {g.ups.name: g.ups.label for g in config.ups_groups}

            # Index remote-server identities (host, user) per UPS group so we
            # can detect cross-tier collisions cleanly.
            ups_server_owners: Dict[tuple, str] = {}
            for group in config.ups_groups:
                for server in group.remote_servers:
                    key = (server.host.strip().lower(), server.user.strip().lower())
                    if key[0] and key[1]:
                        ups_server_owners.setdefault(key, group.ups.label)

            redundancy_server_owners: Dict[tuple, str] = {}

            for rg in config.redundancy_groups:
                label = rg.name or "(unnamed)"

                # Name presence + uniqueness
                if not rg.name:
                    messages.append(
                        "ERROR: Redundancy group missing 'name'. Every group needs a "
                        "unique name for logs, notifications, and shutdown flag files."
                    )
                seen_names[rg.name] = seen_names.get(rg.name, 0) + 1

                # ups_sources presence
                if not rg.ups_sources:
                    messages.append(
                        f"ERROR: Redundancy group '{label}': 'ups_sources' is empty. "
                        "A redundancy group needs at least 2 UPS sources to be useful."
                    )

                # ups_sources reference known UPSes
                unknown_refs = [u for u in rg.ups_sources if u not in ups_known]
                if unknown_refs:
                    messages.append(
                        f"ERROR: Redundancy group '{label}' references unknown UPS "
                        f"name(s): {', '.join(unknown_refs)}. Known UPSes: "
                        f"{', '.join(sorted(ups_known)) or '(none)'}."
                    )

                # ups_sources uniqueness within the group
                if len(set(rg.ups_sources)) != len(rg.ups_sources):
                    dups = sorted({u for u in rg.ups_sources
                                   if rg.ups_sources.count(u) > 1})
                    messages.append(
                        f"ERROR: Redundancy group '{label}' lists duplicate UPS source(s): "
                        f"{', '.join(dups)}."
                    )

                # min_healthy bounds
                if not isinstance(rg.min_healthy, int) or isinstance(rg.min_healthy, bool):
                    messages.append(
                        f"ERROR: Redundancy group '{label}': min_healthy must be an "
                        f"integer, got {rg.min_healthy!r}."
                    )
                elif rg.min_healthy < 1:
                    messages.append(
                        f"ERROR: Redundancy group '{label}': min_healthy must be >= 1, "
                        f"got {rg.min_healthy}. A min_healthy of 0 would mean the group "
                        "never triggers a shutdown -- remove the group instead."
                    )
                elif rg.ups_sources and rg.min_healthy > len(rg.ups_sources):
                    messages.append(
                        f"ERROR: Redundancy group '{label}': min_healthy "
                        f"({rg.min_healthy}) exceeds the number of UPS sources "
                        f"({len(rg.ups_sources)}). The group can never be healthy."
                    )
                elif rg.ups_sources and rg.min_healthy == len(rg.ups_sources):
                    messages.append(
                        f"WARNING: Redundancy group '{label}': min_healthy equals "
                        f"the number of UPS sources ({len(rg.ups_sources)}). "
                        "There is no redundancy -- any single UPS failure triggers shutdown."
                    )

                # degraded_counts_as / unknown_counts_as enums
                if rg.degraded_counts_as not in ("healthy", "critical"):
                    messages.append(
                        f"ERROR: Redundancy group '{label}': degraded_counts_as must be "
                        f"'healthy' or 'critical', got '{rg.degraded_counts_as}'."
                    )
                if rg.unknown_counts_as not in ("healthy", "degraded", "critical"):
                    messages.append(
                        f"ERROR: Redundancy group '{label}': unknown_counts_as must be "
                        f"'healthy', 'degraded', or 'critical', got '{rg.unknown_counts_as}'."
                    )

                # Local-resource ownership
                if not rg.is_local:
                    if rg.virtual_machines.enabled:
                        messages.append(
                            f"ERROR: Redundancy group '{label}' has virtual_machines "
                            "enabled but is not marked is_local. Only an is_local group "
                            "(UPS or redundancy) can manage VMs."
                        )
                    if rg.containers.enabled:
                        messages.append(
                            f"ERROR: Redundancy group '{label}' has containers enabled "
                            "but is not marked is_local. Only an is_local group "
                            "(UPS or redundancy) can manage containers."
                        )
                    if rg.filesystems.unmount.enabled:
                        messages.append(
                            f"ERROR: Redundancy group '{label}' has filesystem unmount "
                            "enabled but is not marked is_local. Only an is_local group "
                            "(UPS or redundancy) can manage filesystems."
                        )

                # Remote-server cross-tier conflict detection
                for server in rg.remote_servers:
                    key = (server.host.strip().lower(), server.user.strip().lower())
                    if not key[0] or not key[1]:
                        continue
                    if key in ups_server_owners:
                        owner = ups_server_owners[key]
                        messages.append(
                            f"ERROR: Remote server '{server.name or server.host}' "
                            f"({server.user}@{server.host}) is owned by both UPS group "
                            f"'{owner}' and redundancy group '{label}'. A remote server "
                            "must belong to exactly one tier."
                        )
                    if key in redundancy_server_owners:
                        owner = redundancy_server_owners[key]
                        messages.append(
                            f"ERROR: Remote server '{server.name or server.host}' "
                            f"({server.user}@{server.host}) appears in two redundancy "
                            f"groups: '{owner}' and '{label}'."
                        )
                    redundancy_server_owners.setdefault(key, label)

            # Duplicate group names
            duplicates = sorted(n for n, c in seen_names.items() if c > 1 and n)
            if duplicates:
                messages.append(
                    f"ERROR: Duplicate redundancy group name(s): {', '.join(duplicates)}."
                )

        # Validate shutdown_order, shutdown_safety_margin, and the
        # mutual-exclusion of shutdown_order vs parallel.
        for group in config.ups_groups:
            for server in group.remote_servers:
                display = server.name or server.host

                so = server.shutdown_order
                so_valid = False
                if so is not None:
                    if not isinstance(so, int) or isinstance(so, bool):
                        messages.append(
                            f"ERROR: Remote server '{display}': shutdown_order "
                            f"must be a positive integer, got {so!r}"
                        )
                    elif so < 1:
                        messages.append(
                            f"ERROR: Remote server '{display}': shutdown_order "
                            f"must be >= 1, got {so}"
                        )
                    else:
                        so_valid = True

                # Mutual exclusion: shutdown_order AND parallel both explicitly set
                # (either parallel: true or parallel: false) is a hard error.
                if so_valid and server.parallel is not None:
                    messages.append(
                        f"ERROR: Remote server '{display}': cannot set both "
                        f"'shutdown_order' ({so}) and 'parallel' ({str(server.parallel).lower()}). "
                        f"Pick one model:\n"
                        f"  - shutdown_order: <int>>=1   (recommended; supports multi-phase ordering)\n"
                        f"  - parallel: true|false       (legacy two-phase behavior)\n"
                        f"Remove the unused field from this server's config."
                    )

                margin = server.shutdown_safety_margin
                if not isinstance(margin, int) or isinstance(margin, bool):
                    messages.append(
                        f"ERROR: Remote server '{display}': shutdown_safety_margin "
                        f"must be a non-negative integer, got {margin!r}"
                    )
                elif margin < 0:
                    messages.append(
                        f"ERROR: Remote server '{display}': shutdown_safety_margin "
                        f"must be >= 0, got {margin}"
                    )

        # Validate trigger_on value
        if config.local_shutdown.trigger_on not in ("any", "none"):
            messages.append(
                f"ERROR: local_shutdown.trigger_on must be 'any' or 'none', "
                f"got '{config.local_shutdown.trigger_on}'"
            )

        return messages
