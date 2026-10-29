"""Heat conduction, modal and thermal-stress analyses against closed-form solutions."""
import numpy as np
import pytest

from opencae.app.session import Session
from opencae.geometry.references import find_planar_face, make_ref
from opencae.physics.thermal import heat_balance
from opencae.project.model import Constraint, Feature, Load

pytestmark = pytest.mark.solver
SIGMA = 5.670374419e-8
K_STEEL, RHO_STEEL, C_STEEL, ALPHA_STEEL, E_STEEL = 50.0, 7850.0, 470.0, 12e-6, 200e3


def _bar(session, L=100.0, a=10.0, h=5.0, analysis="thermal_steady"):
    session.add_feature(Feature("primitive", {"shape": "box", "dx": L, "dy": a, "dz": a}))
    b = session.analysis_body()
    session.project.assign_material(b.id, "mat_steel")
    session.project.analysis.analysis_type = analysis
    session.project.mesh_settings.size_max = h
    f = lambda n, p: make_ref(b, "face", find_planar_face(b, n, p))  # noqa: E731
    return b, {"x0": f([-1, 0, 0], [0, 0, 0]), "xL": f([1, 0, 0], [L, 0, 0]), "y0": f([0, -1, 0], [0, 0, 0]),
               "z0": f([0, 0, -1], [0, 0, 0])}


def _solve(s):
    out, issues = s.run_solver()
    assert out and out.success, (out.errors if out else [str(i) for i in issues])
    return s.results


# ------------------------------------------------------------------ steady conduction
def test_slab_prescribed_temperature_and_flux(session):
    s = session
    _, f = _bar(s)
    s.project.constraints = [Constraint("temperature", [f["x0"]], {"value": 20.0}, name="cold")]
    s.project.loads = [Load("heat_flux", [f["xL"]], {"magnitude": 1e4}, name="q")]  # W/m^2 into the body
    s.generate_mesh()
    rs = _solve(s)
    x = s.mesh.nodes[:, 0] * 1e-3
    exact = 20.0 + 1e4 * x / K_STEEL  # T = T0 + q x / k
    assert rs.quantity("T") == pytest.approx(exact, abs=1e-3)
    assert rs.quantity("HF_x") == pytest.approx(-1e4, rel=1e-4)  # heat flows towards x = 0
    assert rs.quantity("gradT_mag") == pytest.approx(1e4 / K_STEEL, rel=1e-4)
    # energy balance: heat leaving through the fixed-temperature face equals the input (1 W)
    sm = s.project.results.summary
    assert sm["heat_input_W"] == pytest.approx(1.0, rel=1e-12)
    assert sm["heat_at_fixed_temperatures_W"]["T_ALL_FIXED"] == pytest.approx(-1.0, rel=1e-4)
    assert heat_balance(sm)["ok"]


def test_slab_convection_and_radiation(session):
    s = session
    _, f = _bar(s)
    s.generate_mesh()
    x = s.mesh.nodes[:, 0]
    s.project.loads = [Load("heat_flux", [f["xL"]], {"magnitude": 1e4}, name="q"),
                       Load("convection", [f["x0"]], {"film": 100.0, "ambient": 20.0}, name="h")]
    T = _solve(s).quantity("T")
    T0 = 20.0 + 1e4 / 100.0  # q = h (T0 - Tinf)
    assert T == pytest.approx(T0 + 1e4 * x * 1e-3 / K_STEEL, abs=2e-3)
    s.project.loads[1] = Load("radiation", [f["x0"]], {"emissivity": 0.8, "ambient": 20.0}, name="r")
    T = _solve(s).quantity("T")
    T0 = (1e4 / (0.8 * SIGMA) + 293.15 ** 4) ** 0.25 - 273.15  # q = eps sigma (T0^4 - Tinf^4)
    assert T[x < 1e-9] == pytest.approx(T0, abs=2e-2)
    assert T[x > 99.999] == pytest.approx(T0 + 20.0, abs=2e-2)


def test_volumetric_heat_generation_parabolic_profile(session):
    s = session
    _, f = _bar(s, h=4.0)
    q = 1e6  # W/m^3
    s.project.constraints = [Constraint("temperature", [f["x0"], f["xL"]], {"value": 0.0}, name="ends")]
    s.project.loads = [Load("heat_generation", [], {"magnitude": q}, name="qv")]
    s.generate_mesh()
    rs = _solve(s)
    x, L = s.mesh.nodes[:, 0] * 1e-3, 0.1
    exact = q * x * (L - x) / (2 * K_STEEL)  # T_max = q L^2 / (8 k) = 25 K
    assert rs.quantity("T") == pytest.approx(exact, abs=1e-3 * exact.max())  # quadratic: exact for C3D10
    sm = s.project.results.summary
    assert sm["heat_input_W"] == pytest.approx(q * 1e-5, rel=1e-6)  # 10 W
    # part of the generated heat is assigned directly to the fixed nodes and not reported by the solver
    assert 0 < sm["heat_input_on_fixed_W"] < 0.05 * q * 1e-5
    assert sm["heat_at_fixed_temperatures_W"]["T_ALL_FIXED"] == pytest.approx(
        -(q * 1e-5 - sm["heat_input_on_fixed_W"]), rel=1e-4)
    assert heat_balance(sm)["ok"]


def test_thermal_validation_errors(session):
    s = session
    _, f = _bar(s)
    s.project.loads = [Load("heat_flux", [f["xL"]], {"magnitude": 1e4}, name="q")]
    s.generate_mesh()
    out, issues = s.run_solver()
    assert out is None
    assert any("undetermined" in i.message for i in issues)
    # structural supports are reported as ignored, not silently used
    s.project.constraints = [Constraint("fixed", [f["x0"]], name="fix"),
                             Constraint("temperature", [f["x0"]], {"value": 0.0}, name="T0")]
    issues, _ = s.validate()
    assert any("ignored in a thermal analysis" in i.message for i in issues)


# ------------------------------------------------------------------ transient conduction
def test_transient_slab_against_series_solution(session):
    """Insulated bar, initial 20 °C, x = 0 suddenly held at 120 °C (Fourier series solution)."""
    s = session
    _, f = _bar(s, h=5.0, analysis="thermal_transient")
    a = s.project.analysis
    a.initial_temperature, a.time_total, a.time_step = 20.0, 1000.0, 10.0
    s.project.constraints = [Constraint("temperature", [f["x0"]], {"value": 120.0}, name="hot")]
    s.generate_mesh()
    rs = _solve(s)
    L, alpha = 0.1, K_STEEL / (RHO_STEEL * C_STEEL)
    x = s.mesh.nodes[:, 0] * 1e-3

    def exact(t):
        S = 0.0
        for n in range(200):
            m = (2 * n + 1) * np.pi / (2 * L)
            S = S + 4 / ((2 * n + 1) * np.pi) * np.sin(m * x) * np.exp(-m * m * alpha * t)
        return 120.0 - 100.0 * S

    assert len(rs.times) > 10 and rs.times[-1] == pytest.approx(1000.0)
    for k in (len(rs.times) // 2, len(rs.times) - 1):
        err = np.abs(rs.temperature[k] - exact(rs.times[k])).max()
        assert err < 1.0, (rs.times[k], err)  # < 1 % of the 100 K step (backward Euler, dt = 10 s)
    t, T = rs.history(int(np.argmax(x)))
    assert np.all(np.diff(T) >= -1e-6)  # far end heats up monotonically


# ------------------------------------------------------------------ modal
def test_cantilever_natural_frequencies(cantilever):
    s = cantilever
    s.project.analysis.analysis_type = "modal"
    s.project.analysis.n_modes = 6
    s.project.mesh_settings.size_max = 5.0
    s.generate_mesh()
    rs = _solve(s)
    E, rho, a, L = 200e9, 7850.0, 0.02, 0.2
    I, A = a ** 4 / 12, a * a
    f1 = 1.875104 ** 2 / (2 * np.pi) * np.sqrt(E * I / (rho * A * L ** 4))  # Euler-Bernoulli
    f_axial = np.sqrt(E / rho) / (4 * L)
    f_torsion = np.sqrt(E / 2.6 * 0.1406 * a ** 4 / (rho * 2 * I)) / (4 * L)  # square section, J = 0.1406 a^4
    fr = rs.frequencies
    assert fr[0] == pytest.approx(f1, rel=1e-2) and fr[1] == pytest.approx(f1, rel=1e-2)  # two bending planes
    assert np.min(np.abs(fr / f_torsion - 1)) < 1e-2
    assert np.min(np.abs(fr / f_axial - 1)) < 1e-2
    frac = np.asarray(s.project.results.summary["effective_mass_fraction"])
    assert frac[0, :3].sum() + frac[1, :3].sum() == pytest.approx(2 * 0.613, abs=0.02)  # theory: 61.3 % per plane
    rs.set_frame(4)
    assert rs.quantity("U_mag").max() > 0
    assert any("ignored" in str(i) for i in s.validate()[0])  # the tip load does not enter a modal analysis


def test_modal_free_free_rigid_modes(session):
    s = session
    _bar(s, h=6.0, analysis="modal")
    s.project.analysis.n_modes = 8
    s.generate_mesh()
    rs = _solve(s)
    assert np.all(np.abs(rs.frequencies[:6]) < 1.0)  # six rigid-body modes
    assert rs.frequencies[6] > 1000.0


# ------------------------------------------------------------------ thermal stress
def test_thermal_expansion_free_and_constrained(session):
    s = session
    _, f = _bar(s, analysis="thermal_stress")
    s.project.analysis.reference_temperature = 20.0
    s.project.constraints = [Constraint("symmetry", [f[k]], name=k) for k in ("x0", "y0", "z0")]
    s.project.loads = [Load("uniform_temperature", [], {"value": 120.0}, name="T")]
    s.generate_mesh()
    rs = _solve(s)
    x = s.mesh.nodes[:, 0]
    assert rs.displacement[x > 99.999, 0] == pytest.approx(ALPHA_STEEL * 100.0 * 100.0, rel=1e-5)  # free expansion
    assert rs.quantity("S_vm").max() < 1e-6
    s.project.constraints.append(Constraint("symmetry", [f["xL"]], name="xL"))
    rs = _solve(s)
    assert rs.stress[:, 0] == pytest.approx(-E_STEEL * ALPHA_STEEL * 100.0, rel=1e-4)  # -240 MPa
    assert np.abs(rs.stress[:, 1:]).max() < 1e-3


def test_thermal_stress_from_thermal_study_on_another_mesh(session):
    """Temperatures of a tet-meshed thermal study drive a hex-meshed stress study.

    The linear field T = 20 + x is transferred exactly (linear interpolation). The free bar's
    mean elongation is alpha * integral(T - Tref) dx; the end face warps slightly because the
    exact solution has u_x = alpha g (x^2 - y^2 - z^2) / 2, which the x = 0 symmetry plane
    restrains, so only small stresses arise."""
    s = session
    b, f = _bar(s, analysis="thermal_steady")
    th = s.project.study
    s.project.constraints = [Constraint("temperature", [f["x0"]], {"value": 20.0}, name="c"),
                             Constraint("temperature", [f["xL"]], {"value": 120.0}, name="h")]
    s.generate_mesh()
    _solve(s)
    st = s.new_study("stress", "thermal_stress")
    st.analysis.temperature_source_study = th.id
    st.analysis.reference_temperature = 20.0
    st.mesh_settings.element_shape, st.mesh_settings.size_max = "hex", 10.0
    s.project.constraints = [Constraint("symmetry", [f[k]], name=k) for k in ("x0", "y0", "z0")]
    s.generate_mesh()
    issues, rm = s.validate()
    assert any("interpolated" in i.message for i in issues)
    x = s.mesh.nodes[:, 0]
    assert rm.node_temperature == pytest.approx(20.0 + x, abs=1e-3)  # source field T = 20 + x (to 6 digits)
    rs = _solve(s)
    # the restrained end-face warping changes the (unweighted nodal) mean slightly
    assert rs.displacement[x > 99.999, 0].mean() == pytest.approx(ALPHA_STEEL * 50.0 * 100.0, rel=5e-3)
    # the end restraint stresses decay within about one section depth (Saint-Venant); the free
    # length is stress-free, and the support reactions are self-equilibrated
    assert rs.quantity("S_vm")[x > 20].max() < 5e-3 * E_STEEL * ALPHA_STEEL * 100
    assert np.abs(rs.reaction_totals["BC_ALL_SUPPORTS"]).max() < 1e-6
    # changing the thermal study invalidates the stress study's results
    s.set_active_study(th.id)
    s.project.constraints[1].params["value"] = 200.0
    s.set_active_study(st.id)
    assert not s.project.results_are_current()
    assert any("out of date" in i.message for i in s.validate()[0])


def test_thermal_and_modal_results_survive_save_and_reopen(session, tmp_path):
    s = session
    _, f = _bar(s)
    s.project.constraints = [Constraint("temperature", [f["x0"]], {"value": 20.0}, name="cold")]
    s.project.loads = [Load("heat_flux", [f["xL"]], {"magnitude": 1e4}, name="q")]
    s.generate_mesh()
    rs = _solve(s)
    md = s.new_study("modes", "modal")
    s.project.constraints = [Constraint("fixed", [f["x0"]], name="fix")]
    s.generate_mesh()
    rm = _solve(s)
    path = s.save(tmp_path / "tm.ocae")
    s2 = Session()
    s2.open(path)
    assert s2.results.kind == "modal" and np.allclose(s2.results.frequencies, rm.frequencies)
    s2.set_active_study(s2.project.studies[0].id)
    assert s2.results.kind == "thermal" and np.allclose(s2.results.quantity("T"), rs.quantity("T"))
    assert s2.project.results_are_current()


def test_reports_and_exports_for_thermal_and_modal(session, tmp_path):
    from opencae.postprocessing.report import export_results, write_report
    s = session
    _, f = _bar(s)
    s.project.constraints = [Constraint("temperature", [f["x0"]], {"value": 20.0}, name="cold")]
    s.project.loads = [Load("heat_flux", [f["xL"]], {"magnitude": 1e4}, name="q")]
    s.study.notes = "Slab check.\n\nSecond paragraph."
    s.generate_mesh()
    _solve(s)
    txt = write_report(s, tmp_path / "t.html", notes="Reviewed by me.").read_text()
    for needle in ("Steady-State Thermal", "Fourier", "Max temperature", "Energy balance: satisfied",
                   "W/m²", "Slab check.", "Second paragraph.", "Reviewed by me.", "insulated"):
        assert needle in txt, needle
    import meshio
    m = meshio.read(export_results(s, tmp_path / "t.vtu"))
    assert "T" in m.point_data and np.isclose(m.point_data["T"].max(), 40.0, atol=1e-3)
    s.new_study("modes", "modal")
    s.project.constraints = [Constraint("fixed", [f["x0"]], name="fix")]
    s.project.analysis.n_modes = 3
    s.generate_mesh()
    _solve(s)
    txt = write_report(s, tmp_path / "m.html").read_text()
    for needle in ("Modal", "Mode 1", "Hz", "Effective modal mass fractions", "Other studies in this project"):
        assert needle in txt, needle
    m = meshio.read(export_results(s, tmp_path / "m.vtu"))
    assert sum(k.startswith("mode_") for k in m.point_data) == 3
