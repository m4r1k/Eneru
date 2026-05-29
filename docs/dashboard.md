# Web dashboard

Eneru ships a small browser dashboard served by the embedded API server — no
external service, no build toolchain, and no third-party JavaScript. It is a thin
client over the REST API: every value it shows comes from an endpoint, and all
logic stays server-side.

## Enabling it

The dashboard is served automatically whenever the API is enabled — there is no
separate switch:

```yaml
api:
  enabled: true
  bind: "127.0.0.1"   # expose only where you trust the network, or enable auth
  port: 9191
```

Open `http://<host>:9191/` in a browser.

## What it shows

- **UPS status cards** — status badge, battery charge (with a bar), runtime, and
  load, from `/api/v1/ups`.
- **Redundancy groups** — source count and quorum target, when configured.
- **History graphs** — hand-rolled SVG line charts for battery charge, load,
  runtime, and input voltage, from `/api/v1/ups/{name}/history`.
- **Event timeline** — recent power/diagnostic/lifecycle events from
  `/api/v1/events`, with filters for source, event type, and detail text.
- **Control panel** — command buttons and writable-variable forms, shown only
  when you are signed in **and** [`nut_control`](nut-control.md) is enabled. The
  controls reflect the configured command/variable allowlists; the server
  enforces them regardless of what the UI renders.

The page polls every 10 seconds.

## Authentication

When [authentication](authentication.md) is enabled, use **Sign in** to log in
with a local user. The dashboard stores the returned session token in the
browser's `sessionStorage` and sends it as a `Bearer` header — there is no
cookie, so there is no CSRF surface. Read views follow the tiered policy (open
unless `api.auth.require_for_reads`); control actions always require sign-in.

## Security

The HTML is served with a strict `Content-Security-Policy` (`default-src
'self'`) and `X-Content-Type-Options: nosniff`. Only the packaged asset names are
servable, so path traversal is not possible. The dashboard assets themselves
contain no secrets — they are static files; sensitive data only ever flows
through the authenticated API.
