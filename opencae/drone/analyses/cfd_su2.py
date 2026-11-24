"""CFD with SU2: steady incompressible flow around the WHOLE aircraft on a tetrahedral gmsh mesh.

This is the route that runs on a PC without OpenFOAM (OpenFOAM needs WSL on Windows). It follows the same
conventions as the OpenFOAM path in :mod:`opencae.drone.analyses.cfd` (whose helpers it imports, not copies):
frame x forward, y left, z up; the air comes from the nose (freestream U = V (-cos a, 0, sin a)); both sides are
meshed (no symmetry plane); propellers are left out; lift is +z, drag is along the flow.

Pipeline: every non-prop part is turned into a sewn OCC solid (OCP), all of them are fused into ONE watertight
body (the arms overlap the frame), gmsh subtracts that body from a box of air and fills it with tetrahedra
(fine near the body, coarse far away), the mesh is written in SU2 format with two markers (``body`` wall and
``farfield``), SU2_CFD solves the incompressible RANS equations (Spalart-Allmaras) and the surface solution is
integrated on the body triangles for lift, drag and Cp.

Limits, stated in the result summary: coarse tetrahedral mesh without boundary-layer prisms, so skin friction and
separation are only roughly resolved and drag is indicative only.
"""
from __future__ import annotations

import math
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional

import numpy as np

from opencae.drone import types as T

MM = 1e-3
BODY, FARFIELD = "body", "farfield"
# preset: target tetrahedra, growth of the cell size away from the body (size gain per unit distance)
PRESETS = {"coarse": {"cells": 150_000, "growth": 0.35},
           "medium": {"cells": 400_000, "growth": 0.30},
           "fine": {"cells": 1_000_000, "growth": 0.25}}
HEXLESS_FACTOR = 0.62  # gmsh builds about 0.62 x (size-field volume integral / regular-tet volume) tetrahedra (measured)
RHO_DEFAULT = 1.225
NU_AIR = 1.5e-5  # m2/s; mu = rho nu
TET_VOL = 1.0 / (6.0 * math.sqrt(2.0))  # volume of a regular tetrahedron of edge 1
MODEL_TEXT = "SU2 incompressible RANS (Spalart-Allmaras)"


class MeshError(RuntimeError):
    """Geometry or meshing failed; the message is the reason shown to the user."""


# ------------------------------------------------------------------------------------------------ geometry
def _part_solid(name: str, points: np.ndarray, tris: np.ndarray):
    """One part -> a sewn OCC solid from its triangles (mm)."""
    from OCP.BRep import BRep_Builder
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakePolygon, BRepBuilderAPI_Sewing
    from OCP.gp import gp_Pnt
    from OCP.ShapeFix import ShapeFix_Solid
    from OCP.TopAbs import TopAbs_SHELL
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS_Compound
    from opencae.geometry._occ import to_shell
    diag = float(np.linalg.norm(points.max(0) - points.min(0)))
    sew = BRepBuilderAPI_Sewing(max(1e-6 * diag, 1e-6))
    n = 0
    for t in tris:
        a, b, c = (points[i] for i in t)
        if np.linalg.norm(np.cross(b - a, c - a)) < 1e-10 * max(diag, 1.0) ** 2:
            continue  # degenerate triangle
        mp = BRepBuilderAPI_MakePolygon(gp_Pnt(*map(float, a)), gp_Pnt(*map(float, b)), gp_Pnt(*map(float, c)), True)
        mf = BRepBuilderAPI_MakeFace(mp.Wire(), True)
        if mf.IsDone():
            sew.Add(mf.Face())
            n += 1
    if n < 4:
        raise MeshError(f"part {name} has too few usable triangles to make a solid")
    sew.Perform()
    shape = sew.SewedShape()
    ex = TopExp_Explorer(shape, TopAbs_SHELL)
    if not ex.More():
        raise MeshError(f"part {name} could not be sewn into a closed shell")
    shells = []
    while ex.More():
        shells.append(to_shell(ex.Current()))
        ex.Next()
    fixer = ShapeFix_Solid()
    comp = TopoDS_Compound()
    bld = BRep_Builder()
    bld.MakeCompound(comp)
    for sh in shells:
        sol = fixer.SolidFromShell(sh)
        if sol.IsNull():
            raise MeshError(f"part {name} is not watertight (its surface does not close)")
        bld.Add(comp, sol)
    return comp


def _volume(shape) -> float:
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps
    g = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, g)
    return float(g.Mass())


def _unify(shape):
    from OCP.ShapeUpgrade import ShapeUpgrade_UnifySameDomain
    u = ShapeUpgrade_UnifySameDomain(shape, True, True, False)
    u.Build()
    return u.Shape()


def build_body(parts) -> tuple:
    """Fuse the parts into one watertight body. Returns (OCC shape, info dict). Raises MeshError."""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Fuse
    from OCP.BRepCheck import BRepCheck_Analyzer
    from opencae.geometry._occ import ListOfShape
    solids, vols = [], {}
    for p in parts:
        try:
            s = _unify(_part_solid(p.name, np.asarray(p.points, float), np.asarray(p.triangles)))
        except MeshError:
            raise
        except Exception as exc:
            raise MeshError(f"part {p.name} could not be turned into a solid: {exc}") from exc
        v = _volume(s)
        if v < 0:  # inside-out shell
            raise MeshError(f"part {p.name} has inward-facing triangles (negative volume); repair the CAD part")
        if v <= 0:
            raise MeshError(f"part {p.name} has no volume (open or flat surface)")
        solids.append(s)
        vols[p.name] = v
    if len(solids) == 1:
        body = solids[0]
    else:
        args, tools = ListOfShape(), ListOfShape()
        args.Append(solids[0])
        for s in solids[1:]:
            tools.Append(s)
        op = BRepAlgoAPI_Fuse()
        op.SetArguments(args)
        op.SetTools(tools)
        op.SetRunParallel(True)
        op.SetFuzzyValue(1e-4)
        op.Build()
        has_err = getattr(op, "HasErrors", lambda: False)()
        if has_err or not op.IsDone() or op.Shape().IsNull():
            raise MeshError("the boolean union of the parts failed (the parts overlap in a way the CAD kernel "
                            "cannot fuse); check the CAD for self-intersecting or non-watertight parts")
        body = op.Shape()
    body = _unify(body)
    if not BRepCheck_Analyzer(body).IsValid():
        raise MeshError("the fused aircraft body is not a valid solid (non-watertight or self-intersecting parts)")
    vol = _volume(body)
    if vol <= 0:
        raise MeshError("the fused aircraft body has no volume")
    return body, {"volume_mm3": vol, "part_volumes_mm3": vols, "parts": [p.name for p in parts]}


# ------------------------------------------------------------------------------------------------ sizing
def size_function(d: np.ndarray, hb: float, hfar: float, growth: float) -> np.ndarray:
    return np.minimum(hfar, hb + growth * d)


def estimate_cells(samples: np.ndarray, vol_total: float, surf: np.ndarray, wake: tuple, hb: float, hfar: float,
                   growth: float, wake_factor: float = 2.5) -> float:
    """Tetrahedra the size field asks for: integral of dV / (regular tet volume at the local size)."""
    from scipy.spatial import cKDTree
    d, _ = cKDTree(surf).query(samples)
    h = size_function(d, hb, hfar, growth)
    lo, hi = wake
    inw = np.all((samples >= lo) & (samples <= hi), axis=1)
    h = np.where(inw, np.minimum(h, wake_factor * hb), h)
    return float(vol_total / len(samples) * np.sum(1.0 / (TET_VOL * h ** 3)))


def choose_sizes(parts, dom_mm: dict, target: int, growth: float, factor: float = HEXLESS_FACTOR) -> dict:
    """Body cell size (mm) and far-field size (mm) that give about ``target`` tetrahedra."""
    P = np.vstack([p.points for p in parts])
    lo, hi = P.min(0), P.max(0)
    Lc = dom_mm["Lc"]
    rng = np.random.default_rng(1)
    # surface sample points of the (unfused) triangles, area weighted
    pts = []
    for p in parts:
        v = np.asarray(p.points, float)[np.asarray(p.triangles)]
        a = 0.5 * np.linalg.norm(np.cross(v[:, 1] - v[:, 0], v[:, 2] - v[:, 0]), axis=1)
        if a.sum() <= 0:
            continue
        k = max(200, int(4000 * a.sum() / max(1.0, a.sum())))
        idx = rng.choice(len(v), size=min(k, 6000), p=a / a.sum())
        r1, r2 = rng.random((len(idx), 1)), rng.random((len(idx), 1))
        s = np.sqrt(r1)
        pts.append(v[idx, 0] * (1 - s) + v[idx, 1] * (s * (1 - r2)) + v[idx, 2] * (s * r2))
    surf = np.vstack(pts)
    x0, x1 = dom_mm["x"]
    y0, y1 = dom_mm["y"]
    z0, z1 = dom_mm["z"]
    vol_total = (x1 - x0) * (y1 - y0) * (z1 - z0)
    S = np.column_stack([rng.uniform(x0, x1, 120_000), rng.uniform(y0, y1, 120_000), rng.uniform(z0, z1, 120_000)])
    L = dom_mm["L"]
    wake = (np.array([lo[0] - 2.0 * L, lo[1] - 0.05 * Lc, lo[2] - 0.1 * Lc]),
            np.array([lo[0], hi[1] + 0.05 * Lc, hi[2] + 0.1 * Lc]))
    hfar = 0.5 * Lc
    lo_h, hi_h = 1e-4 * Lc, 0.5 * Lc
    for _ in range(40):  # bisection on log(hb): cell count falls as hb grows
        mid = math.sqrt(lo_h * hi_h)
        n = factor * estimate_cells(S, vol_total, surf, wake, mid, hfar, growth)
        if n > target:
            lo_h = mid
        else:
            hi_h = mid
    hb = math.sqrt(lo_h * hi_h)
    return {"h_body_mm": hb, "h_far_mm": hfar, "growth": growth, "wake_box_mm": [wake[0].tolist(), wake[1].tolist()],
            "estimated_cells": factor * estimate_cells(S, vol_total, surf, wake, hb, hfar, growth)}


# ------------------------------------------------------------------------------------------------ meshing
def mesh_air(body, dom_mm: dict, sizes: dict, out_msh: Path, threads: int = 2) -> dict:
    """Air = box minus body, tetrahedral, markers body / farfield. Returns nodes (m), tets, boundary faces."""
    import gmsh
    from OCP.BRepTools import BRepTools
    brep = Path(out_msh).with_suffix(".brep")
    if not BRepTools.Write_s(body, str(brep)):
        raise MeshError("could not hand the fused body to the mesher")
    if gmsh.isInitialized():
        gmsh.finalize()
    gmsh.initialize(["-noenv"], interruptible=False)  # runs in the app's worker thread: no signal handler
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.option.setNumber("General.NumThreads", threads)
        gmsh.option.setNumber("Mesh.MeshSizeFromPoints", 0)
        gmsh.option.setNumber("Mesh.MeshSizeFromCurvature", 0)
        gmsh.option.setNumber("Mesh.MeshSizeExtendFromBoundary", 0)
        gmsh.option.setNumber("Mesh.Algorithm", 6)
        gmsh.option.setNumber("Mesh.Algorithm3D", 1)
        gmsh.option.setNumber("Mesh.Optimize", 1)
        occ = gmsh.model.occ
        bodies = occ.importShapes(str(brep))
        bodies = [b for b in bodies if b[0] == 3]
        if not bodies:
            raise MeshError("the fused body contains no solid")
        x0, x1 = dom_mm["x"]
        y0, y1 = dom_mm["y"]
        z0, z1 = dom_mm["z"]
        box = occ.addBox(x0, y0, z0, x1 - x0, y1 - y0, z1 - z0)
        try:
            out, _ = occ.cut([(3, box)], bodies, removeTool=True)
        except Exception as exc:
            raise MeshError(f"subtracting the aircraft from the air box failed: {exc}") from exc
        occ.synchronize()
        vols = [e for e in out if e[0] == 3]
        if len(vols) != 1:
            raise MeshError(f"the air region came out as {len(vols)} volumes instead of 1 (the aircraft touches "
                            "the domain boundary or the boolean failed)")
        surfs = [s[1] for s in gmsh.model.getBoundary(vols, oriented=False, combined=False)]
        tol = 1e-6 * dom_mm["Lc"]
        far, body_s = [], []
        for s in surfs:
            bb = gmsh.model.getBoundingBox(2, s)
            lo, hi = np.array(bb[:3]), np.array(bb[3:])
            on = any((hi[k] - lo[k]) < tol and (abs(lo[k] - e) < tol)
                     for k, e in ((0, x0), (0, x1), (1, y0), (1, y1), (2, z0), (2, z1)))
            (far if on else body_s).append(s)
        if not body_s or len(far) != 6:
            raise MeshError(f"unexpected boundary after the boolean ({len(body_s)} body faces, {len(far)} outer faces)")
        gmsh.model.addPhysicalGroup(3, [vols[0][1]], 1, "air")
        gmsh.model.addPhysicalGroup(2, body_s, 2, BODY)
        gmsh.model.addPhysicalGroup(2, far, 3, FARFIELD)
        # size field: hb at the body growing with distance, capped at hfar; finer wake box behind the tail
        hb, hfar, g = sizes["h_body_mm"], sizes["h_far_mm"], sizes["growth"]
        fd = gmsh.model.mesh.field
        fd.add("Distance", 1)
        fd.setNumbers(1, "SurfacesList", body_s)
        fd.setNumber(1, "Sampling", 20)
        fd.add("MathEval", 2)
        fd.setString(2, "F", f"Min({hfar}, {hb} + {g}*F1)")
        wlo, whi = sizes["wake_box_mm"]
        fd.add("Box", 3)
        for k, v in zip(("XMin", "YMin", "ZMin"), wlo):
            fd.setNumber(3, k, v)
        for k, v in zip(("XMax", "YMax", "ZMax"), whi):
            fd.setNumber(3, k, v)
        fd.setNumber(3, "VIn", 2.5 * hb)
        fd.setNumber(3, "VOut", hfar)
        fd.add("Min", 4)
        fd.setNumbers(4, "FieldsList", [2, 3])
        fd.setAsBackgroundMesh(4)
        gmsh.option.setNumber("Mesh.MeshSizeMax", hfar)
        gmsh.option.setNumber("Mesh.MeshSizeMin", hb * 0.3)
        try:
            gmsh.model.mesh.generate(3)
        except Exception as exc:
            raise MeshError(f"the volume mesher failed: {exc}") from exc
        tags, xyz, _ = gmsh.model.mesh.getNodes()
        order = np.argsort(tags)
        tags = np.asarray(tags)[order]
        xyz = np.asarray(xyz).reshape(-1, 3)[order]
        remap = np.full(int(tags.max()) + 1, -1, np.int64)
        remap[tags] = np.arange(len(tags))
        _, _, tn = gmsh.model.mesh.getElements(3, vols[0][1])
        tets = remap[np.asarray(tn[0], np.int64).reshape(-1, 4) if len(tn) else np.zeros((0, 4), np.int64)]
        if len(tets) == 0:
            raise MeshError("the volume mesher produced no tetrahedra")
        markers = {}
        for name, group in ((BODY, body_s), (FARFIELD, far)):
            faces = []
            for s in group:
                _, _, tn = gmsh.model.mesh.getElements(2, s)
                for blk in tn:
                    faces.append(remap[np.asarray(blk, np.int64).reshape(-1, 3)])
            markers[name] = np.vstack(faces)
        q = gmsh.model.mesh.getElementQualities(gmsh.model.mesh.getElements(3)[1][0], "minSICN")
        qual = {"min_sicn": float(np.min(q)), "frac_sicn_below_0p05": float(np.mean(np.asarray(q) < 0.05))}
    except MeshError:
        raise
    except Exception as exc:
        raise MeshError(f"meshing failed: {exc}") from exc
    finally:
        if gmsh.isInitialized():
            gmsh.finalize()
        brep.unlink(missing_ok=True)
    used = np.unique(tets)
    if len(used) != len(xyz):  # drop nodes no tetrahedron uses (CAD vertices)
        m2 = np.full(len(xyz), -1, np.int64)
        m2[used] = np.arange(len(used))
        xyz, tets = xyz[used], m2[tets]
        markers = {k: m2[v] for k, v in markers.items()}
    return {"nodes_mm": xyz, "tets": tets, "markers": markers, "quality": qual}


# ------------------------------------------------------------------------------------------------ SU2 files
def write_config(path: Path, mesh_file: str, V: float, alpha_deg: float, rho: float, aref: float, lref: float,
                 iterations: int, model: str = "sa", cfl: float = 10.0) -> Path:
    """SU2 configuration (SI units): INC_RANS/SA (model "sa") or INC_NAVIER_STOKES (model "laminar")."""
    from opencae.drone.analyses.cfd import flow_directions
    d, _ = flow_directions(alpha_deg)  # unit vector along the flow (the air moves towards -x)
    ux, uy, uz = (V * c for c in d)
    mu = rho * NU_AIR
    L = []
    w = L.append
    w("% OpenCAE Studio drone CFD: whole aircraft, incompressible, SI units")
    w("% frame: x forward, y left, z up; freestream towards -x")
    w(f"SOLVER= {'INC_NAVIER_STOKES' if model == 'laminar' else 'INC_RANS'}")
    w(f"KIND_TURB_MODEL= {'NONE' if model == 'laminar' else 'SA'}")
    w("MATH_PROBLEM= DIRECT\nRESTART_SOL= NO")
    w("INC_NONDIM= DIMENSIONAL\nINC_DENSITY_MODEL= CONSTANT\nINC_ENERGY_EQUATION= NO")
    w(f"INC_DENSITY_INIT= {rho:.10g}")
    w(f"INC_VELOCITY_INIT= ( {ux:.10g}, {uy:.10g}, {uz:.10g} )")
    w(f"VISCOSITY_MODEL= CONSTANT_VISCOSITY\nMU_CONSTANT= {mu:.10g}")
    if model != "laminar":
        w("FREESTREAM_TURBULENCEINTENSITY= 0.005\nFREESTREAM_TURB2LAMVISCRATIO= 3.0")
    w(f"MARKER_HEATFLUX= ( {BODY}, 0.0 )")
    w(f"MARKER_FAR= ( {FARFIELD} )")
    w(f"MARKER_MONITORING= ( {BODY} )\nMARKER_PLOTTING= ( {BODY} )")
    w(f"REF_AREA= {aref:.10g}\nREF_LENGTH= {lref:.10g}")
    w("REF_ORIGIN_MOMENT_X= 0.0\nREF_ORIGIN_MOMENT_Y= 0.0\nREF_ORIGIN_MOMENT_Z= 0.0")
    w("NUM_METHOD_GRAD= GREEN_GAUSS")
    w(f"CFL_NUMBER= {cfl:.10g}\nCFL_ADAPT= YES\nCFL_ADAPT_PARAM= ( 0.5, 1.2, 1.0, 200.0 )")
    w(f"ITER= {int(iterations)}")
    w("LINEAR_SOLVER= FGMRES\nLINEAR_SOLVER_PREC= ILU\nLINEAR_SOLVER_ERROR= 1E-5\nLINEAR_SOLVER_ITER= 10")
    w("CONV_NUM_METHOD_FLOW= FDS\nMUSCL_FLOW= YES\nSLOPE_LIMITER_FLOW= VENKATAKRISHNAN\n"
      "VENKAT_LIMITER_COEFF= 0.05\nTIME_DISCRE_FLOW= EULER_IMPLICIT")
    if model != "laminar":
        w("CONV_NUM_METHOD_TURB= BOUNDED_SCALAR\nMUSCL_TURB= NO\nTIME_DISCRE_TURB= EULER_IMPLICIT")
    w("CONV_FIELD= ( REL_RMS_PRESSURE, REL_RMS_VELOCITY-X, REL_RMS_VELOCITY-Y, REL_RMS_VELOCITY-Z )")
    w("CONV_RESIDUAL_MINVAL= -4\nCONV_STARTITER= 20")
    w(f"MESH_FILENAME= {mesh_file}\nMESH_FORMAT= SU2")
    w("OUTPUT_FILES= ( RESTART_ASCII, SURFACE_CSV )\nVOLUME_OUTPUT= ( COORDINATES, SOLUTION, PRIMITIVE )")
    w("RESTART_FILENAME= restart.csv\nSURFACE_FILENAME= surface_flow\nCONV_FILENAME= history")
    w("SCREEN_OUTPUT= ( INNER_ITER, RMS_PRESSURE, RMS_VELOCITY-X, RMS_NU_TILDE, CFx, CFz )"
      if model != "laminar" else "SCREEN_OUTPUT= ( INNER_ITER, RMS_PRESSURE, RMS_VELOCITY-X, CFx, CFz )")
    w("HISTORY_OUTPUT= ( ITER, RMS_RES, REL_RMS_RES, AERO_COEFF )")
    w("SCREEN_WRT_FREQ_INNER= 10")
    Path(path).write_text("\n".join(L) + "\n", encoding="utf-8")
    return Path(path)


# ------------------------------------------------------------------------------------------------ the case
def _dom_scaled(dom: dict, k: float) -> dict:
    return {**{a: [v * k for v in dom[a]] for a in ("x", "y", "z")}, **{a: dom[a] * k for a in ("L", "span", "Lc")},
            "centre": [v * k for v in dom["centre"]]}


def write_case(case: Path, aircraft, V: float, alpha_deg: float, rho: float, aref: float, lref: float,
               preset: str = "coarse", iterations: int = 300, model: str = "sa",
               target_cells: Optional[int] = None) -> dict:
    """Build the body, mesh the air, write ``mesh.su2`` and ``case.cfg`` (no solver run). Raises MeshError.

    Returns the case description; the arrays of the mesh (m) are under ``_mesh`` (not for the result file)."""
    from opencae.cfd.su2_mesh import write_su2
    from opencae.drone.analyses import cfd
    pr = PRESETS[preset]
    parts = cfd._cfd_parts(aircraft)
    if not parts:
        raise MeshError("no parts to put in the flow (every part is a propeller)")
    case = Path(case)
    if case.exists():
        shutil.rmtree(case)
    case.mkdir(parents=True)
    P = np.vstack([p.points for p in parts])  # mm
    lo_m, hi_m = P.min(0) * MM, P.max(0) * MM
    dom_m = cfd.domain_for(lo_m, hi_m)  # the same domain as the OpenFOAM path
    dom_mm = _dom_scaled(dom_m, 1.0 / MM)
    t0 = time.time()
    body, binfo = build_body(parts)
    t_body = time.time() - t0
    target = int(target_cells or pr["cells"])
    sizes = choose_sizes(parts, dom_mm, target, pr["growth"])
    t0 = time.time()
    m = mesh_air(body, dom_mm, sizes, case / "air.msh")
    t_mesh = time.time() - t0
    nodes_m = m["nodes_mm"] * MM
    write_su2(case / "mesh.su2", m["nodes_mm"], m["tets"], m["markers"], scale=MM)
    write_config(case / "case.cfg", "mesh.su2", V, alpha_deg, rho, aref, lref, iterations, model)
    return {"domain_m": dom_m, "bbox_m": [lo_m.tolist(), hi_m.tolist()], "preset": preset, "target_cells": target,
            "cells": int(len(m["tets"])), "nodes": int(len(nodes_m)), "sizes_mm": {
                k: sizes[k] for k in ("h_body_mm", "h_far_mm", "growth")},
            "markers": {k: int(len(v)) for k, v in m["markers"].items()}, "mesh_quality": m["quality"],
            "body": {"volume_mm3": binfo["volume_mm3"]}, "model": model, "speed_ms": V, "alpha_deg": alpha_deg,
            "rho": rho, "frame": "x forward, y left, z up; air flows towards -x", "parts": binfo["parts"],
            "omitted_parts": [p.name for p in aircraft.parts if p.role == "prop"],
            "seconds": {"body_boolean": round(t_body, 1), "mesh": round(t_mesh, 1)},
            "_mesh": {"nodes_m": nodes_m, "tets": m["tets"], "body_faces": m["markers"][BODY]}}


# ------------------------------------------------------------------------------------------ post-processing
def read_surface_csv(path: Path) -> dict:
    import csv
    with open(path, newline="") as fh:
        rd = csv.reader(fh)
        hdr = [h.strip().strip('"') for h in next(rd)]
        rows = np.array([[float(x) for x in r] for r in rd if r])
    return {h: rows[:, k] for k, h in enumerate(hdr)}


def fluid_normals(nodes: np.ndarray, tets: np.ndarray, tris: np.ndarray) -> np.ndarray:
    """Unit normals of boundary triangles pointing OUT of the fluid (into the body), from the adjacent tetrahedron."""
    N = np.int64(len(nodes))
    if N > 2_000_000:
        raise RuntimeError("mesh too large for the face lookup")
    f = np.concatenate([tets[:, [0, 1, 2]], tets[:, [0, 1, 3]], tets[:, [0, 2, 3]], tets[:, [1, 2, 3]]]).astype(np.int64)
    opp = np.concatenate([tets[:, 3], tets[:, 2], tets[:, 1], tets[:, 0]])
    fs = np.sort(f, axis=1)
    key = (fs[:, 0] * N + fs[:, 1]) * N + fs[:, 2]
    order = np.argsort(key)
    ts = np.sort(tris.astype(np.int64), axis=1)
    tk = (ts[:, 0] * N + ts[:, 1]) * N + ts[:, 2]
    pos = np.searchsorted(key[order], tk)
    pos = np.minimum(pos, len(order) - 1)
    idx = order[pos]
    if not np.all(key[idx] == tk):
        raise RuntimeError("a body face has no adjacent tetrahedron")
    v = nodes[tris]
    n = np.cross(v[:, 1] - v[:, 0], v[:, 2] - v[:, 0])
    n /= np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-300)
    inward = nodes[opp[idx]] - v.mean(1)  # towards the tetrahedron (the fluid)
    flip = np.einsum("ij,ij->i", n, inward) > 0
    n[flip] *= -1
    return n


def surface_forces(nodes: np.ndarray, tets: np.ndarray, tris: np.ndarray, p_node: np.ndarray,
                   tau_node: np.ndarray, drag_dir, lift_dir) -> dict:
    """Pressure + wall-shear force on the body (N), per body triangle. ``p_node`` gauge pressure (Pa) and
    ``tau_node`` wall shear vectors (Pa) at the triangle vertices' node ids (arrays over all mesh nodes)."""
    nf = fluid_normals(nodes, tets, tris)
    v = nodes[tris]
    area = 0.5 * np.linalg.norm(np.cross(v[:, 1] - v[:, 0], v[:, 2] - v[:, 0]), axis=1)
    p_f = p_node[tris].mean(1)
    tau_f = tau_node[tris].mean(1)
    f = p_f[:, None] * nf * area[:, None] + tau_f * area[:, None]  # normal out of the fluid: p n A pushes the body
    F = f.sum(0)
    return {"F": F, "drag": float(F @ np.asarray(drag_dir)), "lift": float(F @ np.asarray(lift_dir)),
            "area": float(area.sum()), "f_pressure": (p_f[:, None] * nf * area[:, None]).sum(0)}


def _grid(nodes_m: np.ndarray, tets: np.ndarray, vel: np.ndarray):
    import pyvista as pv
    cells = np.hstack([np.full((len(tets), 1), 4, np.int64), tets.astype(np.int64)]).ravel()
    g = pv.UnstructuredGrid(cells, np.full(len(tets), pv.CellType.TETRA, np.uint8), nodes_m)
    g.point_data["U"] = vel
    return g


def _log_tail(path: Path, n: int = 1800) -> str:
    return Path(path).read_text(errors="replace")[-n:] if Path(path).exists() else ""


def _peak_children_mb() -> Optional[float]:
    try:
        import resource
        return round(resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss / 1024.0, 0)
    except Exception:
        return None


def run_case(ctx, inst, V: float, v_st: str, alpha: float, a_st: str, alpha_default: bool, rho: float, r_st: str,
             preset: str, iterations: int, aref: float, aref_txt: str, ar_st: str) -> dict:
    """The SU2 analysis: returns a ``types.result`` (error result with the reason when anything fails)."""
    from opencae.drone.analyses import cfd
    from opencae.cfd.su2 import analyse_log, read_history, read_restart
    from opencae.drone.runlock import Cancelled, run_cancellable, solver_lock
    from opencae.solver.launcher import solver_environment
    ID, TITLE = cfd.ID, cfd.TITLE
    case = Path(ctx.workdir) / "cfd_su2_case"
    out = Path(ctx.out_dir)
    t0 = time.time()
    log_tail = ""
    try:
        lref = math.sqrt(aref)
        with solver_lock("the CFD run", on_wait=lambda m: ctx.progress(m, 0.0)):
            ctx.progress("CFD (SU2): fusing the parts into one body and meshing the air", 0.05)
            try:
                info = write_case(case, ctx.aircraft, V, alpha, rho, aref, lref, preset, iterations)
            except MeshError as exc:
                return T.result(ID, TITLE, T.ERROR, f"CFD (SU2) could not build the mesh: {exc}. No result was "
                                "computed.", solver={"name": MODEL_TEXT, "converged": False},
                                log=f"mesh failure: {exc}")
            if ctx.cancelled():
                raise Cancelled()
            mesh = info.pop("_mesh")
            ctx.progress(f"CFD (SU2): solving on {info['cells'] / 1e3:.0f} k cells", 0.15)
            ts = time.time()
            logp = case / "su2.log"
            env = solver_environment(inst, 1)
            with open(logp, "w") as lf:
                rc = run_cancellable([inst.exe, "case.cfg"], ctx.cancelled, 6 * 3600, cwd=str(case), env=env,
                                     stdout=lf)
            t_solve = time.time() - ts
            text = logp.read_text(errors="replace")
            ok, errs, warns, conv_log = analyse_log(text, rc, case / "restart.csv")
            if "NaN" in text and "diverged" in text:
                ok, errs = False, errs + ["SU2 diverged (NaN in the solution)."]
            if not ok:
                log_tail = _log_tail(logp)
                raise RuntimeError("SU2 did not finish: " + "; ".join(errs[:3]))
        ctx.progress("CFD (SU2): post-processing", 0.9)
        nodes, tets = mesh["nodes_m"], mesh["tets"]
        rest = read_restart(case / "restart.csv")
        surf = read_surface_csv(case / "surface_flow.csv")
        if not all(np.isfinite(rest["Pressure"])):
            raise RuntimeError("the SU2 solution contains non-finite values")
        q = 0.5 * rho * V * V
        # surface values per mesh node (SU2 PointID = node index of the mesh written by write_case)
        ids = surf["PointID"].astype(np.int64)
        if ids.max() >= len(nodes) or np.abs(nodes[ids] - np.c_[surf["x"], surf["y"], surf["z"]]).max() > 1e-7:
            raise RuntimeError("the SU2 surface file does not match the mesh (point ids)")
        p_node = np.zeros(len(nodes))
        tau_node = np.zeros((len(nodes), 3))
        p_node[ids] = surf["Pressure"]
        for k, c in enumerate("xyz"):
            tau_node[ids, k] = surf[f"Skin_Friction_Coefficient_{c}"] * q
        drag_d, lift_d = cfd.flow_directions(alpha)
        faces = mesh["body_faces"]
        F = surface_forces(nodes, tets, faces, p_node, tau_node, drag_d, lift_d)
        lift_n, drag_n = F["lift"], F["drag"]
        cl, cd = lift_n / (q * aref), drag_n / (q * aref)
        hist = read_history(case / "history.csv")
        its = hist["Inner_Iter"] if "Inner_Iter" in hist else np.arange(len(hist["CFx"]))
        n_it = int(its[-1]) + 1
        fmag = np.hypot(hist["CFx"], hist["CFz"])
        win = fmag[its >= n_it * 0.9 - 1e-9]
        fchg = float((win.max() - win.min()) / max(abs(fmag[-1]), 1e-9)) if len(win) >= 2 else None
        by_force = fchg is not None and fchg < 0.01
        converged = bool(conv_log or by_force)
        # SU2's own force on the same surface, as a cross-check of the integration above (N, aircraft frame)
        su2_f = np.array([hist["CFx"][-1], hist["CFy"][-1], hist["CFz"][-1]]) * 0.5 * rho * V * V * aref
        if converged:  # SU2's own message is about its residual criterion; the force-change rule decided here
            warns = [w for w in warns if "did NOT reach the convergence criterion" not in w]
        resid = {k: float(hist[k][-1]) for k in hist if k.startswith("rms[")}
        # body surface mesh and fields (per vertex)
        used = np.unique(faces)
        remap = np.full(len(nodes), -1, np.int64)
        remap[used] = np.arange(len(used))
        pts_mm, tri = nodes[used] / MM, remap[faces]
        cp = p_node[used] / q
        fields, files = [], []
        spec_fields = [("cfd_cp.npz", "cp", cp, "pressure coefficient", "-"),
                       ("cfd_p.npz", "p_static", p_node[used], "static pressure (gauge)", "Pa"),
                       ("cfd_tau_wall.npz", "wall_shear", np.linalg.norm(tau_node[used], axis=1),
                        "wall shear stress", "Pa")]
        vel = np.c_[rest["Velocity_x"], rest["Velocity_y"], rest["Velocity_z"]]
        flow = {}
        try:
            flow = cfd._flow(_grid(nodes, tets, vel), V, math.radians(alpha), info["domain_m"], info["bbox_m"])
        except Exception as exc:  # slices and streamlines are extras: say so, keep the surface fields
            flow = {}
            warns.append(f"flow slices and streamlines were not produced: {exc}")
        for fname, key, vals, qty, unit in spec_fields:
            if fname == "cfd_cp.npz":
                T.save_field(out / fname, vals, pts_mm, tri, None, **flow)
            else:
                T.save_field(out / fname, vals, mesh="cfd_cp.npz")
            fields.append({"file": fname, "key": key, "quantity": qty, "unit": unit})
        ncell = int(info["cells"])
        mesh_ok = info["mesh_quality"]["min_sicn"] > 0.0
        basis = cfd._weakest([v_st, a_st, r_st or T.USER])
        st = T.OK if converged else T.WATCH
        asp = (f"angle of attack {alpha:g} deg (default 0: set analyses.cfd.alpha_deg or aero.alpha_deg)"
               if alpha_default else f"angle of attack {alpha:g} deg")
        limits = ("coarse tetrahedral mesh, no boundary-layer prisms: drag is indicative only"
                  if preset == "coarse" else "tetrahedral mesh without boundary-layer prisms: drag is indicative only")
        summ = (f"Whole-aircraft airflow at {V:g} m/s, {asp}, computed with SU2 (OpenFOAM was not used): lift "
                f"{lift_n:.2f} N, drag {drag_n:.2f} N (C_L {cl:.3f}, C_D {cd:.3f} on {aref_txt}, {aref:.4f} m2). "
                f"{preset.capitalize()} mesh of {ncell / 1e3:.0f} k tetrahedra, "
                f"{'converged' if converged else 'not converged after ' + str(n_it) + ' iterations'}. Model: "
                f"{MODEL_TEXT}; {limits}; propellers are not modelled.")
        mm = lambda v, u="", s=st, l="": T.metric(v, u, s, basis, l)  # noqa: E731
        metrics = {"cl": mm(cl, "", st, "Lift coefficient"), "cd": mm(cd, "", st, "Drag coefficient"),
                   "ld": mm(cl / cd if cd > 0 else None, "", st, "Lift to drag"),
                   "lift_n": mm(lift_n, "N", st, "Lift"), "drag_n": mm(drag_n, "N", st, "Drag"),
                   "converged": mm(1.0 if converged else 0.0, "", T.OK if converged else T.WATCH, "Converged"),
                   "cells": mm(ncell, "", T.OK, "Cells"), "iterations": mm(n_it, "", T.OK, "Iterations")}
        solver = {"name": MODEL_TEXT, "solver_family": "su2", "converged": converged,
                  "iterations": n_it, "iterations_requested": iterations, "cells": ncell,
                  "runtime_s": round(time.time() - t0, 1), "solve_s": round(t_solve, 1),
                  "step_seconds": info["seconds"], "converged_by_residuals": bool(conv_log),
                  "force_change_last_10pct": fchg, "residuals_final": resid, "mesh_ok": mesh_ok,
                  "su2": getattr(inst, "exe", ""), "peak_children_rss_mb": _peak_children_mb()}
        extra = {"preset": preset, "speed_ms": V, "alpha_deg": alpha, "alpha_is_default": alpha_default,
                 "rho": rho, "reference_area_m2": aref, "reference_area_basis": aref_txt,
                 "omitted_parts": info["omitted_parts"], "domain_m": info["domain_m"],
                 "history": np.c_[its + 1, hist["CFx"], hist["CFz"]].tolist(),
                 "history_columns": ["iteration", "CFx", "CFz"],
                 "mesh": {"cells": ncell, "nodes": info["nodes"], "markers": info["markers"],
                          "sizes_mm": info["sizes_mm"], **info["mesh_quality"]},
                 "force_check": {"integrated_N": F["F"].tolist(), "su2_history_N": su2_f.tolist()},
                 "warnings": warns[:8],
                 "assumptions": ["whole aircraft, both sides meshed, no symmetry plane",
                                 "propellers not modelled (parts with role prop left out)",
                                 "parts fused into one body; internal cavities and gaps below the mesh size are "
                                 "not resolved", f"steady {MODEL_TEXT}, no wall function, no prism layers",
                                 f"{preset} mesh: indicative, not a mesh-independence study",
                                 "incompressible air, no sideslip"]}
        res = T.result(ID, TITLE, st, summ, metrics, fields=fields, solver=solver, extra=extra)
        if not ctx.keep_cases:
            shutil.rmtree(case, ignore_errors=True)
        return res
    except Cancelled:
        return T.result(ID, TITLE, T.ERROR, "The CFD run was cancelled.")
    except Exception as exc:  # solver failures are reported, never hidden
        msg = f"CFD (SU2) failed: {exc}"
        return T.result(ID, TITLE, T.ERROR, msg, solver={"name": MODEL_TEXT, "converged": False},
                        log=(msg + "\n" + log_tail))
