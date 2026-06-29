"""Tests for the v6.1 config sections: battery_health, self_test, reports,
energy — parsing, per-UPS overrides, unknown-key detection, cross-field
validation, and reload classification.
"""

import pytest
import yaml

from eneru import reload as reload_mod
from eneru.config import (
    BatteryHealthConfig,
    Config,
    ConfigLoader,
    EnergyConfig,
    ReportsConfig,
    SelfTestConfig,
)


def _parse(text):
    return ConfigLoader._parse_config(yaml.safe_load(text))


def _validate(text):
    raw = yaml.safe_load(text)
    cfg = ConfigLoader._parse_config(raw)
    return cfg, [m for m in ConfigLoader.validate_config(cfg, raw) if "ERROR" in m]


# --------------------------------------------------------------------------
# defaults + parsing
# --------------------------------------------------------------------------

class TestDefaults:
    @pytest.mark.unit
    def test_defaults_present_on_empty_config(self):
        cfg = Config()
        assert isinstance(cfg.battery_health, BatteryHealthConfig)
        assert isinstance(cfg.self_test, SelfTestConfig)
        assert isinstance(cfg.reports, ReportsConfig)
        assert isinstance(cfg.energy, EnergyConfig)
        assert cfg.battery_health.enabled is True
        assert cfg.self_test.enabled is False
        assert cfg.reports.enabled is False
        assert cfg.energy.cost_per_kwh is None  # cost tracking off by default

    @pytest.mark.unit
    def test_parse_battery_health_and_replacement(self):
        cfg = _parse(
            "ups:\n  name: U@h\n"
            "battery_health:\n"
            "  update_interval: 1800\n"
            "  battery_install_date: '2024-03-01'\n"
            "  expected_life_years: 4\n"
            "  replacement:\n"
            "    threshold_score: 60\n"
            "    horizon_days: 120\n"
        )
        bh = cfg.battery_health
        assert bh.update_interval == 1800
        assert bh.battery_install_date == "2024-03-01"
        assert bh.expected_life_years == 4
        assert bh.replacement.threshold_score == 60
        assert bh.replacement.horizon_days == 120
        assert bh.replacement.min_history_days == 14  # inherited default

    @pytest.mark.unit
    def test_parse_reports_and_energy(self):
        cfg = _parse(
            "ups:\n  name: U@h\n"
            "reports:\n  enabled: true\n  weekly: true\n  weekly_day: friday\n"
            "  include: [events, energy]\n  format: csv\n"
            "energy:\n  cost_per_kwh: 0.2\n  currency: EUR\n  cost_format: '{value} EUR'\n"
        )
        assert cfg.reports.enabled and cfg.reports.weekly
        assert cfg.reports.weekly_day == "friday"
        assert cfg.reports.include == ["events", "energy"]
        assert cfg.reports.format == "csv"
        assert cfg.energy.cost_per_kwh == 0.2
        assert cfg.energy.currency == "EUR"
        assert cfg.energy.cost_format == "{value} EUR"

    @pytest.mark.unit
    def test_scalar_instead_of_dict_is_defensive(self):
        # `energy: true` (a scalar, not a mapping) must not crash; falls to {}.
        cfg = _parse("ups:\n  name: U@h\nenergy: true\nbattery_health: false\n")
        assert cfg.energy.cost_per_kwh is None
        assert cfg.battery_health.enabled is True


# --------------------------------------------------------------------------
# per-UPS overrides
# --------------------------------------------------------------------------

class TestPerUpsOverrides:
    @pytest.mark.unit
    def test_per_ups_battery_health_inherits_global(self):
        cfg = _parse(
            "battery_health:\n  update_interval: 1800\n  expected_life_years: 5\n"
            "ups:\n"
            "  - name: U1@h\n    battery_health:\n      battery_install_date: '2023-01-01'\n"
            "  - name: U2@h\n"
        )
        g1 = next(g for g in cfg.ups_groups if g.ups.name == "U1@h")
        g2 = next(g for g in cfg.ups_groups if g.ups.name == "U2@h")
        # U1 has its own block: install date set, but unset fields inherit global
        assert g1.battery_health.battery_install_date == "2023-01-01"
        assert g1.battery_health.update_interval == 1800       # inherited
        assert g1.battery_health.expected_life_years == 5      # inherited
        # U2 has no override -> None means "use global"
        assert g2.battery_health is None

    @pytest.mark.unit
    def test_per_ups_self_test_override(self):
        cfg = _parse(
            "self_test:\n  schedule: monthly\n  command: test.battery.start\n"
            "ups:\n"
            "  - name: U1@h\n    self_test:\n      schedule: weekly\n"
            "  - name: U2@h\n"
        )
        g1 = next(g for g in cfg.ups_groups if g.ups.name == "U1@h")
        assert g1.self_test.schedule == "weekly"
        assert g1.self_test.command == "test.battery.start"  # inherited


# --------------------------------------------------------------------------
# unknown-key detection
# --------------------------------------------------------------------------

class TestUnknownKeys:
    @pytest.mark.unit
    @pytest.mark.parametrize("section,bad,good", [
        ("battery_health", "expected_life_yr", "expected_life_years"),
        ("self_test", "scedule", "schedule"),
        ("reports", "frmat", "format"),
        ("energy", "currancy", "currency"),
    ])
    def test_unknown_top_level_key(self, section, bad, good):
        _, errs = _validate(f"ups:\n  name: U@h\n{section}:\n  {bad}: x\n")
        assert any(f"{section}.{bad}" in e for e in errs)
        assert any(good in e for e in errs)  # suggestion

    @pytest.mark.unit
    def test_unknown_nested_replacement_key(self):
        _, errs = _validate(
            "ups:\n  name: U@h\nbattery_health:\n  replacement:\n    horizn_days: 90\n")
        assert any("battery_health.replacement.horizn_days" in e for e in errs)

    @pytest.mark.unit
    def test_unknown_per_ups_battery_health_key(self):
        _, errs = _validate(
            "ups:\n  - name: U1@h\n    battery_health:\n      instal_date: x\n")
        assert any("instal_date" in e for e in errs)


# --------------------------------------------------------------------------
# cross-field validation
# --------------------------------------------------------------------------

class TestCrossFieldValidation:
    @pytest.mark.unit
    def test_self_test_requires_nut_control_and_auth(self):
        _, errs = _validate("ups:\n  name: U@h\nself_test:\n  enabled: true\n")
        assert any("requires nut_control.enabled" in e for e in errs)
        assert any("requires api.auth.enabled" in e for e in errs)

    @pytest.mark.unit
    def test_self_test_command_must_be_allowlisted(self):
        _, errs = _validate(
            "ups:\n  name: U@h\napi:\n  auth:\n    enabled: true\n"
            "nut_control:\n  enabled: true\n  allowed_commands: [beeper.toggle]\n"
            "self_test:\n  enabled: true\n  command: test.battery.start\n")
        assert any("not in nut_control.allowed_commands" in e for e in errs)

    @pytest.mark.unit
    def test_per_ups_self_test_override_is_validated(self):
        # Global self_test disabled, but a per-UPS override enables it with a
        # non-allowlisted command and no auth -> must be caught.
        _, errs = _validate(
            "self_test:\n  enabled: false\n"
            "ups:\n  - name: U1@h\n    self_test:\n      enabled: true\n"
            "      command: shutdown.return\n")
        assert any("U1@h" in e and "nut_control.enabled" in e for e in errs)
        assert any("U1@h" in e and "api.auth.enabled" in e for e in errs)
        assert any("shutdown.return" in e and "U1@h" in e for e in errs)

    @pytest.mark.unit
    def test_per_ups_self_test_override_valid(self):
        _, errs = _validate(
            "api:\n  auth:\n    enabled: true\n"
            "nut_control:\n  enabled: true\n  allowed_commands: [test.battery.start]\n"
            "self_test:\n  enabled: false\n"
            "ups:\n  - name: U1@h\n    self_test:\n      enabled: true\n")
        assert errs == []

    @pytest.mark.unit
    def test_global_self_test_vs_per_ups_narrowed_allowlist(self):
        # Global self_test enabled with the default command, but one UPS narrows
        # its OWN nut_control allowlist to exclude it -> caught for that UPS
        # (validated against the resolved per-UPS config, not just the global).
        _, errs = _validate(
            "api:\n  auth:\n    enabled: true\n"
            "nut_control:\n  enabled: true\n  allowed_commands: [test.battery.start]\n"
            "self_test:\n  enabled: true\n  command: test.battery.start\n"
            "ups:\n  - name: U1@h\n"
            "  - name: U2@h\n    nut_control:\n      enabled: true\n"
            "      allowed_commands: [beeper.toggle]\n")
        assert any("U2@h" in e and "not in nut_control.allowed_commands" in e
                   for e in errs)
        assert not any("U1@h" in e for e in errs)   # U1 inherits the global allowlist

    @pytest.mark.unit
    @pytest.mark.parametrize("field,val", [
        ("nominal_runtime_seconds", "abc"),
        ("expected_life_years", "ten"),
        ("update_interval", "soon"),
    ])
    def test_battery_health_rejects_non_numeric(self, field, val):
        _, errs = _validate(
            f"ups:\n  name: U@h\nbattery_health:\n  {field}: {val}\n")
        assert any(f"battery_health.{field}" in e and "must be a number" in e
                   for e in errs)

    @pytest.mark.unit
    @pytest.mark.parametrize("cmd_line", [
        "  command: ''\n",     # empty string
        "  command:\n",        # null
    ])
    def test_self_test_enabled_with_empty_command_is_error(self, cmd_line):
        # An ENABLED self_test with a missing/empty command must be a CONFIG
        # error (it would otherwise bypass the allowlist check and only fail at
        # runtime).
        _, errs = _validate(
            "ups:\n  name: U@h\napi:\n  auth:\n    enabled: true\n"
            "nut_control:\n  enabled: true\n  allowed_commands: [test.battery.start]\n"
            "self_test:\n  enabled: true\n" + cmd_line)
        assert any("is enabled but has no command" in e for e in errs)

    @pytest.mark.unit
    def test_self_test_valid_setup_no_errors(self):
        _, errs = _validate(
            "ups:\n  name: U@h\napi:\n  auth:\n    enabled: true\n"
            "nut_control:\n  enabled: true\n  allowed_commands: [test.battery.start]\n"
            "self_test:\n  enabled: true\n")
        assert errs == []

    @pytest.mark.unit
    @pytest.mark.parametrize("field,val,needle", [
        ("threshold_score", "abc", "must be a number"),
        ("threshold_score", "150", "must be <= 100"),
        ("threshold_score", "-1", "must be >= 0"),
        ("horizon_days", "soon", "must be a number"),
        ("horizon_days", "0", "must be >= 1"),
        ("min_history_days", "true", "must be a number"),
        ("min_history_days", "0", "must be >= 1"),
    ])
    def test_replacement_fields_validated(self, field, val, needle):
        _, errs = _validate(
            "ups:\n  name: U@h\nbattery_health:\n  replacement:\n"
            f"    {field}: {val}\n")
        assert any(f"replacement.{field}" in e and needle in e for e in errs), errs

    @pytest.mark.unit
    def test_replacement_fields_valid_no_errors(self):
        _, errs = _validate(
            "ups:\n  name: U@h\nbattery_health:\n  replacement:\n"
            "    threshold_score: 40\n    horizon_days: 60\n"
            "    min_history_days: 7\n")
        assert errs == []

    @pytest.mark.unit
    @pytest.mark.parametrize("val", ["-1", "abc", "true"])
    def test_energy_cost_per_kwh_must_be_nonneg_number(self, val):
        _, errs = _validate(f"ups:\n  name: U@h\nenergy:\n  cost_per_kwh: {val}\n")
        assert any("cost_per_kwh must be a non-negative number" in e for e in errs)

    @pytest.mark.unit
    def test_energy_cost_unset_is_valid(self):
        _, errs = _validate("ups:\n  name: U@h\nenergy:\n  currency: EUR\n")
        assert errs == []

    @pytest.mark.unit
    def test_energy_nominal_power_parsed(self):
        cfg = _parse("ups:\n  name: U@h\nenergy:\n  nominal_power: 1000\n")
        assert cfg.energy.nominal_power == 1000

    @pytest.mark.unit
    @pytest.mark.parametrize("val", ["-1", "0", "abc"])
    def test_energy_nominal_power_must_be_positive_number(self, val):
        _, errs = _validate(f"ups:\n  name: U@h\nenergy:\n  nominal_power: {val}\n")
        assert any("nominal_power must be a positive number" in e for e in errs)


# --------------------------------------------------------------------------
# reload classification
# --------------------------------------------------------------------------

class TestReloadClassification:
    @pytest.mark.unit
    def test_v61_sections_are_classified(self):
        classified = (reload_mod.SAFE_TOP_SECTIONS
                      + reload_mod.SUBSYSTEM_SECTIONS
                      + reload_mod.RESTART_TOP_SECTIONS)
        for section in ("battery_health", "self_test", "reports", "energy"):
            assert section in classified

    @pytest.mark.unit
    def test_safe_vs_subsystem_split(self):
        # All four v6.1 sections are read live each tick (the self-test/report
        # due-checks recompute their schedule from config every loop), so they
        # are SAFE in-place swaps -- no registered scheduler to re-init.
        for section in ("energy", "battery_health", "self_test", "reports"):
            assert section in reload_mod.SAFE_TOP_SECTIONS
