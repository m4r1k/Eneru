"""Tests for shipped example assets."""

import json
import re
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.unit
def test_grafana_dashboard_has_observability_polish_panels():
    dashboard = json.loads((ROOT / "examples/grafana-dashboard.json").read_text())
    panels = {panel["title"]: panel for panel in dashboard["panels"]}

    assert panels["Power-quality state timeline"]["type"] == "state-timeline"
    event_signals = panels["Event signals"]
    expressions = {target["expr"] for target in event_signals["targets"]}
    assert "eneru_ups_trigger_active" in expressions
    assert "eneru_ups_connection_failed" in expressions
    assert 'eneru_remote_health_status{status!="HEALTHY"}' in expressions


@pytest.mark.unit
def test_architecture_diagram_viewbox_matches_content_width():
    svg = (ROOT / "docs/images/eneru-diagram.svg").read_text()
    match = re.search(r'viewBox="0 0 (?P<width>\d+) (?P<height>\d+)"', svg)

    assert match is not None
    assert match.group("width") == "800"
    assert match.group("height") == "550"
