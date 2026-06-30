---
name: dashboard-preview
description: Visually verify Eneru browser-dashboard (web/) changes against a LIVE daemon. Use whenever you change src/eneru/web/app.js or style.css and need to SEE the result rendered on real telemetry (not synthetic fixtures or string-only tests). Serves the working-tree assets, proxies /api/* to a running daemon, and screenshots every tab in light + dark.
---

# Dashboard preview

The browser dashboard is static assets (`src/eneru/web/`) that talk to the
daemon's JSON API. String-only or fixture-based tests miss layout, theming, and
chart-rendering regressions. This skill renders the **working-tree** assets
against a **live** daemon and screenshots them so the change is verified by
eye.

ELI5: a one-way mirror in front of the running daemon — you see your brand-new
UI code, but every data question is passed through to the real daemon and the
real answer comes back. Tomorrow's dashboard on today's live data.

## When to use

- Any change under `src/eneru/web/` (`app.js`, `style.css`, `index.html`).
- Before pushing a PR that touches the dashboard — capture light + dark and read
  the PNGs to confirm there are no console errors and the affected tab renders.

## Prerequisites

1. A running Eneru daemon with the API enabled (default `http://127.0.0.1:9191`).
   Confirm: `curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:9191/api/v1/ups`
   should print `200`.
2. The dev environment with Playwright + Chromium (per `AGENTS.md` the venv must
   be a `uv` venv; never the system Python):
   ```bash
   uv venv /tmp/eneru-venv && source /tmp/eneru-venv/bin/activate
   uv pip install -e ".[dev]"
   playwright install chromium
   ```

## Run it

```bash
python tools/dashboard-preview.py                       # all tabs, light+dark
python tools/dashboard-preview.py --themes light        # light only
python tools/dashboard-preview.py \
    --daemon http://127.0.0.1:9191 \
    --out /tmp/eneru-preview \
    --tabs overview,battery
```

Then **Read the PNGs** (`<out>/dash-[<theme>-]<tab>.png`) to actually look at
the result. The script also prints any browser `console` errors/warnings and
exits non-zero if it saw any — treat those as failures.

## Options

| Flag | Default | Meaning |
|------|---------|---------|
| `--web-dir` | `src/eneru/web` | working-tree assets to serve |
| `--daemon` | `http://127.0.0.1:9191` | live daemon API to proxy to |
| `--port` | `9232` | local preview-server port |
| `--out` | `/tmp/eneru-preview` | screenshot output directory |
| `--tabs` | all 7 | comma-separated tabs to capture |
| `--themes` | `light,dark` | comma-separated themes |
| `--settle-ms` | `2500` | wait for first refresh + charts to settle |

## Notes

- The preview server proxies everything that is not `/`, `/app.js`, or
  `/style.css` straight to the daemon, so auth, history, energy, battery-health,
  and event endpoints all return real data.
- For endpoints a given daemon can't serve (e.g. a feature still in the working
  tree), point `--daemon` at a daemon that can, or stub the route in a local
  fork of the harness.
- This is the project's standard dashboard-verification loop — prefer it over
  trusting unit tests for any visual change.
