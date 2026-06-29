# Changelog

All notable changes to Eneru are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [6.1.0-rc8] - 2026-06-29

A round of real-world dashboard polish from running rc7 on production hardware.

### Added

- `energy.nominal_power` — a rated W/VA used to estimate watts (and kWh/cost)
  from load% when the UPS reports **neither** `ups.realpower` nor
  `ups.power.nominal` (common on integrated appliances). Threaded through the
  status block, the `/power` series, reports, and kWh integration.

### Changed

- **Energy windows are now calendar-based:** "today" is since local midnight and
  "month" is since the 1st — fixed boundaries that match an electricity bill,
  not a rolling 24 h / 30 d. The status block carries `todayLabel` / `monthLabel`
  so the UI states the window explicitly.
- **Charts** label the metric + unit (e.g. "Input voltage (V)") so the axis
  numbers are identifiable when switching input/output voltage, frequency, etc.
- **Event markers** are now a small color-coded triangle pinned to the time axis
  with a faint hover-only guide (was a bold full-height line in the plot color):
  outage/danger red, recovery green, warnings amber, everything else violet — no
  longer the same blue as the data line. The whole column is hoverable.
- **Energy tab** reads cleaner: the month line is hidden until it has data; a
  configured-but-not-yet-computed cost shows "calculating…" instead of
  "unknown"; the blunt "partial (data gaps)" row became a gentle footnote; and
  the window is stated inline.
- **Config tab** shows only enabled features (no "disabled" noise) plus a
  collapsible, pretty-printed JSON view of the (sanitized) configuration.
- The UPS detail modal closes on a backdrop click (not only the ✕), and the
  Events tab scrolls back to the top when you change the range/source/type
  filters so a big table doesn't strand you mid-page.

## [6.1.0-rc7] - 2026-06-29

Addresses an external review plus three more AI-review passes on rc6, and a round
of real-world dashboard feedback.

### Fixed

- **Critical:** the self-test write path (`POST /api/v1/ups/{name}/self-test`,
  the dashboard "Run self-test" button, and `eneru self-test run`) called an
  undefined `_store_for_ups` and returned 500. It now resolves the per-UPS stats
  store from the live monitor set.
- The `eneru self-test` CLI opened its `StatsStore` without `open()`, so
  `--direct` recorded nothing and `status` always read empty; it now opens (and
  closes) the connection. `--direct` also honors per-UPS `self_test.command`.
- Config validation checks `self_test.command` against each UPS's **resolved**
  `nut_control.allowed_commands` (catching a per-UPS-narrowed allowlist) and
  rejects non-numeric `battery_health` values.
- `Schedule.interval` keeps fractional seconds (`0.5` no longer truncates to a
  permanently-due `0`).
- Replacement prediction fires for an already-below-threshold battery regardless
  of history depth (the history guard ran first before).
- `discover_self_test_command` raises on a transient `upscmd -l` failure
  (distinct from "not exposed"); the scheduler discovers before stamping last-run
  (so a blip retries instead of burning a cadence) and skips when the stats store
  is unavailable.
- Multi-UPS reports are a true daemon-wide digest with a per-UPS section each
  (not just the first monitor); "Running since" survives long uptimes; the send
  timestamp is stamped **after** enqueue.
- API self-test preflights `upscmd -l`; audit writes a typed `CONTROL_SELF_TEST`
  event; single-UPS battery-health / energy reads no longer compute the whole
  fleet's status.
- `self_test` / `reports` are reclassified **SAFE** for hot-reload (their due
  checks read config live each tick — no registered scheduler to re-init).

### Changed (dashboard)

- **Energy tab** is now a dual-line **load% + power (W)** chart via the new
  `GET /api/v1/ups/{name}/power`; cost rows render even when configured-but-unknown
  (the misleading "set cost_per_kwh" hint no longer shows once it's set).
- Range **and the selected UPS** are shared across the Power / Battery / Energy
  tabs; chart loads guard against stale async races; the charts keep working
  through overlapping refreshes.
- Chart event markers are hoverable along the whole vertical guide (not just the
  3px dot), and the tooltip carries the full event description (type, time, detail).
- Monochrome (grayscale) emoji on the tab labels; chart event markers **and** the
  default Events view are restricted to tier-1 power events (no daemon-start /
  upgrade noise) with descriptive tooltips; the event-type dropdown closes on an
  outside click; switching tabs scrolls to the top of the panel; a hidden-tab
  fallback re-syncs the URL hash; the keyboard focus cue on panels is restored.

### Other

- bash/zsh `self-test` completion branches; `pytest-xdist` added to the declared
  dev dependencies; the AGENTS Actions pin table refreshed to the current SHAs;
  the Ubiquiti/TLS-only `upsc` limitation documented (troubleshooting + roadmap
  backlog).

## [6.1.0-rc6] - 2026-06-28

### Added

- **Tabbed dashboard (Nutify-style).** The web UI is now a tabbed SPA —
  Overview / Power / Battery / Energy / Events / Control / Config — built as
  real ARIA tabs (roving tabindex, arrow-key nav, URL-hash routing), still
  vanilla JS + SVG with no build toolchain. Per-tab charts gain **input-voltage
  threshold bands** (a reference overlay of the live config) and **power-event
  overlay markers** (color-coded, capped so dense windows stay readable), and
  the Battery/Energy tabs surface the v6.1 score / replacement / self-test and
  kWh / cost widgets directly.
- **Self-test CLI.** `eneru self-test run` (defaults to the daemon API so the
  daemon owns the audit + record; `--direct` issues via NUT with no daemon) and
  `eneru self-test status`. Both honor the `nut_control` allowlist. Shell
  completions updated.
- **Feature documentation.** New `battery-health`, `self-test`, `reports`, and
  `energy-tracking` pages (and mkdocs nav).
- **More history metrics.** `/history` now serves output voltage, input/output
  frequency, battery voltage, temperature, and real power (already aggregated
  across all retention tiers) so the new charts plot them at every range.

### Fixed

- Reports honor `format: csv` on delivery — the CSV block now rides under the
  text summary instead of being silently dropped.
- Per-UPS `battery_health` / `self_test` / `nut_control` overrides now
  hot-reload live (they are read each tick by the v6.1 resolvers) instead of
  being misclassified as restart-required.
- The dashboard self-test button debounces, so a double-click can't enqueue
  multiple non-idempotent hardware self-tests.
- E2E: `apply_scenario` is now a hard synchronization point (fails the test on a
  >20s unconfirmed apply rather than running against stale dummy state); Test 55
  asserts the tab nav is served and exercises real path-traversal payloads.
- Energy `_median` now averages the two middle values for even-length series, so
  the inferred sample spacing (and thus `integrate_kwh`'s gap cap) isn't inflated.
- The battery-health status block is cleared on a reload to
  `battery_health.enabled: false` instead of leaving a stale score on the API.
- An auto-learned nominal runtime is no longer persisted when the config already
  pins `nominal_runtime_seconds`.
- The self-test API and the in-loop self-test task honor per-UPS
  `self_test.command`; `/self-test` is hidden from `availableEndpoints` unless
  auth + `nut_control` are both on; and an in-flight self-test is persisted to
  `meta` and recovered after a restart so a `running` row is never orphaned.

## [6.1.0-rc5] - 2026-06-28

### Added

- **Battery intelligence.** A composite battery-health score (0-100) built
  from five terms — capacity (runtime trend), runtime-vs-nominal, last
  self-test, confirmed anomalies, and battery age — each carrying an
  availability flag so missing telemetry reports as *unknown* rather than a
  false high score. Least-squares replacement prediction warns (once per
  horizon) when the score is trending toward the configured threshold. New
  `battery_health` config section (per-UPS overridable) and a
  `BATTERY_REPLACEMENT_PREDICTED` event.
- **Energy tracking.** kWh integration from `ups.realpower` (falling back to
  `load% × power.nominal`, flagged "estimated") with daemon-down gaps capped,
  plus optional cost with per-currency formatting. New `energy` config section;
  `cost_per_kwh` unset disables cost tracking entirely.
- **Scheduled self-test.** Issues a UPS battery self-test on a schedule and
  records the normalized result, gated behind the same `nut_control` allowlist
  + API auth as manual control. New `self_test` section (per-UPS overridable),
  an auth-gated `POST /api/v1/ups/{name}/self-test`, and a dashboard trigger.
  Adapts to whatever `upscmd -l` exposes; self-disables otherwise.
- **Periodic reports.** Daily/weekly/monthly digests (events, battery health,
  energy, uptime; optional CSV) delivered through the notification channel.
  New `reports` section.
- **Shared periodic scheduler** (`scheduler.py`) underpinning the above, with
  last-run state persisted in the stats `meta` table so infrequent jobs fire
  correctly across restarts.
- **Surfacing.** New status blocks (`batteryHealth`, `energy`, `selfTest`) on
  the API/MQTT status, Prometheus gauges (`eneru_ups_battery_health_score`,
  `eneru_ups_energy_kwh`/`_cost` with a `period` label, `eneru_ups_self_test_result`,
  `eneru_ups_replacement_days_remaining`; unknown series omitted), and the web
  dashboard detail view.

### Changed

- **Stats schema v7** (additive, idempotent): `real_power` / `power_nominal`
  sample columns (+ aggregates) and `battery_health` / `self_tests` tables.
- **CI / E2E speedup.** E2E image builds are cached with buildx + the GitHub
  Actions cache; the two long-pole groups are split into eight parallel matrix
  jobs; scenario switches poll-until-applied instead of fixed `sleep 3`;
  grace-tied redundancy waits are trimmed; and `validate` runs `pytest-xdist`
  with a reduced PR Python matrix. (Operator: branch-protection required-check
  names updated accordingly.)

## [6.1.0-rc3] - 2026-06-10

### Changed

- **Remote SSH host-key checking now defaults to `accept-new` (issue #73).**
  Previously a `remote_servers` entry with no `ssh_options` inherited OpenSSH's
  `StrictHostKeyChecking=ask`, which fails closed under `BatchMode` when a host
  key is unknown — so a fresh remote could never connect on first contact, and
  the rc2 workaround (hand-run `ssh-keyscan` + `StrictHostKeyChecking=yes`) was
  easy to get wrong (a missing/empty/mismatched `known_hosts` failed closed
  silently until a power event). Eneru now injects
  `StrictHostKeyChecking=accept-new` into any remote that does not set a
  `StrictHostKeyChecking` directive of its own. Bare-metal installs use the
  running user's normal `~/.ssh/known_hosts`; Docker/Podman containers use the
  documented `/var/lib/eneru/ssh/known_hosts` path; Kubernetes samples set
  `ENERU_SSH_KNOWN_HOSTS_FILE=/var/lib/eneru/known_hosts` because SSH keys live
  in a read-only Secret and learned trust belongs on the writable PVC. Any
  explicit `StrictHostKeyChecking` or `UserKnownHostsFile` you set is preserved
  verbatim, including the loopback delegate's `no`. No `ssh_options` are needed
  for the common case.
- **Containers keep the existing SSH mount contract.** Docker/Podman still use
  `/srv/eneru/ssh:/var/lib/eneru/ssh`; the directory is writable so
  `accept-new` can write `known_hosts`, while the private key files remain mode
  `0400`. The shipped Kubernetes Deployment
  (`deploy/kubernetes/remote-deployment.yaml`) backs `state` with a
  `PersistentVolumeClaim` (instead of `emptyDir`) so learned keys — and the
  stats/auth databases — survive pod restarts; it uses the `Recreate` strategy
  to avoid two pods contending for the ReadWriteOnce volume. The container docs
  drop the manual `ssh-keyscan` setup accordingly, and E2E Test 57 proves the
  no-`ssh_options` default learns and preserves trust across a container
  recreate.

## [6.1.0-rc2] - 2026-06-10

### Changed

- **Container SSH host-key setup (issue #73).** A container is a hotel room:
  anything learned interactively inside it disappears when the room is rebuilt.
  The container docs now tell operators to mount both the private key and a
  pre-seeded `known_hosts` file from `/srv/eneru/ssh`, then configure
  `UserKnownHostsFile=/var/lib/eneru/ssh/known_hosts` with
  `StrictHostKeyChecking=yes` so remote-server trust survives
  Docker/Podman/Kubernetes recreation without disabling host-key checks. The same
  pass makes the uid `10001` private-key ownership and mode guidance consistent
  across the container walkthroughs.
- **E2E coverage for issue #71 and issue #73.** The CI matrix now includes a
  real NUT autodiscovery regression that proves a wrong single-UPS name
  self-heals for the running session, plus a container SSH regression that uses
  a mounted private key and mounted `known_hosts` with strict host-key checking
  across container recreation.

## [6.1.0-rc1] - 2026-06-09

### Added

- **NUT name autodiscovery (issue #71).** When a poll can't reach the configured
  UPS, Eneru now runs `upsc -l <host>` to list the UPS names the server actually
  exposes and logs them. If exactly one UPS exists and the configured name is not
  it (the classic case of a NUT *login username* placed where the UPS *device
  name* belongs), Eneru self-heals for the session and tells you to fix
  `ups.name`. With multiple UPSes it lists the choices instead of guessing. The
  operator-configured name, display, and on-disk state are never mutated.

### Changed

- **`expected_host_identity` auto-populates from any `cat /absolute/path`
  (issue #70).** Previously the container-side identity read was hardcoded to
  `/etc/machine-id`, so marker-file setups had to duplicate the value or mount
  over `/etc/machine-id`. Now a simple `host_identity_command: "cat /path"` reads
  the same path locally and fills in the expected value automatically. Non-`cat`
  commands still require an explicit `expected_host_identity`.
- **Quieter NUT polling.** `upsc` runs with `NUT_QUIET_INIT_SSL=true` and the
  benign `Init SSL without certificate database` line is filtered from failure
  output, so real errors stay visible.
- **Docs for non-systemd / no-`machine-id` hosts.** New end-to-end marker-file
  recipe for Alpine and other consumer/non-systemd setups, linked from the
  install, migration, and troubleshooting pages. deb and rpm remain the published
  packages; the OCI image stays a first-class citizen.

## [6.0.0] - 2026-06-04

v6.0 turns Eneru from a shutdown daemon with observability into an operator tool
you can drive from the browser. The API can now serve the dashboard, authenticate
users and API keys, run allowlisted UPS control actions, delete selected event
rows, and reload safe config changes without restarting the daemon.

The release also went through two pre-release audit rounds. The first audit
focused on the shutdown path: like checking that a fire door still opens after
you stack boxes beside it, it verified that config mistakes, slow drains, and
thread races could not block the final host poweroff. The second audit focused on
the new interactive surface: auth fail-closed behavior, dashboard state,
SQLite/resource handling, package contents, and CI/E2E coverage. The fixes from
both audits are included below.

### Added

- **Browser dashboard.** The embedded API now serves a no-build, no-third-party
  JavaScript dashboard when `api.enabled` is on. It includes live UPS cards,
  drill-down panels, redundancy rollups, a shutdown banner, SVG history graphs,
  event filters with multi-type selection and wide-range paging, signed-in
  event deletion, UPS control, and Light / Dark / System themes.
- **Tiered API authentication.** Local users and API keys live in a dedicated
  SQLite auth DB. User passwords are bcrypt hashes; API keys are stored as
  SHA-256 digests. Manage them with `eneru user ...` and `eneru apikey ...`.
  Reads stay open by default so Prometheus and status clients keep working;
  `api.auth.require_for_reads: true` gates reads too. Every write path requires
  auth, and UPS control cannot be enabled while auth is off.
- **New API endpoints and write paths.** Added `GET /api/v1/auth/state`,
  `POST /api/v1/auth/login`, `POST /api/v1/auth/logout`,
  `POST /api/v1/config/reload`, `GET /api/v1/ups/{name}/commands`,
  `POST /api/v1/ups/{name}/command`, `GET /api/v1/ups/{name}/variables`,
  `PUT /api/v1/ups/{name}/variables/{var}`, and
  `DELETE /api/v1/ups/{name}/events`.
- **UPS control via NUT.** `nut_control` wraps the existing `upscmd` and `upsrw`
  tools for allowlisted instant commands and writable variables. Passwords are
  answered through a pseudo-terminal instead of being placed on argv.
- **Event management.** Events now have stable, never-reused IDs. `/api/v1/events`
  supports `from`/`to` range filters and source-qualified cursor paging, and the
  dashboard can delete selected rows while preserving rows that the server did
  not confirm as deleted.
- **Config hot-reload.** `systemctl reload eneru`, `SIGHUP`, `docker kill -s HUP`,
  and authenticated `POST /api/v1/config/reload` can apply safe changes live:
  trigger thresholds, `nut_control`, notifications, MQTT, remote health,
  Prometheus, and stats retention. Unsafe changes are reported as
  restart-required. A bad reload keeps the previous config running.
- **Auth packaging.** Added the `auth` extra for bcrypt. Debian packages depend
  on `python3-bcrypt`, the OCI image bundles it, and RPM recommends it because
  EL package availability varies.

### Removed

- **NUT auto-discovery**, previously listed for 6.0, was dropped: it duplicates
  `nut-scanner` and does not fit Eneru's config-first model.

### Fixed

- **Dashboard first-user sign-in without restart.** Creating the first user with
  `eneru user create` now refreshes the API's effective-auth probe immediately,
  so the dashboard can show Sign-in and accept login without restarting the
  daemon.
- **Dashboard runtime display.** The web UI now formats UPS runtime as seconds,
  minutes, or hours/minutes in cards, drill-down details, and runtime graph
  labels while keeping API responses as raw seconds.
- **Shutdown audit hardening.** A group can no longer abort host poweroff by
  self-joining its own monitor thread during multi-UPS drain. Local drain phases
  are best-effort, bounded filesystem sync prevents a hung mount from wedging
  the sequence, remote pre-shutdown commands cannot spend the final poweroff
  budget, and SIGTERM/SIGINT now waits a bounded, config-derived deadline when a
  shutdown is already in flight.
- **Config validation before outage-time crashes.** Shutdown thresholds,
  depletion settings, extended-time settings, drain timeouts, duplicate UPS
  names, unknown per-UPS/per-redundancy keys, and empty `local_shutdown.command`
  values are now rejected at load instead of failing during an outage.
- **Redundancy and failsafe behavior.** Redundancy groups wait for present
  members to publish an initial snapshot before making a cold-start quorum
  decision. On-battery hard NUT failures are debounced by
  `max_stale_data_tolerance`, and the on-battery failsafe runs once per outage
  instead of repeating every poll in dry-run, delegated, or non-halting configs.
- **T3 depletion trigger at slow poll intervals.** The depletion-rate trigger no
  longer needs a fixed 30 samples. It now derives the needed sample count from
  `depletion.window / check_interval`, with a floor of two samples, so slower
  polling does not silently disable the fast-drain trigger.
- **API/auth hardening.** Request bodies are size-limited and read with a socket
  deadline. Dynamic auth fails closed when an existing auth DB cannot be
  inspected. Password resets and user deletion invalidate active user sessions.
  Write/control paths re-check the account strictly and fail closed during auth
  DB errors; reads stay lenient unless `require_for_reads` is enabled.
- **Dashboard correctness.** Redundancy cards use server-computed quorum health,
  advisory member triggers do not show a false shutdown-imminent banner while
  quorum is intact, event filtering and paging are source-exact, control panel
  rebuilds are cached without losing half-typed variable values, and auth state
  cleanup now clears stale tokens, event selections, and UPS-control UI together.
  The event table Time header can now toggle chronological order, and remote
  health rows from the API's uppercase status constants render as reachable
  when healthy.
- **Remote operator output.** `eneru remote list` now includes last-known remote
  health from the daemon sidecar when remote health is enabled, without running
  SSH probes. The non-root `use_sudo` warning now focuses on Eneru-generated
  privileged actions and plain shutdown commands, so custom NAS admin commands
  that work without sudo do not produce a misleading warning.
- **SQLite and deferred notifications.** Stats open failures no longer leave a
  half-open connection, failed flushes requeue samples, readonly opens return
  `None` on SQLite failures, `_safe_alter()` only swallows duplicate-column
  errors, and monitor/coordinator shutdown closes stats handles. Deferred stop
  notification delivery now claims rows as `delivering` until Apprise succeeds,
  with stale-claim recovery on daemon startup.
- **Packaging, docs, and CI coverage.** Dashboard assets and new Python modules
  are included in both pip package data and nfpm package contents. Requirements
  include MQTT/auth extras where needed. E2E coverage exercises auth, UPS
  control, event deletion, and config reload against the Docker Compose NUT/SSH
  environment. The Single UPS control E2E uses the dummy NUT server's control
  credential, and PTY-backed `upscmd`/`upsrw` failures preserve NUT's response
  text for diagnostics.

### Notes For Operators

- With auth disabled, the API remains read-only as in 5.x. The dashboard loads,
  but sign-in and write controls stay hidden.
- To enable dashboard login or UPS control, set `api.auth.enabled: true`, create
  a user with `eneru user create`, and configure `nut_control` allowlists.
- Existing stats databases migrate automatically on first start. As always, take
  a backup before upgrading production monitoring hosts.

---

## [5.5.1] - 2026-05-19

### Fixed

- Container restarts and image upgrades now send one lifecycle
  notification instead of "Service Stopped" followed by
  Restarted/Upgraded. Container SIGTERM is like one doorbell wired to
  several doors: Eneru cannot tell stop from upgrade, so it leaves the
  stop row pending for the next container to supersede.
- Added regression coverage for explicit legacy logging paths so
  migrated configs keep rewriting to `/var/{log,run}/eneru/...` inside
  containers.

---

## [5.5.0] - 2026-05-18

### Added

- Containerized local-host ownership for Docker and Podman through a
  host-loopback SSH delegate. Eneru can now run as the slim non-root
  container while still shutting down the host, VMs, containers, compose
  stacks, and configured filesystems on the host.
- Zero-config root loopback synthesis for Docker/Podman local configs:
  `host: 127.0.0.1`, `user: root`,
  `ssh_key_path: /var/lib/eneru/ssh/id_loopback`, and
  `shutdown_command: "shutdown -h now"`.
- `remote_servers[].use_sudo` for non-root SSH users. It prefixes
  generated privileged actions and the final shutdown command with
  `sudo -n` when needed.
- `remote_servers[].pre_shutdown_commands[].mounts` for the
  `unmount_filesystems` action, so ordinary remote servers can unmount
  specific filesystems before their final shutdown command. Loopback
  delegates still derive mounts from the local `filesystems.unmount`
  config.
- Loopback host identity validation using `/etc/machine-id`, surfaced in
  remote health and `/ready`.
- SQLite diagnostics events for slow NUT polls and successful but slow
  remote SSH health probes. These now appear in the TUI/API event stream at
  Diagnostics verbosity instead of only in journal/container logs.
- E2E coverage for root loopback, non-root sudo loopback, missing
  machine-id readiness, missing-loopback startup failure, and the generated
  VM/container/sync/unmount loopback action list.

### Changed

- The OCI image is now slim and relies on the host loopback path for
  local host actions instead of shipping Docker, Podman, and libvirt
  clients inside the container.
- Docker image healthchecks now target `/ready`, so orchestration checks
  the configured shutdown contract rather than only API liveness.
- `/ready` now evaluates shutdown capabilities, including native host
  binaries, remote SSH health, and loopback health for containerized
  local ownership.
- Container runtimes suppress `wall(1)` and the missing `logger(1)`
  warning because those native host side channels are not useful inside
  OCI containers.
- Kubernetes remains the remote-only container profile by default; local
  ownership in a pod requires an explicit loopback delegate.

### Fixed

- Synthesized host-loopback delegate now ships with
  `StrictHostKeyChecking=no` + `UserKnownHostsFile=/dev/null`. Without
  these the first SSH probe to `127.0.0.1` failed with
  "Host key verification failed" because the non-root container user has
  no `~/.ssh/known_hosts`.
- Legacy native-install paths (`/var/log/ups-monitor.log`,
  `/var/run/ups-monitor.state`, `/var/run/ups-battery-history`,
  `/var/run/ups-shutdown-scheduled`) now auto-rewrite to
  `/var/{log,run}/eneru/` equivalents under container runtime when the
  config still matches the dataclass default. Preserves the
  "no required YAML changes" migration promise for default configs.
  Operator-set paths are untouched. See
  [docs/migrate-to-container.md](migrate-to-container.md#legacy-logrun-dir-auto-rewrite)
  for opt-out.
- `docs/migrate-to-container.md` Step 6 had three bind mounts written
  as `,Z` (comma) instead of `:Z` (colon). On SELinux hosts Docker
  parsed the destination as the literal path `/var/lib/eneru,Z` and
  the real `/var/lib/eneru` inside the container stayed unmounted — so
  the carried-over stats DB at `/srv/eneru/state/default.db` was never
  reachable and the daemon created a fresh empty DB in the image's
  default directory instead. Corrected to `:Z`.
- Removed the per-startup "v5.5: running non-root inside <runtime>;
  local-host actions will be delegated to <user>@<host> via SSH" banner.
  In v5.5 the loopback path is taken regardless of euid, so the
  non-root vs root distinction was cosmetic and the banner spammed
  the logs on every restart. The privilege check still passes silently
  for the same scenario.
- Removed the legacy-path auto-rewrite banner entirely. The rewrite
  still fires (it preserves the migration promise), but it re-runs
  in-memory on every container restart and the banner was log noise.
  Behavior is documented in `docs/migrate-to-container.md`.
- Softened the non-loopback API auth-off log to an info note. With
  auth disabled in v6.0, write endpoints are disabled and
  `/api/v1/config` returns the sanitized anonymous view; operators
  still get a reminder to keep unauthenticated read endpoints on a
  trusted network.
- TUI (`eneru tui` / `eneru monitor`) now sees the same legacy-path
  auto-rewrite as the daemon inside a container. `_cmd_monitor` used to
  skip `_prepare_runtime_config`, so the TUI kept reading
  `/var/run/ups-monitor.state` while the daemon wrote to
  `/var/run/eneru/ups-monitor.state`, surfacing as "daemon not running"
  + "No data available" on the main UPS panel even though events,
  graphs, REST API, and Prometheus all worked. Moved the rewrite into
  `_load_config` so every subcommand inherits it without needing to
  remember a prepare call.

### Migration notes

- Native deb/rpm/pip installs upgrade without YAML changes.
- Existing remote-only container deployments upgrade without YAML
  changes.
- Docker/Podman containers that own local host actions must provide a
  working loopback SSH path and bind-mount the host `/etc/machine-id`
  read-only.
- Containers that drive **other** remote targets (NAS, secondary hosts)
  must add an explicit `ssh_key_path` to each `remote_servers` entry and
  bind-mount the operator SSH key into the container — root's
  `~/.ssh/id_rsa` is not visible from the eneru user. See
  [docs/migrate-to-container.md Step 2b](migrate-to-container.md#step-2b-migrate-existing-remote-server-ssh-keys).
- Bind-mounted SSH keys must be readable by uid 10001 inside the
  container. Hand the private key to uid 10001 with `chown 10001:10001`
  and keep it at `0400` or `0600`; the matching `.pub` can stay `0644`.
  Loosening the private key to `0644` to "make it work" exposes it to
  every local user on the host and isn't worth the convenience.
- To carry forward existing TUI graphs, event log, and notification
  history, copy `/var/lib/eneru/*.db` to the bind-mount source for
  `/var/lib/eneru` (e.g. `/srv/eneru/state/`) and `chown 10001:10001`
  the files before starting the container. Skip if a clean history
  is acceptable. See
  [Migrate to container](migrate-to-container.md).
- To remove the deb/rpm package after the container is healthy, copy
  `/etc/ups-monitor/config.yaml` to `/srv/eneru/config.yaml` and point
  the container's bind mount at the copy. Decouples the daemon's
  configuration from the package's file ownership. See
  [docs/migrate-to-container.md Step 3c](migrate-to-container.md#step-3c-detach-the-config-file-from-the-package).
- Do not use `authorized_keys command="..."` for loopback keys; it
  replaces Eneru's identity probe and generated shutdown actions.
- Non-root loopback users should set `use_sudo: true` and use the
  documented NOPASSWD sudoers stanza for all enabled delegated actions.

---

## [5.4.0] - 2026-05-15

Stable v5.4 release. This release adds the official container deployment path while keeping local host shutdown on native installs.

### Added
- Official GHCR image for remote-only Docker, Podman, and Kubernetes deployments. The release workflow publishes exact `<version>` tags, `latest` for stable releases, and `testing` for pre-releases.
- `eneru run --api`, `--api-bind`, and `--api-port`, so container healthchecks and Kubernetes probes can enable the read-only API without editing YAML.
- `remote_servers[].ssh_key_path` for SSH private keys mounted from Docker bind mounts or Kubernetes Secrets.
- Kubernetes `Deployment` and `Pod` examples with non-root security contexts, resource requests/limits, HTTP probes, SSH Secret mounts, and a `/var/log/eneru` volume for retained log files.

### Changed
- The OCI image is remote-only by design. Use native deb/rpm or PyPI installs when Eneru must stop local VMs, local containers, filesystems, or the host itself.
- Dependency checks now follow configured behavior: remote-only deployments no longer require local shutdown tooling, and the legacy `logger(1)` syslog side-channel is best effort.
- The container image uses the Python 3.12 slim Trixie base and runs `apt-get upgrade -y` during builds so release images pick up current Debian fixes.
- `/api/v1` now returns an endpoint index, and JSON 404 responses include the same endpoint list. When Prometheus is disabled, `/metrics` is omitted from both.
- The reference Grafana dashboard adds power-event annotations, a battery/load/runtime "now" row, a `$ups` selector, and a nominal-voltage overlay.
- `eneru validate` reports whether it is running in Docker, Podman, another container, systemd, or a bare process.

### Fixed
- Remote-health startup probes no longer record a noisy `REMOTE_HEALTH_HEALTHY` event for the initial `UNKNOWN -> HEALTHY` baseline next to every `DAEMON_START`. Startup failures and later failure/recovery transitions are still recorded.
- The SVG architecture diagram viewBox matches the drawn content width (782×550 instead of 960×550), removing the blank right-side margin in rendered docs.

### Migration notes
- Existing native installs can upgrade without YAML changes.
- Use `ghcr.io/m4r1k/eneru:latest` for the latest stable image, `ghcr.io/m4r1k/eneru:testing` for pre-releases, or pin `ghcr.io/m4r1k/eneru:<version>` for immutable production deployments.
- For local host shutdown, local VM/container teardown, or filesystem unmounts, keep Eneru on the host. The OCI image is for remote UPS monitoring, API/health endpoints, telemetry, and SSH shutdown of remote systems.

---

## [5.3.0] - 2026-05-10

Stable v5.3 release. Drop-in for v5.2 users in most cases — see Migration notes below for two behaviour changes that may need a small adjustment to existing dashboards or RPM-based MQTT setups.

### Added
- **Read-only observability.** The embedded API now serves `/health`, `/ready`, `/api/v1/ups`, `/api/v1/events`, `/api/v1/config`, `/api/v1/remote-health`, and Prometheus `/metrics`. API and MQTT payloads share the same status model, including stable `groupId` values, redundancy-group rows, event verbosity tiers, and power-quality fields.
- **Power-quality metrics.** API, MQTT, Prometheus, and the reference Grafana dashboard now expose input/output voltage, input/output frequency, battery voltage, UPS temperature, nominal voltage, derived warning thresholds, voltage state, AVR state, bypass state, and overload state.
- **Remote SSH health.** Remote health is enabled by default for configured remote servers. The daemon runs the safe SSH `probe_command` (`true` by default), writes live state plus sidecar JSON, marks failed probes `DEGRADED` before `FAILED`, records state transitions in SQLite events, and sends one failure notification per failed period plus one recovery notification.
- **Manual remote shutdown drill.** `eneru shutdown remote --server ...` exercises one configured remote through Eneru's SSH path. `--dry-run` executes no configured commands; real execution requires `--i-really-want-to-proceed-with-remote-shutdown`.
- **Target discovery and full-sequence rehearsal.** `eneru remote list` prints every configured remote target with its group, host, enabled flag, and effective shutdown order. `eneru shutdown group --group ...` rehearses the whole shutdown sequence (VMs, containers, filesystems, ordered remote shutdowns) for one UPS or redundancy group; dry-run by default and gated by `--i-really-want-to-proceed-with-group-shutdown` for real execution.
- **JSON/syslog/MQTT/Grafana surfaces.** JSON logs now carry structured fields where call sites provide them, syslog forwarding is configurable, MQTT reconnects with bounded backoff, and `examples/grafana-dashboard.json` imports with a Prometheus datasource variable.
- **Slow NUT poll visibility.** Slow `upsc` calls now produce rate-limited log lines, and notifications require consecutive slow full-poll cycles. Operators get a journal breadcrumb for NUT latency without alert noise from a one-off slow read.

### Changed
- **Redundancy shutdown re-arms correctly.** The redundancy executor's `/var/run/ups-shutdown-redundancy-{group}` flag is now daemon-managed. Stale flags are cleared at coordinator startup, quorum recovery, and graceful signal exit; active PID-owned flags are refused. Repeated quorum-loss events now fire independently instead of staying pinned after the first event. This closes the issue #4 class of failures.
- **Runtime NUT visibility honors connection grace.** Redundancy members with fresh prior data contribute `DEGRADED` during connection grace and become `UNKNOWN` only after the monitor marks the connection failed. This prevents short NUT flaps from bypassing the existing grace window.
- **TUI events are tiered by verbosity.** Default event views show Power Events first. `-v` / first `<V>` adds Diagnostics, `-vv` / second `<V>` adds Lifecycle, and `--length` now consistently caps event rows in one-shot output. Live grouping keeps Power Events visible before lower-priority rows.
- **On-battery stabilization.** Fresh on-battery transfers wait 30 seconds before charge, runtime, depletion-rate, or extended-time shutdown triggers can fire. FSD and on-battery connection-loss failsafe remain immediate.
- **Remote shutdown accounting is stricter.** Single-server phases use the same deadline-based worker path as multi-server phases, summaries count success/failure/timeout/worker-crash outcomes, and timeout logs include both configured and deadline-capped values.
- **MQTT packaging on RPM is soft.** Debian/Ubuntu packages still depend on `python3-paho-mqtt`. RPM packages recommend it because RHEL 8 and 10 packaging coverage is uneven; if MQTT is enabled without paho, Eneru logs a warning and keeps running.

### Fixed
- API and metrics fixes: single-UPS `/api/v1/remote-health` now reads the live manager, `/metrics` includes redundancy-group remote targets, every metric has `HELP`/`TYPE`, `/api/v1/events` caps `limit` at 10000, and API worker threads no longer block daemon shutdown.
- Config and validation fixes: non-mapping nested YAML falls back cleanly, unknown trigger/behavior keys are validation errors with suggestions, and unsafe `remote_health.probe_command` values with shell metacharacters are rejected.
- TUI and stats fixes: malformed remote-health sidecar rows are ignored, per-group height includes the remote-health row, recent events have deterministic timestamp ties, Ghostty sessions fall back to `xterm-256color` when `xterm-ghostty` terminfo is missing, and `StatsStore.from_connection()` no longer leaks an in-memory handle.
- Logging and docs fixes: syslog-init failures use the configured logger, non-loopback API warnings describe the sanitized config data accurately, manual-drill docs now match `--server` exact-name lookup and dry-run precedence, and SELinux/AppArmor troubleshooting notes were added.
- E2E hardening: Test 43 retries API endpoints, Test 44 uses nanosecond timing for bounded remote failure, Test 45 proves MQTT status payloads reach a broker with power-quality fields, and the v5.3 workflow grep is documented as a wiring guard.

### Migration notes
- No YAML changes are required. `remote_health.enabled` now defaults to `true`, but only configured remote servers are probed, and probes use the harmless `probe_command`. Set it to `false` if you do not want periodic SSH connectivity checks.
- If you were deleting `/var/run/ups-shutdown-redundancy-*` manually after tests or issue #4-style no-op shutdowns, stop doing that. The daemon owns those flags now.
- API, Prometheus, and MQTT remain read-only in v5.3. Authenticated control APIs are still planned for v6.
- **Prometheus no-data semantics**: `/metrics` now exports `NaN` for power-quality fields a UPS does not report (previously `0.0`). Alert rules and Grafana panels written against v5.2 that compared `eneru_ups_input_voltage < 200` would never fire on missing data; under v5.3 they correctly stay quiet (no false alarm) but a `< 200` comparison against `NaN` is also `false`, so an under-voltage alert on a UPS that doesn't report voltage at all will not fire either. If you alert on these fields, audit the rule with `absent()` or `eneru_ups_input_voltage == eneru_ups_input_voltage` to detect the missing-data case.
- **MQTT publishing requires `paho-mqtt`**: the dependency is a hard requirement on Debian/Ubuntu packages and on PyPI installs (`pip install eneru[mqtt]`). RPM packages list it as a soft `Recommends:` because RHEL 8/10 packaging coverage is uneven; if you enable `mqtt:` on RPM, install paho explicitly: `python3 -m pip install paho-mqtt` (use `--break-system-packages` on EL10 per PEP 668). If MQTT is enabled without paho, Eneru logs a warning and keeps running; the publisher just stays disabled.

---

## [5.2.2] - 2026-04-28

Bug-fix release. Drop-in upgrade.

### Fixed
- Shutdown trigger never re-armed after `POWER_RESTORED` when the
  local-shutdown command didn't actually halt the host (bug #4).
  Single-UPS and multi-UPS coordinator paths both fixed. Gated
  re-triggers now log a warning instead of returning silently.
- `eneru tui --graph voltage` silently ignored in interactive mode.
- Phantom 0 V samples squashed the voltage graph into a one-row strip
  at the top. Writer drops on-line `input.voltage <= 0` (real
  outages still record the dip); graph uses 5th/95th percentile bounds.
- Events panel showed daemon-lifecycle chatter instead of power
  events. Priority filter is now tiered: power events always survive
  the cap; daemon events fill remaining slots.
- `eneru tui --once --events-only` silently fell back to log parsing
  because the default 1 h window was empty for sparse events. Events
  no longer use a time window at all.

### Added
- `--verbose` / `-v`: include low-priority events. `<V>` toggles in
  the live TUI.
- `--length N`: cap events output (default 30, `0` = no cap).

### Changed
- `--time` and `<T>` apply to the graph only. Use `--length` for events.
- Events panel defaults to priority-only.
- Live TUI events cap raised (now `min(30, visible_panel_rows)` so
  power events stay inside the visible window on smaller terminals;
  `<M>` still expands to 500 rows for full scrollable history).

### Migration notes
- Scripts grepping `--events-only` output for low-priority event
  types: add `--verbose`.
- Scripts using `--time` to size events: switch to `--length`.

---

## [5.2.1] - 2026-04-24

Bug-fix release for two v5.2.0 regressions. Drop-in upgrade. See
`git log v5.2.0..v5.2.1` for per-commit detail.

### Fixed
- **Two notifications on every `systemctl restart` / package upgrade**
  instead of the v5.2-promised single `🔄  Restarted` / `📦  Upgraded`.
  At SIGTERM the old daemon now picks the cheapest correct path based
  on systemd intent (`systemctl show -p Job eneru.service`):
  `Job=stop` → ship eagerly (instant); `Job=restart` / unknown →
  enqueue + schedule a transient `systemd-run` timer to deliver ~15 s
  later, cancelled by the next daemon's classifier if a replacement
  comes up. Containers / K8s / foreground `eneru run` (no systemd) →
  always eager. New module `deferred_delivery.py` + hidden CLI
  subcommand `eneru _deliver-stop`.
- **`📦  Upgraded vunknown → v5.2.0` on RPM.** New `preinstall.sh`
  captures the outgoing version via `rpm -q eneru` before files unpack
  (RPM doesn't pass it in `$2` the way DEB does). Defensive fallback
  in `lifecycle.classify_startup` covers paths that bypass scriptlets.

### Documentation
- **`docs/notifications.md`** caught up with the v5.0 / 5.1 / 5.2
  architecture: SQLite-backed queue, exponential backoff, new config
  knobs, lifecycle classifier states, brief-power-outage / recovery
  coalescing. Comparison table extended to three columns (v4.6 / v4.7+ /
  v5.2+).

### Migration notes
None.

---

## [5.2.0] - 2026-04-24

Notifications get a rewrite. v5.1's were stateless and noisy: a `systemctl restart` emitted two unrelated events, a shutdown sequence emitted ~22 mid-flight "Shutdown Detail" lines that mirrored the log, and a power outage that took the internet down meant nothing was ever delivered. v5.2 makes them persistent, classified, and coalesced. See `git log v5.1.2..v5.2.0` for per-commit detail.

### Added
- **Persistent SQLite-backed notification queue.** Each notification is a `pending` row in the per-UPS stats DB. The worker thread reads and writes through SQLite, so messages survive process death, network outages, and reboots; pending rows ship in age order once the endpoint is reachable. Per-message exponential backoff (capped at `retry_backoff_max`, default 5 min) so the worker doesn't hammer an unreachable endpoint while it's down.
- **Stateful lifecycle classifier.** Replaces the unconditional "🚀  Started" with one of: `📦  Upgraded vX → vY`, `📊  Recovered` (resumed after a power-loss-triggered shutdown), `🔄  Restarted` (graceful exit, downtime under 30 s), `🚀  Restarted (fatal)`, `🚀  Started (last seen Nh ago)`, `🚀  Started (after crash)`, or plain `🚀  Started`. Uses an on-disk shutdown marker plus `meta.last_seen_version` in the stats DB.
- **Brief-outage coalescing.** A pending `ON_BATTERY` + `POWER_RESTORED` pair from the same outage gets folded into one `📊  Brief Power Outage: Ns on battery` summary before delivery. Same for the post-power-loss recovery: a `Recovered` notification absorbs the previous instance's pending shutdown headline + summary into one message that includes the trigger reason and the downtime.
- **Outage-survival config knobs:** `notifications.retention_days` (default 7, applies to sent/cancelled only; pending is never pruned by TTL), `max_attempts` (default 0 = unlimited; Apprise's bool can't distinguish "bad URL" from "internet down"), `max_age_days` (default 30, the only cap on pending), `max_pending` (default 10000, backlog overflow), `retry_backoff_max` (default 300, 5 min). Sized for a long weekend with the internet down.
- **`flush(timeout=5)` drain on shutdown.** Wired into every shutdown path (signal + sequence-complete, single-UPS + coordinator). Closes the v5.1 "1 message pending" SIGTERM race; whatever doesn't drain stays in SQLite for the next start.
- **TUI events panel: full date.** Rows render `YYYY-MM-DD HH:MM:SS` so multi-day events are distinguishable.

### Changed
- **Shutdown notifications: 22 → 2.** Dropped the per-`_log_message` auto-mirror. The channel now carries the headline (`🚨  EMERGENCY SHUTDOWN INITIATED!` with reason) and a single `✅  Shutdown Sequence Complete` summary at the end. The summary now also fires when `local_shutdown.enabled=false`. Per-step detail stays in journalctl.
- **`wall(1)` opt-in.** Defaults to off via `local_shutdown.wall: false`. Holdover from the v2 `ups-monitor` era when the shell was the only channel; Apprise covers the modern path.
- **Schema v3 → v4.** New `notifications` table + index; append-only migration heals partial state via `CREATE TABLE IF NOT EXISTS`. See `src/eneru/AGENTS.md` "Stats schema evolution".
- **`_send_notification` API.** Adds a `category` keyword (default `general`); used by the coalescer and per-category queries. The `blocking` parameter is now a back-compat shim because the v5.2 queue is always asynchronous.
- **Banner formatting cleanup.** Dropped the `========== BANNER ==========` padding from `monitor.py` and `redundancy.py`. The ALL CAPS body stays (grep-friendly).

### Removed
- `get_retry_count()` / `get_queue_size()` on `NotificationWorker`, replaced by `get_pending_count()`.
- `time.sleep(5)` at the local-shutdown gate, replaced by `flush(timeout=5)` which returns as soon as pending hits 0.
- Per-server "Remote Shutdown Sent" success notifications, covered by the aggregate summary; failures still notify.

### Migration notes
- **deb/rpm upgrades:** postinstall now drops `/var/lib/eneru/.upgrade_marker.json` before `systemctl restart`, so the next start emits a single `📦  Upgraded` notification. Pip users get the same effect via the `meta.last_seen_version` comparison.
- **Wall broadcasts:** if you relied on the v5.1 default of "wall fires on every shutdown", set `local_shutdown.wall: true` explicitly.
- **Stats DB:** schema bumps from v3 to v4 on first start. Idempotent and append-only; existing rows are preserved.

---

## [5.1.2] - 2026-04-23

Bug-fix release for issue #4: voltage warning thresholds were misleading on narrow-firmware UPSes (US 120V APC defaults, EU managed units). Drop-in upgrade for the common wide-firmware case. Sites with narrow firmware get a one-time startup warning and a documented migration tip. See `git log v5.1.1..v5.1.2` for per-commit detail.

### Added
- **`triggers.voltage_sensitivity` preset (per-UPS-group):** `tight` (±5%), `normal` (±10%, default, matches EN 50160), `loose` (±15%). Strict-enum validated. Per-UPS so a clean PDU and a generator-fed leg can use different bands in the same daemon.
- **One-time startup migration warning** with per-side delta when v5.1.1's algorithm would have produced a tighter band on the current UPS. Suppressed once `voltage_sensitivity` is set explicitly in YAML.

### Fixed
- **Voltage warning band misleading on narrow-firmware UPSes.** v5.1.1 picked the tighter of `nominal × (1 ± 0.10)` or `input.transfer.{low,high} ± 5V`, then unconditionally labelled the result `(±10% nominal, EN 50160 envelope)`. The log lied whenever the transfer-derived candidate won. On a 120V grid with APC firmware (transfer 106/127), the band landed at 111/122; routine 122.4V utility readings tripped `OVER_VOLTAGE_DETECTED` repeatedly. Threshold derivation is now a single percentage formula. Transfer points stay informational only, still printed on the second startup-log line and quoted in event messages.
- **Brownout / over-voltage notification text** no longer hardcodes `EN 50160 ±10% envelope`. The wording is now `outside the configured ±10% nominal band`, which stays accurate under any preset.

### Migration notes
None for wide-firmware UPSes (APC defaults of 170/280 on 230V, or no transfer points reported at all). Managed / narrow-firmware sites see the warning band widen on default (`220/240` → `207/253` on a 230V/215/245 unit). Set `voltage_sensitivity: tight` to restore approximately the old behaviour, or `voltage_sensitivity: normal` to acknowledge the new default and silence the startup warning.

---

## [5.1.1] - 2026-04-22

Bug-fix release with one small TUI improvement. Bundles fixes from a third-party AI code review (CodeRabbit Pro + Cubic.Dev) of the v5.1.0 codebase. Drop-in upgrade. See `git log v5.1.0..v5.1.1` for per-commit detail with reviewer attribution.

### Added
- **TUI events panel: full history with arrow-key scrolling.** Drops the 24h window; `↑/↓` scrolls one row, `PgUp/PgDn` ten, `Home/End` jumps to oldest/newest. `<M>` still toggles between 8 and 500 visible rows.

### Fixed
- **XCP-ng VM shutdown silently no-op'd.** `stop_xcpng_vms` passed UUIDs positionally to `xe vm-shutdown uuid=`; `xe` ignored them. Now bound via `xargs -I {}` with `force=true`.
- **Redundancy `is_local` quorum loss never powered off the host.** The executor stopped local services and remote peers but skipped the local poweroff command. Now delegates to the coordinator's `_handle_local_shutdown`.
- **Multi-UPS state-file write race.** `with_suffix('.tmp')` collapsed every monitor's atomic-rename temp file onto a shared name. Same fix in the battery-history persist path.
- **TUI live-blending key mismatch.** Graph right edges lagged ~10s behind SQLite because `_STATE_FILE_TO_COLUMN` used NUT's dotted lowercase names but the daemon writes uppercase keys.
- **`virsh list` failure during VM wait loop produced false success.** A wedged `libvirtd` made the wait loop report "all VMs stopped" and skip force-destroy. Non-zero exit is now treated as transient.
- **Voltage severity escalation never fired** when a brownout crossed the severe threshold AFTER the LOW state was already pending. Severity is now re-evaluated every poll.
- **Silent config drops.** `notifications.suppress`, `notifications.voltage_hysteresis_seconds`, and per-group `statistics` in multi-UPS mode were never read from YAML. All three now round-trip.
- **Legacy `ups-monitor` syslog tag** renamed to `eneru` so journalctl filtering matches the rest of the daemon's output.
- **Smaller follow-up fixes** across `shutdown/`, `health/`, `graph.py`, `logger.py`, `stats.py`, `utils.py`, `cli.py`, and bash completion.

### Security
- **`stop_compose` remote-shell injection.** Template double-quoting didn't block `$()`/backticks/`${...}`. `shlex.quote` now runs at the call site.
- **PyPI publish OIDC token scope.** Workflow split so `pip install build twine` can't reach the publishing token; the `workflow_dispatch` version input is validated against PEP-440 before any shell interpolation.
- **CI supply-chain.** Every third-party GitHub Actions invocation is SHA-pinned. nFPM version-pinned + checksum-verified. Dropped `git push -f` to gh-pages and `|| true` masks on dpkg/rpm install.

### Packaging
- **DEB lifecycle handling.** prerm/postrm rewritten with explicit `case` for the full Debian Policy enumeration; the if/elif cascade was stopping the service on every non-removal lifecycle (`failed-upgrade`, `deconfigure`, etc.).
- **postinstall fresh-vs-upgrade** disambiguated via `$2`; **chroot guard** on `systemctl daemon-reload`; `pyproject.toml` reads version from `eneru.version.__version__` so a future top-level `__init__.py` import can't break wheel builds.

### Examples
- Reference / dual-UPS configs no longer ship `StrictHostKeyChecking=no` as the default.
- `config-reference.yaml`: `compose_files: []` (was `compose_files:`, parsing as YAML null).
- `config-enterprise.yaml`: Slack URL switched to the documented Apprise webhook form.
- `config-homelab.yaml`: `parallel: false` does the OPPOSITE of "shut down LAST" — switched to `shutdown_order: 2` and corrected the misleading comment.

### Test quality
Autouse fixture redirects `StatsConfig` / `StatsStore` defaults to a per-test tmp_path so tests can't leak SQLite files into `/var/lib/eneru`. Failsafe / redundancy-evaluator tests now exercise real code paths; e2e shell scripts assert exit codes via `PIPESTATUS` instead of swallowing them.

### Migration notes
None.

---

## [5.1.0] - 2026-04-21

### Added
- **Redundancy Groups:** Protect resources fed by multiple UPS sources (dual-PSU servers, A+B feeds). Eneru only fires the group's shutdown when fewer than `min_healthy` member UPSes still report healthy.
    - `RedundancyGroupConfig` mirrors `UPSGroupConfig` in full (`remote_servers`, `virtual_machines`, `containers`, `filesystems`)
    - Configurable quorum (`min_healthy`, default `1`) plus `degraded_counts_as` and `unknown_counts_as` (default `critical` for fail-safe)
    - Per-UPS triggers become advisory for redundancy members; independent UPS groups remain byte-identical to single-UPS mode
    - `RedundancyGroupExecutor` composes the four shutdown mixins, inheriting multi-phase ordering verbatim
    - `eneru validate` summarises every redundancy group with its quorum policy
    - See `docs/redundancy-groups.md`
- **Per-UPS SQLite Statistics:** Every poll buffered in-memory; `StatsWriter` thread flushes to per-UPS DB every 10s with 5-min and hourly aggregation
    - Schema v3: 13 raw NUT metrics + Eneru-derived (`depletion_rate`, `time_on_battery`, `connection_state`) + `events.notification_sent` audit trail
    - Tier-aware retention: 24h raw + 30d 5-min + 5y hourly (~17 MB steady-state per UPS)
    - WAL mode, `synchronous=NORMAL`, `PRAGMA busy_timeout=500` for SD-card-friendly contention bounds
    - Failure isolation: SQLite outages cannot crash the daemon
    - See `docs/statistics.md`
- **TUI Graphs:** `BrailleGraph` renderer (Unicode U+2800-U+28FF; falls back to block characters on `LANG=C`)
    - New keybindings in `eneru monitor`: `<G>` cycles metric (charge / load / voltage / runtime), `<T>` cycles range (1h / 6h / 24h / 7d / 30d), `<U>` cycles UPS in multi-UPS mode
    - Y-axis labels with units, now/min/max stat header, `data: Xh of Yd requested` footer when sparse
    - Time-windowed X positioning so a 12h dataset in a 30d view stays in its actual time slice instead of stretching
    - Live deque blends SQLite + state-file snapshots so the right edge stays current between flushes
    - See `docs/tui-graphs.md`
- **TUI Events Panel from SQLite:** Reads each UPS's `events` table via `StatsStore.open_readonly`, merges across UPSes (sorted by timestamp; `[label]` prefix in multi-UPS), falls back to log-tail parser when no DB exists
    - New `eneru monitor --once --graph <metric>` and `--events-only` flags for scripts and CI
- **Multi-Phase Shutdown Ordering (`shutdown_order`):** Define shutdown phases for remote servers (#4)
    - Servers with the same `shutdown_order` run in parallel; different orders run sequentially (ascending)
    - Enables dependency chains: e.g. compute (1) → storage (2) → network (3)
    - Per-server `shutdown_safety_margin` (seconds, default `60`) replaces a hard-coded constant; raise for slow-flushing storage, set `0` to opt out
    - `eneru validate` shows a shutdown sequence tree with phase grouping
- **Voltage Monitoring (#27):** Auto-detect, hysteresis, severity bypass, grid-quality framing
    - **Auto-detect:** `input.voltage.nominal` snaps to the nearest standard grid (100/110/115/120/127/200/208/220/230/240V). After ~10 polls, observed median is cross-checked. On disagreement >25V the nominal re-snaps and a `VOLTAGE_AUTODETECT_MISMATCH` event is recorded. Catches the US-grid case where firmware reports 230V on a 120V UPS
    - **Grid-quality warnings:** `BROWNOUT_DETECTED` / `OVER_VOLTAGE_DETECTED` thresholds clamp to the *tighter* of EN 50160 / IEC 60038 ±10% and `input.transfer.{low,high}` ± 5V. Wide UPS firmware defaults (e.g. APC 170/280 on 230V) no longer mask real brownouts (now warn at 207/253). Managed UPSes with narrow transfer points keep their tighter thresholds
    - **Notification hysteresis:** `notifications.voltage_hysteresis_seconds` (default 30s) defers notification dispatch on mild deviations. State log line + SQLite event row are always immediate. Sub-30s flap is filtered with a `VOLTAGE_FLAP_SUPPRESSED` audit row
    - **Severity bypass:** Deviations >±15% from nominal bypass the dwell entirely with a `(severe, X.X% below/above nominal)` tag and an `Approaching UPS battery-switch threshold` callout
    - **Per-event suppression:** `notifications.suppress: [...]` mutes informational events. Safety-critical names (`OVER_VOLTAGE_DETECTED`, `BROWNOUT_DETECTED`, `OVERLOAD_ACTIVE`, `BYPASS_MODE_ACTIVE`, `ON_BATTERY`, `CONNECTION_LOST`, `SHUTDOWN_*`) are validator-rejected. `events.notification_sent` records what was actually delivered
    - No user-tunable voltage thresholds are exposed; a misconfiguration there would mask real over-voltage events
- **CLI:**
    - `eneru tui` is an alias for `eneru monitor`
    - `eneru completion {bash,zsh,fish}` prints a self-contained completion script (kubectl/helm pattern). nfpm.yaml drops them at the FHS paths so they auto-load when the host's completion framework is present. PyPI users source manually with `source <(eneru completion bash)`
- **Parallel E2E Matrix CI:** The single `e2e-test` job is replaced by four parallel matrix jobs (`E2E CLI`, `E2E UPS Single`, `E2E UPS Multi`, `E2E Redundancy and Stats`). 32 tests total. Wall-clock is bounded by the slowest group instead of the sum

### Changed
- **`shutdown_order` and `parallel` are mutually exclusive.** Setting both is a hard validation error (previously a warning)
- **Parallel-phase join is deadline-based.** Restores the "dead hosts don't block" guarantee from v4.6 (a phase with N stuck servers no longer waits up to `N × max_timeout`)
- **`MonitorState` exposes a lock-protected snapshot for cross-thread reads** via `snapshot() -> HealthSnapshot`. No behaviour change for legacy single-UPS deployments
- **`MultiUPSCoordinator` also routes when only `redundancy_groups` is set** (single UPS + 1 redundancy group is now legal)
- **TUI events panel decoupled from graph timescale.** Pressing `<T>` no longer re-queries the events list (uses fixed 24h window); `<M>` still toggles 8 vs 50 max rows
- **TUI clip-by-display-cell-width.** Emoji and CJK glyphs no longer spill past the gold panel's right edge
- **Wide-transfer UPSes now receive grid-quality notifications.** Operators with default-wide UPS firmware (e.g. APC 170/280 on 230V) previously got essentially zero `BROWNOUT_DETECTED` / `OVER_VOLTAGE_DETECTED` notifications because warnings fired only just before the UPS reacted. After 5.1.0 they fire at the EN 50160 ±10% band (207 / 253 on 230V). If your mains is consistently outside that envelope, you'll start seeing it

### Fixed
- **Right-edge artifacts in the TUI events panel.** Mobile SSH clients showed stray glyph fragments on the gold panel's right edge. Fixed by using `insch` for the bottom-right corner and padding event rows to full width
- **Proxmox `stop_proxmox_vms` / `stop_proxmox_cts` work for non-root SSH users (#4).** Templates now invoke `qm` / `pct` via `sudo`. Root-SSH setups unchanged (Proxmox VE ships sudo with `root NOPASSWD: ALL`); non-root users add a one-line sudoers entry — see [Passwordless sudo → Proxmox VE](remote-servers.md#proxmox-ve)

### Migration notes
- **Existing single-UPS configs continue to work unchanged.** `redundancy_groups:` is optional
- **Stats are on by default.** First start creates `/var/lib/eneru/<sanitized-ups-name>.db`. Override with `statistics.db_directory: <path>` for an SSD on Pi-class hardware. Schema migrations from earlier 5.x are automatic and idempotent (additive `ALTER TABLE` only)
- **Voltage notification behaviour change.** Default `voltage_hysteresis_seconds=30` mutes sub-30s flaps that previously emailed. Set `notifications.voltage_hysteresis_seconds: 0` to restore legacy fire-immediately behaviour. The log line + SQLite event row are always immediate regardless
- **Branch protection on `main` is a one-time manual operator step.** The old `e2e-test` required check is replaced by four `E2E ...` matrix checks. Update branch protection in repo settings to swap them
- **Storage on small devices:** per-UPS DB is ~17 MB steady-state. See `docs/statistics.md`, "Storage on small devices (Raspberry Pi / SD card)"

### Technical details
- New modules under `src/eneru/`: `health_model.py`, `redundancy.py`, `stats.py`, `graph.py`. All wired into `nfpm.yaml`
- Test counts: 751 unit tests across 28 files; 32 E2E scenarios across 4 parallel matrix jobs. New coverage in `tests/test_voltage.py` (grid snap, auto-detect re-snap, notification hysteresis, severity bypass, threshold clamp), `tests/test_stats.py::TestSchemaMigration` (additive `ALTER TABLE` path), and `tests/test_packaging.py` which asserts every `src/eneru/**/*.py` is referenced by `nfpm.yaml`
- Schema migration mechanic: `StatsStore._migrate_schema` applies append-only `ALTER TABLE` migrations gated by `_safe_alter` (idempotent). `meta.schema_version` is bumped after migrations succeed, so a crash mid-migration is replayed safely. Pattern documented in `src/eneru/AGENTS.md` "Stats schema evolution"
- `SAFETY_CRITICAL_EVENTS` + `SUPPRESSIBLE_EVENTS` constants in `src/eneru/config.py` enumerate the notification-suppression policy

---

## [5.0.0] - 2026-04-11

### Added
- **Multi-UPS Monitoring:** Monitor multiple UPS systems from a single Eneru instance (#4)
    - New `UPSGroupConfig` with `is_local` flag to define which UPS powers the Eneru host
    - Per-UPS `display_name` for human-readable labels in logs and notifications
    - Per-UPS trigger overrides with global defaults inheritance
    - `MultiUPSCoordinator` with thread-per-group architecture and shared notification worker
    - Defense-in-depth local shutdown coordination (`threading.Lock` + filesystem flag file) prevents duplicate shutdown
    - Backward-compatible config detection: dict format = single-UPS (legacy), list format = multi-UPS
    - Ownership validation: only the `is_local` group can manage local resources (VMs, containers, filesystems)
    - New example configuration: `examples/config-dual-ups.yaml`
- **TUI Dashboard (`eneru monitor`):** Real-time curses-based monitoring interface
    - Two-panel layout: gray config/status panel + gold events panel
    - Reads daemon state files directly (no NUT polling, no contention with main daemon)
    - Color-coded status badges: green (online), red + blink (on battery/critical), magenta (unknown)
    - 256-color palette for consistent rendering across SSH sessions
    - Interactive controls: `<Q>` quit, `<R>` refresh, `<M>` toggle more logs
    - `--once` mode for scripts and cron health checks (single snapshot, no curses)
    - Auto-refresh every 5 seconds, configurable with `--interval`
    - Multi-UPS display: shows all UPS groups in a single dashboard
- **Battery Anomaly Detection:** Identifies unexpected charge drops while on line power
    - Detects >20% charge drops within 120 seconds while UPS reports OL/CHRG status
    - Sustained-reading confirmation: requires 3 consecutive polls before firing alert
    - Firmware jitter filtering for APC, CyberPower, and Ubiquiti UniFi UPS units after OB→OL transitions
    - Catches firmware recalibrations, battery aging, and hardware issues
    - Sends notification + log warning with charge delta and timing details
- **CLI Subcommand Architecture:** Modern command-line interface with dedicated subcommands
    - `eneru run` — start the UPS monitoring daemon
    - `eneru validate` — validate configuration file and show overview
    - `eneru monitor` — launch the TUI dashboard
    - `eneru test-notifications` — test notification channels
    - `eneru version` — display version information
    - Bare `eneru` now shows help instead of starting the daemon (prevents accidental start)

### Changed
- **Config Reference Relocated:** `config.yaml` → `examples/config-reference.yaml` (installed path `/etc/ups-monitor/` unchanged)
- **Systemd Service Relocated:** `eneru.service` → `packaging/eneru.service` (installed path `/lib/systemd/system/` unchanged)
- **Systemd Service Updated:** ExecStart uses `eneru run` subcommand
- **Changelog Consolidated:** Root `CHANGELOG.md` merged into `docs/changelog.md` with complete version history (v1.0 through v4.11)
- **Test Suite Expanded:** 216 → 300 tests (+84 tests, 39% increase)
    - 20+ multi-UPS tests: config parsing, trigger inheritance, ownership validation, coordinator routing, lock synchronization
    - 26 monitor core tests: status state machine, shutdown triggers, FSD handling, failsafe, shutdown sequencing
    - 23 TUI tests: state file parsing, log filtering, status mapping, color rendering, `--once` output
- **E2E Tests Expanded:** 7 → 18 tests with multi-UPS scenarios
    - NUT dummy server extended with UPS1 and UPS2 driver entries and per-UPS state files
    - New tests: multi-UPS config validation, UPS isolation (one fails, other unaffected), ownership validation, TUI `--once`

### Technical Details
- Thread-per-group model: each UPS group runs in a dedicated thread with its own `UPSGroupMonitor` instance
- Per-group state files suffixed with sanitized UPS name (e.g., `/var/run/ups-monitor.state.UPS1-192-168-1-10`)
- Single-UPS mode completely unchanged -- full backward compatibility
- Legacy config format (dict) auto-detected and supported alongside new list format
- At most one UPS group can be marked `is_local: true`
- Remote servers allowed on any group; local resources restricted to the `is_local` group

### Migration Notes

CLI invocation changed from bare command to subcommands:
```bash
# Before (v4.x)
eneru --config /etc/ups-monitor/config.yaml
eneru --validate-config --config /etc/ups-monitor/config.yaml
eneru --test-notifications --config /etc/ups-monitor/config.yaml

# After (v5.0)
eneru run --config /etc/ups-monitor/config.yaml
eneru validate --config /etc/ups-monitor/config.yaml
eneru test-notifications --config /etc/ups-monitor/config.yaml
```

- **Package users (deb/rpm):** Systemd service is updated automatically — no action needed
- **Config format:** Existing single-UPS configurations work without any modification
- **No breaking changes** for single-UPS deployments

---

## [4.11.0] - 2026-04-02

### Added
- **Connection Loss Grace Period:** Delays `CONNECTION_LOST` notifications for flaky NUT servers
    - New `ups.connection_loss_grace_period` configuration section
    - Holds notifications during brief outages (default: 60 seconds)
    - If connection recovers within the grace period, no notification is sent
    - After grace period expires, `CONNECTION_LOST` notification fires as normal
    - **Flap Detection:** Sends a `WARNING` after repeated grace-period recoveries (default: 5 flaps within 24 hours)
    - **Failsafe Unaffected:** Failsafe (connection lost while on battery = immediate shutdown) bypasses the grace period
    - New connection state: `GRACE_PERIOD` (between `OK` and `FAILED`)
    - 26 new tests for grace period scenarios

### Changed
- **Apprise Dependency:** Bumped minimum version from 1.9.6 to 1.9.7
- **CI Integration Matrix:** Added Debian 11 (Bullseye) and Ubuntu 22.04 (Jammy) to integration tests
    - Both pass `.deb` package installation with system Python
    - Debian 11 also tested with pip-in-container (system pip 20.3.4 works)
    - Ubuntu 22.04 excluded from pip-in-container: system pip 22.0.2 has a regression with `pyproject.toml` dynamic version metadata (workaround: upgrade pip)
    - Integration tests now cover 9 Linux distributions (was 7)
- **CI Python 3.15:** Added Python 3.15-dev to test matrix as non-blocking (`continue-on-error`)
- **Documentation:** Replaced PNG architecture diagram with SVG, fixed documentation formatting

### Technical Details
- No breaking changes -- grace period is enabled by default
- All 216 tests pass (190 existing + 26 new)
- Flap counter uses a 24-hour TTL so rare, spread-out flaps do not trigger false warnings

---

## [4.10.0] - 2026-01-18

### Changed
- **Modular Architecture:** Split monolithic `monitor.py` into focused modules for better maintainability
    - `version.py` - Version string (single source of truth)
    - `config.py` - Configuration dataclasses + ConfigLoader
    - `state.py` - MonitorState dataclass
    - `logger.py` - TimezoneFormatter + UPSLogger
    - `notifications.py` - NotificationWorker (Apprise integration)
    - `utils.py` - Helper functions (run_command, command_exists, is_numeric, format_seconds)
    - `actions.py` - REMOTE_ACTIONS templates for remote pre-shutdown commands
    - `monitor.py` - UPSGroupMonitor class (core daemon logic)
    - `cli.py` - CLI argument parsing + main()
- **Developer Documentation:** Add project guidance for Claude Code

### Technical Details
- No breaking changes to public API or configuration format
- All 190 tests pass, E2E testing, and over two weeks of real-world testing
- Module imports maintain backwards compatibility via `__init__.py` exports

---

## [4.9.0] - 2026-01-06

### Added
- **End-to-End (E2E) Test Suite:** Comprehensive E2E testing infrastructure with real services
    - NUT server with dummy driver for UPS state simulation (8 scenarios)
    - SSH target container for remote shutdown command verification
    - Docker Compose test environment for local and CI testing
    - 7 automated tests covering config validation, power failure detection, SSH shutdown, FSD triggers, voltage events, and notifications
    - New GitHub Actions workflow (`.github/workflows/e2e.yml`) runs on every push/PR
- **`--exit-after-shutdown` CLI Flag:** Exit after completing shutdown sequence instead of continuing to monitor
    - Useful for E2E testing and scripting scenarios
    - Enables clean test completion in CI environments

### Fixed
- **Dry-run Mode:** Wall broadcast messages are now skipped in dry-run mode to avoid false alerts during testing

---

## [4.8.0] - 2026-01-04

### Added
- **PyPI Publishing:** Eneru is now available on PyPI (`pip install eneru`)
    - Automated publishing workflow on GitHub releases
    - Optional `[notifications]` extra for Apprise support
    - Supports Python 3.9, 3.10, 3.11, 3.12, 3.13, and 3.14
- **Integration Testing Workflow:** New CI workflow tests package installation on real Linux distributions
    - `.deb` package testing on Debian 12/13, Ubuntu 24.04/26.04
    - `.rpm` package testing on RHEL 8/9/10
    - `pip install` testing across 6 Python versions and 3 container distros
- **Testing Documentation:** New [Testing](testing.md) page documenting CI/CD strategy

### Changed
- **Python Version:** Minimum required Python version is now 3.9 (was 3.8)
- **Installation Options:** PyPI is now listed as a primary installation method alongside native packages

### Removed
- **Manual Installation Script:** Removed `install.sh` in favor of native packages and pip

---

## [4.7.0] - 2026-01-03

### Added
- **Persistent Notification Retry:** Notifications are now retried until successful delivery
    - Worker thread persistently retries failed notifications instead of dropping them
    - FIFO queue ensures message order is preserved
    - New `notifications.retry_interval` configuration option (default: 5 seconds)
    - New `get_queue_size()` and `get_retry_count()` methods for monitoring
    - Guaranteed delivery during transient network outages (e.g., 30-second power blip)
- **Expanded Test Suite:** Added 7 new tests for notification retry behavior (185 total)

### Changed
- **Notification Architecture:** Evolved from "fire-and-forget" to "persistent retry with ACK"
    - Main thread still queues instantly (zero blocking on shutdown operations)
    - Worker thread now retries each message until success before moving to next
    - Stop signal interrupts retry wait immediately (no delay on shutdown)
    - Pending message count now includes in-progress retries in stop log

---

## [4.6.0] - 2025-12-31

### Added
- **Modern Python Packaging:** Added `pyproject.toml` for PEP 517/518 compliant packaging
    - Can now be installed via `pip install .` from repository root
    - Entry point: `eneru` command available after pip install
    - Optional dependencies: `[notifications]`, `[dev]`, `[docs]`
- **Package Structure:** Reorganized codebase into proper Python package
    - Source code moved to `src/eneru/` directory
    - `__init__.py` exports all public APIs
    - `__main__.py` enables `python -m eneru` invocation
- **Comprehensive Test Suite:** Expanded to 178 tests covering:
    - `run_command` and `command_exists` helper functions
    - CLI validation (`--validate-config`, `--test-notifications`)
    - Remote pre-shutdown command templating
    - Configuration parsing edge cases

### Changed
- **Script Renamed:** `ups_monitor.py` -> `src/eneru/monitor.py`
- **Installed Path:** `/opt/ups-monitor/ups_monitor.py` -> `/opt/ups-monitor/eneru.py`
- **Test Imports:** Updated to use `from eneru import ...` instead of `from ups_monitor import ...`
- **Coverage Path:** CI now reports coverage for `src/eneru` module

### Migration Notes
- Existing installations via packages (deb/rpm) will be updated automatically
- Manual installations should update paths in any custom scripts or systemd overrides
- The installed script path changed from `ups_monitor.py` to `eneru.py`

---

## [4.5.0] - 2025-12-30

### Added
- **Docker/Podman Compose Shutdown:** Ordered shutdown of compose stacks before individual containers
    - New `containers.compose_files` configuration for defining compose files to stop
    - Per-file timeout override support (`stop_timeout`)
    - New `containers.shutdown_all_remaining_containers` option (default: true)
    - Compose availability check at startup with graceful fallback
- **Remote Server Pre-Shutdown Commands:** Execute commands on remote servers before shutdown
    - New `remote_servers[].pre_shutdown_commands` configuration
    - Predefined actions: `stop_containers`, `stop_vms`, `stop_proxmox_vms`, `stop_proxmox_cts`, `stop_xcpng_vms`, `stop_esxi_vms`, `stop_compose`, `sync`
    - Custom command support with per-command timeout
    - All pre-shutdown commands are best-effort (log and continue on failure)
- **Parallel Remote Server Shutdown:** Concurrent shutdown of multiple remote servers using threads
    - New `remote_servers[].parallel` option (default: true)
    - Servers with `parallel: false` shutdown sequentially first, then parallel batch runs concurrently
    - Useful for dependency ordering (e.g., shutdown NAS last after other servers unmount)

### Changed
- **Sync Hardening:** Added 2-second sleep after `os.sync()` to allow storage controller caches (especially battery-backed RAID) to flush before power is cut
- **Notification Worker:** Now logs pending message count when stopping during shutdown

### Fixed
- **GitHub Release Workflow:** Added explicit `tag_name` to gh-release action

---

## [4.4.0] - 2025-12-30

### Added
- **Read The Docs Integration:** Documentation now hosted on Read The Docs with MkDocs Material theme
- **Dark Mode Documentation:** Material theme with automatic dark mode support
- **Improved Search:** Full-text search across all documentation pages
- **Code Copy Buttons:** One-click copy for all code blocks in documentation
- **Tabbed Installation Instructions:** Distro-specific tabs for Debian/Ubuntu, RHEL/Fedora, and manual install
- **Upgrade/Uninstall Instructions:** Previously missing documentation for upgrading and removing Eneru
- **Dedicated Documentation Pages:**
    - Getting Started guide with step-by-step installation
    - Configuration reference with all options
    - Shutdown Triggers deep-dive with diagrams
    - Notifications guide for Apprise setup
    - Remote Servers SSH setup guide
    - Troubleshooting with real log examples

### Changed
- **README Slimmed Down:** Reduced from ~1200 lines to ~145 lines, linking to RTD for details

---

## [4.3.0] - 2025-12-29

### Added
- **Native Package Distribution:** Official `.deb` and `.rpm` packages for easy installation
- **APT/DNF Repository:** Packages available via GitHub Pages hosted repository for Debian, Ubuntu, RHEL, and Fedora
- **Version CLI Option:** New `-v`/`--version` flag to display current version
- **Version Display:** Version now shown at service startup and in notifications
- **nFPM Build System:** Automated package building using nFPM for both Debian and RPM formats
- **GitHub Release Automation:** Packages automatically built and published on GitHub releases
- **GPG Signed Repository:** Repository metadata is GPG signed for security
- **requirements.txt:** Added for pip-based installations (`PyYAML>=5.4.1`, `apprise>=1.9.6`)
- **No Docker Documentation:** Explained why Eneru runs as a systemd daemon (chicken-and-egg problem with container shutdown)

### Changed
- **Service Name:** Renamed from `ups-monitor.service` to `eneru.service` to avoid conflict with nut-client's service
- **Installation Method:** Package installation (deb/rpm) is now the recommended method
- **Service Behavior:** Packages install but do not auto-enable or auto-start the service (config must be edited first)
- **Config File Handling:** Package upgrades preserve existing `/etc/ups-monitor/config.yaml` (marked as conffile)
- **Upgrade Behavior:** Smart detection ensures running service restarts on upgrade, stopped service stays stopped
- **Service File Location:** Moved from `/etc/systemd/system/` to `/lib/systemd/system/` for proper package management

### Fixed
- **Discord Mention Prevention:** Added zero-width space after `@` symbols in notification messages to prevent Discord from interpreting UPS names (e.g., `UPS@192.168.1.1`) as user mentions
- **APT Repository Structure:** Proper `dists/stable/main/binary-all/` hierarchy for Debian/Ubuntu compatibility
- **RPM Repository GPG:** Fixed gpgcheck configuration (repo metadata signed, individual packages served over HTTPS)

### Migration from v4.2

If you installed manually, update your systemd service reference:
```bash
# Stop old service
sudo systemctl stop ups-monitor.service
sudo systemctl disable ups-monitor.service

# Remove old service file
sudo rm /etc/systemd/system/ups-monitor.service

# Install new package (recommended) or copy new service file
sudo cp eneru.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable eneru.service
sudo systemctl start eneru.service
```

### Installation

Install via package manager (after adding the repository):
```bash
# Debian/Ubuntu
apt install eneru

# RHEL/Fedora
dnf install eneru
```

Or download packages directly from GitHub releases.

---

## [4.2.0] - 2025-12-23

### Added
- **Apprise Integration:** Support for 100+ notification services (Discord, Slack, Telegram, ntfy, Pushover, Email, Matrix, and more)
- **Non-Blocking Notification Architecture:** Notifications never delay critical shutdown operations
- **Background Notification Worker:** Dedicated thread processes notifications asynchronously
- **`--test-notifications` CLI Option:** Send test notification to verify configuration
- **Avatar URL Support:** Configurable avatar/icon for supported services (Discord, Slack, etc.)
- **Notification Title Option:** Optional custom title for multi-instance deployments
- **5-Second Grace Period:** Final grace period before shutdown allows queued notifications to send
- **Architecture Documentation:** ASCII diagram explaining non-blocking notification flow
- **Test Suite:** Comprehensive pytest test suite with 80+ unit and integration tests
- **Code Coverage:** Codecov integration for tracking test coverage

### Changed
- **Notification System:** Migrated from native Discord webhooks to Apprise library
- **Notification Behavior:** All shutdown-related notifications are now fire-and-forget
- **Configuration Format:** New `notifications.urls` array replaces `notifications.discord.webhook_url`
- **Script Filename:** Renamed `ups-monitor.py` to `ups_monitor.py` for Python module compatibility
- **Dependency:** `requests` library replaced with `apprise` library

### Removed
- **Native Discord Integration:** Replaced by Apprise (Discord still fully supported via Apprise)
- **`timeout_blocking` Config:** No longer needed with non-blocking architecture

### Backwards Compatibility
- Legacy `discord.webhook_url` configuration automatically converted to Apprise format
- Legacy `notifications.discord` section still supported and auto-migrated
- All existing functionality preserved
- Service file and install script updated for new filename

### Why Non-Blocking Matters
During power outages, network connectivity is often unreliable. The previous blocking implementation could delay shutdown by 10-30+ seconds per notification if the network was down. The new architecture queues notifications immediately and processes them in the background, so critical shutdown work never waits on the network.

---

## [4.1.0] - 2025-12-19

### Added
- Native Podman support alongside Docker
- Container runtime auto-detection (prefers Podman over Docker)
- New `containers.runtime` configuration option: `auto`, `docker`, or `podman`
- Support for stopping rootless Podman user containers (`include_user_containers`)
- Comprehensive "Shutdown Triggers Explained" documentation section
- Detailed depletion rate calculation explanation with examples
- Grace period rationale and behavior documentation
- Trigger interaction and overlap analysis
- Recommended configurations (conservative, balanced, aggressive)
- Trigger evaluation flowchart
- `.gitignore` for common editor and Python files
- "The Name" section explaining One Piece reference

### Changed
- Project rebranded from "UPS Tower" to "Eneru"
- Configuration section `docker` renamed to `containers` (backwards compatible)
- `--validate-config` output updated for containers section
- Diagram renamed to `eneru-diagram.png`
- Updated all documentation with Eneru branding
- Changelog revamped to Keep a Changelog format with version comparisons

### Fixed
- `--validate-config` crash when referencing old `config.docker` attribute

### Backwards Compatibility
- Existing `docker:` configuration sections continue to work
- Technical paths unchanged (`ups-monitor.py`, `/opt/ups-monitor/`, `/etc/ups-monitor/`)

---

## [4.0.0] - 2025-12-17

### Added
- External YAML configuration file support (`/etc/ups-monitor/config.yaml`)
- Multiple remote server shutdown with per-server custom commands
- Command-line arguments: `--config`, `--dry-run`, `--validate-config`
- Graceful degradation when optional dependencies (PyYAML, requests) missing
- Modular configuration classes
- GitHub Actions workflow for syntax and configuration validation (Python 3.9-3.12)
- Comprehensive README with badges and architecture diagram
- Complete version history with detailed changelogs
- Installation guide with multi-distro support
- Configuration reference with all options documented
- Troubleshooting guide for common issues
- Security considerations and best practices
- CONTRIBUTING.md with:
    - Code style guidelines
    - Testing requirements
    - Commit message conventions
    - Development setup instructions
    - Pull request process
- Example configurations:
    - `config-minimal.yaml` - Basic single-server setup
    - `config-homelab.yaml` - VMs, Docker, NAS, Discord notifications
    - `config-enterprise.yaml` - Multi-server enterprise deployment
- GitHub Issue Templates:
    - Bug report template with environment details
    - Feature request template with use case format
    - Issue template chooser configuration
- GitHub Pull Request Template with testing checklist

### Changed
- Configuration now loaded from external file instead of source code
- All features independently toggleable via configuration
- Install script preserves existing configuration on upgrade
- Install script auto-detects package manager (dnf, apt, pacman)
- License changed from Apache 2.0 to MIT

### Removed
- Hardcoded configuration values in source code
- Single remote NAS limitation (now supports multiple servers)
- Apache 2.0 license (replaced with MIT)

---

## [3.0.0] - 2025-12-15

### Added
- Complete rewrite from Bash to Python 3.9+
- Native JSON handling via `requests` library
- Native math operations (no external dependencies)
- Python dataclass configuration with type hints
- In-memory state management with file persistence
- Full type hints throughout codebase
- Python exception-based error handling
- Python string formatting (replacing shell variable expansion)

### Changed
- Language: Bash 4.0+ -> Python 3.9+
- Configuration: Shell variables -> Python dataclass with type hints
- State management: File-based with shell parsing -> In-memory with file persistence
- Error handling: Shell traps -> Python exceptions

### Removed
- `jq` dependency (JSON now handled natively)
- `bc` dependency (math now handled natively)
- `awk` dependency (text processing now handled natively)
- `grep` dependency (pattern matching now handled natively)

### Dependencies
- Added: `python3-requests`
- Removed: `jq`, `bc`, `awk`, `grep`

---

## [2.0.0] - 2025-10-22

### Added
- Discord webhook integration with color-coded embeds
- Depletion rate grace period (prevents false triggers on power loss)
- Failsafe Battery Protection (FSB) - shutdown if connection lost while on battery
- FSD (Forced Shutdown) flag detection from UPS
- Configurable mount list with per-mount options (e.g., lazy unmount)
- Overload state tracking with resolution detection
- Bypass mode detection
- AVR (Automatic Voltage Regulation) Boost/Trim detection
- Service stop notifications
- Dynamic VM wait times (up to 30s with force destroy)
- Timeout-protected unmounting (hang-proof)
- Absolute threshold-based voltage monitoring
- Extended depletion tracking (300-second window, 30 samples)
- Stale data detection for connection handling
- Crisis reporting (elevated notifications during shutdown)
- Passwordless sudo configuration guide

### Changed
- VM shutdown: fixed 10s wait -> dynamic wait up to 30s with force destroy
- NAS authentication: password in script (`sshpass`) -> SSH key-based (no passwords stored)
- Voltage monitoring: relative change detection -> absolute threshold-based detection
- Depletion rate: 60-second window, 15 samples -> 300-second window, 30 samples with grace period
- Connection handling: basic retry -> stale data detection with failsafe shutdown
- Shutdown triggers: 4 triggers -> 4 triggers + FSD flag detection
- Configuration: minimal -> includes mount options

### Security
- Removed `sshpass` and password storage
- SSH key-based authentication for NAS
- Passwordless sudo configuration guide

### Dependencies
- Added: `jq` (for safer JSON generation)
- Removed: `sshpass`
- Required: Bash 4.0+ (for associative arrays)

---

## [1.0.0] - 2025-10-18

### Added
- Initial implementation in Bash
- Basic UPS monitoring via NUT (Network UPS Tools)
- Battery depletion tracking (60-second window, 15 samples)
- Shutdown sequence:
    - Virtual Machines (libvirt/KVM)
    - Docker containers
    - Remote NAS (via SSH with `sshpass`)
- Basic logging
- systemd service integration
- 4 shutdown triggers:
    - Low battery threshold
    - Critical runtime threshold
    - Depletion rate threshold
    - Extended time on battery
