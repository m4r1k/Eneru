# Troubleshooting

Start with the logs and validation output. Most Eneru failures are configuration, NUT connectivity, SSH permissions, or stale shutdown flags from testing.

## Quick checks

Package install:

```bash
sudo eneru validate --config /etc/ups-monitor/config.yaml
sudo systemctl status eneru.service
sudo journalctl -u eneru.service -e
```

PyPI install:

```bash
eneru validate --config /etc/ups-monitor/config.yaml
eneru run --dry-run --config /etc/ups-monitor/config.yaml
```

Check the current state file:

```bash
sudo cat /var/run/ups-monitor.state
```

## Service will not start

| Check | Command |
|-------|---------|
| Last service errors | `sudo journalctl -u eneru.service -e` |
| Unit definition | `systemctl cat eneru.service` |
| Package entry point | `sudo eneru version` |
| Config validity | `sudo eneru validate --config /etc/ups-monitor/config.yaml` |
| Python version | `python3 --version` |

The packaged systemd unit runs:

```bash
sudo eneru run --config /etc/ups-monitor/config.yaml
```

If the wrapper is missing or import errors mention `eneru`, reinstall the native package. Do not try to repair a native package install with system `pip`.

## Cannot connect to UPS

Test NUT directly from the Eneru host:

```bash
upsc -l 192.168.1.100
upsc UPS@192.168.1.100
```

If this fails, Eneru cannot monitor the UPS. Check these in order:

| Area | What to verify |
|------|----------------|
| UPS name | `upsc -l <host>` must list the same name used in `ups.name` |
| NUT listener | `upsd` listens on the network interface, usually port 3493 |
| Firewall | TCP 3493 is reachable from the Eneru host |
| NUT users | Remote access is allowed where your NUT setup requires users |
| Driver health | NUT driver logs on the UPS server show fresh data |

On the NUT server, common checks are:

```bash
systemctl status nut-server
systemctl status nut-driver
journalctl -u nut-driver -e
```

## Intermittent NUT drops

Some networked UPSes and embedded NUT servers flap briefly. Use the connection-loss grace period to avoid alerts for short drops while the UPS is on line power:

```yaml
ups:
  connection_loss_grace_period:
    enabled: true
    duration: 60
    flap_threshold: 5
```

This does not delay failsafe shutdown. If Eneru loses connection while the UPS is on battery, it shuts down immediately.

If flaps continue, check network stability, UPS firmware, and NUT driver logs.

### Connection grace timeline

This is grounded in `monitor.py`: connection grace runs only when Eneru is not on battery. Stale data must first reach `max_stale_data_tolerance` attempts, which defaults to 3. A failed poll path waits 5 seconds before the next loop iteration.

| Time | State | Eneru behavior |
|------|-------|----------------|
| First stale poll | `stale_data_count=1/3` | Logs a stale-data warning, no grace timer yet |
| Third stale poll by default | Tolerance reached | Enters `GRACE_PERIOD` and starts the 60s default grace timer |
| Before 60s expires | Connection recovers | Logs quiet recovery, sends no `CONNECTION_LOST` notification |
| Recovery within grace repeats | Flap counter increments | After 5 recoveries within the 24h flap window, sends unstable-NUT warning |
| 60s grace expires | Still disconnected | Sends `CONNECTION_LOST` and marks connection `FAILED` |
| Any time previous UPS status was `OB` | Connection or stale-data failure | Bypasses grace and starts failsafe shutdown immediately |

## Notifications do not arrive

Run the built-in test:

```bash
sudo eneru test-notifications --config /etc/ups-monitor/config.yaml
```

Then check:

| Cause | Fix |
|-------|-----|
| Apprise missing | Install package notification dependencies or use `eneru[notifications]` for PyPI |
| Bad URL format | Compare with the Apprise service wiki |
| Outbound network blocked | Test DNS and HTTPS from the Eneru host |
| Rate limiting | Wait and retry with fewer test sends |
| Event muted | Check `notifications.suppress` |

Notification rows are stored in SQLite. Pending rows indicate delivery has not succeeded yet:

```bash
sqlite3 /var/lib/eneru/UPS-192-168-1-100.db \
  "SELECT id, status, attempts, cancel_reason, title FROM notifications ORDER BY id DESC LIMIT 20;"
```

## Remote shutdown fails

Test SSH as root on the Eneru host:

```bash
sudo ssh user@remote-server "echo OK"
sudo ssh user@remote-server "sudo -n true && echo sudo OK"
```

| Error | Meaning |
|-------|---------|
| `Permission denied (publickey,password)` | The key is missing, wrong, unreadable, or installed for a different user |
| `sudo: a password is required` | Passwordless sudo is not configured for the command Eneru runs |
| `command not found` | Use a full path or platform-specific shutdown command |
| Timeout | Firewall, host down, SSH service down, or too-low `connect_timeout` |
| Host key changed | Verify the remote host before accepting the new key |

See [Remote servers](remote-servers.md) for sudoers examples and platform-specific shutdown commands.

## Safe dry-run test

Use dry-run before any real shutdown test:

```yaml
behavior:
  dry_run: true
```

or override from the command line:

```bash
sudo eneru run --dry-run --config /etc/ups-monitor/config.yaml
```

During dry-run, Eneru logs every action with a dry-run marker and skips the actual VM, container, remote, filesystem, and local shutdown commands.

For a controlled test:

1. Enable dry-run.
2. Lower `triggers.extended_time.threshold` temporarily.
3. Start Eneru in the foreground.
4. Simulate or create a short on-battery condition.
5. Watch the planned shutdown order in the logs.
6. Restore the production thresholds.

## Clear stale shutdown flags

Eneru writes shutdown flag files so an interrupted process does not run the same sequence repeatedly. After a killed dry-run test, a flag can remain.

Single UPS flag:

```bash
sudo rm -f /var/run/ups-shutdown-scheduled
```

Redundancy group flags:

```bash
sudo rm -f /var/run/ups-shutdown-redundancy-*
```

Remove flags only after confirming no real shutdown is in progress.

If you see this warning in the logs:

```
⚠️ Shutdown trigger fired (...) but a previous shutdown sequence is already
   in progress (/var/run/ups-shutdown-scheduled). Ignoring re-trigger.
```

it means a prior shutdown sequence ran but the host did not actually halt (custom shutdown command was a no-op, sandbox/container intercepted it, or the daemon was running outside systemd's reach). Since 5.2.2 the daemon clears the flag automatically when `POWER_RESTORED` fires, so the next outage re-arms cleanly. The warning surfaces only on the unusual path where a trigger fires *while* a previous sequence is still considered in flight; clearing the flag manually as above is safe once you have confirmed no real shutdown is running.

## Graphs or events are empty

The stats writer flushes every few seconds. For a fresh start, wait at least 10 seconds and check the database:

```bash
sudo ls -lh /var/lib/eneru/
sqlite3 /var/lib/eneru/UPS-192-168-1-100.db "SELECT COUNT(*) FROM samples;"
sqlite3 /var/lib/eneru/UPS-192-168-1-100.db "SELECT COUNT(*) FROM events;"
```

If the DB is missing, check `statistics.db_directory` and permissions.

## Redundancy group does not fire

Check these in order:

| Check | Explanation |
|-------|-------------|
| Quorum still holds | With `min_healthy: 1`, one healthy member keeps a two-UPS group online |
| DEGRADED counts healthy | Default `degraded_counts_as: healthy` means on-battery warning state may still satisfy quorum |
| Advisory trigger missing | Per-UPS thresholds may not have crossed yet |
| Unknown policy | Default `unknown_counts_as: critical`; custom policy may be more tolerant |
| Stale flag | `/var/run/ups-shutdown-redundancy-*` may remain after killed tests |
| Wrong source name | `ups_sources` must exactly match `ups[].name` |

Run validation to catch source and ownership errors:

```bash
sudo eneru validate --config /etc/ups-monitor/config.yaml
```

## Battery anomaly warnings

Eneru warns when battery charge drops sharply while the UPS is on line power. This can mean battery wear, firmware recalibration, or bad telemetry.

Some APC, CyberPower, and Ubiquiti units briefly report a bogus charge after returning from battery. Eneru requires the anomalous reading to persist across multiple polls before alerting, which filters most one- or two-poll jitter.

If anomaly warnings repeat:

- Check UPS firmware and NUT driver versions.
- Compare `battery.charge` manually with `upsc`.
- Run a controlled self-test if your UPS supports it.
- Plan battery replacement if the charge drop persists.

### Battery anomaly timeline

This behavior comes from `src/eneru/health/battery.py`: the drop must be greater than 20 percentage points, occur within 120 seconds while on line power, and persist across 3 consecutive polls.

| Poll | Reading | Eneru behavior |
|------|---------|----------------|
| Previous OL poll | Battery 100% | Baseline is stored |
| Current OL poll | Battery 70% | Drop is greater than 20 points within 120s, so anomaly is marked pending |
| Next poll | Battery still around 70% | Pending count increments, no alert yet |
| Third confirming poll | Battery still low | Alert fires if the drop is still greater than 20 points |
| Any confirming poll | Battery recovers by more than 10 points from pending low value | Pending anomaly is discarded as transient firmware jitter |

## Known limits

| Limit | Detail |
|-------|--------|
| UPS support comes from NUT | If NUT cannot read the UPS reliably, Eneru cannot fix that |
| Runtime estimates can be wrong | Keep depletion and extended-time triggers enabled |
| Remote shutdown needs trust | SSH keys and passwordless sudo are required for unattended operation |
| Local resources need local ownership | In multi-UPS mode, only `is_local: true` can own VMs, containers, and filesystems |

## Getting help

Open a GitHub issue with:

- Eneru version.
- Install method, package or PyPI.
- Redacted `config.yaml`.
- `eneru validate` output.
- Relevant `journalctl -u eneru.service` lines.
- NUT output from `upsc <ups@host>`.
