"""CFD: steady RANS (OpenFOAM simpleFoam, k-omega SST) of the WHOLE aircraft on a snappyHexMesh mesh.

Both sides are meshed (no symmetry plane). Propellers are not modelled (parts with role "prop" are left out);
the motors, arms, body, wings and the rest are. The case follows ``opencae.engineering.field_analyses.run_cfd_cruise``
(its post-processing helpers are imported, not copied): prism layers on the aircraft, Spalding wall function,
function-object-free evaluation of forces from the written fields.

Frame: the project frame (x forward, y left, z up) is right-handed, so the case uses it as it is. The aircraft flies
towards +x, so the air comes from ahead: the freestream is U = V (-cos a, 0, sin a), the inflow boundary lies ahead of
the nose (x max), the wake refinement behind the tail, lift is +z and drag is along the flow.

Saved surface fields share one mesh: cfd_cp.npz holds points/triangles, the other cfd_*.npz hold only ``values`` and
``mesh`` = "cfd_cp.npz" (see :func:`load_surface_field`).
"""
from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional

import numpy as np

from opencae.drone import types as T
from opencae.drone.runlock import Cancelled as _Cancelled
from opencae.drone.runlock import run_cancellable, solver_lock

ID = "cfd"
TITLE = "Aerodynamics (CFD)"
NEEDS = ["type", "analyses.cfd.speed_ms", "analyses.cfd.alpha_deg", "analyses.cfd.preset",
         "analyses.cfd.iterations", "aero.cruise_speed_ms", "aero.alpha_deg", "aero.ref_area_m2", "environment.rho"]

MM = 1e-3
# presets: base cell as a share of max(span, length), surface refinement levels (min, max), cell budget
PRESETS = {"coarse": {"div": 6.0, "level": (3, 4), "cap": 400_000, "layers": 3},
           "medium": {"div": 8.0, "level": (3, 5), "cap": 1_000_000, "layers": 3},
           "fine": {"div": 10.0, "level": (4, 6), "cap": 2_000_000, "layers": 3}}
UP_L, DOWN_L, SIDE_LC = 3.0, 6.0, 2.5
RHO_DEFAULT = 1.225
TURB_INTENSITY = 0.005


# ----------------------------------------------------------------------------------------------- spec
def _leaf(node):
    if isinstance(node, dict):
        return node.get("value"), node.get("status", T.ESTIMATE)
    if node is None:
        return None, ""
    return node, T.ESTIMATE


def _num(spec, dotted):
    cur = spec
    for k in dotted.split("."):
        if not isinstance(cur, dict) or k not in cur:
            return None, ""
        cur = cur[k]
    v, s = _leaf(cur)
    try:
        return (float(v), s) if v is not None else (None, s)
    except (TypeError, ValueError):
        return None, s


RANK = {T.ESTIMATE: 0, T.DATASHEET: 1, T.MEASURED: 2, T.USER: 2}


def _weakest(sts) -> str:
    sts = [s for s in sts if s]
    return min(sts, key=lambda s: RANK.get(s, 0)) if sts else T.ESTIMATE


# ------------------------------------------------------------------------------------------- OpenFOAM
def _find_foam():
    """The OpenFOAM installation (any version that has simpleFoam), or None."""
    try:
        from opencae.cfd.openfoam.install import find_openfoam
        return find_openfoam()
    except Exception:
        return None


def _foam_cmd(inst, case: Path, cmd: str, log: str) -> list:
    from opencae.engineering import field_analyses as FA
    script = f"{FA.CFD_MPI_ENV}( {cmd} ) > '{log}' 2>&1"
    return inst.argv(case, script)


def _foam(inst, case: Path, cmd: str, log: str, timeout=7200, cancelled=None) -> int:
    return run_cancellable(_foam_cmd(inst, case, cmd, log), cancelled, timeout)


def _log_tail(case: Path, log: str, n=1500) -> str:
    p = Path(case) / log
    return p.read_text(errors="replace")[-n:] if p.exists() else ""


# ----------------------------------------------------------------------------------------------- geometry
def _cfd_parts(aircraft) -> list:
    return [p for p in aircraft.parts if p.role != "prop"]


def write_stl(path: Path, points_m: np.ndarray, tris: np.ndarray) -> None:
    dt = np.dtype([("n", "<f4", 3), ("v", "<f4", (3, 3)), ("a", "<u2")])
    a = np.zeros(len(tris), dt)
    v = np.asarray(points_m, float)[tris]
    n = np.cross(v[:, 1] - v[:, 0], v[:, 2] - v[:, 0])
    nn = np.linalg.norm(n, axis=1, keepdims=True)
    a["n"] = n / np.maximum(nn, 1e-30)
    a["v"] = v
    with open(path, "wb") as fh:
        fh.write(b"opencae drone aircraft".ljust(80, b" "))
        fh.write(np.uint32(len(tris)).tobytes())
        fh.write(a.tobytes())


def _raster_area(points2d_mm: np.ndarray, tris: np.ndarray, px: int = 1200) -> float:
    """Union area (m2) of triangles projected to a plane (rasterised)."""
    from PIL import Image, ImageDraw
    if len(tris) == 0:
        return 0.0
    lo, hi = points2d_mm.min(0), points2d_mm.max(0)
    sc = (px - 2) / max(float((hi - lo).max()), 1e-9)
    xy = (points2d_mm - lo) * sc + 1
    img = Image.new("1", (px, px), 0)
    dr = ImageDraw.Draw(img)
    for t in tris:
        dr.polygon([tuple(xy[i]) for i in t], fill=1)
    return float(np.asarray(img).sum()) / sc ** 2 * 1e-6


def reference_area(aircraft, spec: dict) -> tuple:
    """(area m2, basis text): aero.ref_area_m2, else the wing planform (role wing), else the frontal area."""
    a, st = _num(spec, "aero.ref_area_m2")
    if a and a > 0:
        return a, "aero.ref_area_m2 from the drone file", st
    wings = aircraft.role_parts("wing")
    if wings:
        P, Tt = aircraft.surface(("wing",))
        return _raster_area(P[:, [0, 1]], Tt), "wing planform area from the CAD (parts with role wing)", T.ESTIMATE
    P, Tt = aircraft.surface(tuple(r for r in T.ROLES if r != "prop"))
    return _raster_area(P[:, [1, 2]], Tt), "frontal area of the aircraft from the CAD (no wing, no aero.ref_area_m2)", \
        T.ESTIMATE


def flow_directions(alpha_deg: float) -> tuple:
    """(drag_dir, lift_dir) unit vectors in the aircraft frame: the air moves towards -x (rising by sin a), drag is
    along the flow, lift is perpendicular to it and has a positive z component."""
    al = math.radians(alpha_deg)
    return [-math.cos(al), 0.0, math.sin(al)], [math.sin(al), 0.0, math.cos(al)]


def point_inside(points: np.ndarray, tris: np.ndarray, p: np.ndarray) -> bool:
    """Ray-casting inside test of a closed triangulated surface (+x ray, Moller-Trumbore)."""
    v0, v1, v2 = (points[tris[:, i]] for i in range(3))
    d = np.array([1.0, 1e-3, 2e-3])
    e1, e2 = v1 - v0, v2 - v0
    h = np.cross(d, e2)
    a = np.einsum("ij,ij->i", e1, h)
    ok = np.abs(a) > 1e-14
    f = np.where(ok, 1.0 / np.where(ok, a, 1.0), 0.0)
    s = p - v0
    u = f * np.einsum("ij,ij->i", s, h)
    q = np.cross(s, e1)
    v = f * (q @ d)
    t = f * np.einsum("ij,ij->i", e2, q)
    hit = ok & (u >= 0) & (v >= 0) & (u + v <= 1) & (t > 0)
    return bool(hit.sum() % 2 == 1)


def domain_for(lo_m: np.ndarray, hi_m: np.ndarray) -> dict:
    L = float(hi_m[0] - lo_m[0])
    span = float(hi_m[1] - lo_m[1])
    Lc = max(span, L, 1e-3)
    ctr = 0.5 * (lo_m + hi_m)
    m = SIDE_LC * Lc  # clear distance from the aircraft's bounding box to the side / top / bottom boundaries
    return {"x": [float(lo_m[0] - DOWN_L * L), float(hi_m[0] + UP_L * L)],  # inflow ahead (+x), wake behind (-x)
            "y": [float(lo_m[1] - m), float(hi_m[1] + m)], "z": [float(lo_m[2] - m), float(hi_m[2] + m)],
            "L": L, "span": span, "Lc": Lc, "centre": ctr.tolist()}


def location_in_mesh(dom: dict, h0: float, hi_m) -> list:
    """A point in the open air upstream (ahead of the nose) of the aircraft (inside the domain, off the grid planes)."""
    x = hi_m[0] + 0.5 * (dom["x"][1] - hi_m[0]) + 0.137 * h0
    return [float(x), float(dom["centre"][1] + 0.113 * h0), float(dom["centre"][2] + 0.071 * h0)]


# --------------------------------------------------------------------------------------------- the case
def _hdr(cls, obj):
    return "FoamFile\n{\n    version 2.0;\n    format ascii;\n    class " + cls + ";\n    object " + obj + ";\n}\n"


def write_case(case: Path, aircraft, spec: dict, preset: str = "coarse", iterations: int = 300,
               procs: int = 1, speed: Optional[float] = None, alpha_deg: float = 0.0, rho: float = RHO_DEFAULT,
               layers: Optional[int] = None) -> dict:
    """Write the OpenFOAM case (no solver run). Returns the case description (domain, locationInMesh, levels...)."""
    pr = PRESETS[preset]
    parts = _cfd_parts(aircraft)
    if not parts:
        raise ValueError("no parts to put in the flow (every part is a propeller)")
    case = Path(case)
    if case.exists():
        shutil.rmtree(case)
    (case / "constant" / "triSurface").mkdir(parents=True)
    (case / "system").mkdir()
    (case / "0").mkdir()
    P, Tt, off = [], [], 0
    for p in parts:
        P.append(p.points * MM)
        Tt.append(p.triangles + off)
        off += len(p.points)
    P, Tt = np.vstack(P), np.vstack(Tt)
    write_stl(case / "constant" / "triSurface" / "aircraft.stl", P, Tt)
    lo, hi = P.min(0), P.max(0)
    dom = domain_for(lo, hi)
    h0 = dom["Lc"] / pr["div"]
    nx = max(4, math.ceil((dom["x"][1] - dom["x"][0]) / h0))
    ny = max(4, math.ceil((dom["y"][1] - dom["y"][0]) / h0))
    nz = max(4, math.ceil((dom["z"][1] - dom["z"][0]) / h0))
    loc = location_in_mesh(dom, h0, hi)
    for pp in parts:
        if point_inside(pp.points * MM, pp.triangles, np.array(loc)):
            raise RuntimeError(f"locationInMesh {loc} lies inside part {pp.name}")
    lv0, lv1 = pr["level"]
    lay = pr["layers"] if layers is None else int(layers)
    Lc, L = dom["Lc"], dom["L"]
    near_lo, near_hi = lo - 0.2 * Lc, hi + 0.2 * Lc
    wake_lo = np.array([lo[0] - 2.5 * L, lo[1] - 0.05 * Lc, lo[2] - 0.1 * Lc])  # behind the tail (-x)
    wake_hi = np.array([lo[0], hi[1] + 0.05 * Lc, hi[2] + 0.1 * Lc])
    V = float(speed or 1.0)
    al = math.radians(alpha_deg)
    ux, uz = -V * math.cos(al), V * math.sin(al)  # the air flows towards -x
    k_in = 1.5 * (V * TURB_INTENSITY) ** 2
    om_in = math.sqrt(k_in) / (0.09 ** 0.25 * 0.03 * Lc)
    W = lambda p, t: (case / p).write_text(t)  # noqa: E731
    x0, x1 = dom["x"]
    y0, y1 = dom["y"]
    z0, z1 = dom["z"]
    W("system/blockMeshDict", _hdr("dictionary", "blockMeshDict") + f"""
convertToMeters 1;
vertices (({x0:.6f} {y0:.6f} {z0:.6f}) ({x1:.6f} {y0:.6f} {z0:.6f}) ({x1:.6f} {y1:.6f} {z0:.6f}) ({x0:.6f} {y1:.6f} {z0:.6f}) ({x0:.6f} {y0:.6f} {z1:.6f}) ({x1:.6f} {y0:.6f} {z1:.6f}) ({x1:.6f} {y1:.6f} {z1:.6f}) ({x0:.6f} {y1:.6f} {z1:.6f}));
blocks (hex (0 1 2 3 4 5 6 7) ({nx} {ny} {nz}) simpleGrading (1 1 1));
boundary (
 farfield {{ type patch; faces ((0 4 7 3) (1 2 6 5) (3 7 6 2) (0 3 2 1) (4 5 6 7) (0 1 5 4)); }}
);
""")
    W("system/surfaceFeatureExtractDict", _hdr("dictionary", "surfaceFeatureExtractDict") + """
aircraft.stl { extractionMethod extractFromSurface; includedAngle 150;
  subsetFeatures { nonManifoldEdges no; openEdges yes; } trimFeatures { minElem 0; minLen 0; } writeObj no; }
""")
    f3 = lambda a: f"({a[0]:.6f} {a[1]:.6f} {a[2]:.6f})"  # noqa: E731
    near_level, wake_level = max(lv0 - 1, 1), max(lv0 - 2, 1)
    cap = pr["cap"]
    W("system/snappyHexMeshDict", _hdr("dictionary", "snappyHexMeshDict") + f"""
castellatedMesh true; snap true; addLayers {'true' if lay > 0 else 'false'};
geometry {{ aircraft.stl {{ type triSurfaceMesh; name aircraft; }}
           near {{ type searchableBox; min {f3(near_lo)}; max {f3(near_hi)}; }}
           wake {{ type searchableBox; min {f3(wake_lo)}; max {f3(wake_hi)}; }} }}
castellatedMeshControls {{ maxLocalCells {cap}; maxGlobalCells {cap}; minRefinementCells 10;
  maxLoadUnbalance 0.10; nCellsBetweenLevels 3; features ( {{ file "aircraft.eMesh"; level {lv1}; }} );
  refinementSurfaces {{ aircraft {{ level ({lv0} {lv1}); patchInfo {{ type wall; }} }} }}
  resolveFeatureAngle 25;
  refinementRegions {{ near {{ mode inside; levels ((1E15 {near_level})); }}
                      wake {{ mode inside; levels ((1E15 {wake_level})); }} }}
  locationInMesh {f3(loc)}; allowFreeStandingZoneFaces true; }}
snapControls {{ nSmoothPatch 3; tolerance 2.0; nSolveIter 50; nRelaxIter 5; nFeatureSnapIter 10;
  implicitFeatureSnap false; explicitFeatureSnap true; multiRegionFeatureSnap false; }}
addLayersControls {{ relativeSizes true; layers {{ aircraft {{ nSurfaceLayers {max(lay, 1)}; }} }}
  expansionRatio 1.2; finalLayerThickness 0.4; minThickness 0.1;
  nGrow 0; featureAngle 130; slipFeatureAngle 30; nRelaxIter 5; nSmoothSurfaceNormals 1; nSmoothNormals 3;
  nSmoothThickness 10; maxFaceThicknessRatio 0.5; maxThicknessToMedialRatio 0.3; minMedialAxisAngle 90;
  nBufferCellsNoExtrude 0; nLayerIter 50; nRelaxedIter 20; }}
meshQualityControls {{ maxNonOrtho 65; maxBoundarySkewness 20; maxInternalSkewness 4; maxConcave 80; minVol 1e-13;
  minTetQuality 1e-15; minArea -1; minTwist 0.02; minDeterminant 0.001; minFaceWeight 0.05; minVolRatio 0.01;
  minTriangleTwist -1; nSmoothScale 4; errorReduction 0.75; relaxed {{ maxNonOrtho 75; }} }}
mergeTolerance 1e-6;
""")
    W("system/decomposeParDict", _hdr("dictionary", "decomposeParDict") + f"numberOfSubdomains {procs};\n"
      f"method simple;\nsimpleCoeffs {{ n (1 {procs} 1); delta 0.001; }}\n")
    wint = max(1, int(round(iterations / 10)))
    W("system/controlDict", _hdr("dictionary", "controlDict") + f"""
application simpleFoam; startFrom latestTime; startTime 0; stopAt endTime; endTime {iterations}; deltaT 1;
writeControl timeStep; writeInterval {wint}; purgeWrite 0; writeFormat binary; writePrecision 8;
writeCompression off; timeFormat general; timePrecision 6; runTimeModifiable false;
""")
    W("system/fvSchemes", _hdr("dictionary", "fvSchemes") + """
ddtSchemes { default steadyState; }
gradSchemes { default Gauss linear; grad(U) cellLimited Gauss linear 1; }
divSchemes { default none; div(phi,U) bounded Gauss linearUpwind grad(U); div(phi,k) bounded Gauss upwind;
 div(phi,omega) bounded Gauss upwind; div((nuEff*dev2(T(grad(U))))) Gauss linear; }
laplacianSchemes { default Gauss linear limited corrected 0.5; }
interpolationSchemes { default linear; }
snGradSchemes { default limited corrected 0.5; }
wallDist { method meshWave; }
""")
    W("system/fvSolution", _hdr("dictionary", "fvSolution") + """
solvers { p { solver GAMG; smoother GaussSeidel; tolerance 1e-7; relTol 0.1; }
 "(U|k|omega)" { solver smoothSolver; smoother symGaussSeidel; tolerance 1e-8; relTol 0.1; } }
SIMPLE { nNonOrthogonalCorrectors 1; consistent true; residualControl { p 1e-4; U 1e-4; "(k|omega)" 1e-4; } }
relaxationFactors { fields { p 0.5; } equations { U 0.7; ".*" 0.7; } }
""")
    from opencae.engineering import field_analyses as FA
    W("constant/transportProperties", _hdr("dictionary", "transportProperties") +
      f"transportModel Newtonian;\nnu {FA.CFD_NU:g};\n")
    W("constant/turbulenceProperties", _hdr("dictionary", "turbulenceProperties") +
      "simulationType RAS;\nRAS { RASModel kOmegaSST; turbulence on; printCoeffs off; }\n")
    bc = lambda far, wall: f"boundaryField {{ farfield {{ {far} }} aircraft {{ {wall} }} }}\n"  # noqa: E731
    W("0/U", _hdr("volVectorField", "U") + f"dimensions [0 1 -1 0 0 0 0];\ninternalField uniform ({ux:.5f} 0 {uz:.5f});\n"
      + bc(f"type freestreamVelocity; freestreamValue uniform ({ux:.5f} 0 {uz:.5f});", "type noSlip;"))
    W("0/p", _hdr("volScalarField", "p") + "dimensions [0 2 -2 0 0 0 0];\ninternalField uniform 0;\n"
      + bc("type freestreamPressure; freestreamValue uniform 0;", "type zeroGradient;"))
    W("0/k", _hdr("volScalarField", "k") + f"dimensions [0 2 -2 0 0 0 0];\ninternalField uniform {k_in:.6e};\n"
      + bc(f"type inletOutlet; inletValue uniform {k_in:.6e}; value uniform {k_in:.6e};",
           f"type kqRWallFunction; value uniform {k_in:.6e};"))
    W("0/omega", _hdr("volScalarField", "omega") + f"dimensions [0 0 -1 0 0 0 0];\ninternalField uniform {om_in:.5f};\n"
      + bc(f"type inletOutlet; inletValue uniform {om_in:.5f}; value uniform {om_in:.5f};",
           f"type omegaWallFunction; value uniform {om_in:.5f};"))
    W("0/nut", _hdr("volScalarField", "nut") + "dimensions [0 2 -1 0 0 0 0];\ninternalField uniform 0;\n"
      + bc("type calculated; value uniform 0;", "type nutUSpaldingWallFunction; value uniform 0;"))
    return {"domain_m": dom, "location_in_mesh_m": loc, "base_cell_m": h0, "blocks": [nx, ny, nz],
            "surface_levels": [lv0, lv1], "layers": lay, "preset": preset, "cell_cap": cap, "write_interval": wint,
            "bbox_m": [lo.tolist(), hi.tolist()], "procs": procs, "speed_ms": V, "alpha_deg": alpha_deg,
            "rho": rho, "frame": "x forward, y left, z up; air flows towards -x", "parts": [p.name for p in parts],
            "omitted_parts": [p.name for p in aircraft.parts if p.role == "prop"]}


# ------------------------------------------------------------------------------------------ post-processing
def _reconstruct(inst, case: Path, cancelled=None) -> list:
    """Reconstruct every write of a parallel run one at a time and drop each processor copy."""
    from opencae.engineering import field_analyses as FA
    p0 = case / "processor0"
    if not p0.is_dir():
        raise RuntimeError(f"no processor0 in {case}: nothing to reconstruct")
    tn = sorted((d.name for d in p0.iterdir() if d.is_dir() and FA._isnum(d.name) and float(d.name) > 0), key=float)
    if not tn:
        raise RuntimeError("simpleFoam wrote no results")
    for i, t in enumerate(tn):
        last = i == len(tn) - 1
        log = f"log.reconstructPar.{t}"
        rc = _foam(inst, case, f"reconstructPar -time {t}" + ("" if last else " -fields '(p U nut)'"), log,
                   cancelled=cancelled)
        if rc != 0 or not any((case / t / f).exists() for f in ("U", "U.gz")):
            raise RuntimeError(f"reconstructPar failed for time {t} ({rc})\n{_log_tail(case, log)}")
        for pd in case.glob("processor*"):
            shutil.rmtree(pd / t, ignore_errors=True)
    for pd in case.glob("processor*"):
        shutil.rmtree(pd, ignore_errors=True)
    return tn


def _to_vtk(inst, case: Path, cancelled=None) -> None:
    from opencae.engineering import field_analyses as FA
    for name, opts in FA.CFD_VTK:
        log = f"log.foamToVTK_{name}"
        rc = _foam(inst, case, f"foamToVTK -noFunctionObjects -overwrite -name {name} {opts}", log, cancelled=cancelled)
        if rc != 0:
            raise RuntimeError(f"foamToVTK ({name}) failed ({rc})\n{_log_tail(case, log)}")


def _flow(vol, V: float, alpha: float, dom: dict, bbox_m) -> dict:
    """Slices and streamlines of the flow (aircraft frame, m in, mm out). The air flows towards -x."""
    import pyvista as pv
    g = vol if "U" in vol.point_data else vol.cell_data_to_point_data(pass_cell_data=False)
    lo, hi = np.asarray(bbox_m[0]), np.asarray(bbox_m[1])
    Lc, L = dom["Lc"], dom["L"]
    ctr = np.asarray(dom["centre"])
    q = 0.5 * V * V
    crop_lo = np.array([lo[0] - 1.6 * L, lo[1] - 0.4 * Lc, lo[2] - 0.5 * Lc])
    crop_hi = np.array([hi[0] + 0.4 * L, hi[1] + 0.4 * Lc, hi[2] + 0.5 * Lc])
    eps = 1e-4
    specs = [("chordwise, centreline", [0, 1, 0], [0, ctr[1] + eps, 0]),
             ("chordwise, left 30 % span", [0, 1, 0], [0, ctr[1] + 0.3 * dom["span"] + eps, 0]),
             ("chordwise, right 30 % span", [0, 1, 0], [0, ctr[1] - 0.3 * dom["span"] + eps, 0]),
             ("horizontal, mid height", [0, 0, 1], [0, 0, 0.5 * (lo[2] + hi[2])])]
    sp, sv, st, so, names, cnt = [], [], [], [0], [], 0
    for name, nrm, org in specs:
        sl = g.slice(normal=nrm, origin=org)
        if sl.n_cells == 0:
            continue
        sl = sl.triangulate()
        c = np.asarray(sl.cell_centers().points)
        keep = np.where(np.all((c >= crop_lo) & (c <= crop_hi), axis=1))[0]
        if not len(keep):
            continue
        sl = sl.extract_cells(keep).extract_surface(algorithm="dataset_surface").triangulate()
        tri = sl.faces.reshape(-1, 4)[:, 1:]
        sp.append(np.asarray(sl.points) / MM)
        sv.append(np.linalg.norm(np.asarray(sl.point_data["U"]), axis=1))
        st.append(tri + cnt)
        cnt += len(sl.points)
        so.append(cnt)
        names.append(name)
    x0 = hi[0] + 0.3 * L  # seeds ahead of the nose; the air rises by tan(alpha) per metre it travels aft
    ys = np.linspace(lo[1] - 0.05 * Lc, hi[1] + 0.05 * Lc, 21)
    zs = np.linspace(lo[2], hi[2], 4) - (x0 - hi[0]) * math.tan(alpha)
    seeds = np.array([[x0, y, z] for y in ys for z in zs])
    lines = g.streamlines_from_source(pv.PolyData(seeds), vectors="U", integration_direction="forward",
                                      max_length=6 * Lc, initial_step_length=0.2, step_unit="cl", max_steps=6000,
                                      terminal_speed=1e-4)
    pts, spd, offs = [], [], [0]
    if lines.n_points:
        Ls = np.asarray(lines.lines)
        Pp = np.asarray(lines.points)
        us = np.linalg.norm(np.asarray(lines.point_data["U"]), axis=1)
        i = 0
        while i < len(Ls):
            n = int(Ls[i])
            ids = Ls[i + 1:i + 1 + n]
            i += n + 1
            if n < 3:
                continue
            sel = np.unique(np.linspace(0, n - 1, min(n, 120)).round().astype(int))
            pts.append(Pp[ids[sel]] / MM)
            spd.append(us[ids[sel]])
            offs.append(offs[-1] + len(sel))
    cat = lambda a, d=3: np.vstack(a) if a else np.zeros((0, d))  # noqa: E731
    f32, f16 = np.float32, np.float16  # float32 keeps mm coordinates exact over a 2 m domain; values need less
    return {"slice_points": cat(sp).astype(f32), "slice_values": (np.concatenate(sv) if sv else np.zeros(0)).astype(f16),
            "slice_triangles": cat(st).astype(np.int32), "slice_offsets": np.array(so, np.int32),
            "slice_names": np.array(names), "stream_points": cat(pts).astype(f32),
            "stream_offsets": np.array(offs, np.int32),
            "stream_speed": (np.concatenate(spd) if spd else np.zeros(0)).astype(f16)}


def load_surface_field(path) -> dict:
    """``types.load_field`` plus the shared mesh: a field file with a ``mesh`` entry (cfd_p.npz ...) gets its
    ``points`` and ``triangles`` from the file it names (cfd_cp.npz, same folder)."""
    path = Path(path)
    d = T.load_field(path)
    if "points" not in d and "mesh" in d:
        base = T.load_field(path.with_name(str(d["mesh"])))
        d["points"], d["triangles"] = base["points"], base["triangles"]
    return d


# --------------------------------------------------------------------------------------------------- run
def run(ctx: T.RunContext) -> dict:
    spec = ctx.spec or {}
    V, v_st = _num(spec, "analyses.cfd.speed_ms")
    if V is None:
        V, v_st = _num(spec, "aero.cruise_speed_ms")
    if V is None or V <= 0:
        return T.not_evaluated(ID, TITLE, ["analyses.cfd.speed_ms", "aero.cruise_speed_ms"],
                               "Not evaluated: no flow speed in the drone file (set analyses.cfd.speed_ms or "
                               "aero.cruise_speed_ms).")
    alpha, a_st = _num(spec, "analyses.cfd.alpha_deg")
    if alpha is None:
        alpha, a_st = _num(spec, "aero.alpha_deg")
    alpha_default = alpha is None
    if alpha_default:
        alpha, a_st = 0.0, T.USER
    rho, r_st = _num(spec, "environment.rho")
    rho = rho or RHO_DEFAULT
    preset = str(((spec.get("analyses") or {}).get("cfd") or {}).get("preset") or "coarse")
    if preset not in PRESETS:
        return T.not_evaluated(ID, TITLE, ["analyses.cfd.preset"], f"Not evaluated: unknown preset {preset!r}; "
                               "use coarse, medium or fine.")
    its, _ = _num(spec, "analyses.cfd.iterations")
    iterations = int(its) if its and its > 0 else 300
    inst = _find_foam()
    if inst is None:
        return T.result(ID, TITLE, T.ERROR, "OpenFOAM (simpleFoam, snappyHexMesh) was not found, so the CFD "
                        "analysis was not run. Install OpenFOAM (ESI) or set OPENCAE_OPENFOAM.")
    from opencae.engineering import field_analyses as FA
    nproc = FA._cfd_procs(min(os.cpu_count() or 1, 4))
    aref, aref_txt, ar_st = reference_area(ctx.aircraft, spec)
    if not aref or aref <= 0:
        return T.not_evaluated(ID, TITLE, ["aero.ref_area_m2"], "Not evaluated: no reference area could be found.")
    case = Path(ctx.workdir) / "cfd_case"
    out = Path(ctx.out_dir)
    t0 = time.time()
    log_tail = ""
    try:
        info = write_case(case, ctx.aircraft, spec, preset, iterations, nproc, V, alpha, rho)
        timing = {}
        with solver_lock("the CFD run", on_wait=lambda m: ctx.progress(m, 0.0)):
            steps = [("blockMesh", "blockMesh", "log.blockMesh", 0.03),
                     ("surfaceFeatureExtract", "surfaceFeatureExtract", "log.surfaceFeatureExtract", 0.05)]
            if nproc > 1:
                steps += [("decomposePar (background mesh)", "decomposePar -force", "log.decomposeBlock", 0.07),
                          ("snappyHexMesh", FA._foam_mpi(nproc, "snappyHexMesh -overwrite"), "log.snappy", 0.3),
                          ("reconstructParMesh", "reconstructParMesh -constant", "log.reconstructParMesh", 0.35)]
            else:
                steps += [("snappyHexMesh", "snappyHexMesh -overwrite", "log.snappy", 0.3)]
            steps += [("checkMesh", "checkMesh", "log.checkMesh", 0.4)]
            if nproc > 1:
                steps += [("decomposePar", "decomposePar -force", "log.decompose", 0.42),
                          ("simpleFoam", FA._foam_mpi(nproc, "simpleFoam"), "log.simpleFoam", 0.85)]
            else:
                steps += [("simpleFoam", "simpleFoam", "log.simpleFoam", 0.85)]
            for label, cmd, log, frac in steps:
                if ctx.cancelled():
                    raise _Cancelled()
                ctx.progress(f"CFD: {label}", frac)
                ts = time.time()
                rc = _foam(inst, case, cmd, log, cancelled=ctx.cancelled)
                timing[label] = round(time.time() - ts, 1)
                if rc != 0:
                    log_tail = _log_tail(case, log)
                    raise RuntimeError(f"OpenFOAM step '{label}' failed (exit {rc})")
                if label == "surfaceFeatureExtract" and not FA._find_emesh(case):
                    log_tail = _log_tail(case, log)
                    raise RuntimeError("surfaceFeatureExtract wrote no aircraft.eMesh")
                if label == "reconstructParMesh":
                    for pd in case.glob("processor*"):
                        shutil.rmtree(pd)
            if nproc > 1:
                ctx.progress("CFD: reconstruct", 0.87)
                _reconstruct(inst, case, ctx.cancelled)
        ctx.progress("CFD: post-processing", 0.9)
        mq = FA._foam_mesh_profile(case, info["surface_levels"], info["layers"])
        times = sorted((d for d in case.iterdir() if d.is_dir() and FA._isnum(d.name) and float(d.name) > 0),
                       key=lambda d: float(d.name))
        if not times:
            log_tail = _log_tail(case, "log.simpleFoam")
            raise RuntimeError("simpleFoam wrote no results")
        _to_vtk(inst, case, ctx.cancelled)
        wall_cc = FA._foam_wall_cell_centres(case, times[-1].name)
        shutil.rmtree(case / "VTKcell", ignore_errors=True)
        al = math.radians(alpha)
        drag_d, lift_d = flow_directions(alpha)
        hist, last = [], None
        for d in times:
            # _cfd_post builds its flow direction as (cos a, 0, sin a): hand it pi - alpha to get (-cos al, 0, sin al)
            post = FA._cfd_post(case, d.name, rho, V, np.array(lift_d), np.array(drag_d), aref, math.pi - al, wall_cc)
            hist.append([float(d.name), post["cl"], post["cd"]])
            last = post
        import pyvista as pv
        flow = _flow(pv.read(str(FA._foam_vtk_dir(case, "VTKflow", times[-1].name) / "internal.vtu")), V, al,
                     info["domain_m"], info["bbox_m"])
        shutil.rmtree(case / "VTKflow", ignore_errors=True)
        resid = FA._foam_residuals(case / "log.simpleFoam")
        sim_log = (case / "log.simpleFoam").read_text(errors="replace")
        by_residual = "SIMPLE solution converged" in sim_log or (bool(resid) and max(resid.values()) < 1e-4)
        n_it = int(hist[-1][0])
        H = np.array(hist)
        win = H[H[:, 0] >= n_it * 0.9 - 1e-9]
        fc = np.hypot(H[:, 1], H[:, 2])
        fwin = np.hypot(win[:, 1], win[:, 2])
        fchg = float((fwin.max() - fwin.min()) / max(abs(fc[-1]), 1e-9)) if len(win) >= 2 else None
        by_force = fchg is not None and fchg < 0.01
        converged = bool(by_residual or by_force)
        cl, cd = last["cl"], last["cd"]
        q = 0.5 * rho * V * V
        lift_n, drag_n = cl * q * aref, cd * q * aref
        pts, tri = last["points_mm"], last["tri"]
        fields = []
        spec_fields = [("cfd_cp.npz", "cp", last["cp"], "pressure coefficient", "-"),
                       ("cfd_p.npz", "p_static", last["fields"]["p_static"], "static pressure (gauge)", "Pa"),
                       ("cfd_u_wall.npz", "u_near_wall", last["fields"]["u_near_wall"], "near-wall speed", "m/s"),
                       ("cfd_tau_wall.npz", "wall_shear", last["fields"]["wall_shear"], "wall shear stress", "Pa")]
        for fname, key, vals, qty, unit in spec_fields:
            if fname == "cfd_cp.npz":  # the one file with the mesh (and the slices and streamlines)
                T.save_field(out / fname, vals, pts, tri, None, **flow)
            else:
                T.save_field(out / fname, vals, mesh="cfd_cp.npz")
            fields.append({"file": fname, "key": key, "quantity": qty, "unit": unit})
        ncell = int(mq["cells"])
        mesh_ok = bool(mq["checkMesh_ok"])
        basis = _weakest([v_st, a_st, r_st or T.USER])
        st = T.OK if (converged and mesh_ok) else T.WATCH
        coarse = preset == "coarse"
        asp = (f"angle of attack {alpha:g} deg (default 0: set analyses.cfd.alpha_deg or aero.alpha_deg)"
               if alpha_default else f"angle of attack {alpha:g} deg")
        summ = (f"Whole-aircraft airflow at {V:g} m/s, {asp}: lift {lift_n:.2f} N, drag {drag_n:.2f} N "
                f"(C_L {cl:.3f}, C_D {cd:.3f} on {aref_txt}, {aref:.4f} m2). "
                f"{'Coarse mesh' if coarse else preset.capitalize() + ' mesh'} of {ncell / 1e3:.0f} k cells, "
                f"{'converged' if converged else 'not converged after ' + str(n_it) + ' iterations'}; "
                f"propellers are not modelled.")
        if not mesh_ok:
            summ += " The mesh check reported issues."
        mm = lambda v, u="", s=st, l="": T.metric(v, u, s, basis, l)  # noqa: E731
        metrics = {"cl": mm(cl, "", st, "Lift coefficient"), "cd": mm(cd, "", st, "Drag coefficient"),
                   "ld": mm(cl / cd if cd > 0 else None, "", st, "Lift to drag"),
                   "lift_n": mm(lift_n, "N", st, "Lift"), "drag_n": mm(drag_n, "N", st, "Drag"),
                   "converged": mm(1.0 if converged else 0.0, "", T.OK if converged else T.WATCH, "Converged"),
                   "cells": mm(ncell, "", T.OK, "Cells"), "iterations": mm(n_it, "", T.OK, "Iterations")}
        solver = {"name": "OpenFOAM simpleFoam (k-omega SST)", "converged": converged, "iterations": n_it,
                  "iterations_requested": iterations, "cells": ncell, "runtime_s": round(time.time() - t0, 1),
                  "procs": nproc, "step_seconds": timing, "converged_by_residuals": bool(by_residual),
                  "force_change_last_10pct": fchg, "residuals_final": resid, "checkMesh_ok": mesh_ok,
                  "openfoam": inst.describe()}
        extra = {"preset": preset, "speed_ms": V, "alpha_deg": alpha, "alpha_is_default": alpha_default,
                 "rho": rho, "reference_area_m2": aref, "reference_area_basis": aref_txt,
                 "omitted_parts": info["omitted_parts"], "domain_m": info["domain_m"],
                 "location_in_mesh_m": info["location_in_mesh_m"], "history": H.tolist(),
                 "history_columns": ["iteration", "C_L", "C_D"], "mesh": {k: mq[k] for k in mq if k in (
                     "cells", "cell_types", "max_non_orthogonality", "max_skewness", "layer_coverage_pct",
                     "checkMesh")},
                 "y_plus": last["y_plus_stats"], "separated_pct": last["separated_area_pct"],
                 "assumptions": ["whole aircraft, both sides meshed, no symmetry plane", "propellers not modelled "
                                 "(parts with role prop left out)", f"steady RANS k-omega SST, Spalding wall "
                                 f"function, {info['layers']} prism layers", f"{preset} mesh: indicative, not a "
                                 "mesh-independence study", "incompressible air, no sideslip"]}
        res = T.result(ID, TITLE, st, summ, metrics, fields=fields, solver=solver, extra=extra)
        if not ctx.keep_cases:
            shutil.rmtree(case, ignore_errors=True)
        return res
    except _Cancelled:
        return T.result(ID, TITLE, T.ERROR, "The CFD run was cancelled.")
    except Exception as exc:  # solver failures are reported, never hidden
        for pd in case.glob("processor*"):
            shutil.rmtree(pd, ignore_errors=True)
        msg = f"CFD failed: {exc}"
        return T.result(ID, TITLE, T.ERROR, msg + " (OpenFOAM log tail below)", log=(msg + "\n" + log_tail))
