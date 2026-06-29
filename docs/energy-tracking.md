# Energy tracking

Eneru integrates the power your UPS reports into **kWh** for the current day and
month, and — if you give it a tariff — the matching **cost**. It is computed on
read from the per-UPS stats store, so there is no extra cumulative counter to
maintain.

## How power is measured

For each sample, Eneru picks the power in watts in this order:

1. `ups.realpower` (`real_power`), when the UPS reports it; otherwise
2. the fallback **`ups.load / 100 × ups.power.nominal`**, flagged `estimated`;
   otherwise
3. that interval counts as **unknown** (never silently `0`).

Energy over an interval is `power_W × dt_h / 1000`, summed across consecutive
samples *within a single retention tier* (raw, 5-minute, or hourly — never mixing
tiers). If the gap between two samples is much larger than the tier's expected
spacing (daemon down, data outage), that interval is **skipped** rather than
overcounted, and the window is flagged `partial`. If no interval in the window
has usable power, the result is **unknown**, not `0`.

## Configuration

```yaml
energy:
  enabled: true
  cost_per_kwh: null            # null/unset => cost tracking is OFF entirely
  currency: USD                 # ISO 4217 code (USD, EUR, GBP, ...)
  cost_format: null             # optional override, e.g. "{value} €"
  nominal_power: null           # rated W/VA; estimates watts when the UPS reports
                                # neither ups.realpower nor ups.power.nominal
```

### When the UPS reports no power

Some integrated UPSes expose neither `ups.realpower` nor `ups.power.nominal`, so
Eneru has nothing to turn `load%` into watts and energy stays *unknown*. Set
`energy.nominal_power` to the unit's rated power (e.g. `1000` for a 1000 VA
tower) and Eneru estimates `watts = load% × nominal_power` (flagged
`estimated`). The Energy tab's **Power (W)** line then plots, and kWh/cost
populate.

### Windows

`today` is the **calendar day** (since local midnight) and `month` is the
**calendar month** (since the 1st) — fixed boundaries that match how an
electricity bill is measured, not a rolling 24 h / 30 d.

**Cost is gated on `cost_per_kwh`.** While it is unset (`null`), cost tracking is
disabled *entirely* — no `cost` field in the status payload, no
`eneru_ups_energy_cost` metric, and no cost widget in the UI — rather than
rendering a meaningless zero-currency graph. Set a price to turn it on; then
`cost = kWh × cost_per_kwh` (an unknown kWh yields an unknown cost).

### Currency formatting

A small built-in table formats the amount per currency code — `USD → $0.20`,
`EUR → 0.20 €`, `GBP → £0.20` — and an unknown code falls back to amount + code
(`0.20 XYZ`). Set `cost_format` (e.g. `"{value} €"`) to override placement
entirely. The server returns both the numeric value and a preformatted string so
the dashboard and reports render it identically.

`energy` is **hot-reloadable** — the values are read live on each computation, so
changing the tariff or currency takes effect without a restart.

## Where it shows up

- **Dashboard** → the **Energy** tab shows today/month kWh, cost (when enabled),
  and the `estimated` / `partial` flags.
- **API** → `GET /api/v1/ups/{name}/energy`, plus the `energy` block in
  `GET /api/v1/ups`.
- **Prometheus** → `eneru_ups_energy_kwh{period="today|month"}` (a gauge, since
  it is recomputed per window and is not monotonic) and
  `eneru_ups_energy_cost{period=...}` (**omitted entirely** when `cost_per_kwh`
  is unset). Unknown values are omitted rather than emitted as `0`.

## A note on hardware

Not every UPS reports `ups.realpower`. When it doesn't, Eneru uses the
`load × nominal` estimate and labels the figures `estimated` — useful for
trend-watching, but treat the absolute number as approximate. Reports
([Reports](reports.md)) include the same energy summary.
