"""Unit tests for the v6.0 auth foundation (src/eneru/auth.py)."""

import builtins
import sqlite3

import pytest

from eneru import auth
from eneru.auth import (
    API_KEY_PREFIX,
    AuthError,
    AuthStore,
    UserExistsError,
    UserNotFoundError,
    generate_api_key,
    generate_password,
    hash_api_key,
    hash_password,
    verify_password,
)


@pytest.fixture
def store(tmp_path):
    return AuthStore(tmp_path / "auth.db")


# ----- password hashing -----

@pytest.mark.unit
def test_hash_verify_roundtrip():
    h = hash_password("s3cret-passphrase")
    assert h.startswith("$2b$")
    assert verify_password("s3cret-passphrase", h) is True
    assert verify_password("wrong", h) is False


@pytest.mark.unit
def test_hash_is_salted_unique():
    a = hash_password("same")
    b = hash_password("same")
    assert a != b  # random per-hash salt
    assert verify_password("same", a) and verify_password("same", b)


@pytest.mark.unit
def test_verify_rejects_malformed_hash():
    # A corrupt/empty stored hash must deny, never raise.
    assert verify_password("anything", "not-a-bcrypt-hash") is False
    assert verify_password("anything", "") is False


@pytest.mark.unit
def test_password_truncated_at_72_bytes():
    # Classic bcrypt truncation: bytes past 72 don't affect the hash. We document
    # this; here we prove it stays deterministic instead of raising.
    base = "x" * 72
    h = hash_password(base + "AAAA")
    assert verify_password(base + "ZZZZ", h) is True


@pytest.mark.unit
def test_generate_password_distinct_nonempty():
    a, b = generate_password(), generate_password()
    assert a and b and a != b


# ----- api key helpers -----

@pytest.mark.unit
def test_generate_api_key_prefix_and_uniqueness():
    k1, k2 = generate_api_key(), generate_api_key()
    assert k1.startswith(API_KEY_PREFIX) and k2.startswith(API_KEY_PREFIX)
    assert k1 != k2


@pytest.mark.unit
def test_hash_api_key_deterministic_sha256():
    k = generate_api_key()
    assert hash_api_key(k) == hash_api_key(k)
    assert len(hash_api_key(k)) == 64  # sha256 hex
    assert hash_api_key(k) != hash_api_key(generate_api_key())


# ----- user CRUD -----

@pytest.mark.unit
def test_create_list_get_user(store):
    store.create_user("alice", "pw1")
    users = store.list_users()
    assert [u["username"] for u in users] == ["alice"]
    assert users[0]["role"] == "admin"
    assert "password_hash" not in users[0]  # never leaked
    got = store.get_user("alice")
    assert got["username"] == "alice"
    assert store.get_user("ghost") is None
    assert store.user_count() == 1


@pytest.mark.unit
def test_create_duplicate_user_raises(store):
    store.create_user("alice", "pw1")
    with pytest.raises(UserExistsError):
        store.create_user("alice", "pw2")


@pytest.mark.unit
def test_set_password_updates_and_missing_raises(store):
    store.create_user("alice", "pw1")
    before = store.get_user("alice")["password_changed_at"]
    store.set_password("alice", "pw2")
    assert store.authenticate("alice", "pw2") is not None
    assert store.authenticate("alice", "pw1") is None
    assert store.get_user("alice")["password_changed_at"] >= before
    with pytest.raises(UserNotFoundError):
        store.set_password("ghost", "pw")


@pytest.mark.unit
def test_set_password_bumps_timestamp_inside_same_second(store, monkeypatch):
    monkeypatch.setattr(auth.time, "time", lambda: 1000)
    store.create_user("alice", "pw1")
    before = store.get_user("alice")["password_changed_at"]
    store.set_password("alice", "pw2")
    assert store.get_user("alice")["password_changed_at"] == before + 1


@pytest.mark.unit
def test_delete_user_and_missing_raises(store):
    store.create_user("alice", "pw1")
    store.delete_user("alice")
    assert store.get_user("alice") is None
    with pytest.raises(UserNotFoundError):
        store.delete_user("alice")


@pytest.mark.unit
def test_authenticate_paths(store):
    store.create_user("alice", "pw1")
    principal = store.authenticate("alice", "pw1")
    assert principal["username"] == "alice"
    assert principal["role"] == "admin"
    assert principal["kind"] == "user"
    assert isinstance(principal["password_changed_at"], int)
    assert store.authenticate("alice", "bad") is None
    assert store.authenticate("ghost", "pw1") is None  # dummy-hash path


# ----- api keys -----

@pytest.mark.unit
def test_api_key_lifecycle(store):
    key_id, key = store.create_api_key("grafana")
    assert key.startswith(API_KEY_PREFIX)
    keys = store.list_api_keys()
    assert keys[0]["id"] == key_id
    assert keys[0]["label"] == "grafana"
    assert keys[0]["last_used_at"] is None
    assert "key_hash" not in keys[0] and "key" not in keys[0]

    principal = store.authenticate_api_key(key)
    assert principal == {"id": key_id, "label": "grafana", "role": "admin",
                         "kind": "api_key"}
    assert store.list_api_keys()[0]["last_used_at"] is not None  # stamped

    store.revoke_api_key(key_id)
    assert store.list_api_keys() == []
    assert store.authenticate_api_key(key) is None


@pytest.mark.unit
def test_authenticate_api_key_edge_cases(store):
    assert store.authenticate_api_key("") is None
    assert store.authenticate_api_key("eneru_bogus") is None


@pytest.mark.unit
def test_revoke_missing_api_key_raises(store):
    with pytest.raises(AuthError):
        store.revoke_api_key(999)


@pytest.mark.unit
def test_empty_api_key_label_raises(store):
    with pytest.raises(AuthError):
        store.create_api_key("   ")


# ----- validation -----

@pytest.mark.unit
@pytest.mark.parametrize("bad", ["", "  ", "has space", "weird!", "x" * 65,
                                 "naïve", "аdmin"])  # last two: non-ASCII look-alikes
def test_invalid_username_raises(store, bad):
    with pytest.raises(AuthError):
        store.create_user(bad, "pw")


@pytest.mark.unit
def test_api_key_label_rejects_control_chars(store):
    # The label is echoed into the audit log; a newline could forge log lines.
    with pytest.raises(AuthError):
        store.create_api_key("evil\nINJECTED")


@pytest.mark.unit
def test_valid_username_characters(store):
    store.create_user("a.b_c-d@host", "pw")
    assert store.get_user("a.b_c-d@host") is not None


@pytest.mark.unit
def test_invalid_role_raises(store):
    with pytest.raises(AuthError):
        store.create_user("alice", "pw", role="viewer")
    with pytest.raises(AuthError):
        store.create_api_key("k", role="operator")


# ----- schema / persistence -----

@pytest.mark.unit
def test_schema_version_recorded_and_persistent(tmp_path):
    db = tmp_path / "auth.db"
    AuthStore(db).create_user("alice", "pw")
    conn = sqlite3.connect(str(db))
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()
    assert version == auth.SCHEMA_VERSION
    # A second store instance reuses the same DB; the user_version gate makes
    # this a pure read (early-return in _ensure_schema), no re-init.
    assert AuthStore(db).get_user("alice") is not None


@pytest.mark.unit
def test_db_file_is_owner_only(tmp_path):
    db = tmp_path / "auth.db"
    AuthStore(db).create_user("alice", "pw")
    # Store holds password/API-key digests — keep it owner-only like /etc/shadow.
    assert (db.stat().st_mode & 0o777) == 0o600


@pytest.mark.unit
def test_existing_db_permissions_re_tightened(tmp_path):
    import os
    db = tmp_path / "auth.db"
    AuthStore(db).create_user("alice", "pw")          # schema now current
    os.chmod(db, 0o644)                                # loosen out-of-band
    # A fresh store over the same (already-current) DB must re-harden it, not
    # skip because the schema is already up to date.
    AuthStore(db).get_user("alice")
    assert (db.stat().st_mode & 0o777) == 0o600


@pytest.mark.unit
def test_chmod_failure_is_swallowed(tmp_path, monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("filesystem rejects chmod")

    monkeypatch.setattr(auth.os, "chmod", boom)
    # Best-effort permission tightening must never break store creation.
    AuthStore(tmp_path / "auth.db").create_user("alice", "pw")
    assert AuthStore(tmp_path / "auth.db").get_user("alice") is not None


@pytest.mark.unit
def test_connect_creates_parent_directory(tmp_path):
    nested = tmp_path / "deep" / "nested" / "auth.db"
    AuthStore(nested).create_user("alice", "pw")
    assert nested.exists()


# ----- lazy bcrypt import -----

@pytest.mark.unit
def test_require_bcrypt_missing_raises(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "bcrypt":
            raise ImportError("simulated missing bcrypt")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(AuthError):
        auth.require_bcrypt()
