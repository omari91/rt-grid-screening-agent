"""Constraint-Decoupled Dominance classifier implementing the 3D Master Vertical Grid Matrix.

Why physical regime classification runs BEFORE voltage or thermal screening:
    Across the vertical grid continuum (415 V -> 11-20 kV -> 132 kV -> 330/500 kV),
    the governing physical driver and first-binding constraint invert completely:
    - Resistive Suburban LV (`Network R`, high `R_th`): Event magnitude drives voltage rise
      (`r_mag = 0.902`), whereas operating state drives thermal loading (`r_state = 0.907`).
    - Compact Urban LV (`Network H`, `R_th < 0.08 Ohm`): Topologically voltage-immune;
      voltage-only rules overestimate hosting capacity by `>100%` unless thermal ampacity
      is screened first.
    - Balanced MV (`11-20 kV`, `R/X ~ 1.0`): Background operating state dominates (`r = -0.99`);
      magnitude-only rules miss `84.5%` of violations.
    - Weak 132 kV Fringe (`BrokenHill_132kV`, `SCR_N1 < 2.5`): Post-contingency system strength
      (`NER S5.2.5 SCR_N1 >= 2.0`) binds before steady-state thermal limits.
    - Bulk EHV (`330-500 kV`, `X/R ~ 12-14`): `N-1` corridor thermal transfer capacity binds first.
"""

from __future__ import annotations

from grid_screening_agent.models import (
    AssetType,
    ConnectionPointProfile,
    RegimeClassification,
    VoltageTier,
)

EXPORT_ASSETS = {
    AssetType.ROOFTOP_PV,
    AssetType.PV_PLUS_BESS,
    AssetType.BESS_ONLY,
    AssetType.UTILITY_IBR_PLANT,
}


def classify_screening_regime(
    profile: ConnectionPointProfile,
    asset_type: AssetType,
    proposed_capacity_kw: float,
) -> RegimeClassification:
    """Classify the governing physical driver and first-binding constraint at the PCC."""
    is_export = asset_type in EXPORT_ASSETS
    flow_dir = "Reverse (Active Power Injection)" if is_export else "Forward (Load / Demand Withdrawal)"
    p_mw = proposed_capacity_kw / 1000.0
    scr_n1 = profile.s_sc_n1_mva / max(p_mw, 1e-6)

    # 1. LV 415 V Tier: distinguish stiff short-line urban (Network H) vs long resistive radial (Network R)
    if profile.voltage_tier == VoltageTier.LV_415V:
        # Short cumulative resistance (< 0.08 Ohm) exhibits topological voltage immunity up to 6x-7x
        if profile.r_th_ohm < 0.08:
            return RegimeClassification(
                regime_id="LV_COMPACT_THERMAL_DOMINATED",
                voltage_tier=profile.voltage_tier,
                impedance_regime=f"Stiff Short-Line LV (R_th={profile.r_th_ohm:.3f} Ohm, R/X={profile.rx_ratio:.2f})",
                power_flow_direction=flow_dir,
                governing_voltage_driver="Event Magnitude (r_mag = 0.885), but cumulative R_ij is too small to breach 1.10 pu",
                governing_thermal_driver="Operating State & Coincident Feeder Loading (r_state = 0.892)",
                governing_scr_driver="Abundant Terminal Stiffness (SCR > 12)",
                primary_binding_constraint="Thermal Conductor / Transformer Ampacity",
                secondary_binding_constraint="Steady-State Voltage (Topologically Immune under 6.0x)",
                voltage_only_error_risk="Severe (>100% error if thermal screen is omitted; voltage never binds first)",
                valid_screening_rule="Thermal-first + unbalanced OpenDSS ampacity co-reporting",
                requires_state_aware_screen=True,
                requires_scr_first_screen=False,
                topologically_voltage_immune=True,
            )

        return RegimeClassification(
            regime_id="LV_RESISTIVE_MAGNITUDE_DOMINATED",
            voltage_tier=profile.voltage_tier,
            impedance_regime=f"Resistive Radial LV (R_th={profile.r_th_ohm:.3f} Ohm, R/X={profile.rx_ratio:.2f})",
            power_flow_direction=flow_dir,
            governing_voltage_driver="Event Magnitude Dominates (r_mag = 0.902 vs r_load = -0.47; inversion of MV)",
            governing_thermal_driver="Operating State Dominates (r_state = 0.907)",
            governing_scr_driver="Moderate-Strong Terminal Stiffness (SCR typically 5.0 - 8.0 at 6x)",
            primary_binding_constraint=(
                "Steady-State Over-Voltage (AS/NZS 61000.3.100 V > 1.10 pu)"
                if is_export
                else "Steady-State Under-Voltage & Phase Thermal Loading"
            ),
            secondary_binding_constraint="Line Ampacity & AS/NZS 4777.2 Volt-Watt Curtailment (Volt-VAr Q saturates)",
            voltage_only_error_risk="Low at remote terminal (0%), High near substation (+88.9% without thermal screen)",
            valid_screening_rule="Decoupled dual-screen: Season-stratified magnitude dV/dP for V + operating-state for I",
            requires_state_aware_screen=False,
            requires_scr_first_screen=False,
            topologically_voltage_immune=False,
        )

    # 2. MV 11-20 kV Tier: balanced R/X ~ 1.0 (Paper 1 German/Australian MV finding)
    if profile.voltage_tier == VoltageTier.MV_11_20KV:
        return RegimeClassification(
            regime_id="MV_BALANCED_STATE_DOMINATED",
            voltage_tier=profile.voltage_tier,
            impedance_regime=f"Balanced MV Distribution (R/X={profile.rx_ratio:.2f})",
            power_flow_direction=flow_dir,
            governing_voltage_driver="Background Operating State Dominates (r_state = -0.99, r_mag = 0.06)",
            governing_thermal_driver="Background Operating State Dominates (r_state = 0.94)",
            governing_scr_driver="Moderate Fault Level (SCR = 4.5 - 15.0)",
            primary_binding_constraint="State-Dependent Voltage Margin (0.90 - 1.10 pu)",
            secondary_binding_constraint="Zone Substation Transformer / Feeder Thermal Rating",
            voltage_only_error_risk="Severe false-negative risk (84.5% missed violations) if magnitude-only screen is used",
            valid_screening_rule="Two-feature operating-state + event-magnitude regression screen",
            requires_state_aware_screen=True,
            requires_scr_first_screen=False,
            topologically_voltage_immune=False,
        )

    # 3. 132 kV Sub-Transmission: distinguish Weak Fringe (SCR_N1 < 2.5) vs Meshed
    if profile.voltage_tier == VoltageTier.TX_132KV:
        if scr_n1 < 2.5 or profile.s_sc_mva < 400.0:
            return RegimeClassification(
                regime_id="TX_132KV_WEAK_FRINGE_SCR_DOMINATED",
                voltage_tier=profile.voltage_tier,
                impedance_regime=f"High-Z_th Inductive Fringe (X/R={profile.xr_ratio:.1f}, S_sc={profile.s_sc_mva:.0f} MVA)",
                power_flow_direction=flow_dir,
                governing_voltage_driver="Connection Magnitude & Reactive Nose Proximity (r_mag = 0.997)",
                governing_thermal_driver="Corridor Operating State (r_state = 0.925)",
                governing_scr_driver=f"Weak Grid Boundary (N-1 SCR = {scr_n1:.2f}; < 2.0 risks PLL/IBR instability)",
                primary_binding_constraint="System Strength / Short Circuit Ratio (NER S5.2.5 SCR_N1 >= 2.0)",
                secondary_binding_constraint="Static Voltage Collapse (PV/QV Nose Curve)",
                voltage_only_error_risk="Significant (+13.8% capacity overestimation if SCR limit is ignored)",
                valid_screening_rule="Mandatory N-1 SCR system-strength screen prior to steady-state AC power flow",
                requires_state_aware_screen=True,
                requires_scr_first_screen=True,
                topologically_voltage_immune=False,
            )

        return RegimeClassification(
            regime_id="TX_132KV_MESHED_REACTIVE_MARGIN",
            voltage_tier=profile.voltage_tier,
            impedance_regime=f"Inductive Sub-Transmission (X/R={profile.xr_ratio:.1f}, S_sc={profile.s_sc_mva:.0f} MVA)",
            power_flow_direction=flow_dir,
            governing_voltage_driver="Connection Magnitude & Reactive Margin (r_mag = 0.988)",
            governing_thermal_driver="Regional Corridor Operating State (r_state = 0.925)",
            governing_scr_driver=f"Moderate System Strength (N-1 SCR = {scr_n1:.2f})",
            primary_binding_constraint="Voltage Stability (Reactive Nose Margin) & N-1 SCR",
            secondary_binding_constraint="N-1 Corridor Thermal Line Ampacity",
            voltage_only_error_risk="Moderate (+22% regional error without joint thermal + N-1 SCR co-screening)",
            valid_screening_rule="Joint PV/QV reactive margin + N-1 SCR + state-aware thermal screen",
            requires_state_aware_screen=True,
            requires_scr_first_screen=True,
            topologically_voltage_immune=False,
        )

    # 4. 330/500 kV Bulk EHV Backbone
    return RegimeClassification(
        regime_id="TX_EHV_BACKBONE_THERMAL_N1_DOMINATED",
        voltage_tier=profile.voltage_tier,
        impedance_regime=f"Highly Inductive EHV Backbone (X/R={profile.xr_ratio:.1f}, S_sc={profile.s_sc_mva:.0f} MVA)",
        power_flow_direction=flow_dir,
        governing_voltage_driver="Coupled Magnitude & Interconnector State (r_mag = 0.761, r_state = 0.671)",
        governing_thermal_driver="Bulk Regional Transfer State (r_state = 0.925)",
        governing_scr_driver=f"Strong Backbone Stiffness (N-1 SCR = {scr_n1:.2f})",
        primary_binding_constraint="N-1 Thermal Transfer Capacity",
        secondary_binding_constraint="Dynamic Reactive Reserves & Voltage Stability",
        voltage_only_error_risk="Extreme (+76.5% to +108.3% hosting capacity overestimation if voltage-only is used)",
        valid_screening_rule="N-1 thermal transfer headroom + dynamic reactive reserve screen",
        requires_state_aware_screen=True,
        requires_scr_first_screen=False,
        topologically_voltage_immune=False,
    )
