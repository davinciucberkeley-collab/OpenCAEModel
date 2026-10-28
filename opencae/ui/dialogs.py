"""Field definitions for creation/edit dialogs and the properties panel."""
from __future__ import annotations

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
            Field("is_example", "Example values (unverified)", "bool", m.is_example),
            Field("notes", "Notes", "str", m.notes)]


def apply_material_values(m: Material, v: dict, us: UnitSystem) -> None:
    m.name = v["name"]
    m.youngs_modulus = v["youngs_modulus"] / us.stress_from_si
    m.poissons_ratio = v["poissons_ratio"]
    m.density = v["density"] / us.density_from_si
    m.yield_strength = None if v["yield_strength"] is None else v["yield_strength"] / us.stress_from_si
    m.thermal_expansion = v["thermal_expansion"]
    m.is_example = v["is_example"]
    m.notes = v["notes"] if not m.is_example or v["notes"] else EXAMPLE_WARNING


def target_options(body: Body, kinds: tuple[str, ...]) -> list[tuple[str, str]]:
    out = []
    if "face" in kinds:
        for f in body.faces:
            n = "" if f.normal is None else f", n=({f.normal[0]:+.2f},{f.normal[1]:+.2f},{f.normal[2]:+.2f})"
            c = f.centroid
            out.append((f"face:{f.index}", f"Face {f.index + 1}: {f.surface_type}, A={f.area:.4g}{n}, "
                                            f"c=({c[0]:.3g},{c[1]:.3g},{c[2]:.3g})"))
    if "edge" in kinds:
        for e in body.edges:
            out.append((f"edge:{e.index}", f"Edge {e.index + 1}: {e.curve_type}, L={e.length:.4g}"))
    if "vertex" in kinds:
        for v in body.vertices:
            x, y, z = v.point
            out.append((f"vertex:{v.index}", f"Vertex {v.index + 1}: ({x:.4g}, {y:.4g}, {z:.4g})"))
    return out


CONSTRAINT_TARGETS = ("face", "edge", "vertex")
LOAD_TARGETS = {"force": ("face", "vertex"), "pressure": ("face",), "gravity": ()}


def constraint_fields(kind: str, body: Body, us: UnitSystem, c: Optional[Constraint] = None,
                      preselect: Optional[list[str]] = None) -> list[Field]:
    sel = [f"{t.kind}:{t.index}" for t in c.targets] if c else (preselect or [])
    names = {"fixed": "Fixed support", "displacement": "Prescribed displacement", "symmetry": "Symmetry"}
    fields = [Field("name", "Name", "str", c.name if c else names[kind])]
    if kind == "displacement":
        prm = c.params if c else {"ux": 0.0, "uy": None, "uz": None}
        for k, ax in (("ux", "X"), ("uy", "Y"), ("uz", "Z")):
            fields.append(Field(k, f"U{ax} (empty = free)", "optfloat", prm.get(k), us.length))
    tk = ("face",) if kind == "symmetry" else CONSTRAINT_TARGETS
    fields.append(Field("targets", "Targets", "multicheck", sel, options=target_options(body, tk)))
    return fields


def load_fields(kind: str, body: Body, us: UnitSystem, l: Optional[Load] = None,
                preselect: Optional[list[str]] = None) -> list[Field]:
    sel = [f"{t.kind}:{t.index}" for t in l.targets] if l else (preselect or [])
    p = l.params if l else {}
    names = {"force": "Force", "pressure": "Pressure", "gravity": "Gravity"}
    fields = [Field("name", "Name", "str", l.name if l else names[kind])]
    if kind == "force":
        fields += [Field("magnitude", "Total force magnitude", "float", float(p.get("magnitude", 1000.0)), us.force),
                   Field("coordinate_system", "Coordinate system", "choice", p.get("coordinate_system", "global"),
                         options=[("global", "Global Cartesian (direction below)"),
                                  ("face_normal", "Along outward face normal (planar faces)")]),
                   Field("direction", "Direction (global)", "vec3", p.get("direction", [0, 0, -1]))]
    elif kind == "pressure":
        fields += [Field("magnitude", "Pressure (+ = into face)", "float", float(p.get("magnitude", 1.0)), us.pressure)]
    elif kind == "gravity":
        g = 9.80665 * us.accel_from_si
        fields += [Field("magnitude", "Acceleration", "float", float(p.get("magnitude", g)), us.acceleration),
                   Field("direction", "Direction", "vec3", p.get("direction", [0, 0, -1]))]
    if LOAD_TARGETS[kind]:
        fields.append(Field("targets", "Targets", "multicheck", sel, options=target_options(body, LOAD_TARGETS[kind])))
    return fields


MESH_TIPS = {
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
            Field("optimize", "Optimise element quality", "bool", ms.optimize)]


def analysis_fields(a: AnalysisSettings, solver_info: str) -> list[Field]:
    return [Field("title", "Title", "str", a.title),
            Field("analysis_type", "Analysis type", "choice", a.analysis_type,
                  options=[("linear_static", "Linear static structural")]),
            Field("threads", "Solver threads", "int", a.threads, minimum=1, maximum=256),
            Field("write_ip_stress", "Write integration-point stresses (.dat)", "bool", a.write_ip_stress),
            Field("solver", "Solver", "text", solver_info)]


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
