# Eneru

Intelligent UPS monitoring daemon for NUT (Network UPS Tools). Orchestrates graceful shutdown of VMs, containers, remote servers, and local systems during power events.

## Primary Directives

These apply to every task in this repo, ahead of any section-specific guidance below.

1. **Be brief.**
    * if you're Claude Code, absolutely Be brief.
    * if you're Codex, don't be as brief as your defaults forces you; make sure code is commented, commit messages are clear, and enough context is included.
2. **Decide locally. Flag assumptions only when the choice is non-obvious or hard to reverse.**
3. **No speculative complexity. Build for the problem in front of you, not the one you imagine next.**
4. **Define success criteria. Loop until verified.**
5. **Touch only what you must. Clean up only your own mess.**
6. **Explain like I'm five (ELI5).** When you justify a non-obvious fix, debug a tricky bug, or write an AGENTS.md / changelog / commit-body section, lead with a concrete metaphor or analogy (kitchen, plumbing, traffic, whatever fits) before the jargon. The audience for these notes is future-you at 3 a.m. during an incident — not a reviewer looking for sophistication. Jargon comes second, after the picture is in their head.

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
  AGENTS.md                     # Module map + mixin pattern (agent context)
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

tools/
  dashboard-preview.py          # Serve working-tree web/ + proxy /api to a live
                                # daemon, screenshot every tab (Playwright). The
                                # standard way to visually verify dashboard
                                # changes; see the dashboard-preview skill.

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
  - 🛰️ Container loopback delegation (v5.5+: local host actions
    delegated to the host's sshd over 127.0.0.1; distinct from the
    🌐 used for ordinary remote_servers so the loopback path is
    visually unambiguous in logs and notifications)

  **Actions & Progress:**
  - ⏳ Starting a wait / initial wait state
  - 🕒 Still waiting / progress during wait
  - ➡️ Actions in progress (stopping compose, unmounting, pre-shutdown commands)
  - 🔍 Checking/searching (e.g., rootless containers)

  **Users:**
  - 👤 User-specific containers

## Conventions

- Commit messages: conventional commits (feat:, fix:, docs:, refactor:, test:, chore:)
- Codex commits must include this trailer in the commit message body: `Co-authored-by: Codex <noreply@openai.com>`
- Claude commits must include this trailer in the commit message body: `Co-authored-by: Claude <noreply@anthropic.com>` (model-agnostic — don't pin a specific version like `Opus 4.7`; new model releases shouldn't require a doc bump)
- Notifications via Apprise (100+ services supported)
- Config validation before any changes to config handling
- Always test with `--dry-run` before real shutdown logic changes
- When adding new config feature flags, add them to `examples/config-reference.yaml` AND to the relevant table in `docs/configuration.md` (key, default, one-line description). The two surfaces drift apart fast otherwise — pip users land on the rendered docs first; package users grep the example file first; both must agree
- When adding or removing tests, update `docs/testing.md` (per-file breakdown, E2E test case table). The pyramid summary intentionally says "thousands of tests" — no specific count to keep in sync.
- **New features require both synthetic AND end-to-end tests.** Any new feature must ship with (a) unit/integration tests in `tests/` covering the logic with maximum reasonable coverage, **and** (b) a corresponding step in `.github/workflows/e2e.yml` that exercises the feature end-to-end against the Docker Compose environment in `tests/e2e/`. Synthetic tests catch logic bugs; the E2E step proves the feature actually works against real NUT/SSH/Docker. PRs that add a feature without matching E2E coverage should be sent back for it.
- **Coverage bar: ≥95% per file.** Every file under `src/eneru/` must stay at or above 95% line+branch coverage. When adding a new module or extending an existing one, write tests for the new code path AND for the defensive branches (error logging, swallowed exceptions, edge cases) that the feature introduces. Verify with `pytest -m unit --cov=src/eneru --cov-report=term-missing` before pushing. Files that fall below 95% should be brought back up in the same PR; carrying the regression forward turns into a long tail of dead branches later.
- **OCI image changes:** update `Dockerfile`, `.dockerignore`, `docs/containers-kubernetes.md`, the Kubernetes samples under `deploy/kubernetes/`, and the OCI smoke checks in `.github/workflows/integration.yml` / `.github/workflows/release.yml`. The official image is one Python 3.12 image, non-root by default, published to GHCR by `release.yml`, and must work under both Docker and Podman. Remote-only configs must run without root; configs that enable local-host orchestration must fail at startup unless they run as root **or** provide an enabled host-loopback delegate (`is_host_loopback: true`) for SSH delegation to the host's `sshd`. Document SELinux bind mounts with `:Z`/`:z` on **eneru-owned** sources only (see the next rule) and do not advise disabling AppArmor for remote-only use.
- **SELinux `:Z` / `:z` on bind-mounts.** `:Z` tells Docker to **rewrite the SELinux label** of the host's source file to a container-private MCS pair. The new label is an xattr on the host filesystem and **persists across container delete, daemon restart, and host reboot**. For eneru-owned sources (`/srv/eneru/...`) that's correct — relabel makes eneru the sole authorized reader. For any host file other system services also read (`/etc/machine-id`, `/etc/localtime`, `/etc/resolv.conf`, anything under `/run`), `:Z` is a time bomb: already-running services keep their cached fd and don't notice, but the next host reboot dbus-broker / NetworkManager / logind try to open the file fresh, SELinux denies them, and the host comes up to a dead bus + no network (recoverable only from a physical console via `restorecon -Fv`). The default targeted policy already grants `container_t` read access to common system labels, so the relabel is unnecessary anyway. **Rule, no exceptions in docs/examples/CI:** plain `:ro` on shared host files; `:Z` only on eneru-owned paths.
- **Adding a new file under `src/eneru/`?** Also add a matching `contents:` entry in `nfpm.yaml`. The deb/rpm builds enumerate every module file explicitly — they do NOT glob. The pip path uses pyproject.toml autodiscovery, so a missing entry passes pip CI silently and only fails at deb/rpm install time with `ModuleNotFoundError`. Triple-checking via the isolated-interpreter package-layout simulation (see `src/eneru/AGENTS.md`) before push is the way to catch this.
- **Adding state to the SQLite stats DB?** Bump `SCHEMA_VERSION` in `src/eneru/stats.py` and add an idempotent `ALTER TABLE` migration in `_init_schema._migrate_schema` keyed off `meta.schema_version`. Migrations are append-only — never modify a previous version's block. Every `ALTER` is wrapped via `_safe_alter` so duplicate-column errors are benign. The version is bumped *after* the migration succeeds, so a crash mid-migration is replayed safely. See `src/eneru/AGENTS.md` "Stats schema evolution" for the full pattern + when-to-add-a-column guidance. New event types (rows in `events`) do NOT need a schema bump — only new columns or tables do.

## Working efficiently

This repo deliberately keeps individual files small (`monitor.py` is now ~830 lines after the v5.1 mixin decomposition; the largest test file is ~735 lines). To stay within the 200k context window during longer sessions:

- **Use Explore subagents for any "where is X" / "how does Y work" question.** A subagent search returns ~800 tokens vs. ~15-20k tokens for a direct `Read` of a 1,500-line file. Across a multi-turn session this is the single biggest lever — easily tens of thousands of tokens saved per session. Direct `Read` is right when you already know the file and need its current contents; reach for a subagent the moment the question is "where" / "what calls" / "how does this piece work".
- **Read `src/eneru/AGENTS.md`** for the per-module map and the mixin pattern before reading the implementations themselves; the map is ~80 lines vs. ~830 for `monitor.py`.
- **Don't add `.mcp.json` or context-injecting hooks.** They pre-load files into every session — exactly the wrong direction. On-demand loading is the whole point of the decomposition.

## Git Workflow

`main` is protected. All changes go through feature branches and pull requests.

**Branch protection on `main`:**
- Required CI checks before merge: `validate` (v6.1 reduced PR matrix — runs `3.9`, `3.12`, `3.15-dev` on PRs, full set on push-to-`main`; only `3.9` + `3.12` are required checks) + **8** parallel E2E matrix jobs (`E2E CLI`, `E2E UPS Single Core`, `E2E UPS Single Auth`, `E2E UPS Multi`, `E2E Redundancy Quorum`, `E2E Redundancy Regression`, `E2E Stats`, `E2E Loopback`).
- Strict mode: branch must be up-to-date with main before merge
- Enforce admins: maintainers follow the same rules
- No force pushes, no branch deletion
- 0 required reviewers (CI-gated, not review-gated)
- Feature branches auto-delete after merge

**Workflow:**
```
1. Pull latest main:   git checkout main && git pull --ff-only origin main
2. Create feature branch from the up-to-date main
3. Develop, commit, push the first logical chunk
4. Open the PR against main *as soon as you have something pushable* — CI
   does NOT fire on pushes to a feature branch until a PR exists. Mark it
   draft if it's WIP
5. Continue iterating: each subsequent push to the branch triggers one CI
   run on top of the cumulative diff. Push in logical chunks (one per
   slice / logical unit of work) — NOT one commit per push (CI flood,
   AI-reviewer quota burn) and NOT "20 commits → finally open PR" (single
   huge CI run, hard to bisect when something breaks)
6. CI checks must pass (all required: 8 E2E + validate 3.9/3.12) before merge
7. Merge via GitHub (branch auto-deletes)
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

## Code review workflow (manual AI invocation)

This repo uses **three layers of AI review**: a pre-push review via the `agent-skills:code-reviewer` subagent, plus two GitHub-side reviewers (`coderabbitai` and `cubic-dev-ai`) invoked manually after CI is green. All three are configured for **manual invocation** rather than reviewing every PR commit automatically.

**Why manual:**
- **CodeRabbit** free tier allows one review per 45 minutes. Per-commit auto-review burns the quota fast and produces noisy partial reviews against intermediate diffs.
- **cubic.dev** free tier allows 40 reviews per month. Same problem.
- We deliberately push commits early so the GitHub Actions E2E suite (`E2E CLI`, `E2E UPS Single Core`, `E2E UPS Single Auth`, `E2E UPS Multi`, `E2E Redundancy Quorum`, `E2E Redundancy Regression`, `E2E Stats`, `E2E Loopback`) gates work-in-progress and gives feedback fast on real-world scenarios. That CI feedback loop must stay cheap; AI review should not bottleneck it.

**Pre-push: spawn `agent-skills:code-reviewer` as a SUBAGENT**

Before pushing a substantive feature branch, you MUST spawn the `agent-skills:code-reviewer` skill via the `Agent` tool with `subagent_type: agent-skills:code-reviewer` and:
- if you're Claude Code, you must leverage the `model: opus`.
- if you're Codex, you must leverage the `model: gpt-5.5` with `reasoning_effort: high`.

Two reasons:

1. **Minimal context = efficient.** The reviewer runs in a fresh context; the main session's token budget stays unaffected.
2. **Minimal context = independent opinion.** The reviewer doesn't see what the main session decided, defended, or rationalized. That separation is the whole value — a same-session review is biased toward defending its own choices. Findings come back categorized P0/P1/P2/P3; triage and fix before pushing.

**Workflow on every non-trivial PR:**
```
1. Implement the change. Run pytest in the uv venv. Iterate.
2. PRE-PUSH: spawn agent-skills:code-reviewer subagent with the
   diff, the plan file, and any specific concerns. Triage findings.
3. Push commits. Let CI run on each push (E2E especially). Iterate on
   CI failures.
4. When all commits are in AND every required check is green, post:
       @coderabbitai full review
       @cubic-dev-ai review this pull request
   in PR comments (one per line, separate comments are fine).
5. Triage GitHub-side findings; push fixes; CI re-runs automatically.
6. Re-trigger AI review only if substantive new code was added.
7. Merge when both GitHub-side AI reviewers + branch protection are satisfied.
```

**Optional for trivial PRs.** Skip the AI reviews (all three) for:
- Documentation-only changes (README, docs/, comments)
- Version bumps + changelog promotion (release-prep PRs)
- Single-line typo fixes
- Pure dependency upgrades with no code changes

For trivial PRs the CI gates (validate 3.9/3.12 + E2E × 8) are sufficient. Saves quota for substantive changes.

**Configuration:**
- **CodeRabbit:** `.coderabbit.yaml` at repo root sets `reviews.auto_review.enabled: false`. The file also documents the manual-trigger phrase.
- **cubic.dev:** No config-file approach exists. Auto-review is disabled via the cubic.dev dashboard (Settings → Repository → disable "Auto-review on PR open"). Setting persists per-repository.

**If you forget and trigger an auto-review:** harmless but wastes quota. Just close the auto-review and trigger manually after CI is green.

## GitHub Actions SHA pin maintenance

Every third-party GitHub Actions invocation across the workflows (`validate.yml`, `integration.yml`, `e2e.yml`, `codeql.yml`, `pypi.yml`, `release.yml`, plus `.github/actions/e2e-setup/action.yml`) is pinned to a full commit SHA with the corresponding tag in a trailing comment. A moved upstream tag therefore cannot silently change what runs in CI — the pinned SHA is the single source of truth.

These pins drift over time as upstream actions ship security fixes, dependency bumps, and bug fixes under the same major-version tag. The repo doesn't auto-renew them; refresh **about every 3 months**, or when a security advisory lands for one of the pinned actions, or when an upstream major-version bump is needed.

**How to refresh:**

```bash
# For tag-tracked actions (most cases — actions/checkout@v6, etc.):
gh api repos/<owner>/<repo>/git/refs/tags/<tag> --jq '.object.sha'

# For branch-tracked actions (pypa/gh-action-pypi-publish@release/v1):
gh api repos/<owner>/<repo>/branches/<branch> --jq '.commit.sha'
```

Update both the SHA and the `# vX.Y` trailing comment in lockstep. After bumping, run the full CI matrix on a throwaway branch before merging — silent breakage is the failure mode the pins exist to prevent in the first place.

The current pinned set (as of 2026-06-29):

| Action | Tag | SHA prefix |
|---|---|---|
| `actions/checkout` | `v7.0.0` | `9c091bb2…` |
| `actions/setup-python` | `v6` | `ece7cb06…` |
| `actions/upload-artifact` | `v7` | `043fb46d…` |
| `actions/download-artifact` | `v8` | `3e5f45b2…` |
| `codecov/codecov-action` | `v6` | `fb8b3582…` |
| `github/codeql-action` | `v4` | `54f647b7…` |
| `pypa/gh-action-pypi-publish` | `release/v1` | `cef22109…` |
| `softprops/action-gh-release` | `v3` | `718ea10b…` |
| `docker/setup-buildx-action` | `v3` | `8d2750c6…` |
| `docker/build-push-action` | `v6` | `10e90e36…` |

`nFPM` is similarly pinned (`NFPM_VERSION` env var in `release.yml` and `integration.yml`) and verified against the goreleaser-published `checksums.txt` before extraction. Bump the version constant and the checksum check still verifies the new download.

## Changelog

A single changelog is maintained at `docs/changelog.md`. This is the
comprehensive version with detailed changes, migration notes, and release
entries. It is rendered on
[ReadTheDocs](https://eneru.readthedocs.io/latest/changelog/).

### Changelog workflow: verbose during dev, trim before release

The `[Unreleased]` section is a **working surface, not a published artifact**. During development add detail freely — per-rc breakouts (`rc6 — added X`, `rc7 — fixed Y`), file references, code snippets, design rationale. The reasoning: future sessions pick up context cheaper from a single long file than by reading individual commits with `git log -p` (one paragraph in a markdown file ≪ one full diff). Trade a fat `[Unreleased]` block for fewer tokens spent reconstructing what changed.

**Before tagging the release**, consolidate the verbose `[Unreleased]` block into a published-quality entry that matches the size and density of prior shipped releases. Reference points: v5.0.0 was ~600 words; v5.1.0 was trimmed from ~2950 words (rc1 through rc9 accumulated) down to ~1100 words (the published entry). Reader-friendly always wins at release time.

The trim pass is part of the release-cut commit, not a separate step. Specifically:

1. Drop `rcN —` prefixes — the user sees one consolidated 5.X.0 release, not the rc history
2. Collapse paragraph bullets into bullet + sub-bullets (the v5.0.0 style)
3. Move design rationale into `docs/<feature>.md` if it isn't already there; the changelog should *summarise*, not explain
4. Cut walkthrough text and behavioural deep-dives — link to `docs/` instead
5. Run the `humanizer` skill on the trimmed entry for one final pass to remove AI-isms (em-dashes-as-style, promotional adjectives, copula avoidance, signposting). Don't mention the humanizer pass in the commit message.

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
