"""v6.1 API/status surfacing: battery-health, energy, self-test blocks in
monitor_status, and the new Prometheus series (emitted when known, omitted
when unknown)."""

import time
from unittest.mock import MagicMock

import pytest

from eneru.api import render_prometheus_metrics
from eneru.monitor import UPSGroupMonitor
from eneru.state import MonitorState
from eneru.stats import StatsStore
from eneru.status import collect_status, monitor_status


def _series_names(text):
    """Metric names that actually have a data series (not just HELP/TYPE)."""
    names = set()
    for line in text.splitlines():
        if line and not line.startswith("#"):
            names.add(line.split("{")[0].split(" ")[0])
    return names


@pytest.fixture
def mon_with_data(minimal_config, tmp_path):
    minimal_config.logging.battery_history_file = str(tmp_path / "bh")
    minimal_config.logging.shutdown_flag_file = str(tmp_path / "sf")
    minimal_config.logging.state_file = str(tmp_path / "st")
    minimal_config.energy.cost_per_kwh = 0.20  # enable cost
    m = UPSGroupMonitor(minimal_config)
    m.logger = MagicMock()
    store = StatsStore(tmp_path / "s.db")
    store.open()
    m._stats_store = store
    now = time.time()
    # ~16 min of samples at 10s spacing: enough for the raw "today" window and,
    # after aggregate(), several agg_5min buckets for the "month" window.
    for i in range(100):
        store.buffer_sample({"ups.status": "OL", "ups.realpower": "120"},
                            ts=int(now - 100 * 10 + i * 10))
    store.flush()
    store.aggregate()
    tid = store.record_self_test("test.battery.start", "scheduler")
    store.update_self_test_result(tid, result_raw="Done and passed",
                                  result_enum="passed")
    m.state = MonitorState(latest_status="OL CHRG", latest_battery_charge="97",
                           latest_runtime="1200", latest_update_time=now)
    m.state.latest_battery_health = {
        "score": 82.0, "confidence": 0.7, "availableTerms": ["runtime"],
        "terms": {}, "replacementDaysRemaining": 45.0, "replacementDue": False}
    yield m
    store.close()


class TestStatusSurfacing:
    @pytest.mark.unit
    def test_status_includes_v61_blocks(self, mon_with_data):
        row = monitor_status(mon_with_data)
        assert row["batteryHealth"]["score"] == 82.0
        assert "todayKwh" in row["energy"]
        assert row["energy"]["currency"] == "USD"
        assert row["selfTest"]["result"] == "passed"

    @pytest.mark.unit
    def test_status_blocks_none_without_data(self, minimal_config, tmp_path):
        minimal_config.logging.state_file = str(tmp_path / "st")
        minimal_config.energy.enabled = False
        m = UPSGroupMonitor(minimal_config)
        m.logger = MagicMock()
        m.state = MonitorState(latest_status="OL", latest_update_time=time.time())
        row = monitor_status(m)
        assert row["batteryHealth"] is None
        assert row["energy"] is None
        assert row["selfTest"] is None


class TestPrometheus:
    @pytest.mark.unit
    def test_emits_v61_series_when_known(self, mon_with_data):
        text = render_prometheus_metrics(mon_with_data)
        assert "eneru_ups_battery_health_score" in _series_names(text)
        assert "eneru_ups_replacement_days_remaining" in _series_names(text)
        assert 'eneru_ups_energy_kwh{' in text and 'period="today"' in text
        assert 'period="month"' in text
        assert 'eneru_ups_energy_cost{' in text       # cost enabled
        assert 'eneru_ups_self_test_result{' in text and 'result="passed"' in text

    @pytest.mark.unit
    def test_omits_v61_series_when_unknown(self, minimal_config, tmp_path):
        # No battery health, no stats store, energy disabled -> no data series
        # for the v6.1 metrics (HELP/TYPE lines may still be present).
        minimal_config.logging.state_file = str(tmp_path / "st")
        minimal_config.energy.enabled = False
        m = UPSGroupMonitor(minimal_config)
        m.logger = MagicMock()
        m.state = MonitorState(latest_status="OL", latest_update_time=time.time())
        names = _series_names(render_prometheus_metrics(m))
        assert "eneru_ups_battery_health_score" not in names
        assert "eneru_ups_energy_kwh" not in names
        assert "eneru_ups_energy_cost" not in names
        assert "eneru_ups_self_test_result" not in names

    @pytest.mark.unit
    def test_energy_cost_omitted_when_no_price(self, minimal_config, tmp_path):
        minimal_config.logging.state_file = str(tmp_path / "st")
        minimal_config.energy.cost_per_kwh = None  # cost tracking off
        m = UPSGroupMonitor(minimal_config)
        m.logger = MagicMock()
        store = StatsStore(tmp_path / "s.db")
        store.open()
        m._stats_store = store
        now = time.time()
        for i in range(5):
            store.buffer_sample({"ups.status": "OL", "ups.realpower": "100"},
                                ts=int(now - 4 + i))
        store.flush()
        m.state = MonitorState(latest_status="OL", latest_update_time=now)
        text = render_prometheus_metrics(m)
        store.close()
        assert "eneru_ups_energy_kwh{" in text          # kWh present
        assert "eneru_ups_energy_cost{" not in text      # cost omitted
