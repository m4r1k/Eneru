"""UX v6.2 backend: next-trigger outlook, role, freshness, status vocabulary,
event tier selection, replacement caps, progress sidecar, API wiring.

The parity classes pin ``eneru.outlook.evaluate_triggers`` to the REAL
``UPSGroupMonitor._handle_on_battery`` decisions so the display model can
never drift from the shutdown path.
"""

import json
import sqlite3
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from eneru import MonitorState, UPSGroupMonitor
from eneru.config import (
    BehaviorConfig,
    Config,
    ContainersConfig,
    FilesystemsConfig,
    LocalShutdownConfig,
    RedundancyGroupConfig,
    RemoteServerConfig,
    TriggersConfig,
    UnmountConfig,
    UPSConfig,
    UPSGroupConfig,
    VMConfig,
)
from eneru.health import prediction
from eneru.health_model import UPSHealth
from eneru import outlook
from eneru.outlook import (
    TRIGGER_IDS,
    describe_trigger_conditions,
    evaluate_triggers,
    freshness,
    monitor_outlook,
    read_self_test_failure_armed,
    redundancy_group_outlook,
    redundancy_outlook_from_state_files,
    redundancy_role,
    stale_after_seconds,
    state_file_outlook,
    trigger_action,
    ups_role,
)
from eneru.shutdown.progress import (
    ShutdownProgress,
    progress_sidecar_path,
    read_progress_sidecar,
)
from eneru.state import HealthSnapshot
from eneru.utils import (
    STATUS_STATES,
    format_age,
    severity_rank,
    status_summary,
    worst_severity,
)


def _triggers(**kw):
    t = TriggersConfig()
    t.low_battery_threshold = 20
    t.critical_runtime_threshold = 600
    t.depletion.critical_rate = 15.0
    t.depletion.grace_period = 90
    t.extended_time.enabled = True
    t.extended_time.threshold = 900
    t.on_battery_stabilization_delay = 0
    t.self_test_failure_shutdown_delay = 30
    for key, value in kw.items():
        setattr(t, key, value)
    return t


def _by_id(result):
    return {row["id"]: row for row in result["triggers"]}


def _group(**kw):
    defaults = dict(
        ups=UPSConfig(name="U@h"),
        virtual_machines=VMConfig(enabled=False),
        containers=ContainersConfig(enabled=False),
        filesystems=FilesystemsConfig(sync_enabled=False,
                                      unmount=UnmountConfig(enabled=False)),
        is_local=False,
    )
    defaults.update(kw)
    return UPSGroupConfig(**defaults)


def _config(group, *, dry_run=False, local_enabled=True, extra_groups=(),
            redundancy=()):
    return Config(
        ups_groups=[group, *extra_groups],
        redundancy_groups=list(redundancy),
        behavior=BehaviorConfig(dry_run=dry_run),
        local_shutdown=LocalShutdownConfig(enabled=local_enabled),
    )


def _server(name="nas", host="10.0.0.2", loopback=False, enabled=True):
    s = RemoteServerConfig(name=name, host=host, enabled=enabled)
    s.is_host_loopback = loopback
    return s


# ---------------------------------------------------------------------------
# evaluate_triggers (pure)
# ---------------------------------------------------------------------------

class TestEvaluateTriggers:
    @pytest.mark.unit
    def test_on_mains_everything_idle(self):
        r = evaluate_triggers(_triggers(), status="OL CHRG", battery_charge="100",
                              runtime="3600")
        assert [t["id"] for t in r["triggers"]] == list(TRIGGER_IDS)
        by = _by_id(r)
        assert r["onBattery"] is False and r["timeOnBattery"] == 0
        assert by["lowBattery"]["state"] == "idle"
        assert by["selfTestFailure"]["state"] == "disabled"
        assert r["next"] is None and r["firing"] == []
        assert r["summary"].startswith("On mains")

    @pytest.mark.unit
    def test_runtime_eta_and_next(self):
        r = evaluate_triggers(_triggers(), status="OB DISCHRG",
                              battery_charge="60", runtime="700",
                              depletion_rate=2.0, time_on_battery=100)
        by = _by_id(r)
        assert by["criticalRuntime"]["state"] == "ok"
        assert by["criticalRuntime"]["etaSeconds"] == 101
        assert by["criticalRuntime"]["etaBasis"] == "runtime"
        assert by["criticalRuntime"]["margin"] == 100
        # Charge 60 -> 20 at 2 %/min = 20 min.
        assert by["lowBattery"]["etaSeconds"] == 1200
        assert by["lowBattery"]["etaBasis"] == "depletion"
        assert by["extendedTime"]["etaSeconds"] == 801
        assert r["next"]["id"] == "criticalRuntime"
        assert r["summary"].startswith("Next: critical runtime in about")

    @pytest.mark.unit
    def test_fired_first_in_evaluation_order(self):
        r = evaluate_triggers(_triggers(), status="OB", battery_charge="10",
                              runtime="100", time_on_battery=50)
        assert r["firing"] == ["lowBattery", "criticalRuntime"]
        assert r["next"]["id"] == "lowBattery"
        assert r["summary"].startswith("Shutdown condition met: low battery")

    @pytest.mark.unit
    def test_stabilization_holds_and_eta(self):
        t = _triggers(on_battery_stabilization_delay=30)
        r = evaluate_triggers(t, status="OB", battery_charge="10", runtime="100",
                              time_on_battery=10)
        by = _by_id(r)
        assert r["stabilizing"] is True and r["stabilizationRemaining"] == 20
        assert by["lowBattery"]["state"] == "held"
        assert by["lowBattery"]["etaSeconds"] == 20
        assert r["firing"] == []
        assert r["next"]["state"] == "held"
        assert "stabilizing for 20s" in r["summary"]

    @pytest.mark.unit
    def test_unknown_readings(self):
        r = evaluate_triggers(_triggers(), status="OB", battery_charge="-1",
                              runtime="", time_on_battery=5)
        by = _by_id(r)
        assert by["lowBattery"]["state"] == "unknown"
        assert by["criticalRuntime"]["state"] == "unknown"
        off = evaluate_triggers(_triggers(), status="OL", battery_charge=None,
                                runtime="x")
        assert _by_id(off)["lowBattery"]["state"] == "idle"
        assert _by_id(off)["criticalRuntime"]["state"] == "idle"

    @pytest.mark.unit
    def test_depletion_states(self):
        t = _triggers()
        held = _by_id(evaluate_triggers(
            t, status="OB", battery_charge="80", runtime="3000",
            depletion_rate=20.0, time_on_battery=60))["depletionRate"]
        assert held["state"] == "held" and held["etaSeconds"] == 30
        fired = _by_id(evaluate_triggers(
            t, status="OB", battery_charge="80", runtime="3000",
            depletion_rate=20.0, time_on_battery=90))["depletionRate"]
        assert fired["state"] == "fired" and fired["etaSeconds"] == 0
        ok = _by_id(evaluate_triggers(
            t, status="OB", battery_charge="80", runtime="3000",
            depletion_rate="bad", time_on_battery=90))["depletionRate"]
        assert ok["state"] == "ok" and ok["value"] == 0.0

    @pytest.mark.unit
    def test_extended_time_disabled_held_fired(self):
        t = _triggers()
        t.extended_time.enabled = False
        r = _by_id(evaluate_triggers(t, status="OB", battery_charge="80",
                                     runtime="3000", time_on_battery=2000))
        assert r["extendedTime"]["state"] == "disabled"
        assert r["extendedTime"]["enabled"] is False
        t = _triggers(on_battery_stabilization_delay=5000)
        r = _by_id(evaluate_triggers(t, status="OB", battery_charge="80",
                                     runtime="3000", time_on_battery=2000))
        assert r["extendedTime"]["state"] == "held"
        r = _by_id(evaluate_triggers(_triggers(), status="OB",
                                     battery_charge="80", runtime="3000",
                                     time_on_battery=901))
        assert r["extendedTime"]["state"] == "fired"

    @pytest.mark.unit
    def test_self_test_failure_trigger(self):
        t = _triggers()
        waiting = _by_id(evaluate_triggers(
            t, status="OB", battery_charge="80", runtime="3000",
            time_on_battery=10, self_test_failure_armed=True))
        assert waiting["selfTestFailure"]["state"] == "ok"
        assert waiting["selfTestFailure"]["etaSeconds"] == 20
        fired = _by_id(evaluate_triggers(
            t, status="OB", battery_charge="80", runtime="3000",
            time_on_battery=30, self_test_failure_armed=True))
        assert fired["selfTestFailure"]["state"] == "fired"
        idle = _by_id(evaluate_triggers(
            t, status="OL", battery_charge="80", runtime="3000",
            self_test_failure_armed=True))
        assert idle["selfTestFailure"]["state"] == "idle"

    @pytest.mark.unit
    def test_fsd_and_failsafe(self):
        r = evaluate_triggers(_triggers(), status="FSD OL", battery_charge="80",
                              runtime="3000")
        assert r["next"]["id"] == "fsd" and r["firing"] == ["fsd"]
        f = evaluate_triggers(_triggers(), status="OB", battery_charge="80",
                              runtime="3000", connection_state="FAILED")
        assert _by_id(f)["failsafe"]["state"] == "fired"

    @pytest.mark.unit
    def test_on_battery_nothing_close(self):
        t = _triggers()
        t.extended_time.enabled = False
        r = evaluate_triggers(t, status="OB", battery_charge="80",
                              runtime="3000", time_on_battery=5)
        assert r["next"]["id"] == "criticalRuntime"
        r = evaluate_triggers(t, status="OB", battery_charge=None,
                              runtime=None, time_on_battery=5)
        assert r["next"] is None
        assert r["summary"] == "On battery: no trigger is close"

    @pytest.mark.unit
    def test_describe_conditions(self):
        t = _triggers()
        lines = describe_trigger_conditions(t, self_test_failure_armed=True)
        assert lines[0] == "charge below 20%"
        assert lines[1] == "runtime below 10m 0s"
        assert "failed self-test: 30s on battery" in lines
        assert lines[-1] == "connection lost while on battery"
        t.extended_time.enabled = False
        t.low_battery_threshold = 12.5
        lines = describe_trigger_conditions(t)
        assert lines[0] == "charge below 12.5%"
        assert not any("on battery" == line[-10:] and "failed" in line
                       for line in lines)


# ---------------------------------------------------------------------------
# Parity with the real shutdown path
# ---------------------------------------------------------------------------

def _parity_monitor(minimal_config, tmp_path, **trig):
    minimal_config.logging.battery_history_file = str(tmp_path / "bh")
    minimal_config.logging.shutdown_flag_file = str(tmp_path / "flag")
    minimal_config.logging.state_file = str(tmp_path / "state")
    t = minimal_config.triggers
    t.low_battery_threshold = 20
    t.critical_runtime_threshold = 600
    t.depletion.critical_rate = 15.0
    t.depletion.grace_period = 90
    t.extended_time.enabled = True
    t.extended_time.threshold = 900
    t.on_battery_stabilization_delay = trig.get("stab", 0)
    monitor = UPSGroupMonitor(minimal_config)
    monitor.state = MonitorState()
    monitor.logger = MagicMock()
    return monitor


def _run_real(monitor, *, charge, runtime, rate, tob):
    monitor.state.previous_status = "OB DISCHRG"
    monitor.state.on_battery_start_mono = time.monotonic() - tob - 0.5
    monitor.state.on_battery_start_time = int(time.time()) - tob
    with patch.object(monitor, "_calculate_depletion_rate", return_value=rate), \
            patch.object(monitor, "_trigger_immediate_shutdown") as fire:
        monitor._handle_on_battery({
            "ups.status": "OB DISCHRG", "battery.charge": charge,
            "battery.runtime": runtime, "ups.load": "25"})
    return fire.call_args[0][0] if fire.called else None


_REASON_PREFIX = {
    "lowBattery": "Battery charge ",
    "criticalRuntime": "Runtime ",
    "depletionRate": "Depletion rate ",
    "extendedTime": "Time on battery ",
}

_CASES = [
    # charge, runtime, rate, tob, stab
    ("19", "1800", 0.0, 40, 0), ("20", "1800", 0.0, 40, 0),
    ("21", "1800", 0.0, 40, 0), ("19.9", "1800", 0.0, 40, 0),
    ("0", "1800", 0.0, 40, 0), ("-1", "-1", 0.0, 40, 0),
    ("80", "599", 0.0, 40, 0), ("80", "600", 0.0, 40, 0),
    ("80", "601", 0.0, 40, 0), ("80", "0", 0.0, 40, 0),
    ("80", "1800", 15.0, 120, 0), ("80", "1800", 15.01, 120, 0),
    ("80", "1800", 20.0, 89, 0), ("80", "1800", 20.0, 90, 0),
    ("80", "1800", 0.0, 900, 0), ("80", "1800", 0.0, 901, 0),
    ("10", "100", 30.0, 1000, 0),
    ("80", "300", 0.0, 29, 30), ("80", "300", 0.0, 30, 30),
    ("10", "1800", 0.0, 10, 30), ("80", "1800", 20.0, 100, 200),
    ("80", "1800", 0.0, 1000, 2000), ("abc", "xyz", 0.0, 40, 0),
]


class TestParityWithHandleOnBattery:
    """The outlook's first fired trigger == the reason the daemon acts on."""

    @pytest.mark.unit
    @pytest.mark.parametrize("charge,runtime,rate,tob,stab", _CASES)
    def test_parity(self, minimal_config, tmp_path, charge, runtime, rate,
                    tob, stab):
        monitor = _parity_monitor(minimal_config, tmp_path, stab=stab)
        reason = _run_real(monitor, charge=charge, runtime=runtime, rate=rate,
                           tob=tob)
        r = evaluate_triggers(
            monitor.config.triggers, status="OB DISCHRG", battery_charge=charge,
            runtime=runtime, depletion_rate=rate, time_on_battery=tob)
        if reason is None:
            assert r["firing"] == []
        else:
            assert r["firing"], reason
            assert reason.startswith(_REASON_PREFIX[r["firing"][0]])

    @pytest.mark.unit
    @pytest.mark.parametrize("tob,expect", [(29, False), (30, True)])
    def test_self_test_failure_parity(self, minimal_config, tmp_path, tob,
                                      expect):
        monitor = _parity_monitor(minimal_config, tmp_path)
        monitor.config.triggers.self_test_failure_shutdown_delay = 30
        store = MagicMock()
        store.get_meta.side_effect = lambda key: {
            "self_test_failure_latched": "1700000000",
            "self_test_failure_outage_start": "1",
        }.get(key)
        monitor._stats_store = store
        reason = _run_real(monitor, charge="80", runtime="1800", rate=0.0,
                           tob=tob)
        armed = read_self_test_failure_armed(
            store, monitor.state.on_battery_start_time)
        r = evaluate_triggers(
            monitor.config.triggers, status="OB", battery_charge="80",
            runtime="1800", time_on_battery=tob, self_test_failure_armed=armed)
        assert (reason is not None) is expect
        assert (r["firing"] == ["selfTestFailure"]) is expect

    @pytest.mark.unit
    def test_outlook_reads_do_not_change_decisions(self, minimal_config,
                                                   tmp_path):
        """Computing every UX block around a trigger leaves the decision and
        the monitor state exactly as the bare handler would."""
        bare = _parity_monitor(minimal_config, tmp_path)
        expected = _run_real(bare, charge="80", runtime="599", rate=0.0, tob=40)
        monitor = _parity_monitor(minimal_config, tmp_path)
        monitor_outlook(monitor)
        before = (monitor.state.trigger_active, monitor.state.previous_status)
        got = _run_real(monitor, charge="80", runtime="599", rate=0.0, tob=40)
        monitor_outlook(monitor)
        assert got == expected
        assert monitor.state.trigger_active == before[0]

    @pytest.mark.unit
    def test_hint_failure_never_blocks_on_battery(self, minimal_config,
                                                  tmp_path):
        monitor = _parity_monitor(minimal_config, tmp_path)
        monitor.state.previous_status = "OL"
        with patch("eneru.outlook.monitor_role", side_effect=RuntimeError), \
                patch.object(monitor, "_log_power_event") as log_event, \
                patch.object(monitor, "_trigger_immediate_shutdown") as fire:
            monitor._handle_on_battery({
                "ups.status": "OB", "battery.charge": "10",
                "battery.runtime": "1490", "ups.load": "30"})
        details = log_event.call_args[0][1]
        assert "Runtime: 24m 50s" in details and "seconds" not in details
        assert log_event.call_args[1]["notification_details"] == details
        # stabilization default 0 in _parity_monitor -> T1 fires as before.
        fire.assert_called_once()


class TestOnBatteryNotificationHint:
    @pytest.mark.unit
    def test_hint_lists_triggers_and_action(self, minimal_config, tmp_path):
        monitor = _parity_monitor(minimal_config, tmp_path)
        hint = monitor._on_battery_trigger_hint()
        assert hint.startswith("Shutdown triggers: charge below 20%")
        # minimal_config: local group, local_shutdown disabled, dry-run.
        assert "(dry-run)" in hint
        monitor.state.previous_status = "OL"
        with patch.object(monitor, "_log_power_event") as log_event, \
                patch.object(monitor, "_trigger_immediate_shutdown"):
            monitor._handle_on_battery({
                "ups.status": "OB", "battery.charge": "90",
                "battery.runtime": "1490", "ups.load": "30"})
        note = log_event.call_args[1]["notification_details"]
        assert note.endswith(hint)


# ---------------------------------------------------------------------------
# Role + action
# ---------------------------------------------------------------------------

class TestRoleAndAction:
    @pytest.mark.unit
    def test_monitor_only(self):
        g = _group()
        role = ups_role(_config(g), g)
        assert role["kind"] == "monitor-only"
        assert role["hasShutdownActions"] is False
        assert trigger_action(role)["kind"] == "notify-only"

    @pytest.mark.unit
    def test_remote_only_counts_regular_servers(self):
        g = _group(remote_servers=[_server(), _server("b", "b"),
                                   _server("off", "c", enabled=False)])
        role = ups_role(_config(g), g)
        assert role["kind"] == "remote-only" and role["remoteServers"] == 2
        action = trigger_action(role)
        assert action == {"kind": "remote-shutdown",
                          "label": "Shuts down 2 remote servers", "groups": []}
        role["remoteServers"] = 1
        assert trigger_action(role)["label"] == "Shuts down 1 remote server"

    @pytest.mark.unit
    def test_local_host(self):
        g = _group(is_local=True, remote_servers=[_server()])
        role = ups_role(_config(g, dry_run=True), g)
        assert role["kind"] == "local" and role["shutsDownLocalHost"] is True
        assert role["label"] == "Powers this host"
        assert trigger_action(role)["label"] == (
            "Shuts down this host and 1 remote server (dry-run)")

    @pytest.mark.unit
    def test_local_drain_without_poweroff(self):
        g = _group(is_local=True, virtual_machines=VMConfig(enabled=True))
        role = ups_role(_config(g, local_enabled=False), g)
        assert role["kind"] == "local" and role["shutsDownLocalHost"] is False
        assert role["localDrain"] is True
        assert trigger_action(role)["label"] == (
            "Stops local workloads (this host stays up)")
        role["remoteServers"] = 3
        assert trigger_action(role)["label"].endswith(
            "and shuts down 3 remote servers")

    @pytest.mark.unit
    def test_delegated_loopback_powers_host(self):
        g = _group(is_local=True, remote_servers=[
            _server("host", "127.0.0.1", loopback=True)])
        role = ups_role(_config(g), g, delegated=True)
        assert role["shutsDownLocalHost"] is True and role["localDrain"] is True
        assert role["remoteServers"] == 0

    @pytest.mark.unit
    def test_coordinator_handoff_respects_local_shutdown(self):
        g = _group(is_local=True)
        on = ups_role(_config(g), g, coordinator_mode=True,
                      coordinator_handoff=True)
        off = ups_role(_config(g, local_enabled=False), g,
                       coordinator_mode=True, coordinator_handoff=True)
        assert on["shutsDownLocalHost"] is True
        assert off["shutsDownLocalHost"] is False

    @pytest.mark.unit
    def test_redundancy_member(self):
        g = _group()
        role = ups_role(_config(g), g, redundancy_groups=["rack-a"])
        assert role["kind"] == "redundancy-member"
        assert role["label"] == "Redundancy member (rack-a)"
        action = trigger_action(role)
        assert action["kind"] == "redundancy-advisory"
        assert action["groups"] == ["rack-a"]
        anon = ups_role(_config(g), g, in_redundancy=True)
        assert anon["label"] == "Redundancy member"
        assert "its redundancy group" in trigger_action(anon)["label"]


# ---------------------------------------------------------------------------
# Freshness / self-test readers
# ---------------------------------------------------------------------------

class TestFreshness:
    @pytest.mark.unit
    def test_shapes(self):
        assert stale_after_seconds(1) == 30
        assert stale_after_seconds(20) == 60
        assert stale_after_seconds("bad") == 30
        never = freshness(0, check_interval=5)
        assert never == {"lastPollAt": None, "ageSeconds": None,
                         "staleAfterSeconds": 30, "stale": True}
        f = freshness(1000.0, check_interval=5, now=1010.0)
        assert f["ageSeconds"] == 10.0 and f["stale"] is False
        assert freshness(1000.0, check_interval=5, now=1031.0)["stale"] is True
        assert freshness(1000.0, check_interval=5, age_seconds=-3)["ageSeconds"] == 0.0
        assert freshness(time.time(), check_interval=5)["stale"] is False

    @pytest.mark.unit
    def test_self_test_readers(self, tmp_path):
        assert outlook.self_test_failure_armed(
            "bad", "1", attributed=False, on_battery_since=0) is False
        assert outlook.self_test_failure_armed(
            "5", "7", attributed=False, on_battery_since=7) is False
        assert outlook.self_test_failure_armed(
            "5", "7", attributed=True, on_battery_since=8) is False
        assert outlook.self_test_failure_armed(
            "5", "7", attributed=False, on_battery_since=8) is True
        db = tmp_path / "s.db"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO meta VALUES ('self_test_failure_latched', '9')")
        conn.commit()
        assert read_self_test_failure_armed(conn, 0) is True
        conn.close()
        broken = MagicMock()
        broken.get_meta.side_effect = RuntimeError
        assert read_self_test_failure_armed(broken, 0) is False


# ---------------------------------------------------------------------------
# Daemon-side wrapper
# ---------------------------------------------------------------------------

class TestMonitorOutlook:
    @pytest.mark.unit
    def test_live_monitor(self, minimal_config, tmp_path):
        monitor = _parity_monitor(minimal_config, tmp_path)
        now_mono = time.monotonic()
        with monitor.state._lock:
            monitor.state.latest_status = "OB DISCHRG"
            monitor.state.latest_battery_charge = "50"
            monitor.state.latest_runtime = "280"
            monitor.state.latest_time_on_battery = 100
            monitor.state.latest_update_time = time.time() - 2
            monitor.state.latest_update_mono = now_mono - 2
        monitor._stats_store = None
        blocks = monitor_outlook(monitor)
        assert blocks["nextTrigger"]["id"] == "criticalRuntime"
        assert blocks["triggerOutlook"]["timeOnBattery"] >= 102
        assert blocks["triggerOutlook"]["action"]["kind"] in (
            "local-shutdown", "notify-only")
        assert blocks["freshness"]["stale"] is False
        assert blocks["statusSummary"]["state"] == "on_battery"
        # A running shutdown flips the summary.
        monitor._shutdown_progress.start("x")
        assert monitor_outlook(monitor)["statusSummary"]["state"] == "shutting_down"
        broken = MagicMock()
        broken.snapshot.side_effect = RuntimeError
        monitor._shutdown_progress = broken
        assert monitor_outlook(monitor)["statusSummary"]["state"] == "on_battery"

    @pytest.mark.unit
    def test_redundancy_names_from_source(self, minimal_config, tmp_path):
        monitor = _parity_monitor(minimal_config, tmp_path)
        monitor._in_redundancy_group = True
        source_config = SimpleNamespace(redundancy_groups=[
            RedundancyGroupConfig(name="rack-a",
                                  ups_sources=[monitor.config.ups.name]),
            RedundancyGroupConfig(name="other", ups_sources=["X"])])
        role = outlook.monitor_role(monitor, source_config)
        assert role["redundancyGroups"] == ["rack-a"]


# ---------------------------------------------------------------------------
# Redundancy outlook (M9)
# ---------------------------------------------------------------------------

def _snap(status="OL", charge="100", runtime="3000", **kw):
    base = dict(status=status, battery_charge=charge, runtime=runtime, load="10",
                depletion_rate=0.0, time_on_battery=0,
                last_update_time=time.time(), connection_state="OK",
                trigger_active=False, trigger_reason="")
    base.update(kw)
    return HealthSnapshot(**base)


class TestRedundancyOutlook:
    def _rg(self, n=2, min_healthy=1):
        names = [f"U{i}@h" for i in range(n)]
        return RedundancyGroupConfig(name="rack-a", ups_sources=names,
                                     min_healthy=min_healthy,
                                     triggers=_triggers())

    @pytest.mark.unit
    def test_states(self):
        rg = self._rg(3, 1)
        snaps = {"U0@h": _snap(), "U1@h": _snap(), "U2@h": _snap()}
        raw = {n: UPSHealth.HEALTHY for n in snaps}
        out = redundancy_group_outlook(rg, snaps, raw)
        assert out["outlook"]["state"] == "healthy"
        assert out["outlook"]["severity"] == "ok"
        assert out["outlook"]["label"] == "Can lose 2 more members"
        raw["U0@h"] = UPSHealth.CRITICAL
        out = redundancy_group_outlook(rg, snaps, raw)
        assert out["outlook"]["label"] == "Can lose 1 more member"
        assert out["outlook"]["severity"] == "warn"
        assert out["failingMembers"] == ["U0@h"]
        raw["U1@h"] = UPSHealth.UNKNOWN
        out = redundancy_group_outlook(rg, snaps, raw)
        assert out["outlook"]["state"] == "at-risk"
        raw["U2@h"] = UPSHealth.CRITICAL
        out = redundancy_group_outlook(rg, snaps, raw)
        assert out["outlook"]["state"] == "quorum-lost"
        assert out["failuresTolerated"] == -1 and out["quorumLost"] is True
        out = redundancy_group_outlook(rg, snaps, raw, quorum_deferred=True)
        assert out["outlook"]["state"] == "deferred" and not out["quorumLost"]
        out = redundancy_group_outlook(rg, snaps, raw, progress_state="running",
                                       role={"kind": "remote-only",
                                             "remoteServers": 1})
        assert out["outlook"]["state"] == "shutting-down"
        assert out["outlook"]["action"] == "Shuts down 1 remote server"

    @pytest.mark.unit
    def test_health_reasons(self):
        rg = self._rg(2, 1)
        cases = [
            (_snap(last_update_time=0), UPSHealth.UNKNOWN, "no data"),
            (_snap(connection_state="FAILED"), UPSHealth.UNKNOWN, "connection lost"),
            (_snap(), UPSHealth.UNKNOWN, "stale data"),
            (_snap(status="FSD OB"), UPSHealth.CRITICAL,
             "UPS forced shutdown (FSD)"),
            (_snap(status="OB", trigger_active=True, trigger_reason="why"),
             UPSHealth.CRITICAL, "why"),
            (_snap(status="OB", runtime="100", time_on_battery=50),
             UPSHealth.CRITICAL, "Critical runtime: 1m 40s now"),
            (_snap(status="OB"), UPSHealth.CRITICAL, "shutdown trigger active"),
            (_snap(status="OB"), UPSHealth.DEGRADED, "on battery"),
            (_snap(status="OL"), UPSHealth.DEGRADED, "connection unstable"),
            (_snap(), UPSHealth.HEALTHY, "healthy"),
        ]
        for snap, health, expected in cases:
            out = redundancy_group_outlook(
                rg, {"U0@h": snap}, {"U0@h": health, "U1@h": UPSHealth.HEALTHY},
                now_age={"U0@h": 1.0})
            assert out["members"]["U0@h"]["healthReason"].startswith(expected)
        rg.triggers.on_battery_stabilization_delay = 60
        out = redundancy_group_outlook(
            rg, {"U0@h": _snap(status="OB", time_on_battery=5)},
            {"U0@h": UPSHealth.DEGRADED})
        assert out["members"]["U0@h"]["healthReason"] == "on battery (stabilizing)"
        # Missing member snapshot -> placeholder, still reported.
        assert out["members"]["U1@h"]["healthReason"] == "no data"

    @pytest.mark.unit
    def test_role_with_and_without_executor(self):
        rg = self._rg()
        rg.remote_servers = [_server()]
        base = _config(_group())
        role = redundancy_role(rg, base)
        assert role["kind"] == "remote-only" and role["redundancyGroups"] == ["rack-a"]
        executor = SimpleNamespace(config=_config(_group(is_local=True)),
                                   _uses_loopback_delegate=False)
        assert redundancy_role(rg, base, executor)["kind"] in ("local", "monitor-only")

    @pytest.mark.unit
    def test_from_state_files(self, tmp_path):
        g0 = _group(ups=UPSConfig(name="U0@h"))
        g1 = _group(ups=UPSConfig(name="U1@h"))
        rg = self._rg(2, 2)
        cfg = _config(g0, extra_groups=[g1], redundancy=[rg])
        cfg.logging.state_file = str(tmp_path / "state")
        now = time.time()
        (tmp_path / "state.U0-h").write_text(
            f"STATUS=OB DISCHRG\nBATTERY=50\nRUNTIME=2000\nEPOCH={now - 1}\n"
            "TIME_ON_BATTERY=40\nDEPLETION_RATE=1.0\nnot-a-pair\n")
        out = redundancy_outlook_from_state_files(cfg, rg, now=now)
        assert out["health"]["U0@h"] == "degraded"
        assert out["health"]["U1@h"] == "unknown"   # no state file
        assert out["outlook"]["state"] == "at-risk" or out["quorumLost"]
        assert out["members"]["U1@h"]["healthReason"] == "no data"
        # Injected states + a member with no configured group.
        rg.ups_sources.append("ghost")
        out = redundancy_outlook_from_state_files(
            cfg, rg, states={"U0@h": {"STATUS": "OL", "EPOCH": str(now)},
                             "U1@h": {"STATUS": "OL", "EPOCH": str(now)}})
        assert out["health"]["ghost"] == "unknown"
        assert out["health"]["U0@h"] == "healthy"
        # A group progress sidecar is picked up.
        sidecar = progress_sidecar_path(tmp_path / "state.redundancy-rack-a")
        sidecar.write_text(json.dumps({"state": "running"}))
        out = redundancy_outlook_from_state_files(cfg, rg, now=now)
        assert out["outlook"]["state"] == "shutting-down"


# ---------------------------------------------------------------------------
# TUI-side wrapper
# ---------------------------------------------------------------------------

class TestStateFileOutlook:
    @pytest.mark.unit
    def test_single_ups(self, tmp_path):
        g = _group(is_local=True)
        cfg = _config(g)
        cfg.logging.state_file = str(tmp_path / "state")
        now = time.time()
        state = {"STATUS": "OB DISCHRG", "BATTERY": "45", "RUNTIME": "280",
                 "EPOCH": str(now - 4), "TIME_ON_BATTERY": "100",
                 "CHECK_INTERVAL": "5", "TRIGGER_ACTIVE": "0"}
        out = state_file_outlook(cfg, g, state, now=now)
        assert out["nextTrigger"]["id"] == "criticalRuntime"
        assert out["triggerOutlook"]["timeOnBattery"] == 104
        assert out["freshness"]["ageSeconds"] == 4.0
        assert out["role"]["kind"] == "local"
        assert out["statusSummary"]["state"] == "on_battery"
        assert out["shutdownProgress"] is None
        stale = state_file_outlook(cfg, g, dict(state, EPOCH=str(now - 300)),
                                   now=now)
        assert stale["statusSummary"]["state"] == "stale"
        missing = state_file_outlook(cfg, g, None, now=now)
        assert missing["freshness"]["lastPollAt"] is None
        assert missing["statusSummary"]["state"] == "waiting"
        progress_sidecar_path(tmp_path / "state").write_text(
            json.dumps({"state": "running"}))
        out = state_file_outlook(cfg, g, state, now=now,
                                 self_test_failure_armed=True)
        assert out["statusSummary"]["state"] == "shutting_down"
        assert out["shutdownProgress"] == {"state": "running"}
        by = _by_id(out["triggerOutlook"])
        assert by["selfTestFailure"]["enabled"] is True
        attributed = state_file_outlook(
            cfg, g, dict(state, SELF_TEST_ATTRIBUTED="1"), now=now,
            self_test_failure_armed=True)
        assert _by_id(attributed["triggerOutlook"])["selfTestFailure"]["enabled"] is False

    @pytest.mark.unit
    def test_multi_ups_paths_and_membership(self, tmp_path):
        g0 = _group(ups=UPSConfig(name="U0@h"))
        g1 = _group(ups=UPSConfig(name="U1@h"))
        rg = RedundancyGroupConfig(name="rack-a", ups_sources=["U0@h", "U1@h"])
        cfg = _config(g0, extra_groups=[g1], redundancy=[rg])
        cfg.logging.state_file = str(tmp_path / "state")
        progress_sidecar_path(tmp_path / "state.U0-h").write_text("[1]")
        out = state_file_outlook(cfg, g0, {"STATUS": "OL",
                                           "EPOCH": str(time.time())})
        assert out["role"]["kind"] == "redundancy-member"
        assert out["role"]["redundancyGroups"] == ["rack-a"]
        assert out["shutdownProgress"] is None   # non-dict JSON ignored
        assert out["triggerOutlook"]["action"]["kind"] == "redundancy-advisory"


# ---------------------------------------------------------------------------
# Status vocabulary (utils)
# ---------------------------------------------------------------------------

class TestStatusVocabulary:
    @pytest.mark.unit
    @pytest.mark.parametrize("status,kw,state,severity,blink", [
        ("OL CHRG", {}, "online", "ok", False),
        ("OL RB", {}, "online", "warn", False),
        ("OB DISCHRG", {}, "on_battery", "warn", False),
        ("OB LB", {}, "low_battery", "crit", False),
        ("OL FSD", {}, "shutting_down", "crit", True),
        ("OB", {"shutting_down": True}, "shutting_down", "crit", True),
        ("OB", {"trigger_active": True}, "trigger_active", "crit", True),
        ("OB", {"connection_state": "FAILED"}, "connection_lost", "crit", False),
        ("OL", {"connection_state": "failed"}, "connection_lost", "warn", False),
        ("OL", {"stale": True}, "stale", "warn", False),
        ("OFF", {}, "output_off", "crit", False),
        ("BYPASS", {}, "bypass", "warn", False),
        ("WAIT", {}, "waiting", "warn", False),
        ("", {}, "waiting", "warn", False),
        ("CAL", {}, "unknown", "warn", False),
    ])
    def test_status_summary(self, status, kw, state, severity, blink):
        s = status_summary(status, **kw)
        assert s["state"] == state and s["severity"] == severity
        assert s["blink"] is blink
        assert s["label"] == STATUS_STATES[state][0]

    @pytest.mark.unit
    def test_detail_and_tokens(self):
        s = status_summary("ob dischrg")
        assert s["tokens"] == ["OB", "DISCHRG"]
        assert s["detail"] == "Running on battery · Battery discharging"
        assert status_summary(None)["detail"] == "Status unknown"

    @pytest.mark.unit
    def test_severity_helpers(self):
        assert [severity_rank(x) for x in ("ok", "warn", "crit", "?")] == [0, 1, 2, 1]
        assert worst_severity([]) == "ok"
        assert worst_severity(["ok", "crit", "warn"]) == "crit"
        assert worst_severity(["ok", "bogus"]) == "warn"

    @pytest.mark.unit
    @pytest.mark.parametrize("value,text", [
        (None, "unknown"), ("x", "unknown"), (0, "just now"), (1.5, "just now"),
        (12, "12s ago"), (240, "4m ago"), (3 * 3600, "3h ago"),
        (14 * 86400, "14d ago"), (-5, "just now"),
    ])
    def test_format_age(self, value, text):
        assert format_age(value) == text


# ---------------------------------------------------------------------------
# Event tier selection (status.select_event_rows)
# ---------------------------------------------------------------------------

class TestSelectEventRows:
    @staticmethod
    def _rows(power=0, diag=0, life=0, other=0):
        rows = [(i, "power") for i in range(power)]
        rows += [(1000 + i, "diagnostics") for i in range(diag)]
        rows += [(2000 + i, "lifecycle") for i in range(life)]
        rows += [(3000 + i, "noise") for i in range(other)]
        return rows

    def _sel(self, rows, n, **kw):
        from eneru.status import select_event_rows
        return select_event_rows(rows, max_events=n, tier_of=lambda r: r[1], **kw)

    @pytest.mark.unit
    def test_under_cap_and_no_cap(self):
        rows = self._rows(3, 2, other=4)
        assert len(self._sel(rows, 10)) == 5
        assert len(self._sel(rows, None)) == 5
        assert self._sel(list(reversed(rows)), 0)[0] == (0, "power")

    @pytest.mark.unit
    def test_every_tier_visible_when_power_floods(self):
        from collections import Counter
        out = self._sel(self._rows(40, 5, 1), 30)
        counts = Counter(r[1] for r in out)
        assert len(out) == 30
        assert counts["diagnostics"] == 5 and counts["lifecycle"] == 1
        assert counts["power"] == 24
        assert out == sorted(out)
        # Newest power rows kept.
        assert (39, "power") in out and (0, "power") not in out

    @pytest.mark.unit
    def test_split_and_leftover(self):
        from collections import Counter
        out = self._sel(self._rows(40, 40, 40), 10)
        assert Counter(r[1] for r in out) == {"power": 5, "diagnostics": 2,
                                              "lifecycle": 3}
        # Tiny cap: power keeps its reserve, others share what is left.
        out = self._sel(self._rows(40, 40, 40), 2)
        assert Counter(r[1] for r in out) == {"power": 1, "diagnostics": 1}
        out = self._sel(self._rows(40, 40, 40), 1)
        assert out == [(39, "power")]

    @pytest.mark.unit
    def test_single_tier_and_no_power(self):
        assert len(self._sel(self._rows(40), 10)) == 10
        out = self._sel(self._rows(0, 40, 1), 5)
        assert (2000, "lifecycle") in out and len(out) == 5
        assert len(self._sel(self._rows(0, 40), 5)) == 5


# ---------------------------------------------------------------------------
# Replacement estimate caps (H6)
# ---------------------------------------------------------------------------

class TestReplacementCaps:
    @pytest.mark.unit
    @pytest.mark.parametrize("days,text", [
        (None, "unknown"), ("x", "unknown"), (float("nan"), "unknown"),
        (0, "now"), (0.5, "<1 day"), (12, "~12 days"), (91, "~3 mo"),
        (1461, "~4 yr"), (3652.5, "> 10 yr"), (204288, "> 10 yr"),
    ])
    def test_format(self, days, text):
        assert prediction.format_replacement_eta(days) == text

    @pytest.mark.unit
    def test_bounded(self):
        b = prediction.bounded_replacement(7077, age_years=0.9,
                                           expected_life_years=5)
        assert b["source"] == "age" and b["capped"] is True
        assert b["text"] == "~4 yr" and b["days"] == round(4.1 * 365.25, 1)
        b = prediction.bounded_replacement(30, age_years=0.9,
                                           expected_life_years=5)
        assert b == {"days": 30.0, "years": 0.08, "text": "~30 days",
                     "source": "trend", "capped": False, "beyond": False}
        b = prediction.bounded_replacement(204288, age_years=None,
                                           expected_life_years=5)
        assert b["beyond"] is True and b["days"] == prediction.MAX_REPLACEMENT_DAYS
        b = prediction.bounded_replacement(None, age_years=6,
                                           expected_life_years=5)
        assert b["source"] == "age" and b["text"] == "now"
        b = prediction.bounded_replacement(None, age_years=None,
                                           expected_life_years=5)
        assert b["days"] is None and b["text"] == "unknown"
        assert prediction.bounded_replacement(
            None, age_years=1, expected_life_years=0)["source"] is None

    @pytest.mark.unit
    def test_bounded_eta_for_chart(self):
        now = 1_800_000_000.0
        day = 86400.0
        # Nearly flat decline: raw projection is decades out.
        history = [(now - (60 - i) * day, 90.0 - i * 0.001) for i in range(60)]
        out = prediction.bounded_replacement_eta(
            history, threshold_score=40, horizon_days=90, min_history_days=14,
            battery_install_date="2026-01-01", expected_life_years=5, now=now)
        raw_eta, _ = prediction.replacement_eta(
            history, threshold_score=40, horizon_days=90, min_history_days=14,
            battery_install_date=None, expected_life_years=None, now=now)
        assert raw_eta > out["etaTs"]
        assert out["capped"] is True and out["etaSource"] == "age"
        assert abs(out["etaTs"] - (now + out["days"] * day)) < 1
        empty = prediction.bounded_replacement_eta(
            [], threshold_score=40, horizon_days=90, min_history_days=14,
            battery_install_date=None, expected_life_years=5, now=now)
        assert empty["etaTs"] is None and empty["text"] == "unknown"


# ---------------------------------------------------------------------------
# Progress sidecar
# ---------------------------------------------------------------------------

class TestProgressSidecar:
    @pytest.mark.unit
    def test_persist_and_read(self, tmp_path):
        path = progress_sidecar_path(tmp_path / "state.U")
        assert path.name == "state.U.shutdown-progress.json"
        tracker = ShutdownProgress("ups", "U", sidecar_path=path)
        tracker.start("runtime low")
        tracker.phase_start("vms")
        gen = tracker.remote_start("nas", "10.0.0.2")
        assert gen == 1
        data = read_progress_sidecar(path)
        assert data["state"] == "running" and data["writtenAt"] > 0
        assert data["remotes"][0]["state"] == "running"
        tracker.finish("succeeded")
        assert read_progress_sidecar(path)["state"] == "succeeded"
        assert read_progress_sidecar(tmp_path / "missing") is None

    @pytest.mark.unit
    def test_persist_is_best_effort(self, tmp_path):
        tracker = ShutdownProgress("ups", "U")
        tracker.persist()  # no path: no-op
        tracker.sidecar_path = tmp_path / "no-such-dir" / "p.json"
        tracker.start("x")  # write fails silently
        assert tracker.snapshot()["state"] == "running"


# ---------------------------------------------------------------------------
# Monitor state file + status/API wiring
# ---------------------------------------------------------------------------

def _read_state(path):
    return dict(line.split("=", 1) for line in path.read_text().splitlines())


class TestStateFileKeys:
    @pytest.mark.unit
    def test_new_keys_and_seeded_sidecar(self, minimal_config, tmp_path):
        monitor = _parity_monitor(minimal_config, tmp_path)
        monitor.state.on_battery_start_mono = time.monotonic() - 42
        monitor.state.on_battery_start_time = int(time.time()) - 42
        monitor.state.latest_depletion_rate = 1.5
        monitor.state.trigger_active = True
        monitor.state.trigger_reason = "runtime\nlow"
        monitor._save_state({"ups.status": "OB", "battery.charge": "50"})
        data = _read_state(tmp_path / "state")
        assert data["TIMESTAMP"]  # old key kept
        assert abs(float(data["EPOCH"]) - time.time()) < 5
        assert "T" in data["TIMESTAMP_ISO"] and data["TIMESTAMP_ISO"][-6] in "+-"
        assert data["TIME_ON_BATTERY"] in ("42", "43")
        assert data["DEPLETION_RATE"] == "1.5"
        assert data["TRIGGER_ACTIVE"] == "1"
        assert data["TRIGGER_REASON"] == "runtime low"
        assert data["SELF_TEST_ATTRIBUTED"] == "0"
        assert data["CHECK_INTERVAL"] == str(monitor.config.ups.check_interval)
        sidecar = read_progress_sidecar(progress_sidecar_path(tmp_path / "state"))
        assert sidecar["state"] == "idle"
        # Seeded once only.
        monitor._shutdown_progress.persist = MagicMock()
        monitor._save_state({"ups.status": "OL"})
        monitor._shutdown_progress.persist.assert_not_called()

    @pytest.mark.unit
    def test_wall_clock_fallback_and_defensive(self, minimal_config, tmp_path):
        monitor = _parity_monitor(minimal_config, tmp_path)
        monitor.state.on_battery_start_time = int(time.time()) - 10
        monitor._save_state({"ups.status": "OB"})
        assert _read_state(tmp_path / "state")["TIME_ON_BATTERY"] in ("10", "11")
        monitor.state.on_battery_start_time = 0
        monitor._save_state({"ups.status": "OL"})
        assert _read_state(tmp_path / "state")["TIME_ON_BATTERY"] == "0"
        monitor.state.latest_depletion_rate = "garbage"
        monitor._save_state({"ups.status": "OL"})
        data = _read_state(tmp_path / "state")
        assert "EPOCH" not in data and data["STATUS"] == "OL"


class TestStatusAndApiWiring:
    def _monitor(self, minimal_config, tmp_path):
        monitor = _parity_monitor(minimal_config, tmp_path)
        with monitor.state._lock:
            monitor.state.latest_status = "OB DISCHRG"
            monitor.state.latest_runtime = "1490"
            monitor.state.latest_battery_charge = "80"
            monitor.state.latest_time_on_battery = 65
            monitor.state.latest_update_time = time.time()
            monitor.state.latest_update_mono = time.monotonic()
        return monitor

    @pytest.mark.unit
    def test_monitor_status_rows(self, minimal_config, tmp_path):
        from eneru.status import collect_status, monitor_status
        monitor = self._monitor(minimal_config, tmp_path)
        row = monitor_status(monitor)
        assert row["runtimeText"] == "24m 50s"
        assert row["timeOnBatteryText"].startswith("1m ")
        for key in ("statusSummary", "triggerOutlook", "nextTrigger", "role",
                    "freshness"):
            assert row[key] is not None
        with monitor.state._lock:
            monitor.state.latest_status = "OL"
            monitor.state.latest_runtime = ""
        row = monitor_status(monitor)
        assert row["timeOnBatteryText"] is None and row["runtimeText"] is None
        payload = collect_status(monitor)
        assert isinstance(payload["generatedAt"], float)
        with patch("eneru.outlook.monitor_outlook", side_effect=RuntimeError):
            row = monitor_status(monitor)
        assert row["triggerOutlook"] is None and row["role"] is None

    @pytest.mark.unit
    def test_redundancy_rows(self, tmp_path):
        from eneru.status import redundancy_group_statuses
        rg = RedundancyGroupConfig(name="rack-a", ups_sources=["A@h", "B@h"],
                                   min_healthy=1, triggers=_triggers())
        cfg = _config(_group(ups=UPSConfig(name="A@h")),
                      extra_groups=[_group(ups=UPSConfig(name="B@h"))],
                      redundancy=[rg])
        state = MonitorState(latest_status="OB", latest_battery_charge="50",
                             latest_runtime="100", latest_update_time=time.time(),
                             latest_update_mono=time.monotonic(),
                             latest_time_on_battery=40)
        mon_a = SimpleNamespace(config=_config(cfg.ups_groups[0]), state=state)
        source = SimpleNamespace(_monitors=[mon_a])
        rows = redundancy_group_statuses(source, cfg, ups_rows=[])
        row = rows[0]
        assert row["failingMembers"] == ["A@h", "B@h"]
        assert row["outlook"]["state"] == "quorum-lost"
        assert row["role"]["redundancyGroups"] == ["rack-a"]
        member = row["members"][0]
        assert member["nextTrigger"]["id"] == "criticalRuntime"
        assert member["healthReason"].startswith("Critical runtime")
        broken_state = MagicMock()
        broken_state.snapshot.side_effect = RuntimeError
        source._monitors = [SimpleNamespace(config=mon_a.config, state=broken_state)]
        with patch("eneru.outlook.redundancy_role", side_effect=RuntimeError):
            row = redundancy_group_statuses(source, cfg, ups_rows=[])[0]
        assert row["outlook"] is None and row["failingMembers"] is None

    @pytest.mark.unit
    def test_api_routes(self, minimal_config, tmp_path):
        from conftest import make_api_handler
        monitor = self._monitor(minimal_config, tmp_path)
        h = make_api_handler(monitor.config, source=monitor,
                             path=f"/api/v1/ups/{monitor.config.ups.name}")
        status, _, row = h._route()
        assert status == 200 and isinstance(row["generatedAt"], float)
        assert row["nextTrigger"] is not None
        h.path = f"/api/v1/ups/{monitor.config.ups.name}/shutdown-plan"
        status, _, payload = h._route()
        assert status == 200
        trig = payload["triggers"]
        assert trig["conditions"][0] == "charge below 20%"
        assert trig["outlook"]["onBattery"] is True
        assert trig["action"]["kind"]
        assert payload["role"]["kind"]
        with patch("eneru.outlook.monitor_outlook", side_effect=RuntimeError):
            status, _, payload = h._route()
        assert payload["triggers"] is None and payload["role"] is None

    @pytest.mark.unit
    def test_redundancy_plan_route(self):
        from conftest import make_api_handler
        rg = RedundancyGroupConfig(name="rack-a", ups_sources=["A@h", "B@h"],
                                   min_healthy=1)
        cfg = _config(_group(ups=UPSConfig(name="A@h")),
                      extra_groups=[_group(ups=UPSConfig(name="B@h"))],
                      redundancy=[rg])
        h = make_api_handler(
            cfg, source=SimpleNamespace(_monitors=[], _redundancy_executors={}),
            path="/api/v1/redundancy-groups/rack-a/shutdown-plan")
        status, _, payload = h._route()
        assert status == 200
        assert payload["triggers"]["conditions"] == [
            "fewer than 1 of 2 members healthy"]
        assert payload["triggers"]["memberConditions"][0] == "charge below 20%"
        assert payload["role"]["kind"] == "monitor-only"
        with patch("eneru.outlook.redundancy_role", side_effect=RuntimeError):
            status, _, payload = h._route()
        assert payload["triggers"] is None


class TestMonitorDisplayStrings:
    @pytest.mark.unit
    def test_power_restored_downtime_is_human(self):
        import inspect
        source = inspect.getsource(UPSGroupMonitor._emit_lifecycle_startup_notification)
        assert "(downtime {format_seconds(downtime)})" in source


class TestBatteryHealthReplacementBlock:
    @pytest.mark.unit
    def test_published_block_is_capped(self, minimal_config, tmp_path):
        monitor = _parity_monitor(minimal_config, tmp_path)
        cfg = SimpleNamespace(enabled=True, expected_life_years=5,
                              battery_install_date="2020-01-01")
        with patch.object(monitor, "_resolve_battery_health_config",
                          return_value=cfg), \
                patch.object(monitor, "_compute_battery_health",
                             return_value={"score": 90, "terms": {},
                                           "confidence": 1, "runtime_s": 1,
                                           "nominalRuntime": 1,
                                           "ageYears": 0.9}), \
                patch.object(monitor, "_open_store", return_value=None), \
                patch.object(monitor, "_maybe_predict_replacement",
                             return_value={"due": False,
                                           "days_remaining": 204288.0}), \
                patch.object(monitor, "_maybe_alert_health"):
            monitor._update_battery_health_periodic(now=time.time())
        health = monitor.state.latest_battery_health
        assert health["replacementDaysRemaining"] < 1600
        assert health["replacement"]["text"] == "~4 yr"
        with patch.object(monitor, "_resolve_battery_health_config",
                          return_value=cfg), \
                patch.object(monitor, "_compute_battery_health",
                             return_value={"score": 90, "terms": {},
                                           "confidence": 1, "runtime_s": 1,
                                           "nominalRuntime": 1,
                                           "ageYears": None}), \
                patch.object(monitor, "_open_store", return_value=None), \
                patch.object(monitor, "_maybe_predict_replacement",
                             return_value={"due": False,
                                           "days_remaining": None}), \
                patch.object(monitor, "_maybe_alert_health"):
            monitor._update_battery_health_periodic(now=time.time())
        health = monitor.state.latest_battery_health
        assert health["replacementDaysRemaining"] is None
        assert health["replacement"]["text"] == "unknown"
