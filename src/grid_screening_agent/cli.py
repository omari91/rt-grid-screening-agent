"""Command-line interface for interactive triage, natural-language screening, API, and MCP modes."""

from __future__ import annotations

import argparse
import json
import sys

from pathlib import Path

import uvicorn

from grid_screening_agent.agent import GridScreeningAgent
from grid_screening_agent.models import (
    AssetType,
    ConnectionApplication,
    InverterMode,
    ScreeningVerdict,
    StudyType,
)
from grid_screening_agent.physics.topology import list_supported_networks
from grid_screening_agent.plotting import render_verdict_svg


def _print_verdict(verdict: ScreeningVerdict, as_json: bool = False) -> None:
    if as_json:
        print(json.dumps(verdict.model_dump(), indent=2))
        return
    print("=" * 74)
    print(f"  {verdict.headline}")
    print("=" * 74)
    print(verdict.engineering_memo)
    print("-" * 74)
    sg = verdict.sanity_gate
    gate_str = "PASSED" if sg.passed else f"INTERCEPTED ({'; '.join(sg.anomalies_detected)})"
    hr_str = (
        f"YES ({'; '.join(verdict.human_review_reasons)})"
        if verdict.requires_human_review
        else "NO (Automated Fast-Track Eligible)"
    )
    bc_str = ", ".join(c.value for c in verdict.binding_constraints) or "NONE"
    print(f"Binding Constraints     : {bc_str}")
    print(f"Requires Human Review   : {hr_str}")
    if verdict.regulatory_override:
        print(f"Regulatory Override     : {verdict.regulatory_override}")
    print(f"Supervisory Sanity Gate : {gate_str}")
    print(f"Audit Receipt ID        : {verdict.audit_receipt.receipt_id}")
    print(f"Feeder Model SHA-256    : {verdict.audit_receipt.feeder_model_sha256[:16]}...")
    print(f"Reproducibility SHA-256 : {verdict.audit_receipt.reproducibility_sha256[:16]}...")
    print(f"Tools Executed          : {' -> '.join(t['tool'] for t in verdict.audit_receipt.tool_trace)}")
    print("=" * 74)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="grid-screen",
        description="Physics-grounded LV/MV/HV grid connection screening agent.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # 1. `grid-screen networks`
    sub.add_parser("networks", help="List supported LV feeders, MV benchmarks, and NEM substations.")

    # 2. `grid-screen triage`
    p_triage = sub.add_parser("triage", help="Screen a structured connection application.")
    p_triage.add_argument("--network", default="R", help="Network or substation ID (default: R).")
    p_triage.add_argument("--node", default=None, help="Specific bus ID (default: worst terminal bus).")
    p_triage.add_argument("--kw", type=float, required=True, help="Proposed active capacity in kW.")
    p_triage.add_argument(
        "--asset",
        choices=[e.value for e in AssetType],
        default=AssetType.ROOFTOP_PV.value,
        help="Asset class.",
    )
    p_triage.add_argument("--bess-kw", type=float, default=0.0, help="Co-located BESS rating in kW.")
    p_triage.add_argument("--scale", type=float, default=5.0, help="Background LV solar scale factor (default: 5.0).")
    p_triage.add_argument("--load-factor", type=float, default=0.55, help="Coincident load factor (default: 0.55).")
    p_triage.add_argument(
        "--inverter-mode",
        choices=[e.value for e in InverterMode],
        default=InverterMode.AS4777_VOLT_VAR_WATT.value,
    )
    p_triage.add_argument(
        "--study-type",
        choices=[e.value for e in StudyType],
        default=StudyType.SNAPSHOT.value,
        help="Tier-2 AC study scope: 'snapshot' (worst-case seasonal peaks) or 'time_series' (diurnal trajectory).",
    )
    p_triage.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        help="Hard wall-clock timeout in seconds for child-process OpenDSS solver (default: 15.0).",
    )
    p_triage.add_argument("--provider", default="auto", help="Agent provider (auto/deterministic/gemini/anthropic/openai).")
    p_triage.add_argument("--force-tier2", action="store_true", help="Force Tier-2 OpenDSS/AC simulation.")
    p_triage.add_argument("--svg", default=None, help="Optional output file path for SVG voltage & constraint plot.")
    p_triage.add_argument("--json", action="store_true", help="Output full JSON ScreeningVerdict.")

    # 3. `grid-screen ask "..."`
    p_ask = sub.add_parser("ask", help="Screen a free-text connection inquiry.")
    p_ask.add_argument("inquiry", help="Natural-language connection screening prompt.")
    p_ask.add_argument(
        "--study-type",
        choices=[e.value for e in StudyType],
        default=StudyType.SNAPSHOT.value,
        help="Tier-2 AC study scope: 'snapshot' or 'time_series'.",
    )
    p_ask.add_argument("--provider", default="auto", help="Agent provider.")
    p_ask.add_argument("--force-tier2", action="store_true")
    p_ask.add_argument("--svg", default=None, help="Optional output file path for SVG voltage & constraint plot.")
    p_ask.add_argument("--json", action="store_true")

    # 4. `grid-screen serve`
    p_serve = sub.add_parser("serve", help="Launch the FastAPI HTTP service.")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8000)

    # 5. `grid-screen mcp`
    p_mcp = sub.add_parser("mcp", help="Launch the Model Context Protocol (MCP) server.")
    p_mcp.add_argument("--transport", choices=["stdio", "streamable-http", "sse"], default="stdio")
    p_mcp.add_argument("--host", default="0.0.0.0")
    p_mcp.add_argument("--port", type=int, default=8000)
    p_mcp.add_argument("--scope", choices=["full", "tier1_only", "readonly"], default=None)

    args = parser.parse_args(argv)

    if args.command == "networks":
        print(json.dumps(list_supported_networks(), indent=2))
        return 0

    if args.command == "triage":
        try:
            app = ConnectionApplication(
                network_id=args.network,
                connection_node=args.node,
                asset_type=AssetType(args.asset),
                proposed_capacity_kw=args.kw,
                bess_power_kw=args.bess_kw,
                feeder_background_scale=args.scale,
                operating_load_factor=args.load_factor,
                inverter_mode=InverterMode(args.inverter_mode),
                study_type=StudyType(args.study_type),
                solver_timeout_sec=args.timeout,
            )
            agent = GridScreeningAgent(provider=args.provider)
            verdict = agent.screen_application(app, force_tier2=args.force_tier2)
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 2
        if args.svg:
            Path(args.svg).write_text(render_verdict_svg(verdict), encoding="utf-8")
        _print_verdict(verdict, as_json=args.json)
        return 0

    if args.command == "ask":
        try:
            agent = GridScreeningAgent(provider=args.provider)
            verdict = agent.screen_natural_language(
                args.inquiry,
                force_tier2=args.force_tier2,
                study_type=StudyType(args.study_type),
            )
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 2
        if args.svg:
            Path(args.svg).write_text(render_verdict_svg(verdict), encoding="utf-8")
        _print_verdict(verdict, as_json=args.json)
        return 0

    if args.command == "serve":
        from grid_screening_agent.api import app as fastapi_app

        uvicorn.run(fastapi_app, host=args.host, port=args.port)
        return 0

    if args.command == "mcp":
        from grid_screening_agent.mcp_server import run_mcp_server

        run_mcp_server(
            transport=args.transport,
            host=args.host,
            port=args.port,
            scope=args.scope,
        )
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
