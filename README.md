# UPS Monitor Installation Guide

## Prerequisites

Install required packages:
```bash
# Debian/Ubuntu
sudo apt install nut-client bc sshpass

# Fedora/RHEL
sudo dnf install nut-client bc sshpass

# Arch Linux
sudo pacman -S nut bc sshpass
```

## Installation Steps

### 1. Install the monitoring script

```bash
sudo nano /usr/local/bin/ups-monitor.sh
```

Copy the script content, save and make it executable:

```bash
sudo chmod +x /usr/local/bin/ups-monitor.sh
```

### 2. Configure the script

Edit the configuration variables at the top of the script:

```bash
sudo nano /usr/local/bin/ups-monitor.sh
```

Adjust these values as needed:
- `UPS_NAME`: Your UPS identifier (default: "UPS@192.168.178.11")
- `DRY_RUN_MODE`: Set to "true" to test without actual shutdown (default: "false")
- `LOW_BATTERY_THRESHOLD`: Battery percentage for immediate shutdown (default: 20%)
- `CRITICAL_DEPLETION_RATE`: %/minute depletion rate trigger (default: 2.0)
- `CRITICAL_RUNTIME_THRESHOLD`: Minimum runtime in seconds (default: 180s)
- `CHECK_INTERVAL`: Seconds between checks (default: 1s - aggressive monitoring)
- `REMOTE_NAS_USER`, `REMOTE_NAS_HOST`, `REMOTE_NAS_PASSWORD`: Remote NAS credentials

**IMPORTANT SECURITY NOTE**: Store credentials securely. Consider using SSH keys instead of passwords.

**DRY-RUN MODE**: Always test with `DRY_RUN_MODE="true"` first to verify the logic works correctly!

### 3. Install the systemd service

```bash
sudo nano /etc/systemd/system/ups-monitor.service
```

Copy this content:

```ini
[Unit]
Description=UPS Monitor Service
After=network-online.target libvirtd.service docker.service
Wants=network-online.target
Documentation=man:systemd.service(5)

[Service]
Type=simple
ExecStart=/usr/local/bin/ups-monitor.sh
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

# Security hardening
NoNewPrivileges=true
PrivateTmp=true

# Required capabilities for shutdown
AmbientCapabilities=CAP_SYS_BOOT

[Install]
WantedBy=multi-user.target
```

Save and exit (Ctrl+X, Y, Enter).

### 4. Enable and start the service

```bash
# Reload systemd to recognize the new service
sudo systemctl daemon-reload

# Enable the service to start on boot
sudo systemctl enable ups-monitor.service

# Start the service
sudo systemctl start ups-monitor.service
```

## Management Commands

### Check service status
```bash
sudo systemctl status ups-monitor.service
```

### View real-time logs
```bash
sudo journalctl -u ups-monitor.service -f
```

### View log file
```bash
sudo tail -f /var/log/ups-monitor.log
```

### Check current UPS state
```bash
cat /var/run/ups-monitor.state
```

### Stop the service
```bash
sudo systemctl stop ups-monitor.service
```

### Restart the service
```bash
sudo systemctl restart ups-monitor.service
```

## Testing

### Test UPS connectivity
```bash
upsc UPS@192.168.178.11
```

### Test in Dry-Run Mode (RECOMMENDED FIRST)
1. Edit the script and set `DRY_RUN_MODE="true"`
2. Restart the service: `sudo systemctl restart ups-monitor.service`
3. Watch the logs: `sudo journalctl -u ups-monitor.service -f`
4. All shutdown actions will be logged but not executed
5. Look for `[DRY-RUN]` prefixed log entries

### Monitor real-time UPS status
```bash
watch -n 1 'cat /var/run/ups-monitor.state'
```

### Check battery depletion tracking
```bash
cat /var/run/ups-battery-history
```

### Verify libvirt VMs are detected
```bash
virsh list --all
```

### Cancel a scheduled shutdown manually
```bash
sudo shutdown -c
sudo rm -f /var/run/ups-shutdown-scheduled
```

## Features

### Intelligent Shutdown Logic
The script uses **multiple safety triggers** to ensure your system shuts down before power loss:

1. **Battery Level**: Immediate shutdown at <20% (configurable)
2. **Runtime Remaining**: Shutdown if <3 minutes of runtime left
3. **Depletion Rate**: Monitors how fast battery drains (default: >2%/min triggers shutdown)
4. **Time Limit**: Safety net - shutdown after 5 minutes on battery regardless of level

This multi-layered approach adapts to your actual load and ensures safe shutdown even under varying conditions.

### Aggressive Monitoring
- Checks UPS status **every second** for rapid response
- Tracks battery depletion rate over 60-second rolling window
- Logs detailed status every 10 seconds while on battery

### Controlled Shutdown Sequence
When shutdown is triggered, the script executes:
1. **Shuts down all libvirt VMs** (graceful shutdown, then force destroy if needed)
2. Stops all Docker containers
3. Syncs all filesystem buffers (3x)
4. Unmounts `/mnt/media` and `/mnt/nas`
5. Sends shutdown command to remote NAS via SSH
6. Final filesystem sync
7. Immediate local server shutdown

**Dry-Run Mode**: Test the entire sequence without actually executing shutdowns by setting `DRY_RUN_MODE="true"`

### Automatic Shutdown Cancellation
- Automatically cancels shutdown if power is restored
- Clears battery history when back on line power
- Users see notification that shutdown was cancelled

### Power Event Logging
- **Power Failures**: Logs when switching to battery
- **Power Restoration**: Logs when power returns
- **Brownouts**: Detects voltage drops >10V
- **Voltage Surges**: Detects voltage increases >10V
- **Overload**: Detects UPS overload conditions
- **Bypass Mode**: Detects when UPS is in bypass

### Logging Locations
- `/var/log/ups-monitor.log`: Detailed event log
- `journalctl -u ups-monitor.service`: System journal
- `logger` output: System log for critical events

## Troubleshooting

### Service won't start
```bash
# Check for syntax errors
sudo bash -n /usr/local/bin/ups-monitor.sh

# Check permissions
ls -l /usr/local/bin/ups-monitor.sh

# Verify upsc is installed
which upsc
upsc -l
```

### Cannot connect to UPS
```bash
# Test upsc connectivity
upsc UPS@192.168.178.11

# List available UPS devices
upsc -l

# Check NUT client configuration
cat /etc/nut/nut.conf
```

### Shutdown not triggering
Check the logs and verify:
- Battery threshold is set correctly
- UPS is reporting battery level
- Service has proper permissions

## Security Notes

- The service runs with minimal privileges
- Uses `AmbientCapabilities=CAP_SYS_BOOT` only for shutdown capability
- Temporary files are isolated with `PrivateTmp=true`
- No privilege escalation allowed with `NoNewPrivileges=true`

**CRITICAL SECURITY WARNING**: The script stores the remote NAS password in plaintext.

## Customization

You can modify the shutdown sequence in the `execute_shutdown_sequence()` function to:
- Add more Docker commands (stop specific containers, save states)
- Unmount additional filesystems
- Shutdown multiple remote hosts
- Send notifications via email, SMS, Discord, Slack webhooks
- Execute custom backup scripts
- Gracefully stop databases (MySQL, PostgreSQL)
- Close VPN connections

### Example: Add database shutdown
```bash
# Before unmounting, add:
log_message "Stopping PostgreSQL..."
systemctl stop postgresql
```

### Example: Multiple remote hosts
```bash
# Add after NAS shutdown:
for host in server1 server2 server3; do
    log_message "Shutting down $host..."
    ssh -i ~/.ssh/shutdown_key root@$host "shutdown -h now"
done
```
