<div align="center">

# 🔋 UPS Tower

**Intelligent UPS Monitoring & Shutdown Orchestration for NUT**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.9+](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/downloads/)
[![NUT Compatible](https://img.shields.io/badge/NUT-compatible-green.svg)](https://networkupstools.org/)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](http://makeapullrequest.com)

<p align="center">
  <img src="docs/images/ups-tower-diagram.png" alt="UPS Tower Architecture" width="600">
</p>

A Python-based UPS monitoring daemon that watches UPS status via [Network UPS Tools (NUT)](https://networkupstools.org/) and executes configurable shutdown sequences to protect your entire infrastructure during power events.

[Features](#features) •
[Installation](#Installation) •
[Configuration](#configuration) •
[Usage](#usage) •
[Troubleshooting](#troubleshooting) •
[Changelog](CHANGELOG.md)

</div>

---

## ✨ Why UPS Tower?

Most UPS shutdown solutions are **single-system focused**. UPS Tower is designed for **modern infrastructure**:

| Challenge | UPS Tower Solution |
|-----------|-------------------|
| Multiple servers need coordinated shutdown | ✅ Orchestrated multi-server shutdown via SSH |
| VMs and containers need graceful stop | ✅ Libvirt VM and Docker container handling |
| Network mounts hang during power loss | ✅ Timeout-protected unmounting |
| No visibility during power events | ✅ Real-time Discord notifications |
| Different systems need different commands | ✅ Per-server custom shutdown commands |
| Battery estimates are unreliable | ✅ Multi-vector shutdown triggers |

---

## 🎯 Built For

- 🏠 **Homelabs** - Protect your self-hosted infrastructure
- 🖥️ **Virtualization Hosts** - Graceful VM shutdown before power loss
- 🐳 **Container Hosts** - Stop Docker/Podman containers safely
- 📦 **NAS Systems** - Coordinate shutdown of Synology, QNAP, TrueNAS
- 🏢 **Small Business** - Multi-server environments with single UPS
- ☁️ **Hybrid Setups** - Mix of physical and virtual infrastructure

---

## Features

### High-Performance Monitoring
- **Optimized Polling:** Fetches all UPS metrics in a single network call with configurable intervals
- **Robust Error Handling:** Comprehensive input validation prevents failures from corrupted or transient data
- **Atomic State Updates:** Uses atomic file operations for data integrity
- **Connection Resilience:** Automatic recovery from network issues with stale data detection

### Intelligent Shutdown Triggers
Multiple shutdown conditions with configurable thresholds:

1. **FSD Flag:** UPS signals forced shutdown (highest priority)
2. **Critical Battery Level:** Battery percentage below threshold (default: 20%)
3. **Critical Runtime:** Estimated runtime below threshold (default: 10 minutes)
4. **Dangerous Depletion Rate:** Battery draining faster than threshold (default: 15%/min)
5. **Extended Time on Battery:** Safety net for aged batteries (default: 15 minutes)
6. **Failsafe (FSB):** Connection lost while on battery triggers immediate shutdown

### Configurable Shutdown Sequence
All components are optional and independently configurable:

1. **Virtual Machines (libvirt/KVM):** Graceful shutdown with force-destroy fallback
2. **Docker Containers:** Stop all running containers
3. **Filesystem Sync:** Flush buffers to disk
4. **Filesystem Unmount:** Hang-proof unmounting with per-mount options
5. **Remote Servers:** SSH-based shutdown of multiple remote systems
6. **Local Shutdown:** Configurable shutdown command

### Real-Time Notifications
- **Discord Webhooks:** Color-coded notifications for all power events
- **Crisis Reporting:** Elevated notifications during shutdown sequence
- **Service Lifecycle:** Notifications when service starts/stops

### Power Quality Monitoring
- **Voltage Monitoring:** Brownout and over-voltage detection
- **AVR Tracking:** Boost/Trim mode detection
- **Bypass Detection:** Alerts when UPS protection is inactive
- **Overload Detection:** Load threshold monitoring

---

## Installation

### Prerequisites

- Python 3.9 or higher
- NUT (Network UPS Tools) client
- SSH client (for remote server shutdown)
- Root privileges

### Quick Install

```bash
# Clone or download the repository
git clone https://github.com/m4r1k/Eneru.git
cd Eneru

# Run the installer
sudo ./install.sh
```

### Manual Installation

```bash
# Create directories
sudo mkdir -p /opt/ups-monitor
sudo mkdir -p /etc/ups-monitor

# Copy files
sudo cp ups-monitor.py /opt/ups-monitor/
sudo cp config.yaml /etc/ups-monitor/
sudo cp ups-monitor.service /etc/systemd/system/

# Make executable
sudo chmod +x /opt/ups-monitor/ups-monitor.py

# Install dependencies (RHEL/Fedora)
sudo dnf install -y python3 python3-pyyaml python3-requests nut-client openssh-clients

# Install dependencies (Debian/Ubuntu)
sudo apt install -y python3 python3-yaml python3-requests nut-client openssh-client

# Enable and start
sudo systemctl daemon-reload
sudo systemctl enable --now ups-monitor.service
```

---

## Configuration

All configuration is stored in `/etc/ups-monitor/config.yaml`. Features are disabled by removing their section or setting `enabled: false`.

### Minimal Configuration

```yaml
# Minimal config - just UPS monitoring with local shutdown
ups:
  name: "UPS@192.168.1.100"

triggers:
  low_battery_threshold: 20
  critical_runtime_threshold: 600

local_shutdown:
  enabled: true
```

### Full Configuration Example

```yaml
# UPS Monitor Configuration File
# All sections are optional - features are disabled if not configured

# ==============================================================================
# UPS CONNECTION
# ==============================================================================
ups:
  name: "UPS@192.168.178.11"
  check_interval: 1
  max_stale_data_tolerance: 3

# ==============================================================================
# SHUTDOWN TRIGGERS
# ==============================================================================
triggers:
  low_battery_threshold: 20
  critical_runtime_threshold: 600
  
  depletion:
    window: 300
    critical_rate: 15.0
    grace_period: 90
  
  extended_time:
    enabled: true
    threshold: 900

# ==============================================================================
# BEHAVIOR
# ==============================================================================
behavior:
  dry_run: false

# ==============================================================================
# LOGGING
# ==============================================================================
logging:
  file: "/var/log/ups-monitor.log"
  state_file: "/var/run/ups-monitor.state"
  battery_history_file: "/var/run/ups-battery-history"

# ==============================================================================
# NOTIFICATIONS
# ==============================================================================
notifications:
  discord:
    webhook_url: "https://discord.com/api/webhooks/YOUR_WEBHOOK_HERE"
    timeout: 3
    timeout_blocking: 10

# ==============================================================================
# SHUTDOWN SEQUENCE
# ==============================================================================

# Virtual Machines (libvirt/KVM)
virtual_machines:
  enabled: true
  max_wait: 30

# Docker Containers
docker:
  enabled: true
  stop_timeout: 60

# Filesystem Operations
filesystems:
  sync_enabled: true
  unmount:
    enabled: true
    timeout: 15
    mounts:
      - "/mnt/media"
      - path: "/mnt/nas"
        options: "-l"
      - "/mnt/backup"

# Remote Server Shutdown
remote_servers:
  - name: "Synology NAS"
    enabled: true
    host: "192.168.178.229"
    user: "nas-admin"
    connect_timeout: 10
    command_timeout: 30
    shutdown_command: "sudo -i synoshutdown -s"
    ssh_options:
      - "-o StrictHostKeyChecking=no"
  
  - name: "Backup Server"
    enabled: false
    host: "192.168.178.230"
    user: "admin"
    shutdown_command: "sudo shutdown -h now"

# Local Server Shutdown
local_shutdown:
  enabled: true
  command: "shutdown -h now"
  message: "UPS battery critical - emergency shutdown"
```

### Configuration Reference

#### UPS Section
| Key | Default | Description |
|-----|---------|-------------|
| `name` | `UPS@localhost` | UPS identifier (NAME@HOST format) |
| `check_interval` | `1` | Seconds between status checks |
| `max_stale_data_tolerance` | `3` | Stale data attempts before connection lost |

#### Triggers Section
| Key | Default | Description |
|-----|---------|-------------|
| `low_battery_threshold` | `20` | Battery % for immediate shutdown |
| `critical_runtime_threshold` | `600` | Runtime seconds for shutdown |
| `depletion.window` | `300` | Seconds for depletion calculation |
| `depletion.critical_rate` | `15.0` | %/minute threshold |
| `depletion.grace_period` | `90` | Seconds before enforcing depletion rate |
| `extended_time.enabled` | `true` | Enable extended time shutdown |
| `extended_time.threshold` | `900` | Seconds on battery before shutdown |

#### Remote Servers
| Key | Default | Description |
|-----|---------|-------------|
| `name` | (required) | Display name for logging |
| `enabled` | `false` | Enable this server |
| `host` | (required) | Hostname or IP address |
| `user` | (required) | SSH username |
| `connect_timeout` | `10` | SSH connection timeout |
| `command_timeout` | `30` | Command execution timeout |
| `shutdown_command` | `sudo shutdown -h now` | Command to execute |
| `ssh_options` | `[]` | Additional SSH options |

---

## Remote Server Setup

For secure remote shutdown, configure SSH key authentication and passwordless sudo.

### 1. Generate SSH Key

```bash
# As root on the monitoring server
sudo su
ssh-keygen -t ed25519 -f ~/.ssh/id_ups_shutdown -C "ups-monitor@$(hostname)"
ssh-copy-id -i ~/.ssh/id_ups_shutdown.pub user@remote-server
```

### 2. Configure Passwordless Sudo

On the remote server, create a sudoers rule:

```bash
# For standard Linux servers
echo "username ALL=(ALL) NOPASSWD: /sbin/shutdown" | sudo tee /etc/sudoers.d/ups_shutdown
sudo chmod 0440 /etc/sudoers.d/ups_shutdown

# For Synology NAS
echo "username ALL=(ALL) NOPASSWD: /usr/syno/sbin/synoshutdown -s" | sudo tee /etc/sudoers.d/ups_shutdown
sudo chmod 0440 /etc/sudoers.d/ups_shutdown
```

### 3. Test Connection

```bash
# Should execute without password prompts
sudo ssh user@remote-server "sudo shutdown -h now"  # CAUTION: Actually shuts down!
```

### Common Shutdown Commands

| System | Command |
|--------|---------|
| Standard Linux | `sudo shutdown -h now` |
| Synology DSM | `sudo -i synoshutdown -s` |
| QNAP QTS | `sudo /sbin/poweroff` |
| TrueNAS | `sudo shutdown -p now` |
| ESXi | `sudo /bin/halt` |

---

## Usage

### Service Management

```bash
# Start/stop/restart
sudo systemctl start ups-monitor.service
sudo systemctl stop ups-monitor.service
sudo systemctl restart ups-monitor.service

# Check status
sudo systemctl status ups-monitor.service

# View logs
sudo journalctl -u ups-monitor.service -f
sudo tail -f /var/log/ups-monitor.log
```

### Command Line Options

```bash
# Validate configuration
python3 /opt/ups-monitor/ups-monitor.py --validate-config

# Use alternate config file
python3 /opt/ups-monitor/ups-monitor.py --config /path/to/config.yaml

# Force dry-run mode
python3 /opt/ups-monitor/ups-monitor.py --dry-run
```

### Testing with Dry-Run Mode

Always test with dry-run mode before production deployment:

1. Set `behavior.dry_run: true` in config, or use `--dry-run` flag
2. Optionally lower `extended_time.threshold` to trigger shutdown faster
3. Simulate power failure (unplug UPS input)
4. Watch logs for `[DRY-RUN]` prefixed actions
5. Verify Discord notifications arrive correctly

```bash
# Quick dry-run test
sudo systemctl stop ups-monitor.service
sudo python3 /opt/ups-monitor/ups-monitor.py --dry-run --config /etc/ups-monitor/config.yaml
```

### Manually Clear Shutdown State

If a shutdown sequence is interrupted:

```bash
sudo rm -f /var/run/ups-shutdown-scheduled
```

---

## Troubleshooting

### Service Won't Start

```bash
# Check for errors
journalctl -u ups-monitor.service -e

# Validate Python version (must be 3.9+)
python3 --version

# Check dependencies
python3 -c "import yaml; import requests; print('OK')"

# Validate syntax
python3 -m py_compile /opt/ups-monitor/ups-monitor.py
```

### Cannot Connect to UPS

```bash
# Test NUT connection
upsc UPS@192.168.178.11

# Check NUT server is running
systemctl status nut-server

# Verify network connectivity
ping 192.168.178.11
```

### Discord Notifications Not Working

```bash
# Test webhook manually
curl -H "Content-Type: application/json" \
     -d '{"content": "Test"}' \
     "YOUR_WEBHOOK_URL"

# Check Python can reach Discord
python3 -c "import requests; print(requests.get('https://discord.com').status_code)"
```

### Remote Shutdown Fails

```bash
# Test SSH connection as root
sudo ssh user@remote-server "echo OK"

# Test sudo access
sudo ssh user@remote-server "sudo -n true && echo 'sudo OK'"

# Check SSH key permissions
ls -la ~/.ssh/id_*
```

---

## File Locations

| File | Purpose |
|------|---------|
| `/opt/ups-monitor/ups-monitor.py` | Main script |
| `/etc/ups-monitor/config.yaml` | Configuration file |
| `/etc/systemd/system/ups-monitor.service` | Systemd service |
| `/var/log/ups-monitor.log` | Log file |
| `/var/run/ups-monitor.state` | Current UPS state |
| `/var/run/ups-shutdown-scheduled` | Shutdown in progress flag |
| `/var/run/ups-battery-history` | Battery depletion history |

---

## Security Considerations

### SSH Host Key Verification

The example config includes `-o StrictHostKeyChecking=no` for convenience. For production:

1. Manually SSH to each remote server once to accept the host key
2. Remove `StrictHostKeyChecking=no` from `ssh_options`

```bash
# Accept host key once
sudo ssh user@remote-server
# Type 'yes' when prompted
```

### Running as Root

The service requires root for:
- System shutdown commands
- VM management (virsh)
- Docker management
- Filesystem unmounting
- SSH key access

The systemd service includes basic hardening:
- `NoNewPrivileges=true`
- `PrivateTmp=true`

---

## License

MIT License - See LICENSE file for details.
