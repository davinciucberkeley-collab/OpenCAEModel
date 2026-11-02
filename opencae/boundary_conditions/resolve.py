"""Map loads and constraints defined on CAD entities to mesh entities, and
validate the resulting finite element model.

Mapping uses only the validated CAD->mesh association stored in
:class:`~opencae.meshing.mesh_data.FEMesh` (boundary triangles per CAD face,
nodes per CAD edge / vertex). Loads are converted as follows:

* **Face force** (total force F on faces): uniform traction t = F / A_mesh.
  Consistent nodal loads: linear triangles give A/3 per corner; quadratic
  (6-node) triangles give 0 to corners and A/3 to each mid-side node. The
  applied total equals F exactly.
* **Vertex force**: concentrated nodal force at the vertex node.
* **Pressure**: CalculiX ``*DLOAD`` on the element faces lying on the CAD face
  (found topologically, by matching the triangle's corner nodes to the unique
  boundary face of an element).
* **Gravity**: ``*DLOAD, GRAV`` on all elements (requires density).

Under-constraint detection: the six rigid-body modes of the body evaluated at
all constrained DOFs form a matrix whose rank must be 6. Any null-space vector
is a rigid-body motion that the supports do not prevent.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from opencae.geometry.kernel import Body
from opencae.geometry.references import resolve_ref
from opencae.meshing.elements import boundary_faces, nodal_volume_weights as _nvw, surface_integrals
from opencae.meshing.mesh_data import FEMesh
from opencae.project.model import Constraint, EntityRef, Load, Project

ERROR, WARNING, INFO = "error", "warning", "info"
MODE_NAMES = ["translation X", "translation Y", "translation Z", "rotation about X", "rotation about Y",
              "rotation about Z"]


@dataclass
class Issue:
    severity: str
    message: str
    source: str = ""

    def __str__(self) -> str:
        return f"[{self.severity.upper()}] {self.source + ': ' if self.source else ''}{self.message}"


@dataclass
class ResolvedModel:
    # boundary conditions: {node_index: {dof(1..3): value}}
    spc: dict[int, dict[int, float]] = field(default_factory=dict)
    spc_sets: dict[str, np.ndarray] = field(default_factory=dict)  # bc name -> node indices
    # nodal forces (N,3)
    nodal_forces: Optional[np.ndarray] = None
    # pressure: (elem, face_no 1..4, value)
    pressures: list[tuple[int, int, float]] = field(default_factory=list)
    gravity: Optional[tuple[float, np.ndarray]] = None  # (g, dir) - applied as consistent nodal loads
    applied_force_total: np.ndarray = field(default_factory=lambda: np.zeros(3))
    # external load acting directly on constrained DOFs: CalculiX reaction forces exclude it,
    # so equilibrium reads  sum(RF) + applied_force_total - applied_on_constrained = 0
    applied_on_constrained: np.ndarray = field(default_factory=lambda: np.zeros(3))
    pressure_nodal: Optional[np.ndarray] = None  # consistent nodal equivalent of pressures (bookkeeping)
    interactions: list[dict] = field(default_factory=list)  # tie / contact: element faces of master and slave
    issues: list[Issue] = field(default_factory=list)
    # for visualisation
    load_glyphs: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(i.severity == ERROR for i in self.issues)

    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == ERROR]


# --------------------------------------------------------------------------- helpers
def _ref_nodes(ref: EntityRef, idx: int, mesh: FEMesh) -> Optional[np.ndarray]:
    if ref.kind == "face":
        return mesh.face_nodes(idx) if idx in mesh.face_tris else None
    if ref.kind == "edge":
        return mesh.edge_nodes.get(idx)
    if ref.kind == "vertex":
        return np.array([mesh.vertex_nodes[idx]]) if idx in mesh.vertex_nodes else None
    return None


def boundary_face_lookup(mesh: FEMesh) -> dict[tuple[int, ...], tuple[int, int]]:
    """Sorted corner tuple -> (element index, CalculiX face number) for element faces on
    the boundary (faces used by exactly one element). Works for tets and hexes."""
    el, fn, full = boundary_faces(mesh.elements, mesh.element_type)
    nc = 3 if mesh.shape == "tet" else 4
    keys = np.sort(full[:, :nc], axis=1)
    return {tuple(int(v) for v in k): (int(e), int(f)) for k, e, f in zip(keys, el, fn)}


def tri_areas(nodes: np.ndarray, tris: np.ndarray) -> np.ndarray:
    """Exact areas of face elements (tri3/tri6/quad4/quad8, curved geometry included)."""
    return surface_integrals(nodes, tris)[1]


def nodal_volume_weights(mesh: FEMesh) -> tuple[np.ndarray, float]:
    """Consistent body-force weights w_a = integral of N_a dV (exact Gauss integration
    with the element's actual geometry) and the integrated volume."""
    return _nvw(mesh.nodes, mesh.elements, mesh.element_type)


def rigid_body_rank(mesh: FEMesh, spc: dict[int, dict[int, float]]) -> tuple[int, list[str]]:
    """Rank of the rigid-body-mode restraint matrix and names of unrestrained modes."""
    if not spc:
        return 0, MODE_NAMES[:]
    items = [(n, d) for n, dofs in spc.items() for d in dofs]
    if len(items) > 6000:  # rank is preserved by a large random subset in practice; keep extremes
        rng = np.random.default_rng(0)
        items = [items[i] for i in rng.choice(len(items), 6000, replace=False)]
    c = mesh.nodes.mean(axis=0)
    L = float(np.ptp(mesh.nodes, axis=0).max()) or 1.0
    rows = []
    for n, d in items:
        r = (mesh.nodes[n] - c) / L
        e = np.eye(3)[d - 1]
        # translation modes, then rotations: u = w x r  ->  u_d = (w x r)·e_d = w·(r x e_d)
        rows.append(np.concatenate([e, np.cross(r, e)]))
    A = np.array(rows)
    s, vt = np.linalg.svd(A, full_matrices=True)[1:]
    tol = max(A.shape) * np.finfo(float).eps * (s[0] if len(s) else 1) * 1e3
    rank = int(np.sum(s > tol))
    free = []
    if rank < 6:
        null = vt[rank:]  # basis of unrestrained rigid motions
        for v in null:
            k = int(np.argmax(np.abs(v)))
            free.append(MODE_NAMES[k])
    return rank, free


def axis_of_normal(n) -> Optional[int]:
    n = np.asarray(n, float)
    k = int(np.argmax(np.abs(n)))
    return k + 1 if abs(n[k]) > 1 - 1e-9 else None


# --------------------------------------------------------------------------- main
def mesh_components(mesh: FEMesh) -> tuple[int, np.ndarray]:
    """Connected components of the mesh (elements sharing nodes). Separate, unbonded
    parts must each be restrained on their own."""
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components
    E = mesh.corner_elements()
    rows = np.repeat(np.arange(len(E)), E.shape[1])
    inc = coo_matrix((np.ones(E.size), (rows, E.ravel())), shape=(len(E), mesh.n_nodes)).tocsr()
    n, lab = connected_components(inc @ inc.T, directed=False)
    node_comp = np.full(mesh.n_nodes, -1)
    A = np.asarray(mesh.elements)  # all element nodes (mid-side nodes too), so every used node gets a label
    ok = A >= 0
    node_comp[A[ok]] = np.repeat(lab, A.shape[1]).reshape(A.shape)[ok]
    return n, node_comp


THERMAL_ITEM_KINDS = ("temperature", "heat_flux", "heat_power", "convection", "radiation",
                      "heat_generation")


def _resolve_interactions(project, mesh, bodies, body_ids, rm, add) -> list[tuple[int, int]]:
    """Map tie / contact interactions to element faces. Returns node pairs (one per interaction)
    that link the connected parts for the rigid-body check."""
    links = []
    inter = [i for i in getattr(project, "interactions", []) if not i.suppressed]
    if not inter:
        return links
    lookup = boundary_face_lookup(mesh)
    nc = 3 if mesh.shape == "tet" else 4
    for it in inter:
        sides = {}
        for side in ("master", "slave"):
            faces = []
            for ref in getattr(it, side):
                if ref.kind != "face" or ref.body not in body_ids:
                    add(ERROR, f"{side} {ref.label()}: interactions need faces of bodies in this study", it.name)
                    continue
                st = resolve_ref(ref, bodies)
                if not st.ok:
                    add(ERROR, f"{ref.label()}: {st.message}", it.name)
                    continue
                key = mesh.key(ref.body, st.index)
                if key not in mesh.face_tris:
                    add(ERROR, f"{ref.label()} is not mapped to the mesh", it.name)
                    continue
                for f in mesh.face_tris[key]:
                    hit = lookup.get(tuple(int(v) for v in np.sort(f[:nc])))
                    if hit is not None:
                        faces.append(hit)
            sides[side] = sorted(set(faces))
        if not sides["master"] or not sides["slave"]:
            add(ERROR, "both sides of an interaction need boundary faces. If the bodies touch, set Mesh controls > "
                       "Assembly mesh to 'separate' (a conformal mesh merges the touching faces) and remesh",
                it.name)
            continue
        if set(sides["master"]) & set(sides["slave"]):
            add(ERROR, "the same faces are on both sides of the interaction", it.name)
            continue
        if it.kind not in ("bonded", "contact"):
            add(ERROR, f"unknown interaction type '{it.kind}'", it.name)
            continue
        rm.interactions.append({"kind": it.kind, "name": it.name or it.id, "master": sides["master"],
                                "slave": sides["slave"], "params": dict(it.params)})
        e_m, e_s = sides["master"][0][0], sides["slave"][0][0]
        links.append((int(mesh.elements[e_m, 0]), int(mesh.elements[e_s, 0])))
        if it.kind == "contact" and float(it.params.get("friction", 0.0)) == 0.0:
            add(WARNING, "frictionless contact does not hold parts against sliding along the contact faces; "
                         "make sure supports prevent those motions (or use friction / bonded)", it.name)
    return links


def resolve_model(project: Project, body, mesh: FEMesh, bodies: dict[str, Body], thermal_load: bool = False) -> ResolvedModel:
    """Map supports/loads of the active study to mesh entities. ``body`` is the analysis
    body or a list of analysis bodies (multi-body study)."""
    rm = ResolvedModel(nodal_forces=np.zeros((mesh.n_nodes, 3)))
    body_list = list(body) if isinstance(body, (list, tuple)) else [body]
    body_ids = {b.id for b in body_list}
    S = mesh.KEY_STRIDE

    def bface(ref: EntityRef, key: int):
        return bodies[ref.body].faces[key % S]

    def add(sev, msg, src=""):
        rm.issues.append(Issue(sev, msg, src))

    def targets_of(item, allowed: tuple[str, ...]) -> list[tuple[EntityRef, int]]:
        out = []
        if not item.targets:
            add(ERROR, "no geometry selected", item.name)
        for ref in item.targets:
            if ref.kind not in allowed:
                add(ERROR, f"{ref.kind} targets are not supported for this item (allowed: {', '.join(allowed)})",
                    item.name)
                continue
            if ref.body not in body_ids:
                add(ERROR, "target belongs to a body that is not part of this study", item.name)
                continue
            st = resolve_ref(ref, bodies)
            if not st.ok:
                add(ERROR, f"{ref.label()}: {st.message}", item.name)
                continue
            if st.remapped:
                add(WARNING, st.message, item.name)
            key = mesh.key(ref.body, st.index)
            if key is None:
                add(ERROR, f"{ref.label()}: its body is not in the mesh - regenerate the mesh", item.name)
                continue
            out.append((ref, key))
        return out

    # ---------------- constraints
    for bc in project.constraints:
        if bc.suppressed:
            continue
        if bc.kind in THERMAL_ITEM_KINDS:
            add(WARNING, f"thermal condition '{bc.kind}' is ignored in a structural analysis", bc.name)
            continue
        if bc.kind not in ("fixed", "displacement", "symmetry"):
            add(ERROR, f"unsupported constraint type '{bc.kind}'", bc.name)
            continue
        tg = targets_of(bc, ("face", "edge", "vertex"))
        all_nodes = []
        for ref, idx in tg:
            nodes = _ref_nodes(ref, idx, mesh)
            if nodes is None or len(nodes) == 0:
                add(ERROR, f"{ref.label()} has no mesh nodes (not mapped to the mesh)", bc.name)
                continue
            if bc.kind == "fixed":
                dofs = {1: 0.0, 2: 0.0, 3: 0.0}
            elif bc.kind == "displacement":
                dofs = {i + 1: float(bc.params[k]) for i, k in enumerate(("ux", "uy", "uz"))
                        if bc.params.get(k) is not None}
                if not dofs:
                    add(ERROR, "no displacement component specified (all free)", bc.name)
                    continue
            else:  # symmetry
                if ref.kind != "face" or bface(ref, idx).normal is None:
                    add(ERROR, "symmetry requires a planar face", bc.name)
                    continue
                ax = axis_of_normal(bface(ref, idx).normal)
                if ax is None:
                    add(ERROR, "symmetry plane must be normal to a global axis (oblique planes "
                               "need a local coordinate system - not yet supported)", bc.name)
                    continue
                dofs = {ax: 0.0}
            for n in nodes:
                cur = rm.spc.setdefault(int(n), {})
                for d, v in dofs.items():
                    if d in cur and abs(cur[d] - v) > 1e-15 * max(1.0, abs(v)):
                        add(ERROR, f"conflicting prescribed values on node {int(n) + 1} dof {d}", bc.name)
                    cur[d] = v
            all_nodes.append(nodes)
        if all_nodes:
            rm.spc_sets[bc.name or bc.id] = np.unique(np.concatenate(all_nodes))

    # ---------------- interactions (tie / contact between separately meshed bodies)
    links = _resolve_interactions(project, mesh, bodies, body_ids, rm, add)

    check_rigid = project.analysis.analysis_type not in ("modal",)  # free-free modal analysis is allowed
    if check_rigid and (not project.constraints or not rm.spc):
        add(ERROR, "model has no constraints: the stiffness matrix is singular (rigid-body motion).")
    elif check_rigid:
        ncomp, node_comp = mesh_components(mesh)
        # components connected by interactions are checked together
        parent = list(range(ncomp))

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x
        for a_n, b_n in links:
            ra, rb = find(int(node_comp[a_n])), find(int(node_comp[b_n]))
            if ra != rb:
                parent[ra] = rb
        groups = np.array([find(c) for c in range(ncomp)])
        raw_comp = node_comp
        node_comp = np.where(raw_comp >= 0, groups[np.maximum(raw_comp, 0)], -1)
        for c in np.unique(node_comp[node_comp >= 0]):
            spc_c = {n: d for n, d in rm.spc.items() if node_comp[n] == c and raw_comp[n] >= 0}
            rank, free = rigid_body_rank(mesh, spc_c)  # node ids index the full mesh
            if rank < 6:
                part = ""
                if len(np.unique(node_comp[node_comp >= 0])) > 1:
                    slots = np.unique(mesh.element_body[np.isin(mesh.corner_elements(), np.nonzero(node_comp == c)[0]).any(axis=1)])
                    names = [bodies[mesh.body_ids[k]].name if mesh.body_ids[k] in bodies else mesh.body_ids[k] for k in slots]
                    part = f" Part not connected to the rest: {', '.join(names)}."
                add(ERROR, f"model is insufficiently constrained (rigid-body restraint rank {rank}/6).{part} "
                           f"Unrestrained motion(s): {', '.join(free)}.")

    # ---------------- loads
    face_lookup = None
    modal = project.analysis.analysis_type == "modal"
    structural_loads = []
    for ld in project.loads:
        if ld.suppressed:
            continue
        if ld.kind in THERMAL_ITEM_KINDS:
            add(WARNING, f"thermal load '{ld.kind}' is ignored in a structural analysis", ld.name)
            continue
        if ld.kind == "uniform_temperature":
            if not thermal_load:
                add(WARNING, "a uniform temperature only acts in a thermal-stress analysis", ld.name)
            continue
        if modal:
            add(WARNING, "loads do not affect natural frequencies in a linear modal analysis and are ignored",
                ld.name)
            continue
        structural_loads.append(ld)
        p = ld.params
        if ld.kind == "force":
            mag = float(p.get("magnitude", 0.0))
            cs = p.get("coordinate_system", "global")
            tg = targets_of(ld, ("face", "vertex"))
            if not tg:
                continue
            if cs == "global":
                d = np.asarray(p.get("direction", [0, 0, -1]), float)
                if np.linalg.norm(d) < 1e-12:
                    add(ERROR, "force direction vector is zero", ld.name)
                    continue
                d = d / np.linalg.norm(d)
            if mag == 0:
                add(WARNING, "force magnitude is zero", ld.name)
            kinds = {r.kind for r, _ in tg}
            if len(kinds) > 1:
                add(ERROR, "a force must act on faces only or vertices only", ld.name)
                continue
            if "vertex" in kinds:
                if cs != "global":
                    add(ERROR, "vertex forces require the global coordinate system", ld.name)
                    continue
                per = mag / len(tg)
                for ref, idx in tg:
                    if idx not in mesh.vertex_nodes:
                        add(ERROR, f"{ref.label()} not mapped to a mesh node", ld.name)
                        continue
                    n = mesh.vertex_nodes[idx]
                    rm.nodal_forces[n] += per * d
                    rm.load_glyphs.append({"kind": "force", "points": mesh.nodes[[n]], "dir": d})
                rm.applied_force_total += mag * d
                continue
            tris_list, dirs = [], []
            for ref, idx in tg:
                if idx not in mesh.face_tris:
                    add(ERROR, f"{ref.label()} not mapped to the mesh", ld.name)
                    continue
                if cs == "face_normal":
                    nrm = bface(ref, idx).normal
                    if nrm is None:
                        add(ERROR, "face-normal force requires planar faces", ld.name)
                        continue
                    dirs.append(np.asarray(nrm))
                else:
                    dirs.append(d)
                tris_list.append(mesh.face_tris[idx])
            if not tris_list:
                continue
            integ = [surface_integrals(mesh.nodes, t) for t in tris_list]
            area_tot = sum(a.sum() for _, a, _ in integ)
            if area_tot <= 0:
                add(ERROR, "selected faces have zero mesh area", ld.name)
                continue
            tr = mag / area_tot  # uniform traction magnitude
            for t, dd, (wN, A, _) in zip(tris_list, dirs, integ):
                # consistent nodal loads: f_a = t * integral(N_a dA), exact for curved faces
                np.add.at(rm.nodal_forces, t.ravel(), (tr * wN.ravel())[:, None] * dd[None, :])
                rm.applied_force_total += tr * A.sum() * dd
                rm.load_glyphs.append({"kind": "force", "points": mesh.nodes[np.unique(t)], "dir": dd})
        elif ld.kind in ("moment", "remote_force"):
            remote = ld.kind == "remote_force"
            mag = float(p.get("magnitude", 0.0))
            ax = np.asarray(p.get("direction", [0, 0, 1]), float)
            if np.linalg.norm(ax) < 1e-12:
                add(ERROR, ("force direction" if remote else "moment axis") + " vector is zero", ld.name)
                continue
            ax = ax / np.linalg.norm(ax)
            tg = targets_of(ld, ("face",))
            w = np.zeros(mesh.n_nodes)
            for ref, idx in tg:
                if idx not in mesh.face_tris:
                    add(ERROR, f"{ref.label()} not mapped to the mesh", ld.name)
                    continue
                t = mesh.face_tris[idx]
                wN, _, _ = surface_integrals(mesh.nodes, t)
                np.add.at(w, t.ravel(), wN.ravel())
            nodes = np.nonzero(w > 0)[0]
            if not len(nodes):
                continue
            wn = w[nodes]
            c = (wn[:, None] * mesh.nodes[nodes]).sum(axis=0) / wn.sum()  # area centroid of the faces
            r = mesh.nodes[nodes] - c
            # nodal forces f_i = w_i (a x r_i), with J a = M so that sum r_i x f_i = M exactly and sum f_i = 0
            J = (wn[:, None, None] * (np.einsum("ij,ij->i", r, r)[:, None, None] * np.eye(3)[None]
                                       - r[:, :, None] * r[:, None, :])).sum(axis=0)
            if np.linalg.matrix_rank(J, tol=1e-12 * np.trace(J)) < 3:
                add(ERROR, "a moment needs faces that are not all on one line", ld.name)
                continue
            if remote:
                # force F at point P = uniform traction F (consistent loads) + moment (P - c) x F
                F = mag * ax
                P = np.asarray(p.get("point", c), float)
                Mv = np.cross(P - c, F)
                rm.nodal_forces[nodes] += (wn / wn.sum())[:, None] * F[None, :]
                rm.applied_force_total += F
                rm.load_glyphs.append({"kind": "force", "points": P[None, :], "dir": ax})
            else:
                Mv = mag * ax
                rm.load_glyphs.append({"kind": "moment", "points": c[None, :], "dir": ax})
            if np.linalg.norm(Mv) > 0:
                a_vec = np.linalg.solve(J, Mv)
                rm.nodal_forces[nodes] += wn[:, None] * np.cross(a_vec[None, :], r)
            rm.applied_moment_total = getattr(rm, "applied_moment_total", np.zeros(3)) + Mv
            if mag == 0:
                add(WARNING, "moment magnitude is zero", ld.name)
        elif ld.kind == "pressure":
            val = float(p.get("magnitude", 0.0))
            tg = targets_of(ld, ("face",))
            if val == 0:
                add(WARNING, "pressure magnitude is zero", ld.name)
            if face_lookup is None and tg:
                face_lookup = boundary_face_lookup(mesh)
            for ref, idx in tg:
                if idx not in mesh.face_tris:
                    add(ERROR, f"{ref.label()} not mapped to the mesh", ld.name)
                    continue
                t = mesh.face_tris[idx]
                if rm.pressure_nodal is None:
                    rm.pressure_nodal = np.zeros((mesh.n_nodes, 3))
                missing = 0
                nc = 3 if mesh.shape == "tet" else 4
                wN_all, _, nvec_all = surface_integrals(mesh.nodes, t)
                for tri_full, wN, nA in zip(t, wN_all, nvec_all):
                    hit = face_lookup.get(tuple(int(v) for v in np.sort(tri_full[:nc])))
                    if hit is None:
                        missing += 1
                        continue
                    rm.pressures.append((hit[0], hit[1], val))
                    # outward orientation (away from the owning element) for the force bookkeeping
                    fc = mesh.nodes[tri_full[:nc]].mean(axis=0)
                    centre = mesh.nodes[mesh.elements[hit[0], :mesh.etype.n_corners]].mean(axis=0)
                    sgn = 1.0 if np.dot(nA, fc - centre) >= 0 else -1.0
                    area = np.linalg.norm(nA)
                    nrm = sgn * nA / area if area > 0 else nA
                    # consistent nodal equivalent of -p n dA (exact for planar faces)
                    rm.pressure_nodal[tri_full] += (-val * wN)[:, None] * nrm[None, :]
                if missing:
                    add(ERROR, f"{missing} surface triangles of {ref.label()} do not coincide with an "
                               "element boundary face (inconsistent mesh)", ld.name)
                rm.load_glyphs.append({"kind": "pressure", "points": mesh.nodes[np.unique(t)],
                                       "face": idx, "value": val})
        elif ld.kind == "gravity":
            g = float(p.get("magnitude", 0.0))
            d = np.asarray(p.get("direction", [0, 0, -1]), float)
            if np.linalg.norm(d) < 1e-12:
                add(ERROR, "gravity direction is zero", ld.name)
                continue
            mats = [project.material_for_body(b) for b in mesh.body_ids]
            if any(m is None or not m.density for m in mats):
                add(ERROR, "gravity requires a material with non-zero density on every body", ld.name)
                continue
            if rm.gravity is not None:
                add(ERROR, "only one gravity load is allowed", ld.name)
                continue
            rm.gravity = (g, d / np.linalg.norm(d))
            from opencae.materials.library import material_in_units
            from opencae.meshing.elements import nodal_volume_weights as _w
            from opencae.utilities.units import get_unit_system
            us = get_unit_system(project.unit_system)
            fg = np.zeros((mesh.n_nodes, 3))
            for k, mat in enumerate(mats):  # each body with its own density
                rho = material_in_units(mat, us)["rho"]
                els = np.nonzero(mesh.element_body == k)[0]
                w, _ = _w(mesh.nodes, mesh.elements[els], mesh.element_type)
                fg += rho * g * w[:, None] * rm.gravity[1][None, :]
            rm.nodal_forces += fg
            rm.applied_force_total += fg.sum(axis=0)
            rm.load_glyphs.append({"kind": "gravity", "dir": rm.gravity[1]})
        else:
            add(ERROR, f"unsupported load type '{ld.kind}'", ld.name)

    if rm.pressure_nodal is not None:
        rm.applied_force_total += rm.pressure_nodal.sum(axis=0)
    ext = rm.nodal_forces + (rm.pressure_nodal if rm.pressure_nodal is not None else 0.0)
    for n in rm.spc:  # CalculiX RF at a supported node = internal force, i.e. reaction + any load there
        rm.applied_on_constrained += ext[n]

    has_disp = any(v != 0 for dofs in rm.spc.values() for v in dofs.values())
    if not structural_loads and not has_disp and not modal and not thermal_load:
        add(ERROR, "no loads or non-zero prescribed displacements: the solution would be zero.")
    return rm
