# Configuration

Configuration is stored in `/etc/ups-monitor/config.yaml`. Features are disabled by removing their section or setting `enabled: false`.

For a comprehensive reference config with every feature flag documented, see [`examples/config-reference.yaml`](https://github.com/m4r1k/Eneru/blob/main/examples/config-reference.yaml). For real-world shapes, see [`examples/config-minimal.yaml`](https://github.com/m4r1k/Eneru/blob/main/examples/config-minimal.yaml), [`examples/config-homelab.yaml`](https://github.com/m4r1k/Eneru/blob/main/examples/config-homelab.yaml), [`examples/config-enterprise.yaml`](https://github.com/m4r1k/Eneru/blob/main/examples/config-enterprise.yaml), and [`examples/config-dual-ups.yaml`](https://github.com/m4r1k/Eneru/blob/main/examples/config-dual-ups.yaml).

---

## Two configuration modes

Eneru supports two top-level shapes for the `ups:` key:

- **Single-UPS (legacy)**: `ups:` is a mapping. Resources (`virtual_machines`, `containers`, `remote_servers`, `filesystems`) sit at the top level. Use this when one UPS protects everything.
- **Multi-UPS (recommended for >1 UPS)**: `ups:` is a list of entries. Each entry carries its own `triggers:`, resources, and `is_local` flag. Use this when independent UPSes each protect their own set of servers.

Both modes parse through the same dataclasses, so the per-section reference tables below apply to both. Multi-UPS adds [redundancy groups](redundancy-groups.md) on top: shared resources protected by 2+ UPS sources (dual-PSU servers behind A+B feeds), with quorum-based shutdown.

---

## Single-UPS configuration example

```yaml
# UPS Monitor Configuration File
# All sections are optional - features are disabled if not configured

# ==============================================================================
# UPS CONNECTION
# ==============================================================================
ups:
  name: "UPS@192.168.178.11"
  display_name: "Homelab UPS"        # optional, used in logs / notifications
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
  voltage_sensitivity: normal        # tight / normal / loose -- see triggers.md

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
  shutdown_flag_file: "/var/run/ups-shutdown-scheduled"

# ==============================================================================
# NOTIFICATIONS (via Apprise)
# ==============================================================================
# Apprise supports 100+ notification services
# See: https://github.com/caronc/apprise/wiki
notifications:
  title: "⚡ Homelab UPS"            # optional title prefix
  avatar_url: "https://raw.githubusercontent.com/m4r1k/Eneru/main/docs/images/eneru-avatar.png"
  timeout: 10
  retry_interval: 5
  voltage_hysteresis_seconds: 30     # debounce voltage flap (default 30s)
  suppress: []                       # per-event mute -- see Notifications table
  urls:
    - "discord://webhook_id/webhook_token"
    # - "slack://token_a/token_b/token_c/#channel"
    # - "telegram://bot_token/chat_id"

# ==============================================================================
# STATISTICS (always-on per-UPS SQLite store)
# ==============================================================================
statistics:
  db_directory: "/var/lib/eneru"
  retention:
    raw_hours: 24
    agg_5min_days: 30
    agg_hourly_days: 1825             # ~5 years

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
  runtime: "auto"                    # auto / docker / podman
  stop_timeout: 60
  compose_files:
    - path: "/opt/database/docker-compose.yml"
      stop_timeout: 120              # per-file override
    - "/opt/apps/docker-compose.yml" # uses global stop_timeout
  shutdown_all_remaining_containers: true
  include_user_containers: false     # podman-only: also stop rootless user containers

# Filesystem Operations
filesystems:
  sync_enabled: true
  unmount:
    enabled: true
    timeout: 15
    mounts:
      - "/mnt/media"
      - path: "/mnt/nas"
        options: "-l"                # lazy unmount
      - "/mnt/backup"

# Remote Server Shutdown
# Use shutdown_order to define phases. Same order = parallel, ascending = sequential.
# Legacy parallel: false still works for simple two-group setups.
remote_servers:
  - name: "Proxmox Host"
    enabled: true
    host: "192.168.178.100"
    user: "root"
    command_timeout: 30
    shutdown_order: 1                # phase 1
    pre_shutdown_commands:
      - action: "stop_proxmox_vms"
        timeout: 180
      - action: "stop_proxmox_cts"
        timeout: 60
      - action: "sync"
    shutdown_command: "shutdown -h now"

  - name: "Synology NAS"
    enabled: true
    host: "192.168.178.229"
    user: "nas-admin"
    shutdown_order: 2                # phase 2 -- runs after phase 1 completes
    shutdown_command: "sudo -i synoshutdown -s"

# Local Server Shutdown
local_shutdown:
  enabled: true
  command: "shutdown -h now"
  message: "UPS battery critical - emergency shutdown"
  drain_on_local_shutdown: false     # multi-UPS: drain all groups before local shutdown
  trigger_on: "any"                  # multi-UPS: "any" or "none"
  wall: false                        # broadcast shutdown via wall(1) to every tty (off since v5.2)
```

---

## Multi-UPS configuration example

When you have more than one UPS, list them under `ups:` so each carries its own resources and triggers. Only the entry marked `is_local: true` may own local resources (VMs, containers, filesystems). Per-UPS `triggers:` overrides the global defaults; absent keys inherit.

```yaml
ups:
  # UPS 1 powers the Eneru host + its local resources
  - name: "UPS1@192.168.1.10"
    display_name: "Main Rack"
    is_local: true
    triggers:
      voltage_sensitivity: tight     # clean PDU -- want early warning

    virtual_machines:
      enabled: true
    containers:
      enabled: true
      runtime: auto
    remote_servers:
      - name: "Proxmox Node 1"
        enabled: true
        host: "192.168.1.20"
        user: "root"

  # UPS 2 powers a separate rack
  - name: "UPS2@192.168.1.11"
    display_name: "Backup Rack"
    triggers:
      voltage_sensitivity: loose     # generator-fed -- dial back

    remote_servers:
      - name: "Synology NAS"
        enabled: true
        host: "192.168.1.30"
        user: "nas-admin"
        shutdown_command: "sudo -i synoshutdown -s"

# Global triggers used as defaults when a per-UPS entry omits them
triggers:
  low_battery_threshold: 20
  critical_runtime_threshold: 600

# Global notifications, statistics, logging, local_shutdown -- shared
notifications:
  urls: ["discord://webhook_id/webhook_token"]

local_shutdown:
  enabled: true
  drain_on_local_shutdown: false     # don't shut other groups when local UPS dies
  trigger_on: "any"                  # any group's shutdown triggers local shutdown
```

See [`examples/config-dual-ups.yaml`](https://github.com/m4r1k/Eneru/blob/main/examples/config-dual-ups.yaml) for a complete multi-UPS example.

---

## Redundancy groups

For dual-PSU servers fed by two UPSes (A+B power feeds), define a redundancy group. Eneru only fires the group's shutdown when fewer than `min_healthy` member UPSes still report a healthy snapshot. UPSes referenced in `ups_sources:` must also appear in the top-level `ups:` list.

```yaml
ups:
  - name: "UPS-A@192.168.1.10"
  - name: "UPS-B@192.168.1.11"

redundancy_groups:
  - name: "rack-1-dual-psu"
    ups_sources:
      - "UPS-A@192.168.1.10"
      - "UPS-B@192.168.1.11"
    min_healthy: 1                   # shutdown when fewer than 1 healthy
    degraded_counts_as: "healthy"    # voltage warnings still count as healthy
    unknown_counts_as: "critical"    # stale NUT data treated as failed
    is_local: false
    remote_servers:
      - name: "Dual-PSU Server"
        enabled: true
        host: "192.168.1.50"
        user: "root"
```

See [Redundancy Groups](redundancy-groups.md) for the full semantics: quorum policy, advisory-mode triggers on members, cross-group cascades, and the validator's ownership rules.

---

## Configuration reference

### UPS section

In single-UPS mode, this section sits at the top level. In multi-UPS mode, the same keys live under each `ups[].` entry.

| Key | Default | Description |
|-----|---------|-------------|
| `name` | `UPS@localhost` | UPS identifier in `NAME@HOST` format |
| `display_name` | `null` | Optional human-readable name used in logs / notifications. Falls back to `name` |
| `check_interval` | `1` | Seconds between status checks |
| `max_stale_data_tolerance` | `3` | Stale data attempts before connection is marked lost |
| `is_local` | `false` | Multi-UPS only. Marks the UPS that powers the Eneru host. At most one `is_local: true` entry across `ups_groups + redundancy_groups`. Required to own local resources (VMs, containers, filesystems) |
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

Per-UPS in multi-UPS mode. Top-level `triggers:` provides the defaults that per-UPS entries inherit unless they override.

| Key | Default | Description |
|-----|---------|-------------|
| `low_battery_threshold` | `20` | Battery percentage that triggers immediate shutdown |
| `critical_runtime_threshold` | `600` | Runtime (seconds) that triggers shutdown |
| `depletion.window` | `300` | Seconds of history for depletion calculation |
| `depletion.critical_rate` | `15.0` | Percentage per minute threshold |
| `depletion.grace_period` | `90` | Seconds to ignore high depletion after power loss |
| `extended_time.enabled` | `true` | Enable extended time on battery shutdown |
| `extended_time.threshold` | `900` | Seconds on battery before shutdown |
| `voltage_sensitivity` | `normal` | Voltage warning band preset: `tight` (±5%), `normal` (±10%, EN 50160), `loose` (±15%). See [voltage thresholds](triggers.md#voltage-thresholds-preset-driven-raw-thresholds-not-user-configurable) |

See [Shutdown triggers](triggers.md) for details on each trigger.

### Notifications section

Global (one block for the whole daemon). Notifications are dispatched via Apprise.

| Key | Default | Description |
|-----|---------|-------------|
| `urls` | `[]` | List of Apprise notification URLs. Empty = notifications disabled |
| `title` | `null` | Optional title prefix for notifications |
| `avatar_url` | `null` | Avatar URL for supported services (Discord, Slack, Mattermost, Guilded, Zulip) |
| `timeout` | `10` | Notification delivery timeout in seconds |
| `retry_interval` | `5` | Initial wait between retry attempts for a failed notification. Per-message exponential backoff doubles this on each failure up to `retry_backoff_max` |
| `retry_backoff_max` | `300` | Ceiling on the per-message exponential backoff, in seconds (5 min). Keeps reconnection quick once the endpoint returns without hammering the network during a long outage |
| `max_attempts` | `0` | Per-message attempt cap. `0` = unlimited (default). Apprise's success/fail signal is a bool — Eneru can't tell "bad URL" from "internet down", so capping attempts risks dropping legitimate messages during a long outage. Set this only as a poison-message kill switch |
| `max_age_days` | `30` | Pending notifications older than this become `cancelled` with reason `too_old`. The only TTL on `pending` rows; `0` disables it. Sized for "long weekend with the internet down" |
| `max_pending` | `10000` | Backlog cap on pending rows. When pending exceeds this, the oldest are cancelled with reason `backlog_overflow`. Bounds DB growth on runaway-event days |
| `retention_days` | `7` | Days to keep `sent` and `cancelled` rows around for forensic inspection via `sqlite3`. `pending` rows are NEVER pruned by TTL — only `max_age_days` ages them out |
| `voltage_hysteresis_seconds` | `30` | Defer voltage `BROWNOUT` / `OVER_VOLTAGE` notifications until the condition has held this long. State log row is always immediate; only notification dispatch is deferred. `0` = legacy immediate behaviour. Severe deviations (>±15%) bypass the dwell |
| `suppress` | `[]` | Per-event mute. Logs always record the event; only the notification is muted. Safety-critical events (`OVER_VOLTAGE_DETECTED`, `BROWNOUT_DETECTED`, `OVERLOAD_ACTIVE`, `BYPASS_MODE_ACTIVE`, `ON_BATTERY`, `CONNECTION_LOST`, any `SHUTDOWN_*`) are validator-rejected. Suppressible: `POWER_RESTORED`, `VOLTAGE_NORMALIZED`, `AVR_BOOST_ACTIVE`, `AVR_TRIM_ACTIVE`, `AVR_INACTIVE`, `BYPASS_MODE_INACTIVE`, `OVERLOAD_RESOLVED`, `CONNECTION_RESTORED`, `VOLTAGE_AUTODETECT_MISMATCH`, `VOLTAGE_FLAP_SUPPRESSED` |

See [Notifications](notifications.md) for service-specific URL formats and setup.

### Statistics section

Global. Eneru always writes per-UPS SQLite stats; this section tunes where they live and how long each tier is kept.

| Key | Default | Description |
|-----|---------|-------------|
| `db_directory` | `/var/lib/eneru` | Directory for per-UPS `.db` files. One file per UPS, named after the sanitised UPS label |
| `retention.raw_hours` | `24` | Hours of raw 1Hz samples kept |
| `retention.agg_5min_days` | `30` | Days of 5-minute aggregates kept |
| `retention.agg_hourly_days` | `1825` | Days (~5 years) of hourly aggregates kept |

See [Statistics](statistics.md) for the schema, query examples, and storage volume estimates.

### Logging section

Global.

| Key | Default | Description |
|-----|---------|-------------|
| `file` | `/var/log/ups-monitor.log` | Log file path. Set to `null` to disable file logging (stdout still works) |
| `state_file` | `/var/run/ups-monitor.state` | Current UPS state file (read by `eneru monitor`) |
| `battery_history_file` | `/var/run/ups-battery-history` | Battery depletion history file |
| `shutdown_flag_file` | `/var/run/ups-shutdown-scheduled` | Sentinel file written when a shutdown is in progress |

### Behavior section

Global.

| Key | Default | Description |
|-----|---------|-------------|
| `dry_run` | `false` | Log actions without executing them. Equivalent to `eneru run --dry-run` |

### Virtual machines section

Per-UPS in multi-UPS mode (only allowed under `is_local: true`). Top-level in single-UPS mode.

| Key | Default | Description |
|-----|---------|-------------|
| `enabled` | `false` | Enable VM shutdown (libvirt / KVM via `virsh`) |
| `max_wait` | `30` | Seconds to wait for graceful shutdown before force-destroy |

### Containers section

Per-UPS in multi-UPS mode (only allowed under `is_local: true`). Top-level in single-UPS mode.

| Key | Default | Description |
|-----|---------|-------------|
| `enabled` | `false` | Enable container shutdown |
| `runtime` | `auto` | Runtime: `auto` (prefers podman), `docker`, or `podman` |
| `stop_timeout` | `60` | Default seconds to wait for graceful stop |
| `compose_files` | `[]` | List of compose files to shutdown first (in order, best effort) |
| `shutdown_all_remaining_containers` | `true` | Shutdown remaining containers after compose stacks |
| `include_user_containers` | `false` | Podman only: also stop rootless user containers |

Compose files can be specified as strings or objects with custom timeout:

```yaml
compose_files:
  - "/opt/apps/docker-compose.yml"        # uses global stop_timeout
  - path: "/opt/database/docker-compose.yml"
    stop_timeout: 120                      # per-file override
```

### Filesystems section

Per-UPS in multi-UPS mode (only allowed under `is_local: true`). Top-level in single-UPS mode.

| Key | Default | Description |
|-----|---------|-------------|
| `sync_enabled` | `true` | Run `os.sync()` to flush buffers before unmount |
| `unmount.enabled` | `false` | Enable filesystem unmounting |
| `unmount.timeout` | `15` | Seconds before per-mount `umount` times out |
| `unmount.mounts` | `[]` | List of mount points to unmount |

Mount points can be specified as strings or objects with options:

```yaml
mounts:
  - "/mnt/simple"              # simple path
  - path: "/mnt/stubborn"      # path with options
    options: "-l"              # lazy unmount
  - path: "/mnt/force"
    options: "-f"              # force unmount
```

### Remote servers section

Per-UPS in multi-UPS mode. Top-level in single-UPS mode.

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
| `parallel` | (unset) | Legacy two-group flag. `true` = parallel batch, `false` = sequential before parallel batch. **Mutually exclusive with `shutdown_order`** |
| `shutdown_order` | (unset) | Phase number (≥ 1). Same order = parallel, ascending = sequential. **Mutually exclusive with `parallel`** |
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
| `stop_xcpng_vms` | Gracefully shutdown XCP-ng / XenServer VMs |
| `stop_esxi_vms` | Gracefully shutdown VMware ESXi VMs |
| `stop_compose` | Stop a compose stack (requires `path` parameter) |
| `sync` | Sync filesystems |

#### Shutdown ordering

Use `shutdown_order` to define multi-phase shutdown. Servers with the same order run in parallel; different orders run sequentially (ascending). For simple setups, `parallel: false` still works. The two flags are **mutually exclusive** — setting both on the same server is rejected at config load.

See [Remote Servers](remote-servers.md) for SSH setup, sudoers configuration, and `shutdown_safety_margin` tuning guidance.

### Local shutdown section

Global.

| Key | Default | Description |
|-----|---------|-------------|
| `enabled` | `true` | Enable local shutdown of the Eneru host |
| `command` | `shutdown -h now` | Shutdown command to execute |
| `message` | `UPS battery critical - emergency shutdown` | Optional message for `shutdown -h` |
| `drain_on_local_shutdown` | `false` | Multi-UPS only. When the local UPS goes critical, also shut down every other group's resources before powering off the host. `false` (default) = only the local group's resources are touched |
| `trigger_on` | `any` | Multi-UPS only. `any` = any group's shutdown triggers local shutdown (default). `none` = local shutdown is opt-in per group; useful when the host is on its own dedicated UPS that never participates in cascades |
| `wall` | `false` | Broadcast shutdown warnings to every logged-in tty via `wall(1)`. Off by default since v5.2 — Apprise covers the modern path. Flip to `true` if you still want the tty blast on top (legacy from the v2 ups-monitor era) |

### Redundancy groups section

Top-level. List of groups; each entry shares the resource surface of `UPSGroupConfig` plus quorum policy.

| Key | Default | Description |
|-----|---------|-------------|
| `name` | (required) | Group label, used in logs and the dry-run flag-file path |
| `ups_sources` | (required) | List of UPS `name`s that protect this group. Each must appear in the top-level `ups:` list |
| `min_healthy` | `1` | Quorum threshold. Shutdown fires when `healthy_count < min_healthy` |
| `degraded_counts_as` | `healthy` | How a `DEGRADED` member counts: `healthy` (tolerant of voltage warnings, default) or `critical` (strict) |
| `unknown_counts_as` | `critical` | How an `UNKNOWN` member (stale NUT data) counts: `critical` (fail-safe, default), `degraded` (counted via `degraded_counts_as`), or `healthy` (risky — assumes best on missing data) |
| `is_local` | `false` | Marks a redundancy group whose shutdown also powers off the Eneru host (mirror of UPS-group `is_local`). At most one `is_local: true` across all groups |
| `triggers` | inherits | Per-group `triggers:` block (same shape as the per-UPS one). Members run with their per-UPS triggers in advisory mode |
| `remote_servers` / `virtual_machines` / `containers` / `filesystems` | (empty) | Same shape as `UPSGroupConfig`. Resources owned by the group, shut down by the evaluator when quorum is lost |

See [Redundancy Groups](redundancy-groups.md) for the full lifecycle: advisory triggers on member UPSes, cross-group cascades, validator ownership rules, and the dry-run flag-file path.

---

## File locations

| File | Purpose |
|------|---------|
| `/opt/ups-monitor/eneru.py` | Main script (deb/rpm install) |
| `/etc/ups-monitor/config.yaml` | Configuration file |
| `/etc/systemd/system/eneru.service` | Systemd service |
| `/var/log/ups-monitor.log` | Log file |
| `/var/run/ups-monitor.state` | Current UPS state (read by `eneru monitor`) |
| `/var/run/ups-shutdown-scheduled` | Shutdown in progress flag |
| `/var/run/ups-battery-history` | Battery depletion history |
| `/var/lib/eneru/*.db` | Per-UPS SQLite statistics store (one file per UPS) |

---

## Command-line options

| Subcommand | Description |
|------------|-------------|
| `eneru run` | Start the monitoring daemon |
| `eneru validate` | Validate configuration and show overview |
| `eneru monitor` | Launch real-time TUI dashboard |
| `eneru tui` | Alias for `monitor` |
| `eneru test-notifications` | Send a test notification and exit |
| `eneru completion {bash,zsh,fish}` | Print shell completion script (source the output) |
| `eneru version` | Show version information |

Running bare `eneru` without a subcommand shows help.

**Flags for `eneru run`:**

| Flag | Description |
|------|-------------|
| `-c`, `--config` | Path to configuration file |
| `--dry-run` | Run in dry-run mode (overrides config) |
| `--exit-after-shutdown` | Exit after the shutdown sequence completes (used by E2E tests) |

**Flags for `eneru monitor` / `eneru tui`:**

| Flag | Description |
|------|-------------|
| `-c`, `--config` | Path to configuration file |
| `--once` | Print one snapshot and exit (no curses TUI) |
| `--interval` | Refresh interval in seconds (default: 5) |
| `--graph {charge,load,voltage,runtime}` | With `--once`: render an ASCII / Braille graph for the metric |
| `--time {1h,6h,24h,7d,30d}` | With `--once --graph`: time range to plot |
| `--events-only` | With `--once`: print only the events list (reads from SQLite, falls back to log-tail when no DB exists) |

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

# One-shot status snapshot for scripts
eneru monitor --once --config /etc/ups-monitor/config.yaml

# 24h voltage graph for the dashboard's main UPS
eneru monitor --once --graph voltage --time 24h --config /etc/ups-monitor/config.yaml

# Recent events only (uses SQLite)
eneru monitor --once --events-only --config /etc/ups-monitor/config.yaml

# Send a test notification
eneru test-notifications --config /etc/ups-monitor/config.yaml

# Enable shell completion (bash; zsh / fish work the same way)
source <(eneru completion bash)
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
- Strict-enum fields (`local_shutdown.trigger_on`, `triggers.voltage_sensitivity`, `notifications.suppress` event names, redundancy `degraded_counts_as` / `unknown_counts_as`)
- Multi-UPS ownership rules (only `is_local` group can manage VMs / containers / filesystems)
- `is_local` uniqueness across the combined set of UPS groups + redundancy groups
- Redundancy-group rules: `min_healthy` bounds, unknown UPS references, duplicate sources, missing names, duplicate names, cross-tier remote-server conflicts. See [Redundancy Groups](redundancy-groups.md)
- `notifications.voltage_hysteresis_seconds` non-negative; warns above 600s
- `notifications.suppress` accepts only documented suppressible event names; rejects safety-critical names
- Notification service availability (Apprise installed?)
- Reachable UPS (optional connectivity test)

For the per-UPS metrics store (`statistics:` section), see [Statistics](statistics.md).
