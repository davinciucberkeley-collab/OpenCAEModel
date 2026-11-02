"""Buckling, moment loads and large-deflection (NLGEOM) statics against closed-form solutions."""
import numpy as np
import pytest

from opencae.project.model import Constraint, Load
from opencae.tests.test_thermal_modal import _bar, _solve

pytestmark = pytest.mark.solver
E = 200e3  # MPa (steel in mm-N-s)


def test_euler_column_buckling(session):
    """Fixed-free column: P_cr = pi^2 E I / (4 L^2)."""
    s = session
    _, f = _bar(s, L=200.0, a=10.0, h=4.0, analysis="buckling")
    s.project.analysis.n_modes = 3
    s.project.constraints = [Constraint("fixed", [f["x0"]], name="base")]
    s.project.loads = [Load("force", [f["xL"]], {"magnitude": 1000.0, "direction": [-1, 0, 0]}, name="P")]
    s.generate_mesh()
    rs = _solve(s)
    I = 10.0 ** 4 / 12
    p_cr = np.pi ** 2 * E * I / (4 * 200.0 ** 2)
    assert rs.kind == "buckling"
    assert rs.load_factors[0] == pytest.approx(p_cr / 1000.0, rel=5e-3)  # two equal modes (square section)
    assert rs.load_factors[1] == pytest.approx(p_cr / 1000.0, rel=5e-3)
    assert rs.load_factors[2] == pytest.approx(9 * p_cr / 1000.0, rel=3e-2)  # second Euler mode (n = 3)
    assert s.project.results.summary["critical_load_factor"] == pytest.approx(rs.load_factors[0])
    # reversing the load (tension) gives no positive buckling factor for the first mode
    s.project.loads[0].params["direction"] = [1, 0, 0]
    rs2 = _solve(s)
    assert rs2.load_factors[0] < 0


def test_end_moment_gives_pure_bending(session):
    """Cantilever with an end moment M: tip deflection M L^2 / (2 E I), uniform bending stress M c / I."""
    s = session
    L, a, M = 200.0, 20.0, 1e5
    _, f = _bar(s, L=L, a=a, h=5.0, analysis="linear_static")
    s.project.constraints = [Constraint("fixed", [f["x0"]], name="root")]
    s.project.loads = [Load("moment", [f["xL"]], {"magnitude": M, "direction": [0, 1, 0]}, name="M")]
    s.generate_mesh()
    rs = _solve(s)
    I = a ** 4 / 12
    x = s.mesh.nodes[:, 0]
    tip = rs.displacement[x > L - 1e-6, 2].mean()
    assert abs(tip) == pytest.approx(M * L ** 2 / (2 * E * I), rel=1.5e-2)
    # +M about +y bends the free end towards -z (right-hand rule): compression at the top (z = a)
    assert tip < 0
    mid = (np.abs(x - L / 2) < 1e-6)
    top, bot = mid & (s.mesh.nodes[:, 2] > a - 1e-6), mid & (s.mesh.nodes[:, 2] < 1e-6)
    sig = M * (a / 2) / I
    assert rs.stress[top, 0].mean() == pytest.approx(sig, rel=2e-2)
    assert rs.stress[bot, 0].mean() == pytest.approx(-sig, rel=2e-2)
    # zero net force: the reactions carry only the moment
    assert np.abs(rs.reaction_totals["BC_ALL_SUPPORTS"]).max() < 1e-6 * M / a


def test_large_deflection_cantilever_nlgeom(session):
    """End-loaded cantilever with P L^2 / (E I) = 1: elastica tip deflections 0.3017 L (vertical) and
    0.0566 L (horizontal shortening) [Bisshopp & Drucker 1945]; linear theory gives L / 3."""
    s = session
    L, a = 500.0, 10.0
    I = a ** 4 / 12
    P = E * I / L ** 2
    _, f = _bar(s, L=L, a=a, h=5.0, analysis="linear_static")
    ms = s.project.mesh_settings
    ms.element_shape, ms.element_order = "hex", 2
    s.project.constraints = [Constraint("fixed", [f["x0"]], name="root")]
    s.project.loads = [Load("force", [f["xL"]], {"magnitude": P, "direction": [0, 0, -1]}, name="P")]
    s.generate_mesh()
    x = s.mesh.nodes[:, 0]
    lin = _solve(s).displacement[x > L - 1e-6].mean(axis=0)
    assert -lin[2] == pytest.approx(L / 3, rel=1e-2)
    s.project.analysis.nonlinear_geometry = True
    nl = _solve(s).displacement[x > L - 1e-6].mean(axis=0)
    assert -nl[2] == pytest.approx(0.3017 * L, rel=1e-2)
    assert -nl[0] == pytest.approx(0.0566 * L, rel=3e-2)


def test_remote_force_equals_force_plus_moment(session):
    """A transverse remote force at distance d beyond the tip of a cantilever = tip force F plus
    end moment F d: tip deflection F L^3/(3EI) + (F d) L^2/(2EI); reactions carry F."""
    s = session
    L, a, F, d = 200.0, 20.0, 1000.0, 50.0
    _, f = _bar(s, L=L, a=a, h=5.0, analysis="linear_static")
    s.project.constraints = [Constraint("fixed", [f["x0"]], name="root")]
    s.project.loads = [Load("remote_force", [f["xL"]], {"magnitude": F, "direction": [0, 0, -1],
                                                        "point": [L + d, a / 2, a / 2]}, name="thrust")]
    s.generate_mesh()
    rs = _solve(s)
    I = a ** 4 / 12
    x = s.mesh.nodes[:, 0]
    exact = F * L ** 3 / (3 * E * I) + F * d * L ** 2 / (2 * E * I)
    assert -rs.displacement[x > L - 1e-6, 2].mean() == pytest.approx(exact, rel=1.5e-2)
    assert rs.reaction_totals["BC_ALL_SUPPORTS"] == pytest.approx([0, 0, F], abs=1e-6 * F)
