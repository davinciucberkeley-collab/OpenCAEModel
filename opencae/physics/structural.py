"""Linear static structural analysis with CalculiX (small displacements, linear elastic)."""
from __future__ import annotations

import json
from pathlib import Path

from opencae.boundary_conditions.resolve import resolve_model
from opencae.physics import register
from opencae.postprocessing.results import ResultSet


class StructuralStatic:
    name = "Static structural"
    analysis_types = ("linear_static",)
    equations = [
        ("Equilibrium", "∇·σ + b = 0 in Ω,  σ·n = t on Γ_t,  u = ū on Γ_u"),
        ("Kinematics (small strain)", "ε = ½ (∇u + ∇uᵀ)"),
        ("Isotropic linear elasticity", "σ = λ tr(ε) I + 2 μ ε,  λ = Eν/((1+ν)(1−2ν)),  μ = E/(2(1+ν))"),
    ]
    assumptions = ["Small displacements and rotations (geometric linearity) unless NLGEOM is enabled.",
                   "Linear elastic, isotropic, homogeneous material per body; no plasticity or damage.",
                   "Static loading: inertia and damping neglected.",
                   "Bodies in one study are bonded where they touch (shared mesh nodes); no contact or sliding."]

    def resolve(self, session, bodies, mesh):
        return resolve_model(session.project, bodies, mesh, session.bodies)

    def write_input(self, session, job_dir: Path, jobname: str, rm) -> None:
        session.backend.write_input(job_dir, jobname, session.project, session.mesh, rm, session.body_materials(),
                                    session.units)
        (Path(job_dir) / "resolved_loads.json").write_text(json.dumps(
            {"applied_force_total": rm.applied_force_total.tolist(),
             "applied_on_constrained": rm.applied_on_constrained.tolist()}))

    def read_results(self, session, job_dir: Path, jobname: str):
        return session.backend.read_results(job_dir, jobname, session.mesh)

    def load_results(self, path: Path, mesh):
        return ResultSet.load(path, mesh)


register(StructuralStatic())
