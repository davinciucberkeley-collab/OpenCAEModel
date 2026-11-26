"""Multibody -> structural static-hold transfer (gripper study C, fingertip load).

Left finger (CAD frame, mm): box x in [-p-h/2, -p+h/2], y in [0, L], z in [-b/2, b/2]; root face y = 0 (fixed),
load face y = L. The MBD fingertip load is -5 N along the body x axis (the thin direction h = 4 mm), so the finger
is a cantilever bent about z: I = b h^3 / 12, L_eff = distance from the root face to the centroid of the load face
(= L = 60 mm), delta = F L_eff^3 / (3 E I).

Hand-calc expectations (not exact): Timoshenko shear adds about E h^2 / (4 kappa G L^2) = 0.35 % to the
deflection; the clamped root restrains the cross-section warping / Poisson contraction (width b = 2.5 h), which
stiffens the root region by a few percent of the deflection. The FE tip deflection (mean u_x of the nodes of the
load face) is therefore expected slightly BELOW the hand calculation; the tolerance is 5 %.
"""
import math

import numpy as np
import pytest

pytest.importorskip("mujoco", reason="MuJoCo is not installed (python -m pip install mujoco): Motion studies are unavailable")

from opencae.app.session import Session
from opencae.assembly import get_mbd
from opencae.assembly.transfer import (STATIC_HOLD_NOTE, TransferError, TransferRecord, get_transfer,
                                       make_static_hold_study, transfer_summary)
from opencae.drone.runlock import solver_lock
from opencae.examples.mbd_examples import GRIPPER as G
from opencae.examples.mbd_examples import build_gripper
from opencae.geometry.references import find_planar_face, make_ref
from opencae.mbd import run_study

L, b, h, p, F = G["L"], G["b"], G["h"], G["p"], G["tip_force_N"]


@pytest.fixture(scope="module")
def session(tmp_path_factory):
    s = Session()
    s.open(build_gripper(tmp_path_factory.mktemp("transfer")))
    return s


def mbd(session, letter):
    return next(st for st in session.project.studies if st.name.startswith(letter + " "))


@pytest.fixture(scope="module")
def run_c(session):
    return run_study(session.project, mbd(session, "C"), data_dir=session.data_dir)


@pytest.fixture(scope="module")
def run_b(session):
    return run_study(session.project, mbd(session, "B"), data_dir=session.data_dir)


def left(session):
    asm = session.project.assembly
    rb = asm.bodies[1]
    gid = asm.component(rb.component_ids[0]).body_id
    body = session.bodies[gid]
    root = make_ref(body, "face", find_planar_face(body, [0, -1, 0], [-p, 0, 0]))
    tip = make_ref(body, "face", find_planar_face(body, [0, 1, 0], [-p, L, 0]))
    return rb, gid, root, tip


def transfer(session, run, letter="C", t=None, **kw):
    rb, gid, root, tip = left(session)
    return make_static_hold_study(session.project, mbd(session, letter), run.t[-1] if t is None else t, rb.id,
                                  root, tip, result=run, data_dir=session.data_dir, **kw)


def test_record_contents_force_moment_and_equilibrium(session, run_c):
    st, rec = transfer(session, run_c)
    assert st in session.project.studies and st.analysis.analysis_type == "linear_static"
    # the resultant equals the MBD applied load; the load is -F along the body x axis, rotated by nothing in CAD
    assert np.linalg.norm(rec.force_N) == pytest.approx(F, rel=1e-9)
    assert rec.force_N == pytest.approx([-F, 0.0, 0.0], abs=1e-9)
    mb = get_mbd(mbd(session, "C")).loads[0]
    assert np.linalg.norm(rec.force_N) == pytest.approx(np.linalg.norm(mb.force_N), rel=1e-9)
    # moment about the root joint point = F L (z axis, m)
    assert np.linalg.norm(rec.moment_Nm) == pytest.approx(F * L * 1e-3, rel=1e-6)
    eq = rec.equilibrium_check
    assert eq["residual"]["force_norm_N"] < 1e-6 * F and eq["residual"]["moment_norm_Nm"] < 1e-6 * F * L * 1e-3
    assert eq["ok"]
    # the root reaction is stored (CAD frame) but not applied: -applied
    assert eq["root_reaction_N"] == pytest.approx([F, 0.0, 0.0], abs=1e-6)
    assert eq["root_moment_Nm"] == pytest.approx(-np.asarray(rec.moment_Nm), abs=1e-6)
    # only the root is fixed; one force load on the tip face
    assert [c.kind for c in st.constraints] == ["fixed"] and [ld.kind for ld in st.loads] == ["force"]
    assert st.body_ids == [left(session)[1]] and st.loads[0].params["magnitude"] == pytest.approx(F, rel=1e-9)
    # handoff is visible
    assert rec.time_s == run_c.t[-1] and rec.body["name"] == "Left finger" and rec.units == "N, mm, MPa"
    assert rec.source_study["name"].startswith("C ") and rec.note == STATIC_HOLD_NOTE
    assert rec.region["load_face"]["area"] == pytest.approx(b * h) and rec.region["root_face"]["area"] == \
        pytest.approx(b * h)
    assert rec.acceleration["max_abs_qdd"] < 1e-6 and not rec.acceleration["accelerating"]
    text = transfer_summary(rec)
    for needle in ("Static-hold", "Left finger", "N, mm, MPa", "Equilibrium check", "no inertia", "CAD frame"):
        assert needle in text


def test_rotated_pose_is_expressed_in_the_cad_frame(session, run_c):
    """The MBD pose at t is a rigid motion (finger rotated 20 deg); the body-frame load must come back along CAD x
    and the assembly-frame reaction be rotated back: root reaction = -applied in CAD axes."""
    st, rec = transfer(session, run_c)
    j = session.project.assembly.joints[0]
    Fa = run_c.at(run_c.t[-1])["reaction_force_total_assembly"][j.id]
    assert np.linalg.norm(Fa) == pytest.approx(F, rel=1e-9) and abs(Fa[1]) > 1.0  # rotated in the assembly frame
    assert rec.equilibrium_check["root_reaction_N"][1] == pytest.approx(0.0, abs=1e-9)


def test_accelerating_snapshot_is_refused(session, run_b):
    rb, gid, root, tip = left(session)
    n = len(session.project.studies)
    with pytest.raises(TransferError, match=r"accelerating.*static-hold study neglects inertia"):
        make_static_hold_study(session.project, mbd(session, "B"), 0.04, rb.id, root, tip, result=run_b)
    assert len(session.project.studies) == n
    # explicit override: transferred with a visible warning (and no tip load in B -> still refused for lack of load)
    with pytest.raises(TransferError, match="no applied load"):
        make_static_hold_study(session.project, mbd(session, "B"), 0.04, rb.id, root, tip, result=run_b,
                               force_allow=True)


def test_accelerating_with_force_allow_warns(session, run_c):
    """Study C is at rest, so force a tight tolerance to emulate an accelerating snapshot."""
    st, rec = transfer(session, run_c, accel_tol=-1.0, force_allow=True)
    assert rec.acceleration["accelerating"] and any("accelerating" in w for w in rec.warnings)
    with pytest.raises(TransferError, match="accelerating"):
        transfer(session, run_c, accel_tol=-1.0)


def test_gravity_is_part_of_the_load_case_and_of_the_equilibrium(session):
    """With gravity on, the weight m g at the COM joins the fingertip load (and a structural gravity load); the
    root reaction still balances the sum (force and moment about the root joint point)."""
    import copy
    st = copy.deepcopy(mbd(session, "C"))
    st.id += "_g"
    s = get_mbd(st)
    s.gravity = [0.0, 0.0, -9.80665]
    s.duration_s, s.output_dt_s = 0.01, 1e-3
    from opencae.assembly import set_mbd
    set_mbd(st, s)
    run = run_study(session.project, st)
    rb, gid, root, tip = left(session)
    n0 = len(session.project.studies)
    new, rec = make_static_hold_study(session.project, st, run.t[-1], rb.id, root, tip, result=run)
    weight = 6.48e-3 * 9.80665
    assert rec.force_N[2] == pytest.approx(-weight, rel=1e-6) and rec.force_N[0] == pytest.approx(-F, rel=1e-9)
    assert rec.equilibrium_check["ok"], rec.equilibrium_check["residual"]
    assert sorted(ld.kind for ld in new.loads) == ["force", "gravity"]
    session.project.studies.remove(new)
    assert len(session.project.studies) >= n0 - 1


def test_save_and_reopen_keeps_the_record(session, run_c, tmp_path):
    st, rec = transfer(session, run_c)
    path = session.save(tmp_path / "with_transfer.ocae")
    s2 = Session()
    s2.open(path)
    st2 = s2.project.study_by_id(st.id)
    rec2 = get_transfer(st2)
    assert isinstance(rec2, TransferRecord) and rec2 == rec
    assert st2.loads[0].params["magnitude"] == pytest.approx(F, rel=1e-9)
    assert "Static-hold" in st2.notes


def test_errors_are_clear(session, run_c):
    rb, gid, root, tip = left(session)
    pb = session.project.assembly.bodies[0]  # grounded palm
    with pytest.raises(TransferError, match="grounded"):
        make_static_hold_study(session.project, mbd(session, "C"), 0.05, pb.id, root, tip, result=run_c)
    with pytest.raises(TransferError, match="same face"):
        make_static_hold_study(session.project, mbd(session, "C"), 0.05, rb.id, root, root, result=run_c)
    with pytest.raises(TransferError, match="outside the simulated range"):
        make_static_hold_study(session.project, mbd(session, "C"), 5.0, rb.id, root, tip, result=run_c)


# ------------------------------------------------------------------------------------------------ FEA
def solve_tip(session, st, divisions):
    """Mesh + solve the transferred study; returns (mean u_x of the load-face nodes in mm, n_elements, session)."""
    session.set_active_study(st.id)
    st.mesh_settings.size_max = min(b, h, L) / divisions
    mesh = session.generate_mesh()
    with solver_lock("transfer benchmark"):
        out, issues = session.run_solver()
    assert out is not None and out.success, [str(i) for i in issues]
    tip = np.isclose(mesh.nodes[:, 1], L, atol=1e-6)
    return float(session.results.displacement[tip, 0].mean()), len(mesh.elements)


@pytest.mark.solver
def test_fea_tip_deflection_vs_cantilever_hand_calc(session, run_c):
    mat = session.project.material_for_body(left(session)[1], mbd(session, "C"))
    E = mat.youngs_modulus * 1e-6  # Pa -> MPa (N/mm^2)
    I = b * h ** 3 / 12  # bending in the thin direction (h = 4 mm along x), b = 10 mm width
    L_eff = L  # root face (y = 0) to the load-face centroid (y = L)
    hand = F * L_eff ** 3 / (3 * E * I)
    res = {}
    for div in (2, 4):  # elements across the thin direction: coarse, fine
        st, rec = transfer(session, run_c, name=f"hold div {div}", mesh_divisions=div)
        u, n = solve_tip(session, st, div)
        res[div] = (u, n)
    print("\nhand calc delta = %.6g mm" % hand)
    for div, (u, n) in res.items():
        print(f"mesh {div} elements across h: {n} elements, FE tip u_x = {u:.6g} mm, FE/hand = {abs(u) / hand:.4f}")
    u_fine = abs(res[4][0])
    # load is -x: the tip moves in -x
    assert res[4][0] < 0
    assert u_fine == pytest.approx(hand, rel=0.05)
    # convergence: refining changes the answer by less than 2 %
    assert abs(res[2][0] - res[4][0]) / u_fine < 0.02
