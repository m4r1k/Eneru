# Eneru

<p align="center">
  <img src="images/eneru-diagram.svg" alt="Eneru architecture" width="640">
</p>

Eneru monitors UPSes through [Network UPS Tools](https://networkupstools.org/) and coordinates shutdown before the batteries are exhausted. It is built for hosts that run more than one thing: VMs, containers, NAS mounts, remote servers, and multiple UPS groups.

<p align="center">
  <img src="images/eneru-mon.gif" alt="Eneru monitor dashboard" width="700">
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
| Operators | TUI dashboard, one-shot status output, SQLite history, graphs, and Apprise notifications |

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
| Dashboard and history | Limited | Limited | Dashboard | TUI, graphs, SQLite events |

## Start here

1. Install Eneru and create a minimal config: [Getting started](getting-started.md).
2. Choose your shutdown policy: [Configuration reference](configuration.md).
3. Tune the shutdown thresholds: [Shutdown triggers](triggers.md).
4. Add remote systems if needed: [Remote servers](remote-servers.md).
5. Test in dry-run mode before relying on it: [Troubleshooting](troubleshooting.md#safe-dry-run-test).

## Installation style in these docs

Native packages install Eneru under `/opt/ups-monitor/`, but expose `eneru` on `PATH`. The systemd service runs the package wrapper internally; operators can use:

```bash
sudo eneru run --config /etc/ups-monitor/config.yaml
```

PyPI installs expose the `eneru` command:

```bash
eneru run --config /etc/ups-monitor/config.yaml
```

Package commands in these docs use the package path where root/systemd behavior matters. PyPI examples use `eneru` when the context is developer or user-managed execution.
