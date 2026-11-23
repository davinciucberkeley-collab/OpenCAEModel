"""Writes airframe.stl: a 5-inch X-frame quadcopter as an ASCII multi-solid STL (one closed solid per part).

Axes: x forward, y left, z up (right-handed), millimetres; 250 mm motor-to-motor diagonal. Run: python make_geometry.py
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np

DIAGONAL = 250.0
ARM_T, ARM_W = 4.0, 12.0
Z_BOT, Z_ARM, Z_TOP = 0.0, 2.0, 6.0  # bottom plate 0..2, arms 2..6, top plate 6..8


def _orient(verts: np.ndarray, tris: list) -> list:
    """Make every triangle's normal point away from the (convex) solid's centre."""
    c = verts.mean(0)
    out = []
    for a, b, d in tris:
        n = np.cross(verts[b] - verts[a], verts[d] - verts[a])
        out.append((a, b, d) if n @ (verts[a] - c) > 0 else (a, d, b))
    return out


def box(center, size, angle_deg=0.0):
    cx, cy, cz = center
    sx, sy, sz = size
    v = np.array([[x * sx / 2, y * sy / 2, z * sz / 2] for x in (-1, 1) for y in (-1, 1) for z in (-1, 1)])
    a = math.radians(angle_deg)
    R = np.array([[math.cos(a), -math.sin(a), 0], [math.sin(a), math.cos(a), 0], [0, 0, 1]])
    v = v @ R.T + np.array([cx, cy, cz])
    idx = lambda x, y, z: (x * 4 + y * 2 + z)
    quads = [(idx(0, 0, 0), idx(0, 1, 0), idx(0, 1, 1), idx(0, 0, 1)), (idx(1, 0, 0), idx(1, 1, 0), idx(1, 1, 1), idx(1, 0, 1)),
             (idx(0, 0, 0), idx(1, 0, 0), idx(1, 0, 1), idx(0, 0, 1)), (idx(0, 1, 0), idx(1, 1, 0), idx(1, 1, 1), idx(0, 1, 1)),
             (idx(0, 0, 0), idx(1, 0, 0), idx(1, 1, 0), idx(0, 1, 0)), (idx(0, 0, 1), idx(1, 0, 1), idx(1, 1, 1), idx(0, 1, 1))]
    tris = [t for q in quads for t in ((q[0], q[1], q[2]), (q[0], q[2], q[3]))]
    return v, _orient(v, tris)


def cylinder(center, radius, height, n=32):
    cx, cy, cz = center
    ang = np.linspace(0, 2 * math.pi, n, endpoint=False)
    ring = np.c_[cx + radius * np.cos(ang), cy + radius * np.sin(ang)]
    v = np.vstack([np.c_[ring, np.full(n, cz - height / 2)], np.c_[ring, np.full(n, cz + height / 2)],
                   [[cx, cy, cz - height / 2]], [[cx, cy, cz + height / 2]]])
    bot, top = 2 * n, 2 * n + 1
    tris = []
    for i in range(n):
        j = (i + 1) % n
        tris += [(i, j, n + j), (i, n + j, n + i), (bot, j, i), (top, n + i, n + j)]
    return v, _orient(v, tris)


def solids() -> list:
    r = DIAGONAL / 2
    k = r / math.sqrt(2)
    out = [("frame_plate_bottom", box((0, 0, Z_BOT + 1), (100, 40, 2))),
           ("frame_plate_top", box((0, 0, Z_TOP + 1), (100, 40, 2)))]
    corners = {"fl": (k, k), "fr": (k, -k), "rl": (-k, k), "rr": (-k, -k)}  # y is LEFT
    for nm, (x, y) in corners.items():
        # arms start 10 mm from the centre (under the plates) so the four arms are separate bodies
        c = (10 + r) / 2 / r
        out.append((f"arm_{nm}", box((x * c, y * c, Z_ARM + ARM_T / 2), (r - 10, ARM_W, ARM_T),
                                     math.degrees(math.atan2(y, x)))))
    for i, (x, y) in enumerate(corners.values(), 1):
        out.append((f"motor_{i}", cylinder((x, y, Z_ARM + ARM_T + 9), 14, 18)))
    for i, (x, y) in enumerate(corners.values(), 1):
        out.append((f"prop_{i}", cylinder((x, y, Z_ARM + ARM_T + 18 + 2), 63.5, 2, 48)))
    out.append(("battery", box((-20, 0, Z_TOP + 2 + 15), (75, 35, 30))))
    out.append(("fc_stack", box((38, 0, Z_TOP + 2 + 10), (36, 36, 20))))
    return out


def write(path: Path) -> None:
    lines = []
    for name, (v, tris) in solids():
        lines.append(f"solid {name}")
        for a, b, c in tris:
            n = np.cross(v[b] - v[a], v[c] - v[a])
            n = n / (np.linalg.norm(n) or 1.0)
            lines.append(f"  facet normal {n[0]:.6f} {n[1]:.6f} {n[2]:.6f}")
            lines.append("    outer loop")
            for i in (a, b, c):
                lines.append(f"      vertex {v[i][0]:.4f} {v[i][1]:.4f} {v[i][2]:.4f}")
            lines.append("    endloop")
            lines.append("  endfacet")
        lines.append(f"endsolid {name}")
    path.write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    out = Path(__file__).with_name("airframe.stl")
    write(out)
    print(f"wrote {out} ({out.stat().st_size / 1e3:.0f} kB)")
