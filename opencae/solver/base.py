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

class _Analyses:
    """Analysis types with a registered physics workflow (see opencae.physics)."""

    def __contains__(self, item) -> bool:
        from opencae.physics import available_analysis_types
        return item in available_analysis_types()

    def __iter__(self):
        from opencae.physics import available_analysis_types
        return iter(available_analysis_types())


SUPPORTED_ANALYSES = _Analyses()
SUPPORTED_ELEMENTS = {"C3D4", "C3D10", "C3D8", "C3D20", "F2D3", "F2D4"}  # F2D*: planar flow cells (SU2 only)


class SolverBackend(Protocol):
    name: str

    def executable(self) -> str | None: ...

    def version(self) -> str: ...

    def write_input(self, job_dir: Path, jobname: str, project: Project, mesh: FEMesh, rm: ResolvedModel,
                    material: Material, us: UnitSystem) -> Path: ...

    def command(self, jobname: str) -> list[str]: ...

    def read_results(self, job_dir: Path, jobname: str, mesh: FEMesh) -> ResultSet: ...


class CalculixBackend:
    """CalculiX adapter. Discovery and launching go through the isolated launcher
    (:mod:`opencae.solver.launcher`); readiness is decided by an actual test run,
    not by the existence of the executable."""

    name = "CalculiX"

    def __init__(self, configured_path: str | None = None, extra_dll_dirs: list[str] | None = None) -> None:
        from opencae.solver.launcher import find_install
        from opencae.utilities.settings import get_setting
        configured = configured_path or get_setting("ccx_path") or None
        extra = extra_dll_dirs if extra_dll_dirs is not None else (get_setting("ccx_extra_dll_dirs") or [])
        self.install = find_install(configured, extra)
        self._version: str | None = None
        self.verified: bool | None = None  # None = not tested yet in this session
        self.last_report = None

    def executable(self) -> str | None:
        return self.install.exe if self.install else None

    def version(self) -> str:
        if self.install and self._version is None:
            from opencae.solver.calculix.runner import ccx_version
            self._version = ccx_version(self.install)
        return self._version or "not installed"

    def status_text(self) -> str:
        if not self.install:
            return "CalculiX not found"
        if self.verified is True:
            return f"CalculiX {self._version or ''} verified".replace("  ", " ")
        if self.verified is False:
            return "CalculiX FAILED self-test"
        return "CalculiX untested (run Tools > Settings > Test Solver)"

    def selftest(self, keep_dir=None):
        from opencae.solver.calculix.selftest import run_selftest
        rep = run_selftest(self.install, keep_dir)
        self.verified = rep.ok
        self.last_report = rep
        if rep.version:
            self._version = rep.version
        return rep

    def write_input(self, job_dir, jobname, project, mesh, rm, material, us) -> Path:
        from opencae.solver.calculix.deck import write_deck
        job_dir = Path(job_dir)
        job_dir.mkdir(parents=True, exist_ok=True)
        for old in job_dir.glob(f"{jobname}.*"):
            old.unlink()
        path = job_dir / f"{jobname}.inp"
        path.write_text(write_deck(project, mesh, rm, material, us), encoding="utf-8", errors="replace")
        return path

    def command(self, jobname: str) -> list[str]:
        from opencae.solver.calculix.runner import command_line
        if not self.install:
            raise RuntimeError("CalculiX executable not found.")
        return command_line(self.install.exe, jobname)

    def environment(self, threads: int) -> dict[str, str]:
        """Complete environment for the solver child process (GUI env untouched)."""
        from opencae.solver.launcher import solver_environment
        return solver_environment(self.install, threads)

    def analyse(self, text: str, returncode: int, job_dir, jobname: str):
        from opencae.solver.calculix.runner import analyse_output
        return analyse_output(text, returncode, Path(job_dir) / f"{jobname}.frd")

    def read_results(self, job_dir, jobname, mesh) -> ResultSet:
        return ResultSet.from_calculix(Path(job_dir) / f"{jobname}.frd", Path(job_dir) / f"{jobname}.dat", mesh)
