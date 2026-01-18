# Eneru

Intelligent UPS monitoring daemon for NUT (Network UPS Tools). Orchestrates graceful shutdown of VMs, containers, remote servers, and local systems during power events.

## Development Setup

Use `uv` for fast virtual environment management. Always work in a virtualenv.

```bash
# Create and activate virtualenv (disposable tmp folder)
uv venv /tmp/eneru-venv
source /tmp/eneru-venv/bin/activate

# Install package with all dev dependencies
uv pip install -e ".[dev,notifications,docs]"
```

## Commands

```bash
# Testing (always inside virtualenv)
pytest                              # Run all tests
pytest -m unit                      # Unit tests only
pytest -m integration               # Integration tests only
pytest --cov=src/eneru              # With coverage

# Development
python -m eneru --validate-config --config config.yaml
python -m eneru --dry-run --config config.yaml

# Documentation
mkdocs serve                        # Local docs preview
```

## Project Structure

```
src/eneru/                      # Main package
  __init__.py                   # Public API exports
  __main__.py                   # CLI entry point (python -m eneru)
  version.py                    # Version string (single source of truth)
  config.py                     # Configuration dataclasses + ConfigLoader
  state.py                      # MonitorState dataclass
  logger.py                     # TimezoneFormatter + UPSLogger
  notifications.py              # NotificationWorker (Apprise integration)
  utils.py                      # Helper functions (run_command, etc.)
  actions.py                    # REMOTE_ACTIONS templates
  monitor.py                    # UPSMonitor class (core daemon)
  cli.py                        # CLI argument parsing + main()

tests/                          # pytest tests
  conftest.py                   # Shared fixtures
  test_*.py                     # Unit/integration tests
  e2e/                          # End-to-end tests
    docker-compose.yml          # E2E test environment
    config-e2e*.yaml            # E2E test configs
    nut-dummy/Dockerfile        # NUT server simulator
    ssh-target/Dockerfile       # SSH target container

docs/                           # MkDocs documentation (ReadTheDocs)
  index.md                      # Homepage
  getting-started.md            # Installation guide
  configuration.md              # Config reference
  triggers.md                   # Shutdown triggers
  notifications.md              # Apprise setup
  remote-servers.md             # SSH configuration
  testing.md                    # CI/CD strategy
  troubleshooting.md            # Debug guide
  changelog.md                  # Lean changelog (RTD)

.github/
  workflows/
    validate.yml                # Lint + unit tests
    integration.yml             # Package install tests
    e2e.yml                     # End-to-end tests
    release.yml                 # Build .deb/.rpm packages
    pypi.yml                    # Publish to PyPI
  ISSUE_TEMPLATE/               # Bug/feature templates
  PULL_REQUEST_TEMPLATE.md      # PR template

config.yaml                     # Example configuration
examples/                       # Additional example configs
  config-minimal.yaml
  config-homelab.yaml
  config-enterprise.yaml

pyproject.toml                  # PEP 517/518 packaging
pytest.ini                      # pytest configuration
mkdocs.yml                      # MkDocs configuration
nfpm.yaml                       # .deb/.rpm package config
.readthedocs.yaml               # RTD build config
requirements.txt                # Runtime dependencies
requirements-dev.txt            # Dev dependencies
CHANGELOG.md                    # Full changelog with version comparisons
CONTRIBUTING.md                 # Contribution guidelines
README.md                       # Project overview
```

## Code Style

- Python 3.9+ with type hints
- PEP 8 compliant
- Docstrings for public functions/classes
- Tests in `tests/` following `test_*.py` pattern
- **Emojis in logs/notifications**: The codebase uses emojis for visual clarity in log messages and notifications:
  - ⚡ Power events, UPS info
  - 🔋 Battery status
  - ⚠️ Warnings
  - ❌ Errors/failures
  - ✅ Success/completion
  - 📢 Notifications
  - 🖥️ VMs
  - 🐳 Containers
  - 🌐 Remote servers
  - 🔌 Shutdown/power
  - 🛑 Service stop

## Conventions

- Commit messages: conventional commits (feat:, fix:, docs:, refactor:, test:, chore:)
- Notifications via Apprise (100+ services supported)
- Config validation before any changes to config handling
- Always test with `--dry-run` before real shutdown logic changes

## Changelog Format

Two changelog files are maintained:

1. **`docs/changelog.md`** - Lean version for ReadTheDocs
   - Brief summaries per version
   - Links to full changelog on GitHub

2. **`CHANGELOG.md`** - Comprehensive version
   - Detailed changes with migration notes
   - Version comparison tables (e.g., "v4.9 vs v4.8") showing feature differences

When releasing a new version, update both files. The comparison table format:
```markdown
### vX.Y vs vX.Z

| Feature | vX.Z | vX.Y |
|---------|------|------|
| Feature Name | Old behavior | New behavior |
```

## Key Dependencies

- PyYAML: Configuration parsing
- Apprise (optional): Notifications
- pytest: Testing framework
