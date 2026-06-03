"""Unit tests for the v6.0 dashboard static serving (api.py + eneru.web)."""

from io import BytesIO
from unittest.mock import MagicMock

import pytest

from eneru.api import EneruAPIHandler, SessionManager


def _handler(config, *, path):
    h = object.__new__(EneruAPIHandler)
    h.path = path
    h.api_config = config
    h.api_source = MagicMock()
    h.api_auth = None
    h.api_sessions = None
    h.headers = {}
    h.rfile = BytesIO(b"")
    return h


@pytest.mark.unit
@pytest.mark.parametrize("path,ctype", [
    ("/", "text/html"),
    ("/app.js", "application/javascript"),
    ("/style.css", "text/css"),
])
def test_static_assets_served(minimal_config, path, ctype):
    h = _handler(minimal_config, path=path)
    result = h._serve_static(path)
    assert result is not None
    assert result[0] == ctype
    assert isinstance(result[1], bytes) and len(result[1]) > 0


@pytest.mark.unit
@pytest.mark.parametrize("path", [
    "/../config.py", "/etc/passwd", "/sub/dir.js", "/nope.js",
    "/api/v1/ups", "/..%2f", "/a/b",
])
def test_static_rejects_traversal_and_unknown(minimal_config, path):
    assert _handler(minimal_config, path=path)._serve_static(path) is None


@pytest.mark.unit
def test_route_serves_index(minimal_config):
    h = _handler(minimal_config, path="/")
    status, ctype, body = h._route()
    assert status == 200 and ctype == "text/html"
    assert b"<title>Eneru</title>" in body
    assert b'id="event-source-filter"' in body
    assert b'id="event-type-filter"' in body


@pytest.mark.unit
def test_dashboard_open_even_with_require_for_reads(minimal_config):
    # Static assets are served before the read gate, so the login page renders
    # even when reads require auth.
    minimal_config.api.auth.enabled = True
    minimal_config.api.auth.require_for_reads = True
    h = _handler(minimal_config, path="/")
    h.api_sessions = SessionManager(3600)
    assert h._route()[0] == 200


@pytest.mark.unit
def test_do_get_sets_csp_for_html(minimal_config):
    h = _handler(minimal_config, path="/")
    headers = []
    h.send_response = lambda s: headers.append(("status", s))
    h.send_header = lambda k, v: headers.append((k, v))
    h.end_headers = lambda: None
    h.wfile = BytesIO()
    h.do_GET()
    assert ("status", 200) in headers
    assert ("Content-Type", "text/html; charset=utf-8") in headers
    assert any(k == "Content-Security-Policy" for k, _ in headers)
    assert ("X-Content-Type-Options", "nosniff") in headers
    assert b"<title>Eneru</title>" in h.wfile.getvalue()


@pytest.mark.unit
def test_finish_writes_bytes_body_unchanged(minimal_config):
    h = _handler(minimal_config, path="/style.css")
    h.send_response = lambda s: None
    h.send_header = lambda k, v: None
    h.end_headers = lambda: None
    h.wfile = BytesIO()
    h._finish(200, "text/css", b"body{}")
    assert h.wfile.getvalue() == b"body{}"


@pytest.mark.unit
def test_config_summary_exposes_nut_control(minimal_config):
    from eneru.status import config_summary
    anon = config_summary(minimal_config)
    assert anon["nutControl"] == {"enabled": False}
    ext = config_summary(minimal_config, extended=True)
    assert "allowedCommands" in ext["nutControl"]
    assert "allowedVariables" in ext["nutControl"]


@pytest.mark.unit
def test_dashboard_js_contains_plan_control_surfaces(minimal_config):
    body = _handler(minimal_config, path="/app.js")._serve_static("/app.js")[1]
    text = body.decode("utf-8")
    assert "applyEventFilters" in text
    assert "event-source-filter" in text
    assert "event-type-filter" in text
    assert "renderVariableForms" in text
    assert "setVariable(" in text


@pytest.mark.unit
def test_dashboard_js_graph_is_resize_safe(minimal_config):
    # Guard the graph-scaling fix (no browser in CI): the renderer must size the
    # viewBox to the host width, use non-scaling strokes, and register a single
    # resize observer rather than stretching a fixed viewBox.
    text = _handler(minimal_config, path="/app.js")._serve_static(
        "/app.js")[1].decode("utf-8")
    assert "observeGraphResize" in text
    assert "non-scaling-stroke" in text
    assert "host.clientWidth" in text


@pytest.mark.unit
def test_dashboard_has_wide_history_surfaces(minimal_config):
    # Guard the Slice B surfaces: graph + event range selectors and the
    # "load older" paging that drives wide-history viewing.
    html = _handler(minimal_config, path="/")._serve_static("/")[1].decode("utf-8")
    assert 'id="graph-range"' in html
    assert 'id="event-range"' in html
    assert 'id="event-load-older"' in html
    js = _handler(minimal_config, path="/app.js")._serve_static(
        "/app.js")[1].decode("utf-8")
    assert "loadOlderEvents" in js
    assert "mergeEvents" in js
    assert "eventKey" in js
    assert "as < bs ? -1 : as > bs ? 1 : 0" in js
    assert "localeCompare" not in js
    assert "event.ups === source" in js
    # Cursor paging must keep the selected lower bound, and rendered rows should
    # still honor the range if older rows are already cached client-side.
    assert 'if (from !== null) q += "&from=" + from' in js
    assert "return (from === null || e.ts >= from)" in js


@pytest.mark.unit
def test_dashboard_has_event_delete_surface(minimal_config):
    # Guard the Slice C delete-selected surface (auth-gated in JS, server-enforced).
    html = _handler(minimal_config, path="/")._serve_static("/")[1].decode("utf-8")
    assert 'id="event-delete"' in html
    js = _handler(minimal_config, path="/app.js")._serve_static(
        "/app.js")[1].decode("utf-8")
    assert "deleteSelected" in js
    assert 'method: "DELETE"' in js
    # Selections must survive passive polling: loadEvents clears only on an
    # explicit clearSelection (range change), never on the unconditional refresh
    # path. The Delete button surfaces the live, actionable selection count.
    assert "clearSelection" in js
    assert "loadEvents(undefined, true)" in js
    assert "Delete selected (" in js
    assert "deleted === items.length" in js
    assert "JSON.stringify({ items })" in js


@pytest.mark.unit
def test_dashboard_events_time_header_is_sortable(minimal_config):
    """The Events Time header should toggle chronological order."""
    html = _handler(minimal_config, path="/")._serve_static("/")[1].decode("utf-8")
    js = _handler(minimal_config, path="/app.js")._serve_static(
        "/app.js")[1].decode("utf-8")

    assert 'id="event-sort-time"' in html
    assert "eventSortDirection" in js
    assert "toggleEventSort" in js


@pytest.mark.unit
def test_dashboard_remote_health_accepts_uppercase_statuses(minimal_config):
    """API remote-health status constants are uppercase, e.g. HEALTHY."""
    js = _handler(minimal_config, path="/app.js")._serve_static(
        "/app.js")[1].decode("utf-8")

    assert "remoteHealthReachable" in js
    assert 'status === "HEALTHY"' in js


@pytest.mark.unit
def test_dashboard_has_drilldown_and_theme_surfaces(minimal_config):
    # Guard Slice D (drill-down) + Slice E (theme, banner) surfaces.
    html = _handler(minimal_config, path="/")._serve_static("/")[1].decode("utf-8")
    assert 'id="detail-modal"' in html
    assert 'role="dialog"' in html
    assert 'id="theme-select"' in html
    assert 'id="banner"' in html
    js = _handler(minimal_config, path="/app.js")._serve_static(
        "/app.js")[1].decode("utf-8")
    assert "openDetail" in js and "renderDetail" in js
    assert "renderBanner" in js
    assert "groupQuorumLost" in js
    assert "quorumLost" in js
    assert "/api/v1/auth/state" in js
    assert "const [authState, cfg, rh]" in js
    assert "authState.ok" in js
    assert ".style.width" not in js
    assert "applyTheme" in js
    # Drill-down must read the shared snapshot, not fetch per card.
    assert "remoteHealthSnapshot" in js
    # Deleted-user / expired session: the client signs out when it holds a token
    # but /config returns the sanitized (anonymous) view.
    assert 'detail === "sanitized"' in js
    assert "clearAuthState" in js
    assert 'path !== "/api/v1/auth/login"' in js
    assert "selectedEvents = new Set()" in js
    assert 'document.getElementById("control-section")' in js


@pytest.mark.unit
def test_dashboard_theme_palette_supports_light_dark_system(minimal_config):
    css = _handler(minimal_config, path="/style.css")._serve_static(
        "/style.css")[1].decode("utf-8")
    assert 'data-theme="light"' in css
    assert 'data-theme="dark"' in css
    assert "prefers-color-scheme: light" in css


@pytest.mark.unit
def test_stylesheet_makes_hidden_attribute_win(minimal_config):
    # The dashboard shows/hides everything via the `hidden` attribute. A class
    # that sets `display` (e.g. `.modal { display: flex }`) would otherwise win
    # over the UA `[hidden]{display:none}` and pin the login modal in the
    # foreground forever. Guard the reset that fixes this (no browser in CI).
    css = _handler(minimal_config, path="/style.css")._serve_static(
        "/style.css")[1].decode("utf-8")
    norm = css.replace(" ", "").replace("\n", "").lower()
    assert "[hidden]{display:none!important;}" in norm
