"""Solid element quality measures (computed on the actual element geometry).

* ``volume``: element volume by Gauss integration (exact for straight-sided
  elements, includes mid-side curvature for quadratic ones).
* ``edge_min`` / ``edge_max`` / ``size``: corner edge lengths.
* ``aspect_ratio``: tets: longest edge / (2*sqrt(6) * inradius), 1 for a
  regular tetrahedron; hexes: longest / shortest corner edge, 1 for a cube.

Gmsh supplies further measures used alongside these:

* ``gamma`` (tets only): inscribed/circumscribed radius ratio, 1 = regular.
* ``sicn``: signed inverse condition number, 1 = ideal, negative = inverted.
* ``sj``: minimum scaled Jacobian over the element including curvature of
  quadratic elements; <= 0 means an invalid element.
"""
from __future__ import annotations

import numpy as np

from opencae.meshing.elements import element_volumes, solid_type


def element_metrics(nodes: np.ndarray, elements: np.ndarray, element_type: str) -> dict[str, np.ndarray]:
    et = solid_type(element_type)
    E = np.array(et.edges)
    c = nodes[elements[:, :et.n_corners]]
    edges = np.linalg.norm(c[:, E[:, 1]] - c[:, E[:, 0]], axis=2)
    vol = element_volumes(nodes, elements, element_type)
    if et.shape == "tet":
        p0, p1, p2, p3 = (c[:, i] for i in range(4))

        def tri_area(a, b, cc):
            return 0.5 * np.linalg.norm(np.cross(b - a, cc - a), axis=1)

        area = tri_area(p0, p1, p2) + tri_area(p0, p1, p3) + tri_area(p1, p2, p3) + tri_area(p0, p2, p3)
        cvol = np.einsum("ij,ij->i", np.cross(p1 - p0, p2 - p0), p3 - p0) / 6.0
        with np.errstate(divide="ignore", invalid="ignore"):
            ar = edges.max(axis=1) / (2.0 * np.sqrt(6.0) * (3.0 * np.abs(cvol) / area))
    else:
        with np.errstate(divide="ignore", invalid="ignore"):
            ar = edges.max(axis=1) / edges.min(axis=1)
    ar = np.where(np.isfinite(ar), ar, np.inf)
    return {"volume": vol, "aspect_ratio": ar, "edge_min": edges.min(axis=1), "edge_max": edges.max(axis=1),
            "size": edges.mean(axis=1)}


def tet_metrics(nodes: np.ndarray, elements: np.ndarray) -> dict[str, np.ndarray]:
    """Backward-compatible wrapper."""
    return element_metrics(nodes, elements, "C3D10" if elements.shape[1] == 10 else "C3D4")
