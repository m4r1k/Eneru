# Statistics

Eneru records every UPS poll into a per-UPS SQLite database. The TUI
graph view reads from it and you can run `sqlite3` (or DataGrip /
Grafana / a script) for offline analysis. The store is on by default
with no opt-in flag. The hot path is in-memory only; a background
thread does the disk writes.

## Architecture

```
poll cycle (1 Hz)        StatsWriter thread (~10 s)        SQLite store
─────────────────        ────────────────────────         ──────────────
buffer_sample()  ─────►  flush() executemany()  ────►    samples table
                         aggregate() every 5 min ───►    agg_5min table
                         purge() every 5 min     ───►    agg_hourly table
log_event()      ──────────────── direct INSERT ───►    events table
```

- The poll cycle calls `buffer_sample()`, a constant-time append to an
  in-memory deque. No I/O on the hot path.
- A background `StatsWriter` thread drains the deque to SQLite every
  10s with a single `executemany` transaction.
- Every 5 min the writer rolls samples into 5-minute buckets, then
  5-minute buckets into hourly buckets, and applies retention.
- SQLite errors are caught, logged once with rate-limit, and swallowed.
  Stats can never crash the monitor.

## Schema

The `samples` table carries 13 metrics per row: 10 raw NUT vars (per
spec 2.12) plus 3 Eneru-derived state fields. The 4 columns flagged
"v2" were added in v5.1.0-rc6 and arrive automatically on existing
databases via additive `ALTER TABLE` migration -- see
[Schema versioning](#schema-versioning) below.

```sql
samples (
    ts INTEGER NOT NULL,             -- epoch seconds
    status TEXT,                     -- e.g. "OL CHRG", "OB DISCHRG"
    battery_charge REAL,             -- %
    battery_runtime REAL,            -- seconds
    ups_load REAL,                   -- %
    input_voltage REAL,              -- V
    output_voltage REAL,             -- V
    depletion_rate REAL,             -- %/min       (Eneru-derived)
    time_on_battery INTEGER,         -- seconds since OB start (Eneru-derived)
    connection_state TEXT,           -- OK / GRACE_PERIOD / FAILED  (Eneru-derived)
    battery_voltage REAL,            -- V           (v2)
    ups_temperature REAL,            -- °C          (v2)
    input_frequency REAL,            -- Hz          (v2)
    output_frequency REAL            -- Hz          (v2)
)

agg_5min, agg_hourly (
    ts INTEGER PRIMARY KEY,          -- bucket start, epoch seconds
    battery_charge_avg REAL,
    battery_charge_min REAL,
    battery_charge_max REAL,
    battery_runtime_avg REAL,
    ups_load_avg REAL,
    ups_load_max REAL,
    input_voltage_avg REAL,
    input_voltage_min REAL,
    input_voltage_max REAL,
    samples_count INTEGER,
    output_voltage_avg REAL,         -- (v2 -- closes the long-standing gap)
    battery_voltage_avg REAL,        -- (v2)
    ups_temperature_avg REAL,        -- (v2)
    ups_temperature_min REAL,        -- (v2)
    ups_temperature_max REAL,        -- (v2)
    input_frequency_avg REAL,        -- (v2)
    output_frequency_avg REAL        -- (v2)
)

events (
    ts INTEGER NOT NULL,             -- epoch seconds
    event_type TEXT NOT NULL,        -- ON_BATTERY, POWER_RESTORED, ...
    detail TEXT,
    notification_sent INTEGER DEFAULT 1   -- (v3) 1=delivered, 0=muted
)

meta (
    key TEXT PRIMARY KEY,
    value TEXT                       -- e.g. schema_version=3
)
```

### `events.notification_sent` (v3)

Added in v5.1.0-rc7 alongside the [issue #27](https://github.com/m4r1k/Eneru/issues/27)
notification suppression work. Records whether the daemon dispatched
a notification for an event:

- `1` (default for backfilled rows from v2 DBs) — event was logged
  AND a notification was dispatched.
- `0` — event was logged but the notification was muted, either
  because it was in `notifications.suppress`, the voltage
  hysteresis dwell hadn't elapsed, or the event is in the always-
  silent set (`VOLTAGE_NORMALIZED`, `AVR_INACTIVE`,
  `VOLTAGE_FLAP_SUPPRESSED`, `VOLTAGE_AUTODETECT_MISMATCH`).

Audit query:

```bash
sqlite3 /var/lib/eneru/<UPS>.db \
  "SELECT event_type, COUNT(*) FROM events
   WHERE notification_sent = 0
   GROUP BY event_type
   ORDER BY 2 DESC;"
```

### New event types in v3

| Event | When |
|-------|------|
| `VOLTAGE_AUTODETECT_MISMATCH` | NUT's `input.voltage.nominal` disagreed with the observed `input.voltage` median by > 25V at startup. The `detail` carries `nut={N}V, observed median={M}V, re-snapped to {S}V`. |
| `VOLTAGE_FLAP_SUPPRESSED` | A voltage state transition (NORMAL→HIGH/LOW) reverted within `notifications.voltage_hysteresis_seconds`. The `detail` carries `state={LOW|HIGH} duration=Ns peak={V}V`. |

The 3 Eneru-derived columns (`depletion_rate`, `time_on_battery`,
`connection_state`) intentionally have no `*_avg` companion in the agg
tables: they're state-shaped, not signal-shaped, so an average over a
5-minute bucket is meaningless. The TUI graph panel only offers the 9
signal-shaped metrics in its `<G>` cycle.

## Schema versioning

`meta.schema_version` records the schema generation. New deployments
land at the current version (currently `2`). Daemons upgraded from an
older version run idempotent `ALTER TABLE ADD COLUMN` migrations on
first start; existing rows are preserved with `NULL` for the new
columns until the next sample. The migration is wrapped in try/except
so a duplicate-column error is benign and `meta.schema_version` is
bumped *after* the migrations succeed -- a crash mid-migration is
replayed safely on next start.

When a future feature adds new columns or tables, follow the pattern
documented in `src/eneru/CLAUDE.md` ("Stats schema evolution"):

```bash
sqlite3 /var/lib/eneru/UPS-host-3493.db \
  "SELECT key, value FROM meta WHERE key='schema_version';"
```

## Configuration

```yaml
statistics:
  # Directory holding one .db per UPS, named after the sanitized UPS name.
  db_directory: "/var/lib/eneru"
  retention:
    raw_hours: 24                    # raw samples retained 1 day
    agg_5min_days: 30                # 5-min aggregations retained 30 days
    agg_hourly_days: 1825            # hourly aggregations retained 5 years
```

Retention windows are independent per tier. Samples older than
`raw_hours` are deleted from `samples`; their aggregations live on in
`agg_5min` / `agg_hourly` until those tiers' own windows expire.

The deb / rpm package creates `/var/lib/eneru` on install (mode 0755,
owner root). Pip installs create the directory on first start.

## Storage on small devices (Raspberry Pi / SD card)

Per-UPS database, steady state at 1 Hz polling (v2 schema, 13 metrics):

- Raw samples: ~135 bytes × 86,400 polls/day ≈ 11.7 MB/day. Older
  samples are aggregated and deleted after 24 h.
- 5-min aggregations: ~135 bytes × 288 buckets/day × 30 days ≈ 1.1 MB.
- Hourly aggregations: ~135 bytes × 24 buckets/day × 5 years ≈ 5.5 MB.
- Events table: a few hundred bytes per power event, normally negligible.

Steady-state footprint per UPS ≈ 17 MB (24 h raw + 30 d 5-min + 5 y
hourly). For a 4-UPS site that's ~70 MB. Still trivial on the
smallest SD cards.

Disk I/O profile per UPS: one `executemany` transaction every 10s,
batching 10 inserts. That is roughly equivalent to a busy systemd
journald write. Meaningful on an SD card, but not enough to wear it
out faster than journald already does.

If your device has slow or wear-sensitive storage, relocate the
databases to an attached SSD or USB stick:

```yaml
statistics:
  db_directory: "/mnt/ssd/eneru-stats"
```

The directory must be writable by the user the daemon runs as (root
for deb/rpm installs).

## Inspecting a database

```bash
# Schema + table sizes
sqlite3 /var/lib/eneru/UPS-host-3493.db ".schema"
sqlite3 /var/lib/eneru/UPS-host-3493.db ".tables"
sqlite3 /var/lib/eneru/UPS-host-3493.db "SELECT COUNT(*) FROM samples"

# Last-hour battery charge trend
sqlite3 /var/lib/eneru/UPS-host-3493.db <<'SQL'
.mode column
.headers on
SELECT datetime(ts, 'unixepoch') AS time, battery_charge, ups_load, status
FROM samples
WHERE ts > strftime('%s', 'now', '-1 hour')
ORDER BY ts ASC;
SQL

# Recent power events
sqlite3 /var/lib/eneru/UPS-host-3493.db \
    "SELECT datetime(ts, 'unixepoch'), event_type, detail FROM events ORDER BY ts DESC LIMIT 20"
```

## Backup

Each `.db` is a self-contained SQLite file. Use `.backup` for an
online hot backup that does not block the writer:

```bash
sqlite3 /var/lib/eneru/UPS-host-3493.db ".backup '/srv/backups/UPS-host-3493.db'"
```

## Failure isolation

If `/var/lib/eneru` becomes read-only, runs out of space, or the
SQLite file gets corrupted, the daemon logs one warning and keeps
polling without stats persistence. You will see lines like:

```
⚠️ WARNING: stats store open failed at /var/lib/eneru/UPS-host.db: ...
```

or

```
stats: flush failed: disk I/O error
```

Restart the daemon after fixing the underlying storage problem. The
daemon will reopen the database (or create a fresh one) and continue.

## See also

- [Configuration reference](configuration.md). Full `statistics:` field
  reference.
- [Troubleshooting](troubleshooting.md). Disk-related failure modes.
