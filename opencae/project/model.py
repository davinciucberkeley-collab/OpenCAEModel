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
FEATURE_KINDS = ("primitive", "import", "transform", "boolean", "fillet", "chamfer", "extrude", "enclosure")


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
        if self.kind in ("primitive", "import", "enclosure"):
            return True
        if self.kind == "extrude" and not self.params.get("fuse_with"):
            return True
        return False


@dataclass
class GeometryModel:
    features: list[Feature] = field(default_factory=list)
    body_names: dict[str, str] = field(default_factory=dict)
    hidden_bodies: list[str] = field(default_factory=list)
    assemblies: dict[str, list[str]] = field(default_factory=dict)  # assembly name -> body ids

    def revision_hash(self) -> str:
        return stable_hash([asdict(f) for f in self.features if not f.suppressed])

    def contributing_features(self, body_ids: list[str]) -> list[Feature]:
        """Features that affect the given bodies (creation, edits, consumed boolean tools,
        transitively), in history order."""
        affects: dict[str, list[int]] = {}
        active = [f for f in self.features if not f.suppressed]
        for i, f in enumerate(active):
            p = f.params
            if f.creates_body():
                affects[f.id] = [i]
            elif f.kind == "boolean":
                t, tool = p.get("target"), p.get("tool")
                affects[t] = affects.get(t, []) + affects.get(tool, []) + [i]
            elif f.kind == "extrude" and p.get("fuse_with"):
                affects[p["fuse_with"]] = affects.get(p["fuse_with"], []) + [i]
            else:
                b = p.get("body")
                if b:
                    affects[b] = affects.get(b, []) + [i]
        idx = sorted({i for b in body_ids for i in affects.get(b, [])})
        return [active[i] for i in idx]

    def bodies_hash(self, body_ids: list[str]) -> str:
        """Revision hash restricted to the given bodies: edits to other components do not
        invalidate meshes/results of a study that does not use them."""
        return stable_hash({"bodies": sorted(body_ids),
                            "features": [asdict(f) for f in self.contributing_features(body_ids)]})


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
    thermal_conductivity: Optional[float] = None  # W/(m K)
    specific_heat: Optional[float] = None  # J/(kg K)

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
        if self.thermal_conductivity is not None and self.thermal_conductivity <= 0:
            errs.append(f"{self.name}: thermal conductivity must be > 0 if given.")
        if self.specific_heat is not None and self.specific_heat <= 0:
            errs.append(f"{self.name}: specific heat must be > 0 if given.")
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


def hash_without_defaults(obj, added_defaults: dict[str, Any]) -> str:
    """Hash of a dataclass that ignores fields added in later versions while they keep
    their default value, so adding a setting does not invalidate existing meshes/results."""
    d = asdict(obj)
    for k, v in added_defaults.items():
        if d.get(k) == v:
            d.pop(k, None)
    return stable_hash(d)


@dataclass
class MeshSettings:
    body: str = ""  # body to mesh (analysis body)
    element_order: int = 2  # 1 -> linear, 2 -> quadratic
    size_max: float = 0.0  # 0 = automatic (bbox diagonal / 20)
    size_min: float = 0.0
    curvature_points: int = 12  # elements per 2*pi of curvature, 0 = off
    optimize: bool = True
    refinements: list[MeshRefinement] = field(default_factory=list)
    # --- added in 0.2 (defaults reproduce 0.1 behaviour)
    element_shape: str = "tet"  # tet | hex (hex: structured/transfinite volumes only)
    sizing_mode: str = "size"  # size | target_count
    target_elements: int = 0  # requested number of volume elements (target_count mode)
    target_tolerance: float = 0.1  # accepted relative deviation from the target count
    algorithm_2d: int = 6  # Gmsh: 1 MeshAdapt, 5 Delaunay, 6 Frontal-Delaunay, 8 Frontal-Quad
    algorithm_3d: int = 1  # Gmsh: 1 Delaunay, 4 Frontal, 10 HXT
    growth_rate: float = 0.0  # size growth away from refinements (0 = 0.1 behaviour)
    smoothing_steps: int = 1
    quality_threshold: float = 0.1  # warn below this quality (gamma for tets, SICN for hexes)
    hex_formulation: str = "enhanced"  # enhanced: C3D8I / C3D20R; standard: C3D8 / C3D20
    # boundary layers (2-D flow meshes): structured layers on wall edges
    bl_layers: int = 0  # 0 = off
    bl_first: float = 0.0  # first-layer thickness (project length), 0 = automatic
    bl_growth: float = 1.2
    bl_edges: list = field(default_factory=list)  # CAD edge indices of the flow face that get layers (set by the session)
    # several bodies: "conformal" = fragmented, shared nodes (bonded); "separate" = each body meshed on its
    # own, connected by tie / contact interactions
    assembly_mesh: str = "conformal"

    _ADDED = {"element_shape": "tet", "sizing_mode": "size", "target_elements": 0, "target_tolerance": 0.1,
              "algorithm_2d": 6, "algorithm_3d": 1, "growth_rate": 0.0, "smoothing_steps": 1,
              "quality_threshold": 0.1, "hex_formulation": "enhanced", "bl_layers": 0, "bl_first": 0.0,
              "bl_growth": 1.2, "bl_edges": [], "assembly_mesh": "conformal"}

    def settings_hash(self) -> str:
        return hash_without_defaults(self, self._ADDED)

    def solver_element(self, mesh_type: str) -> str:
        """CalculiX element name used in the deck for a mesh of topology ``mesh_type``."""
        if mesh_type == "C3D8":
            return "C3D8I" if self.hex_formulation == "enhanced" else "C3D8"
        if mesh_type == "C3D20":
            return "C3D20R" if self.hex_formulation == "enhanced" else "C3D20"
        return mesh_type


@dataclass
class MeshInfo:
    file: str = ""  # relative path to .npz
    mesh_id: str = ""  # unique id of this generated mesh
    geometry_hash: str = ""
    settings_hash: str = ""
    stats: dict[str, Any] = field(default_factory=dict)
    geometry_scope: str = "all"  # "all": geometry_hash = whole-model hash (0.1); "bodies": per-body hash


# --------------------------------------------------------------------------- BC / loads
CONSTRAINT_KINDS = ("fixed", "displacement", "symmetry", "temperature", "wall", "slip_wall")
LOAD_KINDS = ("velocity_inlet", "pressure_outlet", "force", "remote_force", "moment", "pressure", "gravity", "uniform_temperature", "heat_flux", "heat_power", "convection",
              "radiation", "heat_generation")


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
    * ``moment``: moment ``magnitude`` about the axis ``direction`` (right-hand rule) through
      the area centroid of the selected faces, applied as linearly varying nodal forces
      with zero net force (statically exact).
    * ``remote_force``: force ``magnitude`` * ``direction`` acting at ``point`` (e.g. a motor
      thrust line), transferred to the selected faces as a uniform traction plus the moment
      of the offset (statically equivalent; the faces are not stiffened).
    """

    kind: str
    targets: list[EntityRef]
    params: dict[str, Any] = field(default_factory=dict)
    name: str = ""
    id: str = field(default_factory=lambda: new_id("load"))
    suppressed: bool = False


@dataclass
class Interaction:
    """Connection between faces of two bodies meshed separately (Mesh controls > Assembly mesh =
    separate).

    * ``bonded``: tied (CalculiX *TIE): the slave faces follow the master faces; works with
      non-matching meshes.
    * ``contact``: surface-to-surface contact (penalty), ``friction`` coefficient (0 = frictionless),
      ``stiffness`` penalty slope (0 = automatic). Contact makes the analysis nonlinear.
    """

    kind: str
    master: list[EntityRef]
    slave: list[EntityRef]
    params: dict[str, Any] = field(default_factory=dict)
    name: str = ""
    id: str = field(default_factory=lambda: new_id("int"))
    suppressed: bool = False


# --------------------------------------------------------------------------- analysis
ANALYSIS_TYPES = {
    "linear_static": "Static Structural (linear)",
    "modal": "Modal (natural frequencies)",
    "buckling": "Linear Buckling (eigenvalue)",
    "thermal_steady": "Steady-State Thermal",
    "thermal_transient": "Transient Thermal",
    "thermal_stress": "Thermal Stress (static, from a thermal study)",
    "cfd_incompressible": "Incompressible Flow (CFD, SU2)",
}


@dataclass
class AnalysisSettings:
    analysis_type: str = "linear_static"
    solver: str = "calculix"
    threads: int = 2
    write_ip_stress: bool = True  # *EL PRINT of integration-point stresses
    title: str = "Linear static analysis"
    # --- added in 0.2
    n_modes: int = 10  # modal
    freq_min: float = 0.0  # modal: lower bound of the frequency range (Hz), 0 = none
    time_total: float = 100.0  # transient thermal: total time (s)
    time_step: float = 1.0  # transient thermal: initial/fixed increment (s)
    initial_temperature: float = 20.0  # thermal: initial / reference temperature
    temperature_source_study: str = ""  # thermal stress: study id providing the temperature field
    reference_temperature: float = 20.0  # thermal stress: stress-free temperature
    nonlinear_geometry: bool = False  # NLGEOM (large deformation); off = small-displacement theory
    # --- CFD (SI units: kg/m^3, Pa s, m/s, Pa)
    fluid_name: str = "Air (20 °C)"
    fluid_density: float = 1.204
    fluid_viscosity: float = 1.813e-5
    flow_model: str = "laminar"  # laminar | sa (Spalart-Allmaras) | sst (k-omega SST)
    cfd_iterations: int = 2000
    cfd_cfl: float = 100.0
    cfd_residual: float = -6.0  # log10 of the residual drop (relative to the start) required for convergence
    turbulence_intensity: float = 0.05
    turbulent_viscosity_ratio: float = 10.0
    cfd_dimension: str = "3d"  # 3d | 2d (planar)
    cfd_face: str = ""  # 2d: "<body id>|face:<index>" defining the planar domain
    ref_area: float = 0.0  # force coefficients: reference area in m^2 (0 = report forces only)
    ref_velocity: float = 0.0  # m/s (0 = largest inlet speed)

    _ADDED = {"n_modes": 10, "freq_min": 0.0, "time_total": 100.0, "time_step": 1.0,
              "initial_temperature": 20.0, "temperature_source_study": "", "reference_temperature": 20.0,
              "nonlinear_geometry": False, "fluid_name": "Air (20 °C)", "fluid_density": 1.204,
              "fluid_viscosity": 1.813e-5, "flow_model": "laminar", "cfd_iterations": 2000, "cfd_cfl": 100.0,
              "cfd_residual": -6.0, "turbulence_intensity": 0.05, "turbulent_viscosity_ratio": 10.0,
              "cfd_dimension": "3d", "cfd_face": "", "ref_area": 0.0, "ref_velocity": 0.0}

    def settings_dict(self) -> dict:
        d = asdict(self)
        for k, v in self._ADDED.items():
            if d.get(k) == v:
                d.pop(k)
        return d


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
    finished_at: str = ""  # ISO time of the solve


def _material_hash_dict(m: "Material") -> dict:
    d = asdict(m)
    for k in ("thermal_conductivity", "specific_heat"):  # added in 0.2
        if d.get(k) is None:
            d.pop(k, None)
    return d


# --------------------------------------------------------------------------- studies
@dataclass
class Study:
    """One analysis: its own bodies, material assignments, mesh, supports, loads,
    solver settings and results. A project holds any number of studies."""

    name: str = "Static Structural"
    id: str = field(default_factory=lambda: new_id("study"))
    body_ids: list[str] = field(default_factory=list)  # empty: the single body in mesh_settings.body
    assignments: list[MaterialAssignment] = field(default_factory=list)
    mesh_settings: MeshSettings = field(default_factory=MeshSettings)
    mesh: MeshInfo = field(default_factory=MeshInfo)
    constraints: list[Constraint] = field(default_factory=list)
    loads: list[Load] = field(default_factory=list)
    analysis: AnalysisSettings = field(default_factory=AnalysisSettings)
    interactions: list[Interaction] = field(default_factory=list)  # tie / contact between bodies
    results: ResultsInfo = field(default_factory=ResultsInfo)
    notes: str = ""  # engineering observations / conclusions (shown in reports)

    def analysis_body_ids(self) -> list[str]:
        if self.body_ids:
            return list(self.body_ids)
        return [self.mesh_settings.body] if self.mesh_settings.body else []


_STUDY_PROPS = ("assignments", "mesh_settings", "mesh", "constraints", "loads", "analysis", "results", "interactions")


# --------------------------------------------------------------------------- project
@dataclass
class Project:
    name: str = "Untitled"
    unit_system: str = DEFAULT_UNIT_SYSTEM
    geometry: GeometryModel = field(default_factory=GeometryModel)
    materials: list[Material] = field(default_factory=list)
    studies: list[Study] = field(default_factory=list)
    active_study: str = ""
    description: str = ""

    def __init__(self, name: str = "Untitled", unit_system: str = DEFAULT_UNIT_SYSTEM,
                 geometry: Optional[GeometryModel] = None, materials: Optional[list[Material]] = None,
                 studies: Optional[list[Study]] = None, active_study: str = "", description: str = "",
                 **study_fields: Any) -> None:
        unknown = set(study_fields) - set(_STUDY_PROPS)
        if unknown:
            raise TypeError(f"Unknown Project fields: {sorted(unknown)}")
        self.name, self.unit_system = name, unit_system
        self.geometry = geometry if geometry is not None else GeometryModel()
        self.materials = materials if materials is not None else []
        self.studies = studies if studies else [Study(**study_fields)]
        self.active_study = active_study if any(s.id == active_study for s in self.studies) else self.studies[0].id
        self.description = description

    # ---- active study -----------------------------------------------
    @property
    def study(self) -> Study:
        for s in self.studies:
            if s.id == self.active_study:
                return s
        self.active_study = self.studies[0].id
        return self.studies[0]

    def study_by_id(self, sid: str) -> Optional[Study]:
        return next((s for s in self.studies if s.id == sid), None)

    # ---- helpers -------------------------------------------------------
    def clone(self) -> "Project":
        return copy.deepcopy(self)

    def material_by_id(self, mid: str) -> Optional[Material]:
        return next((m for m in self.materials if m.id == mid), None)

    def material_for_body(self, body: str, study: Optional[Study] = None) -> Optional[Material]:
        st = study or self.study
        a = next((a for a in st.assignments if a.body == body), None)
        return self.material_by_id(a.material) if a else None

    def assign_material(self, body: str, material_id: str, study: Optional[Study] = None) -> None:
        st = study or self.study
        st.assignments = [a for a in st.assignments if a.body != body]
        st.assignments.append(MaterialAssignment(body=body, material=material_id))

    def setup_hash(self, study: Optional[Study] = None) -> str:
        """Hash of everything that affects the solver input of a study (except the mesh)."""
        st = study or self.study
        d = {
            "units": self.unit_system,
            "materials": [_material_hash_dict(m) for m in self.materials],
            "assign": [asdict(a) for a in st.assignments],
            "bc": [asdict(c) for c in st.constraints if not c.suppressed],
            "loads": [asdict(l) for l in st.loads if not l.suppressed],
            "analysis": st.analysis.settings_dict(),
        }
        if st.body_ids:
            d["bodies"] = sorted(st.body_ids)
        if st.interactions:
            d["interactions"] = [asdict(i) for i in st.interactions if not i.suppressed]
        src = self.study_by_id(st.analysis.temperature_source_study) if st.analysis.analysis_type == "thermal_stress" else None
        if src is not None and src is not st:  # thermal stress depends on the temperature results
            # stored temperature results and the source's *current* setup: editing the thermal
            # study makes the stress results out of date even before it is re-solved
            cur = self.setup_hash(src) if src.analysis.analysis_type != "thermal_stress" else ""
            d["temperature_source"] = [src.results.directory, src.results.setup_hash, src.results.mesh_id, cur]
        return stable_hash(d)

    def geometry_hash_for(self, study: Optional[Study] = None, scope: str = "bodies") -> str:
        st = study or self.study
        if scope == "all":
            return self.geometry.revision_hash()
        return self.geometry.bodies_hash(st.analysis_body_ids())

    def mesh_is_current(self, study: Optional[Study] = None) -> bool:
        st = study or self.study
        m = st.mesh
        return bool(m.file) and m.geometry_hash == self.geometry_hash_for(st, m.geometry_scope) \
            and m.settings_hash == st.mesh_settings.settings_hash()

    def results_are_current(self, study: Optional[Study] = None) -> bool:
        st = study or self.study
        r = st.results
        return r.completed and self.mesh_is_current(st) and r.mesh_id == st.mesh.mesh_id \
            and r.setup_hash == self.setup_hash(st)


def _study_property(name: str):
    def get(self):
        return getattr(self.study, name)

    def set_(self, value):
        setattr(self.study, name, value)
    return property(get, set_, doc=f"``{name}`` of the active study")


for _n in _STUDY_PROPS:
    setattr(Project, _n, _study_property(_n))
