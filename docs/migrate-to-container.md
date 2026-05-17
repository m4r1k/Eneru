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

Don't reuse your operator key. Make a fresh one with a `command=`
restriction:

```bash
mkdir -p /srv/eneru/ssh
ssh-keygen -t ed25519 -N '' \
    -f /srv/eneru/ssh/id_loopback \
    -C "eneru-loopback@$(hostname)"
chmod 600 /srv/eneru/ssh/id_loopback
chmod 644 /srv/eneru/ssh/id_loopback.pub
```

## Step 2: Authorize the key for shutdown only

The most blast-radius-limited option: a dedicated user with sudo
NOPASSWD on `/sbin/shutdown` and `/sbin/poweroff`. The simpler
option: root with `command=` restriction. Pick one.

### Option A (recommended): dedicated user

```bash
# On the host:
useradd --system --create-home --shell /bin/bash eneru-loopback
mkdir -p /home/eneru-loopback/.ssh
cat /srv/eneru/ssh/id_loopback.pub | tee -a /home/eneru-loopback/.ssh/authorized_keys
chmod 600 /home/eneru-loopback/.ssh/authorized_keys
chown -R eneru-loopback: /home/eneru-loopback/.ssh

# Allow only the shutdown / poweroff commands via sudo:
cat > /etc/sudoers.d/eneru-loopback <<'EOF'
eneru-loopback ALL=(root) NOPASSWD: /sbin/shutdown, /sbin/poweroff, /usr/sbin/shutdown, /usr/sbin/poweroff
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
    shutdown_command: "sudo shutdown -h now"
    is_host_loopback: true
```

### Option B (simpler): root with command restriction

```bash
# On the host's root authorized_keys:
cat /srv/eneru/ssh/id_loopback.pub | tee -a /root/.ssh/authorized_keys
# Edit /root/.ssh/authorized_keys and prepend the line with:
#   command="/sbin/shutdown -h now",no-pty,no-port-forwarding,no-X11-forwarding,no-agent-forwarding,from="127.0.0.1"
```

Then your config can use the synthesized defaults (no `remote_servers`
entry needed at all — Eneru auto-creates one because it detects the
Docker/Podman runtime + your local capabilities).

## Step 3: Stop the native service

```bash
sudo systemctl stop eneru.service
sudo systemctl disable eneru.service
```

Leave the package installed for now — easy rollback if something is
off.

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

```
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

```diff
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
+
+# v5.5 OCI deployment: explicit loopback overrides the synthesized defaults.
+# Uncomment if you set up the dedicated user from Step 2 (Option A) instead
+# of letting Eneru auto-enable root + the default key path.
+remote_servers:
+  - name: host-loopback
+    enabled: true
+    host: 127.0.0.1
+    user: eneru-loopback
+    shutdown_command: "sudo shutdown -h now"
+    ssh_key_path: /var/lib/eneru/ssh/id_loopback
+    is_host_loopback: true
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
