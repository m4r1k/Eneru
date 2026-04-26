# Architecture

Eneru is intentionally split into small, boring pieces. The daemon has to keep making shutdown decisions while networks are flaky, notification endpoints are unreachable, SQLite is slow, or a remote server is already half gone. Most architectural choices come from that constraint.

This page is a map for operators and contributors. It explains the main runtime paths and points to the files to read next.

## System shape

Eneru does not talk to UPS hardware directly. NUT owns drivers and hardware communication; Eneru consumes NUT data and runs policy.

```text
  Hardware and drivers                 Eneru policy and action

+-----------------------+     +-----------------------+     +-----------------------+
| UPS hardware          |     | NUT server / upsc     |     | Eneru monitor         |
| USB, SNMP, vendor     +---->| driver-specific data  +---->| triggers and policy   |
| protocol              |     | UPS variables         |     | shutdown coordinator  |
+-----------------------+     +-----------------------+     +-----------+-----------+
                                                                  |
                  +-------------------------------+---------------+----------------+
                  |                               |                                |
                  v                               v                                v
       +----------+-----------+       +-----------+----------+        +------------+---------+
       | Shutdown phases      |       | Observability        |        | Notifications        |
       | VMs, containers, SSH |       | SQLite, events, TUI  |        | Apprise queue, retry |
       | filesystems, local   |       | graphs, state file   |        | coalescing          |
       +----------------------+       +----------------------+        +----------------------+
```

Start reading in [`src/eneru/monitor.py`](https://github.com/m4r1k/Eneru/blob/main/src/eneru/monitor.py). The module owns the per-UPS monitor loop, UPS polling, trigger evaluation, state updates, and shutdown orchestration.

## Per-UPS monitor loop

Each UPS group runs the same core loop. The loop polls NUT, updates state, records a stats sample, evaluates health checks, and triggers shutdown when a policy condition is met.

```text
UPSGroupMonitor loop

    +-----------------------+
    | poll NUT via upsc     |
    | one snapshot/cycle    |
    +-----------+-----------+
                |
                v
    +-----------+-----------+        +-------------------------+
    | update MonitorState   +------->| state file              |
    | status, timers, flags |        | SQLite sample buffer    |
    +-----------+-----------+        +-------------------------+
                |
                v
    +-----------+-----------+
    | health checks         |
    | voltage, AVR, bypass  |
    | overload, battery     |
    +-----------+-----------+
                |
                v
    +-----------+-----------+
    | shutdown triggers     |
    | FSD, battery, runtime |
    | depletion, extended   |
    +-----------+-----------+
                |
                v
    +-----------+-----------+
    | shutdown sequence     |
    | only when triggered   |
    +-----------------------+
```

Key files:

| File | What to read there |
|------|--------------------|
| [`src/eneru/monitor.py`](https://github.com/m4r1k/Eneru/blob/main/src/eneru/monitor.py) | `UPSGroupMonitor`, polling, trigger evaluation, shutdown sequence, lifecycle cleanup |
| [`src/eneru/state.py`](https://github.com/m4r1k/Eneru/blob/main/src/eneru/state.py) | `MonitorState`, the in-memory state shared by the loop, TUI snapshots, and redundancy evaluator |
| [`src/eneru/health/voltage.py`](https://github.com/m4r1k/Eneru/blob/main/src/eneru/health/voltage.py) | Voltage thresholds, auto-detect re-snap, hysteresis, AVR, bypass, overload |
| [`src/eneru/health/battery.py`](https://github.com/m4r1k/Eneru/blob/main/src/eneru/health/battery.py) | Depletion-rate history and battery anomaly confirmation |

## Shutdown phases are mixins

Shutdown behavior is decomposed into mixins instead of one large monitor file. `UPSGroupMonitor` owns the sequence; each phase owns its own implementation.

```text
UPSGroupMonitor
  |
  +-- VMShutdownMixin
  |     libvirt / virsh graceful shutdown, force-destroy fallback
  |
  +-- ContainerShutdownMixin
  |     Docker / Podman detection, compose stacks, remaining containers
  |
  +-- FilesystemShutdownMixin
  |     sync, per-mount unmount, timeout handling
  |
  +-- RemoteShutdownMixin
        SSH phases, pre-shutdown actions, final shutdown command
```

The sequence stays readable in `monitor.py`, while the mechanics live in small files:

| File | Phase |
|------|-------|
| [`src/eneru/shutdown/vms.py`](https://github.com/m4r1k/Eneru/blob/main/src/eneru/shutdown/vms.py) | Libvirt/KVM VMs |
| [`src/eneru/shutdown/containers.py`](https://github.com/m4r1k/Eneru/blob/main/src/eneru/shutdown/containers.py) | Docker, Podman, compose |
| [`src/eneru/shutdown/filesystems.py`](https://github.com/m4r1k/Eneru/blob/main/src/eneru/shutdown/filesystems.py) | Sync and unmount |
| [`src/eneru/shutdown/remote.py`](https://github.com/m4r1k/Eneru/blob/main/src/eneru/shutdown/remote.py) | SSH-based remote shutdown |
| [`src/eneru/actions.py`](https://github.com/m4r1k/Eneru/blob/main/src/eneru/actions.py) | Predefined remote action command templates |

This makes shutdown extensions easier to review. A new phase should be a small mixin, wired into `UPSGroupMonitor`, documented in the config reference, and covered by unit and E2E tests.

## Multi-UPS coordination

Single-UPS mode runs one `UPSGroupMonitor`. Multi-UPS mode creates one monitor per UPS group and shares the logger, notification worker, and local-shutdown coordination.

```text
                    +------------------------+
                    | MultiUPSCoordinator    |
                    | starts one monitor per |
                    | configured UPS group   |
                    +-----------+------------+
                                |
       +------------------------+------------------------+
       |                        |                        |
       v                        v                        v
+------+---------+      +-------+--------+       +-------+--------+
| UPS group A    |      | UPS group B    |       | UPS group C    |
| monitor thread |      | monitor thread |       | monitor thread |
+------+---------+      +-------+--------+       +-------+--------+
       |                        |                        |
       +------------------------+------------------------+
                                |
                                v
                    +-----------+------------+
                    | shared services        |
                    | notification worker    |
                    | local shutdown lock    |
                    | lifecycle coordination |
                    +------------------------+
```

The important rule is ownership. Only the group marked `is_local: true` can manage local resources such as VMs, containers, and filesystems. Remote servers can belong to any UPS group, but validation prevents duplicate ownership.

Read [`src/eneru/multi_ups.py`](https://github.com/m4r1k/Eneru/blob/main/src/eneru/multi_ups.py) for coordinator lifecycle, monitor thread management, local shutdown locking, and signal handling.

## Redundancy groups

Redundancy groups are separate from independent UPS groups. They watch multiple UPS snapshots and shut down shared resources only when quorum is lost.

```text
 +----------------------+     +----------------------+
 | UPS-A monitor state  |     | UPS-B monitor state  |
 | health + advisory    |     | health + advisory    |
 +----------+-----------+     +----------+-----------+
            |                            |
            +-------------+--------------+
                          |
                          v
            +-------------+--------------+
            | RedundancyGroupEvaluator   |
            | classify each member as    |
            | HEALTHY, DEGRADED,         |
            | CRITICAL, or UNKNOWN       |
            +-------------+--------------+
                          |
                          v
                  quorum still healthy?
                     |              |
                    yes             no
                     |              |
                     v              v
             keep monitoring   run group shutdown
```

Per-UPS triggers still run for redundancy members, but they become advisory flags. The evaluator decides whether the group should act based on `min_healthy`, `degraded_counts_as`, and `unknown_counts_as`.

Read [`src/eneru/redundancy.py`](https://github.com/m4r1k/Eneru/blob/main/src/eneru/redundancy.py) for the evaluator, executor, group flag files, and shutdown reuse.

## Persistent notifications

Notifications are deliberately asynchronous. The monitor inserts a row into SQLite and continues; a worker thread handles delivery, retry, coalescing, and lifecycle cleanup.

```text
 +-------------------+      insert row       +------------------------+
 | monitor thread    +---------------------->| SQLite notifications   |
 | power/lifecycle   |                       | pending/sent/cancelled |
 | event occurs      |                       +-----------+------------+
 +---------+---------+                                   ^
           |                                             |
           | continue shutdown                           | retry/update rows
           v                                             |
 +---------+---------+                         +---------+--------------+
 | shutdown work     |                         | NotificationWorker    |
 | never waits on    |                         | Apprise delivery      |
 | network delivery  |                         | coalescing, pruning   |
 +-------------------+                         +------------------------+
```

This solves a real outage problem: when power is unstable, the network path to Discord, email, ntfy, or a phone push provider is often unstable too. Eneru should not delay VM shutdown because a notification endpoint is down.

Key files:

| File | What to read there |
|------|--------------------|
| [`src/eneru/notifications.py`](https://github.com/m4r1k/Eneru/blob/main/src/eneru/notifications.py) | `NotificationWorker`, retry, backoff, coalescing, flush behavior |
| [`src/eneru/stats.py`](https://github.com/m4r1k/Eneru/blob/main/src/eneru/stats.py) | SQLite `notifications` table and persistence helpers |
| [`src/eneru/lifecycle.py`](https://github.com/m4r1k/Eneru/blob/main/src/eneru/lifecycle.py) | Startup classification, recovery fold-in, lifecycle coalescing |
| [`src/eneru/deferred_delivery.py`](https://github.com/m4r1k/Eneru/blob/main/src/eneru/deferred_delivery.py) | systemd deferred stop delivery and restart suppression |

## SQLite stats and TUI separation

The monitoring loop writes samples to an in-memory buffer. `StatsWriter` flushes that buffer in the background. The TUI opens the database read-only and blends in live state-file samples so graphs do not lag by the full writer interval.

```text
 +--------------+      +------------------+      +------------------+
 | monitor loop +----->| sample buffer    +----->| StatsWriter      |
 | one UPS poll |      | in memory        |      | flush every 10s  |
 +--------------+      +------------------+      +--------+---------+
                                                           |
                                                           v
                                                 +---------+----------+
                                                 | per-UPS SQLite    |
                                                 | samples, events   |
                                                 | aggregates, queue |
                                                 +---------+----------+
                                                           ^
                                                           |
                                       read-only queries   |
                                                           |
                                                 +---------+----------+
                                                 | TUI dashboard     |
                                                 | status, graph     |
                                                 | recent events     |
                                                 +--------------------+
```

Read [`src/eneru/stats.py`](https://github.com/m4r1k/Eneru/blob/main/src/eneru/stats.py), [`src/eneru/tui.py`](https://github.com/m4r1k/Eneru/blob/main/src/eneru/tui.py), and [`src/eneru/graph.py`](https://github.com/m4r1k/Eneru/blob/main/src/eneru/graph.py) for the data store, dashboard, and Braille graph renderer.

## Configuration as a safety boundary

Config parsing is not just YAML loading. It encodes safety rules:

- Raw voltage warning thresholds are not user-configurable; users choose bounded presets.
- Safety-critical notification events cannot be suppressed.
- Non-local UPS groups cannot own local resources.
- A remote server cannot be owned by both an independent UPS group and a redundancy group.
- `shutdown_order` and legacy `parallel` are mutually exclusive.
- Exactly one UPS or redundancy group may be local.

```text
config.yaml
    |
    v
+---+----------------+
| ConfigLoader       |
| parse YAML         |
| apply defaults     |
| validate safety    |
+---+----------------+
    |
    v
typed dataclasses used by monitors, coordinator, TUI, and workers
```

Read [`src/eneru/config.py`](https://github.com/m4r1k/Eneru/blob/main/src/eneru/config.py) for the dataclasses, parser, compatibility aliases, and validator rules.

## Packaging matters

Eneru is a system daemon first. The native package path and the PyPI path intentionally differ:

| Install path | Runtime shape |
|--------------|---------------|
| deb/rpm | `/opt/ups-monitor/eneru.py` wrapper, systemd service, config under `/etc/ups-monitor/`, command exposed as `eneru` |
| PyPI | Python package entry point, user-managed service or foreground process |

This is why packaging tests check installed files and import layout. The deb/rpm build enumerates package contents explicitly in `nfpm.yaml`, so adding a new module under `src/eneru/` requires a matching package entry.

Read [`packaging/eneru-wrapper.py`](https://github.com/m4r1k/Eneru/blob/main/packaging/eneru-wrapper.py), [`packaging/eneru.service`](https://github.com/m4r1k/Eneru/blob/main/packaging/eneru.service), [`nfpm.yaml`](https://github.com/m4r1k/Eneru/blob/main/nfpm.yaml), and [`tests/test_packaging.py`](https://github.com/m4r1k/Eneru/blob/main/tests/test_packaging.py).

## Contributor reading order

If you want to dig deeper, read in this order:

1. [`src/eneru/CLAUDE.md`](https://github.com/m4r1k/Eneru/blob/main/src/eneru/CLAUDE.md) for the module map and mixin conventions.
2. [`src/eneru/config.py`](https://github.com/m4r1k/Eneru/blob/main/src/eneru/config.py) for the data model.
3. [`src/eneru/monitor.py`](https://github.com/m4r1k/Eneru/blob/main/src/eneru/monitor.py) for the single-UPS runtime.
4. [`src/eneru/multi_ups.py`](https://github.com/m4r1k/Eneru/blob/main/src/eneru/multi_ups.py) for group coordination.
5. [`src/eneru/redundancy.py`](https://github.com/m4r1k/Eneru/blob/main/src/eneru/redundancy.py) if you are working on A+B power.
6. [`src/eneru/notifications.py`](https://github.com/m4r1k/Eneru/blob/main/src/eneru/notifications.py) and [`src/eneru/stats.py`](https://github.com/m4r1k/Eneru/blob/main/src/eneru/stats.py) if you are working on observability.
7. The matching tests under [`tests/`](https://github.com/m4r1k/Eneru/tree/main/tests) before changing behavior.
