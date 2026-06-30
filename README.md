<div align="center">

# ⚡  Eneru

**UPS monitoring and shutdown orchestration for NUT**

<a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-e8e8ed?style=for-the-badge&labelColor=090909" alt="MIT"></a>
<a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/Python-3.9+-e8e8ed?style=for-the-badge&labelColor=090909" alt="Python 3.9+"></a>
<a href="https://codecov.io/gh/m4r1k/Eneru"><img src="https://img.shields.io/codecov/c/github/m4r1k/Eneru?style=for-the-badge&labelColor=090909&color=e8e8ed&label=Coverage" alt="Coverage"></a>
<a href="https://eneru.readthedocs.io/"><img src="https://img.shields.io/badge/Docs-Read%20The%20Docs-e8e8ed?style=for-the-badge&labelColor=090909" alt="Documentation"></a>
<a href="https://pypi.org/project/eneru/"><img src="https://img.shields.io/pypi/v/eneru?style=for-the-badge&labelColor=090909&color=e8e8ed&label=PyPI" alt="PyPI"></a>
<a href="https://buymeacoffee.com/m4r1k"><img src="https://img.shields.io/badge/Buy%20Me%20a%20Coffee-FFDD00?style=for-the-badge&labelColor=090909&logo=buymeacoffee&logoColor=e8e8ed" alt="Buy Me a Coffee"></a>

<p align="center">
  <img src="https://raw.githubusercontent.com/m4r1k/Eneru/main/docs/images/eneru-diagram.svg" alt="Eneru Architecture" width="600">
</p>

A Python-based UPS monitoring daemon for [Network UPS Tools (NUT)](https://networkupstools.org/). Monitors one or more UPSes, orchestrates shutdown of VMs, containers, and remote servers during power events, and exposes a browser dashboard, authenticated API write paths, UPS control, Prometheus, MQTT, and Grafana observability.

[Documentation](https://eneru.readthedocs.io/) •
[Getting Started](https://eneru.readthedocs.io/latest/getting-started/) •
[Configuration](https://eneru.readthedocs.io/latest/configuration/) •
[Dashboard](https://eneru.readthedocs.io/latest/dashboard/) •
[Changelog](https://eneru.readthedocs.io/latest/changelog/) •
[Roadmap](https://eneru.readthedocs.io/latest/roadmap/)

</div>

<p align="center">
  <img src="https://raw.githubusercontent.com/m4r1k/Eneru/main/docs/images/eneru-mon.gif" alt="Eneru TUI monitor dashboard" height="280">
  <img src="https://raw.githubusercontent.com/m4r1k/Eneru/main/docs/images/eneru-webui.png" alt="Eneru browser dashboard" height="280">
  <img src="https://raw.githubusercontent.com/m4r1k/Eneru/main/docs/images/grafana.png" alt="Eneru Grafana dashboard" height="280">
</p>

---

## Why Eneru?

Most UPS shutdown tools handle one machine. If you have more than one, things get complicated fast:

| Challenge | Eneru Solution |
|-----------|----------------|
| Multiple UPSes powering different servers | ✅  Multi-UPS monitoring from a single instance |
| Multiple servers need coordinated shutdown | ✅  Orchestrated multi-server shutdown via SSH |
| VMs and containers need graceful stop | ✅  Libvirt VM and Docker/Podman container handling |
| Network mounts hang during power loss | ✅  Timeout-protected unmounting |
| No visibility during power events | ✅  Browser dashboard, TUI dashboard, and notifications via 100+ services |
| Different systems need different commands | ✅  Per-server custom shutdown commands |
| Hypervisors need VM shutdown before host | ✅  Pre-shutdown actions (Proxmox, ESXi, XCP-ng, libvirt) |
| Battery estimates are unreliable | ✅  Multi-vector shutdown triggers |
| Network down during outage | ✅  Non-blocking notifications with persistent retry |
| Firmware recalibrates battery silently | ✅  Battery anomaly detection and alerts |
| Need power-quality telemetry | ✅  Browser dashboard, API, Prometheus, MQTT, Grafana, JSON logs, and SQLite events |

---

## How Eneru is different

NUT's `upsmon` shuts down one machine with two triggers (low battery, forced shutdown). apcupsd does the same for APC hardware. PeaNUT and NUTCase provide dashboards but no shutdown logic. Enterprise tools (Eaton IPM, PowerChute) add virtualization support but are vendor-locked and proprietary.

Eneru sits on top of NUT and adds what these tools lack:

- **Orchestrated multi-resource shutdown**, VMs, compose stacks, containers, remote servers, filesystems, and local system in a coordinated sequence
- **6 independent shutdown triggers**, including depletion rate (computed from observed battery data, not UPS estimates) and extended time on battery. NUT's 2 triggers miss these failure modes
- **Multi-UPS coordination**, monitor multiple UPSes with per-group triggers and shutdown policies, each with independent failure handling
- **Browser dashboard and authenticated control**, live status, event history, event deletion, config reload, and allowlisted NUT `upscmd` / `upsrw` actions from the embedded API
- **Battery anomaly detection**, catches firmware recalibrations and battery degradation with vendor-specific jitter filtering (APC, CyberPower, Ubiquiti)

See the [full comparison](https://eneru.readthedocs.io/latest/#how-eneru-compares) in the documentation.

---

## Use cases

Homelabs, virtualization hosts (Proxmox, ESXi, libvirt), Docker/Podman container hosts, NAS systems (Synology, QNAP, TrueNAS), multi-UPS environments with multiple server groups, and mixed physical/virtual setups.

---

## Quick start

### Installation

**Docker / Podman:**
```bash
docker pull ghcr.io/m4r1k/eneru:latest

docker run -d --name eneru \
  --restart unless-stopped \
  -p 9191:9191 \
  -v /srv/eneru/config.yaml:/etc/ups-monitor/config.yaml:ro \
  -v /srv/eneru/state:/var/lib/eneru \
  -v /srv/eneru/run:/var/run/eneru \
  -v /srv/eneru/ssh:/var/lib/eneru/ssh \
  ghcr.io/m4r1k/eneru:latest \
  run --config /etc/ups-monitor/config.yaml \
  --api --api-bind 0.0.0.0 --api-port 9191
```

Keep private key files in `/srv/eneru/ssh` mode `0400`. The directory itself
stays writable so Eneru can persist learned SSH host keys in `known_hosts`.

Use `ghcr.io/m4r1k/eneru:testing` for pre-release builds. v5.5+ supports the OCI image for **both** remote-only and local-host deployments. For full local-host ownership from a container (host poweroff, VM teardown, container stop, filesystem unmount), add `--network host`, `-v /etc/machine-id:/etc/machine-id:ro`, and a loopback SSH key. Hosts without systemd (Alpine, musl) have no `/etc/machine-id` — use a [marker file](https://eneru.readthedocs.io/latest/containers-kubernetes/#no-systemd-no-machine-id-alpine-consumer-hosts) instead. See [Choose your install](https://eneru.readthedocs.io/latest/install-comparison/) for the three deployment profiles and [Migrate to container](https://eneru.readthedocs.io/latest/migrate-to-container/) for the step-by-step.

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
- Browser dashboard served by the embedded API, with authentication, history graphs, event management, and UPS control
- Real-time TUI dashboard (`eneru monitor`) with color-coded status
- Shutdown triggers: battery %, runtime, depletion rate, time on battery, FSD flag, and the connection-loss failsafe (six in total)
- Battery anomaly alerts for unexpected charge drops while on line power, with jitter filtering for APC, CyberPower, and Ubiquiti UniFi UPS units
- Battery-health score (0-100) with replacement prediction and tiered low-score alerts
- Scheduled NUT self-tests with recorded, normalized results
- Energy and cost tracking (kWh from real power or load, calendar today/month/year windows)
- Daily, weekly, and monthly reports (events, battery health, energy, uptime) via the notification channel
- Read-only shutdown-plan view: exactly what runs, in order, with per-phase timeouts and an estimate
- Shuts down VMs, containers, remote servers, filesystems, and the local system in order
- Notifications to 100+ services (Discord, Slack, Telegram, ntfy, email) via [Apprise](https://github.com/caronc/apprise/wiki)
- Power quality monitoring: voltage, AVR, bypass, overload
- API, Prometheus metrics, outbound MQTT, JSON/syslog logs, and Grafana dashboard
- Config hot-reload by `systemctl reload`, `SIGHUP`, or authenticated API request
- Official OCI image for both remote-only deployments and full local-host ownership via SSH loopback delegate (v5.5+)
- Dry-run mode for safe testing
- Comprehensive test suite across multiple Linux distros, with E2E tests against real NUT, SSH, Docker, and libvirt on every commit

---

## Three deployment profiles

| Install path | Local-host ownership | Remote systems | Recommended for |
|---|---|---|---|
| **pip / deb / rpm (native)** | First-class via systemd | Yes | Homelab, single-host professional, end-user-managed enterprise |
| **OCI image (Docker / Podman)** | Supported via SSH loopback delegate (v5.5+) | Yes | Homelab, professional, enterprise; the v5.5 default for containerized local-host |
| **Kubernetes** | Not recommended | Yes | Enterprise multi-site fleet monitoring of remote systems |

v5.5 made the OCI image first-class for the local-host case: the
container SSHes to the host it runs on so the namespace barrier
doesn't block the host-poweroff contract. Pick by deployment
preference, not capability.

See [Choose your install](https://eneru.readthedocs.io/latest/install-comparison/)
for the full feature × install matrix and [Containers and
Kubernetes](https://eneru.readthedocs.io/latest/containers-kubernetes/)
for Docker, Podman, SELinux/AppArmor, Kubernetes, and the SSH
walkthrough.

---

## The name

<img src="https://raw.githubusercontent.com/m4r1k/Eneru/main/docs/images/eneru.jpg" alt="Eneru from One Piece" width="120" align="right">

Named after [Eneru (エネル)](https://onepiece.fandom.com/wiki/Enel) from *One Piece*, the self-proclaimed God of Skypiea who ate the Goro Goro no Mi (Rumble-Rumble Fruit) and can control electricity. When the power from the grid fails, this tool takes over and shuts everything down safely. *Unlimited power... management!*

---

## Documentation

Full documentation at [eneru.readthedocs.io](https://eneru.readthedocs.io/):

- [Getting Started](https://eneru.readthedocs.io/latest/getting-started/) - installation and basic setup
- [Configuration](https://eneru.readthedocs.io/latest/configuration/) - full config reference
- [Dashboard](https://eneru.readthedocs.io/latest/dashboard/) - browser dashboard and event management
- [Authentication](https://eneru.readthedocs.io/latest/authentication/) - users, API keys, and read gating
- [NUT Control](https://eneru.readthedocs.io/latest/nut-control/) - authenticated UPS commands and writable variables
- [Shutdown Triggers](https://eneru.readthedocs.io/latest/triggers/) - how shutdown decisions work
- [Notifications](https://eneru.readthedocs.io/latest/notifications/) - Discord, Slack, Telegram, etc.
- [Remote Servers](https://eneru.readthedocs.io/latest/remote-servers/) - SSH setup for NAS and other servers
- [Testing](https://eneru.readthedocs.io/latest/testing/) - testing strategy and coverage
- [Troubleshooting](https://eneru.readthedocs.io/latest/troubleshooting/) - common issues and solutions

---

## Support the project

Eneru is free and MIT-licensed and will stay that way. If it has saved your homelab or rack from a dirty shutdown and you'd like to chip in toward UPS hardware, NUT testing, and the maintainer's coffee budget, [Buy Me a Coffee](https://buymeacoffee.com/m4r1k). Always optional, much appreciated.

---

## License

MIT License - See [LICENSE](LICENSE) file for details.
