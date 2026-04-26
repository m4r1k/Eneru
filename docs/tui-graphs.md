# Monitor and graphs

`eneru monitor` shows current UPS state, recent events, logs, and optional graphs from the SQLite stats store.

## Start the TUI

Package install:

```bash
sudo eneru monitor --config /etc/ups-monitor/config.yaml
```

PyPI install:

```bash
eneru monitor --config /etc/ups-monitor/config.yaml
```

The `tui` subcommand is an alias:

```bash
eneru tui --config /etc/ups-monitor/config.yaml
```

## Keybindings

| Key | Action |
|-----|--------|
| `Q` | Quit |
| `R` | Refresh now |
| `M` | Toggle expanded logs |
| `G` | Cycle graph metric: off, charge, load, voltage, runtime |
| `T` | Cycle graph range: 1h, 6h, 24h, 7d, 30d |
| `U` | Multi-UPS only. Cycle the UPS shown in the graph |

Graphs are hidden until you press `G`.

## One-shot status

For scripts, SSH sessions, and CI:

```bash
sudo eneru monitor --once --config /etc/ups-monitor/config.yaml
```

Render a graph without opening curses:

```bash
sudo eneru monitor --once --graph voltage --time 24h --config /etc/ups-monitor/config.yaml
```

Print recent events only:

```bash
sudo eneru monitor --once --events-only --time 24h --config /etc/ups-monitor/config.yaml
```

## Graph metrics

| Metric | Source |
|--------|--------|
| `charge` | Battery charge percentage |
| `load` | UPS load percentage |
| `voltage` | Input voltage |
| `runtime` | UPS estimated runtime |

The graph uses Unicode Braille cells when the terminal supports them. In non-UTF-8 terminals it falls back to block characters.

## Data source

Graphs read from the per-UPS SQLite database under `statistics.db_directory`. The TUI opens the DB read-only and uses the same retention tiers described in [Statistics](statistics.md).

| Range | Data tier |
|-------|-----------|
| `1h`, `6h`, `24h` | Raw samples |
| `7d` | Five-minute aggregates |
| `30d` | Five-minute or hourly aggregates depending on retention |

The daemon flushes samples about every 10 seconds. The TUI blends in the newest state-file values so the graph edge does not lag far behind the live status panel.

### Graph freshness timeline

The graph freshness behavior is tied to the stats writer's 10-second flush interval and the live-sample blending path in `src/eneru/tui.py`.

| Time | Data source | What the operator sees |
|------|-------------|------------------------|
| 0s | Daemon polls UPS | State file updates quickly |
| 1s-9s | SQLite has not flushed yet | Status panel is current; graph tail is blended from live state |
| About 10s | Stats writer flushes | SQLite catches up with recent samples |
| Every 300s | Aggregation runs | Longer-range graphs use compact aggregate rows |
| TUI exits | Read-only DB handle closes | Daemon writer is unaffected |

## Events panel

The Recent Events panel reads the SQLite `events` table. If no database exists yet, it falls back to the log file.

In multi-UPS mode, event lines include the UPS label:

```text
14:03:12  [Rack A] ON_BATTERY: Battery: 85%
14:03:14  [Rack B] POWER_RESTORED: Outage 6s
```

## Troubleshooting

| Symptom | Check |
|---------|-------|
| Empty graph | Wait at least one stats flush, usually 10 seconds |
| Empty graph after minutes | Confirm `statistics.db_directory` matches the running daemon config |
| Events panel falls back to logs | Database missing, unreadable, or not created yet |
| Block graph instead of Braille | Terminal locale or font lacks Braille support |
| TUI looks misaligned | Use a UTF-8 locale and a monospace font with emoji width support |

Useful checks:

```bash
sudo ls -la /var/lib/eneru/
sqlite3 /var/lib/eneru/UPS-192-168-1-100.db "SELECT COUNT(*) FROM samples;"
sqlite3 /var/lib/eneru/UPS-192-168-1-100.db "SELECT COUNT(*) FROM events;"
```
