# Containers and Kubernetes

Eneru v5.4 publishes one OCI image:

```bash
docker pull ghcr.io/m4r1k/eneru:5.4.0
podman pull ghcr.io/m4r1k/eneru:5.4.0
```

Use the image for remote-only deployments: Eneru monitors NUT, exposes `/health` and `/ready`, and shuts down remote servers over SSH. Native deb/rpm packages remain the recommended path when Eneru must shut down the local host, local VMs, local containers, or local filesystems.

## Tags

| Tag | Meaning |
|-----|---------|
| `5.4.0`, `5.4.0-rc2`, etc. | Exact release |
| `latest` | Latest stable release |
| `testing` | Latest pre-release |

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
  ghcr.io/m4r1k/eneru:5.4.0 \
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
  ghcr.io/m4r1k/eneru:5.4.0 \
  run --config /etc/ups-monitor/config.yaml \
  --api --api-bind 0.0.0.0
```

Rootless Podman works for remote-only deployments as long as the mounted state and SSH-key paths are readable by the container user. If you mount the host Docker or Podman socket for local container orchestration, that is no longer remote-only and must be treated as privileged local-host access.

## AppArmor

The default Docker AppArmor profile is enough for remote-only Eneru because it needs ordinary network access and writable state directories. Do not disable AppArmor for remote-only deployments. Local-host orchestration is different: stopping host containers, VMs, filesystems, or the host itself requires additional host access and usually belongs on the native systemd install path.

## Local-host orchestration in a container

If you configure `is_local: true`, local shutdown, local VMs, local containers, or filesystem unmounts, Eneru must run as root. A non-root container exits at startup with the local feature that requires root.

For local container shutdown, mount the relevant runtime socket and keep Eneru outside the shutdown target set where possible:

```bash
docker run -d --name eneru \
  --user 0:0 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v /srv/eneru/config.yaml:/etc/ups-monitor/config.yaml:ro \
  -v /srv/eneru/state:/var/lib/eneru \
  ghcr.io/m4r1k/eneru:5.4.0
```

Eneru auto-detects and skips its own container during the remaining-container stop phase. If a configured compose file includes the Eneru container, Eneru skips `compose down` for that file to avoid killing itself.

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

The image logs to stdout. Use:

```bash
kubectl logs deploy/eneru
```

For Docker or Podman:

```bash
docker logs eneru
podman logs eneru
```
