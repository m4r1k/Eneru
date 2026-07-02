"""Unit tests for the v6.0 UPS control endpoints (api.py)."""

import json
from io import BytesIO
from unittest.mock import MagicMock

import pytest

from eneru import api as apimod
from eneru.api import APIBadRequest, APIForbidden, EneruAPIHandler, SessionManager


def _control_handler(config, *, path, method_body=b"", token=None, logs=None):
    h = object.__new__(EneruAPIHandler)
    h.path = path
    h.api_config = config
    h.api_source = MagicMock()
    h.api_auth = None
    h.api_sessions = SessionManager(3600)
    h.api_log = (logs.append if logs is not None else (lambda m: None))
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if method_body:
        headers["Content-Length"] = str(len(method_body))
    h.headers = headers
    h.rfile = BytesIO(method_body)
    return h


def _enable(config):
    config.api.auth.enabled = True
    config.nut_control.enabled = True
    config.nut_control.username = "adm"
    config.nut_control.password = "pw"
    config.nut_control.allowed_commands = ["beeper.toggle"]
    config.nut_control.allowed_variables = ["input.transfer.low"]


def _token(h):
    return h.api_sessions.create({"username": "alice", "role": "admin", "kind": "user"})


@pytest.fixture(autouse=True)
def _stub_status(monkeypatch):
    # Resolve any UPS name to a single known device.
    monkeypatch.setattr(apimod, "collect_status",
                        lambda source: {"ups": [{"name": "UPS@h"}]})


# ----- feature gating -----

@pytest.mark.unit
def test_command_requires_nut_control_enabled(minimal_config):
    minimal_config.api.auth.enabled = True  # auth on, but nut_control off
    h = _control_handler(minimal_config, path="/api/v1/ups/UPS@h/command",
                         method_body=json.dumps({"command": "beeper.toggle"}).encode())
    tok = _token(h)
    h.headers["Authorization"] = f"Bearer {tok}"
    with pytest.raises(APIForbidden):
        h._route_post()


@pytest.mark.unit
def test_control_requires_auth(minimal_config):
    _enable(minimal_config)
    # No token -> write authorization fails (401) before feature checks.
    h = _control_handler(minimal_config, path="/api/v1/ups/UPS@h/commands")
    from eneru.api import APIUnauthorized
    with pytest.raises(APIUnauthorized):
        h._route()


# ----- list commands / variables -----

@pytest.mark.unit
def test_list_commands_intersects_allowlist(minimal_config, monkeypatch):
    _enable(minimal_config)
    monkeypatch.setattr(
        apimod.nutctl, "list_commands",
        lambda ups, username="", password="", timeout=10:
            (True, ["beeper.toggle", "load.off"], ""))
    h = _control_handler(minimal_config, path="/api/v1/ups/UPS@h/commands")
    status, _, payload = (lambda hh: (hh.headers.__setitem__(
        "Authorization", f"Bearer {_token(hh)}"), hh._route())[1])(h)
    assert status == 200
    assert payload["commands"] == ["beeper.toggle"]      # allowlisted only
    assert "load.off" in payload["supported"]            # full set still reported


@pytest.mark.unit
def test_list_commands_unknown_ups_404(minimal_config, monkeypatch):
    _enable(minimal_config)
    monkeypatch.setattr(apimod, "collect_status", lambda s: {"ups": []})
    h = _control_handler(minimal_config, path="/api/v1/ups/ghost/commands")
    h.headers["Authorization"] = f"Bearer {_token(h)}"
    assert h._route()[0] == 404


@pytest.mark.unit
def test_list_variables_filtered(minimal_config, monkeypatch):
    _enable(minimal_config)
    monkeypatch.setattr(apimod.nutctl, "list_variables", lambda ups, timeout=10: (
        True,
        [{"name": "input.transfer.low", "type": "STRING", "value": "196"},
         {"name": "ups.delay.shutdown", "type": "STRING", "value": "20"}],
        ""))
    h = _control_handler(minimal_config, path="/api/v1/ups/UPS@h/variables")
    h.headers["Authorization"] = f"Bearer {_token(h)}"
    _, _, payload = h._route()
    assert [v["name"] for v in payload["variables"]] == ["input.transfer.low"]


@pytest.mark.unit
def test_list_commands_nut_error_502(minimal_config, monkeypatch):
    _enable(minimal_config)
    monkeypatch.setattr(
        apimod.nutctl, "list_commands",
        lambda ups, username="", password="", timeout=10: (False, [], "driver down"))
    h = _control_handler(minimal_config, path="/api/v1/ups/UPS@h/commands")
    h.headers["Authorization"] = f"Bearer {_token(h)}"
    assert h._route()[0] == 502


# ----- run command -----

@pytest.mark.unit
def test_run_command_allowed(minimal_config, monkeypatch):
    _enable(minimal_config)
    called = {}

    def fake_run(*a, **k):
        called["a"] = a
        return True, "done", ""

    monkeypatch.setattr(apimod.nutctl, "run_instant_command", fake_run)
    logs = []
    body = json.dumps({"command": "beeper.toggle"}).encode()
    h = _control_handler(minimal_config, path="/api/v1/ups/UPS@h/command",
                         method_body=body, logs=logs)
    h.headers["Authorization"] = f"Bearer {_token(h)}"
    status, _, payload = h._route_post()
    assert status == 200 and payload["status"] == "ok"
    assert called["a"][:2] == ("UPS@h", "beeper.toggle")
    assert any("-> ok" in line for line in logs)         # audited


@pytest.mark.unit
def test_run_command_denied_not_in_allowlist(minimal_config, monkeypatch):
    _enable(minimal_config)
    ran = monkeypatch.setattr(apimod.nutctl, "run_instant_command",
                              lambda *a, **k: pytest.fail("must not run disallowed cmd"))
    logs = []
    body = json.dumps({"command": "load.off"}).encode()
    h = _control_handler(minimal_config, path="/api/v1/ups/UPS@h/command",
                         method_body=body, logs=logs)
    h.headers["Authorization"] = f"Bearer {_token(h)}"
    with pytest.raises(APIForbidden):
        h._route_post()
    assert any("denied" in line for line in logs)


@pytest.mark.unit
def test_run_command_missing_field_400(minimal_config):
    _enable(minimal_config)
    h = _control_handler(minimal_config, path="/api/v1/ups/UPS@h/command",
                         method_body=b"{}")
    h.headers["Authorization"] = f"Bearer {_token(h)}"
    with pytest.raises(APIBadRequest):
        h._route_post()


@pytest.mark.unit
def test_run_command_nut_error_502(minimal_config, monkeypatch):
    _enable(minimal_config)
    monkeypatch.setattr(apimod.nutctl, "run_instant_command",
                        lambda *a, **k: (False, "", "no connection"))
    body = json.dumps({"command": "beeper.toggle"}).encode()
    h = _control_handler(minimal_config, path="/api/v1/ups/UPS@h/command",
                         method_body=body)
    h.headers["Authorization"] = f"Bearer {_token(h)}"
    assert h._route_post()[0] == 502


# ----- set variable (PUT) -----

@pytest.mark.unit
def test_set_variable_allowed(minimal_config, monkeypatch):
    _enable(minimal_config)
    captured = {}

    def fake_set(*a, **k):
        captured["a"] = a
        return True, "", ""

    monkeypatch.setattr(apimod.nutctl, "set_variable", fake_set)
    body = json.dumps({"value": "200"}).encode()
    h = _control_handler(
        minimal_config,
        path="/api/v1/ups/UPS@h/variables/input.transfer.low",
        method_body=body)
    h.headers["Authorization"] = f"Bearer {_token(h)}"
    status, _, payload = h._route_put()
    assert status == 200 and payload["value"] == "200"
    assert captured["a"][:3] == ("UPS@h", "input.transfer.low", "200")


@pytest.mark.unit
def test_set_variable_denied(minimal_config):
    _enable(minimal_config)
    body = json.dumps({"value": "x"}).encode()
    h = _control_handler(
        minimal_config,
        path="/api/v1/ups/UPS@h/variables/ups.delay.shutdown",
        method_body=body)
    h.headers["Authorization"] = f"Bearer {_token(h)}"
    with pytest.raises(APIForbidden):
        h._route_put()


@pytest.mark.unit
@pytest.mark.parametrize("bad", ["196; rm -rf /", "a\nb", "$(whoami)", "x" * 65])
def test_set_variable_rejects_unsafe_value(minimal_config, bad):
    _enable(minimal_config)
    body = json.dumps({"value": bad}).encode()
    h = _control_handler(
        minimal_config,
        path="/api/v1/ups/UPS@h/variables/input.transfer.low",
        method_body=body)
    h.headers["Authorization"] = f"Bearer {_token(h)}"
    with pytest.raises(APIBadRequest):
        h._route_put()


@pytest.mark.unit
def test_set_variable_missing_value_400(minimal_config):
    _enable(minimal_config)
    h = _control_handler(
        minimal_config,
        path="/api/v1/ups/UPS@h/variables/input.transfer.low",
        method_body=b"{}")
    h.headers["Authorization"] = f"Bearer {_token(h)}"
    with pytest.raises(APIBadRequest):
        h._route_put()


@pytest.mark.unit
def test_put_unknown_route_404(minimal_config):
    _enable(minimal_config)
    h = _control_handler(minimal_config, path="/api/v1/nope")
    h.headers["Authorization"] = f"Bearer {_token(h)}"
    assert h._route_put()[0] == 404


# ----- unknown-UPS + NUT-error branches on writes/listings -----

@pytest.mark.unit
def test_list_variables_unknown_ups_404(minimal_config, monkeypatch):
    _enable(minimal_config)
    monkeypatch.setattr(apimod, "collect_status", lambda s: {"ups": []})
    h = _control_handler(minimal_config, path="/api/v1/ups/ghost/variables")
    h.headers["Authorization"] = f"Bearer {_token(h)}"
    assert h._route()[0] == 404


@pytest.mark.unit
def test_list_variables_nut_error_502(minimal_config, monkeypatch):
    _enable(minimal_config)
    monkeypatch.setattr(apimod.nutctl, "list_variables",
                        lambda ups, timeout=10: (False, [], "down"))
    h = _control_handler(minimal_config, path="/api/v1/ups/UPS@h/variables")
    h.headers["Authorization"] = f"Bearer {_token(h)}"
    assert h._route()[0] == 502


@pytest.mark.unit
def test_run_command_unknown_ups_404(minimal_config, monkeypatch):
    _enable(minimal_config)
    monkeypatch.setattr(apimod, "collect_status", lambda s: {"ups": []})
    monkeypatch.setattr(apimod.nutctl, "run_instant_command",
                        lambda *a, **k: (True, "", ""))
    body = json.dumps({"command": "beeper.toggle"}).encode()
    h = _control_handler(minimal_config, path="/api/v1/ups/ghost/command",
                         method_body=body)
    h.headers["Authorization"] = f"Bearer {_token(h)}"
    assert h._route_post()[0] == 404


@pytest.mark.unit
def test_set_variable_unknown_ups_404_and_nut_error_502(minimal_config, monkeypatch):
    _enable(minimal_config)
    monkeypatch.setattr(apimod, "collect_status", lambda s: {"ups": []})
    body = json.dumps({"value": "200"}).encode()
    h = _control_handler(
        minimal_config,
        path="/api/v1/ups/ghost/variables/input.transfer.low",
        method_body=body)
    h.headers["Authorization"] = f"Bearer {_token(h)}"
    assert h._route_put()[0] == 404

    # NUT error path (UPS known, set fails)
    monkeypatch.setattr(apimod, "collect_status", lambda s: {"ups": [{"name": "UPS@h"}]})
    monkeypatch.setattr(apimod.nutctl, "set_variable",
                        lambda *a, **k: (False, "", "rejected"))
    h = _control_handler(
        minimal_config,
        path="/api/v1/ups/UPS@h/variables/input.transfer.low",
        method_body=json.dumps({"value": "200"}).encode())
    h.headers["Authorization"] = f"Bearer {_token(h)}"
    assert h._route_put()[0] == 502


# ----- self-test endpoint (v6.1) -----

def _src_with_store(name="UPS@h", store=None):
    from types import SimpleNamespace
    mon = SimpleNamespace(
        config=SimpleNamespace(ups=SimpleNamespace(name=name)),
        _stats_store=store)
    return SimpleNamespace(_monitors=[mon])


def _open_store_stub():
    """A stats-store stand-in the self-test POST guard treats as available:
    it exposes a non-None ``_conn`` (the API refuses to issue when the store is
    None or its ``_conn`` is None — i.e. unopened/closed)."""
    from types import SimpleNamespace
    return SimpleNamespace(_conn=object(), is_open=True)


@pytest.mark.unit
def test_store_for_ups_resolves_monitor(minimal_config):
    # Regression: _run_self_test referenced a nonexistent _store_for_ups -> 500.
    h = _control_handler(minimal_config, path="/x")
    h.api_source = _src_with_store("UPS@h", store="STORE")
    assert h._store_for_ups("UPS@h") == "STORE"
    assert h._store_for_ups("nope") is None


@pytest.mark.unit
def test_run_self_test_issues(minimal_config, monkeypatch):
    _enable(minimal_config)
    minimal_config.nut_control.allowed_commands = ["test.battery.start"]
    monkeypatch.setattr(apimod.selftest, "list_supported_commands",
                        lambda *a, **k: ["test.battery.start"])
    monkeypatch.setattr(apimod.selftest, "issue_self_test",
                        lambda *a, **k: {"ok": True, "test_id": 5, "error": ""})
    logs = []
    h = _control_handler(minimal_config, path="/api/v1/ups/UPS@h/self-test",
                         method_body=b"{}", logs=logs)
    h.api_source = _src_with_store("UPS@h", store=_open_store_stub())
    h.headers["Authorization"] = f"Bearer {_token(h)}"
    status, _, payload = h._route_post()
    assert status == 200 and payload["status"] == "issued" and payload["testId"] == 5
    assert any("-> ok" in line for line in logs)         # audited


@pytest.mark.unit
@pytest.mark.parametrize("store", [None, "closed"])
def test_run_self_test_503_without_open_store(minimal_config, monkeypatch, store):
    # A self-test must record a `running` row; without an open stats store the
    # API refuses (503) rather than orphaning the test state. Covers both the
    # missing store (None) and the present-but-closed store (_conn is None).
    _enable(minimal_config)
    minimal_config.nut_control.allowed_commands = ["test.battery.start"]
    monkeypatch.setattr(apimod.selftest, "list_supported_commands",
                        lambda *a, **k: ["test.battery.start"])
    monkeypatch.setattr(apimod.selftest, "issue_self_test",
                        lambda *a, **k: pytest.fail("must not issue without a store"))
    if store == "closed":
        from types import SimpleNamespace
        store = SimpleNamespace(_conn=None, is_open=False)   # opened then closed
    logs = []
    h = _control_handler(minimal_config, path="/api/v1/ups/UPS@h/self-test",
                         method_body=b"{}", logs=logs)
    h.api_source = _src_with_store("UPS@h", store=store)
    h.headers["Authorization"] = f"Bearer {_token(h)}"
    status, _, payload = h._route_post()
    assert status == 503 and payload["error"]["code"] == "STATS_UNAVAILABLE"
    assert any("-> failed" in line for line in logs)        # audited


@pytest.mark.unit
def test_run_self_test_unsupported_422(minimal_config, monkeypatch):
    _enable(minimal_config)
    minimal_config.nut_control.allowed_commands = ["test.battery.start"]
    # The UPS exposes quick/deep but NOT the configured bare command (APC case):
    # the 422 must name the startable tests so the operator can pick one.
    monkeypatch.setattr(apimod.selftest, "list_supported_commands",
                        lambda *a, **k: ["test.battery.start.quick",
                                         "test.battery.stop", "beeper.toggle"])
    monkeypatch.setattr(apimod.selftest, "issue_self_test",
                        lambda *a, **k: pytest.fail("must not issue an unsupported cmd"))
    h = _control_handler(minimal_config, path="/api/v1/ups/UPS@h/self-test",
                         method_body=b"{}")
    h.api_source = _src_with_store("UPS@h")
    h.headers["Authorization"] = f"Bearer {_token(h)}"
    status, _, payload = h._route_post()
    assert status == 422 and payload["error"]["code"] == "UNSUPPORTED"
    msg = payload["error"]["message"]
    assert "test.battery.start.quick" in msg      # candidate surfaced
    assert "test.battery.stop" not in msg         # stop is not a startable test


@pytest.mark.unit
def test_run_self_test_denied_not_allowlisted(minimal_config, monkeypatch):
    _enable(minimal_config)
    minimal_config.nut_control.allowed_commands = ["beeper.toggle"]
    minimal_config.self_test.command = "test.battery.start"   # not allowlisted
    monkeypatch.setattr(apimod.selftest, "issue_self_test",
                        lambda *a, **k: pytest.fail("must not issue denied cmd"))
    logs = []
    h = _control_handler(minimal_config, path="/api/v1/ups/UPS@h/self-test",
                         method_body=b"{}", logs=logs)
    h.api_source = _src_with_store("UPS@h")
    h.headers["Authorization"] = f"Bearer {_token(h)}"
    with pytest.raises(APIForbidden):
        h._route_post()
    assert any("denied" in line for line in logs)


@pytest.mark.unit
def test_run_self_test_permitted_by_self_test_flag_without_nut_control(
        minimal_config, monkeypatch):
    # v6.1.2: self_test enabled is its own permission — the API issues the
    # configured command even with nut_control OFF and no allowlist, and hands
    # issue_self_test an effective nut_control with the command auto-allowlisted.
    minimal_config.api.auth.enabled = True
    minimal_config.self_test.enabled = True
    minimal_config.self_test.command = "test.battery.start"
    minimal_config.nut_control.enabled = False
    minimal_config.nut_control.allowed_commands = []
    captured = {}
    monkeypatch.setattr(apimod.selftest, "list_supported_commands",
                        lambda *a, **k: ["test.battery.start"])

    def _issue(ups, cmd, nc, store, source="api"):
        captured["allowed"] = list(nc.allowed_commands)
        captured["source"] = source
        return {"ok": True, "test_id": 9, "error": ""}
    monkeypatch.setattr(apimod.selftest, "issue_self_test", _issue)
    h = _control_handler(minimal_config, path="/api/v1/ups/UPS@h/self-test",
                         method_body=b"{}")
    h.api_source = _src_with_store("UPS@h", store=_open_store_stub())
    h.headers["Authorization"] = f"Bearer {_token(h)}"
    status, _, payload = h._route_post()
    assert status == 200 and payload["testId"] == 9
    assert "test.battery.start" in captured["allowed"]   # auto-allowed
    assert captured["source"] == "api"


@pytest.mark.unit
def test_general_command_still_forbidden_when_only_self_test_enabled(
        minimal_config, monkeypatch):
    # Adversarial (v6.1.2): the self_test softening must NOT widen the general
    # control surface. With self_test ON but nut_control OFF, POST /command is
    # still 403 — even for the very command self_test would auto-permit.
    minimal_config.api.auth.enabled = True
    minimal_config.self_test.enabled = True
    minimal_config.self_test.command = "test.battery.start"
    minimal_config.nut_control.enabled = False
    monkeypatch.setattr(apimod.nutctl, "run_instant_command",
                        lambda *a, **k: pytest.fail("must not reach NUT"))
    h = _control_handler(minimal_config, path="/api/v1/ups/UPS@h/command",
                         method_body=b'{"command":"test.battery.start"}')
    h.api_source = _src_with_store("UPS@h")
    h.headers["Authorization"] = f"Bearer {_token(h)}"
    with pytest.raises(APIForbidden):
        h._route_post()


@pytest.mark.unit
def test_run_self_test_empty_command_is_400(minimal_config):
    minimal_config.api.auth.enabled = True
    minimal_config.self_test.enabled = True
    minimal_config.self_test.command = ""      # misconfigured
    h = _control_handler(minimal_config, path="/api/v1/ups/UPS@h/self-test",
                         method_body=b"{}")
    h.api_source = _src_with_store("UPS@h")
    h.headers["Authorization"] = f"Bearer {_token(h)}"
    with pytest.raises(APIBadRequest):
        h._route_post()


@pytest.mark.unit
def test_run_self_test_denied_when_neither_permission(minimal_config, monkeypatch):
    # self_test disabled AND nut_control disabled -> forbidden.
    minimal_config.api.auth.enabled = True
    minimal_config.self_test.enabled = False
    minimal_config.self_test.command = "test.battery.start"
    minimal_config.nut_control.enabled = False
    monkeypatch.setattr(apimod.selftest, "issue_self_test",
                        lambda *a, **k: pytest.fail("must not issue"))
    logs = []
    h = _control_handler(minimal_config, path="/api/v1/ups/UPS@h/self-test",
                         method_body=b"{}", logs=logs)
    h.api_source = _src_with_store("UPS@h")
    h.headers["Authorization"] = f"Bearer {_token(h)}"
    with pytest.raises(APIForbidden):
        h._route_post()
    assert any("denied" in line for line in logs)


# ----- audit helper -----

@pytest.mark.unit
def test_principal_label_variants():
    assert EneruAPIHandler._principal_label(None) == "anonymous"
    assert EneruAPIHandler._principal_label(
        {"kind": "api_key", "label": "ci"}) == "apikey:ci"
    assert EneruAPIHandler._principal_label(
        {"kind": "user", "username": "bob"}) == "bob"


@pytest.mark.unit
def test_audit_noop_without_log_and_swallows_errors(minimal_config):
    h = object.__new__(EneruAPIHandler)
    h.api_log = None
    h._audit({"username": "x"}, "command", "t", "ok")  # no-op, must not raise

    def boom(_msg):
        raise RuntimeError("log sink down")

    h.api_log = boom
    h._audit({"username": "x"}, "command", "t", "ok")  # swallowed


@pytest.mark.unit
def test_audit_logs_when_event_record_fails(minimal_config):
    h = object.__new__(EneruAPIHandler)
    logs = []
    h.api_log = logs.append
    h.api_source = MagicMock()
    h.api_source.record_control_event.side_effect = OSError("db locked")

    h._audit({"username": "x"}, "command", "UPS@h:test", "ok")

    assert any("control audit event failed" in msg for msg in logs)


# ----- config reload endpoint -----

@pytest.mark.unit
def test_config_reload_success(minimal_config):
    minimal_config.api.auth.enabled = True
    source = MagicMock()
    source.reload_config.return_value = {
        "reloaded": True, "applied": ["triggers:U@h"], "restartRequired": [],
        "errors": []}
    h = object.__new__(EneruAPIHandler)
    h.api_config = minimal_config
    h.api_source = source
    h.api_sessions = SessionManager(3600)
    h.api_auth = None
    h.api_log = lambda m: None
    token = h.api_sessions.create({"username": "a", "kind": "user"})
    h.headers = {"Authorization": f"Bearer {token}"}
    h.path = "/api/v1/config/reload"
    h.rfile = BytesIO(b"")
    status, _, payload = h._route_post()
    assert status == 200 and payload["reloaded"] is True
    source.reload_config.assert_called_once()


@pytest.mark.unit
def test_config_reload_invalid_returns_400(minimal_config):
    minimal_config.api.auth.enabled = True
    source = MagicMock()
    source.reload_config.return_value = {
        "reloaded": False, "applied": [], "restartRequired": [],
        "errors": ["bad config"]}
    h = object.__new__(EneruAPIHandler)
    h.api_config = minimal_config
    h.api_source = source
    h.api_sessions = SessionManager(3600)
    h.api_auth = None
    h.api_log = lambda m: None
    token = h.api_sessions.create({"username": "a", "kind": "user"})
    h.headers = {"Authorization": f"Bearer {token}"}
    h.path = "/api/v1/config/reload"
    h.rfile = BytesIO(b"")
    assert h._route_post()[0] == 400


@pytest.mark.unit
def test_config_reload_requires_auth(minimal_config):
    from eneru.api import APIUnauthorized
    minimal_config.api.auth.enabled = True
    h = object.__new__(EneruAPIHandler)
    h.api_config = minimal_config
    h.api_source = MagicMock()
    h.api_sessions = SessionManager(3600)
    h.api_auth = None
    h.api_log = lambda m: None
    h.headers = {}
    h.path = "/api/v1/config/reload"
    h.rfile = BytesIO(b"")
    with pytest.raises(APIUnauthorized):
        h._route_post()


@pytest.mark.unit
def test_config_reload_unsupported_source_503(minimal_config):
    minimal_config.api.auth.enabled = True
    source = object()  # no reload_config attribute
    h = object.__new__(EneruAPIHandler)
    h.api_config = minimal_config
    h.api_source = source
    h.api_sessions = SessionManager(3600)
    h.api_auth = None
    h.api_log = lambda m: None
    token = h.api_sessions.create({"username": "a", "kind": "user"})
    h.headers = {"Authorization": f"Bearer {token}"}
    h.path = "/api/v1/config/reload"
    h.rfile = BytesIO(b"")
    assert h._route_post()[0] == 503
