# Troubleshooting

Start with the logs and validation output. Most Eneru failures are configuration, NUT connectivity, SSH permissions, or stale shutdown flags from testing.

For container deployments, see also [Choose your install](install-comparison.md)
and [Migrate to container](migrate-to-container.md). Most v5.5 container
issues are SSH wiring or the `/etc/machine-id` bind-mount — covered in
the readiness section below.

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

Eneru writes shutdown flag files so an interrupted process does not run the same sequence repeatedly. Recent versions clear stale flags automatically at startup and after recovery, but manual cleanup is still useful when investigating an interrupted test.

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
⚠️  Shutdown trigger fired (...) but a previous shutdown sequence is already
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

## SELinux and AppArmor checks

Eneru does not ship a custom SELinux policy or AppArmor profile. Native RPM/DEB installs should work with the standard package labels and the distro's default Python/systemd behavior. If a Rocky 9 or RHEL-family host is enforcing SELinux and Eneru cannot read config, write logs, write stats, or execute SSH/NUT helpers, check policy denials before changing the daemon:

```bash
getenforce
sudo ausearch -m AVC,USER_AVC -ts recent
ls -Z /etc/ups-monitor /var/lib/eneru /var/log/ups-monitor.log
sudo restorecon -Rv /etc/ups-monitor /var/lib/eneru /var/log/ups-monitor.log
```

`restorecon` fixes mislabeled package paths after manual copies or backup restores. If AVC denials continue after labels are correct, open an issue with the denial lines and the install method. On AppArmor-based distributions, Eneru relies on the standard systemd/Python behavior unless the operator adds a local profile; check `journalctl -k` or `aa-status` for local profile denials.

## Redundancy group does not fire

Check these in order:

| Check | Explanation |
|-------|-------------|
| Quorum still holds | With `min_healthy: 1`, one healthy member keeps a two-UPS group online |
| DEGRADED counts healthy | Default `degraded_counts_as: healthy` means on-battery warning state may still satisfy quorum |
| Advisory trigger missing | Per-UPS or redundancy-group thresholds may not have crossed yet |
| Unknown policy | Default `unknown_counts_as: critical`; custom policy may be more tolerant |
| Stale flag | Recent versions clear restart-stale redundancy flags automatically; an active PID-owned flag blocks startup instead of being ignored |
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

## v5.5: container loopback delegation

The OCI image needs three things to do local-host shutdown via the
SSH loopback delegate: the host's `sshd` reachable, the SSH key
mounted, and `/etc/machine-id` bind-mounted so the host-identity
guard can verify the SSH target really is this container's host.
When any of those is wrong the loopback's `remote_health` goes
FAILED, the loud "under a real power outage..." notification fires,
and `/ready` returns 503.

### `/ready` vs 503 decision matrix

v5.5 readiness is strict: ANY required capability that's
unachievable fails the probe. The `/ready` JSON payload lists every
capability with `achievable: true|false` and a `reason` so you can
see exactly what's wrong without grepping logs.

| Capability | Required when | Achievable check (native install) | Achievable check (container + loopback) |
|---|---|---|---|
| `nut_polling` | always | NUT connection state is `OK` and last update is fresh | same |
| `local_vm_teardown` | `is_local && vms.enabled` | `virsh` on PATH | loopback `remote_health == HEALTHY` |
| `local_container_teardown` | `is_local && containers.enabled` | `docker` or `podman` on PATH | loopback `remote_health == HEALTHY` |
| `local_filesystem_unmount` | `is_local && filesystems.unmount.enabled` | `umount` on PATH | loopback `remote_health == HEALTHY` |
| `local_host_poweroff` | `local_shutdown.enabled` + local owner | Binary from `local_shutdown.command` on PATH | loopback `remote_health == HEALTHY` |
| `remote_server_shutdown[<name>]` | each enabled non-loopback `remote_servers` entry | that target's `remote_health == HEALTHY` (or `UNKNOWN` if probes disabled) | same |

`/health` always returns 200 while the daemon process is alive — use
it for liveness, `/ready` for "the shutdown contract can be honored
in full."

### Loopback FAILED — common causes

When the loopback's `remote_health` is FAILED, check the cause in
`/api/v1/remote-health` (or the API status payload). Most failures
fall into one of these:

| Symptom in `last_error` | Cause | Fix |
|---|---|---|
| `host identity mismatch: probe returned 'X' but expected 'Y'` | `/etc/machine-id` not bind-mounted from host | Add `-v /etc/machine-id:/etc/machine-id:ro` to the `docker run` command (plain `:ro` only — never `:Z` or `:z`; the relabel persists on disk and breaks dbus-broker on the next host reboot). |
| `authorized_keys command=` | Forced-command SSH key rewrites Eneru's identity probe and generated shutdown actions | Remove `command="..."` from the loopback key. Use the root default or `use_sudo: true` with sudoers. |
| `Permission denied (publickey,password)` | Loopback SSH key not authorized on the host | The container's `/var/lib/eneru/ssh/id_loopback` public half must be in the host user's `authorized_keys`. See [Containers and Kubernetes](containers-kubernetes.md) for the walkthrough. |
| `connection refused` | No `sshd` on `127.0.0.1`, OR container isn't on `network_mode: host` | Either start `sshd` on the host, or switch to `--network host`. For bridge networking, override the loopback `host` to the host's bridge IP (`172.17.0.1` on Linux default Docker bridge). |
| `identity probe failed: timeout after Xs` | `sshd` is up but the probe didn't return — usually host overload or sshd `MaxStartups` reached | Bump `connect_timeout` on the loopback entry; check sshd logs for `MaxStartups` warnings. |
| `unsafe probe command rejected` | `host_identity_command` contains shell metacharacters | Use a plain command like `cat /etc/machine-id` (the default). The safety check in `remote_health.py` blocks pipes, redirects, command substitution. |

### `eneru validate` shows the in-process sequence, not the delegated one

Symptom: in a container, `validate` output lists
`1. Virtual machines`, `2. Containers`, `3. Filesystem ...` instead
of `1. Local actions delegated via loopback SSH: ...`.

Cause: the synthesis or detection didn't kick in. Most likely either
the runtime detection misfired (the container env isn't recognizable
as `container (Docker)` / `container (Podman)`) or the local
capabilities aren't actually configured.

Check:

```bash
docker run --rm <args> eneru:<version> validate --config <path>
# Look for: "Runtime context: container (Docker)" (or Podman / Kubernetes)
```

If the runtime is `container (Kubernetes)`, Eneru does NOT
auto-synthesize a loopback — K8s is the remote-only profile per the
v5.5 framing. Set `is_host_loopback: true` explicitly on a
remote_servers entry if you really want local-host ownership from a
pod.

### `FATAL ERROR: Missing required commands: shutdown`

Symptom: `shutdown group` or `run` aborts with this error inside the
container.

Cause: the loopback wasn't synthesized (or was explicitly disabled),
so Eneru thinks the in-process shutdown path will run and requires
the `shutdown` binary on PATH — which the slim image doesn't have.

Fix: confirm the loopback is configured (either auto-synthesized or
explicit). Run `eneru validate` and look for the `1. Local actions
delegated via loopback SSH: ...` line. If it's missing, either:

* The runtime isn't detected as a container (check the `Runtime
  context:` line at the top of `validate` output), OR
* The local capabilities aren't configured (so synthesis doesn't
  fire), OR
* `is_host_loopback: false` was set explicitly somewhere.

### Synthesis WARNING about missing SSH key

Symptom (during `validate` or `shutdown group --dry-run`):

```text
WARNING: Eneru detected runtime 'container (Docker)' with local capabilities
but the default SSH key for the host-loopback delegate is missing:
  expected at: /var/lib/eneru/ssh/id_loopback
```

In dry-run / validate this is intentionally non-fatal so you can
inspect the config without first generating the key. In `eneru run`
the same situation is a fatal error (the daemon can't honor the
shutdown contract without the key).

Fix: generate the key with the expected name (see
[Migrate to container](migrate-to-container.md#step-1-generate-a-dedicated-ssh-key-for-the-loopback))
or set `ssh_key_path:` explicitly in a `remote_servers` entry with
`is_host_loopback: true`.
