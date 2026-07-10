"""Unit tests for periodic reports (src/eneru/reports.py)."""

import time
from datetime import datetime, timezone

import pytest
import yaml

from eneru import reports
from eneru.config import ConfigLoader
from eneru.stats import StatsStore

DAY = 86400.0
# 2026-06-01 10:00 UTC: a Monday that is also the 1st of the month and well past
# the 08:00 schedule, so the daily/weekly/monthly due-paths are deterministically
# due regardless of the wall clock or calendar day the suite runs on. Paired with
# an explicit tz=UTC so the schedule math never depends on the host timezone.
DUE_TS = 1780308000.0


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
    def test_csv_neutralizes_formula_injection(self):
        """ISS-063: a cell whose first char is = + - @ (or a leading tab/CR)
        is prefixed with ' so a spreadsheet renders it literally instead of
        executing it as a formula."""
        sources = {
            "ups_name": "=cmd|'/c calc'!A1",
            "events": [(1000, "=1+1", "@SUM(A1:A9)")],
        }
        out = reports.build_report("daily", sources, include=["events"],
                                   fmt="csv")
        csv_text = out["csv"]
        # The dangerous cells are quoted; no raw formula-leading cell remains.
        assert "'=cmd" in csv_text
        assert "'=1+1" in csv_text
        assert "'@SUM(A1:A9)" in csv_text

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

class TestPeriodStart:
    @pytest.mark.unit
    def test_daily_is_local_midnight(self):
        # daily report window starts at local midnight, NOT now-24h.
        from datetime import datetime
        now_dt = datetime(2026, 6, 29, 14, 30, 0)
        now = now_dt.timestamp()
        start = reports._period_start("daily", now)
        assert start == int(datetime(2026, 6, 29).timestamp())
        # rolling 24h would have been ~14.5h earlier than midnight
        assert start != int(now - 24 * 3600)

    @pytest.mark.unit
    def test_monthly_is_first_of_month(self):
        # monthly report window starts on the 1st, NOT now-30d.
        from datetime import datetime
        now_dt = datetime(2026, 6, 29, 14, 30, 0)
        now = now_dt.timestamp()
        start = reports._period_start("monthly", now)
        assert start == int(datetime(2026, 6, 1).timestamp())
        assert start != int(now - 30 * 86400)

    @pytest.mark.unit
    def test_weekly_stays_rolling(self):
        # weekly has no calendar anchor -> 7-day rolling window.
        now = time.time()
        start = reports._period_start("weekly", now)
        assert start == int(now - 7 * 86400)


class TestGather:
    @pytest.mark.unit
    def test_gathers_from_store(self, store):
        now = time.time()
        store.log_event("DAEMON_START", "boot")
        store.log_event("ON_BATTERY", "outage")
        store.record_battery_health(75.0, {"runtime": 80.0}, ts=int(now))
        cfg = _config("ups:\n  name: U@h\nenergy:\n  enabled: true\n")
        sources = reports.gather_report_sources(
            store, "U@h", cfg.energy, period="weekly", now=now)
        assert sources["ups_name"] == "U@h"
        assert sources["uptime"]["daemon_starts"] == 1
        assert sources["battery_health"]["score"] == 75.0
        assert any(e[1] == "ON_BATTERY" for e in sources["events"])
        assert "todayKwh" in sources["energy"]

    @pytest.mark.unit
    def test_report_confidence_from_store_detail(self, store):
        # The store row carries confidence in `detail`, not top-level — the
        # report must render the real value, not "(confidence 0%)".
        now = time.time()
        store.record_battery_health(82.0, {"runtime": 100.0},
                                    detail={"confidence": 0.7}, ts=int(now))
        cfg = _config("ups:\n  name: U@h\n")
        sources = reports.gather_report_sources(
            store, "U@h", cfg.energy, period="daily", now=now)
        body = reports.build_report(
            "daily", sources, include=["battery_health"])["body"]
        assert "confidence 70%" in body
        assert "confidence 0%" not in body

    @pytest.mark.unit
    def test_daily_events_and_uptime_cover_previous_full_day(self, store):
        """F-082: yesterday evening appears in this morning's digest."""
        previous_evening = datetime(2026, 6, 28, 19, 0).timestamp()
        previous_boot = datetime(2026, 6, 28, 7, 0).timestamp()
        current_morning = datetime(2026, 6, 29, 7, 0).timestamp()
        send_time = datetime(2026, 6, 29, 8, 0).timestamp()
        store.log_event("DAEMON_START", "yesterday boot", ts=int(previous_boot))
        store.log_event("ON_BATTERY", "evening outage", ts=int(previous_evening))
        store.log_event("DAEMON_START", "today boot", ts=int(current_morning))
        store.log_event("ON_BATTERY", "today outage", ts=int(current_morning))
        cfg = _config("ups:\n  name: U@h\n")

        sources = reports.gather_report_sources(
            store, "U@h", cfg.energy, period="daily", now=send_time,
        )

        assert [event[2] for event in sources["events"]] == ["evening outage"]
        assert sources["uptime"]["daemon_starts"] == 1


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
        now = DUE_TS
        # last sent 2 days ago -> definitely before today's occurrence -> due
        store.set_meta("last_report_sent_daily", str(int(now - 2 * DAY)))
        periods = reports.maybe_send_due_reports(
            cfg, store, "U@h",
            lambda b, t, c: sent.append((t, c)), now=now, tz=timezone.utc)
        assert periods == ["daily"]
        assert sent == [("info", "report")]
        # immediate second call: last is now -> not due -> no resend
        sent.clear()
        periods = reports.maybe_send_due_reports(
            cfg, store, "U@h", lambda b, t, c: sent.append(c),
            now=now + 60, tz=timezone.utc)
        assert periods == [] and sent == []

    @pytest.mark.unit
    def test_meta_write_failure_does_not_send_and_no_resend(self, store):
        # F-028: if the dedup-key meta write fails (silently swallowed), the
        # digest must NOT be sent -- and it must not be re-sent every tick either.
        # We stamp meta FIRST and gate the send on its success, so a failing
        # set_meta yields zero sends across repeated ticks.
        cfg = self._cfg()
        now = DUE_TS
        store.set_meta("last_report_sent_daily", str(int(now - 2 * DAY)))
        sent = []
        from unittest.mock import patch
        with patch.object(store, "set_meta", return_value=False):
            for tick in range(3):
                periods = reports.maybe_send_due_reports(
                    cfg, store, "U@h",
                    lambda b, t, c: sent.append(c),
                    now=now + tick, tz=timezone.utc)
                assert periods == []          # nothing "sent"
        assert sent == []                     # never enqueued -> no duplicate spam

    @pytest.mark.unit
    def test_build_failure_preserves_previous_stamp(self, store, monkeypatch):
        """F-081: a render failure retries instead of burning the cadence."""
        cfg = self._cfg()
        now = DUE_TS
        old = str(int(now - 2 * DAY))
        store.set_meta("last_report_sent_daily", old)
        monkeypatch.setattr(
            reports, "build_report",
            lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("render")),
        )

        with pytest.raises(RuntimeError, match="render"):
            reports.maybe_send_due_reports(
                cfg, store, "U@h", lambda *args: None,
                now=now, tz=timezone.utc,
            )

        assert store.get_meta("last_report_sent_daily") == old

    @pytest.mark.unit
    def test_enqueue_failure_restores_previous_stamp(self, store):
        """F-081: a rejected queue handoff remains due next tick."""
        cfg = self._cfg()
        now = DUE_TS
        old = str(int(now - 2 * DAY))
        store.set_meta("last_report_sent_daily", old)

        def reject(*_args):
            raise RuntimeError("queue down")

        with pytest.raises(RuntimeError, match="queue down"):
            reports.maybe_send_due_reports(
                cfg, store, "U@h", reject, now=now, tz=timezone.utc,
            )

        assert store.get_meta("last_report_sent_daily") == old

    @pytest.mark.unit
    def test_csv_format_delivers_csv_block(self, store):
        # reports.format: csv must actually deliver the CSV (the channel is
        # text-only, so it rides under the summary) — not silently send text.
        cfg = _config("ups:\n  name: U@h\n"
                      "reports:\n  enabled: true\n  daily: true\n  time: '08:00'\n"
                      "  format: csv\n")
        bodies = []
        now = DUE_TS
        store.set_meta("last_report_sent_daily", str(int(now - 2 * DAY)))
        reports.maybe_send_due_reports(
            cfg, store, "U@h", lambda b, t, c: bodies.append(b),
            now=now, tz=timezone.utc)
        assert bodies and "--- CSV ---" in bodies[0]
        assert "timestamp,event_type,detail" in bodies[0]

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
    def test_defaults_now_when_omitted(self, store):
        # First sight with no explicit `now` -> seeds the baseline (exercises the
        # default-now branch), does not send.
        cfg = self._cfg()
        assert reports.maybe_send_due_reports(cfg, store, "U@h", lambda *a: None) == []
        assert store.get_meta("last_report_sent_daily") is not None

    @pytest.mark.unit
    def test_schedule_error_is_skipped(self, store, monkeypatch):
        cfg = self._cfg()
        monkeypatch.setattr(reports, "schedule_for_period",
                            lambda *a, **k: (_ for _ in ()).throw(ValueError("bad")))
        assert reports.maybe_send_due_reports(
            cfg, store, "U@h", lambda *a: None, now=time.time()) == []

    @pytest.mark.unit
    def test_running_since_uses_wide_lookback(self, store):
        # A daemon that started 40 days ago (outside a DAILY window) must still
        # report its real start time, not "unknown".
        now = time.time()
        store.log_event("DAEMON_START", ts=int(now - 40 * DAY))
        sources = reports.gather_report_sources(
            store, "U@h", _config("ups:\n  name: U@h\n").energy,
            period="daily", now=now)
        assert sources["uptime"]["since"] is not None
        assert sources["uptime"]["daemon_starts"] == 0   # none inside the daily window

    @pytest.mark.unit
    def test_weekly_and_monthly_due(self, store):
        cfg = _config("ups:\n  name: U@h\nreports:\n  enabled: true\n"
                      "  weekly: true\n  monthly: true\n  time: '08:00'\n"
                      "  weekly_day: monday\n  monthly_day: 1\n")
        sent = []
        # DUE_TS is a Monday and the 1st of the month, so both the weekly
        # (Monday) and monthly (day 1) occurrences are in the past at this now.
        now = DUE_TS
        store.set_meta("last_report_sent_weekly", str(int(now - 30 * DAY)))
        store.set_meta("last_report_sent_monthly", str(int(now - 90 * DAY)))
        periods = reports.maybe_send_due_reports(
            cfg, store, "U@h", lambda b, t, c: sent.append(c),
            now=now, tz=timezone.utc)
        assert set(periods) == {"weekly", "monthly"}
        assert sent == ["report", "report"]

    @pytest.mark.unit
    def test_corrupt_meta_treated_as_unrun(self, store):
        cfg = self._cfg()
        store.set_meta("last_report_sent_daily", "garbage")
        # corrupt last -> treated as None -> seed, not send
        assert reports.maybe_send_due_reports(
            cfg, store, "U@h", lambda *a: None, now=time.time()) == []


class TestAggregate:
    @pytest.mark.unit
    def test_build_aggregate_report_has_per_ups_sections(self):
        s1 = {"ups_name": "A@h", "events": [], "uptime": {"daemon_starts": 0, "since": None}}
        s2 = {"ups_name": "B@h", "events": [], "uptime": {"daemon_starts": 0, "since": None}}
        content = reports.build_aggregate_report("daily", [s1, s2], include=["uptime"])
        assert "A@h" in content["body"] and "B@h" in content["body"]
        assert "2 UPS" in content["subject"]

    @pytest.mark.unit
    def test_multi_covers_every_unit_once(self, tmp_path):
        cfg = _config("ups:\n  - name: A@h\n  - name: B@h\n"
                      "reports:\n  enabled: true\n  daily: true\n  time: '08:00'\n")
        s1 = StatsStore(tmp_path / "a.db")
        s1.open()
        s2 = StatsStore(tmp_path / "b.db")
        s2.open()
        try:
            now = DUE_TS   # fixed due timestamp (+ tz=UTC) so the due-path is
            s1.set_meta("last_report_sent_daily", str(int(now - 2 * DAY)))  # deterministic
            bodies = []
            units = [("A@h", s1, cfg.energy), ("B@h", s2, cfg.energy)]
            sent = reports.maybe_send_due_reports_multi(
                cfg, units, s1, lambda b, t, c: bodies.append(b),
                now=now, tz=timezone.utc)
            assert sent == ["daily"]
            assert len(bodies) == 1
            assert bodies[0].count("A@h") == 1 and bodies[0].count("B@h") == 1
            # dedup stamp lands in the designated meta store
            assert s1.get_meta("last_report_sent_daily") == str(int(now))
        finally:
            s1.close()
            s2.close()

    @pytest.mark.unit
    def test_multi_gather_failure_preserves_previous_stamp(
        self, tmp_path, monkeypatch,
    ):
        cfg = _config("ups:\n  - name: A@h\nreports:\n  enabled: true\n"
                      "  daily: true\n  time: '08:00'\n")
        store = StatsStore(tmp_path / "multi-failure.db")
        store.open()
        try:
            old = str(int(DUE_TS - 2 * DAY))
            store.set_meta("last_report_sent_daily", old)
            monkeypatch.setattr(
                reports, "gather_report_sources",
                lambda *args, **kwargs: (_ for _ in ()).throw(
                    RuntimeError("gather")),
            )

            with pytest.raises(RuntimeError, match="gather"):
                reports.maybe_send_due_reports_multi(
                    cfg, [("A@h", store, cfg.energy)], store,
                    lambda *args: None, now=DUE_TS, tz=timezone.utc,
                )

            assert store.get_meta("last_report_sent_daily") == old
        finally:
            store.close()

    @pytest.mark.unit
    def test_multi_enqueue_failure_restores_previous_stamp(self, tmp_path):
        cfg = _config("ups:\n  - name: A@h\nreports:\n  enabled: true\n"
                      "  daily: true\n  time: '08:00'\n")
        store = StatsStore(tmp_path / "multi-enqueue-failure.db")
        store.open()
        try:
            old = str(int(DUE_TS - 2 * DAY))
            store.set_meta("last_report_sent_daily", old)

            def reject(*_args):
                raise RuntimeError("queue down")

            with pytest.raises(RuntimeError, match="queue down"):
                reports.maybe_send_due_reports_multi(
                    cfg, [("A@h", store, cfg.energy)], store, reject,
                    now=DUE_TS, tz=timezone.utc,
                )

            assert store.get_meta("last_report_sent_daily") == old
        finally:
            store.close()

    @pytest.mark.unit
    def test_multi_disabled_or_empty(self, tmp_path):
        off = _config("ups:\n  - name: A@h\nreports:\n  enabled: false\n  daily: true\n")
        s = StatsStore(tmp_path / "m.db")
        s.open()
        try:
            assert reports.maybe_send_due_reports_multi(
                off, [("A@h", s, off.energy)], s, lambda *a: None) == []
            on = _config("ups:\n  - name: A@h\nreports:\n  enabled: true\n  daily: true\n")
            assert reports.maybe_send_due_reports_multi(
                on, [], s, lambda *a: None) == []          # no units
            assert reports.maybe_send_due_reports_multi(
                on, [("A@h", s, on.energy)], None, lambda *a: None) == []  # no meta store
        finally:
            s.close()

    @pytest.mark.unit
    def test_multi_first_sight_seeds_and_corrupt_meta(self, tmp_path):
        cfg = _config("ups:\n  - name: A@h\nreports:\n  enabled: true\n"
                      "  daily: true\n  time: '08:00'\n")
        s = StatsStore(tmp_path / "m.db")
        s.open()
        try:
            # First sight: no explicit now -> seeds baseline, sends nothing.
            assert reports.maybe_send_due_reports_multi(
                cfg, [("A@h", s, cfg.energy)], s, lambda *a: None) == []
            assert s.get_meta("last_report_sent_daily") is not None
            # Corrupt meta -> treated as unrun -> reseeds, still no send.
            s.set_meta("last_report_sent_daily", "garbage")
            assert reports.maybe_send_due_reports_multi(
                cfg, [("A@h", s, cfg.energy)], s, lambda *a: None,
                now=time.time()) == []
        finally:
            s.close()

    @pytest.mark.unit
    def test_multi_schedule_error_is_skipped(self, tmp_path, monkeypatch):
        cfg = _config("ups:\n  - name: A@h\nreports:\n  enabled: true\n  daily: true\n")
        s = StatsStore(tmp_path / "m.db")
        s.open()
        monkeypatch.setattr(reports, "schedule_for_period",
                            lambda *a, **k: (_ for _ in ()).throw(ValueError("bad")))
        try:
            assert reports.maybe_send_due_reports_multi(
                cfg, [("A@h", s, cfg.energy)], s, lambda *a: None,
                now=time.time()) == []
        finally:
            s.close()


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
