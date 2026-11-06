"""Mesh convergence study.

The active study is copied, meshed and solved at a sequence of element sizes; the
monitored quantities are recorded against the number of degrees of freedom. The
user's study, its mesh and its results are not modified (the copy is removed
afterwards).

Convergence assessment (Roache's grid convergence index, GCI) for the three finest
levels (fine 1, medium 2, coarse 3):

    r      = (N1 / N2)^(1/3)                           (N: degrees of freedom; 1 = finest)
    p      = observed order from (q3 - q2) / (q2 - q1)  (Celik et al. 2008, unequal ratios allowed)
    q_ext  = (r^p q1 - q2) / (r^p - 1)                 (Richardson extrapolation)
    GCI    = 1.25 |(q1 - q2) / q1| / (r^p - 1)

A quantity whose successive changes do not shrink (e.g. a peak stress at a sharp
re-entrant corner or a point support) is reported as *not converging* - typically a
stress singularity, not a numerical problem to be fixed by more elements.

Worker usage: ``python -m opencae.postprocessing.convergence job.json``.
"""
from __future__ import annotations

import json
import math
import shutil
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import Callable, Optional

import numpy as np


def monitored(session) -> dict[str, float]:
    """Scalar results to follow for the active study's analysis type."""
    rs = session.results
    sm = rs.summary()
    kind = getattr(rs, "kind", "structural")
    if kind == "thermal":
        return {"Max temperature [°C]": sm["max_temperature"], "Min temperature [°C]": sm["min_temperature"]}
    if kind == "modal":
        return {f"f{i + 1} [Hz]": float(f) for i, f in enumerate(sm["frequencies_hz"][:3])}
    if kind == "buckling":
        return {f"Load factor {i + 1}": float(f) for i, f in enumerate(sm["load_factors"][:2])}
    if kind == "cfd":
        out = {"Max velocity [m/s]": sm["max_velocity"]}
        if "pressure_drop_Pa" in sm:
            out["Pressure drop [Pa]"] = sm["pressure_drop_Pa"]
        if "wall_force_N" in sm:
            out["Wall force |F| [N]"] = float(np.linalg.norm(sm["wall_force_N"]))
        return out
    out = {f"Max displacement [{session.units.length}]": sm["max_displacement"]}
    if "max_von_mises_nodal" in sm:
        out[f"Max von Mises, nodal [{session.units.stress}]"] = sm["max_von_mises_nodal"]
    return out


def assess(dofs, values) -> dict:
    """Observed order, Richardson estimate and GCI from the three finest levels.

    The refinement ratio of unstructured meshes is taken from the number of degrees of
    freedom, r = (N_fine / N_coarse)^(1/3), and the order is solved with the fixed-point
    iteration of Celik et al. (J. Fluids Eng. 130, 2008) that allows unequal ratios."""
    n = np.asarray(dofs, float)
    q = np.asarray(values, float)
    order = np.argsort(-n)  # finest (most DOFs) first
    n, q = n[order], q[order]
    out = {"relative_change_last": None, "converging": None, "order": None, "extrapolated": None, "gci": None}
    if len(q) >= 2:
        out["relative_change_last"] = float(abs(q[0] - q[1]) / max(abs(q[0]), 1e-300))
    if len(q) < 3:
        return out
    e21, e32 = q[1] - q[0], q[2] - q[1]
    r21, r32 = (n[0] / n[1]) ** (1 / 3), (n[1] / n[2]) ** (1 / 3)
    if abs(e21) < 1e-12 * max(abs(q[0]), 1e-300):
        out.update(converging=True, extrapolated=float(q[0]), gci=0.0)
        return out
    if e32 == 0 or e32 / e21 <= 0 or abs(e21) >= abs(e32) or r21 <= 1.0 or r32 <= 1.0:
        out["converging"] = False  # oscillating or not shrinking: no asymptotic convergence
        return out
    sgn = 1.0
    p = abs(math.log(abs(e32 / e21))) / math.log(r21)
    for _ in range(100):
        qp = math.log((r21 ** p - sgn) / (r32 ** p - sgn))
        p_new = abs(math.log(abs(e32 / e21)) + qp) / math.log(r21)
        if abs(p_new - p) < 1e-10:
            p = p_new
            break
        p = p_new
    rp = r21 ** p
    out.update(converging=True, order=float(p), extrapolated=float((rp * q[0] - q[1]) / (rp - 1)),
               gci=float(1.25 * abs((q[0] - q[1]) / q[0]) / (rp - 1)))
    return out


def run(session, sizes: list[float], progress: Optional[Callable[[str], None]] = None) -> dict:
    """Mesh and solve a copy of the active study at each size (largest first)."""
    say = progress or (lambda m: None)
    sizes = sorted({float(x) for x in sizes if x > 0}, reverse=True)
    if len(sizes) < 2:
        raise ValueError("give at least two different element sizes")
    p = session.project
    original = p.active_study
    dup = session.duplicate_study(original, "(mesh convergence)", activate=True)
    dup.mesh_settings.sizing_mode = "size"
    levels = []
    try:
        for k, h in enumerate(sizes):
            say(f"level {k + 1}/{len(sizes)}: element size {h:g}")
            dup.mesh_settings.size_max = h
            t0 = time.time()
            mesh = session.generate_mesh()
            out, issues = session.run_solver()
            if out is None or not out.success:
                errs = [str(i) for i in issues if i.severity == "error"] if out is None else out.errors
                raise RuntimeError(f"solve failed at size {h:g}: " + "; ".join(errs[:3]))
            dof_per_node = {"thermal": 1, "cfd": 4}.get(getattr(session.results, "kind", ""), 3)
            levels.append({"size": h, "nodes": mesh.n_nodes, "elements": mesh.n_elements,
                           "dofs": mesh.n_nodes * dof_per_node, "seconds": time.time() - t0,
                           "values": monitored(session)})
            say(f"level {k + 1}: {mesh.n_elements} elements, " +
                ", ".join(f"{n} = {v:.6g}" for n, v in levels[-1]["values"].items()))
    finally:
        session.delete_study(dup.id)
        session.set_active_study(original)
    names = list(levels[0]["values"])
    quantities = {n: [lv["values"][n] for lv in levels] for n in names}
    return {"study": p.study_by_id(original).name, "sizes": [lv["size"] for lv in levels],
            "elements": [lv["elements"] for lv in levels], "dofs": [lv["dofs"] for lv in levels],
            "quantities": quantities,
            "assessment": {n: assess([lv["dofs"] for lv in levels], v) for n, v in quantities.items()},
            "ylabel": "value"}


# ----------------------------------------------------------------------------- automatic loop
SINGULAR_HINT = ("peak stresses at sharp re-entrant edges, point loads or clamped edges are singular and are not "
                 "valid convergence indicators")


def monitor_values(session, monitors: list[dict]) -> dict[str, float]:
    """Values of user-defined monitors on the current results.

    * ``{"kind": "probe", "quantity": "U_mag", "point": [x, y, z], "name": ...}``: value at a point
      (interpolated inside the element containing it);
    * ``{"kind": "summary", "key": "max_displacement", "name": ...}``: a summary value;
    * ``{"kind": "force", "key": "wall_force_N", "component": 0, ...}``: a vector summary component."""
    from opencae.postprocessing.sampling import interpolate_nodal
    rs = session.results
    sm = rs.summary()
    out = {}
    for m in monitors:
        name = m.get("name") or (f"{m['quantity']} at {m['point']}" if m["kind"] == "probe" else m["key"])
        if m["kind"] == "probe":
            v, n_out = interpolate_nodal(session.mesh, rs.quantity(m["quantity"]), np.asarray([m["point"]], float))
            if n_out:
                raise ValueError(f"monitor point {m['point']} is outside the mesh")
            out[name] = float(v[0])
        elif m["kind"] == "summary":
            out[name] = float(sm[m["key"]])
        elif m["kind"] == "force":
            out[name] = float(np.asarray(sm[m["key"]])[int(m.get("component", 0))])
        else:
            raise ValueError(f"unknown monitor kind '{m['kind']}'")
    return out


def check_monitors(monitors: list[dict]) -> list[str]:
    """Warnings for monitors that cannot demonstrate convergence."""
    w = []
    for m in monitors:
        key = (m.get("key") or m.get("quantity") or "").lower()
        if m["kind"] == "summary" and ("von_mises" in key or "max_principal" in key):
            w.append(f"'{m.get('name') or key}': {SINGULAR_HINT}; use a probe point away from them")
    if not monitors:
        w.append("no monitor defined")
    return w


def _scale_sizes(ms, f: float) -> None:
    ms.size_max *= f
    if ms.size_min:
        ms.size_min *= f
    for r in ms.refinements:
        r.size *= f
    if getattr(ms, "bl_first", 0.0):
        ms.bl_first *= f


def run_auto(session, monitors: list[dict], tol: float = 0.01, max_levels: int = 5, ratio: float = 1.5,
             budget: Optional[dict] = None, progress: Optional[Callable[[str], None]] = None) -> dict:
    """Refine the active study's mesh (all sizes / ``ratio`` per level) and re-solve until every
    monitored value changes by less than ``tol`` (relative) between the last two levels and at
    least 3 levels exist (so an order and GCI can be computed), or a limit is reached:
    ``max_levels``, the resource ``budget`` (estimated memory / time of the *next* level), or a
    failed mesh / solve. Works on a copy; the user's study is unchanged."""
    from opencae.meshing.intelligent import default_budget, solver_cost
    say = progress or (lambda m: None)
    budget = budget or default_budget()
    p = session.project
    original = p.active_study
    dup = session.duplicate_study(original, "(auto convergence)", activate=True)
    dup.mesh_settings.sizing_mode = "size"
    if not dup.mesh_settings.size_max:
        dup.mesh_settings.size_max = session.analysis_body().size / 20.0
    levels, reason = [], ""
    warnings = check_monitors(monitors)
    try:
        for k in range(max_levels):
            if k:
                _scale_sizes(dup.mesh_settings, 1.0 / ratio)
            if levels:
                dof_next = levels[-1]["dofs"] * ratio ** 3
                mem, sec = solver_cost(int(dof_next))
                if mem > budget["memory_mb"] or sec > budget["seconds"]:
                    reason = (f"resource limit: the next level needs ~{dof_next:,.0f} DOF, ~{mem:,.0f} MB and "
                              f"~{sec:,.0f} s (budget {budget['memory_mb']:,.0f} MB, {budget['seconds']:,.0f} s)")
                    break
            say(f"level {k + 1}: element size {dup.mesh_settings.size_max:.4g}")
            t0 = time.time()
            try:
                mesh = session.generate_mesh()
                out, issues = session.run_solver()
            except Exception as exc:  # noqa: BLE001 - recorded as the stop reason
                reason = f"level {k + 1} failed: {exc}"
                break
            if out is None or not out.success:
                errs = [str(i) for i in issues if i.severity == "error"] if out is None else out.errors
                reason = f"level {k + 1} solve failed: " + "; ".join(errs[:2])
                break
            dof_per_node = {"thermal": 1, "cfd": 4}.get(getattr(session.results, "kind", ""), 3)
            vals = monitor_values(session, monitors)
            levels.append({"size": dup.mesh_settings.size_max, "nodes": mesh.n_nodes, "elements": mesh.n_elements,
                           "dofs": mesh.n_nodes * dof_per_node, "seconds": time.time() - t0, "values": vals})
            say(f"level {k + 1}: {mesh.n_elements:,} elements, " +
                ", ".join(f"{n} = {v:.6g}" for n, v in vals.items()))
            if len(levels) >= 3:
                def rel(a, b, n):
                    return abs(a["values"][n] - b["values"][n]) / max(abs(a["values"][n]), 1e-300)
                ch = [max(rel(levels[-1], levels[-2], n), rel(levels[-2], levels[-3], n)) for n in vals]
                if max(ch) < tol:
                    reason = (f"converged: all monitored values changed by < {100 * tol:g} % in each of the last two "
                              "refinements")
                    break
        else:
            reason = f"level limit ({max_levels}) reached before the {100 * tol:g} % criterion was met"
    finally:
        session.delete_study(dup.id)
        session.set_active_study(original)
    names = list(levels[0]["values"]) if levels else []
    quantities = {n: [lv["values"][n] for lv in levels] for n in names}
    assessment = {n: assess([lv["dofs"] for lv in levels], v) for n, v in quantities.items()}
    for n, a in assessment.items():
        if len(levels) >= 3 and a["gci"] is None:
            warnings.append(f"'{n}': the changes did not shrink monotonically between levels (unstructured meshes "
                            "do not refine systematically), so no observed order or GCI is reported; the result is "
                            "judged by the change criterion only")
    return {"study": p.study_by_id(original).name, "sizes": [lv["size"] for lv in levels],
            "elements": [lv["elements"] for lv in levels], "dofs": [lv["dofs"] for lv in levels],
            "seconds": [lv["seconds"] for lv in levels], "quantities": quantities,
            "assessment": assessment,
            "stop_reason": reason, "converged": reason.startswith("converged"), "warnings": warnings,
            "tolerance": tol, "ylabel": "value"}


def summary_html(res: dict) -> str:
    rows = []
    for n, a in res["assessment"].items():
        vals = res["quantities"][n]
        if a["converging"] is False:
            verdict = "<span style='color:#c53030'>not converging — changes do not shrink (likely a singularity " \
                      "or too coarse levels)</span>"
        elif a["converging"]:
            verdict = (f"converging, observed order {a['order']:.2f}, extrapolated {a['extrapolated']:.6g}, "
                       f"GCI {100 * a['gci']:.2f} %" if a["order"] is not None else "converged (no change)")
        else:
            verdict = f"last change {100 * a['relative_change_last']:.2f} % (need 3 levels for an estimate)"
        rows.append(f"<tr><td>{n}</td><td>{' → '.join(f'{v:.5g}' for v in vals)}</td><td>{verdict}</td></tr>")
    lv = ", ".join(f"h={h:g}: {e} el." for h, e in zip(res["sizes"], res["elements"]))
    extra = ""
    if res.get("stop_reason"):
        extra += f"<p><b>Stop reason:</b> {res['stop_reason']}</p>"
    for w in res.get("warnings", []):
        extra += f"<p style='color:#c05621'>⚠ {w}</p>"
    return (f"<h3>Mesh convergence — {res['study']}</h3><p>{lv}</p>{extra}<table border=1 cellpadding=4><tr>"
            "<th>Quantity</th><th>Coarse → fine</th><th>Assessment</th></tr>" + "".join(rows) +
            "</table><p>Your study, mesh and results were not changed. Use the Plot Explorer for the chart.</p>")


# ----------------------------------------------------------------------------- worker
def main(argv: Optional[list[str]] = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if len(argv) != 1:
        print("usage: python -m opencae.postprocessing.convergence job.json", file=sys.stderr)
        return 2
    try:
        from opencae.app.session import Session
        from opencae.project import io as pio
        job = json.loads(Path(argv[0]).read_text())
        s = Session()
        s.new()
        s._scratch = Path(tempfile.mkdtemp(prefix="opencae_conv_"))
        src = Path(job["data_dir"])
        for f in job["project"]["project"]["geometry"]["features"]:  # imported CAD files (read-only copies)
            rel = f.get("params", {}).get("file")
            if f.get("kind") == "import" and rel and not Path(rel).is_absolute() and (src / rel).exists():
                (s._scratch / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src / rel, s._scratch / rel)
        s.project = pio.project_from_dict(job["project"])
        s._derived = {}
        s.regenerate()
        s.set_active_study(job["study"])
        say = lambda m: print(f"PROGRESS: {m}", flush=True)  # noqa: E731
        if job.get("auto"):
            res = run_auto(s, job["monitors"], job.get("tol", 0.01), job.get("max_levels", 5),
                           job.get("ratio", 1.5), job.get("budget"), say)
        else:
            res = run(s, job["sizes"], say)
        Path(job["out"]).write_text(json.dumps(res))
        print("DONE: convergence study finished", flush=True)
        shutil.rmtree(s._scratch, ignore_errors=True)
        return 0
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", flush=True)
        traceback.print_exc(file=sys.stdout)
        return 1


if __name__ == "__main__":
    sys.exit(main())
