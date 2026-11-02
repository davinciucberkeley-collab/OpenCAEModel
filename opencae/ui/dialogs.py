"""Field definitions for creation/edit dialogs and the properties panel."""
from __future__ import annotations

import numpy as np

from typing import Any, Optional

from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QTextBrowser, QVBoxLayout

from opencae.geometry.kernel import Body
from opencae.geometry.occ_kernel import PRIMITIVE_DEFAULTS
from opencae.materials.library import EXAMPLE_WARNING
from opencae.project.model import AnalysisSettings, Constraint, Feature, Load, Material, MeshSettings
from opencae.ui.forms import Field
from opencae.utilities.units import UnitSystem

PRIMITIVE_LABELS = {"box": "Box", "cylinder": "Cylinder", "sphere": "Sphere", "cone": "Cone",
                    "plate": "Rectangular plate", "beam": "Beam", "cantilever_beam": "Cantilever beam"}
DIM_LABELS = {"dx": "Length X", "dy": "Length Y", "dz": "Length Z", "radius": "Radius", "height": "Height",
              "radius1": "Base radius", "radius2": "Top radius", "length": "Length", "width": "Width",
              "thickness": "Thickness", "flange_width": "Flange width", "flange_thickness": "Flange thickness",
              "web_thickness": "Web thickness", "outer_diameter": "Outer diameter", "wall_thickness": "Wall thickness"}
PRIMITIVE_NOTES = {
    "box": "Corner at the position, edges along +X, +Y, +Z.",
    "plate": "Corner at the position; length along X, width along Y, thickness along Z.",
    "cylinder": "Base centre at the position, extruded along the axis direction.",
    "cone": "Base centre at the position, along the axis direction.",
    "sphere": "Centre at the position.",
    "beam": "Prismatic beam along +X starting at the position; section centred on the beam axis. "
            "Rectangle uses width (Y) and height (Z).",
    "cantilever_beam": "Rectangular beam along +X (width along Y, height along Z). The template also assigns "
                       "structural steel, fixes the x = 0 face and applies a tip force on the far face.",
}


def body_options(bodies: dict[str, Body], order: list[str], solids_only: bool = True) -> list[tuple[str, str]]:
    return [(b, bodies[b].name) for b in order if b in bodies and (not solids_only or bodies[b].kind == "solid")]


def primitive_fields(shape: str, us: UnitSystem, params: Optional[dict] = None) -> list[Field]:
    p = {**PRIMITIVE_DEFAULTS[shape], "position": [0.0, 0.0, 0.0], "axis": [0.0, 0.0, 1.0], **(params or {})}
    if us.length == "m" and params is None:  # scale defaults to metres
        p = {k: (v / 1000.0 if isinstance(v, float) else v) for k, v in p.items()}
    fields = []
    if shape == "beam":
        fields.append(Field("section", "Section", "choice", p.get("section", "rectangle"),
                            options=[("rectangle", "Rectangle"), ("i_section", "I-section"), ("tube", "Circular tube")]))
    for k, v in PRIMITIVE_DEFAULTS[shape].items():
        if isinstance(v, float):
            fields.append(Field(k, DIM_LABELS.get(k, k), "float", float(p[k]), us.length, minimum=0.0))
    fields.append(Field("position", "Position", "vec3", p["position"], us.length))
    if shape in ("cylinder", "cone"):
        fields.append(Field("axis", "Axis direction", "vec3", p["axis"]))
    if shape == "cantilever_beam" and params is None:
        fields.append(Field("force", "Tip force (-Z)", "float", 1000.0, us.force, minimum=-1e30))
    return fields


def feature_fields(f: Feature, bodies, order, us: UnitSystem) -> list[Field]:
    p = f.params
    if f.kind == "primitive":
        return [Field("name", "Name", "str", f.name)] + primitive_fields(p["shape"], us, p)
    if f.kind == "import":
        return [Field("name", "Name", "str", f.name), Field("file", "File", "text", p.get("file")),
                Field("format", "Format", "text", p.get("format")),
                Field("to_solid", "Reconstruct solid (STL)", "bool", bool(p.get("to_solid")))]
    if f.kind == "transform":
        return [Field("name", "Name", "str", f.name)] + transform_fields(bodies, order, us, p)
    if f.kind == "boolean":
        return [Field("name", "Name", "str", f.name)] + boolean_fields(bodies, order, p.get("op", "union"), p,
                                                                        include_consumed=True)
    if f.kind in ("fillet", "chamfer"):
        return [Field("name", "Name", "str", f.name)] + edge_op_fields(f.kind, bodies, order, us, p)
    if f.kind == "extrude":
        return [Field("name", "Name", "str", f.name)] + extrude_fields(bodies, order, us, p)
    return [Field("info", "Parameters", "text", str(p))]


def transform_fields(bodies, order, us, p: Optional[dict] = None) -> list[Field]:
    p = p or {}
    opts = body_options(bodies, order)
    return [Field("body", "Body", "choice", p.get("body", opts[0][0] if opts else ""), options=opts),
            Field("translate", "Translate", "vec3", p.get("translate", [0, 0, 0]), us.length),
            Field("rotate_axis", "Rotation axis", "vec3", p.get("rotate_axis", [0, 0, 1])),
            Field("rotate_angle", "Rotation angle", "float", float(p.get("rotate_angle", 0.0)), "deg"),
            Field("rotate_center", "Rotation centre", "vec3", p.get("rotate_center", [0, 0, 0]), us.length),
            Field("scale", "Uniform scale", "float", float(p.get("scale", 1.0)), "-", minimum=1e-9),
            Field("scale_center", "Scale centre", "vec3", p.get("scale_center", [0, 0, 0]), us.length)]


def boolean_fields(bodies, order, op: str, p: Optional[dict] = None, include_consumed=False) -> list[Field]:
    p = p or {}
    opts = body_options(bodies, order)
    if include_consumed:
        for key in ("target", "tool"):
            if p.get(key) and p[key] not in [o[0] for o in opts]:
                opts.append((p[key], f"{p[key]} (consumed)"))
    return [Field("op", "Operation", "choice", p.get("op", op),
                  options=[("union", "Union"), ("subtract", "Subtract (target - tool)"), ("intersect", "Intersect")]),
            Field("target", "Target body (kept)", "choice", p.get("target", opts[0][0] if opts else ""), options=opts),
            Field("tool", "Tool body (consumed)", "choice",
                  p.get("tool", opts[1][0] if len(opts) > 1 else ""), options=opts)]


def edge_op_fields(kind: str, bodies, order, us, p: Optional[dict] = None, preselect: Optional[list[int]] = None,
                   body_id: Optional[str] = None) -> list[Field]:
    p = p or {}
    opts = body_options(bodies, order)
    bid = p.get("body", body_id or (opts[0][0] if opts else ""))
    b = bodies.get(bid)
    edges = [(e.index, f"Edge {e.index + 1}: {e.curve_type}, L={e.length:.4g}") for e in (b.edges if b else [])]
    sel = p.get("edges", preselect or [])
    sel = [] if sel == "all" else sel
    key, label = ("radius", "Radius") if kind == "fillet" else ("distance", "Distance")
    return [Field("body", "Body", "choice", bid, options=opts, readonly=bool(p)),
            Field(key, label, "float", float(p.get(key, 2.0 if us.length == "mm" else 0.002)), us.length, minimum=0.0),
            Field("edges", "Edges (none checked = all edges)", "multicheck", sel, options=edges)]


def extrude_fields(bodies, order, us, p: Optional[dict] = None) -> list[Field]:
    p = p or {}
    s = 1.0 if us.length == "mm" else 1e-3
    opts = [("", "(create new body)")] + body_options(bodies, order)
    return [Field("profile", "Profile", "choice", p.get("profile", "rectangle"),
                  options=[("rectangle", "Rectangle"), ("circle", "Circle")]),
            Field("width", "Width (rectangle)", "float", float(p.get("width", 20 * s)), us.length, minimum=0.0),
            Field("height", "Height (rectangle)", "float", float(p.get("height", 10 * s)), us.length, minimum=0.0),
            Field("radius", "Radius (circle)", "float", float(p.get("radius", 5 * s)), us.length, minimum=0.0),
            Field("origin", "Profile centre", "vec3", p.get("origin", [0, 0, 0]), us.length),
            Field("normal", "Extrusion direction (profile normal)", "vec3", p.get("normal", [0, 0, 1])),
            Field("x_dir", "Profile X direction", "vec3", p.get("x_dir", [1, 0, 0])),
            Field("distance", "Distance", "float", float(p.get("distance", 20 * s)), us.length),
            Field("fuse_with", "Fuse with body", "choice", p.get("fuse_with", ""), options=opts)]


def material_fields(m: Material, us: UnitSystem) -> list[Field]:
    return [Field("name", "Name", "str", m.name),
            Field("youngs_modulus", "Young's modulus E", "float", m.youngs_modulus * us.stress_from_si, us.stress,
                  minimum=0.0),
            Field("poissons_ratio", "Poisson's ratio", "float", m.poissons_ratio, "-", minimum=-0.999, maximum=0.4999),
            Field("density", "Density", "float", (m.density or 0.0) * us.density_from_si, us.density, minimum=0.0),
            Field("yield_strength", "Yield strength", "optfloat",
                  None if m.yield_strength is None else m.yield_strength * us.stress_from_si, us.stress),
            Field("thermal_expansion", "Thermal expansion", "optfloat", m.thermal_expansion, "1/K"),
            Field("thermal_conductivity", "Thermal conductivity", "optfloat", m.thermal_conductivity, "W/(m·K)",
                  tooltip="Needed for thermal analyses (SI units whatever the project unit system)"),
            Field("specific_heat", "Specific heat", "optfloat", m.specific_heat, "J/(kg·K)",
                  tooltip="Needed for transient thermal analyses"),
            Field("is_example", "Example values (unverified)", "bool", m.is_example),
            Field("notes", "Notes", "str", m.notes)]


def apply_material_values(m: Material, v: dict, us: UnitSystem) -> None:
    m.name = v["name"]
    m.youngs_modulus = v["youngs_modulus"] / us.stress_from_si
    m.poissons_ratio = v["poissons_ratio"]
    m.density = v["density"] / us.density_from_si
    m.yield_strength = None if v["yield_strength"] is None else v["yield_strength"] / us.stress_from_si
    m.thermal_expansion = v["thermal_expansion"]
    m.thermal_conductivity = v.get("thermal_conductivity", m.thermal_conductivity)
    m.specific_heat = v.get("specific_heat", m.specific_heat)
    m.is_example = v["is_example"]
    m.notes = v["notes"] if not m.is_example or v["notes"] else EXAMPLE_WARNING


def target_key(body_id: str, kind: str, index: int) -> str:
    """Value used for a geometric target in forms: ``<body id>|<kind>:<index>``."""
    return f"{body_id}|{kind}:{index}"


def parse_target_key(key: str) -> tuple[str, str, int]:
    bid, rest = key.split("|", 1)
    kind, idx = rest.split(":")
    return bid, kind, int(idx)


def face_edges(body: Body, face_index: int) -> list[int]:
    from opencae.app.session import face_edge_indices
    return face_edge_indices(body, face_index)


def flow2d_edge_options(body: Body, face_index: int) -> list[tuple[str, str]]:
    out = []
    for i in face_edges(body, face_index):
        e = body.edges[i]
        c = e.centroid
        out.append((target_key(body.id, "edge", i), f"Edge {i + 1}: {e.curve_type}, L={e.length:.4g}, "
                                                     f"c=({c[0]:.3g},{c[1]:.3g},{c[2]:.3g})"))
    return out


def target_options(bodies, kinds: tuple[str, ...]) -> list[tuple[str, str]]:
    """Checkable target list for one body or the bodies of a study (prefixed by body name)."""
    bodies = bodies if isinstance(bodies, (list, tuple)) else [bodies]
    multi = len(bodies) > 1
    out = []
    for body in bodies:
        pre = f"{body.name}: " if multi else ""
        if "face" in kinds:
            for f in body.faces:
                n = "" if f.normal is None else f", n=({f.normal[0]:+.2f},{f.normal[1]:+.2f},{f.normal[2]:+.2f})"
                c = f.centroid
                out.append((target_key(body.id, "face", f.index),
                            f"{pre}Face {f.index + 1}: {f.surface_type}, A={f.area:.4g}{n}, "
                            f"c=({c[0]:.3g},{c[1]:.3g},{c[2]:.3g})"))
        if "edge" in kinds:
            for e in body.edges:
                out.append((target_key(body.id, "edge", e.index),
                            f"{pre}Edge {e.index + 1}: {e.curve_type}, L={e.length:.4g}"))
        if "vertex" in kinds:
            for v in body.vertices:
                x, y, z = v.point
                out.append((target_key(body.id, "vertex", v.index),
                            f"{pre}Vertex {v.index + 1}: ({x:.4g}, {y:.4g}, {z:.4g})"))
    return out


CONSTRAINT_TARGETS = ("face", "edge", "vertex")
LOAD_TARGETS = {"velocity_inlet": ("face",), "pressure_outlet": ("face",),
                "force": ("face", "vertex"), "remote_force": ("face",), "moment": ("face",), "pressure": ("face",), "gravity": (), "heat_flux": ("face",),
                "heat_power": ("face",), "convection": ("face",), "radiation": ("face",), "heat_generation": (),
                "uniform_temperature": ()}
CONSTRAINT_NAMES = {"fixed": "Fixed support", "displacement": "Prescribed displacement", "symmetry": "Symmetry",
                    "temperature": "Temperature", "wall": "Wall (no slip)", "slip_wall": "Slip wall / symmetry"}
FLOW_ITEMS = ("velocity_inlet", "pressure_outlet", "wall", "slip_wall")
LOAD_NAMES = {"velocity_inlet": "Velocity inlet", "pressure_outlet": "Pressure outlet", "force": "Force",
              "moment": "Moment", "remote_force": "Remote force", "pressure": "Pressure", "gravity": "Gravity", "heat_flux": "Heat flux",
              "heat_power": "Heat power", "convection": "Convection", "radiation": "Radiation",
              "heat_generation": "Internal heat generation", "uniform_temperature": "Uniform temperature"}
LOAD_NOTES = {
    "velocity_inlet": "Uniform inflow velocity in m/s (SI). Direction 'normal' = perpendicular into the fluid "
                      "(planar faces); otherwise a global vector.",
    "pressure_outlet": "Gauge static pressure in Pa (SI) imposed on the outlet faces (usually 0).",
    "force": "Total force distributed as a uniform traction over the selected faces (consistent nodal loads), or "
             "split equally between selected vertices.",
    "pressure": "Positive pressure pushes into the surface (opposite to the outward normal).",
    "remote_force": "Force acting at a point away from the faces (e.g. thrust on a motor shaft): transferred to the "
                    "selected faces as a uniform traction plus the moment of the offset. Statically exact; the "
                    "faces are not made rigid.",
    "moment": "Moment about the given axis (right-hand rule) through the area centroid of the selected faces. "
              "Applied as linearly varying nodal forces with zero net force; the resultant moment is exact.",
    "gravity": "Body force rho*g on the whole analysis body; requires material density.",
    "heat_flux": "Uniform heat flux in W/m² (SI, whatever the unit system). Positive = heat ENTERS the body.",
    "heat_power": "Total power in W spread uniformly over the selected faces (positive = into the body).",
    "convection": "q = h (T − T∞) leaves the surface. h in W/(m²·K), T∞ in °C.",
    "radiation": "Grey-body exchange with surroundings at T∞: q = ε σ (T⁴ − T∞⁴); no view factors between "
                 "model surfaces.",
    "heat_generation": "Volumetric heat source in W/m³ in every body of the study (e.g. electronics, coils).",
    "uniform_temperature": "Thermal-stress load: the whole model at this temperature (°C). Thermal strain = "
                           "α (T − T_ref) with T_ref from the analysis settings.",
}
THERMAL_LOADS = ("heat_flux", "heat_power", "convection", "radiation", "heat_generation")
STRUCTURAL_LOADS = ("force", "remote_force", "moment", "pressure", "gravity")

INTERACTION_NAMES = {"bonded": "Bonded (tie)", "contact": "Contact"}
INTERACTION_NOTE = (
    "Connects faces of bodies that are meshed separately (Mesh controls > Assembly mesh = separate). "
    "Bonded: the slave faces are tied to the master faces (non-matching meshes allowed). Contact: the faces may "
    "separate and transmit pressure (and friction), penalty method; the study becomes nonlinear. Use the body "
    "with the finer mesh / softer material as slave. Nothing is connected automatically.")


def interaction_fields(bodies, us: UnitSystem, it=None, master: Optional[list] = None,
                       slave: Optional[list] = None, kind: str = "bonded") -> list[Field]:
    params = it.params if it is not None else {}
    kind = it.kind if it is not None else kind
    m = [target_key(r.body, r.kind, r.index) for r in it.master] if it is not None else (master or [])
    sl = [target_key(r.body, r.kind, r.index) for r in it.slave] if it is not None else (slave or [])
    opts = target_options(bodies, ("face",))
    return [Field("name", "Name", "str", it.name if it is not None else INTERACTION_NAMES[kind]),
            Field("kind", "Type", "choice", kind, options=list(INTERACTION_NAMES.items())),
            Field("master", "Master faces", "multicheck", m, options=opts),
            Field("slave", "Slave faces", "multicheck", sl, options=opts),
            Field("friction", "Friction coefficient (contact)", "float", float(params.get("friction", 0.0)),
                  minimum=0.0, maximum=5.0, tooltip="Coulomb friction; 0 = frictionless (then supports must "
                                                    "prevent sliding)."),
            Field("stiffness", "Penalty stiffness (contact, 0 = automatic)", "float",
                  float(params.get("stiffness", 0.0)), f"{us.stress}/{us.length}", minimum=0.0,
                  tooltip="Normal contact stiffness. Automatic = 10 E / h (largest Young modulus of the materials, h mean "
                          "element size): penetration about p / K.")]


def constraint_fields(kind: str, body: Body, us: UnitSystem, c: Optional[Constraint] = None,
                      preselect: Optional[list[str]] = None, flow2d=None) -> list[Field]:
    sel = [target_key(t.body, t.kind, t.index) for t in c.targets] if c else (preselect or [])
    fields = [Field("name", "Name", "str", c.name if c else CONSTRAINT_NAMES[kind])]
    if kind == "temperature":
        fields.append(Field("value", "Temperature", "float", float((c.params if c else {}).get("value", 20.0)), "°C"))
    if kind in ("wall", "slip_wall"):
        if flow2d is not None:  # 2-D flow: edges of the flow face
            fields.append(Field("targets", "Edges of the flow face", "multicheck", sel,
                                options=flow2d_edge_options(*flow2d)))
        else:
            fields.append(Field("targets", "Faces", "multicheck", sel, options=target_options(body, ("face",))))
        return fields
    if kind == "displacement":
        prm = c.params if c else {"ux": 0.0, "uy": None, "uz": None}
        for k, ax in (("ux", "X"), ("uy", "Y"), ("uz", "Z")):
            fields.append(Field(k, f"U{ax} (empty = free)", "optfloat", prm.get(k), us.length))
    tk = ("face",) if kind == "symmetry" else CONSTRAINT_TARGETS
    fields.append(Field("targets", "Targets", "multicheck", sel, options=target_options(body, tk)))
    return fields


def load_fields(kind: str, body: Body, us: UnitSystem, l: Optional[Load] = None,
                preselect: Optional[list[str]] = None, flow2d=None) -> list[Field]:
    sel = [target_key(t.body, t.kind, t.index) for t in l.targets] if l else (preselect or [])
    p = l.params if l else {}
    fields = [Field("name", "Name", "str", l.name if l else LOAD_NAMES[kind])]
    if kind == "force":
        fields += [Field("magnitude", "Total force magnitude", "float", float(p.get("magnitude", 1000.0)), us.force),
                   Field("coordinate_system", "Coordinate system", "choice", p.get("coordinate_system", "global"),
                         options=[("global", "Global Cartesian (direction below)"),
                                  ("face_normal", "Along outward face normal (planar faces)")]),
                   Field("direction", "Direction (global)", "vec3", p.get("direction", [0, 0, -1]))]
    elif kind == "velocity_inlet":
        d = p.get("direction", "normal")
        fields += [Field("magnitude", "Velocity", "float", float(p.get("magnitude", 1.0)), "m/s", minimum=1e-12),
                   Field("dir_mode", "Direction", "choice", "normal" if isinstance(d, str) else "vector",
                         options=[("normal", "Normal to the faces, into the fluid"), ("vector", "Global vector")]),
                   Field("direction", "Vector (if 'Global vector')", "vec3", [0, 0, 1] if isinstance(d, str) else d)]
    elif kind == "pressure_outlet":
        fields += [Field("magnitude", "Gauge pressure", "float", float(p.get("magnitude", 0.0)), "Pa")]
    elif kind == "remote_force":
        fields += [Field("magnitude", "Force", "float", float(p.get("magnitude", 10.0)), us.force),
                   Field("direction", "Direction (global)", "vec3", p.get("direction", [0, 0, 1])),
                   Field("point", "Point of application", "vec3", p.get("point", [0, 0, 0]))]
    elif kind == "moment":
        fields += [Field("magnitude", "Moment", "float", float(p.get("magnitude", 1000.0)), f"{us.force}·{us.length}"),
                   Field("direction", "Axis (global, right-hand rule)", "vec3", p.get("direction", [0, 0, 1]))]
    elif kind == "pressure":
        fields += [Field("magnitude", "Pressure (+ = into face)", "float", float(p.get("magnitude", 1.0)), us.pressure)]
    elif kind == "gravity":
        g = 9.80665 * us.accel_from_si
        fields += [Field("magnitude", "Acceleration", "float", float(p.get("magnitude", g)), us.acceleration),
                   Field("direction", "Direction", "vec3", p.get("direction", [0, 0, -1]))]
    elif kind == "heat_flux":
        fields += [Field("magnitude", "Heat flux (+ = into body)", "float", float(p.get("magnitude", 1000.0)), "W/m²")]
    elif kind == "heat_power":
        fields += [Field("magnitude", "Total power (+ = into body)", "float", float(p.get("magnitude", 1.0)), "W")]
    elif kind == "convection":
        fields += [Field("film", "Film coefficient h", "float", float(p.get("film", 10.0)), "W/(m²·K)", minimum=1e-12),
                   Field("ambient", "Ambient temperature T∞", "float", float(p.get("ambient", 20.0)), "°C")]
    elif kind == "radiation":
        fields += [Field("emissivity", "Emissivity ε", "float", float(p.get("emissivity", 0.9)), "-", minimum=1e-6,
                         maximum=1.0),
                   Field("ambient", "Surroundings temperature T∞", "float", float(p.get("ambient", 20.0)), "°C")]
    elif kind == "heat_generation":
        fields += [Field("magnitude", "Heat generation", "float", float(p.get("magnitude", 1e5)), "W/m³")]
    elif kind == "uniform_temperature":
        fields += [Field("value", "Temperature", "float", float(p.get("value", 100.0)), "°C")]
    if flow2d is not None and kind in ("velocity_inlet", "pressure_outlet"):
        fields.append(Field("targets", "Edges of the flow face", "multicheck", sel,
                            options=flow2d_edge_options(*flow2d)))
    elif LOAD_TARGETS[kind]:
        fields.append(Field("targets", "Targets", "multicheck", sel, options=target_options(body, LOAD_TARGETS[kind])))
    return fields


MESH_TIPS = {
    "assembly": "Conformal: bodies that touch are fused into one mesh with shared nodes (perfectly bonded). "
                "Separate: each body gets its own mesh; connect them with Assembly > Bonded / Contact "
                "interactions (needed for contact, or to mesh parts with different sizes).",
    "element": "C3D10 (quadratic tet) is the reliable default for any solid. Hexahedra need box-like (six "
               "four-sided faces) volumes; they give accurate results with fewer nodes.",
    "sizing": "Element size: you set the target edge length. Target count: OpenCAE iterates the global size "
              "until the number of volume elements is within the tolerance of your target (unstructured meshes "
              "cannot hit an exact count).",
    "growth": "How fast elements grow away from local refinements: 1.2 = smooth (more elements), 2 = abrupt. "
              "0 keeps the default transition.",
}


def mesh_fields(ms: MeshSettings, bodies, order, us: UnitSystem, auto_size: float) -> list[Field]:
    el = f"{ms.element_shape}:{ms.element_order}"
    return [Field("body", "Body to mesh", "choice", ms.body, options=body_options(bodies, order)),
            Field("element", "Element type", "choice", el, tooltip=MESH_TIPS["element"],
                  options=[("tet:2", "C3D10 - quadratic tetrahedron (recommended)"),
                           ("tet:1", "C3D4 - linear tetrahedron (stiff; checks only)"),
                           ("hex:2", "C3D20 - quadratic hexahedron (box-like volumes only)"),
                           ("hex:1", "C3D8 - linear hexahedron (box-like volumes only)")]),
            Field("hex_formulation", "Hex formulation", "choice", ms.hex_formulation,
                  options=[("enhanced", "Enhanced: C3D8I (incompatible modes) / C3D20R (reduced)"),
                           ("standard", "Standard full integration: C3D8 / C3D20")]),
            Field("sizing_mode", "Mesh density by", "choice", ms.sizing_mode, tooltip=MESH_TIPS["sizing"],
                  options=[("size", "Element size"), ("target_count", "Target number of elements")]),
            Field("target_elements", "Target element count", "int", int(ms.target_elements or 5000),
                  minimum=10, maximum=5_000_000, tooltip="Used when 'Mesh density by' = target count"),
            Field("size_max", f"Max element size (0 = auto ≈ {auto_size:.3g})", "float", ms.size_max, us.length,
                  minimum=0.0),
            Field("size_min", "Min element size (0 = none)", "float", ms.size_min, us.length, minimum=0.0),
            Field("curvature_points", "Elements per 2π curvature (0 = off)", "int", ms.curvature_points, minimum=0,
                  maximum=100),
            Field("growth_rate", "Growth rate from refinements (0 = default)", "float", ms.growth_rate,
                  minimum=0.0, maximum=5.0, tooltip=MESH_TIPS["growth"]),
            Field("algorithm_2d", "Surface algorithm", "choice", ms.algorithm_2d,
                  options=[(6, "Frontal-Delaunay (default)"), (5, "Delaunay"), (1, "MeshAdapt")]),
            Field("algorithm_3d", "Volume algorithm", "choice", ms.algorithm_3d,
                  options=[(1, "Delaunay (default)"), (4, "Frontal"), (10, "HXT (parallel Delaunay)")]),
            Field("smoothing_steps", "Smoothing steps", "int", ms.smoothing_steps, minimum=0, maximum=100),
            Field("quality_threshold", "Warn below quality", "float", ms.quality_threshold, minimum=0.0, maximum=1.0,
                  tooltip="Elements with gamma (tets) or SICN (hexes) below this value are reported"),
            Field("optimize", "Optimise element quality", "bool", ms.optimize),
            Field("assembly_mesh", "Assembly mesh", "choice", ms.assembly_mesh, tooltip=MESH_TIPS.get("assembly", ""),
                  options=[("conformal", "Conformal: touching bodies share nodes (bonded)"),
                           ("separate", "Separate: each body meshed on its own (use interactions)")]),
            Field("bl_layers", "Boundary layers (2-D flow, 0 = off)", "int", ms.bl_layers, minimum=0, maximum=50,
                  tooltip="Thin cells along no-slip wall edges of a 2-D flow face (resolve the wall velocity "
                          "gradient). Not used for 3-D or structural meshes."),
            Field("bl_first", "First layer thickness (0 = auto)", "float", ms.bl_first, us.length, minimum=0.0),
            Field("bl_growth", "Layer growth ratio", "float", ms.bl_growth, minimum=1.0, maximum=3.0)]


ANALYSIS_TIPS = {
    "n_modes": "Number of natural frequencies and mode shapes to extract (modal analysis).",
    "freq_min": "Modal: only frequencies above this value (Hz) are sought (0 = from the lowest).",
    "time_total": "Transient thermal: total simulated time in seconds.",
    "time_step": "Transient thermal: fixed time increment (implicit backward Euler; halve it to check the "
                 "time-step error).",
    "initial_temperature": "Thermal: initial temperature of all nodes (°C); also the start value for steady "
                           "radiation iterations.",
    "temperature_source_study": "Thermal stress: thermal study whose final temperatures load this study.",
    "reference_temperature": "Thermal stress: stress-free temperature T_ref (°C).",
    "nonlinear_geometry": "Large-displacement (NLGEOM) static analysis. Use when deflections are large compared "
                          "with the part thickness.",
}


def analysis_fields(a: AnalysisSettings, solver_info: str, studies=None, current_id: str = "",
                    planar_faces=None) -> list[Field]:
    from opencae.physics import available_analysis_types
    from opencae.project.model import ANALYSIS_TYPES
    types = [(t, ANALYSIS_TYPES.get(t, t)) for t in available_analysis_types()]
    thermal = [("", "(none)")] + [(st.id, st.name) for st in (studies or [])
                                   if st.id != current_id and
                                   st.analysis.analysis_type in ("thermal_steady", "thermal_transient")]
    t = a.analysis_type
    f = [Field("title", "Title", "str", a.title),
         Field("analysis_type", "Analysis type", "choice", t, options=types,
               tooltip="Changing the type keeps supports and loads; items that do not apply are reported as "
                       "ignored when validating.")]
    if t in ("linear_static", "thermal_stress"):
        f.append(Field("nonlinear_geometry", "Large deflection (NLGEOM)", "bool", a.nonlinear_geometry,
                       tooltip=ANALYSIS_TIPS["nonlinear_geometry"]))
    if t == "buckling":
        f.append(Field("n_modes", "Number of buckling modes", "int", a.n_modes, minimum=1, maximum=100,
                       tooltip="Load factors and mode shapes to extract; the lowest positive factor is critical."))
    if t == "modal":
        f += [Field("n_modes", "Number of modes", "int", a.n_modes, minimum=1, maximum=500,
                    tooltip=ANALYSIS_TIPS["n_modes"]),
              Field("freq_min", "Lowest frequency of interest", "float", a.freq_min, "Hz", minimum=0.0,
                    tooltip=ANALYSIS_TIPS["freq_min"])]
    if t in ("thermal_steady", "thermal_transient"):
        f.append(Field("initial_temperature", "Initial temperature", "float", a.initial_temperature, "°C",
                       tooltip=ANALYSIS_TIPS["initial_temperature"]))
    if t == "thermal_transient":
        f += [Field("time_total", "End time", "float", a.time_total, "s", minimum=1e-12,
                    tooltip=ANALYSIS_TIPS["time_total"]),
              Field("time_step", "Time step", "float", a.time_step, "s", minimum=1e-12,
                    tooltip=ANALYSIS_TIPS["time_step"])]
    if t == "thermal_stress":
        f += [Field("temperature_source_study", "Temperatures from study", "choice", a.temperature_source_study,
                    options=thermal, tooltip=ANALYSIS_TIPS["temperature_source_study"]),
              Field("reference_temperature", "Stress-free temperature", "float", a.reference_temperature, "°C",
                    tooltip=ANALYSIS_TIPS["reference_temperature"])]
    if t == "cfd_incompressible":
        from opencae.materials.library import FLUIDS
        f += [Field("cfd_dimension", "Dimension", "choice", a.cfd_dimension,
                    options=[("3d", "3-D (volume mesh of the fluid bodies)"),
                             ("2d", "2-D planar (mesh of one flat face; conditions on its edges)")],
                    tooltip="2-D: the flow in the plane of the chosen face, per metre of depth."),
              Field("cfd_face", "2-D flow face", "choice", a.cfd_face, options=planar_faces or [("", "(none)")],
                    tooltip="Planar face of a study body whose shape is the 2-D flow domain."),
              Field("fluid_name", "Fluid", "choice", a.fluid_name if a.fluid_name in FLUIDS else "custom",
                    options=[(k, k) for k in FLUIDS] + [("custom", "Custom (values below)")],
                    tooltip="Choosing a fluid fills density and viscosity when you press Apply."),
              Field("fluid_density", "Density ρ", "float", a.fluid_density, "kg/m³", minimum=1e-12),
              Field("fluid_viscosity", "Dynamic viscosity μ", "float", a.fluid_viscosity, "Pa·s", minimum=1e-15),
              Field("flow_model", "Flow model", "choice", a.flow_model,
                    options=[("laminar", "Laminar (no turbulence model)"), ("sa", "RANS Spalart–Allmaras"),
                             ("sst", "RANS k–ω SST")],
                    tooltip="Laminar for low Reynolds numbers (pipe Re < ~2300). RANS models for turbulent flow; "
                            "they need fine wall meshes for accurate wall shear."),
              Field("cfd_iterations", "Maximum iterations", "int", a.cfd_iterations, minimum=10, maximum=1_000_000),
              Field("cfd_cfl", "CFL number", "float", a.cfd_cfl, minimum=0.1, maximum=1e6,
                    tooltip="Pseudo-time step of the implicit solver: larger = faster but less robust (reduce "
                            "to 10–50 if the run diverges)."),
              Field("cfd_residual", "Convergence: residual drop (log10)", "float", a.cfd_residual, maximum=-1.0,
                    tooltip="Converged when pressure and velocity residuals fall this many orders of magnitude "
                            "below their initial level (e.g. -6)."),
              Field("turbulence_intensity", "Inlet turbulence intensity", "float", a.turbulence_intensity,
                    minimum=0.0, maximum=1.0),
              Field("turbulent_viscosity_ratio", "Inlet turbulent/laminar viscosity ratio", "float",
                    a.turbulent_viscosity_ratio, minimum=0.0),
              Field("ref_area", "Reference area for coefficients (0 = forces only)", "float", a.ref_area, "m²",
                    minimum=0.0),
              Field("ref_velocity", "Reference velocity (0 = largest inlet)", "float", a.ref_velocity, "m/s",
                    minimum=0.0)]
    f += [Field("threads", "Solver threads", "int", a.threads, minimum=1, maximum=256)]
    if t in ("linear_static", "thermal_stress"):
        f.append(Field("write_ip_stress", "Write integration-point stresses (.dat)", "bool", a.write_ip_stress))
    f.append(Field("solver", "Solver", "text", solver_info))
    return f


ANALYSIS_KEYS = ("cfd_dimension", "cfd_face", "fluid_name", "fluid_density", "fluid_viscosity", "flow_model", "cfd_iterations", "cfd_cfl",
                 "cfd_residual", "turbulence_intensity", "turbulent_viscosity_ratio", "ref_area", "ref_velocity",
                 "title", "analysis_type", "threads", "write_ip_stress", "nonlinear_geometry", "n_modes", "freq_min",
                 "initial_temperature", "time_total", "time_step", "temperature_source_study",
                 "reference_temperature")


class TextDialog(QDialog):
    def __init__(self, title: str, html_text: str, parent=None, width=760, height=560):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(width, height)
        lay = QVBoxLayout(self)
        tb = QTextBrowser()
        tb.setOpenExternalLinks(True)
        tb.setHtml(html_text)
        lay.addWidget(tb)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.reject)
        bb.accepted.connect(self.accept)
        lay.addWidget(bb)
