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
def test_valid_v6_config_has_no_errors():
    cfg, errs = _validate(
        "ups:\n  name: U@h\n"
        "api:\n  enabled: true\n  auth:\n    enabled: true\n    session_ttl: 600\n"
        "nut_control:\n  enabled: true\n  timeout: 5\n"
        "  allowed_commands: [beeper.toggle]\n  allowed_variables: []\n")
    assert [e for e in errs if "ERROR" in e] == []
