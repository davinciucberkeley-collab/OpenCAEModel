"""Validation and re-resolution of topological references.

A load or constraint stores ``EntityRef(kind, body, index, signature)``. After
any regeneration the referenced entity is checked:

1. If the entity at ``index`` still matches the stored signature -> OK.
2. Otherwise a unique entity with a matching signature is searched for
   (topology may have been renumbered by an upstream edit).
3. If no unique match exists the reference is reported as *broken*; it is
   never silently re-attached to a "nearby" entity.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np

from opencae.geometry.kernel import Body
from opencae.project.model import EntityRef

REL_TOL = 1e-6


@dataclass
class RefStatus:
    ok: bool
    index: int
    message: str = ""
    remapped: bool = False


def make_ref(body: Body, kind: str, index: int) -> EntityRef:
    if kind == "face":
        sig = body.faces[index].signature()
    elif kind == "edge":
        sig = body.edges[index].signature()
    elif kind == "vertex":
        sig = body.vertices[index].signature()
    elif kind == "body":
        sig = {}
    else:
        raise ValueError(kind)
    return EntityRef(kind=kind, body=body.id, index=index, signature=sig)


def _close(a, b, scale: float) -> bool:
    return float(np.linalg.norm(np.asarray(a, float) - np.asarray(b, float))) <= max(scale, 1e-12) * 1e-5


def _matches(body: Body, kind: str, idx: int, sig: dict) -> bool:
    L = body.size or 1.0
    if kind == "face":
        f = body.faces[idx]
        if f.surface_type != sig.get("type"):
            return False
        if abs(f.area - sig["area"]) > REL_TOL * max(sig["area"], L * L * 1e-9) * 10:
            return False
        if not _close(f.centroid, sig["centroid"], L):
            return False
        if (f.normal is None) != (sig.get("normal") is None):
            return False
        if f.normal is not None and float(np.dot(f.normal, sig["normal"])) < 1 - 1e-6:
            return False
        return True
    if kind == "edge":
        e = body.edges[idx]
        return e.curve_type == sig.get("type") and abs(e.length - sig["length"]) <= 1e-5 * max(L, 1) \
            and _close(e.centroid, sig["centroid"], L)
    if kind == "vertex":
        return _close(body.vertices[idx].point, sig["point"], L)
    return True


def _count(body: Body, kind: str) -> int:
    return {"face": len(body.faces), "edge": len(body.edges), "vertex": len(body.vertices)}.get(kind, 1)


def resolve_ref(ref: EntityRef, bodies: dict[str, Body]) -> RefStatus:
    body: Optional[Body] = bodies.get(ref.body)
    if body is None:
        return RefStatus(False, ref.index, "referenced body no longer exists")
    if ref.kind == "body":
        return RefStatus(True, -1)
    if body.kind != "solid":
        return RefStatus(False, ref.index, "body is tessellated; topology references unavailable")
    n = _count(body, ref.kind)
    if not ref.signature:
        ok = 0 <= ref.index < n
        return RefStatus(ok, ref.index, "" if ok else "index out of range")
    if 0 <= ref.index < n and _matches(body, ref.kind, ref.index, ref.signature):
        return RefStatus(True, ref.index)
    cands = [i for i in range(n) if _matches(body, ref.kind, i, ref.signature)]
    if len(cands) == 1:
        return RefStatus(True, cands[0], f"{ref.kind} renumbered {ref.index + 1} -> {cands[0] + 1}", True)
    if not cands:
        return RefStatus(False, ref.index, f"{ref.kind} {ref.index + 1} changed shape/position; "
                                          "please re-select it")
    return RefStatus(False, ref.index, f"{ref.kind} reference is ambiguous ({len(cands)} candidates)")


def find_planar_face(body: Body, normal, point, tol: float = 1e-6) -> int:
    """Index of the unique planar face with the given outward normal whose plane
    contains ``point`` (used by templates, e.g. cantilever end faces)."""
    n = np.asarray(normal, float) / np.linalg.norm(normal)
    p = np.asarray(point, float)
    L = body.size or 1.0
    hits = [f.index for f in body.faces if f.normal is not None and np.dot(f.normal, n) > 1 - 1e-9
            and abs(np.dot(f.centroid - p, n)) <= tol * L]
    if len(hits) != 1:
        raise LookupError(f"Expected one planar face with normal {normal} through {point}, found {len(hits)}")
    return hits[0]
