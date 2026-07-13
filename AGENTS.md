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

This rule applies to *every* operation, including **uninstalls**: a system-wide `pip uninstall eneru` rips out files claimed by both pip and the deb/rpm package (e.g. `/usr/local/bin/eneru`), breaking the package install. If a system has stale pip-installed Eneru packages, the only correct cleanup is to reinstall the deb/rpm to restore its files and leave the pip remnants alone, *or* hand-delete only the pip-owned site-packages directory. Never invoke pip against system Python.

To verify an installed deb/rpm package, invoke the package's own entry point (`/usr/local/bin/eneru version`, `python3 /opt/ups-monitor/eneru.py version`) — these read from `/opt/ups-monitor/`, no venv required.

```bash
# Create and activate virtualenv (disposable tmp folder)
uv venv /tmp/eneru-venv
source /tmp/eneru-venv/bin/activate

# Install package with all dev dependencies (same extras CI installs, plus docs)
uv pip install -e ".[dev,notifications,auth,mqtt,docs]"
```

## Commands

```bash
# Testing (always inside virtualenv)
pytest                              # Run all tests
pytest -m unit                      # Unit tests only (integration coverage is the E2E suite, tests/e2e/)
pytest --cov=src/eneru              # With coverage

# Development
python -m eneru validate --config examples/config-reference.yaml
python -m eneru run --dry-run --config examples/config-reference.yaml

# Documentation
mkdocs serve                        # Local docs preview
```

## Project Structure

```text
src/eneru/            # Main package — see src/eneru/AGENTS.md for the per-module
                      # map, mixin pattern, loopback ordering, stats schema, scheduler
  shutdown/           # Per-phase shutdown mixins (vms, containers, filesystems, remote)
  health/             # Health-monitoring mixins (voltage, battery)
  web/                # Browser dashboard static assets (app.js, style.css, index.html)
tests/                # pytest unit/integration tests (test_*.py; shared fixtures in conftest.py)
tests/e2e/            # End-to-end suite: docker-compose env, NUT simulator, SSH targets
docs/                 # MkDocs documentation (ReadTheDocs); changelog.md is the single changelog
examples/             # Example configs; config-reference.yaml covers every feature flag
deploy/kubernetes/    # Kubernetes manifests
packaging/            # Package entry-point wrapper, systemd unit, lifecycle scripts
tools/                # dashboard-preview.py — visual verification of web/ changes
                      # (see the dashboard-preview skill)
.claude/skills/       # Repo skills: dashboard-preview, release-review
.github/workflows/    # validate, integration, e2e, codeql, release, pypi
nfpm.yaml             # .deb/.rpm package config (enumerates every module file)
pyproject.toml        # PEP 517/518 packaging
```

## Code Style

- Python 3.9+ with type hints; PEP 8; docstrings for public functions/classes
- Tests in `tests/` following `test_*.py`
- **Emojis in logs/notifications carry semantic meaning** (⚡ power events, 🔋 battery, 🌐 remotes, 🛰️ loopback delegation, …). They're scanner hints during incident review, not decoration. The full legend lives in `CONTRIBUTING.md` ("Log message emoji conventions") — match existing usage when adding log lines.

## Conventions

- Commit messages: conventional commits (feat:, fix:, docs:, refactor:, test:, chore:)
- Codex commits must include this trailer in the commit message body: `Co-authored-by: Codex <noreply@openai.com>`
- Claude commits must include this trailer in the commit message body: `Co-authored-by: Claude <noreply@anthropic.com>` (model-agnostic — don't pin a specific version; new model releases shouldn't require a doc bump)
- Notifications via Apprise (100+ services supported)
- Config validation before any changes to config handling
- Always test with `--dry-run` before real shutdown logic changes
- **New config feature flags** go in `examples/config-reference.yaml` AND the relevant table in `docs/configuration.md` (key, default, one-line description). The two surfaces drift apart fast otherwise — both must agree.
- **Adding or removing tests?** Update `docs/testing.md` (per-file breakdown, E2E test case table). The pyramid summary intentionally says "thousands of tests" — no specific count to keep in sync.
- **New features require both synthetic AND end-to-end tests:** unit/integration tests in `tests/` covering the logic **and** a step in `.github/workflows/e2e.yml` exercising the feature against the Docker Compose environment in `tests/e2e/`. Synthetic tests catch logic bugs; E2E proves it works against real NUT/SSH/Docker. This applies to drain-path *behavior changes* too, not just features. PRs missing E2E coverage get sent back.
- **Coverage bar: ≥95% per file** (line+branch) for every file under `src/eneru/` — including defensive branches (error logging, swallowed exceptions, edge cases). Verify with `pytest -m unit --cov=src/eneru --cov-report=term-missing` before pushing; bring regressions back up in the same PR.
- **OCI image changes:** update `Dockerfile`, `.dockerignore`, `docs/containers-kubernetes.md`, the Kubernetes samples under `deploy/kubernetes/`, and the OCI smoke checks in `integration.yml`/`release.yml`. One Python 3.12 image, non-root by default, published to GHCR, must work under Docker and Podman. Remote-only configs run without root; local-host orchestration requires root **or** an enabled host-loopback delegate (`is_host_loopback: true`).
- **SELinux bind-mount rule, no exceptions in docs/examples/CI:** `:Z`/`:z` only on **eneru-owned** sources (`/srv/eneru/...`); plain `:ro` on shared host files (`/etc/machine-id`, `/etc/localtime`, anything other services read). `:Z` persistently relabels the host file and can leave the host with a dead bus + no network after reboot. Full rationale: `docs/containers-kubernetes.md` ("Podman and SELinux").
- **Adding a new file under `src/eneru/`?** Add a matching `contents:` entry in `nfpm.yaml`. The deb/rpm builds enumerate every module file explicitly — they do NOT glob; a missing entry passes pip CI silently and fails only at deb/rpm install time (`ModuleNotFoundError`).
- **Adding state to the SQLite stats DB?** Bump `SCHEMA_VERSION` in `src/eneru/stats.py` and add an idempotent, append-only migration — full pattern and when-to-add-a-column guidance in `src/eneru/AGENTS.md` ("Stats schema evolution"). New event types do NOT need a schema bump — only new columns or tables do.

## Working efficiently

This repo deliberately keeps individual source files on the smaller side (the v5.1 mixin decomposition). To stay within the context window during longer sessions:

- **Use Explore subagents for any "where is X" / "how does Y work" question.** A subagent search returns ~800 tokens vs. ~15-20k for a direct `Read` of a large file — the single biggest context lever. Direct `Read` is right when you already know the file and need its current contents.
- **Read `src/eneru/AGENTS.md`** for the per-module map before reading implementations; the map is far cheaper than the `monitor.py` it summarizes.
- **Don't add `.mcp.json` or context-injecting hooks.** They pre-load files into every session — exactly the wrong direction. On-demand loading is the whole point.
- **On long multi-finding tasks, track every item with `/goals`** (or the task tools) and re-check the list before declaring done — "61 of 62 done" reads as done in a long session unless the tracker says otherwise.

## Git Workflow

`main` is protected. All changes go through feature branches and pull requests.

**Branch protection on `main`:** required checks = `validate` (reduced PR matrix `3.9`, `3.12`, `3.15-dev`; only `3.9` + `3.12` required) + `test-oci-image` (aggregates parallel native AMD64 and ARM64 builds, config/native-dependency/metadata checks, and the Podman smoke) + **8** parallel E2E matrix jobs (`E2E CLI`, `E2E UPS Single Core`, `E2E UPS Single Auth`, `E2E UPS Multi`, `E2E Redundancy Quorum`, `E2E Redundancy Regression`, `E2E Stats`, `E2E Loopback`). Strict mode (branch up-to-date with main), enforce admins, no force pushes, 0 required reviewers (CI-gated), branches auto-delete after merge.

**Workflow:**
```text
1. Pull latest main:   git checkout main && git pull --ff-only origin main
   (branching from stale main forces a rebase later)
2. Create feature branch from the up-to-date main
3. Develop, commit, push the first logical chunk
4. Open the PR as soon as you have something pushable — CI does NOT fire on
   feature-branch pushes until a PR exists. Mark draft if WIP
5. Iterate in logical chunks (one push per slice) — NOT one commit per push
   (CI flood, AI-reviewer quota burn) and NOT "20 commits → finally open PR"
6. All required checks green before merge
7. Merge via GitHub (branch auto-deletes)
```

**Releasing a new version:**
```text
1. For X.Y.0 / X.0.0 ONLY — run the release-review skill first (see below)
2. Merge all feature work into main via PRs
3. Update docs/changelog.md and version.py on main
4. Tag the latest commit on main: git tag vX.Y.Z && git push origin vX.Y.Z
5. Create the GitHub Release from the tag (triggers release.yml + pypi.yml)
   — point releases use --latest=false; see docs/maintenance.md
     ("Release mechanics") for the Latest-pinning and blockquote pattern
```

Tags are the immutable release snapshots; no release branches. Point releases (`X.Y.Z`) are for **bug fixes only** — feature work waits for the next minor.

**Pre-release deep review (MANDATORY for minor/major releases).** Before tagging any `X.Y.0` or `X.0.0`, run the `release-review` skill (`.claude/skills/release-review/SKILL.md`) end-to-end: parallel multi-agent audit of the whole repository → stable-ID findings report → user triage → remediation PR → post-implementation verification round with fresh lenses. Patch releases are exempt — they inherit the minor's review. The v6.0.0/v6.1.6/v6.1.7 cycles each surfaced 60–100 real findings; the skill codifies that process so it survives context loss and model changes.

## Code review workflow (manual AI invocation)

Three layers of AI review, all **manually invoked** (free-tier quotas: CodeRabbit one review/45 min, cubic.dev 40/month; per-commit auto-review burns quota on noisy intermediate diffs while the E2E suite already gates every push):

1. **Pre-push:** spawn the `agent-skills:code-reviewer` skill as a SUBAGENT via the `Agent` tool (fresh context = independent opinion — a same-session review defends its own choices; findings come back P0-P3, triage before pushing). Record the reviewer's exact model identifier and reasoning level in the review audit trail when the runtime exposes them; never guess missing metadata.
2. **After CI is green** (never before), post in PR comments:
   `@coderabbitai full review` and `@cubic-dev-ai review this pull request`
3. Triage findings, push fixes (CI re-runs), re-trigger AI review only if substantive new code was added. Merge when both GitHub-side reviewers + branch protection are satisfied.

**Skip all three for trivial PRs** (docs-only, version bumps + changelog promotion, typo fixes, pure dependency upgrades) — the CI gates are sufficient; save the quota.

**Configuration:** CodeRabbit auto-review is off via `.coderabbit.yaml`; cubic.dev auto-review is off in its dashboard (per-repo setting). Accidentally triggered auto-reviews are harmless — close and re-trigger manually after CI is green.

## Changelog

One changelog: `docs/changelog.md` (rendered on [ReadTheDocs](https://eneru.readthedocs.io/latest/changelog/)).

**Verbose during dev, trim before release.** The `[Unreleased]` section is a working surface — add per-rc breakouts, file references, and rationale freely; future sessions pick up context cheaper from one long file than from `git log -p`. **The release-cut commit trims it** to a published-quality entry matching prior releases (reference: v5.1.0 went from ~2950 accumulated words to ~1100 published): drop `rcN —` prefixes, collapse to bullet + sub-bullets, move design rationale to `docs/<feature>.md`, cut walkthroughs. Finally run the `humanizer` skill on the trimmed entry to remove AI-isms; don't mention that pass in the commit message.

## Installation paths (for docs writing)

Two install methods with different invocation styles — package (deb/rpm → `sudo python3 /opt/ups-monitor/eneru.py …`) and pip (`eneru …` / `python -m eneru …`). Use the style matching the doc's audience; full layout and the context table are in `docs/maintenance.md` ("Installation paths").

## Maintenance reference

GitHub Actions tag maintenance, the nFPM pin, the deliberate Docker-base-image freshness policy (ISS-045), and GitHub-release mechanics live in `docs/maintenance.md`. Read it before touching workflow refs or cutting a release.

## Key Dependencies

- PyYAML: Configuration parsing
- Apprise (optional): Notifications
- pytest: Testing framework
