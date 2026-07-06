"""Unit tests for the v6.0 API auth middleware + write-path (api.py)."""

import json
import socket
import time
from io import BytesIO
from unittest.mock import MagicMock

import pytest

import eneru.api as api_module
from eneru.api import (
    APIBadRequest,
    APIForbidden,
    APIPayloadTooLarge,
    APIUnauthorized,
    EneruAPIHandler,
    MAX_BODY_BYTES,
    REQUEST_READ_TIMEOUT_SECONDS,
    SessionManager,
)
from eneru.auth import AuthStore


def _handler(config, *, source=None, auth_store=None, sessions=None,
             path="/", headers=None, body=b""):
    h = object.__new__(EneruAPIHandler)
    h.path = path
    h.api_config = config
    h.api_source = source if source is not None else MagicMock()
    h.api_auth = auth_store
    h.api_sessions = sessions
    hdrs = dict(headers or {})
    if body and "Content-Length" not in hdrs:
        hdrs["Content-Length"] = str(len(body))
    h.headers = hdrs
    h.rfile = BytesIO(body)
    return h


def _enable_auth(config, *, require_for_reads=False, ttl=3600):
    config.api.auth.enabled = True
    # Pin it as an explicit operator choice so the effective-auth path can't be
    # mistaken for the unset/auto-enable case.
    config.api.auth.enabled_explicitly_set = True
    config.api.auth.require_for_reads = require_for_reads
    config.api.auth.session_ttl = ttl


# ----- SessionManager -----

@pytest.mark.unit
def test_session_create_validate_invalidate():
    mgr = SessionManager(3600)
    principal = {"username": "alice", "role": "admin", "kind": "user"}
    token = mgr.create(principal)
    assert mgr.validate(token) == principal
    assert mgr.validate("nope") is None
    assert mgr.invalidate(token) is True
    assert mgr.validate(token) is None
    assert mgr.invalidate(token) is False


@pytest.mark.unit
def test_session_expiry(monkeypatch):
    mgr = SessionManager(3600)
    token = mgr.create({"username": "a", "kind": "user"})
    # Force the stored expiry into the past.
    principal, _ = mgr._sessions[token]
    mgr._sessions[token] = (principal, time.time() - 1)
    assert mgr.validate(token) is None
    assert token not in mgr._sessions  # expired entry is reaped


# ----- _authorize matrix -----

@pytest.mark.unit
def test_authorize_auth_disabled(minimal_config):
    h = _handler(minimal_config)
    assert h._authorize(write=False) is None        # reads open
    with pytest.raises(APIForbidden):               # writes hard-disabled
        h._authorize(write=True)


@pytest.mark.unit
def test_authorize_auth_enabled_anonymous(minimal_config):
    _enable_auth(minimal_config)
    h = _handler(minimal_config, sessions=SessionManager(3600))
    assert h._authorize(write=False) is None        # read open by default
    with pytest.raises(APIUnauthorized):            # write needs a credential
        h._authorize(write=True)


@pytest.mark.unit
def test_authorize_require_for_reads(minimal_config):
    _enable_auth(minimal_config, require_for_reads=True)
    h = _handler(minimal_config, sessions=SessionManager(3600))
    with pytest.raises(APIUnauthorized):
        h._authorize(write=False)


@pytest.mark.unit
def test_authorize_with_valid_session(minimal_config):
    _enable_auth(minimal_config)
    sessions = SessionManager(3600)
    token = sessions.create({"username": "alice", "role": "admin", "kind": "user"})
    h = _handler(minimal_config, sessions=sessions,
                 headers={"Authorization": f"Bearer {token}"})
    assert h._authorize(write=True)["username"] == "alice"
    assert h._authorize(write=False)["username"] == "alice"


# ----- credential extraction / resolution -----

@pytest.mark.unit
def test_bearer_token_sources(minimal_config):
    assert _handler(minimal_config,
                    headers={"Authorization": "Bearer abc"})._bearer_token() == "abc"
    # Scheme is case-insensitive (RFC 7235).
    assert _handler(minimal_config,
                    headers={"Authorization": "bearer abc"})._bearer_token() == "abc"
    assert _handler(minimal_config,
                    headers={"X-API-Key": "xyz"})._bearer_token() == "xyz"
    assert _handler(minimal_config)._bearer_token() is None


@pytest.mark.unit
def test_session_create_purges_expired():
    mgr = SessionManager(3600)
    t1 = mgr.create({"username": "a", "kind": "user"})
    principal, _ = mgr._sessions[t1]
    mgr._sessions[t1] = (principal, time.time() - 1)  # force-expire
    t2 = mgr.create({"username": "b", "kind": "user"})
    assert t1 not in mgr._sessions  # purged on the next create
    assert t2 in mgr._sessions


@pytest.mark.unit
def test_authenticate_request_session_then_apikey(minimal_config, tmp_path):
    store = AuthStore(tmp_path / "auth.db")
    store.create_user("alice", "pw")
    _, key = store.create_api_key("ci")
    sessions = SessionManager(3600)
    stoken = sessions.create({"username": "alice", "role": "admin", "kind": "user"})

    # session token wins
    h = _handler(minimal_config, auth_store=store, sessions=sessions,
                 headers={"Authorization": f"Bearer {stoken}"})
    assert h._authenticate_request()["kind"] == "user"

    # api key resolves when token isn't a session
    h = _handler(minimal_config, auth_store=store, sessions=sessions,
                 headers={"X-API-Key": key})
    assert h._authenticate_request()["kind"] == "api_key"

    # garbage resolves to nobody
    h = _handler(minimal_config, auth_store=store, sessions=sessions,
                 headers={"Authorization": "Bearer nope"})
    assert h._authenticate_request() is None


@pytest.mark.unit
def test_authenticate_request_defensive_paths(minimal_config):
    # api-key lookup raising is swallowed -> None (fail closed)
    store = MagicMock()
    store.authenticate_api_key.side_effect = RuntimeError("db down")
    h = _handler(minimal_config, auth_store=store, sessions=None,
                 headers={"Authorization": "Bearer tok"})
    assert h._authenticate_request() is None

    # no store and no sessions configured -> None
    h = _handler(minimal_config, auth_store=None, sessions=None,
                 headers={"Authorization": "Bearer tok"})
    assert h._authenticate_request() is None


@pytest.mark.unit
def test_session_invalidated_when_user_deleted(minimal_config, tmp_path):
    # A session outlives the user row it was minted from; deleting the user must
    # end the session (the deleted-user-stays-signed-in bug).
    store = AuthStore(tmp_path / "auth.db")
    store.create_user("alice", "pw")
    sessions = SessionManager(3600)
    token = sessions.create({"username": "alice", "role": "admin", "kind": "user"})
    h = _handler(minimal_config, auth_store=store, sessions=sessions,
                 headers={"Authorization": f"Bearer {token}"})
    assert h._authenticate_request()["username"] == "alice"   # still valid

    store.delete_user("alice")
    assert h._authenticate_request() is None                  # now rejected
    assert sessions.validate(token) is None                   # token reaped


@pytest.mark.unit
def test_session_invalidated_when_password_changes(minimal_config, tmp_path):
    store = AuthStore(tmp_path / "auth.db")
    store.create_user("alice", "pw1")
    principal = store.authenticate("alice", "pw1")
    sessions = SessionManager(3600)
    token = sessions.create(principal)
    h = _handler(minimal_config, auth_store=store, sessions=sessions,
                 headers={"Authorization": f"Bearer {token}"})

    assert h._authenticate_request()["username"] == "alice"
    store.set_password("alice", "pw2")

    assert h._authenticate_request() is None
    assert sessions.validate(token) is None


@pytest.mark.unit
def test_config_is_sanitized_for_deleted_user_session(minimal_config, tmp_path):
    # The dashboard signs out when it holds a token but /config comes back
    # "sanitized" (anonymous). Prove the server delivers that signal once the
    # account is gone, even though the read itself stays open (200).
    _enable_auth(minimal_config)
    store = AuthStore(tmp_path / "auth.db")
    store.create_user("alice", "pw")
    sessions = SessionManager(3600)
    token = sessions.create({"username": "alice", "role": "admin", "kind": "user"})
    store.delete_user("alice")
    h = _handler(minimal_config, source=MagicMock(), auth_store=store,
                 sessions=sessions, path="/api/v1/config",
                 headers={"Authorization": f"Bearer {token}"})
    status, _, payload = h._route()
    assert status == 200
    assert payload["detail"] == "sanitized"      # treated as anonymous
    assert sessions.validate(token) is None        # and the session was reaped


@pytest.mark.unit
def test_session_preserved_when_get_user_errors(minimal_config):
    # A transient auth-DB error must NOT log out an already-authenticated user.
    store = MagicMock()
    store.get_user.side_effect = RuntimeError("db locked")
    sessions = SessionManager(3600)
    token = sessions.create({"username": "alice", "role": "admin", "kind": "user"})
    h = _handler(minimal_config, auth_store=store, sessions=sessions,
                 headers={"Authorization": f"Bearer {token}"})
    assert h._authenticate_request()["username"] == "alice"   # session preserved
    assert sessions.validate(token) is not None               # token intact


@pytest.mark.unit
def test_write_recheck_fails_closed_on_db_error(minimal_config):
    # M4: a WRITE/control path must fail closed when the session user lookup
    # errors -- a just-deleted admin must not keep control through a DB blip --
    # while reads stay lenient and the token survives for when the DB recovers.
    store = MagicMock()
    store.get_user.side_effect = RuntimeError("db locked")
    sessions = SessionManager(3600)
    token = sessions.create({"username": "alice", "role": "admin", "kind": "user"})
    h = _handler(minimal_config, auth_store=store, sessions=sessions,
                 headers={"Authorization": f"Bearer {token}"})
    assert h._authenticate_request()["username"] == "alice"      # read: lenient
    assert h._authenticate_request(strict=True) is None          # write: denied
    assert sessions.validate(token) is not None                  # token intact


@pytest.mark.unit
def test_session_validity_ignores_non_user_principals(minimal_config):
    # API-key-kind principals never carry a username; they must not be re-checked
    # via get_user (and must not be invalidated by it).
    store = MagicMock()
    h = _handler(minimal_config, auth_store=store)
    assert h._session_user_status({"kind": "api_key", "id": 1}) == "ok"
    # A user-kind principal without a username is treated as valid (defensive).
    assert h._session_user_status({"kind": "user"}) == "ok"
    store.get_user.assert_not_called()


# ----- login -----

@pytest.mark.unit
def test_login_disabled_returns_404(minimal_config):
    h = _handler(minimal_config, path="/api/v1/auth/login")
    status, _, _ = h._route_post()
    assert status == 404


@pytest.mark.unit
def test_login_success_and_token_usable(minimal_config, tmp_path):
    _enable_auth(minimal_config)
    store = AuthStore(tmp_path / "auth.db")
    store.create_user("alice", "s3cret")
    sessions = SessionManager(3600)
    body = json.dumps({"username": "alice", "password": "s3cret"}).encode()
    h = _handler(minimal_config, auth_store=store, sessions=sessions,
                 path="/api/v1/auth/login", body=body)
    status, _, payload = h._route_post()
    assert status == 200
    assert payload["tokenType"] == "bearer"
    assert payload["expiresIn"] == sessions.ttl  # reports the effective TTL
    assert sessions.validate(payload["token"])["username"] == "alice"


@pytest.mark.unit
def test_login_after_first_user_created_without_restart(
        minimal_config, tmp_path, monkeypatch):
    db_path = tmp_path / "auth.db"
    minimal_config.api.auth.db_path = str(db_path)
    # Simulate a running daemon that checked auth before any user existed.
    # Scope the class-level cache mutation with monkeypatch so it is restored
    # after the test and can't leak a stale "no users" cache into later tests.
    monkeypatch.setattr(api_module.EneruAPIHandler, "_auth_active_ts", time.time())
    monkeypatch.setattr(api_module.EneruAPIHandler, "_auth_active_val", False)
    store = AuthStore(db_path)

    # The first user is created by `eneru user create` while the API keeps
    # running. Login must refresh the effective-auth probe instead of trusting
    # the stale "no users" cache.
    store.create_user("alice", "s3cret")
    sessions = SessionManager(3600)
    body = json.dumps({"username": "alice", "password": "s3cret"}).encode()
    h = _handler(minimal_config, auth_store=store, sessions=sessions,
                 path="/api/v1/auth/login", body=body)

    status, _, payload = h._route_post()

    assert status == 200
    assert sessions.validate(payload["token"])["username"] == "alice"


@pytest.mark.unit
def test_auth_state_refreshes_after_first_user_created_without_restart(
        minimal_config, tmp_path, monkeypatch):
    db_path = tmp_path / "auth.db"
    minimal_config.api.auth.db_path = str(db_path)
    monkeypatch.setattr(api_module.EneruAPIHandler, "_auth_active_ts", time.time())
    monkeypatch.setattr(api_module.EneruAPIHandler, "_auth_active_val", False)

    AuthStore(db_path).create_user("alice", "s3cret")
    h = _handler(minimal_config, path="/api/v1/auth/state")

    status, _, payload = h._route()

    assert status == 200
    assert payload["enabled"] is True


@pytest.mark.unit
def test_login_bad_credentials_401(minimal_config, tmp_path):
    _enable_auth(minimal_config)
    store = AuthStore(tmp_path / "auth.db")
    store.create_user("alice", "s3cret")
    body = json.dumps({"username": "alice", "password": "wrong"}).encode()
    h = _handler(minimal_config, auth_store=store, sessions=SessionManager(3600),
                 path="/api/v1/auth/login", body=body)
    with pytest.raises(APIUnauthorized):
        h._route_post()


@pytest.mark.unit
def test_login_missing_fields_400(minimal_config, tmp_path):
    _enable_auth(minimal_config)
    store = AuthStore(tmp_path / "auth.db")
    body = json.dumps({"username": "alice"}).encode()
    h = _handler(minimal_config, auth_store=store, sessions=SessionManager(3600),
                 path="/api/v1/auth/login", body=body)
    with pytest.raises(APIBadRequest):
        h._route_post()


@pytest.mark.unit
def test_login_backend_error_503(minimal_config, tmp_path):
    _enable_auth(minimal_config)
    store = AuthStore(tmp_path / "auth.db")
    store.authenticate = MagicMock(side_effect=RuntimeError("bcrypt missing"))
    body = json.dumps({"username": "alice", "password": "x"}).encode()
    h = _handler(minimal_config, auth_store=store, sessions=SessionManager(3600),
                 path="/api/v1/auth/login", body=body)
    status, _, payload = h._route_post()
    assert status == 503
    assert payload["error"]["code"] == "AUTH_UNAVAILABLE"


@pytest.mark.unit
def test_login_throttled_after_burst(minimal_config, tmp_path):
    """ISS-032: after LOGIN_FAIL_MAX bad logins the next attempt gets 429 before
    the auth backend is even consulted."""
    import eneru.api as api_mod
    _enable_auth(minimal_config)
    store = AuthStore(tmp_path / "auth.db")
    store.create_user("alice", "s3cret")

    def _bad():
        body = json.dumps({"username": "alice", "password": "wrong"}).encode()
        return _handler(minimal_config, auth_store=store,
                        sessions=SessionManager(3600),
                        path="/api/v1/auth/login", body=body)

    for _ in range(api_mod.LOGIN_FAIL_MAX):
        with pytest.raises(APIUnauthorized):
            _bad()._route_post()
    status, _, payload = _bad()._route_post()
    assert status == 429
    assert payload["error"]["code"] == "TOO_MANY_ATTEMPTS"


@pytest.mark.unit
def test_ready_minimal_for_anonymous_under_require_for_reads(
    minimal_config, monkeypatch,
):
    """ISS-030: an anonymous /ready probe under require_for_reads gets only the
    boolean, not the UPS/remote topology; the probe is never 401'd."""
    _enable_auth(minimal_config, require_for_reads=True)
    monkeypatch.setattr("eneru.api.readiness", lambda src: {
        "ready": True, "ups": [{"name": "UPS@secret-host"}],
        "reasons": ["some detail"],
    })
    h = _handler(minimal_config, sessions=SessionManager(3600), path="/ready")
    status, _, payload = h._route()
    assert status == 200
    assert payload == {"ready": True}


@pytest.mark.unit
def test_ready_full_for_authenticated_under_require_for_reads(
    minimal_config, monkeypatch,
):
    """ISS-030: an authenticated caller still gets the detailed readiness."""
    _enable_auth(minimal_config, require_for_reads=True)
    full = {"ready": True, "ups": [{"name": "UPS@host"}], "reasons": []}
    monkeypatch.setattr("eneru.api.readiness", lambda src: dict(full))
    sessions = SessionManager(3600)
    token = sessions.create({"username": "a", "role": "admin", "kind": "user"})
    h = _handler(minimal_config, sessions=sessions, path="/ready",
                 headers={"Authorization": f"Bearer {token}"})
    status, _, payload = h._route()
    assert status == 200
    assert "ups" in payload and payload["ups"]


@pytest.mark.unit
def test_config_reload_rejected_uses_error_envelope(minimal_config):
    """ISS-028: a rejected reload returns the standard {"error":{code,message}}
    envelope with the report under `details`, not the raw report dict."""
    _enable_auth(minimal_config)
    sessions = SessionManager(3600)
    token = sessions.create({"username": "a", "role": "admin", "kind": "user"})
    source = MagicMock()
    source.reload_config.return_value = {
        "reloaded": False, "errors": ["bad: nope"], "restartRequired": [],
    }
    h = _handler(minimal_config, source=source, sessions=sessions,
                 path="/api/v1/config/reload",
                 headers={"Authorization": f"Bearer {token}"})
    status, _, payload = h._route_post()
    assert status == 400
    assert payload["error"]["code"] == "RELOAD_REJECTED"
    assert payload["details"]["errors"] == ["bad: nope"]


@pytest.mark.unit
def test_login_throttle_memory_is_bounded(monkeypatch):
    """ISS-032: the tracked-IP table stays bounded (sweep expired, then evict
    oldest) so IP-rotation on a direct bind can't grow it without limit."""
    import eneru.api as api_mod
    monkeypatch.setattr(api_mod, "LOGIN_TRACKED_IPS_MAX", 3)
    clock = [1000.0]
    monkeypatch.setattr(api_mod.time, "monotonic", lambda: clock[0])

    for i in range(3):
        api_mod._login_record_failure(f"a{i}")
    assert len(api_mod._login_failures) == 3
    # Advance past the window so the a* entries are expired; the next record
    # triggers the expired-sweep branch.
    clock[0] = 1000.0 + api_mod.LOGIN_FAIL_WINDOW_SECONDS + 1
    api_mod._login_record_failure("b0")
    assert len(api_mod._login_failures) <= 3
    # A burst of fresh (non-expired) IPs past the cap exercises the oldest-evict
    # (popitem) path.
    for i in range(6):
        api_mod._login_record_failure(f"c{i}")
    assert len(api_mod._login_failures) <= 3


@pytest.mark.unit
def test_login_success_clears_throttle(minimal_config, tmp_path):
    """ISS-032: a successful login resets the per-IP failure counter."""
    import eneru.api as api_mod
    _enable_auth(minimal_config)
    store = AuthStore(tmp_path / "auth.db")
    store.create_user("alice", "s3cret")

    for _ in range(api_mod.LOGIN_FAIL_MAX - 1):
        body = json.dumps({"username": "alice", "password": "wrong"}).encode()
        h = _handler(minimal_config, auth_store=store,
                     sessions=SessionManager(3600),
                     path="/api/v1/auth/login", body=body)
        with pytest.raises(APIUnauthorized):
            h._route_post()

    ok_body = json.dumps({"username": "alice", "password": "s3cret"}).encode()
    ok = _handler(minimal_config, auth_store=store, sessions=SessionManager(3600),
                  path="/api/v1/auth/login", body=ok_body)
    assert ok._route_post()[0] == 200

    # Counter cleared: a fresh burst up to the cap is allowed again, not throttled.
    bad_body = json.dumps({"username": "alice", "password": "wrong"}).encode()
    h = _handler(minimal_config, auth_store=store, sessions=SessionManager(3600),
                 path="/api/v1/auth/login", body=bad_body)
    with pytest.raises(APIUnauthorized):  # 401, not 429
        h._route_post()


# ----- logout -----

@pytest.mark.unit
def test_logout_disabled_404(minimal_config):
    h = _handler(minimal_config, path="/api/v1/auth/logout")
    assert h._route_post()[0] == 404


@pytest.mark.unit
def test_logout_requires_session(minimal_config):
    _enable_auth(minimal_config)
    h = _handler(minimal_config, sessions=SessionManager(3600),
                 path="/api/v1/auth/logout")
    with pytest.raises(APIUnauthorized):
        h._route_post()


@pytest.mark.unit
def test_logout_invalidates_session(minimal_config):
    _enable_auth(minimal_config)
    sessions = SessionManager(3600)
    token = sessions.create({"username": "alice", "kind": "user"})
    h = _handler(minimal_config, sessions=sessions,
                 path="/api/v1/auth/logout",
                 headers={"Authorization": f"Bearer {token}"})
    status, _, payload = h._route_post()
    assert status == 200 and payload["status"] == "ok"
    assert sessions.validate(token) is None


@pytest.mark.unit
def test_post_unknown_route_404(minimal_config):
    h = _handler(minimal_config, path="/api/v1/nope")
    assert h._route_post()[0] == 404


# ----- body parsing -----

@pytest.mark.unit
def test_read_json_body_too_large(minimal_config):
    h = _handler(minimal_config, headers={"Content-Length": str(MAX_BODY_BYTES + 1)})
    with pytest.raises(APIPayloadTooLarge):
        h._read_json_body()


@pytest.mark.unit
def test_read_json_body_malformed_and_non_object(minimal_config):
    h = _handler(minimal_config, body=b"{not json")
    with pytest.raises(APIBadRequest):
        h._read_json_body()
    h = _handler(minimal_config, body=b"[1,2,3]")
    with pytest.raises(APIBadRequest):
        h._read_json_body()


@pytest.mark.unit
def test_read_json_body_empty_is_dict(minimal_config):
    assert _handler(minimal_config)._read_json_body() == {}


@pytest.mark.unit
def test_read_json_body_timeout_is_bad_request(minimal_config):
    class TimeoutBody:
        def read(self, _length):
            raise socket.timeout("slow client")

    h = _handler(minimal_config, headers={"Content-Length": "2"})
    h.rfile = TimeoutBody()
    with pytest.raises(APIBadRequest):
        h._read_json_body()


@pytest.mark.unit
def test_read_json_body_non_timeout_read_error_is_bad_request(minimal_config):
    class BrokenBody:
        def read(self, _length):
            raise BrokenPipeError("client went away")

    h = _handler(minimal_config, headers={"Content-Length": "2"})
    h.rfile = BrokenBody()
    with pytest.raises(APIBadRequest, match="failed to read request body"):
        h._read_json_body()


@pytest.mark.unit
def test_read_json_body_enforces_total_deadline(minimal_config, monkeypatch):
    class DripBody:
        def __init__(self):
            self.chunks = [b"{", b"}"]

        def read1(self, _length):
            return self.chunks.pop(0)

    times = iter([
        100.0,  # deadline setup
        100.0,  # first chunk is inside the deadline
        111.0,  # second chunk would arrive after the 10s total budget
    ])
    monkeypatch.setattr(api_module.time, "monotonic", lambda: next(times))

    h = _handler(minimal_config, headers={"Content-Length": "2"})
    h.rfile = DripBody()
    with pytest.raises(APIBadRequest, match="request body timed out"):
        h._read_json_body()


@pytest.mark.unit
def test_read_json_body_rejects_short_read(minimal_config):
    h = _handler(
        minimal_config,
        headers={"Content-Length": "10"},
        body=b"{}",
    )
    with pytest.raises(APIBadRequest):
        h._read_json_body()


@pytest.mark.unit
def test_read_json_body_timeout_is_restored_after_read(minimal_config):
    class FakeConnection:
        def __init__(self):
            self.timeout = None
            self.set_calls = []

        def gettimeout(self):
            return self.timeout

        def settimeout(self, timeout):
            self.timeout = timeout
            self.set_calls.append(timeout)

    h = _handler(minimal_config, body=b"{}")
    h.connection = FakeConnection()

    assert h._read_json_body() == {}
    assert h.connection.set_calls[0] == REQUEST_READ_TIMEOUT_SECONDS
    assert h.connection.set_calls[-1] is None


# ----- tiered /config + read gating via _route -----

@pytest.mark.unit
def test_config_tiered_anonymous_vs_authenticated(minimal_config, tmp_path):
    # anonymous (auth off) -> sanitized
    h = _handler(minimal_config, path="/api/v1/config")
    status, _, payload = h._route()
    assert status == 200 and payload["detail"] == "sanitized"
    assert payload["api"]["auth"]["enabled"] is False

    # authenticated -> extended
    _enable_auth(minimal_config)
    sessions = SessionManager(3600)
    token = sessions.create({"username": "alice", "role": "admin", "kind": "user"})
    h = _handler(minimal_config, sessions=sessions, path="/api/v1/config",
                 headers={"Authorization": f"Bearer {token}"})
    _, _, payload = h._route()
    assert payload["detail"] == "extended"


@pytest.mark.unit
def test_read_gating_blocks_anonymous(minimal_config):
    _enable_auth(minimal_config, require_for_reads=True)
    h = _handler(minimal_config, sessions=SessionManager(3600),
                 path="/api/v1/ups")
    with pytest.raises(APIUnauthorized):
        h._route()


@pytest.mark.unit
def test_auth_state_open_when_reads_require_auth(minimal_config):
    _enable_auth(minimal_config, require_for_reads=True)
    h = _handler(minimal_config, sessions=SessionManager(3600),
                 path="/api/v1/auth/state")
    status, _, payload = h._route()
    assert status == 200
    assert payload == {"enabled": True, "requireForReads": True}


@pytest.mark.unit
def test_auth_state_reports_effective_read_gate(minimal_config):
    minimal_config.api.auth.enabled = False
    minimal_config.api.auth.enabled_explicitly_set = True
    minimal_config.api.auth.require_for_reads = True
    h = _handler(minimal_config, path="/api/v1/auth/state")
    status, _, payload = h._route()
    assert status == 200
    assert payload == {"enabled": False, "requireForReads": False}


@pytest.mark.unit
def test_health_always_open_even_with_auth(minimal_config):
    # /health is reached before the read gate, so it stays open with auth on.
    _enable_auth(minimal_config, require_for_reads=True)
    h = _handler(minimal_config, sessions=SessionManager(3600), path="/health")
    assert h._route()[0] == 200


# ----- dispatch maps exceptions + WWW-Authenticate on 401 -----

@pytest.mark.unit
def test_do_post_401_sets_www_authenticate(minimal_config):
    _enable_auth(minimal_config)
    h = _handler(minimal_config, sessions=SessionManager(3600),
                 path="/api/v1/auth/logout")
    headers = []
    h.send_response = lambda status: headers.append(("status", status))
    h.send_header = lambda k, v: headers.append((k, v))
    h.end_headers = lambda: None
    h.wfile = BytesIO()
    h.do_POST()
    assert ("status", 401) in headers
    assert ("WWW-Authenticate", "Bearer") in headers


@pytest.mark.unit
def test_available_endpoints_hidden_until_features_enabled(minimal_config):
    # Auth + control + reload routes are advertised only when their feature is on.
    h = _handler(minimal_config)
    paths = {e["path"] for e in h._available_endpoints()}
    assert "/api/v1/auth/login" not in paths
    assert "/api/v1/config/reload" not in paths
    assert "/api/v1/ups/{name}/events" not in paths
    assert "/api/v1/ups/{name}/command" not in paths
    minimal_config.api.auth.enabled = True
    minimal_config.nut_control.enabled = True
    paths = {e["path"] for e in h._available_endpoints()}
    assert "/api/v1/auth/login" in paths
    assert "/api/v1/config/reload" in paths
    assert "/api/v1/ups/{name}/events" in paths
    assert "/api/v1/ups/{name}/command" in paths


@pytest.mark.unit
@pytest.mark.parametrize("exc,code", [
    (APIBadRequest("x"), 400),
    (APIPayloadTooLarge("x"), 413),
    (APIUnauthorized("x"), 401),
    (APIForbidden("x"), 403),
    (RuntimeError("x"), 500),
])
def test_dispatch_maps_exceptions(minimal_config, exc, code):
    h = _handler(minimal_config)
    captured = []
    h.send_response = lambda s: captured.append(s)
    h.send_header = lambda k, v: None
    h.end_headers = lambda: None
    h.wfile = BytesIO()

    def router():
        raise exc

    h._dispatch(router)
    assert captured == [code]


@pytest.mark.unit
@pytest.mark.parametrize("clen", ["abc", "-1"])
def test_read_json_body_bad_content_length(minimal_config, clen):
    h = _handler(minimal_config, headers={"Content-Length": clen})
    with pytest.raises(APIBadRequest):
        h._read_json_body()


@pytest.mark.unit
def test_server_builds_auth_and_warns_when_enabled(minimal_config, tmp_path):
    from unittest.mock import patch
    from eneru.api import EneruAPIServer

    _enable_auth(minimal_config)
    minimal_config.api.enabled = True
    minimal_config.api.bind = "0.0.0.0"
    minimal_config.api.auth.db_path = str(tmp_path / "auth.db")
    log = []
    server = EneruAPIServer(MagicMock(), minimal_config, log_fn=log.append)
    assert server._auth_store is not None
    assert server._sessions is not None
    with patch("eneru.api.ThreadingHTTPServer", return_value=MagicMock()):
        server.start()
    try:
        assert any("auth enabled" in m for m in log), log
    finally:
        server.stop()
