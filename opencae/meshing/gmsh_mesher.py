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

from opencae.meshing.elements import SOLID_TYPES, SolidType, element_centroid_jacobian_sign
from opencae.meshing.mesh_data import TET_EDGES, FEMesh
from opencae.meshing.quality import element_metrics
from opencae.project.model import MeshSettings

GMSH_TO_CCX_TET10 = np.array(SOLID_TYPES["C3D10"].gmsh_to_ccx)
MAX_TARGET_ITERATIONS = 8
HEX_LIMITATION = ("All-hexahedral meshing is only possible here for volumes that are topologically hexahedral "
                  "(six four-sided faces: boxes, plates, rectangular beams, and boolean results that keep that "
                  "topology). Gmsh's transfinite method could not be applied to {n} volume(s) of this body. "
                  "Use tetrahedral elements (C3D10) for this geometry.")


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


def verify_midsides(nodes: np.ndarray, elements: np.ndarray, et: SolidType, sample: int = 200) -> float:
    """Max relative distance of each mid-side node from the straight midpoint of its
    edge (CalculiX order). Curved boundary edges deviate slightly; a wrong ordering
    gives values of order 1."""
    E = np.array(et.edges)
    el = elements[: min(sample, len(elements))]
    c = nodes[el[:, :et.n_corners]]
    mids = nodes[el[:, et.n_corners:]]
    expect = 0.5 * (c[:, E[:, 0]] + c[:, E[:, 1]])
    elen = np.linalg.norm(c[:, E[:, 1]] - c[:, E[:, 0]], axis=2)
    return float(np.max(np.linalg.norm(mids - expect, axis=2) / elen))


def verify_c3d10_midsides(nodes: np.ndarray, elements: np.ndarray, sample: int = 200) -> float:
    return verify_midsides(nodes, elements, SOLID_TYPES["C3D10"], sample)


def setup_hex_transfinite(gmsh, vols, h: float) -> int:
    """Structured hexahedral meshing constraints with a controllable size.

    Every volume must be topologically hexahedral (6 faces with 4 boundary curves).
    Curves are grouped into classes that must share the same number of divisions
    (opposite curves of each four-sided face, transitively, via union-find); each
    class gets ceil(longest curve / h) divisions. Returns the predicted number of
    hexahedra. Raises MeshingError when the topology is not hexahedral."""
    surfaces = set()
    bad = 0
    for _, v in vols:
        faces = [t for _, t in gmsh.model.getBoundary([(3, v)], oriented=False)]
        if len(faces) != 6:
            bad += 1
        surfaces.update(faces)
    curves_of = {}
    for sface in surfaces:
        cs = [abs(t) for _, t in gmsh.model.getBoundary([(2, sface)], oriented=False)]
        if len(cs) != 4:
            bad += 1
        curves_of[sface] = cs
    if bad:
        raise MeshingError(HEX_LIMITATION.format(n=len(vols)))
    parent = {}

    def find(a):
        parent.setdefault(a, a)
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def ends(c):
        return {t for _, t in gmsh.model.getBoundary([(1, c)], oriented=False)}

    for sface, cs in curves_of.items():
        e0 = ends(cs[0])
        opp = [c for c in cs[1:] if not (ends(c) & e0)]
        adj = [c for c in cs[1:] if c not in opp]
        if len(opp) != 1 or len(adj) != 2:
            raise MeshingError(HEX_LIMITATION.format(n=len(vols)))
        parent[find(cs[0])] = find(opp[0])
        parent[find(adj[0])] = find(adj[1])
    length = {c: gmsh.model.occ.getMass(1, c) for cs in curves_of.values() for c in cs}
    cls_len = {}
    for c, Lc in length.items():
        r = find(c)
        cls_len[r] = max(cls_len.get(r, 0.0), Lc)
    ndiv = {r: max(1, int(np.ceil(Lc / h - 1e-9))) for r, Lc in cls_len.items()}
    for c in length:
        gmsh.model.mesh.setTransfiniteCurve(c, ndiv[find(c)] + 1)
    for sface in surfaces:
        gmsh.model.mesh.setTransfiniteSurface(sface)
        gmsh.model.mesh.setRecombine(2, sface)
    predicted = 0
    for _, v in vols:
        gmsh.model.mesh.setTransfiniteVolume(v)
        faces = [t for _, t in gmsh.model.getBoundary([(3, v)], oriented=False)]
        cls = {find(c) for f in faces for c in curves_of[f]}
        if len(cls) != 3:
            raise MeshingError(HEX_LIMITATION.format(n=len(vols)))
        n = 1
        for r in cls:
            n *= ndiv[r]
        predicted += n
    return predicted


def initial_size_for_count(volume: float, n_target: int, shape: str) -> float:
    """Empirical first guess: Delaunay tets fill ~0.18 h^3 each, structured hexes ~h^3."""
    c = 0.178 if shape == "tet" else 1.0
    return float((volume / (c * max(n_target, 1))) ** (1.0 / 3.0))


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
        shape = settings.element_shape if settings.element_shape in ("tet", "hex") else "tet"
        order = 2 if settings.element_order == 2 else 1
        et = SOLID_TYPES[{("tet", 1): "C3D4", ("tet", 2): "C3D10", ("hex", 1): "C3D8", ("hex", 2): "C3D20"}[(shape, order)]]
        volume = float(sum(gmsh.model.occ.getMass(3, v) for _, v in vols))
        hmin = settings.size_min if settings.size_min > 0 else 0.0
        if settings.sizing_mode == "target_count" and settings.target_elements > 0:
            hmax = initial_size_for_count(volume, settings.target_elements, shape)
        else:
            hmax = settings.size_max if settings.size_max > 0 else L / 20.0
        gmsh.option.setNumber("Mesh.MeshSizeMin", hmin)
        gmsh.option.setNumber("Mesh.MeshSizeFromCurvature", int(settings.curvature_points) if shape == "tet" else 0)
        gmsh.option.setNumber("Mesh.MeshSizeExtendFromBoundary", 1)
        gmsh.option.setNumber("Mesh.Algorithm", int(settings.algorithm_2d) if shape == "tet" else 8)
        gmsh.option.setNumber("Mesh.Algorithm3D", int(settings.algorithm_3d))
        gmsh.option.setNumber("Mesh.Optimize", 1 if settings.optimize else 0)
        gmsh.option.setNumber("Mesh.OptimizeNetgen", 0)
        gmsh.option.setNumber("Mesh.Smoothing", max(0, int(settings.smoothing_steps)))
        gmsh.option.setNumber("General.NumThreads", 1)
        g = float(settings.growth_rate or 0.0)

        def build_fields(h):
            for tag in gmsh.model.mesh.field.list():
                gmsh.model.mesh.field.remove(tag)
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
                gmsh.model.mesh.field.setNumber(ft, "SizeMax", h)
                gmsh.model.mesh.field.setNumber(ft, "DistMin", ref.size)
                dist_max = (ref.size + max(h - ref.size, 0.0) / (g - 1.0)) if g > 1.0 \
                    else max(4 * ref.size, 0.5 * h + ref.size)
                gmsh.model.mesh.field.setNumber(ft, "DistMax", dist_max)
                fields.append(ft)
            if fields:
                fm = gmsh.model.mesh.field.add("Min")
                gmsh.model.mesh.field.setNumbers(fm, "FieldsList", fields)
                gmsh.model.mesh.field.setAsBackgroundMesh(fm)

        def mesh_linear(h, label=True):
            gmsh.model.mesh.clear()
            gmsh.option.setNumber("Mesh.MeshSizeMax", h)
            build_fields(h)
            if shape == "hex":
                setup_hex_transfinite(gmsh, vols, h)
            for dim, name in ((1, "curves"), (2, "surfaces"), (3, "volume")):
                if label:
                    say(f"Meshing {name}")
                gmsh.model.mesh.generate(dim)
            n = 0
            other = 0
            for _, vt in vols:
                types, etg, _ = gmsh.model.mesh.getElements(3, vt)
                for t, e in zip(types, etg):
                    if t == (5 if shape == "hex" else 4):
                        n += len(e)
                    else:
                        other += len(e)
            if shape == "hex" and other:
                bad = sum(1 for _, vt in vols if any(t != 5 for t in gmsh.model.mesh.getElements(3, vt)[0]))
                raise MeshingError(HEX_LIMITATION.format(n=bad))
            if n == 0:
                raise MeshingError("Gmsh produced no volume elements.")
            return n

        if settings.sizing_mode == "target_count" and settings.target_elements > 0:
            target = int(settings.target_elements)
            tol = max(0.01, float(settings.target_tolerance or 0.1))
            history = []
            h = hmax
            best = None
            for it in range(MAX_TARGET_ITERATIONS):
                n = mesh_linear(h, label=False)
                history.append((h, n))
                say(f"Target {target} elements: size {h:.4g} gave {n} elements (iteration {it + 1})")
                if best is None or abs(n - target) < abs(best[1] - target):
                    best = (h, n)
                if abs(n - target) <= tol * target:
                    break
                # N ~ h^-p; estimate p from the last two iterations when possible (else p = 3)
                p = 3.0
                if len(history) >= 2:
                    (h1, n1), (h2, n2) = history[-2], history[-1]
                    if h1 != h2 and n1 > 0 and n2 > 0 and n1 != n2:
                        p = float(np.clip(-np.log(n2 / n1) / np.log(h2 / h1), 1.0, 4.0))
                h = h * (n / target) ** (1.0 / p)
                if hmin > 0 and h < hmin:
                    h = hmin
            hmax = best[0]
            achieved = best[1]
            converged = abs(achieved - target) <= tol * target
            msg = (f"Target element count {target}: best size {hmax:.5g} -> {achieved} elements "
                   f"({100 * (achieved - target) / target:+.1f} %) after {len(history)} iteration(s)")
            if not converged:
                msg += (". The target could not be met within the tolerance (local refinements, curvature sizing, "
                        "minimum size or geometry features limit the count).")
            log_lines.append(msg)
            say(msg)
            n = mesh_linear(hmax) if history[-1][0] != hmax else achieved
        else:
            mesh_linear(hmax)
        if order == 2:
            say("Elevating to second order (curved, geometry-conforming)")
            gmsh.option.setNumber("Mesh.SecondOrderLinear", 0)
            gmsh.option.setNumber("Mesh.SecondOrderIncomplete", 1 if shape == "hex" else 0)
            gmsh.model.mesh.setOrder(2)
            gmsh.option.setNumber("Mesh.HighOrderOptimize", 2)
            gmsh.model.mesh.optimize("HighOrder")
        say("Extracting mesh")

        etype_g = et.gmsh_type
        nper = et.n_nodes
        tags_all, conn_all = [], []
        for _, vt in vols:
            types, etags, enodes = gmsh.model.mesh.getElements(3, vt)
            for t, etg, en in zip(types, etags, enodes):
                if t != etype_g:
                    raise MeshingError(f"Unexpected 3-D element type {t} in volume {vt}.")
                tags_all.append(etg)
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
        elements = remap(conn)[:, list(et.gmsh_to_ccx)]
        if et.order == 2:
            dev = verify_midsides(nodes, elements, et)
            if dev > 0.25:
                raise MeshingError(f"{et.name} node ordering check failed (deviation {dev:.3f}).")
        # orientation: CalculiX needs positive Jacobians
        sign = element_centroid_jacobian_sign(nodes, elements, et.name)
        neg = sign < 0
        if np.any(neg):
            log_lines.append(f"Reoriented {int(neg.sum())} negatively oriented elements.")
            if et.shape == "tet":
                perm = [0, 2, 1, 3] + ([6, 5, 4, 7, 9, 8] if et.order == 2 else [])
            else:  # mirror bottom/top quads: swap corners 1<->3, 5<->7 (and matching mid-sides)
                perm = [0, 3, 2, 1, 4, 7, 6, 5] + ([11, 10, 9, 8, 15, 14, 13, 12, 16, 19, 18, 17] if et.order == 2 else [])
            elements[neg] = elements[neg][:, perm]
        m = element_metrics(nodes, elements, et.name)

        # ---------------- boundary groups (surface elements per CAD face: tri3/tri6/quad4/quad8)
        face_tris: dict[int, np.ndarray] = {}
        for ci, gi in fmap.items():
            types, _, enodes = gmsh.model.mesh.getElements(2, g_surf[gi])
            k = et.face_nodes
            tris = [en.reshape(-1, k) for t, en in zip(types, enodes) if t == et.surface_gmsh_type]
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
        if et.shape == "tet":
            quality["gamma"] = np.asarray(gmsh.model.mesh.getElementQualities(etags, "gamma"))
        quality["sicn"] = np.asarray(gmsh.model.mesh.getElementQualities(etags, "minSICN"))
        quality["sj"] = np.asarray(gmsh.model.mesh.getElementQualities(etags, "minSJ"))
        log_lines.extend(str(x) for x in gmsh.logger.get())
        if settings.sizing_mode == "target_count" and settings.target_elements > 0:
            quality["target_elements"] = np.array([settings.target_elements])
        mesh = FEMesh(nodes=nodes, elements=elements, element_type=et.name,
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
