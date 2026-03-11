<div align="center">

# ⚡ Eneru

**UPS monitoring and shutdown orchestration for NUT**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.9+](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/downloads/)
[![NUT Compatible](https://img.shields.io/badge/NUT-compatible-green.svg)](https://networkupstools.org/)
[![codecov](https://codecov.io/gh/m4r1k/Eneru/branch/main/graph/badge.svg)](https://codecov.io/gh/m4r1k/Eneru)
[![Documentation](https://img.shields.io/badge/docs-Read%20The%20Docs-blue.svg)](https://eneru.readthedocs.io/)
[![PyPI](https://img.shields.io/pypi/v/eneru.svg)](https://pypi.org/project/eneru/)

<p align="center">
  <img src="https://raw.githubusercontent.com/m4r1k/Eneru/main/docs/images/eneru-diagram.svg" alt="Eneru Architecture" width="600">
</p>

A Python-based UPS monitoring daemon that watches UPS status via [Network UPS Tools (NUT)](https://networkupstools.org/) and executes configurable shutdown sequences to protect your entire infrastructure during power events.

[Documentation](https://eneru.readthedocs.io/) •
[Getting Started](https://eneru.readthedocs.io/latest/getting-started/) •
[Configuration](https://eneru.readthedocs.io/latest/configuration/) •
[Changelog](CHANGELOG.md)

</div>

---

## Why Eneru?

Most UPS shutdown solutions handle a single system. Eneru handles multi-system environments:

| Challenge | Eneru Solution |
|-----------|----------------|
| Multiple servers need coordinated shutdown | ✅ Orchestrated multi-server shutdown via SSH |
| VMs and containers need graceful stop | ✅ Libvirt VM and Docker/Podman container handling |
| Network mounts hang during power loss | ✅ Timeout-protected unmounting |
| No visibility during power events | ✅ Real-time notifications via 100+ services |
| Different systems need different commands | ✅ Per-server custom shutdown commands |
| Hypervisors need graceful VM shutdown | ✅ Pre-shutdown actions (Proxmox, ESXi, XCP-ng, libvirt) |
| Battery estimates are unreliable | ✅ Multi-vector shutdown triggers |
| Network down during outage | ✅ Non-blocking notifications with persistent retry |

---

## Built for

- **Homelabs** - Protect your self-hosted infrastructure
- **Virtualization hosts** - Graceful VM shutdown before power loss
- **Container hosts** - Stop Docker/Podman containers safely
- **NAS systems** - Coordinate shutdown of Synology, QNAP, TrueNAS
- **Small business** - Multi-server environments with single UPS
- **Hybrid setups** - Mix of physical and virtual infrastructure

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
sudo python3 /opt/ups-monitor/eneru.py --validate-config
sudo systemctl enable --now eneru.service
```

### Minimal Config

```yaml
ups:
  name: "UPS@192.168.1.100"

triggers:
  low_battery_threshold: 20
  critical_runtime_threshold: 600

local_shutdown:
  enabled: true
```

See the [full documentation](https://eneru.readthedocs.io/) for complete configuration options.

---

## Features

- **Multi-vector shutdown triggers** - Battery %, runtime, depletion rate, time on battery, FSD flag
- **Orchestrated shutdown** - VMs, containers, remote servers, filesystems, local system
- **100+ notification services** - Discord, Slack, Telegram, ntfy, email via [Apprise](https://github.com/caronc/apprise/wiki)
- **Non-blocking notifications** - Persistent retry without delaying shutdown
- **Power quality monitoring** - Voltage, AVR, bypass, and overload detection
- **Dry-run mode** - Test your configuration safely
- **Tested on every commit** - Unit tests, integration tests across 7 Linux distros, and E2E tests with real NUT/SSH/Docker services

---

## Why a systemd daemon? (No Docker)

Eneru runs as a systemd daemon, not a container. Its job is to shut down Docker/Podman containers during power events, so running inside a container would mean getting killed during its own shutdown sequence.

See the [documentation](https://eneru.readthedocs.io/#why-a-systemd-daemon-no-docker) for the full explanation.

---

## The name

<img src="https://raw.githubusercontent.com/m4r1k/Eneru/main/docs/images/eneru.jpg" alt="Eneru from One Piece" width="120" align="right">

Named after [Eneru (エネル)](https://onepiece.fandom.com/wiki/Enel) from *One Piece*, the self-proclaimed God of Skypiea who ate the Goro Goro no Mi (Rumble-Rumble Fruit) and can control electricity. When the power from the grid fails, this tool takes over and shuts everything down safely. *Unlimited power... management!*

---

## Documentation

Full documentation at [eneru.readthedocs.io](https://eneru.readthedocs.io/):

- [Getting Started](https://eneru.readthedocs.io/latest/getting-started/) - Installation and basic setup
- [Configuration](https://eneru.readthedocs.io/latest/configuration/) - Full configuration reference
- [Shutdown Triggers](https://eneru.readthedocs.io/latest/triggers/) - How shutdown decisions are made
- [Notifications](https://eneru.readthedocs.io/latest/notifications/) - Setting up Discord, Slack, Telegram, etc.
- [Remote Servers](https://eneru.readthedocs.io/latest/remote-servers/) - SSH setup for NAS and other servers
- [Testing](https://eneru.readthedocs.io/latest/testing/) - Testing strategy and coverage
- [Troubleshooting](https://eneru.readthedocs.io/latest/troubleshooting/) - Common issues and solutions

---

## License

MIT License - See [LICENSE](LICENSE) file for details.
