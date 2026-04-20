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

from eneru.version import __version__
from eneru.config import Config, RemoteServerConfig
from eneru.state import MonitorState
from eneru.logger import UPSLogger
from eneru.notifications import NotificationWorker, APPRISE_AVAILABLE
from eneru.stats import StatsStore, StatsWriter
from eneru.utils import run_command, command_exists, is_numeric, format_seconds
from eneru.shutdown.vms import VMShutdownMixin
from eneru.shutdown.containers import ContainerShutdownMixin
from eneru.shutdown.filesystems import FilesystemShutdownMixin
from eneru.shutdown.remote import RemoteShutdownMixin
from eneru.health.voltage import VoltageMonitorMixin
from eneru.health.battery import BatteryMonitorMixin

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


class UPSGroupMonitor(
    VMShutdownMixin,
    ContainerShutdownMixin,
    FilesystemShutdownMixin,
    RemoteShutdownMixin,
    VoltageMonitorMixin,
    BatteryMonitorMixin,
):
    """Main UPS Monitor class."""

    def __init__(self, config: Config, exit_after_shutdown: bool = False,
                 coordinator_mode: bool = False,
                 shutdown_callback=None,
                 stop_event: Optional[threading.Event] = None,
                 log_prefix: str = "",
                 notification_worker: Optional[NotificationWorker] = None,
                 logger: Optional[UPSLogger] = None,
                 state_file_suffix: str = "",
                 in_redundancy_group: bool = False):
        self.config = config
        self.state = MonitorState()
        self.logger: Optional[UPSLogger] = logger
        self._coordinator_mode = coordinator_mode
        self._shutdown_callback = shutdown_callback
        self._stop_event = stop_event or threading.Event()
        self._log_prefix = log_prefix
        # When True, per-UPS triggers (T1-T4, FSD, FAILSAFE) become advisory:
        # they record state under the snapshot lock instead of executing the
        # local shutdown sequence. The redundancy-group evaluator owns the
        # decision to actually drain shared resources.
        self._in_redundancy_group = bool(in_redundancy_group)

        # Per-group state file paths (suffix for multi-UPS)
        sfx = f".{state_file_suffix}" if state_file_suffix else ""
        self._shutdown_flag_path = Path(config.logging.shutdown_flag_file + sfx)
        self._battery_history_path = Path(config.logging.battery_history_file + sfx)
        self._state_file_path = Path(config.logging.state_file + sfx)

        self._container_runtime: Optional[str] = None
        self._compose_available: bool = False
        self._notification_worker: Optional[NotificationWorker] = notification_worker
        self._exit_after_shutdown = exit_after_shutdown

        # Per-UPS SQLite stats store (always-on; per spec 2.12).
        # Filename mirrors the per-group state-file sanitisation so each
        # UPS keeps its own database next to its sibling state files.
        sanitized = sfx.lstrip(".") or "default"
        stats_dir = Path(config.statistics.db_directory)
        self._stats_db_path = stats_dir / f"{sanitized}.db"
        self._stats_store = StatsStore(
            self._stats_db_path,
            retention_raw_hours=config.statistics.retention.raw_hours,
            retention_5min_days=config.statistics.retention.agg_5min_days,
            retention_hourly_days=config.statistics.retention.agg_hourly_days,
        )
        self._stats_writer: Optional[StatsWriter] = None

    def _start_stats(self):
        """Open the per-UPS stats DB and start the background writer.

        SQLite errors are isolated -- a failure here logs once and the
        daemon continues to run without stats persistence.
        """
        try:
            self._stats_store._logger = self.logger
            self._stats_store.open()
        except Exception as e:
            self._log_message(
                f"⚠️ WARNING: stats store open failed at {self._stats_db_path}: {e}. "
                "Stats persistence disabled this run."
            )
            self._stats_writer = None
            return
        self._stats_writer = StatsWriter(
            self._stats_store, self._stop_event,
            log_prefix=self._log_prefix,
        )
        self._stats_writer.start()
        self._stats_store.log_event(
            "DAEMON_START",
            f"Eneru v{__version__} monitoring {self.config.ups.name}",
        )

    def _stop_stats(self):
        """Flush + close the stats store. Safe to call multiple times."""
        if self._stats_writer is not None:
            self._stats_writer = None  # daemon thread; stop_event was set
        try:
            self._stats_store.close()
        except Exception:
            pass

    def _record_advisory_trigger(self, reason: str):
        """Record an advisory trigger for the redundancy-group evaluator.

        Called from the per-UPS trigger sites (T1-T4, FSD, FAILSAFE) when
        this monitor's UPS belongs to a redundancy group. The state lock
        guarantees the snapshot reader sees a consistent
        ``(trigger_active, trigger_reason)`` pair.
        """
        with self.state._lock:
            already_active = self.state.trigger_active
            self.state.trigger_active = True
            self.state.trigger_reason = reason
        if not already_active:
            self._log_message(
                f"⚠️ Trigger condition met (advisory, redundancy group): {reason}"
            )

    def _clear_advisory_trigger(self):
        """Clear the advisory trigger when conditions return to normal."""
        with self.state._lock:
            was_active = self.state.trigger_active
            self.state.trigger_active = False
            self.state.trigger_reason = ""
        if was_active:
            self._log_message(
                "✅ Advisory trigger cleared (redundancy group): conditions normal."
            )

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
        self._start_stats()

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

    def _log_power_event(self, event: str, details: str,
                         *, suppress_notification: bool = False):
        """Log power events with centralized notification logic.

        ``suppress_notification`` (kw-only) lets a caller explicitly
        opt out of the notification dispatch -- used by the voltage
        hysteresis path which logs the BROWNOUT/OVER_VOLTAGE row
        immediately on the state transition and fires the notification
        later (after the dwell timer elapses) via a separate code
        path. Stats persistence and syslog still happen.

        ``notifications.suppress`` (config) provides the user-facing
        per-event-type mute. Logs always record the event; only the
        notification dispatch is gated. Safety-critical events
        (over-voltage, brownout, overload, bypass-active, on-battery,
        connection-lost, anything starting with SHUTDOWN) are
        validation-rejected from ``suppress`` so they cannot be
        silenced here even if a user tried.
        """
        self._log_message(f"⚡ POWER EVENT: {event} - {details}")

        try:
            run_command([
                "logger", "-t", "ups-monitor", "-p", "daemon.warning",
                f"⚡ POWER EVENT: {event} - {details}"
            ])
        except Exception:
            pass

        # Determine notification disposition first so we can record an
        # accurate notification_sent flag in the stats events row.
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

        # Reasons the notification dispatch may be skipped (the log row
        # always lands; this only affects whether the operator gets
        # pinged by Apprise). The stats events row records which path
        # we took via notification_sent.
        skipped_during_shutdown = self._shutdown_flag_path.exists()
        always_silent = event in ("VOLTAGE_NORMALIZED", "AVR_INACTIVE",
                                   "VOLTAGE_FLAP_SUPPRESSED",
                                   "VOLTAGE_AUTODETECT_MISMATCH")
        user_suppressed = event in set(
            getattr(self.config.notifications, "suppress", []) or []
        )

        will_notify = not (suppress_notification or skipped_during_shutdown
                           or always_silent or user_suppressed)

        # Persist the event to the SQLite store (best-effort). Pass the
        # disposition so `WHERE notification_sent = 0` can audit muted
        # events even after the fact.
        try:
            self._stats_store.log_event(
                event, details, notification_sent=will_notify,
            )
        except Exception:
            pass

        if not will_notify:
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

    def _save_state(self, ups_data: Dict[str, str]):
        """Save current UPS state to file + buffer one stats sample."""
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

        # Hot-path: append one sample to the in-memory deque (zero I/O).
        # The StatsWriter flushes the deque to SQLite every 10 s.
        try:
            time_on_battery = (
                int(time.time()) - self.state.on_battery_start_time
                if self.state.on_battery_start_time > 0 else 0
            )
            self._stats_store.buffer_sample(
                ups_data,
                depletion_rate=self.state.latest_depletion_rate,
                time_on_battery=time_on_battery,
                connection_state=self.state.connection_state,
            )
        except Exception:
            pass  # never let stats interfere with monitoring

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
            self._stop_stats()
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
        self._stop_stats()

        self._shutdown_flag_path.unlink(missing_ok=True)
        sys.exit(0)

    # ==========================================================================
    # STATUS CHECKS
    # ==========================================================================

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

        # Publish the freshly computed depletion_rate for the snapshot reader.
        with self.state._lock:
            self.state.latest_depletion_rate = (
                float(depletion_rate) if is_numeric(depletion_rate) else 0.0
            )

        if shutdown_reason:
            if self._in_redundancy_group:
                # Advisory mode: the redundancy-group evaluator decides whether
                # the group should drain. Per-UPS shutdown is suppressed.
                self._record_advisory_trigger(shutdown_reason)
            else:
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

            # Power restored on a redundancy-group member: drop any advisory
            # trigger so the group evaluator sees this UPS as healthy again.
            if self._in_redundancy_group:
                self._clear_advisory_trigger()

        # Off-battery → no depletion-rate signal for the snapshot reader.
        with self.state._lock:
            self.state.latest_depletion_rate = 0.0

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
                    if self._in_redundancy_group:
                        # Advisory mode: redundancy-group evaluator owns the
                        # shutdown decision. Recording the trigger here +
                        # reporting connection_state=FAILED is enough -- the
                        # group's policy + min_healthy decide what happens.
                        self._record_advisory_trigger(
                            "FAILSAFE (FSB): connection lost while On Battery"
                        )
                        self._log_message(
                            "🚨 FAILSAFE (advisory, redundancy group): connection lost "
                            "while On Battery; deferring to group evaluator."
                        )
                    else:
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
                if self._in_redundancy_group:
                    self._clear_advisory_trigger()

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
                if self._in_redundancy_group:
                    self._record_advisory_trigger(
                        "UPS signaled FSD (Forced Shutdown) flag."
                    )
                else:
                    self._trigger_immediate_shutdown(
                        "UPS signaled FSD (Forced Shutdown) flag."
                    )

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

            # Publish the latest observation for redundancy-group evaluators.
            # All snapshot fields (plus previous_status) are written under one
            # lock acquisition so external readers see a consistent snapshot.
            with self.state._lock:
                self.state.latest_status = ups_status
                self.state.latest_battery_charge = ups_data.get('battery.charge', '')
                self.state.latest_runtime = ups_data.get('battery.runtime', '')
                self.state.latest_load = ups_data.get('ups.load', '')
                self.state.latest_time_on_battery = (
                    int(time.time()) - self.state.on_battery_start_time
                    if self.state.on_battery_start_time > 0 else 0
                )
                self.state.latest_update_time = time.time()
                self.state.previous_status = ups_status

            self._stop_event.wait(self.config.ups.check_interval)
