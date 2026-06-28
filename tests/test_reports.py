"""Unit tests for periodic reports (src/eneru/reports.py)."""

import time

import pytest
import yaml

from eneru import reports
from eneru.config import ConfigLoader
from eneru.stats import StatsStore

DAY = 86400.0


@pytest.fixture
def store(tmp_path):
    s = StatsStore(tmp_path / "rep.db")
    s.open()
    yield s
    s.close()


def _config(text):
    return ConfigLoader._parse_config(yaml.safe_load(text))


# --------------------------------------------------------------------------
# build_report (pure)
# --------------------------------------------------------------------------

class TestBuildReport:
    @pytest.mark.unit
    def test_text_report_sections(self):
        sources = {
            "ups_name": "U@h",
            "energy": {"todayKwh": 1.234, "monthKwh": 30.0,
                       "todayCostFormatted": "$0.30", "estimated": False},
            "battery_health": {"score": 82.0, "confidence": 0.7},
            "events": [(1000, "ON_BATTERY", "x"), (1001, "POWER_RESTORED", "y"),
                       (1002, "ON_BATTERY", "z")],
            "uptime": {"daemon_starts": 1, "since": 1000},
        }
        out = build = reports.build_report(
            "daily", sources,
            include=["energy", "battery_health", "events", "uptime"])
        body = out["body"]
        assert "daily report — U@h" in body
        assert "1.234 kWh" in body and "$0.30" in body
        assert "Score: 82/100" in body
        assert "ON_BATTERY: 2" in body and "POWER_RESTORED: 1" in body
        assert out["csv"] is None

    @pytest.mark.unit
    def test_unknown_health_and_energy(self):
        sources = {"ups_name": "U", "energy": {"todayKwh": None, "monthKwh": None},
                   "battery_health": None, "events": [], "uptime": {}}
        body = reports.build_report(
            "weekly", sources,
            include=["energy", "battery_health", "events"])["body"]
        assert "unknown" in body          # energy unknown
        assert "Score: unknown" in body
        assert "none" in body             # no events

    @pytest.mark.unit
    def test_estimated_energy_note(self):
        sources = {"ups_name": "U",
                   "energy": {"todayKwh": 1.0, "monthKwh": 2.0, "estimated": True}}
        body = reports.build_report("daily", sources, include=["energy"])["body"]
        assert "estimated" in body

    @pytest.mark.unit
    def test_csv_attachment(self):
        sources = {"ups_name": "U", "events": [(1000, "ON_BATTERY", "outage")]}
        out = reports.build_report("daily", sources, include=["events"],
                                   fmt="csv")
        assert out["csv"] is not None
        assert "timestamp,event_type,detail" in out["csv"]
        assert "ON_BATTERY" in out["csv"]

    @pytest.mark.unit
    def test_include_filters_sections(self):
        sources = {"ups_name": "U", "energy": {"todayKwh": 1.0, "monthKwh": 2.0},
                   "battery_health": {"score": 50, "confidence": 0.5}}
        body = reports.build_report("daily", sources, include=["energy"])["body"]
        assert "Energy:" in body
        assert "Battery health:" not in body


# --------------------------------------------------------------------------
# gather_report_sources
# --------------------------------------------------------------------------

class TestGather:
    @pytest.mark.unit
    def test_gathers_from_store(self, store):
        now = time.time()
        store.log_event("DAEMON_START", "boot")
        store.log_event("ON_BATTERY", "outage")
        store.record_battery_health(75.0, {"runtime": 80.0}, ts=int(now))
        cfg = _config("ups:\n  name: U@h\nenergy:\n  enabled: true\n")
        sources = reports.gather_report_sources(
            store, "U@h", cfg.energy, period="daily", now=now)
        assert sources["ups_name"] == "U@h"
        assert sources["uptime"]["daemon_starts"] == 1
        assert sources["battery_health"]["score"] == 75.0
        assert any(e[1] == "ON_BATTERY" for e in sources["events"])
        assert "todayKwh" in sources["energy"]


# --------------------------------------------------------------------------
# maybe_send_due_reports
# --------------------------------------------------------------------------

class TestMaybeSend:
    def _cfg(self):
        return _config("ups:\n  name: U@h\n"
                       "reports:\n  enabled: true\n  daily: true\n  time: '08:00'\n")

    @pytest.mark.unit
    def test_sends_when_due_and_dedups(self, store):
        cfg = self._cfg()
        sent = []
        now = time.time()
        # last sent 2 days ago -> definitely before today's occurrence -> due
        store.set_meta("last_report_sent_daily", str(int(now - 2 * DAY)))
        periods = reports.maybe_send_due_reports(
            cfg, store, "U@h",
            lambda b, t, c: sent.append((t, c)), now=now)
        assert periods == ["daily"]
        assert sent == [("info", "report")]
        # immediate second call: last is now -> not due -> no resend
        sent.clear()
        periods = reports.maybe_send_due_reports(
            cfg, store, "U@h", lambda b, t, c: sent.append(c), now=now + 60)
        assert periods == [] and sent == []

    @pytest.mark.unit
    def test_first_sight_seeds_without_sending(self, store):
        cfg = self._cfg()
        sent = []
        periods = reports.maybe_send_due_reports(
            cfg, store, "U@h", lambda *a: sent.append(a), now=time.time())
        assert periods == [] and sent == []          # seeded, not sent
        assert store.get_meta("last_report_sent_daily") is not None

    @pytest.mark.unit
    def test_disabled_does_nothing(self, store):
        cfg = _config("ups:\n  name: U@h\nreports:\n  enabled: false\n  daily: true\n")
        sent = []
        assert reports.maybe_send_due_reports(
            cfg, store, "U@h", lambda *a: sent.append(a)) == []
        assert sent == []

    @pytest.mark.unit
    def test_no_store_safe(self):
        cfg = self._cfg()
        assert reports.maybe_send_due_reports(cfg, None, "U@h", lambda *a: None) == []

    @pytest.mark.unit
    def test_weekly_and_monthly_due(self, store):
        cfg = _config("ups:\n  name: U@h\nreports:\n  enabled: true\n"
                      "  weekly: true\n  monthly: true\n  time: '08:00'\n"
                      "  weekly_day: monday\n  monthly_day: 1\n")
        sent = []
        now = time.time()
        store.set_meta("last_report_sent_weekly", str(int(now - 30 * DAY)))
        store.set_meta("last_report_sent_monthly", str(int(now - 90 * DAY)))
        periods = reports.maybe_send_due_reports(
            cfg, store, "U@h", lambda b, t, c: sent.append(c), now=now)
        assert set(periods) == {"weekly", "monthly"}
        assert sent == ["report", "report"]

    @pytest.mark.unit
    def test_corrupt_meta_treated_as_unrun(self, store):
        cfg = self._cfg()
        store.set_meta("last_report_sent_daily", "garbage")
        # corrupt last -> treated as None -> seed, not send
        assert reports.maybe_send_due_reports(
            cfg, store, "U@h", lambda *a: None, now=time.time()) == []


class TestScheduleForPeriod:
    @pytest.mark.unit
    def test_each_period(self):
        cfg = _config("ups:\n  name: U@h\nreports:\n  time: '07:00'\n"
                      "  weekly_day: friday\n  monthly_day: 15\n").reports
        assert reports.schedule_for_period("daily", cfg).kind == "daily"
        assert reports.schedule_for_period("weekly", cfg).weekday == 4
        assert reports.schedule_for_period("monthly", cfg).day == 15

    @pytest.mark.unit
    def test_unknown_period_raises(self):
        cfg = _config("ups:\n  name: U@h\n").reports
        with pytest.raises(ValueError):
            reports.schedule_for_period("hourly", cfg)


class TestGatherEnergyDisabled:
    @pytest.mark.unit
    def test_energy_disabled_empty_block(self, store):
        cfg = _config("ups:\n  name: U@h\nenergy:\n  enabled: false\n")
        sources = reports.gather_report_sources(
            store, "U@h", cfg.energy, period="daily", now=time.time())
        assert sources["energy"] == {}
