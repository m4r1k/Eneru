"""Unit tests for the v6.0 event-deletion endpoint (DELETE /api/v1/ups/{name}/events)."""

import json
from io import BytesIO
from unittest.mock import MagicMock

import pytest

from conftest import make_api_handler
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
    # F-063: shared EneruAPIHandler builder lives in conftest.py.
    return make_api_handler(
        config, source=source, path=path, body=body, token=token,
        logs=logs, sessions=SessionManager(3600),
    )


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
    assert payload == {"ups": "UPS@h", "deleted": 2, "protected": 0}
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
    status, _, payload = h._route_delete()
    # The store layer de-dups; the API forwards items as-is and reports the
    # store's count (F-154: pin the forwarded tuples, not just "called").
    assert status == 200 and payload["deleted"] == 1
    source.delete_events.assert_called_once_with(
        "UPS@h", [(5, 1000, "ON_BATTERY"), (5, 1000, "ON_BATTERY")])


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
    assert h._route_delete()[0] == 404


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


# ----- F-108 / F-109 / F-110: audit trail + anonymous redaction -----

@pytest.mark.unit
def test_delete_events_keeps_audit_rows(minimal_config):
    """F-108: audit rows are skipped, the rest are deleted, and the count of
    kept rows is reported and audited."""
    minimal_config.api.auth.enabled = True
    source = MagicMock()
    source.delete_events.return_value = 1
    logs = []
    items = [ITEM,
             {"id": 6, "ts": 1001, "eventType": "LOGIN_FAILURE"},
             {"id": 7, "ts": 1002, "eventType": "EVENTS_DELETED"},
             {"id": 8, "ts": 1003, "eventType": "CONTROL"}]
    h = _handler(minimal_config, source=source, body=_body(items), logs=logs)
    _authed(h)
    status, _, payload = h._route_delete()
    assert status == 200
    assert payload == {"ups": "UPS@h", "deleted": 1, "protected": 3}
    source.delete_events.assert_called_once_with("UPS@h", [(5, 1000, "ON_BATTERY")])
    assert any("1 rows, 3 audit rows kept" in line for line in logs)


@pytest.mark.unit
def test_audit_event_types_cover_every_audit_kind():
    assert apimod.AUDIT_EVENT_TYPES >= set(EneruAPIHandler._AUDIT_EVENT_TYPES.values())
    assert "CONTROL" in apimod.AUDIT_EVENT_TYPES


def _seed_events(minimal_config, tmp_path):
    from eneru.stats import StatsStore
    minimal_config.statistics.db_directory = str(tmp_path)
    store = StatsStore(tmp_path / "default.db")
    store.open()
    try:
        store.log_event("ON_BATTERY", "power", ts=100)
        store.log_event("LOGIN_FAILURE", "user bob from 10.0.0.9", ts=101)
        store.log_event("CONTROL_COMMAND", "alice ran beeper.disable", ts=102)
    finally:
        store.close()


@pytest.mark.unit
@pytest.mark.parametrize("auth_on,signed_in,expected", [
    (True, False, ["ON_BATTERY"]),                                      # anonymous: hidden
    (True, True, ["ON_BATTERY", "LOGIN_FAILURE", "CONTROL_COMMAND"]),    # signed in: all
    (False, False, ["ON_BATTERY", "LOGIN_FAILURE", "CONTROL_COMMAND"]),  # auth off: all
])
def test_events_audit_rows_hidden_from_anonymous(minimal_config, tmp_path,
                                                auth_on, signed_in, expected):
    """F-109."""
    _seed_events(minimal_config, tmp_path)
    minimal_config.api.auth.enabled = auth_on
    minimal_config.api.auth.enabled_explicitly_set = True
    h = _handler(minimal_config, path="/api/v1/events?limit=10")
    if signed_in:
        _authed(h)
    status, _, payload = h._route()
    assert status == 200
    assert sorted(e["eventType"] for e in payload["events"]) == sorted(expected)


@pytest.mark.unit
def test_events_hidden_audit_rows_do_not_shrink_the_page(minimal_config, tmp_path):
    """F-109: filtered in SQL, so limit=1 still returns the real event."""
    _seed_events(minimal_config, tmp_path)
    minimal_config.api.auth.enabled = True
    h = _handler(minimal_config, path="/api/v1/events?limit=1&verbosity=1")
    status, _, payload = h._route()
    assert [e["eventType"] for e in payload["events"]] == ["ON_BATTERY"]


_SECRET = "loopback identity mismatch: machine-id aaaa != bbbb"


def _health_source():
    manager = MagicMock()
    manager.snapshot.return_value = [
        {"server": "nas", "status": "FAILED", "last_error": _SECRET},
        {"server": "pi", "status": "HEALTHY", "last_error": ""},
    ]
    return MagicMock(_remote_health_manager=manager, _monitors=[])


@pytest.mark.unit
@pytest.mark.parametrize("auth_on,signed_in,redacted", [
    (True, False, True), (True, True, False), (False, False, False)])
def test_remote_health_last_error_redacted_for_anonymous(
        minimal_config, auth_on, signed_in, redacted):
    """F-110."""
    minimal_config.api.auth.enabled = auth_on
    minimal_config.api.auth.enabled_explicitly_set = True
    source = _health_source()
    h = _handler(minimal_config, source=source, path="/api/v1/remote-health")
    if signed_in:
        _authed(h)
    status, _, payload = h._route()
    assert status == 200
    rows = {r["server"]: r for r in payload["servers"]}
    want = apimod.REDACTED_REMOTE_ERROR if redacted else _SECRET
    assert rows["nas"]["last_error"] == want
    assert rows["pi"]["last_error"] == ""
    # The manager's own snapshot rows are never mutated.
    assert source._remote_health_manager.snapshot.return_value[0]["last_error"] == _SECRET


def _status_with_loopback_error():
    return {"ups": [{"name": "UPS@h"}],
            "runtime": {"loopbackDelegate": {"configured": True,
                                             "lastError": _SECRET}}}


@pytest.mark.unit
@pytest.mark.parametrize("signed_in", [False, True])
def test_ups_status_loopback_error_redacted_for_anonymous(
        minimal_config, monkeypatch, signed_in):
    """F-110: the loopback lastError also rides /api/v1/ups."""
    monkeypatch.setattr(apimod, "collect_status",
                        lambda source: _status_with_loopback_error())
    minimal_config.api.auth.enabled = True
    h = _handler(minimal_config, path="/api/v1/ups")
    if signed_in:
        _authed(h)
    _, _, payload = h._route()
    got = payload["runtime"]["loopbackDelegate"]["lastError"]
    assert got == (_SECRET if signed_in else apimod.REDACTED_REMOTE_ERROR)


@pytest.mark.unit
@pytest.mark.parametrize("auth_on,signed_in,redacted", [
    (True, False, True), (True, True, False), (False, False, False)])
def test_ready_loopback_error_redacted_for_anonymous(
        minimal_config, monkeypatch, auth_on, signed_in, redacted):
    """F-110: /ready (reads open) carries the same loopback lastError."""
    payload_in = dict(_status_with_loopback_error(), ready=True)
    monkeypatch.setattr(apimod, "readiness", lambda source: payload_in)
    minimal_config.api.auth.enabled = auth_on
    minimal_config.api.auth.enabled_explicitly_set = True
    h = _handler(minimal_config, path="/ready")
    if signed_in:
        _authed(h)
    status, _, payload = h._route()
    assert status == 200
    got = payload["runtime"]["loopbackDelegate"]["lastError"]
    assert got == (apimod.REDACTED_REMOTE_ERROR if redacted else _SECRET)


@pytest.mark.unit
def test_redact_loopback_error_tolerates_missing_runtime():
    payload = {"ups": []}
    apimod._redact_loopback_error(payload)
    assert payload == {"ups": []}
    empty = {"runtime": {"loopbackDelegate": {"lastError": ""}}}
    apimod._redact_loopback_error(empty)
    assert empty["runtime"]["loopbackDelegate"]["lastError"] == ""
