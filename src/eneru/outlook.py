"""Read-only "what happens next, and when?" model (UX round, v6.2).

ELI5: the car dashboard, not the engine. The engine (``monitor.py`` →
``_handle_on_battery`` and ``_main_loop``) decides when to shut down. This
module only READS the same inputs and draws the fuel gauge: every configured
shutdown trigger, how far each one is from firing, a rough "empty in N
minutes", and what pulling the handbrake would actually do for THIS UPS (power
off the host? only remote servers? nothing but a notification? a vote in a
redundancy group?).

Nothing here is ever called from a trigger decision. The comparison rules
below are copied from the trigger code and pinned to it by parity tests
(``tests/test_outlook.py``), so if the engine changes, the gauge's tests fail
instead of the gauge silently lying.

Three entry points serve the three surfaces:

- :func:`monitor_outlook` — daemon side, used by the API status rows;
- :func:`state_file_outlook` / :func:`redundancy_outlook_from_state_files` —
  the out-of-process TUI, built from the state files (no API needed);
- the pure pieces (:func:`evaluate_triggers`, :func:`ups_role`,
  :func:`trigger_action`, :func:`freshness`) for tests and reuse.

The JSON shapes are documented in ``docs/observability-api.md``.
"""

import math
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from eneru.health_model import RETRY_WAIT_SECONDS, UPSHealth, assess_health
from eneru.redundancy import effective_redundancy_health
from eneru.shutdown.progress import progress_sidecar_path, read_progress_sidecar
from eneru.state import HealthSnapshot
from eneru.utils import (
    SEVERITY_CRIT,
    SEVERITY_OK,
    SEVERITY_WARN,
    format_seconds,
    is_numeric,
    read_side_file,
    runs_coordinator,
    sanitize_name,
    status_has_token,
    status_summary,
    ups_state_file_path,
)

__all__ = [
    "TRIGGER_IDS",
    "describe_trigger_conditions",
    "evaluate_triggers",
    "freshness",
    "monitor_outlook",
    "progress_sidecar_path",
    "read_progress_sidecar",
    "read_self_test_failure_armed",
    "redundancy_group_outlook",
    "redundancy_outlook_from_state_files",
    "self_test_failure_armed",
    "stale_after_seconds",
    "state_file_outlook",
    "trigger_action",
    "ups_role",
]

# Evaluation order: FSD and FAILSAFE are checked by _main_loop before the
# on-battery handler; T1..T5 follow in _handle_on_battery's order.
TRIGGER_IDS = ("fsd", "failsafe", "lowBattery", "criticalRuntime",
               "depletionRate", "extendedTime", "selfTestFailure")

_LABELS = {
    "fsd": "UPS forced shutdown (FSD)",
    "failsafe": "Connection lost on battery",
    "lowBattery": "Low battery",
    "criticalRuntime": "Critical runtime",
    "depletionRate": "Fast battery drain",
    "extendedTime": "Time on battery",
    "selfTestFailure": "Failed self-test",
}

# Stats-DB meta keys the T5 trigger reads (monitor._handle_on_battery).
_META_SELF_TEST_FAILED = "self_test_failure_latched"
_META_SELF_TEST_OUTAGE = "self_test_failure_outage_start"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _num(value: Any) -> Optional[float]:
    """A finite, non-negative float reading, else None (NUT's -1 = unknown)."""
    if not is_numeric(value):
        return None
    number = float(value)
    return number if number >= 0 else None


def _int_or(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _fmt_pct(value: Any) -> str:
    number = float(value)
    return f"{number:.0f}%" if number == int(number) else f"{number:g}%"


def stale_after_seconds(check_interval: Any) -> int:
    """Age after which a poll is considered stale: max(3 polls, 30 s)."""
    return max(3 * max(1, _int_or(check_interval, 1)), 30)


def freshness(last_update_time: Any, *, check_interval: Any,
              now: Optional[float] = None,
              age_seconds: Optional[float] = None) -> Dict[str, Any]:
    """H4/H5: ``{lastPollAt, ageSeconds, staleAfterSeconds, stale}``.

    ``age_seconds`` lets the daemon pass a monotonic age (immune to NTP
    steps); otherwise the age is ``now - last_update_time``.
    """
    stale_after = stale_after_seconds(check_interval)
    last = float(last_update_time) if is_numeric(last_update_time) else 0.0
    if last <= 0:
        return {"lastPollAt": None, "ageSeconds": None,
                "staleAfterSeconds": stale_after, "stale": True}
    if age_seconds is None:
        age_seconds = (time.time() if now is None else now) - last
    age = max(0.0, float(age_seconds))
    return {"lastPollAt": round(last, 3), "ageSeconds": round(age, 1),
            "staleAfterSeconds": stale_after, "stale": age > stale_after}


def self_test_failure_armed(failed_raw: Any, failed_outage: Any, *,
                            attributed: bool, on_battery_since: Any) -> bool:
    """Mirror of T5's arming test in ``_handle_on_battery`` (sans the delay)."""
    try:
        failed_at = float(failed_raw) if failed_raw else 0.0
    except (TypeError, ValueError):
        failed_at = 0.0
    return bool(failed_at and not attributed
                and str(on_battery_since) != failed_outage)


def read_self_test_failure_armed(conn_or_store: Any, on_battery_since: Any,
                                 *, attributed: bool = False) -> bool:
    """T5 arming from a stats DB (a ``StatsStore`` or a read-only sqlite3
    connection, as the TUI opens). False on any error."""
    try:
        getter = getattr(conn_or_store, "get_meta", None)
        if getter is None:
            from eneru.stats import StatsStore
            getter = StatsStore.from_connection(conn_or_store).get_meta
        return self_test_failure_armed(
            getter(_META_SELF_TEST_FAILED), getter(_META_SELF_TEST_OUTAGE),
            attributed=attributed, on_battery_since=on_battery_since)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Pure trigger evaluation (mirrors monitor.py; parity-tested)
# ---------------------------------------------------------------------------

def _trigger(tid: str, *, enabled: bool = True, state: str,
             comparison: str, value: Any, threshold: Any, unit: str,
             margin: Any = None, eta: Optional[float] = None,
             eta_basis: Optional[str] = None, condition: str,
             text: str) -> Dict[str, Any]:
    return {
        "id": tid,
        "label": _LABELS[tid],
        "enabled": bool(enabled),
        "state": state,
        "comparison": comparison,
        "value": value,
        "threshold": threshold,
        "unit": unit,
        "margin": margin,
        "etaSeconds": (max(0, int(math.ceil(eta))) if eta is not None else None),
        "etaBasis": eta_basis if eta is not None else None,
        "condition": condition,
        "text": text,
    }


def describe_trigger_conditions(triggers: Any, *,
                                self_test_failure_armed: bool = False
                                ) -> List[str]:
    """Static human list of every configured trigger (Shutdown tab, notices)."""
    dep = triggers.depletion
    out = [
        f"charge below {_fmt_pct(triggers.low_battery_threshold)}",
        f"runtime below {format_seconds(triggers.critical_runtime_threshold)}",
        f"depletion above {float(dep.critical_rate):g}%/min "
        f"(after {format_seconds(dep.grace_period)} on battery)",
    ]
    if triggers.extended_time.enabled:
        out.append(f"{format_seconds(triggers.extended_time.threshold)} on battery")
    if self_test_failure_armed:
        out.append(
            "failed self-test: "
            f"{format_seconds(triggers.self_test_failure_shutdown_delay)} on battery")
    out.append("UPS forced shutdown (FSD)")
    out.append("connection lost while on battery")
    return out


def evaluate_triggers(triggers: Any, *, status: Any, battery_charge: Any,
                      runtime: Any, depletion_rate: Any = 0.0,
                      time_on_battery: Any = 0, connection_state: str = "OK",
                      self_test_failure_armed: bool = False,
                      failed_polls: Any = 0,
                      failed_poll_tolerance: Any = 3) -> Dict[str, Any]:
    """Every configured trigger's live state, closest first-to-fire, summary.

    Pure. See the module docstring and ``docs/observability-api.md`` for the
    returned shape. Comparison rules mirror ``_handle_on_battery`` (T1-T5) and
    ``_main_loop`` (FSD, FAILSAFE) exactly.

    ``failed_polls`` is the monitor's consecutive failed/stale NUT polls and
    ``failed_poll_tolerance`` its ``max_stale_data_tolerance``: FAILSAFE fires
    on the tolerance-th failure while on battery, so while the count climbs the
    row says how many polls are left and gets a clock ETA (F-180).
    """
    on_battery = status_has_token(status, "OB")
    tob = max(0, _int_or(time_on_battery, 0)) if on_battery else 0
    stab_delay = max(0, _int_or(triggers.on_battery_stabilization_delay, 0))
    stabilizing = on_battery and tob < stab_delay
    stab_left = (stab_delay - tob) if stabilizing else 0
    charge = _num(battery_charge)
    rt = _num(runtime)
    rate = float(depletion_rate) if is_numeric(depletion_rate) else 0.0
    rows: List[Dict[str, Any]] = []

    # FSD: acted on regardless of OB (main loop checks it first).
    fsd = status_has_token(status, "FSD")
    rows.append(_trigger(
        "fsd", state="fired" if fsd else "idle", comparison="flag",
        value=fsd, threshold=None, unit="", eta=0 if fsd else None,
        eta_basis="clock" if fsd else None,
        condition="UPS signals forced shutdown (FSD)",
        text="UPS is signalling FSD" if fsd else "not signalled"))

    # FAILSAFE: NUT lost (after the stale/connection tolerance) while OB.
    failsafe = on_battery and str(connection_state).upper() == "FAILED"
    failed = max(0, _int_or(failed_polls, 0))
    tolerance = max(1, _int_or(failed_poll_tolerance, 3))
    counting = on_battery and not failsafe and failed > 0
    if failsafe:
        fs_eta, fs_text = 0.0, "connection lost while on battery"
    elif counting:
        # One retry wait per remaining failed poll (a floor: a hung upsc adds
        # its own timeout on top).
        fs_eta = float(max(0, tolerance - failed) * RETRY_WAIT_SECONDS)
        fs_text = (f"{failed} of {tolerance} NUT polls failed · fires at "
                   f"{tolerance}")
    else:
        fs_eta, fs_text = None, "connection OK"
    rows.append(_trigger(
        "failsafe",
        state="fired" if failsafe else ("ok" if on_battery else "idle"),
        comparison="flag", value=failsafe, threshold=None, unit="",
        eta=fs_eta, eta_basis="clock" if fs_eta is not None else None,
        condition="connection to NUT lost while on battery",
        text=fs_text))

    def _held_state(met: bool) -> str:
        if not on_battery:
            return "idle"
        if not met:
            return "ok"
        return "held" if stabilizing else "fired"

    # T1 low battery: int(charge) < threshold.
    lb_thr = triggers.low_battery_threshold
    lb_cond = f"charge < {_fmt_pct(lb_thr)}"
    if charge is None:
        rows.append(_trigger(
            "lowBattery", state="unknown" if on_battery else "idle",
            comparison="below", value=None, threshold=lb_thr, unit="%",
            condition=lb_cond, text="battery charge not reported"))
    else:
        met = int(charge) < lb_thr
        eta = None
        basis = None
        if on_battery:
            if met:
                eta, basis = float(stab_left), "clock"
            elif rate > 0:
                eta = max((charge - lb_thr) / rate * 60.0, float(stab_left))
                basis = "depletion"
        rows.append(_trigger(
            "lowBattery", state=_held_state(met), comparison="below",
            value=round(charge, 1), threshold=lb_thr, unit="%",
            margin=round(charge - lb_thr, 1), eta=eta, eta_basis=basis,
            condition=lb_cond,
            text=f"{_fmt_pct(round(charge, 1))} now · fires below {_fmt_pct(lb_thr)}"))

    # T2 critical runtime: int(runtime) < threshold.
    rt_thr = triggers.critical_runtime_threshold
    rt_cond = f"runtime < {format_seconds(rt_thr)}"
    if rt is None:
        rows.append(_trigger(
            "criticalRuntime", state="unknown" if on_battery else "idle",
            comparison="below", value=None, threshold=rt_thr, unit="s",
            condition=rt_cond, text="runtime not reported"))
    else:
        met = int(rt) < rt_thr
        eta = None
        basis = None
        if on_battery:
            # Runtime counts down in real time while discharging.
            eta = max(float(int(rt) - rt_thr + 1), float(stab_left)) if not met \
                else float(stab_left)
            basis = "runtime" if not met else "clock"
        rows.append(_trigger(
            "criticalRuntime", state=_held_state(met), comparison="below",
            value=int(rt), threshold=rt_thr, unit="s",
            margin=int(rt) - rt_thr, eta=eta, eta_basis=basis,
            condition=rt_cond,
            text=f"{format_seconds(rt)} now · fires below {format_seconds(rt_thr)}"))

    # T3 depletion: rate > critical_rate AND tob >= grace (and not stabilizing).
    dep = triggers.depletion
    dep_thr = float(dep.critical_rate)
    grace = _int_or(dep.grace_period, 0)
    dep_cond = (f"drain > {dep_thr:g}%/min after "
                f"{format_seconds(grace)} on battery")
    met = rate > 0 and rate > dep_thr
    if not on_battery:
        dep_state, eta = "idle", None
    elif not met:
        dep_state, eta = "ok", None
    elif stabilizing or tob < grace:
        dep_state = "held"
        eta = float(max(stab_left, grace - tob))
    else:
        dep_state, eta = "fired", 0.0
    rows.append(_trigger(
        "depletionRate", state=dep_state, comparison="above",
        value=round(rate, 2), threshold=dep_thr, unit="%/min",
        margin=round(dep_thr - rate, 2), eta=eta,
        eta_basis="clock" if eta is not None else None, condition=dep_cond,
        text=f"{rate:g}%/min now · fires above {dep_thr:g}%/min"))

    # T4 extended time: tob > threshold (enabled), held while stabilizing.
    ext = triggers.extended_time
    ext_thr = _int_or(ext.threshold, 0)
    ext_cond = f"on battery > {format_seconds(ext_thr)}"
    if not ext.enabled:
        ext_state, eta = "disabled", None
    elif not on_battery:
        ext_state, eta = "idle", None
    elif tob > ext_thr:
        ext_state = "held" if stabilizing else "fired"
        eta = float(stab_left)
    else:
        ext_state, eta = "ok", float(max(ext_thr + 1 - tob, stab_left))
    rows.append(_trigger(
        "extendedTime", enabled=ext.enabled, state=ext_state,
        comparison="longer", value=tob, threshold=ext_thr, unit="s",
        margin=ext_thr - tob, eta=eta,
        eta_basis="clock" if eta is not None else None, condition=ext_cond,
        text=(f"{format_seconds(tob)} on battery · fires after "
              f"{format_seconds(ext_thr)}")))

    # T5 failed self-test: armed latch AND tob >= delay. No stabilization hold.
    st_delay = max(0, _int_or(triggers.self_test_failure_shutdown_delay, 0))
    st_cond = f"failed self-test and on battery ≥ {format_seconds(st_delay)}"
    if not self_test_failure_armed:
        st_state, eta = "disabled", None
    elif not on_battery:
        st_state, eta = "idle", None
    elif tob >= st_delay:
        st_state, eta = "fired", 0.0
    else:
        st_state, eta = "ok", float(st_delay - tob)
    rows.append(_trigger(
        "selfTestFailure", enabled=self_test_failure_armed, state=st_state,
        comparison="longer", value=tob, threshold=st_delay, unit="s",
        margin=st_delay - tob, eta=eta,
        eta_basis="clock" if eta is not None else None, condition=st_cond,
        text=("last self-test failed · fires after "
              f"{format_seconds(st_delay)} on battery"
              if self_test_failure_armed else "no failed self-test latched")))

    firing = [row["id"] for row in rows if row["state"] == "fired"]
    nxt = None
    if firing:
        nxt = next(row for row in rows if row["state"] == "fired")
    else:
        candidates = [row for row in rows
                      if row["etaSeconds"] is not None
                      and row["state"] in ("ok", "held")]
        if candidates:
            nxt = min(candidates, key=lambda row: row["etaSeconds"])
    if nxt is not None and nxt["state"] == "fired":
        summary = f"Shutdown condition met: {nxt['label'].lower()} ({nxt['text']})"
    elif not on_battery and not fsd:
        summary = "On mains: shutdown triggers are armed but idle"
    elif nxt is not None:
        summary = (f"Next: {nxt['label'].lower()} in about "
                   f"{format_seconds(nxt['etaSeconds'])} ({nxt['condition']})")
    else:
        summary = "On battery: no trigger is close"
    if stabilizing and not firing:
        summary += f"; stabilizing for {format_seconds(stab_left)}"
    return {
        "onBattery": on_battery,
        "timeOnBattery": tob,
        "stabilizing": stabilizing,
        "stabilizationRemaining": stab_left,
        "triggers": rows,
        "firing": firing,
        "next": nxt,
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# Role + action (H1 / M4): what does THIS UPS shut down?
# ---------------------------------------------------------------------------

def ups_role(config: Any, group: Any, *, redundancy_groups: Sequence[str] = (),
             in_redundancy: Optional[bool] = None,
             coordinator_mode: bool = False,
             coordinator_handoff: Optional[bool] = None,
             delegated: bool = False) -> Dict[str, Any]:
    """Classify a (group-scoped) config by the shutdown plan it would run.

    Built on :func:`eneru.shutdown.plan.build_shutdown_plan` so the badge and
    the Shutdown tab can never disagree.
    """
    from eneru.shutdown.plan import build_shutdown_plan
    is_local = bool(getattr(group, "is_local", False))
    plan = build_shutdown_plan(
        config, is_local=is_local, delegated=bool(delegated),
        coordinator_mode=bool(coordinator_mode),
        coordinator_handoff=coordinator_handoff, reveal_commands=False)
    enabled = {p["id"] for p in plan["phases"] if p["enabled"]}
    servers = [s for s in (getattr(config, "remote_servers", None) or [])
               if getattr(s, "enabled", False)]
    loopback = any(getattr(s, "is_host_loopback", False) is True for s in servers)
    regular = sum(1 for s in servers
                  if getattr(s, "is_host_loopback", False) is not True)
    local_poweroff = "local-poweroff" in enabled and (
        not coordinator_mode
        or bool(getattr(config.local_shutdown, "enabled", True)))
    host = bool(local_poweroff or loopback)
    drain = bool(enabled & {"vms", "containers", "filesystem-sync",
                            "filesystem-unmount"}) or (delegated and loopback)
    names = [str(n) for n in redundancy_groups]
    member = bool(names) if in_redundancy is None else bool(in_redundancy)
    # F-184: a member's own triggers are advisory; its UPS never runs its own
    # sequence (the redundancy evaluator does), so its role says "no own
    # actions" whatever resources the entry lists. The group decides.
    if member:
        kind = "redundancy-member"
        label = ("Redundancy member (" + ", ".join(names) + ")"
                 if names else "Redundancy member")
    elif host or drain:
        kind, label = "local", "Powers this host"
    elif regular:
        kind, label = "remote-only", "Remote shutdowns only"
    else:
        kind, label = "monitor-only", "Monitoring only"
    return {
        "kind": kind,
        "label": label,
        "shutsDownLocalHost": host,
        "localDrain": drain,
        "remoteServers": regular,
        "hasShutdownActions": bool(enabled) and not member,
        "redundancyGroups": names,
        "dryRun": bool(getattr(getattr(config, "behavior", None), "dry_run", False)),
    }


def member_status_summary(summary: Dict[str, Any],
                          role: Optional[Dict[str, Any]], *,
                          running: bool = False) -> Dict[str, Any]:
    """F-184: for a redundancy member, a fired trigger (or the UPS's FSD
    flag) is a vote, and the group decides. Keep the state, drop it to amber
    without blinking, and say so. A shutdown really ``running`` is shown as
    is."""
    if not role or role.get("kind") != "redundancy-member" or running:
        return summary
    if summary.get("state") not in ("trigger_active", "shutting_down"):
        return summary
    out = dict(summary)
    out.update({"label": "Trigger met, group decides",
                "severity": SEVERITY_WARN, "blink": False,
                "groupDecides": True})
    return out


def _servers_text(count: int) -> str:
    return f"{count} remote server{'s' if count != 1 else ''}"


def trigger_action(role: Dict[str, Any]) -> Dict[str, Any]:
    """What firing a trigger does for this UPS, in one line."""
    kind = role.get("kind")
    remotes = int(role.get("remoteServers") or 0)
    groups = list(role.get("redundancyGroups") or [])
    if kind == "redundancy-member":
        target = ", ".join(groups) if groups else "its redundancy group"
        action = {"kind": "redundancy-advisory",
                  "label": (f"Marks this UPS critical for redundancy group "
                            f"{target} (group decides)"),
                  "groups": groups}
    elif role.get("shutsDownLocalHost"):
        label = "Shuts down this host"
        if remotes:
            label += f" and {_servers_text(remotes)}"
        action = {"kind": "local-shutdown", "label": label, "groups": []}
    elif role.get("localDrain"):
        label = "Stops local workloads (this host stays up)"
        if remotes:
            label += f" and shuts down {_servers_text(remotes)}"
        action = {"kind": "local-shutdown", "label": label, "groups": []}
    elif remotes:
        action = {"kind": "remote-shutdown",
                  "label": f"Shuts down {_servers_text(remotes)}", "groups": []}
    else:
        action = {"kind": "notify-only",
                  "label": "Notification only — nothing is shut down here",
                  "groups": []}
    if role.get("dryRun"):
        action["label"] += " (dry-run)"
    return action


# ---------------------------------------------------------------------------
# Daemon side
# ---------------------------------------------------------------------------

def _monitor_redundancy_names(monitor: Any, source_config: Any) -> List[str]:
    name = monitor.config.ups.name
    groups = getattr(source_config, "redundancy_groups", None) or []
    return [rg.name for rg in groups if name in rg.ups_sources]


def _monitor_self_test_armed(monitor: Any) -> bool:
    store = getattr(monitor, "_stats_store", None)
    if store is None:
        return False
    return read_self_test_failure_armed(
        store, monitor.state.on_battery_start_time,
        attributed=bool(getattr(monitor, "_self_test_outage_attributed", False)))


def monitor_role(monitor: Any, source_config: Any = None) -> Dict[str, Any]:
    """:func:`ups_role` from a live monitor's own runtime flags."""
    config = monitor.config
    group = config.ups_groups[0] if config.ups_groups else None
    return ups_role(
        config, group,
        redundancy_groups=_monitor_redundancy_names(monitor, source_config),
        in_redundancy=bool(getattr(monitor, "_in_redundancy_group", False)),
        coordinator_mode=bool(getattr(monitor, "_coordinator_mode", False)),
        coordinator_handoff=getattr(monitor, "_coordinator_handoff", None),
        delegated=bool(getattr(monitor, "_uses_loopback_delegate", False)))


def monitor_outlook(monitor: Any, source_config: Any = None) -> Dict[str, Any]:
    """Daemon-side blocks for one monitor's API row.

    Returns ``{triggerOutlook, nextTrigger, role, freshness, statusSummary}``.
    Time on battery is advanced by the snapshot's age so an ETA keeps counting
    down between polls (display only).
    """
    snap = monitor.state.snapshot()
    config = monitor.config
    last_mono = getattr(snap, "last_update_mono", 0.0) or 0.0
    age = (time.monotonic() - last_mono) if last_mono > 0 else None
    fresh = freshness(snap.last_update_time,
                      check_interval=config.ups.check_interval,
                      age_seconds=age)
    tob = _int_or(snap.time_on_battery, 0)
    if status_has_token(snap.status, "OB") and fresh["ageSeconds"]:
        tob += int(fresh["ageSeconds"])
    outlook = evaluate_triggers(
        config.triggers, status=snap.status,
        battery_charge=snap.battery_charge, runtime=snap.runtime,
        depletion_rate=snap.depletion_rate, time_on_battery=tob,
        connection_state=snap.connection_state,
        self_test_failure_armed=_monitor_self_test_armed(monitor),
        failed_polls=max(_int_or(getattr(snap, "stale_data_count", 0), 0),
                         _int_or(getattr(monitor.state,
                                         "connection_error_count", 0), 0)),
        failed_poll_tolerance=getattr(config.ups, "max_stale_data_tolerance", 3))
    role = monitor_role(monitor, source_config)
    outlook["action"] = trigger_action(role)
    tracker = getattr(monitor, "_shutdown_progress", None)
    running = False
    if tracker is not None:
        try:
            running = tracker.snapshot().get("state") == "running"
        except Exception:
            running = False
    summary = member_status_summary(status_summary(
        snap.status, trigger_active=bool(snap.trigger_active),
        shutting_down=running, connection_state=snap.connection_state,
        stale=bool(fresh["stale"] and snap.last_update_time)), role,
        running=running)
    return {"triggerOutlook": outlook, "nextTrigger": outlook["next"],
            "role": role, "freshness": fresh, "statusSummary": summary}


# ---------------------------------------------------------------------------
# Redundancy groups (M9)
# ---------------------------------------------------------------------------

def _member_health_reason(raw: UPSHealth, snap: Any,
                          outlook: Dict[str, Any]) -> str:
    if raw == UPSHealth.UNKNOWN:
        if not snap.last_update_time:
            return "no data"
        if str(snap.connection_state).upper() == "FAILED":
            return "connection lost"
        return "stale data"
    if raw == UPSHealth.CRITICAL:
        if status_has_token(snap.status, "FSD"):
            return _LABELS["fsd"]
        if snap.trigger_active and snap.trigger_reason:
            return str(snap.trigger_reason)
        nxt = outlook.get("next")
        if nxt is not None and nxt["state"] == "fired":
            return f"{nxt['label']}: {nxt['text']}"
        return "shutdown trigger active"
    if raw == UPSHealth.DEGRADED:
        if status_has_token(snap.status, "OB"):
            return ("on battery (stabilizing)" if outlook.get("stabilizing")
                    else "on battery")
        return "connection unstable"
    return "healthy"


def redundancy_group_outlook(group: Any, member_snaps: Dict[str, Any],
                             raw_health: Dict[str, UPSHealth], *,
                             quorum_deferred: bool = False,
                             progress_state: Optional[str] = None,
                             role: Optional[Dict[str, Any]] = None,
                             now_age: Optional[Dict[str, float]] = None
                             ) -> Dict[str, Any]:
    """Group-level M9 additions from per-member snapshots + raw health.

    Returns ``{members: {name: {nextTrigger, healthReason}}, failingMembers,
    healthyMembers, failuresTolerated, healthyCount, quorumLost, outlook}``.
    """
    members: Dict[str, Dict[str, Any]] = {}
    healthy, failing = [], []
    for name in group.ups_sources:
        snap = member_snaps.get(name)
        raw = raw_health.get(name, UPSHealth.UNKNOWN)
        if snap is None:
            snap = HealthSnapshot(*([""] * 4), 0.0, 0, 0.0, "OK", False, "")
        tob = _int_or(snap.time_on_battery, 0)
        if now_age and status_has_token(snap.status, "OB"):
            tob += int(now_age.get(name) or 0)
        outlook = evaluate_triggers(
            group.triggers, status=snap.status,
            battery_charge=snap.battery_charge, runtime=snap.runtime,
            depletion_rate=snap.depletion_rate, time_on_battery=tob,
            connection_state=snap.connection_state)
        members[name] = {"nextTrigger": outlook["next"],
                         "healthReason": _member_health_reason(raw, snap, outlook)}
        if effective_redundancy_health(group, raw) == UPSHealth.HEALTHY:
            healthy.append(name)
        else:
            failing.append(name)
    tolerated = len(healthy) - int(group.min_healthy)
    quorum_lost = tolerated < 0 and not quorum_deferred
    action = trigger_action(role)["label"] if role else ""
    if progress_state == "running":
        state, sev, label = ("shutting-down", SEVERITY_CRIT,
                             "Group shutdown in progress")
    elif tolerated < 0 and quorum_deferred:
        state, sev, label = ("deferred", SEVERITY_WARN,
                             "Quorum decision deferred (members still starting)")
    elif quorum_lost:
        state, sev, label = ("quorum-lost", SEVERITY_CRIT,
                             "Quorum lost → group shutdown runs")
    elif tolerated == 0:
        state, sev, label = ("at-risk", SEVERITY_WARN,
                             "1 more failure → group shutdown")
    else:
        state = "healthy"
        sev = SEVERITY_WARN if failing else SEVERITY_OK
        label = f"Can lose {tolerated} more member{'s' if tolerated != 1 else ''}"
    return {
        "members": members,
        "failingMembers": failing,
        "healthyMembers": healthy,
        "failuresTolerated": tolerated,
        "healthyCount": len(healthy),
        "quorumLost": quorum_lost,
        "outlook": {"state": state, "severity": sev, "label": label,
                    "action": action},
    }


def redundancy_role(group: Any, base_config: Any,
                    executor: Any = None) -> Dict[str, Any]:
    """:func:`ups_role` for a redundancy group's own resources."""
    from eneru.config import Config, UPSGroupConfig
    if executor is not None and getattr(executor, "config", None) is not None:
        plan_config = executor.config
        delegated = bool(getattr(executor, "_uses_loopback_delegate", False))
    else:
        plan_config = Config(
            ups_groups=[UPSGroupConfig(
                remote_servers=list(group.remote_servers),
                virtual_machines=group.virtual_machines,
                containers=group.containers,
                filesystems=group.filesystems,
                is_local=group.is_local,
            )],
            behavior=base_config.behavior,
            local_shutdown=base_config.local_shutdown,
        )
        delegated = False
    role = ups_role(plan_config, plan_config.ups_groups[0],
                    in_redundancy=False, coordinator_mode=True,
                    coordinator_handoff=bool(group.is_local),
                    delegated=delegated)
    role["redundancyGroups"] = [group.name]
    return role


# ---------------------------------------------------------------------------
# TUI side: the same blocks from state files (no API, no daemon objects)
# ---------------------------------------------------------------------------

def _state_float(state: Dict[str, str], key: str, default: float = 0.0) -> float:
    value = state.get(key, "")
    return float(value) if is_numeric(value) else default


def _group_scoped_config(config: Any, group: Any) -> Any:
    from eneru.config import Config
    return Config(ups_groups=[group], behavior=config.behavior,
                  local_shutdown=config.local_shutdown)


def _state_file_path(config: Any, group: Any) -> Path:
    return Path(ups_state_file_path(config, group))


def state_file_outlook(config: Any, group: Any,
                       state: Optional[Dict[str, str]], *,
                       now: Optional[float] = None,
                       self_test_failure_armed: bool = False) -> Dict[str, Any]:
    """TUI twin of :func:`monitor_outlook`, from a parsed state file.

    ``state`` is ``tui.parse_state_file()``'s dict (``None`` when missing).
    Adds ``shutdownProgress`` (the §3.4 sidecar, or None).
    """
    now = time.time() if now is None else now
    state = state or {}
    epoch = _state_float(state, "EPOCH")
    check_interval = state.get("CHECK_INTERVAL") or group.ups.check_interval
    fresh = freshness(epoch, check_interval=check_interval, now=now)
    status = state.get("STATUS", "")
    tob = int(_state_float(state, "TIME_ON_BATTERY"))
    if status_has_token(status, "OB") and fresh["ageSeconds"]:
        tob += int(fresh["ageSeconds"])
    outlook = evaluate_triggers(
        group.triggers, status=status, battery_charge=state.get("BATTERY"),
        runtime=state.get("RUNTIME"),
        depletion_rate=_state_float(state, "DEPLETION_RATE"),
        time_on_battery=tob,
        self_test_failure_armed=bool(
            self_test_failure_armed
            and state.get("SELF_TEST_ATTRIBUTED", "0") != "1"))
    names = [rg.name for rg in getattr(config, "redundancy_groups", []) or []
             if group.ups.name in rg.ups_sources]
    handoff = bool(group.is_local) or (
        getattr(config.local_shutdown, "trigger_on", "") == "any"
        and not any(g.is_local for g in config.ups_groups))
    from eneru.runtime import _uses_loopback_delegate
    scoped = _group_scoped_config(config, group)
    coordinated = runs_coordinator(config)
    role = ups_role(
        scoped, group, redundancy_groups=names,
        coordinator_mode=coordinated,
        coordinator_handoff=handoff if coordinated else None,
        delegated=bool(_uses_loopback_delegate(scoped, group)))
    outlook["action"] = trigger_action(role)
    progress = read_progress_sidecar(
        progress_sidecar_path(_state_file_path(config, group)))
    running = bool(progress and progress.get("state") == "running")
    summary = member_status_summary(status_summary(
        status, trigger_active=state.get("TRIGGER_ACTIVE") == "1",
        shutting_down=running, stale=bool(fresh["stale"] and state)), role,
        running=running)
    return {"triggerOutlook": outlook, "nextTrigger": outlook["next"],
            "role": role, "freshness": fresh, "statusSummary": summary,
            "shutdownProgress": progress}


def _snapshot_from_state(state: Optional[Dict[str, str]]) -> HealthSnapshot:
    state = state or {}
    return HealthSnapshot(
        status=state.get("STATUS", ""),
        battery_charge=state.get("BATTERY", ""),
        runtime=state.get("RUNTIME", ""),
        load=state.get("LOAD", ""),
        depletion_rate=_state_float(state, "DEPLETION_RATE"),
        time_on_battery=int(_state_float(state, "TIME_ON_BATTERY")),
        last_update_time=_state_float(state, "EPOCH"),
        connection_state="OK",
        trigger_active=state.get("TRIGGER_ACTIVE") == "1",
        trigger_reason=state.get("TRIGGER_REASON", ""),
    )


def redundancy_outlook_from_state_files(config: Any, rg: Any, *,
                                        now: Optional[float] = None,
                                        states: Optional[Dict[str, Any]] = None
                                        ) -> Dict[str, Any]:
    """TUI parity for a redundancy group (M9), from member state files.

    Uses the evaluator's ``assess_health`` + ``effective_redundancy_health``
    on wall-clock ages (the TUI has no monotonic stamps). No cold-start hold.
    ``states`` (name -> parsed state dict) skips the file reads (tests).
    """
    now = time.time() if now is None else now
    groups = {g.ups.name: g for g in config.ups_groups}
    snaps: Dict[str, Any] = {}
    raw: Dict[str, UPSHealth] = {}
    ages: Dict[str, float] = {}
    for name in rg.ups_sources:
        group = groups.get(name)
        if states is not None:
            state = states.get(name)
        elif group is not None:
            state = _read_state_file(_state_file_path(config, group))
        else:
            state = None
        snap = _snapshot_from_state(state)
        snaps[name] = snap
        ages[name] = max(0.0, now - snap.last_update_time) if snap.last_update_time else 0.0
        if group is None or state is None:
            raw[name] = UPSHealth.UNKNOWN
            continue
        grace = group.ups.connection_loss_grace_period
        raw[name] = assess_health(
            snap, rg.triggers, group.ups.check_interval,
            max_stale_data_tolerance=group.ups.max_stale_data_tolerance,
            connection_grace_enabled=grace.enabled,
            connection_grace_duration=grace.duration, now=now)
    progress = read_progress_sidecar(progress_sidecar_path(Path(
        config.logging.state_file + f".redundancy-{sanitize_name(rg.name)}")))
    role = redundancy_role(rg, config)
    out = redundancy_group_outlook(
        rg, snaps, raw, role=role, now_age=ages,
        progress_state=(progress or {}).get("state"))
    out.update({"name": rg.name, "minHealthy": rg.min_healthy,
                "health": {n: h.value for n, h in raw.items()},
                "role": role, "shutdownProgress": progress})
    return out


def _read_state_file(path: Path) -> Optional[Dict[str, str]]:
    """KEY=VALUE state-file reader (same format tui.parse_state_file reads)."""
    try:
        text = read_side_file(path)
    except OSError:
        return None
    out: Dict[str, str] = {}
    for line in text.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            out[key.strip()] = value.strip()
    return out

