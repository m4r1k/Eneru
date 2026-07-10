"""Tests for the read-only API and Prometheus formatter."""

import json
import logging
import time
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from eneru import (
    Config,
    MonitorState,
    RedundancyGroupConfig,
    UPSGroupMonitor,
)
from eneru.api import EneruAPIHandler, render_prometheus_metrics
from eneru.logger import JSONFormatter
from eneru.mqtt import MQTTPublisher
from eneru.stats import StatsStore
from eneru.remote_health import remote_health_sidecar_path
from eneru.status import (
    collect_status,
    config_summary,
    query_events,
    query_history,
    readiness,
    remote_health_for_config,
    state_file_path_for_group,
)


@pytest.fixture
def monitor(minimal_config, tmp_path):
    minimal_config.logging.battery_history_file = str(tmp_path / "battery-history")
    minimal_config.logging.shutdown_flag_file = str(tmp_path / "shutdown-flag")
    minimal_config.logging.state_file = str(tmp_path / "state")
    monitor = UPSGroupMonitor(minimal_config)
    monitor.state = MonitorState(
        latest_status="OL CHRG",
        latest_battery_charge="97",
        latest_runtime="1200",
        latest_load="20",
        latest_input_voltage="229.4",
        latest_output_voltage="230.1",
        latest_battery_voltage="27.2",
        latest_ups_temperature="32",
        latest_input_frequency="50.0",
        latest_output_frequency="50.0",
        latest_update_time=time.time(),
    )
    monitor.state.nominal_voltage = 230.0
    monitor.state.voltage_warning_low = 207.0
    monitor.state.voltage_warning_high = 253.0
    monitor.logger = MagicMock()
    return monitor


@pytest.mark.unit
def test_collect_status_is_read_only_shape(monitor):
    payload = collect_status(monitor)
    assert payload["ups"][0]["status"] == "OL CHRG"
    assert payload["ups"][0]["batteryCharge"] == "97"
    assert payload["ups"][0]["connectionState"] == "OK"
    assert payload["ups"][0]["groupId"] == "TestUPS-localhost"
    assert payload["ups"][0]["powerQuality"]["inputVoltage"] == "229.4"
    assert payload["ups"][0]["powerQuality"]["voltageState"] == "NORMAL"
    # v6.1.2: the daemon version + runtime context are surfaced at the top level
    # (dashboard footer), the latter mirroring the nested runtime.context.
    from eneru.version import __version__
    assert payload["version"] == __version__
    assert payload["runtimeContext"] == payload["runtime"]["context"]
    assert isinstance(payload["runtimeContext"], str) and payload["runtimeContext"]


@pytest.mark.unit
def test_readiness_failed_when_monitor_has_no_fresh_data(monitor):
    monitor.state.latest_update_time = 0
    payload = readiness(monitor)
    assert payload["ready"] is False
    # v5.5: readiness now decomposes per-capability. NUT polling shows up
    # as its own capability in the reasons list and capabilities array.
    assert "NUT monitoring not connected" in payload["reason"]
    assert any("nut_polling" in r for r in payload["reasons"])
    nut_cap = next(c for c in payload["capabilities"] if c["id"] == "nut_polling")
    assert nut_cap["achievable"] is False


@pytest.mark.unit
def test_readiness_uses_lightweight_snapshot(monkeypatch, monitor):
    def fail_if_called(_monitor):
        raise AssertionError("readiness must not read remote-health sidecars")

    monkeypatch.setattr("eneru.status.remote_health_for_monitor", fail_if_called)

    payload = readiness(monitor)

    assert payload["ready"] is True
    assert payload["ups"][0]["name"] == "TestUPS@localhost"


@pytest.mark.unit
def test_config_summary_hides_notification_urls(minimal_config):
    minimal_config.notifications.urls = ["discord://secret/token"]
    minimal_config.mqtt.broker = "mqtt://user:pass@example:1883"
    summary = config_summary(minimal_config)
    text = str(summary)
    assert "secret/token" not in text
    assert "user:pass" not in text
    assert summary["notifications"]["serviceCount"] == 1
    assert summary["mqtt"]["brokerConfigured"] is True


@pytest.mark.unit
def test_remote_health_for_config_reads_sidecars(minimal_config, tmp_path):
    minimal_config.logging.state_file = str(tmp_path / "state")
    sidecar = remote_health_sidecar_path(
        state_file_path_for_group(minimal_config, minimal_config.ups_groups[0])
    )
    sidecar.write_text(json.dumps({
        "servers": [{
            "group": "Rack",
            "server": "nas",
            "host": "nas.example",
            "user": "ups",
            "status": "HEALTHY",
        }]
    }))

    rows = remote_health_for_config(minimal_config)

    assert rows[0]["server"] == "nas"
    assert rows[0]["status"] == "HEALTHY"


@pytest.mark.unit
def test_prometheus_metrics_include_eneru_specific_values(monitor):
    text = render_prometheus_metrics(monitor)
    assert "eneru_up 1" in text
    assert "eneru_ups_battery_charge" in text
    assert "eneru_ups_depletion_rate_percent_per_minute" in text
    assert 'eneru_ups_input_voltage{ups="TestUPS@localhost",label="TestUPS@localhost"} 229.4' in text
    assert 'eneru_ups_voltage_state{ups="TestUPS@localhost",label="TestUPS@localhost",state="NORMAL"} 1.0' in text


@pytest.mark.unit
def test_prometheus_metrics_emit_nan_for_missing_readings(monitor):
    monitor.state.latest_input_voltage = ""
    monitor.state.latest_ups_temperature = ""

    text = render_prometheus_metrics(monitor)

    assert 'eneru_ups_input_voltage{ups="TestUPS@localhost",label="TestUPS@localhost"} NaN' in text
    assert 'eneru_ups_temperature_celsius{ups="TestUPS@localhost",label="TestUPS@localhost"} NaN' in text


@pytest.mark.unit
def test_prometheus_metric_line_formats_inf_and_nan_per_spec():
    # Prometheus exposition is case-sensitive: parsers reject lowercase
    # ``inf``/``nan``. Python's f"{float(...)}" emits the lowercase
    # forms, so _metric_line must canonicalise to ``+Inf``/``-Inf``/``NaN``.
    from eneru.api import _metric_line

    overflow = _metric_line("test_metric", {"ups": "u"}, "1e500")
    underflow = _metric_line("test_metric", {"ups": "u"}, "-1e500")
    not_a_number = _metric_line("test_metric", {"ups": "u"}, float("nan"))

    assert overflow.endswith(" +Inf"), overflow
    assert underflow.endswith(" -Inf"), underflow
    assert not_a_number.endswith(" NaN"), not_a_number


@pytest.mark.unit
def test_prometheus_state_metrics_emit_one_series_per_label(monitor):
    text = render_prometheus_metrics(monitor)

    voltage_lines = [
        line for line in text.splitlines()
        if line.startswith("eneru_ups_voltage_state{")
    ]
    avr_lines = [
        line for line in text.splitlines()
        if line.startswith("eneru_ups_avr_state{")
    ]
    bypass_lines = [
        line for line in text.splitlines()
        if line.startswith("eneru_ups_bypass_state{")
    ]
    overload_lines = [
        line for line in text.splitlines()
        if line.startswith("eneru_ups_overload_state{")
    ]

    # Three voltage states (NORMAL/LOW/HIGH), three AVR (INACTIVE/BOOST/TRIM),
    # two bypass (INACTIVE/ACTIVE), two overload (INACTIVE/ACTIVE).
    assert len(voltage_lines) == 3
    assert len(avr_lines) == 3
    assert len(bypass_lines) == 2
    assert len(overload_lines) == 2

    # Exactly one active series per metric (the one matching current state).
    assert sum(line.endswith(" 1.0") for line in voltage_lines) == 1
    assert sum(line.endswith(" 0.0") for line in voltage_lines) == 2
    assert any('state="NORMAL"' in line and line.endswith(" 1.0")
               for line in voltage_lines)


@pytest.mark.unit
def test_prometheus_metrics_escape_label_values(monitor):
    monitor.config.ups.name = 'UPS"rack\\a'
    monitor.config.ups.display_name = "Line\nOne"

    text = render_prometheus_metrics(monitor)

    assert 'ups="UPS\\"rack\\\\a"' in text
    assert 'label="Line\\nOne"' in text


@pytest.mark.unit
def test_collect_status_includes_redundancy_groups(minimal_config):
    minimal_config.redundancy_groups = [
        RedundancyGroupConfig(name="rack-a", ups_sources=["TestUPS@localhost"])
    ]
    source = MagicMock()
    source.config = minimal_config
    source._monitors = []
    source._redundancy_remote_health_managers = []

    payload = collect_status(source)

    assert payload["redundancyGroups"][0]["groupId"] == "redundancy-rack-a"
    assert payload["redundancyGroups"][0]["name"] == "rack-a"


@pytest.mark.unit
def test_query_events_honors_verbosity(minimal_config, tmp_path):
    minimal_config.statistics.db_directory = str(tmp_path)
    store = StatsStore(tmp_path / "default.db")
    store.open()
    try:
        store.log_event("ON_BATTERY", "power", ts=100)
        store.log_event("SERVICE_STARTED", "start", ts=101)
        store.log_event("REMOTE_HEALTH_FAILED", "ssh", ts=102)
        store.log_event("SLOW_NUT_RESPONSE", "nut", ts=103)
        store.log_event("REMOTE_SSH_SLOW_RESPONSE", "slow ssh", ts=104)
    finally:
        store.close()

    power_only = query_events(minimal_config, verbosity=0)
    diagnostics = query_events(minimal_config, verbosity=1)
    all_events = query_events(minimal_config, verbosity=2)

    assert [row["eventType"] for row in power_only] == ["ON_BATTERY"]
    assert [row["eventType"] for row in diagnostics] == [
        "ON_BATTERY",
        "REMOTE_HEALTH_FAILED",
        "SLOW_NUT_RESPONSE",
        "REMOTE_SSH_SLOW_RESPONSE",
    ]
    assert [row["eventType"] for row in all_events] == [
        "ON_BATTERY",
        "SERVICE_STARTED",
        "REMOTE_HEALTH_FAILED",
        "SLOW_NUT_RESPONSE",
        "REMOTE_SSH_SLOW_RESPONSE",
    ]


@pytest.mark.unit
def test_query_events_applies_limit_in_sql(minimal_config, tmp_path):
    minimal_config.statistics.db_directory = str(tmp_path)
    store = StatsStore(tmp_path / "default.db")
    store.open()
    try:
        for idx in range(5):
            store.log_event("ON_BATTERY", f"power-{idx}", ts=100 + idx)
    finally:
        store.close()

    rows = query_events(minimal_config, limit=2, verbosity=0)

    assert [row["detail"] for row in rows] == ["power-3", "power-4"]


@pytest.mark.unit
def test_query_history_unknown_metric_is_bad_request_signal(minimal_config):
    assert query_history(minimal_config, "TestUPS@localhost", "watts", 0, 1) is None


@pytest.mark.unit
def test_api_events_route_accepts_verbosity(minimal_config, tmp_path):
    minimal_config.statistics.db_directory = str(tmp_path)
    store = StatsStore(tmp_path / "default.db")
    store.open()
    try:
        store.log_event("ON_BATTERY", "power", ts=100)
        store.log_event("REMOTE_HEALTH_FAILED", "ssh", ts=101)
    finally:
        store.close()
    handler = object.__new__(EneruAPIHandler)
    handler.path = "/api/v1/events?verbosity=0&limit=10"
    handler.api_config = minimal_config
    handler.api_source = MagicMock()
    status, content_type, payload = handler._route()

    assert status == 200
    assert content_type == "application/json"
    assert [row["eventType"] for row in payload["events"]] == ["ON_BATTERY"]


@pytest.mark.unit
def test_api_events_route_range_id_and_before_paging(minimal_config, tmp_path):
    minimal_config.statistics.db_directory = str(tmp_path)
    store = StatsStore(tmp_path / "default.db")
    store.open()
    try:
        store.log_event("A", "a", ts=1000)
        store.log_event("B", "b", ts=2000)   # three share 2000
        store.log_event("C", "c", ts=2000)
        store.log_event("D", "d", ts=2000)
        store.log_event("E", "e", ts=3000)
    finally:
        store.close()

    def route(query):
        h = object.__new__(EneruAPIHandler)
        h.path = "/api/v1/events?" + query
        h.api_config = minimal_config
        h.api_source = MagicMock()
        return h._route()

    # `from` bounds the window; rows carry source-qualified id + source.
    status, _, payload = route("from=2000&limit=10")
    evs = payload["events"]
    assert status == 200
    assert [e["eventType"] for e in evs] == ["B", "C", "D", "E"]
    assert all(isinstance(e["id"], int) and e["source"] for e in evs)

    # Timestamp-only `before` remains backward compatible: the boundary overlaps
    # and the client de-dups by (source,id).
    status, _, page1 = route("limit=2")
    p1 = page1["events"]
    assert [e["eventType"] for e in p1] == ["D", "E"]
    oldest = p1[0]
    status, _, page2 = route(f"limit=2&before={oldest['ts']}")
    assert [e["eventType"] for e in page2["events"]] == ["C", "D"]

    # Source-qualified cursor advances strictly inside a same-second cluster,
    # avoiding a stuck "Load older" loop when one second has more rows than a page.
    cursor = ("limit=2&before={ts}&beforeSource={source}&beforeId={event_id}"
              .format(ts=oldest["ts"], source=oldest["source"], event_id=oldest["id"]))
    status, _, page3 = route(cursor)
    assert [e["eventType"] for e in page3["events"]] == ["B", "C"]

    # from > to is a 400.
    status, _, err = route("from=3000&to=1000")
    assert status == 400 and err["error"]["code"] == "INVALID_REQUEST"

    # A non-integer `before` raises APIBadRequest (→ 400 via _dispatch),
    # consistent with from/to validation.
    from eneru.api import APIBadRequest
    with pytest.raises(APIBadRequest):
        route("before=not-a-cursor&limit=10")
    with pytest.raises(APIBadRequest):
        route("before=2000&beforeSource=default&limit=10")


@pytest.mark.unit
def test_api_history_rejects_from_after_to(minimal_config):
    h = object.__new__(EneruAPIHandler)
    h.path = "/api/v1/ups/TestUPS@localhost/history?metric=charge&from=200&to=100"
    h.api_config = minimal_config
    h.api_source = MagicMock()
    status, _, payload = h._route()
    assert status == 400
    assert payload["error"]["code"] == "INVALID_REQUEST"


@pytest.mark.unit
def test_api_history_all_clamps_to_retention_horizon(minimal_config, tmp_path):
    # Omitting `from` ("All") maps to the hourly-retention horizon, not 0.
    minimal_config.statistics.db_directory = str(tmp_path)
    minimal_config.statistics.retention.agg_hourly_days = 10
    store = StatsStore(tmp_path / "default.db")
    store.open()
    store.close()
    h = object.__new__(EneruAPIHandler)
    h.path = "/api/v1/ups/TestUPS@localhost/history?metric=charge&to=1000000"
    h.api_config = minimal_config
    h.api_source = MagicMock()
    status, _, payload = h._route()
    assert status == 200
    assert payload["from"] == 1000000 - 10 * 86400


@pytest.mark.unit
def test_api_core_routes(minimal_config, monitor):
    handler = object.__new__(EneruAPIHandler)
    handler.api_config = minimal_config
    handler.api_source = monitor

    handler.path = "/health"
    assert handler._route()[0] == 200

    handler.path = "/ready"
    status, _, payload = handler._route()
    assert status == 200
    assert payload["ready"] is True

    handler.path = "/api/v1/ups"
    status, _, payload = handler._route()
    assert status == 200
    assert payload["ups"][0]["name"] == "TestUPS@localhost"

    handler.path = "/api/v1"
    status, _, payload = handler._route()
    assert status == 200
    assert any(row["path"] == "/api/v1/events" for row in payload["endpoints"])
    events_ep = next(row for row in payload["endpoints"]
                     if row["path"] == "/api/v1/events")
    assert events_ep["query"]["from"] == "unix timestamp"
    assert events_ep["query"]["beforeSource"] == "source-qualified cursor source"

    handler.path = "/api/v1/ups/TestUPS-localhost"
    status, _, payload = handler._route()
    assert status == 200
    assert payload["groupId"] == "TestUPS-localhost"

    handler.path = "/api/v1/ups/missing"
    status, _, payload = handler._route()
    assert status == 404
    assert payload["error"]["code"] == "NOT_FOUND"
    assert any(row["path"] == "/api/v1/ups" for row in payload["availableEndpoints"])

    minimal_config.prometheus.enabled = False
    handler.path = "/metrics"
    status, _, payload = handler._route()
    assert status == 404
    assert payload["error"]["message"] == "Metrics disabled"
    advertised = {row["path"] for row in payload["availableEndpoints"]}
    assert "/metrics" not in advertised
    assert "/api/v1" in advertised
    assert "/api/v1/ups" in advertised


@pytest.mark.unit
def test_api_remote_health_route_uses_live_managers(minimal_config):
    manager = MagicMock()
    manager.snapshot.return_value = [{"server": "nas", "status": "HEALTHY"}]
    source = SimpleNamespace(_monitors=[SimpleNamespace(_remote_health_manager=manager)])
    handler = object.__new__(EneruAPIHandler)
    handler.path = "/api/v1/remote-health"
    handler.api_config = minimal_config
    handler.api_source = source

    status, _, payload = handler._route()

    assert status == 200
    assert payload["servers"] == [{"server": "nas", "status": "HEALTHY"}]


@pytest.mark.unit
def test_api_do_get_uses_whitelisted_content_type():
    handler = object.__new__(EneruAPIHandler)
    handler.headers = {"Host": "localhost"}  # F-016 dispatch host guard
    headers = []
    handler._route = lambda: (200, "text/plain\r\nX-Bad: injected", "ok")
    handler.send_response = lambda status: headers.append(("status", status))
    handler.send_header = lambda key, value: headers.append((key, value))
    handler.end_headers = lambda: None
    handler.wfile = BytesIO()

    handler.do_GET()

    assert ("Content-Type", "application/json; charset=utf-8") in headers
    assert handler.wfile.getvalue() == b"ok"


@pytest.mark.unit
def test_api_do_head_probe_sends_headers_only():
    """ISS-061: HEAD on /health and /ready returns the same headers (incl.
    Content-Length) but no body."""
    for path in ("/health", "/ready"):
        handler = object.__new__(EneruAPIHandler)
        handler.path = path
        handler.headers = {"Host": "localhost"}  # F-016 dispatch host guard
        handler._route = lambda: (200, "application/json", {"status": "ok"})
        headers = []
        handler.send_response = lambda status: headers.append(("status", status))
        handler.send_header = lambda key, value: headers.append((key, value))
        handler.end_headers = lambda: None
        handler.wfile = BytesIO()

        handler.do_HEAD()

        assert ("status", 200) in headers
        assert any(k == "Content-Length" for k, _ in headers)
        assert handler.wfile.getvalue() == b""  # HEAD carries no body


@pytest.mark.unit
def test_api_do_head_non_probe_path_405():
    """ISS-061: HEAD on a payload route is 405 (we don't ship a body-less 200)."""
    handler = object.__new__(EneruAPIHandler)
    handler.path = "/api/v1/ups"
    headers = []
    handler.send_response = lambda status: headers.append(("status", status))
    handler.send_header = lambda key, value: headers.append((key, value))
    handler.end_headers = lambda: None
    handler.wfile = BytesIO()

    handler.do_HEAD()

    assert ("status", 405) in headers
    assert handler.wfile.getvalue() == b""  # HEAD: still no body, even on 405


@pytest.mark.unit
def test_api_read_json_body_rejects_chunked():
    """ISS-061: a chunked Transfer-Encoding (no Content-Length) is rejected
    rather than mis-read as an empty body."""
    from eneru.api import APILengthRequired
    handler = object.__new__(EneruAPIHandler)
    handler.headers = {"Transfer-Encoding": "chunked"}
    handler.rfile = BytesIO(b"")
    with pytest.raises(APILengthRequired):
        handler._read_json_body()


@pytest.mark.unit
def test_api_dispatch_maps_length_required_to_411():
    """ISS-061: APILengthRequired surfaces as a 411 error envelope."""
    from eneru.api import APILengthRequired
    handler = object.__new__(EneruAPIHandler)
    handler.headers = {"Host": "localhost"}  # F-016 dispatch host guard

    def _boom():
        raise APILengthRequired("chunked not supported")

    headers = []
    handler.send_response = lambda status: headers.append(("status", status))
    handler.send_header = lambda key, value: None
    handler.end_headers = lambda: None
    handler.wfile = BytesIO()

    handler._dispatch(_boom)

    assert ("status", 411) in headers
    body = json.loads(handler.wfile.getvalue())
    assert body["error"]["code"] == "LENGTH_REQUIRED"


@pytest.mark.unit
@pytest.mark.parametrize(
    "path,message",
    [
        ("/api/v1/events?limit=bad", "limit must be an integer"),
        ("/api/v1/events?verbosity=9", "verbosity must be <= 2"),
        ("/api/v1/ups/TestUPS%40localhost/history?from=bad", "from must be an integer"),
    ],
)
def test_api_rejects_invalid_numeric_query_params(minimal_config, path, message):
    handler = object.__new__(EneruAPIHandler)
    handler.path = path
    handler.api_config = minimal_config
    handler.api_source = MagicMock()

    with pytest.raises(Exception) as exc_info:
        handler._route()

    assert message in str(exc_info.value)


@pytest.mark.unit
def test_api_history_rejects_unknown_metric(minimal_config):
    handler = object.__new__(EneruAPIHandler)
    handler.path = "/api/v1/ups/TestUPS%40localhost/history?metric=watts"
    handler.api_config = minimal_config
    handler.api_source = MagicMock()

    status, content_type, payload = handler._route()

    assert status == 400
    assert content_type == "application/json"
    assert payload["error"]["code"] == "INVALID_REQUEST"


@pytest.mark.unit
def test_api_index_omits_metrics_when_prometheus_disabled(minimal_config):
    minimal_config.prometheus.enabled = False
    handler = object.__new__(EneruAPIHandler)
    handler.path = "/api/v1"
    handler.api_config = minimal_config
    handler.api_source = MagicMock()

    status, content_type, payload = handler._route()

    assert status == 200
    assert content_type == "application/json"
    advertised = {row["path"] for row in payload["endpoints"]}
    assert "/metrics" not in advertised
    # The rest of the index is unchanged.
    assert "/health" in advertised
    assert "/api/v1" in advertised
    assert "/api/v1/events" in advertised


@pytest.mark.unit
def test_api_history_404_includes_endpoint_index(minimal_config):
    handler = object.__new__(EneruAPIHandler)
    handler.path = "/api/v1/ups/missing/history"
    handler.api_config = minimal_config
    handler.api_source = MagicMock()

    status, content_type, payload = handler._route()

    assert status == 404
    assert content_type == "application/json"
    assert payload["error"]["code"] == "NOT_FOUND"
    assert any(row["path"] == "/api/v1/ups/{name}/history"
               for row in payload["availableEndpoints"])


@pytest.mark.unit
def test_api_do_get_returns_400_on_api_bad_request(minimal_config):
    """When _route raises APIBadRequest, do_GET should send a 400 JSON
    response rather than propagating (api.py line 140)."""
    handler = object.__new__(EneruAPIHandler)
    handler.api_config = minimal_config
    handler.api_source = MagicMock()
    handler.headers = {"Host": "localhost"}  # F-016 dispatch host guard
    handler.path = "/api/v1/events?limit=abc"  # triggers APIBadRequest

    headers = []
    handler.send_response = lambda status: headers.append(("status", status))
    handler.send_header = lambda k, v: headers.append((k, v))
    handler.end_headers = lambda: None
    handler.wfile = BytesIO()

    handler.do_GET()
    assert ("status", 400) in headers
    body = json.loads(handler.wfile.getvalue())
    assert body["error"]["code"] == "INVALID_REQUEST"


@pytest.mark.unit
def test_api_do_get_returns_500_on_unexpected_exception(minimal_config):
    """An unexpected exception in _route should be caught and surfaced as
    a 500 INTERNAL_ERROR response (covers the broad except in do_GET)."""
    handler = object.__new__(EneruAPIHandler)
    handler.api_config = minimal_config
    handler.api_source = MagicMock()
    handler.headers = {"Host": "localhost"}  # F-016 dispatch host guard
    handler.path = "/api/v1/ups"

    def boom():
        raise RuntimeError("explode")

    handler._route = boom
    headers = []
    handler.send_response = lambda status: headers.append(("status", status))
    handler.send_header = lambda k, v: headers.append((k, v))
    handler.end_headers = lambda: None
    handler.wfile = BytesIO()

    handler.do_GET()
    assert ("status", 500) in headers
    body = json.loads(handler.wfile.getvalue())
    assert body["error"]["code"] == "INTERNAL_ERROR"


@pytest.mark.unit
def test_api_do_get_uses_text_plain_header_for_text_route(minimal_config, monitor):
    """When _route returns content_type 'text/plain' (literal), do_GET
    must emit 'text/plain; charset=utf-8' (api.py line 158)."""
    handler = object.__new__(EneruAPIHandler)
    handler.api_config = minimal_config
    handler.api_source = monitor
    handler.headers = {"Host": "localhost"}  # F-016 dispatch host guard
    handler._route = lambda: (200, "text/plain", "eneru_up 1\n")
    headers = []
    handler.send_response = lambda status: headers.append(("status", status))
    handler.send_header = lambda k, v: headers.append((k, v))
    handler.end_headers = lambda: None
    handler.wfile = BytesIO()

    handler.do_GET()
    assert ("Content-Type", "text/plain; charset=utf-8") in headers


@pytest.mark.unit
def test_api_handler_log_message_is_noop():
    """The overridden log_message must silently drop stdlib's BaseHTTPRequestHandler
    access-log spam (api.py line 168)."""
    handler = object.__new__(EneruAPIHandler)
    # Should not raise, not return anything truthy.
    assert handler.log_message("%s %d", "GET", 200) is None


@pytest.mark.unit
def test_api_metrics_route_returns_prometheus_text(minimal_config, monitor):
    """When prometheus is enabled, /metrics returns text/plain rendered
    metrics (api.py line 185)."""
    minimal_config.prometheus.enabled = True
    handler = object.__new__(EneruAPIHandler)
    handler.path = "/metrics"
    handler.api_config = minimal_config
    handler.api_source = monitor

    status, content_type, body = handler._route()
    assert status == 200
    assert content_type == "text/plain"
    assert "eneru_up" in body


@pytest.mark.unit
def test_api_config_route_returns_summary(minimal_config):
    """`/api/v1/config` returns config_summary (api.py line 232)."""
    handler = object.__new__(EneruAPIHandler)
    handler.path = "/api/v1/config"
    handler.api_config = minimal_config
    handler.api_source = MagicMock()

    status, content_type, payload = handler._route()
    assert status == 200
    assert content_type == "application/json"
    assert "notifications" in payload


@pytest.mark.unit
def test_api_history_route_returns_rows(minimal_config, monitor, monkeypatch):
    """A successful history query returns the rows payload (api.py
    line 215)."""
    monkeypatch.setattr(
        "eneru.api.query_history",
        lambda config, ups, metric, start, end: [
            {"ts": 1, "value": 90.0},
            {"ts": 2, "value": 89.0},
        ],
    )
    handler = object.__new__(EneruAPIHandler)
    handler.path = "/api/v1/ups/TestUPS%40localhost/history?metric=charge&from=0&to=10"
    handler.api_config = minimal_config
    handler.api_source = monitor

    status, _content_type, payload = handler._route()
    assert status == 200
    assert payload["ups"] == "TestUPS@localhost"
    assert payload["metric"] == "charge"
    assert payload["data"] == [
        {"ts": 1, "value": 90.0},
        {"ts": 2, "value": 89.0},
    ]


@pytest.mark.unit
def test_api_parse_int_param_rejects_below_minimum(minimal_config):
    """`?limit=0` violates the minimum=1 floor and raises
    APIBadRequest with the >= message (api.py line 283)."""
    from eneru.api import APIBadRequest, _parse_int_param
    with pytest.raises(APIBadRequest, match="limit must be >= 1"):
        _parse_int_param({"limit": ["0"]}, "limit", 100, minimum=1, maximum=10)


@pytest.mark.unit
def test_api_parse_int_param_default_none_returns_none_when_absent():
    """ISS-029: default=None with an absent param returns None, not int('None')
    (which would raise a spurious 400)."""
    from eneru.api import _parse_int_param
    assert _parse_int_param({}, "before", None) is None
    # Present value still parses normally.
    assert _parse_int_param({"before": ["42"]}, "before", None) == 42


@pytest.mark.unit
def test_api_prometheus_metrics_include_remote_health_per_ups(minimal_config, monitor):
    """When a UPS has a remote-health manager exposing servers, the
    metrics endpoint must emit per-server status + consecutive_failures
    series (api.py lines 463-473)."""
    minimal_config.prometheus.enabled = True
    # Inject a remote-health manager so collect_status reports a server.
    manager = MagicMock()
    manager.snapshot.return_value = [{
        "group": "TestUPS@localhost",
        "server": "nas-01",
        "host": "nas-01.lan",
        "user": "ups",
        "status": "HEALTHY",
        "consecutive_failures": 0,
    }]
    monitor._remote_health_manager = manager

    text = render_prometheus_metrics(monitor)
    assert "eneru_remote_health_status" in text
    assert 'server="nas-01"' in text
    assert "eneru_remote_health_consecutive_failures" in text


@pytest.mark.unit
def test_json_formatter_adds_group_and_redacts_secrets():
    formatter = JSONFormatter()
    record = logging.LogRecord(
        "ups-monitor",
        logging.INFO,
        __file__,
        1,
        "[Rack A] MQTT broker mqtt://user:secret@example:1883 token=abc123",
        (),
        None,
    )

    payload = json.loads(formatter.format(record))

    assert payload["group"] == "Rack A"
    assert payload["message"].startswith("MQTT broker")
    assert "secret" not in json.dumps(payload)
    assert "abc123" not in json.dumps(payload)


@pytest.mark.unit
def test_timezone_formatter_also_redacts_secrets():
    """ISS-063: the plain-text formatter (journal/stdout/file) must redact
    credentials too, not just the JSON one."""
    from eneru.logger import TimezoneFormatter
    formatter = TimezoneFormatter("%(message)s")
    record = logging.LogRecord(
        "ups-monitor", logging.INFO, __file__, 1,
        "MQTT broker mqtt://user:secret@example:1883 token=abc123",
        (), None,
    )
    out = formatter.format(record)
    assert "secret" not in out
    assert "abc123" not in out


@pytest.mark.unit
def test_mqtt_publishes_on_state_change_without_optional_dependency(monkeypatch):
    published = []

    class FakeClient:
        def connect(self, *args, **kwargs):
            return None

        def loop_start(self):
            return None

        def publish(self, topic, payload, qos=0, retain=False):
            published.append(json.loads(payload))

        def loop_stop(self):
            return None

        def disconnect(self):
            return None

    class FakeStopEvent:
        def __init__(self):
            self.waits = 0

        def is_set(self):
            return False

        def wait(self, timeout):
            self.waits += 1
            return self.waits >= 3

    statuses = [
        {"generatedAt": 1, "ups": [{"status": "OL"}]},
        {"generatedAt": 2, "ups": [{"status": "OB"}]},
        {"generatedAt": 3, "ups": [{"status": "OB"}]},
    ]
    config = Config()
    config.mqtt.enabled = True
    config.mqtt.broker = "mqtt://localhost:1883"
    # Keep publish_interval high so the periodic-publish branch
    # doesn't fire — we want to assert state-change semantics here.
    config.mqtt.publish_interval = 99
    monkeypatch.setattr("eneru.mqtt.MQTT_AVAILABLE", True)
    monkeypatch.setattr(
        "eneru.mqtt.mqtt_client",
        SimpleNamespace(Client=lambda **kwargs: FakeClient()),
    )
    # Freeze monotonic so the rate-limit check at line 153 sees enough
    # elapsed time on iteration 2 to allow a second collect (the test
    # fixture's wait() doesn't advance real time).
    fake_clock = [0.0]

    def fake_monotonic():
        fake_clock[0] += 5.0
        return fake_clock[0]

    monkeypatch.setattr("eneru.mqtt.time.monotonic", fake_monotonic)
    monkeypatch.setattr("eneru.mqtt.collect_status", lambda source: statuses.pop(0))

    MQTTPublisher(object(), config, FakeStopEvent())._run()

    assert [row["ups"][0]["status"] for row in published] == ["OL", "OB"]


@pytest.mark.unit
def test_mqtt_uses_credentials_and_paho_v2_callback_api(monkeypatch):
    calls = {}

    class FakeClient:
        def username_pw_set(self, username, password=None):
            calls["username"] = username
            calls["password"] = password

        def connect(self, host, port, keepalive=30):
            calls["host"] = host
            calls["port"] = port

        def loop_start(self):
            return None

        def publish(self, *args, **kwargs):
            return None

        def loop_stop(self):
            return None

        def disconnect(self):
            return None

    class FakeStopEvent:
        def is_set(self):
            return False

        def wait(self, timeout):
            return True

    def client_factory(**kwargs):
        calls["callback_api_version"] = kwargs.get("callback_api_version")
        return FakeClient()

    config = Config()
    config.mqtt.enabled = True
    config.mqtt.broker = "mqtt://user:p%40ss@example.test:1884"
    monkeypatch.setattr("eneru.mqtt.MQTT_AVAILABLE", True)
    monkeypatch.setattr(
        "eneru.mqtt.mqtt_client",
        SimpleNamespace(
            CallbackAPIVersion=SimpleNamespace(VERSION1=1),
            Client=client_factory,
        ),
    )
    monkeypatch.setattr("eneru.mqtt.collect_status", lambda source: {"generatedAt": 1})

    MQTTPublisher(object(), config, FakeStopEvent())._run()

    assert calls == {
        "callback_api_version": 1,
        "username": "user",
        "password": "p@ss",
        "host": "example.test",
        "port": 1884,
    }


@pytest.mark.unit
def test_mqtt_disconnect_failures_are_logged(monkeypatch):
    logs = []

    class FakeClient:
        def connect(self, *args, **kwargs):
            return None

        def loop_start(self):
            return None

        def publish(self, *args, **kwargs):
            return None

        def loop_stop(self):
            raise RuntimeError("loop gone")

        def disconnect(self):
            return None

    class FakeStopEvent:
        def is_set(self):
            return False

        def wait(self, timeout):
            return True

    config = Config()
    config.mqtt.enabled = True
    config.mqtt.broker = "mqtt://localhost:1883"
    monkeypatch.setattr("eneru.mqtt.MQTT_AVAILABLE", True)
    monkeypatch.setattr(
        "eneru.mqtt.mqtt_client",
        SimpleNamespace(Client=lambda **kwargs: FakeClient()),
    )
    monkeypatch.setattr("eneru.mqtt.collect_status", lambda source: {"generatedAt": 1})

    MQTTPublisher(object(), config, FakeStopEvent(), log_fn=logs.append)._run()

    assert any("disconnect failed" in line for line in logs)


@pytest.mark.unit
def test_mqtt_enables_tls_for_mqtts_scheme(monkeypatch):
    """mqtts:// URLs must call client.tls_set() and default port 8883."""
    calls = {}

    class FakeClient:
        def tls_set(self, *args, **kwargs):
            calls["tls_set"] = True

        def connect(self, host, port, keepalive=30):
            calls["host"] = host
            calls["port"] = port

        def loop_start(self):
            return None

        def publish(self, *args, **kwargs):
            return SimpleNamespace(rc=0)

        def loop_stop(self):
            return None

        def disconnect(self):
            return None

    class FakeStopEvent:
        def is_set(self):
            return False

        def wait(self, timeout):
            return True

    config = Config()
    config.mqtt.enabled = True
    # No port in URL — should default to 8883 for mqtts.
    config.mqtt.broker = "mqtts://broker.example"
    monkeypatch.setattr("eneru.mqtt.MQTT_AVAILABLE", True)
    monkeypatch.setattr(
        "eneru.mqtt.mqtt_client",
        SimpleNamespace(Client=lambda **kwargs: FakeClient()),
    )
    monkeypatch.setattr("eneru.mqtt.collect_status", lambda source: {"generatedAt": 1})

    MQTTPublisher(object(), config, FakeStopEvent())._run()

    assert calls.get("tls_set") is True
    assert calls.get("port") == 8883
    assert calls.get("host") == "broker.example"


@pytest.mark.unit
def test_mqtt_does_not_enable_tls_for_plain_mqtt_scheme(monkeypatch):
    """Plaintext mqtt:// must NOT call tls_set."""
    calls = {"tls_set": False}

    class FakeClient:
        def tls_set(self, *args, **kwargs):
            calls["tls_set"] = True

        def connect(self, *args, **kwargs):
            return None

        def loop_start(self):
            return None

        def publish(self, *args, **kwargs):
            return SimpleNamespace(rc=0)

        def loop_stop(self):
            return None

        def disconnect(self):
            return None

    class FakeStopEvent:
        def is_set(self):
            return False

        def wait(self, timeout):
            return True

    config = Config()
    config.mqtt.enabled = True
    config.mqtt.broker = "mqtt://broker.example:1883"
    monkeypatch.setattr("eneru.mqtt.MQTT_AVAILABLE", True)
    monkeypatch.setattr(
        "eneru.mqtt.mqtt_client",
        SimpleNamespace(Client=lambda **kwargs: FakeClient()),
    )
    monkeypatch.setattr("eneru.mqtt.collect_status", lambda source: {"generatedAt": 1})

    MQTTPublisher(object(), config, FakeStopEvent())._run()

    assert calls["tls_set"] is False


@pytest.mark.unit
def test_redact_broker_strips_userinfo():
    """F-017: the redactor covers user:pass@ regardless of scheme presence."""
    from eneru.mqtt import _redact_broker
    assert _redact_broker("alice:s3cret@host:8883") == "***@host:8883"
    assert _redact_broker("mqtt://alice:s3cret@host:1883") == "mqtt://***@host:1883"
    assert _redact_broker("host:1883") == "host:1883"          # nothing to redact
    assert _redact_broker("") == ""


@pytest.mark.unit
def test_mqtt_schemeless_broker_warning_redacts_userinfo(monkeypatch):
    """F-017: the scheme-less warning must not echo the broker password."""
    logs = []

    class StopNow:
        def is_set(self):
            return True

        def wait(self, timeout):
            return True

    config = Config()
    config.mqtt.enabled = True
    config.mqtt.broker = "alice:s3cret@broker.example:8883"  # no mqtt:// scheme
    monkeypatch.setattr("eneru.mqtt.MQTT_AVAILABLE", True)
    pub = MQTTPublisher(object(), config, StopNow(), log_fn=logs.append)

    assert pub._connect_with_backoff() is None  # stopped, never connects
    warn = [m for m in logs if "no mqtt://" in m]
    assert warn, logs
    assert "s3cret" not in warn[0]
    assert "***@broker.example:8883" in warn[0]


@pytest.mark.unit
def test_mqtt_cleartext_username_warns_once(monkeypatch):
    """F-017: a username on a non-TLS broker warns exactly once, not per
    reconnect attempt."""
    logs = []

    class StopNow:
        def is_set(self):
            return True

        def wait(self, timeout):
            return True

    config = Config()
    config.mqtt.enabled = True
    config.mqtt.broker = "mqtt://user:pass@host:1883"  # creds, no TLS
    monkeypatch.setattr("eneru.mqtt.MQTT_AVAILABLE", True)
    pub = MQTTPublisher(object(), config, StopNow(), log_fn=logs.append)

    pub._connect_with_backoff()
    pub._connect_with_backoff()  # a "reconnect": must not warn again

    warns = [m for m in logs if "cleartext" in m]
    assert len(warns) == 1, logs


@pytest.mark.unit
def test_mqtt_retries_with_backoff_until_connect_succeeds(monkeypatch):
    """A failing connect must retry; stop_event.wait short-circuits backoff."""
    connect_attempts = []
    backoff_waits = []

    class FailThenSucceedClient:
        """First connect raises; subsequent ones succeed."""
        instances = 0

        def __init__(self):
            FailThenSucceedClient.instances += 1
            self._idx = FailThenSucceedClient.instances

        def connect(self, host, port, keepalive=30):
            connect_attempts.append(self._idx)
            if self._idx <= 2:
                raise OSError(f"fake refused #{self._idx}")

        def loop_start(self):
            return None

        def publish(self, *args, **kwargs):
            return SimpleNamespace(rc=0)

        def loop_stop(self):
            return None

        def disconnect(self):
            return None

    class FakeStopEvent:
        """Records every wait() call (backoff sleeps go through here)."""
        def __init__(self):
            self._publish_waits = 0

        def is_set(self):
            return False

        def wait(self, timeout):
            backoff_waits.append(timeout)
            # Let publish loop's wait() exit the test on first hit
            # AFTER we've successfully connected. Backoff waits return
            # False so we keep retrying.
            if timeout >= 1.0 and len(connect_attempts) >= 3:
                # publish-loop wait(1) — exit
                return True
            return False

    config = Config()
    config.mqtt.enabled = True
    config.mqtt.broker = "mqtt://broker.example:1883"
    monkeypatch.setattr("eneru.mqtt.MQTT_AVAILABLE", True)
    monkeypatch.setattr(
        "eneru.mqtt.mqtt_client",
        SimpleNamespace(Client=lambda **kwargs: FailThenSucceedClient()),
    )
    monkeypatch.setattr("eneru.mqtt.collect_status", lambda source: {"generatedAt": 1})

    MQTTPublisher(object(), config, FakeStopEvent())._run()

    # Two failed connects + one successful connect = 3 attempts.
    assert connect_attempts == [1, 2, 3]
    # First two backoff sleeps follow the exponential schedule.
    assert backoff_waits[0] == 1.0
    assert backoff_waits[1] == 2.0


@pytest.mark.unit
def test_mqtt_on_disconnect_during_connect_does_not_force_extra_reconnect(monkeypatch):
    """on_disconnect firing while we're still inside connect()/loop_start()
    must not cause the publish loop to immediately bail and reconnect.

    Regression guard for a race where attaching on_disconnect BEFORE
    loop_start() caused paho's background thread to set
    _needs_reconnect against a not-yet-fully-constructed publisher
    state — every clean stop then triggered one spurious reconnect
    cycle on the way out.
    """
    publisher_holder = {}
    publish_count = {"n": 0}

    class RaceClient:
        """Calls on_disconnect (if attached) inside its own connect()."""
        on_disconnect = None

        def tls_set(self, *args, **kwargs):
            return None

        def username_pw_set(self, *args, **kwargs):
            return None

        def connect(self, host, port, keepalive=30):
            # Simulate paho's background thread firing on_disconnect
            # in the same window where the outer code is constructing
            # the publisher state.
            cb = self.on_disconnect
            if cb is not None:
                cb(self, None, 0)

        def loop_start(self):
            return None

        def publish(self, *args, **kwargs):
            publish_count["n"] += 1
            return SimpleNamespace(rc=0)

        def loop_stop(self):
            return None

        def disconnect(self):
            return None

    class StopOnFirstWait:
        def __init__(self):
            self._calls = 0

        def is_set(self):
            return False

        def wait(self, timeout):
            self._calls += 1
            # Let the publish loop run one tick, then exit.
            return self._calls >= 1

    config = Config()
    config.mqtt.enabled = True
    config.mqtt.broker = "mqtt://broker.example:1883"
    monkeypatch.setattr("eneru.mqtt.MQTT_AVAILABLE", True)
    monkeypatch.setattr(
        "eneru.mqtt.mqtt_client",
        SimpleNamespace(Client=lambda **kwargs: RaceClient()),
    )
    monkeypatch.setattr("eneru.mqtt.collect_status", lambda source: {"generatedAt": 1})

    publisher = MQTTPublisher(object(), config, StopOnFirstWait())
    publisher_holder["p"] = publisher
    publisher._run()

    # The publisher should have published exactly once and exited
    # cleanly. If on_disconnect had spuriously set _needs_reconnect
    # during the connect window, _run would have looped back and
    # tried to reconnect — that's the regression we're guarding.
    assert publish_count["n"] == 1


@pytest.mark.unit
def test_mqtt_local_stop_does_not_set_global_stop_event(monkeypatch):
    """MQTTPublisher.stop() must not signal the daemon-wide stop event.

    Otherwise calling stop() on the publisher (config reload, test
    teardown, future "bounce only MQTT" code paths) would force the
    rest of the daemon to shut down too.
    """
    class FakeClient:
        def connect(self, *args, **kwargs):
            return None

        def loop_start(self):
            return None

        def publish(self, *args, **kwargs):
            return SimpleNamespace(rc=0)

        def loop_stop(self):
            return None

        def disconnect(self):
            return None

    config = Config()
    config.mqtt.enabled = True
    config.mqtt.broker = "mqtt://broker.example:1883"
    monkeypatch.setattr("eneru.mqtt.MQTT_AVAILABLE", True)
    monkeypatch.setattr(
        "eneru.mqtt.mqtt_client",
        SimpleNamespace(Client=lambda **kwargs: FakeClient()),
    )

    import threading
    daemon_stop = threading.Event()
    publisher = MQTTPublisher(object(), config, daemon_stop)
    # No background thread started — call stop() directly.
    publisher.stop(timeout=0)

    assert not daemon_stop.is_set(), (
        "publisher.stop() must not set the daemon-wide stop_event"
    )


@pytest.mark.unit
def test_mqtt_start_creates_daemon_thread(monkeypatch):
    """start() owns only publisher lifecycle: it creates the MQTT thread
    without running the target inline."""
    created = []

    class FakeThread:
        def __init__(self, *, target, name, daemon):
            self.target = target
            self.name = name
            self.daemon = daemon
            self.started = False
            created.append(self)

        def start(self):
            self.started = True

    config = Config()
    config.mqtt.enabled = True
    monkeypatch.setattr("eneru.mqtt.MQTT_AVAILABLE", True)
    monkeypatch.setattr("eneru.mqtt.threading.Thread", FakeThread)

    publisher = MQTTPublisher(object(), config, stop_event=MagicMock())
    publisher.start()
    publisher.start()

    assert len(created) == 1
    assert created[0].target == publisher._run
    assert created[0].name == "eneru-mqtt"
    assert created[0].daemon is True
    assert created[0].started is True
    assert publisher._thread is created[0]


@pytest.mark.unit
def test_mqtt_stop_leaves_alive_thread_reference():
    """If join times out, keep the thread reference so a later stop can
    observe and join the same worker."""
    class AliveThread:
        def __init__(self):
            self.join_timeout = None

        def join(self, timeout=None):
            self.join_timeout = timeout

        def is_alive(self):
            return True

    config = Config()
    publisher = MQTTPublisher(object(), config, stop_event=MagicMock())
    thread = AliveThread()
    publisher._thread = thread

    publisher.stop(timeout=7)

    assert publisher._local_stop.is_set()
    assert thread.join_timeout == 7
    assert publisher._thread is thread


@pytest.mark.unit
def test_mqtt_backoff_exits_when_stop_event_short_circuits(monkeypatch):
    """stop_event.wait returning True during backoff must exit cleanly."""
    connect_attempts = []

    class AlwaysFailClient:
        def connect(self, *args, **kwargs):
            connect_attempts.append(1)
            raise OSError("never reachable")

        def loop_start(self):
            return None

        def loop_stop(self):
            return None

        def disconnect(self):
            return None

    class StopOnFirstWait:
        def is_set(self):
            return False

        def wait(self, timeout):
            return True  # short-circuit immediately

    config = Config()
    config.mqtt.enabled = True
    config.mqtt.broker = "mqtt://broker.example:1883"
    monkeypatch.setattr("eneru.mqtt.MQTT_AVAILABLE", True)
    monkeypatch.setattr(
        "eneru.mqtt.mqtt_client",
        SimpleNamespace(Client=lambda **kwargs: AlwaysFailClient()),
    )

    MQTTPublisher(object(), config, StopOnFirstWait())._run()

    # Exactly one connect attempt — stop_event short-circuited the
    # backoff before a retry.
    assert connect_attempts == [1]


@pytest.mark.unit
def test_json_formatter_uses_structured_extras_over_message_text():
    """Structured extra={...} fields take precedence over message-text heuristics."""
    formatter = JSONFormatter()
    record = logging.LogRecord(
        "ups-monitor",
        logging.INFO,
        __file__,
        1,
        "no recognisable structure here",
        (),
        None,
    )
    # Mimic logging's handling of `extra={...}` — it sets attributes
    # on the LogRecord directly.
    record.category = "shutdown"
    record.event_type = "FSD"
    record.group = "Rack-A"
    record.ups = "ups-01"

    payload = json.loads(formatter.format(record))

    assert payload["category"] == "shutdown"
    assert payload["event_type"] == "FSD"
    assert payload["group"] == "Rack-A"
    assert payload["ups"] == "ups-01"
    # Message is preserved verbatim when no heuristic group prefix.
    assert payload["message"] == "no recognisable structure here"


@pytest.mark.unit
def test_json_formatter_extras_win_over_heuristics():
    """If extras are present they override the heuristic group/category."""
    formatter = JSONFormatter()
    record = logging.LogRecord(
        "ups-monitor",
        logging.INFO,
        __file__,
        1,
        "[Wrong-Group] ⚡  POWER EVENT: OL - back",
        (),
        None,
    )
    record.group = "Right-Group"
    record.category = "lifecycle"

    payload = json.loads(formatter.format(record))

    assert payload["group"] == "Right-Group"
    assert payload["category"] == "lifecycle"


# ====================================================================
# EneruAPIServer lifecycle tests
# ====================================================================


@pytest.mark.unit
def test_api_server_start_no_op_when_disabled(minimal_config):
    from eneru.api import EneruAPIServer

    minimal_config.api.enabled = False
    server = EneruAPIServer(MagicMock(), minimal_config)
    server.start()
    assert server._thread is None
    assert server._httpd is None


@pytest.mark.unit
def test_api_server_start_is_idempotent_when_already_running(minimal_config):
    from unittest.mock import patch
    from eneru.api import EneruAPIServer

    minimal_config.api.enabled = True
    server = EneruAPIServer(MagicMock(), minimal_config)

    with patch("eneru.api._BoundedThreadingHTTPServer") as fake_server:
        fake_server.return_value = MagicMock()
        server.start()
        first_thread = server._thread
        # Second call must early-return without re-binding
        server.start()
        assert server._thread is first_thread
        assert fake_server.call_count == 1
    server.stop()


@pytest.mark.unit
def test_handler_has_read_timeout():
    """ISS-007: the handler must carry a socket read timeout so header/request-
    line reads are bounded (else a stalled client hangs server shutdown)."""
    from eneru.api import REQUEST_READ_TIMEOUT_SECONDS
    assert EneruAPIHandler.timeout == REQUEST_READ_TIMEOUT_SECONDS
    assert EneruAPIHandler.timeout is not None


@pytest.mark.unit
@pytest.mark.timeout(30)  # a regression would HANG stop(); fail fast instead
def test_stalled_connection_does_not_hang_stop(minimal_config):
    """ISS-007: a client that connects and never sends a request line must not
    block EneruAPIServer.stop() indefinitely; the handler timeout lets the
    non-daemon worker exit so server_close()'s join completes."""
    import socket as _socket
    from eneru.api import EneruAPIServer

    minimal_config.api.enabled = True
    minimal_config.api.bind = "127.0.0.1"
    minimal_config.api.port = 0  # OS-assigned ephemeral port
    logs = []
    server = EneruAPIServer(MagicMock(), minimal_config, log_fn=logs.append)
    elapsed = None
    with patch.object(EneruAPIHandler, "timeout", 0.5):
        server.start()
        assert server._httpd is not None, logs
        host, port = server._httpd.server_address[:2]
        sock = _socket.create_connection((host, port), timeout=2)
        try:
            time.sleep(0.1)  # let the worker accept and block on the read
            start = time.monotonic()
            server.stop()
            elapsed = time.monotonic() - start
        finally:
            sock.close()
    # stop() must return within a few timeout windows, not hang forever.
    assert elapsed is not None and elapsed < 5


@pytest.mark.unit
def test_handler_uses_http11_keepalive():
    """F-046: the handler must default to HTTP/1.1 so a browser/Prometheus
    client can reuse one TCP connection for a whole dashboard poll instead of
    the stdlib-default HTTP/1.0 connection-per-request."""
    assert EneruAPIHandler.protocol_version == "HTTP/1.1"


@pytest.mark.unit
@pytest.mark.timeout(30)
def test_concurrent_active_request_bound_enforced():
    """F-018: at most MAX_CONCURRENT_REQUESTS requests are PROCESSED at once;
    the (N+1)th blocks on the semaphore (queues) rather than running unbounded.

    Drive _dispatch directly on several threads with a slow router and a tiny
    (size-2) semaphore, and assert active processing never exceeds the bound and
    the 3rd request is queued while the first two run."""
    import threading as _t
    from eneru.api import EneruAPIHandler as _H, MAX_CONCURRENT_REQUESTS

    assert MAX_CONCURRENT_REQUESTS == 32  # named constant, not a magic literal

    small = _t.BoundedSemaphore(2)
    active = []
    active_lock = _t.Lock()
    peak = [0]
    entered = _t.Semaphore(0)
    release = _t.Event()

    def slow_router():
        with active_lock:
            active.append(1)
            peak[0] = max(peak[0], len(active))
        entered.release()
        release.wait(10)
        with active_lock:
            active.pop()
        return (200, "application/json", {})

    def run_one():
        h = object.__new__(_H)
        h._host_allowed = lambda: True
        h._finish = lambda *a, **k: None
        h._dispatch(slow_router)

    with patch.object(_H, "_request_semaphore", small):
        threads = [_t.Thread(target=run_one) for _ in range(3)]
        for t in threads:
            t.start()
        # Two routers should enter; the third must be blocked on the semaphore.
        assert entered.acquire(timeout=3)
        assert entered.acquire(timeout=3)
        time.sleep(0.3)  # give the (wrongly-admitted) 3rd a chance to enter
        with active_lock:
            assert len(active) == 2       # bound holds — 3rd is queued
        release.set()
        for t in threads:
            t.join(10)
    assert peak[0] == 2                    # active processing never exceeded 2


@pytest.mark.unit
def test_bounded_server_refuses_connection_when_saturated():
    """cubic P1 (round 1): the request semaphore bounds PROCESSING, but the
    stock ThreadingHTTPServer spawned one thread per accepted CONNECTION before
    that gate — a flood piled up blocked threads without limit. Saturated, the
    bounded server must close the socket at accept time WITHOUT spawning a
    thread."""
    import threading as _t
    from http.server import ThreadingHTTPServer as _Base
    from eneru.api import (
        _BoundedThreadingHTTPServer, MAX_CONCURRENT_CONNECTIONS,
    )

    assert _BoundedThreadingHTTPServer.max_connections == \
        MAX_CONCURRENT_CONNECTIONS

    srv = object.__new__(_BoundedThreadingHTTPServer)
    srv._connection_slots = _t.BoundedSemaphore(1)
    assert srv._connection_slots.acquire(blocking=False)  # saturate the cap

    closed = []
    srv.shutdown_request = closed.append
    with patch.object(_Base, "process_request",
                      side_effect=AssertionError("must not spawn a thread")):
        srv.process_request("sock", ("192.0.2.1", 12345))
    assert closed == ["sock"]


@pytest.mark.unit
def test_bounded_server_releases_slot_exactly_once():
    """The connection slot is returned when the worker thread finishes — and
    on the thread-creation failure path — never twice (BoundedSemaphore would
    raise on over-release)."""
    import threading as _t
    from http.server import ThreadingHTTPServer as _Base
    from eneru.api import _BoundedThreadingHTTPServer

    srv = object.__new__(_BoundedThreadingHTTPServer)
    srv._connection_slots = _t.BoundedSemaphore(1)

    # Normal path: slot held during the handler thread, released after.
    assert srv._connection_slots.acquire(blocking=False)
    with patch.object(_Base, "process_request_thread", return_value=None):
        srv.process_request_thread("sock", ("192.0.2.1", 1))
    assert srv._connection_slots.acquire(blocking=False)  # slot came back
    srv._connection_slots.release()

    # Thread-creation failure path: process_request re-raises but must not
    # leak the slot it acquired.
    with patch.object(_Base, "process_request",
                      side_effect=RuntimeError("thread spawn failed")):
        with pytest.raises(RuntimeError):
            srv.process_request("sock", ("192.0.2.1", 2))
    assert srv._connection_slots.acquire(blocking=False)  # not leaked
    srv._connection_slots.release()


@pytest.mark.unit
def test_api_server_uses_bounded_server_class(minimal_config):
    """The live server construction must go through the bounded subclass —
    a regression back to plain ThreadingHTTPServer reopens the flood hole."""
    import inspect
    from eneru import api as api_mod
    src = inspect.getsource(api_mod.EneruAPIServer.start)
    assert "_BoundedThreadingHTTPServer(" in src


@pytest.mark.unit
@pytest.mark.timeout(30)
def test_idle_keepalive_connection_closed_after_timeout(minimal_config):
    """F-046 interaction: an idle HTTP/1.1 keep-alive connection must be dropped
    after the per-connection idle timeout so its worker thread returns to the
    pool (a bounded pool + keep-alive would otherwise let idle clients starve
    the API — the slowloris trap). Prove the server closes an idle keep-alive:
    after one request the socket goes quiet and the server hangs it up (recv
    returns EOF) within a few idle windows."""
    import http.client as _http
    from eneru.api import EneruAPIServer

    minimal_config.api.enabled = True
    minimal_config.api.bind = "127.0.0.1"
    minimal_config.api.port = 0
    logs = []
    server = EneruAPIServer(MagicMock(), minimal_config, log_fn=logs.append)
    with patch.object(EneruAPIHandler, "timeout", 0.5):
        server.start()
        try:
            assert server._httpd is not None, logs
            host, port = server._httpd.server_address[:2]
            conn = _http.HTTPConnection(host, port, timeout=5)
            conn.request("GET", "/health")
            resp = conn.getresponse()
            resp.read()             # consume the body; keep-alive keeps it open
            assert resp.version == 11    # HTTP/1.1 on the wire (F-046)
            sock = conn.sock
            sock.settimeout(4)      # comfortably longer than the 0.5s idle bound
            # The server drops the idle keep-alive: recv() returns b"" (EOF).
            assert sock.recv(1024) == b""
            conn.close()
        finally:
            server.stop()


@pytest.mark.unit
def test_api_server_start_logs_bind_failure_and_returns(minimal_config):
    from unittest.mock import patch
    from eneru.api import EneruAPIServer

    minimal_config.api.enabled = True
    minimal_config.api.bind = "127.0.0.1"
    minimal_config.api.port = 9191
    log = []
    server = EneruAPIServer(MagicMock(), minimal_config, log_fn=log.append)

    with patch("eneru.api._BoundedThreadingHTTPServer", side_effect=OSError("port in use")):
        server.start()

    assert server._httpd is None
    assert server._thread is None
    assert any("API server failed to bind" in m for m in log), log


@pytest.mark.unit
def test_api_server_start_notes_auth_off_on_non_loopback_bind(minimal_config):
    from unittest.mock import patch
    from eneru.api import EneruAPIServer

    minimal_config.api.enabled = True
    minimal_config.api.bind = "0.0.0.0"
    log = []
    server = EneruAPIServer(MagicMock(), minimal_config, log_fn=log.append)

    with patch("eneru.api._BoundedThreadingHTTPServer", return_value=MagicMock()):
        server.start()

    try:
        # The bind announcement comes first, then the auth-off note. With auth
        # disabled, writes are closed and config is sanitized; reads may still
        # need a trusted network boundary. Assert the relative order so the
        # ordering contract is actually enforced, not just message presence.
        bind_idx = next(
            (i for i, m in enumerate(log)
             if "API server listening on 0.0.0.0" in m),
            None,
        )
        note_idx = next(
            (i for i, m in enumerate(log)
             if "API bound to 0.0.0.0" in m
             and "auth disabled" in m
             and "Write endpoints are disabled" in m
             and "/api/v1/config returns a sanitized view" in m),
            None,
        )
        assert bind_idx is not None, log
        assert note_idx is not None, log
        assert bind_idx < note_idx, log
        # ISS-009: a non-loopback bind must warn about cleartext transport.
        assert any(
            "PLAIN HTTP" in m and "unencrypted" in m and "TLS reverse proxy" in m
            for m in log
        ), log
    finally:
        server.stop()


@pytest.mark.unit
def test_api_no_cleartext_warning_on_loopback_bind(minimal_config):
    """ISS-009: the cleartext-transport warning fires ONLY for non-loopback binds."""
    from unittest.mock import patch
    from eneru.api import EneruAPIServer

    minimal_config.api.enabled = True
    minimal_config.api.bind = "127.0.0.1"
    log = []
    server = EneruAPIServer(MagicMock(), minimal_config, log_fn=log.append)
    with patch("eneru.api._BoundedThreadingHTTPServer", return_value=MagicMock()):
        server.start()
    try:
        assert not any("PLAIN HTTP" in m for m in log), log
    finally:
        server.stop()


@pytest.mark.unit
def test_api_server_start_notes_auth_off_on_loopback(minimal_config):
    from unittest.mock import patch
    from eneru.api import EneruAPIServer

    # Loopback bind + auth disabled: no off-loopback warning, but a clear notice
    # that Sign-in is hidden and how to enable login — so the hidden button is
    # not a mystery.
    minimal_config.api.enabled = True
    minimal_config.api.bind = "127.0.0.1"
    log = []
    server = EneruAPIServer(MagicMock(), minimal_config, log_fn=log.append)

    with patch("eneru.api._BoundedThreadingHTTPServer", return_value=MagicMock()):
        server.start()

    try:
        assert any(
            "authentication is disabled" in m
            and "Sign-in is hidden" in m
            and "eneru user create" in m
            for m in log
        ), log
        # The off-loopback security warning must NOT fire on a loopback bind.
        assert not any("no authentication" in m for m in log), log
    finally:
        server.stop()


@pytest.mark.unit
def test_api_server_start_no_auth_note_when_auth_enabled(minimal_config):
    from unittest.mock import patch
    from eneru.api import EneruAPIServer

    minimal_config.api.enabled = True
    minimal_config.api.bind = "127.0.0.1"
    minimal_config.api.auth.enabled = True
    minimal_config.api.auth.enabled_explicitly_set = True  # explicit operator choice
    log = []
    server = EneruAPIServer(MagicMock(), minimal_config, log_fn=log.append)

    with patch("eneru.api._BoundedThreadingHTTPServer", return_value=MagicMock()):
        server.start()

    try:
        assert not any("authentication is disabled" in m for m in log), log
    finally:
        server.stop()


@pytest.mark.unit
def test_api_server_stop_no_op_when_not_started(minimal_config):
    from eneru.api import EneruAPIServer

    server = EneruAPIServer(MagicMock(), minimal_config)
    server.stop()  # Must not raise


@pytest.mark.unit
def test_api_server_stop_swallows_shutdown_exceptions(minimal_config):
    from eneru.api import EneruAPIServer

    server = EneruAPIServer(MagicMock(), minimal_config)
    server._httpd = MagicMock()
    server._httpd.shutdown.side_effect = RuntimeError("already stopped")
    server._httpd.server_close.side_effect = RuntimeError("twice over")
    # Both exceptions must be swallowed by the broad except.
    server.stop()
    assert server._httpd is None
    assert server._thread is None


@pytest.mark.unit
def test_api_handler_returns_500_on_unexpected_exception():
    """do_GET must catch generic exceptions and return a 500 JSON envelope."""
    handler = object.__new__(EneruAPIHandler)
    handler.headers = {"Host": "localhost"}  # F-016 dispatch host guard
    handler._route = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    headers = []
    handler.send_response = lambda status: headers.append(("status", status))
    handler.send_header = lambda key, value: headers.append((key, value))
    handler.end_headers = lambda: None
    handler.wfile = BytesIO()

    handler.do_GET()

    assert ("status", 500) in headers
    body = json.loads(handler.wfile.getvalue())
    assert body["error"]["code"] == "INTERNAL_ERROR"


@pytest.mark.unit
def test_api_500_path_logs_traceback():
    """F-030: the 500 handler logs the full traceback via api_log before
    responding, so the swallowed exception is diagnosable."""
    logs = []
    handler = object.__new__(EneruAPIHandler)
    handler.headers = {"Host": "localhost"}
    handler.api_log = logs.append
    handler._route = lambda: (_ for _ in ()).throw(RuntimeError("kaboom-xyz"))
    headers = []
    handler.send_response = lambda status: headers.append(("status", status))
    handler.send_header = lambda key, value: None
    handler.end_headers = lambda: None
    handler.wfile = BytesIO()

    handler.do_GET()

    assert ("status", 500) in headers
    joined = "\n".join(logs)
    assert "unhandled exception" in joined
    assert "kaboom-xyz" in joined          # the real traceback, not just a code


@pytest.mark.unit
def test_api_metrics_route_returns_404_when_prometheus_disabled(minimal_config):
    """When prometheus.enabled=False, /metrics genuinely returns 404 with
    NOT_FOUND payload — independently of the index advertising."""
    minimal_config.prometheus.enabled = False
    handler = object.__new__(EneruAPIHandler)
    handler.path = "/metrics"
    handler.api_config = minimal_config
    handler.api_source = MagicMock()

    status, content_type, payload = handler._route()

    assert status == 404
    assert content_type == "application/json"
    assert payload["error"]["code"] == "NOT_FOUND"
    # And the available-endpoints list should not include /metrics
    assert all(ep["path"] != "/metrics" for ep in payload["availableEndpoints"])


@pytest.mark.unit
def test_api_root_unknown_path_returns_404_with_endpoint_index(minimal_config):
    handler = object.__new__(EneruAPIHandler)
    handler.path = "/does-not-exist"
    handler.api_config = minimal_config
    handler.api_source = MagicMock()

    status, content_type, payload = handler._route()

    assert status == 404
    assert payload["error"]["code"] == "NOT_FOUND"
    assert isinstance(payload["availableEndpoints"], list)
    assert len(payload["availableEndpoints"]) > 0


@pytest.mark.unit
def test_is_loopback_bind_matrix():
    from eneru.api import _is_loopback_bind

    assert _is_loopback_bind("127.0.0.1") is True
    assert _is_loopback_bind("::1") is True
    assert _is_loopback_bind("localhost") is True
    assert _is_loopback_bind(" LOCALHOST ") is True  # case + whitespace
    assert _is_loopback_bind("0.0.0.0") is False
    assert _is_loopback_bind("192.168.1.10") is False
    assert _is_loopback_bind("") is False
    assert _is_loopback_bind(None) is False  # type: ignore[arg-type]


@pytest.mark.unit
def test_metric_line_handles_value_error_with_nan_sentinel():
    """A non-numeric, non-empty value that raises during float() must
    render as NaN — never let Prometheus see lowercase 'nan' or
    a Python repr that breaks strict parsers."""
    from eneru.api import _metric_line

    line = _metric_line("eneru_test", {"ups": "u1"}, [1, 2, 3])  # list raises TypeError
    assert line == 'eneru_test{ups="u1"} NaN'


@pytest.mark.unit
def test_prometheus_metrics_emit_redundancy_group_remote_health(minimal_config):
    """Coverage for the redundancy-group remote-health metric block."""
    from eneru.api import render_prometheus_metrics

    source = MagicMock()
    # Stub collect_status to inject a redundancy-group remote-health row
    with patch("eneru.api.collect_status", return_value={
        "ups": [],
        "redundancyGroups": [{
            "name": "rack-a",
            "remoteHealth": [{
                "server": "node1",
                "host": "node1.lan",
                "status": "HEALTHY",
                "consecutive_failures": 0,
            }],
        }],
    }):
        text = render_prometheus_metrics(source)

    assert 'eneru_remote_health_status{' in text
    assert 'redundancy_group="rack-a"' in text
    assert 'server="node1"' in text
    assert 'eneru_remote_health_consecutive_failures{' in text


# ====================================================================
# MQTT publisher lifecycle tests
# ====================================================================


@pytest.mark.unit
def test_mqtt_start_no_op_when_disabled(minimal_config):
    minimal_config.mqtt.enabled = False
    pub = MQTTPublisher(MagicMock(), minimal_config, stop_event=__import__("threading").Event())
    pub.start()
    assert pub._thread is None


@pytest.mark.unit
def test_mqtt_start_warns_when_paho_missing(minimal_config):
    """When MQTT_AVAILABLE is False, start() logs a warning and returns."""
    minimal_config.mqtt.enabled = True
    log = []
    pub = MQTTPublisher(
        MagicMock(),
        minimal_config,
        stop_event=__import__("threading").Event(),
        log_fn=log.append,
    )

    with patch("eneru.mqtt.MQTT_AVAILABLE", False):
        pub.start()

    assert pub._thread is None
    assert any("paho-mqtt is not installed" in m for m in log), log


@pytest.mark.unit
def test_mqtt_stop_no_op_when_no_thread(minimal_config):
    pub = MQTTPublisher(MagicMock(), minimal_config, stop_event=__import__("threading").Event())
    pub.stop()  # Must not raise


@pytest.mark.unit
def test_mqtt_local_stop_short_path_returns_immediately(minimal_config):
    """_wait short-circuits if the local-stop event was already set."""
    pub = MQTTPublisher(MagicMock(), minimal_config, stop_event=__import__("threading").Event())
    pub._local_stop.set()
    # _wait must return True without ever blocking, regardless of timeout.
    assert pub._wait(timeout=0.001) is True
    assert pub._wait(timeout=999) is True


@pytest.mark.unit
def test_mqtt_publisher_stop_does_not_set_daemon_stop_event(minimal_config):
    """Regression guard: publisher.stop() must signal only its local
    stop event, never the daemon-wide one. Otherwise a config-reload
    or test cleanup that calls publisher.stop() ends the entire
    monitor."""
    import threading
    daemon_stop = threading.Event()
    pub = MQTTPublisher(MagicMock(), minimal_config, stop_event=daemon_stop)
    pub.stop()
    assert pub._local_stop.is_set()
    assert not daemon_stop.is_set()


@pytest.mark.unit
def test_mqtt_publish_one_publish_exception_triggers_reconnect(minimal_config):
    """When client.publish() raises, _publish_one logs the error,
    sets _needs_reconnect, and returns False so the outer loop
    tears down and reconnects."""
    import threading
    log = []
    pub = MQTTPublisher(MagicMock(), minimal_config,
                        stop_event=threading.Event(), log_fn=log.append)
    fake_client = MagicMock()
    fake_client.publish.side_effect = OSError("connection reset")

    ok = pub._publish_one(fake_client, "eneru/status", "{}")

    assert ok is False
    assert pub._needs_reconnect.is_set()
    assert any("MQTT publish failed" in m for m in log), log


@pytest.mark.unit
def test_mqtt_publish_one_nonzero_rc_triggers_reconnect(minimal_config):
    """paho returns rc != 0 (e.g. MQTT_ERR_NO_CONN) when the broker
    connection has dropped under us — surface and reconnect rather
    than silently dropping every subsequent publish."""
    import threading
    log = []
    pub = MQTTPublisher(MagicMock(), minimal_config,
                        stop_event=threading.Event(), log_fn=log.append)
    fake_client = MagicMock()
    fake_client.publish.return_value = MagicMock(rc=4)  # MQTT_ERR_NO_CONN

    ok = pub._publish_one(fake_client, "eneru/status", "{}")

    assert ok is False
    assert pub._needs_reconnect.is_set()
    assert any("rc=4" in m for m in log), log


@pytest.mark.unit
def test_mqtt_publish_one_success_does_not_trigger_reconnect(minimal_config):
    import threading
    pub = MQTTPublisher(MagicMock(), minimal_config, stop_event=threading.Event())
    fake_client = MagicMock()
    fake_client.publish.return_value = MagicMock(rc=0)

    ok = pub._publish_one(fake_client, "eneru/status", "{}")

    assert ok is True
    assert not pub._needs_reconnect.is_set()


@pytest.mark.unit
def test_mqtt_on_disconnect_unexpected_sets_reconnect(minimal_config):
    """Non-zero rc on the on_disconnect callback means a broker-side
    drop, not our own teardown — we must flag for reconnect."""
    import threading
    log = []
    pub = MQTTPublisher(MagicMock(), minimal_config,
                        stop_event=threading.Event(), log_fn=log.append)
    pub._on_disconnect(client=None, userdata=None, rc=7)
    assert pub._needs_reconnect.is_set()
    assert any("disconnected unexpectedly" in m for m in log), log


@pytest.mark.unit
def test_mqtt_on_disconnect_clean_does_not_set_reconnect(minimal_config):
    """rc=0 means we initiated the disconnect ourselves — don't reconnect."""
    import threading
    pub = MQTTPublisher(MagicMock(), minimal_config, stop_event=threading.Event())
    pub._on_disconnect(client=None, userdata=None, rc=0)
    assert not pub._needs_reconnect.is_set()


@pytest.mark.unit
def test_mqtt_on_disconnect_handles_non_int_rc(minimal_config):
    """A reason object whose .value can't be coerced to int (e.g. weird
    paho ReasonCode subclass in a future release) must fall back to
    bool(rc) rather than crashing the callback (mqtt.py lines 265-266)."""
    import threading
    log = []
    pub = MQTTPublisher(MagicMock(), minimal_config,
                        stop_event=threading.Event(), log_fn=log.append)

    class _WeirdReason:
        value = "not-an-int"

    pub._on_disconnect(client=None, userdata=None, rc=_WeirdReason())
    # Truthy object -> unexpected; reconnect flagged.
    assert pub._needs_reconnect.is_set()


@pytest.mark.unit
def test_mqtt_wait_long_timeout_returns_true_when_local_stop_fires_midloop(
    minimal_config,
):
    """Inside the slicing loop, a local stop set between slices must
    return True immediately (mqtt.py line 93)."""
    import threading
    pub = MQTTPublisher(
        MagicMock(), minimal_config, stop_event=threading.Event(),
    )

    real_wait = pub.stop_event.wait
    call_count = {"n": 0}

    def wait_then_set(timeout):
        call_count["n"] += 1
        # After the first slice elapses, simulate publisher.stop() landing.
        pub._local_stop.set()
        return real_wait(timeout)

    pub.stop_event.wait = wait_then_set
    with patch.object(pub, "_LOCAL_STOP_POLL_SECONDS", 0.001):
        assert pub._wait(timeout=0.01) is True
    assert call_count["n"] >= 1


@pytest.mark.unit
def test_mqtt_wait_long_timeout_returns_true_when_stop_event_set_midloop(
    minimal_config,
):
    """Inside the slicing loop, the stop_event firing during a slice
    causes wait() to return True and propagate True (mqtt.py line 99)."""
    import threading
    daemon_stop = threading.Event()
    pub = MQTTPublisher(MagicMock(), minimal_config, stop_event=daemon_stop)

    def wait_signals_stop(timeout):
        daemon_stop.set()
        return True

    pub.stop_event.wait = wait_signals_stop
    with patch.object(pub, "_LOCAL_STOP_POLL_SECONDS", 0.001):
        assert pub._wait(timeout=0.01) is True


@pytest.mark.unit
def test_mqtt_stop_clears_thread_when_join_completes(minimal_config):
    """After join() the thread reference must be cleared so a subsequent
    start() can spin a fresh one (mqtt.py line 120)."""
    import threading
    pub = MQTTPublisher(
        MagicMock(), minimal_config, stop_event=threading.Event(),
    )
    # Install a dummy thread that's already finished. Use a real Thread
    # so .is_alive() returns False after the body exits.
    t = threading.Thread(target=lambda: None)
    t.start()
    t.join()
    pub._thread = t
    pub.stop(timeout=1)
    assert pub._thread is None


@pytest.mark.unit
def test_mqtt_run_swallows_on_disconnect_assignment_failure(minimal_config):
    """Detaching the on_disconnect callback at teardown is best-effort:
    if assignment raises (paho replaced the attribute), the outer loop
    must continue cleanup (mqtt.py lines 148-149)."""
    import threading
    daemon_stop = threading.Event()
    pub = MQTTPublisher(
        MagicMock(), minimal_config, stop_event=daemon_stop,
    )

    # A fake client whose `on_disconnect` setter raises and whose
    # loop_stop/disconnect are no-ops.
    class _PickyClient:
        def loop_stop(self): pass
        def disconnect(self): pass

        @property
        def on_disconnect(self): return None

        @on_disconnect.setter
        def on_disconnect(self, value):
            raise RuntimeError("cannot reassign callback")

    fake_client = _PickyClient()

    # Patch _connect_with_backoff to hand back our fake, and
    # _publish_loop to exit immediately. After one iteration, set the
    # daemon stop so _run exits cleanly.
    def fake_connect(): return fake_client

    def fake_loop(client): daemon_stop.set()

    pub._connect_with_backoff = fake_connect
    pub._publish_loop = fake_loop
    # Must not raise even though on_disconnect setter blows up.
    pub._run()


@pytest.mark.unit
def test_mqtt_run_exits_when_stop_requested_after_teardown(minimal_config):
    """After teardown, if stop was requested during the loop we must
    return from _run (mqtt.py line 159)."""
    import threading
    daemon_stop = threading.Event()
    pub = MQTTPublisher(
        MagicMock(), minimal_config, stop_event=daemon_stop,
    )

    client = MagicMock()

    def fake_loop(c):
        # Simulate the publish loop returning and then a stop landing.
        pub._local_stop.set()

    pub._connect_with_backoff = lambda: client
    pub._publish_loop = fake_loop
    pub._run()
    # No second connect attempt occurred (would raise because
    # _connect_with_backoff returns the same MagicMock each time and
    # we'd recurse). Verifying via no exception + immediate return.


@pytest.mark.unit
def test_mqtt_connect_with_backoff_returns_none_on_stop(minimal_config):
    """If stop is requested while the backoff loop is sleeping between
    failed connect attempts, _connect_with_backoff exits with None
    (mqtt.py line 204 via the `_wait` short-circuit)."""
    import threading
    daemon_stop = threading.Event()
    pub = MQTTPublisher(
        MagicMock(), minimal_config, stop_event=daemon_stop,
    )
    minimal_config.mqtt.enabled = True
    minimal_config.mqtt.broker = "mqtt://broker.invalid:1883"
    daemon_stop.set()  # Force the while-not-stopping guard to fail first time.
    assert pub._connect_with_backoff() is None


@pytest.mark.unit
def test_mqtt_publish_loop_returns_when_needs_reconnect_set(minimal_config):
    """The publish loop checks _needs_reconnect each tick and exits
    cleanly so the outer loop can re-establish the client (mqtt.py
    line 221)."""
    import threading
    pub = MQTTPublisher(
        MagicMock(), minimal_config, stop_event=threading.Event(),
    )
    minimal_config.mqtt.enabled = True
    minimal_config.mqtt.publish_interval = 1
    pub._needs_reconnect.set()
    # Should return immediately without touching the client.
    fake_client = MagicMock()
    pub._publish_loop(fake_client)
    fake_client.publish.assert_not_called()


@pytest.mark.unit
def test_mqtt_publish_loop_returns_when_publish_one_fails(minimal_config):
    """When _publish_one returns False the loop must return so the outer
    reconnect logic kicks in (mqtt.py line 234)."""
    import threading
    pub = MQTTPublisher(
        MagicMock(), minimal_config, stop_event=threading.Event(),
    )
    minimal_config.mqtt.enabled = True
    minimal_config.mqtt.publish_interval = 1
    # Make collect_status return something deterministic so the loop
    # decides to publish on the very first tick.
    with patch("eneru.mqtt.collect_status", return_value={"x": 1}), \
         patch.object(pub, "_publish_one", return_value=False) as p1:
        pub._publish_loop(MagicMock())
    p1.assert_called_once()


@pytest.mark.unit
def test_mqtt_wait_short_timeout_uses_stop_event_directly(minimal_config):
    """For timeouts <= LOCAL_STOP_POLL_SECONDS (5s), `_wait` waits on the
    daemon-wide stop_event in one shot — no slicing loop needed."""
    import threading
    daemon_stop = threading.Event()
    pub = MQTTPublisher(MagicMock(), minimal_config, stop_event=daemon_stop)

    # Daemon stop already set → wait returns True immediately
    daemon_stop.set()
    assert pub._wait(timeout=2) is True


@pytest.mark.unit
def test_mqtt_wait_long_timeout_slices_for_local_stop_responsiveness(minimal_config):
    """For timeouts > 5s, `_wait` loops in 5s slices so a publisher.stop()
    mid-wait is detected within ~5s rather than waiting out the full
    backoff (60s reconnect path)."""
    import threading
    daemon_stop = threading.Event()
    pub = MQTTPublisher(MagicMock(), minimal_config, stop_event=daemon_stop)
    # Configure local-stop to be set very quickly
    pub._local_stop.set()
    # Long timeout but local_stop fires in the first slice → returns True
    assert pub._wait(timeout=30) is True


@pytest.mark.unit
def test_mqtt_wait_long_timeout_returns_false_when_no_stop(minimal_config):
    """A long timeout with neither stop fired returns False (timeout
    exhausted). Use a tiny effective timeout via monkeypatched poll
    constant so the test stays fast."""
    import threading
    daemon_stop = threading.Event()
    pub = MQTTPublisher(MagicMock(), minimal_config, stop_event=daemon_stop)
    # Force the slicing loop with a 6s wait but a tiny poll interval
    with patch.object(pub, "_LOCAL_STOP_POLL_SECONDS", 0.001):
        # wait 0.01s sliced into 0.001s polls — must return False
        assert pub._wait(timeout=0.01) is False


@pytest.mark.unit
def test_mqtt_status_fingerprint_excludes_generatedat():
    """Two payloads that differ only in generatedAt must hash the same
    so cache-coherent UPS state doesn't trigger a publish every poll."""
    snap_a = {"generatedAt": 100, "ups": [{"name": "u", "battery": "75"}]}
    snap_b = {"generatedAt": 999, "ups": [{"name": "u", "battery": "75"}]}
    assert MQTTPublisher._status_fingerprint(snap_a) == MQTTPublisher._status_fingerprint(snap_b)

    snap_c = {"generatedAt": 100, "ups": [{"name": "u", "battery": "60"}]}
    assert MQTTPublisher._status_fingerprint(snap_a) != MQTTPublisher._status_fingerprint(snap_c)


# ====================================================================
# logger.JSONFormatter heuristic-category tests
# ====================================================================


@pytest.mark.unit
def test_json_formatter_categorizes_remote_health_messages():
    formatter = JSONFormatter()
    record = logging.LogRecord(
        "ups-monitor", logging.INFO, __file__, 1,
        "Remote health probe failed for node1", (), None,
    )
    payload = json.loads(formatter.format(record))
    assert payload["category"] == "health"


@pytest.mark.unit
def test_json_formatter_categorizes_uppercase_shutdown_only():
    """Lowercase 'shutdown' in config-key text or info lines must NOT be
    miscategorised as a shutdown event — only the uppercase SHUTDOWN
    marker the daemon emits is classified."""
    formatter = JSONFormatter()
    record = logging.LogRecord(
        "ups-monitor", logging.INFO, __file__, 1,
        "🔌  SHUTDOWN SEQUENCE STARTING", (), None,
    )
    payload = json.loads(formatter.format(record))
    assert payload["category"] == "shutdown"

    # And the lowercase informational line must NOT trip it
    record2 = logging.LogRecord(
        "ups-monitor", logging.INFO, __file__, 1,
        "Local shutdown disabled", (), None,
    )
    payload2 = json.loads(formatter.format(record2))
    assert payload2.get("category") != "shutdown"


@pytest.mark.unit
def test_json_formatter_extracts_event_type_from_power_event_message():
    """⚡  POWER EVENT: <type> ... messages auto-fill event_type when no
    structured extra was provided."""
    formatter = JSONFormatter()
    record = logging.LogRecord(
        "ups-monitor", logging.INFO, __file__, 1,
        "⚡  POWER EVENT: OB DISCHRG - on battery", (), None,
    )
    payload = json.loads(formatter.format(record))
    assert payload["category"] == "power_event"
    assert payload["event_type"] == "OB"


# ====================================================================
# UPSLogger setup tests — file/syslog handler init paths
# ====================================================================


@pytest.mark.unit
def test_ups_logger_log_file_permission_error_falls_back_to_console(minimal_config, tmp_path, capsys):
    """When the configured log file path is unwritable, UPSLogger
    prints a warning to stdout and continues with console-only logging."""
    from eneru.logger import UPSLogger
    # Point at a path where mkdir succeeds but FileHandler can't open
    bad_path = tmp_path / "logs" / "eneru.log"

    with patch("eneru.logger.logging.FileHandler", side_effect=PermissionError("ro fs")):
        UPSLogger(str(bad_path), minimal_config)

    out = capsys.readouterr().out
    assert "Cannot write to" in out
    assert str(bad_path) in out


@pytest.mark.unit
def test_ups_logger_syslog_init_failure_logs_warning(minimal_config):
    """A misconfigured syslog address must not crash logger setup —
    log a warning through the already-installed handlers and move on."""
    from eneru.logger import UPSLogger
    minimal_config.logging.syslog.enabled = True
    minimal_config.logging.syslog.address = "127.0.0.1"
    minimal_config.logging.syslog.port = 0  # Forces SysLogHandler to fail
    minimal_config.logging.syslog.facility = "user"

    with patch(
        "eneru.logger.logging.handlers.SysLogHandler",
        side_effect=OSError("syslog socket refused"),
    ):
        # Must not raise.
        ups_logger = UPSLogger(None, minimal_config)
        # And the syslog handler must NOT have been attached.
        assert all(
            type(h).__name__ != "SysLogHandler"
            for h in ups_logger.logger.handlers
        )


@pytest.mark.unit
def test_ups_logger_log_with_extras_forwards_to_logger_info(minimal_config):
    """`UPSLogger.log(msg, **extra)` must forward extras as the
    `extra=` kwarg to the underlying logger so JSONFormatter sees them
    as LogRecord attributes."""
    from eneru.logger import UPSLogger
    ups_logger = UPSLogger(None, minimal_config)
    with patch.object(ups_logger.logger, "info") as info_mock:
        ups_logger.log("hello", category="shutdown", group="ups0")
    info_mock.assert_called_once()
    # Second positional or `extra=` kwarg must contain the extras
    kwargs = info_mock.call_args.kwargs
    assert kwargs.get("extra") == {"category": "shutdown", "group": "ups0"}


@pytest.mark.unit
def test_ups_logger_log_without_extras_omits_extra_kwarg(minimal_config):
    """When no extras are passed, UPSLogger uses the simple .info(msg)
    path so the LogRecord has no extra attributes set."""
    from eneru.logger import UPSLogger
    ups_logger = UPSLogger(None, minimal_config)
    with patch.object(ups_logger.logger, "info") as info_mock:
        ups_logger.log("hello")
    info_mock.assert_called_once_with("hello")


@pytest.mark.unit
def test_ups_logger_init_clears_stale_handlers(minimal_config):
    """Re-initialising UPSLogger against the same module-level logger
    must close and remove handlers from the prior instance — otherwise
    every restart leaks file descriptors."""
    from eneru.logger import UPSLogger
    first = UPSLogger(None, minimal_config)
    handler_count_first = len(first.logger.handlers)

    # Build a second logger with the same config — handler set should
    # match the first run, not be doubled.
    second = UPSLogger(None, minimal_config)
    assert len(second.logger.handlers) == handler_count_first


@pytest.mark.unit
def test_ups_logger_uses_json_formatter_when_configured(minimal_config):
    """`logging.format == "json"` must pick JSONFormatter for every handler
    (logger.py line 129)."""
    from eneru.logger import UPSLogger, JSONFormatter
    minimal_config.logging.format = "json"
    ups_logger = UPSLogger(None, minimal_config)
    assert any(
        isinstance(h.formatter, JSONFormatter)
        for h in ups_logger.logger.handlers
    )


@pytest.mark.unit
def test_ups_logger_clears_stale_handlers_swallows_close_error(
    minimal_config, monkeypatch,
):
    """Re-init must keep cleaning up even when a stale handler's close()
    raises (logger.py lines 121-122)."""
    import logging as _logging
    from eneru.logger import UPSLogger

    # Seed the module-level logger with a handler whose .close() blows up.
    bad = _logging.NullHandler()
    monkeypatch.setattr(
        bad, "close", lambda: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    _logging.getLogger("ups-monitor").addHandler(bad)

    # Must not raise: the except clause swallows the close() failure.
    UPSLogger(None, minimal_config)


@pytest.mark.unit
def test_ups_logger_attaches_syslog_handler_on_success(minimal_config):
    """When SysLogHandler initialises cleanly it must be registered
    against the logger (logger.py lines 158-159)."""
    import logging.handlers as _handlers
    from unittest.mock import MagicMock
    from eneru.logger import UPSLogger

    minimal_config.logging.syslog.enabled = True
    minimal_config.logging.syslog.address = "127.0.0.1"
    minimal_config.logging.syslog.port = 514
    minimal_config.logging.syslog.facility = "user"

    # Build a stand-in handler class that records the constructor call and
    # behaves like a real logging.Handler for setFormatter/addHandler.
    fake_handler = _handlers.MemoryHandler(capacity=1)
    with patch(
        "eneru.logger.logging.handlers.SysLogHandler",
        return_value=fake_handler,
    ) as syslog_cls:
        ups_logger = UPSLogger(None, minimal_config)
        syslog_cls.assert_called_once()
    assert fake_handler in ups_logger.logger.handlers


@pytest.mark.unit
def test_redact_sensitive_text_strips_common_credential_shapes():
    """Token / password / api_key query strings + scheme://user:pass@
    auth must be redacted before structured logging."""
    from eneru.logger import redact_sensitive_text

    samples = [
        ("https://api.example.com/?token=abcdef123",
         "<redacted>", "abcdef123"),
        ("postgres://admin:supersecret@db.example.com",
         "<redacted>", "supersecret"),
        ("SLACK_WEBHOOK=password=mypw&channel=ops",
         "<redacted>", "mypw"),
        ("discord://1234567/abcdef",
         "<redacted>", "1234567/abcdef"),
    ]
    for raw, must_contain, must_not_contain in samples:
        scrubbed = redact_sensitive_text(raw)
        assert must_contain in scrubbed, (raw, scrubbed)
        assert must_not_contain not in scrubbed, (raw, scrubbed)


# ====================================================================
# status helpers — query_history defensive paths
# ====================================================================


@pytest.mark.unit
def test_query_history_unknown_ups_returns_none(minimal_config):
    """An unknown UPS name short-circuits to None (not an empty list)."""
    rows = query_history(minimal_config, "no-such-ups", "charge", 0, 100)
    assert rows is None


@pytest.mark.unit
def test_query_history_unknown_metric_returns_none(minimal_config):
    """Unknown metric also returns None so callers map it to a 404."""
    rows = query_history(minimal_config, "TestUPS@localhost", "watts", 0, 100)
    assert rows is None


@pytest.mark.unit
def test_query_history_missing_db_returns_empty_list(minimal_config, tmp_path):
    """When the UPS exists but the stats DB hasn't been opened (no run yet),
    query_history returns [], not None — distinguishing 'no data' from
    'no such UPS'."""
    minimal_config.statistics.db_directory = str(tmp_path / "no-such-dir")
    rows = query_history(minimal_config, "TestUPS@localhost", "charge", 0, 100)
    assert rows == []


@pytest.mark.unit
def test_query_history_returns_buffered_samples_in_range(minimal_config, tmp_path):
    """Happy path with a real (seeded) stats DB: query_history must
    return the rows that fall inside [from, to], converted to the
    JSON-friendly {ts, value} shape, and close the connection in
    its finally block."""
    minimal_config.statistics.db_directory = str(tmp_path)
    store = StatsStore(tmp_path / "default.db")
    store.open()
    try:
        # Seed a few samples spanning ts 100..103
        for ts, charge in [(100, 50.0), (101, 60.0), (102, 70.0), (103, 80.0)]:
            store.buffer_sample(
                {"battery.charge": str(charge), "ups.status": "OL CHRG"},
                ts=ts,
            )
        store.flush()
    finally:
        store.close()

    # Query a range that includes 101..102
    rows = query_history(minimal_config, "TestUPS@localhost", "charge", 101, 102)
    assert isinstance(rows, list)
    assert len(rows) == 2
    # Rows shaped {ts, value}, sorted by ts
    assert all(set(r.keys()) == {"ts", "value"} for r in rows)
    assert rows[0]["ts"] == 101
    assert rows[1]["ts"] == 102
    assert rows[0]["value"] == 60.0
    assert rows[1]["value"] == 70.0


@pytest.mark.unit
def test_state_file_path_for_group_multi_ups_uses_per_ups_suffix():
    """In multi-UPS mode the state-file path is suffixed with the
    sanitized UPS name so per-UPS rows don't collide."""
    from eneru.status import state_file_path_for_group
    from eneru import Config, UPSConfig, UPSGroupConfig, LoggingConfig
    config = Config(
        ups_groups=[
            UPSGroupConfig(ups=UPSConfig(name="UPS-A@host:3493")),
            UPSGroupConfig(ups=UPSConfig(name="UPS-B@host:3493")),
        ],
        logging=LoggingConfig(state_file="/tmp/eneru-state"),
    )
    p = state_file_path_for_group(config, config.ups_groups[0])
    assert str(p).endswith(".UPS-A-host-3493")  # sanitized suffix


@pytest.mark.unit
def test_state_file_path_for_group_single_ups_uses_unsuffixed_path():
    from eneru.status import state_file_path_for_group
    from eneru import Config, UPSConfig, UPSGroupConfig, LoggingConfig
    config = Config(
        ups_groups=[UPSGroupConfig(ups=UPSConfig(name="UPS@host"))],
        logging=LoggingConfig(state_file="/tmp/eneru-state"),
    )
    p = state_file_path_for_group(config, config.ups_groups[0])
    assert str(p) == "/tmp/eneru-state"


@pytest.mark.unit
def test_redundancy_group_statuses_returns_empty_when_config_is_none():
    from eneru.status import redundancy_group_statuses
    assert redundancy_group_statuses(MagicMock(), None) == []


@pytest.mark.unit
def test_readiness_returns_no_monitors_for_empty_coordinator(minimal_config):
    """A coordinator with zero monitors registered must report
    `ready=False, reason='no monitors'` so the K8s probe fails clean
    instead of returning success on no data."""
    from eneru.status import readiness
    coord = MagicMock()
    coord._monitors = []  # No monitors registered
    # Disable own state by removing the attribute the readiness path inspects
    del coord.state
    payload = readiness(coord)
    assert payload["ready"] is False
    assert "no monitors" in payload["reason"]


@pytest.mark.unit
def test_query_history_via_sanitized_ups_name(minimal_config, tmp_path):
    """The lookup accepts both the raw UPS name and its sanitized form
    (the one used in the URL path)."""
    from eneru.status import sanitize_name
    minimal_config.statistics.db_directory = str(tmp_path)
    store = StatsStore(tmp_path / "default.db")
    store.open()
    try:
        store.buffer_sample(
            {"battery.charge": "42", "ups.status": "OL"}, ts=200,
        )
        store.flush()
    finally:
        store.close()

    sanitized = sanitize_name("TestUPS@localhost")
    rows = query_history(minimal_config, sanitized, "charge", 0, 1000)
    assert rows == [{"ts": 200, "value": 42.0}]


# ====================================================================
# remote_health helper coverage
# ====================================================================


@pytest.mark.unit
def test_build_ssh_probe_command_dangling_option_raises():
    """An ssh_options entry like '-i' with no following key path is
    rejected with a clear error — otherwise OpenSSH errors mid-shutdown
    instead of failing fast."""
    from eneru.remote_health import build_ssh_probe_command
    from eneru import RemoteServerConfig

    server = RemoteServerConfig(
        name="nas",
        host="nas.lan",
        user="ups",
        ssh_options=["-i"],  # Dangling - expects a path next
    )
    with pytest.raises(ValueError, match="dangling SSH option"):
        build_ssh_probe_command(server, probe_command="true")


@pytest.mark.unit
def test_run_remote_probe_returns_timeout_message_on_124():
    """Exit code 124 (GNU timeout) is reported with the connect_timeout
    seconds so operators know which knob to bump."""
    from eneru.remote_health import run_remote_probe
    from eneru import RemoteServerConfig

    server = RemoteServerConfig(
        name="nas", host="nas.lan", user="ups", connect_timeout=7,
    )
    with patch("eneru.remote_health.run_command", return_value=(124, "", "")):
        ok, err, _ = run_remote_probe(server, "true")
    assert ok is False
    assert "timed out after 7s" in err


@pytest.mark.unit
def test_run_remote_probe_falls_back_to_exit_code_when_stderr_empty():
    """When stderr is empty, the failure message uses the exit code so
    the row in the sidecar / TUI is never blank."""
    from eneru.remote_health import run_remote_probe
    from eneru import RemoteServerConfig

    server = RemoteServerConfig(name="nas", host="nas.lan", user="ups")
    with patch("eneru.remote_health.run_command", return_value=(255, "", "")):
        ok, err, _ = run_remote_probe(server, "true")
    assert ok is False
    assert "exit code 255" in err


@pytest.mark.unit
def test_remote_health_manager_start_no_op_when_disabled(minimal_config, tmp_path):
    """When remote_health.enabled=False, start() writes a sidecar then returns."""
    import threading
    from eneru.remote_health import RemoteHealthManager
    from eneru import RemoteServerConfig

    minimal_config.remote_health.enabled = False
    sidecar = tmp_path / "remote-health.json"
    mgr = RemoteHealthManager(
        config=minimal_config,
        group_label="g",
        servers=[RemoteServerConfig(name="n", host="h", user="u")],
        sidecar_path=sidecar,
        stop_event=threading.Event(),
        log_fn=lambda _: None,
    )
    mgr.start()
    assert mgr._thread is None  # No background thread spawned


@pytest.mark.unit
def test_remote_health_manager_start_no_op_with_no_servers(minimal_config, tmp_path):
    import threading
    from eneru.remote_health import RemoteHealthManager

    minimal_config.remote_health.enabled = True
    sidecar = tmp_path / "remote-health.json"
    mgr = RemoteHealthManager(
        config=minimal_config,
        group_label="g",
        servers=[],  # Empty server list
        sidecar_path=sidecar,
        stop_event=threading.Event(),
        log_fn=lambda _: None,
    )
    mgr.start()
    assert mgr._thread is None


@pytest.mark.unit
def test_remote_health_manager_stop_handles_missing_thread(minimal_config, tmp_path):
    import threading
    from eneru.remote_health import RemoteHealthManager

    sidecar = tmp_path / "rh.json"
    mgr = RemoteHealthManager(
        config=minimal_config,
        group_label="g",
        servers=[],
        sidecar_path=sidecar,
        stop_event=threading.Event(),
        log_fn=lambda _: None,
    )
    # No start() called, _thread is None
    mgr.stop()  # Must not raise


@pytest.mark.unit
def test_remote_health_manager_snapshot_returns_serializable_rows(minimal_config, tmp_path):
    import threading
    from eneru.remote_health import RemoteHealthManager
    from eneru import RemoteServerConfig

    sidecar = tmp_path / "rh.json"
    mgr = RemoteHealthManager(
        config=minimal_config,
        group_label="g",
        servers=[RemoteServerConfig(name="n", enabled=True, host="h.lan", user="u")],
        sidecar_path=sidecar,
        stop_event=threading.Event(),
        log_fn=lambda _: None,
    )
    rows = mgr.snapshot()
    assert isinstance(rows, list)
    assert len(rows) == 1
    assert rows[0]["server"] == "n"
    assert rows[0]["host"] == "h.lan"
    # The status field exists and is one of the known states
    assert "status" in rows[0]


@pytest.mark.unit
def test_remote_health_manager_start_idempotent_when_already_running(minimal_config, tmp_path):
    """A second start() call must not spawn a second thread."""
    import threading
    from eneru.remote_health import RemoteHealthManager
    from eneru import RemoteServerConfig

    minimal_config.remote_health.enabled = True
    mgr = RemoteHealthManager(
        config=minimal_config,
        group_label="g",
        servers=[RemoteServerConfig(name="n", enabled=True, host="h.lan", user="u")],
        sidecar_path=tmp_path / "rh.json",
        stop_event=threading.Event(),
        log_fn=lambda _: None,
    )
    sentinel = MagicMock()
    sentinel.is_alive.return_value = True
    mgr._thread = sentinel  # Pretend already running
    with patch("eneru.remote_health.threading.Thread") as t_cls:
        mgr.start()
    t_cls.assert_not_called()
    assert mgr._thread is sentinel


@pytest.mark.unit
def test_remote_health_manager_stop_clears_thread_when_dead(minimal_config, tmp_path):
    """`stop()` must clear `_thread` once the thread is no longer alive
    so a subsequent `start()` can spawn a fresh one."""
    import threading
    from eneru.remote_health import RemoteHealthManager

    mgr = RemoteHealthManager(
        config=minimal_config, group_label="g", servers=[],
        sidecar_path=tmp_path / "rh.json",
        stop_event=threading.Event(), log_fn=lambda _: None,
    )
    fake_thread = MagicMock()
    fake_thread.is_alive.return_value = False
    mgr._thread = fake_thread
    mgr.stop(timeout=0)
    fake_thread.join.assert_called_once_with(timeout=0)
    assert mgr._thread is None


@pytest.mark.unit
def test_remote_health_event_fn_failure_logs_only_first_time(minimal_config, tmp_path):
    """A broken event_fn must not interrupt health checking — the first
    failure logs, subsequent failures are silent."""
    import threading
    from eneru.remote_health import RemoteHealthManager
    from eneru import RemoteServerConfig

    log = []

    def bad_event_fn(*_args, **_kw):
        raise OSError("events table missing")

    mgr = RemoteHealthManager(
        config=minimal_config,
        group_label="g",
        servers=[RemoteServerConfig(name="n", enabled=True, host="h.lan", user="u")],
        sidecar_path=tmp_path / "rh.json",
        stop_event=threading.Event(),
        log_fn=log.append,
        event_fn=bad_event_fn,
    )

    for _ in range(2):
        mgr._record_status_transition(
            previous="HEALTHY",
            current="FAILED",
            display="n",
            host="h.lan",
            error="ssh timed out",
            notification_sent=False,
        )

    failure_logs = [
        m for m in log
        if "Remote health stats event failed" in m
    ]
    assert len(failure_logs) == 1
    assert "further failures will be silent" in failure_logs[0]


@pytest.mark.unit
def test_redact_broker_at_before_scheme_returns_raw():
    """Behavioural-gap 10 (mqtt edge): an '@' that is NOT userinfo -- it sits
    before the '://' separator -- leaves the broker string untouched, because
    the post-scheme remainder has no '@' to redact."""
    from eneru.mqtt import _redact_broker
    assert _redact_broker("weird@host://path") == "weird@host://path"
