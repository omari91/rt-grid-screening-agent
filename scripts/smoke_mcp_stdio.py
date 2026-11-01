"""Installed-wheel MCP stdio smoke test (used by CI after `pip install dist/*.whl`).

Why this runs against the *installed* console script rather than the source tree:
    It proves the published artifact — entry point, bundled CSIRO fixtures, isolated
    OpenDSS child worker — works end-to-end over real JSON-RPC stdio, exactly as
    `uvx grid-screen-mcp` will run it on a user's machine.

Why it asserts physics values and hash *stability* instead of a fixed hash prefix:
    `reproducibility_sha256` deliberately includes `runtime_versions` (Python and
    OpenDSSDirect.py). A CI runner on Python 3.11 legitimately produces a different
    digest from a developer laptop on 3.13; what must never change is (a) the physics
    numbers and (b) identical digests for identical inputs on the same runtime.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
from typing import Any

from mcp import Client, StdioServerParameters

EXPECTED_TOOLS = 9
S1A_ARGS: dict[str, Any] = {
    "network_id": "R",
    "proposed_capacity_kw": 25.0,
    "bess_power_kw": 15.0,
    "feeder_background_scale": 6.0,
    "asset_type": "pv_plus_bess",
    "study_type": "snapshot",
}


def _json(result: Any) -> dict[str, Any]:
    if getattr(result, "is_error", False) or getattr(result, "isError", False):
        raise RuntimeError(f"MCP tool error: {result}")
    for block in result.content:
        text = getattr(block, "text", None)
        if text:
            return json.loads(text)
    raise RuntimeError("No JSON text content in MCP result")


async def main() -> None:
    exe = shutil.which("grid-screen-mcp")
    if exe is None:
        sys.exit("FAIL: grid-screen-mcp console script not found on PATH")

    params = StdioServerParameters(command=exe, args=["--transport", "stdio"])
    async with Client(params, read_timeout_seconds=90.0) as client:
        tools = (await client.list_tools()).tools
        assert len(tools) == EXPECTED_TOOLS, f"expected {EXPECTED_TOOLS} tools, got {len(tools)}"

        first = _json(await client.call_tool("screen_connection_application", S1A_ARGS))
        second = _json(await client.call_tool("screen_connection_application", S1A_ARGS))

        t2 = first["tier2"]
        assert t2["peak_voltage_pu"] == 1.12315, t2["peak_voltage_pu"]
        assert t2["max_line_loading_pct"] == 157.4, t2["max_line_loading_pct"]
        assert t2["intervals_solved"] == 17, t2["intervals_solved"]
        assert first["decision"] == "CONDITIONAL_DOE_OR_CAP", first["decision"]
        h1 = first["audit_receipt"]["reproducibility_sha256"]
        h2 = second["audit_receipt"]["reproducibility_sha256"]
        assert h1 == h2, "reproducibility_sha256 drifted between identical calls"

        bh = _json(
            await client.call_tool(
                "screen_connection_application",
                {"network_id": "BrokenHill_132kV", "proposed_capacity_kw": 150000.0, "asset_type": "hyperscale_datacentre"},
            )
        )
        assert bh["tier1"]["scr_n1"] == 1.26, bh["tier1"]["scr_n1"]
        assert bh["regulatory_override"] == "NER S5.2.5 System Strength violation"

    print(
        f"OK: {len(tools)} tools | S1A 1.12315 pu / 157.4% | BrokenHill SCR_N1 1.26 | "
        f"runtime hash {h1[:16]} stable across calls"
    )


if __name__ == "__main__":
    asyncio.run(main())

