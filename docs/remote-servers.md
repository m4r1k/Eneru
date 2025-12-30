# Remote Server Setup

Eneru can shut down remote servers via SSH during a power event. This is useful for:

- NAS devices (Synology, QNAP, TrueNAS)
- Additional servers sharing the same UPS
- Network equipment with SSH access
- Any system that needs coordinated shutdown

---

## Configuration

```yaml
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
    enabled: true
    host: "192.168.178.230"
    user: "admin"
    shutdown_command: "sudo shutdown -h now"
```

### Configuration Options

| Key | Default | Description |
|-----|---------|-------------|
| `name` | (required) | Display name for logging |
| `enabled` | `false` | Enable this server |
| `host` | (required) | Hostname or IP address |
| `user` | (required) | SSH username |
| `connect_timeout` | `10` | SSH connection timeout in seconds |
| `command_timeout` | `30` | Command execution timeout in seconds |
| `shutdown_command` | `sudo shutdown -h now` | Command to execute |
| `ssh_options` | `[]` | Additional SSH options |

---

## SSH Key Setup

For secure, passwordless authentication, set up SSH keys:

### 1. Generate SSH Key

As root on the Eneru server (since Eneru runs as root):

```bash
sudo su
ssh-keygen -t ed25519 -f ~/.ssh/id_ups_shutdown -C "ups-monitor@$(hostname)"
```

Press Enter for no passphrase (required for unattended operation).

### 2. Copy Key to Remote Server

```bash
ssh-copy-id -i ~/.ssh/id_ups_shutdown.pub user@remote-server
```

### 3. Test Connection

```bash
# Should connect without password prompt
sudo ssh -i ~/.ssh/id_ups_shutdown user@remote-server "echo OK"
```

---

## Passwordless Sudo

The shutdown command typically requires root privileges. Configure passwordless sudo for the specific command:

### Standard Linux

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

!!! warning "Synology Note"
    Synology DSM resets `/etc/sudoers.d/` on updates. You may need to re-apply this after DSM updates, or use a scheduled task to maintain it.

### QNAP QTS

```bash
echo "username ALL=(ALL) NOPASSWD: /sbin/poweroff" | sudo tee /etc/sudoers.d/ups_shutdown
sudo chmod 0440 /etc/sudoers.d/ups_shutdown
```

### TrueNAS

TrueNAS uses a different approach. Configure via the web UI:
1. Go to **System → Advanced → Init/Shutdown Scripts**
2. Or use the API to grant shutdown permissions

---

## Common Shutdown Commands

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

## Testing Remote Shutdown

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

## Security Considerations

### Host Key Verification

The example config includes `-o StrictHostKeyChecking=no` for convenience during initial setup. For production:

1. **Manually accept host keys once:**
   ```bash
   sudo ssh user@remote-server
   # Type 'yes' when prompted for host key
   ```

2. **Remove StrictHostKeyChecking from config:**
   ```yaml
   ssh_options: []  # or remove the line entirely
   ```

This ensures that if a server's host key changes unexpectedly (potential MITM attack), the connection will fail rather than proceeding silently.

### Limiting Sudo Access

The sudoers rules above grant access only to the specific shutdown command, not full root access. This follows the principle of least privilege.

### SSH Key Security

- Store keys in `/root/.ssh/` with restrictive permissions (600)
- Consider using a dedicated key (`id_ups_shutdown`) rather than the default key
- The key has no passphrase by design (unattended operation), so protect access to the Eneru server

---

## Troubleshooting

### Connection Timeout

```
ERROR: SSH connection to 192.168.178.229 timed out
```

- Verify network connectivity: `ping 192.168.178.229`
- Check SSH service is running on remote server
- Verify firewall allows SSH (port 22)
- Increase `connect_timeout` if network is slow

### Permission Denied

```
ERROR: Permission denied (publickey,password)
```

- Verify SSH key is copied: `ssh-copy-id -i ~/.ssh/id_ups_shutdown.pub user@host`
- Check key permissions: `ls -la ~/.ssh/id_ups_shutdown` (should be 600)
- Ensure you're running as root: `sudo ssh ...`

### Sudo Password Required

```
sudo: a password is required
```

- Verify sudoers rule is in place on remote server
- Check rule syntax: `sudo visudo -c`
- Ensure the command in sudoers matches exactly what Eneru runs

### Command Not Found

```
synoshutdown: command not found
```

- Use full path in shutdown command: `/usr/syno/sbin/synoshutdown`
- Or use `sudo -i` to get a login shell with full PATH
