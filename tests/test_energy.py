"""Unit tests for energy integration + cost/currency formatting (energy.py)."""

import pytest

from eneru.energy import (
    EnergyResult,
    _median,
    compute_cost,
    format_cost,
    integrate_kwh,
    power_sample_w,
    summarize,
)


class TestNominalFallback:
    @pytest.mark.unit
    def test_fallback_estimates_when_no_realpower_or_nominal(self):
        # samples report neither realpower nor power.nominal -> needs the config
        # fallback to turn load% into watts.
        samples = [(0, None, 50.0, None), (3600, None, 50.0, None)]
        r = integrate_kwh(samples, nominal_fallback=1000.0)
        assert r.kwh is not None and r.estimated is True
        assert abs(r.kwh - 0.5) < 1e-6          # 50% of 1000W over 1h
        # Without the fallback the window is unknown (not zero).
        assert integrate_kwh(samples).kwh is None

    @pytest.mark.unit
    def test_summarize_threads_fallback(self):
        s = [(0, None, 25.0, None), (3600, None, 25.0, None)]
        block = summarize(s, s, cost_per_kwh=None, nominal_fallback=2000.0)
        assert block["todayKwh"] is not None and block["estimated"] is True


class TestMedian:
    @pytest.mark.unit
    def test_odd_length(self):
        assert _median([3.0, 1.0, 2.0]) == 2.0

    @pytest.mark.unit
    def test_even_length_averages_middle(self):
        # The bug: [1, 3600] used to return 3600 (upper-middle), inflating the
        # inferred sample spacing and loosening integrate_kwh's gap cap.
        assert _median([1.0, 3600.0]) == 1800.5

    @pytest.mark.unit
    def test_empty_is_none(self):
        assert _median([]) is None


# --------------------------------------------------------------------------
# power_sample_w
# --------------------------------------------------------------------------

class TestPowerSample:
    @pytest.mark.unit
    def test_real_power_preferred(self):
        assert power_sample_w(120.0, 50.0, 1000.0) == (120.0, False)

    @pytest.mark.unit
    def test_fallback_load_times_nominal(self):
        # 40% of 1000 VA -> 400 W, flagged as fallback
        assert power_sample_w(None, 40.0, 1000.0) == (400.0, True)

    @pytest.mark.unit
    def test_unknown_when_neither(self):
        assert power_sample_w(None, None, None) == (None, False)
        assert power_sample_w(None, 40.0, None) == (None, False)
        assert power_sample_w(None, 40.0, 0) == (None, False)  # nominal 0 unusable

    @pytest.mark.unit
    def test_negative_real_power_falls_back(self):
        assert power_sample_w(-5.0, 50.0, 1000.0) == (500.0, True)


# --------------------------------------------------------------------------
# integrate_kwh
# --------------------------------------------------------------------------

class TestIntegrate:
    @pytest.mark.unit
    def test_real_power_path(self):
        # 3600 W held for two 1s intervals = 2 Wh = 0.002 kWh
        samples = [(0, 3600.0, None, None), (1, 3600.0, None, None),
                   (2, 3600.0, None, None)]
        r = integrate_kwh(samples, expected_interval_s=1)
        assert r.kwh == pytest.approx(0.002)
        assert r.estimated is False and r.partial is False

    @pytest.mark.unit
    def test_fallback_path_marks_estimated(self):
        # load 50% of 1000 VA = 500 W for one 1s interval = 500 Wh? no:
        # 500 W * (1/3600) h / 1000 = 0.0001389 kWh
        samples = [(0, None, 50.0, 1000.0), (1, None, 50.0, 1000.0)]
        r = integrate_kwh(samples, expected_interval_s=1)
        assert r.kwh == pytest.approx(500 * (1 / 3600) / 1000)
        assert r.estimated is True
        assert r.partial is False

    @pytest.mark.unit
    def test_missing_power_is_unknown_not_zero(self):
        samples = [(0, None, None, None), (1, None, None, None)]
        r = integrate_kwh(samples, expected_interval_s=1)
        assert r.kwh is None        # unknown, NOT 0.0
        assert r.partial is True

    @pytest.mark.unit
    def test_long_gap_capped_and_marked_partial(self):
        # 1h gap with a 1s expected interval -> skipped (daemon was down)
        samples = [(0, 3600.0, None, None), (3600, 3600.0, None, None)]
        r = integrate_kwh(samples, expected_interval_s=1, gap_factor=2.0)
        assert r.kwh is None        # the only interval was skipped
        assert r.partial is True

    @pytest.mark.unit
    def test_single_sample_yields_unknown_not_partial(self):
        r = integrate_kwh([(0, 3600.0, None, None)], expected_interval_s=1)
        assert r.kwh is None
        assert r.partial is False   # no interval existed to skip

    @pytest.mark.unit
    def test_empty_series(self):
        r = integrate_kwh([], expected_interval_s=1)
        assert r == EnergyResult(kwh=None, estimated=False, partial=False)

    @pytest.mark.unit
    def test_nonpositive_expected_interval_defaults_to_one(self):
        # expected_interval_s <= 0 must not divide-by-zero or skip everything
        samples = [(0, 3600.0, None, None), (1, 3600.0, None, None)]
        r = integrate_kwh(samples, expected_interval_s=0)
        assert r.kwh == pytest.approx(0.001)

    @pytest.mark.unit
    def test_out_of_order_timestamps_ignored(self):
        samples = [(10, 3600.0, None, None), (5, 3600.0, None, None)]  # dt < 0
        r = integrate_kwh(samples, expected_interval_s=1)
        assert r.kwh is None

    @pytest.mark.unit
    def test_tier_boundary_respected_via_expected_interval(self):
        # 5-min tier: samples 300s apart integrate cleanly at expected=300
        samples = [(0, 1000.0, None, None), (300, 1000.0, None, None)]
        r = integrate_kwh(samples, expected_interval_s=300)
        assert r.kwh == pytest.approx(1000 * (300 / 3600) / 1000)
        assert r.partial is False

    @pytest.mark.unit
    def test_mixed_known_and_gap_is_partial_but_counts_known(self):
        # interval 0->1 good; 1->5000 is a gap (skipped)
        samples = [(0, 3600.0, None, None), (1, 3600.0, None, None),
                   (5000, 3600.0, None, None)]
        r = integrate_kwh(samples, expected_interval_s=1, gap_factor=2.0)
        assert r.kwh == pytest.approx(0.001)  # only the 1s interval counted
        assert r.partial is True


# --------------------------------------------------------------------------
# cost + currency
# --------------------------------------------------------------------------

class TestCost:
    @pytest.mark.unit
    def test_compute_cost(self):
        assert compute_cost(2.0, 0.25) == 0.5

    @pytest.mark.unit
    def test_compute_cost_disabled_or_unknown(self):
        assert compute_cost(2.0, None) is None     # cost tracking off
        assert compute_cost(None, 0.25) is None     # kWh unknown

    @pytest.mark.unit
    @pytest.mark.parametrize("currency,expected", [
        ("USD", "$0.20"),
        ("GBP", "£0.20"),
        ("EUR", "0.20 €"),
        ("xyz", "0.20 XYZ"),     # unknown code -> amount + uppercased code
    ])
    def test_format_cost_by_currency(self, currency, expected):
        assert format_cost(0.2, currency) == expected

    @pytest.mark.unit
    def test_format_cost_numeric_spec_template(self):
        # A numeric format spec applies to the NUMERIC value (the v6.1 fix —
        # previously {value} got a pre-stringified amount and a numeric spec
        # raised, silently falling back to the symbol table).
        assert format_cost(0.2, "EUR", "{value:.2f} EUR") == "0.20 EUR"
        assert format_cost(0.2, "USD", "{value:.3f} USD") == "0.200 USD"

    @pytest.mark.unit
    def test_format_cost_plain_text_template(self):
        # A plain {value} template still renders (unrounded numeric repr).
        assert format_cost(0.2, "EUR", "{value} EUR") == "0.2 EUR"
        assert format_cost(0.25, "GBP", "GBP {value}") == "GBP 0.25"

    @pytest.mark.unit
    def test_format_cost_malformed_override_falls_back(self):
        # bad placeholder -> fall through to the currency table
        assert format_cost(0.2, "USD", "{nope}") == "$0.20"

    @pytest.mark.unit
    def test_format_cost_bad_format_spec_falls_back(self):
        # A template whose format spec is invalid for a number raises ValueError
        # internally -> safe fall-through to the currency table, never an
        # exception out of format_cost.
        assert format_cost(0.2, "EUR", "{value:Z}") == "0.20 €"


# --------------------------------------------------------------------------
# summarize (status block shape)
# --------------------------------------------------------------------------

class TestSummarize:
    @pytest.mark.unit
    def test_cost_fields_omitted_when_disabled(self):
        today = [(0, 1000.0, None, None), (1, 1000.0, None, None)]
        block = summarize(today, today, cost_per_kwh=None, currency="EUR",
                          expected_interval_s=1)
        assert "todayKwh" in block and "monthKwh" in block
        assert block["currency"] == "EUR"
        assert "todayCost" not in block and "monthCost" not in block
        assert "costFormatted" not in block

    @pytest.mark.unit
    def test_cost_fields_present_when_enabled(self):
        # 3600 W for 1s = 0.001 kWh; cost 0.001 * 0.30 = 0.0003
        s = [(0, 3600.0, None, None), (1, 3600.0, None, None)]
        block = summarize(s, s, cost_per_kwh=0.30, currency="USD",
                          expected_interval_s=1)
        assert block["todayKwh"] == pytest.approx(0.001)
        assert block["todayCost"] == pytest.approx(0.0003)
        assert block["todayCostFormatted"].startswith("$")
        assert block["monthCost"] == pytest.approx(0.0003)

    @pytest.mark.unit
    def test_estimated_and_partial_or_across_windows(self):
        today = [(0, None, 50.0, 1000.0), (1, None, 50.0, 1000.0)]  # fallback
        month = [(0, 3600.0, None, None), (5000, 3600.0, None, None)]  # gap
        block = summarize(today, month, cost_per_kwh=None, expected_interval_s=1)
        assert block["estimated"] is True   # today used fallback
        assert block["partial"] is True     # month had a gap

    @pytest.mark.unit
    def test_unknown_cost_formatted_is_none(self):
        # month unknown (gap) -> monthCost None, monthCostFormatted None
        today = [(0, 3600.0, None, None), (1, 3600.0, None, None)]
        month = [(0, 3600.0, None, None), (9000, 3600.0, None, None)]
        block = summarize(today, month, cost_per_kwh=0.20, expected_interval_s=1)
        assert block["monthKwh"] is None
        assert block["monthCost"] is None
        assert block["monthCostFormatted"] is None
        assert block["todayCostFormatted"] is not None

    @pytest.mark.unit
    def test_year_window_optional_and_backward_compatible(self):
        s = [(0, 3600.0, None, None), (1, 3600.0, None, None)]
        # Omitting year_samples keeps the old shape (no year keys).
        block = summarize(s, s, cost_per_kwh=0.20, expected_interval_s=1)
        assert "yearKwh" not in block and "yearCost" not in block
        # Providing it adds yearKwh + (cost configured) yearCost/formatted.
        block = summarize(s, s, year_samples=s, cost_per_kwh=0.20,
                          currency="EUR", expected_interval_s=1)
        assert block["yearKwh"] is not None
        assert block["yearCost"] is not None
        assert block["yearCostFormatted"] is not None

    @pytest.mark.unit
    def test_year_kwh_without_cost(self):
        s = [(0, 3600.0, None, None), (1, 3600.0, None, None)]
        block = summarize(s, s, year_samples=s, cost_per_kwh=None,
                          expected_interval_s=1)
        assert block["yearKwh"] is not None
        assert "yearCost" not in block  # cost tracking off
