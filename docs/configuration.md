# Configuration

Configuration is stored in `/etc/ups-monitor/config.yaml`. Features are disabled by removing their section or setting `enabled: false`.

For a comprehensive reference config with every feature flag documented, see [`examples/config-reference.yaml`](https://github.com/m4r1k/Eneru/blob/main/examples/config-reference.yaml).

---

## Full configuration example

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
  connection_loss_grace_period:
    enabled: true
    duration: 60
    flap_threshold: 5

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
# Use shutdown_order to define phases. Same order = parallel, ascending = sequential.
# Legacy parallel: false still works for simple two-group setups.
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

## Configuration reference

### UPS section

| Key | Default | Description |
|-----|---------|-------------|
| `name` | `UPS@localhost` | UPS identifier in `NAME@HOST` format |
| `check_interval` | `1` | Seconds between status checks |
| `max_stale_data_tolerance` | `3` | Stale data attempts before connection is marked lost |
| `connection_loss_grace_period.enabled` | `true` | Enable grace period for connection loss notifications |
| `connection_loss_grace_period.duration` | `60` | Seconds to suppress notifications after connection loss |
| `connection_loss_grace_period.flap_threshold` | `5` | Send warning after this many grace-period recoveries within 24h |

#### Connection loss grace period

When a NUT server becomes unreachable while the UPS is on line power, Eneru waits
for the configured duration before sending a `CONNECTION_LOST` notification. If the
connection recovers within this window, no notification is sent. This avoids
notification storms from flaky NUT server connections, which are common with
integrated NUT servers on some UPS devices.

If the connection repeatedly flaps (recovers within the grace period), Eneru sends a
single `WARNING` notification after the configured number of flaps (`flap_threshold`)
within a 24-hour window to indicate the NUT server is unstable.

**Important:** The grace period never affects the failsafe mechanism. If the UPS is
on battery power when the connection is lost, Eneru triggers an immediate emergency
shutdown regardless of the grace period setting.

### Triggers section

| Key | Default | Description |
|-----|---------|-------------|
| `low_battery_threshold` | `20` | Battery percentage that triggers immediate shutdown |
| `critical_runtime_threshold` | `600` | Runtime (seconds) that triggers shutdown |
| `depletion.window` | `300` | Seconds of history for depletion calculation |
| `depletion.critical_rate` | `15.0` | Percentage per minute threshold |
| `depletion.grace_period` | `90` | Seconds to ignore high depletion after power loss |
| `extended_time.enabled` | `true` | Enable extended time on battery shutdown |
| `extended_time.threshold` | `900` | Seconds on battery before shutdown |
| `voltage_sensitivity` | `normal` | Voltage warning band preset: `tight` (±5%), `normal` (±10%, EN 50160), or `loose` (±15%). See [voltage thresholds](triggers.md#voltage-thresholds-preset-driven-raw-thresholds-not-user-configurable). |

See [Shutdown triggers](triggers.md) for details on each trigger.

### Behavior section

| Key | Default | Description |
|-----|---------|-------------|
| `dry_run` | `false` | Log actions without executing them |

### Logging section

| Key | Default | Description |
|-----|---------|-------------|
| `file` | `/var/log/ups-monitor.log` | Log file path |
| `state_file` | `/var/run/ups-monitor.state` | Current UPS state file |
| `battery_history_file` | `/var/run/ups-battery-history` | Battery depletion history file |

### Notifications section

| Key | Default | Description |
|-----|---------|-------------|
| `title` | `null` | Optional title prefix for notifications |
| `avatar_url` | `null` | Avatar URL for supported services (Discord, Slack) |
| `timeout` | `10` | Notification delivery timeout in seconds |
| `urls` | `[]` | List of Apprise notification URLs |

See [Notifications](notifications.md) for setup instructions.

### Virtual machines section

| Key | Default | Description |
|-----|---------|-------------|
| `enabled` | `false` | Enable VM shutdown |
| `max_wait` | `30` | Seconds to wait for graceful shutdown before force-destroy |

### Containers section

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

### Filesystems section

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

### Remote servers section

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
| `parallel` | (unset) | Legacy two-group flag; `true` = parallel batch, `false` = sequential before parallel batch. Mutually exclusive with `shutdown_order` |
| `shutdown_order` | (none) | Phase number (>= 1). Same order = parallel, ascending = sequential. Mutually exclusive with `parallel` |
| `shutdown_safety_margin` | `60` | Seconds added to the per-server timeout budget when waiting for the parallel-shutdown thread. Set `0` to opt out |

#### Pre-shutdown commands

Commands to run on remote servers before the final shutdown:

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

**Predefined actions:**

| Action | Description |
|--------|-------------|
| `stop_containers` | Stop all Docker/Podman containers |
| `stop_vms` | Gracefully shutdown libvirt/KVM VMs |
| `stop_proxmox_vms` | Gracefully shutdown Proxmox QEMU VMs (runs via `sudo`) |
| `stop_proxmox_cts` | Gracefully shutdown Proxmox LXC containers (runs via `sudo`) |
| `stop_xcpng_vms` | Gracefully shutdown XCP-ng/XenServer VMs |
| `stop_esxi_vms` | Gracefully shutdown VMware ESXi VMs |
| `stop_compose` | Stop a compose stack (requires `path` parameter) |
| `sync` | Sync filesystems |

#### Shutdown ordering

Use `shutdown_order` to define multi-phase shutdown. Servers with the same order run in parallel; different orders run sequentially (ascending). For simple setups, `parallel: false` still works. The two flags are **mutually exclusive** — setting both on the same server is rejected at config load time.

See [Remote Servers](remote-servers.md) for detailed examples, SSH setup instructions, and `shutdown_safety_margin` tuning guidance.

### Local shutdown section

| Key | Default | Description |
|-----|---------|-------------|
| `enabled` | `false` | Enable local shutdown |
| `command` | `shutdown -h now` | Shutdown command to execute |
| `message` | `""` | Optional message for shutdown command |

---

## File locations

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

## Command-line options

| Subcommand | Description |
|------------|-------------|
| `eneru run` | Start the monitoring daemon |
| `eneru validate` | Validate configuration and show overview |
| `eneru monitor` | Launch real-time TUI dashboard |
| `eneru test-notifications` | Send a test notification and exit |
| `eneru version` | Show version information |

Running bare `eneru` without a subcommand shows help.

**Flags for `eneru run`:**

| Flag | Description |
|------|-------------|
| `-c`, `--config` | Path to configuration file |
| `--dry-run` | Run in dry-run mode (overrides config) |
| `--exit-after-shutdown` | Exit after shutdown sequence (for testing) |

### Examples

```bash
# Validate configuration
eneru validate --config /etc/ups-monitor/config.yaml

# Start monitoring
eneru run --config /etc/ups-monitor/config.yaml

# Test in dry-run mode
eneru run --dry-run --config /etc/ups-monitor/config.yaml

# Real-time dashboard
eneru monitor --config /etc/ups-monitor/config.yaml

# Send a test notification
eneru test-notifications --config /etc/ups-monitor/config.yaml
```

---

## Validating configuration

Validate your configuration before starting the service:

```bash
eneru validate --config /etc/ups-monitor/config.yaml
```

This checks for:

- YAML syntax errors
- Required fields and valid value ranges
- Multi-UPS ownership rules (only `is_local` group can manage VMs/containers)
- Redundancy-group rules: `min_healthy` bounds, unknown UPS references,
  `is_local` uniqueness across all groups, cross-tier remote-server
  conflicts. See [Redundancy Groups](redundancy-groups.md).
- Notification service availability
- Reachable UPS (optional connectivity test)

For the per-UPS metrics store (`statistics:` section), see
[Statistics](statistics.md).
