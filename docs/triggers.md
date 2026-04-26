# Shutdown triggers

Eneru starts shutdown when any configured trigger says the remaining power is no longer safe. The triggers overlap on purpose. UPS firmware can report bad runtime estimates, old batteries can fall off a cliff, and networked NUT servers can disappear during the event you most need them.

## Decision order

When a UPS is on battery, Eneru evaluates these conditions in order. The first matching condition starts shutdown.

| Order | Trigger | Default | Why it exists |
|-------|---------|---------|---------------|
| 1 | FSD flag | UPS-provided | The UPS is explicitly telling clients to shut down |
| 2 | Low battery | `20%` | Hard floor when the battery is nearly empty |
| 3 | Critical runtime | `600s` | UPS estimate says runtime is too short |
| 4 | Depletion rate | `15%/min` after `90s` | Observed battery loss is too fast |
| 5 | Extended time | `900s` | Safety net for long outages or stuck UPS readings |
| Always | Failsafe battery protection | built in | Connection lost while on battery means shut down now |

Only on-battery status activates the shutdown triggers. Voltage, AVR, bypass, overload, and battery anomaly events are health alerts unless they also lead to one of the trigger conditions above.

### Power-event evaluation timeline

This shows the normal path for a UPS that goes on battery but does not hit a shutdown trigger immediately. The numbers use the defaults from `src/eneru/config.py`: `check_interval: 1`, depletion `grace_period: 90`, critical runtime `600`, and extended time `900`.

| Time | UPS data | Eneru action |
|------|----------|--------------|
| 0s | `ups.status` changes from `OL` to `OB` | Logs the transition and records `ON_BATTERY` |
| Next poll | Fresh NUT snapshot arrives | Checks FSD, battery percentage, runtime, depletion, and extended-time triggers in priority order |
| About 30 polls | Battery history reaches 30 readings | Depletion rate can now be calculated. With the default 1s `check_interval`, this is roughly 30s, but the 90s depletion grace period can still block shutdown |
| 90s | Default depletion grace period expires | Depletion rate can trigger if it is above 15%/min |
| Any poll | Runtime drops below 600s | Critical-runtime trigger fires unless an earlier trigger already started shutdown |
| First poll after 900s | `time_on_battery > 900s` | Extended-time trigger fires even if battery and runtime still look safe |
| Any poll | Power returns to `OL` | On-battery timers reset and no shutdown starts |

## Low battery

```yaml
triggers:
  low_battery_threshold: 20
```

Eneru shuts down when `battery.charge` drops below the threshold. This is the simplest and most portable trigger. It works even when runtime estimates are missing or jump around.

### Low-battery timeline

| Time | Battery reading | Eneru action |
|------|-----------------|--------------|
| 0s | UPS goes on battery at 45% | Logs `ON_BATTERY` and keeps monitoring |
| 5m | Battery reaches 24% | Still above the default 20% threshold; no shutdown |
| 8m | Battery reaches 19% | Low-battery trigger fires because the code checks `battery < 20` |
| 8m+ | Configured shutdown sequence starts | Local VMs, containers, filesystems, remote servers, and local poweroff run according to the enabled sections |

## Critical runtime

```yaml
triggers:
  critical_runtime_threshold: 600
```

Eneru shuts down when the UPS-reported runtime estimate falls below the threshold. Runtime accounts for current load, so a UPS at 50% charge can still be critical if the load is high.

Runtime estimates come from UPS firmware. They can be wrong on old batteries, cheap units, or immediately after a load change. Keep the other triggers enabled.

### Runtime-trigger timeline

| Time | Runtime estimate | Eneru action |
|------|------------------|--------------|
| 0s | UPS goes on battery, runtime estimate is 1800s | Continue monitoring |
| 2m | Load rises and runtime estimate falls to 800s | Still above threshold |
| 3m | Runtime estimate falls to 590s | Critical-runtime trigger fires because the code checks `runtime < critical_runtime_threshold` |
| 3m+ | Shutdown sequence starts | Battery percentage may still be high; runtime is the limiting metric |

## Depletion rate

```yaml
triggers:
  depletion:
    window: 300
    critical_rate: 15.0
    grace_period: 90
```

Depletion rate is calculated from observed battery readings, not the UPS runtime estimate.

```text
oldest reading in window: 85%
current reading:          70%
elapsed time:              5 minutes
depletion rate:            3%/min
```

Eneru needs at least 30 readings before the rate can trigger. The `grace_period` ignores the first part of an outage because many UPSes recalibrate battery charge right after power is lost.

### Timeline with 90s grace period

The rate is not even returned until 30 readings are present. At the default 1-second poll interval, that is roughly 30 seconds; with a different `check_interval`, scale that part accordingly.

| Time | On battery for | Example rate | Eneru action |
|------|----------------|--------------|--------------|
| 0s | 0s | N/A | Power loss detected; no depletion rate yet |
| 10s | 10s | 54%/min | Ignored because there are fewer than 30 readings and Eneru is still inside the grace period |
| About 30 polls | About 30s at default polling | 28%/min | Rate can be calculated, but shutdown is still blocked by the 90s grace period |
| 60s | 60s | 16%/min | Still ignored inside grace period |
| 90s | 90s | 12%/min | Grace period has expired, but the rate is below the default 15%/min threshold |
| 120s | 120s | 18%/min | Depletion trigger fires |

Use this trigger to catch:

- Old batteries that drop from a healthy-looking charge to empty very quickly.
- Load spikes during an outage.
- UPSes with optimistic runtime estimates.

## Extended time on battery

```yaml
triggers:
  extended_time:
    enabled: true
    threshold: 900
```

Extended time shuts down after the UPS has been on battery for the configured number of seconds. It does not care what the charge or runtime estimate says.

This catches long outages where the battery still looks safe but you do not want the system running indefinitely. It also catches UPSes that stop updating battery values correctly.

### Extended-time timeline

| Time | UPS report | Eneru action |
|------|------------|--------------|
| 0s | Power loss, battery 100% | Starts the time-on-battery counter |
| 5m | Battery 85%, runtime estimate 40m | No extended-time trigger yet |
| 10m | Battery 75%, runtime estimate 35m | Still below the default 900s threshold |
| First poll after 15m | `time_on_battery > 900s` | Extended-time trigger fires as a safety net. The code uses a strict greater-than comparison, so it fires on the first poll after the threshold is exceeded |

Disable it only if your site intentionally rides through long outages and you trust the UPS telemetry:

```yaml
triggers:
  extended_time:
    enabled: false
```

## FSD flag

FSD is the highest-priority trigger. If `ups.status` includes `FSD`, Eneru starts shutdown immediately. The UPS may be about to cut output power.

## Failsafe battery protection

If Eneru was on battery and then loses visibility of the UPS, it starts shutdown immediately. This is not configurable.

Examples:

- NUT server dies during an outage.
- Network path to a remote NUT server drops.
- USB connection to the UPS fails.
- The UPS stops returning fresh data.

The connection-loss grace period only applies while the UPS is on line power. It does not delay failsafe shutdown.

### Failsafe timeline

| Time | State | Eneru action |
|------|-------|--------------|
| Line power | NUT connection drops while previous UPS status is `OL` | Connection grace may suppress the notification |
| Line power | NUT returns during grace | No shutdown. The recovery can count toward flap detection |
| On battery | NUT connection drops while previous UPS status is `OB` | Failsafe bypasses grace and starts shutdown immediately |
| Afterwards | UPS visibility remains lost | Eneru assumes the UPS may be near cut-off and runs the shutdown sequence |

## Voltage sensitivity

Voltage monitoring warns about line power problems before, during, and after UPS battery events. Eneru does not expose raw voltage thresholds because a bad value could hide a damaging over-voltage or brownout.

Use the bounded preset instead:

```yaml
triggers:
  voltage_sensitivity: normal
```

| Preset | Band from nominal | Use it for |
|--------|-------------------|------------|
| `tight` | +/- 5% | Clean power, managed UPSes, lab environments |
| `normal` | +/- 10% | Default. Matches common grid quality expectations |
| `loose` | +/- 15% | Noisy utility power, generator-fed circuits, hot-running grids |

Eneru reads `input.voltage.nominal`, snaps it to a standard grid voltage when possible, then derives warning thresholds from the preset.

| Nominal | tight | normal | loose |
|--------:|------:|-------:|------:|
| 100 V | 95.0 / 105.0 | 90.0 / 110.0 | 85.0 / 115.0 |
| 120 V | 114.0 / 126.0 | 108.0 / 132.0 | 102.0 / 138.0 |
| 127 V | 120.7 / 133.4 | 114.3 / 139.7 | 108.0 / 146.1 |
| 208 V | 197.6 / 218.4 | 187.2 / 228.8 | 176.8 / 239.2 |
| 220 V | 209.0 / 231.0 | 198.0 / 242.0 | 187.0 / 253.0 |
| 230 V | 218.5 / 241.5 | 207.0 / 253.0 | 195.5 / 264.5 |
| 240 V | 228.0 / 252.0 | 216.0 / 264.0 | 204.0 / 276.0 |

Some UPS firmware reports the wrong nominal voltage, especially 120 V devices claiming 230 V. After startup, Eneru compares the observed input voltage median with the NUT nominal value. If they disagree sharply, Eneru re-snaps the nominal, logs the correction, and records `VOLTAGE_AUTODETECT_MISMATCH` in the stats database.

Check your UPS transfer points directly:

```bash
upsc UPS@192.168.1.100 input.voltage.nominal input.transfer.low input.transfer.high
```

Transfer points are the UPS firmware's battery-switch thresholds. Eneru includes them in logs and notifications for context, but the warning band comes from `voltage_sensitivity`.

### Common UPS transfer points

These are typical vendor defaults. Always confirm against your own unit with the `upsc` command above. Managed UPSes are often retuned by the operator, and firmware revisions vary.

| UPS family | Grid | Transfer points (low / high) | Notes |
|------------|------|------------------------------|-------|
| APC Smart-UPS SUA / SMT / SMC | US 120 V  | ~106 / ~127 V                          | Eneru `normal` warns at 108 / 132 V before the UPS switches. `tight` (114 / 126 V) fires inside the firmware band, which is too aggressive. |
| APC Smart-UPS (default-wide)  | EU 230 V  | 170 / 280 V                            | Wide factory band. `normal` (207 / 253 V) gives plenty of warning before the UPS switches. |
| APC SMX / Symmetra (managed)  | EU 230 V  | operator-tightened, often ~207 / ~253  | Use `tight` (218.5 / 241.5 V) for an early signal when the UPS itself is tightened. |
| Eaton 5P / 9PX                | EU 230 V  | typically narrow defaults              | `tight` is reasonable; `normal` still warns before the UPS acts. |
| CyberPower CP1500             | US 120 V  | ~95 / ~140 V (wide)                    | `normal` (108 / 132 V) warns well before the UPS switches; `loose` matches the firmware band. |
| Tripp Lite SMART series       | EU 230 V  | typically narrow                       | `tight` matches the firmware band; `normal` adds warning headroom. |

### Voltage auto-detect timeline

This timeline is backed by `AUTODETECT_OBSERVATION_COUNT = 10` and `AUTODETECT_DISCREPANCY_V = 25.0` in `src/eneru/health/voltage.py`.

| Time | Data | Eneru action |
|------|------|--------------|
| Startup | NUT reports `input.voltage.nominal=230` | Computes initial thresholds from 230 V |
| First 10 valid voltage polls | Observed input voltage is around 120 V | Fills the 10-reading auto-detect observation window |
| 10th valid reading | Median differs from NUT nominal by more than 25 V | Re-snaps nominal voltage to the nearest standard grid, such as 120 V |
| Same startup | Corrected nominal is available | Recomputes thresholds and records `VOLTAGE_AUTODETECT_MISMATCH` with notification suppressed |

## Voltage notification hysteresis

Voltage warnings are logged immediately. Notifications wait for `notifications.voltage_hysteresis_seconds` unless the event is severe.

```yaml
notifications:
  voltage_hysteresis_seconds: 30
```

If a voltage flap clears inside the dwell time, Eneru records `VOLTAGE_FLAP_SUPPRESSED` and does not send a notification. Severe deviations beyond +/- 15% of nominal bypass the dwell and notify immediately.

### Brownout notification timeline

The default dwell is `notifications.voltage_hysteresis_seconds: 30`. Severe means deviation is greater than 15% from nominal (`VOLTAGE_SEVERE_DEVIATION_PCT = 0.15`).

| Time | Input voltage on 230 V nominal | Eneru action |
|------|--------------------------------|--------------|
| 0s | 205 V, below normal low threshold 207 V | Logs `BROWNOUT_DETECTED` immediately and starts the dwell timer |
| 10s | 215 V, back inside band | Suppresses notification and records `VOLTAGE_FLAP_SUPPRESSED` |
| 0s | 205 V again | Starts a new dwell window |
| 30s | 204 V, still low | Sends brownout notification with persisted-duration note |
| Any time | Deviation exceeds 15% from nominal | Severe event bypasses dwell and notifies immediately |

## Trigger profiles

| Profile | Battery | Runtime | Depletion | Extended time |
|---------|---------|---------|-----------|---------------|
| Conservative | 30% | 900s | 10%/min | 600s |
| Balanced | 20% | 600s | 15%/min | 900s |
| Runtime-focused | 10% | 300s | 20%/min | disabled |

Balanced is the default. Runtime-focused settings are only reasonable with tested batteries and trustworthy UPS telemetry.

## Redundancy groups

When a UPS belongs to a [redundancy group](redundancy-groups.md), its triggers still run on the UPS monitor thread. They do not directly shut down the resource group. Instead they mark the member as advisory-critical, and the redundancy evaluator applies the group's quorum policy.

For a dual-PSU rack with `min_healthy: 1`, one critical UPS is not enough to shut the rack down if the other UPS is still healthy. Both feeds must fail, unless you set a stricter quorum policy.
