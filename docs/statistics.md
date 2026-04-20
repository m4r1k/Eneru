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

```sql
samples (
    ts INTEGER NOT NULL,             -- epoch seconds
    status TEXT,                     -- e.g. "OL CHRG", "OB DISCHRG"
    battery_charge REAL,             -- %
    battery_runtime REAL,            -- seconds
    ups_load REAL,                   -- %
    input_voltage REAL,              -- V
    output_voltage REAL,             -- V
    depletion_rate REAL,             -- %/min
    time_on_battery INTEGER,         -- seconds since on-battery start
    connection_state TEXT            -- OK / GRACE_PERIOD / FAILED
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
    samples_count INTEGER
)

events (
    ts INTEGER NOT NULL,             -- epoch seconds
    event_type TEXT NOT NULL,        -- ON_BATTERY, POWER_RESTORED, ...
    detail TEXT
)

meta (
    key TEXT PRIMARY KEY,
    value TEXT                       -- e.g. schema_version=1
)
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

Per-UPS database, steady state at 1 Hz polling:

- Raw samples: ~100 bytes × 86,400 polls/day ≈ 8.6 MB/day. Older
  samples are aggregated and deleted after 24 h.
- 5-min aggregations: ~100 bytes × 288 buckets/day × 30 days ≈ 0.8 MB.
- Hourly aggregations: ~100 bytes × 24 buckets/day × 5 years ≈ 4 MB.
- Events table: a few hundred bytes per power event, normally negligible.

Steady-state footprint per UPS ≈ 14 MB (24 h raw + 30 d 5-min + 5 y
hourly). For a 4-UPS site that's ~56 MB.

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
