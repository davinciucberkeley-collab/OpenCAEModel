"""Finite element mesh container (solver-neutral).

Conventions
-----------
* ``nodes``: (N, 3) coordinates; node *i* is written to the solver as id i+1.
* ``elements``: (E, k) zero-based node indices in **CalculiX/Abaqus order**
  (all types are defined in :mod:`opencae.meshing.elements`), for example:

  - C3D4 : corners 1-4 (positive volume: node 4 on the positive side of 1-2-3)
  - C3D10: corners 1-4, then mid-side nodes on edges
    (1-2), (2-3), (3-1), (1-4), (2-4), (3-4)

* ``face_tris[f]``: boundary face elements of CAD face ``f``: (T, 3|6) triangles
  for tet meshes, (T, 4|8) quadrilaterals for hex meshes; corners first, then
  mid-side nodes (1-2), (2-3), ... in cyclic order.
* ``vertex_nodes[v]``: node index at CAD vertex ``v``.
* ``edge_nodes[e]``: node indices on CAD edge ``e`` (including end vertices).

The CAD-to-mesh association is built from Gmsh's geometric entities after a
validated one-to-one matching of entity signatures (see ``gmsh_mesher``);
it is never inferred from node proximity.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import numpy as np

ELEMENT_TYPES = {"C3D4": 4, "C3D10": 10, "C3D8": 8, "C3D20": 20}
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
    body_ids: list[str] = field(default_factory=list)  # bodies in this (bonded) mesh; slot = position
    element_body: Optional[np.ndarray] = None  # (E,) body slot of each element

    def __post_init__(self) -> None:
        if not self.body_ids:
            self.body_ids = [self.body_id] if self.body_id else [""]
        if self.element_body is None or len(self.element_body) != len(self.elements):
            self.element_body = np.zeros(len(self.elements), dtype=np.int64)

    # ---- body-aware entity keys (slot * stride + CAD index; slot 0 == CAD index) ----
    KEY_STRIDE = 1_000_000

    def slot(self, body: str) -> Optional[int]:
        if body in self.body_ids:
            return self.body_ids.index(body)
        if len(self.body_ids) == 1 and not self.body_ids[0]:
            return 0
        return None

    def key(self, body: str, index: int) -> Optional[int]:
        sl = self.slot(body)
        return None if sl is None or index < 0 else sl * self.KEY_STRIDE + int(index)

    def body_elements(self, body: str) -> np.ndarray:
        sl = self.slot(body)
        return np.zeros(0, dtype=np.int64) if sl is None else np.nonzero(self.element_body == sl)[0]

    @property
    def n_nodes(self) -> int:
        return int(len(self.nodes))

    @property
    def n_elements(self) -> int:
        return int(len(self.elements))

    @property
    def etype(self):
        from opencae.meshing.elements import solid_type
        return solid_type(self.element_type)

    @property
    def order(self) -> int:
        return self.etype.order

    @property
    def shape(self) -> str:
        return self.etype.shape

    def face_nodes(self, face: int) -> np.ndarray:
        return np.unique(self.face_tris[face].ravel())

    def corner_elements(self) -> np.ndarray:
        return self.elements[:, :self.etype.n_corners]

    def boundary_triangles(self) -> np.ndarray:
        """All boundary faces split into linear display triangles, concatenated."""
        from opencae.meshing.elements import face_display_triangles
        if not self.face_tris:
            return np.zeros((0, 3), dtype=np.int64)
        return np.concatenate([face_display_triangles(t) for t in self.face_tris.values()])

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
        qkey = "gamma" if "gamma" in q else "sicn" if "sicn" in q else None
        if qkey:
            s["quality_measure"] = qkey
            s["poor_elements_gamma_lt_0.1"] = int(np.sum(q[qkey] < 0.1))
            s["degenerate_elements"] = int(np.sum(q[qkey] < 1e-3))
        if "target_elements" in q and len(q["target_elements"]):
            s["target_elements"] = int(q["target_elements"][0])
        if "volume" in q:
            s["total_volume"] = float(np.sum(q["volume"]))
        s["mapped_faces"] = len(self.face_tris)
        s["unmapped_faces"] = list(self.unmapped_faces)
        return s

    def warnings(self, quality_threshold: float = 0.1) -> list[str]:
        w = []
        st = self.stats()
        qk = st.get("quality_measure")
        if qk and quality_threshold != 0.1:
            st["poor_elements_gamma_lt_0.1"] = int(np.sum(self.quality[qk] < quality_threshold))
        if st.get("invalid_elements", 0):
            w.append(f"{st['invalid_elements']} element(s) have non-positive Jacobian (inverted) - "
                     "results would be invalid. Refine the mesh or use first-order elements.")
        qm = st.get("quality_measure", "gamma")
        if st.get("degenerate_elements", 0):
            w.append(f"{st['degenerate_elements']} degenerate (sliver) element(s) with {qm} < 1e-3.")
        if st.get("poor_elements_gamma_lt_0.1", 0):
            w.append(f"{st['poor_elements_gamma_lt_0.1']} poor-quality element(s) with {qm} < {quality_threshold:g}.")
        if st.get("target_elements"):
            t, n = st["target_elements"], st["elements"]
            if abs(n - t) > 0.25 * t:
                w.append(f"Generated {n} elements for a target of {t} ({100 * (n - t) / t:+.0f} %).")
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
            "body_ids": np.array(self.body_ids), "element_body": np.asarray(self.element_body, dtype=np.int64),
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
        if "body_ids" in z.files:  # meshes from 0.1 have a single body
            m.body_ids = [str(b) for b in z["body_ids"]]
            m.element_body = z["element_body"]
        return m

    def to_meshio(self):
        import meshio
        # meshio/VTK orderings: tetra10 mid-sides (01,12,02,03,13,23) and hexahedron20
        # (bottom edges, top edges, verticals) both coincide with CalculiX order.
        cell = {"C3D4": "tetra", "C3D10": "tetra10", "C3D8": "hexahedron", "C3D20": "hexahedron20",
                "F2D3": "triangle", "F2D4": "quad"}[self.element_type]
        return meshio.Mesh(self.nodes, [(cell, self.elements)])

    def export(self, path: Path) -> None:
        path = Path(path)
        if path.suffix.lower() == ".inp":
            from opencae.solver.calculix.deck import write_mesh_block
            path.write_text(write_mesh_block(self))
            return
        self.to_meshio().write(path)
