"""Geometry analysis for meshing and manufacturing decisions (OpenCascade backend).

Measures what matters for element sizing and printability, without modifying the body:

* local wall thickness, by casting rays inwards from the surface triangulation
  (Moller-Trumbore intersection against all triangles);
* curvature radii of every face (exact for analytic surfaces, sampled principal curvatures
  for B-splines);
* holes (concave cylindrical faces), fillets (concave or convex blends) and their radii;
* edge convexity (convex / concave / tangent) from ``BRepOffset_Analyse``; concave edges
  without a fillet are re-entrant corners, i.e. stress singularities in linear elasticity;
* small edges and faces (candidates for defeaturing).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from opencae.geometry.kernel import Body


@dataclass
class FaceReport:
    index: int
    surface_type: str
    area: float
    min_radius: float  # smallest radius of curvature (inf for planes)
    thickness: Optional[float]  # typical wall thickness behind the face (median), None if no hit
    thickness_min: Optional[float] = None  # 5th percentile (thin regions of the face)
    role: str = "plane"  # plane | hole | fillet_concave | fillet_convex | curved
    full_circle: bool = False


@dataclass
class GeometryReport:
    body: str
    size: float  # bounding-box diagonal
    extent: list  # bbox edge lengths
    volume: float
    area: float
    min_thickness: Optional[float]
    faces: list[FaceReport] = field(default_factory=list)
    concave_edges: list[int] = field(default_factory=list)  # re-entrant edges not blended by a fillet
    small_edges: list[int] = field(default_factory=list)
    small_faces: list[int] = field(default_factory=list)
    holes: list[tuple[int, float]] = field(default_factory=list)  # (face, radius)
    thin_edges: list[tuple[int, float]] = field(default_factory=list)  # (edge, local thickness) of thin regions
    edge_lengths: dict = field(default_factory=dict)
    # thickness samples: points, face index, thickness (inf = no opposite wall found) - for estimates
    sample_points: Optional[np.ndarray] = None
    sample_faces: Optional[np.ndarray] = None
    sample_thickness: Optional[np.ndarray] = None
    sample_radius: Optional[np.ndarray] = None  # smallest curvature radius at each sample (inf = flat)
    notes: list[str] = field(default_factory=list)

    def summary(self) -> list[str]:
        out = [f"size (bounding-box diagonal) {self.size:.4g}, extents " +
               " x ".join(f"{e:.4g}" for e in self.extent),
               f"volume {self.volume:.4g}, surface area {self.area:.4g}",
               f"minimum wall thickness ≈ {self.min_thickness:.4g}" if self.min_thickness else
               "wall thickness: not measured"]
        if self.holes:
            out.append(f"{len(self.holes)} hole face(s), radii " +
                       ", ".join(f"{r:.3g}" for r in sorted({round(r, 6) for _, r in self.holes})))
        nf = sum(f.role.startswith("fillet") for f in self.faces)
        if nf:
            out.append(f"{nf} fillet face(s), smallest radius "
                       f"{min(f.min_radius for f in self.faces if f.role.startswith('fillet')):.3g}")
        if self.thin_edges:
            out.append(f"thin regions along {len(self.thin_edges)} edge(s), thinnest "
                       f"{min(t for _, t in self.thin_edges):.3g}")
        if self.concave_edges:
            out.append(f"{len(self.concave_edges)} sharp re-entrant edge(s): stresses there are singular")
        if self.small_edges or self.small_faces:
            out.append(f"{len(self.small_edges)} small edge(s), {len(self.small_faces)} small face(s) "
                       "(defeaturing candidates)")
        return out + self.notes


# ------------------------------------------------------------------------- thickness
def _ray_hits(orig: np.ndarray, d: np.ndarray, P: np.ndarray, T: np.ndarray, chunk: int = 256) -> np.ndarray:
    """Distance to the nearest triangle hit along each ray (inf if none). Moller-Trumbore."""
    v0, v1, v2 = P[T[:, 0]], P[T[:, 1]], P[T[:, 2]]
    e1, e2 = v1 - v0, v2 - v0
    out = np.full(len(orig), np.inf)
    eps = 1e-12
    for s in range(0, len(orig), chunk):
        o = orig[s:s + chunk, None, :]
        dd = d[s:s + chunk, None, :]
        pv = np.cross(dd, e2[None])
        det = np.einsum("ijk,jk->ij", pv, e1)
        ok = np.abs(det) > eps
        inv = np.where(ok, 1.0 / np.where(ok, det, 1.0), 0.0)
        tv = o - v0[None]
        u = np.einsum("ijk,ijk->ij", tv, pv) * inv
        qv = np.cross(tv, e1[None])
        v = np.einsum("ijk,ijk->ij", dd, qv) * inv
        t = np.einsum("jk,ijk->ij", e2, qv) * inv
        hit = ok & (u >= 0) & (v >= 0) & (u + v <= 1) & (t > 0)
        t = np.where(hit, t, np.inf)
        out[s:s + chunk] = t.min(axis=1)
    return out


def local_thickness(body: Body, max_samples: int = 3000, seed: int = 0) -> tuple[np.ndarray, np.ndarray]:
    """(face index, thickness) per sampled surface point: distance along the inward normal to
    the opposite surface. Points are spread uniformly over the surface (area-weighted triangle
    choice, uniform barycentric position)."""
    tess = body.tessellation
    if tess is None or len(tess.triangles) == 0:
        return np.zeros(0, int), np.zeros(0)
    P, T = np.asarray(tess.points, float), np.asarray(tess.triangles, int)
    a, b, c = P[T[:, 0]], P[T[:, 1]], P[T[:, 2]]
    n = np.cross(b - a, c - a)
    area = 0.5 * np.linalg.norm(n, axis=1)
    good = area > 0
    # orientation: outward if the enclosed volume (divergence theorem) is positive
    vol = np.einsum("ij,ij->i", a, np.cross(b, c)).sum() / 6.0
    sign = 1.0 if vol > 0 else -1.0
    idx = np.nonzero(good)[0]
    rng = np.random.default_rng(seed)
    idx = rng.choice(idx, size=max_samples, replace=True, p=area[idx] / area[idx].sum())
    r1, r2 = rng.random(len(idx)), rng.random(len(idx))
    flip = r1 + r2 > 1  # uniform point in the triangle
    r1, r2 = np.where(flip, 1 - r1, r1), np.where(flip, 1 - r2, r2)
    # keep away from triangle edges (rays grazing an adjacent face near sharp edges)
    r1, r2 = 0.05 + 0.9 * r1, 0.05 + 0.9 * r2
    cen = a[idx] + r1[:, None] * (b[idx] - a[idx]) + r2[:, None] * (c[idx] - a[idx])
    nin = -sign * n[idx] / np.linalg.norm(n[idx], axis=1)[:, None]
    scale = body.size
    t = _ray_hits(cen + 1e-7 * scale * nin, nin, P, T) + 1e-7 * scale
    _LAST_SAMPLES[id(body)] = cen
    return np.asarray(tess.tri_face)[idx], t


# ------------------------------------------------------------------------- curvature / faces
def _face_radius(face) -> tuple[str, float, bool]:
    """(kind, min radius, concave-cylinder flag) for one TopoDS_Face."""
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.GeomAbs import GeomAbs_Cone, GeomAbs_Cylinder, GeomAbs_Plane, GeomAbs_Sphere, GeomAbs_Torus
    from OCP.TopAbs import TopAbs_REVERSED
    s = BRepAdaptor_Surface(face)
    tp = s.GetType()
    rev = face.Orientation() == TopAbs_REVERSED
    if tp == GeomAbs_Plane:
        return "plane", np.inf, False
    if tp == GeomAbs_Cylinder:
        cyl = s.Cylinder()
        # outward face normal points towards the axis for a hole: natural cylinder normals point outwards,
        # so a reversed cylindrical face is concave (a hole)
        return "cylinder", float(cyl.Radius()), bool(rev)
    if tp == GeomAbs_Sphere:
        return "sphere", float(s.Sphere().Radius()), bool(rev)
    if tp == GeomAbs_Torus:
        return "torus", float(s.Torus().MinorRadius()), bool(rev)
    if tp == GeomAbs_Cone:
        return "cone", np.inf, False
    # free-form: sampled principal curvatures
    from OCP.BRepLProp import BRepLProp_SLProps
    u0, u1, v0, v1 = s.FirstUParameter(), s.LastUParameter(), s.FirstVParameter(), s.LastVParameter()
    kmax = 0.0
    for u in np.linspace(u0, u1, 41)[1:-1]:
        for v in np.linspace(v0, v1, 11)[1:-1]:
            pr = BRepLProp_SLProps(s, float(u), float(v), 2, 1e-9)
            if pr.IsCurvatureDefined():
                kmax = max(kmax, abs(pr.MaxCurvature()), abs(pr.MinCurvature()))
    return "freeform", (1.0 / kmax if kmax > 0 else np.inf), False


_LAST_SAMPLES: dict = {}


def _dist_polyline(p: np.ndarray, pl: np.ndarray) -> float:
    """Distance from a point to a polyline (segments, not only its vertices)."""
    if len(pl) == 0:
        return np.inf
    if len(pl) == 1:
        return float(np.linalg.norm(pl[0] - p))
    a, b = pl[:-1], pl[1:]
    ab = b - a
    tt = np.clip(np.einsum("ij,ij->i", p - a, ab) / np.maximum(np.einsum("ij,ij->i", ab, ab), 1e-300), 0, 1)
    return float(np.min(np.linalg.norm(a + tt[:, None] * ab - p, axis=1)))


def _thin_points(body: Body, fidx, th, faces) -> tuple[np.ndarray, np.ndarray]:
    """Sample points (re-generated deterministically) thinner than half of their face's median."""
    med = {f.index: f.thickness for f in faces if f.thickness}
    keep = np.array([med.get(int(f)) is not None and np.isfinite(t) and t < 0.5 * med[int(f)]
                     for f, t in zip(fidx, th)], bool)
    pts = _LAST_SAMPLES.get(id(body))
    if pts is None or len(pts) != len(th) or not keep.any():
        return np.zeros((0, 3)), np.zeros(0)
    return pts[keep], np.asarray(th)[keep]


def _sample_radii(body: Body, pts: np.ndarray, fidx: np.ndarray) -> np.ndarray:
    """Smallest principal radius of curvature of the surface at each sample point."""
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepLProp import BRepLProp_SLProps
    from OCP.BRep import BRep_Tool
    from OCP.GeomAPI import GeomAPI_ProjectPointOnSurf
    from OCP.gp import gp_Pnt
    from opencae.geometry.occ_kernel import faces_of
    faces = faces_of(body.shape)
    out = np.full(len(pts), np.inf)
    for i, f in enumerate(faces):
        sel = np.nonzero(fidx == i)[0]
        if not len(sel):
            continue
        kind, r, _ = _face_radius_kind_only(f)
        if kind == "plane":
            continue
        if kind != "freeform":
            out[sel] = r
            continue
        srf = BRep_Tool.Surface_s(f)
        ad = BRepAdaptor_Surface(f)
        for k in sel:
            pr = GeomAPI_ProjectPointOnSurf(gp_Pnt(*map(float, pts[k])), srf)
            if pr.NbPoints() == 0:
                continue
            u, v = pr.LowerDistanceParameters()
            lp = BRepLProp_SLProps(ad, u, v, 2, 1e-9)
            if lp.IsCurvatureDefined():
                kk = max(abs(lp.MaxCurvature()), abs(lp.MinCurvature()))
                if kk > 0:
                    out[k] = 1.0 / kk
    return out


def _face_radius_kind_only(face) -> tuple[str, float, bool]:
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.GeomAbs import GeomAbs_Cylinder, GeomAbs_Plane, GeomAbs_Sphere, GeomAbs_Torus, GeomAbs_Cone
    s = BRepAdaptor_Surface(face)
    tp = s.GetType()
    if tp in (GeomAbs_Plane, GeomAbs_Cone):
        return "plane", np.inf, False
    if tp == GeomAbs_Cylinder:
        return "cylinder", float(s.Cylinder().Radius()), False
    if tp == GeomAbs_Sphere:
        return "sphere", float(s.Sphere().Radius()), False
    if tp == GeomAbs_Torus:
        return "torus", float(s.Torus().MinorRadius()), False
    return "freeform", np.inf, False


def _u_span(face) -> float:
    from OCP.BRepTools import BRepTools
    u0, u1, _, _ = BRepTools.UVBounds_s(face)
    return float(u1 - u0)


def edge_convexity(body: Body, angle_tol_deg: float = 1.0) -> dict[int, str]:
    """Edge index -> 'convex' | 'concave' | 'tangent' | 'other'."""
    from OCP.BRepOffset import BRepOffset_Analyse
    from OCP.ChFiDS import ChFiDS_TypeOfConcavity
    from opencae.geometry.occ_kernel import edges_of
    an = BRepOffset_Analyse(body.shape, np.radians(angle_tol_deg))
    out = {}
    for i, e in enumerate(edges_of(body.shape)):
        try:
            iv = an.Type(e)
            t = iv.First().Type() if iv.Size() else None
        except Exception:  # noqa: BLE001 - degenerate / seam edges
            t = None
        out[i] = {ChFiDS_TypeOfConcavity.ChFiDS_Concave: "concave", ChFiDS_TypeOfConcavity.ChFiDS_Convex: "convex",
                  ChFiDS_TypeOfConcavity.ChFiDS_Tangential: "tangent"}.get(t, "other")
    return out


def analyse_body(body: Body, thickness_samples: int = 3000) -> GeometryReport:
    from opencae.geometry.occ_kernel import edges_of, faces_of
    lo, hi = (np.asarray(x, float) for x in body.bbox)
    rep = GeometryReport(body=body.name, size=body.size, extent=(hi - lo).tolist(), volume=body.volume,
                         area=body.area, min_thickness=None)
    if body.kind != "solid" or body.shape is None:
        rep.notes.append("tessellated body: only size and thickness are analysed")
    fidx, th = local_thickness(body, thickness_samples) if body.tessellation is not None else (np.zeros(0), [])
    th = np.asarray(th)
    finite = np.isfinite(th)
    rep.sample_points = _LAST_SAMPLES.get(id(body))
    rep.sample_faces, rep.sample_thickness = np.asarray(fidx), th
    if body.kind == "solid" and body.shape is not None and rep.sample_points is not None:
        rep.sample_radius = _sample_radii(body, rep.sample_points, rep.sample_faces)
    if finite.any():
        rep.min_thickness = float(np.percentile(th[finite], 2))
    if body.kind != "solid" or body.shape is None:
        return rep
    conv = edge_convexity(body)
    faces = faces_of(body.shape)
    # faces bounded by concave edges (a concave blend sits between two tangent edges, so the
    # concave edges are those of sharp corners)
    for i, (f, info) in enumerate(zip(faces, body.faces)):
        kind, r, concave = _face_radius(f)
        sel = (fidx == i) & finite
        t_face = float(np.percentile(th[sel], 50)) if sel.sum() >= 3 else None
        t_low = float(np.percentile(th[sel], 5)) if sel.sum() >= 3 else None
        role = "plane" if kind == "plane" else "curved"
        full = False
        if kind == "cylinder":
            full = _u_span(f) > 2 * np.pi - 1e-6
            if concave and full:
                role = "hole"
                rep.holes.append((i, r))
            elif r < 0.1 * body.size:
                role = "fillet_concave" if concave else "fillet_convex"
        elif kind == "torus" and r < 0.1 * body.size:
            role = "fillet_concave" if concave else "fillet_convex"
        rep.faces.append(FaceReport(i, kind, info.area, float(r), t_face, t_low, role, full))
    rep.concave_edges = [i for i, c in conv.items() if c == "concave"]
    # thin regions: samples much thinner than their face's median -> nearest edges (e.g. trailing edges)
    if finite.any() and body.tessellation is not None and body.tessellation.edge_polylines:
        pts, thin_t = _thin_points(body, fidx, th, rep.faces)
        if len(pts):
            polys = body.tessellation.edge_polylines
            best: dict[int, float] = {}
            for p, tt in zip(pts, thin_t):
                d = [_dist_polyline(p, np.asarray(pl, float)) for pl in polys]
                e = int(np.argmin(d))
                if d[e] < 10 * tt + 1e-9 * body.size:
                    best[e] = min(best.get(e, np.inf), float(tt))
            rep.thin_edges = sorted(best.items())
    L = body.size
    rep.small_edges = [e.index for e in body.edges if e.length < 0.01 * L]
    rep.small_faces = [f.index for f in body.faces if f.area < (0.01 * L) ** 2]
    del edges_of
    return rep
