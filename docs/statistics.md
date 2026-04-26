# Statistics

Eneru writes UPS history to SQLite. The TUI graphs, event panel, notification retry queue, and offline troubleshooting all use the same per-UPS database files.

Statistics are always enabled. If the database cannot be opened or written, Eneru logs the problem and keeps monitoring.

## Storage path

```yaml
statistics:
  db_directory: "/var/lib/eneru"
  retention:
    raw_hours: 24
    agg_5min_days: 30
    agg_hourly_days: 1825
```

The package creates `/var/lib/eneru`. PyPI installs create it on first daemon start if permissions allow.

Each UPS gets its own `.db` file named from the sanitized UPS label.

## Write path

```text
UPS poll loop
    |
    v
append sample to in-memory buffer
    |
    v
StatsWriter flushes every ~10 seconds
    |
    v
SQLite samples table
    |
    v
5-minute and hourly aggregates
```

Polls do not wait on SQLite writes. The writer batches rows in the background. Event rows are inserted directly, but failures are isolated from the safety-critical monitor path.

### Stats write timeline

These intervals come from `StatsWriter` defaults in `src/eneru/stats.py`: `flush_interval=10.0` and `maintenance_interval=300.0`.

| Time | Action | Result |
|------|--------|--------|
| Every poll | Monitor appends sample to memory | No SQLite work runs on the hot path |
| About every 10s | Writer flushes buffered samples | One batched SQLite transaction persists recent samples |
| Every 300s | Writer aggregates raw rows | `agg_5min` receives new buckets |
| Every 300s | Writer rolls 5-minute buckets | `agg_hourly` receives hourly buckets |
| Every 300s | Retention runs | Old raw and aggregate rows are purged by tier |
| Any SQLite failure | Error is logged and rate-limited | Monitoring continues without stats persistence |

## Tables

| Table | Purpose |
|-------|---------|
| `samples` | Raw poll samples, typically 1 Hz |
| `agg_5min` | Five-minute aggregate buckets |
| `agg_hourly` | Hourly aggregate buckets |
| `events` | Power, health, lifecycle, and shutdown events |
| `notifications` | Persistent notification queue and delivery history |
| `meta` | Schema version and lifecycle metadata |

The main sample metrics include status, battery charge, runtime, load, input/output voltage, battery voltage, temperature, frequency, depletion rate, time on battery, and connection state.

## Retention

| Tier | Default retention | Use |
|------|-------------------|-----|
| Raw samples | 24 hours | Detailed incident review |
| Five-minute aggregates | 30 days | Recent trends |
| Hourly aggregates | 5 years | Long-term battery and load history |

Raw rows are deleted after they have been aggregated. Aggregates have their own retention windows.

## Disk usage

At a 1-second poll interval, expect roughly 15 to 20 MB per UPS at the default retention settings, plus small growth from events and notification rows.

For SD-card systems, the write pattern is a small transaction about every 10 seconds per UPS. That is usually less write pressure than normal system logging, but you can move the database to better storage:

```yaml
statistics:
  db_directory: "/mnt/ssd/eneru-stats"
```

Make sure the daemon user can write there.

## Inspect data

List databases:

```bash
sudo ls -lh /var/lib/eneru/
```

Show tables:

```bash
sqlite3 /var/lib/eneru/UPS-192-168-1-100.db ".tables"
```

Recent samples:

```bash
sqlite3 /var/lib/eneru/UPS-192-168-1-100.db <<'SQL'
.headers on
.mode column
SELECT datetime(ts, 'unixepoch') AS time,
       status,
       battery_charge,
       battery_runtime,
       ups_load,
       input_voltage
FROM samples
ORDER BY ts DESC
LIMIT 20;
SQL
```

Recent events:

```bash
sqlite3 /var/lib/eneru/UPS-192-168-1-100.db \
  "SELECT datetime(ts, 'unixepoch'), event_type, detail FROM events ORDER BY ts DESC LIMIT 20;"
```

Muted events:

```bash
sqlite3 /var/lib/eneru/UPS-192-168-1-100.db \
  "SELECT event_type, COUNT(*) FROM events WHERE notification_sent = 0 GROUP BY event_type;"
```

Pending notifications:

```bash
sqlite3 /var/lib/eneru/UPS-192-168-1-100.db \
  "SELECT id, attempts, next_attempt_at, title FROM notifications WHERE status='pending';"
```

## TUI graphs

`eneru monitor` reads the stats database read-only. Graphs select the smallest table that covers the requested time range:

| Window | Table | Resolution |
|--------|-------|------------|
| Up to 24 hours | `samples` | Per poll |
| Up to 30 days | `agg_5min` | Five-minute buckets |
| More than 30 days | `agg_hourly` | Hourly buckets |

See [Monitor and graphs](tui-graphs.md) for TUI usage.

## Backup

SQLite supports online backup without stopping Eneru:

```bash
sqlite3 /var/lib/eneru/UPS-192-168-1-100.db \
  ".backup '/srv/backups/UPS-192-168-1-100.db'"
```

## Schema migrations

Eneru stores the schema version in `meta.schema_version`. New releases migrate existing databases with additive `ALTER TABLE` statements and preserve old rows.

Check the version:

```bash
sqlite3 /var/lib/eneru/UPS-192-168-1-100.db \
  "SELECT key, value FROM meta WHERE key='schema_version';"
```

Migrations are designed to be replay-safe. If a daemon crashes mid-migration, the next start retries before bumping the schema version.

## Failure behavior

Stats failures do not stop monitoring or shutdown. You may see logs like:

```text
stats store open failed at /var/lib/eneru/UPS.db: ...
stats: flush failed: disk I/O error
```

Fix the storage problem, then restart Eneru so it can reopen the database.
