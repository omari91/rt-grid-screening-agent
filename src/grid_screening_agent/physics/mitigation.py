"""Physics-verified conditional connection envelope and network mitigation synthesizer.

Why mitigations are solver-verified and distinguish static DOEs from live WLT:
    1. Rejecting Hallucinated Export Caps: An LLM cannot guess whether a `5.0 kW` or
       `3.0 kW` Static Operating Envelope (SOE) resolves a feeder breach. Every candidate
       cap is re-solved through `run_tier2_ac_study(..., export_cap_kw=cap_kw)` so
       `compliant=True` is backed by converged unbalanced 3-phase AC power flow and
       conductor thermal checks.
    2. Snapshot/Diurnal DOE vs Live Telemetry Warning Lead Time (WLT): Offline connection
       screening does not ingest real-time SCADA/AMI streams or run a 17,520-interval
       SSID-Kalman state estimator. Therefore `DOE_SNAPSHOT_ENVELOPE` explicitly sets
       `telemetry_forecast_active=False` and labels `envelope_basis` (`snapshot_static_bound`
       or `diurnal_time_series_bound`) so utilities never confuse planning envelopes with
       operational predictive dispatch.
"""

from __future__ import annotations

from grid_screening_agent.models import (
    ConnectionApplication,
    ConnectionPointProfile,
    MitigationOption,
    RegimeClassification,
    StudyType,
    Tier1ScreenResult,
    Tier2OpenDSSResult,
    VoltageTier,
)
from grid_screening_agent.physics.opendss_worker import run_tier2_ac_study
from grid_screening_agent.physics.regime import EXPORT_ASSETS


def propose_connection_envelopes(
    app: ConnectionApplication,
    profile: ConnectionPointProfile,
    regime: RegimeClassification,
    tier1: Tier1ScreenResult,
    tier2: Tier2OpenDSSResult | None = None,
) -> list[MitigationOption]:
    """Evaluate concrete conditional approval envelopes when unconstrained connection binds."""
    if tier2 is not None and tier2.solver_failures > 0:
        return []
    options: list[MitigationOption] = []
    is_export = app.asset_type in EXPORT_ASSETS

    # --- LV 415 V Distribution Envelopes (OpenDSS-verified) ---
    basis_label = (
        "diurnal_time_series_bound"
        if app.study_type == StudyType.TIME_SERIES
        else "snapshot_static_bound"
    )
    if profile.voltage_tier == VoltageTier.LV_415V and is_export:
        for cap_kw, label in [(5.0, "5.0 kW Static Operating Envelope"), (3.0, "3.0 kW Conservative Static Cap")]:
            sim = run_tier2_ac_study(app, profile, export_cap_kw=cap_kw)
            ok = sim.statutory_voltage_compliant and sim.thermal_compliant and (sim.solver_failures == 0)
            options.append(
                MitigationOption(
                    option_id=f"SOE_{int(cap_kw)}KW",
                    title=label,
                    mechanism=f"Per-NMI active export limit of {cap_kw:.1f} kW at PCC with AS/NZS 4777.2 droop",
                    export_cap_kw=cap_kw,
                    dynamic_envelope_enabled=False,
                    envelope_basis=basis_label,
                    telemetry_forecast_active=False,
                    resulting_peak_v_pu=sim.peak_voltage_pu,
                    resulting_loading_pct=max(sim.max_line_loading_pct, sim.transformer_loading_pct),
                    resulting_scr_n1=tier1.scr_n1,
                    compliant=ok,
                    recommendation_notes=(
                        f"OpenDSS-verified peak V = {sim.peak_voltage_pu:.4f} pu "
                        f"({'<= 1.10 pu compliant' if ok else 'still exceeds feeder headroom'})."
                    ),
                )
            )

        # Snapshot/diurnal-bounded Dynamic Operating Envelope (explicitly NOT a live telemetry WLT forecast)
        doe_ref = (
            options[0]
            if (options and options[0].compliant)
            else (options[1] if len(options) > 1 else None)
        )
        doe_floor_kw = doe_ref.export_cap_kw if (doe_ref and doe_ref.export_cap_kw is not None) else 3.0
        doe_peak_v = doe_ref.resulting_peak_v_pu if doe_ref else 1.06
        doe_loading = doe_ref.resulting_loading_pct if doe_ref else 75.0
        options.append(
            MitigationOption(
                option_id="DOE_SNAPSHOT_ENVELOPE",
                title="Dynamic Operating Envelope (Snapshot-Bounded Ceiling/Floor)",
                mechanism=(
                    f"Allow up to {app.proposed_capacity_kw:.1f} kW export in unconstrained off-peak intervals, "
                    f"stepping down to {doe_floor_kw:.1f} kW during peak solar windows. "
                    f"Envelope floor is derived from deterministic AC {app.study_type.value} bounds "
                    "(not a live 17,520-interval SSID-Kalman telemetry WLT forecast)."
                ),
                export_cap_kw=doe_floor_kw,
                dynamic_envelope_enabled=True,
                envelope_basis=basis_label,
                telemetry_forecast_active=False,
                resulting_peak_v_pu=doe_peak_v,
                resulting_loading_pct=doe_loading,
                resulting_scr_n1=tier1.scr_n1,
                compliant=bool(doe_peak_v is not None and doe_peak_v <= 1.10 and (doe_loading or 0.0) <= 100.0),
                recommendation_notes=(
                    "Static/snapshot-bounded DOE specification (`telemetry_forecast_active=false`). "
                    "Operational Warning Lead Time (WLT) dispatch requires external live SCADA/AMI telemetry."
                ),
            )
        )
        return options

    # --- MV / Wholesale Transmission Envelopes ---
    safe_kw = max(0.0, round(tier1.pareto_joint_headroom_kw * 0.92, 1))
    unit = "MW" if profile.nominal_kv >= 66.0 else "kW"
    disp_val = safe_kw / 1000.0 if unit == "MW" else safe_kw

    if tier1.scr_n1 < 2.0:
        min_s_sc_needed = (app.proposed_capacity_kw / 1000.0) * 2.5
        add_mva = max(0.0, min_s_sc_needed - profile.s_sc_n1_mva)
        options.append(
            MitigationOption(
                option_id="TX_SYSTEM_STRENGTH_REMEDIATION",
                title="System Strength Remediation (Synchronous Condenser / Grid-Forming BESS)",
                mechanism=(
                    f"Procure +{add_mva:.0f} MVA post-contingency fault level contribution or grid-forming (GFM) "
                    "inverter tuning to lift N-1 SCR above 2.5 under NER S5.2.5."
                ),
                export_cap_kw=safe_kw if safe_kw > 0 else None,
                dynamic_envelope_enabled=False,
                envelope_basis="tx_n1_headroom",
                telemetry_forecast_active=False,
                resulting_peak_v_pu=tier1.predicted_peak_v_pu,
                resulting_loading_pct=tier1.estimated_thermal_loading_pct,
                resulting_scr_n1=2.5,
                compliant=False,
                recommendation_notes="Requires AEMO/TNSP full EMT (PSCAD) study before unconditional connection.",
            )
        )

    if safe_kw > 0 and safe_kw < app.proposed_capacity_kw:
        sim_staged = run_tier2_ac_study(app, profile, export_cap_kw=safe_kw)
        ok_staged = (
            sim_staged.statutory_voltage_compliant
            and sim_staged.thermal_compliant
            and (profile.s_sc_n1_mva / max(safe_kw / 1000.0, 1e-6) >= 2.0)
        )
        options.append(
            MitigationOption(
                option_id="STAGED_CAPACITY_OR_RUNBACK",
                title=f"Staged Connection / Special Protection Scheme ({disp_val:.1f} {unit} Firm)",
                mechanism=(
                    f"Approve {disp_val:.1f} {unit} firm capacity within Pareto joint headroom, with automated "
                    "post-contingency N-1 runback / flexible load curtailment above that threshold."
                ),
                export_cap_kw=safe_kw,
                dynamic_envelope_enabled=True,
                envelope_basis="tx_n1_headroom",
                telemetry_forecast_active=False,
                resulting_peak_v_pu=sim_staged.peak_voltage_pu,
                resulting_loading_pct=sim_staged.max_line_loading_pct,
                resulting_scr_n1=round(profile.s_sc_n1_mva / max(safe_kw / 1000.0, 1e-6), 2),
                compliant=ok_staged,
                recommendation_notes=(
                    f"Bounds primary constraint ({regime.primary_binding_constraint}) within N-1 limits."
                ),
            )
        )

    return options
