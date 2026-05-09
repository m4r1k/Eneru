"""Shared read-only status models for API, metrics, MQTT, and TUI."""

import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from eneru.config import Config, UPSGroupConfig
from eneru.remote_health import read_remote_health_sidecar, remote_health_sidecar_path
from eneru.stats import StatsStore


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
    return {
        "name": config.ups.name,
        "label": label,
        "displayName": config.ups.display_name,
        "isLocal": bool(getattr(group, "is_local", True)),
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
    return {
        "generatedAt": time.time(),
        "ups": [monitor_status(m) for m in monitors],
    }


def readiness(source: Any) -> Dict[str, Any]:
    """Return readiness state from monitor snapshots."""
    rows = [monitor_status(m) for m in iter_monitors(source)]
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


def config_summary(config: Config) -> Dict[str, Any]:
    """Return a sanitized configuration summary."""
    return {
        "ups": [
            {
                "name": group.ups.name,
                "label": group.ups.label,
                "isLocal": group.is_local,
                "remoteServers": [
                    {
                        "name": s.name or s.host,
                        "host": s.host,
                        "user": s.user,
                        "enabled": s.enabled,
                        "shutdownOrder": s.shutdown_order,
                        "hasPreShutdownCommands": bool(s.pre_shutdown_commands),
                        "sshOptionsConfigured": bool(s.ssh_options),
                    }
                    for s in group.remote_servers
                ],
            }
            for group in config.ups_groups
        ],
        "redundancyGroups": [
            {
                "name": group.name,
                "upsSources": list(group.ups_sources),
                "minHealthy": group.min_healthy,
                "isLocal": group.is_local,
                "remoteServers": [
                    {
                        "name": s.name or s.host,
                        "host": s.host,
                        "user": s.user,
                        "enabled": s.enabled,
                        "shutdownOrder": s.shutdown_order,
                        "hasPreShutdownCommands": bool(s.pre_shutdown_commands),
                        "sshOptionsConfigured": bool(s.ssh_options),
                    }
                    for s in group.remote_servers
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
    """Read remote health sidecars for all configured UPS groups."""
    rows: List[dict] = []
    for group in config.ups_groups:
        rows.extend(read_remote_health_sidecar(
            remote_health_sidecar_path(state_file_path_for_group(config, group))
        ))
    return rows


def query_events(config: Config, *, limit: int = 100) -> List[dict]:
    """Return recent event rows from all per-UPS stats DBs."""
    rows: List[dict] = []
    now = int(time.time())
    for group in config.ups_groups:
        conn = StatsStore.open_readonly(stats_db_path_for_group(config, group))
        if conn is None:
            continue
        try:
            store = StatsStore(Path(":memory:"))
            store._conn = conn
            try:
                for ts, event_type, detail in store.query_events(0, now):
                    rows.append({
                        "ts": int(ts),
                        "ups": group.ups.name,
                        "label": group.ups.label,
                        "eventType": event_type,
                        "detail": detail or "",
                    })
            finally:
                store._conn = None
        finally:
            try:
                conn.close()
            except Exception:
                pass
    rows.sort(key=lambda row: row["ts"])
    return rows[-max(1, int(limit)):]


def query_history(config: Config, ups_name: str, metric: str,
                  start: int, end: int) -> Optional[List[dict]]:
    """Return metric history for one UPS, or None when the UPS is unknown."""
    group = next((g for g in config.ups_groups
                  if g.ups.name == ups_name or sanitize_name(g.ups.name) == ups_name), None)
    if group is None:
        return None
    allowed = {
        "charge": "battery_charge",
        "runtime": "battery_runtime",
        "load": "ups_load",
        "voltage": "input_voltage",
        "depletion": "depletion_rate",
    }
    column = allowed.get(metric)
    if column is None:
        return []
    conn = StatsStore.open_readonly(stats_db_path_for_group(config, group))
    if conn is None:
        return []
    try:
        store = StatsStore(Path(":memory:"))
        store._conn = conn
        try:
            return [{"ts": int(ts), "value": value}
                    for ts, value in store.query_range(column, int(start), int(end))]
        finally:
            store._conn = None
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
