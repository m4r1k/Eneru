"""Embedded HTTP API, Prometheus endpoint, and (v6.0) authenticated write-path."""

import importlib.resources
import json
import math
import os
import re
import secrets
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import parse_qs, unquote, urlparse

from eneru import nut_control as nutctl
from eneru import self_test as selftest
from eneru.auth import AuthStore
from eneru.status import (
    HISTORY_METRICS,
    collect_status,
    config_summary,
    find_status,
    live_remote_health,
    monitor_status,
    power_series,
    query_events,
    query_history,
    readiness,
)

# Cap request bodies so a hostile/accidental huge POST can't exhaust memory in a
# handler thread. Auth + control payloads are tiny JSON objects.
MAX_BODY_BYTES = 64 * 1024
# Bound reads from each client socket. Without this, a client can declare a
# small Content-Length and drip bytes forever, pinning a non-daemon handler.
REQUEST_READ_TIMEOUT_SECONDS = 10

# Dashboard static assets (served from the eneru.web package). Only these flat
# names are servable; the strict name check below makes path traversal impossible.
_STATIC_CONTENT_TYPES = {
    ".html": "text/html",
    ".js": "application/javascript",
    ".css": "text/css",
}
_DASHBOARD_INDEX = "index.html"
# A conservative charset for upsrw values. NUT values are short tokens (numbers,
# enum words, voltages). upscmd/upsrw run via execve arg lists (never a shell),
# so this is defense-in-depth, not the only barrier — but it keeps control
# characters and shell-ish metacharacters out of the value entirely.
# L9: anchor with \Z, not $ -- in Python `$` also matches just before a trailing
# newline, so "value\n" would slip through this defense-in-depth charset filter.
_SAFE_NUT_VALUE = re.compile(r"\A[A-Za-z0-9 ._:+%/,\-]{1,64}\Z")
# Strict response content-type whitelist (avoids header injection if a route
# ever passes user data through as a content type).
_CONTENT_TYPE_HEADERS = {
    "application/json": "application/json; charset=utf-8",
    "text/plain": "text/plain; charset=utf-8",
    "text/html": "text/html; charset=utf-8",
    "application/javascript": "application/javascript; charset=utf-8",
    "text/css": "text/css; charset=utf-8",
}

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

    @property
    def ttl(self) -> int:
        """The effective session lifetime in seconds (clamped to >= 1)."""
        return self._ttl

    def create(self, principal: Dict[str, Any]) -> str:
        token = secrets.token_urlsafe(32)
        now = time.time()
        expiry = now + self._ttl
        with self._lock:
            # Opportunistically drop expired entries so repeated logins don't
            # grow the table unbounded (they're only otherwise reaped on reuse).
            expired = [t for t, (_p, e) in self._sessions.items() if e < now]
            for t in expired:
                del self._sessions[t]
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
        "query": {
            "limit": "1..10000",
            "verbosity": "0..2",
            "from": "unix timestamp",
            "to": "unix timestamp",
            "before": "unix timestamp cursor",
            "beforeSource": "source-qualified cursor source",
            "beforeId": "source-qualified cursor id",
        },
    },
    {"path": "/api/v1/config", "description": "Configuration summary (extended when authenticated)"},
    {"path": "/api/v1/remote-health", "description": "Remote SSH health rows"},
    {"path": "/api/v1/auth/state", "description": "Effective auth state for dashboard login bootstrap"},
    {"path": "/api/v1/auth/login", "description": "POST username/password for a session token (when auth enabled)"},
    {"path": "/api/v1/auth/logout", "description": "POST to invalidate the current session token"},
    {"path": "/api/v1/ups/{name}/commands", "description": "Allowlisted UPS commands (when nut_control enabled)"},
    {"path": "/api/v1/ups/{name}/command", "description": "POST {command} to run an allowlisted upscmd"},
    {"path": "/api/v1/ups/{name}/variables", "description": "Allowlisted writable UPS variables (upsrw)"},
    {"path": "/api/v1/ups/{name}/variables/{var}", "description": "PUT {value} to set an allowlisted upsrw variable"},
    {"path": "/api/v1/ups/{name}/events", "description": "DELETE selected events {items:[{id,ts,eventType}]} (auth required)"},
    {"path": "/api/v1/ups/{name}/battery-health", "description": "Battery-health score, terms, and replacement projection (v6.1)"},
    {"path": "/api/v1/ups/{name}/energy", "description": "Energy (kWh) and optional cost, today/month (v6.1)"},
    {"path": "/api/v1/ups/{name}/power", "description": "Per-sample load% + watts series for the Energy chart (v6.1)"},
    {"path": "/api/v1/ups/{name}/self-test", "description": "POST to issue a UPS self-test (auth required, allowlisted) (v6.1)"},
    {"path": "/api/v1/config/reload", "description": "POST to re-read config and apply the safe subset live"},
)


def _auth_is_active(config: Any) -> bool:
    """Whether API authentication is effectively enforced.

    An explicit ``api.auth.enabled`` (true *or* false) always wins. When it is
    left unset, auth is active iff the auth DB already has at least one user — so
    "create a user, then sign in" works **with no restart**, while a fresh
    install with no users stays open (v5.3 read-only behavior). The DB is never
    created as a side effect of this check. Once the DB file exists, a
    broken/unreadable DB fails closed to "active": reads that require auth stay
    gated and writes require credentials rather than silently reopening.
    """
    auth_cfg = getattr(getattr(config, "api", None), "auth", None)
    if auth_cfg is None:
        return False
    if getattr(auth_cfg, "enabled_explicitly_set", False):
        return bool(auth_cfg.enabled)
    if auth_cfg.enabled:
        return True
    try:
        if not os.path.exists(auth_cfg.db_path):
            return False
        return AuthStore(auth_cfg.db_path).user_count() > 0
    except Exception:
        return True


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
        # Auth machinery is built whenever the auth config exists — construction
        # is free (no I/O until first use), and the store must be ready in case
        # auth becomes active at runtime without a restart (e.g. the first user
        # is created while the daemon runs; see _auth_is_active). Whether auth is
        # actually *enforced* is decided dynamically per request, not here.
        self._auth_store: Optional[AuthStore] = None
        self._sessions: Optional[SessionManager] = None
        auth_cfg = getattr(config.api, "auth", None)
        if auth_cfg is not None:
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
            self.log_fn(f"⚠️  API server failed to bind {addr[0]}:{addr[1]}: {exc}")
            return
        # v6.0: worker threads are NON-daemon. The API now has non-idempotent
        # write endpoints (control commands, config reload), so a worker must
        # not be cut off mid-request on shutdown — that would leave a caller
        # unsure whether a command actually ran. ``stop()`` calls
        # ``shutdown()`` + ``server_close()`` to drain in-flight handlers
        # gracefully. Handlers are bounded (subprocess timeouts on control
        # calls), so this can't hang shutdown indefinitely.
        self._httpd.daemon_threads = False
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            name="eneru-api",
            daemon=True,
        )
        self._thread.start()
        self.log_fn(f"📊  API server listening on {addr[0]}:{addr[1]}")
        # Off-loopback binds still need a trusted network boundary, but auth
        # disabled is not a write-surface problem in v6.0: write endpoints are
        # closed, and anonymous /api/v1/config responses are sanitized.
        auth_on = _auth_is_active(self.config)
        if not _is_loopback_bind(self.config.api.bind):
            if auth_on:
                self.log_fn(
                    f"ℹ️  API bound to {addr[0]} with auth enabled. Read endpoints "
                    "stay open unless api.auth.require_for_reads is set."
                )
            else:
                self.log_fn(
                    f"ℹ️  API bound to {addr[0]} with auth disabled. Write endpoints "
                    "are disabled, and /api/v1/config returns a sanitized view "
                    "to anonymous clients; restrict network access before "
                    "exposing read endpoints beyond trusted hosts."
                )
        elif not auth_on:
            # Loopback + auth off: the dashboard hides its Sign-in button in this
            # state, so spell out why and how to enable login/control. (The
            # off-loopback branch already warns above.)
            self.log_fn(
                "ℹ️  API authentication is disabled; the dashboard is read-only "
                "and Sign-in is hidden. Set api.auth.enabled: true and run "
                "`eneru user create` to enable login and UPS control."
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

    def do_DELETE(self):  # noqa: N802 - stdlib hook
        self._dispatch(self._route_delete)

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
        elif isinstance(body, (bytes, bytearray)):
            raw = bytes(body)
        else:
            raw = str(body).encode("utf-8")
        self.send_response(status)
        self.send_header(
            "Content-Type",
            _CONTENT_TYPE_HEADERS.get(content_type,
                                      "application/json; charset=utf-8"))
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        # N2: nosniff on EVERY response (JSON/JS/CSS/HTML), not just HTML -- the
        # dashboard's static assets are served with specific content types and
        # must not be MIME-sniffed.
        self.send_header("X-Content-Type-Options", "nosniff")
        if content_type == "text/html":
            # The dashboard ships no inline scripts/styles and loads nothing
            # third-party, so a strict same-origin CSP locks it down.
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; base-uri 'none'; frame-ancestors 'none'")
        if status == 401:
            self.send_header("WWW-Authenticate", "Bearer")
        self.end_headers()
        self.wfile.write(raw)

    def _serve_static(self, path: str) -> Optional[Tuple[str, bytes]]:
        """Return ``(content_type, bytes)`` for a dashboard asset, or None.

        Only flat asset names from the ``eneru.web`` package are servable. The
        strict name check (no ``/``, no ``..``) makes path traversal impossible.
        """
        name = _DASHBOARD_INDEX if path == "/" else path.lstrip("/")
        if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
            return None
        content_type = _STATIC_CONTENT_TYPES.get(os.path.splitext(name)[1])
        if content_type is None:
            return None
        try:
            data = (importlib.resources.files("eneru.web") / name).read_bytes()
        except (FileNotFoundError, OSError, ModuleNotFoundError):
            return None
        return content_type, data

    # ----- auth helpers (v6.0) -----

    # Cache for the "do users exist" probe on the read path: every GET calls
    # _authorize(), so an uncached per-request user_count() would add a SQLite
    # open to each scrape. Stored on the base class (shared across the per-start
    # Handler subclass and all worker threads); races are benign — independent
    # writes of a float and a bool, worst case a redundant probe.
    _auth_active_ts: float = 0.0
    _auth_active_val: bool = False
    _auth_active_key: Optional[str] = None
    _AUTH_ACTIVE_TTL: float = 5.0

    def _auth_active(self, *, refresh: bool = False) -> bool:
        """Effective auth state for this request (see :func:`_auth_is_active`).

        Explicit/enabled cases are resolved instantly with no I/O; only the
        unpinned "users exist?" branch consults the DB, behind a short TTL cache
        so a brand-new first user is honored within seconds and no restart is
        ever required.
        """
        auth_cfg = getattr(self.api_config.api, "auth", None)
        if auth_cfg is None:
            return False
        if getattr(auth_cfg, "enabled_explicitly_set", False):
            return bool(auth_cfg.enabled)
        if auth_cfg.enabled:
            return True
        base = EneruAPIHandler
        now = time.time()
        cache_key = str(getattr(auth_cfg, "db_path", ""))
        if (
            refresh
            or base._auth_active_key != cache_key
            or now - base._auth_active_ts > base._AUTH_ACTIVE_TTL
        ):
            base._auth_active_val = _auth_is_active(self.api_config)
            base._auth_active_ts = now
            base._auth_active_key = cache_key
        return base._auth_active_val

    def _bearer_token(self) -> Optional[str]:
        """Return the credential from Authorization: Bearer or X-API-Key."""
        header = self.headers.get("Authorization", "") or ""
        # The auth scheme is case-insensitive per RFC 7235 ("Bearer"/"bearer").
        if header[:7].lower() == "bearer ":
            return header[7:].strip() or None
        api_key = self.headers.get("X-API-Key")
        return api_key.strip() if api_key else None

    def _authenticate_request(self, *, strict: bool = False) -> Optional[Dict[str, Any]]:
        """Resolve the caller from a session token, then an API key. None if neither.

        ``strict`` is set for WRITE/control paths: when a session's user lookup
        fails (auth DB unavailable) the request is denied rather than allowed, so
        a deleted admin can't run control commands during a DB outage. Reads
        stay lenient (``strict=False``) so a transient blip doesn't log out a
        valid user. An *unknown* status never invalidates the token either way,
        so a genuine session recovers once the DB is back (M4).
        """
        token = self._bearer_token()
        if not token:
            return None
        if self.api_sessions is not None:
            principal = self.api_sessions.validate(token)
            if principal is not None:
                status = self._session_user_status(principal)
                if status == "ok":
                    return principal
                if status == "gone":
                    # The backing user was deleted while the session lived — kill
                    # the token so the client (and any later request) 401s.
                    self.api_sessions.invalidate(token)
                    return None
                # status == "unknown" (transient auth-DB error): writes fail
                # closed, reads keep the session. Token left intact.
                return None if strict else principal
        if self.api_auth is not None:
            try:
                principal = self.api_auth.authenticate_api_key(token)
            except Exception:
                principal = None
            if principal is not None:
                return principal
        return None

    def _session_user_status(self, principal: Dict[str, Any]) -> str:
        """Return ``'ok'`` | ``'gone'`` | ``'unknown'`` for a session principal.

        - ``'ok'``      -- the user exists (or this isn't a DB-backed user session)
        - ``'gone'``    -- the user row is definitively absent (deleted)
        - ``'unknown'`` -- the lookup raised (auth DB unavailable)

        Sessions are in-memory and outlive the DB row they were minted from, so a
        deleted user would otherwise stay signed in until TTL. Non-user
        principals (API keys) are re-checked against the DB on their own path.
        """
        if principal.get("kind") != "user":
            return "ok"
        store = self.api_auth
        if store is None:
            return "ok"
        username = principal.get("username")
        if not username:
            return "ok"
        try:
            user = store.get_user(username)
            if user is None:
                return "gone"
            issued_at = principal.get("password_changed_at")
            if issued_at is not None and user.get("password_changed_at") != issued_at:
                return "gone"
            return "ok"
        except Exception:
            return "unknown"

    def _authorize(self, *, write: bool) -> Optional[Dict[str, Any]]:
        """Enforce the tiered auth policy. Returns the principal (may be None).

        - auth disabled: reads are open; **writes are hard-disabled (403)** —
          "auth off" never means "control open".
        - auth enabled: writes require a credential (401 otherwise); reads are
          open unless ``require_for_reads``, then they require one too.
        """
        auth_cfg = getattr(self.api_config.api, "auth", None)
        enabled = self._auth_active()
        if not enabled:
            if write:
                raise APIForbidden(
                    "write operations require api.auth.enabled")
            return None
        # Writes re-check the user account strictly (fail closed on DB error).
        principal = self._authenticate_request(strict=write)
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
        deadline = time.monotonic() + REQUEST_READ_TIMEOUT_SECONDS
        timeout_changed = False
        previous_timeout = None
        connection = getattr(self, "connection", None)
        if length and connection is not None:
            try:
                previous_timeout = connection.gettimeout()
                connection.settimeout(REQUEST_READ_TIMEOUT_SECONDS)
                timeout_changed = True
            except Exception:
                # Tests and uncommon socket wrappers may not expose timeout
                # controls. Real sockets get the read-specific timeout above.
                pass
        try:
            try:
                chunks = []
                remaining = length
                reader = getattr(self.rfile, "read1", None)
                if not callable(reader):
                    reader = self.rfile.read
                while remaining:
                    time_left = deadline - time.monotonic()
                    if time_left <= 0:
                        raise APIBadRequest("request body timed out")
                    if timeout_changed:
                        try:
                            connection.settimeout(time_left)
                        except Exception:
                            pass
                    chunk = reader(min(remaining, 65536))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                raw = b"".join(chunks) if length else b""
            except (socket.timeout, TimeoutError):
                raise APIBadRequest("request body timed out")
            except OSError:
                raise APIBadRequest("failed to read request body")
        finally:
            if timeout_changed:
                try:
                    connection.settimeout(previous_timeout)
                except Exception:
                    pass
        if length and len(raw) != length:
            raise APIBadRequest("incomplete request body")
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

        # Dashboard static assets (HTML/CSS/JS) carry no data and are served
        # unauthenticated so the login form can render; the API calls the page
        # then makes are themselves gated. Served whenever the API is enabled.
        static = self._serve_static(path)
        if static is not None:
            content_type, data = static
            return 200, content_type, data

        # Liveness/readiness are always open — Kubernetes probes and load
        # balancers can't carry credentials, and they expose no sensitive data.
        if path == "/health":
            return 200, "application/json", {"status": "ok", "generatedAt": time.time()}

        if path == "/ready":
            payload = readiness(self.api_source)
            return (200 if payload["ready"] else 503), "application/json", payload

        # Auth bootstrap must stay open even when require_for_reads=true;
        # otherwise the dashboard cannot learn that it should show the login
        # form before it has a token.
        if path == "/api/v1/auth/state":
            auth_cfg = getattr(self.api_config.api, "auth", None)
            # This is the dashboard bootstrap signal. Refresh it immediately so
            # `eneru user create` can make the Sign-in button appear without a
            # daemon restart or waiting for the read-path cache to expire.
            auth_active = self._auth_active(refresh=True)
            return 200, "application/json", {
                "enabled": auth_active,
                "requireForReads": bool(
                    auth_active and getattr(auth_cfg, "require_for_reads", False)
                ),
            }

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
                # Earliest data that can exist = the hourly-aggregate retention
                # horizon. Omitting `from` ("All") maps here, not to an unbounded
                # scan; an explicit `from` older than this is clamped up to it.
                horizon = end - int(
                    self.api_config.statistics.retention.agg_hourly_days) * 86400
                if "from" in qs:
                    start = _parse_int_param(qs, "from", end - 3600)
                    if start > end:
                        return 400, "application/json", self._error(
                            "INVALID_REQUEST", "'from' must be <= 'to'")
                    start = max(start, horizon)
                else:
                    start = horizon
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
            if len(parts) == 6 and parts[5] in ("battery-health", "energy"):
                # v6.1 read endpoints (same data the /status row carries). Build
                # only the matched monitor's status — not the whole fleet — so a
                # single-UPS read doesn't run every other UPS's (DB-heavy) energy
                # query.
                mon = self._monitor_for(ups_name)
                if mon is None:
                    return 404, "application/json", self._not_found("UPS not found")
                row = monitor_status(mon)
                key = "batteryHealth" if parts[5] == "battery-health" else "energy"
                return 200, "application/json", {
                    "ups": row.get("name", ups_name), key: row.get(key)}
            if len(parts) == 6 and parts[5] == "power":
                # v6.1 Energy-tab series: per-sample load% + watts (realpower or
                # the load*nominal fallback, flagged estimated). Same retention
                # window handling as /history.
                mon = self._monitor_for(ups_name)
                if mon is None:
                    return 404, "application/json", self._not_found("UPS not found")
                end = _parse_int_param(qs, "to", int(time.time()))
                horizon = end - int(
                    self.api_config.statistics.retention.agg_hourly_days) * 86400
                if "from" in qs:
                    start = _parse_int_param(qs, "from", end - 3600)
                    if start > end:
                        return 400, "application/json", self._error(
                            "INVALID_REQUEST", "'from' must be <= 'to'")
                    start = max(start, horizon)
                else:
                    start = horizon
                store = getattr(mon, "_stats_store", None)
                return 200, "application/json", {
                    "ups": ups_name, "from": start, "to": end,
                    "data": power_series(store, start, end),
                }

        if path == "/api/v1/events":
            # Cap ``limit`` so a hostile or accidental ``?limit=10000000``
            # can't fan out into a multi-second SQLite scan across every
            # configured stats DB.
            limit = _parse_int_param(qs, "limit", 100, minimum=1, maximum=10000)
            verbosity = _parse_int_param(qs, "verbosity", 2, minimum=0, maximum=2)
            start_ts = _parse_int_param(qs, "from", None) if "from" in qs else None
            end_ts = _parse_int_param(qs, "to", None) if "to" in qs else None
            if start_ts is not None and end_ts is not None and start_ts > end_ts:
                return 400, "application/json", self._error(
                    "INVALID_REQUEST", "'from' must be <= 'to'")
            before_ts = None
            before_cursor = None
            has_cursor_detail = "beforeSource" in qs or "beforeId" in qs
            if "before" in qs:
                before_ts = _parse_int_param(qs, "before", None)
                if has_cursor_detail:
                    if "beforeSource" not in qs or "beforeId" not in qs:
                        raise APIBadRequest(
                            "'beforeSource' and 'beforeId' must be supplied together")
                    before_source = (qs.get("beforeSource") or [""])[0]
                    if not before_source:
                        raise APIBadRequest("'beforeSource' is required")
                    before_id = _parse_int_param(qs, "beforeId", None, minimum=1)
                    before_cursor = (before_ts, before_source, before_id)
            elif has_cursor_detail:
                raise APIBadRequest("'before' is required with cursor details")
            return 200, "application/json", {
                "generatedAt": time.time(),
                "events": query_events(
                    self.api_config, limit=limit, verbosity=verbosity,
                    start_ts=start_ts, end_ts=end_ts, before_ts=before_ts,
                    before_cursor=before_cursor),
            }

        if path == "/api/v1/config":
            # Anonymous -> sanitized; authenticated -> extended (still no secrets).
            summary = config_summary(
                self.api_config, extended=principal is not None)
            # Report the *effective* auth state: auth can be active via existing
            # users even when api.auth.enabled is unset, and the dashboard reads
            # this field to decide whether to show the Sign-in button.
            summary["api"]["auth"]["enabled"] = self._auth_active()
            return 200, "application/json", summary

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
        # POST /api/v1/ups/{name}/self-test (v6.1)
        if len(parts) == 6 and parts[1:4] == ["api", "v1", "ups"] \
                and parts[5] == "self-test":
            return self._run_self_test(unquote(parts[4]))

        return 404, "application/json", self._not_found("Endpoint not found")

    def _run_self_test(self, ups_name: str) -> Tuple[int, str, Any]:
        """Issue a UPS self-test (auth-gated, audited). Goes through the same
        nut_control allowlist as the manual command path -- the self-test
        command must be allowlisted."""
        principal = self._authorize(write=True)
        self._require_nut_control()
        real = self._resolve_ups_name(ups_name)
        if real is None:
            return 404, "application/json", self._not_found("UPS not found")
        nc = self._effective_nut_control(real)
        command = self._effective_self_test(real).command
        if not nutctl.command_allowed(command, nc.allowed_commands):
            self._audit(principal, "self-test", f"{real}:{command}", "denied")
            raise APIForbidden(
                f"self_test.command {command!r} is not in allowed_commands")
        # Preflight against `upscmd -l` like the scheduled/direct paths, so the
        # API honors the same "self-disable if unsupported" contract the docs
        # promise rather than letting NUT reject it after the fact.
        try:
            exposed = selftest.discover_self_test_command(
                real, command, timeout=nc.timeout)
        except selftest.SelfTestUnavailable as exc:
            self._audit(principal, "self-test", f"{real}:{command}", "failed")
            return 502, "application/json", self._error("NUT_ERROR", str(exc))
        if exposed is None:
            self._audit(principal, "self-test", f"{real}:{command}", "failed")
            return 422, "application/json", self._error(
                "UNSUPPORTED",
                f"UPS {real} does not expose {command!r} (upscmd -l)")
        store = self._store_for_ups(real)
        with _ups_lock(real):
            result = selftest.issue_self_test(
                real, command, nc, store, source="api")
        self._audit(principal, "self-test", f"{real}:{command}",
                    "ok" if result["ok"] else "failed")
        if not result["ok"]:
            return 502, "application/json", self._error("NUT_ERROR", result["error"])
        return 200, "application/json", {"ups": real, "command": command,
                                         "status": "issued",
                                         "testId": result["test_id"]}

    def _route_delete(self) -> Tuple[int, str, Any]:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        parts = path.split("/")
        # DELETE /api/v1/ups/{name}/events
        if len(parts) == 6 and parts[1:4] == ["api", "v1", "ups"] \
                and parts[5] == "events":
            return self._delete_events(unquote(parts[4]))
        return 404, "application/json", self._not_found("Endpoint not found")

    def _delete_events(self, ups_name: str) -> Tuple[int, str, Any]:
        """Delete selected events for one UPS (auth-gated, audited).

        Body: ``{"items": [{"id", "ts", "eventType"}, ...]}``. Each item is
        validated strictly; the store matches on all three fields so a stale
        client can only delete the exact rows it saw.
        """
        principal = self._authorize(write=True)
        data = self._read_json_body()
        items = data.get("items")
        if not isinstance(items, list):
            raise APIBadRequest("'items' must be a list")
        if len(items) > 1000:
            raise APIPayloadTooLarge("too many items (max 1000)")
        normalized = []
        for it in items:
            if not isinstance(it, dict):
                raise APIBadRequest("each item must be an object")
            event_id, ts, event_type = it.get("id"), it.get("ts"), it.get("eventType")
            if not isinstance(event_id, int) or isinstance(event_id, bool):
                raise APIBadRequest("item 'id' must be an integer")
            if not isinstance(ts, int) or isinstance(ts, bool):
                raise APIBadRequest("item 'ts' must be an integer")
            if not isinstance(event_type, str) or not event_type:
                raise APIBadRequest("item 'eventType' is required")
            normalized.append((event_id, ts, event_type))
        real = self._resolve_ups_name(ups_name)
        if real is None:
            return 404, "application/json", self._not_found("UPS not found")
        source = self.api_source
        deleted = (source.delete_events(real, normalized)
                   if hasattr(source, "delete_events") else None)
        if deleted is None:
            return 503, "application/json", self._error(
                "STATS_UNAVAILABLE", "the statistics store is unavailable")
        self._audit(principal, "events", f"{real}:delete", f"{deleted} rows")
        return 200, "application/json", {"ups": real, "deleted": deleted}

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
        # Refresh the first-user probe here too: a user may create the initial
        # account in another shell and submit the login form before any
        # background dashboard poll has refreshed /auth/state.
        if not self._auth_active(refresh=True):
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
            # Report the manager's effective TTL (clamped), not the raw config,
            # so the client's expiry matches the server's.
            "expiresIn": self.api_sessions.ttl,
        }

    def _handle_logout(self) -> Tuple[int, str, Any]:
        if not self._auth_active():
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

    def _effective_nut_control(self, ups_name: str):
        """Resolve the nut_control config for one UPS.

        A per-group override is already fully resolved at parse time (unset fields
        inherited the global config), so it's used as-is — no per-field fallback
        that could silently widen a deliberately-narrowed allowlist. The
        ``enabled`` flag is always forced from the global config: a per-group
        block can never enable control when the global gate is off.
        """
        glob = self.api_config.nut_control
        for group in getattr(self.api_config, "ups_groups", []):
            if group.ups.name == ups_name and getattr(group, "nut_control", None):
                override = group.nut_control
                from eneru.config import NutControlConfig
                return NutControlConfig(
                    enabled=glob.enabled,
                    username=override.username,
                    password=override.password,
                    allowed_commands=override.allowed_commands,
                    allowed_variables=override.allowed_variables,
                    timeout=override.timeout,
                )
        return glob

    def _effective_self_test(self, ups_name: str):
        """Resolve the self_test config for one UPS (per-group override if set),
        mirroring _effective_nut_control so a per-UPS `self_test.command` is
        honored instead of always using the global command."""
        glob = self.api_config.self_test
        for group in getattr(self.api_config, "ups_groups", []):
            if group.ups.name == ups_name and getattr(group, "self_test", None):
                return group.self_test
        return glob

    def _monitor_for(self, ups_name: str):
        """Return the live monitor object owning ``ups_name`` (raw or sanitized
        match), or None. Used to reach a single UPS's stats store / status
        without walking the whole fleet."""
        from eneru.status import iter_monitors, sanitize_name
        target = sanitize_name(ups_name)
        for mon in iter_monitors(self.api_source):
            name = getattr(getattr(mon, "config", None), "ups", None)
            name = getattr(name, "name", None)
            if name and (name == ups_name or sanitize_name(name) == target):
                return mon
        return None

    def _store_for_ups(self, ups_name: str):
        """The per-UPS stats store for ``ups_name``, or None when unavailable
        (the store methods no-op on None, matching the daemon's failure
        isolation)."""
        mon = self._monitor_for(ups_name)
        return getattr(mon, "_stats_store", None) if mon is not None else None

    def _list_commands(self, ups_name: str) -> Tuple[int, str, Any]:
        self._authorize(write=True)
        self._require_nut_control()
        real = self._resolve_ups_name(ups_name)
        if real is None:
            return 404, "application/json", self._not_found("UPS not found")
        nc = self._effective_nut_control(real)
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
        self._require_nut_control()
        real = self._resolve_ups_name(ups_name)
        if real is None:
            return 404, "application/json", self._not_found("UPS not found")
        nc = self._effective_nut_control(real)
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
        self._require_nut_control()
        data = self._read_json_body()
        command = data.get("command")
        if not isinstance(command, str) or not command:
            raise APIBadRequest("command is required")
        real = self._resolve_ups_name(ups_name)
        if real is None:
            return 404, "application/json", self._not_found("UPS not found")
        nc = self._effective_nut_control(real)
        if not nutctl.command_allowed(command, nc.allowed_commands):
            self._audit(principal, "command", f"{real}:{command}", "denied")
            raise APIForbidden(f"command {command!r} is not in allowed_commands")
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
        self._require_nut_control()
        data = self._read_json_body()
        value = data.get("value")
        if not isinstance(value, (str, int, float)) or isinstance(value, bool):
            raise APIBadRequest("value is required (string or number)")
        value = str(value)
        # The value is the only non-allowlisted user input that reaches the NUT
        # CLI; constrain it to a safe charset (defense-in-depth — the call is an
        # execve arg list, not a shell).
        if not _SAFE_NUT_VALUE.match(value):
            raise APIBadRequest("value contains unsupported characters")
        real = self._resolve_ups_name(ups_name)
        if real is None:
            return 404, "application/json", self._not_found("UPS not found")
        nc = self._effective_nut_control(real)
        if variable not in set(nc.allowed_variables):
            self._audit(principal, "variable", f"{real}:{variable}", "denied")
            raise APIForbidden(f"variable {variable!r} is not in allowed_variables")
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

    _AUDIT_EVENT_TYPES = {
        "command": "CONTROL_COMMAND",
        "variable": "CONTROL_VARIABLE",
        "config": "CONFIG_RELOAD",
        "events": "EVENTS_DELETED",
        "self-test": "CONTROL_SELF_TEST",
    }

    @staticmethod
    def _scrub(text: str) -> str:
        """Strip control characters so audit values can't forge log/event lines."""
        return "".join(c for c in str(text)
                       if ord(c) >= 0x20 and ord(c) != 0x7f)

    def _audit(self, principal, kind: str, target: str, result: str) -> None:
        """Record a control action to the daemon log AND the SQLite events table
        (v7.0 adds a tamper-evident audit log; this is the groundwork)."""
        label = self._scrub(self._principal_label(principal))
        target = self._scrub(target)
        line = f"🔌  control: {label} {kind} {target} -> {result}"
        if self.api_log is not None:
            try:
                self.api_log(line)
            except Exception:
                pass
        source = self.api_source
        if hasattr(source, "record_control_event"):
            # target is "{ups}:{command_or_var}"; the UPS NUT name itself can
            # contain a colon (e.g. UPS@host:3493), so split on the LAST one.
            ups = target.rsplit(":", 1)[0] if ":" in target else ""
            event_type = self._AUDIT_EVENT_TYPES.get(kind, "CONTROL")
            try:
                source.record_control_event(ups, event_type,
                                            f"{label} {target} -> {result}")
            except Exception as exc:
                if self.api_log is not None:
                    try:
                        self.api_log(f"⚠️  control audit event failed: {exc}")
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
        # Only advertise endpoints that are actually reachable in the active
        # config — listing a route that returns 404/403 would mislead clients
        # discovering the API via availableEndpoints.
        prometheus_enabled = bool(getattr(self.api_config.prometheus, "enabled", False))
        auth_enabled = self._auth_active()
        nut_enabled = bool(getattr(getattr(self.api_config, "nut_control", None),
                                   "enabled", False))

        def _visible(path: str) -> bool:
            if path == "/metrics":
                return prometheus_enabled
            if path == "/api/v1/auth/state":
                return True
            if path.startswith("/api/v1/auth/") or path == "/api/v1/config/reload":
                return auth_enabled
            if path == "/api/v1/ups/{name}/events":
                return auth_enabled
            if path.startswith("/api/v1/ups/{name}/command") or \
                    path.startswith("/api/v1/ups/{name}/variables"):
                return nut_enabled
            if path == "/api/v1/ups/{name}/self-test":
                # POST self-test needs auth (write) AND nut_control, exactly like
                # the manual control path.
                return auth_enabled and nut_enabled
            return True

        return [dict(e) for e in API_ENDPOINTS if _visible(e["path"])]


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
    # v6.1
    ("eneru_ups_battery_health_score", "gauge",
     "Composite battery-health score 0-100 (omitted when unknown)."),
    ("eneru_ups_replacement_days_remaining", "gauge",
     "Projected days until the battery-health score crosses the replacement "
     "threshold (omitted when not projectable)."),
    ("eneru_ups_energy_kwh", "gauge",
     "Energy consumed in kWh, by period label (today|month). A gauge, NOT a "
     "_total: it is recomputed per window and is not monotonic."),
    ("eneru_ups_energy_cost", "gauge",
     "Energy cost by period label (today|month); omitted entirely when "
     "energy.cost_per_kwh is unset."),
    ("eneru_ups_self_test_result", "gauge",
     "Latest self-test result, one series per normalized result label "
     "(passed|failed|running|unknown|unsupported)."),
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
        # v6.1: battery health, replacement, energy, self-test. Omit a series
        # when its value is unknown rather than emitting a misleading 0.
        bh = row.get("batteryHealth") or {}
        if bh.get("score") is not None:
            lines.append(_metric_line(
                "eneru_ups_battery_health_score", labels, bh["score"]))
        if bh.get("replacementDaysRemaining") is not None:
            lines.append(_metric_line(
                "eneru_ups_replacement_days_remaining", labels,
                bh["replacementDaysRemaining"]))
        energy = row.get("energy") or {}
        for period, kwh_key, cost_key in (("today", "todayKwh", "todayCost"),
                                          ("month", "monthKwh", "monthCost")):
            if energy.get(kwh_key) is not None:
                lines.append(_metric_line(
                    "eneru_ups_energy_kwh", {**labels, "period": period},
                    energy[kwh_key]))
            if energy.get(cost_key) is not None:
                lines.append(_metric_line(
                    "eneru_ups_energy_cost", {**labels, "period": period},
                    energy[cost_key]))
        st = row.get("selfTest") or {}
        if st.get("result"):
            lines.append(_metric_line(
                "eneru_ups_self_test_result",
                {**labels, "result": st["result"]}, 1))
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
