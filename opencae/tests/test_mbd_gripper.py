"""Gripper example: prescribed closing (A), torque-driven (B), static hold with fingertip loads (C), plus the
model-validation errors and the save / reopen / reproduce check.

Geometry (mm, see ``opencae.examples.mbd_examples.GRIPPER``): palm grounded; fingers of length L, in-plane
thickness h and width b hang along +y from pivots at x = -p (left) and x = +p (right); both hinge axes are the
palm normal, reversed, so q > 0 rotates the tips towards each other. The right finger follows the left one:
q_right = -q_left (ideal transmission).
"""
import copy
import csv
import math

import numpy as np
import pytest

from opencae.app.session import Session
from opencae.assembly import Driver, MassProps, Profile, RigidBody, get_mbd, set_mbd
from opencae.examples.mbd_examples import GRIPPER as G
from opencae.examples.mbd_examples import build_gripper
from opencae.mbd import MBDModelError, MBDResult, available, pose_transforms, run_study

RHO = 2700.0  # mat_al6061 (library)
L, b, h, p = G["L"] * 1e-3, G["b"] * 1e-3, G["h"] * 1e-3, G["p"] * 1e-3
M_FINGER = RHO * L * b * h
# moment of inertia of one finger about its pivot axis (z): box about its COM m (h^2 + L^2) / 12, plus m (L/2)^2
I_PIVOT = M_FINGER * (h ** 2 + L ** 2) / 12 + M_FINGER * (L / 2) ** 2
RATIO = G["ratio"]


@pytest.fixture(scope="module")
def project_path(tmp_path_factory):
    return build_gripper(tmp_path_factory.mktemp("gripper"))


@pytest.fixture(scope="module")
def session(project_path):
    s = Session()
    s.open(project_path)
    return s


def pick(project, letter):
    return next(st for st in project.studies if st.name.startswith(letter + " "))


def study(session, letter):
    return pick(session.project, letter)


@pytest.fixture(scope="module")
def run_a(session):
    return run_study(session.project, study(session, "A"), data_dir=session.data_dir)


@pytest.fixture(scope="module")
def run_b(session):
    return run_study(session.project, study(session, "B"), data_dir=session.data_dir)


@pytest.fixture(scope="module")
def run_c(session):
    return run_study(session.project, study(session, "C"), data_dir=session.data_dir)


def ids(session):
    asm = session.project.assembly
    return dict(jl=asm.joints[0].id, jr=asm.joints[1].id, left=asm.bodies[1].id, right=asm.bodies[2].id,
                palm=asm.bodies[0].id, comps=[c.id for c in asm.components])


def test_backend_available_and_registered():
    ok, why = available()
    assert ok and why == ""
    from opencae.physics import physics_for
    assert physics_for("mbd").available() == (True, "")


def test_example_files_describe_the_idealisation(session):
    st = study(session, "A")
    assert "massless, rigid, lossless, without backlash and without friction" in st.notes
    assert st.analysis.analysis_type == "mbd" and get_mbd(st).drivers[0].mode == "position"
    asm = session.project.assembly
    assert asm.validate() == [] and asm.couplings[0].ratio == RATIO and "lossless" in asm.couplings[0].note
    assert asm.bodies[1].mass.mass_kg == pytest.approx(M_FINGER, rel=1e-9)


# --------------------------------------------------------------------------- study A: prescribed closing
def tip_gap_mm(session, res, i):
    """x distance between the inner fingertip corners (mm) from the animation poses of sample i."""
    T = pose_transforms(res, i)
    c = ids(session)["comps"]
    pl = np.array([-G["p"] + G["h"] / 2, G["L"], 0.0, 1.0])  # inner tip corner of the left finger (reference)
    pr = np.array([G["p"] - G["h"] / 2, G["L"], 0.0, 1.0])
    a, bb = T[c[1]] @ pl, T[c[2]] @ pr
    assert a[1] == pytest.approx(bb[1], abs=1e-9)  # symmetric closing: same y
    return bb[0] - a[0]


def test_gripper_a_gap_vs_geometry(session, run_a):
    """Fingertip gap vs geometry. The left finger rotates by phi = -theta about +z (axis reversed, theta = q_left =
    closing angle), the right one by +theta. The inner tip corners start at (-p + h/2, L) and (p - h/2, L)
    relative to the palm; rotating the left corner about its pivot (-p, 0) by -theta gives
    x_l = -p + (h/2) cos(theta) + L sin(theta), and the right one about (p, 0) by +theta gives
    x_r = p - (h/2) cos(theta) - L sin(theta); the tips stay at equal y. Hence
        gap(theta) = x_r - x_l = 2 p - h cos(theta) - 2 L sin(theta)
    (mm). With theta(t) = closed * t on 0..1 s: t = 0, 0.5 and 1.0 s. Tolerance 1e-6 m = 1e-3 mm."""
    th_end = math.radians(G["closed_deg"])
    for t in (0.0, 0.5, 1.0):
        th = th_end * t
        expected = 2 * G["p"] - G["h"] * math.cos(th) - 2 * G["L"] * math.sin(th)
        i = int(np.argmin(np.abs(run_a.t - t)))
        assert tip_gap_mm(session, run_a, i) == pytest.approx(expected, abs=1e-3)
    assert expected < 20.0  # closed gap about 15 mm: the fingers do not cross


def test_gripper_a_follower_is_minus_leader_and_motion_is_exact(session, run_a):
    j = ids(session)
    ql, qr = run_a.joint_q[j["jl"]], run_a.joint_q[j["jr"]]
    assert np.max(np.abs(qr + ql)) < 1e-8
    assert run_a.meta["constraint_violation_max"] < 1e-8 and np.max(run_a.constraint_error) < 1e-8
    assert np.allclose(ql, math.radians(G["closed_deg"]) * run_a.t, rtol=0, atol=1e-13)  # prescribed: exact
    assert np.allclose(run_a.joint_qd[j["jl"]][:-1], math.radians(G["closed_deg"]), atol=1e-12)
    assert np.allclose(run_a.joint_qd[j["jr"]], -run_a.joint_qd[j["jl"]], atol=1e-9)
    assert run_a.meta["joints"][0]["driver_kind"] == "prescribed (kinematic)"
    assert run_a.meta["joints"][1]["driver_kind"].startswith("coupled follower")


def test_gripper_a_effort_and_reaction_are_physical(session, run_a):
    """Constant closing speed, gravity along the hinge axis: no torque is needed (abs < 1e-9 N m) and the hinge
    carries the finger weight along z in the assembly frame (force by the parent ON the child = +m g)."""
    j = ids(session)
    assert np.max(np.abs(run_a.joint_effort[j["jl"]])) < 1e-9
    for key in ("jl", "jr"):
        Fz = run_a.reaction_force_assembly[j[key]][:, 2]
        assert Fz == pytest.approx(M_FINGER * 9.80665, rel=1e-8)
    # joint frame has z = -z_assembly (axis reversed): the same force reads -m g along the joint axis
    assert run_a.reaction_force_joint[j["jl"]][:, 2] == pytest.approx(-M_FINGER * 9.80665, rel=1e-8)


def test_inverse_dynamics_effort_equals_inertia_times_acceleration(session):
    """Velocity ramp 0 -> 1 rad/s in 0.1 s (alpha = 10 rad/s^2, gravity off): the prescribed joint needs
    (I_left + ratio^2 I_right) alpha = 2 I_pivot alpha at the motor (reflected through the transmission)."""
    proj = copy.deepcopy(session.project)
    st = pick(proj, "A")
    S = get_mbd(st)
    S.drivers[0] = Driver(name="v", joint=S.drivers[0].joint, mode="velocity",
                          profile=Profile("ramp", t0=0.0, t1=0.1, v0=0.0, v1=1.0))
    S.gravity, S.duration_s, S.output_dt_s = [0, 0, 0], 0.15, 1e-2
    set_mbd(st, S)
    res = run_study(proj, st)
    jl = proj.assembly.joints[0].id
    expected = (1 + RATIO ** 2) * I_PIVOT * 10.0
    eff = res.joint_effort[jl]
    assert eff[1:9] == pytest.approx(expected, rel=1e-3)  # inside the ramp (0.01 .. 0.09 s)
    assert abs(eff[-1]) < 1e-3 * expected  # constant speed afterwards
    assert res.joint_q[jl][-1] == pytest.approx(0.5 * 10 * 0.1 ** 2 + 1.0 * 0.05, rel=1e-12)  # exact integral
    assert res.meta["constraint_violation_max"] < 1e-8


def test_gripper_a_files_and_lookup(session, run_a):
    st = study(session, "A")
    d = session.data_dir / "results" / st.id
    assert (d / "mbd.npz").exists() and (d / "mbd.json").exists()
    assert st.results.completed and st.results.directory == f"results/{st.id}"
    back = MBDResult.load(d)
    j = ids(session)
    assert np.array_equal(back.joint_q[j["jl"]], run_a.joint_q[j["jl"]])
    assert back.meta["backend"]["name"] == "MuJoCo" and back.meta["units"]["length"] == "mm"
    snap = run_a.at(0.505)  # between samples: linear interpolation (the ramp is linear: exact)
    assert snap["joint_q"][j["jl"]] == pytest.approx(math.radians(G["closed_deg"]) * 0.505, rel=1e-12)
    with pytest.raises(ValueError):
        run_a.at(5.0)


# --------------------------------------------------------------------------- study B: torque-driven
def test_gripper_b_initial_acceleration_matches_hand_calc(session, run_b):
    """Constant motor torque tau on the left joint, gravity off, rest. With q_right = -q_left the virtual work
    gives (I_left + ratio^2 I_right) alpha = tau, where I = m (h^2 + L^2) / 12 + m (L/2)^2 about the pivot axis."""
    j = ids(session)
    alpha = G["motor_torque_Nm"] / ((1 + RATIO ** 2) * I_PIVOT)
    qdd = run_b.joint_qdd[j["jl"]]
    assert qdd == pytest.approx(alpha, rel=1e-3)  # every sample incl. t = 0
    assert run_b.joint_qdd[j["jr"]] == pytest.approx(-alpha, rel=1e-3)
    assert run_b.joint_effort[j["jl"]] == pytest.approx(G["motor_torque_Nm"], rel=1e-12)
    assert run_b.joint_q[j["jl"]][-1] == pytest.approx(0.5 * alpha * run_b.t[-1] ** 2, rel=1e-3)
    assert run_b.meta["constraint_violation_max"] < 1e-8
    assert run_b.meta["joints"][0]["driver_kind"] == "torque-driven (dynamic)"


def test_gripper_b_doubling_finger_inertia_halves_acceleration(session, run_b):
    proj = copy.deepcopy(session.project)
    for rb in proj.assembly.bodies[1:]:  # both fingers: twice the mass and the inertia (about every axis)
        m = rb.mass
        rb.manual = MassProps(2 * m.mass_kg, list(m.com), (2 * m.inertia_matrix()).tolist(), "manual",
                              note="doubled for the test")
    st = pick(proj, "B")
    res = run_study(proj, st)
    jl = proj.assembly.joints[0].id
    j = ids(session)
    ratio = res.joint_qdd[jl][0] / run_b.joint_qdd[j["jl"]][0]
    assert ratio == pytest.approx(0.5, rel=1e-3)
    assert res.joint_qdd[jl][5:] == pytest.approx(0.5 * run_b.joint_qdd[j["jl"]][5:], rel=1e-3)


def test_gripper_b_csv_export_has_labelled_headers(session, run_b, tmp_path):
    j = ids(session)
    files = run_b.export_csv(tmp_path, joints=[j["jl"]], bodies=[j["left"]])
    assert {f.name for f in files} == {"joint_Left_hinge.csv", "body_Left_finger.csv"}
    text = (tmp_path / "joint_Left_hinge.csv").read_text().splitlines()
    head = [ln for ln in text if ln.startswith("#")]
    assert any("quantity: q;" in ln and "unit: rad;" in ln and "frame:" in ln and "sign:" in ln for ln in head)
    assert any("quantity: reaction_moment_z" in ln and "unit: N m" in ln and "joint frame" in ln
               and "BY the parent body ON the child body" in ln for ln in head)
    assert any("quantity: effort" in ln and "torque-driven" in ln for ln in head)
    assert any("units: length mm" in ln and "angle rad" in ln for ln in head)
    rows = list(csv.reader(ln for ln in text if not ln.startswith("#")))
    assert rows[0][0] == "t[s]" and "q[rad]" in rows[0] and "effort[N m]" in rows[0]
    assert len(rows) == len(run_b.t) + 1
    col = rows[0].index("q[rad]")
    assert [float(r[col]) for r in rows[1:]] == pytest.approx(run_b.joint_q[j["jl"]], rel=1e-9)
    body = (tmp_path / "body_Left_finger.csv").read_text()
    assert "quantity: com_x; unit: mm; frame: assembly frame" in body


# --------------------------------------------------------------------------- study C: static hold with loads
def test_gripper_c_static_hold_reactions_equal_the_fingertip_load(session, run_c):
    """Hold at theta = 20 deg; the tip load F is perpendicular to the finger (finger frame) at distance L from the
    pivot. The finger is in equilibrium, so the parent exerts on it -F (force) and -(r x F) (moment about the
    pivot), |moment| = F L about the hinge axis. Measured on the settled samples (t >= 0.05 s); the first samples
    carry the 1e-4 start-up error of MuJoCo's soft constraint (see the plan)."""
    j = ids(session)
    F, Lm, th = G["tip_force_N"], G["L"] * 1e-3, math.radians(G["closed_deg"])
    sel = run_c.t >= 0.05
    # load directions in the assembly frame: the left finger is rotated by -theta about +z, the right by +theta
    for key, sx, phi in (("jl", -1.0, -th), ("jr", +1.0, th)):
        load = F * np.array([sx * math.cos(phi), sx * math.sin(phi), 0.0])
        Fa = run_c.reaction_force_assembly[j[key]][sel]
        assert Fa == pytest.approx(np.tile(-load, (sel.sum(), 1)), rel=1e-6, abs=1e-6 * F)
        assert np.linalg.norm(Fa, axis=1) == pytest.approx(F, rel=1e-6)
        # moment: -(r x F), r from the pivot to the tip centre (finger rotated by phi), about +z
        r = Lm * np.array([-math.sin(phi), math.cos(phi), 0.0])
        Ma = -np.cross(r, load)
        assert run_c.reaction_moment_total_assembly[j[key]][sel] == pytest.approx(
            np.tile(Ma, (sel.sum(), 1)), rel=1e-6, abs=1e-6 * F * Lm)
        assert abs(Ma[2]) == pytest.approx(F * Lm, rel=1e-12)  # = F L about the hinge axis
        assert np.abs(run_c.reaction_moment_total_joint[j[key]][sel][:, 2]) == pytest.approx(F * Lm, rel=1e-6)
    # hinge-axis moments in the joint frame: total (what a root support carries) and without the actuator
    assert run_c.reaction_moment_total_joint[j["jl"]][sel][:, 2] == pytest.approx(+F * Lm, rel=1e-6)
    assert run_c.reaction_moment_total_joint[j["jr"]][sel][:, 2] == pytest.approx(-F * Lm, rel=1e-6)


def test_gripper_c_actuator_hold_torque_follows_the_coupling(session, run_c):
    """Virtual work through the ideal coupling q_r = -q_l: tau_motor = Q_r - Q_l with Q_i the generalised force
    of the tip load about each hinge axis (Q_l = -F L, Q_r = +F L), so the motor holds 2 F L = 0.6 N m, and
    reports it as joint_effort on the left joint, separately from the reaction (which keeps -F L there)."""
    j = ids(session)
    F, Lm = G["tip_force_N"], G["L"] * 1e-3
    sel = run_c.t >= 0.05
    assert run_c.joint_effort[j["jl"]][sel] == pytest.approx(2 * F * Lm, rel=1e-6)
    assert np.all(run_c.joint_effort[j["jr"]] == 0.0)  # the follower has no actuator
    assert run_c.reaction_moment_joint[j["jl"]][sel][:, 2] == pytest.approx(-F * Lm, rel=1e-6)  # total - effort
    assert run_c.reaction_moment_joint[j["jr"]][sel][:, 2] == pytest.approx(-F * Lm, rel=1e-6)
    # hold: no motion; the follower stays on the constraint
    assert np.max(np.abs(run_c.joint_qd[j["jl"]])) == 0.0
    assert run_c.joint_q[j["jl"]] == pytest.approx(math.radians(G["closed_deg"]), abs=1e-13)
    assert np.max(np.abs(run_c.joint_q[j["jr"]] + run_c.joint_q[j["jl"]])) < 1e-8
    assert run_c.meta["constraint_violation_max"] < 1e-8
    assert np.max(np.abs(run_c.joint_qdd[j["jr"]][sel])) < 1e-6


# --------------------------------------------------------------------------- validation errors
def fresh(session):
    proj = copy.deepcopy(session.project)
    return proj, pick(proj, "B")


def test_two_drivers_on_one_joint_is_an_error(session):
    proj, st = fresh(session)
    S = get_mbd(st)
    S.drivers.append(Driver(name="second", joint=S.drivers[0].joint, mode="position", profile=Profile(value=0.1)))
    set_mbd(st, S)
    with pytest.raises(MBDModelError) as e:
        run_study(proj, st)
    assert "apply a physical command only once" in str(e.value) and "prescribed" in str(e.value)
    assert "Left hinge" in str(e.value)
    # two effort drivers on a joint: also refused
    S.drivers[1] = Driver(name="second", joint=S.drivers[0].joint, mode="effort", profile=Profile(value=1.0))
    set_mbd(st, S)
    with pytest.raises(MBDModelError, match="only once"):
        run_study(proj, st)


def test_driver_on_coupling_follower_is_an_error(session):
    proj, st = fresh(session)
    S = get_mbd(st)
    S.drivers.append(Driver(name="right", joint=proj.assembly.joints[1].id, mode="effort", profile=Profile(value=1.0)))
    set_mbd(st, S)
    with pytest.raises(MBDModelError, match="follower of a coupling"):
        run_study(proj, st)


def test_unconnected_body_and_missing_mass_and_loops_are_errors(session):
    proj, st = fresh(session)
    proj.assembly.bodies.append(RigidBody(name="Loose part"))
    with pytest.raises(MBDModelError, match="Loose part.*not connected"):
        run_study(proj, st)
    proj, st = fresh(session)
    proj.assembly.bodies[1].mass = None
    with pytest.raises(MBDModelError, match="Left finger.*no mass properties"):
        run_study(proj, st)
    proj, st = fresh(session)
    proj.assembly.bodies[1].mass.mass_kg = 0.0
    with pytest.raises(MBDModelError, match="zero or negative mass"):
        run_study(proj, st)
    proj, st = fresh(session)
    proj.assembly.bodies[1].mass.inertia_kgm2 = np.zeros((3, 3)).tolist()
    with pytest.raises(MBDModelError, match="inertia"):
        run_study(proj, st)
    proj, st = fresh(session)
    proj.assembly.joints[1].child = proj.assembly.joints[0].child  # right hinge now also attaches the left finger
    with pytest.raises(MBDModelError, match="closed kinematic loops"):
        run_study(proj, st)
    proj, st = fresh(session)
    for rb in proj.assembly.bodies:
        rb.grounded = False
    with pytest.raises(MBDModelError, match="not connected|nothing is grounded"):
        run_study(proj, st)


def test_prescribed_motion_outside_limits_and_bad_time_settings_are_errors(session):
    proj, st = fresh(session)
    S = get_mbd(st)
    S.drivers[0] = Driver(name="too far", joint=S.drivers[0].joint, mode="position",
                          profile=Profile("ramp", t0=0, t1=0.05, v0=0, v1=1.0))  # 57 deg > 30 deg limit
    set_mbd(st, S)
    with pytest.raises(MBDModelError, match="outside its limits"):
        run_study(proj, st)
    proj, st = fresh(session)
    S = get_mbd(st)
    S.output_dt_s = 1.55e-4
    set_mbd(st, S)
    with pytest.raises(MBDModelError, match="integer multiple"):
        run_study(proj, st)


def test_instability_is_reported_not_a_crash(session):
    """Explicit damping far above the stability limit of RK4 blows up: the run must raise a readable error."""
    proj, st = fresh(session)
    proj.assembly.joints[0].damping = 1e9
    S = get_mbd(st)
    S.integrator = "RK4"
    set_mbd(st, S)
    with pytest.raises(MBDModelError, match="unstable"):
        run_study(proj, st)


# --------------------------------------------------------------------------- save, reopen, reproduce
def test_save_reopen_run_again_reproduces_results(session, tmp_path):
    s = Session()
    s.open(build_gripper(tmp_path / "first"))
    st = study(s, "B")
    r1 = run_study(s.project, st, data_dir=s.data_dir)
    path = s.save(tmp_path / "second" / "reopened.ocae")
    s2 = Session()
    s2.open(path)
    st2 = study(s2, "B")
    assert st2.results.completed  # the result record survives save / reopen
    r2 = run_study(s2.project, st2, data_dir=s2.data_dir)
    r3 = MBDResult.load(s2.data_dir / "results" / st2.id)
    for grp in ("joint_q", "joint_qd", "joint_qdd", "joint_effort", "reaction_force_joint",
                "reaction_moment_joint", "reaction_force_assembly", "body_pos", "body_quat"):
        a, b2, c = getattr(r1, grp), getattr(r2, grp), getattr(r3, grp)
        assert list(a) == list(b2) == list(c)
        for k in a:
            assert np.allclose(a[k], b2[k], rtol=1e-12, atol=1e-15) and np.allclose(b2[k], c[k], rtol=1e-12, atol=1e-15)
    assert np.allclose(r1.t, r2.t, rtol=1e-12) and np.allclose(r1.constraint_error, r2.constraint_error, atol=1e-15)


# --------------------------------------------------------------------------- applied load options
def test_applied_load_in_assembly_frame_with_profile(session):
    """Left tip load fixed in the assembly frame, force (-F, 0, 0), scaled by a ramp profile f(t) = t / 0.1 s.
    The finger is held at theta = 20 deg (rotated by -theta about +z), so the tip centre is at
    r = L (sin(theta), cos(theta), 0) from the pivot. Force by the parent on the finger: (+F f, 0, 0). Moment of
    the load about the pivot: z = x Fy - y Fx = L cos(theta) F f about +z; about the joint axis (-z) the parent's
    (balancing) moment is therefore +F f L cos(theta) in the joint frame."""
    proj = copy.deepcopy(session.project)
    st = pick(proj, "C")
    S = get_mbd(st)
    S.loads = [S.loads[0]]
    S.loads[0].frame = "assembly"
    S.loads[0].profile = Profile("ramp", t0=0.0, t1=0.1, v0=0.0, v1=1.0)
    set_mbd(st, S)
    res = run_study(proj, st)
    jl = proj.assembly.joints[0].id
    F, Lm, th = G["tip_force_N"], G["L"] * 1e-3, math.radians(G["closed_deg"])
    f = res.t / 0.1
    sel = res.t >= 0.01
    assert res.reaction_force_assembly[jl][sel][:, 0] == pytest.approx(F * f[sel], rel=1e-6)
    assert np.abs(res.reaction_force_assembly[jl][:, 1:]).max() < 1e-6
    assert res.reaction_moment_total_joint[jl][sel][:, 2] == pytest.approx(F * f[sel] * Lm * math.cos(th), rel=1e-5)
    assert res.driver_command[S.drivers[0].id] == pytest.approx(th, abs=1e-15)
