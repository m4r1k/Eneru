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

## Voltage thresholds (preset-driven; raw thresholds not user-configurable)

Issue [#27](https://github.com/m4r1k/Eneru/issues/27) asked for
user-tunable over-voltage and brownout thresholds. **Raw volt-level
overrides are deliberately not exposed.** A misconfigured
`warning_high: 200` on a 120V grid would mask a real damaging
over-voltage condition — Eneru would sit silent while line voltage took
out PSUs. The safety contract is non-negotiable.

The bounded escape hatch is the `voltage_sensitivity` preset (added in
v5.1.2 after issue [#4](https://github.com/m4r1k/Eneru/issues/4)),
configured per UPS group:

```yaml
ups:
  - name: "Tower UPS"
    triggers:
      voltage_sensitivity: loose      # noisy utility, dial it back
  - name: "Main Rack UPS"
    triggers:
      voltage_sensitivity: tight      # clean PDU, want early warning
  - name: "Generator-fed Rack"
    # voltage_sensitivity omitted -> defaults to `normal`
```

| Preset  | Band              | When to pick                                                  |
|---------|-------------------|---------------------------------------------------------------|
| `tight` | ±5% from nominal  | Clean PDU / managed UPS / lab environment; want early signal  |
| `normal`| ±10% from nominal | **Default.** Matches the EN 50160 / IEC 60038 envelope        |
| `loose` | ±15% from nominal | Noisy utility, generator-fed leg, hot-running grid            |

What Eneru does:

1. **Auto-detect at startup.** Read `input.voltage.nominal` from NUT
   and snap it to the nearest standard grid voltage from
   `(100, 110, 115, 120, 127, 200, 208, 220, 230, 240)` if within 15V
   tolerance.
2. **Derive grid-quality warning thresholds.** `warning_low` /
   `warning_high` = `nominal × (1 ∓ pct)` where `pct` comes from the
   `voltage_sensitivity` preset above. NUT's
   `input.transfer.{low,high}` are **cached for context only** — the
   daemon surfaces them in the startup log and in every brownout /
   over-voltage notification so the operator knows when the UPS firmware
   itself will switch to battery — but they no longer compute the
   warning band.

   Resolved thresholds across the standard grids (output of `round(nominal × (1 ∓ pct), 1)` —
   Python's banker's rounding rounds .X5 floats to even, so 109.25 prints as
   109.2 and 132.25 as 132.2; the daemon log line shows the same values):

   | Nominal | tight (±5%)   | normal (±10%) | loose (±15%)  |
   |--------:|--------------:|--------------:|--------------:|
   | 100 V   | 95.0 / 105.0  | 90.0 / 110.0  | 85.0 / 115.0  |
   | 110 V   | 104.5 / 115.5 | 99.0 / 121.0  | 93.5 / 126.5  |
   | 115 V   | 109.2 / 120.8 | 103.5 / 126.5 | 97.8 / 132.2  |
   | 120 V   | 114.0 / 126.0 | 108.0 / 132.0 | 102.0 / 138.0 |
   | 127 V   | 120.6 / 133.3 | 114.3 / 139.7 | 108.0 / 146.0 |
   | 200 V   | 190.0 / 210.0 | 180.0 / 220.0 | 170.0 / 230.0 |
   | 208 V   | 197.6 / 218.4 | 187.2 / 228.8 | 176.8 / 239.2 |
   | 220 V   | 209.0 / 231.0 | 198.0 / 242.0 | 187.0 / 253.0 |
   | 230 V   | 218.5 / 241.5 | 207.0 / 253.0 | 195.5 / 264.5 |
   | 240 V   | 228.0 / 252.0 | 216.0 / 264.0 | 204.0 / 276.0 |

   ### Vendor reference (verify against your unit)

   UPS firmware transfer points vary by family, region, and field
   tuning. The numbers below are **typical defaults, not contracts** —
   always verify against your actual hardware before relying on them:

   ```bash
   upsc <ups@host> input.voltage.nominal input.transfer.low input.transfer.high
   ```

   | Vendor / family               | Region    | Typical `transfer.low` / `transfer.high` | Behaviour with `normal` (±10%)                                            |
   |-------------------------------|:---------:|:---------------------------------------:|---------------------------------------------------------------------------|
   | APC Smart-UPS SUA / SMT / SMC | US 120 V  | ~106 / ~127 V                            | Warnings at 108 / 132 V. UPS won't switch to battery until 106 / 127 V.   |
   | APC Smart-UPS (default-wide)  | EU 230 V  | 170 / 280 V                              | Warnings at 207 / 253 V. UPS won't switch until ±20% from nominal.        |
   | APC SMX / Symmetra (managed)  | EU 230 V  | operator-tightened, often ~207 / ~253    | Warnings at 207 / 253 V. Use `tight` for 218.5 / 241.5 V.                 |
   | Eaton 5P / 9PX                | EU 230 V  | typically narrow defaults                | Warnings at 207 / 253 V. Use `tight` for an early signal.                 |
   | CyberPower CP1500             | US 120 V  | ~95 / ~140 V (wide)                      | Warnings at 108 / 132 V. UPS won't switch until 95 / 140 V.               |
   | Tripp Lite SMART series       | EU 230 V  | typically narrow                         | Warnings at 207 / 253 V.                                                   |

3. **Cross-check with observed reality.** Some UPS firmwares (notably
   on US 120V grids) mis-report `input.voltage.nominal=230`. After
   ~10 polls Eneru takes the median of observed `input.voltage`
   readings and re-snaps the nominal if the readings disagree with
   NUT by more than 25V. The re-snap is logged and recorded as a
   `VOLTAGE_AUTODETECT_MISMATCH` event in the SQLite events table:

       sqlite3 /var/lib/eneru/<UPS>.db \
         "SELECT * FROM events WHERE event_type='VOLTAGE_AUTODETECT_MISMATCH';"

   If you see one of these rows, your UPS firmware is mis-reporting
   nominal — the daemon corrected for you, but it's worth filing a
   NUT driver bug upstream. The re-snap re-applies the same percentage
   band against the new nominal (your `voltage_sensitivity` preset
   carries through).

4. **Severity-aware notification hysteresis.** The state log line for
   `OVER_VOLTAGE_DETECTED` / `BROWNOUT_DETECTED` is always written
   immediately on transition. The *notification* dispatch is gated by
   severity:
   - **Non-severe voltage warnings** (up to and including ±15% from
     nominal) go through `notifications.voltage_hysteresis_seconds`
     (default 30s). A 2-second flap to 105V on a 120V grid (12.5%
     under nominal — past the warning threshold but not severe) no
     longer pages you; a sustained event still does, and arrives
     with a `Persisted Ns.` annotation. With narrow UPS transfer
     points the warning band can be tighter than ±10%, so non-severe
     events can fire at less than 10% deviation.
   - **Severe deviations** (`>±15%` from nominal) bypass the dwell
     and notify **immediately** with a `(severe, X.X% below/above
     nominal)` tag. These signal real grid trouble — utility fault,
     generator instability, site wiring — that the operator wants to
     know about NOW, not 30 seconds from now.

   See [Notifications → Tuning alert noise](notifications.md#tuning-alert-noise).

5. **Per-event mute, with a safety blocklist.**
   `notifications.suppress: [...]` mutes specific informational
   events (AVR cycling, voltage normalized) but rejects safety-
   critical event names at config-load time. There is no way to
   silence `OVER_VOLTAGE_DETECTED`, `BROWNOUT_DETECTED`,
   `OVERLOAD_ACTIVE`, `BYPASS_MODE_ACTIVE`, `ON_BATTERY`,
   `CONNECTION_LOST`, or any `SHUTDOWN_*` event.

### What you'll see in the log

```text
📊 Voltage Monitoring Active.
   Nominal: 230.0V (NUT=230.0).
   Grid-quality warnings: 207.0V / 253.0V (±10% nominal, sensitivity=normal).
   UPS battery-switch points: 170.0V / 280.0V (from NUT input.transfer.{low,high}).
📊 Voltage auto-detect re-snap: NUT=230V disagreed with observed median 120.0V (window=[120.5, 119.0, ...]V). Re-snapped to 120V; new thresholds 108.0V / 132.0V.
⚡ POWER EVENT: VOLTAGE_AUTODETECT_MISMATCH - NUT nominal=230V, observed median=120.0V, re-snapped to 120V
```

If you upgraded from v5.1.1 and one of your UPSes had narrow firmware
transfer points (the `transfer ± 5V` candidate beat ±10% of nominal),
the daemon emits a one-time startup warning with a per-side delta so
you don't miss the change:

```text
⚠️ Voltage warning band changed from v5.1.1 on this UPS: low 220.0V→207.0V
   (widened); high 240.0V→253.0V (widened). v5.1.2 dropped the
   tighter-of-percentage-or-transfer clamp in favour of a single percentage-
   band formula (issue #4). Current band is ±10% nominal (207.0V/253.0V).
   Set 'voltage_sensitivity: tight' under this UPS's triggers block to
   restore a tighter band, or set 'voltage_sensitivity: normal' to
   acknowledge the new default and silence this warning. See
   https://eneru.readthedocs.io/latest/changelog/ for details.
```

The warning suppresses once you set `voltage_sensitivity` explicitly
(any value, even `normal`) — the daemon takes that as your decision and
stops nagging.

The autodetect-mismatch row lands in the SQLite `events` table with
`notification_sent=0` so it doesn't ping you (it's startup
information, not an active power event).

### What you'll see in a brownout notification

**Mild brownout (UPS won't switch):**

```text
🔻 BROWNOUT_DETECTED: input voltage 200.0V is 13.0% below 230V nominal
   (warning threshold 207.0V). Persisted 30s.
   UPS will not switch to battery until 170.0V (firmware setting);
   this is a grid-quality issue (outside the configured ±10% nominal band),
   not an imminent power loss.
```

**Severe brownout (UPS may switch shortly):**

```text
🔻 BROWNOUT_DETECTED: (severe, 21.7% below nominal): input voltage 180.0V.
   Notifying immediately (bypassed hysteresis).
   Approaching UPS battery-switch threshold (170.0V) -- battery may
   engage shortly.
```

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

When a UPS belongs to a [redundancy group](redundancy-groups.md), the
triggers above (T1-T4, FSD, FAILSAFE) still evaluate on the member's
monitor thread. Instead of running a local shutdown, they set an
advisory flag on the per-UPS state snapshot:

```
⚠️ Trigger condition met (advisory, redundancy group): <reason>
```

The group's `RedundancyGroupEvaluator` reads those flags from every
member, applies the group's quorum policy (`min_healthy`,
`degraded_counts_as`, `unknown_counts_as`), and only fires the group's
shutdown when fewer than `min_healthy` members contribute as healthy.
See [Redundancy Groups](redundancy-groups.md) for the full lifecycle.

A few notes when picking trigger values for redundancy members:

- Per-UPS thresholds still matter. They decide when each member
  contributes "critical" to the group.
- The FAILSAFE rule (connection lost while On Battery) does not run a
  local shutdown for a redundancy member. It sets
  `connection_state=FAILED` plus the advisory trigger, and the group
  evaluator decides via `unknown_counts_as`.
- An independent UPS group (one not referenced by any redundancy
  group) is unaffected. Its triggers run the local shutdown path
  byte-identically to single-UPS mode.
