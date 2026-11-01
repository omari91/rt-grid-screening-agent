"""Process-isolated Tier-2 AC power-flow study coordinator (OpenDSS LV + AC Nose-Curve MV/TX).

Why this coordinator bounds concurrency and solver wall-clock time:
    1. DoS & Fork-Bomb Prevention: A batch of interconnection queue requests could
       otherwise spawn dozens of simultaneous OpenDSS C-engine processes. `_DSS_SEMAPHORE`
       (`GRID_AGENT_MAX_DSS_WORKERS`) caps concurrent child solvers cleanly.
    2. Deterministic Hang Recovery: Ill-conditioned radial feeders or heavy `time_series`
       sweeps cannot block an MCP/FastAPI worker indefinitely. `timeout=app.solver_timeout_sec`
       catches `subprocess.TimeoutExpired` and returns `timed_out=True, solver_failures=1`
       so the Supervisory Sanity Gate abstains safely (`ABSTAIN_DATA_OR_SOLVER_FAULT`).
    3. Exact Saddle-Node Collapse Detection on MV/TX: Rather than linearizing HV voltage
       drops, `_solve_two_bus_ac_pu` solves the exact quartic 2-bus AC power-flow equation
       and detects `discriminant < 0` as closed-form static PV/QV nose-curve collapse.
"""

from __future__ import annotations

import json
import logging
import math
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_MAX_DSS_WORKERS = max(
    1,
    int(os.environ.get("GRID_AGENT_MAX_DSS_WORKERS", str(min(8, max(2, os.cpu_count() or 4))))),
)
_DSS_SEMAPHORE = threading.BoundedSemaphore(_MAX_DSS_WORKERS)

from grid_screening_agent.models import (
    ConnectionApplication,
    ConnectionPointProfile,
    StudyType,
    Tier2OpenDSSResult,
    VoltageProfilePoint,
    VoltageTier,
)
_SECRET_ENV_SUFFIXES = ("_API_KEY", "_SECRET", "_TOKEN", "_PASSWORD", "_CREDENTIALS")
_SECRET_ENV_EXACT = frozenset(
    {
        "GEMINI_API_KEY",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "GRID_AGENT_API_TOKEN",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
    }
)


def build_sanitized_worker_env(src_root: str) -> dict[str, str]:
    """Construct a minimal, credential-scrubbed environment for the isolated OpenDSS child worker.

    Why host credentials are stripped before `subprocess.run`:
        Enforces zero-trust privilege separation between the host broker (which may hold
        LLM API keys or enterprise OAuth/IAM tokens) and the isolated physics solver, so
        child OpenDSS processes are structurally blinded to all host secrets.
    """
    clean_env: dict[str, str] = {}
    for key, val in os.environ.items():
        upper_k = key.upper()
        if upper_k in _SECRET_ENV_EXACT or upper_k.endswith(_SECRET_ENV_SUFFIXES):
            continue
        clean_env[key] = val
    existing_pp = clean_env.get("PYTHONPATH", "")
    clean_env["PYTHONPATH"] = f"{src_root}{os.pathsep}{existing_pp}" if existing_pp else src_root
    return clean_env


def _demote_to_unprivileged_user() -> None:
    """Drop root privileges inside child OpenDSS process when parent runs as UID 0 on POSIX.

    Why `preexec_fn` privilege demotion is paired with container `USER gridagent`:
        Even if an operator overrides container startup as `root`, any child OpenDSS C++
        worker drops supplementary groups and switches to `gridagent` (UID/GID 10001)
        before executing `_dss_runner.py`.
    """
    if os.name != "posix" or not hasattr(os, "geteuid") or os.geteuid() != 0:
        return
    try:
        import pwd

        target_user = pwd.getpwnam("gridagent")
        if hasattr(os, "setgroups"):
            os.setgroups([])
        os.setgid(target_user.pw_gid)
        os.setuid(target_user.pw_uid)
    except (ImportError, KeyError, PermissionError):
        # Running outside the standardized container image where `gridagent` may not exist
        pass
from grid_screening_agent.physics.regime import EXPORT_ASSETS
from grid_screening_agent.physics.topology import load_reference_data, parse_lv_feeder_graph


def _solve_two_bus_ac_pu(
    p_pu: float,
    q_pu: float,
    r_pu: float,
    x_pu: float,
    v_source_pu: float = 1.0,
) -> tuple[float, bool, bool]:
    """Exact quadratic 2-bus AC receiving/sending voltage equation (detects nose-curve collapse).

    Solves V^4 + (2*(P*R + Q*X) - V_s^2)*V^2 + (P^2 + Q^2)*(R^2 + X^2) = 0
    where P, Q > 0 denotes load withdrawal (reverse sign for export injection).

    Returns:
        (v_pu, solver_valid, nose_collapsed)
    """
    if any(math.isnan(x) or math.isinf(x) for x in (p_pu, q_pu, r_pu, x_pu, v_source_pu)):
        return (float("nan"), False, False)
    b = 2.0 * (p_pu * r_pu + q_pu * x_pu) - (v_source_pu**2)
    c = (p_pu**2 + q_pu**2) * (r_pu**2 + x_pu**2)
    discriminant = (b**2) - 4.0 * c
    if discriminant < 0.0:
        # Closed-form proof of saddle-node static voltage collapse past PV/QV nose limit
        v_crit = math.sqrt(max(0.36, min(0.72, -b / 2.0 if -b > 0 else 0.49)))
        return (v_crit, True, True)
    v_sq = (-b + math.sqrt(discriminant)) / 2.0
    if v_sq <= 0.0:
        return (0.60, True, True)
    return (math.sqrt(v_sq), True, False)


def run_tier2_ac_study(
    app: ConnectionApplication,
    profile: ConnectionPointProfile,
    export_cap_kw: Optional[float] = None,
) -> Tier2OpenDSSResult:
    """Execute Tier-2 AC verification in an isolated OpenDSS worker (LV) or exact AC N-1 solver (MV/TX)."""
    t0 = time.perf_counter()
    ref = load_reference_data()
    is_export = app.asset_type in EXPORT_ASSETS

    # --- LV 415 V Unbalanced 3-Phase OpenDSS Subprocess ---
    if profile.voltage_tier == VoltageTier.LV_415V:
        parsed = parse_lv_feeder_graph(profile.network_id)
        ranked_buses = parsed["ranked_phase1_buses"]
        ordered_custs = ref["customers"]["ordered_ids"]
        n_cust = min(25, len(ranked_buses), len(ordered_custs))

        allowed_seasons = set(app.seasons)
        crit_snaps = [s for s in ref["critical_snapshots"] if s["season"] in allowed_seasons]
        if not crit_snaps:
            crit_snaps = ref["critical_snapshots"][:8]

        if app.study_type == StudyType.TIME_SERIES:
            diurnal = list(ref.get("diurnal_summer_peak_day", []))
            seen_idx = {int(s["event_idx"]) for s in diurnal}
            snaps = diurnal + [s for s in crit_snaps if int(s["event_idx"]) not in seen_idx]
            scope_desc = (
                f"time_series ({len(diurnal)}-interval 30-min diurnal trajectory + seasonal peaks = "
                f"{len(snaps)} sequential AC states; sub-annual reference window, not full 17,520-interval annual telemetry)"
            )
        else:
            snaps = crit_snaps
            scope_desc = (
                f"snapshot ({len(snaps)} worst-case seasonal peak states; excludes full 17,520-interval annual sweep)"
            )

        payload = {
            "customers": ordered_custs[:n_cust],
            "buses": ranked_buses[:n_cust],
            "caps_1x": ref["customers"]["generator_capacity_1x_kw"],
            "feeder_background_scale": app.feeder_background_scale,
            "max_agg_1x": ref["metadata"]["max_aggregate_export_1x_kw"],
            "snapshots": snaps,
            "connection_node": profile.connection_node,
            "proposed_capacity_kw": app.proposed_capacity_kw,
            "bess_power_kw": app.bess_power_kw,
            "is_export": is_export,
            "inverter_mode": app.inverter_mode.value,
            "export_cap_kw": export_cap_kw,
            "voltage_max_pu": 1.10,
            "voltage_min_pu": 0.94,
        }

        src_root = str(Path(__file__).resolve().parent.parent.parent)
        env = build_sanitized_worker_env(src_root)

        try:
            with _DSS_SEMAPHORE:
                proc = subprocess.run(
                    [sys.executable, "-m", "grid_screening_agent.physics._dss_runner"],
                    input=json.dumps(payload),
                    capture_output=True,
                    text=True,
                    cwd=parsed["network_dir"],
                    env=env,
                    timeout=float(app.solver_timeout_sec),
                    preexec_fn=_demote_to_unprivileged_user if os.name == "posix" else None,
                    check=False,
                )
        except subprocess.TimeoutExpired as exc:
            logger.error("Isolated OpenDSS worker timed out after %.1fs: %s", app.solver_timeout_sec, exc)
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            return Tier2OpenDSSResult(
                executed=True,
                study_type=app.study_type,
                study_scope=scope_desc,
                solver_engine="OpenDSS Direct Unbalanced 3-Phase AC (Isolated Subprocess)",
                intervals_solved=0,
                solver_failures=1,
                timed_out=True,
                peak_voltage_pu=1.0,
                min_voltage_pu=1.0,
                worst_season="Unknown",
                worst_timestamp="",
                violations_count=1,
                max_line_loading_pct=0.0,
                transformer_loading_pct=0.0,
                statutory_voltage_compliant=False,
                thermal_compliant=False,
                execution_ms=round(elapsed_ms, 2),
            )
        except OSError as exc:
            logger.error("Isolated OpenDSS worker OS error: %s", exc)
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            return Tier2OpenDSSResult(
                executed=True,
                study_type=app.study_type,
                study_scope=scope_desc,
                solver_engine="OpenDSS Direct Unbalanced 3-Phase AC (Isolated Subprocess)",
                intervals_solved=0,
                solver_failures=1,
                timed_out=False,
                peak_voltage_pu=1.0,
                min_voltage_pu=1.0,
                worst_season="Unknown",
                worst_timestamp="",
                violations_count=1,
                max_line_loading_pct=0.0,
                transformer_loading_pct=0.0,
                statutory_voltage_compliant=False,
                thermal_compliant=False,
                execution_ms=round(elapsed_ms, 2),
            )

        if proc.returncode != 0:
            logger.error(
                "Isolated OpenDSS worker exited with rc=%s: %s",
                proc.returncode,
                proc.stderr.strip(),
            )
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            return Tier2OpenDSSResult(
                executed=True,
                study_type=app.study_type,
                study_scope=scope_desc,
                solver_engine="OpenDSS Direct Unbalanced 3-Phase AC (Isolated Subprocess)",
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
                execution_ms=round(elapsed_ms, 2),
            )

        try:
            out = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            logger.error("Malformed JSON from OpenDSS worker: %s", exc)
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            return Tier2OpenDSSResult(
                executed=True,
                study_type=app.study_type,
                study_scope=scope_desc,
                solver_engine="OpenDSS Direct Unbalanced 3-Phase AC (Isolated Subprocess)",
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
                execution_ms=round(elapsed_ms, 2),
            )
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        v_ok = (out["violations_count"] == 0) and (out["peak_voltage_pu"] <= 1.10) and (out["min_voltage_pu"] >= 0.94)
        th_ok = (out["max_line_loading_pct"] <= 100.0) and (out["transformer_loading_pct"] <= 100.0)

        return Tier2OpenDSSResult(
            executed=True,
            study_type=app.study_type,
            study_scope=scope_desc,
            solver_engine="OpenDSS Direct Unbalanced 3-Phase AC (Isolated Subprocess)",
            intervals_solved=int(out["intervals_solved"]),
            solver_failures=int(out["solver_failures"]),
            timed_out=False,
            peak_voltage_pu=float(out["peak_voltage_pu"]),
            min_voltage_pu=float(out["min_voltage_pu"]),
            worst_season=str(out["worst_season"]),
            worst_timestamp=str(out["worst_timestamp"]),
            violations_count=int(out["violations_count"]),
            max_line_loading_pct=float(out["max_line_loading_pct"]),
            transformer_loading_pct=float(out["transformer_loading_pct"]),
            as4777_curtailment_pct=float(out["as4777_curtailment_pct"]),
            as4777_q_saturated=bool(out["as4777_q_saturated"]),
            bess_cliff_delta_v_pu=float(out["bess_cliff_delta_v_pu"]),
            voltage_profile=[VoltageProfilePoint(**pt) for pt in out.get("voltage_profile", [])],
            statutory_voltage_compliant=v_ok and (int(out["solver_failures"]) == 0),
            thermal_compliant=th_ok,
            execution_ms=round(elapsed_ms, 2),
        )

    # --- MV / Wholesale Transmission Exact 2-Bus AC Contingency Sweep ---
    s_base_mva = 100.0
    z_base_ohm = (profile.nominal_kv**2) / s_base_mva
    r_pu_n0 = profile.r_th_ohm / z_base_ohm
    x_pu_n0 = profile.x_th_ohm / z_base_ohm

    # Evaluate across 8 seasonal peak contingency states (snapshot) or 48x2 diurnal states (time_series)
    if app.study_type == StudyType.TIME_SERIES:
        season_multipliers = []
        for idx in range(48):
            hh = idx // 2
            mm = 30 if (idx % 2) else 0
            # Diurnal demand wave (0.55 overnight -> 1.00 afternoon peak)
            diurnal_shape = 0.55 + 0.45 * max(0.0, math.sin(math.pi * (idx - 12) / 30.0))
            ts_base = f"2026-01-22T{hh:02d}:{mm:02d}:00"
            season_multipliers.append(("Summer", ts_base, round(diurnal_shape, 4), 1.0))
            season_multipliers.append(("Summer", ts_base, round(diurnal_shape, 4), 1.0 / 0.65))
        mv_tx_scope = "time_series (96 chronological 30-min diurnal N-0 & N-1 contingency intervals)"
    else:
        season_multipliers = [
            ("Summer", "2026-01-22T14:30:00", 1.00, 1.0),
            ("Summer", "2026-01-22T15:00:00", 1.00, 1.0 / 0.65),
            ("Spring", "2026-10-14T13:00:00", 0.72, 1.0),
            ("Spring", "2026-10-14T13:30:00", 0.72, 1.0 / 0.65),
            ("Autumn", "2026-04-11T14:00:00", 0.80, 1.0),
            ("Autumn", "2026-04-11T14:30:00", 0.80, 1.0 / 0.65),
            ("Winter", "2026-07-19T18:30:00", 0.92, 1.0),
            ("Winter", "2026-07-19T19:00:00", 0.92, 1.0 / 0.65),
        ]
        mv_tx_scope = "snapshot (8 seasonal peak N-0 & N-1 contingency states; excludes full annual sweep)"

    eff_kw = min(app.proposed_capacity_kw, export_cap_kw) if export_cap_kw is not None else app.proposed_capacity_kw
    p_app_mw = eff_kw / 1000.0
    v_min_lim = 0.90
    v_max_lim = 1.10
    if profile.voltage_tier == VoltageTier.MV_11_20KV:
        mv_cfg = ref["mv_benchmarks"][profile.network_id]
        v_min_lim = float(mv_cfg["voltage_min_pu"])
        v_max_lim = float(mv_cfg["voltage_max_pu"])
        corridor_mw = float(mv_cfg["thermal_limit_mw"])
    else:
        sub_cfg = ref["transmission_substations"][profile.network_id]
        corridor_mw = float(sub_cfg["hc_thermal_n1_mw"])

    solved = 0
    failures = 0
    violations = 0
    peak_v = 0.0
    min_v = 2.0
    worst_season = "Summer"
    worst_ts = ""
    max_loading_pct = 0.0
    mv_tx_profile: list[VoltageProfilePoint] = []

    for s_name, ts_iso, s_mult, z_n1_mult in season_multipliers:
        if s_name not in app.seasons:
            continue
        bg_mw = corridor_mw * app.operating_load_factor * s_mult * 0.45
        net_load_mw = (bg_mw - p_app_mw) if is_export else (bg_mw + p_app_mw)
        # Reactive demand/support
        if app.inverter_mode in ("as4777_volt_var", "as4777_volt_var_watt"):
            q_mw = -0.10 * p_app_mw if not is_export else 0.15 * p_app_mw
        else:
            q_mw = 0.20 * net_load_mw

        v_pu, ok, collapsed = _solve_two_bus_ac_pu(
            p_pu=net_load_mw / s_base_mva,
            q_pu=q_mw / s_base_mva,
            r_pu=r_pu_n0 * z_n1_mult,
            x_pu=x_pu_n0 * z_n1_mult,
            v_source_pu=1.0,
        )
        solved += 1
        if not ok or math.isnan(v_pu):
            failures += 1
            violations += 1
            continue

        eff_limit_mw = corridor_mw if z_n1_mult == 1.0 else corridor_mw * 0.85
        loading = (abs(net_load_mw) / max(eff_limit_mw, 0.1)) * 100.0
        max_loading_pct = max(max_loading_pct, loading)
        mv_tx_profile.append(
            VoltageProfilePoint(
                label=f"{ts_iso} ({'N-0' if z_n1_mult == 1.0 else 'N-1'})",
                season=s_name,
                v_max_pu=round(v_pu, 5),
                v_min_pu=round(v_pu, 5),
                loading_pct=round(loading, 2),
            )
        )

        if v_pu > peak_v:
            peak_v = v_pu
        if v_pu < min_v:
            min_v = v_pu
            worst_season = s_name
            worst_ts = ts_iso

        if collapsed or v_pu < v_min_lim or v_pu > v_max_lim:
            violations += 1
            worst_season = s_name
            worst_ts = ts_iso

    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    v_ok = (violations == 0) and (failures == 0) and (min_v >= v_min_lim) and (peak_v <= v_max_lim)
    th_ok = max_loading_pct <= 100.0

    return Tier2OpenDSSResult(
        executed=True,
        study_type=app.study_type,
        study_scope=mv_tx_scope,
        solver_engine="Exact Complex 2-Bus AC Contingency Solver (N-0 & N-1)",
        intervals_solved=solved,
        solver_failures=failures,
        timed_out=False,
        peak_voltage_pu=round(peak_v if peak_v > 0 else 1.0, 5),
        min_voltage_pu=round(min_v if min_v < 1.9 else 1.0, 5),
        worst_season=worst_season,
        worst_timestamp=worst_ts,
        violations_count=violations,
        max_line_loading_pct=round(max_loading_pct, 2),
        transformer_loading_pct=round(max_loading_pct * 0.92, 2),
        as4777_curtailment_pct=0.0,
        as4777_q_saturated=False,
        bess_cliff_delta_v_pu=0.0,
        voltage_profile=mv_tx_profile,
        statutory_voltage_compliant=v_ok,
        thermal_compliant=th_ok,
        execution_ms=round(elapsed_ms, 2),
    )
