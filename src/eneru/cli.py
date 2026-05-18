"""CLI entry point for Eneru."""

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from eneru.version import __version__
from eneru.config import Config, ConfigLoader, UPSConfig, UPSGroupConfig
from eneru.monitor import UPSGroupMonitor, compute_effective_order
from eneru.multi_ups import MultiUPSCoordinator
from eneru.notifications import APPRISE_AVAILABLE
from eneru.redundancy import RedundancyGroupExecutor
from eneru.remote_health import is_safe_probe_command, run_remote_probe

# Optional import for Apprise (needed for test notifications)
try:
    import apprise
except ImportError:
    apprise = None


class ConfigValidationLoadError(Exception):
    """Raised when raw YAML cannot be loaded for startup validation."""


def _non_negative_int(value: str) -> int:
    """argparse type for ``--length``: int >= 0 (0 = no cap)."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(
            f"--length must be a non-negative integer, got {value!r}"
        )
    if n < 0:
        raise argparse.ArgumentTypeError(
            f"--length must be >= 0 (0 = no cap), got {n}"
        )
    return n


def _port_int(value: str) -> int:
    """argparse type for TCP port arguments: integer in 1..65535."""
    try:
        port = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(
            f"port must be an integer, got {value!r}"
        )
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError(
            f"port must be in 1..65535, got {port}"
        )
    return port


def _load_config(args):
    """Load configuration from the --config path."""
    return ConfigLoader.load(getattr(args, 'config', None))


def _apply_run_overrides(config: Config, args: argparse.Namespace) -> None:
    """Apply `eneru run` CLI overrides after YAML load, before validation."""
    if args.dry_run:
        config.behavior.dry_run = True

    if getattr(args, "api", False):
        config.api.enabled = True
    if getattr(args, "api_bind", None) is not None:
        config.api.enabled = True
        config.api.bind = args.api_bind
    if getattr(args, "api_port", None) is not None:
        config.api.enabled = True
        config.api.port = args.api_port


def _root_required_reasons(config: Config) -> list[str]:
    """Return local-host features that require root at daemon startup."""
    reasons: list[str] = []
    groups = list(config.ups_groups)
    if not groups:
        reasons.append("implicit single-UPS local-host mode")
    for group in groups:
        label = group.ups.label
        if group.is_local:
            reasons.append(f"UPS group '{label}' is marked is_local")
        if group.virtual_machines.enabled:
            reasons.append(f"UPS group '{label}' has virtual_machines enabled")
        if group.containers.enabled:
            reasons.append(f"UPS group '{label}' has containers enabled")
        if group.filesystems.unmount.enabled:
            reasons.append(f"UPS group '{label}' has filesystem unmount enabled")

    for group in config.redundancy_groups:
        label = group.name or "(unnamed)"
        if group.is_local:
            reasons.append(f"redundancy group '{label}' is marked is_local")
        if group.virtual_machines.enabled:
            reasons.append(f"redundancy group '{label}' has virtual_machines enabled")
        if group.containers.enabled:
            reasons.append(f"redundancy group '{label}' has containers enabled")
        if group.filesystems.unmount.enabled:
            reasons.append(f"redundancy group '{label}' has filesystem unmount enabled")

    has_local_owner = any(g.is_local for g in groups) or any(
        g.is_local for g in config.redundancy_groups
    )
    if config.local_shutdown.enabled and (
        has_local_owner or not groups or config.local_shutdown.trigger_on == "any"
    ):
        reasons.append("local_shutdown can power off the Eneru host")

    return sorted(set(reasons))


def _detect_kubernetes() -> bool:
    """Return True when this process is running inside a Kubernetes pod.

    Three independent signals; any one is sufficient. The env var alone is
    enough for ~all real deployments (kubelet injects it for every pod by
    default across vanilla K8s, k3s, OpenShift, EKS/GKE/AKS), but the SA
    token mount and cgroup checks catch hardened pods that explicitly
    unset env vars.
    """
    if os.environ.get("KUBERNETES_SERVICE_HOST"):
        return True
    if Path("/var/run/secrets/kubernetes.io/serviceaccount/token").exists():
        return True
    try:
        cgroup_text = Path("/proc/1/cgroup").read_text(encoding="utf-8")
    except OSError:
        cgroup_text = ""
    if "kubepods" in cgroup_text:
        return True
    return False


def _detect_runtime_context() -> str:
    """Best-effort runtime-context label for the current process.

    Returns one of:
      - ``"container (Kubernetes)"`` when running inside a K8s pod. Evaluated
        FIRST so K8s wins over generic Docker/Podman even when ``/.dockerenv``
        also exists (some CNI plugins / sidecars create it).
      - ``"container (Docker)"`` when ``/.dockerenv`` exists.
      - ``"container (Podman)"`` when ``/run/.containerenv`` exists.
      - ``"container"`` for other OCI runtimes (lxc, systemd-nspawn, etc.)
        detected via the ``container`` env var or container paths in
        ``/proc/1/cgroup`` or ``/proc/self/mountinfo``.
      - ``"systemd service"`` when running under a systemd unit
        (``INVOCATION_ID`` env var) and not in a container.
      - ``"bare process"`` otherwise.

    Container detection takes precedence: a systemd unit running inside
    a container is reported as a container, since that is the user-visible
    fact when troubleshooting. K8s detection takes precedence over generic
    container branches because the v5.5 three-profile framing treats K8s
    as a distinct deployment profile (remote-only by recommendation;
    local-host ownership is not a fit).
    """
    if _detect_kubernetes():
        return "container (Kubernetes)"

    if Path("/.dockerenv").exists():
        return "container (Docker)"
    if Path("/run/.containerenv").exists():
        return "container (Podman)"

    container_env = os.environ.get("container", "").strip().lower()
    if container_env:
        # Normalize known runtime names so the output matches the
        # /.dockerenv and /run/.containerenv branches above.
        pretty = {"docker": "Docker", "podman": "Podman"}.get(container_env, container_env)
        return f"container ({pretty})"

    try:
        cgroup_text = Path("/proc/1/cgroup").read_text(encoding="utf-8")
    except OSError:
        cgroup_text = ""
    if any(marker in cgroup_text for marker in ("docker", "kubepods", "containerd", "lxc")):
        return "container"

    try:
        mountinfo = Path("/proc/self/mountinfo").read_text(encoding="utf-8")
    except OSError:
        mountinfo = ""
    if "/docker/containers/" in mountinfo or "/containers/storage/overlay-containers/" in mountinfo:
        return "container"

    if os.environ.get("INVOCATION_ID"):
        return "systemd service"

    try:
        comm = Path("/proc/1/comm").read_text(encoding="utf-8").strip()
    except OSError:
        comm = ""
    if comm == "systemd" and os.environ.get("JOURNAL_STREAM"):
        return "systemd service"

    return "bare process"


def _is_container_runtime(label: str) -> bool:
    """True for any container-runtime label returned by _detect_runtime_context."""
    return label.startswith("container")


def _is_kubernetes_runtime(label: str) -> bool:
    """True only for the Kubernetes-specific container label."""
    return label == "container (Kubernetes)"


def _find_host_loopback(config: Config):
    """Return the (group_label, server) pair flagged is_host_loopback, or None.

    Config validation already enforces at-most-one across the whole config,
    so the first enabled match is authoritative. Disabled loopback entries
    are explicit non-contracts: they should not make delegation, readiness,
    or status paths look usable.

    Note: ``Config.remote_servers`` is a property that returns the first
    UPS group's remote_servers — there is no separate top-level list,
    so the loop over ``config.ups_groups`` already covers single-UPS
    legacy configs and any synthesized entries that land on the first
    group via ``_synthesize_loopback_if_needed``'s owner-attach path.
    """
    for group in config.ups_groups:
        for server in group.remote_servers:
            if server.enabled and server.is_host_loopback is True:
                return group.ups.label, server
    for group in config.redundancy_groups:
        for server in group.remote_servers:
            if server.enabled and server.is_host_loopback is True:
                return group.name or "(unnamed)", server
    return None


def _has_explicit_loopback_opt_out(config: Config) -> bool:
    """Return True if any YAML entry explicitly set is_host_loopback: false."""
    for group in config.ups_groups:
        for server in group.remote_servers:
            if (
                getattr(server, "_is_host_loopback_explicit", False)
                and server.is_host_loopback is False
            ):
                return True
    for group in config.redundancy_groups:
        for server in group.remote_servers:
            if (
                getattr(server, "_is_host_loopback_explicit", False)
                and server.is_host_loopback is False
            ):
                return True
    return False


def _uses_loopback_delegate(config: Config, group=None) -> bool:
    """Shared loopback-delegation predicate for monitor and redundancy code."""
    runtime = _detect_runtime_context()
    if not _is_container_runtime(runtime):
        return False
    if group is None:
        group = _local_owner_group(config)
    if group is None or not getattr(group, "is_local", False):
        return False
    return any(
        s.enabled and s.is_host_loopback is True
        for s in getattr(group, "remote_servers", [])
    )


def _local_owner_group(config: Config):
    """Return the group (UPS or redundancy) flagged is_local, or None.

    Single-UPS legacy mode is always is_local=True via _parse_legacy_ups,
    so this finds the implicit owner too.
    """
    for group in config.ups_groups:
        if group.is_local:
            return group
    for group in config.redundancy_groups:
        if group.is_local:
            return group
    if not config.ups_groups and not config.redundancy_groups:
        return None  # the "implicit single-UPS local-host" mode
    return None


def _local_capabilities_required(config: Config) -> bool:
    """True iff the config declares any local action that needs a loopback.

    Mirrors _root_required_reasons() but boolean — VMs, containers,
    filesystem unmount, or local_shutdown effectively configured.
    """
    return bool(_root_required_reasons(config))


# v5.5: synthesized loopback defaults (auto-enabled for Docker/Podman + local
# capabilities + no explicit entry). The SSH key path is the documented
# container convention; the user mounts it as a read-only volume.
_LOOPBACK_DEFAULT_SSH_KEY_PATH = "/var/lib/eneru/ssh/id_loopback"


def _synthesize_loopback_if_needed(
    config: Config, *, strict_key_check: bool = True,
) -> None:
    """Inject a default is_host_loopback entry for the zero-config homelab case.

    Conditions to synthesize: runtime is Docker/Podman (NOT Kubernetes — that
    profile is remote-only by recommendation) AND local capabilities are
    configured AND no existing remote_servers entry is already flagged.

    The synthesized entry uses 127.0.0.1 + root + shutdown -h now, with the
    documented default SSH key path.

    ``strict_key_check``:
    * True (``run``) — missing default SSH key is a fatal error; the
      daemon can't honor the contract without it.
    * False (``validate``, ``shutdown group --dry-run``) — missing key
      becomes a WARNING and synthesis proceeds with the default
      ssh_key_path. The user is diagnosing config or rehearsing; the
      key may legitimately not exist yet.
    """
    runtime = _detect_runtime_context()
    if not _is_container_runtime(runtime):
        return
    if _is_kubernetes_runtime(runtime):
        # Kubernetes is the remote-only profile per the v5.5 three-profile
        # framing; never auto-enable local-host delegation there.
        return
    if not _local_capabilities_required(config):
        return
    if _find_host_loopback(config) is not None:
        return
    if _has_explicit_loopback_opt_out(config):
        return

    # Pick the synthesis target — prefer the explicit local owner. In legacy
    # single-UPS mode `_parse_legacy_ups` already created an is_local group;
    # in implicit-no-groups mode we have nothing to attach to, so we error.
    owner = _local_owner_group(config)
    if owner is None and config.ups_groups:
        # No owner flagged but groups exist — defensive; should be a
        # validation error already.
        return

    # Use stat() instead of exists(): Python 3.14 Path.exists() suppresses
    # OSError subclasses, but PermissionError is the operator-actionable case
    # we need to distinguish from a genuinely missing key.
    try:
        Path(_LOOPBACK_DEFAULT_SSH_KEY_PATH).stat()
        key_present = True
        permission_error = False
    except OSError as exc:
        key_present = False
        permission_error = isinstance(exc, PermissionError)

    if not key_present:
        level = "ERROR" if strict_key_check else "WARNING"
        if permission_error:
            print(
                f"{level}: Eneru detected runtime '{runtime}' with local "
                "capabilities but the default SSH key for the host-loopback "
                "delegate is not readable by the container user:",
                file=sys.stderr,
            )
            print(
                f"  expected at: {_LOOPBACK_DEFAULT_SSH_KEY_PATH}",
                file=sys.stderr,
            )
            print(
                "  cause: PermissionError — the parent directory inside the "
                "container is not readable by uid 10001 (eneru). This is "
                "common when bind-mounting host paths like /root/.ssh/ "
                "(mode 0700) directly. Fixes:\n"
                "  1. Mount a dedicated directory with mode 0755 + the key "
                "file mode 0400 or 0600 owned by uid 10001, or grant uid "
                "10001 read access with an ACL.\n"
                "  2. Run the container as root with `--user 0:0` (works "
                "but defeats the non-root design).\n"
                "  3. Configure a remote_servers entry with an explicit "
                "ssh_key_path pointing at a readable location.\n"
                "See docs/containers-kubernetes.md for the recommended "
                "walkthrough using a dedicated /srv/eneru/ssh/ directory.",
                file=sys.stderr,
            )
        else:
            print(
                f"{level}: Eneru detected runtime '{runtime}' with local "
                "capabilities but the default SSH key for the host-loopback "
                "delegate is missing:",
                file=sys.stderr,
            )
            print(
                f"  expected at: {_LOOPBACK_DEFAULT_SSH_KEY_PATH}",
                file=sys.stderr,
            )
            print(
                "Options:\n"
                f"  1. Generate the key and bind-mount it read-only "
                f"({_LOOPBACK_DEFAULT_SSH_KEY_PATH}).\n"
                "  2. Configure a remote_servers entry explicitly with "
                "is_host_loopback: true and a custom ssh_key_path.\n"
                "  3. Switch to the deb/rpm install if you want native host "
                "ownership without SSH delegation.\n"
                "See docs/containers-kubernetes.md for the walkthrough.",
                file=sys.stderr,
            )
        if strict_key_check:
            sys.exit(1)
        # Non-strict: synthesize anyway so validate/dry-run can show the
        # delegated sequence. Real shutdown would fail at SSH time, but
        # the user is diagnosing config, not running production.

    from eneru.config import RemoteServerConfig
    synthesized = RemoteServerConfig(
        name="host-loopback",
        enabled=True,
        host="127.0.0.1",
        user="root",
        shutdown_command="shutdown -h now",
        ssh_key_path=_LOOPBACK_DEFAULT_SSH_KEY_PATH,
        # Eneru runs as uid 10001 inside the container with no
        # ~/.ssh/known_hosts. The MITM surface on 127.0.0.1 is zero
        # (sshd is the same kernel namespace), so skip strict host-key
        # checking — otherwise the first probe fails with
        # "Host key verification failed" and /ready stays 503.
        ssh_options=[
            "StrictHostKeyChecking=no",
            "UserKnownHostsFile=/dev/null",
        ],
        is_host_loopback=True,
        # Highest shutdown_order so the host poweroff runs LAST — anything
        # earlier would be killed by the loopback's shutdown_command.
        shutdown_order=999,
    )
    if owner is not None:
        owner.remote_servers.append(synthesized)
    else:
        # Implicit single-UPS mode with no groups — attach to top-level.
        config.remote_servers.append(synthesized)

    print(
        f"v5.5: auto-enabled host-loopback delegate (127.0.0.1, root, "
        f"key={_LOOPBACK_DEFAULT_SSH_KEY_PATH}) for {runtime} with local "
        "capabilities. Configure a remote_servers entry explicitly to override.",
        file=sys.stderr,
    )


def _exit_on_missing_loopback_contract(config: Config) -> None:
    """Fail Docker/Podman local-host ownership when no enabled loopback exists."""
    runtime = _detect_runtime_context()
    if not _is_container_runtime(runtime) or _is_kubernetes_runtime(runtime):
        return
    if not _local_capabilities_required(config):
        return
    if _find_host_loopback(config) is not None:
        return
    print(
        f"ERROR: Eneru detected runtime '{runtime}' with local-host "
        "capabilities but no enabled is_host_loopback delegate is configured.",
        file=sys.stderr,
    )
    print(
        "Docker/Podman local-host ownership must delegate host actions "
        "through a healthy loopback SSH target. Configure "
        "remote_servers[].is_host_loopback: true, or remove the local "
        "capabilities from this container config.",
        file=sys.stderr,
    )
    sys.exit(1)


def _inject_delegated_actions(config: Config) -> None:
    """Generate the loopback's pre_shutdown_commands from the local config.

    When a loopback delegate is configured, the operator does NOT write
    pre_shutdown_commands themselves — Eneru translates the already-declared
    local phases (VMs, containers, sync, etc.) into REMOTE_ACTIONS templates
    and prepends them to the loopback entry. The host's sshd then executes
    them in the same order the in-process path would have used.

    Idempotent within a single process: call once at startup, after
    ``_synthesize_loopback_if_needed`` and before validation. Any user
    pre_shutdown_commands on the loopback are preserved and run after the
    generated ones.
    """
    # Avoid acting on configurations that won't actually delegate. Same
    # condition as monitor's ``_uses_loopback_delegate``.
    if not _is_container_runtime(_detect_runtime_context()):
        return
    found = _find_host_loopback(config)
    if found is None:
        return

    # Locate the local owner group whose capabilities we delegate. In
    # multi-UPS this is the is_local group; in legacy single-UPS the only
    # group is implicitly local. We only delegate the local owner's
    # local-host actions — non-local groups' remote_servers shutdowns are
    # unaffected.
    owner = _local_owner_group(config)
    if owner is None:
        return

    from eneru.config import RemoteCommandConfig

    generated: list[RemoteCommandConfig] = []
    # Order mirrors the in-process sequence in monitor._execute_shutdown_sequence:
    # VMs first (long graceful wait), then containers (compose stacks then
    # leftover container stops), then sync. Filesystem unmount via SSH is a
    # known gap in this commit; Commit 2 adds the unmount_filesystems
    # template and the matching delegated action.
    if owner.virtual_machines.enabled:
        generated.append(RemoteCommandConfig(action="stop_vms"))
    if owner.containers.enabled:
        for compose in owner.containers.compose_files:
            generated.append(RemoteCommandConfig(
                action="stop_compose", path=compose.path,
            ))
        if owner.containers.shutdown_all_remaining_containers:
            generated.append(RemoteCommandConfig(action="stop_containers"))
        if owner.containers.include_user_containers:
            generated.append(RemoteCommandConfig(action="stop_containers_rootless"))
    if owner.filesystems.sync_enabled:
        generated.append(RemoteCommandConfig(action="sync"))
    # v5.5 (Commit 2): the unmount_filesystems template covers per-mount
    # umount with operator-configured options. Skipped when no mounts are
    # configured — the template no-ops on an empty target list.
    if owner.filesystems.unmount.enabled and owner.filesystems.unmount.mounts:
        generated.append(RemoteCommandConfig(action="unmount_filesystems"))

    if not generated:
        return

    _owner_label, server = found
    # PREPEND so generated actions (the actual local-host work) run BEFORE
    # any user-defined pre_shutdown_commands on the loopback entry.
    server.pre_shutdown_commands = generated + list(server.pre_shutdown_commands)


def _warn_on_kubernetes_local_misuse(config: Config) -> None:
    """K8s + local capabilities is supported but not recommended.

    Per the v5.5 three-profile framing, Kubernetes is for remote monitoring
    of remote systems; local-host ownership in K8s is unusual. Emit a
    startup WARNING (not ERROR) pointing operators at the right doc.
    """
    if not _is_kubernetes_runtime(_detect_runtime_context()):
        return
    if not _local_capabilities_required(config):
        return
    print(
        "WARNING: Kubernetes runtime detected with local-host capabilities "
        "configured. K8s is the remote-only profile per the v5.5 framing — "
        "local-host ownership inside a pod is unusual and not recommended. "
        "See docs/install-comparison.md for the three deployment profiles.",
        file=sys.stderr,
    )


def _exit_on_privilege_errors(config: Config) -> None:
    """Refuse non-root startup when config declares local-host ownership.

    v5.5 adds the container-loopback acceptance path: when running inside a
    container runtime (Docker, Podman, or Kubernetes) and a remote_servers
    entry is flagged ``is_host_loopback: true``, the privilege check passes
    because the actual host actions will be delegated over SSH to the host's
    sshd (which is what's privileged, not Eneru).

    The ``ENERU_SKIP_PRIVILEGE_CHECK`` env var (``1`` or ``true``) downgrades
    the check to a printed warning. Intended for E2E suites and developers
    iterating on dry-run configs where actual privilege isn't required.
    Containers in production never set this var, so the default safety
    guarantee for shipped images is unchanged.
    """
    geteuid = getattr(os, "geteuid", None)
    if geteuid is None or geteuid() == 0:
        return

    reasons = _root_required_reasons(config)
    if not reasons:
        return

    runtime = _detect_runtime_context()

    # v5.5: container + loopback configured → delegate over SSH, no root needed.
    # No banner: the synthesis (or operator-declared) loopback entry already
    # printed its own line, and root vs non-root container is purely cosmetic
    # in v5.5 — both code paths end up SSH-delegating through the loopback.
    if _is_container_runtime(runtime) and _find_host_loopback(config) is not None:
        return

    # v5.5: K8s + local capabilities + no loopback → start anyway (the warning
    # from _warn_on_kubernetes_local_misuse already fired). The daemon will
    # report 503 on /ready because capabilities aren't achievable, but it
    # stays up so it can still notify on power events from remote UPSes.
    if _is_kubernetes_runtime(runtime):
        print(
            f"v5.5: running non-root inside {runtime} with local capabilities "
            "but no host-loopback delegate configured. /ready will report 503 "
            "until either a loopback is configured or the local config is "
            "removed. See docs/install-comparison.md.",
            file=sys.stderr,
        )
        return

    if os.environ.get("ENERU_SKIP_PRIVILEGE_CHECK", "").strip().lower() in ("1", "true"):
        print(
            "WARNING: ENERU_SKIP_PRIVILEGE_CHECK is set; running non-root despite "
            "local-host orchestration features in config:",
            file=sys.stderr,
        )
        for reason in reasons:
            print(f"  - {reason}", file=sys.stderr)
        return

    print("ERROR: Eneru must run as root for local-host orchestration.")
    for reason in reasons:
        print(f"  - {reason}")
    if _is_container_runtime(runtime):
        # Docker/Podman + capabilities + no loopback usually means the user
        # disabled the auto-synthesis or explicitly set is_host_loopback: false.
        print(
            "v5.5: this container runtime supports a loopback SSH delegate. "
            "Configure a remote_servers entry with is_host_loopback: true "
            "(see examples/config-container-local.yaml and "
            "docs/containers-kubernetes.md)."
        )
    else:
        print(
            "For remote-only container/Kubernetes deployments, use multi-UPS "
            "configuration with is_local: false and set local_shutdown.enabled: false."
        )
    print(
        "To bypass for testing/dry-run, set ENERU_SKIP_PRIVILEGE_CHECK=1 "
        "(downgrades the check to a warning)."
    )
    sys.exit(1)


def _load_raw_config_for_validation(args):
    """Load the YAML mapping used for unknown-key validation."""
    config_path = getattr(args, 'config', None)
    path = Path(config_path) if config_path else None
    if path is None:
        for candidate in ConfigLoader.DEFAULT_CONFIG_PATHS:
            if candidate.exists():
                path = candidate
                break
    if path is None or not path.exists():
        return None
    try:
        import yaml
        with open(path, 'r') as f:
            raw_data = yaml.safe_load(f)
        if raw_data is None:
            return {}
        if not isinstance(raw_data, dict):
            raise ConfigValidationLoadError(
                f"ERROR: Config root in {path} must be a YAML mapping."
            )
        return raw_data
    except Exception as exc:
        if isinstance(exc, ConfigValidationLoadError):
            raise
        raise ConfigValidationLoadError(
            f"ERROR: Failed to parse {path} for validation: {exc}"
        ) from exc


def _exit_on_config_errors(config, args):
    """Prevent daemon startup when validation reports hard errors."""
    try:
        raw_data = _load_raw_config_for_validation(args)
    except ConfigValidationLoadError as exc:
        print(exc)
        sys.exit(1)
    messages = ConfigLoader.validate_config(
        config, raw_data=raw_data,
    )
    errors = [m for m in messages if m.startswith("ERROR:")]
    if not errors:
        return
    for msg in errors:
        print(msg)
    sys.exit(1)


# v5.5: legacy logging/runtime paths predate the /var/{log,run}/eneru/
# convention and only worked on the native install because the daemon ran
# as root. Inside the OCI image eneru runs as uid 10001 and cannot write
# to /var/log/ or /var/run/ directly. When a container-runtime user keeps
# the dataclass defaults (the migration-guide "no required YAML changes"
# case), transparently rewrite to the eneru-owned subdir so the existing
# native config keeps working. See docs/migrate-to-container.md.
_LEGACY_CONTAINER_PATH_REWRITES = {
    "file": ("/var/log/ups-monitor.log", "/var/log/eneru/ups-monitor.log"),
    "state_file": ("/var/run/ups-monitor.state", "/var/run/eneru/ups-monitor.state"),
    "battery_history_file": (
        "/var/run/ups-battery-history",
        "/var/run/eneru/ups-battery-history",
    ),
    "shutdown_flag_file": (
        "/var/run/ups-shutdown-scheduled",
        "/var/run/eneru/ups-shutdown-scheduled",
    ),
}


def _rewrite_legacy_paths_for_container(config: Config) -> None:
    """Auto-rewrite legacy native-install paths to /var/{log,run}/eneru/ inside containers.

    Silent — the rewrite is documented in docs/migrate-to-container.md
    and printing a banner on every container restart was noise. The
    rewrite still only fires when (a) runtime is Docker/Podman/Kubernetes
    AND (b) the current value still matches the dataclass default; an
    operator who sets explicit paths in the config opts out completely.
    """
    if not _is_container_runtime(_detect_runtime_context()):
        return
    for attr, (legacy, replacement) in _LEGACY_CONTAINER_PATH_REWRITES.items():
        if getattr(config.logging, attr, None) == legacy:
            setattr(config.logging, attr, replacement)


def _prepare_runtime_config(config: Config, *, strict_key_check: bool = True) -> None:
    """v5.5 startup preparation: auto-enable loopback, inject delegated
    actions, surface K8s warnings.

    All subcommands that act on a config (run, validate, shutdown group,
    shutdown remote) call this so the in-memory config reflects what
    would actually execute. Without it, ``eneru validate`` in a
    container shows the in-process shutdown sequence even when
    delegation would apply, and dry-run rehearsals miss the loopback
    entry entirely.

    ``strict_key_check`` controls how synthesis treats a missing default
    SSH key path:
    * True (default; used by ``run``) — error and exit 1. The daemon
      can't honor the contract without the key.
    * False (used by ``validate`` / ``shutdown group --dry-run``) —
      warn and proceed. The user is diagnosing or rehearsing; the key
      may legitimately not exist yet.
    """
    _rewrite_legacy_paths_for_container(config)
    _synthesize_loopback_if_needed(config, strict_key_check=strict_key_check)
    _warn_on_kubernetes_local_misuse(config)
    _inject_delegated_actions(config)


def _cmd_run(args):
    """Start the monitoring daemon."""
    config = _load_config(args)
    _apply_run_overrides(config, args)

    _prepare_runtime_config(config, strict_key_check=True)

    _exit_on_config_errors(config, args)
    _exit_on_missing_loopback_contract(config)
    _exit_on_privilege_errors(config)

    if config.multi_ups or config.redundancy_groups:
        coordinator = MultiUPSCoordinator(config, exit_after_shutdown=args.exit_after_shutdown)
        coordinator.run()
    else:
        monitor = UPSGroupMonitor(config, exit_after_shutdown=args.exit_after_shutdown)
        monitor.run()


def _print_shutdown_sequence(group, enabled_servers, has_local, prefix):
    """Print the shutdown sequence tree for a UPS group."""
    # v5.5: when a loopback is configured, in-process local phases are
    # SKIPPED at run time — the same work is sent over SSH to the host
    # via the loopback's pre_shutdown_commands. Show that explicitly so
    # `eneru validate` reflects what would actually execute.
    delegated = any(s.enabled and s.is_host_loopback is True for s in group.remote_servers)
    print(f"{prefix}  Shutdown sequence:")
    step = 1
    indent = f"{prefix}    "

    if has_local and not delegated:
        if group.virtual_machines.enabled:
            print(f"{indent}{step}. Virtual machines")
            step += 1
        if group.containers.enabled:
            containers = group.containers
            compose_count = len(containers.compose_files)
            detail = f" ({containers.runtime}"
            if compose_count > 0:
                detail += f", {compose_count} compose file(s)"
            detail += ")"
            print(f"{indent}{step}. Containers{detail}")
            step += 1
        if group.filesystems.sync_enabled or group.filesystems.unmount.enabled:
            parts = []
            if group.filesystems.sync_enabled:
                parts.append("sync")
            if group.filesystems.unmount.enabled:
                mount_count = len(group.filesystems.unmount.mounts)
                parts.append(f"unmount {mount_count} mount(s)")
            print(f"{indent}{step}. Filesystem {' + '.join(parts)}")
            step += 1
    elif has_local and delegated:
        # Build a brief summary of what will be delegated to the loopback.
        delegated_parts = []
        if group.virtual_machines.enabled:
            delegated_parts.append("VMs")
        if group.containers.enabled:
            delegated_parts.append("containers")
        if group.filesystems.sync_enabled:
            delegated_parts.append("sync")
        if group.filesystems.unmount.enabled:
            delegated_parts.append(
                f"unmount({len(group.filesystems.unmount.mounts)})"
            )
        if delegated_parts:
            summary = ", ".join(delegated_parts)
            print(
                f"{indent}{step}. Local actions delegated via loopback SSH: "
                f"{summary}"
            )
            step += 1

    if enabled_servers:
        ordered = compute_effective_order(enabled_servers)
        phases = {}
        for effective, server in ordered:
            phases.setdefault(effective, []).append(server)
        sorted_keys = sorted(phases.keys())
        num_phases = len(sorted_keys)

        # Detect legacy mode: no server has explicit shutdown_order
        is_legacy = all(s.shutdown_order is None for s in enabled_servers)

        if num_phases == 1:
            names = ", ".join(s.name or s.host for s in enabled_servers)
            if len(enabled_servers) == 1:
                print(f"{indent}{step}. Remote server: {names}")
            else:
                print(f"{indent}{step}. Remote servers ({len(enabled_servers)}): {names}")
        else:
            print(f"{indent}{step}. Remote servers ({len(enabled_servers)}, {num_phases} phases):")
            for phase_idx, key in enumerate(sorted_keys, 1):
                phase_servers = phases[key]
                names = ", ".join(s.name or s.host for s in phase_servers)
                if is_legacy:
                    # Legacy mode: label by execution style
                    if key < 0:
                        print(f"{indent}   Sequential: {names}")
                    else:
                        print(f"{indent}   Parallel: {names}")
                else:
                    print(f"{indent}   Phase {phase_idx} (order={key}): {names}")
        step += 1
    else:
        print(f"{indent}(no remote servers)")

    if has_local:
        if delegated:
            print(
                f"{indent}{step}. Local shutdown (host poweroff "
                "delegated via loopback SSH)"
            )
        else:
            print(f"{indent}{step}. Local shutdown")


def _print_group_summary(group, idx, multi_ups):
    """Print a single UPS group summary for validate output."""
    label = group.ups.label
    prefix = f"  Group {idx}: " if multi_ups else "  "

    print(f"{prefix}UPS: {label}", end="")
    if group.ups.display_name:
        print(f" ({group.ups.name})", end="")
    if group.is_local:
        print(" [is_local]", end="")
    print()

    # Shutdown sequence tree
    enabled_servers = [s for s in group.remote_servers if s.enabled]
    has_local = group.is_local or not multi_ups
    _print_shutdown_sequence(group, enabled_servers, has_local, prefix)


def _cmd_validate(args):
    """Validate configuration and print overview."""
    config = _load_config(args)
    # v5.5: run synthesis + injection so the validate output reflects what
    # would actually execute (the delegated shutdown sequence in a
    # container, not the in-process one). Non-strict — a missing default
    # SSH key downgrades to a warning so users can still inspect their
    # config without first generating the key.
    _prepare_runtime_config(config, strict_key_check=False)
    exit_code = 0

    print(f"Eneru v{__version__}")
    print(f"  Runtime context: {_detect_runtime_context()}")

    multi_ups = config.multi_ups
    if multi_ups:
        print(f"  Mode: multi-UPS ({len(config.ups_groups)} groups)")
        local_groups = [g for g in config.ups_groups if g.is_local]
        if local_groups:
            print(f"  Local UPS: {local_groups[0].ups.label}")
        else:
            print(f"  Local UPS: none (Eneru host has independent power)")
        print(f"  Drain on local shutdown: {config.local_shutdown.drain_on_local_shutdown}")
        print()

    for idx, group in enumerate(config.ups_groups, 1):
        _print_group_summary(group, idx, multi_ups)
        if idx < len(config.ups_groups):
            print()

    if config.redundancy_groups:
        print()
        print(f"  Redundancy groups ({len(config.redundancy_groups)}):")
        for rg_idx, rg in enumerate(config.redundancy_groups, 1):
            label = rg.name or "(unnamed)"
            tags = []
            if rg.is_local:
                tags.append("is_local")
            tag_suffix = f" [{', '.join(tags)}]" if tags else ""
            sources = ", ".join(rg.ups_sources) if rg.ups_sources else "(none)"
            print(f"    {rg_idx}. {label}{tag_suffix}")
            print(f"       Sources ({len(rg.ups_sources)}): {sources}")
            print(
                f"       Quorum: min_healthy={rg.min_healthy} "
                f"(degraded→{rg.degraded_counts_as}, unknown→{rg.unknown_counts_as})"
            )
            enabled_servers = [s for s in rg.remote_servers if s.enabled]
            if enabled_servers:
                names = ", ".join(s.name or s.host for s in enabled_servers)
                print(f"       Remote servers ({len(enabled_servers)}): {names}")
            local_parts = []
            if rg.is_local and rg.virtual_machines.enabled:
                local_parts.append("VMs")
            if rg.is_local and rg.containers.enabled:
                local_parts.append("containers")
            if rg.is_local and (rg.filesystems.sync_enabled or rg.filesystems.unmount.enabled):
                local_parts.append("filesystems")
            if local_parts:
                print(f"       Local resources: {', '.join(local_parts)}")

    print(f"  Dry-run: {config.behavior.dry_run}")

    # Notification status
    print(f"  Notifications:")
    if config.notifications.enabled and config.notifications.urls:
        if APPRISE_AVAILABLE:
            print(f"    Enabled: {len(config.notifications.urls)} service(s)")
            for url in config.notifications.urls:
                if '://' in url:
                    scheme = url.split('://')[0]
                    print(f"      - {scheme}://***")
                else:
                    print(f"      - {url[:20]}...")
            if config.notifications.title:
                print(f"    Title: {config.notifications.title}")
            else:
                print(f"    Title: (none)")
            if config.notifications.avatar_url:
                print(f"    Avatar URL: {config.notifications.avatar_url[:50]}...")
            print(f"    Retry interval: {config.notifications.retry_interval}s")
        else:
            print(f"    Apprise not installed - notifications disabled")
            print(f"    Install with: pip install apprise")
    else:
        print(f"    Disabled")

    # Re-parse the raw YAML so validation can catch misspelled safety keys
    # that ConfigLoader intentionally ignores while building dataclasses.
    raw_data = None
    try:
        raw_data = _load_raw_config_for_validation(args)
    except ConfigValidationLoadError as exc:
        print()
        print(f"  {exc}")
        exit_code = 1
    messages = ConfigLoader.validate_config(config, raw_data=raw_data)
    if messages:
        print()
        for msg in messages:
            print(f"  {msg}")
            if msg.startswith("ERROR"):
                exit_code = 1

    print()
    if exit_code == 0:
        print("Configuration is valid.")
    else:
        print("Configuration is INVALID — fix the ERROR(s) above and re-run.")

    sys.exit(exit_code)


def _cmd_test_notifications(args):
    """Send a test notification and exit."""
    config = _load_config(args)
    exit_code = 0

    print("Testing notifications...")

    if not config.notifications.enabled or not config.notifications.urls:
        print("No notification URLs configured.")
        print("   Add URLs to the 'notifications.urls' section in your config file.")
        sys.exit(1)

    if not APPRISE_AVAILABLE:
        print("Apprise is not installed.")
        print("   Install with: pip install apprise")
        sys.exit(1)

    apobj = apprise.Apprise()
    valid_urls = 0

    for url in config.notifications.urls:
        if apobj.add(url):
            valid_urls += 1
            scheme = url.split('://')[0] if '://' in url else 'unknown'
            print(f"  Added: {scheme}://***")
        else:
            print(f"  Invalid URL: {url[:30]}...")

    if valid_urls == 0:
        print("No valid notification URLs found.")
        sys.exit(1)

    print(f"\nSending test notification to {valid_urls} service(s)...")

    if config.notifications.title:
        print(f"  Title: {config.notifications.title}")
    if config.notifications.avatar_url:
        print(f"  Avatar: {config.notifications.avatar_url[:50]}...")

    # Build test body with all UPS names
    ups_lines = []
    for group in config.ups_groups:
        ups_lines.append(f"  {group.ups.label}")

    test_body = (
        "**Test Notification**\n"
        "This is a test notification from Eneru.\n"
        "If you see this, notifications are working correctly!\n"
        f"\n---\nUPS monitored:\n" + "\n".join(ups_lines) + "\n"
        f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S %Z')}"
    )

    escaped_body = test_body.replace("@", "@\u200B")

    notify_kwargs = {
        'body': escaped_body,
        'notify_type': apprise.NotifyType.INFO,
    }
    if config.notifications.title:
        notify_kwargs['title'] = config.notifications.title

    result = apobj.notify(**notify_kwargs)

    if result:
        print("Test notification sent successfully!")
    else:
        print("Failed to send test notification.")
        print("   Check your notification URLs and network connectivity.")
        exit_code = 1

    sys.exit(exit_code)


class _CLILogger:
    """Small logger adapter used by one-shot CLI drills."""

    def __init__(self, log_file: Optional[Path] = None) -> None:
        self.log_file = Path(log_file) if log_file else None

    def log(self, message: str, **_extra) -> None:
        # ``**_extra`` mirrors UPSLogger.log(message, **extra). The drill is
        # one-shot and prints to stdout / appends to a flat log file, so
        # structured kwargs (category, event_type, group, ...) are accepted
        # for signature compatibility but otherwise ignored — they're
        # meaningful only under the JSON formatter that the daemon uses.
        print(message)
        if self.log_file:
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
            # encoding="utf-8" so emoji and non-ASCII names in server.host /
            # server.user round-trip cleanly into the log file regardless
            # of the user's LANG/LC_ALL.
            with self.log_file.open("a", encoding="utf-8") as f:
                f.write(message + "\n")


def _iter_remote_server_owners(config):
    """Yield ``(owner_label, owner_name, server)`` for every remote server."""
    for group in config.ups_groups:
        for server in group.remote_servers:
            yield group.ups.label, group.ups.name, server
    for group in config.redundancy_groups:
        label = f"redundancy:{group.name}"
        for server in group.remote_servers:
            yield label, group.name, server


def _format_remote_list_table(rows: list) -> str:
    """Format remote-target rows as a fixed-width ASCII table.

    Width is computed per-column from the actual data so long names
    don't truncate; each column has a minimum that keeps the header
    readable when every row is short.
    """
    headers = ("NAME", "GROUP", "KIND", "HOST", "ENABLED", "ORDER")
    minimums = (10, 10, 10, 10, 7, 5)
    columns = list(zip(*[headers, *rows])) if rows else [(h,) for h in headers]
    widths = [
        max(minimums[i], max(len(str(cell)) for cell in column))
        for i, column in enumerate(columns)
    ]
    last = len(widths) - 1

    def fmt_row(row):
        return "  ".join(
            str(cell) if i == last else str(cell).ljust(widths[i])
            for i, cell in enumerate(row)
        )

    lines = [fmt_row(headers)]
    for row in rows:
        lines.append(fmt_row(row))
    return "\n".join(lines)


def _build_remote_list_rows_for_group(group, group_name: str, kind: str) -> tuple:
    """Build display rows for one group's remote_servers.

    Returns ``(rows, enabled_count)``. ``group_name`` is the value an
    operator passes to ``--group`` to address this group — keeping the
    GROUP column 1-to-1 with the CLI flag avoids the v5.3-rc disagreement
    where the column showed ``name (label)`` but ``--group`` only
    accepted the raw token.

    Effective order is computed on ``enabled`` servers only so the
    printed ORDER matches what the daemon would actually use during
    shutdown (the daemon also filters before computing). Disabled rows
    show ``—`` since they don't participate in the rotation at all.
    """
    enabled_servers = [s for s in group.remote_servers if s.enabled]
    order_by_id = {
        id(s): effective for effective, s in compute_effective_order(enabled_servers)
    }
    rows = []
    for server in group.remote_servers:
        host = f"{server.user}@{server.host}" if server.user else server.host
        if server.enabled:
            order_text = str(order_by_id[id(server)])
        else:
            order_text = "—"
        rows.append((
            server.name or server.host,
            group_name,
            kind,
            host,
            "yes" if server.enabled else "no",
            order_text,
        ))
    return rows, len(enabled_servers)


def _cmd_remote_list(args):
    """List configured remote shutdown targets across all groups."""
    config = _load_config(args)
    _exit_on_config_errors(config, args)

    # Stable per-group ordering so users see related rows next to each
    # other. Within a group, the helper sorts by daemon-effective
    # shutdown order so the printed sequence matches a real shutdown.
    rows = []
    enabled_count = 0
    for group in config.ups_groups:
        # Use the canonical name when present so the GROUP column is
        # exactly the string `eneru shutdown group --group ...` accepts;
        # fall back to the label only when name is empty.
        group_name = group.ups.name or group.ups.label or "(unnamed)"
        group_rows, group_enabled = _build_remote_list_rows_for_group(
            group, group_name, "ups",
        )
        rows.extend(group_rows)
        enabled_count += group_enabled
    for group in config.redundancy_groups:
        group_rows, group_enabled = _build_remote_list_rows_for_group(
            group, group.name, "redundancy",
        )
        rows.extend(group_rows)
        enabled_count += group_enabled

    if not rows:
        print("No remote targets configured.")
        sys.exit(1)

    print(f"REMOTE TARGETS ({len(rows)} configured, {enabled_count} enabled)")
    print()
    print(_format_remote_list_table(rows))


def _select_remote_server(config, server_ref: str, group_ref: str = None):
    """Select exactly one remote server from config."""
    matches = []
    for owner_label, owner_name, server in _iter_remote_server_owners(config):
        if not server.enabled:
            continue
        names = {server.name, server.host, server.name or server.host}
        if server_ref in names:
            if group_ref and group_ref not in {owner_label, owner_name}:
                continue
            matches.append((owner_label, owner_name, server))
    if not matches:
        raise SystemExit(f"ERROR: enabled remote server {server_ref!r} not found")
    if len(matches) > 1:
        owners = ", ".join(owner for owner, _, _ in matches)
        raise SystemExit(
            f"ERROR: remote server {server_ref!r} is ambiguous. "
            f"Use --group. Matches: {owners}"
        )
    return matches[0]


def _cmd_shutdown_remote(args):
    """Run a manual one-server remote shutdown drill."""
    config = _load_config(args)
    _exit_on_config_errors(config, args)

    if not args.dry_run and not args.confirm:
        print(
            "ERROR: real remote shutdown requires "
            "--i-really-want-to-proceed-with-remote-shutdown"
        )
        sys.exit(2)

    owner_label, owner_name, server = _select_remote_server(
        config, args.server, args.group,
    )

    logger = _CLILogger(args.log_file)
    logger.log(f"Manual remote shutdown drill: {server.name or server.host}")
    logger.log(f"  Group: {owner_label}")
    logger.log(f"  Host: {server.user}@{server.host}")
    logger.log(f"  Mode: {'dry-run' if args.dry_run else 'REAL SHUTDOWN'}")

    if args.connectivity_check:
        probe = config.remote_health.probe_command
        if not is_safe_probe_command(probe):
            logger.log("  Connectivity check: skipped (unsafe probe command rejected)")
        else:
            ok, error, latency = run_remote_probe(server, probe)
            if ok:
                logger.log(f"  Connectivity check: OK ({latency} ms)")
            else:
                logger.log(f"  Connectivity check: FAILED ({error})")

    drill_config = Config(
        ups_groups=[
            UPSGroupConfig(
                ups=UPSConfig(name=owner_name or owner_label,
                              display_name=owner_label),
                remote_servers=[server],
                is_local=False,
            )
        ],
        behavior=config.behavior,
        logging=config.logging,
        notifications=config.notifications,
        local_shutdown=config.local_shutdown,
        statistics=config.statistics,
        api=config.api,
        prometheus=config.prometheus,
        remote_health=config.remote_health,
        mqtt=config.mqtt,
    )
    # PRECEDENCE NOTE: the drill follows the CLI flag, NOT the config-level
    # ``behavior.dry_run`` setting. The drill is a per-invocation operator
    # tool gated by ``--i-really-want-to-proceed-with-remote-shutdown``;
    # that explicit confirmation flag is the safety contract, and the
    # ``--dry-run`` flag is what the operator picks per drill. Config-level
    # ``behavior.dry_run: true`` does NOT silently downgrade a confirmed
    # drill to dry-run — see docs/remote-servers.md "Manual remote
    # shutdown drill" for the full precedence rationale. If you're
    # auditing this for safety: the drill cannot run real commands
    # without ``--i-really-want-to-proceed-with-remote-shutdown`` (line
    # 426-431 above).
    drill_config.behavior.dry_run = bool(args.dry_run)

    monitor = UPSGroupMonitor(drill_config)
    monitor.logger = logger
    monitor._notification_worker = None

    if args.dry_run:
        logger.log("  Dry-run: configured remote commands will not be executed.")
    monitor._shutdown_remote_server(server)
    logger.log("Manual remote shutdown drill complete.")


def _resolve_group_for_rehearsal(config, group_ref: str):
    """Locate a UPS or redundancy group by name for the rehearsal command.

    Returns ``(kind, group)`` where ``kind`` is ``"ups"`` or
    ``"redundancy"``. Matches UPS groups against the friendly label
    first and the canonical name second; redundancy groups against
    ``RedundancyGroupConfig.name``. Raises SystemExit with an
    operator-friendly message when nothing matches or the name is
    ambiguous (within or across kinds).
    """
    matches = []
    for group in config.ups_groups:
        if group_ref in {group.ups.label, group.ups.name}:
            matches.append(("ups", group))
    for group in config.redundancy_groups:
        if group_ref == group.name:
            matches.append(("redundancy", group))
    if not matches:
        raise SystemExit(
            f"ERROR: group {group_ref!r} not found. "
            f"Use 'eneru remote list' to see configured names."
        )
    if len(matches) > 1:
        described = ", ".join(
            f"{(g.ups.label if kind == 'ups' else g.name) or '(unnamed)'} ({kind})"
            for kind, g in matches
        )
        raise SystemExit(
            f"ERROR: group {group_ref!r} matches multiple groups: "
            f"{described}. Rename one of them or open an issue if you "
            f"need a --kind flag."
        )
    return matches[0]


def _cmd_shutdown_group(args):
    """Run a manual full-sequence shutdown rehearsal for one named group."""
    import atexit
    import shutil
    import tempfile
    import threading

    config = _load_config(args)
    # v5.5: synthesize loopback + inject delegated actions so the
    # rehearsal exercises the same shutdown path the daemon would. Use
    # strict only for real shutdown. Dry-run rehearsals still work before
    # the default loopback key is materialized because they never SSH.
    _prepare_runtime_config(config, strict_key_check=not args.dry_run)
    _exit_on_config_errors(config, args)

    if not args.dry_run and not args.confirm:
        print(
            "ERROR: real group shutdown requires "
            "--i-really-want-to-proceed-with-group-shutdown"
        )
        sys.exit(2)

    kind, group = _resolve_group_for_rehearsal(config, args.group)

    logger = _CLILogger(args.log_file)
    label = group.ups.label if kind == "ups" else group.name
    logger.log(f"Manual group shutdown rehearsal: {label}")
    logger.log(f"  Kind: {kind}")
    logger.log(f"  Mode: {'dry-run' if args.dry_run else 'REAL SHUTDOWN'}")
    if kind == "redundancy":
        # The coordinator's local-poweroff callback isn't wired in this
        # one-shot path. Calling it would let an operator confirm a
        # "rehearsal" and accidentally halt the host because the
        # callback bypasses the per-rehearsal flag isolation. The
        # executor still drains is_local resources (VMs, containers,
        # filesystems) before that gated step, so a confirmed rehearsal
        # of an is_local redundancy group really does stop them.
        is_local_redundancy = bool(getattr(group, "is_local", False))
        if is_local_redundancy and not args.dry_run and args.confirm:
            logger.log(
                "  WARNING: this is_local redundancy group WILL stop "
                "local VMs/containers and unmount configured filesystems "
                "on this host. Only the final poweroff command is "
                "suppressed by the rehearsal."
            )
        else:
            logger.log(
                "  Note: redundancy rehearsal does not fire local "
                "poweroff even with confirm flag."
            )

    # Isolate per-rehearsal state files so the rehearsal can never
    # collide with a running daemon's flag/state files (which would
    # block the daemon from re-firing its own shutdowns). The
    # atexit hook is belt-and-braces: try/finally below covers normal
    # control flow, atexit covers an unexpected interpreter shutdown
    # before the finally runs (e.g. an unhandled SystemExit raised
    # deep in a mixin). Neither path covers SIGKILL — accepted, the
    # tempdir holds no secrets and is mode 0700.
    rehearsal_dir = Path(tempfile.mkdtemp(prefix="eneru-rehearsal-"))
    atexit.register(shutil.rmtree, str(rehearsal_dir), True)
    try:
        config.logging.shutdown_flag_file = str(
            rehearsal_dir / "rehearsal.shutdown-flag"
        )
        config.logging.battery_history_file = str(
            rehearsal_dir / "rehearsal.battery-history"
        )
        config.logging.state_file = str(
            rehearsal_dir / "rehearsal.state"
        )
        config.behavior.dry_run = bool(args.dry_run)

        if kind == "ups":
            drill_config = Config(
                ups_groups=[group],
                behavior=config.behavior,
                logging=config.logging,
                notifications=config.notifications,
                local_shutdown=config.local_shutdown,
                statistics=config.statistics,
                api=config.api,
                prometheus=config.prometheus,
                remote_health=config.remote_health,
                mqtt=config.mqtt,
            )
            monitor = UPSGroupMonitor(drill_config)
            monitor.logger = logger
            monitor._notification_worker = None
            # Detect container runtime + compose support before the
            # shutdown sequence runs. The daemon does this in
            # _initialize() before its main loop; the rehearsal goes
            # straight to _execute_shutdown_sequence(), so without this
            # call _container_runtime stays None and the containers
            # phase is silently skipped — defeating the point of a
            # rehearsal. Mirrors what RedundancyGroupExecutor does in
            # its own __init__.
            monitor._check_dependencies()
            monitor._execute_shutdown_sequence()
        else:
            executor = RedundancyGroupExecutor(
                group,
                base_config=config,
                logger=logger,
                stop_event=threading.Event(),
                notification_worker=None,
                local_shutdown_callback=None,
            )
            executor.shutdown(reason="manual rehearsal via CLI")
    finally:
        shutil.rmtree(rehearsal_dir, ignore_errors=True)

    logger.log("Manual group shutdown rehearsal complete.")


def _cmd_version(args):
    """Print version and exit."""
    print(f"Eneru v{__version__}")


def _cmd_completion(args):
    """Print a self-contained shell completion script to stdout.

    Scripts live inside the ``eneru.completion`` subpackage so they ship
    with both pip and deb/rpm installs and can be read via
    ``importlib.resources`` regardless of how the package was installed.
    nfpm.yaml additionally drops them at the canonical FHS paths so the
    host shell auto-loads them when bash-completion / zsh / fish is
    present. PyPI users source the runtime output directly:
    ``source <(eneru completion bash)``.
    """
    import importlib.resources

    shell = args.shell
    filename = {"bash": "eneru.bash",
                "zsh":  "eneru.zsh",
                "fish": "eneru.fish"}[shell]
    try:
        text = (importlib.resources.files("eneru.completion") / filename).read_text()
    except (FileNotFoundError, ModuleNotFoundError) as exc:
        print(f"Error: completion script for '{shell}' not found ({exc})",
              file=sys.stderr)
        sys.exit(1)
    sys.stdout.write(text)


def _cmd_deliver_stop(args):
    """v5.2.1 internal subcommand — invoked by the systemd-run timer
    scheduled at the previous daemon's exit. Idempotent:

    - If the lifecycle 'Service Stopped' row was already cancelled by
      the next daemon's classifier (`status='cancelled'`,
      `cancel_reason='superseded'`), we exit silently — the user gets
      a single Restarted/Upgraded/Recovered notification.
    - If the row is still `pending` (no replacement daemon came up),
      we deliver via Apprise and mark the row `sent` — the user gets
      a single Stopped notification.

    The subcommand name is prefixed with `_` to mark it as internal;
    it's intentionally absent from the `--help` listing.
    """
    from pathlib import Path
    from eneru.deferred_delivery import deliver_pending_stop

    config = _load_config(args)
    sys.exit(deliver_pending_stop(
        notification_id=int(args.notification_id),
        db_path=Path(args.db_path),
        config=config,
    ))


def _cmd_monitor(args):
    """Launch the TUI dashboard."""
    config = _load_config(args)

    from eneru.tui import run_tui, run_once, EVENTS_MAX_ROWS_NORMAL

    if args.once:
        run_once(
            config,
            graph_metric=getattr(args, "graph", None),
            time_range=getattr(args, "time", "1h"),
            events_only=getattr(args, "events_only", False),
            verbose=getattr(args, "verbose", False),
            length=getattr(args, "length", EVENTS_MAX_ROWS_NORMAL),
        )
    else:
        run_tui(
            config,
            interval=args.interval,
            initial_graph=getattr(args, "graph", None),
            initial_time_range=getattr(args, "time", "1h"),
            verbose=getattr(args, "verbose", False),
        )


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Eneru - Intelligent UPS Monitoring & Shutdown Orchestration for NUT",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "subcommands:\n"
            "  run                  Start the monitoring daemon\n"
            "  remote list          List configured remote shutdown targets\n"
            "  shutdown remote      Manually drill one configured remote shutdown\n"
            "  shutdown group       Rehearse the full shutdown sequence for one group\n"
            "  validate             Validate configuration and show overview\n"
            "  monitor / tui        Launch real-time TUI dashboard\n"
            "  test-notifications   Send a test notification\n"
            "  completion           Print shell completion script (bash/zsh/fish)\n"
            "  version              Show version information\n"
            "\nExamples:\n"
            "  eneru run --config /etc/ups-monitor/config.yaml\n"
            "  eneru remote list --config /etc/ups-monitor/config.yaml\n"
            "  eneru shutdown group --group rack-a --dry-run --config /etc/ups-monitor/config.yaml\n"
            "  eneru validate --config /etc/ups-monitor/config.yaml\n"
            "  eneru monitor --config /etc/ups-monitor/config.yaml\n"
            "  eneru tui --config /etc/ups-monitor/config.yaml\n"
        ),
    )

    subparsers = parser.add_subparsers(dest="command")

    # --- run ---
    run_parser = subparsers.add_parser("run", help="Start the monitoring daemon")
    run_parser.add_argument("-c", "--config", help="Path to configuration file", default=None)
    run_parser.add_argument("--dry-run", action="store_true",
                            help="Run in dry-run mode (overrides config)")
    run_parser.add_argument("--api", action="store_true",
                            help="Enable the embedded read-only API")
    run_parser.add_argument("--api-bind",
                            help="API listen address (implies --api)")
    run_parser.add_argument("--api-port", type=_port_int,
                            help="API listen port, 1..65535 (implies --api)")
    run_parser.add_argument("--exit-after-shutdown", action="store_true",
                            help="Exit after completing shutdown sequence")
    run_parser.set_defaults(func=_cmd_run)

    # --- shutdown remote ---
    shutdown_parser = subparsers.add_parser("shutdown", help="Manual shutdown drills")
    shutdown_sub = shutdown_parser.add_subparsers(dest="shutdown_command")
    remote_parser = shutdown_sub.add_parser(
        "remote",
        help="Run a manual shutdown drill for one configured remote server",
    )
    remote_parser.add_argument("-c", "--config", help="Path to configuration file",
                               default=None)
    remote_parser.add_argument("--server", required=True,
                               help="Remote server name or host from config")
    remote_parser.add_argument("--group",
                               help="UPS/redundancy group when server name is ambiguous")
    remote_parser.add_argument("--dry-run", action="store_true",
                               help="Do not execute configured remote commands")
    remote_parser.add_argument(
        "--i-really-want-to-proceed-with-remote-shutdown",
        dest="confirm",
        action="store_true",
        help="Required for real remote command execution",
    )
    remote_parser.add_argument("--connectivity-check", dest="connectivity_check",
                               action="store_true", default=True,
                               help="Run harmless SSH probe first (default)")
    remote_parser.add_argument("--no-connectivity-check", dest="connectivity_check",
                               action="store_false",
                               help="Skip harmless SSH probe")
    remote_parser.add_argument("--log-file",
                               help="Optional file to append this drill log to")
    remote_parser.set_defaults(func=_cmd_shutdown_remote)

    # --- shutdown group ---
    group_parser = shutdown_sub.add_parser(
        "group",
        help="Rehearse the full configured shutdown sequence for one group",
    )
    group_parser.add_argument("-c", "--config", help="Path to configuration file",
                              default=None)
    group_parser.add_argument("--group", required=True,
                              help="UPS group label/name or redundancy group name")
    group_parser.add_argument("--dry-run", action="store_true",
                              help="Log every phase without executing real commands")
    group_parser.add_argument(
        "--i-really-want-to-proceed-with-group-shutdown",
        dest="confirm",
        action="store_true",
        help=(
            "Required for real execution. For UPS groups this WILL halt the "
            "host if local_shutdown.enabled. Redundancy groups never fire "
            "local poweroff from the CLI rehearsal."
        ),
    )
    group_parser.add_argument("--log-file",
                              help="Optional file to append this rehearsal log to")
    group_parser.set_defaults(func=_cmd_shutdown_group)

    # --- remote list ---
    remote_top_parser = subparsers.add_parser(
        "remote", help="Inspect configured remote shutdown targets",
    )
    remote_top_sub = remote_top_parser.add_subparsers(dest="remote_command")
    remote_list_parser = remote_top_sub.add_parser(
        "list", help="List configured remote shutdown targets",
    )
    remote_list_parser.add_argument(
        "-c", "--config", help="Path to configuration file", default=None,
    )
    remote_list_parser.set_defaults(func=_cmd_remote_list)

    # --- validate ---
    val_parser = subparsers.add_parser("validate", help="Validate configuration and show overview")
    val_parser.add_argument("-c", "--config", help="Path to configuration file", default=None)
    val_parser.set_defaults(func=_cmd_validate)

    # --- monitor / tui ---
    # `tui` is an alias for `monitor` -- same handler, same options. We
    # register two parsers (rather than argparse `aliases=`) so each shows
    # up as a first-class entry in the top-level help and gets its own
    # `--help` page that names the subcommand the user actually typed.
    def _add_monitor_args(p):
        p.add_argument("-c", "--config", help="Path to configuration file", default=None)
        p.add_argument("--once", action="store_true",
                       help="Print status snapshot and exit (no TUI)")
        p.add_argument("--interval", type=int, default=5,
                       help="Refresh interval in seconds (default: 5)")
        p.add_argument("--graph",
                       choices=["charge", "load", "voltage", "runtime"],
                       help="Initial graph metric. With --once renders a Braille snapshot; "
                            "in interactive TUI pre-selects the metric (still cycle with <G>)")
        # IMPORTANT: --time is GRAPH-ONLY in 5.2.2+. It must NOT be
        # threaded into the events query. Events are sparse and a fixed
        # window made the panel silently empty for normal homelab usage
        # (the events panel then fell back to log parsing without the
        # operator noticing). Use --length to size the events list.
        p.add_argument("--time", default="1h",
                       help="Graph time range (1h/6h/24h/7d/30d). Applies only to the "
                            "graph -- the events list is independent (use --length to size it)")
        p.add_argument("--events-only", action="store_true",
                       help="With --once: print only the events list (SQLite, log-tail fallback)")
        p.add_argument("--verbose", "-v", action="count", default=0,
                       help="Increase event verbosity. Default shows Power Events only; "
                            "-v adds Diagnostics; -vv adds Lifecycle. Applies to both "
                            "--once and the interactive TUI; cycle in-session with <V>")
        p.add_argument("--length", type=_non_negative_int, default=30,
                       metavar="N",
                       help="With --once: max events to print (default: 30, 0 = no cap). "
                            "Power events are always preserved within the cap; diagnostics "
                            "fill next, lifecycle fills last")
        p.set_defaults(func=_cmd_monitor)

    mon_parser = subparsers.add_parser("monitor", help="Launch real-time TUI dashboard")
    _add_monitor_args(mon_parser)

    tui_parser = subparsers.add_parser(
        "tui", help="Alias for 'monitor' -- launch real-time TUI dashboard")
    _add_monitor_args(tui_parser)

    # --- test-notifications ---
    tn_parser = subparsers.add_parser("test-notifications",
                                      help="Send a test notification and exit")
    tn_parser.add_argument("-c", "--config", help="Path to configuration file", default=None)
    tn_parser.set_defaults(func=_cmd_test_notifications)

    # --- version ---
    ver_parser = subparsers.add_parser("version", help="Show version information")
    ver_parser.set_defaults(func=_cmd_version)

    # --- completion ---
    comp_parser = subparsers.add_parser(
        "completion",
        help="Print shell completion script (source it: source <(eneru completion bash))")
    comp_parser.add_argument("shell", choices=["bash", "zsh", "fish"],
                             help="Shell to emit completion for")
    comp_parser.set_defaults(func=_cmd_completion)

    # --- _deliver-stop (internal, v5.2.1) ---
    # Hidden from the --help listing on purpose: this is invoked by a
    # systemd-run transient timer scheduled by the previous daemon's
    # _cleanup_and_exit / _handle_signal, never by users directly.
    ds_parser = subparsers.add_parser("_deliver-stop", help=argparse.SUPPRESS)
    ds_parser.add_argument("--notification-id", required=True, type=int)
    ds_parser.add_argument("--db-path", required=True)
    ds_parser.add_argument("-c", "--config", required=True,
                           help="Path to configuration file")
    ds_parser.set_defaults(func=_cmd_deliver_stop)

    args = parser.parse_args()

    # No subcommand provided -> show help
    if args.command is None or not hasattr(args, "func"):
        parser.print_help()
        sys.exit(0)

    args.func(args)


if __name__ == "__main__":
    main()
