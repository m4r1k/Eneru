#!/usr/bin/env python3
"""
Eneru - Generic UPS Monitoring and Shutdown Management
Monitors UPS status via NUT and triggers configurable shutdown sequences.
https://github.com/m4r1k/Eneru
"""

import sqlite3
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
from eneru.deferred_delivery import schedule_deferred_stop_or_eager_send
from eneru.lifecycle import (
    EVENT_TYPE_DAEMON_START,
    REASON_FATAL,
    REASON_SEQUENCE_COMPLETE,
    REASON_SIGNAL,
    classify_event_type,
    classify_startup,
    coalesce_recovered_with_prev_shutdown,
    delete_shutdown_marker,
    delete_upgrade_marker,
    read_shutdown_marker,
    read_upgrade_marker,
    write_shutdown_marker,
)
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
                 power_restored_callback=None,
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
        # Coordinator-supplied hook fired on the OB/FSD->OL transition so
        # the coordinator can re-arm its own _local_shutdown_initiated
        # lock + global flag (the per-monitor flag we clear locally is
        # suffixed and lives separately). Bug #4 / 5.2.2.
        self._power_restored_callback = power_restored_callback
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
        """Open the per-UPS stats DB (if not already open) and start the
        background writer.

        v5.2: ``_initialize_notifications`` opens the store earlier so
        the notification worker can persist messages from the very first
        notification. We keep the open() call here for the
        ``notifications.enabled=False`` path where _initialize_notifications
        skips the open. SQLite errors are isolated — a failure here
        logs once and the daemon continues without stats persistence.
        """
        try:
            if self._stats_store._conn is None:
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
                self.config.NOTIFY_FAILURE,
                category="lifecycle",
            )
            # Slice 3: tag this exit so the next start emits
            # "🚀 Restarted (last instance exited fatally)" rather than
            # a generic Started.
            try:
                from pathlib import Path
                write_shutdown_marker(
                    Path(self.config.statistics.db_directory),
                    version=__version__, reason=REASON_FATAL,
                )
            except Exception:
                # Marker write must never mask the original FATAL.
                pass
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
        # Always invoke — the helper updates meta.last_seen_version for
        # next-start upgrade detection. The actual SEND is suppressed in
        # coordinator mode because the coordinator emits ONE classified
        # lifecycle notification at the multi_ups level (per-monitor
        # sends would be N copies of the same event).
        self._emit_lifecycle_startup_notification()

        if self.config.behavior.dry_run:
            self._log_message("🧪 *** RUNNING IN DRY-RUN MODE - NO ACTUAL SHUTDOWN WILL OCCUR ***")

        self._log_enabled_features()
        self._wait_for_initial_connection()
        self._initialize_voltage_thresholds()
        self._start_stats()

    def _initialize_notifications(self):
        """Initialize the notification worker.

        v5.2 wires the worker to the per-UPS stats DB so notifications
        can be persisted and replayed across restarts. The store is
        opened early (here) — earlier than ``_start_stats()`` — because
        the very first notification (the lifecycle "Started" / "Recovered"
        message) fires before stats opens by the legacy ordering, and
        we want it persisted too.

        In coordinator mode the worker is created externally and shared;
        we still open + register our store so the shared worker can pick
        up our pending rows on the next iteration.
        """
        # Open the per-UPS stats store now (idempotent; _start_stats
        # will skip the open() call if it sees us already opened it).
        try:
            if self._stats_store._conn is None:
                self._stats_store._logger = self.logger
                self._stats_store.open()
        except Exception as e:
            self._log_message(
                f"⚠️ WARNING: stats store open failed at "
                f"{self._stats_db_path}: {e}. Notifications will not "
                "persist across restarts."
            )

        # v5.2.1: cancel any pending lifecycle row left by the previous
        # instance BEFORE the worker can deliver it. The supersede block
        # in _emit_lifecycle_startup_notification (lines ~405-407) is
        # the canonical location, but it runs AFTER worker.start() +
        # register_store() — opening a delivery-race window where the
        # worker could ship the deferred 'Service Stopped' from the
        # prior daemon before the classifier has a chance to cancel it
        # (Cubic P2). Doing the cancel here too is idempotent: by the
        # time the lifecycle classifier runs, there's nothing left.
        # Best-effort: a transient sqlite error here just means the
        # late cancel still has work to do — same outcome as v5.2.0
        # in that worst case.
        if self._stats_store._conn is not None:
            try:
                for row in self._stats_store.find_pending_by_category(
                        "lifecycle"):
                    self._stats_store.cancel_notification(
                        row[0], "superseded")
            except (sqlite3.Error, OSError) as e:
                self._log_message(
                    f"⚠️ WARNING: pre-worker lifecycle sweep failed: {e}"
                )

        if self._notification_worker is not None:
            # Coordinator mode: register our store with the shared worker.
            if self._stats_store._conn is not None:
                self._notification_worker.register_store(self._stats_store)
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
            if self._stats_store._conn is not None:
                self._notification_worker.register_store(self._stats_store)
            service_count = self._notification_worker.get_service_count()
            self._log_message(f"📢 Notifications: enabled ({service_count} service(s))")
        else:
            self._log_message("⚠️ WARNING: Failed to initialize notifications")
            self.config.notifications.enabled = False

    def _emit_lifecycle_startup_notification(self):
        """Update lifecycle meta + (single-UPS only) emit the classified
        startup notification.

        Replaces the v5.1 unconditional ``🚀 Started`` so the user sees:
        - ``📦 Upgraded vX → vY`` after a deb/rpm upgrade
        - ``📊 Recovered`` after a power-loss-triggered shutdown
        - ``🔄 Restarted`` after a quick `systemctl restart`
        - ``🚀 Started (after crash)`` if the previous instance died
          without writing its marker
        - ``🚀 Started`` on a fresh install

        In coordinator mode the coordinator emits the single classified
        notification at the multi_ups level (firing per monitor would be
        N copies of the same event); we still update
        ``meta.last_seen_version`` here so the next-start pip-path
        upgrade detector has its data.
        """
        # Read the PREVIOUS last_seen_version BEFORE we overwrite it
        # with the current run's version — the classifier needs the
        # delta to detect a pip-path upgrade. After classification, set
        # to current so the NEXT start sees this run's version as
        # "previous".
        last_seen = (self._stats_store.get_meta("last_seen_version")
                     if self._stats_store._conn is not None else None)
        if self._stats_store._conn is not None:
            self._stats_store.set_meta("last_seen_version", __version__)

        if self._coordinator_mode:
            return

        from pathlib import Path
        stats_dir = Path(self.config.statistics.db_directory)
        shutdown_marker = read_shutdown_marker(stats_dir)
        upgrade_marker = read_upgrade_marker(stats_dir)

        body, notify_type = classify_startup(
            current_version=__version__,
            shutdown_marker=shutdown_marker,
            upgrade_marker=upgrade_marker,
            last_seen_version=last_seen,
        )

        # Mirror the classification into the stats events table so the
        # TUI's --events-only view (and any sqlite3 / Grafana query)
        # carries the same lifecycle taxonomy as the user-facing
        # notification. _start_stats already inserts a DAEMON_START on
        # every boot, so we only insert here when the classification is
        # something more informative than "fresh start".
        event_type = classify_event_type(
            current_version=__version__,
            shutdown_marker=shutdown_marker,
            upgrade_marker=upgrade_marker,
            last_seen_version=last_seen,
        )
        if (event_type != EVENT_TYPE_DAEMON_START
                and self._stats_store._conn is not None):
            try:
                self._stats_store.log_event(event_type, body)
            except Exception:
                pass  # never mask a startup notification on a stats hiccup

        # Cancel any pending lifecycle rows from the previous instance —
        # they're superseded by the new classification (Restarted folds
        # the previous "Stopped", Recovered folds the previous "Stopped",
        # etc.). Keeps the user from seeing ghost messages on the next
        # successful delivery.
        if self._stats_store._conn is not None:
            for row in self._stats_store.find_pending_by_category("lifecycle"):
                self._stats_store.cancel_notification(row[0], "superseded")

        # Slice 4 bonus: when this start is "Recovered" (reason was
        # sequence_complete), fold the previous instance's pending
        # shutdown headline + summary into ONE richer message. Saves the
        # user from seeing 3 messages (headline + summary + recovered)
        # for what's really a single power-outage round trip.
        if (shutdown_marker
                and shutdown_marker.get("reason") == REASON_SEQUENCE_COMPLETE
                and self._stats_store._conn is not None):
            import time as _time
            try:
                marker_shutdown_at = int(shutdown_marker.get("shutdown_at", 0))
            except (TypeError, ValueError):
                marker_shutdown_at = 0
            downtime = max(0, int(_time.time()) - marker_shutdown_at)
            coalesced_body = coalesce_recovered_with_prev_shutdown(
                self._stats_store,
                downtime_secs=downtime,
                # Bound the coalesce to the current outage so unrelated
                # older pending shutdown rows aren't cancelled (Cubic P2).
                shutdown_at=marker_shutdown_at or None,
            )
            if coalesced_body:
                body = coalesced_body

        # Markers consumed — drop them now so a crash on the next line
        # doesn't replay this classification on the start after that.
        delete_shutdown_marker(stats_dir)
        delete_upgrade_marker(stats_dir)

        self._send_notification(body, notify_type, category="lifecycle")

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

    def _send_notification(self, body: str, notify_type: str = "info",
                           blocking: bool = False,
                           category: str = "general"):
        """Queue a notification via the persistent notification worker.

        v5.2 change: notifications are inserted as ``pending`` rows in
        the per-UPS stats DB before delivery is attempted. The worker
        thread reads/writes through the DB, so messages survive process
        death and prolonged endpoint outages — see ``notifications.py``
        for the full architecture.

        Args:
            body: Notification body text.
            notify_type: One of 'info', 'success', 'warning', 'failure'.
            blocking: Back-compat shim. The v5.2 queue is always
                asynchronous (delivery happens on the worker thread).
                The flag is accepted for API stability but ignored.
            category: Coarse classification used by Slice 4 coalescing
                and per-category queries. Common values: ``lifecycle``,
                ``power_event``, ``voltage``, ``shutdown``,
                ``shutdown_summary``, ``general`` (default).
        """
        del blocking  # see docstring
        if not self._notification_worker:
            return None

        # Prefix notification body with UPS name in multi-UPS mode.
        prefixed_body = f"{self._log_prefix}{body}" if self._log_prefix else body

        # Escape @ symbols to prevent Discord mentions (e.g., UPS@192.168.1.1)
        escaped_body = prefixed_body.replace("@", "@\u200B")  # Zero-width space after @

        return self._notification_worker.send(
            body=escaped_body,
            notify_type=notify_type,
            category=category,
            store=self._stats_store,
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
                # syslog identifier renamed from the legacy "ups-monitor"
                # to "eneru" — the package + service rebrand happened in
                # v5.0 but this side-channel was missed, so power events
                # showed up under a different identifier than every
                # other journal line emitted by the daemon.
                "logger", "-t", "eneru", "-p", "daemon.warning",
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
            # Sub-typed categories for the Slice 4 brief-outage coalescer:
            # the worker pairs on_battery + on_line by exact category match
            # so it doesn't have to grep the user-visible body strings
            # (which can change wording without anyone updating the
            # coalescer). Other power events stay on the generic category.
            event_to_category = {
                "ON_BATTERY": "power_event_on_battery",
                "POWER_RESTORED": "power_event_on_line",
            }
            category = event_to_category.get(event, "power_event")
            self._send_notification(*notification, category=category)

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
            # with_name(name + '.tmp') appends '.tmp' to the full filename;
            # with_suffix('.tmp') would replace the per-UPS suffix
            # (e.g. '.ups1') and collapse every monitor's temp file onto
            # a shared 'ups-state.tmp', racing on the atomic rename.
            temp_file = self._state_file_path.with_name(
                self._state_file_path.name + '.tmp'
            )
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
        sequence_start = time.monotonic()

        self._log_message("🚨 INITIATING EMERGENCY SHUTDOWN SEQUENCE")

        if self.config.behavior.dry_run:
            self._log_message("🧪 *** DRY-RUN MODE: No actual shutdown will occur ***")

        if self.config.local_shutdown.wall and not self.config.behavior.dry_run:
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
            self._log_message("✅ GROUP SHUTDOWN SEQUENCE COMPLETE")
            if self._shutdown_callback:
                group = self.config.ups_groups[0] if self.config.ups_groups else None
                self._shutdown_callback(group)
            return

        elapsed = int(time.monotonic() - sequence_start)

        # Mirror the sequence completion into the events table (regardless
        # of local_shutdown.enabled) so the TUI's --events-only view
        # carries the elapsed-time row between EMERGENCY_SHUTDOWN_INITIATED
        # and the next start's DAEMON_RECOVERED.
        try:
            self._stats_store.log_event(
                "SHUTDOWN_SEQUENCE_COMPLETE", f"elapsed: {elapsed}s",
            )
        except Exception:
            pass

        if self.config.local_shutdown.enabled:
            self._log_message("🔌 Shutting down local server NOW")
            self._log_message("✅ SHUTDOWN SEQUENCE COMPLETE")

            if self.config.behavior.dry_run:
                self._log_message(f"🧪 [DRY-RUN] Would execute: {self.config.local_shutdown.command}")
                self._log_message("🧪 [DRY-RUN] Shutdown sequence completed successfully (no actual shutdown)")
                self._shutdown_flag_path.unlink(missing_ok=True)
            else:
                # Single-shot summary notification covering the whole sequence;
                # the per-phase chatter that used to mirror every log line is
                # gone in v5.2 (journalctl is the forensic record).
                self._send_notification(
                    f"✅ **Shutdown Sequence Complete** (took {elapsed}s)\n"
                    f"Powering down local server NOW.",
                    self.config.NOTIFY_FAILURE,
                    category="shutdown_summary",
                )
                # Give the persistent worker a chance to drain before the
                # halt cuts power. Returns as soon as pending hits 0,
                # rather than always waiting the full 5s. Whatever doesn't
                # drain stays in SQLite as 'pending' and ships on the
                # next start (the lossless guarantee).
                if self._notification_worker:
                    self._notification_worker.flush(timeout=5)

                # Slice 3: tag this shutdown as power-loss-triggered so
                # the next start can emit "📊 Recovered" and (with Slice
                # 4 coalescing) fold this run's "Shutdown sequence
                # complete" notification into one richer message.
                from pathlib import Path
                write_shutdown_marker(
                    Path(self.config.statistics.db_directory),
                    version=__version__,
                    reason=REASON_SEQUENCE_COMPLETE,
                )

                cmd_parts = self.config.local_shutdown.command.split()
                if self.config.local_shutdown.message:
                    cmd_parts.append(self.config.local_shutdown.message)
                run_command(cmd_parts)
        else:
            self._log_message("✅ SHUTDOWN SEQUENCE COMPLETE (local shutdown disabled)")
            self._send_notification(
                f"✅ **Shutdown Sequence Complete** (took {elapsed}s)\n"
                f"Local shutdown is disabled — system stays up.",
                self.config.NOTIFY_INFO,
                category="shutdown_summary",
            )
            self._shutdown_flag_path.unlink(missing_ok=True)

            # Exit if --exit-after-shutdown was specified
            if self._exit_after_shutdown:
                self._log_message("🛑 Exiting after shutdown sequence")
                self._cleanup_and_exit(None, None)

    def _trigger_immediate_shutdown(self, reason: str):
        """Trigger an immediate shutdown if not already in progress."""
        if self._shutdown_flag_path.exists():
            # Surface gated re-triggers (bug #4). The early return used
            # to be silent, which made correlating "trigger conditions
            # met but nothing fired" with the stuck flag much harder
            # than it should have been.
            self._log_message(
                f"⚠️ Shutdown trigger fired ({reason}) but a previous "
                f"shutdown sequence is already in progress "
                f"({self._shutdown_flag_path}). Ignoring re-trigger."
            )
            return

        self._shutdown_flag_path.touch()

        # Send notification (non-blocking - fire and forget)
        self._send_notification(
            f"🚨 **EMERGENCY SHUTDOWN INITIATED!**\n"
            f"Reason: {reason}\n"
            "Executing shutdown tasks (VMs, Containers, Remote Servers).",
            self.config.NOTIFY_FAILURE,
            category="shutdown",
        )

        # Mirror to the events table so the TUI's --events-only view
        # carries the shutdown trigger between the ON_BATTERY row and
        # the eventual DAEMON_RECOVERED row on the next start.
        try:
            self._stats_store.log_event(
                "EMERGENCY_SHUTDOWN_INITIATED", reason,
            )
        except Exception:
            pass  # stats hiccup must not block the safety-critical path

        self._log_message(f"🚨 CRITICAL: Triggering immediate shutdown. Reason: {reason}")
        if self.config.local_shutdown.wall and not self.config.behavior.dry_run:
            run_command([
                "wall",
                f"🚨 CRITICAL: UPS battery critical! Immediate shutdown initiated! Reason: {reason}"
            ])

        self._execute_shutdown_sequence()

    def _cleanup_and_exit(self, signum: int, frame):
        """Handle clean exit on signals."""
        from pathlib import Path
        stats_dir = Path(self.config.statistics.db_directory)

        if self._shutdown_flag_path.exists():
            if self._notification_worker:
                # Mid-shutdown signal: still try to drain any in-flight
                # rows; whatever's left persists for the next start.
                self._notification_worker.flush(timeout=5)
                self._notification_worker.stop()
            self._stop_stats()
            sys.exit(0)

        self._shutdown_flag_path.touch()

        self._log_message("🛑 Service stopped by signal (SIGTERM/SIGINT). Monitoring is inactive.")

        # Mirror to the events table so the TUI's --events-only view
        # shows when the daemon was last cleanly stopped (it already
        # logs DAEMON_START on every boot; pairing it with DAEMON_STOP
        # makes the on-off audit trail symmetric).
        try:
            self._stats_store.log_event(
                "DAEMON_STOP",
                f"Eneru v{__version__} stopped by signal (SIGTERM/SIGINT)",
            )
        except Exception:
            pass

        # v5.2.1: postinstall.sh drops the upgrade marker BEFORE invoking
        # `systemctl restart`, so the marker is on disk by the time
        # SIGTERM lands here on a deb/rpm upgrade. The next daemon will
        # emit a single "📦 Upgraded vX → vY" message that supersedes
        # this stop, so suppress the stop entirely — saves a write and
        # avoids the prior-instance "Stopped" leaking through.
        upgrade_in_progress = read_upgrade_marker(stats_dir) is not None

        # Drain anything ALREADY in the queue (emergency-shutdown summary,
        # voltage events, etc.) BEFORE enqueueing the lifecycle stop —
        # so the worker doesn't pick up our deferred-stop row and ship
        # it eagerly, defeating the deferred-delivery mechanism below.
        # After this drain + stop, the worker is gone; the row we
        # enqueue stays `pending` in SQLite and can be cancelled by
        # either the next daemon's classifier (within the
        # `schedule_deferred_stop_or_eager_send` window) or delivered
        # by the systemd-run timer if no replacement comes up.
        body = "🛑 **Eneru Service Stopped**\nMonitoring is now inactive."
        notify_type = self.config.NOTIFY_WARNING

        # Order matters: flush + stop FIRST, then enqueue. If we enqueued
        # before stop(), the worker could pick the row up on its next
        # iteration and ship it eagerly — defeating the deferred-delivery
        # mechanism. After stop(), the worker thread is dead but
        # `_send_notification` still writes the row to SQLite (the
        # enqueue is a synchronous DB insert; only delivery requires the
        # worker thread).
        if self._notification_worker:
            self._notification_worker.flush(timeout=5)
            self._notification_worker.stop()

        notif_id = None
        if not upgrade_in_progress:
            notif_id = self._send_notification(
                body, notify_type, category="lifecycle",
            )

        # Schedule deferred delivery via systemd-run (or fall back to
        # eager Apprise send if systemd-run is unavailable). The timer
        # fires ~15 s after our exit; if the new daemon's classifier
        # cancels the row before then (single-UPS:
        # `_emit_lifecycle_startup_notification`; multi-UPS:
        # `_cancel_prev_pending_lifecycle_rows`), the timer is a no-op
        # and the user sees a single Restarted/Upgraded/Recovered. If
        # no replacement starts (true `systemctl stop`), the timer
        # delivers the stop and the user sees a single Stopped.
        if not upgrade_in_progress:
            if notif_id is not None and self._stats_store._conn is not None:
                # Normal path: row was enqueued in SQLite, hand off to
                # the deferred-delivery scheduler.
                schedule_deferred_stop_or_eager_send(
                    notification_id=notif_id,
                    db_path=Path(self._stats_db_path),
                    config_path=getattr(self.config, "config_path", None),
                    body=body,
                    notify_type=notify_type,
                    worker=self._notification_worker,
                    log_fn=self._log_message,
                )
            elif self._notification_worker is not None:
                # CodeRabbit P1: stats DB open() failed (per the warning
                # logged in _initialize_notifications), so notif_id is
                # None and the row never landed in SQLite. Without this
                # branch the lifecycle stop would be silently dropped on
                # every graceful exit. Ship eagerly via Apprise so the
                # user still gets a notification (loses restart-
                # coalescing in this degraded case, but the alternative
                # is no notification at all).
                try:
                    self._notification_worker._send_via_apprise(
                        body, notify_type,
                    )
                except Exception:
                    pass  # best-effort; nothing more we can do here

        self._stop_stats()

        # Slice 3: drop the shutdown marker so the next start can
        # classify this exit (signal → "🔄 Restarted" if it comes back
        # within RESTART_DOWNTIME_THRESHOLD_SECS, else cold "Started").
        # Don't downgrade an existing sequence_complete marker — that
        # would mask a power-loss shutdown when systemd's stop signal
        # arrives during the shutdown sequence (Cubic P2).
        existing = read_shutdown_marker(stats_dir)
        if not (existing
                and existing.get("reason") == REASON_SEQUENCE_COMPLETE):
            write_shutdown_marker(
                stats_dir, version=__version__, reason=REASON_SIGNAL,
            )

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

            # Re-arm the shutdown trigger (bug #4). The flag file is
            # _trigger_immediate_shutdown's re-entry guard; the
            # local_shutdown.enabled=true real-mode path doesn't clear
            # it (it implicitly trusts the OS reboot is about to take
            # the daemon down). On a healthy production install systemd
            # reaps us within seconds and this never matters, but on
            # edge installs (custom shutdown command, container/sandbox,
            # dummy UPS test rig) the host stays up and the second
            # outage's trigger silently no-ops. Clearing the flag here
            # means: "power came back, daemon is still alive ⇒ the
            # previous attempt did not actually halt the host ⇒ re-arm".
            self._shutdown_flag_path.unlink(missing_ok=True)
            # Coordinator hook: in multi-UPS mode the per-monitor flag
            # above is suffixed; the coordinator owns a separate
            # unsuffixed flag + an in-memory _local_shutdown_initiated
            # lock that would otherwise still gate the next trigger.
            # The callback resets both. None for single-UPS / standalone.
            if self._power_restored_callback is not None:
                try:
                    self._power_restored_callback()
                except Exception as exc:  # defensive: never raise into the loop
                    self._log_message(
                        f"⚠️ power_restored_callback raised: {exc}"
                    )

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
                            self.config.NOTIFY_FAILURE,
                            category="shutdown",
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
                        self.config.NOTIFY_WARNING,
                        category="health",
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
