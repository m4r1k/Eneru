"""Energy (kWh) integration + cost/currency formatting (v6.1, pure functions).

Given a power series (from ``StatsStore.power_samples``) this integrates kWh
and, when a price is configured, computes cost. Everything here is pure and
unit-tested; ``status.py`` does the fetching and hands us the samples.

Integration contract (deliberately explicit):

- One tier at a time. The caller fetches a single retention tier (raw /
  agg_5min / agg_hourly), so we never integrate across a tier boundary where
  the sample spacing jumps.
- Power for an interval = the power at its START sample, held for ``dt``
  (left-Riemann; periodic power readings are "the draw until the next poll").
  Power is ``real_power`` when the UPS reports it, else the explicit fallback
  ``ups.load% / 100 * power.nominal``, else the interval is **unknown**.
- Energy(interval) = ``power_W * dt_h / 1000``. ``dt`` is capped: a gap larger
  than ``gap_factor`` x the expected sample interval means the daemon was down
  / data is missing, so that interval is skipped and the window marked partial
  rather than overcounting one giant rectangle.
- If NO interval has usable power, kWh is ``None`` ("unknown"), never ``0.0`` --
  zero energy and "we don't know" are different answers.
- Cost is gated on ``cost_per_kwh``: unset (``None``) disables cost tracking
  entirely (no cost fields), rather than a meaningless zero-currency number.
"""

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

__all__ = [
    "EnergyResult",
    "power_sample_w",
    "integrate_kwh",
    "compute_cost",
    "format_cost",
    "summarize",
]

# (ts, real_power_W, ups_load_pct, power_nominal_VA) — what power_samples returns.
PowerSample = Tuple[int, Optional[float], Optional[float], Optional[float]]

# Currency symbol + placement. "before" => "$0.20"; "after" => "0.20 €".
# Unknown codes fall back to "<amount> <CODE>".
_CURRENCY: Dict[str, Tuple[str, str]] = {
    "USD": ("$", "before"),
    "GBP": ("£", "before"),
    "CAD": ("$", "before"),
    "AUD": ("$", "before"),
    "JPY": ("¥", "before"),
    "EUR": ("€", "after"),
    "CHF": ("CHF", "after"),
    "SEK": ("kr", "after"),
    "NOK": ("kr", "after"),
    "DKK": ("kr", "after"),
    "PLN": ("zł", "after"),
}


@dataclass
class EnergyResult:
    """Integration result for one window/tier."""
    kwh: Optional[float]   # None => unknown (no usable power in any interval)
    estimated: bool        # any used interval came from the load*nominal fallback
    partial: bool          # any interval skipped (data gap / unknown power)


def power_sample_w(real_power: Optional[float],
                   ups_load: Optional[float],
                   power_nominal: Optional[float]) -> Tuple[Optional[float], bool]:
    """Power (W) for one sample and whether it used the fallback.

    Returns ``(power_w, was_fallback)``. ``power_w`` is ``None`` when neither
    real power nor a usable load+nominal pair is available.
    """
    if real_power is not None and real_power >= 0:
        return float(real_power), False
    if (ups_load is not None and power_nominal is not None
            and power_nominal > 0 and ups_load >= 0):
        return max(0.0, float(ups_load) / 100.0 * float(power_nominal)), True
    return None, False


def _median(values: List[float]) -> Optional[float]:
    s = sorted(values)
    if not s:
        return None
    return s[len(s) // 2]


def integrate_kwh(samples: List[PowerSample], *,
                  expected_interval_s: Optional[float] = None,
                  gap_factor: float = 2.0) -> EnergyResult:
    """Integrate a single-tier power series into kWh (see module contract).

    ``expected_interval_s`` sets the gap cap (a dt above ``gap_factor`` x it is
    treated as missing data). When ``None`` (the usual case) it is INFERRED
    from the data as the median consecutive dt -- so the same code handles the
    raw tier (~1s) and the aggregate tiers (300s / 3600s) without the caller
    having to know which tier it fetched.
    """
    if expected_interval_s is None or expected_interval_s <= 0:
        dts = [nxt[0] - cur[0] for cur, nxt in zip(samples, samples[1:])
               if nxt[0] - cur[0] > 0]
        expected_interval_s = _median(dts) or 1.0
    cap = gap_factor * expected_interval_s
    total = 0.0
    used_any = False
    estimated = False
    partial = False

    for (ts0, rp0, load0, nom0), nxt in zip(samples, samples[1:]):
        dt = nxt[0] - ts0
        if dt <= 0:
            continue  # out-of-order / duplicate timestamp; ignore
        if dt > cap:
            partial = True
            continue
        power, was_fallback = power_sample_w(rp0, load0, nom0)
        if power is None:
            partial = True
            continue
        total += power * (dt / 3600.0) / 1000.0
        used_any = True
        estimated = estimated or was_fallback

    if not used_any:
        return EnergyResult(kwh=None, estimated=False, partial=partial)
    return EnergyResult(kwh=total, estimated=estimated, partial=partial)


def compute_cost(kwh: Optional[float],
                 cost_per_kwh: Optional[float]) -> Optional[float]:
    """Cost for a kWh figure. ``None`` when cost tracking is off or kWh unknown."""
    if cost_per_kwh is None or kwh is None:
        return None
    return kwh * cost_per_kwh


def format_cost(value: float, currency: str,
                cost_format: Optional[str] = None) -> str:
    """Format a cost value. ``cost_format`` (e.g. ``"{value} €"``) wins; else a
    per-currency symbol/placement table; else ``"<amount> <CODE>"``."""
    amount = f"{value:.2f}"
    if cost_format:
        try:
            return cost_format.format(value=amount)
        except (KeyError, IndexError, ValueError):
            pass  # malformed override -> fall through to the table
    code = (currency or "USD").upper()
    entry = _CURRENCY.get(code)
    if entry is None:
        return f"{amount} {code}"
    symbol, placement = entry
    if placement == "before":
        return f"{symbol}{amount}"
    return f"{amount} {symbol}"


def summarize(today_samples: List[PowerSample],
              month_samples: List[PowerSample], *,
              cost_per_kwh: Optional[float],
              currency: str = "USD",
              cost_format: Optional[str] = None,
              expected_interval_s: Optional[float] = None) -> Dict:
    """Build the live ``energy`` status block from two windows' samples.

    Shape: ``{todayKwh, monthKwh, currency, estimated, partial}`` plus, only
    when cost tracking is enabled (``cost_per_kwh`` is set),
    ``todayCost/todayCostFormatted/monthCost/monthCostFormatted``. kWh values
    are ``None`` when unknown. ``estimated``/``partial`` are the OR across both
    windows.
    """
    today = integrate_kwh(today_samples, expected_interval_s=expected_interval_s)
    month = integrate_kwh(month_samples, expected_interval_s=expected_interval_s)

    block: Dict = {
        "todayKwh": today.kwh,
        "monthKwh": month.kwh,
        "currency": (currency or "USD").upper(),
        "estimated": today.estimated or month.estimated,
        "partial": today.partial or month.partial,
    }

    if cost_per_kwh is not None:
        today_cost = compute_cost(today.kwh, cost_per_kwh)
        month_cost = compute_cost(month.kwh, cost_per_kwh)
        block["todayCost"] = today_cost
        block["monthCost"] = month_cost
        block["todayCostFormatted"] = (
            format_cost(today_cost, block["currency"], cost_format)
            if today_cost is not None else None)
        block["monthCostFormatted"] = (
            format_cost(month_cost, block["currency"], cost_format)
            if month_cost is not None else None)

    return block
