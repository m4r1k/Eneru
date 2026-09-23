"""Option catalog for `eneru config`: every key, its type, default and meaning.

ELI5: the config file is a cockpit with hundreds of switches. This module is
the laminated card next to each switch: what it's called, what position it
ships in, and -- in plain words -- what happens to the plane when you flip it.
The TUI reads the card to render each row, validate what you type, and write
the explanation as a comment above any key it adds to your file.

Defaults are NOT duplicated here: they are read from the loader's own
dataclasses (``eneru.config``), so the card can never disagree with what the
daemon does when a key is omitted. ``tests/test_config_catalog.py`` fails when
a dataclass grows a field that has no card, so a new option can't ship without
an explanation.

``tier="basic"`` marks the options the guided (basic) mode shows; advanced mode
shows everything.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple, Union

from eneru.actions import REMOTE_ACTIONS
from eneru.config import (
    APIConfig,
    AuthConfig,
    BatteryHealthConfig,
    BatteryReplacementConfig,
    BehaviorConfig,
    ConnectionLossGracePeriodConfig,
    ContainersConfig,
    DepletionConfig,
    EnergyConfig,
    ExtendedTimeConfig,
    FilesystemsConfig,
    LocalShutdownConfig,
    LoggingConfig,
    MQTTConfig,
    NotificationsConfig,
    NutControlConfig,
    PrometheusConfig,
    RedundancyGroupConfig,
    RemoteHealthConfig,
    RemoteServerConfig,
    ReportsConfig,
    SelfTestConfig,
    StatsConfig,
    StatsRetentionConfig,
    SyslogConfig,
    TriggersConfig,
    UnmountConfig,
    UPSConfig,
    VMConfig,
)

BASIC = "basic"
ADVANCED = "advanced"

# Option kinds understood by the editor.
KINDS = ("bool", "int", "float", "str", "secret", "choice", "tristate",
         "list")


@dataclass(frozen=True)
class Option:
    """One scalar (or list-of-scalars) key."""

    key: str
    kind: str
    help: str
    default: Any = None
    choices: Tuple[str, ...] = ()
    nullable: bool = False
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    tier: str = ADVANCED
    # Example shown when the value is empty (e.g. "UPS@192.168.1.10").
    example: str = ""
    # list kind: choices for items (empty = free text).
    item_choices: Tuple[str, ...] = ()


@dataclass(frozen=True)
class Section:
    """A mapping of options / nested sections / lists."""

    key: str
    title: str
    help: str
    children: Tuple[Any, ...] = ()
    tier: str = ADVANCED
    # For list items that may be a bare scalar (compose file, mount): the
    # key that the scalar form stands for ("path").
    scalar_key: str = ""
    # Which child names a list item in the UI.
    label_key: str = "name"
    # Editor actions offered on this section's page ("test_ups", ...).
    actions: Tuple[str, ...] = ()


@dataclass(frozen=True)
class ListSection:
    """A list of mappings (remote servers, UPS entries, ...)."""

    key: str
    title: str
    help: str
    item: Section = field(default_factory=lambda: Section("", "", ""))
    tier: str = ADVANCED
    # A template for new items (plain dict) -- safe, opinionated defaults.
    new_item: Tuple[Tuple[str, Any], ...] = ()


Node = Union[Option, Section, ListSection]


def _d(obj: Any, attr: str) -> Any:
    return getattr(obj, attr)


# Dataclass instances: the single source of truth for defaults.
_UPS = UPSConfig()
_CLGP = ConnectionLossGracePeriodConfig()
_TRG = TriggersConfig()
_DEP = DepletionConfig()
_EXT = ExtendedTimeConfig()
_BEH = BehaviorConfig()
_LOG = LoggingConfig()
_SYS = SyslogConfig()
_API = APIConfig()
_AUTH = AuthConfig()
_PROM = PrometheusConfig()
_RH = RemoteHealthConfig()
_MQTT = MQTTConfig()
_NC = NutControlConfig()
_BH = BatteryHealthConfig()
_BR = BatteryReplacementConfig()
_ST = SelfTestConfig()
_REP = ReportsConfig()
_EN = EnergyConfig()
_NOT = NotificationsConfig()
_VM = VMConfig()
_CT = ContainersConfig()
_UM = UnmountConfig()
_FS = FilesystemsConfig()
_RS = RemoteServerConfig()
_LS = LocalShutdownConfig()
_STATS = StatsConfig()
_RET = StatsRetentionConfig()
_RG = RedundancyGroupConfig()

SUPPRESSIBLE_EVENTS = (
    "POWER_RESTORED", "VOLTAGE_NORMALIZED", "AVR_BOOST_ACTIVE",
    "AVR_TRIM_ACTIVE", "AVR_INACTIVE", "BYPASS_MODE_INACTIVE",
    "OVERLOAD_RESOLVED", "CONNECTION_RESTORED", "VOLTAGE_AUTODETECT_MISMATCH",
    "VOLTAGE_FLAP_SUPPRESSED",
)
REPORT_FIELDS = ("events", "battery_health", "self_tests", "energy", "uptime")
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday",
            "saturday", "sunday")

# Presets for remote shutdown commands, shown by the editor's picker.
SHUTDOWN_PRESETS: Tuple[Tuple[str, str], ...] = (
    ("sudo shutdown -h now", "Linux (non-root user with sudo)"),
    ("shutdown -h now", "Linux (root user)"),
    ("sudo -i synoshutdown -s", "Synology DSM"),
    ("sudo poweroff", "Alpine / BusyBox"),
    ("poweroff", "ESXi / XCP-ng (root)"),
    ("sudo systemctl poweroff", "systemd (explicit)"),
)

# ---------------------------------------------------------------------------
# Reusable sections
# ---------------------------------------------------------------------------

CLGP_SECTION = Section(
    "connection_loss_grace_period", "Connection-loss grace period",
    "Rides out brief NUT server blips without alerting. It never delays "
    "safety: losing the UPS connection while ON BATTERY still shuts down "
    "immediately.",
    (
        Option("enabled", "bool",
               "On: a short NUT outage while on mains power waits `duration` "
               "seconds before alerting. Off: alert on the first failed poll.",
               _d(_CLGP, "enabled")),
        Option("duration", "int",
               "Seconds of lost NUT connection (on mains) before the "
               "CONNECTION_LOST notification is sent.",
               _d(_CLGP, "duration"), minimum=0),
        Option("flap_threshold", "int",
               "Warn once the connection has dropped and recovered this many "
               "times within 24 hours: a sign of a flaky network or upsd.",
               _d(_CLGP, "flap_threshold"), minimum=1),
    ))

TRIGGERS_SECTION = Section(
    "triggers", "Shutdown triggers",
    "When Eneru starts the shutdown sequence while the UPS is on battery. "
    "Whichever trigger fires first wins; the UPS's own forced-shutdown (FSD) "
    "signal always triggers immediately.",
    (
        Option("low_battery_threshold", "int",
               "Start shutting down when battery charge falls to this "
               "percentage. Higher = more safety margin, less time on battery.",
               _d(_TRG, "low_battery_threshold"), minimum=0, maximum=100,
               tier=BASIC),
        Option("critical_runtime_threshold", "int",
               "Start shutting down when the UPS estimates this many seconds "
               "of runtime left. Keep it above the time your whole shutdown "
               "sequence needs (the review page compares them).",
               _d(_TRG, "critical_runtime_threshold"), minimum=0, tier=BASIC),
        Option("on_battery_stabilization_delay", "int",
               "Ignore charge/runtime/depletion/time triggers for this many "
               "seconds after switching to battery, because many UPSes report "
               "pessimistic numbers for a moment. FSD and connection loss "
               "still act immediately.",
               _d(_TRG, "on_battery_stabilization_delay"), minimum=0),
        Option("self_test_failure_shutdown_delay", "int",
               "After a failed battery self-test, wait this many seconds into "
               "the NEXT real outage before shutting down. 0 = immediately.",
               _d(_TRG, "self_test_failure_shutdown_delay"), minimum=0),
        Section("depletion", "Battery depletion rate",
                "Shuts down when the battery drains unusually fast (a sign of "
                "a weak battery or heavy load).",
                (
                    Option("window", "int",
                           "Seconds of battery history used to compute the "
                           "drain rate.", _d(_DEP, "window"), minimum=1),
                    Option("critical_rate", "float",
                           "Shut down when charge drops faster than this many "
                           "percent per minute.", _d(_DEP, "critical_rate"),
                           minimum=0),
                    Option("grace_period", "int",
                           "Seconds after power loss before the drain rate "
                           "may trigger (the first minute is noisy).",
                           _d(_DEP, "grace_period"), minimum=0),
                )),
        Section("extended_time", "Time on battery",
                "A wall-clock safety net: shut down after being on battery "
                "for a fixed time, whatever the battery reports.",
                (
                    Option("enabled", "bool",
                           "On: shut down after `threshold` seconds on battery "
                           "even if charge and runtime still look fine.",
                           _d(_EXT, "enabled"), tier=BASIC),
                    Option("threshold", "int",
                           "Seconds on battery before this safety net fires.",
                           _d(_EXT, "threshold"), minimum=1, tier=BASIC),
                ), tier=BASIC),
        Option("voltage_sensitivity", "choice",
               "Voltage warning band around nominal: tight (+/-5%), normal "
               "(+/-10%, EN 50160) or loose (+/-15%, noisy grid/generator). "
               "Warnings only; it never triggers shutdown.",
               _d(_TRG, "voltage_sensitivity"),
               choices=("tight", "normal", "loose")),
    ), tier=BASIC)

VMS_SECTION = Section(
    "virtual_machines", "Virtual machines (libvirt)",
    "Gracefully shuts down running libvirt/KVM VMs on this host with `virsh "
    "shutdown`, force-destroying any still running after `max_wait`.",
    (
        Option("enabled", "bool",
               "On: stop every running libvirt VM first in the shutdown "
               "sequence. Needs `virsh` on this host.",
               _d(_VM, "enabled"), tier=BASIC),
        Option("max_wait", "int",
               "Seconds to wait for VMs to shut down cleanly before they are "
               "forcibly destroyed (like pulling their plug).",
               _d(_VM, "max_wait"), minimum=0, tier=BASIC),
    ), tier=BASIC)

COMPOSE_FILE_SECTION = Section(
    "", "Compose file",
    "A compose stack stopped with `<runtime> compose -f <path> down` before "
    "the remaining containers.",
    (
        Option("path", "str", "Absolute path of the compose file.", "",
               tier=BASIC, example="/opt/app/docker-compose.yml"),
        Option("stop_timeout", "int",
               "Seconds this stack may take to stop; empty = the containers "
               "stop_timeout.", None, nullable=True, minimum=0),
    ), scalar_key="path", label_key="path")

CONTAINERS_SECTION = Section(
    "containers", "Containers (Docker / Podman)",
    "Stops compose stacks (in order), then every remaining container on this "
    "host, so databases flush to disk before power is lost.",
    (
        Option("enabled", "bool",
               "On: stop containers on this host during shutdown.",
               _d(_CT, "enabled"), tier=BASIC),
        Option("runtime", "choice",
               "Which container runtime to drive. auto prefers Podman when "
               "both are installed.", _d(_CT, "runtime"),
               choices=("auto", "docker", "podman"), tier=BASIC),
        Option("stop_timeout", "int",
               "Seconds each container gets to stop gracefully before it is "
               "killed.", _d(_CT, "stop_timeout"), minimum=0, tier=BASIC),
        ListSection("compose_files", "Compose files",
                    "Compose stacks stopped first, in this order (e.g. apps "
                    "before their database).", COMPOSE_FILE_SECTION,
                    tier=BASIC),
        Option("shutdown_all_remaining_containers", "bool",
               "On: after the compose stacks, stop every other running "
               "container too. Off: only the listed stacks are stopped.",
               _d(_CT, "shutdown_all_remaining_containers")),
        Option("include_user_containers", "bool",
               "Podman only: also stop rootless containers of every regular "
               "user (needs root and lingering users).",
               _d(_CT, "include_user_containers")),
    ), tier=BASIC)

MOUNT_SECTION = Section(
    "", "Mount point",
    "A filesystem unmounted before poweroff (network shares first: they hang "
    "if the NAS goes away first).",
    (
        Option("path", "str", "Mount point to unmount.", "", tier=BASIC,
               example="/mnt/nas"),
        Option("options", "str",
               "Extra umount options, e.g. `-l` (lazy) for network mounts "
               "that could hang.", "", nullable=True, tier=BASIC),
    ), scalar_key="path", label_key="path")

FILESYSTEMS_SECTION = Section(
    "filesystems", "Filesystems",
    "Flushes pending writes and unmounts filesystems before poweroff.",
    (
        Option("sync_enabled", "bool",
               "On: flush all pending disk writes (sync) before and at the end "
               "of the sequence. Leave on unless you know why.",
               _d(_FS, "sync_enabled"), tier=BASIC),
        Section("unmount", "Unmount", "Unmount listed filesystems.", (
            Option("enabled", "bool",
                   "On: unmount the listed mount points during shutdown.",
                   _d(_UM, "enabled"), tier=BASIC),
            Option("timeout", "int",
                   "Seconds each umount may take before Eneru moves on.",
                   _d(_UM, "timeout"), minimum=1),
            ListSection("mounts", "Mount points",
                        "Mount points to unmount, in order.", MOUNT_SECTION,
                        tier=BASIC),
        ), tier=BASIC),
    ), tier=BASIC)

PRE_SHUTDOWN_SECTION = Section(
    "", "Pre-shutdown step",
    "One step run over SSH before the final shutdown command. Use either a "
    "predefined `action` (Eneru knows how to test it) or a custom `command` "
    "(only checked for presence, never run by the checker).",
    (
        Option("action", "choice",
               "Predefined, tested step: stop containers/VMs/compose stacks, "
               "sync or unmount on the remote. Leave empty for a custom "
               "command.", None, nullable=True,
               choices=tuple(sorted(REMOTE_ACTIONS)), tier=BASIC),
        Option("command", "str",
               "Custom shell command run on the server. With use_sudo it runs "
               "as `sudo -n <command>` (only the first command of a pipeline).",
               None,
               nullable=True, tier=BASIC,
               example="systemctl stop my-service"),
        Option("timeout", "int",
               "Seconds this step may take; empty = the server's "
               "command_timeout.", None, nullable=True, minimum=0),
        Option("path", "str", "Compose file path (stop_compose only).", None,
               nullable=True),
        ListSection("mounts", "Mounts (unmount_filesystems only)",
                    "Remote mount points to unmount.", MOUNT_SECTION,
                    tier=BASIC),
    ), label_key="action")

REMOTE_SERVER_SECTION = Section(
    "", "Remote server",
    "A machine Eneru shuts down over SSH (NAS, hypervisor, other servers). "
    "Eneru logs in with a key (never a password), optionally runs "
    "pre-shutdown steps, then the shutdown command.",
    (
        Option("name", "str", "Label used in logs, notifications and the "
               "dashboard.", "", tier=BASIC, example="Synology NAS"),
        Option("enabled", "bool",
               "Off keeps the entry but skips it entirely (no health checks, "
               "no shutdown).", _d(_RS, "enabled"), tier=BASIC),
        Option("host", "str", "Hostname or IP address.", "", tier=BASIC,
               example="192.168.1.20"),
        Option("user", "str",
               "SSH user. Root, or a user with NOPASSWD sudo for the shutdown "
               "tools (see use_sudo).", "", tier=BASIC, example="root"),
        Option("ssh_key_path", "str",
               "Private key used to log in. Empty = the SSH client's default "
               "keys for the user running Eneru.", None, nullable=True,
               tier=BASIC, example="/root/.ssh/id_ed25519"),
        Option("use_sudo", "bool",
               "On: run the built-in actions, custom commands and the shutdown "
               "command through `sudo -n` (non-interactive sudo). Needs NOPASSWD "
               "sudoers rules; the checker verifies them with `sudo -n -l`.",
               _d(_RS, "use_sudo"), tier=BASIC),
        Option("shutdown_command", "str",
               "Final command that powers the machine off. Pick a preset "
               "(Linux, Synology, ...) or type your own.",
               _d(_RS, "shutdown_command"), tier=BASIC,
               choices=tuple(p for p, _ in SHUTDOWN_PRESETS)),
        Option("shutdown_order", "int",
               "Phase number. Lower phases shut down first; servers with the "
               "same number shut down in parallel (e.g. 1 = compute, 2 = "
               "storage, 3 = network). Empty = legacy `parallel` behavior.",
               None, nullable=True, minimum=1, tier=BASIC),
        ListSection("pre_shutdown_commands", "Pre-shutdown steps",
                    "Steps run on this server, in order, before the shutdown "
                    "command.", PRE_SHUTDOWN_SECTION, tier=BASIC),
        Option("connect_timeout", "int",
               "Seconds to wait for the SSH connection.",
               _d(_RS, "connect_timeout"), minimum=1),
        Option("command_timeout", "int",
               "Default seconds each remote command may take.",
               _d(_RS, "command_timeout"), minimum=1),
        Option("ssh_options", "list",
               "Extra ssh options such as `-o Port=2222`. Eneru already uses "
               "StrictHostKeyChecking=accept-new (learn the host key once, "
               "refuse if it changes).", []),
        Option("parallel", "tristate",
               "Legacy ordering: false = shut down before the parallel batch. "
               "Mutually exclusive with shutdown_order.", None, nullable=True),
        Option("shutdown_safety_margin", "int",
               "Extra seconds Eneru waits for this server's shutdown thread "
               "(SSH setup, slow RAID flush). 0 disables the margin.",
               _d(_RS, "shutdown_safety_margin"), minimum=0),
        Option("is_host_loopback", "bool",
               "Container deployments only: this entry is SSH back to the "
               "host that runs the Eneru container, used to stop the host's "
               "VMs/containers and power it off.",
               _d(_RS, "is_host_loopback")),
        Option("host_identity_command", "str",
               "Loopback only: harmless command whose output proves the SSH "
               "target is really this container's host.",
               _d(_RS, "host_identity_command")),
        Option("expected_host_identity", "str",
               "Loopback only: expected output of host_identity_command. "
               "Auto-filled from a bind-mounted /etc/machine-id.", None,
               nullable=True),
    ), actions=("test_remote",))

REMOTE_SERVERS_LIST = ListSection(
    "remote_servers", "Remote servers",
    "Machines shut down over SSH when this group's shutdown starts.",
    REMOTE_SERVER_SECTION, tier=BASIC,
    new_item=(("name", "New server"), ("enabled", True), ("host", ""),
              ("user", "root"), ("shutdown_command", "shutdown -h now")))

NUT_CONTROL_OVERRIDE = Section(
    "nut_control", "NUT login (this UPS)",
    "Credentials for this UPS's upsd, used to list and send instant commands "
    "(self-test, beeper). Overrides the global nut_control for this UPS only.",
    (
        Option("username", "str",
               "A user from upsd.users on the NUT server.", _d(_NC, "username"),
               tier=BASIC),
        Option("password", "secret", "That user's password.",
               _d(_NC, "password"), tier=BASIC),
        Option("allowed_commands", "list",
               "Instant commands the dashboard/API may send to this UPS.", []),
        Option("allowed_variables", "list",
               "Writable variables (upsrw) the API may change. Keep empty "
               "unless you know the risk.", []),
        Option("timeout", "int", "Seconds per NUT command.",
               _d(_NC, "timeout"), minimum=1),
    ), tier=BASIC)

BATTERY_HEALTH_CHILDREN: Tuple[Any, ...] = (
    Option("enabled", "bool",
           "On: compute a 0-100 battery health score and predict when the "
           "battery needs replacing.", _d(_BH, "enabled")),
    Option("update_interval", "int", "Seconds between score updates.",
           _d(_BH, "update_interval"), minimum=1),
    Option("nominal_runtime_seconds", "int",
           "Expected full-charge runtime under load. Empty = learn it at the "
           "first 100% charge reading.", None, nullable=True, minimum=1),
    Option("battery_install_date", "str",
           "When this battery was installed (YYYY-MM-DD). Empty = the age part "
           "of the score is unavailable.", None, nullable=True,
           example="2024-05-01"),
    Option("expected_life_years", "float",
           "Expected battery life in years (lead-acid: 3-5).",
           _d(_BH, "expected_life_years"), minimum=1),
    Option("warn_score", "float",
           "Warn once when the score drops below this. Empty disables.",
           _d(_BH, "warn_score"), nullable=True, minimum=0, maximum=100),
    Option("critical_score", "float",
           "Escalate when the score drops below this (must be below "
           "warn_score). Empty disables.", _d(_BH, "critical_score"),
           nullable=True, minimum=0, maximum=100),
    Section("replacement", "Replacement prediction",
            "Trend-based warning before the battery wears out.", (
                Option("threshold_score", "float",
                       "Score at which the battery counts as due.",
                       _d(_BR, "threshold_score"), minimum=0, maximum=100),
                Option("horizon_days", "int",
                       "Warn when the due date is within this many days.",
                       _d(_BR, "horizon_days"), minimum=1),
                Option("min_history_days", "int",
                       "Days of history needed before predicting.",
                       _d(_BR, "min_history_days"), minimum=1),
            )),
)

SELF_TEST_CHILDREN: Tuple[Any, ...] = (
    Option("enabled", "bool",
           "On: Eneru ISSUES a battery self-test on the schedule below. Needs "
           "API auth. Results of tests the UPS runs itself are recorded "
           "either way.", _d(_ST, "enabled")),
    Option("schedule", "str",
           "daily, weekly, monthly, or `every <N>d/h/m`.",
           _d(_ST, "schedule"), example="every 30d"),
    Option("time", "str", "Wall-clock time (HH:MM) for calendar schedules.",
           _d(_ST, "time")),
    Option("command", "str",
           "Instant command to issue; must be listed by `upscmd -l` (APC "
           "often uses test.battery.start.quick). The UPS test checks it.",
           _d(_ST, "command")),
    Option("result_poll_after", "int",
           "Seconds after issuing before reading the result.",
           _d(_ST, "result_poll_after"), minimum=1),
)

ENERGY_OVERRIDE = Section(
    "energy", "Energy (this UPS)",
    "Per-UPS tariff and watt rating; other energy settings are global.", (
        Option("cost_per_kwh", "float",
               "Price per kWh for this UPS. Empty inherits the global value.",
               None, nullable=True, minimum=0),
        Option("nominal_power", "float",
               "Rated WATTS (not VA) of this UPS, used to estimate power from "
               "load %. Empty = use what NUT reports.", None, nullable=True,
               minimum=1),
    ))

UPS_COMMON: Tuple[Any, ...] = (
    Option("name", "str",
           "NUT identifier NAME@HOST[:PORT]. NAME is the UPS section in the "
           "NUT server's ups.conf (not a login user). `eneru config check` "
           "verifies it exists.",
           _d(_UPS, "name"), tier=BASIC, example="ups@192.168.1.10"),
    Option("display_name", "str",
           "Friendly name for logs, notifications and the dashboard.",
           None, nullable=True, tier=BASIC, example="Rack UPS"),
    Option("check_interval", "int",
           "Seconds between UPS polls. 1 reacts fastest to power events.",
           _d(_UPS, "check_interval"), minimum=1),
    Option("max_stale_data_tolerance", "int",
           "Failed/stale polls tolerated before the connection counts as "
           "lost.", _d(_UPS, "max_stale_data_tolerance"), minimum=1),
    CLGP_SECTION,
)

UPS_LEGACY_SECTION = Section(
    "ups", "UPS", "The UPS that powers this host (single-UPS layout).",
    UPS_COMMON, tier=BASIC, actions=("test_ups",))

UPS_ENTRY_SECTION = Section(
    "", "UPS",
    "One UPS watched by this Eneru instance, with the resources it protects.",
    UPS_COMMON + (
        Option("is_local", "bool",
               "On: this UPS powers the machine running Eneru, so its "
               "shutdown also stops local VMs/containers and powers this host "
               "off. At most one UPS (or redundancy group) may be local.",
               False, tier=BASIC),
        NUT_CONTROL_OVERRIDE,
        TRIGGERS_SECTION,
        REMOTE_SERVERS_LIST,
        VMS_SECTION,
        CONTAINERS_SECTION,
        FILESYSTEMS_SECTION,
        Section("battery_health", "Battery health (this UPS)",
                "Per-UPS battery facts; unset keys inherit the global block.",
                BATTERY_HEALTH_CHILDREN),
        Section("self_test", "Self-test (this UPS)",
                "Per-UPS self-test command/schedule.", SELF_TEST_CHILDREN),
        ENERGY_OVERRIDE,
    ), actions=("test_ups",))

UPS_LIST = ListSection(
    "ups", "UPS list", "Every UPS this Eneru instance watches.",
    UPS_ENTRY_SECTION, tier=BASIC,
    new_item=(("name", "ups@localhost"), ("is_local", False)))

REDUNDANCY_SECTION = Section(
    "", "Redundancy group",
    "Resources fed by several UPSes (dual-PSU servers). They shut down only "
    "when too few of the source UPSes are still healthy.",
    (
        Option("name", "str", "Unique group label.", "", tier=BASIC,
               example="dual-psu-rack"),
        Option("ups_sources", "list",
               "UPS names (exactly as in the `ups:` list) feeding these "
               "resources.", [], tier=BASIC),
        Option("min_healthy", "int",
               "Shut down when fewer than this many sources are healthy. For "
               "2 UPSes, 1 = only when both fail.", _d(_RG, "min_healthy"),
               minimum=1, tier=BASIC),
        Option("degraded_counts_as", "choice",
               "How a UPS with a warning (voltage, AVR) counts.",
               _d(_RG, "degraded_counts_as"), choices=("healthy", "critical")),
        Option("unknown_counts_as", "choice",
               "How a UPS that stopped answering counts (critical = "
               "fail-safe).", _d(_RG, "unknown_counts_as"),
               choices=("critical", "degraded", "healthy")),
        Option("is_local", "bool",
               "On: these UPSes power the machine running Eneru.", False),
        TRIGGERS_SECTION,
        REMOTE_SERVERS_LIST,
        VMS_SECTION,
        CONTAINERS_SECTION,
        FILESYSTEMS_SECTION,
    ))

REDUNDANCY_LIST = ListSection(
    "redundancy_groups", "Redundancy groups",
    "Quorum-based groups for dual-fed equipment.", REDUNDANCY_SECTION,
    new_item=(("name", "redundant-rack"), ("ups_sources", []),
              ("min_healthy", 1)))

# ---------------------------------------------------------------------------
# Top-level sections
# ---------------------------------------------------------------------------

BEHAVIOR_SECTION = Section(
    "behavior", "Behavior", "Global safety switch.", (
        Option("dry_run", "bool",
               "On: every shutdown step is only LOGGED, nothing is stopped or "
               "powered off. Use it to rehearse; turn it off for real "
               "protection.", _d(_BEH, "dry_run"), tier=BASIC),
    ), tier=BASIC)

LOCAL_SHUTDOWN_SECTION = Section(
    "local_shutdown", "Local host poweroff",
    "The last step: powering off the machine that runs Eneru.", (
        Option("enabled", "bool",
               "On: power this host off at the end of the sequence. Off: "
               "drain everything but keep this host running.",
               _d(_LS, "enabled"), tier=BASIC),
        Option("command", "str", "Command that powers this host off.",
               _d(_LS, "command"), tier=BASIC),
        Option("message", "str",
               "Message passed to shutdown(8) and wall.", _d(_LS, "message")),
        Option("trigger_on", "choice",
               "Multi-UPS without a local UPS: any = any group's shutdown also "
               "powers this host off; none = this host never powers itself "
               "off (recommended when it has independent power).",
               _d(_LS, "trigger_on"), choices=("any", "none"), tier=BASIC),
        Option("drain_on_local_shutdown", "bool",
               "Multi-UPS: before this host powers off, also drain every other "
               "group's resources.", _d(_LS, "drain_on_local_shutdown")),
        Option("wall", "bool",
               "Broadcast a warning to logged-in terminals (wall).",
               _d(_LS, "wall")),
    ), tier=BASIC)

NOTIFICATIONS_SECTION = Section(
    "notifications", "Notifications",
    "Where power events are reported, through Apprise (Discord, Telegram, "
    "ntfy, Slack, e-mail and 100+ more).", (
        Option("urls", "list",
               "Apprise URLs, one per service, e.g. discord://id/token or "
               "ntfy://topic.", [], tier=BASIC),
        Option("title", "str",
               "Optional title prefix; useful with several Eneru instances.",
               None, nullable=True, tier=BASIC),
        Option("enabled", "tristate",
               "Empty = on whenever URLs exist. false mutes everything while "
               "keeping the URLs.", None, nullable=True),
        Option("avatar_url", "str", "Avatar image for Discord/Slack.", None,
               nullable=True),
        Option("timeout", "int", "Seconds per delivery attempt.",
               _d(_NOT, "timeout"), minimum=1),
        Option("retry_interval", "int",
               "First retry delay for a failed send (doubles each time).",
               _d(_NOT, "retry_interval"), minimum=1),
        Option("retry_backoff_max", "int", "Longest delay between retries.",
               _d(_NOT, "retry_backoff_max"), minimum=1),
        Option("max_attempts", "int",
               "Give up after this many attempts; 0 = keep trying (messages "
               "survive long internet outages).", _d(_NOT, "max_attempts"),
               minimum=0),
        Option("max_age_days", "int",
               "Drop undelivered messages older than this; 0 = never.",
               _d(_NOT, "max_age_days"), minimum=0),
        Option("max_pending", "int", "Cap on queued undelivered messages.",
               _d(_NOT, "max_pending"), minimum=1),
        Option("retention_days", "int",
               "Days to keep delivered/cancelled rows for auditing.",
               _d(_NOT, "retention_days"), minimum=0),
        Option("voltage_hysteresis_seconds", "int",
               "Only notify about voltage problems lasting this long; 0 = "
               "immediately. Severe events bypass it.",
               _d(_NOT, "voltage_hysteresis_seconds"), minimum=0),
        Option("suppress", "list",
               "Non-critical events to log but not notify. Safety events "
               "can't be muted.", [], item_choices=SUPPRESSIBLE_EVENTS),
    ), tier=BASIC)

LOGGING_SECTION = Section(
    "logging", "Logging & state files", "Where Eneru writes its files.", (
        Option("file", "str", "Log file; empty disables file logging.",
               _d(_LOG, "file"), nullable=True),
        Option("format", "choice", "text, or json for log pipelines.",
               _d(_LOG, "format"), choices=("text", "json")),
        Option("state_file", "str", "Live status read by `eneru monitor`.",
               _d(_LOG, "state_file")),
        Option("battery_history_file", "str",
               "Rolling history used for the depletion rate.",
               _d(_LOG, "battery_history_file")),
        Option("shutdown_flag_file", "str",
               "Marker preventing a second shutdown while one is running.",
               _d(_LOG, "shutdown_flag_file")),
        Section("syslog", "Syslog forwarding", "Also send logs to syslog.", (
            Option("enabled", "bool", "Forward log lines to syslog.",
                   _d(_SYS, "enabled")),
            Option("address", "str", "/dev/log or a remote syslog host.",
                   _d(_SYS, "address")),
            Option("port", "int", "Remote syslog UDP port.", _d(_SYS, "port"),
                   minimum=1, maximum=65535),
            Option("facility", "str", "Syslog facility (daemon, local0...).",
                   _d(_SYS, "facility")),
        )),
    ))

API_SECTION = Section(
    "api", "HTTP API & dashboard",
    "Embedded API that also serves the browser dashboard.", (
        Option("enabled", "bool", "Start the API/dashboard with the daemon.",
               _d(_API, "enabled")),
        Option("bind", "str",
               "Listen address. 127.0.0.1 = this host only; a LAN address "
               "exposes plain HTTP (use a TLS reverse proxy).",
               _d(_API, "bind")),
        Option("port", "int", "Listen port.", _d(_API, "port"), minimum=1,
               maximum=65535),
        Option("allowed_hosts", "list",
               "Hostnames the API answers to when reached by name (DNS "
               "rebinding guard). IPs and localhost always work.", []),
        Section("auth", "Authentication",
                "Required for any write action (UPS control, self-test, "
                "reload).", (
                    Option("enabled", "tristate",
                           "Empty = auto (on once `eneru user create` made a "
                           "user). true/false forces it.", None,
                           nullable=True),
                    Option("require_for_reads", "bool",
                           "Also require login for read endpoints.",
                           _d(_AUTH, "require_for_reads")),
                    Option("session_ttl", "int",
                           "Dashboard session lifetime in seconds.",
                           _d(_AUTH, "session_ttl"), minimum=1),
                    Option("db_path", "str", "Users/API keys database.",
                           _d(_AUTH, "db_path")),
                )),
    ))

PROMETHEUS_SECTION = Section(
    "prometheus", "Prometheus", "Metrics at /metrics on the API.", (
        Option("enabled", "bool", "Serve Prometheus metrics.",
               _d(_PROM, "enabled")),
    ))

REMOTE_HEALTH_SECTION = Section(
    "remote_health", "Remote health checks",
    "Harmless periodic SSH logins that warn you BEFORE an outage that a "
    "remote server has become unreachable.", (
        Option("enabled", "bool", "Run the periodic SSH checks.",
               _d(_RH, "enabled")),
        Option("startup_check", "bool", "Check every remote at startup.",
               _d(_RH, "startup_check")),
        Option("interval", "int", "Seconds between checks.",
               _d(_RH, "interval"), minimum=60),
        Option("probe_command", "str",
               "Harmless command run over SSH (default: true).",
               _d(_RH, "probe_command")),
        Option("failure_threshold", "int",
               "Consecutive failures before a server counts as failed.",
               _d(_RH, "failure_threshold"), minimum=1),
        Option("notify_on_failure", "bool", "Notify when a server fails.",
               _d(_RH, "notify_on_failure")),
        Option("notify_on_recovery", "bool", "Notify when it recovers.",
               _d(_RH, "notify_on_recovery")),
    ))

MQTT_SECTION = Section(
    "mqtt", "MQTT", "Publish status to an MQTT broker (Home Assistant...).", (
        Option("enabled", "bool", "Publish status snapshots.",
               _d(_MQTT, "enabled")),
        Option("broker", "str",
               "mqtt://host:1883 or mqtts://host:8883 (TLS). Credentials go "
               "in the URL.", _d(_MQTT, "broker"),
               example="mqtts://broker.lan:8883"),
        Option("topic_prefix", "str", "Messages go to <prefix>/status.",
               _d(_MQTT, "topic_prefix")),
        Option("publish_interval", "int", "Seconds between publishes.",
               _d(_MQTT, "publish_interval"), minimum=1),
    ))

NUT_CONTROL_SECTION = Section(
    "nut_control", "UPS control (NUT)",
    "Lets the dashboard/API send commands to the UPS (self-test, beeper). A "
    "write surface: it needs API authentication.", (
        Option("enabled", "bool", "Allow UPS control from the API.",
               _d(_NC, "enabled")),
        Option("username", "str", "upsd.users account with instcmds.",
               _d(_NC, "username"), tier=BASIC),
        Option("password", "secret", "Its password.", _d(_NC, "password"),
               tier=BASIC),
        Option("allowed_commands", "list",
               "Instant commands that may be sent.", []),
        Option("allowed_variables", "list",
               "Writable variables that may be changed (keep empty unless "
               "needed).", []),
        Option("timeout", "int", "Seconds per NUT command.",
               _d(_NC, "timeout"), minimum=1),
    ))

BATTERY_HEALTH_SECTION = Section(
    "battery_health", "Battery health", "Battery score and replacement "
    "prediction (defaults for every UPS).", BATTERY_HEALTH_CHILDREN)

SELF_TEST_SECTION = Section(
    "self_test", "Scheduled self-test",
    "Periodic UPS battery self-test (defaults for every UPS).",
    SELF_TEST_CHILDREN)

REPORTS_SECTION = Section(
    "reports", "Periodic reports",
    "Compact daily/weekly/monthly summaries sent to your notification "
    "services.", (
        Option("enabled", "bool", "Send reports.", _d(_REP, "enabled")),
        Option("daily", "bool", "Daily report (previous day).",
               _d(_REP, "daily")),
        Option("weekly", "bool", "Weekly report.", _d(_REP, "weekly")),
        Option("monthly", "bool", "Monthly report.", _d(_REP, "monthly")),
        Option("time", "str", "Send time (HH:MM).", _d(_REP, "time")),
        Option("weekly_day", "choice", "Day for the weekly report.",
               _d(_REP, "weekly_day"), choices=WEEKDAYS),
        Option("monthly_day", "int", "Day of month for the monthly report.",
               _d(_REP, "monthly_day"), minimum=1, maximum=31),
        Option("include", "list", "Fields included in each report.",
               list(_d(_REP, "include")), item_choices=REPORT_FIELDS),
        Option("format", "choice", "text, or csv (attaches event rows).",
               _d(_REP, "format"), choices=("text", "csv")),
    ))

ENERGY_SECTION = Section(
    "energy", "Energy & cost", "kWh tracking and optional cost.", (
        Option("enabled", "bool", "Track energy use.", _d(_EN, "enabled")),
        Option("cost_per_kwh", "float",
               "Price per kWh; empty disables cost tracking.", None,
               nullable=True, minimum=0),
        Option("currency", "str", "ISO 4217 code (USD, EUR, ...).",
               _d(_EN, "currency")),
        Option("cost_format", "str", "Custom format, e.g. `{value} EUR`.",
               None, nullable=True),
        Option("nominal_power", "float",
               "Rated WATTS used to estimate power from load % when the UPS "
               "doesn't report watts.", None, nullable=True, minimum=1),
    ))

STATISTICS_SECTION = Section(
    "statistics", "Statistics", "Per-UPS SQLite history for graphs/reports.", (
        Option("db_directory", "str", "Directory for the .db files.",
               _d(_STATS, "db_directory")),
        Section("retention", "Retention", "How long each tier is kept.", (
            Option("raw_hours", "int", "Hours of raw 1-second samples.",
                   _d(_RET, "raw_hours"), minimum=1),
            Option("agg_5min_days", "int", "Days of 5-minute averages.",
                   _d(_RET, "agg_5min_days"), minimum=1),
            Option("agg_hourly_days", "int", "Days of hourly averages.",
                   _d(_RET, "agg_hourly_days"), minimum=1),
        )),
    ))

# Root-level (legacy single-UPS) resource sections share the group specs.
ROOT_SECTIONS: Tuple[Node, ...] = (
    BEHAVIOR_SECTION, UPS_LEGACY_SECTION, TRIGGERS_SECTION,
    LOCAL_SHUTDOWN_SECTION, VMS_SECTION, CONTAINERS_SECTION,
    FILESYSTEMS_SECTION, REMOTE_SERVERS_LIST, REDUNDANCY_LIST,
    NOTIFICATIONS_SECTION, LOGGING_SECTION, API_SECTION, PROMETHEUS_SECTION,
    REMOTE_HEALTH_SECTION, MQTT_SECTION, NUT_CONTROL_SECTION,
    BATTERY_HEALTH_SECTION, SELF_TEST_SECTION, REPORTS_SECTION,
    ENERGY_SECTION, STATISTICS_SECTION,
)

# Legacy aliases the loader still accepts but the editor never writes.
LEGACY_KEYS = ("docker", "discord")


def child(node: Union[Section, ListSection], key: str) -> Optional[Node]:
    """Find a direct child spec by YAML key (None below a leaf Option)."""
    if isinstance(node, Option):
        return None
    children = node.item.children if isinstance(node, ListSection) else node.children
    for c in children:
        if c.key == key:
            return c
    return None


def root_section(key: str) -> Optional[Node]:
    if key == "docker":  # legacy alias, edited in place by the TUI
        key = "containers"
    for node in ROOT_SECTIONS:
        if node.key == key:
            return node
    return None


def iter_options(node: Any, prefix: Tuple[str, ...] = ()):
    """Yield (path, Option) for every option under ``node`` (depth-first)."""
    if isinstance(node, Option):
        yield prefix, node
        return
    if isinstance(node, ListSection):
        yield from iter_options(node.item, prefix + ("[]",))
        return
    for c in node.children:
        yield from iter_options(c, prefix + (c.key,))


def has_tier(node: Any, tier: str) -> bool:
    """True if ``node`` (or anything under it) is visible in ``tier``."""
    if tier == ADVANCED:
        return True
    if isinstance(node, Option):
        return node.tier == BASIC
    if node.tier != BASIC:
        return False
    return True


def help_for_path(path: Tuple[Any, ...]) -> Optional[str]:
    """Help text for a concrete YAML path (list indices allowed)."""
    node: Any = None
    for i, key in enumerate(path):
        if isinstance(key, int):
            if isinstance(node, ListSection):
                node = node.item
            continue
        if node is None:
            if key == "ups" and i + 1 < len(path) and isinstance(path[i + 1], int):
                node = UPS_LIST
            else:
                node = root_section(key)
        else:
            node = child(node, key)
        if node is None:
            return None
    return getattr(node, "help", None)


def option_defaults() -> Dict[str, Any]:
    """Flat {dotted.path: default} for docs/tests."""
    out: Dict[str, Any] = {}
    for sec in ROOT_SECTIONS:
        for path, opt in iter_options(sec, (sec.key,)):
            out[".".join(path)] = opt.default
    return out
