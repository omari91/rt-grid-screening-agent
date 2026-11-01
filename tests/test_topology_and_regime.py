"""Tests for path-safe topology extraction, SHA-256 provenance, and 3D regime classification (10 tests)."""

from __future__ import annotations

import os

import pytest

from grid_screening_agent.models import AssetType, VoltageTier
from grid_screening_agent.physics.regime import classify_screening_regime
from grid_screening_agent.physics.topology import (
    inspect_connection_point,
    list_supported_networks,
)


def test_list_supported_networks_catalog() -> None:
    catalog = list_supported_networks()
    assert "R" in catalog["lv_feeders"]
    assert "H" in catalog["lv_feeders"]
    assert catalog["lv_feeders"]["R"]["worst_terminal_bus"] == "3110858"
    assert "CIGRE_MV_Rudolstadt" in catalog["mv_benchmarks"]
    assert "BrokenHill_132kV" in catalog["transmission_substations"]
    assert "Bayswater_500kV" in catalog["transmission_substations"]


def test_inspect_connection_point_never_mutates_cwd() -> None:
    cwd_before = os.getcwd()
    inspect_connection_point("R")
    inspect_connection_point("H")
    inspect_connection_point("CIGRE_MV_Rudolstadt")
    inspect_connection_point("BrokenHill_132kV")
    assert os.getcwd() == cwd_before


def test_network_r_worst_terminal_bus_physics() -> None:
    prof_r = inspect_connection_point("R")
    prof_tx = inspect_connection_point("BrokenHill_132kV")
    assert prof_r.connection_node == "3110858"
    assert prof_r.voltage_tier == VoltageTier.LV_415V
    assert prof_r.electrical_rank == 1
    assert prof_r.r_th_ohm > 0.08
    assert prof_r.rx_ratio > prof_tx.rx_ratio * 4.0
    assert prof_r.dv_dp_pu_per_kw == pytest.approx(0.003002, rel=1e-2)


def test_network_h_compact_urban_stiffness() -> None:
    prof_r = inspect_connection_point("R")
    prof_h = inspect_connection_point("H")
    assert prof_h.voltage_tier == VoltageTier.LV_415V
    assert prof_h.r_th_ohm < 0.08
    assert prof_h.s_sc_mva > prof_r.s_sc_mva * 3.0


def test_mv_benchmarks_impedance_and_rank() -> None:
    prof_cigre = inspect_connection_point("CIGRE_MV_Rudolstadt")
    prof_aus = inspect_connection_point("Aus_MV_11kV_Suburban")
    assert prof_cigre.voltage_tier == VoltageTier.MV_11_20KV
    assert prof_cigre.nominal_kv == pytest.approx(20.0)
    assert prof_aus.nominal_kv == pytest.approx(11.0)
    assert 0.5 < prof_cigre.rx_ratio < 1.5


def test_transmission_substations_fault_levels() -> None:
    prof_bh = inspect_connection_point("BrokenHill_132kV")
    prof_bw = inspect_connection_point("Bayswater_500kV")
    assert prof_bh.voltage_tier == VoltageTier.TX_132KV
    assert prof_bh.s_sc_mva == pytest.approx(290.0)
    assert prof_bh.s_sc_n1_mva == pytest.approx(188.5)
    assert prof_bw.voltage_tier == VoltageTier.TX_330_500KV
    assert prof_bw.s_sc_mva > 5000.0


def test_sha256_provenance_is_deterministic() -> None:
    p1 = inspect_connection_point("R", "3110858")
    p2 = inspect_connection_point("R", "3110859")
    assert len(p1.model_sha256) == 64
    assert p1.model_sha256 == p2.model_sha256
    assert p1.dv_dp_pu_per_kw > p2.dv_dp_pu_per_kw


def test_invalid_network_or_bus_raises_value_error() -> None:
    with pytest.raises(ValueError, match="Unknown network_id"):
        inspect_connection_point("NON_EXISTENT_GRID")
    with pytest.raises(ValueError, match="not found in Network R"):
        inspect_connection_point("R", "9999999")


def test_3d_master_vertical_matrix_lv_and_mv_regimes() -> None:
    prof_r = inspect_connection_point("R")
    reg_r = classify_screening_regime(prof_r, AssetType.ROOFTOP_PV, 10.0)
    assert reg_r.regime_id == "LV_RESISTIVE_MAGNITUDE_DOMINATED"
    assert not reg_r.topologically_voltage_immune

    prof_h = inspect_connection_point("H")
    reg_h = classify_screening_regime(prof_h, AssetType.ROOFTOP_PV, 10.0)
    assert reg_h.regime_id == "LV_COMPACT_THERMAL_DOMINATED"
    assert reg_h.topologically_voltage_immune

    prof_mv = inspect_connection_point("CIGRE_MV_Rudolstadt")
    reg_mv = classify_screening_regime(prof_mv, AssetType.EV_OR_COMMERCIAL_LOAD, 2500.0)
    assert reg_mv.regime_id == "MV_BALANCED_STATE_DOMINATED"
    assert reg_mv.requires_state_aware_screen


def test_3d_master_vertical_matrix_tx_regimes() -> None:
    prof_bh = inspect_connection_point("BrokenHill_132kV")
    reg_bh = classify_screening_regime(prof_bh, AssetType.HYPERSCALE_DATACENTRE, 150_000.0)
    assert reg_bh.regime_id == "TX_132KV_WEAK_FRINGE_SCR_DOMINATED"
    assert reg_bh.requires_scr_first_screen

    prof_wg = inspect_connection_point("Wangaratta_132kV")
    reg_wg = classify_screening_regime(prof_wg, AssetType.HYPERSCALE_DATACENTRE, 50_000.0)
    assert reg_wg.regime_id == "TX_132KV_MESHED_REACTIVE_MARGIN"

    prof_bw = inspect_connection_point("Bayswater_500kV")
    reg_bw = classify_screening_regime(prof_bw, AssetType.HYPERSCALE_DATACENTRE, 500_000.0)
    assert reg_bw.regime_id == "TX_EHV_BACKBONE_THERMAL_N1_DOMINATED"
    assert not reg_bw.requires_scr_first_screen
