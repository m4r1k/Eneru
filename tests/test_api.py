"""Tests for the read-only API and Prometheus formatter."""

import json
import logging
import time
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import MagicMock

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
        latest_update_time=time.time(),
    )
    monitor.logger = MagicMock()
    return monitor


@pytest.mark.unit
def test_collect_status_is_read_only_shape(monitor):
    payload = collect_status(monitor)
    assert payload["ups"][0]["status"] == "OL CHRG"
    assert payload["ups"][0]["batteryCharge"] == "97"
    assert payload["ups"][0]["connectionState"] == "OK"
    assert payload["ups"][0]["groupId"] == "TestUPS-localhost"


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

    handler.path = "/api/v1/ups/TestUPS-localhost"
    status, _, payload = handler._route()
    assert status == 200
    assert payload["groupId"] == "TestUPS-localhost"

    handler.path = "/api/v1/ups/missing"
    status, _, payload = handler._route()
    assert status == 404
    assert payload["error"]["code"] == "NOT_FOUND"

    minimal_config.prometheus.enabled = False
    handler.path = "/metrics"
    status, _, payload = handler._route()
    assert status == 404
    assert payload["error"]["message"] == "Metrics disabled"


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
    config.mqtt.publish_interval = 99
    monkeypatch.setattr("eneru.mqtt.MQTT_AVAILABLE", True)
    monkeypatch.setattr(
        "eneru.mqtt.mqtt_client",
        SimpleNamespace(Client=lambda **kwargs: FakeClient()),
    )
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
