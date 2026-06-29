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
def test_dashboard_js_contains_v61_surfaces(minimal_config):
    # The v6.1 battery-health / energy / self-test wiring must stay present in
    # the served dashboard so the new sensors remain visible + actionable.
    body = _handler(minimal_config, path="/app.js")._serve_static("/app.js")[1]
    text = body.decode("utf-8")
    assert "batteryHealth" in text
    assert "u.energy" in text
    assert "runSelfTest(" in text
    assert "/self-test" in text
    # The self-test button must debounce: a non-idempotent hardware POST can't be
    # double-clicked into multiple tests.
    assert "if (btn) btn.disabled = true" in text


@pytest.mark.unit
def test_dashboard_js_graph_is_resize_safe(minimal_config):
    # Guard the graph-scaling fix (no browser in CI): the renderer must size the
    # viewBox to the host width, use non-scaling strokes, and register a resize
    # observer per chart rather than stretching a fixed viewBox. v6.1: the single
    # global graph became the reusable makeChart factory (one instance per tab).
    text = _handler(minimal_config, path="/app.js")._serve_static(
        "/app.js")[1].decode("utf-8")
    assert "function makeChart" in text
    assert "function drawChart" in text
    assert "new ResizeObserver(redraw)" in text
    assert "non-scaling-stroke" in text
    assert "host.clientWidth" in text


@pytest.mark.unit
def test_dashboard_has_wide_history_surfaces(minimal_config):
    # Guard the Slice B surfaces: per-tab chart + event range selectors and the
    # "load older" paging that drives wide-history viewing. v6.1 split the single
    # History graph into per-tab charts (power/battery/energy ranges).
    html = _handler(minimal_config, path="/")._serve_static("/")[1].decode("utf-8")
    assert 'id="battery-range"' in html
    assert 'id="power-range"' in html
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
def test_dashboard_formats_runtime_for_humans(minimal_config):
    """Web UI formats runtime seconds without changing API values."""
    html = _handler(minimal_config, path="/")._serve_static("/")[1].decode("utf-8")
    js = _handler(minimal_config, path="/app.js")._serve_static(
        "/app.js")[1].decode("utf-8")

    assert '<option value="runtime">Runtime</option>' in html
    assert "function formatRuntimeSeconds" in js
    assert 'return Math.floor(seconds / 3600) + "h "' in js
    assert 'return Math.floor(seconds / 60) + "m " + (seconds % 60) + "s"' in js
    assert 'text: formatRuntimeSeconds(u.runtime)' in js
    assert 'detailRow("Runtime", formatRuntimeSeconds(u.runtime))' in js
    assert 'metric === "runtime"' in js


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
    assert "window.scrollY" in js
    assert "window.scrollTo(scrollX, scrollY)" in js


@pytest.mark.unit
def test_dashboard_event_type_filter_supports_multiple_selection(minimal_config):
    """The Events Type filter should allow selecting more than one event type."""
    html = _handler(minimal_config, path="/")._serve_static("/")[1].decode("utf-8")
    js = _handler(minimal_config, path="/app.js")._serve_static(
        "/app.js")[1].decode("utf-8")
    css = _handler(minimal_config, path="/style.css")._serve_static(
        "/style.css")[1].decode("utf-8")

    assert 'id="event-type-summary"' in html
    assert 'role="group" aria-labelledby="event-type-label"' in html
    assert "selectedEventTypes" in js
    assert "types.size === 0 || types.has(eventType)" in js
    assert 'input[type="checkbox"]:checked' in js
    assert 'selected.length + " types"' in js
    assert ".event-type-picker" in css
    assert ".event-type-option" in css


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
def test_dashboard_serves_tabbed_shell(minimal_config):
    # v6.1: the dashboard is a tabbed SPA. The tab nav + every panel must be in
    # the served markup with real ARIA roles, and the JS must own the tab logic
    # (arrow-key nav + hash routing). No browser in CI, so assert the surfaces.
    html = _handler(minimal_config, path="/")._serve_static("/")[1].decode("utf-8")
    assert 'role="tablist"' in html
    for tab in ("overview", "power", "battery", "energy", "events", "control", "config"):
        assert f'id="tab-{tab}"' in html
        assert f'id="panel-{tab}"' in html
    assert 'role="tab"' in html
    assert 'role="tabpanel"' in html
    assert 'aria-controls="panel-overview"' in html
    js = _handler(minimal_config, path="/app.js")._serve_static(
        "/app.js")[1].decode("utf-8")
    assert "function selectTab" in js
    assert "function initTabs" in js
    assert "aria-selected" in js
    assert "ArrowRight" in js and "ArrowLeft" in js
    assert "hashchange" in js
    # The Control tab is hidden until authenticated + nut_control enabled.
    assert 'id="tab-control"' in html and "tab.hidden = !available" in js


@pytest.mark.unit
def test_dashboard_charts_have_bands_and_event_overlays(minimal_config):
    # v6.1 B9b: the Power chart carries voltage threshold bands (reference
    # overlay of the live config) and charts carry power-event overlay markers.
    js = _handler(minimal_config, path="/app.js")._serve_static(
        "/app.js")[1].decode("utf-8")
    assert "isVoltageMetric" in js
    assert "eventMarkerClass" in js
    assert "nominalVoltage" in js and "warningLow" in js and "warningHigh" in js
    # Band is presented as a reference overlay, not historical truth.
    assert "reference overlay" in js
    # Marker count is bounded so a dense window doesn't drown the SVG.
    assert "const MAX = 100" in js
    css = _handler(minimal_config, path="/style.css")._serve_static(
        "/style.css")[1].decode("utf-8")
    assert ".band" in css and ".ev-line" in css
    html = _handler(minimal_config, path="/")._serve_static("/")[1].decode("utf-8")
    assert 'id="power-graph"' in html
    assert 'id="battery-health"' in html
    assert 'id="energy-cards"' in html


@pytest.mark.unit
def test_dashboard_energy_dual_line_and_power_endpoint(minimal_config):
    # v6.1 UX: the Energy chart is a dual-line load% + watts plot fed by the new
    # /power endpoint (not the old load-only history metric).
    js = _handler(minimal_config, path="/app.js")._serve_static(
        "/app.js")[1].decode("utf-8")
    assert "function makeEnergyChart" in js
    assert "function drawEnergyChart" in js
    assert "/power" in js
    assert "plot-load" in js and "plot-watts" in js
    css = _handler(minimal_config, path="/style.css")._serve_static(
        "/style.css")[1].decode("utf-8")
    assert ".plot-watts" in css


@pytest.mark.unit
def test_dashboard_tier1_events_and_dropdown_and_icons(minimal_config):
    js = _handler(minimal_config, path="/app.js")._serve_static(
        "/app.js")[1].decode("utf-8")
    # Chart markers + default events selection are restricted to tier-1 events.
    assert "isTier1Event" in js
    assert "TIER1_EVENT_PATTERNS" in js
    assert "_eventTypeDefaultApplied" in js
    # Dropdown closes on outside click; chart load() has a generation race guard.
    assert "details.event-type-picker[open]" in js
    assert "myGen !== gen" in js
    # Tabs carry inline-SVG icons injected by initTabs (no emoji font / tofu).
    assert "TAB_ICONS" in js and "function initTabs" in js
    html = _handler(minimal_config, path="/")._serve_static("/")[1].decode("utf-8")
    # Brand lightning bolt in the header + inline ⚡ SVG favicon (no packaged
    # asset, no emoji font dependency).
    assert 'class="ic brand-bolt"' in html
    assert "image/svg+xml" in html
    css = _handler(minimal_config, path="/style.css")._serve_static(
        "/style.css")[1].decode("utf-8")
    assert ".tab .ic" in css and ".brand-bolt" in css
    # Keyboard focus cue on panels is preserved (not removed).
    assert ".panel:focus-visible" in css and "outline: 2px solid var(--accent)" in css


@pytest.mark.unit
def test_dashboard_shared_range_and_cost_hint(minimal_config):
    html = _handler(minimal_config, path="/")._serve_static("/")[1].decode("utf-8")
    # All three chart ranges carry identical options so the shared-range sync works.
    for rid in ('id="power-range"', 'id="battery-range"', 'id="energy-range"'):
        assert rid in html
    js = _handler(minimal_config, path="/app.js")._serve_static(
        "/app.js")[1].decode("utf-8")
    assert "RANGE_SELECTS" in js
    # UPS selection is shared across the chart tabs too (like Range).
    assert "CHART_UPS_SELECTS" in js
    # Cost hint keys off whether cost is CONFIGURED, not whether a value exists.
    assert "energyCostConfigured" in js
    assert '"todayCost" in en' in js


@pytest.mark.unit
def test_dashboard_round3_ux(minimal_config):
    js = _handler(minimal_config, path="/app.js")._serve_static(
        "/app.js")[1].decode("utf-8")
    css = _handler(minimal_config, path="/style.css")._serve_static(
        "/style.css")[1].decode("utf-8")
    html = _handler(minimal_config, path="/")._serve_static("/")[1].decode("utf-8")
    # Metric name + unit label on charts.
    assert "function metricLabel" in js and "Input voltage (V)" in js
    assert ".chart-title" in css
    # Event markers: triangle pin + color-by-type incl. green recovery + non-blue.
    assert "ev-pin" in js and "ev-ok" in js
    assert ".ev-ok" in css and ".ev-pin" in css
    # Energy: "calculating…" not "unknown"; month hidden until data; the
    # window/estimated context moved from footnotes to "?" hints (ENERGY_HELP).
    assert "calculating" in js
    assert "ENERGY_HELP" in js and "function helpHint" in js
    assert "This month" in js
    # Config tab: colored, collapsible JSON tree (<details> per section) + only
    # enabled features.
    assert "json-tree" in js and "Enabled features" in js
    assert "function jsonNode" in js and "json-node" in js
    assert ".json-tree" in css and ".j-key" in css and ".j-str" in css
    # Detail modal closes on backdrop click; events filters scroll to top.
    assert 'ev.target.id === "detail-modal"' in js
    assert "window.scrollTo(0, 0)" in js


@pytest.mark.unit
def test_dashboard_event_markers_are_hoverable(minimal_config):
    # The whole vertical guide (not just the 3px dot) must be hoverable and the
    # tooltip must carry the event description (type + time + detail).
    js = _handler(minimal_config, path="/app.js")._serve_static(
        "/app.js")[1].decode("utf-8")
    assert "function appendEventMarker" in js
    assert "function eventDescription" in js
    assert "ev-hit" in js
    css = _handler(minimal_config, path="/style.css")._serve_static(
        "/style.css")[1].decode("utf-8")
    assert ".ev-hit" in css and "stroke: transparent" in css


@pytest.mark.unit
def test_dashboard_instant_tooltip_replaces_native_title(minimal_config):
    # Event markers use the instant, themed floating tip (bindTip/eventTipNode),
    # NOT the native SVG <title> that only appeared after a ~1s browser delay.
    js = _handler(minimal_config, path="/app.js")._serve_static(
        "/app.js")[1].decode("utf-8")
    css = _handler(minimal_config, path="/style.css")._serve_static(
        "/style.css")[1].decode("utf-8")
    assert "function bindTip" in js and "function showTip" in js
    assert "function eventTipNode" in js
    # The native-title element must be gone from the marker builder.
    assert 'createElementNS(SVG_NS, "title")' not in js
    assert ".tip {" in css and ".help {" in css


@pytest.mark.unit
def test_dashboard_remote_server_health_widget(minimal_config):
    # Overview surfaces remote-server reachability from /api/v1/remote-health.
    js = _handler(minimal_config, path="/app.js")._serve_static(
        "/app.js")[1].decode("utf-8")
    html = _handler(minimal_config, path="/")._serve_static("/")[1].decode("utf-8")
    assert "function renderRemoteHealth" in js and "remoteStatusClass" in js
    assert 'id="remote-section"' in html and 'id="remote-cards"' in html
    assert "reachable" in js and "unreachable" in js


@pytest.mark.unit
def test_dashboard_line_quality_card(minimal_config):
    # Power tab carries a derived Good/Fair/Poor line-quality summary built from
    # the live power-quality block (voltage band, frequency, regulation states).
    js = _handler(minimal_config, path="/app.js")._serve_static(
        "/app.js")[1].decode("utf-8")
    html = _handler(minimal_config, path="/")._serve_static("/")[1].decode("utf-8")
    assert "function renderLineQuality" in js and "function lineQuality" in js
    assert 'id="line-quality"' in html
    assert "Line quality" in js
    # Reads the UPS regulation states the daemon exposes.
    for state in ("voltageState", "avrState", "bypassState", "overloadState"):
        assert state in js


@pytest.mark.unit
def test_dashboard_rc11_surfaces(minimal_config):
    js = _handler(minimal_config, path="/app.js")._serve_static(
        "/app.js")[1].decode("utf-8")
    css = _handler(minimal_config, path="/style.css")._serve_static(
        "/style.css")[1].decode("utf-8")
    html = _handler(minimal_config, path="/")._serve_static("/")[1].decode("utf-8")
    # Shutdown tab: nav button + panel + DAG renderer.
    assert 'data-tab="shutdown"' in html and 'id="panel-shutdown"' in html
    assert 'id="shutdown-plan"' in html
    assert "function renderShutdownPlan" in js and "shutdown-plan" in js
    assert ".sd-flow" in css and ".sd-node" in css
    # Shutdown plan is reachable per-UPS (remote-only/multi-UPS) + shows the
    # redundancy-group quorum trigger.
    assert 'id="shutdown-ups"' in html
    assert "function populateShutdownUpsSelect" in js
    assert "function shutdownTriggerNodes" in js and ".sd-trigger" in css
    assert "drops below" in js
    # Battery: per-term breakdown + score trend graph (new history endpoint).
    assert "BH_TERM_LABELS" in js and "function renderBatteryHealthGraph" in js
    assert "battery-health-history" in js
    assert 'id="bh-graph"' in html
    # Temperature is graphable on the Battery tab.
    assert '<option value="temperature"' in html
    # Events table: colored, icon-led type badges.
    assert "function eventTypeBadge" in js and ".ev-badge" in css
    # Energy: this-year window.
    assert "yearKwh" in js and "This year" in js
    # Event-marker hover works over the whole column (decoration non-interactive).
    assert "pointer-events: none" in css


@pytest.mark.unit
def test_dashboard_json_tree_preserves_state_and_drops_counts(minimal_config):
    # The config JSON tree must not collapse on every 10s poll (rebuild only when
    # the config changed) and must not show the ugly per-section item count.
    js = _handler(minimal_config, path="/app.js")._serve_static(
        "/app.js")[1].decode("utf-8")
    css = _handler(minimal_config, path="/style.css")._serve_static(
        "/style.css")[1].decode("utf-8")
    assert "_lastConfigJson" in js
    assert "j-ellipsis" in js and ".j-ellipsis" in css
    # The old "{ <count> }" annotation (template literal over entries.length) is gone.
    assert "entries.length" not in js


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
