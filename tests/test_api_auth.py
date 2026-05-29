"""Unit tests for the v6.0 API auth middleware + write-path (api.py)."""

import json
import time
from io import BytesIO
from unittest.mock import MagicMock

import pytest

from eneru.api import (
    APIBadRequest,
    APIForbidden,
    APIPayloadTooLarge,
    APIUnauthorized,
    EneruAPIHandler,
    MAX_BODY_BYTES,
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
    assert "/api/v1/ups/{name}/command" not in paths
    minimal_config.api.auth.enabled = True
    minimal_config.nut_control.enabled = True
    paths = {e["path"] for e in h._available_endpoints()}
    assert "/api/v1/auth/login" in paths
    assert "/api/v1/config/reload" in paths
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
