"""Structure: linear static CalculiX analysis of the load-carrying parts (arms; wings for a fixed wing).

Each part is volume-meshed with gmsh (C3D10, at most 40 000 elements) from its closed tessellated surface.
Multirotor / tailsitter / vtol: the arm end nearest the aircraft CG is fixed; ``load_factor`` x the maximum
thrust of the rotor(s) on that arm acts along the rotor axis on the surface nodes nearest the motor position.
Fixed wing: root fixed, ``load_factor`` x weight / 2 distributed over each wing half.
Safety factor = yield strength / 99th-percentile von Mises stress on the surface (the top 1 % is dropped as
singular: support edges and load points). Material data are never assumed: a missing value is reported.
"""
from __future__ import annotations

import math
import os
import shutil
import time
from pathlib import Path

import numpy as np

from opencae.drone import types as T
from opencae.drone.runlock import solver_lock

ID = "structure"
TITLE = "Structure"
NEEDS = ["type", "role_materials", "materials", "analyses.structure.load_factor", "environment.g"]

MAX_ELEMENTS = 40_000
SF_OK, SF_WATCH = 1.5, 1.0
FIXED_FRAC = 0.06       # fixed region: this share of the part length at the root end
LOAD_NODES = 40         # surface nodes nearest the motor position that carry the thrust
G_DEFAULT = 9.80665
RANK = {T.ESTIMATE: 0, T.DATASHEET: 1, T.MEASURED: 2, T.USER: 2}


def _leaf(node):
    if isinstance(node, dict):
        return node.get("value"), node.get("status", T.ESTIMATE)
    if node is None:
        return None, ""
    return node, T.ESTIMATE


def _num(d, *path):
    cur = d
    for k in path:
        if not isinstance(cur, dict) or k not in cur:
            return None, ""
        cur = cur[k]
    v, s = _leaf(cur)
    try:
        return (float(v), s) if v is not None else (None, s)
    except (TypeError, ValueError):
        return None, s


def _weakest(statuses) -> str:
    st = [s for s in statuses if s]
    return min(st, key=lambda s: RANK.get(s, 0)) if st else T.ESTIMATE


def _mv(res, name):
    m = ((res or {}).get("metrics") or {}).get(name)
    return m.get("value") if isinstance(m, dict) else m


def _mbasis(res, name) -> str:
    m = ((res or {}).get("metrics") or {}).get(name)
    return (m.get("basis") or T.ESTIMATE) if isinstance(m, dict) else T.ESTIMATE


def _status(sf: float) -> str:
    return T.OK if sf >= SF_OK else (T.WATCH if sf >= SF_WATCH else T.FAIL)


def _find_ccx():
    return os.environ.get("OPENCAE_CCX") or shutil.which("ccx")


def material_for(spec: dict, role: str) -> tuple:
    """(material dict with E MPa, nu, yield MPa, name, status) or (None, missing keys)."""
    name = (spec.get("role_materials") or {}).get(role)
    if not name:
        return None, [f"role_materials.{role}"]
    mat = (spec.get("materials") or {}).get(name)
    if not isinstance(mat, dict):
        return None, [f"materials.{name}"]
    missing, out, sts = [], {}, [mat.get("status", T.ESTIMATE)]
    for key, k2, scale in (("youngs_modulus_gpa", "E", 1000.0), ("poisson", "nu", 1.0),
                           ("yield_strength_mpa", "yield", 1.0)):
        v, s = _num(mat, key)
        if v is None or v <= 0:
            missing.append(f"materials.{name}.{key}")
        else:
            out[k2] = v * scale
            sts.append(s)
    if missing:
        return None, missing
    out.update(name=name, status=_weakest(sts))
    return out, []


class _Shim:  # lets field_analyses._tet_mesh read a Part
    def body_mesh(self, b):
        return b.points, b.triangles


def _closed_volume(points, tris):
    """(volume mm3, closed?) of a tessellated surface."""
    p = np.asarray(points, float)
    t = np.asarray(tris)
    v = float(np.einsum("ij,ij->i", p[t[:, 0]], np.cross(p[t[:, 1]], p[t[:, 2]])).sum() / 6.0)
    e = np.sort(np.concatenate([t[:, [0, 1]], t[:, [1, 2]], t[:, [2, 0]]]), axis=1)
    _, cnt = np.unique(e, axis=0, return_counts=True)
    return abs(v), bool(np.all(cnt == 2))


def _mesh(part: T.Part, size: float, wd: Path):
    from opencae.engineering import field_analyses as FA
    return FA._tet_mesh(_Shim(), [part], size, wd)


def _mesh_limited(part: T.Part, vol: float, wd: Path):
    """Volume mesh with at most MAX_ELEMENTS elements: coarsen until it fits."""
    lo, hi = part.points.min(0), part.points.max(0)
    thin = float(np.sort(hi - lo)[0])
    size = max((vol / (0.12 * 15000)) ** (1 / 3), 1e-3)
    size = min(size, max(thin / 2.5, size * 0.3))
    for _ in range(8):
        nodes, el = _mesh(part, size, wd)
        if len(el) <= MAX_ELEMENTS:
            return nodes, el, size
        size *= max(1.15, (len(el) / MAX_ELEMENTS) ** (1 / 3) * 1.05)
    raise RuntimeError(f"cannot mesh {part.name} with at most {MAX_ELEMENTS} elements")


def _nodal_area(nodes, tri):
    a = np.zeros(len(nodes))
    ar = 0.5 * np.linalg.norm(np.cross(nodes[tri[:, 1]] - nodes[tri[:, 0]], nodes[tri[:, 2]] - nodes[tri[:, 0]]),
                              axis=1)
    for k in range(3):
        np.add.at(a, tri[:, k], ar / 3)
    return a


def _solve(nodes, el, fixed, loads: dict, mat: dict, wd: Path, title: str):
    """Write the deck, run ccx, return (S (n,6), U (n,3), seconds, log tail)."""
    from opencae.engineering import field_analyses as FA
    ccx = _find_ccx()
    L = FA._ccx_deck_header(nodes, el, title)
    L += FA._nset("FIX", fixed)
    L += ["*MATERIAL, NAME=MAT", "*ELASTIC", f"{mat['E']}, {mat['nu']}", "*SOLID SECTION, ELSET=EALL, MATERIAL=MAT",
          "*STEP", "*STATIC", "*BOUNDARY", "FIX, 1, 3, 0.0", "*CLOAD"]
    for nid, f in loads.items():
        for d in range(3):
            if abs(f[d]) > 0:
                L.append(f"{int(nid) + 1}, {d + 1}, {f[d]:.6e}")
    L += ["*NODE FILE", "U", "*EL FILE", "S", "*END STEP"]
    (wd / "job.inp").write_text("\n".join(L) + "\n")
    t0 = time.time()
    rc = FA._run([ccx, "-i", "job"], wd, wd / "ccx.log", env=FA._ccx_env(wd))
    log = (wd / "ccx.log").read_text(errors="replace")[-1500:] if (wd / "ccx.log").exists() else ""
    if rc != 0 or not (wd / "job.frd").exists():
        raise RuntimeError(f"CalculiX failed ({rc}): {log[-600:]}")
    S = FA._read_frd_nodal(wd / "job.frd", "STRESS", len(nodes))
    U = FA._read_frd_nodal(wd / "job.frd", "DISP", len(nodes))
    return S, U, time.time() - t0, log


def build_case(part: T.Part, role: str, spec: dict, ctx_info: dict, wd: Path):
    """Mesh the part and define supports and loads. Returns dict(nodes, el, tri, fixed, loads, text, size)."""
    from opencae.engineering import field_analyses as FA
    vol, closed = _closed_volume(part.points, part.triangles)
    if not closed:
        raise ValueError(f"part {part.name} is not a closed surface (cannot be volume-meshed)")
    nodes, el, size = _mesh_limited(part, vol, wd)
    tri = FA._surface(nodes, el)
    surf = np.unique(tri)
    cg = np.asarray(ctx_info["cg_mm"], float)
    # principal axis of the part
    c = nodes[surf].mean(0)
    w, V = np.linalg.eigh(np.cov((nodes[surf] - c).T))
    ax = V[:, -1]
    s = (nodes[surf] - c) @ ax
    length = float(s.max() - s.min())
    end_a, end_b = c + ax * s.min(), c + ax * s.max()
    if role == "wing":
        return _wing_case(nodes, el, tri, surf, cg, ctx_info, size)
    root_is_min = np.linalg.norm(end_a - cg) <= np.linalg.norm(end_b - cg)
    tip = end_b if root_is_min else end_a
    fixed = surf[(s <= s.min() + FIXED_FRAC * length) if root_is_min else (s >= s.max() - FIXED_FRAC * length)]
    # rotors on this arm: nearest-arm assignment is made by the caller (ctx_info["rotors"] already filtered)
    rot = ctx_info["rotors"]
    if not rot:
        return {"nodes": nodes, "el": el, "tri": tri, "fixed": fixed, "loads": {}, "size": size,
                "text": "no rotor on this arm: not loaded", "unloaded": True}
    pos = np.mean([np.asarray(r["position_mm"], float) for r in rot], axis=0)
    axis = np.mean([np.asarray(r["axis"], float) / np.linalg.norm(r["axis"]) for r in rot], axis=0)
    F = ctx_info["load_factor"] * ctx_info["thrust_n"] * len(rot)
    d = np.linalg.norm(nodes[surf] - pos, axis=1)
    near = surf[np.argsort(d)[:LOAD_NODES]]
    near = np.setdiff1d(near, fixed)
    loads = {int(i): axis * F / len(near) for i in near}
    return {"nodes": nodes, "el": el, "tri": tri, "fixed": fixed, "loads": loads, "size": size,
            "text": (f"root end fixed; {F:.1f} N ({ctx_info['load_factor']:g} x {ctx_info['thrust_n']:.2f} N x "
                     f"{len(rot)} rotor) along the rotor axis on the {len(near)} surface nodes nearest the motor "
                     f"position; arm self-weight ignored")}


def _wing_case(nodes, el, tri, surf, cg, info, size):
    W = info["weight_n"] * info["load_factor"]
    P = nodes[surf]
    yc = cg[1]
    ymin, ymax = P[:, 1].min(), P[:, 1].max()
    area = _nodal_area(nodes, tri)
    if ymin < yc - 0.2 * (ymax - ymin) and ymax > yc + 0.2 * (ymax - ymin):  # full span: both halves
        fixed = surf[np.abs(P[:, 1] - yc) <= FIXED_FRAC * (ymax - ymin)]
        mask = ~np.isin(surf, fixed)
        txt = (f"centre band fixed; {W:.1f} N ({info['load_factor']:g} x weight) lifting the two halves "
               f"({W / 2:.1f} N each), spread over the surface nodes by area")
        tot = W
    else:
        root_is_min = abs(ymin - yc) <= abs(ymax - yc)
        span = ymax - ymin
        fixed = surf[(P[:, 1] <= ymin + FIXED_FRAC * span) if root_is_min else (P[:, 1] >= ymax - FIXED_FRAC * span)]
        mask = ~np.isin(surf, fixed)
        tot = W / 2
        txt = (f"root fixed; {tot:.1f} N ({info['load_factor']:g} x weight / 2) spread over the surface nodes "
               f"by area")
    ids = surf[mask]
    a = area[ids]
    loads = {int(i): np.array([0.0, 0.0, tot * ai / a.sum()]) for i, ai in zip(ids, a)}
    return {"nodes": nodes, "el": el, "tri": tri, "fixed": fixed, "loads": loads, "size": size, "text": txt}


def run(ctx: T.RunContext) -> dict:
    spec = ctx.spec or {}
    typ = spec.get("type", "multirotor")
    role = "wing" if typ == "fixed_wing" else "arm"
    parts = ctx.aircraft.role_parts(role)
    if not parts:
        return T.not_evaluated(ID, TITLE, [f"role:{role}"], f"Not evaluated: no part has the role \"{role}\"; "
                               f"name the {role} parts under roles in the drone file.")
    mat, missing = material_for(spec, role)
    if mat is None:
        return T.not_evaluated(ID, TITLE, missing)
    lf, lf_st = _num(spec, "analyses", "structure", "load_factor")
    if lf is None or lf <= 0:
        return T.not_evaluated(ID, TITLE, ["analyses.structure.load_factor"])
    prev = ctx.previous or {}
    mass_res, prop_res = prev.get("mass"), prev.get("propulsion")
    cg = _mv(mass_res, "cg_mm")
    cg_note = ""
    if cg is None:
        allp = np.vstack([p.points for p in ctx.aircraft.parts])
        cg = ((allp.min(0) + allp.max(0)) / 2).tolist()
        cg_note = " The CG came from the geometry bounding box (no mass analysis)."
    info = {"cg_mm": cg, "load_factor": lf}
    missing = []
    basis_parts = [mat["status"]]
    if role == "arm":
        thrust = _mv(prop_res, "thrust_max_n")
        rotors = ((prop_res or {}).get("extra") or {}).get("rotors")
        if thrust is None:
            missing.append("propulsion.thrust_max_n")
        if not rotors:
            missing.append("propulsion.rotors")
        basis_parts.append(_mbasis(prop_res, "thrust_max_n"))
        info["thrust_n"] = thrust
    else:
        mass = _mv(mass_res, "mass_kg")
        if mass is None:
            missing.append("mass.mass_kg")
        g, _ = _num(spec, "environment", "g")
        info["weight_n"] = (mass or 0) * (g or G_DEFAULT)
        basis_parts.append(_mbasis(mass_res, "mass_kg"))
    if missing:
        return T.not_evaluated(ID, TITLE, missing)
    ccx = _find_ccx()
    if not ccx:
        return T.result(ID, TITLE, T.ERROR, "CalculiX (ccx) is not installed or not on the PATH; the structure "
                        "analysis was not run. Install CalculiX or set OPENCAE_CCX.", missing=[])
    basis = _weakest(basis_parts)
    # rotor -> nearest arm
    assign = {p.name: [] for p in parts}
    if role == "arm":
        for r in rotors:
            q = np.asarray(r["position_mm"], float)
            best = min(parts, key=lambda p: np.min(np.linalg.norm(p.points - q, axis=1)))
            assign[best.name].append(r)
    rows, fields, texts, errors = [], [], [], []
    wd0 = Path(ctx.workdir)
    ctx.out_dir = Path(ctx.out_dir)
    with solver_lock("structure analysis", on_wait=lambda m: ctx.progress(m, 0.0)):
        for i, part in enumerate(parts):
            if ctx.cancelled():
                break
            ctx.progress(f"structure: {part.name}", i / len(parts))
            wd = wd0 / f"structure_{i}"
            wd.mkdir(parents=True, exist_ok=True)
            info["rotors"] = assign[part.name]
            try:
                case = build_case(part, role, spec, info, wd)
                if case.get("unloaded"):
                    texts.append(f"{part.name}: {case['text']}")
                    continue
                S, U, secs, log = _solve(case["nodes"], case["el"], case["fixed"], case["loads"], mat, wd, part.name)
            except ValueError as exc:
                return T.not_evaluated(ID, TITLE, [f"closed geometry of {part.name}"], f"Not evaluated: {exc}.")
            except Exception as exc:
                errors.append(f"{part.name}: {exc}")
                continue
            from opencae.engineering import field_analyses as FA
            vm = FA._von_mises(S[:, :6])
            tri = case["tri"]
            surf = np.unique(tri)
            vs = vm[surf]
            p99 = float(np.percentile(vs, 99))
            if p99 <= 0:
                errors.append(f"{part.name}: zero stress (no load reached the part)")
                continue
            sf = mat["yield"] / p99
            umax = float(np.linalg.norm(U[surf], axis=1).max())
            remap = -np.ones(len(case["nodes"]), int)
            remap[surf] = np.arange(len(surf))
            fname = f"structure_{_slug(part.name)}.npz"
            T.save_field(ctx.out_dir / fname, vs, case["nodes"][surf], remap[tri], U[surf])
            fields.append({"file": fname, "key": "von_mises", "quantity": "von Mises stress", "unit": "MPa",
                           "part": part.name})
            rows.append({"part": part.name, "safety_factor": sf, "p99_mpa": p99, "peak_mpa": float(vs.max()),
                         "max_disp_mm": umax, "elements": int(len(case["el"])), "size_mm": case["size"],
                         "seconds": round(secs, 1), "load": case["text"]})
    if not ctx.keep_cases and not errors:
        for d in wd0.glob("structure_*"):
            shutil.rmtree(d, ignore_errors=True)
    if not rows:
        msg = "; ".join(errors) if errors else "; ".join(texts) or "nothing was solved"
        return T.result(ID, TITLE, T.ERROR if errors else T.NOT_EVALUATED, f"Structure analysis produced no result: "
                        f"{msg}", log=msg, missing=[])
    worst = min(rows, key=lambda r: r["safety_factor"])
    sf = worst["safety_factor"]
    st = _status(sf)
    what = "arm" if role == "arm" else "wing"
    summ = (f"The weakest {what} ({worst['part']}) has a safety factor of {sf:.1f} at {lf:g} g "
            f"(peak stress {worst['p99_mpa']:.0f} MPa against {mat['yield']:.0f} MPa yield, bends "
            f"{worst['max_disp_mm']:.1f} mm).")
    if basis == T.ESTIMATE:
        summ += " Material or thrust data are estimates."
    summ += cg_note
    if errors:
        summ += " Some parts failed: " + "; ".join(errors)
    metrics = {"safety_factor": T.metric(sf, "", st, basis, "Safety factor"),
               "max_stress_mpa": T.metric(max(r["p99_mpa"] for r in rows), "MPa", st, basis,
                                          "Max stress (99th percentile)"),
               "max_disp_mm": T.metric(max(r["max_disp_mm"] for r in rows), "mm", st, basis, "Max deflection")}
    return T.result(ID, TITLE, st, summ, metrics, fields=fields,
                    solver={"name": "CalculiX ccx (C3D10)", "converged": True, "iterations": 1,
                            "cells": int(sum(r["elements"] for r in rows)),
                            "runtime_s": round(sum(r["seconds"] for r in rows), 1)},
                    log="; ".join(errors),
                    extra={"parts": rows, "material": mat["name"], "load_factor": lf, "role": role,
                           "assumptions": ["linear elastic, small displacement", "stress = 99th percentile of the "
                                           "surface von Mises (top 1 % dropped as singular)",
                                           "load: " + (rows[0]["load"] if rows else "")]})


def _slug(s: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in s)
