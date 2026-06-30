# Eneru

<p align="center">
  <img src="images/eneru-diagram.svg" alt="Eneru architecture" width="640">
</p>

Eneru monitors UPSes through [Network UPS Tools](https://networkupstools.org/) and coordinates shutdown before the batteries are exhausted. It is built for hosts that run more than one thing: VMs, containers, NAS mounts, remote servers, and multiple UPS groups. v6.0 added a browser dashboard, authenticated API write paths, UPS control, and config hot-reload; v6.1 adds battery-health scoring with replacement prediction, energy and cost tracking, scheduled self-tests, periodic reports, and a read-only shutdown-plan view.

<p align="center">
  <img src="images/eneru-mon.gif" alt="Eneru TUI monitor dashboard" height="260">
  <img src="images/eneru-webui.gif" alt="Eneru browser dashboard" height="260">
  <img src="images/grafana.png" alt="Eneru Grafana dashboard" height="260">
</p>

## What Eneru does

Eneru is the layer above NUT. NUT talks to the UPS hardware. Eneru decides what to do with that information.

| Area | What Eneru adds |
|------|-----------------|
| Shutdown decisions | Battery level, runtime, depletion rate, extended time on battery, FSD, and failsafe connection loss |
| Local resources | Libvirt VMs, Docker or Podman containers, compose stacks, filesystem sync, and unmounts |
| Remote systems | SSH shutdown with ordered phases and pre-shutdown actions for Proxmox, ESXi, XCP-ng, Docker, and custom commands |
| Multiple UPSes | Independent UPS groups, shared configuration defaults, and one local-shutdown owner |
| Redundant power | Quorum-based redundancy groups for dual-PSU servers and A+B power feeds |
| Battery and energy | Battery-health score (0-100) with replacement prediction and tiered alerts, scheduled NUT self-tests, kWh and cost tracking, and daily/weekly/monthly reports |
| Operators | Browser dashboard, TUI dashboard, one-shot status output, SQLite history/events, authenticated API writes, a read-only shutdown-plan view, Prometheus, MQTT, Grafana, JSON/syslog logs, and Apprise notifications |
| Deployment | Native systemd packages, plus an OCI image that is first-class for both remote-only AND full local-host ownership (v5.5+ SSH loopback delegate) |

!!! note "Eneru does not replace NUT"
    NUT still owns UPS drivers, hardware communication, and the `upsc` data model. Eneru consumes that data and runs the shutdown policy.

## When to use it

Use Eneru when a basic `upsmon` script is no longer enough:

- One UPS protects a host with VMs, containers, and mounted storage.
- Several servers need to shut down in a specific order.
- A NAS should shut down after compute nodes release NFS or SMB mounts.
- Multiple UPSes protect different racks from one monitoring host.
- Dual-PSU servers should remain online while at least one UPS feed is healthy.
- You want alerts and historical data during power problems.

For a single workstation with one UPS and no dependencies, NUT's built-in `upsmon` may be simpler.

## Shutdown model

Every shutdown phase is optional. A typical sequence looks like this:

```text
Power event detected
  -> evaluate triggers
  -> stop local VMs
  -> stop compose stacks
  -> stop remaining containers
  -> sync and unmount filesystems
  -> shut down remote servers by phase
  -> power off the local host
```

Multi-UPS mode runs the same sequence per UPS group. Redundancy groups use the same resource model, but they fire only when the group loses quorum.

## How it compares

| Capability | NUT upsmon | apcupsd | PeaNUT | Eneru |
|------------|------------|---------|--------|-------|
| Hardware support | NUT drivers | APC only | NUT data | NUT data |
| Shutdown triggers | LOWBATT, FSD | Timer and scripts | None | Six trigger paths plus failsafe |
| VM/container handling | Script yourself | Script yourself | None | Built in |
| Remote shutdown | Script yourself | Script yourself | None | SSH phases and predefined actions |
| Multiple UPS groups | Host-level | One UPS per instance | Display only | Per-group orchestration |
| Redundant A+B feeds | No | No | No | Quorum evaluator |
| Notifications | Script yourself | Event scripts | No | Apprise with persistent retry |
| Dashboard and history | Limited | Limited | Dashboard | Browser dashboard, TUI, graphs, SQLite events |

## Start here

1. Pick the right install for your deployment: [Choose your install](install-comparison.md) (native vs OCI container vs Kubernetes).
2. Install Eneru and create a minimal config: [Getting started](getting-started.md).
3. Choose your shutdown policy: [Configuration reference](configuration.md).
4. Tune the shutdown thresholds: [Shutdown triggers](triggers.md).
5. Add remote systems if needed: [Remote servers](remote-servers.md).
6. Enable the browser dashboard, authentication, or UPS control if needed: [Dashboard](dashboard.md), [Authentication](authentication.md), [NUT control](nut-control.md).
7. If you are deploying in containers, use [Containers and Kubernetes](containers-kubernetes.md). Migrating from deb/rpm: [Migrate to container](migrate-to-container.md).
8. Test in dry-run mode before relying on it: [Troubleshooting](troubleshooting.md#safe-dry-run-test).

## Installation style in these docs

Native packages install Eneru under `/opt/ups-monitor/`, but expose `eneru` on `PATH`. The systemd service runs the package wrapper internally; operators can use:

```bash
sudo eneru run --config /etc/ups-monitor/config.yaml
```

PyPI installs expose the `eneru` command:

```bash
eneru run --config /etc/ups-monitor/config.yaml
```

OCI container examples use `docker run`, `podman run`, or Kubernetes YAML
and keep the same `eneru` entry point inside the image. Remote-only containers
can run as non-root. Containerized local-host ownership, including local VM,
Docker/Podman, compose, sync, unmount, and host shutdown actions, uses the
v5.5 SSH loopback delegate; see [Containers and Kubernetes](containers-kubernetes.md)
for the required mounts, SSH key, and security model.

Package commands in these docs use the package path where root/systemd behavior matters. PyPI and in-container examples use `eneru` when the context is developer or user-managed execution.

## Support the project

Eneru is free and MIT-licensed and will stay that way. If it has saved your homelab or rack from a dirty shutdown and you'd like to chip in toward UPS hardware, NUT testing, and the maintainer's coffee budget, [Buy Me a Coffee](https://buymeacoffee.com/m4r1k). Always optional, much appreciated.
