"""Tests for shipped example assets."""

import json
import re
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.unit
def test_grafana_dashboard_has_observability_polish_panels():
    dashboard = json.loads((ROOT / "examples/grafana-dashboard.json").read_text())
    titles = [panel["title"] for panel in dashboard["panels"]]
    # Build the lookup only after asserting titles are unique so a
    # duplicate doesn't silently overwrite a real panel and let later
    # assertions pass against the wrong target.
    duplicates = sorted({t for t in titles if titles.count(t) > 1})
    assert not duplicates, f"duplicate panel titles: {duplicates}"
    panels = {panel["title"]: panel for panel in dashboard["panels"]}

    # Gauge row at the top: "now" view for at-a-glance health.
    assert panels["Battery charge (now)"]["type"] == "gauge"
    assert panels["UPS load (now)"]["type"] == "gauge"
    assert panels["Runtime remaining (now)"]["type"] == "stat"

    # Time-series view of the same trio underneath the gauges.
    assert panels["Battery charge"]["type"] == "timeseries"
    assert panels["Runtime remaining"]["type"] == "timeseries"
    assert panels["UPS load"]["type"] == "timeseries"

    # Standalone status panels are intentionally absent — the same
    # information is overlaid on every time-series panel via the
    # dashboard-wide annotations, so a separate panel would duplicate
    # what's already in context. The state-timeline variant misrendered
    # the merged voltage/AVR/bypass/overload series in Grafana 12, so it
    # is not part of the shipped dashboard either.
    assert "Event signals" not in panels
    assert "Remote health failures" not in panels
    assert "Power-quality state timeline" not in panels

    # Voltage panel includes nominal and warning thresholds for context.
    voltage = panels["Input and output voltage"]
    voltage_exprs = {target["expr"] for target in voltage["targets"]}
    assert any("eneru_ups_nominal_voltage" in expr for expr in voltage_exprs)
    assert any("eneru_ups_voltage_warning_low" in expr for expr in voltage_exprs)
    assert any("eneru_ups_voltage_warning_high" in expr for expr in voltage_exprs)


@pytest.mark.unit
def test_grafana_dashboard_overlays_events_via_annotations():
    dashboard = json.loads((ROOT / "examples/grafana-dashboard.json").read_text())
    annotations = {a["name"]: a for a in dashboard["annotations"]["list"]}

    # Every event the user wants to correlate against the time-series
    # panels has a Prometheus-sourced annotation. The exprs use ``> 0``
    # / ``== 1`` so Grafana renders a region for the event window.
    expected = {
        "On battery": "eneru_ups_time_on_battery_seconds",
        "Brownout": 'eneru_ups_voltage_state{state="LOW"',
        "Over-voltage": 'eneru_ups_voltage_state{state="HIGH"',
        "AVR engaged": 'eneru_ups_avr_state{state=~"BOOST|TRIM"',
        "Bypass active": 'eneru_ups_bypass_state{state="ACTIVE"',
        "Overload": 'eneru_ups_overload_state{state="ACTIVE"',
        "Shutdown trigger": "eneru_ups_trigger_active",
        "Connection failed": "eneru_ups_connection_failed",
        "Remote health failed": 'eneru_remote_health_status{status="FAILED"',
    }
    for name, fragment in expected.items():
        assert name in annotations, f"missing annotation: {name}"
        annotation = annotations[name]
        assert annotation["enable"] is True
        # Hidden from the top-of-dashboard annotation toggle bar — they
        # render on the panels but don't crowd the dashboard chrome.
        assert annotation["hide"] is True, (
            f"{name} annotation should set hide:true to skip the toolbar toggle"
        )
        # titleFormat and textFormat are what makes the event name visible
        # in the tooltip when you hover the marker. Without them Grafana
        # falls back to the raw metric value (1) and labels.
        assert annotation["titleFormat"], f"{name} missing titleFormat"
        assert annotation["textFormat"], f"{name} missing textFormat"
        assert fragment in annotation["target"]["expr"], (
            f"{name} expr does not contain {fragment!r}: "
            f"{annotation['target']['expr']}"
        )


@pytest.mark.unit
def test_grafana_dashboard_has_ups_template_variable():
    dashboard = json.loads((ROOT / "examples/grafana-dashboard.json").read_text())
    variables = {var["name"]: var for var in dashboard["templating"]["list"]}

    assert "ups" in variables, "expected $ups multi-select template variable"
    ups_var = variables["ups"]
    assert ups_var["multi"] is True
    assert ups_var["includeAll"] is True


@pytest.mark.unit
def test_architecture_diagram_viewbox_matches_content_width():
    svg = (ROOT / "docs/images/eneru-diagram.svg").read_text()
    match = re.search(r'viewBox="0 0 (?P<width>\d+) (?P<height>\d+)"', svg)

    assert match is not None
    # Rightmost drawn elements end at x=780 (the targets-zone rect at
    # x=540 w=240, and the bottom strip rect at x=20 w=760). 782 leaves
    # 2px of right padding without clipping anything.
    assert match.group("width") == "782"
    assert match.group("height") == "550"
