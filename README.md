<div align="center">

# ⚡ Eneru

**Intelligent UPS Monitoring & Shutdown Orchestration for NUT**

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

## ✨ Why Eneru?

Most UPS shutdown solutions are **single-system focused**. Eneru is designed for **modern infrastructure**:

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

## 🎯 Built For

- 🏠 **Homelabs** - Protect your self-hosted infrastructure
- 🖥️ **Virtualization Hosts** - Graceful VM shutdown before power loss
- 🐳 **Container Hosts** - Stop Docker/Podman containers safely
- 📦 **NAS Systems** - Coordinate shutdown of Synology, QNAP, TrueNAS
- 🏢 **Small Business** - Multi-server environments with single UPS
- ☁️ **Hybrid Setups** - Mix of physical and virtual infrastructure

---

## 🚀 Quick Start

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

## ✨ Features

- **Multi-vector shutdown triggers** - Battery %, runtime, depletion rate, time on battery, FSD flag
- **Orchestrated shutdown** - VMs, containers, remote servers, filesystems, local system
- **100+ notification services** - Discord, Slack, Telegram, ntfy, Email via [Apprise](https://github.com/caronc/apprise/wiki)
- **Non-blocking notifications** - Persistent retry without delaying shutdown
- **Power quality monitoring** - Voltage, AVR, bypass, and overload detection
- **Dry-run mode** - Test your configuration safely
- **Comprehensive testing** - Unit tests, integration tests across 7 Linux distros, and E2E tests with real NUT/SSH/Docker services on every commit

---

## 🤔 Why an Old-Fashioned Systemd Daemon? (No Docker)

Eneru runs as a systemd daemon, not a container. This is intentional—Eneru's job is to shut down Docker/Podman containers during power events. If Eneru ran inside a container, it would be killed during its own shutdown sequence.

See the [documentation](https://eneru.readthedocs.io/#why-a-systemd-daemon-no-docker) for the full explanation.

---

## ⚡ The Name

<img src="https://raw.githubusercontent.com/m4r1k/Eneru/main/docs/images/eneru.jpg" alt="Eneru from One Piece" width="120" align="right">

Named after [Eneru (エネル)](https://onepiece.fandom.com/wiki/Enel) from *One Piece*—the self-proclaimed God of Skypiea who ate the Goro Goro no Mi (Rumble-Rumble Fruit), granting him absolute control over electricity. Just as Eneru commands lightning from the sky, this tool commands your infrastructure when the power from the grid fails. *Unlimited power... management!* ⚡

---

## 📚 Documentation

Full documentation is available at **[eneru.readthedocs.io](https://eneru.readthedocs.io/)**:

- [Getting Started](https://eneru.readthedocs.io/latest/getting-started/) - Installation and basic setup
- [Configuration](https://eneru.readthedocs.io/latest/configuration/) - Full configuration reference
- [Shutdown Triggers](https://eneru.readthedocs.io/latest/triggers/) - How shutdown decisions are made
- [Notifications](https://eneru.readthedocs.io/latest/notifications/) - Setting up Discord, Slack, Telegram, etc.
- [Remote Servers](https://eneru.readthedocs.io/latest/remote-servers/) - SSH setup for NAS and other servers
- [Testing](https://eneru.readthedocs.io/latest/testing/) - Testing strategy and coverage
- [Troubleshooting](https://eneru.readthedocs.io/latest/troubleshooting/) - Common issues and solutions

---

## 📄 License

MIT License - See [LICENSE](LICENSE) file for details.
