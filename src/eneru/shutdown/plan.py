"""Read-only introspection of the controlled shutdown sequence (v6.1).

Pure and side-effect-free: turns the resolved config (plus the few runtime
flags the monitor already knows — local group? delegating to the host? running
under a coordinator?) into the SAME ordered phase list that
``UPSGroupMonitor._execute_shutdown_sequence`` walks. This lets the dashboard
show operators *what would happen* on a power-loss shutdown without touching —
and so never risking — the execution path itself.

**The order in ``PHASE_ORDER`` is kept in lockstep with the executor**
(monitor._execute_shutdown_sequence): VM → containers → filesystem sync →
filesystem unmount → remote servers (parallel within a shutdown-order group) →
final sync → local host poweroff (or coordinator handoff). A unit test asserts
the planner walks exactly this order so the two cannot silently diverge.
"""
from typing import Any, Dict, List, Optional

__all__ = ["build_shutdown_plan", "PHASE_ORDER"]

# Canonical phase order — mirrors monitor._execute_shutdown_sequence. Keep in
# sync (test_shutdown_plan asserts the built plan follows this order).
PHASE_ORDER = (
    "vms", "containers", "filesystem-sync", "filesystem-unmount",
    "remote", "final-sync", "local-poweroff",
)


def _phase(pid: str, title: str, *, enabled: bool,
           steps: List[Dict[str, Any]], mode: str = "sequential",
           skipped: Optional[str] = None,
           estimate_s: Optional[float] = None) -> Dict[str, Any]:
    return {"id": pid, "title": title, "mode": mode, "enabled": bool(enabled),
            "skipped": skipped, "estimateS": estimate_s, "steps": steps}


def _unmount_step(mount: Any) -> Dict[str, Any]:
    """A mount entry (dict or object) -> an unmount step. Mounts carry a
    ``path`` and optional ``options`` (e.g. ``-l`` for lazy)."""
    if isinstance(mount, dict):
        path, opts = mount.get("path"), mount.get("options")
    else:
        path, opts = getattr(mount, "path", None), getattr(mount, "options", None)
    path = path or str(mount)
    step = {"label": f"Unmount {path}"}
    if opts:
        step["detail"] = f"options: {opts}"
    return step


def _local_skip(is_local: bool, delegated: bool, enabled: bool) -> Optional[str]:
    """Why a local drain phase wouldn't run, or None when it would."""
    if delegated:
        return "delegated to host"
    if not is_local:
        return "non-local group"
    if not enabled:
        return "disabled"
    return None


def build_shutdown_plan(config: Any, *, is_local: bool = True,
                        delegated: bool = False,
                        coordinator_mode: bool = False) -> Dict[str, Any]:
    """Build the structured shutdown plan from a (group-scoped) config.

    ``is_local`` / ``delegated`` / ``coordinator_mode`` are the runtime flags the
    monitor uses to gate phases; pass the monitor's actual values so the plan
    matches what that daemon would really do.
    """
    local_active = is_local and not delegated
    phases: List[Dict[str, Any]] = []

    # 1) Virtual machines (libvirt).
    vm = config.virtual_machines
    vm_on = local_active and vm.enabled
    phases.append(_phase(
        "vms", "Virtual machines", enabled=vm_on,
        skipped=_local_skip(is_local, delegated, vm.enabled),
        steps=[{"label": "Gracefully shut down running VMs (libvirt)"}] if vm_on else []))

    # 2) Containers.
    c = config.containers
    c_on = local_active and c.enabled
    runtime = c.runtime if c.runtime != "auto" else "auto-detect"
    n_compose = len(c.compose_files or [])
    detail = runtime + (f", {n_compose} compose file{'s' if n_compose != 1 else ''}"
                        if n_compose else "")
    phases.append(_phase(
        "containers", "Containers", enabled=c_on,
        skipped=_local_skip(is_local, delegated, c.enabled),
        estimate_s=float(c.stop_timeout) if c_on else None,
        steps=[{"label": f"Stop containers ({detail})",
                "detail": f"stop timeout {c.stop_timeout}s"}] if c_on else []))

    # 3) Filesystem sync.
    fs = config.filesystems
    sync_on = local_active and fs.sync_enabled
    phases.append(_phase(
        "filesystem-sync", "Filesystem sync", enabled=sync_on,
        skipped=_local_skip(is_local, delegated, fs.sync_enabled),
        steps=[{"label": "Flush pending writes to disk (sync)"}] if sync_on else []))

    # 4) Filesystem unmount.
    um = fs.unmount
    um_on = local_active and um.enabled
    phases.append(_phase(
        "filesystem-unmount", "Filesystem unmount", enabled=um_on,
        skipped=_local_skip(is_local, delegated, um.enabled),
        steps=[_unmount_step(m) for m in (um.mounts or [])] if um_on else []))

    # 5) Remote servers — best-effort, runs regardless of local/delegated. Within
    #    a shutdown-order group they run in PARALLEL; groups run in order.
    enabled_servers = [s for s in config.remote_servers if s.enabled]
    ordered = sorted(enabled_servers, key=lambda s: (s.shutdown_order
                                                     if s.shutdown_order is not None else 0))
    rsteps = []
    for s in ordered:
        bits = [f"{(s.user + '@') if s.user else ''}{s.host}",
                s.shutdown_command or "shutdown"]
        if s.shutdown_order is not None:
            bits.append(f"order {s.shutdown_order}")
        if s.is_host_loopback:
            bits.append("host-loopback")
        rsteps.append({"label": s.name or s.host, "detail": " · ".join(bits),
                       "host": s.host, "order": s.shutdown_order,
                       "loopback": bool(s.is_host_loopback)})
    parallel = any(s.parallel for s in enabled_servers)
    timeouts = [float(s.command_timeout) for s in enabled_servers
                if getattr(s, "command_timeout", None)]
    remote_est = (max(timeouts) if parallel else sum(timeouts)) if timeouts else None
    phases.append(_phase(
        "remote", "Remote servers", mode="parallel" if parallel else "sequential",
        enabled=bool(enabled_servers),
        skipped=None if enabled_servers else "none configured",
        estimate_s=remote_est, steps=rsteps))

    # 6) Final filesystem sync (local, sync enabled, not delegated).
    final_on = is_local and fs.sync_enabled and not delegated
    phases.append(_phase(
        "final-sync", "Final filesystem sync", enabled=final_on,
        skipped=_local_skip(is_local, delegated, fs.sync_enabled),
        steps=[{"label": "Final sync before halt"}] if final_on else []))

    # 7) Terminal step — coordinator handoff, or the local host poweroff.
    if coordinator_mode:
        phases.append(_phase(
            "local-poweroff", "Group handoff", enabled=True,
            steps=[{"label": "Report group shutdown complete to the coordinator",
                    "detail": "the coordinator performs the host poweroff"}]))
    else:
        ls = config.local_shutdown
        po_on = ls.enabled and not delegated
        phases.append(_phase(
            "local-poweroff", "Local host poweroff", enabled=po_on,
            skipped=(None if po_on
                     else ("delegated to host" if delegated else "disabled")),
            steps=[{"label": "Power off this host", "detail": ls.command}] if po_on else []))

    note = None
    if delegated:
        note = ("Container loopback mode: VM / container / filesystem / poweroff "
                "actions run on the host via the host-loopback SSH target (see "
                "Remote servers), not in-process.")
    elif not is_local:
        note = ("Non-local UPS group: only remote-server shutdown runs; local "
                "VM / container / filesystem / poweroff phases belong to the host "
                "that owns this UPS.")
    elif coordinator_mode:
        note = ("Multi-UPS coordinator mode: this group reports completion to the "
                "coordinator, which performs the single host poweroff.")

    return {
        "dryRun": bool(getattr(config.behavior, "dry_run", False)),
        "delegated": bool(delegated),
        "isLocal": bool(is_local),
        "coordinatorMode": bool(coordinator_mode),
        "note": note,
        "phases": phases,
    }
