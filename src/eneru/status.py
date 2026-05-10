"""Shared read-only status models for API, metrics, MQTT, and TUI."""

import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from eneru.config import Config, UPSGroupConfig
from eneru.remote_health import read_remote_health_sidecar, remote_health_sidecar_path
from eneru.stats import StatsStore


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
    return {
        "generatedAt": time.time(),
        "ups": [monitor_status(m) for m in monitors],
        "redundancyGroups": redundancy_group_statuses(source, config),
    }


def redundancy_group_statuses(source: Any, config: Optional[Config]) -> List[dict]:
    """Return status rows for redundancy groups configured on a coordinator."""
    if config is None:
        return []
    rows = []
    live_managers = {
        getattr(manager, "group_label", ""): manager
        for manager in getattr(source, "_redundancy_remote_health_managers", []) or []
    }
    for group in config.redundancy_groups:
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
            "isLocal": group.is_local,
            "remoteHealth": remote_health,
        })
    return rows


def readiness(source: Any) -> Dict[str, Any]:
    """Return readiness state from monitor snapshots."""
    rows = []
    for monitor in iter_monitors(source):
        config = monitor.config
        snap = monitor.state.snapshot()
        rows.append({
            "groupId": sanitize_name(config.ups.name),
            "name": config.ups.name,
            "label": config.ups.label,
            "connectionState": snap.connection_state,
            "lastUpdateTime": snap.last_update_time,
        })
    if not rows:
        return {"ready": False, "reason": "no monitors", "ups": []}
    failed = [
        row for row in rows
        if row["connectionState"] == "FAILED" or not row["lastUpdateTime"]
    ]
    return {
        "ready": not failed,
        "reason": "ready" if not failed else "monitoring visibility failed",
        "ups": rows,
    }


def _remote_server_summary(server: Any) -> Dict[str, Any]:
    """Return a sanitized remote-server configuration summary."""
    return {
        "name": server.name or server.host,
        "host": server.host,
        "user": server.user,
        "enabled": server.enabled,
        "shutdownOrder": server.shutdown_order,
        "hasPreShutdownCommands": bool(server.pre_shutdown_commands),
        "sshOptionsConfigured": bool(server.ssh_options),
    }


def config_summary(config: Config) -> Dict[str, Any]:
    """Return a sanitized configuration summary."""
    return {
        "ups": [
            {
                "groupId": sanitize_name(group.ups.name),
                "name": group.ups.name,
                "label": group.ups.label,
                "isLocal": group.is_local,
                "remoteServers": [
                    _remote_server_summary(s) for s in group.remote_servers
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
                    _remote_server_summary(s) for s in group.remote_servers
                ],
            }
            for group in config.redundancy_groups
        ],
        "api": {
            "enabled": config.api.enabled,
            "bind": config.api.bind,
            "port": config.api.port,
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
    }


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


def query_events(config: Config, *, limit: int = 100, verbosity: int = 2) -> List[dict]:
    """Return recent event rows from all per-UPS stats DBs."""
    rows: List[dict] = []
    now = int(time.time())
    limit = max(1, int(limit))
    verbosity = int(verbosity)
    include_types = POWER_EVENT_TYPES if verbosity == 0 else None
    exclude_types = LIFECYCLE_EVENT_TYPES if verbosity == 1 else None
    for group in config.ups_groups:
        conn = StatsStore.open_readonly(stats_db_path_for_group(config, group))
        if conn is None:
            continue
        try:
            store = StatsStore.from_connection(conn)
            for ts, event_type, detail in store.query_recent_events(
                end_ts=now,
                limit=limit,
                include_types=include_types,
                exclude_types=exclude_types,
            ):
                rows.append({
                    "ts": int(ts),
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
    rows.sort(key=lambda row: row["ts"])
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
