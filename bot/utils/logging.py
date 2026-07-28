"""Logging setup: rotating files plus console, text or JSON.

Three sinks are configured:

* ``stdout``    — what ``docker logs`` shows.
* ``app.log``   — everything at the configured level, rotated by size.
* ``error.log`` — WARNING and above only, so incidents are easy to find.

Rotation is size-based rather than time-based: a scraping or import burst can
produce more output in an hour than a quiet week, and size caps are what
actually protect a small VPS disk.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_TEXT_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)-28s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

#: Attributes present on every LogRecord; anything else was added by the caller
#: via ``extra=`` and is therefore worth emitting in the JSON payload.
_RESERVED_RECORD_ATTRS = frozenset(
    {
        "args", "asctime", "created", "exc_info", "exc_text", "filename",
        "funcName", "levelname", "levelno", "lineno", "message", "module",
        "msecs", "msg", "name", "pathname", "process", "processName",
        "relativeCreated", "stack_info", "taskName", "thread", "threadName",
    }
)

#: Third-party loggers that are chatty at DEBUG and rarely useful.
_NOISY_LOGGERS = (
    "aiogram.event",
    "aiohttp.access",
    "apscheduler.executors.default",
    "asyncio",
    "sqlalchemy.engine.Engine",
)


class JsonFormatter(logging.Formatter):
    """Render records as one JSON object per line for log shippers."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)

        for key, value in record.__dict__.items():
            if key not in _RESERVED_RECORD_ATTRS and not key.startswith("_"):
                payload[key] = value if isinstance(value, str | int | float | bool | None) else repr(value)

        return json.dumps(payload, ensure_ascii=False, default=str)


def _build_formatter(log_format: str) -> logging.Formatter:
    if log_format == "json":
        return JsonFormatter()
    return logging.Formatter(fmt=_TEXT_FORMAT, datefmt=_DATE_FORMAT)


def _build_file_handler(
    path: Path,
    level: int,
    formatter: logging.Formatter,
    max_bytes: int,
    backup_count: int,
) -> logging.Handler:
    handler = logging.handlers.RotatingFileHandler(
        filename=path,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
        delay=True,
    )
    handler.setLevel(level)
    handler.setFormatter(formatter)
    return handler


def setup_logging(
    *,
    level: str = "INFO",
    log_dir: Path,
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 10,
    log_format: str = "text",
) -> None:
    """Configure the root logger. Safe to call more than once.

    Args:
        level: Minimum level for stdout and ``app.log``.
        log_dir: Directory to write log files into; created if absent.
        max_bytes: Size at which a log file is rotated.
        backup_count: How many rotated files to keep.
        log_format: ``"text"`` for humans, ``"json"`` for log shippers.
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    numeric_level = logging.getLevelNamesMapping().get(level.upper(), logging.INFO)
    formatter = _build_formatter(log_format)

    root = logging.getLogger()
    root.setLevel(numeric_level)

    # Replace existing handlers so repeated calls do not duplicate every line.
    for existing in list(root.handlers):
        root.removeHandler(existing)
        existing.close()

    console = logging.StreamHandler(stream=sys.stdout)
    console.setLevel(numeric_level)
    console.setFormatter(formatter)
    root.addHandler(console)

    root.addHandler(
        _build_file_handler(
            log_dir / "app.log", numeric_level, formatter, max_bytes, backup_count
        )
    )
    root.addHandler(
        _build_file_handler(
            log_dir / "error.log", logging.WARNING, formatter, max_bytes, backup_count
        )
    )

    for noisy in _NOISY_LOGGERS:
        logging.getLogger(noisy).setLevel(max(numeric_level, logging.WARNING))

    logging.captureWarnings(True)


def get_logger(name: str) -> logging.Logger:
    """Return a module-scoped logger.

    Call as ``get_logger(__name__)`` so the emitting module is visible in every
    line without hand-maintained prefixes.
    """
    return logging.getLogger(name)
