"""Mass, centre of gravity and inertia from the part masses and the closed volumes of the CAD parts."""
from __future__ import annotations

import numpy as np

from opencae.drone import spec as S
from opencae.drone.types import (CONDITIONAL, OK, Part, RunContext, metric, not_evaluated, result)

ID = "mass"
TITLE = "Mass and balance"
VERSION = "1"
NEEDS = ["geometry.units", "geometry.forward", "geometry.up", "roles", "role_materials", "materials", "mass",
         "propulsion.motor.mass_g"]


def _closed(tris: np.ndarray) -> bool:
    e = np.sort(np.concatenate([tris[:, [0, 1]], tris[:, [1, 2]], tris[:, [2, 0]]]), axis=1)
    _, cnt = np.unique(e, axis=0, return_counts=True)
    return bool(len(cnt) and (cnt == 2).all())


def solid_moments(p: Part):
    """(volume mm3, centroid mm, second-moment matrix about the centroid per unit volume mm2) of a part.
    Exact for a closed mesh; a bounding box stands in when the mesh is open (``exact`` False)."""
    P, T = p.points, p.triangles
    if len(T) and _closed(T):
        a, b, c = P[T[:, 0]], P[T[:, 1]], P[T[:, 2]]
        v = np.einsum("ij,ij->i", a, np.cross(b, c)) / 6.0
        V = v.sum()
        if abs(V) > 1e-9:
            cen = ((a + b + c) / 4.0 * v[:, None]).sum(0) / V
            s = a + b + c
            S2 = (v[:, None, None] / 20.0 * (a[:, :, None] * a[:, None, :] + b[:, :, None] * b[:, None, :]
                                             + c[:, :, None] * c[:, None, :]
                                             + s[:, :, None] * s[:, None, :])).sum(0)
            S2 = S2 / V - np.outer(cen, cen)
            return abs(V), cen, S2, True
    lo, hi = P.min(0), P.max(0)
    d = hi - lo
    return float(np.prod(d)), (lo + hi) / 2, np.diag(d ** 2 / 12.0), False


def run(ctx: RunContext) -> dict:
    spec, ac = ctx.spec, ctx.aircraft
    ctx.progress("mass", 0.0)
    if not ac.parts:
        return not_evaluated(ID, TITLE, ["geometry.file"], "The CAD file has no parts.")
    parts_mass = (spec.get("mass") or {}).get("parts") or {}
    masses, bases, missing, notes, used = {}, [], [], [], set()
    shapes = {}
    for p in ac.parts:
        shapes[p.name] = solid_moments(p)
        g, src = None, None
        for key in (p.name, p.role):
            if key in parts_mass:
                pa = S.param(spec, f"mass.parts.{key}")
                if pa is not None and S._is_num(pa.value):
                    g, src, _ = float(pa.value), f"mass.parts.{key}", used.add(key)
                    bases.append(pa.status)
                    break
        if g is None and p.role == "motor":
            pa = S.param(spec, "propulsion.motor.mass_g")
            if pa is not None and S._is_num(pa.value):
                g, src = float(pa.value), "propulsion.motor.mass_g"
                bases.append(pa.status)
        if g is None:
            mat = (spec.get("role_materials") or {}).get(p.role)
            rho = S.param(spec, f"materials.{mat}.density_kg_m3") if mat else None
            if rho is not None and S._is_num(rho.value):
                V, _, _, exact = shapes[p.name]
                if exact:
                    g = V * 1e-9 * float(rho.value) * 1000.0
                    src = f"materials.{mat}.density_kg_m3 x volume"
                    bases.append(rho.status)
                else:
                    notes.append(f"part '{p.name}' is not a closed solid, so its volume is unknown")
        if g is None:
            missing.append(f"mass.parts.{p.role}" if p.role != "other" else f"mass.parts.{p.name}")
        else:
            masses[p.name] = g
    if missing:
        miss = list(dict.fromkeys(missing))
        extra_note = (" " + "; ".join(notes) + ".") if notes else ""
        return not_evaluated(ID, TITLE, miss,
                             "Not evaluated: these parts have no mass (set the grams in mass.parts, or give their "
                             "role a material density): " + ", ".join(miss) + "." + extra_note)
    pay = S.param(spec, "mass.payload_g") or S.param(spec, "mission.payload_g")
    pay_g = float(pay.value) if pay is not None and S._is_num(pay.value) else 0.0
    total_g = sum(masses.values()) + pay_g
    m_kg = total_g / 1000.0
    # centre of gravity (payload, if any, is not placed anywhere: it is counted in the mass only)
    cg = sum(np.asarray(shapes[n][1]) * g for n, g in masses.items()) / max(sum(masses.values()), 1e-12)
    cg_m = cg / 1000.0
    I = np.zeros((3, 3))
    for n, g in masses.items():
        m = g / 1000.0
        S2 = shapes[n][2] * 1e-6  # m2
        I += m * (np.trace(S2) * np.eye(3) - S2)
        d = (np.asarray(shapes[n][1]) - cg) / 1000.0
        I += m * (d @ d * np.eye(3) - np.outer(d, d))
    basis = S.weakest(bases)
    status = CONDITIONAL if basis == "estimate" else OK
    summary = (f"The aircraft weighs {total_g:.0f} g. Its centre of gravity is at x = {cg[0]:.0f} mm "
               f"(forward positive, measured from the CAD origin)." + (" The masses are estimates." if basis == "estimate" else ""))
    return result(ID, TITLE, status, summary, metrics={
        "mass_kg": metric(m_kg, "kg", OK, basis, "Take-off mass"),
        "cg_mm": metric(cg.tolist(), "mm", OK, basis, "Centre of gravity (x forward, y right, z up)"),
        "inertia_kgm2": metric(I.tolist(), "kg m2", OK, basis, "Inertia about the centre of gravity")},
        extra={"part_masses": {n: round(g, 3) for n, g in masses.items()}, "payload_g": pay_g,
               "unused_mass_keys": sorted(set(parts_mass) - used), "notes": notes})
