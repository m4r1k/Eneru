"""Remote SSH healthcheck runtime.

Healthchecks are advisory only. They use a dedicated harmless probe command
and never execute configured pre-shutdown or final shutdown commands.
"""

import json
import time
import threading
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from eneru.config import Config, RemoteServerConfig
from eneru.utils import run_command


REMOTE_HEALTH_DISABLED = "DISABLED"
REMOTE_HEALTH_UNKNOWN = "UNKNOWN"
REMOTE_HEALTH_CHECKING = "CHECKING"
REMOTE_HEALTH_HEALTHY = "HEALTHY"
REMOTE_HEALTH_DEGRADED = "DEGRADED"
REMOTE_HEALTH_FAILED = "FAILED"

DANGEROUS_PROBE_WORDS = (
    "shutdown", "poweroff", "reboot", "halt", "init 0",
    "systemctl poweroff", "systemctl reboot", "systemctl halt",
    "systemctl stop", "service stop",
    "docker stop", "docker compose stop", "docker-compose stop",
    "podman stop", "virsh shutdown", "virsh destroy",
    "qm shutdown", "qm stop", "pct shutdown", "pct stop",
)
# Reject any shell metacharacter that could chain a dangerous command
# after a benign-looking probe (e.g. ``true; shutdown -h now`` slips
# past the keyword list because the prefix is harmless). The probe runs
# inside ssh's remote shell, so command-line operators here apply
# remote-side. Newlines are rejected too — multi-line commands are
# almost certainly a misconfiguration in this context.
DANGEROUS_PROBE_CHARS = frozenset(";|&$`><()\n\r")
SSH_OPTIONS_WITH_SEPARATE_ARG = {
    "-B", "-b", "-c", "-D", "-E", "-e", "-F", "-I", "-i", "-J",
    "-L", "-l", "-m", "-O", "-o", "-p", "-Q", "-R", "-S", "-W", "-w",
}


@dataclass
class RemoteHealthStatus:
    """Latest healthcheck result for one configured remote server."""
    group: str
    server: str
    host: str
    user: str
    status: str = REMOTE_HEALTH_UNKNOWN
    last_checked_at: float = 0.0
    last_success_at: float = 0.0
    last_error: str = ""
    latency_ms: int = 0
    consecutive_failures: int = 0


def remote_health_sidecar_path(state_file_path: Path) -> Path:
    """Return the sidecar path paired with a daemon state file."""
    return state_file_path.with_name(state_file_path.name + ".remote-health.json")


def is_safe_probe_command(command: str) -> bool:
    """Reject obvious shutdown/control commands in remote health probes.

    Two-stage check:
      1. Reject shell metacharacters that could chain a dangerous
         command after a benign prefix.
      2. Reject any keyword from the dangerous-words list.
    """
    raw = (command or "").strip()
    if not raw:
        return False
    if any(ch in DANGEROUS_PROBE_CHARS for ch in raw):
        return False
    lowered = raw.lower()
    return not any(word in lowered for word in DANGEROUS_PROBE_WORDS)


def build_ssh_probe_command(server: RemoteServerConfig,
                            probe_command: str) -> List[str]:
    """Build an SSH argv for a remote health probe.

    Raises:
        ValueError: If ``server.ssh_options`` ends with a flag that
            requires a separate argument (e.g. trailing ``-i`` with no
            key path). Without this guard the ``-o ConnectTimeout=…``
            we append below would silently be consumed as the dangling
            flag's value.
    """
    ssh_cmd = ["ssh"]
    pending_arg = False
    for opt in server.ssh_options:
        if pending_arg:
            ssh_cmd.append(opt)
            pending_arg = False
        elif opt.startswith("-o "):
            ssh_cmd.extend(opt.split(None, 1))
        elif opt.startswith("-"):
            ssh_cmd.append(opt)
            pending_arg = opt in SSH_OPTIONS_WITH_SEPARATE_ARG
        else:
            ssh_cmd.extend(["-o", opt])
    if pending_arg:
        raise ValueError(
            f"remote server {server.name or server.host!r} has a dangling "
            f"SSH option in ssh_options ({ssh_cmd[-1]!r}); add the "
            f"argument as the next list entry"
        )
    ssh_cmd.extend([
        "-o", f"ConnectTimeout={server.connect_timeout}",
        "-o", "BatchMode=yes",
        f"{server.user}@{server.host}",
        probe_command,
    ])
    return ssh_cmd


def run_remote_probe(server: RemoteServerConfig,
                     probe_command: str) -> Tuple[bool, str, int]:
    """Run one harmless remote health probe.

    Returns ``(success, error, latency_ms)``.
    """
    start = time.monotonic()
    exit_code, _, stderr = run_command(
        build_ssh_probe_command(server, probe_command),
        timeout=server.connect_timeout + 10,
    )
    latency_ms = int((time.monotonic() - start) * 1000)
    if exit_code == 0:
        return True, "", latency_ms
    if exit_code == 124:
        return False, f"timed out after {server.connect_timeout}s", latency_ms
    return False, stderr.strip() or f"exit code {exit_code}", latency_ms


class RemoteHealthManager:
    """Background healthcheck loop for one UPS/redundancy group."""

    def __init__(
        self,
        *,
        config: Config,
        group_label: str,
        servers: List[RemoteServerConfig],
        sidecar_path: Path,
        stop_event: threading.Event,
        log_fn: Callable[[str], None],
        notify_fn: Optional[Callable[[str, str], None]] = None,
        event_fn: Optional[Callable[[str, str, bool], None]] = None,
    ):
        self.config = config
        self.group_label = group_label
        self.servers = [s for s in servers if s.enabled]
        self.sidecar_path = Path(sidecar_path)
        self.stop_event = stop_event
        self.log_fn = log_fn
        self.notify_fn = notify_fn
        self.event_fn = event_fn
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._notified_failed: Dict[str, bool] = {}
        self._event_fn_logged_failure = False
        probe = config.remote_health.probe_command
        self._validated_probe_command = probe if is_safe_probe_command(probe) else None
        self._sidecar_write_failed_paths = set()
        initial = REMOTE_HEALTH_UNKNOWN if config.remote_health.enabled else REMOTE_HEALTH_DISABLED
        self._statuses: Dict[str, RemoteHealthStatus] = {
            self._key(server): RemoteHealthStatus(
                group=group_label,
                server=server.name or server.host,
                host=server.host,
                user=server.user,
                status=initial,
            )
            for server in self.servers
        }

    def start(self) -> None:
        """Start the background healthcheck loop if configured."""
        if (not self.config.remote_health.enabled) or not self.servers:
            self._write_sidecar()
            return
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run_loop,
            name=f"remote-health-{self.group_label}",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: int = 5) -> None:
        """Signal the healthcheck loop and wait briefly for it to exit."""
        self.stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            if not self._thread.is_alive():
                self._thread = None

    def snapshot(self) -> List[dict]:
        """Return JSON-serializable health rows."""
        with self._lock:
            return [asdict(row) for row in self._statuses.values()]

    def check_once(self) -> List[dict]:
        """Run one healthcheck cycle synchronously."""
        if not self.config.remote_health.enabled:
            self._write_sidecar()
            return self.snapshot()
        for server in self.servers:
            if self.stop_event.is_set():
                break
            self._check_server(server)
        self._write_sidecar()
        return self.snapshot()

    def _run_loop(self) -> None:
        if self.config.remote_health.startup_check:
            self.check_once()
        interval = max(60, int(self.config.remote_health.interval))
        while not self.stop_event.wait(interval):
            self.check_once()

    def _check_server(self, server: RemoteServerConfig) -> None:
        key = self._key(server)
        with self._lock:
            row = self._statuses[key]
            previous = row.status
            row.status = REMOTE_HEALTH_CHECKING
        self._write_sidecar()

        probe = self._validated_probe_command
        if probe is None:
            success, error, latency_ms = False, "unsafe probe command rejected", 0
        else:
            success, error, latency_ms = run_remote_probe(server, probe)

        now = time.time()
        with self._lock:
            row = self._statuses[key]
            row.last_checked_at = now
            row.latency_ms = latency_ms
            if success:
                row.status = REMOTE_HEALTH_HEALTHY
                row.last_success_at = now
                row.last_error = ""
                row.consecutive_failures = 0
            else:
                row.consecutive_failures += 1
                row.last_error = error
                threshold = max(1, int(self.config.remote_health.failure_threshold))
                row.status = (
                    REMOTE_HEALTH_FAILED
                    if row.consecutive_failures >= threshold
                    else REMOTE_HEALTH_DEGRADED
                )
            current = row.status

        display = server.name or server.host
        notification_sent = False
        if success and previous in (REMOTE_HEALTH_DEGRADED, REMOTE_HEALTH_FAILED):
            self._notified_failed[key] = False
            self.log_fn(f"✅ Remote health recovered: {display}")
            if (
                previous == REMOTE_HEALTH_FAILED
                and self.config.remote_health.notify_on_recovery
                and self.notify_fn
            ):
                self.notify_fn(
                    f"✅ **Remote SSH Health Recovered:** {display}\nHost: {server.host}",
                    self.config.NOTIFY_SUCCESS,
                )
                notification_sent = True
        elif current == REMOTE_HEALTH_FAILED and not self._notified_failed.get(key):
            self._notified_failed[key] = True
            self.log_fn(f"❌ Remote health failed: {display}: {error}")
            if self.config.remote_health.notify_on_failure and self.notify_fn:
                self.notify_fn(
                    f"❌ **Remote SSH Health Failed:** {display}\n"
                    f"Host: {server.host}\nError: {error}",
                    self.config.NOTIFY_FAILURE,
                )
                notification_sent = True
        elif not success:
            self.log_fn(f"⚠️ Remote health degraded: {display}: {error}")
        self._record_status_transition(
            previous, current, display, server.host, row.last_error,
            notification_sent,
        )

    def _record_status_transition(
        self,
        previous: str,
        current: str,
        display: str,
        host: str,
        error: str,
        notification_sent: bool,
    ) -> None:
        """Record stable remote-health state transitions in SQLite events."""
        if self.event_fn is None or current == previous:
            return
        # UNKNOWN -> HEALTHY is the initial baseline after daemon start,
        # not an operator-actionable state change. Keep startup failures
        # and later recoveries visible, but do not add a healthy row next
        # to every DAEMON_START event.
        if previous == REMOTE_HEALTH_UNKNOWN and current == REMOTE_HEALTH_HEALTHY:
            return
        detail = (
            f"{self.group_label}/{display} ({host}) "
            f"{previous} -> {current}"
        )
        if error:
            detail = f"{detail}: {error}"
        try:
            self.event_fn(
                f"REMOTE_HEALTH_{current}",
                detail,
                notification_sent,
            )
        except Exception as exc:
            # Stats are diagnostic only. A broken DB must not affect
            # health checks or the shutdown path. Log the first failure
            # so a silently broken events table leaves a journal trail.
            if not self._event_fn_logged_failure:
                self._event_fn_logged_failure = True
                self.log_fn(
                    f"⚠️ Remote health stats event failed (further "
                    f"failures will be silent): {exc}"
                )

    def _write_sidecar(self) -> None:
        try:
            payload = {
                "group": self.group_label,
                "generated_at": time.time(),
                "servers": self.snapshot(),
            }
            self.sidecar_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.sidecar_path.with_name(self.sidecar_path.name + ".tmp")
            tmp.write_text(json.dumps(payload, sort_keys=True))
            tmp.replace(self.sidecar_path)
        except Exception as exc:
            key = str(self.sidecar_path)
            if key not in self._sidecar_write_failed_paths:
                self._sidecar_write_failed_paths.add(key)
                self.log_fn(
                    f"⚠️ Failed to write remote health sidecar for "
                    f"{self.group_label} at {self.sidecar_path}: {exc}"
                )

    @staticmethod
    def _key(server: RemoteServerConfig) -> str:
        return f"{server.user}@{server.host}:{server.name or server.host}"


def read_remote_health_sidecar(path: Path) -> List[dict]:
    """Read remote-health sidecar rows, returning an empty list on failure."""
    try:
        data = json.loads(Path(path).read_text())
        rows = data.get("servers", [])
        return rows if isinstance(rows, list) else []
    except Exception:
        return []
