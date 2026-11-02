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


def _sharp_corners(gmsh, curves, max_angle_deg: float = 60.0) -> list[int]:
    """Points where two of ``curves`` meet in a wedge sharper than ``max_angle_deg`` (angle between
    the curve tangents leaving the point; a smooth junction is 180 degrees)."""
    ends: dict[int, list[np.ndarray]] = {}
    for c in curves:
        lo, hi = gmsh.model.getParametrizationBounds(1, c)
        for t0, sgn in ((lo[0], 1.0), (hi[0], -1.0)):
            x = np.asarray(gmsh.model.getValue(1, c, [t0]))
            d = sgn * np.asarray(gmsh.model.getDerivative(1, c, [t0]))
            for _, p in gmsh.model.getBoundary([(1, c)], oriented=False):
                if np.linalg.norm(np.asarray(gmsh.model.getValue(0, p, [])) - x) <= 1e-6 * (1 + np.linalg.norm(x)):
                    n = np.linalg.norm(d)
                    if n > 0:
                        ends.setdefault(p, []).append(d / n)
    out = []
    for p, ds in ends.items():
        if len(ds) == 2:
            ang = np.degrees(np.arccos(np.clip(float(ds[0] @ ds[1]), -1.0, 1.0)))
            if ang < max_angle_deg:
                out.append(int(p))
    return out


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
        # CAD edge <-> gmsh curve (geometry only, before meshing)
        tol = 1e-5 * size
        curve_edge = {}
        for _, c in curves:
            L = gmsh.model.occ.getMass(1, c)
            cm = np.array(gmsh.model.occ.getCenterOfMass(1, c))
            for j, e in enumerate(topo.edges):
                if abs(e["length"] - L) <= 1e-4 * max(L, 1e-12) + tol and \
                        np.linalg.norm(np.asarray(e["centroid"]) - cm) <= 1e-4 * size + tol:
                    curve_edge[c] = j
                    break
        # local refinement on edges of the face (e.g. an airfoil contour in a large domain)
        ref_fields = []
        for ref in getattr(settings, "refinements", []) or []:
            idx = {r.index for r in ref.targets if r.kind == "edge"}
            rc = [c for c, j in curve_edge.items() if j in idx]
            if not rc or ref.size <= 0:
                continue
            fd = gmsh.model.mesh.field.add("Distance")
            gmsh.model.mesh.field.setNumbers(fd, "CurvesList", rc)
            gmsh.model.mesh.field.setNumber(fd, "Sampling", 200)
            ft = gmsh.model.mesh.field.add("Threshold")
            gmsh.model.mesh.field.setNumber(ft, "InField", fd)
            gmsh.model.mesh.field.setNumber(ft, "SizeMin", ref.size)
            gmsh.model.mesh.field.setNumber(ft, "SizeMax", h)
            gmsh.model.mesh.field.setNumber(ft, "DistMin", 2 * ref.size)
            g = float(getattr(settings, "growth_rate", 0.0) or 0.0)
            gmsh.model.mesh.field.setNumber(ft, "DistMax", (ref.size + (h - ref.size) / (g - 1.0)) if g > 1.0
                                            else max(20 * ref.size, 0.5 * h))
            ref_fields.append(ft)
            say(f"Local refinement '{ref.name}': size {ref.size:g} on {len(rc)} edge(s)")
        if ref_fields:
            fmin = gmsh.model.mesh.field.add("Min")
            gmsh.model.mesh.field.setNumbers(fmin, "FieldsList", ref_fields)
            gmsh.model.mesh.field.setAsBackgroundMesh(fmin)
            gmsh.option.setNumber("Mesh.MeshSizeExtendFromBoundary", 0)
            gmsh.option.setNumber("Mesh.MeshSizeFromPoints", 0)
        nbl = int(getattr(settings, "bl_layers", 0) or 0)
        bl_curves =[c for c, j in curve_edge.items() if j in set(getattr(settings, "bl_edges", []) or [])]
        if nbl > 0 and bl_curves and quads and len(curves) == 4:
            say("Boundary layers skipped: the structured quadrilateral mesh already resolves the walls; grade it "
                "with the element size instead")
            nbl = 0
        if nbl > 0 and bl_curves:
            g = float(getattr(settings, "bl_growth", 1.2) or 1.2)
            first = float(getattr(settings, "bl_first", 0.0) or 0.0) or h / 20
            thick = first * (g ** nbl - 1) / (g - 1) if abs(g - 1) > 1e-9 else first * nbl
            fbl = gmsh.model.mesh.field.add("BoundaryLayer")
            gmsh.model.mesh.field.setNumbers(fbl, "CurvesList", bl_curves)
            gmsh.model.mesh.field.setNumber(fbl, "Size", first)
            gmsh.model.mesh.field.setNumber(fbl, "Ratio", g)
            gmsh.model.mesh.field.setNumber(fbl, "Thickness", thick)
            gmsh.model.mesh.field.setNumber(fbl, "Quads", 1)
            fans = _sharp_corners(gmsh, bl_curves)
            if fans:  # e.g. an airfoil trailing edge: layers fan around the point instead of failing
                gmsh.model.mesh.field.setNumbers(fbl, "FanPointsList", fans)
                say(f"Boundary layer fans at {len(fans)} sharp corner(s)")
            gmsh.model.mesh.field.setAsBoundaryLayer(fbl)
            say(f"Boundary layer: {nbl} layers, first {first:.3g}, growth {g:g} on {len(bl_curves)} wall curve(s)")
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
        parts = {}
        for et, en in zip(etypes, enodes):
            k = {2: 3, 3: 4}.get(int(et))
            if k is not None:
                parts[int(et)] = np.vectorize(index.get)(en.reshape(-1, k).astype(np.int64))
        if not parts:
            raise Mesh2DError("no cells generated")
        if len(parts) > 1 or want not in parts:
            if 2 in parts and 3 in parts and not quads:
                # boundary-layer quads in a triangle mesh: split them so all cells are triangles
                q = parts[3]
                parts = {2: np.vstack([parts[2], q[:, [0, 1, 2]], q[:, [0, 2, 3]]])}
            elif quads and 2 in parts:
                raise Mesh2DError("could not make an all-quadrilateral mesh of this face; use triangles")
        cells = parts[2] if not quads else parts[3]
        # counter-clockwise about the face normal
        X = nodes[cells]
        n = np.cross(X[:, 1] - X[:, 0], X[:, 2] - X[:, 0])
        flip = n @ nrm < 0
        cells[flip] = cells[flip][:, ::-1]
        # map CAD edges / vertices of the body to gmsh curves / points by geometry
        edge_nodes, vertex_nodes, unmapped = {}, {}, []
        for _, c in curves:
            best = curve_edge.get(c)
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
