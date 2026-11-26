# SPDX-License-Identifier: Apache-2.0
"""Structured logging: human-readable on stderr, JSON lines in a size-capped rotating file.

Pipeline stages log an ``event`` with timing fields. The JSON log is how we prove the latency
and capacity targets.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import time
from pathlib import Path
from typing import Any

LOG_FILE_MAX_BYTES = 10 * 1024 * 1024
LOG_FILE_BACKUPS = 5  # disk use is capped at ~60 MB


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": round(record.created, 3),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        fields = getattr(record, "fields", None)
        if fields:
            entry.update(fields)
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False, default=str)


class ConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        stamp = time.strftime("%H:%M:%S", time.localtime(record.created))
        text = f"{stamp} {record.levelname.lower():7} {record.name}: {record.getMessage()}"
        fields = getattr(record, "fields", None)
        if fields:
            text += " " + " ".join(f"{k}={v}" for k, v in fields.items())
        if record.exc_info:
            text += "\n" + self.formatException(record.exc_info)
        return text


def setup_logging(log_dir: Path | None, *, console_level: int = logging.WARNING) -> None:
    root = logging.getLogger("wavescribe")
    root.setLevel(logging.DEBUG)
    root.handlers.clear()
    root.propagate = False

    console = logging.StreamHandler()
    console.setLevel(console_level)
    console.setFormatter(ConsoleFormatter())
    root.addHandler(console)

    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_dir / "wavescribe.jsonl",
            maxBytes=LOG_FILE_MAX_BYTES,
            backupCount=LOG_FILE_BACKUPS,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.INFO)
        file_handler.setFormatter(JsonFormatter())
        root.addHandler(file_handler)


def event(logger: logging.Logger, name: str, level: int = logging.INFO, **fields: Any) -> None:
    """Log a structured event; ``fields`` become top-level keys in the JSON log."""
    logger.log(level, name, extra={"fields": fields})
