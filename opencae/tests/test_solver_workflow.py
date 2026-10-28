"""End-to-end solver tests with CalculiX (marked 'solver')."""
import threading

import numpy as np
import pytest

from opencae.app.session import Session
from opencae.geometry.references import find_planar_face, make_ref
from opencae.postprocessing.report import export_results, write_report
from opencae.postprocessing.theory import cantilever_comparison
from opencae.project import io as pio
from opencae.project.model import Constraint, Feature, Load
from opencae.solver.calculix.runner import run_blocking

pytestmark = pytest.mark.solver


def _axial_bar(session: Session, order=2, L=100.0, a=10.0, F=5000.0):
    f = session.add_feature(Feature("primitive", {"shape": "box", "dx": L, "dy": a, "dz": a}))
    b = session.analysis_body()
    session.project.assign_material(b.id, "mat_steel")
    x0 = find_planar_face(b, [-1, 0, 0], [0, 0, 0])
    y0 = find_planar_face(b, [0, -1, 0], [0, 0, 0])
    z0 = find_planar_face(b, [0, 0, -1], [0, 0, 0])
    tip = find_planar_face(b, [1, 0, 0], [L, 0, 0])
    session.project.constraints = [Constraint("symmetry", [make_ref(b, "face", x0)], name="sx"),
                                   Constraint("symmetry", [make_ref(b, "face", y0)], name="sy"),
                                   Constraint("symmetry", [make_ref(b, "face", z0)], name="sz")]
    session.project.loads = [Load("force", [make_ref(b, "face", tip)], {"magnitude": F, "direction": [1, 0, 0]},
                                  name="F")]
    session.project.mesh_settings.element_order = order
    session.project.mesh_settings.size_max = 5.0
    return b


@pytest.mark.parametrize("order", [1, 2])
def test_axial_bar_exact(session, order):
    L, a, F, E = 100.0, 10.0, 5000.0, 200e3
    _axial_bar(session, order, L, a, F)
    session.generate_mesh()
    out, issues = session.run_solver()
    assert out and out.success, (out.errors if out else [str(i) for i in issues])
    rs = session.results
    u_tip = rs.displacement[np.isclose(rs.mesh.nodes[:, 0], L), 0]
    assert u_tip == pytest.approx(F * L / (a * a * E), rel=2e-5)  # frd: 6 significant digits
    assert rs.stress[:, 0] == pytest.approx(F / a ** 2, rel=1e-4)
    assert np.abs(rs.stress[:, 1:]).max() < 1e-3 * F / a ** 2
    assert rs.ip_stress[:, 0] == pytest.approx(F / a ** 2, rel=1e-4)
    # lateral contraction: eps_yy = -nu * sigma / E
    assert rs.strain[:, 1] == pytest.approx(-0.3 * F / a ** 2 / E, rel=1e-3)


def test_tensor_shear_strain_convention(cantilever):
    """CalculiX writes tensor shear strain: eps_xy = sigma_xy / (2G)."""
    s = cantilever
    s.generate_mesh()
    out, _ = s.run_solver()
    assert out.success
    rs = s.results
    G = 200e3 / (2 * 1.3)
    i = np.argmax(np.abs(rs.stress[:, 5]))  # zx shear is significant in a z-loaded cantilever
    assert rs.strain[i, 5] == pytest.approx(rs.stress[i, 5] / (2 * G), rel=1e-3)


def test_cantilever_against_beam_theory_and_equilibrium(cantilever, tmp_path):
    s = cantilever
    s.project.mesh_settings.size_max = 4.0
    s.generate_mesh()
    out, _ = s.run_solver()
    assert out.success
    comp = cantilever_comparison(s)
    eb = comp.quantities[0]
    assert abs(eb["diff_pct"]) < 2.0  # C3D10, 3-D solid vs Euler-Bernoulli
    sm = s.results.summary()
    total = np.asarray(sm["reaction_totals"]["BC_ALL_SUPPORTS"])
    assert total == pytest.approx([0, 0, 1000.0], abs=1e-6)
    rep = write_report(s, tmp_path / "r.html")
    assert "Global equilibrium satisfied" in rep.read_text()
    for ext in ("vtu", "csv", "json"):
        assert export_results(s, tmp_path / f"r.{ext}").stat().st_size > 100


def test_gravity_and_pressure_equilibrium(session):
    f = session.add_feature(Feature("primitive", {"shape": "box", "dx": 50, "dy": 20, "dz": 10}))
    b = session.analysis_body()
    session.project.assign_material(b.id, "mat_al6061")
    bottom = find_planar_face(b, [0, 0, -1], [0, 0, 0])
    top = find_planar_face(b, [0, 0, 1], [0, 0, 10])
    session.project.constraints = [Constraint("fixed", [make_ref(b, "face", bottom)], name="base")]
    session.project.loads = [Load("pressure", [make_ref(b, "face", top)], {"magnitude": 1.5}, name="p"),
                             Load("gravity", [], {"magnitude": 9806.65, "direction": [0, 0, -1]}, name="g")]
    session.project.mesh_settings.size_max = 4
    session.generate_mesh()
    out, issues = session.run_solver()
    assert out.success, [str(i) for i in issues]
    rho = 2700e-12
    expected = 1.5 * 50 * 20 + rho * 50 * 20 * 10 * 9806.65
    from opencae.postprocessing.results import equilibrium_check
    summ = session.project.results.summary
    assert summ["applied_force_total"][2] == pytest.approx(-expected, rel=1e-12)  # exact consistent loads
    eq = equilibrium_check(summ)
    assert eq["ok"], eq
    # part of the gravity load acts directly on the fixed base nodes and is not in the reaction
    assert eq["on_constrained"][2] < 0
    rt = list(summ["reaction_totals"].values())[0]
    assert rt[2] == pytest.approx(expected + eq["on_constrained"][2], rel=1e-6)


def test_under_constrained_model_is_blocked(cantilever):
    s = cantilever
    s.generate_mesh()
    b = s.analysis_body()
    fix = s.project.constraints[0].targets[0]
    s.project.constraints = [Constraint("displacement", [fix], {"ux": 0.0, "uy": None, "uz": None}, name="x only")]
    out, issues = s.run_solver()
    assert out is None
    assert any("insufficiently constrained" in i.message for i in issues)


def test_cancel_running_solver(cantilever):
    s = cantilever
    s.project.mesh_settings.size_max = 2.0
    s.generate_mesh()
    job_dir, jobname, issues = s.prepare_solve()
    flag = {"stop": False}
    threading.Timer(0.3, lambda: flag.__setitem__("stop", True)).start()
    out = run_blocking(s.backend.install, job_dir, jobname, 1, cancel=lambda: flag["stop"])
    assert out.cancelled and not out.success


def test_save_reopen_equivalence(cantilever, tmp_path):
    """Benchmark E (serialization): reopened project has equivalent inputs and results."""
    s = cantilever
    s.generate_mesh()
    assert s.run_solver()[0].success
    path = s.save(tmp_path / "beam.ocae")
    s2 = Session()
    s2.open(path)
    assert pio.project_to_dict(s2.project) == pio.project_to_dict(s.project)
    assert s2.project.results_are_current() and s2.project.mesh_is_current()
    assert np.array_equal(s2.mesh.elements, s.mesh.elements)
    assert np.allclose(s2.results.displacement, s.results.displacement)
    assert s2.analysis_body().volume == pytest.approx(s.analysis_body().volume, rel=1e-12)
    # re-solving the reopened model reproduces the results
    assert s2.run_solver()[0].success
    assert np.allclose(s2.results.displacement, s.results.displacement, rtol=0, atol=1e-9)


def test_unit_systems_give_identical_physics(tmp_path):
    """Benchmark E (units): the same physical model solved in mm-N-s and m-N-s.

    The identical mesh (scaled by 1e-3) is used in both systems so the comparison
    isolates unit handling from mesher differences (Gmsh tolerances are absolute,
    so meshing the metre-scaled CAD gives a slightly different mesh)."""
    import copy
    sessions = {}
    for us, scale in (("mm-N-s", 1.0), ("m-N-s", 1e-3)):
        s = Session()
        s.new("u", us)
        s.create_cantilever(200 * scale, 20 * scale, 20 * scale, 1000.0)
        s.project.mesh_settings.size_max = 6 * scale
        sessions[us] = s
    mm, si = sessions["mm-N-s"], sessions["m-N-s"]
    mesh_mm = mm.generate_mesh()
    si.mesh_job()  # validates the body
    mesh_si = copy.deepcopy(mesh_mm)
    mesh_si.nodes = mesh_mm.nodes * 1e-3
    mesh_si.body_id = si.analysis_body().id
    mesh_si.body_ids = [mesh_si.body_id]
    si.set_mesh(mesh_si)
    for s in (mm, si):
        out, issues = s.run_solver()
        assert out and out.success, [str(i) for i in issues]
    assert si.results.displacement == pytest.approx(mm.results.displacement * 1e-3, rel=1e-4, abs=1e-12)
    vm_mm, vm_si = mm.results.quantity("S_vm"), si.results.quantity("S_vm")
    assert vm_si == pytest.approx(vm_mm * 1e6, rel=1e-4, abs=1e-4 * vm_mm.max() * 1e6)
    # independently meshed models agree on the (smooth) tip deflection
    s2 = Session()
    s2.new("u2", "m-N-s")
    s2.create_cantilever(0.2, 0.02, 0.02, 1000.0)
    s2.project.mesh_settings.size_max = 0.006
    s2.generate_mesh()
    assert s2.run_solver()[0].success
    assert s2.results.summary()["max_abs_uz"] == pytest.approx(mm.results.summary()["max_abs_uz"] * 1e-3, rel=2e-3)


def test_workflow_states_through_solve(cantilever):
    s = cantilever
    s.generate_mesh()
    st = {k: v for k, v, _ in s.workflow_status()}
    assert st["Mesh"] == "completed" and st["Validation"] == "ready" and st["Solve"] == "ready"
    assert s.run_solver()[0].success
    st = {k: v for k, v, _ in s.workflow_status()}
    assert st["Solve"] == "completed" and st["Results"] == "completed"
    s.project.loads[0].params["magnitude"] = 2000.0  # model changed after solving
    st = {k: v for k, v, _ in s.workflow_status()}
    assert st["Solve"] == "invalid" and st["Results"] == "invalid"


@pytest.mark.parametrize("order,formulation,h,tol", [(1, "enhanced", 5, 0.5), (2, "enhanced", 5, 0.5),
                                                      (2, "standard", 5, 0.5)])
def test_hex_cantilever_against_theory(cantilever, order, formulation, h, tol):
    s = cantilever
    ms = s.project.mesh_settings
    ms.element_shape, ms.element_order, ms.hex_formulation, ms.size_max = "hex", order, formulation, h
    s.generate_mesh()
    out, issues = s.run_solver()
    assert out and out.success, [str(i) for i in issues]
    deck = (s.data_dir / s.project.results.directory / "job.inp").read_text()
    assert f"TYPE={ms.solver_element(s.mesh.element_type)}," in deck
    comp = cantilever_comparison(s)
    assert abs(comp.quantities[0]["diff_pct"]) < tol
    assert abs(comp.quantities[2]["diff_pct"]) < 1.0  # mid-span bending stress
