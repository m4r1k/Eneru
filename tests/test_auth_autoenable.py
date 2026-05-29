"""Tests for v6.0 auth auto-enable + explicit-flag tracking.

The daemon turns API auth on automatically when the operator created users but
never set ``api.auth.enabled`` — so "create a user, then sign in" just works.
An explicit value (true *or* false) always wins, and the auth DB is never
conjured into existence as a side effect.
"""

import pytest

from eneru import auth, cli
from eneru.config import ConfigLoader


def _cfg(text):
    import yaml
    return ConfigLoader._parse_config(yaml.safe_load(text))


# ----- explicit-flag tracking -----

@pytest.mark.unit
def test_enabled_explicitly_set_true_when_present():
    cfg = _cfg("ups:\n  name: U@h\napi:\n  auth:\n    enabled: false\n")
    assert cfg.api.auth.enabled_explicitly_set is True
    cfg = _cfg("ups:\n  name: U@h\napi:\n  auth:\n    enabled: true\n")
    assert cfg.api.auth.enabled_explicitly_set is True


@pytest.mark.unit
def test_enabled_explicitly_set_false_when_absent():
    # auth block present but no `enabled` key
    cfg = _cfg("ups:\n  name: U@h\napi:\n  auth:\n    session_ttl: 600\n")
    assert cfg.api.auth.enabled_explicitly_set is False
    # no auth block at all
    cfg = _cfg("ups:\n  name: U@h\napi:\n  enabled: true\n")
    assert cfg.api.auth.enabled_explicitly_set is False


@pytest.mark.unit
def test_enabled_explicitly_set_excluded_from_equality():
    # The flag must not perturb config comparisons or serialized output.
    a = _cfg("ups:\n  name: U@h\napi:\n  auth:\n    session_ttl: 5\n").api.auth
    b = _cfg("ups:\n  name: U@h\napi:\n  auth:\n    enabled: false\n    "
             "session_ttl: 5\n").api.auth
    assert a == b
    assert "enabled_explicitly_set" not in repr(a)


# ----- auto-enable helper -----

@pytest.mark.unit
def test_auto_enable_flips_on_when_users_exist(tmp_path, capsys):
    db = str(tmp_path / "auth.db")
    auth.AuthStore(db).create_user("alice", "pw")
    cfg = _cfg(f"ups:\n  name: U@h\napi:\n  enabled: true\n  auth:\n    "
               f"db_path: {db}\n")
    assert cfg.api.auth.enabled is False
    cli._auto_enable_auth_if_users_exist(cfg)
    assert cfg.api.auth.enabled is True
    assert "auto-enabled" in capsys.readouterr().out


@pytest.mark.unit
def test_auto_enable_respects_explicit_false(tmp_path, capsys):
    db = str(tmp_path / "auth.db")
    auth.AuthStore(db).create_user("alice", "pw")
    cfg = _cfg(f"ups:\n  name: U@h\napi:\n  enabled: true\n  auth:\n    "
               f"enabled: false\n    db_path: {db}\n")
    cli._auto_enable_auth_if_users_exist(cfg)
    assert cfg.api.auth.enabled is False
    assert "auto-enabled" not in capsys.readouterr().out


@pytest.mark.unit
def test_auto_enable_noop_when_api_disabled(tmp_path):
    db = str(tmp_path / "auth.db")
    auth.AuthStore(db).create_user("alice", "pw")
    cfg = _cfg(f"ups:\n  name: U@h\napi:\n  enabled: false\n  auth:\n    "
               f"db_path: {db}\n")
    cli._auto_enable_auth_if_users_exist(cfg)
    assert cfg.api.auth.enabled is False


@pytest.mark.unit
def test_auto_enable_skips_when_db_missing_and_creates_nothing(tmp_path):
    db = tmp_path / "auth.db"
    cfg = _cfg(f"ups:\n  name: U@h\napi:\n  enabled: true\n  auth:\n    "
               f"db_path: {db}\n")
    cli._auto_enable_auth_if_users_exist(cfg)
    assert cfg.api.auth.enabled is False
    # No surprise file creation on a fresh install.
    assert not db.exists()


@pytest.mark.unit
def test_auto_enable_noop_with_zero_users(tmp_path):
    db = str(tmp_path / "auth.db")
    # Materialize the DB (schema only, no users).
    assert auth.AuthStore(db).user_count() == 0
    cfg = _cfg(f"ups:\n  name: U@h\napi:\n  enabled: true\n  auth:\n    "
               f"db_path: {db}\n")
    cli._auto_enable_auth_if_users_exist(cfg)
    assert cfg.api.auth.enabled is False
