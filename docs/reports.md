# Reports

Eneru can deliver daily, weekly, and monthly summaries through the same
[notification channel](notifications.md) used for alerts. Each UPS takes one or
two compact lines, so a multi-UPS report remains readable on a phone.

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
  include: [events, battery_health, self_tests, energy, uptime]
  format: text                  # text | csv
```

A report is sent only for the periods you turn on (`daily` / `weekly` /
`monthly`); `enabled: false` disables all of them. The `include` list controls
which sections appear:

- `events` records the outage count, total duration, and whether an outage is
  still open.
- `battery_health` shows the latest score and flags partial confidence.
- `self_tests` counts results by normalized result, such as `1 test passed`.
- `energy` shows kWh and optional cost for the report window. A leading `~`
  means Eneru estimated consumption from UPS load percentage.
- `uptime` counts daemon restarts inside the report window.

The report uses `display_name` when configured. It does not expose the raw NUT
address as the operator-facing UPS name.

Example fleet report:

```text
📊 Weekly report · 1-7 Sep 2026 · 2 UPS

Lab UPS  3.42 kWh · USD 0.96 · 🔋 91
         1 outage (2m 14s) · 1 test passed · no restarts
Rack UPS ~5.10 kWh · USD 1.43 · 🔋 78 · confidence 75%
         no outages · 1 test warning · 1 restart

Total  ~8.52 kWh · USD 2.39

~ estimated from UPS load
```

## Report windows

All time boundaries use the daemon's local timezone.

| Report | Window |
|--------|--------|
| Daily | The previous complete local calendar day |
| Weekly | The seven days ending when the report is gathered |
| Monthly | The first day of the current month through report time |

Events, self-tests, restart counts, energy, and cost use the same displayed
window. Battery health uses the latest stored score because it is a current
condition rather than an accumulated total.

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

In a multi-UPS deployment the coordinator sends one daemon-wide digest. It
aligns the labels, adds one row per UPS, and totals energy and cost when every
UPS has usable data. If any UPS used estimated power, the total is also marked
with `~` and the report adds one estimate note.

`reports` is a hot-reload **safe** section: the monitor/coordinator rereads the
schedule from config on each loop, so schedule changes apply on SIGHUP without
a restart (see
[Configuration reference](configuration.md#hot-reload)).

## See also

- [Notifications](notifications.md) — configuring the delivery channel.
- [Energy tracking](energy-tracking.md) — the energy section of the digest.
- [Battery health](battery-health.md) — the battery-health section.
