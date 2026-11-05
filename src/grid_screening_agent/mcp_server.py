"""Model Context Protocol (MCP) server exposing deterministic grid connection screening tools.

Why this server hardens Domain 1 (Zero-Trust Security & Scoping) and Domain 2 (Token Efficiency):
    1. Zero-Trust HTTP/SSE Transport Auth (`MCPAuthMiddleware`): Enforces constant-time
       (`secrets.compare_digest`) Bearer token verification against `GRID_AGENT_API_TOKEN`
       on remote `sse` and `streamable-http` transports while leaving `/health` open for
       liveness probes and `stdio` zero-config for local IDE pipelines.
    2. Tool-Level Permission Scoping (`GRID_AGENT_MCP_SCOPE`): Supports `full` (default),
       `tier1_only`, and `readonly` scopes so untrusted intake agents cannot invoke
       child-process OpenDSS solvers (`run_tier2_ac_study`, `sweep_connection_capacities`).
    3. Off-Context `@mcp.resource` URIs & Trajectory Pruning: Parks 17-to-96 point voltage
       trajectories and vector SVG plots at `grid://verdicts/{fingerprint}/trajectory` and
       `grid://verdicts/{fingerprint}/plot.svg`, pruning raw arrays from tool responses by
       default (`include_trajectory=False`) without altering `reproducibility_sha256`.
    4. Native Server-Side Sweeps (`sweep_connection_capacities`): Executes N-point hosting
       capacity sweeps inside Python, eliminating N sequential LLM round-trips.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
from typing import Any, Literal, Optional, cast

import uvicorn
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

try:
    from mcp.server.mcpserver import MCPServer as _MCPServer
    from mcp_types import ToolAnnotations as _ToolAnnotations
except ModuleNotFoundError:  # pragma: no cover - compatibility with mcp 1.x
    from mcp.server.fastmcp import FastMCP as _MCPServer
    from mcp.types import ToolAnnotations as _ToolAnnotations  # type: ignore[no-redef]

from grid_screening_agent import __version__
from grid_screening_agent.agent import GridScreeningAgent
from grid_screening_agent.models import (
    AssetType,
    AssetTypeLiteral,
    BindingConstraintCode,
    CapacitySweepPoint,
    CapacitySweepResponse,
    ClearanceDecision,
    ConnectionApplication,
    ConnectionPointProfile,
    InverterMode,
    InverterModeLiteral,
    MitigationsResponse,
    NetworkCatalogResponse,
    RegimeClassification,
    ScreeningVerdict,
    StudyType,
    StudyTypeLiteral,
    SupportedNetworkId,
    Tier1ScreenResult,
    Tier2OpenDSSResult,
    ToolDiscoveryResponse,
    ToolMetadataItem,
)
from grid_screening_agent.physics.topology import inspect_connection_point as _inspect_pcc
from grid_screening_agent.physics.topology import list_supported_networks
from grid_screening_agent.plotting import render_verdict_svg
from grid_screening_agent.queue_cache import INTERCONNECTION_QUEUE
from grid_screening_agent.tools import (
    tool_classify_regime,
    tool_inspect_connection_point,
    tool_list_networks,
    tool_propose_mitigations,
    tool_run_tier1_screen,
    tool_run_tier2_ac_study,
)

MCPScopeLiteral = Literal["full", "tier1_only", "readonly"]

_SERVER_INSTRUCTIONS = (
    "Physics-grounded LV/MV/HV grid connection screening server ('LLM Orchestrates, Physics Solves, Gate Bounds'). "
    "Always call `inspect_connection_point` and `classify_screening_regime` first, then `run_tier1_screen`. "
    "Only call `run_tier2_ac_study` when Tier-1 flags risk (unless N-1 SCR < 2.0 already triggers NER S5.2.5 escalation). "
    "Use `sweep_connection_capacities` for multi-point sizing sweeps instead of looping in prompt space, and read "
    "`grid://verdicts/{fingerprint}/trajectory` or `plot.svg` only when detailed profiles are needed. "
    "Never fabricate electrical quantities."
)

_PURE_PHYSICS_ANNOTATIONS = _ToolAnnotations.model_validate(
    {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)

_STATEFUL_TRIAGE_ANNOTATIONS = _ToolAnnotations.model_validate(
    {
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
)

_TOOL_DIRECTORY: tuple[ToolMetadataItem, ...] = (
    ToolMetadataItem(
        name="list_networks",
        scope_required="tier1_only",
        cost_tier="catalog (<1ms)",
        summary="List supported 415 V LV feeders (R, H), 11-20 kV MV benchmarks, and NEM 132-500 kV substations.",
    ),
    ToolMetadataItem(
        name="inspect_connection_point",
        scope_required="tier1_only",
        cost_tier="catalog (<1ms)",
        summary="Extract deterministic Thevenin impedance (R_th, X_th, Z_th), R/X, S_sc, dV/dP, and SHA-256 hash.",
    ),
    ToolMetadataItem(
        name="classify_screening_regime",
        scope_required="tier1_only",
        cost_tier="catalog (<1ms)",
        summary="Classify governing physical driver and primary binding constraint via 3D Master Vertical Grid Matrix.",
    ),
    ToolMetadataItem(
        name="search_tools",
        scope_required="tier1_only",
        cost_tier="catalog (<1ms)",
        summary="Progressive discovery index returning available MCP tools filtered by keyword and active scope.",
    ),
    ToolMetadataItem(
        name="run_tier1_screen",
        scope_required="tier1_only",
        cost_tier="tier1_analytical (<10ms)",
        summary="Sub-10ms analytical short-circuit, thermal, and season-stratified voltage screen (no full AC flow).",
    ),
    ToolMetadataItem(
        name="run_tier2_ac_study",
        scope_required="full",
        cost_tier="tier2_solver (~150-300ms)",
        summary="Isolated child-process 3-phase OpenDSS AC power flow (LV) or exact 2-bus N-0/N-1 AC contingency solve.",
    ),
    ToolMetadataItem(
        name="propose_mitigations",
        scope_required="full",
        cost_tier="tier2_solver (~150-300ms)",
        summary="Synthesize OpenDSS-verified Static Operating Envelopes (5kW/3kW), snapshot DOEs, or TX remediation.",
    ),
    ToolMetadataItem(
        name="screen_connection_application",
        scope_required="full",
        cost_tier="tier2_solver (~150-300ms)",
        summary="End-to-end progressive-fidelity triage with Supervisory Sanity Gate and SHA-256 AuditReceipt.",
    ),
    ToolMetadataItem(
        name="sweep_connection_capacities",
        scope_required="full",
        cost_tier="tier2_solver (~150-300ms)",
        summary="Server-side N-point capacity sweep identifying exact hosting-capacity inflection without LLM loops.",
    ),
)


# --- DOMAIN 1: ZERO-TRUST STREAMING-SAFE AUTHENTICATION MIDDLEWARE ---


class MCPAuthMiddleware:
    """Enforces `GRID_AGENT_API_TOKEN` on HTTP/SSE MCP transports using constant-time comparison.

    Why implemented as pure ASGI middleware rather than `BaseHTTPMiddleware`:
        Starlette's `BaseHTTPMiddleware` buffers response bodies and breaks raw ASGI
        Server-Sent Events (`SseServerTransport.connect_sse`). Operating directly on
        `(scope, receive, send)` protects both `/sse`, `/messages/`, and `/mcp` streams
        with zero buffering while keeping `/health` unauthenticated for orchestrator probes.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if path == "/health":
            resp = JSONResponse({"status": "ok", "service": "grid-screen-mcp", "version": __version__})
            await resp(scope, receive, send)
            return

        expected_token = os.environ.get("GRID_AGENT_API_TOKEN", "").strip()
        if expected_token:
            headers = {
                k.decode("latin-1").lower(): v.decode("latin-1")
                for k, v in scope.get("headers", [])
            }
            auth_header = headers.get("authorization", "")
            if not auth_header.startswith("Bearer "):
                resp = JSONResponse(
                    {"error": "Missing or invalid Bearer token"},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
                await resp(scope, receive, send)
                return

            provided_token = auth_header[7:].strip()
            if not secrets.compare_digest(provided_token, expected_token):
                resp = JSONResponse({"error": "Unauthorized"}, status_code=403)
                await resp(scope, receive, send)
                return

        await self.app(scope, receive, send)


def _normalize_mcp_scope(scope: Optional[str] = None) -> MCPScopeLiteral:
    raw = (scope or os.environ.get("GRID_AGENT_MCP_SCOPE", "full")).strip().lower()
    if raw in ("readonly", "read_only"):
        return "readonly"
    if raw in ("tier1", "tier1_only", "fast_track"):
        return "tier1_only"
    return "full"


# --- TOOL IMPLEMENTATIONS (IMPORTABLE & REGISTERED BY SCOPE) ---


def list_networks() -> NetworkCatalogResponse:
    """List all supported ARENA/CSIRO 415 V LV feeders, 11-20 kV MV benchmarks, and NEM 132-500 kV substations.

    Catalog metadata lookup only. Does NOT run short-circuit or AC power-flow calculations.
    """
    return cast(NetworkCatalogResponse, tool_list_networks())


def inspect_connection_point(
    network_id: SupportedNetworkId,
    connection_node: Optional[str] = None,
) -> ConnectionPointProfile:
    """Extract deterministic Thevenin impedance (R_th, X_th, Z_th), R/X ratio, fault level S_sc, dV/dP, and SHA-256 hash.

    Call this tool FIRST to inspect electrical stiffness at the Point of Common Coupling (PCC). Does NOT run AC power flow.
    """
    return cast(
        ConnectionPointProfile,
        tool_inspect_connection_point(network_id=network_id, connection_node=connection_node),
    )


def classify_screening_regime(
    network_id: SupportedNetworkId,
    proposed_capacity_kw: float,
    asset_type: AssetTypeLiteral = "rooftop_pv",
    connection_node: Optional[str] = None,
) -> RegimeClassification:
    """Apply the 3D Master Vertical Grid Matrix to determine which physical constraint binds first (Voltage, Thermal, or N-1 SCR).

    Classifies Constraint-Decoupled Dominance across 415 V LV, 11-20 kV MV, and 132-500 kV Transmission. Does NOT run AC power flow.
    """
    return cast(
        RegimeClassification,
        tool_classify_regime(
            network_id=network_id,
            proposed_capacity_kw=proposed_capacity_kw,
            asset_type=asset_type,
            connection_node=connection_node,
        ),
    )


def run_tier1_screen(
    network_id: SupportedNetworkId,
    proposed_capacity_kw: float,
    asset_type: AssetTypeLiteral = "rooftop_pv",
    connection_node: Optional[str] = None,
    bess_power_kw: float = 0.0,
    feeder_background_scale: float = 5.0,
    operating_load_factor: float = 0.55,
    inverter_mode: InverterModeLiteral = "as4777_volt_var_watt",
) -> Tier1ScreenResult:
    """Calculates fast short-circuit and voltage screening. Returns a pass/fail. Does NOT run full AC power flow.

    Executes sub-10ms analytical screening using season-stratified voltage calibration (1.0752 pu), thermal margin,
    post-contingency N-1 Short Circuit Ratio (NER S5.2.5 SCR >= 2.0), and Pareto joint hosting capacity.
    Use `fast_clear_eligible` and `flagged_for_tier2` to decide whether to escalate to `run_tier2_ac_study`.
    """
    return cast(
        Tier1ScreenResult,
        tool_run_tier1_screen(
            network_id=network_id,
            proposed_capacity_kw=proposed_capacity_kw,
            asset_type=asset_type,
            connection_node=connection_node,
            bess_power_kw=bess_power_kw,
            feeder_background_scale=feeder_background_scale,
            operating_load_factor=operating_load_factor,
            inverter_mode=inverter_mode,
        ),
    )


def run_tier2_ac_study(
    network_id: SupportedNetworkId,
    proposed_capacity_kw: float,
    asset_type: AssetTypeLiteral = "rooftop_pv",
    connection_node: Optional[str] = None,
    bess_power_kw: float = 0.0,
    feeder_background_scale: float = 5.0,
    operating_load_factor: float = 0.55,
    inverter_mode: InverterModeLiteral = "as4777_volt_var_watt",
    study_type: StudyTypeLiteral = "snapshot",
    solver_timeout_sec: float = 15.0,
    export_cap_kw: Optional[float] = None,
    include_trajectory: bool = False,
) -> Tier2OpenDSSResult:
    """Run isolated child-process unbalanced 3-phase OpenDSS AC power flow (LV) or exact 2-bus N-0/N-1 AC contingency solve (MV/TX).

    Study scope: `study_type='snapshot'` evaluates selected worst-case seasonal peak states (17 LV / 8 MV-TX states);
    `study_type='time_series'` evaluates the chronological 30-min diurnal trajectory + seasonal peaks.
    Does NOT run a synchronous 17,520/35,040-interval full-year telemetry simulation. Enforces a hard subprocess timeout.
    By default, `include_trajectory=False` prunes the raw trajectory array to protect LLM context tokens and returns
    `trajectory_resource_uri` (`grid://verdicts/{fingerprint}/trajectory`) for on-demand inspection.
    """
    raw_res = dict(
        tool_run_tier2_ac_study(
            network_id=network_id,
            proposed_capacity_kw=proposed_capacity_kw,
            asset_type=asset_type,
            connection_node=connection_node,
            bess_power_kw=bess_power_kw,
            feeder_background_scale=feeder_background_scale,
            operating_load_factor=operating_load_factor,
            inverter_mode=inverter_mode,
            study_type=study_type,
            solver_timeout_sec=solver_timeout_sec,
            export_cap_kw=export_cap_kw,
        )
    )
    key_material = json.dumps(
        {
            "network_id": network_id,
            "connection_node": connection_node,
            "proposed_capacity_kw": proposed_capacity_kw,
            "asset_type": asset_type,
            "bess_power_kw": bess_power_kw,
            "feeder_background_scale": feeder_background_scale,
            "operating_load_factor": operating_load_factor,
            "inverter_mode": inverter_mode,
            "study_type": study_type,
            "export_cap_kw": export_cap_kw,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    traj_fp = hashlib.sha256(key_material).hexdigest()
    traj_pts = list(raw_res.get("voltage_profile") or [])
    INTERCONNECTION_QUEUE.store_trajectory(
        traj_fp,
        {
            "fingerprint": traj_fp,
            "network_id": network_id,
            "study_scope": raw_res.get("study_scope", ""),
            "points_count": len(traj_pts),
            "voltage_profile": traj_pts,
        },
    )
    raw_res["trajectory_resource_uri"] = f"grid://verdicts/{traj_fp}/trajectory"
    if not include_trajectory:
        raw_res["voltage_profile"] = []
    return cast(Tier2OpenDSSResult, raw_res)


def propose_mitigations(
    network_id: SupportedNetworkId,
    proposed_capacity_kw: float,
    asset_type: AssetTypeLiteral = "rooftop_pv",
    connection_node: Optional[str] = None,
    bess_power_kw: float = 0.0,
    feeder_background_scale: float = 5.0,
    operating_load_factor: float = 0.55,
    inverter_mode: InverterModeLiteral = "as4777_volt_var_watt",
    study_type: StudyTypeLiteral = "snapshot",
) -> MitigationsResponse:
    """Synthesize OpenDSS-verified Static Operating Envelopes (5kW/3kW), snapshot-bounded DOEs, or TX system-strength options.

    Note: The Dynamic Operating Envelope (`DOE_SNAPSHOT_ENVELOPE`) is a static envelope derived from deterministic AC
    snapshot/diurnal bounds (`telemetry_forecast_active=false`), NOT a live SSID-Kalman predictive Warning Lead Time (WLT) forecast.
    """
    return cast(
        MitigationsResponse,
        tool_propose_mitigations(
            network_id=network_id,
            proposed_capacity_kw=proposed_capacity_kw,
            asset_type=asset_type,
            connection_node=connection_node,
            bess_power_kw=bess_power_kw,
            feeder_background_scale=feeder_background_scale,
            operating_load_factor=operating_load_factor,
            inverter_mode=inverter_mode,
            study_type=study_type,
        ),
    )


def screen_connection_application(
    network_id: SupportedNetworkId = "R",
    proposed_capacity_kw: float = 10.0,
    asset_type: AssetTypeLiteral = "rooftop_pv",
    connection_node: Optional[str] = None,
    bess_power_kw: float = 0.0,
    feeder_background_scale: float = 5.0,
    operating_load_factor: float = 0.55,
    inverter_mode: InverterModeLiteral = "as4777_volt_var_watt",
    study_type: StudyTypeLiteral = "snapshot",
    solver_timeout_sec: float = 15.0,
    force_tier2: bool = False,
    include_trajectory: bool = False,
) -> ScreeningVerdict:
    """Execute end-to-end progressive-fidelity triage with Supervisory Sanity Gate, `requires_human_review`, `regulatory_override`, and SHA-256 AuditReceipt.

    By default, `include_trajectory=False` omits raw Tier-2 voltage arrays to save LLM context tokens while preserving
    the exact `reproducibility_sha256` digest and exposing `grid://verdicts/{queue_fingerprint}/trajectory` & `plot.svg`.
    """
    app = ConnectionApplication(
        network_id=network_id,
        connection_node=connection_node,
        asset_type=AssetType(asset_type),
        proposed_capacity_kw=proposed_capacity_kw,
        bess_power_kw=bess_power_kw,
        feeder_background_scale=feeder_background_scale,
        operating_load_factor=operating_load_factor,
        inverter_mode=InverterMode(inverter_mode),
        study_type=StudyType(study_type),
        solver_timeout_sec=solver_timeout_sec,
    )
    agent = GridScreeningAgent(provider="deterministic", use_cache=True)
    verdict = agent.screen_application(app, force_tier2=force_tier2)
    dumped = verdict.model_dump()
    if dumped.get("tier2") is not None:
        fp = verdict.audit_receipt.queue_fingerprint
        dumped["tier2"]["trajectory_resource_uri"] = f"grid://verdicts/{fp}/trajectory"
        if not include_trajectory:
            dumped["tier2"]["voltage_profile"] = []
    return cast(ScreeningVerdict, dumped)


def sweep_connection_capacities(
    network_id: SupportedNetworkId,
    start_kw: float,
    end_kw: float,
    steps: int = 5,
    asset_type: AssetTypeLiteral = "rooftop_pv",
    connection_node: Optional[str] = None,
    bess_power_kw: float = 0.0,
    feeder_background_scale: float = 5.0,
    operating_load_factor: float = 0.55,
    inverter_mode: InverterModeLiteral = "as4777_volt_var_watt",
    halt_on_violation: bool = True,
) -> CapacitySweepResponse:
    """Execute a server-side N-point hosting-capacity sweep to locate the exact constraint inflection point.

    Shields the LLM context window from N sequential tool round-trips by evaluating candidate capacities
    natively in Python (`1 <= steps <= 20`) and returning a compact inflection table.
    """
    if start_kw <= 0 or end_kw < start_kw:
        raise ValueError(f"Invalid sweep bounds: require 0 < start_kw <= end_kw (got {start_kw}..{end_kw}).")
    bounded_steps = max(1, min(20, int(steps)))
    step_size = (end_kw - start_kw) / (bounded_steps - 1) if bounded_steps > 1 else 0.0
    capacities = [round(start_kw + (idx * step_size), 4) for idx in range(bounded_steps)]

    profile = _inspect_pcc(network_id=network_id, connection_node=connection_node)
    agent = GridScreeningAgent(provider="deterministic", use_cache=True)

    sweep_points: list[CapacitySweepPoint] = []
    max_compliant_kw = 0.0
    inflection_kw: Optional[float] = None
    first_binding = BindingConstraintCode.NONE
    halted_early = False

    for idx, kw in enumerate(capacities):
        app = ConnectionApplication(
            network_id=network_id,
            connection_node=profile.connection_node,
            asset_type=AssetType(asset_type),
            proposed_capacity_kw=kw,
            bess_power_kw=bess_power_kw,
            feeder_background_scale=feeder_background_scale,
            operating_load_factor=operating_load_factor,
            inverter_mode=InverterMode(inverter_mode),
            study_type=StudyType.SNAPSHOT,
        )
        verdict = agent.screen_application(app)
        primary_lim = verdict.binding_constraints[0] if verdict.binding_constraints else BindingConstraintCode.NONE
        peak_v = (
            verdict.tier2.peak_voltage_pu
            if verdict.tier2 is not None
            else verdict.tier1.predicted_peak_v_pu
        )
        max_th = (
            max(verdict.tier2.max_line_loading_pct, verdict.tier2.transformer_loading_pct)
            if verdict.tier2 is not None
            else verdict.tier1.estimated_thermal_loading_pct
        )
        sweep_points.append(
            CapacitySweepPoint(
                capacity_kw=round(kw, 2),
                decision=verdict.decision,
                limiting_factor=primary_lim,
                peak_voltage_pu=round(peak_v, 5),
                max_thermal_loading_pct=round(max_th, 2),
                scr_n1=verdict.tier1.scr_n1,
                requires_human_review=verdict.requires_human_review,
            )
        )

        if verdict.decision == ClearanceDecision.FAST_TRACK_APPROVED:
            max_compliant_kw = round(kw, 2)
        elif inflection_kw is None:
            inflection_kw = round(kw, 2)
            first_binding = primary_lim
            if halt_on_violation and idx < (len(capacities) - 1):
                halted_early = True
                break

    resp = CapacitySweepResponse(
        network_id=profile.network_id,
        connection_node=profile.connection_node,
        asset_type=asset_type,
        steps_evaluated=len(sweep_points),
        max_compliant_kw=max_compliant_kw,
        inflection_capacity_kw=inflection_kw,
        first_binding_constraint=first_binding,
        halted_early=halted_early,
        sweep_results=sweep_points,
    )
    return cast(CapacitySweepResponse, resp.model_dump())


# --- DOMAIN 2: OFF-CONTEXT MCP RESOURCE HANDLERS ---


def read_catalog_networks_resource() -> str:
    """Return full LV/MV/HV network & substation catalog JSON outside tool-call context."""
    return json.dumps(list_supported_networks(), sort_keys=True)


def read_queue_stats_resource() -> str:
    """Return live interconnection queue deduplication & clearance telemetry JSON."""
    return json.dumps(INTERCONNECTION_QUEUE.snapshot_stats(), sort_keys=True)


def read_verdict_trajectory_resource(fingerprint: str) -> str:
    """Resolve `grid://verdicts/{fingerprint}/trajectory` by queue fingerprint, prefix, or receipt_id."""
    traj = INTERCONNECTION_QUEUE.get_trajectory(fingerprint)
    if traj is None:
        raise ValueError(
            f"Trajectory resource not found for identifier '{fingerprint}'. "
            "Run `screen_connection_application` or `run_tier2_ac_study` first."
        )
    return json.dumps(traj, sort_keys=True)


def read_verdict_svg_resource(fingerprint: str) -> str:
    """Resolve `grid://verdicts/{fingerprint}/plot.svg` into a self-contained vector SVG chart."""
    verdict = INTERCONNECTION_QUEUE.get_verdict(fingerprint)
    if verdict is None:
        raise ValueError(
            f"Verdict plot resource not found for identifier '{fingerprint}'. "
            "Run `screen_connection_application` first."
        )
    return render_verdict_svg(verdict)


# --- SCOPED MCP SERVER FACTORY & TRANSPORT BUILDERS ---


def build_mcp_server(scope: Optional[str] = None) -> _MCPServer:
    """Construct a scope-enforced `MCPServer` (`full`, `tier1_only`, or `readonly`).

    Why tools are conditionally registered at server construction:
        Under zero-trust MCP scoping, an untrusted or public-facing agent connected to a
        `tier1_only` or `readonly` server neither sees Tier-2 child-process tools in
        `tools/list` nor can invoke them via `tools/call` ("confused deputy" prevention).
    """
    active_scope = _normalize_mcp_scope(scope)
    server = _MCPServer(
        "grid-screening-agent",
        instructions=_SERVER_INSTRUCTIONS,
    )

    def _scoped_search_tools(query: str = "") -> ToolDiscoveryResponse:
        """Search available MCP screening tools by keyword, returning cost tier and scope metadata."""
        q = query.strip().lower()
        allowed: list[ToolMetadataItem] = []
        for item in _TOOL_DIRECTORY:
            if active_scope == "readonly" and item.name not in (
                "list_networks",
                "inspect_connection_point",
                "classify_screening_regime",
                "search_tools",
            ):
                continue
            if active_scope == "tier1_only" and item.scope_required == "full":
                continue
            if not q or q in item.name.lower() or q in item.summary.lower():
                allowed.append(item)
        return cast(
            ToolDiscoveryResponse,
            ToolDiscoveryResponse(active_scope=active_scope, matched_tools=allowed).model_dump(),
        )

    # Always-available catalog & topology discovery tools
    server.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
        structured_output=True,
    )(list_networks)
    server.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
        structured_output=True,
    )(inspect_connection_point)
    server.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
        structured_output=True,
    )(classify_screening_regime)
    server.tool(
        name="search_tools",
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
        structured_output=True,
    )(_scoped_search_tools)

    # Tier-1 sub-10ms analytical heuristic screen (enabled in `tier1_only` and `full`)
    if active_scope in ("tier1_only", "full"):
        server.tool(
            annotations={
                "readOnlyHint": True,
                "destructiveHint": False,
                "idempotentHint": True,
                "openWorldHint": False,
            },
            structured_output=True,
        )(run_tier1_screen)

    # Tier-2 isolated OpenDSS child-process & batch sweep tools (restricted strictly to `full` scope)
    if active_scope == "full":
        server.tool(
            annotations={
                "readOnlyHint": True,
                "destructiveHint": False,
                "idempotentHint": True,
                "openWorldHint": False,
            },
            structured_output=True,
        )(run_tier2_ac_study)
        server.tool(
            annotations={
                "readOnlyHint": True,
                "destructiveHint": False,
                "idempotentHint": True,
                "openWorldHint": False,
            },
            structured_output=True,
        )(propose_mitigations)
        server.tool(
            annotations={
                "readOnlyHint": False,
                "destructiveHint": False,
                "idempotentHint": True,
                "openWorldHint": False,
            },
            structured_output=True,
        )(screen_connection_application)
        server.tool(
            annotations={
                "readOnlyHint": False,
                "destructiveHint": False,
                "idempotentHint": True,
                "openWorldHint": False,
            },
            structured_output=True,
        )(sweep_connection_capacities)

    # Off-context MCP Resources (Domain 2 Token Efficiency)
    server.resource(
        "grid://catalog/networks",
        name="Supported Grid Networks",
        description="Complete catalog of 415 V LV feeders, 11-20 kV MV benchmarks, and NEM 132-500 kV substations.",
        mime_type="application/json",
    )(read_catalog_networks_resource)
    server.resource(
        "grid://queue/stats",
        name="Interconnection Queue Telemetry",
        description="Live backlog deduplication statistics, fast-track counts, and recent SHA-256 audit entries.",
        mime_type="application/json",
    )(read_queue_stats_resource)
    server.resource(
        "grid://verdicts/{fingerprint}/trajectory",
        name="Verdict Voltage Trajectory",
        description="Full solved Tier-2 voltage & thermal trajectory JSON parked outside LLM tool context.",
        mime_type="application/json",
    )(read_verdict_trajectory_resource)
    server.resource(
        "grid://verdicts/{fingerprint}/plot.svg",
        name="Verdict Engineering Plot SVG",
        description="Zero-dependency vector SVG voltage & statutory envelope chart for a screened application.",
        mime_type="image/svg+xml",
    )(read_verdict_svg_resource)

    return server


def search_tools(query: str = "") -> ToolDiscoveryResponse:
    """Search available MCP screening tools by keyword, returning cost tier and scope metadata."""
    active_scope = _normalize_mcp_scope()
    q = query.strip().lower()
    matched = [
        item
        for item in _TOOL_DIRECTORY
        if (active_scope == "full" or item.scope_required != "full")
        and (not q or q in item.name.lower() or q in item.summary.lower())
    ]
    return cast(
        ToolDiscoveryResponse,
        ToolDiscoveryResponse(active_scope=active_scope, matched_tools=matched).model_dump(),
    )


def create_mcp_http_app(
    transport: Literal["sse", "streamable-http"] = "sse",
    host: str = "0.0.0.0",
    server_instance: Optional[_MCPServer] = None,
) -> Starlette:
    """Build a Starlette ASGI application for SSE or StreamableHTTP wrapped in `MCPAuthMiddleware`."""
    srv = server_instance or build_mcp_server()
    if transport == "sse":
        starlette_app = srv.sse_app(host=host)
    else:
        starlette_app = srv.streamable_http_app(host=host)
    starlette_app.add_middleware(MCPAuthMiddleware)
    return starlette_app


# Default module-level server instance (honors `GRID_AGENT_MCP_SCOPE` at import time)
mcp = build_mcp_server()


def run_mcp_server(
    transport: Literal["stdio", "streamable-http", "sse"] = "stdio",
    host: str = "0.0.0.0",
    port: int = 8000,
    scope: Optional[str] = None,
) -> None:
    """Launch the MCP server over `stdio` or zero-trust authenticated `sse` / `streamable-http`."""
    server_instance = build_mcp_server(scope=scope) if scope is not None else build_mcp_server()
    if transport == "stdio":
        server_instance.run(transport="stdio")
        return

    asgi_app = create_mcp_http_app(
        transport=transport,
        host=host,
        server_instance=server_instance,
    )
    uvicorn.run(asgi_app, host=host, port=port)


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="Run the Grid Connection Screening MCP server.")
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http", "sse"],
        default="stdio",
        help="MCP transport mode (default: stdio).",
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Bind host for HTTP/SSE transports (default: 0.0.0.0).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("PORT", "8000")),
        help="Bind port for HTTP/SSE transports (default: 8000).",
    )
    parser.add_argument(
        "--scope",
        choices=["full", "tier1_only", "readonly"],
        default=None,
        help="Optional override for GRID_AGENT_MCP_SCOPE ('full', 'tier1_only', or 'readonly').",
    )
    args = parser.parse_args(argv)
    run_mcp_server(
        transport=args.transport,
        host=args.host,
        port=args.port,
        scope=args.scope,
    )


if __name__ == "__main__":
    main()
