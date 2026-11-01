# Changelog

All notable changes to the **Grid Connection Screening Agent** (`grid-screen-mcp` / `rt-grid-screening-agent`) will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [0.1.0] - 2026-11-01

### Added
- **Model Context Protocol (MCP) Server (`grid-screen-mcp`)**:
  - Exposes 9 deterministic physics tools: `list_networks`, `inspect_connection_point`, `classify_screening_regime`, `search_tools`, `run_tier1_screen`, `run_tier2_ac_study`, `propose_mitigations`, `screen_connection_application`, and `sweep_connection_capacities`.
  - Dynamic tool scoping via `GRID_AGENT_MCP_SCOPE` (`full`, `tier1_only`, `readonly`) to prevent confused-deputy attacks on unprivileged agents.
  - Zero-trust streaming authentication middleware (`MCPAuthMiddleware`) enforcing constant-time `GRID_AGENT_API_TOKEN` Bearer validation on `sse` and `streamable-http` transports.
  - Off-context `@mcp.resource` URI trajectory parking (`grid://verdicts/{fingerprint}/trajectory` and `plot.svg`) to prevent LLM context token bloat.
  - Server-side hosting capacity sweeps (`sweep_connection_capacities`) finding exact constraint inflections without sequential LLM round-trips.
  - Strict JSON Schema input `enum` constraints and typed `outputSchema` definitions (`structuredContent`) with `openWorldHint=false` across all tools.

- **Physics & Power-Flow Solvers**:
  - Isolated child-process 3-phase OpenDSS AC power-flow runner (`physics/_dss_runner.py`) with C-singleton quarantine, working-directory confinement, and `BoundedSemaphore` concurrency control (`GRID_AGENT_MAX_DSS_WORKERS`).
  - Child-process sandboxing with host secret scrubbing (`_SAFE_WORKER_ENV_KEYS`) and network socket denial (`install_network_socket_sandbox`).
  - AS/NZS 4777.2 Volt-VAr and Volt-Watt droop solver with post-droop thermal re-measurement ($I_q$ reactive current check), preventing conductor/transformer overload masking.
  - Sub-10ms analytical Tier-1 screening combining season-stratified voltage calibration (1.0752 pu), thermal loading, and Pareto joint headroom.
  - Post-contingency $N-1$ Short-Circuit Ratio calculation enforcing NEM National Electricity Rules (NER) S5.2.5 system strength floors ($\text{SCR}_{N-1} < 2.0$).
  - Weak-grid human-in-the-loop escalation triggers for borderline system strength ($2.0 \le \text{SCR}_{N-1} < 3.0$).

- **Interconnection Queue & Audit Ledger**:
  - In-memory state-compression queue deduplication (`InterconnectionQueueTracker`) intercepting identical sizing requests in $<0.1\text{ ms}$.
  - Dual cryptographic digests in `AuditReceipt`: `feeder_model_sha256` (raw `.dss` topology) and bit-for-bit stable `reproducibility_sha256`.
  - Deterministic Supervisory Physics Sanity Gate validating tool outputs, binding constraint codes, and regulatory overrides.

- **FastAPI Transport & Operator Dashboard (`grid-screen-api`)**:
  - Interactive HTML5 Operator Compliance Dashboard (`GET /dashboard`) with live queue metrics and audit log table.
  - Structured screening (`POST /v1/screen`) and resilient natural-language intake (`POST /v1/screen/intake`).
  - Zero-dependency vector SVG plotting (`POST /v1/screen/plot`, `POST /v1/screen/intake/plot`) rendering voltage trajectories against AS/NZS statutory envelopes.
  - Air-gapped privacy enforcement (`GRID_AGENT_AIRGAPPED=1`) blocking outbound cloud LLM calls on confidential connection studies.

- **Packaging & Registry Integration**:
  - Hatchling distribution build in `pyproject.toml` publishing package `grid-screen-mcp` with bundled CSIRO networks R & H `.dss` and JSON fixtures.
  - Three console scripts: `grid-screen` (CLI), `grid-screen-mcp` (MCP server), and `grid-screen-api` (FastAPI/Dashboard).
  - Official MCP Registry manifest (`server.json`) matching schema `2025-12-11`.
  - CI test matrix (`.github/workflows/ci.yml`) verifying pytest unit tests, Glue schema guards, and clean-venv wheel installation.
  - Continuous publishing workflow (`.github/workflows/publish.yml`) with GitHub OIDC Trusted Publishing to PyPI and `mcp-publisher`.
  - Release verification guard (`scripts/check_release.py`) and live MCP stdio client smoke test (`scripts/smoke_mcp_stdio.py`).
  - Certified engineering and regulatory boundaries report (`AUDIT.md`).

### Fixed
- **Audit Gap Q8**: Fixed the Volt-VAr reactive absorption paradox where inductive current ($I_q$) caused unmonitored conductor heating above rated ampacity.
- **Audit Gap Q13 & Q30**: Fixed non-deterministic screening outputs by enforcing canonical sorted input digests and deterministic `reproducibility_sha256`.
- **Audit Gap Q1, Q15 & Q16**: Fixed silent fallback to default capacities in natural-language intake; now returns `HTTP 422` when power ratings are missing or $>500\text{ kW}$ is submitted without a target voltage tier.
- **Audit Gap Q2 & Q3**: Fixed unmonitored borderline system strength by triggering human review on $2.0 \le \text{SCR}_{N-1} < 3.0$ and returning explicit machine-readable `binding_constraints`.
- **Audit Gap Q18**: Fixed open-ended MCP tool parameter schemas by binding strict `Literal` enums for network IDs, asset types, inverter modes, and study types.
- **Audit Gap Q17 & Q20**: Fixed unconstrained subprocess spawning with bounded worker semaphores and converted the Docker image to an unprivileged multi-stage build ($<300\text{ MB}$).
- **Audit Gap Q26 & Q28**: Fixed confidential network data leakage risks by enforcing air-gapped offline operation and replacing external charting dependencies with self-contained SVG generation.

