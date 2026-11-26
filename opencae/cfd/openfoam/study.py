"""Turn an OpenCAE flow study into an OpenFOAM case (geometry, patches, mesh controls, physics).

Conditions are taken from the study (velocity inlet, pressure outlet, wall,
slip wall; faces without a condition are no-slip walls). Mesh controls come from the study's
mesh settings: ``size_max`` = background cell size, local refinements = surface refinement
levels (size / 2^level), ``bl_layers`` / ``bl_first`` / ``bl_growth`` = prism layers on walls.
Flow quantities are SI; geometry is scaled to metres.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

import numpy as np

from opencae.boundary_conditions.resolve import ERROR, WARNING, Issue
from opencae.cfd.openfoam.case import FoamSetup, Patch, fine_tessellation, refinement_level, write_case

FLOW_KINDS = {"velocity_inlet": "inlet", "pressure_outlet": "outlet", "wall": "wall", "slip_wall": "slip"}


def _name(s: str, used: set) -> str:
    n = re.sub(r"[^A-Za-z0-9_]", "_", s).strip("_") or "patch"
    if n[0].isdigit():
        n = "p_" + n
    base, k = n, 2
    while n in used or n == "background":
        n = f"{base}_{k}"
        k += 1
    used.add(n)
    return n


def inside_point(body, rng_seed: int = 7) -> Optional[np.ndarray]:
    """A point strictly inside the (fluid) solid, away from its boundary (project units)."""
    from OCP.BRepClass3d import BRepClass3d_SolidClassifier
    from OCP.TopAbs import TopAbs_IN
    from OCP.gp import gp_Pnt
    from OCP.BRepExtrema import BRepExtrema_DistShapeShape
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeVertex
    lo, hi = (np.asarray(x, float) for x in body.bbox)
    rng = np.random.default_rng(rng_seed)
    best, best_d = None, -1.0
    for _ in range(400):
        p = lo + rng.random(3) * (hi - lo)
        cl = BRepClass3d_SolidClassifier(body.shape, gp_Pnt(*map(float, p)), 1e-7)
        if cl.State() != TopAbs_IN:
            continue
        d = BRepExtrema_DistShapeShape(BRepBuilderAPI_MakeVertex(gp_Pnt(*map(float, p))).Vertex(), body.shape)
        dist = d.Value() if d.IsDone() else 0.0
        if dist > best_d:
            best, best_d = p, dist
        if best_d > 0.05 * body.size:
            break
    return best


def build(session, case_dir: Optional[Path], iterations: Optional[int] = None,
          write: bool = True) -> tuple[Optional[FoamSetup], list[Issue]]:
    """Write the OpenFOAM case of the active flow study into ``case_dir`` (``write=False``: only
    check the conditions)."""
    p = session.project
    a = p.analysis
    ms = p.mesh_settings
    issues: list[Issue] = []

    def add(sev, msg, src=""):
        issues.append(Issue(sev, msg, src))

    bodies = session.analysis_bodies()
    if len(bodies) != 1:
        add(ERROR, "OpenFOAM flow studies need exactly one fluid body (use the external flow domain for flow "
                   "around parts)", "Geometry")
        return None, issues
    if a.cfd_dimension == "2d":
        add(ERROR, "2-D flow is not available with OpenFOAM yet; use 3-D", "Analysis")
        return None, issues
    body = bodies[0]
    sc = 1.0 / session.units.length_from_si  # project length -> m
    face_patch: dict[int, str] = {}
    patches: list[Patch] = []
    used: set = set()
    for it in [c for c in p.constraints if not c.suppressed] + [l for l in p.loads if not l.suppressed]:
        kind = FLOW_KINDS.get(it.kind)
        if kind is None:
            add(WARNING, f"'{it.kind}' does not act in a flow analysis and is ignored", it.name)
            continue
        faces = []
        for r in it.targets:
            if r.kind != "face" or r.body != body.id:
                add(ERROR, f"{r.label()}: flow conditions need faces of the fluid body", it.name)
                continue
            if r.index in face_patch:
                add(ERROR, f"face {r.index + 1} has two flow conditions", it.name)
                continue
            faces.append(r.index)
        if not faces:
            continue
        pt = Patch(_name(it.name, used), kind, faces)
        if kind == "inlet":
            speed = float(it.params.get("magnitude", 0.0))
            d = it.params.get("direction", "normal")
            if isinstance(d, str):  # normal: into the fluid = - outward normal of a planar face
                ns = [body.faces[i].normal for i in faces]
                if any(n is None for n in ns):
                    add(ERROR, "a 'normal' inlet direction needs planar faces; give a vector", it.name)
                    continue
                n = -np.mean(ns, axis=0)
                n /= np.linalg.norm(n)
            else:
                n = np.asarray(d, float) / np.linalg.norm(d)
            pt.velocity = list(map(float, speed * n))
        if kind == "outlet":
            pt.pressure = float(it.params.get("magnitude", 0.0))
        patches.append(pt)
        for i in faces:
            face_patch[i] = pt.name
    free = [f.index for f in body.faces if f.index not in face_patch]
    if free:
        patches.append(Patch(_name("walls", used), "wall", free))
        add(WARNING, f"{len(free)} face(s) without a flow condition are no-slip walls", "Flow")
    if not any(pt.kind == "inlet" for pt in patches):
        add(ERROR, "no velocity inlet", "Flow")
    if not any(pt.kind == "outlet" for pt in patches):
        add(ERROR, "no pressure outlet", "Flow")
    if a.fluid_density <= 0 or a.fluid_viscosity <= 0:
        add(ERROR, "fluid density and viscosity must be > 0", "Analysis")
    # mesh controls
    hb = (ms.size_max or body.size / 20.0)
    for pt in patches:
        sizes = [r.size for r in ms.refinements
                 if any(t.kind == "face" and t.body == body.id and t.index in pt.faces for t in r.targets)]
        pt.level = refinement_level(hb, min(sizes)) if sizes else (1 if pt.kind == "wall" else 0)
    # snappyHexMesh layer heuristics: stated as warnings, never adjusted silently
    if int(ms.bl_layers or 0) and float(ms.bl_first or 0) > 0:
        n, g, t1 = int(ms.bl_layers), float(ms.bl_growth or 1.2), float(ms.bl_first)
        total = t1 * n if abs(g - 1) < 1e-9 else t1 * (g ** n - 1) / (g - 1)
        for pt in patches:
            if pt.kind != "wall":
                continue
            cell = hb / 2 ** pt.level
            if t1 < 0.05 * cell:
                add(WARNING, f"{pt.name}: first layer {t1:.3g} is under 5% of the wall cell size {cell:.3g}; "
                             "snappyHexMesh typically adds few or no layers then (coverage is reported after "
                             "meshing). Use a thicker first layer or a finer wall refinement level", "Mesh")
            if total > cell:
                add(WARNING, f"{pt.name}: the {n} layers are {total:.3g} thick, more than the wall cell size "
                             f"{cell:.3g}; snappyHexMesh shrinks or drops such layers. Use fewer layers or "
                             "a lower growth rate", "Mesh")
    if any(e.severity == ERROR for e in issues) or not write:
        return None, issues
    loc = inside_point(body)
    if loc is None:
        add(ERROR, "could not find a point inside the fluid body (is it a closed solid?)", "Geometry")
        return None, issues
    # STL resolution: chordal deflection well below the finest cell size
    finest = hb / 2 ** max([pt.level for pt in patches] + [0])
    P, T, F = fine_tessellation(body.shape, min(finest / 20.0, body.size * 1e-3))
    surfaces = {}
    for pt in patches:
        sel = np.isin(F, pt.faces)
        if not sel.any():
            add(ERROR, f"patch {pt.name}: no surface triangles", "Mesh")
            continue
        surfaces[pt.name] = (P * sc, T[sel])
    nu = a.fluid_viscosity / a.fluid_density
    V = a.ref_velocity or max([float(np.linalg.norm(pt.velocity)) for pt in patches if pt.velocity] + [0.0])
    setup = FoamSetup(scale=sc, background_size=hb * sc, patches=patches, location_in_mesh=list(loc * sc),
                      bbox=(np.asarray(body.bbox[0]) * sc, np.asarray(body.bbox[1]) * sc), nu=nu,
                      rho=a.fluid_density, model={"laminar": "laminar", "sa": "sa", "sst": "sst"}.get(a.flow_model,
                                                                                                    "laminar"),
                      iterations=int(iterations or a.cfd_iterations),
                      residual=10 ** (a.cfd_residual + 1) if a.cfd_residual < -1 else 1e-5,
                      turbulence_intensity=a.turbulence_intensity,
                      viscosity_ratio=a.turbulent_viscosity_ratio,
                      layers=int(ms.bl_layers or 0), first_layer=float(ms.bl_first or 0.0) * sc,
                      expansion=float(ms.bl_growth or 1.2), ref_area=a.ref_area, ref_velocity=V,
                      threads=max(1, int(a.threads or 1)))
    if setup.layers and setup.first_layer <= 0:
        add(ERROR, "wall layers need a first-layer thickness (Mesh controls)", "Mesh")
        return None, issues
    write_case(case_dir, setup, surfaces)
    return setup, issues
