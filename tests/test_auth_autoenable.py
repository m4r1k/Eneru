"""Tests for v6.0 dynamic auth activation + explicit-flag tracking.

Auth is enforced dynamically: an explicit ``api.auth.enabled`` (true or false)
always wins; when unset, auth turns on as soon as the auth DB has a user — so
"create a user, then sign in" works with no restart, and a fresh install with no
users stays open.
"""

import pytest

from eneru import auth
from eneru.api import _auth_is_active
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
    cfg = _cfg("ups:\n  name: U@h\napi:\n  auth:\n    session_ttl: 600\n")
    assert cfg.api.auth.enabled_explicitly_set is False
    cfg = _cfg("ups:\n  name: U@h\napi:\n  enabled: true\n")
    assert cfg.api.auth.enabled_explicitly_set is False


@pytest.mark.unit
def test_enabled_explicitly_set_participates_in_equality_not_repr():
    # Unpinned vs explicitly-pinned (even to the same effective value) is a real
    # api.auth change, so the hot-reload diff must see it: equality includes the
    # flag. It stays out of repr to avoid noise.
    a = _cfg("ups:\n  name: U@h\napi:\n  auth:\n    session_ttl: 5\n").api.auth
    b = _cfg("ups:\n  name: U@h\napi:\n  auth:\n    enabled: false\n    "
             "session_ttl: 5\n").api.auth
    assert a != b                                    # unpinned != explicitly off
    assert "enabled_explicitly_set" not in repr(a)


# ----- dynamic activation (_auth_is_active) -----

def _cfg_with_db(tmp_path, body=""):
    db = str(tmp_path / "auth.db")
    cfg = _cfg(f"ups:\n  name: U@h\napi:\n  enabled: true\n  auth:\n    "
               f"db_path: {db}\n{body}")
    return cfg, db


@pytest.mark.unit
def test_inactive_when_unpinned_and_db_missing(tmp_path):
    cfg, db = _cfg_with_db(tmp_path)
    assert _auth_is_active(cfg) is False
    # The probe must not create the DB on a fresh install.
    import os
    assert not os.path.exists(db)


@pytest.mark.unit
def test_inactive_when_unpinned_and_no_users(tmp_path):
    cfg, db = _cfg_with_db(tmp_path)
    auth.AuthStore(db).user_count()  # materialize schema, zero users
    assert _auth_is_active(cfg) is False


@pytest.mark.unit
def test_active_when_unpinned_and_users_exist(tmp_path):
    cfg, db = _cfg_with_db(tmp_path)
    auth.AuthStore(db).create_user("alice", "pw")
    assert _auth_is_active(cfg) is True


@pytest.mark.unit
def test_explicit_false_wins_even_with_users(tmp_path):
    cfg, db = _cfg_with_db(tmp_path, body="    enabled: false\n")
    auth.AuthStore(db).create_user("alice", "pw")
    assert cfg.api.auth.enabled_explicitly_set is True
    assert _auth_is_active(cfg) is False


@pytest.mark.unit
def test_explicit_true_wins_even_with_no_users(tmp_path):
    cfg, db = _cfg_with_db(tmp_path, body="    enabled: true\n")
    assert _auth_is_active(cfg) is True


@pytest.mark.unit
def test_inactive_when_unpinned_and_db_unreadable(tmp_path):
    # Unpinned auth + an unreadable DB -> auth INACTIVE. This is still fail-closed
    # on the write surface: with auth inactive, reads stay open (as always) but
    # every write is hard-disabled (403), so a corrupt/unreadable DB can never
    # open up control. (It is not "auth active with a dead DB", which would only
    # make every login fail while blocking the same writes.)
    cfg, db = _cfg_with_db(tmp_path)
    with open(db, "w") as fh:
        fh.write("not a sqlite database")
    assert _auth_is_active(cfg) is False


@pytest.mark.unit
def test_inactive_when_config_has_no_api_auth():
    # A config object without api/auth attributes must not raise -> inactive.
    class Bare:
        pass
    assert _auth_is_active(Bare()) is False
