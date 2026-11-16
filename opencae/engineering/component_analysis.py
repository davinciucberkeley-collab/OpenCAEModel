"""Component-level structural analysis on the engineering model: choose what to analyse, rebuild it, solve it.

The K09-L geometry is a tessellated Fusion export (no B-rep), so a component is analysed by rebuilding
an exact solid from the dimensions in its authoritative part name (e.g. ``Motor tube CF 16x13.08x380``:
OD 16, ID 13.08, length 380 mm) and placing it where the mesh body is: the tube axis is the principal
direction (PCA) of the body's mesh points, the centre is the middle of its axial extent. Dimensions from
the name are cross-checked against the mesh (and the vendor part number where it carries dimensions) and
the differences are reported, never silently used.

Selection engine (:func:`analysis_context`)
-------------------------------------------
For a physical question (``static_strength``) the structurally relevant set follows the load path:

R1  *Analysed part*: the component instance itself (rebuilt as a solid).
R2  *Load introduction*: bodies in contact with the part that carry an external load into it (a motor
    mount carrying a propeller joint). If their geometry cannot be rebuilt from their part data (printed
    PETG elbows have no dimensions in the name) they are represented by a remote load (force at the
    true load point + its moment) on the overlap region (the sleeve) of the part.
R3  *Support path*: the chain of bodies from the part to the primary structure. The support boundary
    is placed where that chain is clamped to the primary structure; links whose material data are missing
    (Depron, XPS) are idealised as rigid there and as carrying nothing elsewhere (conservative for the
    part: their distributed support is neglected). The rigid clamp creates a stress singularity at its edge.
R4  *Excluded*: fasteners (holes and bolt bearing are not modelled; listed as a limitation), wiring and
    other bodies that touch the part but do not carry its loads; each is listed with its reason.
R5  *Load cases*: maximum motor thrust (calcs.propulsion ``thrust_max_full`` / 4 per motor) with the motor
    reaction torque (calcs.motor_point ``p_shaft / omega`` at the same operating point) at the propeller
    joints; landing: the ground contact force (calcs.landing_impact) is applied only if the part lies
    between a contact point and the airframe; otherwise the part sees the landing through the inertia of
    the masses it carries (n x g x mass of the motor set, n from the same energy method).
R6  *Wing spar*: half-span cantilever clamped over the joiner overlap, carrying the half-wing lift at a
    design load factor (the XPS core's share of the bending is neglected - conservative).

Every load and result is a :class:`Q` with its evidence class: thrust rests on provisional propeller
coefficients and the CF laminate properties are estimates, so FE stresses and safety factors are
``provisional_estimate`` - they are never presented as verified.

:func:`run_component_analysis` builds an OpenCAE project in ``workdir`` (STEP of the rebuilt solid,
study, mesh, CalculiX solve) and returns stresses, displacements, the equilibrium check, mesh statistics,
the Euler-Bernoulli (with shear correction) beam check and provenance hashes.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
from pathlib import Path
from typing import Optional

import numpy as np

from opencae.engineering import calcs
from opencae.engineering.model import Body, Component, EngineeringModel
from opencae.engineering.quantities import (ESTIMATE, MISSING, REDUCED, SIMULATION, SUPPLIER, Q,
                                            derived, weakest)

MOTOR_TUBE = "Motor tube CF 16x13.08x380 — DragonPlate FDPT.625*.515*24"
SPAR_TUBE = "Spar tube CF 12x10x560 — Superstition CFT3K12101"
SPAR_JOINER = "Spar joiner CF rod 9.8x100 — K09L-AF-SPAR-JOINER"
INCH = 25.4
CONTACT_TOL = 1.5  # mm: a vertex this close to the part's surface counts as contact
UNRESOLVED_CF = ("CF tube laminate properties",
                 "Axial / hoop moduli, shear modulus and allowable stresses of the DragonPlate and Superstition CF "
                 "tubes and the CF rod are not in the BOM or a datasheet read here: FEA uses provisional estimates.")

# per-motor parts that ride on the motor mount (component name prefix, count per motor); masses from the BOM
MOTOR_SET = [("Motor SunnySky", 1.0), ("Motor mount PETG elbow", 1.0), ("Prop adapter collet", 1.0),
             ("Prop adapter ring", 1.0), ("Prop locknut M5", 1.0), ("Washer M5", 1.0), ("SHCS M3x8", 4.0)]


# =========================================================================== helpers
def _find_component(m: EngineeringModel, key: str) -> Component:
    if key in m.components:
        return m.components[key]
    hits = [k for k in m.components if key.lower() in k.lower()]
    if len(hits) == 1:
        return m.components[hits[0]]
    raise KeyError(f"component '{key}' not found" + (f" (ambiguous: {hits})" if hits else ""))


def parse_dimensions(name: str) -> dict:
    """Dimensions (mm) in a K09-L part name: 'CF 16x13.08x380' (tube OD x ID x L) or 'CF rod 9.8x100'
    (rod D x L); vendor inch dimensions 'FDPT.625*.515*24' (OD, ID, stock length) when present."""
    out: dict = {}
    t = re.search(r"CF\s+(\d+(?:\.\d+)?)x(\d+(?:\.\d+)?)x(\d+(?:\.\d+)?)", name)
    r = re.search(r"CF rod\s+(\d+(?:\.\d+)?)x(\d+(?:\.\d+)?)", name)
    if t:
        out.update(kind="tube", od=float(t.group(1)), id=float(t.group(2)), length=float(t.group(3)))
    elif r:
        out.update(kind="rod", od=float(r.group(1)), id=0.0, length=float(r.group(2)))
    v = re.search(r"(\.\d+)\*(\.\d+)\*(\d+(?:\.\d+)?)", name)
    if v:
        out["vendor"] = {"od": float(v.group(1)) * INCH, "id": float(v.group(2)) * INCH,
                         "stock_length": float(v.group(3)) * INCH, "text": v.group(0)}
    return out


def _unique_points(P: np.ndarray) -> np.ndarray:
    return np.unique(np.round(P, 4), axis=0)


def fit_tube(m: EngineeringModel, body: Body) -> dict:
    """Axis (PCA of the mesh points), centre, axial extent and inner / outer radius of a tube-like body."""
    P = _unique_points(m.body_mesh(body)[0])
    c0 = P.mean(axis=0)
    _, sv, vt = np.linalg.svd(P - c0, full_matrices=False)
    a = vt[0] / np.linalg.norm(vt[0])
    if a[int(np.argmax(np.abs(a)))] < 0:
        a = -a
    e1 = np.cross(a, [1.0, 0.0, 0.0] if abs(a[0]) < 0.9 else [0.0, 1.0, 0.0])
    e1 /= np.linalg.norm(e1)
    e2 = np.cross(a, e1)
    s = (P - c0) @ a
    p1, p2 = (P - c0) @ e1, (P - c0) @ e2
    centre = c0 + a * 0.5 * (s.max() + s.min()) + e1 * 0.5 * (p1.max() + p1.min()) + e2 * 0.5 * (p2.max() + p2.min())
    d = P - centre
    s = d @ a
    rad = np.linalg.norm(d - np.outer(s, a), axis=1)
    ang = math.degrees(math.acos(min(1.0, float(np.max(np.abs(a))))))
    return {"axis": a, "centre": centre, "e1": e1, "e2": e2, "s_min": float(s.min()), "s_max": float(s.max()),
            "length": float(s.max() - s.min()), "r_out": float(rad.max()), "r_in": float(rad.min()),
            "n_points": int(len(P)), "axis_deviation_from_global_deg": ang,
            "pca_ratio": float(sv[1] / sv[0]) if sv[0] > 0 else 0.0}


def _s_of(P: np.ndarray, fit: dict) -> np.ndarray:
    return (P - fit["centre"]) @ fit["axis"]


def _radial(P: np.ndarray, fit: dict) -> np.ndarray:
    d = P - fit["centre"]
    s = d @ fit["axis"]
    return np.linalg.norm(d - np.outer(s, fit["axis"]), axis=1)


def _bbox(m: EngineeringModel, b: Body) -> tuple[np.ndarray, np.ndarray]:
    P = m.body_mesh(b)[0]
    return P.min(axis=0), P.max(axis=0)


def _bbox_gap(A, B) -> float:
    """Largest separation along any axis between two boxes (<= 0: they overlap or touch)."""
    return float(np.max(np.maximum(A[0] - B[1], B[0] - A[1])))


def _touching_part(m: EngineeringModel, fit: dict, exclude: set, tol: float = CONTACT_TOL) -> list[Body]:
    """Active bodies with a mesh vertex within ``tol`` of the part's outer surface (or inside it)."""
    out = []
    for b in m.active_bodies():
        if b.index in exclude:
            continue
        P = m.body_mesh(b)[0]
        s = _s_of(P, fit)
        r = _radial(P, fit)
        if np.any((r <= fit["r_out"] + tol) & (s >= fit["s_min"] - tol) & (s <= fit["s_max"] + tol)):
            out.append(b)
    return out


def _prop_joints_on(m: EngineeringModel, fit: dict, reach: float = 45.0) -> list[dict]:
    out = []
    for j in m.joints.values():
        if not j["name"].startswith("Prop "):
            continue
        p = np.asarray(j["origin_mm"], float)
        s = float(_s_of(p[None], fit)[0])
        r = float(_radial(p[None], fit)[0])
        if r <= fit["r_out"] + reach and fit["s_min"] - reach <= s <= fit["s_max"] + reach:
            out.append(j)
    return out


def _side(y: float) -> str:
    return "right (y > 0)" if y > 0 else "left (y < 0)"


def _motor_label(jname: str) -> str:
    return jname.replace("Prop ", "").split(" ")[0]


# =========================================================================== material
def cf_tube_material(m: Optional[EngineeringModel] = None, comp: Optional[Component] = None,
                     volume_mm3: Optional[float] = None):
    """CF tube / rod laminate, transversely isotropic about the tube axis (material axis 3). The OpenCAE
    database has only an isotropic quasi-isotropic CFRP *example* (50 GPa, no strength), which is not a tube
    laminate, so a provisional record is created. Density from the BOM mass / rebuilt volume when given."""
    from opencae.project.model import Material
    E3, E1, G13, nu31 = 100e9, 10e9, 5.0e9, 0.30
    rho, rho_src = 1550.0, "typical CF/epoxy"
    if comp is not None and volume_mm3:
        mass = (m.component_mass(comp).value if m is not None else comp.unit_mass.value) or 0.0
        if mass > 0:
            rho, rho_src = mass / (volume_mm3 * 1e-9), f"BOM mass {mass * 1000:.1f} g / rebuilt volume"
    allow = 500e6
    mat = Material(
        "CF tube laminate (provisional estimate)", youngs_modulus=E3, poissons_ratio=0.30, density=rho,
        yield_strength=allow, thermal_expansion=1.0e-6, thermal_conductivity=5.0, specific_heat=900.0,
        is_example=True, category="Composite",
        source=("Provisional estimate, not a supplier value: typical standard-modulus CF/epoxy roll-wrapped / "
                "pultruded tube (axial modulus 70-130 GPa, compressive strength 500-900 MPa in open literature "
                "and vendor catalogues in general). DragonPlate FDPT / Superstition CFT3K datasheets not read. "
                f"Density: {rho_src}."),
        estimated=["E1", "E2", "E3", "nu12", "nu13", "nu23", "G12", "G13", "G23", "youngs_modulus",
                   "poissons_ratio", "yield_strength", "thermal_expansion", "thermal_conductivity", "specific_heat"]
        + ([] if comp is not None and volume_mm3 else ["density"]),
        uncertainty={"E3": 0.25, "G13": 0.4, "yield_strength": 0.3},
        strengths={"axial_allowable_estimate": allow},
        notes=("Transversely isotropic about the tube axis (material axis 3). The allowable is a compression-"
               "governed screening value without knock-down for clamping, bonding, holes or damage. "
               "Von Mises stress is not a failure criterion for a composite: compare the axial stress."),
        orthotropic={"E1": E1, "E2": E1, "E3": E3, "nu12": 0.45, "nu13": nu31 * E1 / E3, "nu23": nu31 * E1 / E3,
                     "G12": E1 / (2 * 1.45), "G13": G13, "G23": G13})
    return mat


def _material_q(mat) -> dict[str, Q]:
    ref = mat.source
    return {"E_axial": Q(mat.orthotropic["E3"], "Pa", ESTIMATE, ref, uncertainty=0.25),
            "G_axial": Q(mat.orthotropic["G13"], "Pa", ESTIMATE, ref, uncertainty=0.4),
            "allowable": Q(mat.yield_strength, "Pa", ESTIMATE, ref, uncertainty=0.3,
                           note="screening value, not a verified allowable")}


def _register_unresolved(m: EngineeringModel) -> None:
    if not any(u[0] == UNRESOLVED_CF[0] for u in m.unresolved):
        m.unresolved.append(UNRESOLVED_CF)


def _material_status(c: Component) -> dict:
    """What is known about the material of a component in the load path."""
    from opencae.materials.database import database_materials
    db = {mm.category: mm for mm in database_materials()}
    mat = c.material or "unknown"
    if mat == "Carbon fibre":
        return {"material": mat, "database": None, "status": ESTIMATE,
                "missing": ["axial and hoop moduli", "shear modulus", "allowable stresses"],
                "note": "no CF tube record in the OpenCAE database (only an isotropic CFRP example); "
                        "provisional estimate used, registered as unresolved"}
    if mat.startswith("PETG"):
        rec = db.get("PETG")
        return {"material": mat, "database": rec.name if rec else None, "status": SUPPLIER if rec else MISSING,
                "missing": ["strength of the printed elbow (print settings, orientation, layer adhesion) - not "
                            "tested"], "note": "UltiMaker PETG TDS specimens; printed-part strength unresolved"}
    return {"material": mat, "database": None, "status": MISSING,
            "missing": [f"{mat} stiffness and strength"], "note": "no database record"}


# =========================================================================== selection engine
def analysis_context(model: EngineeringModel, component_key: str, question: str = "static_strength",
                     instance: Optional[int] = None, load_factor: float = 4.0) -> dict:
    """The structurally relevant set for ``question`` and the load path of a component.

    Returns ``{"component", "instance", "question", "components": [{key, role, bodies, modelled_as, reason}],
    "loads": {case: [load dicts with Q]}, "supports", "assumptions", "materials", "missing", "unresolved",
    "geometry", "fe_template"}``. ``instance`` selects the body index of a multi-instance component; the
    default rule is documented in the result. Registers 'CF tube laminate properties' as unresolved."""
    if question != "static_strength":
        raise ValueError(f"question '{question}' is not supported (available: 'static_strength')")
    comp = _find_component(model, component_key)
    name = comp.name + " " + comp.key
    if comp.key.startswith("Motor tube"):
        ctx = _ctx_motor_tube(model, comp, instance)
    elif comp.key.startswith("Spar tube"):
        ctx = _ctx_spar_tube(model, comp, instance, load_factor)
    elif "CF" in name or "carbon" in name.lower():
        ctx = _ctx_generic(model, comp, instance)
        ctx["assumptions"].append("No load-path rule for this CF part yet: loads and supports must be defined "
                                  "before an FE run (run_component_analysis raises).")
    else:
        ctx = _ctx_generic(model, comp, instance)
    ctx["question"] = question
    ctx["config_hash"] = model.config_hash()
    ctx["geometry_hash"] = model.geometry_hash()
    if any(c.get("material") == "Carbon fibre" for c in ctx["materials"]):
        _register_unresolved(model)
        ctx["unresolved"].append(UNRESOLVED_CF)
    return ctx


def _geometry_block(m: EngineeringModel, comp: Component, body: Body) -> tuple[dict, dict]:
    fit = fit_tube(m, body)
    dims = parse_dimensions(comp.name) or parse_dimensions(comp.key)  # name follows design revisions
    g = {"body": body.index, "axis": fit["axis"].tolist(), "centre_mm": fit["centre"].tolist(),
         "axis_deviation_from_global_deg": fit["axis_deviation_from_global_deg"],
         "name": {k: dims.get(k) for k in ("od", "id", "length")},
         "mesh": {"od": 2 * fit["r_out"], "id": 2 * fit["r_in"], "length": fit["length"],
                  "volume_mm3": body.volume},
         "used": "part name"}
    if dims.get("od"):
        g["diff_pct"] = {"od": 100 * (2 * fit["r_out"] / dims["od"] - 1),
                         "length": 100 * (fit["length"] / dims["length"] - 1)}
        if dims.get("id"):
            g["diff_pct"]["id"] = 100 * (2 * fit["r_in"] / dims["id"] - 1)
            A = math.pi / 4 * (dims["od"] ** 2 - dims["id"] ** 2)
            g["diff_pct"]["volume"] = 100 * (body.volume / (A * dims["length"]) - 1)
    if "vendor" in dims:
        v = dims["vendor"]
        g["vendor"] = v
        if dims.get("od"):
            I_n = math.pi / 64 * (dims["od"] ** 4 - dims["id"] ** 4)
            I_v = math.pi / 64 * (v["od"] ** 4 - v["id"] ** 4)
            g["vendor_note"] = (f"vendor PN '{v['text']}' gives OD {v['od']:.3f} / ID {v['id']:.3f} mm (the name "
                                f"rounds the OD to {dims['od']:g}); with the vendor OD the bending stiffness would "
                                f"be {100 * (I_v / I_n - 1):+.1f} %. The name value is used.")
    return fit, dims, g


def _instances(m: EngineeringModel, comp: Component) -> list[Body]:
    active = {b.index for b in m.active_bodies()}
    return [m.bodies[i] for i in comp.bodies if i in active]


def _ctx_motor_tube(m: EngineeringModel, comp: Component, instance: Optional[int]) -> dict:
    bodies = _instances(m, comp)
    carried = {b.index: [j["name"] for j in _prop_joints_on(m, fit_tube(m, b))] for b in bodies}
    if instance is None:
        pick = next((b for b in bodies if any(n.startswith("Prop M1") for n in carried[b.index])), bodies[0])
        rule = "default: the instance that carries M1 (and M4)"
    else:
        pick = m.bodies[instance]
        rule = "requested instance"
    fit, dims, geo = _geometry_block(m, comp, pick)
    joints = _prop_joints_on(m, fit)
    side = _side(fit["centre"][1])
    others = [b.index for b in bodies if b.index != pick.index]
    inst = {"body": pick.index, "side": side, "carries": [j["name"] for j in joints],
            "rule": rule, "other_instances": {i: carried[i] for i in others},
            "text": f"{side} motor tube (body {pick.index}, y = {fit['centre'][1]:+.0f} mm) carrying "
                    + " and ".join(_motor_label(j["name"]) for j in joints)}
    touching = _touching_part(m, fit, {pick.index})
    comps = [{"key": comp.key, "role": "analysed part", "bodies": [pick.index],
              "modelled_as": "solid rebuilt from the part-name dimensions (C3D10, transversely isotropic CF)",
              "reason": "R1: the component under study"}]
    mounts, fasteners, supports_path, excluded = [], [], [], []
    for b in touching:
        c = m.components[b.component]
        if b.component.startswith("Motor mount"):
            mounts.append(b)
        elif c.group in ("hardware", "propulsion") and ("SHCS" in c.key or "Locknut" in c.key or "Washer" in c.key):
            fasteners.append(b)
        elif b.component.startswith("Fin Depron"):
            supports_path.append(b)
        else:
            excluded.append(b)
    # motor mounts: sleeve overlap with the tube and the motor they carry
    sleeves = []
    for b in mounts:
        P = m.body_mesh(b)[0]
        s = _s_of(P, fit)
        lo, hi = max(float(s.min()), fit["s_min"]), min(float(s.max()), fit["s_max"])
        cen = P.mean(axis=0)
        j = min(joints, key=lambda jj: float(np.linalg.norm(np.asarray(jj["origin_mm"]) - cen))) if joints else None
        sleeves.append({"body": b.index, "s_range": [lo, hi], "joint": j["name"] if j else None})
        comps.append({"key": b.component, "role": "load introduction", "bodies": [b.index],
                      "modelled_as": "remote load (force at the propeller joint + its moment) and the motor reaction "
                                     f"torque on the tube's sleeve region s = {lo:.1f}..{hi:.1f} mm",
                      "reason": f"R2: mount of {_motor_label(j['name']) if j else '?'} clamped on the tube "
                                f"({hi - lo:.1f} mm overlap); PETG elbow geometry has no dimensions in its part data, "
                                "so it is not rebuilt (its stiffness is not modelled)"})
    # support path: fin (bonded at the leading edge) -> fin clips -> wing core
    band = None
    clips = []
    for fb in supports_path:
        fbb = _bbox(m, fb)
        for b in m.active_bodies():
            if b.component.startswith("Fin clip") and _bbox_gap(_bbox(m, b), fbb) <= 0.5:
                clips.append(b)
    if clips:
        S = np.concatenate([_s_of(m.body_mesh(b)[0], fit) for b in clips])
        band = [float(S.min()), float(S.max())]
    for fb in supports_path:
        comps.append({"key": fb.component, "role": "support path", "bodies": [fb.index],
                      "modelled_as": "rigid over the fin-clip band, no support elsewhere (Depron properties missing)",
                      "reason": "R3: the only body in contact along the tube's length (fin leading edge on the tube "
                                "OD); carries the tube loads to the fin clips"})
    by_comp: dict[str, list[int]] = {}
    for b in clips:
        by_comp.setdefault(b.component, []).append(b.index)
    for k, idx in by_comp.items():
        comps.append({"key": k, "role": "support", "bodies": idx,
                      "modelled_as": f"fixed support of the tube's outer surface over their axial band "
                                     f"s = {band[0]:.1f}..{band[1]:.1f} mm",
                      "reason": "R3: clamp the fin (and with it the tube) to the wing; first link to the primary "
                                "structure"})
    wing = [b for b in m.active_bodies() if b.component.startswith("Wing core")]
    if wing:
        comps.append({"key": wing[0].component, "role": "reaction sink (not modelled)", "bodies": [wing[0].index],
                      "modelled_as": "rigid ground behind the clips", "reason": "R3: primary structure"})
    for mb in mounts:  # clamp hardware of the mounts (bolts beside the tube, nuts, washers)
        mbb = _bbox(m, mb)
        for b in m.active_bodies():
            c = m.components[b.component]
            if (("SHCS" in c.key or "Locknut" in c.key or "Washer M3" in c.key) and b not in fasteners
                    and _bbox_gap(_bbox(m, b), mbb) <= 0.0):
                P = m.body_mesh(b)[0]
                if np.min(_radial(P, fit)) > fit["r_out"] and np.all(_s_of(P, fit) <= fit["s_max"] + 1):
                    fasteners.append(b)
    fb_by_comp: dict[str, list[int]] = {}
    for b in fasteners:
        fb_by_comp.setdefault(b.component, []).append(b.index)
    for k, idx in fb_by_comp.items():
        comps.append({"key": k, "role": "excluded (fastener)", "bodies": idx, "modelled_as": "not modelled",
                      "reason": "R4: clamp bolt / nut of the motor-mount sleeve (beside the tube, not through it); "
                                "the clamping pressure on the tube is not modelled"})
    ex_by_comp: dict[str, list[int]] = {}
    for b in excluded:
        ex_by_comp.setdefault(b.component, []).append(b.index)
    for k, idx in ex_by_comp.items():
        comps.append({"key": k, "role": "excluded", "bodies": idx, "modelled_as": "not modelled",
                      "reason": "R4: touches the tube but does not carry its loads"})
    # ---------------------------------------------------------------- loads
    mp = calcs.mass_properties(m)
    prop = calcs.propulsion(m, mp)
    T4 = prop["thrust_max_full"]
    v = prop["v_batt_max_full"].value
    op = calcs.motor_point(m, v, 1.0)
    omega = op["rpm"] * 2 * math.pi / 60
    mot, pr = m.option("motor"), m.option("prop")
    ins = {"T4": T4, "kv": mot.q("kv"), "rm": mot.q("rm"), "ct": pr.q("ct0"), "cp": pr.q("cp0")}
    F = derived(T4.value / 4, "N", "calcs.propulsion thrust_max_full / 4 (full battery, under load)", {"T4": T4})
    Qm = derived(op["p_shaft"] / omega, "N m", "calcs.motor_point p_shaft / omega at the full-throttle point "
                                                f"({op['rpm']:.0f} rpm, {v:.2f} V)", ins)
    land = calcs.landing_impact(m, mp)
    loads_thrust, loads_land = [], []
    for sl in sleeves:
        j = m.joints.get(sl["joint"]) if sl["joint"] else None
        if j is None:
            continue
        ax = np.asarray(j["axis"], float)
        ax /= np.linalg.norm(ax)
        ccw = "CCW" in j["name"]
        lab = _motor_label(j["name"])
        loads_thrust.append({"name": f"{lab} thrust (max)", "kind": "remote_force", "q": F, "value_N": F.value,
                             "direction": ax.tolist(), "point_mm": list(j["origin_mm"]), "s_range": sl["s_range"],
                             "motor": j["name"],
                             "note": "thrust along the propeller joint axis, acting at the joint origin"})
        # prop angular velocity: CCW seen from the thrust side -> omega along the thrust axis (= joint axis);
        # the reaction on the stator is opposite to it
        tdir = -(ax if ccw else -ax)
        loads_thrust.append({"name": f"{lab} reaction torque", "kind": "moment", "q": Qm, "value_Nmm": Qm.value * 1e3,
                             "direction": tdir.tolist(), "s_range": sl["s_range"], "motor": j["name"],
                             "note": ("reaction on the motor stator, opposite to the propeller rotation "
                                      f"({'CCW' if ccw else 'CW'} seen from the thrust side; sign convention, the "
                                      "peak stress does not depend on it: the torque bends the tube in the plane "
                                      "normal to the thrust bending)")})
        mset, cg = _motor_set_mass(m, j, ccw)
        if not land["landing_n"].missing:
            n = land["landing_n"]
            Fl = derived(n.value * m.params["g"].value * mset.value, "N",
                         "n x g x motor-set mass (energy method, 1 m/s touchdown)", {"n": n, "m": mset}, kind=REDUCED)
            loads_land.append({"name": f"{lab} motor-set inertia (landing)", "kind": "remote_force", "q": Fl,
                               "value_N": Fl.value, "direction": (-ax).tolist(), "point_mm": cg.tolist(),
                               "s_range": sl["s_range"], "motor": j["name"],
                               "note": f"n = {n.value:.1f} g on {mset.value * 1000:.0f} g (motor, prop, mount, "
                                       "adapters, screws) towards the ground (+x when standing on the tail)"})
    # is the tube in the ground-contact load path?
    cps = calcs.contact_points(m) * 1000.0
    dmin = float(np.min(np.hypot(np.maximum(0.0, np.abs(_s_of(cps, fit)) - fit["length"] / 2),
                                 np.maximum(0.0, _radial(cps, fit) - fit["r_out"])))) if len(cps) else math.inf
    land_path = dmin < 5.0
    landing_note = (f"Ground contact at the {'feet' if m.option('landing').geometry != 'suppress' else 'fin tips'} "
                    f"(nearest contact point {dmin:.0f} mm from the tube): "
                    + ("the tube IS in the ground load path, the contact force is applied at the contact point."
                       if land_path else
                       "the contact force goes contact point -> fin (aft end) -> fin clips -> wing and does not pass "
                       "through the tube; the tube sees the landing through the inertia of the motor sets it "
                       "carries (n x g x mass)."))
    if land_path and "landing_force_per_contact" in land:
        loads_land.append({"name": "ground contact", "kind": "info", "q": land["landing_force_per_contact"],
                           "note": "contact on the tube itself: not modelled by this template"})
    supports = [{"name": "Fin clips / wing (fin taken as rigid there)", "kind": "fixed", "face": "outer",
                 "s_range": band,
                 "reason": "tube loads reach the primary structure (wing) through the fin and its clips; the fin's "
                           "distributed support along the rest of the tube is neglected (conservative for the tube)"}]
    mats = [{"component": comp.key, **_material_status(comp)}]
    for k in {c["key"] for c in comps if c["role"] in ("load introduction", "support", "support path")}:
        mats.append({"component": k, **_material_status(m.components[k])})
    missing = sorted({f"{x['component']}: {', '.join(x['missing'])}" for x in mats if x["missing"]})
    assumptions = [
        "Linear static, small displacements, transversely isotropic CF tube (estimated properties).",
        f"Support: tube outer surface fixed over the fin-clip band s = {band[0]:.1f}..{band[1]:.1f} mm (rigid clamp: "
        "stress singularity at its edges; compare stresses 1.5 OD away)." if band else "Support band not found.",
        "Motor mounts not rebuilt: each motor's loads act as a remote load on the tube's sleeve region (uniform "
        "traction + moment of the offset); the stiffening of the tube by the PETG sleeve is not modelled.",
        "Max-thrust case: all four motors at full throttle with a full battery under load; motor-set weight and "
        "inertia during the resulting climb acceleration are neglected (they reduce the net load: conservative).",
        "The clamping pressure of the mount sleeves (M3 clamp bolts) and any adhesive are not modelled.",
        landing_note,
    ]
    return {"component": comp.key, "instance": inst, "components": comps,
            "loads": {"max_thrust": loads_thrust, "landing": loads_land},
            "load_inputs": {"thrust_max_full_total": T4, "thrust_per_motor": F, "reaction_torque": Qm,
                            "rpm": Q(op["rpm"], "rpm", T4.source, "calcs.motor_point"),
                            "landing_n": land.get("landing_n"),
                            "landing_force_per_contact": land.get("landing_force_per_contact")},
            "landing_in_load_path": land_path, "supports": supports, "assumptions": assumptions,
            "materials": mats, "missing": missing, "unresolved": [], "geometry": geo,
            "fe_template": "tube", "_fit": fit, "_dims": dims, "_sleeves": sleeves, "_band": band}


def _motor_set_mass(m: EngineeringModel, j: dict, ccw: bool) -> tuple[Q, np.ndarray]:
    """Mass (kg, BOM) of the parts carried by one motor mount, and their centroid (mm) near the joint."""
    total, srcs = 0.0, []
    parts = MOTOR_SET + [("Prop APC 11x4.5MR (CCW)" if ccw else "Prop APC 11x4.5MRP (CW)", 1.0)]
    for prefix, n in parts:
        c = next((c for k, c in m.components.items() if k.startswith(prefix)), None)
        if c is None:
            continue
        q = m.component_mass(c)
        total += (q.value or 0.0) * n
        srcs.append(q.source)
    o = np.asarray(j["origin_mm"], float)
    pts, w = [], []
    for b in m.active_bodies():
        if b.group == "propulsion" and np.linalg.norm(b.centroid - o) < 40.0 and b.volume > 0:
            pts.append(b.centroid)
            w.append(b.volume)
    cg = (np.asarray(pts) * np.asarray(w)[:, None]).sum(0) / sum(w) if w else o
    return Q(total, "kg", weakest(srcs) if srcs else MISSING, "BOM unit masses of the motor set"), cg


def _ctx_spar_tube(m: EngineeringModel, comp: Component, instance: Optional[int], load_factor: float) -> dict:
    bodies = _instances(m, comp)
    if instance is None:
        pick = max(bodies, key=lambda b: b.centroid[1])
        rule = "default: the right-hand half (y > 0); the left half is its mirror image"
    else:
        pick, rule = m.bodies[instance], "requested instance"
    fit, dims, geo = _geometry_block(m, comp, pick)
    touching = _touching_part(m, fit, {pick.index})
    comps = [{"key": comp.key, "role": "analysed part", "bodies": [pick.index],
              "modelled_as": "solid rebuilt from the part-name dimensions", "reason": "R1: the component under study"}]
    joiner = [b for b in touching if b.component.startswith("Spar joiner")]
    band = None
    if joiner:
        s = _s_of(m.body_mesh(joiner[0])[0], fit)
        band = [max(float(s.min()), fit["s_min"]), min(float(s.max()), fit["s_max"])]
        comps.append({"key": joiner[0].component, "role": "support", "bodies": [joiner[0].index],
                      "modelled_as": f"fixed support of the tube's inner surface over the overlap s = {band[0]:.1f}.."
                                     f"{band[1]:.1f} mm (joiner taken as rigid)",
                      "reason": "R6: joins the two spar halves through the pod; the half-span cantilever is clamped "
                                "where the joiner ends"})
    wing = [b for b in m.active_bodies() if b.component.startswith("Wing core")]
    if wing:
        comps.append({"key": wing[0].component, "role": "load introduction", "bodies": [wing[0].index],
                      "modelled_as": "uniform lift traction on the tube outboard of the joiner",
                      "reason": "R6: the wing core carries the lift into the spar; its own bending stiffness is "
                                "neglected (XPS data missing: conservative for the spar)"})
    for b in touching:
        if b.index not in [x.index for x in joiner] and not b.component.startswith("Wing core"):
            comps.append({"key": b.component, "role": "excluded", "bodies": [b.index], "modelled_as": "not modelled",
                          "reason": "R4: touches the spar but does not carry its bending"})
    mp = calcs.mass_properties(m)
    wr = calcs.wing_reference(m)
    semi = wr["span"].value * 500.0  # mm
    n = Q(load_factor, "-", ESTIMATE, "assumed design load factor (no K09-L flight-envelope requirement)")
    root_end = (band[1] - fit["s_min"]) if band else 0.0
    frac = (semi - root_end) / semi
    L = derived(n.value * mp["weight"].value / 2 * frac, "N",
                "n x W / 2, uniform over the semi-span; the share outboard of the joiner is put on the tube",
                {"n": n, "W": mp["weight"], "b": wr["span"]})
    up = np.array([0.0, 0.0, 1.0])
    loads = [{"name": "Half-wing lift (outboard of the joiner)", "kind": "force", "q": L, "value_N": L.value,
              "direction": up.tolist(), "s_range": [band[1] if band else fit["s_min"], fit["s_max"]],
              "note": f"n = {load_factor:g}, uniform per unit length"}]
    supports = [{"name": "Spar joiner (rigid)", "kind": "fixed", "face": "inner", "s_range": band,
                 "reason": "half-span cantilever from the joiner end"}]
    mats = [{"component": comp.key, **_material_status(comp)}]
    if joiner:
        mats.append({"component": joiner[0].component, **_material_status(m.components[joiner[0].component])})
    missing = sorted({f"{x['component']}: {', '.join(x['missing'])}" for x in mats if x["missing"]}
                     | {"Wing core XPS: stiffness (its share of the bending is neglected)",
                        "Design load factor: no flight-envelope requirement in the K09-L files"})
    assumptions = ["Linear static, transversely isotropic CF tube (estimated properties).",
                   "Spar carries the whole wing bending (XPS core neglected) - conservative.",
                   "Uniform spanwise lift (conservative vs elliptic for the root moment); wing weight relief "
                   "neglected.",
                   "Hover/thrust loads on the spar are chordwise (in the wing plane) and taken by the core: not in "
                   "this case."]
    return {"component": comp.key,
            "instance": {"body": pick.index, "side": _side(fit["centre"][1]), "rule": rule,
                         "text": f"{_side(fit['centre'][1])} spar half (body {pick.index})"},
            "components": comps, "loads": {"lift": loads}, "load_inputs": {"lift_half": L, "load_factor": n},
            "landing_in_load_path": False, "supports": supports, "assumptions": assumptions, "materials": mats,
            "missing": missing, "unresolved": [], "geometry": geo, "fe_template": "tube",
            "_fit": fit, "_dims": dims, "_sleeves": [], "_band": band, "_support_face": "inner"}


def _ctx_generic(m: EngineeringModel, comp: Component, instance: Optional[int]) -> dict:
    bodies = _instances(m, comp)
    pick = m.bodies[instance] if instance is not None else (bodies[0] if bodies else None)
    comps = [{"key": comp.key, "role": "analysed part", "bodies": [pick.index] if pick else [],
              "modelled_as": "not rebuilt", "reason": "R1"}]
    return {"component": comp.key,
            "instance": {"body": pick.index if pick else None, "rule": "first instance", "text": ""},
            "components": comps, "loads": {}, "supports": [], "landing_in_load_path": False,
            "assumptions": ["No load-path rule for this component: loads and supports are not defined."],
            "materials": [{"component": comp.key, **_material_status(comp)}],
            "missing": ["load path / loads / supports rule for this component"], "unresolved": [],
            "geometry": {}, "fe_template": None}


def public_context(ctx: dict) -> dict:
    """JSON-friendly copy of a context (Q -> dict, private fields dropped)."""
    def conv(x):
        if isinstance(x, Q):
            return x.to_dict()
        if isinstance(x, dict):
            return {str(k): conv(v) for k, v in x.items() if not str(k).startswith("_")}
        if isinstance(x, (list, tuple)):
            return [conv(v) for v in x]
        if isinstance(x, np.ndarray):
            return x.tolist()
        if isinstance(x, (np.floating, np.integer)):
            return x.item()
        return x
    return conv(ctx)


# =========================================================================== geometry rebuild
def tube_solid(fit: dict, od: float, id_: float, length: float, stations: list[float]):
    """Exact tube (or rod, id_ = 0) on the fitted axis, centred on the fitted centre, made by revolving a
    profile whose outer and inner lines are split at ``stations`` (axial coordinates, mm) so that each
    segment is its own face (load / support regions)."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakePolygon
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeRevol
    from OCP.gp import gp_Ax1, gp_Dir, gp_Pnt
    a, c, e1 = fit["axis"], fit["centre"], fit["e1"]
    ro, ri = od / 2, id_ / 2
    s0, s1 = -length / 2, length / 2
    st = sorted({round(x, 6) for x in stations if s0 + 1e-3 < x < s1 - 1e-3})
    prof = [(ri, s0), (ro, s0)] + [(ro, x) for x in st] + [(ro, s1), (ri, s1)]
    if ri > 0:
        prof += [(ri, x) for x in reversed(st)]
    else:  # solid rod: close along the axis
        prof = [(0.0, s0), (ro, s0)] + [(ro, x) for x in st] + [(ro, s1), (0.0, s1)]
    poly = BRepBuilderAPI_MakePolygon()
    for r, s in prof:
        p = c + s * a + r * e1
        poly.Add(gp_Pnt(*map(float, p)))
    poly.Close()
    face = BRepBuilderAPI_MakeFace(poly.Wire(), True).Face()
    return BRepPrimAPI_MakeRevol(face, gp_Ax1(gp_Pnt(*map(float, c)), gp_Dir(*map(float, a))), 2 * math.pi).Shape()


def _classify_faces(body, fit: dict, ro: float, ri: float) -> list[dict]:
    """Axial range and side (outer / inner / end) of each face of the rebuilt tube."""
    out = []
    for f in body.faces:
        lo, hi = np.asarray(f.bbox[0]), np.asarray(f.bbox[1])
        corners = np.array([[x, y, z] for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2])])
        s = _s_of(corners, fit)
        # bbox corners overestimate the axial extent of a tilted face by up to r sin(tilt): fine for matching
        sc = float(_s_of(np.asarray(f.centroid)[None], fit)[0])
        if f.normal is not None and abs(abs(float(np.dot(f.normal, fit["axis"]))) - 1) < 1e-6:
            side = "end"
            ln = 0.0
        else:
            ln = float(s.max() - s.min())
            r_eff = f.area / (2 * math.pi * max(ln, 1e-9))
            side = "outer" if abs(r_eff - ro) <= abs(r_eff - ri) else "inner"
        out.append({"index": f.index, "side": side, "s_c": sc, "area": f.area})
    return out


def _faces_in(cls: list[dict], side: str, rng: list[float], tol: float = 0.05) -> list[int]:
    return [c["index"] for c in cls if c["side"] == side and rng[0] - tol <= c["s_c"] <= rng[1] + tol]


# =========================================================================== beam check
def beam_check(fit: dict, od: float, id_: float, E: float, G: float, root_s: float, outward: int,
               loads: list[dict], s_eval: float, s_defl: float, n: int = 4001) -> dict:
    """Cantilever along the tube axis from ``root_s`` towards ``outward`` (+1/-1), loaded by point forces
    (vector F at point P), couples and uniform line loads ({'q_vec', 's_range'}). Euler-Bernoulli
    curvature u'' = -(d x M) / EI plus the Timoshenko shear term u' = V / (k G A), k = 0.5 (thin tube).
    Units: N, mm, MPa. Returns bending stress at ``s_eval`` and at the root and the deflection vector at
    ``s_defl``."""
    a, c = fit["axis"], fit["centre"]
    d = a * outward
    I = math.pi / 64 * (od ** 4 - id_ ** 4)
    A = math.pi / 4 * (od ** 2 - id_ ** 2)
    k = 0.5 if id_ > 0 else 0.9
    Lmax = max(abs(s_defl - root_s), abs(s_eval - root_s))
    for ld in loads:
        if "point" in ld:
            Lmax = max(Lmax, float(np.dot(np.asarray(ld["point"]) - c, d) - root_s * outward))
        if "s_range" in ld:
            Lmax = max(Lmax, max(abs(x - root_s) for x in ld["s_range"]))
    t = np.linspace(0.0, Lmax, n)
    X = c[None, :] + np.outer(root_s + outward * t, a)
    M = np.zeros((n, 3))
    V = np.zeros((n, 3))
    for ld in loads:
        if ld["kind"] == "point":
            P, F = np.asarray(ld["point"], float), np.asarray(ld["F"], float)
            tp = float(np.dot(P - c, d) - root_s * outward)
            m_ = tp > t
            M[m_] += np.cross(P[None, :] - X[m_], F[None, :])
            V[m_] += F
        elif ld["kind"] == "couple":
            tp = ld["t"]
            M[tp > t] += np.asarray(ld["C"], float)
        elif ld["kind"] == "line":
            q = np.asarray(ld["q_vec"], float)  # N/mm
            ta, tb = sorted(abs(x - root_s) for x in ld["s_range"])
            lo = np.maximum(t, ta)
            ln = np.clip(tb - lo, 0, None)
            arm = 0.5 * (lo + tb) - t  # centroid of the remaining load from the section
            V += np.outer(ln, q)
            M += np.cross(np.outer(arm * ln, d), q[None, :])
    Mt = M - np.outer(M @ a, a)
    Vt = V - np.outer(V @ a, a)
    kap = -np.cross(d[None, :], Mt) / (E * I)
    dt = t[1] - t[0]
    slope = np.vstack([np.zeros(3), np.cumsum(0.5 * (kap[1:] + kap[:-1]) * dt, axis=0)])
    u_b = np.vstack([np.zeros(3), np.cumsum(0.5 * (slope[1:] + slope[:-1]) * dt, axis=0)])
    gam = Vt / (k * G * A)
    u_s = np.vstack([np.zeros(3), np.cumsum(0.5 * (gam[1:] + gam[:-1]) * dt, axis=0)])
    sig = np.linalg.norm(Mt, axis=1) * (od / 2) / I
    ie = int(np.argmin(np.abs(t - abs(s_eval - root_s))))
    idf = int(np.argmin(np.abs(t - abs(s_defl - root_s))))
    return {"I_mm4": I, "A_mm2": A, "E_MPa": E, "G_MPa": G, "k_shear": k,
            "sigma_root_MPa": float(sig[0]), "M_root_Nmm": float(np.linalg.norm(Mt[0])),
            "sigma_eval_MPa": float(sig[ie]), "u_bending_mm": u_b[idf].tolist(), "u_shear_mm": u_s[idf].tolist(),
            "u_total_mm": (u_b[idf] + u_s[idf]).tolist(),
            "defl_bending_mm": float(np.linalg.norm(u_b[idf])),
            "defl_total_mm": float(np.linalg.norm(u_b[idf] + u_s[idf]))}


# =========================================================================== FE run
def _ip_stress(dat: Path) -> dict:
    """Integration-point stresses from the CalculiX .dat (*EL PRINT). With an *ORIENTATION the lines carry
    the orientation name and the stresses are in the material frame (axis 3 = tube axis); von Mises is
    frame-independent. Read here because the lines with the extra column are skipped by the generic reader."""
    vals = []
    try:
        on = False
        with open(dat, errors="replace") as fh:
            for ln in fh:
                if "stresses (elem, integ.pnt." in ln:
                    on = True
                    continue
                if on:
                    tok = ln.split()
                    if not tok:
                        continue
                    if len(tok) < 8 or not tok[0].isdigit():
                        if vals:
                            on = False
                        continue
                    vals.append([float(x) for x in tok[2:8]])
    except OSError:
        return {}
    if not vals:
        return {}
    S = np.asarray(vals)  # sxx, syy, szz, sxy, sxz, syz (material frame when oriented)
    from opencae.postprocessing.results import von_mises
    vm = von_mises(S[:, [0, 1, 2, 3, 5, 4]])
    return {"n": int(len(S)), "vm_max": float(vm.max()), "axial_abs_max": float(np.abs(S[:, 2]).max())}


def _patch_orientation(inp: Path, axis: np.ndarray, e1: np.ndarray) -> bool:
    """Material axis 3 = tube axis in the CalculiX deck (OpenCAE writes the global Z axis for orthotropic
    materials without print settings; print settings would apply FDM homogenisation)."""
    from opencae.solver.calculix.deck import _fmt, orientation_vectors
    txt = inp.read_text()
    a, b = orientation_vectors(axis, e1)
    new = ", ".join(_fmt(v) for v in list(a) + list(b))
    txt2, n = re.subn(r"(\*ORIENTATION, NAME=PRINT1, SYSTEM=RECTANGULAR\n)[^\n]*\n", lambda mm: mm.group(1) + new + "\n",
                      txt)
    if n:
        inp.write_text(txt2)
    return bool(n)


def run_component_analysis(model: EngineeringModel, component_key: str, workdir, case: Optional[str] = None,
                           element_size: Optional[float] = None, instance: Optional[int] = None,
                           threads: int = 2, load_factor: float = 4.0) -> dict:
    """Rebuild, mesh and solve the component for one load case of :func:`analysis_context`.

    Cases: motor tube ``max_thrust`` (default) | ``landing``; spar tube ``lift`` (default). Returns a dict with
    stresses (MPa), displacements (mm), equilibrium, beam check, safety factor (with evidence classes),
    mesh statistics, solver log path, warnings and provenance hashes."""
    from opencae.app.session import Session
    from opencae.examples.tiltwing_geometry import write_step
    from opencae.geometry.references import make_ref
    from opencae.project.model import Constraint, Load, MaterialAssignment
    from opencae.solver.calculix.runner import run_blocking

    t_start = time.time()
    ctx = analysis_context(model, component_key, instance=instance, load_factor=load_factor)
    if ctx.get("fe_template") != "tube":
        raise NotImplementedError(f"no FE template for '{ctx['component']}': " + "; ".join(ctx["missing"]))
    case = case or next(iter(ctx["loads"]))
    if case not in ctx["loads"]:
        raise ValueError(f"case '{case}' not available (cases: {list(ctx['loads'])})")
    lds = [ld for ld in ctx["loads"][case] if ld["kind"] != "info"]
    if not lds:
        raise ValueError(f"case '{case}' has no loads for this component")
    fit, dims = ctx["_fit"], ctx["_dims"]
    od, id_, L = dims["od"], dims.get("id", 0.0), dims["length"]
    # the part is centred on the mesh body's axial mid-point; stations relative to that
    band = ctx["_band"]
    support_face = ctx.get("_support_face", "outer")
    stations = list(band) + [x for ld in lds for x in ld.get("s_range", [])]
    shape = tube_solid(fit, od, id_, L, stations)
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^A-Za-z0-9]+", "_", ctx["component"].split(" — ")[0]).strip("_").lower()
    s = Session()
    s.new(f"{ctx['component']} - {case}", "mm-N-s")
    s.project.description = (f"K09-L component analysis ({ctx['instance'].get('text', '')}), case '{case}'. "
                             "Solid rebuilt from the part-name dimensions on the axis fitted to the Fusion mesh body. "
                             f"Engineering-model config {model.config_hash()}, geometry {model.geometry_hash()}.")
    path = s.save(workdir / f"{slug}_{case}.ocae")
    src = s.data_dir / "geometry" / "source"
    src.mkdir(parents=True, exist_ok=True)
    step = write_step([shape], src / f"{slug}.step")
    f = s.import_geometry(step)
    bid = f.id
    s.project.geometry.body_names[bid] = ctx["instance"].get("text") or ctx["component"]
    s.regenerate()
    body = s.bodies[bid]
    vol = body.volume
    comp = model.components[ctx["component"]]
    mat = cf_tube_material(model, comp, vol)
    s.project.materials.append(mat)
    st = s.study
    st.name = f"{ctx['component'].split(' — ')[0]} - {case}"
    st.mesh_settings.body = bid
    st.assignments = [MaterialAssignment(bid, mat.id)]
    h = element_size or round(min(2.0, max(0.8, (od - id_) / 2 * 1.4 if id_ else od / 6)), 2)
    ms = st.mesh_settings
    ms.size_max, ms.size_min, ms.element_order = h, 0.5 * h, 2
    st.analysis.threads = threads
    st.analysis.write_ip_stress = True
    cls = _classify_faces(body, fit, od / 2, id_ / 2)
    sup_faces = _faces_in(cls, support_face, band)
    if not sup_faces:
        raise RuntimeError("support faces not found on the rebuilt solid")
    st.constraints = [Constraint("fixed", [make_ref(body, "face", i) for i in sup_faces],
                                 name=ctx["supports"][0]["name"])]
    loads, applied_F, applied_M = [], np.zeros(3), np.zeros(3)
    beam_loads = []
    for ld in lds:
        faces = _faces_in(cls, "outer", ld["s_range"])
        if not faces:
            raise RuntimeError(f"load region of '{ld['name']}' not found on the rebuilt solid")
        refs = [make_ref(body, "face", i) for i in faces]
        dvec = np.asarray(ld["direction"], float)
        dvec /= np.linalg.norm(dvec)
        if ld["kind"] == "remote_force":
            Fv = ld["value_N"] * dvec
            loads.append(Load("remote_force", refs, {"magnitude": ld["value_N"], "direction": dvec.tolist(),
                                                     "point": list(map(float, ld["point_mm"]))}, name=ld["name"]))
            applied_F += Fv
            applied_M += np.cross(np.asarray(ld["point_mm"], float), Fv)
            beam_loads.append({"kind": "point", "point": ld["point_mm"], "F": Fv, "s_range": ld["s_range"]})
        elif ld["kind"] == "moment":
            C = ld["value_Nmm"] * dvec
            loads.append(Load("moment", refs, {"magnitude": ld["value_Nmm"], "direction": dvec.tolist()},
                              name=ld["name"]))
            applied_M += C
            beam_loads.append({"kind": "couple", "C": C, "s_range": ld["s_range"]})
        elif ld["kind"] == "force":
            Fv = ld["value_N"] * dvec
            loads.append(Load("force", refs, {"magnitude": ld["value_N"], "direction": dvec.tolist()},
                              name=ld["name"]))
            applied_F += Fv
            sa, sb = ld["s_range"]
            mid = fit["centre"] + fit["axis"] * 0.5 * (sa + sb)
            applied_M += np.cross(mid, Fv)
            beam_loads.append({"kind": "line", "q_vec": Fv / (sb - sa), "s_range": ld["s_range"]})
    st.loads = loads
    st.notes = "\n".join(ctx["assumptions"])
    s.save()
    t0 = time.time()
    s.generate_mesh()
    t_mesh = time.time() - t0
    job_dir, jobname, issues = s.prepare_solve()
    errs = [str(i) for i in issues if i.severity == "error"]
    if not jobname:
        raise RuntimeError(f"model validation failed: {errs}")
    patched = _patch_orientation(job_dir / f"{jobname}.inp", fit["axis"], fit["e1"])
    t0 = time.time()
    out = run_blocking(s.backend.install, job_dir, jobname, threads, None, None)
    t_solve = time.time() - t0
    if not out.success:
        raise RuntimeError(f"CalculiX failed: {out.errors}; log {job_dir / 'solver.log'}")
    rs = s.load_results(job_dir, jobname, out.wall_time)
    s.save()
    sm = s.project.results.summary
    mesh = s.mesh
    X = mesh.nodes
    a = fit["axis"]
    sN = _s_of(X, fit)
    rN = _radial(X, fit)
    U = rs.displacement
    S = rs.stress
    Sm = np.stack([np.stack([S[:, 0], S[:, 3], S[:, 5]], -1), np.stack([S[:, 3], S[:, 1], S[:, 4]], -1),
                   np.stack([S[:, 5], S[:, 4], S[:, 2]], -1)], -2)
    s_ax = np.einsum("i,nij,j->n", a, Sm, a)  # axial (fibre-direction) stress
    vm = rs.quantity("S_vm")
    # ---------------------------------------------------------------- equilibrium
    # CalculiX RF is K u at every node (non-zero at loaded nodes too): only the support nodes carry reactions
    sup_nodes = np.unique(np.concatenate([np.unique(mesh.face_tris[i]) for i in sup_faces if i in mesh.face_tris]))
    rt = rs.reaction_totals or {}
    R_tot = next((np.asarray(v, float) for k, v in rt.items() if "ALL_SUPPORTS" in k.upper()), None)
    M_R = None
    if rs.reaction is not None:
        R_nodes = rs.reaction[sup_nodes]
        R = R_nodes.sum(axis=0)
        M_R = np.cross(X[sup_nodes], R_nodes).sum(axis=0)
    else:
        R = R_tot if R_tot is not None else np.full(3, np.nan)
    fref = max(np.linalg.norm(applied_F), 1e-12)
    eq = {"applied_force_N": applied_F.tolist(), "reaction_force_N": R.tolist(),
          "reaction_force_ccx_totals_N": R_tot.tolist() if R_tot is not None else None,
          "force_residual_pct": float(100 * np.linalg.norm(R + applied_F) / fref),
          "convention": "equilibrium: reactions + applied loads = 0 (forces and moments about the origin)"}
    if M_R is not None:
        mref = max(np.linalg.norm(applied_M), fref * L)
        eq.update({"applied_moment_Nmm": applied_M.tolist(), "reaction_moment_Nmm": M_R.tolist(),
                   "moment_residual_pct": float(100 * np.linalg.norm(M_R + applied_M) / mref)})
    # ---------------------------------------------------------------- beam check per cantilever side
    E = mat.orthotropic["E3"] / 1e6
    G = mat.orthotropic["G13"] / 1e6
    outer = rN > od / 2 - 0.05 * (od - id_ if id_ else od)
    sides = []
    for outward, root in ((+1, band[1]), (-1, band[0])):
        mine = [bl for bl in beam_loads if (np.mean(bl["s_range"]) - root) * outward > 0]
        if not mine:
            continue
        near = min((min(bl["s_range"], key=lambda x: abs(x - root)) for bl in mine), key=lambda x: abs(x - root))
        far_end = fit["s_max"] if outward > 0 else fit["s_min"]
        is_line = all(bl["kind"] == "line" for bl in mine)
        s_defl = far_end if is_line else near  # sleeve start (loads introduced beyond it) or the free tip
        s_eval = root + outward * 1.5 * od
        bc_loads = []
        for bl in mine:
            if bl["kind"] == "couple":
                t_c = abs(np.mean(bl["s_range"]) - root)
                bc_loads.append({"kind": "couple", "C": bl["C"], "t": t_c})
            else:
                bc_loads.append(bl)
        bc = beam_check(fit, od, id_, E, G, root, outward, bc_loads, s_eval, s_defl)
        slab = max(0.6 * h, 0.5)
        sel_e = outer & (np.abs(sN - s_eval) < slab)
        sel_d = np.abs(sN - s_defl) < slab
        u_fe = U[sel_d].mean(axis=0)
        u_fe_t = u_fe - (u_fe @ a) * a
        sig_fe = float(np.abs(s_ax[sel_e]).max()) if np.any(sel_e) else float("nan")
        half = (sN - root) * outward > 0
        excl = half & ((sN - root) * outward > 0.5 * od)
        sides.append({
            "side": "+axis" if outward > 0 else "-axis", "root_s_mm": root, "eval_s_mm": s_eval,
            "deflection_s_mm": s_defl, "deflection_at": "free tip" if is_line else "inboard edge of the load sleeve",
            "beam": bc,
            "fe_sigma_axial_eval_MPa": sig_fe,
            "fe_deflection_mm": float(np.linalg.norm(u_fe_t)), "fe_deflection_vec_mm": u_fe_t.tolist(),
            "fe_sigma_axial_peak_MPa": float(np.abs(s_ax[half]).max()),
            "fe_sigma_axial_peak_outside_clamp_zone_MPa": float(np.abs(s_ax[excl]).max()) if np.any(excl) else None,
            "diff_sigma_pct": 100 * (sig_fe / bc["sigma_eval_MPa"] - 1) if bc["sigma_eval_MPa"] > 0 else None,
            "diff_defl_vs_EB_pct": 100 * (np.linalg.norm(u_fe_t) / bc["defl_bending_mm"] - 1)
            if bc["defl_bending_mm"] > 0 else None,
            "diff_defl_vs_EB_shear_pct": 100 * (np.linalg.norm(u_fe_t) / bc["defl_total_mm"] - 1)
            if bc["defl_total_mm"] > 0 else None})
    # ---------------------------------------------------------------- strength (evidence classes)
    mq = _material_q(mat)
    load_q = {ld["name"]: ld["q"] for ld in lds}
    sig_gov = max(max(sd["beam"]["sigma_root_MPa"] for sd in sides),
                  max((sd["fe_sigma_axial_peak_outside_clamp_zone_MPa"] or 0.0) for sd in sides))
    ins = {**load_q, "E": mq["E_axial"]}
    sf = derived(mat.yield_strength / 1e6 / sig_gov, "-",
                 "estimated axial allowable / max(beam bending stress at the clamp edge, FE axial stress outside "
                 "0.5 OD of the clamp)", {**ins, "allow": mq["allowable"]}, kind=SIMULATION)
    sf_peak = derived(mat.yield_strength / 1e6 / max(sd["fe_sigma_axial_peak_MPa"] for sd in sides), "-",
                      "estimated allowable / FE peak axial stress incl. the rigid-clamp edge (lower bound)",
                      {**ins, "allow": mq["allowable"]}, kind=SIMULATION)
    ip = _ip_stress(job_dir / f"{jobname}.dat")
    ivm = int(np.argmax(vm))
    iu = int(np.argmax(np.linalg.norm(U, axis=1)))
    log_path = job_dir / "solver.log"
    warn = list(out.warnings)
    try:
        warn += [ln.strip() for ln in log_path.read_text(errors="replace").splitlines()
                 if "*WARNING" in ln.upper() and ln.strip() not in warn][:20]
    except OSError:
        pass
    warn += [str(i) for i in issues if i.severity != "error" and not (patched and "print orientation" in str(i))]
    if patched:
        warn.append("material axis 3 set to the tube axis in the CalculiX deck (*ORIENTATION) - replaces the "
                    "global-Z default that OpenCAE warns about for orthotropic materials")
    geo_params = {"od": od, "id": id_, "length": L, "axis": np.round(fit["axis"], 6).tolist(),
                  "centre": np.round(fit["centre"], 3).tolist(), "stations": sorted(round(x, 3) for x in stations)}
    res = {
        "component": ctx["component"], "instance": ctx["instance"], "case": case,
        "project": str(path), "step_file": str(step),
        "config_hash": model.config_hash(), "geometry_hash": model.geometry_hash(),
        "fe_geometry_hash": hashlib.sha1(json.dumps(geo_params, sort_keys=True).encode()).hexdigest()[:12],
        "fe_geometry": geo_params, "geometry_check": ctx["geometry"],
        "loads": [{k: (v.to_dict() if isinstance(v, Q) else v) for k, v in ld.items()} for ld in lds],
        "supports": ctx["supports"], "assumptions": ctx["assumptions"],
        "material": {"name": mat.name, "E_axial_MPa": E, "G_axial_MPa": G, "density": mat.density,
                     "allowable_MPa": mat.yield_strength / 1e6, "class": ESTIMATE, "source": mat.source,
                     "estimated": list(mat.estimated)},
        "converged": bool(out.success), "solver_returncode": out.returncode, "solver_log": str(log_path),
        "solver_errors": list(out.errors), "warnings": warn, "orientation_patched": patched,
        "max_von_mises_nodal_MPa": float(sm.get("max_von_mises_nodal", vm.max())),
        "max_von_mises_integration_point_MPa": sm.get("max_von_mises_integration_point") or ip.get("vm_max"),
        "max_axial_stress_integration_point_MPa": ip.get("axial_abs_max"),
        "integration_points": ip.get("n", 0),
        "max_von_mises_location_mm": X[ivm].tolist(), "max_von_mises_s_mm": float(sN[ivm]),
        "max_axial_stress_MPa": float(np.abs(s_ax).max()),
        "max_axial_stress_s_mm": float(sN[int(np.argmax(np.abs(s_ax)))]),
        "max_displacement_mm": float(np.linalg.norm(U[iu])), "max_displacement_location_mm": X[iu].tolist(),
        "max_displacement_s_mm": float(sN[iu]),
        "equilibrium": eq, "beam_check": sides,
        "safety_factor": sf.to_dict(), "safety_factor_peak": sf_peak.to_dict(),
        "governing_axial_stress_MPa": sig_gov,
        "mesh": {**{k: v for k, v in mesh.stats().items() if not isinstance(v, (list, np.ndarray))},
                 "element_size_mm": h, "dof": int(3 * mesh.n_nodes)},
        "timing_s": {"mesh": t_mesh, "solve": t_solve, "ccx_wall": out.wall_time, "total": time.time() - t_start},
        "note": ("FE stresses rest on estimated CF properties and (for thrust) provisional propeller coefficients: "
                 "class " + sf.source + ". Von Mises is reported for reference only; the axial stress governs a "
                 "CF tube."),
    }
    rq = next((r for r in model.requirements if r.metric == "motor_tube_sf"), None)
    if rq is not None and ctx["component"].startswith("Motor tube"):
        res["requirement"] = {"id": rq.id, "text": rq.text, "threshold": rq.threshold.value,
                              "met": bool(sf.value >= rq.threshold.value), "class": sf.source,
                              "note": "screening result on estimated material data - not a verification"}
    (workdir / f"{slug}_{case}_results.json").write_text(json.dumps(res, indent=1, default=float))
    return res
