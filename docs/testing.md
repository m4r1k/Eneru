# Testing

Eneru uses a comprehensive testing strategy to ensure reliability across different environments. **Every commit triggers the full test suite**—unit tests, integration tests across 7 Linux distributions, and configuration validation—ensuring no regressions reach users.

This page documents the automated test suite and the manual validation performed before each release.

---

## Testing Strategy

```
                          ▲
                         ╱ ╲
                        ╱   ╲
                       ╱Real ╲
                      ╱  UPS  ╲
                     ╱  Power  ╲
                    ╱  Events   ╲
                   ╱─────────────╲
                  ╱  Integration  ╲
                 ╱  Package & Pip  ╲
                ╱   Linux Distros   ╲
               ╱─────────────────────╲
              ╱      Unit Tests       ╲
             ╱   pytest + Coverage     ╲
            ╱     Python Versions       ╲
           ╱─────────────────────────────╲
          ╱       Static Analysis         ╲
         ╱    Syntax Check + Validation    ╲
        ╱───────────────────────────────────╲
       ╱          AI-Assisted Dev            ╲
      ╱      Claude Code Review & QA          ╲
     ╱─────────────────────────────────────────╲
```

| Layer | Frequency | What It Tests |
|-------|-----------|---------------|
| **AI-Assisted Dev** | Continuous | Code review, implementation guidance |
| **Static Analysis** | Every commit | Python syntax, config validation |
| **Unit Tests** | Every commit | Logic, state machine, edge cases |
| **Integration** | Every commit | Package install on real distros |
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
| Release | `.github/workflows/release.yml` | Release published |

### Viewing Results

- [Validate Workflow Runs](https://github.com/m4r1k/Eneru/actions/workflows/validate.yml)
- [Integration Workflow Runs](https://github.com/m4r1k/Eneru/actions/workflows/integration.yml)
- [Release Workflow Runs](https://github.com/m4r1k/Eneru/actions/workflows/release.yml)

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
python src/eneru/monitor.py --validate-config --config config.yaml
```

See the [GitHub repository](https://github.com/m4r1k/Eneru) for contribution guidelines.
