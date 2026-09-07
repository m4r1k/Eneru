# Self-test

Eneru handles UPS **battery self-tests** two ways:

1. **Observe** the result of a test the UPS ran on its own — on the device's own
   schedule or triggered by hand — and record it (no configuration required).
2. **Issue** a test itself — on a schedule, from the CLI, or from the dashboard.

Both feed the [battery-health score](battery-health.md). Issuing uses the same
NUT `upscmd` path as [UPS control](nut-control.md).

## Observing device-run tests (no config)

On every poll Eneru reads `ups.test.result` / `ups.test.date` and, when a new
settled result appears (`passed`, `warning`, `failed`, or `aborted`), records it
as a `source: device` row.
This happens whether or not scheduled self-tests are enabled — many UPSes run a
test on their own cadence (`ups.test.interval`), and some operators only ever
test by hand. The latest result shows up in the dashboard, the API `selfTest`
block, the Prometheus series, and the battery-health score with no setup.

## Issuing tests: safety model

Issuing a test is a **write surface**, so it always requires **API
authentication** — an explicit `api.auth.enabled: true`, or simply a user in the
auth DB (`eneru user create`), which activates auth with no restart. Auth-off
always means read-only.

Enabling `self_test` is its own narrow permission: it grants exactly the one
command in `self_test.command`. You do **not** also need `nut_control.enabled`
or that command on `nut_control.allowed_commands` — the general control surface
(arbitrary `/command`, variable writes) stays gated separately. Credentials for
`upscmd` still come from `nut_control.username`/`password` when your upsd
requires a login to run instant commands.

If `upscmd -l` does not expose the configured command, the feature self-disables
for that UPS with a logged warning. Note that some upsd setups (notably UniFi's
NUT) only return the command list to a **logged-in** client — Eneru forwards the
`nut_control` credentials to `upscmd -l` so discovery works there.

**The command name varies by vendor.** The `test.battery.start` default is not
universal: APC devices via the `usbhid-ups` driver expose
`test.battery.start.quick` and `test.battery.start.deep` (plus
`test.battery.stop`) but **not** the bare `test.battery.start`. When the
configured command isn't offered, the CLI and the API's 422 response now list
the battery-test commands the UPS *does* expose, so you can set `self_test.command`
to the right one. Run `upscmd -l <ups>` yourself to see the full list.

**Multi-UPS:** the self-test command is per-UPS. When UPSes live on different
upsd servers, set `self_test.command` (and, if that upsd needs a login,
`nut_control.username` / `password`) under each `ups:` list entry — see the
multi-UPS example in `examples/config-reference.yaml`.

## Configuration

```yaml
api:
  auth:
    enabled: true            # or just: eneru user create <name>

self_test:
  enabled: true
  schedule: monthly          # daily | weekly | monthly | "every <N>d|h|m"
  time: "03:00"              # wall-clock for calendar schedules
  command: test.battery.start # adapts to what upscmd -l exposes
  result_poll_after: 60      # seconds to wait before reading the result

# Only needed if your upsd requires a login for upscmd (list/issue). The
# self_test command above is auto-permitted; you do NOT need nut_control.enabled
# or an allowed_commands entry just for self-test.
nut_control:
  username: "eneru"
  password: "secret"
```

`schedule` accepts `daily`, `weekly`, `monthly`, or an interval such as
`every 30d` / `every 12h`. The scheduler persists its last-run time, so a
30-day cadence survives daemon restarts (it does not reset to "now" on boot),
and a restart never kicks off an unscheduled test. `self_test` is a **SAFE**
hot-reload section: its `Schedule` is recomputed from config on every loop, so a
SIGHUP schedule change is picked up on the next due-check with no re-register
step.

## Result normalization

The raw `ups.test.result` string is vendor-specific and unbounded, so Eneru
stores it verbatim **and** maps it to a small stable enum that the API,
Prometheus, and UI consume:

`passed` · `warning` · `failed` · `aborted` · `running` · `unknown` · `unsupported`

Eneru writes the active test row before it sends the NUT command. Think of the
row as a claim ticket: if the daemon restarts after issuing the command, it can
still find the ticket and finish tracking the result. A second test is refused
while that ticket is active. If the row cannot be stored, Eneru sends no command.

After `result_poll_after`, Eneru reads the result. `running` and `unknown` are
polled again at the same interval instead of being treated as final. Polling
ends on a terminal result or after 24 hours; a timeout becomes `unknown`.
Eneru queues one start notification and one terminal notification per test,
including across daemon restarts.

The direct CLI path has no resident monitor. Its active ticket therefore blocks
another direct test for at most 24 hours; after that, the stale row becomes
`unknown` and a new test may start.

## Power events and failed tests

A UPS may briefly report `OB` while it exercises the battery. If Eneru sees
`CAL`, a running result, or a recently issued test, it records that pair as
`SELF_TEST_ON_BATTERY` and `SELF_TEST_POWER_RESTORED`. Ordinary outage
notifications are suppressed for the pair, but FSD, low battery, runtime,
depletion, extended-time, and connection-loss protections still run. If the
UPS remains on battery after the test finishes, Eneru records a normal
`ON_BATTERY` event and treats the continuing interval as a utility outage.

A hard `failed` result persists a failure latch. A later `passed` result clears
it; `warning`, `aborted`, and `unknown` leave it unchanged. The failed test does
not shut anything down while line power is present or during the test's own OB
interval. On a later genuine outage, shutdown starts after
`triggers.self_test_failure_shutdown_delay`, which defaults to 30 seconds. Set
the value to `0` to act on the first poll of that later outage.

Redundancy members publish this condition as an advisory trigger and let the
group quorum decide whether to act. A monitoring-only UPS sends a critical
notification but runs no shutdown actions.

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

- `POST /api/v1/ups/{name}/self-test` — auth-gated and audited. Permitted when
  `self_test` is enabled (grants exactly `self_test.command`) or the general
  `nut_control` surface allows the command.
- The latest result appears in the `selfTest` block of `GET /api/v1/ups`,
  including its `source` (`device` for an observed test, `scheduler`/`api`/`cli`
  for one Eneru issued). (The `GET /api/v1/ups/{name}/battery-health` endpoint
  returns the health score and replacement projection; the self-test result
  lives in the status `selfTest` block.)
- The dashboard **Control** tab has a per-UPS *Run self-test* button; the
  **Battery** tab shows the latest result.
- **Prometheus** → `eneru_ups_self_test_result{result="passed|failed|..."}`
  (the normalized enum, never the raw vendor string as a label).

## Testing it for real

The NUT **dummy driver has no INSTCMD**, so *issuing* a test has no end-to-end CI
coverage (the issue logic is unit-tested). The **observe** path *is* covered
end-to-end: the E2E suite serves passive running and failed states, checks
self-test attribution, then simulates a later outage and verifies the delayed
failed-test trigger. On real
hardware, confirm `upscmd -l` lists your test command first — e.g. the Ubiquiti
TOWER_1000VA exposes `test.battery.start` (pass `nut_control` credentials if your
upsd requires a login to list) — then run `eneru self-test run` and check
`eneru self-test status`.
