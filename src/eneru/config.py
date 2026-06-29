"""Configuration classes and loader for Eneru."""

import shlex
from dataclasses import dataclass, field
from difflib import get_close_matches
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
    on_battery_stabilization_delay: int = 30
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
class SyslogConfig:
    """Optional syslog forwarding configuration."""
    enabled: bool = False
    address: str = "/dev/log"
    port: int = 514
    facility: str = "daemon"


@dataclass
class LoggingConfig:
    """Logging configuration."""
    file: Optional[str] = "/var/log/ups-monitor.log"
    state_file: str = "/var/run/ups-monitor.state"
    battery_history_file: str = "/var/run/ups-battery-history"
    shutdown_flag_file: str = "/var/run/ups-shutdown-scheduled"
    format: str = "text"
    syslog: SyslogConfig = field(default_factory=SyslogConfig)


@dataclass
class AuthConfig:
    """API authentication configuration (v6.0).

    Opt-in: when ``enabled`` is False the API behaves exactly as it did in
    v5.3 (read-only, no credentials) and every write surface is hard
    disabled. When True, all writes (UPS control, config reload) require a
    valid credential; reads stay open unless ``require_for_reads`` is set.
    """
    enabled: bool = False
    require_for_reads: bool = False
    session_ttl: int = 3600
    db_path: str = "/var/lib/eneru/auth.db"
    # True only when the operator wrote ``api.auth.enabled`` in the config. The
    # daemon uses this to decide whether it may *auto-enable* auth when the auth
    # DB already has users (create-a-user-then-just-log-in). An explicit value —
    # true or false — always wins. It participates in equality so the hot-reload
    # diff treats "unpinned" vs "explicitly pinned" as a real ``api.auth`` change
    # (reported restart-required); it stays out of ``repr`` to avoid noise.
    enabled_explicitly_set: bool = field(default=False, repr=False)


@dataclass
class APIConfig:
    """Embedded HTTP API configuration."""
    enabled: bool = False
    bind: str = "127.0.0.1"
    port: int = 9191
    auth: AuthConfig = field(default_factory=AuthConfig)


@dataclass
class PrometheusConfig:
    """Prometheus endpoint configuration."""
    enabled: bool = True


@dataclass
class RemoteHealthConfig:
    """Harmless SSH healthcheck configuration for remote servers."""
    enabled: bool = True
    startup_check: bool = True
    interval: int = 3600
    probe_command: str = "true"
    failure_threshold: int = 2
    notify_on_failure: bool = True
    notify_on_recovery: bool = True


@dataclass
class MQTTConfig:
    """Optional outbound MQTT publishing configuration."""
    enabled: bool = False
    broker: str = ""
    topic_prefix: str = "eneru"
    publish_interval: int = 10


@dataclass
class NutControlConfig:
    """UPS control via NUT upscmd/upsrw (v6.0).

    Off by default. A write surface: it can only be enabled when API auth is
    enabled (enforced in validation), so control is never reachable without a
    credential. Commands and writable variables are allowlisted; the variable
    allowlist defaults empty because upsrw can change risky settings.
    """
    enabled: bool = False
    username: str = ""
    password: str = ""
    allowed_commands: List[str] = field(default_factory=list)
    allowed_variables: List[str] = field(default_factory=list)
    timeout: int = 10


@dataclass
class BatteryReplacementConfig:
    """When to predict battery replacement (nested under battery_health)."""
    threshold_score: float = 50.0   # health score the battery is "due" at
    horizon_days: int = 90          # only warn if the crossing is within this
    min_history_days: int = 14      # don't trend on less history than this


@dataclass
class BatteryHealthConfig:
    """Battery-health scoring + replacement prediction (v6.1).

    Per-UPS overridable (different UPSes have different batteries): the
    install date, learned/declared nominal runtime, and expected life are
    UPS-specific. A per-UPS block overrides these for that UPS; unset fields
    inherit this global default.
    """
    enabled: bool = True
    update_interval: int = 3600                      # seconds between computations
    nominal_runtime_seconds: Optional[int] = None    # None => autodetect at 100%
    battery_install_date: Optional[str] = None       # "YYYY-MM-DD"; None => age term unavailable
    expected_life_years: float = 5.0
    replacement: BatteryReplacementConfig = field(
        default_factory=BatteryReplacementConfig)


@dataclass
class SelfTestConfig:
    """Scheduled UPS self-test (v6.1). Off by default.

    A write surface like nut_control: enabling it requires nut_control +
    api.auth and that the command is on the nut_control allowlist (enforced
    in validation), so a scheduled test is never a back door around the v6.0
    control allowlist. Per-UPS overridable.
    """
    enabled: bool = False
    schedule: str = "monthly"            # daily|weekly|monthly or "every <N>d/h/m"
    time: str = "03:00"                  # wall-clock for calendar schedules
    command: str = "test.battery.start"  # adapts to whatever upscmd -l exposes
    result_poll_after: int = 60          # seconds after issue before polling result


@dataclass
class ReportsConfig:
    """Periodic summary reports delivered via the notification channel (v6.1)."""
    enabled: bool = False
    daily: bool = False
    weekly: bool = False
    monthly: bool = False
    time: str = "08:00"                  # wall-clock send time
    weekly_day: str = "monday"
    monthly_day: int = 1
    include: List[str] = field(default_factory=lambda: [
        "events", "battery_health", "energy", "uptime"])
    format: str = "text"                 # text | csv


@dataclass
class EnergyConfig:
    """Energy (kWh) + optional cost tracking (v6.1).

    cost_per_kwh None/unset => cost tracking disabled entirely (no cost in
    status/metrics/UI), rather than a meaningless zero-currency graph.
    """
    enabled: bool = True
    cost_per_kwh: Optional[float] = None
    currency: str = "USD"                # ISO 4217 code
    cost_format: Optional[str] = None    # e.g. "{value} €"; overrides the currency table
    nominal_power: Optional[float] = None  # rated W/VA; estimates watts when the
    # UPS reports neither ups.realpower nor ups.power.nominal


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

    # ---- v5.2 persistent-queue knobs ----
    # Days to keep ``sent`` and ``cancelled`` rows around for forensic
    # inspection via sqlite3. ``pending`` rows are NEVER pruned by TTL.
    retention_days: int = 7
    # Per-message attempt cap. 0 (default, unlimited) means a stuck
    # message keeps retrying with exponential backoff until it succeeds
    # or hits ``max_age_days``. Apprise's success/fail signal is a bool
    # — we can't tell "bad URL" from "internet down" — so giving up on
    # attempts alone risks dropping legitimate messages during a long
    # outage. Set this only if you want a poison-message kill switch.
    max_attempts: int = 0
    # Pending notifications older than this become ``cancelled``
    # (reason: ``too_old``). 30 d covers a month-long absence; longer
    # than that the message is probably stale. Set 0 to disable.
    max_age_days: int = 30
    # Backlog cap. When pending exceeds this, the oldest are cancelled
    # with reason ``backlog_overflow``. 10000 is well above normal use
    # but bounds DB growth on runaway-event days.
    max_pending: int = 10000
    # Exponential backoff ceiling, in seconds. The per-message wait
    # doubles on each failure (starting at ``retry_interval``) up to
    # this cap. 5 min keeps reconnection quick once the endpoint
    # returns without hammering the network during a long outage.
    retry_backoff_max: int = 300


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
    # For the unmount_filesystems action on ordinary remote servers.
    # Loopback delegates ignore this field and derive mounts from the local
    # filesystems.unmount config so operators declare local mounts once.
    mounts: List[Dict[str, str]] = field(default_factory=list)


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
    use_sudo: bool = False
    ssh_key_path: Optional[str] = None
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
    # v5.5: container loopback delegation. When the host running Eneru lives inside an OCI
    # container and the config declares local-host ownership (is_local + vms/containers/
    # filesystems/local_shutdown), one remote_servers entry is flagged as the host loopback.
    # Eneru's privilege check accepts non-root in that case, and the shutdown sequence
    # delegates every local-host action to this entry over SSH. The host_identity_command
    # probe and expected_host_identity (auto-populated from /etc/machine-id when the
    # operator bind-mounts it) prove the SSH target is actually the host Eneru is supposed
    # to control before any destructive action is sent. See docs/install-comparison.md
    # and docs/containers-kubernetes.md.
    is_host_loopback: bool = False
    _is_host_loopback_explicit: bool = False
    host_identity_command: str = "cat /etc/machine-id"
    expected_host_identity: Optional[str] = None

    def __post_init__(self):
        # Default remote host-key checking to accept-new. OpenSSH's own default
        # (StrictHostKeyChecking=ask) fails closed under BatchMode when a host
        # key is unknown, so a remote with no ssh_options would never connect on
        # first contact (issue #73). accept-new learns and pins the key on the
        # first probe and still fails closed if the key later changes. The key
        # is recorded in the active OpenSSH known_hosts file. Bare-metal runs
        # use the running user's default ~/.ssh/known_hosts; the SSH command
        # builders add a container-only UserKnownHostsFile default so Docker
        # keeps using the documented /var/lib/eneru/ssh mount. Any operator-supplied
        # StrictHostKeyChecking directive is preserved verbatim, including the
        # loopback delegate's explicit "no" (127.0.0.1 is MITM-safe). The
        # default is prepended, not appended, so a trailing dangling flag in
        # ssh_options (e.g. a bare "-i") stays trailing and is still rejected by
        # build_ssh_probe_command instead of silently consuming this value.
        if not isinstance(self.ssh_options, list):
            self.ssh_options = []
        if not any(
            isinstance(opt, str) and "stricthostkeychecking" in opt.lower()
            for opt in self.ssh_options
        ):
            self.ssh_options = ["StrictHostKeyChecking=accept-new",
                                *self.ssh_options]


@dataclass
class LocalShutdownConfig:
    """Local shutdown configuration."""
    enabled: bool = True
    command: str = "shutdown -h now"
    message: str = "UPS battery critical - emergency shutdown"
    drain_on_local_shutdown: bool = False  # Drain all groups before local shutdown
    trigger_on: str = "any"  # "any" or "none" — when to trigger local shutdown in multi-UPS
    # Whether to broadcast shutdown warnings via wall(1) to every logged-in
    # tty. Off by default since v5.2 — the `wall` blast was a holdover from
    # the v2 "ups-monitor" days when the shell was the only notification
    # channel. Apprise covers the modern path; opt in here if you still want
    # tty broadcasts on top.
    wall: bool = False


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
    # v6.0: optional per-group UPS-control override (creds/allowlists) for
    # deployments where this UPS lives on a different upsd. None => use global.
    nut_control: Optional[NutControlConfig] = None
    # v6.1: optional per-UPS overrides. Battery health (install date, nominal
    # runtime, expected life) and self-test support are UPS-specific, so a
    # multi-UPS user can give each UPS its own values. None => use global.
    battery_health: Optional[BatteryHealthConfig] = None
    self_test: Optional[SelfTestConfig] = None

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
    api: APIConfig = field(default_factory=APIConfig)
    prometheus: PrometheusConfig = field(default_factory=PrometheusConfig)
    remote_health: RemoteHealthConfig = field(default_factory=RemoteHealthConfig)
    mqtt: MQTTConfig = field(default_factory=MQTTConfig)
    nut_control: NutControlConfig = field(default_factory=NutControlConfig)
    # v6.1: battery intelligence, scheduled self-test, periodic reports, energy.
    battery_health: BatteryHealthConfig = field(default_factory=BatteryHealthConfig)
    self_test: SelfTestConfig = field(default_factory=SelfTestConfig)
    reports: ReportsConfig = field(default_factory=ReportsConfig)
    energy: EnergyConfig = field(default_factory=EnergyConfig)
    # v5.2.1: source path of the YAML this Config was loaded from.
    # Used by deferred_delivery to spawn a systemd-run timer that
    # re-loads the same config out-of-process. None when the Config
    # was constructed in-memory (tests, programmatic usage).
    config_path: Optional[str] = None

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

    @staticmethod
    def _is_int_nonbool_in_range(value: Any, *, minimum: int = None,
                                 maximum: int = None) -> bool:
        """Return True for integers, excluding bool, inside optional bounds."""
        if not isinstance(value, int) or isinstance(value, bool):
            return False
        if minimum is not None and value < minimum:
            return False
        if maximum is not None and value > maximum:
            return False
        return True

    DEFAULT_CONFIG_PATHS = [
        Path("/etc/ups-monitor/config.yaml"),
        Path("/etc/ups-monitor/config.yml"),
    ]

    @staticmethod
    def _unknown_key_errors(section: str, data: Any,
                            allowed: set) -> List[str]:
        """Return ERROR lines for unknown YAML keys in one mapping.

        Deprecated-but-supported aliases are included in ``allowed`` by the
        caller. Everything else is a hard error because misspelled safety
        settings should never be silently ignored.
        """
        if not isinstance(data, dict):
            return []
        errors = []
        for key in sorted(data):
            if key in allowed:
                continue
            suggestion = get_close_matches(str(key), sorted(allowed), n=1)
            hint = f" Did you mean '{suggestion[0]}'?" if suggestion else ""
            errors.append(f"ERROR: unknown config key '{section}.{key}'.{hint}")
        return errors

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
                raw_data = yaml.safe_load(f)
            if raw_data is None:
                data = {}
            elif isinstance(raw_data, dict):
                data = raw_data
            else:
                print(f"Error reading config file {path}: root must be a YAML mapping.")
                print("Using default configuration.")
                return config
        except Exception as e:
            print(f"Error reading config file {path}: {e}")
            print("Using default configuration.")
            return config

        # Parse configuration sections
        config = cls._parse_config(data)
        # v5.2.1: stash the source path so deferred_delivery can spawn
        # a systemd-run timer that re-loads the same YAML out-of-process.
        config.config_path = str(path)
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
            on_battery_stabilization_delay=triggers_data.get(
                'on_battery_stabilization_delay',
                defaults.on_battery_stabilization_delay,
            ),
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
                    mounts = []
                    for mount in cmd_data.get('mounts', []) or []:
                        if isinstance(mount, str):
                            mounts.append({'path': mount, 'options': ''})
                        elif isinstance(mount, dict):
                            mounts.append({
                                'path': mount.get('path', ''),
                                'options': mount.get('options', ''),
                            })
                    pre_cmds.append(RemoteCommandConfig(
                        action=cmd_data.get('action'),
                        command=cmd_data.get('command'),
                        timeout=cmd_data.get('timeout'),
                        path=cmd_data.get('path'),
                        mounts=mounts,
                    ))
            is_loopback_explicit = 'is_host_loopback' in server_data
            is_loopback = (
                server_data.get('is_host_loopback', False)
                if is_loopback_explicit
                else False
            )
            # Loopback entries default host to 127.0.0.1 — with `network_mode: host`
            # (the recommended path for full local ownership) this is the host's sshd.
            default_host = '127.0.0.1' if is_loopback is True else ''
            servers.append(RemoteServerConfig(
                name=server_data.get('name', ''),
                enabled=server_data.get('enabled', False),
                host=server_data.get('host') or default_host,
                user=server_data.get('user', ''),
                connect_timeout=server_data.get('connect_timeout', 10),
                command_timeout=server_data.get('command_timeout', 30),
                shutdown_command=server_data.get('shutdown_command', 'sudo shutdown -h now'),
                use_sudo=server_data.get('use_sudo', False),
                ssh_key_path=server_data.get('ssh_key_path'),
                ssh_options=server_data.get('ssh_options', []),
                pre_shutdown_commands=pre_cmds,
                parallel=server_data.get('parallel'),
                shutdown_order=server_data.get('shutdown_order'),
                shutdown_safety_margin=server_data.get('shutdown_safety_margin', 60),
                is_host_loopback=is_loopback,
                _is_host_loopback_explicit=is_loopback_explicit,
                host_identity_command=server_data.get(
                    'host_identity_command', 'cat /etc/machine-id'),
                expected_host_identity=server_data.get('expected_host_identity'),
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
        # v5.2 persistent-queue defaults.
        notif_retention_days = 7
        notif_max_attempts = 0
        notif_max_age_days = 30
        notif_max_pending = 10000
        notif_retry_backoff_max = 300

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
            # Coerce numeric YAML values to int defensively (Cubic P2).
            # Strings like "7" parse cleanly; garbage raises ValueError
            # which would otherwise surface as a TypeError deep inside
            # the worker's backoff math at runtime, far from the source.
            def _as_int(key, default):
                v = notif_data.get(key, default)
                try:
                    return int(v)
                except (TypeError, ValueError):
                    print(
                        f"⚠️  Notifications config: {key}={v!r} not numeric; "
                        f"using default {default}"
                    )
                    return default

            notif_retention_days = _as_int(
                'retention_days', notif_retention_days,
            )
            notif_max_attempts = _as_int(
                'max_attempts', notif_max_attempts,
            )
            notif_max_age_days = _as_int(
                'max_age_days', notif_max_age_days,
            )
            notif_max_pending = _as_int(
                'max_pending', notif_max_pending,
            )
            notif_retry_backoff_max = _as_int(
                'retry_backoff_max', notif_retry_backoff_max,
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
            retention_days=notif_retention_days,
            max_attempts=notif_max_attempts,
            max_age_days=notif_max_age_days,
            max_pending=notif_max_pending,
            retry_backoff_max=notif_retry_backoff_max,
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
            logging_data = data['logging'] if isinstance(data.get('logging'), dict) else {}
            raw_syslog = logging_data.get('syslog')
            syslog_data = raw_syslog if isinstance(raw_syslog, dict) else {}
            config.logging = LoggingConfig(
                file=logging_data.get('file', config.logging.file),
                state_file=logging_data.get('state_file', config.logging.state_file),
                battery_history_file=logging_data.get('battery_history_file',
                                                      config.logging.battery_history_file),
                shutdown_flag_file=logging_data.get('shutdown_flag_file',
                                                    config.logging.shutdown_flag_file),
                format=logging_data.get('format', config.logging.format),
                syslog=SyslogConfig(
                    enabled=syslog_data.get('enabled', config.logging.syslog.enabled),
                    address=syslog_data.get('address', config.logging.syslog.address),
                    port=syslog_data.get('port', config.logging.syslog.port),
                    facility=syslog_data.get('facility', config.logging.syslog.facility),
                ),
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
                wall=local_data.get('wall', False),
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

        if 'api' in data:
            # Each nested section is parsed defensively — a YAML scalar
            # like ``api: true`` (instead of ``api: {enabled: true}``)
            # would otherwise crash the loader with AttributeError on
            # ``.get`` instead of producing a clean validation message.
            raw_api = data.get('api')
            api_data = raw_api if isinstance(raw_api, dict) else {}
            raw_auth = api_data.get('auth')
            auth_data = raw_auth if isinstance(raw_auth, dict) else {}
            config.api = APIConfig(
                enabled=api_data.get('enabled', config.api.enabled),
                bind=api_data.get('bind', config.api.bind),
                port=api_data.get('port', config.api.port),
                auth=AuthConfig(
                    enabled=auth_data.get('enabled', config.api.auth.enabled),
                    require_for_reads=auth_data.get(
                        'require_for_reads', config.api.auth.require_for_reads),
                    session_ttl=auth_data.get(
                        'session_ttl', config.api.auth.session_ttl),
                    db_path=auth_data.get('db_path', config.api.auth.db_path),
                    enabled_explicitly_set='enabled' in auth_data,
                ),
            )

        if 'prometheus' in data:
            raw_prom = data.get('prometheus')
            prom_data = raw_prom if isinstance(raw_prom, dict) else {}
            config.prometheus = PrometheusConfig(
                enabled=prom_data.get('enabled', config.prometheus.enabled),
            )

        if 'remote_health' in data:
            raw_rh = data.get('remote_health')
            rh_data = raw_rh if isinstance(raw_rh, dict) else {}
            config.remote_health = RemoteHealthConfig(
                enabled=rh_data.get('enabled', config.remote_health.enabled),
                startup_check=rh_data.get('startup_check',
                                          config.remote_health.startup_check),
                interval=rh_data.get('interval', config.remote_health.interval),
                probe_command=rh_data.get('probe_command',
                                          config.remote_health.probe_command),
                failure_threshold=rh_data.get('failure_threshold',
                                              config.remote_health.failure_threshold),
                notify_on_failure=rh_data.get('notify_on_failure',
                                              config.remote_health.notify_on_failure),
                notify_on_recovery=rh_data.get('notify_on_recovery',
                                               config.remote_health.notify_on_recovery),
            )

        if 'mqtt' in data:
            raw_mqtt = data.get('mqtt')
            mqtt_data = raw_mqtt if isinstance(raw_mqtt, dict) else {}
            config.mqtt = MQTTConfig(
                enabled=mqtt_data.get('enabled', config.mqtt.enabled),
                broker=mqtt_data.get('broker', config.mqtt.broker),
                topic_prefix=mqtt_data.get('topic_prefix', config.mqtt.topic_prefix),
                publish_interval=mqtt_data.get('publish_interval',
                                               config.mqtt.publish_interval),
            )

        if 'nut_control' in data:
            raw_nc = data.get('nut_control')
            nc_data = raw_nc if isinstance(raw_nc, dict) else {}
            config.nut_control = cls._parse_nut_control(nc_data, config.nut_control)

        # v6.1 sections. battery_health / self_test are parsed before the UPS
        # list so per-UPS overrides can inherit from these globals.
        if 'battery_health' in data:
            raw_bh = data.get('battery_health')
            bh_data = raw_bh if isinstance(raw_bh, dict) else {}
            config.battery_health = cls._parse_battery_health(
                bh_data, config.battery_health)

        if 'self_test' in data:
            raw_st = data.get('self_test')
            st_data = raw_st if isinstance(raw_st, dict) else {}
            config.self_test = cls._parse_self_test(st_data, config.self_test)

        if 'reports' in data:
            raw_r = data.get('reports')
            r = raw_r if isinstance(raw_r, dict) else {}
            inc = r.get('include', config.reports.include)
            config.reports = ReportsConfig(
                enabled=r.get('enabled', config.reports.enabled),
                daily=r.get('daily', config.reports.daily),
                weekly=r.get('weekly', config.reports.weekly),
                monthly=r.get('monthly', config.reports.monthly),
                time=r.get('time', config.reports.time),
                weekly_day=r.get('weekly_day', config.reports.weekly_day),
                monthly_day=r.get('monthly_day', config.reports.monthly_day),
                include=list(inc) if isinstance(inc, list) else config.reports.include,
                format=r.get('format', config.reports.format),
            )

        if 'energy' in data:
            raw_e = data.get('energy')
            e = raw_e if isinstance(raw_e, dict) else {}
            config.energy = EnergyConfig(
                enabled=e.get('enabled', config.energy.enabled),
                cost_per_kwh=e.get('cost_per_kwh', config.energy.cost_per_kwh),
                currency=e.get('currency', config.energy.currency),
                cost_format=e.get('cost_format', config.energy.cost_format),
                nominal_power=e.get('nominal_power', config.energy.nominal_power),
            )

        # Detect legacy vs multi-UPS format
        ups_raw = data.get('ups', {})

        if isinstance(ups_raw, list):
            # --- Multi-UPS mode ---
            config.ups_groups = cls._parse_multi_ups(
                ups_raw, global_triggers, config.nut_control,
                config.battery_health, config.self_test)
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

    @staticmethod
    def _parse_nut_control(nc_data: Dict[str, Any],
                           base: "NutControlConfig") -> "NutControlConfig":
        """Parse a nut_control mapping, inheriting unset fields from ``base``.

        Allowlists are coerced defensively: a scalar or ``null`` becomes an empty
        list (validate_config reports the malformed type) rather than crashing or
        turning a string into a character list.
        """
        def _as_list(value):
            return [str(v) for v in value] if isinstance(value, list) else []
        return NutControlConfig(
            enabled=nc_data.get('enabled', base.enabled),
            username=nc_data.get('username', base.username),
            password=nc_data.get('password', base.password),
            allowed_commands=_as_list(nc_data.get('allowed_commands',
                                                  base.allowed_commands)),
            allowed_variables=_as_list(nc_data.get('allowed_variables',
                                                   base.allowed_variables)),
            timeout=nc_data.get('timeout', base.timeout),
        )

    @staticmethod
    def _parse_battery_health(bh_data: Dict[str, Any],
                              base: "BatteryHealthConfig") -> "BatteryHealthConfig":
        """Parse a battery_health mapping, inheriting unset fields from ``base``."""
        base = base or BatteryHealthConfig()
        raw_rep = bh_data.get('replacement')
        rep = raw_rep if isinstance(raw_rep, dict) else {}
        return BatteryHealthConfig(
            enabled=bh_data.get('enabled', base.enabled),
            update_interval=bh_data.get('update_interval', base.update_interval),
            nominal_runtime_seconds=bh_data.get(
                'nominal_runtime_seconds', base.nominal_runtime_seconds),
            battery_install_date=bh_data.get(
                'battery_install_date', base.battery_install_date),
            expected_life_years=bh_data.get(
                'expected_life_years', base.expected_life_years),
            replacement=BatteryReplacementConfig(
                threshold_score=rep.get(
                    'threshold_score', base.replacement.threshold_score),
                horizon_days=rep.get('horizon_days', base.replacement.horizon_days),
                min_history_days=rep.get(
                    'min_history_days', base.replacement.min_history_days),
            ),
        )

    @staticmethod
    def _parse_self_test(st_data: Dict[str, Any],
                         base: "SelfTestConfig") -> "SelfTestConfig":
        """Parse a self_test mapping, inheriting unset fields from ``base``."""
        base = base or SelfTestConfig()
        return SelfTestConfig(
            enabled=st_data.get('enabled', base.enabled),
            schedule=st_data.get('schedule', base.schedule),
            time=st_data.get('time', base.time),
            command=st_data.get('command', base.command),
            result_poll_after=st_data.get(
                'result_poll_after', base.result_poll_after),
        )

    @classmethod
    def _parse_multi_ups(cls, ups_list: list,
                          global_triggers: TriggersConfig,
                          global_nut_control: "NutControlConfig" = None,
                          global_battery_health: "BatteryHealthConfig" = None,
                          global_self_test: "SelfTestConfig" = None
                          ) -> List[UPSGroupConfig]:
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

            # Per-group UPS-control override. None => use the global config.
            # When present, unset fields INHERIT the global config (base), and an
            # explicitly-empty allowlist means deny-all for this group — so a
            # narrowed group can never silently fall back to the wider global set.
            nut_control = None
            if isinstance(entry.get('nut_control'), dict):
                base = global_nut_control or NutControlConfig()
                nut_control = cls._parse_nut_control(entry['nut_control'], base)

            # v6.1 per-UPS overrides: same base-inheritance contract as
            # nut_control — unset fields inherit the global default for that UPS.
            battery_health = None
            if isinstance(entry.get('battery_health'), dict):
                base = global_battery_health or BatteryHealthConfig()
                battery_health = cls._parse_battery_health(
                    entry['battery_health'], base)
            self_test = None
            if isinstance(entry.get('self_test'), dict):
                base = global_self_test or SelfTestConfig()
                self_test = cls._parse_self_test(entry['self_test'], base)

            group = UPSGroupConfig(
                ups=ups_config,
                triggers=triggers,
                remote_servers=remote_servers,
                virtual_machines=vm_config,
                containers=containers_config,
                filesystems=fs_config,
                is_local=is_local,
                nut_control=nut_control,
                battery_health=battery_health,
                self_test=self_test,
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

        if raw_data:
            trigger_keys = {
                "low_battery_threshold", "critical_runtime_threshold",
                "on_battery_stabilization_delay", "depletion",
                "extended_time", "voltage_sensitivity",
            }
            remote_server_keys = {
                "name", "enabled", "host", "user", "connect_timeout",
                "command_timeout", "shutdown_command", "ssh_key_path",
                "use_sudo", "ssh_options", "pre_shutdown_commands", "parallel",
                "shutdown_order", "shutdown_safety_margin",
                "is_host_loopback", "host_identity_command",
                "expected_host_identity",
            }
            pre_shutdown_keys = {"action", "command", "timeout", "path", "mounts"}
            depletion_keys = {"window", "critical_rate", "grace_period"}
            extended_time_keys = {"enabled", "threshold"}
            messages.extend(cls._unknown_key_errors(
                "behavior", raw_data.get("behavior", {}), {"dry_run"},
            ))
            # M13: local_shutdown is a safety section -- its parser defaults
            # `enabled` to True, so a misspelled key would silently leave local
            # poweroff enabled. Sweep it for unknown keys like every other
            # safety section (configuration.md promises typos are caught).
            messages.extend(cls._unknown_key_errors(
                "local_shutdown", raw_data.get("local_shutdown", {}),
                {"enabled", "command", "message", "drain_on_local_shutdown",
                 "trigger_on", "wall"},
            ))
            messages.extend(cls._unknown_key_errors(
                "api", raw_data.get("api", {}),
                {"enabled", "bind", "port", "auth"},
            ))
            raw_api_section = raw_data.get("api", {})
            if isinstance(raw_api_section, dict):
                messages.extend(cls._unknown_key_errors(
                    "api.auth", raw_api_section.get("auth", {}),
                    {"enabled", "require_for_reads", "session_ttl", "db_path"},
                ))
            messages.extend(cls._unknown_key_errors(
                "prometheus", raw_data.get("prometheus", {}), {"enabled"},
            ))
            messages.extend(cls._unknown_key_errors(
                "remote_health",
                raw_data.get("remote_health", {}),
                {
                    "enabled", "startup_check", "interval", "probe_command",
                    "failure_threshold", "notify_on_failure",
                    "notify_on_recovery",
                },
            ))
            messages.extend(cls._unknown_key_errors(
                "mqtt",
                raw_data.get("mqtt", {}),
                {"enabled", "broker", "topic_prefix", "publish_interval"},
            ))
            # v6.1 sections
            _bh_keys = {"enabled", "update_interval", "nominal_runtime_seconds",
                        "battery_install_date", "expected_life_years",
                        "replacement"}
            _rep_keys = {"threshold_score", "horizon_days", "min_history_days"}
            _st_keys = {"enabled", "schedule", "time", "command",
                        "result_poll_after"}

            def _check_battery_health(block, label):
                if not isinstance(block, dict):
                    return
                messages.extend(cls._unknown_key_errors(label, block, _bh_keys))
                if isinstance(block.get("replacement"), dict):
                    messages.extend(cls._unknown_key_errors(
                        f"{label}.replacement", block["replacement"], _rep_keys))

            _check_battery_health(raw_data.get("battery_health", {}),
                                  "battery_health")
            messages.extend(cls._unknown_key_errors(
                "self_test", raw_data.get("self_test", {}), _st_keys,
            ))
            messages.extend(cls._unknown_key_errors(
                "reports", raw_data.get("reports", {}),
                {"enabled", "daily", "weekly", "monthly", "time",
                 "weekly_day", "monthly_day", "include", "format"},
            ))
            messages.extend(cls._unknown_key_errors(
                "energy", raw_data.get("energy", {}),
                {"enabled", "cost_per_kwh", "currency", "cost_format",
                 "nominal_power"},
            ))
            _nc_keys = {"enabled", "username", "password", "allowed_commands",
                        "allowed_variables", "timeout"}

            def _check_nut_control(block, label):
                # Validate one nut_control mapping (global or per-group) so a
                # malformed allowlist is a hard error, never a silent widening.
                if not isinstance(block, dict):
                    return
                messages.extend(cls._unknown_key_errors(label, block, _nc_keys))
                # N4: NUT command/variable names are dotted alphanumerics. Reject
                # an allowlist entry with spaces / shell metacharacters / '=' at
                # load (a typo'd entry would otherwise flow verbatim into the
                # upscmd/upsrw argv). Not an injection (argv, not shell), but
                # catching it here turns a silent no-op into a startup error.
                _nut_name_chars = set(
                    "abcdefghijklmnopqrstuvwxyz"
                    "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._+-")
                for list_key in ("allowed_commands", "allowed_variables"):
                    val = block.get(list_key)
                    if val is None:
                        continue
                    if not isinstance(val, list):
                        messages.append(
                            f"ERROR: {label}.{list_key} must be a list")
                        continue
                    for entry in val:
                        if not (isinstance(entry, str) and entry
                                and all(c in _nut_name_chars for c in entry)):
                            messages.append(
                                f"ERROR: {label}.{list_key} entry {entry!r} is not "
                                "a valid NUT name (letters, digits, . _ + - only)")
                t = block.get("timeout")
                if t is not None and (isinstance(t, bool) or not isinstance(t, int)
                                      or t < 1):
                    messages.append(
                        f"ERROR: {label}.timeout must be an integer >= 1, "
                        f"got {t!r}")

            _check_nut_control(raw_data.get("nut_control", {}), "nut_control")
            # Per-group overrides (multi-UPS list form).
            raw_ups = raw_data.get("ups")
            if isinstance(raw_ups, list):
                for idx, entry in enumerate(raw_ups):
                    if not isinstance(entry, dict) or "nut_control" not in entry:
                        continue
                    name = entry.get("name") or f"ups[{idx}]"
                    block = entry["nut_control"]
                    if not isinstance(block, dict):
                        messages.append(
                            f"ERROR: nut_control for UPS '{name}' must be a mapping")
                        continue
                    # The feature is gated by the GLOBAL nut_control.enabled; a
                    # per-group `enabled` is ignored at runtime, so reject it
                    # rather than silently mislead the operator.
                    if "enabled" in block:
                        messages.append(
                            f"ERROR: nut_control for UPS '{name}' must not set "
                            "'enabled' (UPS control is enabled globally)")
                    _check_nut_control(block, f"ups '{name}' nut_control")
            # v6.1 per-UPS battery_health / self_test override key checks.
            if isinstance(raw_ups, list):
                for idx, entry in enumerate(raw_ups):
                    if not isinstance(entry, dict):
                        continue
                    name = entry.get("name") or f"ups[{idx}]"
                    if isinstance(entry.get("battery_health"), dict):
                        _check_battery_health(
                            entry["battery_health"],
                            f"ups '{name}' battery_health")
                    if isinstance(entry.get("self_test"), dict):
                        messages.extend(cls._unknown_key_errors(
                            f"ups '{name}' self_test",
                            entry["self_test"], _st_keys))
            logging_raw = raw_data.get("logging", {})
            messages.extend(cls._unknown_key_errors(
                "logging",
                logging_raw,
                {
                    "file", "state_file", "battery_history_file",
                    "shutdown_flag_file", "format", "syslog",
                },
            ))
            messages.extend(cls._unknown_key_errors(
                "logging.syslog",
                logging_raw.get("syslog", {}) if isinstance(logging_raw, dict) else {},
                {"enabled", "address", "port", "facility"},
            ))

            def _validate_triggers(section: str, data: Any):
                if not isinstance(data, dict):
                    return
                messages.extend(cls._unknown_key_errors(section, data, trigger_keys))
                messages.extend(cls._unknown_key_errors(
                    f"{section}.depletion",
                    data.get("depletion", {}),
                    depletion_keys,
                ))
                messages.extend(cls._unknown_key_errors(
                    f"{section}.extended_time",
                    data.get("extended_time", {}),
                    extended_time_keys,
                ))

            def _validate_remote_servers(section: str, data: Any):
                if not isinstance(data, list):
                    return
                for idx, entry in enumerate(data):
                    if not isinstance(entry, dict):
                        continue
                    label = entry.get("name") or entry.get("host") or idx
                    server_section = f"{section}[{label!r}]"
                    messages.extend(cls._unknown_key_errors(
                        server_section, entry, remote_server_keys,
                    ))
                    ssh_options = entry.get("ssh_options")
                    if ssh_options is not None:
                        if not isinstance(ssh_options, list):
                            messages.append(
                                f"ERROR: {server_section}.ssh_options must be a list"
                            )
                        else:
                            for opt_idx, opt in enumerate(ssh_options):
                                if not isinstance(opt, str):
                                    messages.append(
                                        f"ERROR: {server_section}.ssh_options"
                                        f"[{opt_idx}] must be a string"
                                    )
                    pre_cmds = entry.get("pre_shutdown_commands", []) or []
                    if not isinstance(pre_cmds, list):
                        continue
                    for cmd_idx, cmd in enumerate(pre_cmds):
                        if not isinstance(cmd, dict):
                            continue
                        messages.extend(cls._unknown_key_errors(
                            f"{server_section}.pre_shutdown_commands[{cmd_idx}]",
                            cmd,
                            pre_shutdown_keys,
                        ))

            _validate_triggers("triggers", raw_data.get("triggers", {}))
            _validate_remote_servers(
                "remote_servers", raw_data.get("remote_servers", []),
            )
            # Per-UPS-entry top-level keys. Every other section gets a strict
            # unknown-key sweep; without this one a typo like `is_locl: true`
            # silently parses the group as non-local (is_local defaults False),
            # disabling local-host self-protection while the operator believes
            # it is on. Mirror the contract: misspelled safety keys must error.
            ups_entry_keys = {
                "name", "display_name", "check_interval",
                "max_stale_data_tolerance", "connection_loss_grace_period",
                "is_local", "triggers", "remote_servers", "virtual_machines",
                "containers", "filesystems", "nut_control",
                # v6.1 per-UPS overrides
                "battery_health", "self_test",
            }
            redundancy_entry_keys = {
                "name", "ups_sources", "min_healthy", "degraded_counts_as",
                "unknown_counts_as", "is_local", "triggers", "remote_servers",
                "virtual_machines", "containers", "filesystems",
            }
            ups_raw = raw_data.get("ups")
            if isinstance(ups_raw, list):
                for idx, entry in enumerate(ups_raw):
                    if not isinstance(entry, dict):
                        continue
                    label = entry.get("name", idx)
                    messages.extend(cls._unknown_key_errors(
                        f"ups[{label!r}]", entry, ups_entry_keys,
                    ))
                    # Also sweep the nested connection_loss_grace_period sub-keys
                    # -- it's a safety sub-section, so a typo there must error too
                    # rather than silently fall back to defaults (cubic P2).
                    clgp = entry.get("connection_loss_grace_period")
                    if isinstance(clgp, dict):
                        messages.extend(cls._unknown_key_errors(
                            f"ups[{label!r}].connection_loss_grace_period",
                            clgp, {"enabled", "duration", "flap_threshold"},
                        ))
                    _validate_triggers(
                        f"ups[{label!r}].triggers",
                        entry.get("triggers", {}),
                    )
                    _validate_remote_servers(
                        f"ups[{label!r}].remote_servers",
                        entry.get("remote_servers", []),
                    )
            groups_raw = raw_data.get("redundancy_groups", []) or []
            if isinstance(groups_raw, list):
                for idx, entry in enumerate(groups_raw):
                    if not isinstance(entry, dict):
                        continue
                    label = entry.get("name", idx)
                    messages.extend(cls._unknown_key_errors(
                        f"redundancy_groups[{label!r}]", entry,
                        redundancy_entry_keys,
                    ))
                    group_triggers = entry.get("triggers", {})
                    _validate_triggers(
                        f"redundancy_groups[{label!r}].triggers",
                        group_triggers,
                    )
                    _validate_remote_servers(
                        f"redundancy_groups[{label!r}].remote_servers",
                        entry.get("remote_servers", []),
                    )
                    depletion = (
                        group_triggers.get("depletion", {})
                        if isinstance(group_triggers, dict)
                        else {}
                    )
                    if isinstance(depletion, dict) and "window" in depletion:
                        messages.append(
                            "ERROR: "
                            f"redundancy_groups[{label!r}].triggers."
                            "depletion.window is not supported; depletion "
                            "rate history is computed by each UPS monitor. "
                            "Set triggers.depletion.window globally or on "
                            "ups[*].triggers instead."
                        )

        # Check Apprise availability
        if config.notifications.enabled and not APPRISE_AVAILABLE:
            messages.append(
                "WARNING: Notifications enabled but apprise package not installed. "
                "Notifications will be disabled. Install with: uv pip install apprise"
            )

        if config.logging.format not in ("text", "json"):
            messages.append(
                "ERROR: logging.format must be 'text' or 'json', "
                f"got {config.logging.format!r}."
            )

        if not cls._is_int_nonbool_in_range(
            config.logging.syslog.port, minimum=1, maximum=65535,
        ):
            messages.append(
                "ERROR: logging.syslog.port must be an integer between 1 and 65535, "
                f"got {config.logging.syslog.port!r}."
            )

        import logging.handlers
        valid_facilities = set(logging.handlers.SysLogHandler.facility_names)
        facility = config.logging.syslog.facility
        if (not isinstance(facility, str)
                or facility.lower() not in valid_facilities):
            messages.append(
                "ERROR: logging.syslog.facility must be a valid syslog "
                f"facility, got {facility!r}."
            )
        elif facility != facility.lower():
            config.logging.syslog.facility = facility.lower()

        if not cls._is_int_nonbool_in_range(config.api.port, minimum=1, maximum=65535):
            messages.append(
                "ERROR: api.port must be an integer between 1 and 65535, "
                f"got {config.api.port!r}."
            )

        if not cls._is_int_nonbool_in_range(config.remote_health.interval, minimum=60):
            messages.append(
                "ERROR: remote_health.interval must be an integer >= 60 seconds, "
                f"got {config.remote_health.interval!r}."
            )

        if not cls._is_int_nonbool_in_range(
            config.remote_health.failure_threshold, minimum=1,
        ):
            messages.append(
                "ERROR: remote_health.failure_threshold must be an integer >= 1, "
                f"got {config.remote_health.failure_threshold!r}."
            )

        if not str(config.remote_health.probe_command).strip():
            messages.append("ERROR: remote_health.probe_command cannot be empty.")
        else:
            from eneru.remote_health import is_safe_probe_command
            if not is_safe_probe_command(config.remote_health.probe_command):
                messages.append(
                    "ERROR: remote_health.probe_command must be a harmless "
                    f"SSH probe, got {config.remote_health.probe_command!r}."
                )

        if config.mqtt.enabled and not str(config.mqtt.broker).strip():
            messages.append("ERROR: mqtt.broker is required when mqtt.enabled is true.")

        if not cls._is_int_nonbool_in_range(config.mqtt.publish_interval, minimum=1):
            messages.append(
                "ERROR: mqtt.publish_interval must be an integer >= 1 second, "
                f"got {config.mqtt.publish_interval!r}."
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
        # Type-check before the membership lookup: an unhashable YAML
        # value (e.g., `voltage_sensitivity: [tight]` parses as a list)
        # would otherwise raise TypeError inside `value not in ...` and
        # bypass the validator's normal error-reporting flow.
        def _is_invalid_sensitivity(v) -> bool:
            return not isinstance(v, str) or v not in VOLTAGE_SENSITIVITY_PRESETS

        for group in config.ups_groups:
            value = group.triggers.voltage_sensitivity
            if _is_invalid_sensitivity(value):
                messages.append(
                    f"ERROR: invalid ups[{group.ups.label!r}]."
                    f"triggers.voltage_sensitivity {value!r}; "
                    f"expected one of {sorted(VOLTAGE_SENSITIVITY_PRESETS)}."
                )
        for rg in config.redundancy_groups:
            value = rg.triggers.voltage_sensitivity
            if _is_invalid_sensitivity(value):
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

        for group in config.ups_groups:
            delay = group.triggers.on_battery_stabilization_delay
            if (not isinstance(delay, int)
                    or isinstance(delay, bool)
                    or delay < 0):
                messages.append(
                    f"ERROR: ups[{group.ups.label!r}]."
                    "triggers.on_battery_stabilization_delay must be a "
                    f"non-negative integer, got {delay!r}."
                )

        for rg in config.redundancy_groups:
            delay = rg.triggers.on_battery_stabilization_delay
            if (not isinstance(delay, int)
                    or isinstance(delay, bool)
                    or delay < 0):
                label = rg.name or "(unnamed)"
                messages.append(
                    f"ERROR: redundancy_groups[{label!r}]."
                    "triggers.on_battery_stabilization_delay must be a "
                    f"non-negative integer, got {delay!r}."
                )

        # Shutdown-trigger numeric fields feed direct comparisons in the
        # on-battery hot path (monitor._handle_on_battery, health/battery.py).
        # A non-numeric YAML scalar -- most commonly a quoted "20", which
        # templating tools (Ansible/Helm/envsubst) emit routinely -- survives
        # parse as a str and raises TypeError on the FIRST on-battery poll,
        # killing the monitor loop exactly when a shutdown decision is due.
        # Validate every group's PARSED triggers so a bad value is a startup
        # error, never a mid-outage crash.
        def _check_trigger_numbers(label: str, t: TriggersConfig):
            if not cls._is_int_nonbool_in_range(
                    t.low_battery_threshold, minimum=0, maximum=100):
                messages.append(
                    f"ERROR: {label}.triggers.low_battery_threshold must be an "
                    f"integer between 0 and 100, got {t.low_battery_threshold!r}."
                )
            if not cls._is_int_nonbool_in_range(
                    t.critical_runtime_threshold, minimum=0):
                messages.append(
                    f"ERROR: {label}.triggers.critical_runtime_threshold must be "
                    f"a non-negative integer, got {t.critical_runtime_threshold!r}."
                )
            if not cls._is_int_nonbool_in_range(t.depletion.window, minimum=1):
                messages.append(
                    f"ERROR: {label}.triggers.depletion.window must be an integer "
                    f">= 1, got {t.depletion.window!r}."
                )
            rate = t.depletion.critical_rate
            if (isinstance(rate, bool) or not isinstance(rate, (int, float))
                    or rate <= 0):
                messages.append(
                    f"ERROR: {label}.triggers.depletion.critical_rate must be a "
                    f"number greater than 0, got {rate!r}."
                )
            if not cls._is_int_nonbool_in_range(
                    t.depletion.grace_period, minimum=0):
                messages.append(
                    f"ERROR: {label}.triggers.depletion.grace_period must be a "
                    f"non-negative integer, got {t.depletion.grace_period!r}."
                )
            if not cls._is_int_nonbool_in_range(
                    t.extended_time.threshold, minimum=0):
                messages.append(
                    f"ERROR: {label}.triggers.extended_time.threshold must be a "
                    f"non-negative integer, got {t.extended_time.threshold!r}."
                )
            # L2: relationship check -- a stabilization window >= the
            # critical-runtime threshold suppresses the runtime trigger for the
            # entire remaining runtime, so the host could die on battery before
            # the window ever opens. Warn (don't reject -- it can be deliberate).
            sd = t.on_battery_stabilization_delay
            crt = t.critical_runtime_threshold
            if (cls._is_int_nonbool_in_range(sd, minimum=0)
                    and cls._is_int_nonbool_in_range(crt, minimum=1)
                    and sd >= crt):
                messages.append(
                    f"WARNING: {label}.triggers.on_battery_stabilization_delay "
                    f"({sd}s) >= critical_runtime_threshold ({crt}s): the "
                    "stabilization window can suppress the runtime trigger for "
                    "the whole remaining runtime. Consider lowering the delay."
                )

        for group in config.ups_groups:
            _check_trigger_numbers(f"ups[{group.ups.label!r}]", group.triggers)
        for rg in config.redundancy_groups:
            _check_trigger_numbers(
                f"redundancy_groups[{(rg.name or '(unnamed)')!r}]", rg.triggers)

        # Drain-phase timeouts feed `while time_waited < max_wait`,
        # `stop_timeout + 30`, and subprocess timeouts during shutdown. A
        # non-int (quoted "30s") crashes the phase mid-sequence; a null
        # unmount.timeout becomes subprocess.run(timeout=None) -> a busy umount
        # hangs forever. Validate them at load so the host still powers off.
        def _check_drain_timeouts(label: str, grp):
            mw = grp.virtual_machines.max_wait
            if not cls._is_int_nonbool_in_range(mw, minimum=0):
                messages.append(
                    f"ERROR: {label}.virtual_machines.max_wait must be a "
                    f"non-negative integer, got {mw!r}."
                )
            st = grp.containers.stop_timeout
            if not cls._is_int_nonbool_in_range(st, minimum=0):
                messages.append(
                    f"ERROR: {label}.containers.stop_timeout must be a "
                    f"non-negative integer, got {st!r}."
                )
            ut = grp.filesystems.unmount.timeout
            if not cls._is_int_nonbool_in_range(ut, minimum=1):
                messages.append(
                    f"ERROR: {label}.filesystems.unmount.timeout must be an "
                    f"integer >= 1, got {ut!r}."
                )

        for group in config.ups_groups:
            _check_drain_timeouts(f"ups[{group.ups.label!r}]", group)
        for rg in config.redundancy_groups:
            _check_drain_timeouts(
                f"redundancy_groups[{(rg.name or '(unnamed)')!r}]", rg)

        # ups.check_interval and ups.max_stale_data_tolerance feed the poll loop
        # and the failsafe debounce comparisons. A non-int (quoted "1") would
        # TypeError there; validate at load. (cubic P1 follow-up to H3.)
        for group in config.ups_groups:
            ci = group.ups.check_interval
            if not cls._is_int_nonbool_in_range(ci, minimum=1):
                messages.append(
                    f"ERROR: ups[{group.ups.label!r}].check_interval must be an "
                    f"integer >= 1, got {ci!r}."
                )
            mst = group.ups.max_stale_data_tolerance
            if not cls._is_int_nonbool_in_range(mst, minimum=1):
                messages.append(
                    f"ERROR: ups[{group.ups.label!r}].max_stale_data_tolerance "
                    f"must be an integer >= 1, got {mst!r}."
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

        # M7: multi-UPS with NO is_local group, local shutdown enabled, and the
        # default trigger_on='any' means ANY monitored UPS going critical (even
        # one powering only a remote server) will run the local poweroff. That's
        # rarely intended; warn so the operator confirms it (or marks a group
        # is_local / sets trigger_on: none). A single-UPS config implicitly owns
        # local resources, so this only applies to the multi-UPS topology.
        if (config.multi_ups and not all_local
                and config.local_shutdown.enabled
                and config.local_shutdown.trigger_on == "any"):
            messages.append(
                "WARNING: multi-UPS config has no is_local group but "
                "local_shutdown is enabled with trigger_on='any' -- ANY UPS "
                "going critical will power off this host. Mark the owning group "
                "is_local, or set local_shutdown.trigger_on: none, to confirm "
                "this is intended."
            )

        # ups.name uniqueness. The name keys the per-group stats DB path, the
        # state-file suffix, the monitors-by-name routing dict, and redundancy
        # member resolution -- duplicates corrupt or cross-wire all of those.
        # Dedup on the SANITIZED name actually used for filenames, so two names
        # differing only in @/:/ (which sanitize to the same file) still collide.
        def _sanitize_name(n: str) -> str:
            return (n or "").replace("@", "-").replace(":", "-").replace("/", "-")

        seen_ups_names: Dict[str, int] = {}
        for group in config.ups_groups:
            key = _sanitize_name(group.ups.name)
            seen_ups_names[key] = seen_ups_names.get(key, 0) + 1
        dup_ups = sorted(k for k, c in seen_ups_names.items() if c > 1)
        if dup_ups:
            messages.append(
                "ERROR: duplicate UPS name(s) across ups_groups: "
                f"{', '.join(dup_ups)}. Each ups.name must be unique -- it keys "
                "the stats DB, state file, API command routing, and redundancy "
                "membership."
            )

        # is_host_loopback uniqueness + per-entry rules. Runtime-context checks
        # (must be in a container, must have local capabilities) live in cli.py
        # since they depend on the live process environment.
        loopback_entries = []
        for group in config.ups_groups:
            for server in group.remote_servers:
                if server.is_host_loopback is True:
                    where = (
                        f"{group.ups.label}/"
                        f"{server.name or server.host or '(unnamed)'}"
                    )
                    if not group.is_local:
                        messages.append(
                            f"ERROR: remote_server '{where}' is_host_loopback: "
                            "true but the owning UPS group is not is_local. "
                            "The loopback delegate only makes sense on the "
                            "single group that owns the host."
                        )
                        continue
                    loopback_entries.append((group.ups.label, server))
        for group in config.redundancy_groups:
            for server in group.remote_servers:
                if server.is_host_loopback is True:
                    label = group.name or "(unnamed)"
                    where = (
                        f"{label}/{server.name or server.host or '(unnamed)'}"
                    )
                    if not group.is_local:
                        messages.append(
                            f"ERROR: remote_server '{where}' is_host_loopback: "
                            "true but the owning redundancy group is not "
                            "is_local. The loopback delegate only makes sense "
                            "on the single group that owns the host."
                        )
                        continue
                    loopback_entries.append((label, server))
        # Top-level remote_servers (single-UPS legacy layout) live on the
        # single UPS group, so they're already covered above.

        if len(loopback_entries) > 1:
            labels = ", ".join(
                f"{owner}/{srv.name or srv.host}" for owner, srv in loopback_entries
            )
            messages.append(
                f"ERROR: Multiple remote_servers marked is_host_loopback: {labels}. "
                "At most one entry across the whole config can be the host loopback."
            )

        if loopback_entries:
            from eneru.remote_health import is_safe_probe_command
            for owner, srv in loopback_entries:
                where = f"{owner}/{srv.name or srv.host or '(unnamed)'}"
                if not srv.enabled:
                    messages.append(
                        f"ERROR: remote_server '{where}' is is_host_loopback but "
                        "enabled is false. Loopback must be enabled to function."
                    )
                if not srv.user.strip():
                    messages.append(
                        f"ERROR: remote_server '{where}' is is_host_loopback but "
                        "'user' is empty. SSH-to-host needs a user; root is the "
                        "default, sudo NOPASSWD on /sbin/shutdown is recommended."
                    )
                if not is_safe_probe_command(srv.host_identity_command):
                    messages.append(
                        f"ERROR: remote_server '{where}' host_identity_command "
                        f"{srv.host_identity_command!r} contains unsafe shell "
                        "constructs. Must be a harmless probe like "
                        "'cat /etc/machine-id'."
                    )

        # Sudo guard for ALL remote_servers (loopback and otherwise).
        # Think of this as two keys on a keyring. Inline sudo in
        # shutdown_command unlocks only that final command; it does not unlock
        # generated pre-shutdown actions that Eneru builds separately from
        # action templates. Those still need use_sudo: true.
        def _has_generated_remote_actions(srv: RemoteServerConfig) -> bool:
            return any(bool(cmd.action) for cmd in srv.pre_shutdown_commands)

        def _command_invokes_sudo(command: str) -> bool:
            try:
                parts = shlex.split(command or "")
            except ValueError:
                return False
            if not parts:
                return False
            return Path(parts[0]).name == "sudo"

        def _all_remote_servers():
            for g in config.ups_groups:
                for s in g.remote_servers:
                    yield g.ups.label, s
            for g in config.redundancy_groups:
                for s in g.remote_servers:
                    yield g.name or "(unnamed)", s
        for owner, srv in _all_remote_servers():
            if not srv.enabled:
                continue
            user = srv.user.strip().lower()
            if (
                user
                and user != "root"
                and not srv.use_sudo
                and (
                    _has_generated_remote_actions(srv)
                    or not _command_invokes_sudo(srv.shutdown_command)
                )
            ):
                where = f"{owner}/{srv.name or srv.host or '(unnamed)'}"
                messages.append(
                    f"WARNING: remote_server '{where}' user is {srv.user!r} "
                    "but use_sudo is false. Non-root users typically need "
                    "use_sudo: true unless shutdown_command invokes sudo "
                    "itself and no generated pre-shutdown actions are enabled."
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

        # Validate SSH options, shutdown_order, shutdown_safety_margin,
        # and the mutual-exclusion of shutdown_order vs parallel.
        #
        # v5.5: is_host_loopback delegates no longer need shutdown_order
        # > max(others). The runtime now brackets every loopback around
        # the regular remotes (pre-actions first, poweroff last) in
        # RemoteShutdownMixin._shutdown_remote_servers. Any
        # shutdown_order set on a loopback entry is ignored at execution
        # time — kept here for backward compatibility with explicit YAML
        # but not validated.
        server_groups = [g.remote_servers for g in config.ups_groups]
        server_groups.extend(g.remote_servers for g in config.redundancy_groups)
        for servers in server_groups:
            for server in servers:
                display = server.name or server.host

                if server.ssh_key_path is not None:
                    if (not isinstance(server.ssh_key_path, str)
                            or not server.ssh_key_path.strip()):
                        messages.append(
                            f"ERROR: Remote server '{display}': ssh_key_path "
                            "must be a non-empty string when set."
                        )

                if not isinstance(server.use_sudo, bool):
                    messages.append(
                        f"ERROR: Remote server '{display}': use_sudo must be "
                        f"a boolean, got {server.use_sudo!r}"
                    )

                if not isinstance(server.is_host_loopback, bool):
                    messages.append(
                        f"ERROR: Remote server '{display}': is_host_loopback "
                        f"must be a boolean, got {server.is_host_loopback!r}"
                    )

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

        # local_shutdown.command is the host poweroff itself. `command:` with a
        # null value parses to None (the default only applies to an ABSENT key),
        # and an empty string yields run_command([]) -- both silently skip the
        # poweroff AFTER VMs/containers/remotes were already drained. Reject a
        # missing/empty command at load when local shutdown is enabled.
        if config.local_shutdown.enabled:
            cmd = config.local_shutdown.command
            if not isinstance(cmd, str) or not cmd.strip():
                messages.append(
                    "ERROR: local_shutdown.command must be a non-empty string "
                    f"when local_shutdown.enabled is true, got {cmd!r}."
                )

        # api.auth.session_ttl and nut_control.timeout are coerced with int()
        # downstream (SessionManager, subprocess timeouts) — validate here so a
        # bad value surfaces as a config error, not a runtime crash.
        ttl = config.api.auth.session_ttl
        if isinstance(ttl, bool) or not isinstance(ttl, int) or ttl < 1:
            messages.append(
                f"ERROR: api.auth.session_ttl must be an integer >= 1, got {ttl!r}")
        nct = config.nut_control.timeout
        if isinstance(nct, bool) or not isinstance(nct, int) or nct < 1:
            messages.append(
                f"ERROR: nut_control.timeout must be an integer >= 1, got {nct!r}")

        # Fail-closed: UPS control is a write surface, so it must never be
        # reachable without authentication. "Auth disabled" means read-only,
        # full stop — refuse to start rather than expose unauthenticated control.
        if config.nut_control.enabled and not config.api.auth.enabled:
            messages.append(
                "ERROR: nut_control.enabled requires api.auth.enabled — UPS "
                "control endpoints are write operations and must be "
                "authenticated. Enable api.auth and create a user with "
                "'eneru user create' first."
            )

        messages.extend(cls._validate_v61(config))
        return messages

    @staticmethod
    def _validate_v61(config: Config) -> List[str]:
        """Cross-field validation for the v6.1 sections."""
        messages: List[str] = []

        # Self-test is a scheduled write surface. Validate the EFFECTIVE config
        # per UPS (per-UPS override else global) against the EFFECTIVE
        # nut_control, so a per-UPS-narrowed allowlist or a global command that
        # isn't in a group's own allowlist is caught — a scheduled test can never
        # be a back door around the v6.0 control allowlist.
        for group in config.ups_groups:
            st = getattr(group, "self_test", None) or config.self_test
            if not st.enabled:
                continue
            name = group.ups.name
            nc = group.nut_control or config.nut_control
            if not nc.enabled:
                messages.append(
                    f"ERROR: self_test for UPS '{name}' requires "
                    "nut_control.enabled (per-UPS or global) — self-tests issue "
                    "a UPS command via NUT control.")
            if not config.api.auth.enabled:
                messages.append(
                    f"ERROR: self_test for UPS '{name}' requires "
                    "api.auth.enabled — a scheduled self-test is privileged.")
            if st.command and st.command not in nc.allowed_commands:
                messages.append(
                    f"ERROR: self_test.command '{st.command}' for UPS '{name}' "
                    "is not in nut_control.allowed_commands — a scheduled test "
                    "must not bypass the control allowlist. Add it to "
                    "nut_control.allowed_commands.")

        # Battery-health numeric fields must be numbers (a quoted/typo'd YAML
        # value would otherwise blow up the runtime int()/float() coercions).
        def _check_num(label, val, *, allow_none=True, minimum=None):
            if val is None:
                if not allow_none:
                    messages.append(f"ERROR: {label} must be set to a number")
                return
            if isinstance(val, bool) or not isinstance(val, (int, float)):
                messages.append(f"ERROR: {label} must be a number, got {val!r}")
            elif minimum is not None and val < minimum:
                messages.append(f"ERROR: {label} must be >= {minimum}, got {val!r}")

        for label_prefix, bh in (
            ("battery_health", config.battery_health),
            *(((f"battery_health (UPS '{g.ups.name}')"),
               getattr(g, "battery_health", None))
              for g in config.ups_groups),
        ):
            if bh is None:
                continue
            _check_num(f"{label_prefix}.nominal_runtime_seconds",
                       bh.nominal_runtime_seconds, minimum=0)
            _check_num(f"{label_prefix}.expected_life_years",
                       bh.expected_life_years, minimum=0)
            _check_num(f"{label_prefix}.update_interval",
                       bh.update_interval, minimum=1)

        # Energy cost: cost_per_kwh, when set, must be a non-negative number.
        # None/unset is valid and disables cost tracking entirely (B3).
        cpk = config.energy.cost_per_kwh
        if cpk is not None:
            if isinstance(cpk, bool) or not isinstance(cpk, (int, float)) or cpk < 0:
                messages.append(
                    f"ERROR: energy.cost_per_kwh must be a non-negative number "
                    f"or unset, got {cpk!r}")
        npw = config.energy.nominal_power
        if npw is not None:
            if isinstance(npw, bool) or not isinstance(npw, (int, float)) or npw <= 0:
                messages.append(
                    f"ERROR: energy.nominal_power must be a positive number "
                    f"or unset, got {npw!r}")

        return messages
