"""Intelligent Mesh Manager: geometry- and physics-aware mesh plans.

Presets fix *resolution targets*, never absolute sizes. Every size is derived from the measured
geometry (``geometry.analysis``) and, for flow, from the flow conditions:

=========================== ========= ========== ===========
target                      Quick     Balanced   Validation
=========================== ========= ========== ===========
elements along body size    12        25         40
elements through thickness  1         2          3          (quadratic elements; x2 for linear)
elements around a hole      12        24         36
elements per 90° of fillet  2         4          6
loaded / support faces      global    global/2   global/3
convergence levels          1         1          >= 3 (automatic loop)
=========================== ========= ========== ===========

Sharp re-entrant edges are singular in linear elasticity: they are refined moderately and the
plan says that stresses there do not converge (judge stresses away from them).

Flow (wall faces of the fluid body): wall size from the immersed body's size; first-layer height
for a target y+ from the flat-plate skin-friction correlation (turbulent: Cf = 0.026 Re_x^(-1/7),
White, Fluid Mechanics; laminar: delta = 5 x / sqrt(Re_x), Blasius).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from opencae.geometry.analysis import GeometryReport

PRESETS = {
    "quick": {"along": 12, "thick": 1, "hole": 12, "fillet": 2, "bc_div": 1, "levels": 1, "curv": 8, "growth": 1.5},
    "balanced": {"along": 25, "thick": 2, "hole": 24, "fillet": 4, "bc_div": 2, "levels": 1, "curv": 12,
                 "growth": 1.4},
    "validation": {"along": 40, "thick": 3, "hole": 36, "fillet": 6, "bc_div": 3, "levels": 3, "curv": 18,
                   "growth": 1.3},
}
PRESET_TEXT = {
    "quick": "Quick Analysis: early design screening; coarse but geometry-aware mesh.",
    "balanced": "Balanced Analysis: routine calculations; holes, fillets, thin walls and loaded faces refined.",
    "validation": "Validation Analysis: systematic refinement (>= 3 meshes) with convergence assessment.",
}
TET_VOLUME_FACTOR = 0.118  # volume of a regular tetrahedron with edge h: h^3 / (6 sqrt 2)


@dataclass
class LocalSize:
    kind: str  # face | edge
    indices: list[int]
    size: float
    reason: str


@dataclass
class MeshPlan:
    preset: str
    physics: str
    global_size: float
    min_size: float
    curvature_points: int
    element_order: int
    growth: float = 1.4
    local: list[LocalSize] = field(default_factory=list)
    bl_first: Optional[float] = None  # flow: first-layer height (project length)
    bl_layers: int = 0
    estimated_elements: int = 0
    estimated_nodes: int = 0
    estimated_memory_mb: float = 0.0
    estimated_seconds: float = 0.0
    notes: list[str] = field(default_factory=list)

    def describe(self) -> list[str]:
        out = [PRESET_TEXT[self.preset],
               f"global size {self.global_size:.4g}, minimum size {self.min_size:.4g}, "
               f"{'quadratic' if self.element_order == 2 else 'linear'} elements, "
               f"{self.curvature_points} elements per circle on curved faces"]
        out += [f"{ls.kind}s {', '.join(str(i + 1) for i in ls.indices[:8])}{'…' if len(ls.indices) > 8 else ''}: "
                f"size {ls.size:.4g} ({ls.reason})" for ls in self.local]
        if self.bl_first:
            out.append(f"wall layers: first height {self.bl_first:.3g}, {self.bl_layers} layers")
        out.append(f"estimate (about ±factor 2): ~{self.estimated_elements:,} elements, ~{self.estimated_nodes:,} "
                   f"nodes, ~{self.estimated_memory_mb:,.0f} MB solver memory, ~{self.estimated_seconds:,.0f} s "
                   "static solve")
        return out + self.notes


def _first_layer(speed: float, nu: float, length: float, y_plus: float, turbulent: bool) -> float:
    """First-cell height [m] for a target y+ at the end of a flat plate of length ``length`` [m]."""
    re = max(speed * length / nu, 1.0)
    if turbulent:
        cf = 0.026 * re ** (-1.0 / 7.0)
        u_tau = speed * math.sqrt(cf / 2.0)
        return y_plus * nu / u_tau
    delta = 5.0 * length / math.sqrt(re)
    return delta / 15.0  # about 15 cells across the laminar boundary layer


def plan_mesh(rep: GeometryReport, preset: str = "balanced", physics: str = "structural",
              order: int = 2, bc_faces: Optional[list[int]] = None, wall_faces: Optional[list[int]] = None,
              flow: Optional[dict] = None, length_to_m: float = 1e-3,
              max_elements: Optional[int] = None, body=None) -> MeshPlan:
    """Mesh plan for one body. ``bc_faces``: faces carrying loads / supports / interfaces.
    Flow: ``wall_faces`` (no-slip walls), ``flow`` = {speed [m/s], nu [m^2/s], length [project
    units] of the immersed body, turbulent [bool], y_plus}."""
    if preset not in PRESETS:
        raise ValueError(f"unknown preset '{preset}'")
    P = PRESETS[preset]
    L = rep.size
    plan = MeshPlan(preset, physics, global_size=L / P["along"], min_size=0.0, curvature_points=P["curv"],
                    element_order=order, growth=P["growth"])
    tdiv = P["thick"] * (1 if order == 2 else 2)
    locs: dict[tuple[str, int], tuple[float, str]] = {}

    def want(kind, i, h, why):
        cur = locs.get((kind, i))
        if h < plan.global_size * 0.999 and (cur is None or h < cur[0]):
            locs[(kind, i)] = (h, why)

    if physics in ("structural", "thermal", "modal"):
        if rep.min_thickness and rep.min_thickness / tdiv < plan.global_size and physics != "thermal":
            # the whole part is thin: global size from the wall thickness
            thin_faces = [f for f in rep.faces if f.thickness is not None and f.thickness / tdiv < plan.global_size]
            if len(thin_faces) > 0.6 * len(rep.faces):
                plan.global_size = max(rep.min_thickness / tdiv, L / 400)
                plan.notes.append(f"thin-walled part: global size set to wall thickness / {tdiv}")
        for f in rep.faces:
            if f.thickness is not None and physics != "thermal":
                want("face", f.index, f.thickness / tdiv, f"wall thickness {f.thickness:.3g} / {tdiv}")
        if rep.thin_edges:
            plan.notes.append("thin tapering regions (e.g. a trailing edge) near edge(s) " +
                              ", ".join(str(e + 1) for e, _ in rep.thin_edges[:6]) +
                              ": not refined separately; the mesh follows the geometry there. Check the "
                              "element quality report.")
            if f.role == "hole":
                want("face", f.index, 2 * math.pi * f.min_radius / P["hole"],
                     f"hole r = {f.min_radius:.3g}, {P['hole']} elements around")
            elif f.role == "fillet_concave":
                want("face", f.index, 0.5 * math.pi * f.min_radius / P["fillet"],
                     f"concave fillet r = {f.min_radius:.3g} (stress raiser), {P['fillet']} per 90°")
        for i in bc_faces or []:
            if P["bc_div"] > 1:
                want("face", i, plan.global_size / P["bc_div"], "load / support / interface face")
        if rep.concave_edges:
            for e in rep.concave_edges:
                want("edge", e, plan.global_size / max(P["bc_div"], 2), "sharp re-entrant edge")
            plan.notes.append(f"{len(rep.concave_edges)} sharp re-entrant edge(s): the elastic stress there is "
                              "singular and does not converge with refinement; judge stresses away from these "
                              "edges, or model the real fillet radius.")
    elif physics == "cfd":
        walls = wall_faces or []
        fl = flow or {}
        Lb = float(fl.get("length") or L)
        hw = Lb / {"quick": 20, "balanced": 40, "validation": 70}[preset]
        for i in walls:
            want("face", i, hw, "no-slip wall")
        if fl.get("speed") and fl.get("nu"):
            turb = bool(fl.get("turbulent", True))
            yp = float(fl.get("y_plus", 1.0 if preset != "quick" else 30.0))
            y1 = _first_layer(float(fl["speed"]), float(fl["nu"]), Lb * length_to_m, yp, turb) / length_to_m
            plan.bl_first = y1
            g = 1.2
            # layers until the cell height reaches ~ the wall size / 3 (smooth transition)
            plan.bl_layers = int(max(3, min(30, math.ceil(math.log(max(hw / 3 / y1, 1.0001)) / math.log(g)))))
            plan.notes.append(f"first wall cell {y1:.3g} for y+ ≈ {yp:g} ({'turbulent' if turb else 'laminar'} "
                              "flat-plate estimate; check y+ in the results)")
        plan.element_order = 1
    # group local sizes by (kind, size, reason)
    groups: dict[tuple, list[int]] = {}
    for (kind, i), (h, why) in sorted(locs.items()):
        groups.setdefault((kind, round(h, 12), why), []).append(i)
    plan.local = [LocalSize(k, idx, h, why) for (k, h, why), idx in groups.items()]
    sizes = [ls.size for ls in plan.local] + [plan.global_size]
    plan.min_size = min(sizes) / 2.0
    _estimate(plan, rep, body)
    if max_elements and plan.estimated_elements > max_elements:
        f = (plan.estimated_elements / max_elements) ** (1.0 / 3.0)
        before = plan.estimated_elements
        plan.global_size *= f
        plan.min_size *= f
        for ls in plan.local:
            ls.size *= f
        _estimate(plan, rep, body)
        plan.notes.append(f"resource budget: the {preset} plan needed ~{before:,} elements (> {max_elements:,}); "
                          f"all sizes were enlarged x{f:.2f}. Accuracy is lower than the preset intends.")
    return plan


def _zone(h0: float, hg: float, g: float, depth: float, kind: str) -> tuple[float, float]:
    """(elements, volume) of a refinement zone whose size grows linearly from h0 at the target
    with rate (g - 1) up to hg (Gmsh threshold field). Face zones: per unit area up to ``depth``;
    edge zones: per unit length, a half-cylinder (body on one side of the surface)."""
    if h0 >= hg:
        return 0.0, 0.0
    D = min((hg - h0) / max(g - 1.0, 1e-6), depth)
    d = np.linspace(0.0, D, 400)
    h = h0 + (g - 1.0) * d
    if kind == "face":
        dens = 1.0 / (TET_VOLUME_FACTOR * h ** 3)
        return float(np.trapezoid(dens, d)), D
    dens = 0.5 * math.pi * d / (TET_VOLUME_FACTOR * h ** 3)
    return float(np.trapezoid(dens, d)), 0.25 * math.pi * D ** 2


def _estimate(plan: MeshPlan, rep: GeometryReport, body=None) -> None:
    """Element / node / memory estimate.

    With thickness samples (the usual case) the volume behind every surface sample (area share x
    half the local thickness) is meshed at the local size h = min(size field, local thickness):
    the size field grows from each refinement target at rate (growth - 1) to the global size,
    and no element can be thicker than the wall it fills. Without samples a zone integral is
    used. Checked against Gmsh meshes (plate, bracket, wing) within about a factor 1.5."""
    hg, g = plan.global_size, plan.growth
    pts, th = rep.sample_points, rep.sample_thickness
    if pts is not None and th is not None and len(pts) == len(th) and len(pts) > 0 and body is not None:
        from scipy.spatial import cKDTree
        from opencae.geometry.analysis import _ray_hits
        tess = body.tessellation
        P, T = np.asarray(tess.points, float), np.asarray(tess.triangles, int)
        lo, hi = (np.asarray(x, float) for x in body.bbox)
        rng = np.random.default_rng(1)
        q = lo + rng.random((6000, 3)) * (hi - lo)
        # inside test: parity of crossings along a slightly skewed ray
        d = np.array([1.0, 0.0173, 0.0091])
        d /= np.linalg.norm(d)
        cnt = np.zeros(len(q), int)
        o = q.copy()
        for _ in range(64):  # walk crossing by crossing
            hit = _ray_hits(o, np.repeat(d[None], len(o), 0), P, T)
            ok = np.isfinite(hit)
            if not ok.any():
                break
            cnt += ok
            o = np.where(ok[:, None], o + (hit[:, None] + 1e-9 * rep.size) * d, o)
            o[~ok] = hi + 10 * rep.size  # done
        inside = (cnt % 2) == 1
        qi = q[inside]
        if len(qi) >= 50:
            h = np.full(len(qi), hg)
            tri_face = np.asarray(tess.tri_face)
            for ls in plan.local:
                tgt = []
                for i in ls.indices:
                    if ls.kind == "face":
                        tri = T[tri_face == i]
                        if len(tri):
                            # points on the face, spaced ~ the local size
                            a, b, c = P[tri[:, 0]], P[tri[:, 1]], P[tri[:, 2]]
                            ar = 0.5 * np.linalg.norm(np.cross(b - a, c - a), axis=1)
                            m = int(min(20000, max(50, ar.sum() / max(ls.size, 1e-12) ** 2)))
                            k = rng.choice(len(tri), size=m, p=ar / ar.sum())
                            r1, r2 = rng.random(m), rng.random(m)
                            fl = r1 + r2 > 1
                            r1, r2 = np.where(fl, 1 - r1, r1), np.where(fl, 1 - r2, r2)
                            tgt.append(a[k] + r1[:, None] * (b[k] - a[k]) + r2[:, None] * (c[k] - a[k]))
                    elif i < len(tess.edge_polylines) and len(tess.edge_polylines[i]):
                        pl = np.asarray(tess.edge_polylines[i])
                        s_ = np.r_[0, np.cumsum(np.linalg.norm(np.diff(pl, axis=0), axis=1))]
                        u = np.linspace(0, s_[-1], int(min(5000, max(2, s_[-1] / max(ls.size, 1e-12)))))
                        tgt.append(np.c_[[np.interp(u, s_, pl[:, j]) for j in range(3)]].T)
                if tgt:
                    dist, _ = cKDTree(np.vstack(tgt)).query(qi)
                    h = np.minimum(h, ls.size + (g - 1.0) * dist)
            # no element is thicker than the wall around it (thickness at the nearest surface sample)
            tw = np.where(np.isfinite(th), th, rep.size)
            dsurf, j = cKDTree(pts).query(qi)
            h = np.minimum(h, np.maximum(tw[j], plan.min_size or 0.0))
            # curvature sizing (Gmsh: curvature_points elements per 2 pi R), growing into the volume
            if rep.sample_radius is not None and plan.curvature_points:
                hc = 2 * math.pi * rep.sample_radius[j] / plan.curvature_points
                h = np.minimum(h, hc + (g - 1.0) * dsurf)
            h = np.maximum(h, plan.min_size or 1e-12)
            n = float(np.mean(1.0 / (TET_VOLUME_FACTOR * h ** 3))) * rep.volume
        else:
            pts = None
    if pts is None or th is None or body is None:
        faces = {f.index: f for f in rep.faces}
        n, zone_vol = 0.0, 0.0
        for ls in plan.local:
            for i in ls.indices:
                if ls.kind == "face" and i in faces:
                    ne, dv = _zone(ls.size, hg, g, min(faces[i].thickness or np.inf, 0.5 * rep.size), "face")
                    n += ne * faces[i].area
                    zone_vol += dv * faces[i].area
                elif ls.kind == "edge":
                    ne, av = _zone(ls.size, hg, g, 0.25 * rep.size, "edge")
                    n += ne * rep.edge_lengths.get(i, 0.0)
                    zone_vol += av * rep.edge_lengths.get(i, 0.0)
        n += max(rep.volume - zone_vol, 0.0) / (TET_VOLUME_FACTOR * hg ** 3)
    n = int(n * CALIBRATION)
    plan.estimated_elements = n
    plan.estimated_nodes = int(n * (1.75 if plan.element_order == 2 else 0.22))
    plan.estimated_memory_mb, plan.estimated_seconds = solver_cost(3 * plan.estimated_nodes)


# Element-count calibration: Gmsh produced 0.9-2.5x the raw estimate on the plate, bracket and wing
# test cases (geometric mean 1.7); the estimate is quoted as "about a factor 2".
CALIBRATION = 1.7


def solver_cost(dofs: int) -> tuple[float, float]:
    """(memory MB, seconds) of a CalculiX linear static solve (SPOOLES, 2 threads), fitted to
    measurements with ccx 2.21 on Linux: 99k DOF 690 MB / 7 s, 204k 2050 MB / 23 s,
    409k 4670 MB / 75 s. Other platforms and solvers (PARDISO) differ."""
    k = max(dofs, 1) / 1e5
    return 7.0 * k ** 0.35 * dofs / 1000.0, 7.0 * k ** 1.67


def machine_memory_mb() -> float:
    """Physical memory of this computer in MB (0 if unknown)."""
    import os
    import sys
    try:
        if sys.platform == "win32":
            import ctypes

            class MS(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            st = MS()
            st.dwLength = ctypes.sizeof(MS)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
            return st.ullTotalPhys / 2 ** 20
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 2 ** 20
    except (OSError, ValueError, AttributeError):
        return 0.0


def default_budget() -> dict:
    """Resource budget: solver memory at most half of the RAM, 30 min per solve."""
    ram = machine_memory_mb()
    return {"memory_mb": 0.5 * ram if ram else 4000.0, "seconds": 1800.0}


def check_budget(plan: MeshPlan, budget: Optional[dict] = None) -> list[str]:
    """Warnings when the estimated cost exceeds the budget."""
    b = budget or default_budget()
    w = []
    if plan.estimated_memory_mb > b["memory_mb"]:
        w.append(f"estimated solver memory {plan.estimated_memory_mb:,.0f} MB exceeds the budget "
                 f"({b['memory_mb']:,.0f} MB): use a coarser preset, refine fewer faces, or a symmetry model")
    if plan.estimated_seconds > b["seconds"]:
        w.append(f"estimated solve time {plan.estimated_seconds / 60:,.0f} min exceeds the budget "
                 f"({b['seconds'] / 60:,.0f} min)")
    return w


def apply_plan(session, plan: MeshPlan, body_id: Optional[str] = None) -> None:
    """Write a plan into the active study's mesh settings (refinements keyed by the body's faces)."""
    from opencae.geometry.references import make_ref
    from opencae.project.model import MeshRefinement
    ms = session.project.mesh_settings
    bid = body_id or session.analysis_body().id
    b = session.bodies[bid]
    ms.sizing_mode = "size"
    ms.size_max = float(plan.global_size)
    ms.size_min = float(plan.min_size)
    ms.curvature_points = int(plan.curvature_points)
    ms.element_order = int(plan.element_order)
    ms.growth_rate = float(plan.growth)
    ms.refinements = [r for r in ms.refinements if not r.name.startswith("auto: ")]
    for ls in plan.local:
        refs = [make_ref(b, ls.kind, i) for i in ls.indices]
        ms.refinements.append(MeshRefinement(refs, float(ls.size), name=f"auto: {ls.reason}"))
    if plan.physics == "cfd" and plan.bl_first:
        ms.bl_first, ms.bl_layers = float(plan.bl_first), int(plan.bl_layers)


def bc_face_indices(session, body_id: str) -> list[int]:
    """Faces of a body that carry loads, supports or interactions in the active study."""
    p = session.project
    out = set()
    for item in list(p.constraints) + list(p.loads):
        for r in item.targets:
            if r.body == body_id and r.kind == "face":
                out.add(r.index)
    for it in getattr(p, "interactions", []) or []:
        for r in it.master + it.slave:
            if r.body == body_id and r.kind == "face":
                out.add(r.index)
    return sorted(out)
