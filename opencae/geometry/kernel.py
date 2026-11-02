"""Kernel-neutral geometry interface.

The rest of the application only uses the dataclasses and the
:class:`GeometryKernel` protocol defined here, so the OpenCascade backend
(:mod:`opencae.geometry.occ_kernel`) can be swapped for another B-rep kernel.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Protocol

import numpy as np

from opencae.project.model import Feature


class GeometryError(Exception):
    pass


@dataclass
class FaceInfo:
    index: int
    surface_type: str
    area: float
    centroid: np.ndarray
    normal: Optional[np.ndarray]  # outward unit normal for planar faces, else None
    bbox: tuple[np.ndarray, np.ndarray]

    def signature(self) -> dict[str, Any]:
        return {
            "type": self.surface_type,
            "area": float(self.area),
            "centroid": [float(v) for v in self.centroid],
            "normal": None if self.normal is None else [float(v) for v in self.normal],
        }


@dataclass
class EdgeInfo:
    index: int
    curve_type: str
    length: float
    centroid: np.ndarray

    def signature(self) -> dict[str, Any]:
        return {"type": self.curve_type, "length": float(self.length),
                "centroid": [float(v) for v in self.centroid]}


@dataclass
class VertexInfo:
    index: int
    point: np.ndarray

    def signature(self) -> dict[str, Any]:
        return {"point": [float(v) for v in self.point]}


@dataclass
class BodyCheck:
    valid: bool
    is_solid: bool
    closed: bool
    n_solids: int
    n_shells: int
    n_faces: int
    n_edges: int
    n_vertices: int
    free_edges: int
    issues: list[str] = field(default_factory=list)


@dataclass
class Tessellation:
    points: np.ndarray  # (N,3)
    triangles: np.ndarray  # (T,3) int
    tri_face: np.ndarray  # (T,) face index per triangle (-1 for STL)
    edge_polylines: list[np.ndarray]  # one (n,3) array per edge
    vertices: np.ndarray  # (V,3)


@dataclass
class Body:
    id: str
    name: str
    kind: str  # "solid" (exact B-rep) or "tessellated" (STL, not an exact solid)
    shape: Any  # kernel-specific object (TopoDS_Shape) or None
    tessellation: Optional[Tessellation] = None
    faces: list[FaceInfo] = field(default_factory=list)
    edges: list[EdgeInfo] = field(default_factory=list)
    vertices: list[VertexInfo] = field(default_factory=list)
    check: Optional[BodyCheck] = None
    volume: float = 0.0
    area: float = 0.0
    bbox: tuple[np.ndarray, np.ndarray] = field(default_factory=lambda: (np.zeros(3), np.zeros(3)))
    source_feature: str = ""

    @property
    def size(self) -> float:
        return float(np.linalg.norm(self.bbox[1] - self.bbox[0]))


@dataclass
class RegenerationResult:
    bodies: dict[str, Body]
    errors: dict[str, str]  # feature id -> message
    order: list[str]  # body ids in creation order


class GeometryKernel(Protocol):
    name: str

    def regenerate(self, features: list[Feature], data_dir: Path, length_scale: float,
                   names: dict[str, str]) -> RegenerationResult: ...

    def export(self, bodies: list[Body], path: Path, fmt: str, length_unit: str) -> None: ...

    def write_native(self, body: Body, path: Path) -> Path: ...

    def native_suffix(self) -> str: ...
