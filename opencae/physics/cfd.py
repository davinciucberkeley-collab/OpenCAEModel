"""Steady incompressible flow with OpenFOAM (finite volumes, simpleFoam / snappyHexMesh).

The study bodies are the FLUID region (internal flow: duct, pipe, manifold, or an
enclosure body created around solid parts for external flow). Boundary conditions
are assigned to faces of the fluid body:

* load ``velocity_inlet``: ``magnitude`` m/s; ``direction`` "normal" (into the fluid,
  planar faces) or a global vector;
* load ``pressure_outlet``: ``magnitude`` gauge pressure in Pa;
* constraint ``wall``: no-slip wall; constraint ``slip_wall``: slip / symmetry plane.

Faces without a condition are no-slip walls (reported as a warning). Flow quantities
are in SI units (m/s, Pa) whatever the project unit system; the geometry is scaled to
metres for OpenFOAM. The case is written and read by :mod:`opencae.cfd.openfoam`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from opencae.boundary_conditions.resolve import ERROR, Issue
from opencae.physics import register

FLOW_CONSTRAINTS = ("wall", "slip_wall")
FLOW_LOADS = ("velocity_inlet", "pressure_outlet")


@dataclass
class FlowModel:
    issues: list[Issue] = field(default_factory=list)
    scale: float = 1.0  # project length -> m

    @property
    def ok(self) -> bool:
        return not any(i.severity == ERROR for i in self.issues)


class FlowPhysics:
    name = "Incompressible flow (OpenFOAM)"
    solver = "openfoam"
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
                   "Cell-centred finite volumes (simpleFoam, SIMPLE) on a snappyHexMesh mesh of the fluid region."]

    def backend(self, session):
        return session.solver_backend()

    def resolve(self, session, bodies, mesh):
        from opencae.cfd.openfoam import study as foam_study
        fm = FlowModel(scale=1.0 / session.units.length_from_si)
        _, fm.issues = foam_study.build(session, None, write=False)
        return fm

    def write_input(self, session, job_dir, jobname, fm) -> None:
        from opencae.cfd.openfoam.backend import write_solve_case
        write_solve_case(session, Path(job_dir))

    def read_results(self, session, job_dir, jobname):
        from opencae.cfd.openfoam.backend import read_results as foam_read
        return foam_read(session, Path(job_dir))

    def load_results(self, path, mesh):
        from opencae.cfd.results import CFDResultSet
        return CFDResultSet.load(path, mesh)


register(FlowPhysics())
