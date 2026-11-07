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

import numpy as np
from dataclasses import asdict
from pathlib import Path
from typing import Callable, Optional

from opencae.boundary_conditions.resolve import ERROR, INFO, WARNING, Issue, ResolvedModel, resolve_model
from opencae.geometry.kernel import Body, RegenerationResult
from opencae.geometry.occ_kernel import OccKernel
from opencae.geometry.references import find_planar_face, make_ref
from opencae.materials.library import builtin_materials
from opencae.meshing.gmsh_mesher import MeshPart, TopologySignatures, generate_mesh
from opencae.meshing.mesh_data import FEMesh
from opencae.postprocessing.results import ResultSet
from opencae.project import io as pio
from opencae.project.model import (ANALYSIS_TYPES, AnalysisSettings, Constraint, Feature, Load, MeshInfo,
                                   MeshSettings, Project, ResultsInfo, Study)
from opencae.solver.base import SUPPORTED_ANALYSES, SUPPORTED_ELEMENTS, CalculixBackend
from opencae.solver.calculix.runner import INSTALL_HELP, SolveOutcome, run_blocking
from opencae.utilities.logging_setup import get_logger
from opencae.utilities.units import get_unit_system

log = get_logger("session")

WORKFLOW_STAGES = ["Geometry", "Materials", "Mesh", "Physics", "Constraints & Loads", "Validation", "Solve",
                   "Results", "Report"]


def face_edge_indices(body, face_index: int) -> list[int]:
    """Edges bounding a planar face (centroid on the face plane and inside its bounding box)."""
    f = body.faces[face_index]
    if f.normal is None:
        return []
    n = np.asarray(f.normal)
    lo, hi = (np.asarray(v) for v in f.bbox)
    tol = 1e-6 * max(body.size, 1e-9)
    return [e.index for e in body.edges
            if abs(n @ (np.asarray(e.centroid) - np.asarray(f.centroid))) <= tol
            and np.all(np.asarray(e.centroid) >= lo - tol) and np.all(np.asarray(e.centroid) <= hi + tol)]


_SCRATCH_DIRS: set = set()


def _cleanup_scratch() -> None:
    """Remove the temporary data folders of projects that were never saved (at interpreter exit)."""
    for d in list(_SCRATCH_DIRS):
        shutil.rmtree(d, ignore_errors=True)
        _SCRATCH_DIRS.discard(d)


import atexit as _atexit  # noqa: E402

_atexit.register(_cleanup_scratch)


class Session:
    def __init__(self, project: Optional[Project] = None, ccx_path: Optional[str] = None) -> None:
        self.kernel = OccKernel()
        self.backend = CalculixBackend(ccx_path)
        self.project = project or Project(materials=builtin_materials())
        self.path: Optional[Path] = None
        self._scratch_dir: Optional[Path] = None  # data folder of an unsaved project (created when needed)
        self.regen = RegenerationResult({}, {}, [])
        self._derived: dict[str, dict] = {}  # study id -> {"mesh": FEMesh, "results": ResultSet}
        self.last_report: Optional[Path] = None
        self.running = ""  # "mesh" | "solve" while a background job runs (set by the GUI)
        self.last_solve_error = ""
        self.regenerate()

    # ------------------------------------------------------------------ per-study derived data
    def _slot(self) -> dict:
        return self._derived.setdefault(self.project.active_study, {"mesh": None, "results": None})

    @property
    def mesh(self) -> Optional[FEMesh]:
        return self._slot()["mesh"]

    @mesh.setter
    def mesh(self, value: Optional[FEMesh]) -> None:
        self._slot()["mesh"] = value

    @property
    def results(self):
        return self._slot()["results"]

    @results.setter
    def results(self, value) -> None:
        self._slot()["results"] = value

    @property
    def study(self) -> Study:
        return self.project.study

    # ------------------------------------------------------------------ solvers
    def solver_backend(self):
        """Solver used by the active study: CalculiX, or SU2 for flow studies."""
        from opencae.physics import physics_for
        try:
            ph = physics_for(self.project.analysis.analysis_type)
        except KeyError:
            return self.backend
        if getattr(ph, "solver", "calculix") == "su2":
            if self.is_openfoam_study():
                if getattr(self, "foam_backend", None) is None:
                    from opencae.cfd.openfoam.backend import OpenFOAMBackend
                    from opencae.utilities.settings import get_setting
                    self.foam_backend = OpenFOAMBackend(get_setting("openfoam_env") or "",
                                                        get_setting("openfoam_wsl_distro") or "")
                return self.foam_backend
            if getattr(self, "cfd_backend", None) is None:
                from opencae.cfd.su2 import SU2Backend
                self.cfd_backend = SU2Backend()
            return self.cfd_backend
        return self.backend

    def is_openfoam_study(self, study=None) -> bool:
        st = study or self.study
        return st.analysis.analysis_type.startswith("cfd") and getattr(st.analysis, "cfd_solver", "su2") == "openfoam"

    # ------------------------------------------------------------------ studies
    def set_active_study(self, study_id: str) -> None:
        if self.project.study_by_id(study_id) is None:
            raise KeyError(study_id)
        self.project.active_study = study_id
        if study_id not in self._derived:
            self._load_derived()
        self.last_solve_error = ""

    def new_study(self, name: str, analysis_type: str = "linear_static", body_ids: Optional[list[str]] = None,
                  activate: bool = True) -> Study:
        if analysis_type not in ANALYSIS_TYPES:
            raise ValueError(f"Unknown analysis type '{analysis_type}'")
        cur = self.study
        bodies = list(body_ids) if body_ids else cur.analysis_body_ids()
        st = Study(name=name, body_ids=bodies if len(bodies) > 1 or body_ids else [],
                   mesh_settings=MeshSettings(body=bodies[0] if bodies else ""),
                   analysis=AnalysisSettings(analysis_type=analysis_type, title=ANALYSIS_TYPES[analysis_type],
                                             threads=cur.analysis.threads))
        # carry material assignments of the selected bodies over (they can be changed per study)
        st.assignments = [a for a in cur.assignments if a.body in bodies]
        self.project.studies.append(st)
        if activate:
            self.set_active_study(st.id)
        return st

    def duplicate_study(self, study_id: Optional[str] = None, name: Optional[str] = None,
                        activate: bool = True) -> Study:
        """Copy a study's complete setup (not its mesh/results files, which stay shared read-only)."""
        import copy as _copy
        from opencae.project.model import new_id
        src = self.project.study_by_id(study_id or self.project.active_study)
        st = _copy.deepcopy(src)
        st.id = new_id("study")
        st.name = name or f"{src.name} (copy)"
        st.results = ResultsInfo()
        for item in st.constraints + st.loads:
            item.id = new_id("bc" if item in st.constraints else "load")
        self.project.studies.append(st)
        if src.id in self._derived:  # same mesh object is valid for the copy as long as it is current
            self._derived[st.id] = {"mesh": self._derived[src.id]["mesh"], "results": None}
        if activate:
            self.set_active_study(st.id)
        return st

    def rename_study(self, study_id: str, name: str) -> None:
        self.project.study_by_id(study_id).name = name

    def delete_study(self, study_id: str) -> None:
        if len(self.project.studies) == 1:
            raise ValueError("A project needs at least one study.")
        self.project.studies = [s for s in self.project.studies if s.id != study_id]
        self._derived.pop(study_id, None)
        if self.project.active_study == study_id:
            self.set_active_study(self.project.studies[0].id)

    def set_study_bodies(self, body_ids: list[str]) -> None:
        st = self.study
        st.body_ids = list(body_ids) if len(body_ids) > 1 else []
        st.mesh_settings.body = body_ids[0] if body_ids else ""

    def load_study_results(self, study_id: str):
        """Mesh and results of any study (loaded on demand, e.g. for comparisons)."""
        cur = self.project.active_study
        if study_id not in self._derived:
            self.project.active_study = study_id
            try:
                self._load_derived()
            finally:
                self.project.active_study = cur
        d = self._derived.get(study_id, {})
        return d.get("mesh"), d.get("results")

    # ------------------------------------------------------------------ paths
    @property
    def _scratch(self) -> Path:
        if self._scratch_dir is None:
            self._scratch_dir = Path(tempfile.mkdtemp(prefix="opencae_"))
            _SCRATCH_DIRS.add(self._scratch_dir)
        return self._scratch_dir

    @_scratch.setter
    def _scratch(self, value: Path) -> None:  # used by workers that manage their own temporary folder
        self._scratch_dir = Path(value)

    def _drop_scratch(self) -> None:
        """Delete the temporary data folder (after it was saved, or when the project is discarded)."""
        if self._scratch_dir is not None:
            shutil.rmtree(self._scratch_dir, ignore_errors=True)
            _SCRATCH_DIRS.discard(self._scratch_dir)
            self._scratch_dir = None

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
        self._drop_scratch()
        self._derived = {}
        self.last_solve_error = ""
        self.last_report = None
        self.regenerate()

    def open(self, path: Path) -> None:
        project = pio.load_project(Path(path))
        self.project, self.path = project, Path(path)
        self._drop_scratch()
        self._derived = {}
        self.last_solve_error = ""
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
        was_scratch = self.path is None
        self.path = path
        if was_scratch:
            self._drop_scratch()
        return path

    def reload_derived(self) -> None:
        """Re-synchronise mesh/results with the files referenced by the project
        (used after undo/redo)."""
        self._derived = {}
        self.regenerate()
        self._load_derived()

    def _load_derived(self) -> None:
        p = self.project
        self.mesh = self.results = None
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
                from opencae.physics import physics_for
                ph = physics_for(p.analysis.analysis_type)
                if f.exists():
                    self.results = ph.load_results(f, self.mesh)
                else:
                    self.results = ph.read_results(self, self.data_dir / r.directory, r.jobname)
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
            st = self.study
            if not st.body_ids and (not st.mesh_settings.body or st.mesh_settings.body not in self.bodies):
                st.mesh_settings.body = feature.id
        self.regenerate()
        return feature

    def analysis_bodies(self) -> list[Body]:
        """Bodies analysed by the active study (all of them, in study order)."""
        st = self.study
        if st.body_ids:
            return [self.bodies[b] for b in st.body_ids if b in self.bodies]
        b = self.analysis_body()
        return [b] if b else []

    def analysis_body(self) -> Optional[Body]:
        """First (or only) analysis body of the active study."""
        st = self.study
        if st.body_ids:
            return next((self.bodies[b] for b in st.body_ids if b in self.bodies), None)
        bid = st.mesh_settings.body
        if bid in self.bodies:
            return self.bodies[bid]
        solids = [self.bodies[b] for b in self.regen.order if self.bodies[b].kind == "solid"]
        return solids[-1] if solids else None

    def import_geometry(self, file: Path, to_solid: bool = False, split: bool = False,
                        sew_tolerance: Optional[float] = None) -> Feature:
        """Import STEP/IGES/BREP/STL. ``split``: one body per solid (CAD) or per connected
        triangle shell (STL); parts are never joined and gaps are never filled automatically.
        ``sew_tolerance``: STL-to-solid sewing tolerance in model units (None = 1e-6 x size)."""
        from opencae.geometry.occ_kernel import copy_into_project
        rel = copy_into_project(Path(file), self.data_dir)
        prm = {"file": rel, "format": Path(file).suffix.lower().lstrip("."), "to_solid": to_solid}
        if split:
            prm["split"] = True
        if sew_tolerance:
            prm["sew_tolerance"] = float(sew_tolerance)
        f = Feature("import", prm, name=Path(file).stem)
        f = self.add_feature(f, Path(file).stem)
        if f.id not in self.bodies:  # split into several bodies: their ids are <feature id>#<k>
            parts = self.feature_bodies(f.id)
            for bid in parts:
                self.project.geometry.body_names.setdefault(bid, self.bodies[bid].name)
            st = self.study
            if parts and not st.body_ids and st.mesh_settings.body not in self.bodies:
                st.mesh_settings.body = parts[0]
            self.project.geometry.body_names.pop(f.id, None)
            self.regenerate()
        return f

    def create_flow_domain(self, body_ids: list[str], axis: str = "+x", speed: float = 1.0, upstream: float = 3.0,
                           downstream: float = 6.0, lateral: float = 2.0, name: str = "External flow",
                           symmetry: str = "") -> Study:
        """External-flow setup: an enclosure body (box minus the selected solids, which are kept)
        and a new flow study on it with a velocity inlet on the upstream face, a pressure outlet
        downstream, slip walls on the sides and no-slip walls on the solids."""
        from opencae.geometry.references import find_planar_face, make_ref
        f = self.add_feature(Feature("enclosure", {"bodies": list(body_ids), "axis": axis, "upstream": upstream,
                                                   "downstream": downstream, "lateral": lateral,
                                                   "symmetry": symmetry or ""},
                                     name="Flow domain"))
        if f.id in self.regen.errors:
            raise ValueError(self.regen.errors[f.id])
        dom = self.bodies[f.id]
        st = self.new_study(name, "cfd_incompressible", [f.id])
        st.mesh_settings.element_order = 1
        k = "xyz".index(axis[-1])
        sgn = -1.0 if axis.startswith("-") else 1.0
        lo, hi = dom.bbox
        def face(normal, point):
            return make_ref(dom, "face", find_planar_face(dom, normal, point))
        n_in = np.zeros(3)
        n_in[k] = -sgn
        p_in = lo.copy() if sgn > 0 else hi.copy()
        p_out = hi.copy() if sgn > 0 else lo.copy()
        sides, sym_faces = [], []
        for j in range(3):
            if j == k:
                continue
            for side, pt in ((-1, lo), (1, hi)):
                nv = np.zeros(3)
                nv[j] = side
                is_sym = bool(symmetry) and symmetry.lower()[-1] == "xyz"[j] and \
                    (symmetry.startswith("-") == (side < 0))
                (sym_faces if is_sym else sides).append(face(nv, pt))
        st.loads = [Load("velocity_inlet", [face(n_in, p_in)], {"magnitude": float(speed), "direction": "normal"},
                         name="Inlet"),
                    Load("pressure_outlet", [face(-n_in, p_out)], {"magnitude": 0.0}, name="Outlet")]
        st.constraints = [Constraint("slip_wall", sides, name="Far-field sides (slip)")]
        if sym_faces:
            st.constraints.append(Constraint("slip_wall", sym_faces, name="Symmetry plane"))
        return st

    def feature_bodies(self, feature_id: str) -> list[str]:
        """Bodies created by a feature (one, or several for split imports)."""
        return [b for b in self.regen.order if b == feature_id or b.startswith(feature_id + "#")]

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
    def _foam_mesh_job(self) -> dict:
        """OpenFOAM study: write the meshing case (versioned folder) and describe the job."""
        from opencae.cfd.openfoam import study as foam_study
        be = self.solver_backend()
        if be is None or not be.install:
            from opencae.cfd.openfoam.install import INSTALL_HELP
            raise ValueError("OpenFOAM not found. " + INSTALL_HELP.splitlines()[0])
        rel = Path("mesh") / f"foam_{uuid.uuid4().hex[:10]}"
        case = self.data_dir / rel
        setup, issues = foam_study.build(self, case)
        errs = [str(i) for i in issues if i.severity == ERROR]
        if errs or setup is None:
            shutil.rmtree(case, ignore_errors=True)
            raise ValueError("; ".join(errs) or "could not set up the OpenFOAM case")
        from dataclasses import asdict as _asdict
        return {"mode": "openfoam", "case": str(case), "case_rel": str(rel).replace("\\", "/"),
                "install": _asdict(be.install), "scale": setup.scale, "body_id": self.analysis_bodies()[0].id,
                "patches": [{"name": p.name, "kind": p.kind, "faces": p.faces} for p in setup.patches],
                "settings": asdict(self.project.mesh_settings),
                "out": str(self.data_dir / "mesh" / f"mesh_job_{self.project.active_study}.npz")}

    def foam_patch_signature(self) -> list:
        """Current grouping of fluid faces into OpenFOAM patches (kind, faces), to detect a mesh
        made with different flow conditions."""
        from opencae.cfd.openfoam.study import FLOW_KINDS
        bodies = self.analysis_bodies()
        if len(bodies) != 1:
            return []
        b = bodies[0]
        sig, used = [], set()
        for it in [c for c in self.project.constraints if not c.suppressed] + \
                [l for l in self.project.loads if not l.suppressed]:
            k = FLOW_KINDS.get(it.kind)
            if k:
                fs = sorted(r.index for r in it.targets if r.kind == "face" and r.body == b.id)
                if fs:
                    sig.append([k, fs])
                    used |= set(fs)
        free = sorted(f.index for f in b.faces if f.index not in used)
        if free:
            sig.append(["wall", free])
        return sorted(sig)

    def mesh_job(self) -> dict:
        if self.is_openfoam_study():
            return self._foam_mesh_job()
        """Prepare a meshing job (B-rep files + topology signatures + settings) for all bodies of
        the active study; several bodies are meshed as one bonded (conformal) mesh."""
        bodies = self.analysis_bodies()
        if not bodies:
            raise ValueError("No solid body to mesh.")
        for body in bodies:
            if body.kind != "solid" or not body.check.is_solid:
                raise ValueError(f"Body '{body.name}' is not a solid (tessellated or open); it cannot be meshed. "
                                 "Convert STL to solid or repair the geometry.")
            if not body.check.valid:
                raise ValueError(f"Body '{body.name}' failed the geometry validity check.")
        mdir = self.data_dir / "mesh"
        mdir.mkdir(parents=True, exist_ok=True)
        a = self.project.analysis
        if a.analysis_type.startswith("cfd") and a.cfd_dimension == "2d":
            bid, face = self.flow_face()
            body = self.bodies[bid]
            ms = self.project.mesh_settings
            ms.bl_edges = self.flow_wall_edges() if ms.bl_layers > 0 else []
            brep = self.kernel.write_face(body, face, mdir / "flow_face_2d.brep")
            return {"mode": "2d", "face_brep": str(brep), "face": face, "body_id": bid,
                    "topology": TopologySignatures.from_body(body).to_dict(),
                    "settings": asdict(self.project.mesh_settings),
                    "out": str(mdir / f"mesh_job_{self.project.active_study}.npz")}
        parts = []
        for k, body in enumerate(bodies):
            brep = self.kernel.write_native(body, mdir / f"analysis_body_{k}.brep")
            parts.append({"body_id": body.id, "brep": str(brep),
                          "topology": TopologySignatures.from_body(body).to_dict()})
        if not self.study.body_ids:
            self.project.mesh_settings.body = bodies[0].id
        return {"parts": parts, "brep": parts[0]["brep"], "topology": parts[0]["topology"],
                "settings": asdict(self.project.mesh_settings), "body_id": bodies[0].id,
                "out": str(mdir / f"mesh_job_{self.project.active_study}.npz")}

    def flow_wall_edges(self) -> list[int]:
        """2-D flow: edges of the flow face that are no-slip walls (explicit wall conditions or no
        condition at all) - where boundary layers are generated."""
        bid, face = self.flow_face()
        body = self.bodies[bid]
        edges = set(face_edge_indices(body, face))
        other = set()
        for it in self.project.constraints + self.project.loads:
            if it.suppressed or it.kind == "wall":
                continue
            other |= {t.index for t in it.targets if t.kind == "edge" and t.body == bid}
        return sorted(edges - other)

    def flow_wall_faces(self, body_id: str) -> list[int]:
        """3-D flow: faces of the fluid body that are no-slip walls (explicit wall conditions or no
        condition at all)."""
        body = self.bodies[body_id]
        other = set()
        for it in self.project.constraints + self.project.loads:
            if it.suppressed or it.kind == "wall":
                continue
            other |= {t.index for t in it.targets if t.kind == "face" and t.body == body_id}
        return [f.index for f in body.faces if f.index not in other]

    def flow_face(self) -> tuple[str, int]:
        """(body id, face index) of the planar face that defines a 2-D flow study."""
        ref = self.project.analysis.cfd_face
        if not ref or "|" not in ref:
            raise ValueError("2-D flow: choose the planar face that defines the flow plane "
                             "(Analysis settings > 2-D flow face).")
        bid, rest = ref.split("|", 1)
        kind, idx = rest.split(":")
        if bid not in self.bodies or bid not in self.study.analysis_body_ids():
            raise ValueError("2-D flow face belongs to a body that is not part of this study.")
        idx = int(idx)
        f = self.bodies[bid].faces[idx] if 0 <= idx < len(self.bodies[bid].faces) else None
        if f is None or f.normal is None:
            raise ValueError("2-D flow needs a planar face.")
        return bid, idx

    @staticmethod
    def run_mesh_job(job: dict, progress: Callable[[str], None] | None = None) -> FEMesh:
        from opencae.project.io import _from_dict
        if job.get("mode") == "openfoam":
            from opencae.cfd.openfoam.backend import run_mesh
            return run_mesh(job, progress)
        settings = _from_dict(MeshSettings, job["settings"])
        if job.get("mode") == "2d":
            from opencae.meshing.mesh2d import generate_mesh_2d
            return generate_mesh_2d(Path(job["face_brep"]), job["body_id"], int(job["face"]),
                                    TopologySignatures.from_dict(job["topology"]), settings, progress)
        parts = [MeshPart(p["body_id"], Path(p["brep"]), TopologySignatures.from_dict(p["topology"]))
                 for p in job.get("parts") or [{"body_id": job["body_id"], "brep": job["brep"],
                                                "topology": job["topology"]}]]
        return generate_mesh(None, settings=settings, progress=progress, parts=parts)

    def generate_mesh(self, progress: Callable[[str], None] | None = None) -> FEMesh:
        mesh = self.run_mesh_job(self.mesh_job(), progress)
        self.set_mesh(mesh)
        return mesh

    def set_mesh(self, mesh: FEMesh) -> None:
        mesh_id = uuid.uuid4().hex[:12]
        # versioned file name so undo/redo can restore an earlier mesh consistently
        path = mesh.save(self.data_dir / "mesh" / f"mesh_{mesh_id}.npz")
        self.mesh = mesh
        self.results = None
        self.project.mesh = MeshInfo(file=str(path.relative_to(self.data_dir)), mesh_id=mesh_id,
                                     geometry_hash=self.project.geometry_hash_for(scope="bodies"),
                                     settings_hash=self.project.mesh_settings.settings_hash(), stats=mesh.stats(),
                                     geometry_scope="bodies")
        self.project.results = ResultsInfo()

    # ------------------------------------------------------------------ validation
    def validate(self) -> tuple[list[Issue], Optional[ResolvedModel]]:
        issues: list[Issue] = []

        def add(sev, msg, src):
            issues.append(Issue(sev, msg, src))

        p = self.project
        st = self.study
        # 1 geometry
        for fid, msg in self.regen.errors.items():
            add(ERROR, msg, "Geometry")
        bodies = self.analysis_bodies()
        if not bodies:
            add(ERROR, "no solid body selected for this study", "Geometry")
            return issues, None
        missing = [b for b in st.body_ids if b not in self.bodies]
        if missing:
            add(ERROR, f"study refers to {len(missing)} body/bodies that no longer exist", "Geometry")
        for body in bodies:
            if body.kind != "solid" or not body.check.is_solid:
                add(ERROR, f"analysis body '{body.name}' is not an exact solid", "Geometry")
            elif not body.check.valid or not body.check.closed:
                add(ERROR, f"analysis body '{body.name}' is invalid or not closed: {body.check.issues}", "Geometry")
        size = max(b.size for b in bodies)
        if self.units.length == "mm" and size < 0.1:
            add(WARNING, f"model is only {size:.3g} mm in size - check the unit system", "Units")
        if self.units.length == "m" and size > 100:
            add(WARNING, f"model is {size:.3g} m in size - check the unit system", "Units")
        # 2 mesh
        mesh = self.mesh
        ids = [b.id for b in bodies]
        mesh_ok = mesh is not None and list(mesh.body_ids) == ids
        if mesh is None:
            add(ERROR, "no mesh - generate the mesh", "Mesh")
        else:
            if not p.mesh_is_current():
                add(ERROR, "mesh is out of date with geometry or mesh settings - regenerate the mesh", "Mesh")
            if not mesh_ok:
                add(ERROR, "mesh was generated for a different body selection - regenerate the mesh", "Mesh")
            foam_mesh = getattr(mesh, "source", "gmsh") == "openfoam"
            if foam_mesh != self.is_openfoam_study():
                add(ERROR, "the mesh was made for a different solver (OpenFOAM / CalculiX-SU2) - regenerate the "
                           "mesh", "Mesh")
            elif foam_mesh:
                meta = mesh.meta or {}
                if meta.get("patches") != self.foam_patch_signature():
                    add(ERROR, "the flow conditions changed since the OpenFOAM mesh was made (its patches no longer "
                               "match) - regenerate the mesh", "Mesh")
                if not (self.data_dir / meta.get("case_rel", "?") / "constant" / "polyMesh").exists():
                    add(ERROR, "the OpenFOAM mesh folder is missing - regenerate the mesh", "Mesh")
                for w in meta.get("check_warnings", []):
                    add(WARNING, w, "Mesh")
            if foam_mesh:
                pass
            elif mesh.element_type not in SUPPORTED_ELEMENTS:
                add(ERROR, f"element type {mesh.element_type} not supported", "Mesh")
            planar = mesh.element_type in ("F2D3", "F2D4")
            flow2d = p.analysis.analysis_type.startswith("cfd") and p.analysis.cfd_dimension == "2d"
            if planar and not flow2d:
                add(ERROR, "the mesh is a 2-D (planar) flow mesh; this study needs a volume mesh - regenerate it",
                    "Mesh")
            elif flow2d and not planar:
                add(ERROR, "2-D flow needs a planar mesh of the flow face - regenerate the mesh", "Mesh")
            for w in ([] if foam_mesh else mesh.warnings(p.mesh_settings.quality_threshold)):
                add(ERROR if "non-positive Jacobian" in w else WARNING, w, "Mesh")
        # 3 materials (one per body; flow studies use the fluid properties of the analysis instead)
        flow = p.analysis.analysis_type.startswith("cfd")
        for body in ([] if flow else bodies):
            mat = p.material_for_body(body.id)
            if mat is None:
                add(ERROR, f"no material assigned to '{body.name}'", "Material")
                continue
            for e in mat.validate():
                add(ERROR, e, "Material")
            if mat.is_example:
                add(WARNING, f"'{mat.name}' uses example property values - verify before engineering use",
                    "Material")
        # 4 analysis
        if p.analysis.analysis_type not in SUPPORTED_ANALYSES:
            add(ERROR, f"analysis type '{ANALYSIS_TYPES.get(p.analysis.analysis_type, p.analysis.analysis_type)}' "
                       "is not available with the configured solver", "Analysis")
        be = self.solver_backend()
        if be is self.backend:
            if not self.backend.executable():
                add(ERROR, "CalculiX (ccx) not found - " + INSTALL_HELP.splitlines()[0], "Solver")
            elif self.backend.verified is False:
                add(ERROR, "CalculiX failed its self-test, so it cannot run here. Open Tools > Settings > "
                           "Test Solver for the cause (e.g. a missing DLL).", "Solver")
        elif self.is_openfoam_study():
            if be is None or not be.install:
                from opencae.cfd.openfoam.install import INSTALL_HELP as FOAM_HELP
                add(ERROR, "OpenFOAM not found - " + FOAM_HELP.splitlines()[0] + " See Tools > Settings.", "Solver")
            elif not be.supported():
                add(ERROR, be.status_text(), "Solver")
        elif be is None or not be.executable():
            from opencae.cfd.su2 import INSTALL_HELP as SU2_HELP
            add(ERROR, "SU2 (SU2_CFD) not found - flow studies need it. " + SU2_HELP.splitlines()[0] +
                " See Tools > Settings.", "Solver")
        elif be.verified is False:
            add(ERROR, f"{be.name} failed its self-test (Tools > Settings > Test {be.name}).", "Solver")
        # 5 physics-specific setup (supports, loads, thermal conditions...)
        rm = None
        if mesh_ok:
            from opencae.physics import physics_for
            rm = physics_for(p.analysis.analysis_type).resolve(self, bodies, mesh)
            issues.extend(rm.issues)
        else:
            if not p.constraints and not p.loads:
                add(ERROR, "no supports or loads defined", "Constraints")
        return issues, rm

    # ------------------------------------------------------------------ solving
    def body_materials(self) -> dict[int, object]:
        """Material per body slot of the current mesh, as used by the solver: effective
        (manufacturing-aware) properties where print settings are given, and temperature-
        dependent data evaluated at the operating temperature (thermal stress uses the table)."""
        a = self.project.analysis
        out = {}
        for k, b in enumerate(self.mesh.body_ids):
            m = self.project.material_for_body(b)
            if m is not None:
                m = self.effective_material(b, m)
                if a.analysis_type != "thermal_stress":
                    m = m.at_temperature(a.operating_temperature)
            out[k] = m
        return out

    def effective_material(self, body_id: str, material):
        """Manufacturing-aware material of a body (identity until print settings are defined)."""
        ps = (self.project.study.print_settings or {}).get(body_id)
        if not ps:
            return material
        from opencae.manufacturing.effective import effective_material
        return effective_material(material, ps, self.shell_fraction(body_id, ps))

    def shell_fraction(self, body_id: str, ps) -> float | None:
        """Volume fraction of the printed shell (wall thickness = perimeters x line width), from an
        inward offset of the body. Cached per geometry and thickness; None if the offset fails."""
        from opencae.manufacturing.print_settings import PrintSettings
        s = ps if isinstance(ps, PrintSettings) else PrintSettings.from_dict(ps)
        if s.infill_percent >= 100.0 or body_id not in self.bodies:
            return 1.0 if s.infill_percent >= 100.0 else None
        key = (self.project.geometry.bodies_hash([body_id]), round(s.wall_thickness, 9))
        cache = self.__dict__.setdefault("_shell_cache", {})
        if key not in cache:
            from opencae.geometry.occ_kernel import _volume, inner_offset_volume
            b = self.bodies[body_id]
            try:
                full = _volume(b.shape)
                cache[key] = 1.0 - inner_offset_volume(b, s.wall_thickness) / full
            except Exception as exc:  # noqa: BLE001 - reported as unknown shell fraction
                log.warning("shell fraction of %s: %s", b.name, exc)
                cache[key] = None
        return cache[key]

    def prepare_solve(self) -> tuple[Path, str, list[Issue]]:
        issues, rm = self.validate()
        if any(i.severity == ERROR for i in issues) or rm is None:
            return Path(), "", issues
        from opencae.physics import physics_for
        job_dir = self.data_dir / "results" / f"run_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        jobname = "job"
        job_dir.mkdir(parents=True, exist_ok=True)
        physics_for(self.project.analysis.analysis_type).write_input(self, job_dir, jobname, rm)
        return job_dir, jobname, issues

    def run_solver(self, on_line: Callable[[str], None] | None = None,
                   cancel: Callable[[], bool] | None = None) -> tuple[SolveOutcome | None, list[Issue]]:
        job_dir, jobname, issues = self.prepare_solve()
        if not jobname:
            return None, issues
        be = self.solver_backend()
        if be is self.backend:
            outcome = run_blocking(self.backend.install, job_dir, jobname, self.project.analysis.threads,
                                   on_line, cancel)
        else:
            from opencae.solver.runner_generic import run_backend_blocking
            outcome = run_backend_blocking(be, job_dir, jobname, self.project.analysis.threads, on_line, cancel)
        if outcome.success:
            self.load_results(job_dir, jobname, outcome.wall_time)
        else:
            self.project.results = ResultsInfo()
            self.results = None
        return outcome, issues

    def load_results(self, job_dir: Path, jobname: str, wall_time: float = 0.0):
        from opencae.physics import physics_for
        rs = physics_for(self.project.analysis.analysis_type).read_results(self, Path(job_dir), jobname)
        rs.save(Path(job_dir) / "results.npz")
        self.results = rs
        summary = rs.summary()
        try:
            rl = json.loads((Path(job_dir) / "resolved_loads.json").read_text())
            summary.update(rl)
        except (OSError, KeyError, json.JSONDecodeError):
            pass
        self.project.results = ResultsInfo(directory=str(Path(job_dir).relative_to(self.data_dir)), jobname=jobname,
                                           mesh_id=self.project.mesh.mesh_id, setup_hash=self.project.setup_hash(),
                                           completed=True,
                                           solver_version=f"{self.solver_backend().name} "
                                                          f"{self.solver_backend().version()}",
                                           wall_time_s=wall_time, summary=summary,
                                           finished_at=time.strftime("%Y-%m-%d %H:%M:%S"))
        return rs

    # ------------------------------------------------------------------ workflow status
    def validate_cached(self) -> tuple[list[Issue], Optional[ResolvedModel]]:
        """validate() memoised on everything that affects it (cheap to call on every refresh)."""
        key = (self.project.geometry.revision_hash(), self.project.setup_hash(), self.project.mesh.mesh_id,
               self.project.mesh_settings.settings_hash(), self.project.mesh_settings.body,
               bool(self.solver_backend() and self.solver_backend().executable()),
               self.solver_backend().verified if self.solver_backend() else None)
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
        elif p.analysis.analysis_type.startswith("cfd"):
            a = p.analysis
            ok = a.fluid_density > 0 and a.fluid_viscosity > 0
            out.append(("Materials", "completed" if ok else "invalid",
                        f"fluid: {a.fluid_name} (ρ = {a.fluid_density:g} kg/m³, μ = {a.fluid_viscosity:g} Pa·s)"))
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
                        (f"{self.solver_backend().name if self.solver_backend() else 'solver'} not found"
                         if not (self.solver_backend() and self.solver_backend().executable())
                         else "fix validation errors first")))
            out.append(("Results", "not_started", ""))
        rep = self.last_report
        out.append(("Report", "completed" if rep and out[-1][1] == "completed" else "not_started",
                    rep.name if rep else ""))
        return out
