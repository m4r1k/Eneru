# Testing

Every commit runs unit tests, integration tests across 7 Linux distributions, end-to-end tests with real NUT/SSH/Docker services, and configuration validation.

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
           ╱   pytest + Coverage (190)   ╲
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
| **Unit Tests** | Every commit | Logic, state machine, edge cases (190 tests) |
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

#### Pip installation testing

Tests `pip install .` to ensure `pyproject.toml` is valid:

| Environment | Python Versions |
|-------------|-----------------|
| Ubuntu runner | 3.9, 3.10, 3.11, 3.12, 3.13, 3.14, 3.15-dev |
| Debian 13 container | System Python |
| Ubuntu 26.04 container | System Python |
| RHEL 10 container | System Python |

---

## Test coverage

The test suite covers:

- Configuration parsing and validation: all YAML options, defaults, and error handling
- Trigger logic: battery level, runtime, depletion rate, time on battery, FSD
- State machine transitions between monitoring states
- Notification formatting, message templates, and Apprise integration
- Shutdown sequence: command execution order and error handling
- Edge cases: missing UPS data, connection failures, malformed input

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
python -m eneru --validate-config --config config.yaml
# Or using the entry point:
eneru --validate-config --config config.yaml
```

See the [GitHub repository](https://github.com/m4r1k/Eneru) for contribution guidelines.
