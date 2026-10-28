"""Application core (no Qt).

A :class:`Session` owns the project model and the derived heavy data
(regenerated bodies, FE mesh, results) and implements every workflow step
synchronously. The GUI controller calls the same methods, running the slow
ones (meshing, solving) in background processes; tests and the command line
call them directly. This keeps engineering logic independent of the UI.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Callable, Optional

from opencae.boundary_conditions.resolve import ERROR, INFO, WARNING, Issue, ResolvedModel, resolve_model
from opencae.geometry.kernel import Body, RegenerationResult
from opencae.geometry.occ_kernel import OccKernel
from opencae.geometry.references import find_planar_face, make_ref
from opencae.materials.library import builtin_materials
from opencae.meshing.gmsh_mesher import TopologySignatures, generate_mesh
from opencae.meshing.mesh_data import FEMesh
from opencae.postprocessing.results import ResultSet
from opencae.project import io as pio
from opencae.project.model import (Constraint, Feature, Load, MeshInfo, Project, ResultsInfo)
from opencae.solver.base import SUPPORTED_ANALYSES, SUPPORTED_ELEMENTS, CalculixBackend
from opencae.solver.calculix.runner import INSTALL_HELP, SolveOutcome, run_blocking
from opencae.utilities.logging_setup import get_logger
from opencae.utilities.units import get_unit_system

log = get_logger("session")

WORKFLOW_STAGES = ["Geometry", "Materials", "Mesh", "Physics", "Constraints & Loads", "Validation", "Solve",
                   "Results", "Report"]


class Session:
    def __init__(self, project: Optional[Project] = None, ccx_path: Optional[str] = None) -> None:
        self.kernel = OccKernel()
        self.backend = CalculixBackend(ccx_path)
        self.project = project or Project(materials=builtin_materials())
        self.path: Optional[Path] = None
        self._scratch = Path(tempfile.mkdtemp(prefix="opencae_"))
        self.regen = RegenerationResult({}, {}, [])
        self.mesh: Optional[FEMesh] = None
        self.results: Optional[ResultSet] = None
        self.last_report: Optional[Path] = None
        self.running = ""  # "mesh" | "solve" while a background job runs (set by the GUI)
        self.last_solve_error = ""
        self.regenerate()

    # ------------------------------------------------------------------ paths
    @property
    def data_dir(self) -> Path:
        d = pio.data_dir_for(self.path) if self.path else self._scratch
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def units(self):
        return get_unit_system(self.project.unit_system)

    # ------------------------------------------------------------------ project IO
    def new(self, name: str = "Untitled", unit_system: str = "mm-N-s") -> None:
        self.project = Project(name=name, unit_system=unit_system)
        self.project.materials = builtin_materials()
        self.path = None
        self._scratch = Path(tempfile.mkdtemp(prefix="opencae_"))
        self.mesh = self.results = None
        self.last_solve_error = ""
        self.last_report = None
        self.regenerate()

    def open(self, path: Path) -> None:
        project = pio.load_project(Path(path))
        self.project, self.path = project, Path(path)
        self.mesh = self.results = None
        self.regenerate()
        self._load_derived()

    def save(self, path: Optional[Path] = None) -> Path:
        previous = self.path or self._scratch.with_suffix(".ocae")
        if path is None:
            if self.path is None:
                raise ValueError("No file name given.")
            path = self.path
        path = Path(path).with_suffix(pio.PROJECT_SUFFIX)
        prev_data = pio.data_dir_for(previous) if self.path else self._scratch
        new_data = pio.data_dir_for(path)
        if prev_data.resolve() != new_data.resolve() and prev_data.exists():
            if new_data.exists():
                shutil.rmtree(new_data)
            shutil.copytree(prev_data, new_data)
        pio.save_project(self.project, path)
        self.path = path
        return path

    def reload_derived(self) -> None:
        """Re-synchronise mesh/results with the files referenced by the project
        (used after undo/redo)."""
        self.mesh = self.results = None
        self.regenerate()
        self._load_derived()

    def _load_derived(self) -> None:
        p = self.project
        if p.mesh.file and (self.data_dir / p.mesh.file).exists():
            try:
                self.mesh = FEMesh.load(self.data_dir / p.mesh.file)
            except Exception as exc:
                log.warning("Could not load mesh: %s", exc)
                self.mesh = None
        r = p.results
        if self.mesh is not None and r.completed and r.directory:
            f = self.data_dir / r.directory / "results.npz"
            try:
                if f.exists():
                    self.results = ResultSet.load(f, self.mesh)
                else:
                    self.results = self.backend.read_results(self.data_dir / r.directory, r.jobname, self.mesh)
            except Exception as exc:
                log.warning("Could not load results: %s", exc)
                self.results = None

    # ------------------------------------------------------------------ geometry
    def regenerate(self) -> RegenerationResult:
        g = self.project.geometry
        scale = 1e-3 if self.units.length == "m" else 1.0
        self.regen = self.kernel.regenerate(g.features, self.data_dir, scale, g.body_names)
        return self.regen

    @property
    def bodies(self) -> dict[str, Body]:
        return self.regen.bodies

    def add_feature(self, feature: Feature, body_name: str | None = None) -> Feature:
        if not feature.name:
            n = sum(1 for f in self.project.geometry.features if f.kind == feature.kind) + 1
            feature.name = f"{feature.params.get('shape', feature.kind).replace('_', ' ').title()} {n}"
        self.project.geometry.features.append(feature)
        if feature.creates_body():
            self.project.geometry.body_names[feature.id] = body_name or feature.name
            if not self.project.mesh_settings.body or feature.kind in ("primitive", "import"):
                if not self.project.mesh_settings.body or self.project.mesh_settings.body not in self.bodies:
                    self.project.mesh_settings.body = feature.id
        self.regenerate()
        return feature

    def analysis_body(self) -> Optional[Body]:
        bid = self.project.mesh_settings.body
        if bid in self.bodies:
            return self.bodies[bid]
        solids = [self.bodies[b] for b in self.regen.order if self.bodies[b].kind == "solid"]
        return solids[-1] if solids else None

    def import_geometry(self, file: Path, to_solid: bool = False) -> Feature:
        from opencae.geometry.occ_kernel import copy_into_project
        rel = copy_into_project(Path(file), self.data_dir)
        f = Feature("import", {"file": rel, "format": Path(file).suffix.lower().lstrip("."), "to_solid": to_solid},
                    name=Path(file).stem)
        return self.add_feature(f, Path(file).stem)

    def export_geometry(self, path: Path, body_ids: list[str] | None = None) -> None:
        bodies = [self.bodies[b] for b in (body_ids or self.regen.order) if b in self.bodies]
        if not bodies:
            raise ValueError("No bodies to export.")
        self.kernel.export(bodies, Path(path), Path(path).suffix.lstrip("."), self.units.length)

    # ------------------------------------------------------------------ templates
    def create_cantilever(self, length=200.0, width=20.0, height=20.0, force=1000.0,
                          direction=(0.0, 0.0, -1.0), material_id="mat_steel", position=(0.0, 0.0, 0.0)) -> Feature:
        """Cantilever beam template: geometry + material + fixed end + tip force."""
        pos = [float(v) for v in position]
        f = self.add_feature(Feature("primitive", {"shape": "cantilever_beam", "length": length, "width": width,
                                                    "height": height, "position": pos},
                                     name="Cantilever beam"))
        body = self.bodies[f.id]
        if not any(m.id == material_id for m in self.project.materials):
            self.project.materials.extend(m for m in builtin_materials() if m.id == material_id)
        self.project.assign_material(body.id, material_id)
        fixed = find_planar_face(body, [-1, 0, 0], pos)
        tip = find_planar_face(body, [1, 0, 0], [pos[0] + length, pos[1], pos[2]])
        self.project.constraints.append(Constraint("fixed", [make_ref(body, "face", fixed)], name="Fixed end"))
        self.project.loads.append(Load("force", [make_ref(body, "face", tip)],
                                       {"magnitude": force, "direction": list(direction),
                                        "coordinate_system": "global"}, name="Tip force"))
        self.project.mesh_settings.body = body.id
        return f

    # ------------------------------------------------------------------ meshing
    def mesh_job(self) -> dict:
        """Prepare a meshing job (B-rep file + topology signatures + settings)."""
        body = self.analysis_body()
        if body is None:
            raise ValueError("No solid body to mesh.")
        if body.kind != "solid" or not body.check.is_solid:
            raise ValueError(f"Body '{body.name}' is not a solid (tessellated or open); it cannot be meshed. "
                             "Convert STL to solid or repair the geometry.")
        if not body.check.valid:
            raise ValueError(f"Body '{body.name}' failed the geometry validity check.")
        mdir = self.data_dir / "mesh"
        mdir.mkdir(parents=True, exist_ok=True)
        brep = self.kernel.write_native(body, mdir / "analysis_body.brep")
        self.project.mesh_settings.body = body.id
        return {"brep": str(brep), "topology": TopologySignatures.from_body(body).to_dict(),
                "settings": asdict(self.project.mesh_settings), "body_id": body.id,
                "out": str(mdir / "mesh.npz")}

    def generate_mesh(self, progress: Callable[[str], None] | None = None) -> FEMesh:
        job = self.mesh_job()
        from opencae.project.io import _from_dict
        from opencae.project.model import MeshSettings
        settings = _from_dict(MeshSettings, job["settings"])
        mesh = generate_mesh(Path(job["brep"]), TopologySignatures.from_dict(job["topology"]), settings,
                             progress, job["body_id"])
        self.set_mesh(mesh)
        return mesh

    def set_mesh(self, mesh: FEMesh) -> None:
        mesh_id = uuid.uuid4().hex[:12]
        # versioned file name so undo/redo can restore an earlier mesh consistently
        path = mesh.save(self.data_dir / "mesh" / f"mesh_{mesh_id}.npz")
        self.mesh = mesh
        self.results = None
        self.project.mesh = MeshInfo(file=str(path.relative_to(self.data_dir)), mesh_id=mesh_id,
                                     geometry_hash=self.project.geometry.revision_hash(),
                                     settings_hash=self.project.mesh_settings.settings_hash(), stats=mesh.stats())
        self.project.results = ResultsInfo()

    # ------------------------------------------------------------------ validation
    def validate(self) -> tuple[list[Issue], Optional[ResolvedModel]]:
        issues: list[Issue] = []

        def add(sev, msg, src):
            issues.append(Issue(sev, msg, src))

        p = self.project
        # 1 geometry
        for fid, msg in self.regen.errors.items():
            add(ERROR, msg, "Geometry")
        body = self.analysis_body()
        if body is None:
            add(ERROR, "no solid body available for analysis", "Geometry")
            return issues, None
        if body.kind != "solid" or not body.check.is_solid:
            add(ERROR, f"analysis body '{body.name}' is not an exact solid", "Geometry")
        elif not body.check.valid or not body.check.closed:
            add(ERROR, f"analysis body '{body.name}' is invalid or not closed: {body.check.issues}", "Geometry")
        size = body.size
        if self.units.length == "mm" and size < 0.1:
            add(WARNING, f"body is only {size:.3g} mm in size - check the unit system", "Units")
        if self.units.length == "m" and size > 100:
            add(WARNING, f"body is {size:.3g} m in size - check the unit system", "Units")
        # 2 mesh
        mesh = self.mesh
        if mesh is None:
            add(ERROR, "no mesh - generate the mesh", "Mesh")
        else:
            if not p.mesh_is_current():
                add(ERROR, "mesh is out of date with geometry or mesh settings - regenerate the mesh", "Mesh")
            if mesh.body_id != body.id:
                add(ERROR, "mesh belongs to a different body", "Mesh")
            if mesh.element_type not in SUPPORTED_ELEMENTS:
                add(ERROR, f"element type {mesh.element_type} not supported", "Mesh")
            for w in mesh.warnings():
                add(ERROR if "non-positive Jacobian" in w else WARNING, w, "Mesh")
        # 3 material
        mat = p.material_for_body(body.id)
        if mat is None:
            add(ERROR, f"no material assigned to '{body.name}'", "Material")
        else:
            for e in mat.validate():
                add(ERROR, e, "Material")
            if mat.is_example:
                add(WARNING, f"'{mat.name}' uses example property values - verify before engineering use",
                    "Material")
        # 4 analysis
        if p.analysis.analysis_type not in SUPPORTED_ANALYSES:
            add(ERROR, f"analysis type '{p.analysis.analysis_type}' is not implemented", "Analysis")
        if not self.backend.executable():
            add(ERROR, "CalculiX (ccx) not found - " + INSTALL_HELP.splitlines()[0], "Solver")
        elif self.backend.verified is False:
            add(ERROR, "CalculiX failed its self-test, so it cannot run here. Open Tools > Settings > "
                       "Test Solver for the cause (e.g. a missing DLL).", "Solver")
        # 5 constraints/loads
        rm = None
        if mesh is not None and mesh.body_id == body.id:
            rm = resolve_model(p, body, mesh, self.bodies)
            issues.extend(rm.issues)
        else:
            if not p.constraints:
                add(ERROR, "no constraints defined", "Constraints")
            if not p.loads:
                add(ERROR, "no loads defined", "Loads")
        return issues, rm

    # ------------------------------------------------------------------ solving
    def prepare_solve(self) -> tuple[Path, str, list[Issue]]:
        issues, rm = self.validate()
        if any(i.severity == ERROR for i in issues) or rm is None:
            return Path(), "", issues
        body = self.analysis_body()
        job_dir = self.data_dir / "results" / f"run_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        jobname = "job"
        self.backend.write_input(job_dir, jobname, self.project, self.mesh, rm,
                                 self.project.material_for_body(body.id), self.units)
        (job_dir / "resolved_loads.json").write_text(json.dumps(
            {"applied_force_total": rm.applied_force_total.tolist(),
             "applied_on_constrained": rm.applied_on_constrained.tolist()}))
        return job_dir, jobname, issues

    def run_solver(self, on_line: Callable[[str], None] | None = None,
                   cancel: Callable[[], bool] | None = None) -> tuple[SolveOutcome | None, list[Issue]]:
        job_dir, jobname, issues = self.prepare_solve()
        if not jobname:
            return None, issues
        outcome = run_blocking(self.backend.install, job_dir, jobname, self.project.analysis.threads,
                               on_line, cancel)
        if outcome.success:
            self.load_results(job_dir, jobname, outcome.wall_time)
        else:
            self.project.results = ResultsInfo()
            self.results = None
        return outcome, issues

    def load_results(self, job_dir: Path, jobname: str, wall_time: float = 0.0) -> ResultSet:
        rs = self.backend.read_results(job_dir, jobname, self.mesh)
        rs.save(Path(job_dir) / "results.npz")
        self.results = rs
        summary = rs.summary()
        try:
            rl = json.loads((Path(job_dir) / "resolved_loads.json").read_text())
            summary["applied_force_total"] = rl["applied_force_total"]
            summary["applied_on_constrained"] = rl.get("applied_on_constrained", [0.0, 0.0, 0.0])
        except (OSError, KeyError, json.JSONDecodeError):
            pass
        self.project.results = ResultsInfo(directory=str(Path(job_dir).relative_to(self.data_dir)), jobname=jobname,
                                           mesh_id=self.project.mesh.mesh_id, setup_hash=self.project.setup_hash(),
                                           completed=True, solver_version=f"CalculiX {self.backend.version()}",
                                           wall_time_s=wall_time, summary=summary)
        return rs

    # ------------------------------------------------------------------ workflow status
    def validate_cached(self) -> tuple[list[Issue], Optional[ResolvedModel]]:
        """validate() memoised on everything that affects it (cheap to call on every refresh)."""
        key = (self.project.geometry.revision_hash(), self.project.setup_hash(), self.project.mesh.mesh_id,
               self.project.mesh_settings.settings_hash(), self.project.mesh_settings.body,
               bool(self.backend.executable()), self.backend.verified)
        if getattr(self, "_vcache", (None,))[0] != key:
            self._vcache = (key, self.validate())
        return self._vcache[1]

    def workflow_status(self) -> list[tuple[str, str, str]]:
        """(stage, state, message). States: not_started, in_progress, ready, completed, invalid, failed.

        Each stage is derived from the model itself (and the validation result), so a
        stage can never appear complete while its inputs are missing."""
        p = self.project
        body = self.analysis_body()
        running = getattr(self, "running", "")
        out: list[tuple[str, str, str]] = []
        # Geometry
        n_feat = len([f for f in p.geometry.features if not f.suppressed])
        if self.regen.errors:
            geo = ("invalid", "; ".join(self.regen.errors.values()))
        elif n_feat == 0 or body is None:
            geo = ("not_started" if n_feat == 0 else "invalid",
                   "create or import geometry" if n_feat == 0 else "no valid solid body to analyse")
        elif not (body.check and body.check.valid and body.check.is_solid and body.check.closed):
            geo = ("invalid", "; ".join(body.check.issues) if body.check else "invalid geometry")
        else:
            geo = ("completed", f"{body.name}: V = {body.volume:.6g} {self.units.length}^3")
        out.append(("Geometry",) + geo)
        geo_ok = geo[0] == "completed"
        # Materials
        mat = p.material_for_body(body.id) if body else None
        if not geo_ok:
            out.append(("Materials", "not_started", "needs valid geometry"))
        elif mat is None:
            out.append(("Materials", "not_started", f"assign a material to '{body.name}'"))
        elif mat.validate():
            out.append(("Materials", "invalid", "; ".join(mat.validate())))
        else:
            out.append(("Materials", "completed", mat.name + (" (example values)" if mat.is_example else "")))
        # Mesh
        if running == "mesh":
            out.append(("Mesh", "in_progress", "meshing…"))
        elif self.mesh is None:
            out.append(("Mesh", "not_started", "generate the mesh"))
        elif not p.mesh_is_current() or (body and self.mesh.body_id != body.id):
            out.append(("Mesh", "invalid", "out of date with geometry or mesh settings - regenerate"))
        else:
            st = self.mesh.stats()
            bad = st.get("invalid_elements", 0)
            out.append(("Mesh", "invalid" if bad else "completed",
                        f"{bad} invalid elements" if bad else f"{self.mesh.n_elements} {self.mesh.element_type} elements"))
        # Physics
        if not geo_ok:
            out.append(("Physics", "not_started", "needs valid geometry"))
        elif p.analysis.analysis_type not in SUPPORTED_ANALYSES:
            out.append(("Physics", "invalid", f"analysis type '{p.analysis.analysis_type}' not available"))
        else:
            out.append(("Physics", "completed", p.analysis.analysis_type.replace("_", " ")))
        # Constraints & loads
        nc = [c for c in p.constraints if not c.suppressed]
        nl = [l for l in p.loads if not l.suppressed]
        empty = [x.name for x in nc + nl if not x.targets and x.kind != "gravity"]
        issues, rm = self.validate_cached() if geo_ok else ([], None)
        bc_err = [i for i in issues if i.severity == ERROR and i.source not in ("Geometry", "Mesh", "Material",
                                                                              "Solver", "Analysis", "Units")]
        if not nc and not nl:
            out.append(("Constraints & Loads", "not_started", "add supports and loads"))
        elif not nc or not nl or empty:
            out.append(("Constraints & Loads", "in_progress",
                        "missing supports" if not nc else "missing loads" if not nl else
                        f"no geometry selected for: {', '.join(empty)}"))
        elif bc_err:
            out.append(("Constraints & Loads", "invalid", bc_err[0].message))
        else:
            out.append(("Constraints & Loads", "completed", f"{len(nc)} supports, {len(nl)} loads"))
        # Validation
        errs = [i for i in issues if i.severity == ERROR]
        if not geo_ok:
            out.append(("Validation", "not_started", "complete the previous steps"))
        elif errs:
            out.append(("Validation", "invalid", f"{len(errs)} error(s): {errs[0]}"))
        else:
            nw = len(issues)
            out.append(("Validation", "ready", f"model ready to solve ({nw} warning(s))"))
        # Solve / Results
        fail = getattr(self, "last_solve_error", "")
        if running == "solve":
            out.append(("Solve", "in_progress", "solver running…"))
            out.append(("Results", "not_started", ""))
        elif p.results.completed and p.results_are_current() and self.results is not None:
            out.append(("Solve", "completed", p.results.solver_version))
            out.append(("Results", "completed", "results loaded"))
        elif fail:
            out.append(("Solve", "failed", fail))
            out.append(("Results", "not_started", "no results"))
        elif p.results.completed:
            out.append(("Solve", "invalid", "results are out of date - re-run"))
            out.append(("Results", "invalid", "stale"))
        else:
            ready = out[-1][1] == "ready"
            out.append(("Solve", "ready" if ready else "not_started",
                        "ready to solve" if ready else
                        ("CalculiX not found" if not self.backend.executable() else "fix validation errors first")))
            out.append(("Results", "not_started", ""))
        rep = self.last_report
        out.append(("Report", "completed" if rep and out[-1][1] == "completed" else "not_started",
                    rep.name if rep else ""))
        return out
