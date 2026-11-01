"""Deterministic supervisory physics sanity gate and regulatory provenance builder.

Why this module exists ("Gate Bounds"):
    Even when AC power flow is delegated to deterministic solvers, raw numerical
    outputs can still misrepresent grid safety if:
      - an iterative AC solve diverges or hits a subprocess wall-clock timeout,
      - per-unit voltages fall outside credible physical bounds ([0.50, 1.40] pu),
      - active export at unity PF appears to depress voltage on a resistive LV feeder, or
      - steady-state voltage passes on a weak 132 kV fringe where post-contingency
        system strength (`N-1 SCR < 2.0`) violates NER S5.2.5 PLL stability limits.

    Every screening path passes through `evaluate_supervisory_sanity_gate` and
    `evaluate_human_review_requirement` before a `ScreeningVerdict` can be emitted.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import platform
import uuid
from datetime import datetime, timezone
from typing import Any

from grid_screening_agent import __version__
from grid_screening_agent.models import (
    AuditReceipt,
    BindingConstraintCode,
    ClearanceDecision,
    ConnectionApplication,
    ConnectionPointProfile,
    InverterMode,
    MitigationOption,
    SanityGateReport,
    Tier1ScreenResult,
    Tier2OpenDSSResult,
    VoltageTier,
)
from grid_screening_agent.physics.regime import EXPORT_ASSETS
from grid_screening_agent.physics.topology import load_reference_data


def evaluate_supervisory_sanity_gate(
    app: ConnectionApplication,
    profile: ConnectionPointProfile,
    tier1: Tier1ScreenResult,
    tier2: Tier2OpenDSSResult | None,
    proposed_decision: ClearanceDecision,
    mitigations: list[MitigationOption],
) -> tuple[ClearanceDecision, SanityGateReport]:
    """Audit numerical & physical invariants and enforce mandatory escalation boundaries."""
    checks_run = [
        "voltage_numerical_bounds_check",
        "solver_convergence_zero_failure_check",
        "resistive_injection_sign_invariant_check",
        "n1_scr_weak_grid_hard_gate",
        "statutory_clearance_consistency_check",
    ]
    anomalies: list[str] = []
    abstention = False
    fallback: str | None = None
    regulatory_override: str | None = None
    final_decision = proposed_decision

    # 1. Solver convergence & timeout check
    if tier2 is not None and (tier2.solver_failures > 0 or tier2.timed_out):
        reason = (
            f"Isolated AC solver timed out after {app.solver_timeout_sec:.1f}s; automatic clearance blocked."
            if tier2.timed_out
            else f"AC power-flow recorded {tier2.solver_failures} non-converged interval(s); automatic clearance blocked."
        )
        anomalies.append(reason)
        abstention = True
        final_decision = ClearanceDecision.ABSTAIN_DATA_OR_SOLVER_FAULT
        fallback = "Blocked clearance and marked ABSTAIN_DATA_OR_SOLVER_FAULT due to non-converged or timed-out AC solve."

    # 2. Numerical bounds check on voltages
    v_candidates = [tier1.predicted_peak_v_pu, tier1.predicted_min_v_pu]
    if tier2 is not None:
        v_candidates.extend([tier2.peak_voltage_pu, tier2.min_voltage_pu])

    for v in v_candidates:
        if v < 0.50 or v > 1.40:
            anomalies.append(f"Unphysical per-unit voltage {v:.4f} pu outside [0.50, 1.40] pu sanity window.")
            abstention = True
            final_decision = ClearanceDecision.ABSTAIN_DATA_OR_SOLVER_FAULT
            fallback = "Intercepted out-of-bounds voltage estimate."

    # 3. Physics sign check: active export at unity PF on resistive LV cannot depress peak voltage below 0.99 pu
    is_export = app.asset_type in EXPORT_ASSETS
    if (
        profile.voltage_tier == VoltageTier.LV_415V
        and is_export
        and app.inverter_mode == InverterMode.UNMANAGED_PF1
        and tier2 is not None
        and tier2.peak_voltage_pu < 0.99
    ):
        anomalies.append(
            f"Physics Sign Inversion: Unmanaged active export produced peak voltage {tier2.peak_voltage_pu:.4f} pu < 0.99 pu."
        )
        abstention = True
        final_decision = ClearanceDecision.ABSTAIN_DATA_OR_SOLVER_FAULT
        fallback = "Triggered physics-inversion halt."

    # 4. Weak-grid N-1 SCR < 2.0 hard escalation boundary (NER S5.2.5)
    if not abstention and tier1.scr_n1 < 2.0:
        regulatory_override = "NER S5.2.5 System Strength violation"
        if final_decision in (ClearanceDecision.FAST_TRACK_APPROVED, ClearanceDecision.CONDITIONAL_DOE_OR_CAP):
            anomalies.append(
                f"Weak-grid override: N-1 SCR ({tier1.scr_n1:.2f}) < 2.0 cannot be fast-tracked or DOE-cleared without EMT study."
            )
            final_decision = ClearanceDecision.ESCALATE_DETAILED_STUDY
            fallback = "Overrode decision to ESCALATE_DETAILED_STUDY (NER S5.2.5 System Strength violation: SCR < 2.0)."

    # 5. Statutory compliance consistency check
    if not abstention and final_decision == ClearanceDecision.FAST_TRACK_APPROVED:
        if tier1.flagged_for_tier2 and tier2 is None:
            anomalies.append("Tier-1 flagged risk but Tier-2 AC verification was not executed.")
            final_decision = ClearanceDecision.ESCALATE_DETAILED_STUDY
            fallback = "Downgraded unverified fast-track approval to ESCALATE_DETAILED_STUDY."
        elif tier2 is not None and (not tier2.statutory_voltage_compliant or not tier2.thermal_compliant):
            compliant_mit = any(m.compliant for m in mitigations)
            final_decision = (
                ClearanceDecision.CONDITIONAL_DOE_OR_CAP
                if compliant_mit
                else ClearanceDecision.ESCALATE_DETAILED_STUDY
            )
            anomalies.append("Blocked unconditional approval because unconstrained Tier-2 AC check breached limits.")
            fallback = f"Reclassified verdict to {final_decision.value} based on deterministic Tier-2 results."

    report = SanityGateReport(
        passed=(len(anomalies) == 0),
        checks_run=checks_run,
        anomalies_detected=anomalies,
        abstention_triggered=abstention,
        regulatory_override=regulatory_override,
        fallback_applied=fallback,
    )
    return final_decision, report


def derive_binding_constraints(
    tier1: Tier1ScreenResult,
    tier2: Tier2OpenDSSResult | None,
    sanity_report: SanityGateReport,
    final_decision: ClearanceDecision,
) -> list[BindingConstraintCode]:
    """Return explicit decoupled constraint codes distinguishing SCR, thermal, voltage, or solver faults."""
    if (
        sanity_report.abstention_triggered
        or final_decision == ClearanceDecision.ABSTAIN_DATA_OR_SOLVER_FAULT
        or (tier2 is not None and (tier2.solver_failures > 0 or tier2.timed_out))
    ):
        return [BindingConstraintCode.SOLVER_OR_DATA_FAULT]

    if final_decision == ClearanceDecision.FAST_TRACK_APPROVED:
        return [BindingConstraintCode.NONE]

    codes: list[BindingConstraintCode] = []
    if tier1.scr_n1 < 2.0 or sanity_report.regulatory_override:
        codes.append(BindingConstraintCode.SYSTEM_STRENGTH_SCR_N1)

    if tier2 is not None:
        if not tier2.statutory_voltage_compliant:
            codes.append(BindingConstraintCode.STEADY_STATE_VOLTAGE)
        if not tier2.thermal_compliant:
            codes.append(BindingConstraintCode.THERMAL_AMPACITY)
    else:
        if (
            tier1.predicted_peak_v_pu >= tier1.season_stratified_threshold_pu
            or tier1.predicted_min_v_pu <= (tier1.voltage_limit_min_pu + 0.015)
        ):
            codes.append(BindingConstraintCode.STEADY_STATE_VOLTAGE)
        if tier1.estimated_thermal_loading_pct >= 85.0:
            codes.append(BindingConstraintCode.THERMAL_AMPACITY)

    if not codes:
        if tier1.scr_n1 < 3.0:
            codes.append(BindingConstraintCode.SYSTEM_STRENGTH_SCR_N1)
        else:
            codes.append(BindingConstraintCode.NONE)
    return codes


def evaluate_human_review_requirement(
    app: ConnectionApplication,
    profile: ConnectionPointProfile,
    final_decision: ClearanceDecision,
    sanity_report: SanityGateReport,
    tier1: Tier1ScreenResult | None = None,
) -> tuple[bool, list[str]]:
    """Determine whether human-in-the-loop engineering review is mandatory before sign-off."""
    reasons: list[str] = []

    if profile.voltage_tier in (VoltageTier.TX_132KV, VoltageTier.TX_330_500KV):
        reasons.append(
            f"{profile.nominal_kv:g} kV sub-transmission/transmission connection always requires senior planning engineer sign-off."
        )
    if app.proposed_capacity_kw >= 5_000.0:
        reasons.append(
            f"Proposed capacity ({app.proposed_capacity_kw / 1000.0:.2f} MW) >= 5.0 MW large-connection threshold."
        )
    if tier1 is not None and 2.0 <= tier1.scr_n1 < 3.0:
        reasons.append(
            f"Borderline post-contingency system strength (N-1 SCR = {tier1.scr_n1:.2f} in [2.0, 3.0)) requires dynamic reactive / PLL stability review."
        )
    if sanity_report.regulatory_override:
        reasons.append(f"Regulatory hard-gate triggered: {sanity_report.regulatory_override}.")
    if final_decision != ClearanceDecision.FAST_TRACK_APPROVED:
        reasons.append(f"Decision '{final_decision.value}' requires conditional agreement or detailed engineering study.")
    if not sanity_report.passed:
        reasons.append("Supervisory Physics Sanity Gate recorded one or more anomalies/interceptions.")

    return (len(reasons) > 0), reasons


def _collect_runtime_versions() -> dict[str, str]:
    try:
        dss_ver = importlib.metadata.version("opendssdirect.py")
    except importlib.metadata.PackageNotFoundError:  # pragma: no cover
        dss_ver = "unknown"
    return {
        "grid_screening_agent": __version__,
        "python": platform.python_version(),
        "opendssdirect.py": dss_ver,
    }


def build_audit_receipt(
    app: ConnectionApplication,
    profile: ConnectionPointProfile,
    tier2: Tier2OpenDSSResult | None,
    orchestration_mode: str,
    tool_trace: list[dict[str, Any]],
    *,
    final_decision: ClearanceDecision | None = None,
    binding_constraints: list[BindingConstraintCode] | None = None,
    regulatory_override: str | None = None,
    requires_human_review: bool | None = None,
    tier1: Tier1ScreenResult | None = None,
    mitigations: list[MitigationOption] | None = None,
    queue_fingerprint: str = "",
    cache_hit: bool = False,
) -> AuditReceipt:
    """Create a deterministic regulatory provenance receipt with canonical reproducibility digest.

    Why `reproducibility_sha256` excludes UUIDs, wall-clock timestamps, and `execution_ms`:
        A TSO/DNSP auditor re-running the same application tomorrow must obtain the exact
        same 64-character SHA-256 verification hash (`reproducibility_sha256`), whereas
        `receipt_id`, `timestamp_utc`, `application_id`, and `cache_hit` track individual
        request handling in the interconnection queue.
    """
    ref = load_reference_data()
    conv_ok = True if tier2 is None else (tier2.solver_failures == 0)
    runtime_versions = _collect_runtime_versions()

    repro_payload = {
        "application": app.model_dump(exclude={"application_id", "solver_timeout_sec"}),
        "feeder_model_sha256": profile.model_sha256,
        "runtime_versions": runtime_versions,
        "decision": final_decision.value if final_decision is not None else None,
        "binding_constraints": [c.value for c in (binding_constraints or [])],
        "regulatory_override": regulatory_override,
        "requires_human_review": requires_human_review,
        "tier1": tier1.model_dump(exclude={"execution_ms"}) if tier1 is not None else None,
        "tier2": (
            tier2.model_dump(exclude={"execution_ms", "trajectory_resource_uri"})
            if tier2 is not None
            else None
        ),
        "mitigations": [m.model_dump() for m in (mitigations or [])],
    }
    canonical_bytes = json.dumps(repro_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    repro_sha256 = hashlib.sha256(canonical_bytes).hexdigest()

    return AuditReceipt(
        receipt_id=f"AUD-{uuid.uuid4().hex[:10].upper()}",
        timestamp_utc=datetime.now(timezone.utc).isoformat(),
        application_id=app.application_id,
        feeder_model_sha256=profile.model_sha256,
        reproducibility_sha256=repro_sha256,
        runtime_versions=runtime_versions,
        queue_fingerprint=queue_fingerprint,
        cache_hit=cache_hit,
        orchestration_mode=orchestration_mode,
        standards_checked=list(ref["metadata"]["standards"]),
        solver_convergence_verified=conv_ok,
        calibration_convention="Season-stratified 4-season causal calibration (eliminates Winter-only threshold bias)",
        tool_trace=tool_trace,
    )
