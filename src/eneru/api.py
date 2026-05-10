"""Embedded read-only HTTP API and Prometheus endpoint."""

import json
import math
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import parse_qs, unquote, urlparse

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


class APIBadRequest(ValueError):
    """Raised when a client supplies invalid API query parameters."""


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

    def start(self) -> None:
        """Start the API server when enabled."""
        if not self.config.api.enabled or self._thread is not None:
            return

        source = self.source
        config = self.config

        class Handler(EneruAPIHandler):
            api_source = source
            api_config = config

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
        # The API ships with no auth in v5.3 by design (read-only,
        # localhost-by-default). If the user opted in to a non-loopback
        # bind, surface that /api/v1/config exposes server hostnames,
        # SSH-options-configured flags, and pre-shutdown command
        # templates to anyone who can reach this socket.
        if not _is_loopback_bind(self.config.api.bind):
            self.log_fn(
                f"⚠️ API bound to {addr[0]} (non-loopback). v5.3 ships "
                f"no authentication; /api/v1/config will expose remote "
                f"server hostnames, SSH usernames, shutdown ordering, "
                f"and presence flags for SSH options and pre-shutdown "
                f"commands to any client that can reach this socket. "
                f"Restrict network access (firewall, reverse proxy with "
                f"auth) before exposing this beyond trusted hosts."
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
    """Request handler for the read-only v5.3 API."""

    api_source: Any = None
    api_config: Any = None

    server_version = "EneruAPI/1.0"

    def do_GET(self):  # noqa: N802 - stdlib hook
        try:
            status, content_type, body = self._route()
        except APIBadRequest as exc:
            status, content_type, body = (
                400, "application/json",
                self._error("INVALID_REQUEST", str(exc)),
            )
        except Exception:
            status, content_type, body = (
                500, "application/json",
                {"error": {"code": "INTERNAL_ERROR", "message": "Internal server error"}},
            )
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
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, fmt, *args):  # noqa: A003 - stdlib hook
        return

    def _route(self) -> Tuple[int, str, Any]:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        qs = parse_qs(parsed.query)

        if path == "/health":
            return 200, "application/json", {"status": "ok", "generatedAt": time.time()}

        if path == "/ready":
            payload = readiness(self.api_source)
            return (200 if payload["ready"] else 503), "application/json", payload

        if path == "/metrics":
            if not self.api_config.prometheus.enabled:
                return 404, "application/json", self._error("NOT_FOUND", "Metrics disabled")
            return 200, "text/plain", render_prometheus_metrics(self.api_source)

        if path == "/api/v1/ups":
            return 200, "application/json", collect_status(self.api_source)

        if path.startswith("/api/v1/ups/"):
            parts = path.split("/")
            ups_name = unquote(parts[4]) if len(parts) > 4 else ""
            if len(parts) == 5:
                payload = collect_status(self.api_source)
                row = find_status(payload, ups_name)
                if row is None:
                    return 404, "application/json", self._error("NOT_FOUND", "UPS not found")
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
                    return 404, "application/json", self._error("NOT_FOUND", "UPS not found")
                return 200, "application/json", {
                    "ups": ups_name, "metric": metric, "from": start,
                    "to": end, "data": rows,
                }

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
            return 200, "application/json", config_summary(self.api_config)

        if path == "/api/v1/remote-health":
            rows = live_remote_health(self.api_source, self.api_config)
            return 200, "application/json", {"generatedAt": time.time(), "servers": rows}

        return 404, "application/json", self._error("NOT_FOUND", "Endpoint not found")

    @staticmethod
    def _error(code: str, message: str) -> Dict[str, Dict[str, str]]:
        return {"error": {"code": code, "message": message}}


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
