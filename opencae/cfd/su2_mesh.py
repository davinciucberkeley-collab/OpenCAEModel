"""Write OpenCAE meshes in SU2 native format (linear tetrahedra / hexahedra).

Boundary markers are named groups of CAD faces. Points are written in mesh node
order, so SU2 point ids equal OpenCAE node indices (results map back directly).
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

# VTK cell type ids used by SU2
SU2_TET, SU2_HEX, SU2_TRI, SU2_QUAD, SU2_LINE = 10, 12, 5, 9, 3


def write_su2(path: Path, nodes: np.ndarray, elements: np.ndarray, markers: dict[str, np.ndarray],
              scale: float = 1.0) -> Path:
    """nodes (N,3); elements (E,4) tets or (E,8) hexes (corner nodes, OpenCAE/Gmsh order);
    markers: name -> boundary faces (F,3) triangles or (F,4) quads. ``scale`` converts
    the coordinates (e.g. 1e-3 for mm -> m)."""
    nodes = np.asarray(nodes, float) * scale
    el = np.asarray(elements, np.int64)
    etype = {4: SU2_TET, 8: SU2_HEX}[el.shape[1]]
    out = [f"NDIME= 3\nNELEM= {len(el)}\n"]
    out += [f"{etype} " + " ".join(map(str, e)) + f" {i}\n" for i, e in enumerate(el)]
    out.append(f"NPOIN= {len(nodes)}\n")
    out += [f"{x:.15g} {y:.15g} {z:.15g} {i}\n" for i, (x, y, z) in enumerate(nodes)]
    out.append(f"NMARK= {len(markers)}\n")
    for name, faces in markers.items():
        faces = np.asarray(faces, np.int64)
        ft = {3: SU2_TRI, 4: SU2_QUAD}[faces.shape[1]]
        out.append(f"MARKER_TAG= {name}\nMARKER_ELEMS= {len(faces)}\n")
        out += [f"{ft} " + " ".join(map(str, f)) + "\n" for f in faces]
    Path(path).write_text("".join(out))
    return Path(path)


def write_su2_2d(path: Path, nodes2d: np.ndarray, cells: list[np.ndarray], markers: dict[str, np.ndarray],
                 scale: float = 1.0) -> Path:
    """2-D mesh: nodes (N,2), cells: arrays of triangles (T,3) and/or quads (Q,4) (counter-
    clockwise), markers: name -> edges (E,2)."""
    nodes = np.asarray(nodes2d, float) * scale
    rows = []
    k = 0
    for c in cells:
        c = np.asarray(c, np.int64)
        if not len(c):
            continue
        t = {3: SU2_TRI, 4: SU2_QUAD}[c.shape[1]]
        for e in c:
            rows.append(f"{t} " + " ".join(map(str, e)) + f" {k}\n")
            k += 1
    out = [f"NDIME= 2\nNELEM= {k}\n", *rows, f"NPOIN= {len(nodes)}\n"]
    out += [f"{x:.15g} {y:.15g} {i}\n" for i, (x, y) in enumerate(nodes)]
    out.append(f"NMARK= {len(markers)}\n")
    for name, edges in markers.items():
        edges = np.asarray(edges, np.int64)
        out.append(f"MARKER_TAG= {name}\nMARKER_ELEMS= {len(edges)}\n")
        out += [f"{SU2_LINE} {a} {b}\n" for a, b in edges]
    Path(path).write_text("".join(out))
    return Path(path)
