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
           ╱   pytest + Coverage (338)   ╲
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
| **Unit Tests** | Every commit | Logic, state machine, edge cases (338 tests) |
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

338 tests across 13 files:

- Configuration parsing (83 tests) -- YAML options, defaults, multi-UPS detection, trigger inheritance, ownership validation, trigger_on enum validation, shutdown_order parsing and validation (incl. YAML type coercion edge cases, mutual-exclusion error with `parallel`), shutdown_safety_margin parsing and validation
- Core monitor logic (45 tests) -- OL/OB/FSD state machine, all four shutdown triggers, failsafe, shutdown sequence ordering, multi-phase shutdown (compute_effective_order, phased execution, thread verification, backward compat, deadline-based join, per-server safety margin)
- Multi-UPS coordination (33 tests) -- coordinator routing, is_local/drain/trigger_on, defense-in-depth lock, battery anomaly with jitter filtering, notification prefixing, runtime is_local enforcement, exit_after_shutdown in coordinator, ownership rejection (VMs/containers/filesystems)
- Remote commands (29 tests) -- SSH execution, pre-shutdown actions, parallel and sequential modes
- Connection grace period (26 tests) -- OK/GRACE_PERIOD/FAILED transitions, flap detection, stale data
- TUI dashboard (23 tests) -- state file parsing, log filtering, human-readable status, --once output
- CLI (20 tests) -- subcommands, bare invocation, multi-UPS validate
- Calculations (17 tests) -- depletion rate, battery history
- Notifications (16 tests) -- formatting, retry, Apprise
- State, triggers, integration, command execution (46 tests combined)

To run tests locally:

```bash
# Install dev dependencies
pip install ".[dev]"

# Run tests with coverage
pytest --cov=src/eneru --cov-report=term -v

# Run specific test file
pytest tests/test_config.py -v
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

The E2E workflow (`.github/workflows/e2e.yml`) runs 19 tests on every push and PR:

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
