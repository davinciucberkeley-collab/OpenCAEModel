"""Multibody results: storage (``mbd.npz`` + ``mbd.json``), lookup at a time, CSV export, animation poses.

Sign conventions (also written into every export):

* joint coordinate q: revolute = right-hand rotation about the joint axis (rad), prismatic = translation along
  the axis (project length unit); the joint axis is the stored axis, reversed if ``reversed``;
* ``reaction_*``: force / moment exerted BY the PARENT body ON the CHILD body at the joint origin (the point of
  the child body that coincides with the joint origin in the reference configuration), expressed in the joint
  frame (``*_joint``: z = joint axis, x = stored x axis, carried by the parent) and in the assembly frame
  (``*_assembly``). The component of the wrench along the joint axis that is delivered by the actuator is
  reported separately as ``joint_effort`` and is NOT part of ``reaction_*``; ``reaction_*_total_*`` is the full
  transmitted wrench (reaction + actuator effort along the axis), i.e. what a structural support at the joint
  would carry;
* ``joint_effort``: actuator torque (N m) / force (N) along +q: the commanded effort for effort drivers, the
  inverse-dynamics effort that realises the motion for prescribed (position / velocity) joints, 0 otherwise;
* body position = centre of mass in the assembly frame (project length unit), orientation = unit quaternion
  (w, x, y, z) of the body frame relative to the assembly frame (reference configuration = identity).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np

NPZ_NAME = "mbd.npz"
JSON_NAME = "mbd.json"
FORMAT = "opencae-mbd-result"

# per-id array groups: name -> (unit key, frame text, sign text)
JOINT_FIELDS = ("joint_q", "joint_qd", "joint_qdd", "joint_effort", "reaction_force_joint", "reaction_moment_joint",
                "reaction_force_assembly", "reaction_moment_assembly", "reaction_force_total_joint",
                "reaction_moment_total_joint", "reaction_force_total_assembly", "reaction_moment_total_assembly")
BODY_FIELDS = ("body_pos", "body_quat")
DRIVER_FIELDS = ("driver_command",)


def _slug(text: str) -> str:
    out = "".join(c if c.isalnum() or c in "-_" else "_" for c in text.strip())
    return out or "x"


def quat_to_matrix(q) -> np.ndarray:
    w, x, y, z = np.asarray(q, float) / np.linalg.norm(q)
    return np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                     [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                     [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])


@dataclass
class MBDResult:
    t: np.ndarray
    body_pos: dict = field(default_factory=dict)
    body_quat: dict = field(default_factory=dict)
    joint_q: dict = field(default_factory=dict)
    joint_qd: dict = field(default_factory=dict)
    joint_qdd: dict = field(default_factory=dict)
    joint_effort: dict = field(default_factory=dict)
    reaction_force_joint: dict = field(default_factory=dict)
    reaction_moment_joint: dict = field(default_factory=dict)
    reaction_force_assembly: dict = field(default_factory=dict)
    reaction_moment_assembly: dict = field(default_factory=dict)
    reaction_force_total_joint: dict = field(default_factory=dict)
    reaction_moment_total_joint: dict = field(default_factory=dict)
    reaction_force_total_assembly: dict = field(default_factory=dict)
    reaction_moment_total_assembly: dict = field(default_factory=dict)
    driver_command: dict = field(default_factory=dict)
    constraint_error: np.ndarray = field(default_factory=lambda: np.zeros(0))
    meta: dict = field(default_factory=dict)
    path: Optional[Path] = None

    # ---- storage ---------------------------------------------------------
    def save(self, directory: Path) -> Path:
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        arrays = {"t": self.t, "constraint_error": self.constraint_error}
        for grp in JOINT_FIELDS + BODY_FIELDS + DRIVER_FIELDS:
            for key, arr in getattr(self, grp).items():
                arrays[f"{grp}__{key}"] = np.asarray(arr)
        np.savez_compressed(directory / NPZ_NAME, **arrays)
        (directory / JSON_NAME).write_text(json.dumps({"format": FORMAT, "version": 1, **self.meta}, indent=2),
                                           encoding="utf-8")
        self.path = directory
        return directory

    @classmethod
    def load(cls, path: Path) -> "MBDResult":
        """Load from a result directory (or from its ``mbd.npz`` / ``mbd.json``)."""
        path = Path(path)
        directory = path.parent if path.is_file() else path
        meta = json.loads((directory / JSON_NAME).read_text(encoding="utf-8"))
        if meta.get("format") != FORMAT:
            raise ValueError(f"{directory / JSON_NAME} is not a multibody result")
        meta.pop("format", None)
        meta.pop("version", None)
        z = np.load(directory / NPZ_NAME, allow_pickle=False)
        res = cls(t=z["t"], constraint_error=z["constraint_error"], meta=meta, path=directory)
        for name in z.files:
            if "__" in name:
                grp, key = name.split("__", 1)
                getattr(res, grp)[key] = z[name]
        return res

    # ---- lookup ----------------------------------------------------------
    @property
    def joint_ids(self) -> list[str]:
        return [j["id"] for j in self.meta.get("joints", [])]

    @property
    def body_ids(self) -> list[str]:
        return [b["id"] for b in self.meta.get("bodies", [])]

    def index_at(self, time: float) -> int:
        """Index of the last sample at or before ``time``."""
        if time < self.t[0] - 1e-12 or time > self.t[-1] + 1e-12:
            raise ValueError(f"time {time:g} s is outside the simulated range [{self.t[0]:g}, {self.t[-1]:g}] s")
        return int(min(max(np.searchsorted(self.t, time + 1e-12, side="right") - 1, 0), len(self.t) - 1))

    def at(self, time: float) -> dict:
        """Snapshot at ``time``: linear interpolation between samples (quaternions: normalised lerp)."""
        i = self.index_at(time)
        j = min(i + 1, len(self.t) - 1)
        w = 0.0 if j == i else float((time - self.t[i]) / (self.t[j] - self.t[i]))
        w = min(max(w, 0.0), 1.0)
        snap: dict = {"t": float(time), "index": i}
        for grp in JOINT_FIELDS + BODY_FIELDS + DRIVER_FIELDS:
            snap[grp] = {}
            for key, arr in getattr(self, grp).items():
                v = (1 - w) * arr[i] + w * arr[j]
                if grp == "body_quat":
                    v = v / np.linalg.norm(v)
                snap[grp][key] = v
        snap["constraint_error"] = float(max(self.constraint_error[i], self.constraint_error[j]) if w > 0
                                         else self.constraint_error[i])
        return snap

    # ---- CSV ---------------------------------------------------------------
    def _joint_info(self, jid: str) -> dict:
        return next(j for j in self.meta["joints"] if j["id"] == jid)

    def export_csv(self, directory: Path, joints: Optional[list] = None, bodies: Optional[list] = None) -> list[Path]:
        """Write one CSV per joint and per body (all by default; ids). Every file starts with ``#`` header
        lines giving, for each column, the quantity, unit, frame and sign convention."""
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        u = self.meta["units"]
        written: list[Path] = []
        for jid in (self.joint_ids if joints is None else joints):
            info = self._joint_info(jid)
            prism = info["kind"] == "prismatic"
            qu, qdu, qddu = (u["length"], f"{u['length']}/s", f"{u['length']}/s^2") if prism else \
                ("rad", "rad/s", "rad/s^2")
            eu = "N" if prism else "N m"
            cols = [("t", "s", "time", "-", None)]
            q_sign = ("positive = translation along the joint axis" if prism
                      else "positive = right-hand rotation about the joint axis")
            cols += [("q", qu, "joint coordinate", q_sign, self.joint_q[jid]),
                     ("qd", qdu, "joint velocity", "derivative of q", self.joint_qd[jid]),
                     ("qdd", qddu, "joint acceleration", "second derivative of q", self.joint_qdd[jid]),
                     ("effort", eu, "actuator " + ("force" if prism else "torque") + " along the joint axis",
                      "positive along +q; " + info["driver_kind"], self.joint_effort[jid])]
            sign = ("exerted BY the parent body ON the child body at the joint origin, actuator effort excluded "
                    "(joint damping and the coupling torque along the axis remain)")
            tsign = "exerted BY the parent body ON the child body at the joint origin, actuator effort included"
            for name, grp, unit, frame, sg in (
                    ("reaction_force", "reaction_force_joint", "N", "joint frame", sign),
                    ("reaction_moment", "reaction_moment_joint", "N m", "joint frame", sign),
                    ("reaction_force_assembly", "reaction_force_assembly", "N", "assembly frame", sign),
                    ("reaction_moment_assembly", "reaction_moment_assembly", "N m", "assembly frame", sign),
                    ("transmitted_force", "reaction_force_total_joint", "N", "joint frame", tsign),
                    ("transmitted_moment", "reaction_moment_total_joint", "N m", "joint frame", tsign)):
                for k, ax in enumerate("xyz"):
                    cols.append((f"{name}_{ax}", unit, frame, sg, getattr(self, grp)[jid][:, k]))
            for d in self.meta.get("drivers", []):
                if d["joint"] == jid:
                    cols.append((f"command_{_slug(d['name'])}", d["unit"], f"driver mode: {d['mode']}",
                                 d["kind"], self.driver_command[d["id"]]))
            cols.append(("constraint_error", "rad (m for prismatic)", "coupling constraints",
                         "max |q_follower - ratio q_leader| since the previous sample", self.constraint_error))
            written.append(self._write_csv(directory / f"joint_{_slug(info['name'])}.csv",
                                           f"joint '{info['name']}' ({info['kind']}), parent "
                                           f"'{info['parent_name']}', child '{info['child_name']}'", cols))
        for bid in (self.body_ids if bodies is None else bodies):
            info = next(b for b in self.meta["bodies"] if b["id"] == bid)
            cols = [("t", "s", "time", "-", None)]
            for k, ax in enumerate("xyz"):
                cols.append((f"com_{ax}", u["length"], "assembly frame", "centre-of-mass position",
                             self.body_pos[bid][:, k]))
            for k, ax in enumerate("wxyz"):
                cols.append((f"quat_{ax}", "-", "assembly frame",
                             "unit quaternion of the body frame (reference configuration = identity)",
                             self.body_quat[bid][:, k]))
            written.append(self._write_csv(directory / f"body_{_slug(info['name'])}.csv",
                                           f"body '{info['name']}'" + (" (grounded)" if info["grounded"] else ""),
                                           cols))
        return written

    def _write_csv(self, path: Path, title: str, cols: list) -> Path:
        lines = [f"# OpenCAE Studio multibody result: {title}",
                 f"# study: {self.meta.get('study', '')}; backend: {self.meta.get('backend', {}).get('name', '')} "
                 f"{self.meta.get('backend', {}).get('version', '')}",
                 f"# units: length {self.meta['units']['length']}, mass kg, force N, moment N m, angle rad, time s"]
        for name, unit, frame, sign, _ in cols:
            lines.append(f"# quantity: {name}; unit: {unit}; frame: {frame}; sign: {sign}")
        lines.append(",".join(f"{c[0]}[{c[1]}]" for c in cols))
        data = [self.t if c[4] is None else np.asarray(c[4]) for c in cols]
        body = [",".join(f"{float(col[i]):.12g}" for col in data) for i in range(len(self.t))]
        path.write_text("\n".join(lines + body) + "\n", encoding="utf-8")
        return path


def pose_transforms(result: MBDResult, i: int) -> dict:
    """``{component id: 4x4}`` at sample ``i``: body motion x component placement (project length unit).
    The source CAD and its placement are never modified; apply these to the viewport actors only."""
    comps = result.meta["components"]
    com0 = {b["id"]: np.asarray(b["com0"], float) for b in result.meta["bodies"]}
    out = {}
    for cid, c in comps.items():
        bid = c["body"]
        R = quat_to_matrix(result.body_quat[bid][i])
        T = np.eye(4)
        T[:3, :3] = R
        T[:3, 3] = result.body_pos[bid][i] - R @ com0[bid]
        out[cid] = T @ np.asarray(c["placement"], float).reshape(4, 4)
    return out
