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
| `?` / `H` | Show every key with its current value; any key closes it |

Graphs are hidden until you press `G`. On an 80-column terminal the key hints
wrap onto two rows; on narrower ones the last hint is always `<?> Help`.

## Reading the status panel

Each UPS gets a block, then each redundancy group. The panel is sized to what
it holds; on a short terminal the least important rows (resources, remote
health) go first.

```text
   Lab  (ups@192.168.178.11)  · Powers this host                 ON BATTERY
   Battery: 45% (6m 20s)  Load: 22%  Input: 0.0V  Output: 230.1V
   Updated 5s ago (00:01:33)
   On battery 7m 5s · Next trigger: critical runtime in ~1m 21s
   Shutdown when: charge < 20%: 45% (~20m) · runtime < 5m: 6m 20s (~1m 21s)
                  drain > 15%/min: 1.2%/min · on battery > 20m: in 13m
   If a trigger fires: Shuts down this host and 1 remote server
```

- **Role** after the name: `Powers this host`, `Remote shutdowns only`,
  `Monitoring only`, or `Redundancy member (<group>)`.
- **Badge** uses the same words as the dashboard: `ON MAINS`, `ON BATTERY`,
  `LOW BATTERY`, `SHUTDOWN TRIGGERED`, `SHUTTING DOWN`. Green is fine, amber
  needs attention, red is critical. Only `SHUTDOWN TRIGGERED` and
  `SHUTTING DOWN` blink. A monitoring-only UPS never shows them, because
  nothing is shut down for it.
- **Updated** is the age of the daemon's last successful poll, from the
  state file's `EPOCH` (or the file's modification time for daemons older
  than 6.2), in local time. Once it is older than three polls or 30 seconds,
  whichever is longer, the badge becomes `STALE` and the readings are shown
  as `Last known:`. The trigger countdowns pause until data is fresh again.
- **While on battery**: time on battery, the trigger that fires next, one
  chip per trigger (`(~20m)` is the estimated time until it fires, `FIRED`
  means the condition is met), and what firing does for this UPS.
- **Shutdown progress** appears while a shutdown runs:
  `SHUTDOWN IN PROGRESS: phase 3/7 Sync`, the reason, which phases are done,
  running or to do, and each remote server. It comes from the
  `*.shutdown-progress.json` file next to the state file, which the daemon
  resets on its first poll after a restart, so an old run never shows as live.
- **Redundancy groups** show healthy members against the quorum
  (`1/2 healthy, need 2`), what happens next (`Quorum lost → group shutdown
  runs`, `1 more failure → group shutdown`), each member's health and the
  group's shutdown action.
- **`NO DATA`** means the state file could not be read at the path shown.
  If the daemon runs in a container, point `logging.state_file` and
  `statistics.db_directory` at the host side of its bind mounts in the config
  you pass to `eneru monitor`, or run the TUI inside the container
  (`docker exec <container> eneru monitor`).

## One-shot status

For scripts, SSH sessions, and CI:

```bash
sudo eneru monitor --once --config /etc/ups-monitor/config.yaml
```

The snapshot prints the same lines as the live panel. The header ends with the
badge and the raw NUT tokens, for example `--  ON BATTERY (OB DISCHRG)`, and
readings the UPS does not report are left out.

Render a graph without opening curses (the last line states the y-axis scale
and now/min/max):

```bash
sudo eneru monitor --once --graph voltage --time 24h --config /etc/ups-monitor/config.yaml
```

Print recent events only:

```bash
sudo eneru monitor --once --events-only --length 100 --config /etc/ups-monitor/config.yaml
```

Use `--length 0` to remove the event row cap for one-shot output.
`--events-only` keeps the stable script format
(`YYYY-MM-DD HH:MM:SS  [UPS] EVENT_TYPE: detail`); the snapshot and the live
panel use readable labels (see below).

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

The live TUI groups enabled tiers as Power Events, Diagnostics, then Lifecycle. `--once` keeps a flat timestamp-sorted list for scripts. When the row cap applies, Power Events keep at least half of it (all of it at the default verbosity), every other enabled tier gets an equal share of the rest, and unused rows go to the newest events. A long outage history therefore no longer hides `-v` / `-vv` rows.

Each line shows the time, how long ago it was, the UPS label in multi-UPS mode, and a readable event name. Notification emoji and the UPS name already shown in the label are dropped, and older `Runtime: 1490 seconds` details read `24m 50s`. Narrow terminals (under 100 columns) shorten the time to `MM-DD HH:MM`:

```text
2026-09-10 10:45:13   14d ago  [APC] UPS connection lost: Cannot connect to UPS (Network, Server, or Config error).
2026-09-05 08:40:17   19d ago  [Lab] Power failure: Battery: 100%, Runtime: 24m 50s, Load: 20%
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
| `NO DATA` badge | The state file path in the config is not visible here (daemon stopped, or running in a container); see [Reading the status panel](#reading-the-status-panel) |
| `STALE` badge | The daemon stopped writing the state file: daemon down, NUT unreachable, or a different config |
| Block graph instead of Braille | Terminal locale or font lacks Braille support |
| TUI looks misaligned | Use a UTF-8 locale and a monospace font with emoji width support |

Useful checks:

```bash
sudo ls -la /var/lib/eneru/
sqlite3 /var/lib/eneru/UPS-192-168-1-100.db "SELECT COUNT(*) FROM samples;"
sqlite3 /var/lib/eneru/UPS-192-168-1-100.db "SELECT COUNT(*) FROM events;"
```
