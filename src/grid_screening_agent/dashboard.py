"""Zero-dependency HTML5 Operator Compliance & Interconnection Queue Dashboard.

Why this module exists:
    Provides DNSP/TSO connection engineers with a lightweight, self-contained browser
    control room (`GET /dashboard`) directly inside the FastAPI service—monitoring
    interconnection backlog reduction, state-compression duplicate hits, inline SVG
    voltage trajectories, and SHA-256 cryptographic audit logs without adding external
    UI framework dependencies (`fasthtml`, `pandas`, or `pandapower`) to the runtime image.
"""

from __future__ import annotations

import html
from typing import Any, Optional

from grid_screening_agent import __version__
from grid_screening_agent.models import (
    SUPPORTED_NETWORK_IDS,
    AssetType,
    ScreeningVerdict,
    StudyType,
)
from grid_screening_agent.plotting import render_verdict_svg

_BADGE_STYLES: dict[str, str] = {
    "FAST_TRACK_APPROVED": "background:#059669;color:#fff;",
    "CONDITIONAL_DOE_OR_CAP": "background:#d97706;color:#fff;",
    "ESCALATE_DETAILED_STUDY": "background:#dc2626;color:#fff;",
    "ABSTAIN_DATA_OR_SOLVER_FAULT": "background:#7c3aed;color:#fff;",
}


def render_compliance_dashboard_html(
    queue_stats: dict[str, Any],
    latest_verdict: Optional[ScreeningVerdict] = None,
    error_message: Optional[str] = None,
) -> str:
    """Render the complete Operator Compliance & Interconnection Queue Dashboard HTML."""
    net_options = "\n".join(
        f'<option value="{html.escape(nid)}">{html.escape(nid)}</option>'
        for nid in SUPPORTED_NETWORK_IDS
    )
    asset_options = "\n".join(
        f'<option value="{html.escape(a.value)}">{html.escape(a.value)}</option>'
        for a in AssetType
    )
    study_options = "\n".join(
        f'<option value="{html.escape(s.value)}">{html.escape(s.value)}</option>'
        for s in StudyType
    )

    rows_html: list[str] = []
    for entry in queue_stats.get("recent_audit_entries", []):
        dec = str(entry.get("decision", ""))
        badge_css = _BADGE_STYLES.get(dec, "background:#475569;color:#fff;")
        cap_kw = float(entry.get("proposed_capacity_kw", 0.0))
        nom_kv = float(entry.get("nominal_kv", 0.415))
        cap_str = f"{cap_kw / 1000.0:g} MW" if nom_kv >= 66.0 else f"{cap_kw:g} kW"
        repro_short = html.escape(str(entry.get("reproducibility_sha256", ""))[:12])
        loc_str = html.escape(f"{entry.get('network_id')} ({entry.get('connection_node')})")
        constraints = html.escape(", ".join(entry.get("binding_constraints", [])) or "NONE")
        cache_tag = (
            ' <span style="background:#0284c7;color:#fff;padding:2px 6px;border-radius:4px;font-size:0.7rem;">CACHE HIT</span>'
            if entry.get("cache_hit")
            else ""
        )
        rows_html.append(
            "<tr>"
            f'<td style="font-family:ui-monospace,monospace;font-size:0.8rem;">{repro_short}…</td>'
            f"<td>{html.escape(cap_str)} <small style='color:#64748b;'>({html.escape(str(entry.get('asset_type', '')))})</small></td>"
            f"<td>{loc_str}</td>"
            f"<td>{float(entry.get('peak_voltage_pu', 1.0)):.4f} pu</td>"
            f"<td>{float(entry.get('scr_n1', 0.0)):.2f}</td>"
            f'<td><span style="{badge_css}padding:3px 8px;border-radius:4px;font-weight:700;font-size:0.74rem;">{html.escape(dec)}</span>{cache_tag}</td>'
            f"<td style='font-size:0.82rem;'>{constraints}</td>"
            "</tr>"
        )

    table_rows = (
        "\n".join(rows_html)
        if rows_html
        else '<tr><td colspan="7" style="text-align:center;color:#64748b;padding:1.2rem;">No screening applications processed yet. Submit a stress-test request above.</td></tr>'
    )

    error_banner = (
        f'<div style="background:#fef2f2;border:1px solid #dc2626;color:#991b1b;padding:0.85rem 1rem;border-radius:8px;margin-bottom:1.2rem;">'
        f"<strong>Validation Gate Intercepted Input:</strong> {html.escape(error_message)}</div>"
        if error_message
        else ""
    )

    svg_panel = ""
    if latest_verdict is not None:
        svg_markup = render_verdict_svg(latest_verdict)
        svg_panel = (
            '<section style="background:#fff;border:1px solid #cbd5e1;border-radius:10px;padding:1.1rem;margin-bottom:1.5rem;">'
            f"<h3 style='margin:0 0 0.6rem 0;font-size:1.05rem;'>Latest Engineering Verdict &amp; Trajectory</h3>"
            f"<div style='overflow-x:auto;'>{svg_markup}</div>"
            "</section>"
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Grid Interconnection Assessment &amp; Compliance Dashboard</title>
  <style>
    body {{ font-family: system-ui, -apple-system, sans-serif; background: #f8fafc; color: #0f172a; margin: 0; padding: 1.8rem; }}
    .container {{ max-width: 1120px; margin: 0 auto; }}
    .kpis {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 1rem; margin: 1.2rem 0 1.5rem 0; }}
    .kpi {{ background: #fff; border: 1px solid #cbd5e1; border-radius: 10px; padding: 1rem 1.2rem; }}
    .kpi h3 {{ margin: 0; font-size: 1.75rem; }}
    .kpi p {{ margin: 0.25rem 0 0 0; color: #475569; font-size: 0.88rem; }}
    .card {{ background: #fff; border: 1px solid #cbd5e1; border-radius: 10px; padding: 1.25rem; margin-bottom: 1.5rem; }}
    .form-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 0.9rem; align-items: end; }}
    label {{ display: block; font-size: 0.82rem; font-weight: 600; color: #334155; margin-bottom: 0.3rem; }}
    input, select {{ width: 100%; padding: 0.52rem 0.6rem; border: 1px solid #cbd5e1; border-radius: 6px; box-sizing: border-box; font-size: 0.9rem; }}
    button {{ background: #0f172a; color: #fff; border: none; border-radius: 6px; padding: 0.6rem 1rem; font-weight: 600; cursor: pointer; font-size: 0.9rem; }}
    button:hover {{ background: #1e293b; }}
    table {{ width: 100%; border-collapse: collapse; background: #fff; border: 1px solid #cbd5e1; border-radius: 10px; overflow: hidden; }}
    th, td {{ text-align: left; padding: 0.7rem 0.85rem; border-bottom: 1px solid #e2e8f0; }}
    th {{ background: #f1f5f9; font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.03em; color: #475569; }}
  </style>
</head>
<body>
  <div class="container">
    <h1 style="margin:0;font-size:1.55rem;">⚡ rt-grid-screening-agent • Interconnection Queue &amp; Compliance Dashboard</h1>
    <p style="margin:0.35rem 0 0 0;color:#475569;font-size:0.92rem;">
      <em>"LLM Orchestrates, Physics Solves, Gate Bounds"</em> — Engine Core <code>v{html.escape(__version__)}</code> (Isolated OpenDSS 3-Phase AC + Exact N-1 Contingency Gate)
    </p>
    <div class="kpis">
      <div class="kpi"><h3>{queue_stats['total_screenings_handled']}</h3><p>Applications Screened</p></div>
      <div class="kpi"><h3 style="color:#059669;">{queue_stats['fast_track_clearances']}</h3><p>Fast-Track Clearances</p></div>
      <div class="kpi"><h3 style="color:#0284c7;">{queue_stats['backlog_duplicates_intercepted']}</h3><p>Backlog Duplicates Intercepted</p></div>
      <div class="kpi"><h3 style="color:#d97706;">{queue_stats['escalated_to_human_review']}</h3><p>Escalated to Human Review</p></div>
    </div>
    {error_banner}
    <div class="card">
      <h3 style="margin:0 0 0.85rem 0;font-size:1.05rem;">Simulate Connection Screening Request</h3>
      <form action="/dashboard/submit" method="post">
        <div class="form-grid">
          <div><label for="network_id">Network / Substation</label><select id="network_id" name="network_id">{net_options}</select></div>
          <div><label for="asset_type">Asset Class</label><select id="asset_type" name="asset_type">{asset_options}</select></div>
          <div><label for="proposed_capacity_kw">Capacity Rating (kW)</label><input id="proposed_capacity_kw" name="proposed_capacity_kw" type="number" step="any" value="25" required /></div>
          <div><label for="bess_power_kw">Co-located BESS (kW)</label><input id="bess_power_kw" name="bess_power_kw" type="number" step="any" value="15" /></div>
          <div><label for="feeder_background_scale">Solar Scale (0.5–10x)</label><input id="feeder_background_scale" name="feeder_background_scale" type="number" step="0.1" value="6.0" /></div>
          <div><label for="study_type">Study Scope</label><select id="study_type" name="study_type">{study_options}</select></div>
          <div><button type="submit">Run Interconnection Check</button></div>
        </div>
      </form>
    </div>
    {svg_panel}
    <h3 style="margin:0 0 0.65rem 0;font-size:1.05rem;">Cryptographic Verification &amp; Regulatory Audit Trail</h3>
    <table>
      <thead>
        <tr>
          <th>Reproducibility SHA-256</th>
          <th>Capacity</th>
          <th>Target Location</th>
          <th>Peak Voltage</th>
          <th>N-1 SCR</th>
          <th>Decision Status</th>
          <th>Binding Constraints</th>
        </tr>
      </thead>
      <tbody>
        {table_rows}
      </tbody>
    </table>
  </div>
</body>
</html>"""

