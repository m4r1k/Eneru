# Install: Docker (quick start)

Stand Eneru up in a container on a host you control, with full
local-host ownership delegated through an SSH loopback. This guide is
for fresh installs. If you already run the deb/rpm/pip native service
and want to switch over, follow
[Migrate to container](migrate-to-container.md) instead so your existing
config and stats history carry forward.

## Prerequisites

- Linux host with Docker (or Podman) installed.
- A working NUT server (`upsc <UPS@host>` must answer).
- `/etc/machine-id` populated on the host. Most systemd distros put
  one there at first boot; if `cat /etc/machine-id` returns empty,
  run `sudo systemd-machine-id-setup`.
- Root access on the host long enough to authorize the loopback SSH key
  and create the writable directories below.

## Step 1: Generate the loopback SSH key

Eneru runs as uid 10001 inside the container and delegates host actions
(VM teardown, container stop, filesystem sync, poweroff) to the host's
`sshd` over a 127.0.0.1 SSH loopback. Generate a dedicated key for that
delegate. Don't reuse your operator key:

```bash
sudo mkdir -p /srv/eneru/ssh
sudo ssh-keygen -t ed25519 -N '' \
    -f /srv/eneru/ssh/id_loopback \
    -C "eneru-loopback@$(hostname)"
sudo chown 10001:10001 /srv/eneru/ssh/id_loopback /srv/eneru/ssh/id_loopback.pub
sudo chmod 0400 /srv/eneru/ssh/id_loopback
```

## Step 2: Authorize the key on the host

Default path: authorize the key for `root` with no forced command.
For a non-root sudo alternative, see
[Migrate to container Step 2 Option B](migrate-to-container.md#option-b-dedicated-user).

```bash
sudo mkdir -p /root/.ssh
sudo bash -c 'cat /srv/eneru/ssh/id_loopback.pub >> /root/.ssh/authorized_keys'
sudo chmod 600 /root/.ssh/authorized_keys
```

Confirm the loopback is reachable:

```bash
sudo ssh -i /srv/eneru/ssh/id_loopback \
    -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    root@127.0.0.1 true && echo "loopback OK"
```

## Step 3: Create the writable host dirs and the config

The container writes to three host paths through bind mounts (state,
run, logs). Create all three owned by uid 10001 so the daemon can
write the moment it starts:

```bash
sudo mkdir -p /srv/eneru/{state,run,logs}
sudo chown 10001:10001 /srv/eneru/{state,run,logs}
```

Drop a minimal config at `/srv/eneru/config.yaml`:

```yaml
ups:
  name: "UPS@<your-nut-host>"

local_shutdown:
  enabled: true

# Optional. Uncomment when you want to wire any of these in.
# virtual_machines: { enabled: true }
# containers:       { enabled: true }
# filesystems:      { sync_enabled: true }
# notifications:
#   enabled: true
#   urls:
#     - "discord://<webhook-id>/<token>"
```

Eneru auto-synthesizes the loopback delegate at startup when it detects
Docker/Podman + local capabilities + no explicit loopback in the
config, so you don't have to write a `remote_servers` entry for it
yourself.

## Step 4: Start the container

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

On RHEL/Alma/Rocky the `:Z` SELinux relabel is required for the four
**eneru-owned** mount sources (`/srv/eneru/...`). Use `:Z` (colon) for
writable mounts and `:ro,Z` (comma, with Z as the second option) for
read-only ones. A bare `,Z` on a writable mount is parsed by Docker as
part of the destination path, and the mount silently lands at the
wrong place.

The `/etc/machine-id` mount stays plain `:ro` — never `:Z` or `:z`.
The relabel persists on disk and would break dbus-broker /
NetworkManager / logind on the next host reboot. See the SELinux note
in [Choose your install](install-comparison.md#selinux-note).

If you prefer a versioned manifest, the same setup expresses as a
compose file:

```yaml
services:
  eneru:
    image: ghcr.io/m4r1k/eneru:latest
    container_name: eneru
    restart: unless-stopped
    network_mode: host
    volumes:
      - /etc/machine-id:/etc/machine-id:ro   # NEVER :Z — shared host file (see install-comparison.md)
      - /srv/eneru/config.yaml:/etc/ups-monitor/config.yaml:ro,Z
      - /srv/eneru/ssh:/var/lib/eneru/ssh:ro,Z
      - /srv/eneru/state:/var/lib/eneru:Z
      - /srv/eneru/run:/var/run/eneru:Z
      - /srv/eneru/logs:/var/log/eneru:Z
```

## Step 5: Verify

```bash
# Logs: one synthesis line, then the normal startup flow.
docker logs eneru

# Health + readiness: 200 means the configured shutdown contract
# is achievable (loopback SSH reachable, NUT polled successfully,
# every declared capability has a backing binary or delegated path).
curl http://127.0.0.1:9191/health
curl http://127.0.0.1:9191/ready

# TUI dashboard from inside the container.
docker exec -it eneru eneru tui
```

If `/ready` returns 503, the JSON body lists every required
capability with `achievable: true|false` and a reason. See the
[Troubleshooting decision matrix](troubleshooting.md#ready-vs-503-decision-matrix).

## Next steps

To shut down other targets (NAS, secondary hosts) alongside the local
host, see [Remote servers](remote-servers.md). Each entry needs an
explicit `ssh_key_path` pointing at a key readable by uid 10001 inside
the container; root's `~/.ssh/id_rsa` is not visible from the
`eneru` user.

For battery thresholds, brownout sensitivity, and the other
power-event knobs, see [Shutdown triggers](triggers.md). Runtime
notes for Podman, Kubernetes, and unprivileged-runtime caveats live
in [Containers and Kubernetes](containers-kubernetes.md).

If you're switching over from an existing native install,
[Migrate to container](migrate-to-container.md) covers config
carryover, stats DB carryover, and the rollback path.
