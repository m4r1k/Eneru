#!/usr/bin/env python3
"""
Eneru - Generic UPS Monitoring and Shutdown Management
Monitors UPS status via NUT and triggers configurable shutdown sequences.
https://github.com/m4r1k/Eneru
"""

import sys
import os
import time
import signal
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Tuple, List
from collections import deque

# Import from new modules
from eneru.version import __version__
from eneru.config import (
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
    ConfigLoader,
    YAML_AVAILABLE,
)
from eneru.state import MonitorState
from eneru.logger import UPSLogger, TimezoneFormatter
from eneru.notifications import NotificationWorker, APPRISE_AVAILABLE
from eneru.utils import run_command, command_exists, is_numeric, format_seconds
from eneru.actions import REMOTE_ACTIONS

# Re-export for backwards compatibility (tests may mock these)
try:
    import apprise
except ImportError:
    apprise = None


def compute_effective_order(
    servers: List[RemoteServerConfig],
) -> List[Tuple[int, RemoteServerConfig]]:
    """Compute effective shutdown order for each server.

    Rules:
    - If shutdown_order is explicitly set: use that value.
    - If shutdown_order is None and parallel is False: each gets a unique
      negative value based on its position among parallel=False servers,
      preserving the legacy "sequential-first" behavior.
    - Otherwise (shutdown_order is None and parallel is None or True):
      effective = 0 (parallel batch).

    This ensures existing configs with only ``parallel`` produce identical
    behavior to the old two-phase code.
    """
    result: List[Tuple[int, RemoteServerConfig]] = []
    legacy_seq_count = sum(
        1 for s in servers
        if s.shutdown_order is None and s.parallel is False
    )
    legacy_seq_index = 0
    for server in servers:
        if server.shutdown_order is not None:
            effective = server.shutdown_order
        elif server.parallel is False:
            # Legacy sequential: assign negative orders in config order
            # so they all run before the parallel batch (order 0).
            effective = -(legacy_seq_count - legacy_seq_index)
            legacy_seq_index += 1
        else:
            effective = 0
        result.append((effective, server))
    return result


class UPSGroupMonitor:
    """Main UPS Monitor class."""

    def __init__(self, config: Config, exit_after_shutdown: bool = False,
                 coordinator_mode: bool = False,
                 shutdown_callback=None,
                 stop_event: Optional[threading.Event] = None,
                 log_prefix: str = "",
                 notification_worker: Optional[NotificationWorker] = None,
                 logger: Optional[UPSLogger] = None,
                 state_file_suffix: str = ""):
        self.config = config
        self.state = MonitorState()
        self.logger: Optional[UPSLogger] = logger
        self._coordinator_mode = coordinator_mode
        self._shutdown_callback = shutdown_callback
        self._stop_event = stop_event or threading.Event()
        self._log_prefix = log_prefix

        # Per-group state file paths (suffix for multi-UPS)
        sfx = f".{state_file_suffix}" if state_file_suffix else ""
        self._shutdown_flag_path = Path(config.logging.shutdown_flag_file + sfx)
        self._battery_history_path = Path(config.logging.battery_history_file + sfx)
        self._state_file_path = Path(config.logging.state_file + sfx)

        self._container_runtime: Optional[str] = None
        self._compose_available: bool = False
        self._notification_worker: Optional[NotificationWorker] = notification_worker
        self._exit_after_shutdown = exit_after_shutdown

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
        if not self._coordinator_mode:
            signal.signal(signal.SIGTERM, self._cleanup_and_exit)
            signal.signal(signal.SIGINT, self._cleanup_and_exit)

        if self.logger is None:
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
        # In coordinator mode, the notification worker is shared and pre-initialized
        if self._notification_worker is not None:
            return

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

        grace_cfg = self.config.ups.connection_loss_grace_period
        if grace_cfg.enabled:
            features.append(f"Connection Grace ({grace_cfg.duration}s)")

        self._log_message(f"📋 Enabled features: {', '.join(features) if features else 'None'}")

    def _log_message(self, message: str):
        """Log a message using the logger, with optional prefix for multi-UPS."""
        prefixed = f"{self._log_prefix}{message}" if self._log_prefix else message
        if self.logger:
            self.logger.log(prefixed)
        else:
            tz_name = time.strftime('%Z')
            timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            print(f"{timestamp} {tz_name} - {prefixed}")

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

        # Prefix notification body with UPS name in multi-UPS mode
        prefixed_body = f"{self._log_prefix}{body}" if self._log_prefix else body

        # Escape @ symbols to prevent Discord mentions (e.g., UPS@192.168.1.1)
        escaped_body = prefixed_body.replace("@", "@\u200B")  # Zero-width space after @

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
                f"⚠️ **UPS IN BYPASS MODE!**\nNo protection active!\nDetails: {details}",
                self.config.NOTIFY_FAILURE
            ),
            "BYPASS_MODE_INACTIVE": (
                f"✅ **Bypass Mode Inactive**\nProtection restored.\nDetails: {details}",
                self.config.NOTIFY_SUCCESS
            ),
            "OVERLOAD_ACTIVE": (
                f"⚠️ **UPS OVERLOAD DETECTED!**\nDetails: {details}",
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

        Servers are grouped by their effective shutdown order and processed
        in ascending order.  All servers within a group run in parallel.
        A server alone in its group effectively runs sequentially.

        When shutdown_order is not set, the legacy parallel flag determines
        effective order:
        - parallel: true  (default) -> effective order 0
        - parallel: false -> unique negative orders (run before order 0)
        This preserves exact backward compatibility with existing configs.
        """
        enabled_servers = [s for s in self.config.remote_servers if s.enabled]

        if not enabled_servers:
            return

        # Group servers by effective shutdown order
        ordered = compute_effective_order(enabled_servers)
        phases: Dict[int, List[RemoteServerConfig]] = {}
        for effective, server in ordered:
            phases.setdefault(effective, []).append(server)
        sorted_keys = sorted(phases.keys())

        server_count = len(enabled_servers)
        num_phases = len(sorted_keys)

        if num_phases > 1:
            self._log_message(
                f"🌐 Shutting down {server_count} remote server(s) in {num_phases} phases..."
            )
        elif server_count > 1:
            self._log_message(f"🌐 Shutting down {server_count} remote server(s) in parallel...")
        else:
            self._log_message(f"🌐 Shutting down 1 remote server...")

        completed = 0

        for phase_idx, key in enumerate(sorted_keys, 1):
            phase_servers = phases[key]
            names = ", ".join(s.name or s.host for s in phase_servers)

            if num_phases > 1:
                self._log_message(f"  📋 Phase {phase_idx}/{num_phases} (order={key}): {names}")

            if len(phase_servers) == 1:
                server = phase_servers[0]
                display_name = server.name or server.host
                try:
                    self._shutdown_remote_server(server)
                    completed += 1
                except Exception as e:
                    self._log_message(f"  ❌ {display_name} shutdown failed: {e}")
            else:
                completed += self._shutdown_servers_parallel(phase_servers)

        # Log summary
        self._log_message(f"  ✅ Remote shutdown complete ({completed}/{server_count} servers)")

    def _shutdown_servers_parallel(self, servers: List[RemoteServerConfig]) -> int:
        """Shutdown multiple remote servers in parallel using threads.

        Returns the number of servers whose threads finished within the
        timeout window (regardless of individual success/failure — per-server
        errors are logged inside _shutdown_remote_server).
        """
        def calc_server_timeout(server: RemoteServerConfig) -> int:
            pre_cmd_time = sum(
                (cmd.timeout or server.command_timeout) for cmd in server.pre_shutdown_commands
            )
            return (
                pre_cmd_time
                + server.command_timeout
                + server.connect_timeout
                + server.shutdown_safety_margin
            )

        max_timeout = max(calc_server_timeout(s) for s in servers)

        def shutdown_server_thread(server: RemoteServerConfig):
            """Thread worker for shutting down a single server."""
            try:
                self._shutdown_remote_server(server)
            except Exception:
                pass  # Errors logged inside _shutdown_remote_server

        threads: List[threading.Thread] = []
        for server in servers:
            t = threading.Thread(
                target=shutdown_server_thread,
                args=(server,),
                name=f"remote-shutdown-{server.name or server.host}"
            )
            t.start()
            threads.append(t)

        # Deadline-based join: cap total wait at max_timeout regardless of
        # how many threads are stuck. Per-thread join() with the same
        # max_timeout would stack to N × max_timeout in the worst case.
        deadline = time.monotonic() + max_timeout
        for t in threads:
            remaining = max(0.0, deadline - time.monotonic())
            t.join(timeout=remaining)

        still_running = [t for t in threads if t.is_alive()]
        if still_running:
            self._log_message(
                f"  ⚠️ {len(still_running)} remote shutdown(s) still in progress "
                "(continuing with next phase)"
            )

        return len(servers) - len(still_running)

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

        if not self.config.behavior.dry_run:
            run_command([
                "wall",
                "🚨 CRITICAL: Executing emergency UPS shutdown sequence NOW!"
            ])

        # Runtime is_local enforcement: only the local UPS group can
        # manage VMs, containers, and filesystems. Non-local groups skip
        # these even if config validation was somehow bypassed.
        group = self.config.ups_groups[0] if self.config.ups_groups else None
        is_local = group.is_local if group else True  # legacy single-UPS = local

        if is_local:
            self._shutdown_vms()
            self._shutdown_containers()
            self._sync_filesystems()
            self._unmount_filesystems()
        self._shutdown_remote_servers()

        if is_local and self.config.filesystems.sync_enabled:
            self._log_message("💾 Final filesystem sync...")
            if self.config.behavior.dry_run:
                self._log_message("  🧪 [DRY-RUN] Would perform final sync")
            else:
                os.sync()
                self._log_message("  ✅ Final sync complete")

        # In coordinator mode, notify the coordinator instead of doing local shutdown
        if self._coordinator_mode:
            self._log_message("✅ ========== GROUP SHUTDOWN SEQUENCE COMPLETE ==========")
            if self._shutdown_callback:
                group = self.config.ups_groups[0] if self.config.ups_groups else None
                self._shutdown_callback(group)
            return

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

            # Exit if --exit-after-shutdown was specified
            if self._exit_after_shutdown:
                self._log_message("🛑 Exiting after shutdown sequence")
                self._cleanup_and_exit(None, None)

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
        if not self.config.behavior.dry_run:
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

    def _check_battery_anomaly(self, ups_data: Dict[str, str]):
        """Detect abnormal battery charge changes while on line power.

        Catches firmware recalibrations, battery aging events, or hardware
        issues that cause sudden charge drops (e.g., 100% -> 60% in seconds)
        while the UPS is on line power and not discharging.

        Uses sustained-reading confirmation: an anomalous drop must persist
        across 3 consecutive polls before firing.  This filters out transient
        firmware jitter that some UPS units (notably APC, CyberPower, and
        Ubiquiti UniFi UPS) exhibit after an OB -> OL transition, where the first
        few readings may report a wildly incorrect charge that self-corrects
        within a couple of seconds.
        """
        ups_status = ups_data.get('ups.status', '')
        battery_charge_str = ups_data.get('battery.charge', '')

        if not is_numeric(battery_charge_str):
            return

        current_charge = float(battery_charge_str)
        current_time = time.time()

        # Only track anomalies while on line power (OL/CHRG)
        if "OB" in ups_status:
            # On battery -- reset tracking, drops are expected
            self.state.last_battery_charge = current_charge
            self.state.last_battery_charge_time = current_time
            self.state.pending_anomaly_charge = -1.0
            self.state.pending_anomaly_count = 0
            return

        prev_charge = self.state.last_battery_charge
        prev_time = self.state.last_battery_charge_time

        # Update tracking
        self.state.last_battery_charge = current_charge
        self.state.last_battery_charge_time = current_time

        # Skip if not yet initialized
        if prev_charge < 0:
            return

        # Check for significant drop while online
        drop = prev_charge - current_charge
        elapsed = current_time - prev_time if prev_time > 0 else 0

        # Threshold: >20% drop within 120 seconds while on line power
        if drop > 20 and elapsed < 120:
            # First detection -- record as pending, wait for confirmation
            self.state.pending_anomaly_charge = current_charge
            self.state.pending_anomaly_prev_charge = prev_charge
            self.state.pending_anomaly_time = current_time
            self.state.pending_anomaly_count = 1
            return

        # Check if a pending anomaly is being confirmed across polls
        if self.state.pending_anomaly_charge >= 0:
            # Charge recovered -- transient jitter, discard the anomaly
            if current_charge > self.state.pending_anomaly_charge + 10:
                self.state.pending_anomaly_charge = -1.0
                self.state.pending_anomaly_count = 0
                return

            # Still low -- increment confirmation counter
            self.state.pending_anomaly_count += 1

            # Need 3 consecutive polls to confirm (filters firmware jitter)
            if self.state.pending_anomaly_count < 3:
                return

            # Confirmed anomaly (sustained across 3 polls)
            anomaly_prev = self.state.pending_anomaly_prev_charge
            anomaly_drop = anomaly_prev - current_charge
            anomaly_elapsed = current_time - self.state.pending_anomaly_time
            self.state.pending_anomaly_charge = -1.0
            self.state.pending_anomaly_count = 0

            self._log_message(
                f"⚠️ WARNING: Battery charge dropped from {anomaly_prev:.0f}% to "
                f"{current_charge:.0f}% ({anomaly_drop:.0f}% drop) while on line power. "
                f"Possible firmware recalibration, battery aging, or hardware issue."
            )
            self._send_notification(
                f"⚠️ **Battery Anomaly Detected**\n"
                f"Charge dropped from {anomaly_prev:.0f}% to {current_charge:.0f}% "
                f"({anomaly_drop:.0f}% drop in {anomaly_elapsed:.0f}s) while on line power.\n"
                f"Possible causes: firmware recalibration, battery aging, or hardware issue.",
                self.config.NOTIFY_WARNING
            )

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
            if not self.config.behavior.dry_run:
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
            if not self.config.behavior.dry_run:
                run_command([
                    "wall",
                    f"✅ Power has been restored. UPS Status: {ups_status}. "
                    f"Battery at {battery_charge}%."
                ])

            self.state.on_battery_start_time = 0
            self.state.extended_time_logged = False
            self.state.battery_history.clear()

    def _handle_connection_failure(self, error_msg: str):
        """Handle connection failure with optional grace period.

        Called only when NOT on battery (failsafe is handled separately).
        Implements the connection loss grace period to suppress notification
        storms from flaky NUT servers.
        """
        grace_cfg = self.config.ups.connection_loss_grace_period

        if not grace_cfg.enabled:
            # Grace period disabled: immediate notification (original behavior)
            if self.state.connection_state != "FAILED":
                if "Data stale" in error_msg:
                    self._log_power_event(
                        "CONNECTION_LOST",
                        f"Data from UPS {self.config.ups.name} is persistently stale "
                        f"(>= {self.config.ups.max_stale_data_tolerance} attempts). "
                        f"Monitoring is inactive."
                    )
                else:
                    self._log_power_event(
                        "CONNECTION_LOST",
                        f"Cannot connect to UPS {self.config.ups.name} "
                        "(Network, Server, or Config error). Monitoring is inactive."
                    )
                self.state.connection_state = "FAILED"
            return

        # Grace period enabled
        if self.state.connection_state == "OK":
            # First failure: enter grace period
            self.state.connection_state = "GRACE_PERIOD"
            self.state.connection_lost_time = time.time()
            if "Data stale" in error_msg:
                self._log_message(
                    f"⚠️ Connection to UPS {self.config.ups.name} lost "
                    f"(data stale). Grace period started "
                    f"({grace_cfg.duration}s)."
                )
            else:
                self._log_message(
                    f"⚠️ Connection to UPS {self.config.ups.name} lost. "
                    f"Grace period started ({grace_cfg.duration}s)."
                )

        elif self.state.connection_state == "GRACE_PERIOD":
            elapsed = time.time() - self.state.connection_lost_time
            if elapsed >= grace_cfg.duration:
                # Grace period expired: fire full notification
                if "Data stale" in error_msg:
                    self._log_power_event(
                        "CONNECTION_LOST",
                        f"Data from UPS {self.config.ups.name} is persistently stale "
                        f"(>= {self.config.ups.max_stale_data_tolerance} attempts). "
                        f"Monitoring is inactive. "
                        f"(Grace period {grace_cfg.duration}s expired)"
                    )
                else:
                    self._log_power_event(
                        "CONNECTION_LOST",
                        f"Cannot connect to UPS {self.config.ups.name} "
                        "(Network, Server, or Config error). Monitoring is inactive. "
                        f"(Grace period {grace_cfg.duration}s expired)"
                    )
                self.state.connection_state = "FAILED"
                self.state.connection_lost_time = 0.0

        # If connection_state == "FAILED": already notified, nothing to do

    def _main_loop(self):
        """Main monitoring loop."""
        while not self._stop_event.is_set():
            success, ups_data, error_msg = self._get_all_ups_data()

            # ==================================================================
            # CONNECTION HANDLING AND FAILSAFE
            # ==================================================================

            if not success:
                is_failsafe_trigger = False

                if "Data stale" in error_msg:
                    self.state.stale_data_count += 1
                    if self.state.connection_state not in ("FAILED", "GRACE_PERIOD"):
                        self._log_message(
                            f"⚠️ WARNING: Data stale from UPS {self.config.ups.name} "
                            f"(Attempt {self.state.stale_data_count}/{self.config.ups.max_stale_data_tolerance})."
                        )

                    if self.state.stale_data_count >= self.config.ups.max_stale_data_tolerance:
                        is_failsafe_trigger = True
                else:
                    if self.state.connection_state not in ("FAILED", "GRACE_PERIOD"):
                        self._log_message(
                            f"❌ ERROR: Cannot connect to UPS {self.config.ups.name}. Output: {error_msg}"
                        )
                    self.state.stale_data_count = 0
                    is_failsafe_trigger = True

                # FAILSAFE: If connection lost while on battery, shutdown immediately
                # This is NEVER affected by the grace period
                if is_failsafe_trigger and "OB" in self.state.previous_status:
                    self.state.connection_state = "FAILED"
                    self.state.connection_lost_time = 0.0
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

                # Grace period logic (only when NOT on battery)
                elif is_failsafe_trigger:
                    self._handle_connection_failure(error_msg)

                self._stop_event.wait(5)
                continue

            # ==================================================================
            # DATA PROCESSING
            # ==================================================================

            self.state.stale_data_count = 0

            if self.state.connection_state == "GRACE_PERIOD":
                # Recovered during grace period: quiet recovery, no notification
                elapsed = time.time() - self.state.connection_lost_time
                self._log_message(
                    f"✅ Connection to UPS {self.config.ups.name} recovered during "
                    f"grace period ({elapsed:.0f}s elapsed). No notification sent."
                )
                self.state.connection_state = "OK"
                self.state.connection_lost_time = 0.0

                # Flap detection with 24h TTL
                now = time.time()
                if (self.state.connection_flap_count > 0
                        and (now - self.state.connection_first_flap_time) > 86400):
                    self.state.connection_flap_count = 0
                if self.state.connection_flap_count == 0:
                    self.state.connection_first_flap_time = now
                self.state.connection_flap_count += 1

                grace_cfg = self.config.ups.connection_loss_grace_period
                if self.state.connection_flap_count >= grace_cfg.flap_threshold:
                    self._log_message(
                        f"⚠️ NUT server is unstable: connection to UPS {self.config.ups.name} "
                        f"has flapped {self.state.connection_flap_count} times."
                    )
                    self._send_notification(
                        f"⚠️ **NUT Server Unstable**\n"
                        f"Connection to UPS {self.config.ups.name} has flapped "
                        f"{self.state.connection_flap_count} times "
                        f"(recovered within grace period each time). "
                        f"Check your UPS network connection or NUT server configuration.",
                        self.config.NOTIFY_WARNING
                    )
                    self.state.connection_flap_count = 0
                    self.state.connection_first_flap_time = 0.0

            elif self.state.connection_state == "FAILED":
                self._log_power_event(
                    "CONNECTION_RESTORED",
                    f"Connection to UPS {self.config.ups.name} restored. Monitoring is active."
                )
                self.state.connection_state = "OK"
                self.state.connection_lost_time = 0.0
                self.state.connection_flap_count = 0
                self.state.connection_first_flap_time = 0.0

            ups_status = ups_data.get('ups.status', '')

            if not ups_status:
                self._log_message(
                    "❌ ERROR: Received data from UPS but 'ups.status' is missing. "
                    "Check NUT configuration."
                )
                self._stop_event.wait(5)
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

            # Battery anomaly detection (runs on every cycle with valid data)
            self._check_battery_anomaly(ups_data)

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

            self._stop_event.wait(self.config.ups.check_interval)


# ==============================================================================
# MULTI-UPS COORDINATOR
# ==============================================================================

class MultiUPSCoordinator:
    """Coordinates multiple UPSGroupMonitor threads for multi-UPS setups.

    Each UPS group runs its own UPSGroupMonitor in a dedicated thread. The
    coordinator owns shared resources (notifications, logger) and handles
    local shutdown coordination with defense-in-depth (threading.Lock +
    filesystem flag).
    """

    def __init__(self, config: Config, exit_after_shutdown: bool = False):
        self.config = config
        self._exit_after_shutdown = exit_after_shutdown
        self._monitors: List[UPSGroupMonitor] = []
        self._threads: List[threading.Thread] = []
        self._stop_event = threading.Event()
        self._local_shutdown_lock = threading.Lock()
        self._local_shutdown_initiated = False
        self._global_shutdown_flag = Path(config.logging.shutdown_flag_file)

        # Shared resources
        self._logger: Optional[UPSLogger] = None
        self._notification_worker: Optional[NotificationWorker] = None

    def run(self):
        """Start all UPS group monitors and wait for shutdown or signal."""
        try:
            self._initialize()
            self._start_monitors()
            self._wait_for_completion()
        except KeyboardInterrupt:
            self._handle_signal(signal.SIGINT, None)

    def _initialize(self):
        """Initialize shared resources."""
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

        self._logger = UPSLogger(self.config.logging.file, self.config)

        if self.config.logging.file:
            try:
                Path(self.config.logging.file).touch(exist_ok=True)
            except PermissionError:
                pass

        self._global_shutdown_flag.unlink(missing_ok=True)

        # Initialize shared notification worker
        if self.config.notifications.enabled and APPRISE_AVAILABLE:
            self._notification_worker = NotificationWorker(self.config)
            if self._notification_worker.start():
                count = self._notification_worker.get_service_count()
                self._log(f"📢 Notifications: enabled ({count} service(s))")
            else:
                self._log("⚠️ WARNING: Failed to initialize notifications")
                self._notification_worker = None

        group_count = len(self.config.ups_groups)
        self._log(f"🚀 Eneru v{__version__} starting - multi-UPS mode ({group_count} groups)")

        if self.config.behavior.dry_run:
            self._log("🧪 *** RUNNING IN DRY-RUN MODE - NO ACTUAL SHUTDOWN WILL OCCUR ***")

    def _start_monitors(self):
        """Create and start one UPSGroupMonitor thread per group."""
        for group in self.config.ups_groups:
            # Build a single-group Config for this monitor
            group_config = Config(
                ups_groups=[group],
                behavior=self.config.behavior,
                logging=self.config.logging,
                notifications=self.config.notifications,
                local_shutdown=self.config.local_shutdown,
            )

            # Sanitize UPS name for file paths
            sanitized = group.ups.name.replace("@", "-").replace(":", "-").replace("/", "-")
            prefix = f"[{group.ups.label}] "

            monitor = UPSGroupMonitor(
                config=group_config,
                exit_after_shutdown=self._exit_after_shutdown,
                coordinator_mode=True,
                shutdown_callback=self._on_group_shutdown,
                stop_event=self._stop_event,
                log_prefix=prefix,
                notification_worker=self._notification_worker,
                logger=self._logger,
                state_file_suffix=sanitized,
            )
            self._monitors.append(monitor)

            thread = threading.Thread(
                target=self._run_monitor,
                args=(monitor, group),
                name=f"ups-{sanitized}",
                daemon=True,
            )
            self._threads.append(thread)
            thread.start()
            self._log(f"  Started monitor thread for {group.ups.label}"
                      f"{' [is_local]' if group.is_local else ''}")

    def _run_monitor(self, monitor: UPSGroupMonitor, group):
        """Thread target: run a single UPS monitor."""
        try:
            monitor.run()
        except Exception as e:
            label = group.ups.label
            self._log(f"❌ Monitor thread for {label} crashed: {e}")
            if self._notification_worker:
                self._notification_worker.send(
                    f"❌ **Monitor Crashed:** {label}\nError: {e}",
                    "failure",
                )

    def _on_group_shutdown(self, group):
        """Called by a UPS monitor when its group triggers shutdown."""
        if group is None:
            return

        label = group.ups.label
        is_local = group.is_local
        should_local_shutdown = False

        if is_local:
            should_local_shutdown = True
        elif self.config.local_shutdown.trigger_on == "any":
            has_any_local = any(g.is_local for g in self.config.ups_groups)
            if not has_any_local:
                should_local_shutdown = True

        if should_local_shutdown:
            self._handle_local_shutdown(label)
        elif self._exit_after_shutdown:
            # Non-local group shutdown completed, exit if requested
            self._log(f"🛑 Group {label} shutdown complete. Exiting (--exit-after-shutdown).")
            self._stop_event.set()

    def _handle_local_shutdown(self, triggered_by: str):
        """Execute local shutdown with defense-in-depth protection."""
        # Defense layer 1: in-memory lock
        proceed = False
        with self._local_shutdown_lock:
            if not self._local_shutdown_initiated:
                self._local_shutdown_initiated = True
                proceed = True

        if not proceed:
            return

        # Defense layer 2: filesystem flag
        self._global_shutdown_flag.touch()

        self._log(f"🚨 Local shutdown triggered by {triggered_by}")

        # Drain other groups if configured
        if self.config.local_shutdown.drain_on_local_shutdown:
            self._log("⏳ Draining all UPS groups before local shutdown...")
            self._drain_all_groups(timeout=120)

        # Execute local shutdown
        if self.config.local_shutdown.enabled:
            self._log("🔌 Shutting down local server NOW")
            if self.config.behavior.dry_run:
                self._log(f"🧪 [DRY-RUN] Would execute: {self.config.local_shutdown.command}")
                self._global_shutdown_flag.unlink(missing_ok=True)
            else:
                if self._notification_worker:
                    self._notification_worker.send(
                        "🛑 **Shutdown Sequence Complete**\nShutting down local server NOW.",
                        "failure",
                    )
                time.sleep(5)
                cmd_parts = self.config.local_shutdown.command.split()
                if self.config.local_shutdown.message:
                    cmd_parts.append(self.config.local_shutdown.message)
                run_command(cmd_parts)
        else:
            self._log("✅ Local shutdown disabled. Group shutdown complete.")
            self._global_shutdown_flag.unlink(missing_ok=True)

        # Exit if --exit-after-shutdown was specified
        if self._exit_after_shutdown:
            self._log("🛑 Exiting after shutdown sequence (--exit-after-shutdown)")
            self._stop_event.set()

    def _drain_all_groups(self, timeout: int = 120):
        """Shut down all groups' resources, then stop monitor threads.

        This triggers each monitor's shutdown sequence (VMs, containers,
        remote servers) before stopping the monitoring loops. Resources
        are actively shut down, not just abandoned.
        """
        self._log("⏳ Draining all UPS groups -- shutting down their resources...")

        # First, trigger shutdown on each monitor that hasn't already shut down
        for monitor in self._monitors:
            if not monitor._shutdown_flag_path.exists():
                self._log(f"  ➡️ Triggering shutdown for {monitor._log_prefix.strip()}")
                try:
                    monitor._execute_shutdown_sequence()
                except Exception as e:
                    self._log(f"  ⚠️ Error during drain shutdown: {e}")

        # Then stop the monitoring loops
        self._stop_event.set()
        deadline = time.time() + timeout
        for thread in self._threads:
            remaining = max(0, deadline - time.time())
            thread.join(timeout=remaining)
        still_running = [t for t in self._threads if t.is_alive()]
        if still_running:
            self._log(f"⚠️ {len(still_running)} monitor(s) still running after drain timeout")

    def _wait_for_completion(self):
        """Block until all monitors finish or a signal is received."""
        try:
            while not self._stop_event.is_set():
                # Check if any thread is still alive
                alive = [t for t in self._threads if t.is_alive()]
                if not alive:
                    break
                self._stop_event.wait(1)
        except KeyboardInterrupt:
            self._handle_signal(signal.SIGINT, None)

    def _handle_signal(self, signum: int, frame):
        """Handle SIGTERM/SIGINT for clean shutdown."""
        self._log("🛑 Service stopped by signal (SIGTERM/SIGINT). Monitoring is inactive.")

        if self._notification_worker:
            self._notification_worker.send(
                "🛑 **Eneru Service Stopped**\nMonitoring is now inactive.",
                "warning",
            )

        self._stop_event.set()

        # Wait briefly for threads to finish
        for thread in self._threads:
            thread.join(timeout=5)

        if self._notification_worker:
            self._notification_worker.stop()

        self._global_shutdown_flag.unlink(missing_ok=True)
        sys.exit(0)

    def _log(self, message: str):
        """Log a message using the shared logger."""
        if self._logger:
            self._logger.log(message)
        else:
            tz_name = time.strftime('%Z')
            timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            print(f"{timestamp} {tz_name} - {message}")
