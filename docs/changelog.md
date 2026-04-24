# Changelog

All notable changes to Eneru are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased] — v5.2.0 — Stateful, lossless notifications

Architectural rework of the notification subsystem. v5.1's notifications were stateless and noisy: a `systemctl restart` produced two unrelated events, a shutdown sequence produced ~22 mid-flight "ℹ️ Shutdown Detail" lines that mirrored the log, and a power outage that took the internet down meant no notifications ever got delivered. v5.2 fixes all of that.

(Verbose `[Unreleased]` block per project convention — trimmed at release-tag time. See `git log v5.1.2..HEAD` for per-commit detail with rationale.)

### Added
- **DB-backed notification queue (Slice 2).** `NotificationWorker` now reads/writes through each registered `StatsStore`'s `notifications` table (schema v3 → v4). Pending rows survive process death and prolonged endpoint outages and deliver in age order when the network returns. Per-message exponential backoff (capped at `retry_backoff_max`, default 5 min) so a multi-day outage doesn't hammer an unreachable endpoint while it's down. New CRUD: `enqueue_notification`, `next_pending_notifications`, `mark_notification_sent`, `mark_notification_attempt`, `cancel_notification`, `pending_notification_count`, `cap_pending_notifications`, `prune_old_notifications`, `find_pending_by_category`, `get_meta` / `set_meta`.
- **Stateful lifecycle classifier (Slice 3).** Replaces the v5.1 unconditional "🚀 Started" with one of: `📦 Upgraded vX → vY` (deb/rpm postinstall marker OR pip-path version comparison via `meta.last_seen_version`), `📊 Recovered` (graceful exit reason=`sequence_complete` — power-loss-triggered shutdown), `🚀 Restarted (fatal)` (last instance died), `🔄 Restarted (downtime: Ns)` (clean signal exit + downtime < 30s), `🚀 Started (last seen Nh ago)` (clean signal exit + older), `🚀 Started (after crash)` (no marker but `last_seen_version` set), or `🚀 Started` (fresh install). New module `src/eneru/lifecycle.py` with marker-file CRUD + pure `classify_startup` function.
- **Shutdown markers.** Daemon writes `/var/lib/eneru/.shutdown_state.json` on graceful exit with `shutdown_at` / `version` / `reason` (`signal` / `sequence_complete` / `fatal`). Postinstall.sh writes `.upgrade_marker.json` with `old_version` BEFORE `systemctl restart` on deb/rpm upgrade.
- **Brief-outage coalescing (Slice 4).** `NotificationWorker._coalesce_pending_outages` runs once per worker iteration: when a pending `ON_BATTERY` and a pending `POWER_RESTORED` from the same outage both sit in the queue (network was down for the duration of a short blip), it folds them into one "📊 Brief Power Outage" summary and cancels the originals with `cancel_reason='coalesced'`.
- **Recovered + previous shutdown coalescing (Slice 4 bonus).** When startup classification returns "Recovered", `lifecycle.coalesce_recovered_with_prev_shutdown` folds the previous instance's pending shutdown headline + summary into a single richer message that includes the trigger reason lifted from the headline, the time-of-day at both ends, and the downtime. Saves the user from seeing 3 messages for what's really one power-outage round trip.
- **Outage-survival defaults (Slice 2).** Five new `notifications.*` config knobs sized for a multi-day weekend internet outage: `retention_days=7` (TTL on sent/cancelled rows; pending NEVER pruned), `max_attempts=0` (unlimited — Apprise's bool can't tell "bad URL" from "internet down"), `max_age_days=30` (only cap on pending), `max_pending=10000` (backlog overflow, oldest cancelled), `retry_backoff_max=300` (5 min ceiling on per-message exponential backoff).
- **`flush(timeout)` on the worker (Slice 5).** Drains the queue before `stop()` joins the worker thread. Wired into all four shutdown paths (`monitor._cleanup_and_exit`, `monitor._execute_shutdown_sequence`, `multi_ups._handle_signal`, `multi_ups._handle_local_shutdown`). Closes the v5.1 `Stopping notification worker with 1 message(s) pending` SIGTERM race.
- **TUI events panel: full date prefix (Slice 6).** Rows render `2026-04-24 14:23:05` instead of just `14:23:05`, so multi-day events are distinguishable.

### Changed
- **Shutdown notifications: 22 → 2 (Slice 1).** Removed the per-`_log_message` auto-mirror that wrapped every shutdown log line as an "ℹ️ Shutdown Detail:" notification. The notification channel now only carries: the headline ("🚨 EMERGENCY SHUTDOWN INITIATED!" with the trigger reason), per-remote-server "Starting" notifications (failures still notify), and a single "✅ Shutdown Sequence Complete" summary at the end (now also fires when `local_shutdown.enabled=false`). journalctl carries the per-step trace.
- **`local_shutdown.wall` defaults to `false` (Slice 1).** Holdover from the v2 `ups-monitor` era when the shell was the only notification channel; Apprise covers the modern path. Flip to `true` if you still want the tty broadcasts on top.
- **`_send_notification` API.** New `category` keyword argument (default `general`) — used by the worker for coalescing and per-category queries. Common values: `lifecycle`, `power_event`, `voltage`, `shutdown`, `shutdown_summary`. The `blocking` parameter is now a back-compat shim (the v5.2 queue is always asynchronous; delivery happens on the worker thread).
- **`========== BANNER ==========` formatting dropped (Slice 1).** The padded `==========` style across `monitor.py` and `redundancy.py` was a v2 shell-scanning legacy. The ALL CAPS body remains (still grep-friendly, still an externally-observable string the E2E tests assert against).
- **Schema migration v3 → v4.** New `notifications` table + index; idempotent via `CREATE TABLE IF NOT EXISTS` so a partially-migrated DB self-heals on next open. Append-only per the project pattern in `src/eneru/CLAUDE.md`.

### Removed
- **`get_retry_count()` / `get_queue_size()`** on `NotificationWorker`. The new persistent worker exposes `get_pending_count()` (sum across registered stores) instead. `_retry_count` was a single-message in-memory counter; `attempts` per row now lives in SQLite.
- **`time.sleep(5)`** at the local-shutdown gate. Replaced by `flush(timeout=5)` which returns as soon as pending hits 0 instead of always waiting the full 5s.
- **Per-server "Remote Shutdown Sent" success notifications.** Redundant with the per-server "Starting" + the aggregate "Sequence Complete" summary. Failures still notify.

### Migration notes
- **deb/rpm upgrades**: postinstall.sh now drops `/var/lib/eneru/.upgrade_marker.json` before `systemctl restart`. Pip users get the same effect via the `meta.last_seen_version` comparison.
- **Wall broadcasts**: if you relied on the v5.1 default of "wall fires on every shutdown", set `local_shutdown.wall: true` in your config.
- **Stats DB schema**: bumps from v3 to v4 on first start. Schema migration is idempotent and append-only; existing rows are preserved.

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
- **Long tail of robustness fixes** across `shutdown/`, `health/`, `graph.py`, `logger.py`, `stats.py`, `utils.py`, `cli.py`, and bash completion.

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
- Schema migration mechanic: `StatsStore._migrate_schema` applies append-only `ALTER TABLE` migrations gated by `_safe_alter` (idempotent). `meta.schema_version` is bumped after migrations succeed, so a crash mid-migration is replayed safely. Pattern documented in `src/eneru/CLAUDE.md` "Stats schema evolution"
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
- **Developer Documentation:** Add `CLAUDE.md` for Claude Code

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
During power outages, network connectivity is often unreliable. The previous blocking implementation could delay shutdown by 10-30+ seconds per notification if the network was down. The new architecture queues notifications instantly and processes them in the background, ensuring critical shutdown operations are never delayed.

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
- Configuration: minimal -> comprehensive with mount options

### Security
- Removed `sshpass` and password storage
- SSH key-based authentication for NAS
- Passwordless sudo configuration guide

### Dependencies
- Added: `jq` (for robust JSON generation)
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

---

## Version Comparison

### v5.2 vs v5.1

| Feature | v5.1 | v5.2 |
|---------|------|------|
| Notification queue | In-memory FIFO; messages dropped on shutdown | Persistent SQLite-backed; lossless across restarts and prolonged outages |
| Lifecycle messaging | Always `🚀 Started` + `🛑 Stopped` (2 unrelated events per restart) | Stateful classifier: `Started` / `Restarted` / `Recovered` / `Upgraded` |
| Power-loss recovery | Stop + Start (2 messages, no context) | Single `📊 Recovered` message folding the trigger reason + downtime |
| Brief power outages | 2 separate messages (on_battery + on_line) | Coalesced into 1 `📊 Brief Power Outage` summary if both still pending |
| Shutdown noise | ~22 mid-flight `ℹ️ Shutdown Detail` lines per sequence | 2 messages: headline + summary; journalctl carries the rest |
| `wall(1)` broadcasts | Always-on (legacy from v2 ups-monitor era) | Opt-in via `local_shutdown.wall: false` |
| Long-outage delivery | Best-effort retries; can drop on shutdown | Per-message exponential backoff capped at 5min, default 30-day max age |
| Schema version | v3 (raw NUT + agg + events.notification_sent) | v4 (notifications table) |
| TUI events panel | `HH:MM:SS` only | Full `YYYY-MM-DD HH:MM:SS` (multi-day rows distinguishable) |
| Test count | 615 | 871 (+1 new E2E for panic-attack coalescing) |

### v5.1 vs v5.0

| Feature | v5.0 | v5.1 |
|---------|------|------|
| Redundancy groups | Not available | Quorum-based via `min_healthy`; supports A+B PSU pairs and dual-feed racks |
| UPS health model | Implicit (per-poll status) | `HEALTHY` / `DEGRADED` / `CRITICAL` / `UNKNOWN` with configurable mapping |
| Stats persistence | Not available | Per-UPS SQLite DB with 13 raw NUT metrics + Eneru-derived; tier-aware retention (24h raw / 30d 5-min / 5y hourly) |
| TUI graphs | Not available | Braille time-series for any tracked metric, blended live (state-file → SQLite) |
| TUI events panel | Log-tail parsing only | SQLite events table with arrow-key scroll (5.1.1) |
| Voltage warning thresholds | Hardcoded `±10% / EN 50160 envelope` | Per-UPS-group `voltage_sensitivity` preset: `tight` / `normal` / `loose` (5.1.2) |
| Schema version | N/A | v3 (raw NUT + agg tiers + events.notification_sent audit trail) |
| Module decomposition | Single `monitor.py` (~1900 lines) | Split into `shutdown/` (4 mixins) + `health/` (2 mixins); `monitor.py` ~830 lines |
| E2E test scenarios | 18 (single + multi-UPS) | 33 (+ stats, redundancy, voltage-autodetect groups) |
| Test count | 300 | 615 |

### v5.0 vs v4.11

| Feature | v4.11 | v5.0 |
|---------|-------|------|
| UPS Support | Single UPS only | Multiple UPS with per-group resources |
| UPS Ownership | N/A | `is_local` flag defines host UPS |
| Per-UPS Triggers | N/A | Override global defaults per UPS group |
| TUI Dashboard | Not available | `eneru monitor` with real-time status |
| Battery Anomaly Detection | Not available | Charge drop detection with jitter filtering |
| CLI Interface | Flat flags (`--validate-config`) | Subcommands (`eneru run`, `validate`, `monitor`) |
| Bare `eneru` | Starts daemon | Shows help (safe default) |
| Config Reference Location | `config.yaml` (root) | `examples/config-reference.yaml` |
| Service File Location | Root directory | `packaging/eneru.service` |
| Thread Model | 2 threads (monitor + notifications) | N+2 threads (N UPS monitors + notifications + main) |
| Shutdown Coordination | N/A | Lock + flag file (defense-in-depth) |
| E2E Test Scenarios | 7 (single-UPS) | 18 (single + multi-UPS) |
| Test Count | 216 tests | 300 tests |

### v4.11 vs v4.10

| Feature | v4.10 | v4.11 |
|---------|-------|-------|
| Connection Loss Handling | Immediate notification on every failure | Configurable grace period (default 60s) |
| Flap Detection | Not available | WARNING after N recoveries within 24h |
| Transient Failure Handling | None | Grace period skips notifications for brief outages |
| Connection States | `OK`, `FAILED` | `OK`, `GRACE_PERIOD`, `FAILED` |
| Failsafe (FSB) | Immediate shutdown on battery | Unchanged (grace period bypassed) |
| Apprise Minimum | 1.9.6 | 1.9.7 |
| CI Python Versions | 3.9-3.14 | 3.9-3.15-dev |
| CI Integration Distros | 7 (Debian 12-13, Ubuntu 24.04-26.04, RHEL 8-10) | 9 (+ Debian 11, Ubuntu 22.04) |
| Test Count | 190 tests | 216 tests |

### v4.10 vs v4.9

| Feature | v4.9 | v4.10 |
|---------|------|-------|
| Code Structure | Single `monitor.py` (~2500 lines) | 9 focused modules |
| Module Count | 1 main module | 9 modules (`version`, `config`, `state`, `logger`, `notifications`, `utils`, `actions`, `monitor`, `cli`) |
| Developer Documentation | Basic emoji list | Comprehensive categorized emoji reference |
| Emoji Reference | 11 emojis listed | 20+ emojis with semantic meanings |
| Test Count | 190 tests | 190 tests |
| Breaking Changes | N/A | None (backwards compatible imports) |

### v4.9 vs v4.8

| Feature | v4.8 | v4.9 |
|---------|------|------|
| E2E Testing | Not available | Full E2E suite with NUT, SSH, Docker |
| UPS Simulation | Manual testing only | 8 automated scenarios (dummy driver) |
| `--exit-after-shutdown` | Not available | Exit after shutdown for scripting/testing |
| Dry-run Wall Broadcast | Sent wall messages | Skipped in dry-run mode |
| CI Workflows | 4 (validate, integration, release, pypi) | 5 (+ e2e) |
| Test Automation | Unit + package install | Unit + package install + E2E |

### v4.8 vs v4.7

| Feature | v4.7 | v4.8 |
|---------|------|------|
| PyPI Distribution | Not available | `pip install eneru` |
| Installation Methods | deb/rpm packages only | PyPI + deb/rpm packages |
| Integration Testing | Unit tests only | Package install on 7 distros |
| Python Versions | 3.8+ | 3.9+ (3.8 dropped) |
| Manual Install Script | `install.sh` available | Removed (use pip or packages) |
| Testing Documentation | Not documented | Dedicated Testing page |
| CI Workflows | 2 (validate, release) | 4 (+ integration, pypi) |

### v4.7 vs v4.6

| Feature | v4.6 | v4.7 |
|---------|------|------|
| Notification Retry | Fire-and-forget (dropped on failure) | Persistent retry until success |
| Network Outage Handling | Notifications lost during outage | Notifications queued and retried |
| Message Ordering | N/A (no retry) | FIFO order preserved |
| Retry Configuration | None | `notifications.retry_interval` (default: 5s) |
| Queue Monitoring | Basic queue size | `get_queue_size()` + `get_retry_count()` |
| Stop Behavior | Logs pending count | Logs pending + current retry number |
| Test Count | 178 tests | 185 tests |

### v4.6 vs v4.5

| Feature | v4.5 | v4.6 |
|---------|------|------|
| Package Structure | Single file at root (`ups_monitor.py`) | Python package (`src/eneru/`) |
| Installation | Script copy only | pip installable (`pip install .`) + script |
| Entry Points | `python3 ups_monitor.py` | `eneru` command, `python -m eneru` |
| Installed Script Path | `/opt/ups-monitor/ups_monitor.py` | `/opt/ups-monitor/eneru.py` |
| Packaging Config | None | `pyproject.toml` (PEP 517/518) |
| Optional Dependencies | Manual installation | `[notifications]`, `[dev]`, `[docs]` extras |
| Test Count | ~98 tests | 178 tests |
| Test Imports | `from ups_monitor import ...` | `from eneru import ...` |
| Module Invocation | Not supported | `python -m eneru` supported |
| Public API | Direct script import | `__init__.py` exports all public APIs |

### v4.5 vs v4.4

| Feature | v4.4 | v4.5 |
|---------|------|------|
| Compose Shutdown | Not supported | Ordered compose file shutdown with per-file timeout |
| Container Shutdown | Stop all containers | Compose first -> remaining containers (configurable) |
| Remote Pre-Shutdown | Direct shutdown only | Pre-shutdown commands (actions + custom) |
| Remote Shutdown | Sequential | Parallel (threaded) with `parallel` option |
| Predefined Actions | None | 8 actions (stop_containers, stop_vms, stop_proxmox_*, etc.) |
| Dependency Ordering | Not possible | `parallel: false` for sequential servers |
| Filesystem Sync | `os.sync()` only | `os.sync()` + 2s sleep for controller cache flush |

### v4.4 vs v4.3

| Feature | v4.3 | v4.4 |
|---------|------|------|
| Documentation | README only (~1200 lines) | Read The Docs + slim README (~145 lines) |
| Documentation Theme | GitHub markdown | MkDocs Material (dark mode) |
| Documentation Search | None | Full-text search |
| Code Blocks | Basic | Copy buttons, syntax highlighting |
| Installation Docs | Single section | Tabbed by distro |
| Upgrade/Uninstall | Undocumented | Dedicated instructions |
| Content Organization | Monolithic README | 7 focused pages |
| Navigation | Scroll through README | Sidebar navigation |

### v4.3 vs v4.2

| Feature | v4.2 | v4.3 |
|---------|------|------|
| Installation Method | Manual (install.sh) | Native packages (.deb/.rpm) |
| Package Repository | None | GitHub Pages (GPG signed) |
| Version Display | None | `-v`/`--version` flag |
| Service Auto-Start | Yes (via install.sh) | No (manual enable required) |
| Config on Upgrade | May overwrite | Preserved (conffile) |
| Discord @ Mentions | Could trigger false mentions | Escaped with zero-width space |
| Build System | None | nFPM + GitHub Actions |
| Distribution | GitHub clone only | apt/dnf + GitHub releases |

### v4.2 vs v4.1

| Feature | v4.1 | v4.2 |
|---------|------|------|
| Notification Backend | Native Discord (requests) | Apprise (100+ services) |
| Supported Services | Discord only | Discord, Slack, Telegram, ntfy, Email, 100+ more |
| Notification Behavior | Blocking during shutdown | Non-blocking (fire-and-forget) |
| Network Failure Impact | Delays shutdown 10-30s+ | Zero delay |
| Test Command | None | `--test-notifications` |
| Avatar Support | Hardcoded | Configurable per-service |
| Title Support | Hardcoded | Optional, configurable |
| Test Suite | None | 80+ pytest tests |
| Code Coverage | None | Codecov integration |
| Script Filename | `ups-monitor.py` | `ups_monitor.py` |

### v4.1 vs v4.0

| Feature | v4.0 | v4.1 |
|---------|------|------|
| Container Runtime | Docker only | Docker + Podman |
| Runtime Detection | N/A | Auto-detect (prefers Podman) |
| Rootless Containers | No | Yes (Podman) |
| Project Name | UPS Tower | Eneru |
| Trigger Documentation | Basic | Comprehensive with examples |
| Changelog Format | Basic | Keep a Changelog format |

### v4.0 vs v3.0

| Feature | v3.0 | v4.0 |
|---------|------|------|
| Configuration | Python dataclass in code | External YAML file |
| Remote Servers | Single hardcoded NAS | Multiple configurable servers |
| Shutdown Commands | Hardcoded per system type | Customizable per server |
| Feature Toggles | Edit source code | Enable/disable in config |
| CLI Options | None | `--config`, `--dry-run`, `--validate-config` |
| Dependencies | Hard failure if missing | Graceful degradation |
| Installation | Overwrites config | Preserves existing config |
| Documentation | Basic README | Comprehensive docs, examples, guides |
| Community | None | Issue templates, PR templates, CI |
| License | Apache 2.0 | MIT |

### v3.0 vs v2.0

| Feature | v2.0 (Bash) | v3.0 (Python) |
|---------|-------------|---------------|
| Language | Bash 4.0+ | Python 3.9+ |
| JSON Handling | External `jq` | Native (`requests`) |
| Math Operations | External `bc` | Native |
| Configuration | Shell variables | Python dataclass |
| State Management | File-based with shell parsing | In-memory + file |
| Type Safety | None | Full type hints |
| Error Handling | Shell traps | Python exceptions |
| String Formatting | Shell variable expansion | Python f-strings |

### v2.0 vs v1.0

| Feature | v1.0 | v2.0 |
|---------|------|------|
| Notifications | None | Discord webhooks with rich embeds |
| Event Tracking | Basic logging | Stateful tracking (prevents spam) |
| VM Shutdown | Fixed 10s wait | Dynamic up to 30s + force destroy |
| NAS Auth | Password (`sshpass`) | SSH keys (no passwords) |
| Unmounting | Basic | Timeout-protected (hang-proof) |
| Voltage Monitoring | Relative | Absolute thresholds |
| Depletion Rate | 60s window, 15 samples | 300s window, 30 samples + grace period |
| AVR Monitoring | None | Boost/Trim detection |
| Connection Handling | Basic retry | Stale detection + failsafe |
| Crisis Reporting | None | Elevated notifications during shutdown |
| Shutdown Triggers | 4 triggers | 4 triggers + FSD flag |
| Bypass/Overload | None | Full detection + resolution tracking |
