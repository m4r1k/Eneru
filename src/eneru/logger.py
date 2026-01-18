"""Logging utilities for Eneru."""

import logging
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


class UPSLogger:
    """Custom logger that handles both file and console output."""

    def __init__(self, log_file: Optional[str], config: Config):
        self.log_file = Path(log_file) if log_file else None
        self.config = config
        self.logger = logging.getLogger("ups-monitor")
        self.logger.setLevel(logging.INFO)

        if self.logger.handlers:
            self.logger.handlers.clear()

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

    def log(self, message: str):
        """Log a message with timezone info."""
        self.logger.info(message)
