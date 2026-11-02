"""Build PyVista datasets from geometry, mesh and results (no Qt).

Boundary surfaces of the FE mesh are extracted topologically from the
elements (faces used by exactly one element), so every exterior face is shown
even if it could not be mapped to a CAD face. Quadratic faces are split into
four linear triangles so mid-side node values and curvature are displayed.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pyvista as pv

from opencae.geometry.kernel import Body
from opencae.meshing.elements import boundary_faces, face_display_edges, face_display_triangles
from opencae.meshing.mesh_data import FEMesh


def _faces_array(tris: np.ndarray) -> np.ndarray:
    return np.hstack([np.full((len(tris), 1), 3), tris]).ravel().astype(np.int64)


def body_faces_polydata(body: Body) -> pv.PolyData:
    t = body.tessellation
    pd = pv.PolyData(t.points.copy(), _faces_array(t.triangles))
    pd.cell_data["face_id"] = t.tri_face.astype(np.int64)
    return pd


def body_edges_polydata(body: Body) -> pv.PolyData | None:
    polys = [p for p in body.tessellation.edge_polylines if len(p) >= 2]
    if not polys:
        return None
    pts, lines, eid, off = [], [], [], 0
    for i, p in enumerate(body.tessellation.edge_polylines):
        if len(p) < 2:
            continue
        pts.append(p)
        lines.append(np.concatenate([[len(p)], np.arange(off, off + len(p))]))
        eid.append(i)
        off += len(p)
    pd = pv.PolyData(np.concatenate(pts), lines=np.concatenate(lines).astype(np.int64))
    pd.cell_data["edge_id"] = np.array(eid, dtype=np.int64)
    return pd


def body_vertices_polydata(body: Body) -> pv.PolyData | None:
    v = body.tessellation.vertices
    if not len(v):
        return None
    pd = pv.PolyData(v.copy())
    pd.cell_data["vertex_id"] = np.arange(len(v), dtype=np.int64)
    return pd


def face_subset(body: Body, faces: list[int]) -> pv.PolyData | None:
    t = body.tessellation
    m = np.isin(t.tri_face, faces)
    if not m.any():
        return None
    return pv.PolyData(t.points.copy(), _faces_array(t.triangles[m]))


@dataclass
class BoundarySurface:
    point_ids: np.ndarray  # surface point -> mesh node index
    triangles: np.ndarray  # (T,3) indices into point_ids
    owner: np.ndarray  # (T,) element index owning each triangle
    edges: np.ndarray  # (K,2) element edges on the boundary (indices into point_ids) for wireframe

    def polydata(self, nodes: np.ndarray, displacement: np.ndarray | None = None, scale: float = 0.0) -> pv.PolyData:
        pts = nodes[self.point_ids].copy()
        if displacement is not None and scale:
            pts += scale * displacement[self.point_ids]
        pd = pv.PolyData(pts, _faces_array(self.triangles))
        pd.point_data["node_id"] = self.point_ids
        pd.cell_data["element_id"] = self.owner
        return pd

    def edges_polydata(self, nodes, displacement=None, scale=0.0) -> pv.PolyData:
        pts = nodes[self.point_ids].copy()
        if displacement is not None and scale:
            pts += scale * displacement[self.point_ids]
        lines = np.hstack([np.full((len(self.edges), 1), 2), self.edges]).ravel()
        return pv.PolyData(pts, lines=lines.astype(np.int64))


def boundary_surface(mesh: FEMesh) -> BoundarySurface:
    """Exterior faces of any solid mesh, split into linear display triangles (mid-side
    nodes included, so quadratic results and curvature are shown)."""
    if mesh.element_type in ("F2D3", "F2D4"):  # planar flow mesh: the cells themselves are the surface
        cells = mesh.elements
        tris = cells if cells.shape[1] == 3 else cells[:, [[0, 1, 2], [0, 2, 3]]].reshape(-1, 3)
        owner = np.repeat(np.arange(len(cells)), 1 if cells.shape[1] == 3 else 2)
        k = cells.shape[1]
        segs = np.unique(np.sort(np.stack([cells, np.roll(cells, -1, axis=1)], axis=2).reshape(-1, 2), axis=1),
                         axis=0)
        pid, local = np.unique(np.concatenate([tris.ravel(), segs.ravel()]), return_inverse=True)
        return BoundarySurface(point_ids=pid, triangles=local[: tris.size].reshape(-1, 3), owner=owner,
                               edges=local[tris.size:].reshape(-1, 2))
    el, _, full = boundary_faces(mesh.elements, mesh.element_type)
    per = len(face_display_triangles(full[:1])) if len(full) else 1
    tris = face_display_triangles(full)
    owner = np.repeat(el, per)
    segs = np.unique(np.sort(face_display_edges(full), axis=1), axis=0)
    pid, local = np.unique(np.concatenate([tris.ravel(), segs.ravel()]), return_inverse=True)
    lt = local[: tris.size].reshape(-1, 3)
    ls = local[tris.size:].reshape(-1, 2)
    return BoundarySurface(point_ids=pid, triangles=lt, owner=owner, edges=ls)


def auto_deformation_scale(nodes: np.ndarray, displacement: np.ndarray, fraction: float = 0.08) -> float:
    umax = float(np.linalg.norm(displacement, axis=1).max())
    L = float(np.ptp(nodes, axis=0).max())
    if umax <= 0 or L <= 0:
        return 1.0
    s = fraction * L / umax
    # round to 1, 2 or 5 x 10^n for readability
    e = np.floor(np.log10(s))
    m = s / 10 ** e
    m = 1 if m < 2 else 2 if m < 5 else 5
    return float(m * 10 ** e)


def sample_points(points: np.ndarray, n: int = 40) -> np.ndarray:
    if len(points) <= n:
        return points
    idx = np.linspace(0, len(points) - 1, n).astype(int)
    return points[idx]


def face_glyph_points(sub: pv.PolyData, n: int = 25) -> tuple[np.ndarray, np.ndarray]:
    """Evenly spread sample points and outward normals on a face patch for glyphs."""
    tri = sub.triangulate()
    while tri.n_cells < n and tri.n_cells < 5000:
        tri = tri.subdivide(1, "linear")
    cn = tri.compute_normals(cell_normals=True, point_normals=False, auto_orient_normals=False,
                             consistent_normals=False)
    centers = cn.cell_centers().points
    normals = cn.cell_data["Normals"]
    idx = np.linspace(0, len(centers) - 1, min(len(centers), n)).astype(int)
    return centers[idx], normals[idx]
