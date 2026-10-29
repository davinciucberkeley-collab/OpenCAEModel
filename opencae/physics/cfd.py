"""Steady incompressible flow with SU2 (finite volumes, vertex-based).

The study bodies are the FLUID region (internal flow: duct, pipe, manifold, or an
enclosure body created around solid parts for external flow). Boundary conditions
are assigned to faces of the fluid body:

* load ``velocity_inlet``: ``magnitude`` m/s; ``direction`` "normal" (into the fluid,
  planar faces) or a global vector;
* load ``pressure_outlet``: ``magnitude`` gauge pressure in Pa;
* constraint ``wall``: no-slip wall; constraint ``slip_wall``: slip / symmetry plane.

Faces without a condition are no-slip walls (reported as a warning). Flow quantities
are in SI units (m/s, Pa) whatever the project unit system; the mesh is scaled to
metres for SU2.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np

from opencae.boundary_conditions.resolve import ERROR, WARNING, Issue
from opencae.geometry.references import resolve_ref
from opencae.meshing.elements import boundary_faces
from opencae.physics import register

FLOW_CONSTRAINTS = ("wall", "slip_wall")
FLOW_LOADS = ("velocity_inlet", "pressure_outlet")


@dataclass
class FlowModel:
    bcs: list = field(default_factory=list)  # FlowBC
    issues: list[Issue] = field(default_factory=list)
    scale: float = 1.0  # project length -> m

    @property
    def ok(self) -> bool:
        return not any(i.severity == ERROR for i in self.issues)


def _marker_name(name: str, used: set) -> str:
    base = re.sub(r"[^A-Za-z0-9_]", "_", name.strip()) or "bc"
    if base[0].isdigit():
        base = "m_" + base
    out, k = base, 2
    while out in used:
        out, k = f"{base}_{k}", k + 1
    used.add(out)
    return out


def _faces_normal(nodes, faces) -> np.ndarray:
    X = nodes[faces[:, :3]]
    n = np.cross(X[:, 1] - X[:, 0], X[:, 2] - X[:, 0])
    return n


def resolve_flow(session, bodies_list, mesh) -> FlowModel:
    from opencae.cfd.su2 import FlowBC
    p = session.project
    a = p.analysis
    us = session.units
    fm = FlowModel(scale=1.0 / us.length_from_si)

    def add(sev, msg, src=""):
        fm.issues.append(Issue(sev, msg, src))

    if mesh.order != 1:
        add(ERROR, "flow studies need linear elements: set Mesh controls > Element type to C3D4 (linear tet) or "
                   "C3D8 (linear hex) and regenerate the mesh", "Mesh")
        return fm
    if a.fluid_density <= 0 or a.fluid_viscosity <= 0:
        add(ERROR, "fluid density and viscosity must be > 0", "Analysis")
    if a.cfd_dimension != "3d":
        add(ERROR, "2-D flow uses a planar mesh - not available for this study yet", "Analysis")
    body_ids = {b.id for b in bodies_list}
    # all boundary faces of the fluid mesh (sorted corner tuples)
    el, fn, full = boundary_faces(mesh.elements, mesh.element_type)
    nc = 3 if mesh.shape == "tet" else 4
    corners = full[:, :nc]
    all_keys = {tuple(sorted(int(v) for v in f)): i for i, f in enumerate(corners)}
    assigned: dict[int, str] = {}
    used: set = set()
    # orientation: outward normals of boundary faces from the element interior
    cent_el = mesh.nodes[mesh.corner_elements()[el]].mean(axis=1)

    def outward(fidx: np.ndarray) -> np.ndarray:
        nv = _faces_normal(mesh.nodes, corners[fidx])
        fc = mesh.nodes[corners[fidx]].mean(axis=1)
        sgn = np.sign(np.einsum("ij,ij->i", nv, fc - cent_el[fidx]))
        return nv * sgn[:, None]

    def oriented(fidx: np.ndarray) -> np.ndarray:
        """Face corner lists ordered so that their right-hand normal points out of the fluid."""
        f = corners[fidx].copy()
        nv = _faces_normal(mesh.nodes, f)
        fc = mesh.nodes[f].mean(axis=1)
        flip = np.einsum("ij,ij->i", nv, fc - cent_el[fidx]) < 0
        f[flip] = f[flip][:, ::-1]
        return f

    items = [(c, "constraint") for c in p.constraints if not c.suppressed] + \
            [(l, "load") for l in p.loads if not l.suppressed]
    n_in = n_out = 0
    for it, _ in items:
        kind = it.kind
        if kind not in FLOW_CONSTRAINTS + FLOW_LOADS:
            add(WARNING, f"'{kind}' does not act in a flow analysis and is ignored", it.name)
            continue
        faces_idx = []
        for ref in it.targets:
            if ref.kind != "face" or ref.body not in body_ids:
                add(ERROR, f"{ref.label()}: flow conditions need faces of the fluid body", it.name)
                continue
            st = resolve_ref(ref, session.bodies)
            if not st.ok:
                add(ERROR, f"{ref.label()}: {st.message}", it.name)
                continue
            key = mesh.key(ref.body, st.index)
            if key not in mesh.face_tris:
                add(ERROR, f"{ref.label()} is not mapped to the mesh", it.name)
                continue
            for f in mesh.face_tris[key]:
                k = tuple(sorted(int(v) for v in f[:nc]))
                if k not in all_keys:
                    continue  # interior interface between fluid bodies
                fi = all_keys[k]
                if fi in assigned and assigned[fi] != it.name:
                    add(ERROR, f"a face is assigned to both '{assigned[fi]}' and '{it.name}'", it.name)
                assigned[fi] = it.name
                faces_idx.append(fi)
        if not it.targets:
            add(ERROR, "no faces selected", it.name)
            continue
        if not faces_idx:
            continue
        fidx = np.unique(faces_idx)
        bc = FlowBC(_marker_name(it.name, used), kind, oriented(fidx), source=it.name)
        if kind == "velocity_inlet":
            n_in += 1
            bc.speed = float(it.params.get("magnitude", 1.0))
            if bc.speed <= 0:
                add(ERROR, "inlet speed must be > 0 (use an outlet for outflow)", it.name)
            d = it.params.get("direction", "normal")
            if isinstance(d, str) or d is None:
                nv = outward(fidx).sum(axis=0)
                if np.linalg.norm(nv) < 1e-12:
                    add(ERROR, "cannot derive an inlet direction from these faces; give a vector", it.name)
                    continue
                inward = -nv / np.linalg.norm(nv)
                spread = np.abs(np.einsum("ij,j->i", outward(fidx) / np.linalg.norm(outward(fidx), axis=1,
                                                                                         keepdims=True), inward))
                if spread.min() < 0.98:
                    add(WARNING, "inlet faces are not planar; the flow enters along their average normal", it.name)
                bc.direction = inward
            else:
                dv = np.asarray(d, float)
                if np.linalg.norm(dv) < 1e-12:
                    add(ERROR, "inlet direction vector is zero", it.name)
                    continue
                bc.direction = dv / np.linalg.norm(dv)
        elif kind == "pressure_outlet":
            n_out += 1
            bc.pressure = float(it.params.get("magnitude", 0.0))
        fm.bcs.append(bc)
    rest = np.array([i for i in range(len(corners)) if i not in assigned], dtype=np.int64)
    if len(rest):
        add(WARNING, f"{len(rest)} boundary faces have no flow condition and are treated as no-slip walls",
            "Flow conditions")
        fm.bcs.append(FlowBC(_marker_name("walls_default", used), "wall", oriented(rest), source="unassigned faces"))
    if n_in == 0 and n_out == 0:
        add(ERROR, "no inlet or outlet: define a velocity inlet and a pressure outlet", "Flow conditions")
    elif n_out == 0:
        add(WARNING, "no pressure outlet: the pressure level is undetermined (closed domain)", "Flow conditions")
    elif n_in == 0:
        add(WARNING, "no velocity inlet: the flow is driven only by outlet pressure differences", "Flow conditions")
    rho, mu = a.fluid_density, a.fluid_viscosity
    if fm.bcs and n_in:
        v = max(b.speed for b in fm.bcs if b.kind == "velocity_inlet")
        Lref = float(np.ptp(mesh.nodes, axis=0).max()) / us.length_from_si
        Re = rho * v * Lref / mu
        if a.flow_model == "laminar" and Re > 5e4:
            add(WARNING, f"Reynolds number based on the model size is about {Re:.3g}: the flow is probably "
                         "turbulent; consider the SA or SST turbulence model", "Analysis")
    return fm


def flow_balance(mesh, bcs, velocity, pressure, rho: float, scale: float) -> dict:
    """Per-marker volumetric flow (m^3/s, + = leaving the fluid), mean pressure, and the
    mass balance over all inlets and outlets. Face corner lists are ordered with outward
    normals (see resolve_flow), velocities are averaged over each face's corners."""
    nodes = mesh.nodes * scale
    out = {}
    net, inflow = 0.0, 0.0
    for b in bcs:
        if b.kind not in ("velocity_inlet", "pressure_outlet"):
            continue
        f = np.asarray(b.faces)
        X = nodes[f]
        if f.shape[1] == 3:
            nv = 0.5 * np.cross(X[:, 1] - X[:, 0], X[:, 2] - X[:, 0])
        else:
            nv = 0.5 * np.cross(X[:, 2] - X[:, 0], X[:, 3] - X[:, 1])
        A = np.linalg.norm(nv, axis=1)
        q = float(np.einsum("ij,ij->i", velocity[f].mean(axis=1), nv).sum())
        pm = float((pressure[f].mean(axis=1) * A).sum() / A.sum())
        out[b.marker] = {"source": b.source, "kind": b.kind, "flow_m3s": q, "mass_flow_kgs": rho * q,
                         "area_m2": float(A.sum()), "mean_pressure_Pa": pm}
        net += q
        inflow += max(-q, 0.0)
    return {"markers": out, "net_flow_m3s": net, "inflow_m3s": inflow,
            "mass_imbalance": abs(net) / inflow if inflow > 0 else None}


class FlowPhysics:
    name = "Incompressible flow (SU2)"
    solver = "su2"
    analysis_types = ("cfd_incompressible",)
    equations = [
        ("Continuity", "∇·u = 0"),
        ("Momentum (steady)", "ρ (u·∇) u = −∇p + ∇·[(μ + μ_t)(∇u + ∇uᵀ)]"),
        ("Turbulence", "laminar: μ_t = 0;  SA: one-equation Spalart–Allmaras;  SST: Menter k–ω SST"),
        ("Boundary conditions", "inlet: u = U n;  outlet: p = p̄;  wall: u = 0;  slip: u·n = 0"),
    ]
    assumptions = ["Steady, incompressible, isothermal, Newtonian fluid with constant density and viscosity.",
                   "RANS turbulence models predict the mean flow only; wall-resolved meshes (y+ ~ 1) are needed "
                   "for accurate wall shear with SA/SST.",
                   "Uniform velocity inlets; at inlet edges touching no-slip walls the velocity is zero, so the "
                   "delivered flow rate on coarse meshes is slightly below the nominal value (reported).",
                   "Vertex-based finite volumes on the study's linear tet/hex mesh (SU2 FDS scheme, MUSCL "
                   "second-order reconstruction)."]

    def backend(self, session):
        return session.solver_backend()

    def resolve(self, session, bodies, mesh):
        return resolve_flow(session, bodies, mesh)

    def write_input(self, session, job_dir, jobname, fm) -> None:
        from opencae.cfd.su2 import write_config
        from opencae.cfd.su2_mesh import write_su2
        mesh = session.mesh
        markers = {b.marker: b.faces for b in fm.bcs}
        write_su2(Path(job_dir) / f"{jobname}.su2", mesh.nodes, mesh.corner_elements(), markers, fm.scale)
        write_config(Path(job_dir) / f"{jobname}.cfg", f"{jobname}.su2", session.project.analysis, fm.bcs)
        (Path(job_dir) / "flow_markers.json").write_text(json.dumps(
            [{"marker": b.marker, "kind": b.kind, "source": b.source, "speed": b.speed,
              "direction": None if b.direction is None else list(map(float, b.direction)),
              "pressure": b.pressure, "faces": np.asarray(b.faces).tolist()} for b in fm.bcs]))
        (Path(job_dir) / "resolved_loads.json").write_text("{}")

    def read_results(self, session, job_dir, jobname):
        from opencae.cfd.results import CFDResultSet
        from opencae.cfd.su2 import FlowBC, read_history, read_restart
        from opencae.postprocessing.results import ResultsError
        job_dir = Path(job_dir)
        mesh = session.mesh
        a = session.project.analysis
        d = read_restart(job_dir / "restart.csv")
        if len(d["PointID"]) != mesh.n_nodes:
            raise ResultsError(f"SU2 wrote {len(d['PointID'])} points, the mesh has {mesh.n_nodes} nodes.")
        vel = np.zeros((mesh.n_nodes, 3))
        ids = d["PointID"].astype(int)
        for k, c in enumerate("xyz"):
            col = f"Velocity_{c}"
            if col in d:
                vel[ids, k] = d[col]
        pres = np.zeros(mesh.n_nodes)
        pres[ids] = d["Pressure"]
        mut = None
        if "Eddy_Viscosity" in d:
            mut = np.zeros(mesh.n_nodes)
            mut[ids] = d["Eddy_Viscosity"]
        bcs = []
        for m in json.loads((job_dir / "flow_markers.json").read_text()):
            bcs.append(FlowBC(m["marker"], m["kind"], np.asarray(m["faces"]), m["speed"],
                              None if m["direction"] is None else np.asarray(m["direction"]), m["pressure"],
                              m["source"]))
        scale = 1.0 / session.units.length_from_si
        info = flow_balance(mesh, bcs, vel, pres, a.fluid_density, scale)
        info["converged"] = False
        log = (job_dir / "solver.log").read_text(errors="replace") if (job_dir / "solver.log").exists() else ""
        info["converged"] = "All convergence criteria satisfied" in log
        hist = job_dir / "history.csv"
        if hist.exists():
            h = read_history(hist)
            if len(h.get("Inner_Iter", [])):
                info["iterations"] = int(h["Inner_Iter"][-1]) + 1
                info["final_rms_pressure"] = float(h["rms[P]"][-1]) if "rms[P]" in h else None
                if "relrms[P]" in h:
                    info["residual_drop_pressure"] = float(h["relrms[P]"][-1])
                if "RefForce" in h and h["RefForce"][-1] > 0:
                    rf = float(h["RefForce"][-1])  # REF_AREA = 1 m^2, so force = coefficient * RefForce
                    info["wall_force_N"] = [float(h[c][-1]) * rf for c in ("CFx", "CFy", "CFz") if c in h]
        V = a.ref_velocity or max([b.speed for b in bcs if b.kind == "velocity_inlet"] or [0.0])
        if a.ref_area > 0 and V > 0 and "wall_force_N" in info:
            q = 0.5 * a.fluid_density * V ** 2 * a.ref_area
            info["force_coefficients"] = [f / q for f in info["wall_force_N"]]
            info["dynamic_pressure_Pa"] = 0.5 * a.fluid_density * V ** 2
        ins = [m for m in info["markers"].values() if m["kind"] == "velocity_inlet"]
        outs = [m for m in info["markers"].values() if m["kind"] == "pressure_outlet"]
        if ins and outs:
            info["pressure_drop_Pa"] = (sum(m["mean_pressure_Pa"] * m["area_m2"] for m in ins) /
                                        sum(m["area_m2"] for m in ins) -
                                        sum(m["mean_pressure_Pa"] * m["area_m2"] for m in outs) /
                                        sum(m["area_m2"] for m in outs))
            info["nominal_inflow_m3s"] = sum(next(b.speed for b in bcs if b.marker == k) * m["area_m2"]
                                             for k, m in info["markers"].items() if m["kind"] == "velocity_inlet")
        return CFDResultSet(mesh, vel, pres, mut, a.fluid_density, info)

    def load_results(self, path, mesh):
        from opencae.cfd.results import CFDResultSet
        return CFDResultSet.load(path, mesh)


register(FlowPhysics())
