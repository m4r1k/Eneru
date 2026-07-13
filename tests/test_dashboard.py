"""Unit tests for the v6.0 dashboard static serving (api.py + eneru.web)."""

import json
import shutil
import subprocess
import textwrap
from io import BytesIO
from unittest.mock import MagicMock

import pytest

from conftest import make_api_handler
from eneru.api import EneruAPIHandler, SessionManager

NODE = shutil.which("node")


def _handler(config, *, path):
    # F-063: shared EneruAPIHandler builder lives in conftest.py. It
    # defaults Host: localhost so do_GET() clears the F-016 dispatch guard.
    return make_api_handler(config, path=path)


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
def test_dashboard_perf_hardening_v617(minimal_config):
    """v6.1.7 dashboard perf fixes (no browser in CI, so assert the source).

    F-043: refresh() has an in-flight re-entrancy guard so a slow 10s cycle
    can't stack overlapping refreshes.
    F-022/F-088: chart event markers are cached between steady-state refreshes;
    genuine activation/range and exact NUT power-state changes rescan.
    F-029: the accumulated-events cap grows on explicit "Load older" so paged
    rows survive the newest-N slice instead of being discarded.
    """
    js = _handler(minimal_config, path="/app.js")._serve_static(
        "/app.js")[1].decode("utf-8")
    # F-043: re-entrancy guard around the async refresh.
    assert "let refreshing = false;" in js
    assert "if (refreshing) return;" in js
    assert "async function refreshOnce()" in js
    # F-022/F-088: steady-state polls reuse markers; power changes refetch.
    assert "refetchEvents" in js
    assert "powerStateSignature(lastUpsRows)" in js
    assert "refetchEvents: powerStateChanged" in js
    assert "state.eventsKey" in js
    # F-029: the newest-rows cap grows on explicit paging, resets on new dataset.
    assert "let eventsCap = EVENTS_BASE_CAP;" in js
    assert "eventsCap += EVENTS_BASE_CAP;" in js
    assert "slice(-eventsCap)" in js
    assert "slice(-2000)" not in js   # the old fixed cap is gone


@pytest.mark.unit
def test_dashboard_login_clears_password_field(minimal_config):
    """F-041: the plaintext password must not persist in the DOM — the login
    field is cleared on submit (success AND failure) and around open/close.
    No browser in CI, so assert the source scrubs the field."""
    js = _handler(minimal_config, path="/app.js")._serve_static(
        "/app.js")[1].decode("utf-8")
    # The doLogin handler wipes the field unconditionally after submit, and
    # both openLogin and closeLogin scrub it too.
    assert js.count('document.getElementById("login-pass").value = ""') >= 3


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
    assert 'vital("Runtime", formatRuntimeSeconds(u.runtime))' in js
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
@pytest.mark.skipif(NODE is None, reason="needs node")
def test_dashboard_fleet_overview_summarizes_every_ups(minimal_config):
    """Fleet mode must describe the fleet instead of promoting one UPS."""
    html = _handler(minimal_config, path="/")._serve_static("/")[1].decode("utf-8")
    js = _handler(minimal_config, path="/app.js")._serve_static(
        "/app.js")[1].decode("utf-8")

    assert 'aria-label="Dashboard view"' in html
    assert "Fleet overview" in js
    assert "function fleetOverview" in js
    assert "hero.appendChild(fleetOverview(view))" in js
    assert "heroCard(primary)" not in js

    status = js[js.index("function statusClass"):js.index("// ----- rendering -----")]
    health = js[js.index("function upsHealthy"):js.index("function groupHealthyCount")]
    start = js.index("function fleetSnapshot")
    snapshot = js[start:js.index("function fleetOverview", start)]
    script = status + health + snapshot + textwrap.dedent("""
        const state = fleetSnapshot([
          {name: "rack", status: "OL", connectionState: "OK"},
          {name: "desk", status: "OB DISCHRG", connectionState: "OK"},
          {name: "lab", status: "OL", connectionState: "DISCONNECTED"},
          {name: "bypass", status: "OL BYPASS", connectionState: "OK"},
        ]);
        const severity = {
          healthy: fleetOverallClass([
            {name: "rack", status: "OL", connectionState: "OK"},
          ]),
          disconnected: fleetOverallClass([
            {name: "rack", status: "OL", connectionState: "DISCONNECTED"},
          ]),
          lowBattery: fleetOverallClass([
            {name: "rack", status: "LB", connectionState: "OK"},
          ]),
          forcedShutdown: fleetOverallClass([
            {name: "rack", status: "FSD", connectionState: "OK"},
          ]),
          boost: fleetOverallClass([
            {name: "rack", status: "OL BOOST", connectionState: "OK"},
          ]),
          trim: fleetOverallClass([
            {name: "rack", status: "OL TRIM", connectionState: "OK"},
          ]),
          bypass: fleetOverallClass([
            {name: "rack", status: "OL BYPASS", connectionState: "OK"},
          ]),
          disconnectedOnBattery: fleetOverallClass([
            {name: "rack", status: "OB", connectionState: "DISCONNECTED"},
          ]),
          disconnectedForcedShutdown: fleetOverallClass([
            {name: "rack", status: "FSD", connectionState: "DISCONNECTED"},
          ]),
        };
        const quorumHealth = {
          boost: upsHealthy({status: "OL BOOST", connectionState: "OK"}),
          trim: upsHealthy({status: "OL TRIM", connectionState: "OK"}),
          bypass: upsHealthy({status: "OL BYPASS", connectionState: "OK"}),
          forcedShutdown: upsHealthy({status: "FSD", connectionState: "OK"}),
        };
        process.stdout.write(JSON.stringify({state, severity, quorumHealth}));
    """)
    result = subprocess.run([NODE, "-"], input=script, text=True,
                            capture_output=True, check=True)
    assert json.loads(result.stdout) == {
        "state": {
            "total": 4,
            "healthy": 1,
            "attention": 3,
            "onBattery": 1,
        },
        "severity": {
            "healthy": "ok",
            "disconnected": "warn",
            "lowBattery": "crit",
            "forcedShutdown": "crit",
            "boost": "warn",
            "trim": "warn",
            "bypass": "warn",
            "disconnectedOnBattery": "crit",
            "disconnectedForcedShutdown": "crit",
        },
        "quorumHealth": {
            "boost": True,
            "trim": True,
            "bypass": True,
            "forcedShutdown": False,
        },
    }


@pytest.mark.unit
@pytest.mark.skipif(NODE is None, reason="needs node")
def test_dashboard_fleet_overview_marks_blank_telemetry_unknown(minimal_config):
    """Empty monitoring values must render as unknown, never as bare units."""
    js = _handler(minimal_config, path="/app.js")._serve_static(
        "/app.js")[1].decode("utf-8")
    helper_start = js.index("function numOrNull")
    helper_end = js.index("function statusClass", helper_start)
    helpers = js[helper_start:helper_end]
    script = helpers + textwrap.dedent("""
        process.stdout.write(JSON.stringify({
          emptyLoad: formatFleetMetric("", "%"),
          emptyInput: formatFleetMetric("", " V"),
          missing: formatFleetMetric(null, "%"),
          invalid: formatFleetMetric("unknown", " V"),
          zero: formatFleetMetric(0, "%"),
          numericText: formatFleetMetric("230", " V"),
        }));
    """)
    result = subprocess.run([NODE, "-"], input=script, text=True,
                            capture_output=True, check=True)
    assert json.loads(result.stdout) == {
        "emptyLoad": "—",
        "emptyInput": "—",
        "missing": "—",
        "invalid": "—",
        "zero": "0%",
        "numericText": "230 V",
    }


@pytest.mark.unit
def test_dashboard_fleet_overview_stays_accessible_at_tablet_width(minimal_config):
    """Wide comparison rows must remain scrollable before mobile stacking."""
    css = _handler(minimal_config, path="/style.css")._serve_static(
        "/style.css")[1].decode("utf-8")

    assert ".fleet-overview-list { padding: 0.35rem 0; overflow-x: auto;" in css
    assert ".fleet-overview-name strong { min-width: 0; overflow: hidden;" in css
    assert "text-overflow: ellipsis; white-space: nowrap;" in css


@pytest.mark.unit
@pytest.mark.skipif(NODE is None, reason="needs node")
def test_dashboard_fleet_chart_source_is_explicit_and_persistent(minimal_config):
    """Fleet charts keep their explicit source instead of resetting to UPS 1."""
    html = _handler(minimal_config, path="/")._serve_static("/")[1].decode("utf-8")
    js = _handler(minimal_config, path="/app.js")._serve_static(
        "/app.js")[1].decode("utf-8")

    assert html.count("Chart UPS") == 3
    assert '" (primary)"' not in js

    start = js.index("function chartSourceName")
    helper = js[start:js.index("function populateChartUpsSelects", start)]
    script = "const SCOPE_ALL = '__all__';\n" + helper + textwrap.dedent("""
        const rows = [{name: "rack"}, {name: "desk"}];
        process.stdout.write(JSON.stringify({
          fleetKeepsPrior: chartSourceName(SCOPE_ALL, rows, "desk"),
          fleetDefaultsFirst: chartSourceName(SCOPE_ALL, rows, "missing"),
          scopedFollowsView: chartSourceName("desk", rows, "rack"),
          emptyHasNoSource: chartSourceName(SCOPE_ALL, [], "rack"),
        }));
    """)
    result = subprocess.run([NODE, "-"], input=script, text=True,
                            capture_output=True, check=True)
    assert json.loads(result.stdout) == {
        "fleetKeepsPrior": "desk",
        "fleetDefaultsFirst": "rack",
        "scopedFollowsView": "desk",
        "emptyHasNoSource": "",
    }


@pytest.mark.unit
@pytest.mark.skipif(NODE is None, reason="needs node")
def test_dashboard_control_tab_honors_dashboard_view(minimal_config):
    """Control shows the fleet or exactly the UPS selected in View."""
    js = _handler(minimal_config, path="/app.js")._serve_static(
        "/app.js")[1].decode("utf-8")

    control = js[js.index("async function renderControl"):
                 js.index("async function renderVariableForms")]
    assert "rowsForScope(allRows, currentScope())" in control
    assert 'else if (name === "control") renderControl({ ups: lastUpsRows })' in js

    start = js.index("function rowsForScope")
    helper = js[start:js.index("function scopedRows", start)]
    script = "const SCOPE_ALL = '__all__';\n" + helper + textwrap.dedent("""
        const rows = [{name: "rack"}, {name: "desk"}];
        process.stdout.write(JSON.stringify({
          fleet: rowsForScope(rows, SCOPE_ALL).map(row => row.name),
          rack: rowsForScope(rows, "rack").map(row => row.name),
          desk: rowsForScope(rows, "desk").map(row => row.name),
          staleFallsBackSafely: rowsForScope(rows, "missing").map(row => row.name),
        }));
    """)
    result = subprocess.run([NODE, "-"], input=script, text=True,
                            capture_output=True, check=True)
    assert json.loads(result.stdout) == {
        "fleet": ["rack", "desk"],
        "rack": ["rack"],
        "desk": ["desk"],
        "staleFallsBackSafely": ["rack", "desk"],
    }


@pytest.mark.unit
@pytest.mark.skipif(NODE is None, reason="needs node")
def test_dashboard_control_discards_stale_scope_render(minimal_config):
    """A slow old request must never restore controls for the prior View."""
    js = _handler(minimal_config, path="/app.js")._serve_static(
        "/app.js")[1].decode("utf-8")
    helper_start = js.index("function rowsForScope")
    helper = js[helper_start:js.index("function scopedRows", helper_start)]
    control_start = js.index("let _controlBuiltKey")
    control = js[control_start:js.index(
        "async function renderVariableForms", control_start)]
    script = "const SCOPE_ALL = '__all__';\n" + helper + textwrap.dedent("""
        let scope = "lab";
        let cfgSnapshot = {nutControl: {
          enabled: true, allowedCommands: [], allowedVariables: [],
        }};
        let activeTab = "control";
        const nodes = {
          "control-section": {hidden: false},
          "control-empty": {hidden: false},
          "control-panel": {
            childNodes: [],
            replaceChildren(...children) {
              this.childNodes = children.flatMap(
                child => child && child.isFragment ? child.childNodes : [child]);
            },
          },
          "tab-control": {hidden: false},
        };
        const document = {
          getElementById: id => nodes[id],
          createDocumentFragment() {
            return {isFragment: true, childNodes: [], appendChild(node) {
              this.childNodes.push(node);
            }};
          },
        };
        function el(tag, attrs, children) {
          return {
            tag, attrs: attrs || {}, childNodes: (children || []).slice(),
            appendChild(node) { this.childNodes.push(node); },
            addEventListener() {},
          };
        }
        function token() { return "session"; }
        function currentScope() { return scope; }
        function selectTab() {}
        function runCommand() {}
        function runSelfTest() {}
        async function renderVariableForms(name) {
          return {ok: true, node: el("div", {ups: name})};
        }
        const pending = {};
        function api(path) {
          const name = decodeURIComponent(path.split("/")[4]);
          return new Promise(resolve => { pending[name] = resolve; });
        }
    """) + control + textwrap.dedent("""
        function headings() {
          return nodes["control-panel"].childNodes
            .flatMap(box => box.childNodes || [])
            .filter(node => node.tag === "h3")
            .map(node => node.attrs.text);
        }
        (async () => {
          const lab = renderControl({ups: [
            {name: "lab", label: "Lab"}, {name: "apc", label: "APC"},
          ]});
          scope = "apc";
          const apc = renderControl({ups: [
            {name: "lab", label: "Lab"}, {name: "apc", label: "APC"},
          ]});
          const loadingIsInert = headings().length === 0;
          pending.apc({ok: true, data: {commands: ["test.apc"]}});
          await apc;
          const afterApc = headings();
          pending.lab({ok: true, data: {commands: ["test.lab"]}});
          await lab;
          process.stdout.write(JSON.stringify({
            loadingIsInert, afterApc, afterLateLab: headings(),
          }));
        })();
    """)
    result = subprocess.run([NODE, "-"], input=script, text=True,
                            capture_output=True, check=True)
    assert json.loads(result.stdout) == {
        "loadingIsInert": True,
        "afterApc": ["APC"],
        "afterLateLab": ["APC"],
    }


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
    # Chart markers + default events view key off the window-independent tier.
    assert "isTier1Event" in js
    assert "TIER1_EVENT_PATTERNS" in js
    assert "function eventPassesTier" in js
    # Dropdown closes on outside click; chart load() has a generation race guard.
    assert "details.event-type-picker[open]" in js
    assert "myGen !== gen" in js
    # Tabs carry inline-SVG icons injected by initTabs (no emoji font / tofu).
    assert "TAB_ICONS" in js and "function initTabs" in js
    html = _handler(minimal_config, path="/")._serve_static("/")[1].decode("utf-8")
    # Brand lightning bolt in the header + a same-origin /favicon.svg link
    # (ISS-011: it's a PACKAGED asset served by the daemon, since the CSP blocks
    # data: URLs — shipped in wheels via the eneru.web *.svg glob and in deb/rpm
    # via nfpm).
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
    # AVR is ternary in the API: BOOST/TRIM are active regulation states, not
    # the literal ACTIVE used by the binary bypass/overload flags.
    assert "function isAvrActive" in js
    assert '"BOOST", "TRIM"' in js
    assert "isAvrActive(pq.avrState)" in js
    assert "isBinaryActive(pq.bypassState)" in js


@pytest.mark.unit
@pytest.mark.skipif(NODE is None, reason="needs node")
def test_dashboard_line_quality_state_behavior(minimal_config):
    js = _handler(minimal_config, path="/app.js")._serve_static(
        "/app.js")[1].decode("utf-8")
    start = js.index("function nearNominalFreq")
    end = js.index("function lineQualityCard")
    helpers = js[start:end]
    script = textwrap.dedent(f"""
        function numOrNull(v) {{
          if (v === undefined || v === null || v === "") return null;
          const n = Number(v);
          return Number.isFinite(n) ? n : null;
        }}
        function titleCase(v) {{ return String(v || ""); }}
        function el(tag, opts, children) {{
          return {{ tag, opts: opts || {{}}, children: children || [] }};
        }}
        {helpers}
        const base = {{
          inputVoltage: "230", warningLow: 207, warningHigh: 253,
          voltageState: "NORMAL", avrState: "INACTIVE",
          bypassState: "INACTIVE", overloadState: "INACTIVE"
        }};
        const cases = [
          [{{ ...base, avrState: "BOOST" }}, {{ cls: "warn", label: "Fair" }}],
          [{{ ...base, avrState: "TRIM" }}, {{ cls: "warn", label: "Fair" }}],
          [{{ ...base, bypassState: "BOOST" }}, {{ cls: "ok", label: "Good" }}],
          [{{ ...base, overloadState: "TRIM" }}, {{ cls: "ok", label: "Good" }}],
          [{{ ...base, bypassState: "ACTIVE" }}, {{ cls: "crit", label: "Poor" }}],
          [{{ ...base, overloadState: "ACTIVE" }}, {{ cls: "crit", label: "Poor" }}],
        ];
        for (const [input, expected] of cases) {{
          const actual = lineQuality(input);
          if (JSON.stringify(actual) !== JSON.stringify(expected)) {{
            throw new Error(`lineQuality mismatch: ${{JSON.stringify(input)}} -> `
              + `${{JSON.stringify(actual)}} expected ${{JSON.stringify(expected)}}`);
          }}
        }}
        function rowClass(label, value) {{
          return stateRow(label, value).children[1].opts.class;
        }}
        const rows = {{
          avrBoost: rowClass("AVR", "BOOST"),
          avrTrim: rowClass("AVR", "TRIM"),
          bypassBoost: rowClass("Bypass", "BOOST"),
          overloadTrim: rowClass("Overload", "TRIM"),
          bypassActive: rowClass("Bypass", "ACTIVE"),
          overloadActive: rowClass("Overload", "ACTIVE"),
        }};
        process.stdout.write(JSON.stringify(rows));
    """)
    result = subprocess.run([NODE, "-"], input=script, text=True,
                            capture_output=True, check=True)
    rows = json.loads(result.stdout)
    assert rows["avrBoost"] == "badge warn"
    assert rows["avrTrim"] == "badge warn"
    assert rows["bypassBoost"] == "badge "
    assert rows["overloadTrim"] == "badge "
    assert rows["bypassActive"] == "badge warn"
    assert rows["overloadActive"] == "badge warn"


@pytest.mark.unit
def test_dashboard_event_tier_dropdown(minimal_config):
    # The Events tab gains a window-INDEPENDENT tier selector (power/diag/all)
    # so widening the time range still surfaces power events.
    js = _handler(minimal_config, path="/app.js")._serve_static(
        "/app.js")[1].decode("utf-8")
    html = _handler(minimal_config, path="/")._serve_static("/")[1].decode("utf-8")
    assert 'id="event-tier"' in html
    assert "function eventTierMode" in js and "function eventPassesTier" in js
    assert "function eventTierOf" in js and "function isLifecycleEvent" in js
    # Emitted names are matched correctly (BATTERY_LOW, not LOW_BATTERY) and the
    # v6.1 battery-health alerts are tier-1.
    assert '"BATTERY_LOW"' in js and '"BATTERY_HEALTH"' in js
    assert '"LOW_BATTERY"' not in js
    # The old window-derived default was removed.
    assert "_eventTypeDefaultApplied" not in js


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


@pytest.mark.unit
@pytest.mark.skipif(NODE is None, reason="needs node")
def test_dashboard_merge_events_dedup_sort_cap(minimal_config):
    """Execute event merge, power-transition, and auth-state helpers in Node."""
    js = _handler(minimal_config, path="/app.js")._serve_static(
        "/app.js")[1].decode("utf-8")
    event_key = js[js.index("function eventKey"):js.index("// F-029: the accumulated")]
    merge = js[js.index("function mergeEvents"):js.index("function eventRangeFrom")]
    refresh_helpers = js[
        js.index("function nutStatusTokens"):
        js.index("// ----- theme (light / dark / system)")
    ]
    script = textwrap.dedent("""
        let lastEvents = [];
        let eventsCap = 3;
        function updateEventTypeFilter() {}
        function preserveWindowScroll(fn) { if (fn) fn(); }
        function applyEventFilters() {}
    """) + refresh_helpers + event_key + merge + textwrap.dedent("""
        mergeEvents([{source:"A", id:1, ts:10, eventType:"ON_BATTERY"}]);
        mergeEvents([{source:"A", id:1, ts:10, eventType:"ON_LINE"}]); // same key -> replace
        mergeEvents([{source:"A", id:2, ts:5}]);                        // earlier ts sorts first
        const afterDedup = lastEvents.map(e => e.source + ":" + e.id + ":" + e.eventType);
        mergeEvents([{source:"A", id:3, ts:20}, {source:"A", id:4, ts:30}]); // overflow cap
        const power = {
          olNoiseStable: powerStateSignature([{name:"A", status:"OL CHRG"}])
            === powerStateSignature([{name:"A", status:"OL"}]),
          orderStable: powerStateSignature([
            {name:"B", status:"OB"}, {name:"A", status:"OL"}
          ]) === powerStateSignature([
            {name:"A", status:"OL"}, {name:"B", status:"OB"}
          ]),
          outageStarts: powerStateSignature([{name:"A", status:"OL"}])
            !== powerStateSignature([{name:"A", status:"OB DISCHRG"}]),
          outageEnds: powerStateSignature([{name:"A", status:"OB"}])
            !== powerStateSignature([{name:"A", status:"OL CHRG"}]),
        };
        const auth = {
          enabledKeeps: authStateRequiresClear(
            {ok:true, data:{enabled:true}}, {ok:true, data:{detail:"extended"}}, true),
          transientKeeps: authStateRequiresClear(
            {ok:false, data:null}, {ok:false, data:null}, true),
          disabledClears: authStateRequiresClear(
            {ok:true, data:{enabled:false}}, {ok:true, data:{}}, true),
          sanitizedClears: authStateRequiresClear(
            {ok:true, data:{enabled:true}}, {ok:true, data:{detail:"sanitized"}}, true),
          api401Clears: responseInvalidatesAuth(401, "/api/v1/config"),
          login401Keeps: responseInvalidatesAuth(401, "/api/v1/auth/login"),
        };
        process.stdout.write(JSON.stringify({
          afterDedup: afterDedup,
          count: lastEvents.length,
          oldest: lastEvents[0].id,
          newest: lastEvents[lastEvents.length - 1].id,
          power,
          auth,
        }));
    """)
    result = subprocess.run([NODE, "-"], input=script, text=True,
                            capture_output=True, check=True)
    data = json.loads(result.stdout)
    # De-dup by (source,id): the second merge REPLACED id 1's row, not appended.
    assert len(data["afterDedup"]) == 2
    assert "A:1:ON_LINE" in data["afterDedup"]
    # Cap keeps the newest 3 by timestamp (id 2 @ ts5 drops off).
    assert data["count"] == 3
    assert data["oldest"] == 1
    assert data["newest"] == 4
    assert data["power"] == {
        "olNoiseStable": True,
        "orderStable": True,
        "outageStarts": True,
        "outageEnds": True,
    }
    assert data["auth"] == {
        "enabledKeeps": False,
        "transientKeeps": False,
        "disabledClears": True,
        "sanitizedClears": True,
        "api401Clears": True,
        "login401Keeps": False,
    }


@pytest.mark.unit
@pytest.mark.skipif(NODE is None, reason="needs node")
def test_dashboard_outage_spans_close_at_shutdown_boundary(minimal_config):
    """v6.1.7: exercise app.js `computeOutageSpans` in a node shim.

    The prod bug: after two real power outages the chart tabs stayed
    perma-RED. The power died, the daemon shut the host down, and on reboot
    it started FRESH already on line — it never witnessed the OB→OL flip, so
    it never emitted POWER_RESTORED, and the outage band ran to "now" forever.

    Asserts the fixed span math:
      (a) ON_BATTERY → EMERGENCY_SHUTDOWN_INITIATED → DAEMON_START with NO
          POWER_RESTORED closes the band AT the shutdown boundary, not at t1.
      (b) ON_BATTERY → POWER_RESTORED still closes cleanly (restored).
      (c) ON_BATTERY with nothing after genuinely extends to t1 (ongoing).

    Skips cleanly where node is unavailable."""
    js = _handler(minimal_config, path="/app.js")._serve_static(
        "/app.js")[1].decode("utf-8")
    spans_src = js[js.index("const OUTAGE_CLOSE_BOUNDARIES"):
                   js.index("function appendOutageBands")]
    script = spans_src + textwrap.dedent("""
        const T0 = 0, T1 = 1000;
        // (a) outage closed by a shutdown/restart boundary (no restore).
        const a = computeOutageSpans([
          {ts: 100, eventType: "ON_BATTERY"},
          {ts: 150, eventType: "EMERGENCY_SHUTDOWN_INITIATED"},
          {ts: 400, eventType: "DAEMON_START"},
        ], T0, T1);
        // (b) clean restore.
        const b = computeOutageSpans([
          {ts: 100, eventType: "ON_BATTERY"},
          {ts: 200, eventType: "POWER_RESTORED"},
        ], T0, T1);
        // (c) genuinely ongoing outage, nothing after.
        const c = computeOutageSpans([
          {ts: 100, eventType: "ON_BATTERY"},
        ], T0, T1);
        process.stdout.write(JSON.stringify({
          a: a.map(s => ({start: s.start, end: s.end,
                          restore: s.restore ? s.restore.ts : null,
                          boundary: !!s.endedAtBoundary})),
          b: b.map(s => ({start: s.start, end: s.end,
                          restore: s.restore ? s.restore.ts : null,
                          boundary: !!s.endedAtBoundary})),
          c: c.map(s => ({start: s.start, end: s.end,
                          restore: s.restore ? s.restore.ts : null,
                          boundary: !!s.endedAtBoundary})),
        }));
    """)
    result = subprocess.run([NODE, "-"], input=script, text=True,
                            capture_output=True, check=True)
    data = json.loads(result.stdout)

    # (a) One span, ending AT the shutdown boundary (ts150), NOT at t1(1000),
    #     with no restore and flagged as ended-at-boundary.
    assert len(data["a"]) == 1
    assert data["a"][0]["start"] == 100
    assert data["a"][0]["end"] == 150
    assert data["a"][0]["end"] != 1000
    assert data["a"][0]["restore"] is None
    assert data["a"][0]["boundary"] is True

    # (b) Clean restore: span closes at the POWER_RESTORED ts, restore set.
    assert len(data["b"]) == 1
    assert data["b"][0]["start"] == 100
    assert data["b"][0]["end"] == 200
    assert data["b"][0]["restore"] == 200
    assert data["b"][0]["boundary"] is False

    # (c) Genuinely ongoing: extends to t1, no restore, not a boundary close.
    assert len(data["c"]) == 1
    assert data["c"][0]["start"] == 100
    assert data["c"][0]["end"] == 1000
    assert data["c"][0]["restore"] is None
    assert data["c"][0]["boundary"] is False


@pytest.mark.unit
@pytest.mark.skipif(NODE is None, reason="needs node")
def test_dashboard_chart_feed_keeps_boundary_events(minimal_config):
    """F-094: compose the REAL chart-feed filter with the span math.

    The span math above accepts DAEMON_START as a close boundary, but the
    chart feed (`fetchTierEvents`) used to strip lifecycle events before
    `computeOutageSpans` ever saw them — so an ON_BATTERY whose only closing
    event was a daemon restart (the actual 2026-07-09 production history:
    power died, host shut down, fresh boot already on line) still ran the
    red band to "now". The prior test could not catch that because it fed
    `computeOutageSpans` directly, skipping the filter.

    This test runs the same predicate the feed uses (`isChartFeedEvent`)
    plus the draw-time tier-1 marker re-filter, end to end."""
    js = _handler(minimal_config, path="/app.js")._serve_static(
        "/app.js")[1].decode("utf-8")
    tier_src = js[js.index("const TIER1_EVENT_PATTERNS"):
                  js.index("// Which display tier")]
    spans_src = js[js.index("const OUTAGE_CLOSE_BOUNDARIES"):
                   js.index("function appendOutageBands")]
    script = tier_src + spans_src + textwrap.dedent("""
        // The exact prod shape: two outages whose ONLY closing events are
        // daemon restarts — no POWER_RESTORED, no *_SHUTDOWN_* rows.
        const rows = [
          {ts: 100, eventType: "ON_BATTERY"},
          {ts: 150, eventType: "DAEMON_START"},
          {ts: 300, eventType: "ON_BATTERY"},
          {ts: 350, eventType: "DAEMON_START"},
          {ts: 380, eventType: "DAEMON_UPGRADED"},
          {ts: 390, eventType: "SLOW_NUT_RESPONSE"},
        ];
        // Same predicate fetchTierEvents applies (source matching aside).
        const feed = rows.filter((e) => isChartFeedEvent(e.eventType));
        const spans = computeOutageSpans(feed, 0, 1000);
        // Same tier-1 re-filter the marker draw sites apply.
        const markers = feed.filter((e) => isTier1Event(e.eventType));
        process.stdout.write(JSON.stringify({
          feedTypes: feed.map((e) => e.eventType),
          spans: spans.map((s) => ({start: s.start, end: s.end})),
          markerTypes: markers.map((e) => e.eventType),
        }));
    """)
    result = subprocess.run([NODE, "-"], input=script, text=True,
                            capture_output=True, check=True)
    data = json.loads(result.stdout)

    # The feed keeps the boundary rows (and drops diag-tier noise)...
    assert data["feedTypes"] == [
        "ON_BATTERY", "DAEMON_START", "ON_BATTERY", "DAEMON_START"]
    # ...so BOTH orphaned outages close at their restart boundary — two
    # bounded bands, not one running to t1 (the perma-red regression).
    assert data["spans"] == [
        {"start": 100, "end": 150}, {"start": 300, "end": 350}]
    # And the boundary rows never render as chart marker dots.
    assert data["markerTypes"] == ["ON_BATTERY", "ON_BATTERY"]
