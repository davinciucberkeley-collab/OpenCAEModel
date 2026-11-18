"""Field analyses of the K09-L with contour results: structural, thermal and fluid.

Every analysis here runs a real solver on the configuration's CAD geometry and returns a *field record*: the
surface of the analysed part (triangles in the aircraft frame, mm) with nodal values, the critical region,
and the failure parameter evaluated against its allowable. The same record feeds the desktop app and the
website (opencae.web).

    structural  fea_foot        PETG outrigger foot under the whole touchdown load on one foot  (CalculiX)
                fea_motor_tube  / fea_spar: surface fields read from the component FEA runs        (CalculiX)
    thermal     thermal_mount   PETG motor-mount elbow heated by the motor base                  (CalculiX)
    fluid       cfd_cruise      half aircraft at the cruise trim point, RANS k-omega SST          (OpenFOAM)

Inputs that are estimates (material allowables, heat-transfer coefficients, motor base temperature) are
labelled in the record; a coarse CFD mesh is stated as such. Nothing here is test evidence.
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

from opencae.engineering.model import EngineeringModel

MM = 1e-3
CCX = shutil.which("ccx") or "ccx"

# provisional material data (estimates; replace with coupon tests)
PETG = {"name": "PETG (FDM print)", "E": 2100.0, "nu": 0.38, "k": 0.20, "rho": 1.27e-9,
        "strength_MPa": 25.0, "strength_note": "FDM PETG across layers, open literature 20-35 MPa (estimate)",
        "T_allow_C": 65.0, "T_note": "PETG glass transition about 80 °C; 15 K margin for creep under the motor "
                                    "screws (estimate)"}


# =============================================================================================== utilities
def _ccx_env(workdir: Path) -> dict:
    env = dict(os.environ)
    env["OMP_NUM_THREADS"] = str(max(1, (os.cpu_count() or 2)))
    return env


def _run(cmd, cwd: Path, log: Path, env=None, timeout=3600, shell=False) -> int:
    with open(log, "w") as fh:
        p = subprocess.run(cmd, cwd=cwd, stdout=fh, stderr=subprocess.STDOUT, env=env, timeout=timeout, shell=shell,
                           executable="/bin/bash" if shell else None)
    return p.returncode


def _tet_mesh(model: EngineeringModel, bodies, size: float, workdir: Path):
    """Quadratic tetra mesh (mm) of the closed tessellated bodies via gmsh (STL -> volume)."""
    import gmsh
    workdir.mkdir(parents=True, exist_ok=True)
    stl = workdir / "part.stl"
    _write_stl(model, bodies, stl)
    gmsh.initialize(interruptible=False)
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        def build(remesh: bool):
            gmsh.clear()
            gmsh.merge(str(stl))
            if remesh:  # re-parametrise the CAD facets and remesh the surface at the target size
                gmsh.model.mesh.classifySurfaces(40 * math.pi / 180, True, True, math.pi)
                gmsh.model.mesh.createGeometry()
            s = gmsh.model.getEntities(2)
            sl = gmsh.model.geo.addSurfaceLoop([e[1] for e in s])
            gmsh.model.geo.addVolume([sl])
            gmsh.model.geo.synchronize()
            gmsh.option.setNumber("Mesh.MeshSizeMax", size)
            gmsh.option.setNumber("Mesh.MeshSizeMin", size / 3)
            gmsh.option.setNumber("Mesh.ElementOrder", 2)
            gmsh.option.setNumber("Mesh.SecondOrderLinear", 1)  # mid-nodes on the facets: no curving of flat CAD
            gmsh.option.setNumber("Mesh.Optimize", 1)
            gmsh.model.mesh.generate(3)

        def has_tets() -> bool:
            et = gmsh.model.mesh.getElements(3)[0]
            return any(int(t) == 11 for t in et)

        try:
            build(True)
            ok = has_tets()
        except Exception:
            ok = False
        if not ok:  # remeshing failed (thin features): keep the CAD's own facets as the surface mesh,
            gmsh.finalize()  # then split uniformly (exact for flat facets) and raise to quadratic elements
            gmsh.initialize(interruptible=False)  # (a fresh session: a failed attempt leaves state behind)
            gmsh.option.setNumber("General.Terminal", 0)
            gmsh.merge(str(stl))
            s = gmsh.model.getEntities(2)
            sl = gmsh.model.geo.addSurfaceLoop([e[1] for e in s])
            gmsh.model.geo.addVolume([sl])
            gmsh.model.geo.synchronize()
            gmsh.model.mesh.generate(3)
            for _ in range(2):
                gmsh.model.mesh.refine()
            gmsh.model.mesh.setOrder(2)
        tags, xyz, _ = gmsh.model.mesh.getNodes()
        nodes = xyz.reshape(-1, 3)
        idx = {int(t): i for i, t in enumerate(tags)}
        et, _, en = gmsh.model.mesh.getElements(3)
        k = [int(t) for t in et].index(11)  # 10-node tetrahedron
        el = np.array([idx[int(t)] for t in en[k]]).reshape(-1, 10)
    finally:
        gmsh.finalize()
    # gmsh 10-node tet: edges (0,1)(1,2)(0,2)(0,3)(2,3)(1,3); CalculiX C3D10: (1,2)(2,3)(1,3)(1,4)(2,4)(3,4)
    el = el[:, [0, 1, 2, 3, 4, 5, 6, 7, 9, 8]]
    a, b, c, d = (nodes[el[:, i]] for i in range(4))
    neg = np.einsum("ij,ij->i", np.cross(b - a, c - a), d - a) < 0
    if neg.any():  # positive orientation: swap corners 2 and 3 with their edge nodes
        el[neg] = el[neg][:, [0, 2, 1, 3, 6, 5, 4, 7, 9, 8]]
    used = np.unique(el)
    remap = -np.ones(len(nodes), int)
    remap[used] = np.arange(len(used))
    return nodes[used], remap[el]


def _write_stl(model: EngineeringModel, bodies, path: Path, scale: float = 1.0, clip_y0: bool = False) -> None:
    with open(path, "w") as fh:
        fh.write("solid part\n")
        for b in bodies:
            P, T = model.body_mesh(b)
            P = P * scale
            for t in T:
                a, bb, c = P[t[0]], P[t[1]], P[t[2]]
                n = np.cross(bb - a, c - a)
                nn = np.linalg.norm(n)
                n = n / nn if nn > 0 else n
                fh.write(f" facet normal {n[0]:.6e} {n[1]:.6e} {n[2]:.6e}\n  outer loop\n")
                for v in (a, bb, c):
                    fh.write(f"   vertex {v[0]:.6e} {v[1]:.6e} {v[2]:.6e}\n")
                fh.write("  endloop\n endfacet\n")
        fh.write("endsolid part\n")


def _surface(nodes: np.ndarray, el10: np.ndarray):
    """Boundary triangles (corner nodes) of a tet mesh and the node ids they use."""
    import pyvista as pv
    corners = el10[:, :4]
    cells = np.hstack([np.full((len(corners), 1), 4), corners]).ravel()
    g = pv.UnstructuredGrid(cells, np.full(len(corners), 10, np.uint8), nodes)
    g.point_data["nid"] = np.arange(len(nodes))
    s = g.extract_surface(algorithm="dataset_surface").triangulate()
    tri = s.faces.reshape(-1, 4)[:, 1:]
    return np.asarray(s.point_data["nid"])[tri]


TET_POOR_SICN = 0.2  # gmsh minSICN below this: a poorly shaped tetrahedron


def _gmsh_sicn(nodes: np.ndarray, corners: np.ndarray) -> Optional[np.ndarray]:
    """gmsh minSICN of the linear tetrahedra (corner nodes) of a mesh; None if gmsh is not usable here."""
    try:
        import gmsh
    except Exception:
        return None
    own = not gmsh.isInitialized()
    prev = None
    try:
        if own:
            gmsh.initialize(interruptible=False)
            gmsh.option.setNumber("General.Terminal", 0)
        else:
            prev = gmsh.model.getCurrent()
        gmsh.model.add("_opencae_quality")
        tag = gmsh.model.addDiscreteEntity(3)
        gmsh.model.mesh.addNodes(3, tag, np.arange(1, len(nodes) + 1, dtype=np.uint64),
                                 np.asarray(nodes, float).ravel())
        et = np.arange(1, len(corners) + 1, dtype=np.uint64)
        gmsh.model.mesh.addElementsByType(tag, 4, et, (np.asarray(corners, np.int64) + 1).astype(np.uint64).ravel())
        q = np.asarray(gmsh.model.mesh.getElementQualities(et, "minSICN"), float)
        gmsh.model.remove()
        if prev:
            gmsh.model.setCurrent(prev)
        return q
    except Exception:
        return None
    finally:
        if own and gmsh.isInitialized():
            gmsh.finalize()


def tet_mesh_quality(nodes: np.ndarray, el: np.ndarray, target_size_mm: Optional[float] = None) -> dict:
    """Quality profile of a tetrahedral mesh (mm): edge lengths and the gmsh minSICN distribution of the
    corner tetrahedra (1 = regular, <= 0 = inverted). Mid-side nodes are on straight edges here, so the
    corner-tet shape is the element shape."""
    nodes = np.asarray(nodes, float)
    c = np.asarray(el)[:, :4]
    pairs = ((0, 1), (1, 2), (0, 2), (0, 3), (1, 3), (2, 3))
    L = np.stack([np.linalg.norm(nodes[c[:, a]] - nodes[c[:, b]], axis=1) for a, b in pairs], 1)
    q = _gmsh_sicn(nodes, c)
    metric = "gmsh minSICN (signed inverse condition number, 1 = regular tet)"
    if q is None:  # fallback: gamma = 3 r_in / R_circ (1 = regular), computed here
        metric = "gamma 3 r_in / R_circ (1 = regular tet; gmsh not available)"
        a, b, cc, d = (nodes[c[:, i]] for i in range(4))
        sv = np.einsum("ij,ij->i", np.cross(b - a, cc - a), d - a) / 6  # signed: < 0 is inverted
        vol = np.abs(sv)
        area = sum(0.5 * np.linalg.norm(np.cross(y - x, z - x), axis=1) for x, y, z in
                   ((a, b, cc), (a, b, d), (a, cc, d), (b, cc, d)))
        r_in = 3 * vol / np.maximum(area, 1e-30)
        # circumradius: R = sqrt((aA+bB+cC)(aA+bB-cC)(aA-bB+cC)(-aA+bB+cC)) / (24 V), products of opposite edges
        p1, p2, p3 = L[:, 0] * L[:, 5], L[:, 1] * L[:, 3], L[:, 2] * L[:, 4]
        prod = np.clip((p1 + p2 + p3) * (p1 + p2 - p3) * (p1 - p2 + p3) * (-p1 + p2 + p3), 0, None)
        R = np.sqrt(prod) / np.maximum(24 * vol, 1e-30)
        q = np.clip(3 * r_in / np.maximum(R, 1e-30), 0, 1) * np.where(sv > 0, 1.0, -1.0)
    hist, edges = np.histogram(np.clip(q, 0, 1), bins=10, range=(0.0, 1.0))  # inverted ones in the first bin
    poor = int((q < TET_POOR_SICN).sum())
    return {"element_type": "C3D10 (quadratic 10-node tetrahedron)",
            "target_size_mm": None if target_size_mm is None else float(target_size_mm),
            "edge_length_mm": {"min": float(L.min()), "mean": float(L.mean()), "max": float(L.max())},
            "quality": {"metric": metric, "min": float(q.min()), "p5": float(np.percentile(q, 5)),
                        "mean": float(q.mean()), "histogram": hist.astype(int).tolist(),
                        "bins": [round(float(x), 2) for x in edges], "poor_threshold": TET_POOR_SICN,
                        "poor_elements": poor},
            "poor_elements": poor, "poor_pct": float(100.0 * poor / max(len(q), 1)),
            "inverted_elements": int((q <= 0).sum())}


def _ccx_deck_header(nodes, el10, title: str) -> list[str]:
    L = [f"** {title}", "*NODE, NSET=NALL"]
    L += [f"{i + 1}, {x:.6f}, {y:.6f}, {z:.6f}" for i, (x, y, z) in enumerate(nodes)]
    L.append("*ELEMENT, TYPE=C3D10, ELSET=EALL")
    for i, e in enumerate(el10):
        n = e + 1
        L.append(f"{i + 1}, " + ", ".join(str(int(v)) for v in n[:8]) + ",")
        L.append(", ".join(str(int(v)) for v in n[8:]))
    return L


def _nset(name: str, ids) -> list[str]:
    ids = [int(i) + 1 for i in ids]
    L = [f"*NSET, NSET={name}"]
    for k in range(0, len(ids), 12):
        L.append(", ".join(map(str, ids[k:k + 12])))
    return L


def _read_frd_nodal(path: Path, block: str, n: int) -> np.ndarray:
    from opencae.solver.calculix.frd import read_frd
    f = read_frd(path)
    b = f.get(block)
    if b is None:
        raise RuntimeError(f"{block} not in {path.name}")
    out = np.zeros((n, b.values.shape[1]))
    out[b.node_ids - 1] = b.values
    return out


def _von_mises(s):
    sx, sy, sz, txy, tyz, txz = s.T
    return np.sqrt(0.5 * ((sx - sy) ** 2 + (sy - sz) ** 2 + (sz - sx) ** 2) + 3 * (txy ** 2 + tyz ** 2 + txz ** 2))


def _critical(values: np.ndarray, tri: np.ndarray, frac: float = 0.8, higher_is_worse: bool = True) -> dict:
    """Critical region: surface nodes at >= frac of the worst value (or <= for lower-is-worse fields)."""
    v = values
    worst = v.max() if higher_is_worse else v.min()
    ref = v.min() if higher_is_worse else v.max()
    thr = ref + frac * (worst - ref)
    # at least the worst 3 % of the surface, so a single singular node never hides the hot spot
    thr = min(thr, float(np.percentile(v, 97))) if higher_is_worse else max(thr, float(np.percentile(v, 3)))
    mask = v >= thr if higher_is_worse else v <= thr
    return {"threshold": float(thr), "node_mask": mask}


def field_record(name, title, discipline, part, nodes_mm, tri, values, unit, quantity, *, critical, failure,
                 solver, mesh_info, load_case, assumptions, evidence, extra=None, fields=None,
                 displacement=None) -> dict:
    """Common record: surface + nodal field + critical region + failure parameter.

    ``values`` is the primary field (the one the failure parameter and critical region refer to). Optional
    secondary nodal fields over the same nodes: ``fields`` = [{"key", "quantity", "unit", "values",
    "cmap" (turbo|ylorrd|coolwarm|viridis, optional), "symmetric" (bool, optional)}], e.g. displacement
    magnitude, strain, pressure, near-wall velocity. ``displacement`` (n, 3) mm: nodal displacement vectors of
    a structural run, for the deformed shape and the loading animation."""
    used = np.unique(tri)
    remap = -np.ones(int(tri.max()) + 1, int)
    remap[used] = np.arange(len(used))
    i_worst = int(np.argmax(values[used]) if critical.get("higher_is_worse", True) else np.argmin(values[used]))
    sec = []
    for fd in fields or []:
        v = np.asarray(fd["values"], float)[used]
        sec.append({**{k: x for k, x in fd.items() if k != "values"}, "values": v,
                    "range": [float(np.nanmin(v)), float(np.nanmax(v))]})
    disp = None if displacement is None else np.asarray(displacement, float)[used]
    return {"id": name, "title": title, "discipline": discipline, "part": part,
            "surface": {"points_mm": np.asarray(nodes_mm)[used], "tri": remap[tri], "values": np.asarray(values)[used],
                        "fields": sec, "displacement": disp},
            "quantity": quantity, "unit": unit,
            "range": [float(np.min(values[used])), float(np.max(values[used]))],
            "worst_location_mm": np.asarray(nodes_mm)[used][i_worst].round(2).tolist(),
            "critical": {"threshold": critical["threshold"], "higher_is_worse": critical.get("higher_is_worse", True),
                         "node_mask": np.asarray(critical["node_mask"])[used], "text": critical.get("text", "")},
            "failure": failure, "solver": solver, "mesh": mesh_info, "load_case": load_case,
            "assumptions": assumptions, "evidence": evidence, "extra": extra or {},
            "time": time.strftime("%Y-%m-%d %H:%M:%S")}


def _status(sf: float, line: float = 1.5) -> str:
    return "fail" if sf < 1.0 else ("watch" if sf < line else "ok")


# =============================================================================================== structural
def run_foot_fea(engine, workdir: Path, size: float = 2.5) -> dict:
    """Outrigger foot (PETG) under the whole 1 m/s touchdown load on one foot, pushed in through the pad face,
    held where the foot clamps the fin (the fin-slot walls). Linear static, C3D10."""
    m = engine.model
    r = engine.results
    land = m.option("landing")
    feet = [b for b in m.active_bodies() if b.component == land.bom_component and "PETG" in b.name]
    if not feet:
        raise RuntimeError("no PETG foot body in this configuration")
    foot = max(feet, key=lambda b: (b.centroid[1], b.centroid[2]))  # right, upper foot
    nodes, el = _tet_mesh(m, [foot], size, workdir)
    tri = _surface(nodes, el)
    F = float(r["landing_force_total"].value)  # whole touchdown load on one foot (tilted touchdown)
    xmax = nodes[:, 0].max()
    surf_nodes = np.unique(tri)
    load_nodes = surf_nodes[nodes[surf_nodes, 0] >= xmax - 0.25]
    # fin-slot walls: surface nodes on the slot faces |y| = 317 / 323 (+-0.3 mm) inside the fin span
    ay = np.abs(nodes[surf_nodes, 1])
    fin_z = max(float(np.abs(m.body_mesh(b)[0][:, 2]).max()) for b in m.bodies if b.component.startswith("Fin Depron"))
    slot = surf_nodes[((np.abs(ay - 317.0) < 0.35) | (np.abs(ay - 323.0) < 0.35))
                      & (np.abs(nodes[surf_nodes, 2]) <= fin_z + 0.3)]
    if len(slot) < 6:
        raise RuntimeError("fin-slot faces not found on the foot mesh")
    L = _ccx_deck_header(nodes, el, "K09-L outrigger foot, touchdown load")
    L += _nset("FIX", slot) + _nset("LOAD", load_nodes)
    L += ["*MATERIAL, NAME=PETG", "*ELASTIC", f"{PETG['E']}, {PETG['nu']}",
          "*SOLID SECTION, ELSET=EALL, MATERIAL=PETG", "*STEP", "*STATIC", "*BOUNDARY", "FIX, 1, 3, 0.0",
          "*CLOAD"]
    fn = -F / len(load_nodes)  # pushes the pad face forward (-x): the ground reaction on a nose-up landing
    L += [f"{int(i) + 1}, 1, {fn:.6e}" for i in load_nodes]
    L += ["*NODE FILE", "U", "*EL FILE", "S", "*END STEP"]
    (workdir / "foot.inp").write_text("\n".join(L) + "\n")
    t0 = time.time()
    rc = _run([CCX, "-i", "foot"], workdir, workdir / "ccx.log", env=_ccx_env(workdir))
    if rc != 0 or not (workdir / "foot.frd").exists():
        raise RuntimeError(f"CalculiX failed ({rc}), see {workdir / 'ccx.log'}")
    S = _read_frd_nodal(workdir / "foot.frd", "STRESS", len(nodes))
    U = _read_frd_nodal(workdir / "foot.frd", "DISP", len(nodes))
    vm = _von_mises(S)
    smax = float(vm[np.unique(tri)].max())
    p99 = float(np.percentile(vm[np.unique(tri)], 99))
    sf = PETG["strength_MPa"] / smax
    crit = _critical(vm, tri, 0.6)
    crit["text"] = f"surface where von Mises >= {crit['threshold']:.2f} MPa (60 % of the peak)"
    return field_record(
        "fea_foot", "Outrigger foot - touchdown", "structural", land.bom_component, nodes, tri, vm, "MPa",
        "von Mises stress", critical=crit,
        failure={"criterion": "von Mises vs PETG strength", "value": smax, "allowable": PETG["strength_MPa"],
                 "unit": "MPa", "safety_factor": sf, "status": _status(sf, 2.0), "line": 2.0,
                 "p99": p99,
                 "text": f"peak {smax:.1f} MPa vs {PETG['strength_MPa']:.0f} MPa (estimate): SF {sf:.2f} "
                         f"(line 2.0 for printed parts). The peak sits where the foot is held at the fin slot "
                         f"(a support corner); 99 % of the surface stays below {p99:.1f} MPa"},
        solver={"name": "CalculiX ccx", "seconds": round(time.time() - t0, 1), "returncode": rc,
                "max_displacement_mm": float(np.linalg.norm(U, axis=1).max())},
        mesh_info={"elements": int(len(el)), "nodes": int(len(nodes)), "type": "C3D10", "size_mm": size,
                   **tet_mesh_quality(nodes, el, size)},
        load_case=f"whole 1 m/s touchdown load {F:.0f} N on one foot, through the pad face; held at the fin-slot "
                  f"walls",
        assumptions=["PETG linear elastic E 2.1 GPa (estimate)", PETG["strength_note"],
                     "all of the touchdown load on one foot (tilted touchdown): conservative",
                     "foot held rigidly at the slot walls (the Depron fin is far softer: real stresses at the "
                     "slot are lower, the overhang root sees the full moment)"],
        evidence="simulation on estimated material data")


def _load_text(load: dict) -> str:
    v = load.get("value_N")
    try:
        return f"{load.get('name')} {float(v):.1f} N"
    except (TypeError, ValueError):
        return str(load.get("name"))


def _project_mesh_size(proj) -> Optional[float]:
    """Target element size (mm, mesh_settings.size_max) of a saved OpenCAE project, if it records one."""
    import json
    try:
        d = json.loads(Path(proj).read_text(encoding="utf-8"))
    except Exception:
        return None
    stack = [d]
    while stack:
        x = stack.pop()
        if isinstance(x, dict):
            ms = x.get("mesh_settings")
            if isinstance(ms, dict) and isinstance(ms.get("size_max"), (int, float)):
                return float(ms["size_max"])
            stack.extend(x.values())
        elif isinstance(x, list):
            stack.extend(x)
    return None


def fea_from_component_run(engine, nid: str) -> Optional[dict]:
    """Surface stress field of an existing component FEA run (motor tube / spar) from its CalculiX files."""
    h = engine.heavy_status(nid)
    rec = h.get("record") or {}
    res = rec.get("result") or {}
    proj = res.get("project")
    if not proj or not Path(proj).exists():
        return None
    data = Path(str(proj)).with_suffix("").as_posix() + "_data"
    meshes = sorted(Path(data, "mesh").glob("mesh_*.npz"))
    runs = sorted(Path(data, "results").glob("run_*/job.frd"))
    if not meshes or not runs:
        return None
    d = np.load(meshes[-1], allow_pickle=True)
    nodes, el = d["nodes"], d["elements"]
    if el.min() == 1:
        el = el - 1
    tri = _surface(nodes, el)
    S = _read_frd_nodal(runs[-1], "STRESS", len(nodes))
    vm = _von_mises(S)
    sf = (res.get("safety_factor") or {}).get("value")
    gov = res.get("governing_axial_stress_MPa")
    allow = (sf or 0) * (gov or 0)
    crit = _critical(vm, tri, 0.6)
    crit["text"] = f"surface where von Mises >= {crit['threshold']:.1f} MPa (60 % of the peak)"
    part = res.get("component", nid)
    title = "Motor tube - full thrust" if nid == "fea_motor_tube" else "Wing spar - 4 g lift"
    return field_record(
        nid, title, "structural", part, nodes, tri, vm, "MPa", "von Mises stress", critical=crit,
        failure={"criterion": "governing axial stress vs CF laminate allowable", "value": gov, "allowable": allow,
                 "unit": "MPa", "safety_factor": sf, "status": _status(sf or 0), "line": 1.5,
                 "text": f"governing axial stress {gov:.1f} MPa vs {allow:.0f} MPa (estimated CF allowable): "
                         f"SF {sf:.1f}; the peak von Mises {float(vm.max()):.1f} MPa sits at the clamp edge"},
        solver={"name": "CalculiX ccx", "seconds": rec.get("seconds"), "converged": res.get("converged"),
                "equilibrium": res.get("equilibrium"), "beam_check": res.get("beam_check")},
        mesh_info={"elements": int(len(el)), "nodes": int(len(nodes)), "type": str(d["element_type"]),
                   **tet_mesh_quality(nodes, el, _project_mesh_size(proj))},
        load_case="; ".join(_load_text(l) for l in res.get("loads", []) if isinstance(l, dict)),
        assumptions=list(res.get("assumptions", []))[:6],
        evidence="simulation on estimated laminate data",
        extra={"frame": "component frame (rebuilt tube); highlighted on the aircraft by its bodies"})


# =============================================================================================== thermal
def run_mount_thermal(engine, workdir: Path, size: float = 2.5, t_amb: float = 35.0, h_conv: float = 25.0) -> dict:
    """Steady conduction in the PETG motor-mount elbow: the motor base face held at the motor's steady hover
    temperature, convection to hot-day air on every other face."""
    m = engine.model
    r = engine.results
    mat = m.option("mount_material")
    mat_allow, mat_k = float(mat.q("T_allow").value), float(mat.q("k").value)
    mat_name = "ASA" if mat.id == "asa" else "PETG"
    mounts = [b for b in m.active_bodies() if b.component.startswith("Motor mount PETG")]
    if not mounts:
        raise RuntimeError("no motor mount body")
    mount = max(mounts, key=lambda b: (b.centroid[1], b.centroid[2]))
    nodes, el = _tet_mesh(m, [mount], size, workdir)
    tri = _surface(nodes, el)
    # motor base contact: mount surface within 1.0 mm of the motor base body
    base = [b for b in m.active_bodies() if "Motor SunnySky" in b.component and b.name == "Base"
            and np.linalg.norm(b.centroid[1:] - mount.centroid[1:]) < 60]
    from scipy.spatial import cKDTree
    bp = m.body_mesh(base[0])[0]
    tree = cKDTree(bp)
    surf = np.unique(tri)
    d, _ = tree.query(nodes[surf])
    hot = surf[d <= 1.2]
    if len(hot) < 6:
        raise RuntimeError("motor contact face not found on the mount mesh")
    # motor temperature: hover loss into the motor's lumped thermal resistance (same model as the flight sim)
    p_loss = float(r["motor_loss_hover"].value) if "motor_loss_hover" in r else 5.0
    r_th = 2.5  # K/W, outrunner in prop wash (estimate, the flight sim's motor model value)
    t_motor = t_amb + p_loss * r_th
    film_faces = []
    hot_set = set(int(i) for i in hot)
    # element faces on the boundary that are not in the contact: *FILM on them (C3D10 face numbering)
    fdef = {1: (0, 1, 2), 2: (0, 3, 1), 3: (1, 3, 2), 4: (2, 3, 0)}
    from collections import Counter
    cnt = Counter()
    for e in el:
        for f in fdef.values():
            cnt[tuple(sorted(int(e[i]) for i in f))] += 1
    for k, e in enumerate(el):
        for fid, f in fdef.items():
            key = tuple(sorted(int(e[i]) for i in f))
            if cnt[key] == 1 and not all(n in hot_set for n in key):
                film_faces.append((k + 1, fid))
    L = _ccx_deck_header(nodes, el, "K09-L motor mount, steady heat conduction")
    L += _nset("HOT", hot)
    L += ["*MATERIAL, NAME=PETG", "*CONDUCTIVITY", f"{mat_k * 1e-3:.6e}",  # W/(mm K)
          "*SOLID SECTION, ELSET=EALL, MATERIAL=PETG", "*INITIAL CONDITIONS, TYPE=TEMPERATURE", f"NALL, {t_amb}",
          "*STEP", "*HEAT TRANSFER, STEADY STATE", "*BOUNDARY", f"HOT, 11, 11, {t_motor:.3f}", "*FILM"]
    L += [f"{e}, F{f}, {t_amb}, {h_conv * 1e-6:.6e}" for e, f in film_faces]  # W/(mm^2 K)
    L += ["*NODE FILE", "NT", "*END STEP"]
    (workdir / "mount.inp").write_text("\n".join(L) + "\n")
    t0 = time.time()
    rc = _run([CCX, "-i", "mount"], workdir, workdir / "ccx.log", env=_ccx_env(workdir))
    if rc != 0 or not (workdir / "mount.frd").exists():
        raise RuntimeError(f"CalculiX failed ({rc}), see {workdir / 'ccx.log'}")
    T = _read_frd_nodal(workdir / "mount.frd", "NDTEMP", len(nodes))[:, 0]
    tmax = float(T[surf].max())
    allow = mat_allow
    margin = allow - tmax
    hot_area = float(np.mean(T[surf] > allow)) * 100
    crit = _critical(T, tri, 0.75)
    crit["text"] = f"surface above {crit['threshold']:.1f} °C (75 % of the way from the coolest to the hottest point)"
    st = "fail" if tmax > allow else ("watch" if margin < 10 else "ok")
    return field_record(
        "thermal_mount", f"Motor mount ({mat_name}) - hover heat", "thermal", mount.component, nodes, tri, T, "°C",
        "temperature",
        critical=crit,
        failure={"criterion": f"{mat_name} temperature vs softening limit", "value": tmax, "allowable": allow,
                 "unit": "°C",
                 "safety_factor": None, "margin": margin, "status": st, "line": 10.0,
                 "text": f"hottest point {tmax:.1f} °C vs {allow:.0f} °C (estimate): margin {margin:.1f} K; "
                         f"{hot_area:.0f} % of the surface above the limit"},
        solver={"name": "CalculiX ccx (heat transfer)", "seconds": round(time.time() - t0, 1), "returncode": rc},
        mesh_info={"elements": int(len(el)), "nodes": int(len(nodes)), "type": "C3D10", "size_mm": size,
                   **tet_mesh_quality(nodes, el, size)},
        load_case=f"steady hover on a {t_amb:.0f} °C day: motor base at {t_motor:.1f} °C "
                  f"(hover loss {p_loss:.1f} W per motor x {r_th} K/W); convection h {h_conv:.0f} W/m²K elsewhere",
        assumptions=[f"motor base contact face held at the motor temperature (no contact resistance): conservative",
                     f"h = {h_conv:.0f} W/m²K in the prop wash (estimate)", f"motor thermal resistance {r_th} K/W "
                     "(estimate, flight-sim motor model)", f"{mat_name} k = {mat_k:.2f} W/mK (estimate)",
                     mat.q("T_allow").ref + " (estimate)"],
        evidence="simulation on estimated thermal data",
        extra={"t_motor_C": t_motor, "t_amb_C": t_amb})


# =============================================================================================== fluid
def _foam(cmd: str, case: Path, log: str, timeout=7200) -> int:
    full = f"source /usr/share/openfoam/etc/bashrc >/dev/null 2>&1; cd '{case}' && {cmd}"
    return _run(full, case, case / log, shell=True, timeout=timeout)


CFD_BASE_CELL_M = 0.1  # blockMesh cell (m); snappy level L halves it L times
CFD_LAYERS = 3


def run_cfd_cruise(engine, workdir: Path, iterations: int = 500, level=(4, 5), procs: int = 1,
                   layers: int = CFD_LAYERS, write_every: int = 50, postprocess_only: bool = False) -> dict:
    """Half aircraft (y >= 0, symmetry plane) at the cruise trim point of the flight model, steady RANS
    (simpleFoam, k-omega SST, Spalding wall function) on a snappyHexMesh mesh with prism layers on the
    aircraft and explicit feature-edge snapping (trailing edges, elevon edges, fin and tube edges).
    Returns the surface pressure coefficient field, lift and drag with their history over the iterations,
    the separated-flow region (reversed wall shear), the mesh profile and the flow-visualisation data
    (a slice plane and streamlines). ``postprocess_only`` re-evaluates a finished case already in ``workdir``
    (same configuration and settings) without meshing or solving again."""
    m = engine.model
    r = engine.results
    V = float(r["v_cruise"].value)
    cl_req = float(r["cl_cruise"].value)
    rho = float(m.params["rho"].value)
    from opencae.engineering import calcs
    wr = calcs.wing_reference(m)
    S, b, c = wr["area"].value, wr["span"].value, wr["chord"].value
    AR = b * b / S
    cl0 = float(m.params["cl0"].value) if "cl0" in m.params else 0.05
    cla = 2 * math.pi * AR / (2 + math.sqrt(AR * AR + 4)) * (float(m.params["cla_factor"].value)
                                                            if "cla_factor" in m.params else 1.0)
    alpha = (cl_req - cl0) / cla  # flight-model trim: CL = cl0 + CLa alpha
    case = Path(workdir)
    if postprocess_only and not (case / "log.simpleFoam").exists():
        raise RuntimeError(f"no finished OpenFOAM case in {case} to post-process")
    if postprocess_only:  # keep the case: write the dictionaries to a scratch folder that is thrown away
        case = case / "_rewrite"
    if case.exists():
        shutil.rmtree(case)
    (case / "constant" / "triSurface").mkdir(parents=True)
    (case / "system").mkdir()
    (case / "0").mkdir()
    keep = ("Wing core", "Elevon inboard", "Elevon outboard", "Fin Depron", "Pod shell", "Pod hatch", "Motor tube")
    bodies = [bb for bb in m.active_bodies() if bb.component.startswith(keep) and bb.centroid[1] > -60]
    _write_stl(m, bodies, case / "constant" / "triSurface" / "aircraft.stl", scale=MM)
    ux, uz = V * math.cos(alpha), V * math.sin(alpha)
    k_in = 1.5 * (V * 0.005) ** 2
    om_in = math.sqrt(k_in) / (0.09 ** 0.25 * 0.03)
    hdr = lambda cls, obj: ("FoamFile\n{\n    version 2.0;\n    format ascii;\n    class " + cls +  # noqa: E731
                            ";\n    object " + obj + ";\n}\n")
    W = lambda p, t: (case / p).write_text(t)  # noqa: E731
    # domain: 6.7 chords upstream, 13 downstream, 3.5 semispans to the side, 6 chords above / below
    h = CFD_BASE_CELL_M
    x0, x1, y1, z1 = -2.0, 4.0, 2.0, 1.8
    nx, ny, nz = round((x1 - x0) / h), round(y1 / h), round(2 * z1 / h)
    W("system/blockMeshDict", hdr("dictionary", "blockMeshDict") + f"""
convertToMeters 1;
vertices (({x0} 0 {-z1}) ({x1} 0 {-z1}) ({x1} {y1} {-z1}) ({x0} {y1} {-z1}) ({x0} 0 {z1}) ({x1} 0 {z1}) ({x1} {y1} {z1}) ({x0} {y1} {z1}));
blocks (hex (0 1 2 3 4 5 6 7) ({nx} {ny} {nz}) simpleGrading (1 1 1));
boundary (
 farfield {{ type patch; faces ((0 4 7 3) (1 2 6 5) (3 7 6 2) (0 3 2 1) (4 5 6 7)); }}
 symmetry {{ type symmetryPlane; faces ((0 1 5 4)); }}
);
""")
    # feature edges of the CAD facets (trailing edges, elevon and fin edges): snapped explicitly
    W("system/surfaceFeatureExtractDict", hdr("dictionary", "surfaceFeatureExtractDict") + """
aircraft.stl { extractionMethod extractFromSurface; includedAngle 150;
  subsetFeatures { nonManifoldEdges no; openEdges yes; } trimFeatures { minElem 0; minLen 0; } writeObj no; }
""")
    lv0, lv1 = int(level[0]), int(level[1])
    lay = int(layers)
    W("system/snappyHexMeshDict", hdr("dictionary", "snappyHexMeshDict") + f"""
castellatedMesh true; snap true; addLayers {'true' if lay > 0 else 'false'};
geometry {{ aircraft.stl {{ type triSurfaceMesh; name aircraft; }}
           near {{ type searchableBox; min (-0.35 0 -0.35); max (0.85 0.75 0.35); }}
           close {{ type searchableBox; min (-0.13 0 -0.07); max (0.50 0.62 0.09); }} }}
castellatedMeshControls {{ maxLocalCells 3000000; maxGlobalCells 6000000; minRefinementCells 10;
  maxLoadUnbalance 0.10; nCellsBetweenLevels 3; features ( {{ file "aircraft.eMesh"; level {lv1}; }} );
  refinementSurfaces {{ aircraft {{ level ({lv0} {lv1}); patchInfo {{ type wall; }} }} }}
  resolveFeatureAngle 25;
  refinementRegions {{ near {{ mode inside; levels ((1E15 2)); }} close {{ mode inside; levels ((1E15 3)); }} }}
  locationInMesh (2.5 1.2 1.0); allowFreeStandingZoneFaces true; }}
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
    W("system/decomposeParDict", hdr("dictionary", "decomposeParDict") + f"numberOfSubdomains {procs};\nmethod simple;\n"
      f"simpleCoeffs {{ n ({procs} 1 1); delta 0.001; }}\n")  # geometric split: no scotch dependency
    lift = [-math.sin(alpha), 0, math.cos(alpha)]
    drag = [math.cos(alpha), 0, math.sin(alpha)]
    # no function objects: Ubuntu's OpenFOAM v1912 package aborts in every function object ("sha1"), so
    # forces and wall shear are evaluated from the written fields (_cfd_post), at every write
    wint = max(1, min(int(write_every), iterations))
    W("system/controlDict", hdr("dictionary", "controlDict") + f"""
application simpleFoam; startFrom latestTime; startTime 0; stopAt endTime; endTime {iterations}; deltaT 1;
writeControl timeStep; writeInterval {wint}; purgeWrite 0; writeFormat binary; writePrecision 8;
writeCompression off; timeFormat general; timePrecision 6; runTimeModifiable false;
""")
    W("system/fvSchemes", hdr("dictionary", "fvSchemes") + """
ddtSchemes { default steadyState; }
gradSchemes { default Gauss linear; grad(U) cellLimited Gauss linear 1; }
divSchemes { default none; div(phi,U) bounded Gauss linearUpwind grad(U); div(phi,k) bounded Gauss upwind;
 div(phi,omega) bounded Gauss upwind; div((nuEff*dev2(T(grad(U))))) Gauss linear; }
laplacianSchemes { default Gauss linear limited corrected 0.5; }
interpolationSchemes { default linear; }
snGradSchemes { default limited corrected 0.5; }
wallDist { method meshWave; }
""")
    W("system/fvSolution", hdr("dictionary", "fvSolution") + """
solvers { p { solver GAMG; smoother GaussSeidel; tolerance 1e-7; relTol 0.1; }
 "(U|k|omega)" { solver smoothSolver; smoother symGaussSeidel; tolerance 1e-8; relTol 0.1; } }
SIMPLE { nNonOrthogonalCorrectors 1; consistent true; residualControl { p 1e-4; U 1e-5; "(k|omega)" 1e-5; } }
relaxationFactors { fields { p 0.5; } equations { U 0.7; ".*" 0.7; } }
""")
    W("constant/transportProperties", hdr("dictionary", "transportProperties") + "transportModel Newtonian;\nnu 1.5e-05;\n")
    W("constant/turbulenceProperties", hdr("dictionary", "turbulenceProperties") +
      "simulationType RAS;\nRAS { RASModel kOmegaSST; turbulence on; printCoeffs off; }\n")
    bc = lambda far, wall: (f"boundaryField {{ farfield {{ {far} }} symmetry {{ type symmetryPlane; }} "  # noqa: E731
                            f"aircraft {{ {wall} }} }}\n")
    W("0/U", hdr("volVectorField", "U") + f"dimensions [0 1 -1 0 0 0 0];\ninternalField uniform ({ux:.5f} 0 {uz:.5f});\n"
      + bc(f"type freestreamVelocity; freestreamValue uniform ({ux:.5f} 0 {uz:.5f});", "type noSlip;"))
    W("0/p", hdr("volScalarField", "p") + "dimensions [0 2 -2 0 0 0 0];\ninternalField uniform 0;\n"
      + bc("type freestreamPressure; freestreamValue uniform 0;", "type zeroGradient;"))
    W("0/k", hdr("volScalarField", "k") + f"dimensions [0 2 -2 0 0 0 0];\ninternalField uniform {k_in:.6e};\n"
      + bc(f"type inletOutlet; inletValue uniform {k_in:.6e}; value uniform {k_in:.6e};",
           f"type kqRWallFunction; value uniform {k_in:.6e};"))
    W("0/omega", hdr("volScalarField", "omega") + f"dimensions [0 0 -1 0 0 0 0];\ninternalField uniform {om_in:.5f};\n"
      + bc(f"type inletOutlet; inletValue uniform {om_in:.5f}; value uniform {om_in:.5f};",
           f"type omegaWallFunction; value uniform {om_in:.5f};"))
    # Spalding's law covers the viscous sublayer, buffer and log layer: valid wherever the first layer lands
    W("0/nut", hdr("volScalarField", "nut") + "dimensions [0 2 -1 0 0 0 0];\ninternalField uniform 0;\n"
      + bc("type calculated; value uniform 0;", "type nutUSpaldingWallFunction; value uniform 0;"))
    t0 = time.time()
    steps = [("blockMesh", "log.blockMesh"), ("surfaceFeatureExtract", "log.surfaceFeatureExtract"),
             ("snappyHexMesh -overwrite", "log.snappy"),
             ("checkMesh", "log.checkMesh"), ("decomposePar -force", "log.decompose"),
             (f"mpirun --allow-run-as-root --oversubscribe -np {procs} simpleFoam -parallel", "log.simpleFoam"),
             ("reconstructPar", "log.reconstruct"),
             ]
    timing = {}
    if postprocess_only:
        shutil.rmtree(case)
        case, steps = Path(workdir), []
    for cmd, log in steps:
        if procs <= 1 and ("decompose" in cmd or "reconstruct" in cmd):
            continue
        if procs <= 1 and "simpleFoam" in cmd:
            cmd = "simpleFoam"
        ts = time.time()
        rc = _foam(cmd, case, log)
        timing[cmd.split()[0] if "mpirun" not in cmd else "simpleFoam"] = round(time.time() - ts, 1)
        if rc != 0:
            raise RuntimeError(f"OpenFOAM step '{cmd}' failed ({rc}); see {case / log}")
        if "surfaceFeatureExtract" in cmd and not _find_emesh(case):
            raise RuntimeError(f"surfaceFeatureExtract wrote no aircraft.eMesh; see {case / log}")
    secs = time.time() - t0
    mq = _foam_mesh_profile(case, level, lay)
    ncells = mq["cells"]
    mesh_ok = mq["checkMesh_ok"]
    times = sorted((d for d in case.iterdir() if d.is_dir() and _isnum(d.name) and float(d.name) > 0),
                   key=lambda d: float(d.name))
    if not times:
        raise RuntimeError(f"simpleFoam wrote no results; see {case / 'log.simpleFoam'}")
    tp = time.time()
    rc = _foam("foamToVTK -noFunctionObjects -noZero -fields '(p U nut)'", case, "log.foamToVTK")
    if rc != 0:
        raise RuntimeError(f"foamToVTK failed ({rc}); see {case / 'log.foamToVTK'}")
    lift, drag = np.array(lift), np.array(drag)
    hist, last = [], None
    for i, d in enumerate(times):
        final = i == len(times) - 1
        post = _cfd_post(case, d.name, rho, V, lift, drag, S / 2, alpha, convert=False, flow=final)
        hist.append([float(d.name), post["cl"], post["cd"]])
        last = post
    timing["post"] = round(time.time() - tp, 1)
    n_it = int(hist[-1][0])
    cl, cd = last["cl"], last["cd"]

    def osc(frac):  # peak-to-peak of C_L over the writes in the last ``frac`` of the run (incl. its start)
        win = [h[1] for h in hist if h[0] >= n_it * (1 - frac) - 1e-9]
        return float(np.ptp(win)) if len(win) >= 2 else None
    cl_osc10, cl_osc25 = osc(0.10), osc(0.25)
    pts, tri, cp, sep = last["points_mm"], last["tri"], last["cp"], last["separated"]
    sep_area = last["separated_area_pct"]
    hist = np.array(hist)
    lift_ratio = cl / cl_req
    crit = {"threshold": float(np.percentile(cp, 3)), "higher_is_worse": False,
            "node_mask": sep | (cp <= np.percentile(cp, 3)),
            "text": "reversed near-wall flow (separation) and the strongest suction (lowest 3 % of Cp)"}
    st = "fail" if lift_ratio < 0.9 else ("watch" if lift_ratio < 1.0 or sep_area > 10 else "ok")
    implication = ""
    if lift_ratio < 1.0 and cl > cl0:
        cla_cfd = (cl - cl0) / alpha  # the lift slope this run implies, same zero-lift offset as the flight model
        a_need = (cl_req - cl0) / cla_cfd
        implication = (f". At this lift slope ({cla_cfd:.2f}/rad vs {cla:.2f}/rad in the flight model) level cruise "
                       f"at {V:.1f} m/s needs about α {math.degrees(a_need):.0f}°, or about "
                       f"{V * math.sqrt(cl_req / cl):.1f} m/s at α {math.degrees(alpha):.0f}°: the cruise speed and "
                       f"endurance estimates are optimistic until a wind-tunnel or flight test settles the lift slope.")
    conv = (f"C_L varied by {cl_osc25:.3f} over the last 25 % of the iterations" if cl_osc25 is not None else
            "too few writes to judge C_L convergence")
    cov = mq.get("layer_coverage_pct")
    lay_txt = (f"{lay} prism layers ({cov:.0f} % of the aircraft surface extruded)" if lay and cov is not None
               else ("no prism layers" if not lay else f"{lay} prism layers requested (coverage not reported)"))
    rec = field_record(
        "cfd_cruise", "Aircraft - cruise aerodynamics", "fluid", "Wing core XPS — K09L-AF-WING-CORE",
        pts, tri, cp, "-", "pressure coefficient Cp", critical=crit,
        failure={"criterion": "lift at the trim angle vs the lift the flight model needs", "value": cl,
                 "allowable": cl_req, "unit": "C_L", "safety_factor": lift_ratio, "status": st, "line": 1.0,
                 "text": f"C_L {cl:.3f} at α {math.degrees(alpha):.1f}° vs {cl_req:.3f} needed for level cruise at "
                         f"{V:.1f} m/s ({lift_ratio * 100:.0f} %); C_D {cd:.4f}, L/D {cl / cd:.1f}; separated area "
                         f"{sep_area:.1f} % of the wetted surface; {conv}" + implication},
        solver={"name": "OpenFOAM v1912 simpleFoam (k-omega SST)", "seconds": round(time.time() - t0, 0),
                "seconds_meshing_and_solving": round(secs, 0), "step_seconds": timing,
                "iterations": n_it, "iterations_requested": int(iterations),
                "converged_by_residuals": n_it < int(iterations),
                "cl_oscillation_last_10pct": cl_osc10, "cl_oscillation_last_25pct": cl_osc25,
                "cl_oscillation_last_25pct_rel": (cl_osc25 / abs(cl)) if cl_osc25 is not None and cl else None,
                "write_interval": wint, "cells": ncells,
                "checkMesh": "Mesh OK" if mesh_ok else "checkMesh reported issues (see log)"},
        mesh_info={**mq, "type": "snappyHexMesh, half model", "surface_level": list(level),
                   "note": f"coarse mesh, {lay_txt}, Spalding wall function: indicative C_L / C_p, drag "
                           f"over-predicted; not a mesh-independence study"},
        load_case=f"cruise trim of the flight model: V {V:.2f} m/s, α {math.degrees(alpha):.2f}° (C_L {cl0:.3g} + "
                  f"{cla:.2f} α), ρ {rho:.3f} kg/m³, props stopped, elevons neutral",
        assumptions=["half model with a symmetry plane (no sideslip)", "props, motors, feet and hardware omitted",
                     f"steady RANS k-omega SST, {lay_txt}, Spalding wall function (no transition model: the "
                     f"laminar bubbles of a Re {V * c / 1.5e-5 / 1e3:.0f}k wing are not captured)",
                     "coarse mesh (surface cells "
                     f"{CFD_BASE_CELL_M * 1e3 / 2 ** lv1:.1f}-{CFD_BASE_CELL_M * 1e3 / 2 ** lv0:.1f} mm); "
                     "not a mesh-independence study",
                     "packing-tape skin as hydraulically smooth"],
        evidence="simulation (coarse RANS)",
        extra={"cl": cl, "cd": cd, "cl_required": cl_req, "alpha_deg": math.degrees(alpha), "V": V,
               "separated_pct": sep_area, "history": hist.tolist(),
               "history_columns": ["iteration", "C_L", "C_D"],
               "residuals_final": _foam_residuals(case / "log.simpleFoam")})
    rec["flow"] = last.get("flow")  # slice plane and streamlines: saved with the field arrays (save_field)
    return rec


def _find_emesh(case: Path) -> Optional[Path]:
    for p in (case / "constant" / "triSurface" / "aircraft.eMesh",
              case / "constant" / "extendedFeatureEdgeMesh" / "aircraft.eMesh"):
        if p.exists():
            return p
    return None


def _foam_mesh_profile(case: Path, level, layers: int) -> dict:
    """Mesh profile from log.checkMesh and log.snappy: cell counts and types, non-orthogonality, skewness,
    the checkMesh verdict and the prism-layer coverage."""
    import re
    F = r"[-+]?\d+(?:\.\d*)?(?:[eE][-+]?\d+)?"
    cm = (case / "log.checkMesh").read_text(errors="replace")
    out: dict = {"element_type": "polyhedral, hex-dominant (snappyHexMesh)"}

    def num(pat, txt=cm, cast=float):
        mm = re.search(pat, txt)
        return cast(mm.group(1).rstrip(".")) if mm else None
    for ln in cm.splitlines():
        s = ln.strip()
        if s.startswith("cells:"):
            out["cells"] = int(s.split()[-1])
        elif s.startswith("points:"):
            out["nodes"] = int(s.split()[-1])
        elif s.startswith("faces:"):
            out["faces"] = int(s.split()[-1])
    out["cells"] = out.get("cells") or 0
    out["elements"] = out["cells"]
    types = {}
    for k in ("hexahedra", "prisms", "wedges", "pyramids", "tet wedges", "tetrahedra", "polyhedra"):
        mm = re.search(rf"^\s*{k}:\s+(\d+)", cm, re.M)
        if mm:
            types[k] = int(mm.group(1))
    out["cell_types"] = types
    out["max_non_orthogonality"] = num(rf"non-orthogonality Max:\s*({F})")
    out["avg_non_orthogonality"] = num(rf"non-orthogonality Max:\s*{F}\s+average:\s*({F})")
    out["max_skewness"] = num(rf"Max skewness =\s*({F})")
    out["max_aspect_ratio"] = num(rf"Max aspect ratio =\s*({F})")
    vmin, vmax = num(rf"Min volume =\s*({F})"), num(rf"Max volume =\s*({F})")
    out["cell_size_mm"] = {"min": vmin ** (1 / 3) * 1e3 if vmin else None, "max": vmax ** (1 / 3) * 1e3 if vmax else None,
                           "note": "cube root of the smallest / largest cell volume"}
    nf = num(r"Failed (\d+) mesh checks", cm, int)
    ok = "Mesh OK" in cm
    out["checkMesh_ok"] = ok
    out["checkMesh_failed_checks"] = 0 if ok else (nf or None)
    failed = [ln.strip().lstrip("*") for ln in cm.splitlines() if ln.strip().startswith("***")]
    out["checkMesh"] = "Mesh OK" if ok else f"Failed {nf} mesh check(s): " + "; ".join(failed)[:400]
    lv0, lv1 = int(level[0]), int(level[1])
    out["target_size_mm"] = CFD_BASE_CELL_M * 1e3 / 2 ** lv1
    out["refinement_levels"] = {"base_cell_mm": CFD_BASE_CELL_M * 1e3, "surface": [lv0, lv1], "feature_edges": lv1,
                                "near_box": 2, "close_box": 3,
                                "surface_cell_mm": [CFD_BASE_CELL_M * 1e3 / 2 ** lv0, CFD_BASE_CELL_M * 1e3 / 2 ** lv1]}
    out.update(_snappy_layers(case / "log.snappy", layers))
    return out


def _snappy_layers(log: Path, layers: int) -> dict:
    """Prism-layer coverage of the aircraft patch from snappyHexMesh's log: the share of faces extruded
    (last 'Extruding n out of N faces' line) and the per-patch layer table (average layer count and the
    achieved share of the requested thickness)."""
    import re
    out = {"layers_requested": int(layers), "layer_coverage_pct": None, "layers_avg": None,
           "layer_thickness_pct": None}
    if not layers or not log.exists():
        if not layers:
            out["layer_coverage_pct"] = 0.0
        return out
    txt = log.read_text(errors="replace")
    ext = re.findall(r"Extruding\s+(\d+)\s+out of\s+(\d+)\s+faces", txt)
    if ext:
        a, n = map(int, ext[-1])
        out["layer_coverage_pct"] = 100.0 * a / max(n, 1)
    for ln in txt.splitlines():
        t = ln.split()
        if len(t) >= 4 and t[0] == "aircraft" and _isnum(t[1]) and _isnum(t[2]):
            out["layers_avg"] = float(t[2])
            if len(t) >= 5 and _isnum(t[-1]):
                out["layer_thickness_pct"] = float(t[-1])
    return out


def _isnum(s: str) -> bool:
    try:
        float(s)
        return True
    except ValueError:
        return False


def _foam_residuals(log: Path) -> dict:
    out = {}
    for ln in log.read_text(errors="replace").splitlines():
        if "Solving for" in ln and "Initial residual" in ln:
            f = ln.split("Solving for ")[1].split(",")[0]
            out[f] = float(ln.split("Initial residual = ")[1].split(",")[0])
    return out


def _cfd_post(case: Path, time_name: str, rho: float, V: float, lift_dir, drag_dir, aref: float, alpha: float,
              convert: bool = True, flow: bool = False) -> dict:
    """Forces, Cp and wall shear on the aircraft patch from the fields written at ``time_name``: pressure
    force sum(p Sf) plus the wall shear nu_eff,wall |U_t| / y of the wall-adjacent cell (OpenFOAM's own
    snGrad evaluation, with the wall-function nu_t on the patch). With ``flow``, also the slice-plane and
    streamline data for flow visualisation."""
    import pyvista as pv
    from scipy.spatial import cKDTree
    if convert:
        _foam(f"foamToVTK -time '{time_name}' -noFunctionObjects", case, f"log.foamToVTK_{time_name}")
    idx = _vtk_index(case, time_name)
    vdir = case / "VTK"
    tdir = next((d for d in vdir.iterdir() if d.is_dir() and d.name.endswith(f"_{idx}")), None)
    if tdir is None:
        raise RuntimeError(f"foamToVTK output for time {time_name} not found in {vdir}")
    vol = pv.read(str(tdir / "internal.vtu"))
    pat = tdir / "boundary" / "aircraft.vtp"
    sp = pv.read(str(pat))
    nu = 1.5e-5
    fc = sp.cell_centers().points
    nrm = sp.compute_normals(cell_normals=True, point_normals=False, auto_orient_normals=False,
                             consistent_normals=False, split_vertices=False).cell_data["Normals"]
    area = sp.compute_cell_sizes(length=False, volume=False).cell_data["Area"]
    Sf = nrm * area[:, None]
    p_f = np.asarray(sp.cell_data["p"]) if "p" in sp.cell_data else np.asarray(sp.point_data_to_cell_data().cell_data["p"])
    nut_w = np.asarray(sp.cell_data["nut"]) if "nut" in sp.cell_data else np.zeros(len(fc))
    cc = vol.cell_centers().points
    Uc = np.asarray(vol.cell_data["U"])
    d, idx = cKDTree(cc).query(fc)
    dn = np.abs(np.einsum("ij,ij->i", cc[idx] - fc, nrm)) + 1e-9
    Ut = Uc[idx] - np.einsum("ij,ij->i", Uc[idx], nrm)[:, None] * nrm
    tau = (nu + nut_w)[:, None] * Ut / dn[:, None]  # kinematic wall shear, along the near-wall flow
    # boundary normals point out of the fluid (into the body): pressure force on the body = sum p Sf
    F = rho * ((p_f[:, None] * Sf).sum(0) + (tau * area[:, None]).sum(0))
    q = 0.5 * rho * V * V
    cl, cd = float(F @ np.asarray(lift_dir)) / (q * aref), float(F @ np.asarray(drag_dir)) / (q * aref)
    if cd < 0:  # normals came out reversed: flip
        F = rho * ((-p_f[:, None] * Sf).sum(0) + (tau * area[:, None]).sum(0))
        cl, cd = float(F @ np.asarray(lift_dir)) / (q * aref), float(F @ np.asarray(drag_dir)) / (q * aref)
    fdir = np.array([math.cos(alpha), 0, math.sin(alpha)])
    sep_c = (tau @ fdir) < 0  # near-wall flow running against the freestream: reversed (separated) flow
    sp.cell_data["cp"] = p_f / (0.5 * V * V)
    sp.cell_data["sep"] = sep_c.astype(float)
    pt = sp.cell_data_to_point_data().triangulate()
    tri = pt.faces.reshape(-1, 4)[:, 1:]
    sep_area = float((area * sep_c).sum() / area.sum() * 100)
    out = {"cl": cl, "cd": cd, "points_mm": np.asarray(pt.points) / MM, "tri": tri,
           "cp": np.asarray(pt.point_data["cp"]), "separated": np.asarray(pt.point_data["sep"]) > 0.5,
           "separated_area_pct": sep_area, "force_N_half": F.tolist()}
    if flow:
        out["flow"] = _cfd_flow(vol, V, alpha)
    return out


FLOW_SLICE_Y_M = 0.25  # spanwise station of the flow slice (m): between the pod and the fin, through the elevon
FLOW_STREAM_MAX_PTS = 150


def _cfd_flow(vol, V: float, alpha: float, y_slice: float = FLOW_SLICE_Y_M) -> dict:
    """Flow-visualisation data from the volume field (aircraft frame, mm): a chordwise slice plane at
    y = y_slice with speed and Cp, and streamlines seeded upstream across the span (forward-integrated,
    decimated to <= FLOW_STREAM_MAX_PTS points each)."""
    import pyvista as pv
    g = vol.cell_data_to_point_data(pass_cell_data=False)
    q = 0.5 * V * V
    # slice: the region around the wing section (x -0.25..0.75 m, z -0.25..0.30 m)
    sl = g.slice(normal="y", origin=(0.0, y_slice, 0.0)).triangulate()
    ctr = sl.cell_centers().points
    keep = np.where((ctr[:, 0] > -0.25) & (ctr[:, 0] < 0.75) & (ctr[:, 2] > -0.25) & (ctr[:, 2] < 0.30))[0]
    sl = sl.extract_cells(keep).extract_surface(algorithm="dataset_surface").triangulate()
    s_tri = sl.faces.reshape(-1, 4)[:, 1:]
    s_U = np.asarray(sl.point_data["U"])
    slice_d = {"points_mm": np.asarray(sl.points) / MM, "tri": s_tri,
               "speed": np.linalg.norm(s_U, axis=1), "cp": np.asarray(sl.point_data["p"]) / q,
               "y_mm": y_slice / MM}
    # streamlines: 15 span stations x 4 heights, 0.15 m upstream, placed along the freestream direction
    x0 = -0.15
    ys = np.linspace(0.03, 0.70, 15)
    zs = np.array([-0.03, -0.005, 0.015, 0.04]) + x0 * math.tan(alpha)
    seeds = np.array([[x0, y, z] for y in ys for z in zs])
    lines = g.streamlines_from_source(pv.PolyData(seeds), vectors="U", integration_direction="forward",
                                      max_length=1.6, initial_step_length=0.2, step_unit="cl", max_steps=6000,
                                      terminal_speed=1e-4)
    streams = []
    if lines.n_points:
        L = np.asarray(lines.lines)
        P = np.asarray(lines.points)
        sp = np.linalg.norm(np.asarray(lines.point_data["U"]), axis=1)
        i = 0
        while i < len(L):
            n = int(L[i])
            ids = L[i + 1:i + 1 + n]
            i += n + 1
            if n < 3:
                continue
            sel = np.unique(np.linspace(0, n - 1, min(n, FLOW_STREAM_MAX_PTS)).round().astype(int))
            streams.append({"points_mm": P[ids[sel]] / MM, "speed": sp[ids[sel]]})
    return {"slice": slice_d, "streamlines": streams, "freestream": float(V), "n_seeds": int(len(seeds))}


def _vtk_index(case: Path, time_name: str) -> str:
    # foamToVTK names files by time index: <case>_<index>; with writeInterval steps, index == iteration
    return str(int(float(time_name)))


# =============================================================================================== storage
def save_field(rec: dict, fields_dir: Path) -> dict:
    """Write the arrays of a field record to <fields_dir>/<id>_<hash>.npz and return the record without them
    (JSON-safe, with 'field_file'). A CFD record's flow-visualisation data (rec['flow']) goes into the same
    npz: slice_points (mm), slice_tri, slice_speed (m/s), slice_cp, stream_points (mm, all lines
    concatenated), stream_offsets (line i = stream_points[off[i]:off[i+1]]), stream_speed (m/s),
    freestream (m/s), slice_y_mm; read it back with load_flow."""
    import hashlib
    import json
    fields_dir = Path(fields_dir)
    fields_dir.mkdir(parents=True, exist_ok=True)
    s = rec["surface"]
    h = hashlib.sha1(np.asarray(s["values"], np.float32).tobytes() + np.asarray(s["tri"], np.int32).tobytes()).hexdigest()[:10]
    f = fields_dir / f"{rec['id']}_{h}.npz"
    arrs = dict(points=np.asarray(s["points_mm"], np.float32), tri=np.asarray(s["tri"], np.int32),
                values=np.asarray(s["values"], np.float32),
                critical=np.asarray(rec["critical"]["node_mask"], bool))
    flow = rec.get("flow")
    flow_meta = None
    if flow:
        sl, streams = flow["slice"], flow["streamlines"]
        n = [len(x["speed"]) for x in streams]
        arrs.update(slice_points=np.asarray(sl["points_mm"], np.float32), slice_tri=np.asarray(sl["tri"], np.int32),
                    slice_speed=np.asarray(sl["speed"], np.float32), slice_cp=np.asarray(sl["cp"], np.float32),
                    slice_y_mm=np.float32(sl.get("y_mm", np.nan)),
                    stream_points=(np.concatenate([x["points_mm"] for x in streams]).astype(np.float32)
                                   if streams else np.zeros((0, 3), np.float32)),
                    stream_offsets=np.r_[0, np.cumsum(n)].astype(np.int32),
                    stream_speed=(np.concatenate([x["speed"] for x in streams]).astype(np.float32)
                                  if streams else np.zeros(0, np.float32)),
                    freestream=np.float32(flow["freestream"]))
        flow_meta = {"slice_y_mm": float(sl.get("y_mm", float("nan"))), "slice_points": int(len(sl["points_mm"])),
                     "slice_triangles": int(len(sl["tri"])), "streamlines": len(streams),
                     "streamline_seeds": flow.get("n_seeds"), "freestream": float(flow["freestream"])}
    sec_meta, disp_meta = [], None
    for fd in s.get("fields") or []:
        arrs["fv_" + fd["key"]] = np.asarray(fd["values"], np.float32)
        sec_meta.append({k: v for k, v in fd.items() if k != "values"})
    if s.get("displacement") is not None:
        d = np.asarray(s["displacement"], np.float32)
        arrs["disp"] = d
        mag = np.linalg.norm(d, axis=1)
        disp_meta = {"max_mm": float(mag.max()) if len(mag) else 0.0,
                     "at_mm": np.asarray(s["points_mm"])[int(np.argmax(mag))].round(2).tolist() if len(mag) else None}
    np.savez_compressed(f, **arrs)
    out = {k: v for k, v in rec.items() if k not in ("surface", "flow")}
    if sec_meta:
        out["fields"] = sec_meta
    if disp_meta:
        out["displacement"] = disp_meta
    out["critical"] = {k: v for k, v in rec["critical"].items() if k != "node_mask"}
    if flow_meta:
        out["flow"] = flow_meta
    out["field_file"] = str(f)
    out["field_name"] = f.name
    return json.loads(json.dumps(out, default=lambda o: o.tolist() if hasattr(o, "tolist") else str(o)))


FIELD_DIRS = [Path(__file__).resolve().parent.parent / "examples" / "k09l" / "fields",
              Path.home() / ".opencae" / "k09l_fields"]


def _field_path(summary: Optional[dict]) -> Optional[Path]:
    if not summary:
        return None
    cands = [Path(summary.get("field_file") or "")] + [d / (summary.get("field_name") or "") for d in FIELD_DIRS]
    return next((c for c in cands if c.name and c.is_file()), None)


def load_field(summary: dict) -> Optional[dict]:
    """Arrays of a saved field record (searched by its path, then by name in the known field folders)."""
    f = _field_path(summary)
    if f is None:
        return None
    d = np.load(f)
    return {"points_mm": d["points"], "tri": d["tri"], "values": d["values"], "critical": d["critical"],
            "fields": {k[3:]: d[k] for k in d.files if k.startswith("fv_")},
            "disp": d["disp"] if "disp" in d.files else None}


def load_flow(summary: dict) -> Optional[dict]:
    """Flow-visualisation data saved with a CFD field record, in the aircraft frame (mm) like the surface:
    {"slice": {"points_mm" (n,3), "tri" (m,3), "speed" (n,) m/s, "cp" (n,), "y_mm"},
     "streamlines": [{"points_mm" (k,3), "speed" (k,) m/s}, ...], "freestream": V m/s}.
    None when the record has no flow data (older runs, FEA / thermal records) or its file is missing."""
    f = _field_path(summary)
    if f is None:
        return None
    d = np.load(f)
    if "slice_points" not in d.files or "stream_offsets" not in d.files:
        return None
    off = np.asarray(d["stream_offsets"], int)
    P, Sp = d["stream_points"], d["stream_speed"]
    streams = [{"points_mm": P[a:b], "speed": Sp[a:b]} for a, b in zip(off[:-1], off[1:]) if b > a]
    y = float(d["slice_y_mm"]) if "slice_y_mm" in d.files else None
    return {"slice": {"points_mm": d["slice_points"], "tri": d["slice_tri"], "speed": d["slice_speed"],
                      "cp": d["slice_cp"], "y_mm": y},
            "streamlines": streams, "freestream": float(d["freestream"])}
