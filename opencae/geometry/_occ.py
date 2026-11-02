"""Version shims for OCP (OpenCascade Python bindings) 7.7 - 8.x."""
from __future__ import annotations

import OCP
from OCP.TopoDS import TopoDS

try:  # OCP >= 7.8 / 8.x
    IndexedMapOfShape = OCP.OCP.collections.IndexedMap_TopoDS_Shape_TopTools_ShapeMapHasher  # type: ignore[attr-defined]
except AttributeError:  # pragma: no cover - OCP 7.7
    from OCP.TopTools import TopTools_IndexedMapOfShape as IndexedMapOfShape  # type: ignore

_c = getattr(OCP.OCP, "collections", None)
IndexedDataMapShapeList = None
for _n in ("IndexedDataMap_TopoDS_Shape_List_TopoDS_Shape_TopTools_ShapeMapHasher",
           "IndexedDataMap_TopoDS_Shape_NCollection_List_TopoDS_Shape_TopTools_ShapeMapHasher"):
    IndexedDataMapShapeList = IndexedDataMapShapeList or getattr(_c, _n, None)
if IndexedDataMapShapeList is None:  # pragma: no cover - OCP 7.7
    try:
        from OCP.TopTools import TopTools_IndexedDataMapOfShapeListOfShape as IndexedDataMapShapeList  # type: ignore
    except ImportError:
        IndexedDataMapShapeList = None  # type: ignore


def _cast(name: str):
    fn = getattr(TopoDS, name, None) or getattr(TopoDS, name + "_s")
    return fn


to_face = _cast("Face")
to_edge = _cast("Edge")
to_vertex = _cast("Vertex")
to_shell = _cast("Shell")
to_solid = _cast("Solid")
to_wire = _cast("Wire")
