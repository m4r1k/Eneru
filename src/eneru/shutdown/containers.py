"""Container runtime detection and Docker/Podman shutdown phase.

Owns runtime detection (``self._container_runtime``) and the multi-step
container shutdown: compose stacks first, then remaining containers, plus
optional rootless Podman containers per non-system user.
"""

from pathlib import Path
import socket
from typing import Optional, Set

from eneru.utils import command_exists, run_command


class ContainerShutdownMixin:
    """Mixin: container runtime detection + shutdown for Docker/Podman."""

    def _current_container_ids(self) -> Set[str]:
        """Best-effort IDs for the container currently running Eneru.

        Three signals, walked in order:

        1. ``socket.gethostname()`` — Docker and Podman default the
           container hostname to the short container ID. Fails when the
           user passes ``--hostname`` or sets ``container_name`` in
           Compose.
        2. ``/proc/self/cgroup`` and ``/proc/1/cgroup`` — works on
           cgroup v1 and on cgroup v2 hosts that don't enable cgroup
           namespaces. Returns ``0::/`` (and therefore no token) on
           modern Docker (>= 20.10 with cgroupns=private, the default
           for cgroup v2 hosts).
        3. ``/proc/self/mountinfo`` — Docker bind-mounts ``/etc/hostname``,
           ``/etc/resolv.conf``, and ``/etc/hosts`` from
           ``/var/lib/docker/containers/<full-id>/...``; Podman uses a
           similar convention under ``/var/lib/containers/...``. Those
           source paths survive cgroupns and are the most reliable
           fallback when cgroup tokens go away.
        """
        ids: Set[str] = set()

        hostname = socket.gethostname().strip()
        if _looks_like_container_id(hostname):
            ids.add(hostname)

        for path in (Path("/proc/self/cgroup"), Path("/proc/1/cgroup")):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            for token in _container_id_tokens(text):
                ids.add(token)

        try:
            mountinfo = Path("/proc/self/mountinfo").read_text(encoding="utf-8")
        except OSError:
            mountinfo = ""
        for token in _container_ids_from_mountinfo(mountinfo):
            ids.add(token)

        return ids

    def _is_current_container(self, container_id: str, current_ids: Set[str]) -> bool:
        """Return True when a runtime ID appears to identify this process."""
        cid = container_id.strip()
        if not cid:
            return False
        for current in current_ids:
            if cid.startswith(current) or current.startswith(cid):
                return True
        return False

    def _compose_stack_contains_self(self, file_path: str) -> bool:
        """Return True when a compose file appears to include Eneru itself."""
        current_ids = self._current_container_ids()
        if not current_ids:
            return False

        runtime = self._container_runtime
        exit_code, stdout, _ = run_command(
            [runtime, "compose", "-f", file_path, "ps", "-q"],
            timeout=10,
        )
        if exit_code != 0:
            return False

        for cid in stdout.strip().splitlines():
            if self._is_current_container(cid, current_ids):
                return True
        return False

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

            if self._compose_stack_contains_self(file_path):
                self._log_message(
                    f"  ⚠️ {file_path} includes the Eneru container; "
                    "skipping compose down to avoid stopping this daemon."
                )
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

        current_ids = self._current_container_ids()
        container_ids = []
        skipped_self = []
        for cid in stdout.strip().split('\n'):
            cid = cid.strip()
            if not cid:
                continue
            if self._is_current_container(cid, current_ids):
                skipped_self.append(cid)
                continue
            container_ids.append(cid)

        if skipped_self:
            self._log_message(
                "  ℹ️ Skipping Eneru's own container during container shutdown"
            )

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


def _looks_like_container_id(value: str) -> bool:
    """Return True for common short/full OCI container ID tokens."""
    if len(value) < 12 or len(value) > 64:
        return False
    return all(ch in "0123456789abcdef" for ch in value.lower())


def _container_id_tokens(text: str) -> Set[str]:
    """Extract likely OCI container IDs from cgroup file content."""
    tokens: Set[str] = set()
    for raw in text.replace(":", "/").split("/"):
        token = raw.strip()
        if token.startswith("docker-") and token.endswith(".scope"):
            token = token[len("docker-"):-len(".scope")]
        if token.startswith("cri-containerd-") and token.endswith(".scope"):
            token = token[len("cri-containerd-"):-len(".scope")]
        if _looks_like_container_id(token):
            tokens.add(token)
    return tokens


_MOUNTINFO_CONTAINER_PATH_MARKERS = (
    "/docker/containers/",
    "/var/lib/docker/containers/",
    "/var/lib/containers/storage/overlay-containers/",
    "/containers/storage/overlay-containers/",
    "/var/lib/containerd/",
    "/containerd/",
)


def _container_ids_from_mountinfo(text: str) -> Set[str]:
    """Extract container IDs from /proc/self/mountinfo source paths.

    Docker bind-mounts /etc/hostname, /etc/resolv.conf, /etc/hosts from
    /var/lib/docker/containers/<full-id>/<file>; Podman uses
    /var/lib/containers/storage/overlay-containers/<full-id>/. Those source
    paths appear in the mountinfo even when /proc/self/cgroup has been
    flattened by cgroup namespaces.

    Restricted to known container-runtime path prefixes so unrelated
    hex-shaped tokens elsewhere in mountinfo (overlay layer hashes, etc.)
    don't pollute the result set.
    """
    tokens: Set[str] = set()
    for line in text.splitlines():
        for marker in _MOUNTINFO_CONTAINER_PATH_MARKERS:
            idx = line.find(marker)
            if idx == -1:
                continue
            tail = line[idx + len(marker):]
            # The container ID is the next path component.
            candidate = tail.split("/", 1)[0].split()[0]
            if _looks_like_container_id(candidate):
                tokens.add(candidate)
    return tokens
