"""Drone rotor assembly: the same implementation as the gripper (grounded frame, fixed joints, revolute rotors,
velocity drivers). The rotor motion is a kinematic animation of the commanded speed, not a flight prediction.
"""
import copy
import math

import numpy as np
import pytest

pytest.importorskip("mujoco", reason="MuJoCo is not installed (python -m pip install mujoco): Motion studies are unavailable")

from opencae.app.session import Session
from opencae.assembly import Profile, get_mbd, set_mbd
from opencae.examples.mbd_examples import DRONE as D
from opencae.examples.mbd_examples import build_drone_rotors
from opencae.mbd import MBDResult, pose_transforms, run_study

RHO = 2700.0
OMEGA = D["rpm"] * 2 * math.pi / 60
G = 9.80665
M_ROTOR = RHO * math.pi * (D["rotor_r"] * 1e-3) ** 2 * (D["rotor_t"] * 1e-3)
M_HOUSING = RHO * math.pi * (D["housing_r"] * 1e-3) ** 2 * (D["housing_h"] * 1e-3)
I_ROTOR = 0.5 * M_ROTOR * (D["rotor_r"] * 1e-3) ** 2  # disc about its axis
CW = {1: False, 2: True, 3: False, 4: True}  # rotor k: clockwise seen from +z


@pytest.fixture(scope="module")
def session(tmp_path_factory):
    s = Session()
    s.open(build_drone_rotors(tmp_path_factory.mktemp("drone")))
    return s


@pytest.fixture(scope="module")
def result(session):
    return run_study(session.project, session.project.studies[0], data_dir=session.data_dir)


def rotor_joints(project):
    return [j for j in project.assembly.joints if j.kind == "revolute"]


def omega_z(res, joint):
    """Angular velocity about +z (rad/s) from the joint speed and the joint axis stored in the result."""
    axis_z = next(j for j in res.meta["joints"] if j["id"] == joint.id)["axis"][2]
    return res.joint_qd[joint.id] * axis_z


def test_assembly_structure(session):
    asm = session.project.assembly
    assert asm.validate() == []
    assert [j.kind for j in asm.joints] == ["fixed", "revolute"] * 4
    assert sum(b.grounded for b in asm.bodies) == 1 and asm.bodies[0].name == "Frame"
    st = session.project.studies[0]
    assert st.analysis.analysis_type == "mbd" and all(d.mode == "velocity" for d in get_mbd(st).drivers)
    assert len(asm.thrust_models) == 4
    for t in asm.thrust_models:
        assert t.source == "prescribed" and "does not predict thrust or flight" in t.validity
    assert "not a flight prediction" in st.notes


def test_rotor_speeds_have_the_commanded_signs_and_values(session, result):
    t = result.t
    for k, j in enumerate(rotor_joints(session.project), start=1):
        sign = -1.0 if CW[k] else 1.0
        w = omega_z(result, j)
        expected = np.interp(t, [0, D["ramp_s"]], [0, OMEGA])
        assert w == pytest.approx(sign * expected, rel=1e-12, abs=1e-9)
        assert result.joint_qd[j.id][-1] == pytest.approx(OMEGA, rel=1e-12)  # commanded 6000 rpm along the axis
        # the body really turns that way: z component of its quaternion = sin(angle / 2) about +z
        theta = sign * (0.5 * OMEGA * D["ramp_s"] + OMEGA * (D["duration_s"] - D["ramp_s"]))
        q = result.body_quat[session.project.assembly.joint(j.id).child][-1]
        assert q[0] == pytest.approx(math.cos(theta / 2), abs=1e-9) and q[3] == pytest.approx(math.sin(theta / 2), abs=1e-9)
        assert result.joint_q[j.id][-1] == pytest.approx(0.5 * OMEGA * D["ramp_s"] + OMEGA * 0.5, rel=1e-12)


def test_changing_one_driver_profile_changes_only_that_rotor(session, result):
    proj = copy.deepcopy(session.project)
    st = proj.studies[0]
    S = get_mbd(st)
    rotors = rotor_joints(proj)
    third = next(d for d in S.drivers if d.joint == rotors[2].id)
    third.profile = Profile("ramp", t0=0.0, t1=D["ramp_s"], v0=0.0, v1=0.5 * OMEGA)  # 3000 rpm
    set_mbd(st, S)
    res2 = run_study(proj, st)
    for k, j in enumerate(rotors):
        same = np.array_equal(res2.joint_qd[j.id], result.joint_qd[j.id]) and \
            np.array_equal(res2.body_quat[proj.assembly.joint(j.id).child], result.body_quat[proj.assembly.joint(j.id).child])
        assert same == (k != 2)
    assert res2.joint_qd[rotors[2].id][-1] == pytest.approx(0.5 * OMEGA, rel=1e-12)
    for jid, arr in result.joint_effort.items():  # every other joint's effort and reaction unchanged
        if jid != rotors[2].id:
            assert np.allclose(res2.joint_effort[jid], arr, rtol=1e-12, atol=1e-15)
            assert np.allclose(res2.reaction_force_joint[jid], result.reaction_force_joint[jid], rtol=1e-12, atol=1e-12)


def test_frame_stays_fixed_and_housings_follow_it(session, result):
    asm = session.project.assembly
    frame, housings = asm.bodies[0], [b for b in asm.bodies if b.name.startswith("Motor housing")]
    assert np.ptp(result.body_pos[frame.id], axis=0) == pytest.approx(0.0, abs=1e-12)
    assert result.body_quat[frame.id] == pytest.approx(np.tile([1, 0, 0, 0], (len(result.t), 1)), abs=1e-14)
    for h in housings:
        assert np.ptp(result.body_pos[h.id], axis=0) == pytest.approx(0.0, abs=1e-12)
        assert result.body_quat[h.id] == pytest.approx(np.tile([1, 0, 0, 0], (len(result.t), 1)), abs=1e-14)
    # animation: the rotor discs turn about their own centre, the others keep their placement
    T = pose_transforms(result, len(result.t) - 1)
    comp = {c.name: c.id for c in asm.components}
    assert np.allclose(T[comp["Frame"]], np.eye(4), atol=1e-12)
    rot = T[comp["Rotor 1"]]
    centre = np.array([D["arm"], D["arm"], D["plate"][2] + D["housing_h"] + D["rotor_t"] / 2, 1.0])
    assert (rot @ centre)[:3] == pytest.approx(centre[:3], abs=1e-9)  # the disc centre is on the axis
    assert np.linalg.det(rot[:3, :3]) == pytest.approx(1.0)


def test_reactions_and_efforts_are_finite_and_match_hand_values(session, result):
    """Gravity only: the fixed mount of housing k carries the weight of housing + rotor (force by the parent on
    the child is +z); the rotor joint carries the rotor weight; the actuator effort of a spinning-up disc is
    I alpha (alpha = omega / ramp, for t < ramp) and 0 afterwards; a symmetric disc spinning about its axis
    gives no transverse moment."""
    asm = session.project.assembly
    for grp in ("reaction_force_joint", "reaction_moment_joint", "reaction_force_assembly", "joint_effort"):
        assert all(np.all(np.isfinite(a)) for a in getattr(result, grp).values())
    alpha = OMEGA / D["ramp_s"]
    for k in range(1, 5):
        mount = next(j for j in asm.joints if j.name == f"Housing {k} mount")
        axis = next(j for j in asm.joints if j.name == f"Rotor {k} axis")
        Fm = result.reaction_force_assembly[mount.id]
        assert Fm[:, 2] == pytest.approx((M_HOUSING + M_ROTOR) * G, rel=1e-9)
        assert np.abs(Fm[:, :2]).max() < 1e-9
        Fr = result.reaction_force_assembly[axis.id]
        assert Fr[:, 2] == pytest.approx(M_ROTOR * G, rel=1e-9)
        eff = result.joint_effort[axis.id]
        inside = result.t < D["ramp_s"] - 1e-9
        assert eff[inside] == pytest.approx(I_ROTOR * alpha, rel=1e-6)
        assert np.abs(eff[~inside]).max() < 1e-9
        assert np.abs(result.reaction_moment_joint[axis.id][:, :2]).max() < 1e-9
        # transmitted wrench = reaction + actuator along the axis (here the axis is +-z)
        tot = result.reaction_moment_total_joint[axis.id][:, 2] - result.reaction_moment_joint[axis.id][:, 2]
        assert tot == pytest.approx(eff, abs=1e-12)


def test_save_reopen_reproduces(session, result, tmp_path):
    s2 = Session()
    s2.open(session.save(tmp_path / "drone_copy.ocae"))
    r2 = run_study(s2.project, s2.project.studies[0], data_dir=s2.data_dir)
    for jid in result.joint_q:
        assert np.allclose(r2.joint_q[jid], result.joint_q[jid], rtol=1e-12, atol=1e-15)
        assert np.allclose(r2.reaction_force_joint[jid], result.reaction_force_joint[jid], rtol=1e-12, atol=1e-15)
    back = MBDResult.load(s2.data_dir / "results" / s2.project.studies[0].id)
    assert np.array_equal(back.joint_effort[next(iter(back.joint_effort))], r2.joint_effort[next(iter(r2.joint_effort))])
