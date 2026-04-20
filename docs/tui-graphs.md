# TUI graphs

`eneru monitor` renders line graphs from the
[per-UPS SQLite stats](statistics.md). The graphs use the Unicode
Braille pattern block (U+2800-U+28FF). Each terminal cell encodes a
2 × 4 dot grid (8 binary pixels per cell), so a few rows of text
hold a usable plot.

## Keybindings

While the TUI is running:

| Key | Action |
|---|---|
| `Q` | Quit |
| `R` | Refresh now (forces an out-of-band redraw) |
| `M` | Toggle "more logs" |
| `G` | Cycle the graph metric: `off → charge → load → voltage → runtime` |
| `T` | Cycle the time range: `1h → 6h → 24h → 7d → 30d` |
| `U` | (multi-UPS) Cycle which UPS the graph shows |

The graph panel is hidden when the mode is `off` (the default).

## Time-range tier selection

`StatsStore.query_range()` picks the smallest aggregation tier that
still covers the requested window:

| Window | Tier used | Resolution |
|---|---|---|
| ≤ 24 h | `samples` | per poll (1 Hz typical) |
| ≤ 30 d | `agg_5min` | 5-minute averages |
| > 30 d | `agg_hourly` | hourly averages |

The `1h` view is dot-accurate. The `7d` view is a smoothed 5-minute
trend. The `30d` view aggregates further still. Keeps the database
small and the queries fast at 5-year retention.

## Headless rendering: `monitor --once --graph`

For scripts, screenshots, or CI, render a graph straight to stdout:

```bash
eneru monitor --once --graph charge --time 1h --config /etc/ups-monitor/config.yaml
```

Output (with Braille support):

```
Eneru v5.1.0
Time: 2026-04-20 16:00:00

TestUPS@localhost  --  Status: OL CHRG
  Battery: 100% (30m 0s)  Load: 25%  Input: 230.5V  Output: 230.0V
  ...

Graph: TestUPS@localhost
charge -- last 1h  (0-100%)
⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀
⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀⣀
y-axis: 0-100%
```

When the terminal is not Braille-capable (`LANG=C`, very old fonts),
the renderer falls back to standard block characters
(`▁ ▂ ▃ ▄ ▅ ▆ ▇ █`). Force the fallback for testing with
`force_fallback=True` when calling `BrailleGraph.plot()` directly.

## Architecture

- `eneru.graph.BrailleGraph` is a pure, stateless module with no
  curses dependency. `BrailleGraph.plot(values, width=, height=)`
  returns one string per terminal row.
- The TUI opens the per-UPS DB read-only via
  `StatsStore.open_readonly(path)` (`?mode=ro` URI), so it never
  contends with the daemon's writer thread. WAL + `PRAGMA
  busy_timeout = 500` keep reads non-blocking with a half-second
  upper bound on contention even on slow storage.
- The DB is opened lazily on the first non-`off` graph mode and
  closed when the TUI exits.
- **Live blending (rc6+).** The daemon's stats writer flushes every
  10 s, but the state file is rewritten every poll cycle (~1 s).
  Without blending, the graph's right edge would lag the live status
  panel by up to 10 s. The TUI keeps a per-UPS deque (60 entries,
  one per refresh) populated from the same state file
  `collect_group_data` reads. `query_metric_series` extends the
  SQLite tail with any deque samples newer than the most recent
  flush, deduped by timestamp. The deque is bounded and bytes
  cheap; no extra DB I/O.

## Footer hints

The bottom-row hints in `eneru monitor` interpolate the current
cycle state -- so an operator can see at a glance what the next
keypress will do:

```
 <Q>  Quit    <R>  Refresh    <M>  More logs   \
 <G>  Graph: charge    <T>  Time: 1h    <U>  UPS: 1/2
```

`<U>` is rendered as `UPS` (without the index) when only one UPS is
configured.

## Troubleshooting

**The graph is empty.**
The daemon has not flushed any samples yet (writer flushes every 10s),
or the TUI's `db_directory` does not match the daemon's. Check with:

```bash
ls -la /var/lib/eneru/
sqlite3 /var/lib/eneru/<sanitized-ups-name>.db "SELECT COUNT(*) FROM samples"
```

**The graph shows blocks instead of Braille dots.**
The locale is not UTF-8 capable, or the terminal font lacks Braille
glyphs. Both are normal in stripped-down minimal images. The block
fallback is accurate; only the rendering changes.

## Events panel: sourced from SQLite

The TUI's "Recent Events" panel reads from each UPS's `events` table
in the per-UPS SQLite store. When no DB is present (fresh installs
before the first poll, sandbox runs without a writable `db_directory`,
etc.), the panel falls back to tailing the log file. Same behaviour
as v5.0.

In multi-UPS mode, each line is prefixed with the UPS label and rows
from different sources interleave by timestamp:

```
14:03:12  [Rack PSU-A] ON_BATTERY: Battery: 85%
14:03:14  [Rack PSU-B] ON_BATTERY: Battery: 82%
14:03:18  [Rack PSU-A] POWER_RESTORED: Outage 6s
```

For headless or scripted use, `eneru monitor --once --events-only`
prints just the events list:

```bash
eneru monitor --once --events-only --time 24h
```

## See also

- [Statistics](statistics.md). The SQLite store the TUI reads from.
- [Configuration](configuration.md). `statistics:` and other settings.
