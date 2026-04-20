# Redundancy Groups

Redundancy groups let you protect a set of resources with **two or more UPS
sources** and only act when the group's quorum is lost. This is the
canonical solution for dual-PSU servers, redundant rack feeds, and
data-centre A+B power topologies.

This page covers the **concepts and configuration**. The behaviour
(advisory triggers, evaluator timing, executor mixin composition) lands in
later commits of v5.1.0 and is documented in
[Shutdown Triggers](triggers.md) once it ships.

## When to use a redundancy group

| Situation                                                    | Use a redundancy group? |
| ------------------------------------------------------------ | ----------------------- |
| Single UPS feeding a single PSU server                       | No — use `ups:` only.   |
| Two UPSes, each feeding a *different* server                 | No — use two independent UPS groups. |
| Two UPSes feeding redundant PSUs on the *same* server / rack | **Yes.**                |
| A+B feeds across a chassis row, with cross-cabling           | **Yes.**                |

The litmus test: *if losing one UPS does **not** mean losing the resource,
the resource belongs in a redundancy group.* Eneru should only shut it
down when the redundancy itself is gone.

## Quick example

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
    remote_servers:
      - name: "Compute Node 1"
        enabled: true
        host: "10.0.0.20"
        user: "root"
```

`min_healthy: 1` means: shut down only when *fewer than 1* member UPS is
healthy — i.e., when **both** UPSes have failed. With two UPSes and
`min_healthy: 1` the rack tolerates a single UPS outage indefinitely.

## Field reference

```yaml
redundancy_groups:
  - name: "<unique-name>"             # required, used in logs / shutdown flag files
    ups_sources:                       # required, 2+ UPS names from the top-level `ups:` section
      - "UPS-A@host"
      - "UPS-B@host"
    min_healthy: 1                     # quorum: shutdown when healthy_count < min_healthy
    degraded_counts_as: "healthy"      # "healthy" | "critical"
    unknown_counts_as: "critical"      # "healthy" | "degraded" | "critical"
    is_local: false                    # at most one group across the config can be is_local
    triggers:                          # optional; inherits from top-level triggers
      low_battery_threshold: 20
    remote_servers: [...]              # owned by this group
    virtual_machines: { enabled: ... } # only valid when is_local: true
    containers: { enabled: ... }       # only valid when is_local: true
    filesystems: { ... }               # only valid when is_local: true
```

### `min_healthy`

`min_healthy` is the *quorum threshold*. Shutdown fires when

```
healthy_count(group) < min_healthy
```

For a 2-UPS group, the practical choices are:

| `min_healthy` | Behaviour                                                |
| ------------- | -------------------------------------------------------- |
| `1`           | Shut down only when **both** UPSes fail (recommended).   |
| `2`           | Shut down when **either** UPS fails (no redundancy — flagged with a warning during validation). |

For a 3-UPS group:

| `min_healthy` | Behaviour                                                |
| ------------- | -------------------------------------------------------- |
| `1`           | Tolerate any 2 simultaneous failures.                    |
| `2`           | Tolerate any 1 failure; shut down on the second.         |
| `3`           | No redundancy (warning).                                 |

`min_healthy: 0` is rejected — a group that never triggers a shutdown is
never useful; remove the group instead.

### `degraded_counts_as`

A *degraded* member UPS is one that is reporting valid data but in a
warning state — e.g. voltage outside thresholds, AVR active, on battery
but above all triggers. `degraded_counts_as` controls how that contributes
to `healthy_count`:

| Value      | Effect                                            |
| ---------- | ------------------------------------------------- |
| `healthy`  | (default) Degraded UPSes still count as healthy. Tolerant — fits homelab and most production environments. |
| `critical` | Degraded UPSes count as failed. Strict — fits sites where any voltage warning means "the protective margin is gone, pre-emptively shut down". |

### `unknown_counts_as`

A member UPS becomes *unknown* when the snapshot is stale (no successful
poll for `5 * check_interval` seconds), the NUT connection has dropped,
or the per-UPS monitor thread is mid-recovery from a connection failure.

| Value      | Effect                                            |
| ---------- | ------------------------------------------------- |
| `critical` | (default) Unknown counts as failed — *fail-safe*. If you cannot see the UPS, assume the worst. |
| `degraded` | Unknown counts as degraded; then `degraded_counts_as` decides whether that maps to healthy or critical. Useful when you have flaky NUT servers and want a controlled escalation. |
| `healthy`  | Unknown counts as healthy. **Risky** — only use when your UPSes are genuinely independent and a transient NUT outage is *more likely* than a real power event. |

The conservative choice (`critical`) is the right default for almost
everyone. Consider `degraded` only after you have hardened your NUT setup
and verified flap behaviour with the connection-loss grace period.

### `is_local`

`is_local: true` declares that this redundancy group powers the Eneru
host itself, allowing the group to declare local resources
(`virtual_machines`, `containers`, `filesystems`).

**At most one group across the entire configuration** — UPS group **or**
redundancy group — can be `is_local: true`. Validation rejects
configurations that declare two `is_local` groups.

### Resource ownership rules

A remote server (identified by `host`+`user`) must belong to exactly one
tier:

- It can live under a single `ups_groups[*].remote_servers` entry, **or**
- It can live under a single `redundancy_groups[*].remote_servers` entry.

Listing the same `host`+`user` in both tiers is rejected at validation
time — otherwise, Eneru would shut the same server down through two
different code paths.

## Validating your config

```bash
eneru validate --config /etc/ups-monitor/config.yaml
```

The output gains a "Redundancy groups" section when any are configured:

```
  Redundancy groups (1):
    1. rack-1-dual-psu
       Sources (2): UPS-A@10.0.0.10, UPS-B@10.0.0.11
       Quorum: min_healthy=1 (degraded→healthy, unknown→critical)
       Remote servers (2): Compute Node 1, Compute Node 2
```

If the config is invalid, `validate` exits non-zero and prints the
specific rules that failed (`min_healthy must be >= 1`, `references
unknown UPS name(s)`, `Multiple groups marked as is_local`, etc.).

## Working example

A complete dual-PSU config is shipped at
[`examples/config-redundancy.yaml`](https://github.com/m4r1k/Eneru/blob/main/examples/config-redundancy.yaml).
Copy it as a starting point and adjust the `ups_sources`, `host`s, and
SSH user names for your environment.

## How it actually fires

Each member UPS keeps polling on its own thread at its `check_interval`.
When a per-UPS trigger condition is met (low battery, low runtime,
depletion rate, extended time on battery, FSD signal, or FAILSAFE), the
member doesn't run a local shutdown — it sets an **advisory flag** in its
state snapshot and emits a log line:

```
⚠️ Trigger condition met (advisory, redundancy group): <reason>
```

A separate `RedundancyGroupEvaluator` thread (one per group, ~1 s tick)
reads every member's snapshot under a lock, classifies each into
`HEALTHY` / `DEGRADED` / `CRITICAL` / `UNKNOWN`, applies the group's
`degraded_counts_as` / `unknown_counts_as` policy, and only fires the
group's shutdown when:

```
healthy_count(group) < min_healthy
```

When the group fires, the executor reuses the same shutdown mixins as
the per-UPS path — so multi-phase ordering (`shutdown_order`),
`shutdown_safety_margin`, and deadline-based join behave identically.
The group's flag file lives at
`/var/run/ups-shutdown-redundancy-{sanitized-group-name}` and makes the
shutdown idempotent across signals or restarts mid-flow.

### Cascade timeline (dual-PSU, `min_healthy: 1`)

| Time   | Event                                        | Group state                     |
| ------ | -------------------------------------------- | ------------------------------- |
| t = 0  | UPS-A loses input power → on battery         | UPS-A=DEGRADED, UPS-B=HEALTHY → quorum holds |
| t = 30 | UPS-A battery drops below threshold          | UPS-A advisory trigger set; CRITICAL. UPS-B=HEALTHY → quorum still holds |
| t = 90 | UPS-B also loses input power                 | UPS-B=DEGRADED → still 1 healthy via DEGRADED→healthy → quorum holds |
| t = 95 | UPS-B battery also drops below threshold     | both CRITICAL → quorum LOST → executor fires |

### Load redistribution guidance

When one UPS in the group loses input power, the *other* UPS picks up
the full rack load. Rack-level UPSes are typically sized for the *peak*
load on a single feed, so this is fine — but verify your sizing:

- Peak load per UPS during normal (both feeds up) ≤ 50% of rated output.
- Peak load per UPS during single-feed degraded mode ≤ 80% of rated output.
- Margin for inrush / boot transients is left at 20%.

If the surviving UPS is overloaded (the `OVERLOAD` flag flips to ACTIVE),
that UPS will likely flip to bypass or drop the load entirely. There is
no software workaround — fix the load.

## See also

- [Configuration reference](configuration.md) — every field, with defaults.
- [Shutdown triggers](triggers.md) — per-UPS triggers (still active in
  advisory mode for redundancy-group members).
- [Remote servers](remote-servers.md) — SSH / pre-shutdown command
  semantics. Identical between independent and redundancy-group servers.
- [Troubleshooting](troubleshooting.md) — "why isn't my redundancy
  shutdown firing?" and friends.
