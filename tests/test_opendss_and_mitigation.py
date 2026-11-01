"""Integration tests for isolated OpenDSS worker, Volt-VAr I_q thermal readout, trajectories, and mitigations (10 tests)."""

from __future__ import annotations

import os

from grid_screening_agent.models import (
    AssetType,
    ConnectionApplication,
    InverterMode,
    StudyType,
    Tier2OpenDSSResult,
)
from grid_screening_agent.physics.mitigation import propose_connection_envelopes
from grid_screening_agent.physics.opendss_worker import run_tier2_ac_study
from grid_screening_agent.physics.regime import classify_screening_regime
from grid_screening_agent.physics.tier1_screen import run_tier1_fast_screen
from grid_screening_agent.physics.topology import inspect_connection_point


def test_isolated_opendss_network_r_overvoltage_breach() -> None:
    cwd_before = os.getcwd()
    app_r = ConnectionApplication(
        network_id="R",
        asset_type=AssetType.PV_PLUS_BESS,
        proposed_capacity_kw=20.0,
        bess_power_kw=12.0,
        feeder_background_scale=6.0,
        inverter_mode=InverterMode.UNMANAGED_PF1,
    )
    prof_r = inspect_connection_point("R")
    res_r = run_tier2_ac_study(app_r, prof_r)

    assert os.getcwd() == cwd_before
    assert res_r.executed is True
    assert res_r.solver_failures == 0
    assert res_r.peak_voltage_pu > 1.10
    assert res_r.statutory_voltage_compliant is False
    assert res_r.bess_cliff_delta_v_pu > 0.01


def test_isolated_opendss_network_h_topological_voltage_immunity() -> None:
    cwd_before = os.getcwd()
    app_h = ConnectionApplication(
        network_id="H",
        asset_type=AssetType.ROOFTOP_PV,
        proposed_capacity_kw=15.0,
        feeder_background_scale=6.0,
        inverter_mode=InverterMode.UNMANAGED_PF1,
    )
    prof_h = inspect_connection_point("H")
    res_h = run_tier2_ac_study(app_h, prof_h)

    assert os.getcwd() == cwd_before
    assert res_h.solver_failures == 0
    assert res_h.peak_voltage_pu < 1.10
    assert res_h.statutory_voltage_compliant is True


def test_study_type_snapshot_vs_time_series_scope_lv() -> None:
    prof_r = inspect_connection_point("R")
    app_snap = ConnectionApplication(
        network_id="R",
        proposed_capacity_kw=10.0,
        feeder_background_scale=5.5,
        study_type=StudyType.SNAPSHOT,
    )
    app_ts = app_snap.model_copy(update={"study_type": StudyType.TIME_SERIES})
    res_snap = run_tier2_ac_study(app_snap, prof_r)
    res_ts = run_tier2_ac_study(app_ts, prof_r)

    assert res_snap.study_type == StudyType.SNAPSHOT
    assert res_ts.study_type == StudyType.TIME_SERIES
    assert res_snap.intervals_solved == 17
    assert res_ts.intervals_solved > 50
    assert "excludes full 17,520-interval" in res_snap.study_scope
    assert "diurnal trajectory" in res_ts.study_scope


def test_study_type_snapshot_vs_time_series_scope_mv_tx() -> None:
    prof_mv = inspect_connection_point("CIGRE_MV_Rudolstadt")
    app_snap = ConnectionApplication(
        network_id="CIGRE_MV_Rudolstadt",
        asset_type=AssetType.EV_OR_COMMERCIAL_LOAD,
        proposed_capacity_kw=3000.0,
        study_type=StudyType.SNAPSHOT,
    )
    app_ts = app_snap.model_copy(update={"study_type": StudyType.TIME_SERIES})
    res_snap = run_tier2_ac_study(app_snap, prof_mv)
    res_ts = run_tier2_ac_study(app_ts, prof_mv)

    assert res_snap.intervals_solved == 8
    assert res_ts.intervals_solved == 96


def test_as4777_volt_var_watt_droop_curtailment_and_q_saturation() -> None:
    app = ConnectionApplication(
        network_id="R",
        asset_type=AssetType.PV_PLUS_BESS,
        proposed_capacity_kw=25.0,
        bess_power_kw=15.0,
        feeder_background_scale=6.0,
        inverter_mode=InverterMode.AS4777_VOLT_VAR_WATT,
    )
    prof = inspect_connection_point("R")
    t2 = run_tier2_ac_study(app, prof)
    assert t2.as4777_q_saturated is True
    assert t2.as4777_curtailment_pct > 0.0


def test_lv_doe_snapshot_envelope_disclaims_telemetry_wlt() -> None:
    app = ConnectionApplication(
        network_id="R",
        asset_type=AssetType.PV_PLUS_BESS,
        proposed_capacity_kw=25.0,
        bess_power_kw=15.0,
        feeder_background_scale=6.0,
        inverter_mode=InverterMode.AS4777_VOLT_VAR_WATT,
    )
    prof = inspect_connection_point("R")
    reg = classify_screening_regime(prof, app.asset_type, app.proposed_capacity_kw)
    t1 = run_tier1_fast_screen(app, prof, reg)
    t2 = run_tier2_ac_study(app, prof)

    options = propose_connection_envelopes(app, prof, reg, t1, t2)
    assert {o.option_id for o in options} == {"SOE_5KW", "SOE_3KW", "DOE_SNAPSHOT_ENVELOPE"}
    doe = next(o for o in options if o.option_id == "DOE_SNAPSHOT_ENVELOPE")
    assert doe.telemetry_forecast_active is False
    assert doe.envelope_basis == "snapshot_static_bound"
    assert "not a live 17,520-interval" in doe.mechanism
    assert any(o.compliant for o in options)


def test_tx_system_strength_and_staged_runback_mitigations() -> None:
    app = ConnectionApplication(
        network_id="BrokenHill_132kV",
        asset_type=AssetType.HYPERSCALE_DATACENTRE,
        proposed_capacity_kw=150_000.0,
    )
    prof = inspect_connection_point("BrokenHill_132kV")
    reg = classify_screening_regime(prof, app.asset_type, app.proposed_capacity_kw)
    t1 = run_tier1_fast_screen(app, prof, reg)
    opts = propose_connection_envelopes(app, prof, reg, t1, None)
    ids = {o.option_id for o in opts}
    assert "TX_SYSTEM_STRENGTH_REMEDIATION" in ids
    assert "STAGED_CAPACITY_OR_RUNBACK" in ids


def test_mitigation_short_circuits_on_diverged_tier2() -> None:
    app = ConnectionApplication(network_id="R", proposed_capacity_kw=10.0)
    prof = inspect_connection_point("R")
    reg = classify_screening_regime(prof, app.asset_type, app.proposed_capacity_kw)
    t1 = run_tier1_fast_screen(app, prof, reg)
    diverged = Tier2OpenDSSResult(
        executed=True,
        solver_engine="TestMock",
        intervals_solved=0,
        solver_failures=1,
        peak_voltage_pu=1.0,
        min_voltage_pu=1.0,
        worst_season="Unknown",
        worst_timestamp="",
        violations_count=1,
        max_line_loading_pct=0.0,
        transformer_loading_pct=0.0,
        statutory_voltage_compliant=False,
        thermal_compliant=False,
        execution_ms=1.0,
    )
    assert propose_connection_envelopes(app, prof, reg, t1, diverged) == []


def test_post_droop_volt_var_reactive_current_increases_thermal_loading() -> None:
    """Q8 Critical Fix: absorbing inductive Q under Volt-VAr lowers V_peak while increasing I_q ampacity & kVA."""
    prof_r = inspect_connection_point("R")
    app_pf1 = ConnectionApplication(
        network_id="R",
        asset_type=AssetType.ROOFTOP_PV,
        proposed_capacity_kw=85.0,
        feeder_background_scale=0.5,
        inverter_mode=InverterMode.UNMANAGED_PF1,
        seasons=["Summer"],
    )
    app_vv = app_pf1.model_copy(update={"inverter_mode": InverterMode.AS4777_VOLT_VAR})

    res_pf1 = run_tier2_ac_study(app_pf1, prof_r)
    res_vv = run_tier2_ac_study(app_vv, prof_r)

    assert res_vv.as4777_curtailment_pct == 0.0
    assert res_vv.peak_voltage_pu < res_pf1.peak_voltage_pu - 0.05
    assert res_vv.max_line_loading_pct > res_pf1.max_line_loading_pct + 8.0
    assert res_vv.transformer_loading_pct > res_pf1.transformer_loading_pct


def test_tier2_populates_voltage_profile_trajectory_lv_and_mv() -> None:
    prof_r = inspect_connection_point("R")
    res_lv = run_tier2_ac_study(
        ConnectionApplication(network_id="R", proposed_capacity_kw=10.0, feeder_background_scale=5.5),
        prof_r,
    )
    assert len(res_lv.voltage_profile) == 17

    prof_mv = inspect_connection_point("CIGRE_MV_Rudolstadt")
    res_mv = run_tier2_ac_study(
        ConnectionApplication(
            network_id="CIGRE_MV_Rudolstadt",
            asset_type=AssetType.EV_OR_COMMERCIAL_LOAD,
            proposed_capacity_kw=3000.0,
        ),
        prof_mv,
    )
    assert len(res_mv.voltage_profile) == 8
