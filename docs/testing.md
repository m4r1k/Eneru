# Testing

Eneru uses a comprehensive testing strategy to ensure reliability across different environments. **Every commit triggers the full test suite**—unit tests, integration tests across 7 Linux distributions, end-to-end tests with real NUT/SSH/Docker services, and configuration validation—ensuring no regressions reach users.

This page documents the automated test suite and the manual validation performed before each release.

---

## Testing Strategy

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
           ╱   pytest + Coverage (190)   ╲
          ╱      6 Python Versions        ╲
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
| **Unit Tests** | Every commit | Logic, state machine, edge cases (190 tests) |
| **Integration** | Every commit | Package install on 7 Linux distros |
| **E2E Tests** | Every commit | Full workflow with real NUT, SSH, Docker |
| **Real UPS** | Pre-release | Actual hardware, power events |

---

## Automated Testing

Every commit and pull request triggers automated testing via GitHub Actions.

### Validate Workflow

The **Validate** workflow runs on every push and pull request to `main`:

| Check | Description |
|-------|-------------|
| **Python Syntax** | Verifies the code compiles correctly |
| **Unit Tests** | Runs the full pytest test suite with coverage |
| **Configuration Validation** | Validates the default and example configs |

**Python Versions Tested:** 3.9, 3.10, 3.11, 3.12, 3.13, 3.14

### Integration Workflow

The **Integration** workflow performs OS-level testing to ensure packages install and work correctly on real distributions:

#### Package Installation Testing

Tests `.deb` and `.rpm` package installation:

| Distribution | Version | Package |
|--------------|---------|---------|
| Debian | 12 (Bookworm) | .deb |
| Debian | 13 (Trixie) | .deb |
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

#### Pip Installation Testing

Tests `pip install .` to ensure `pyproject.toml` is valid:

| Environment | Python Versions |
|-------------|-----------------|
| Ubuntu runner | 3.9, 3.10, 3.11, 3.12, 3.13, 3.14 |
| Debian 13 container | System Python |
| Ubuntu 26.04 container | System Python |
| RHEL 10 container | System Python |

---

## Test Coverage

The test suite covers:

- **Configuration parsing and validation** - All YAML options, defaults, and error handling
- **Trigger logic** - Battery level, runtime, depletion rate, time on battery, FSD
- **State machine** - Transitions between monitoring states
- **Notification formatting** - Message templates and Apprise integration
- **Shutdown sequence** - Command execution order and error handling
- **Edge cases** - Missing UPS data, connection failures, malformed input

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

## End-to-End (E2E) Testing

In addition to unit and integration tests, Eneru includes a comprehensive E2E test suite that validates the full monitoring and shutdown workflow using real services.

### E2E Test Environment

The E2E tests spin up a complete test environment with Docker Compose:

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

- **NUT Server** with dummy driver - Simulates UPS states without real hardware
- **SSH Target** container - Receives and logs shutdown commands
- **Target Containers** - Docker containers that Eneru can shut down
- **tmpfs Mount** - For testing filesystem unmount operations

### UPS Scenarios

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

### E2E Test Cases

The E2E workflow (`.github/workflows/e2e.yml`) runs 7 tests on every push and PR:

| Test | Description |
|------|-------------|
| **Test 1** | Validate E2E config against real NUT server |
| **Test 2** | Monitor normal state - verify no false shutdown triggers |
| **Test 3** | Detect power failure in dry-run mode |
| **Test 4** | SSH remote shutdown with real command execution |
| **Test 5** | FSD (Forced Shutdown) flag triggers immediate shutdown |
| **Test 6** | Voltage event detection (brownout, AVR) |
| **Test 7** | Notification delivery (if `E2E_NOTIFICATION_URL` secret configured) |

### Running E2E Tests Locally

You can run the E2E tests on your local machine:

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

# Verify NUT is working
upsc TestUPS@localhost:3493

# Run Eneru in dry-run mode
eneru --validate-config --config config-e2e-dry-run.yaml

# Simulate a power failure
cp scenarios/low-battery.dev scenarios/apply.dev
eneru --config config-e2e-dry-run.yaml --exit-after-shutdown

# Cleanup
docker compose down -v
```

See `tests/e2e/README.md` for more details.

---

## Pre-Release Validation

!!! important "Real Hardware Testing"
    Before each official release, Eneru is tested on real hardware with actual UPS units and simulated power events.

### Test Environment

The pre-release validation environment includes:

- Physical UPS units connected via USB and network
- NUT server configured and serving UPS data
- Multiple test systems (physical and virtual)
- Real power event simulation (unplugging UPS from mains)

### Validation Checklist

Before each release:

- [ ] **Power loss detection** - UPS status correctly transitions to `OB` (on battery)
- [ ] **Trigger thresholds** - Shutdown initiates at configured battery/runtime levels
- [ ] **Notification delivery** - Alerts are sent to configured services
- [ ] **Remote server shutdown** - SSH-based shutdown executes successfully
- [ ] **Container shutdown** - Docker/Podman containers stop gracefully
- [ ] **VM shutdown** - Libvirt VMs shut down before host
- [ ] **Local shutdown** - System powers off cleanly
- [ ] **Recovery** - Service resumes monitoring after power restoration

### Simulating Power Events

To test without actually losing power:

```bash
# On the NUT server, force the UPS into "on battery" mode (if supported)
upsrw -s ups.status=OB your-ups@localhost

# Or use a test UPS driver
upsdrvctl -t stop

# Monitor Eneru's response
journalctl -u eneru.service -f
```

!!! warning "Test Responsibly"
    When testing shutdown sequences, ensure you have console access to the system. Remote SSH sessions may be terminated during the shutdown process.

---

## Continuous Integration

### Workflow Files

| Workflow | File | Trigger |
|----------|------|---------|
| Validate | `.github/workflows/validate.yml` | Push, PR |
| Integration | `.github/workflows/integration.yml` | Push, PR |
| E2E | `.github/workflows/e2e.yml` | Push, PR |
| Release | `.github/workflows/release.yml` | Release published |
| PyPI | `.github/workflows/pypi.yml` | Release published |

### Viewing Results

- [Validate Workflow Runs](https://github.com/m4r1k/Eneru/actions/workflows/validate.yml)
- [Integration Workflow Runs](https://github.com/m4r1k/Eneru/actions/workflows/integration.yml)
- [E2E Workflow Runs](https://github.com/m4r1k/Eneru/actions/workflows/e2e.yml)
- [Release Workflow Runs](https://github.com/m4r1k/Eneru/actions/workflows/release.yml)
- [PyPI Workflow Runs](https://github.com/m4r1k/Eneru/actions/workflows/pypi.yml)

---

## Contributing Tests

When contributing new features or bug fixes:

1. **Add unit tests** for new functionality in the `tests/` directory
2. **Update example configs** if new configuration options are added
3. **Test locally** before submitting a pull request:

```bash
# Run the full test suite
pytest -v

# Validate your config changes
python -m eneru --validate-config --config config.yaml
# Or using the entry point:
eneru --validate-config --config config.yaml
```

See the [GitHub repository](https://github.com/m4r1k/Eneru) for contribution guidelines.
