"""Multi-UPS coordination.

Hosts the :class:`MultiUPSCoordinator`, which spins up one
:class:`~eneru.monitor.UPSGroupMonitor` thread per configured UPS group and
owns the shared resources (logger, notification worker) plus local-shutdown
arbitration with defense-in-depth (in-memory lock + filesystem flag).
"""

import sqlite3
import sys
import time
import signal
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from eneru.version import __version__
from eneru.config import Config
from eneru.logger import UPSLogger
from eneru.notifications import APPRISE_AVAILABLE, NotificationWorker
from eneru.monitor import UPSGroupMonitor
from eneru.api import EneruAPIServer
from eneru.mqtt import MQTTPublisher
from eneru.remote_health import RemoteHealthManager, remote_health_sidecar_path
from eneru.deferred_delivery import schedule_deferred_stop_or_eager_send
from eneru.redundancy import RedundancyGroupEvaluator, RedundancyGroupExecutor
from eneru.stats import StatsStore
from eneru.status import redundancy_state_file_path
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
        # M1: set while a committed local shutdown is running outside the lock,
        # so recovery can't re-arm the guard mid-flight and admit a 2nd poweroff.
        self._local_shutdown_in_flight = False
        # If a recovery (OB->OL) arrives DURING the in-flight window we defer the
        # re-arm to the end of _handle_local_shutdown instead of dropping it --
        # otherwise a no-op/non-halting shutdown would stay latched forever and
        # block the next outage (cubic P1).
        self._rearm_after_inflight = False
        # L5: re-entrancy guard for the SIGTERM/SIGINT handler.
        self._signal_handling = False
        self._global_shutdown_flag = Path(config.logging.shutdown_flag_file)

        # Shared resources
        self._logger: Optional[UPSLogger] = None
        self._notification_worker: Optional[NotificationWorker] = None
        self._api_server: Optional[EneruAPIServer] = None
        self._mqtt_publisher: Optional[MQTTPublisher] = None
        self._redundancy_remote_health_managers: List[RemoteHealthManager] = []

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

    def _clear_global_shutdown_flag(self, context: str) -> None:
        """Best-effort cleanup for the coordinator's cross-process guard."""
        try:
            self._global_shutdown_flag.unlink(missing_ok=True)
        except OSError as exc:
            self._log(
                f"⚠️  Could not clear global shutdown flag "
                f"{self._global_shutdown_flag} during {context}: {exc}"
            )

    def _global_shutdown_guard_active(self) -> bool:
        """Return whether the coordinator-level shutdown guard is active."""
        try:
            return self._global_shutdown_flag.exists()
        except OSError as exc:
            self._log(
                f"⚠️  Could not inspect global shutdown flag "
                f"{self._global_shutdown_flag}: {exc}"
            )
            return False

    def _monitor_shutdown_guard_active(self, monitor: UPSGroupMonitor) -> bool:
        """Return whether a child monitor has admitted a shutdown sequence."""
        guard = getattr(type(monitor), "_shutdown_guard_active", None)
        if callable(guard):
            try:
                return bool(guard(monitor))
            except Exception as exc:
                self._log(
                    "  ⚠️  Could not inspect monitor shutdown guard via helper: "
                    f"{exc}. Falling back to raw guard fields."
                )

        if bool(vars(monitor).get("_shutdown_in_progress", False)):
            return True
        flag_path = getattr(monitor, "_shutdown_flag_path", None)
        if flag_path is None:
            return False
        try:
            return bool(flag_path.exists())
        except OSError as exc:
            self._log(
                f"  ⚠️  Could not inspect monitor shutdown flag {flag_path}: {exc}"
            )
            return False

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
        # SIGHUP hot-reloads config across every group (systemctl reload /
        # docker kill -s HUP).
        signal.signal(signal.SIGHUP, self._handle_sighup)

        self._logger = UPSLogger(self.config.logging.file, self.config)

        if self.config.logging.file:
            try:
                Path(self.config.logging.file).touch(exist_ok=True)
            except PermissionError:
                pass

        self._clear_global_shutdown_flag("startup")

        # Initialize shared notification worker
        if self.config.notifications.enabled and APPRISE_AVAILABLE:
            self._notification_worker = NotificationWorker(self.config)
            if self._notification_worker.start():
                count = self._notification_worker.get_service_count()
                self._log(f"📢  Notifications: enabled ({count} service(s))")
            else:
                self._log("⚠️  WARNING: Failed to initialize notifications")
                self._notification_worker = None

        group_count = len(self.config.ups_groups)
        self._log(f"🚀  Eneru v{__version__} starting - multi-UPS mode ({group_count} groups)")

        if self.config.behavior.dry_run:
            self._log("🧪  *** RUNNING IN DRY-RUN MODE - NO ACTUAL SHUTDOWN WILL OCCUR ***")

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
        # of "📦  Upgraded vX → vY" (caught in pre-push review).
        last_seen = self._read_last_seen_version_from_first_group(stats_dir)

        body, notify_type = classify_startup(
            current_version=__version__,
            shutdown_marker=shutdown_marker,
            upgrade_marker=upgrade_marker,
            last_seen_version=last_seen,
        )
        # v5.2.1: sweep each per-UPS store for any pending lifecycle row
        # left over from the previous instance (the deferred 'Service
        # Stopped' from _handle_signal) and cancel it BEFORE the new
        # lifecycle send. The single-UPS path does this inside
        # _emit_lifecycle_startup_notification (monitor.py) but that
        # function early-returns in coordinator mode without reaching
        # the cancel block, so without this sweep multi-UPS users still
        # see two notifications on every restart/upgrade. Order matters:
        # the new lifecycle send below goes to the worker's in-memory
        # buffer (no stores are registered yet at this point in startup);
        # the per-UPS monitors register their stores in _start_monitors,
        # which drains the buffer into them as new pending rows. So the
        # cancel-then-send order here is safe — the cancel only sees
        # rows already on disk from the previous process.
        self._cancel_prev_pending_lifecycle_rows(stats_dir)

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

    def _cancel_prev_pending_lifecycle_rows(self, stats_dir: Path) -> None:
        """Cancel any pending lifecycle row left in each per-UPS store
        from the previous process instance. Mirrors the cancel block in
        ``UPSGroupMonitor._emit_lifecycle_startup_notification`` so the
        coordinator path produces exactly one lifecycle notification per
        restart/upgrade — same contract as single-UPS mode.

        Best-effort by design: any sqlite or filesystem error during the
        sweep is swallowed (a transient failure must not break startup;
        the worst case is the user sees a stop + start pair on this one
        restart, which is the v5.2.0 bug — not worse than the prior
        state). Stores that don't exist yet (first-ever start) are
        silently skipped.
        """
        for group in self.config.ups_groups:
            sanitized = (group.ups.name
                         .replace("@", "-")
                         .replace(":", "-")
                         .replace("/", "-"))
            db_path = stats_dir / f"{sanitized}.db"
            if not db_path.exists():
                continue
            store = StatsStore(db_path)
            try:
                store.open()
                for row in store.find_pending_by_category("lifecycle"):
                    store.cancel_notification(row[0], "superseded")
            except (sqlite3.Error, OSError) as e:
                # Visible-failure (CodeRabbit + Cubic P2): a silent
                # except: pass made the duplicate-lifecycle symptom
                # opaque if SQLite or the filesystem misbehaved during
                # the sweep. One log line keeps it best-effort while
                # giving the operator a thread to pull on.
                self._log(
                    f"⚠️  Lifecycle sweep skipped for {db_path.name}: {e}"
                )
            finally:
                try:
                    store.close()
                except (sqlite3.Error, OSError) as e:
                    self._log(
                        f"⚠️  Failed to close stats DB {db_path.name}: {e}"
                    )

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
                api=self.config.api,
                prometheus=self.config.prometheus,
                remote_health=self.config.remote_health,
                mqtt=self.config.mqtt,
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
                power_restored_callback=self._clear_local_shutdown_state,
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
                # 5.3.0 contract: daemon owns the redundancy flag's
                # lifecycle. Clear any stale flag from a prior daemon
                # instance so the executor starts from a known-clean
                # state. Mirrors the per-UPS unlink at line 95 above.
                try:
                    executor.clear_shutdown_state(refuse_active_peer=True)
                except Exception as e:
                    self._log(
                        f"❌  FATAL ERROR: Cannot clear redundancy shutdown flag "
                        f"for '{rg.name}' at startup: {e}"
                    )
                    sys.exit(1)
                self._redundancy_executors[rg.name] = executor
                self._start_redundancy_remote_health(rg, monitors_by_name)
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

        self._start_api_server()
        self._start_mqtt_publisher()

    def _start_redundancy_remote_health(
        self,
        group,
        monitors_by_name: Dict[str, UPSGroupMonitor],
    ) -> None:
        """Start advisory SSH healthchecks for redundancy-group remotes."""
        enabled_servers = [s for s in group.remote_servers if s.enabled]
        if not enabled_servers:
            # If a previous run had enabled servers, the sidecar still
            # exists on disk and the API/TUI/MQTT will surface it as
            # current state. Remove it so consumers don't see ghost
            # entries after the operator turned remote-health off.
            stale = remote_health_sidecar_path(
                redundancy_state_file_path(self.config, group.name)
            )
            try:
                stale.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass
            return

        notify_fn = None
        if self._notification_worker is not None:
            def notify_fn(body, typ):
                if self._notification_worker is not None:
                    self._notification_worker.send(body, typ, category="health")

        manager = RemoteHealthManager(
            config=self.config,
            group_label=f"redundancy:{group.name}",
            servers=enabled_servers,
            sidecar_path=remote_health_sidecar_path(
                redundancy_state_file_path(self.config, group.name)
            ),
            stop_event=self._stop_event,
            log_fn=self._log,
            notify_fn=notify_fn,
            event_fn=lambda event_type, detail, notification_sent: (
                self._record_redundancy_remote_health_event(
                    group, monitors_by_name, event_type, detail,
                    notification_sent,
                )
            ),
        )
        self._redundancy_remote_health_managers.append(manager)
        manager.start()

    def _record_redundancy_remote_health_event(
        self,
        group,
        monitors_by_name: Dict[str, UPSGroupMonitor],
        event_type: str,
        detail: str,
        notification_sent: bool,
    ) -> None:
        """Write redundancy remote-health transitions to member UPS stores.

        Stats are per-UPS in the current schema, so a redundancy-group
        remote-health transition is fanned out to every member's events
        table. The detail string carries the ``redundancy:<group>``
        prefix and the originating server, so per-UPS event readers
        (TUI, /api/v1/events) still attribute the event correctly even
        though it appears in N rows for an N-UPS group. A future
        group-scoped events store would let this become a single write.

        Each member's ``log_event`` is wrapped in its own try/except so
        a broken or not-yet-opened stats DB on one member doesn't
        suppress the fan-out to the rest. Stats are diagnostic only and
        must never block the health-check path.
        """
        for source_name in getattr(group, "ups_sources", []):
            monitor = monitors_by_name.get(source_name)
            store = getattr(monitor, "_stats_store", None)
            if store is None:
                continue
            try:
                store.log_event(
                    event_type,
                    detail,
                    notification_sent=notification_sent,
                )
            except Exception as exc:
                self._log(
                    f"⚠️  stats: failed to record redundancy remote-health "
                    f"event on {source_name}: {exc}"
                )

    def _start_api_server(self):
        """Start the read-only API server for coordinator mode."""
        if self._api_server is not None:
            return
        self._api_server = EneruAPIServer(self, self.config, log_fn=self._log)
        self._api_server.start()

    def _start_mqtt_publisher(self):
        """Start optional outbound MQTT publishing for coordinator mode."""
        if self._mqtt_publisher is not None:
            return
        self._mqtt_publisher = MQTTPublisher(
            self, self.config, self._stop_event, log_fn=self._log,
        )
        self._mqtt_publisher.start()

    def _run_monitor(self, monitor: UPSGroupMonitor, group):
        """Thread target: run a single UPS monitor."""
        try:
            monitor.run()
        except Exception as e:
            label = group.ups.label
            self._log(f"❌  Monitor thread for {label} crashed: {e}")
            if self._notification_worker:
                # Pin to the monitor's store ONLY if it actually opened
                # (the crash may have happened before _initialize_notifications
                # got that far). Otherwise pass None so the worker can
                # fall back to another registered store or the pre-store
                # buffer (Cubic P2).
                store = getattr(monitor, "_stats_store", None)
                if store is not None and getattr(store, "_conn", None) is None:
                    store = None
                self._notification_worker.send(
                    f"❌  **Monitor Crashed:** {label}\nError: {e}",
                    "failure",
                    category="lifecycle",
                    store=store,
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
            self._log(f"🛑  Group {label} shutdown complete. Exiting (--exit-after-shutdown).")
            self._stop_event.set()

    def _clear_local_shutdown_state(self):
        """Re-arm coordinator-level shutdown state on POWER_RESTORED.

        Mirrors UPSGroupMonitor._handle_on_line's per-group flag unlink
        for the coordinator-owned state. Invoked via the per-monitor
        ``power_restored_callback`` hook on the OB/FSD->OL transition.

        Without this, the in-memory ``_local_shutdown_initiated`` lock
        + the unsuffixed ``_global_shutdown_flag`` would persist after
        a no-op or sandboxed local shutdown, blocking the next outage's
        trigger across all UPS groups (multi-UPS path of bug #4).

        Idempotent: safe to call repeatedly when no shutdown was in
        flight, or when this group is the second OL transition in a row.

        The boolean reset AND the file unlink BOTH live inside the lock
        so the two halves of "shutdown is no longer in flight" stay
        atomic with respect to ``_handle_local_shutdown``. Without
        this, an interleaving where (A) we cleared the bool, (B) a
        concurrent ON_BATTERY thread re-took the lock and re-touched
        the flag, then (C) we unlinked it would leave the coordinator
        believing a sequence is in flight while the on-disk evidence
        is gone -- a state worse than the one we set out to fix.
        """
        with self._local_shutdown_lock:
            # M1: never re-arm while a local shutdown is committed and running
            # (its drain/flush/run_command execute outside the lock). Clearing
            # the guard mid-flight would let a concurrent trigger admit a SECOND
            # poweroff. But we must NOT simply drop this recovery: if it's the
            # only OB->OL transition (a no-op/non-halting shutdown), nothing else
            # would re-arm and the next outage would be blocked (cubic P1). So
            # remember it and let _handle_local_shutdown's finally re-arm once
            # the sequence has returned.
            if self._local_shutdown_in_flight:
                self._rearm_after_inflight = True
                return
            self._local_shutdown_initiated = False
            self._clear_global_shutdown_flag("power recovery")

    def _handle_local_shutdown(self, triggered_by: str):
        """Execute local shutdown with defense-in-depth protection."""
        # Defense layer 1: in-memory lock. Set BOTH the "initiated" guard and the
        # "in flight" flag atomically: in_flight tells _clear_local_shutdown_state
        # not to re-arm the guard mid-sequence (M1), which would otherwise let an
        # unrelated group's recovery clear the guard while the drain/flush/
        # run_command below run outside the lock and admit a SECOND poweroff.
        proceed = False
        with self._local_shutdown_lock:
            if not self._local_shutdown_initiated:
                self._local_shutdown_initiated = True
                self._local_shutdown_in_flight = True
                proceed = True

        if not proceed:
            return

        try:
            # Defense layer 2: filesystem flag
            try:
                self._global_shutdown_flag.touch()
            except OSError as exc:
                self._log(
                    f"⚠️  Could not write shutdown flag "
                    f"{self._global_shutdown_flag}: {exc}. Continuing "
                    "without the on-disk guard."
                )

            self._log(f"🚨  Local shutdown triggered by {triggered_by}")

            # Drain other groups if configured
            if self.config.local_shutdown.drain_on_local_shutdown:
                self._log("⏳  Draining all UPS groups before local shutdown...")
                self._drain_all_groups(timeout=120)

            # Execute local shutdown
            if self.config.local_shutdown.enabled:
                self._log("🔌  Shutting down local server NOW")
                if self.config.behavior.dry_run:
                    self._log(f"🧪  [DRY-RUN] Would execute: {self.config.local_shutdown.command}")
                    self._clear_global_shutdown_flag("dry-run local shutdown")
                else:
                    if self._notification_worker:
                        self._notification_worker.send(
                            "🛑  **Shutdown Sequence Complete**\nShutting down local server NOW.",
                            "failure",
                            category="shutdown_summary",
                        )
                        # Drain in flight before halt; lossless guarantee on
                        # what doesn't make it.
                        self._notification_worker.flush(timeout=5)
                    else:
                        time.sleep(5)
                    # Slice 3: tag this shutdown as power-loss-triggered so
                    # the next start can emit "📊  Recovered" and the Slice 4
                    # bonus folds the prev shutdown into a richer message.
                    # (Single-UPS path already does this in monitor.py;
                    # coordinator mode was missing it — caught in pre-push
                    # review.)
                    write_shutdown_marker(
                        Path(self.config.statistics.db_directory),
                        version=__version__,
                        reason=REASON_SEQUENCE_COMPLETE,
                    )
                    # Defense-in-depth: config validation already rejects an
                    # empty/None command at load, but a programmatically-built
                    # Config could still reach here. str()+strip guards against
                    # None.split() / run_command([]) silently no-op'ing the
                    # poweroff after peers were already drained.
                    cmd_parts = str(self.config.local_shutdown.command or "").split()
                    if not cmd_parts:
                        self._log(
                            "❌  local_shutdown.command is empty -- cannot power off "
                            "the host. Set local_shutdown.command to a valid command."
                        )
                    else:
                        if self.config.local_shutdown.message:
                            cmd_parts.append(self.config.local_shutdown.message)
                        run_command(cmd_parts)
                        # NOTE (H9): we deliberately do NOT eagerly re-arm the
                        # guard here. Re-entry during the SAME outage is already
                        # blocked by the monitor's suffixed _shutdown_flag_path
                        # (coordinator mode never clears it mid-sequence), and the
                        # guard is re-armed on the OB/FSD->OL recovery via
                        # power_restored_callback -> _clear_local_shutdown_state.
                        # Clearing it right after run_command would instead
                        # re-drain peers and re-send the "shutting down"
                        # notification on every subsequent failed poll in a
                        # non-halting/sandbox config (the multi-UPS analog of the
                        # H2 failsafe re-fire).
            else:
                self._log("✅  Local shutdown disabled. Group shutdown complete.")
                self._clear_global_shutdown_flag("disabled local shutdown")

            # Exit if --exit-after-shutdown was specified
            if self._exit_after_shutdown:
                self._log("🛑  Exiting after shutdown sequence (--exit-after-shutdown)")
                self._stop_event.set()
        finally:
            # The committed sequence is no longer running outside the lock, so
            # recovery is allowed to re-arm again. (On a real halt the process
            # never reaches here -- the host is already going down.) If a
            # recovery arrived DURING the in-flight window, apply the deferred
            # re-arm now so a no-op/non-halting shutdown doesn't stay latched and
            # block the next outage (cubic P1).
            with self._local_shutdown_lock:
                self._local_shutdown_in_flight = False
                if self._rearm_after_inflight:
                    self._rearm_after_inflight = False
                    self._local_shutdown_initiated = False
                    self._clear_global_shutdown_flag("deferred power recovery")

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
        self._log("⏳  Draining all UPS groups -- shutting down their resources...")

        # This runs ON one of the monitor threads (the group whose trigger
        # fired calls _handle_local_shutdown -> here, synchronously). That
        # thread's own Thread object is in self._threads, and joining the
        # CURRENT thread raises RuntimeError("cannot join current thread"),
        # which would unwind out of the whole shutdown sequence BEFORE the
        # host poweroff -- a missed local shutdown. So never join ourselves.
        me = threading.current_thread()

        # Phase 1: signal every peer monitor to stop its poll loop, then
        # join with a short window so the loops exit before we run their
        # shutdown sequences.
        self._stop_event.set()
        join_deadline = time.time() + max(1, timeout // 4)
        for thread in self._threads:
            if thread is me:
                continue
            remaining = max(0.0, join_deadline - time.time())
            thread.join(timeout=remaining)

        # Phase 2: run each monitor's shutdown sequence sequentially.
        for monitor in self._monitors:
            already_shutting_down = self._monitor_shutdown_guard_active(monitor)
            if not already_shutting_down:
                self._log(f"  ➡️  Triggering shutdown for {monitor._log_prefix.strip()}")
                try:
                    monitor._execute_shutdown_sequence()
                except Exception as e:
                    self._log(f"  ⚠️  Error during drain shutdown: {e}")

        # Final join window for any threads still wrapping up.
        deadline = time.time() + timeout
        for thread in self._threads:
            if thread is me:
                continue
            remaining = max(0.0, deadline - time.time())
            thread.join(timeout=remaining)
        still_running = [t for t in self._threads if t is not me and t.is_alive()]
        if still_running:
            self._log(f"⚠️  {len(still_running)} monitor(s) still running after drain timeout")

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

    def reload_config(self) -> dict:
        """Re-read config and apply the safe subset live to every group.

        Updates the coordinator's shared config and each per-group monitor's
        config in place. Returns a report (also used by the API endpoint).
        """
        from eneru.reload import perform_reload
        monitor_configs = [m.config for m in self._monitors]
        report = perform_reload(self.config, monitor_configs, self.config.config_path)
        if report.get("reloaded") and report.get("subsystems"):
            self._apply_subsystem_reload(report["subsystems"])
        self._log_reload_report(report)
        return report

    def _apply_subsystem_reload(self, subsystems: list) -> None:
        """Re-init live subsystems across the coordinator (best-effort)."""
        if "statistics" in subsystems:
            for mon in self._monitors:
                store = getattr(mon, "_stats_store", None)
                if store is not None:
                    try:
                        store.apply_reload(self.config.statistics)
                    except Exception as exc:  # pragma: no cover - defensive
                        self._log(f"⚠️  stats retention reload failed: {exc}")
        if "notifications" in subsystems:
            self._reload_notification_worker()
        if "remote_health" in subsystems:
            self._reload_remote_health()
        if "mqtt" in subsystems:
            self._reload_mqtt_publisher()

    def _reload_notification_worker(self) -> None:
        """Bounce the shared notification worker after config reload."""
        if self._notification_worker is not None:
            self._notification_worker.stop()
            self._notification_worker = None
        for mon in self._monitors:
            mon._notification_worker = None
        for executor in self._redundancy_executors.values():
            executor._notification_worker = None
        if not self.config.notifications.enabled:
            self._log("📢  Notifications: disabled")
            return
        if not APPRISE_AVAILABLE:
            self._log(
                "⚠️  WARNING: Notifications enabled but apprise not installed. "
                "Install with: uv pip install apprise"
            )
            return
        worker = NotificationWorker(self.config)
        if not worker.start():
            self._log("⚠️  WARNING: Failed to reload notifications")
            return
        for mon in self._monitors:
            mon._notification_worker = worker
            store = getattr(mon, "_stats_store", None)
            if store is not None and store._conn is not None:
                worker.register_store(store)
        for executor in self._redundancy_executors.values():
            executor._notification_worker = worker
        self._notification_worker = worker
        count = worker.get_service_count()
        self._log(f"📢  Notifications reloaded ({count} service(s))")

    def _reload_remote_health(self) -> None:
        """Bounce per-UPS and redundancy remote-health managers."""
        for mon in self._monitors:
            mon._reload_remote_health()
        for manager in self._redundancy_remote_health_managers:
            manager.stop()
        self._redundancy_remote_health_managers = []
        if self.config.redundancy_groups:
            monitors_by_name = {m.config.ups.name: m for m in self._monitors}
            for rg in self.config.redundancy_groups:
                self._start_redundancy_remote_health(rg, monitors_by_name)
        self._log("🔄  Remote health checks reloaded")

    def _reload_mqtt_publisher(self) -> None:
        """Bounce the coordinator MQTT publisher so broker/topic changes apply."""
        if self._mqtt_publisher is not None:
            self._mqtt_publisher.stop()
            self._mqtt_publisher = None
        self._start_mqtt_publisher()
        self._log("🔄  MQTT publisher reloaded")

    def record_control_event(self, ups_name: str, event_type: str,
                             detail: str) -> None:
        """Record an API control/reload action to the matching UPS's events
        table (v7.0 audit-log groundwork). Best-effort."""
        # ups_name is the resolved NUT name (from the API handler), matched
        # against each monitor's configured ups.name. If they ever diverge the
        # event still lands (fallback below), just under the first store.
        target = None
        for mon in self._monitors:
            groups = getattr(mon.config, "ups_groups", [])
            if groups and groups[0].ups.name == ups_name:
                target = mon
                break
        if target is None and self._monitors:
            target = self._monitors[0]  # reload / unknown UPS -> first store
        store = getattr(target, "_stats_store", None) if target else None
        if store is not None:
            try:
                store.log_event(event_type, detail)
            except Exception:  # pragma: no cover - defensive
                pass

    def delete_events(self, ups_name: str, items):
        """Delete events from the matching UPS's live store. Returns the count
        removed, or ``None`` when that UPS has no open store (→ API 503). Unlike
        the audit path there is no first-store fallback: a destructive op must
        never land on the wrong UPS's database."""
        for mon in self._monitors:
            groups = getattr(mon.config, "ups_groups", [])
            if groups and groups[0].ups.name == ups_name:
                store = getattr(mon, "_stats_store", None)
                if store is None or not store.is_open:
                    return None
                return store.delete_events(items)
        return None

    def _handle_sighup(self, signum, frame):
        """SIGHUP -> hot-reload config across all groups (never crashes on error)."""
        self._log("🔄  SIGHUP received — reloading configuration")
        try:
            self.reload_config()
        except Exception as exc:  # pragma: no cover - defensive
            self._log(f"⚠️  Config reload error (ignored): {exc}")

    def _log_reload_report(self, report: dict) -> None:
        from eneru.reload import format_report
        for line in format_report(report):
            self._log(line)

    def _shutdown_join_deadline(self) -> int:
        """Bounded wait (seconds) for an in-flight shutdown to finish on signal.

        Tied to the configured remote-shutdown + drain budgets so the host
        poweroff (the last step of the sequence) can complete, and capped so a
        wedged sequence can't block exit forever. (systemd's TimeoutStopSec is
        the ultimate governor; this just stops us abandoning our own work.)
        """
        def _server_budget(srv) -> int:
            # Full per-server wall time: pre-shutdown commands + the final
            # command + connect + safety margin (cubic -- pre-command runtime
            # was previously ignored, under-budgeting servers with pre-commands).
            try:
                pre = sum(
                    int(c.timeout) if c.timeout is not None
                    else int(srv.command_timeout)
                    for c in srv.pre_shutdown_commands
                )
                return (pre + int(srv.command_timeout) + int(srv.connect_timeout)
                        + int(srv.shutdown_safety_margin))
            except (TypeError, ValueError):
                return 0

        max_remote = 0
        # Include redundancy-group remotes too (cubic): a redundancy-group
        # shutdown is just as much "in flight" as a per-UPS one on signal.
        groups = list(self.config.ups_groups) + list(self.config.redundancy_groups)
        for group in groups:
            for srv in getattr(group, "remote_servers", []):
                max_remote = max(max_remote, _server_budget(srv))
        budget = max_remote + 120  # + local drain/poweroff headroom
        if self.config.local_shutdown.drain_on_local_shutdown:
            budget += 120
        return min(max(budget, 30), 600)

    def _handle_signal(self, signum: int, frame):
        """Handle SIGTERM/SIGINT for clean shutdown."""
        # L5: a second signal arriving during the join/teardown window must not
        # re-run the whole teardown (double notifications, redundant joins). The
        # first signal owns the exit; ignore the rest.
        if self._signal_handling:
            return
        self._signal_handling = True
        self._log("🛑  Service stopped by signal (SIGTERM/SIGINT). Monitoring is inactive.")

        self._stop_event.set()

        for manager in self._redundancy_remote_health_managers:
            manager.stop()
        if self._mqtt_publisher is not None:
            self._mqtt_publisher.stop()
        if self._api_server is not None:
            self._api_server.stop()

        # If a shutdown sequence is already in flight (a real power event, not
        # just `systemctl stop`), a monitor/evaluator thread is mid-sequence and
        # the host poweroff is its LAST step. Racing a 5 s join then sys.exit()
        # would kill that thread before the poweroff runs -- a missed shutdown.
        # When in flight, wait a generous, config-derived, bounded deadline so
        # the sequence can finish; otherwise keep the brisk 5 s exit.
        with self._local_shutdown_lock:
            local_shutdown_in_flight = self._local_shutdown_in_flight
        shutdown_in_flight = (
            local_shutdown_in_flight
            or self._global_shutdown_guard_active()
            or any(self._monitor_shutdown_guard_active(m) for m in self._monitors)
        )
        if shutdown_in_flight:
            join_budget = self._shutdown_join_deadline()
            self._log(
                f"⏳  Shutdown sequence in progress; waiting up to {join_budget}s "
                "for it to complete (host poweroff) before exit."
            )
        else:
            join_budget = 5

        # Deadline-based join so the TOTAL wait is bounded by join_budget, not
        # join_budget per thread. The signal handler runs on the main thread, so
        # none of these is the current thread (no self-join hazard).
        deadline = time.time() + join_budget
        for thread in (*self._threads, *self._evaluator_threads):
            remaining = max(0.0, deadline - time.time())
            thread.join(timeout=remaining)

        # v5.2.1: see UPSGroupMonitor._cleanup_and_exit for the full
        # rationale. Drain pending non-lifecycle rows first, then enqueue
        # the speculative lifecycle stop and stop the worker, then hand
        # off to the systemd-run timer (or eager fallback) for delivery.
        # Skip the enqueue entirely when an upgrade is in flight — the
        # next daemon's "📦  Upgraded" classification covers both ends.
        stats_dir = Path(self.config.statistics.db_directory)
        upgrade_in_progress = read_upgrade_marker(stats_dir) is not None

        body = "🛑  **Eneru Service Stopped**\nMonitoring is now inactive."
        notify_type = "warning"

        # Order matters: flush + stop the worker FIRST, then enqueue —
        # see UPSGroupMonitor._cleanup_and_exit for the rationale.
        # Capturing first_store BEFORE stop() because stop() doesn't
        # actually clear the registered stores list, but doing it here
        # is symmetric with how we'll use it for the deferred handoff.
        first_store = None
        if self._notification_worker is not None:
            with self._notification_worker._stores_lock:
                first_store = (self._notification_worker._stores[0]
                               if self._notification_worker._stores else None)
            self._notification_worker.flush(timeout=5)
            self._notification_worker.stop()

        notif_id = None
        if self._notification_worker and not upgrade_in_progress:
            notif_id = self._notification_worker.send(
                body=body,
                notify_type=notify_type,
                category="lifecycle",
            )

        # Schedule deferred delivery against the FIRST registered store
        # (which is what worker.send() above wrote to). The systemd-run
        # timer fires ~15 s after our exit; if a new coordinator's
        # `_cancel_prev_pending_lifecycle_rows` cancels the row first,
        # the timer is a no-op and the user sees a single Restarted.
        if not upgrade_in_progress:
            if notif_id is not None and first_store is not None:
                schedule_deferred_stop_or_eager_send(
                    notification_id=notif_id,
                    db_path=first_store.db_path,
                    config_path=getattr(self.config, "config_path", None),
                    body=body,
                    notify_type=notify_type,
                    worker=self._notification_worker,
                    log_fn=self._log,
                )
            elif self._notification_worker is not None:
                # CodeRabbit P1 (mirrored from monitor.py): no store
                # registered means worker.send() returned None and the
                # row never landed in SQLite. Ship eagerly via Apprise
                # so the lifecycle stop isn't silently lost.
                try:
                    self._notification_worker._send_via_apprise_bounded(
                        body, notify_type,
                    )
                except Exception:
                    pass

        # Per-monitor threads normally close their own StatsStore in their
        # cleanup path. Do it again here after notification drain/scheduling so a
        # stuck monitor thread cannot leave SQLite handles open at coordinator
        # exit.
        for monitor in self._monitors:
            try:
                stop_stats = getattr(monitor, "_stop_stats", None)
                if callable(stop_stats):
                    stop_stats()
                else:
                    store = getattr(monitor, "_stats_store", None)
                    if store is not None:
                        store.close()
            except Exception as exc:
                self._log(
                    "⚠️  Failed to close monitor stats during coordinator "
                    f"shutdown: {exc}"
                )

        # Slice 3: tag this exit so the next start can emit "🔄  Restarted"
        # if it comes back within RESTART_DOWNTIME_THRESHOLD_SECS, else
        # "🚀  Started (last seen Nh ago)". Coordinator mode was missing
        # this — caught in pre-push review.
        # BUT: don't downgrade an existing sequence_complete marker
        # (Cubic P2). If a power-loss shutdown sequence already wrote
        # it, the SIGTERM handler that fires when systemd shuts the
        # service down should preserve "we shut ourselves down for a
        # reason" so the next start emits "📊  Recovered" rather than
        # "🔄  Restarted".
        existing = read_shutdown_marker(stats_dir)
        if not (existing
                and existing.get("reason") == REASON_SEQUENCE_COMPLETE):
            write_shutdown_marker(
                stats_dir,
                version=__version__,
                reason=REASON_SIGNAL,
            )

        self._clear_global_shutdown_flag("signal shutdown")
        # 5.3.0 contract: clear redundancy executor flags too on
        # graceful exit so the next daemon instance starts from a
        # known-clean state. Defensive try-block: a flag-cleanup
        # failure must NOT block process exit. Log a breadcrumb so
        # operators have something to grep when a non-graceful exit
        # leaves a flag the next startup-cleanup then has to handle.
        for name, executor in self._redundancy_executors.items():
            try:
                executor.clear_shutdown_state()
            except Exception as e:
                self._log(
                    f"⚠️  Failed to clear redundancy flag for '{name}' "
                    f"during exit: {e}. Next startup will re-clear."
                )
        sys.exit(0)

    def _log(self, message: str):
        """Log a message using the shared logger."""
        if self._logger:
            self._logger.log(message)
        else:
            tz_name = time.strftime('%Z')
            timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            print(f"{timestamp} {tz_name} - {message}")
