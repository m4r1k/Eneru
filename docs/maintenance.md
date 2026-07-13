# Maintenance

Maintainer-facing reference: dependency pinning, installation layouts, and
release mechanics. Day-to-day contributor rules live in `AGENTS.md` at the
repo root; this page holds the detail that is only needed a few times a year.

## GitHub Actions tag maintenance

Third-party GitHub Actions use readable upstream release refs across the
workflows and `.github/actions/e2e-setup/action.yml`. Think of the ref as the
label on a toolbox: `@v7.0.0` tells a maintainer what is inside at a glance,
while a 40-character commit ID must be looked up before it means anything.

The tradeoff is deliberate: unlike a commit SHA, an upstream tag or release
branch can be moved. Eneru accepts that supply-chain risk for maintainability
and limits it by using actions from trusted publishers, having Dependabot scan
both workflow and composite-action directories weekly, reviewing upstream
release notes, and requiring CI to pass before an update merges.

Prefer an exact release tag when the publisher provides one. Use a maintained
major tag when that is the action's normal release channel, and retain an
official release branch only where the publisher documents one (currently
`pypa/gh-action-pypi-publish@release/v1`). Dependabot opens update PRs; review
the proposed ref and release notes, then let the full CI matrix test it.

The current set (as of 2026-07-13):

| Action | Release ref |
|---|---|
| `actions/checkout` | `v7.0.0` |
| `actions/setup-python` | `v6` |
| `actions/upload-artifact` | `v7.0.1` |
| `actions/download-artifact` | `v8.0.1` |
| `codecov/codecov-action` | `v6` |
| `github/codeql-action` | `v4` |
| `pypa/gh-action-pypi-publish` | `release/v1` |
| `softprops/action-gh-release` | `v3` |
| `docker/setup-qemu-action` | `v4.2.0` |
| `docker/setup-buildx-action` | `v4.2.0` |
| `docker/login-action` | `v4.4.0` |
| `docker/build-push-action` | `v7.3.0` |

`nFPM` is similarly pinned (`NFPM_VERSION` env var in `release.yml` and
`integration.yml`) and verified against the goreleaser-published
`checksums.txt` before extraction. Bump the version constant and the checksum
check still verifies the new download.

The **Docker base image** (`Dockerfile`, `python:3.12-slim-trixie`) is also
deliberately tag-based rather than digest-pinned (ISS-045). For the base OS,
freshness beats reproducibility: the mutable tag plus
`apt-get upgrade -y` pulls current security patches on every build, whereas a
frozen digest drifts stale (and potentially vulnerable) between manual
refreshes. nFPM remains version-pinned and checksum-verified because it is a
downloaded executable rather than an action or base image.

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
