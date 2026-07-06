# Containers and Kubernetes

Eneru v5.5 publishes one OCI image:

```bash
docker pull ghcr.io/m4r1k/eneru:latest
podman pull ghcr.io/m4r1k/eneru:latest
```

The v5.5 image is **first-class for both remote-only and full
local-host deployments**. For local-host ownership from a container,
Eneru SSHes to the host it runs on (the "loopback delegate") so the
namespace barrier doesn't block the host-poweroff contract. See
[Choose your install](install-comparison.md) for the three-profile
framing and [Migrate to container](migrate-to-container.md) for a
step-by-step from a deb/rpm install.

## Tags

| Tag | Meaning |
|-----|---------|
| `5.5.0`, `5.5.1`, etc. | Exact stable release. Immutable. |
| `5.5.0-rc1`, `5.5.0-rc2`, etc. | Exact pre-release. Immutable. |
| `latest` | Latest stable release. Moves on each stable tag. Convenient for samples and quick starts. |
| `testing` | Latest pre-release (rc/beta/alpha). Moves on each pre-release tag. |

The samples below use `:latest` so they work without per-release edits. Pin to a specific `<version>` tag for production — `:latest` is convenient but not immutable, and rolling restarts on a moving tag can mix versions. When you pin a version, also flip `imagePullPolicy` from `Always` to `IfNotPresent` so pod restarts don't hit the registry on every reschedule. Pre-release builds never become `:latest`; they land at `:testing` and at their explicit `<version>` tag.

## Remote-only Docker

Mount your config and any SSH key volume. The API flags keep healthchecks independent from the YAML file:

```bash
docker run -d --name eneru \
  --restart unless-stopped \
  -p 9191:9191 \
  -v /srv/eneru/config.yaml:/etc/ups-monitor/config.yaml:ro \
  -v /srv/eneru/state:/var/lib/eneru \
  -v /srv/eneru/run:/var/run/eneru \
  -v /srv/eneru/ssh:/var/lib/eneru/ssh:rw \
  ghcr.io/m4r1k/eneru:latest \
  run --config /etc/ups-monitor/config.yaml \
  --api --api-bind 0.0.0.0 --api-port 9191
```

Remote-only config shape:

```yaml
ups:
  - name: "UPS@nut-server"
    display_name: "Remote UPS"
    is_local: false
    remote_servers:
      - name: "nas"
        enabled: true
        host: "nas.example.lan"
        user: "ups"
        ssh_key_path: "/var/lib/eneru/ssh/id_ups_shutdown"
        # No ssh_options needed: Eneru defaults to StrictHostKeyChecking=
        # accept-new and records keys in /var/lib/eneru/ssh/known_hosts.
        shutdown_command: "sudo shutdown -h now"

local_shutdown:
  enabled: false
  trigger_on: none

logging:
  file: null
  state_file: "/var/run/eneru/ups-monitor.state"
  battery_history_file: "/var/run/eneru/ups-battery-history"
  shutdown_flag_file: "/var/run/eneru/ups-shutdown-scheduled"
```

## Local-host Docker (v5.5 loopback)

For full local-host ownership from a container — host poweroff, VM
teardown, container stop, filesystem sync/unmount — add three mounts
to the remote-only command above:

```bash
docker run -d --name eneru \
  --restart unless-stopped \
  --network host \
  -v /etc/machine-id:/etc/machine-id:ro \
  -v /srv/eneru/config.yaml:/etc/ups-monitor/config.yaml:ro \
  -v /srv/eneru/state:/var/lib/eneru \
  -v /srv/eneru/run:/var/run/eneru \
  -v /srv/eneru/ssh:/var/lib/eneru/ssh:rw \
  ghcr.io/m4r1k/eneru:latest
```

What changed vs remote-only:

* `--network host` so the container's `127.0.0.1` reaches the host's
  `sshd`. Bridge mode also works — see [Network mode decision
  table](#network-mode-decision-table) below.
* `-v /etc/machine-id:/etc/machine-id:ro` — **mandatory** for the
  host identity guard. Eneru reads it inside the container and
  compares to what the host's `sshd` returns. Missing mount → loopback
  marked FAILED → `/ready` 503.
* The default SSH key at `/srv/eneru/ssh/id_loopback` (looked up
  inside the container at `/var/lib/eneru/ssh/id_loopback`).

Your existing config-with-is_local works unchanged — Eneru detects
the Docker runtime + local capabilities and synthesizes a default
loopback `remote_servers` entry at startup (host `127.0.0.1`, user
`root`, command `shutdown -h now`). Override defaults by writing the
entry explicitly:

```yaml
remote_servers:
  - name: host-loopback
    enabled: true
    host: 127.0.0.1
    user: eneru-loopback   # dedicated user with sudo NOPASSWD
    use_sudo: true
    shutdown_command: "shutdown -h now"
    ssh_key_path: /var/lib/eneru/ssh/id_loopback
    is_host_loopback: true
```

Full reference shape: `examples/config-container-local.yaml`.

## SSH-from-container-to-host walkthrough

Generate a dedicated key, authorize it on the host with the
narrowest possible scope.

```bash
# On the host (one-time):
mkdir -p /srv/eneru/ssh
ssh-keygen -t ed25519 -N '' \
    -f /srv/eneru/ssh/id_loopback \
    -C "eneru-loopback@$(hostname)"
chown 10001:10001 /srv/eneru/ssh/id_loopback /srv/eneru/ssh/id_loopback.pub
chmod 0400 /srv/eneru/ssh/id_loopback
chmod 0644 /srv/eneru/ssh/id_loopback.pub
```

Then authorize either the default root loopback key, or a dedicated
non-root user with sudo NOPASSWD. Do **not** use `command=` in
`authorized_keys`: sshd applies that forced command to Eneru's
identity probe and to every generated VM/container/filesystem action,
so the loopback health check and delegated shutdown sequence no
longer mean what Eneru asked them to mean.

### Option A (default): root loopback

```bash
# On the host:
mkdir -p /root/.ssh
cat /srv/eneru/ssh/id_loopback.pub | tee -a /root/.ssh/authorized_keys
chmod 600 /root/.ssh/authorized_keys
```

This matches the synthesized defaults: `user: root`,
`shutdown_command: "shutdown -h now"`, and key path
`/var/lib/eneru/ssh/id_loopback`.

Ordinary remote targets need no host-key setup. Eneru defaults remotes to
`StrictHostKeyChecking=accept-new`, so on the first probe SSH records each
host key in `/var/lib/eneru/ssh/known_hosts` and pins it; a later key
*change* fails closed. Keep `/srv/eneru/ssh` persistent and writable, while
the private key file itself stays mode `0400`. Kubernetes uses a PVC-backed
known_hosts path instead because `/var/lib/eneru/ssh` is a read-only Secret
mount there. `accept-new` trusts the first connection, so do that first start
on a network you trust. Confirm with
`curl -s http://localhost:9191/api/v1/ups | jq '.ups[].remoteHealth'`
(every remote should read `"status": "HEALTHY"`).

### Option B: dedicated user + sudoers

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

Then point the loopback at this user and enable `use_sudo`:

```yaml
remote_servers:
  - name: host-loopback
    enabled: true
    host: 127.0.0.1
    user: eneru-loopback
    use_sudo: true
    shutdown_command: "shutdown -h now"
    ssh_key_path: /var/lib/eneru/ssh/id_loopback
    is_host_loopback: true
```

## `/etc/machine-id` and the host identity guard

The loopback delegate runs two probes:

1. The standard SSH probe (`id -u` by default) — proves SSH
   reachability.
2. A host-identity probe (`cat /etc/machine-id` by default) — proves
   the SSH target is actually the host running this container.

The second probe compares the SSH response to the **container's**
`/etc/machine-id`. When you bind-mount the host's machine-id at the
same path, both sides see the same value and the probe passes. When
you don't, the container has its own random machine-id (created at
runtime by systemd or the libc init), the values differ, and the
loopback is marked FAILED with this hint in `last_error`:

> host identity mismatch: probe returned 'X' but expected 'Y'.
> Most likely cause: /etc/machine-id is NOT bind-mounted from the
> host into the container, so Eneru sees a different machine-id
> locally than what the loopback SSH target reports.

The behavior fails closed — Eneru refuses to consider the loopback
ready, and `/ready` returns 503, so an orchestrator won't route
shutdown traffic to a daemon that can't deliver. To override the
default machine-id approach (e.g., use a dedicated marker file
instead), set:

```yaml
remote_servers:
  - is_host_loopback: true
    host_identity_command: "cat /etc/eneru-host-marker"
    expected_host_identity: "host-rack-3a-2026-05"   # any stable string
```

### No systemd / no machine-id (Alpine, consumer hosts)

`/etc/machine-id` is created by systemd (or `dbus`) and is the expected,
enterprise-tested baseline. Some hosts don't have it — most commonly
**Alpine Linux** and other non-systemd / musl setups. There, the default
`cat /etc/machine-id` probe has nothing to read, and the loopback fails
closed. The fix is a **stable marker file** you create once and bind-mount
at the **same path** on both sides.

1. On the host, generate a stable identity (run once; it persists):

   ```sh
   cat /proc/sys/kernel/random/uuid | tr -d '-' > /etc/eneru-machine-id
   ```

2. Bind-mount that file read-only into the container **at the same path**,
   and point the probe at it:

   ```sh
   docker run ... \
     -v /etc/eneru-machine-id:/etc/eneru-machine-id:ro \
     ...
   ```

   ```yaml
   remote_servers:
     - is_host_loopback: true
       host_identity_command: "cat /etc/eneru-machine-id"
   ```

That's all. Because `host_identity_command` is a simple `cat /absolute/path`,
Eneru reads the **same path locally** inside the container and auto-populates
`expected_host_identity` for you — no need to copy the value into YAML. The
SSH probe reads the host's copy, the local read sees the bind-mounted copy,
the two match, and the guard passes. (If you use a non-`cat` command Eneru
cannot infer the expected output, so set `expected_host_identity` explicitly,
as in the marker-file example above.)

> The same `:ro` rule applies as for `/etc/machine-id`: never add `:Z`/`:z`
> to a host file other services also read. A file you created solely for
> Eneru (like `/etc/eneru-machine-id`) is safe to relabel, but plain `:ro`
> is all you need.

## Network mode decision table

| Container network mode | Loopback `host:` value | Notes |
|---|---|---|
| `--network host` | `127.0.0.1` (default) | **Recommended.** The container shares the host's network namespace, so loopback reaches the host's `sshd` natively. |
| Default Docker bridge | `172.17.0.1` (the docker0 gateway) | Confirm with `ip route` on the host — some setups remap. |
| Custom Docker bridge | Bridge gateway IP | `docker network inspect <bridge>` |
| Docker Desktop (Mac/Win) | `host.docker.internal` | Not relevant for Linux servers. |
| Podman default | `host.containers.internal` or `--network host` | rootless Podman: use `--network host` for simplicity. |
| Kubernetes pod | Node IP via `hostPath` or `hostNetwork: true` | See K8s section below; not the recommended profile. |

The host identity guard catches the dangerous case where this address
points at the wrong machine — the SSH probe would return a different
`/etc/machine-id`, the loopback would be marked FAILED, and `/ready`
would return 503 before any destructive command is sent.

## Config hot-reload in containers

Edit the mounted config file on the host, then signal the container — `tini`
(PID 1) forwards `SIGHUP` to the daemon:

```bash
docker kill -s HUP <container>     # or: podman kill -s HUP <container>
```

Eneru re-reads and validates the file and applies the safe subset live
(thresholds, `nut_control` allowlists, `prometheus`, `dry_run`); a bad file is
rejected and the running config is kept. Changes to bind/port, topology,
logging, or DB paths still need a container restart. When the API is enabled you
can instead `POST /api/v1/config/reload` with a credential. See
[Configuration → Hot-reload](configuration.md#hot-reload).

## Podman and SELinux

On SELinux hosts, label **eneru-owned** bind mounts (the
`/srv/eneru/...` sources) with `:Z` for container-private access, or
`:z` when multiple containers share the same source. **Never** add
`:Z` or `:z` to `/etc/machine-id` or any other host file that other
system services also read — see
[install-comparison.md](install-comparison.md#selinux-note)
for the failure mode (broken dbus-broker, dead NetworkManager, host
locked out at next reboot):

```bash
podman run -d --name eneru \
  --replace \
  --network host \
  -v /etc/machine-id:/etc/machine-id:ro \
  -v /srv/eneru/config.yaml:/etc/ups-monitor/config.yaml:ro,Z \
  -v /srv/eneru/state:/var/lib/eneru:Z \
  -v /srv/eneru/run:/var/run/eneru:Z \
  -v /srv/eneru/ssh:/var/lib/eneru/ssh:Z \
  ghcr.io/m4r1k/eneru:latest
```

Rootless Podman works the same way — the loopback path doesn't need
container root because the privileged work happens on the host's
`sshd`, not inside the container.

## AppArmor

The default Docker AppArmor profile is sufficient for both remote-only
AND v5.5 loopback deployments. Eneru needs ordinary network access
(SSH) and writable state directories — both are allowed by the
default profile. **Do not disable AppArmor.** The previous v5.4
guidance to switch deployment paths for local-host ownership no
longer applies: under v5.5, local-host work is shipped over SSH from
within the default AppArmor confinement.

## Dangers and hardening

**The loopback SSH key can shut the host down.**
Treat it that way:

1. **Do not use forced commands.** `authorized_keys command="..."`
   breaks Eneru because sshd substitutes the identity probe and every
   generated shutdown action. Use a root key without `command=`, or a
   dedicated user with `use_sudo: true` and the sudoers stanza above.
2. **`from="127.0.0.1"`** in `authorized_keys` so even if the key
   leaks, it can't be used remotely without first compromising the
   host.
3. **Mount the SSH key read-only** (`:ro` in bind mounts). The
   container should never modify it.
4. **Key file mode 0400.** Eneru warns at startup if the loopback's
   key file is world-readable.
5. **A container escape becomes a host poweroff.** That's the worst
   case. It is bad, but it is still the action this daemon is designed
   to take during an outage. Mitigate by treating
   the Eneru container's lifecycle the same as any other privileged
   workload on the host.
6. **No `--privileged`, no `--cap-add SYS_ADMIN`, no `--pid=host`.**
   The loopback design exists specifically to avoid these.

## Kubernetes — remote-only profile

Per the v5.5 three-profile framing, Kubernetes is the **remote-only**
deployment profile. A pod shutting down its own node mid-shutdown
raises ordering questions (node drain? PDB? operator?) that Eneru
doesn't try to answer. Use K8s when you want to monitor a fleet of
remote UPSes from inside a cluster.

If you configure local capabilities (`is_local: true` + VMs /
containers / filesystems / `local_shutdown.enabled`) on a Kubernetes
runtime, Eneru emits a startup WARNING and does NOT auto-synthesize
a loopback — explicit opt-in only. Most operators want to remove the
local config in that case; if you really want pod-to-node SSH
delegation, set `is_host_loopback: true` explicitly on a
remote_servers entry.

The Kubernetes examples under `deploy/kubernetes/` are remote-only.
They run as UID 10001, mount config through a ConfigMap, mount SSH
keys through a Secret, and use HTTP probes. Unlike the `docker run`
snippets above, these committed manifests pin an explicit image version
(not `:latest`) because they're meant to be `kubectl apply`'d as-is —
bump the tag deliberately on upgrade. `remote-pod.yaml` reuses the
`eneru-config` ConfigMap defined in `remote-deployment.yaml`, so apply
the deployment (or copy that ConfigMap) first:

```bash
kubectl apply -f deploy/kubernetes/remote-deployment.yaml
```

Use a Secret for the SSH key:

```bash
kubectl create secret generic eneru-ssh-key \
  --from-file=id_ups_shutdown=/srv/eneru/ssh/id_ups_shutdown
```

## Logging

Inside a container Eneru does not use journald. Logs go to two places:

1. **stdout / stderr**, captured by the container runtime. Tail with:

   ```bash
   docker logs -f eneru
   podman logs -f eneru
   kubectl logs -f deploy/eneru
   ```

2. **`/var/log/eneru/ups-monitor.log`** when `logging.file` points there. Mount a persistent volume at `/var/log/eneru` so the file survives container restarts and gives you a forensic timeline after the runtime log buffer rotates away. The container `eneru` user is UID/GID 10001, so the volume must be group-writable by 10001. `fsGroup: 10001` in the pod `securityContext` is the simplest way; the sample manifests already do this.

   Docker bind mount:

   ```bash
   -v /srv/eneru/logs:/var/log/eneru:Z
   ```

   The Kubernetes samples mount an `emptyDir` at `/var/log/eneru` so the path exists; swap in a `PersistentVolumeClaim` if you want the log file to survive pod rescheduling.
