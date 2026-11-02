"""OpenCascade (OCP) implementation of the geometry kernel.

Topology indexing: faces, edges and vertices are numbered by their position
(0-based) in ``TopExp::MapShapes`` indexed maps. For a given shape this order
is deterministic, so it is stable across save/reopen of an unchanged feature
history. References additionally carry geometric signatures that are checked
after regeneration (see :mod:`opencae.geometry.references`).
"""
from __future__ import annotations

import math
import shutil
from pathlib import Path
from typing import Any, Optional

import numpy as np
from OCP.BRep import BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Surface
from OCP.BRepAlgoAPI import BRepAlgoAPI_Common, BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
from OCP.BRepBuilderAPI import (BRepBuilderAPI_MakeEdge, BRepBuilderAPI_MakeFace,
                                BRepBuilderAPI_MakePolygon, BRepBuilderAPI_MakeSolid,
                                BRepBuilderAPI_MakeWire, BRepBuilderAPI_Sewing,
                                BRepBuilderAPI_Transform)
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.BRepFilletAPI import BRepFilletAPI_MakeChamfer, BRepFilletAPI_MakeFillet
from OCP.BRepGProp import BRepGProp
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.BRepPrimAPI import (BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCone, BRepPrimAPI_MakeCylinder,
                             BRepPrimAPI_MakePrism, BRepPrimAPI_MakeSphere)
from OCP.BRepTools import BRepTools
from OCP.BRep import BRep_Builder
from OCP.GCPnts import GCPnts_TangentialDeflection
from OCP.GProp import GProp_GProps
from OCP.gp import gp_Ax1, gp_Ax2, gp_Ax3, gp_Circ, gp_Dir, gp_Pnt, gp_Trsf, gp_Vec
from OCP.IFSelect import IFSelect_RetDone
from OCP.IGESControl import IGESControl_Reader, IGESControl_Writer
from OCP.Interface import Interface_Static
from OCP.RWStl import RWStl
from OCP.ShapeFix import ShapeFix_Shape, ShapeFix_Solid
from OCP.ShapeUpgrade import ShapeUpgrade_UnifySameDomain
from OCP.STEPControl import STEPControl_AsIs, STEPControl_Reader, STEPControl_Writer
from OCP.StlAPI import StlAPI_Writer
from OCP.TopAbs import (TopAbs_EDGE, TopAbs_FACE, TopAbs_REVERSED, TopAbs_SHELL, TopAbs_SOLID,
                        TopAbs_VERTEX)
from OCP.TopLoc import TopLoc_Location
from OCP.TopExp import TopExp
from OCP.TopoDS import TopoDS_Compound, TopoDS_Shape

from opencae.geometry._occ import IndexedMapOfShape, to_edge, to_face, to_shell, to_vertex
from opencae.geometry.kernel import (Body, BodyCheck, EdgeInfo, FaceInfo, GeometryError,
                                     RegenerationResult, Tessellation, VertexInfo)
from opencae.project.model import Feature
from opencae.utilities.logging_setup import get_logger

log = get_logger("geometry.occ")

PRIMITIVES = ("box", "cylinder", "sphere", "cone", "plate", "beam", "cantilever_beam")

# default parameters for each primitive (lengths in project length units)
PRIMITIVE_DEFAULTS: dict[str, dict[str, Any]] = {
    "box": {"dx": 100.0, "dy": 50.0, "dz": 30.0},
    "cylinder": {"radius": 20.0, "height": 100.0},
    "sphere": {"radius": 25.0},
    "cone": {"radius1": 30.0, "radius2": 10.0, "height": 60.0},
    "plate": {"length": 200.0, "width": 100.0, "thickness": 5.0},
    "beam": {"length": 500.0, "section": "rectangle", "width": 20.0, "height": 40.0,
             "flange_width": 40.0, "flange_thickness": 5.0, "web_thickness": 4.0,
             "outer_diameter": 40.0, "wall_thickness": 4.0},
    "cantilever_beam": {"length": 200.0, "width": 20.0, "height": 20.0},
}


# --------------------------------------------------------------------------- small helpers
def _pnt(v) -> gp_Pnt:
    return gp_Pnt(float(v[0]), float(v[1]), float(v[2]))


def _dir(v) -> gp_Dir:
    a = np.asarray(v, dtype=float)
    n = np.linalg.norm(a)
    if n < 1e-12:
        raise GeometryError("Zero-length direction vector.")
    a = a / n
    return gp_Dir(float(a[0]), float(a[1]), float(a[2]))


def _vec(v) -> gp_Vec:
    return gp_Vec(float(v[0]), float(v[1]), float(v[2]))


def _xyz(p) -> np.ndarray:
    return np.array([p.X(), p.Y(), p.Z()])


def _enum_name(e) -> str:
    s = str(e).split(".")[-1]
    for prefix in ("GeomAbs_",):
        s = s.replace(prefix, "")
    return s.lower()


def index_map(shape: TopoDS_Shape, kind) -> IndexedMapOfShape:
    m = IndexedMapOfShape()
    TopExp.MapShapes_s(shape, kind, m)
    return m


def faces_of(shape) -> list:
    m = index_map(shape, TopAbs_FACE)
    return [to_face(m.FindKey(i)) for i in range(1, m.Extent() + 1)]


def edges_of(shape) -> list:
    m = index_map(shape, TopAbs_EDGE)
    return [to_edge(m.FindKey(i)) for i in range(1, m.Extent() + 1)]


def vertices_of(shape) -> list:
    m = index_map(shape, TopAbs_VERTEX)
    return [to_vertex(m.FindKey(i)) for i in range(1, m.Extent() + 1)]


def _check_done(builder, what: str) -> TopoDS_Shape:
    try:
        builder.Build()
    except Exception:  # some builders build in constructor
        pass
    if not builder.IsDone():
        raise GeometryError(f"{what} failed (OpenCascade builder not done).")
    if hasattr(builder, "HasErrors") and builder.HasErrors():
        raise GeometryError(f"{what} failed (OpenCascade reported errors).")
    shape = builder.Shape()
    if shape.IsNull():
        raise GeometryError(f"{what} produced an empty shape.")
    return shape


def _unify(shape: TopoDS_Shape) -> TopoDS_Shape:
    u = ShapeUpgrade_UnifySameDomain(shape, True, True, False)
    u.Build()
    return u.Shape()


def _volume(shape) -> float:
    p = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, p)
    return p.Mass()


# --------------------------------------------------------------------------- primitives
def make_primitive(shape: str, p: dict[str, Any]) -> TopoDS_Shape:
    params = {**PRIMITIVE_DEFAULTS.get(shape, {}), **p}
    pos = params.get("position", [0.0, 0.0, 0.0])
    axis = params.get("axis", [0.0, 0.0, 1.0])

    def pos_dim(*names):
        for n in names:
            if float(params[n]) <= 0:
                raise GeometryError(f"Parameter '{n}' must be positive.")

    if shape == "box":
        pos_dim("dx", "dy", "dz")
        return BRepPrimAPI_MakeBox(_pnt(pos), params["dx"], params["dy"], params["dz"]).Shape()
    if shape == "plate":
        pos_dim("length", "width", "thickness")
        return BRepPrimAPI_MakeBox(_pnt(pos), params["length"], params["width"], params["thickness"]).Shape()
    if shape == "cylinder":
        pos_dim("radius", "height")
        return BRepPrimAPI_MakeCylinder(gp_Ax2(_pnt(pos), _dir(axis)), params["radius"], params["height"]).Shape()
    if shape == "sphere":
        pos_dim("radius")
        return BRepPrimAPI_MakeSphere(_pnt(pos), params["radius"]).Shape()
    if shape == "cone":
        pos_dim("height")
        r1, r2 = float(params["radius1"]), float(params["radius2"])
        if r1 < 0 or r2 < 0 or (r1 == 0 and r2 == 0) or abs(r1 - r2) < 1e-12:
            raise GeometryError("Cone radii must be >= 0, not both zero and not equal (use a cylinder).")
        return BRepPrimAPI_MakeCone(gp_Ax2(_pnt(pos), _dir(axis)), r1, r2, params["height"]).Shape()
    if shape == "cantilever_beam":
        pos_dim("length", "width", "height")
        L, w, h = params["length"], params["width"], params["height"]
        x0, y0, z0 = pos
        return BRepPrimAPI_MakeBox(gp_Pnt(x0, y0 - w / 2, z0 - h / 2), L, w, h).Shape()
    if shape == "beam":
        return _make_beam(params, pos)
    raise GeometryError(f"Unknown primitive '{shape}'.")


def _polygon_face(points3d: list[np.ndarray]) -> Any:
    poly = BRepBuilderAPI_MakePolygon()
    for q in points3d:
        poly.Add(_pnt(q))
    poly.Close()
    return BRepBuilderAPI_MakeFace(poly.Wire(), True).Face()


def _make_beam(params: dict, pos) -> TopoDS_Shape:
    """Prismatic beam along +X. Cross section in the YZ plane, centred on the axis."""
    L = float(params["length"])
    if L <= 0:
        raise GeometryError("Beam length must be positive.")
    sec = params.get("section", "rectangle")
    x0, y0, z0 = (float(v) for v in pos)
    if sec == "rectangle":
        w, h = float(params["width"]), float(params["height"])
        if w <= 0 or h <= 0:
            raise GeometryError("Section width/height must be positive.")
        return BRepPrimAPI_MakeBox(gp_Pnt(x0, y0 - w / 2, z0 - h / 2), L, w, h).Shape()
    if sec == "i_section":
        b, h = float(params["flange_width"]), float(params["height"])
        tf, tw = float(params["flange_thickness"]), float(params["web_thickness"])
        if min(b, h, tf, tw) <= 0 or 2 * tf >= h or tw >= b:
            raise GeometryError("Invalid I-section dimensions (need 2*tf < h and tw < b).")
        yz = [(-b / 2, -h / 2), (b / 2, -h / 2), (b / 2, -h / 2 + tf), (tw / 2, -h / 2 + tf),
              (tw / 2, h / 2 - tf), (b / 2, h / 2 - tf), (b / 2, h / 2), (-b / 2, h / 2),
              (-b / 2, h / 2 - tf), (-tw / 2, h / 2 - tf), (-tw / 2, -h / 2 + tf), (-b / 2, -h / 2 + tf)]
        face = _polygon_face([np.array([x0, y0 + y, z0 + z]) for y, z in yz])
        return BRepPrimAPI_MakePrism(face, gp_Vec(L, 0, 0)).Shape()
    if sec == "tube":
        d, t = float(params["outer_diameter"]), float(params["wall_thickness"])
        if d <= 0 or t <= 0 or 2 * t >= d:
            raise GeometryError("Invalid tube: need 0 < 2*t < D.")
        ax = gp_Ax2(gp_Pnt(x0, y0, z0), gp_Dir(1, 0, 0))
        outer = BRepPrimAPI_MakeCylinder(ax, d / 2, L).Shape()
        inner = BRepPrimAPI_MakeCylinder(ax, d / 2 - t, L).Shape()
        return _unify(_check_done(BRepAlgoAPI_Cut(outer, inner), "Tube cut"))
    raise GeometryError(f"Unknown beam section '{sec}'.")


def make_extrusion(p: dict[str, Any]) -> TopoDS_Shape:
    origin = p.get("origin", [0, 0, 0])
    normal = p.get("normal", [0, 0, 1])
    xdir = p.get("x_dir", [1, 0, 0])
    n = np.asarray(normal, float)
    xd = np.asarray(xdir, float)
    if abs(np.dot(n, xd)) / (np.linalg.norm(n) * np.linalg.norm(xd) + 1e-30) > 0.999:
        xd = np.array([0, 1, 0]) if abs(n[1]) < 0.9 else np.array([1, 0, 0])
    ax3 = gp_Ax3(_pnt(origin), _dir(n), _dir(xd))
    o = np.asarray(origin, float)
    u = _xyz(ax3.XDirection())
    v = _xyz(ax3.YDirection())
    prof = p.get("profile", "rectangle")
    dist = float(p.get("distance", 10.0))
    if abs(dist) < 1e-12:
        raise GeometryError("Extrusion distance must be non-zero.")
    if prof == "rectangle":
        w, h = float(p.get("width", 10)), float(p.get("height", 10))
        if w <= 0 or h <= 0:
            raise GeometryError("Profile width/height must be positive.")
        pts = [o + a * u + b * v for a, b in ((-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2))]
        face = _polygon_face(pts)
    elif prof == "circle":
        r = float(p.get("radius", 5))
        if r <= 0:
            raise GeometryError("Profile radius must be positive.")
        edge = BRepBuilderAPI_MakeEdge(gp_Circ(ax3.Ax2(), r)).Edge()
        face = BRepBuilderAPI_MakeFace(BRepBuilderAPI_MakeWire(edge).Wire(), True).Face()
    elif prof == "polygon":
        pts2 = p.get("points", [])
        if len(pts2) < 3:
            raise GeometryError("Polygon profile needs at least 3 points.")
        face = _polygon_face([o + a * u + b * v for a, b in pts2])
    else:
        raise GeometryError(f"Unknown profile '{prof}'.")
    nn = n / np.linalg.norm(n)
    return BRepPrimAPI_MakePrism(face, _vec(nn * dist)).Shape()


# --------------------------------------------------------------------------- operations
def apply_transform(shape, p: dict[str, Any]) -> TopoDS_Shape:
    out = shape
    s = float(p.get("scale", 1.0) or 1.0)
    if s <= 0:
        raise GeometryError("Scale factor must be positive.")
    if abs(s - 1.0) > 1e-12:
        t = gp_Trsf()
        t.SetScale(_pnt(p.get("scale_center", [0, 0, 0])), s)
        out = BRepBuilderAPI_Transform(out, t, True).Shape()
    ang = float(p.get("rotate_angle", 0.0) or 0.0)
    if abs(ang) > 1e-12:
        t = gp_Trsf()
        t.SetRotation(gp_Ax1(_pnt(p.get("rotate_center", [0, 0, 0])), _dir(p.get("rotate_axis", [0, 0, 1]))),
                      math.radians(ang))
        out = BRepBuilderAPI_Transform(out, t, True).Shape()
    tr = p.get("translate", [0, 0, 0])
    if any(abs(float(v)) > 0 for v in tr):
        t = gp_Trsf()
        t.SetTranslation(_vec(tr))
        out = BRepBuilderAPI_Transform(out, t, True).Shape()
    return out


def apply_boolean(a, b, op: str) -> TopoDS_Shape:
    builders = {"union": BRepAlgoAPI_Fuse, "subtract": BRepAlgoAPI_Cut, "intersect": BRepAlgoAPI_Common}
    if op not in builders:
        raise GeometryError(f"Unknown boolean op '{op}'.")
    res = _check_done(builders[op](a, b), f"Boolean {op}")
    res = _unify(res)
    if index_map(res, TopAbs_SOLID).Extent() == 0 or _volume(res) <= 0:
        raise GeometryError(f"Boolean {op} produced no solid (bodies may not overlap).")
    return res


def _select_edges(shape, edges) -> list:
    all_edges = edges_of(shape)
    if edges in ("all", None, []):
        return all_edges
    out = []
    for i in edges:
        if not 0 <= int(i) < len(all_edges):
            raise GeometryError(f"Edge index {int(i) + 1} out of range (body has {len(all_edges)} edges).")
        out.append(all_edges[int(i)])
    return out


def apply_fillet(shape, radius: float, edges) -> TopoDS_Shape:
    if radius <= 0:
        raise GeometryError("Fillet radius must be positive.")
    mk = BRepFilletAPI_MakeFillet(shape)
    for e in _select_edges(shape, edges):
        mk.Add(float(radius), e)
    return _check_done(mk, "Fillet")


def apply_chamfer(shape, distance: float, edges) -> TopoDS_Shape:
    if distance <= 0:
        raise GeometryError("Chamfer distance must be positive.")
    mk = BRepFilletAPI_MakeChamfer(shape)
    for e in _select_edges(shape, edges):
        mk.Add(float(distance), e)
    return _check_done(mk, "Chamfer")


# --------------------------------------------------------------------------- import / export
def _scale_shape(shape, factor: float):
    if abs(factor - 1.0) < 1e-15:
        return shape
    t = gp_Trsf()
    t.SetScale(gp_Pnt(0, 0, 0), factor)
    return BRepBuilderAPI_Transform(shape, t, True).Shape()


def read_step(path: Path) -> TopoDS_Shape:
    Interface_Static.SetCVal_s("xstep.cascade.unit", "MM")
    r = STEPControl_Reader()
    if r.ReadFile(str(path)) != IFSelect_RetDone:
        raise GeometryError(f"Could not read STEP file {path}.")
    r.TransferRoots()
    s = r.OneShape()
    if s.IsNull():
        raise GeometryError("STEP file contains no transferable shape.")
    return s


def read_iges(path: Path) -> TopoDS_Shape:
    Interface_Static.SetCVal_s("xstep.cascade.unit", "MM")
    r = IGESControl_Reader()
    if r.ReadFile(str(path)) != IFSelect_RetDone:
        raise GeometryError(f"Could not read IGES file {path}.")
    r.TransferRoots()
    s = r.OneShape()
    if s.IsNull():
        raise GeometryError("IGES file contains no transferable shape.")
    # IGES usually delivers trimmed surfaces; try to sew into a solid.
    if index_map(s, TopAbs_SOLID).Extent() == 0 and index_map(s, TopAbs_FACE).Extent() > 0:
        try:
            s = sew_to_solid(s)
        except GeometryError as exc:
            log.warning("IGES surfaces could not be sewn into a solid: %s", exc)
    return s


def read_brep(path: Path) -> TopoDS_Shape:
    s = TopoDS_Shape()
    if not BRepTools.Read_s(s, str(path), BRep_Builder()):
        raise GeometryError(f"Could not read BREP file {path}.")
    return s


def read_stl(path: Path) -> tuple[np.ndarray, np.ndarray]:
    tri = RWStl.ReadFile_s(str(path))
    if tri is None or tri.NbTriangles() == 0:
        raise GeometryError(f"Could not read STL file {path} (empty or invalid).")
    pts = np.array([_xyz(tri.Node(i)) for i in range(1, tri.NbNodes() + 1)])
    tris = np.array([tri.Triangle(i).Get() for i in range(1, tri.NbTriangles() + 1)], dtype=np.int64) - 1
    return pts, tris


def stl_topology_check(points: np.ndarray, tris: np.ndarray) -> dict[str, Any]:
    """Watertightness / manifoldness check on a triangle soup (after the
    vertex merging RWStl already performs)."""
    e = np.sort(np.concatenate([tris[:, [0, 1]], tris[:, [1, 2]], tris[:, [2, 0]]]), axis=1)
    _, counts = np.unique(e, axis=0, return_counts=True)
    boundary = int(np.sum(counts == 1))
    nonmanifold = int(np.sum(counts > 2))
    degenerate = int(np.sum(np.linalg.norm(np.cross(points[tris[:, 1]] - points[tris[:, 0]],
                                                    points[tris[:, 2]] - points[tris[:, 0]]), axis=1) < 1e-14))
    out = {"triangles": int(len(tris)), "boundary_edges": boundary, "nonmanifold_edges": nonmanifold,
           "degenerate_triangles": degenerate, "watertight": boundary == 0 and nonmanifold == 0}
    if len(tris):
        # orientation consistency: in a consistently oriented closed surface every directed edge appears once
        d = np.concatenate([tris[:, [0, 1]], tris[:, [1, 2]], tris[:, [2, 0]]])
        _, dc = np.unique(d, axis=0, return_counts=True)
        out["inconsistent_orientation_edges"] = int(np.sum(dc > 1))
        v = points[tris]
        out["signed_volume"] = float(np.einsum("ij,ij->i", v[:, 0], np.cross(v[:, 1], v[:, 2])).sum() / 6.0)
        out["bbox_size"] = float(np.ptp(points, axis=0).max())
    return out


def sew_to_solid(shape_or_faces, tolerance: float = 1e-6) -> TopoDS_Shape:
    sew = BRepBuilderAPI_Sewing(tolerance)
    if isinstance(shape_or_faces, list):
        for f in shape_or_faces:
            sew.Add(f)
    else:
        sew.Add(shape_or_faces)
    sew.Perform()
    sewn = sew.SewedShape()
    shells = index_map(sewn, TopAbs_SHELL)
    if shells.Extent() != 1:
        raise GeometryError(f"Sewing produced {shells.Extent()} shells; expected 1 closed shell.")
    shell = to_shell(shells.FindKey(1))
    if not BRep_Tool.IsClosed_s(shell):
        raise GeometryError("Sewn shell is not closed (surface has gaps).")
    mk = BRepBuilderAPI_MakeSolid(shell)
    solid = mk.Solid()
    fix = ShapeFix_Solid(solid)
    fix.Perform()
    solid = fix.Solid()
    if not BRepCheck_Analyzer(solid).IsValid():
        raise GeometryError("Reconstructed solid is not valid.")
    if _volume(solid) < 0:
        solid.Reverse()
    return solid


def solids_of(shape) -> list:
    """The individual solids of a (compound) shape, in file order."""
    from opencae.geometry._occ import to_solid
    m = index_map(shape, TopAbs_SOLID)
    return [to_solid(m.FindKey(i)) for i in range(1, m.Extent() + 1)]


def stl_components(points: np.ndarray, tris: np.ndarray) -> list[tuple[np.ndarray, np.ndarray]]:
    """Split a triangle mesh into edge-connected components (separate shells/parts).
    Components are only separated, never joined: gaps are not filled."""
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components
    n = len(tris)
    if n == 0:
        return []
    e = np.sort(np.concatenate([tris[:, [0, 1]], tris[:, [1, 2]], tris[:, [2, 0]]]), axis=1)
    owner = np.tile(np.arange(n), 3)
    _, inv = np.unique(e, axis=0, return_inverse=True)
    inv = inv.ravel()
    order = np.argsort(inv, kind="stable")
    a, b = owner[order][:-1], owner[order][1:]
    same = inv[order][:-1] == inv[order][1:]
    g = coo_matrix((np.ones(int(same.sum())), (a[same], b[same])), shape=(n, n))
    nc, lab = connected_components(g, directed=False)
    out = []
    for c in range(nc):
        t = tris[lab == c]
        used, newt = np.unique(t, return_inverse=True)
        out.append((points[used], newt.reshape(-1, 3)))
    out.sort(key=lambda pt: -len(pt[1]))
    return out


def stl_to_solid(points: np.ndarray, tris: np.ndarray, tolerance: Optional[float] = None) -> TopoDS_Shape:
    """Reconstruct a planar-faceted exact solid from a watertight STL. ``tolerance``:
    sewing tolerance (model units); default 1e-6 x model size."""
    chk = stl_topology_check(points, tris)
    if not chk["watertight"]:
        raise GeometryError(f"STL is not watertight ({chk['boundary_edges']} boundary edges, "
                            f"{chk['nonmanifold_edges']} non-manifold edges); cannot build a solid.")
    if len(tris) > 20000:
        raise GeometryError("STL too large for facet-to-solid reconstruction (> 20000 triangles).")
    faces = []
    for t in tris:
        a, b, c = points[t]
        if np.linalg.norm(np.cross(b - a, c - a)) < 1e-14:
            continue
        faces.append(_polygon_face([a, b, c]))
    tol = tolerance if tolerance else 1e-6 * max(1.0, float(np.ptp(points, axis=0).max()))
    solid = sew_to_solid(faces, tolerance=tol)
    return _unify(solid)


# --------------------------------------------------------------------------- inspection
def face_info(face, index: int) -> FaceInfo:
    p = GProp_GProps()
    BRepGProp.SurfaceProperties_s(face, p)
    ad = BRepAdaptor_Surface(face)
    stype = _enum_name(ad.GetType())
    normal = None
    if stype == "plane":
        d = _xyz(ad.Plane().Axis().Direction())
        if face.Orientation() == TopAbs_REVERSED:
            d = -d
        normal = d / np.linalg.norm(d)
    u0, u1, v0, v1 = ad.FirstUParameter(), ad.LastUParameter(), ad.FirstVParameter(), ad.LastVParameter()
    pts = []
    for uu in np.linspace(u0, u1, 5):
        for vv in np.linspace(v0, v1, 5):
            try:
                pts.append(_xyz(ad.Value(uu, vv)))
            except Exception:
                pass
    pts = np.array(pts) if pts else np.zeros((1, 3))
    return FaceInfo(index=index, surface_type=stype, area=p.Mass(), centroid=_xyz(p.CentreOfMass()),
                    normal=normal, bbox=(pts.min(axis=0), pts.max(axis=0)))


def edge_info(edge, index: int) -> EdgeInfo:
    p = GProp_GProps()
    BRepGProp.LinearProperties_s(edge, p)
    c = BRepAdaptor_Curve(edge)
    return EdgeInfo(index=index, curve_type=_enum_name(c.GetType()), length=p.Mass(),
                    centroid=_xyz(p.CentreOfMass()))


def check_shape(shape) -> BodyCheck:
    issues = []
    valid = bool(BRepCheck_Analyzer(shape).IsValid())
    if not valid:
        issues.append("BRepCheck reports an invalid shape.")
    solids = index_map(shape, TopAbs_SOLID).Extent()
    shells_map = index_map(shape, TopAbs_SHELL)
    closed = True
    for i in range(1, shells_map.Extent() + 1):
        if not BRep_Tool.IsClosed_s(to_shell(shells_map.FindKey(i))):
            closed = False
    free_edges = 0
    if not closed:
        issues.append("At least one shell is open (free edges present).")
        try:
            from OCP.ShapeAnalysis import ShapeAnalysis_FreeBounds
            fb = ShapeAnalysis_FreeBounds(shape)
            free_edges = index_map(fb.GetClosedWires(), TopAbs_EDGE).Extent() + \
                index_map(fb.GetOpenWires(), TopAbs_EDGE).Extent()
        except Exception:  # pragma: no cover
            free_edges = -1
    if solids == 0:
        issues.append("Shape contains no solid; it cannot be meshed with volume elements.")
    elif solids > 1:
        issues.append(f"Shape contains {solids} solids; they will be fragmented into a conformal "
                      "(bonded) mesh.")
    return BodyCheck(valid=valid, is_solid=solids >= 1, closed=closed, n_solids=solids,
                     n_shells=shells_map.Extent(), n_faces=index_map(shape, TopAbs_FACE).Extent(),
                     n_edges=index_map(shape, TopAbs_EDGE).Extent(),
                     n_vertices=index_map(shape, TopAbs_VERTEX).Extent(), free_edges=free_edges,
                     issues=issues)


def tessellate(shape, size_hint: float) -> Tessellation:
    lin = max(size_hint * 1e-3, 1e-7)
    BRepMesh_IncrementalMesh(shape, lin, False, 0.25, True)
    pts_all, tris_all, tri_face = [], [], []
    offset = 0
    for fi, face in enumerate(faces_of(shape)):
        loc = TopLoc_Location()
        tri = BRep_Tool.Triangulation_s(face, loc)
        if tri is None:
            continue
        trsf = loc.Transformation()
        pts = np.array([_xyz(tri.Node(i).Transformed(trsf)) for i in range(1, tri.NbNodes() + 1)])
        t = np.array([tri.Triangle(i).Get() for i in range(1, tri.NbTriangles() + 1)], dtype=np.int64) - 1
        if face.Orientation() == TopAbs_REVERSED:
            t = t[:, [0, 2, 1]]
        pts_all.append(pts)
        tris_all.append(t + offset)
        tri_face.append(np.full(len(t), fi, dtype=np.int64))
        offset += len(pts)
    edges = []
    for e in edges_of(shape):
        try:
            c = BRepAdaptor_Curve(e)
            d = GCPnts_TangentialDeflection(c, 0.1, lin * 2)
            edges.append(np.array([_xyz(d.Value(i)) for i in range(1, d.NbPoints() + 1)]))
        except Exception:  # degenerated edges (e.g. sphere poles)
            edges.append(np.zeros((0, 3)))
    verts = np.array([_xyz(BRep_Tool.Pnt_s(v)) for v in vertices_of(shape)]) if vertices_of(shape) else np.zeros((0, 3))
    return Tessellation(
        points=np.concatenate(pts_all) if pts_all else np.zeros((0, 3)),
        triangles=np.concatenate(tris_all) if tris_all else np.zeros((0, 3), dtype=np.int64),
        tri_face=np.concatenate(tri_face) if tri_face else np.zeros(0, dtype=np.int64),
        edge_polylines=edges, vertices=verts)


def build_body(body_id: str, name: str, shape, source_feature: str) -> Body:
    chk = check_shape(shape)
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    bb = Bnd_Box()
    BRepBndLib.Add_s(shape, bb, True)
    bmin, bmax = _xyz(bb.CornerMin()), _xyz(bb.CornerMax())
    size = float(np.linalg.norm(bmax - bmin)) or 1.0
    tess = tessellate(shape, size)
    if len(tess.points):
        bmin, bmax = tess.points.min(axis=0), tess.points.max(axis=0)
    gp = GProp_GProps()
    BRepGProp.SurfaceProperties_s(shape, gp)
    faces = [face_info(f, i) for i, f in enumerate(faces_of(shape))]
    edges = [edge_info(e, i) for i, e in enumerate(edges_of(shape))]
    verts = [VertexInfo(i, _xyz(BRep_Tool.Pnt_s(v))) for i, v in enumerate(vertices_of(shape))]
    return Body(id=body_id, name=name, kind="solid", shape=shape, tessellation=tess, faces=faces,
                edges=edges, vertices=verts, check=chk, volume=_volume(shape) if chk.is_solid else 0.0,
                area=gp.Mass(), bbox=(bmin, bmax), source_feature=source_feature)


def build_tessellated_body(body_id: str, name: str, pts: np.ndarray, tris: np.ndarray, src: str) -> Body:
    chk = stl_topology_check(pts, tris)
    issues = ["Tessellated (STL) body: not an exact CAD solid. Use 'Convert STL to solid' "
              "to reconstruct a faceted B-rep before meshing."]
    if not chk["watertight"]:
        issues.append(f"Not watertight: {chk['boundary_edges']} boundary and "
                      f"{chk['nonmanifold_edges']} non-manifold edges.")
    tess = Tessellation(points=pts, triangles=tris, tri_face=np.full(len(tris), -1), edge_polylines=[],
                        vertices=np.zeros((0, 3)))
    check = BodyCheck(valid=chk["watertight"], is_solid=False, closed=chk["watertight"], n_solids=0,
                      n_shells=0, n_faces=0, n_edges=0, n_vertices=0, free_edges=chk["boundary_edges"],
                      issues=issues)
    area = float(0.5 * np.linalg.norm(np.cross(pts[tris[:, 1]] - pts[tris[:, 0]],
                                                  pts[tris[:, 2]] - pts[tris[:, 0]]), axis=1).sum())
    return Body(id=body_id, name=name, kind="tessellated", shape=None, tessellation=tess, check=check,
                area=area, bbox=(pts.min(axis=0), pts.max(axis=0)), source_feature=src)


# --------------------------------------------------------------------------- kernel
class OccKernel:
    name = "OpenCascade (OCP)"

    def native_suffix(self) -> str:
        return ".brep"

    def regenerate(self, features: list[Feature], data_dir: Path, length_scale: float = 1.0,
                   names: Optional[dict[str, str]] = None) -> RegenerationResult:
        """Replay the feature history.

        ``length_scale`` converts millimetres (the unit OCCT uses when reading
        STEP/IGES) to the project length unit (1.0 for mm, 1e-3 for m).
        """
        names = names or {}
        shapes: dict[str, Any] = {}
        stl: dict[str, tuple[np.ndarray, np.ndarray]] = {}
        order: list[str] = []
        errors: dict[str, str] = {}
        src: dict[str, str] = {}
        default_names: dict[str, str] = {}

        def need(bid: str):
            if bid not in shapes:
                raise GeometryError(f"Referenced body '{names.get(bid, bid)}' does not exist "
                                    "(deleted, consumed by a boolean, or failed to build).")
            return shapes[bid]

        for f in features:
            if f.suppressed:
                continue
            p = f.params
            try:
                if f.kind == "primitive":
                    shapes[f.id] = make_primitive(p["shape"], p)
                    order.append(f.id)
                elif f.kind == "import":
                    path = Path(data_dir) / p["file"] if not Path(p["file"]).is_absolute() else Path(p["file"])
                    fmt = p.get("format") or path.suffix.lower().lstrip(".")
                    if not path.exists():
                        raise GeometryError(f"Imported file not found: {path}")
                    split = bool(p.get("split"))
                    base = f.name or path.stem
                    parts: list[tuple[str, Any]] = []  # (kind, shape or (pts, tris))
                    if fmt in ("step", "stp", "iges", "igs", "brep"):
                        shp = read_step(path) if fmt in ("step", "stp") else read_iges(path) if fmt in ("iges", "igs") \
                            else read_brep(path)
                        if fmt != "brep":
                            shp = _scale_shape(shp, length_scale)
                        sol = solids_of(shp) if split else []
                        parts = [("shape", x) for x in sol] if len(sol) > 1 else [("shape", shp)]
                    elif fmt == "stl":
                        pts, tris = read_stl(path)
                        comps = stl_components(pts, tris) if split else [(pts, tris)]
                        for cp, ct in comps:
                            if p.get("to_solid"):
                                parts.append(("shape", stl_to_solid(cp, ct, p.get("sew_tolerance"))))
                            else:
                                parts.append(("stl", (cp, ct)))
                    else:
                        raise GeometryError(f"Unsupported import format '{fmt}'.")
                    for k, (kind, obj) in enumerate(parts):
                        bid = f.id if len(parts) == 1 else f"{f.id}#{k + 1}"
                        if kind == "shape":
                            shapes[bid] = obj
                        else:
                            stl[bid] = obj
                        if len(parts) > 1:
                            default_names[bid] = f"{base}-{k + 1}"
                        order.append(bid)
                elif f.kind == "transform":
                    bid = p["body"]
                    if bid in stl:
                        raise GeometryError("Transform of tessellated (STL) bodies is not supported; "
                                            "convert to solid first.")
                    shapes[bid] = apply_transform(need(bid), p)
                elif f.kind == "boolean":
                    a, b = p["target"], p["tool"]
                    if a == b:
                        raise GeometryError("Boolean target and tool must differ.")
                    shapes[a] = apply_boolean(need(a), need(b), p.get("op", "union"))
                    del shapes[b]
                    order.remove(b)
                elif f.kind == "fillet":
                    shapes[p["body"]] = apply_fillet(need(p["body"]), float(p["radius"]), p.get("edges", "all"))
                elif f.kind == "chamfer":
                    shapes[p["body"]] = apply_chamfer(need(p["body"]), float(p["distance"]), p.get("edges", "all"))
                elif f.kind == "enclosure":
                    shapes[f.id] = make_enclosure([need(b) for b in p["bodies"]], p)
                    order.append(f.id)
                elif f.kind == "extrude":
                    prism = make_extrusion(p)
                    target = p.get("fuse_with")
                    if target:
                        shapes[target] = apply_boolean(need(target), prism, "union")
                    else:
                        shapes[f.id] = prism
                        order.append(f.id)
                else:
                    raise GeometryError(f"Unknown feature kind '{f.kind}'.")
            except (GeometryError, KeyError, ValueError, TypeError, RuntimeError) as exc:
                msg = str(exc) if isinstance(exc, GeometryError) else f"{type(exc).__name__}: {exc}"
                errors[f.id] = msg
                log.error("Feature '%s' (%s) failed: %s", f.name or f.id, f.kind, msg)

        bodies: dict[str, Body] = {}
        for bid in order:
            nm = names.get(bid) or default_names.get(bid) or bid
            try:
                if bid in stl:
                    bodies[bid] = build_tessellated_body(bid, nm, *stl[bid], src=bid)
                else:
                    bodies[bid] = build_body(bid, nm, shapes[bid], src.get(bid, bid))
            except Exception as exc:  # pragma: no cover - defensive
                errors[bid] = f"Body evaluation failed: {exc}"
        return RegenerationResult(bodies=bodies, errors=errors, order=[b for b in order if b in bodies])

    # ------------------------------------------------------------------ export
    def write_native(self, body: Body, path: Path) -> Path:
        path = Path(path).with_suffix(".brep")
        path.parent.mkdir(parents=True, exist_ok=True)
        if body.shape is None:
            raise GeometryError("Body has no exact geometry (tessellated).")
        if not BRepTools.Write_s(body.shape, str(path)):
            raise GeometryError(f"Could not write {path}")
        return path

    def write_face(self, body: Body, face_index: int, path: Path) -> Path:
        """Write one face of a body as a B-rep (2-D flow meshing of a planar section)."""
        path = Path(path).with_suffix(".brep")
        path.parent.mkdir(parents=True, exist_ok=True)
        if body.shape is None:
            raise GeometryError("Body has no exact geometry (tessellated).")
        faces = faces_of(body.shape)
        if not 0 <= face_index < len(faces):
            raise GeometryError(f"Face {face_index + 1} does not exist on '{body.name}'.")
        if not BRepTools.Write_s(faces[face_index], str(path)):
            raise GeometryError(f"Could not write {path}")
        return path

    def export(self, bodies: list[Body], path: Path, fmt: str, length_unit: str = "mm") -> None:
        path = Path(path)
        fmt = fmt.lower()
        to_mm = 1000.0 if length_unit == "m" else 1.0
        exact = [b for b in bodies if b.shape is not None]
        if fmt in ("step", "stp", "iges", "igs", "brep"):
            if len(exact) != len(bodies):
                raise GeometryError("Tessellated (STL) bodies cannot be exported as exact CAD.")
            comp = TopoDS_Compound()
            bld = BRep_Builder()
            bld.MakeCompound(comp)
            for b in exact:
                bld.Add(comp, b.shape)
            shape = comp if len(exact) > 1 else exact[0].shape
            if fmt in ("step", "stp"):
                Interface_Static.SetCVal_s("xstep.cascade.unit", "MM")
                Interface_Static.SetCVal_s("write.step.unit", "MM")
                w = STEPControl_Writer()
                w.Transfer(_scale_shape(shape, to_mm), STEPControl_AsIs)
                if w.Write(str(path)) != IFSelect_RetDone:
                    raise GeometryError(f"STEP export to {path} failed.")
            elif fmt in ("iges", "igs"):
                Interface_Static.SetCVal_s("write.iges.unit", "MM")
                w = IGESControl_Writer("MM", 1)
                w.AddShape(_scale_shape(shape, to_mm))
                w.ComputeModel()
                if not w.Write(str(path)):
                    raise GeometryError(f"IGES export to {path} failed.")
            else:
                if not BRepTools.Write_s(shape, str(path)):
                    raise GeometryError(f"BREP export to {path} failed.")
        elif fmt == "stl":
            pts, tris, off = [], [], 0
            for b in bodies:
                t = b.tessellation
                pts.append(t.points)
                tris.append(t.triangles + off)
                off += len(t.points)
            _write_stl_binary(path, np.concatenate(pts), np.concatenate(tris))
        else:
            raise GeometryError(f"Unsupported export format '{fmt}'.")
        log.info("Exported %d bodies to %s", len(bodies), path)


def _write_stl_binary(path: Path, pts: np.ndarray, tris: np.ndarray) -> None:
    a, b, c = pts[tris[:, 0]], pts[tris[:, 1]], pts[tris[:, 2]]
    n = np.cross(b - a, c - a)
    nn = np.linalg.norm(n, axis=1, keepdims=True)
    n = np.divide(n, nn, out=np.zeros_like(n), where=nn > 0)
    rec = np.zeros(len(tris), dtype=[("n", "<f4", 3), ("a", "<f4", 3), ("b", "<f4", 3), ("c", "<f4", 3),
                                     ("attr", "<u2")])
    rec["n"], rec["a"], rec["b"], rec["c"] = n, a, b, c
    with open(path, "wb") as fh:
        fh.write(b"OpenCAE Studio binary STL".ljust(80, b" "))
        fh.write(np.uint32(len(tris)).tobytes())
        fh.write(rec.tobytes())


def copy_into_project(src: Path, data_dir: Path) -> str:
    """Copy an imported CAD file into the project data dir; return relative path."""
    dst_dir = Path(data_dir) / "geometry" / "imports"
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / Path(src).name
    i = 1
    while dst.exists() and dst.resolve() != Path(src).resolve():
        dst = dst_dir / f"{Path(src).stem}_{i}{Path(src).suffix}"
        i += 1
    if dst.resolve() != Path(src).resolve():
        shutil.copy2(src, dst)
    return str(dst.relative_to(data_dir))


def make_enclosure(solids: list, p: dict[str, Any]):
    """Fluid domain for external flow: an axis-aligned box around the solids minus the solids.

    ``axis`` ("+x", "-z", ...) is the flow direction; ``upstream``, ``downstream`` and
    ``lateral`` are distances as multiples of the largest body dimension. The solids are
    not consumed (they stay available for structural or thermal studies).

    ``symmetry`` ("+y", "-z", ..., or "" for none) puts that side of the box exactly on the
    bodies' bounding box (e.g. the root plane of a half-wing), so the model can use a symmetry
    plane there instead of modelling the mirror half."""
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    if not solids:
        raise GeometryError("Select at least one body to enclose.")
    bb = Bnd_Box()
    for sh in solids:
        BRepBndLib.Add_s(sh, bb, False)
    lo, hi = _xyz(bb.CornerMin()), _xyz(bb.CornerMax())
    size = float(np.max(hi - lo))
    axis = str(p.get("axis", "+x")).lower()
    k = "xyz".index(axis[-1])
    sgn = -1.0 if axis.startswith("-") else 1.0
    lat = float(p.get("lateral", 2.0)) * size
    up = float(p.get("upstream", 3.0)) * size
    down = float(p.get("downstream", 6.0)) * size
    a, b = lo - lat, hi + lat
    if sgn > 0:
        a[k], b[k] = lo[k] - up, hi[k] + down
    else:
        a[k], b[k] = lo[k] - down, hi[k] + up
    sym = str(p.get("symmetry", "") or "").lower()
    if sym:
        ks = "xyz".index(sym[-1])
        if ks == k:
            raise GeometryError("The symmetry plane cannot be normal to the flow direction.")
        if sym.startswith("-"):
            a[ks] = lo[ks]
        else:
            b[ks] = hi[ks]
    box = BRepPrimAPI_MakeBox(_pnt(a), _pnt(b)).Shape()
    for sh in solids:
        box = apply_boolean(box, sh, "subtract")
    return box


def pair_relations(bodies: list[Body], tol: float) -> list[dict]:
    """Interference / contact / gap between every pair of solid bodies.

    ``overlap_volume`` > 0: the solids interpenetrate (a bonded mesh would be wrong).
    ``distance`` <= tol: the bodies touch and are bonded when meshed together.
    Otherwise they are separate and each needs its own supports in a study.
    Nothing is modified."""
    from OCP.BRepExtrema import BRepExtrema_DistShapeShape
    out = []
    solids = [b for b in bodies if b.kind == "solid" and b.shape is not None]
    for i in range(len(solids)):
        for j in range(i + 1, len(solids)):
            a, b = solids[i], solids[j]
            (amin, amax), (bmin, bmax) = a.bbox, b.bbox
            gap_bb = float(np.max(np.maximum(np.asarray(bmin) - amax, np.asarray(amin) - bmax)))
            rec = {"a": a.id, "b": b.id, "a_name": a.name, "b_name": b.name}
            if gap_bb > tol:
                rec.update(relation="separate", distance=None, overlap_volume=0.0)
                out.append(rec)
                continue
            d = BRepExtrema_DistShapeShape(a.shape, b.shape)
            dist = float(d.Value()) if d.IsDone() else float("nan")
            ov = 0.0
            if dist <= tol:
                try:
                    ov = max(0.0, _volume(_check_done(BRepAlgoAPI_Common(a.shape, b.shape), "common")))
                except Exception:  # pragma: no cover
                    ov = float("nan")
            small = 1e-9 * min(a.volume, b.volume)
            rel = "overlapping" if ov > small else "touching" if dist <= tol else "separate"
            rec.update(relation=rel, distance=dist, overlap_volume=ov)
            out.append(rec)
    return out


def touching_faces(a: Body, b: Body, tol: float) -> list[tuple[int, int, float]]:
    """Face pairs (index on a, index on b, shared area) where the boundaries of two solids
    coincide: candidate interaction faces. Faces must be within ``tol`` of each other and
    share a surface patch of non-zero area (edge or point contact is ignored). Nothing is
    modified."""
    from OCP.BRepExtrema import BRepExtrema_DistShapeShape
    out = []
    fa, fb = faces_of(a.shape), faces_of(b.shape)
    for i, (fi, ia) in enumerate(zip(fa, a.faces)):
        for j, (fj, ib) in enumerate(zip(fb, b.faces)):
            (lo1, hi1), (lo2, hi2) = ia.bbox, ib.bbox
            if np.any(np.asarray(lo2) - hi1 > tol) or np.any(np.asarray(lo1) - hi2 > tol):
                continue
            if ia.normal is not None and ib.normal is not None and float(np.dot(ia.normal, ib.normal)) > -1 + 1e-6:
                continue  # planar faces in contact have opposite outward normals
            d = BRepExtrema_DistShapeShape(fi, fj)
            if not d.IsDone() or d.Value() > tol:
                continue
            try:
                common = _check_done(BRepAlgoAPI_Common(fi, fj), "common")
                p = GProp_GProps()
                BRepGProp.SurfaceProperties_s(common, p)
                area = float(p.Mass())
            except GeometryError:
                area = 0.0
            if area > 1e-6 * min(ia.area, ib.area):
                out.append((i, j, area))
    return out


def face_adjacency(shape) -> dict[int, set[int]]:
    """Face index -> indices of faces sharing an edge (index order of faces_of / MapShapes)."""
    from opencae.geometry._occ import IndexedDataMapShapeList
    fmap = index_map(shape, TopAbs_FACE)
    emap = IndexedDataMapShapeList()
    TopExp.MapShapesAndAncestors_s(shape, TopAbs_EDGE, TopAbs_FACE, emap)
    adj: dict[int, set[int]] = {i: set() for i in range(fmap.Extent())}
    for i in range(1, emap.Extent() + 1):
        fl = [fmap.FindIndex(f) - 1 for f in emap.FindFromIndex(i)]
        fl = [x for x in fl if x >= 0]
        for a in fl:
            for b in fl:
                if a != b:
                    adj[a].add(b)
    return adj


def _face_normal_at_edge(face, edge) -> Optional[tuple[np.ndarray, np.ndarray]]:
    """(point, outward normal) of a face at the middle of one of its edges."""
    from OCP.BRepLProp import BRepLProp_SLProps
    from OCP.ShapeAnalysis import ShapeAnalysis_Surface
    try:
        c = BRepAdaptor_Curve(edge)
        p = c.Value(0.5 * (c.FirstParameter() + c.LastParameter()))
        surf = BRep_Tool.Surface_s(face)
        uv = ShapeAnalysis_Surface(surf).ValueOfUV(p, 1e-6)
        props = BRepLProp_SLProps(BRepAdaptor_Surface(face), uv.X(), uv.Y(), 1, 1e-6)
        if not props.IsNormalDefined():
            return None
        n = _xyz(props.Normal())
        if face.Orientation() == TopAbs_REVERSED:
            n = -n
        return _xyz(p), n / np.linalg.norm(n)
    except Exception:  # pragma: no cover - degenerate geometry
        return None


def tangent_faces(shape, start: int, angle_deg: float = 10.0) -> list[int]:
    """Faces reachable from ``start`` across edges where the surfaces meet smoothly (normals
    within ``angle_deg``): e.g. the whole skin of a wing made of several patches, without
    the sharp trailing edge or the end caps."""
    from opencae.geometry._occ import IndexedDataMapShapeList
    faces = faces_of(shape)
    fmap = index_map(shape, TopAbs_FACE)
    emap = IndexedDataMapShapeList()
    TopExp.MapShapesAndAncestors_s(shape, TopAbs_EDGE, TopAbs_FACE, emap)
    cos_tol = math.cos(math.radians(angle_deg))
    smooth: dict[int, set[int]] = {i: set() for i in range(len(faces))}
    for i in range(1, emap.Extent() + 1):
        edge = to_edge(emap.FindKey(i))
        fl = [fmap.FindIndex(f) - 1 for f in emap.FindFromIndex(i)]
        fl = sorted({x for x in fl if x >= 0})
        if len(fl) != 2:
            continue
        a, b = fl
        na, nb = _face_normal_at_edge(faces[a], edge), _face_normal_at_edge(faces[b], edge)
        if na is None or nb is None:
            continue
        if float(na[1] @ nb[1]) >= cos_tol:
            smooth[a].add(b)
            smooth[b].add(a)
    seen, todo = {start}, [start]
    while todo:
        f = todo.pop()
        for g in smooth[f]:
            if g not in seen:
                seen.add(g)
                todo.append(g)
    return sorted(seen)
