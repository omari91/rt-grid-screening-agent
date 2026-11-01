"""Prometheus metrics exposition and request latency instrumentation for FastAPI.

Why this module monitors solver stability and queue health:
    Provides real-time telemetry for Kubernetes `ServiceMonitor` scrapers, triggering
    Prometheus alerting rules when OpenDSS solver failure rates spike, P99 latency
    degrades above 500ms, or interconnection queue backlog exceeds SLA thresholds.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from fastapi import FastAPI, Request, Response

try:
    from prometheus_client import (  # type: ignore[import-not-found]
        CONTENT_TYPE_LATEST,
        Counter,
        Gauge,
        Histogram,
        generate_latest,
    )

    PROMETHEUS_AVAILABLE = True
except ModuleNotFoundError:  # pragma: no cover - fallback when running in minimal environments
    PROMETHEUS_AVAILABLE = False
    CONTENT_TYPE_LATEST = "text/plain; version=0.0.4; charset=utf-8"

    class _DummyMetric:
        def labels(self, *args: Any, **kwargs: Any) -> "_DummyMetric":
            return self

        def inc(self, amount: float = 1.0) -> None:
            pass

        def set(self, value: float) -> None:
            pass

        def observe(self, amount: float) -> None:
            pass

    Histogram = Gauge = Counter = lambda *args, **kwargs: _DummyMetric()  # type: ignore[assignment,misc]
    generate_latest = lambda: b"# Prometheus client not installed in environment\n"  # type: ignore[assignment]


HTTP_REQUEST_DURATION = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency histogram for screening endpoints",
    ["method", "endpoint"],
    buckets=[0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0],
)

SOLVER_FAILURES_TOTAL = Counter(
    "grid_opendss_solver_failures_total",
    "Total count of Tier-2 OpenDSS solver convergence or execution failures",
    ["network_id"],
)

QUEUE_BACKLOG_GAUGE = Gauge(
    "grid_queue_backlog_pending_total",
    "Current number of pending grid interconnection applications waiting in queue",
)

SCREENING_REQUESTS_TOTAL = Counter(
    "grid_screening_requests_total",
    "Total screening requests processed by tier and status",
    ["tier", "status"],
)


async def prometheus_middleware(request: Request, call_next: Callable[[Request], Any]) -> Response:
    """Middleware to automatically measure and record HTTP request duration and status."""
    start_time = time.perf_counter()
    response = Response(status_code=500)

    try:
        response = await call_next(request)
    finally:
        duration = time.perf_counter() - start_time
        endpoint = request.url.path
        if not endpoint.startswith("/metrics"):
            HTTP_REQUEST_DURATION.labels(method=request.method, endpoint=endpoint).observe(duration)

    return response


def setup_metrics_endpoints(app: FastAPI) -> None:
    """Registers middleware and the /metrics Prometheus scrape endpoint on the FastAPI app."""
    app.middleware("http")(prometheus_middleware)

    @app.get("/metrics", include_in_schema=False)
    async def metrics_endpoint() -> Response:
        """Exposes Prometheus text exposition format for scraper collection."""
        return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)

