# Redundancy groups

A redundancy group protects one set of resources with two or more UPS sources. Eneru shuts the group down only when the configured quorum is lost.

Use this for dual-PSU servers, A+B rack feeds, and other setups where losing one UPS does not mean the protected system has lost power.

## When to use one

| Situation | Use a redundancy group? |
|-----------|-------------------------|
| One UPS feeds one server | No. Use a normal UPS group |
| Two UPSes feed two independent racks | No. Use multi-UPS groups |
| Two UPSes feed both PSUs on the same server | Yes |
| Two UPSes feed a shared A+B rack | Yes |

The practical test is simple: if a resource can survive one UPS failure, put it in a redundancy group instead of assigning it directly to a single UPS group.

## Example

```yaml
ups:
  - name: "UPS-A@10.0.0.10"
  - name: "UPS-B@10.0.0.11"

redundancy_groups:
  - name: "rack-1-dual-psu"
    ups_sources:
      - "UPS-A@10.0.0.10"
      - "UPS-B@10.0.0.11"
    min_healthy: 1
    degraded_counts_as: healthy
    unknown_counts_as: critical
    remote_servers:
      - name: "Compute Node 1"
        enabled: true
        host: "10.0.0.20"
        user: "root"
```

With two UPSes and `min_healthy: 1`, the group tolerates one failed UPS. It shuts down only when the healthy count drops below 1.

## Fields

| Key | Default | Description |
|-----|---------|-------------|
| `name` | required | Unique group label |
| `ups_sources` | required | Two or more UPS names from the top-level `ups:` list |
| `min_healthy` | `1` | Shutdown fires when healthy member count is below this number |
| `degraded_counts_as` | `healthy` | Count DEGRADED members as `healthy` or `critical` |
| `unknown_counts_as` | `critical` | Count UNKNOWN members as `critical`, `degraded`, or `healthy` |
| `is_local` | `false` | This group powers the Eneru host and may own local resources |
| `triggers` | inherits | Trigger overrides for this group |
| `remote_servers` | `[]` | Remote resources owned by the group |
| `virtual_machines`, `containers`, `filesystems` | disabled | Local resources. Valid only when `is_local: true` |

## Quorum

The group fires when:

```text
healthy_count < min_healthy
```

For a two-UPS dual-PSU server:

| `min_healthy` | Behavior |
|---------------|----------|
| `1` | Shut down when both UPSes fail. This is the usual choice |
| `2` | Shut down when either UPS fails. This removes practical redundancy |

For a three-UPS group:

| `min_healthy` | Behavior |
|---------------|----------|
| `1` | Tolerate two failed members |
| `2` | Tolerate one failed member |
| `3` | Shut down when any member fails |

`min_healthy: 0` is invalid because the group would never shut down.

## Member states

Each UPS member is classified on every evaluator tick.

| State | Meaning |
|-------|---------|
| `HEALTHY` | UPS reports usable data and no active problem |
| `DEGRADED` | UPS is visible but in a warning state, such as on battery or voltage warning |
| `CRITICAL` | UPS hit a shutdown trigger, FSD, overload-critical path, or explicit advisory condition |
| `UNKNOWN` | Snapshot is stale, NUT connection is lost, or the monitor cannot provide current data |

`degraded_counts_as` controls whether warning states still contribute to quorum. `unknown_counts_as` controls how missing data is counted. The default is tolerant of degraded power but fail-safe on missing visibility.

## Advisory triggers

Member UPS triggers still run. In a redundancy group they do not directly run the shutdown sequence. They mark the member as advisory-critical, then the group evaluator decides whether quorum is gone.

You will see log lines like:

```text
Trigger condition met (advisory, redundancy group): battery below threshold
```

For `min_healthy: 1`, that advisory condition only shuts down the protected resource if every other member has also stopped counting as healthy.

## Local ownership

At most one group across the whole config can be `is_local: true`. That group may own local VMs, containers, filesystems, and local shutdown behavior.

This is valid:

```yaml
redundancy_groups:
  - name: "local-dual-feed"
    is_local: true
    virtual_machines:
      enabled: true
```

This is not valid if another UPS group already has `is_local: true`.

## Remote-server ownership

A remote server, identified by `host` and `user`, can belong to only one place:

- One UPS group's `remote_servers` list.
- One redundancy group's `remote_servers` list.

Validation rejects duplicate ownership so Eneru cannot shut down the same server through two paths.

## Validate

```bash
sudo eneru validate --config /etc/ups-monitor/config.yaml
```

Validation prints configured redundancy groups:

```text
Redundancy groups (1):
  1. rack-1-dual-psu
     Sources (2): UPS-A@10.0.0.10, UPS-B@10.0.0.11
     Quorum: min_healthy=1 (degraded->healthy, unknown->critical)
     Remote servers (1): Compute Node 1
```

Common validation failures:

| Error class | Cause |
|-------------|-------|
| Unknown UPS source | `ups_sources` does not exactly match a top-level `ups[].name` |
| Duplicate UPS source | Same member listed twice |
| Duplicate group name | Two redundancy groups share a name |
| Multiple local groups | More than one UPS or redundancy group has `is_local: true` |
| Duplicate remote ownership | Same `host` and `user` assigned to more than one group |

## Failure timeline

For a dual-UPS group with `min_healthy: 1` and default counting:

| Time | Event | Group result |
|------|-------|--------------|
| 0s | UPS-A loses input power | UPS-A is `DEGRADED`, UPS-B is `HEALTHY`. Quorum holds |
| 60s | UPS-A hits low battery | UPS-A is `CRITICAL`, UPS-B is `HEALTHY`. Quorum still holds |
| 90s | UPS-B also loses input power | UPS-B is `DEGRADED` and counts as healthy by default. Quorum holds |
| 120s | UPS-B hits low battery | Both members are `CRITICAL`. Quorum is lost and shutdown starts |

If you want the group to shut down as soon as a member is merely degraded, set `degraded_counts_as: critical`.

## Sizing warning

Redundancy only works if the remaining feed can carry the load. For A+B power, verify that each UPS can carry the full protected load during single-feed operation.

Good targets:

- Normal operation: each UPS at or below about 50% load.
- Single-feed degraded operation: surviving UPS below about 80% load.
- Extra headroom for inrush, battery age, and generator transitions.

If the surviving UPS overloads, software cannot save the rack. Fix the load or UPS sizing.

## Troubleshooting

| Symptom | Likely cause |
|---------|--------------|
| One UPS failed but nothing shut down | Quorum still holds. Check `min_healthy` and member states |
| On-battery member still counts healthy | `degraded_counts_as: healthy` is the default |
| Advisory log appears but no shutdown | Another member still satisfies quorum |
| Group never starts | `ups_sources` names do not match exactly |
| Repeated tests do not fire | A previous dry-run or killed process left `/var/run/ups-shutdown-redundancy-*` |

Clear stale redundancy flags only after confirming no real shutdown is in progress:

```bash
sudo rm -f /var/run/ups-shutdown-redundancy-*
```
