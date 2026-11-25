"""Assembly + study  ->  MuJoCo model.

All unit conversion between the project unit system and SI happens here (and in ``run``/``results`` for the
output). Bodies are emitted as a kinematic tree in the reference configuration (all q = 0):

* every MuJoCo body frame is aligned with the assembly axes; its origin is the origin of the joint that attaches
  it (the world origin for grounded bodies);
* joint ``axis`` is the (possibly reversed) assembly-frame axis; the joint position is the body origin;
* inertia is given by ``fullinertia`` about the COM in assembly axes;
* a fixed joint is a body without joint (welded); a coupling is a MuJoCo joint equality constraint.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

import numpy as np

from opencae.assembly.model import (DRIVER_MODES, GROUND, PRESCRIBED_MODES, Assembly, Driver, Joint, MBDSettings,
                                    get_mbd)
from opencae.mbd.backend import MBDModelError, available
from opencae.utilities.units import get_unit_system

MAX_STEPS = 50_000_000
SOLIMP = "0.9999 0.9999 0.001 0.5 2"  # largest impedance MuJoCo allows: closest to a rigid constraint


@dataclass
class JointInfo:
    joint: Joint
    mj_joint: Optional[int]  # None for fixed joints
    qadr: int = -1
    dadr: int = -1
    mj_child: int = 0
    mj_parent: int = 0  # 0 = world
    R0: np.ndarray = field(default_factory=lambda: np.eye(3))  # joint frame in assembly axes (reference)
    origin_m: np.ndarray = field(default_factory=lambda: np.zeros(3))
    qs: float = 1.0  # metres per model unit of the coordinate (project length unit for prismatic, 1 for rad)
    driver: Optional[Driver] = None
    damping: float = 0.0

    @property
    def kind(self) -> str:
        return self.joint.kind

    @property
    def prescribed(self) -> bool:
        return self.driver is not None and self.driver.mode in PRESCRIBED_MODES

    @property
    def effort_driven(self) -> bool:
        return self.driver is not None and self.driver.mode == "effort"


@dataclass
class BuiltModel:
    model: Any
    mujoco: Any
    project_name: str
    settings: MBDSettings
    assembly: Assembly
    unit_length: str
    length_from_si: float  # multiply SI metres by this to get project length units
    body_mj: dict  # body id -> mj body index
    com0_m: dict  # body id -> reference COM (assembly frame, m)
    origin0_m: dict  # body id -> body-frame origin (assembly frame, m)
    joints: dict  # joint id -> JointInfo, in tree order
    couplings: list  # (leader JointInfo, follower JointInfo, ratio in MuJoCo coordinates, Coupling)
    kinematic: list  # JointInfo with a prescribed driver
    effort: list  # JointInfo with an effort driver
    determined: list  # (follower JointInfo, leader JointInfo, ratio) whose motion follows a kinematic leader
    loads: list  # AppliedLoad
    warnings: list = field(default_factory=list)
    body_mass_kg: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- validation
def _fmt(v) -> str:
    return " ".join(f"{float(x):.17g}" for x in np.atleast_1d(v))


def validate_mbd(project, study) -> list[str]:
    """Every reason why ``study`` cannot be simulated (empty list = ready)."""
    asm: Assembly = project.assembly
    S: MBDSettings = get_mbd(study)
    out: list[str] = []
    if not asm.bodies:
        return ["the assembly has no rigid bodies: add components and group them into bodies first"]
    out += asm.validate()
    if not any(b.grounded for b in asm.bodies) and not any(j.parent == GROUND for j in asm.joints):
        out.append("nothing is grounded: ground one body (or attach a joint to ground) so that the assembly "
                   "does not float freely")
    s = 1.0 / get_unit_system(project.unit_system).length_from_si

    for b in asm.bodies:
        mp = b.effective_mass()
        if b.grounded and mp is None:
            continue  # a grounded body does not move: its mass does not enter the dynamics
        if mp is None:
            out.append(f"body '{b.name}' has no mass properties: assign a material with a density to its "
                       "components (or enter mass and inertia manually)")
            continue
        if not np.isfinite(mp.mass_kg) or not mp.mass_kg > 0:
            out.append(f"body '{b.name}' has zero or negative mass ({mp.mass_kg:g} kg)")
            continue
        I = mp.inertia_matrix()
        if not np.all(np.isfinite(I)) or np.linalg.norm(I - I.T) > 1e-9 * max(np.linalg.norm(I), 1e-30) \
                or np.linalg.eigvalsh(0.5 * (I + I.T)).min() <= 0:
            out.append(f"body '{b.name}' has zero, non-symmetric or non-positive-definite inertia")

    # drivers
    by_joint: dict[str, list[Driver]] = {}
    for d in S.drivers:
        j = asm.joint(d.joint)
        if j is None:
            out.append(f"driver '{d.name}' refers to a joint that does not exist")
            continue
        if d.mode not in DRIVER_MODES:
            out.append(f"driver '{d.name}': unknown mode '{d.mode}' (use position, velocity or effort)")
        if j.kind == "fixed":
            out.append(f"driver '{d.name}' is on fixed joint '{j.name}': a fixed joint cannot be driven")
        for p in d.profile.problems():
            out.append(f"driver '{d.name}': {p}")
        by_joint.setdefault(j.id, []).append(d)
    for jid, ds in by_joint.items():
        if len(ds) > 1:
            j = asm.joint(jid)
            modes = {d.mode for d in ds}
            extra = (" The joint is prescribed (position/velocity) and also driven by effort: a prescribed joint "
                     "already carries whatever effort the motion needs." if "effort" in modes and len(modes) > 1
                     else "")
            out.append(f"joint '{j.name}' has {len(ds)} drivers ({', '.join(d.name for d in ds)}); apply a "
                       f"physical command only once: a joint takes a single driver.{extra}")
    followers = {c.follower for c in asm.couplings}
    for jid in followers & set(by_joint):
        out.append(f"joint '{asm.joint(jid).name}' is the follower of a coupling and also has a driver: "
                   "the transmission already determines its motion")
    kinds = {j.id: j.kind for j in asm.joints}
    for c in asm.couplings:
        for jid in (c.leader, c.follower):
            if kinds.get(jid) == "fixed":
                out.append(f"coupling '{c.name}' involves fixed joint '{asm.joint(jid).name}'")
        if not np.isfinite(c.ratio) or c.ratio == 0.0:
            out.append(f"coupling '{c.name}': ratio must be finite and non-zero")
    # coupling cycles (a follower that is its own ancestor leader)
    parent_of = {c.follower: c.leader for c in asm.couplings}
    for f in parent_of:
        seen, cur = {f}, f
        while cur in parent_of:
            cur = parent_of[cur]
            if cur in seen:
                out.append("couplings form a cycle: the transmission is over-constrained")
                break
            seen.add(cur)

    # loads
    for ld in S.loads:
        if asm.body(ld.body) is None:
            out.append(f"applied load '{ld.name}' is on a body that does not exist")
        if ld.frame not in ("assembly", "body"):
            out.append(f"applied load '{ld.name}': frame must be 'assembly' or 'body'")
        vals = list(ld.point) + list(ld.force_N) + list(ld.moment_Nm)
        if len(vals) != 9 or not np.all(np.isfinite(vals)):
            out.append(f"applied load '{ld.name}': point, force and moment need three finite numbers each")
        if ld.profile is not None:
            out += [f"applied load '{ld.name}': {p}" for p in ld.profile.problems()]

    # settings
    if len(S.gravity) != 3 or not np.all(np.isfinite(S.gravity)):
        out.append("gravity must be three finite numbers (m/s^2)")
    if S.integrator not in ("RK4", "implicitfast"):
        out.append(f"integrator must be RK4 or implicitfast, not '{S.integrator}'")
    if not S.dt_s > 0 or not S.duration_s > 0 or not S.output_dt_s > 0:
        out.append("dt, duration and output interval must be > 0")
    else:
        ratio = S.output_dt_s / S.dt_s
        if ratio < 1 - 1e-9 or abs(ratio - round(ratio)) > 1e-6:
            out.append(f"the output interval ({S.output_dt_s:g} s) must be an integer multiple of the time step "
                       f"({S.dt_s:g} s)")
        n = S.duration_s / S.output_dt_s
        if abs(n - round(n)) > 1e-6:
            out.append(f"the duration ({S.duration_s:g} s) must be an integer multiple of the output interval "
                       f"({S.output_dt_s:g} s)")
        if S.duration_s / S.dt_s > MAX_STEPS:
            out.append("too many time steps (> 5e7): increase dt or shorten the duration")

    # prescribed motion must respect the joint limits (the coordinate follows the command exactly)
    if not out:
        ts = np.linspace(0.0, S.duration_s, 2001)
        for d in S.drivers:
            j = asm.joint(d.joint)
            if d.mode not in PRESCRIBED_MODES or j is None or j.limits is None:
                continue
            if d.mode == "position":
                q = np.array([d.profile.value_at(t) for t in ts])
            else:
                q = j.initial_q + np.array([d.profile.integral(t) for t in ts])
            if q.min() < j.limits[0] - 1e-12 or q.max() > j.limits[1] + 1e-12:
                out.append(f"prescribed motion of joint '{j.name}' goes outside its limits "
                           f"[{j.limits[0]:g}, {j.limits[1]:g}] (command range [{q.min():g}, {q.max():g}])")
    return list(dict.fromkeys(out))


# --------------------------------------------------------------------------- build
def build_model(project, study) -> BuiltModel:
    ok, why = available()
    if not ok:
        raise MBDModelError(why)
    problems = validate_mbd(project, study)
    if problems:
        raise MBDModelError("The multibody study cannot be built:\n- " + "\n- ".join(problems), problems)
    import mujoco

    asm: Assembly = project.assembly
    S = get_mbd(study)
    us = get_unit_system(project.unit_system)
    s = 1.0 / us.length_from_si  # model length unit -> m
    drivers = {d.joint: d for d in S.drivers}

    child_joint = {j.child: j for j in asm.joints}
    kids: dict[str, list[Joint]] = {}
    for j in asm.joints:
        kids.setdefault(j.parent, []).append(j)

    body_mj: dict[str, int] = {}
    joints: dict[str, JointInfo] = {}
    com0: dict[str, np.ndarray] = {}
    org0: dict[str, np.ndarray] = {}
    masses: dict[str, float] = {}
    xml_body: list[str] = []
    counters = {"body": 0, "joint": 0}
    order: list[tuple[Joint, str]] = []  # (joint, mj body name) in emission order

    def emit(rb, origin: np.ndarray, parent_origin: np.ndarray, joint: Optional[Joint], depth: int) -> None:
        counters["body"] += 1
        bname = f"b{counters['body']}"
        body_mj[rb.id] = counters["body"]
        org0[rb.id] = origin
        mp = rb.effective_mass()
        pad = "  " * depth
        xml_body.append(f'{pad}<body name="{bname}" pos="{_fmt(origin - parent_origin)}">')
        if joint is not None and joint.kind != "fixed":
            counters["joint"] += 1
            jname = f"j{counters['joint']}"
            attrs = f'name="{jname}" type="{"hinge" if joint.kind == "revolute" else "slide"}" ' \
                    f'axis="{_fmt(joint.z_axis())}" pos="0 0 0" damping="{joint.damping:.17g}"'
            drv = drivers.get(joint.id)
            if joint.limits is not None and not (drv is not None and drv.mode in PRESCRIBED_MODES):
                q = 1.0 if joint.kind == "revolute" else s
                attrs += f' limited="true" range="{joint.limits[0] * q:.17g} {joint.limits[1] * q:.17g}"'
            xml_body.append(f"{pad}  <joint {attrs}/>")
            order.append((joint, jname))
        elif joint is not None:
            order.append((joint, ""))
        if mp is not None:
            c = np.asarray(mp.com, float) * s
            com0[rb.id] = c
            masses[rb.id] = float(mp.mass_kg)
            I = mp.inertia_matrix()
            I = 0.5 * (I + I.T)
            xml_body.append(f'{pad}  <inertial pos="{_fmt(c - origin)}" mass="{mp.mass_kg:.17g}" '
                            f'fullinertia="{_fmt([I[0, 0], I[1, 1], I[2, 2], I[0, 1], I[0, 2], I[1, 2]])}"/>')
        else:
            com0[rb.id] = origin
        for cj in kids.get(rb.id, []):
            emit(asm.body(cj.child), np.asarray(cj.origin, float) * s, origin, cj, depth + 1)
        xml_body.append(f"{pad}</body>")

    zero = np.zeros(3)
    for rb in asm.bodies:
        if rb.grounded:
            emit(rb, zero, zero, None, 1)
    for cj in kids.get(GROUND, []):
        emit(asm.body(cj.child), np.asarray(cj.origin, float) * s, zero, cj, 1)

    jname_of = {j.id: n for j, n in order}
    eq = []
    couplings_raw = []
    for c in asm.couplings:
        jl, jf = asm.joint(c.leader), asm.joint(c.follower)
        ql = 1.0 if jl.kind == "revolute" else s
        qf = 1.0 if jf.kind == "revolute" else s
        ratio_mj = c.ratio * qf / ql
        couplings_raw.append((c, ratio_mj))
        eq.append(f'    <joint joint1="{jname_of[c.follower]}" joint2="{jname_of[c.leader]}" '
                  f'polycoef="0 {ratio_mj:.17g} 0 0 0" solref="{2 * S.dt_s:.17g} 1" solimp="{SOLIMP}"/>')
    xml = (f'<mujoco model="opencae-mbd">\n  <compiler angle="radian" fusestatic="false" '
           f'inertiafromgeom="false"/>\n  <option timestep="{S.dt_s:.17g}" gravity="{_fmt(S.gravity)}" '
           f'integrator="{S.integrator}" solver="Newton" iterations="200" tolerance="1e-14" cone="pyramidal"/>\n'
           f'  <worldbody>\n' + "\n".join(xml_body) + "\n  </worldbody>\n"
           + (f"  <equality>\n" + "\n".join(eq) + "\n  </equality>\n" if eq else "") + "</mujoco>\n")
    try:
        model = mujoco.MjModel.from_xml_string(xml)
    except Exception as exc:
        raise MBDModelError(f"MuJoCo rejected the model: {exc}", [str(exc)]) from exc

    # resolve indices: bodies are emitted depth-first, so MuJoCo's body index is the emission counter
    body_mj_real = dict(body_mj)
    for bid, k in body_mj_real.items():
        assert mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_BODY, k) == f"b{k}"

    for j, jn in order:
        info = JointInfo(joint=j, mj_joint=None, mj_child=body_mj_real[j.child],
                         mj_parent=0 if j.parent == GROUND else body_mj_real[j.parent],
                         origin_m=np.asarray(j.origin, float) * s, R0=j.frame()[1],
                         qs=1.0 if j.kind == "revolute" else s, driver=drivers.get(j.id), damping=j.damping)
        if jn:
            jid = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_JOINT, jn)
            info.mj_joint = jid
            info.qadr = int(model.jnt_qposadr[jid])
            info.dadr = int(model.jnt_dofadr[jid])
        joints[j.id] = info

    couplings = [(joints[c.leader], joints[c.follower], r, c) for c, r in couplings_raw]
    kinematic = [i for i in joints.values() if i.prescribed]
    effort = [i for i in joints.values() if i.effort_driven]
    # followers whose motion is fixed by a kinematic leader (directly or through a chain of couplings)
    determined: list = []
    known = {i.joint.id for i in kinematic}
    changed = True
    while changed:
        changed = False
        for lead, fol, r, c in couplings:
            if lead.joint.id in known and fol.joint.id not in known:
                determined.append((fol, lead, r))
                known.add(fol.joint.id)
                changed = True
    return BuiltModel(model=model, mujoco=mujoco, project_name=project.name, settings=S, assembly=asm,
                      unit_length=us.length, length_from_si=us.length_from_si, body_mj=body_mj_real, com0_m=com0,
                      origin0_m=org0, joints=joints, couplings=couplings, kinematic=kinematic, effort=effort,
                      determined=determined, loads=list(S.loads), body_mass_kg=masses)
