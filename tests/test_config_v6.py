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
def test_notifications_non_numeric_value_warns_and_falls_back(capsys):
    """A non-numeric notifications int field must not crash parsing: it prints
    a warning and falls back to the default (covers the _as_int ValueError
    guard in _parse_notifications)."""
    cfg, _ = _validate(
        "ups:\n  name: U@h\n"
        "notifications:\n  enabled: true\n  retention_days: not-a-number\n")
    out = capsys.readouterr().out
    assert "not numeric" in out
    assert "retention_days" in out
    # Fell back to a usable integer default rather than carrying the string.
    assert isinstance(cfg.notifications.retention_days, int)


@pytest.mark.unit
def test_non_root_remote_unparseable_shutdown_command_still_warns_use_sudo():
    """A shutdown_command that can't be shell-split (unbalanced quote) is
    treated as "does not invoke sudo", so a non-root server with use_sudo:false
    still earns the use_sudo warning (covers the shlex.split ValueError guard
    in _command_invokes_sudo)."""
    _, errs = _validate(
        "ups:\n  name: U@h\n"
        "remote_servers:\n"
        "  - name: nas\n"
        "    enabled: true\n"
        "    host: 10.0.0.5\n"
        "    user: admin\n"
        "    use_sudo: false\n"
        "    shutdown_command: 'sudo \"unbalanced'\n")
    assert any("use_sudo is false" in e for e in errs), errs


@pytest.mark.unit
def test_non_root_remote_empty_shutdown_command_still_warns_use_sudo():
    """A whitespace-only shutdown_command yields no argv[0], so it cannot be
    invoking sudo; the non-root server still needs use_sudo (covers the
    empty-parts guard in _command_invokes_sudo)."""
    _, errs = _validate(
        "ups:\n  name: U@h\n"
        "remote_servers:\n"
        "  - name: nas\n"
        "    enabled: true\n"
        "    host: 10.0.0.5\n"
        "    user: admin\n"
        "    use_sudo: false\n"
        "    shutdown_command: '   '\n")
    assert any("use_sudo is false" in e for e in errs), errs


@pytest.mark.unit
def test_valid_v6_config_has_no_errors():
    cfg, errs = _validate(
        "ups:\n  name: U@h\n"
        "api:\n  enabled: true\n  auth:\n    enabled: true\n    session_ttl: 600\n"
        "nut_control:\n  enabled: true\n  timeout: 5\n"
        "  allowed_commands: [beeper.toggle]\n  allowed_variables: []\n")
    assert [e for e in errs if "ERROR" in e] == []


# ----- F-016: api.allowed_hosts plumbing -----

@pytest.mark.unit
def test_api_allowed_hosts_list_parses():
    cfg, errs = _validate(
        "ups:\n  name: U@h\napi:\n  allowed_hosts:\n    - eneru.lan\n"
        "    - ups.example.com\n")
    assert cfg.api.allowed_hosts == ["eneru.lan", "ups.example.com"]
    assert [e for e in errs if "ERROR" in e] == []


@pytest.mark.unit
def test_api_allowed_hosts_scalar_is_error_and_parses_safely():
    # A scalar would char-split; the schema gate rejects it as a list...
    cfg, errs = _validate(
        "ups:\n  name: U@h\napi:\n  allowed_hosts: myhost\n")
    assert any("api.allowed_hosts' must be a list" in e for e in errs)
    # ...and parsing degraded to the empty default instead of char-splitting.
    assert cfg.api.allowed_hosts == []


@pytest.mark.unit
def test_api_allowed_hosts_unknown_key_not_reported():
    # allowed_hosts is a KNOWN api key, so it must not surface as unknown.
    _, errs = _validate(
        "ups:\n  name: U@h\napi:\n  allowed_hosts: [eneru.lan]\n")
    assert not any("allowed_hosts" in e and "unknown" in e.lower() for e in errs)


# ----- F-032: NUT control credentials stay out of repr() -----

@pytest.mark.unit
def test_nut_control_repr_hides_credentials():
    from eneru.config import NutControlConfig
    text = repr(NutControlConfig(username="operator", password="s3cret"))
    assert "s3cret" not in text
    assert "operator" not in text
