# Self-test

Eneru can issue a UPS **battery self-test** — on a schedule, from the CLI, or
from the dashboard — and record the result so it feeds the
[battery-health score](battery-health.md). It uses the same NUT `upscmd` path as
[UPS control](nut-control.md), so the same safety rules apply.

## Safety model

A self-test is a **write surface**. Enabling the scheduled test requires **all**
of the following, or the daemon refuses to start — a scheduled test must never be
a back door around the v6.0 control allowlist:

- `nut_control.enabled: true` with valid `upsd.users` credentials,
- `api.auth.enabled: true` (auth-off always means read-only), and
- `self_test.command` listed in `nut_control.allowed_commands`.

The same allowlist check runs on the scheduled path, the API path, and the
`--direct` CLI path — none is exempt. If `upscmd -l` does not actually expose the
configured command, the feature self-disables for that UPS with a logged warning.

## Configuration

```yaml
api:
  auth:
    enabled: true

nut_control:
  enabled: true
  username: "eneru"
  password: "secret"
  allowed_commands:
    - test.battery.start          # must include self_test.command

self_test:
  enabled: true
  schedule: monthly               # daily | weekly | monthly | "every <N>d|h|m"
  time: "03:00"                   # wall-clock for calendar schedules
  command: test.battery.start     # adapts to what upscmd -l exposes
  result_poll_after: 60           # seconds to wait before reading the result
```

`schedule` accepts `daily`, `weekly`, `monthly`, or an interval such as
`every 30d` / `every 12h`. The scheduler persists its last-run time, so a
30-day cadence survives daemon restarts (it does not reset to "now" on boot),
and a restart never kicks off an unscheduled test. `self_test` is a hot-reload
**subsystem** section — the scheduler re-registers its jobs on reload.

## Result normalization

The raw `ups.test.result` string is vendor-specific and unbounded, so Eneru
stores it verbatim **and** maps it to a small stable enum that the API,
Prometheus, and UI consume:

`passed` · `failed` · `running` · `unknown` · `unsupported`

## CLI

```bash
# Issue a test through the running daemon (the daemon owns the audit record and
# the self_tests row in its state DB). Needs an API credential:
eneru self-test run --token "$ENERU_TOKEN"            # or --api-key / $ENERU_API_TOKEN
eneru self-test run --ups "UPS-B@10.0.0.11" --url http://127.0.0.1:9191

# Or issue directly via NUT with no daemon (one-shot; still allowlist-checked):
eneru self-test run --direct --config /etc/ups-monitor/config.yaml

# Read the latest recorded result (from the local stats DB):
eneru self-test status
```

`run` defaults to the **API client** so a single daemon owns the issue, audit,
and recording. `--direct` issues `test.battery.start` straight through
`nut_control` credentials and records the row in the configured stats DB — use
it when no daemon is running. `--ups` defaults to the only configured UPS.

## API and dashboard

- `POST /api/v1/ups/{name}/self-test` — auth-gated, audited, allowlist-checked.
- The latest result appears in the `selfTest` block of `GET /api/v1/ups`. (The
  `GET /api/v1/ups/{name}/battery-health` endpoint returns the health score and
  replacement projection; the self-test result lives in the status `selfTest`
  block.)
- The dashboard **Control** tab has a per-UPS *Run self-test* button; the
  **Battery** tab shows the latest result.
- **Prometheus** → `eneru_ups_self_test_result{result="passed|failed|..."}`
  (the normalized enum, never the raw vendor string as a label).

## Testing it for real

The NUT **dummy driver has no INSTCMD**, so self-test has no end-to-end CI
coverage (the logic is unit-tested). On real hardware, confirm `upscmd -l` lists
your test command first — e.g. the Ubiquiti TOWER_1000VA exposes
`test.battery.start` — then run `eneru self-test run` and check
`eneru self-test status`.
