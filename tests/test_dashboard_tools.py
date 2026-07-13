"""Unit tests for the reusable dashboard browser-audit tooling."""

import importlib.util
from pathlib import Path

import pytest


TOOL = Path(__file__).parents[1] / "tools" / "dashboard-audit.py"


def _load_tool():
    spec = importlib.util.spec_from_file_location("dashboard_audit", TOOL)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.mark.unit
def test_dashboard_audit_parser_defaults_to_safe_read_only_run():
    audit = _load_tool()
    args = audit.build_parser().parse_args([])

    assert args.url == "http://127.0.0.1:9191"
    assert args.themes == "light,dark"
    assert args.tabs is None
    assert args.username is None
    assert args.mobile_width == 390
    assert args.capture_scopes is False


@pytest.mark.unit
def test_dashboard_audit_csv_and_tab_selection():
    audit = _load_tool()

    assert audit.parse_csv(" light, dark ,,light ") == ["light", "dark"]
    assert audit.select_tabs(["overview", "power", "events"], None) == [
        "overview", "power", "events"]
    assert audit.select_tabs(
        ["overview", "power", "events"], ["events", "overview"]) == [
            "events", "overview"]
    with pytest.raises(ValueError, match="not visible"):
        audit.select_tabs(["overview", "power"], ["control"])


@pytest.mark.unit
@pytest.mark.parametrize("raw,expected", [
    ("Fleet overview", "fleet-overview"),
    ("UPS @ rack/01", "ups-rack-01"),
    ("", "item"),
])
def test_dashboard_audit_safe_slug(raw, expected):
    audit = _load_tool()
    assert audit.safe_slug(raw) == expected


@pytest.mark.unit
def test_dashboard_audit_rejects_credentialed_or_non_http_urls():
    audit = _load_tool()

    assert audit.normalize_url("http://eneru.local:9191/") == (
        "http://eneru.local:9191")
    with pytest.raises(ValueError, match="credentials"):
        audit.normalize_url("http://user:secret@eneru.local:9191")
    with pytest.raises(ValueError, match="http"):
        audit.normalize_url("file:///tmp/index.html")


@pytest.mark.unit
def test_dashboard_audit_tab_filter_also_limits_drilldowns():
    """A focused audit must not visit tabs outside the requested subset."""
    source = TOOL.read_text(encoding="utf-8")

    assert 'if "overview" in tabs:' in source
    assert 'if "events" in tabs:' in source
    assert 'if "config" in tabs:' in source
    assert 'args.mobile_width and "overview" in tabs' in source
