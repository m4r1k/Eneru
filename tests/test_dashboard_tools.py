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
    assert args.settle_ms == 1200
    assert args.tab_settle_ms == 650
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
def test_dashboard_audit_scope_screenshot_names_are_unique():
    audit = _load_tool()

    assert audit.scope_screenshot_name(0, "Rack UPS") == (
        "scope-01-rack-ups.png")
    assert audit.scope_screenshot_name(1, "Rack UPS") == (
        "scope-02-rack-ups.png")
    assert audit.scope_screenshot_name(2, "Rack/UPS") == (
        "scope-03-rack-ups.png")


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
    audit = _load_tool()

    assert audit.build_drilldown_plan(
        ["events"], capture_scopes=True, mobile_width=390) == {
            "details": False,
            "events": True,
            "config": False,
            "scopes": False,
            "mobile": False,
        }
    assert audit.build_drilldown_plan(
        ["overview", "config"], capture_scopes=True, mobile_width=390) == {
            "details": True,
            "events": False,
            "config": True,
            "scopes": True,
            "mobile": True,
        }
    assert audit.build_drilldown_plan(
        ["overview"], capture_scopes=False, mobile_width=0)["mobile"] is False


@pytest.mark.unit
def test_dashboard_audit_ignores_only_expected_pre_login_http_errors():
    audit = _load_tool()
    report = {"httpErrors": []}
    recorder = audit.HttpErrorRecorder(report, wait_for_login=True)

    response = type(
        "Response", (),
        {"status": 401, "url": "https://host/api/v1/ups"})()
    recorder(response)
    assert report["httpErrors"] == []

    recorder.enable()
    recorder(response)
    assert report["httpErrors"] == [{"status": 401, "path": "/api/v1/ups"}]


@pytest.mark.unit
def test_dashboard_audit_mobile_inventory_handles_missing_tab_strip():
    audit = _load_tool()

    class MissingLocator:
        def count(self):
            return 0

        def evaluate(self, _script):
            raise AssertionError("evaluate must not run without a matching node")

    class Page:
        def locator(self, selector):
            assert selector == ".tabs"
            return MissingLocator()

    assert audit.mobile_layout_inventory(Page()) == {"tabsPresent": False}
