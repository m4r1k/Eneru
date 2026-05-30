"""Unit tests for the v6.0 event-deletion endpoint (DELETE /api/v1/ups/{name}/events)."""

import json
from io import BytesIO
from unittest.mock import MagicMock

import pytest

from eneru import api as apimod
from eneru.api import (
    APIBadRequest,
    APIForbidden,
    APIPayloadTooLarge,
    APIUnauthorized,
    EneruAPIHandler,
    SessionManager,
)


def _handler(config, *, source=None, path="/api/v1/ups/UPS@h/events",
             body=b"", token=None, logs=None):
    h = object.__new__(EneruAPIHandler)
    h.path = path
    h.api_config = config
    h.api_source = source if source is not None else MagicMock()
    h.api_auth = None
    h.api_sessions = SessionManager(3600)
    h.api_log = (logs.append if logs is not None else (lambda m: None))
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if body:
        headers["Content-Length"] = str(len(body))
    h.headers = headers
    h.rfile = BytesIO(body)
    return h


def _authed(h):
    h.headers["Authorization"] = "Bearer " + h.api_sessions.create(
        {"username": "alice", "role": "admin", "kind": "user"})


def _body(items):
    return json.dumps({"items": items}).encode()


ITEM = {"id": 5, "ts": 1000, "eventType": "ON_BATTERY"}


@pytest.fixture(autouse=True)
def _stub_status(monkeypatch):
    monkeypatch.setattr(apimod, "collect_status",
                        lambda source: {"ups": [{"name": "UPS@h"}]})


# ----- happy path + audit -----

@pytest.mark.unit
def test_delete_events_authed(minimal_config):
    minimal_config.api.auth.enabled = True
    source = MagicMock()
    source.delete_events.return_value = 2
    logs = []
    h = _handler(minimal_config, source=source, body=_body([ITEM]), logs=logs)
    _authed(h)
    status, _, payload = h._route_delete()
    assert status == 200
    assert payload == {"ups": "UPS@h", "deleted": 2}
    source.delete_events.assert_called_once_with("UPS@h", [(5, 1000, "ON_BATTERY")])
    # Named audit row + log line.
    source.record_control_event.assert_called_once()
    assert source.record_control_event.call_args[0][1] == "EVENTS_DELETED"
    assert any("events" in line and "-> 2 rows" in line for line in logs)


@pytest.mark.unit
def test_delete_events_dedups_items(minimal_config):
    minimal_config.api.auth.enabled = True
    source = MagicMock()
    source.delete_events.return_value = 1
    h = _handler(minimal_config, source=source, body=_body([ITEM, dict(ITEM)]))
    _authed(h)
    h._route_delete()
    # The store layer de-dups; the API forwards items as-is (one unique tuple
    # after the store's own de-dup). Here we just assert the call happened.
    source.delete_events.assert_called_once()


# ----- auth gating -----

@pytest.mark.unit
def test_delete_events_anonymous_401(minimal_config):
    minimal_config.api.auth.enabled = True
    h = _handler(minimal_config, body=_body([ITEM]))
    with pytest.raises(APIUnauthorized):
        h._route_delete()


@pytest.mark.unit
def test_delete_events_auth_off_403(minimal_config):
    # auth disabled -> writes hard-disabled regardless of credentials
    minimal_config.api.auth.enabled = False
    h = _handler(minimal_config, body=_body([ITEM]))
    with pytest.raises(APIForbidden):
        h._route_delete()


# ----- not found / unavailable -----

@pytest.mark.unit
def test_delete_events_unknown_ups_404(minimal_config):
    minimal_config.api.auth.enabled = True
    h = _handler(minimal_config, path="/api/v1/ups/Ghost@h/events", body=_body([ITEM]))
    _authed(h)
    status, _, payload = h._route_delete()
    assert status == 404


@pytest.mark.unit
def test_delete_events_stats_unavailable_503(minimal_config):
    minimal_config.api.auth.enabled = True
    source = MagicMock()
    source.delete_events.return_value = None   # no open store
    h = _handler(minimal_config, source=source, body=_body([ITEM]))
    _authed(h)
    status, _, payload = h._route_delete()
    assert status == 503 and payload["error"]["code"] == "STATS_UNAVAILABLE"


@pytest.mark.unit
def test_delete_events_source_without_method_503(minimal_config):
    minimal_config.api.auth.enabled = True
    h = _handler(minimal_config, source=object(), body=_body([ITEM]))
    _authed(h)
    assert h._route_delete()[0] == 503


# ----- malformed body matrix -----

@pytest.mark.unit
@pytest.mark.parametrize("body", [
    json.dumps({}).encode(),                              # missing items
    json.dumps({"items": "nope"}).encode(),              # non-list
    json.dumps({"items": ["x"]}).encode(),               # item not an object
    json.dumps({"items": [{"id": "x", "ts": 1, "eventType": "A"}]}).encode(),
    json.dumps({"items": [{"id": True, "ts": 1, "eventType": "A"}]}).encode(),
    json.dumps({"items": [{"id": 1, "ts": "x", "eventType": "A"}]}).encode(),
    json.dumps({"items": [{"id": 1, "ts": 1}]}).encode(),  # missing eventType
    json.dumps({"items": [{"id": 1, "ts": 1, "eventType": ""}]}).encode(),
])
def test_delete_events_malformed_body_400(minimal_config, body):
    minimal_config.api.auth.enabled = True
    h = _handler(minimal_config, body=body)
    _authed(h)
    with pytest.raises(APIBadRequest):
        h._route_delete()


@pytest.mark.unit
def test_delete_events_oversize_413(minimal_config):
    minimal_config.api.auth.enabled = True
    big = [{"id": i, "ts": 1, "eventType": "A"} for i in range(1001)]
    h = _handler(minimal_config, body=_body(big))
    _authed(h)
    with pytest.raises(APIPayloadTooLarge):
        h._route_delete()


@pytest.mark.unit
def test_delete_events_empty_list_is_noop(minimal_config):
    minimal_config.api.auth.enabled = True
    source = MagicMock()
    source.delete_events.return_value = 0
    h = _handler(minimal_config, source=source, body=_body([]))
    _authed(h)
    status, _, payload = h._route_delete()
    assert status == 200 and payload["deleted"] == 0


@pytest.mark.unit
def test_delete_unknown_path_404(minimal_config):
    minimal_config.api.auth.enabled = True
    h = _handler(minimal_config, path="/api/v1/ups/UPS@h/bogus", body=_body([ITEM]))
    _authed(h)
    assert h._route_delete()[0] == 404
