"""Structured (JSON lines) logging on top of the stdlib ``logging`` module.

Usage::

    log = get_logger(__name__)
    log.info("ws_connected", route="market", streams=100)
"""

from __future__ import annotations

import logging
import sys
import time
from typing import Any

import orjson

_RESERVED = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        doc: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        fields = getattr(record, "fields", None)
        if fields:
            doc.update(fields)
        if record.exc_info:
            doc["exc"] = self.formatException(record.exc_info)
        return orjson.dumps(doc, default=str).decode()


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = f"{self.formatTime(record, '%H:%M:%S')} {record.levelname:<7} {record.name}: {record.getMessage()}"
        fields = getattr(record, "fields", None)
        if fields:
            base += " " + " ".join(f"{k}={v}" for k, v in fields.items())
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)
        return base


class StructLogger:
    __slots__ = ("_log",)

    def __init__(self, logger: logging.Logger) -> None:
        self._log = logger

    def _emit(self, level: int, event: str, exc_info: bool, fields: dict[str, Any]) -> None:
        if self._log.isEnabledFor(level):
            self._log.log(level, event, exc_info=exc_info, extra={"fields": fields}, stacklevel=3)

    def debug(self, event: str, **fields: Any) -> None:
        self._emit(logging.DEBUG, event, False, fields)

    def info(self, event: str, **fields: Any) -> None:
        self._emit(logging.INFO, event, False, fields)

    def warning(self, event: str, **fields: Any) -> None:
        self._emit(logging.WARNING, event, False, fields)

    def error(self, event: str, **fields: Any) -> None:
        self._emit(logging.ERROR, event, False, fields)

    def exception(self, event: str, **fields: Any) -> None:
        self._emit(logging.ERROR, event, True, fields)

    def is_debug(self) -> bool:
        return self._log.isEnabledFor(logging.DEBUG)


def get_logger(name: str) -> StructLogger:
    return StructLogger(logging.getLogger(name))


def setup_logging(level: str = "INFO", json: bool = True, file: str | None = None) -> None:
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    fmt: logging.Formatter = JsonFormatter() if json else TextFormatter()
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if file:
        from pathlib import Path

        Path(file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(file, encoding="utf-8"))
    for h in handlers:
        h.setFormatter(fmt)
        root.addHandler(h)
    root.setLevel(level.upper())
    # third-party chatter
    for noisy in ("websockets", "httpx", "httpcore", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
