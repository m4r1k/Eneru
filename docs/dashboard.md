# Web dashboard

Eneru ships a small browser dashboard served by the embedded API server. There is
no external service, build toolchain, or third-party JavaScript. It is a thin
client over the REST API: every value it shows comes from an endpoint, and all
logic stays server-side.

![Eneru browser dashboard](images/eneru-webui.gif){ width="900" }

## Enabling it

The dashboard is served automatically whenever the API is enabled. There is no
separate switch:

```yaml
api:
  enabled: true
  bind: "127.0.0.1"   # expose only where you trust the network, or enable auth
  port: 9191
```

Open `http://<host>:9191/` in a browser.

## What it shows

- **UPS status cards:** status badge, battery charge (with a threshold-colored
  bar), runtime, and load, from `/api/v1/ups`. **Click a card** to open a detail
  panel with live status, power quality (input/output/battery voltage,
  frequencies, temperature), the UPS's configuration, its redundancy-group
  membership, and remote-health for that source. The detail panel reads a shared
  config + remote-health snapshot taken once per refresh, so opening it costs no
  extra requests.
- **Redundancy groups:** a healthy/required rollup (how many member UPSes are
  currently healthy vs the quorum target), when configured. Each card names
  the failing members, says what happens next ("1 more failure → group
  shutdown", "Quorum lost → group shutdown runs") and what the group's
  shutdown does. The header **View**
  selector can scope Overview, Power, Battery, Energy, Events, and Shutdown to
  one redundancy group. Group views use the member UPS telemetry and show only
  remote servers owned by that group. UPS write controls remain per-UPS, so the
  Control tab asks you to select a UPS instead of exposing group-wide buttons.
- **On-battery / shutdown banner:** driven by live UPS and redundancy status (not
  stale events), so it appears when a UPS goes on battery or a shutdown trigger
  fires and clears as soon as power returns. The wording depends on the UPS's
  role: a monitoring-only UPS gets an amber note that nothing is shut down on
  this host; a UPS that powers this host names the trigger that fired and what
  the shutdown does; a running shutdown links to its progress on the Shutdown
  tab. A redundancy member's own alarms stay amber and say the group decides,
  even when its entry has local resources: only the group's quorum verdict
  (quorum lost, group shutdown running) turns the banner red. Red alerts are announced to screen readers (`role="alert"`), and the
  browser tab title follows the outage (`⚠ On battery · Lab 62% — Eneru`).
- **What happens next:** while a UPS is on battery, its view lists every armed
  trigger (charge, runtime, drain rate, time on battery, ...) with the live
  value and an ETA, led by the closest one, plus what firing does. Fleet rows
  show time on battery and the next trigger. Nothing is shown for a
  monitoring-only UPS beyond a note that it only alerts.
- **Freshness:** each UPS shows how old its reading is ("updated 3s ago"). If
  the daemon stops answering, the footer and the error line say how old the
  data on screen is; after three missed polls (30 s) the page is greyed out and
  marked `STALE` so a frozen daemon never looks live.
- **History graphs:** hand-rolled SVG line charts for battery charge, load,
  runtime, and input voltage, from `/api/v1/ups/{name}/history`, with a **range
  selector** (1 hour → 1 year, or All). Charts scale to the panel width and
  redraw on resize.
- **Event timeline:** power/diagnostic/lifecycle events from `/api/v1/events`,
  newest first, with filters for source, event type, and detail text, a **range
  selector**, and a **Load older** button under the table that pages further
  back through the full retained history.
- **Delete events:** when signed in, select events with the row checkboxes and
  use **Delete selected** to remove them (auth-gated; the server enforces it).
  Only currently-visible selected rows are deleted.
- **Control panel:** command buttons and writable-variable forms, shown only
  when you are signed in **and** [`nut_control`](nut-control.md) is enabled. The
  controls reflect the configured command/variable allowlists; the server
  enforces them regardless of what the UI renders.
- **Shutdown plan and progress:** the Shutdown tab shows the exact plan for a
  UPS or redundancy group, every configured trigger with its threshold and the
  closest one right now, phase order, parallel remotes, advisory
  remote health, and the current or most recent execution result. During an
  active shutdown it marks phases and remote targets as running, succeeded,
  failed, timed out, or skipped.

The page polls status every 10 seconds. While the Shutdown tab is visible, it
polls the small progress snapshots every second so phase changes appear promptly.

### Status and event wording

Status badges use the shared status vocabulary (see
[Observability API](observability-api.md), "Status vocabulary"): one short
label (**On mains**, **On battery**, **Low battery**, **Shutdown triggered**,
**Shutting down**, **Stale data**, ...) on a green/amber/red scale. On battery
is amber; only a triggered or running shutdown pulses. Hover a badge for the
full NUT wording. Each UPS also carries a role tag: **Powers this host**,
**Remote shutdowns only**, **Monitoring only** or **Redundancy member**, taken
from its real shutdown plan.

The dashboard translates NUT status flags and Eneru event identifiers into
plain language. For example, `OL CHRG` appears as **Utility power · Battery
charging**, `OB LB` appears as **Battery low · Running on battery**, and
`POWER_RESTORED` appears as **Power restored**. Safety states take priority, so
`FSD` appears first as **Shutdown in progress** even if the UPS also reports
`OL` or `OB`.

Vendor-specific status tokens are not hidden. The dashboard appends them as a
custom state after any recognized status. This translation happens only in the
browser: the REST API, SQLite history, and logs retain the exact NUT values for
integrations and troubleshooting. `eneru monitor` uses the same short labels.

## Theme

A **Theme** switcher in the header offers **System / Light / Dark**, persisted in
the browser's `localStorage`. The default is **System**, which follows the OS
light/dark preference with no flash (it's pure CSS); choosing Light or Dark pins
the theme regardless of the OS setting.

## Authentication

When [authentication](authentication.md) is enabled, use **Sign in** to log in
with a local user. The dashboard stores the returned session token in the
browser's `sessionStorage` and sends it as a `Bearer` header. There is no
cookie, so there is no CSRF surface. Read views follow the tiered policy (open
unless `api.auth.require_for_reads`); control actions always require sign-in.

The **Sign in** button appears whenever auth is enabled — the dashboard re-checks
`/api/v1/config` on every refresh, so it shows up on its own once auth becomes
active. When auth is off there is nothing to sign into. If a login fails, the
dashboard shows the server's actual reason. Creating a user with `eneru user
create` [auto-enables auth](authentication.md#auto-enable-create-a-user-then-just-sign-in-no-restart)
**within seconds, no restart**, so signing in works without hand-editing the config.

## Security

The HTML is served with a strict `Content-Security-Policy` (`default-src
'self'`) and `X-Content-Type-Options: nosniff`. Only the packaged asset names are
servable, so path traversal is not possible. The dashboard assets themselves
contain no secrets. They are static files; sensitive data only ever flows
through the authenticated API.
