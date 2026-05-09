"""Logging utilities for Eneru."""

import json
import logging
import logging.handlers
import sys
import time
from pathlib import Path
from typing import Optional

from eneru.config import Config


class TimezoneFormatter(logging.Formatter):
    """Custom formatter that includes timezone abbreviation."""

    def format(self, record):
        record.timezone = time.strftime('%Z')
        return super().format(record)


class JSONFormatter(logging.Formatter):
    """Minimal structured formatter for SIEM/log pipeline ingestion."""

    def format(self, record):
        message = record.getMessage()
        payload = {
            "timestamp": time.strftime(
                "%Y-%m-%dT%H:%M:%S%z",
                time.localtime(record.created),
            ),
            "level": record.levelname,
            "logger": record.name,
            "message": message,
        }
        if "POWER EVENT:" in message:
            payload["category"] = "power_event"
            try:
                event_part = message.split("POWER EVENT:", 1)[1].strip()
                payload["event_type"] = event_part.split(" ", 1)[0]
            except Exception:
                pass
        elif "Remote health" in message:
            payload["category"] = "health"
        elif "SHUTDOWN" in message or "shutdown" in message:
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

    def log(self, message: str):
        """Log a message with timezone info."""
        self.logger.info(message)
