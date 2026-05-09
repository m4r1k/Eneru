"""Logging utilities for Eneru."""

import json
import logging
import logging.handlers
import re
import sys
import time
from pathlib import Path
from typing import Optional

from eneru.config import Config


SENSITIVE_PATTERNS = (
    re.compile(r"(discord://)[^\s]+", re.IGNORECASE),
    re.compile(r"(https://discord(?:app)?\.com/api/webhooks/)[^\s]+", re.IGNORECASE),
    re.compile(r"([a-z][a-z0-9+.-]*://)([^:/\s]+):([^@\s]+)@", re.IGNORECASE),
    re.compile(r"((?:token|password|passwd|secret|api[_-]?key)=)[^\s&]+", re.IGNORECASE),
)


def redact_sensitive_text(value: str) -> str:
    """Redact credentials from log text before structured output."""
    redacted = value
    redacted = SENSITIVE_PATTERNS[0].sub(r"\1<redacted>", redacted)
    redacted = SENSITIVE_PATTERNS[1].sub(r"\1<redacted>", redacted)
    redacted = SENSITIVE_PATTERNS[2].sub(r"\1\2:<redacted>@", redacted)
    redacted = SENSITIVE_PATTERNS[3].sub(r"\1<redacted>", redacted)
    return redacted


class TimezoneFormatter(logging.Formatter):
    """Custom formatter that includes timezone abbreviation."""

    def format(self, record):
        record.timezone = time.strftime('%Z')
        return super().format(record)


class JSONFormatter(logging.Formatter):
    """Minimal structured formatter for SIEM/log pipeline ingestion.

    Prefers explicit ``extra={...}`` fields on the LogRecord (set by
    callers via ``logger.log(msg, extra={"category": "shutdown", ...})``).
    Falls back to heuristic message parsing for legacy call sites that
    don't pass structured context — these are correctness-best-effort
    and should be migrated over time.
    """

    # The set of fields a caller may opt into via the ``extra`` kwarg of
    # ``logging.Logger`` calls. Anything else is ignored to keep the
    # JSON shape stable for log pipelines.
    _EXTRA_FIELDS = ("group", "event_type", "category", "ups", "server")

    def format(self, record):
        raw_message = record.getMessage()
        message = redact_sensitive_text(raw_message)
        payload = {
            "timestamp": time.strftime(
                "%Y-%m-%dT%H:%M:%S%z",
                time.localtime(record.created),
            ),
            "level": record.levelname,
            "logger": record.name,
            "message": message,
        }
        # Pull explicit structured fields first. The Python logging
        # module promotes ``extra={...}`` keys into LogRecord attributes,
        # so we read them directly off the record.
        for field in self._EXTRA_FIELDS:
            value = getattr(record, field, None)
            if value is not None and value != "":
                payload[field] = value
        # Fallback heuristics for call sites that don't yet pass
        # structured extras. These run only to fill in fields the
        # caller didn't already provide.
        if "group" not in payload and message.startswith("[") and "] " in message:
            group, rest = message.split("] ", 1)
            payload["group"] = group.strip("[]")
            payload["message"] = rest
        if "category" not in payload:
            if "POWER EVENT:" in message:
                payload["category"] = "power_event"
                if "event_type" not in payload:
                    try:
                        event_part = message.split("POWER EVENT:", 1)[1].strip()
                        payload["event_type"] = event_part.split(" ", 1)[0]
                    except Exception:
                        pass
            elif "Remote health" in message:
                payload["category"] = "health"
            elif "SHUTDOWN" in message:
                # Uppercase SHUTDOWN is the daemon's own marker. Avoid
                # the lowercase variant — it falsely tags config keys
                # like "shutdown_safety_margin" and informational lines
                # like "Local shutdown disabled" as shutdown events.
                payload["category"] = "shutdown"
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)


class UPSLogger:
    """Custom logger that handles both file and console output."""

    def __init__(self, log_file: Optional[str], config: Config):
        self.log_file = Path(log_file) if log_file else None
        self.config = config
        self.logger = logging.getLogger("ups-monitor")
        self.logger.setLevel(logging.INFO)

        # Iterate-and-close so file descriptors held by previously
        # registered FileHandlers are released. Bare `.handlers.clear()`
        # leaks the underlying file objects when re-init runs against
        # a logger that had a FileHandler attached.
        for h in list(self.logger.handlers):
            self.logger.removeHandler(h)
            try:
                h.close()
            except Exception:
                pass
        # Don't propagate to the root logger — Eneru manages its own
        # console + file output; propagation would duplicate every line
        # if the embedding application configured root handlers.
        self.logger.propagate = False

        if config.logging.format == "json":
            formatter = JSONFormatter()
        else:
            formatter = TimezoneFormatter(
                '%(asctime)s %(timezone)s - %(message)s',
                datefmt='%Y-%m-%d %H:%M:%S'
            )

        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(formatter)
        self.logger.addHandler(console_handler)

        if self.log_file:
            try:
                self.log_file.parent.mkdir(parents=True, exist_ok=True)
                file_handler = logging.FileHandler(self.log_file)
                file_handler.setFormatter(formatter)
                self.logger.addHandler(file_handler)
            except PermissionError:
                print(f"Warning: Cannot write to {self.log_file}, logging to console only")

        if config.logging.syslog.enabled:
            try:
                address = config.logging.syslog.address
                if not str(address).startswith("/"):
                    address = (address, int(config.logging.syslog.port))
                syslog_handler = logging.handlers.SysLogHandler(
                    address=address,
                    facility=config.logging.syslog.facility,
                )
                syslog_handler.setFormatter(formatter)
                self.logger.addHandler(syslog_handler)
            except Exception as exc:
                print(f"Warning: Cannot initialize syslog forwarding: {exc}")

    def log(self, message: str, **extra):
        """Log a message with timezone info.

        Optional ``**extra`` keyword arguments (e.g. ``category="shutdown"``,
        ``group="ups0"``, ``event_type="OB"``) are forwarded to the
        underlying ``logging.Logger`` as structured fields. The text
        formatter ignores them; ``JSONFormatter`` uses them in
        preference to its message-text heuristics. See
        ``JSONFormatter._EXTRA_FIELDS`` for the recognised keys.
        """
        if extra:
            self.logger.info(message, extra=extra)
        else:
            self.logger.info(message)
