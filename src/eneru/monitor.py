#!/usr/bin/env python3
"""
Eneru - Generic UPS Monitoring and Shutdown Management
Monitors UPS status via NUT and triggers configurable shutdown sequences.
https://github.com/m4r1k/Eneru
"""

# Version is set at build time via git describe --tags
# Format: "4.3.0" for tagged releases, "4.3.0-5-gabcdef1" for dev builds
__version__ = "4.7.0-rc0"

import subprocess
import sys
import os
import time
import signal
import logging
import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any, Tuple, List
from collections import deque
from dataclasses import dataclass, field
import threading
import queue

# Optional imports with graceful degradation
try:
    import yaml
    YAML_AVAILABLE = True
except ImportError:
    YAML_AVAILABLE = False

try:
    import apprise
    APPRISE_AVAILABLE = True
except ImportError:
    APPRISE_AVAILABLE = False


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
    def validate_config(cls, config: Config) -> List[str]:
        """Validate configuration and return list of warnings/info messages."""
        messages = []

        # Check Apprise availability
        if config.notifications.enabled and not APPRISE_AVAILABLE:
            messages.append(
                "WARNING: Notifications enabled but apprise package not installed. "
                "Notifications will be disabled. Install with: pip install apprise"
            )

        # Check for legacy Discord configuration
        has_legacy_discord = any(
            'discord://' in url.lower() for url in config.notifications.urls
        )
        if has_legacy_discord:
            messages.append(
                "INFO: Discord webhook detected. Using Apprise for notifications. "
                "Native Discord embeds are now handled via Apprise."
            )

        return messages


# ==============================================================================
# REMOTE ACTION TEMPLATES
# ==============================================================================

# Predefined actions for remote pre-shutdown commands
# {timeout} is replaced with the command timeout in seconds
# {path} is replaced with the compose file path (for stop_compose)
REMOTE_ACTIONS: Dict[str, str] = {
    # Stop all Docker/Podman containers
    "stop_containers": (
        't={timeout}; '
        'docker ps -q | xargs -r docker stop -t $t 2>/dev/null; '
        'podman ps -q | xargs -r podman stop -t $t 2>/dev/null; '
        'true'
    ),

    # Stop libvirt/KVM VMs with graceful shutdown, then force destroy
    "stop_vms": (
        'virsh list --name --state-running | xargs -r -n1 virsh shutdown; '
        'end=$((SECONDS+{timeout})); '
        'while [ $SECONDS -lt $end ] && virsh list --name --state-running | grep -q .; do sleep 1; done; '
        'virsh list --name --state-running | xargs -r -n1 virsh destroy 2>/dev/null; '
        'true'
    ),

    # Stop Proxmox QEMU VMs with graceful shutdown, then force stop
    "stop_proxmox_vms": (
        'qm list | awk \'NR>1 && $3=="running" {{print $1}}\' | xargs -r -n1 qm shutdown --timeout {timeout}; '
        'end=$((SECONDS+{timeout})); '
        'while [ $SECONDS -lt $end ] && qm list | awk \'$3=="running"\' | grep -q .; do sleep 1; done; '
        'qm list | awk \'NR>1 && $3=="running" {{print $1}}\' | xargs -r -n1 qm stop 2>/dev/null; '
        'true'
    ),

    # Stop Proxmox LXC containers with graceful shutdown, then force stop
    "stop_proxmox_cts": (
        'pct list | awk \'NR>1 && $2=="running" {{print $1}}\' | xargs -r -n1 pct shutdown --timeout {timeout}; '
        'end=$((SECONDS+{timeout})); '
        'while [ $SECONDS -lt $end ] && pct list | awk \'$2=="running"\' | grep -q .; do sleep 1; done; '
        'pct list | awk \'NR>1 && $2=="running" {{print $1}}\' | xargs -r -n1 pct stop 2>/dev/null; '
        'true'
    ),

    # Stop XCP-ng/XenServer VMs with graceful shutdown, then force
    "stop_xcpng_vms": (
        'ids=$(xe vm-list power-state=running is-control-domain=false --minimal); '
        '[ -z "$ids" ] && exit 0; '
        'echo "$ids" | tr \',\' \'\\n\' | xargs -r -n1 xe vm-shutdown uuid= 2>/dev/null; '
        'end=$((SECONDS+{timeout})); '
        'while [ $SECONDS -lt $end ]; do '
        'ids=$(xe vm-list power-state=running is-control-domain=false --minimal); '
        '[ -z "$ids" ] && break; sleep 1; done; '
        'xe vm-list power-state=running is-control-domain=false --minimal | tr \',\' \'\\n\' | '
        'xargs -r -n1 xe vm-shutdown uuid= --force 2>/dev/null; '
        'true'
    ),

    # Stop VMware ESXi VMs with graceful shutdown, then force power-off
    "stop_esxi_vms": (
        'for i in $(vim-cmd vmsvc/getallvms 2>/dev/null | awk \'NR>1 {{print $1}}\'); do '
        'vim-cmd vmsvc/power.shutdown $i 2>/dev/null; done; '
        'c=0; while [ $c -lt {timeout} ]; do '
        '[ $(vim-cmd vmsvc/getallvms 2>/dev/null | awk \'NR>1\' | wc -l) -eq 0 ] && break; '
        'pwr=$(vim-cmd vmsvc/getallvms 2>/dev/null | awk \'NR>1 {{print $1}}\' | '
        'while read i; do vim-cmd vmsvc/power.getstate $i 2>/dev/null; done | grep -c "Powered on"); '
        '[ "$pwr" -eq 0 ] && break; sleep 1; c=$((c+1)); done; '
        'for i in $(vim-cmd vmsvc/getallvms 2>/dev/null | awk \'NR>1 {{print $1}}\'); do '
        'vim-cmd vmsvc/power.off $i 2>/dev/null; done; '
        'true'
    ),

    # Stop docker/podman compose stack
    "stop_compose": (
        't={timeout}; '
        'if command -v docker &>/dev/null && docker compose version &>/dev/null; then '
        'docker compose -f "{path}" down -t $t; '
        'elif command -v podman &>/dev/null; then '
        'podman compose -f "{path}" down -t $t; fi; '
        'true'
    ),

    # Sync filesystems
    "sync": 'sync; sync; sleep 2',
}


# ==============================================================================
# STATE TRACKING
# ==============================================================================

@dataclass
class MonitorState:
    """Tracks the current state of the UPS monitor."""
    previous_status: str = ""
    on_battery_start_time: int = 0
    extended_time_logged: bool = False
    voltage_state: str = "NORMAL"
    avr_state: str = "INACTIVE"
    bypass_state: str = "INACTIVE"
    overload_state: str = "INACTIVE"
    connection_state: str = "OK"
    stale_data_count: int = 0
    voltage_warning_low: float = 0.0
    voltage_warning_high: float = 0.0
    nominal_voltage: float = 230.0
    battery_history: deque = field(default_factory=lambda: deque(maxlen=1000))


# ==============================================================================
# LOGGING SETUP
# ==============================================================================

class TimezoneFormatter(logging.Formatter):
    """Custom formatter that includes timezone abbreviation."""

    def format(self, record):
        record.timezone = time.strftime('%Z')
        return super().format(record)


class UPSLogger:
    """Custom logger that handles both file and console output."""

    def __init__(self, log_file: Optional[str], config: Config):
        self.log_file = Path(log_file) if log_file else None
        self.config = config
        self.logger = logging.getLogger("ups-monitor")
        self.logger.setLevel(logging.INFO)

        if self.logger.handlers:
            self.logger.handlers.clear()

        formatter = TimezoneFormatter(
            '%(asctime)s %(timezone)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )

        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(formatter)
        self.logger.addHandler(console_handler)

        if self.log_file:
            try:
                self.log_file.parent.mkdir(parents=True, exist_ok=True)
                file_handler = logging.FileHandler(self.log_file)
                file_handler.setFormatter(formatter)
                self.logger.addHandler(file_handler)
            except PermissionError:
                print(f"Warning: Cannot write to {self.log_file}, logging to console only")

    def log(self, message: str):
        """Log a message with timezone info."""
        self.logger.info(message)


# ==============================================================================
# NOTIFICATION WORKER
# ==============================================================================

class NotificationWorker:
    """Non-blocking notification worker with persistent retry using a background thread.

    This worker ensures that notifications never block the main monitoring loop
    or shutdown sequence. The main thread queues notifications instantly and
    continues with critical operations. The worker thread persistently retries
    failed notifications until they succeed (or the process exits).

    Architecture:
    - Main thread: Queues notifications instantly (non-blocking)
    - Worker thread: Processes queue in FIFO order, retrying each message
      until successful before moving to the next one
    - Apprise handles parallel delivery to multiple backends

    This design ensures:
    1. Zero impact on shutdown operations (main thread never waits)
    2. Guaranteed delivery during transient network issues
    3. Order preservation (FIFO queue, no message skipping)
    4. No message loss during brief outages (e.g., 30-second power blip)
    """

    def __init__(self, config: Config):
        self.config = config
        self._queue: queue.Queue = queue.Queue()
        self._worker_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._apprise_instance: Optional[Any] = None
        self._initialized = False
        self._retry_count = 0  # Track retries for current message (for logging)

    def start(self) -> bool:
        """Initialize Apprise and start the background worker thread."""
        if not self.config.notifications.enabled:
            return False

        if not APPRISE_AVAILABLE:
            return False

        if not self.config.notifications.urls:
            return False

        # Initialize Apprise
        self._apprise_instance = apprise.Apprise()

        for url in self.config.notifications.urls:
            if not self._apprise_instance.add(url):
                print(f"Warning: Failed to add notification URL: {url}")

        if len(self._apprise_instance) == 0:
            print("Warning: No valid notification URLs configured")
            return False

        # Start background worker thread (daemon=True ensures it won't block shutdown)
        self._stop_event.clear()
        self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker_thread.start()
        self._initialized = True

        return True

    def stop(self):
        """Stop the background worker thread gracefully.

        Note: During shutdown, the worker will attempt to send any pending
        notifications. Messages that cannot be delivered before process exit
        will be lost, but this is acceptable since journalctl logs remain
        for forensics.
        """
        if self._worker_thread and self._worker_thread.is_alive():
            # Log pending notifications
            pending = self._queue.qsize()
            in_progress = 1 if self._retry_count > 0 else 0
            total_pending = pending + in_progress
            if total_pending > 0:
                retry_info = f" (current message: retry #{self._retry_count})" if in_progress else ""
                print(f"⚠️ Stopping notification worker with {total_pending} message(s) pending{retry_info}")

            self._stop_event.set()
            # Add sentinel to unblock the queue
            self._queue.put(None)
            # Don't wait too long - we might be shutting down
            self._worker_thread.join(timeout=2)

    def send(self, body: str, notify_type: str = "info", blocking: bool = False):
        """
        Queue a notification for sending.

        Args:
            body: Notification body
            notify_type: One of 'info', 'success', 'warning', 'failure'
            blocking: If True, wait for notification to be sent.
                      NOTE: This should only be used for test notifications,
                      never during shutdown sequences where network may be down.
        """
        if not self._initialized:
            return

        notification = {
            'title': self.config.notifications.title,  # Can be None
            'body': body,
            'notify_type': notify_type,
            'blocking_event': threading.Event() if blocking else None,
        }

        self._queue.put(notification)

        # If blocking, wait for the notification to be processed
        # This should ONLY be used for --test-notifications, never during shutdown
        if blocking and notification['blocking_event']:
            # For blocking calls, use a generous timeout that allows for retries
            max_wait = self.config.notifications.timeout * 3 + 10
            notification['blocking_event'].wait(timeout=max_wait)

    def _worker_loop(self):
        """Background worker that processes the notification queue with persistent retry."""
        while not self._stop_event.is_set():
            try:
                notification = self._queue.get(timeout=1)

                if notification is None:
                    # Sentinel value, exit loop
                    break

                self._send_with_retry(notification)

            except queue.Empty:
                continue
            except Exception:
                # Silently ignore errors - notifications should never crash the monitor
                pass

    def _send_with_retry(self, notification: Dict[str, Any]):
        """Send notification with persistent retry until success or stop signal."""
        self._retry_count = 0
        retry_interval = self.config.notifications.retry_interval

        while not self._stop_event.is_set():
            success = self._send_notification(notification)

            if success:
                self._retry_count = 0
                return

            # Failed - wait and retry
            self._retry_count += 1

            # Use stop_event.wait() instead of time.sleep() so we can interrupt quickly
            if self._stop_event.wait(timeout=retry_interval):
                # Stop was requested during wait
                break

        # If we exit the loop without success (stop requested), reset counter
        self._retry_count = 0
        # Signal completion even on failure (for blocking calls)
        if notification.get('blocking_event'):
            notification['blocking_event'].set()

    def _send_notification(self, notification: Dict[str, Any]) -> bool:
        """Actually send the notification via Apprise.

        Returns:
            True if notification was sent successfully, False otherwise.
        """
        if not self._apprise_instance:
            return False

        try:
            # Map notify_type string to Apprise NotifyType
            type_map = {
                "info": apprise.NotifyType.INFO,
                "success": apprise.NotifyType.SUCCESS,
                "warning": apprise.NotifyType.WARNING,
                "failure": apprise.NotifyType.FAILURE,
            }
            notify_type = type_map.get(notification['notify_type'], apprise.NotifyType.INFO)

            # Build notification parameters
            notify_kwargs = {
                'body': notification['body'],
                'notify_type': notify_type,
            }

            # Only add title if configured (not None/empty)
            if notification.get('title'):
                notify_kwargs['title'] = notification['title']

            # Apprise.notify() returns True if at least one notification succeeded
            success = self._apprise_instance.notify(**notify_kwargs)

            if success:
                # Signal completion for blocking calls
                if notification.get('blocking_event'):
                    notification['blocking_event'].set()

            return success

        except Exception:
            # Network error, DNS failure, etc. - will retry
            return False

    def get_service_count(self) -> int:
        """Return the number of configured notification services."""
        if self._apprise_instance:
            return len(self._apprise_instance)
        return 0

    def get_queue_size(self) -> int:
        """Return the number of pending notifications in the queue."""
        return self._queue.qsize()

    def get_retry_count(self) -> int:
        """Return the current retry count for the message being processed."""
        return self._retry_count


# ==============================================================================
# HELPER FUNCTIONS
# ==============================================================================

def is_numeric(value: Any) -> bool:
    """Check if a value is numeric (int or float)."""
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return True
    if isinstance(value, str):
        try:
            float(value)
            return True
        except (ValueError, TypeError):
            return False
    return False


def run_command(
    cmd: List[str],
    timeout: int = 30,
    capture_output: bool = True
) -> Tuple[int, str, str]:
    """Run a shell command and return (exit_code, stdout, stderr)."""
    try:
        result = subprocess.run(
            cmd,
            capture_output=capture_output,
            text=True,
            timeout=timeout,
            env={**os.environ, 'LC_NUMERIC': 'C'}
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "Command timed out"
    except FileNotFoundError:
        return 127, "", f"Command not found: {cmd[0]}"
    except Exception as e:
        return 1, "", str(e)


def command_exists(cmd: str) -> bool:
    """Check if a command exists in the system PATH."""
    exit_code, _, _ = run_command(["which", cmd])
    return exit_code == 0


def format_seconds(seconds: Any) -> str:
    """Format seconds into a human-readable string."""
    if not is_numeric(seconds):
        return "N/A"
    seconds = int(float(seconds))
    if seconds < 60:
        return f"{seconds}s"
    elif seconds < 3600:
        mins = seconds // 60
        secs = seconds % 60
        return f"{mins}m {secs}s"
    else:
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        return f"{hours}h {minutes}m"


# ==============================================================================
# UPS MONITOR CLASS
# ==============================================================================

class UPSMonitor:
    """Main UPS Monitor class."""

    def __init__(self, config: Config):
        self.config = config
        self.state = MonitorState()
        self.logger: Optional[UPSLogger] = None
        self._shutdown_flag_path = Path(config.logging.shutdown_flag_file)
        self._battery_history_path = Path(config.logging.battery_history_file)
        self._state_file_path = Path(config.logging.state_file)
        self._container_runtime: Optional[str] = None
        self._compose_available: bool = False
        self._notification_worker: Optional[NotificationWorker] = None

    def run(self):
        """Main entry point."""
        try:
            self._initialize()
            self._main_loop()
        except KeyboardInterrupt:
            self._cleanup_and_exit(signal.SIGINT, None)
        except Exception as e:
            self._log_message(f"❌ FATAL ERROR: {e}")
            self._send_notification(
                f"❌ **FATAL ERROR**\nError: {e}",
                self.config.NOTIFY_FAILURE
            )
            raise

    def _initialize(self):
        """Initialize the UPS monitor."""
        signal.signal(signal.SIGTERM, self._cleanup_and_exit)
        signal.signal(signal.SIGINT, self._cleanup_and_exit)

        self.logger = UPSLogger(self.config.logging.file, self.config)

        if self.config.logging.file:
            try:
                Path(self.config.logging.file).touch(exist_ok=True)
            except PermissionError:
                pass

        self._shutdown_flag_path.unlink(missing_ok=True)

        try:
            self._battery_history_path.write_text("")
        except PermissionError:
            self._log_message(f"⚠️ WARNING: Cannot write to {self._battery_history_path}")

        # Initialize notification worker
        self._initialize_notifications()

        self._check_dependencies()

        self._log_message(f"🚀 Eneru v{__version__} starting - monitoring {self.config.ups.name}")
        self._send_notification(
            f"🚀 **Eneru v{__version__} Started**\nMonitoring {self.config.ups.name}",
            self.config.NOTIFY_INFO
        )

        if self.config.behavior.dry_run:
            self._log_message("🧪 *** RUNNING IN DRY-RUN MODE - NO ACTUAL SHUTDOWN WILL OCCUR ***")

        self._log_enabled_features()
        self._wait_for_initial_connection()
        self._initialize_voltage_thresholds()

    def _initialize_notifications(self):
        """Initialize the notification worker."""
        if not self.config.notifications.enabled:
            self._log_message("📢 Notifications: disabled")
            return

        if not APPRISE_AVAILABLE:
            self._log_message("⚠️ WARNING: Notifications enabled but apprise not installed. "
                              "Install with: pip install apprise")
            self.config.notifications.enabled = False
            return

        self._notification_worker = NotificationWorker(self.config)
        if self._notification_worker.start():
            service_count = self._notification_worker.get_service_count()
            self._log_message(f"📢 Notifications: enabled ({service_count} service(s))")
        else:
            self._log_message("⚠️ WARNING: Failed to initialize notifications")
            self.config.notifications.enabled = False

    def _log_enabled_features(self):
        """Log which features are enabled."""
        features = []

        if self.config.virtual_machines.enabled:
            features.append("VMs")
        if self.config.containers.enabled:
            runtime = self.config.containers.runtime
            compose_count = len(self.config.containers.compose_files)
            if runtime == "auto":
                if compose_count > 0:
                    features.append(f"Containers (auto-detect, {compose_count} compose)")
                else:
                    features.append("Containers (auto-detect)")
            else:
                if compose_count > 0:
                    features.append(f"Containers ({runtime}, {compose_count} compose)")
                else:
                    features.append(f"Containers ({runtime})")
        # Filesystem features
        fs_parts = []
        if self.config.filesystems.sync_enabled:
            fs_parts.append("sync")
        if self.config.filesystems.unmount.enabled:
            fs_parts.append(f"unmount ({len(self.config.filesystems.unmount.mounts)} mounts)")
        if fs_parts:
            features.append(f"FS ({', '.join(fs_parts)})")

        enabled_servers = [s for s in self.config.remote_servers if s.enabled]
        if enabled_servers:
            features.append(f"Remote ({len(enabled_servers)} servers)")

        if self.config.local_shutdown.enabled:
            features.append("Local Shutdown")

        self._log_message(f"📋 Enabled features: {', '.join(features) if features else 'None'}")

    def _log_message(self, message: str):
        """Log a message using the logger."""
        if self.logger:
            self.logger.log(message)
        else:
            tz_name = time.strftime('%Z')
            timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            print(f"{timestamp} {tz_name} - {message}")

        # During shutdown, also send log messages as notifications (non-blocking)
        if self._shutdown_flag_path.exists():
            discord_safe_message = message.replace('`', '\\`')
            self._send_notification(
                f"ℹ️ **Shutdown Detail:** {discord_safe_message}",
                self.config.NOTIFY_INFO
            )

    def _send_notification(self, body: str, notify_type: str = "info",
                           blocking: bool = False):
        """Send a notification via the notification worker.

        IMPORTANT: During shutdown sequences, notifications are ALWAYS non-blocking.
        This ensures that network failures (common during power outages) do not
        delay the critical shutdown process. The blocking parameter is only
        honored for non-shutdown scenarios like --test-notifications.

        Args:
            body: Notification body text
            notify_type: One of 'info', 'success', 'warning', 'failure'
            blocking: If True AND not during shutdown, wait for send completion.
                      Ignored during shutdown to prevent delays.
        """
        if not self._notification_worker:
            return

        # Escape @ symbols to prevent Discord mentions (e.g., UPS@192.168.1.1)
        escaped_body = body.replace("@", "@\u200B")  # Zero-width space after @

        # CRITICAL: During shutdown, NEVER block on notifications
        # Network is likely unreliable during power outages
        is_shutdown = self._shutdown_flag_path.exists()
        actual_blocking = blocking and not is_shutdown

        self._notification_worker.send(
            body=escaped_body,
            notify_type=notify_type,
            blocking=actual_blocking
        )

    def _log_power_event(self, event: str, details: str):
        """Log power events with centralized notification logic."""
        self._log_message(f"⚡ POWER EVENT: {event} - {details}")

        try:
            run_command([
                "logger", "-t", "ups-monitor", "-p", "daemon.warning",
                f"⚡ POWER EVENT: {event} - {details}"
            ])
        except Exception:
            pass

        if self._shutdown_flag_path.exists():
            return

        notification: Optional[Tuple[str, str]] = None  # (body, type)

        event_handlers = {
            "ON_BATTERY": (
                f"⚠️ **POWER FAILURE DETECTED!**\nSystem running on battery.\nDetails: {details}",
                self.config.NOTIFY_WARNING
            ),
            "POWER_RESTORED": (
                f"✅ **POWER RESTORED**\nSystem back on line power/charging.\nDetails: {details}",
                self.config.NOTIFY_SUCCESS
            ),
            "BROWNOUT_DETECTED": (
                f"⚠️ **VOLTAGE ISSUE:** {event}\nDetails: {details}",
                self.config.NOTIFY_WARNING
            ),
            "OVER_VOLTAGE_DETECTED": (
                f"⚠️ **VOLTAGE ISSUE:** {event}\nDetails: {details}",
                self.config.NOTIFY_WARNING
            ),
            "AVR_BOOST_ACTIVE": (
                f"⚡ **AVR ACTIVE:** {event}\nDetails: {details}",
                self.config.NOTIFY_WARNING
            ),
            "AVR_TRIM_ACTIVE": (
                f"⚡ **AVR ACTIVE:** {event}\nDetails: {details}",
                self.config.NOTIFY_WARNING
            ),
            "BYPASS_MODE_ACTIVE": (
                f"🚨 **UPS IN BYPASS MODE!**\nNo protection active!\nDetails: {details}",
                self.config.NOTIFY_FAILURE
            ),
            "BYPASS_MODE_INACTIVE": (
                f"✅ **Bypass Mode Inactive**\nProtection restored.\nDetails: {details}",
                self.config.NOTIFY_SUCCESS
            ),
            "OVERLOAD_ACTIVE": (
                f"🚨 **UPS OVERLOAD DETECTED!**\nDetails: {details}",
                self.config.NOTIFY_FAILURE
            ),
            "OVERLOAD_RESOLVED": (
                f"✅ **Overload Resolved**\nDetails: {details}",
                self.config.NOTIFY_SUCCESS
            ),
            "CONNECTION_LOST": (
                f"❌ **ERROR: Connection Lost**\n{details}",
                self.config.NOTIFY_FAILURE
            ),
            "CONNECTION_RESTORED": (
                f"✅ **Connection Restored**\n{details}",
                self.config.NOTIFY_SUCCESS
            ),
        }

        # Skip these events for notifications
        if event in ("VOLTAGE_NORMALIZED", "AVR_INACTIVE"):
            return

        if event in event_handlers:
            notification = event_handlers[event]
        else:
            notification = (
                f"⚡ **Event:** {event}\nDetails: {details}",
                self.config.NOTIFY_INFO
            )

        if notification:
            self._send_notification(*notification)

    def _check_dependencies(self):
        """Check for required and optional dependencies."""
        required_cmds = ["upsc", "sync", "shutdown", "logger"]
        missing = [cmd for cmd in required_cmds if not command_exists(cmd)]

        if missing:
            error_msg = f"❌ FATAL ERROR: Missing required commands: {', '.join(missing)}"
            print(error_msg)
            sys.exit(1)

        # Check optional dependencies based on enabled features
        if self.config.virtual_machines.enabled and not command_exists("virsh"):
            self._log_message("⚠️ WARNING: 'virsh' not found but VM shutdown is enabled. VMs will be skipped.")
            self.config.virtual_machines.enabled = False

        # Container runtime detection
        if self.config.containers.enabled:
            self._container_runtime = self._detect_container_runtime()
            if self._container_runtime:
                self._log_message(f"🐳 Container runtime detected: {self._container_runtime}")
                # Check compose availability if compose_files are configured
                if self.config.containers.compose_files:
                    self._compose_available = self._check_compose_available()
                    if self._compose_available:
                        self._log_message(
                            f"🐳 Compose support: enabled ({self._container_runtime} compose, "
                            f"{len(self.config.containers.compose_files)} file(s))"
                        )
                    else:
                        self._log_message(
                            f"⚠️ WARNING: compose_files configured but '{self._container_runtime} compose' "
                            "not available. Compose shutdown will be skipped."
                        )
            else:
                self._log_message("⚠️ WARNING: No container runtime found. Container shutdown will be skipped.")
                self.config.containers.enabled = False

        enabled_servers = [s for s in self.config.remote_servers if s.enabled]
        if enabled_servers and not command_exists("ssh"):
            self._log_message("⚠️ WARNING: 'ssh' not found but remote servers are configured. Remote shutdown will be skipped.")
            for server in self.config.remote_servers:
                server.enabled = False

    def _detect_container_runtime(self) -> Optional[str]:
        """Detect available container runtime."""
        runtime_config = self.config.containers.runtime.lower()

        if runtime_config == "docker":
            if command_exists("docker"):
                return "docker"
            self._log_message("⚠️ WARNING: Docker specified but not found")
            return None

        elif runtime_config == "podman":
            if command_exists("podman"):
                return "podman"
            self._log_message("⚠️ WARNING: Podman specified but not found")
            return None

        elif runtime_config == "auto":
            if command_exists("podman"):
                return "podman"
            elif command_exists("docker"):
                return "docker"
            return None

        else:
            self._log_message(f"⚠️ WARNING: Unknown container runtime '{runtime_config}'")
            return None

    def _check_compose_available(self) -> bool:
        """Check if compose subcommand is available for the detected runtime."""
        if not self._container_runtime:
            return False

        # Try running 'docker/podman compose version' to check availability
        exit_code, _, _ = run_command(
            [self._container_runtime, "compose", "version"],
            timeout=10
        )
        return exit_code == 0

    def _get_ups_var(self, var_name: str) -> Optional[str]:
        """Get a single UPS variable using upsc."""
        exit_code, stdout, _ = run_command(["upsc", self.config.ups.name, var_name])
        if exit_code == 0:
            return stdout.strip()
        return None

    def _get_all_ups_data(self) -> Tuple[bool, Dict[str, str], str]:
        """Query all UPS data using a single upsc call."""
        exit_code, stdout, stderr = run_command(["upsc", self.config.ups.name])

        if exit_code != 0:
            return False, {}, stderr

        if "Data stale" in stdout or "Data stale" in stderr:
            return False, {}, "Data stale"

        ups_data: Dict[str, str] = {}
        for line in stdout.strip().split('\n'):
            if ':' in line:
                key, value = line.split(':', 1)
                ups_data[key.strip()] = value.strip()

        return True, ups_data, ""

    def _initialize_voltage_thresholds(self):
        """Initialize voltage thresholds dynamically from UPS data."""
        nominal = self._get_ups_var("input.voltage.nominal")
        low_transfer = self._get_ups_var("input.transfer.low")
        high_transfer = self._get_ups_var("input.transfer.high")

        if is_numeric(nominal):
            self.state.nominal_voltage = float(nominal)
        else:
            self.state.nominal_voltage = 230.0

        if is_numeric(low_transfer):
            self.state.voltage_warning_low = float(low_transfer) + 5
        else:
            self.state.voltage_warning_low = self.state.nominal_voltage * 0.9

        if is_numeric(high_transfer):
            self.state.voltage_warning_high = float(high_transfer) - 5
        else:
            self.state.voltage_warning_high = self.state.nominal_voltage * 1.1

        self._log_message(
            f"📊 Voltage Monitoring Active. Nominal: {self.state.nominal_voltage}V. "
            f"Low Warning: {self.state.voltage_warning_low}V. "
            f"High Warning: {self.state.voltage_warning_high}V."
        )

    def _wait_for_initial_connection(self):
        """Wait for initial connection to NUT server."""
        self._log_message(f"⏳ Checking initial connection to {self.config.ups.name}...")

        max_wait = 30
        wait_interval = 5
        time_waited = 0
        connected = False

        while time_waited < max_wait:
            success, _, _ = self._get_all_ups_data()
            if success:
                connected = True
                self._log_message("✅ Initial connection successful.")
                break
            time.sleep(wait_interval)
            time_waited += wait_interval

        if not connected:
            self._log_message(
                f"⚠️ WARNING: Failed to connect to {self.config.ups.name} "
                f"within {max_wait}s. Proceeding, but voltage thresholds may default."
            )

    def _calculate_depletion_rate(self, current_battery: str) -> float:
        """Calculate battery depletion rate based on history."""
        current_time = int(time.time())

        if not is_numeric(current_battery):
            return 0.0

        current_battery_float = float(current_battery)
        cutoff_time = current_time - self.config.triggers.depletion.window

        self.state.battery_history = deque(
            [(ts, bat) for ts, bat in self.state.battery_history if ts >= cutoff_time],
            maxlen=1000
        )
        self.state.battery_history.append((current_time, current_battery_float))

        try:
            temp_file = self._battery_history_path.with_suffix('.tmp')
            with open(temp_file, 'w') as f:
                for ts, bat in self.state.battery_history:
                    f.write(f"{ts}:{bat}\n")
            temp_file.replace(self._battery_history_path)
        except Exception:
            pass

        if len(self.state.battery_history) < 30:
            return 0.0

        oldest_time, oldest_battery = self.state.battery_history[0]
        time_diff = current_time - oldest_time

        if time_diff > 0:
            battery_diff = oldest_battery - current_battery_float
            rate = (battery_diff / time_diff) * 60
            return round(rate, 2)

        return 0.0

    def _save_state(self, ups_data: Dict[str, str]):
        """Save current UPS state to file."""
        state_content = (
            f"STATUS={ups_data.get('ups.status', '')}\n"
            f"BATTERY={ups_data.get('battery.charge', '')}\n"
            f"RUNTIME={ups_data.get('battery.runtime', '')}\n"
            f"LOAD={ups_data.get('ups.load', '')}\n"
            f"INPUT_VOLTAGE={ups_data.get('input.voltage', '')}\n"
            f"OUTPUT_VOLTAGE={ups_data.get('output.voltage', '')}\n"
            f"TIMESTAMP={datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        )
        try:
            temp_file = self._state_file_path.with_suffix('.tmp')
            temp_file.write_text(state_content)
            temp_file.replace(self._state_file_path)
        except Exception:
            pass

    # ==========================================================================
    # SHUTDOWN SEQUENCE
    # ==========================================================================

    def _shutdown_vms(self):
        """Shutdown all libvirt virtual machines."""
        if not self.config.virtual_machines.enabled:
            return

        self._log_message("🖥️ Shutting down all libvirt virtual machines...")

        if not command_exists("virsh"):
            self._log_message("  ℹ️ virsh not available, skipping VM shutdown")
            return

        exit_code, stdout, _ = run_command(["virsh", "list", "--name", "--state-running"])
        if exit_code != 0:
            self._log_message("  ⚠️ Failed to get VM list")
            return

        running_vms = [vm.strip() for vm in stdout.strip().split('\n') if vm.strip()]

        if not running_vms:
            self._log_message("  ℹ️ No running VMs found")
            return

        for vm in running_vms:
            self._log_message(f"  ⏹️ Shutting down VM: {vm}")
            if self.config.behavior.dry_run:
                self._log_message(f"  🧪 [DRY-RUN] Would shutdown VM: {vm}")
            else:
                exit_code, stdout, stderr = run_command(["virsh", "shutdown", vm])
                if stdout.strip():
                    self._log_message(f"    {stdout.strip()}")

        if self.config.behavior.dry_run:
            return

        max_wait = self.config.virtual_machines.max_wait
        self._log_message(f"  ⏳ Waiting up to {max_wait}s for VMs to shutdown gracefully...")
        wait_interval = 5
        time_waited = 0
        remaining_vms: List[str] = []

        while time_waited < max_wait:
            exit_code, stdout, _ = run_command(["virsh", "list", "--name", "--state-running"])
            still_running = set(vm.strip() for vm in stdout.strip().split('\n') if vm.strip())
            remaining_vms = [vm for vm in running_vms if vm in still_running]

            if not remaining_vms:
                self._log_message(f"  ✅ All VMs stopped gracefully after {time_waited}s.")
                break

            self._log_message(f"  🕒 Still waiting for: {' '.join(remaining_vms)} (Waited {time_waited}s)")
            time.sleep(wait_interval)
            time_waited += wait_interval

        if remaining_vms:
            self._log_message("  ⚠️ Timeout reached. Force destroying remaining VMs.")
            for vm in remaining_vms:
                self._log_message(f"  ⚡ Force destroying VM: {vm}")
                run_command(["virsh", "destroy", vm])

        self._log_message("  ✅ All VMs shutdown complete")

    def _shutdown_compose_stacks(self):
        """Shutdown docker/podman compose stacks in order (best effort)."""
        if not self._compose_available:
            return

        if not self.config.containers.compose_files:
            return

        runtime = self._container_runtime
        runtime_display = runtime.capitalize()

        self._log_message(
            f"🐳 Stopping {runtime_display} Compose stacks "
            f"({len(self.config.containers.compose_files)} file(s))..."
        )

        for compose_file in self.config.containers.compose_files:
            file_path = compose_file.path
            if not file_path:
                continue

            # Determine timeout: per-file or global
            timeout = compose_file.stop_timeout
            if timeout is None:
                timeout = self.config.containers.stop_timeout

            # Check if file exists (best effort - warn if not)
            if not Path(file_path).exists():
                self._log_message(f"  ⚠️ Compose file not found: {file_path} (skipping)")
                continue

            self._log_message(f"  ➡️ Stopping: {file_path} (timeout: {timeout}s)")

            if self.config.behavior.dry_run:
                self._log_message(
                    f"  🧪 [DRY-RUN] Would execute: {runtime} compose -f {file_path} down"
                )
                continue

            # Run compose down
            compose_cmd = [runtime, "compose", "-f", file_path, "down"]
            exit_code, stdout, stderr = run_command(compose_cmd, timeout=timeout + 30)

            if exit_code == 0:
                self._log_message(f"  ✅ {file_path} stopped successfully")
            elif exit_code == 124:
                self._log_message(
                    f"  ⚠️ {file_path} compose down timed out after {timeout}s (continuing)"
                )
            else:
                error_msg = stderr.strip() if stderr.strip() else f"exit code {exit_code}"
                self._log_message(f"  ⚠️ {file_path} compose down failed: {error_msg} (continuing)")

        self._log_message("  ✅ Compose stacks shutdown complete")

    def _shutdown_containers(self):
        """Stop all containers using detected runtime (Docker/Podman).

        Execution order:
        1. Shutdown compose stacks (best effort, in order)
        2. Shutdown all remaining containers (if shutdown_all_remaining_containers is True)
        """
        if not self.config.containers.enabled:
            return

        if not self._container_runtime:
            return

        runtime = self._container_runtime
        runtime_display = runtime.capitalize()

        # Phase 1: Shutdown compose stacks first (best effort)
        self._shutdown_compose_stacks()

        # Phase 2: Shutdown all remaining containers
        if not self.config.containers.shutdown_all_remaining_containers:
            self._log_message(f"🐳 Skipping remaining {runtime_display} container shutdown (disabled)")
            return

        self._log_message(f"🐳 Stopping all remaining {runtime_display} containers...")

        # Get list of running containers
        exit_code, stdout, _ = run_command([runtime, "ps", "-q"])
        if exit_code != 0:
            self._log_message(f"  ⚠️ Failed to get {runtime_display} container list")
            return

        container_ids = [cid.strip() for cid in stdout.strip().split('\n') if cid.strip()]

        if not container_ids:
            self._log_message(f"  ℹ️ No running {runtime_display} containers found")
        else:
            if self.config.behavior.dry_run:
                exit_code, stdout, _ = run_command([runtime, "ps", "--format", "{{.Names}}"])
                names = stdout.strip().replace('\n', ' ')
                self._log_message(f"  🧪 [DRY-RUN] Would stop {runtime_display} containers: {names}")
            else:
                # Stop containers with timeout
                stop_cmd = [runtime, "stop", "-t", str(self.config.containers.stop_timeout)]
                stop_cmd.extend(container_ids)
                run_command(stop_cmd, timeout=self.config.containers.stop_timeout + 30)
                self._log_message(f"  ✅ {runtime_display} containers stopped")

        # Handle Podman rootless containers if configured
        if runtime == "podman" and self.config.containers.include_user_containers:
            self._shutdown_podman_user_containers()

    def _shutdown_podman_user_containers(self):
        """Stop Podman containers running as non-root users."""
        self._log_message("  🔍 Checking for rootless Podman containers...")

        if self.config.behavior.dry_run:
            self._log_message("  🧪 [DRY-RUN] Would stop rootless Podman containers for all users")
            return

        # Get list of users with active Podman containers
        # This requires loginctl and users with linger enabled
        exit_code, stdout, _ = run_command(["loginctl", "list-users", "--no-legend"])
        if exit_code != 0:
            self._log_message("  ⚠️ Failed to list users for rootless container check")
            return

        for line in stdout.strip().split('\n'):
            if not line.strip():
                continue

            parts = line.split()
            if len(parts) >= 2:
                uid = parts[0]
                username = parts[1]

                # Skip system users (UID < 1000)
                try:
                    if int(uid) < 1000:
                        continue
                except ValueError:
                    continue

                # Check for running containers as this user
                exit_code, stdout, _ = run_command([
                    "sudo", "-u", username,
                    "podman", "ps", "-q"
                ], timeout=10)

                if exit_code == 0 and stdout.strip():
                    container_ids = [cid.strip() for cid in stdout.strip().split('\n') if cid.strip()]
                    if container_ids:
                        self._log_message(f"  👤 Stopping {len(container_ids)} container(s) for user '{username}'")
                        stop_cmd = [
                            "sudo", "-u", username,
                            "podman", "stop", "-t", str(self.config.containers.stop_timeout)
                        ]
                        stop_cmd.extend(container_ids)
                        run_command(stop_cmd, timeout=self.config.containers.stop_timeout + 30)

        self._log_message("  ✅ Rootless Podman containers stopped")

    def _sync_filesystems(self):
        """Sync all filesystems.

        Note: os.sync() schedules buffers to be flushed but may return before
        physical write completion on some systems. The 2-second sleep allows
        storage controllers (especially battery-backed RAID) to flush their
        write-back caches before power is cut.
        """
        if not self.config.filesystems.sync_enabled:
            return

        self._log_message("💾 Syncing all filesystems...")
        if self.config.behavior.dry_run:
            self._log_message("  🧪 [DRY-RUN] Would sync filesystems")
        else:
            os.sync()
            time.sleep(2)  # Allow storage controller caches to flush
            self._log_message("  ✅ Filesystems synced")

    def _unmount_filesystems(self):
        """Unmount configured filesystems."""
        if not self.config.filesystems.unmount.enabled:
            return

        if not self.config.filesystems.unmount.mounts:
            return

        timeout = self.config.filesystems.unmount.timeout
        self._log_message(f"📤 Unmounting filesystems (Max wait: {timeout}s)...")

        for mount in self.config.filesystems.unmount.mounts:
            mount_point = mount.get('path', '')
            options = mount.get('options', '')

            if not mount_point:
                continue

            options_display = f" {options}" if options else ""
            self._log_message(f"  ➡️ Unmounting {mount_point}{options_display}")

            if self.config.behavior.dry_run:
                self._log_message(
                    f"  🧪 [DRY-RUN] Would execute: timeout {timeout}s umount {options} {mount_point}"
                )
                continue

            cmd = ["umount"]
            if options:
                cmd.append(options)
            cmd.append(mount_point)

            exit_code, _, stderr = run_command(cmd, timeout=timeout)

            if exit_code == 0:
                self._log_message(f"  ✅ {mount_point} unmounted successfully")
            elif exit_code == 124:
                self._log_message(
                    f"  ⚠️ {mount_point} unmount timed out "
                    "(device may be busy/unreachable). Proceeding anyway."
                )
            else:
                check_code, _, _ = run_command(["mountpoint", "-q", mount_point])
                if check_code == 0:
                    self._log_message(
                        f"  ❌ Failed to unmount {mount_point} "
                        f"(Error code {exit_code}). Proceeding anyway."
                    )
                else:
                    self._log_message(f"  ℹ️ {mount_point} was likely not mounted.")

    def _shutdown_remote_servers(self):
        """Shutdown all enabled remote servers via SSH.

        Servers are processed in two phases:
        1. Sequential phase: Servers with parallel=False are shutdown one by one
           in config order. Use this for servers with dependencies (e.g., a server
           that hosts storage used by other servers should be shutdown last).
        2. Parallel phase: Remaining servers (parallel=True, the default) are
           shutdown concurrently using threads to avoid sequential timeouts.

        This hybrid approach ensures dependency order while still benefiting from
        parallel execution for independent servers.
        """
        enabled_servers = [s for s in self.config.remote_servers if s.enabled]

        if not enabled_servers:
            return

        # Separate servers into sequential and parallel groups
        sequential_servers = [s for s in enabled_servers if not s.parallel]
        parallel_servers = [s for s in enabled_servers if s.parallel]

        server_count = len(enabled_servers)
        seq_count = len(sequential_servers)
        par_count = len(parallel_servers)

        if seq_count > 0 and par_count > 0:
            self._log_message(
                f"🌐 Shutting down {server_count} remote server(s) "
                f"({seq_count} sequential, {par_count} parallel)..."
            )
        elif seq_count > 0:
            self._log_message(f"🌐 Shutting down {server_count} remote server(s) sequentially...")
        else:
            self._log_message(f"🌐 Shutting down {server_count} remote server(s) in parallel...")

        completed = 0

        # Phase 1: Sequential servers (in config order)
        for server in sequential_servers:
            display_name = server.name or server.host
            try:
                self._shutdown_remote_server(server)
                completed += 1
            except Exception as e:
                self._log_message(f"  ❌ {display_name} shutdown failed: {e}")

        # Phase 2: Parallel servers
        if parallel_servers:
            # Calculate max timeout for parallel servers (for the join timeout)
            def calc_server_timeout(server: RemoteServerConfig) -> int:
                pre_cmd_time = sum(
                    (cmd.timeout or server.command_timeout) for cmd in server.pre_shutdown_commands
                )
                return pre_cmd_time + server.command_timeout + server.connect_timeout + 60

            max_timeout = max(calc_server_timeout(s) for s in parallel_servers)

            # Track results for logging
            results: Dict[str, Tuple[bool, str]] = {}
            results_lock = threading.Lock()

            def shutdown_server_thread(server: RemoteServerConfig):
                """Thread worker for shutting down a single server."""
                display_name = server.name or server.host
                try:
                    self._shutdown_remote_server(server)
                    with results_lock:
                        results[display_name] = (True, "")
                except Exception as e:
                    with results_lock:
                        results[display_name] = (False, str(e))

            # Start all threads
            threads: List[threading.Thread] = []
            for server in parallel_servers:
                t = threading.Thread(
                    target=shutdown_server_thread,
                    args=(server,),
                    name=f"remote-shutdown-{server.name or server.host}"
                )
                t.start()
                threads.append(t)

            # Wait for all threads to complete with global timeout
            for t in threads:
                t.join(timeout=max_timeout)

            # Check for any threads that are still running (timed out)
            still_running = [t for t in threads if t.is_alive()]
            if still_running:
                self._log_message(
                    f"  ⚠️ {len(still_running)} remote shutdown(s) still in progress "
                    "(continuing with local shutdown)"
                )

            completed += par_count - len(still_running)

        # Log summary
        self._log_message(f"  ✅ Remote shutdown complete ({completed}/{server_count} servers)")

    def _run_remote_command(
        self,
        server: RemoteServerConfig,
        command: str,
        timeout: int,
        description: str
    ) -> Tuple[bool, str]:
        """Run a single command on a remote server via SSH.

        Returns:
            Tuple of (success, error_message)
        """
        display_name = server.name or server.host

        ssh_cmd = ["ssh"]

        # Add configured SSH options
        for opt in server.ssh_options:
            if opt.startswith("-o"):
                ssh_cmd.append(opt)
            else:
                ssh_cmd.extend(["-o", opt])

        ssh_cmd.extend([
            "-o", f"ConnectTimeout={server.connect_timeout}",
            "-o", "BatchMode=yes",  # Prevent password prompts from hanging
            f"{server.user}@{server.host}",
            command
        ])

        # Add buffer to timeout to account for SSH connection overhead
        exit_code, stdout, stderr = run_command(ssh_cmd, timeout=timeout + 30)

        if exit_code == 0:
            return True, ""
        elif exit_code == 124:
            return False, f"timed out after {timeout}s"
        else:
            error_msg = stderr.strip() if stderr.strip() else f"exit code {exit_code}"
            return False, error_msg

    def _execute_remote_pre_shutdown(self, server: RemoteServerConfig) -> bool:
        """Execute pre-shutdown commands on a remote server.

        Returns:
            True if all commands executed (success or best-effort failure)
            False if SSH connection failed entirely
        """
        if not server.pre_shutdown_commands:
            return True

        display_name = server.name or server.host
        cmd_count = len(server.pre_shutdown_commands)

        self._log_message(f"  📋 Executing {cmd_count} pre-shutdown command(s)...")

        for idx, cmd_config in enumerate(server.pre_shutdown_commands, 1):
            # Determine timeout
            timeout = cmd_config.timeout
            if timeout is None:
                timeout = server.command_timeout

            # Handle predefined action
            if cmd_config.action:
                action_name = cmd_config.action.lower()

                if action_name not in REMOTE_ACTIONS:
                    self._log_message(
                        f"    ⚠️ [{idx}/{cmd_count}] Unknown action: {action_name} (skipping)"
                    )
                    continue

                # Get command template and substitute placeholders
                command_template = REMOTE_ACTIONS[action_name]
                command = command_template.format(
                    timeout=timeout,
                    path=cmd_config.path or ""
                )
                description = action_name

                # Validate stop_compose has path
                if action_name == "stop_compose" and not cmd_config.path:
                    self._log_message(
                        f"    ⚠️ [{idx}/{cmd_count}] stop_compose requires 'path' parameter (skipping)"
                    )
                    continue

            # Handle custom command
            elif cmd_config.command:
                command = cmd_config.command
                # Truncate long commands for display
                if len(command) > 50:
                    description = command[:47] + "..."
                else:
                    description = command

            else:
                self._log_message(
                    f"    ⚠️ [{idx}/{cmd_count}] No action or command specified (skipping)"
                )
                continue

            # Log what we're about to do
            self._log_message(f"    ➡️ [{idx}/{cmd_count}] {description} (timeout: {timeout}s)")

            if self.config.behavior.dry_run:
                self._log_message(f"    🧪 [DRY-RUN] Would execute on {display_name}")
                continue

            # Execute the command
            success, error_msg = self._run_remote_command(
                server, command, timeout, description
            )

            if success:
                self._log_message(f"    ✅ [{idx}/{cmd_count}] {description} completed")
            else:
                self._log_message(
                    f"    ⚠️ [{idx}/{cmd_count}] {description} failed: {error_msg} (continuing)"
                )

        return True

    def _shutdown_remote_server(self, server: RemoteServerConfig):
        """Shutdown a single remote server via SSH.

        Execution order:
        1. Execute pre_shutdown_commands (if any) - best effort
        2. Execute shutdown_command
        """
        display_name = server.name or server.host
        has_pre_cmds = len(server.pre_shutdown_commands) > 0

        self._log_message(f"🌐 Initiating remote shutdown: {display_name} ({server.host})...")

        # Send notification for remote server shutdown start
        self._send_notification(
            f"🌐 **Remote Shutdown Starting:** {display_name}\n"
            f"Host: {server.host}",
            self.config.NOTIFY_INFO
        )

        # Execute pre-shutdown commands first
        if has_pre_cmds:
            self._execute_remote_pre_shutdown(server)

        # Execute final shutdown command
        self._log_message(f"  🔌 Sending shutdown command: {server.shutdown_command}")

        if self.config.behavior.dry_run:
            self._log_message(
                f"  🧪 [DRY-RUN] Would send command '{server.shutdown_command}' to "
                f"{server.user}@{server.host}"
            )
            return

        success, error_msg = self._run_remote_command(
            server,
            server.shutdown_command,
            server.command_timeout,
            "shutdown"
        )

        if success:
            self._log_message(f"  ✅ {display_name} shutdown command sent successfully")
            self._send_notification(
                f"✅ **Remote Shutdown Sent:** {display_name}\n"
                f"Server is shutting down.",
                self.config.NOTIFY_SUCCESS
            )
        else:
            self._log_message(
                f"  ❌ WARNING: Failed to execute shutdown command on {display_name}: {error_msg}"
            )
            self._send_notification(
                f"❌ **Remote Shutdown Failed:** {display_name}\n"
                f"Error: {error_msg}",
                self.config.NOTIFY_FAILURE
            )

    def _execute_shutdown_sequence(self):
        """Execute the controlled shutdown sequence."""
        self._shutdown_flag_path.touch()

        self._log_message("🚨 ========== INITIATING EMERGENCY SHUTDOWN SEQUENCE ==========")

        if self.config.behavior.dry_run:
            self._log_message("🧪 *** DRY-RUN MODE: No actual shutdown will occur ***")

        wall_msg = "🚨 CRITICAL: Executing emergency UPS shutdown sequence NOW!"
        if self.config.behavior.dry_run:
            wall_msg = "[DRY-RUN] " + wall_msg

        run_command(["wall", wall_msg])

        self._shutdown_vms()
        self._shutdown_containers()
        self._sync_filesystems()
        self._unmount_filesystems()
        self._shutdown_remote_servers()

        if self.config.filesystems.sync_enabled:
            self._log_message("💾 Final filesystem sync...")
            if self.config.behavior.dry_run:
                self._log_message("  🧪 [DRY-RUN] Would perform final sync")
            else:
                os.sync()
                self._log_message("  ✅ Final sync complete")

        if self.config.local_shutdown.enabled:
            self._log_message("🔌 Shutting down local server NOW")
            self._log_message("✅ ========== SHUTDOWN SEQUENCE COMPLETE ==========")

            if self.config.behavior.dry_run:
                self._log_message(f"🧪 [DRY-RUN] Would execute: {self.config.local_shutdown.command}")
                self._log_message("🧪 [DRY-RUN] Shutdown sequence completed successfully (no actual shutdown)")
                self._shutdown_flag_path.unlink(missing_ok=True)
            else:
                # Send final notification (non-blocking - fire and forget)
                self._send_notification(
                    "🛑 **Shutdown Sequence Complete**\nShutting down local server NOW.",
                    self.config.NOTIFY_FAILURE
                )
                # Give notification time to send
                time.sleep(5)

                cmd_parts = self.config.local_shutdown.command.split()
                if self.config.local_shutdown.message:
                    cmd_parts.append(self.config.local_shutdown.message)
                run_command(cmd_parts)
        else:
            self._log_message("✅ ========== SHUTDOWN SEQUENCE COMPLETE (local shutdown disabled) ==========")
            self._shutdown_flag_path.unlink(missing_ok=True)

    def _trigger_immediate_shutdown(self, reason: str):
        """Trigger an immediate shutdown if not already in progress."""
        if self._shutdown_flag_path.exists():
            return

        self._shutdown_flag_path.touch()

        # Send notification (non-blocking - fire and forget)
        self._send_notification(
            f"🚨 **EMERGENCY SHUTDOWN INITIATED!**\n"
            f"Reason: {reason}\n"
            "Executing shutdown tasks (VMs, Containers, Remote Servers).",
            self.config.NOTIFY_FAILURE
        )

        self._log_message(f"🚨 CRITICAL: Triggering immediate shutdown. Reason: {reason}")
        run_command([
            "wall",
            f"🚨 CRITICAL: UPS battery critical! Immediate shutdown initiated! Reason: {reason}"
        ])

        self._execute_shutdown_sequence()

    def _cleanup_and_exit(self, signum: int, frame):
        """Handle clean exit on signals."""
        if self._shutdown_flag_path.exists():
            if self._notification_worker:
                self._notification_worker.stop()
            sys.exit(0)

        self._shutdown_flag_path.touch()

        self._log_message("🛑 Service stopped by signal (SIGTERM/SIGINT). Monitoring is inactive.")

        # Send notification (non-blocking - fire and forget)
        self._send_notification(
            "🛑 **Eneru Service Stopped**\nMonitoring is now inactive.",
            self.config.NOTIFY_WARNING
        )

        if self._notification_worker:
            self._notification_worker.stop()

        self._shutdown_flag_path.unlink(missing_ok=True)
        sys.exit(0)

    # ==========================================================================
    # STATUS CHECKS
    # ==========================================================================

    def _check_voltage_issues(self, ups_status: str, input_voltage: str):
        """Check for voltage quality issues."""
        if "OL" not in ups_status:
            if "OB" in ups_status or "FSD" in ups_status:
                self.state.voltage_state = "NORMAL"
            return

        if not is_numeric(input_voltage):
            return

        voltage = float(input_voltage)

        if voltage < self.state.voltage_warning_low:
            if self.state.voltage_state != "LOW":
                self._log_power_event(
                    "BROWNOUT_DETECTED",
                    f"Voltage is low: {voltage}V (Threshold: {self.state.voltage_warning_low}V)"
                )
                self.state.voltage_state = "LOW"
        elif voltage > self.state.voltage_warning_high:
            if self.state.voltage_state != "HIGH":
                self._log_power_event(
                    "OVER_VOLTAGE_DETECTED",
                    f"Voltage is high: {voltage}V (Threshold: {self.state.voltage_warning_high}V)"
                )
                self.state.voltage_state = "HIGH"
        elif self.state.voltage_state != "NORMAL":
            self._log_power_event(
                "VOLTAGE_NORMALIZED",
                f"Voltage returned to normal: {voltage}V. Previous state: {self.state.voltage_state}"
            )
            self.state.voltage_state = "NORMAL"

    def _check_avr_status(self, ups_status: str, input_voltage: str):
        """Check for Automatic Voltage Regulation activity."""
        voltage_str = f"{input_voltage}V" if is_numeric(input_voltage) else "N/A"

        if "BOOST" in ups_status:
            if self.state.avr_state != "BOOST":
                self._log_power_event(
                    "AVR_BOOST_ACTIVE",
                    f"Input voltage low ({voltage_str}). UPS is boosting output."
                )
                self.state.avr_state = "BOOST"
        elif "TRIM" in ups_status:
            if self.state.avr_state != "TRIM":
                self._log_power_event(
                    "AVR_TRIM_ACTIVE",
                    f"Input voltage high ({voltage_str}). UPS is trimming output."
                )
                self.state.avr_state = "TRIM"
        elif self.state.avr_state != "INACTIVE":
            self._log_power_event("AVR_INACTIVE", f"AVR is inactive. Input voltage: {voltage_str}.")
            self.state.avr_state = "INACTIVE"

    def _check_bypass_status(self, ups_status: str):
        """Check for bypass mode."""
        if "BYPASS" in ups_status:
            if self.state.bypass_state != "ACTIVE":
                self._log_power_event("BYPASS_MODE_ACTIVE", "UPS in bypass mode - no protection active!")
                self.state.bypass_state = "ACTIVE"
        elif self.state.bypass_state != "INACTIVE":
            self._log_power_event("BYPASS_MODE_INACTIVE", "UPS left bypass mode.")
            self.state.bypass_state = "INACTIVE"

    def _check_overload_status(self, ups_status: str, ups_load: str):
        """Check for overload condition."""
        if "OVER" in ups_status:
            if self.state.overload_state != "ACTIVE":
                self._log_power_event("OVERLOAD_ACTIVE", f"UPS overload detected! Load: {ups_load}%")
                self.state.overload_state = "ACTIVE"
        elif self.state.overload_state != "INACTIVE":
            reported_load = str(ups_load) if is_numeric(ups_load) else "N/A"
            self._log_power_event("OVERLOAD_RESOLVED", f"UPS overload resolved. Load: {reported_load}%")
            self.state.overload_state = "INACTIVE"

    def _handle_on_battery(self, ups_data: Dict[str, str]):
        """Handle the On Battery state."""
        ups_status = ups_data.get('ups.status', '')
        battery_charge = ups_data.get('battery.charge', '')
        battery_runtime = ups_data.get('battery.runtime', '')
        ups_load = ups_data.get('ups.load', '')

        if "OB" not in self.state.previous_status and "FSD" not in self.state.previous_status:
            self.state.on_battery_start_time = int(time.time())
            self.state.extended_time_logged = False
            self.state.battery_history.clear()

            self._log_power_event(
                "ON_BATTERY",
                f"Battery: {battery_charge}%, Runtime: {battery_runtime} seconds, Load: {ups_load}%"
            )
            run_command([
                "wall",
                f"⚠️ WARNING: Power failure detected! System running on UPS battery "
                f"({battery_charge}% remaining, {format_seconds(battery_runtime)} runtime)"
            ])

        current_time = int(time.time())
        time_on_battery = current_time - self.state.on_battery_start_time
        depletion_rate = self._calculate_depletion_rate(battery_charge)

        shutdown_reason = ""

        # T1. Critical battery level
        if is_numeric(battery_charge):
            battery_int = int(float(battery_charge))
            if battery_int < self.config.triggers.low_battery_threshold:
                shutdown_reason = (
                    f"Battery charge {battery_int}% below threshold "
                    f"{self.config.triggers.low_battery_threshold}%"
                )
        else:
            self._log_message(f"⚠️ WARNING: Received non-numeric battery charge value: '{battery_charge}'")

        # T2. Critical runtime remaining
        if not shutdown_reason and is_numeric(battery_runtime):
            runtime_int = int(float(battery_runtime))
            if runtime_int < self.config.triggers.critical_runtime_threshold:
                shutdown_reason = (
                    f"Runtime {format_seconds(runtime_int)} below threshold "
                    f"{format_seconds(self.config.triggers.critical_runtime_threshold)}"
                )

        # T3. Dangerous depletion rate (with grace period)
        if not shutdown_reason and is_numeric(depletion_rate) and depletion_rate > 0:
            if depletion_rate > self.config.triggers.depletion.critical_rate:
                if time_on_battery < self.config.triggers.depletion.grace_period:
                    self._log_message(
                        f"🕒 INFO: High depletion rate ({depletion_rate}%/min) ignored during "
                        f"grace period ({time_on_battery}s/{self.config.triggers.depletion.grace_period}s)."
                    )
                else:
                    shutdown_reason = (
                        f"Depletion rate {depletion_rate}%/min above threshold "
                        f"{self.config.triggers.depletion.critical_rate}%/min (after grace period)"
                    )

        # T4. Extended time on battery
        if not shutdown_reason and time_on_battery > self.config.triggers.extended_time.threshold:
            if self.config.triggers.extended_time.enabled:
                shutdown_reason = (
                    f"Time on battery {format_seconds(time_on_battery)} exceeded "
                    f"threshold {format_seconds(self.config.triggers.extended_time.threshold)}"
                )
            elif not self.state.extended_time_logged:
                self._log_message(
                    f"⏳ INFO: System on battery for {format_seconds(time_on_battery)} "
                    f"exceeded threshold ({format_seconds(self.config.triggers.extended_time.threshold)}) - "
                    "extended time shutdown disabled"
                )
                self.state.extended_time_logged = True

        if shutdown_reason:
            self._trigger_immediate_shutdown(shutdown_reason)

        # Log status every 5 seconds
        if int(time.time()) % 5 == 0:
            self._log_message(
                f"🔋 On battery: {battery_charge}% ({format_seconds(battery_runtime)}), "
                f"Load: {ups_load}%, Depletion: {depletion_rate}%/min, "
                f"Time on battery: {format_seconds(time_on_battery)}"
            )

    def _handle_on_line(self, ups_data: Dict[str, str]):
        """Handle the On Line / Charging state."""
        ups_status = ups_data.get('ups.status', '')
        battery_charge = ups_data.get('battery.charge', '')
        input_voltage = ups_data.get('input.voltage', '')

        if "OB" in self.state.previous_status or "FSD" in self.state.previous_status:
            time_on_battery = 0
            if self.state.on_battery_start_time > 0:
                time_on_battery = int(time.time()) - self.state.on_battery_start_time

            self._log_power_event(
                "POWER_RESTORED",
                f"Battery: {battery_charge}% (Status: {ups_status}), "
                f"Input: {input_voltage}V, Outage duration: {format_seconds(time_on_battery)}"
            )
            run_command([
                "wall",
                f"✅ Power has been restored. UPS Status: {ups_status}. "
                f"Battery at {battery_charge}%."
            ])

            self.state.on_battery_start_time = 0
            self.state.extended_time_logged = False
            self.state.battery_history.clear()

    def _main_loop(self):
        """Main monitoring loop."""
        while True:
            success, ups_data, error_msg = self._get_all_ups_data()

            # ==================================================================
            # CONNECTION HANDLING AND FAILSAFE
            # ==================================================================

            if not success:
                is_failsafe_trigger = False

                if "Data stale" in error_msg:
                    self.state.stale_data_count += 1
                    if self.state.connection_state != "FAILED":
                        self._log_message(
                            f"⚠️ WARNING: Data stale from UPS {self.config.ups.name} "
                            f"(Attempt {self.state.stale_data_count}/{self.config.ups.max_stale_data_tolerance})."
                        )

                    if self.state.stale_data_count >= self.config.ups.max_stale_data_tolerance:
                        if self.state.connection_state != "FAILED":
                            self._log_power_event(
                                "CONNECTION_LOST",
                                f"Data from UPS {self.config.ups.name} is persistently stale "
                                f"(>= {self.config.ups.max_stale_data_tolerance} attempts). Monitoring is inactive."
                            )
                            self.state.connection_state = "FAILED"
                        is_failsafe_trigger = True
                else:
                    if self.state.connection_state != "FAILED":
                        self._log_message(
                            f"❌ ERROR: Cannot connect to UPS {self.config.ups.name}. Output: {error_msg}"
                        )
                    self.state.stale_data_count = 0

                    if self.state.connection_state != "FAILED":
                        self._log_power_event(
                            "CONNECTION_LOST",
                            f"Cannot connect to UPS {self.config.ups.name} "
                            "(Network, Server, or Config error). Monitoring is inactive."
                        )
                        self.state.connection_state = "FAILED"
                    is_failsafe_trigger = True

                # FAILSAFE: If connection lost while on battery, shutdown immediately
                if is_failsafe_trigger and "OB" in self.state.previous_status:
                    self._shutdown_flag_path.touch()
                    self._log_message(
                        "🚨 FAILSAFE TRIGGERED (FSB): Connection lost or data persistently stale "
                        "while On Battery. Initiating emergency shutdown."
                    )
                    # Send notification (non-blocking - fire and forget)
                    self._send_notification(
                        "🚨 **FAILSAFE (FSB) TRIGGERED!**\n"
                        "Connection to UPS lost or data stale while system was running On Battery.\n"
                        "Assuming critical failure. Executing immediate shutdown.",
                        self.config.NOTIFY_FAILURE
                    )
                    self._execute_shutdown_sequence()

                time.sleep(5)
                continue

            # ==================================================================
            # DATA PROCESSING
            # ==================================================================

            self.state.stale_data_count = 0

            if self.state.connection_state == "FAILED":
                self._log_power_event(
                    "CONNECTION_RESTORED",
                    f"Connection to UPS {self.config.ups.name} restored. Monitoring is active."
                )
                self.state.connection_state = "OK"

            ups_status = ups_data.get('ups.status', '')

            if not ups_status:
                self._log_message(
                    "❌ ERROR: Received data from UPS but 'ups.status' is missing. "
                    "Check NUT configuration."
                )
                time.sleep(5)
                continue

            self._save_state(ups_data)

            # Detect status changes
            if ups_status != self.state.previous_status and self.state.previous_status:
                battery_charge = ups_data.get('battery.charge', '')
                battery_runtime = ups_data.get('battery.runtime', '')
                ups_load = ups_data.get('ups.load', '')
                self._log_message(
                    f"🔄 Status changed: {self.state.previous_status} -> {ups_status} "
                    f"(Battery: {battery_charge}%, Runtime: {format_seconds(battery_runtime)}, "
                    f"Load: {ups_load}%)"
                )

            # ==================================================================
            # POWER STATE ANALYSIS AND SHUTDOWN TRIGGERS
            # ==================================================================

            if "FSD" in ups_status:
                self._trigger_immediate_shutdown("UPS signaled FSD (Forced Shutdown) flag.")

            elif "OB" in ups_status:
                self._handle_on_battery(ups_data)

            elif "OL" in ups_status or "CHRG" in ups_status:
                self._handle_on_line(ups_data)

            # ==================================================================
            # ENVIRONMENT MONITORING
            # ==================================================================

            input_voltage = ups_data.get('input.voltage', '')
            ups_load = ups_data.get('ups.load', '')

            self._check_voltage_issues(ups_status, input_voltage)
            self._check_avr_status(ups_status, input_voltage)
            self._check_bypass_status(ups_status)
            self._check_overload_status(ups_status, ups_load)

            self.state.previous_status = ups_status

            time.sleep(self.config.ups.check_interval)


# ==============================================================================
# ENTRY POINT
# ==============================================================================

def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Eneru - Intelligent UPS Monitoring & Shutdown Orchestration for NUT"
    )
    parser.add_argument(
        "-c", "--config",
        help="Path to configuration file (default: /etc/ups-monitor/config.yaml)",
        default=None
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run in dry-run mode (overrides config file setting)"
    )
    parser.add_argument(
        "--validate-config",
        action="store_true",
        help="Validate configuration file and exit"
    )
    parser.add_argument(
        "--test-notifications",
        action="store_true",
        help="Send a test notification and exit"
    )
    parser.add_argument(
        "-v", "--version",
        action="version",
        version=f"Eneru v{__version__}"
    )

    args = parser.parse_args()

    # Load configuration
    config = ConfigLoader.load(args.config)

    # Override dry-run if specified on command line
    if args.dry_run:
        config.behavior.dry_run = True

    # Handle --validate-config and/or --test-notifications
    if args.validate_config or args.test_notifications:
        exit_code = 0

        # Validate config if requested
        if args.validate_config:
            print(f"Eneru v{__version__}")
            print("Configuration is valid.")
            print(f"  UPS: {config.ups.name}")
            print(f"  Dry-run: {config.behavior.dry_run}")
            print(f"  VMs enabled: {config.virtual_machines.enabled}")
            print(f"  Containers enabled: {config.containers.enabled}", end="")
            if config.containers.enabled:
                compose_count = len(config.containers.compose_files)
                if compose_count > 0:
                    print(f" (runtime: {config.containers.runtime}, {compose_count} compose file(s))")
                else:
                    print(f" (runtime: {config.containers.runtime})")
            else:
                print()
            print(f"  Filesystems sync: {config.filesystems.sync_enabled}", end="")
            if config.filesystems.unmount.enabled:
                mount_count = len(config.filesystems.unmount.mounts)
                print(f", unmount: {mount_count} mount(s)")
            else:
                print()
            print(f"  Remote servers: {len([s for s in config.remote_servers if s.enabled])}")

            # Notification status
            print(f"  Notifications:")
            if config.notifications.enabled and config.notifications.urls:
                if APPRISE_AVAILABLE:
                    print(f"    Enabled: {len(config.notifications.urls)} service(s)")
                    for url in config.notifications.urls:
                        if '://' in url:
                            scheme = url.split('://')[0]
                            print(f"      - {scheme}://***")
                        else:
                            print(f"      - {url[:20]}...")
                    if config.notifications.title:
                        print(f"    Title: {config.notifications.title}")
                    else:
                        print(f"    Title: (none)")
                    if config.notifications.avatar_url:
                        print(f"    Avatar URL: {config.notifications.avatar_url[:50]}...")
                    print(f"    Retry interval: {config.notifications.retry_interval}s")
                else:
                    print(f"    ⚠️ Apprise not installed - notifications disabled")
                    print(f"    Install with: pip install apprise")
            else:
                print(f"    Disabled")

            # Run validation checks and print warnings/info
            messages = ConfigLoader.validate_config(config)
            if messages:
                print()
                for msg in messages:
                    print(f"  ℹ️ {msg}")

        # Test notifications if requested
        if args.test_notifications:
            if args.validate_config:
                print()  # Add separator between outputs
                print("-" * 50)
                print()

            print("Testing notifications...")

            if not config.notifications.enabled or not config.notifications.urls:
                print("❌ No notification URLs configured.")
                print("   Add URLs to the 'notifications.urls' section in your config file.")
                exit_code = 1
            elif not APPRISE_AVAILABLE:
                print("❌ Apprise is not installed.")
                print("   Install with: pip install apprise")
                exit_code = 1
            else:
                # Initialize Apprise
                apobj = apprise.Apprise()
                valid_urls = 0

                for url in config.notifications.urls:
                    if apobj.add(url):
                        valid_urls += 1
                        # Extract scheme without avatar params for display
                        scheme = url.split('://')[0] if '://' in url else 'unknown'
                        print(f"  ✅ Added: {scheme}://***")
                    else:
                        print(f"  ❌ Invalid URL: {url[:30]}...")

                if valid_urls == 0:
                    print("❌ No valid notification URLs found.")
                    exit_code = 1
                else:
                    print(f"\nSending test notification to {valid_urls} service(s)...")

                    if config.notifications.title:
                        print(f"  Title: {config.notifications.title}")
                    if config.notifications.avatar_url:
                        print(f"  Avatar: {config.notifications.avatar_url[:50]}...")

                    # Send test notification
                    test_body = (
                        "🧪 **Test Notification**\n"
                        "This is a test notification from Eneru.\n"
                        "If you see this, notifications are working correctly!\n"
                        f"\n---\n⚡ UPS: {config.ups.name}\n"
                        f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M:%S %Z')}"
                    )

                    # Escape @ symbols to prevent Discord mentions (e.g., UPS@192.168.1.1)
                    escaped_body = test_body.replace("@", "@\u200B")  # Zero-width space after @

                    # Build notify kwargs
                    notify_kwargs = {
                        'body': escaped_body,
                        'notify_type': apprise.NotifyType.INFO,
                    }

                    # Only add title if configured
                    if config.notifications.title:
                        notify_kwargs['title'] = config.notifications.title

                    result = apobj.notify(**notify_kwargs)

                    if result:
                        print("✅ Test notification sent successfully!")
                    else:
                        print("❌ Failed to send test notification.")
                        print("   Check your notification URLs and network connectivity.")
                        exit_code = 1

        sys.exit(exit_code)

    # Run monitor
    monitor = UPSMonitor(config)
    monitor.run()


if __name__ == "__main__":
    main()
