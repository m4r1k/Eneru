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
