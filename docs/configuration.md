# Configuration reference

Eneru reads YAML from `/etc/ups-monitor/config.yaml` by default. Pass a different file with `--config`.

The config has two shapes:

| Shape | Use it when | Resource placement |
|-------|-------------|--------------------|
| Single UPS | One UPS protects the Eneru host and its resources | Top-level `virtual_machines`, `containers`, `filesystems`, and `remote_servers` |
| Multi UPS | One Eneru instance monitors several independent UPSes | Resources live under each `ups:` list entry |

Features are off unless their section enables them, except `local_shutdown`, `filesystems.sync_enabled`, extended-time shutdown, and statistics, which have safe defaults.

The tables below cover every YAML key currently parsed by `ConfigLoader`, including the legacy `docker:` and `discord:` compatibility forms. The exhaustive commented sample is [`examples/config-reference.yaml`](https://github.com/m4r1k/Eneru/blob/main/examples/config-reference.yaml). Shorter starting points are in [`examples/`](https://github.com/m4r1k/Eneru/tree/main/examples).

Eneru treats unknown keys in safety-sensitive sections as validation errors.
This catches typos such as `behavior.dry-run` or `triggers.exteneded_time`
before the daemon starts. Legacy compatibility forms that still work, such as
top-level `docker:` and `discord:` webhook config, remain accepted.

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
| `api` | Global | Embedded read-only HTTP API |
| `prometheus` | Global | `/metrics` endpoint toggle |
| `remote_health` | Global | Harmless SSH connectivity checks for remote servers |
| `mqtt` | Global | Optional outbound MQTT status publishing |
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
| `on_battery_stabilization_delay` | `30` | Seconds after a fresh OB transition before charge/runtime/rate/time triggers can fire |
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

Slow-response diagnostics are event rows too. Rate-limited slow NUT polls
use `SLOW_NUT_RESPONSE`; successful but slow remote SSH health probes use
`REMOTE_SSH_SLOW_RESPONSE`. They are hidden from the default Power Events
view and appear in the TUI/API events list at Diagnostics verbosity (`-v`).

## Logging and behavior

| Key | Default | Description |
|-----|---------|-------------|
| `logging.file` | `/var/log/ups-monitor.log` | File log path. Set `null` to disable file logging |
| `logging.format` | `text` | `text` or `json` |
| `logging.state_file` | `/var/run/ups-monitor.state` | Current state file read by `eneru monitor` |
| `logging.battery_history_file` | `/var/run/ups-battery-history` | Rolling battery history for depletion calculations |
| `logging.shutdown_flag_file` | `/var/run/ups-shutdown-scheduled` | Idempotency flag for shutdown in progress |
| `logging.syslog.enabled` | `false` | Forward log rows to syslog |
| `logging.syslog.address` | `/dev/log` | Local syslog socket or remote syslog host |
| `logging.syslog.port` | `514` | Remote syslog UDP port |
| `logging.syslog.facility` | `daemon` | Syslog facility |
| `behavior.dry_run` | `false` | Log shutdown actions without executing them |

## API, metrics, remote health, and MQTT

The API is opt-in and binds to localhost by default when enabled. With `api.auth` off it is **read-only** (every write surface is hard-disabled), exactly as in v5.x. Turning `api.auth` on (see [Authentication](authentication.md)) adds the tiered write path: reads stay open unless `api.auth.require_for_reads`, while UPS control, event deletion, and config reload require a credential. Still, do not expose the socket to untrusted networks without auth: anonymous `/api/v1/config` reveals server hostnames and presence flags. Two settings in this section are on by default once their parent feature is enabled: Prometheus `/metrics` is on once the API is enabled, and `remote_health.enabled` defaults to `true` because the probes run only against explicitly enabled remote servers and use the configured `probe_command` (`true` by default).

| Key | Default | Description |
|-----|---------|-------------|
| `api.enabled` | `false` | Start the embedded HTTP API with `eneru run` |
| `api.bind` | `127.0.0.1` | Listen address |
| `api.port` | `9191` | Listen port |
| `api.auth.enabled` | `false` | Opt-in API authentication. When off, the API is read-only and all write surfaces are hard disabled (v5.3 behavior) |
| `api.auth.require_for_reads` | `false` | When off, read endpoints (incl. `/metrics`) stay open even with auth on; writes always require a credential. Set on to also gate reads |
| `api.auth.session_ttl` | `3600` | Dashboard session token lifetime, seconds |
| `api.auth.db_path` | `/var/lib/eneru/auth.db` | Where local users and API keys are stored (global SQLite DB, separate from per-UPS stats). CLI `--auth-db` overrides |
| `prometheus.enabled` | `true` | Serve Prometheus text metrics at `/metrics` |
| `remote_health.enabled` | `true` | Run harmless SSH probes for explicitly enabled remote servers |
| `remote_health.startup_check` | `true` | Check remote SSH connectivity at daemon startup |
| `remote_health.interval` | `3600` | Periodic check interval in seconds |
| `remote_health.probe_command` | `"true"` | Harmless SSH command (the Unix `true(1)`) used only for healthchecks |
| `remote_health.failure_threshold` | `2` | Consecutive failures before a target is marked failed |
| `remote_health.notify_on_failure` | `true` | Send notification when a target enters failed state |
| `remote_health.notify_on_recovery` | `true` | Send notification when a failed target recovers |
| `mqtt.enabled` | `false` | Publish outbound status snapshots to MQTT |
| `mqtt.broker` | `""` | Broker URL: `mqtt://host:port` for plaintext or `mqtts://host:port` for TLS via the system trust store (default port 8883 for `mqtts`). |
| `mqtt.topic_prefix` | `eneru` | Topic prefix; messages publish to `<topic_prefix>/status` (QoS 0, retain false) |
| `mqtt.publish_interval` | `10` | Republish at this interval even when the status fingerprint is unchanged |
| `nut_control.enabled` | `false` | Enable UPS control (upscmd/upsrw). Requires `api.auth.enabled` or startup fails (write surface). See [UPS control](nut-control.md) |
| `nut_control.username` | `""` | NUT `upsd.users` account with INSTCMD/SET actions |
| `nut_control.password` | `""` | Password for that NUT account |
| `nut_control.allowed_commands` | `[]` | Allowlisted instant commands (e.g. `test.battery.start`, `beeper.toggle`). Calibration/FSD omitted by default |
| `nut_control.allowed_variables` | `[]` | Allowlisted writable variables for upsrw. Empty by default — opt in each one |
| `nut_control.timeout` | `10` | Per-command subprocess timeout (seconds) |

`api.bind` defaults to `127.0.0.1`. If you set it to a non-loopback address, Eneru emits a startup warning because `/api/v1/config` returns server hostnames, SSH usernames, shutdown ordering, and presence flags with no auth. Front-end the API with SSH or a reverse proxy that adds auth before exposing it beyond a trusted boundary.

## Hot-reload

Eneru can re-read its configuration without restarting. Trigger it with `SIGHUP`
or, when the API is enabled, an authenticated `POST /api/v1/config/reload`.

Reload is nginx-style: the file is re-parsed and validated first. If it is
invalid, the daemon **keeps running on the previous config** and logs the error,
so a typo never takes monitoring down. Valid changes are split in two:

- **Applied live:** trigger thresholds (per UPS group), `behavior.dry_run`,
  `nut_control` allowlists/credentials, `prometheus.enabled`, `notifications`
  (URLs/targets, via an Apprise rebuild), MQTT broker/topic/interval,
  `remote_health` interval/probe/thresholds, and `statistics.retention`.
- **Restart-required (reported, not applied):** `api.bind`/`port` and auth,
  UPS/redundancy topology, `logging`, `local_shutdown`, and
  `statistics.db_directory`. These are captured at startup by sockets, file
  handlers, dependency checks, or DB connections, so Eneru reports them as
  restart-required rather than half-applying them.

```bash
# systemd / bare-metal
sudo systemctl reload eneru          # sends SIGHUP via ExecReload

# container (tini forwards the signal to the daemon)
docker kill -s HUP <container>

# via the API (needs a session token or API key)
curl -X POST -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:9191/api/v1/config/reload
```

The reload response (and the daemon log) lists what was applied and what still
needs a restart.

`eneru run --api`, `--api-bind`, and `--api-port` override these API settings for one daemon invocation. This is mainly for Docker, Podman, and Kubernetes healthchecks where the image should expose `/health` even if the mounted config does not enable the API.

`remote_health.probe_command` is rejected at validation time if it contains shell metacharacters (`;`, `|`, `&`, `$`, backtick, redirections, parentheses, or newlines) or any keyword in the dangerous-words blocklist. Probes are advisory: they never run pre-shutdown commands, VM/container shutdown commands, custom commands, or the configured `shutdown_command`.

**MQTT on RHEL.** Debian/Ubuntu `.deb` packages install `python3-paho-mqtt` as a hard dependency. RPM packages list it as a `Recommends:` only. RHEL 9 + EPEL pulls it in automatically, but on RHEL 8 (where the EPEL build targets the system python3.6, not the python3.9 used by Eneru) and on RHEL 10 (no `python3-paho-mqtt` exists in BaseOS / AppStream / CRB / EPEL 10) you need to install it via pip after installing eneru:

```bash
# RHEL 8 (with python39 alternative):
python3 -m pip install paho-mqtt

# RHEL 10 (PEP 668 — system site-packages externally managed):
python3 -m pip install --break-system-packages paho-mqtt
```

If MQTT is enabled but `paho-mqtt` isn't importable, the publisher logs a warning and disables itself; the daemon keeps running. The MQTT publisher reconnects with bounded exponential backoff (1 s → 60 s) on connection failure or unexpected disconnect.

Remote health is advisory. The daemon runs the safe SSH `probe_command` itself, default `true`, and moves unreachable targets to `DEGRADED` before `FAILED` at `failure_threshold`. Failure notifications fire once per failed period and recovery notifications fire once when the target returns. API, MQTT, and Prometheus only read the manager's live state or its sidecar JSON; they do not run SSH.

The API and MQTT status payloads include `powerQuality` for each UPS: input/output voltage, input/output frequency, battery voltage, UPS temperature, nominal voltage, derived warning band, AVR state, bypass state, overload state, and grid voltage state.

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
| `host` | required (or `127.0.0.1` when `is_host_loopback: true`) | Hostname or IP |
| `user` | required | SSH username |
| `connect_timeout` | `10` | SSH connection timeout |
| `command_timeout` | `30` | Default timeout for remote commands |
| `shutdown_command` | `sudo shutdown -h now` | Final shutdown command |
| `use_sudo` | `false` | Prefix generated privileged actions and non-sudo final shutdown commands with `sudo -n`. Useful for non-root loopback or remote users with NOPASSWD sudo |
| `ssh_key_path` | `null` | Optional SSH private-key path, useful for container/Kubernetes volume mounts |
| `ssh_options` | `[]` | Extra SSH options. Avoid disabling host-key checks in production |
| `pre_shutdown_commands` | `[]` | Pre-shutdown actions or commands. For loopback entries Eneru generates these from the local config — don't duplicate |
| `pre_shutdown_commands[].mounts` | `[]` | Mounts for `action: unmount_filesystems` on ordinary remote servers. Loopback entries derive mounts from `filesystems.unmount.mounts` |
| `shutdown_order` | unset | Explicit phase. Same value runs in parallel; higher values run later |
| `parallel` | unset | Legacy mode. `false` runs before the default parallel batch. Mutually exclusive with `shutdown_order` |
| `shutdown_safety_margin` | `60` | Extra wait budget for parallel server threads |
| `is_host_loopback` | `false` | **v5.5.** Mark this entry as the host-loopback delegate for the containerized OCI deployment. See [Remote servers](remote-servers.md#v55-host-loopback-delegate-container-only) |
| `host_identity_command` | `cat /etc/machine-id` | **v5.5.** Safe SSH probe used to verify the loopback target is really this container's host. Only used when `is_host_loopback: true` |
| `expected_host_identity` | auto-populated from `/etc/machine-id` inside the container | **v5.5.** Expected output of `host_identity_command`. Auto-populated at startup so operators bind-mount `/etc/machine-id` instead of supplying a value |

### Pre-shutdown action templates

Eneru ships a registry of SSH-side templates under predefined `action`
names. Use them in `pre_shutdown_commands[].action` for regular remote
servers (the loopback gets them auto-injected by Eneru from the local
config — don't write them manually there).

| `action` | What it does |
|---|---|
| `sync` | `sync; sync; sleep 2` — flushes filesystem caches |
| `stop_containers` | Stops all running Docker and Podman containers. v5.5: honors mandatory self-skip for the Eneru container when delegated |
| `stop_containers_rootless` | **v5.5 (new).** Same as `stop_containers` but iterates rootless Podman per non-system user via `loginctl` + `sudo -u` |
| `stop_compose` | Compose `down` for the given `path`. v5.5: skips stacks that include the Eneru container when delegated |
| `stop_vms` | Graceful `virsh shutdown` of all running libvirt VMs, then force-destroy after the configured timeout |
| `unmount_filesystems` | **v5.5 (new).** Iterates per-mount `umount` with configurable options. Regular remotes provide `pre_shutdown_commands[].mounts`; loopback derives mounts from the local filesystem config |
| `stop_proxmox_vms` / `stop_proxmox_cts` | Proxmox QEMU VM and LXC container teardown via `qm` / `pct` (sudo) |
| `stop_xcpng_vms` | XCP-ng / XenServer VMs via `xe` |
| `stop_esxi_vms` | VMware ESXi VMs via `vim-cmd` |

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
| `triggers` | inherits | Per-group trigger overrides. `depletion.window` is not supported here; set it globally or on `ups[*].triggers` |
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
| `run` | `--api` | Enable the embedded read-only API for this run |
| `run` | `--api-bind ADDRESS` | API listen address for this run; implies `--api` |
| `run` | `--api-port PORT` | API listen port for this run; implies `--api` |
| `run` | `--exit-after-shutdown` | Exit after a shutdown sequence finishes |
| `monitor` | `--once` | Print one status snapshot |
| `monitor` | `--interval` | TUI refresh interval |
| `monitor` | `--graph {charge,load,voltage,runtime}` | Initial graph metric (interactive: cycle with `<G>`) |
| `monitor` | `--time {1h,6h,24h,7d,30d}` | **Graph** time range (interactive: cycle with `<T>`). Does NOT affect the events list — events have no time window |
| `monitor` | `--events-only` | Print recent events only |
| `monitor` | `--verbose`, `-v` | Increase event verbosity: default shows Power Events, `-v` adds Diagnostics, `-vv` adds Lifecycle; `<V>` cycles in-session |
| `monitor` | `--length N` | With `--once`: max events to print (default 30, 0 = no cap). Caps preserve Power, then Diagnostics, then Lifecycle |

Example package commands:

```bash
sudo eneru validate --config /etc/ups-monitor/config.yaml
sudo eneru run --dry-run --config /etc/ups-monitor/config.yaml
sudo eneru monitor --once --events-only --config /etc/ups-monitor/config.yaml
sudo eneru monitor --once --events-only -vv --length 100 --config /etc/ups-monitor/config.yaml
```
