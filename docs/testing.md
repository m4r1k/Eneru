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

## Pre-release code review (v6.1.6)

The 6.1.x line is, by design, a massively stabilizing release series. Rather
than chase new features, each point release has been anchored by an in-depth,
full-repository code review performed with **Anthropic Claude Fable 5** — the
same broad-then-deep, adversarial method as the v6.0.0 pass above, run again
and again across the series so regressions and latent hazards are found and
fixed before they reach a real outage.

v6.1.6 is, on paper, a bugfix release — and a point release is emphatically
*not* where dozens of fixes across the whole tree normally belong. It carries
this much deliberately: the maintainer wanted Eneru operating at the best
possible level rather than spreading the work across several minors or
deferring it, so the entire remediation landed in one advertised bugfix
instead.

The work came out of a full-repository adversarial audit in the same spirit as
the v6.0.0 pass above — source, tests, CI, packaging, deployment manifests, and
config parsing all read in full at HEAD, not just a release diff. The findings
clustered into four systemic patterns:

- **Triplicated shutdown orchestration that drifts.** The single-UPS,
  coordinator, and redundancy paths each carried near-copies of the loopback
  success predicate, the marker-before-validation ordering guard, and the name
  sanitizers, and a fix applied to one copy had repeatedly been missed in the
  others. Consolidated into shared helpers (`select_loopback_results`,
  `loopback_poweroff_sent`, `poweroff_command_parts`, one `sanitize_name`).
- **Validation gaps that surface as raw tracebacks.** Scalar/null config
  sections, non-numeric thresholds, and unknown keys in legacy config shapes
  crashed or silently defaulted instead of producing a clean error. Every
  parse path now guards its shape and sweeps unknown keys.
- **Concurrency / clock hazards in the daemon loop.** Wall-clock deltas,
  modulo-clock log throttles, non-serialized reloads, and `time.monotonic`
  cooldowns seeded at `0.0` (which never fire on a fresh boot) were replaced
  with monotonic timing, `None` sentinels, and a reload lock.
- **Documented guarantees CI didn't enforce.** The ≥95%-per-file coverage bar
  lived only in prose, the `integration` pytest marker selected zero tests, a
  required E2E check could pass while SKIPping its core assertion, and CI
  installed a hand-picked dependency subset that drifted from `pyproject`.
  Each is now enforced by a CI gate.

The Critical tier was the shutdown decision path: a SIGTERM aborting an
in-flight single-UPS poweroff, a coordinator writing the "recovered" marker
before validating the poweroff command, and delegated-loopback success being
misclassified as failure. As in v6.0.0, every code finding ships with a
regression test that fails against the pre-fix behavior, per-file coverage
stays at or above 95%, and the AI review bots (CodeRabbit, cubic) ran over the
combined change set before merge.

## Pre-release code review (v6.1.7)

v6.1.7 continues the series with another full-repository pass with **Claude
Fable 5** — this time a parallel fan-out of independent specialist reviews (a
five-axis code review, a security audit, and a test-coverage analysis) merged
into a single go/no-go decision, with the whole unit suite (thousands of tests
at ≥95% per-file coverage) executed for real as part of the review.

The pass returned an initial no-go on three config-loader Criticals that all
shared one root cause: the loader trusted YAML scalar types and container
shapes, so a scalar `mounts` char-split into per-letter mount paths, a
templated `"false"` boolean stayed truthy-armed, and a missing explicit
`--config` path started the daemon on all-default, shutdown-armed config. Those
were fixed as a single declarative schema gate, and the remediation then worked
outward:

- **Shutdown path.** Silent notification and host-poweroff failures are now
  observable (buffer-and-replay, exit-code checks that clear the false
  "recovered" marker); the graceful-wait loops are POSIX-portable so they work
  on dash/BusyBox remotes; and a containerized multi-UPS loopback delegate no
  longer double-issues the poweroff inside its own container.
- **Security surface.** Host-header validation closes a DNS-rebinding read of
  the anonymous API, MQTT and NUT credential handling is hardened, and the
  login throttle gains a global ceiling — all without weakening the trusted-LAN,
  no-TLS-by-design posture.
- **Performance.** A bounded API server, a cached energy block, and a dashboard
  that fetches its heavy event scans only on demand stop the daemon from being
  DoSed (including by its own dashboard) during an incident.
- **Release pipeline.** A dedicated RHEL 8 RPM, a `:latest` tag promoted only
  after the image is verified, and a smoke install from the freshly-published
  apt/dnf repositories.

As in every pass, each code finding ships with a regression test that fails
against the pre-fix behavior, per-file coverage stays at or above 95%, and the
GitHub-side AI reviewers ran over the combined change set before merge.

## Pre-release code review (v6.2.0)

v6.2.0 ran the same three-reviewer fan-out, this time over a release that
added a config editor and a live-probing `config check`. The code and security
reviews found three High issues (shutdown commands that built SSH differently
from the health probe, a stale self-test result arming the failed-test
trigger, and real outages mislabelled as self-tests), plus a tail of hardening
items in the API, remote execution and the editor's save path.

The test-quality review changed method: instead of reading coverage reports it
**mutated the source** (about 290 mutants across concurrency, config, API and
E2E). Coverage was already ≥95% on every file, yet roughly one mutant in three
survived. Think of a smoke alarm that is installed in every room (coverage) but
was never tested with smoke (mutation). The survivors were turned into tests
that fail against their mutant, among them:

- per-route auth on every UPS control write, and strict auth on writes;
- flush → marker → poweroff ordering and the SIGTERM join budget;
- lock races proven with a thread stalled inside the critical section;
- check/runtime parity for the sudo prefix and the catalog bounds;
- E2E assertions that could pass vacuously (an FSD test that other triggers
  satisfied, multi-UPS isolation tests that never checked the healthy side,
  UNKNOWN-quorum tests that never produced an UNKNOWN member).

It also found one real bug the suite could not: a config reload that disabled
notifications during a shutdown could crash the sequence before the host
poweroff.

## CI layout

| Workflow | File | What it checks |
|----------|------|----------------|
| Validate | `.github/workflows/validate.yml` | Unit tests, coverage, example config validation |
| Integration | `.github/workflows/integration.yml` | Wheel install, `.deb`, `.rpm`, and package layout |
| E2E Tests | `.github/workflows/e2e.yml` | Real NUT/SSH/Docker behavior across grouped scenarios |
| Release | `.github/workflows/release.yml` | Release package build |
| PyPI | `.github/workflows/pypi.yml` | PyPI publish from release tags |

The protected `main` branch requires the `3.9` and `3.12` validate jobs plus the eight E2E matrix jobs.

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
pytest --cov=src/eneru --cov-report=term
```

<!-- ISS-050: there is no `pytest -m integration` selector. Cross-component
integration coverage is the shell-based E2E suite under tests/e2e/ (run by
the E2E workflow), not pytest-marked tests, so the ghost marker was removed
rather than advertised. -->



Validate example configs:

```bash
for config in examples/*.yaml; do
  python -m eneru validate --config "$config"
done
```

## Dashboard visual verification

The browser dashboard (`src/eneru/web/`) is static assets talking to the
daemon's JSON API. Unit tests cover the asset-serving surface, but they cannot
catch layout, theming, or chart-rendering regressions. **Any change under
`src/eneru/web/` should be screenshot-verified against a live daemon before
pushing** — this is strongly recommended, not optional.

There are two browser harnesses. Think of the preview as trying on a new shirt
in the mirror, while the deployed audit photographs the shirt that actually
left the shop:

- `tools/dashboard-preview.py` serves the working-tree assets and proxies every
  `/api/*` call to a running daemon. Use it before committing UI code.
- `tools/dashboard-audit.py` visits a deployment directly, screenshots every
  visible tab and safe submenu, inventories select options, checks a mobile
  viewport, and records console, HTTP, request, and accessible-name findings in
  `report.json`. Use it after building or deploying an image.

Playwright ships in the `dev` extra; fetch the browser once:

```bash
uv pip install -e ".[dev]"   # inside the uv venv (includes playwright)
playwright install chromium

# With a daemon running on :9191 (curl -s 127.0.0.1:9191/api/v1/ups -> 200):
python tools/dashboard-preview.py                 # all tabs, light + dark
python tools/dashboard-preview.py --themes light --tabs overview,battery

# Audit the exact static assets served by a deployed instance:
python tools/dashboard-audit.py --url http://eneru-host:9191 \
  --out /tmp/eneru-dashboard-audit --capture-scopes

# Authenticated read-only audit (password is prompted, never passed on argv):
python tools/dashboard-audit.py --url https://eneru.example.test --username admin
```

Read the resulting `dash-*.png` files to confirm the change renders; the script
also exits non-zero on browser/runtime findings. The deployed audit never clicks
UPS control commands, self-tests, variable writes, event deletion, config
reload, or shutdown. See the `dashboard-preview` skill
(`.claude/skills/dashboard-preview/`) for the complete agent workflow.

## Test areas

| Area | Coverage |
|------|----------|
| Config loading and validation | YAML parsing, defaults, enum validation, multi-UPS inheritance (including per-UPS energy tariff/nominal-watt overrides, explicit-null clearing, single-error unknown-key reporting, and oversized-number rejection), local ownership, loopback delegation config shape, redundancy rules, per-step `use_sudo` type checks, the non-root sudo warning (absolute sudo path, disabled servers, redundancy-group remotes), option-like remote user/host rejection, non-finite depletion `critical_rate` |
| Monitor state machine | OL/OB transitions, one-time warning when configured `nominal_power` exceeds the reported watt rating, self-test power attribution and continuing-OB reclassification, failed-test latch and delayed later-outage trigger re-arming after a successful unknown-status interval, FSD, failsafe, shutdown trigger order, dry-run behavior, first-sight self-test baseline seeding (stale device results never arm the latch), attribution that ends with its battery interval, polls bounded to 1 s behind a NUT control command, flush → marker → poweroff order, the in-flight flag set at trigger admission, and a reload that nulls the notification worker mid-sequence still powering off, T1-T4 and stabilization at threshold-1/threshold/threshold+1, a real 0% charge or 0 s runtime firing, and an FSD-only poll keeping the FAILSAFE latch, connection grace timed on the monotonic clock (a +1 h wall step does not expire it), an API self-test ticket not announced or attributed while its `upscmd` is in flight, a partial poll not seeding the self-test baseline, and the missing charge/runtime warning throttled to once per interval |
| Shutdown mixins | VMs, containers, compose files, filesystem sync and unmounts, remote SSH phases (including final-command exit code/response capture for regular and loopback remotes, and worker-start failures that still reach the loopback poweroff), remote pre-shutdown action rendering, loopback delegate bracketing (Phase A pre-actions → regulars → Phase C poweroff), full-lifecycle loopback progress, partial-drain failure reporting, exception isolation across phases, dry-run + per-server notification paths, one shared SSH argv builder (split `-i`/`-p` options, a flag and its value in one item split identically on the shutdown path, remote health and `config check`, `--` before the destination), per-step `use_sudo` overrides and the shared sudo-prefix rule, the loopback poweroff honoring `use_sudo`, the remote poweroff reserve under hung pre-commands (fake clock), Phase C still sent after a Phase B crash, VM names with spaces/quotes, bounded local unmount, VM waits on a virtual clock |
| CLI inspection vs runtime | `python -m eneru validate` shutdown-sequence tree, `python -m eneru remote list` ORDER + last-known HEALTH columns, `python -m eneru shutdown remote` drill, container legacy-path rewrite — all partition `is_host_loopback` delegates out of `compute_effective_order` and invoke `_prepare_runtime_config` / `_load_config` so the inspection output matches what the daemon would execute |
| Multi-UPS coordinator | Group routing, `is_local`, drain policy, local shutdown locking, trigger-specific progress reasons, shared poweroff outcomes for concurrent handoffs (including owner failure), positional startup-event compatibility, signal handling, coordinator send → flush → marker → poweroff order, a reload disabling notifications mid-shutdown (native and delegated-loopback paths), the in-flight SIGTERM join sharing one budget, drain/SIGTERM join deadlines on the monotonic clock, the local-shutdown lock proven with a thread stalled inside the guard, and peers stopped before their sequences on drain |
| Redundancy runtime | Quorum evaluation, advisory triggers, connection-grace handling, member data age on the monotonic clock (a ±1 h wall-clock step keeps healthy members healthy and fires no shutdown), idempotent group execution, aggregate member telemetry with configured watts overriding reported ratings, exact group plans, and live phase/remote progress snapshots including coordinator-callback failure, and the executor lock proven with a thread stalled inside the critical section |
| Health monitoring | Voltage thresholds, AVR, bypass, overload, battery anomaly filtering, depletion safeguards (>100% clamp, 2-sample minimum, inclusive window edge), exact-edge anomaly/voltage/staleness windows |
| Notifications | Plain-language power-event/status formatting with raw database preservation, retry queue, lifecycle classification, coalescing, suppression rules, atomic metadata preservation across memory replay/reload, container restart/upgrade stop-row deferral, deferred stop delivery claim/recovery races, reload-worker claim races, and mark-sent failure logging, `stop()` halting a large due-row drain, and a real two-worker reload handing buffered rows over |
| Statistics and TUI | SQLite schema (including the v5 `events.id` table rebuild, v6 notification claim recovery, v7 energy tables/columns, and v8 nominal-real-power columns with preservation, aggregate-tier queries, and replay-safe reopen coverage), self-test queries and conservative OB/OL history repair that excludes device observations and aborted results, stale-claim recovery failure propagation, aggregation, event tier filtering, wide-range + composite-cursor event paging across duplicate timestamps, `delete_events` (id+ts+type guard, dedup, per-DB isolation), TUI grouping, graphs, one-shot monitor output, events kept for the hourly retention, exclusive raw purge cutoff, inclusive tier edges, non-finite NUT readings stored as NULL |
| Observability | API routing, signed-in-only redacted remote shutdown detail, readiness, Prometheus escaping, power-quality metrics, remote-health sidecars, MQTT publishing, remote-health flap re-alerts, DEGRADED → HEALTHY recovery, and a reload `stop()` interrupting the interval wait |
| Authentication | User/API-key SQLite store (bcrypt hashing, salt uniqueness, truncation, CRUD), `eneru user`/`apikey` CLI lifecycle, password-input safety (getpass/generate/stdin), lazy bcrypt import, the 12-character minimum for new passwords |
| API auth middleware | Session manager (TTL/expiry), tiered authorization matrix (reads open vs `require_for_reads`, writes fail-closed when auth off), bearer/API-key resolution, session re-validation against user state (deleted user or password reset signs out; DB error preserves the session), login/logout, transition-only throttle auditing, Host-rejection warn-once, body-size + JSON validation including total body-read deadlines, read-error mapping, and closing keep-alive after an unread body, tiered `/config`, a wall-clock request-header deadline and localhost-reserved connection slots, write auth failing closed on an auth-DB error while reads keep the session |
| Event management API | `DELETE /api/v1/ups/{name}/events` — authed delete + `EVENTS_DELETED` audit, anonymous 401 / auth-off 403, unknown UPS 404, stats-unavailable 503, malformed-body matrix (400) and oversize (413); monitor/coordinator routing to the live per-UPS store; events `from`/`to`/`before` paging and history `from > to`/`All` validation, audit event types protected on delete, audit events and remote-check errors hidden from anonymous readers |
| UPS control | `upscmd`/`upsrw` wrappers and output parsing (including PTY output on NUT errors), fixed-binary argv validation before subprocess execution, username/password pairing before PTY prompt handling, command/variable allowlist enforcement, per-group credential/allowlist overrides, feature-disabled and unknown-UPS handling, NUT-error mapping, self-test write-before-command state, duplicate refusal, repeated result polling, restart recovery, and terminal notification deduplication, fail-closed config validation (control requires auth), value sanitization, audit logging to the events table, every mutating route refusing anonymous callers (401 auth on / 403 auth off) without touching NUT, a `running` self-test row at exactly the timeout no longer blocking |
| Config hot-reload | Strict load+validate (bad YAML / non-mapping / validation error rejected, running config kept), safe-vs-restart classification, in-place live apply across shared + per-monitor configs, subsystem reload hooks for stats/notifications/MQTT/remote-health, SIGHUP handler and API `/config/reload` endpoint |
| Periodic reports | Daily/weekly/monthly scheduling and deduplication, retry after gather/render/enqueue failures, shared event/self-test/energy/restart windows, exclusive duration boundaries, missing-period fallback, poll/tier-aware sparse-sample handling, compact labeled UPS rows, estimated-energy markers, and aggregate multi-UPS totals, no second fire for a run stamped exactly at the occurrence |
| Web dashboard | Static asset serving via `importlib.resources`, MIME mapping, path-traversal rejection, strict CSP + `nosniff` on HTML, bytes-body responses, dashboard open before the read gate, human-readable NUT status/event labels with priority and retained vendor tokens, event filters, sortable Time header, uppercase remote-health status rendering, fleet-level summary counts and blank-telemetry handling, explicit chart-source persistence, typed UPS/redundancy-group scoping including empty groups, group-safe Control behavior, state-colored shutdown progress and polling, live trigger health counts, remote timing/pre-command lines and the response/exit-code pop-up, stale asynchronous Control render rejection, control variable forms, `nutControl` exposure in the config summary, Power-tab line-quality handling for AVR `BOOST`/`TRIM` versus binary bypass/overload states, deployed-audit CLI parsing/safety helpers, and marker guards for the asset-level surfaces with no browser in CI (`[hidden]` reset, resize-safe graph, wide-history range/paging, delete-selected, drill-down, Light/Dark/System theme) |
| Config editor and checker | `tests/test_config_check.py` (static findings, section classification, NUT/SSH/local probes with mocks, the remote check script executed locally, sudo semantics, power-loss plan, report rendering, `eneru config` CLI), `tests/test_config_doc.py` (byte-identical ruamel round trip for every example and E2E config, comment placement for new keys/sections, legacy-to-multi-UPS conversion, atomic 0600 saves with `.bak`), `tests/test_config_catalog.py` (drift guard: every loader dataclass field has an explained catalog entry with the same default), `tests/test_config_tui.py` (every page, row, prompt and key through the model, curses rendering with a fake window). Also: check/runtime sudo-prefix parity, builtins and compounds flagged under sudo, `static_findings` wiring (loopback contract, privilege, delegate synthesis), probe `timeout` wrapper and marker-only parsing, every numeric catalog bound accepted by the loader, an all-defaults document validating clean, save refusing a symlink swapped in after load, MQTT broker credentials masked, `nan`/`inf` and padded text rejected or trimmed in the editor, per-UPS-only triggers hidden under redundancy groups, and duplicate YAML keys warned by `config check` |
| Packaging | nFPM file list, package install paths, pre-3.9 interpreter guard, RHEL 8 retirement (no el8 build, frozen el8 repo kept out of default metadata), exact-version release smoke contracts, safe artifact selection, readable Actions-ref policy, parallel native AMD64/ARM64 OCI smoke tests with a required aggregate gate |

Dashboard regression tests are split by responsibility:

- `tests/test_dashboard.py` covers fleet summary rendering, explicit and
  persistent chart sources, typed UPS/redundancy-group view scoping, group-safe
  Control behavior, empty-group rendering, shutdown progress surfaces and state
  colors, stale Control render rejection,
  blank fleet telemetry, readable NUT/event labels, retained custom status
  tokens, and tablet-width comparison-table access.
- `tests/test_dashboard_tools.py` covers the deployed-audit CLI contract,
  focused drill-down planning, authentication-aware HTTP findings, unique
  scope captures, and structural fallbacks.

Self-test and notification regressions are split by responsibility:

- `tests/test_self_test.py` covers result normalization and persisted tickets.
- `tests/test_monitor_core.py` and `tests/test_monitor_periodic.py` cover power
  attribution, delayed triggers, scheduling, snapshot-consistent result polling,
  and passive device observations.
- `tests/test_notifications.py` covers durable queueing, replay, and retries.

## End-to-end tests

The E2E suite runs on every pull request to `main` and every push to `main`. It is intentionally heavier than unit testing because it starts the same kinds of services Eneru depends on in production: NUT, SSH, Docker targets, real config files, and SQLite-backed state.

The E2E environment lives under `tests/e2e/` and uses Docker Compose:

```text
Eneru under test
  -> NUT dummy server
  -> SSH target container
  -> target containers and test mounts
```

The workflow is split into eight parallel matrix groups (v6.1 split the
two long poles — `UPS Single` and `Redundancy` — each into two so the
matrix wall-clock is bounded by a smaller slowest group):

| Group | Focus |
|-------|-------|
| CLI | Validation, bare command safety, one-shot output, auth CLI, NUT name autodiscovery, `config check` / config editor, `use_sudo` custom commands, split `ssh_options` (tests 1, 8, 11–13, 20, 51, 58, 65–67, 69, E1) |
| UPS Single Core | Single UPS events, shutdown paths, manual remote drills, embedded API, MQTT, unconditional remote PATH augmentation, real Compose timeout shutdown, shutdown re-arm (tests 2–7, 33, 39–46, 59, 60, 70) |
| UPS Single Auth | v6.0 auth, UPS control, hot-reload, dashboard, event management, self-test attribution and failed-test outage trigger, authenticated remote shutdown output (tests 52–56, 62, 64) |
| UPS Multi | Independent UPS groups, local-drain policies, shutdown ordering, multi-UPS restart notification (tests 9, 10, 14–19, 36) |
| Redundancy Quorum | Quorum behavior, advisory triggers, and group shutdown observability (tests 21–27, 37, 38, 63) |
| Redundancy Regression | Runtime NUT-visibility regressions (R1, R2) |
| Stats | SQLite, graphs, events, notification coalescing, restart notification (tests 28–32, 34, 35) |
| Loopback | Containerized local-host ownership through root and sudo SSH loopback, including generated local VM/container/sync/unmount actions, verified delegated-poweroff completion markers, strict container SSH trust, and the in-container config editor (tests 47–50, 57, 61, 68) |

In the inventory below the **Group** column shows the original coarse
focus area; *UPS Single* rows now run in **UPS Single Core** or **UPS
Single Auth**, and *Redundancy* rows in **Redundancy Quorum** (21–27,
37, 38, 63) or **Redundancy Regression** (R1, R2), per the table above.

The scenario files simulate online, on-battery, neutral/unknown, low-battery, FSD, brownout, overload, hot-grid, nominal-voltage-mismatch, and passive self-test states.

### E2E test inventory

The numbered E2E tests are defined in `tests/e2e/groups/*.sh`. There are 70 numbered tests, two redundancy runtime regression cases, plus one CLI completion smoke check. Every group script must end with its `=== Group '<name>' completed successfully ===` line and have a matrix entry in `.github/workflows/e2e.yml`; a workflow step checks both.

| Test | Group | What it proves |
|------|-------|----------------|
| 1 | CLI | Main E2E config validates against the running test environment |
| 2 | UPS Single | Normal online state keeps the daemon running and does not trigger shutdown |
| 3 | UPS Single | Low battery triggers the dry-run shutdown sequence and exits cleanly |
| 4 | UPS Single | Remote SSH shutdown command reaches the target container |
| 5 | UPS Single | UPS `FSD` flag triggers immediate shutdown with the exact FSD reason; the scenario keeps charge and runtime above the thresholds so no other trigger can fire |
| 6 | UPS Single | Brownout detection logs the expected voltage event and startup threshold context |
| 7 | UPS Single | Apprise notification test command can deliver through the configured secret-backed URL when available |
| 8 | CLI | Multi-UPS config validates while both dummy UPSes are reachable through NUT |
| 9 | UPS Multi | UPS1 failure triggers only UPS1's group (its SSH target is drained) and never UPS2's |
| 10 | UPS Multi | Both UPSes online keep the coordinator running (killed by the timeout) without false multi-UPS shutdowns |
| 11 | CLI | Validation rejects local-resource ownership on a non-local UPS group with a non-zero exit |
| 12 | CLI | Bare `eneru` exits 0 with argparse help and never starts the daemon |
| 13 | CLI | `eneru monitor --once` prints the per-UPS block (UPS header and resources), not only the banner |
| 14 | UPS Multi | Concurrent failure of both UPSes triggers both groups' shutdown |
| 15 | UPS Multi | Non-local UPS2 failure completes as a non-local group and leaves UPS1's group and SSH target untouched |
| 16 | UPS Multi | `drain_on_local_shutdown=true` logs `Draining all UPS groups` and executes the drain path |
| 17 | UPS Multi | Default no-drain behavior skips the drain path |
| 18 | UPS Multi | Power restored before shutdown logs recovery and avoids shutdown |
| 19 | UPS Multi | `shutdown_order` runs remote targets in ordered phases with parallel servers inside a phase |
| 20 | CLI | Redundancy-group validation accepts valid quorum config and rejects invalid `min_healthy` |
| 21 | Redundancy | Quorum holds when one of two UPSes remains healthy |
| 22 | Redundancy | Both UPSes critical exhaust quorum and fire redundancy shutdown |
| 23 | Redundancy | UPS1 critical plus UPS2's NUT driver killed: once UPS2's connection grace expires it is `unknown`, `unknown_counts_as: critical` counts it as failed, and the quorum-loss tally and redundancy shutdown prove it |
| 24 | Redundancy | Both NUT drivers killed while online: the fail-safe redundancy shutdown fires with both members `unknown` in the quorum-loss tally |
| 25 | Redundancy | Cross-group topology: the evaluator runs and sees UPS1's advisory trigger, and quorum holds (no cascade into redundancy shutdown) |
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
| 41 | UPS Single | Manual remote shutdown dry-run executes no configured remote commands |
| 42 | UPS Single | Manual confirmed remote shutdown reaches only the selected target |
| 43 | UPS Single | `/health`, `/ready`, `/metrics`, `/api/v1`, and JSON 404 discovery respond; a real NUT low-battery event publishes the exact per-UPS plan plus terminal phase/remote progress |
| 44 | UPS Single | An unreachable remote target ends in a clean exit with the exact `0/1 succeeded` summary within the time bound instead of stalling shutdown |
| 45 | UPS Single | MQTT status publishing reaches the broker and includes power-quality fields |
| 46 | UPS Single | The OCI image runs against the E2E NUT server with the API enabled only by CLI flags, and serves the browser dashboard (`/`, `/app.js`) |
| 47 | Loopback | Containerized local-host ownership delegates VM, compose/container, sync, unmount, and host shutdown actions through the root loopback path and `/ready` is green; an in-container `eneru config check` fills a missing `expected_host_identity` from the container's copy of the identity file and matches the host |
| 48 | Loopback | Containerized local-host ownership delegates the same action set through a non-root SSH user with `use_sudo: true` |
| 49 | Loopback | Missing `/etc/machine-id` bind mount keeps `/ready` false with a setup hint |
| 50 | Loopback | Docker/Podman local capabilities with explicit no-loopback config fail startup |
| 51 | CLI | `eneru user`/`apikey` lifecycle round-trips against a real bcrypt + SQLite auth DB (create/list/show/passwd/delete, key create/list/revoke), and never leaks a hash or key |
| 52 | UPS Single | API auth: login issues a bearer token, `/api/v1/config` is sanitized for anonymous and extended for authenticated callers, bad credentials are 401, and an anonymous write is rejected 401 |
| 53 | UPS Single | UPS control: `nut_control` without auth is rejected at startup (fail-closed), with auth a disallowed command is 403, an unauthenticated control call is 401, and an allowlisted command reaches NUT (the dummy driver returns `CMD-NOT-SUPPORTED`, proving the request crossed the API -> upsd boundary) |
| 54 | UPS Single | Config hot-reload: SIGHUP applies a threshold change live (`triggers:<ups>` in the applied list), the authenticated `/config/reload` endpoint returns a report (anonymous is 401), and a broken config is rejected without dropping the daemon |
| 55 | UPS Single | Browser dashboard: the embedded API serves the SPA shell and assets with a strict CSP, human-readable status/event formatting ships while API status stays raw, and path traversal / unknown assets are rejected with 404 |
| 56 | UPS Single | Event management: a wide-range `/api/v1/events` query returns source-qualified rows, an authenticated `DELETE` removes a real event (anonymous is 401), and a history `from > to` is 400 |
| 57 | Loopback | Containerized remote SSH with **no** `ssh_options` relies on the built-in `StrictHostKeyChecking=accept-new` default: it learns the host key on the first probe into `/var/lib/eneru/ssh/known_hosts`, reaches `HEALTHY`, and preserves the first learned trust entries after the container is recreated |
| 58 | CLI | NUT name autodiscovery (issue #71) lists a single exposed UPS with `upsc -l`, auto-corrects the runtime poll target, and logs the `ups.name` fix hint |
| 59 | UPS Single | Unconditional remote PATH augmentation resolves a Synology-only bare command without per-server configuration |
| 60 | UPS Single | A real local Compose stack is removed through Eneru's `down -t <seconds>` shutdown path, using the per-file `stop_timeout` |
| 61 | Loopback | A real coordinator/list-form delegated poweroff reaches the SSH target, skips in-container poweroff, and persists a `sequence_complete` recovery marker |
| 62 | UPS Single | The daemon starts on `online-charging` (no `ups.test.result`), waits for the empty self-test baseline, then applies `self-test-passed`: the first device result is recorded, not adopted. Passive running/failed self-test states are recorded and attributed without ordinary outage alerts; failed tests that remain on battery are reclassified as outages, and a later outage after a successful unknown-status interval fires the delayed failed-test trigger without an OL poll |
| 63 | Redundancy | The live API proves per-UPS nominal-watt override/global inheritance beating NUT's reported `ups.realpower.nominal` (with exactly one above-rating warning), anonymous readers never receiving raw remote output, redundancy member + aggregate energy/failover-load telemetry, the exact group shutdown plan, and terminal phase/remote progress after real quorum loss |
| 64 | UPS Single | A real remote shutdown (harmless command exiting 3 on the SSH target) keeps anonymous progress sanitized while a signed-in read returns `exitCode: 3` and the command's output |
| 65 | CLI | `eneru config check` against the real NUT server and SSH target: NUT name/login, SSH, `sudo -n -l`, PATH-augmented binaries, and a missing tool/runtime flagged red; a custom command that sudo would refuse is named; a wrong UPS name lists the real ones; the custom command and shutdown command are never executed (a world-writable marker catches even a non-sudo `shutdown` run) |
| 66 | CLI | `eneru config` TUI driven through a pty (`tests/e2e/config-tui-driver.py`): an in-place edit changes one line, keeps the comment and writes `.bak`; a new file is 0600, explained, validates, and passes the live NUT check |
| 67 | CLI | `use_sudo: true` sends a custom pre-shutdown command as `sudo -n ...` (a root-only `touch` succeeds), and without `use_sudo` it is sent as-is and fails as the SSH user |
| 68 | Loopback | `docker exec -it <ctr> eneru config` saves a writable config bind mount in place (same host inode, operator comment kept), keeps the `.bak` 0600 in the state volume, the container hot-reloads on SIGHUP, and a `:ro` mount opens as `[read-only]` and stays untouched |
| 69 | CLI | A real remote shutdown with split `ssh_options` (`-i KEY -p PORT`) reaches the target, and `config check` reaches it with the same options |
| 70 | UPS Single | Shutdown re-arms on POWER_RESTORED: OB -> OL -> OB records two `EMERGENCY_SHUTDOWN_INITIATED` rows (bug #4; numbered 34 before 6.2.0, which collided with the Stats test) |
| E1 | CLI | Bash, zsh, and fish shell completion output is syntactically usable |

Every commit on the protected workflow has to prove the daemon works against real services. That means real NUT sockets, Dockerized SSH targets, a live SQLite database, rendered TUI output, validated production-shaped configs, and a full shutdown orchestration run. None of it depends on local developer state.

## Run E2E locally (human authorization required)

Do not run the Docker-backed E2E suite locally unless a human explicitly
authorizes it for the current task. This includes Docker Compose setup,
execution, and teardown. Authorization from an earlier task does not carry
forward. Use GitHub Actions by default.

When a human has authorized a local run for the current task, use the `uv`
virtualenv created in [Local test environment](#local-test-environment) for
Eneru commands; Docker Compose provides the services.

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
