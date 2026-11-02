"""Sampling nodal results at arbitrary points (field transfer, line plots, probes)."""
from __future__ import annotations

import numpy as np

HEX_TO_TETS = np.array([[0, 1, 3, 4], [1, 2, 3, 6], [1, 3, 4, 6], [3, 4, 6, 7], [1, 4, 5, 6]])


def locate(mesh, points, k: int = 24, tol: float = 1e-6):
    """For each point: (tet corner node ids (4,), barycentric weights (4,), inside flag).

    Hexahedra are split into five tetrahedra; interpolation uses corner nodes only (linear),
    so quadratic mid-side values are not used. Points outside the mesh get the closest
    candidate element with weights clamped to its faces."""
    from scipy.spatial import cKDTree
    points = np.atleast_2d(np.asarray(points, float))
    corners = mesh.corner_elements()
    tets = corners if corners.shape[1] == 4 else corners[:, HEX_TO_TETS].reshape(-1, 4)
    X = mesh.nodes[tets]
    tree = cKDTree(X.mean(axis=1))
    k = min(k, len(tets))
    _, cand = tree.query(points, k=k)
    cand = cand.reshape(len(points), k)
    Tinv = np.linalg.inv(np.transpose(X[:, 1:] - X[:, :1], (0, 2, 1)))
    ids = np.zeros((len(points), 4), dtype=np.int64)
    w = np.zeros((len(points), 4))
    inside = np.zeros(len(points), dtype=bool)
    for i, p in enumerate(points):
        c = cand[i]
        lam = np.einsum("tij,tj->ti", Tinv[c], p - X[c, 0])
        bary = np.column_stack([1 - lam.sum(axis=1), lam])
        score = bary.min(axis=1)
        j = int(np.argmax(score))
        inside[i] = score[j] >= -tol
        b = np.clip(bary[j], 0, None)
        w[i] = b / b.sum()
        ids[i] = tets[c[j]]
    return ids, w, inside


def interpolate_nodal(src_mesh, values, dst_points, k: int = 24):
    """Nodal values of ``src_mesh`` interpolated at ``dst_points``. Returns (values, n_outside)."""
    ids, w, inside = locate(src_mesh, dst_points, k)
    v = np.asarray(values)
    out = np.einsum("pk,pk...->p...", w, v[ids])
    return out, int((~inside).sum())


def sample_line(mesh, values, start, end, n: int = 101):
    """Values along the straight line start -> end. Returns (distance, values, inside); points
    outside the solid (holes, concave regions) are NaN."""
    start, end = np.asarray(start, float), np.asarray(end, float)
    t = np.linspace(0.0, 1.0, int(n))
    pts = start[None, :] + t[:, None] * (end - start)[None, :]
    ids, w, inside = locate(mesh, pts)
    v = np.einsum("pk,pk->p", w, np.asarray(values)[ids])
    v[~inside] = np.nan
    return t * np.linalg.norm(end - start), v, inside
