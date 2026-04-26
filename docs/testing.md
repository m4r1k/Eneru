# Testing

Eneru uses unit tests, package-install tests, and end-to-end tests with real NUT, SSH, and Docker services. The goal is to catch both Python logic bugs and packaging/runtime failures.

```text
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
              ╱  on key Linux Distros ╲
             ╱─────────────────────────╲
            ╱         Unit Tests        ╲
           ╱       pytest + Coverage     ╲
          ╱        3.9 -> 3.15 Python     ╲
         ╱─────────────────────────────────╲
        ╱          Static Analysis          ╲
       ╱   Syntax Check + Config Validation  ╲
      ╱───────────────────────────────────────╲
     ╱          AI-Assisted Development        ╲
    ╱   Claude Code Review & Quality Assurance  ╲
   ╱─────────────────────────────────────────────╲
```

The pyramid is intentionally bottom-heavy. Most behavior is covered by fast pytest tests. E2E tests are fewer, but they exercise the real service boundaries where packaging, NUT, SSH, Docker, filesystem, and CLI assumptions meet.

## CI layout

| Workflow | File | What it checks |
|----------|------|----------------|
| Validate | `.github/workflows/validate.yml` | Unit tests, coverage, example config validation |
| Integration | `.github/workflows/integration.yml` | Wheel install, `.deb`, `.rpm`, and package layout |
| E2E Tests | `.github/workflows/e2e.yml` | Real NUT/SSH/Docker behavior across grouped scenarios |
| Release | `.github/workflows/release.yml` | Release package build |
| PyPI | `.github/workflows/pypi.yml` | PyPI publish from release tags |

The protected `main` branch requires the validate matrix and five E2E matrix jobs.

## Local test environment

All Python development commands must run inside a `uv` virtualenv. Do not run `pip`, `python`, or `pytest` against the system Python while working in this repo.

```bash
uv venv /tmp/eneru-venv
source /tmp/eneru-venv/bin/activate
uv pip install -e ".[dev,notifications,docs]"
```

Then run tests from inside the activated venv:

```bash
pytest
pytest -m unit
pytest -m integration
pytest --cov=src/eneru --cov-report=term
```

Validate example configs:

```bash
for config in examples/*.yaml; do
  python -m eneru validate --config "$config"
done
```

## Test areas

| Area | Coverage |
|------|----------|
| Config loading and validation | YAML parsing, defaults, enum validation, multi-UPS inheritance, local ownership, redundancy rules |
| Monitor state machine | OL/OB transitions, FSD, failsafe, shutdown trigger order, dry-run behavior |
| Shutdown mixins | VMs, containers, compose files, filesystem sync and unmounts, remote SSH phases |
| Multi-UPS coordinator | Group routing, `is_local`, drain policy, local shutdown locking, signal handling |
| Redundancy runtime | Quorum evaluation, advisory triggers, idempotent group execution |
| Health monitoring | Voltage thresholds, AVR, bypass, overload, battery anomaly filtering |
| Notifications | Formatting, retry queue, lifecycle classification, coalescing, suppression rules |
| Statistics and TUI | SQLite schema, aggregation, event queries, graphs, one-shot monitor output |
| Packaging | nFPM file list, package install paths, wrapper execution |

## End-to-end tests

The E2E suite runs on every pull request to `main` and every push to `main`. It is intentionally heavier than unit testing because it starts the same kinds of services Eneru depends on in production: NUT, SSH, Docker targets, real config files, and SQLite-backed state.

The E2E environment lives under `tests/e2e/` and uses Docker Compose:

```text
Eneru under test
  -> NUT dummy server
  -> SSH target container
  -> target containers and test mounts
```

The workflow is split into five parallel matrix groups:

| Group | Focus |
|-------|-------|
| CLI | Validation, bare command safety, one-shot output |
| UPS Single | Single UPS events and shutdown paths |
| UPS Multi | Independent UPS groups and local-drain policies |
| Redundancy | Quorum behavior and advisory triggers |
| Stats | SQLite, graphs, events, notification coalescing |

The scenario files simulate online, on-battery, low-battery, FSD, brownout, overload, hot-grid, and nominal-voltage-mismatch states.

### E2E test inventory

The numbered E2E tests are defined in `tests/e2e/groups/*.sh`. There are 36 numbered tests plus one CLI completion smoke check.

| Test | Group | What it proves |
|------|-------|----------------|
| 1 | CLI | Main E2E config validates against the running test environment |
| 2 | UPS Single | Normal online state keeps the daemon running and does not trigger shutdown |
| 3 | UPS Single | Low battery triggers the dry-run shutdown sequence and exits cleanly |
| 4 | UPS Single | Remote SSH shutdown command reaches the target container |
| 5 | UPS Single | UPS `FSD` flag triggers immediate shutdown handling |
| 6 | UPS Single | Brownout detection logs the expected voltage event and startup threshold context |
| 7 | UPS Single | Apprise notification test command can deliver through the configured secret-backed URL when available |
| 8 | CLI | Multi-UPS config validates while both dummy UPSes are reachable through NUT |
| 9 | UPS Multi | UPS1 failure is isolated while UPS2 remains online |
| 10 | UPS Multi | Both UPSes online does not create false multi-UPS shutdowns |
| 11 | CLI | Validation rejects local-resource ownership on a non-local UPS group |
| 12 | CLI | Bare `eneru` is safe and shows help instead of starting shutdown behavior |
| 13 | CLI | `eneru monitor --once` prints a usable one-shot UPS snapshot |
| 14 | UPS Multi | Concurrent failure of both UPSes triggers grouped shutdown behavior |
| 15 | UPS Multi | Non-local UPS failure triggers only that UPS group context |
| 16 | UPS Multi | `drain_on_local_shutdown=true` logs and executes the drain path |
| 17 | UPS Multi | Default no-drain behavior skips the drain path |
| 18 | UPS Multi | Power restored before shutdown logs recovery and avoids shutdown |
| 19 | UPS Multi | `shutdown_order` runs remote targets in ordered phases with parallel servers inside a phase |
| 20 | CLI | Redundancy-group validation accepts valid quorum config and rejects invalid `min_healthy` |
| 21 | Redundancy | Quorum holds when one of two UPSes remains healthy |
| 22 | Redundancy | Both UPSes critical exhaust quorum and fire redundancy shutdown |
| 23 | Redundancy | Default unknown-state handling is surfaced and does not fire in healthy steady state |
| 24 | Redundancy | Fail-safe redundancy shutdown fires when the group is effectively unsafe |
| 25 | Redundancy | Cross-group topology does not cascade into redundancy shutdown while quorum holds |
| 26 | Redundancy | Redundancy members log advisory trigger mode instead of immediate local shutdown |
| 27 | Redundancy | Separate Eneru-host UPS plus remote rack redundancy topology fires only the remote rack group |
| 28 | Stats | SQLite stats DB is created with samples and daemon-start event rows |
| 29 | Stats | Stats writer failure is non-fatal and does not crash monitoring |
| 30 | Stats | `monitor --once --graph` renders a graph from persisted stats data |
| 31 | Stats | `monitor --once --events-only` reads events from SQLite |
| 32 | Stats | Voltage nominal auto-detect re-snaps misreported NUT nominal voltage and records a silent event |
| 33 | UPS Single | `voltage_sensitivity` avoids 120 V hot-grid false alarms while preserving real brownout detection |
| 34 | Stats | Pending on-battery and restored notifications coalesce into one brief-outage summary |
| 35 | Stats | Single-UPS restart lifecycle sends one restart notification instead of stop/start noise |
| 36 | UPS Multi | Multi-UPS coordinator applies the same single-restart-notification contract across per-UPS stores |
| E1 | CLI | Bash, zsh, and fish shell completion output is syntactically usable |

Every commit on the protected workflow has to prove the daemon works against real services, not just isolated Python assertions: real NUT sockets, Dockerized SSH targets, a live SQLite database, rendered TUI output, validated production-shaped configs, and a full shutdown orchestration run. None of it depends on local developer state.

## Run E2E locally

Use the Python venv for Eneru commands, but Docker Compose provides the services.

```bash
source /tmp/eneru-venv/bin/activate

ssh-keygen -t ed25519 -f /tmp/e2e-ssh-key -N ""
cp /tmp/e2e-ssh-key.pub tests/e2e/ssh-target/authorized_keys

docker compose --project-directory tests/e2e up -d --build
sleep 10

upsc TestUPS@localhost:3493
python -m eneru validate --config tests/e2e/config-e2e-dry-run.yaml
python -m eneru run --config tests/e2e/config-e2e-dry-run.yaml --exit-after-shutdown

docker compose --project-directory tests/e2e down -v --remove-orphans
```

The GitHub workflow scripts under `tests/e2e/groups/` are the source of truth for exact CI steps.

## Documentation build

Build the ReadTheDocs site locally from the same uv venv:

```bash
source /tmp/eneru-venv/bin/activate
mkdocs build --strict
```

Serve it locally:

```bash
mkdocs serve
```

## Adding tests

For code changes:

- Add focused unit or integration tests under `tests/`.
- Add or update E2E coverage in `.github/workflows/e2e.yml` and `tests/e2e/groups/` for new features.
- Update example configs and docs when config keys change.
- Update this page if test counts, workflow groups, or test responsibilities change materially.

For documentation-only changes, a strict MkDocs build is usually enough.
