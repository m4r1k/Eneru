# Eneru

Intelligent UPS monitoring daemon for NUT (Network UPS Tools). Orchestrates graceful shutdown of VMs, containers, remote servers, and local systems during power events.

## Development Setup

**CRITICAL: NEVER run `pip`, `pip3`, `python -m pip`, `python`, `pytest`, or any other dev/Python tooling directly against the system Python. ALL Python work — install, uninstall, run, test, version-check — MUST happen inside a `uv` virtualenv. No exceptions.**

This rule applies to *every* operation, including:

- Installing packages (`pip install ...`)
- **Uninstalling packages (`pip uninstall ...`) — even to "clean up" or fix broken state.** A system-wide `pip uninstall eneru` will rip out files claimed by both pip and the deb/rpm package (e.g. `/usr/local/bin/eneru`), breaking the package install. If the system has stale pip-installed Python packages owned by Eneru, the only correct cleanup is to reinstall the deb/rpm to restore the package's files, then leave the pip remnants alone, *or* delete only the pip-owned site-packages directory by hand after confirming nothing else needs it. Never invoke pip itself.
- Running the test suite (`pytest`)
- Running ad-hoc scripts (`python -c '...'`)
- Editable dev installs (`pip install -e .` — use `uv pip install -e .` inside the venv only)

If you need to verify the installed deb/rpm package, invoke the package's own entry point (e.g. `/usr/local/bin/eneru version`, `python3 /opt/ups-monitor/eneru.py version`) — these read from `/opt/ups-monitor/`, not from system Python paths, so no venv is required.

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
python -m eneru validate --config examples/config-reference.yaml
python -m eneru run --dry-run --config examples/config-reference.yaml

# Documentation
mkdocs serve                        # Local docs preview
```

## Project Structure

```
src/eneru/                      # Main package
  CLAUDE.md                     # Module map + mixin pattern (Claude session context)
  __init__.py                   # Public API exports
  __main__.py                   # CLI entry point (python -m eneru)
  version.py                    # Version string (single source of truth)
  config.py                     # Configuration dataclasses + ConfigLoader
  state.py                      # MonitorState dataclass
  logger.py                     # TimezoneFormatter + UPSLogger
  notifications.py              # NotificationWorker (Apprise integration)
  utils.py                      # Helper functions (run_command, etc.)
  actions.py                    # REMOTE_ACTIONS templates
  monitor.py                    # UPSGroupMonitor core: init, polling, orchestration, main loop
  multi_ups.py                  # MultiUPSCoordinator (thread-per-group)
  cli.py                        # CLI argument parsing + main()
  shutdown/                     # Per-phase shutdown mixins
    vms.py                      # VMShutdownMixin (libvirt)
    containers.py               # ContainerShutdownMixin (docker/podman + compose)
    filesystems.py              # FilesystemShutdownMixin (sync + unmount)
    remote.py                   # RemoteShutdownMixin (SSH-based remote servers)
  health/                       # Health-monitoring mixins
    voltage.py                  # VoltageMonitorMixin (thresholds, AVR, bypass, overload)
    battery.py                  # BatteryMonitorMixin (depletion rate, anomaly detection)

tests/                          # pytest tests
  conftest.py                   # Shared fixtures
  test_constants.py             # Shared test constants (sample webhook URLs, etc.)
  test_config_loading.py        # Config: defaults + YAML file parse
  test_config_notifications.py  # Config: legacy Discord, avatar handling
  test_config_filesystems.py    # Config: mount path parsing
  test_config_vm_containers.py  # Config: compose files, container runtime
  test_config_remote.py         # Config: remote servers, ordering, safety margin
  test_config_validation.py     # Config: cross-field validation, edge cases
  test_*.py                     # Unit/integration tests for non-config modules
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
  changelog.md                  # Changelog (comprehensive, single source of truth)

.github/
  workflows/
    validate.yml                # Lint + unit tests
    integration.yml             # Package install tests
    e2e.yml                     # End-to-end tests
    release.yml                 # Build .deb/.rpm packages
    pypi.yml                    # Publish to PyPI
  ISSUE_TEMPLATE/               # Bug/feature templates
  PULL_REQUEST_TEMPLATE.md      # PR template

examples/                       # Example configs
  config-reference.yaml         # Comprehensive reference (every feature flag)
  config-minimal.yaml           # Minimal single-UPS setup
  config-homelab.yaml           # Homelab: VMs, containers, NAS
  config-enterprise.yaml        # Multi-server enterprise setup
  config-dual-ups.yaml          # Multi-UPS setup

packaging/
  eneru-wrapper.py              # Package entry point wrapper
  eneru.service                 # Systemd service file
  scripts/                      # Package lifecycle scripts

pyproject.toml                  # PEP 517/518 packaging
pytest.ini                      # pytest configuration
mkdocs.yml                      # MkDocs configuration
nfpm.yaml                       # .deb/.rpm package config
.readthedocs.yaml               # RTD build config
requirements.txt                # Runtime dependencies
requirements-dev.txt            # Dev dependencies
CONTRIBUTING.md                 # Contribution guidelines
README.md                       # Project overview
```

## Code Style

- Python 3.9+ with type hints
- PEP 8 compliant
- Docstrings for public functions/classes
- Tests in `tests/` following `test_*.py` pattern
- **Emojis in logs/notifications**: The codebase uses emojis for visual clarity in log messages and notifications. Each emoji has a specific semantic meaning - use them consistently:

  **System State:**
  - 🚀 Service startup
  - 🛑 Service stop, exiting

  **Modes:**
  - 🧪 Dry-run mode indicators

  **Configuration & Info:**
  - 📢 Notification status
  - 📋 Feature lists, pre-shutdown command lists
  - 📊 Voltage monitoring statistics
  - ℹ️ Informational messages (indented)

  **Status Messages:**
  - ⚠️ Warnings
  - ❌ Errors, failures
  - ✅ Success, completion
  - 🚨 Critical alerts, emergency shutdown

  **Power & UPS:**
  - ⚡ Power events, AVR activity, force actions (e.g., force destroy VM)
  - 🔋 Battery status (periodic updates)
  - 🔄 UPS status changes

  **Shutdown Components:**
  - 🖥️ Virtual machines (section header)
  - ⏹️ Stopping individual VM
  - 🐳 Containers - Docker/Podman (section header)
  - 🌐 Remote servers (section header and per-server)
  - 💾 Filesystem sync
  - 📤 Unmounting filesystems (section header)
  - 🔌 Shutdown commands (local and remote)

  **Actions & Progress:**
  - ⏳ Starting a wait / initial wait state
  - 🕒 Still waiting / progress during wait
  - ➡️ Actions in progress (stopping compose, unmounting, pre-shutdown commands)
  - 🔍 Checking/searching (e.g., rootless containers)

  **Users:**
  - 👤 User-specific containers

## Conventions

- Commit messages: conventional commits (feat:, fix:, docs:, refactor:, test:, chore:)
- Notifications via Apprise (100+ services supported)
- Config validation before any changes to config handling
- Always test with `--dry-run` before real shutdown logic changes
- When adding new config feature flags, add them to `examples/config-reference.yaml`
- When adding or removing tests, update `docs/testing.md` (test counts in pyramid/table, per-file breakdown, E2E test case table)
- **New features require both synthetic AND end-to-end tests.** Any new feature must ship with (a) unit/integration tests in `tests/` covering the logic with maximum reasonable coverage, **and** (b) a corresponding step in `.github/workflows/e2e.yml` that exercises the feature end-to-end against the Docker Compose environment in `tests/e2e/`. Synthetic tests catch logic bugs; the E2E step proves the feature actually works against real NUT/SSH/Docker. PRs that add a feature without matching E2E coverage should be sent back for it.
- **Adding a new file under `src/eneru/`?** Also add a matching `contents:` entry in `nfpm.yaml`. The deb/rpm builds enumerate every module file explicitly — they do NOT glob. The pip path uses pyproject.toml autodiscovery, so a missing entry passes pip CI silently and only fails at deb/rpm install time with `ModuleNotFoundError`. Triple-checking via the isolated-interpreter package-layout simulation (see `src/eneru/CLAUDE.md`) before push is the way to catch this.
- **Adding state to the SQLite stats DB?** Bump `SCHEMA_VERSION` in `src/eneru/stats.py` and add an idempotent `ALTER TABLE` migration in `_init_schema._migrate_schema` keyed off `meta.schema_version`. Migrations are append-only — never modify a previous version's block. Every `ALTER` is wrapped via `_safe_alter` so duplicate-column errors are benign. The version is bumped *after* the migration succeeds, so a crash mid-migration is replayed safely. See `src/eneru/CLAUDE.md` "Stats schema evolution" for the full pattern + when-to-add-a-column guidance. New event types (rows in `events`) do NOT need a schema bump — only new columns or tables do.

## Working efficiently in Claude Code

This repo deliberately keeps individual files small (`monitor.py` is now ~830 lines after the v5.1 mixin decomposition; the largest test file is ~735 lines). To stay within the 200k context window during longer sessions:

- **Use Explore subagents for any "where is X" / "how does Y work" question.** A subagent search returns ~800 tokens vs. ~15-20k tokens for a direct `Read` of a 1,500-line file. Across a multi-turn session this is the single biggest lever — easily tens of thousands of tokens saved per session. Direct `Read` is right when you already know the file and need its current contents; reach for a subagent the moment the question is "where" / "what calls" / "how does this piece work".
- **Read `src/eneru/CLAUDE.md`** for the per-module map and the mixin pattern before reading the implementations themselves; the map is ~80 lines vs. ~830 for `monitor.py`.
- **Don't add `.mcp.json` or context-injecting hooks.** They pre-load files into every session — exactly the wrong direction. On-demand loading is the whole point of the decomposition.

## Git Workflow

`main` is protected. All changes go through feature branches and pull requests.

**Branch protection on `main`:**
- Required CI checks before merge: `validate` (Python 3.9-3.14) + `e2e-test` (7 checks total)
- Strict mode: branch must be up-to-date with main before merge
- Enforce admins: maintainers follow the same rules
- No force pushes, no branch deletion
- 0 required reviewers (CI-gated, not review-gated)
- Feature branches auto-delete after merge

**Workflow:**
```
1. Pull latest main:   git checkout main && git pull --ff-only origin main
2. Create feature branch from the up-to-date main
3. Develop, commit, push
4. Open PR against main
5. CI checks must pass (all 7)
6. Merge via GitHub (branch auto-deletes)
```

**Always pull `main` before creating a feature branch.** Branching from a stale local `main` forces a rebase later and risks landing PRs against an obsolete base.

**Releasing a new version:**
```
1. Merge all feature work into main via PRs
2. Update docs/changelog.md and version.py on main
3. Tag the latest commit on main: git tag v5.0.0
4. Push the tag: git push origin v5.0.0
5. Create GitHub Release from the tag
   (triggers release.yml for .deb/.rpm and pypi.yml for PyPI)
```

Tags are the immutable release snapshots. No release branches -- tags are sufficient for a single active version. GitHub Releases, .deb/.rpm packages, and PyPI artifacts are all built from tags via CI.

## Changelog

A single changelog is maintained at `docs/changelog.md`. This is the comprehensive version with detailed changes, migration notes, and version comparison tables. It is rendered on [ReadTheDocs](https://eneru.readthedocs.io/latest/changelog/).

When releasing a new version, update `docs/changelog.md` with the comparison table format:
```markdown
### vX.Y vs vX.Z

| Feature | vX.Z | vX.Y |
|---------|------|------|
| Feature Name | Old behavior | New behavior |
```

## Installation Paths

Eneru has two installation methods with different invocation paths:

### Package Installation (deb/rpm)

Installs to `/opt/ups-monitor/`:
```
/opt/ups-monitor/
  eneru.py              # Wrapper script (packaging/eneru-wrapper.py)
  eneru/                # Package modules
    __init__.py
    cli.py
    monitor.py
    ...
```

**Invocation:** `sudo python3 /opt/ups-monitor/eneru.py [options]`

The wrapper script (`eneru.py`) adds `/opt/ups-monitor` to `sys.path` and calls `eneru.cli.main()`.

### Pip Installation

Installs as a Python package with entry points defined in `pyproject.toml`.

**Invocation:** `eneru [options]` or `python -m eneru [options]`

### Documentation Guidelines

When writing documentation, use the correct invocation style for the context:

| Context | Command Style | Example |
|---------|---------------|---------|
| Package users (README, troubleshooting) | `/opt/ups-monitor/eneru.py` | `sudo python3 /opt/ups-monitor/eneru.py --validate-config` |
| Developers (CONTRIBUTING, testing) | `python -m eneru` or `eneru` | `python -m eneru run --dry-run --config examples/config-reference.yaml` |
| PyPI users | `eneru` | `eneru --validate-config` |

## Key Dependencies

- PyYAML: Configuration parsing
- Apprise (optional): Notifications
- pytest: Testing framework
