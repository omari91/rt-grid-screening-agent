# Contributing to `rt-grid-screening-agent`

Thank you for your interest in contributing to `rt-grid-screening-agent`! Whether you are adding new distribution feeders, regional grid-code droop curves (e.g., VDE-AR-N 4105/4110, IEEE 1547, ENA EREC G99 alongside AS/NZS 4777.2 & NER S5.2.5), substation benchmarks, or MCP/API enhancements, this guide explains how to extend the engine safely without introducing numerical drift.

---

## 1. Core Architectural Non-Negotiables

Every pull request must preserve the three foundational invariants of the engine (**"LLM Orchestrates, Physics Solves, Gate Bounds"**):

1. **LLMs Never Compute Electrical Quantities**: Language models may parse unstructured text into a validated `ConnectionApplication` or summarize an already-verified `ScreeningVerdict`, but every impedance, voltage, ampacity, Short Circuit Ratio (`N-1 SCR`), and `ClearanceDecision` **must** come from deterministic Python / OpenDSS functions.
2. **Zero Working-Directory Mutation & Subprocess Isolation**: `opendssdirect.py` uses a process-global C singleton and relative `Redirect` paths in `.dss` files. Parent API/MCP processes must **never** call `os.chdir()` or import `opendssdirect` directly on request threads—all unbalanced 3-phase OpenDSS solves execute inside `src/grid_screening_agent/physics/_dss_runner.py` via a timeout-bounded, semaphore-throttled child process.
3. **Supervisory Physics Sanity Gate Cannot Be Bypassed**: Any solver non-convergence (`solver_failures > 0`), worker timeout (`timed_out == True`), unphysical per-unit voltage (`< 0.50 pu` or `> 1.40 pu`), or weak-grid boundary breach (`N-1 SCR < 2.0`) must be caught deterministically in `guardrails.py`.

---

## 2. Local Development Setup

Requires **Python `>= 3.10`** (`3.11–3.13` recommended).

```bash
# Fork & clone the repository
git clone https://github.com/<your-username>/rt-grid-screening-agent.git
cd rt-grid-screening-agent

# Option A: Using uv (recommended)
uv sync --extra dev
uv run pytest -v

# Option B: Using standard venv + pip
python3 -m venv .venv
./.venv/bin/pip install -e ".[dev]"
./.venv/bin/pytest -v
```

Keep runtime dependencies lean so the multi-stage production container (`Dockerfile`) stays under **`300 MB`**. Do not add heavy dataframe/notebook libraries (`pandas`, `pandapower`, `scipy`, `matplotlib`) to `[project.dependencies]`.

---

## 3. Docstring Standard: Explain *"Why"*, Not Just *"What"*

In safety-critical power-systems software, future maintainers and TSO/DNSP engineers need to understand **why** a threshold, clamp, or isolation boundary exists directly from IDE tooltips and source docstrings—without hunting through external audit memos.

When adding or modifying functions, include a `Why ...:` note in the docstring whenever physical or operational subtleties apply, for example:
- Why `PVSystem` elements set `vminpu=0.70 vmaxpu=1.50` (preventing OpenDSS's default `vmaxpu=1.10` constant-impedance fallback from distorting MPPT PWM inverter current).
- Why thermal loading is re-measured *after* Volt-VAr reactive absorption (`I_q` ampacity inflation).
- Why `reproducibility_sha256` excludes `application_id`, `receipt_id`, `timestamp_utc`, and `execution_ms`.

---

## 4. How to Contribute Common Extensions

### A. Adding a New LV OpenDSS Feeder or MV/HV Benchmark
1. **LV OpenDSS Feeder (`.dss`)**:
   - Place sanitized, open-license `.dss` files (`Master.dss`, `LineCodes.dss`, `Lines.dss`, `Loads.dss`) under `src/grid_screening_agent/fixtures/networks/<NETWORK_ID>/`.
   - Register metadata in `LV_NETWORK_META` (`src/grid_screening_agent/physics/topology.py`) and add the ID to `SupportedNetworkId` and `SUPPORTED_NETWORK_IDS` (`src/grid_screening_agent/models.py`).
   - Verify `compute_directory_sha256()` produces a stable 64-character hex digest and `inspect_connection_point("<NETWORK_ID>")` never mutates `os.getcwd()`.
2. **MV Benchmark or Transmission Substation**:
   - Add the benchmark entry (`nominal_kv`, `s_sc_mva`, `s_sc_n1_mva`, `xr_ratio` / `rx_ratio`, thermal rating) to `src/grid_screening_agent/fixtures/reference_profiles.json`.
   - Add the ID to `SupportedNetworkId` and `SUPPORTED_NETWORK_IDS` in `models.py` (and optional natural-language aliases in `parse_natural_language_inquiry` in `agent.py`).

### B. Adding or Updating MCP Tools (`grid-screen-mcp`)
1. Expose tool parameters using flat `Literal[...]` type aliases (`SupportedNetworkId`, `AssetTypeLiteral`, `InverterModeLiteral`, `StudyTypeLiteral`) in `src/grid_screening_agent/mcp_server.py` so `tools/list` emits flat JSON-Schema `"enum"` arrays without `$ref` indirection.
2. Keep tool docstrings non-overlapping: state clearly in the first sentence whether a tool performs catalog lookup, `<10 ms` analytical short-circuit/voltage screening, or isolated child-process AC power flow, and state explicit scope limits (e.g., `snapshot` vs. `time_series` vs. live telemetry).
3. Test over real `stdio` JSON-RPC (`grid-screen-mcp --transport stdio`) to ensure zero `stdout` pollution breaks the protocol stream.

---

## 5. Pre-PR Verification & Zero-Drift Check

Before opening a Pull Request, run the full verification checklist:

```bash
# 1. Run the complete unit, OpenDSS subprocess, API, CLI, and MCP test suite
uv run pytest -v

# 2. Verify CLI fast-track and regulatory escalation flows
uv run grid-screen triage --network R --kw 3.0 --scale 3.0
uv run grid-screen ask "Screen a 150 MW hyperscale data centre at Broken Hill 132 kV"

# 3. Verify zero numerical drift on canonical reproducibility digests
uv run python -c "
from grid_screening_agent.mcp_server import screen_connection_application
v1 = screen_connection_application(network_id='R', proposed_capacity_kw=25.0, bess_power_kw=15.0, feeder_background_scale=6.0, asset_type='pv_plus_bess')
v2 = screen_connection_application(network_id='R', proposed_capacity_kw=25.0, bess_power_kw=15.0, feeder_background_scale=6.0, asset_type='pv_plus_bess')
assert v1['audit_receipt']['reproducibility_sha256'] == v2['audit_receipt']['reproducibility_sha256']
print('Reproducibility SHA-256 verified (zero drift):', v1['audit_receipt']['reproducibility_sha256'])
"
```

If a PR intentionally updates a physical model or statutory curve that changes canonical `reproducibility_sha256` digests, document the exact physical equation change and before/after benchmark numbers in your PR description.

