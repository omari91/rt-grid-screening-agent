"""Typed domain schemas for connection screening requests, tool outputs, and audit verdicts.

Why strict Pydantic schemas & Literal unions are enforced here:
    1. Prevents LLM tool-callers over MCP/REST from inventing unsupported feeder IDs,
       inverter droop modes, or negative/zero capacities before solver cycles run.
    2. Separates physical constraint outcomes (`BindingConstraintCode`) from narrative
       prose so upstream interconnection queue portals never scrape free text.
    3. Captures dual cryptographic hashes (`feeder_model_sha256` + `reproducibility_sha256`)
       and queue deduplication metadata (`queue_fingerprint`, `cache_hit`) on `AuditReceipt`.
"""

from __future__ import annotations

import uuid
from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

SupportedNetworkId = Literal[
    "R",
    "H",
    "CIGRE_MV_Rudolstadt",
    "Aus_MV_11kV_Suburban",
    "BrokenHill_132kV",
    "Wangaratta_132kV",
    "Wellington_132kV",
    "Armidale_330kV",
    "SydneyWest_330kV",
    "Bayswater_500kV",
]

SUPPORTED_NETWORK_IDS: tuple[str, ...] = (
    "R",
    "H",
    "CIGRE_MV_Rudolstadt",
    "Aus_MV_11kV_Suburban",
    "BrokenHill_132kV",
    "Wangaratta_132kV",
    "Wellington_132kV",
    "Armidale_330kV",
    "SydneyWest_330kV",
    "Bayswater_500kV",
)


class BindingConstraintCode(str, Enum):
    NONE = "NONE"
    SYSTEM_STRENGTH_SCR_N1 = "SYSTEM_STRENGTH_SCR_N1"
    THERMAL_AMPACITY = "THERMAL_AMPACITY"
    STEADY_STATE_VOLTAGE = "STEADY_STATE_VOLTAGE"
    SOLVER_OR_DATA_FAULT = "SOLVER_OR_DATA_FAULT"


class AssetType(str, Enum):
    ROOFTOP_PV = "rooftop_pv"
    PV_PLUS_BESS = "pv_plus_bess"
    BESS_ONLY = "bess_only"
    EV_OR_COMMERCIAL_LOAD = "ev_or_commercial_load"
    HYPERSCALE_DATACENTRE = "hyperscale_datacentre"
    UTILITY_IBR_PLANT = "utility_ibr_plant"


class VoltageTier(str, Enum):
    LV_415V = "LV_415V"
    MV_11_20KV = "MV_11_20kV"
    TX_132KV = "TX_132kV"
    TX_330_500KV = "TX_330_500kV"


class InverterMode(str, Enum):
    UNMANAGED_PF1 = "unmanaged_pf1"
    AS4777_VOLT_VAR = "as4777_volt_var"
    AS4777_VOLT_VAR_WATT = "as4777_volt_var_watt"


class StudyType(str, Enum):
    SNAPSHOT = "snapshot"
    TIME_SERIES = "time_series"


AssetTypeLiteral = Literal[
    "rooftop_pv",
    "pv_plus_bess",
    "bess_only",
    "ev_or_commercial_load",
    "hyperscale_datacentre",
    "utility_ibr_plant",
]

InverterModeLiteral = Literal[
    "unmanaged_pf1",
    "as4777_volt_var",
    "as4777_volt_var_watt",
]

StudyTypeLiteral = Literal[
    "snapshot",
    "time_series",
]


class ClearanceDecision(str, Enum):
    FAST_TRACK_APPROVED = "FAST_TRACK_APPROVED"
    CONDITIONAL_DOE_OR_CAP = "CONDITIONAL_DOE_OR_CAP"
    ESCALATE_DETAILED_STUDY = "ESCALATE_DETAILED_STUDY"
    ABSTAIN_DATA_OR_SOLVER_FAULT = "ABSTAIN_DATA_OR_SOLVER_FAULT"


class ConnectionApplication(BaseModel):
    """Normalized connection inquiry submitted by an installer, proponent, or engineer."""

    application_id: str = Field(
        default_factory=lambda: f"APP-{uuid.uuid4().hex[:8].upper()}",
        description="Unique connection application identifier.",
    )
    network_id: str = Field(
        default="R",
        description=(
            "Target network or substation ID (e.g., 'R', 'H', 'CIGRE_MV_Rudolstadt', "
            "'Aus_MV_11kV_Suburban', 'BrokenHill_132kV', 'Wangaratta_132kV', "
            "'SydneyWest_330kV', 'Bayswater_500kV')."
        ),
    )
    connection_node: Optional[str] = Field(
        default=None,
        description="Target bus ID on the feeder. Defaults to most remote terminal bus for conservative bounding.",
    )
    asset_type: AssetType = Field(
        default=AssetType.ROOFTOP_PV,
        description="Type of generation, storage, or large load requesting connection.",
    )
    proposed_capacity_kw: float = Field(
        ...,
        gt=0.0,
        description="Proposed active power rating in kW (use kW for all tiers; e.g. 150,000 kW = 150 MW).",
    )
    bess_power_kw: float = Field(
        default=0.0,
        ge=0.0,
        description="Co-located battery inverter step-export/charge rating in kW (0 if none).",
    )
    feeder_background_scale: float = Field(
        default=5.0,
        ge=0.5,
        le=10.0,
        description=(
            "Background feeder DER capacity multiplier relative to 2012-2013 Ausgrid baseline "
            "(4.31x represents current Australian national growth; >=5.25x enters Network R violation band)."
        ),
    )
    operating_load_factor: float = Field(
        default=0.55,
        ge=0.05,
        le=1.20,
        description="Coincident background network loading factor (0.05 minimum demand to 1.00 peak load).",
    )
    inverter_mode: InverterMode = Field(
        default=InverterMode.AS4777_VOLT_VAR_WATT,
        description="Inverter control mode (unmanaged unity PF vs AS/NZS 4777.2:2020 Volt-VAr / Volt-Watt).",
    )
    study_type: StudyType = Field(
        default=StudyType.SNAPSHOT,
        description=(
            "Tier-2 AC study scope: 'snapshot' evaluates selected worst-case seasonal peak states; "
            "'time_series' evaluates the chronological half-hourly diurnal trajectory + seasonal peaks."
        ),
    )
    solver_timeout_sec: float = Field(
        default=15.0,
        ge=1.0,
        le=120.0,
        description="Hard wall-clock timeout in seconds for the isolated OpenDSS child-process solver.",
    )
    seasons: list[str] = Field(
        default_factory=lambda: ["Spring", "Summer", "Autumn", "Winter"],
        description="Seasons to evaluate during screening.",
    )

    @field_validator("seasons")
    @classmethod
    def _validate_seasons(cls, v: list[str]) -> list[str]:
        valid = {"Spring", "Summer", "Autumn", "Winter"}
        cleaned = [s.capitalize() for s in v if s.capitalize() in valid]
        return cleaned or ["Spring", "Summer", "Autumn", "Winter"]

    @model_validator(mode="after")
    def _enforce_cross_field_voltage_tier_limits(self) -> "ConnectionApplication":
        """Reject physically impossible >500 kW applications targeting 415 V LV street feeders."""
        net_clean = self.network_id.strip()
        if net_clean.upper() in ("R", "H") and self.proposed_capacity_kw > 500.0:
            raise ValueError(
                f"Invalid cross-field combination: {self.proposed_capacity_kw:g} kW on 415 V LV feeder "
                f"'{net_clean}'. Capacities exceeding 500 kW cannot connect to a 500 kVA Low Voltage "
                "street feeder; select an 11-20 kV MV benchmark ('Aus_MV_11kV_Suburban', "
                "'CIGRE_MV_Rudolstadt') or 132-500 kV transmission substation."
            )
        return self


class ConnectionPointProfile(BaseModel):
    """Electrical and topological parameters extracted at the point of common coupling (PCC)."""

    network_id: str
    connection_node: str
    voltage_tier: VoltageTier
    nominal_kv: float
    description: str
    r_th_ohm: float
    x_th_ohm: float
    z_th_ohm: float
    rx_ratio: float
    xr_ratio: float
    s_sc_mva: float
    s_sc_n1_mva: float
    dv_dp_pu_per_kw: float
    transformer_kva: Optional[float] = None
    electrical_rank: int = 1
    total_candidate_buses: int = 1
    model_sha256: str


class RegimeClassification(BaseModel):
    """Constraint-Decoupled Dominance classification from the 3D Master Vertical Grid Matrix."""

    regime_id: str
    voltage_tier: VoltageTier
    impedance_regime: str
    power_flow_direction: str
    governing_voltage_driver: str
    governing_thermal_driver: str
    governing_scr_driver: str
    primary_binding_constraint: str
    secondary_binding_constraint: str
    voltage_only_error_risk: str
    valid_screening_rule: str
    requires_state_aware_screen: bool
    requires_scr_first_screen: bool
    topologically_voltage_immune: bool


class Tier1ScreenResult(BaseModel):
    """Millisecond analytical screening result with season-stratified calibration."""

    predicted_peak_v_pu: float
    predicted_min_v_pu: float
    voltage_limit_max_pu: float
    voltage_limit_min_pu: float
    season_stratified_threshold_pu: float
    winter_biased_threshold_pu: float
    estimated_thermal_loading_pct: float
    scr_base: float
    scr_n1: float
    system_strength_class: str
    pareto_joint_headroom_kw: float
    voltage_only_headroom_kw: float
    single_constraint_error_penalty_pct: float
    flagged_for_tier2: bool
    fast_clear_eligible: bool
    hard_escalation_reason: Optional[str] = None
    flag_reasons: list[str] = Field(default_factory=list)
    wilson_95ci_violation_pct: tuple[float, float] = (0.0, 0.0)
    execution_ms: float


class VoltageProfilePoint(BaseModel):
    """Single solved state in the Tier-2 voltage trajectory for plotting and inspection."""

    label: str
    season: str
    v_max_pu: float
    v_min_pu: float
    loading_pct: float = 0.0


class Tier2OpenDSSResult(BaseModel):
    """Unbalanced 3-phase AC OpenDSS simulation result run in an isolated worker process."""

    executed: bool
    study_type: StudyType = StudyType.SNAPSHOT
    study_scope: str = "snapshot (17 critical seasonal peak states; excludes full 17,520-interval annual telemetry)"
    solver_engine: str
    intervals_solved: int
    solver_failures: int
    timed_out: bool = False
    peak_voltage_pu: float
    min_voltage_pu: float
    worst_season: str
    worst_timestamp: str
    violations_count: int
    max_line_loading_pct: float
    transformer_loading_pct: float
    as4777_curtailment_pct: float = 0.0
    as4777_q_saturated: bool = False
    bess_cliff_delta_v_pu: float = 0.0
    voltage_profile: list[VoltageProfilePoint] = Field(default_factory=list)
    trajectory_resource_uri: Optional[str] = Field(
        default=None,
        description=(
            "Off-context MCP Resource URI (`grid://verdicts/{fingerprint}/trajectory`) "
            "referencing the full time-series/snapshot trajectory when omitted to save LLM tokens."
        ),
    )
    statutory_voltage_compliant: bool
    thermal_compliant: bool
    execution_ms: float


class MitigationOption(BaseModel):
    """Actionable conditional approval envelope or network mitigation."""

    option_id: str
    title: str
    mechanism: str
    export_cap_kw: Optional[float] = None
    dynamic_envelope_enabled: bool = False
    envelope_basis: str = Field(
        default="snapshot_static_bound",
        description="Physical basis of envelope ('snapshot_static_bound', 'diurnal_time_series_bound', or 'tx_n1_headroom').",
    )
    telemetry_forecast_active: bool = Field(
        default=False,
        description="False for offline snapshot/diurnal screening; True only when paired with live SCADA/AMI telemetry forecasting.",
    )
    resulting_peak_v_pu: Optional[float] = None
    resulting_loading_pct: Optional[float] = None
    resulting_scr_n1: Optional[float] = None
    compliant: bool
    recommendation_notes: str


class SanityGateReport(BaseModel):
    """Supervisory physics sanity gate auditing tool consistency and numerical bounds."""

    passed: bool
    checks_run: list[str]
    anomalies_detected: list[str] = Field(default_factory=list)
    abstention_triggered: bool = False
    regulatory_override: Optional[str] = None
    fallback_applied: Optional[str] = None


class AuditReceipt(BaseModel):
    """Deterministic regulatory and cryptographic provenance bundle."""

    receipt_id: str
    timestamp_utc: str
    application_id: str
    feeder_model_sha256: str
    reproducibility_sha256: str = Field(
        default="",
        description="Deterministic SHA-256 over canonical inputs, feeder hash, runtime versions, and verified physics verdict.",
    )
    runtime_versions: dict[str, str] = Field(
        default_factory=dict,
        description="Exact runtime versions (grid_screening_agent, python, opendssdirect.py) bound into reproducibility_sha256.",
    )
    queue_fingerprint: str = Field(
        default="",
        description="Deterministic state-compression SHA-256 fingerprint used to intercept duplicate queue submissions.",
    )
    cache_hit: bool = Field(
        default=False,
        description="True when an identical verified configuration was served from the interconnection queue cache.",
    )
    orchestration_mode: str
    standards_checked: list[str]
    solver_convergence_verified: bool
    calibration_convention: str
    tool_trace: list[dict[str, Any]] = Field(default_factory=list)


class NetworkCatalogResponse(BaseModel):
    """Typed MCP output schema for `list_networks`."""

    lv_feeders: dict[str, dict[str, Any]]
    mv_benchmarks: dict[str, dict[str, Any]]
    transmission_substations: dict[str, dict[str, Any]]


class MitigationsResponse(BaseModel):
    """Typed MCP output schema for `propose_mitigations`."""

    mitigation_options: list[MitigationOption]


class CapacitySweepPoint(BaseModel):
    """Single evaluated capacity step in a server-side hosting-capacity sweep."""

    capacity_kw: float
    decision: ClearanceDecision
    limiting_factor: BindingConstraintCode
    peak_voltage_pu: float
    max_thermal_loading_pct: float
    scr_n1: float
    requires_human_review: bool


class CapacitySweepResponse(BaseModel):
    """Typed MCP output schema for `sweep_connection_capacities` (shielding LLM context from N round-trips)."""

    network_id: str
    connection_node: str
    asset_type: str
    steps_evaluated: int
    max_compliant_kw: float
    inflection_capacity_kw: Optional[float] = None
    first_binding_constraint: BindingConstraintCode = BindingConstraintCode.NONE
    halted_early: bool = False
    sweep_results: list[CapacitySweepPoint]


class ToolMetadataItem(BaseModel):
    """Lightweight discovery entry for progressive/lazy MCP tool loading."""

    name: str
    scope_required: Literal["tier1_only", "full"]
    cost_tier: Literal["catalog (<1ms)", "tier1_analytical (<10ms)", "tier2_solver (~150-300ms)"]
    summary: str


class ToolDiscoveryResponse(BaseModel):
    """Typed MCP output schema for `search_tools`."""

    active_scope: str
    matched_tools: list[ToolMetadataItem]


class ScreeningVerdict(BaseModel):
    """Complete auditable connection screening verdict."""

    application: ConnectionApplication
    decision: ClearanceDecision
    binding_constraints: list[BindingConstraintCode] = Field(
        default_factory=list,
        description="Explicit decoupled physical/solver constraint codes explaining which limit bound.",
    )
    requires_human_review: bool = Field(
        ...,
        description="True when human engineering review is mandatory (e.g., >=132 kV tier, >=5 MW capacity, borderline SCR, conditional envelope, regulatory override, or solver fault).",
    )
    human_review_reasons: list[str] = Field(
        default_factory=list,
        description="Explicit engineering or regulatory reasons requiring human-in-the-loop sign-off.",
    )
    regulatory_override: Optional[str] = Field(
        default=None,
        description="Explicit regulatory override flag (e.g., 'NER S5.2.5 System Strength violation'), when applicable.",
    )
    headline: str
    engineering_memo: str
    connection_point: ConnectionPointProfile
    regime: RegimeClassification
    tier1: Tier1ScreenResult
    tier2: Optional[Tier2OpenDSSResult] = None
    mitigations: list[MitigationOption] = Field(default_factory=list)
    sanity_gate: SanityGateReport
    audit_receipt: AuditReceipt
