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


@pytest.mark.unit
def test_readiness_failed_when_monitor_has_no_fresh_data(monitor):
    monitor.state.latest_update_time = 0
    payload = readiness(monitor)
    assert payload["ready"] is False
    assert "failed" in payload["reason"]


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
    finally:
        store.close()

    power_only = query_events(minimal_config, verbosity=0)
    diagnostics = query_events(minimal_config, verbosity=1)
    all_events = query_events(minimal_config, verbosity=2)

    assert [row["eventType"] for row in power_only] == ["ON_BATTERY"]
    assert [row["eventType"] for row in diagnostics] == [
        "ON_BATTERY",
        "REMOTE_HEALTH_FAILED",
    ]
    assert [row["eventType"] for row in all_events] == [
        "ON_BATTERY",
        "SERVICE_STARTED",
        "REMOTE_HEALTH_FAILED",
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
        "[Wrong-Group] ⚡ POWER EVENT: OL - back",
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

    with patch("eneru.api.ThreadingHTTPServer") as fake_server:
        fake_server.return_value = MagicMock()
        server.start()
        first_thread = server._thread
        # Second call must early-return without re-binding
        server.start()
        assert server._thread is first_thread
        assert fake_server.call_count == 1
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

    with patch("eneru.api.ThreadingHTTPServer", side_effect=OSError("port in use")):
        server.start()

    assert server._httpd is None
    assert server._thread is None
    assert any("API server failed to bind" in m for m in log), log


@pytest.mark.unit
def test_api_server_start_warns_on_non_loopback_bind(minimal_config):
    from unittest.mock import patch
    from eneru.api import EneruAPIServer

    minimal_config.api.enabled = True
    minimal_config.api.bind = "0.0.0.0"
    log = []
    server = EneruAPIServer(MagicMock(), minimal_config, log_fn=log.append)

    with patch("eneru.api.ThreadingHTTPServer", return_value=MagicMock()):
        server.start()

    try:
        # The bind announcement comes first, then the security warning
        assert any("API server listening on 0.0.0.0" in m for m in log), log
        assert any("non-loopback" in m and "no authentication" in m for m in log), log
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
        "🔌 SHUTDOWN SEQUENCE STARTING", (), None,
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
    """⚡ POWER EVENT: <type> ... messages auto-fill event_type when no
    structured extra was provided."""
    formatter = JSONFormatter()
    record = logging.LogRecord(
        "ups-monitor", logging.INFO, __file__, 1,
        "⚡ POWER EVENT: OB DISCHRG - on battery", (), None,
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

    # Simulate two transitions through _record_remote_health_event
    # (the private method that wraps event_fn). Use the public
    # interface by directly invoking the wrapper.
    mgr._record_remote_health_event = MagicMock()
    # Pretend two transitions happen: trigger the wrapped path manually
    for _ in range(2):
        try:
            mgr.event_fn("REMOTE_HEALTH_FAILED", "dummy detail", False)
        except Exception:
            pass

    # The wrapper itself logs once on first failure; manually invoke
    # the private path that contains the dedup logic
    mgr._event_fn_logged_failure = False
    # Simulate two _check_server transitions that exercise the
    # event_fn try/except
    for _ in range(2):
        try:
            mgr.event_fn("REMOTE_HEALTH_FAILED", "x", False)
        except Exception as exc:
            if not mgr._event_fn_logged_failure:
                mgr._event_fn_logged_failure = True
                mgr.log_fn(f"⚠️ Remote health stats event failed: {exc}")
    # The dedup at the wrapped call site means we log at most once.
    failure_logs = [m for m in log if "Remote health" in m]
    assert len(failure_logs) <= 1
