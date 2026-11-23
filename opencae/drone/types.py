"""Shared types of the generic drone pipeline.

Every module in ``opencae.drone`` (and the CLI, the dashboard server and the app) talks through these types.
They are deliberately small and JSON-friendly: an analysis returns a plain ``dict`` built with
:func:`result`, and the pipeline writes those dicts to ``results.json``.

Evidence rules (they apply to every analysis):

* A metric is ``ok``/``watch``/``fail`` only when it was computed. An input that is missing makes the analysis
  ``not_evaluated`` and lists the missing parameter in ``missing``; it is never filled with a guess.
* A result that rests on an input whose status is ``estimate`` can at best be ``conditional`` in the
  requirement checks. Only ``measured`` or ``datasheet`` inputs can support a ``pass``.
* A solver failure is reported as ``error`` with the log tail; it is never hidden or replaced by a number.
* No result here is flight approval.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import numpy as np

# ------------------------------------------------------------------------------------------------ statuses
OK, WATCH, FAIL = "ok", "watch", "fail"
CONDITIONAL, NOT_EVALUATED, ERROR = "conditional", "not_evaluated", "error"
STATUSES = (OK, WATCH, FAIL, CONDITIONAL, NOT_EVALUATED, ERROR)

# input-parameter evidence levels (weakest first)
ESTIMATE, DATASHEET, MEASURED, USER = "estimate", "datasheet", "measured", "user"
EVIDENCE = (ESTIMATE, DATASHEET, MEASURED, USER)  # USER = a choice (mission time, budget), not a property

ROLES = ("frame", "arm", "motor", "prop", "battery", "wing", "fin", "control_surface", "landing", "payload",
         "electronics", "other")
TYPES = ("multirotor", "fixed_wing", "tailsitter", "vtol")
ANALYSES = ("mass", "propulsion", "structure", "cfd", "flight")  # run order; "checks" always runs last


@dataclass
class Param:
    """One input value from drone.json with its evidence level."""
    value: object
    unit: str = ""
    status: str = ESTIMATE
    source: str = ""

    def as_dict(self) -> dict:
        return {"value": self.value, "unit": self.unit, "status": self.status, "source": self.source}


@dataclass
class Part:
    """One body of the imported airframe, in the aircraft frame (x forward, y left, z up; right-handed), millimetres."""
    name: str
    role: str
    points: np.ndarray            # (n, 3) float64, mm
    triangles: np.ndarray         # (m, 3) int64
    material: Optional[str] = None  # key into spec["materials"], from the role's material mapping

    @property
    def bbox(self) -> tuple[np.ndarray, np.ndarray]:
        return self.points.min(0), self.points.max(0)

    @property
    def centroid(self) -> np.ndarray:
        return self.points.mean(0)


@dataclass
class Aircraft:
    """The imported geometry, every part assigned a role."""
    name: str
    parts: list[Part]
    source_file: str = ""

    def role_parts(self, role: str) -> list[Part]:
        return [p for p in self.parts if p.role == role]

    @property
    def bbox(self) -> tuple[np.ndarray, np.ndarray]:
        lo = np.min([p.points.min(0) for p in self.parts], axis=0)
        hi = np.max([p.points.max(0) for p in self.parts], axis=0)
        return lo, hi

    def surface(self, roles: Optional[tuple] = None) -> tuple[np.ndarray, np.ndarray]:
        """All (or the given roles') triangles merged into one (points mm, triangles) pair."""
        P, T, off = [], [], 0
        for p in self.parts:
            if roles is None or p.role in roles:
                P.append(p.points)
                T.append(p.triangles + off)
                off += len(p.points)
        if not P:
            return np.zeros((0, 3)), np.zeros((0, 3), dtype=np.int64)
        return np.vstack(P), np.vstack(T)


@dataclass
class RunContext:
    """What an analysis receives. ``workdir`` is scratch (deleted after the run unless keep_cases);
    ``out_dir`` keeps the results (results.json, compact field files)."""
    project_dir: Path
    spec: dict
    aircraft: Aircraft
    workdir: Path
    out_dir: Path
    previous: dict = field(default_factory=dict)  # results of the analyses already run in this pass, by id
    progress: Callable[[str, float], None] = lambda msg, frac: None
    cancelled: Callable[[], bool] = lambda: False
    keep_cases: bool = False


def metric(value, unit: str = "", status: str = OK, basis: str = "", label: str = "") -> dict:
    """One computed quantity. ``basis`` lists the input statuses it rests on (e.g. "estimate")."""
    v = None if value is None else (float(value) if isinstance(value, (int, float, np.floating, np.integer))
                                    else value)
    return {"value": v, "unit": unit, "status": status, "basis": basis, "label": label}


def result(id: str, title: str, status: str, summary: str, metrics: Optional[dict] = None,
           missing: Optional[list] = None, fields: Optional[list] = None, solver: Optional[dict] = None,
           log: str = "", extra: Optional[dict] = None) -> dict:
    """The record every analysis returns.

    ``summary`` is one or two plain-language sentences for the dashboard (no jargon without explanation).
    ``fields`` lists contour files written to out_dir: [{"file", "key", "quantity", "unit", "part"?}].
    ``solver`` = {"name", "converged": bool|None, "iterations", "cells", "runtime_s", ...}."""
    assert status in STATUSES, status
    return {"id": id, "title": title, "status": status, "summary": summary, "metrics": metrics or {},
            "missing": list(missing or []), "fields": list(fields or []), "solver": solver or {},
            "log": log[-4000:] if log else "", "extra": extra or {}}


def not_evaluated(id: str, title: str, missing: list, why: str = "") -> dict:
    return result(id, title, NOT_EVALUATED,
                  why or ("Not evaluated: these inputs are missing from the drone file: " + ", ".join(missing)),
                  missing=missing)


def save_field(path: Path, values: np.ndarray, points: Optional[np.ndarray] = None,
               triangles: Optional[np.ndarray] = None, displacement: Optional[np.ndarray] = None,
               **extra_arrays) -> int:
    """Write a contour field compactly (float16 values, float32 points, compressed). Returns bytes written."""
    arrs = {"values": np.asarray(values, dtype=np.float16)}
    if points is not None:
        arrs["points"] = np.asarray(points, dtype=np.float32)
    if triangles is not None:
        arrs["triangles"] = np.asarray(triangles, dtype=np.int32)
    if displacement is not None:
        arrs["displacement"] = np.asarray(displacement, dtype=np.float16)
    arrs.update({k: np.asarray(v) for k, v in extra_arrays.items()})
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(path, **arrs)
    return path.stat().st_size


def load_field(path: Path) -> dict:
    with np.load(path) as z:
        return {k: (z[k].astype(np.float32) if z[k].dtype == np.float16 else z[k]) for k in z.files}
