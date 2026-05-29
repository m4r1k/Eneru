"""Validation tests for v6.0 config additions (auth, nut_control)."""

import yaml

import pytest

from eneru.config import ConfigLoader


def _validate(text):
    raw = yaml.safe_load(text)
    cfg = ConfigLoader._parse_config(raw)
    return cfg, ConfigLoader.validate_config(cfg, raw)


@pytest.mark.unit
def test_session_ttl_must_be_positive_int():
    _, errs = _validate("ups:\n  name: U@h\napi:\n  auth:\n    session_ttl: '1h'\n")
    assert any("session_ttl must be an integer >= 1" in e for e in errs)
    _, errs = _validate("ups:\n  name: U@h\napi:\n  auth:\n    session_ttl: 0\n")
    assert any("session_ttl must be an integer >= 1" in e for e in errs)


@pytest.mark.unit
def test_nut_control_timeout_must_be_positive_int():
    _, errs = _validate("ups:\n  name: U@h\nnut_control:\n  timeout: 'soon'\n")
    assert any("nut_control.timeout must be an integer >= 1" in e for e in errs)


@pytest.mark.unit
def test_nut_control_allowlist_must_be_list_and_parse_safely():
    # A scalar is reported as a config error...
    cfg, errs = _validate(
        "ups:\n  name: U@h\nnut_control:\n  allowed_commands: load.off\n")
    assert any("nut_control.allowed_commands must be a list" in e for e in errs)
    # ...and parsing did not crash or turn it into a character list.
    assert cfg.nut_control.allowed_commands == []


@pytest.mark.unit
def test_nut_control_allowlist_null_does_not_crash():
    cfg, errs = _validate(
        "ups:\n  name: U@h\nnut_control:\n  allowed_variables: null\n")
    assert cfg.nut_control.allowed_variables == []
    # null is YAML-absent-ish; no list-type error is required for null.
    assert not any("allowed_variables must be a list" in e for e in errs)


@pytest.mark.unit
def test_per_group_nut_control_scalar_allowlist_is_error():
    # A narrowed group must never silently widen to the global allowlist via a
    # malformed (scalar) value — it has to be a hard config error.
    _, errs = _validate(
        "api:\n  auth:\n    enabled: true\n"
        "nut_control:\n  enabled: true\n  allowed_commands: [beeper.toggle]\n"
        "ups:\n  - name: U1@h\n    nut_control:\n      allowed_commands: load.off\n")
    assert any("ups 'U1@h' nut_control.allowed_commands must be a list" in e
               for e in errs)


@pytest.mark.unit
def test_per_group_nut_control_bad_timeout_and_unknown_key():
    _, errs = _validate(
        "ups:\n  - name: U1@h\n    nut_control:\n      timeout: 'fast'\n"
        "      allowed_command: [x]\n")
    assert any("ups 'U1@h' nut_control.timeout must be an integer" in e for e in errs)
    assert any("ups 'U1@h' nut_control.allowed_command" in e for e in errs)


@pytest.mark.unit
def test_per_group_nut_control_enabled_is_rejected():
    # The feature is gated globally; a per-group `enabled` is ignored at runtime,
    # so it must be a hard error rather than a silent no-op.
    _, errs = _validate(
        "ups:\n  - name: U1@h\n    nut_control:\n      enabled: false\n")
    assert any("must not set 'enabled'" in e for e in errs)


@pytest.mark.unit
def test_per_group_nut_control_non_mapping_is_error():
    _, errs = _validate("ups:\n  - name: U1@h\n    nut_control: true\n")
    assert any("nut_control for UPS 'U1@h' must be a mapping" in e for e in errs)


@pytest.mark.unit
def test_per_group_inherits_global_but_empty_list_denies():
    cfg, errs = _validate(
        "api:\n  auth:\n    enabled: true\n"
        "nut_control:\n  enabled: true\n  username: glob\n  password: gpw\n"
        "  allowed_commands: [beeper.toggle]\n  timeout: 7\n"
        "ups:\n  - name: U1@h\n    nut_control:\n      allowed_commands: []\n"
        "  - name: U2@h\n    nut_control:\n      username: u2\n")
    assert [e for e in errs if "ERROR" in e] == []
    g1 = next(g for g in cfg.ups_groups if g.ups.name == "U1@h").nut_control
    g2 = next(g for g in cfg.ups_groups if g.ups.name == "U2@h").nut_control
    # explicit empty -> deny-all (NOT inherited)
    assert g1.allowed_commands == []
    # unset fields inherit the global config
    assert g1.username == "glob" and g1.timeout == 7
    assert g2.username == "u2" and g2.allowed_commands == ["beeper.toggle"]


@pytest.mark.unit
def test_valid_v6_config_has_no_errors():
    cfg, errs = _validate(
        "ups:\n  name: U@h\n"
        "api:\n  enabled: true\n  auth:\n    enabled: true\n    session_ttl: 600\n"
        "nut_control:\n  enabled: true\n  timeout: 5\n"
        "  allowed_commands: [beeper.toggle]\n  allowed_variables: []\n")
    assert [e for e in errs if "ERROR" in e] == []
