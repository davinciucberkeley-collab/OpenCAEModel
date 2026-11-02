"""Tetrahedral element quality measures (computed on corner nodes).

* ``aspect_ratio``: longest edge / (2*sqrt(6) * inradius). Equals 1 for a
  regular tetrahedron and grows without bound for degenerate elements.
* ``volume``: signed volume of the corner tetrahedron (CalculiX orientation).
* ``edge_min`` / ``edge_max``: shortest / longest corner edge.

Gmsh supplies further measures used alongside these:

* ``gamma``: inscribed/circumscribed radius ratio, normalised (1 = regular).
* ``sicn``: signed inverse condition number of the corner tet (1 = regular,
  negative = inverted).
* ``sj``: minimum scaled Jacobian over the element. For 10-node elements this
  includes curvature of the mid-side nodes and is the correct validity measure
  (<= 0 means an invalid element).
"""
from __future__ import annotations

import numpy as np

from opencae.meshing.mesh_data import TET_EDGES


def tet_metrics(nodes: np.ndarray, elements: np.ndarray) -> dict[str, np.ndarray]:
    c = nodes[elements[:, :4]]  # (E,4,3)
    p0, p1, p2, p3 = c[:, 0], c[:, 1], c[:, 2], c[:, 3]
    vol = np.einsum("ij,ij->i", np.cross(p1 - p0, p2 - p0), p3 - p0) / 6.0
    edges = np.linalg.norm(c[:, TET_EDGES[:, 1]] - c[:, TET_EDGES[:, 0]], axis=2)  # (E,6)

    def tri_area(a, b, cc):
        return 0.5 * np.linalg.norm(np.cross(b - a, cc - a), axis=1)

    area = tri_area(p0, p1, p2) + tri_area(p0, p1, p3) + tri_area(p1, p2, p3) + tri_area(p0, p2, p3)
    with np.errstate(divide="ignore", invalid="ignore"):
        inradius = 3.0 * np.abs(vol) / area
        ar = edges.max(axis=1) / (2.0 * np.sqrt(6.0) * inradius)
    ar = np.where(np.isfinite(ar), ar, np.inf)
    return {"volume": vol, "aspect_ratio": ar, "edge_min": edges.min(axis=1), "edge_max": edges.max(axis=1),
            "size": edges.mean(axis=1)}
