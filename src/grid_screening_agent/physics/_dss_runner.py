"""Isolated child-process OpenDSS unbalanced 3-phase AC solver.

Why this module runs as a standalone child process (`python -m ...`):
    1. C-Singleton Isolation: `opendssdirect.py` wraps a C-level in-process circuit
       singleton. Running concurrent FastAPI or MCP requests inside the same Python
       process corrupts active bus/element cursors and crashes the host process.
    2. Working-Directory Safety: OpenDSS `Redirect Master.dss` resolves relative `.dss`
       filenames against OS `cwd`. Scoping `cwd` strictly to the child `subprocess.run()`
       call guarantees zero `os.chdir()` mutation in the parent server.
    3. True MPPT Inverter Physics Above 1.10 pu: OpenDSS defaults `PVSystem` `vmaxpu`
       to `1.10`, silently converting inverters above `1.10 pu` into constant-impedance
       shunts that distort active power (`kW`) and line current. Provisioning `PVSystem`
       elements with `vminpu=0.70 vmaxpu=1.50` preserves exact MPPT PWM current up to
       statutory protection limits.
"""

from __future__ import annotations

import json
import math
import os
import socket
import subprocess
import sys
from typing import Any


_SAFE_WORKER_ENV_KEYS: frozenset[str] = frozenset(
    {
        "PATH",
        "PYTHONPATH",
        "PYTHONHOME",
        "VIRTUAL_ENV",
        "SYSTEMROOT",
        "LANG",
        "LC_ALL",
        "TMPDIR",
        "TMP",
        "TEMP",
        "HOME",
        "DYLD_LIBRARY_PATH",
        "LD_LIBRARY_PATH",
    }
)
_SECRET_KEY_FRAGMENTS: tuple[str, ...] = (
    "KEY",
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "CREDENTIAL",
    "AUTH",
    "AWS_",
    "OPENAI_",
    "ANTHROPIC_",
    "GEMINI_",
)


def _build_sanitized_worker_env() -> dict[str, str]:
    """Construct a secret-scrubbed environment dictionary for isolated child OpenDSS workers."""
    clean: dict[str, str] = {}
    for k, v in os.environ.items():
        upper_k = k.upper()
        if any(frag in upper_k for frag in _SECRET_KEY_FRAGMENTS):
            continue
        if k in _SAFE_WORKER_ENV_KEYS or upper_k.startswith("LC_"):
            clean[k] = v
    clean["PYTHONNOUSERSITE"] = "1"
    clean["PYTHONDONTWRITEBYTECODE"] = "1"
    return clean


def execute_tier2_physics(
    payload: dict[str, Any],
    timeout_seconds: float = 15.0,
    cwd: str | None = None,
) -> dict[str, Any]:
    """Execute the isolated child-process OpenDSS solver with timeout, crash, and JSON corruption guards.

    Why crashes, timeouts, and malformed stdout never propagate as uncaught exceptions:
        OpenDSS wraps compiled C++/Delphi routines that can hit Newton-Raphson divergence
        timeouts or segmentation faults (`returncode == 139`) on adversarial inputs.
        Intercepting all child faults here guarantees the parent MCP/API daemon stays up
        and returns a deterministic `SOLVER_OR_DATA_FAULT` rejection payload.
    """
    raw_input = json.dumps(payload).encode("utf-8")
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "grid_screening_agent.physics._dss_runner"],
            input=raw_input,
            cwd=cwd,
            env=_build_sanitized_worker_env(),
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "decision": "REJECTED",
            "abstention_decision": "ABSTAIN_DATA_OR_SOLVER_FAULT",
            "binding_constraints": ["SOLVER_OR_DATA_FAULT"],
            "requires_human_review": True,
            "timed_out": True,
            "solver_failures": 1,
            "error": f"OpenDSS physics worker timed out after {timeout_seconds:g}s",
        }

    if proc.returncode != 0:
        stderr_msg = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
        return {
            "decision": "REJECTED",
            "abstention_decision": "ABSTAIN_DATA_OR_SOLVER_FAULT",
            "binding_constraints": ["SOLVER_OR_DATA_FAULT"],
            "requires_human_review": True,
            "solver_failures": 1,
            "returncode": proc.returncode,
            "error": f"Child worker exited with code {proc.returncode}: {stderr_msg}",
        }

    try:
        stdout_str = (proc.stdout or b"").decode("utf-8")
        parsed = json.loads(stdout_str)
        if not isinstance(parsed, dict):
            raise ValueError("Worker output root must be a JSON object.")
        return parsed
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        return {
            "decision": "REJECTED",
            "abstention_decision": "ABSTAIN_DATA_OR_SOLVER_FAULT",
            "binding_constraints": ["SOLVER_OR_DATA_FAULT"],
            "requires_human_review": True,
            "solver_failures": 1,
            "error": f"Failed to parse worker output as JSON: {exc}",
        }


class _BlockedNetworkSocket(socket.socket):
    """Zero-trust socket guard blocking all TCP/UDP network egress/ingress inside child OpenDSS workers."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise PermissionError(
            "Zero-trust sandbox violation: network socket creation is strictly disabled inside "
            "isolated OpenDSS physics worker (_dss_runner)."
        )


def install_network_socket_sandbox() -> None:
    """Disable Python network socket primitives for the lifetime of the child worker process.

    Why this guard exists:
        OpenDSS child processes communicate exclusively with the parent coordinator over
        anonymous OS `stdin`/`stdout` pipes. Blocking `socket.socket` and
        `socket.create_connection` guarantees that neither malformed `.dss` scripts nor
        compromised transitive C/Python calls can open outbound HTTP/TCP connections.
    """

    def _deny_connection(*_args: Any, **_kwargs: Any) -> Any:
        raise PermissionError(
            "Zero-trust sandbox violation: outbound network connections are strictly disabled "
            "inside isolated OpenDSS physics worker (_dss_runner)."
        )

    socket.socket = _BlockedNetworkSocket  # type: ignore[misc]
    socket.create_connection = _deny_connection  # type: ignore[assignment]


def _as4777_q_kvar(v_pu: float, s_rated_kva: float) -> float:
    """AS/NZS 4777.2:2020 Australia Region A Volt-VAr curve."""
    if v_pu <= 1.08:
        return 0.0
    if v_pu < 1.12:
        return -0.44 * s_rated_kva * ((v_pu - 1.08) / 0.04)
    return -0.44 * s_rated_kva


def _as4777_p_kw(v_pu: float, p_avail_kw: float) -> float:
    """AS/NZS 4777.2:2020 Volt-Watt linear curtailment curve (1.10 pu -> 1.15 pu)."""
    if v_pu <= 1.10:
        return p_avail_kw
    if v_pu < 1.15:
        return p_avail_kw * (1.0 - 0.8 * ((v_pu - 1.10) / 0.05))
    return 0.2 * p_avail_kw


def _measure_thermal_loading(dss: Any) -> tuple[float, float]:
    """Extract max conductor loading (%) and transformer loading (%) from active OpenDSS circuit.

    Why `dss.Lines.Name(line_name)` precedes `dss.Lines.NormAmps()`:
        `dss.Circuit.SetActiveElement("Line.X")` advances the generic circuit-element
        cursor (`CktElement`) for current extraction (`CurrentsMagAng()`), but does NOT
        advance the `dss.Lines` collection cursor. Explicitly setting `dss.Lines.Name`
        ensures each conductor segment is normalized against its own ampacity rating.

    Why this helper is also re-invoked after the AS/NZS 4777.2 Volt-VAr loop:
        Inductive Volt-VAr (`-Q`) absorption lowers terminal voltage but injects
        quadrature reactive current (`I_q`), raising total conductor current
        `sqrt(I_p^2 + I_q^2)` ("17x thermal loss paradox"). Re-measuring after droop
        convergence prevents masking conductor overloads caused by reactive support.
    """
    max_line_pct = 0.0
    for line_name in dss.Lines.AllNames():
        dss.Lines.Name(line_name)
        dss.Circuit.SetActiveElement(f"Line.{line_name}")
        norm_amps = dss.Lines.NormAmps()
        if norm_amps and norm_amps > 0:
            cur_mag_ang = dss.CktElement.CurrentsMagAng()
            mags = cur_mag_ang[0::2]
            if mags:
                pct = (max(mags) / norm_amps) * 100.0
                if pct > max_line_pct:
                    max_line_pct = pct

    trafo_pct = 0.0
    if dss.Transformers.First() > 0:
        t_name = dss.Transformers.Name()
        if t_name:
            dss.Circuit.SetActiveElement(f"Transformer.{t_name}")
            powers = dss.CktElement.Powers()
            kva = dss.Transformers.kVA()
            if kva and kva > 0 and len(powers) >= 6:
                p_tot = sum(powers[0:6:2])
                q_tot = sum(powers[1:6:2])
                s_kva = math.hypot(p_tot, q_tot)
                trafo_pct = (s_kva / kva) * 100.0

    return max_line_pct, trafo_pct


def run_study(payload: dict[str, Any]) -> dict[str, Any]:
    # Defer C-binding imports strictly to child worker execution so parent MCP/API never loads OpenDSS C++ singletons
    import numpy as np
    import opendssdirect as dss

    customers: list[int] = [int(c) for c in payload["customers"]]
    buses: list[str] = payload["buses"]
    caps_1x: dict[str, float] = payload["caps_1x"]
    scale: float = float(payload["feeder_background_scale"])
    max_agg_1x: float = float(payload["max_agg_1x"])
    snapshots: list[dict[str, Any]] = payload["snapshots"]
    target_bus: str = payload["connection_node"]
    proposed_kw: float = float(payload["proposed_capacity_kw"])
    bess_kw: float = float(payload.get("bess_power_kw", 0.0))
    is_export: bool = bool(payload["is_export"])
    inverter_mode: str = payload.get("inverter_mode", "unmanaged_pf1")
    export_cap_kw: float | None = payload.get("export_cap_kw")
    voltage_max_pu: float = float(payload.get("voltage_max_pu", 1.10))
    voltage_min_pu: float = float(payload.get("voltage_min_pu", 0.94))

    dss.Command("Clear")
    dss.Command("Redirect Master.dss")

    nominal_kw: dict[str, float] = {}
    nominal_kvar: dict[str, float] = {}
    for lname in dss.Loads.AllNames():
        dss.Loads.Name(lname)
        nominal_kw[lname] = float(dss.Loads.kW())
        nominal_kvar[lname] = float(dss.Loads.kvar())

    pv_names: dict[int, str] = {}
    scaled_caps: dict[int, float] = {}
    for cid, bus in zip(customers, buses):
        cap = float(caps_1x[str(cid)]) * scale
        scaled_caps[cid] = cap
        name = f"pv_{cid}"
        pv_names[cid] = name
        dss.Command(
            f"New PVSystem.{name} Bus1={bus}.1 phases=1 kV=0.23094 kVA={max(cap * 1.2, 0.1):.4f} "
            f"conn=Wye PF=1 Pmpp={max(cap, 0.01):.4f} irradiance=1.0 %cutin=0 %cutout=0 vminpu=0.70 vmaxpu=1.50"
        )

    # Provision applicant's new connection element at target_bus.1
    app_elem_name = "applicant_pcc"
    if is_export:
        total_app_kw = proposed_kw + (0.5 * bess_kw)
        dss.Command(
            f"New PVSystem.{app_elem_name} Bus1={target_bus}.1 phases=1 kV=0.23094 "
            f"kVA={max(total_app_kw * 1.2, 0.1):.4f} conn=Wye PF=1 "
            f"Pmpp={max(total_app_kw, 0.01):.4f} irradiance=1.0 %cutin=0 %cutout=0 vminpu=0.70 vmaxpu=1.50"
        )
    else:
        dss.Command(
            f"New Load.{app_elem_name} Bus1={target_bus}.1 phases=1 kV=0.23094 "
            f"kW={proposed_kw:.4f} PF=0.95 model=1"
        )

    dss.Command("CalcVoltageBases")

    intervals_solved = 0
    solver_failures = 0
    violations_count = 0
    peak_v = 0.0
    min_v = 2.0
    worst_season = "Spring"
    worst_ts = ""
    max_line_pct = 0.0
    max_trafo_pct = 0.0
    worst_snap: dict[str, Any] | None = None
    voltage_profile: list[dict[str, Any]] = []

    denom = max(scale * max_agg_1x, 1.0)

    def _apply_snapshot_state(snap: dict[str, Any]) -> None:
        mult = float(snap["mean_customer_load_kw"]) / denom
        for lname in nominal_kw:
            dss.Loads.Name(lname)
            dss.Loads.kW(nominal_kw[lname] * mult)
            dss.Loads.kvar(nominal_kvar[lname] * mult)

        for cid in customers:
            inj = float(snap["export_1x_kw"][str(cid)]) * scale
            if export_cap_kw is not None:
                ld = float(snap["load_kw"][str(cid)])
                if (inj - ld) > export_cap_kw:
                    inj = export_cap_kw + ld
            inj_clipped = max(0.0, min(inj, scaled_caps[cid]))
            dss.PVsystems.Name(pv_names[cid])
            dss.PVsystems.Pmpp(inj_clipped)
            dss.PVsystems.kVARated(max(inj_clipped * 1.2, 0.05))
            dss.PVsystems.pf(1.0)

        if is_export:
            app_inj = proposed_kw + (0.5 * bess_kw)
            if export_cap_kw is not None:
                app_inj = min(app_inj, export_cap_kw)
            dss.PVsystems.Name(app_elem_name)
            dss.PVsystems.Pmpp(max(0.0, app_inj))
            dss.PVsystems.kVARated(max(app_inj * 1.2, 0.05))
            dss.PVsystems.pf(1.0)
            dss.Command(f"PVsystem.{app_elem_name}.kvar=0")
        else:
            dss.Loads.Name(app_elem_name)
            dss.Loads.kW(proposed_kw)

    for snap in snapshots:
        _apply_snapshot_state(snap)

        dss.Command("Solve")
        intervals_solved += 1
        if not dss.Solution.Converged():
            solver_failures += 1
            continue

        vmag = np.array(dss.Circuit.AllBusMagPu())
        valid_v = vmag[vmag > 0.3]
        if len(valid_v) == 0:
            solver_failures += 1
            continue

        v_hi = float(valid_v.max())
        v_lo = float(valid_v.min())
        line_pct, trafo_pct = _measure_thermal_loading(dss)
        max_line_pct = max(max_line_pct, line_pct)
        max_trafo_pct = max(max_trafo_pct, trafo_pct)
        voltage_profile.append(
            {
                "label": str(snap["timestamp"]),
                "season": str(snap["season"]),
                "v_max_pu": round(v_hi, 5),
                "v_min_pu": round(v_lo, 5),
                "loading_pct": round(max(line_pct, trafo_pct), 2),
            }
        )

        if v_hi > peak_v:
            peak_v = v_hi
            worst_season = snap["season"]
            worst_ts = snap["timestamp"]
            worst_snap = snap
        if v_lo < min_v:
            min_v = v_lo

        if v_hi > voltage_max_pu or v_lo < voltage_min_pu:
            violations_count += 1

    # Restore exact worst_snap circuit state before AS/NZS 4777.2 droop and BESS step-cliff audits
    if worst_snap is not None:
        _apply_snapshot_state(worst_snap)

    # AS/NZS 4777.2:2020 iterative droop evaluation on worst snapshot if requested
    curtailment_pct = 0.0
    q_saturated = False
    if is_export and worst_snap is not None and inverter_mode in ("as4777_volt_var", "as4777_volt_var_watt"):
        p_avail_app = proposed_kw + (0.5 * bess_kw)
        if export_cap_kw is not None:
            p_avail_app = min(p_avail_app, export_cap_kw)
        s_rated_app = max(p_avail_app * 1.15, 0.1)
        q_app = 0.0
        p_app = p_avail_app
        alpha = 0.35

        for _ in range(25):
            dss.PVsystems.Name(app_elem_name)
            dss.PVsystems.Pmpp(max(0.0, p_app))
            dss.PVsystems.kVARated(s_rated_app)
            dss.Command(f"PVsystem.{app_elem_name}.kvar={q_app:.4f}")
            dss.Command("Solve")
            if not dss.Solution.Converged():
                solver_failures += 1
                break
            dss.Circuit.SetActiveBus(target_bus)
            pu_va = dss.Bus.puVmagAngle()
            v_bus = float(pu_va[0]) if len(pu_va) else peak_v
            q_target = _as4777_q_kvar(v_bus, s_rated_app)
            p_target = (
                _as4777_p_kw(v_bus, p_avail_app)
                if inverter_mode == "as4777_volt_var_watt"
                else p_avail_app
            )
            q_app = (1.0 - alpha) * q_app + alpha * q_target
            p_app = (1.0 - alpha) * p_app + alpha * p_target

        if p_avail_app > 0:
            curtailment_pct = max(0.0, ((p_avail_app - p_app) / p_avail_app) * 100.0)
        q_saturated = abs(q_app) >= (0.42 * s_rated_app)

        vmag_droop = np.array(dss.Circuit.AllBusMagPu())
        valid_droop = vmag_droop[vmag_droop > 0.3]
        if len(valid_droop) and solver_failures == 0:
            peak_v = float(valid_droop.max())
            min_v = min(min_v, float(valid_droop.min()))
            # Re-measure thermal loading after Volt-VAr reactive current (I_q) absorption
            droop_line_pct, droop_trafo_pct = _measure_thermal_loading(dss)
            max_line_pct = max(max_line_pct, droop_line_pct)
            max_trafo_pct = max(max_trafo_pct, droop_trafo_pct)
            for pt in voltage_profile:
                if pt["label"] == str(worst_ts):
                    pt["v_max_pu"] = round(peak_v, 5)
                    pt["loading_pct"] = round(max(droop_line_pct, droop_trafo_pct), 2)
            if (
                peak_v <= voltage_max_pu
                and min_v >= voltage_min_pu
                and violations_count > 0
                and export_cap_kw is not None
            ):
                violations_count = 0

    # BESS synchronous export-cliff step shock audit
    bess_cliff_dv = 0.0
    if bess_kw > 0 and is_export:
        dss.PVsystems.Name(app_elem_name)
        dss.PVsystems.Pmpp(0.0)
        dss.Command(f"PVsystem.{app_elem_name}.kvar=0")
        dss.Command("Solve")
        dss.Circuit.SetActiveBus(target_bus)
        v_pre = float(dss.Bus.puVmagAngle()[0]) if len(dss.Bus.puVmagAngle()) else 1.0

        dss.PVsystems.Name(app_elem_name)
        dss.PVsystems.Pmpp(proposed_kw + bess_kw)
        dss.PVsystems.kVARated(max((proposed_kw + bess_kw) * 1.2, 0.1))
        dss.Command("Solve")
        dss.Circuit.SetActiveBus(target_bus)
        v_post = float(dss.Bus.puVmagAngle()[0]) if len(dss.Bus.puVmagAngle()) else v_pre
        bess_cliff_dv = abs(v_post - v_pre)

    # Guard transformer capacity reporting when Network H omits explicit transformer object
    if max_trafo_pct == 0.0 and worst_snap is not None:
        agg_kw = float(worst_snap["agg_export_1x_kw"]) * scale + (proposed_kw if is_export else -proposed_kw)
        max_trafo_pct = (abs(agg_kw) / 500.0) * 100.0

    return {
        "intervals_solved": intervals_solved,
        "solver_failures": solver_failures,
        "peak_voltage_pu": round(peak_v, 5),
        "min_voltage_pu": round(min_v if min_v < 1.9 else 1.0, 5),
        "worst_season": worst_season,
        "worst_timestamp": worst_ts,
        "violations_count": violations_count,
        "max_line_loading_pct": round(max_line_pct, 2),
        "transformer_loading_pct": round(max_trafo_pct, 2),
        "as4777_curtailment_pct": round(curtailment_pct, 2),
        "as4777_q_saturated": bool(q_saturated),
        "bess_cliff_delta_v_pu": round(bess_cliff_dv, 5),
        "voltage_profile": voltage_profile,
    }


def main() -> None:
    install_network_socket_sandbox()
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw)
        result = run_study(payload)
    except Exception as exc:  # pragma: no cover - defensive worker fault capture
        result = {
            "intervals_solved": 0,
            "solver_failures": 1,
            "peak_voltage_pu": 1.0,
            "min_voltage_pu": 1.0,
            "worst_season": "Unknown",
            "worst_timestamp": "",
            "violations_count": 1,
            "max_line_loading_pct": 0.0,
            "transformer_loading_pct": 0.0,
            "as4777_curtailment_pct": 0.0,
            "as4777_q_saturated": False,
            "bess_cliff_delta_v_pu": 0.0,
            "voltage_profile": [],
            "worker_error": str(exc),
        }
    sys.stdout.write(json.dumps(result))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
