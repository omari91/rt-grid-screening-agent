"""Structured JSON logging and request-scoped correlation ID tracking for FastAPI.

Why this module uses `contextvars.ContextVar`:
    FastAPI handles concurrent requests across the asyncio event loop. Using standard
    thread-local or global variables causes correlation IDs to bleed across concurrent
    requests. `ContextVar` securely isolates the `correlation_id` to the asynchronous
    task lifecycle of each individual HTTP request.
"""

from __future__ import annotations

import json
import logging
import time
from contextvars import ContextVar
from typing import Any, Callable
from uuid import uuid4

from fastapi import Request, Response

CORRELATION_ID_VAR: ContextVar[str] = ContextVar("correlation_id", default="system-init")


class JSONLogFormatter(logging.Formatter):
    """Formats log records into structured JSON lines including correlation IDs and ISO timestamps."""

    def format(self, record: logging.LogRecord) -> str:
        log_data: dict[str, Any] = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "correlation_id": CORRELATION_ID_VAR.get(),
            "message": record.getMessage(),
        }
        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_data)


def setup_root_logger() -> logging.Logger:
    """Configures the root logger with the structured JSON formatter."""
    app_logger = logging.getLogger("grid_screening_agent")
    app_logger.setLevel(logging.INFO)
    if not any(isinstance(h.formatter, JSONLogFormatter) for h in app_logger.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(JSONLogFormatter())
        app_logger.handlers = [handler]
    return app_logger


logger = setup_root_logger()


async def correlation_id_middleware(request: Request, call_next: Callable[[Request], Any]) -> Response:
    """Extract incoming X-Correlation-ID header or generate a new UUID4, binding it to contextvars."""
    incoming_id = request.headers.get("X-Correlation-ID", "").strip()
    correlation_id = incoming_id if incoming_id else str(uuid4())

    token = CORRELATION_ID_VAR.set(correlation_id)
    t0 = time.perf_counter()

    try:
        logger.info(f"Incoming {request.method} request to {request.url.path}")
        response: Response = await call_next(request)
    except Exception as exc:
        logger.error(f"Unhandled exception during request processing: {exc}", exc_info=True)
        raise
    finally:
        duration_ms = (time.perf_counter() - t0) * 1000.0
        status_code = getattr(response, "status_code", 500) if "response" in locals() else 500
        logger.info(
            f"Completed {request.method} {request.url.path} with status {status_code} in {duration_ms:.2f}ms"
        )
        CORRELATION_ID_VAR.reset(token)

    response.headers["X-Correlation-ID"] = correlation_id
    return response

