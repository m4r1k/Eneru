# Configuration reference

Eneru reads YAML from `/etc/ups-monitor/config.yaml` by default. Pass a different file with `--config`.

The config has two shapes:

| Shape | Use it when | Resource placement |
|-------|-------------|--------------------|
| Single UPS | One UPS protects the Eneru host and its resources | Top-level `virtual_machines`, `containers`, `filesystems`, and `remote_servers` |
| Multi UPS | One Eneru instance monitors several independent UPSes | Resources live under each `ups:` list entry |

Features are off unless their section enables them, except `local_shutdown`, `filesystems.sync_enabled`, extended-time shutdown, and statistics, which have safe defaults.

The tables below cover every YAML key currently parsed by `ConfigLoader`, including the legacy `docker:` and `discord:` compatibility forms. The exhaustive commented sample is [`examples/config-reference.yaml`](https://github.com/m4r1k/Eneru/blob/main/examples/config-reference.yaml). Shorter starting points are in [`examples/`](https://github.com/m4r1k/Eneru/tree/main/examples).

## Single UPS example

```yaml
ups:
  name: "UPS@192.168.1.100"
  display_name: "Homelab UPS"
  check_interval: 1
  max_stale_data_tolerance: 3
  connection_loss_grace_period:
    enabled: true
    duration: 60
    flap_threshold: 5

triggers:
  low_battery_threshold: 20
  critical_runtime_threshold: 600
  voltage_sensitivity: normal
  depletion:
    window: 300
    critical_rate: 15.0
    grace_period: 90
  extended_time:
    enabled: true
    threshold: 900

notifications:
  title: "Homelab UPS"
  urls:
    - "discord://webhook_id/webhook_token"

virtual_machines:
  enabled: true
  max_wait: 60

containers:
  enabled: true
  runtime: auto
  stop_timeout: 60
  compose_files:
    - path: "/opt/database/docker-compose.yml"
      stop_timeout: 120
    - "/opt/apps/docker-compose.yml"
  shutdown_all_remaining_containers: true

filesystems:
  sync_enabled: true
  unmount:
    enabled: true
    timeout: 15
    mounts:
      - path: "/mnt/nas"
        options: "-l"

remote_servers:
  - name: "Proxmox Host"
    enabled: true
    host: "192.168.1.60"
    user: "root"
    shutdown_order: 1
    pre_shutdown_commands:
      - action: "stop_proxmox_vms"
        timeout: 180
      - action: "stop_proxmox_cts"
        timeout: 60
      - action: "sync"
    shutdown_command: "shutdown -h now"

local_shutdown:
  enabled: true
  command: "shutdown -h now"
```

## Multi UPS example

Use a list under `ups:` when each UPS protects a different set of resources. Exactly one group may set `is_local: true` if Eneru should manage local VMs, containers, filesystems, or local shutdown ownership.

```yaml
ups:
  - name: "UPS1@192.168.1.10"
    display_name: "Main rack"
    is_local: true
    triggers:
      voltage_sensitivity: tight
    virtual_machines:
      enabled: true
    containers:
      enabled: true
    remote_servers:
      - name: "Proxmox Node"
        enabled: true
        host: "192.168.1.20"
        user: "root"

  - name: "UPS2@192.168.1.11"
    display_name: "Storage rack"
    triggers:
      voltage_sensitivity: loose
    remote_servers:
      - name: "NAS"
        enabled: true
        host: "192.168.1.30"
        user: "admin"
        shutdown_command: "sudo -i synoshutdown -s"

triggers:
  low_battery_threshold: 20
  critical_runtime_threshold: 600

local_shutdown:
  enabled: true
  drain_on_local_shutdown: false
  trigger_on: any
```

Top-level `triggers:` acts as the default. Per-UPS `triggers:` overrides only the keys it sets.

## Redundancy group example

Redundancy groups are for shared resources fed by multiple UPSes, usually dual-PSU servers. Members still appear under top-level `ups:`; the group references their exact names.

```yaml
ups:
  - name: "UPS-A@10.0.0.10"
  - name: "UPS-B@10.0.0.11"

redundancy_groups:
  - name: "rack-1-dual-psu"
    ups_sources:
      - "UPS-A@10.0.0.10"
      - "UPS-B@10.0.0.11"
    min_healthy: 1
    degraded_counts_as: healthy
    unknown_counts_as: critical
    remote_servers:
      - name: "Dual PSU server"
        enabled: true
        host: "10.0.0.20"
        user: "root"
```

See [Redundancy groups](redundancy-groups.md) for quorum behavior.

## Validate config

Package install:

```bash
sudo eneru validate --config /etc/ups-monitor/config.yaml
```

PyPI install:

```bash
eneru validate --config /etc/ups-monitor/config.yaml
```

Validation catches YAML errors, invalid enum values, local-resource ownership mistakes, duplicate remote-server ownership, bad redundancy-group references, and unsafe notification suppression.

## Top-level sections

| Section | Scope | Purpose |
|---------|-------|---------|
| `ups` | Required | One UPS mapping or a list of UPS groups |
| `triggers` | Global defaults, overridable per group | Shutdown thresholds and voltage sensitivity |
| `behavior` | Global | Dry-run mode |
| `logging` | Global | Log, state, history, and shutdown flag paths |
| `notifications` | Global | Apprise URLs, retry, coalescing, and event suppression |
| `statistics` | Global | SQLite history location and retention |
| `virtual_machines` | Single UPS or local group only | Libvirt VM shutdown |
| `containers` | Single UPS or local group only | Docker or Podman shutdown |
| `filesystems` | Single UPS or local group only | Sync and unmounts |
| `remote_servers` | Single UPS, UPS group, or redundancy group | SSH-based remote shutdown |
| `local_shutdown` | Global | Local host poweroff behavior |
| `redundancy_groups` | Global | Quorum-controlled shared resources |
| `docker` | Legacy single-UPS alias | Compatibility alias for `containers:` with Docker runtime |
| `discord` | Legacy notification alias | Compatibility alias converted into `notifications.urls` |

## UPS

In single-UPS mode this is `ups:`. In multi-UPS mode each list entry accepts the same fields.

| Key | Default | Description |
|-----|---------|-------------|
| `name` | `UPS@localhost` | NUT UPS identifier in `NAME@HOST` form. Use `NAME@HOST:PORT` if needed |
| `display_name` | `null` | Human label for logs, notifications, and TUI |
| `check_interval` | `1` | Poll interval in seconds |
| `max_stale_data_tolerance` | `3` | Failed or stale polls before connection handling starts |
| `is_local` | `false` | Multi-UPS only. This group powers the Eneru host and may own local resources |
| `connection_loss_grace_period.enabled` | `true` | Suppress notifications for brief NUT outages while the UPS is on line power |
| `connection_loss_grace_period.duration` | `60` | Seconds to wait before sending `CONNECTION_LOST` |
| `connection_loss_grace_period.flap_threshold` | `5` | Warning threshold for repeated grace-period recoveries within 24 hours |

The grace period never weakens failsafe behavior. If Eneru loses UPS visibility while the UPS is on battery, shutdown starts immediately.

See [Troubleshooting](troubleshooting.md#intermittent-nut-drops) for tuning guidance on flaky NUT servers and the flap-counter behavior.

## Triggers

| Key | Default | Description |
|-----|---------|-------------|
| `low_battery_threshold` | `20` | Battery percentage that triggers shutdown |
| `critical_runtime_threshold` | `600` | UPS runtime estimate, in seconds, that triggers shutdown |
| `depletion.window` | `300` | Battery-history window for depletion calculation |
| `depletion.critical_rate` | `15.0` | Percentage points per minute that triggers shutdown |
| `depletion.grace_period` | `90` | Seconds after power loss before depletion rate can trigger shutdown |
| `extended_time.enabled` | `true` | Enable wall-clock time-on-battery shutdown |
| `extended_time.threshold` | `900` | Seconds on battery before extended-time shutdown |
| `voltage_sensitivity` | `normal` | Voltage warning preset: `tight`, `normal`, or `loose` |

See [Shutdown triggers](triggers.md) for decision order, voltage threshold details, and common UPS transfer points by vendor.

## Notifications

Notifications use Apprise. Empty `urls` disables notification delivery.

| Key | Default | Description |
|-----|---------|-------------|
| `urls` | `[]` | Apprise service URLs |
| `title` | `null` | Optional notification title prefix |
| `avatar_url` | `null` | Avatar URL for supported services |
| `timeout` | `10` | Per-send timeout in seconds |
| `retry_interval` | `5` | Initial retry delay for failed sends |
| `retry_backoff_max` | `300` | Maximum exponential backoff delay |
| `max_attempts` | `0` | Attempt cap per pending message. `0` means unlimited until age/backlog policy cancels it |
| `max_age_days` | `30` | Pending-message age limit. `0` disables age cancellation |
| `max_pending` | `10000` | Pending backlog cap |
| `retention_days` | `7` | Retention for sent and cancelled notification rows |
| `voltage_hysteresis_seconds` | `30` | Delay voltage warnings until they persist. Severe events bypass this |
| `suppress` | `[]` | Mute specific non-critical event notifications while still logging them |

See [Notifications](notifications.md) for URL examples, retry behavior, coalescing, and suppressible event names.

### Legacy Discord compatibility

Older configs can still use the Discord webhook shape. Eneru converts it to an Apprise `discord://...` URL at load time.

Top-level legacy form:

```yaml
discord:
  webhook_url: "https://discord.com/api/webhooks/WEBHOOK_ID/WEBHOOK_TOKEN"
  timeout: 3
```

Nested legacy form:

```yaml
notifications:
  discord:
    webhook_url: "https://discord.com/api/webhooks/WEBHOOK_ID/WEBHOOK_TOKEN"
    timeout: 3
```

If both `notifications.urls` and `notifications.discord.webhook_url` are present, Eneru keeps the URL list and prepends the converted Discord URL if it is not already present.

## Statistics

Eneru writes one SQLite database per UPS. The writer is best effort; stats failures are logged and monitoring continues.

| Key | Default | Description |
|-----|---------|-------------|
| `db_directory` | `/var/lib/eneru` | Directory for per-UPS `.db` files |
| `retention.raw_hours` | `24` | Raw poll sample retention |
| `retention.agg_5min_days` | `30` | Five-minute aggregate retention |
| `retention.agg_hourly_days` | `1825` | Hourly aggregate retention |

See [Statistics](statistics.md) for schema and queries.

## Logging and behavior

| Key | Default | Description |
|-----|---------|-------------|
| `logging.file` | `/var/log/ups-monitor.log` | File log path. Set `null` to disable file logging |
| `logging.state_file` | `/var/run/ups-monitor.state` | Current state file read by `eneru monitor` |
| `logging.battery_history_file` | `/var/run/ups-battery-history` | Rolling battery history for depletion calculations |
| `logging.shutdown_flag_file` | `/var/run/ups-shutdown-scheduled` | Idempotency flag for shutdown in progress |
| `behavior.dry_run` | `false` | Log shutdown actions without executing them |

## Virtual machines

| Key | Default | Description |
|-----|---------|-------------|
| `enabled` | `false` | Enable libvirt VM shutdown through `virsh` |
| `max_wait` | `30` | Seconds to wait for graceful shutdown before force-destroy |

## Containers

| Key | Default | Description |
|-----|---------|-------------|
| `enabled` | `false` | Enable container shutdown |
| `runtime` | `auto` | `auto`, `docker`, or `podman`. Auto prefers Podman when present |
| `stop_timeout` | `60` | Default graceful stop timeout |
| `compose_files` | `[]` | Compose files to stop first, in order |
| `shutdown_all_remaining_containers` | `true` | Stop remaining containers after compose stacks |
| `include_user_containers` | `false` | Podman only. Also inspect rootless user containers |

Compose entries can be strings or objects:

```yaml
compose_files:
  - "/opt/apps/docker-compose.yml"
  - path: "/opt/database/docker-compose.yml"
    stop_timeout: 120
```

### Legacy `docker:` compatibility

Older single-UPS configs may use a top-level `docker:` section instead of `containers:`. Eneru still parses it and treats it as Docker-only container config:

```yaml
docker:
  enabled: true
  stop_timeout: 60
  compose_files:
    - "/opt/app/docker-compose.yml"
  shutdown_all_remaining_containers: true
```

For new configs, use `containers:`. The legacy `docker:` alias is not used for multi-UPS entries.

## Filesystems

| Key | Default | Description |
|-----|---------|-------------|
| `sync_enabled` | `true` | Run `os.sync()` before shutdown |
| `unmount.enabled` | `false` | Enable unmount phase |
| `unmount.timeout` | `15` | Timeout per mount point |
| `unmount.mounts` | `[]` | Mount paths to unmount |

Mounts can be strings or objects:

```yaml
mounts:
  - "/mnt/media"
  - path: "/mnt/nas"
    options: "-l"
```

## Remote servers

| Key | Default | Description |
|-----|---------|-------------|
| `name` | required | Display name |
| `enabled` | `false` | Enable this remote server |
| `host` | required | Hostname or IP |
| `user` | required | SSH username |
| `connect_timeout` | `10` | SSH connection timeout |
| `command_timeout` | `30` | Default timeout for remote commands |
| `shutdown_command` | `sudo shutdown -h now` | Final shutdown command |
| `ssh_options` | `[]` | Extra SSH options. Avoid disabling host-key checks in production |
| `pre_shutdown_commands` | `[]` | Pre-shutdown actions or commands |
| `shutdown_order` | unset | Explicit phase. Same value runs in parallel; higher values run later |
| `parallel` | unset | Legacy mode. `false` runs before the default parallel batch. Mutually exclusive with `shutdown_order` |
| `shutdown_safety_margin` | `60` | Extra wait budget for parallel server threads |

See [Remote servers](remote-servers.md) for SSH keys, sudoers, predefined actions, and ordering examples.

## Local shutdown

| Key | Default | Description |
|-----|---------|-------------|
| `enabled` | `true` | Power off the Eneru host when its policy fires |
| `command` | `shutdown -h now` | Local shutdown command |
| `message` | `UPS battery critical - emergency shutdown` | Message passed to shutdown where supported |
| `drain_on_local_shutdown` | `false` | Multi-UPS. Drain all groups before powering off the local host |
| `trigger_on` | `any` | Multi-UPS. `any` means any group can trigger local shutdown; `none` disables cross-group local shutdown |
| `wall` | `false` | Broadcast shutdown warnings to logged-in TTYs |

## Redundancy groups

| Key | Default | Description |
|-----|---------|-------------|
| `name` | required | Unique group label |
| `ups_sources` | required | UPS `name` values from the top-level `ups:` list |
| `min_healthy` | `1` | Shutdown fires when healthy member count drops below this number |
| `degraded_counts_as` | `healthy` | Count DEGRADED members as `healthy` or `critical` |
| `unknown_counts_as` | `critical` | Count UNKNOWN members as `critical`, `degraded`, or `healthy` |
| `is_local` | `false` | This redundancy group powers the Eneru host. At most one local group is allowed across all groups |
| `triggers` | inherits | Per-group trigger overrides |
| Resource sections | empty | Same resource surface as a UPS group |

## File locations

| Path | Purpose |
|------|---------|
| `eneru` | Package command on `PATH` |
| `/opt/ups-monitor/eneru.py` | Package wrapper used internally by the native install |
| `/etc/ups-monitor/config.yaml` | Default configuration file |
| `/etc/systemd/system/eneru.service` | Packaged systemd service |
| `/var/log/ups-monitor.log` | Default file log |
| `/var/run/ups-monitor.state` | Current state for the TUI |
| `/var/run/ups-shutdown-scheduled` | Single-UPS shutdown flag |
| `/var/run/ups-shutdown-redundancy-*` | Redundancy group shutdown flags |
| `/var/run/ups-battery-history` | Battery history file |
| `/var/lib/eneru/*.db` | Per-UPS SQLite history |

## CLI reference

| Command | Purpose |
|---------|---------|
| `run` | Start the monitoring daemon |
| `validate` | Validate config and print the shutdown plan |
| `monitor` | Open the TUI dashboard |
| `tui` | Alias for `monitor` |
| `test-notifications` | Send one test notification |
| `completion {bash,zsh,fish}` | Print a shell completion script |
| `version` | Print version |

Common flags:

| Command | Flag | Purpose |
|---------|------|---------|
| `run`, `validate`, `monitor`, `test-notifications` | `-c`, `--config` | Config path |
| `run` | `--dry-run` | Override config and do not execute shutdown actions |
| `run` | `--exit-after-shutdown` | Exit after a shutdown sequence finishes |
| `monitor` | `--once` | Print one status snapshot |
| `monitor` | `--interval` | TUI refresh interval |
| `monitor` | `--graph {charge,load,voltage,runtime}` | Initial graph metric (interactive: cycle with `<G>`) |
| `monitor` | `--time {1h,6h,24h,7d,30d}` | Initial graph / event window (interactive seeds the cycle, still toggle with `<T>`) |
| `monitor` | `--events-only` | Print recent events only |
| `monitor` | `--verbose`, `-v` | Show low-priority events too (default: priority only); `<V>` toggles in-session |
| `monitor` | `--full-history` | Ignore `--time`, query events from the beginning (`--once` only) |

Example package commands:

```bash
sudo eneru validate --config /etc/ups-monitor/config.yaml
sudo eneru run --dry-run --config /etc/ups-monitor/config.yaml
sudo eneru monitor --once --events-only --config /etc/ups-monitor/config.yaml
sudo eneru monitor --once --events-only --verbose --full-history --config /etc/ups-monitor/config.yaml
```
