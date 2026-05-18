# Migrate from native install to container

This guide walks you from a working deb/rpm/pip install of Eneru to
the same setup running inside the OCI image. Your existing
configuration keeps working. The only YAML you need to touch is the
loopback override, and only if you don't want the synthesized
defaults.

If you're new here, start at [Choose your install](install-comparison.md)
to confirm the OCI image is the right profile for your deployment.

## What changes vs your native install

| Aspect | Native (deb/rpm/pip) | OCI container |
|---|---|---|
| Process runs as | `root` (systemd) | `eneru` (uid 10001), non-root |
| Local actions run via | Direct kernel calls / local subprocess | SSH to host's `sshd` (loopback delegate) |
| Config file location | `/etc/ups-monitor/config.yaml` | Bind-mounted into container at the same path |
| State files | `/var/lib/eneru`, `/var/run/eneru`, `/var/log/eneru` | Persistent volumes mounted at the same paths |
| Host poweroff binary | Required on PATH | Lives on the host; the container doesn't need it |
| `wall(1)` broadcast | Fires when configured | Silently suppressed (reaches nobody from container) |

## Pre-migration checklist

Before swapping in the container, on the **host** that will run it:

1. **Container runtime installed.** Docker or Podman. `docker --version`
   / `podman --version` should report a sane number.

2. **Host `sshd` reachable on `127.0.0.1`.** Quick check:
   ```bash
   ssh -o BatchMode=yes -o ConnectTimeout=3 root@127.0.0.1 true
   ```
   On a fresh host you'll usually get `Permission denied (publickey)`.
   That's expected. The key wiring is the next step.

3. **`/etc/machine-id` exists.** This is true on every modern systemd
   host. Quick check:
   ```bash
   cat /etc/machine-id
   ```
   If empty, run `systemd-machine-id-setup`.

4. Notification URLs, NUT credentials, and everything else from your
   YAML carry over unchanged. They live in the config file you'll
   bind-mount.

## Step 1: Generate a dedicated SSH key for the loopback

Don't reuse your operator key. Make a fresh one for the loopback:

```bash
mkdir -p /srv/eneru/ssh
ssh-keygen -t ed25519 -N '' \
    -f /srv/eneru/ssh/id_loopback \
    -C "eneru-loopback@$(hostname)"
# Eneru runs as uid 10001 inside the container. The bind mount maps host
# uids 1:1, so a key owned by root with mode 0600 is unreadable from
# the container. Hand the private key to uid 10001 and keep the
# permission bits tight (0400 or 0600); the public key can be 0644.
# Do NOT loosen the private key to 0644 just to make the container
# happy — it would be readable by every local user on the host.
chown 10001:10001 /srv/eneru/ssh/id_loopback /srv/eneru/ssh/id_loopback.pub
chmod 0400 /srv/eneru/ssh/id_loopback
chmod 0644 /srv/eneru/ssh/id_loopback.pub
```

`/srv/eneru/` follows the FHS site-specific data convention. Use
`/opt/eneru/` (or anything else) if your distribution policy prefers
it; just update the bind-mount source in Step 6 to match.

## Step 2: Authorize the key

Default path: authorize the key for root with no forced command. Plan
B: use a dedicated user with `use_sudo: true`.

### Option A (default): root

```bash
# On the host:
mkdir -p /root/.ssh
cat /srv/eneru/ssh/id_loopback.pub | tee -a /root/.ssh/authorized_keys
chmod 600 /root/.ssh/authorized_keys
```

Your config can now use the synthesized defaults. No `remote_servers`
entry is needed; Eneru auto-creates one when it detects a Docker or
Podman runtime alongside local capabilities.

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

If your native config already drives other remote targets (a NAS, a
secondary host, anything else), those entries currently rely on
`/root/.ssh/id_rsa` implicitly. Eneru inside the container doesn't have
access to root's home, so each entry needs an explicit `ssh_key_path`
pointing at a file the container can read.

```bash
# On the host. Copy the operator key into the same bind-mount tree as
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

Leave the package installed for now. Easy rollback if something is
off.

## Step 3b: Create the writable host dirs and carry forward the stats DB

The container writes to three host paths through bind mounts: state
(SQLite stats DB, lifecycle state), run (per-poll battery history,
shutdown flag, remote-health sidecar), and logs. Create all three up
front owned by uid 10001 so the daemon can write the moment it starts:

```bash
sudo mkdir -p /srv/eneru/{state,run,logs}
sudo chown 10001:10001 /srv/eneru/{state,run,logs}
```

**Carry forward the stats DB (optional).** The DB stores sample
history (TUI graphs), event log, and notification rows. A fresh
container with an empty `/srv/eneru/state` starts with no history. To
preserve it, copy every `.db` file before the container starts. The
daemon must not be running on either side during the copy: SQLite WAL
files make hot copies unreliable.

```bash
# Native service is already stopped from Step 3.
sudo cp -a /var/lib/eneru/*.db /srv/eneru/state/
sudo chown 10001:10001 /srv/eneru/state/*.db
```

Filename conventions, in case you need to know what to look for. The
`cp -a *.db` above handles both cases without you having to pick:

| Mode | DB filename |
|---|---|
| Single-UPS (legacy `ups:` block) | `default.db` |
| Multi-UPS (`ups:` is a list) | `<sanitized-ups-name>.db` where `@`, `:`, `/` in the UPS name become `-` (e.g. `UPS@192.168.178.11` → `UPS-192.168.178.11.db`) |

If you already started the container against an empty
`/srv/eneru/state` and then copied the DB, stop the container before
recopying so the daemon picks up the imported file:

```bash
docker stop eneru
sudo cp -a /var/lib/eneru/*.db /srv/eneru/state/
sudo chown -R 10001:10001 /srv/eneru/state/
docker start eneru
```

Skip this step entirely if you don't care about graph or notification
history. The daemon starts fine on an empty stats dir.

## Step 3c: Detach the config file from the package

The native install owns `/etc/ups-monitor/config.yaml`. If you ever
`apt remove eneru` or `dnf remove eneru` to finish the migration, that
file disappears with the package and the container loses its
configuration on the next restart.

Copy the config under `/srv/eneru/` once, then point the container at
the copy. The deb/rpm package can then be removed without breaking
anything:

```bash
sudo cp /etc/ups-monitor/config.yaml /srv/eneru/config.yaml
# Read-only mount inside the container, so root ownership on the host
# is fine. No chown required.
```

The Steps 4, 5, and 6 `docker run` examples below all source the config
from `/srv/eneru/config.yaml`. If you'd rather keep editing in
`/etc/ups-monitor/config.yaml` (because the package is staying around
as a rollback path, say), swap that one bind-mount source back. Nothing
else changes.

## Step 4: Pre-flight the container

Run `validate` against your existing config from inside the new
image. Add `:Z` to the **eneru-owned** bind mounts on SELinux hosts
(RHEL/Alma/Rocky), but NEVER on `/etc/machine-id` — see the warning
just below Step 6:

```bash
docker run --rm \
    --network host \
    -v /etc/machine-id:/etc/machine-id:ro \
    -v /srv/eneru/config.yaml:/etc/ups-monitor/config.yaml:ro,Z \
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
    -v /etc/machine-id:/etc/machine-id:ro \
    -v /srv/eneru/config.yaml:/etc/ups-monitor/config.yaml:ro,Z \
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
    -v /etc/machine-id:/etc/machine-id:ro \
    -v /srv/eneru/config.yaml:/etc/ups-monitor/config.yaml:ro,Z \
    -v /srv/eneru/ssh:/var/lib/eneru/ssh:ro,Z \
    -v /srv/eneru/state:/var/lib/eneru:Z \
    -v /srv/eneru/run:/var/run/eneru:Z \
    -v /srv/eneru/logs:/var/log/eneru:Z \
    ghcr.io/m4r1k/eneru:latest
```

The `/etc/machine-id` mount stays plain `:ro` (no `:Z`, no `:z`). `:Z`
rewrites the on-disk SELinux label of the source file and the change
persists across reboots, which breaks dbus-broker / NetworkManager /
logind on the next host boot. The default targeted policy already
grants `container_t` read access. Same rule for any other host file
shared with system services (`/etc/localtime`, `/etc/resolv.conf`,
anything under `/run`). If you already ran the container with `:Z` on
`/etc/machine-id`, see [Troubleshooting](#recover-from-z-on-etcmachine-id)
before the next reboot.

Tail the logs to confirm the loopback comes up HEALTHY on the first
healthcheck:

```bash
docker logs -f eneru
```

When a loopback is synthesized (explicit-loopback configs skip this
line) you should see:

* `v5.5: auto-enabled host-loopback delegate (127.0.0.1, root, ...)`

Then the periodic `Remote SSH probe` lines should pass.

## Step 6b: docker compose alternative

If you prefer a versioned manifest over a long `docker run`, the same
mounts express as the compose file below. Drop it at
`/srv/eneru/docker-compose.yml` and run `docker compose -f
/srv/eneru/docker-compose.yml up -d`:

```yaml
services:
  eneru:
    image: ghcr.io/m4r1k/eneru:latest
    container_name: eneru
    restart: unless-stopped
    network_mode: host        # daemon polls NUT and reaches the loopback
    volumes:
      - /etc/machine-id:/etc/machine-id:ro   # NEVER :Z — see warning above
      - /srv/eneru/config.yaml:/etc/ups-monitor/config.yaml:ro,Z
      - /srv/eneru/ssh:/var/lib/eneru/ssh:ro,Z
      - /srv/eneru/state:/var/lib/eneru:Z
      - /srv/eneru/run:/var/run/eneru:Z
      - /srv/eneru/logs:/var/log/eneru:Z
```

The same `:Z` rule applies to the eneru-owned mounts: colon (not
comma) for writable, `:ro,Z` for read-only. The `/etc/machine-id`
entry MUST stay plain `:ro` — no relabel suffix. See the warning under
Step 6.

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

Step 3c only copied the config to `/srv/eneru/config.yaml`; the
original `/etc/ups-monitor/config.yaml` is still on disk, and the
native unit reads it as-is. If you already removed the deb/rpm
package, restore the file first:

```bash
sudo install -D -m 0644 /srv/eneru/config.yaml /etc/ups-monitor/config.yaml
```

## Legacy log/run-dir auto-rewrite

Eneru silently rewrites the four native-install defaults
(`/var/log/ups-monitor.log`, `/var/run/ups-monitor.state`,
`/var/run/ups-battery-history`, `/var/run/ups-shutdown-scheduled`) to
their `/var/{log,run}/eneru/` equivalents inside container runtimes so
the daemon can write as uid 10001. The rewrite only fires when the
config still matches the exact legacy default; setting any other value
in your `logging:` block opts out and your value wins.

The rewrite applies on every config load, so the daemon (`run`) and
every read-only subcommand (`validate`, `monitor` / `tui`, `shutdown
group`, `remote list`, …) all observe the same effective paths. If
`docker exec -it eneru eneru tui` reports "daemon not running" while
`docker logs eneru` shows the daemon happily polling, you're on a build
that predates this fix — upgrade to v5.5.0-rc8 or newer, or set the
four `logging.*` paths explicitly to their `/var/{log,run}/eneru/...`
values in your config to bypass the auto-rewrite entirely.

### Where state lives across container restarts

| Bind mount | What it holds |
|---|---|
| `-v /srv/eneru/config.yaml:/etc/ups-monitor/config.yaml:ro` | Daemon configuration; decoupled from the package (see Step 3c). |
| `-v /srv/eneru/state:/var/lib/eneru` | SQLite stats DB (samples, events, notifications). Persistent; do not skip. |
| `-v /srv/eneru/run:/var/run/eneru` | Per-run state (battery history, shutdown flag, monitor state file). |
| `-v /srv/eneru/logs:/var/log/eneru` | Forensic log file. |
| `-v /srv/eneru/ssh:/var/lib/eneru/ssh:ro` | Loopback + operator SSH keys (read-only). |

## Troubleshooting

### Recover from `:Z` on `/etc/machine-id`

Only relevant if you tested a v5.5.0-rc image whose docs recommended
`:Z` on the `/etc/machine-id` mount (rc7 and earlier). The relabel
persists on disk, so the host will fail to boot dbus-broker /
NetworkManager / logind on the next reboot until you restore the
default label:

```bash
docker stop eneru
sudo restorecon -Fv /etc/machine-id    # -F is required; plain restorecon
                                       # refuses with "not reset as customized by admin"
ls -lZ /etc/machine-id                 # expect system_u:object_r:machineid_t:s0
```

Then drop `:Z` from the `/etc/machine-id` line in your compose /
`docker run` (plain `:ro` only — see the note under Step 6) and start
eneru again. If the host is already locked out at boot, recover from a
physical console: `setenforce 0` → finish booting → `restorecon -Fv
/etc/machine-id` → `setenforce 1` → fix the mount → restart eneru.
