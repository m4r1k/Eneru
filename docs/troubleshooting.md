# Troubleshooting

This guide covers common issues and how to resolve them.

---

## Service Management

### Basic Commands

```bash
# Start/stop/restart
sudo systemctl start eneru.service
sudo systemctl stop eneru.service
sudo systemctl restart eneru.service

# Check status
sudo systemctl status eneru.service

# View logs (follow mode)
sudo journalctl -u eneru.service -f

# View recent logs
sudo journalctl -u eneru.service -e

# View log file directly
sudo tail -f /var/log/ups-monitor.log
```

---

## Service Won't Start

### Check for Errors

```bash
journalctl -u eneru.service -e
```

### Validate Python Version

Eneru requires Python 3.9 or higher:

```bash
python3 --version
```

If your version is older, you'll need to upgrade Python or use a distribution with a newer version.

### Check Dependencies

```bash
python3 -c "import yaml; print('PyYAML OK')"
python3 -c "import apprise; print('Apprise OK')"
```

If either fails, install the missing dependency:

```bash
# Debian/Ubuntu
sudo apt install python3-yaml apprise

# RHEL/Fedora
sudo dnf install python3-pyyaml apprise
```

### Validate Script Syntax

```bash
python3 -m py_compile /opt/ups-monitor/ups_monitor.py
```

If this produces errors, the script file may be corrupted. Reinstall the package.

### Validate Configuration

```bash
sudo python3 /opt/ups-monitor/ups_monitor.py --validate-config
```

This checks for YAML syntax errors and invalid configuration values.

---

## Cannot Connect to UPS

### Test NUT Connection

```bash
upsc UPS@192.168.178.11
```

This should display all UPS variables. If it fails:

1. **Check NUT server is running:**
   ```bash
   systemctl status nut-server
   ```

2. **Verify network connectivity:**
   ```bash
   ping 192.168.178.11
   ```

3. **Check NUT server allows remote connections:**
   On the NUT server, verify `/etc/nut/upsd.conf` has:
   ```
   LISTEN 0.0.0.0 3493
   ```
   And `/etc/nut/upsd.users` has appropriate user configuration.

4. **Check firewall:**
   NUT uses port 3493 by default.

### Verify UPS Name

The UPS name in your config must match exactly what NUT reports:

```bash
upsc -l 192.168.178.11
```

This lists all UPS names on that server.

---

## Notifications Not Working

### Test Built-in Command

```bash
sudo python3 /opt/ups-monitor/ups_monitor.py --test-notifications
```

### Test Apprise Directly

```bash
python3 -c "
import apprise
ap = apprise.Apprise()
ap.add('discord://webhook_id/webhook_token')
result = ap.notify(body='Test from Apprise', title='Test')
print('Success' if result else 'Failed')
"
```

### Common Issues

1. **Wrong URL format:** Each service has a specific format. Check the [Apprise Wiki](https://github.com/caronc/apprise/wiki).

2. **Network issues:** Can the server reach the notification service?
   ```bash
   curl -I https://discord.com
   ```

3. **Rate limiting:** If testing frequently, you may hit service rate limits.

4. **Firewall blocking outbound:** Ensure HTTPS (443) is allowed outbound.

---

## Remote Shutdown Fails

See also: [Remote Servers](remote-servers.md#troubleshooting)

### Test SSH Connection

```bash
# As root (Eneru runs as root)
sudo ssh user@remote-server "echo OK"
```

### Test Sudo Access

```bash
sudo ssh user@remote-server "sudo -n true && echo 'sudo OK'"
```

If this prompts for a password, the sudoers rule is not configured correctly.

### Check SSH Key Permissions

```bash
ls -la ~/.ssh/id_*
```

Keys should be mode 600 (readable only by owner).

---

## Dry-Run Mode for Testing

Test the full shutdown sequence without actually shutting anything down:

### Option 1: Config File

```yaml
behavior:
  dry_run: true
```

### Option 2: Command Line

```bash
sudo python3 /opt/ups-monitor/ups_monitor.py --dry-run
```

In dry-run mode, all actions are logged with `[DRY-RUN]` prefix but not executed.

### Simulate Power Failure

1. Enable dry-run mode
2. Optionally lower `extended_time.threshold` to trigger faster
3. Unplug UPS input power (or use NUT's test commands if available)
4. Watch logs for the shutdown sequence
5. Verify notifications arrive

```bash
# Watch logs during test
sudo journalctl -u eneru.service -f
```

---

## Clear Shutdown State

If a shutdown sequence is interrupted (e.g., you restored power mid-sequence during dry-run testing), clear the state file:

```bash
sudo rm -f /var/run/ups-shutdown-scheduled
```

This allows Eneru to trigger a new shutdown sequence if needed.

---

## Check Current UPS State

View what Eneru sees from the UPS:

```bash
cat /var/run/ups-monitor.state
```

This shows the current battery percentage, runtime, status, and other metrics.

---

## Example Log Output

### Normal Operation (Service Startup)

```
Dec 29 17:13:10 nuc.local python3[3366019]: Configuration loaded from: /etc/ups-monitor/config.yaml
Dec 29 17:13:10 nuc.local python3[3366019]: 2025-12-29 17:13:10 CET - 📢 Notifications: enabled (1 service(s))
Dec 29 17:13:10 nuc.local python3[3366019]: 2025-12-29 17:13:10 CET - 🐳 Container runtime detected: docker
Dec 29 17:13:10 nuc.local python3[3366019]: 2025-12-29 17:13:10 CET - 🚀 Eneru v4.3 starting - monitoring UPS@192.168.178.11
Dec 29 17:13:10 nuc.local python3[3366019]: 2025-12-29 17:13:10 CET - 📋 Enabled features: VMs, Containers (docker), FS Sync, Unmount (3 mounts), Remote (1 servers), Local Shutdown
Dec 29 17:13:10 nuc.local python3[3366019]: 2025-12-29 17:13:10 CET - ⏳ Checking initial connection to UPS@192.168.178.11...
Dec 29 17:13:10 nuc.local python3[3366019]: 2025-12-29 17:13:10 CET - ✅ Initial connection successful.
Dec 29 17:13:10 nuc.local python3[3366019]: 2025-12-29 17:13:10 CET - 📊 Voltage Monitoring Active. Nominal: 230.0V. Low Warning: 175.0V. High Warning: 275.0V.
```

### Power Failure Event

```
Dec 22 22:19:07 nuc.local python3[1274572]: 2025-12-22 22:19:07 CET - 🔄 Status changed: OL CHRG -> OB DISCHRG (Battery: 100%, Runtime: 28m 9s, Load: 29%)
Dec 22 22:19:07 nuc.local python3[1274572]: 2025-12-22 22:19:07 CET - ⚡ POWER EVENT: ON_BATTERY - Battery: 100%, Runtime: 1689 seconds, Load: 29%
Dec 22 22:19:10 nuc.local python3[1274572]: 2025-12-22 22:19:10 CET - 🔋 On battery: 100% (28m 5s), Load: 28%, Depletion: 0.0%/min, Time on battery: 3s
Dec 22 22:19:40 nuc.local python3[1274572]: 2025-12-22 22:19:40 CET - 🔋 On battery: 97% (19m 35s), Load: 23%, Depletion: 5.45%/min, Time on battery: 33s
Dec 22 22:20:10 nuc.local python3[1274572]: 2025-12-22 22:20:10 CET - 🔋 On battery: 96% (19m 5s), Load: 27%, Depletion: 3.81%/min, Time on battery: 1m 3s
Dec 22 22:20:45 nuc.local python3[1274572]: 2025-12-22 22:20:45 CET - 🔋 On battery: 93% (18m 31s), Load: 26%, Depletion: 4.29%/min, Time on battery: 1m 38s
Dec 22 22:21:15 nuc.local python3[1274572]: 2025-12-22 22:21:15 CET - 🔋 On battery: 93% (18m 1s), Load: 23%, Depletion: 3.28%/min, Time on battery: 2m 8s
```

### Power Restored

```
Dec 22 22:21:17 nuc.local python3[1274572]: 2025-12-22 22:21:17 CET - 🔄 Status changed: OB DISCHRG -> OL CHRG (Battery: 65%, Runtime: 17m 59s, Load: 18%)
Dec 22 22:21:17 nuc.local python3[1274572]: 2025-12-22 22:21:17 CET - ⚡ POWER EVENT: POWER_RESTORED - Battery: 65% (Status: OL CHRG), Input: 237.6V, Outage duration: 2m 10s
```

---

## Known Limitations

### Single UPS Only

Eneru monitors one UPS per instance. For multiple UPS units, run multiple instances with different config files:

```bash
sudo python3 /opt/ups-monitor/ups_monitor.py --config /etc/ups-monitor/ups1.yaml
sudo python3 /opt/ups-monitor/ups_monitor.py --config /etc/ups-monitor/ups2.yaml
```

### UPS Compatibility

Eneru works with any UPS supported by NUT. However, some UPS models may:

- Report inaccurate runtime estimates
- Have delayed battery percentage updates
- Not support all status flags

The multi-vector trigger system is designed to compensate for these issues.

---

## Getting Help

If you're still stuck:

1. **Check the logs** - Most issues are visible in the logs
2. **Enable dry-run** - Test safely without consequences
3. **Open an issue** - [GitHub Issues](https://github.com/m4r1k/Eneru/issues) with logs and config (redact sensitive data)
