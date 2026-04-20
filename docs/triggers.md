# Shutdown triggers

Eneru uses multiple independent triggers to decide when to shut down. This way, if one metric is unreliable (e.g., aged batteries with bad runtime estimates), other triggers can still catch the problem.

!!! info "Beyond NUT's built-in triggers"
    NUT's `upsmon` supports two shutdown conditions: `LOWBATT` (UPS hardware signals low battery) and `FSD` (forced shutdown flag). These depend on the UPS firmware's own assessment, which can be unreliable with aged batteries or certain hardware. Eneru adds four more triggers computed from observed data to cover scenarios that firmware-based triggers miss.

---

## Trigger priority

When on battery power, triggers are evaluated in this order. The **first** condition met initiates shutdown:

| Priority | Trigger | Default | Purpose |
|----------|---------|---------|---------|
| 1 | FSD Flag | N/A | UPS signals forced shutdown |
| 2 | Low Battery | 20% | Battery percentage critically low |
| 3 | Critical Runtime | 10 min | Estimated runtime too short |
| 4 | Depletion Rate | 15%/min | Battery draining dangerously fast |
| 5 | Extended Time | 15 min | Safety net for prolonged outages |


---

## Low battery threshold

```yaml
triggers:
  low_battery_threshold: 20  # percentage
```

Triggers shutdown when battery charge falls below the configured percentage.

**When it helps:**

- Available on all UPS devices
- Works when runtime estimates are unavailable or inaccurate
- Hard floor regardless of load conditions

**Example:** With threshold at 20%, shutdown triggers when battery reports 19% or lower.

---

## Critical runtime threshold

```yaml
triggers:
  critical_runtime_threshold: 600  # seconds (10 minutes)
```

Triggers shutdown when the UPS-estimated remaining runtime falls below the configured value.

**When it helps:**

- Accounts for current load, since the UPS calculates runtime from actual power draw
- More accurate than battery percentage alone under varying loads

### How runtime is calculated by the UPS

The UPS continuously measures current battery capacity, power draw (load), and battery voltage curve to estimate: *"At this load, the battery will last X more seconds."*

**Example scenario:**

```
Battery: 50%
Load: 80% (high)
UPS Runtime Estimate: 8 minutes

Even though battery shows 50%, high load means only 8 minutes remain.
With threshold at 10 minutes (600s), shutdown triggers.
```

**Limitations:**

- Estimates can be inaccurate with aged batteries
- Some UPS models provide unreliable estimates
- Sudden load changes cause estimate jumps

Multiple triggers compensate for each other's weaknesses.

---

## Depletion rate

```yaml
triggers:
  depletion:
    window: 300         # seconds (5 minutes)
    critical_rate: 15.0 # percent per minute
    grace_period: 90    # seconds
```

The depletion rate measures **how fast the battery is draining** based on observed data, independent of UPS estimates.

### How depletion rate is calculated

The daemon maintains a rolling history of battery readings within the configured window (default: 5 minutes).

**Step 1: Collect Data**

Every check cycle (default: 1 second), the current battery percentage and timestamp are recorded:

```
History Buffer (last 5 minutes):
┌──────────────┬───────────┐
│ Time         │ Battery   │
├──────────────┼───────────┤
│ 5 min ago    │ 85%       │ ← Oldest reading
│ 4 min ago    │ 82%       │
│ 3 min ago    │ 79%       │
│ 2 min ago    │ 76%       │
│ 1 min ago    │ 73%       │
│ Now          │ 70%       │ ← Current reading
└──────────────┴───────────┘
```

**Step 2: Calculate Rate**

Compare the oldest reading to the current reading:

```
Battery difference = 85% - 70% = 15%
Time difference    = 5 minutes
Depletion rate     = 15% ÷ 5 min = 3%/min
```

**Step 3: Evaluate**

If rate exceeds threshold (default: 15%/min) and grace period has passed, trigger shutdown.

### Minimum data requirement

At least 30 readings are required before calculating a rate. With 1-second intervals, this means 30 seconds of data minimum. This prevents false positives from single bad readings, startup transients, or statistical noise in short samples.

### The grace period

When power fails, battery readings are often unstable for the first 30-90 seconds as the UPS recalibrates:

```
Time 0s:   Power fails
Time 1s:   Battery reads 100%
Time 2s:   Battery reads 95%   ← Sudden drop (recalibrating)
Time 5s:   Battery reads 91%   ← Still adjusting
Time 10s:  Battery reads 94%   ← Bouncing back
Time 30s:  Battery reads 93%   ← Stabilizing
Time 90s:  Battery reads 91%   ← Reliable now
```

Without a grace period, the initial 100% to 91% drop in 10 seconds would calculate as **54%/min**, triggering a false shutdown.

The grace period (default: 90 seconds) ignores high depletion rates immediately after power loss:

```
Timeline with 90s grace period:

Time     On Battery   Rate        Action
──────────────────────────────────────────────
0s       0s           N/A         Power lost
10s      10s          54%/min     Ignored (grace period)
30s      30s          28%/min     Ignored (grace period)
60s      60s          16%/min     Ignored (grace period)
90s      90s          12%/min     Evaluated → OK (below 15%)
120s     120s         18%/min     SHUTDOWN TRIGGERED
```

### When depletion rate helps

- Old batteries may show 50% charge but drain to 0% in minutes
- Some UPS models have unreliable runtime calculations
- Catches load spikes mid-outage
- Uses observed data rather than UPS predictions

---

## Extended time on battery

```yaml
triggers:
  extended_time:
    enabled: true
    threshold: 900  # seconds (15 minutes)
```

Triggers shutdown after the system has been running on battery for the configured duration, regardless of battery level or runtime estimates.

**When it helps:**

- If battery still shows 80% after 15 minutes, something may be wrong
- Old batteries can suddenly fail after appearing stable
- Catches scenarios where UPS reports incorrect data
- Shuts down gracefully before potential battery cliff

### Example scenarios

**Scenario 1: Reliable data, extended outage**

```
Power out for 15 minutes
Battery: 45%
Runtime estimate: 20 minutes
Depletion rate: 3%/min

All metrics look fine, but extended time threshold reached.
Shutdown triggered as a precaution.
```

**Scenario 2: Unreliable UPS data**

```
Power out for 15 minutes
Battery: 75% (stuck/not updating)
Runtime estimate: 45 minutes (clearly wrong)
Depletion rate: 0%/min (no change detected)

Something is wrong with UPS reporting.
Extended time safety net catches this and triggers shutdown.
```

### Disabling extended time

For environments where long outages are expected and battery capacity is sufficient:

```yaml
triggers:
  extended_time:
    enabled: false
```

When disabled, Eneru logs when the threshold is exceeded but does not trigger shutdown.

---

## Critical runtime vs extended time

These two triggers cover different failure modes:

| Trigger | Based On | Catches |
|---------|----------|---------|
| Critical Runtime | UPS estimate | High load draining battery fast |
| Extended Time | Wall clock | Prolonged outage, unreliable UPS data |

**Example: Low load, long outage**

```
Runtime estimate: 2 hours (high, low load)
Actual time on battery: 20 minutes

Critical runtime won't trigger (estimate is high).
Extended time triggers at 15 minutes.
```

**Example: High load, short outage**

```
Runtime estimate: 5 minutes (low, high load)
Actual time on battery: 3 minutes

Critical runtime triggers at 10-minute threshold.
Extended time never reached.
```

---

## Failsafe battery protection (FSB)

Eneru has a hardcoded failsafe beyond the configured triggers:

!!! warning "Immediate shutdown"
    If connection to the UPS is lost while running on battery, immediate shutdown is triggered.

This catches:

- NUT server crash during outage
- Network failure to remote NUT server
- USB cable disconnect
- UPS communication failure

If the system was on battery and can no longer confirm UPS status, it assumes the worst and shuts down.

```
Timeline:
1. Power fails, system on battery (OB status)
2. UPS connection lost (network issue, NUT crash, etc.)
3. Script detects stale/missing data
4. FSB triggers: "Was on battery and lost visibility, shut down NOW"
```

!!! note "Grace period does not affect FSB"
    The [connection loss grace period](configuration.md#connection-loss-grace-period) only suppresses notifications when the UPS is on line power. If the system is on battery when the connection is lost, FSB triggers immediately regardless of grace period settings.

---

## FSD (forced shutdown) flag

The highest priority trigger. When the UPS itself signals FSD, shutdown is immediate.

FSD is set when:

- UPS battery is critically low (UPS-determined)
- UPS is commanding connected systems to shut down
- UPS is about to cut power

The UPS has direct knowledge of its own state and may cut power imminently, so all other triggers defer to FSD.

---

## Why multiple triggers?

Each trigger catches scenarios the others might miss:

| Scenario | Low Battery | Runtime | Depletion | Extended |
|----------|:-----------:|:-------:|:---------:|:--------:|
| Normal discharge | ✓ | ✓ | ✓ | ✓ |
| Aged battery (sudden failure) | ✗ | ✗ | ✓ | ✓ |
| UPS reporting stuck values | ✗ | ✗ | ✗ | ✓ |
| High load spike | ✓ | ✓ | ✓ | ✗ |
| Inaccurate runtime estimate | ✓ | ✗ | ✓ | ✓ |
| Very slow discharge | ✓ | ✓ | ✗ | ✓ |

✓ = Would catch this scenario | ✗ = Might miss this scenario

---

## Trigger evaluation flow

```
                    ┌─────────────────────┐
                    │  On Battery Power   │
                    └──────────┬──────────┘
                               │
                               ▼
                    ┌─────────────────────┐
                    │  FSD Flag Set?      │───Yes───▶ SHUTDOWN
                    └──────────┬──────────┘
                               │ No
                               ▼
                    ┌─────────────────────┐
                    │  Battery < 20%?     │───Yes───▶ SHUTDOWN
                    └──────────┬──────────┘
                               │ No
                               ▼
                    ┌─────────────────────┐
                    │  Runtime < 10min?   │───Yes───▶ SHUTDOWN
                    └──────────┬──────────┘
                               │ No
                               ▼
                    ┌─────────────────────┐
                    │  Depletion > 15%/m? │───Yes───┐
                    └──────────┬──────────┘         │
                               │ No                 ▼
                               │          ┌─────────────────────┐
                               │          │  Grace Period Over? │──No──▶ Log & Continue
                               │          └──────────┬──────────┘
                               │                     │ Yes
                               │                     ▼
                               │                  SHUTDOWN
                               ▼
                    ┌─────────────────────┐
                    │  On Battery > 15m?  │───Yes───┐
                    └──────────┬──────────┘         │
                               │ No                 ▼
                               │          ┌─────────────────────┐
                               │          │  Extended Enabled?  │──No──▶ Log & Continue
                               │          └──────────┬──────────┘
                               │                     │ Yes
                               │                     ▼
                               │                  SHUTDOWN
                               ▼
                    ┌─────────────────────┐
                    │  Continue Monitoring│
                    │  (check again in 1s)│
                    └─────────────────────┘
```

---

## Recommended configurations

### Conservative (maximum protection)

```yaml
triggers:
  low_battery_threshold: 30
  critical_runtime_threshold: 900  # 15 minutes
  depletion:
    window: 300
    critical_rate: 10.0
    grace_period: 90
  extended_time:
    enabled: true
    threshold: 600  # 10 minutes
```

Shuts down early, prioritizes data safety over runtime.

### Balanced (Default)

```yaml
triggers:
  low_battery_threshold: 20
  critical_runtime_threshold: 600  # 10 minutes
  depletion:
    window: 300
    critical_rate: 15.0
    grace_period: 90
  extended_time:
    enabled: true
    threshold: 900  # 15 minutes
```

Balances protection against unnecessary shutdowns.

### Aggressive (maximum runtime)

```yaml
triggers:
  low_battery_threshold: 10
  critical_runtime_threshold: 300  # 5 minutes
  depletion:
    window: 300
    critical_rate: 20.0
    grace_period: 120
  extended_time:
    enabled: false
```

Maximizes runtime, accepts higher risk. Only recommended with reliable UPS and new batteries.

---

## Triggers in redundancy groups

When a UPS belongs to a [redundancy group](redundancy-groups.md), all of
the triggers above (T1-T4, FSD, FAILSAFE) still evaluate normally on the
member's monitor thread — but instead of executing a local shutdown,
they set an **advisory flag** on the per-UPS state snapshot:

```
⚠️ Trigger condition met (advisory, redundancy group): <reason>
```

The group's `RedundancyGroupEvaluator` reads those flags from every
member, applies the group's quorum policy
(`min_healthy`, `degraded_counts_as`, `unknown_counts_as`), and only
fires the **group's** shutdown when fewer than `min_healthy` members
contribute as healthy. See
[Redundancy Groups](redundancy-groups.md) for the full lifecycle.

Practical implications when configuring triggers for redundancy members:

- Per-UPS thresholds still need to be sensible — they decide *when*
  each member contributes "critical" to the group.
- The FAILSAFE rule (connection lost while On Battery) does not run a
  local shutdown for a redundancy member; it sets
  `connection_state=FAILED` + the advisory trigger, and the group
  evaluator decides via `unknown_counts_as`.
- An independent UPS group (one not referenced by any redundancy group)
  is unaffected — its triggers run the local shutdown path
  byte-identically to single-UPS mode.
