"""Solid and surface element definitions (CalculiX/Abaqus conventions).

One table drives everything that depends on the element type: Gmsh -> CalculiX
node permutation, element faces (with CalculiX face numbers, used for *DLOAD
Pn), boundary extraction, shape functions for consistent load integration and
display triangulation. Node orderings were verified numerically against Gmsh
(mid-side nodes coincide with edge midpoints on straight-edged meshes).

Volume elements
---------------
* C3D4 / C3D10: tetrahedra; mid-side (CalculiX) on edges 12, 23, 31, 14, 24, 34.
* C3D8 / C3D20: hexahedra; corners 1-4 bottom, 5-8 top; C3D20 mid-side on
  12, 23, 34, 41, 56, 67, 78, 85, 15, 26, 37, 48.

Surface (face) elements: tri3, tri6 (mids 01, 12, 20), quad4, quad8 (mids
01, 12, 23, 30).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np


@dataclass(frozen=True)
class SolidType:
    name: str
    n_nodes: int
    n_corners: int
    order: int
    shape: str  # tet | hex
    gmsh_type: int
    gmsh_to_ccx: tuple[int, ...]
    faces: tuple[tuple[int, ...], ...]  # corner indices of CalculiX faces 1..n
    face_mids: Optional[tuple[tuple[int, ...], ...]]  # mid-side nodes of each face in face-local order
    edges: tuple[tuple[int, int], ...]  # corner edges; for quadratic, mid node of edge k is n_corners + k
    surface_gmsh_type: int
    face_nodes: int  # nodes per face element (3, 6, 4, 8)


TET_FACES = ((0, 1, 2), (0, 3, 1), (1, 3, 2), (2, 3, 0))
TET_EDGES = ((0, 1), (1, 2), (2, 0), (0, 3), (1, 3), (2, 3))
_TET10_FACE_MIDS = ((4, 5, 6), (7, 8, 4), (8, 9, 5), (9, 7, 6))
HEX_FACES = ((0, 1, 2, 3), (4, 7, 6, 5), (0, 4, 5, 1), (1, 5, 6, 2), (2, 6, 7, 3), (3, 7, 4, 0))
HEX_EDGES = ((0, 1), (1, 2), (2, 3), (3, 0), (4, 5), (5, 6), (6, 7), (7, 4), (0, 4), (1, 5), (2, 6), (3, 7))


def _face_mids(faces, edges, nc):
    emap = {frozenset(e): nc + k for k, e in enumerate(edges)}
    return tuple(tuple(emap[frozenset((f[i], f[(i + 1) % len(f)]))] for i in range(len(f))) for f in faces)


SOLID_TYPES: dict[str, SolidType] = {
    "C3D4": SolidType("C3D4", 4, 4, 1, "tet", 4, (0, 1, 2, 3), TET_FACES, None, TET_EDGES, 2, 3),
    "C3D10": SolidType("C3D10", 10, 4, 2, "tet", 11, (0, 1, 2, 3, 4, 5, 6, 7, 9, 8), TET_FACES,
                       _TET10_FACE_MIDS, TET_EDGES, 9, 6),
    "C3D8": SolidType("C3D8", 8, 8, 1, "hex", 5, tuple(range(8)), HEX_FACES, None, HEX_EDGES, 3, 4),
    "C3D20": SolidType("C3D20", 20, 8, 2, "hex", 17,
                       (0, 1, 2, 3, 4, 5, 6, 7, 8, 11, 13, 9, 16, 18, 19, 17, 10, 12, 14, 15),
                       HEX_FACES, _face_mids(HEX_FACES, HEX_EDGES, 8), HEX_EDGES, 16, 8),
}
assert SOLID_TYPES["C3D10"].face_mids == _face_mids(TET_FACES, TET_EDGES, 4)

# Planar cells for 2-D flow (SU2): boundary "faces" are edges (2 nodes); nodes carry 3-D
# coordinates on the planar CAD face. Not used by CalculiX.
TRI_EDGES = ((0, 1), (1, 2), (2, 0))
QUAD_EDGES = ((0, 1), (1, 2), (2, 3), (3, 0))
SOLID_TYPES["F2D3"] = SolidType("F2D3", 3, 3, 1, "tri", 2, (0, 1, 2), TRI_EDGES, None, TRI_EDGES, 1, 2)
SOLID_TYPES["F2D4"] = SolidType("F2D4", 4, 4, 1, "quad", 3, (0, 1, 2, 3), QUAD_EDGES, None, QUAD_EDGES, 1, 2)
PLANAR_TYPES = ("F2D3", "F2D4")


def planar_areas(nodes: np.ndarray, elements: np.ndarray) -> np.ndarray:
    """Areas of planar tri/quad cells (quads split along 0-2)."""
    X = nodes[elements]
    a = 0.5 * np.linalg.norm(np.cross(X[:, 1] - X[:, 0], X[:, 2] - X[:, 0]), axis=1)
    if elements.shape[1] == 4:
        a += 0.5 * np.linalg.norm(np.cross(X[:, 2] - X[:, 0], X[:, 3] - X[:, 0]), axis=1)
    return a

# CalculiX element variants that share a mesh topology
CCX_VARIANTS = {"C3D4": ("C3D4",), "C3D10": ("C3D10",), "C3D8": ("C3D8I", "C3D8"), "C3D20": ("C3D20R", "C3D20")}
VARIANT_INFO = {
    "C3D4": "4-node linear tetrahedron (very stiff in bending; use for checks only)",
    "C3D10": "10-node quadratic tetrahedron (recommended general-purpose element)",
    "C3D8I": "8-node hexahedron with incompatible modes (good bending behaviour)",
    "C3D8": "8-node fully integrated hexahedron (shear-locks in bending)",
    "C3D20R": "20-node quadratic hexahedron, reduced integration (accurate, robust)",
    "C3D20": "20-node quadratic hexahedron, full integration",
}


def solid_type(name: str) -> SolidType:
    base = {"C3D8I": "C3D8", "C3D20R": "C3D20"}.get(name, name)
    return SOLID_TYPES[base]


def face_corners(face_elems: np.ndarray) -> int:
    return 3 if face_elems.shape[1] in (3, 6) else 4


# ----------------------------------------------------------------------------- shape functions
def _tri_gauss():
    a, b = 0.6590276223740922, 0.2319333685530305
    c = 0.1090390090728769
    pts = np.array([[a, b], [b, a], [a, c], [c, a], [b, c], [c, b]])  # 6-pt degree-3 rule
    return pts, np.full(6, 1.0 / 12.0)


def _quad_gauss(n=3):
    x, w = np.polynomial.legendre.leggauss(n)
    P = np.array([[a, b] for a in x for b in x])
    W = np.array([wa * wb for wa in w for wb in w])
    return P, W


def surface_shape(nn: int, xi: float, eta: float) -> tuple[np.ndarray, np.ndarray]:
    """Shape functions (nn,) and derivatives (nn, 2) of a face element."""
    if nn in (3, 6):
        L = np.array([1 - xi - eta, xi, eta])
        dL = np.array([[-1, -1], [1, 0], [0, 1]], float)
        if nn == 3:
            return L, dL
        N = np.zeros(6)
        dN = np.zeros((6, 2))
        for i in range(3):
            N[i] = L[i] * (2 * L[i] - 1)
            dN[i] = (4 * L[i] - 1) * dL[i]
        for k, (i, j) in enumerate(((0, 1), (1, 2), (2, 0))):
            N[3 + k] = 4 * L[i] * L[j]
            dN[3 + k] = 4 * (L[i] * dL[j] + L[j] * dL[i])
        return N, dN
    s = np.array([[-1, -1], [1, -1], [1, 1], [-1, 1]], float)
    if nn == 4:
        N = 0.25 * (1 + s[:, 0] * xi) * (1 + s[:, 1] * eta)
        dN = np.stack([0.25 * s[:, 0] * (1 + s[:, 1] * eta), 0.25 * s[:, 1] * (1 + s[:, 0] * xi)], -1)
        return N, dN
    N = np.zeros(8)
    dN = np.zeros((8, 2))
    for i in range(4):
        a, b = s[i]
        N[i] = 0.25 * (1 + a * xi) * (1 + b * eta) * (a * xi + b * eta - 1)
        dN[i, 0] = 0.25 * a * (1 + b * eta) * (2 * a * xi + b * eta)
        dN[i, 1] = 0.25 * b * (1 + a * xi) * (a * xi + 2 * b * eta)
    for k, (i, j) in enumerate(((0, 1), (1, 2), (2, 3), (3, 0))):
        a, b = (s[i] + s[j]) / 2
        if a == 0:
            N[4 + k] = 0.5 * (1 - xi * xi) * (1 + b * eta)
            dN[4 + k] = [-xi * (1 + b * eta), 0.5 * b * (1 - xi * xi)]
        else:
            N[4 + k] = 0.5 * (1 + a * xi) * (1 - eta * eta)
            dN[4 + k] = [0.5 * a * (1 - eta * eta), -eta * (1 + a * xi)]
    return N, dN


def surface_integrals(nodes: np.ndarray, faces: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """For each face element: exact integral of each shape function over the (curved)
    face, area, and area-weighted normal vector (orientation as given by node order)."""
    nn = faces.shape[1]
    P, W = _tri_gauss() if nn in (3, 6) else _quad_gauss(3)
    X = nodes[faces]  # (F, nn, 3)
    wN = np.zeros((len(faces), nn))
    area = np.zeros(len(faces))
    nvec = np.zeros((len(faces), 3))
    for (xi, eta), w in zip(P, W):
        N, dN = surface_shape(nn, xi, eta)
        t1 = np.einsum("fka,k->fa", X, dN[:, 0])
        t2 = np.einsum("fka,k->fa", X, dN[:, 1])
        nv = np.cross(t1, t2)
        dA = np.linalg.norm(nv, axis=1) * w
        wN += dA[:, None] * N[None, :]
        area += dA
        nvec += nv * w
    return wN, area, nvec


def solid_shape(et: SolidType, r: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Shape functions and natural derivatives at point r (3,) for a solid element."""
    xi, eta, ze = r
    if et.shape == "tet":
        L = np.array([1 - xi - eta - ze, xi, eta, ze])
        dL = np.array([[-1, -1, -1], [1, 0, 0], [0, 1, 0], [0, 0, 1]], float)
        if et.n_nodes == 4:
            return L, dL
        N = np.zeros(10)
        dN = np.zeros((10, 3))
        for i in range(4):
            N[i] = L[i] * (2 * L[i] - 1)
            dN[i] = (4 * L[i] - 1) * dL[i]
        for k, (i, j) in enumerate(TET_EDGES):
            N[4 + k] = 4 * L[i] * L[j]
            dN[4 + k] = 4 * (L[i] * dL[j] + L[j] * dL[i])
        return N, dN
    s = np.array([[-1, -1, -1], [1, -1, -1], [1, 1, -1], [-1, 1, -1],
                  [-1, -1, 1], [1, -1, 1], [1, 1, 1], [-1, 1, 1]], float)
    if et.n_nodes == 8:
        f = 1 + s * r  # (8,3)
        N = 0.125 * f.prod(axis=1)
        dN = 0.125 * np.stack([s[:, 0] * f[:, 1] * f[:, 2], s[:, 1] * f[:, 0] * f[:, 2],
                               s[:, 2] * f[:, 0] * f[:, 1]], -1)
        return N, dN
    N = np.zeros(20)
    dN = np.zeros((20, 3))
    for i in range(8):
        a = s[i]
        f = 1 + a * r
        g = a @ r - 2
        N[i] = 0.125 * f.prod() * g
        for d in range(3):
            o = [k for k in range(3) if k != d]
            dN[i, d] = 0.125 * a[d] * f[o[0]] * f[o[1]] * (g + f[d])
    for k, (i, j) in enumerate(HEX_EDGES):
        m = (s[i] + s[j]) / 2
        d0 = int(np.where(m == 0)[0][0])  # the coordinate along the edge
        o = [q for q in range(3) if q != d0]
        fo = (1 + m[o[0]] * r[o[0]]) * (1 + m[o[1]] * r[o[1]])
        N[8 + k] = 0.25 * (1 - r[d0] ** 2) * fo
        dN[8 + k, d0] = -0.5 * r[d0] * fo
        dN[8 + k, o[0]] = 0.25 * (1 - r[d0] ** 2) * m[o[0]] * (1 + m[o[1]] * r[o[1]])
        dN[8 + k, o[1]] = 0.25 * (1 - r[d0] ** 2) * m[o[1]] * (1 + m[o[0]] * r[o[0]])
    return N, dN


def solid_gauss(et: SolidType):
    if et.shape == "tet":
        if et.order == 1:
            return np.array([[0.25, 0.25, 0.25]]), np.array([1 / 6])
        a, b = 0.5854101966249685, 0.1381966011250105
        return np.array([[b, b, b], [a, b, b], [b, a, b], [b, b, a]]), np.full(4, 1 / 24)
    x, w = np.polynomial.legendre.leggauss(3)
    P = np.array([[a, b, c] for a in x for b in x for c in x])
    W = np.array([wa * wb * wc for wa in w for wb in w for wc in w])
    return P, W


def nodal_volume_weights(nodes: np.ndarray, elements: np.ndarray, element_type: str) -> tuple[np.ndarray, float]:
    """w_a = integral of N_a over the mesh (consistent body-force weights) and total volume."""
    et = solid_type(element_type)
    X = nodes[elements]
    w = np.zeros(len(nodes))
    vol = 0.0
    P, W = solid_gauss(et)
    for r, wt in zip(P, W):
        N, dN = solid_shape(et, r)
        J = np.einsum("eka,kb->eab", X, dN)
        dV = np.linalg.det(J) * wt
        np.add.at(w, elements.ravel(), (dV[:, None] * N[None, :]).ravel())
        vol += float(dV.sum())
    return w, vol


def element_volumes(nodes: np.ndarray, elements: np.ndarray, element_type: str) -> np.ndarray:
    if element_type in PLANAR_TYPES:
        return planar_areas(nodes, elements)
    et = solid_type(element_type)
    X = nodes[elements]
    P, W = solid_gauss(et)
    v = np.zeros(len(elements))
    for r, wt in zip(P, W):
        _, dN = solid_shape(et, r)
        v += np.linalg.det(np.einsum("eka,kb->eab", X, dN)) * wt
    return v


def element_centroid_jacobian_sign(nodes: np.ndarray, elements: np.ndarray, element_type: str) -> np.ndarray:
    """Sign of det J at the element centre (orientation check)."""
    et = solid_type(element_type)
    r = np.array([0.25, 0.25, 0.25]) if et.shape == "tet" else np.zeros(3)
    _, dN = solid_shape(et, r)
    return np.sign(np.linalg.det(np.einsum("eka,kb->eab", nodes[elements], dN)))


def boundary_faces(elements: np.ndarray, element_type: str) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Faces used by exactly one element: (element index, CalculiX face number 1..n, full face nodes)."""
    et = solid_type(element_type)
    F = np.array(et.faces)  # (nf, k)
    nf = len(F)
    corners = elements[:, F].reshape(-1, F.shape[1])
    key = np.sort(corners, axis=1)
    _, inv, counts = np.unique(key, axis=0, return_inverse=True, return_counts=True)
    idx = np.nonzero(counts[inv.ravel()] == 1)[0]
    el, fn = idx // nf, idx % nf
    full = corners[idx]
    if et.face_mids is not None:
        M = np.array(et.face_mids)
        full = np.concatenate([full, elements[el[:, None], M[fn]]], axis=1)
    return el, fn + 1, full


def face_display_triangles(faces: np.ndarray) -> np.ndarray:
    """Split face elements into linear triangles for display (uses mid-side nodes)."""
    nn = faces.shape[1]
    if nn == 3:
        return faces
    if nn == 6:
        return faces[:, [[0, 3, 5], [3, 1, 4], [5, 4, 2], [3, 4, 5]]].reshape(-1, 3)
    if nn == 4:
        return faces[:, [[0, 1, 2], [0, 2, 3]]].reshape(-1, 3)
    return faces[:, [[0, 4, 7], [4, 1, 5], [5, 2, 6], [6, 3, 7], [4, 5, 7], [5, 6, 7]]].reshape(-1, 3)


def face_display_edges(faces: np.ndarray) -> np.ndarray:
    nn = faces.shape[1]
    nc = 3 if nn in (3, 6) else 4
    if nn == nc:
        return np.concatenate([faces[:, [i, (i + 1) % nc]] for i in range(nc)])
    return np.concatenate([np.concatenate([faces[:, [i, nc + i]], faces[:, [nc + i, (i + 1) % nc]]])
                           for i in range(nc)])
