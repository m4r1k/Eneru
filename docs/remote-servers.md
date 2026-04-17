# Remote server setup

Eneru can shut down remote servers via SSH during a power event: NAS devices (Synology, QNAP, TrueNAS), additional servers sharing the same UPS, network equipment with SSH access, or any system that needs coordinated shutdown.

---

## Configuration

```yaml
remote_servers:
  # Proxmox hypervisor - stop VMs/CTs before shutdown
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
      - action: "sync"
    shutdown_command: "shutdown -h now"

  # NAS - shutdown LAST since other servers mount its storage
  - name: "Synology NAS"
    enabled: true
    host: "192.168.1.50"
    user: "admin"
    parallel: false  # Shutdown after all parallel servers
    shutdown_command: "sudo -i synoshutdown -s"
    ssh_options:
      - "-o StrictHostKeyChecking=no"
```

### Configuration options

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
| `pre_shutdown_commands` | `[]` | Commands to run before shutdown |
| `parallel` | `true` | Shutdown concurrently with other parallel servers |

---

## Pre-shutdown commands

Eneru can run a sequence of commands on remote servers before the final shutdown command.

### Predefined actions

| Action | Description |
|--------|-------------|
| `stop_containers` | Stop all Docker/Podman containers |
| `stop_vms` | Gracefully shutdown libvirt/KVM VMs (then force-destroy remaining) |
| `stop_proxmox_vms` | Gracefully shutdown Proxmox QEMU VMs (then force-stop remaining) |
| `stop_proxmox_cts` | Gracefully shutdown Proxmox LXC containers (then force-stop remaining) |
| `stop_xcpng_vms` | Gracefully shutdown XCP-ng/XenServer VMs (then force-shutdown remaining) |
| `stop_esxi_vms` | Gracefully shutdown VMware ESXi VMs (then force-off remaining) |
| `stop_compose` | Stop a compose stack (requires `path` parameter) |
| `sync` | Sync filesystems before shutdown |

### Example: Proxmox server

```yaml
- name: "Proxmox Host"
  enabled: true
  host: "192.168.1.60"
  user: "root"
  pre_shutdown_commands:
    - action: "stop_proxmox_vms"
      timeout: 180  # Give VMs 3 minutes for graceful shutdown
    - action: "stop_proxmox_cts"
      timeout: 60
    - action: "sync"
  shutdown_command: "shutdown -h now"
```

### Example: Docker server with custom commands

```yaml
- name: "Docker Server"
  enabled: true
  host: "192.168.1.70"
  user: "root"
  command_timeout: 30  # Default timeout for commands
  pre_shutdown_commands:
    - action: "stop_compose"
      path: "/opt/myapp/docker-compose.yml"
      timeout: 120
    - action: "stop_containers"
      timeout: 60
    - command: "systemctl stop my-critical-service"  # Custom command
      timeout: 30
    - action: "sync"
  shutdown_command: "shutdown -h now"
```

### Error handling

Pre-shutdown commands are **best-effort**:

- Errors are logged but don't prevent the shutdown sequence from continuing
- Timeouts are logged with a warning, then the next command runs
- The final shutdown command always runs (unless SSH connection fails entirely)

---

## Shutdown ordering

By default, all enabled remote servers shut down concurrently using threads, so one slow or unreachable server does not block others.

### Multi-phase shutdown with `shutdown_order`

Use `shutdown_order` to define phases when servers have dependencies. Servers with the same `shutdown_order` run in parallel; different orders run sequentially (ascending). A server alone in its order effectively runs sequentially.

```yaml
remote_servers:
  # Phase 1: Compute servers shutdown in parallel
  - name: "App Server 1"
    enabled: true
    host: "192.168.1.10"
    user: "root"
    shutdown_order: 1
    shutdown_command: "shutdown -h now"

  - name: "App Server 2"
    enabled: true
    host: "192.168.1.11"
    user: "root"
    shutdown_order: 1
    shutdown_command: "shutdown -h now"

  # Phase 2: Storage shuts down alone (after compute releases NFS mounts)
  - name: "NAS"
    enabled: true
    host: "192.168.1.50"
    user: "admin"
    shutdown_order: 2
    shutdown_command: "sudo -i synoshutdown -s"

  # Phase 3: Network infrastructure shuts down last
  - name: "Router"
    enabled: true
    host: "192.168.1.254"
    user: "admin"
    shutdown_order: 3
    shutdown_command: "shutdown -h now"

  - name: "Switch"
    enabled: true
    host: "192.168.1.253"
    user: "admin"
    shutdown_order: 3
    shutdown_command: "shutdown -h now"
```

`shutdown_order` must be a positive integer (>= 1). Gaps are allowed (e.g., 1, 5, 10) — only relative order matters.

### Legacy `parallel` flag

For simple two-group setups, the `parallel` flag still works:

```yaml
remote_servers:
  - name: "App Server"
    enabled: true
    host: "192.168.1.10"
    user: "root"
    shutdown_command: "shutdown -h now"
    # parallel: true is the default

  - name: "NAS"
    enabled: true
    host: "192.168.1.50"
    user: "admin"
    parallel: false  # Shuts down before the parallel batch
    shutdown_command: "sudo -i synoshutdown -s"
```

### Behavior summary

| Config | Behavior |
|--------|----------|
| No `shutdown_order`, no `parallel` | Default: runs in the parallel batch |
| No `shutdown_order`, `parallel: true` | Runs in the parallel batch |
| No `shutdown_order`, `parallel: false` | Runs sequentially before the parallel batch |
| `shutdown_order: N` (no `parallel`) | Grouped with other order-N servers, all in parallel |
| `shutdown_order` + `parallel: true` or `false` | **Hard validation error** — pick one model |

!!! warning "shutdown_order and parallel are mutually exclusive"
    Setting both `shutdown_order` and `parallel` on the same server is rejected at config load time. Use `shutdown_order` for multi-phase ordering, or `parallel` for the legacy two-group behavior — never both.

!!! tip "Use `eneru validate` to preview"
    Run `eneru validate --config your-config.yaml` to see the shutdown sequence tree, including remote server phases.

### Tuning `shutdown_safety_margin`

Each remote server has a `shutdown_safety_margin` (seconds, default `60`) added on top of `pre_shutdown_commands + command_timeout + connect_timeout` when waiting for its parallel-shutdown thread to finish. The margin covers SSH session setup, OS scheduling jitter, and the brief window between the remote shutdown command starting and SSH closing the channel.

| When to tune | Suggested value |
|--------------|-----------------|
| Servers with battery-backed RAID, large write-back caches, or known slow shutdown paths | Raise (e.g. `120`–`300`) |
| Fast VMs or stateless containers where shutdown completes immediately | Lower (e.g. `10`–`30`) |
| Opt out of the buffer entirely (use only the explicit timeouts) | `0` |

The phase-wide join window uses the **maximum** `shutdown_safety_margin` across the servers in that phase, so tuning one slow server up does not penalise the others' actual execution time — it only extends the worst-case wait before Eneru moves to the next phase.

---

## SSH key setup

Passwordless SSH keys are required for unattended operation.

### 1. Generate SSH key

As root on the Eneru server (since Eneru runs as root):

```bash
sudo su
ssh-keygen -t ed25519 -f ~/.ssh/id_ups_shutdown -C "ups-monitor@$(hostname)"
```

Press Enter for no passphrase (required for unattended operation).

### 2. Copy key to remote server

```bash
ssh-copy-id -i ~/.ssh/id_ups_shutdown.pub user@remote-server
```

### 3. Test connection

```bash
# Should connect without password prompt
sudo ssh -i ~/.ssh/id_ups_shutdown user@remote-server "echo OK"
```

---

## Passwordless sudo

The shutdown command requires root privileges. Configure passwordless sudo for the specific command:

### Standard linux

On the remote server:

```bash
echo "username ALL=(ALL) NOPASSWD: /sbin/shutdown" | sudo tee /etc/sudoers.d/ups_shutdown
sudo chmod 0440 /etc/sudoers.d/ups_shutdown
```

### Synology DSM

```bash
echo "username ALL=(ALL) NOPASSWD: /usr/syno/sbin/synoshutdown -s" | sudo tee /etc/sudoers.d/ups_shutdown
sudo chmod 0440 /etc/sudoers.d/ups_shutdown
```

!!! warning "Synology note"
    Synology DSM resets `/etc/sudoers.d/` on updates. You may need to re-apply this after DSM updates, or use a scheduled task to maintain it.

### QNAP QTS

```bash
echo "username ALL=(ALL) NOPASSWD: /sbin/poweroff" | sudo tee /etc/sudoers.d/ups_shutdown
sudo chmod 0440 /etc/sudoers.d/ups_shutdown
```

### TrueNAS

TrueNAS does not use sudoers. Configure via the web UI:
1. Go to **System → Advanced → Init/Shutdown Scripts**
2. Or use the API to grant shutdown permissions

---

## Common shutdown commands

| System | Command |
|--------|---------|
| Standard Linux | `sudo shutdown -h now` |
| Synology DSM | `sudo -i synoshutdown -s` |
| QNAP QTS | `sudo /sbin/poweroff` |
| TrueNAS CORE | `sudo shutdown -p now` |
| TrueNAS SCALE | `sudo shutdown -h now` |
| ESXi | `sudo /bin/halt` |
| Proxmox VE | `sudo shutdown -h now` |
| pfSense/OPNsense | `sudo /sbin/shutdown -p now` |

---

## Testing remote shutdown

!!! danger "This will actually shut down the server!"
    Only run this when you're prepared for the server to go offline.

```bash
# Test the full command as root
sudo ssh user@remote-server "sudo shutdown -h now"
```

For a safer test, use a command that doesn't shut down:

```bash
# Verify SSH and sudo work
sudo ssh user@remote-server "sudo whoami"
# Should output: root
```

---

## Security considerations

### Host key verification

The example config includes `-o StrictHostKeyChecking=no` for initial setup. For production:

1. **Manually accept host keys once:**
   ```bash
   sudo ssh user@remote-server
   # Type 'yes' when prompted for host key
   ```

2. **Remove StrictHostKeyChecking from config:**
   ```yaml
   ssh_options: []  # or remove the line entirely
   ```

If a server's host key changes unexpectedly (potential MITM attack), the connection will fail rather than proceeding silently.

### Limiting sudo access

The sudoers rules above grant access only to the specific shutdown command.

### SSH key security

- Store keys in `/root/.ssh/` with permissions `600`
- Use a dedicated key (`id_ups_shutdown`) rather than the default key
- The key has no passphrase (required for unattended operation), so protect access to the Eneru server

---

## Troubleshooting

### Connection timeout

```
ERROR: SSH connection to 192.168.178.229 timed out
```

- Verify network connectivity: `ping 192.168.178.229`
- Check SSH service is running on remote server
- Verify firewall allows SSH (port 22)
- Increase `connect_timeout` if network is slow

### Permission denied

```
ERROR: Permission denied (publickey,password)
```

- Verify SSH key is copied: `ssh-copy-id -i ~/.ssh/id_ups_shutdown.pub user@host`
- Check key permissions: `ls -la ~/.ssh/id_ups_shutdown` (should be 600)
- Ensure you're running as root: `sudo ssh ...`

### Sudo password required

```
sudo: a password is required
```

- Verify sudoers rule is in place on remote server
- Check rule syntax: `sudo visudo -c`
- Ensure the command in sudoers matches exactly what Eneru runs

### Command not found

```
synoshutdown: command not found
```

- Use full path in shutdown command: `/usr/syno/sbin/synoshutdown`
- Or use `sudo -i` to get a login shell with full PATH
