"""Redundancy-group runtime: evaluator thread + shutdown executor.

The :class:`RedundancyGroupEvaluator` polls every member UPS's snapshot on
its own ~1s tick, applies the group's ``degraded_counts_as`` /
``unknown_counts_as`` policy, and asks the executor to fire when
``healthy_count < min_healthy``.

The :class:`RedundancyGroupExecutor` composes the four shutdown mixins
(``VMShutdownMixin``, ``ContainerShutdownMixin``, ``FilesystemShutdownMixin``,
``RemoteShutdownMixin``) so a redundancy group inherits multi-phase
ordering, ``shutdown_safety_margin``, and deadline-based join verbatim.

A separate filesystem flag at
``/var/run/ups-shutdown-redundancy-{sanitized-group-name}`` makes the
executor idempotent; the in-memory ``_lock`` + ``_shutdown_done`` pair
catches concurrent calls inside one process.
"""

import threading
import time
from pathlib import Path
from typing import Dict, Optional

from eneru.config import (
    Config,
    RedundancyGroupConfig,
    UPSGroupConfig,
)
from eneru.health_model import UPSHealth, assess_health
from eneru.logger import UPSLogger
from eneru.notifications import NotificationWorker
from eneru.shutdown.containers import ContainerShutdownMixin
from eneru.shutdown.filesystems import FilesystemShutdownMixin
from eneru.shutdown.remote import RemoteShutdownMixin
from eneru.shutdown.vms import VMShutdownMixin
from eneru.state import MonitorState


def _sanitize(name: str) -> str:
    """Sanitize a group name for filesystem flag-file names."""
    return (name or "unnamed").replace("@", "-").replace(":", "-").replace("/", "-")


class RedundancyGroupExecutor(
    VMShutdownMixin,
    ContainerShutdownMixin,
    FilesystemShutdownMixin,
    RemoteShutdownMixin,
):
    """Shutdown executor for one redundancy group.

    Composes all four shutdown mixins so the redundancy path reuses the
    *same* multi-phase ordering, ``shutdown_safety_margin``, and deadline
    semantics as the per-UPS shutdown path.
    """

    def __init__(
        self,
        group: RedundancyGroupConfig,
        *,
        base_config: Config,
        logger: Optional[UPSLogger] = None,
        log_prefix: str = "",
        stop_event: Optional[threading.Event] = None,
        notification_worker: Optional[NotificationWorker] = None,
    ):
        # Build a one-group Config so the shutdown mixins' lookups
        # (self.config.behavior, self.config.remote_servers,
        # self.config.virtual_machines, etc.) work without modification.
        synthetic_group = UPSGroupConfig(
            remote_servers=list(group.remote_servers),
            virtual_machines=group.virtual_machines,
            containers=group.containers,
            filesystems=group.filesystems,
            is_local=group.is_local,
        )
        self.config = Config(
            ups_groups=[synthetic_group],
            behavior=base_config.behavior,
            logging=base_config.logging,
            notifications=base_config.notifications,
            local_shutdown=base_config.local_shutdown,
        )

        self._group = group
        self.state = MonitorState()
        self.logger = logger
        self._log_prefix = log_prefix
        self._notification_worker = notification_worker
        self._stop_event = stop_event or threading.Event()
        # The mixins' own logging knows about coordinator mode; we run
        # under the coordinator so set this for symmetry.
        self._coordinator_mode = True

        # Per-group flag file -- mirrors the monitor's ``_shutdown_flag_path``
        # but lives in a separate namespace so the per-UPS and per-redundancy
        # paths never trample each other.
        sanitized = _sanitize(group.name)
        flag_dir = Path(base_config.logging.shutdown_flag_file).parent
        self._shutdown_flag_path = flag_dir / f"ups-shutdown-redundancy-{sanitized}"
        # _battery_history_path / _state_file_path are part of the documented
        # mixin contract but are unused inside the shutdown mixins -- set them
        # to safe per-group paths so anything that *did* reach for them stays
        # isolated from the per-UPS files.
        self._battery_history_path = Path(
            base_config.logging.battery_history_file + f".redundancy-{sanitized}"
        )
        self._state_file_path = Path(
            base_config.logging.state_file + f".redundancy-{sanitized}"
        )

        # Container-runtime detection (only relevant when is_local + containers
        # are enabled -- container shutdown is otherwise skipped).
        self._container_runtime: Optional[str] = None
        self._compose_available: bool = False
        if group.is_local and group.containers.enabled:
            try:
                self._container_runtime = self._detect_container_runtime()
                if self._container_runtime and group.containers.compose_files:
                    self._compose_available = self._check_compose_available()
            except Exception:
                self._container_runtime = None
                self._compose_available = False

        self._lock = threading.Lock()
        self._shutdown_done = False

    # ----- mixin contract: minimum logging + notification primitives -----

    def _log_message(self, message: str):
        prefixed = f"{self._log_prefix}{message}" if self._log_prefix else message
        if self.logger is not None:
            self.logger.log(prefixed)
        else:
            print(prefixed)

    def _send_notification(self, body: str, notify_type: str = "info",
                           blocking: bool = False):
        if not self._notification_worker:
            return
        prefixed = f"{self._log_prefix}{body}" if self._log_prefix else body
        # Match the monitor's @-escape so notifications can carry UPS@host
        # strings without triggering Discord mentions.
        escaped = prefixed.replace("@", "@\u200B")
        # Redundancy-group shutdowns are always "during shutdown" by definition,
        # so notifications are non-blocking to avoid network-stall delays.
        self._notification_worker.send(
            body=escaped, notify_type=notify_type, blocking=False,
        )

    def _log_power_event(self, event: str, details: str):
        # Some shutdown mixins call this. Forward to the standard log path so
        # it shows up alongside per-UPS power events.
        self._log_message(f"⚡ POWER EVENT: {event} - {details}")

    # ----- public API -----

    def shutdown(self, reason: str) -> bool:
        """Run the redundancy group's shutdown sequence (idempotent).

        Returns ``True`` if this call performed the shutdown,
        ``False`` if a previous call (or external flag file) already did.
        """
        with self._lock:
            if self._shutdown_done:
                return False
            if self._shutdown_flag_path.exists():
                self._shutdown_done = True
                return False
            self._shutdown_done = True
            self._shutdown_flag_path.touch()

        self._log_message(
            f"🚨 ========== REDUNDANCY GROUP SHUTDOWN: {self._group.name} =========="
        )
        self._log_message(f"   Reason: {reason}")
        self._send_notification(
            f"🚨 **Redundancy Group Shutdown:** {self._group.name}\n"
            f"Reason: {reason}\n"
            f"Sources: {', '.join(self._group.ups_sources) or '(none)'}",
            "failure",
        )

        if self.config.behavior.dry_run:
            self._log_message("🧪 *** DRY-RUN MODE: No actual shutdown will occur ***")

        try:
            if self._group.is_local:
                self._shutdown_vms()
                self._shutdown_containers()
                self._sync_filesystems()
                self._unmount_filesystems()
            self._shutdown_remote_servers()
            self._log_message(
                f"✅ ========== REDUNDANCY GROUP SHUTDOWN COMPLETE: "
                f"{self._group.name} =========="
            )
        except Exception as e:
            self._log_message(
                f"❌ Redundancy group '{self._group.name}' shutdown error: {e}"
            )
        finally:
            # In dry-run we clear the flag so repeated test runs aren't pinned.
            if self.config.behavior.dry_run:
                self._shutdown_flag_path.unlink(missing_ok=True)
                with self._lock:
                    self._shutdown_done = False

        return True


class RedundancyGroupEvaluator(threading.Thread):
    """Watches a redundancy group's member UPSes and triggers the executor.

    Reads each member's ``MonitorState.snapshot()`` under the state lock
    on every tick (default 1s), counts how many contribute as ``HEALTHY``
    after applying the group's policy, and calls
    :meth:`RedundancyGroupExecutor.shutdown` when ``healthy_count``
    drops below ``min_healthy``. Edge-detected logging keeps the log
    quiet during steady state.
    """

    def __init__(
        self,
        group: RedundancyGroupConfig,
        monitors_by_ups_name: Dict[str, object],
        executor: RedundancyGroupExecutor,
        *,
        stop_event: threading.Event,
        logger: Optional[UPSLogger] = None,
        log_prefix: str = "",
        tick: float = 1.0,
        startup_grace_seconds: Optional[float] = None,
    ):
        super().__init__(name=f"redundancy-{_sanitize(group.name)}", daemon=True)
        self._group = group
        self._monitors = monitors_by_ups_name
        self._executor = executor
        self._stop_event = stop_event
        self._logger = logger
        self._log_prefix = log_prefix
        self._tick = tick
        # Startup grace: delay the first evaluation so the per-UPS monitors
        # have time to take their initial poll and publish a snapshot.
        # Default = 5 * max(check_interval across members) + 5s, mirroring
        # the stale-snapshot rule. Without this, the evaluator's very first
        # tick would see every member as UNKNOWN (last_update_time == 0)
        # and -- with default unknown_counts_as=critical -- spuriously
        # fire the group shutdown immediately on startup.
        if startup_grace_seconds is None:
            check_intervals = []
            for m in monitors_by_ups_name.values():
                try:
                    check_intervals.append(int(m.config.ups.check_interval))
                except Exception:
                    pass
            base = max(check_intervals) if check_intervals else 1
            startup_grace_seconds = 5 * max(1, base) + 5
        self._startup_grace = float(startup_grace_seconds)
        # Edge-state tracking so we only emit lifecycle log lines on transitions.
        self._was_quorum_lost = False
        self._fired = False

    def _log(self, message: str):
        prefixed = f"{self._log_prefix}{message}" if self._log_prefix else message
        if self._logger is not None:
            self._logger.log(prefixed)
        else:
            print(prefixed)

    def _map_degraded(self) -> UPSHealth:
        if self._group.degraded_counts_as == "critical":
            return UPSHealth.CRITICAL
        return UPSHealth.HEALTHY

    def _map_unknown(self) -> UPSHealth:
        policy = self._group.unknown_counts_as
        if policy == "healthy":
            return UPSHealth.HEALTHY
        if policy == "degraded":
            return self._map_degraded()
        return UPSHealth.CRITICAL

    def _effective_health(self, raw: UPSHealth) -> UPSHealth:
        if raw == UPSHealth.UNKNOWN:
            return self._map_unknown()
        if raw == UPSHealth.DEGRADED:
            return self._map_degraded()
        return raw

    def evaluate_once(self):
        """Single evaluation pass -- exposed so tests can drive it directly."""
        healthy_count = 0
        per_ups: Dict[str, UPSHealth] = {}
        for ups_name in self._group.ups_sources:
            monitor = self._monitors.get(ups_name)
            if monitor is None:
                raw = UPSHealth.UNKNOWN
                check_interval = 1
                triggers = None
            else:
                snap = monitor.state.snapshot()
                check_interval = monitor.config.ups.check_interval
                triggers = monitor.config.triggers
                raw = assess_health(snap, triggers, check_interval)

            per_ups[ups_name] = raw
            if self._effective_health(raw) == UPSHealth.HEALTHY:
                healthy_count += 1

        quorum_lost = healthy_count < self._group.min_healthy

        if quorum_lost and not self._was_quorum_lost:
            tally = ", ".join(f"{name}={h.value}" for name, h in per_ups.items())
            self._log(
                f"🚨 Redundancy group '{self._group.name}' quorum LOST "
                f"(healthy={healthy_count}, min_healthy={self._group.min_healthy}; {tally})"
            )
        elif (not quorum_lost) and self._was_quorum_lost and not self._fired:
            tally = ", ".join(f"{name}={h.value}" for name, h in per_ups.items())
            self._log(
                f"✅ Redundancy group '{self._group.name}' quorum restored "
                f"(healthy={healthy_count}, min_healthy={self._group.min_healthy}; {tally})"
            )

        self._was_quorum_lost = quorum_lost

        if quorum_lost and not self._fired:
            reason = (
                f"redundancy quorum lost: healthy={healthy_count} < "
                f"min_healthy={self._group.min_healthy}"
            )
            fired = self._executor.shutdown(reason)
            if fired:
                self._fired = True

        return healthy_count, per_ups

    def run(self):
        self._log(
            f"🛡️ Redundancy group '{self._group.name}' evaluator started "
            f"({len(self._group.ups_sources)} sources, "
            f"min_healthy={self._group.min_healthy}, "
            f"startup_grace={self._startup_grace:.0f}s)"
        )
        # Startup grace: hold off the first evaluation so the per-UPS
        # monitor threads have time to publish their initial snapshots.
        if self._startup_grace > 0:
            self._stop_event.wait(self._startup_grace)
        try:
            while not self._stop_event.is_set():
                try:
                    self.evaluate_once()
                except Exception as e:
                    self._log(
                        f"❌ Redundancy evaluator '{self._group.name}' error: {e}"
                    )
                self._stop_event.wait(self._tick)
        finally:
            self._log(
                f"🛑 Redundancy group '{self._group.name}' evaluator stopped"
            )
