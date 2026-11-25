"""Run a multibody study: time stepping, kinematic (prescribed) joints, reactions, result storage.

Method (see the plan, "Prescribed motion"):

* **effort drivers** are forward dynamics: the commanded torque / force is applied to the joint;
* **position / velocity drivers are kinematic**: the joint coordinate follows the command EXACTLY (velocity
  commands are integrated analytically from the profile); no servo, no controller dynamics. The effort the
  motion needs is the inverse-dynamics effort: each step the actuator force on every prescribed joint is solved
  (linear solve on MuJoCo's forward dynamics) so that the joint acceleration equals the commanded one; after the
  step the prescribed coordinates are reset to the exact command. Free joints see the prescribed motion through
  the full mass matrix;
* couplings are MuJoCo joint-equality constraints; their violation is reported every step.
"""
from __future__ import annotations

import contextlib
import time as _time
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from opencae.assembly.model import PRESCRIBED_MODES, get_mbd
from opencae.mbd import backend
from opencae.mbd.backend import MBDCancelled, MBDModelError
from opencae.mbd.build import BuiltModel, build_model
from opencae.mbd.results import MBDResult, quat_to_matrix

ASSUMPTIONS = (
    "Rigid bodies; joints are ideal (no clearance, no friction beyond the stated damping).",
    "No contact: gripping force exists only as an explicit applied load.",
    "Prescribed (position / velocity) joints follow the command exactly; the actuator effort is the "
    "inverse-dynamics effort needed for it (no motor, gearbox or controller model).",
    "Couplings are ideal transmissions: massless, rigid, lossless, no backlash, no friction; the follower is "
    "driven by the constraint (MuJoCo soft equality constraint with the stiffest allowed settings, "
    "violation reported).",
    "Rotor drivers move the rotor discs kinematically; thrust models are stored only, a rotor animation does "
    "not predict flight.",
)


@contextlib.contextmanager
def _capture_warnings(mj, sink: list):
    """Collect MuJoCo warnings (instead of the MUJOCO_LOG.TXT file in the working directory)."""
    try:
        mj.set_mju_user_warning(lambda msg: sink.append(str(msg).strip()))
    except Exception:  # pragma: no cover - older bindings
        pass
    try:
        yield
    finally:
        try:
            mj.set_mju_user_warning(None)
        except Exception:  # pragma: no cover
            pass


def _side(t_index: int, n_total: int) -> str:
    return "left" if t_index == n_total else "right"


def _kin_state(ji, t: float, side: str) -> tuple[float, float, float]:
    """Prescribed (q, qd, qdd) of a joint in MuJoCo (SI) coordinates at time t."""
    drv, P = ji.driver, ji.driver.profile
    if drv.mode == "position":
        q, qd, qdd = P.value_at(t), P.slope(t, side), 0.0
    else:  # velocity
        q, qd, qdd = ji.joint.initial_q + P.integral(t), P.value_at(t), P.slope(t, side)
    return q * ji.qs, qd * ji.qs, qdd * ji.qs


class _Sim:
    def __init__(self, bm: BuiltModel) -> None:
        self.bm = bm
        self.mj = bm.mujoco
        self.m = bm.model
        self.d = self.mj.MjData(self.m)
        self.S = bm.settings
        self.kin = bm.kinematic
        self.kd = np.array([k.dadr for k in self.kin], int)
        self.tau = np.zeros(len(self.kin))
        self.warnings: list[str] = []
        self.messages: list[str] = []  # MuJoCo warnings captured during the run
        self.viol = 0.0
        self._init_state()

    # ---- state -----------------------------------------------------------
    def _init_state(self) -> None:
        d, bm = self.d, self.bm
        for ji in bm.joints.values():
            if ji.mj_joint is None:
                continue
            d.qpos[ji.qadr] = ji.joint.initial_q * ji.qs
            d.qvel[ji.dadr] = ji.joint.initial_qd * ji.qs
        # couplings: start on the constraint manifold (leader first), report any inconsistent input
        warned: set = set()
        for _ in range(len(bm.couplings) + 1):
            for lead, fol, r, c in bm.couplings:
                q_f, qd_f = r * d.qpos[lead.qadr], r * d.qvel[lead.dadr]
                if not lead.prescribed and not fol.prescribed and abs(d.qpos[fol.qadr] - q_f) > 1e-9 \
                        and c.id not in warned:
                    warned.add(c.id)
                    self.warnings.append(f"coupling '{c.name}': the initial q of the follower did not satisfy the "
                                         "transmission; it was set to ratio x leader")
                d.qpos[fol.qadr], d.qvel[fol.dadr] = q_f, qd_f
        self.set_kinematic(0.0, "right", init=True)

    def set_kinematic(self, t: float, side: str, init: bool = False) -> None:
        d = self.d
        self.acmd = np.zeros(len(self.kin))
        for n, ji in enumerate(self.kin):
            q, qd, qdd = _kin_state(ji, t, side)
            d.qpos[ji.qadr], d.qvel[ji.dadr], self.acmd[n] = q, qd, qdd
        for fol, lead, r in self.bm.determined:  # velocity-level compatibility of the ideal transmission
            d.qvel[fol.dadr] = r * d.qvel[lead.dadr]
            if init:
                d.qpos[fol.qadr] = r * d.qpos[lead.qadr]

    # ---- forces ------------------------------------------------------------
    def apply_loads(self, t: float) -> None:
        d, bm = self.d, self.bm
        d.xfrc_applied[:] = 0.0
        s = 1.0 / bm.length_from_si
        for ld in bm.loads:
            k = bm.body_mj[ld.body]
            fac = ld.profile.value_at(t) if ld.profile is not None else 1.0
            F, M = np.asarray(ld.force_N, float) * fac, np.asarray(ld.moment_Nm, float) * fac
            R = d.xmat[k].reshape(3, 3)
            if ld.frame == "body":
                F, M = R @ F, R @ M
            p = d.xpos[k] + R @ (np.asarray(ld.point, float) * s - bm.origin0_m[ld.body])
            d.xfrc_applied[k, :3] += F
            d.xfrc_applied[k, 3:] += M + np.cross(p - d.xipos[k], F)  # transfer to the COM

    def apply_efforts(self, t: float) -> None:
        for ji in self.bm.effort:
            self.d.qfrc_applied[ji.dadr] = ji.driver.profile.value_at(t)

    def solve_dynamics(self, t: float) -> None:
        """Leave ``d`` in a consistent forward state at time ``t`` (prescribed state set, loads and efforts
        applied, inverse-dynamics effort solved so that prescribed joints have the commanded acceleration)."""
        mj, m, d = self.mj, self.m, self.d
        mj.mj_kinematics(m, d)
        self.apply_loads(t)
        self.apply_efforts(t)
        nk = len(self.kin)
        if nk == 0:
            mj.mj_forward(m, d)
            return
        kd, tau, G = self.kd, self.tau.copy(), None
        for _ in range(5):
            d.qfrc_applied[kd] = tau
            mj.mj_forward(m, d)
            r = d.qacc[kd] - self.acmd
            scale = 1.0 + float(np.max(np.abs(d.qacc))) + float(np.max(np.abs(self.acmd)))
            if np.max(np.abs(r)) <= 1e-11 * scale:
                break
            if G is None:
                h, base, G = 1e-2, d.qacc[kd].copy(), np.zeros((nk, nk))
                for c in range(nk):
                    d.qfrc_applied[kd[c]] = tau[c] + h
                    mj.mj_forward(m, d)
                    G[:, c] = (d.qacc[kd] - base) / h
                    d.qfrc_applied[kd[c]] = tau[c]
                try:
                    Ginv = np.linalg.inv(G)
                except np.linalg.LinAlgError as exc:
                    raise MBDModelError("a prescribed joint cannot be moved by an actuator at this configuration "
                                        "(singular inertia coupling)") from exc
            tau = tau - Ginv @ r
        else:
            d.qfrc_applied[kd] = tau
            mj.mj_forward(m, d)
        self.tau = tau

    def check(self, t: float) -> None:
        d = self.d
        bad = [self.mj.mjtWarning(i).name for i in range(len(d.warning)) if d.warning[i].number > 0]
        if bad or not (np.all(np.isfinite(d.qpos)) and np.all(np.isfinite(d.qvel))):
            why = self.messages[-1] if self.messages else (', '.join(bad) or 'NaN or infinite state')
            raise MBDModelError(f"the simulation became unstable at t = {t:g} s ({why}): reduce the time step, "
                                "check the masses and inertias, or soften the command")

    def update_violation(self) -> None:
        d = self.d
        for lead, fol, r, c in self.bm.couplings:
            self.viol = max(self.viol, abs(d.qpos[fol.qadr] - r * d.qpos[lead.qadr]))

    # ---- recording -----------------------------------------------------------
    def record(self, rec: dict, t: float) -> None:
        mj, m, d, bm = self.mj, self.m, self.d, self.bm
        mj.mj_rnePostConstraint(m, d)
        s_inv = bm.length_from_si
        for bid, k in bm.body_mj.items():
            rec["body_pos"][bid].append(d.xipos[k] * s_inv)
            rec["body_quat"][bid].append(d.xquat[k].copy())
        for jid, ji in bm.joints.items():
            if ji.mj_joint is not None:
                rec["joint_q"][jid].append(d.qpos[ji.qadr] / ji.qs)
                rec["joint_qd"][jid].append(d.qvel[ji.dadr] / ji.qs)
                rec["joint_qdd"][jid].append(d.qacc[ji.dadr] / ji.qs)
                if ji.prescribed:
                    eff = float(self.tau[self.kin.index(ji)])
                elif ji.effort_driven:
                    eff = float(ji.driver.profile.value_at(t))
                else:
                    eff = 0.0
            else:
                for g in ("joint_q", "joint_qd", "joint_qdd"):
                    rec[g][jid].append(0.0)
                eff = 0.0
            rec["joint_effort"][jid].append(eff)
            # wrench transmitted from the parent to the child, re-expressed at the joint origin point
            c = ji.mj_child
            tor, frc = d.cfrc_int[c][:3], d.cfrc_int[c][3:]
            p = d.xpos[c]
            mom = tor + np.cross(d.subtree_com[m.body_rootid[c]] - p, frc)
            Rj = d.xmat[ji.mj_parent].reshape(3, 3) @ ji.R0 if ji.mj_parent else ji.R0
            f_j, m_j = Rj.T @ frc, Rj.T @ mom
            f_red, m_red = f_j.copy(), m_j.copy()
            if ji.kind == "revolute":
                m_red[2] -= eff
            elif ji.kind == "prismatic":
                f_red[2] -= eff
            rec["reaction_force_total_joint"][jid].append(f_j)
            rec["reaction_moment_total_joint"][jid].append(m_j)
            rec["reaction_force_total_assembly"][jid].append(Rj @ f_j)
            rec["reaction_moment_total_assembly"][jid].append(Rj @ m_j)
            rec["reaction_force_joint"][jid].append(f_red)
            rec["reaction_moment_joint"][jid].append(m_red)
            rec["reaction_force_assembly"][jid].append(Rj @ f_red)
            rec["reaction_moment_assembly"][jid].append(Rj @ m_red)
        for drv in bm.settings.drivers:
            rec["driver_command"][drv.id].append(drv.profile.value_at(t))
        rec["constraint_error"].append(self.viol)
        self.viol = 0.0


def _meta(bm: BuiltModel, project, study, sim: _Sim, wall: float, viol_max: float) -> dict:
    asm, S = bm.assembly, bm.settings
    drivers = {d.joint: d for d in S.drivers}
    cpl_of = {c.follower: c for c in asm.couplings}
    lead_of = {c.leader: c for c in asm.couplings}

    def unit(j, d):
        if d.mode == "effort":
            return "N" if j.kind == "prismatic" else "N m"
        base = bm.unit_length if j.kind == "prismatic" else "rad"
        return base if d.mode == "position" else f"{base}/s"

    joints = []
    for j in asm.joints:
        d = drivers.get(j.id)
        kind = ("prescribed (kinematic)" if d is not None and d.mode in PRESCRIBED_MODES
                else "torque-driven (dynamic)" if d is not None
                else "coupled follower (driven by the constraint)" if j.id in cpl_of
                else "free" if j.kind != "fixed" else "fixed (welded)")
        o, R = j.frame()
        joints.append({"id": j.id, "name": j.name, "kind": j.kind, "parent": j.parent,
                       "parent_name": "ground" if j.parent == "ground" else asm.body(j.parent).name,
                       "child": j.child, "child_name": asm.body(j.child).name, "origin": o.tolist(),
                       "axis": R[:, 2].tolist(), "x_axis": R[:, 0].tolist(), "limits": j.limits,
                       "driver_kind": kind, "driver": d.id if d is not None else None,
                       "coupling_leader_of": lead_of[j.id].id if j.id in lead_of else None,
                       "coupling_follower_of": cpl_of[j.id].id if j.id in cpl_of else None})
    bodies = [{"id": b.id, "name": b.name, "grounded": b.grounded,
               "com0": (bm.com0_m[b.id] * bm.length_from_si).tolist(),
               "mass_kg": bm.body_mass_kg.get(b.id)} for b in asm.bodies]
    assumptions = list(ASSUMPTIONS)
    for c in asm.couplings:
        assumptions.append(f"Coupling '{c.name}': q_follower = {c.ratio:g} x q_leader. {c.note}".strip())
    return {
        "study": study.name, "study_id": study.id, "project": project.name,
        "backend": {"name": "MuJoCo", "version": backend.version()},
        "units": {"length": bm.unit_length, "mass": "kg", "force": "N", "moment": "N m", "angle": "rad",
                  "time": "s", "inertia": "kg m^2"},
        "frames": {"assembly": "project frame; reference configuration = all joint coordinates zero",
                   "joint": "origin = joint origin, z = joint axis (reversed if flagged), x = stored x axis; "
                            "carried by the parent body",
                   "body_position": "centre of mass, assembly frame",
                   "body_quaternion": "(w, x, y, z) of the body frame relative to the assembly frame"},
        "sign_conventions": {
            "reaction": "force / moment exerted BY the parent body ON the child body at the joint origin; "
                        "actuator effort along the joint axis excluded (see joint_effort); *_total_* includes it",
            "q": "revolute: right-hand rotation about the joint axis; prismatic: translation along it",
            "effort": "actuator torque / force along +q; for prescribed joints the inverse-dynamics effort"},
        "settings": S.to_dict(),
        "joints": joints, "bodies": bodies,
        "drivers": [{"id": d.id, "name": d.name, "joint": d.joint, "mode": d.mode,
                     "kind": "prescribed (kinematic)" if d.mode in PRESCRIBED_MODES else "torque-driven (dynamic)",
                     "unit": unit(asm.joint(d.joint), d), "profile": d.profile.to_dict()} for d in S.drivers],
        "loads": [ld.to_dict() for ld in S.loads],
        "components": {c.id: {"body": asm.body_of_component(c.id).id, "placement": c.placement,
                              "name": c.name} for c in asm.components if asm.body_of_component(c.id) is not None},
        "thrust_models": [t.to_dict() for t in asm.thrust_models],
        "constraint_violation_max": viol_max,
        "assumptions": assumptions, "warnings": list(sim.warnings),
        "n_steps": int(round(S.duration_s / S.dt_s)), "wall_time_s": wall,
    }


def run_study(project, study, progress: Optional[Callable[[float, str], None]] = None,
              cancelled: Optional[Callable[[], bool]] = None, data_dir: Optional[Path] = None) -> MBDResult:
    """Simulate a multibody study. Raises :class:`MBDModelError` for model problems or instability.

    ``data_dir``: the project's ``<stem>_data`` folder; the result is written to
    ``<data_dir>/results/<study id>/mbd.npz`` + ``mbd.json`` and recorded in ``study.results``."""
    t_wall = _time.perf_counter()
    bm = build_model(project, study)
    S = bm.settings
    sim = _Sim(bm)
    n_sub = int(round(S.output_dt_s / S.dt_s))
    n_out = int(round(S.duration_s / S.output_dt_s))
    n_total = n_out * n_sub
    gids = list(bm.joints)
    rec = {g: {k: [] for k in (gids if g.startswith(("joint", "reaction")) else
                               list(bm.body_mj) if g.startswith("body") else [d.id for d in S.drivers])}
           for g in ("body_pos", "body_quat", "joint_q", "joint_qd", "joint_qdd", "joint_effort",
                     "reaction_force_joint", "reaction_moment_joint", "reaction_force_assembly",
                     "reaction_moment_assembly", "reaction_force_total_joint", "reaction_moment_total_joint",
                     "reaction_force_total_assembly", "reaction_moment_total_assembly", "driver_command")}
    rec["constraint_error"] = []
    times: list[float] = []
    viol_max = 0.0
    mj, m, d = bm.mujoco, bm.model, sim.d
    with _capture_warnings(mj, sim.messages):
        for step in range(n_total + 1):
            t = step * S.dt_s
            sample = step % n_sub == 0
            side = _side(step, n_total)
            if step > 0:
                sim.set_kinematic(t, side)
                sim.update_violation()
            sim.solve_dynamics(t)
            sim.check(t)
            if sample:
                sim.record(rec, t)
                times.append(round(step / n_sub * S.output_dt_s, 12))
                viol_max = max(viol_max, rec["constraint_error"][-1])
                if progress:
                    progress(step / max(n_total, 1), f"t = {t:.4g} s")
                if cancelled and cancelled():
                    raise MBDCancelled("the multibody run was cancelled")
            if step == n_total:
                break
            mj.mj_step(m, d)
            sim.check(t + S.dt_s)
    res = MBDResult(t=np.asarray(times), constraint_error=np.asarray(rec["constraint_error"]),
                    meta=_meta(bm, project, study, sim, _time.perf_counter() - t_wall, viol_max))
    for grp in ("body_pos", "body_quat", "joint_q", "joint_qd", "joint_qdd", "joint_effort",
                "reaction_force_joint", "reaction_moment_joint", "reaction_force_assembly",
                "reaction_moment_assembly", "reaction_force_total_joint", "reaction_moment_total_joint",
                "reaction_force_total_assembly", "reaction_moment_total_assembly", "driver_command"):
        setattr(res, grp, {k: np.asarray(v, float) for k, v in rec[grp].items()})
    if data_dir is not None:
        rel = Path("results") / study.id
        res.save(Path(data_dir) / rel)
        from opencae.project.model import ResultsInfo
        study.results = ResultsInfo(directory=rel.as_posix(), jobname="mbd", completed=True,
                                    solver_version=f"MuJoCo {backend.version()}",
                                    wall_time_s=res.meta["wall_time_s"],
                                    summary={"kind": "mbd", "duration_s": S.duration_s, "n_samples": len(times),
                                             "constraint_violation_max": viol_max})
    return res
