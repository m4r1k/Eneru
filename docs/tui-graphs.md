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
| `V` | Cycle event verbosity: power, diagnostics, all |
| `↑` / `↓` | Scroll the events panel |
| `PgUp` / `PgDn` | Scroll events by a larger step |
| `Home` / `End` | Jump to oldest / newest event rows |

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
sudo eneru monitor --once --events-only --length 100 --config /etc/ups-monitor/config.yaml
```

Use `--length 0` to remove the event row cap for one-shot output.

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

Events are filtered by operator relevance:

| Verbosity | CLI | Live TUI | Events shown |
|-----------|-----|----------|--------------|
| Default | none | initial view | Power Events only |
| Diagnostics | `-v` / `--verbose` | press `<V>` once | Power Events and Diagnostics |
| All | `-vv` | press `<V>` twice | Power Events, Diagnostics, and Lifecycle |

The live TUI groups enabled tiers as Power Events, Diagnostics, then Lifecycle. `--once` keeps a flat timestamp-sorted list for scripts. When `--length` caps output, Power Events are kept first, Diagnostics fill next, and Lifecycle rows fill last.

In multi-UPS mode, event lines include the UPS label:

```text
14:03:12  [Rack A] ON_BATTERY: Battery: 85%
14:03:14  [Rack B] POWER_RESTORED: Outage 6s
```

## Remote health

When remote SSH healthchecks are enabled, the live monitor and one-shot status include the latest remote target status when a health sidecar exists. Healthchecks run a dedicated harmless probe command and never execute configured pre-shutdown or shutdown commands.

Remote health is advisory. During a shutdown sequence, Eneru still attempts the configured remote pre-shutdown commands and final shutdown command with bounded timeouts even if the last healthcheck failed.

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
