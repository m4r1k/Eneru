# Remote servers

Eneru can shut down other systems over SSH before the local host powers off. Use this for NAS devices, hypervisors, Docker hosts, storage nodes, and network equipment that must leave cleanly during an outage.

## Minimal remote server

```yaml
remote_servers:
  - name: "NAS"
    enabled: true
    host: "192.168.1.50"
    user: "admin"
    shutdown_command: "sudo -i synoshutdown -s"
```

Eneru connects as `admin@192.168.1.50` and runs the final shutdown command.

## Pre-shutdown actions

Run cleanup commands before the final shutdown:

```yaml
remote_servers:
  - name: "Proxmox Host"
    enabled: true
    host: "192.168.1.60"
    user: "root"
    command_timeout: 30
    pre_shutdown_commands:
      - action: "stop_proxmox_vms"
        timeout: 180
      - action: "stop_proxmox_cts"
        timeout: 60
      - action: "unmount_filesystems"
        timeout: 20
        mounts:
          - path: "/mnt/backup"
            options: "-l"
      - action: "sync"
    shutdown_command: "shutdown -h now"
```

Pre-shutdown commands run in the order listed, before the final `shutdown_command`.
They are best effort: Eneru logs failures and continues to the final shutdown
command unless the remote phase deadline is exhausted.

| Action | What it does |
|--------|--------------|
| `stop_containers` | Stop all Docker or Podman containers |
| `stop_vms` | Stop libvirt/KVM VMs through `virsh` |
| `stop_proxmox_vms` | Stop Proxmox QEMU VMs through `qm` |
| `stop_proxmox_cts` | Stop Proxmox LXC containers through `pct` |
| `stop_xcpng_vms` | Stop XCP-ng or XenServer VMs |
| `stop_esxi_vms` | Stop VMware ESXi VMs |
| `stop_compose` | Stop a compose stack. Requires `path` |
| `unmount_filesystems` | Unmount the command's `mounts` list with optional `options` |
| `sync` | Flush remote filesystems |

Custom commands are also allowed:

```yaml
pre_shutdown_commands:
  - action: "stop_compose"
    path: "/opt/app/docker-compose.yml"
    timeout: 120
  - command: "systemctl stop my-critical-service"
    timeout: 30
  - action: "unmount_filesystems"
    timeout: 20
    mounts:
      - "/mnt/media"
      - path: "/mnt/backup disk"
        options: "-l"
  - action: "sync"
```

For ordinary remote servers, `unmount_filesystems` uses the mounts listed on
that command. For the v5.5 host-loopback delegate, leave `mounts` unset:
Eneru derives them from the local `filesystems.unmount.mounts` config so the
host's local mounts are declared once.

## Ordering

Use `shutdown_order` for dependencies. Servers with the same order run in parallel. Lower orders run first.

```yaml
remote_servers:
  - name: "App Server 1"
    enabled: true
    host: "192.168.1.10"
    user: "root"
    shutdown_order: 1

  - name: "App Server 2"
    enabled: true
    host: "192.168.1.11"
    user: "root"
    shutdown_order: 1

  - name: "NAS"
    enabled: true
    host: "192.168.1.50"
    user: "admin"
    shutdown_order: 2
    shutdown_command: "sudo -i synoshutdown -s"
```

This shuts down both app servers first, then the NAS.

### Remote phase timeline

| Time | Phase | What happens |
|------|-------|--------------|
| 0s | Shutdown sequence reaches remote servers | Eneru groups enabled servers by `shutdown_order` |
| 0s | Phase 1 starts | App Server 1 and App Server 2 run in parallel |
| During phase 1 | Per-server pre-shutdown | Each server uses its own `pre_shutdown_commands` list, with each command bounded by its configured timeout |
| During phase 1 | Final shutdown command | Each server runs `shutdown_command`, bounded by `command_timeout` |
| Phase deadline reached or all threads finish | Join window closes | Eneru moves on when all phase-1 threads finish or the phase deadline expires |
| Next | Phase 2 starts | NAS runs after clients have released storage |
| Phase 2 done | Remote phase complete | Shutdown sequence continues to remaining local steps |

The legacy `parallel` flag still exists for old configs:

| Config | Behavior |
|--------|----------|
| No `shutdown_order`, no `parallel` | Default parallel batch |
| `parallel: true` | Default parallel batch |
| `parallel: false` | Sequential phase before the default parallel batch |
| `shutdown_order: N` | Explicit phase. Same N runs in parallel |
| Both `shutdown_order` and `parallel` | Validation error |

Do not use `parallel: false` to make a NAS run last. Use `shutdown_order` for that.

## Safety margin

```yaml
remote_servers:
  - name: "Storage"
    shutdown_safety_margin: 120
```

Eneru waits for remote shutdown threads using the command timeouts plus `shutdown_safety_margin`. The default is 60 seconds. Raise it for slow storage servers or battery-backed RAID. Lower it for fast, stateless VMs. Set `0` to use only explicit command timeouts.

### Safety-margin timeline

| Step | Timeout contribution |
|------|----------------------|
| SSH connection budget in phase calculation | `connect_timeout` is added once per server budget |
| Each pre-shutdown command | Command-specific `timeout`, or `command_timeout` when no per-command timeout is set |
| Final shutdown command | `command_timeout` |
| OS and SSH close delay | `shutdown_safety_margin` |
| Phase deadline | Worst-case server budget in that phase |

One slow server can extend the phase deadline, but it does not make other servers run sequentially. Servers in the same phase still start together. Individual SSH command execution also gets a 30-second subprocess buffer in `_run_remote_command`; the phase deadline calculation uses the configured command timeouts plus `connect_timeout` and `shutdown_safety_margin`.

### Timeout semantics

When a remote command exceeds its budget, Eneru kills the **local** SSH process (SIGKILL via `subprocess.run(timeout=…)`). The remote shell command may still be running on the target host afterwards. For example, a configured `pre_shutdown_commands` entry of `systemctl stop kubelet` that takes longer than its timeout will leave `systemctl` running unattended on the remote, even though Eneru has moved on to the next command. Set timeouts conservatively (longer than the worst-case successful runtime) so the local kill only happens for genuinely-stuck commands, and avoid `pre_shutdown_commands` entries that you can't tolerate being interrupted mid-flight.

## SSH key setup

Eneru normally runs as root, so create and test the key as root on the Eneru host.

```bash
sudo install -d -m 700 /root/.ssh
sudo ssh-keygen -t ed25519 -f /root/.ssh/id_ups_shutdown -C "ups-monitor@$(hostname)"
sudo ssh-copy-id -i /root/.ssh/id_ups_shutdown.pub user@remote-server
sudo ssh -i /root/.ssh/id_ups_shutdown user@remote-server "echo OK"
```

Leave the key without a passphrase. A passphrase-protected key cannot be used unattended during a power event.

If you use a non-default key, set `ssh_key_path`:

```yaml
remote_servers:
  - name: "NAS"
    enabled: true
    host: "nas.example.lan"
    user: "ups"
    ssh_key_path: "/var/lib/eneru/ssh/id_ups_shutdown"
```

Eneru passes that path to OpenSSH as `-i <path>` for both remote shutdown commands and remote-health probes. This is the preferred form for Docker, Podman, and Kubernetes because the key can be mounted as a volume or Secret.

The older `ssh_options` form still works for advanced OpenSSH settings:

```yaml
ssh_options:
  - "-i"
  - "/root/.ssh/id_ups_shutdown"
```

## Host-key verification

Accept host keys deliberately before relying on shutdown:

```bash
sudo ssh user@remote-server "echo OK"
```

For Docker, Podman, or Kubernetes you don't configure host-key trust at
all. Eneru defaults every remote to `StrictHostKeyChecking=accept-new`:
on the first connection it records the remote's host key and reuses it
afterward, failing closed only if that key later changes.

Bare-metal installs use the running user's normal OpenSSH trust store
(`~/.ssh/known_hosts`, such as `/root/.ssh/known_hosts` when the daemon
runs as root). Containers keep the documented SSH mount contract: the
Docker/Podman bind mount `/srv/eneru/ssh:/var/lib/eneru/ssh` stores both
the private key and `known_hosts`, with the private key itself locked down
to mode `0400`. Kubernetes keeps the private key in a read-only Secret and
sets `ENERU_SSH_KNOWN_HOSTS_FILE=/var/lib/eneru/known_hosts`, which is on
the writable state PVC.

```yaml
# config.yaml — no ssh_options needed
remote_servers:
  - name: "NAS"
    enabled: true
    host: "nas.example.lan"
    user: "ups"
    ssh_key_path: "/var/lib/eneru/ssh/id_ups_shutdown"
```

`accept-new` trusts whatever answers on that first connection
(trust-on-first-use), so do the first start on a network you trust. To
pre-verify keys out of band instead, set `StrictHostKeyChecking=yes` with
a pre-populated `UserKnownHostsFile` per remote — Eneru leaves any
`StrictHostKeyChecking` you set untouched.

After the first start, confirm each target is trusted:

```bash
curl -s http://localhost:9191/api/v1/ups \
  | jq '.ups[].remoteHealth[] | {server, host, status}'
# every remote you depend on must report "status": "HEALTHY"
```

Do not leave `StrictHostKeyChecking=no` in production. If a host key changes unexpectedly, SSH should fail closed instead of sending shutdown commands to an untrusted host.

## Passwordless sudo

The SSH user needs passwordless access to the exact commands Eneru runs.

### Standard Linux

```bash
echo "username ALL=(ALL) NOPASSWD: /sbin/shutdown, /usr/bin/systemctl, /bin/sync" \
  | sudo tee /etc/sudoers.d/ups_shutdown
sudo chmod 0440 /etc/sudoers.d/ups_shutdown
sudo visudo -c
```

### Synology DSM

```bash
echo "username ALL=(ALL) NOPASSWD: /usr/syno/sbin/synoshutdown -s" \
  | sudo tee /etc/sudoers.d/ups_shutdown
sudo chmod 0440 /etc/sudoers.d/ups_shutdown
```

DSM updates can reset sudoers changes. Re-check after DSM upgrades.

### Proxmox VE

`qm` and `pct` need root. If the SSH user is not root:

```bash
echo "username ALL=(ALL) NOPASSWD: /usr/sbin/qm, /usr/sbin/pct, /sbin/shutdown" \
  | sudo tee /etc/sudoers.d/ups_proxmox
sudo chmod 0440 /etc/sudoers.d/ups_proxmox
sudo visudo -c
```

### TrueNAS SCALE

Use the Web UI. Go to Credentials, Local Users, edit the SSH user, and allow the exact sudo command you plan to run. For current SCALE releases, use:

```text
/usr/bin/midclt call system.shutdown "ups_event"
```

### TrueNAS CORE and FreeBSD appliances

Use the platform's user or sudo configuration UI where available. The shutdown command is usually:

```text
sudo shutdown -p now
```

## Common shutdown commands

These commands match the previously documented, validated shutdown forms. Keep platform-specific forms unless you have tested an alternative on that device.

| System | Command | Notes |
|--------|---------|-------|
| Standard Linux (systemd) | `sudo systemctl poweroff` | Modern, unambiguous form |
| Standard Linux (portable) | `sudo shutdown -h now` | Works on systemd and SysV; on systemd `-h` powers off |
| Synology DSM | `sudo -i synoshutdown -s` | DSM 6/7. On DSM 7, `sudo poweroff` is also valid |
| QNAP QTS | `sudo /sbin/poweroff` | |
| TrueNAS CORE | `sudo shutdown -p now` | `-p` cuts power on FreeBSD; `-h` only halts |
| TrueNAS SCALE (>= 25.04) | `sudo /usr/bin/midclt call system.shutdown "ups_event"` | Vendor-recommended; orchestrates app/VM teardown. `reason` arg required since Fangtooth |
| TrueNAS SCALE (< 25.04) | `sudo /usr/bin/midclt call system.shutdown` | Same as above without the mandatory reason arg |
| VMware ESXi 7.x/8.x | `esxcli system shutdown poweroff --reason="UPS power event"` | No `sudo` on ESXi when SSH login is root. Reason is logged to vmkernel.log |
| Proxmox VE | `sudo shutdown -h now` | Stop guests first with `stop_proxmox_vms` and `stop_proxmox_cts` pre-shutdown actions |
| pfSense / OPNsense | `sudo /sbin/shutdown -p now` | FreeBSD `-p` for ACPI power-off |

## Safe test checklist

Run these before relying on remote shutdown:

```bash
sudo ssh user@remote-server "echo OK"
sudo ssh user@remote-server "sudo -n true && echo sudo OK"
sudo eneru validate --config /etc/ups-monitor/config.yaml
sudo eneru run --dry-run --config /etc/ups-monitor/config.yaml
```

Eneru can also run advisory SSH healthchecks. These use a dedicated harmless probe command (`true` by default) and never execute configured pre-shutdown or shutdown commands:

```yaml
remote_health:
  enabled: true
  startup_check: true
  interval: 3600
  probe_command: "true"
```

Remote health is enabled by default for configured remote servers. Health status appears in the TUI, API, MQTT payload, and Prometheus metrics. The daemon runs only the safe `probe_command`; those read-only surfaces only consume live manager state or sidecar JSON. An unreachable target becomes `DEGRADED`, then `FAILED` at `failure_threshold`; Eneru sends one failure notification per failed period and one recovery notification when it returns. The health signal is advisory: during a real shutdown sequence, Eneru still attempts each configured remote pre-shutdown command and final shutdown command with bounded timeouts.

## Discovering configured targets

`eneru remote list` prints every remote target across UPS groups and redundancy groups:

```bash
eneru remote list --config /etc/ups-monitor/config.yaml
```

```text
REMOTE TARGETS (3 configured, 2 enabled)

NAME          GROUP       KIND        HOST                    ENABLED  ORDER
Synology NAS  UPS-A       ups         nas-admin@192.168.1.10  yes      10
Proxmox-1     UPS-A       ups         root@192.168.1.20       yes      5
dev-box       rack-pair   redundancy  ubuntu@dev.local        no       —
```

The `NAME` column is what `--server` accepts. The `GROUP` column is the exact string `--group` accepts; `KIND` distinguishes UPS groups from redundancy groups when names happen to collide. `ORDER` is the value `compute_effective_order` resolves over the **enabled** servers in that group (matching what the daemon actually uses); disabled rows show `—` because they don't participate in the rotation at all. The command exits non-zero with a friendly note when no remote targets are configured.

## Manual remote shutdown drill

To test one configured remote target through Eneru's SSH command path without waiting for a UPS event:

```bash
sudo eneru shutdown remote --config /etc/ups-monitor/config.yaml --server NAS --dry-run
```

Dry-run mode does not execute configured remote commands. It may run the harmless connectivity probe so you can verify SSH access.

Real execution requires an intentionally long confirmation flag:

```bash
sudo eneru shutdown remote --config /etc/ups-monitor/config.yaml --server NAS \
  --i-really-want-to-proceed-with-remote-shutdown
```

This command only targets the selected remote server. It does not run local VM shutdown, local container shutdown, filesystem unmounts, local poweroff, or whole-group drain.

### Full-sequence rehearsal

When you want to verify the entire configured shutdown sequence for one group, use `shutdown group` instead. It covers multi-server `shutdown_order`, VMs, containers, filesystems, then per-server pre-shutdown commands and shutdown commands:

```bash
sudo eneru shutdown group --config /etc/ups-monitor/config.yaml --group rack-a --dry-run
```

Behaviour by group kind:

- **UPS group** (matched by `display_name` or `name`): runs `_execute_shutdown_sequence` end to end. With `--i-really-want-to-proceed-with-group-shutdown`, the final `local_shutdown.command` will fire if `local_shutdown.enabled`, halting the host.
- **Redundancy group** (matched by `redundancy_groups[*].name`): runs `RedundancyGroupExecutor.shutdown` end to end. Local poweroff is **not** wired in the rehearsal even with the confirm flag. The coordinator's poweroff callback is intentionally absent so an operator cannot accidentally halt the host with a "rehearsal". To exercise that path, run `eneru run` with `behavior.dry_run: true` against the same config.

The rehearsal isolates its own `shutdown_flag_file` / `state_file` / `battery_history_file` in a per-invocation temp directory so it never collides with a running daemon.

When in doubt, `eneru remote list` shows the names you can pass to `--group` and `--server`.

### Dry-run precedence

The drill respects the `--dry-run` CLI flag, **not** the daemon's
`behavior.dry_run` config setting. The two are deliberately decoupled:

- `behavior.dry_run: true` in your config protects the **daemon** —
  scheduled and event-triggered shutdowns simulate without running real
  commands. It applies to every shutdown the daemon initiates.
- `--dry-run` on the drill protects **this single invocation** of the
  drill — it tells `eneru shutdown remote` whether you want a
  simulation or a real test, regardless of the daemon's config.

The drill's safety contract is the
`--i-really-want-to-proceed-with-remote-shutdown` flag (line 426-431
of `cli.py` enforces it). If you typed that flag without `--dry-run`,
you've explicitly asked for real execution — config-level dry-run does
not silently override that choice. If you want the drill to honor the
daemon's dry-run config too, run with `--dry-run`.

Only test the final shutdown command when you are prepared for the remote server to power off:

```bash
sudo ssh user@remote-server "sudo shutdown -h now"
```

## v5.5: host-loopback delegate (container only)

When Eneru runs inside an OCI container with local capabilities
configured (`is_local: true` + VMs / containers / filesystems /
local_shutdown), it cannot execute those local actions directly —
namespace isolation blocks host poweroff and host umount, even with
`--privileged`. The v5.5 answer: configure one `remote_servers`
entry that points at `127.0.0.1` (or whatever address inside the
container reaches the host) and mark it `is_host_loopback: true`.
Eneru SSHes to that target and lets the host's `sshd` perform the
privileged work.

For the **Docker/Podman + zero-config** case, Eneru auto-synthesizes
the loopback entry at startup with the documented defaults. You
don't need to write a `remote_servers` block at all — your existing
deb/rpm-shaped config just works. See [Choose your
install](install-comparison.md) and [Migrate to
container](migrate-to-container.md).

For the explicit form (overrides synthesis):

```yaml
remote_servers:
  - name: host-loopback
    enabled: true
    host: 127.0.0.1
    user: eneru-loopback
    is_host_loopback: true
    use_sudo: true
    shutdown_command: "shutdown -h now"
    ssh_key_path: /var/lib/eneru/ssh/id_loopback
```

The loopback entry behaves like any other `remote_servers` entry
with three additions:

1. **`is_host_loopback: true`** — at most one per config; the
   privilege check accepts non-root in a container when this is
   present, and the local-host shutdown phases are delegated to it
   instead of running in-process.
2. **Host identity guard.** A second probe (`host_identity_command`,
   default `cat /etc/machine-id`) verifies the SSH target really is
   this container's host. `expected_host_identity` is auto-populated
   from the container's `/etc/machine-id` — operators bind-mount the
   host's value at the same path instead of supplying a literal
   string. Mismatch marks the loopback FAILED with a clear hint.

   The identity guard runs in the **background remote-health loop**
   (and gates `/ready`), where a mismatch is logged and notified. It is
   deliberately **not** run as a blocking probe at the start of the
   shutdown sequence: that would add an SSH round-trip on the critical
   path before the poweroff during an outage, and Eneru would proceed
   regardless (the configured host-loopback target is the host that must
   go down), so the probe would only add latency. Fix a reported
   mismatch from the health-loop warning, not at shutdown time.
3. **Eneru generates `pre_shutdown_commands` for you.** Don't
   duplicate VM / container / filesystem actions on the loopback —
   Eneru translates the local config into the equivalent SSH
   templates at startup. Any custom `pre_shutdown_commands` you add
   run AFTER Eneru's generated ones (do-the-work first, then your
   extras).

Do not use `authorized_keys command="..."` for the loopback key.
Forced commands make sshd replace Eneru's identity probe and generated
shutdown actions with that single command, so readiness can no longer
prove the configured behavior. The full walkthrough — SSH key
generation, root/default setup, sudoers, AppArmor/SELinux notes, and
hardening — lives in
[Containers and Kubernetes](containers-kubernetes.md#ssh-from-container-to-host-walkthrough).

## Troubleshooting

| Symptom | Check |
|---------|-------|
| `Permission denied (publickey,password)` | Copy the key for the same user Eneru uses, and test as root from the Eneru host |
| SSH timeout | Network path, firewall, SSH service, `connect_timeout` |
| `sudo: a password is required` | Add or fix the sudoers rule. Use `sudo -n` in tests |
| `command not found` | Use full command paths or a login shell where the platform requires one |
| NAS shuts down too early | Use `shutdown_order` instead of legacy `parallel: false` |
| Host key verification fails | Re-check the host identity before accepting the new key |
| **v5.5 loopback** `host identity mismatch` | Add `-v /etc/machine-id:/etc/machine-id:ro` (plain `:ro` only — never `:Z` or `:z`; relabel persists on disk and breaks dbus-broker on the next reboot). |
| **v5.5 loopback** `PermissionError` on SSH key | Container user (uid 10001) can't read the bind-mounted key. Use a dedicated dir with mode 0755, set the key to mode 0400 or 0600, and make uid 10001 the owner or grant it an ACL |
