"""drone.json (format ``opencae-drone/1``): load, save, read a value with its evidence, edit by dotted key.

A numeric leaf is a bare number (evidence ``estimate``; inside a material it inherits the material's own
``status``) or ``{"value", "unit", "status", "source"}``. ``null`` means "not filled in yet": the analyses that
need it report ``not_evaluated`` and name the key.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
from pathlib import Path
from typing import Optional

from opencae.drone.types import ANALYSES, ESTIMATE, EVIDENCE, ROLES, TYPES, Param

FORMAT = "opencae-drone/1"
FILE_NAME = "drone.json"
UNITS = ("mm", "m", "in")
AXES = ("+x", "-x", "+y", "-y", "+z", "-z")
OPS = (">=", "<=", ">", "<")
ENGINES = ("generic", "k09l")
_RANK = {s: i for i, s in enumerate(EVIDENCE)}


def _file(path) -> Path:
    p = Path(path)
    return p / FILE_NAME if p.is_dir() else p


def load(path) -> dict:
    """Read drone.json (``path`` is the project folder or the file itself)."""
    p = _file(path)
    if not p.exists():
        raise FileNotFoundError(f"no drone.json at {p}")
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{p} is not valid JSON: {exc}") from exc


def save(spec: dict, path) -> Path:
    """Write drone.json (atomically: the old file stays if writing fails)."""
    p = _file(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(spec, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, p)
    return p


# ------------------------------------------------------------------------------------------------ navigation
def _step(node, seg: str):
    if isinstance(node, dict):
        if seg in node:
            return node[seg]
    elif isinstance(node, list):
        try:
            return node[int(seg)]
        except (ValueError, IndexError):
            pass
    raise KeyError(seg)


def _get(spec: dict, dotted: str):
    """(value, parent) at a dotted key; KeyError when the path does not exist."""
    node, parent = spec, None
    for seg in dotted.split("."):
        parent, node = node, _step(node, seg)
    return node, parent


def get(spec: dict, dotted: str, default=None):
    """The raw JSON at a dotted key, or ``default`` when absent."""
    try:
        return _get(spec, dotted)[0]
    except KeyError:
        return default


def _is_num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def param(spec: dict, dotted: str) -> Optional[Param]:
    """The value at a dotted key as a Param (value, unit, status, source); None when absent or null."""
    try:
        node, parent = _get(spec, dotted)
    except KeyError:
        return None
    if node is None:
        return None
    if isinstance(node, dict):
        if "value" not in node or node["value"] is None:
            return None
        st = node.get("status", ESTIMATE)
        return Param(node["value"], node.get("unit", ""), st if st in EVIDENCE else ESTIMATE,
                     node.get("source", ""))
    if isinstance(node, list):
        return None
    st, src = ESTIMATE, ""
    if isinstance(parent, dict) and parent.get("status") in EVIDENCE:  # a material: status/source of the whole
        st, src = parent["status"], parent.get("source", "")
    return Param(node, "", st, src)


def val(spec: dict, dotted: str, default=None):
    """Just the value of :func:`param` (``default`` when absent or null)."""
    p = param(spec, dotted)
    return default if p is None else p.value


def weakest(statuses) -> str:
    """The weakest evidence level of the given statuses (estimate < datasheet < measured < user)."""
    st = [s for s in statuses if s in _RANK]
    return min(st, key=_RANK.__getitem__) if st else ESTIMATE


def basis(spec: dict, keys) -> str:
    """The weakest evidence among the parameters at ``keys`` that are present."""
    return weakest([p.status for k in keys for p in [param(spec, k)] if p is not None])


# ------------------------------------------------------------------------------------------------ ranges
# Where physics says a number cannot be negative or zero.
_POSITIVE = {"propulsion.motor.kv", "propulsion.motor.r_ohm", "propulsion.motor.i_max_a", "propulsion.motor.mass_g",
             "propulsion.prop.diameter_in", "propulsion.prop.pitch_in", "propulsion.prop.ct", "propulsion.prop.cp",
             "propulsion.battery.cells", "propulsion.battery.capacity_mah", "propulsion.battery.c_rating",
             "propulsion.battery.r_int_mohm_per_cell", "propulsion.esc.i_cont_a", "aero.cruise_speed_ms",
             "aero.ref_area_m2", "aero.ref_chord_m", "environment.rho", "environment.g", "analyses.cfd.speed_ms",
             "analyses.cfd.iterations", "analyses.structure.load_factor"}
_NONNEG = {"propulsion.motor.i0_a", "aero.cd0", "mission.hover_s", "mission.cruise_s", "mission.payload_g",
           "mass.payload_g"}
_FRACTION = {"propulsion.battery.usable_fraction", "propulsion.esc.efficiency"}
_INTEGER = {"propulsion.battery.cells", "analyses.cfd.iterations"}
_ANGLE = {"aero.alpha_deg": (-90.0, 90.0), "analyses.cfd.alpha_deg": (-90.0, 90.0),
          "analyses.flight.disturbance_deg": (0.0, 90.0)}


def limits(key: str):
    """(low, high, low_is_open, integer) for a key; None for no limit. 'Open' means the value must be above it."""
    parts = key.split(".")
    if key in _POSITIVE:
        return 0.0, None, True, key in _INTEGER
    if key in _FRACTION:
        return 0.0, 1.0, True, False
    if key in _NONNEG or key.startswith("mass.parts."):
        return 0.0, None, False, False
    if key in _ANGLE:
        lo, hi = _ANGLE[key]
        return lo, hi, False, False
    if parts[0] == "materials" and len(parts) == 3:
        if parts[2] == "poisson":
            return 0.0, 0.5, True, False
        if parts[2] in ("density_kg_m3", "youngs_modulus_gpa", "yield_strength_mpa"):
            return 0.0, None, True, False
    return None


def is_integer_key(key: str) -> bool:
    return key in _INTEGER


def _label(key: str) -> str:
    return key.split(".")[-1].replace("_", " ")


def check_value(key: str, value, label: Optional[str] = None, shown=None) -> Optional[str]:
    """A plain-words problem with a number for this key, or None when it is fine (or the key has no limit).

    Negative CG offsets and angles of attack, and a zero payload, are legitimate and pass."""
    if not _is_num(value):
        return None
    lim = limits(key)
    if not lim:
        return None
    lo, hi, lo_open, integer = lim
    name, t = label or _label(key), (shown if shown is not None else f"{value:g}")
    if lo is not None and (value < lo or (lo_open and value == lo)):
        return (f"{name} must be greater than {lo:g} (you entered {t})." if lo_open else
                f"{name} cannot be below {lo:g} (you entered {t}).")
    if hi is not None and value > hi:
        return f"{name} cannot be above {hi:g} (you entered {t})."
    if integer and value != int(value):
        return f"{name} must be a whole number (you entered {t})."
    return None


def set_status(spec: dict, key: str, status: str) -> None:
    """Set the evidence status of the number at ``key`` in place. One rule for every caller: inside a material the
    status belongs to the whole material; elsewhere a bare number becomes {value, unit, status, source}."""
    parts = key.split(".")
    node = spec
    for p in parts[:-1]:
        node = _step(node, p)
    leaf = _step(node, parts[-1])
    if isinstance(leaf, dict):
        leaf["status"] = status
    elif isinstance(node, dict) and node.get("status") in EVIDENCE and "source" in node:
        node["status"] = status
    else:
        node[int(parts[-1]) if isinstance(node, list) else parts[-1]] = {
            "value": leaf, "unit": "", "status": status, "source": ""}


# ------------------------------------------------------------------------------------------------ editing
def apply_overrides(spec: dict, overrides: dict) -> dict:
    """A deep copy of ``spec`` with ``{dotted.key: value}`` applied (the input is never modified).

    KeyError for a key that does not exist; ValueError when the value has the wrong type (a number is expected
    where the file holds a number). The status of an overridden value is kept."""
    out = copy.deepcopy(spec)
    for key, value in (overrides or {}).items():
        try:
            node, parent = _get(out, key)
        except KeyError:
            raise KeyError(f"unknown key '{key}' (not in drone.json)") from None
        last = key.split(".")[-1]
        target_dict, target_key = parent, last
        if isinstance(node, dict):
            if "value" not in node:
                raise ValueError(f"'{key}' is a section, not a value; set one of the keys inside it")
            target_dict, target_key, node = node, "value", node["value"]
        cur = node
        if cur is None:
            ok = isinstance(value, (int, float, str)) and not isinstance(value, bool) and (
                not isinstance(value, float) or math.isfinite(value))
        elif isinstance(cur, bool):
            ok = isinstance(value, bool)
        elif isinstance(cur, (int, float)):
            ok = _is_num(value)
        elif isinstance(cur, str):
            ok = isinstance(value, str)
        else:
            ok = False
        if not ok:
            want = "a number" if isinstance(cur, (int, float)) and not isinstance(cur, bool) else \
                "text" if isinstance(cur, str) else "a boolean" if isinstance(cur, bool) else "a number or text"
            raise ValueError(f"'{key}' needs {want}, got {value!r}")
        problem = check_value(key, value)
        if problem:
            raise ValueError(problem)
        if isinstance(target_dict, list):
            target_dict[int(target_key)] = value
        else:
            target_dict[target_key] = value
    return out


def input_hash(spec: dict, keys) -> str:
    """Stable hash of the parts of the spec at ``keys`` (a missing key hashes as null)."""
    blob = json.dumps({k: get(spec, k) for k in sorted(set(keys))}, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


# ------------------------------------------------------------------------------------------------ validation
def _walk(node, path, errors):
    if isinstance(node, dict):
        if "value" in node and "status" in node:
            if node["status"] not in EVIDENCE:
                errors.append(f"{path}: status must be one of {', '.join(EVIDENCE)}, got {node['status']!r}")
            v = node["value"]
            if v is not None and not (_is_num(v) or isinstance(v, (str, bool))):
                errors.append(f"{path}.value: must be a number")
        for k, v in node.items():
            if not str(k).startswith("_"):
                _walk(v, f"{path}.{k}" if path else k, errors)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            _walk(v, f"{path}.{i}", errors)
    elif isinstance(node, float) and not math.isfinite(node):
        errors.append(f"{path}: not a finite number")


def validate(spec: dict) -> list[str]:
    """Problems in drone.json as plain sentences (empty list = fine). Missing (null) values are not problems."""
    e: list[str] = []
    if not isinstance(spec, dict):
        return ["drone.json must be a JSON object"]
    if spec.get("format") != FORMAT:
        e.append(f"format must be '{FORMAT}'")
    if not spec.get("name"):
        e.append("name is missing")
    if spec.get("type") not in TYPES:
        e.append(f"type must be one of {', '.join(TYPES)}")
    engine = spec.get("engine", "generic")
    if engine not in ENGINES:
        e.append(f"engine must be one of {', '.join(ENGINES)}")
    if engine == "k09l":
        if not spec.get("study"):
            e.append("study (path to the K09-L study file) is missing")
        return e
    g = spec.get("geometry")
    if not isinstance(g, dict) or not g.get("file"):
        e.append("geometry.file is missing")
    else:
        if g.get("units", "mm") not in UNITS:
            e.append(f"geometry.units must be one of {', '.join(UNITS)}")
        for k, d in (("forward", "+x"), ("up", "+z")):
            if g.get(k, d) not in AXES:
                e.append(f"geometry.{k} must be one of {', '.join(AXES)}")
        if g.get("forward", "+x")[1] == g.get("up", "+z")[1] and g.get("forward", "+x") in AXES:
            e.append("geometry.forward and geometry.up must point along different axes")
    roles = spec.get("roles", {})
    if not isinstance(roles, dict):
        e.append("roles must be an object {role: [patterns]}")
    else:
        for r, pats in roles.items():
            if r not in ROLES:
                e.append(f"roles.{r}: unknown role (use {', '.join(ROLES)})")
            elif not isinstance(pats, list) or not all(isinstance(p, str) for p in pats):
                e.append(f"roles.{r}: must be a list of name patterns")
    mats = spec.get("materials", {}) or {}
    for r, m in (spec.get("role_materials", {}) or {}).items():
        if r not in ROLES:
            e.append(f"role_materials.{r}: unknown role")
        if m is not None and m not in mats:
            e.append(f"role_materials.{r}: material '{m}' is not defined under materials")
    for r in (spec.get("requirements") or []):
        if not isinstance(r, dict) or r.get("op") not in OPS or "." not in str(r.get("metric", "")) \
                or not _is_num(r.get("value")):
            e.append(f"requirement {r.get('id') if isinstance(r, dict) else r!r}: needs id, metric "
                     f"'analysis.metric', op ({' '.join(OPS)}) and a numeric value")
    rot = (spec.get("propulsion") or {}).get("rotors", "auto")
    if rot is not None and rot != "auto" and not isinstance(rot, list):
        e.append("propulsion.rotors must be \"auto\" or a list of rotors")
    an = spec.get("analyses") or {}
    for k in an:
        if k not in ANALYSES:
            e.append(f"analyses.{k}: unknown analysis")
    if (an.get("cfd") or {}).get("preset", "coarse") not in ("coarse", "medium", "fine"):
        e.append("analyses.cfd.preset must be coarse, medium or fine")
    _walk(spec, "", e)
    return e
