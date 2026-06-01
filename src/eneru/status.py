"""Shared read-only status models for API, metrics, MQTT, and TUI."""

import time
import shlex
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from eneru.config import Config, UPSGroupConfig
from eneru.health_model import UPSHealth, assess_health
from eneru.remote_health import (
    REMOTE_HEALTH_DISABLED,
    REMOTE_HEALTH_HEALTHY,
    REMOTE_HEALTH_UNKNOWN,
    read_remote_health_sidecar,
    remote_health_sidecar_path,
)
from eneru.stats import StatsStore
from eneru.utils import command_exists


HISTORY_METRICS = {
    "charge": "battery_charge",
    "runtime": "battery_runtime",
    "load": "ups_load",
    "voltage": "input_voltage",
    "depletion": "depletion_rate",
}
POWER_EVENT_TYPES = {
    "ON_BATTERY",
    "POWER_RESTORED",
    "EMERGENCY_SHUTDOWN_INITIATED",
    "SHUTDOWN_SEQUENCE_COMPLETE",
    "VOLTAGE_LOW",
    "VOLTAGE_HIGH",
    "BROWNOUT_DETECTED",
    "OVER_VOLTAGE_DETECTED",
    "BYPASS_MODE_ACTIVE",
    "OVERLOAD_ACTIVE",
    "OVERLOAD_DETECTED",
    "BATTERY_LOW",
    "FSD_DETECTED",
    "CONNECTION_LOST",
    "CONNECTION_RESTORED",
}
LIFECYCLE_EVENT_TYPES = {
    "DAEMON_START",
    "DAEMON_STOP",
    "DAEMON_RESTARTED",
    "DAEMON_UPGRADED",
    "DAEMON_RECOVERED",
    # Back-compat aliases from pre-release observability drafts.
    "SERVICE_STARTED",
    "SERVICE_STOPPED",
    "SERVICE_RESTARTED",
    "SERVICE_UPGRADED",
    "SERVICE_RECOVERED",
}


def sanitize_name(name: str) -> str:
    """Return the path-safe per-UPS identifier used by stats/state files."""
    return name.replace("@", "-").replace(":", "-").replace("/", "-")


def stats_db_path_for_group(config: Config, group: UPSGroupConfig) -> Path:
    """Return the stats DB path for a group."""
    stem = sanitize_name(group.ups.name) if config.multi_ups else "default"
    return Path(config.statistics.db_directory) / f"{stem}.db"


def state_file_path_for_group(config: Config, group: UPSGroupConfig) -> Path:
    """Return the state file path for a group."""
    if config.multi_ups:
        return Path(config.logging.state_file + f".{sanitize_name(group.ups.name)}")
    return Path(config.logging.state_file)


def redundancy_state_file_path(config: Config, group_name: str) -> Path:
    """Return the state path used by a redundancy-group executor."""
    return Path(config.logging.state_file + f".redundancy-{sanitize_name(group_name)}")


def iter_monitors(source: Any) -> List[Any]:
    """Return monitor-like objects from a single monitor or coordinator."""
    monitors = getattr(source, "_monitors", None)
    if monitors is not None:
        return list(monitors)
    return [source]


def monitor_status(monitor: Any) -> Dict[str, Any]:
    """Return one monitor's live status as a JSON-serializable dict."""
    config = monitor.config
    group = config.ups_groups[0] if config.ups_groups else None
    snap = monitor.state.snapshot()
    label = config.ups.label
    group_id = sanitize_name(config.ups.name)
    return {
        "groupId": group_id,
        "name": config.ups.name,
        "label": label,
        "displayName": config.ups.display_name,
        "isLocal": bool(getattr(group, "is_local", False)),
        "status": snap.status,
        "batteryCharge": snap.battery_charge,
        "runtime": snap.runtime,
        "load": snap.load,
        "powerQuality": {
            "inputVoltage": snap.input_voltage,
            "outputVoltage": snap.output_voltage,
            "batteryVoltage": snap.battery_voltage,
            "temperature": snap.ups_temperature,
            "inputFrequency": snap.input_frequency,
            "outputFrequency": snap.output_frequency,
            "voltageState": snap.voltage_state,
            "avrState": snap.avr_state,
            "bypassState": snap.bypass_state,
            "overloadState": snap.overload_state,
            # L13: these are Eneru-DERIVED thresholds, set by
            # _initialize_voltage_thresholds on the first successful poll. Before
            # that they hold dataclass defaults (230.0 / 0.0 / 0.0) that are
            # indistinguishable from real readings. Report null until a poll has
            # landed so consumers (Prometheus -> NaN, MQTT -> none) see "unknown"
            # rather than a fake 0V warning band.
            "nominalVoltage": (snap.nominal_voltage
                               if snap.last_update_time else None),
            "warningLow": (snap.voltage_warning_low
                           if snap.last_update_time else None),
            "warningHigh": (snap.voltage_warning_high
                            if snap.last_update_time else None),
        },
        "depletionRate": snap.depletion_rate,
        "timeOnBattery": snap.time_on_battery,
        "lastUpdateTime": snap.last_update_time,
        "connectionState": snap.connection_state,
        "triggerActive": snap.trigger_active,
        "triggerReason": snap.trigger_reason,
        "staleDataCount": snap.stale_data_count,
        "remoteHealth": remote_health_for_monitor(monitor),
    }


def collect_status(source: Any) -> Dict[str, Any]:
    """Collect all live UPS statuses from a source object."""
    monitors = iter_monitors(source)
    config = getattr(source, "config", None)
    payload: Dict[str, Any] = {
        "generatedAt": time.time(),
        "ups": [monitor_status(m) for m in monitors],
        "redundancyGroups": redundancy_group_statuses(source, config),
    }
    # v5.5: include the runtime context + loopback delegate state at the
    # top level so dashboards don't need to call /ready separately.
    if config is not None:
        health_rows = live_remote_health(source, config)
        loopback_row = _loopback_health_row(health_rows)
        payload["runtime"] = {
            "context": _runtime_context_label(),
            "loopbackDelegate": _loopback_runtime_summary(config, loopback_row),
        }
    return payload


def _map_redundancy_degraded(group: Any) -> UPSHealth:
    """Map DEGRADED through the group's quorum policy."""
    if getattr(group, "degraded_counts_as", "healthy") == "critical":
        return UPSHealth.CRITICAL
    return UPSHealth.HEALTHY


def _effective_redundancy_health(group: Any, raw: UPSHealth) -> UPSHealth:
    """Return how a raw UPS health tier contributes to group quorum."""
    if raw == UPSHealth.DEGRADED:
        return _map_redundancy_degraded(group)
    if raw == UPSHealth.UNKNOWN:
        policy = getattr(group, "unknown_counts_as", "critical")
        if policy == "healthy":
            return UPSHealth.HEALTHY
        if policy == "degraded":
            return _map_redundancy_degraded(group)
        return UPSHealth.CRITICAL
    return raw


def _redundancy_member_health(monitor: Any, group: Any) -> UPSHealth:
    """Assess one redundancy member using the same inputs as the evaluator."""
    snap = monitor.state.snapshot()
    ups_cfg = monitor.config.ups
    grace_cfg = ups_cfg.connection_loss_grace_period
    return assess_health(
        snap,
        group.triggers,
        ups_cfg.check_interval,
        max_stale_data_tolerance=ups_cfg.max_stale_data_tolerance,
        connection_grace_enabled=grace_cfg.enabled,
        connection_grace_duration=grace_cfg.duration,
    )


def redundancy_group_statuses(source: Any, config: Optional[Config]) -> List[dict]:
    """Return status rows for redundancy groups configured on a coordinator."""
    if config is None:
        return []
    rows = []
    monitors_by_name = {
        monitor.config.ups.name: monitor
        for monitor in iter_monitors(source)
    }
    live_managers = {
        getattr(manager, "group_label", ""): manager
        for manager in getattr(source, "_redundancy_remote_health_managers", []) or []
    }
    for group in config.redundancy_groups:
        members = []
        healthy_count = 0
        for ups_name in group.ups_sources:
            monitor = monitors_by_name.get(ups_name)
            raw = (
                _redundancy_member_health(monitor, group)
                if monitor is not None else UPSHealth.UNKNOWN
            )
            effective = _effective_redundancy_health(group, raw)
            if effective == UPSHealth.HEALTHY:
                healthy_count += 1
            members.append({
                "name": ups_name,
                "health": raw.value,
                "effectiveHealth": effective.value,
            })
        label = f"redundancy:{group.name}"
        manager = live_managers.get(label)
        remote_health = (
            manager.snapshot()
            if manager is not None
            else read_remote_health_sidecar(
                remote_health_sidecar_path(redundancy_state_file_path(config, group.name))
            )
        )
        rows.append({
            "groupId": f"redundancy-{sanitize_name(group.name)}",
            "name": group.name,
            "upsSources": list(group.ups_sources),
            "minHealthy": group.min_healthy,
            "healthyCount": healthy_count,
            "quorumLost": healthy_count < group.min_healthy,
            "members": members,
            "isLocal": group.is_local,
            "remoteHealth": remote_health,
        })
    return rows


# -----------------------------------------------------------------------------
# v5.5: capability matrix that drives /ready and dashboards.
#
# Required capabilities are derived from config (what the operator said the
# daemon must be able to do at power-loss time). Each capability is then
# scored "achievable" against the live state: NUT polling, host-binary
# presence (native install), or loopback-delegate health (containerized).
#
# Strict readiness semantics — Eneru is defense technology: ANY required
# capability that's not achievable returns /ready 503. Better to surface a
# broken shutdown contract loudly at every health probe than to fail at the
# most critical phase. The /ready payload still lists every capability and
# its individual achievability so operators can see exactly what works.
# -----------------------------------------------------------------------------

# (capability_id, host binary list — empty means "no binary needed at all")
_LOCAL_CAPABILITY_BINARIES: Dict[str, List[str]] = {
    "local_vm_teardown": ["virsh"],
    "local_container_teardown": ["docker", "podman"],  # either works
    "local_filesystem_unmount": ["umount"],
}


def _required_capabilities(config: Config) -> List[str]:
    """Compute the list of capability IDs this config requires at shutdown time."""
    caps = ["nut_polling"]
    for group in config.ups_groups:
        if not group.is_local:
            continue
        if group.virtual_machines.enabled:
            caps.append("local_vm_teardown")
        if group.containers.enabled:
            caps.append("local_container_teardown")
        if group.filesystems.unmount.enabled:
            caps.append("local_filesystem_unmount")
    for group in config.redundancy_groups:
        if not group.is_local:
            continue
        if group.virtual_machines.enabled:
            caps.append("local_vm_teardown")
        if group.containers.enabled:
            caps.append("local_container_teardown")
        if group.filesystems.unmount.enabled:
            caps.append("local_filesystem_unmount")
    has_local = any(g.is_local for g in config.ups_groups) or any(
        g.is_local for g in config.redundancy_groups
    )
    if config.local_shutdown.enabled and (
        has_local or not config.ups_groups or config.local_shutdown.trigger_on == "any"
    ):
        caps.append("local_host_poweroff")
    remote_targets: List[Tuple[str, str]] = []
    for group in config.ups_groups:
        for s in group.remote_servers:
            if s.enabled and s.is_host_loopback is not True:
                remote_targets.append((group.ups.label, s.name or s.host))
    for group in config.redundancy_groups:
        for s in group.remote_servers:
            if s.enabled and s.is_host_loopback is not True:
                remote_targets.append((group.name, s.name or s.host))
    target_counts: Dict[str, int] = {}
    for _group_label, target in remote_targets:
        target_counts[target] = target_counts.get(target, 0) + 1
    for group_label, target in remote_targets:
        caps.append(
            f"remote_server_shutdown[{_remote_capability_target(group_label, target, target_counts)}]"
        )
    # Deduplicate while preserving order
    seen = set()
    deduped = []
    for c in caps:
        if c not in seen:
            seen.add(c)
            deduped.append(c)
    return deduped


def _remote_capability_target(
    group_label: str,
    target: str,
    target_counts: Dict[str, int],
) -> str:
    """Return the public readiness target id for a remote server.

    Unique names keep the historic ``remote_server_shutdown[nas]`` shape.
    Duplicate names are scoped with the owning group so one failed ``nas``
    cannot be hidden by another healthy ``nas`` in a different group.
    """
    if target_counts.get(target, 0) <= 1:
        return target
    return f"{group_label}/{target}"


def _loopback_health_row(remote_health_rows: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Find the is_host_loopback row in a merged health snapshot, or None."""
    for row in remote_health_rows:
        if row.get("is_host_loopback"):
            return row
    return None


def _capability_achievable(
    cap: str,
    *,
    runtime_label: str,
    nut_ready: bool,
    loopback_status: Optional[str],
    remote_health_by_target: Dict[str, str],
    local_shutdown_command: str = "",
) -> Tuple[bool, str]:
    """Return ``(achievable, reason)`` for one capability under the live state.

    ``reason`` is the empty string when achievable; otherwise a short
    operator-actionable explanation.
    """
    from eneru.cli import _is_container_runtime  # local import to avoid cycle

    if cap == "nut_polling":
        return (nut_ready, "" if nut_ready else "NUT monitoring not connected")

    if cap.startswith("remote_server_shutdown["):
        target = cap[len("remote_server_shutdown["):-1]
        status = remote_health_by_target.get(target)
        if status is None or status in (REMOTE_HEALTH_UNKNOWN, REMOTE_HEALTH_DISABLED):
            # Probes disabled or not yet run → treat as achievable; the SSH
            # path itself is the source of truth at shutdown time.
            return True, ""
        if status == REMOTE_HEALTH_HEALTHY:
            return True, ""
        return False, f"remote target '{target}' health is {status}"

    # local_* capabilities
    in_container = _is_container_runtime(runtime_label)
    if in_container:
        if loopback_status is None:
            return False, (
                "container runtime detected but no is_host_loopback delegate "
                "is configured; see docs/install-comparison.md"
            )
        if loopback_status == REMOTE_HEALTH_HEALTHY:
            return True, ""
        return False, (
            f"loopback delegate health is {loopback_status} — local actions "
            "cannot be executed on the host"
        )
    # Native install: check binary presence
    if cap == "local_host_poweroff":
        try:
            parts = shlex.split(local_shutdown_command)
        except ValueError as exc:
            return False, f"invalid local shutdown command: {exc}"
        # Strip sudo and its option flags so the candidate is the real
        # privileged binary. Stripping only `sudo -n` (CodeRabbit #2)
        # caused `sudo -u root shutdown` to score `-u` as the binary
        # and report a false 503. The flag set covers every sudo
        # option that takes a separate argument; the loop bails at
        # `--` or at the first non-flag token.
        if parts and parts[0] in ("sudo", "/usr/bin/sudo"):
            sudo_flags_with_arg = {
                "-u", "--user",
                "-g", "--group",
                "-h", "--host",
                "-p", "--prompt",
                "-r", "--role",
                "-t", "--type",
                "-C", "--close-from",
                "-D", "--chdir",
                "-T", "--command-timeout",
                "-U", "--other-user",
            }
            idx = 1
            while idx < len(parts):
                token = parts[idx]
                if token == "--":
                    idx += 1
                    break
                if not token.startswith("-"):
                    break
                if token in sudo_flags_with_arg and idx + 1 < len(parts):
                    idx += 2
                else:
                    idx += 1
            parts = parts[idx:]
        candidates = [parts[0]] if parts else []
    else:
        candidates = _LOCAL_CAPABILITY_BINARIES.get(cap, [])
    if not candidates:
        return True, ""  # no binary needed
    for binary in candidates:
        if command_exists(binary):
            return True, ""
    return False, (
        f"host binary missing for configured capability: required one of "
        f"{candidates}"
    )


def _runtime_context_label() -> str:
    """Detect runtime context — local import keeps the cycle quiet."""
    from eneru.cli import _detect_runtime_context
    return _detect_runtime_context()


def readiness(source: Any) -> Dict[str, Any]:
    """Return readiness state from monitor snapshots + capability matrix.

    v5.5 extends the legacy "NUT connected?" check with the v5.5 capability
    matrix. ``/ready`` returns 503 when ANY required capability is
    unachievable — see _capability_achievable() for the per-runtime rules.
    """
    rows = []
    config = getattr(source, "config", None)
    nut_failed_any = False
    nut_visible_any = False
    for monitor in iter_monitors(source):
        monitor_config = monitor.config
        snap = monitor.state.snapshot()
        row = {
            "groupId": sanitize_name(monitor_config.ups.name),
            "name": monitor_config.ups.name,
            "label": monitor_config.ups.label,
            "connectionState": snap.connection_state,
            "lastUpdateTime": snap.last_update_time,
        }
        rows.append(row)
        if snap.connection_state == "FAILED" or not snap.last_update_time:
            nut_failed_any = True
        else:
            nut_visible_any = True
    if not rows or config is None:
        return {
            "ready": False, "reason": "no monitors", "reasons": ["no monitors"],
            "ups": [], "capabilities": [],
        }

    # Build merged remote-health snapshot for capability scoring.
    health_rows = live_remote_health(source, config)
    loopback_row = _loopback_health_row(health_rows)
    loopback_status = loopback_row.get("status") if loopback_row else None
    health_targets = [
        (row.get("group") or "", row.get("server") or row.get("host"))
        for row in health_rows
        if not row.get("is_host_loopback") and (row.get("server") or row.get("host"))
    ]
    health_counts: Dict[str, int] = {}
    for _group_label, target in health_targets:
        health_counts[target] = health_counts.get(target, 0) + 1
    remote_health_by_target: Dict[str, str] = {}
    for row in health_rows:
        if row.get("is_host_loopback"):
            continue
        name = row.get("server") or row.get("host")
        status = row.get("status")
        if name and status:
            remote_health_by_target[name] = status
            group_name = row.get("group") or ""
            remote_health_by_target[
                _remote_capability_target(group_name, name, health_counts)
            ] = status

    nut_ready = nut_visible_any and not nut_failed_any
    runtime_label = _runtime_context_label()
    # Local import (same cycle reason as _capability_achievable above).
    from eneru.cli import _is_container_runtime

    capabilities: List[Dict[str, Any]] = []
    reasons: List[str] = []
    for cap in _required_capabilities(config):
        ok, reason = _capability_achievable(
            cap,
            runtime_label=runtime_label,
            nut_ready=nut_ready,
            loopback_status=loopback_status,
            remote_health_by_target=remote_health_by_target,
            local_shutdown_command=config.local_shutdown.command,
        )
        capabilities.append({
            "id": cap,
            "achievable": ok,
            "reason": reason,
        })
        if not ok and reason:
            reasons.append(f"{cap}: {reason}")

    ready = not reasons
    return {
        "ready": ready,
        # Legacy single-string reason for back-compat consumers.
        "reason": "ready" if ready else "; ".join(reasons),
        "reasons": reasons,
        "runtime": {
            "context": runtime_label,
            # Use the canonical container predicate so the runtime
            # flag stays consistent with the capability scoring above
            # (which already uses _is_container_runtime). A bare
            # startswith("container") could disagree on non-Docker
            # container labels.
            "container": _is_container_runtime(runtime_label),
            "loopbackDelegate": _loopback_runtime_summary(config, loopback_row),
        },
        "capabilities": capabilities,
        "ups": rows,
    }


def _loopback_runtime_summary(
    config: Config, loopback_row: Optional[Dict[str, Any]]
) -> Dict[str, Any]:
    """Return the loopback section of the readiness/status runtime payload."""
    # Find the configured loopback (any group)
    configured = None
    for group in config.ups_groups:
        for s in group.remote_servers:
            if s.enabled and s.is_host_loopback is True:
                configured = s
                break
        if configured:
            break
    if configured is None:
        for group in config.redundancy_groups:
            for s in group.remote_servers:
                if s.enabled and s.is_host_loopback is True:
                    configured = s
                    break
            if configured:
                break
    if configured is None:
        return {"configured": False}
    out: Dict[str, Any] = {
        "configured": True,
        "host": configured.host,
        "user": configured.user,
    }
    if loopback_row is not None:
        out["status"] = loopback_row.get("status")
        out["lastChecked"] = loopback_row.get("last_checked_at")
        out["lastError"] = loopback_row.get("last_error") or ""
    return out


def _remote_server_summary(server: Any, *, extended: bool = False) -> Dict[str, Any]:
    """Return a sanitized remote-server configuration summary.

    ``extended`` adds structural detail (counts, margins) for authenticated
    callers — never secrets. Raw pre-shutdown commands stay hidden in both modes
    because they can embed credentials in their arguments.
    """
    out = {
        "name": server.name or server.host,
        "host": server.host,
        "user": server.user,
        "enabled": server.enabled,
        "shutdownOrder": server.shutdown_order,
        "hasPreShutdownCommands": bool(server.pre_shutdown_commands),
        "sshOptionsConfigured": bool(server.ssh_options),
        # v5.5: flag the host-loopback delegate so dashboards / TUI can
        # render it differently from regular remote_servers.
        "isHostLoopback": bool(getattr(server, "is_host_loopback", False)),
    }
    if extended:
        out["preShutdownCommandCount"] = len(server.pre_shutdown_commands or [])
        out["shutdownSafetyMargin"] = getattr(server, "shutdown_safety_margin", None)
    return out


def config_summary(config: Config, *, extended: bool = False) -> Dict[str, Any]:
    """Return a configuration summary.

    Anonymous callers get the sanitized view (the v5.3 shape). Authenticated
    callers (``extended=True``) get additional structural detail — still no
    passwords, hashes, tokens, or raw commands.
    """
    summary = {
        "ups": [
            {
                "groupId": sanitize_name(group.ups.name),
                "name": group.ups.name,
                "label": group.ups.label,
                "isLocal": group.is_local,
                "remoteServers": [
                    _remote_server_summary(s, extended=extended)
                    for s in group.remote_servers
                ],
            }
            for group in config.ups_groups
        ],
        "redundancyGroups": [
            {
                "groupId": f"redundancy-{sanitize_name(group.name)}",
                "name": group.name,
                "upsSources": list(group.ups_sources),
                "minHealthy": group.min_healthy,
                "isLocal": group.is_local,
                "remoteServers": [
                    _remote_server_summary(s, extended=extended)
                    for s in group.remote_servers
                ],
            }
            for group in config.redundancy_groups
        ],
        "api": {
            "enabled": config.api.enabled,
            "bind": config.api.bind,
            "port": config.api.port,
            "auth": {
                "enabled": config.api.auth.enabled,
                "requireForReads": config.api.auth.require_for_reads,
            },
        },
        "prometheus": {"enabled": config.prometheus.enabled},
        "remoteHealth": {
            "enabled": config.remote_health.enabled,
            "startupCheck": config.remote_health.startup_check,
            "interval": config.remote_health.interval,
            "failureThreshold": config.remote_health.failure_threshold,
        },
        "mqtt": {
            "enabled": config.mqtt.enabled,
            "brokerConfigured": bool(config.mqtt.broker),
            "topicPrefix": config.mqtt.topic_prefix,
            "publishInterval": config.mqtt.publish_interval,
        },
        "notifications": {
            "enabled": config.notifications.enabled,
            "serviceCount": len(config.notifications.urls),
        },
        "nutControl": {"enabled": config.nut_control.enabled},
        "detail": "extended" if extended else "sanitized",
    }
    if extended:
        # Structure, not secrets: the allowlists help the dashboard render the
        # control surface. Credentials are never included.
        summary["nutControl"]["allowedCommands"] = list(
            config.nut_control.allowed_commands)
        summary["nutControl"]["allowedVariables"] = list(
            config.nut_control.allowed_variables)
    return summary


def remote_health_for_monitor(monitor: Any) -> List[dict]:
    """Return remote health rows for one monitor."""
    manager = getattr(monitor, "_remote_health_manager", None)
    if manager is not None:
        return manager.snapshot()
    sidecar = getattr(monitor, "_remote_health_path", None)
    return read_remote_health_sidecar(sidecar) if sidecar else []


def remote_health_for_config(config: Config) -> List[dict]:
    """Read remote health sidecars for all configured UPS and redundancy groups."""
    rows: List[dict] = []
    for group in config.ups_groups:
        rows.extend(read_remote_health_sidecar(
            remote_health_sidecar_path(state_file_path_for_group(config, group))
        ))
    for group in config.redundancy_groups:
        rows.extend(read_remote_health_sidecar(
            remote_health_sidecar_path(redundancy_state_file_path(config, group.name))
        ))
    return rows


def live_remote_health(source: Any, config: Config) -> List[dict]:
    """Aggregate remote-health rows from in-process managers, falling back to sidecars.

    Centralises the lookup that the API's ``/api/v1/remote-health``
    endpoint and any future in-daemon consumer needs. Looks at four
    surfaces in order:

    1. The single-UPS source's own ``_remote_health_manager`` (when the
       API is attached directly to a ``UPSGroupMonitor``, not a
       ``MultiUPSCoordinator``).
    2. Each per-UPS monitor's ``_remote_health_manager`` (multi-UPS
       coordinator path — iterates ``source._monitors``).
    3. Redundancy-group managers held on the multi-UPS coordinator.
    4. On-disk sidecars written by steps (1) - (3), used when the
       caller is the read-only API process running outside the daemon
       (or before the managers have published their first snapshot).

    Returning an empty list when nothing is configured is intentional.
    """
    rows: List[dict] = []
    # Single-UPS source case: the source itself is the monitor and
    # holds the manager directly. Without this lookup the live snapshot
    # is invisible to the API in single-UPS deployments and we fall
    # back to the on-disk sidecar (stale until the next write tick).
    own_manager = getattr(source, "_remote_health_manager", None)
    if own_manager is not None:
        rows.extend(own_manager.snapshot())
    for monitor in getattr(source, "_monitors", []) or []:
        manager = getattr(monitor, "_remote_health_manager", None)
        if manager is not None:
            rows.extend(manager.snapshot())
    for manager in getattr(source, "_redundancy_remote_health_managers", []) or []:
        rows.extend(manager.snapshot())
    if not rows:
        rows = remote_health_for_config(config)
    return rows


def query_events(config: Config, *, limit: int = 100, verbosity: int = 2,
                 start_ts: Optional[int] = None, end_ts: Optional[int] = None,
                 before_ts: Optional[int] = None,
                 before_cursor: Optional[tuple] = None) -> List[dict]:
    """Return recent event rows aggregated from all per-UPS stats DBs.

    Each row carries a **source-qualified identity** — ``source`` (the UPS
    groupId) plus ``id`` (the per-DB row id) — because the id is unique only
    within one per-UPS DB. ``start_ts``/``end_ts`` bound the window for wide-range
    viewing. ``before_cursor`` is the "load older" cursor ``(ts, source, id)``;
    the timestamp-only ``before_ts`` path stays supported for older clients. Rows
    are ordered by ``(ts, source, id)`` so the merge across sources is
    deterministic and paging can progress through many same-second rows.
    """
    rows: List[dict] = []
    now = int(time.time())
    limit = max(1, int(limit))
    verbosity = int(verbosity)
    cursor_ts, cursor_source, cursor_id = before_cursor or (None, None, None)
    end = int(cursor_ts if cursor_ts is not None else before_ts) \
        if (cursor_ts is not None or before_ts is not None) else \
        (int(end_ts) if end_ts is not None else now)
    include_types = POWER_EVENT_TYPES if verbosity == 0 else None
    exclude_types = LIFECYCLE_EVENT_TYPES if verbosity == 1 else None
    for group in config.ups_groups:
        source = sanitize_name(group.ups.name)
        local_end = end
        before_id = None
        if cursor_ts is not None:
            if source < cursor_source:
                local_end = int(cursor_ts)
            elif source == cursor_source:
                local_end = int(cursor_ts)
                before_id = int(cursor_id)
            else:
                local_end = int(cursor_ts) - 1
        conn = StatsStore.open_readonly(stats_db_path_for_group(config, group))
        if conn is None:
            continue
        try:
            store = StatsStore.from_connection(conn)
            for event_id, ts, event_type, detail in store.query_recent_events(
                end_ts=local_end,
                limit=limit,
                start_ts=start_ts,
                include_types=include_types,
                exclude_types=exclude_types,
                include_id=True,
                before_id=before_id,
            ):
                rows.append({
                    "ts": int(ts),
                    "id": int(event_id),
                    "source": source,
                    "ups": group.ups.name,
                    "label": group.ups.label,
                    "eventType": event_type,
                    "detail": detail or "",
                })
        finally:
            try:
                conn.close()
            except Exception:
                pass
    rows.sort(key=lambda row: (row["ts"], row["source"], row["id"]))
    return rows[-limit:]


def query_history(config: Config, ups_name: str, metric: str,
                  start: int, end: int) -> Optional[List[dict]]:
    """Return metric history, or None for an unknown UPS or metric."""
    group = next((g for g in config.ups_groups
                  if g.ups.name == ups_name or sanitize_name(g.ups.name) == ups_name), None)
    if group is None:
        return None
    column = HISTORY_METRICS.get(metric)
    if column is None:
        return None
    conn = StatsStore.open_readonly(stats_db_path_for_group(config, group))
    if conn is None:
        return []
    try:
        store = StatsStore.from_connection(conn)
        return [{"ts": int(ts), "value": value}
                for ts, value in store.query_range(column, int(start), int(end))]
    finally:
        try:
            conn.close()
        except Exception:
            pass


def find_status(status_payload: Dict[str, Any], ups_name: str) -> Optional[dict]:
    """Find one UPS row in a collected status payload."""
    for row in status_payload.get("ups", []):
        if row["name"] == ups_name or sanitize_name(row["name"]) == ups_name:
            return row
    return None
