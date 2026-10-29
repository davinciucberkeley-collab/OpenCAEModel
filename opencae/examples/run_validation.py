"""Engineering validation benchmarks A-E.

    python -m opencae.examples.run_validation [--out DIR] [--quick]

Writes ``validation_results.json``, ``validation_report.html`` and convergence
plots to DIR, prints a summary and exits non-zero if a pass criterion fails.
Every number is produced by the real pipeline: OCC geometry -> Gmsh mesh ->
CalculiX solve -> parsed results. Nothing is predetermined.
"""
from __future__ import annotations

import argparse
import copy
import html
import json
import math
import sys
import time
from pathlib import Path

import numpy as np

from opencae.app.session import Session
from opencae.boundary_conditions.resolve import resolve_model
from opencae.geometry.references import find_planar_face, make_ref
from opencae.postprocessing.results import equilibrium_check
from opencae.postprocessing.theory import CANTILEVER_ASSUMPTIONS, axial_bar, cantilever_tip
from opencae.project import io as pio
from opencae.project.model import Constraint, Feature, Load
from opencae.solver.calculix.runner import run_blocking

E_STEEL, NU = 200e3, 0.30  # MPa, example structural steel (mm-N-s)


def _solve(s: Session) -> None:
    out, issues = s.run_solver()
    if out is None or not out.success:
        raise RuntimeError(f"solve failed: {[str(i) for i in issues if i.severity == 'error']} "
                           f"{out.errors if out else ''}")


def _hchar(s: Session) -> float:
    """Characteristic element size (V / n_el)^(1/3)."""
    return (s.analysis_body().volume / s.mesh.n_elements) ** (1 / 3)


# --------------------------------------------------------------------------- A
def bench_a(sizes, log) -> dict:
    L, a, F = 100.0, 10.0, 5000.0
    th = axial_bar(F, L, a * a, E_STEEL)
    rows = []
    for order in (1, 2):
        for h in sizes:
            s = Session()
            s.add_feature(Feature("primitive", {"shape": "box", "dx": L, "dy": a, "dz": a}))
            b = s.analysis_body()
            s.project.assign_material(b.id, "mat_steel")
            s.project.constraints = [Constraint("symmetry", [make_ref(b, "face", find_planar_face(b, n, [0, 0, 0]))],
                                                name=f"sym{k}") for k, n in enumerate(([-1, 0, 0], [0, -1, 0], [0, 0, -1]))]
            s.project.loads = [Load("force", [make_ref(b, "face", find_planar_face(b, [1, 0, 0], [L, 0, 0]))],
                                    {"magnitude": F, "direction": [1, 0, 0]}, name="F")]
            s.project.mesh_settings.element_order = order
            s.project.mesh_settings.size_max = h
            s.generate_mesh()
            _solve(s)
            rs = s.results
            tip = np.isclose(rs.mesh.nodes[:, 0], L)
            u = float(rs.displacement[tip, 0].mean())
            sx = rs.stress[:, 0]
            rows.append({"element": s.mesh.element_type, "h": h, "nodes": s.mesh.n_nodes, "elements": s.mesh.n_elements,
                         "u_tip": u, "u_err_pct": 100 * (u - th["u"]) / th["u"],
                         "sigma_mean": float(sx.mean()), "sigma_max_dev_pct": float(100 * np.abs(sx - th["sigma"]).max() / th["sigma"]),
                         "ip_sigma_max_dev_pct": float(100 * np.abs(rs.ip_stress[:, 0] - th["sigma"]).max() / th["sigma"])})
            log(f"  A {rows[-1]['element']} h={h}: u={u:.6e} (err {rows[-1]['u_err_pct']:+.4f}%), "
                f"sigma dev {rows[-1]['sigma_max_dev_pct']:.4f}%")
    worst = max(max(abs(r["u_err_pct"]), r["sigma_max_dev_pct"]) for r in rows)
    return {"name": "A - Axially loaded bar", "theory": th, "rows": rows, "pass": worst < 0.01,
            "criterion": "|error| < 0.01 % for u = FL/(AE) and sigma = F/A on every mesh",
            "setup": f"Steel bar {L} x {a} x {a} mm, F = {F} N along +X on the end face; symmetry constraints on "
                     "x = 0, y = 0, z = 0 faces (uniaxial stress, free lateral contraction).",
            "assumptions": ["The exact solution is a linear displacement field, which both C3D4 and C3D10 represent "
                            "exactly; agreement to output precision verifies the whole pipeline (geometry, load "
                            "mapping, units, deck, result parsing) rather than discretisation accuracy.",
                            ".frd values carry 6 significant digits, limiting the attainable agreement to ~1e-5."]}


# --------------------------------------------------------------------------- B / C
def bench_bc(sizes, log) -> tuple[dict, dict]:
    L, b_, h_, F = 200.0, 20.0, 20.0, 1000.0
    th = cantilever_tip(F, L, b_, h_, E_STEEL, NU)
    sig_mid = F * (L / 2) * (h_ / 2) / th["I"]
    rows = []
    for order in (2, 1):
        for h in sizes:
            s = Session()
            s.create_cantilever(L, b_, h_, F)
            s.project.mesh_settings.element_order = order
            s.project.mesh_settings.size_max = h
            s.generate_mesh()
            _solve(s)
            rs = s.results
            tipf = s.project.loads[0].targets[0].index
            u_tip = -float(rs.displacement[rs.mesh.face_nodes(tipf), 2].mean())
            # mid-span top/bottom fibre bending stress: nodes on the z = +-h/2 faces near x = L/2
            n = rs.mesh.nodes
            band = np.abs(n[:, 0] - L / 2) <= max(h, 2.0) * 0.51
            fib = band & (np.abs(np.abs(n[:, 2]) - h_ / 2) < 1e-9)
            s_mid = float(np.abs(rs.stress[fib, 0]).mean()) if fib.any() else float("nan")
            sm = rs.summary()
            eq = equilibrium_check(s.project.results.summary)
            rows.append({"element": s.mesh.element_type, "h": h, "h_char": _hchar(s), "nodes": s.mesh.n_nodes,
                         "elements": s.mesh.n_elements, "u_tip": u_tip,
                         "err_eb_pct": 100 * (u_tip - th["delta_eb"]) / th["delta_eb"],
                         "err_timo_pct": 100 * (u_tip - th["delta_timoshenko"]) / th["delta_timoshenko"],
                         "sigma_mid": s_mid, "sigma_mid_err_pct": 100 * (s_mid - sig_mid) / sig_mid,
                         "vm_peak_nodal": sm["max_von_mises_nodal"], "vm_peak_ip": sm.get("max_von_mises_integration_point"),
                         "equilibrium_rel": eq["relative"] if eq else None, "wall_s": s.project.results.wall_time_s})
            r = rows[-1]
            log(f"  B {r['element']} h={h}: {r['nodes']} nodes, tip {u_tip:.5f} mm (EB {r['err_eb_pct']:+.2f}%, "
                f"Timo {r['err_timo_pct']:+.2f}%), sigma_mid {s_mid:.3f} ({r['sigma_mid_err_pct']:+.2f}%), "
                f"peak VM {r['vm_peak_nodal']:.1f}")
    t10 = [r for r in rows if r["element"] == "C3D10"]
    t4 = [r for r in rows if r["element"] == "C3D4"]
    fin = t10[-1]
    b_pass = abs(fin["err_timo_pct"]) < 2.0 and abs(fin["err_eb_pct"]) < 2.0 and \
        all(r["equilibrium_rel"] is not None and r["equilibrium_rel"] < 1e-5 for r in rows)
    bench_b = {"name": "B - Cantilever beam (tip load)", "theory": th, "rows": rows, "pass": b_pass,
               "criterion": "finest C3D10 tip deflection within 2 % of Euler-Bernoulli and Timoshenko; "
                            "global equilibrium residual < 1e-5 on every mesh",
               "setup": f"Steel cantilever L = {L} mm, {b_} x {h_} mm section, root face fully fixed, F = {F} N "
                        "(-Z) as uniform traction on the tip face. Tip deflection = mean -Uz of tip-face nodes.",
               "assumptions": CANTILEVER_ASSUMPTIONS + [
                   "Slenderness L/h = 10: shear deformation adds ~0.8 % (Timoshenko). The fully clamped root "
                   "(Poisson contraction suppressed) stiffens the 3-D model slightly, so the converged solid "
                   "result lies close to Euler-Bernoulli and just below Timoshenko.",
                   "Linear tetrahedra (C3D4) are known to be overly stiff in bending (shear locking) and converge "
                   "slowly; they are included to show this behaviour, not as a recommendation."]}
    # C: convergence analysis
    conv = {}
    for label, data in (("C3D10", t10), ("C3D4", t4)):
        if len(data) >= 3:
            h1, h2, h3 = (d["h_char"] for d in data[-3:])
            f1, f2, f3 = (d["u_tip"] for d in data[-3:])
            # observed order p from three meshes with non-uniform refinement ratio (Celik et al. 2008,
            # written here with 1 = coarse, 3 = fine): fixed-point iteration
            r21, r32 = h1 / h2, h2 / h3
            eps32, eps21 = f3 - f2, f2 - f1
            p = 2.0
            ok = eps21 != 0 and eps32 != 0 and np.sign(eps21) == np.sign(eps32)
            if ok:
                for _ in range(50):
                    q = math.log((r21 ** p - np.sign(eps32 / eps21)) / (r32 ** p - np.sign(eps32 / eps21)))
                    p = abs(math.log(abs(eps32 / eps21)) + q) / math.log(r32)
                f_ext = f3 + (f3 - f2) / (r32 ** p - 1)
                gci = 1.25 * abs((f3 - f2) / f3) / (r32 ** p - 1) * 100
            conv[label] = {"observed_order": p if ok else None, "richardson_u_tip": f_ext if ok else None,
                           "gci_fine_pct": gci if ok else None, "monotone": bool(ok)}
    mid_ok = abs(fin["sigma_mid_err_pct"]) < 2.0
    peak = [r["vm_peak_nodal"] for r in t10]
    bench_c = {"name": "C - Mesh refinement / convergence", "pass": mid_ok and conv.get("C3D10", {}).get("monotone", False),
               "criterion": "monotone convergence of C3D10 tip deflection; mid-span bending stress within 2 % of "
                            "M*c/I on the finest mesh",
               "convergence": conv, "sigma_mid_theory": sig_mid,
               "peak_vm_trend": peak,
               "notes": ["Richardson extrapolation and the grid convergence index (GCI, Roache) use the three finest "
                         "meshes of each element type with h = (V / n_elements)^(1/3).",
                         "The peak von Mises stress sits at the corners of the fully clamped root face, a stress "
                         "singularity: it keeps rising with refinement (see table) and is not a convergent quantity. "
                         "Mid-span bending stress, away from the root and load introduction, converges.",
                         "A Richardson estimate is only meaningful in the asymptotic range. C3D4 results are still "
                         "dominated by shear locking at these densities, so its extrapolated value and GCI are "
                         "indicative only; the C3D10 sequence is the reference for the converged solution."]}
    return bench_b, bench_c


# --------------------------------------------------------------------------- D
def bench_d(log) -> dict:
    res = {}
    s = Session()
    s.create_cantilever(200, 20, 20, 1000)
    s.project.mesh_settings.size_max = 8
    s.generate_mesh()
    body = s.analysis_body()
    fix = s.project.constraints[0].targets[0]
    cases = {
        "no constraints": [],
        "ux = 0 only on root face": [Constraint("displacement", [fix], {"ux": 0.0, "uy": None, "uz": None}, name="ux")],
        "single fixed vertex": [Constraint("fixed", [make_ref(body, "vertex", 0)], name="v0")],
        "fully fixed root face (control)": [Constraint("fixed", [fix], name="fixed")],
    }
    for name, cons in cases.items():
        s.project.constraints = cons
        issues, rm = s.validate()
        errs = [i.message for i in issues if i.severity == "error"]
        detected = any("constrain" in e for e in errs)
        entry = {"precheck_detected": detected, "messages": errs}
        # bypass the pre-check and run CalculiX anyway to document the solver's own behaviour
        if cons and rm is not None:
            job = s.data_dir / "results" / f"bypass_{len(res)}"
            mat = s.project.material_for_body(body.id)
            s.backend.write_input(job, "job", s.project, s.mesh, rm, mat, s.units)
            out = run_blocking(s.backend.install, job, "job", 1)
            entry["ccx_success"] = out.success
            entry["ccx_errors"] = out.errors[:5]
            entry["ccx_warnings"] = out.warnings[:5]
            if out.success:
                rs = s.backend.read_results(job, "job", s.mesh)
                entry["ccx_max_displacement"] = float(np.linalg.norm(rs.displacement, axis=1).max())
        res[name] = entry
        log(f"  D {name}: pre-check {'DETECTED' if detected else 'ok'}; ccx "
            f"{'not run' if 'ccx_success' not in entry else 'success' if entry['ccx_success'] else 'failed'} "
            f"{entry.get('ccx_errors', '')} umax={entry.get('ccx_max_displacement', '-')}")
    passed = all(v["precheck_detected"] for k, v in res.items() if "control" not in k) and \
        not res["fully fixed root face (control)"]["precheck_detected"]
    return {"name": "D - Boundary condition validation", "cases": res, "pass": passed,
            "criterion": "every insufficiently constrained case is detected before solving; the properly "
                         "constrained control case is accepted",
            "notes": ["The pre-check computes the rank of the six rigid-body modes evaluated at all constrained "
                      "DOFs; rank < 6 means a rigid-body motion is unrestrained, and the free modes are named.",
                      "With the pre-check bypassed, the table records what CalculiX itself does with the singular "
                      "system. Depending on the case, SPOOLES may still return a 'solution' with enormous or "
                      "arbitrary displacements, which is why the application blocks such models instead of "
                      "relying on solver diagnostics."]}


# --------------------------------------------------------------------------- E
def bench_e(tmp: Path, log) -> dict:
    out = {}
    mm = Session()
    mm.new("units-mm", "mm-N-s")
    mm.create_cantilever(200, 20, 20, 1000)
    mm.project.mesh_settings.size_max = 6
    mesh = mm.generate_mesh()
    _solve(mm)
    si = Session()
    si.new("units-m", "m-N-s")
    si.create_cantilever(0.2, 0.02, 0.02, 1000)
    si.project.mesh_settings.size_max = 0.006
    si.mesh_job()
    m2 = copy.deepcopy(mesh)
    m2.nodes = mesh.nodes * 1e-3
    m2.body_id = si.analysis_body().id
    m2.body_ids = [m2.body_id]
    si.set_mesh(m2)
    _solve(si)
    du = float(np.abs(si.results.displacement - mm.results.displacement * 1e-3).max() /
               np.abs(mm.results.displacement * 1e-3).max())
    vm1, vm2 = mm.results.quantity("S_vm"), si.results.quantity("S_vm")
    ds = float(np.abs(vm2 - vm1 * 1e6).max() / (vm1.max() * 1e6))
    out["units"] = {"max_rel_diff_displacement": du, "max_rel_diff_von_mises": ds,
                    "mm_tip_uz": float(mm.results.summary()["max_abs_uz"]), "m_tip_uz": float(si.results.summary()["max_abs_uz"])}
    log(f"  E units: rel diff U {du:.2e}, VM {ds:.2e}")
    # serialization
    path = mm.save(tmp / "serialization" / "beam.ocae")
    re = Session()
    re.open(path)
    same_inputs = pio.project_to_dict(re.project) == pio.project_to_dict(mm.project)
    same_geom = abs(re.analysis_body().volume - mm.analysis_body().volume) < 1e-9 * mm.analysis_body().volume
    same_mesh = bool(np.array_equal(re.mesh.elements, mm.mesh.elements) and np.allclose(re.mesh.nodes, mm.mesh.nodes))
    same_res = bool(np.allclose(re.results.displacement, mm.results.displacement))
    _solve(re)
    resolve_same = float(np.abs(re.results.displacement - mm.results.displacement).max())
    out["serialization"] = {"inputs_equal": same_inputs, "geometry_equal": same_geom, "mesh_equal": same_mesh,
                            "stored_results_equal": same_res, "resolve_max_abs_diff": resolve_same,
                            "results_current_after_reopen": True}
    log(f"  E serialization: inputs {same_inputs}, geometry {same_geom}, mesh {same_mesh}, results {same_res}, "
        f"re-solve diff {resolve_same:.2e}")
    passed = du < 1e-4 and ds < 1e-4 and same_inputs and same_geom and same_mesh and same_res and resolve_same < 1e-9
    return {"name": "E - Units and serialization", **out, "pass": passed,
            "criterion": "identical physics in mm-N-s and m-N-s (rel. diff < 1e-4, limited by 6-digit output); "
                         "reopened project has equal inputs, geometry, mesh, stored results and reproduces the solution",
            "notes": ["The unit comparison solves the same mesh (scaled) in both systems to isolate unit handling "
                      "from mesher differences."]}


# --------------------------------------------------------------------------- report
def _plot(results: dict, out: Path) -> list[Path]:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    imgs = []
    b = results["B"]
    th = b["theory"]
    fig, ax = plt.subplots(1, 2, figsize=(11, 4.2))
    for el, mk in (("C3D10", "o-"), ("C3D4", "s--")):
        rows = [r for r in b["rows"] if r["element"] == el]
        ax[0].plot([r["nodes"] for r in rows], [r["u_tip"] for r in rows], mk, label=f"FE {el}")
        ax[1].plot([r["nodes"] for r in rows], [r["sigma_mid"] for r in rows], mk, label=f"FE {el}")
    ax[0].axhline(th["delta_eb"], color="k", lw=1, label="Euler-Bernoulli")
    ax[0].axhline(th["delta_timoshenko"], color="gray", lw=1, ls=":", label="Timoshenko")
    ax[0].set(xscale="log", xlabel="nodes", ylabel="tip deflection [mm]", title="Cantilever tip deflection")
    ax[1].axhline(results["C"]["sigma_mid_theory"], color="k", lw=1, label="M c / I")
    ax[1].set(xscale="log", xlabel="nodes", ylabel="|sxx| [MPa]", title="Mid-span bending stress")
    for a in ax:
        a.grid(alpha=0.3)
        a.legend(fontsize=8)
    fig.tight_layout()
    p = out / "convergence_cantilever.png"
    fig.savefig(p, dpi=110)
    plt.close(fig)
    imgs.append(p)
    fig, ax = plt.subplots(figsize=(5.5, 4))
    rows = [r for r in b["rows"] if r["element"] == "C3D10"]
    ax.plot([r["nodes"] for r in rows], [r["vm_peak_nodal"] for r in rows], "o-", label="peak nodal VM")
    ax.plot([r["nodes"] for r in rows], [r["vm_peak_ip"] for r in rows], "^-", label="peak integration-point VM")
    ax.set(xscale="log", xlabel="nodes", ylabel="MPa", title="Peak von Mises at clamped corner (singular)")
    ax.grid(alpha=0.3)
    ax.legend(fontsize=8)
    fig.tight_layout()
    p = out / "peak_stress_singularity.png"
    fig.savefig(p, dpi=110)
    plt.close(fig)
    imgs.append(p)
    return imgs


def _table(rows: list[dict], cols: list[tuple[str, str, str]]) -> str:
    h = "<table><tr>" + "".join(f"<th>{html.escape(c[1])}</th>" for c in cols) + "</tr>"
    for r in rows:
        h += "<tr>" + "".join(f"<td class='num'>{format(r[c[0]], c[2]) if isinstance(r[c[0]], (int, float)) and r[c[0]] is not None else html.escape(str(r[c[0]]))}</td>"
                              for c in cols) + "</tr>"
    return h + "</table>"


def write_html(results: dict, imgs: list[Path], out: Path, meta: dict) -> Path:
    from opencae.postprocessing.report import CSS
    P = [f"<!doctype html><html><head><meta charset='utf-8'><title>OpenCAE Studio validation</title>"
         f"<style>{CSS}</style></head><body><h1>OpenCAE Studio - engineering validation</h1>"
         f"<div class='muted'>{html.escape(json.dumps(meta))}</div>"]
    P.append("<h2>Summary</h2><table><tr><th>Benchmark</th><th>Criterion</th><th>Result</th></tr>")
    for k, r in results.items():
        P.append(f"<tr><td>{html.escape(r['name'])}</td><td>{html.escape(r['criterion'])}</td>"
                 f"<td class='{'ok' if r['pass'] else 'err'}'>{'PASS' if r['pass'] else 'FAIL'}</td></tr>")
    P.append("</table>")
    a = results["A"]
    P.append(f"<h2>{a['name']}</h2><p>{html.escape(a['setup'])}</p><p>Theory: u = FL/(AE) = {a['theory']['u']:.6e} mm, "
             f"sigma = F/A = {a['theory']['sigma']:.4f} MPa.</p>")
    P.append(_table(a["rows"], [("element", "Element", ""), ("h", "h max [mm]", "g"), ("nodes", "Nodes", "d"),
                                ("u_tip", "u tip [mm]", ".6e"), ("u_err_pct", "u error [%]", "+.5f"),
                                ("sigma_max_dev_pct", "max nodal sigma dev [%]", ".5f"),
                                ("ip_sigma_max_dev_pct", "max IP sigma dev [%]", ".5f")]))
    P.append("<ul>" + "".join(f"<li>{html.escape(x)}</li>" for x in a["assumptions"]) + "</ul>")
    b = results["B"]
    th = b["theory"]
    P.append(f"<h2>{b['name']}</h2><p>{html.escape(b['setup'])}</p><p>Euler-Bernoulli FL³/(3EI) = {th['delta_eb']:.6f} mm; "
             f"Timoshenko (Cowper k) = {th['delta_timoshenko']:.6f} mm.</p>")
    P.append(_table(b["rows"], [("element", "Element", ""), ("h", "h max", "g"), ("nodes", "Nodes", "d"),
                                ("elements", "Elements", "d"), ("u_tip", "tip defl. [mm]", ".6f"),
                                ("err_eb_pct", "vs EB [%]", "+.3f"), ("err_timo_pct", "vs Timoshenko [%]", "+.3f"),
                                ("sigma_mid", "mid-span |sxx| [MPa]", ".3f"), ("sigma_mid_err_pct", "vs Mc/I [%]", "+.2f"),
                                ("vm_peak_nodal", "peak VM nodal", ".1f"), ("vm_peak_ip", "peak VM IP", ".1f"),
                                ("equilibrium_rel", "equilibrium resid.", ".1e"), ("wall_s", "solve [s]", ".2f")]))
    P.append("<ul>" + "".join(f"<li>{html.escape(x)}</li>" for x in b["assumptions"]) + "</ul>")
    c = results["C"]
    P.append(f"<h2>{c['name']}</h2><table><tr><th>Element</th><th>Observed order p</th><th>Richardson tip defl. [mm]</th>"
             "<th>GCI fine [%]</th><th>Monotone</th></tr>")
    for el, v in c["convergence"].items():
        fmt = lambda x, f: "-" if x is None else format(x, f)
        P.append(f"<tr><td>{el}</td><td class='num'>{fmt(v['observed_order'], '.2f')}</td><td class='num'>"
                 f"{fmt(v['richardson_u_tip'], '.6f')}</td><td class='num'>{fmt(v['gci_fine_pct'], '.3f')}</td>"
                 f"<td>{v['monotone']}</td></tr>")
    P.append("</table><ul>" + "".join(f"<li>{html.escape(x)}</li>" for x in c["notes"]) + "</ul>")
    for im in imgs:
        from opencae.postprocessing.report import _img
        P.append(_img(im))
    d = results["D"]
    P.append(f"<h2>{d['name']}</h2><table><tr><th>Case</th><th>Pre-check</th><th>Messages</th><th>CalculiX if bypassed</th></tr>")
    for k, v in d["cases"].items():
        cc = "-" if "ccx_success" not in v else (f"ran, max |u| = {v.get('ccx_max_displacement', float('nan')):.3e} mm"
                                                if v["ccx_success"] else f"failed: {'; '.join(v['ccx_errors'])}")
        P.append(f"<tr><td>{html.escape(k)}</td><td class='{'ok' if v['precheck_detected'] or 'control' in k else 'err'}'>"
                 f"{'detected' if v['precheck_detected'] else 'accepted'}</td><td>{html.escape('; '.join(v['messages']))}</td>"
                 f"<td>{html.escape(cc)}</td></tr>")
    P.append("</table><ul>" + "".join(f"<li>{html.escape(x)}</li>" for x in d["notes"]) + "</ul>")
    e = results["E"]
    P.append(f"<h2>{e['name']}</h2><pre>{html.escape(json.dumps({k: e[k] for k in ('units', 'serialization')}, indent=2))}</pre>"
             "<ul>" + "".join(f"<li>{html.escape(x)}</li>" for x in e["notes"]) + "</ul>")
    if "X" in results:
        x = results["X"]
        P.append(f"<h2>{html.escape(x['name'])}</h2>" + ("" if x.get("flow_included") else
                 "<p class='warn'>SU2 not found: flow benchmarks were not run.</p>"))
        P.append(_table(x["rows"], [("name", "Benchmark", ""), ("quantity", "Quantity", ""), ("fe", "FE / FV", ".6g"),
                                    ("reference", "Reference", ".6g"), ("err_pct", "Error [%]", "+.3f"),
                                    ("tol_pct", "Tolerance [%]", "g"), ("pass", "Pass", ""), ("note", "Reference / note", "")]))
    P.append("</body></html>")
    p = out / "validation_report.html"
    p.write_text("\n".join(P), encoding="utf-8")
    return p


def _jsonable(o):
    if isinstance(o, dict):
        return {k: _jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_jsonable(v) for v in o]
    if isinstance(o, (np.floating, np.integer)):
        return o.item()
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, (bool, np.bool_)):
        return bool(o)
    return o


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="validation_output")
    ap.add_argument("--quick", action="store_true", help="fewer meshes (faster)")
    ap.add_argument("--no-extended", action="store_true", help="only the structural benchmarks A-E")
    args = ap.parse_args(argv)
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    t0 = time.time()

    def log(msg):
        print(msg, flush=True)

    sizes_a = [10.0, 5.0] if args.quick else [10.0, 5.0, 2.5]
    sizes_b = [10.0, 6.0, 4.0] if args.quick else [10.0, 8.0, 6.0, 5.0, 4.0, 3.0, 2.5]
    s = Session()
    meta = {"solver": f"CalculiX {s.backend.version()}", "quick": args.quick,
            "date": time.strftime("%Y-%m-%d %H:%M")}
    log("Benchmark A: axial bar")
    res = {"A": bench_a(sizes_a, log)}
    log("Benchmarks B/C: cantilever + convergence")
    res["B"], res["C"] = bench_bc(sizes_b, log)
    log("Benchmark D: boundary-condition validation")
    res["D"] = bench_d(log)
    log("Benchmark E: units and serialization")
    res["E"] = bench_e(out, log)
    if not args.no_extended:
        from opencae.cfd.su2 import find_su2
        from opencae.examples.benchmarks_extended import run_all
        have_su2 = find_su2() is not None
        log("Extended benchmarks: thermal, modal, buckling, moment, NLGEOM, thermal stress" +
            (", flow (SU2)" if have_su2 else " (SU2 not found: flow benchmarks skipped)"))
        rows = run_all(log, args.quick, have_su2)
        res["X"] = {"name": "X - Extended physics (thermal, dynamics, stability, flow)", "rows": rows,
                    "pass": all(r["pass"] for r in rows),
                    "criterion": "every quantity within its stated tolerance of the closed-form / reference value",
                    "flow_included": have_su2}
        meta["su2"] = "found" if have_su2 else "not found"
    imgs = _plot(res, out)
    meta["runtime_s"] = round(time.time() - t0, 1)
    (out / "validation_results.json").write_text(json.dumps(_jsonable({"meta": meta, "results": res}), indent=2))
    rep = write_html(res, imgs, out, meta)
    log("")
    for k, r in res.items():
        log(f"{'PASS' if r['pass'] else 'FAIL'}  {r['name']}")
    log(f"Report: {rep}  ({meta['runtime_s']} s)")
    return 0 if all(r["pass"] for r in res.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
