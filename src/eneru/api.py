"""Embedded HTTP API, Prometheus endpoint, and (v6.0) authenticated write-path."""

import json
import math
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import parse_qs, unquote, urlparse

from eneru import nut_control as nutctl
from eneru.auth import AuthStore
from eneru.status import (
    HISTORY_METRICS,
    collect_status,
    config_summary,
    find_status,
    live_remote_health,
    query_events,
    query_history,
    readiness,
)

# Cap request bodies so a hostile/accidental huge POST can't exhaust memory in a
# handler thread. Auth + control payloads are tiny JSON objects.
MAX_BODY_BYTES = 64 * 1024

# Serialize control writes per UPS so two concurrent requests can't race an
# INSTCMD/SET against the same device. Keyed by the real NUT name.
_ups_command_locks: Dict[str, threading.Lock] = {}
_ups_locks_guard = threading.Lock()


def _ups_lock(name: str) -> threading.Lock:
    with _ups_locks_guard:
        lock = _ups_command_locks.get(name)
        if lock is None:
            lock = threading.Lock()
            _ups_command_locks[name] = lock
        return lock


class APIBadRequest(ValueError):
    """Raised when a client supplies invalid API query parameters."""


class APIPayloadTooLarge(ValueError):
    """Raised when a request body exceeds ``MAX_BODY_BYTES`` (413)."""


class APIUnauthorized(Exception):
    """Raised when a request needs a credential it didn't supply (401)."""


class APIForbidden(Exception):
    """Raised when an action is not permitted in the current mode (403)."""


class SessionManager:
    """In-memory bearer-token sessions with a fixed TTL.

    Sessions are deliberately ephemeral — they live only in the daemon process
    and are lost on restart. That's fine: a restart just means re-login, and
    keeping them out of the DB avoids persisting anything credential-like.
    Thread-safe because requests run on ThreadingHTTPServer worker threads.
    """

    def __init__(self, ttl_seconds: int) -> None:
        self._ttl = max(1, int(ttl_seconds))
        self._sessions: Dict[str, Tuple[Dict[str, Any], float]] = {}
        self._lock = threading.Lock()

    def create(self, principal: Dict[str, Any]) -> str:
        token = secrets.token_urlsafe(32)
        expiry = time.time() + self._ttl
        with self._lock:
            self._sessions[token] = (principal, expiry)
        return token

    def validate(self, token: str) -> Optional[Dict[str, Any]]:
        now = time.time()
        with self._lock:
            entry = self._sessions.get(token)
            if entry is None:
                return None
            principal, expiry = entry
            if expiry < now:
                del self._sessions[token]
                return None
            return principal

    def invalidate(self, token: str) -> bool:
        with self._lock:
            return self._sessions.pop(token, None) is not None


API_ENDPOINTS = (
    {"path": "/health", "description": "Liveness probe"},
    {"path": "/ready", "description": "Readiness probe with fresh UPS data"},
    {"path": "/metrics", "description": "Prometheus metrics when enabled"},
    {"path": "/api/v1", "description": "API endpoint index"},
    {"path": "/api/v1/ups", "description": "Current UPS and redundancy status"},
    {"path": "/api/v1/ups/{name}", "description": "Current status for one UPS"},
    {
        "path": "/api/v1/ups/{name}/history",
        "description": "SQLite metric history for one UPS",
        "query": {
            "metric": "charge|runtime|load|voltage|depletion",
            "from": "unix timestamp",
            "to": "unix timestamp",
        },
    },
    {
        "path": "/api/v1/events",
        "description": "Recent SQLite event rows",
        "query": {"limit": "1..10000", "verbosity": "0..2"},
    },
    {"path": "/api/v1/config", "description": "Configuration summary (extended when authenticated)"},
    {"path": "/api/v1/remote-health", "description": "Remote SSH health rows"},
    {"path": "/api/v1/auth/login", "description": "POST username/password for a session token (when auth enabled)"},
    {"path": "/api/v1/auth/logout", "description": "POST to invalidate the current session token"},
    {"path": "/api/v1/ups/{name}/commands", "description": "Allowlisted UPS commands (when nut_control enabled)"},
    {"path": "/api/v1/ups/{name}/command", "description": "POST {command} to run an allowlisted upscmd"},
    {"path": "/api/v1/ups/{name}/variables", "description": "Allowlisted writable UPS variables (upsrw)"},
    {"path": "/api/v1/ups/{name}/variables/{var}", "description": "PUT {value} to set an allowlisted upsrw variable"},
    {"path": "/api/v1/config/reload", "description": "POST to re-read config and apply the safe subset live"},
)


class EneruAPIServer:
    """Small stdlib HTTP server for read-only observability endpoints."""

    def __init__(
        self,
        source: Any,
        config: Any,
        log_fn: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.source: Any = source
        self.config: Any = config
        self.log_fn: Callable[[str], None] = log_fn or (lambda msg: None)
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        # Auth machinery is built only when api.auth is enabled; otherwise the
        # handler leaves write routes hard-disabled (v5.3 read-only behavior).
        self._auth_store: Optional[AuthStore] = None
        self._sessions: Optional[SessionManager] = None
        auth_cfg = getattr(config.api, "auth", None)
        if auth_cfg is not None and auth_cfg.enabled:
            self._auth_store = AuthStore(auth_cfg.db_path)
            self._sessions = SessionManager(auth_cfg.session_ttl)

    def start(self) -> None:
        """Start the API server when enabled."""
        if not self.config.api.enabled or self._thread is not None:
            return

        source = self.source
        config = self.config
        auth_store = self._auth_store
        sessions = self._sessions
        log_fn = self.log_fn

        class Handler(EneruAPIHandler):
            api_source = source
            api_config = config
            api_auth = auth_store
            api_sessions = sessions
            api_log = staticmethod(log_fn)

        addr = (self.config.api.bind, int(self.config.api.port))
        try:
            self._httpd = ThreadingHTTPServer(addr, Handler)
        except OSError as exc:
            self.log_fn(f"⚠️ API server failed to bind {addr[0]}:{addr[1]}: {exc}")
            return
        # Mark per-request worker threads as daemon. ThreadingHTTPServer's
        # ``server_close()`` only stops the accept loop; in-flight worker
        # threads keep running. With daemon_threads=True a hung handler
        # cannot keep the daemon process alive past shutdown — this is
        # acceptable here because every endpoint is read-only and idempotent.
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            name="eneru-api",
            daemon=True,
        )
        self._thread.start()
        self.log_fn(f"📊 API server listening on {addr[0]}:{addr[1]}")
        # Warn when bound off-loopback without auth: any caller that can reach
        # the socket can read /api/v1/config. With api.auth enabled, writes are
        # gated, so the warning softens to a reminder about open read endpoints.
        if not _is_loopback_bind(self.config.api.bind):
            auth_cfg = getattr(self.config.api, "auth", None)
            if auth_cfg and auth_cfg.enabled:
                self.log_fn(
                    f"ℹ️ API bound to {addr[0]} with auth enabled. Read endpoints "
                    "stay open unless api.auth.require_for_reads is set."
                )
            else:
                self.log_fn(
                    f"⚠️ API bound to {addr[0]} with no authentication; enable "
                    "api.auth or restrict network access before exposing beyond "
                    "trusted hosts."
                )

    def stop(self) -> None:
        """Stop the API server."""
        if self._httpd is None:
            return
        try:
            self._httpd.shutdown()
            self._httpd.server_close()
        except Exception:
            pass
        self._httpd = None
        self._thread = None


class EneruAPIHandler(BaseHTTPRequestHandler):
    """Request handler for the Eneru API (reads + v6.0 authenticated writes)."""

    api_source: Any = None
    api_config: Any = None
    # Set by EneruAPIServer.start() only when api.auth is enabled.
    api_auth: Any = None
    api_sessions: Any = None
    api_log: Any = None

    server_version = "EneruAPI/1.0"

    def do_GET(self):  # noqa: N802 - stdlib hook
        self._dispatch(self._route)

    def do_POST(self):  # noqa: N802 - stdlib hook
        self._dispatch(self._route_post)

    def do_PUT(self):  # noqa: N802 - stdlib hook
        self._dispatch(self._route_put)

    def log_message(self, fmt, *args):  # noqa: A003 - stdlib hook
        return

    def _dispatch(self, router: Callable[[], Tuple[int, str, Any]]) -> None:
        """Run a router, map exceptions to responses, and write the result."""
        try:
            status, content_type, body = router()
        except APIBadRequest as exc:
            status, content_type, body = (
                400, "application/json", self._error("INVALID_REQUEST", str(exc)))
        except APIPayloadTooLarge as exc:
            status, content_type, body = (
                413, "application/json", self._error("PAYLOAD_TOO_LARGE", str(exc)))
        except APIUnauthorized as exc:
            status, content_type, body = (
                401, "application/json", self._error("UNAUTHORIZED", str(exc)))
        except APIForbidden as exc:
            status, content_type, body = (
                403, "application/json", self._error("FORBIDDEN", str(exc)))
        except Exception:
            status, content_type, body = (
                500, "application/json",
                {"error": {"code": "INTERNAL_ERROR", "message": "Internal server error"}})
        self._finish(status, content_type, body)

    def _finish(self, status: int, content_type: str, body: Any) -> None:
        if content_type == "application/json":
            raw = json.dumps(body, sort_keys=True).encode("utf-8")
        else:
            raw = str(body).encode("utf-8")
        self.send_response(status)
        # Keep the header value on a literal whitelist. Routes only return
        # these two content types, but this avoids header-injection false
        # positives if a future route accidentally passes user data through.
        if content_type == "text/plain":
            content_type_header = "text/plain; charset=utf-8"
        else:
            content_type_header = "application/json; charset=utf-8"
        self.send_header("Content-Type", content_type_header)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        if status == 401:
            self.send_header("WWW-Authenticate", "Bearer")
        self.end_headers()
        self.wfile.write(raw)

    # ----- auth helpers (v6.0) -----

    def _bearer_token(self) -> Optional[str]:
        """Return the credential from Authorization: Bearer or X-API-Key."""
        header = self.headers.get("Authorization", "") or ""
        if header.startswith("Bearer "):
            return header[len("Bearer "):].strip() or None
        api_key = self.headers.get("X-API-Key")
        return api_key.strip() if api_key else None

    def _authenticate_request(self) -> Optional[Dict[str, Any]]:
        """Resolve the caller from a session token, then an API key. None if neither."""
        token = self._bearer_token()
        if not token:
            return None
        if self.api_sessions is not None:
            principal = self.api_sessions.validate(token)
            if principal is not None:
                return principal
        if self.api_auth is not None:
            try:
                principal = self.api_auth.authenticate_api_key(token)
            except Exception:
                principal = None
            if principal is not None:
                return principal
        return None

    def _authorize(self, *, write: bool) -> Optional[Dict[str, Any]]:
        """Enforce the tiered auth policy. Returns the principal (may be None).

        - auth disabled: reads are open; **writes are hard-disabled (403)** —
          "auth off" never means "control open".
        - auth enabled: writes require a credential (401 otherwise); reads are
          open unless ``require_for_reads``, then they require one too.
        """
        auth_cfg = getattr(self.api_config.api, "auth", None)
        enabled = bool(auth_cfg and auth_cfg.enabled)
        if not enabled:
            if write:
                raise APIForbidden(
                    "write operations require api.auth.enabled")
            return None
        principal = self._authenticate_request()
        if write:
            if principal is None:
                raise APIUnauthorized("authentication required")
            return principal
        if auth_cfg.require_for_reads and principal is None:
            raise APIUnauthorized("authentication required")
        return principal

    def _read_json_body(self) -> Dict[str, Any]:
        """Read + parse a JSON object body, bounded by ``MAX_BODY_BYTES``."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            raise APIBadRequest("invalid Content-Length")
        if length < 0:
            raise APIBadRequest("invalid Content-Length")
        if length > MAX_BODY_BYTES:
            raise APIPayloadTooLarge(
                f"body exceeds {MAX_BODY_BYTES} bytes")
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise APIBadRequest("body must be valid JSON")
        if not isinstance(data, dict):
            raise APIBadRequest("body must be a JSON object")
        return data

    def _route(self) -> Tuple[int, str, Any]:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        qs = parse_qs(parsed.query)

        # Liveness/readiness are always open — Kubernetes probes and load
        # balancers can't carry credentials, and they expose no sensitive data.
        if path == "/health":
            return 200, "application/json", {"status": "ok", "generatedAt": time.time()}

        if path == "/ready":
            payload = readiness(self.api_source)
            return (200 if payload["ready"] else 503), "application/json", payload

        # Every remaining GET is a read: open by default, gated when
        # require_for_reads is set. principal is None for anonymous callers.
        principal = self._authorize(write=False)

        if path == "/metrics":
            if not self.api_config.prometheus.enabled:
                return 404, "application/json", self._not_found("Metrics disabled")
            return 200, "text/plain", render_prometheus_metrics(self.api_source)

        if path == "/api/v1":
            return 200, "application/json", self._api_index()

        if path == "/api/v1/ups":
            return 200, "application/json", collect_status(self.api_source)

        if path.startswith("/api/v1/ups/"):
            parts = path.split("/")
            ups_name = unquote(parts[4]) if len(parts) > 4 else ""
            if len(parts) == 5:
                payload = collect_status(self.api_source)
                row = find_status(payload, ups_name)
                if row is None:
                    return 404, "application/json", self._not_found("UPS not found")
                return 200, "application/json", row
            if len(parts) == 6 and parts[5] == "history":
                metric = (qs.get("metric") or ["charge"])[0]
                if metric not in HISTORY_METRICS:
                    allowed = ", ".join(sorted(HISTORY_METRICS))
                    return 400, "application/json", self._error(
                        "INVALID_REQUEST",
                        f"metric must be one of: {allowed}",
                    )
                end = _parse_int_param(qs, "to", int(time.time()))
                start = _parse_int_param(qs, "from", end - 3600)
                rows = query_history(self.api_config, ups_name, metric, start, end)
                if rows is None:
                    return 404, "application/json", self._not_found("UPS not found")
                return 200, "application/json", {
                    "ups": ups_name, "metric": metric, "from": start,
                    "to": end, "data": rows,
                }
            if len(parts) == 6 and parts[5] == "commands":
                return self._list_commands(ups_name)
            if len(parts) == 6 and parts[5] == "variables":
                return self._list_variables(ups_name)

        if path == "/api/v1/events":
            # Cap ``limit`` so a hostile or accidental ``?limit=10000000``
            # can't fan out into a multi-second SQLite scan across every
            # configured stats DB.
            limit = _parse_int_param(qs, "limit", 100, minimum=1, maximum=10000)
            verbosity = _parse_int_param(qs, "verbosity", 2, minimum=0, maximum=2)
            return 200, "application/json", {
                "generatedAt": time.time(),
                "events": query_events(self.api_config, limit=limit, verbosity=verbosity),
            }

        if path == "/api/v1/config":
            # Anonymous -> sanitized; authenticated -> extended (still no secrets).
            return 200, "application/json", config_summary(
                self.api_config, extended=principal is not None)

        if path == "/api/v1/remote-health":
            rows = live_remote_health(self.api_source, self.api_config)
            return 200, "application/json", {"generatedAt": time.time(), "servers": rows}

        return 404, "application/json", self._not_found("Endpoint not found")

    def _route_post(self) -> Tuple[int, str, Any]:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if path == "/api/v1/auth/login":
            return self._handle_login()
        if path == "/api/v1/auth/logout":
            return self._handle_logout()
        if path == "/api/v1/config/reload":
            return self._handle_config_reload()

        # POST /api/v1/ups/{name}/command
        parts = path.split("/")
        if len(parts) == 6 and parts[1:4] == ["api", "v1", "ups"] \
                and parts[5] == "command":
            return self._run_instant_command(unquote(parts[4]))

        return 404, "application/json", self._not_found("Endpoint not found")

    def _route_put(self) -> Tuple[int, str, Any]:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        parts = path.split("/")
        # PUT /api/v1/ups/{name}/variables/{var}
        if len(parts) == 7 and parts[1:4] == ["api", "v1", "ups"] \
                and parts[5] == "variables":
            return self._set_variable(unquote(parts[4]), unquote(parts[6]))
        return 404, "application/json", self._not_found("Endpoint not found")

    def _handle_login(self) -> Tuple[int, str, Any]:
        auth_cfg = getattr(self.api_config.api, "auth", None)
        if not (auth_cfg and auth_cfg.enabled):
            return 404, "application/json", self._not_found("Authentication is disabled")
        data = self._read_json_body()
        username = data.get("username")
        password = data.get("password")
        if not isinstance(username, str) or not isinstance(password, str) \
                or not username or not password:
            raise APIBadRequest("username and password are required")
        try:
            principal = self.api_auth.authenticate(username, password)
        except Exception:
            # bcrypt missing / store error — fail closed without leaking detail.
            return 503, "application/json", self._error(
                "AUTH_UNAVAILABLE", "authentication backend unavailable")
        if principal is None:
            raise APIUnauthorized("invalid credentials")
        token = self.api_sessions.create(principal)
        return 200, "application/json", {
            "token": token,
            "tokenType": "bearer",
            "expiresIn": int(auth_cfg.session_ttl),
        }

    def _handle_logout(self) -> Tuple[int, str, Any]:
        auth_cfg = getattr(self.api_config.api, "auth", None)
        if not (auth_cfg and auth_cfg.enabled):
            return 404, "application/json", self._not_found("Authentication is disabled")
        token = self._bearer_token()
        # Only an active session token can be logged out (API keys aren't
        # sessions). Treat anything else as unauthenticated.
        if not token or self.api_sessions.validate(token) is None:
            raise APIUnauthorized("authentication required")
        self.api_sessions.invalidate(token)
        return 200, "application/json", {"status": "ok"}

    # ----- config hot-reload (v6.0) -----

    def _handle_config_reload(self) -> Tuple[int, str, Any]:
        principal = self._authorize(write=True)
        source = self.api_source
        if not hasattr(source, "reload_config"):
            return 503, "application/json", self._error(
                "RELOAD_UNAVAILABLE", "config reload is not supported here")
        report = source.reload_config()
        self._audit(principal, "config", "reload",
                    "ok" if report.get("reloaded") else "rejected")
        status = 200 if report.get("reloaded") else 400
        return status, "application/json", report

    # ----- UPS control (v6.0) -----

    def _require_nut_control(self):
        """Return the nut_control config, or raise if the feature is off."""
        nc = getattr(self.api_config, "nut_control", None)
        if not (nc and nc.enabled):
            raise APIForbidden("UPS control is disabled (set nut_control.enabled)")
        return nc

    def _resolve_ups_name(self, ups_name: str) -> Optional[str]:
        """Return the real NUT name for a UPS id/name, or None if unknown."""
        row = find_status(collect_status(self.api_source), ups_name)
        return row["name"] if row is not None else None

    def _list_commands(self, ups_name: str) -> Tuple[int, str, Any]:
        self._authorize(write=True)
        nc = self._require_nut_control()
        real = self._resolve_ups_name(ups_name)
        if real is None:
            return 404, "application/json", self._not_found("UPS not found")
        ok, commands, err = nutctl.list_commands(real, timeout=nc.timeout)
        if not ok:
            return 502, "application/json", self._error("NUT_ERROR", err)
        allowed = set(nc.allowed_commands)
        return 200, "application/json", {
            "ups": real,
            "commands": sorted(c for c in commands if c in allowed),
            "supported": sorted(commands),
        }

    def _list_variables(self, ups_name: str) -> Tuple[int, str, Any]:
        self._authorize(write=True)
        nc = self._require_nut_control()
        real = self._resolve_ups_name(ups_name)
        if real is None:
            return 404, "application/json", self._not_found("UPS not found")
        ok, variables, err = nutctl.list_variables(real, timeout=nc.timeout)
        if not ok:
            return 502, "application/json", self._error("NUT_ERROR", err)
        allowed = set(nc.allowed_variables)
        return 200, "application/json", {
            "ups": real,
            "variables": [v for v in variables if v["name"] in allowed],
        }

    def _run_instant_command(self, ups_name: str) -> Tuple[int, str, Any]:
        principal = self._authorize(write=True)
        nc = self._require_nut_control()
        data = self._read_json_body()
        command = data.get("command")
        if not isinstance(command, str) or not command:
            raise APIBadRequest("command is required")
        if command not in set(nc.allowed_commands):
            self._audit(principal, "command", f"{ups_name}:{command}", "denied")
            raise APIForbidden(f"command {command!r} is not in allowed_commands")
        real = self._resolve_ups_name(ups_name)
        if real is None:
            return 404, "application/json", self._not_found("UPS not found")
        with _ups_lock(real):
            ok, out, err = nutctl.run_instant_command(
                real, command, nc.username, nc.password, timeout=nc.timeout)
        self._audit(principal, "command", f"{real}:{command}",
                    "ok" if ok else "failed")
        if not ok:
            return 502, "application/json", self._error("NUT_ERROR", err)
        return 200, "application/json", {"ups": real, "command": command,
                                         "status": "ok", "output": out}

    def _set_variable(self, ups_name: str, variable: str) -> Tuple[int, str, Any]:
        principal = self._authorize(write=True)
        nc = self._require_nut_control()
        data = self._read_json_body()
        value = data.get("value")
        if not isinstance(value, (str, int, float)) or isinstance(value, bool):
            raise APIBadRequest("value is required (string or number)")
        value = str(value)
        if variable not in set(nc.allowed_variables):
            self._audit(principal, "variable", f"{ups_name}:{variable}", "denied")
            raise APIForbidden(f"variable {variable!r} is not in allowed_variables")
        real = self._resolve_ups_name(ups_name)
        if real is None:
            return 404, "application/json", self._not_found("UPS not found")
        with _ups_lock(real):
            ok, out, err = nutctl.set_variable(
                real, variable, value, nc.username, nc.password, timeout=nc.timeout)
        self._audit(principal, "variable", f"{real}:{variable}",
                    "ok" if ok else "failed")
        if not ok:
            return 502, "application/json", self._error("NUT_ERROR", err)
        return 200, "application/json", {"ups": real, "variable": variable,
                                         "value": value, "status": "ok"}

    @staticmethod
    def _principal_label(principal: Optional[Dict[str, Any]]) -> str:
        if not principal:
            return "anonymous"
        if principal.get("kind") == "api_key":
            return f"apikey:{principal.get('label', principal.get('id'))}"
        return str(principal.get("username", "unknown"))

    def _audit(self, principal, kind: str, target: str, result: str) -> None:
        """Record a control action to the daemon log (7.0 adds a tamper-evident
        audit log; a structured log line is the groundwork)."""
        if self.api_log is None:
            return
        try:
            self.api_log(
                f"🔌 control: {self._principal_label(principal)} {kind} "
                f"{target} -> {result}"
            )
        except Exception:
            pass

    @staticmethod
    def _error(code: str, message: str) -> Dict[str, Dict[str, str]]:
        return {"error": {"code": code, "message": message}}

    def _not_found(self, message: str) -> Dict[str, Any]:
        payload = self._error("NOT_FOUND", message)
        payload["availableEndpoints"] = self._available_endpoints()
        return payload

    def _api_index(self) -> Dict[str, Any]:
        return {
            "generatedAt": time.time(),
            "version": "v1",
            "endpoints": self._available_endpoints(),
        }

    def _available_endpoints(self) -> List[Dict[str, Any]]:
        # Don't advertise /metrics when Prometheus is disabled — the route
        # genuinely returns 404 in that mode, so listing it would mislead
        # clients that read availableEndpoints to discover what to call.
        prometheus_enabled = bool(getattr(self.api_config.prometheus, "enabled", False))
        return [
            dict(endpoint)
            for endpoint in API_ENDPOINTS
            if endpoint["path"] != "/metrics" or prometheus_enabled
        ]


def _parse_int_param(
    qs: Dict[str, list],
    name: str,
    default: int,
    *,
    minimum: Optional[int] = None,
    maximum: Optional[int] = None,
) -> int:
    """Return one integer query parameter or raise ``APIBadRequest``."""
    raw = (qs.get(name) or [str(default)])[0]
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise APIBadRequest(f"{name} must be an integer") from None
    if minimum is not None and value < minimum:
        raise APIBadRequest(f"{name} must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise APIBadRequest(f"{name} must be <= {maximum}")
    return value


def _escape_label_value(value: Any) -> str:
    """Escape a Prometheus label value."""
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace("\r", "\\n")
        .replace('"', '\\"')
    )


def _metric_line(name: str, labels: Dict[str, str], value) -> str:
    label_text = ",".join(
        f'{k}="{_escape_label_value(v)}"'
        for k, v in labels.items()
    )
    # Prometheus exposition treats NaN as "no data" rather than as a
    # genuine zero reading. Emitting 0.0 for an unreported NUT field
    # (e.g. a UPS that never publishes input.voltage) would let alert
    # rules confuse "no telemetry" with "voltage collapsed to zero".
    # The exposition format also requires the canonical sentinels
    # ``NaN``, ``+Inf``, ``-Inf`` (case-sensitive); Python's default
    # float repr produces lowercase ``nan``/``inf`` which strict parsers
    # reject — drop the whole scrape, blank dashboards, mute alerts.
    if value is None or value == "":
        numeric_text = "NaN"
    else:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            numeric_text = "NaN"
        else:
            if math.isnan(numeric):
                numeric_text = "NaN"
            elif math.isinf(numeric):
                numeric_text = "+Inf" if numeric > 0 else "-Inf"
            else:
                numeric_text = f"{numeric}"
    return f"{name}{{{label_text}}} {numeric_text}"


# State-machine metric labels. Standard Prometheus practice for
# enum/state metrics is to emit one series per possible label with
# 0 or 1, so transitions appear as flips rather than series gaps.
# Keep these tuples aligned with the values written by
# src/eneru/health/voltage.py.
_VOLTAGE_STATE_LABELS = ("NORMAL", "LOW", "HIGH")
_AVR_STATE_LABELS = ("INACTIVE", "BOOST", "TRIM")
_BINARY_STATE_LABELS = ("INACTIVE", "ACTIVE")


def _state_metric_lines(
    name: str,
    labels: Dict[str, str],
    current: Any,
    possible_labels: Sequence[str],
    default: str,
) -> List[str]:
    # Build the labels dict once and only mutate the "state" entry per
    # iteration; the previous {**labels, "state": label} comprehension
    # allocated one fresh dict per state per UPS per scrape (10 per UPS
    # across the four state metrics). _metric_line consumes the dict
    # synchronously so reusing the same instance is safe.
    active = current if current in possible_labels else default
    line_labels = dict(labels)
    lines = []
    for label in possible_labels:
        line_labels["state"] = label
        lines.append(_metric_line(name, line_labels, 1 if label == active else 0))
    return lines


_LOOPBACK_BINDS = frozenset({"127.0.0.1", "::1", "localhost"})


def _is_loopback_bind(bind: str) -> bool:
    """Return True if the configured bind address is loopback-only."""
    return (bind or "").strip().lower() in _LOOPBACK_BINDS


# Prometheus exposition format expects every metric to be preceded by
# its own HELP/TYPE block exactly once per scrape. Centralising the
# catalogue keeps render_prometheus_metrics() honest as new metrics are
# added — every entry here gets emitted, regardless of whether any UPS
# row currently produces a sample.
_METRIC_CATALOGUE = (
    ("eneru_up", "gauge", "Whether the Eneru API is serving metrics (1) or not."),
    ("eneru_ups_battery_charge", "gauge", "UPS battery charge percentage."),
    ("eneru_ups_runtime_seconds", "gauge", "UPS runtime estimate in seconds."),
    ("eneru_ups_load_percent", "gauge", "UPS load percentage."),
    ("eneru_ups_input_voltage", "gauge", "UPS input voltage in volts."),
    ("eneru_ups_output_voltage", "gauge", "UPS output voltage in volts."),
    ("eneru_ups_battery_voltage", "gauge", "UPS battery voltage in volts."),
    ("eneru_ups_temperature_celsius", "gauge", "UPS temperature in degrees Celsius."),
    ("eneru_ups_input_frequency_hz", "gauge", "UPS input frequency in hertz."),
    ("eneru_ups_output_frequency_hz", "gauge", "UPS output frequency in hertz."),
    ("eneru_ups_nominal_voltage", "gauge", "Snapped nominal grid voltage in volts."),
    ("eneru_ups_voltage_warning_low", "gauge", "Derived low-voltage warning threshold."),
    ("eneru_ups_voltage_warning_high", "gauge", "Derived high-voltage warning threshold."),
    ("eneru_ups_voltage_state", "gauge",
     "Grid-quality voltage state (one series per label, 1 if active)."),
    ("eneru_ups_avr_state", "gauge",
     "AVR state (one series per label, 1 if active)."),
    ("eneru_ups_bypass_state", "gauge",
     "Bypass state (one series per label, 1 if active)."),
    ("eneru_ups_overload_state", "gauge",
     "Overload state (one series per label, 1 if active)."),
    ("eneru_ups_depletion_rate_percent_per_minute", "gauge",
     "UPS battery depletion rate (charge percent per minute on battery)."),
    ("eneru_ups_time_on_battery_seconds", "gauge",
     "Seconds the UPS has been continuously on battery (0 when on line)."),
    ("eneru_ups_connection_failed", "gauge",
     "1 if the upsd connection for this UPS is in the FAILED state, 0 otherwise."),
    ("eneru_ups_trigger_active", "gauge",
     "1 if a shutdown trigger is currently active for this UPS, 0 otherwise."),
    ("eneru_remote_health_status", "gauge",
     "Remote SSH target status indicator (1 per status label combination)."),
    ("eneru_remote_health_consecutive_failures", "gauge",
     "Consecutive failed remote-health probes for this SSH target."),
)


def render_prometheus_metrics(source: Any) -> str:
    """Render Prometheus text exposition for live Eneru state."""
    payload = collect_status(source)
    lines = []
    for name, mtype, help_text in _METRIC_CATALOGUE:
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {mtype}")
    lines.append("eneru_up 1")
    for row in payload.get("ups", []):
        labels = {"ups": row["name"], "label": row["label"]}
        power = row.get("powerQuality", {})
        lines.append(_metric_line("eneru_ups_battery_charge", labels, row["batteryCharge"]))
        lines.append(_metric_line("eneru_ups_runtime_seconds", labels, row["runtime"]))
        lines.append(_metric_line("eneru_ups_load_percent", labels, row["load"]))
        lines.append(_metric_line("eneru_ups_input_voltage", labels, power.get("inputVoltage")))
        lines.append(_metric_line("eneru_ups_output_voltage", labels, power.get("outputVoltage")))
        lines.append(_metric_line("eneru_ups_battery_voltage", labels, power.get("batteryVoltage")))
        lines.append(_metric_line("eneru_ups_temperature_celsius", labels, power.get("temperature")))
        lines.append(_metric_line("eneru_ups_input_frequency_hz", labels, power.get("inputFrequency")))
        lines.append(_metric_line("eneru_ups_output_frequency_hz", labels, power.get("outputFrequency")))
        lines.append(_metric_line("eneru_ups_nominal_voltage", labels, power.get("nominalVoltage")))
        lines.append(_metric_line("eneru_ups_voltage_warning_low", labels, power.get("warningLow")))
        lines.append(_metric_line("eneru_ups_voltage_warning_high", labels, power.get("warningHigh")))
        lines.extend(_state_metric_lines(
            "eneru_ups_voltage_state", labels,
            power.get("voltageState"), _VOLTAGE_STATE_LABELS, "NORMAL",
        ))
        lines.extend(_state_metric_lines(
            "eneru_ups_avr_state", labels,
            power.get("avrState"), _AVR_STATE_LABELS, "INACTIVE",
        ))
        lines.extend(_state_metric_lines(
            "eneru_ups_bypass_state", labels,
            power.get("bypassState"), _BINARY_STATE_LABELS, "INACTIVE",
        ))
        lines.extend(_state_metric_lines(
            "eneru_ups_overload_state", labels,
            power.get("overloadState"), _BINARY_STATE_LABELS, "INACTIVE",
        ))
        lines.append(_metric_line("eneru_ups_depletion_rate_percent_per_minute", labels, row["depletionRate"]))
        lines.append(_metric_line("eneru_ups_time_on_battery_seconds", labels, row["timeOnBattery"]))
        lines.append(_metric_line(
            "eneru_ups_connection_failed",
            labels,
            1 if row["connectionState"] == "FAILED" else 0,
        ))
        lines.append(_metric_line(
            "eneru_ups_trigger_active",
            labels,
            1 if row["triggerActive"] else 0,
        ))
        for server in row.get("remoteHealth", []):
            s_labels = {
                "ups": row["name"],
                "server": server.get("server", ""),
                "host": server.get("host", ""),
            }
            lines.append(_metric_line(
                "eneru_remote_health_status",
                {**s_labels, "status": server.get("status", "UNKNOWN")},
                1,
            ))
            lines.append(_metric_line(
                "eneru_remote_health_consecutive_failures",
                s_labels,
                server.get("consecutive_failures", 0),
            ))
    # Redundancy-group-owned remote targets (configured under
    # ``redundancy_groups[*].remote_servers``) need the same coverage as
    # ups-owned ones — otherwise a Prometheus consumer scraping for
    # ``eneru_remote_health_status`` silently misses every target attached
    # to a redundancy group. Use ``redundancy_group`` as the grouping
    # label so a downstream alert can ``by (redundancy_group)`` cleanly.
    for group in payload.get("redundancyGroups", []):
        for server in group.get("remoteHealth", []):
            s_labels = {
                "redundancy_group": group.get("name", ""),
                "server": server.get("server", ""),
                "host": server.get("host", ""),
            }
            lines.append(_metric_line(
                "eneru_remote_health_status",
                {**s_labels, "status": server.get("status", "UNKNOWN")},
                1,
            ))
            lines.append(_metric_line(
                "eneru_remote_health_consecutive_failures",
                s_labels,
                server.get("consecutive_failures", 0),
            ))
    return "\n".join(lines) + "\n"
