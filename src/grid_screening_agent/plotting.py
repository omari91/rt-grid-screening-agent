"""Zero-dependency vector SVG voltage & constraint envelope plotter for ScreeningVerdict.

Why SVG plots are generated natively via Python stdlib (`html`) instead of Matplotlib/Cairo:
    1. Container Budget (`<300 MB`): Plotting libraries (`matplotlib`, `pillow`, `fontconfig`,
       `pycairo`) add `>120 MB` of binary wheels, font caches, and C shared libraries.
    2. Crisp Browser & Portal Embedding: Pure vector `<svg>` renders sharply at any DPI
       inside the Operator Compliance Dashboard (`GET /dashboard`), DNSP portals, and
       static Markdown reports with `<1 ms` render latency and HTML-escaped labels.
"""

from __future__ import annotations

import html
from typing import Sequence

from grid_screening_agent.models import ClearanceDecision, ScreeningVerdict, VoltageProfilePoint

_DECISION_PALETTE: dict[ClearanceDecision, tuple[str, str]] = {
    ClearanceDecision.FAST_TRACK_APPROVED: ("#059669", "#ecfdf5"),
    ClearanceDecision.CONDITIONAL_DOE_OR_CAP: ("#d97706", "#fffbeb"),
    ClearanceDecision.ESCALATE_DETAILED_STUDY: ("#dc2626", "#fef2f2"),
    ClearanceDecision.ABSTAIN_DATA_OR_SOLVER_FAULT: ("#7c3aed", "#f5f3ff"),
}


def _polyline_points(
    values: Sequence[float],
    x0: float,
    width: float,
    y0: float,
    height: float,
    v_bottom: float,
    v_top: float,
) -> str:
    n = len(values)
    if n == 0:
        return ""
    span = max(v_top - v_bottom, 1e-6)
    coords: list[str] = []
    for idx, val in enumerate(values):
        x = x0 + (width / 2.0 if n == 1 else (idx / (n - 1)) * width)
        clamped = min(max(val, v_bottom), v_top)
        y = y0 + height - ((clamped - v_bottom) / span) * height
        coords.append(f"{x:.1f},{y:.1f}")
    return " ".join(coords)


def _y_pos(val: float, y0: float, height: float, v_bottom: float, v_top: float) -> float:
    span = max(v_top - v_bottom, 1e-6)
    clamped = min(max(val, v_bottom), v_top)
    return y0 + height - ((clamped - v_bottom) / span) * height


def render_verdict_svg(verdict: ScreeningVerdict) -> str:
    """Render a self-contained SVG engineering voltage & constraint plot for a ScreeningVerdict."""
    w, h = 880, 430
    x0, y0, pw, ph = 78.0, 96.0, 762.0, 244.0

    fg_badge, bg_badge = _DECISION_PALETTE.get(verdict.decision, ("#1e293b", "#f1f5f9"))
    v_max_lim = verdict.tier1.voltage_limit_max_pu
    v_min_lim = verdict.tier1.voltage_limit_min_pu
    strat_lim = verdict.tier1.season_stratified_threshold_pu

    pts: list[VoltageProfilePoint] = (
        list(verdict.tier2.voltage_profile)
        if (verdict.tier2 is not None and verdict.tier2.voltage_profile)
        else []
    )

    candidate_vs = [v_max_lim, v_min_lim, strat_lim, 1.0, verdict.tier1.predicted_peak_v_pu, verdict.tier1.predicted_min_v_pu]
    if pts:
        candidate_vs.extend(p.v_max_pu for p in pts)
        candidate_vs.extend(p.v_min_pu for p in pts)

    compliant_mit = next((m for m in verdict.mitigations if m.compliant and m.resulting_peak_v_pu is not None), None)
    if compliant_mit and compliant_mit.resulting_peak_v_pu is not None:
        candidate_vs.append(compliant_mit.resulting_peak_v_pu)

    v_bottom = round(min(candidate_vs) - 0.02, 2)
    v_top = round(max(candidate_vs) + 0.02, 2)

    y_vmax = _y_pos(v_max_lim, y0, ph, v_bottom, v_top)
    y_vmin = _y_pos(v_min_lim, y0, ph, v_bottom, v_top)
    y_strat = _y_pos(strat_lim, y0, ph, v_bottom, v_top)
    y_nom = _y_pos(1.0, y0, ph, v_bottom, v_top)

    headline = html.escape(verdict.headline)
    constraints_str = html.escape(", ".join(c.value for c in verdict.binding_constraints) or "NONE")
    regime_str = html.escape(verdict.regime.regime_id)
    repro_short = html.escape((verdict.audit_receipt.reproducibility_sha256 or "n/a")[:16])
    feeder_short = html.escape(verdict.audit_receipt.feeder_model_sha256[:16])
    thermal_val = (
        verdict.tier2.max_line_loading_pct
        if verdict.tier2 is not None
        else verdict.tier1.estimated_thermal_loading_pct
    )

    svg_parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}" role="img" aria-label="{headline}">',
        '<rect x="0" y="0" width="880" height="430" rx="10" fill="#ffffff" stroke="#cbd5e1" stroke-width="1.5"/>',
        # Header banner
        f'<rect x="20" y="16" width="840" height="60" rx="8" fill="{bg_badge}" stroke="{fg_badge}" stroke-width="1.2"/>',
        f'<text x="34" y="41" font-family="system-ui, -apple-system, sans-serif" font-size="15" font-weight="700" fill="{fg_badge}">{headline}</text>',
        (
            f'<text x="34" y="62" font-family="ui-monospace, SFMono-Regular, monospace" font-size="11.5" fill="#334155">'
            f'Regime: {regime_str} | Binding: {constraints_str} | N-1 SCR: {verdict.tier1.scr_n1:.2f} | Max Thermal: {thermal_val:.1f}%'
            f'</text>'
        ),
        # Plot frame
        f'<rect x="{x0:.1f}" y="{y0:.1f}" width="{pw:.1f}" height="{ph:.1f}" fill="#f8fafc" stroke="#e2e8f0" stroke-width="1"/>',
        # Statutory & Tier-1 reference lines
        f'<line x1="{x0:.1f}" y1="{y_vmax:.1f}" x2="{x0 + pw:.1f}" y2="{y_vmax:.1f}" stroke="#dc2626" stroke-width="1.5" stroke-dasharray="6,4"/>',
        f'<text x="{x0 + pw - 6:.1f}" y="{y_vmax - 5:.1f}" text-anchor="end" font-family="system-ui, sans-serif" font-size="10.5" font-weight="600" fill="#dc2626">Upper Statutory Limit ({v_max_lim:.2f} pu)</text>',
        f'<line x1="{x0:.1f}" y1="{y_strat:.1f}" x2="{x0 + pw:.1f}" y2="{y_strat:.1f}" stroke="#d97706" stroke-width="1.2" stroke-dasharray="4,4"/>',
        f'<text x="{x0 + 6:.1f}" y="{y_strat - 5:.1f}" font-family="system-ui, sans-serif" font-size="10" fill="#b45309">Tier-1 Stratified Gate ({strat_lim:.4f} pu)</text>',
        f'<line x1="{x0:.1f}" y1="{y_nom:.1f}" x2="{x0 + pw:.1f}" y2="{y_nom:.1f}" stroke="#94a3b8" stroke-width="1" stroke-dasharray="2,3"/>',
        f'<line x1="{x0:.1f}" y1="{y_vmin:.1f}" x2="{x0 + pw:.1f}" y2="{y_vmin:.1f}" stroke="#dc2626" stroke-width="1.5" stroke-dasharray="6,4"/>',
        f'<text x="{x0 + pw - 6:.1f}" y="{y_vmin + 13:.1f}" text-anchor="end" font-family="system-ui, sans-serif" font-size="10.5" font-weight="600" fill="#dc2626">Lower Statutory Limit ({v_min_lim:.2f} pu)</text>',
    ]

    # Y-axis ticks
    for tick_idx in range(5):
        v_tick = v_bottom + (tick_idx / 4.0) * (v_top - v_bottom)
        yt = _y_pos(v_tick, y0, ph, v_bottom, v_top)
        svg_parts.append(
            f'<text x="{x0 - 8:.1f}" y="{yt + 4:.1f}" text-anchor="end" font-family="ui-monospace, monospace" font-size="10.5" fill="#475569">{v_tick:.3f}</text>'
        )

    if pts:
        vmax_poly = _polyline_points([p.v_max_pu for p in pts], x0, pw, y0, ph, v_bottom, v_top)
        vmin_poly = _polyline_points([p.v_min_pu for p in pts], x0, pw, y0, ph, v_bottom, v_top)
        svg_parts.append(
            f'<polyline fill="none" stroke="#2563eb" stroke-width="2.4" points="{vmax_poly}"/>'
        )
        if any(abs(p.v_max_pu - p.v_min_pu) > 1e-4 for p in pts):
            svg_parts.append(
                f'<polyline fill="none" stroke="#0891b2" stroke-width="1.8" stroke-dasharray="4,2" points="{vmin_poly}"/>'
            )
        if compliant_mit and compliant_mit.resulting_peak_v_pu is not None:
            y_mit = _y_pos(compliant_mit.resulting_peak_v_pu, y0, ph, v_bottom, v_top)
            mit_label = html.escape(f"Mitigated ({compliant_mit.option_id}: {compliant_mit.resulting_peak_v_pu:.4f} pu)")
            svg_parts.append(
                f'<line x1="{x0:.1f}" y1="{y_mit:.1f}" x2="{x0 + pw:.1f}" y2="{y_mit:.1f}" stroke="#059669" stroke-width="1.8" stroke-dasharray="7,3"/>'
            )
            svg_parts.append(
                f'<text x="{x0 + 6:.1f}" y="{y_mit + 13:.1f}" font-family="system-ui, sans-serif" font-size="10.5" font-weight="600" fill="#059669">{mit_label}</text>'
            )
        scope_caption = html.escape(
            f"Tier-2 AC Trajectory ({len(pts)} solved states; peak={verdict.tier2.peak_voltage_pu:.4f} pu, min={verdict.tier2.min_voltage_pu:.4f} pu)"
            if verdict.tier2 is not None
            else ""
        )
        svg_parts.append(
            f'<text x="{x0 + pw / 2:.1f}" y="{y0 + ph + 24:.1f}" text-anchor="middle" font-family="system-ui, sans-serif" font-size="11.5" fill="#334155">{scope_caption}</text>'
        )
    else:
        # Tier-1 Analytical Fast-Screen / Regulatory Override marker
        y_t1_hi = _y_pos(verdict.tier1.predicted_peak_v_pu, y0, ph, v_bottom, v_top)
        y_t1_lo = _y_pos(verdict.tier1.predicted_min_v_pu, y0, ph, v_bottom, v_top)
        cx = x0 + pw / 2.0
        svg_parts.append(
            f'<line x1="{cx:.1f}" y1="{y_t1_hi:.1f}" x2="{cx:.1f}" y2="{y_t1_lo:.1f}" stroke="#2563eb" stroke-width="4"/>'
        )
        svg_parts.append(f'<circle cx="{cx:.1f}" cy="{y_t1_hi:.1f}" r="6" fill="#2563eb"/>')
        svg_parts.append(f'<circle cx="{cx:.1f}" cy="{y_t1_lo:.1f}" r="6" fill="#0891b2"/>')
        note = (
            f"Tier-2 AC Bypassed ({verdict.regulatory_override}; N-1 SCR={verdict.tier1.scr_n1:.2f} < 2.0)"
            if verdict.regulatory_override
            else f"Tier-1 Analytical Fast-Track Clear (V_peak={verdict.tier1.predicted_peak_v_pu:.4f} pu, V_min={verdict.tier1.predicted_min_v_pu:.4f} pu; {verdict.tier1.execution_ms:.2f} ms)"
        )
        svg_parts.append(
            f'<text x="{cx:.1f}" y="{y0 + ph + 24:.1f}" text-anchor="middle" font-family="system-ui, sans-serif" font-size="11.5" fill="#334155">{html.escape(note)}</text>'
        )

    # Provenance footer
    svg_parts.append(
        f'<text x="24" y="408" font-family="ui-monospace, SFMono-Regular, monospace" font-size="10.5" fill="#64748b">'
        f'Audit Receipt: {html.escape(verdict.audit_receipt.receipt_id)} | Feeder SHA-256: {feeder_short}... | Reproducibility SHA-256: {repro_short}...'
        f'</text>'
    )
    svg_parts.append("</svg>")
    return "\n".join(svg_parts)

