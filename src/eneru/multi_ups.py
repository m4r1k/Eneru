"""Multi-UPS coordination.

Hosts the :class:`MultiUPSCoordinator`, which spins up one
:class:`~eneru.monitor.UPSGroupMonitor` thread per configured UPS group and
owns the shared resources (logger, notification worker) plus local-shutdown
arbitration with defense-in-depth (in-memory lock + filesystem flag).
"""

import sys
import time
import signal
import threading
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from eneru.version import __version__
from eneru.config import Config
from eneru.logger import UPSLogger
from eneru.notifications import APPRISE_AVAILABLE, NotificationWorker
from eneru.monitor import UPSGroupMonitor
from eneru.redundancy import RedundancyGroupEvaluator, RedundancyGroupExecutor
from eneru.stats import StatsStore
from eneru.lifecycle import (
    REASON_SEQUENCE_COMPLETE,
    REASON_SIGNAL,
    classify_startup,
    delete_shutdown_marker,
    delete_upgrade_marker,
    read_shutdown_marker,
    read_upgrade_marker,
    write_shutdown_marker,
)
from eneru.utils import run_command


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

        # Redundancy-group runtime (Phase 2). Populated after monitors start.
        self._redundancy_executors: dict = {}
        self._evaluator_threads: List[threading.Thread] = []
        # UPS names that belong to at least one redundancy group -- precomputed
        # so each per-UPS monitor can be marked advisory at construction time.
        self._in_redundancy = {
            name
            for rg in config.redundancy_groups
            for name in rg.ups_sources
        }

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

        # Slice 3: emit ONE classified lifecycle notification at the
        # coordinator level (the per-monitor _emit_lifecycle_startup
        # is suppressed in coordinator_mode so we don't get N copies).
        # Markers + classification ALWAYS run; the SEND only fires when
        # a notification worker is configured. Otherwise the markers
        # would leak across configurations (P2 finding from review).
        stats_dir = Path(self.config.statistics.db_directory)
        shutdown_marker = read_shutdown_marker(stats_dir)
        upgrade_marker = read_upgrade_marker(stats_dir)

        # Pip-path upgrade detection in coord mode: peek at the first
        # group's stats DB for meta.last_seen_version. Without this the
        # coordinator passes None and a pip user upgrading via
        # `pip install -U eneru` between runs gets "Restarted" instead
        # of "📦 Upgraded vX → vY" (caught in pre-push review).
        last_seen = self._read_last_seen_version_from_first_group(stats_dir)

        body, notify_type = classify_startup(
            current_version=__version__,
            shutdown_marker=shutdown_marker,
            upgrade_marker=upgrade_marker,
            last_seen_version=last_seen,
        )
        if self._notification_worker:
            self._notification_worker.send(
                body=body, notify_type=notify_type, category="lifecycle",
            )
        # Always consume the markers so the next start doesn't replay
        # this classification. Safe whether or not the SEND happened —
        # the per-monitor lifecycle pass updates last_seen_version on
        # every store from inside its _emit_lifecycle_startup_notification
        # (which still runs in coordinator mode for the meta side).
        delete_shutdown_marker(stats_dir)
        delete_upgrade_marker(stats_dir)

    def _read_last_seen_version_from_first_group(self, stats_dir):
        """Read meta.last_seen_version from the first group's stats DB
        (read-only). Returns ``None`` if the DB doesn't exist yet (first
        ever start) or the row is missing.

        Coordinator startup runs BEFORE any monitor opens its DB write
        connection, so we use StatsStore.open_readonly to avoid stepping
        on the writer. Any exception here is swallowed — the worst case
        is the classifier degrades to "no pip-path upgrade detected"."""
        if not self.config.ups_groups:
            return None
        first = self.config.ups_groups[0]
        sanitized = first.ups.name.replace("@", "-").replace(":", "-").replace("/", "-")
        db_path = stats_dir / f"{sanitized}.db"
        try:
            conn = StatsStore.open_readonly(db_path)
        except Exception:
            return None
        if conn is None:
            return None
        try:
            row = conn.execute(
                "SELECT value FROM meta WHERE key='last_seen_version'"
            ).fetchone()
            return str(row[0]) if row and row[0] else None
        except Exception:
            return None
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _start_monitors(self):
        """Create and start one UPSGroupMonitor thread per group."""
        for group in self.config.ups_groups:
            # Build a single-group Config for this monitor. Every shared
            # field on Config that affects per-group behavior must be passed
            # through here -- omissions silently fall back to dataclass
            # defaults and the user's YAML is ignored in multi-UPS mode.
            group_config = Config(
                ups_groups=[group],
                behavior=self.config.behavior,
                logging=self.config.logging,
                notifications=self.config.notifications,
                local_shutdown=self.config.local_shutdown,
                statistics=self.config.statistics,
            )

            # Sanitize UPS name for file paths
            sanitized = group.ups.name.replace("@", "-").replace(":", "-").replace("/", "-")
            prefix = f"[{group.ups.label}] "

            in_rg = group.ups.name in self._in_redundancy

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
                in_redundancy_group=in_rg,
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
            tags = []
            if group.is_local:
                tags.append("is_local")
            if in_rg:
                tags.append("redundancy")
            tag_suffix = f" [{', '.join(tags)}]" if tags else ""
            self._log(f"  Started monitor thread for {group.ups.label}{tag_suffix}")

        # ------- Redundancy groups (Phase 2) -------
        if self.config.redundancy_groups:
            monitors_by_name = {m.config.ups.name: m for m in self._monitors}
            for rg in self.config.redundancy_groups:
                executor = RedundancyGroupExecutor(
                    rg,
                    base_config=self.config,
                    logger=self._logger,
                    log_prefix=f"[redundancy:{rg.name}] ",
                    stop_event=self._stop_event,
                    notification_worker=self._notification_worker,
                    local_shutdown_callback=self._handle_local_shutdown,
                )
                self._redundancy_executors[rg.name] = executor
                evaluator = RedundancyGroupEvaluator(
                    rg,
                    monitors_by_name,
                    executor,
                    stop_event=self._stop_event,
                    logger=self._logger,
                    log_prefix=f"[redundancy:{rg.name}] ",
                )
                evaluator.start()
                self._evaluator_threads.append(evaluator)
                self._log(
                    f"  Started redundancy evaluator '{rg.name}' "
                    f"({len(rg.ups_sources)} sources, min_healthy={rg.min_healthy})"
                )

    def _run_monitor(self, monitor: UPSGroupMonitor, group):
        """Thread target: run a single UPS monitor."""
        try:
            monitor.run()
        except Exception as e:
            label = group.ups.label
            self._log(f"❌ Monitor thread for {label} crashed: {e}")
            if self._notification_worker:
                # The monitor's _stats_store is opened by _initialize_notifications,
                # so it's safe to pin the destination here.
                self._notification_worker.send(
                    f"❌ **Monitor Crashed:** {label}\nError: {e}",
                    "failure",
                    category="lifecycle",
                    store=getattr(monitor, "_stats_store", None),
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
                        category="shutdown_summary",
                    )
                    # Drain in flight before halt; lossless guarantee on
                    # what doesn't make it.
                    self._notification_worker.flush(timeout=5)
                else:
                    time.sleep(5)
                # Slice 3: tag this shutdown as power-loss-triggered so
                # the next start can emit "📊 Recovered" and the Slice 4
                # bonus folds the prev shutdown into a richer message.
                # (Single-UPS path already does this in monitor.py;
                # coordinator mode was missing it — caught in pre-push
                # review.)
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

        Order matters: signal stop_event first so peer monitors stop
        their main loops, give them a brief window to drain, then run
        the per-monitor shutdown sequence sequentially. Running shutdown
        on peers while their loops are still active risked concurrent
        access to the same shutdown path (notifications, state-file
        writes) inside the peer's poll cycle.
        """
        self._log("⏳ Draining all UPS groups -- shutting down their resources...")

        # Phase 1: signal every peer monitor to stop its poll loop, then
        # join with a short window so the loops exit before we run their
        # shutdown sequences.
        self._stop_event.set()
        join_deadline = time.time() + max(1, timeout // 4)
        for thread in self._threads:
            remaining = max(0.0, join_deadline - time.time())
            thread.join(timeout=remaining)

        # Phase 2: run each monitor's shutdown sequence sequentially.
        for monitor in self._monitors:
            if not monitor._shutdown_flag_path.exists():
                self._log(f"  ➡️ Triggering shutdown for {monitor._log_prefix.strip()}")
                try:
                    monitor._execute_shutdown_sequence()
                except Exception as e:
                    self._log(f"  ⚠️ Error during drain shutdown: {e}")

        # Final join window for any threads still wrapping up.
        deadline = time.time() + timeout
        for thread in self._threads:
            remaining = max(0.0, deadline - time.time())
            thread.join(timeout=remaining)
        still_running = [t for t in self._threads if t.is_alive()]
        if still_running:
            self._log(f"⚠️ {len(still_running)} monitor(s) still running after drain timeout")

    def _wait_for_completion(self):
        """Block until all monitors finish or a signal is received."""
        try:
            while not self._stop_event.is_set():
                # Check if any monitor or evaluator thread is still alive
                alive = [t for t in self._threads if t.is_alive()]
                alive += [t for t in self._evaluator_threads if t.is_alive()]
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
                category="lifecycle",
            )

        self._stop_event.set()

        # Wait briefly for monitor + evaluator threads to finish
        for thread in self._threads:
            thread.join(timeout=5)
        for thread in self._evaluator_threads:
            thread.join(timeout=5)

        if self._notification_worker:
            # Same SIGTERM-race fix as in monitor.py:_cleanup_and_exit:
            # drain the queue before joining the worker thread so the
            # final 'Service Stopped' notification actually ships.
            self._notification_worker.flush(timeout=5)
            self._notification_worker.stop()

        # Slice 3: tag this exit so the next start can emit "🔄 Restarted"
        # if it comes back within RESTART_DOWNTIME_THRESHOLD_SECS, else
        # "🚀 Started (last seen Nh ago)". Coordinator mode was missing
        # this — caught in pre-push review.
        write_shutdown_marker(
            Path(self.config.statistics.db_directory),
            version=__version__,
            reason=REASON_SIGNAL,
        )

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
