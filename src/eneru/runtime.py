"""Runtime-context detection + loopback-delegation predicate (leaf module).

F-057: these helpers answer "what kind of process am I, and should my local-host
actions be delegated to the host over SSH?" -- questions the DOMAIN core
(``monitor.py``, ``redundancy.py``, ``multi_ups.py``) and the CLI both need.

They used to live in ``cli.py``, so ``monitor.py`` / ``redundancy.py`` reached
UP into the CLI with function-local ``from eneru.cli import ...`` imports -- a
layering inversion (core → CLI). Moving them here, a leaf that imports nothing
from the daemon or CLI, lets every caller import DOWN instead. ``cli.py`` now
re-exports these names for its own internal use and for existing test patch
targets (``eneru.cli._detect_runtime_context`` still resolves, because cli-local
bare-name calls consult cli's namespace); tests that exercise the predicate
itself patch the canonical ``eneru.runtime.*`` location.
"""

from __future__ import annotations

import functools
import os
from pathlib import Path

from eneru.config import Config


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


@functools.lru_cache(maxsize=1)
def _detect_runtime_context() -> str:
    """Best-effort runtime-context label for the current process.

    F-049: memoized. A process's container/runtime identity is fixed for its
    whole life — a daemon can't migrate from bare metal into a Docker container
    mid-run — so the ``/proc/1/cgroup`` / ``/proc/self/mountinfo`` / ``/proc/1/
    comm`` / env-var probing here is pure overhead when repeated. Startup alone
    called this up to ~6× (plus once per status collection via status.py), each
    re-reading the same unchanging files. ``lru_cache(maxsize=1)`` (the function
    takes no args) computes it once and hands back the cached label thereafter.
    Because the cache outlives individual calls, any TEST that patches ``/proc``
    or the environment to simulate a DIFFERENT runtime must call
    ``_detect_runtime_context.cache_clear()`` first, or it will see a stale
    label from an earlier scenario.

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
