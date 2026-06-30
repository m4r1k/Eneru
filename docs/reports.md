# Reports

Eneru can deliver **periodic summary reports** — daily, weekly, and/or monthly —
through the same [notification channel](notifications.md) you already use for
alerts. A report rolls up power events, battery health, energy, and uptime for
the period into one digest.

## Configuration

```yaml
reports:
  enabled: true
  daily: false
  weekly: true
  monthly: false
  time: "08:00"                 # send time (daemon local time)
  weekly_day: monday            # day-of-week for the weekly digest
  monthly_day: 1                # day-of-month for the monthly digest
  include: [events, battery_health, energy, uptime]
  format: text                  # text | csv
```

A report is sent only for the periods you turn on (`daily` / `weekly` /
`monthly`); `enabled: false` disables all of them. The `include` list controls
which sections appear:

- `events` — power events for the period (`query_events`),
- `battery_health` — the latest health score / trend (`query_battery_health`),
- `energy` — kWh and cost for the period (see [Energy tracking](energy-tracking.md)),
- `uptime` — daemon uptime derived from `DAEMON_START` events.

`format: csv` attaches a machine-readable summary built with the stdlib `csv`
module (no extra dependency). PDF output is not included this round.

## Delivery and scheduling

Reports are delivered as **INFO** notifications tagged `category="report"`. That
category is the notification queue's coalescing concept — it is **not** the
[`notifications.suppress`](notifications.md) mechanism (which only mutes specific
power-event names). Reports are gated **solely** by `reports.enabled` plus the
per-period toggles; you cannot accidentally suppress a report by muting an event.

The shared scheduler records the last send time per period in the stats `meta`
table (`last_report_sent_<period>`), so:

- an infrequent monthly digest still fires correctly after a daemon restart, and
- the daemon never double-sends a period it already delivered.

In a multi-UPS deployment the report is **daemon-wide** — the coordinator sends a
single digest covering every UPS, not one per UPS.

`reports` is a hot-reload **safe** section: the monitor/coordinator rereads the
schedule from config on each loop, so schedule changes apply on SIGHUP without
a restart (see
[Configuration reference](configuration.md#hot-reload)).

## See also

- [Notifications](notifications.md) — configuring the delivery channel.
- [Energy tracking](energy-tracking.md) — the energy section of the digest.
- [Battery health](battery-health.md) — the battery-health section.
