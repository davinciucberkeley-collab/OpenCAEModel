"""Finite element mesh container (solver-neutral).

Conventions
-----------
* ``nodes``: (N, 3) coordinates; node *i* is written to the solver as id i+1.
* ``elements``: (E, k) zero-based node indices in **CalculiX/Abaqus order**:

  - C3D4 : corners 1-4 (positive volume: node 4 on the positive side of 1-2-3)
  - C3D10: corners 1-4, then mid-side nodes on edges
    (1-2), (2-3), (3-1), (1-4), (2-4), (3-4)

* ``face_tris[f]``: boundary triangles of CAD face ``f``, (T, 3) or (T, 6);
  6-node order: corners, then mid-side (1-2), (2-3), (3-1).
* ``vertex_nodes[v]``: node index at CAD vertex ``v``.
* ``edge_nodes[e]``: node indices on CAD edge ``e`` (including end vertices).

The CAD-to-mesh association is built from Gmsh's geometric entities after a
validated one-to-one matching of entity signatures (see ``gmsh_mesher``);
it is never inferred from node proximity.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

ELEMENT_TYPES = {"C3D4": 4, "C3D10": 10}
# Edges (corner pairs) of a tet in CalculiX order; the mid-side node of edge k is node 4+k
TET_EDGES = np.array([[0, 1], [1, 2], [2, 0], [0, 3], [1, 3], [2, 3]])
# CalculiX face numbering for tets: F1=1-2-3, F2=1-4-2, F3=2-4-3, F4=3-4-1 (0-based corners)
TET_FACES = np.array([[0, 1, 2], [0, 3, 1], [1, 3, 2], [2, 3, 0]])


@dataclass
class FEMesh:
    nodes: np.ndarray
    elements: np.ndarray
    element_type: str
    body_id: str = ""
    face_tris: dict[int, np.ndarray] = field(default_factory=dict)
    vertex_nodes: dict[int, int] = field(default_factory=dict)
    edge_nodes: dict[int, np.ndarray] = field(default_factory=dict)
    quality: dict[str, np.ndarray] = field(default_factory=dict)
    unmapped_faces: list[int] = field(default_factory=list)
    log: list[str] = field(default_factory=list)

    @property
    def n_nodes(self) -> int:
        return int(len(self.nodes))

    @property
    def n_elements(self) -> int:
        return int(len(self.elements))

    @property
    def order(self) -> int:
        return 2 if self.element_type == "C3D10" else 1

    def face_nodes(self, face: int) -> np.ndarray:
        return np.unique(self.face_tris[face].ravel())

    def corner_elements(self) -> np.ndarray:
        return self.elements[:, :4]

    def boundary_triangles(self) -> np.ndarray:
        """All boundary corner triangles (for display), concatenated."""
        if not self.face_tris:
            return np.zeros((0, 3), dtype=np.int64)
        return np.concatenate([t[:, :3] for t in self.face_tris.values()])

    # ------------------------------------------------------------------ stats
    def stats(self) -> dict[str, Any]:
        q = self.quality
        s: dict[str, Any] = {
            "element_type": self.element_type,
            "nodes": self.n_nodes,
            "elements": self.n_elements,
        }
        for key in ("gamma", "sicn", "sj", "aspect_ratio", "edge_min", "edge_max", "volume"):
            if key in q and len(q[key]):
                a = q[key]
                s[f"{key}_min"] = float(np.min(a))
                s[f"{key}_max"] = float(np.max(a))
                s[f"{key}_mean"] = float(np.mean(a))
        if "sj" in q:
            s["invalid_elements"] = int(np.sum(q["sj"] <= 0))
        if "gamma" in q:
            s["poor_elements_gamma_lt_0.1"] = int(np.sum(q["gamma"] < 0.1))
            s["degenerate_elements"] = int(np.sum(q["gamma"] < 1e-3))
        if "volume" in q:
            s["total_volume"] = float(np.sum(q["volume"]))
        s["mapped_faces"] = len(self.face_tris)
        s["unmapped_faces"] = list(self.unmapped_faces)
        return s

    def warnings(self) -> list[str]:
        w = []
        st = self.stats()
        if st.get("invalid_elements", 0):
            w.append(f"{st['invalid_elements']} element(s) have non-positive Jacobian (inverted) - "
                     "results would be invalid. Refine the mesh or use first-order elements.")
        if st.get("degenerate_elements", 0):
            w.append(f"{st['degenerate_elements']} degenerate (sliver) element(s) with gamma < 1e-3.")
        if st.get("poor_elements_gamma_lt_0.1", 0):
            w.append(f"{st['poor_elements_gamma_lt_0.1']} poor-quality element(s) with gamma < 0.1.")
        if self.unmapped_faces:
            w.append(f"CAD faces {[f + 1 for f in self.unmapped_faces]} could not be mapped to the mesh; "
                     "loads/constraints on them cannot be applied.")
        return w

    # ------------------------------------------------------------------ IO
    def save(self, path: Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        data: dict[str, Any] = {
            "nodes": self.nodes, "elements": self.elements,
            "element_type": np.array(self.element_type), "body_id": np.array(self.body_id),
            "unmapped_faces": np.array(self.unmapped_faces, dtype=np.int64),
            "log": np.array("\n".join(self.log)),
            "vertex_keys": np.array(list(self.vertex_nodes.keys()), dtype=np.int64),
            "vertex_vals": np.array(list(self.vertex_nodes.values()), dtype=np.int64),
        }
        for prefix, dct in (("face", self.face_tris), ("edge", self.edge_nodes)):
            keys = list(dct.keys())
            data[f"{prefix}_keys"] = np.array(keys, dtype=np.int64)
            for k in keys:
                data[f"{prefix}_{k}"] = dct[k]
        for k, v in self.quality.items():
            data[f"q_{k}"] = v
        np.savez_compressed(path, **data)
        return path

    @classmethod
    def load(cls, path: Path) -> "FEMesh":
        z = np.load(path, allow_pickle=False)
        m = cls(nodes=z["nodes"], elements=z["elements"], element_type=str(z["element_type"]),
                body_id=str(z["body_id"]))
        m.unmapped_faces = [int(v) for v in z["unmapped_faces"]]
        m.log = str(z["log"]).split("\n") if str(z["log"]) else []
        m.vertex_nodes = {int(k): int(v) for k, v in zip(z["vertex_keys"], z["vertex_vals"])}
        m.face_tris = {int(k): z[f"face_{k}"] for k in z["face_keys"]}
        m.edge_nodes = {int(k): z[f"edge_{k}"] for k in z["edge_keys"]}
        m.quality = {k[2:]: z[k] for k in z.files if k.startswith("q_")}
        return m

    def to_meshio(self):
        import meshio
        cell = "tetra10" if self.element_type == "C3D10" else "tetra"
        el = self.elements
        if cell == "tetra10":
            # meshio/VTK tetra10 midside order: (0,1),(1,2),(0,2),(0,3),(1,3),(2,3) == CalculiX order
            pass
        return meshio.Mesh(self.nodes, [(cell, el)])

    def export(self, path: Path) -> None:
        path = Path(path)
        if path.suffix.lower() == ".inp":
            from opencae.solver.calculix.deck import write_mesh_block
            path.write_text(write_mesh_block(self))
            return
        self.to_meshio().write(path)
