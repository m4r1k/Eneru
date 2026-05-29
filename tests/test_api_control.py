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
    monkeypatch.setattr(apimod.nutctl, "list_commands",
                        lambda ups, timeout=10: (True, ["beeper.toggle", "load.off"], ""))
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
    monkeypatch.setattr(apimod.nutctl, "list_commands",
                        lambda ups, timeout=10: (False, [], "driver down"))
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
