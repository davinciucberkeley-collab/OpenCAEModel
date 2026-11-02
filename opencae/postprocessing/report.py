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
from opencae.postprocessing.results import equilibrium_check
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


LOAD_UNITS = {"heat_flux": "W/m²", "heat_power": "W", "heat_generation": "W/m³"}
LIMITATIONS = {
    "structural": ["Linear elastic, isotropic material; no plasticity or damage. Bodies that touch are bonded "
                   "unless they are meshed separately and connected by interactions (tie or penalty contact; "
                   "contact pressure depends on the penalty stiffness and mesh).", "Small strains and rotations unless large deflection (NLGEOM) was enabled.",
                   "Nodal stresses are extrapolated from integration points and averaged by CalculiX; they smooth "
                   "discontinuities. Raw integration-point maxima are reported separately.",
                   "Peak stresses at re-entrant corners, point loads or clamped edges can be mesh-dependent "
                   "singularities; check mesh convergence before using them for design decisions."],
    "thermal": ["Temperature-independent conductivity and specific heat.",
                "Faces without a condition are perfectly insulated.",
                "Radiation exchanges with surroundings at the ambient temperature only (no view factors between "
                "model surfaces).",
                "Transient analyses use implicit time stepping with a fixed increment; halve the step to check "
                "the time-discretisation error."],
    "cfd": ["Steady, incompressible, isothermal Newtonian flow with constant properties; flow quantities in SI "
            "units.", "RANS turbulence models give mean-flow estimates; wall shear and separation need wall-resolved "
            "meshes and a mesh-convergence check.", "Uniform inlet profiles: delivered flow can be below the "
            "nominal value on coarse meshes (reported).", "Results are only meaningful when the solver converged "
            "and mass is conserved (both reported above)."],
    "buckling": ["Linear (eigenvalue) buckling of the perfect geometry; imperfections, plasticity and large "
                 "pre-buckling deformation lower the real collapse load.",
                 "Use the lowest positive load factor with an appropriate knock-down factor."],
    "modal": ["Linear, undamped free vibration about the unloaded state (no pre-stress stiffening).",
              "Mode-shape amplitudes are mass-normalised and have no physical magnitude.",
              "Supports are perfectly rigid; real joints are softer and lower the frequencies."],
}


def _physics(session):
    from opencae.physics import physics_for
    try:
        return physics_for(session.project.analysis.analysis_type)
    except KeyError:
        return None


def build_report(session, images: Optional[list[Path]] = None, notes: str = "") -> str:
    from opencae.postprocessing.summary import summary_rows, unit_for
    from opencae.project.model import ANALYSIS_TYPES
    p = session.project
    st = p.study
    us = session.units
    bodies = session.analysis_bodies()
    rs = session.results
    a = p.analysis
    kind = getattr(rs, "kind", None) or ("thermal" if a.analysis_type.startswith("thermal_") and
                                         a.analysis_type != "thermal_stress" else
                                         a.analysis_type if a.analysis_type in ("modal", "buckling") else
                                         "cfd" if a.analysis_type.startswith("cfd") else "structural")
    parts = [f"<!doctype html><html><head><meta charset='utf-8'><title>{html.escape(p.name)} - {html.escape(st.name)}"
             f"</title><style>{CSS}</style></head><body>"]
    parts.append(f"<h1>{html.escape(p.name)}</h1><div class='muted'>Study <b>{html.escape(st.name)}</b> &middot; "
                 f"{html.escape(ANALYSIS_TYPES.get(a.analysis_type, a.analysis_type))} &middot; OpenCAE Studio "
                 f"{__version__} &middot; {datetime.now():%Y-%m-%d %H:%M}</div>")
    if p.description:
        parts.append(f"<p>{html.escape(p.description)}</p>")
    if not p.results_are_current():
        parts.append("<div class='err'><b>Results are missing or out of date.</b> The model was changed after "
                     "the last solve; numbers below may not correspond to the current model.</div>")

    parts.append("<h2>1. Model</h2><table>")
    parts.append(_row("Unit system", f"{us.key}: length {us.length}, force {us.force}, stress {us.stress}, "
                                     f"density {us.density}; temperatures in °C, thermal inputs in SI"))
    parts.append(_row("Analysis", ANALYSIS_TYPES.get(a.analysis_type, a.analysis_type)))
    parts.append(_row("Solver", p.results.solver_version or "-"))
    parts.append("</table><table>" + _row("Body", "Material", f"Volume [{us.length}³]", "Bounding box", header=True))
    for b in bodies:
        mat = p.material_for_body(b.id)
        parts.append(_row(b.name, mat.name if mat else "(none)", b.volume,
                          f"{np.round(b.bbox[0], 4).tolist()} – {np.round(b.bbox[1], 4).tolist()}"))
    parts.append("</table>")
    if len(bodies) > 1:
        parts.append("<p class='muted'>Bodies are meshed together; where they touch they share nodes (bonded "
                     "connection, no sliding or separation).</p>")

    ph = _physics(session)
    if ph is not None:
        parts.append("<h2>2. Governing equations and assumptions</h2><table>")
        for name, eq in ph.equations:
            parts.append(_row(name, eq))
        parts.append("</table><ul>" + "".join(f"<li>{html.escape(x)}</li>" for x in ph.assumptions) + "</ul>")
        settings = []
        if a.analysis_type == "modal":
            settings.append(("Modes requested", a.n_modes))
            if a.freq_min:
                settings.append(("Lowest frequency of interest", f"{a.freq_min} Hz"))
        if a.analysis_type == "thermal_transient":
            settings += [("End time", f"{a.time_total} s"), ("Time step", f"{a.time_step} s"),
                         ("Initial temperature", f"{a.initial_temperature} °C")]
        if a.analysis_type == "thermal_stress":
            src = p.study_by_id(a.temperature_source_study)
            settings += [("Temperature source", src.name if src else "(uniform temperature load)"),
                         ("Stress-free temperature", f"{a.reference_temperature} °C")]
        if a.nonlinear_geometry and a.analysis_type in ("linear_static", "thermal_stress"):
            settings.append(("Large deflection", "NLGEOM on"))
        if settings:
            parts.append("<table>" + "".join(_row(k, v) for k, v in settings) + "</table>")

    parts.append("<h2>3. Materials</h2>")
    seen = set()
    for b in bodies:
        mat = p.material_for_body(b.id)
        if mat is None or mat.id in seen:
            continue
        seen.add(mat.id)
        mu = material_in_units(mat, us)
        parts.append(f"<h3>{html.escape(mat.name)}</h3><table>" + _row("Property", "Value", "Unit", header=True))
        parts.append(_row("Young's modulus E", mu["E"], us.stress))
        parts.append(_row("Poisson's ratio", mu["nu"], "-"))
        if mu["rho"]:
            parts.append(_row("Density", mu["rho"], us.density))
        if mu["yield"]:
            parts.append(_row("Yield strength", mu["yield"], us.stress))
        if mat.thermal_expansion:
            parts.append(_row("Thermal expansion", mat.thermal_expansion, "1/K"))
        if mat.thermal_conductivity:
            parts.append(_row("Thermal conductivity", mat.thermal_conductivity, "W/(m·K)"))
        if mat.specific_heat:
            parts.append(_row("Specific heat", mat.specific_heat, "J/(kg·K)"))
        parts.append("</table>")
        if mat.is_example:
            parts.append(f"<div class='warn'>{html.escape(EXAMPLE_WARNING)}</div>")

    parts.append("<h2>4. Mesh</h2>")
    if session.mesh is not None:
        stt = session.mesh.stats()
        parts.append("<table>")
        from opencae.meshing.elements import VARIANT_INFO
        ccx_el = p.mesh_settings.solver_element(stt["element_type"])
        parts.append(_row("Element type", f"{ccx_el}: {VARIANT_INFO.get(ccx_el, '')}"))
        if stt.get("target_elements"):
            parts.append(_row("Requested / generated elements", f"{stt['target_elements']} / {stt['elements']}"))
        parts.append(_row("Nodes / elements", f"{stt['nodes']} / {stt['elements']}"))
        for k, label in (("edge_min_min", "Shortest edge"), ("edge_max_max", "Longest edge")):
            if k in stt:
                parts.append(_row(label, f"{stt[k]:.4g} {us.length}"))
        for k, label in (("gamma_min", "Min gamma (1 = regular)"), ("sicn_min", "Min SICN (1 = regular)"),
                         ("sj_min", "Min scaled Jacobian"), ("aspect_ratio_max", "Max aspect ratio (1 = regular)")):
            if k in stt:
                parts.append(_row(label, stt[k]))
        parts.append(_row("Invalid elements", stt.get("invalid_elements", 0)))
        ms = p.mesh_settings
        parts.append(_row("Settings", f"max size {ms.size_max or 'auto'}, min size {ms.size_min or 'auto'}, "
                                      f"{len(ms.refinements)} local refinement(s), assembly mesh "
                                      f"{getattr(ms, 'assembly_mesh', 'conformal')}"))
        parts.append("</table>")
    else:
        parts.append("<p>No mesh.</p>")

    parts.append("<h2>5. Boundary conditions and loads</h2><table>")
    parts.append(_row("Name", "Type", "Targets", "Parameters", header=True))
    for c in p.constraints:
        prm = ", ".join(f"{k}={v}" for k, v in c.params.items()) or "all DOF = 0"
        if c.kind == "temperature":
            prm = f"T = {c.params.get('value')} °C"
        parts.append(_row(c.name, c.kind + (" (suppressed)" if c.suppressed else ""),
                          ", ".join(t.label() for t in c.targets), prm))
    for ld in p.loads:
        prm = ld.params
        if ld.kind == "convection":
            txt = f"h = {prm.get('film')} W/(m²·K), T∞ = {prm.get('ambient')} °C"
        elif ld.kind == "radiation":
            txt = f"ε = {prm.get('emissivity')}, T∞ = {prm.get('ambient')} °C"
        elif ld.kind == "uniform_temperature":
            txt = f"T = {prm.get('value')} °C"
        else:
            unit = {"force": us.force, "pressure": us.stress, "gravity": us.acceleration}.get(ld.kind,
                                                                                          LOAD_UNITS.get(ld.kind, ""))
            txt = f"magnitude {prm.get('magnitude')} {unit}"
            if "direction" in prm:
                txt += f", direction {prm['direction']}"
        parts.append(_row(ld.name, ld.kind + (" (suppressed)" if ld.suppressed else ""),
                          ", ".join(t.label() for t in ld.targets) or "whole study", txt))
    for it in getattr(p, "interactions", []) or []:
        prm = "tied (*TIE)" if it.kind == "bonded" else (
            f"penalty contact, friction {it.params.get('friction', 0.0)}, stiffness "
            f"{it.params.get('stiffness') or 'automatic (10 E/h)'}")
        parts.append(_row(it.name, f"interaction: {it.kind}" + (" (suppressed)" if it.suppressed else ""),
                          "master " + ", ".join(r.label() for r in it.master) + "; slave " +
                          ", ".join(r.label() for r in it.slave), prm))
    parts.append("</table>")

    parts.append("<h2>6. Results</h2>")
    if rs is not None:
        rows, checks = summary_rows(session)
        parts.append("<table>" + _row("Quantity", "Value", "Location / note", header=True))
        parts += [_row(x, y, z) for x, y, z in rows]
        parts.append("</table>")
        for name, ok, detail in checks:
            parts.append(f"<p class='{'ok' if ok else 'err'}'>{html.escape(name)}: "
                         f"{'satisfied' if ok else 'NOT satisfied'} — {html.escape(detail)}</p>")
        if kind == "modal" and getattr(rs, "effective_mass", None) is not None:
            fr = rs.mass_fractions()
            parts.append("<h3>Effective modal mass fractions</h3><table>" +
                         _row("Mode", "f [Hz]", "X", "Y", "Z", "RX", "RY", "RZ", header=True))
            for i, f in enumerate(rs.frequencies):
                parts.append(_row(i + 1, float(f), *[f"{100 * v:.2f} %" for v in np.nan_to_num(fr[i])]))
            parts.append("</table>")
        if kind == "structural":
            sm = rs.summary()
            eq = equilibrium_check({**sm, **p.results.summary})
            if eq:
                parts.append("<h3>Equilibrium check</h3><table>" + _row("Item", "Fx", "Fy", "Fz", header=True))
                for k, v in sm.get("reaction_totals", {}).items():
                    label = ("Total reaction, all supports (each node once)" if k == "BC_ALL_SUPPORTS"
                             else f"Reaction {k} (sets may share edge nodes)")
                    parts.append(_row(label, *[float(x) for x in v]))
                parts.append(_row("Applied external load", *[float(x) for x in eq["applied"]]))
                if np.abs(eq["on_constrained"]).max() > 0:
                    parts.append(_row("of which acting directly on constrained DOFs",
                                      *[float(x) for x in eq["on_constrained"]]))
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
                parts.extend(f"<li>{html.escape(x)}</li>" for x in comp.assumptions)
                parts.append("</ul>")
    else:
        parts.append("<p>No results available.</p>")

    if images:
        parts.append("<h2>7. Figures</h2>")
        for im in images:
            parts.append(f"<p class='muted'>{html.escape(Path(im).stem.replace('_', ' '))}</p>{_img(im)}")

    user_notes = "\n\n".join(x for x in (st.notes, notes) if x and x.strip())
    if user_notes:
        parts.append("<h2>Engineering notes</h2>" + "".join(f"<p>{html.escape(par)}</p>"
                                                            for par in user_notes.split("\n\n")))
    if len(p.studies) > 1:
        from opencae.project.model import ANALYSIS_TYPES as AT
        parts.append("<h2>Other studies in this project</h2><table>" +
                     _row("Study", "Type", "Solved", "Status", header=True))
        for o in p.studies:
            if o.id == st.id:
                continue
            status = ("current" if p.results_are_current(o) else "out of date") if o.results.completed else "not solved"
            parts.append(_row(o.name, AT.get(o.analysis.analysis_type, "?"), o.results.finished_at or "-", status))
        parts.append("</table>")
    parts.append("<h2>Limitations</h2><ul>" + "".join(f"<li>{html.escape(x)}</li>" for x in LIMITATIONS[kind]) +
                 "<li>Results depend on mesh density; perform a convergence study for design decisions.</li>"
                 "<li>Material values marked as examples must be verified.</li></ul>")
    parts.append("</body></html>")
    return "\n".join(parts)


def write_report(session, path: Path, images: Optional[list[Path]] = None, notes: str = "") -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(build_report(session, images, notes), encoding="utf-8")
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
        m = rs.mesh.to_meshio()
        m.point_data = {k: rs.quantity(k) for k in rs.available()}
        if getattr(rs, "kind", "structural") == "modal":  # every mode shape as a vector field
            for i, f in enumerate(rs.frequencies):
                m.point_data[f"mode_{i + 1}_{f:.4g}Hz"] = rs.modes[i]
        elif getattr(rs, "kind", "structural") == "thermal":
            for i, t in enumerate(rs.times):
                m.point_data[f"T_t{t:.6g}s"] = rs.temperature[i]
        else:
            m.point_data["displacement"] = rs.displacement
        m.write(path)
    else:
        raise ValueError(f"Unsupported results export format '{suf}' (use .vtu, .csv or .json).")
    return path
