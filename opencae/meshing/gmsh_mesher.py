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


# Gmsh node order: corner pairs of the mid-side nodes (gmsh.info/doc/texinfo, "Node ordering")
_GMSH_MIDSIDE_EDGES = {
    11: ((0, 1), (1, 2), (2, 0), (3, 0), (3, 2), (3, 1)),  # 10-node tetrahedron
    17: ((0, 1), (0, 3), (0, 4), (1, 2), (1, 5), (2, 3), (2, 6), (3, 7), (4, 5), (4, 7), (5, 6), (6, 7)),  # hex20
}


def untangle_quadratic(vols, et: SolidType, max_pass: int = 6) -> str:
    """Make every curved quadratic element valid (scaled Jacobian > 0).

    Elements that are inverted after curving get straight edges (mid-side node moved to the edge
    midpoint); straightening can affect neighbours, so this repeats. If elements remain invalid,
    all mid-side nodes are straightened (straight-sided quadratic elements over the valid linear
    mesh). Returns a log message, or "" when nothing was changed."""
    import gmsh
    edges = _GMSH_MIDSIDE_EDGES.get(et.gmsh_type)
    if edges is None:
        return ""
    tags = [t for _, v in vols for t in gmsh.model.mesh.getElements(3, v)[1][0]]
    tags = np.asarray(tags, dtype=np.int64)

    def bad():
        if len(tags) == 0:
            return tags
        q = np.asarray(gmsh.model.mesh.getElementQualities(tags, "minSJ"))
        return tags[q <= 0.0]

    b = bad()
    if len(b) == 0:
        return ""
    n_first, moved = len(b), set()
    nc = et.n_corners
    for _ in range(max_pass):
        if len(b) == 0:
            break
        for tag in b:
            _, nodes, _, _ = gmsh.model.mesh.getElement(int(tag))
            corner = [np.asarray(gmsh.model.mesh.getNode(int(n))[0]) for n in nodes[:nc]]
            for k, (i, j) in enumerate(edges):
                mid = int(nodes[nc + k])
                if mid in moved:
                    continue
                gmsh.model.mesh.setNode(mid, list(0.5 * (corner[i] + corner[j])), [])
                moved.add(mid)
        b = bad()
    if len(b) == 0:
        return (f"{n_first} curved element(s) were inverted after curving (thin or sharp features, e.g. a trailing "
                f"edge); {len(moved)} mid-side node(s) were moved onto straight edges. All elements are valid.")
    gmsh.model.mesh.setOrder(1)
    gmsh.option.setNumber("Mesh.SecondOrderLinear", 1)
    gmsh.model.mesh.setOrder(2)
    b = bad()
    return (f"{n_first} curved element(s) were inverted after curving and could not be repaired locally; all "
            "elements use straight edges (quadratic interpolation, geometry follows the linear mesh)." +
            (f" {len(b)} element(s) are still invalid: refine the mesh." if len(b) else ""))


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


FACE_KEY_STRIDE = 1_000_000  # mesh entity key = body slot * stride + CAD index (slot 0 == plain index)


@dataclass
class MeshPart:
    body_id: str
    brep: Path
    topo: TopologySignatures


def _part_entities(gmsh, vols: list[tuple[int, int]]) -> tuple[list[int], list[int], list[int]]:
    surf = sorted({t for _, t in gmsh.model.getBoundary(vols, combined=False, oriented=False)})
    curv = sorted({abs(t) for _, t in gmsh.model.getBoundary([(2, x) for x in surf], combined=False, oriented=False)})
    pts = sorted({abs(t) for _, t in gmsh.model.getBoundary([(1, x) for x in curv], combined=False, oriented=False)})
    return surf, curv, pts


def _signature_map(gmsh, topo: TopologySignatures, surf, curv, pts):
    """One-to-one CAD -> gmsh entity matching by signatures (within one part)."""
    L = max(topo.size, 1e-12)
    cad_f = [np.array([f["area"] / L ** 2, *(np.asarray(f["centroid"]) / L)]) for f in topo.faces]
    gm_f = [np.array([gmsh.model.occ.getMass(2, x) / L ** 2, *(np.asarray(gmsh.model.occ.getCenterOfMass(2, x)) / L)])
            for x in surf]
    fmap = {k: surf[v] for k, v in _match(cad_f, gm_f, np.ones(4), tol=1e-5).items()}
    cad_e = [np.array([e["length"] / L, *(np.asarray(e["centroid"]) / L)]) for e in topo.edges]
    gm_e = [np.array([gmsh.model.occ.getMass(1, c) / L, *(np.asarray(gmsh.model.occ.getCenterOfMass(1, c)) / L)])
            for c in curv]
    emap = {k: curv[v] for k, v in _match(cad_e, gm_e, np.ones(4), tol=1e-5).items()}
    cad_v = [np.asarray(v["point"]) / L for v in topo.vertices]
    gm_v = [np.asarray(gmsh.model.getValue(0, x, [])) / L for x in pts]
    vmap = {k: pts[v] for k, v in _match(cad_v, gm_v, np.ones(3), tol=1e-7).items()}
    return fmap, emap, vmap


def _sample_points(gmsh, dim: int, tag: int, n: int = 7) -> list[list[float]]:
    """Points lying on a (trimmed) entity of the current model."""
    try:
        lo, hi = gmsh.model.getParametrizationBounds(dim, tag)
    except Exception:
        return [list(gmsh.model.occ.getCenterOfMass(dim, tag))]
    out = []
    if dim == 1:
        for t in np.linspace(lo[0], hi[0], n + 2)[1:-1]:
            out.append(list(gmsh.model.getValue(1, tag, [t])))
    else:
        for u in np.linspace(lo[0], hi[0], n + 2)[1:-1]:
            for v in np.linspace(lo[1], hi[1], n + 2)[1:-1]:
                xyz = list(gmsh.model.getValue(2, tag, [u, v]))
                if gmsh.model.isInside(2, tag, xyz):
                    out.append(xyz)
    return out[:25] or [list(gmsh.model.occ.getCenterOfMass(dim, tag))]


def _on_entity(gmsh, dim: int, tag: int, xyz, tol: float) -> bool:
    """True if the point lies ON the trimmed entity. gmsh.isInside alone only tests the
    parametric domain of the projection, so the projection distance is checked as well."""
    if not gmsh.model.isInside(dim, tag, xyz):
        return False
    cp = gmsh.model.getClosestPoint(dim, tag, xyz)[0]
    return float(np.linalg.norm(np.asarray(cp) - np.asarray(xyz, float))) <= tol


def _build_work_model(gmsh, parts: list[MeshPart]):
    """Import all parts into the current model and fragment them (conformal, bonded)."""
    gmsh.model.add("work")
    part_vols = []
    for p in parts:
        dt = gmsh.model.occ.importShapes(str(p.brep), highestDimOnly=False)
        part_vols.append([x for x in dt if x[0] == 3])
    gmsh.model.occ.synchronize()
    for p, pv in zip(parts, part_vols):
        if not pv:
            raise MeshingError(f"Body '{p.body_id}' contains no solid volume.")
    all_in = [v for pv in part_vols for v in pv]
    if len(all_in) == 1:
        return {all_in[0][1]: 0}
    _, omap = gmsh.model.occ.fragment(all_in[:1], all_in[1:])
    gmsh.model.occ.synchronize()
    owners: dict[int, set] = {}
    k = 0
    for slot, pv in enumerate(part_vols):
        for _ in pv:
            for d, t in omap[k]:
                if d == 3:
                    owners.setdefault(t, set()).add(slot)
            k += 1
    clash = {t: o for t, o in owners.items() if len(o) > 1}
    if clash:
        names = sorted({parts[i].body_id for o in clash.values() for i in o})
        raise MeshingError("Bodies overlap (interfering volumes) and cannot be bonded: " + ", ".join(names)
                           + ". Remove the overlap (e.g. boolean subtract) or analyse them separately.")
    return {t: next(iter(o)) for t, o in owners.items()}


def _restart(gmsh):
    gmsh.finalize()
    gmsh.initialize(interruptible=False)
    gmsh.option.setNumber("General.Terminal", 0)


def _import_and_map(gmsh, parts: list[MeshPart], say, log_lines):
    """Build the (fragmented) work model and map every CAD face/edge/vertex of every part
    to mesh entities. Returns (vols, vol_slot, fmap, emap, vmap, unmapped, L) with
    fmap/emap: key -> list of gmsh tags, vmap: key -> gmsh point tag.

    Several bodies: Gmsh sessions are used one after another (keeping two models alive
    in one session corrupts meshing): (1) fragment + record points sampled on every
    resulting surface/curve, (2) test those points against each part's own, un-fragmented
    CAD faces/edges, (3) rebuild the identical fragmented model for meshing."""
    L = max(p.topo.size for p in parts)
    say("Importing geometry")
    vol_slot = _build_work_model(gmsh, parts)
    n_vols = len(vol_slot)
    single = len(parts) == 1 and n_vols == 1
    if n_vols > 1:
        say(f"Fragmenting {n_vols} solids into a conformal (bonded) mesh")
    say("Mapping CAD topology to mesh entities")
    fmap: dict[int, list[int]] = {}
    emap: dict[int, list[int]] = {}
    vmap: dict[int, int] = {}
    unmapped: list[int] = []
    if single:
        vols = [(3, t) for t in sorted(vol_slot)]
        surf, curv, pts = _part_entities(gmsh, vols)
        f1, e1, v1 = _signature_map(gmsh, parts[0].topo, surf, curv, pts)
        fmap = {k: [v] for k, v in f1.items()}
        emap = {k: [v] for k, v in e1.items()}
        vmap = dict(v1)
        unmapped = [i for i in range(len(parts[0].topo.faces)) if i not in fmap]
        return vols, vol_slot, fmap, emap, vmap, unmapped, L
    # (1) samples on the fragmented work entities
    work_surf = [t for _, t in gmsh.model.getEntities(2)]
    work_curv = [t for _, t in gmsh.model.getEntities(1)]
    work_pts = {t: np.asarray(gmsh.model.getValue(0, t, [])) for _, t in gmsh.model.getEntities(0)}
    samples_s = {t: _sample_points(gmsh, 2, t) for t in work_surf}
    samples_c = {t: _sample_points(gmsh, 1, t) for t in work_curv}
    area_s = {t: gmsh.model.occ.getMass(2, t) for t in work_surf}
    counts = tuple(len(gmsh.model.getEntities(d)) for d in range(4))
    # (2) each part on its own
    for slot, p in enumerate(parts):
        _restart(gmsh)
        gmsh.model.add("reference")
        dt = gmsh.model.occ.importShapes(str(p.brep), highestDimOnly=False)
        gmsh.model.occ.synchronize()
        pv = [x for x in dt if x[0] == 3]
        surf, curv, pts = _part_entities(gmsh, pv)
        f1, e1, v1 = _signature_map(gmsh, p.topo, surf, curv, pts)
        base = slot * FACE_KEY_STRIDE
        for ci, rs in f1.items():
            hits = [w for w in work_surf if all(_on_entity(gmsh, 2, rs, xyz, 1e-7 * L) for xyz in samples_s[w])]
            a_ref = gmsh.model.occ.getMass(2, rs)
            if hits and abs(sum(area_s[w] for w in hits) - a_ref) <= 1e-5 * max(a_ref, L * L * 1e-9):
                fmap[base + ci] = hits
        for ci, rc in e1.items():
            hits = [w for w in work_curv if all(_on_entity(gmsh, 1, rc, xyz, 1e-7 * L) for xyz in samples_c[w])]
            if hits:
                emap[base + ci] = hits
        for ci, rp in v1.items():
            xyz = np.asarray(gmsh.model.getValue(0, rp, []))
            hit = [t for t, q in work_pts.items() if np.linalg.norm(q - xyz) <= 1e-7 * L]
            if len(hit) == 1:
                vmap[base + ci] = hit[0]
        unmapped += [base + i for i in range(len(p.topo.faces)) if base + i not in fmap]
    # (3) identical work model for meshing
    _restart(gmsh)
    vol_slot2 = _build_work_model(gmsh, parts)
    if vol_slot2 != vol_slot or tuple(len(gmsh.model.getEntities(d)) for d in range(4)) != counts:
        raise MeshingError("Internal error: the fragmented geometry was not reproducible.")
    if unmapped:
        log_lines.append(f"Warning: {len(unmapped)} CAD face(s) could not be mapped to the mesh: {unmapped[:20]}")
    vols = [(3, t) for t in sorted(vol_slot)]
    return vols, vol_slot, fmap, emap, vmap, unmapped, L


def _mesh_parts_separately(parts: list[MeshPart], settings, progress=None) -> FEMesh:
    """Each body meshed on its own (non-matching meshes at the interfaces), merged into one
    FEMesh with node offsets. Bodies are then connected only by interactions (tie or
    contact), so touching faces stay boundary faces."""
    import copy
    say = progress or (lambda s: None)
    meshes = []
    vol_of = []
    for k, part in enumerate(parts):
        st = copy.deepcopy(settings)
        st.assembly_mesh = "conformal"
        st.refinements = [r for r in settings.refinements if all(t.body == part.body_id for t in r.targets)]
        if settings.sizing_mode == "target_count" and settings.target_elements > 0:
            vol_of.append(None)
        say(f"Body {k + 1}/{len(parts)}")
        meshes.append(generate_mesh(None, settings=st, progress=progress, parts=[part]))
    if settings.sizing_mode == "target_count" and settings.target_elements > 0:
        pass  # each body met the target individually; documented limitation
    S = FEMesh.KEY_STRIDE
    nodes, elems, eb = [], [], []
    face_tris, edge_nodes, vertex_nodes, unmapped, log = {}, {}, {}, [], []
    quality: dict[str, list] = {}
    off = 0
    for k, m in enumerate(meshes):
        nodes.append(m.nodes)
        elems.append(m.elements + off)
        eb.append(np.full(len(m.elements), k, dtype=np.int64))
        for key, f in m.face_tris.items():
            face_tris[k * S + key] = f + off
        for key, n in m.edge_nodes.items():
            edge_nodes[k * S + key] = n + off
        for key, n in m.vertex_nodes.items():
            vertex_nodes[k * S + key] = int(n) + off
        unmapped += [k * S + u for u in m.unmapped_faces]
        for qk, qv in m.quality.items():
            quality.setdefault(qk, []).append(np.atleast_1d(qv))
        log += [f"[{parts[k].body_id}] {ln}" for ln in m.log]
        off += len(m.nodes)
    q = {k: np.concatenate(v) for k, v in quality.items() if k != "target_elements"}
    if "target_elements" in quality:
        q["target_elements"] = quality["target_elements"][0]
    log.append(f"Assembly meshed body by body ({len(parts)} bodies, non-matching interfaces): connect them with "
               "tie (bonded) or contact interactions.")
    return FEMesh(nodes=np.vstack(nodes), elements=np.vstack(elems), element_type=meshes[0].element_type,
                  body_id=parts[0].body_id, face_tris=face_tris, vertex_nodes=vertex_nodes, edge_nodes=edge_nodes,
                  quality=q, unmapped_faces=unmapped, log=log, body_ids=[p.body_id for p in parts],
                  element_body=np.concatenate(eb))


def generate_mesh(brep_path, topo: Optional[TopologySignatures] = None, settings: Optional[MeshSettings] = None,
                  progress: Optional[Callable[[str], None]] = None, body_id: str = "",
                  parts: Optional[list[MeshPart]] = None) -> FEMesh:
    """Mesh one body (brep_path + topo) or several bodies (``parts``) as one bonded mesh."""
    import gmsh

    if parts is None:
        parts = [MeshPart(body_id, Path(brep_path), topo)]
    settings = settings or MeshSettings()
    if len(parts) > 1 and getattr(settings, "assembly_mesh", "conformal") == "separate":
        return _mesh_parts_separately(parts, settings, progress)
    slots = {p.body_id: i for i, p in enumerate(parts)}
    say = progress or (lambda s: None)
    if gmsh.isInitialized():
        gmsh.finalize()
    gmsh.initialize(interruptible=False)
    log_lines: list[str] = []
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        vols, vol_slot, fmap, emap, vmap, unmapped, L = _import_and_map(gmsh, parts, say, log_lines)
        gmsh.logger.start()

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
                def rkey(r):
                    return slots.get(r.body, -1) * FACE_KEY_STRIDE + r.index if r.body in slots else -1
                surfs = [t for r in ref.targets if r.kind == "face" for t in fmap.get(rkey(r), [])]
                curves = [t for r in ref.targets if r.kind == "edge" for t in emap.get(rkey(r), [])]
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
            # Gmsh's high-order optimiser can abort the whole process (uncatchable C++ exception
            # "Failed to reach critical value") on thin features such as airfoil trailing edges, so it is
            # not used. Elements that curving inverts are straightened instead (always valid).
            msg = untangle_quadratic(vols, et)
            if msg:
                log_lines.append(msg)
                say(msg)
        say("Extracting mesh")

        etype_g = et.gmsh_type
        nper = et.n_nodes
        tags_all, conn_all, slot_all = [], [], []
        for _, vt in vols:
            types, etags, enodes = gmsh.model.mesh.getElements(3, vt)
            for t, etg, en in zip(types, etags, enodes):
                if t != etype_g:
                    raise MeshingError(f"Unexpected 3-D element type {t} in volume {vt}.")
                tags_all.append(etg)
                conn_all.append(en.reshape(-1, nper))
                slot_all.append(np.full(len(etg), vol_slot[vt], dtype=np.int64))
        if not tags_all:
            raise MeshingError("Gmsh produced no volume elements.")
        etags = np.concatenate(tags_all)
        conn = np.concatenate(conn_all).astype(np.int64)
        element_body = np.concatenate(slot_all)

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
        for key, tags in fmap.items():
            chunks = []
            for tag in tags:
                types, _, enodes = gmsh.model.mesh.getElements(2, tag)
                chunks += [en.reshape(-1, et.face_nodes) for t, en in zip(types, enodes) if t == et.surface_gmsh_type]
            if chunks:
                face_tris[key] = remap(np.concatenate(chunks).astype(np.int64))
        edge_nodes = {}
        for key, tags in emap.items():
            ids = set()
            for tag in tags:
                nt, _, _ = gmsh.model.mesh.getNodes(1, tag, includeBoundary=True)
                ids.update(int(t) for t in nt if int(t) in new_index)
            edge_nodes[key] = np.array(sorted(new_index[t] for t in ids), dtype=np.int64)
        vertex_nodes = {}
        for key, tag in vmap.items():
            nt, _, _ = gmsh.model.mesh.getNodes(0, tag)
            if len(nt) and int(nt[0]) in new_index:
                vertex_nodes[key] = new_index[int(nt[0])]

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
                      body_id=parts[0].body_id, body_ids=[p.body_id for p in parts], element_body=element_body,
                      face_tris=face_tris, vertex_nodes=vertex_nodes, edge_nodes=edge_nodes,
                      quality=quality, unmapped_faces=unmapped, log=log_lines)
        say(f"Done: {mesh.n_nodes} nodes, {mesh.n_elements} elements")
        return mesh
    finally:
        try:
            gmsh.logger.stop()
        except Exception:
            pass
        gmsh.finalize()
