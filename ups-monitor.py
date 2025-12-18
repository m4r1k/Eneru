#!/usr/bin/env python3
"""
UPS Monitor - Generic UPS Monitoring and Shutdown Management
Monitors UPS status via NUT and triggers configurable shutdown sequences.
"""

import subprocess
import sys
import os
import time
import signal
import logging
import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, Any, Tuple, List, Union
from collections import deque
from dataclasses import dataclass, field
import threading

# Optional imports with graceful degradation
try:
    import yaml
    YAML_AVAILABLE = True
except ImportError:
    YAML_AVAILABLE = False

try:
    import requests
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False


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
class DiscordConfig:
    """Discord notification configuration."""
    webhook_url: str = ""
    timeout: int = 3
    timeout_blocking: int = 10


@dataclass
class NotificationsConfig:
    """Notifications configuration."""
    discord: Optional[DiscordConfig] = None


@dataclass
class VMConfig:
    """Virtual machine shutdown configuration."""
    enabled: bool = False
    max_wait: int = 30


@dataclass
class ContainersConfig:
    """Container runtime shutdown configuration."""
    enabled: bool = False
    runtime: str = "auto"  # "auto", "docker", or "podman"
    stop_timeout: int = 60
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

    # Discord embed colors (not configurable via file)
    COLOR_RED: int = 15158332
    COLOR_GREEN: int = 3066993
    COLOR_YELLOW: int = 15844367
    COLOR_ORANGE: int = 15105570
    COLOR_BLUE: int = 3447003


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
                print(f"Warning: Config file not found: {config_path}")
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
        if 'notifications' in data:
            notif_data = data['notifications']
            if 'discord' in notif_data:
                discord_data = notif_data['discord']
                config.notifications = NotificationsConfig(
                    discord=DiscordConfig(
                        webhook_url=discord_data.get('webhook_url', ''),
                        timeout=discord_data.get('timeout', 3),
                        timeout_blocking=discord_data.get('timeout_blocking', 10),
                    )
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
            # Handle legacy 'docker' section format
            if 'docker' in data and 'containers' not in data:
                # Legacy format: docker.enabled, docker.stop_timeout
                config.containers = ContainersConfig(
                    enabled=containers_data.get('enabled', False),
                    runtime="docker",  # Legacy config assumes docker
                    stop_timeout=containers_data.get('stop_timeout', 60),
                    include_user_containers=False,
                )
            else:
                # New format: containers section
                config.containers = ContainersConfig(
                    enabled=containers_data.get('enabled', False),
                    runtime=containers_data.get('runtime', 'auto'),
                    stop_timeout=containers_data.get('stop_timeout', 60),
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
                servers.append(RemoteServerConfig(
                    name=server_data.get('name', ''),
                    enabled=server_data.get('enabled', False),
                    host=server_data.get('host', ''),
                    user=server_data.get('user', ''),
                    connect_timeout=server_data.get('connect_timeout', 10),
                    command_timeout=server_data.get('command_timeout', 30),
                    shutdown_command=server_data.get('shutdown_command', 'sudo shutdown -h now'),
                    ssh_options=server_data.get('ssh_options', []),
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

    def run(self):
        """Main entry point."""
        try:
            self._initialize()
            self._main_loop()
        except KeyboardInterrupt:
            self._cleanup_and_exit(signal.SIGINT, None)
        except Exception as e:
            self._log_message(f"❌ FATAL ERROR: {e}")
            self._send_notification(f"❌ **FATAL ERROR:** {e}", self.config.COLOR_RED)
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

        self._check_dependencies()

        self._log_message(f"🚀 UPS Monitor starting - monitoring {self.config.ups.name}")
        self._send_notification(
            f"🚀 **UPS Monitor Service Started.**\nMonitoring {self.config.ups.name}.",
            self.config.COLOR_BLUE
        )

        if self.config.behavior.dry_run:
            self._log_message("🧪 *** RUNNING IN DRY-RUN MODE - NO ACTUAL SHUTDOWN WILL OCCUR ***")

        self._log_enabled_features()
        self._wait_for_initial_connection()
        self._initialize_voltage_thresholds()

    def _log_enabled_features(self):
        """Log which features are enabled."""
        features = []

        if self.config.virtual_machines.enabled:
            features.append("VMs")
        if self.config.containers.enabled:
            runtime = self.config.containers.runtime
            if runtime == "auto":
                features.append("Containers (auto-detect)")
            else:
                features.append(f"Containers ({runtime})")
        if self.config.filesystems.sync_enabled:
            features.append("FS Sync")
        if self.config.filesystems.unmount.enabled:
            features.append(f"Unmount ({len(self.config.filesystems.unmount.mounts)} mounts)")

        enabled_servers = [s for s in self.config.remote_servers if s.enabled]
        if enabled_servers:
            features.append(f"Remote ({len(enabled_servers)} servers)")

        if self.config.local_shutdown.enabled:
            features.append("Local Shutdown")

        if self._is_notifications_enabled():
            features.append("Discord")

        self._log_message(f"📋 Enabled features: {', '.join(features) if features else 'None'}")

    def _is_notifications_enabled(self) -> bool:
        """Check if notifications are enabled."""
        if not REQUESTS_AVAILABLE:
            return False
        if not self.config.notifications:
            return False
        if not self.config.notifications.discord:
            return False
        return bool(self.config.notifications.discord.webhook_url)

    def _log_message(self, message: str):
        """Log a message using the logger."""
        if self.logger:
            self.logger.log(message)
        else:
            tz_name = time.strftime('%Z')
            timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            print(f"{timestamp} {tz_name} - {message}")

        if self._shutdown_flag_path.exists():
            discord_safe_message = message.replace('`', '\\`')
            notification_text = f"ℹ️ **Shutdown Detail:** {discord_safe_message}"
            self._send_notification(notification_text, self.config.COLOR_BLUE)

    def _send_notification(self, message: str, color: Optional[int] = None):
        """Send a Discord notification."""
        if not self._is_notifications_enabled():
            return

        if color is None:
            color = self.config.COLOR_YELLOW

        discord_config = self.config.notifications.discord
        payload = {
            "embeds": [{
                "title": "UPS Monitor Alert",
                "description": message,
                "color": int(color),
                "footer": {"text": f"UPS: {self.config.ups.name}"},
                "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
            }]
        }

        is_shutdown = self._shutdown_flag_path.exists()
        timeout_val = discord_config.timeout_blocking if is_shutdown else discord_config.timeout

        def send_request():
            try:
                requests.post(
                    discord_config.webhook_url,
                    json=payload,
                    timeout=timeout_val,
                    headers={"Content-Type": "application/json"}
                )
                if is_shutdown:
                    time.sleep(0.5)
            except Exception:
                pass

        if is_shutdown:
            send_request()
        else:
            thread = threading.Thread(target=send_request, daemon=True)
            thread.start()

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

        discord_message: Optional[str] = None
        discord_color = self.config.COLOR_YELLOW

        event_handlers = {
            "ON_BATTERY": (
                f"⚠️ **POWER FAILURE DETECTED!**\nSystem running on battery.\nDetails: {details}",
                self.config.COLOR_ORANGE
            ),
            "POWER_RESTORED": (
                f"✅ **POWER RESTORED.**\nSystem back on line power/charging.\nDetails: {details}",
                self.config.COLOR_GREEN
            ),
            "BROWNOUT_DETECTED": (
                f"⚠️ **VOLTAGE ISSUE:** {event}\nDetails: {details}",
                self.config.COLOR_ORANGE
            ),
            "OVER_VOLTAGE_DETECTED": (
                f"⚠️ **VOLTAGE ISSUE:** {event}\nDetails: {details}",
                self.config.COLOR_ORANGE
            ),
            "AVR_BOOST_ACTIVE": (
                f"⚡ **AVR ACTIVE:** {event}\nDetails: {details}",
                self.config.COLOR_YELLOW
            ),
            "AVR_TRIM_ACTIVE": (
                f"⚡ **AVR ACTIVE:** {event}\nDetails: {details}",
                self.config.COLOR_YELLOW
            ),
            "BYPASS_MODE_ACTIVE": (
                f"🚨 **UPS IN BYPASS MODE!**\nNo protection active!\nDetails: {details}",
                self.config.COLOR_RED
            ),
            "BYPASS_MODE_INACTIVE": (
                f"✅ **Bypass Mode Inactive.**\nProtection restored.\nDetails: {details}",
                self.config.COLOR_GREEN
            ),
            "OVERLOAD_ACTIVE": (
                f"🚨 **UPS OVERLOAD DETECTED!**\nDetails: {details}",
                self.config.COLOR_RED
            ),
            "OVERLOAD_RESOLVED": (
                f"✅ **Overload Resolved.**\nDetails: {details}",
                self.config.COLOR_GREEN
            ),
            "CONNECTION_LOST": (
                f"❌ **ERROR: Connection Lost**\n{details}",
                self.config.COLOR_RED
            ),
            "CONNECTION_RESTORED": (
                f"✅ **Connection Restored.**\n{details}",
                self.config.COLOR_GREEN
            ),
        }

        # Skip these events for Discord
        if event in ("VOLTAGE_NORMALIZED", "AVR_INACTIVE"):
            return

        if event in event_handlers:
            discord_message, discord_color = event_handlers[event]
        else:
            discord_message = f"⚡ **Event:** {event}\nDetails: {details}"

        if discord_message:
            self._send_notification(discord_message, discord_color)

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
            # Auto-detect: prefer podman (more common on modern RHEL/Fedora)
            if command_exists("podman"):
                return "podman"
            elif command_exists("docker"):
                return "docker"
            return None

        else:
            self._log_message(f"⚠️ WARNING: Unknown container runtime '{runtime_config}'")
            return None

    def _check_dependencies(self):
        """Check for required and optional dependencies."""
        required_cmds = ["upsc", "sync", "shutdown", "logger"]
        missing = [cmd for cmd in required_cmds if not command_exists(cmd)]

        if missing:
            error_msg = f"❌ FATAL ERROR: Missing required commands: {', '.join(missing)}"
            print(error_msg)
            self._shutdown_flag_path.touch()
            self._send_notification(
                f"❌ **FATAL ERROR:** Missing dependencies: {', '.join(missing)}. Script cannot start.",
                self.config.COLOR_RED
            )
            self._shutdown_flag_path.unlink(missing_ok=True)
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
            else:
                self._log_message("⚠️ WARNING: No container runtime found. Container shutdown will be skipped.")
                self.config.containers.enabled = False

        enabled_servers = [s for s in self.config.remote_servers if s.enabled]
        if enabled_servers and not command_exists("ssh"):
            self._log_message("⚠️ WARNING: 'ssh' not found but remote servers are configured. Remote shutdown will be skipped.")
            for server in self.config.remote_servers:
                server.enabled = False

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
            self._log_message(" ℹ️ virsh not available, skipping VM shutdown")
            return

        exit_code, stdout, _ = run_command(["virsh", "list", "--name", "--state-running"])
        if exit_code != 0:
            self._log_message(" ⚠️ Failed to get VM list")
            return

        running_vms = [vm.strip() for vm in stdout.strip().split('\n') if vm.strip()]

        if not running_vms:
            self._log_message(" ℹ️ No running VMs found")
            return

        for vm in running_vms:
            self._log_message(f" ⏹️ Shutting down VM: {vm}")
            if self.config.behavior.dry_run:
                self._log_message(f" 🧪 [DRY-RUN] Would shutdown VM: {vm}")
            else:
                exit_code, stdout, stderr = run_command(["virsh", "shutdown", vm])
                if stdout.strip():
                    self._log_message(f"  {stdout.strip()}")

        if self.config.behavior.dry_run:
            return

        max_wait = self.config.virtual_machines.max_wait
        self._log_message(f" ⏳ Waiting up to {max_wait}s for VMs to shutdown gracefully...")
        wait_interval = 5
        time_waited = 0
        remaining_vms: List[str] = []

        while time_waited < max_wait:
            exit_code, stdout, _ = run_command(["virsh", "list", "--name", "--state-running"])
            still_running = set(vm.strip() for vm in stdout.strip().split('\n') if vm.strip())
            remaining_vms = [vm for vm in running_vms if vm in still_running]

            if not remaining_vms:
                self._log_message(f" ✅ All VMs stopped gracefully after {time_waited}s.")
                break

            self._log_message(f" 🕒 Still waiting for: {' '.join(remaining_vms)} (Waited {time_waited}s)")
            time.sleep(wait_interval)
            time_waited += wait_interval

        if remaining_vms:
            self._log_message(" ⚠️ Timeout reached. Force destroying remaining VMs.")
            for vm in remaining_vms:
                self._log_message(f" ⚡ Force destroying VM: {vm}")
                run_command(["virsh", "destroy", vm])

        self._log_message(" ✅ All VMs shutdown complete")

    def _shutdown_containers(self):
        """Stop all containers using detected runtime (Docker/Podman)."""
        if not self.config.containers.enabled:
            return

        if not self._container_runtime:
            return

        runtime = self._container_runtime
        runtime_display = runtime.capitalize()

        self._log_message(f"🐳 Stopping all {runtime_display} containers...")

        # Get list of running containers
        exit_code, stdout, _ = run_command([runtime, "ps", "-q"])
        if exit_code != 0:
            self._log_message(f" ⚠️ Failed to get {runtime_display} container list")
            return

        container_ids = [cid.strip() for cid in stdout.strip().split('\n') if cid.strip()]

        if not container_ids:
            self._log_message(f" ℹ️ No running {runtime_display} containers found")
        else:
            if self.config.behavior.dry_run:
                exit_code, stdout, _ = run_command([runtime, "ps", "--format", "{{.Names}}"])
                names = stdout.strip().replace('\n', ' ')
                self._log_message(f" 🧪 [DRY-RUN] Would stop {runtime_display} containers: {names}")
            else:
                # Stop containers with timeout
                stop_cmd = [runtime, "stop", "-t", str(self.config.containers.stop_timeout)]
                stop_cmd.extend(container_ids)
                run_command(stop_cmd, timeout=self.config.containers.stop_timeout + 30)
                self._log_message(f" ✅ {runtime_display} containers stopped")

        # Handle Podman rootless containers if configured
        if runtime == "podman" and self.config.containers.include_user_containers:
            self._shutdown_podman_user_containers()

    def _shutdown_podman_user_containers(self):
        """Stop Podman containers running as non-root users."""
        self._log_message(" 🔍 Checking for rootless Podman containers...")

        if self.config.behavior.dry_run:
            self._log_message(" 🧪 [DRY-RUN] Would stop rootless Podman containers for all users")
            return

        # Get list of users with active Podman containers
        # This requires loginctl and users with linger enabled
        exit_code, stdout, _ = run_command(["loginctl", "list-users", "--no-legend"])
        if exit_code != 0:
            self._log_message(" ⚠️ Failed to list users for rootless container check")
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
                        self._log_message(f" 👤 Stopping {len(container_ids)} container(s) for user '{username}'")
                        stop_cmd = [
                            "sudo", "-u", username,
                            "podman", "stop", "-t", str(self.config.containers.stop_timeout)
                        ]
                        stop_cmd.extend(container_ids)
                        run_command(stop_cmd, timeout=self.config.containers.stop_timeout + 30)

        self._log_message(" ✅ Rootless Podman containers stopped")

    def _sync_filesystems(self):
        """Sync all filesystems."""
        if not self.config.filesystems.sync_enabled:
            return

        self._log_message("💾 Syncing all filesystems...")
        if self.config.behavior.dry_run:
            self._log_message(" 🧪 [DRY-RUN] Would sync filesystems")
        else:
            os.sync()
            self._log_message(" ✅ Filesystems synced")

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
            self._log_message(f" ➡️ Unmounting {mount_point}{options_display}")

            if self.config.behavior.dry_run:
                self._log_message(
                    f" 🧪 [DRY-RUN] Would execute: timeout {timeout}s umount {options} {mount_point}"
                )
                continue

            cmd = ["umount"]
            if options:
                cmd.append(options)
            cmd.append(mount_point)

            exit_code, _, stderr = run_command(cmd, timeout=timeout)

            if exit_code == 0:
                self._log_message(f" ✅ {mount_point} unmounted successfully")
            elif exit_code == 124:
                self._log_message(
                    f" ⚠️ {mount_point} unmount timed out "
                    "(device may be busy/unreachable). Proceeding anyway."
                )
            else:
                check_code, _, _ = run_command(["mountpoint", "-q", mount_point])
                if check_code == 0:
                    self._log_message(
                        f" ❌ Failed to unmount {mount_point} "
                        f"(Error code {exit_code}). Proceeding anyway."
                    )
                else:
                    self._log_message(f" ℹ️ {mount_point} was likely not mounted.")

    def _shutdown_remote_servers(self):
        """Shutdown all enabled remote servers via SSH."""
        enabled_servers = [s for s in self.config.remote_servers if s.enabled]

        if not enabled_servers:
            return

        for server in enabled_servers:
            self._shutdown_remote_server(server)

    def _shutdown_remote_server(self, server: RemoteServerConfig):
        """Shutdown a single remote server via SSH."""
        display_name = server.name or server.host
        self._log_message(f"🌐 Initiating remote shutdown: {display_name} ({server.host})...")

        if self.config.behavior.dry_run:
            self._log_message(
                f" 🧪 [DRY-RUN] Would send command '{server.shutdown_command}' to "
                f"{server.user}@{server.host}"
            )
            return

        ssh_cmd = ["ssh"]

        # Add configured SSH options
        for opt in server.ssh_options:
            ssh_cmd.extend(["-o", opt.lstrip("-o ").lstrip("-o")] if opt.startswith("-o") else [opt])

        ssh_cmd.extend([
            "-o", f"ConnectTimeout={server.connect_timeout}",
            f"{server.user}@{server.host}",
            server.shutdown_command
        ])

        exit_code, stdout, stderr = run_command(ssh_cmd, timeout=server.command_timeout)

        if exit_code == 0:
            self._log_message(f" ✅ {display_name} shutdown command sent successfully")
        else:
            self._log_message(
                f" ❌ WARNING: Failed to execute shutdown command on {display_name} "
                f"(Error code {exit_code})"
            )
            if stderr.strip():
                self._log_message(f"  Error: {stderr.strip()}")

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
                self._log_message(" 🧪 [DRY-RUN] Would perform final sync")
            else:
                os.sync()
                self._log_message(" ✅ Final sync complete")

        if self.config.local_shutdown.enabled:
            self._log_message("🔌 Shutting down local server NOW")
            self._log_message("✅ ========== SHUTDOWN SEQUENCE COMPLETE ==========")

            if self.config.behavior.dry_run:
                self._log_message(f"🧪 [DRY-RUN] Would execute: {self.config.local_shutdown.command}")
                self._log_message("🧪 [DRY-RUN] Shutdown sequence completed successfully (no actual shutdown)")
                self._shutdown_flag_path.unlink(missing_ok=True)
            else:
                self._send_notification(
                    "🛑 **Shutdown Sequence Complete.**\nShutting down local server NOW.",
                    self.config.COLOR_RED
                )
                # Parse command and add message
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

        self._send_notification(
            f"🚨 **EMERGENCY SHUTDOWN INITIATED!**\n"
            f"Reason: {reason}\n"
            "Executing shutdown tasks (VMs, Docker, NAS).",
            self.config.COLOR_RED
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
            sys.exit(0)

        self._shutdown_flag_path.touch()

        self._log_message("🛑 Service stopped by signal (SIGTERM/SIGINT). Monitoring is inactive.")
        self._send_notification(
            "🛑 **UPS Monitor Service Stopped.**\nMonitoring is now inactive.",
            self.config.COLOR_ORANGE
        )

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
                    f"Battery charge {battery_charge}% below threshold "
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
                    self._send_notification(
                        "🚨 **FAILSAFE (FSB) TRIGGERED!**\n"
                        "Connection to UPS lost or data stale while system was running On Battery.\n"
                        "Assuming critical failure. Executing immediate shutdown.",
                        self.config.COLOR_RED
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
        description="UPS Monitor - Monitor UPS status and trigger safe shutdown on power events"
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

    args = parser.parse_args()

    # Load configuration
    config = ConfigLoader.load(args.config)

    # Override dry-run if specified on command line
    if args.dry_run:
        config.behavior.dry_run = True

    # Validate config and exit if requested
    if args.validate_config:
        print("Configuration is valid.")
        print(f"  UPS: {config.ups.name}")
        print(f"  Dry-run: {config.behavior.dry_run}")
        print(f"  VMs enabled: {config.virtual_machines.enabled}")
        print(f"  Containers enabled: {config.containers.enabled}", end="")
        if config.containers.enabled:
            print(f" (runtime: {config.containers.runtime})")
        else:
            print()
        print(f"  Remote servers: {len([s for s in config.remote_servers if s.enabled])}")
        print(f"  Notifications: {config.notifications.discord is not None and bool(config.notifications.discord.webhook_url)}")
        sys.exit(0)

    # Run monitor
    monitor = UPSMonitor(config)
    monitor.run()


if __name__ == "__main__":
    main()
