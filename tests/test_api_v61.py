"""v6.1 API/status surfacing: battery-health, energy, self-test blocks in
monitor_status, and the new Prometheus series (emitted when known, omitted
when unknown)."""

import time
from unittest.mock import MagicMock

import pytest
import yaml

from eneru.api import EneruAPIHandler, render_prometheus_metrics
from eneru.config import ConfigLoader


def _parse_cfg(text):
    return ConfigLoader._parse_config(yaml.safe_load(text))


class TestSelfTestApiResolution:
    @pytest.mark.unit
    def test_effective_self_test_honors_per_ups_command(self):
        cfg = _parse_cfg(
            "self_test:\n  command: test.battery.start\n"
            "ups:\n  - name: U1@h\n    self_test:\n      command: test.battery.start.quick\n"
            "  - name: U2@h\n")
        h = object.__new__(EneruAPIHandler)
        h.api_config = cfg
        assert h._effective_self_test("U1@h").command == "test.battery.start.quick"
        assert h._effective_self_test("U2@h").command == "test.battery.start"

    @pytest.mark.unit
    def test_self_test_endpoint_hidden_when_nut_control_off(self):
        # POST self-test needs auth AND nut_control; with nut_control off it must
        # not be advertised in availableEndpoints.
        h = object.__new__(EneruAPIHandler)
        h.api_config = _parse_cfg(
            "api:\n  auth:\n    enabled: true\n"
            "nut_control:\n  enabled: false\n"
            "ups:\n  name: U@h\n")
        paths = {e["path"] for e in h._available_endpoints()}
        assert "/api/v1/ups/{name}/self-test" not in paths

    @pytest.mark.unit
    def test_self_test_endpoint_visible_when_callable(self):
        h = object.__new__(EneruAPIHandler)
        h.api_config = _parse_cfg(
            "api:\n  auth:\n    enabled: true\n"
            "nut_control:\n  enabled: true\n  allowed_commands: [test.battery.start]\n"
            "ups:\n  name: U@h\n")
        paths = {e["path"] for e in h._available_endpoints()}
        assert "/api/v1/ups/{name}/self-test" in paths
from eneru.monitor import UPSGroupMonitor
from eneru.state import MonitorState
from eneru.stats import StatsStore
from eneru.status import (
    _battery_health_for_monitor,
    _energy_for_monitor,
    _self_test_for_monitor,
    collect_status,
    monitor_status,
)


class TestPowerSeries:
    @pytest.mark.unit
    def test_power_series_real_and_fallback(self):
        from eneru.status import power_series

        class _S:
            def power_samples(self, a, b):
                return [(100, 120.0, 50.0, 1000.0),   # realpower -> watts 120
                        (200, None, 25.0, 1000.0)]    # fallback  -> 25% * 1000
        out = power_series(_S(), 0, 300)
        assert out[0]["watts"] == 120.0 and out[0]["loadPct"] == 50.0
        assert out[0]["estimated"] is False
        assert out[1]["watts"] == 250.0 and out[1]["estimated"] is True

    @pytest.mark.unit
    def test_power_series_no_store(self):
        from eneru.status import power_series
        assert power_series(None, 0, 1) == []

    @pytest.mark.unit
    def test_power_series_uses_nominal_fallback(self):
        from eneru.status import power_series

        class _S:
            def power_samples(self, a, b):
                return [(100, None, 40.0, None)]   # no realpower, no nominal
        # Without a fallback -> watts unknown; with one -> estimated 40% * 500.
        assert power_series(_S(), 0, 200)[0]["watts"] is None
        out = power_series(_S(), 0, 200, nominal_fallback=500.0)
        assert out[0]["watts"] == 200.0 and out[0]["estimated"] is True

    @pytest.mark.unit
    def test_power_endpoint_route(self):
        from types import SimpleNamespace
        from eneru.api import EneruAPIHandler

        class _S:
            def power_samples(self, a, b):
                return [(100, 120.0, 50.0, 1000.0)]
        mon = SimpleNamespace(
            config=SimpleNamespace(ups=SimpleNamespace(name="U@h")), _stats_store=_S())
        h = object.__new__(EneruAPIHandler)
        h.api_config = _parse_cfg("ups:\n  name: U@h\n")
        h.api_source = SimpleNamespace(_monitors=[mon])
        h.api_auth = None
        h.api_sessions = None
        h.headers = {}
        h.path = "/api/v1/ups/U@h/power?from=0&to=300"
        status, _, payload = h._route()
        assert status == 200
        assert payload["data"][0]["watts"] == 120.0
        # Unknown UPS -> 404.
        h.path = "/api/v1/ups/nope/power"
        assert h._route()[0] == 404


class TestStatusHelperGuards:
    """The v6.1 status helpers fail soft (return None) so a stats/store hiccup
    never breaks the status payload."""

    @pytest.mark.unit
    def test_self_test_no_store_is_none(self):
        from types import SimpleNamespace
        assert _self_test_for_monitor(SimpleNamespace(_stats_store=None)) is None

    @pytest.mark.unit
    def test_self_test_store_error_is_none(self):
        from types import SimpleNamespace

        class _S:
            def latest_self_test(self):
                raise RuntimeError("db gone")
        assert _self_test_for_monitor(SimpleNamespace(_stats_store=_S())) is None

    @pytest.mark.unit
    def test_energy_error_is_none(self):
        from types import SimpleNamespace

        class _S:
            def power_samples(self, *a):
                raise RuntimeError("db gone")
        cfg = SimpleNamespace(energy=SimpleNamespace(
            enabled=True, cost_per_kwh=None, currency="USD", cost_format=None))
        mon = SimpleNamespace(_stats_store=_S(), config=cfg)
        assert _energy_for_monitor(mon) is None

    @pytest.mark.unit
    def test_battery_health_error_is_none(self):
        from types import SimpleNamespace
        # A state whose _lock blows up -> guarded -> None. Use a real
        # context-manager class: `with` looks up __enter__/__exit__ on the TYPE,
        # so dunders set as SimpleNamespace instance attrs would never fire and
        # the test would pass for the wrong reason (TypeError, not RuntimeError).
        class _BadLock:
            def __enter__(self):
                raise RuntimeError("x")

            def __exit__(self, *a):
                return False
        mon = SimpleNamespace(state=SimpleNamespace(_lock=_BadLock()))
        assert _battery_health_for_monitor(mon) is None


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


class TestShutdownPlanEndpoint:
    @pytest.mark.unit
    def test_shutdown_plan_route(self):
        from types import SimpleNamespace
        from eneru.api import EneruAPIHandler
        cfg = _parse_cfg("ups:\n  name: U@h\n")
        mon = SimpleNamespace(config=cfg)
        h = object.__new__(EneruAPIHandler)
        h.api_config = cfg
        h.api_source = SimpleNamespace(_monitors=[mon])
        h.api_auth = None
        h.api_sessions = None
        h.headers = {}
        h.path = "/api/v1/ups/U@h/shutdown-plan"
        status, _, payload = h._route()
        assert status == 200
        ids = [p["id"] for p in payload["plan"]["phases"]]
        assert ids[0] == "vms" and ids[-1] == "local-poweroff"
        # Unknown UPS -> 404.
        h.path = "/api/v1/ups/nope/shutdown-plan"
        assert h._route()[0] == 404


class TestBatteryHealthHistoryEndpoint:
    @pytest.mark.unit
    def test_history_route(self):
        from types import SimpleNamespace
        from eneru.api import EneruAPIHandler

        class _S:
            def query_battery_health(self, a, b):
                return [{"ts": 100, "score": 90.0}]
        cfg = _parse_cfg("ups:\n  name: U@h\n")
        mon = SimpleNamespace(config=cfg, _stats_store=_S())
        h = object.__new__(EneruAPIHandler)
        h.api_config = cfg
        h.api_source = SimpleNamespace(_monitors=[mon])
        h.api_auth = None
        h.api_sessions = None
        h.headers = {}
        h.path = "/api/v1/ups/U@h/battery-health-history?from=0&to=200"
        status, _, payload = h._route()
        assert status == 200
        assert payload["data"][0]["score"] == 90.0
        # Unknown UPS -> 404.
        h.path = "/api/v1/ups/nope/battery-health-history"
        assert h._route()[0] == 404
