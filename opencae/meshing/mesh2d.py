"""Planar (2-D) meshing of one CAD face for 2-D flow studies.

The face must be planar. Cells are linear triangles (F2D3) or, with element shape
"hex", all-quadrilateral cells (F2D4, Gmsh recombination + subdivision; structured
when the face has four sides). Node coordinates stay in 3-D on the face; CAD edges and
vertices of the face are mapped to mesh nodes by geometric matching, so boundary
conditions are assigned to edges.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from opencae.meshing.mesh_data import FEMesh
from opencae.meshing.quality import element_metrics


class Mesh2DError(RuntimeError):
    pass


def generate_mesh_2d(face_brep: Path, body_id: str, face_index: int, topo, settings,
                     progress: Optional[Callable[[str], None]] = None) -> FEMesh:
    import gmsh
    say = progress or (lambda m: None)
    t0 = time.time()
    sig = topo.faces[face_index]
    if sig.get("type") != "plane" or sig.get("normal") is None:
        raise Mesh2DError("2-D flow needs a planar face (choose a flat face of the body).")
    nrm = np.asarray(sig["normal"], float)
    nrm /= np.linalg.norm(nrm)
    gmsh.initialize(interruptible=False)
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.model.add("face2d")
        say("Importing face")
        gmsh.model.occ.importShapes(str(face_brep))
        gmsh.model.occ.synchronize()
        surfs = gmsh.model.getEntities(2)
        if len(surfs) != 1:
            raise Mesh2DError(f"expected one surface, got {len(surfs)}")
        size = float(topo.size) or 1.0
        h = settings.size_max or size / 20
        gmsh.option.setNumber("Mesh.MeshSizeMax", h)
        if settings.size_min:
            gmsh.option.setNumber("Mesh.MeshSizeMin", settings.size_min)
        gmsh.option.setNumber("Mesh.Algorithm", int(settings.algorithm_2d or 6))
        quads = settings.element_shape == "hex"
        curves = gmsh.model.getEntities(1)
        if quads:
            if len(curves) == 4:  # structured quads
                for _, c in curves:
                    L = gmsh.model.occ.getMass(1, c)
                    gmsh.model.mesh.setTransfiniteCurve(c, max(2, int(round(L / h)) + 1))
                try:
                    gmsh.model.mesh.setTransfiniteSurface(surfs[0][1])
                except Exception:  # pragma: no cover
                    pass
            gmsh.model.mesh.setRecombine(2, surfs[0][1])
            gmsh.option.setNumber("Mesh.RecombineAll", 1)
            if len(curves) != 4:
                gmsh.option.setNumber("Mesh.SubdivisionAlgorithm", 1)  # all quadrilaterals
        say("Meshing surface")
        gmsh.model.mesh.generate(2)
        if settings.optimize and not quads:
            gmsh.model.mesh.optimize("Laplace2D")
        tags, xyz, _ = gmsh.model.mesh.getNodes()
        nodes = xyz.reshape(-1, 3)
        index = {int(t): i for i, t in enumerate(tags)}
        etypes, _, enodes = gmsh.model.mesh.getElements(2)
        want = 3 if quads else 2
        cells = None
        for et, en in zip(etypes, enodes):
            k = {2: 3, 3: 4}.get(int(et))
            if k is None:
                continue
            arr = np.vectorize(index.get)(en.reshape(-1, k).astype(np.int64))
            if int(et) != want:
                raise Mesh2DError("mixed triangle/quadrilateral cells; use triangles for this face")
            cells = arr
        if cells is None or not len(cells):
            raise Mesh2DError("no cells generated")
        # counter-clockwise about the face normal
        X = nodes[cells]
        n = np.cross(X[:, 1] - X[:, 0], X[:, 2] - X[:, 0])
        flip = n @ nrm < 0
        cells[flip] = cells[flip][:, ::-1]
        # map CAD edges / vertices of the body to gmsh curves / points by geometry
        edge_nodes, vertex_nodes, unmapped = {}, {}, []
        S = FEMesh.KEY_STRIDE
        tol = 1e-5 * size
        for _, c in curves:
            L = gmsh.model.occ.getMass(1, c)
            cm = np.array(gmsh.model.occ.getCenterOfMass(1, c))
            best = None
            for j, e in enumerate(topo.edges):
                if abs(e["length"] - L) <= 1e-4 * max(L, 1e-12) + tol and \
                        np.linalg.norm(np.asarray(e["centroid"]) - cm) <= 1e-4 * size + tol:
                    best = j
                    break
            if best is None:
                unmapped.append(c)
                continue
            ntags, _, _ = gmsh.model.mesh.getNodes(1, c, includeBoundary=True)
            edge_nodes[best] = np.array([index[int(t)] for t in ntags], dtype=np.int64)
        for j, v in enumerate(topo.vertices):
            d = np.linalg.norm(nodes - np.asarray(v["point"]), axis=1)
            k = int(np.argmin(d))
            if d[k] <= 1e-6 * size + tol:
                vertex_nodes[j] = k
        etype = "F2D4" if quads else "F2D3"
        q = element_metrics(nodes, cells, etype)
        mesh = FEMesh(nodes=nodes, elements=cells, element_type=etype, body_id=body_id,
                      face_tris={face_index: cells}, edge_nodes=edge_nodes, vertex_nodes=vertex_nodes,
                      quality=q, log=[f"2-D mesh of face {face_index + 1}: {len(nodes)} nodes, {len(cells)} "
                                      f"{'quadrilaterals' if quads else 'triangles'} in {time.time() - t0:.2f} s"],
                      body_ids=[body_id], element_body=np.zeros(len(cells), dtype=np.int64))
        if unmapped:
            mesh.log.append(f"{len(unmapped)} boundary curve(s) could not be matched to CAD edges")
        say("Done")
        return mesh
    finally:
        gmsh.finalize()
