# Maintenance

Maintainer-facing reference: dependency pinning, installation layouts, and
release mechanics. Day-to-day contributor rules live in `AGENTS.md` at the
repo root; this page holds the detail that is only needed a few times a year.

## GitHub Actions SHA pin maintenance

Every third-party GitHub Actions invocation across the workflows
(`validate.yml`, `integration.yml`, `e2e.yml`, `codeql.yml`, `pypi.yml`,
`release.yml`, plus `.github/actions/e2e-setup/action.yml`) is pinned to a
full commit SHA with the corresponding tag in a trailing comment. A moved
upstream tag therefore cannot silently change what runs in CI — the pinned
SHA is the single source of truth.

These pins drift over time as upstream actions ship security fixes,
dependency bumps, and bug fixes under the same major-version tag. The repo
doesn't auto-renew them; refresh **about every 3 months**, or when a security
advisory lands for one of the pinned actions, or when an upstream
major-version bump is needed.

**How to refresh:**

```bash
# For tag-tracked actions (most cases — actions/checkout@v6, etc.):
gh api repos/<owner>/<repo>/git/refs/tags/<tag> --jq '.object.sha'

# For branch-tracked actions (pypa/gh-action-pypi-publish@release/v1):
gh api repos/<owner>/<repo>/branches/<branch> --jq '.commit.sha'
```

Update both the SHA and the `# vX.Y` trailing comment in lockstep. After
bumping, run the full CI matrix on a throwaway branch before merging — silent
breakage is the failure mode the pins exist to prevent in the first place.

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
| `docker/setup-buildx-action` | `v4.2.0` | `bb05f3f5…` |
| `docker/build-push-action` | `v7.3.0` | `53b7df96…` |

`nFPM` is similarly pinned (`NFPM_VERSION` env var in `release.yml` and
`integration.yml`) and verified against the goreleaser-published
`checksums.txt` before extraction. Bump the version constant and the checksum
check still verifies the new download.

The **Docker base image** (`Dockerfile`, `python:3.12-slim-trixie`) is
deliberately NOT digest-pinned (ISS-045). A base OS image is the one
dependency where freshness beats reproducibility: the mutable tag plus
`apt-get upgrade -y` pulls current security patches on every build, whereas a
frozen digest drifts stale (and potentially vulnerable) between manual
refreshes. This is the intentional exception to the SHA-pinning convention,
which still governs GitHub Actions and nFPM.

## Installation paths

Eneru has two installation methods with different invocation paths:

### Package installation (deb/rpm)

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

The wrapper script (`eneru.py`) adds `/opt/ups-monitor` to `sys.path` and
calls `eneru.cli.main()`. (On RHEL 8 it also re-execs onto `python39` — the
system `python3` there is 3.6.)

### Pip installation

Installs as a Python package with entry points defined in `pyproject.toml`.

**Invocation:** `eneru [options]` or `python -m eneru [options]`

### Documentation guidelines

When writing documentation, use the correct invocation style for the context:

| Context | Command style | Example |
|---------|---------------|---------|
| Package users (README, troubleshooting) | `/opt/ups-monitor/eneru.py` | `sudo python3 /opt/ups-monitor/eneru.py validate --config /etc/ups-monitor/config.yaml` |
| Developers (CONTRIBUTING, testing) | `python -m eneru` or `eneru` | `python -m eneru run --dry-run --config examples/config-reference.yaml` |
| PyPI users | `eneru` | `eneru validate` |

## Release mechanics

Tags are the immutable release snapshots. No release branches — tags are
sufficient for a single active version. GitHub Releases, .deb/.rpm packages,
and PyPI artifacts are all built from tags via CI (`release.yml`, `pypi.yml`).

**GitHub release "Latest" policy within a minor line:** the `X.Y.0` release
stays pinned as the advertised **Latest** on GitHub; every subsequent point
release is created with `--latest=false`:

```bash
gh release create vX.Y.Z ... --latest=false
```

Then advertise the point release from the pinned one: the `X.Y.0` release
body carries a top blockquote (`> **Update:** X.Y.Z is out — …`) that is
swapped on each point release. Fetch with
`gh release view vX.Y.0 --json body`, replace only the blockquote (what it
is, no new features / no breaking config changes, apt/dnf and Docker `latest`
upgrade automatically, link to the new notes, closing "This page stays pinned
as the latest stable."), and apply with
`gh release edit vX.Y.0 --notes-file <edited-body>` — preserve the rest of
the body byte-for-byte. Established by the v6.1.x series.
