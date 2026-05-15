# Containers and Kubernetes

Eneru v5.4 publishes one OCI image:

```bash
docker pull ghcr.io/m4r1k/eneru:latest
podman pull ghcr.io/m4r1k/eneru:latest
```

Use the image for remote-only deployments: Eneru monitors NUT, exposes `/health` and `/ready`, and shuts down remote servers over SSH. Native deb/rpm packages remain the recommended path when Eneru must shut down the local host, local VMs, local containers, or local filesystems.

## Tags

| Tag | Meaning |
|-----|---------|
| `5.4.0`, `5.4.1`, etc. | Exact stable release. Immutable. |
| `5.4.0-rc1`, `5.4.0-rc2`, etc. | Exact pre-release. Immutable. |
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
  -v /srv/eneru/ssh:/var/lib/eneru/ssh:ro \
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

## Podman and SELinux

On SELinux hosts, label bind mounts for container access. Use `:Z` for private mounts and `:z` for shared mounts:

```bash
podman run -d --name eneru \
  --replace \
  -p 9191:9191 \
  -v /srv/eneru/config.yaml:/etc/ups-monitor/config.yaml:ro,Z \
  -v /srv/eneru/state:/var/lib/eneru:Z \
  -v /srv/eneru/run:/var/run/eneru:Z \
  -v /srv/eneru/ssh:/var/lib/eneru/ssh:ro,Z \
  ghcr.io/m4r1k/eneru:latest \
  run --config /etc/ups-monitor/config.yaml \
  --api --api-bind 0.0.0.0
```

Rootless Podman works for remote-only deployments as long as the mounted state and SSH-key paths are readable by the container user.

## AppArmor

The default Docker AppArmor profile is enough for remote-only Eneru because it needs ordinary network access and writable state directories. Do not disable AppArmor for remote-only deployments. If Eneru must stop local VMs, local containers, filesystems, or the host itself, use a native host install instead of the OCI image.

## Kubernetes

The Kubernetes examples under `deploy/kubernetes/` are remote-only. They run as UID 10001, mount config through a ConfigMap, mount SSH keys through a Secret, and use HTTP probes:

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

2. **`/var/log/eneru/ups-monitor.log`** when `logging.file` points there. Mount a persistent volume at `/var/log/eneru` so the file survives container restarts and gives you a forensic timeline after the runtime log buffer rotates away. The container `eneru` user is UID/GID 10001, so the volume must be group-writable by 10001 — `fsGroup: 10001` in the pod `securityContext` (the sample manifests do this) is the simplest way.

   Docker bind mount:

   ```bash
   -v /srv/eneru/logs:/var/log/eneru:Z
   ```

   The Kubernetes samples mount an `emptyDir` at `/var/log/eneru` so the path exists; swap in a `PersistentVolumeClaim` if you want the log file to survive pod rescheduling.
