"""Structured console logging and request correlation."""

import json
import logging
from contextvars import ContextVar, Token
from datetime import UTC, datetime
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


def configure_logging(level: str) -> None:
    """Configure the root logger once with JSON output to standard error."""
    root_logger = logging.getLogger()
    root_logger.setLevel(level.upper())
    handler = logging.StreamHandler()
    handler.setFormatter(TaskJsonFormatter())
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
