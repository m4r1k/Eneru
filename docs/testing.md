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
| **Unit Tests** | Every commit | Logic, state machine, edge cases (410 tests) |
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

628 tests across 25 files:

- Configuration parsing (116 tests across 6 files) -- YAML options, defaults, multi-UPS detection, trigger inheritance, ownership validation, trigger_on enum validation, shutdown_order parsing and validation (incl. YAML type coercion edge cases, mutual-exclusion error with `parallel`), shutdown_safety_margin parsing and validation, plus the **redundancy-group** dataclass: parsing, defaults, inheritance, multi-group, malformed-entry handling (9 in `test_config_loading.py`), and validation rules (24 in `test_config_validation.py`: `min_healthy` bounds, unknown UPS references, duplicate sources, missing names, duplicate names, enum checks, local-resource ownership, cross-tier server conflicts, `is_local` uniqueness across all groups). Files: `test_config_loading.py` (19), `test_config_notifications.py` (9), `test_config_filesystems.py` (3), `test_config_vm_containers.py` (7), `test_config_remote.py` (29), `test_config_validation.py` (49)
- Multi-UPS coordination (65 tests) -- coordinator routing, is_local/drain/trigger_on, defense-in-depth lock, battery anomaly with jitter filtering, notification prefixing, runtime is_local enforcement, exit_after_shutdown in coordinator, ownership rejection (VMs/containers/filesystems), plus full coverage of MultiUPSCoordinator lifecycle (initialize, start_monitors, run_monitor crash path, handle_signal, wait_for_completion, real local-shutdown command path, drain edge cases, log fallback). 6 new tests cover redundancy-group wiring inside the coordinator (in_redundancy set computation, in_redundancy_group flag passed to monitors, evaluator + executor instantiation, signal-handler join of evaluator threads).
- Core monitor logic (57 tests) -- OL/OB/FSD state machine, all four shutdown triggers, failsafe, shutdown sequence ordering, multi-phase shutdown (compute_effective_order, phased execution, thread verification, backward compat, deadline-based join, per-server safety margin). 12 new tests cover the **advisory-mode** branches at the 3 trigger sites (T1-T4, FSD, FAILSAFE) under `in_redundancy_group=True`, plus regression tests verifying the legacy single-UPS / independent-group paths are byte-identical.
- Health model (32 tests) -- pure-function classification of `HealthSnapshot` into `HEALTHY` / `DEGRADED` / `CRITICAL` / `UNKNOWN` per the documented priority (FAILED beats trigger_active beats FSD beats OB), `5 * check_interval` staleness rule, parametrised `ups.status` and `connection_state` table.
- Redundancy runtime (28 tests) -- evaluator counting and policy translation for `degraded_counts_as` / `unknown_counts_as`, executor synthetic Config wiring + flag-file namespace + sanitisation, dry-run cleanup, idempotency (in-process + against pre-existing flag), local-resource gating on `is_local`, log-prefix and `@`-escape behaviour, evaluator thread lifecycle and exception swallowing, cross-group cascade regression.
- Shutdown phase mixins (46 tests across 3 files) -- per-mixin coverage for the shutdown phase code: `test_shutdown_vms.py` (7) covers libvirt graceful shutdown, force-destroy on timeout, dry-run, missing virsh, no running VMs; `test_shutdown_containers.py` (26) covers runtime detection (docker/podman/auto), compose subcommand availability, compose-stack shutdown with per-file timeouts, container shutdown with dry-run + real-stop paths, ps failure handling; `test_shutdown_filesystems.py` (13) covers sync (real, dry-run, disabled), unmount with options, timeout (exit 124), busy-mount handling, already-unmounted detection, multi-mount independence
- Remote commands (29 tests) -- SSH execution, pre-shutdown actions, parallel and sequential modes
- Connection grace period (26 tests) -- OK/GRACE_PERIOD/FAILED transitions, flap detection, stale data
- TUI dashboard (50 tests) -- state file parsing, log filtering, human-readable status, --once output, plus the new graph integration: `cycle()` keybinding helper, per-UPS stats DB path mirroring the daemon's sanitization, `render_graph_text` no-data + with-samples + unknown-metric paths, `run_once --graph` block. Events-panel additions: `query_events_for_display` for single-UPS (no label prefix), multi-UPS (with `[label]` prefix and timestamp interleave), time-window exclusion, max_events cap; `run_once --events-only` SQLite path + log-tail fallback + "(no events)" placeholder. Width-helper regression: `display_width` correctly counts emoji and CJK as 2 cells; `truncate_to_width` clips before partial double-width chars; `render_logs_panel` no longer overflows the gold-panel right edge with emoji-heavy event lines.
- BrailleGraph (24 tests) -- code-point arithmetic against hand-computed glyphs (top-left, top-right, bottom row); `supported()` detection (LANG=C, UTF-8 vs ISO-8859-1); `plot()` geometry (height/width match request, empty data, zero dims); auto-scale (max at top, min at bottom, zero-range padding); explicit bounds clipping (above/below, NULL skipped); fallback character path; `render_to_window` curses helper.
- CLI (20 tests) -- subcommands, bare invocation, multi-UPS validate
- Calculations (17 tests) -- depletion rate, battery history
- Notifications (16 tests) -- formatting, retry, Apprise
- State (23 tests) -- transition tests + new lock/snapshot/concurrent-write infrastructure (8) used by the redundancy evaluator
- SQLite statistics (42 tests) -- schema + WAL/synchronous pragmas, hot-path zero-I/O `buffer_sample`, thread-safe buffering, single-transaction flush, 5-min and hourly aggregation, retention purge, tier-aware `query_range`, events round-trip, read-only TUI connection, concurrent reader+writer, failure-isolation contract (every method swallows `sqlite3.Error` / `OSError`), `StatsConfig` YAML round-trip, `StatsWriter` thread lifecycle.
- Packaging structural guard (3 tests) -- every `src/eneru/**/*.py` is referenced in `nfpm.yaml`; no dangling `src:` references; `/var/lib/eneru` directory entry present (catches the PR #23 class of bug where a new module file fails at deb/rpm install with `ModuleNotFoundError` while pip CI passes silently).
- Triggers, integration, command execution (31 tests combined)

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

The E2E workflow (`.github/workflows/e2e.yml`) runs 31 tests on every push and PR:

| Test | Description |
|------|-------------|
| **Test 1** | Validate E2E config against real NUT server |
| **Test 2** | Monitor normal state - verify no false shutdown triggers |
| **Test 3** | Detect power failure in dry-run mode |
| **Test 4** | SSH remote shutdown with real command execution |
| **Test 5** | FSD (Forced Shutdown) flag triggers immediate shutdown |
| **Test 6** | Voltage event detection (brownout, AVR) |
| **Test 7** | Notification delivery (if `E2E_NOTIFICATION_URL` secret configured) |
| **Test 8** | Multi-UPS config validation against real NUT (both UPS1 and UPS2) |
| **Test 9** | Multi-UPS isolation: UPS1 fails, UPS2 unaffected |
| **Test 10** | Multi-UPS both online: no false shutdown triggers |
| **Test 11** | Ownership validation: non-local group with containers rejected |
| **Test 12** | CLI safety: bare `eneru` shows help, does not start daemon |
| **Test 13** | TUI `--once` snapshot outputs UPS status |
| **Test 14** | Multi-UPS concurrent failure: both UPSes fail, both groups shut down |
| **Test 15** | Non-local failure: UPS2 fails, UPS1 and local resources unaffected |
| **Test 16** | Local drain (`drain_on_local_shutdown=true`): all groups drain before local shutdown |
| **Test 17** | Local no-drain (`drain_on_local_shutdown=false`): only local group shuts down |
| **Test 18** | Power recovery: OB then power restored, no shutdown triggered |
| **Test 19** | Multi-phase shutdown ordering: 3 SSH targets across 2 phases (`shutdown_order: 1, 1, 2`) — verifies all received shutdown, "Phase N/M (order=X)" log lines, and timestamp ordering across phases |
| **Test 20** | Redundancy-group config validation: valid config passes (with `Redundancy groups (1):` summary); `min_healthy: 0` exits non-zero with the documented error |
| **Test 21** | Redundancy quorum *holds* when 1 of 2 members healthy (`min_healthy: 1`) — no shutdown |
| **Test 22** | Redundancy quorum *exhausted* (both critical) — `quorum LOST` log + `REDUNDANCY GROUP SHUTDOWN` sequence |
| **Test 23** | UNKNOWN handling under default `unknown_counts_as: critical` — evaluator startup line confirmed |
| **Test 24** | Both UPSes critical → fail-safe redundancy shutdown fires |
| **Test 25** | Cross-group cascade: a UPS shared between an independent group and a redundancy group does not falsely fire the redundancy shutdown when the other member is healthy |
| **Test 26** | Advisory-mode log signature: `Trigger condition met (advisory, redundancy group): ...` appears for redundancy members; `Triggering immediate shutdown` does *not* |
| **Test 27** | Separate-Eneru-UPS topology: TestUPS protects the host (`is_local: true`), the redundancy group protects a remote rack — rack shutdown fires, host UPS unaffected |
| **Test 28** | SQLite stats DB created at `db_directory`, `samples` table populated, `events` table contains the `DAEMON_START` row |
| **Test 29** | Stats writer failure isolation: a broken `db_directory` (file where a directory was expected) logs the warning but does *not* crash the daemon |
| **Test 30** | `eneru monitor --once --graph charge --time 1h` renders the ASCII / Braille graph header and y-axis label with seeded sample data |
| **Test 31** | `eneru monitor --once --events-only` reads from the SQLite events table — verified by injecting a known event row into the DB and asserting the line surfaces in the output |

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
