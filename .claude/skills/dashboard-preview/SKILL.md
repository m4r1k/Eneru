---
name: dashboard-preview
description: Visually verify Eneru browser-dashboard changes against a live daemon or audit an exact deployment. Use whenever web assets change and you need to see the result rendered on real telemetry. Preview serves working-tree assets over a live API; deployed audit inventories and screenshots the assets a running instance actually serves.
---

# Dashboard preview

The browser dashboard is static assets (`src/eneru/web/`) that talk to the
daemon's JSON API. String-only or fixture-based tests miss layout, theming, and
chart-rendering regressions. This skill supports two related checks:

1. `dashboard-preview.py` renders **working-tree** assets against a **live**
   daemon so an uncommitted change can be verified by eye.
2. `dashboard-audit.py` visits a **deployed** daemon directly so the screenshots
   prove what that installation actually serves.

ELI5: a one-way mirror in front of the running daemon — you see your brand-new
UI code, but every data question is passed through to the real daemon and the
real answer comes back. Tomorrow's dashboard on today's live data.

## When to use

- Any change under `src/eneru/web/` (`app.js`, `style.css`, `index.html`).
- Before pushing a PR that touches the dashboard — capture light + dark and read
  the PNGs to confirm there are no console errors and the affected tab renders.
- After deploying a dashboard build — audit every visible tab and its safe,
  read-only submenus at the real URL.

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

### Preview working-tree assets

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

### Audit an exact deployment

```bash
/tmp/eneru-venv/bin/python tools/dashboard-audit.py \
    --url http://eneru-host:9191 \
    --out /tmp/eneru-dashboard-audit \
    --capture-scopes

# Include the authenticated Control tab. The password is prompted securely.
/tmp/eneru-venv/bin/python tools/dashboard-audit.py \
    --url https://eneru.example.test \
    --username admin
```

Read the PNGs and `report.json`. The audit covers every visible tab in light
and dark themes, UPS detail dialogs, Event type filters, the sanitized Config
tree, a mobile viewport, and optional per-UPS scopes. It records console
warnings/errors, failed or HTTP-error requests, select options, and visible
controls without accessible names. It exits non-zero when those checks find an
issue.

The deployed audit is read-only. It never clicks UPS commands, self-tests,
variable writes, event deletion, config reload, or shutdown actions. Do not add
one of those actions to the tool merely to broaden coverage; hardware-changing
tests belong in a purpose-built, disposable environment.

## Options

### Working-tree preview

| Flag | Default | Meaning |
|------|---------|---------|
| `--web-dir` | `src/eneru/web` | working-tree assets to serve |
| `--daemon` | `http://127.0.0.1:9191` | live daemon API to proxy to |
| `--port` | `9232` | local preview-server port |
| `--out` | `/tmp/eneru-preview` | screenshot output directory |
| `--tabs` | all 7 | comma-separated tabs to capture |
| `--themes` | `light,dark` | comma-separated themes |
| `--settle-ms` | `2500` | wait for first refresh + charts to settle |

### Deployed audit

| Flag | Default | Meaning |
|------|---------|---------|
| `--url` | `http://127.0.0.1:9191` | deployed dashboard URL |
| `--out` | `/tmp/eneru-dashboard-audit` | PNG and JSON output directory |
| `--tabs` | every visible tab | comma-separated tabs to capture |
| `--themes` | `light,dark` | comma-separated themes |
| `--username` | none | sign in to expose authenticated read-only UI; password is prompted |
| `--capture-scopes` | off | capture Overview once per individual UPS |
| `--mobile-width` | `390` | mobile Overview width; `0` disables it |

## Notes

- The preview server proxies everything that is not `/`, `/app.js`, or
  `/style.css` straight to the daemon, so auth, history, energy, battery-health,
  and event endpoints all return real data.
- For endpoints a given daemon can't serve (e.g. a feature still in the working
  tree), point `--daemon` at a daemon that can, or stub the route in a local
  fork of the harness.
- This is the project's standard dashboard-verification loop — prefer it over
  trusting unit tests for any visual change.
- Use preview before deployment and deployed audit after deployment. Together,
  they distinguish a source-code regression from stale or incorrectly packaged
  static assets.
