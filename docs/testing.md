# Testing

Eneru uses unit tests, package-install tests, and end-to-end tests with real NUT, SSH, and Docker services. The goal is to catch both Python logic bugs and packaging/runtime failures.

```text
                          ▲
                         ╱ ╲
                        ╱   ╲
                       ╱ UPS ╲
                      ╱ Power ╲
                     ╱  Events ╲
                    ╱───────────╲
                   ╱  End-to-End ╲
                  ╱ NUT+SSH+Docker╲
                 ╱─────────────────╲
                ╱    Integration    ╲
               ╱Package & Pip Install╲
              ╱  on key Linux Distros ╲
             ╱─────────────────────────╲
            ╱         Unit Tests        ╲
           ╱       pytest + Coverage     ╲
          ╱        3.9 -> 3.15 Python     ╲
         ╱─────────────────────────────────╲
        ╱          Static Analysis          ╲
       ╱   Syntax Check + Config Validation  ╲
      ╱───────────────────────────────────────╲
     ╱          AI-Assisted Development        ╲
    ╱   Claude Code Review & Quality Assurance  ╲
   ╱─────────────────────────────────────────────╲
```

The pyramid is intentionally bottom-heavy. The local pytest suite contains
thousands of tests, all gated at ≥95% per-file line+branch coverage. E2E
tests are fewer, but they exercise the real service boundaries where
packaging, NUT, SSH, Docker, filesystem, and CLI assumptions meet.

## Pre-release code review (v6.0.0)

Automated tests prove the code does what a test author *thought to ask*. They
do not, on their own, prove that nobody overlooked a way the daemon can drop a
healthy host or miss a real outage. So before the v6.0.0 release the whole
repository at HEAD — not just the release diff — went through a structured,
adversarial audit on top of the test pyramid. Think of it as a second pair of
eyes that is paid to assume every safety claim is wrong until it reads the code
and proves otherwise.

The audit was deliberately broad-then-deep:

- **Fan-out.** Eighteen independent reviewers each took one subsystem — the
  trigger evaluation and shutdown sequence, the multi-UPS coordinator and its
  locks, the redundancy quorum math, every shutdown/health mixin, remote-health
  flapping, the v6.0 API/auth/`nut_control`/dashboard surface, the SQLite stats
  layer, config parsing and hot-reload, and the test suite itself — and read
  the relevant files **in full**, not in excerpts.
- **Adversarial verification.** Every Critical/High/Medium candidate was handed
  to a second, independent reviewer whose job was to *refute* it by tracing the
  real call path and looking for an upstream guard the first reviewer missed.
  Only findings that survived that second pass were kept; one proposed High was
  refuted this way and dropped.
- **Maintainer confirmation.** The crown-jewel findings (the shutdown decision
  path and the auth/control gate) were then re-read by hand against the live
  code before any fix was written, so no fix rests solely on an automated claim.

The pass classified findings as Critical / High / Medium / Low / Nit using a
UPS-specific rubric where "false shutdown of a healthy host", "missed shutdown
during a real outage", and "auth bypass to a control endpoint" are the
Critical-tier outcomes. The headline result: the new v6.0 security surface
(argv-only NUT control, bcrypt + CSPRNG tokens, parameterized SQL, a strict
static-asset name check, and a write gate in front of every mutating route)
held up well; the residual risk was concentrated in the shutdown decision path,
where a plausible config typo or a slow/wedged subsystem could crash the daemon
*before* the host poweroff. rc10 fixed the first Critical/High tranche and opened
the upstream PR so CI could start immediately. rc11 then closed the remaining
confirmed Medium/Low/Nit items from the same audit: request-body read bounding/timeouts,
auth bootstrap under read-gated APIs, redundancy health reporting, SQLite
lifecycle races, dashboard event identity, password-reset session invalidation,
package-data drift, and docs/examples drift. Each code finding has a regression
test that fails against the pre-fix behavior, so the same class of bug cannot
silently return.

## CI layout

| Workflow | File | What it checks |
|----------|------|----------------|
| Validate | `.github/workflows/validate.yml` | Unit tests, coverage, example config validation |
| Integration | `.github/workflows/integration.yml` | Wheel install, `.deb`, `.rpm`, and package layout |
| E2E Tests | `.github/workflows/e2e.yml` | Real NUT/SSH/Docker behavior across grouped scenarios |
| Release | `.github/workflows/release.yml` | Release package build |
| PyPI | `.github/workflows/pypi.yml` | PyPI publish from release tags |

The protected `main` branch requires the validate matrix and six E2E matrix jobs.

## Local test environment

All Python development commands must run inside a `uv` virtualenv. Do not run `pip`, `python`, or `pytest` against the system Python while working in this repo.

```bash
uv venv /tmp/eneru-venv
source /tmp/eneru-venv/bin/activate
uv pip install -e ".[dev,notifications,docs]"
```

Then run tests from inside the activated venv:

```bash
pytest
pytest -m unit
pytest -m integration
pytest --cov=src/eneru --cov-report=term
```

Validate example configs:

```bash
for config in examples/*.yaml; do
  python -m eneru validate --config "$config"
done
```

## Test areas

| Area | Coverage |
|------|----------|
| Config loading and validation | YAML parsing, defaults, enum validation, multi-UPS inheritance, local ownership, loopback delegation config shape, redundancy rules |
| Monitor state machine | OL/OB transitions, FSD, failsafe, shutdown trigger order, dry-run behavior |
| Shutdown mixins | VMs, containers, compose files, filesystem sync and unmounts, remote SSH phases, remote pre-shutdown action rendering, loopback delegate bracketing (Phase A pre-actions → regulars → Phase C poweroff), exception isolation across phases, dry-run + per-server notification paths |
| CLI inspection vs runtime | `python -m eneru validate` shutdown-sequence tree, `python -m eneru remote list` ORDER + last-known HEALTH columns, `python -m eneru shutdown remote` drill, container legacy-path rewrite — all partition `is_host_loopback` delegates out of `compute_effective_order` and invoke `_prepare_runtime_config` / `_load_config` so the inspection output matches what the daemon would execute |
| Multi-UPS coordinator | Group routing, `is_local`, drain policy, local shutdown locking, signal handling |
| Redundancy runtime | Quorum evaluation, advisory triggers, connection-grace handling, idempotent group execution |
| Health monitoring | Voltage thresholds, AVR, bypass, overload, battery anomaly filtering |
| Notifications | Formatting, retry queue, lifecycle classification, coalescing, suppression rules, container restart/upgrade stop-row deferral, deferred stop delivery claim/recovery races and mark-sent failure logging |
| Statistics and TUI | SQLite schema (incl. the v5 `events.id` AUTOINCREMENT table-rebuild migration — column added, rows/version preserved, idempotent, id-not-reused-after-delete — and the v6 `notifications.delivering_at` migration for stale deferred-delivery claim recovery), stale-claim recovery failure propagation, aggregation, event tier filtering, wide-range + composite-cursor event paging across duplicate timestamps, `delete_events` (id+ts+type guard, dedup, per-DB isolation), TUI grouping, graphs, one-shot monitor output |
| Observability | API routing, readiness, Prometheus escaping, power-quality metrics, remote-health sidecars, MQTT publishing |
| Authentication | User/API-key SQLite store (bcrypt hashing, salt uniqueness, truncation, CRUD), `eneru user`/`apikey` CLI lifecycle, password-input safety (getpass/generate/stdin), lazy bcrypt import |
| API auth middleware | Session manager (TTL/expiry), tiered authorization matrix (reads open vs `require_for_reads`, writes fail-closed when auth off), bearer/API-key resolution, session re-validation against user state (deleted user or password reset signs out; DB error preserves the session), login/logout, body-size + JSON validation including total body-read deadlines and read-error mapping, tiered `/config` |
| Event management API | `DELETE /api/v1/ups/{name}/events` — authed delete + `EVENTS_DELETED` audit, anonymous 401 / auth-off 403, unknown UPS 404, stats-unavailable 503, malformed-body matrix (400) and oversize (413); monitor/coordinator routing to the live per-UPS store; events `from`/`to`/`before` paging and history `from > to`/`All` validation |
| UPS control | `upscmd`/`upsrw` wrappers and output parsing (including PTY output on NUT errors), fixed-binary argv validation before subprocess execution, username/password pairing before PTY prompt handling, command/variable allowlist enforcement, per-group credential/allowlist overrides, feature-disabled and unknown-UPS handling, NUT-error mapping, fail-closed config validation (control requires auth), value sanitization, audit logging to the events table |
| Config hot-reload | Strict load+validate (bad YAML / non-mapping / validation error rejected, running config kept), safe-vs-restart classification, in-place live apply across shared + per-monitor configs, subsystem reload hooks for stats/notifications/MQTT/remote-health, SIGHUP handler and API `/config/reload` endpoint |
| Web dashboard | Static asset serving via `importlib.resources`, MIME mapping, path-traversal rejection, strict CSP + `nosniff` on HTML, bytes-body responses, dashboard open before the read gate, event filters, sortable Time header, uppercase remote-health status rendering, control variable forms, `nutControl` exposure in the config summary, and marker guards for the asset-level surfaces with no browser in CI (`[hidden]` reset, resize-safe graph, wide-history range/paging, delete-selected, drill-down, Light/Dark/System theme) |
| Packaging | nFPM file list, package install paths, wrapper execution, OCI image smoke tests |

## End-to-end tests

The E2E suite runs on every pull request to `main` and every push to `main`. It is intentionally heavier than unit testing because it starts the same kinds of services Eneru depends on in production: NUT, SSH, Docker targets, real config files, and SQLite-backed state.

The E2E environment lives under `tests/e2e/` and uses Docker Compose:

```text
Eneru under test
  -> NUT dummy server
  -> SSH target container
  -> target containers and test mounts
```

The workflow is split into six parallel matrix groups:

| Group | Focus |
|-------|-------|
| CLI | Validation, bare command safety, one-shot output |
| UPS Single | Single UPS events and shutdown paths |
| UPS Multi | Independent UPS groups and local-drain policies |
| Redundancy | Quorum behavior, advisory triggers, and runtime NUT-visibility regressions |
| Stats | SQLite, graphs, events, notification coalescing |
| Loopback | Containerized local-host ownership through root and sudo SSH loopback, including generated local VM/container/sync/unmount actions |

The scenario files simulate online, on-battery, low-battery, FSD, brownout, overload, hot-grid, and nominal-voltage-mismatch states.

### E2E test inventory

The numbered E2E tests are defined in `tests/e2e/groups/*.sh`. There are 56 numbered tests, two redundancy runtime regression cases, plus one CLI completion smoke check.

| Test | Group | What it proves |
|------|-------|----------------|
| 1 | CLI | Main E2E config validates against the running test environment |
| 2 | UPS Single | Normal online state keeps the daemon running and does not trigger shutdown |
| 3 | UPS Single | Low battery triggers the dry-run shutdown sequence and exits cleanly |
| 4 | UPS Single | Remote SSH shutdown command reaches the target container |
| 5 | UPS Single | UPS `FSD` flag triggers immediate shutdown handling |
| 6 | UPS Single | Brownout detection logs the expected voltage event and startup threshold context |
| 7 | UPS Single | Apprise notification test command can deliver through the configured secret-backed URL when available |
| 8 | CLI | Multi-UPS config validates while both dummy UPSes are reachable through NUT |
| 9 | UPS Multi | UPS1 failure is isolated while UPS2 remains online |
| 10 | UPS Multi | Both UPSes online does not create false multi-UPS shutdowns |
| 11 | CLI | Validation rejects local-resource ownership on a non-local UPS group |
| 12 | CLI | Bare `eneru` is safe and shows help instead of starting shutdown behavior |
| 13 | CLI | `eneru monitor --once` prints a usable one-shot UPS snapshot |
| 14 | UPS Multi | Concurrent failure of both UPSes triggers grouped shutdown behavior |
| 15 | UPS Multi | Non-local UPS failure triggers only that UPS group context |
| 16 | UPS Multi | `drain_on_local_shutdown=true` logs and executes the drain path |
| 17 | UPS Multi | Default no-drain behavior skips the drain path |
| 18 | UPS Multi | Power restored before shutdown logs recovery and avoids shutdown |
| 19 | UPS Multi | `shutdown_order` runs remote targets in ordered phases with parallel servers inside a phase |
| 20 | CLI | Redundancy-group validation accepts valid quorum config and rejects invalid `min_healthy` |
| 21 | Redundancy | Quorum holds when one of two UPSes remains healthy |
| 22 | Redundancy | Both UPSes critical exhaust quorum and fire redundancy shutdown |
| 23 | Redundancy | Default unknown-state handling is surfaced and does not fire in healthy steady state |
| 24 | Redundancy | Fail-safe redundancy shutdown fires when the group is effectively unsafe |
| 25 | Redundancy | Cross-group topology does not cascade into redundancy shutdown while quorum holds |
| 26 | Redundancy | Redundancy members log advisory trigger mode instead of immediate local shutdown |
| 27 | Redundancy | Separate Eneru-host UPS plus remote rack redundancy topology fires only the remote rack group |
| R1 | Redundancy | Brief runtime loss of fresh NUT data recovers inside connection grace without redundancy shutdown |
| R2 | Redundancy | Persistent runtime loss of fresh NUT data still fires redundancy shutdown after connection grace expires |
| 28 | Stats | SQLite stats DB is created with samples and daemon-start event rows |
| 29 | Stats | Stats writer failure is non-fatal and does not crash monitoring |
| 30 | Stats | `monitor --once --graph` renders a graph from persisted stats data |
| 31 | Stats | `monitor --once --events-only` reads SQLite events and enforces event verbosity tiers |
| 32 | Stats | Voltage nominal auto-detect re-snaps misreported NUT nominal voltage and records a silent event |
| 33 | UPS Single | `voltage_sensitivity` avoids 120 V hot-grid false alarms while preserving real brownout detection |
| 34 | Stats | Pending on-battery and restored notifications coalesce into one brief-outage summary |
| 35 | Stats | Single-UPS restart lifecycle sends one restart notification instead of stop/start noise |
| 36 | UPS Multi | Multi-UPS coordinator applies the same single-restart-notification contract across per-UPS stores |
| 37 | Redundancy | Two consecutive quorum-loss events both fire shutdown — proves the daemon-managed flag-file lifecycle (issue #4) |
| 38 | Redundancy | A stale redundancy flag from a prior daemon start is cleared before quorum-loss shutdown |
| 39 | UPS Single | On-battery stabilization ignores transient low runtime after a fresh transfer |
| 40 | UPS Single | Remote SSH healthcheck reaches the test target without sending shutdown commands |
| 41 | CLI | Manual remote shutdown dry-run executes no configured remote commands |
| 42 | CLI | Manual confirmed remote shutdown reaches only the selected target |
| 43 | UPS Single | `/health`, `/ready`, `/metrics`, `/api/v1`, and JSON 404 endpoint discovery respond from the embedded API |
| 44 | UPS Single | An unreachable remote target is reported as a bounded best-effort failure instead of stalling shutdown |
| 45 | UPS Single | MQTT status publishing reaches the broker and includes power-quality fields |
| 46 | UPS Single | The OCI image runs against the E2E NUT server with the API enabled only by CLI flags, and serves the browser dashboard (`/`, `/app.js`) |
| 47 | Loopback | Containerized local-host ownership delegates VM, compose/container, sync, unmount, and host shutdown actions through the root loopback path and `/ready` is green |
| 48 | Loopback | Containerized local-host ownership delegates the same action set through a non-root SSH user with `use_sudo: true` |
| 49 | Loopback | Missing `/etc/machine-id` bind mount keeps `/ready` false with a setup hint |
| 50 | Loopback | Docker/Podman local capabilities with explicit no-loopback config fail startup |
| 51 | CLI | `eneru user`/`apikey` lifecycle round-trips against a real bcrypt + SQLite auth DB (create/list/show/passwd/delete, key create/list/revoke), and never leaks a hash or key |
| 52 | UPS Single | API auth: login issues a bearer token, `/api/v1/config` is sanitized for anonymous and extended for authenticated callers, bad credentials are 401, and an anonymous write is rejected 401 |
| 53 | UPS Single | UPS control: `nut_control` without auth is rejected at startup (fail-closed), with auth a disallowed command is 403, an unauthenticated control call is 401, and an allowlisted command reaches NUT (the dummy driver returns `CMD-NOT-SUPPORTED`, proving the request crossed the API -> upsd boundary) |
| 54 | UPS Single | Config hot-reload: SIGHUP applies a threshold change live, the authenticated `/config/reload` endpoint returns a report (anonymous is 401), and a broken config is rejected without dropping the daemon |
| 55 | UPS Single | Browser dashboard: the embedded API serves the SPA shell and assets with a strict CSP, and rejects path traversal / unknown assets with 404 |
| 56 | UPS Single | Event management: a wide-range `/api/v1/events` query returns source-qualified rows, an authenticated `DELETE` removes a real event (anonymous is 401), and a history `from > to` is 400 |
| E1 | CLI | Bash, zsh, and fish shell completion output is syntactically usable |

Every commit on the protected workflow has to prove the daemon works against real services. That means real NUT sockets, Dockerized SSH targets, a live SQLite database, rendered TUI output, validated production-shaped configs, and a full shutdown orchestration run. None of it depends on local developer state.

## Run E2E locally

Use the Python venv for Eneru commands, but Docker Compose provides the services.

```bash
source /tmp/eneru-venv/bin/activate

ssh-keygen -t ed25519 -f /tmp/e2e-ssh-key -N ""
cp /tmp/e2e-ssh-key.pub tests/e2e/ssh-target/authorized_keys

docker compose --project-directory tests/e2e up -d --build
sleep 10

upsc TestUPS@localhost:3493
python -m eneru validate --config tests/e2e/config-e2e-dry-run.yaml
python -m eneru run --config tests/e2e/config-e2e-dry-run.yaml --exit-after-shutdown

docker compose --project-directory tests/e2e down -v --remove-orphans
```

The GitHub workflow scripts under `tests/e2e/groups/` are the source of truth for exact CI steps.

## Documentation build

Build the ReadTheDocs site locally from the same uv venv:

```bash
source /tmp/eneru-venv/bin/activate
mkdocs build --strict
```

Serve it locally:

```bash
mkdocs serve
```

## Adding tests

For code changes:

- Add focused unit or integration tests under `tests/`.
- Add or update E2E coverage in `.github/workflows/e2e.yml` and `tests/e2e/groups/` for new features.
- Update example configs and docs when config keys change.
- Update this page if test counts, workflow groups, or test responsibilities change materially.

For documentation-only changes, a strict MkDocs build is usually enough.
