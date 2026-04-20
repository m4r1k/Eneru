# Troubleshooting

Common issues and how to resolve them.

---

## Service management

### Basic commands

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

## Service won't start

### Check for errors

```bash
journalctl -u eneru.service -e
```

### Validate Python version

Eneru requires Python 3.9 or higher:

```bash
python3 --version
```

If your version is older, you'll need to upgrade Python or use a distribution with a newer version.

### Check dependencies

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

### Validate package syntax

```bash
# For installed package
python3 -c "import eneru; print('OK')"

# For development (from repository root)
python3 -m py_compile src/eneru/*.py
```

If this produces errors, the package may be corrupted. Reinstall it.

### Pip install shows UNKNOWN-0.0.0 (Ubuntu 22.04)

Ubuntu 22.04 ships pip 22.0.2, which has a regression with `pyproject.toml` dynamic version metadata. Running `pip install eneru` produces an `UNKNOWN-0.0.0` package and the `eneru` command is not available.

Fix by upgrading pip first:

```bash
pip install --upgrade pip
pip install eneru
```

Or use a virtualenv, which typically includes a newer pip:

```bash
python3 -m venv ~/.venv/eneru
source ~/.venv/eneru/bin/activate
pip install eneru
```

This does not affect `.deb` package installation, which works on Ubuntu 22.04 without issues.

### Validate configuration

```bash
eneru validate --config /etc/ups-monitor/config.yaml
```

This checks for YAML syntax errors, invalid configuration values, and multi-UPS ownership rules.

---

## Cannot connect to UPS

### Test NUT connection

```bash
upsc UPS@192.168.178.11
```

This should display all UPS variables. If it fails:

1. Check that the NUT server is running:
   ```bash
   systemctl status nut-server
   ```

2. Verify network connectivity:
   ```bash
   ping 192.168.178.11
   ```

3. Check that the NUT server allows remote connections.
   On the NUT server, verify `/etc/nut/upsd.conf` has:
   ```
   LISTEN 0.0.0.0 3493
   ```
   And `/etc/nut/upsd.users` has appropriate user configuration.

4. Check the firewall. NUT uses port 3493 by default.

### Verify UPS name

The UPS name in your config must match exactly what NUT reports:

```bash
upsc -l 192.168.178.11
```

This lists all UPS names on that server.

### Flaky NUT server (intermittent connection drops)

Some UPS devices with integrated NUT servers can be intermittently unreachable, causing notification storms. Eneru has a connection loss grace period that suppresses notifications during brief outages:

```yaml
ups:
  connection_loss_grace_period:
    enabled: true    # Suppress transient connection failures
    duration: 60     # Wait 60s before sending CONNECTION_LOST notification
    flap_threshold: 5  # Warn after 5 recoveries within 24h
```

If your NUT server flaps frequently, check:

1. Network stability between Eneru and the NUT server
2. NUT server logs for driver or USB errors (`journalctl -u nut-driver`)
3. UPS firmware updates from the manufacturer

See [Connection loss grace period](configuration.md#connection-loss-grace-period) for details.

---

## Notifications not working

### Test built-in command

```bash
sudo python3 /opt/ups-monitor/eneru.py --test-notifications
```

### Test Apprise directly

```bash
python3 -c "
import apprise
ap = apprise.Apprise()
ap.add('discord://webhook_id/webhook_token')
result = ap.notify(body='Test from Apprise', title='Test')
print('Success' if result else 'Failed')
"
```

### Common issues

1. Wrong URL format. Each service has a specific format; check the [Apprise Wiki](https://github.com/caronc/apprise/wiki).

2. Network issues. Can the server reach the notification service?
   ```bash
   curl -I https://discord.com
   ```

3. Rate limiting. If testing frequently, you may hit service rate limits.

4. Firewall blocking outbound. Ensure HTTPS (443) is allowed outbound.

---

## Remote shutdown fails

See also: [Remote servers](remote-servers.md#troubleshooting)

### Test SSH connection

```bash
# As root (Eneru runs as root)
sudo ssh user@remote-server "echo OK"
```

### Test sudo access

```bash
sudo ssh user@remote-server "sudo -n true && echo 'sudo OK'"
```

If this prompts for a password, the sudoers rule is not configured correctly.

### Check SSH key permissions

```bash
ls -la ~/.ssh/id_*
```

Keys should be mode 600 (readable only by owner).

---

## Dry-run mode for testing

Test the full shutdown sequence without actually shutting anything down:

### Option 1: Config file

```yaml
behavior:
  dry_run: true
```

### Option 2: Command line

```bash
sudo python3 /opt/ups-monitor/eneru.py --dry-run
```

In dry-run mode, all actions are logged with `[DRY-RUN]` prefix but not executed.

### Simulate power failure

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

## Clear shutdown state

If a shutdown sequence is interrupted (e.g., you restored power mid-sequence during dry-run testing), clear the state file:

```bash
sudo rm -f /var/run/ups-shutdown-scheduled
```

This allows Eneru to trigger a new shutdown sequence if needed.

---

## Check current UPS state

View what Eneru sees from the UPS:

```bash
cat /var/run/ups-monitor.state
```

This shows the current battery percentage, runtime, status, and other metrics.

---

## Example log output

### Normal operation (service startup)

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

### Power failure event

```
Dec 22 22:19:07 nuc.local python3[1274572]: 2025-12-22 22:19:07 CET - 🔄 Status changed: OL CHRG -> OB DISCHRG (Battery: 100%, Runtime: 28m 9s, Load: 29%)
Dec 22 22:19:07 nuc.local python3[1274572]: 2025-12-22 22:19:07 CET - ⚡ POWER EVENT: ON_BATTERY - Battery: 100%, Runtime: 1689 seconds, Load: 29%
Dec 22 22:19:10 nuc.local python3[1274572]: 2025-12-22 22:19:10 CET - 🔋 On battery: 100% (28m 5s), Load: 28%, Depletion: 0.0%/min, Time on battery: 3s
Dec 22 22:19:40 nuc.local python3[1274572]: 2025-12-22 22:19:40 CET - 🔋 On battery: 97% (19m 35s), Load: 23%, Depletion: 5.45%/min, Time on battery: 33s
Dec 22 22:20:10 nuc.local python3[1274572]: 2025-12-22 22:20:10 CET - 🔋 On battery: 96% (19m 5s), Load: 27%, Depletion: 3.81%/min, Time on battery: 1m 3s
Dec 22 22:20:45 nuc.local python3[1274572]: 2025-12-22 22:20:45 CET - 🔋 On battery: 93% (18m 31s), Load: 26%, Depletion: 4.29%/min, Time on battery: 1m 38s
Dec 22 22:21:15 nuc.local python3[1274572]: 2025-12-22 22:21:15 CET - 🔋 On battery: 93% (18m 1s), Load: 23%, Depletion: 3.28%/min, Time on battery: 2m 8s
```

### Power restored

```
Dec 22 22:21:17 nuc.local python3[1274572]: 2025-12-22 22:21:17 CET - 🔄 Status changed: OB DISCHRG -> OL CHRG (Battery: 65%, Runtime: 17m 59s, Load: 18%)
Dec 22 22:21:17 nuc.local python3[1274572]: 2025-12-22 22:21:17 CET - ⚡ POWER EVENT: POWER_RESTORED - Battery: 65% (Status: OL CHRG), Input: 237.6V, Outage duration: 2m 10s
```

---

## Known limitations

### Single UPS only

Eneru monitors one UPS per instance. For multiple UPS units, run multiple instances with different config files:

```bash
sudo python3 /opt/ups-monitor/eneru.py --config /etc/ups-monitor/ups1.yaml
sudo python3 /opt/ups-monitor/eneru.py --config /etc/ups-monitor/ups2.yaml
```

### UPS compatibility

Eneru works with any UPS supported by NUT. However, some UPS models may:

- Report inaccurate runtime estimates
- Have delayed battery percentage updates
- Not support all status flags

The multi-trigger system compensates for these issues.

### Battery anomaly detection and firmware jitter

Eneru watches for unexpected battery charge drops (>20% within 120 seconds) while the UPS is on line power. If the charge suddenly falls without the UPS ever going on battery, something is wrong: a firmware recalibration, battery aging, or a hardware problem.

However, some UPS models (APC, CyberPower, and Ubiquiti UniFi UPS are known offenders) briefly report a bogus charge value right after switching from battery (OB) back to line power (OL). A UPS sitting at 100% charge might report 50% for a second or two after power is restored, then go right back to the correct value.

To avoid false alarms, Eneru requires the anomalous reading to persist across 3 consecutive polls before firing a warning. If the charge bounces back before that, the reading is discarded as jitter.

In practice:

- If the charge stays low across multiple polls, it is a real anomaly and Eneru fires the warning
- If the charge recovers within 1-2 polls, Eneru treats it as transient jitter and ignores it

If you see repeated false `Battery Anomaly Detected` warnings, check your UPS firmware version. Some firmware updates improve charge reporting accuracy during power transitions.

---

## Why isn't my redundancy-group server shutting down?

A server lives under a [redundancy group](redundancy-groups.md), one
UPS clearly failed, but Eneru did nothing. Walk through the list:

**1. The group's quorum has not been lost yet.**

By default `min_healthy: 1` means any single healthy member keeps the
group up. Check the logs:

```
🛡️ Redundancy group 'rack-1' evaluator started (2 sources, min_healthy=1)
```

Then watch for either of these on every tick (~1 s):

- No log line: quorum is healthy and steady.
- `🚨 Redundancy group 'rack-1' quorum LOST`: the group dropped
  below `min_healthy`. The next log line is
  `🚨 ========== REDUNDANCY GROUP SHUTDOWN: rack-1 ==========`.

If quorum is not lost but you expected it to be, `min_healthy` is set
higher than you intended, or one UPS member is still being counted as
healthy. Recheck `degraded_counts_as` / `unknown_counts_as` for the
policies you actually want.

**2. The member UPS is DEGRADED, not CRITICAL.**

A UPS that is on battery but has not yet hit any per-UPS trigger
condition (low battery, low runtime, depletion, extended time, FSD)
is `DEGRADED`, not `CRITICAL`. With `degraded_counts_as: healthy`
(default), a degraded UPS still contributes to `healthy_count`.

For strict behaviour, set `degraded_counts_as: critical`.

**3. The advisory trigger never fired.**

If a per-UPS trigger should have fired but did not, check the member
monitor's log for `Trigger condition met (advisory, redundancy
group): ...`. If the line is missing, the per-UPS thresholds (e.g.
`triggers.low_battery_threshold`) have not been crossed yet. Adjust
them per [Shutdown triggers](triggers.md#triggers-in-redundancy-groups).

**4. The redundancy executor's flag file is sticky.**

The redundancy shutdown is gated by
`/var/run/ups-shutdown-redundancy-{sanitized-group-name}` for
idempotency. If a previous run created the flag and was killed before
clearing it, the executor will skip subsequent shutdowns. Clean it up:

```bash
sudo rm /var/run/ups-shutdown-redundancy-*
```

This is normal after a manual `kill -9` of Eneru. SIGTERM and SIGINT
clear the flag automatically.

**5. The redundancy group references the wrong UPS names.**

The names in `ups_sources` must match the `name:` field in the
top-level `ups:` section exactly (including `@host:port` suffix). Run
`eneru validate --config <path>`. Unknown references show up as
`ERROR: Redundancy group 'X' references unknown UPS name(s): ...`.

---

## Getting help

If you're still stuck:

1. Check the logs. Most issues are visible there.
2. Enable dry-run mode to test safely.
3. Open a [GitHub issue](https://github.com/m4r1k/Eneru/issues) with logs and config (redact sensitive data).
