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
from opencae.meshing.mesh_data import TET_FACES, FEMesh
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


def boundary_face_lookup(mesh: FEMesh) -> dict[tuple[int, int, int], tuple[int, int]]:
    """Sorted corner triple -> (element index, CalculiX face number 1..4) for
    element faces on the boundary (faces used by exactly one element)."""
    E = mesh.corner_elements()
    faces = E[:, TET_FACES].reshape(-1, 3)  # (4E,3)
    srt = np.sort(faces, axis=1)
    _, inv, counts = np.unique(srt, axis=0, return_inverse=True, return_counts=True)
    bmask = counts[inv.ravel()] == 1
    idx = np.nonzero(bmask)[0]
    return {tuple(srt[i]): (int(i // 4), int(i % 4) + 1) for i in idx}


def tri_areas(nodes: np.ndarray, tris: np.ndarray) -> np.ndarray:
    a, b, c = nodes[tris[:, 0]], nodes[tris[:, 1]], nodes[tris[:, 2]]
    return 0.5 * np.linalg.norm(np.cross(b - a, c - a), axis=1)


# 4-point Gauss rule on the reference tetrahedron (barycentric a, b, b, b), exact for quadratics
_GA, _GB = 0.5854101966249685, 0.1381966011250105
_GAUSS_L = np.array([[_GA, _GB, _GB, _GB], [_GB, _GA, _GB, _GB], [_GB, _GB, _GA, _GB], [_GB, _GB, _GB, _GA]])
_TET10_EDGES = [(0, 1), (1, 2), (2, 0), (0, 3), (1, 3), (2, 3)]


def _tet10_shape(L: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Shape functions (10,) and derivatives wrt (xi, eta, zeta) (10, 3) at barycentric point L,
    with L0 = 1 - xi - eta - zeta, L1 = xi, L2 = eta, L3 = zeta (CalculiX node order)."""
    N = np.zeros(10)
    dL = np.zeros((10, 4))  # dN/dL_k
    for i in range(4):
        N[i] = L[i] * (2 * L[i] - 1)
        dL[i, i] = 4 * L[i] - 1
    for k, (i, j) in enumerate(_TET10_EDGES):
        N[4 + k] = 4 * L[i] * L[j]
        dL[4 + k, i] = 4 * L[j]
        dL[4 + k, j] = 4 * L[i]
    dxi = dL[:, 1:] - dL[:, [0]]  # chain rule through L0
    return N, dxi


def nodal_volume_weights(mesh: FEMesh) -> tuple[np.ndarray, float]:
    """Consistent load weights w_a = integral of N_a over the mesh (so that a body force b
    gives nodal forces w_a * b), and the integrated volume. Uses exact integration with the
    element's actual (possibly curved) geometry for C3D10."""
    X = mesh.nodes[mesh.elements]  # (E,k,3)
    w = np.zeros(mesh.n_nodes)
    if mesh.element_type == "C3D4":
        p0, p1, p2, p3 = (X[:, i] for i in range(4))
        V = np.einsum("ij,ij->i", np.cross(p1 - p0, p2 - p0), p3 - p0) / 6.0
        np.add.at(w, mesh.elements.ravel(), np.repeat(V / 4.0, 4))
        return w, float(V.sum())
    vol = 0.0
    for L in _GAUSS_L:
        N, dN = _tet10_shape(L)
        J = np.einsum("eka,kb->eab", X, dN)  # (E,3,3)
        detJ = np.linalg.det(J) / 6.0 / 4.0  # reference volume 1/6, 4 equal weights
        np.add.at(w, mesh.elements.ravel(), (detJ[:, None] * N[None, :]).ravel())
        vol += float(detJ.sum())
    return w, vol


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
def resolve_model(project: Project, body: Body, mesh: FEMesh, bodies: dict[str, Body]) -> ResolvedModel:
    rm = ResolvedModel(nodal_forces=np.zeros((mesh.n_nodes, 3)))

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
            if ref.body != body.id:
                add(ERROR, "target belongs to a body that is not the meshed analysis body", item.name)
                continue
            st = resolve_ref(ref, bodies)
            if not st.ok:
                add(ERROR, f"{ref.label()}: {st.message}", item.name)
                continue
            if st.remapped:
                add(WARNING, st.message, item.name)
            out.append((ref, st.index))
        return out

    # ---------------- constraints
    for bc in project.constraints:
        if bc.suppressed:
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
                if ref.kind != "face" or body.faces[idx].normal is None:
                    add(ERROR, "symmetry requires a planar face", bc.name)
                    continue
                ax = axis_of_normal(body.faces[idx].normal)
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

    if not project.constraints or not rm.spc:
        add(ERROR, "model has no constraints: the stiffness matrix is singular (rigid-body motion).")
    else:
        rank, free = rigid_body_rank(mesh, rm.spc)
        if rank < 6:
            add(ERROR, f"model is insufficiently constrained (rigid-body restraint rank {rank}/6). "
                       f"Unrestrained motion(s): {', '.join(free)}.")

    # ---------------- loads
    face_lookup = None
    for ld in project.loads:
        if ld.suppressed:
            continue
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
                    nrm = body.faces[idx].normal
                    if nrm is None:
                        add(ERROR, "face-normal force requires planar faces", ld.name)
                        continue
                    dirs.append(np.asarray(nrm))
                else:
                    dirs.append(d)
                tris_list.append(mesh.face_tris[idx])
            if not tris_list:
                continue
            area_tot = sum(tri_areas(mesh.nodes, t[:, :3]).sum() for t in tris_list)
            if area_tot <= 0:
                add(ERROR, "selected faces have zero mesh area", ld.name)
                continue
            for t, dd in zip(tris_list, dirs):
                A = tri_areas(mesh.nodes, t[:, :3])
                tr = mag / area_tot  # traction magnitude
                if t.shape[1] == 3:
                    w = np.repeat(A[:, None] / 3.0, 3, axis=1)
                    cols = t
                else:  # 6-node triangle: corners 0, mid-sides A/3
                    w = np.repeat(A[:, None] / 3.0, 3, axis=1)
                    cols = t[:, 3:6]
                np.add.at(rm.nodal_forces, cols.ravel(), (tr * w.ravel())[:, None] * dd[None, :])
                rm.applied_force_total += tr * A.sum() * dd
                rm.load_glyphs.append({"kind": "force", "points": mesh.nodes[np.unique(t[:, :3])], "dir": dd})
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
                for tri_full in t:
                    tri = np.sort(tri_full[:3])
                    hit = face_lookup.get(tuple(int(v) for v in tri))
                    if hit is None:
                        missing += 1
                        continue
                    rm.pressures.append((hit[0], hit[1], val))
                    # outward normal (pointing away from the owning element) * area
                    a, b, c = mesh.nodes[tri_full[:3]]
                    nA = 0.5 * np.cross(b - a, c - a)
                    centre = mesh.nodes[mesh.elements[hit[0], :4]].mean(axis=0)
                    if np.dot(nA, (a + b + c) / 3 - centre) < 0:
                        nA = -nA
                    nodes_w = tri_full[3:6] if len(tri_full) == 6 else tri_full[:3]
                    rm.pressure_nodal[nodes_w] += -val * nA / 3.0
                if missing:
                    add(ERROR, f"{missing} surface triangles of {ref.label()} do not coincide with an "
                               "element boundary face (inconsistent mesh)", ld.name)
                rm.load_glyphs.append({"kind": "pressure", "points": mesh.nodes[np.unique(t[:, :3])],
                                       "face": idx, "value": val})
        elif ld.kind == "gravity":
            g = float(p.get("magnitude", 0.0))
            d = np.asarray(p.get("direction", [0, 0, -1]), float)
            if np.linalg.norm(d) < 1e-12:
                add(ERROR, "gravity direction is zero", ld.name)
                continue
            mat = project.material_for_body(body.id)
            if mat is None or not mat.density:
                add(ERROR, "gravity requires a material with non-zero density", ld.name)
                continue
            if rm.gravity is not None:
                add(ERROR, "only one gravity load is allowed", ld.name)
                continue
            rm.gravity = (g, d / np.linalg.norm(d))
            from opencae.materials.library import material_in_units
            from opencae.utilities.units import get_unit_system
            rho = material_in_units(mat, get_unit_system(project.unit_system))["rho"]
            w, vol = nodal_volume_weights(mesh)
            fg = rho * g * w[:, None] * rm.gravity[1][None, :]
            rm.nodal_forces += fg
            rm.applied_force_total += fg.sum(axis=0)
            rm.load_glyphs.append({"kind": "gravity", "dir": rm.gravity[1]})
        else:
            add(ERROR, f"unsupported load type '{ld.kind}'", ld.name)

    if rm.pressure_nodal is not None:
        rm.applied_force_total += rm.pressure_nodal.sum(axis=0)
    ext = rm.nodal_forces + (rm.pressure_nodal if rm.pressure_nodal is not None else 0.0)
    for n, dofs in rm.spc.items():
        for d in dofs:
            rm.applied_on_constrained[d - 1] += ext[n, d - 1]

    has_disp = any(v != 0 for dofs in rm.spc.values() for v in dofs.values())
    if not any(not l.suppressed for l in project.loads) and not has_disp:
        add(ERROR, "no loads or non-zero prescribed displacements: the solution would be zero.")
    return rm
