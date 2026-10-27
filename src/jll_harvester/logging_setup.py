"""Structured logging: human-readable key=value lines, or JSON lines with ``IRIS_LOG_FORMAT=json``."""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import UTC, datetime

_STANDARD = set(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {"message", "asctime"}


class _Formatter(logging.Formatter):
    def __init__(self, as_json: bool) -> None:
        super().__init__()
        self.as_json = as_json

    def format(self, record: logging.LogRecord) -> str:
        extras = {k: v for k, v in record.__dict__.items() if k not in _STANDARD}
        ts = datetime.fromtimestamp(record.created, UTC).isoformat(timespec="seconds")
        if self.as_json:
            payload = {
                "ts": ts,
                "level": record.levelname.lower(),
                "logger": record.name,
                "msg": record.getMessage(),
                **extras,
            }
            if record.exc_info:
                payload["exc"] = self.formatException(record.exc_info)
            return json.dumps(payload, ensure_ascii=False, default=str)
        kv = " ".join(f"{k}={v}" for k, v in extras.items())
        line = f"{ts} {record.levelname:<7} {record.name}: {record.getMessage()}" + (f"  {kv}" if kv else "")
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


def configure(level: str | None = None) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_Formatter(os.getenv("IRIS_LOG_FORMAT", "text").lower() == "json"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel((level or os.getenv("IRIS_LOG_LEVEL") or "INFO").upper())
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
