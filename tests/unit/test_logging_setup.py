from __future__ import annotations

import json
import logging
from pathlib import Path

from scmgen.core.logging_setup import setup_logging
from scmgen.core.settings import LoggingSettings


def test_file_log_is_json_with_extra_fields(tmp_path: Path) -> None:
    log_file = tmp_path / "logs" / "scmgen.log"
    setup_logging(LoggingSettings(level="INFO", file=log_file, console=False))

    logging.getLogger("scmgen.test").info("hello", extra={"event": "test.event", "rows": 3})
    logging.getLogger("scmgen.test").debug("not written at INFO level")
    for handler in logging.getLogger("scmgen").handlers:
        handler.flush()

    lines = log_file.read_text().strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["message"] == "hello"
    assert record["level"] == "INFO"
    assert record["event"] == "test.event"
    assert record["rows"] == 3


def test_setup_is_idempotent(tmp_path: Path) -> None:
    settings = LoggingSettings(level="INFO", file=tmp_path / "x.log", console=True)
    setup_logging(settings)
    setup_logging(settings)
    assert len(logging.getLogger("scmgen").handlers) == 2  # file + console, not duplicated
