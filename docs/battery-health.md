# Battery health

Eneru computes a composite **0–100 battery-health score** for each UPS and, once
it has enough history, projects when the battery will need replacing. The score
is derived entirely from data Eneru already collects (the per-UPS stats store) —
there is nothing new to install.

## The score

The score is a weighted blend of up to five terms. Each term carries an
**availability flag**, and a missing term is *not* silently scored as full marks:

| Term | Source | Unavailable when |
|------|--------|------------------|
| Capacity degradation | runtime-under-load trend (and self-test) | too little history |
| Runtime vs expected | runtime-under-load vs `nominal_runtime_seconds` | nominal runtime not yet learned |
| Last self-test | latest normalized self-test result | never self-tested |
| Confirmed anomalies | `_check_battery_anomaly` counters | (always available) |
| Battery age | `battery_install_date` + `expected_life_years` | install date unset |

> **"Unknown" is not "healthy."** If the available terms don't clear a minimum
> confidence threshold, Eneru reports the score as **unknown** (with an explicit
> `confidence` and the list of `availableTerms`) rather than inventing a
> confident high mark from thin telemetry. Fill in `battery_install_date` and let
> the nominal runtime autodetect to unlock the remaining terms.

`nominal_runtime_seconds` autodetects from the first reading at 100 % charge if
left `null`; set it explicitly if you know your battery's rated runtime.

## Configuration

```yaml
battery_health:
  enabled: true
  update_interval: 3600          # seconds between recomputations
  nominal_runtime_seconds: null  # null = autodetect at first 100% charge
  battery_install_date: null     # "YYYY-MM-DD" — unlocks the age term
  expected_life_years: 5.0
  replacement:
    threshold_score: 50.0        # warn when the projected score will cross this
    horizon_days: 90             # ...within this many days
    min_history_days: 14         # don't trend on less history than this
```

Battery characteristics are **per-UPS**, so `battery_install_date`,
`nominal_runtime_seconds`, and `expected_life_years` can be overridden per UPS in
a multi-UPS config:

```yaml
battery_health:
  expected_life_years: 5.0       # global default
ups:
  - name: "UPS-A@10.0.0.10"
    battery_health:
      battery_install_date: "2023-01-15"   # this battery is older
  - name: "UPS-B@10.0.0.11"               # inherits the global default
```

`battery_health` is **hot-reloadable** (it is read live on each computation — no
restart needed; see [Configuration reference](configuration.md#hot-reload)).

## Replacement prediction

When at least `min_history_days` of score history exists, Eneru fits a
least-squares trend to the score series and projects the date it will cross
`threshold_score`. If that date is within `horizon_days`, it:

- sends a `BATTERY_REPLACEMENT_PREDICTED` notification (category `health`), and
- logs the matching event,

de-duplicated via the stats `meta` table so you are warned at most once per
prediction window — a steadily declining battery does not spam you every hour.
A flat or improving trend never fires.

## Where it shows up

- **Dashboard** → the **Battery** tab shows the score, confidence, per-term
  availability, the replacement projection, and the latest self-test.
- **API** → `GET /api/v1/ups/{name}/battery-health`.
- **Status** → the `batteryHealth` block in `GET /api/v1/ups`.
- **Prometheus** → `eneru_ups_battery_health_score` and
  `eneru_ups_replacement_days_remaining` (both **omitted** while the value is
  unknown, rather than emitting a misleading `0`).

See also: [Self-test](self-test.md) (a self-test result feeds the score) and
[Statistics](statistics.md) (the underlying history store).
