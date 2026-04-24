# Testing

Every commit runs unit tests, integration tests across 9 Linux distributions, end-to-end tests with real NUT/SSH/Docker services, and configuration validation.

---

## Testing strategy

```
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
              ╱    7 Linux Distros    ╲
             ╱─────────────────────────╲
            ╱         Unit Tests        ╲
           ╱   pytest + Coverage (410)   ╲
          ╱      7 Python Versions        ╲
         ╱─────────────────────────────────╲
        ╱          Static Analysis          ╲
       ╱   Syntax Check + Config Validation  ╲
      ╱───────────────────────────────────────╲
     ╱          AI-Assisted Development        ╲
    ╱   Claude Code Review & Quality Assurance  ╲
   ╱─────────────────────────────────────────────╲
```

| Layer | Frequency | What It Tests |
|-------|-----------|---------------|
| **AI-Assisted Dev** | Continuous | Code review, implementation guidance |
| **Static Analysis** | Every commit | Python syntax, config validation |
| **Unit Tests** | Every commit | Logic, state machine, edge cases (813 tests) |
| **Integration** | Every commit | Package install on 7 Linux distros |
| **E2E Tests** | Every commit | Full workflow with real NUT, SSH, Docker |
| **Real UPS** | Pre-release | Actual hardware, power events |

---

## Automated testing

All automated tests run via GitHub Actions on every commit and pull request.

### Validate workflow

The **Validate** workflow runs on every push and pull request to `main`:

| Check | Description |
|-------|-------------|
| **Python Syntax** | Verifies the code compiles correctly |
| **Unit Tests** | Runs the full pytest test suite with coverage |
| **Configuration Validation** | Validates the default and example configs |

**Python versions tested:** 3.9, 3.10, 3.11, 3.12, 3.13, 3.14, 3.15-dev

### Integration workflow

The **Integration** workflow tests package installation on real distributions:

#### Package installation testing

Tests `.deb` and `.rpm` package installation:

| Distribution | Version | Package |
|--------------|---------|---------|
| Debian | 11 (Bullseye) | .deb |
| Debian | 12 (Bookworm) | .deb |
| Debian | 13 (Trixie) | .deb |
| Ubuntu | 22.04 (Jammy) | .deb |
| Ubuntu | 24.04 (Noble) | .deb |
| Ubuntu | 26.04 (Resolute) | .deb |
| RHEL | 8 (Ootpa) | .rpm |
| RHEL | 9 (Plow) | .rpm |
| RHEL | 10 (Coughlan) | .rpm |

Each test:

1. Installs the package using the native package manager
2. Verifies files are installed in the correct locations
3. Runs `--version` to confirm the script executes
4. Validates the default and example configurations

!!! note "Debian 11 and PyYAML"
    Debian 11 ships PyYAML 5.3.1, which is below the `>=5.4.1` requirement in `pyproject.toml`. The `.deb` package does not enforce a PyYAML version, and Eneru only uses `yaml.safe_load()` which is identical across both versions, so CI passes. However, all real-world testing and production deployments have used PyYAML 5.4.1 or newer. If you run into YAML parsing issues on Debian 11, upgrade PyYAML first: `pip install --upgrade PyYAML`.

#### Pip installation testing

Tests `pip install .` to ensure `pyproject.toml` is valid:

| Environment | Python Versions |
|-------------|-----------------|
| Ubuntu runner | 3.9, 3.10, 3.11, 3.12, 3.13, 3.14, 3.15-dev |
| Debian 11 container | System Python (3.9) |
| Debian 13 container | System Python |
| Ubuntu 26.04 container | System Python |
| RHEL 10 container | System Python |

!!! note "Ubuntu 22.04 not tested with pip"
    Ubuntu 22.04 ships pip 22.0.2, which has a known regression with `pyproject.toml` dynamic version metadata. `pip install eneru` produces an `UNKNOWN-0.0.0` package and no `eneru` entry point. Upgrading pip fixes it, but that no longer tests the real system environment, so Ubuntu 22.04 is excluded from pip-in-container tests. It is still tested with `.deb` package installation. If you need pip install on Ubuntu 22.04, upgrade pip first (`pip install --upgrade pip`) or use a virtualenv.

---

## Test coverage

813 tests across 28 files:

- Configuration parsing (142 tests across 6 files): YAML options, defaults, multi-UPS detection, trigger inheritance, ownership validation, `trigger_on` enum validation, `shutdown_order` parsing and validation (YAML type coercion edge cases, mutual-exclusion error with `parallel`), `shutdown_safety_margin` parsing and validation, `voltage_sensitivity` strict-enum + per-UPS-group + explicit-flag round-trip + redundancy-group symmetry. Adds the redundancy-group dataclass (parsing, defaults, inheritance, multi-group, malformed-entry handling) and 24 validation rules (`min_healthy` bounds, unknown UPS references, duplicate sources, missing names, duplicate names, enum checks, local-resource ownership, cross-tier server conflicts, `is_local` uniqueness). Files: `test_config_loading.py` (20), `test_config_notifications.py` (9), `test_config_filesystems.py` (3), `test_config_vm_containers.py` (7), `test_config_remote.py` (29), `test_config_validation.py` (74).
- Multi-UPS coordination (65 tests): coordinator routing, `is_local` / drain / `trigger_on`, defense-in-depth lock, battery anomaly with jitter filtering, notification prefixing, runtime `is_local` enforcement, `exit_after_shutdown` in coordinator, ownership rejection (VMs / containers / filesystems), and the full `MultiUPSCoordinator` lifecycle (`initialize`, `start_monitors`, `run_monitor` crash path, `handle_signal`, `wait_for_completion`, real local-shutdown command path, drain edge cases, log fallback). 6 new tests cover redundancy-group wiring inside the coordinator: `in_redundancy` set computation, `in_redundancy_group` flag passed to monitors, evaluator + executor instantiation, signal-handler join of evaluator threads.
- Core monitor logic (57 tests): OL / OB / FSD state machine, all four shutdown triggers, failsafe, shutdown sequence ordering, multi-phase shutdown (`compute_effective_order`, phased execution, thread verification, backward compat, deadline-based join, per-server safety margin). 12 new tests cover the advisory-mode branches at the three trigger sites (T1-T4, FSD, FAILSAFE) under `in_redundancy_group=True`, plus regression tests that verify the legacy single-UPS and independent-group paths stay byte-identical.
- Health model (32 tests): pure-function classification of `HealthSnapshot` into `HEALTHY` / `DEGRADED` / `CRITICAL` / `UNKNOWN` with the documented priority (FAILED beats `trigger_active` beats FSD beats OB), `5 * check_interval` staleness rule, parametrised `ups.status` and `connection_state` table.
- Redundancy runtime (28 tests): evaluator counting and policy translation for `degraded_counts_as` / `unknown_counts_as`, executor synthetic Config wiring, flag-file namespace and sanitisation, dry-run cleanup, idempotency (in-process and against a pre-existing flag), local-resource gating on `is_local`, log-prefix and `@`-escape behaviour, evaluator thread lifecycle and exception swallowing, cross-group cascade regression.
- Shutdown phase mixins (46 tests across 3 files): per-mixin coverage. `test_shutdown_vms.py` (7) covers libvirt graceful shutdown, force-destroy on timeout, dry-run, missing virsh, no running VMs. `test_shutdown_containers.py` (26) covers runtime detection (docker / podman / auto), compose subcommand availability, compose-stack shutdown with per-file timeouts, container shutdown (dry-run and real-stop paths), `ps` failure handling. `test_shutdown_filesystems.py` (13) covers sync (real, dry-run, disabled), unmount with options, timeout (exit 124), busy-mount handling, already-unmounted detection, multi-mount independence.
- Remote commands (29 tests): SSH execution, pre-shutdown actions, parallel and sequential modes.
- Connection grace period (26 tests): OK / GRACE_PERIOD / FAILED transitions, flap detection, stale data.
- TUI dashboard (50 tests): state file parsing, log filtering, human-readable status, `--once` output. Graph integration: `cycle()` keybinding helper, per-UPS stats DB path that mirrors the daemon's sanitization, `render_graph_text` (no-data, with-samples, unknown-metric paths), `run_once --graph` block. Events-panel: `query_events_for_display` for single-UPS (no label prefix) and multi-UPS (with `[label]` prefix and timestamp interleave), time-window exclusion, `max_events` cap. `run_once --events-only` SQLite path with log-tail fallback and "(no events)" placeholder. Width-helper regression: `display_width` counts emoji and CJK as 2 cells, `truncate_to_width` clips before a partial double-width char, `render_logs_panel` no longer overflows the gold-panel right edge with emoji-heavy lines.
- BrailleGraph (24 tests): code-point arithmetic against hand-computed glyphs (top-left, top-right, bottom row), `supported()` detection (`LANG=C`, UTF-8 vs ISO-8859-1), `plot()` geometry (height / width match request, empty data, zero dims), auto-scale (max at top, min at bottom, zero-range padding), explicit bounds clipping (above and below, NULL skipped), fallback character path, `render_to_window` curses helper.
- CLI (20 tests): subcommands, bare invocation, multi-UPS validate.
- Calculations (17 tests): depletion rate, battery history.
- Notifications (16 tests): formatting, retry, Apprise.
- State (23 tests): transition tests, plus the new lock/snapshot/concurrent-write infrastructure (8) used by the redundancy evaluator.
- SQLite statistics (42 tests): schema and WAL / synchronous pragmas, hot-path `buffer_sample` (no I/O), thread-safe buffering, single-transaction flush, 5-min and hourly aggregation, retention purge, tier-aware `query_range`, events round-trip, read-only TUI connection, concurrent reader and writer, failure-isolation contract (every method swallows `sqlite3.Error` / `OSError`), `StatsConfig` YAML round-trip, `StatsWriter` thread lifecycle.
- Packaging structural guard (3 tests): every `src/eneru/**/*.py` is referenced in `nfpm.yaml`. No dangling `src:` references. `/var/lib/eneru` directory entry present. Catches the PR #23 class of bug where a new module file fails at deb/rpm install with `ModuleNotFoundError` while pip CI passes silently.
- Voltage health (78 tests): grid-snap helper across STANDARD_GRIDS, autodetect re-snap (NUT-mis-reports-nominal path + per-UPS sensitivity preserved across re-snap), single-formula thresholds at every standard grid × every preset, Chris's repro (issue #4: 120V/106/127 → 108/132, no false alarm at 122.4V, brownout at 107V), notification-text framing under any preset, hysteresis dwell + flap suppression, severity bypass at ±15%, severity-escalation refresh inside same state, migration-warning matrix (fires/suppresses + per-side delta + acknowledgement-via-explicit), legacy-band recompute helpers preserved for the migration comparison.
- Triggers, integration, command execution (31 tests combined).

To run tests locally:

```bash
# Install dev dependencies
pip install ".[dev]"

# Run tests with coverage
pytest --cov=src/eneru --cov-report=term -v

# Run specific test file
pytest tests/test_config_loading.py -v
```

---

## End-to-end (E2E) testing

The E2E tests run the full monitoring and shutdown workflow using real services.

### E2E test environment

The E2E tests spin up a test environment with Docker Compose:

```
┌─────────────────────────────────────────────────────────────┐
│                    Test Environment                         │
│                                                             │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐   │
│  │  NUT Server  │    │  SSH Target  │    │   Target     │   │
│  │  (dummy-ups) │    │   (sshd)     │    │  Containers  │   │
│  │  :3493       │    │   :2222      │    │              │   │
│  └──────────────┘    └──────────────┘    └──────────────┘   │
│         │                   │                   │           │
│         └───────────────────┼───────────────────┘           │
│                             │                               │
│                     ┌───────▼───────┐                       │
│                     │    Eneru      │                       │
│                     │  (under test) │                       │
│                     └───────────────┘                       │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

- NUT server with dummy driver, simulating UPS states without real hardware
- SSH target container that receives and logs shutdown commands
- Target containers that Eneru can shut down
- tmpfs mount for testing filesystem unmount operations

### UPS scenarios

The E2E tests use scenario files to simulate different UPS states:

| Scenario | Description | Triggers Shutdown? |
|----------|-------------|-------------------|
| `online-charging.dev` | Normal operation, fully charged | No |
| `on-battery.dev` | On battery, battery OK | No |
| `low-battery.dev` | Battery below 20% threshold | Yes |
| `critical-runtime.dev` | Runtime below 600s threshold | Yes |
| `fsd.dev` | UPS signals Forced Shutdown | Yes |
| `avr-boost.dev` | AVR boosting low voltage | No |
| `brownout.dev` | Voltage below warning threshold | No |
| `overload.dev` | UPS overloaded | No |

### E2E test cases

The E2E workflow (`.github/workflows/e2e.yml`) runs 36 tests on every push and PR.
Tests are partitioned into five parallel matrix jobs (`E2E CLI`,
`E2E UPS Single`, `E2E UPS Multi`, `E2E Redundancy`, `E2E Stats`) so the
total wall-clock is bounded by the slowest group rather than the sum of
all 36 tests. v5.1.2 split the previous `Redundancy and Stats` group
in two so the heaviest job no longer gates the matrix on its own.

| Test | Group | Description |
|------|-------|-------------|
| **Test 1** | CLI | Validate E2E config against real NUT server |
| **Test 2** | UPS Single | Monitor normal state - verify no false shutdown triggers |
| **Test 3** | UPS Single | Detect power failure in dry-run mode |
| **Test 4** | UPS Single | SSH remote shutdown with real command execution |
| **Test 5** | UPS Single | FSD (Forced Shutdown) flag triggers immediate shutdown |
| **Test 6** | UPS Single | Voltage event detection (brownout, AVR) |
| **Test 7** | UPS Single | Notification delivery (if `E2E_NOTIFICATION_URL` secret configured) |
| **Test 8** | CLI | Multi-UPS config validation against real NUT (both UPS1 and UPS2) |
| **Test 9** | UPS Multi | Multi-UPS isolation: UPS1 fails, UPS2 unaffected |
| **Test 10** | UPS Multi | Multi-UPS both online: no false shutdown triggers |
| **Test 11** | CLI | Ownership validation: non-local group with containers rejected |
| **Test 12** | CLI | CLI safety: bare `eneru` shows help, does not start daemon |
| **Test 13** | CLI | TUI `--once` snapshot outputs UPS status |
| **Test 14** | UPS Multi | Multi-UPS concurrent failure: both UPSes fail, both groups shut down |
| **Test 15** | UPS Multi | Non-local failure: UPS2 fails, UPS1 and local resources unaffected |
| **Test 16** | UPS Multi | Local drain (`drain_on_local_shutdown=true`): all groups drain before local shutdown |
| **Test 17** | UPS Multi | Local no-drain (`drain_on_local_shutdown=false`): only local group shuts down |
| **Test 18** | UPS Multi | Power recovery: OB then power restored, no shutdown triggered |
| **Test 19** | UPS Multi | Multi-phase shutdown ordering: 3 SSH targets across 2 phases (`shutdown_order: 1, 1, 2`) — verifies all received shutdown, "Phase N/M (order=X)" log lines, and timestamp ordering across phases |
| **Test 20** | CLI | Redundancy-group config validation: valid config passes (with `Redundancy groups (1):` summary); `min_healthy: 0` exits non-zero with the documented error |
| **Test 21** | Redundancy | Redundancy quorum *holds* when 1 of 2 members healthy (`min_healthy: 1`) — no shutdown |
| **Test 22** | Redundancy | Redundancy quorum *exhausted* (both critical) — `quorum LOST` log + `REDUNDANCY GROUP SHUTDOWN` sequence |
| **Test 23** | Redundancy | UNKNOWN handling under default `unknown_counts_as: critical` — evaluator startup line confirmed |
| **Test 24** | Redundancy | Both UPSes critical → fail-safe redundancy shutdown fires |
| **Test 25** | Redundancy | Cross-group cascade: a UPS shared between an independent group and a redundancy group does not falsely fire the redundancy shutdown when the other member is healthy |
| **Test 26** | Redundancy | Advisory-mode log signature: `Trigger condition met (advisory, redundancy group): ...` appears for redundancy members; `Triggering immediate shutdown` does *not* |
| **Test 27** | Redundancy | Separate-Eneru-UPS topology: TestUPS protects the host (`is_local: true`), the redundancy group protects a remote rack — rack shutdown fires, host UPS unaffected |
| **Test 28** | Stats | SQLite stats DB created at `db_directory`, `samples` table populated, `events` table contains the `DAEMON_START` row |
| **Test 29** | Stats | Stats writer failure isolation: a broken `db_directory` (file where a directory was expected) logs the warning but does *not* crash the daemon |
| **Test 30** | Stats | `eneru monitor --once --graph charge --time 1h` renders the ASCII / Braille graph header and y-axis label with seeded sample data |
| **Test 31** | Stats | `eneru monitor --once --events-only` reads from the SQLite events table — verified by injecting a known event row into the DB and asserting the line surfaces in the output |
| **Test 32** | Stats | Voltage auto-detect re-snaps NUT mis-reported nominal — scenario `us-grid-misreport.dev` reports `input.voltage.nominal=230V` while actual `input.voltage=120V`; daemon detects the mismatch, logs `auto-detect re-snap`, records a `VOLTAGE_AUTODETECT_MISMATCH` event with `notification_sent=0`, and confirms `meta.schema_version=3` |
| **Test 33** | UPS Single | Issue #4: voltage_sensitivity preset prevents Chris's false-alarm flood — scenario `us-grid-hot.dev` (120V/106/127, input 122.4V) does NOT fire `OVER_VOLTAGE_DETECTED` on default `normal` (warnings 108/132), startup log says `sensitivity=normal`, the migration warning surfaces for the narrow-firmware UPS, and `us-grid-brownout.dev` at 107V still fires `BROWNOUT_DETECTED` |
| **Test 34** | Stats | v5.2 panic-attack coalescing: `ON_BATTERY` + `POWER_RESTORED` rows pointed at TEST-NET-1 stay pending; the worker folds them into one `📊 Brief Power Outage` summary with the originals cancelled with `cancel_reason='coalesced'` |
| **Test 35** | Stats | v5.2.1 single-restart-notification (single-UPS): SIGTERM enqueues `🛑 Service Stopped` AFTER `flush()` so it lands as `pending`; the next daemon's classifier cancels it with `cancel_reason='superseded'` and emits a single `🔄 Restarted` row — proves one notification per restart, never two |
| **Test 36** | UPS Multi | v5.2.1 single-restart-notification (multi-UPS coordinator): same contract as Test 35 but exercises `MultiUPSCoordinator._cancel_prev_pending_lifecycle_rows` which sweeps each per-UPS store on the next coordinator startup |

### Running E2E tests locally

To run the E2E tests locally:

```bash
# From repository root
cd tests/e2e

# Generate SSH keys for the test
ssh-keygen -t ed25519 -f /tmp/e2e-ssh-key -N ""
cp /tmp/e2e-ssh-key.pub ssh-target/authorized_keys

# Start the test environment
docker compose up -d --build

# Wait for services to be ready
sleep 10

# Verify NUT is working (single-UPS and multi-UPS)
upsc TestUPS@localhost:3493
upsc UPS1@localhost:3493
upsc UPS2@localhost:3493

# Run Eneru in dry-run mode
eneru validate --config config-e2e-dry-run.yaml

# Simulate a power failure (single-UPS)
cp scenarios/low-battery.dev scenarios/apply.dev
eneru run --config config-e2e-dry-run.yaml --exit-after-shutdown

# Multi-UPS: simulate UPS1 failure while UPS2 stays online
cp scenarios/low-battery.dev scenarios/apply-UPS1.dev
eneru run --config config-e2e-multi-ups.yaml --exit-after-shutdown

# Cleanup
docker compose down -v
```

See `tests/e2e/README.md` for more details.

---

## Pre-release validation

!!! important "Real hardware testing"
    Before each official release, Eneru is tested on real hardware with actual UPS units and simulated power events.

### Test environment

The pre-release environment consists of:

- Physical UPS units connected via USB and network
- NUT server configured and serving UPS data
- Multiple test systems (physical and virtual)
- Real power event simulation (unplugging UPS from mains)

### Validation checklist

Before each release:

- [ ] Power loss detection: UPS status correctly transitions to `OB` (on battery)
- [ ] Trigger thresholds: shutdown initiates at configured battery/runtime levels
- [ ] Notification delivery: alerts sent to configured services
- [ ] Remote server shutdown: SSH-based shutdown executes successfully
- [ ] Container shutdown: Docker/Podman containers stop gracefully
- [ ] VM shutdown: libvirt VMs shut down before host
- [ ] Local shutdown: system powers off cleanly
- [ ] Recovery: service resumes monitoring after power restoration

### Simulating power events

To test without actually losing power:

```bash
# On the NUT server, force the UPS into "on battery" mode (if supported)
upsrw -s ups.status=OB your-ups@localhost

# Or use a test UPS driver
upsdrvctl -t stop

# Monitor Eneru's response
journalctl -u eneru.service -f
```

!!! warning "Test responsibly"
    When testing shutdown sequences, ensure you have console access to the system. Remote SSH sessions may be terminated during the shutdown process.

---

## Continuous integration

### Workflow files

| Workflow | File | Trigger |
|----------|------|---------|
| Validate | `.github/workflows/validate.yml` | Push, PR |
| Integration | `.github/workflows/integration.yml` | Push, PR |
| E2E | `.github/workflows/e2e.yml` | Push, PR |
| Release | `.github/workflows/release.yml` | Release published |
| PyPI | `.github/workflows/pypi.yml` | Release published |

### Viewing results

- [Validate Workflow Runs](https://github.com/m4r1k/Eneru/actions/workflows/validate.yml)
- [Integration Workflow Runs](https://github.com/m4r1k/Eneru/actions/workflows/integration.yml)
- [E2E Workflow Runs](https://github.com/m4r1k/Eneru/actions/workflows/e2e.yml)
- [Release Workflow Runs](https://github.com/m4r1k/Eneru/actions/workflows/release.yml)
- [PyPI Workflow Runs](https://github.com/m4r1k/Eneru/actions/workflows/pypi.yml)

---

## Contributing tests

When contributing new features or bug fixes:

1. Add unit tests for new functionality in the `tests/` directory
2. Update example configs if new configuration options are added
3. Test locally before submitting a pull request:

```bash
# Run the full test suite
pytest -v

# Validate your config changes
eneru validate --config /etc/ups-monitor/config.yaml
```

See the [GitHub repository](https://github.com/m4r1k/Eneru) for contribution guidelines.
