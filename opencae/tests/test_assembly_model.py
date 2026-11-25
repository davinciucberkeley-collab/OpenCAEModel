"""Assembly data model, profiles, mass properties, schema v3 and old-project compatibility."""
import json
import shutil
from pathlib import Path

import numpy as np
import pytest

from opencae.app.session import Session
from opencae.assembly import (GROUND, AppliedLoad, Assembly, Component, Coupling, Driver, Joint, MassProps,
                              MBDSettings, Profile, RigidBody, ThrustModel, get_mbd, set_mbd)
from opencae.assembly.massprops import compute_massprops, missing_density, update_body_masses
from opencae.project import io as pio
from opencae.project.model import Feature, Material

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "projects"


def sample_assembly() -> Assembly:
    c1, c2 = Component(name="palm", body_id="geo1", source="box"), Component(name="finger", body_id="geo2")
    b1 = RigidBody(name="Palm", component_ids=[c1.id], grounded=True)
    b2 = RigidBody(name="Finger", component_ids=[c2.id],
                   mass=MassProps(0.01, [1, 2, 3], np.diag([1e-6, 2e-6, 3e-6]).tolist(), "cad", 2700.0),
                   manual=MassProps(0.02, [1, 2, 3], np.eye(3).tolist(), "manual"))
    j = Joint(name="hinge", kind="revolute", parent=b1.id, child=b2.id, origin=[1, 0, 0], axis=[0, 0, 1],
              reversed=True, limits=[-0.1, 0.5], damping=1e-4)
    j2 = Joint(name="hinge2", kind="revolute", parent=b1.id, child=b2.id, origin=[-1, 0, 0])
    cpl = Coupling(name="gear", leader=j.id, follower=j2.id, ratio=-1.0, note="ideal")
    return Assembly([c1, c2], [b1, b2], [j, j2], [cpl], [ThrustModel(rotor_joint=j.id, kt=1e-5)])


def test_model_roundtrip_dict():
    a = sample_assembly()
    d = json.loads(json.dumps(a.to_dict()))
    b = Assembly.from_dict(d)
    assert b.to_dict() == a.to_dict()
    assert isinstance(b.bodies[1].manual, MassProps) and b.bodies[1].effective_mass().mass_kg == 0.02
    assert b.bodies[0].effective_mass() is None
    assert b.joint(a.joints[0].id).reversed and b.body_of_component(a.components[1].id).name == "Finger"
    assert Assembly.from_dict(None).is_empty()


def test_mbd_settings_roundtrip_and_study_storage():
    a = sample_assembly()
    s = MBDSettings(gravity=[0, 0, 0], duration_s=2.0, dt_s=1e-4, output_dt_s=1e-2,
                    drivers=[Driver(name="m", joint=a.joints[0].id, mode="position",
                                    profile=Profile("ramp", t0=0, t1=1, v0=0, v1=0.3))],
                    loads=[AppliedLoad(name="F", body=a.bodies[1].id, force_N=[1, 0, 0],
                                       profile=Profile("table", table=[[0, 0], [1, 1]]))])
    t = MBDSettings.from_dict(json.loads(json.dumps(s.to_dict())))
    assert t.to_dict() == s.to_dict() and t.drivers[0].prescribed and t.loads[0].profile.kind == "table"

    class St:
        mbd = {}
    st = St()
    assert get_mbd(st).dt_s == MBDSettings().dt_s  # defaults for a study without settings
    set_mbd(st, s)
    assert get_mbd(st).to_dict() == s.to_dict()


def test_joint_frame_is_deterministic_and_right_handed():
    for axis, rev in (([0, 0, 1], False), ([0, 0, 1], True), ([1, 2, 3], False), ([0, 1, 0], True)):
        j = Joint(kind="revolute", axis=axis, reversed=rev)
        _, R = j.frame()
        assert np.allclose(R.T @ R, np.eye(3), atol=1e-14) and np.linalg.det(R) == pytest.approx(1.0)
        z = np.asarray(axis, float) / np.linalg.norm(axis)
        assert np.allclose(R[:, 2], -z if rev else z)
        assert Joint(kind="revolute", axis=axis, reversed=rev).x_axis == j.x_axis  # same every time
    j = Joint(kind="revolute", axis=[0, 0, 1])
    j.set_axis([1, 0, 0])
    assert np.dot(j.x_axis, [1, 0, 0]) == pytest.approx(0.0)


def test_profiles_value_slope_integral():
    r = Profile("ramp", t0=1.0, t1=3.0, v0=2.0, v1=6.0)
    assert [r.value_at(t) for t in (0, 1, 2, 3, 9)] == [2, 2, 4, 6, 6]
    assert r.slope(0.5) == 0 and r.slope(1.0) == 2.0 and r.slope(2.0) == 2.0 and r.slope(3.0) == 0.0
    assert r.slope(3.0, "left") == 2.0 and r.slope(1.0, "left") == 0.0
    # integral: 2*1 + (2+6)/2*2 + 6*(5-3)
    assert r.integral(5.0) == pytest.approx(2 + 8 + 12)
    assert r.integral(2.0) == pytest.approx(2 + 3.0)
    tbl = Profile("table", table=[[0, 0], [1, 10], [2, 10]])
    assert tbl.value_at(0.5) == 5 and tbl.value_at(7) == 10 and tbl.integral(2.0) == pytest.approx(5 + 10)
    c = Profile("constant", value=3.0)
    assert c.integral(2.5) == pytest.approx(7.5) and c.slope(1.0) == 0.0
    assert Profile("ramp", t0=1, t1=1).problems() and Profile("table", table=[[1, 0], [0, 0]]).problems()


def test_assembly_validate_finds_graph_problems():
    a = sample_assembly()
    assert any("closed kinematic loops" in p for p in a.validate())  # finger has two parent joints
    a.joints.pop()  # drop hinge2 and its coupling target
    a.couplings.clear()
    assert a.validate() == []
    free = RigidBody(name="Floating")
    a.bodies.append(free)
    assert any("not connected" in p and "Floating" in p for p in a.validate())
    a.bodies.pop()
    a.joints.append(Joint(name="loopA", parent=a.bodies[1].id, child=a.bodies[0].id))
    probs = a.validate()
    assert any("grounded" in p for p in probs)


def test_assembly_validate_cycle():
    b1, b2, b3 = RigidBody(name="g", grounded=True), RigidBody(name="a"), RigidBody(name="b")
    a = Assembly(bodies=[b1, b2, b3], joints=[Joint(name="j1", parent=b3.id, child=b2.id),
                                              Joint(name="j2", parent=b2.id, child=b3.id)])
    assert any("cycle" in p for p in a.validate())


# --------------------------------------------------------------------------- project IO (schema v3)
def test_schema_is_v3_and_has_migration_from_v2():
    assert pio.SCHEMA_VERSION == 3 and set(pio.MIGRATIONS) == {1, 2}
    doc = pio.project_to_dict(pio.m.Project(name="x"))
    doc["schema_version"] = 2
    doc["project"].pop("assembly")
    for st in doc["project"]["studies"]:
        st.pop("mbd", None)
    q = pio.project_from_dict(doc)
    assert q.assembly.is_empty() and q.study.mbd == {}


def test_project_save_load_same_assembly_and_studies(tmp_path):
    s = Session()
    s.new("asm", "mm-N-s")
    s.project.assembly = sample_assembly()
    st = s.new_study("Motion", "mbd")
    set_mbd(st, MBDSettings(drivers=[Driver(name="m", joint=s.project.assembly.joints[0].id, mode="velocity",
                                            profile=Profile("constant", value=1.5))]))
    path = s.save(tmp_path / "asm.ocae")
    doc = json.loads(path.read_text())
    assert doc["schema_version"] == 3 and doc["project"]["assembly"]["bodies"]
    r = Session()
    r.open(path)
    assert r.project.assembly.to_dict() == s.project.assembly.to_dict()
    assert pio.project_to_dict(r.project) == pio.project_to_dict(s.project)
    mst = [x for x in r.project.studies if x.analysis.analysis_type == "mbd"][0]
    assert get_mbd(mst).drivers[0].profile.value == 1.5
    # the structural study is untouched by the new fields
    assert r.project.studies[0].mbd == {} and r.project.studies[0].analysis.analysis_type == "linear_static"


def test_old_project_loads_with_empty_assembly(tmp_path):
    src = EXAMPLES / "cantilever_beam.ocae"
    old = json.loads(src.read_text())
    assert old["schema_version"] in (1, 2)
    s = Session()
    s.open(src)
    assert s.project.assembly.is_empty() and len(s.project.studies) == 1
    assert s.project.study.analysis.analysis_type == "linear_static" and s.project.study.mbd == {}
    assert s.project.results_are_current() and s.results is not None  # stored results still valid
    dst = s.save(tmp_path / "old.ocae")
    assert json.loads(dst.read_text())["schema_version"] == 3


@pytest.mark.solver
def test_old_project_still_solves_structurally(tmp_path):
    for p in (EXAMPLES / "cantilever_beam.ocae", EXAMPLES / "cantilever_beam_data"):
        (shutil.copytree if p.is_dir() else shutil.copy)(p, tmp_path / p.name)
    s = Session()
    s.open(tmp_path / "cantilever_beam.ocae")
    s.project.mesh_settings.size_max = 8.0  # coarser than the stored mesh: forces a fresh mesh and solve
    s.generate_mesh()
    out, issues = s.run_solver()
    assert out.success, [str(i) for i in issues]
    # 200x20x20 mm steel cantilever, 1000 N: Euler-Bernoulli 1.000 mm (shear and mesh add a little)
    assert s.results.summary()["max_displacement"] == pytest.approx(1.0, rel=0.05)


# --------------------------------------------------------------------------- mass properties
def box_session(dx, dy, dz, pos=(0, 0, 0), material="mat_al6061"):
    s = Session()
    f = s.add_feature(Feature("primitive", {"shape": "box", "dx": dx, "dy": dy, "dz": dz,
                                            "position": list(pos)}), "Box")
    if material:
        s.project.assign_material(f.id, material)
    return s, f


def test_massprops_box_matches_closed_form():
    """Box a x b x c mm, offset from the origin: m = rho V, COM at the centre,
    Ixx = m (b^2 + c^2) / 12 about the COM (and so on)."""
    a, b, c, rho = 60.0, 10.0, 4.0, 2700.0
    s, f = box_session(a, b, c, pos=(100, 50, -7))
    mp = compute_massprops(s.project, [f.id], rho, bodies=s.bodies)
    m = rho * a * b * c * 1e-9
    assert mp.mass_kg == pytest.approx(m, rel=1e-6)
    assert mp.com == pytest.approx([130.0, 55.0, -5.0], rel=1e-9)
    I = mp.inertia_matrix()
    exact = np.diag([m * (b ** 2 + c ** 2) / 12, m * (a ** 2 + c ** 2) / 12, m * (a ** 2 + b ** 2) / 12]) * 1e-6
    assert np.allclose(I, exact, rtol=1e-6, atol=0) and abs(I[0, 1]) < 1e-15 * I[0, 0]
    assert mp.source == "cad" and mp.density_kg_m3 == rho


def test_massprops_uses_material_density_and_reports_missing():
    s, f = box_session(10, 10, 10)
    mp = compute_massprops(s.project, [f.id], bodies=s.bodies)  # mat_al6061 assigned
    assert mp.mass_kg == pytest.approx(s.project.material_by_id("mat_al6061").density * 1e-6, rel=1e-6)
    s2, f2 = box_session(10, 10, 10, material=None)
    assert compute_massprops(s2.project, [f2.id], bodies=s2.bodies) is None
    assert "no material assigned" in missing_density(s2.project, [f2.id])[0]
    s2.project.materials.append(Material("nodens", 1e9, 0.3, None, id="nd", is_example=False))
    s2.project.assign_material(f2.id, "nd")
    assert "has no density" in missing_density(s2.project, [f2.id])[0]


def test_massprops_two_bodies_parallel_axis_and_placement():
    """Two equal boxes 10 mm apart on x: parallel-axis theorem about the common COM; a placement
    translation moves the COM and leaves the inertia unchanged."""
    rho = 1000.0
    s, f = box_session(10, 10, 10)
    g = s.add_feature(Feature("primitive", {"shape": "box", "dx": 10, "dy": 10, "dz": 10, "position": [20, 0, 0]}))
    m1 = rho * 1e-6
    mp = compute_massprops(s.project, [f.id, g.id], rho, bodies=s.bodies)
    assert mp.mass_kg == pytest.approx(2 * m1, rel=1e-9) and mp.com == pytest.approx([15, 5, 5])
    I0 = m1 * (100 + 100) / 12 * 1e-6
    assert mp.inertia_matrix()[0, 0] == pytest.approx(2 * I0, rel=1e-9)
    # axes y, z: own inertia + m d^2 with d = 10 mm each
    assert mp.inertia_matrix()[2, 2] == pytest.approx(2 * (I0 + m1 * (10e-3) ** 2), rel=1e-9)
    T = np.eye(4)
    T[:3, 3] = [5, 0, 0]
    mp2 = compute_massprops(s.project, [f.id], rho, bodies=s.bodies, placements={f.id: T})
    assert mp2.com == pytest.approx([10, 5, 5]) and mp2.inertia_matrix()[0, 0] == pytest.approx(I0, rel=1e-9)


def test_update_body_masses_fills_rigid_bodies():
    s, f = box_session(60, 10, 4)
    c = Component(name="finger", body_id=f.id)
    rb = RigidBody(name="Finger", component_ids=[c.id])
    s.project.assembly = Assembly([c], [rb], [], [], [])
    assert update_body_masses(s.project, bodies=s.bodies) == []
    assert rb.mass.mass_kg == pytest.approx(2700.0 * 60e-3 * 10e-3 * 4e-3, rel=1e-6)
    s.project.assign_material(f.id, "mat_al6061")
    s.project.study.assignments.clear()
    assert "no material assigned" in update_body_masses(s.project, bodies=s.bodies)[0]
