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
