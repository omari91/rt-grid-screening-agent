"""Unit tests verifying structured exception handling, error contracts, and correlation tracing."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field

from grid_screening_agent.exceptions import SolverExecutionError, register_exception_handlers
from grid_screening_agent.logging_config import correlation_id_middleware


class MockApplicationPayload(BaseModel):
    network_id: str = Field(..., min_length=2)
    capacity_kw: float = Field(..., gt=0)


@pytest.fixture
def test_app() -> FastAPI:
    """Build a temporary FastAPI app wired with correlation middleware and exception handlers."""
    app = FastAPI()

    # Register middleware and exception handlers under test
    app.middleware("http")(correlation_id_middleware)
    register_exception_handlers(app)

    @app.get("/test/solver-error")
    def trigger_solver_error() -> None:
        raise SolverExecutionError("Newton-Raphson Jacobian matrix singular", network_id="R")

    @app.post("/test/validation-error")
    def trigger_validation(payload: MockApplicationPayload) -> dict[str, str]:
        return {"status": "success"}

    return app


@pytest.fixture
def client(test_app: FastAPI) -> TestClient:
    return TestClient(test_app)


def test_solver_execution_error_propagates_correlation_id(client: TestClient) -> None:
    """Validates that custom SolverExecutionError returns 502 and echoes correlation ID."""
    custom_corr_id = "corr-test-solver-9988"
    response = client.get(
        "/test/solver-error",
        headers={"X-Correlation-ID": custom_corr_id},
    )

    assert response.status_code == 502
    data = response.json()

    # Check error payload structure
    assert data["error"]["code"] == "SOLVER_EXECUTION_FAILURE"
    assert data["error"]["network_id"] == "R"
    assert data["error"]["correlation_id"] == custom_corr_id
    assert "timestamp_utc" in data["error"]

    # Check response header propagation
    assert response.headers.get("X-Correlation-ID") == custom_corr_id


def test_request_validation_error_returns_structured_details(client: TestClient) -> None:
    """Validates that Pydantic validation failures return 422 with structured error details."""
    response = client.post(
        "/test/validation-error",
        json={"network_id": "A", "capacity_kw": -10.0},  # Invalid: capacity must be > 0
    )

    assert response.status_code == 422
    data = response.json()

    assert data["error"]["code"] == "REQUEST_VALIDATION_ERROR"
    assert "details" in data["error"]
    assert len(data["error"]["details"]) > 0
    assert "correlation_id" in data["error"]
    assert "X-Correlation-ID" in response.headers


def test_auto_generated_correlation_id_when_missing(client: TestClient) -> None:
    """Validates that a UUID4 correlation ID is automatically generated if none is provided."""
    response = client.get("/test/solver-error")

    assert response.status_code == 502
    data = response.json()

    # Verify fallback correlation ID exists and matches header
    returned_corr_id = data["error"]["correlation_id"]
    assert returned_corr_id is not None
    assert len(returned_corr_id) > 5
    assert response.headers.get("X-Correlation-ID") == returned_corr_id

