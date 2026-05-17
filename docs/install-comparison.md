# Choose your install

Eneru ships in three deployment profiles. v5.5 made the OCI container a
first-class home for **every** feature including local-host shutdown —
the previous "container = remote-only" framing from v5.4 is gone.

## The three profiles at a glance

| Install path | Local-host ownership | Remote systems | Recommended for |
|---|---|---|---|
| **pip / deb / rpm (native)** | First-class via systemd | Yes | Homelab, single-host professional, end-user-managed enterprise |
| **OCI image (Docker / Podman)** | First-class via SSH loopback delegate | Yes | Homelab, professional, enterprise — the v5.5 default for containerized local-host |
| **Kubernetes** | Not recommended | Yes | Enterprise multi-site fleet monitoring of remote systems |

### Quick decision

- "I want the simplest path on a single host and I'm comfortable with
  systemd": **native deb/rpm/pip**.
- "I want one image to deploy, I'm OK with SSH-to-localhost, I want
  to stop using package managers": **OCI image (Docker / Podman)**.
- "I have a cluster and I want to monitor a fleet of remote UPSes
  from inside the cluster": **Kubernetes**.

## Feature × install matrix

| Capability | Native (deb/rpm/pip) | OCI w/ loopback | OCI w/o loopback | Kubernetes |
|---|---|---|---|---|
| NUT polling + power-event triggers | ✅ | ✅ | ✅ | ✅ |
| Notifications (Apprise, MQTT) | ✅ | ✅ | ✅ | ✅ |
| `/health`, `/ready`, `/metrics`, `/api/v1/*` | ✅ | ✅ | ✅ | ✅ |
| Remote server shutdown (SSH) | ✅ | ✅ | ✅ | ✅ |
| **Local VM teardown** (libvirt) | ✅ in-process | ✅ via loopback SSH | ❌ | ⚠️ Not recommended |
| **Local container teardown** (Docker/Podman) | ✅ in-process | ✅ via loopback SSH | ❌ | ⚠️ Not recommended |
| **Local filesystem sync + unmount** | ✅ in-process | ✅ via loopback SSH | ❌ | ⚠️ Not recommended |
| **Local host poweroff** | ✅ direct | ✅ via loopback SSH | ❌ | ⚠️ Not recommended |
| `wall(1)` broadcast | ✅ | ⏸️ suppressed (no host tty) | ⏸️ suppressed | ⏸️ suppressed |
| Hot-reload of config | ❌ | ❌ | ❌ | ❌ |
| TUI dashboard | ✅ (curses) | ✅ (`docker exec -it eneru monitor`) | ✅ | ✅ |
| Statistics SQLite DB | ✅ | ✅ (persistent volume needed) | ✅ | ✅ |

**Legend:**

- ✅ — supported, well-tested.
- ❌ — Eneru's privilege check refuses startup (or `/ready` reports 503)
  when the config requires this capability in this profile.
- ⏸️ — silently suppressed because the capability has no observable
  effect in this runtime (e.g. `wall` inside a container reaches nobody).
- ⚠️ — technically possible if the operator explicitly enables it, but
  not recommended by the maintainer. K8s + local-host ownership in
  particular is unusual (a pod shutting down its own node mid-shutdown
  is a fundamentally awkward control loop). See the K8s notes below.

## Why three profiles, not two

The v5.5 OCI image **is** the recommended path for any user who would
have reached for the deb/rpm install on a single host. The container
SSHes to the host it runs on (the loopback delegate), so it can power
off the host, stop local VMs, stop local containers, and unmount
filesystems — without escaping its own namespaces. The host's own
`sshd` is the only thing with the privilege to actually power off
itself, which is exactly the right boundary.

The native install is still first-class for users who want a single
process on the host with no SSH-to-self ceremony. Both paths offer
the **same** feature set; pick by deployment preference, not
capability.

Kubernetes is its own profile because the local-host ownership
question doesn't translate cleanly to a pod-on-node model. A pod
shutting down its own node mid-shutdown raises ordering questions
(node drain? PDB? operator?) that Eneru doesn't try to answer. Use
K8s when you want to monitor a fleet of remote UPSes from inside a
cluster — the remote_servers shutdown path works the same as it does
on the native install.

## What the OCI image needs from you

For the **remote-only** path (no local-host ownership):

- The container image. Nothing on the host beyond a container runtime.
- A config with `is_local: false` (or no `is_local` flag — default is false).
- Optional: a Kubernetes `Secret` / Docker bind-mount for the SSH key
  used to reach remote_servers.

For the **loopback** path (full local-host ownership from a
container):

- `network_mode: "host"` — so the container's `127.0.0.1` reaches the
  host's `sshd`. Bridge mode works too but requires overriding the
  loopback `host` field to the host's bridge IP (`172.17.0.1` on
  Linux default Docker bridge).
- `-v /etc/machine-id:/etc/machine-id:ro` — **mandatory** for the
  host identity guard. Eneru reads it inside the container and
  compares it to what the host's `sshd` returns. Without the mount,
  the container generates its own random machine-id, the identity
  probe fails on the first health check, the loopback is marked
  FAILED, and `/ready` returns 503. **Fails closed by construction.**
- `-v /srv/eneru/ssh:/var/lib/eneru/ssh:ro` — SSH private key for the
  loopback. Defaults to `/var/lib/eneru/ssh/id_loopback` inside the
  container; the host's `authorized_keys` for the matching user holds
  the public key. Do not use `authorized_keys command="..."`; it
  rewrites Eneru's identity probe and generated shutdown actions. See
  [Containers and Kubernetes](containers-kubernetes.md).
- A user on the host with shutdown privilege. Either:
  - SSH as `root` (one-line setup, larger blast radius), OR
  - SSH as a dedicated user with sudo NOPASSWD on `/sbin/shutdown`
    and `/sbin/poweroff` (recommended).

See [Migrate to container](migrate-to-container.md) for a step-by-step
walk through.

## SELinux note

On RHEL / CentOS / Rocky / Alma hosts, bind-mounted files need the
correct SELinux label or the container user can't read them. Use the
`:Z` mount option to relabel:

```bash
-v /etc/machine-id:/etc/machine-id:ro,Z
-v /srv/eneru/config.yaml:/etc/ups-monitor/config.yaml:ro,Z
-v /srv/eneru/ssh:/var/lib/eneru/ssh:ro,Z
```

Use `:z` (lowercase) only when multiple containers share the same
mount source.

## Privilege model

Native install (deb/rpm/pip) is expected to run as `root` under
systemd — local actions need direct kernel calls.

OCI image runs as non-root (`uid 10001`) by default. The v5.5
privilege check accepts the loopback configuration as a substitute
for root: the actual privileged work happens on the host's `sshd`,
not inside the container. No `--privileged`, no
`--cap-add SYS_ADMIN`, no socket mounts.

Kubernetes pod follows the OCI image privilege model. Add a
`securityContext` with `runAsNonRoot: true` and a `fsGroup: 10001`
so volume mounts are readable by the container user.

## When Eneru reports `/ready` 503

Eneru is defense technology — strict readiness. ANY required
capability that's unachievable returns 503. See
[Troubleshooting](troubleshooting.md#ready-vs-503-decision-matrix)
for the full per-runtime decision table.
