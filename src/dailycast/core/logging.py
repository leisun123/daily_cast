"""Structured console logging and request correlation."""

import json
import logging
from contextvars import ContextVar, Token
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

request_id_context: ContextVar[str] = ContextVar("request_id", default="-")


def set_request_id(request_id: str) -> Token[str]:
    """Set the request ID for the current context and return its reset token."""
    return request_id_context.set(request_id)


def reset_request_id(token: Token[str]) -> None:
    """Restore the previous request ID context."""
    request_id_context.reset(token)


def get_request_id() -> str:
    """Return the request ID currently associated with this execution context."""
    return request_id_context.get()


class JsonFormatter(logging.Formatter):
    """Serialize standard log records to a compact JSON console line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": get_request_id(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


class TaskJsonFormatter(JsonFormatter):
    """Preserve optional generic task correlation fields when callers provide them."""

    def format(self, record: logging.LogRecord) -> str:
        payload = json.loads(super().format(record))
        for field_name in ("task_id", "task_step"):
            value = getattr(record, field_name, None)
            if value is not None:
                payload[field_name] = value
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(
    level: str,
    *,
    file_path: Path | None = None,
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 3,
) -> None:
    """Configure the root logger once with JSON output to standard error.

    When ``file_path`` is set, the same JSON stream is mirrored to a rotating
    file so deployments whose platform log-query API is unavailable can still
    inspect past runs through container exec.
    """
    root_logger = logging.getLogger()
    root_logger.setLevel(level.upper())
    handler = logging.StreamHandler()
    handler.setFormatter(TaskJsonFormatter())
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    if file_path is None:
        return
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        file_path,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(TaskJsonFormatter())
    root_logger.addHandler(file_handler)
