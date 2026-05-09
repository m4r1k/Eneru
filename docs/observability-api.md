# Observability and API

Eneru v5.3 adds read-only observability endpoints and outbound integrations. Nothing on this page can trigger shutdown or run UPS/SSH commands.

## API server

The API starts with `eneru run` when enabled:

```yaml
api:
  enabled: true
  bind: "127.0.0.1"
  port: 9100
```

Endpoints:

| Endpoint | Purpose |
|----------|---------|
| `/health` | API process is alive |
| `/ready` | Monitoring has usable UPS visibility |
| `/api/v1/ups` | Current UPS/group status |
| `/api/v1/ups/<name>` | One UPS status |
| `/api/v1/ups/<name>/history` | SQLite metric history |
| `/api/v1/events` | Recent event rows |
| `/api/v1/config` | Sanitized config summary |
| `/api/v1/remote-health` | Remote SSH health status |
| `/metrics` | Prometheus text metrics |

The default bind address is localhost because v5.3 has no auth layer. Keep it behind SSH, a local reverse proxy, or a trusted network boundary.

UPS rows include a stable `groupId` derived from the configured UPS name. Multi-UPS responses also include `redundancyGroups` rows with their source UPS names, quorum target, locality flag, and remote-health rows.

`/api/v1/events` accepts `limit` and `verbosity` query parameters. `verbosity=0` returns power/shutdown events, `verbosity=1` also includes lifecycle events, and `verbosity=2` returns all recorded events.

## Health and readiness

`/health` returns 200 when the API server can answer.

`/ready` returns 200 only when monitoring has usable UPS data. It returns 503 if the daemon has not published a snapshot yet or UPS visibility is failed.

## Prometheus

Prometheus metrics are served from the same API port:

```yaml
prometheus:
  enabled: true
```

Useful metric names include:

| Metric | Meaning |
|--------|---------|
| `eneru_up` | API serving metrics |
| `eneru_ups_battery_charge` | Battery percentage |
| `eneru_ups_runtime_seconds` | UPS runtime estimate |
| `eneru_ups_load_percent` | UPS load |
| `eneru_ups_depletion_rate_percent_per_minute` | Eneru depletion-rate calculation |
| `eneru_ups_connection_failed` | UPS visibility failed |
| `eneru_ups_trigger_active` | Shutdown trigger active/advisory |
| `eneru_remote_health_status` | Last remote health state |

`examples/grafana-dashboard.json` is a starting dashboard for these metrics.

## Remote SSH health

Remote healthchecks run a separate harmless probe command, default `true`.

```yaml
remote_health:
  enabled: true
  startup_check: true
  interval: 3600
  probe_command: "true"
  failure_threshold: 2
```

Healthchecks never execute `pre_shutdown_commands`, VM/container shutdown commands, custom commands, or `shutdown_command`. Remote health is advisory: during a real shutdown sequence Eneru still attempts the configured remote command chain with bounded timeouts. Failed or unreachable remote targets are reported in the remote shutdown summary and do not block later shutdown phases indefinitely.

## MQTT

MQTT publishing is outbound only and disabled by default:

```yaml
mqtt:
  enabled: false
  broker: "mqtt://192.0.2.10:1883"
  topic_prefix: "eneru"
  publish_interval: 10
```

MQTT publishes when the status payload changes and also republishes at `publish_interval` while unchanged. Debian and RPM packages install the MQTT client dependency. Install the optional dependency when using PyPI:

```bash
uv pip install "eneru[mqtt]"
```

No inbound MQTT commands are supported in v5.3.

## JSON logs and syslog

Use JSON logs for SIEM pipelines:

```yaml
logging:
  format: "json"
```

Forward logs to syslog:

```yaml
logging:
  syslog:
    enabled: true
    address: "/dev/log"
    facility: "daemon"
```

The existing local power-event `logger -t eneru` compatibility path remains.
