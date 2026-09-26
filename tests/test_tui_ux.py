"""6.2 UX round for `eneru monitor` (review items H3, H5, M1, M3-M9, M11, L8).

The TUI builds plain text "lines" first (view model) and paints them second,
so most of these tests read the words without a terminal. A few drive the
curses loop with a fake screen to prove the wiring (help overlay, sizing).
"""

import curses
import json
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from eneru import tui
from eneru.config import (
    Config,
    LocalShutdownConfig,
    LoggingConfig,
    RedundancyGroupConfig,
    RemoteServerConfig,
    StatsConfig,
    UPSConfig,
    UPSGroupConfig,
)
from eneru.stats import StatsStore
from eneru.utils import SEVERITY_CRIT, SEVERITY_OK, SEVERITY_WARN

from test_tui import _FakeTuiScreen, _FakeWin

NOW = 1_790_000_000.0


def _state(status="OL", *, epoch=NOW - 2, **extra):
    data = {"STATUS": status, "BATTERY": "100", "RUNTIME": "1500",
            "LOAD": "20", "INPUT_VOLTAGE": "230.0", "OUTPUT_VOLTAGE": "229.0",
            "TIMESTAMP": "2026-09-24 21:08:21", "EPOCH": f"{epoch:.3f}",
            "CHECK_INTERVAL": "5", "TIME_ON_BATTERY": "0", "DEPLETION_RATE": "0.0",
            "TRIGGER_ACTIVE": "0", "TRIGGER_REASON": "", "SELF_TEST_ATTRIBUTED": "0"}
    data.update({k: str(v) for k, v in extra.items()})
    return data


def _write_state(path: Path, state: dict, mtime=None):
    path.write_text("".join(f"{k}={v}\n" for k, v in state.items()))
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def _config(tmp_path, names=("ups-a@h",), *, local=(True,), redundancy=(),
            remotes=0):
    groups = []
    for i, name in enumerate(names):
        servers = [RemoteServerConfig(name=f"srv{j}", host=f"10.0.0.{j}",
                                      user="root", enabled=True)
                   for j in range(remotes)] if i == 0 else []
        groups.append(UPSGroupConfig(
            ups=UPSConfig(name=name, display_name=name.split("@")[0].upper()),
            is_local=bool(local[i]) if i < len(local) else False,
            remote_servers=servers))
    return Config(
        ups_groups=groups,
        redundancy_groups=list(redundancy),
        logging=LoggingConfig(state_file=str(tmp_path / "ups.state"),
                              file=str(tmp_path / "eneru.log")),
        local_shutdown=LocalShutdownConfig(enabled=True),
        statistics=StatsConfig(db_directory=str(tmp_path)),
    )


def _text(lines):
    return [line.text for line in lines]


def _progress(state="running", **extra):
    phases = [{"id": pid, "state": "pending", "startedAt": None,
               "finishedAt": None, "detail": ""}
              for pid in ("vms", "containers", "filesystem-sync",
                          "filesystem-unmount", "remote", "final-sync",
                          "local-poweroff")]
    phases[0]["state"] = "succeeded"
    phases[1]["state"] = "skipped"
    phases[2].update(state="running", startedAt=NOW - 6)
    data = {"scope": {"kind": "ups", "name": "ups-a@h"}, "runId": 1,
            "state": state, "reason": "Runtime 4m 40s below threshold 5m 0s",
            "startedAt": NOW - 42, "finishedAt": None, "phases": phases,
            "remotes": [{"server": "nas", "host": "10.0.0.5", "state": "failed",
                         "error": "timeout"}],
            "writtenAt": NOW - 1}
    data.update(extra)
    return data


# ---------------------------------------------------------------------------
# H5: freshness from EPOCH (or mtime), never the naive TIMESTAMP
# ---------------------------------------------------------------------------

class TestStateEpoch:

    @pytest.mark.unit
    def test_epoch_key_wins(self, tmp_path):
        assert tui.state_epoch({"EPOCH": "123.5"}, tmp_path) == (123.5, "epoch")

    @pytest.mark.unit
    def test_pre_62_daemon_falls_back_to_mtime(self, tmp_path):
        path = tmp_path / "s"
        _write_state(path, {"STATUS": "OL"}, mtime=NOW - 50)
        epoch, source = tui.state_epoch({"STATUS": "OL"}, path)
        assert source == "mtime" and abs(epoch - (NOW - 50)) < 1

    @pytest.mark.unit
    def test_nothing_available(self, tmp_path):
        assert tui.state_epoch(None, None) == (None, "none")
        assert tui.state_epoch({"EPOCH": "bad"}, tmp_path / "gone") == (None, "none")
        assert tui.state_epoch({"EPOCH": "0"}, None) == (None, "none")

    @pytest.mark.unit
    def test_state_for_outlook_fills_epoch_only_when_missing(self):
        assert tui._state_for_outlook(None, 5.0) is None
        assert tui._state_for_outlook({"STATUS": "OL"}, 5.0)["EPOCH"] == "5.0"
        assert tui._state_for_outlook({"EPOCH": "7"}, 5.0)["EPOCH"] == "7"
        assert "EPOCH" not in tui._state_for_outlook({"STATUS": "OL"}, None)


class TestFreshnessView:

    @pytest.mark.unit
    def test_fresh_line_uses_relative_age_and_local_clock(self):
        view = tui.freshness_view({"epoch": NOW - 3}, NOW)
        assert view["stale"] is False
        assert view["text"].startswith("Updated 3s ago (")
        assert "STALE" not in view["text"]

    @pytest.mark.unit
    def test_stale_after_threshold_from_outlook(self):
        data = {"epoch": NOW - 100,
                "outlook": {"freshness": {"staleAfterSeconds": 60}}}
        view = tui.freshness_view(data, NOW)
        assert view["stale"] is True
        assert "STALE (limit 1m 0s)" in view["text"]

    @pytest.mark.unit
    def test_never_updated(self):
        view = tui.freshness_view({"epoch": None}, NOW)
        assert view == {"age": None, "stale": True, "stale_after": 30,
                        "text": "Never updated"}

    @pytest.mark.unit
    def test_clock_text_same_day_other_day_and_bad(self):
        assert len(tui.clock_text(NOW - 5, NOW)) == 8          # HH:MM:SS
        assert len(tui.clock_text(NOW - 5 * 86400, NOW)) == 19  # with date
        assert tui.clock_text(None, NOW) == "?"
        assert tui.clock_text(float("nan"), NOW) == "?"


# ---------------------------------------------------------------------------
# M8: missing state explains where it looked
# ---------------------------------------------------------------------------

class TestMissingState:

    @pytest.mark.unit
    def test_reasons(self, tmp_path):
        assert tui.missing_state_reason(tmp_path / "nope" / "s") == "no-dir"
        assert tui.missing_state_reason(tmp_path / "s") == "missing"
        empty = tmp_path / "e"
        empty.write_text("")
        assert tui.missing_state_reason(empty) == "empty"
        with patch.object(tui.os, "access", return_value=False):
            assert tui.missing_state_reason(empty) == "unreadable"

    @pytest.mark.unit
    @pytest.mark.parametrize("reason,needle", [
        ("no-dir", "does not exist here"),
        ("unreadable", "not readable by this user"),
        ("empty", "is empty"),
        ("missing", "No state file at /x/s"),
    ])
    def test_lines_name_the_path_and_the_container_hint(self, reason, needle):
        lines = tui.missing_state_lines("/x/s", reason)
        assert needle in lines[0]
        assert "docker exec <container> eneru monitor" in lines[-1]
        assert all(len(line) <= 76 for line in lines)  # fits 80 columns


# ---------------------------------------------------------------------------
# Data collection
# ---------------------------------------------------------------------------

class TestCollect:

    @pytest.mark.unit
    def test_collect_group_data_builds_outlook(self, tmp_path):
        config = _config(tmp_path)
        _write_state(tmp_path / "ups.state", _state(epoch=time.time() - 1))
        data = tui.collect_group_data(config.ups_groups[0], config)
        assert data["epoch_source"] == "epoch"
        assert data["missing_reason"] is None
        assert data["outlook"]["statusSummary"]["label"] == "On mains"
        assert data["outlook"]["role"]["label"] == "Powers this host"

    @pytest.mark.unit
    def test_collect_group_data_without_state(self, tmp_path):
        config = _config(tmp_path)
        data = tui.collect_group_data(config.ups_groups[0], config, NOW)
        assert data["state"] is None
        assert data["missing_reason"] == "missing"
        assert data["state_path"].endswith("ups.state")

    @pytest.mark.unit
    def test_group_outlook_failure_is_swallowed(self, tmp_path):
        config = _config(tmp_path)
        with patch.object(tui, "state_file_outlook", side_effect=RuntimeError):
            assert tui.group_outlook(config.ups_groups[0], config,
                                     {"STATUS": "OL"}, NOW) is None

    @pytest.mark.unit
    def test_self_test_latch_read_only_on_battery(self, tmp_path):
        config = _config(tmp_path)
        group = config.ups_groups[0]
        assert tui._self_test_armed(group, config, {"STATUS": "OL"}) is False
        # On battery but no DB yet.
        assert tui._self_test_armed(group, config, {"STATUS": "OB"}) is False
        store = StatsStore(tui.stats_db_path_for(group, config))
        store.open()
        store.set_meta("self_test_failure_latched", str(NOW))
        store.close()
        state = {"STATUS": "OB DISCHRG", "ON_BATTERY_SINCE": "123"}
        assert tui._self_test_armed(group, config, state) is True
        assert tui._self_test_armed(
            group, config, dict(state, SELF_TEST_ATTRIBUTED="1")) is False

    @pytest.mark.unit
    def test_self_test_latch_close_error_is_swallowed(self, tmp_path):
        config = _config(tmp_path)

        class _Conn:
            def close(self):
                raise RuntimeError("boom")

        with patch.object(tui.StatsStore, "open_readonly", return_value=_Conn()), \
             patch.object(tui, "read_self_test_failure_armed", return_value=True):
            assert tui._self_test_armed(config.ups_groups[0], config,
                                        {"STATUS": "OB"}) is True

    @pytest.mark.unit
    def test_collect_redundancy_data(self, tmp_path):
        rg = RedundancyGroupConfig(name="rack-a", ups_sources=["ups-a@h", "ups-b@h"],
                                   min_healthy=2)
        config = _config(tmp_path, ("ups-a@h", "ups-b@h"), local=(False, False),
                         redundancy=[rg])
        groups_data = [
            {"name": "ups-a@h", "label": "UPS-A",
             "outlook_state": _state(epoch=NOW - 1)},
            {"name": "ups-b@h", "label": "UPS-B", "outlook_state": None},
        ]
        [data] = tui.collect_redundancy_data(config, groups_data, NOW)
        assert data["healthyCount"] == 1 and data["total"] == 2
        assert data["quorumLost"] is True
        assert data["labels"] == {"ups-a@h": "UPS-A", "ups-b@h": "UPS-B"}
        assert data["failingMembers"] == ["ups-b@h"]

    @pytest.mark.unit
    def test_collect_redundancy_data_error(self, tmp_path):
        rg = RedundancyGroupConfig(name="rack-a", ups_sources=["x"], min_healthy=1)
        config = _config(tmp_path, redundancy=[rg])
        with patch.object(tui, "redundancy_outlook_from_state_files",
                          side_effect=RuntimeError):
            [data] = tui.collect_redundancy_data(config, [])
        assert data["error"] is True and data["labels"] == {"x": "x"}


# ---------------------------------------------------------------------------
# H3: what happens next
# ---------------------------------------------------------------------------

def _trigger(tid, state, **kw):
    base = {"id": tid, "state": state, "comparison": "below", "unit": "",
            "value": None, "threshold": None, "etaSeconds": None,
            "label": tid}
    base.update(kw)
    return base


class TestTriggerChips:

    @pytest.mark.unit
    def test_short_duration(self):
        assert tui.short_duration(45) == "45s"
        assert tui.short_duration(81) == "1m 21s"
        assert tui.short_duration(120) == "2m"
        assert tui.short_duration(1251) == "20m"
        assert tui.short_duration(3900) == "1h 5m"
        assert tui.short_duration(None) == "?"
        assert tui.short_duration(-5) == "0s"

    @pytest.mark.unit
    def test_chip_variants(self):
        chip = tui.trigger_chip
        assert chip(_trigger("fsd", "fired")) is None
        assert chip(_trigger("lowBattery", "idle")) is None
        assert chip(_trigger("extendedTime", "disabled")) is None
        assert chip(_trigger("lowBattery", "ok", unit="%", value=45.0,
                             threshold=20, etaSeconds=1251)) == \
            "charge < 20%: 45% (~20m)"
        assert chip(_trigger("criticalRuntime", "fired", unit="s", value=280,
                             threshold=300)) == "runtime < 5m: FIRED at 4m 40s"
        assert chip(_trigger("criticalRuntime", "held", unit="s", value=280,
                             threshold=300, etaSeconds=20)) == \
            "runtime < 5m: met, held 20s"
        assert chip(_trigger("criticalRuntime", "unknown", unit="s",
                             threshold=300)) == "runtime < 5m: no reading"
        assert chip(_trigger("depletionRate", "ok", comparison="above",
                             unit="%/min", value=1.2, threshold=15.0)) == \
            "drain > 15%/min: 1.2%/min"
        assert chip(_trigger("extendedTime", "ok", comparison="longer",
                             unit="s", value=420, threshold=1200,
                             etaSeconds=781)) == "on battery > 20m: in 13m"
        assert chip(_trigger("extendedTime", "fired", comparison="longer",
                             unit="s", value=1300, threshold=1200)) == \
            "on battery > 20m: FIRED"
        assert chip(_trigger("selfTestFailure", "ok", comparison="longer",
                             unit="s", value=5, threshold=30)) == \
            "failed self-test + 30s on battery: ok"
        assert chip(_trigger("lowBattery", "ok", unit="%", value=None,
                             threshold="x")) == "charge < ?: ok"

    @pytest.mark.unit
    def test_headline(self):
        fired = {"next": {"state": "fired", "label": "Critical runtime"},
                 "firing": ["criticalRuntime"]}
        assert tui.outlook_headline(fired) == \
            "Shutdown condition met: critical runtime"
        held = {"next": {"state": "held", "label": "Low battery",
                         "etaSeconds": 20}, "stabilizing": True}
        assert tui.outlook_headline(held) == \
            "Low battery met, waiting 20s (stabilizing)"
        nxt = {"next": {"state": "ok", "label": "Critical runtime",
                        "etaSeconds": 81}}
        assert tui.outlook_headline(nxt) == \
            "Next trigger: critical runtime in ~1m 21s"
        assert tui.outlook_headline({"next": None}) == "No trigger is close"
        stab = {"next": None, "stabilizing": True, "stabilizationRemaining": 12}
        assert tui.outlook_headline(stab) == "No trigger is close; stabilizing 12s"

    @pytest.mark.unit
    def test_wrap_items(self):
        rows = tui.wrap_items("P: ", ["aaaa", "bbbb", "cccc"], 12, "   ")
        assert rows == ["P: aaaa", "   bbbb", "   cccc"]
        assert tui.wrap_items("P: ", [], 12, "  ") == []
        assert tui.wrap_items("P: ", ["a", "b"], 40, "  ") == ["P: a · b"]


class TestOutlookLines:
    """End to end from real state files through outlook.state_file_outlook."""

    def _data(self, tmp_path, state, *, remotes=1, local=True, now=NOW,
              local_shutdown=True):
        config = _config(tmp_path, remotes=remotes, local=(local,))
        config.local_shutdown.enabled = local_shutdown
        _write_state(tmp_path / "ups.state", state)
        return tui.collect_group_data(config.ups_groups[0], config, now)

    @pytest.mark.unit
    def test_on_mains_has_no_outlook_lines(self, tmp_path):
        data = self._data(tmp_path, _state())
        text = "\n".join(_text(tui.ups_block_lines(data, NOW, 80)))
        assert "Shutdown when" not in text
        assert "If a trigger fires" not in text

    @pytest.mark.unit
    def test_on_battery_near_runtime_trigger(self, tmp_path):
        # Default thresholds: runtime < 10m, charge < 20%, 15m on battery.
        state = _state("OB DISCHRG", BATTERY=45, RUNTIME=700, TIME_ON_BATTERY=420,
                       DEPLETION_RATE=1.2, ON_BATTERY_SINCE=int(NOW - 420))
        data = self._data(tmp_path, state)
        lines = tui.ups_block_lines(data, NOW, 80)
        text = _text(lines)
        assert lines[0].badge == ("ON BATTERY", SEVERITY_WARN, False)
        assert "   On battery 7m 2s · Next trigger: critical runtime in ~1m 41s" in text
        chips = " ".join(t for t in text if "Shutdown when" in t or t.startswith(" " * 18))
        assert "runtime < 10m: 11m (~1m 41s)" in chips
        assert "charge < 20%: 45% (~20m)" in chips
        assert "on battery > 15m: in 7m 59s" in chips
        assert ("   If a trigger fires: Shuts down this host and 1 remote server"
                in text)
        assert all(len(t) <= 80 for t in text if "Shutdown when" in t)

    @pytest.mark.unit
    def test_fired_trigger_escalates_badge_and_blinks(self, tmp_path):
        state = _state("OB DISCHRG", BATTERY=41, RUNTIME=280, TIME_ON_BATTERY=610)
        data = self._data(tmp_path, state)
        lines = tui.ups_block_lines(data, NOW, 80)
        assert lines[0].badge == ("SHUTDOWN TRIGGERED", SEVERITY_CRIT, True)
        assert any("Trigger fired -> Shuts down this host" in t for t in _text(lines))

    @pytest.mark.unit
    def test_monitor_only_ups_never_says_shutdown(self, tmp_path):
        """H1 parity: nothing is shut down here, so no red blinking badge."""
        state = _state("OB DISCHRG LB", BATTERY=10, RUNTIME=100, TIME_ON_BATTERY=610)
        # F-178: single-UPS mode powers the host off unless local_shutdown
        # is disabled, whatever is_local says; this UPS really does nothing.
        data = self._data(tmp_path, state, remotes=0, local=False,
                          local_shutdown=False)
        lines = tui.ups_block_lines(data, NOW, 80)
        assert lines[0].badge[0] == "LOW BATTERY"
        assert lines[0].badge[2] is False
        assert any("Notification only" in t for t in _text(lines))
        assert "· Monitoring only" in lines[0].text

    @pytest.mark.unit
    def test_advisory_trigger_reason_shown(self, tmp_path):
        state = _state("OB", TRIGGER_ACTIVE=1, TRIGGER_REASON="Runtime low",
                       TIME_ON_BATTERY=100)
        data = self._data(tmp_path, state)
        text = _text(tui.ups_block_lines(data, NOW, 80))
        assert "   Trigger: Runtime low" in text

    @pytest.mark.unit
    def test_failsafe_arming_renders_amber_with_its_text(self):
        """F-180: the FAILSAFE failed-poll countdown (``arming``) gets its own
        chip and headline, in amber (warn), never red and never "ok"."""
        arming = _trigger("failsafe", "arming", label="Connection lost on battery",
                          etaSeconds=10,
                          text="1 of 3 NUT polls failed · fires at 3")
        assert tui.trigger_chip(arming) == \
            "NUT lost: 1 of 3 NUT polls failed · fires at 3 (~10s)"
        assert tui.trigger_chip(dict(arming, etaSeconds=None)) == \
            "NUT lost: 1 of 3 NUT polls failed · fires at 3"
        assert tui.trigger_chip(dict(arming, text="")) == \
            "NUT lost: polls failing (~10s)"
        # A failsafe row that is merely ``ok`` stays chip-less.
        assert tui.trigger_chip(_trigger("failsafe", "ok")) is None
        trig = {"onBattery": True, "timeOnBattery": 60, "triggers": [arming],
                "firing": [], "next": arming, "action": {"label": "Shuts down this host"}}
        assert tui.outlook_headline(trig) == (
            "Connection lost on battery: 1 of 3 NUT polls failed · fires at 3, "
            "in ~10s")
        assert tui.outlook_headline({"next": dict(arming, text="",
                                                  etaSeconds=None)}) == \
            "Connection lost on battery: NUT polls failing, in ~0s"
        lines = tui.outlook_lines({"triggerOutlook": trig},
                                  {"STATUS": "OB", "TIME_ON_BATTERY": "60"}, 120)
        assert lines[0].text.endswith("fires at 3, in ~10s")
        assert lines[0].style == "warn"
        assert any("NUT lost: 1 of 3" in line.text for line in lines)

    @pytest.mark.unit
    def test_legacy_state_without_time_on_battery(self):
        ol = {"triggerOutlook": {"onBattery": True, "triggers": [], "next": None,
                                 "action": {"label": "X"}}}
        text = _text(tui.outlook_lines(ol, {"STATUS": "OB"}, 80))
        assert text[0] == "   On battery · No trigger is close"

    @pytest.mark.unit
    def test_stale_on_battery_pauses_countdowns(self, tmp_path):
        state = _state("OB DISCHRG", epoch=NOW - 600, TIME_ON_BATTERY=100)
        data = self._data(tmp_path, state)
        lines = tui.ups_block_lines(data, NOW, 80)
        text = _text(lines)
        assert lines[0].badge == ("STALE 10m", SEVERITY_WARN, False)
        assert text[1].startswith("   Last known: On battery (OB DISCHRG)")
        assert "STALE (limit 30s)" in text[2]
        assert text[3] == "   " + tui.STALE_HINT
        assert "countdowns paused" in text[4]
        assert not any("Shutdown when" in t for t in text)
        # Without an action label only the paused line is shown.
        assert len(tui.outlook_lines({"triggerOutlook": {"onBattery": True}},
                                     {}, 80, stale=True)) == 1

    @pytest.mark.unit
    def test_fallback_without_outlook(self):
        data = {"label": "A", "name": "a@h", "is_local": True,
                "state": {"STATUS": "OL", "BATTERY": "90"}, "epoch": NOW - 1,
                "resources": "none"}
        lines = tui.ups_block_lines(data, NOW, 80)
        assert lines[0].text.endswith("· Powers this host")
        assert lines[0].badge == ("ON MAINS", SEVERITY_OK, False)
        data["state"] = {"STATUS": ""}
        assert "Status unknown" not in _text(tui.ups_block_lines(data, NOW, 80))[1]
        data["epoch"] = NOW - 100
        assert "Status unknown" in _text(tui.ups_block_lines(data, NOW, 80))[1]

    @pytest.mark.unit
    def test_readings_skip_missing_values(self):
        assert tui.reading_parts({"BATTERY": "100", "RUNTIME": "3700", "LOAD": "9",
                                  "INPUT_VOLTAGE": "230", "OUTPUT_VOLTAGE": ""}) == [
            "Battery: 100% (1h 1m)", "Load: 9%", "Input: 230V"]
        assert tui.reading_parts({"RUNTIME": "90"}) == ["Runtime: 1m 30s"]
        assert tui.reading_parts({"BATTERY": "50", "OUTPUT_VOLTAGE": "229"}) == [
            "Battery: 50%", "Output: 229V"]
        data = {"label": "A", "name": "A", "state": {"STATUS": "OL"},
                "epoch": NOW, "resources": "none"}
        assert "No readings reported" in _text(tui.ups_block_lines(data, NOW))[1]

    @pytest.mark.unit
    def test_remote_health_failed_is_a_warning(self):
        data = {"label": "A", "name": "A", "state": None, "resources": "none",
                "remote_health_summary": "1 healthy, 1 failed"}
        last = tui.ups_block_lines(data, NOW)[-1]
        assert last.style == "warn" and last.priority == 1


# ---------------------------------------------------------------------------
# Shutdown progress (item 7)
# ---------------------------------------------------------------------------

class TestProgressLines:

    @pytest.mark.unit
    def test_idle_and_garbage(self):
        assert tui.progress_lines(None, NOW) == []
        assert tui.progress_lines({"state": "idle"}, NOW) == []
        assert tui.progress_lines("nope", NOW) == []

    @pytest.mark.unit
    def test_running(self):
        text = _text(tui.progress_lines(_progress(), NOW))
        assert text[0] == ("   SHUTDOWN IN PROGRESS: phase 3/7 Sync (running 6s),"
                           " started 42s ago")
        assert text[1] == "   Reason: Runtime 4m 40s below threshold 5m 0s"
        assert text[2] == ("   Phases -- Done: VMs | Running: Sync | To do: Unmount,"
                           " Remotes, Final sync, Poweroff | Skipped: Containers")
        assert text[3] == "   Remotes -- nas failed (timeout)"

    @pytest.mark.unit
    def test_running_edge_shapes(self):
        bare = {"state": "running", "phases": [], "remotes": []}
        assert _text(tui.progress_lines(bare, NOW)) == ["   SHUTDOWN IN PROGRESS"]
        done = _progress()
        for phase in done["phases"]:
            phase["state"] = "succeeded"
        head = tui.progress_lines(done, NOW)[0].text
        assert "phase 7/7 Poweroff" in head and "(running" not in head

    @pytest.mark.unit
    def test_finished_states(self):
        ok = _text(tui.progress_lines(_progress("succeeded", finishedAt=NOW - 300),
                                      NOW))
        assert ok[0] == "   Last shutdown run: succeeded 5m ago"
        failed = tui.progress_lines(_progress("failed", reason=""), NOW)
        assert failed[0].text == "   Last shutdown run: failed"
        assert failed[0].style == "crit"

    @pytest.mark.unit
    def test_silent_progress_with_stale_state_is_not_live(self):
        silent = _progress(writtenAt=NOW - 600)
        assert tui.progress_silent(silent, NOW, True) is True
        assert tui.progress_silent(silent, NOW, False) is False
        assert tui.progress_silent(_progress(), NOW, True) is False
        assert tui.progress_silent(_progress("succeeded"), NOW, True) is False
        assert tui.progress_silent(None, NOW, True) is False
        text = _text(tui.progress_lines(silent, NOW, stale=True))
        assert text[1] == "   No progress for 10m: is the daemon still running?"

    @pytest.mark.unit
    def test_dead_daemon_mid_shutdown_badge_is_stale(self):
        ol = {"statusSummary": {"state": "shutting_down", "label": "Shutting down",
                                "severity": SEVERITY_CRIT, "blink": True},
              "shutdownProgress": _progress(writtenAt=NOW - 600)}
        data = {"label": "A", "name": "A", "state": {"STATUS": "OB"},
                "epoch": NOW - 700, "outlook": ol, "resources": "none"}
        assert tui.ups_block_lines(data, NOW)[0].badge[0] == "STALE 11m"
        # Live progress (recent writes) keeps the blinking badge even when the
        # daemon has stopped rewriting the state file mid-shutdown.
        ol["shutdownProgress"] = _progress()
        assert tui.ups_block_lines(data, NOW)[0].badge == (
            "SHUTTING DOWN", SEVERITY_CRIT, True)


# ---------------------------------------------------------------------------
# M9: redundancy groups
# ---------------------------------------------------------------------------

class TestRedundancyLines:

    @pytest.mark.unit
    def test_quorum_lost(self):
        rg = {"name": "rack-a", "healthyCount": 1, "total": 2, "minHealthy": 2,
              "outlook": {"state": "quorum-lost", "severity": SEVERITY_CRIT,
                          "label": "Quorum lost → group shutdown runs",
                          "action": "Shuts down 2 remote servers"},
              "labels": {"u1@h": "UPS1", "u2@h": "UPS2"},
              "failingMembers": ["u2@h"],
              "members": {"u1@h": {"healthReason": "healthy"},
                          "u2@h": {"healthReason": "no data"}},
              "shutdownProgress": _progress()}
        lines = tui.redundancy_block_lines(rg, NOW, 80)
        text = _text(lines)
        assert text[0] == "   Redundancy group rack-a: 1/2 healthy, need 2"
        assert lines[0].badge == ("QUORUM LOST", SEVERITY_CRIT, False)
        assert text[1] == "     Quorum lost → group shutdown runs"
        assert lines[1].style == "crit"
        assert text[2] == ("     Members: UPS1: healthy · UPS2: no data"
                           " (counts as failed)")
        assert text[3] == "     Group shutdown: Shuts down 2 remote servers"
        assert lines[3].priority == 1
        assert text[4].startswith("     SHUTDOWN IN PROGRESS")

    @pytest.mark.unit
    def test_healthy_and_error(self):
        rg = {"name": "g", "healthyCount": 2, "total": 2, "minHealthy": 1,
              "outlook": {"state": "shutting-down", "severity": SEVERITY_OK,
                          "label": "", "action": ""},
              "members": {"a": {}}}
        lines = tui.redundancy_block_lines(rg, NOW)
        assert lines[0].badge == ("SHUTTING DOWN", SEVERITY_OK, True)
        assert _text(lines)[1] == "     Members: a: unknown"
        err = tui.redundancy_block_lines({"name": "g", "error": True}, NOW)
        assert err[0].badge[0] == "UNKNOWN"
        assert "status unavailable" in err[0].text


# ---------------------------------------------------------------------------
# L8 + layout
# ---------------------------------------------------------------------------

class TestPanelLayout:

    @pytest.mark.unit
    def test_config_panel_lines_order(self):
        groups = [{"label": "A", "name": "A", "state": None, "resources": "x"},
                  {"label": "B", "name": "B", "state": None, "resources": "y"}]
        rgs = [{"name": "g", "error": True}]
        text = _text(tui.config_panel_lines(groups, rgs, 80, NOW))
        assert text.count("") == 2
        assert text[-1].startswith("   Redundancy group g")

    @pytest.mark.unit
    def test_fit_lines_drops_least_important_first(self):
        L = tui.Line
        lines = [L("", "plain", 1, None), L("head", "bold", 0, None),
                 L("res", "plain", 2, None), L("", "plain", 1, None),
                 L("chips", "plain", 1, None), L("act", "bold", 0, None)]
        assert _text(tui.fit_lines(lines, 6)) == ["head", "res", "", "chips", "act"]
        assert _text(tui.fit_lines(lines, 4)) == ["head", "", "chips", "act"]
        assert _text(tui.fit_lines(lines, 3)) == ["head", "", "act"]
        assert _text(tui.fit_lines(lines, 2)) == ["head", "act"]
        assert _text(tui.fit_lines(lines, 1)) == ["head"]
        assert tui.fit_lines(lines, 0) == []

    @pytest.mark.unit
    def test_paint_line_badge_and_styles(self):
        win = _FakeWin(3, 40)
        with patch.object(curses, "color_pair", lambda n: n):
            tui.paint_line(win, 0, 40, tui.Line("   " + "x" * 50, "warn", 0,
                                                ("ON BATTERY", SEVERITY_WARN, False)))
            tui.paint_line(win, 1, 40, tui.Line("   plain", "crit", 0, None))
        row0 = "".join(win.cells.get((0, x), (" ", 0))[0] for x in range(40))
        assert "…" in row0 and "ON BATTERY" in row0
        assert win.cells[(0, 3)][1] & curses.A_BOLD  # warn text starts at indent
        assert (0, 0) not in win.cells                # indent left untouched
        assert win.cells[(1, 3)][1] == tui.C_STATUS_CRIT | curses.A_BOLD

    @pytest.mark.unit
    def test_style_attr_and_ellipsize(self):
        with patch.object(curses, "color_pair", lambda n: n):
            assert tui._style_attr("plain") == tui.C_GRAY_BG
            assert tui._style_attr("bold") == tui.C_GRAY_BG | curses.A_BOLD
            assert tui._style_attr("warn") == tui.C_STATUS_WARN | curses.A_BOLD
        assert tui._ellipsize("abcdef", 0) == ""
        assert tui._ellipsize("abc", 5) == "abc"
        assert tui._ellipsize("abcdef", 4) == "abc…"

    @pytest.mark.unit
    def test_render_config_panel_returns_rows(self):
        win = _FakeWin(6, 80)
        groups = [{"label": "A", "name": "A", "state": None, "resources": "x"}]
        with patch.object(curses, "color_pair", lambda n: n):
            rows = tui.render_config_panel(win, 0, 4, 80, groups, None, NOW)
        assert rows == 3  # fitted to the 3 rows below the padding

    @pytest.mark.unit
    def test_summary_attr_blink_only_when_asked(self):
        with patch.object(curses, "color_pair", lambda n: n):
            assert not tui.summary_attr({"severity": "warn"}) & curses.A_BLINK
            assert tui.summary_attr({"severity": "crit", "blink": True}) & curses.A_BLINK


# ---------------------------------------------------------------------------
# M7: key hints + help overlay
# ---------------------------------------------------------------------------

class TestKeyHints:

    @pytest.mark.unit
    def test_every_key_visible_at_80_columns(self):
        rows = tui.layout_key_hints(tui.key_hints(ups_total=2), 80)
        assert len(rows) == 2
        keys = [label for row in rows for label, _ in row]
        assert keys == ["<Q>", "<R>", "<M>", "<↑↓>", "<G>", "<T>", "<U>", "<V>", "<?>"]

    @pytest.mark.unit
    def test_one_row_on_wide_terminals(self):
        assert len(tui.layout_key_hints(tui.key_hints(), 200)) == 1

    @pytest.mark.unit
    def test_help_hint_survives_narrow_terminals(self):
        rows = tui.layout_key_hints(tui.key_hints(), 50)
        assert len(rows) == 2
        assert rows[-1][-1] == tui.HELP_HINT
        assert sum(tui._hint_width(h) for h in rows[-1]) <= 47

    @pytest.mark.unit
    def test_help_lines_state(self):
        lines = tui.help_lines(graph_mode="load", time_range="7d", ups_index=1,
                               ups_total=3, verbosity=2)
        text = "\n".join(lines)
        assert "(now load)" in text and "(now 7d)" in text
        assert "(now 2/3)" in text and "(now all)" in text
        assert "only one" in "\n".join(tui.help_lines())
        assert all(len("   " + line) <= 76 for line in lines)

    @pytest.mark.unit
    def test_render_logs_panel_help_and_two_hint_rows(self):
        win = _FakeWin(20, 80)
        with patch.object(curses, "color_pair", lambda n: n):
            tui.render_logs_panel(win, 0, 20, 80, ["e1"], False, show_help=True)
        text = ["".join(win.cells.get((y, x), (" ", 0))[0] for x in range(80))
                for y in range(20)]
        assert "Keys (press any key to close)" in text[1]
        assert not any("e1" in t for t in text)
        assert "<Q>" in text[18] and "<?>" in text[19]

    @pytest.mark.unit
    def test_scrolled_title(self):
        win = _FakeWin(12, 120)
        with patch.object(curses, "color_pair", lambda n: n):
            tui.render_logs_panel(win, 0, 12, 120, [f"e{i}" for i in range(20)],
                                  True, scroll_offset=3)
        row = "".join(win.cells.get((1, x), (" ", 0))[0] for x in range(120))
        assert "scrolled" in row

    @pytest.mark.unit
    def test_event_rows_are_cleaned_before_painting(self):
        """Escape/control bytes in an event detail never reach curses."""
        win = _FakeWin(12, 120)
        with patch.object(curses, "color_pair", lambda n: n):
            tui.render_logs_panel(win, 0, 12, 120,
                                  ["evil \x1b[2Jcleared\x08\x07 tail"], False)
        painted = "".join(ch for (ch, _a) in win.cells.values())
        assert "\x1b" not in painted and "\x08" not in painted
        assert "\x07" not in painted and "cleared" in painted


# ---------------------------------------------------------------------------
# M5 / M6 / M3: readable events
# ---------------------------------------------------------------------------

class TestEventLines:

    @pytest.mark.unit
    def test_clean_detail(self):
        detail = "📦  **Eneru Upgraded** v5.2.0 → v5.2.1\nback"
        assert tui.clean_event_detail(detail) == "Eneru Upgraded v5.2.0 → v5.2.1 · back"
        assert tui.clean_event_detail(
            "Cannot connect to UPS apc@10.0.0.2 (Network error)", "apc@10.0.0.2") == \
            "Cannot connect to UPS (Network error)"
        assert tui.clean_event_detail("Battery: 45%, Runtime: 1490 seconds") == \
            "Battery: 45%, Runtime: 24m 50s"
        assert tui.clean_event_detail("") == ""
        assert tui.clean_event_detail("🔄") == ""

    @pytest.mark.unit
    def test_format_human_compact_raw_and_bad(self):
        line = tui._format_event_line(NOW - 14 * 86400, "APC", "CONNECTION_LOST",
                                      "x", True, now=NOW, compact=True)
        assert line.endswith("  14d ago  [APC] UPS connection lost: x")
        assert len(line.split("  ")[0]) == len("09-10 10:45")
        assert tui._format_event_line(NOW, "L", "DAEMON_START", "", False,
                                      now=NOW).endswith("just now  Daemon start")
        raw = tui._format_event_line(NOW, "L", "ON_BATTERY", "**b**", True, raw=True)
        assert raw.endswith("  [L] ON_BATTERY: b")
        assert tui._format_event_line(NOW, "L", "X_Y", "", False, raw=True).endswith("  X_Y")
        bad = tui._format_event_line("bad", "L", "X", "", False, raw=True)
        assert bad.startswith("????-??-??")
        bad = tui._format_event_line("bad", "L", "X", "", False, compact=True)
        assert bad.startswith("??-?? ??:??") and "  ?  " in bad

    @pytest.mark.unit
    def test_verbosity_shows_every_tier_on_a_long_history(self, tmp_path):
        """M5: 40 old power events no longer hide -v / -vv rows."""
        config = _config(tmp_path)
        store = StatsStore(tui.stats_db_path_for(config.ups_groups[0], config))
        store.open()
        now = int(time.time())
        for i in range(40):
            store.log_event("ON_BATTERY", f"outage {i}", ts=now - 86400 * 40 + i)
        store.log_event("SLOW_NUT_RESPONSE", "slow", ts=now - 100)
        store.log_event("DAEMON_UPGRADED", "📦 up", ts=now - 50)
        store.close()
        flat = tui.query_events_for_display(config, max_events=30, verbosity=2)
        assert len(flat) == 30
        assert any("Slow NUT response" in line or "Slow nut response" in line
                   for line in flat)
        assert any("Daemon upgraded: up" in line for line in flat)
        grouped = tui.query_events_for_display(config, max_events=10,
                                               verbosity=2, grouped=True)
        assert len(grouped) <= 10
        assert {"Power Events", "Diagnostics", "Lifecycle"} <= set(grouped)
        assert any("50s ago" in line for line in grouped)


# ---------------------------------------------------------------------------
# M11: --once
# ---------------------------------------------------------------------------

class TestRunOnceUx:

    @pytest.mark.unit
    def test_once_on_battery_with_redundancy_and_progress(self, tmp_path, capsys):
        rg = RedundancyGroupConfig(name="rack-a", ups_sources=["ups-a@h", "ups-b@h"],
                                   min_healthy=2)
        config = _config(tmp_path, ("ups-a@h", "ups-b@h"), local=(True, False),
                         redundancy=[rg])
        now = time.time()
        _write_state(Path(config.logging.state_file + ".ups-a-h"),
                     _state("OB DISCHRG", epoch=now - 1, BATTERY=45, RUNTIME=380,
                            TIME_ON_BATTERY=420, OUTPUT_VOLTAGE=""))
        Path(config.logging.state_file + ".ups-a-h.shutdown-progress.json").write_text(
            json.dumps(_progress(writtenAt=now)))
        tui.run_once(config)
        out = capsys.readouterr().out
        assert "UPS-A  (ups-a@h)  · Redundancy member (rack-a)  --  SHUTTING DOWN (OB DISCHRG)" in out
        assert "Output: V" not in out
        assert "  Updated " in out
        assert "  SHUTDOWN IN PROGRESS" in out
        assert "UPS-B  (ups-b@h)  · Redundancy member (rack-a)  --  NO DATA" in out
        assert "Redundancy group rack-a: 0/2 healthy, need 2  --  QUORUM LOST" in out
        assert "    Quorum lost → group shutdown runs" in out
        assert "    Members: UPS-A: Critical runtime:" in out
        assert "UPS-B: no data (counts as failed)" in out

    @pytest.mark.unit
    def test_graph_footer_states_the_scale(self, tmp_path):
        config = _config(tmp_path)
        group = config.ups_groups[0]
        store = StatsStore(tui.stats_db_path_for(group, config))
        store.open()
        now = int(time.time())
        for i in range(5):
            store.buffer_sample({"ups.status": "OL", "battery.charge": "90",
                                 "input.voltage": str(228 + i)}, ts=now - 50 + i)
        store.flush()
        store.close()
        charge = tui.render_graph_text(config, group, "charge", "1h",
                                       force_fallback=True)
        assert charge[-1].startswith("y-axis: 0% (bottom) to 100% (top)")
        assert "now: 90%" in charge[-1]
        volts = tui.render_graph_text(config, group, "voltage", "1h",
                                      force_fallback=True)
        assert volts[0].endswith("(V, auto-scaled)")
        assert volts[-1].startswith("y-axis: 228.0V (bottom) to 232.0V (top)")
        tui.clear_live_buffers()

    @pytest.mark.unit
    def test_graph_footer_flat_series_is_padded(self, tmp_path):
        config = _config(tmp_path)
        group = config.ups_groups[0]
        with patch.object(tui, "query_metric_series",
                          return_value=[(1, 230.0), (2, 230.0)]):
            lines = tui.render_graph_text(config, group, "voltage", "1h",
                                          force_fallback=True)
        assert lines[-1].startswith("y-axis: 218.5V (bottom) to 241.5V (top)")

    @pytest.mark.unit
    def test_once_text_helper(self):
        L = tui.Line
        out = tui._once_text([L("   Head", "bold", 0, ("OK", "ok", False)),
                              L("   body", "plain", 1, None),
                              L("odd", "plain", 1, None)])
        assert out == ["Head  --  OK", "  body", "odd"]


# ---------------------------------------------------------------------------
# The curses loop: help key, sizing with redundancy data
# ---------------------------------------------------------------------------

class TestRunTuiUx:

    def _run(self, config, screen, **kw):
        def wrapper(callback):
            callback(screen)

        with patch.object(tui.curses, "wrapper", side_effect=wrapper), \
             patch.object(tui.curses, "COLORS", 256, create=True), \
             patch.object(tui.curses, "start_color", lambda: None), \
             patch.object(tui.curses, "init_pair", lambda *args: None), \
             patch.object(tui.curses, "color_pair", lambda n: n), \
             patch.object(tui.curses, "curs_set", lambda _value: None):
            tui.run_tui(config, **kw)

    def _rows(self, screen):
        return ["".join(screen.cells.get((y, x), (" ", 0))[0]
                        for x in range(screen.width)) for y in range(screen.height)]

    @pytest.mark.unit
    def test_help_overlay_opens_closes_and_ignores_timeouts(self, tmp_path):
        config = _config(tmp_path)
        _write_state(tmp_path / "ups.state", _state(epoch=time.time()))
        snapshots = []
        screen = _FakeTuiScreen(24, 80, [ord("?"), -1, ord("x"), ord("h"), ord("q")])
        real_refresh = screen.refresh

        def refresh():
            snapshots.append(self._rows(screen))
            real_refresh()

        screen.refresh = refresh
        self._run(config, screen)
        assert "Keys (press any key to close)" not in "\n".join(snapshots[0])
        assert "Keys (press any key to close)" in "\n".join(snapshots[1])
        assert "Keys (press any key to close)" in "\n".join(snapshots[2])  # -1 kept it
        assert "Keys (press any key to close)" not in "\n".join(snapshots[3])
        assert "Keys (press any key to close)" in "\n".join(snapshots[4])

    @pytest.mark.unit
    def test_panel_sized_to_content_with_redundancy(self, tmp_path):
        rg = RedundancyGroupConfig(name="rack-a", ups_sources=["ups-a@h", "ups-b@h"],
                                   min_healthy=2)
        config = _config(tmp_path, ("ups-a@h", "ups-b@h"), local=(True, False),
                         redundancy=[rg])
        screen = _FakeTuiScreen(24, 80, [ord("g"), ord("q")])
        self._run(config, screen)
        rows = self._rows(screen)
        text = "\n".join(rows)
        assert "Redundancy group rack-a: 0/2 healthy, need 2" in text
        assert "<?>" in rows[-1]
        # Nothing painted past the right edge.
        assert all(len(r) == 80 for r in rows)


# ---------------------------------------------------------------------------
# Release-review cycle 3 (F-181, F-183, F-184, F-187)
# ---------------------------------------------------------------------------

class TestCycle3:

    @pytest.mark.unit
    def test_single_ups_with_redundancy_group_reads_suffixed_paths(self, tmp_path):
        """F-181: one UPS + a redundancy group runs the coordinator, which
        writes suffixed paths; the TUI must read those, not the bare path."""
        rg = RedundancyGroupConfig(name="rack", ups_sources=["ups-a@h"],
                                   min_healthy=1)
        config = _config(tmp_path, redundancy=[rg], local=(False,))
        config.local_shutdown.trigger_on = "none"
        assert not config.multi_ups
        group = config.ups_groups[0]
        path = tui.state_file_path_for(group, config)
        assert path.name == "ups.state.ups-a-h"
        assert tui.stats_db_path_for(group, config).name == "ups-a-h.db"
        _write_state(path, _state(epoch=time.time() - 1))
        data = tui.collect_group_data(group, config)
        assert data["outlook"]["statusSummary"]["label"] == "On mains"
        # Coordinator semantics (trigger_on: none): a non-local UPS never
        # powers the host off; single-UPS semantics would say it does.
        assert data["outlook"]["role"]["shutsDownLocalHost"] is False
        [red] = tui.collect_redundancy_data(config, [data])
        assert red["outlook"]["state"] != "quorum-lost"
        assert red["quorumLost"] is False
        # Single-UPS without redundancy keeps the bare path and default.db.
        plain = _config(tmp_path)
        assert tui.state_file_path_for(plain.ups_groups[0], plain).name == "ups.state"
        assert tui.stats_db_path_for(plain.ups_groups[0], plain).name == "default.db"

    @pytest.mark.unit
    def test_member_trigger_never_blinks_red(self, tmp_path):
        """F-184: a member's fired trigger is a vote; the group decides."""
        rg = RedundancyGroupConfig(name="rack", ups_sources=["ups-a@h", "ups-b@h"],
                                   min_healthy=1)
        config = _config(tmp_path, ("ups-a@h", "ups-b@h"), local=(True, False),
                         redundancy=[rg], remotes=1)
        state = _state("OB DISCHRG", epoch=NOW - 1, BATTERY=10, RUNTIME=100,
                       TIME_ON_BATTERY=610, TRIGGER_ACTIVE=1,
                       TRIGGER_REASON="Battery low")
        _write_state(Path(config.logging.state_file + ".ups-a-h"), state)
        data = tui.collect_group_data(config.ups_groups[0], config, NOW)
        assert data["outlook"]["role"]["kind"] == "redundancy-member"
        lines = tui.ups_block_lines(data, NOW, 80)
        assert lines[0].badge == ("TRIGGER MET, GROUP DECIDES", SEVERITY_WARN,
                                  False)

    @pytest.mark.unit
    def test_stale_fired_trigger_is_not_dressed_as_live(self, tmp_path):
        """F-187 (T04): old data whose trigger had fired shows STALE, not a
        red blinking SHUTDOWN TRIGGERED."""
        state = _state("OB DISCHRG", epoch=NOW - 600, BATTERY=10, RUNTIME=100,
                       TIME_ON_BATTERY=610)
        config = _config(tmp_path, remotes=1)
        _write_state(tmp_path / "ups.state", state)
        data = tui.collect_group_data(config.ups_groups[0], config, NOW)
        assert data["outlook"]["triggerOutlook"]["firing"]
        assert data["outlook"]["role"]["hasShutdownActions"] is True
        badge = tui.ups_block_lines(data, NOW, 80)[0].badge
        assert badge == ("STALE 10m", SEVERITY_WARN, False)

    @pytest.mark.unit
    def test_self_test_latch_does_not_open_db_on_mains(self, tmp_path):
        """F-187 (T11): the stats DB is opened only while on battery."""
        config = _config(tmp_path)
        with patch.object(tui.StatsStore, "open_readonly",
                          return_value=None) as opener:
            assert tui._self_test_armed(config.ups_groups[0], config,
                                        {"STATUS": "OL CHRG"}) is False
            opener.assert_not_called()
            tui._self_test_armed(config.ups_groups[0], config, {"STATUS": "OB"})
            opener.assert_called_once()

    @pytest.mark.unit
    def test_once_strips_terminal_escapes(self, tmp_path, capsys):
        """F-183: NUT status, state-file text, the sidecar reason and event
        details are external text; --once must not pass escapes through."""
        evil = "\x1b]0;PWNED\x07\x1b[2J"
        config = _config(tmp_path)
        _write_state(tmp_path / "ups.state",
                     _state(f"OB {evil}", epoch=time.time() - 1,
                            TRIGGER_ACTIVE=1,
                            TRIGGER_REASON=f"low\x1b[31m{evil}"))
        Path(str(tmp_path / "ups.state") + ".shutdown-progress.json").write_text(
            json.dumps(_progress(reason=f"why{evil}", writtenAt=time.time())))
        store = StatsStore(tui.stats_db_path_for(config.ups_groups[0], config))
        store.open()
        store.log_event("ON_BATTERY", f"detail{evil}", ts=int(time.time()) - 5)
        store.close()
        tui.run_once(config)
        out = capsys.readouterr().out
        assert "\x1b" not in out and "\x07" not in out
        assert "Trigger: low" in out and "PWNED" in out
        tui.run_once(config, events_only=True)
        out = capsys.readouterr().out
        assert "\x1b" not in out and "detail" in out

    @pytest.mark.unit
    def test_side_file_reads_are_bounded(self, tmp_path):
        """F-183: a symlink (e.g. to /dev/zero) or a FIFO is refused, and a
        huge regular file is read only up to the cap."""
        from eneru.outlook import _read_state_file
        from eneru.shutdown.progress import read_progress_sidecar
        from eneru.utils import SIDE_FILE_MAX_BYTES, read_side_file
        link = tmp_path / "state"
        link.symlink_to("/dev/zero")
        assert tui.parse_state_file(link) is None
        assert _read_state_file(link) is None
        assert read_progress_sidecar(link) is None
        fifo = tmp_path / "fifo"
        os.mkfifo(fifo)
        assert tui.parse_state_file(fifo) is None
        big = tmp_path / "big"
        big.write_text("STATUS=OL\n" + "X" * (SIDE_FILE_MAX_BYTES * 2))
        assert len(read_side_file(big)) == SIDE_FILE_MAX_BYTES
        assert tui.parse_state_file(big) == {"STATUS": "OL"}

    @pytest.mark.unit
    def test_sidecar_writes_do_not_follow_a_planted_tmp_symlink(self, tmp_path):
        """A symlink planted at ``<sidecar>.tmp`` must not redirect the
        progress / remote-health sidecar write to another file."""
        import threading
        from eneru.config import Config
        from eneru.remote_health import RemoteHealthManager
        from eneru.shutdown.progress import ShutdownProgress
        victim = tmp_path / "victim"
        victim.write_text("keep")
        sidecar = tmp_path / "state.progress.json"
        (tmp_path / "state.progress.json.tmp").symlink_to(victim)
        ShutdownProgress("ups", "UPS", sidecar_path=sidecar).persist()
        assert victim.read_text() == "keep"
        assert json.loads(sidecar.read_text())["state"] == "idle"
        assert not (tmp_path / "state.progress.json.tmp").exists()

        health = tmp_path / "state.remote-health.json"
        (tmp_path / "state.remote-health.json.tmp").symlink_to(victim)
        RemoteHealthManager(
            config=Config(), group_label="Rack", servers=[],
            sidecar_path=health, stop_event=threading.Event(),
            log_fn=lambda *_: None, notify_fn=lambda *_: None,
        )._write_sidecar()
        assert victim.read_text() == "keep"
        assert json.loads(health.read_text())["group"] == "Rack"
