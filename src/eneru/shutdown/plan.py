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

__all__ = ["PHASE_ORDER", "build_shutdown_plan"]

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
                        coordinator_mode: bool = False,
                        reveal_commands: bool = True) -> Dict[str, Any]:
    """Build the structured shutdown plan from a (group-scoped) config.

    ``is_local`` / ``delegated`` / ``coordinator_mode`` are the runtime flags the
    monitor uses to gate phases; pass the monitor's actual values so the plan
    matches what that daemon would really do.

    ``reveal_commands`` controls whether raw shutdown commands (per-remote
    ``shutdown_command`` and the local poweroff command) appear in the output.
    Pass ``False`` for unauthenticated callers — those commands can embed
    sensitive flags/credentials, so they're redacted exactly like the config
    summary and the remote-health view do for anonymous reads.
    """
    hidden = "command hidden — sign in to view"
    local_active = is_local and not delegated
    phases: List[Dict[str, Any]] = []

    # 1) Virtual machines (libvirt).
    vm = config.virtual_machines
    vm_on = local_active and vm.enabled
    vm_wait = getattr(vm, "max_wait", None)
    phases.append(_phase(
        "vms", "Virtual machines", enabled=vm_on,
        skipped=_local_skip(is_local, delegated, vm.enabled),
        estimate_s=float(vm_wait) if (vm_on and vm_wait) else None,
        steps=[{"label": "Gracefully shut down running VMs (libvirt)",
                "detail": (f"up to {vm_wait}s each" if vm_wait else None)}]
        if vm_on else []))

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

    # 5) Remote servers — best-effort, runs regardless of local/delegated. This
    #    mirrors RemoteShutdownMixin._shutdown_remote_servers exactly: loopback
    #    pre-actions bracket FIRST, then regular remotes run grouped by EFFECTIVE
    #    shutdown_order (parallel only WITHIN a same-order group; groups run in
    #    sequence), then loopback shutdown commands run LAST. Estimate sums the
    #    sequential groups (max timeout within a parallel group).
    enabled_servers = [s for s in config.remote_servers if s.enabled]
    loopbacks = [s for s in enabled_servers if s.is_host_loopback is True]
    regulars = [s for s in enabled_servers if s.is_host_loopback is not True]

    def _remote_step(s, note):
        bits = [f"{(s.user + '@') if s.user else ''}{s.host}",
                (s.shutdown_command or "shutdown") if reveal_commands else hidden]
        if getattr(s, "command_timeout", None):
            bits.append(f"timeout {s.command_timeout}s")
        if note:
            bits.append(note)
        if s.is_host_loopback:
            bits.append("host-loopback")
        return {"label": s.name or s.host, "detail": " · ".join(bits),
                "host": s.host, "order": s.shutdown_order,
                "loopback": bool(s.is_host_loopback)}

    rsteps = []
    est = 0.0
    any_parallel = False
    # Loopback pre-actions run before the regulars.
    if any(getattr(lb, "pre_shutdown_commands", None) for lb in loopbacks):
        for lb in loopbacks:
            if getattr(lb, "pre_shutdown_commands", None):
                rsteps.append(_remote_step(lb, "pre-shutdown · runs first"))
    # Regular remotes, grouped by the SAME effective-order logic the executor uses.
    if regulars:
        from eneru.monitor import compute_effective_order
        groups = {}
        for order, s in compute_effective_order(regulars):
            groups.setdefault(order, []).append(s)
        for order in sorted(groups):
            members = groups[order]
            parallel_group = len(members) > 1
            any_parallel = any_parallel or parallel_group
            note = f"order {order}" + (" · ⇉ parallel" if parallel_group else "")
            for s in members:
                rsteps.append(_remote_step(s, note))
            gto = [float(s.command_timeout) for s in members
                   if getattr(s, "command_timeout", None)]
            if gto:
                est += max(gto) if parallel_group else sum(gto)
    # Loopback shutdown commands run after all regulars.
    for lb in loopbacks:
        rsteps.append(_remote_step(lb, "shutdown · runs last"))
        if getattr(lb, "command_timeout", None):
            est += float(lb.command_timeout)
    phases.append(_phase(
        "remote", "Remote servers",
        mode="parallel" if any_parallel else "sequential",
        enabled=bool(enabled_servers),
        skipped=None if enabled_servers else "none configured",
        estimate_s=(est or None), steps=rsteps))

    # 6) Final filesystem sync (local, sync enabled, not delegated).
    final_on = is_local and fs.sync_enabled and not delegated
    phases.append(_phase(
        "final-sync", "Final filesystem sync", enabled=final_on,
        skipped=_local_skip(is_local, delegated, fs.sync_enabled),
        steps=[{"label": "Final sync before halt"}] if final_on else []))

    # 7) Terminal step — coordinator handoff, or the local host poweroff.
    if coordinator_mode:
        # The coordinator performs the single host poweroff — but that is a
        # LOCAL-ownership action. A non-local (monitoring-only) group must NOT
        # show a host-poweroff handoff: losing a UPS that doesn't power this host
        # triggers nothing here. Gate it exactly like the other local phases.
        handoff_skip = _local_skip(is_local, delegated, True)
        handoff_on = handoff_skip is None
        phases.append(_phase(
            "local-poweroff", "Group handoff", enabled=handoff_on,
            skipped=handoff_skip,
            steps=[{"label": "Report group shutdown complete to the coordinator",
                    "detail": "the coordinator performs the host poweroff"}]
            if handoff_on else []))
    else:
        ls = config.local_shutdown
        # Host poweroff is a LOCAL-ownership action: gate it the same way as the
        # other local drain phases (a non-local group never powers off this host).
        po_skip = _local_skip(is_local, delegated, ls.enabled)
        po_on = po_skip is None
        phases.append(_phase(
            "local-poweroff", "Local host poweroff", enabled=po_on,
            skipped=po_skip,
            steps=[{"label": "Power off this host",
                    "detail": ls.command if reveal_commands else hidden}]
            if po_on else []))

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

    # Rough total: sum the per-phase estimates we have (sequential phases). Phases
    # without a known timeout (sync, poweroff) aren't counted, so it's a floor.
    total = sum(p["estimateS"] for p in phases
                if p["enabled"] and p["estimateS"] is not None)
    return {
        "dryRun": bool(getattr(config.behavior, "dry_run", False)),
        "delegated": bool(delegated),
        "isLocal": bool(is_local),
        "coordinatorMode": bool(coordinator_mode),
        "note": note,
        "totalEstimateS": total or None,
        "phases": phases,
    }
