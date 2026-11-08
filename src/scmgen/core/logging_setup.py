"""Structured logging: human-readable lines on the console, JSON lines in the log file.

Usage:
    log = logging.getLogger(__name__)
    log.info("server checked", extra={"event": "db.check", "version": "8.4.3"})

Anything passed in `extra` appears as its own JSON field, which keeps logs
machine-searchable (e.g. by event name or workspace code).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from scmgen.core.settings import LoggingSettings

ROOT_LOGGER = "scmgen"

# Attributes every LogRecord has; anything else came from `extra=`.
_STANDARD_ATTRS = set(vars(logging.makeLogRecord({}))) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created).astimezone().isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in vars(record).items():
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def setup_logging(settings: LoggingSettings) -> logging.Logger:
    """Configure the 'scmgen' logger tree. Safe to call more than once."""
    logger = logging.getLogger(ROOT_LOGGER)
    logger.setLevel(settings.level)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    settings.file.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(settings.file, encoding="utf-8")
    file_handler.setFormatter(JsonFormatter())
    logger.addHandler(file_handler)

    if settings.console:
        console = logging.StreamHandler()
        console.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S"))
        logger.addHandler(console)
    return logger
