"""Closed-form reference solutions used for comparison and validation.

These are *approximations* with stated assumptions; differences against a
3-D solid FE model are expected and explained in the report.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np


@dataclass
class Comparison:
    case: str
    quantities: list[dict] = field(default_factory=list)  # {name, theory, fe, unit, diff_pct, note}
    assumptions: list[str] = field(default_factory=list)

    def add(self, name, theory, fe, unit, note=""):
        diff = 100.0 * (fe - theory) / theory if theory else float("nan")
        self.quantities.append({"name": name, "theory": float(theory), "fe": float(fe), "unit": unit,
                                "diff_pct": float(diff), "note": note})


def cowper_shear_coefficient(nu: float) -> float:
    """Timoshenko shear coefficient for a rectangular section (Cowper 1966)."""
    return 10.0 * (1.0 + nu) / (12.0 + 11.0 * nu)


def cantilever_tip(F: float, L: float, b: float, h: float, E: float, nu: float) -> dict[str, float]:
    """Rectangular cantilever, tip load F transverse to the dimension h.
    b = section dimension perpendicular to load, h = section depth along load."""
    I = b * h ** 3 / 12.0
    A = b * h
    G = E / (2.0 * (1.0 + nu))
    d_eb = F * L ** 3 / (3.0 * E * I)
    d_shear = F * L / (cowper_shear_coefficient(nu) * G * A)
    return {"I": I, "A": A, "G": G, "delta_eb": d_eb, "delta_timoshenko": d_eb + d_shear,
            "sigma_root": F * L * (h / 2.0) / I}


def axial_bar(F: float, L: float, A: float, E: float) -> dict[str, float]:
    return {"u": F * L / (A * E), "sigma": F / A}


CANTILEVER_ASSUMPTIONS = [
    "Euler-Bernoulli: plane sections remain plane and normal to the axis; shear deformation neglected.",
    "Timoshenko: adds shear deformation with Cowper's shear coefficient k = 10(1+nu)/(12+11nu).",
    "Small displacements, linear-elastic isotropic material.",
    "Beam theory assumes the load acts at the section centroid and ignores how the end is clamped; "
    "the 3-D model fixes all displacements on the root face (which also suppresses Poisson contraction, "
    "slightly stiffening the root) and applies the load as a uniform traction on the tip face.",
    "Beam theory gives the bending stress M*c/I away from the ends. The 3-D model has stress "
    "singularities at the corners of the fully clamped root face, so peak FE stress there grows with "
    "mesh refinement and must not be compared directly; compare stresses a section depth away from the root.",
]


def detect_cantilever(session) -> Optional[dict]:
    """Detect the cantilever-template configuration in a session and return
    its parameters, or None if the model is not a simple cantilever."""
    p = session.project
    body = session.analysis_body()
    if body is None:
        return None
    feats = [f for f in p.geometry.features if not f.suppressed]
    src = next((f for f in feats if f.id == body.id), None)
    if src is None or src.kind != "primitive" or src.params.get("shape") not in ("cantilever_beam", "beam"):
        return None
    if src.params.get("shape") == "beam" and src.params.get("section", "rectangle") != "rectangle":
        return None
    if any(f.params.get("body") == body.id or f.params.get("target") == body.id for f in feats if f is not src):
        return None  # modified after creation
    cons = [c for c in p.constraints if not c.suppressed]
    loads = [l for l in p.loads if not l.suppressed]
    if len(cons) != 1 or cons[0].kind != "fixed" or len(loads) != 1 or loads[0].kind != "force":
        return None
    ld = loads[0]
    if ld.params.get("coordinate_system", "global") != "global" or len(ld.targets) != 1 or ld.targets[0].kind != "face":
        return None
    x0 = float(src.params.get("position", [0, 0, 0])[0])
    L = float(src.params["length"])
    fx = cons[0].targets
    if len(fx) != 1 or fx[0].kind != "face":
        return None
    nfix = body.faces[fx[0].index].normal if 0 <= fx[0].index < len(body.faces) else None
    ntip = body.faces[ld.targets[0].index].normal if 0 <= ld.targets[0].index < len(body.faces) else None
    if nfix is None or ntip is None or nfix[0] > -0.999 or ntip[0] < 0.999:
        return None
    d = np.asarray(ld.params.get("direction", [0, 0, -1]), float)
    d = d / np.linalg.norm(d)
    if abs(d[0]) > 1e-9 or not (abs(d[1]) > 0.999 or abs(d[2]) > 0.999):
        return None
    w = float(src.params["width"])
    h = float(src.params["height"])
    axis = 2 if abs(d[2]) > 0.999 else 1
    b, depth = (w, h) if axis == 2 else (h, w)
    return {"L": L, "b": b, "h": depth, "F": float(ld.params["magnitude"]), "direction": d, "axis": axis,
            "tip_face": ld.targets[0].index, "x0": x0}


def cantilever_comparison(session) -> Optional[Comparison]:
    info = detect_cantilever(session)
    rs = session.results
    if info is None or rs is None:
        return None
    from opencae.materials.library import material_in_units
    mat = material_in_units(session.project.material_for_body(session.analysis_body().id), session.units)
    th = cantilever_tip(info["F"], info["L"], info["b"], info["h"], mat["E"], mat["nu"])
    mesh = rs.mesh
    tip_nodes = mesh.face_nodes(info["tip_face"]) if info["tip_face"] in mesh.face_tris else None
    if tip_nodes is None:
        return None
    u_tip = float(np.mean(rs.displacement[tip_nodes] @ info["direction"]))
    c = Comparison("Cantilever beam with tip load")
    unit = session.units.length
    c.add("Tip deflection vs Euler-Bernoulli FL^3/(3EI)", th["delta_eb"], u_tip, unit,
          "FE: mean deflection of tip-face nodes along load direction")
    c.add("Tip deflection vs Timoshenko (with shear)", th["delta_timoshenko"], u_tip, unit)
    if rs.stress is not None:
        # bending stress at mid-span top/bottom fibre (away from root singularity and load introduction)
        x_mid = info["x0"] + info["L"] / 2
        ax = info["axis"]
        nodes = mesh.nodes
        sel = np.where(np.abs(nodes[:, 0] - x_mid) <= 0.6 * info["L"] / 20 + 1e-9)[0]
        if len(sel):
            fib = sel[np.argsort(np.abs(nodes[sel, ax]))[-max(1, len(sel) // 10):]]
            sxx = np.abs(rs.stress[fib, 0]).mean()
            M_mid = info["F"] * info["L"] / 2
            c.add("Bending stress |sxx| at mid-span outer fibre vs M*c/I", M_mid * (info["h"] / 2) / th["I"],
                  sxx, session.units.stress, "nodal-averaged FE stress, mean over outer-fibre nodes near x = L/2")
    c.assumptions = CANTILEVER_ASSUMPTIONS
    return c
