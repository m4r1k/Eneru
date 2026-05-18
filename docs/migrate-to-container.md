# Migrate from native install to container

This guide walks you from a working deb/rpm/pip install of Eneru to
the same setup running inside the OCI image. Your existing
configuration keeps working — there are no required YAML changes
unless you want to override the synthesized loopback defaults.

If you're new here, start at [Choose your install](install-comparison.md)
to confirm the OCI image is the right profile for your deployment.

## What changes vs your native install

| Aspect | Native (deb/rpm/pip) | OCI container |
|---|---|---|
| Process runs as | `root` (systemd) | `eneru` (uid 10001), non-root |
| Local actions run via | Direct kernel calls / local subprocess | SSH to host's `sshd` (loopback delegate) |
| Config file location | `/etc/ups-monitor/config.yaml` | Bind-mounted into container at the same path |
| State files | `/var/lib/eneru`, `/var/run/eneru`, `/var/log/eneru` | Persistent volumes mounted at the same paths |
| Host poweroff binary | Required on PATH | Lives on the host — container doesn't need it |
| `wall(1)` broadcast | Fires when configured | Silently suppressed (reaches nobody from container) |

## Pre-migration checklist

Before swapping in the container, on the **host** that will run it:

1. **Container runtime installed.** Docker or Podman. `docker --version`
   / `podman --version` should report a sane number.

2. **Host `sshd` reachable on `127.0.0.1`.** Quick check:
   ```bash
   ssh -o BatchMode=yes -o ConnectTimeout=3 root@127.0.0.1 true
   ```
   Most likely you'll get `Permission denied (publickey)` on a fresh
   host — that's fine, the key wiring is the next step.

3. **`/etc/machine-id` exists.** This is true on every modern systemd
   host. Quick check:
   ```bash
   cat /etc/machine-id
   ```
   If empty, run `systemd-machine-id-setup`.

4. **Notification URLs, NUT credentials, etc.** carry over unchanged
   — they're in the config file you'll bind-mount.

## Step 1: Generate a dedicated SSH key for the loopback

Don't reuse your operator key. Make a fresh one for the loopback:

```bash
mkdir -p /srv/eneru/ssh
ssh-keygen -t ed25519 -N '' \
    -f /srv/eneru/ssh/id_loopback \
    -C "eneru-loopback@$(hostname)"
chmod 600 /srv/eneru/ssh/id_loopback
chmod 644 /srv/eneru/ssh/id_loopback.pub
# Eneru runs as uid 10001 inside the container — the bind mount maps host
# uids through 1:1, so a key owned by root mode 0600 is unreadable from the
# container. Either chown to 10001:10001, or relax to 0644 if you're
# comfortable with a world-readable file on disk.
chown 10001:10001 /srv/eneru/ssh/id_loopback /srv/eneru/ssh/id_loopback.pub
```

(`/srv/eneru/` follows the FHS site-specific data convention. Pick a different
prefix — e.g. `/opt/eneru/` — if your distribution's policy prefers it; just
update the bind-mount source in Step 6 to match.)

## Step 2: Authorize the key

Default path: authorize the key for root with no forced command. Plan
B: use a dedicated user with `use_sudo: true`.

Do **not** use `authorized_keys command="..."`. sshd substitutes that
forced command for Eneru's identity probe and for every generated
VM/container/filesystem action, so `/ready` can no longer prove the
configured shutdown behavior is achievable.

### Option A (default): root

```bash
# On the host:
mkdir -p /root/.ssh
cat /srv/eneru/ssh/id_loopback.pub | tee -a /root/.ssh/authorized_keys
chmod 600 /root/.ssh/authorized_keys
```

Then your config can use the synthesized defaults (no `remote_servers`
entry needed at all — Eneru auto-creates one because it detects the
Docker/Podman runtime + your local capabilities).

### Option B: dedicated user

```bash
# On the host:
useradd --system --create-home --shell /bin/bash eneru-loopback
mkdir -p /home/eneru-loopback/.ssh
cat /srv/eneru/ssh/id_loopback.pub | tee -a /home/eneru-loopback/.ssh/authorized_keys
chmod 600 /home/eneru-loopback/.ssh/authorized_keys
chown -R eneru-loopback: /home/eneru-loopback/.ssh

cat > /etc/sudoers.d/eneru-loopback <<'EOF'
eneru-loopback ALL=(root) NOPASSWD: /sbin/shutdown, /usr/sbin/shutdown, /sbin/poweroff, /usr/sbin/poweroff
eneru-loopback ALL=(root) NOPASSWD: /usr/bin/virsh, /usr/sbin/virsh, /usr/bin/docker, /usr/bin/podman, /bin/umount, /usr/bin/umount
EOF
chmod 440 /etc/sudoers.d/eneru-loopback
```

Then in your config:

```yaml
remote_servers:
  - name: host-loopback
    enabled: true
    host: 127.0.0.1
    user: eneru-loopback
    use_sudo: true
    shutdown_command: "shutdown -h now"
    is_host_loopback: true
```

## Step 2b: Migrate existing remote-server SSH keys

If your native config already drives **other** remote targets (a NAS, a
secondary host, etc.), those entries currently rely on `~root/.ssh/id_rsa`
implicitly. Eneru inside the container doesn't have access to root's
home, so each entry needs an explicit `ssh_key_path` pointing at a file
the container can read.

```bash
# On the host — copy the operator key into the same bind-mount tree as
# the loopback key so a single -v mount covers both.
cp /root/.ssh/id_rsa /srv/eneru/ssh/id_rsa
cp /root/.ssh/id_rsa.pub /srv/eneru/ssh/id_rsa.pub
chown 10001:10001 /srv/eneru/ssh/id_rsa /srv/eneru/ssh/id_rsa.pub
chmod 0400 /srv/eneru/ssh/id_rsa
```

Then add `ssh_key_path` to each existing `remote_servers` entry in your
config:

```yaml
remote_servers:
  - name: "Synology NAS"
    enabled: true
    host: 192.168.x.y
    user: nas-admin
    ssh_key_path: /var/lib/eneru/ssh/id_rsa   # ADD THIS
    shutdown_command: "shutdown -h now"
```

The container-side path `/var/lib/eneru/ssh/id_rsa` resolves to the host
file via the `-v /srv/eneru/ssh:/var/lib/eneru/ssh:ro` mount in Step 6.

## Step 3: Stop the native service

```bash
sudo systemctl stop eneru.service
sudo systemctl disable eneru.service
```

Leave the package installed for now — easy rollback if something is
off.

## Step 3b: Carry forward the existing stats DB (optional)

Eneru's per-UPS SQLite stats database lives at
`/var/lib/eneru/<sanitized-ups-name>.db`. It stores sample history (the
TUI graphs), event log, and notification rows (the notification
history). **A fresh container with an empty `/srv/eneru/state` bind
mount starts with no history** — the graphs and notification list will
be empty until new data accumulates.

If you want continuity, copy the existing DB(s) over after stopping the
service:

```bash
sudo mkdir -p /srv/eneru/state
sudo cp -a /var/lib/eneru/*.db /srv/eneru/state/
sudo chown -R 10001:10001 /srv/eneru/state/
```

Sanitization rule: `@`, `:`, `/` in the UPS name become `-`. So
`UPS@192.168.178.11` is stored as
`/var/lib/eneru/UPS-192.168.178.11.db`. Copy every `.db` file in
`/var/lib/eneru/` — `cp -a *.db` handles them in one shot.

Skip this step entirely if you don't care about graph/notification
history; the daemon happily starts on an empty stats dir.

## Step 4: Pre-flight the container

Run `validate` against your existing config from inside the new
image. Add `:Z` to every bind mount on SELinux hosts (RHEL/Alma/Rocky):

```bash
docker run --rm \
    --network host \
    -v /etc/machine-id:/etc/machine-id:ro,Z \
    -v /etc/ups-monitor/config.yaml:/etc/ups-monitor/config.yaml:ro,Z \
    -v /srv/eneru/ssh:/var/lib/eneru/ssh:ro,Z \
    ghcr.io/m4r1k/eneru:latest \
    validate --config /etc/ups-monitor/config.yaml
```

Expected output highlights:

```text
Runtime context: container (Docker)
UPS: ... [is_local]
  Shutdown sequence:
    1. Local actions delegated via loopback SSH: VMs, containers, sync, unmount(N)
    2. Remote server: ...   (your existing remote_servers entries)
    3. Local shutdown (host poweroff delegated via loopback SSH)
```

If you see the in-process per-phase tree (`1. Virtual machines`, `2.
Containers`, ...) instead, the loopback wasn't detected. Re-check
that you're on the v5.5+ image and that `/etc/machine-id` is mounted.

## Step 5: Dry-run rehearsal

Rehearse the full sequence without firing any destructive command:

```bash
docker run --rm \
    --network host \
    -v /etc/machine-id:/etc/machine-id:ro,Z \
    -v /etc/ups-monitor/config.yaml:/etc/ups-monitor/config.yaml:ro,Z \
    -v /srv/eneru/ssh:/var/lib/eneru/ssh:ro,Z \
    ghcr.io/m4r1k/eneru:latest \
    shutdown group --group "<your-ups-name>" --dry-run \
    --config /etc/ups-monitor/config.yaml
```

The output traces the same SSH path a real shutdown would take and
prints every command it would have sent. No host action is performed.

## Step 6: Start the container for real

```bash
docker run -d --name eneru \
    --restart unless-stopped \
    --network host \
    -v /etc/machine-id:/etc/machine-id:ro,Z \
    -v /etc/ups-monitor/config.yaml:/etc/ups-monitor/config.yaml:ro,Z \
    -v /srv/eneru/ssh:/var/lib/eneru/ssh:ro,Z \
    -v /srv/eneru/state:/var/lib/eneru,Z \
    -v /srv/eneru/run:/var/run/eneru,Z \
    -v /srv/eneru/logs:/var/log/eneru,Z \
    ghcr.io/m4r1k/eneru:latest
```

Tail the logs to confirm the loopback comes up HEALTHY on the first
healthcheck:

```bash
docker logs -f eneru
```

You should see one of:

* `v5.5: auto-enabled host-loopback delegate (127.0.0.1, root, ...)`
* `v5.5: running non-root inside container (Docker); local-host actions will be delegated to <user>@127.0.0.1 via SSH`

Then the periodic `Remote SSH probe` lines should pass.

## Step 7: Hit `/health` and `/ready`

```bash
curl http://127.0.0.1:9191/health   # always 200 when the daemon is up
curl http://127.0.0.1:9191/ready    # 200 when every required capability is achievable
```

If `/ready` returns 503, the JSON body lists every required capability
with `achievable: true|false` and a `reason`. See
[Troubleshooting](troubleshooting.md#ready-vs-503-decision-matrix).

## Rollback

If something is off and you want to revert to the native install:

```bash
docker stop eneru
docker rm eneru
sudo systemctl enable --now eneru.service
```

The native install reads the same `/etc/ups-monitor/config.yaml`, so
nothing on the host has to change.

## Side-by-side YAML diff

Most users have **zero config changes**. Eneru's auto-synthesis adds
a default loopback entry at runtime when it detects
Docker/Podman + local capabilities + no explicit loopback. The
synthesis uses `host: 127.0.0.1`, `user: root`,
`shutdown_command: "shutdown -h now"`, and looks for the SSH key at
`/var/lib/eneru/ssh/id_loopback` — all assumptions that match the
walkthrough above.

Override only when you want different defaults. Example: dedicated
user with sudo:

```yaml
# /etc/ups-monitor/config.yaml
ups:
  name: "UPS@nut-server"
local_shutdown:
  enabled: true
virtual_machines:
  enabled: true
containers:
  enabled: true
filesystems:
  sync_enabled: true

# v5.5 OCI deployment: explicit loopback overrides the synthesized defaults.
# Uncomment if you set up the dedicated user from Step 2 (Option B) instead
# of letting Eneru auto-enable root + the default key path.
remote_servers:
  - name: host-loopback
    enabled: true
    host: 127.0.0.1
    user: eneru-loopback
    shutdown_command: "sudo shutdown -h now"
    ssh_key_path: /var/lib/eneru/ssh/id_loopback
    is_host_loopback: true
```

## What you'll see differently in operation

* **`wall(1)` broadcasts no longer fire.** They reached the host's
  ttys on a native install; they reach nobody inside a container.
  Use a notification channel (Apprise, Discord, Slack, MQTT) for
  in-room alerts instead.
* **`shutdown` binary is not in the container image.** That's by
  design — the host has it. `eneru run` and `eneru shutdown group`
  skip the local-binary requirement when delegating.
* **`/ready` becomes strict.** Any unachievable required capability
  returns 503. Most commonly: the loopback's `remote_health` is
  FAILED (SSH not reachable, or `/etc/machine-id` mismatch — see
  Troubleshooting).

## Legacy log/run-dir auto-rewrite

The native-install defaults (`/var/log/ups-monitor.log`,
`/var/run/ups-monitor.state`, `/var/run/ups-battery-history`,
`/var/run/ups-shutdown-scheduled`) predate the `/var/{log,run}/eneru/`
convention and are only writable by root. Eneru inside the container runs
as uid 10001 and cannot write to `/var/log/` or `/var/run/` directly.

To preserve the "no required YAML changes" promise above, on startup
inside a container runtime, Eneru auto-rewrites these four paths IF AND
ONLY IF they still match the dataclass defaults:

| Legacy default | Container rewrite |
|---|---|
| `/var/log/ups-monitor.log` | `/var/log/eneru/ups-monitor.log` |
| `/var/run/ups-monitor.state` | `/var/run/eneru/ups-monitor.state` |
| `/var/run/ups-battery-history` | `/var/run/eneru/ups-battery-history` |
| `/var/run/ups-shutdown-scheduled` | `/var/run/eneru/ups-shutdown-scheduled` |

A one-line banner on stderr lists every rewrite that fired. Operator
paths — anything that doesn't match the legacy default exactly — are
left untouched.

### How to disable the auto-rewrite

The rewrite is opt-out, not opt-in. To revert to the legacy paths (e.g.
because you bind-mount a persistent volume at `/var/log/ups-monitor.log`
directly), set explicit values in the `logging:` block of your config —
the rewrite only acts on values that still equal the dataclass default:

```yaml
logging:
  file: "/var/log/ups-monitor.log"          # exactly the legacy path → still rewritten
  file: "/var/log/ups-monitor.log "         # any non-equal string → preserved as-is
  # Recommended: be explicit about where you want the daemon to write.
  file: "/var/log/eneru/ups-monitor.log"
  state_file: "/var/run/eneru/ups-monitor.state"
  battery_history_file: "/var/run/eneru/ups-battery-history"
  shutdown_flag_file: "/var/run/eneru/ups-shutdown-scheduled"
```

The first two lines above are the same value in `equals == legacy`
detection terms; the comparison is exact-string. The recommended pattern
is the third block — explicit `/var/{log,run}/eneru/` paths that survive
any future change to the defaults.

### Where state lives across container restarts

The Step 6 bind mounts cover every writable path the daemon needs:

| Bind mount | What it holds |
|---|---|
| `-v /srv/eneru/state:/var/lib/eneru` | SQLite stats DB (samples, events, notifications), notification-worker state, lifecycle classifier state. **Persistent across container restarts/upgrades — do not skip.** |
| `-v /srv/eneru/run:/var/run/eneru` | Per-run state (battery history, shutdown flag, monitor state file). Safe to wipe on restart, but persisting it avoids losing one cycle of battery-depletion history on restart. |
| `-v /srv/eneru/logs:/var/log/eneru` | Forensic log file. |
| `-v /srv/eneru/ssh:/var/lib/eneru/ssh:ro` | Loopback + operator SSH keys (read-only). |
