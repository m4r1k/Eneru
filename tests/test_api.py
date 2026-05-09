"""Tests for the read-only API and Prometheus formatter."""

import json
import logging
import time
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
from eneru.status import collect_status, config_summary, query_events, readiness


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
def test_prometheus_metrics_include_eneru_specific_values(monitor):
    text = render_prometheus_metrics(monitor)
    assert "eneru_up 1" in text
    assert "eneru_ups_battery_charge" in text
    assert "eneru_ups_depletion_rate_percent_per_minute" in text


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
    lifecycle = query_events(minimal_config, verbosity=1)
    all_events = query_events(minimal_config, verbosity=2)

    assert [row["eventType"] for row in power_only] == ["ON_BATTERY"]
    assert [row["eventType"] for row in lifecycle] == [
        "ON_BATTERY",
        "SERVICE_STARTED",
    ]
    assert [row["eventType"] for row in all_events] == [
        "ON_BATTERY",
        "SERVICE_STARTED",
        "REMOTE_HEALTH_FAILED",
    ]


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
        SimpleNamespace(Client=lambda: FakeClient()),
    )
    monkeypatch.setattr("eneru.mqtt.collect_status", lambda source: statuses.pop(0))

    MQTTPublisher(object(), config, FakeStopEvent())._run()

    assert [row["ups"][0]["status"] for row in published] == ["OL", "OB"]
