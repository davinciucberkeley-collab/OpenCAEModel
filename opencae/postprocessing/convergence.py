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
    return (f"<h3>Mesh convergence — {res['study']}</h3><p>{lv}</p><table border=1 cellpadding=4><tr>"
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
        res = run(s, job["sizes"], lambda m: print(f"PROGRESS: {m}", flush=True))
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
