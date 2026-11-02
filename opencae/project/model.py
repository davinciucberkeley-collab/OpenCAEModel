"""Project data model.

Pure data classes (no Qt, no OCC, no Gmsh). Everything the user defines lives
here and is serialised to the project JSON file. Heavy data (B-rep, mesh,
results) is stored in separate files referenced from this model.

Geometry is *parametric*: a project stores an ordered list of features that is
replayed by the geometry kernel to regenerate bodies. Loads and constraints
refer to topology through :class:`EntityRef`, which stores both an index and a
geometric signature so references can be re-validated after regeneration.
"""
from __future__ import annotations

import copy
import hashlib
import json
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from opencae.utilities.units import DEFAULT_UNIT_SYSTEM


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def stable_hash(obj: Any) -> str:
    return hashlib.sha1(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:16]


# --------------------------------------------------------------------------- geometry
FEATURE_KINDS = ("primitive", "import", "transform", "boolean", "fillet", "chamfer", "extrude")


@dataclass
class Feature:
    """One step of the parametric feature history.

    kind / params:

    * ``primitive``: shape in {box, cylinder, sphere, cone, plate, beam,
      cantilever_beam} plus dimensions and ``position`` [x, y, z], ``axis``.
    * ``import``: ``file`` (path relative to project data dir), ``format``.
    * ``transform``: ``body``, ``translate``, ``rotate_axis``, ``rotate_angle``
      (deg), ``rotate_center``, ``scale`` (uniform), ``scale_center``.
    * ``boolean``: ``target``, ``tool``, ``op`` in {union, subtract, intersect}.
      The tool body is consumed.
    * ``fillet`` / ``chamfer``: ``body``, ``radius`` / ``distance``,
      ``edges`` ("all" or list of edge indices).
    * ``extrude``: ``profile`` in {rectangle, circle, polygon}, profile params,
      ``origin``, ``normal``, ``x_dir``, ``distance``; ``fuse_with`` optional body.
    """

    kind: str
    params: dict[str, Any]
    name: str = ""
    id: str = field(default_factory=lambda: new_id("feat"))
    suppressed: bool = False

    def creates_body(self) -> bool:
        if self.kind in ("primitive", "import"):
            return True
        if self.kind == "extrude" and not self.params.get("fuse_with"):
            return True
        return False


@dataclass
class GeometryModel:
    features: list[Feature] = field(default_factory=list)
    body_names: dict[str, str] = field(default_factory=dict)
    hidden_bodies: list[str] = field(default_factory=list)

    def revision_hash(self) -> str:
        return stable_hash([asdict(f) for f in self.features if not f.suppressed])


@dataclass
class EntityRef:
    """Reference to a topological entity of a body (or a mesh node).

    ``index`` is the 0-based position in the kernel's deterministic topology
    exploration order. ``signature`` (area/length, centroid, normal) is used to
    re-validate the reference after regeneration.
    """

    kind: str  # face | edge | vertex | body
    body: str
    index: int = -1
    signature: dict[str, Any] = field(default_factory=dict)

    def label(self) -> str:
        return f"{self.kind.capitalize()} {self.index + 1}" if self.index >= 0 else self.kind


# --------------------------------------------------------------------------- materials
@dataclass
class Material:
    """Isotropic linear-elastic material. Values in base SI units."""

    name: str
    youngs_modulus: float  # Pa
    poissons_ratio: float
    density: float  # kg/m^3
    yield_strength: Optional[float] = None  # Pa
    thermal_expansion: Optional[float] = None  # 1/K
    id: str = field(default_factory=lambda: new_id("mat"))
    is_example: bool = True
    notes: str = ""

    def validate(self) -> list[str]:
        errs = []
        if not self.name.strip():
            errs.append("Material name is empty.")
        if not (self.youngs_modulus > 0):
            errs.append(f"{self.name}: Young's modulus must be > 0.")
        if not (-1.0 < self.poissons_ratio < 0.5):
            errs.append(f"{self.name}: Poisson's ratio must satisfy -1 < nu < 0.5.")
        if self.density is not None and self.density < 0:
            errs.append(f"{self.name}: density must be >= 0.")
        if self.yield_strength is not None and self.yield_strength <= 0:
            errs.append(f"{self.name}: yield strength must be > 0 if given.")
        return errs


@dataclass
class MaterialAssignment:
    body: str
    material: str  # material id


# --------------------------------------------------------------------------- mesh
@dataclass
class MeshRefinement:
    targets: list[EntityRef]
    size: float
    id: str = field(default_factory=lambda: new_id("ref"))
    name: str = "Local refinement"


@dataclass
class MeshSettings:
    body: str = ""  # body to mesh (analysis body)
    element_order: int = 2  # 1 -> C3D4, 2 -> C3D10
    size_max: float = 0.0  # 0 = automatic (bbox diagonal / 20)
    size_min: float = 0.0
    curvature_points: int = 12  # elements per 2*pi of curvature, 0 = off
    optimize: bool = True
    refinements: list[MeshRefinement] = field(default_factory=list)

    def settings_hash(self) -> str:
        return stable_hash(asdict(self))


@dataclass
class MeshInfo:
    file: str = ""  # relative path to .npz
    mesh_id: str = ""  # unique id of this generated mesh
    geometry_hash: str = ""
    settings_hash: str = ""
    stats: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------- BC / loads
CONSTRAINT_KINDS = ("fixed", "displacement", "symmetry")
LOAD_KINDS = ("force", "pressure", "gravity")


@dataclass
class Constraint:
    """Boundary condition.

    * ``fixed``: all translational DOF zero on targets.
    * ``displacement``: params ``ux``, ``uy``, ``uz`` - each a number (prescribed,
      0 = fixed in that direction) or ``None`` (free).
    * ``symmetry``: zero normal displacement on a planar, axis-aligned face.
    """

    kind: str
    targets: list[EntityRef]
    params: dict[str, Any] = field(default_factory=dict)
    name: str = ""
    id: str = field(default_factory=lambda: new_id("bc"))
    suppressed: bool = False


@dataclass
class Load:
    """Applied load.

    * ``force``: total force vector ``magnitude`` * ``direction`` (global CS)
      applied to faces as a uniform traction (consistent nodal loads), or to a
      vertex as a concentrated nodal force. ``coordinate_system`` is
      ``global``; ``face_normal`` uses the (planar) face outward normal.
    * ``pressure``: ``magnitude`` (positive = pushes into the face, i.e. opposite
      to the outward normal) on faces.
    * ``gravity``: acceleration ``magnitude`` along ``direction`` on the body.
    """

    kind: str
    targets: list[EntityRef]
    params: dict[str, Any] = field(default_factory=dict)
    name: str = ""
    id: str = field(default_factory=lambda: new_id("load"))
    suppressed: bool = False


# --------------------------------------------------------------------------- analysis
@dataclass
class AnalysisSettings:
    analysis_type: str = "linear_static"
    solver: str = "calculix"
    threads: int = 2
    write_ip_stress: bool = True  # *EL PRINT of integration-point stresses
    title: str = "Linear static analysis"


@dataclass
class ResultsInfo:
    directory: str = ""  # relative to project data dir
    jobname: str = ""
    mesh_id: str = ""
    setup_hash: str = ""
    completed: bool = False
    solver_version: str = ""
    wall_time_s: float = 0.0
    summary: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------- project
@dataclass
class Project:
    name: str = "Untitled"
    unit_system: str = DEFAULT_UNIT_SYSTEM
    geometry: GeometryModel = field(default_factory=GeometryModel)
    materials: list[Material] = field(default_factory=list)
    assignments: list[MaterialAssignment] = field(default_factory=list)
    mesh_settings: MeshSettings = field(default_factory=MeshSettings)
    mesh: MeshInfo = field(default_factory=MeshInfo)
    constraints: list[Constraint] = field(default_factory=list)
    loads: list[Load] = field(default_factory=list)
    analysis: AnalysisSettings = field(default_factory=AnalysisSettings)
    results: ResultsInfo = field(default_factory=ResultsInfo)
    description: str = ""

    # ---- helpers -------------------------------------------------------
    def clone(self) -> "Project":
        return copy.deepcopy(self)

    def material_by_id(self, mid: str) -> Optional[Material]:
        return next((m for m in self.materials if m.id == mid), None)

    def material_for_body(self, body: str) -> Optional[Material]:
        a = next((a for a in self.assignments if a.body == body), None)
        return self.material_by_id(a.material) if a else None

    def assign_material(self, body: str, material_id: str) -> None:
        self.assignments = [a for a in self.assignments if a.body != body]
        self.assignments.append(MaterialAssignment(body=body, material=material_id))

    def setup_hash(self) -> str:
        """Hash of everything that affects the solver input (except the mesh)."""
        return stable_hash({
            "units": self.unit_system,
            "materials": [asdict(m) for m in self.materials],
            "assign": [asdict(a) for a in self.assignments],
            "bc": [asdict(c) for c in self.constraints if not c.suppressed],
            "loads": [asdict(l) for l in self.loads if not l.suppressed],
            "analysis": asdict(self.analysis),
        })

    def mesh_is_current(self) -> bool:
        return bool(self.mesh.file) and self.mesh.geometry_hash == self.geometry.revision_hash() \
            and self.mesh.settings_hash == self.mesh_settings.settings_hash()

    def results_are_current(self) -> bool:
        r = self.results
        return r.completed and self.mesh_is_current() and r.mesh_id == self.mesh.mesh_id \
            and r.setup_hash == self.setup_hash()
