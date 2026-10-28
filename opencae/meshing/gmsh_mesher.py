"""Gmsh-based tetrahedral mesher.

Pipeline
--------
1. Import the exact B-rep (``.brep`` written by the geometry kernel).
2. If the body has several solids, fragment them into a conformal mesh.
3. Map every CAD face/edge/vertex to a Gmsh entity by **signature matching**
   (surface area + centre of mass for faces, length + centre of mass for
   edges, coordinates for vertices). A mapping is accepted only if it is
   one-to-one within a tight tolerance; otherwise the entity is reported as
   unmapped and cannot carry loads (no proximity guessing).
4. Apply global size, curvature-based sizing and local refinement fields.
5. Generate a 3-D mesh (first or second order) and optimise it.
6. Extract nodes/elements in CalculiX ordering, boundary triangles per CAD
   face, nodes per CAD edge/vertex, and quality measures.

Gmsh tet10 mid-side order is (0,1),(1,2),(0,2),(0,3),(2,3),(1,3), whereas
CalculiX C3D10 expects (0,1),(1,2),(2,0),(0,3),(1,3),(2,3): nodes 8 and 9 are
swapped. This was verified numerically and is re-checked at run time by
:func:`verify_c3d10_midsides`.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

import numpy as np

from opencae.meshing.mesh_data import TET_EDGES, FEMesh
from opencae.meshing.quality import tet_metrics
from opencae.project.model import MeshSettings

GMSH_TO_CCX_TET10 = np.array([0, 1, 2, 3, 4, 5, 6, 7, 9, 8])


class MeshingError(Exception):
    pass


@dataclass
class TopologySignatures:
    """Signatures of the CAD body's entities (from the geometry kernel)."""

    faces: list[dict[str, Any]]
    edges: list[dict[str, Any]]
    vertices: list[dict[str, Any]]
    size: float

    @classmethod
    def from_body(cls, body) -> "TopologySignatures":
        return cls([f.signature() for f in body.faces], [e.signature() for e in body.edges],
                   [v.signature() for v in body.vertices], body.size)

    def to_dict(self) -> dict:
        return {"faces": self.faces, "edges": self.edges, "vertices": self.vertices, "size": self.size}

    @classmethod
    def from_dict(cls, d: dict) -> "TopologySignatures":
        return cls(d["faces"], d["edges"], d["vertices"], d["size"])


def verify_c3d10_midsides(nodes: np.ndarray, elements: np.ndarray, sample: int = 200) -> float:
    """Max relative distance of each mid-side node from the straight midpoint of
    its edge (CalculiX order). Curved boundary edges deviate slightly; a wrong
    ordering gives values of order 1."""
    el = elements[: min(sample, len(elements))]
    c = nodes[el[:, :4]]
    mids = nodes[el[:, 4:10]]
    expect = 0.5 * (c[:, TET_EDGES[:, 0]] + c[:, TET_EDGES[:, 1]])
    elen = np.linalg.norm(c[:, TET_EDGES[:, 1]] - c[:, TET_EDGES[:, 0]], axis=2)
    return float(np.max(np.linalg.norm(mids - expect, axis=2) / elen))


def _match(cad: list[np.ndarray], gm: list[np.ndarray], scale: np.ndarray, tol: float) -> dict[int, int]:
    """One-to-one matching of feature vectors. Returns {cad_index: gmsh_index}."""
    out: dict[int, int] = {}
    if not cad or not gm:
        return out
    A = np.array(cad) / scale
    B = np.array(gm) / scale
    D = np.linalg.norm(A[:, None, :] - B[None, :, :], axis=2)
    for i in range(len(cad)):
        hits = np.where(D[i] <= tol)[0]
        if len(hits) == 1:
            j = int(hits[0])
            # reverse uniqueness
            if np.sum(D[:, j] <= tol) == 1:
                out[i] = j
    return out


def generate_mesh(brep_path: Path, topo: TopologySignatures, settings: MeshSettings,
                  progress: Optional[Callable[[str], None]] = None, body_id: str = "") -> FEMesh:
    import gmsh

    say = progress or (lambda s: None)
    if gmsh.isInitialized():
        gmsh.finalize()
    gmsh.initialize(interruptible=False)
    log_lines: list[str] = []
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.logger.start()
        gmsh.model.add("opencae")
        say("Importing geometry")
        gmsh.model.occ.importShapes(str(brep_path), highestDimOnly=False)
        gmsh.model.occ.synchronize()
        vols = gmsh.model.getEntities(3)
        if not vols:
            raise MeshingError("Geometry contains no volume; cannot create a solid mesh.")
        if len(vols) > 1:
            say(f"Fragmenting {len(vols)} solids into a conformal (bonded) mesh")
            gmsh.model.occ.fragment(vols[:1], vols[1:])
            gmsh.model.occ.synchronize()
            vols = gmsh.model.getEntities(3)

        L = max(topo.size, 1e-12)
        # ---------------- entity mapping by signatures
        say("Mapping CAD topology to mesh entities")
        g_surf = [t for _, t in gmsh.model.getEntities(2)]
        g_curv = [t for _, t in gmsh.model.getEntities(1)]
        g_pts = [t for _, t in gmsh.model.getEntities(0)]

        def fvec(area, c):
            return np.array([area / L ** 2, *(np.asarray(c) / L)])

        cad_f = [fvec(f["area"], f["centroid"]) for f in topo.faces]
        gm_f = [fvec(gmsh.model.occ.getMass(2, s), gmsh.model.occ.getCenterOfMass(2, s)) for s in g_surf]
        fmap = _match(cad_f, gm_f, np.ones(4), tol=1e-5)
        cad_e = [np.array([e["length"] / L, *(np.asarray(e["centroid"]) / L)]) for e in topo.edges]
        gm_e = [np.array([gmsh.model.occ.getMass(1, c) / L, *(np.asarray(gmsh.model.occ.getCenterOfMass(1, c)) / L)])
                for c in g_curv]
        emap = _match(cad_e, gm_e, np.ones(4), tol=1e-5)
        cad_v = [np.asarray(v["point"]) / L for v in topo.vertices]
        gm_v = [np.asarray(gmsh.model.getValue(0, p, [])) / L for p in g_pts]
        vmap = _match(cad_v, gm_v, np.ones(3), tol=1e-7)
        unmapped = [i for i in range(len(topo.faces)) if i not in fmap]
        if unmapped:
            log_lines.append(f"Warning: {len(unmapped)} CAD face(s) not uniquely mapped: {[u + 1 for u in unmapped]}")

        # ---------------- sizing
        hmax = settings.size_max if settings.size_max > 0 else L / 20.0
        hmin = settings.size_min if settings.size_min > 0 else 0.0
        gmsh.option.setNumber("Mesh.MeshSizeMax", hmax)
        gmsh.option.setNumber("Mesh.MeshSizeMin", hmin)
        gmsh.option.setNumber("Mesh.MeshSizeFromCurvature", int(settings.curvature_points))
        gmsh.option.setNumber("Mesh.MeshSizeExtendFromBoundary", 1)
        gmsh.option.setNumber("Mesh.Algorithm", 6)  # Frontal-Delaunay 2D
        gmsh.option.setNumber("Mesh.Algorithm3D", 1)  # Delaunay 3D
        gmsh.option.setNumber("Mesh.Optimize", 1 if settings.optimize else 0)
        gmsh.option.setNumber("Mesh.OptimizeNetgen", 0)
        gmsh.option.setNumber("General.NumThreads", 1)
        fields = []
        for ref in settings.refinements:
            surfs = [g_surf[fmap[r.index]] for r in ref.targets if r.kind == "face" and r.index in fmap]
            curves = [g_curv[emap[r.index]] for r in ref.targets if r.kind == "edge" and r.index in emap]
            if not surfs and not curves:
                log_lines.append(f"Warning: refinement '{ref.name}' has no mapped targets; ignored.")
                continue
            fd = gmsh.model.mesh.field.add("Distance")
            if surfs:
                gmsh.model.mesh.field.setNumbers(fd, "SurfacesList", surfs)
            if curves:
                gmsh.model.mesh.field.setNumbers(fd, "CurvesList", curves)
            gmsh.model.mesh.field.setNumber(fd, "Sampling", 50)
            ft = gmsh.model.mesh.field.add("Threshold")
            gmsh.model.mesh.field.setNumber(ft, "InField", fd)
            gmsh.model.mesh.field.setNumber(ft, "SizeMin", ref.size)
            gmsh.model.mesh.field.setNumber(ft, "SizeMax", hmax)
            gmsh.model.mesh.field.setNumber(ft, "DistMin", ref.size)
            gmsh.model.mesh.field.setNumber(ft, "DistMax", max(4 * ref.size, 0.5 * hmax + ref.size))
            fields.append(ft)
        if fields:
            fm = gmsh.model.mesh.field.add("Min")
            gmsh.model.mesh.field.setNumbers(fm, "FieldsList", fields)
            gmsh.model.mesh.field.setAsBackgroundMesh(fm)

        # ---------------- generate
        for dim, label in ((1, "curves"), (2, "surfaces"), (3, "volume")):
            say(f"Meshing {label}")
            gmsh.model.mesh.generate(dim)
        if settings.element_order == 2:
            say("Elevating to second order (curved, geometry-conforming)")
            gmsh.option.setNumber("Mesh.SecondOrderLinear", 0)
            gmsh.model.mesh.setOrder(2)
            gmsh.option.setNumber("Mesh.HighOrderOptimize", 2)
            gmsh.model.mesh.optimize("HighOrder")
        say("Extracting mesh")

        etype_g = 11 if settings.element_order == 2 else 4
        nper = 10 if settings.element_order == 2 else 4
        tags_all, conn_all = [], []
        for _, vt in vols:
            types, etags, enodes = gmsh.model.mesh.getElements(3, vt)
            for t, et, en in zip(types, etags, enodes):
                if t != etype_g:
                    raise MeshingError(f"Unexpected 3-D element type {t} in volume {vt}.")
                tags_all.append(et)
                conn_all.append(en.reshape(-1, nper))
        if not tags_all:
            raise MeshingError("Gmsh produced no volume elements.")
        etags = np.concatenate(tags_all)
        conn = np.concatenate(conn_all).astype(np.int64)

        ntags, ncoord, _ = gmsh.model.mesh.getNodes()
        coord_of = dict(zip(ntags.tolist(), range(len(ntags))))
        ncoord = ncoord.reshape(-1, 3)
        used = np.unique(conn)
        new_index = {int(t): i for i, t in enumerate(used)}
        nodes = ncoord[[coord_of[int(t)] for t in used]]
        remap = np.vectorize(new_index.__getitem__, otypes=[np.int64])
        elements = remap(conn)
        if nper == 10:
            elements = elements[:, GMSH_TO_CCX_TET10]
            dev = verify_c3d10_midsides(nodes, elements)
            if dev > 0.25:
                raise MeshingError(f"C3D10 node ordering check failed (deviation {dev:.3f}).")
        # orientation: CalculiX needs positive corner volume
        m = tet_metrics(nodes, elements)
        neg = m["volume"] < 0
        if np.any(neg):
            log_lines.append(f"Reoriented {int(neg.sum())} negatively oriented elements.")
            elements[neg] = elements[neg][:, [0, 2, 1, 3] + ([6, 5, 4, 7, 9, 8] if nper == 10 else [])]
            m = tet_metrics(nodes, elements)

        # ---------------- boundary groups
        tri_type = 9 if nper == 10 else 2
        face_tris: dict[int, np.ndarray] = {}
        for ci, gi in fmap.items():
            types, _, enodes = gmsh.model.mesh.getElements(2, g_surf[gi])
            k = 6 if tri_type == 9 else 3
            tris = [en.reshape(-1, k) for t, en in zip(types, enodes) if t == tri_type]
            if tris:
                face_tris[ci] = remap(np.concatenate(tris).astype(np.int64))
        edge_nodes = {}
        for ci, gi in emap.items():
            nt, _, _ = gmsh.model.mesh.getNodes(1, g_curv[gi], includeBoundary=True)
            edge_nodes[ci] = np.array([new_index[int(t)] for t in nt if int(t) in new_index], dtype=np.int64)
        vertex_nodes = {}
        for ci, gi in vmap.items():
            nt, _, _ = gmsh.model.mesh.getNodes(0, g_pts[gi])
            if len(nt) and int(nt[0]) in new_index:
                vertex_nodes[ci] = new_index[int(nt[0])]

        # ---------------- quality
        say("Computing element quality")
        quality = dict(m)
        quality["gamma"] = np.asarray(gmsh.model.mesh.getElementQualities(etags, "gamma"))
        quality["sicn"] = np.asarray(gmsh.model.mesh.getElementQualities(etags, "minSICN"))
        quality["sj"] = np.asarray(gmsh.model.mesh.getElementQualities(etags, "minSJ"))
        log_lines.extend(str(x) for x in gmsh.logger.get())
        mesh = FEMesh(nodes=nodes, elements=elements, element_type="C3D10" if nper == 10 else "C3D4",
                      body_id=body_id, face_tris=face_tris, vertex_nodes=vertex_nodes, edge_nodes=edge_nodes,
                      quality=quality, unmapped_faces=unmapped, log=log_lines)
        say(f"Done: {mesh.n_nodes} nodes, {mesh.n_elements} elements")
        return mesh
    finally:
        try:
            gmsh.logger.stop()
        except Exception:
            pass
        gmsh.finalize()
