"""VTK scene helpers for the K09-L platform: the aircraft as one picking-friendly surface, the
virtual flight-test field, and coordinate-frame glyphs. Geometry is never modified here: exploded
or flown positions are new point arrays / actor matrices."""
from __future__ import annotations

import math

import numpy as np
import pyvista as pv

from opencae.engineering.model import GROUP_COLOURS, EngineeringModel

MM = 1e-3


def _rgb(h: str) -> np.ndarray:
    h = h.lstrip("#")
    return np.array([int(h[i:i + 2], 16) for i in (0, 2, 4)], dtype=np.uint8)


def aircraft_polydata(m: EngineeringModel, points: np.ndarray | None = None, bodies=None,
                      colour_by: str = "body") -> pv.PolyData:
    """All (active) bodies in one PolyData (mm, aircraft frame) with cell arrays 'body' (index) and 'rgb'.
    ``points`` replaces the model points (e.g. exploded copy); ``colour_by`` = body | group."""
    m.modified_bodies()  # apply the configuration's parametric geometry changes before reading points
    P = m.points if points is None else points
    bodies = m.active_bodies() if bodies is None else bodies
    tris, bid, cols = [], [], []
    for b in bodies:
        T = m.indices[b.io:b.io + b.ic].reshape(-1, 3) + b.vo
        tris.append(T)
        bid.append(np.full(len(T), b.index))
        c = np.array(b.colour, dtype=np.uint8) if colour_by == "body" else _rgb(GROUP_COLOURS.get(b.group, "#999999"))
        cols.append(np.repeat(c[None, :], len(T), axis=0))
    T = np.vstack(tris)
    used, inv = np.unique(T, return_inverse=True)
    faces = np.hstack([np.full((len(T), 1), 3), inv.reshape(-1, 3)]).ravel()
    pd = pv.PolyData(np.asarray(P[used], float), faces)
    pd.cell_data["body"] = np.concatenate(bid)
    pd.cell_data["rgb"] = np.vstack(cols)
    return pd


def highlight_colours(pd: pv.PolyData, base: np.ndarray, selected: set, colour="#FF8A00",
                      dim: set | None = None) -> np.ndarray:
    rgb = base.copy()
    body = np.asarray(pd.cell_data["body"])
    if selected:
        rgb[np.isin(body, list(selected))] = _rgb(colour)
    if dim:
        mask = np.isin(body, list(dim))
        rgb[mask] = (rgb[mask] * 0.35 + 160).clip(0, 255).astype(np.uint8)
    return rgb


def frame_glyph(origin, length: float, labels=("x", "y", "z")) -> list:
    """Three arrows (x red, y green, z blue) for a coordinate frame."""
    out = []
    for i, c in enumerate(("#d43c3c", "#3ca04a", "#3c6fd4")):
        d = np.zeros(3)
        d[i] = 1
        out.append((pv.Arrow(start=origin, direction=d, scale=length, tip_length=0.18, shaft_radius=0.02,
                             tip_radius=0.06), c))
    return out


def garden(size: float = 60.0, seed: int = 4) -> list[tuple]:
    """A simple outdoor test field (world frame, m, z up): lawn, mown strip, landing pad, trees, a shed,
    a fence line and range markers. Returns (mesh, colour, kwargs) tuples."""
    rng = np.random.default_rng(seed)
    out = [(pv.Plane(center=(0, 0, 0), direction=(0, 0, 1), i_size=size * 2, j_size=size * 2, i_resolution=40,
                     j_resolution=40), "#6f9f4a", {}),
           (pv.Plane(center=(0, 0, 0.005), direction=(0, 0, 1), i_size=6, j_size=size * 1.6), "#88b45c", {}),
           (pv.Disc(center=(0, 0, 0.01), inner=0.0, outer=1.5, normal=(0, 0, 1), c_res=48), "#c9c4b6", {}),
           (pv.Disc(center=(0, 0, 0.012), inner=1.25, outer=1.4, normal=(0, 0, 1), c_res=48), "#e8b23a", {})]
    for _ in range(26):  # trees around the field
        r = rng.uniform(18, size * 0.9)
        a = rng.uniform(0, 2 * np.pi)
        x, y = r * np.cos(a), r * np.sin(a)
        h = rng.uniform(4, 9)
        out.append((pv.Cylinder(center=(x, y, h * 0.2), direction=(0, 0, 1), radius=0.18, height=h * 0.4),
                    "#6b4a2b", {}))
        out.append((pv.Cone(center=(x, y, h * 0.62), direction=(0, 0, 1), height=h * 0.75,
                            radius=rng.uniform(1.4, 2.4), resolution=14), rng.choice(["#2f6b34", "#3b7a3b",
                                                                                       "#2a5e2f"]), {}))
    out.append((pv.Box(bounds=(-14, -9, 12, 16, 0, 2.6)), "#9b6b4b", {}))  # shed
    out.append((pv.Cone(center=(-11.5, 14, 3.3), direction=(0, 0, 1), height=1.4, radius=3.6, resolution=4),
                "#5a3e2b", {}))
    for i in range(-10, 11):  # fence along the north edge
        out.append((pv.Box(bounds=(i * 3 - 0.05, i * 3 + 0.05, 30, 30.1, 0, 1.1)), "#d9cbb2", {}))
    out.append((pv.Box(bounds=(-30, 30, 30.0, 30.05, 0.85, 0.95)), "#d9cbb2", {}))
    for d in (5, 10, 20):  # range rings
        out.append((pv.Disc(center=(0, 0, 0.008), inner=d - 0.04, outer=d + 0.04, normal=(0, 0, 1), c_res=96),
                    "#f2f2f2", {"opacity": 0.6}))
    return out


SPIN_DISPLAY = 1.0 / 40.0  # props are drawn at 1/40 of their real speed (real speed aliases at screen frame rates)


def rotor_groups(m: EngineeringModel) -> list[dict]:
    """The rotating parts of each motor (prop, adapter, nut, washer: the bodies on a 'Prop M..' joint), in the
    order the flight simulation numbers the motors. Each: name, origin (mm), unit axis, sense (+1 CCW seen
    from the nose: rotation +angle about the joint axis), body list."""
    act = m.active_bodies()
    out = []
    for name in sorted(n for n in m.joints if n.startswith("Prop M")):
        j = m.joints[name]
        ax = np.asarray(j["axis"], float)
        out.append({"name": name, "origin": np.asarray(j["origin_mm"], float), "axis": ax / np.linalg.norm(ax),
                    "sense": 1.0 if "CCW" in name else -1.0, "bodies": [b for b in act if b.joint == name]})
    return out


def spin_matrix(origin, axis, angle) -> np.ndarray:
    """4x4 rotation by ``angle`` (rad) about the line through ``origin`` (mm) along ``axis``."""
    a = np.asarray(axis, float)
    K = np.array([[0, -a[2], a[1]], [a[2], 0, -a[0]], [-a[1], a[0], 0]])
    R = np.eye(3) + math.sin(angle) * K + (1 - math.cos(angle)) * K @ K
    M = np.eye(4)
    M[:3, :3] = R
    M[:3, 3] = np.asarray(origin) - R @ np.asarray(origin)
    return M


def rotor_angles(t: np.ndarray, rpm: np.ndarray, display: float = SPIN_DISPLAY) -> np.ndarray:
    """Displayed rotor angle (rad) per sample and motor: the integral of the simulated rpm, scaled."""
    w = np.asarray(rpm, float) * 2 * math.pi / 60.0 * display
    dt = np.diff(np.asarray(t, float), prepend=float(t[0]))
    return np.cumsum(w * dt[:, None], axis=0)


def pose_matrix(pos, quat, cg) -> np.ndarray:
    """4x4 transform of the aircraft mesh (mm, aircraft frame) into the world (m): v_W = pos + R (v_B - cg)."""
    w, x, y, z = quat
    R = np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)],
                  [2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)],
                  [2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)]])
    M = np.eye(4)
    M[:3, :3] = R * MM
    M[:3, 3] = np.asarray(pos) - (R * MM) @ np.asarray(cg)
    return M
