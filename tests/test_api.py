"""Tests for the read-only API and Prometheus formatter."""

import time
from unittest.mock import MagicMock

import pytest

from eneru import Config, MonitorState, UPSGroupMonitor
from eneru.api import render_prometheus_metrics
from eneru.status import collect_status, config_summary, readiness


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
