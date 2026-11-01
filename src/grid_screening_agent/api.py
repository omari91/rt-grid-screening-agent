"""FastAPI REST service & Interconnection Queue Compliance Dashboard for grid connection screening.

Why this service couples REST JSON endpoints with state-compression queue tracking:
    Provides DNSP/TSO portals and developer frontends (`POST /v1/screen`,
    `POST /v1/screen/intake`, `POST /v1/screen/plot`) alongside a zero-dependency
    operator control room (`GET /dashboard`, `GET /v1/queue`) that tracks fast-track
    clearances, intercepted duplicate backlog requests, and SHA-256 audit receipts.
"""

from __future__ import annotations

import argparse
import hmac
import logging
import os
import urllib.parse
from typing import Any, Literal

import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field, ValidationError

from grid_screening_agent import __version__
from grid_screening_agent.agent import GridScreeningAgent
from grid_screening_agent.dashboard import render_compliance_dashboard_html
from grid_screening_agent.exceptions import register_exception_handlers
from grid_screening_agent.logging_config import correlation_id_middleware, logger
from grid_screening_agent.metrics import setup_metrics_endpoints
from grid_screening_agent.models import (
    AssetType,
    ConnectionApplication,
    InverterMode,
    ScreeningVerdict,
    StudyType,
)
from grid_screening_agent.physics.topology import list_supported_networks
from grid_screening_agent.plotting import render_verdict_svg
from grid_screening_agent.queue_cache import INTERCONNECTION_QUEUE

ProviderChoice = Literal["auto", "deterministic", "gemini", "anthropic", "openai"]

app = FastAPI(
    title="Grid Connection Screening Agent API",
    version=__version__,
    description=(
        "Progressive-fidelity LV/MV/HV connection screening agent backed by isolated "
        "OpenDSS unbalanced 3-phase AC power flow, Constraint-Decoupled Dominance routing, "
        "and a deterministic Supervisory Physics Sanity Gate."
    ),
)

_cors_origins = [
    origin.strip()
    for origin in os.environ.get("GRID_AGENT_CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000").split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "X-Correlation-ID"],
    expose_headers=["X-Correlation-ID"],
)
app.middleware("http")(correlation_id_middleware)
setup_metrics_endpoints(app)
register_exception_handlers(app)


@app.middleware("http")
async def _zero_trust_bearer_auth_middleware(request: Request, call_next: Any) -> Response:
    """Enforce optional host-held Bearer token authentication when `GRID_AGENT_API_TOKEN` is set.

    Why credentials live strictly in this host middleware:
        Tokens are verified in constant time (`hmac.compare_digest`) at the HTTP boundary
        and are never forwarded into LLM prompts, tool traces, or child OpenDSS workers.
        `/health` remains open for container orchestrator liveness probes.
    """
    expected_token = os.environ.get("GRID_AGENT_API_TOKEN", "").strip()
    if expected_token and request.method != "OPTIONS" and request.url.path != "/health":
        auth_header = request.headers.get("Authorization", "")
        scheme, _, supplied = auth_header.partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(supplied.strip(), expected_token):
            return JSONResponse(
                status_code=401,
                content={
                    "detail": {
                        "error_code": "UNAUTHORIZED",
                        "message": "Valid Bearer token required by host zero-trust policy.",
                    }
                },
                headers={"WWW-Authenticate": "Bearer"},
            )
    return await call_next(request)


def _format_validation_error_detail(exc: ValidationError) -> dict[str, Any]:
    """Format Pydantic cross-field/schema violations into an auditable HTTP 422 payload."""
    return {
        "error_code": "CONSTRAINT_VIOLATION",
        "message": "Extraction yielded physically invalid engineering parameters.",
        "unresolved_constraints": [
            {
                "field": [str(part) for part in err.get("loc", ())],
                "violation": err.get("msg", ""),
            }
            for err in exc.errors()
        ],
    }


class NaturalLanguageScreeningRequest(BaseModel):
    inquiry: str = Field(
        ...,
        min_length=5,
        max_length=2000,
        description="Free-text engineering connection request (e.g., 'Screen 25 kW PV + 15 kW BESS on Network R at 6x solar scale').",
    )
    provider: ProviderChoice = Field(
        default="auto",
        description="Orchestration provider ('auto', 'deterministic', 'gemini', 'anthropic', 'openai').",
    )
    study_type: StudyType = Field(
        default=StudyType.SNAPSHOT,
        description="Tier-2 AC study scope ('snapshot' vs 'time_series').",
    )
    force_tier2: bool = Field(
        default=False,
        description="Force Tier-2 AC power-flow verification even if Tier-1 clears fast-track.",
    )


class StructuredScreeningRequest(BaseModel):
    application: ConnectionApplication
    provider: ProviderChoice = Field(default="auto")
    force_tier2: bool = Field(default=False)


@app.get("/health")
def health_check() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


@app.get("/v1/networks")
def get_networks() -> dict[str, Any]:
    return list_supported_networks()


@app.get("/v1/queue")
def get_queue_stats() -> dict[str, Any]:
    """Return live interconnection queue backlog reduction metrics and recent SHA-256 audit trail."""
    return INTERCONNECTION_QUEUE.snapshot_stats()


@app.get("/dashboard", response_class=HTMLResponse)
def get_compliance_dashboard() -> HTMLResponse:
    """Render the zero-dependency Operator Compliance & Interconnection Queue Dashboard."""
    html_doc = render_compliance_dashboard_html(INTERCONNECTION_QUEUE.snapshot_stats())
    return HTMLResponse(content=html_doc)


@app.post("/dashboard/submit", response_class=HTMLResponse)
async def submit_compliance_dashboard(request: Request) -> HTMLResponse:
    """Process form submissions from `/dashboard` without requiring `python-multipart`."""
    raw_body = (await request.body()).decode("utf-8", errors="replace")
    parsed = urllib.parse.parse_qs(raw_body, keep_blank_values=True)

    def _first(key: str, default: str) -> str:
        vals = parsed.get(key)
        return vals[0].strip() if vals and vals[0].strip() else default

    try:
        app_req = ConnectionApplication(
            network_id=_first("network_id", "R"),
            asset_type=AssetType(_first("asset_type", AssetType.ROOFTOP_PV.value)),
            proposed_capacity_kw=float(_first("proposed_capacity_kw", "25")),
            bess_power_kw=float(_first("bess_power_kw", "0")),
            feeder_background_scale=float(_first("feeder_background_scale", "5.0")),
            inverter_mode=InverterMode(_first("inverter_mode", InverterMode.AS4777_VOLT_VAR_WATT.value)),
            study_type=StudyType(_first("study_type", StudyType.SNAPSHOT.value)),
        )
        agent = GridScreeningAgent(provider="deterministic", use_cache=True)
        verdict = agent.screen_application(app_req)
        html_doc = render_compliance_dashboard_html(
            INTERCONNECTION_QUEUE.snapshot_stats(),
            latest_verdict=verdict,
        )
        return HTMLResponse(content=html_doc)
    except Exception as exc:
        html_doc = render_compliance_dashboard_html(
            INTERCONNECTION_QUEUE.snapshot_stats(),
            error_message=str(exc),
        )
        return HTMLResponse(content=html_doc, status_code=422)


@app.post("/v1/screen", response_model=ScreeningVerdict)
def screen_structured(req: StructuredScreeningRequest) -> ScreeningVerdict:
    try:
        agent = GridScreeningAgent(provider=req.provider, use_cache=True)
        return agent.screen_application(req.application, force_tier2=req.force_tier2)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=_format_validation_error_detail(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Unhandled screening error on /v1/screen")
        raise HTTPException(
            status_code=500,
            detail="Internal AC screening engine error; inspect server logs for diagnostics.",
        ) from exc


@app.post("/v1/screen/intake", response_model=ScreeningVerdict)
def screen_intake(req: NaturalLanguageScreeningRequest) -> ScreeningVerdict:
    try:
        agent = GridScreeningAgent(provider=req.provider, use_cache=True)
        return agent.screen_natural_language(
            req.inquiry,
            force_tier2=req.force_tier2,
            study_type=req.study_type,
        )
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=_format_validation_error_detail(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Unhandled screening error on /v1/screen/intake")
        raise HTTPException(
            status_code=500,
            detail="Internal AC screening engine error; inspect server logs for diagnostics.",
        ) from exc


@app.post(
    "/v1/screen/plot",
    response_class=Response,
    responses={200: {"content": {"image/svg+xml": {}}}},
)
def screen_structured_plot(req: StructuredScreeningRequest) -> Response:
    """Execute structured connection screening and return a self-contained SVG voltage & envelope plot."""
    verdict = screen_structured(req)
    svg = render_verdict_svg(verdict)
    return Response(
        content=svg,
        media_type="image/svg+xml",
        headers={
            "X-Screening-Decision": verdict.decision.value,
            "X-Reproducibility-SHA256": verdict.audit_receipt.reproducibility_sha256,
        },
    )


@app.post(
    "/v1/screen/intake/plot",
    response_class=Response,
    responses={200: {"content": {"image/svg+xml": {}}}},
)
def screen_intake_plot(req: NaturalLanguageScreeningRequest) -> Response:
    """Execute natural-language connection screening and return a self-contained SVG voltage & envelope plot."""
    verdict = screen_intake(req)
    svg = render_verdict_svg(verdict)
    return Response(
        content=svg,
        media_type="image/svg+xml",
        headers={
            "X-Screening-Decision": verdict.decision.value,
            "X-Reproducibility-SHA256": verdict.audit_receipt.reproducibility_sha256,
        },
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Start the Grid Screening Agent FastAPI server.")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="Bind port (default: 8000)")
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
