"""Assembly data model: components, rigid bodies, joints, couplings, drivers, applied loads.

Pure data (no Qt, no OpenCASCADE, no MuJoCo). Everything here serialises to plain JSON with
``to_dict`` / ``from_dict`` (``to_dict`` equals :func:`dataclasses.asdict`, so the generic project
codec and these helpers agree).

Conventions (see ``plans/2026-11-25-assembly-mbd/contract.md``):

* geometry, placements and joint origins are in the *project length unit*; angles in radians;
  mass in kg and inertia in kg m^2 (always SI, labelled);
* the assembly frame is the project frame; the *reference configuration* is the one in which all
  joint coordinates are zero (components are placed in it);
* joint frame: origin ``origin``, z = ``axis`` (reversed if ``reversed``), x = ``x_axis``;
  revolute q > 0 is a right-hand rotation about z, prismatic q > 0 a translation along z;
* a joint reaction is the force / moment exerted BY the parent body ON the child body at the joint
  origin (documented in :mod:`opencae.mbd.results`).
"""
from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Optional

import numpy as np


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


GROUND = "ground"
JOINT_KINDS = ("fixed", "revolute", "prismatic")
DRIVER_MODES = ("position", "velocity", "effort")
PRESCRIBED_MODES = ("position", "velocity")
IDENTITY4 = [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0], [0.0, 0.0, 1.0, 0.0], [0.0, 0.0, 0.0, 1.0]]


def _pick(cls, data: dict) -> dict:
    """Keep only the constructor fields of ``cls`` that are present in ``data``."""
    names = {f.name for f in fields(cls)}
    return {k: v for k, v in data.items() if k in names}


# --------------------------------------------------------------------------- components, bodies
@dataclass
class Component:
    """A placed instance of a geometry body. ``placement`` is a 4x4 transform in project length units."""

    name: str = ""
    body_id: str = ""  # GeometryModel body id
    placement: list = field(default_factory=lambda: [list(r) for r in IDENTITY4])
    source: str = ""
    id: str = field(default_factory=lambda: _new_id("comp"))

    def placement_matrix(self) -> np.ndarray:
        return np.asarray(self.placement, float).reshape(4, 4)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Component":
        return cls(**_pick(cls, d))


@dataclass
class MassProps:
    """Mass, centre of mass and inertia tensor about the COM, in assembly axes."""

    mass_kg: float = 0.0
    com: list = field(default_factory=lambda: [0.0, 0.0, 0.0])  # project length unit, assembly frame
    inertia_kgm2: list = field(default_factory=lambda: [[0.0] * 3 for _ in range(3)])
    source: str = "manual"  # "cad" | "manual" | "cad+manual"
    density_kg_m3: Optional[float] = None
    note: str = ""

    def inertia_matrix(self) -> np.ndarray:
        return np.asarray(self.inertia_kgm2, float).reshape(3, 3)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> Optional["MassProps"]:
        return None if d is None else cls(**_pick(cls, d))


@dataclass
class RigidBody:
    """A rigid body made of one or more components. ``mass`` is computed from CAD, ``manual`` overrides it."""

    name: str = ""
    component_ids: list = field(default_factory=list)
    grounded: bool = False
    mass: Optional[MassProps] = None
    manual: Optional[MassProps] = None
    id: str = field(default_factory=lambda: _new_id("rb"))

    def effective_mass(self) -> Optional[MassProps]:
        return self.manual if self.manual is not None else self.mass

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "RigidBody":
        d = dict(d)
        d["mass"] = MassProps.from_dict(d.get("mass"))
        d["manual"] = MassProps.from_dict(d.get("manual"))
        return cls(**_pick(cls, d))


# --------------------------------------------------------------------------- joints
@dataclass
class Joint:
    name: str = ""
    kind: str = "revolute"  # fixed | revolute | prismatic
    parent: str = GROUND  # body id or "ground"
    child: str = ""  # body id
    origin: list = field(default_factory=lambda: [0.0, 0.0, 0.0])  # assembly frame, project length unit
    axis: list = field(default_factory=lambda: [0.0, 0.0, 1.0])
    reversed: bool = False
    x_axis: Optional[list] = None
    limits: Optional[list] = None  # [lo, hi] rad (revolute) or project length unit (prismatic)
    initial_q: float = 0.0
    initial_qd: float = 0.0
    damping: float = 0.0  # N m s/rad (revolute) or N s/m (prismatic)
    id: str = field(default_factory=lambda: _new_id("joint"))

    def __post_init__(self) -> None:
        if self.x_axis is None and self.kind != "fixed":
            self.x_axis = self._perpendicular().tolist()

    # ---- frame ----------------------------------------------------------
    def z_axis(self) -> np.ndarray:
        a = np.asarray(self.axis, float)
        n = np.linalg.norm(a)
        if n < 1e-12:
            return np.array([0.0, 0.0, 1.0]) if self.kind == "fixed" else np.zeros(3)
        a = a / n
        return -a if self.reversed else a

    def _perpendicular(self) -> np.ndarray:
        z = self.z_axis()
        if np.linalg.norm(z) < 1e-12:
            return np.array([1.0, 0.0, 0.0])
        ref = np.zeros(3)
        ref[int(np.argmin(np.abs(z)))] = 1.0
        x = ref - np.dot(ref, z) * z
        return x / np.linalg.norm(x)

    def set_axis(self, axis, reversed_: Optional[bool] = None) -> None:
        """Change the axis; the stored x axis is recomputed (deterministically)."""
        self.axis = [float(v) for v in axis]
        if reversed_ is not None:
            self.reversed = bool(reversed_)
        self.x_axis = None if self.kind == "fixed" else self._perpendicular().tolist()

    def frame(self) -> tuple[np.ndarray, np.ndarray]:
        """(origin, R) with R's columns the joint x, y, z axes in assembly axes."""
        z = self.z_axis()
        x = np.asarray(self.x_axis if self.x_axis is not None else self._perpendicular(), float)
        if np.linalg.norm(z) > 1e-12:  # re-orthogonalise (the stored x may have been edited)
            x = x - np.dot(x, z) * z
            n = np.linalg.norm(x)
            x = x / n if n > 1e-12 else self._perpendicular()
        y = np.cross(z, x)
        return np.asarray(self.origin, float), np.column_stack([x, y, z])

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Joint":
        return cls(**_pick(cls, d))


@dataclass
class Coupling:
    """Ideal transmission ``q_follower = ratio * q_leader`` (massless, rigid, lossless, no backlash)."""

    name: str = ""
    leader: str = ""  # joint id
    follower: str = ""  # joint id
    ratio: float = 1.0
    note: str = ""
    id: str = field(default_factory=lambda: _new_id("cpl"))

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Coupling":
        return cls(**_pick(cls, d))


# --------------------------------------------------------------------------- profiles, drivers, loads
@dataclass
class Profile:
    """Scalar function of time: constant, ramp (held outside t0..t1) or table (linear, held outside)."""

    kind: str = "constant"  # constant | ramp | table
    value: Optional[float] = None
    t0: float = 0.0
    t1: float = 1.0
    v0: float = 0.0
    v1: float = 0.0
    table: Optional[list] = None  # [[t, v], ...]

    def breakpoints(self) -> tuple[np.ndarray, np.ndarray]:
        if self.kind == "constant":
            return np.array([0.0]), np.array([float(self.value or 0.0)])
        if self.kind == "ramp":
            return np.array([float(self.t0), float(self.t1)]), np.array([float(self.v0), float(self.v1)])
        if self.kind == "table":
            tab = np.asarray(self.table if self.table else [[0.0, 0.0]], float).reshape(-1, 2)
            tab = tab[np.argsort(tab[:, 0], kind="stable")]
            return tab[:, 0], tab[:, 1]
        raise ValueError(f"Unknown profile kind '{self.kind}'")

    def problems(self) -> list[str]:
        out = []
        if self.kind not in ("constant", "ramp", "table"):
            return [f"unknown profile kind '{self.kind}'"]
        if self.kind == "constant" and self.value is None:
            out.append("constant profile without a value")
        if self.kind == "ramp" and not self.t1 > self.t0:
            out.append("ramp profile needs t1 > t0")
        if self.kind == "table":
            if not self.table or len(self.table) < 1:
                out.append("table profile is empty")
            else:
                ts = [row[0] for row in self.table]
                if any(b <= a for a, b in zip(ts, ts[1:])):
                    out.append("table profile times must be strictly increasing")
        return out

    def value_at(self, t: float) -> float:
        ts, vs = self.breakpoints()
        return float(np.interp(t, ts, vs))

    def slope(self, t: float, side: str = "right") -> float:
        """dv/dt; at a breakpoint the right- (default) or left-hand derivative. Zero outside the table."""
        ts, vs = self.breakpoints()
        if len(ts) < 2:
            return 0.0
        if side == "right":
            i = int(np.searchsorted(ts, t, side="right")) - 1
        else:
            i = int(np.searchsorted(ts, t, side="left")) - 1
        if i < 0 or i >= len(ts) - 1:
            return 0.0
        return float((vs[i + 1] - vs[i]) / (ts[i + 1] - ts[i]))

    def _antiderivative(self, t: float) -> float:
        ts, vs = self.breakpoints()
        if t <= ts[0]:
            return float(vs[0] * (t - ts[0]))
        acc = 0.0
        for i in range(len(ts) - 1):
            if t >= ts[i + 1]:
                acc += 0.5 * (vs[i] + vs[i + 1]) * (ts[i + 1] - ts[i])
            else:
                v_t = vs[i] + (vs[i + 1] - vs[i]) * (t - ts[i]) / (ts[i + 1] - ts[i])
                return float(acc + 0.5 * (vs[i] + v_t) * (t - ts[i]))
        return float(acc + vs[-1] * (t - ts[-1]))

    def integral(self, t: float) -> float:
        """Exact integral of the profile from 0 to t."""
        return self._antiderivative(t) - self._antiderivative(0.0)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> Optional["Profile"]:
        return None if d is None else cls(**_pick(cls, d))


@dataclass
class Driver:
    """Command on one joint. position / velocity = PRESCRIBED (kinematic); effort = torque / force (dynamic)."""

    name: str = ""
    joint: str = ""
    mode: str = "position"  # position | velocity | effort
    profile: Profile = field(default_factory=Profile)
    id: str = field(default_factory=lambda: _new_id("drv"))

    @property
    def prescribed(self) -> bool:
        return self.mode in PRESCRIBED_MODES

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Driver":
        d = dict(d)
        d["profile"] = Profile.from_dict(d.get("profile")) or Profile()
        return cls(**_pick(cls, d))


@dataclass
class AppliedLoad:
    """Explicit external force / moment on a body at a point (never inferred). The optional profile is a
    dimensionless factor on ``force_N`` and ``moment_Nm``."""

    name: str = ""
    body: str = ""
    point: list = field(default_factory=lambda: [0.0, 0.0, 0.0])  # assembly frame, reference configuration
    force_N: list = field(default_factory=lambda: [0.0, 0.0, 0.0])
    moment_Nm: list = field(default_factory=lambda: [0.0, 0.0, 0.0])
    frame: str = "assembly"  # "assembly" (fixed directions) | "body" (rotates with the body)
    profile: Optional[Profile] = None
    note: str = ""
    id: str = field(default_factory=lambda: _new_id("load"))

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "AppliedLoad":
        d = dict(d)
        d["profile"] = Profile.from_dict(d.get("profile"))
        return cls(**_pick(cls, d))


@dataclass
class ThrustModel:
    """Rotor thrust / torque coefficients. Stored only; not used for flight prediction."""

    rotor_joint: str = ""
    kt: float = 0.0  # thrust = kt * omega^2 [N] (omega in rad/s)
    kq: float = 0.0  # drag torque = kq * omega^2 [N m]
    units_note: str = "thrust N, torque N m, omega rad/s"
    validity: str = ""
    source: str = "prescribed"  # prescribed | cfd
    id: str = field(default_factory=lambda: _new_id("thr"))

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ThrustModel":
        return cls(**_pick(cls, d))


# --------------------------------------------------------------------------- assembly
@dataclass
class Assembly:
    components: list = field(default_factory=list)
    bodies: list = field(default_factory=list)
    joints: list = field(default_factory=list)
    couplings: list = field(default_factory=list)
    thrust_models: list = field(default_factory=list)

    # ---- lookup ----------------------------------------------------------
    def body(self, bid: str) -> Optional[RigidBody]:
        return next((b for b in self.bodies if b.id == bid), None)

    def joint(self, jid: str) -> Optional[Joint]:
        return next((j for j in self.joints if j.id == jid), None)

    def component(self, cid: str) -> Optional[Component]:
        return next((c for c in self.components if c.id == cid), None)

    def body_of_component(self, cid: str) -> Optional[RigidBody]:
        return next((b for b in self.bodies if cid in b.component_ids), None)

    def is_empty(self) -> bool:
        return not (self.components or self.bodies or self.joints or self.couplings)

    # ---- validation ---------------------------------------------------------
    def validate(self) -> list[str]:
        """Structural problems of the kinematic graph (empty list = a valid kinematic tree)."""
        out: list[str] = []
        ids = [b.id for b in self.bodies]
        if len(set(ids)) != len(ids):
            out.append("duplicate rigid body ids")
        for b in self.bodies:
            for cid in b.component_ids:
                if self.component(cid) is None:
                    out.append(f"body '{b.name}' refers to unknown component '{cid}'")
        owner: dict[str, str] = {}
        for b in self.bodies:
            for cid in b.component_ids:
                if cid in owner and owner[cid] != b.id:
                    out.append(f"component '{cid}' belongs to two bodies")
                owner[cid] = b.id
        child_joint: dict[str, list[Joint]] = {}
        for j in self.joints:
            if j.kind not in JOINT_KINDS:
                out.append(f"joint '{j.name}': unknown kind '{j.kind}'")
            if j.parent != GROUND and self.body(j.parent) is None:
                out.append(f"joint '{j.name}': parent body '{j.parent}' does not exist")
            cb = self.body(j.child)
            if cb is None:
                out.append(f"joint '{j.name}': child body '{j.child}' does not exist")
                continue
            if j.parent == j.child:
                out.append(f"joint '{j.name}': parent and child are the same body")
            if cb.grounded:
                out.append(f"joint '{j.name}': child body '{cb.name}' is grounded (fixed to the world) and cannot "
                           "also be the child of a joint")
            if j.kind in ("revolute", "prismatic") and np.linalg.norm(np.asarray(j.axis, float)) < 1e-12:
                out.append(f"joint '{j.name}': axis is the zero vector")
            if j.limits is not None and not (len(j.limits) == 2 and j.limits[0] < j.limits[1]):
                out.append(f"joint '{j.name}': limits must be [lo, hi] with lo < hi")
            child_joint.setdefault(j.child, []).append(j)
        for bid, js in child_joint.items():
            if len(js) > 1:
                b = self.body(bid)
                out.append(f"body '{b.name if b else bid}' is the child of {len(js)} joints "
                           f"({', '.join(j.name for j in js)}): closed kinematic loops are not supported")
        for b in self.bodies:
            if not b.grounded and b.id not in child_joint:
                out.append(f"body '{b.name}' is not connected: it is not grounded and no joint attaches it")
        # cycles: follow parents up from each body
        for b in self.bodies:
            seen, cur = {b.id}, b.id
            while cur in child_joint and child_joint[cur][0].parent != GROUND:
                cur = child_joint[cur][0].parent
                if cur in seen:
                    out.append(f"kinematic cycle through body '{b.name}' (closed loops are not supported)")
                    break
                seen.add(cur)
        jids = {j.id for j in self.joints}
        for c in self.couplings:
            for role, jid in (("leader", c.leader), ("follower", c.follower)):
                if jid not in jids:
                    out.append(f"coupling '{c.name}': {role} joint '{jid}' does not exist")
            if c.leader == c.follower:
                out.append(f"coupling '{c.name}': leader and follower are the same joint")
        followers = [c.follower for c in self.couplings]
        for jid in set(followers):
            if followers.count(jid) > 1:
                j = self.joint(jid)
                out.append(f"joint '{j.name if j else jid}' is the follower of more than one coupling")
        return list(dict.fromkeys(out))

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "Assembly":
        d = d or {}
        return cls(components=[Component.from_dict(x) for x in d.get("components", [])],
                   bodies=[RigidBody.from_dict(x) for x in d.get("bodies", [])],
                   joints=[Joint.from_dict(x) for x in d.get("joints", [])],
                   couplings=[Coupling.from_dict(x) for x in d.get("couplings", [])],
                   thrust_models=[ThrustModel.from_dict(x) for x in d.get("thrust_models", [])])


# --------------------------------------------------------------------------- study settings
@dataclass
class MBDSettings:
    """Settings of a multibody study (stored as a dict in ``Study.mbd``)."""

    gravity: list = field(default_factory=lambda: [0.0, 0.0, -9.80665])  # m/s^2, assembly frame
    duration_s: float = 1.0
    dt_s: float = 1e-3
    output_dt_s: float = 1e-2
    drivers: list = field(default_factory=list)
    loads: list = field(default_factory=list)
    integrator: str = "RK4"  # RK4 | implicitfast
    notes: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "MBDSettings":
        d = dict(d or {})
        d["drivers"] = [Driver.from_dict(x) for x in d.get("drivers", [])]
        d["loads"] = [AppliedLoad.from_dict(x) for x in d.get("loads", [])]
        return cls(**_pick(cls, d))


def get_mbd(study: Any) -> MBDSettings:
    """MBD settings of a study (defaults if the study has none)."""
    return MBDSettings.from_dict(getattr(study, "mbd", None) or {})


def set_mbd(study: Any, settings: MBDSettings) -> None:
    study.mbd = settings.to_dict()
