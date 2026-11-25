"""Multibody dynamics (assembly motion) in the analysis-type registry.

This registers the type so that the study selector lists it. A multibody study has no mesh and no external
solver process: it is run by :func:`opencae.mbd.run_study` from the assembly. It is *available* only when the
backend (MuJoCo) can be imported: ``MBDPhysics.available()`` returns ``(ok, reason)``.
"""
from __future__ import annotations

from opencae.boundary_conditions.resolve import ERROR, Issue
from opencae.physics import register


class _NoModel:
    def __init__(self, issues):
        self.issues = issues
        self.ok = False


class MBDPhysics:
    name = "mbd"
    analysis_types = ("mbd",)
    solver = "mujoco"

    def available(self) -> tuple[bool, str]:
        from opencae.mbd.backend import available
        return available()

    def resolve(self, session, bodies, mesh):
        ok, why = self.available()
        msg = ("multibody studies are run from the assembly (opencae.mbd.run_study), not with the finite-element "
               "solver" if ok else why)
        return _NoModel([Issue(ERROR, msg, "Analysis")])

    def write_input(self, session, job_dir, jobname, model) -> None:
        raise NotImplementedError("multibody studies have no solver input file; use opencae.mbd.run_study")

    def read_results(self, session, job_dir, jobname):
        from opencae.mbd.results import MBDResult
        return MBDResult.load(job_dir)

    def load_results(self, path, mesh):
        from opencae.mbd.results import MBDResult
        return MBDResult.load(path)


register(MBDPhysics())
