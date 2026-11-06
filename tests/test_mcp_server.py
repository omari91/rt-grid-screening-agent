"""Tests for MCP server registration, zero-trust auth/scoping, resources, sweeps, and schemas."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from unittest.mock import patch

from starlette.testclient import TestClient

from grid_screening_agent.mcp_server import (
    build_mcp_server,
    classify_screening_regime,
    create_mcp_http_app,
    inspect_connection_point,
    mcp,
    run_tier2_ac_study,
    screen_connection_application,
    search_tools,
    sweep_connection_capacities,
)
from grid_screening_agent.models import SUPPORTED_NETWORK_IDS, AssetType, InverterMode, StudyType
from grid_screening_agent.tools import TOOL_SPECS


def test_mcp_server_registers_all_full_scope_tools() -> None:
    tools = asyncio.run(mcp.list_tools())
    tool_names = {t.name for t in tools}
    expected = {
        "list_networks",
        "inspect_connection_point",
        "classify_screening_regime",
        "search_tools",
        "run_tier1_screen",
        "run_tier2_ac_study",
        "propose_mitigations",
        "screen_connection_application",
        "sweep_connection_capacities",
        "triage_connection_cluster",
    }
    assert expected == tool_names


def test_mcp_scope_tier1_only_and_readonly_hide_child_process_tools() -> None:
    """Domain 1: Confused-deputy defense — restricted scopes never expose OpenDSS child-process tools."""
    t1_server = build_mcp_server(scope="tier1_only")
    t1_names = {t.name for t in asyncio.run(t1_server.list_tools())}
    assert t1_names == {
        "list_networks",
        "inspect_connection_point",
        "classify_screening_regime",
        "search_tools",
        "run_tier1_screen",
        "triage_connection_cluster",
    }

    ro_server = build_mcp_server(scope="readonly")
    ro_names = {t.name for t in asyncio.run(ro_server.list_tools())}
    assert ro_names == {
        "list_networks",
        "inspect_connection_point",
        "classify_screening_regime",
        "search_tools",
    }


def test_mcp_zero_trust_bearer_auth_middleware_on_sse_transport() -> None:
    """Domain 1: Remote HTTP/SSE MCP transport enforces GRID_AGENT_API_TOKEN via constant-time check."""
    sse_app = create_mcp_http_app(transport="sse", host="127.0.0.1")
    client = TestClient(sse_app)

    with patch.dict(os.environ, {"GRID_AGENT_API_TOKEN": "mcp-secret-token-99"}, clear=False):
        assert client.get("/health").status_code == 200
        r_missing = client.post("/messages/")
        assert r_missing.status_code == 401
        assert "Bearer" in r_missing.json()["error"]

        r_wrong = client.post(
            "/messages/",
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert r_wrong.status_code == 403


def test_mcp_tier1_docstring_states_fast_screen_no_full_ac() -> None:
    tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
    t1_desc = tools["run_tier1_screen"].description or ""
    required = "Calculates fast short-circuit and voltage screening. Returns a pass/fail. Does NOT run full AC power flow."
    assert required in t1_desc


def test_mcp_tier2_and_mitigation_docstrings_clarify_scope_and_wlt() -> None:
    tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
    t2_desc = tools["run_tier2_ac_study"].description or ""
    mit_desc = tools["propose_mitigations"].description or ""
    assert "snapshot" in t2_desc and "time_series" in t2_desc
    assert "17,520" in t2_desc
    assert "DOE_SNAPSHOT_ENVELOPE" in mit_desc
    assert "NOT a live" in mit_desc


def test_mcp_tool_schemas_include_study_type_enum() -> None:
    specs_by_name = {s["name"]: s for s in TOOL_SPECS}
    t2_props = specs_by_name["run_tier2_ac_study"]["parameters"]["properties"]
    assert t2_props["study_type"]["enum"] == ["snapshot", "time_series"]


def test_mcp_inspect_and_classify_roundtrip() -> None:
    prof = inspect_connection_point(network_id="R")
    assert prof["connection_node"] == "3110858"
    reg = classify_screening_regime(network_id="R", proposed_capacity_kw=10.0)
    assert reg["regime_id"] == "LV_RESISTIVE_MAGNITUDE_DOMINATED"


def test_mcp_fast_track_screening_roundtrip() -> None:
    verdict = screen_connection_application(
        network_id="R",
        proposed_capacity_kw=3.0,
        feeder_background_scale=3.0,
    )
    assert verdict["decision"] == "FAST_TRACK_APPROVED"
    assert verdict["requires_human_review"] is False
    assert verdict["sanity_gate"]["passed"] is True


def test_mcp_ner_s525_regulatory_override_roundtrip() -> None:
    verdict = screen_connection_application(
        network_id="BrokenHill_132kV",
        asset_type="hyperscale_datacentre",
        proposed_capacity_kw=150_000.0,
    )
    assert verdict["decision"] == "ESCALATE_DETAILED_STUDY"
    assert verdict["regulatory_override"] == "NER S5.2.5 System Strength violation"
    assert verdict["requires_human_review"] is True


def test_mcp_live_tool_schemas_enforce_strict_enums_for_network_asset_inverter_study() -> None:
    tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
    t2_schema = getattr(tools["run_tier2_ac_study"], "input_schema", None) or getattr(
        tools["run_tier2_ac_study"], "inputSchema"
    )
    props = t2_schema["properties"]
    assert props["network_id"]["enum"] == list(SUPPORTED_NETWORK_IDS)
    assert props["asset_type"]["enum"] == [e.value for e in AssetType]
    assert props["inverter_mode"]["enum"] == [e.value for e in InverterMode]
    assert props["study_type"]["enum"] == [e.value for e in StudyType]


def test_mcp_verdict_includes_binding_constraints_and_reproducibility_sha256() -> None:
    v1 = screen_connection_application(
        network_id="R",
        proposed_capacity_kw=3.0,
        feeder_background_scale=3.0,
    )
    v2 = screen_connection_application(
        network_id="R",
        proposed_capacity_kw=3.0,
        feeder_background_scale=3.0,
    )
    assert v1["binding_constraints"] == ["NONE"]
    assert len(v1["audit_receipt"]["reproducibility_sha256"]) == 64
    assert v1["audit_receipt"]["reproducibility_sha256"] == v2["audit_receipt"]["reproducibility_sha256"]


def test_mcp_all_tools_publish_typed_output_schemas_and_annotations() -> None:
    """Every registered MCP tool must publish an explicit JSON outputSchema and openWorldHint=False."""
    tools = asyncio.run(mcp.list_tools())
    assert len(tools) == 10
    for tool in tools:
        out_schema = getattr(tool, "output_schema", None) or getattr(tool, "outputSchema", None)
        assert isinstance(out_schema, dict), f"Tool {tool.name} is missing outputSchema!"
        assert out_schema.get("type") == "object"
        assert len(out_schema.get("properties", {})) >= 1, f"Tool {tool.name} has empty outputSchema properties!"
        ann = getattr(tool, "annotations", None)
        assert ann is not None
        ann_dump = ann.model_dump(by_alias=True) if hasattr(ann, "model_dump") else dict(ann)
        assert ann_dump.get("openWorldHint") is False, f"Tool {tool.name} must set openWorldHint=False!"


def test_mcp_trajectory_pruning_and_off_context_resources_preserve_reproducibility_hash() -> None:
    """Domain 2: Pruning trajectories saves LLM tokens, preserves SHA-256, and serves URI resources."""
    v_pruned = screen_connection_application(
        network_id="R",
        proposed_capacity_kw=25.0,
        bess_power_kw=15.0,
        feeder_background_scale=6.0,
        asset_type="pv_plus_bess",
        study_type="snapshot",
        include_trajectory=False,
    )
    v_full = screen_connection_application(
        network_id="R",
        proposed_capacity_kw=25.0,
        bess_power_kw=15.0,
        feeder_background_scale=6.0,
        asset_type="pv_plus_bess",
        study_type="snapshot",
        include_trajectory=True,
    )
    assert v_pruned["tier2"]["voltage_profile"] == []
    assert len(v_full["tier2"]["voltage_profile"]) == 17
    assert (
        v_pruned["audit_receipt"]["reproducibility_sha256"]
        == v_full["audit_receipt"]["reproducibility_sha256"]
    )
    assert v_pruned["audit_receipt"]["reproducibility_sha256"].startswith("eddbe8832ef3d785")

    traj_uri = v_pruned["tier2"]["trajectory_resource_uri"]
    assert traj_uri.startswith("grid://verdicts/") and traj_uri.endswith("/trajectory")

    # Verify static & template MCP resources
    resources = asyncio.run(mcp.list_resources())
    res_uris = {str(r.uri) for r in resources}
    assert "grid://catalog/networks" in res_uris
    assert "grid://queue/stats" in res_uris

    templates = asyncio.run(mcp.list_resource_templates())
    tmpl_uris = {str(getattr(t, "uri_template", None) or getattr(t, "uriTemplate", "")) for t in templates}
    assert "grid://verdicts/{fingerprint}/trajectory" in tmpl_uris
    assert "grid://verdicts/{fingerprint}/plot.svg" in tmpl_uris

    traj_contents = asyncio.run(mcp.read_resource(traj_uri))
    traj_text = traj_contents[0].content if hasattr(traj_contents[0], "content") else traj_contents[0].text
    traj_json = json.loads(traj_text)
    assert traj_json["points_count"] == 17
    assert len(traj_json["voltage_profile"]) == 17

    fp = v_pruned["audit_receipt"]["queue_fingerprint"]
    svg_contents = asyncio.run(mcp.read_resource(f"grid://verdicts/{fp}/plot.svg"))
    svg_text = svg_contents[0].content if hasattr(svg_contents[0], "content") else svg_contents[0].text
    assert "<svg" in svg_text and "Upper Statutory Limit" in svg_text

    # Standalone run_tier2_ac_study also prunes by default and parks trajectory resource
    t2_standalone = run_tier2_ac_study(
        network_id="CIGRE_MV_Rudolstadt",
        asset_type="ev_or_commercial_load",
        proposed_capacity_kw=3000.0,
    )
    assert t2_standalone["voltage_profile"] == []
    assert t2_standalone["trajectory_resource_uri"].startswith("grid://verdicts/")


def test_mcp_sweep_connection_capacities_and_search_tools() -> None:
    """Domain 2: Server-side sweep locates inflection without N LLM round-trips."""
    sweep = sweep_connection_capacities(
        network_id="R",
        start_kw=3.0,
        end_kw=25.0,
        steps=5,
        feeder_background_scale=3.0,
        halt_on_violation=True,
    )
    assert sweep["max_compliant_kw"] >= 3.0
    assert sweep["steps_evaluated"] >= 2
    assert len(sweep["sweep_results"]) == sweep["steps_evaluated"]

    disc = search_tools("sweep")
    assert any(item["name"] == "sweep_connection_capacities" for item in disc["matched_tools"])


def test_dss_runner_defers_opendssdirect_import_out_of_parent_process() -> None:
    """Importing `_dss_runner` in parent process must NOT load C++ `opendssdirect` bindings."""
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "import grid_screening_agent.physics._dss_runner; "
                "assert 'opendssdirect' not in sys.modules, 'C++ bindings leaked into parent!'"
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr


def test_mcp_triage_connection_cluster_and_ups_modes() -> None:
    """Test multi-party queue cluster triage and UPS reactive compensation modes."""
    from grid_screening_agent.mcp_server import triage_connection_cluster
    from grid_screening_agent.tools import tool_propose_mitigations

    # 1. Test triage_connection_cluster tool across Western Sydney cluster
    batch = [
        {"project_id": "DC_EASTERN_CK", "network_id": "EasternCreek_132kV", "proposed_capacity_mw": 120.0, "admin_readiness_score": 85.0},
        {"project_id": "DC_SYD_WEST", "network_id": "SydneyWest_330kV", "proposed_capacity_mw": 250.0, "admin_readiness_score": 90.0},
        {"project_id": "BESS_KEMPS_CK", "network_id": "KempsCreek_330kV", "proposed_capacity_mw": 100.0, "admin_readiness_score": 70.0},
    ]
    res = triage_connection_cluster(batch)
    assert res["total_projects"] == 3
    assert res["total_proposed_mw"] == 470.0
    assert len(res["items"]) == 3
    assert res["items"][0]["rank"] == 1
    assert len(res["audit_sha256"]) == 64

    # 2. Test UPS reactive compensation mitigation synthesis for hyperscale data centre
    mits = tool_propose_mitigations(
        network_id="EasternCreek_132kV",
        proposed_capacity_kw=80000.0,  # 80 MW
        asset_type="hyperscale_datacentre",
        inverter_mode="ups_leading_pf095",
    )
    ups_mit = next(
        (m for m in mits["mitigation_options"] if m["option_id"] == "UPS_4QUADRANT_REACTIVE_COMPENSATION"),
        None,
    )
    assert ups_mit is not None
    assert "Capacitive Support" in ups_mit["title"]
    assert ups_mit["compliant"] is True

