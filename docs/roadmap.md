# Roadmap

Planned direction for Eneru. None of this is built yet -- things will change as we go. Some features might get cut if they turn out to be impractical or nobody actually wants them.

Feature requests and feedback: [GitHub Issues](https://github.com/m4r1k/Eneru/issues).

Recently shipped: v5.0 (2026-04-11), v5.1 (2026-04-21), v5.2 (2026-04-24). See the [changelog](changelog.md) for details.

---

## v5.3 -- API and observability (planned)

Right now nothing can talk to Eneru programmatically. This version adds the plumbing for that.

- Read-only REST API: UPS status, time-series history, event log, config summary, health check
- Prometheus `/metrics` endpoint with Eneru-specific metrics (trigger states, depletion rate, connection flap count, group health, anomaly status)
- `/health` and `/ready` endpoints for Kubernetes probes and load balancers
- MQTT publishing to a configurable broker (Home Assistant, Node-RED, custom automation)
- Optional JSON log format for SIEM integration (Splunk, Elastic, Loki)
- Syslog forwarding (RFC 5424)
- Reference Grafana dashboard JSON

---

## v6.0 -- Web dashboard and UPS control (planned)

- Browser-based dashboard with UPS status, battery graphs, event timeline, and group overview. Served by the embedded API server, no external dependencies
- Authentication with local user accounts and API keys
- UPS control via NUT `upscmd`: battery self-tests, beeper, calibration
- Read/write UPS variables via NUT `upsrw` (sensitivity, transfer voltages)
- Config hot-reload via `SIGHUP` or API endpoint
- NUT server auto-discovery on the network

---

## v6.1 -- Battery intelligence and reporting (planned)

- Battery health score (0-100) from charge capacity, runtime under load, self-test results, anomaly history, and age
- Replacement prediction: trend the health score over time, alert when it projects below a threshold (default 90 days out)
- Built-in self-test scheduling via NUT `upscmd`, with result tracking
- Periodic reports (daily/weekly/monthly) via notification channels: power events, battery health, uptime
- Energy tracking: kWh from load data, with cost projection

---

## v7.0 -- Enterprise auth and integration (planned)

- RBAC: admin, operator, and viewer roles for the API and dashboard
- LDAP/Active Directory authentication with group-to-role mapping
- Audit log: append-only, tamper-detected record of all user actions
- SNMP trap generation (RFC 1628 standard traps plus Eneru-specific)
- VMware vCenter integration via native API, replacing SSH-based ESXi shutdown
- Kubernetes operator: cordon and drain nodes when UPS triggers fire, respecting PodDisruptionBudgets

---

## v7.1 -- Fleet management (planned)

- Agent-coordinator model: remote Eneru instances push status to a central coordinator over REST
- Central dashboard aggregating all managed instances
- Alerting escalation with PagerDuty and OpsGenie
- Config templates pushed from coordinator to agents

---

## v8.0 -- Advanced analytics (planned)

- Environmental sensor monitoring (temperature, humidity) from SNMP probes
- Power capacity trending: project when UPS load will hit capacity based on historical growth
- Compliance event export for SOX/HIPAA/ISO 27001 audit documentation
- Plugin system for custom triggers, actions, and data sources

---

## Version philosophy

| Range | Theme |
|-------|-------|
| v5.x | Foundation -- data, redundancy, observability |
| v6.x | Interface -- web dashboard, control, reporting |
| v7.x | Enterprise -- auth, compliance, fleet |
| v8.x | Intelligence -- analytics, environmental, extensibility |
