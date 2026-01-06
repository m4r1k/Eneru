# Configuration

All configuration is stored in `/etc/ups-monitor/config.yaml`. Features are disabled by removing their section or setting `enabled: false`.

---

## Full Configuration Example

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

  # Seconds between retry attempts for failed notifications (default: 5)
  retry_interval: 5

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

  # Compose files to shutdown first (in order, best effort)
  compose_files:
    - path: "/opt/database/docker-compose.yml"
      stop_timeout: 120  # Override global timeout for this file
    - "/opt/apps/docker-compose.yml"  # Uses global stop_timeout

  # Shutdown remaining containers after compose stacks (default: true)
  shutdown_all_remaining_containers: true

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
# Servers are shutdown in two phases:
#   1. Sequential: Servers with parallel: false (in config order)
#   2. Parallel: Remaining servers (parallel: true, the default)
remote_servers:
  # Proxmox host with pre-shutdown commands
  - name: "Proxmox Host"
    enabled: true
    host: "192.168.178.100"
    user: "root"
    command_timeout: 30
    pre_shutdown_commands:
      - action: "stop_proxmox_vms"
        timeout: 180
      - action: "stop_proxmox_cts"
        timeout: 60
      - action: "sync"
    shutdown_command: "shutdown -h now"

  # NAS with dependency - shutdown LAST
  - name: "Synology NAS"
    enabled: true
    host: "192.168.178.229"
    user: "nas-admin"
    parallel: false  # Shutdown after all parallel servers
    shutdown_command: "sudo -i synoshutdown -s"
    ssh_options:
      - "-o StrictHostKeyChecking=no"

# Local Server Shutdown
local_shutdown:
  enabled: true
  command: "shutdown -h now"
  message: "UPS battery critical - emergency shutdown"
```

---

## Configuration Reference

### UPS Section

| Key | Default | Description |
|-----|---------|-------------|
| `name` | `UPS@localhost` | UPS identifier in `NAME@HOST` format |
| `check_interval` | `1` | Seconds between status checks |
| `max_stale_data_tolerance` | `3` | Stale data attempts before connection is marked lost |

### Triggers Section

| Key | Default | Description |
|-----|---------|-------------|
| `low_battery_threshold` | `20` | Battery percentage that triggers immediate shutdown |
| `critical_runtime_threshold` | `600` | Runtime (seconds) that triggers shutdown |
| `depletion.window` | `300` | Seconds of history for depletion calculation |
| `depletion.critical_rate` | `15.0` | Percentage per minute threshold |
| `depletion.grace_period` | `90` | Seconds to ignore high depletion after power loss |
| `extended_time.enabled` | `true` | Enable extended time on battery shutdown |
| `extended_time.threshold` | `900` | Seconds on battery before shutdown |

See [Shutdown Triggers](triggers.md) for detailed explanations of each trigger.

### Behavior Section

| Key | Default | Description |
|-----|---------|-------------|
| `dry_run` | `false` | Log actions without executing them |

### Logging Section

| Key | Default | Description |
|-----|---------|-------------|
| `file` | `/var/log/ups-monitor.log` | Log file path |
| `state_file` | `/var/run/ups-monitor.state` | Current UPS state file |
| `battery_history_file` | `/var/run/ups-battery-history` | Battery depletion history file |

### Notifications Section

| Key | Default | Description |
|-----|---------|-------------|
| `title` | `null` | Optional title prefix for notifications |
| `avatar_url` | `null` | Avatar URL for supported services (Discord, Slack) |
| `timeout` | `10` | Notification delivery timeout in seconds |
| `urls` | `[]` | List of Apprise notification URLs |

See [Notifications](notifications.md) for setup instructions.

### Virtual Machines Section

| Key | Default | Description |
|-----|---------|-------------|
| `enabled` | `false` | Enable VM shutdown |
| `max_wait` | `30` | Seconds to wait for graceful shutdown before force-destroy |

### Containers Section

| Key | Default | Description |
|-----|---------|-------------|
| `enabled` | `false` | Enable container shutdown |
| `runtime` | `auto` | Runtime: `auto`, `docker`, or `podman` |
| `stop_timeout` | `60` | Default seconds to wait for graceful stop |
| `compose_files` | `[]` | List of compose files to shutdown first (in order) |
| `shutdown_all_remaining_containers` | `true` | Shutdown remaining containers after compose stacks |
| `include_user_containers` | `false` | Podman only: also stop rootless user containers |

Compose files can be specified as strings or objects with custom timeout:

```yaml
compose_files:
  - "/opt/apps/docker-compose.yml"       # Uses global stop_timeout
  - path: "/opt/database/docker-compose.yml"
    stop_timeout: 120                    # Custom timeout for this file
```

### Filesystems Section

| Key | Default | Description |
|-----|---------|-------------|
| `sync_enabled` | `true` | Run `sync` to flush buffers |
| `unmount.enabled` | `false` | Enable filesystem unmounting |
| `unmount.timeout` | `15` | Seconds before unmount times out |
| `unmount.mounts` | `[]` | List of mount points to unmount |

Mount points can be specified as strings or objects with options:

```yaml
mounts:
  - "/mnt/simple"              # Simple path
  - path: "/mnt/stubborn"      # Path with options
    options: "-l"              # Lazy unmount
  - path: "/mnt/force"
    options: "-f"              # Force unmount
```

### Remote Servers Section

| Key | Default | Description |
|-----|---------|-------------|
| `name` | (required) | Display name for logging |
| `enabled` | `false` | Enable this server |
| `host` | (required) | Hostname or IP address |
| `user` | (required) | SSH username |
| `connect_timeout` | `10` | SSH connection timeout in seconds |
| `command_timeout` | `30` | Default timeout for commands in seconds |
| `shutdown_command` | `sudo shutdown -h now` | Final shutdown command |
| `ssh_options` | `[]` | Additional SSH options |
| `pre_shutdown_commands` | `[]` | Commands to run before shutdown (see below) |
| `parallel` | `true` | Shutdown concurrently with other parallel servers |

#### Pre-Shutdown Commands

Run commands on remote servers before the final shutdown. Supports predefined actions and custom commands:

```yaml
pre_shutdown_commands:
  # Predefined actions
  - action: "stop_proxmox_vms"
    timeout: 180
  - action: "sync"

  # Custom commands
  - command: "systemctl stop my-service"
    timeout: 30
```

**Predefined Actions:**

| Action | Description |
|--------|-------------|
| `stop_containers` | Stop all Docker/Podman containers |
| `stop_vms` | Gracefully shutdown libvirt/KVM VMs |
| `stop_proxmox_vms` | Gracefully shutdown Proxmox QEMU VMs |
| `stop_proxmox_cts` | Gracefully shutdown Proxmox LXC containers |
| `stop_xcpng_vms` | Gracefully shutdown XCP-ng/XenServer VMs |
| `stop_esxi_vms` | Gracefully shutdown VMware ESXi VMs |
| `stop_compose` | Stop a compose stack (requires `path` parameter) |
| `sync` | Sync filesystems |

#### Parallel vs Sequential Shutdown

Servers are shutdown in two phases:

1. **Sequential**: Servers with `parallel: false` shutdown one-by-one in config order
2. **Parallel**: Remaining servers (default `parallel: true`) shutdown concurrently

Use `parallel: false` for servers with dependencies (e.g., NAS that other servers mount).

See [Remote Servers](remote-servers.md) for SSH setup instructions.

### Local Shutdown Section

| Key | Default | Description |
|-----|---------|-------------|
| `enabled` | `false` | Enable local shutdown |
| `command` | `shutdown -h now` | Shutdown command to execute |
| `message` | `""` | Optional message for shutdown command |

---

## File Locations

| File | Purpose |
|------|---------|
| `/opt/ups-monitor/eneru.py` | Main script |
| `/etc/ups-monitor/config.yaml` | Configuration file |
| `/etc/systemd/system/eneru.service` | Systemd service |
| `/var/log/ups-monitor.log` | Log file |
| `/var/run/ups-monitor.state` | Current UPS state |
| `/var/run/ups-shutdown-scheduled` | Shutdown in progress flag |
| `/var/run/ups-battery-history` | Battery depletion history |

---

## Command-Line Options

Eneru supports the following command-line options:

| Option | Description |
|--------|-------------|
| `-h`, `--help` | Show help message and exit |
| `-c CONFIG`, `--config CONFIG` | Path to configuration file (default: `/etc/ups-monitor/config.yaml`) |
| `--dry-run` | Run in dry-run mode (overrides config file setting) |
| `--validate-config` | Validate configuration file and exit |
| `--test-notifications` | Send a test notification and exit |
| `-v`, `--version` | Show version number and exit |
| `--exit-after-shutdown` | Exit after completing shutdown sequence (useful for testing/scripting) |

### Examples

```bash
# Validate configuration
eneru --validate-config

# Run with custom config file
eneru --config /path/to/config.yaml

# Test in dry-run mode
eneru --dry-run

# Send a test notification
eneru --test-notifications

# Run once and exit after shutdown (for scripting/testing)
eneru --exit-after-shutdown
```

---

## Validating Configuration

Always validate your configuration before starting the service:

```bash
sudo python3 /opt/ups-monitor/eneru.py --validate-config
```

This checks for:

- YAML syntax errors
- Required fields
- Valid value ranges
- Reachable UPS (optional connectivity test)
