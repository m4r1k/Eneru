<div align="center">

# ⚡ Eneru

**UPS monitoring and shutdown orchestration for NUT**

<a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-e8e8ed?style=for-the-badge&labelColor=090909" alt="MIT"></a>
<a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/Python-3.9+-e8e8ed?style=for-the-badge&labelColor=090909" alt="Python 3.9+"></a>
<a href="https://networkupstools.org/"><img src="https://img.shields.io/badge/NUT-compatible-e8e8ed?style=for-the-badge&labelColor=090909" alt="NUT compatible"></a>
<a href="https://codecov.io/gh/m4r1k/Eneru"><img src="https://img.shields.io/codecov/c/github/m4r1k/Eneru?style=for-the-badge&labelColor=090909&color=e8e8ed&label=Coverage" alt="Coverage"></a>
<a href="https://eneru.readthedocs.io/"><img src="https://img.shields.io/badge/Docs-Read%20The%20Docs-e8e8ed?style=for-the-badge&labelColor=090909" alt="Documentation"></a>
<a href="https://pypi.org/project/eneru/"><img src="https://img.shields.io/pypi/v/eneru?style=for-the-badge&labelColor=090909&color=e8e8ed&label=PyPI" alt="PyPI"></a>

<p align="center">
  <img src="https://raw.githubusercontent.com/m4r1k/Eneru/main/docs/images/eneru-diagram.svg" alt="Eneru Architecture" width="600">
</p>

A Python-based UPS monitoring daemon for [Network UPS Tools (NUT)](https://networkupstools.org/). Monitors one or more UPSes, orchestrates shutdown of VMs, containers, and remote servers during power events.

[Documentation](https://eneru.readthedocs.io/) •
[Getting Started](https://eneru.readthedocs.io/latest/getting-started/) •
[Configuration](https://eneru.readthedocs.io/latest/configuration/) •
[Changelog](https://eneru.readthedocs.io/latest/changelog/) •
[Roadmap](https://eneru.readthedocs.io/latest/roadmap/)

</div>

<p align="center">
  <img src="https://raw.githubusercontent.com/m4r1k/Eneru/main/docs/images/eneru-mon.gif" alt="Eneru Monitor Dashboard" width="400">
</p>

---

## Why Eneru?

Most UPS shutdown tools handle one machine. If you have more than one, things get complicated fast:

| Challenge | Eneru Solution |
|-----------|----------------|
| Multiple UPSes powering different servers | ✅ Multi-UPS monitoring from a single instance |
| Multiple servers need coordinated shutdown | ✅ Orchestrated multi-server shutdown via SSH |
| VMs and containers need graceful stop | ✅ Libvirt VM and Docker/Podman container handling |
| Network mounts hang during power loss | ✅ Timeout-protected unmounting |
| No visibility during power events | ✅ Real-time TUI dashboard + notifications via 100+ services |
| Different systems need different commands | ✅ Per-server custom shutdown commands |
| Hypervisors need VM shutdown before host | ✅ Pre-shutdown actions (Proxmox, ESXi, XCP-ng, libvirt) |
| Battery estimates are unreliable | ✅ Multi-vector shutdown triggers |
| Network down during outage | ✅ Non-blocking notifications with persistent retry |
| Firmware recalibrates battery silently | ✅ Battery anomaly detection and alerts |

---

## How Eneru is different

NUT's `upsmon` shuts down one machine with two triggers (low battery, forced shutdown). apcupsd does the same for APC hardware. PeaNUT and NUTCase provide dashboards but no shutdown logic. Enterprise tools (Eaton IPM, PowerChute) add virtualization support but are vendor-locked and proprietary.

Eneru sits on top of NUT and adds what these tools lack:

- **Orchestrated multi-resource shutdown**, VMs, compose stacks, containers, remote servers, filesystems, and local system in a coordinated sequence
- **6 independent shutdown triggers**, including depletion rate (computed from observed battery data, not UPS estimates) and extended time on battery. NUT's 2 triggers miss these failure modes
- **Multi-UPS coordination**, monitor multiple UPSes with per-group triggers and shutdown policies, each with independent failure handling
- **Battery anomaly detection**, catches firmware recalibrations and battery degradation with vendor-specific jitter filtering (APC, CyberPower, Ubiquiti)

See the [full comparison](https://eneru.readthedocs.io/latest/#how-eneru-compares) in the documentation.

---

## Use cases

Homelabs, virtualization hosts (Proxmox, ESXi, libvirt), Docker/Podman container hosts, NAS systems (Synology, QNAP, TrueNAS), multi-UPS environments with multiple server groups, and mixed physical/virtual setups.

---

## Quick start

### Installation

**PyPI:**
```bash
pip install eneru[notifications]
```

**Debian/Ubuntu:**
```bash
curl -fsSL https://m4r1k.github.io/Eneru/KEY.gpg | sudo gpg --dearmor -o /usr/share/keyrings/eneru.gpg
echo "deb [arch=all signed-by=/usr/share/keyrings/eneru.gpg] https://m4r1k.github.io/Eneru/deb stable main" | sudo tee /etc/apt/sources.list.d/eneru.list
sudo apt update && sudo apt install eneru
```

**RHEL/Fedora:**
```bash
sudo dnf install -y epel-release
sudo curl -o /etc/yum.repos.d/eneru.repo https://m4r1k.github.io/Eneru/rpm/eneru.repo
sudo dnf install eneru
```

### Configuration

```bash
# Edit configuration
sudo nano /etc/ups-monitor/config.yaml

# Validate and start
eneru validate --config /etc/ups-monitor/config.yaml
sudo systemctl enable --now eneru.service

# Monitor in real time
eneru monitor --config /etc/ups-monitor/config.yaml
```

### Single UPS

```yaml
ups:
  name: "UPS@192.168.1.100"
  display_name: "Main UPS"

triggers:
  low_battery_threshold: 20
  critical_runtime_threshold: 600

local_shutdown:
  enabled: true
```

### Multiple UPSes

```yaml
ups:
  - name: "UPS1@192.168.1.10"
    display_name: "Rack A UPS"
    is_local: true
    remote_servers:
      - name: "Proxmox Node"
        enabled: true
        host: "192.168.1.20"
        user: "root"

  - name: "UPS2@192.168.1.11"
    display_name: "Rack B UPS"
    remote_servers:
      - name: "NAS"
        enabled: true
        host: "192.168.1.30"
        user: "admin"
```

See the [full documentation](https://eneru.readthedocs.io/) for complete configuration options.

---

## Features

- Monitor one or more UPSes from a single instance, each with its own shutdown group
- Real-time TUI dashboard (`eneru monitor`) with color-coded status
- Shutdown triggers: battery %, runtime, depletion rate, time on battery, FSD flag
- Battery anomaly alerts for unexpected charge drops while on line power, with jitter filtering for APC, CyberPower, and Ubiquiti UniFi UPS units
- Shuts down VMs, containers, remote servers, filesystems, and the local system in order
- Notifications to 100+ services (Discord, Slack, Telegram, ntfy, email) via [Apprise](https://github.com/caronc/apprise/wiki)
- Power quality monitoring: voltage, AVR, bypass, overload
- Dry-run mode for safe testing
- 300 tests, 9 Linux distros, E2E tests with real NUT/SSH/Docker on every commit

---

## Why a systemd daemon? (No Docker)

Eneru runs as a systemd daemon, not a container. It shuts down Docker/Podman containers during power events, so running inside a container would mean getting killed during its own shutdown sequence.

See the [documentation](https://eneru.readthedocs.io/#why-a-systemd-daemon-no-docker) for the full explanation.

---

## The name

<img src="https://raw.githubusercontent.com/m4r1k/Eneru/main/docs/images/eneru.jpg" alt="Eneru from One Piece" width="120" align="right">

Named after [Eneru (エネル)](https://onepiece.fandom.com/wiki/Enel) from *One Piece*, the self-proclaimed God of Skypiea who ate the Goro Goro no Mi (Rumble-Rumble Fruit) and can control electricity. When the power from the grid fails, this tool takes over and shuts everything down safely. *Unlimited power... management!*

---

## Documentation

Full documentation at [eneru.readthedocs.io](https://eneru.readthedocs.io/):

- [Getting Started](https://eneru.readthedocs.io/latest/getting-started/) - installation and basic setup
- [Configuration](https://eneru.readthedocs.io/latest/configuration/) - full config reference
- [Shutdown Triggers](https://eneru.readthedocs.io/latest/triggers/) - how shutdown decisions work
- [Notifications](https://eneru.readthedocs.io/latest/notifications/) - Discord, Slack, Telegram, etc.
- [Remote Servers](https://eneru.readthedocs.io/latest/remote-servers/) - SSH setup for NAS and other servers
- [Testing](https://eneru.readthedocs.io/latest/testing/) - testing strategy and coverage
- [Troubleshooting](https://eneru.readthedocs.io/latest/troubleshooting/) - common issues and solutions

---

## License

MIT License - See [LICENSE](LICENSE) file for details.
