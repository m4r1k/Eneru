# TUI graphs

Eneru's TUI (`eneru monitor`) renders compact line graphs from the
[per-UPS SQLite stats](statistics.md). The graphs use the Unicode
**Braille pattern block** (U+2800-U+28FF) so each terminal cell
encodes a 2 × 4 dot grid — eight binary pixels — giving a tight,
high-density plot in just a few rows of text.

## Keybindings

While the TUI is running:

| Key | Action |
|-----|--------|
| `Q` | Quit |
| `R` | Refresh now (forces an out-of-band redraw) |
| `M` | Toggle "more logs" |
| `G` | Cycle the graph metric: `off → charge → load → voltage → runtime` |
| `T` | Cycle the time range: `1h → 6h → 24h → 7d → 30d` |
| `U` | (multi-UPS) Cycle which UPS the graph shows |

The graph panel is hidden when the mode is `off` (the default).

## Time-range tier selection

The TUI calls `StatsStore.query_range()`, which automatically picks the
best aggregation tier for the requested window:

| Window | Tier used | Resolution |
|--------|-----------|------------|
| ≤ 24 h  | `samples`     | per poll (1 Hz typical) |
| ≤ 30 d  | `agg_5min`    | 5-minute averages       |
| > 30 d  | `agg_hourly`  | hourly averages         |

So the `1h` view is dot-accurate; the `7d` view is a smoothed 5-minute
trend; the `30d` view aggregates further still. This keeps the database
small and the queries fast even at 5-year retention.

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

- The renderer is `eneru.graph.BrailleGraph` — a pure, stateless module
  with no curses dependency. `BrailleGraph.plot(values, width=, height=)`
  returns one string per terminal row.
- The TUI opens the per-UPS DB **read-only** via
  `StatsStore.open_readonly(path)` (`?mode=ro` URI), so it never
  contends with the daemon's writer thread. WAL mode keeps reads
  non-blocking even while the writer is flushing.
- The DB is opened lazily — on the first non-`off` graph mode — and
  closed when the TUI exits.

## Troubleshooting

**The graph is empty.**
The daemon hasn't flushed any samples yet (writer flushes every 10 s)
or the stats `db_directory` doesn't match the daemon's. Check with:

```bash
ls -la /var/lib/eneru/
sqlite3 /var/lib/eneru/<sanitized-ups-name>.db "SELECT COUNT(*) FROM samples"
```

**The graph shows blocks instead of Braille dots.**
Your locale isn't UTF-8 capable, or your terminal font lacks Braille
glyphs. Both are normal in stripped-down minimal images. The block
fallback is fully accurate; only the rendering changes.

## See also

- [Statistics](statistics.md) — the SQLite store the TUI reads from.
- [Configuration](configuration.md) — `statistics:` and other settings.
