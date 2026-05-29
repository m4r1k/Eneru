# UPS control

Eneru can send commands and write variables to a UPS through NUT's `upscmd` and
`upsrw` clients — battery self-tests, beeper toggles, transfer-voltage tuning,
and so on. It wraps the same NUT CLIs the daemon already uses for `upsc`, so
there is nothing new to install beyond `nut-client`.

## Safety model

UPS control is a **write surface**, and Eneru treats it that way:

- **Off by default.** Set `nut_control.enabled: true` to turn it on.
- **Auth is mandatory.** Eneru refuses to start if `nut_control.enabled` is set
  while `api.auth.enabled` is false — "auth disabled" always means read-only.
  Every control request requires a valid credential (a logged-in session token
  or an API key). See [Authentication](authentication.md).
- **Allowlisted.** Only commands in `allowed_commands` and variables in
  `allowed_variables` can be invoked; anything else is rejected with `403` before
  any NUT call. Calibration and forced-shutdown (FSD) are not in the defaults,
  and `allowed_variables` is empty by default because `upsrw` can change
  protective settings.
- **Audited.** Each control action (allowed, denied, or failed) is logged with
  the principal who initiated it. (v7.0 adds a tamper-evident audit log.)

## Configuration

```yaml
api:
  enabled: true
  auth:
    enabled: true            # required for nut_control

nut_control:
  enabled: true
  username: "eneru"          # NUT upsd.users account with INSTCMD/SET actions
  password: "secret"
  allowed_commands:
    - test.battery.start
    - test.battery.start.quick
    - beeper.toggle
  allowed_variables:
    - input.transfer.low
    - input.transfer.high
  timeout: 10
```

The NUT account must have the matching actions granted in `upsd.users`, e.g.:

```ini
[eneru]
  password = secret
  instcmds = test.battery.start
  instcmds = beeper.toggle
  actions = SET
```

## Endpoints

All require authentication and `nut_control.enabled`.

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/v1/ups/{name}/commands` | Allowlisted commands the UPS supports (plus the full `supported` set) |
| POST | `/api/v1/ups/{name}/command` | Run a command: body `{"command": "beeper.toggle"}` |
| GET | `/api/v1/ups/{name}/variables` | Allowlisted writable variables and current values |
| PUT | `/api/v1/ups/{name}/variables/{var}` | Set a variable: body `{"value": "200"}` |

`{name}` is the UPS name (`UPS@host`) or its sanitized id. Commands against the
same UPS are serialized so two requests can't race an `INSTCMD`/`SET`.

```bash
# Trigger a quick battery self-test (with a session bearer token)
curl -X POST -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"command":"test.battery.start.quick"}' \
  http://127.0.0.1:9191/api/v1/ups/UPS@localhost/command
```

A NUT-side failure (driver offline, permission denied) is returned as `502` with
the underlying message; a disallowed command or variable is `403`.

> **Multi-UPS note (v6.0):** `nut_control` credentials are global. Deployments
> where different UPSes live on separate `upsd` servers with different
> credentials are not yet supported; per-group credentials are planned.
