<div align="center">

# ⚡ Eneru

**Intelligent UPS Monitoring & Shutdown Orchestration for NUT**

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.9+](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/downloads/)
[![NUT Compatible](https://img.shields.io/badge/NUT-compatible-green.svg)](https://networkupstools.org/)
[![codecov](https://codecov.io/gh/m4r1k/Eneru/branch/main/graph/badge.svg)](https://codecov.io/gh/m4r1k/Eneru)

<p align="center">
  <img src="docs/images/eneru-diagram.png" alt="Eneru Architecture" width="600">
</p>

A Python-based UPS monitoring daemon that watches UPS status via [Network UPS Tools (NUT)](https://networkupstools.org/) and executes configurable shutdown sequences to protect your entire infrastructure during power events.

[Features](#features) •
[Installation](#installation) •
[Configuration](#configuration) •
[Shutdown Triggers](#shutdown-triggers-explained) •
[Usage](#usage) •
[Troubleshooting](#troubleshooting) •
[Changelog](CHANGELOG.md)

---

### ⚡ The Name

<img src="docs/images/eneru.jpg" alt="Eneru from One Piece" width="120" align="right">

Named after [Eneru (エネル)](https://onepiece.fandom.com/wiki/Enel) from *One Piece*—the self-proclaimed God of Skypiea who ate the Goro Goro no Mi (Rumble-Rumble Fruit), granting him absolute control over electricity. Just as Eneru commands lightning from the sky, this tool commands your infrastructure when the power from the grid fails. *Unlimited power... management!* ⚡

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
| Battery estimates are unreliable | ✅ Multi-vector shutdown triggers |
| Network down during outage might block/slow down shutdown | ✅ Non-blocking notification architecture |

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

See [Shutdown Triggers Explained](#shutdown-triggers-explained) for detailed documentation.

### Configurable Shutdown Sequence
All components are optional and independently configurable:

1. **Virtual Machines (libvirt/KVM):** Graceful shutdown with force-destroy fallback
2. **Containers (Docker/Podman):** Stop all running containers with auto-detection
3. **Filesystem Sync:** Flush buffers to disk
4. **Filesystem Unmount:** Hang-proof unmounting with per-mount options
5. **Remote Servers:** SSH-based shutdown of multiple remote systems
6. **Local Shutdown:** Configurable shutdown command

### Real-Time Notifications (via Apprise)
- **100+ Notification Services:** Discord, Slack, Telegram, ntfy, Pushover, Email, and [many more](https://github.com/caronc/apprise/wiki)
- **Non-Blocking Architecture:** Notifications never delay critical shutdown operations
- **Power Event Alerts:** Color-coded notifications for all power events
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

### Package Installation (Recommended)

Native `.deb` and `.rpm` packages are available for easy installation.

#### Option 1: APT/DNF Repository (Recommended)

Add the repository to get automatic updates:

**Debian/Ubuntu:**
```bash
# Import GPG key
curl -fsSL https://m4r1k.github.io/Eneru/KEY.gpg | sudo gpg --dearmor -o /usr/share/keyrings/eneru.gpg

# Add repository
echo "deb [arch=all signed-by=/usr/share/keyrings/eneru.gpg] https://m4r1k.github.io/Eneru/deb stable main" | sudo tee /etc/apt/sources.list.d/eneru.list

# Install
sudo apt update
sudo apt install eneru
```

**RHEL/Fedora:**
```bash
# RHEL 8/9: Enable EPEL first (required for apprise dependency)
sudo dnf install -y epel-release

# Add repository
sudo curl -o /etc/yum.repos.d/eneru.repo https://m4r1k.github.io/Eneru/rpm/eneru.repo

# Install
sudo dnf install eneru
```

#### Option 2: Direct Download from GitHub Releases

Download the latest package from [GitHub Releases](https://github.com/m4r1k/Eneru/releases):

**Debian/Ubuntu:**
```bash
sudo dpkg -i eneru_4.3.0_all.deb
sudo apt install -f  # Install dependencies if needed
```

**RHEL/Fedora:**
```bash
# RHEL 8/9: Enable EPEL first (required for apprise dependency)
sudo dnf install -y epel-release

sudo dnf install ./eneru-4.3.0.noarch.rpm
```

#### After Installation

The package installs but does **not** auto-enable or auto-start the service (configuration must be completed first):

```bash
# 1. Edit configuration
sudo nano /etc/ups-monitor/config.yaml

# 2. Validate configuration
sudo python3 /opt/ups-monitor/ups_monitor.py --validate-config

# 3. Enable and start the service
sudo systemctl enable eneru.service
sudo systemctl start eneru.service
```

### Manual Installation

For development or systems without package manager support:

```bash
# Clone or download the repository
git clone https://github.com/m4r1k/Eneru.git
cd Eneru

# Run the installer
sudo ./install.sh
```

Or install manually:

```bash
# Create directories
sudo mkdir -p /opt/ups-monitor
sudo mkdir -p /etc/ups-monitor

# Copy files
sudo cp ups_monitor.py /opt/ups-monitor/
sudo cp config.yaml /etc/ups-monitor/
sudo cp eneru.service /etc/systemd/system/

# Make executable
sudo chmod +x /opt/ups-monitor/ups_monitor.py

# Install dependencies (RHEL/Fedora - EPEL required for apprise)
sudo dnf install -y epel-release
sudo dnf install -y python3 python3-pyyaml apprise nut-client openssh-clients

# Install dependencies (Debian/Ubuntu)
sudo apt install -y python3 python3-yaml apprise nut-client openssh-client

# Reload systemd
sudo systemctl daemon-reload
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
# NOTIFICATIONS (via Apprise)
# ==============================================================================
# Apprise supports 100+ notification services
# See: https://github.com/caronc/apprise/wiki
notifications:
  # Optional title for notifications
  title: "⚡ Homelab UPS"

  # Avatar/icon URL (supported by Discord, Slack, and others)
  avatar_url: "https://raw.githubusercontent.com/m4r1k/Eneru/main/docs/images/eneru-avatar.png"

  # Timeout for notification delivery (seconds)
  timeout: 10

  # Notification service URLs
  urls:
    # Discord
    - "discord://webhook_id/webhook_token"

    # Slack
    # - "slack://token_a/token_b/token_c/#channel"

    # Telegram
    # - "telegram://bot_token/chat_id"

# ==============================================================================
# SHUTDOWN SEQUENCE
# ==============================================================================

# Virtual Machines (libvirt/KVM)
virtual_machines:
  enabled: true
  max_wait: 30

# Container Runtime (Docker/Podman)
containers:
  enabled: true
  # Runtime to use: "auto", "docker", or "podman"
  # auto = detect available runtime (prefers podman)
  runtime: "auto"
  stop_timeout: 60
  # For Podman: stop rootless user containers as well
  include_user_containers: false

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

#### Notifications Section
| Key | Default | Description |
|-----|---------|-------------|
| `title` | `null` | Optional title for notifications |
| `avatar_url` | `null` | Avatar URL for supported services |
| `timeout` | `10` | Notification delivery timeout |
| `urls` | `[]` | List of Apprise notification URLs |

#### Containers Section
| Key | Default | Description |
|-----|---------|-------------|
| `enabled` | `false` | Enable container shutdown |
| `runtime` | `auto` | Runtime: `auto`, `docker`, or `podman` |
| `stop_timeout` | `60` | Seconds to wait for graceful stop |
| `include_user_containers` | `false` | Podman only: stop rootless user containers |

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

## Shutdown Triggers Explained

Eneru uses multiple independent triggers to decide when to initiate an emergency shutdown. This multi-vector approach ensures protection even when individual metrics are unreliable (e.g., aged batteries with inaccurate runtime estimates).

### Trigger Priority

When on battery power, triggers are evaluated in this order. The **first** condition met initiates shutdown:

| Priority | Trigger | Default | Purpose |
|----------|---------|---------|---------|
| 1 | FSD Flag | N/A | UPS signals forced shutdown |
| 2 | Low Battery | 20% | Battery percentage critically low |
| 3 | Critical Runtime | 10 min | Estimated runtime too short |
| 4 | Depletion Rate | 15%/min | Battery draining dangerously fast |
| 5 | Extended Time | 15 min | Safety net for prolonged outages |

Each trigger serves a specific purpose and catches different failure scenarios.

---

### Low Battery Threshold

```yaml
triggers:
  low_battery_threshold: 20  # percentage
```

**What it does:** Triggers shutdown when battery charge falls below the configured percentage.

**When it helps:**
- Simple, reliable metric available on all UPS devices
- Works when runtime estimates are unavailable or inaccurate
- Provides a hard floor regardless of load conditions

**Example:** With threshold at 20%, shutdown triggers when battery reports 19% or lower.

---

### Critical Runtime Threshold

```yaml
triggers:
  critical_runtime_threshold: 600  # seconds (10 minutes)
```

**What it does:** Triggers shutdown when the UPS-estimated remaining runtime falls below the configured value.

**When it helps:**
- Accounts for current load conditions
- UPS calculates runtime based on actual power draw
- More accurate than battery percentage alone under varying loads

**How runtime is calculated by the UPS:**

The UPS continuously measures current battery capacity, power draw (load), and battery voltage curve to estimate: *"At this load, the battery will last X more seconds."*

**Example scenario:**
```
Battery: 50%
Load: 80% (high)
UPS Runtime Estimate: 8 minutes

Even though battery shows 50%, high load means only 8 minutes remain.
With threshold at 10 minutes (600s), shutdown triggers.
```

**Limitations:**
- Runtime estimates can be inaccurate, especially with aged batteries
- Some UPS models provide unreliable estimates
- Sudden load changes can cause estimate jumps

This is why multiple triggers exist—they compensate for each other's weaknesses.

---

### Depletion Rate

```yaml
triggers:
  depletion:
    window: 300         # seconds (5 minutes)
    critical_rate: 15.0 # percent per minute
    grace_period: 90    # seconds
```

The depletion rate measures **how fast the battery is actually draining** based on observed data, independent of UPS estimates.

#### How Depletion Rate is Calculated

The script maintains a rolling history of battery readings within the configured window (default: 5 minutes).

**Step 1: Collect Data**

Every check cycle (default: 1 second), the current battery percentage and timestamp are recorded:

```
History Buffer (last 5 minutes):
┌──────────────┬───────────┐
│ Time         │ Battery   │
├──────────────┼───────────┤
│ 5 min ago    │ 85%       │ ← Oldest reading
│ 4 min ago    │ 82%       │
│ 3 min ago    │ 79%       │
│ 2 min ago    │ 76%       │
│ 1 min ago    │ 73%       │
│ Now          │ 70%       │ ← Current reading
└──────────────┴───────────┘
```

**Step 2: Calculate Rate**

Compare the oldest reading to the current reading:

```
Battery difference = 85% - 70% = 15%
Time difference    = 5 minutes
Depletion rate     = 15% ÷ 5 min = 3%/min
```

**Step 3: Evaluate**

If rate exceeds threshold (default: 15%/min) and grace period has passed, trigger shutdown.

#### Minimum Data Requirement

The script requires at least 30 readings before calculating a rate. With 1-second intervals, this means 30 seconds of data minimum. This prevents:

- Single bad readings from skewing results
- Startup false positives
- Statistical noise in short samples

#### The Grace Period

**Problem:** When power fails, battery readings are often unstable for the first 30-90 seconds as the UPS recalibrates:

```
Time 0s:   Power fails
Time 1s:   Battery reads 100%
Time 2s:   Battery reads 95%   ← Sudden drop (recalibrating)
Time 5s:   Battery reads 91%   ← Still adjusting
Time 10s:  Battery reads 94%   ← Bouncing back
Time 30s:  Battery reads 93%   ← Stabilizing
Time 90s:  Battery reads 91%   ← Reliable now
```

Without a grace period, the initial 100% → 91% drop in 10 seconds would calculate as **54%/min**—triggering a false shutdown.

**Solution:** The grace period (default: 90 seconds) ignores high depletion rates immediately after power loss:

```
Timeline with 90s grace period:

Time     On Battery   Rate        Action
──────────────────────────────────────────────
0s       0s           N/A         Power lost
10s      10s          54%/min     Ignored (grace period)
30s      30s          28%/min     Ignored (grace period)
60s      60s          16%/min     Ignored (grace period)
90s      90s          12%/min     Evaluated → OK (below 15%)
120s     120s         18%/min     SHUTDOWN TRIGGERED
```

#### When Depletion Rate Helps

- **Aged batteries:** Old batteries may show 50% charge but drain to 0% in minutes
- **Inaccurate UPS estimates:** Some UPS models have unreliable runtime calculations
- **Sudden load increases:** Catches scenarios where load spikes mid-outage
- **Real-world validation:** Uses observed data rather than UPS predictions

---

### Extended Time on Battery

```yaml
triggers:
  extended_time:
    enabled: true
    threshold: 900  # seconds (15 minutes)
```

**What it does:** Triggers shutdown after the system has been running on battery for the configured duration, regardless of battery level or runtime estimates.

**When it helps:**
- **Ultimate safety net:** Even if battery shows 80% after 15 minutes, something may be wrong
- **Aged battery protection:** Old batteries can suddenly fail after appearing stable
- **UPS malfunction detection:** Catches scenarios where UPS reports incorrect data
- **Prolonged outage protection:** Ensures graceful shutdown before potential battery failure

**Example scenarios:**

*Scenario 1: Reliable data, extended outage*
```
Power out for 15 minutes
Battery: 45%
Runtime estimate: 20 minutes
Depletion rate: 3%/min

All metrics look fine, but extended time threshold reached.
Shutdown triggered—better safe than sorry.
```

*Scenario 2: Unreliable UPS data*
```
Power out for 15 minutes
Battery: 75% (stuck/not updating)
Runtime estimate: 45 minutes (clearly wrong)
Depletion rate: 0%/min (no change detected)

Something is wrong with UPS reporting.
Extended time safety net catches this and triggers shutdown.
```

#### Disabling Extended Time

For environments where long outages are expected and battery capacity is sufficient:

```yaml
triggers:
  extended_time:
    enabled: false
```

When disabled, the script logs when the threshold is exceeded but does not trigger shutdown.

---

### Critical Runtime vs Extended Time

These triggers serve **different purposes** and complement each other:

| Trigger | Based On | Catches |
|---------|----------|---------|
| Critical Runtime | UPS estimate | High load draining battery fast |
| Extended Time | Wall clock | Prolonged outage, unreliable UPS data |

**Example: Low load, long outage**
```
Runtime estimate: 2 hours (high—low load)
Actual time on battery: 20 minutes

Critical runtime won't trigger (estimate is high).
Extended time triggers at 15 minutes—safety net works.
```

**Example: High load, short outage**
```
Runtime estimate: 5 minutes (low—high load)
Actual time on battery: 3 minutes

Critical runtime triggers at 10-minute threshold.
Extended time never reached—faster trigger caught it.
```

---

### Failsafe Battery Protection (FSB)

Beyond the configured triggers, Eneru includes a hardcoded failsafe:

**If connection to the UPS is lost while running on battery, immediate shutdown is triggered.**

This catches:
- NUT server crash during outage
- Network failure to remote NUT server
- USB cable disconnect
- UPS communication failure

**The logic:** If we were on battery and suddenly can't confirm UPS status, assume the worst and shut down safely.

```
Timeline:
1. Power fails, system on battery (OB status)
2. UPS connection lost (network issue, NUT crash, etc.)
3. Script detects stale/missing data
4. FSB triggers: "We were on battery and lost visibility—shut down NOW"
```

---

### FSD (Forced Shutdown) Flag

The highest priority trigger. When the UPS itself signals FSD, shutdown is immediate.

**What causes FSD:**
- UPS battery critically low (UPS-determined)
- UPS commanding connected systems to shut down
- UPS about to cut power

**Why it's highest priority:** The UPS has direct knowledge of its state and may cut power imminently. All other triggers defer to FSD.

---

### Why Multiple Triggers?

Each trigger catches scenarios the others might miss:

| Scenario | Low Battery | Runtime | Depletion | Extended |
|----------|:-----------:|:-------:|:---------:|:--------:|
| Normal discharge | ✓ | ✓ | ✓ | ✓ |
| Aged battery (sudden failure) | ✗ | ✗ | ✓ | ✓ |
| UPS reporting stuck values | ✗ | ✗ | ✗ | ✓ |
| High load spike | ✓ | ✓ | ✓ | ✗ |
| Inaccurate runtime estimate | ✓ | ✗ | ✓ | ✓ |
| Very slow discharge | ✓ | ✓ | ✗ | ✓ |

✓ = Would catch this scenario | ✗ = Might miss this scenario

---

### Trigger Evaluation Flow

```
                    ┌─────────────────────┐
                    │  On Battery Power   │
                    └──────────┬──────────┘
                               │
                               ▼
                    ┌─────────────────────┐
                    │  FSD Flag Set?      │───Yes───▶ SHUTDOWN
                    └──────────┬──────────┘
                               │ No
                               ▼
                    ┌─────────────────────┐
                    │  Battery < 20%?     │───Yes───▶ SHUTDOWN
                    └──────────┬──────────┘
                               │ No
                               ▼
                    ┌─────────────────────┐
                    │  Runtime < 10min?   │───Yes───▶ SHUTDOWN
                    └──────────┬──────────┘
                               │ No
                               ▼
                    ┌─────────────────────┐
                    │  Depletion > 15%/m? │───Yes───┐
                    └──────────┬──────────┘         │
                               │ No                 ▼
                               │          ┌─────────────────────┐
                               │          │  Grace Period Over? │──No──▶ Log & Continue
                               │          └──────────┬──────────┘
                               │                     │ Yes
                               │                     ▼
                               │                  SHUTDOWN
                               ▼
                    ┌─────────────────────┐
                    │  On Battery > 15m?  │───Yes───┐
                    └──────────┬──────────┘         │
                               │ No                 ▼
                               │          ┌─────────────────────┐
                               │          │  Extended Enabled?  │──No──▶ Log & Continue
                               │          └──────────┬──────────┘
                               │                     │ Yes
                               │                     ▼
                               │                  SHUTDOWN
                               ▼
                    ┌─────────────────────┐
                    │  Continue Monitoring│
                    │  (check again in 1s)│
                    └─────────────────────┘
```

---

### Recommended Configurations

#### Conservative (Maximum Protection)

```yaml
triggers:
  low_battery_threshold: 30
  critical_runtime_threshold: 900  # 15 minutes
  depletion:
    window: 300
    critical_rate: 10.0
    grace_period: 90
  extended_time:
    enabled: true
    threshold: 600  # 10 minutes
```

Shuts down early, prioritizes data safety over runtime.

#### Balanced (Default)

```yaml
triggers:
  low_battery_threshold: 20
  critical_runtime_threshold: 600  # 10 minutes
  depletion:
    window: 300
    critical_rate: 15.0
    grace_period: 90
  extended_time:
    enabled: true
    threshold: 900  # 15 minutes
```

Good balance between protection and avoiding unnecessary shutdowns.

#### Aggressive (Maximum Runtime)

```yaml
triggers:
  low_battery_threshold: 10
  critical_runtime_threshold: 300  # 5 minutes
  depletion:
    window: 300
    critical_rate: 20.0
    grace_period: 120
  extended_time:
    enabled: false
```

Maximizes runtime, accepts higher risk. Only recommended with reliable UPS and new batteries.

---

## Non-Blocking Notification Architecture

During power outages, network connectivity is often unreliable or completely unavailable. Eneru uses a **fire-and-forget** notification system that ensures shutdown operations are never delayed by notification failures.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                     ENERU NOTIFICATION ARCHITECTURE                         │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│   MAIN THREAD (Critical Path)          WORKER THREAD (Best-Effort)          │
│   ═══════════════════════════          ════════════════════════════         │
│                                                                             │
│   ┌─────────────────────┐                                                   │
│   │ Shutdown Triggered  │                                                   │
│   └──────────┬──────────┘                                                   │
│              │                                                              │
│              ▼                                                              │
│   ┌─────────────────────┐         ┌─────────────────────┐                   │
│   │ Queue Notification  │────────▶│  Notification Queue │                   │
│   │ (non-blocking)      │         │  ┌───┬───┬───┬───┐  │                   │
│   └──────────┬──────────┘         │  │ N │ N │ N │...│  │                   │
│              │                    │  └───┴───┴───┴───┘  │                   │
│              │ continues          └──────────┬──────────┘                   │
│              │ immediately                   │                              │
│              ▼                               ▼                              │
│   ┌─────────────────────┐         ┌─────────────────────┐                   │
│   │ Stop VMs            │         │ Send to Discord     │──▶ Success/Fail   │
│   └──────────┬──────────┘         │ Send to Slack       │──▶ Success/Fail   │
│              │                    │ Send to Telegram    │──▶ Success/Fail   │
│              ▼                    └─────────────────────┘                   │
│   ┌─────────────────────┐                   │                               │
│   │ Stop Containers     │                   │ Network down?                 │
│   └──────────┬──────────┘                   │ Timeout? No problem!          │
│              │                              │ Worker handles it silently    │
│              ▼                              ▼                               │
│   ┌─────────────────────┐         ┌─────────────────────┐                   │
│   │ Unmount Filesystems │         │ Thread terminates   │                   │
│   └──────────┬──────────┘         │ with process exit   │                   │
│              │                    └─────────────────────┘                   │
│              ▼                                                              │
│   ┌─────────────────────┐                                                   │
│   │ Shutdown Remote     │         ┌─────────────────────┐                   │
│   │ Servers             │         │    KEY BENEFITS     │                   │
│   └──────────┬──────────┘         ├─────────────────────┤                   │
│              │                    │ ✓ Zero blocking     │                   │
│              ▼                    │ ✓ Graceful failure  │                   │
│   ┌─────────────────────┐         │ ✓ Best-effort send  │                   │
│   │ 5-Second Grace      │         │ ✓ No data loss risk │                   │
│   │ (flush queue)       │         │ ✓ Daemon thread     │                   │
│   └──────────┬──────────┘         └─────────────────────┘                   │
│              │                                                              │
│              ▼                                                              │
│   ┌─────────────────────┐                                                   │
│   │ shutdown -h now     │                                                   │
│   └─────────────────────┘                                                   │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Why Non-Blocking Matters

| Scenario | Blocking Notifications | Eneru (Non-Blocking) |
|----------|------------------------|----------------------|
| Network down during outage | ❌ Shutdown delayed by timeout (10-30s per notification) | ✅ Shutdown proceeds immediately |
| Discord rate-limited | ❌ Waits for retry | ✅ Continues without waiting |
| DNS resolution fails | ❌ Hangs until timeout | ✅ Worker handles silently |
| Multiple notification services | ❌ Sequential delays compound | ✅ All queued instantly |
| Power about to fail | ❌ Risk of incomplete shutdown | ✅ Critical operations prioritized |

### The 5-Second Grace Period

After all critical shutdown operations complete, Eneru waits 5 seconds before issuing the final `shutdown -h now` command. This grace period allows queued notifications to be sent if the network is available, without risking data loss if it's not.

```
Timeline (worst case - network down):
─────────────────────────────────────────────────────────────────
0s     │ Shutdown triggered, notification queued (instant)
0.1s   │ VMs stopping...
15s    │ VMs stopped, containers stopping...
30s    │ Containers stopped, filesystems synced...
45s    │ Remote servers notified...
50s    │ All critical operations complete
50-55s │ Grace period (notifications attempted)
55s    │ shutdown -h now executed
─────────────────────────────────────────────────────────────────
        Total: ~55 seconds (network issues added 0 seconds delay)
```

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
sudo systemctl start eneru.service
sudo systemctl stop eneru.service
sudo systemctl restart eneru.service

# Check status
sudo systemctl status eneru.service

# View logs
sudo journalctl -u eneru.service -f
sudo tail -f /var/log/ups-monitor.log
```

### Command Line Options

```bash
# Validate configuration
python3 /opt/ups-monitor/ups_monitor.py --validate-config

# Test notifications
python3 /opt/ups-monitor/ups_monitor.py --test-notifications

# Use alternate config file
python3 /opt/ups-monitor/ups_monitor.py --config /path/to/config.yaml

# Force dry-run mode
python3 /opt/ups-monitor/ups_monitor.py --dry-run
```

### Testing with Dry-Run Mode

Always test with dry-run mode before production deployment:

1. Set `behavior.dry_run: true` in config, or use `--dry-run` flag
2. Optionally lower `extended_time.threshold` to trigger shutdown faster
3. Simulate power failure (unplug UPS input)
4. Watch logs for `[DRY-RUN]` prefixed actions
5. Verify notifications arrive correctly

```bash
# Quick dry-run test
sudo systemctl stop eneru.service
sudo python3 /opt/ups-monitor/ups_monitor.py --dry-run --config /etc/ups-monitor/config.yaml
```

### Testing Notifications

```bash
# Send a test notification to verify configuration
sudo python3 /opt/ups-monitor/ups_monitor.py --test-notifications

# Combine with config validation
sudo python3 /opt/ups-monitor/ups_monitor.py --validate-config --test-notifications
```

### Running Tests

Eneru includes a comprehensive test suite. To run tests locally:

```bash
# Install test dependencies
dnf install python3-pytest python3-pytest-cov python3-pytest-mock python3-pytest-timeout python3-pyyaml python3-apprise

# Run all tests
pytest -v

# Run with coverage report
pytest --cov=ups_monitor --cov-report=term -v

# Run only unit tests (fast)
pytest -m unit -v

# Run only integration tests
pytest -m integration -v

# Generate HTML coverage report
pytest --cov=ups_monitor --cov-report=html
# Open htmlcov/index.html in browser
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
journalctl -u eneru.service -e

# Validate Python version (must be 3.9+)
python3 --version

# Check dependencies
python3 -c "import yaml; print('PyYAML OK')"
python3 -c "import apprise; print('Apprise OK')"

# Validate syntax
python3 -m py_compile /opt/ups-monitor/ups_monitor.py
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

### Notifications Not Working

```bash
# Test with built-in command
python3 /opt/ups-monitor/ups_monitor.py --test-notifications

# Test Apprise directly
python3 -c "
import apprise
ap = apprise.Apprise()
ap.add('discord://webhook_id/webhook_token')
ap.notify(body='Test from Apprise', title='Test')
"
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
| `/opt/ups-monitor/ups_monitor.py` | Main script |
| `/etc/ups-monitor/config.yaml` | Configuration file |
| `/etc/systemd/system/eneru.service` | Systemd service |
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
- Docker/Podman management
- Filesystem unmounting
- SSH key access

The systemd service includes basic hardening:
- `NoNewPrivileges=true`
- `PrivateTmp=true`

---

## License

MIT License - See LICENSE file for details.
