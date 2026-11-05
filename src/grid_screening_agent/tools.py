"""Unified tool registry shared across MCP server, LLM tool-calling loop, and deterministic triage.

Why all three interfaces route through a single `tools.py` registry:
    Maintaining separate parameter schemas or calculation wrappers for MCP, REST, and CLI
    invites subtle numerical divergence across entry points. Centralizing validation
    (`ConnectionApplication`) and JSON-Schema `enum` descriptors (`TOOL_SCHEMAS`) here
    guarantees that an MCP agent, an HTTP client, and a CLI engineer invoke the exact
    same deterministic physics pipeline.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from grid_screening_agent.models import (
    SUPPORTED_NETWORK_IDS,
    AssetType,
    ConnectionApplication,
    InverterMode,
    StudyType,
)
from grid_screening_agent.physics.mitigation import propose_connection_envelopes
from grid_screening_agent.physics.opendss_worker import run_tier2_ac_study
from grid_screening_agent.physics.regime import classify_screening_regime
from grid_screening_agent.physics.tier1_screen import run_tier1_fast_screen
from grid_screening_agent.physics.topology import (
    inspect_connection_point,
    list_supported_networks,
)


def tool_list_networks() -> dict[str, Any]:
    """List all supported 415 V LV feeders (CSIRO R, H), 11-20 kV MV benchmarks, and NEM 132-500 kV substations.

    Catalog lookup only. Does NOT run short-circuit or AC power-flow calculations.
    """
    return list_supported_networks()


def tool_inspect_connection_point(
    network_id: str,
    connection_node: Optional[str] = None,
) -> dict[str, Any]:
    """Extract deterministic Thevenin impedance (R_th, X_th, Z_th), R/X ratio, fault MVA (S_sc), dV/dP, and SHA-256 hash.

    Call FIRST to inspect Point of Common Coupling (PCC) stiffness before screening. Does NOT run AC power flow.
    """
    profile = inspect_connection_point(network_id=network_id, connection_node=connection_node)
    return profile.model_dump()


def tool_classify_regime(
    network_id: str,
    proposed_capacity_kw: float,
    asset_type: str = "rooftop_pv",
    connection_node: Optional[str] = None,
) -> dict[str, Any]:
    """Apply the 3D Master Vertical Grid Matrix to classify the governing physical driver and primary binding constraint.

    Routes between resistive LV magnitude dominance, compact LV thermal dominance, balanced MV operating-state dominance,
    weak 132 kV N-1 SCR dominance, and 330-500 kV N-1 thermal dominance. Does NOT run AC power flow.
    """
    profile = inspect_connection_point(network_id=network_id, connection_node=connection_node)
    regime = classify_screening_regime(
        profile=profile,
        asset_type=AssetType(asset_type),
        proposed_capacity_kw=float(proposed_capacity_kw),
    )
    return regime.model_dump()


def tool_run_tier1_screen(
    network_id: str,
    proposed_capacity_kw: float,
    asset_type: str = "rooftop_pv",
    connection_node: Optional[str] = None,
    bess_power_kw: float = 0.0,
    feeder_background_scale: float = 5.0,
    operating_load_factor: float = 0.55,
    inverter_mode: str = "as4777_volt_var_watt",
) -> dict[str, Any]:
    """Calculates fast short-circuit and voltage screening. Returns a pass/fail. Does NOT run full AC power flow.

    Executes sub-10ms analytical checks across season-stratified voltage threshold (1.0752 pu), thermal margin,
    post-contingency N-1 Short Circuit Ratio (NER S5.2.5 SCR >= 2.0), and Pareto joint hosting capacity.
    Check `fast_clear_eligible` vs `flagged_for_tier2` to decide whether `run_tier2_ac_study` is needed.
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
    )
    profile = inspect_connection_point(app.network_id, app.connection_node)
    regime = classify_screening_regime(profile, app.asset_type, app.proposed_capacity_kw)
    t1 = run_tier1_fast_screen(app, profile, regime)
    return t1.model_dump()


def tool_run_tier2_ac_study(
    network_id: str,
    proposed_capacity_kw: float,
    asset_type: str = "rooftop_pv",
    connection_node: Optional[str] = None,
    bess_power_kw: float = 0.0,
    feeder_background_scale: float = 5.0,
    operating_load_factor: float = 0.55,
    inverter_mode: str = "as4777_volt_var_watt",
    study_type: str = "snapshot",
    solver_timeout_sec: float = 15.0,
    export_cap_kw: Optional[float] = None,
) -> dict[str, Any]:
    """Run isolated Tier-2 unbalanced 3-phase OpenDSS AC power flow (LV) or exact 2-bus N-0/N-1 AC solve (MV/TX).

    Scope note: `study_type='snapshot'` evaluates selected worst-case seasonal peak states (17 LV / 8 MV-TX states),
    whereas `study_type='time_series'` evaluates the chronological 30-min diurnal trajectory + seasonal peaks.
    Neither mode runs a full 17,520/35,040-interval annual telemetry sweep synchronously.
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
    profile = inspect_connection_point(app.network_id, app.connection_node)
    t2 = run_tier2_ac_study(app, profile, export_cap_kw=export_cap_kw)
    return t2.model_dump()


def tool_propose_mitigations(
    network_id: str,
    proposed_capacity_kw: float,
    asset_type: str = "rooftop_pv",
    connection_node: Optional[str] = None,
    bess_power_kw: float = 0.0,
    feeder_background_scale: float = 5.0,
    operating_load_factor: float = 0.55,
    inverter_mode: str = "as4777_volt_var_watt",
    study_type: str = "snapshot",
) -> dict[str, Any]:
    """Synthesize OpenDSS-verified Static Operating Envelopes (5kW/3kW), snapshot-bounded DOEs, or TX remediation.

    Important scope boundary: Dynamic Operating Envelope (`DOE_SNAPSHOT_ENVELOPE`) bounds are deterministic static
    envelopes derived from the evaluated snapshot/diurnal AC study (`telemetry_forecast_active=false`), NOT a live
    17,520-interval SSID-Kalman telemetry Warning Lead Time (WLT) forecast.
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
    )
    profile = inspect_connection_point(app.network_id, app.connection_node)
    regime = classify_screening_regime(profile, app.asset_type, app.proposed_capacity_kw)
    t1 = run_tier1_fast_screen(app, profile, regime)
    opts = propose_connection_envelopes(app, profile, regime, t1)
    return {"mitigation_options": [o.model_dump() for o in opts]}


TOOL_DISPATCH: dict[str, Callable[..., dict[str, Any]]] = {
    "list_networks": tool_list_networks,
    "inspect_connection_point": tool_inspect_connection_point,
    "classify_screening_regime": tool_classify_regime,
    "run_tier1_screen": tool_run_tier1_screen,
    "run_tier2_ac_study": tool_run_tier2_ac_study,
    "propose_mitigations": tool_propose_mitigations,
}

_COMMON_APP_PROPERTIES: dict[str, Any] = {
    "network_id": {
        "type": "string",
        "enum": list(SUPPORTED_NETWORK_IDS),
        "description": "Network or substation ID (e.g. 'R', 'H', 'CIGRE_MV_Rudolstadt', 'BrokenHill_132kV', 'SydneyWest_330kV', 'Bayswater_500kV').",
    },
    "proposed_capacity_kw": {
        "type": "number",
        "description": "Proposed connection active power rating in kW (e.g. 10 for 10 kW PV; 150000 for 150 MW data centre).",
    },
    "asset_type": {
        "type": "string",
        "enum": [e.value for e in AssetType],
        "description": "Asset class requesting connection.",
    },
    "connection_node": {
        "type": "string",
        "description": "Optional specific bus ID on feeder. Omit for worst-case terminal bus.",
    },
    "bess_power_kw": {
        "type": "number",
        "description": "Optional co-located battery inverter rating in kW.",
    },
    "feeder_background_scale": {
        "type": "number",
        "description": "Background LV solar growth scale relative to Ausgrid 2012-13 baseline (default 5.0; 4.31 is national mean; >=5.5 is stressed).",
    },
    "operating_load_factor": {
        "type": "number",
        "description": "Coincident feeder/corridor load factor in [0.05, 1.20].",
    },
    "inverter_mode": {
        "type": "string",
        "enum": [e.value for e in InverterMode],
        "description": "Inverter Volt-VAr / Volt-Watt droop mode.",
    },
    "study_type": {
        "type": "string",
        "enum": [e.value for e in StudyType],
        "description": "Tier-2 AC study scope: 'snapshot' (selected worst-case seasonal peaks) or 'time_series' (diurnal trajectory).",
    },
}

TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "list_networks",
        "description": "List all available LV feeders (CSIRO R, H), MV benchmarks, and NEM 132-500 kV substations. Catalog lookup only; does NOT run power flow.",
        "parameters": {"type": "object", "properties": {}, "required": []},
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "inspect_connection_point",
        "description": "Extract exact Thevenin impedance (R_th, X_th, Z_th), R/X ratio, fault MVA (S_sc), dV/dP sensitivity, and SHA-256 hash at the connection point. Does NOT run AC power flow.",
        "parameters": {
            "type": "object",
            "properties": {
                "network_id": _COMMON_APP_PROPERTIES["network_id"],
                "connection_node": _COMMON_APP_PROPERTIES["connection_node"],
            },
            "required": ["network_id"],
        },
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "classify_screening_regime",
        "description": "Classify the governing physical driver (Magnitude vs Operating State vs SCR) and primary binding constraint using the 3D Master Vertical Grid Matrix. Does NOT run AC power flow.",
        "parameters": {
            "type": "object",
            "properties": {
                "network_id": _COMMON_APP_PROPERTIES["network_id"],
                "proposed_capacity_kw": _COMMON_APP_PROPERTIES["proposed_capacity_kw"],
                "asset_type": _COMMON_APP_PROPERTIES["asset_type"],
                "connection_node": _COMMON_APP_PROPERTIES["connection_node"],
            },
            "required": ["network_id", "proposed_capacity_kw"],
        },
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "run_tier1_screen",
        "description": "Calculates fast short-circuit and voltage screening. Returns a pass/fail. Does NOT run full AC power flow.",
        "parameters": {
            "type": "object",
            "properties": _COMMON_APP_PROPERTIES,
            "required": ["network_id", "proposed_capacity_kw"],
        },
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "run_tier2_ac_study",
        "description": "Execute isolated Tier-2 unbalanced 3-phase OpenDSS AC power flow (LV) or N-0/N-1 AC contingency solve (MV/TX) over 'snapshot' or 'time_series' reference states (not a full 17,520-interval annual telemetry sweep).",
        "parameters": {
            "type": "object",
            "properties": {
                **_COMMON_APP_PROPERTIES,
                "export_cap_kw": {
                    "type": "number",
                    "description": "Optional per-customer export cap in kW to test mitigated compliance.",
                },
            },
            "required": ["network_id", "proposed_capacity_kw"],
        },
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
    {
        "name": "propose_mitigations",
        "description": "Evaluate conditional approval options (5 kW / 3 kW Static Operating Envelopes, snapshot-bounded Dynamic Operating Envelopes, or TX system-strength remediation). DOE bounds are deterministic AC snapshot envelopes, NOT live telemetry WLT forecasts.",
        "parameters": {
            "type": "object",
            "properties": _COMMON_APP_PROPERTIES,
            "required": ["network_id", "proposed_capacity_kw"],
        },
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
]
