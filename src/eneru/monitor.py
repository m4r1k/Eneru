#!/usr/bin/env python3
"""
Eneru - Generic UPS Monitoring and Shutdown Management
Monitors UPS status via NUT and triggers configurable shutdown sequences.
https://github.com/m4r1k/Eneru
"""

import sqlite3
import sys
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
from eneru.remote_health import RemoteHealthManager, remote_health_sidecar_path
from eneru.api import EneruAPIServer
from eneru.mqtt import MQTTPublisher
from eneru.deferred_delivery import schedule_deferred_stop_or_eager_send
from eneru import self_test as selftest
from eneru import reports as reports_mod
from eneru import nut_control as nutctl
from eneru.config import NutControlConfig
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
from eneru.shutdown.remote import RemoteShutdownMixin, loopback_poweroff_sent
from eneru.health.voltage import VoltageMonitorMixin
from eneru.health.battery import BatteryMonitorMixin
# ISS-017: single source of truth for the connection-retry cadence. The poll
# loop waits this many seconds between failed attempts, and health_model sizes
# the pre-grace stale window off the same value — import it so the two can't
# drift (health_model is a leaf module: no import cycle).
from eneru.health_model import RETRY_WAIT_SECONDS as CONNECTION_RETRY_WAIT_SECONDS

SLOW_NUT_LOG_THRESHOLD_SECONDS = 2.0
SLOW_NUT_LOG_RATE_LIMIT_SECONDS = 300.0
SLOW_NUT_NOTIFY_THRESHOLD_SECONDS = 10.0
SLOW_NUT_NOTIFY_CONSECUTIVE_POLLS = 3
# Backoff between scheduled self-test ISSUE retries after a failure, so a
# persistently-broken config doesn't re-attempt (and spawn an upscmd subprocess)
# on every poll tick.
SELF_TEST_ISSUE_RETRY_SECONDS = 300.0

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


class DependencyError(RuntimeError):
    """A required external command is missing at startup (ISS-006).

    Raised by _check_dependencies instead of sys.exit(1) so coordinator mode's
    ``except Exception`` crash handler catches it (SystemExit would bypass it and
    silently kill the per-group thread). run()'s FATAL handler treats this as an
    ENVIRONMENT problem, not a runtime crash: it does NOT write the FATAL marker,
    so the next start (after the operator installs the tool) is not misclassified
    as "exited fatally".
    """


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
        # H2: per-outage latch so the on-battery FAILSAFE runs the shutdown
        # sequence at most once per connection-loss event (non-halting configs
        # clear the flag and would otherwise re-fire every poll). Reset on the
        # next successful poll.
        self._failsafe_initiated = False

        # Per-group state file paths (suffix for multi-UPS)
        sfx = f".{state_file_suffix}" if state_file_suffix else ""
        self._shutdown_flag_path = Path(config.logging.shutdown_flag_file + sfx)
        self._shutdown_in_progress = False
        self._shutdown_flag_unusable = False
        # ISS-001: True only while _execute_shutdown_sequence is running. In
        # single-UPS mode the sequence AND the SIGTERM/SIGINT handler both run
        # on the main thread, so a signal landing mid-sequence would otherwise
        # sys.exit(0) and abort the host poweroff. _cleanup_and_exit consults
        # this flag to decline exiting while a sequence is in flight.
        self._shutdown_sequence_in_flight = False
        # ISS-018 / ISS-022: monotonic throttle timestamps (0.0 => fire on first
        # occurrence). On-battery status log cadence, and the upsc -l name
        # diagnostic cooldown.
        self._last_ob_status_log_mono = 0.0
        self._last_name_diagnostic_mono = 0.0
        # ISS-019: throttle the neutral/unrecognized-status notice.
        self._last_unknown_status_log_mono = 0.0
        self._last_unknown_status_logged = ""
        self._battery_history_path = Path(config.logging.battery_history_file + sfx)
        self._state_file_path = Path(config.logging.state_file + sfx)
        self._remote_health_path = remote_health_sidecar_path(self._state_file_path)

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
        self._remote_health_manager: Optional[RemoteHealthManager] = None
        self._api_server: Optional[EneruAPIServer] = None
        self._mqtt_publisher: Optional[MQTTPublisher] = None
        self._slow_nut_log_threshold_seconds = SLOW_NUT_LOG_THRESHOLD_SECONDS
        self._slow_nut_log_rate_limit_seconds = SLOW_NUT_LOG_RATE_LIMIT_SECONDS
        self._slow_nut_notify_threshold_seconds = SLOW_NUT_NOTIFY_THRESHOLD_SECONDS
        self._slow_nut_notify_consecutive_polls = SLOW_NUT_NOTIFY_CONSECUTIVE_POLLS
        self._last_slow_nut_log_time = 0.0
        self._slow_nut_poll_streak = 0
        self._slow_nut_poll_notified = False
        # v6.1: monotonic gate for the per-UPS battery-health update (interval
        # read live from config.battery_health -> SAFE reload). None = run on
        # the first loop iteration.
        self._last_health_update_mono: Optional[float] = None
        # v6.1: per-UPS self-test issue/poll timing (set up in B6).
        self._self_test_pending_id: Optional[int] = None
        self._self_test_poll_due_mono: Optional[float] = None
        # Backoff after a failed ISSUE so a persistently-broken self-test (bad
        # creds, command pulled from the allowlist) retries periodically instead
        # of re-attempting on every poll tick.
        self._self_test_retry_after_mono: Optional[float] = None

        # Effective target passed to ``upsc`` (``upsname@host:port``). Normally
        # identical to ``config.ups.name``, but the autodiscovery diagnostic may
        # self-heal it to the real UPS name when the configured one is wrong
        # (issue #71). Only ``_run_upsc`` reads this; every display/log/state
        # path keeps using ``config.ups.name`` so operator-facing identity and
        # on-disk state never fragment.
        self._poll_target = self.config.ups.name
        self._ups_name_autocorrected = False

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
                f"⚠️  WARNING: stats store open failed at {self._stats_db_path}: {e}. "
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
        writer = self._stats_writer
        self._stats_writer = None
        if writer is not None and writer is not threading.current_thread():
            try:
                writer.join(timeout=2)
            except RuntimeError:
                pass
        try:
            self._stats_store.close()
        except Exception:
            pass

    def _shutdown_guard_active(self) -> bool:
        """Return whether a shutdown sequence is already admitted."""
        if self._shutdown_in_progress:
            return True
        if self._shutdown_flag_unusable:
            return False
        try:
            return self._shutdown_flag_path.exists()
        except OSError as exc:
            self._shutdown_flag_unusable = True
            self._log_message(
                f"⚠️  Could not inspect shutdown flag {self._shutdown_flag_path}: "
                f"{exc}. Ignoring the on-disk guard for this process."
            )
            return False

    def _clear_shutdown_in_progress(self) -> None:
        """Clear both in-memory and on-disk duplicate-shutdown guards."""
        try:
            self._shutdown_flag_path.unlink(missing_ok=True)
            self._shutdown_flag_unusable = False
        except OSError as exc:
            self._shutdown_flag_unusable = True
            self._log_message(
                f"⚠️  Could not clear shutdown flag {self._shutdown_flag_path}: "
                f"{exc}. Ignoring the on-disk guard for this process."
            )
        finally:
            self._shutdown_in_progress = False

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
                f"⚠️  Trigger condition met (advisory, redundancy group): {reason}"
            )

    def _clear_advisory_trigger(self):
        """Clear the advisory trigger when conditions return to normal."""
        with self.state._lock:
            was_active = self.state.trigger_active
            self.state.trigger_active = False
            self.state.trigger_reason = ""
        if was_active:
            self._log_message(
                "✅  Advisory trigger cleared (redundancy group): conditions normal."
            )

    def run(self):
        """Main entry point."""
        try:
            self._initialize()
            self._main_loop()
        except KeyboardInterrupt:
            self._cleanup_and_exit(signal.SIGINT, None)
        except Exception as e:
            self._log_message(f"❌  FATAL ERROR: {e}")
            self._send_notification(
                f"❌  **FATAL ERROR**\nError: {e}",
                self.config.NOTIFY_FAILURE,
                category="lifecycle",
            )
            # Slice 3: tag this exit so the next start emits
            # "🚀  Restarted (last instance exited fatally)" rather than
            # a generic Started. ISS-006: a missing-dependency at startup is an
            # environment problem, not a runtime crash — skip the marker so the
            # next (fixed) start isn't misclassified as "exited fatally".
            if not isinstance(e, DependencyError):
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
            # SIGHUP re-reads config and applies the safe subset live
            # (systemctl reload / docker kill -s HUP). In coordinator mode the
            # coordinator owns the signal and reloads every group.
            signal.signal(signal.SIGHUP, self._handle_sighup)

        if self.logger is None:
            self.logger = UPSLogger(self.config.logging.file, self.config)

        if self.config.logging.file:
            try:
                Path(self.config.logging.file).touch(exist_ok=True)
            except PermissionError:
                pass

        self._clear_shutdown_in_progress()

        try:
            self._battery_history_path.write_text("")
        except PermissionError:
            self._log_message(f"⚠️  WARNING: Cannot write to {self._battery_history_path}")

        # Initialize notification worker
        self._initialize_notifications()

        self._check_dependencies()

        self._log_message(f"🚀  Eneru v{__version__} starting - monitoring {self.config.ups.name}")
        # Always invoke — the helper updates meta.last_seen_version for
        # next-start upgrade detection. The actual SEND is suppressed in
        # coordinator mode because the coordinator emits ONE classified
        # lifecycle notification at the multi_ups level (per-monitor
        # sends would be N copies of the same event).
        self._emit_lifecycle_startup_notification()

        if self.config.behavior.dry_run:
            self._log_message("🧪  *** RUNNING IN DRY-RUN MODE - NO ACTUAL SHUTDOWN WILL OCCUR ***")

        self._log_enabled_features()
        self._wait_for_initial_connection()
        self._initialize_voltage_thresholds()
        self._start_stats()
        self._start_remote_health()
        if not self._coordinator_mode:
            self._start_api_server()
            self._start_mqtt_publisher()

    def _start_remote_health(self):
        """Start advisory remote SSH healthchecks for this group."""
        if self._remote_health_manager is not None:
            return
        self._remote_health_manager = RemoteHealthManager(
            config=self.config,
            group_label=self.config.ups.label,
            servers=self.config.remote_servers,
            sidecar_path=self._remote_health_path,
            stop_event=self._stop_event,
            log_fn=self._log_message,
            notify_fn=lambda body, notify_type: self._send_notification(
                body, notify_type, category="health",
            ),
            event_fn=self._record_remote_health_event,
        )
        self._remote_health_manager.start()

    def _record_remote_health_event(
        self, event_type: str, detail: str, notification_sent: bool
    ) -> None:
        """Mirror remote-health state changes into the per-UPS event log."""
        self._stats_store.log_event(
            event_type,
            detail,
            notification_sent=notification_sent,
        )

    def _start_api_server(self):
        """Start the read-only API server for single-UPS mode."""
        if self._api_server is not None:
            return
        self._api_server = EneruAPIServer(self, self.config, log_fn=self._log_message)
        self._api_server.start()

    def _start_mqtt_publisher(self):
        """Start optional outbound MQTT publishing for single-UPS mode."""
        if self._mqtt_publisher is not None:
            return
        self._mqtt_publisher = MQTTPublisher(
            self, self.config, self._stop_event, log_fn=self._log_message,
        )
        self._mqtt_publisher.start()

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
                f"⚠️  WARNING: stats store open failed at "
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
                    f"⚠️  WARNING: pre-worker lifecycle sweep failed: {e}"
                )

        if self._notification_worker is not None:
            # Coordinator mode: register our store with the shared worker.
            if self._stats_store._conn is not None:
                self._notification_worker.register_store(self._stats_store)
            return

        if not self.config.notifications.enabled:
            self._log_message("📢  Notifications: disabled")
            return

        if not APPRISE_AVAILABLE:
            self._log_message("⚠️  WARNING: Notifications enabled but apprise not installed. "
                              "Install with: uv pip install apprise")
            self.config.notifications.enabled = False
            return

        self._notification_worker = NotificationWorker(self.config)
        if self._notification_worker.start():
            if self._stats_store._conn is not None:
                self._notification_worker.register_store(self._stats_store)
            service_count = self._notification_worker.get_service_count()
            self._log_message(f"📢  Notifications: enabled ({service_count} service(s))")
        else:
            self._log_message("⚠️  WARNING: Failed to initialize notifications")
            self.config.notifications.enabled = False

    def _emit_lifecycle_startup_notification(self):
        """Update lifecycle meta + (single-UPS only) emit the classified
        startup notification.

        Replaces the v5.1 unconditional ``🚀  Started`` so the user sees:
        - ``📦  Upgraded vX → vY`` after a deb/rpm upgrade
        - ``📊  Recovered`` after a power-loss-triggered shutdown
        - ``🔄  Restarted`` after a quick `systemctl restart`
        - ``🚀  Started (after crash)`` if the previous instance died
          without writing its marker
        - ``🚀  Started`` on a fresh install

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

        self._log_message(f"📋  Enabled features: {', '.join(features) if features else 'None'}")

    def _log_message(self, message: str, **extra):
        """Log a message using the logger, with optional prefix for multi-UPS.

        Extra keyword arguments (``category``, ``event_type``, etc.) are
        forwarded to ``UPSLogger.log`` and become structured fields under
        the JSON formatter. The text formatter ignores them. The
        ``group`` field is filled in automatically from
        ``self._log_prefix`` when present so JSON pipelines can group
        per-UPS rows without parsing the message text.
        """
        prefixed = f"{self._log_prefix}{message}" if self._log_prefix else message
        if self._log_prefix and "group" not in extra:
            extra["group"] = self._log_prefix.strip().rstrip(":").strip(" []")
        if self.logger:
            self.logger.log(prefixed, **extra)
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
        self._log_message(
            f"⚡  POWER EVENT: {event} - {details}",
            category="power_event",
            event_type=event,
        )

        try:
            run_command([
                # syslog identifier renamed from the legacy "ups-monitor"
                # to "eneru" — the package + service rebrand happened in
                # v5.0 but this side-channel was missed, so power events
                # showed up under a different identifier than every
                # other journal line emitted by the daemon.
                "logger", "-t", "eneru", "-p", "daemon.warning",
                f"⚡  POWER EVENT: {event} - {details}"
            ])
        except Exception:
            pass

        # Determine notification disposition first so we can record an
        # accurate notification_sent flag in the stats events row.
        notification: Optional[Tuple[str, str]] = None  # (body, type)

        event_handlers = {
            "ON_BATTERY": (
                f"⚠️  **POWER FAILURE DETECTED!**\nSystem running on battery.\nDetails: {details}",
                self.config.NOTIFY_WARNING
            ),
            "POWER_RESTORED": (
                f"✅  **POWER RESTORED**\nSystem back on line power/charging.\nDetails: {details}",
                self.config.NOTIFY_SUCCESS
            ),
            "BROWNOUT_DETECTED": (
                f"⚠️  **VOLTAGE ISSUE:** {event}\nDetails: {details}",
                self.config.NOTIFY_WARNING
            ),
            "OVER_VOLTAGE_DETECTED": (
                f"⚠️  **VOLTAGE ISSUE:** {event}\nDetails: {details}",
                self.config.NOTIFY_WARNING
            ),
            "AVR_BOOST_ACTIVE": (
                f"⚡  **AVR ACTIVE:** {event}\nDetails: {details}",
                self.config.NOTIFY_WARNING
            ),
            "AVR_TRIM_ACTIVE": (
                f"⚡  **AVR ACTIVE:** {event}\nDetails: {details}",
                self.config.NOTIFY_WARNING
            ),
            "BYPASS_MODE_ACTIVE": (
                f"⚠️  **UPS IN BYPASS MODE!**\nNo protection active!\nDetails: {details}",
                self.config.NOTIFY_FAILURE
            ),
            "BYPASS_MODE_INACTIVE": (
                f"✅  **Bypass Mode Inactive**\nProtection restored.\nDetails: {details}",
                self.config.NOTIFY_SUCCESS
            ),
            "OVERLOAD_ACTIVE": (
                f"⚠️  **UPS OVERLOAD DETECTED!**\nDetails: {details}",
                self.config.NOTIFY_FAILURE
            ),
            "OVERLOAD_RESOLVED": (
                f"✅  **Overload Resolved**\nDetails: {details}",
                self.config.NOTIFY_SUCCESS
            ),
            "CONNECTION_LOST": (
                f"❌  **ERROR: Connection Lost**\n{details}",
                self.config.NOTIFY_FAILURE
            ),
            "CONNECTION_RESTORED": (
                f"✅  **Connection Restored**\n{details}",
                self.config.NOTIFY_SUCCESS
            ),
        }

        # Reasons the notification dispatch may be skipped (the log row
        # always lands; this only affects whether the operator gets
        # pinged by Apprise). The stats events row records which path
        # we took via notification_sent.
        skipped_during_shutdown = self._shutdown_guard_active()
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
                f"⚡  **Event:** {event}\nDetails: {details}",
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
        required_cmds = ["upsc"]
        group = self.config.ups_groups[0] if self.config.ups_groups else None
        is_local = group.is_local if group else True
        # v5.5: when delegating local-host actions to the host via the
        # loopback SSH entry, the host-poweroff binary lives on the host,
        # NOT in the container. Skip the local-binary requirement in that
        # case; the loopback's shutdown_command runs over SSH instead.
        if (
            is_local
            and self.config.local_shutdown.enabled
            and not self._uses_loopback_delegate
        ):
            shutdown_cmd = str(self.config.local_shutdown.command).split()
            if shutdown_cmd:
                required_cmds.append(shutdown_cmd[0])
        if self._uses_loopback_delegate:
            required_cmds.append("ssh")
        missing = [cmd for cmd in required_cmds if not command_exists(cmd)]

        if missing:
            error_msg = f"❌  FATAL ERROR: Missing required commands: {', '.join(missing)}"
            # ISS-006: raise instead of sys.exit(1). In coordinator mode this
            # runs on a per-group daemon thread; SystemExit derives from
            # BaseException and would bypass _run_monitor's `except Exception`,
            # killing the thread silently (no crash notification, no logger
            # record). A RuntimeError is caught there and by run()'s FATAL
            # handler (single-UPS), so the failure is always visible.
            self._log_message(error_msg)
            raise DependencyError(error_msg)

        if not command_exists("logger") and not self._is_container_runtime:
            self._log_message(
                "⚠️  WARNING: 'logger' not found. Power events will still be "
                "written to Eneru logs, but the legacy syslog side-channel "
                "will be skipped."
            )

        # v5.5: when delegating local-host actions over SSH, the host binaries
        # (virsh, docker, podman, etc.) live on the HOST, not in the container.
        # Skip the in-process binary checks AND don't disable the corresponding
        # features — the loopback's pre_shutdown_commands (already injected by
        # cli._inject_delegated_actions) handle them on the host. Logging a
        # warning here would be misleading and would flip enabled-flags off,
        # creating false negatives in `eneru validate` output.
        delegating = self._uses_loopback_delegate

        # Check optional dependencies based on enabled features
        if (
            not delegating
            and self.config.virtual_machines.enabled
            and not command_exists("virsh")
        ):
            self._log_message("⚠️  WARNING: 'virsh' not found but VM shutdown is enabled. VMs will be skipped.")
            self.config.virtual_machines.enabled = False

        # Container runtime detection. Skip entirely when delegating — host
        # owns the runtime decision, and the loopback's stop_containers /
        # stop_compose templates probe for docker/podman on the remote side.
        if self.config.containers.enabled and not delegating:
            self._container_runtime = self._detect_container_runtime()
            if self._container_runtime:
                self._log_message(f"🐳  Container runtime detected: {self._container_runtime}")
                # Check compose availability if compose_files are configured
                if self.config.containers.compose_files:
                    self._compose_available = self._check_compose_available()
                    if self._compose_available:
                        self._log_message(
                            f"🐳  Compose support: enabled ({self._container_runtime} compose, "
                            f"{len(self.config.containers.compose_files)} file(s))"
                        )
                    else:
                        self._log_message(
                            f"⚠️  WARNING: compose_files configured but '{self._container_runtime} compose' "
                            "not available. Compose shutdown will be skipped."
                        )
            else:
                self._log_message("⚠️  WARNING: No container runtime found. Container shutdown will be skipped.")
                self.config.containers.enabled = False

        enabled_servers = [s for s in self.config.remote_servers if s.enabled]
        if enabled_servers and not command_exists("ssh"):
            self._log_message("⚠️  WARNING: 'ssh' not found but remote servers are configured. Remote shutdown will be skipped.")
            for server in self.config.remote_servers:
                server.enabled = False

    def _get_ups_var(self, var_name: str) -> Optional[str]:
        """Get a single UPS variable using upsc."""
        exit_code, stdout, _ = self._run_upsc([var_name], full_poll=False)
        if exit_code == 0:
            return stdout.strip()
        return None

    def _get_all_ups_data(self) -> Tuple[bool, Dict[str, str], str]:
        """Query all UPS data using a single upsc call."""
        exit_code, stdout, stderr = self._run_upsc([], full_poll=True)

        if exit_code != 0:
            return False, {}, self._format_upsc_error(stdout, stderr)

        if "Data stale" in stdout or "Data stale" in stderr:
            return False, {}, "Data stale"

        ups_data: Dict[str, str] = {}
        for line in stdout.strip().split('\n'):
            if ':' in line:
                key, value = line.split(':', 1)
                ups_data[key.strip()] = value.strip()

        if not ups_data.get('ups.status'):
            return False, {}, "Missing ups.status"

        return True, ups_data, ""

    def _mark_shutdown_in_progress(self, context: str) -> bool:
        """Best-effort persistent shutdown guard.

        The flag prevents duplicate shutdown sequences. It is still just
        bookkeeping: if the flag path is unavailable during a power event, the
        daemon must keep moving toward the actual poweroff.
        """
        self._shutdown_in_progress = True
        try:
            self._shutdown_flag_path.touch()
            self._shutdown_flag_unusable = False
            return True
        except OSError as exc:
            self._shutdown_flag_unusable = True
            self._log_message(
                f"⚠️  Could not write shutdown flag {self._shutdown_flag_path} "
                f"while {context}: {exc}. Continuing without the on-disk guard."
            )
            return False

    def _run_upsc(self, args: List[str], *, full_poll: bool) -> Tuple[int, str, str]:
        cmd = ["upsc", self._poll_target, *args]
        started = time.monotonic()
        # NUT's NSS-backed libupsclient can emit "Init SSL without certificate
        # database" on stderr even for plain read-only polling. Suppress that
        # upstream noise so real connection/UPS-name errors stay visible.
        result = run_command(cmd, env_overrides={"NUT_QUIET_INIT_SSL": "true"})
        elapsed = time.monotonic() - started
        self._record_upsc_latency(elapsed, cmd, full_poll=full_poll)
        return result

    def _format_upsc_error(self, stdout: str, stderr: str) -> str:
        """Return an operator-facing error from a failed ``upsc`` call."""
        parts = []
        for stream in (stderr, stdout):
            for line in (stream or "").splitlines():
                text = line.strip()
                if not text:
                    continue
                if text == "Init SSL without certificate database":
                    continue
                if text not in parts:
                    parts.append(text)
        if not parts:
            # All output was blank or filtered noise (e.g. only the SSL-init
            # line): do not resurface what we just stripped.
            return "upsc exited without output"
        message = " | ".join(parts)
        # A NUT server that ACCEPTS STARTTLS and then botches the handshake
        # makes upsc fail hard with no plaintext fallback, emitting inscrutable
        # OpenSSL text ("SSL_connect -1", "shutdown while in init"). The most
        # common cause in the wild is UniFi UPS firmware once NUT login
        # credentials are enabled — it breaks TLS for every client on the port,
        # not just authenticated ones. Point the operator at the actual fix.
        lowered = message.lower()
        ssl_markers = ("ssl_connect", "ssl error", "ssl_error",
                       "ssl routines", "handshake", "shutdown while in init")
        if any(marker in lowered for marker in ssl_markers):
            message += (
                " | hint: the NUT server offered TLS but the handshake failed. "
                "If this is a UniFi UPS, disable NUT login credentials — its "
                "firmware breaks TLS when auth is enabled (anonymous upsc reads "
                "still work). See docs/troubleshooting.md."
            )
        return message

    @staticmethod
    def _parse_nut_host(target: str) -> str:
        """Extract the ``host[:port]`` part of an ``upsname@host:port`` target.

        A NUT UPS name cannot contain ``@``, so the first ``@`` separates the
        UPS name from the host spec. Returns ``localhost`` when no host is
        given (bare ``upsname``), matching NUT's own default.
        """
        if "@" in (target or ""):
            host = target.split("@", 1)[1].strip()
            return host or "localhost"
        return "localhost"

    @staticmethod
    def _parse_nut_name(target: str) -> str:
        """Extract the UPS-name part (before ``@``) of a poll target."""
        return (target or "").split("@", 1)[0].strip()

    def _discover_ups_names(self, host: str) -> List[str]:
        """List the UPS names a NUT server actually exposes via ``upsc -l``.

        Called directly (not through ``_run_upsc``, which would prepend the
        configured UPS name). Returns the discovered names, or an empty list if
        the server is unreachable / returns nothing. SSL-init noise is silenced
        and connection/SSL banner lines are filtered out so only bare UPS names
        (no whitespace, no ``:``) survive.
        """
        exit_code, stdout, _ = run_command(
            ["upsc", "-l", host],
            timeout=10,
            env_overrides={"NUT_QUIET_INIT_SSL": "true"},
        )
        if exit_code != 0:
            return []
        names = []
        for line in (stdout or "").splitlines():
            token = line.strip()
            # `upsc -l` prints bare UPS names (ups.conf section names) one per
            # line on stdout; SSL/connection banners go to stderr and are
            # already discarded above. This filter is defensive against any
            # unexpected stdout decoration: real names never contain whitespace
            # or a colon, so dropping such lines cannot lose a valid name.
            if not token or " " in token or "\t" in token or ":" in token:
                continue
            if token not in names:
                names.append(token)
        return names

    def _run_ups_name_diagnostic(self, error_msg: str) -> None:
        """On a hard NUT connection failure, help the operator (issue #71).

        The benign ``Init SSL without certificate database`` line used to mask
        the real cause, which is most often a wrong ``ups.name`` — e.g. a NUT
        login *username* placed where the UPS *device name* belongs. We probe
        the server with ``upsc -l`` to list the real names and:
          * self-heal the poll target when exactly one UPS exists and the
            configured name is not it (unambiguous), or
          * tell the operator which names are available otherwise.
        """
        host = self._parse_nut_host(self._poll_target)
        configured_name = self._parse_nut_name(self._poll_target)
        names = self._discover_ups_names(host)

        if not names:
            self._log_message(
                f"🔎  Could not list UPS names on {host} (server unreachable, "
                f"access-restricted, or network issue), so the configured name "
                f"'{configured_name}' could not be verified. Check that NUT "
                f"(upsd) is running and reachable there."
            )
            return

        # Educational note for the classic username-vs-UPS-name mix-up.
        name_hint = (
            "Note: the value before '@' in ups.name must be the UPS device "
            "name from the server's ups.conf, not a NUT login username. If you "
            "meant it as a control credential, set nut_control.username instead."
        )

        if configured_name in names:
            self._log_message(
                f"🔎  UPS '{configured_name}' exists on {host} "
                f"(available: {', '.join(names)}), so this failure is likely "
                f"access control (ERR ACCESS-DENIED) or transient, not the "
                f"UPS name. Last error: {error_msg}"
            )
            return

        if len(names) == 1 and not self._ups_name_autocorrected:
            discovered = names[0]
            self._poll_target = f"{discovered}@{host}"
            self._ups_name_autocorrected = True
            self._log_message(
                f"⚠️  UPS '{configured_name}' was not found on {host}; the only "
                f"UPS there is '{discovered}'. Auto-correcting this session to "
                f"poll '{self._poll_target}'. Please fix ups.name in your config "
                f"so this is not needed on restart (NUT control commands keep "
                f"using the configured name until then). {name_hint}"
            )
            return

        self._log_message(
            f"⚠️  UPS '{configured_name}' was not found on {host}. Available "
            f"UPS names: {', '.join(names)}. Set ups.name to one of these "
            f"(as '<name>@{host}'). {name_hint}"
        )

    def _record_upsc_latency(self, elapsed: float, cmd: List[str], *, full_poll: bool):
        now = time.time()
        # Logs are the first visibility tier: show slow NUT responses quickly,
        # but rate-limit per UPS so a wedged local NUT socket does not bury
        # the shutdown-relevant log lines.
        if elapsed >= self._slow_nut_log_threshold_seconds:
            if (
                self._last_slow_nut_log_time == 0.0
                or now - self._last_slow_nut_log_time
                >= self._slow_nut_log_rate_limit_seconds
            ):
                self._log_message(
                    f"⚠️  Slow NUT response from {self.config.ups.name}: "
                    f"{elapsed:.1f}s for {' '.join(cmd)}"
                )
                try:
                    self._stats_store.log_event(
                        "SLOW_NUT_RESPONSE",
                        (
                            f"{self.config.ups.name} slow NUT response: "
                            f"{elapsed:.1f}s for {' '.join(cmd)}"
                        ),
                        notification_sent=False,
                    )
                except Exception:
                    # SQLite diagnostics must never affect polling or shutdown.
                    pass
                self._last_slow_nut_log_time = now

        if not full_poll:
            return

        # Notifications are intentionally stricter than logs. One slow poll
        # is operator-visible in the journal; only sustained full-poll latency
        # becomes an Apprise alert. A single fast poll resets BOTH the streak
        # counter and the "already notified" gate, so the threshold is
        # "N consecutive slow polls" -- not "sustained slowness over a window".
        # That keeps the alert tied to a clearly-degraded state rather than
        # firing on intermittent jitter.
        if elapsed >= self._slow_nut_notify_threshold_seconds:
            self._slow_nut_poll_streak += 1
        else:
            self._slow_nut_poll_streak = 0
            self._slow_nut_poll_notified = False
            return

        if (
            not self._slow_nut_poll_notified
            and self._slow_nut_poll_streak
            >= self._slow_nut_notify_consecutive_polls
        ):
            self._send_notification(
                f"⚠️  **Sustained slow NUT responses**\n"
                f"UPS: {self.config.ups.name}\n"
                f"Latest poll took {elapsed:.1f}s. "
                f"Threshold: {self._slow_nut_notify_threshold_seconds:.1f}s "
                f"for {self._slow_nut_notify_consecutive_polls} consecutive polls.",
                self.config.NOTIFY_WARNING,
                category="health",
            )
            try:
                self._stats_store.log_event(
                    "SLOW_NUT_RESPONSE",
                    (
                        f"{self.config.ups.name} sustained slow NUT "
                        f"responses: latest {elapsed:.1f}s; threshold "
                        f"{self._slow_nut_notify_threshold_seconds:.1f}s "
                        f"for {self._slow_nut_notify_consecutive_polls} "
                        "consecutive polls"
                    ),
                    notification_sent=True,
                )
            except Exception:
                # SQLite diagnostics must never mask notifications.
                pass
            self._slow_nut_poll_notified = True

    def _wait_for_initial_connection(self):
        """Wait for initial connection to NUT server (interruptible)."""
        self._log_message(f"⏳  Checking initial connection to {self.config.ups.name}...")

        max_wait = 30
        wait_interval = 5
        attempts = max_wait // wait_interval

        for attempt in range(attempts):
            success, _, _ = self._get_all_ups_data()
            if success:
                self._log_message("✅  Initial connection successful.")
                return
            # ISS-021: wait on the stop event (not time.sleep) so a SIGTERM
            # during startup interrupts immediately instead of blocking up to
            # ~30s; and never sleep after the final attempt.
            if attempt < attempts - 1:
                if self._stop_event.wait(wait_interval):
                    self._log_message(
                        "🛑  Startup interrupted before initial connection."
                    )
                    return

        self._log_message(
            f"⚠️  WARNING: Failed to connect to {self.config.ups.name} "
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

    @property
    def _uses_loopback_delegate(self) -> bool:
        """v5.5: True when this group's local-host actions are delegated to
        the host over SSH via an is_host_loopback remote_servers entry.

        Condition: running inside a container AND this group is the local
        owner AND a loopback entry is configured for this group. When True,
        the in-process VM/container/filesystem/host-poweroff phases are
        skipped — the loopback's pre_shutdown_commands and shutdown_command
        own those host-side effects.
        """
        from eneru.cli import _uses_loopback_delegate
        group = self.config.ups_groups[0] if self.config.ups_groups else None
        return _uses_loopback_delegate(self.config, group)

    @property
    def _is_container_runtime(self) -> bool:
        """Return True when this daemon is running inside any container runtime."""
        from eneru.cli import _detect_runtime_context, _is_container_runtime
        return _is_container_runtime(_detect_runtime_context())

    def _should_fire_wall(self) -> bool:
        """wall(1) is useful on native ttys, but reaches nobody from containers."""
        return (
            self.config.local_shutdown.wall
            and not self.config.behavior.dry_run
            and not self._is_container_runtime
        )

    def _execute_shutdown_sequence(self):
        """Execute the controlled shutdown sequence.

        Thin wrapper that marks the sequence in flight for its entire duration
        (ISS-001) so `_cleanup_and_exit` can decline to sys.exit() on a
        SIGTERM/SIGINT that lands mid-sequence in single-UPS mode (where the
        sequence and the signal handler share the main thread). The flag is
        cleared in `finally` even if the implementation raises or exits.
        """
        self._shutdown_sequence_in_flight = True
        try:
            self._execute_shutdown_sequence_impl()
        finally:
            self._shutdown_sequence_in_flight = False

    def _execute_shutdown_sequence_impl(self):
        """Execute the controlled shutdown sequence."""
        self._mark_shutdown_in_progress("starting shutdown sequence")
        sequence_start = time.monotonic()

        delegated = self._uses_loopback_delegate

        self._log_message("🚨  INITIATING EMERGENCY SHUTDOWN SEQUENCE")

        if self.config.behavior.dry_run:
            self._log_message("🧪  *** DRY-RUN MODE: No actual shutdown will occur ***")

        if delegated:
            # v5.5: emit a distinct event so dashboards / SIEMs can tell
            # container-mediated shutdowns apart from native ones.
            try:
                self._stats_store.log_event(
                    "DELEGATED_SHUTDOWN_INITIATED",
                    "host-loopback SSH delegate will execute local actions",
                )
            except Exception:
                pass
            self._log_message(
                "🛰️  Container loopback mode: local actions will be delegated "
                "to the host via SSH (no in-process VM/container/filesystem "
                "phases, no in-process host poweroff)."
            )

        # `wall` is an archaic v1/v2 path that reaches users via local ttys.
        # When delegating from a container it reaches nobody on the host, so
        # suppress it entirely rather than firing an empty broadcast.
        if self._should_fire_wall():
            run_command([
                "wall",
                "🚨  CRITICAL: Executing emergency UPS shutdown sequence NOW!"
            ])

        # Runtime is_local enforcement: only the local UPS group can
        # manage VMs, containers, and filesystems. Non-local groups skip
        # these even if config validation was somehow bypassed.
        # v5.5 delegated mode: also skip these in-process phases — the
        # loopback's generated pre_shutdown_commands run them on the host.
        group = self.config.ups_groups[0] if self.config.ups_groups else None
        is_local = group.is_local if group else True  # legacy single-UPS = local

        if is_local and not delegated:
            # Each drain phase is best-effort housekeeping. A failure in one
            # (a wedged libvirt, a bad config value, a hung mount) must NEVER
            # abort the path to the host poweroff below -- that is the actual
            # protective action. Previously an unguarded exception here
            # propagated to run() (FATAL + re-raise) or, in coordinator mode,
            # was swallowed without ever reaching the poweroff. Wrap each phase.
            for phase_name, phase_fn in (
                ("VM shutdown", self._shutdown_vms),
                ("container shutdown", self._shutdown_containers),
                ("filesystem sync", self._sync_filesystems),
                ("filesystem unmount", self._unmount_filesystems),
            ):
                try:
                    phase_fn()
                except Exception as exc:
                    self._log_message(
                        f"❌  {phase_name} phase failed: {exc}. Continuing the "
                        "shutdown sequence -- drain is best-effort, the host "
                        "halt is not."
                    )
        # Remote shutdown is also best-effort: its per-server work is already
        # guarded internally, but wrap the call too so the H4 contract -- the
        # host poweroff below is ALWAYS reached -- is structural, not dependent
        # on the callee never raising during setup.
        try:
            remote_results = self._shutdown_remote_servers() or []
        except Exception as exc:
            self._log_message(
                f"❌  remote shutdown phase failed: {exc}. Continuing to the "
                "host poweroff."
            )
            remote_results = []

        if is_local and self.config.filesystems.sync_enabled and not delegated:
            self._log_message("💾  Final filesystem sync...")
            if self.config.behavior.dry_run:
                self._log_message("  🧪  [DRY-RUN] Would perform final sync")
            else:
                # Bounded sync (FilesystemShutdownMixin) so a hung mount can't
                # wedge the sequence before the host poweroff below.
                self._bounded_sync("Final filesystem sync")

        # In coordinator mode, notify the coordinator instead of doing local shutdown
        if self._coordinator_mode:
            self._log_message("✅  GROUP SHUTDOWN SEQUENCE COMPLETE")
            if self._shutdown_callback:
                group = self.config.ups_groups[0] if self.config.ups_groups else None
                self._shutdown_callback(group)
            return

        elapsed = int(time.monotonic() - sequence_start)

        def record_sequence_complete() -> None:
            # Mirror true sequence completion into the events table so the
            # TUI's --events-only view carries the elapsed-time row between
            # EMERGENCY_SHUTDOWN_INITIATED and DAEMON_RECOVERED.
            try:
                self._stats_store.log_event(
                    "SHUTDOWN_SEQUENCE_COMPLETE", f"elapsed: {elapsed}s",
                )
            except Exception:
                pass

        if self.config.local_shutdown.enabled and not delegated:
            if self.config.behavior.dry_run:
                record_sequence_complete()
                self._log_message("🔌  Shutting down local server NOW")
                self._log_message("✅  SHUTDOWN SEQUENCE COMPLETE")
                self._log_message(f"🧪  [DRY-RUN] Would execute: {self.config.local_shutdown.command}")
                self._log_message("🧪  [DRY-RUN] Shutdown sequence completed successfully (no actual shutdown)")
                self._clear_shutdown_in_progress()
            else:
                # Validate the poweroff command BEFORE recording the run as a
                # completed shutdown (CodeRabbit). Config validation rejects an
                # empty/None command at load, but a programmatically-built Config
                # could still reach here; if it does, the host stays up, so we
                # must NOT write SHUTDOWN_SEQUENCE_COMPLETE / the recovery marker
                # or leave the flag set (which would gate future triggers until
                # line power returns). Report INCOMPLETE, clear the flag, bail.
                cmd_parts = str(self.config.local_shutdown.command or "").split()
                if not cmd_parts:
                    self._log_message(
                        "❌  local_shutdown.command is empty -- host poweroff "
                        "SKIPPED; shutdown sequence INCOMPLETE. Set "
                        "local_shutdown.command to a valid command."
                    )
                    self._send_notification(
                        f"❌  **Shutdown Sequence Incomplete** (took {elapsed}s)\n"
                        "Host poweroff command is empty; the host is still up.",
                        self.config.NOTIFY_FAILURE,
                        category="shutdown_summary",
                    )
                    self._clear_shutdown_in_progress()
                    return

                record_sequence_complete()
                self._log_message("🔌  Shutting down local server NOW")
                self._log_message("✅  SHUTDOWN SEQUENCE COMPLETE")
                # Single-shot summary notification covering the whole sequence;
                # the per-phase chatter that used to mirror every log line is
                # gone in v5.2 (journalctl is the forensic record).
                self._send_notification(
                    f"✅  **Shutdown Sequence Complete** (took {elapsed}s)\n"
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
                # the next start can emit "📊  Recovered" and (with Slice
                # 4 coalescing) fold this run's "Shutdown sequence
                # complete" notification into one richer message.
                from pathlib import Path
                write_shutdown_marker(
                    Path(self.config.statistics.db_directory),
                    version=__version__,
                    reason=REASON_SEQUENCE_COMPLETE,
                )

                if self.config.local_shutdown.message:
                    cmd_parts.append(self.config.local_shutdown.message)
                run_command(cmd_parts)
        elif self.config.local_shutdown.enabled and delegated:
            # v5.5: the loopback's shutdown_command (already executed during
            # _shutdown_remote_servers) is what actually powers off the host.
            # The container dies with it. Notify + flush + marker, then exit.
            loopback_results = [
                r for r in remote_results
                if any(
                    s.enabled
                    and s.is_host_loopback is True
                    and (s.name or s.host) == r.server
                    and s.host == r.host
                    for s in self.config.remote_servers
                )
            ]
            if not loopback_results or not all(
                loopback_poweroff_sent(r) for r in loopback_results
            ):
                details = "; ".join(
                    r.error or "shutdown command was not sent"
                    for r in loopback_results
                    if not loopback_poweroff_sent(r)
                ) or "loopback shutdown result missing"
                self._log_message(
                    "❌  Delegated host poweroff failed; shutdown sequence "
                    f"is incomplete: {details}"
                )
                self._send_notification(
                    f"❌  **Shutdown Sequence Incomplete** (took {elapsed}s)\n"
                    f"Host poweroff delegation failed: {details}",
                    self.config.NOTIFY_FAILURE,
                    category="shutdown_summary",
                )
                # Clear the re-entry flag so future triggers can retry the
                # delegated shutdown. In the success branch below the flag
                # stays touched because the container is going down with
                # the host; here the container stays up, so any subsequent
                # trigger has to be allowed through.
                self._clear_shutdown_in_progress()
                return
            # ISS-005: the poweroff WAS delivered (loopback_poweroff_sent is
            # True for every loopback result), so the sequence is complete even
            # if a Phase-A drain crashed. Surface the partial drain failure as a
            # warning but still record completion + write the marker below.
            drain_failures = "; ".join(
                r.error or "Phase-A drain reported a failure"
                for r in loopback_results
                if not r.success
            )
            if drain_failures:
                self._log_message(
                    "⚠️  Delegated host poweroff was sent, but a local drain "
                    f"phase partially failed: {drain_failures}. The host is "
                    "powering off; treating the sequence as complete."
                )
            record_sequence_complete()
            self._log_message(
                "🛰️  Host poweroff delegated to loopback SSH (already sent). "
                "Container will terminate when the host goes down."
            )
            self._log_message("✅  SHUTDOWN SEQUENCE COMPLETE")
            if self.config.behavior.dry_run:
                self._log_message(
                    "🧪  [DRY-RUN] Would have delegated host poweroff to loopback "
                    "SSH (no actual shutdown performed)."
                )
                self._clear_shutdown_in_progress()
            else:
                self._send_notification(
                    f"✅  **Shutdown Sequence Complete** (took {elapsed}s)\n"
                    f"Host poweroff delegated to loopback SSH.",
                    self.config.NOTIFY_FAILURE,
                    category="shutdown_summary",
                )
                if self._notification_worker:
                    self._notification_worker.flush(timeout=5)
                from pathlib import Path
                write_shutdown_marker(
                    Path(self.config.statistics.db_directory),
                    version=__version__,
                    reason=REASON_SEQUENCE_COMPLETE,
                )
        else:
            record_sequence_complete()
            self._log_message("✅  SHUTDOWN SEQUENCE COMPLETE (local shutdown disabled)")
            self._send_notification(
                f"✅  **Shutdown Sequence Complete** (took {elapsed}s)\n"
                f"Local shutdown is disabled — system stays up.",
                self.config.NOTIFY_INFO,
                category="shutdown_summary",
            )
            self._clear_shutdown_in_progress()

            # Exit if --exit-after-shutdown was specified
            if self._exit_after_shutdown:
                self._log_message("🛑  Exiting after shutdown sequence")
                self._cleanup_and_exit(None, None)

    def _trigger_immediate_shutdown(self, reason: str):
        """Trigger an immediate shutdown if not already in progress."""
        if self._shutdown_guard_active():
            # Surface gated re-triggers (bug #4). The early return used
            # to be silent, which made correlating "trigger conditions
            # met but nothing fired" with the stuck flag much harder
            # than it should have been.
            self._log_message(
                f"⚠️  Shutdown trigger fired ({reason}) but a previous "
                f"shutdown sequence is already in progress "
                f"({self._shutdown_flag_path}). Ignoring re-trigger."
            )
            return

        self._mark_shutdown_in_progress("triggering immediate shutdown")

        # Send notification (non-blocking - fire and forget)
        self._send_notification(
            f"🚨  **EMERGENCY SHUTDOWN INITIATED!**\n"
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

        self._log_message(f"🚨  CRITICAL: Triggering immediate shutdown. Reason: {reason}")
        if self._should_fire_wall():
            run_command([
                "wall",
                f"🚨  CRITICAL: UPS battery critical! Immediate shutdown initiated! Reason: {reason}"
            ])

        self._execute_shutdown_sequence()

    def reload_config(self) -> dict:
        """Re-read the config file and apply the safe subset live.

        Returns a report dict (also used by the API reload endpoint). Never
        raises on a bad config — the daemon keeps running on the old one.
        """
        from eneru.reload import perform_reload, _RELOAD_LOCK
        # ISS-027: hold the reload lock across parse + swap + worker bounce so a
        # SIGHUP and an API reload can't race the non-thread-safe bounce in
        # _apply_subsystem_reload.
        with _RELOAD_LOCK:
            report = perform_reload(
                self.config, [self.config], self.config.config_path)
            if report.get("reloaded") and report.get("subsystems"):
                self._apply_subsystem_reload(report["subsystems"])
            self._log_reload_report(report)
            return report

    def _apply_subsystem_reload(self, subsystems: list) -> None:
        """Re-init live subsystems whose config changed (best-effort)."""
        if "statistics" in subsystems and self._stats_store is not None:
            try:
                self._stats_store.apply_reload(self.config.statistics)
            except Exception as exc:  # pragma: no cover - defensive
                self._log_message(f"⚠️  stats retention reload failed: {exc}")
        if "notifications" in subsystems and not self._coordinator_mode:
            self._reload_notification_worker()
        if "remote_health" in subsystems:
            self._reload_remote_health()
        if "mqtt" in subsystems and not self._coordinator_mode:
            self._reload_mqtt_publisher()

    def _reload_notification_worker(self) -> None:
        """Bounce the single-UPS notification worker after config reload."""
        if self._notification_worker is not None:
            self._notification_worker.stop()
            self._notification_worker = None
        if not self.config.notifications.enabled:
            self._log_message("📢  Notifications: disabled")
            return
        if not APPRISE_AVAILABLE:
            self._log_message(
                "⚠️  WARNING: Notifications enabled but apprise not installed. "
                "Install with: uv pip install apprise"
            )
            return
        worker = NotificationWorker(self.config)
        if worker.start():
            if self._stats_store is not None and self._stats_store._conn is not None:
                worker.register_store(self._stats_store)
            self._notification_worker = worker
            count = worker.get_service_count()
            self._log_message(f"📢  Notifications reloaded ({count} service(s))")
        else:
            self._log_message("⚠️  WARNING: Failed to reload notifications")

    def _reload_remote_health(self) -> None:
        """Bounce remote-health with the new interval/probe/thresholds."""
        if self._remote_health_manager is not None:
            self._remote_health_manager.stop()
            self._remote_health_manager = None
        self._start_remote_health()
        self._log_message("🔄  Remote health checks reloaded")

    def _reload_mqtt_publisher(self) -> None:
        """Bounce the MQTT publisher so broker/topic changes take effect."""
        if self._mqtt_publisher is not None:
            self._mqtt_publisher.stop()
            self._mqtt_publisher = None
        self._start_mqtt_publisher()
        self._log_message("🔄  MQTT publisher reloaded")

    def record_control_event(self, ups_name: str, event_type: str,
                             detail: str) -> None:
        """Record an API control/reload action to the SQLite events table
        (v7.0 audit-log groundwork). Best-effort — never raises into the API."""
        try:
            if self._stats_store is not None:
                self._stats_store.log_event(event_type, detail)
        except Exception:  # pragma: no cover - defensive
            pass

    def delete_events(self, ups_name: str, items):
        """Delete events from the live per-UPS stats store. Returns the count
        removed, or ``None`` when the store is unavailable (statistics disabled /
        not open) so the API can answer 503 instead of silently succeeding."""
        store = self._stats_store
        if store is None or not store.is_open:
            return None
        return store.delete_events(items)

    def _handle_sighup(self, signum, frame):
        """SIGHUP -> hot-reload config (systemctl reload / docker kill -s HUP).

        Defensive: a reload must never crash the daemon via the signal handler,
        so any unexpected error is logged and swallowed.
        """
        self._log_message("🔄  SIGHUP received — reloading configuration")
        try:
            self.reload_config()
        except Exception as exc:  # pragma: no cover - defensive
            self._log_message(f"⚠️  Config reload error (ignored): {exc}")

    def _log_reload_report(self, report: dict) -> None:
        from eneru.reload import format_report
        for line in format_report(report):
            self._log_message(line)

    def _cleanup_and_exit(self, signum: int, frame):
        """Handle clean exit on signals."""
        from pathlib import Path
        stats_dir = Path(self.config.statistics.db_directory)

        # ISS-001: A real SIGTERM/SIGINT arriving while the shutdown sequence
        # is running on this (main) thread must NOT unwind it — sys.exit here
        # would abort the sequence before the host poweroff (run_command) fires.
        # Ignore the signal and let the in-flight sequence finish; its own
        # completion path performs cleanup. `signum is None` is the internal
        # _exit_after_shutdown call, which must still proceed to exit.
        # Tradeoff: in the no-poweroff branches (local_shutdown disabled, empty
        # command, failed delegate) the sequence returns without halting, the
        # flag clears, and the daemon keeps running; the swallowed `systemctl
        # stop` then degrades to a SIGKILL after TimeoutStopSec (skipping the
        # graceful DAEMON_STOP path). Acceptable: never abort a live poweroff,
        # and the sequence is short.
        if signum is not None and self._shutdown_sequence_in_flight:
            self._log_message(
                "⚠️  Signal received during shutdown sequence — ignoring; "
                "the in-flight poweroff sequence continues."
            )
            return

        self._stop_event.set()
        if self._remote_health_manager is not None:
            self._remote_health_manager.stop()
        if self._mqtt_publisher is not None:
            self._mqtt_publisher.stop()
        if self._api_server is not None:
            self._api_server.stop()

        if self._shutdown_guard_active():
            if self._notification_worker:
                # Mid-shutdown signal: still try to drain any in-flight
                # rows; whatever's left persists for the next start.
                self._notification_worker.flush(timeout=5)
                self._notification_worker.stop()
            self._stop_stats()
            sys.exit(0)

        self._mark_shutdown_in_progress("recording service stop")

        self._log_message("🛑  Service stopped by signal (SIGTERM/SIGINT). Monitoring is inactive.")

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
        # emit a single "📦  Upgraded vX → vY" message that supersedes
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
        body = "🛑  **Eneru Service Stopped**\nMonitoring is now inactive."
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
                    self._notification_worker._send_via_apprise_bounded(
                        body, notify_type,
                    )
                except Exception:
                    pass  # best-effort; nothing more we can do here

        self._stop_stats()

        # Slice 3: drop the shutdown marker so the next start can
        # classify this exit (signal → "🔄  Restarted" if it comes back
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

        self._clear_shutdown_in_progress()
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
            self.state.on_battery_start_mono = time.monotonic()  # ISS-020
            self.state.extended_time_logged = False
            self.state.battery_history.clear()

            self._log_power_event(
                "ON_BATTERY",
                f"Battery: {battery_charge}%, Runtime: {battery_runtime} seconds, Load: {ups_load}%"
            )
            if self._should_fire_wall():
                run_command([
                    "wall",
                    f"⚠️  WARNING: Power failure detected! System running on UPS battery "
                    f"({battery_charge}% remaining, {format_seconds(battery_runtime)} runtime)"
                ])

        current_time = int(time.time())
        # ISS-020: derive the trigger-relevant duration from the monotonic clock
        # so an NTP step mid-outage can't skew T3-grace / T4 timing. Fall back to
        # the wall delta only if the monotonic anchor is somehow unset.
        if self.state.on_battery_start_mono > 0:
            time_on_battery = int(
                time.monotonic() - self.state.on_battery_start_mono
            )
        else:
            time_on_battery = current_time - self.state.on_battery_start_time
        depletion_rate = self._calculate_depletion_rate(battery_charge)
        stabilization_delay = max(
            0, int(self.config.triggers.on_battery_stabilization_delay)
        )
        stabilizing = time_on_battery < stabilization_delay

        shutdown_reason = ""

        # T1. Critical battery level
        if is_numeric(battery_charge):
            battery_int = int(float(battery_charge))
            if battery_int < self.config.triggers.low_battery_threshold:
                if stabilizing:
                    self._log_message(
                        f"🕒  INFO: Low battery reading ({battery_int}% < "
                        f"{self.config.triggers.low_battery_threshold}%) ignored during "
                        f"on-battery stabilization "
                        f"({time_on_battery}s/{stabilization_delay}s)."
                    )
                else:
                    shutdown_reason = (
                        f"Battery charge {battery_int}% below threshold "
                        f"{self.config.triggers.low_battery_threshold}%"
                    )
        else:
            self._log_message(f"⚠️  WARNING: Received non-numeric battery charge value: '{battery_charge}'")

        # T2. Critical runtime remaining
        if not shutdown_reason and is_numeric(battery_runtime):
            runtime_int = int(float(battery_runtime))
            if runtime_int < self.config.triggers.critical_runtime_threshold:
                if stabilizing:
                    self._log_message(
                        f"🕒  INFO: Critical runtime reading "
                        f"({format_seconds(runtime_int)} < "
                        f"{format_seconds(self.config.triggers.critical_runtime_threshold)}) "
                        "ignored during on-battery stabilization "
                        f"({time_on_battery}s/{stabilization_delay}s)."
                    )
                else:
                    shutdown_reason = (
                        f"Runtime {format_seconds(runtime_int)} below threshold "
                        f"{format_seconds(self.config.triggers.critical_runtime_threshold)}"
                    )

        # T3. Dangerous depletion rate (with grace period)
        if not shutdown_reason and is_numeric(depletion_rate) and depletion_rate > 0:
            if depletion_rate > self.config.triggers.depletion.critical_rate:
                if stabilizing:
                    self._log_message(
                        f"🕒  INFO: High depletion rate ({depletion_rate}%/min) ignored during "
                        f"on-battery stabilization ({time_on_battery}s/{stabilization_delay}s)."
                    )
                elif time_on_battery < self.config.triggers.depletion.grace_period:
                    self._log_message(
                        f"🕒  INFO: High depletion rate ({depletion_rate}%/min) ignored during "
                        f"grace period ({time_on_battery}s/{self.config.triggers.depletion.grace_period}s)."
                    )
                else:
                    shutdown_reason = (
                        f"Depletion rate {depletion_rate}%/min above threshold "
                        f"{self.config.triggers.depletion.critical_rate}%/min (after grace period)"
                    )

        # T4. Extended time on battery
        if not shutdown_reason and time_on_battery > self.config.triggers.extended_time.threshold:
            if stabilizing:
                self._log_message(
                    f"🕒  INFO: Extended-time trigger ignored during on-battery "
                    f"stabilization ({time_on_battery}s/{stabilization_delay}s)."
                )
            elif self.config.triggers.extended_time.enabled:
                shutdown_reason = (
                    f"Time on battery {format_seconds(time_on_battery)} exceeded "
                    f"threshold {format_seconds(self.config.triggers.extended_time.threshold)}"
                )
            elif not self.state.extended_time_logged:
                self._log_message(
                    f"⏳  INFO: System on battery for {format_seconds(time_on_battery)} "
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
        elif (self._in_redundancy_group and not stabilizing
                and self.state.trigger_active):
            # ISS-016: a clean on-battery reading (no trigger reason) while an
            # advisory trigger is latched means conditions recovered (charge/
            # runtime back above threshold). Clear the latch so one transient
            # sub-threshold reading doesn't keep this member CRITICAL for the
            # whole outage and bias quorum toward group shutdown. Only clear on a
            # fully-clean reading, and never during stabilization.
            # NOTE: an FSD-originated advisory (latched in _main_loop) would also
            # clear here on a subsequent clean OB poll; in practice upsd keeps FSD
            # latched, and the group evaluator re-latches on the next FSD poll.
            self._clear_advisory_trigger()

        # Log on-battery status roughly every 5s. ISS-018: throttle on a
        # monotonic timestamp, not `int(time.time()) % 5 == 0` -- with a 5s poll
        # and an unlucky phase the modulo could never (or double-) fire.
        if time.monotonic() - self._last_ob_status_log_mono >= 5:
            self._last_ob_status_log_mono = time.monotonic()
            self._log_message(
                f"🔋  On battery: {battery_charge}% ({format_seconds(battery_runtime)}), "
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
            if self._should_fire_wall():
                run_command([
                    "wall",
                    f"✅  Power has been restored. UPS Status: {ups_status}. "
                    f"Battery at {battery_charge}%."
                ])

            self.state.on_battery_start_time = 0
            self.state.on_battery_start_mono = 0.0  # ISS-020
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
            self._clear_shutdown_in_progress()
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
                        f"⚠️  power_restored_callback raised: {exc}"
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
            # First failure: enter grace period. Hold the snapshot lock so
            # the redundancy evaluator never observes a torn pair (e.g.
            # GRACE_PERIOD with stale connection_lost_time).
            with self.state._lock:
                self.state.connection_state = "GRACE_PERIOD"
                self.state.connection_lost_time = time.time()
            if "Data stale" in error_msg:
                self._log_message(
                    f"⚠️  Connection to UPS {self.config.ups.name} lost "
                    f"(data stale). Grace period started "
                    f"({grace_cfg.duration}s)."
                )
            else:
                self._log_message(
                    f"⚠️  Connection to UPS {self.config.ups.name} lost. "
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
                with self.state._lock:
                    self.state.connection_state = "FAILED"
                    self.state.connection_lost_time = 0.0

        # If connection_state == "FAILED": already notified, nothing to do

    def _run_periodic_tasks(self, ups_data: Optional[Dict[str, str]] = None) -> None:
        """End-of-iteration per-UPS periodic tasks (v6.1).

        Time-gated and fully failure-isolated: any exception here is logged
        and swallowed so a scheduler/health hiccup can never interrupt the
        poll loop or the shutdown path. The battery-health interval is read
        live from config (SAFE hot-reload). ``ups_data`` is the latest
        successful poll, used by the passive self-test observer.
        """
        try:
            bh = self._resolve_battery_health_config()
            if bh.enabled:
                interval = max(1, int(bh.update_interval))
                now_mono = time.monotonic()
                if (self._last_health_update_mono is None
                        or now_mono - self._last_health_update_mono >= interval):
                    self._last_health_update_mono = now_mono
                    self._update_battery_health_periodic()
        except Exception as exc:
            self._log_message(f"⚠️  battery-health task failed: {exc}")
        try:
            self._run_self_test_task()
        except Exception as exc:
            self._log_message(f"⚠️  self-test task failed: {exc}")
        # Passive observation: record a self-test the UPS ran on its own
        # (device schedule or a manual test), independent of self_test.enabled.
        try:
            self._check_observed_self_test(ups_data)
        except Exception as exc:
            self._log_message(f"⚠️  self-test observer failed: {exc}")
        # Reports are daemon-wide (one digest). In multi-UPS mode the
        # coordinator owns them; a per-group monitor must not send N copies.
        try:
            # ISS-023: skip when there is no notification worker. maybe_send_due
            # _reports stamps last_report_sent_* even when the enqueue silently
            # no-ops without a worker, so a whole period's digest would be lost.
            # The coordinator path already guards this; mirror it here.
            if (not self._coordinator_mode and self.config.reports.enabled
                    and self._notification_worker is not None):
                reports_mod.maybe_send_due_reports(
                    self.config, getattr(self, "_stats_store", None),
                    self.config.ups.name, self._enqueue_report)
        except Exception as exc:
            self._log_message(f"⚠️  reports task failed: {exc}")

    def _enqueue_report(self, body: str, notify_type: str, category: str) -> None:
        """Adapter so reports.py can deliver via the notification queue."""
        self._send_notification(body, notify_type, category=category)

    def _resolve_self_test_config(self):
        """Per-UPS self_test override if present, else the global config."""
        glob = self.config.self_test
        # Resolve defensively with getattr() rather than a broad try/except: a
        # bare `except Exception: pass` here would hide a genuinely broken
        # per-UPS override and silently run the global config instead.
        name = getattr(getattr(self.config, "ups", None), "name", None)
        for group in getattr(self.config, "ups_groups", None) or []:
            group_ups = getattr(group, "ups", None)
            if (name is not None
                    and getattr(group_ups, "name", None) == name
                    and getattr(group, "self_test", None)):
                return group.self_test
        return glob

    def _resolve_nut_control_config(self) -> NutControlConfig:
        """Per-UPS nut_control override if present, else the global config."""
        glob = self.config.nut_control
        name = getattr(getattr(self.config, "ups", None), "name", None)
        for group in getattr(self.config, "ups_groups", None) or []:
            group_ups = getattr(group, "ups", None)
            if (name is not None
                    and getattr(group_ups, "name", None) == name
                    and getattr(group, "nut_control", None)):
                return group.nut_control
        return glob

    def _check_observed_self_test(self, ups_data: Optional[Dict[str, str]]) -> None:
        """Record a self-test the UPS ran on its own (device schedule or manual).

        ELI5: your UPS keeps its own logbook — "last test: passed, 2026-06-02".
        Eneru reads that logbook on every poll and copies a NEW settled entry
        into its own records exactly once. A fingerprint in the meta table stops
        it re-copying the same entry, and it stands down while an Eneru-issued
        test is mid-flight (that path owns its own row). This runs regardless of
        whether scheduled self-tests are enabled — some UPSes test on their own
        cadence, and some operators only ever test by hand.
        """
        store = getattr(self, "_stats_store", None)
        if store is None or not getattr(store, "is_open", False):
            return
        raw = (ups_data or {}).get("ups.test.result")
        if not raw:
            return  # this UPS doesn't report a test result
        enum = selftest.normalize_result(raw)
        # Only persist a SETTLED, meaningful result. running/unknown churn while a
        # test is in flight or was never run; unsupported is one-time noise.
        if enum not in ("passed", "failed"):
            return
        date = (ups_data or {}).get("ups.test.date") or ""
        key = f"{date}|{raw}"
        # Fingerprint of the last result Eneru already accounted for — via this
        # observer OR its own scheduled finalise (which stamps the same key).
        if store.get_meta("self_test_observed_key") == key:
            return
        # Never race the scheduled path: it owns the row for a test it issued.
        if self._self_test_pending_id is not None:
            return
        # command="" — Eneru issued nothing; this is the device's own test.
        test_id = store.record_self_test(
            "", "device", result_raw=raw, result_enum=enum,
            result_date=(date or None))
        if test_id is None:
            return  # write failed — don't fingerprint, so it retries next poll
        store.set_meta("self_test_observed_key", key)
        self._log_message(f"🔋 Observed UPS self-test: {enum} ({raw!r})")

    def _run_self_test_task(self) -> None:
        """Issue / poll the scheduled UPS self-test (v6.1).

        Wall-clock + meta-persisted due check (survives restarts, unlike a
        monotonic timer that would reset and never reach a 30-day cadence);
        the short result poll uses monotonic time. Self-disables if the UPS
        doesn't expose the command. Validation already guarantees nut_control
        + api.auth are enabled whenever self_test is.
        """
        cfg = self._resolve_self_test_config()
        store = getattr(self, "_stats_store", None)
        # The scheduler needs the stats store to dedup runs (last-run meta) and
        # record the result row. Without it we can neither track state nor avoid
        # re-issuing every tick, so skip rather than fire blindly. A store
        # created in __init__ but never opened (or already closed) silently
        # no-ops get_meta()/set_meta(), so treat a closed store as unavailable
        # too — otherwise scheduled tests would fire without state tracking.
        if store is None or not getattr(store, "is_open", False):
            return

        # 0) Recover an in-flight test persisted before a restart or issued by
        # the API. ELI5: the row id is the order ticket, and the due timestamp is
        # the kitchen timer. If either the scheduler or API starts a test, the
        # monitor can pick up the ticket later without polling too early.
        if self._self_test_pending_id is None:
            pend = store.get_meta(selftest.PENDING_ID_META)
            if pend:
                try:
                    self._self_test_pending_id = int(pend)
                    delay = 0.0
                    due_raw = store.get_meta(selftest.PENDING_DUE_TS_META)
                    if due_raw:
                        try:
                            delay = max(0.0, float(due_raw) - time.time())
                        except (TypeError, ValueError):
                            store.set_meta(selftest.PENDING_DUE_TS_META, "")
                    self._self_test_poll_due_mono = time.monotonic() + delay
                except (TypeError, ValueError):
                    selftest.clear_pending_self_test(store)
            else:
                latest_running = getattr(
                    store, "latest_running_self_test", lambda: None)()
                if latest_running:
                    due_ts = selftest.self_test_poll_due_ts(
                        latest_running["started_ts"], cfg.result_poll_after)
                    self._self_test_pending_id = int(latest_running["id"])
                    self._self_test_poll_due_mono = (
                        time.monotonic() + max(0.0, due_ts - time.time()))
                    selftest.persist_pending_self_test(
                        store, self._self_test_pending_id, due_ts)

        # 1) Finalise a pending test once its poll window has elapsed. This runs
        # BEFORE the cfg.enabled / nut_control gates below: a config change that
        # disables or retargets self_test must NOT orphan a test that was already
        # issued — its result (a read, not a control command) is finalised first.
        if (self._self_test_pending_id is not None
                and self._self_test_poll_due_mono is not None):
            if time.monotonic() >= self._self_test_poll_due_mono:
                raw = self._get_ups_var("ups.test.result")
                date = self._get_ups_var("ups.test.date")
                enum = selftest.record_self_test_result(
                    store, self._self_test_pending_id, raw, date)
                self._log_message(f"🔋 Self-test result: {enum} ({raw!r})")
                # Stamp the observer fingerprint so the passive path doesn't
                # re-record the same result Eneru just finalised.
                store.set_meta(
                    "self_test_observed_key", f"{date or ''}|{raw or ''}")
                self._self_test_pending_id = None
                self._self_test_poll_due_mono = None
                selftest.clear_pending_self_test(store)
            return  # never issue while one is in flight

        # Now honor the current config: a disabled self_test issues nothing
        # (the pending test, if any, was already finalised above).
        if not cfg.enabled:
            return
        nc = self._resolve_nut_control_config()
        # self_test is its own narrow permission (v6.1.2): enabling it grants
        # exactly cfg.command even when nut_control is otherwise off. Auth is the
        # real privilege gate and is enforced at config-validation time, so it
        # is not re-checked here. The effective nut_control (cfg.command
        # guaranteed on its allowlist; credentials/timeout inherited) is built
        # at issue time below.

        # 2) Due? (calendar/interval via the shared scheduler helpers)
        try:
            schedule = selftest.parse_schedule(cfg.schedule, cfg.time)
        except ValueError as exc:
            self._log_message(f"⚠️  invalid self_test.schedule: {exc}")
            return
        now = time.time()
        last_raw = store.get_meta("self_test_last_run") if store else None
        try:
            last = float(last_raw) if last_raw else None
        except (TypeError, ValueError):
            last = None
        if not schedule.due(now, last):
            if last is None:
                store.set_meta("self_test_last_run", str(int(now)))  # seed baseline
            return

        # A recent issue attempt failed: back off before retrying so a
        # persistently-broken self-test doesn't re-attempt every poll tick (the
        # cadence isn't stamped on failure, so `due` stays true).
        if (self._self_test_retry_after_mono is not None
                and time.monotonic() < self._self_test_retry_after_mono):
            return

        # Never issue a real control command against an AUTO-CORRECTED poll
        # target: _run_ups_name_diagnostic() keeps the rest of the control
        # surface on the configured ups.name until the operator fixes the
        # config, so a scheduled self-test must not be the one path that fires
        # an INSTCMD at the discovered UPS. Back off and wait for the fix.
        if getattr(self, "_ups_name_autocorrected", False):
            self._self_test_retry_after_mono = (
                time.monotonic() + SELF_TEST_ISSUE_RETRY_SECONDS)
            self._log_message(
                "⚠️  self-test skipped: polling target was auto-corrected; "
                "fix ups.name before scheduled control commands resume.")
            return

        # 3) Issue. Discover BEFORE stamping last_run so a transient ``upscmd -l``
        # failure retries next cycle instead of silently burning a (possibly
        # 30-day) cadence; a genuine "not exposed" still consumes the cycle.
        try:
            cmd = selftest.discover_self_test_command(
                self._poll_target, cfg.command, username=nc.username,
                password=nc.password, timeout=nc.timeout)
        except selftest.SelfTestUnavailable as exc:
            self._log_message(
                f"⚠️  self-test discovery failed ({exc}); retrying next cycle")
            return
        if cmd is None:
            store.set_meta("self_test_last_run", str(int(now)))  # genuinely unsupported
            self._self_test_retry_after_mono = None
            self._log_message(
                f"⚠️  self_test command '{cfg.command}' not exposed by "
                f"{self._poll_target}; skipping this cycle")
            return
        # Serialize against the API control path (same per-UPS lock identity) so
        # a scheduled self-test can't race an operator-issued command/self-test
        # on the same device.
        # Build the effective control the issue path uses: self_test.enabled
        # auto-allows exactly `cmd` (the general allowlist is untouched).
        _permitted, eff_nc = selftest.self_test_control(nc, cfg, cmd)
        with nutctl.command_lock(self._poll_target):
            result = selftest.issue_self_test(
                self._poll_target, cmd, eff_nc, store, source="scheduler")
        if result["ok"]:
            # Stamp the cadence ONLY once the issue succeeds: a failed issue
            # (NUT error, transient lock contention) must retry next cycle, not
            # silently burn a (possibly 30-day) interval. The genuinely-
            # unsupported path above still consumes the cycle.
            store.set_meta("self_test_last_run", str(int(now)))
            self._self_test_retry_after_mono = None
            self._self_test_pending_id = result["test_id"]
            poll_delay = max(1, int(cfg.result_poll_after))
            self._self_test_poll_due_mono = time.monotonic() + poll_delay
            if store is not None and result["test_id"] is not None:
                # Persist so a restart before the poll can recover + finalise it.
                selftest.persist_pending_self_test(
                    store, result["test_id"],
                    selftest.self_test_poll_due_ts(time.time(), poll_delay))
            self._log_message(
                f"🔋 Self-test issued ({cmd}); polling result in "
                f"{cfg.result_poll_after}s")
        else:
            # Don't stamp the cadence (so it retries), but back off so a
            # persistent failure doesn't re-attempt every poll tick.
            self._self_test_retry_after_mono = (
                time.monotonic() + SELF_TEST_ISSUE_RETRY_SECONDS)
            self._log_message(f"⚠️  self-test issue failed: {result['error']}")

    def _main_loop(self):
        """Main monitoring loop."""
        while not self._stop_event.is_set():
            success, ups_data, error_msg = self._get_all_ups_data()

            # ==================================================================
            # CONNECTION HANDLING AND FAILSAFE
            # ==================================================================

            if not success:
                # ``is_failsafe_trigger`` drives the OFF-battery grace path and
                # fires on the first hard error (unchanged). ``onbattery_failsafe``
                # gates the irreversible ON-battery shutdown and is debounced for
                # hard errors (H3) the same way stale data already is.
                is_failsafe_trigger = False
                onbattery_failsafe = False
                # Coerce defensively: a quoted YAML max_stale_data_tolerance
                # ("3") would otherwise make the `count >= tolerance` comparisons
                # below raise TypeError and kill the loop on a failed poll. (Also
                # validated at config load, but guard the hot path too.)
                try:
                    tolerance = max(1, int(self.config.ups.max_stale_data_tolerance))
                except (TypeError, ValueError):
                    tolerance = 3

                if "Data stale" in error_msg:
                    self.state.stale_data_count += 1
                    if self.state.connection_state not in ("FAILED", "GRACE_PERIOD"):
                        self._log_message(
                            f"⚠️  WARNING: Data stale from UPS {self.config.ups.name} "
                            f"(Attempt {self.state.stale_data_count}/{tolerance})."
                        )

                    if self.state.stale_data_count >= tolerance:
                        is_failsafe_trigger = True
                        onbattery_failsafe = True
                else:
                    self.state.connection_error_count += 1
                    if self.state.connection_state not in ("FAILED", "GRACE_PERIOD"):
                        self._log_message(
                            f"❌  ERROR: Cannot connect to UPS {self.config.ups.name} "
                            f"(Attempt {self.state.connection_error_count}/"
                            f"{tolerance}). Output: {error_msg}"
                        )
                    # Diagnose once per failure episode (the counter resets to 0
                    # on the next successful poll): list the server's real UPS
                    # names and self-heal an obviously-wrong ups.name (issue #71).
                    # Skip while On Battery: the discovery subprocess (up to 10s)
                    # must never sit in front of an FSB emergency shutdown, and a
                    # wrong ups.name could not have produced an "OB" status in the
                    # first place, so there is nothing to discover on that path.
                    # ISS-022: the counter resets on every successful poll, so a
                    # once-a-minute flap would pay the ~10s upsc -l probe on each
                    # episode, stalling the poll thread (delays state publishing;
                    # interacts with the health-model stale window). Gate it on a
                    # 10-minute monotonic cooldown so a flapping server is probed
                    # at most once per cooldown window.
                    if (self.state.connection_error_count == 1
                            and "OB" not in self.state.previous_status
                            and time.monotonic() - self._last_name_diagnostic_mono
                            > 600):
                        self._last_name_diagnostic_mono = time.monotonic()
                        self._run_ups_name_diagnostic(error_msg)
                    self.state.stale_data_count = 0
                    # Off-battery: first hard error feeds the grace period as
                    # before. On-battery (H3): require max_stale_data_tolerance
                    # consecutive hard failures before the IRREVERSIBLE shutdown,
                    # so a single transient NUT blip (connection refused during a
                    # upsd restart, or a 30s upsc timeout -> run_command 124)
                    # can't drop a healthy host riding out a survivable dip. Set
                    # max_stale_data_tolerance=1 to restore instant FSB.
                    is_failsafe_trigger = True
                    if self.state.connection_error_count >= tolerance:
                        onbattery_failsafe = True

                # FAILSAFE: If connection lost while on battery, shut down.
                # This is NEVER affected by the grace period.
                if onbattery_failsafe and "OB" in self.state.previous_status:
                    with self.state._lock:
                        self.state.connection_state = "FAILED"
                        self.state.connection_lost_time = 0.0
                    # ``stale_data_count`` is intentionally NOT reset here:
                    # once connection_state == "FAILED", health_model short-
                    # circuits to UNKNOWN regardless of the count, and the
                    # next successful poll resets it (see line ~1486).
                    if self._failsafe_initiated:
                        # H2: already acted on this outage's FAILSAFE. Do NOT
                        # re-run the sequence every poll while NUT stays
                        # unreachable. Non-halting configs (local_shutdown
                        # disabled, dry-run, delegated) clear the shutdown flag
                        # on completion, so without this latch the entire
                        # remote/VM/container sequence would re-fire each poll
                        # and flood notifications. The latch resets on the next
                        # successful poll (recovery).
                        pass
                    elif self._in_redundancy_group:
                        # Advisory mode: redundancy-group evaluator owns the
                        # shutdown decision. Recording the trigger here +
                        # reporting connection_state=FAILED is enough -- the
                        # group's policy + min_healthy decide what happens.
                        self._failsafe_initiated = True
                        self._record_advisory_trigger(
                            "FAILSAFE (FSB): connection lost while On Battery"
                        )
                        self._log_message(
                            "🚨  FAILSAFE (advisory, redundancy group): connection lost "
                            "while On Battery; deferring to group evaluator."
                        )
                    else:
                        self._failsafe_initiated = True
                        self._mark_shutdown_in_progress("starting failsafe shutdown")
                        self._log_message(
                            "🚨  FAILSAFE TRIGGERED (FSB): Connection lost or data persistently stale "
                            "while On Battery. Initiating emergency shutdown."
                        )
                        # Send notification (non-blocking - fire and forget)
                        self._send_notification(
                            "🚨  **FAILSAFE (FSB) TRIGGERED!**\n"
                            "Connection to UPS lost or data stale while system was running On Battery.\n"
                            "Assuming critical failure. Executing immediate shutdown.",
                            self.config.NOTIFY_FAILURE,
                            category="shutdown",
                        )
                        self._execute_shutdown_sequence()

                # On battery but the hard-error debounce hasn't reached tolerance
                # yet (H3): wait for the next poll. Do NOT start the off-battery
                # grace machinery and do NOT shut down -- just let the counter
                # accumulate. (Stale-data-below-tolerance lands here too, exactly
                # as before, since neither flag is set yet.)
                elif "OB" in self.state.previous_status:
                    pass

                # Grace period logic (only when NOT on battery)
                elif is_failsafe_trigger:
                    self._handle_connection_failure(error_msg)

                self._stop_event.wait(CONNECTION_RETRY_WAIT_SECONDS)
                continue

            # ==================================================================
            # DATA PROCESSING
            # ==================================================================

            # A successful poll clears the failure debounce counters: NUT is
            # visible again. The FAILSAFE latch is NOT reset here -- a brief NUT
            # reconnection while the UPS is still ON BATTERY must not re-arm the
            # failsafe (which would let the next hard error re-drain in dry-run/
            # delegated/enabled=false setups). The latch is per-OUTAGE: it clears
            # only once the UPS is back on line power (see below).
            self.state.stale_data_count = 0
            self.state.connection_error_count = 0

            if self.state.connection_state == "GRACE_PERIOD":
                # Recovered during grace period: quiet recovery, no notification
                elapsed = time.time() - self.state.connection_lost_time
                self._log_message(
                    f"✅  Connection to UPS {self.config.ups.name} recovered during "
                    f"grace period ({elapsed:.0f}s elapsed). No notification sent."
                )
                with self.state._lock:
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
                        f"⚠️  NUT server is unstable: connection to UPS {self.config.ups.name} "
                        f"has flapped {self.state.connection_flap_count} times."
                    )
                    self._send_notification(
                        f"⚠️  **NUT Server Unstable**\n"
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
                with self.state._lock:
                    self.state.connection_state = "OK"
                    self.state.connection_lost_time = 0.0
                self.state.connection_flap_count = 0
                self.state.connection_first_flap_time = 0.0
                if self._in_redundancy_group:
                    self._clear_advisory_trigger()

            ups_status = ups_data.get('ups.status', '')

            # ISS-019: match on whitespace-separated status TOKENS, not
            # substrings. NUT statuses are space-separated flags, so `"CHRG" in
            # ups_status` also matched `DISCHRG` (shielded here only by elif
            # order); token membership fixes that aliasing structurally and lets
            # neutral statuses (OFF, BYPASS, bare DISCHRG) fall through cleanly.
            status_tokens = set(ups_status.split())

            # Re-arm the FAILSAFE latch only when the outage is over: we have a
            # VALID status AND it shows line power. A missing/empty status is an
            # unresolved state, not "on line", so it must NOT re-arm the latch
            # mid-outage (cubic). On battery a reconnection alone doesn't end the
            # outage either, so the latch is per-outage, not per-reconnect.
            if ups_status and "OB" not in status_tokens and "FSD" not in status_tokens:
                self._failsafe_initiated = False

            if not ups_status:
                self._log_message(
                    "❌  ERROR: Received data from UPS but 'ups.status' is missing. "
                    "Check NUT configuration."
                )
                self._stop_event.wait(CONNECTION_RETRY_WAIT_SECONDS)
                continue

            self._save_state(ups_data)

            # Detect status changes
            if ups_status != self.state.previous_status and self.state.previous_status:
                battery_charge = ups_data.get('battery.charge', '')
                battery_runtime = ups_data.get('battery.runtime', '')
                ups_load = ups_data.get('ups.load', '')
                self._log_message(
                    f"🔄  Status changed: {self.state.previous_status} -> {ups_status} "
                    f"(Battery: {battery_charge}%, Runtime: {format_seconds(battery_runtime)}, "
                    f"Load: {ups_load}%)"
                )

            # ==================================================================
            # POWER STATE ANALYSIS AND SHUTDOWN TRIGGERS
            # ==================================================================

            if "FSD" in status_tokens:
                if self._in_redundancy_group:
                    self._record_advisory_trigger(
                        "UPS signaled FSD (Forced Shutdown) flag."
                    )
                else:
                    self._trigger_immediate_shutdown(
                        "UPS signaled FSD (Forced Shutdown) flag."
                    )

            elif "OB" in status_tokens:
                self._handle_on_battery(ups_data)

            elif "OL" in status_tokens or "CHRG" in status_tokens:
                self._handle_on_line(ups_data)

            else:
                # ISS-019: neutral/unrecognized status (OFF, BYPASS, bare
                # DISCHRG, ...). Neither on-line nor on-battery: take no power-
                # state action and do not reset on_battery timing here. (The
                # per-outage failsafe latch is handled separately above at its
                # own re-arm check, which clears on any non-OB/non-FSD status.)
                # Note it, throttled, for visibility; environment checks below
                # still run on the raw status.
                if (ups_status != self._last_unknown_status_logged
                        or time.monotonic() - self._last_unknown_status_log_mono
                        >= 300):
                    self._last_unknown_status_logged = ups_status
                    self._last_unknown_status_log_mono = time.monotonic()
                    self._log_message(
                        f"ℹ️  UPS status '{ups_status}' is neither on-line nor "
                        "on-battery; no power-state action taken."
                    )

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
                self.state.latest_input_voltage = ups_data.get('input.voltage', '')
                self.state.latest_output_voltage = ups_data.get('output.voltage', '')
                self.state.latest_battery_voltage = ups_data.get('battery.voltage', '')
                self.state.latest_ups_temperature = ups_data.get('ups.temperature', '')
                self.state.latest_input_frequency = ups_data.get('input.frequency', '')
                self.state.latest_output_frequency = ups_data.get('output.frequency', '')
                # ISS-020: publish the MONOTONIC on-battery duration. This field
                # becomes snapshot.time_on_battery, which the redundancy
                # evaluator's assess_health() feeds into DECISION logic
                # (stabilization gate, depletion grace, extended-time), not just
                # display — so an NTP step mid-outage must not skew it either.
                # Wall fallback only if the monotonic anchor is unset.
                if self.state.on_battery_start_mono > 0:
                    self.state.latest_time_on_battery = int(
                        time.monotonic() - self.state.on_battery_start_mono
                    )
                elif self.state.on_battery_start_time > 0:
                    self.state.latest_time_on_battery = (
                        int(time.time()) - self.state.on_battery_start_time
                    )
                else:
                    self.state.latest_time_on_battery = 0
                self.state.latest_update_time = time.time()
                self.state.previous_status = ups_status

            # v6.1: time-gated per-UPS periodic tasks (battery-health update,
            # self-test). Failure-isolated so a scheduler hiccup can never
            # touch the poll/shutdown path.
            self._run_periodic_tasks(ups_data)

            self._stop_event.wait(self.config.ups.check_interval)
