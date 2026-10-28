"""Analysis report and result export.

The HTML report is self-contained (images embedded as base64) and can be
printed to PDF from the GUI. Result export writes VTU (ParaView), CSV and JSON.
"""
from __future__ import annotations

import base64
import html
import json
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np

from opencae import __version__
from opencae.materials.library import EXAMPLE_WARNING, material_in_units
from opencae.postprocessing.results import QUANTITIES, equilibrium_check
from opencae.postprocessing.theory import cantilever_comparison

CSS = """
body{font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;max-width:960px;margin:24px auto;color:#1d2430;line-height:1.45;padding:0 16px}
h1{font-size:26px;margin-bottom:2px}h2{font-size:19px;border-bottom:2px solid #2b6cb0;padding-bottom:3px;margin-top:28px}
table{border-collapse:collapse;margin:8px 0 14px;width:100%}td,th{border:1px solid #cfd6e0;padding:5px 8px;text-align:left;font-size:13.5px}
th{background:#eef2f7}.num{text-align:right;font-variant-numeric:tabular-nums}.warn{background:#fff6e0;border-left:4px solid #d69e2e;padding:8px 12px;margin:10px 0}
.err{background:#fde8e8;border-left:4px solid #c53030;padding:8px 12px}.ok{color:#276749;font-weight:600}.muted{color:#5a6577;font-size:13px}
img{max-width:100%;border:1px solid #cfd6e0;margin:6px 0}
"""


def _img(path: Path) -> str:
    data = base64.b64encode(Path(path).read_bytes()).decode()
    return f'<img src="data:image/png;base64,{data}" alt="{html.escape(Path(path).stem)}"/>'


def _row(*cells, header=False) -> str:
    tag = "th" if header else "td"
    out = []
    for c in cells:
        cls = ' class="num"' if isinstance(c, (int, float)) and not header else ""
        txt = f"{c:.6g}" if isinstance(c, float) else html.escape(str(c))
        out.append(f"<{tag}{cls}>{txt}</{tag}>")
    return "<tr>" + "".join(out) + "</tr>"


def build_report(session, images: Optional[list[Path]] = None) -> str:
    p = session.project
    us = session.units
    body = session.analysis_body()
    rs = session.results
    parts = [f"<!doctype html><html><head><meta charset='utf-8'><title>{html.escape(p.name)} - analysis report"
             f"</title><style>{CSS}</style></head><body>"]
    parts.append(f"<h1>{html.escape(p.name)}</h1><div class='muted'>OpenCAE Studio {__version__} &middot; "
                 f"{datetime.now():%Y-%m-%d %H:%M} &middot; {html.escape(p.analysis.title)}</div>")
    if not p.results_are_current():
        parts.append("<div class='err'><b>Results are missing or out of date.</b> The model was changed after "
                     "the last solve; numbers below may not correspond to the current model.</div>")

    parts.append("<h2>1. Model</h2><table>")
    parts.append(_row("Unit system", f"{us.key}: length {us.length}, force {us.force}, stress {us.stress}, "
                                     f"density {us.density}"))
    if body:
        parts.append(_row("Analysis body", body.name))
        parts.append(_row("Volume", f"{body.volume:.6g} {us.length}^3"))
        bb = body.bbox
        parts.append(_row("Bounding box", f"{np.round(bb[0], 6).tolist()} to {np.round(bb[1], 6).tolist()} {us.length}"))
        parts.append(_row("Topology", f"{len(body.faces)} faces, {len(body.edges)} edges, {len(body.vertices)} vertices"))
    parts.append(_row("Analysis", "Linear static, small displacement, isotropic linear elastic"))
    parts.append(_row("Solver", p.results.solver_version or "-"))
    parts.append("</table>")

    mat = p.material_for_body(body.id) if body else None
    parts.append("<h2>2. Material</h2>")
    if mat:
        mu = material_in_units(mat, us)
        parts.append("<table>" + _row("Property", "Value", "Unit", header=True))
        parts.append(_row("Name", mat.name, ""))
        parts.append(_row("Young's modulus E", mu["E"], us.stress))
        parts.append(_row("Poisson's ratio", mu["nu"], "-"))
        if mu["rho"]:
            parts.append(_row("Density", mu["rho"], us.density))
        if mu["yield"]:
            parts.append(_row("Yield strength", mu["yield"], us.stress))
        parts.append("</table>")
        if mat.is_example:
            parts.append(f"<div class='warn'>{html.escape(EXAMPLE_WARNING)}</div>")

    parts.append("<h2>3. Mesh</h2>")
    if session.mesh is not None:
        st = session.mesh.stats()
        parts.append("<table>")
        from opencae.meshing.elements import VARIANT_INFO
        ccx_el = p.mesh_settings.solver_element(st["element_type"])
        parts.append(_row("Element type", f"{ccx_el}: {VARIANT_INFO.get(ccx_el, '')}"))
        if st.get("target_elements"):
            parts.append(_row("Requested / generated elements", f"{st['target_elements']} / {st['elements']}"))
        parts.append(_row("Nodes / elements", f"{st['nodes']} / {st['elements']}"))
        for k, label in (("edge_min_min", "Shortest edge"), ("edge_max_max", "Longest edge")):
            if k in st:
                parts.append(_row(label, f"{st[k]:.4g} {us.length}"))
        for k, label in (("gamma_min", "Min gamma (1 = regular)"), ("sj_min", "Min scaled Jacobian"),
                         ("aspect_ratio_max", "Max aspect ratio (1 = regular)")):
            if k in st:
                parts.append(_row(label, st[k]))
        parts.append(_row("Invalid elements", st.get("invalid_elements", 0)))
        s = p.mesh_settings
        parts.append(_row("Settings", f"max size {s.size_max or 'auto'}, min size {s.size_min or 'auto'}, "
                                      f"{len(s.refinements)} local refinement(s)"))
        parts.append("</table>")
    else:
        parts.append("<p>No mesh.</p>")

    parts.append("<h2>4. Boundary conditions and loads</h2><table>")
    parts.append(_row("Name", "Type", "Targets", "Parameters", header=True))
    for c in p.constraints:
        prm = ", ".join(f"{k}={v}" for k, v in c.params.items()) or "all DOF = 0"
        parts.append(_row(c.name, c.kind + (" (suppressed)" if c.suppressed else ""),
                          ", ".join(t.label() for t in c.targets), prm))
    for l in p.loads:
        prm = l.params.copy()
        unit = {"force": us.force, "pressure": us.stress, "gravity": us.acceleration}.get(l.kind, "")
        txt = f"magnitude {prm.get('magnitude')} {unit}"
        if "direction" in prm:
            txt += f", direction {prm['direction']}"
        parts.append(_row(l.name, l.kind + (" (suppressed)" if l.suppressed else ""),
                          ", ".join(t.label() for t in l.targets) or "whole body", txt))
    parts.append("</table>")

    parts.append("<h2>5. Results</h2>")
    if rs is not None:
        sm = rs.summary()
        parts.append("<table>" + _row("Quantity", "Value", "Unit", "Location / note", header=True))
        parts.append(_row("Max displacement magnitude", sm["max_displacement"], us.length,
                          f"node {sm['max_displacement_node']} at {np.round(sm['max_displacement_location'], 4).tolist()}"))
        for c in "xyz":
            parts.append(_row(f"Max |U{c}|", sm[f"max_abs_u{c}"], us.length, "nodal"))
        if "max_von_mises_nodal" in sm:
            parts.append(_row("Max von Mises (nodal, extrapolated & averaged)", sm["max_von_mises_nodal"], us.stress,
                              f"node {sm['max_von_mises_node']}"))
        if "max_von_mises_integration_point" in sm:
            parts.append(_row("Max von Mises (raw integration points)", sm["max_von_mises_integration_point"],
                              us.stress, "from CalculiX .dat"))
        if "max_principal" in sm:
            parts.append(_row("Max / min principal stress", f"{sm['max_principal']:.6g} / {sm['min_principal']:.6g}",
                              us.stress, "nodal-averaged"))
        parts.append("</table>")
        if mat and mat.yield_strength and "max_von_mises_nodal" in sm:
            y = material_in_units(mat, us)["yield"]
            fos = y / max(sm["max_von_mises_nodal"], 1e-300)
            parts.append(f"<p>Yield safety factor (yield / max nodal von Mises): <b>{fos:.3g}</b>. "
                         "<span class='muted'>Peak stresses at re-entrant corners or clamped edges may be "
                         "singularities; check convergence before using this value.</span></p>")
        eq = equilibrium_check({**sm, **p.results.summary})
        if eq:
            parts.append("<h3>Equilibrium check</h3><table>" + _row("Item", "Fx", "Fy", "Fz", header=True))
            for k, v in sm.get("reaction_totals", {}).items():
                label = ("Total reaction, all supports (each node once)" if k == "BC_ALL_SUPPORTS"
                         else f"Reaction {k} (sets may share edge nodes)")
                parts.append(_row(label, *[float(x) for x in v]))
            parts.append(_row("Applied external load", *[float(x) for x in eq["applied"]]))
            if np.abs(eq["on_constrained"]).max() > 0:
                parts.append(_row("of which acting directly on constrained DOFs", *[float(x) for x in eq["on_constrained"]]))
            parts.append(_row("Residual", *[float(x) for x in eq["residual"]]))
            parts.append("</table>")
            parts.append(f"<p class='{'ok' if eq['ok'] else 'err'}'>Global equilibrium "
                         f"{'satisfied' if eq['ok'] else 'NOT satisfied'} (relative residual "
                         f"{eq['relative']:.2e}).</p>")
        comp = cantilever_comparison(session)
        if comp:
            parts.append(f"<h3>Comparison with theory: {html.escape(comp.case)}</h3><table>")
            parts.append(_row("Quantity", "Theory", "FE", "Unit", "Difference", header=True))
            for q in comp.quantities:
                parts.append(_row(q["name"], q["theory"], q["fe"], q["unit"], f"{q['diff_pct']:+.2f} %"))
            parts.append("</table><p><b>Assumptions and differences</b></p><ul>")
            parts.extend(f"<li>{html.escape(a)}</li>" for a in comp.assumptions)
            parts.append("</ul>")
    else:
        parts.append("<p>No results available.</p>")

    if images:
        parts.append("<h2>6. Figures</h2>")
        for im in images:
            parts.append(f"<p class='muted'>{html.escape(Path(im).stem.replace('_', ' '))}</p>{_img(im)}")

    parts.append("<h2>Limitations</h2><ul>"
                 "<li>Linear static analysis: small strains and rotations, no contact, no plasticity, "
                 "no buckling check.</li>"
                 "<li>Nodal stresses are extrapolated from integration points and averaged by CalculiX; "
                 "they smooth discontinuities. Raw integration-point maxima are reported separately.</li>"
                 "<li>Results depend on mesh density; perform a convergence study for design decisions.</li>"
                 "<li>Material values marked as examples must be verified.</li></ul>")
    parts.append("</body></html>")
    return "\n".join(parts)


def write_report(session, path: Path, images: Optional[list[Path]] = None) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(build_report(session, images), encoding="utf-8")
    session.last_report = path
    return path


def export_results(session, path: Path) -> Path:
    """Export results: .vtu (all fields, for ParaView), .csv (nodal table) or .json (summary)."""
    rs = session.results
    if rs is None:
        raise ValueError("No results to export.")
    path = Path(path)
    suf = path.suffix.lower()
    if suf == ".json":
        path.write_text(json.dumps({"units": session.project.unit_system, "summary": rs.summary(),
                                    "solver": session.project.results.solver_version}, indent=2))
    elif suf == ".csv":
        keys = rs.available()
        cols = np.column_stack([np.arange(1, rs.mesh.n_nodes + 1), rs.mesh.nodes] + [rs.quantity(k) for k in keys])
        hdr = "node,x,y,z," + ",".join(keys)
        np.savetxt(path, cols, delimiter=",", header=hdr, comments="", fmt="%.10g")
    elif suf in (".vtu", ".vtk"):
        import meshio
        m = rs.mesh.to_meshio()
        m.point_data = {k: rs.quantity(k) for k in rs.available()}
        m.point_data["displacement"] = rs.displacement
        m.write(path)
    else:
        raise ValueError(f"Unsupported results export format '{suf}' (use .vtu, .csv or .json).")
    return path
