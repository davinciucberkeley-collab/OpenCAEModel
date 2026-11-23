"""CAD import for drone projects: STL (ASCII multi-solid or binary), OBJ and STEP -> parts with roles.

Result is an :class:`~opencae.drone.types.Aircraft` in the aircraft frame: x forward, y right, z up, millimetres.
``geometry.units`` (mm | m | in) scales STL/OBJ files; ``geometry.forward`` / ``geometry.up`` name the CAD axes
that point forward and up. STEP files carry their own units and are always read in millimetres.
"""
from __future__ import annotations

import fnmatch
import hashlib
import re
from pathlib import Path
from typing import Optional

import numpy as np

from opencae.drone.types import ROLES, Aircraft, Part

SCALE = {"mm": 1.0, "m": 1000.0, "in": 25.4}
_AXIS = {"+x": (1, 0, 0), "-x": (-1, 0, 0), "+y": (0, 1, 0), "-y": (0, -1, 0), "+z": (0, 0, 1), "-z": (0, 0, -1)}

# role -> words that suggest it; the role order below decides when a name has words of several roles
_WORDS = {
    "control_surface": ("elevon", "aileron", "elevator", "flap"),
    "prop": ("prop", "propeller", "blade", "rotor"),
    "motor": ("motor",),
    "battery": ("batt", "battery", "lipo"),
    "landing": ("leg", "foot", "feet", "skid", "gear"),
    "electronics": ("fc", "esc", "gps", "receiver", "rx", "camera", "stack", "avionics"),
    "payload": ("payload",),
    "wing": ("wing",),
    "fin": ("fin", "tail", "rudder", "stabilizer", "stabiliser"),
    "arm": ("arm", "boom"),
    "frame": ("frame", "body", "plate", "pod", "fuselage", "hub", "chassis"),
}


class GeometryError(ValueError):
    """The CAD file cannot be read."""


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


# ------------------------------------------------------------------------------------------------ roles
def _role_of_name(name: str) -> Optional[str]:
    toks = re.findall(r"[a-z]+", re.sub(r"([a-z])([A-Z])", r"\1 \2", name).lower())
    for role, words in _WORDS.items():
        for w in words:
            for t in toks:
                if t == w or t == w + "s" or (len(w) >= 4 and t.startswith(w)):
                    return role
    return None


def guess_roles(names: list[str]) -> dict[str, list[str]]:
    """Role patterns guessed from part names: ``{role: [glob patterns]}`` that match exactly the guessed parts
    (names that differ only in a number, motor_1..motor_4, share one pattern ``motor_*``)."""
    guess = {n: _role_of_name(n) for n in names}
    out: dict[str, list[str]] = {}
    for n, role in guess.items():
        if role is None:
            continue
        pat = re.sub(r"\d+", "*", n) if re.search(r"\d", n) else n
        if "*" in pat and not all(guess[m] == role for m in names if fnmatch.fnmatchcase(m.lower(), pat.lower())):
            pat = n
        pat = pat if "*" in pat else re.sub(r"([\[\]?])", r"[\1]", pat)
        out.setdefault(role, [])
        if pat not in out[role]:
            out[role].append(pat)
    return out


def assign_roles(names: list[str], patterns: dict) -> dict[str, str]:
    """name -> role by case-insensitive glob patterns; the first role in ROLES order that matches wins."""
    res = {}
    for n in names:
        res[n] = "other"
        for role in ROLES:
            if any(fnmatch.fnmatchcase(n.lower(), str(p).lower()) for p in (patterns or {}).get(role, [])):
                res[n] = role
                break
    return res


# ------------------------------------------------------------------------------------------------ mesh helpers
def weld(tri_xyz: np.ndarray, tol: float) -> tuple[np.ndarray, np.ndarray]:
    """(m,3,3) triangle corners -> (points, triangles) with duplicate vertices merged; degenerate triangles dropped."""
    flat = tri_xyz.reshape(-1, 3)
    key = np.round(flat / tol).astype(np.int64)
    _, first, inv = np.unique(key, axis=0, return_index=True, return_inverse=True)
    pts = flat[first]
    tris = inv.reshape(-1, 3)
    ok = (tris[:, 0] != tris[:, 1]) & (tris[:, 1] != tris[:, 2]) & (tris[:, 0] != tris[:, 2])
    return pts, tris[ok]


def _compact(points: np.ndarray, tris: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    used, inv = np.unique(tris, return_inverse=True)
    return points[used], inv.reshape(-1, 3)


def components(points: np.ndarray, tris: np.ndarray) -> list[tuple[np.ndarray, np.ndarray]]:
    """Split into connected pieces (triangles sharing a vertex), largest first. Pieces are only separated."""
    if len(tris) == 0:
        return []
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components
    n = len(points)
    g = coo_matrix((np.ones(len(tris) * 2), (np.r_[tris[:, 0], tris[:, 1]], np.r_[tris[:, 1], tris[:, 2]])),
                   shape=(n, n))
    nc, lab = connected_components(g, directed=False)
    tl = lab[tris[:, 0]]
    out = []
    for c in range(nc):
        t = tris[tl == c]
        if len(t):
            out.append(_compact(points, t))
    out.sort(key=lambda pt: -len(pt[1]))
    return out


def _tol(tri_xyz: np.ndarray) -> float:
    d = float(np.linalg.norm(tri_xyz.reshape(-1, 3).max(0) - tri_xyz.reshape(-1, 3).min(0)))
    return max(d * 1e-7, 1e-9)


def _unique_name(name: str, used: set) -> str:
    base, i = name, 2
    while name in used:
        name, i = f"{base}_{i}", i + 1
    used.add(name)
    return name


# ------------------------------------------------------------------------------------------------ readers
RawParts = list  # [(name, points (n,3) float64, triangles (m,3) int64)] in the file's own units


def _read_stl(path: Path) -> RawParts:
    data = path.read_bytes()
    n_bin = int.from_bytes(data[80:84], "little") if len(data) >= 84 else -1
    is_binary = len(data) == 84 + 50 * n_bin
    if not is_binary and not (data[:5].lower() == b"solid" and b"facet" in data[:4000]) and len(data) >= 84:
        is_binary, n_bin = True, min(n_bin, (len(data) - 84) // 50)  # truncated or padded binary file
    if is_binary:
        dt = np.dtype([("n", "<f4", 3), ("v", "<f4", (3, 3)), ("a", "<u2")])
        tri = np.frombuffer(data, dtype=dt, count=n_bin, offset=84)["v"].astype(np.float64)
        if len(tri) == 0:
            raise GeometryError(f"{path.name}: the STL file has no triangles")
        pts, t = weld(tri, _tol(tri))
        return [(f"part_{i + 1}", p, tt) for i, (p, tt) in enumerate(components(pts, t))]
    solids: list[tuple[str, list]] = []
    cur: Optional[list] = None
    for line in data.decode("utf-8", "replace").splitlines():
        s = line.strip()
        low = s[:8].lower()
        if low.startswith("solid"):
            cur = []
            solids.append((s[5:].strip(), cur))
        elif low.startswith("vertex"):
            if cur is None:
                cur = []
                solids.append(("", cur))
            cur.append([float(x) for x in s.split()[1:4]])
    solids = [(n, v) for n, v in solids if len(v) >= 3]
    if not solids:
        raise GeometryError(f"{path.name}: no triangles found in the ASCII STL file")
    if len(solids) == 1:  # one body: split it into connected pieces
        tri = np.array(solids[0][1], dtype=np.float64).reshape(-1, 3, 3)
        pts, t = weld(tri, _tol(tri))
        comps = components(pts, t)
        if len(comps) == 1 and solids[0][0]:
            return [(solids[0][0], comps[0][0], comps[0][1])]
        return [(f"part_{i + 1}", p, tt) for i, (p, tt) in enumerate(comps)]
    allv = np.array([x for _, v in solids for x in v], dtype=np.float64)
    tol = _tol(allv.reshape(-1, 3, 3)) if len(allv) % 3 == 0 else 1e-6
    out, used = [], set()
    for i, (n, v) in enumerate(solids):
        tri = np.array(v, dtype=np.float64).reshape(-1, 3, 3)
        pts, t = weld(tri, tol)
        out.append((_unique_name(n or f"solid_{i + 1}", used), pts, t))
    return out


def _read_obj(path: Path) -> RawParts:
    verts: list = []
    groups: dict[str, list] = {}
    cur = "part_1"
    for line in path.read_text(errors="replace").splitlines():
        s = line.split()
        if not s:
            continue
        if s[0] == "v" and len(s) >= 4:
            verts.append([float(x) for x in s[1:4]])
        elif s[0] in ("o", "g"):
            cur = " ".join(s[1:]) or cur
        elif s[0] == "f" and len(s) >= 4:
            idx = []
            for tok in s[1:]:
                k = int(tok.split("/")[0])
                idx.append(k - 1 if k > 0 else len(verts) + k)
            for j in range(1, len(idx) - 1):
                groups.setdefault(cur, []).append((idx[0], idx[j], idx[j + 1]))
    if not groups:
        raise GeometryError(f"{path.name}: no faces found in the OBJ file")
    V = np.array(verts, dtype=np.float64)
    named = any(n != "part_1" for n in groups)
    out, used = [], set()
    if named:
        for n, f in groups.items():
            p, t = _compact(V, np.array(f, dtype=np.int64))
            out.append((_unique_name(n, used), p, t))
        return out
    f = np.array(groups["part_1"], dtype=np.int64)
    p, t = weld(V[f], _tol(V[f]))
    return [(f"part_{i + 1}", pp, tt) for i, (pp, tt) in enumerate(components(p, t))]


def _read_step(path: Path) -> RawParts:
    from opencae.geometry import occ_kernel as K
    shape = K.read_step(path)
    solids = K.solids_of(shape)
    out: RawParts = []
    if solids:
        t0 = K.tessellate(shape, 1.0)
        diag = float(np.linalg.norm(t0.points.max(0) - t0.points.min(0))) if len(t0.points) else 100.0
        hint = max(diag / 20.0, 1e-3)
        for i, s in enumerate(solids):
            t = K.tessellate(s, hint)
            if len(t.triangles):
                p, tt = weld(t.points[t.triangles], max(diag * 1e-7, 1e-9))
                out.append((f"solid_{i + 1}", p, tt))
    else:
        t = K.tessellate(shape, 1.0)
        p, tt = weld(t.points[t.triangles], 1e-6)
        out = [(f"part_{i + 1}", pp, q) for i, (pp, q) in enumerate(components(p, tt))]
    if not out:
        raise GeometryError(f"{path.name}: the STEP file has no solids or surfaces I can read")
    return out


def read_raw(path: Path) -> RawParts:
    """Parts as stored in the file (own units and axes), no roles."""
    ext = path.suffix.lower()
    if ext == ".stl":
        return _read_stl(path)
    if ext == ".obj":
        return _read_obj(path)
    if ext in (".step", ".stp"):
        return _read_step(path)
    raise GeometryError(f"{path.name}: unsupported CAD format '{ext}' (use .stl, .obj, .step or .stp)")


def _raw_cached(project_dir: Path, path: Path) -> RawParts:
    """read_raw with a cache in results/geometry_cache.npz keyed by the CAD file hash."""
    h = file_hash(path)
    cache = project_dir / "results" / "geometry_cache.npz"
    try:
        if cache.exists():
            with np.load(cache, allow_pickle=False) as z:
                if str(z["cad_hash"]) == h:
                    return [(str(n), z[f"p{i}"].astype(np.float64), z[f"t{i}"].astype(np.int64))
                            for i, n in enumerate(z["names"])]
    except Exception:
        pass
    raw = read_raw(path)
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        arrs = {"cad_hash": np.array(h), "names": np.array([n for n, _, _ in raw])}
        for i, (_, p, t) in enumerate(raw):
            arrs[f"p{i}"], arrs[f"t{i}"] = p, t.astype(np.int32)
        np.savez_compressed(cache, **arrs)
    except OSError:
        pass
    return raw


def frame_matrix(forward: str, up: str) -> np.ndarray:
    """Rows are the aircraft x (forward), y (right = up x forward) and z (up) axes in CAD coordinates."""
    f, u = np.array(_AXIS[forward], float), np.array(_AXIS[up], float)
    if abs(float(f @ u)) > 0.5:
        raise GeometryError("geometry.forward and geometry.up must point along different axes")
    return np.array([f, np.cross(u, f), u])


def load(project_dir, spec: dict) -> Aircraft:
    """Import the CAD file named in ``spec["geometry"]`` and assign roles from ``spec["roles"]``
    (patterns guessed from the part names when drone.json has no ``roles`` section)."""
    project_dir = Path(project_dir)
    g = spec.get("geometry") or {}
    if not g.get("file"):
        raise GeometryError("geometry.file is missing in drone.json")
    path = project_dir / g["file"]
    if not path.exists():
        raise GeometryError(f"CAD file not found: {path}")
    units = g.get("units", "mm")
    if units not in SCALE:
        raise GeometryError(f"geometry.units must be mm, m or in (got {units!r})")
    raw = _raw_cached(project_dir, path)
    scale = 1.0 if path.suffix.lower() in (".step", ".stp") else SCALE[units]
    R = frame_matrix(g.get("forward", "+x"), g.get("up", "+z"))
    names = [n for n, _, _ in raw]
    patterns = spec["roles"] if "roles" in spec else guess_roles(names)
    role = assign_roles(names, patterns)
    mats = spec.get("role_materials") or {}
    parts = [Part(n, role[n], (p * scale) @ R.T, t, mats.get(role[n])) for n, p, t in raw]
    return Aircraft(spec.get("name", path.stem), parts, str(path))


def save_npz(aircraft: Aircraft, path: Path) -> int:
    """geometry.npz for the dashboard: names, roles, p{i} float32 (n,3) mm, t{i} int32 (m,3)."""
    arrs = {"names": np.array([p.name for p in aircraft.parts]), "roles": np.array([p.role for p in aircraft.parts])}
    for i, p in enumerate(aircraft.parts):
        arrs[f"p{i}"] = p.points.astype(np.float32)
        arrs[f"t{i}"] = p.triangles.astype(np.int32)
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(path, **arrs)
    return path.stat().st_size
