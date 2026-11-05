"""Custom exception handlers enforcing deterministic error schemas and correlation tracing.

Why this module unifies exception handling:
    1. Deterministic Error Schemas: Ensures every API failure returns a uniform JSON contract
       (`error.code`, `error.message`, `error.correlation_id`, and `error.timestamp_utc`).
    2. End-to-End Traceability: Propagates the active `X-Correlation-ID` into both the response
       body and response headers so clients and operators can trace incidents effortlessly.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from grid_screening_agent.logging_config import CORRELATION_ID_VAR, logger


class SolverExecutionError(Exception):
    """Raised when the OpenDSS or Tier-2 physics solver encounters unrecoverable convergence failure."""

    def __init__(self, message: str, network_id: str) -> None:
        self.message = message
        self.network_id = network_id
        super().__init__(self.message)


def register_exception_handlers(app: FastAPI) -> None:
    """Registers global exception handlers on the FastAPI application instance."""

    @app.exception_handler(SolverExecutionError)
    async def solver_execution_error_handler(request: Request, exc: SolverExecutionError) -> JSONResponse:
        corr_id = CORRELATION_ID_VAR.get()
        logger.error(f"Solver execution failed on network {exc.network_id}: {exc.message}")

        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={
                "error": {
                    "code": "SOLVER_EXECUTION_FAILURE",
                    "message": exc.message,
                    "network_id": exc.network_id,
                    "correlation_id": corr_id,
                    "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                }
            },
            headers={"X-Correlation-ID": corr_id},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        corr_id = CORRELATION_ID_VAR.get()
        logger.warning(f"Validation error on {request.url.path}: {exc.errors()}")

        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "detail": exc.errors(),
                "error": {
                    "code": "REQUEST_VALIDATION_ERROR",
                    "message": "The provided connection application payload failed schema validation.",
                    "details": exc.errors(),
                    "correlation_id": corr_id,
                    "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                },
            },
            headers={"X-Correlation-ID": corr_id},
        )

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
        corr_id = CORRELATION_ID_VAR.get()

        return JSONResponse(
            status_code=exc.status_code,
            content={
                "detail": exc.detail,
                "error": {
                    "code": f"HTTP_{exc.status_code}",
                    "message": exc.detail,
                    "correlation_id": corr_id,
                    "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                },
            },
            headers={"X-Correlation-ID": corr_id},
        )

