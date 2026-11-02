"""Solver backend interface.

A backend turns a validated, resolved model into solver input, describes how
to launch the solver process, and converts solver output into a
:class:`~opencae.postprocessing.results.ResultSet`. Future analysis types
(modal, buckling, thermal stress) add capabilities here without changing the
UI or pre-processing layers.
"""
from __future__ import annotations

from pathlib import Path
from typing import Protocol

from opencae.boundary_conditions.resolve import ResolvedModel
from opencae.meshing.mesh_data import FEMesh
from opencae.postprocessing.results import ResultSet
from opencae.project.model import Material, Project
from opencae.utilities.units import UnitSystem

SUPPORTED_ANALYSES = {"linear_static"}
SUPPORTED_ELEMENTS = {"C3D4", "C3D10"}


class SolverBackend(Protocol):
    name: str

    def executable(self) -> str | None: ...

    def version(self) -> str: ...

    def write_input(self, job_dir: Path, jobname: str, project: Project, mesh: FEMesh, rm: ResolvedModel,
                    material: Material, us: UnitSystem) -> Path: ...

    def command(self, jobname: str) -> list[str]: ...

    def read_results(self, job_dir: Path, jobname: str, mesh: FEMesh) -> ResultSet: ...


class CalculixBackend:
    name = "CalculiX"

    def __init__(self, configured_path: str | None = None) -> None:
        from opencae.solver.calculix.runner import find_ccx
        self._exe = find_ccx(configured_path)
        self._version: str | None = None

    def executable(self) -> str | None:
        return self._exe

    def version(self) -> str:
        if self._exe and self._version is None:
            from opencae.solver.calculix.runner import ccx_version
            self._version = ccx_version(self._exe)
        return self._version or "not installed"

    def write_input(self, job_dir, jobname, project, mesh, rm, material, us) -> Path:
        from opencae.solver.calculix.deck import write_deck
        job_dir = Path(job_dir)
        job_dir.mkdir(parents=True, exist_ok=True)
        for old in job_dir.glob(f"{jobname}.*"):
            old.unlink()
        path = job_dir / f"{jobname}.inp"
        path.write_text(write_deck(project, mesh, rm, material, us))
        return path

    def command(self, jobname: str) -> list[str]:
        from opencae.solver.calculix.runner import command_line
        if not self._exe:
            raise RuntimeError("CalculiX executable not found.")
        return command_line(self._exe, jobname)

    def read_results(self, job_dir, jobname, mesh) -> ResultSet:
        return ResultSet.from_calculix(Path(job_dir) / f"{jobname}.frd", Path(job_dir) / f"{jobname}.dat", mesh)
