"""Linear static structural analysis with CalculiX (small displacements, linear elastic)."""
from __future__ import annotations

import json

import numpy as np
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


class ThermalStress(StructuralStatic):
    """Static structural analysis loaded by a temperature field.

    Temperatures come from a solved thermal study (final time frame) and/or a
    ``uniform_temperature`` load. Thermal strain ε_th = α (T − T_ref) with T_ref the
    stress-free reference temperature of the analysis settings.
    """

    name = "Thermal stress"
    analysis_types = ("thermal_stress",)
    equations = StructuralStatic.equations[:2] + [
        ("Thermo-elasticity", "σ = C : (ε − ε_th),  ε_th = α (T − T_ref) I")]
    assumptions = StructuralStatic.assumptions + [
        "One-way coupling: the temperature field is imposed; deformation does not change the heat flow.",
        "Temperature-independent E, ν and α.",
        "Temperatures from a thermal study on a different mesh are interpolated linearly inside the "
        "source elements (corner nodes)."]

    def resolve(self, session, bodies, mesh):
        from opencae.boundary_conditions.resolve import ERROR, WARNING, Issue
        p = session.project
        a = p.analysis
        T = np.full(mesh.n_nodes, np.nan)
        issues = []
        uniform = [ld for ld in p.loads if ld.kind == "uniform_temperature" and not ld.suppressed]
        for ld in uniform:
            T[:] = float(ld.params.get("value", a.reference_temperature))
        if a.temperature_source_study:
            src = p.study_by_id(a.temperature_source_study)
            if src is None or src.analysis.analysis_type not in ("thermal_steady", "thermal_transient"):
                issues.append(Issue(ERROR, "the temperature source must be a thermal study", "Analysis"))
            elif not src.results.completed:
                issues.append(Issue(ERROR, f"thermal study '{src.name}' has not been solved", "Analysis"))
            else:
                cur = p.active_study
                p.active_study = src.id
                try:
                    current = p.results_are_current()
                finally:
                    p.active_study = cur
                if not current:
                    issues.append(Issue(ERROR, f"results of thermal study '{src.name}' are out of date - "
                                               "re-solve it first", "Analysis"))
                smesh, srs = session.load_study_results(src.id)
                if srs is None or getattr(srs, "kind", "") != "thermal":
                    issues.append(Issue(ERROR, f"no temperature results available from '{src.name}'", "Analysis"))
                else:
                    Ts = srs.temperature[-1]
                    if (smesh is mesh) or (smesh.nodes.shape == mesh.nodes.shape and
                                                         np.allclose(smesh.nodes, mesh.nodes, rtol=0, atol=1e-9)):
                        T = Ts.copy()
                    else:
                        T, n_out = interpolate_nodal(smesh, Ts, mesh.nodes)
                        issues.append(Issue(WARNING, f"temperatures interpolated from the mesh of '{src.name}' "
                                                     f"(different mesh; {n_out} nodes outside it took the "
                                                     "nearest element value)", "Analysis"))
        rm = resolve_model(p, bodies, mesh, session.bodies, thermal_load=bool(uniform or a.temperature_source_study))
        rm.issues += issues
        if not uniform and not a.temperature_source_study:
            rm.issues.append(Issue(ERROR, "no temperature load: choose a thermal study as temperature source "
                                          "or add a uniform temperature", "Analysis"))
        elif np.isnan(T).any():
            rm.issues.append(Issue(ERROR, "temperature field is incomplete", "Analysis"))
        for b in bodies:
            mat = p.material_for_body(b.id)
            if mat is not None and not mat.thermal_expansion:
                rm.issues.append(Issue(WARNING, f"'{mat.name}' has no thermal expansion coefficient: "
                                                "it will not expand", "Material"))
        rm.node_temperature = T
        rm.reference_temperature = a.reference_temperature
        return rm

    def write_input(self, session, job_dir: Path, jobname: str, rm) -> None:
        super().write_input(session, job_dir, jobname, rm)
        T = rm.node_temperature
        (Path(job_dir) / "resolved_loads.json").write_text(json.dumps(
            {"applied_force_total": rm.applied_force_total.tolist(),
             "applied_on_constrained": rm.applied_on_constrained.tolist(),
             "temperature_min": float(np.min(T)), "temperature_max": float(np.max(T)),
             "reference_temperature": rm.reference_temperature}))


from opencae.postprocessing.sampling import interpolate_nodal  # noqa: E402  (re-export)


register(ThermalStress())
