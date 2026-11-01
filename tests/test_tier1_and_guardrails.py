"""Tests for Tier-1 season-stratified screening, decoupled constraints, reproducibility, and Sanity Gate (13 tests)."""

from __future__ import annotations

import pytest

from grid_screening_agent.guardrails import (
    build_audit_receipt,
    derive_binding_constraints,
    evaluate_human_review_requirement,
    evaluate_supervisory_sanity_gate,
)
from grid_screening_agent.models import (
    AssetType,
    BindingConstraintCode,
    ClearanceDecision,
    ConnectionApplication,
    InverterMode,
    Tier2OpenDSSResult,
)
from grid_screening_agent.physics.regime import classify_screening_regime
from grid_screening_agent.physics.tier1_screen import run_tier1_fast_screen, wilson_ci_pct
from grid_screening_agent.physics.topology import inspect_connection_point


def test_season_stratified_calibration_eliminates_winter_false_alarm() -> None:
    app = ConnectionApplication(
        network_id="R",
        asset_type=AssetType.ROOFTOP_PV,
        proposed_capacity_kw=3.0,
        feeder_background_scale=3.0,
    )
    prof = inspect_connection_point("R")
    reg = classify_screening_regime(prof, app.asset_type, app.proposed_capacity_kw)
    t1 = run_tier1_fast_screen(app, prof, reg)

    assert t1.winter_biased_threshold_pu == pytest.approx(1.030377, abs=1e-4)
    assert t1.season_stratified_threshold_pu == pytest.approx(1.0752, abs=1e-4)
    assert t1.winter_biased_threshold_pu < t1.predicted_peak_v_pu < t1.season_stratified_threshold_pu
    assert t1.fast_clear_eligible is True
    assert t1.flagged_for_tier2 is False


def test_tier1_flags_stressed_lv_pv_plus_bess() -> None:
    app_lv = ConnectionApplication(
        network_id="R",
        asset_type=AssetType.PV_PLUS_BESS,
        proposed_capacity_kw=20.0,
        bess_power_kw=12.0,
        feeder_background_scale=6.0,
    )
    prof_lv = inspect_connection_point("R")
    reg_lv = classify_screening_regime(prof_lv, app_lv.asset_type, app_lv.proposed_capacity_kw)
    t1_lv = run_tier1_fast_screen(app_lv, prof_lv, reg_lv)
    assert t1_lv.flagged_for_tier2 is True
    assert t1_lv.fast_clear_eligible is False
    assert any("BESS" in r for r in t1_lv.flag_reasons)


def test_tier1_flags_weak_132kv_scr_violation() -> None:
    app_tx = ConnectionApplication(
        network_id="BrokenHill_132kV",
        asset_type=AssetType.HYPERSCALE_DATACENTRE,
        proposed_capacity_kw=150_000.0,
    )
    prof_tx = inspect_connection_point("BrokenHill_132kV")
    reg_tx = classify_screening_regime(prof_tx, app_tx.asset_type, app_tx.proposed_capacity_kw)
    t1_tx = run_tier1_fast_screen(app_tx, prof_tx, reg_tx)
    assert t1_tx.scr_n1 == pytest.approx(1.26, abs=0.02)
    assert t1_tx.hard_escalation_reason is not None
    assert t1_tx.single_constraint_error_penalty_pct > 10.0


def test_tier1_mv_operating_state_dominance() -> None:
    prof_mv = inspect_connection_point("CIGRE_MV_Rudolstadt")
    reg_mv = classify_screening_regime(prof_mv, AssetType.EV_OR_COMMERCIAL_LOAD, 3000.0)
    app_light = ConnectionApplication(
        network_id="CIGRE_MV_Rudolstadt",
        asset_type=AssetType.EV_OR_COMMERCIAL_LOAD,
        proposed_capacity_kw=3000.0,
        operating_load_factor=0.20,
    )
    app_heavy = app_light.model_copy(update={"operating_load_factor": 0.95})
    t1_light = run_tier1_fast_screen(app_light, prof_mv, reg_mv)
    t1_heavy = run_tier1_fast_screen(app_heavy, prof_mv, reg_mv)
    assert t1_heavy.predicted_min_v_pu < t1_light.predicted_min_v_pu
    assert t1_heavy.flagged_for_tier2 is True


def test_tier1_ehv_500kv_pareto_headroom_penalty() -> None:
    prof_bw = inspect_connection_point("Bayswater_500kV")
    reg_bw = classify_screening_regime(prof_bw, AssetType.HYPERSCALE_DATACENTRE, 400_000.0)
    app_bw = ConnectionApplication(
        network_id="Bayswater_500kV",
        asset_type=AssetType.HYPERSCALE_DATACENTRE,
        proposed_capacity_kw=400_000.0,
    )
    t1_bw = run_tier1_fast_screen(app_bw, prof_bw, reg_bw)
    assert t1_bw.scr_n1 > 3.0
    assert t1_bw.voltage_only_headroom_kw > t1_bw.pareto_joint_headroom_kw
    assert t1_bw.single_constraint_error_penalty_pct > 50.0


def test_wilson_ci_bounds_and_clamping() -> None:
    lo, hi = wilson_ci_pct(0, 4368)
    assert lo == 0.0
    assert 0.05 < hi < 0.12
    assert wilson_ci_pct(0, 0) == (0.0, 0.0)
    lo_hi_over = wilson_ci_pct(999_999, 4368)
    assert 99.0 < lo_hi_over[0] <= 100.0
    assert lo_hi_over[1] == 100.0
    assert wilson_ci_pct(-50, 4368)[0] == 0.0


def test_sanity_gate_intercepts_nonzero_solver_failures_and_timeout() -> None:
    app = ConnectionApplication(network_id="R", proposed_capacity_kw=10.0, feeder_background_scale=5.5)
    prof = inspect_connection_point("R")
    reg = classify_screening_regime(prof, app.asset_type, app.proposed_capacity_kw)
    t1 = run_tier1_fast_screen(app, prof, reg)

    faulty_t2 = Tier2OpenDSSResult(
        executed=True,
        solver_engine="TestMock",
        intervals_solved=16,
        solver_failures=2,
        timed_out=True,
        peak_voltage_pu=1.05,
        min_voltage_pu=0.98,
        worst_season="Summer",
        worst_timestamp="2013-01-01T12:00:00",
        violations_count=0,
        max_line_loading_pct=40.0,
        transformer_loading_pct=35.0,
        statutory_voltage_compliant=False,
        thermal_compliant=True,
        execution_ms=10.0,
    )
    dec, rep = evaluate_supervisory_sanity_gate(
        app, prof, t1, faulty_t2, ClearanceDecision.FAST_TRACK_APPROVED, []
    )
    assert dec == ClearanceDecision.ABSTAIN_DATA_OR_SOLVER_FAULT
    assert rep.abstention_triggered is True
    assert not rep.passed


def test_sanity_gate_intercepts_unphysical_voltage_bounds() -> None:
    app = ConnectionApplication(network_id="R", proposed_capacity_kw=10.0)
    prof = inspect_connection_point("R")
    reg = classify_screening_regime(prof, app.asset_type, app.proposed_capacity_kw)
    t1 = run_tier1_fast_screen(app, prof, reg)
    corrupt_t2 = Tier2OpenDSSResult(
        executed=True,
        solver_engine="TestMock",
        intervals_solved=1,
        solver_failures=0,
        peak_voltage_pu=230.0,
        min_voltage_pu=0.98,
        worst_season="Summer",
        worst_timestamp="2013-01-01T12:00:00",
        violations_count=1,
        max_line_loading_pct=20.0,
        transformer_loading_pct=15.0,
        statutory_voltage_compliant=False,
        thermal_compliant=True,
        execution_ms=5.0,
    )
    dec, rep = evaluate_supervisory_sanity_gate(
        app, prof, t1, corrupt_t2, ClearanceDecision.FAST_TRACK_APPROVED, []
    )
    assert dec == ClearanceDecision.ABSTAIN_DATA_OR_SOLVER_FAULT
    assert any("Unphysical per-unit voltage" in a for a in rep.anomalies_detected)


def test_sanity_gate_intercepts_resistive_sign_inversion() -> None:
    app = ConnectionApplication(
        network_id="R",
        asset_type=AssetType.ROOFTOP_PV,
        proposed_capacity_kw=10.0,
        inverter_mode=InverterMode.UNMANAGED_PF1,
    )
    prof = inspect_connection_point("R")
    reg = classify_screening_regime(prof, app.asset_type, app.proposed_capacity_kw)
    t1 = run_tier1_fast_screen(app, prof, reg)
    inverted_t2 = Tier2OpenDSSResult(
        executed=True,
        solver_engine="TestMock",
        intervals_solved=16,
        solver_failures=0,
        peak_voltage_pu=0.965,
        min_voltage_pu=0.95,
        worst_season="Summer",
        worst_timestamp="2013-01-01T12:00:00",
        violations_count=0,
        max_line_loading_pct=20.0,
        transformer_loading_pct=15.0,
        statutory_voltage_compliant=True,
        thermal_compliant=True,
        execution_ms=5.0,
    )
    dec, rep = evaluate_supervisory_sanity_gate(
        app, prof, t1, inverted_t2, ClearanceDecision.FAST_TRACK_APPROVED, []
    )
    assert dec == ClearanceDecision.ABSTAIN_DATA_OR_SOLVER_FAULT
    assert any("Sign Inversion" in a for a in rep.anomalies_detected)


def test_sanity_gate_enforces_ner_s525_regulatory_override_and_human_review() -> None:
    app_bh = ConnectionApplication(
        network_id="BrokenHill_132kV",
        asset_type=AssetType.HYPERSCALE_DATACENTRE,
        proposed_capacity_kw=150_000.0,
    )
    prof_bh = inspect_connection_point("BrokenHill_132kV")
    reg_bh = classify_screening_regime(prof_bh, app_bh.asset_type, app_bh.proposed_capacity_kw)
    t1_bh = run_tier1_fast_screen(app_bh, prof_bh, reg_bh)
    dec, rep = evaluate_supervisory_sanity_gate(
        app_bh, prof_bh, t1_bh, None, ClearanceDecision.FAST_TRACK_APPROVED, []
    )
    assert dec == ClearanceDecision.ESCALATE_DETAILED_STUDY
    assert rep.regulatory_override == "NER S5.2.5 System Strength violation"

    needs_hr, hr_reasons = evaluate_human_review_requirement(app_bh, prof_bh, dec, rep)
    assert needs_hr is True
    assert any("NER S5.2.5 System Strength violation" in r for r in hr_reasons)


def test_scr_201_borderline_caution_flags_tier2_and_human_review_across_tiers() -> None:
    # 1. 132 kV Broken Hill at 93.78 MW -> S_sc_n1 = 188.5 MVA => SCR_N1 = 2.01
    prof_bh = inspect_connection_point("BrokenHill_132kV")
    app_bh_201 = ConnectionApplication(
        network_id="BrokenHill_132kV",
        asset_type=AssetType.UTILITY_IBR_PLANT,
        proposed_capacity_kw=93_780.0,
    )
    reg_bh = classify_screening_regime(prof_bh, app_bh_201.asset_type, app_bh_201.proposed_capacity_kw)
    t1_bh = run_tier1_fast_screen(app_bh_201, prof_bh, reg_bh)
    assert t1_bh.scr_n1 == pytest.approx(2.01, abs=0.02)
    assert t1_bh.hard_escalation_reason is None
    assert t1_bh.flagged_for_tier2 is True
    assert t1_bh.fast_clear_eligible is False

    # 2. 11 kV MV feeder at SCR_N1 ~= 2.01 -> must flag Tier-2 AND require human review even if AC passes
    prof_mv = inspect_connection_point("Aus_MV_11kV_Suburban")
    cap_kw_201 = (prof_mv.s_sc_n1_mva / 2.01) * 1000.0
    app_mv_201 = ConnectionApplication(
        network_id="Aus_MV_11kV_Suburban",
        asset_type=AssetType.BESS_ONLY,
        proposed_capacity_kw=round(cap_kw_201, 2),
        operating_load_factor=0.50,
    )
    reg_mv = classify_screening_regime(prof_mv, app_mv_201.asset_type, app_mv_201.proposed_capacity_kw)
    t1_mv = run_tier1_fast_screen(app_mv_201, prof_mv, reg_mv)
    assert t1_mv.scr_n1 == pytest.approx(2.01, abs=0.02)
    assert t1_mv.flagged_for_tier2 is True
    assert t1_mv.fast_clear_eligible is False
    assert any("Moderate MV N-1 SCR" in r for r in t1_mv.flag_reasons)

    # Even if a small <5 MW MV plant had SCR_N1 = 2.5 and passed Tier-2 FAST_TRACK_APPROVED, human review is forced
    app_small_mv = app_mv_201.model_copy(update={"proposed_capacity_kw": 2000.0})
    t1_borderline = t1_mv.model_copy(update={"scr_n1": 2.01, "flagged_for_tier2": False})
    dec_ok, rep_ok = evaluate_supervisory_sanity_gate(
        app_small_mv, prof_mv, t1_borderline, None, ClearanceDecision.FAST_TRACK_APPROVED, []
    )
    needs_hr, hr_reasons = evaluate_human_review_requirement(
        app_small_mv, prof_mv, dec_ok, rep_ok, tier1=t1_borderline
    )
    assert needs_hr is True
    assert any("Borderline post-contingency system strength" in r for r in hr_reasons)


def test_derive_binding_constraints_decouples_thermal_vs_voltage_vs_scr() -> None:
    prof_wg = inspect_connection_point("Wangaratta_132kV")
    app_wg = ConnectionApplication(
        network_id="Wangaratta_132kV",
        asset_type=AssetType.HYPERSCALE_DATACENTRE,
        proposed_capacity_kw=160_000.0,
    )
    reg_wg = classify_screening_regime(prof_wg, app_wg.asset_type, app_wg.proposed_capacity_kw)
    t1_wg = run_tier1_fast_screen(app_wg, prof_wg, reg_wg)

    # Thermal breach while steady-state voltage passes
    t2_thermal_only = Tier2OpenDSSResult(
        executed=True,
        solver_engine="TestMock",
        intervals_solved=8,
        solver_failures=0,
        peak_voltage_pu=1.01,
        min_voltage_pu=0.96,
        worst_season="Summer",
        worst_timestamp="2026-01-22T15:00:00",
        violations_count=0,
        max_line_loading_pct=128.4,
        transformer_loading_pct=118.1,
        statutory_voltage_compliant=True,
        thermal_compliant=False,
        execution_ms=2.0,
    )
    dec_th, rep_th = evaluate_supervisory_sanity_gate(
        app_wg, prof_wg, t1_wg, t2_thermal_only, ClearanceDecision.ESCALATE_DETAILED_STUDY, []
    )
    codes_th = derive_binding_constraints(t1_wg, t2_thermal_only, rep_th, dec_th)
    assert codes_th == [BindingConstraintCode.THERMAL_AMPACITY]

    # Voltage breach while thermal ampacity passes
    t2_voltage_only = t2_thermal_only.model_copy(
        update={
            "peak_voltage_pu": 1.125,
            "violations_count": 3,
            "max_line_loading_pct": 54.0,
            "transformer_loading_pct": 48.0,
            "statutory_voltage_compliant": False,
            "thermal_compliant": True,
        }
    )
    dec_v, rep_v = evaluate_supervisory_sanity_gate(
        app_wg, prof_wg, t1_wg, t2_voltage_only, ClearanceDecision.CONDITIONAL_DOE_OR_CAP, []
    )
    codes_v = derive_binding_constraints(t1_wg, t2_voltage_only, rep_v, dec_v)
    assert codes_v == [BindingConstraintCode.STEADY_STATE_VOLTAGE]


def test_audit_receipt_reproducibility_sha256_is_invariant_across_runs_and_sensitive_to_inputs() -> None:
    prof_r = inspect_connection_point("R")
    app_day1 = ConnectionApplication(
        application_id="APP-DAY1AAAA",
        network_id="R",
        connection_node="3110858",
        proposed_capacity_kw=5.0,
        feeder_background_scale=3.0,
        solver_timeout_sec=15.0,
    )
    app_day2 = ConnectionApplication(
        application_id="APP-DAY2BBBB",
        network_id="R",
        connection_node="3110858",
        proposed_capacity_kw=5.0,
        feeder_background_scale=3.0,
        solver_timeout_sec=45.0,
    )
    reg = classify_screening_regime(prof_r, app_day1.asset_type, app_day1.proposed_capacity_kw)
    t1_a = run_tier1_fast_screen(app_day1, prof_r, reg).model_copy(update={"execution_ms": 1.11})
    t1_b = run_tier1_fast_screen(app_day2, prof_r, reg).model_copy(update={"execution_ms": 9.87})

    rec1 = build_audit_receipt(
        app_day1,
        prof_r,
        None,
        "progressive_fidelity (deterministic)",
        [{"step": 1, "tool": "inspect_connection_point"}],
        final_decision=ClearanceDecision.FAST_TRACK_APPROVED,
        binding_constraints=[BindingConstraintCode.NONE],
        regulatory_override=None,
        requires_human_review=False,
        tier1=t1_a,
        mitigations=[],
    )
    rec2 = build_audit_receipt(
        app_day2,
        prof_r,
        None,
        "progressive_fidelity (gemini)",
        [{"step": 1, "tool": "inspect_connection_point"}, {"step": 2, "tool": "memo"}],
        final_decision=ClearanceDecision.FAST_TRACK_APPROVED,
        binding_constraints=[BindingConstraintCode.NONE],
        regulatory_override=None,
        requires_human_review=False,
        tier1=t1_b,
        mitigations=[],
    )
    assert rec1.receipt_id != rec2.receipt_id
    assert len(rec1.reproducibility_sha256) == 64
    assert rec1.reproducibility_sha256 == rec2.reproducibility_sha256
    assert "opendssdirect.py" in rec1.runtime_versions

    app_changed = app_day1.model_copy(update={"proposed_capacity_kw": 5.5})
    rec3 = build_audit_receipt(
        app_changed,
        prof_r,
        None,
        "progressive_fidelity (deterministic)",
        [],
        final_decision=ClearanceDecision.FAST_TRACK_APPROVED,
        binding_constraints=[BindingConstraintCode.NONE],
        regulatory_override=None,
        requires_human_review=False,
        tier1=t1_a,
        mitigations=[],
    )
    assert rec3.reproducibility_sha256 != rec1.reproducibility_sha256
