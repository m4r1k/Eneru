"""triggers.trust_battery_charge + the warning-only charge plausibility check.

Background: on 2026-10-08 a UniFi UPS reported battery.charge falling
100% -> 52% in 90 s while battery.voltage stayed at 12.2 V and its own
battery.runtime estimate still showed ~18 minutes. The depletion trigger (T3)
fired correctly on the numbers it was given; the real battery then ran the
host for 30+ minutes. These tests pin the opt-in escape hatch (skip T1/T3
for a UPS whose charge lies, with extended_time required as a safety net)
and the warning that tells the operator about it without changing any
shutdown decision.
"""

import time
from unittest.mock import MagicMock, patch

import pytest
import yaml

from eneru import MonitorState, UPSGroupMonitor
from eneru.config import ConfigLoader, TriggersConfig
from eneru.config_check import _trigger_line
from eneru.health_model import UPSHealth, assess_health
from eneru.outlook import describe_trigger_conditions, evaluate_triggers
from eneru.state import HealthSnapshot

# The 2026-10-08 UniFi samples (charge %, battery V, runtime s), one per poll
# once the switch to battery had settled.
INCIDENT = [
    (87, 12.2, 1119), (82, 12.2, 1112), (77, 12.2, 1106), (72, 12.2, 1101),
    (65, 12.2, 1095), (62, 12.2, 1089), (60, 12.2, 1084), (57, 12.2, 1078),
]


def _ups(charge, voltage, runtime, status="OB DISCHRG"):
    return {
        "ups.status": status,
        "battery.charge": str(charge),
        "battery.voltage": str(voltage),
        "battery.runtime": str(runtime),
        "ups.load": "18",
    }


@pytest.fixture
def monitor(minimal_config, tmp_path):
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
    t.on_battery_stabilization_delay = 30
    m = UPSGroupMonitor(minimal_config)
    m.state = MonitorState()
    m.logger = MagicMock()
    return m


def _on_battery_for(m, seconds):
    """Pretend the outage started ``seconds`` ago (continuing outage)."""
    m.state.previous_status = "OB DISCHRG"
    m.state.on_battery_start_time = int(time.time()) - seconds
    m.state.on_battery_start_mono = time.monotonic() - seconds


# --------------------------------------------------------------------------
# Config: parse, inherit, validate
# --------------------------------------------------------------------------

class TestConfig:

    @pytest.mark.unit
    def test_default_is_true(self):
        assert TriggersConfig().trust_battery_charge is True

    @pytest.mark.unit
    def test_parse_global_and_per_group_override(self, tmp_path):
        path = tmp_path / "c.yaml"
        path.write_text(
            "triggers:\n"
            "  trust_battery_charge: false\n"
            "ups:\n"
            "  - name: lab@h\n"
            "  - name: apc@h\n"
            "    triggers:\n"
            "      trust_battery_charge: true\n")
        cfg = ConfigLoader.load(str(path))
        assert cfg.ups_groups[0].triggers.trust_battery_charge is False
        assert cfg.ups_groups[1].triggers.trust_battery_charge is True

    @pytest.mark.unit
    def test_false_without_extended_time_is_rejected(self, minimal_config):
        minimal_config.triggers.trust_battery_charge = False
        minimal_config.triggers.extended_time.enabled = False
        errors = [m for m in ConfigLoader.validate_config(minimal_config)
                  if m.startswith("ERROR")]
        assert any("trust_battery_charge is false" in m
                   and "extended_time" in m for m in errors), errors

    @pytest.mark.unit
    def test_false_with_extended_time_is_accepted(self, minimal_config):
        minimal_config.triggers.trust_battery_charge = False
        minimal_config.triggers.extended_time.enabled = True
        msgs = ConfigLoader.validate_config(minimal_config)
        assert not any("trust_battery_charge" in m for m in msgs), msgs

    @pytest.mark.unit
    def test_redundancy_group_gets_the_same_rule(self, tmp_path):
        path = tmp_path / "c.yaml"
        path.write_text(
            "ups:\n"
            "  - name: a@h\n"
            "  - name: b@h\n"
            "redundancy_groups:\n"
            "  - name: rg\n"
            "    ups_sources: [a@h, b@h]\n"
            "    triggers:\n"
            "      trust_battery_charge: false\n"
            "      extended_time:\n"
            "        enabled: false\n")
        cfg = ConfigLoader.load(str(path))
        errors = [m for m in ConfigLoader.validate_config(cfg)
                  if m.startswith("ERROR")]
        assert any(m.startswith("ERROR: redundancy_groups['rg'].triggers."
                                "trust_battery_charge") for m in errors), errors

    @pytest.mark.unit
    def test_quoted_false_is_a_schema_error(self, tmp_path):
        text = ("ups:\n  name: u@h\ntriggers:\n"
                "  trust_battery_charge: \"false\"\n")
        path = tmp_path / "c.yaml"
        path.write_text(text)
        cfg = ConfigLoader.load(str(path))
        errors = ConfigLoader.validate_config(cfg, yaml.safe_load(text))
        assert any("trust_battery_charge" in m and m.startswith("ERROR")
                   for m in errors), errors

    @pytest.mark.unit
    def test_unknown_key_sweep_accepts_it(self, tmp_path):
        text = ("ups:\n  name: u@h\ntriggers:\n"
                "  trust_battery_charge: false\n")
        path = tmp_path / "c.yaml"
        path.write_text(text)
        cfg = ConfigLoader.load(str(path))
        msgs = ConfigLoader.validate_config(cfg, yaml.safe_load(text))
        assert not any("trust_battery_charge" in m for m in msgs), msgs

    @pytest.mark.unit
    def test_anomaly_event_is_suppressible(self, tmp_path):
        text = ("ups:\n  name: u@h\nnotifications:\n"
                "  suppress: [BATTERY_CHARGE_ANOMALY]\n")
        path = tmp_path / "c.yaml"
        path.write_text(text)
        cfg = ConfigLoader.load(str(path))
        msgs = ConfigLoader.validate_config(cfg, yaml.safe_load(text))
        assert not any("BATTERY_CHARGE_ANOMALY" in m for m in msgs), msgs


# --------------------------------------------------------------------------
# Monitor: T1/T3 stand down, everything else still fires
# --------------------------------------------------------------------------

class TestUntrustedTriggers:

    @pytest.mark.unit
    def test_low_charge_does_not_fire(self, monitor):
        monitor.config.triggers.trust_battery_charge = False
        _on_battery_for(monitor, 120)
        with patch.object(monitor, "_trigger_immediate_shutdown") as sd:
            monitor._handle_on_battery(_ups(5, 12.2, 1000))
        sd.assert_not_called()

    @pytest.mark.unit
    def test_garbage_charge_logs_no_invalid_reading_noise(self, monitor):
        monitor.config.triggers.trust_battery_charge = False
        _on_battery_for(monitor, 120)
        with patch.object(monitor, "_log_invalid_reading") as invalid, \
                patch.object(monitor, "_trigger_immediate_shutdown"):
            monitor._handle_on_battery(_ups("n/a", 12.2, 1000))
        assert not any(c.args[0] == "battery.charge"
                       for c in invalid.call_args_list)

    @pytest.mark.unit
    def test_high_depletion_does_not_fire(self, monitor):
        monitor.config.triggers.trust_battery_charge = False
        _on_battery_for(monitor, 120)
        with patch.object(monitor, "_calculate_depletion_rate",
                          return_value=32.0), \
                patch.object(monitor, "_trigger_immediate_shutdown") as sd:
            monitor._handle_on_battery(_ups(52, 12.2, 1065))
        sd.assert_not_called()

    @pytest.mark.unit
    def test_high_depletion_fires_when_trusted(self, monitor):
        _on_battery_for(monitor, 120)
        with patch.object(monitor, "_calculate_depletion_rate",
                          return_value=32.0), \
                patch.object(monitor, "_trigger_immediate_shutdown") as sd:
            monitor._handle_on_battery(_ups(52, 12.2, 1065))
        sd.assert_called_once()
        assert "Depletion rate" in sd.call_args[0][0]

    @pytest.mark.unit
    def test_runtime_trigger_still_fires(self, monitor):
        monitor.config.triggers.trust_battery_charge = False
        _on_battery_for(monitor, 120)
        with patch.object(monitor, "_trigger_immediate_shutdown") as sd:
            monitor._handle_on_battery(_ups(5, 11.0, 300))
        sd.assert_called_once()
        assert "Runtime" in sd.call_args[0][0]

    @pytest.mark.unit
    def test_extended_time_still_fires(self, monitor):
        monitor.config.triggers.trust_battery_charge = False
        _on_battery_for(monitor, 1000)
        with patch.object(monitor, "_trigger_immediate_shutdown") as sd:
            monitor._handle_on_battery(_ups(5, 12.0, 1200))
        sd.assert_called_once()
        assert "Time on battery" in sd.call_args[0][0]

    @pytest.mark.unit
    def test_plausibility_check_skipped_when_untrusted(self, monitor):
        monitor.config.triggers.trust_battery_charge = False
        _on_battery_for(monitor, 120)
        with patch.object(monitor, "_check_charge_plausibility") as chk, \
                patch.object(monitor, "_trigger_immediate_shutdown"):
            monitor._handle_on_battery(_ups(52, 12.2, 1065))
        chk.assert_not_called()


# --------------------------------------------------------------------------
# Warning-only plausibility check
# --------------------------------------------------------------------------

class TestPlausibilityCheck:

    @staticmethod
    def _feed(m, samples, rate=32.0, tob=60, delay=30):
        for charge, volt, runtime in samples:
            m._check_charge_plausibility(_ups(charge, volt, runtime),
                                         rate, tob, delay)

    @pytest.mark.unit
    def test_incident_replay_warns_once(self, monitor):
        with patch.object(monitor, "_log_power_event") as ev:
            self._feed(monitor, INCIDENT)
        ev.assert_called_once()
        event, details = ev.call_args[0]
        assert event == "BATTERY_CHARGE_ANOMALY"
        assert "trust_battery_charge: false" in details
        assert "12.2 V" in details
        assert "no shutdown decision was changed" in details

    @pytest.mark.unit
    def test_needs_three_consecutive_polls(self, monitor):
        with patch.object(monitor, "_log_power_event") as ev:
            self._feed(monitor, INCIDENT[:3])  # reference + 2 disagreements
            ev.assert_not_called()
            self._feed(monitor, INCIDENT[3:4])
            ev.assert_called_once()

    @pytest.mark.unit
    def test_agreeing_poll_resets_the_count(self, monitor):
        with patch.object(monitor, "_log_power_event") as ev:
            self._feed(monitor, INCIDENT[:3])
            # Runtime now agrees with the charge (about 2 min left).
            self._feed(monitor, [(60, 12.2, 120)])
            self._feed(monitor, INCIDENT[3:5])
            ev.assert_not_called()

    @pytest.mark.unit
    def test_falling_voltage_means_the_charge_is_honest(self, monitor):
        samples = [(87, 12.2, 1119)] + [
            (80 - i * 5, 11.6 - i * 0.1, 1100) for i in range(5)]
        with patch.object(monitor, "_log_power_event") as ev:
            self._feed(monitor, samples)
        ev.assert_not_called()

    @pytest.mark.unit
    def test_runtime_agreeing_with_charge_is_not_flagged(self, monitor):
        samples = [(87, 12.2, 200), (80, 12.2, 180), (75, 12.2, 160),
                   (70, 12.2, 150), (65, 12.2, 140)]
        with patch.object(monitor, "_log_power_event") as ev:
            self._feed(monitor, samples)
        ev.assert_not_called()

    @pytest.mark.unit
    def test_waits_for_the_switchover_to_settle(self, monitor):
        with patch.object(monitor, "_log_power_event") as ev:
            self._feed(monitor, INCIDENT, tob=20, delay=30)
            self._feed(monitor, INCIDENT, tob=5, delay=0)  # 10 s floor
        ev.assert_not_called()
        assert monitor.state.charge_check_ref_voltage is None

    @pytest.mark.unit
    @pytest.mark.parametrize("charge,volt,runtime", [
        ("", 12.2, 1000), (50, "", 1000), (50, 12.2, ""),
        (0, 12.2, 1000), (50, 0, 1000), (50, 12.2, 0),
    ])
    def test_missing_or_non_positive_readings_never_warn(
            self, monitor, charge, volt, runtime):
        with patch.object(monitor, "_log_power_event") as ev:
            self._feed(monitor, [(charge, volt, runtime)] * 5)
        ev.assert_not_called()
        assert monitor.state.charge_check_count == 0

    @pytest.mark.unit
    @pytest.mark.parametrize("rate", [0.0, None])
    def test_no_depletion_rate_never_warns(self, monitor, rate):
        with patch.object(monitor, "_log_power_event") as ev:
            self._feed(monitor, INCIDENT, rate=rate)
        ev.assert_not_called()

    @pytest.mark.unit
    def test_new_outage_rearms_the_warning(self, monitor):
        with patch.object(monitor, "_log_power_event") as ev, \
                patch.object(monitor, "_trigger_immediate_shutdown"), \
                patch("eneru.monitor.run_command"):
            self._feed(monitor, INCIDENT)
            assert monitor.state.charge_anomaly_reported is True
            # A fresh OL -> OB transition clears the per-outage state.
            monitor.state.previous_status = "OL"
            monitor._handle_on_battery(_ups(100, 12.8, 3600))
        assert monitor.state.charge_anomaly_reported is False
        assert monitor.state.charge_check_ref_voltage is None
        assert monitor.state.charge_check_count == 0
        assert ev.call_args_list[0][0][0] == "BATTERY_CHARGE_ANOMALY"

    @pytest.mark.unit
    def test_warning_does_not_change_the_shutdown_decision(self, monitor):
        """Through the real handler: the warning lands AND T3 still fires."""
        _on_battery_for(monitor, 120)
        events = []
        with patch.object(monitor, "_calculate_depletion_rate",
                          return_value=32.0), \
                patch.object(monitor, "_log_power_event",
                             side_effect=lambda e, d, **k: events.append(e)), \
                patch.object(monitor, "_trigger_immediate_shutdown") as sd:
            for charge, volt, runtime in INCIDENT[:4]:
                monitor._handle_on_battery(_ups(charge, volt, runtime))
        assert events == ["BATTERY_CHARGE_ANOMALY"]
        assert sd.call_count == 4
        assert all("Depletion rate" in c.args[0] for c in sd.call_args_list)

    @pytest.mark.unit
    def test_final_poll_warns_on_the_first_disagreement(self, monitor):
        """Slow polling: the rate first appears on the poll T3 fires, so the
        3-poll confirmation can't complete; the last chance warns at once."""
        with patch.object(monitor, "_log_power_event") as ev:
            self._feed(monitor, INCIDENT[:1])          # reference voltage
            monitor._check_charge_plausibility(
                _ups(*INCIDENT[1]), 32.0, 90, 30, final_poll=True)
        ev.assert_called_once()

    @pytest.mark.unit
    def test_slow_polling_still_warns_before_t3_fires(self, monitor):
        """check_interval=5 through the real handler: the depletion rate
        exists only from the poll T3 fires on; the warning still lands."""
        monitor.config.ups.check_interval = 5
        events = []
        with patch.object(monitor, "_log_power_event",
                          side_effect=lambda e, d, **k: events.append(e)), \
                patch.object(monitor, "_trigger_immediate_shutdown") as sd:
            _on_battery_for(monitor, 40)
            with patch.object(monitor, "_calculate_depletion_rate",
                              return_value=0.0):
                monitor._handle_on_battery(_ups(87, 12.2, 1119))
            _on_battery_for(monitor, 95)
            with patch.object(monitor, "_calculate_depletion_rate",
                              return_value=32.0):
                monitor._handle_on_battery(_ups(52, 12.2, 1065))
        assert events == ["BATTERY_CHARGE_ANOMALY"]
        sd.assert_called_once()
        assert "Depletion rate" in sd.call_args[0][0]

    @pytest.mark.unit
    def test_a_failing_check_never_breaks_the_decision(self, monitor):
        _on_battery_for(monitor, 120)
        with patch.object(monitor, "_check_charge_plausibility",
                          side_effect=RuntimeError("boom")), \
                patch.object(monitor, "_calculate_depletion_rate",
                             return_value=32.0), \
                patch.object(monitor, "_log_message") as log, \
                patch.object(monitor, "_trigger_immediate_shutdown") as sd:
            monitor._handle_on_battery(_ups(52, 12.2, 1065))
        sd.assert_called_once()
        assert any("plausibility check failed: boom" in c.args[0]
                   for c in log.call_args_list)

    @pytest.mark.unit
    def test_notification_is_a_warning(self, monitor):
        monitor._stats_store = MagicMock()
        with patch.object(monitor, "_send_notification") as notify, \
                patch("eneru.monitor.run_command"):
            monitor._log_power_event("BATTERY_CHARGE_ANOMALY", "details")
        body, kind = notify.call_args[0]
        assert "BATTERY CHARGE READING LOOKS WRONG" in body
        assert kind == monitor.config.NOTIFY_WARNING


# --------------------------------------------------------------------------
# Display models: outlook, health model, config check
# --------------------------------------------------------------------------

def _untrusted():
    t = TriggersConfig()
    t.on_battery_stabilization_delay = 0
    t.trust_battery_charge = False
    return t


class TestDisplayModels:

    @pytest.mark.unit
    def test_outlook_disables_charge_rows(self):
        out = evaluate_triggers(_untrusted(), status="OB DISCHRG",
                                battery_charge="5", runtime="1000",
                                depletion_rate=40.0, time_on_battery=200)
        rows = {r["id"]: r for r in out["triggers"]}
        for tid in ("lowBattery", "depletionRate"):
            assert rows[tid]["enabled"] is False
            assert rows[tid]["state"] == "disabled"
            assert "trust_battery_charge" in rows[tid]["text"]
        assert out["firing"] == []
        assert out["next"]["id"] == "criticalRuntime"

    @pytest.mark.unit
    def test_outlook_disabled_row_without_charge(self):
        out = evaluate_triggers(_untrusted(), status="OB", battery_charge="",
                                runtime="1000")
        row = next(r for r in out["triggers"] if r["id"] == "lowBattery")
        assert row["state"] == "disabled" and row["value"] is None

    @pytest.mark.unit
    def test_outlook_trusted_still_fires(self):
        t = _untrusted()
        t.trust_battery_charge = True
        out = evaluate_triggers(t, status="OB DISCHRG", battery_charge="5",
                                runtime="1000", time_on_battery=200)
        assert "lowBattery" in out["firing"]

    @pytest.mark.unit
    def test_describe_conditions_omits_charge_triggers(self):
        text = " | ".join(describe_trigger_conditions(_untrusted()))
        assert "charge below" not in text
        assert "depletion above" not in text
        assert "runtime below" in text

    @pytest.mark.unit
    def test_health_model_ignores_charge(self):
        snap = HealthSnapshot(
            status="OB", battery_charge="5", runtime="1800", load="20",
            depletion_rate=40.0, time_on_battery=200,
            last_update_time=1_000_000.0, connection_state="OK",
            trigger_active=False, trigger_reason="", stale_data_count=0)
        assert assess_health(snap, _untrusted(), 1,
                             now=1_000_000.0) == UPSHealth.DEGRADED
        t = _untrusted()
        t.trust_battery_charge = True
        assert assess_health(snap, t, 1, now=1_000_000.0) == UPSHealth.CRITICAL

    @pytest.mark.unit
    def test_config_check_trigger_line(self):
        line = _trigger_line(_untrusted())
        assert "charge ignored" in line
        assert "battery <=" not in line and "drain >" not in line
