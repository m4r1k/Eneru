"""Tests for the BatteryMonitorMixin battery-health score + replacement
prediction (src/eneru/health/battery.py). Uses a lightweight harness around
the mixin with a real Config, MonitorState, and StatsStore.
"""

import time

import pytest

from eneru.config import ConfigLoader
from eneru.health.battery import (
    _META_NOMINAL_RUNTIME,
    _META_REPLACEMENT_PREDICTED,
    BatteryMonitorMixin,
)
from eneru.state import MonitorState
from eneru.stats import StatsStore

DAY = 86400.0


class _Mon(BatteryMonitorMixin):
    def __init__(self, config, store):
        self.config = config
        self.state = MonitorState()
        self._stats_store = store
        self.logs = []
        self.notifications = []

    def _log_message(self, m):
        self.logs.append(m)

    def _send_notification(self, body, ntype, category="general"):
        self.notifications.append((body, category))


def _config(text="ups:\n  name: U@h\n"):
    return ConfigLoader._parse_config(__import__("yaml").safe_load(text))


@pytest.fixture
def store(tmp_path):
    s = StatsStore(tmp_path / "bh.db")
    s.open()
    yield s
    s.close()


# --------------------------------------------------------------------------
# compute: unknown vs healthy
# --------------------------------------------------------------------------

class TestCompute:
    @pytest.mark.unit
    def test_thin_telemetry_is_unknown_not_healthy(self, store):
        # No nominal runtime, no install date, no self-test, no anomalies ->
        # only the anomaly term is available -> below MIN_CONFIDENCE -> unknown.
        mon = _Mon(_config(), store)
        mon.state.latest_battery_charge = "50"
        mon.state.latest_runtime = ""        # no runtime
        health = mon._compute_battery_health(mon.config.battery_health, time.time())
        assert health["score"] is None          # unknown, NOT a confident 100
        assert health["availableTerms"] == ["anomaly"]

    @pytest.mark.unit
    def test_good_telemetry_yields_score(self, store):
        cfg = _config(
            "ups:\n  name: U@h\n"
            "battery_health:\n  nominal_runtime_seconds: 1800\n"
            "  battery_install_date: '2025-06-01'\n  expected_life_years: 5\n")
        store.record_self_test("test.battery.start", "scheduler")
        latest = store.latest_self_test()
        store.update_self_test_result(latest["id"], result_raw="passed",
                                      result_enum="passed")
        mon = _Mon(cfg, store)
        mon.state.latest_battery_charge = "100"
        mon.state.latest_runtime = "1800"       # exactly nominal -> 100
        health = mon._compute_battery_health(cfg.battery_health,
                                             time.mktime((2026, 6, 1, 0, 0, 0, 0, 0, -1)))
        assert health["score"] is not None
        assert health["score"] > 80
        assert "runtime" in health["availableTerms"]
        assert "self_test" in health["availableTerms"]
        assert "age" in health["availableTerms"]

    @pytest.mark.unit
    def test_nominal_runtime_learned_at_full_charge(self, store):
        mon = _Mon(_config(), store)
        mon.state.latest_battery_charge = "100"
        mon.state.latest_runtime = "2400"
        mon._compute_battery_health(mon.config.battery_health, time.time())
        assert store.get_meta(_META_NOMINAL_RUNTIME) == "2400"

    @pytest.mark.unit
    def test_nominal_not_learned_below_full_charge(self, store):
        mon = _Mon(_config(), store)
        mon.state.latest_battery_charge = "80"   # not full
        mon.state.latest_runtime = "2400"
        mon._compute_battery_health(mon.config.battery_health, time.time())
        assert store.get_meta(_META_NOMINAL_RUNTIME) is None

    @pytest.mark.unit
    def test_learned_nominal_corrupt_meta_is_none(self, store):
        store.set_meta(_META_NOMINAL_RUNTIME, "not-a-number")
        mon = _Mon(_config(), store)
        assert mon._learned_nominal_runtime() is None

    @pytest.mark.unit
    def test_capacity_term_from_stored_runtime_history(self, store):
        # Two prior health rows with declining runtime in detail -> the
        # capacity (runtime-trend) term becomes available.
        cfg = _config("ups:\n  name: U@h\n"
                      "battery_health:\n  nominal_runtime_seconds: 1800\n")
        now = time.time()
        store.record_battery_health(80.0, {}, detail={"runtime_s": 1800},
                                    ts=int(now - 10 * DAY))
        store.record_battery_health(75.0, {}, detail={"runtime_s": 1600},
                                    ts=int(now - 5 * DAY))
        mon = _Mon(cfg, store)
        mon.state.latest_battery_charge = "100"
        mon.state.latest_runtime = "1700"
        health = mon._compute_battery_health(cfg.battery_health, now)
        assert "capacity" in health["availableTerms"]

    @pytest.mark.unit
    def test_compute_safe_without_store(self):
        mon = _Mon(_config(), None)
        mon.state.latest_battery_charge = "50"
        health = mon._compute_battery_health(mon.config.battery_health, time.time())
        assert health["score"] is None  # only anomaly term -> unknown


# --------------------------------------------------------------------------
# periodic update: persistence
# --------------------------------------------------------------------------

class TestPeriodicUpdate:
    @pytest.mark.unit
    def test_persists_and_publishes(self, store):
        cfg = _config(
            "ups:\n  name: U@h\n"
            "battery_health:\n  nominal_runtime_seconds: 1800\n"
            "  battery_install_date: '2025-06-01'\n")
        mon = _Mon(cfg, store)
        mon.state.latest_battery_charge = "100"
        mon.state.latest_runtime = "1800"
        now = time.time()
        mon._update_battery_health_periodic(now)
        # state published
        assert mon.state.latest_battery_health is not None
        # row persisted
        rows = store.query_battery_health(int(now - 10), int(now + 10))
        assert len(rows) == 1
        assert rows[0]["detail"]["runtime_s"] == 1800.0

    @pytest.mark.unit
    def test_disabled_does_nothing(self, store):
        cfg = _config("ups:\n  name: U@h\nbattery_health:\n  enabled: false\n")
        mon = _Mon(cfg, store)
        mon._update_battery_health_periodic(time.time())
        assert mon.state.latest_battery_health is None

    @pytest.mark.unit
    def test_disable_clears_previously_published_block(self, store):
        # A reload to enabled:false must clear any stale block from the status
        # surfaces, not leave the last score frozen forever.
        cfg = _config("ups:\n  name: U@h\nbattery_health:\n  enabled: false\n")
        mon = _Mon(cfg, store)
        mon.state.latest_battery_health = {"score": 88}   # left over from when on
        mon._update_battery_health_periodic(time.time())
        assert mon.state.latest_battery_health is None

    @pytest.mark.unit
    def test_nominal_not_learned_when_config_pins_it(self, store):
        cfg = _config("ups:\n  name: U@h\n"
                      "battery_health:\n  nominal_runtime_seconds: 1800\n")
        mon = _Mon(cfg, store)
        mon._maybe_learn_nominal_runtime(100.0, 2400.0)   # full charge, but pinned
        assert store.get_meta(_META_NOMINAL_RUNTIME) is None

    @pytest.mark.unit
    def test_no_store_is_safe(self):
        mon = _Mon(_config(), None)
        mon.state.latest_battery_charge = "100"
        mon.state.latest_runtime = "1800"
        # must not raise even with no stats store
        mon._update_battery_health_periodic(time.time())


# --------------------------------------------------------------------------
# replacement prediction
# --------------------------------------------------------------------------

class TestPrediction:
    @pytest.mark.unit
    def test_declining_trend_fires_once(self, store):
        cfg = _config(
            "ups:\n  name: U@h\n"
            "battery_health:\n  replacement:\n    threshold_score: 50\n"
            "    horizon_days: 90\n    min_history_days: 14\n")
        mon = _Mon(cfg, store)
        now = time.time()
        # Seed a clearly declining score series spanning >14 days.
        store.record_battery_health(80.0, {}, ts=int(now - 30 * DAY))
        store.record_battery_health(67.0, {}, ts=int(now - 15 * DAY))
        store.record_battery_health(55.0, {}, ts=int(now))
        mon._maybe_predict_replacement(cfg.battery_health, now)
        assert any("Replacement Predicted" in body for body, _ in mon.notifications)
        # event logged
        events = store.query_battery_health(0, int(now + 1))  # sanity store works
        assert events is not None
        # Dedup: a second call within the horizon does not re-notify.
        mon.notifications.clear()
        mon._maybe_predict_replacement(cfg.battery_health, now + DAY)
        assert mon.notifications == []

    @pytest.mark.unit
    def test_flat_trend_does_not_fire(self, store):
        cfg = _config("ups:\n  name: U@h\n")
        mon = _Mon(cfg, store)
        now = time.time()
        store.record_battery_health(90.0, {}, ts=int(now - 30 * DAY))
        store.record_battery_health(90.0, {}, ts=int(now))
        mon._maybe_predict_replacement(cfg.battery_health, now)
        assert mon.notifications == []
        assert store.get_meta(_META_REPLACEMENT_PREDICTED) is None


# --------------------------------------------------------------------------
# per-UPS override resolution
# --------------------------------------------------------------------------

class TestResolve:
    @pytest.mark.unit
    def test_per_ups_override_wins(self, store):
        cfg = _config(
            "battery_health:\n  update_interval: 3600\n"
            "ups:\n  - name: U1@h\n    battery_health:\n      update_interval: 900\n")
        mon = _Mon(cfg, store)
        resolved = mon._resolve_battery_health_config()
        assert resolved.update_interval == 900   # per-UPS override

    @pytest.mark.unit
    def test_falls_back_to_global(self, store):
        cfg = _config("battery_health:\n  update_interval: 1234\n"
                      "ups:\n  - name: U1@h\n")
        mon = _Mon(cfg, store)
        assert mon._resolve_battery_health_config().update_interval == 1234

    @pytest.mark.unit
    def test_malformed_config_logs_and_falls_back(self, store):
        # A malformed config shape (a group whose .ups access raises) must NOT
        # be swallowed silently: it falls back to the global config AND logs.
        cfg = _config("battery_health:\n  update_interval: 1234\n"
                      "ups:\n  - name: U1@h\n")

        class _BadGroup:
            @property
            def ups(self):
                raise AttributeError("ups missing")

        cfg.ups_groups = [_BadGroup()]
        mon = _Mon(cfg, store)
        resolved = mon._resolve_battery_health_config()
        assert resolved.update_interval == 1234           # global fallback
        assert any("battery-health config resolution failed" in m
                   for m in mon.logs)
