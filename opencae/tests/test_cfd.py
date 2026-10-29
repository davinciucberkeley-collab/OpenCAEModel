"""Incompressible flow with SU2 against exact laminar solutions (skipped if SU2 is absent)."""
import numpy as np
import pytest

from opencae.app.session import Session
from opencae.geometry.references import find_planar_face, make_ref
from opencae.postprocessing.sampling import locate
from opencae.project.model import Constraint, Feature, Load

pytestmark = pytest.mark.cfd
MU, RHO = 1e-3, 1000.0


def _duct(dx, dy, L, h, shape="hex"):
    s = Session()
    s.new("flow")
    s.add_feature(Feature("primitive", {"shape": "box", "dx": dx, "dy": dy, "dz": L}))
    b = s.analysis_body()
    a = s.project.analysis
    a.analysis_type = "cfd_incompressible"
    a.fluid_density, a.fluid_viscosity, a.cfd_cfl = RHO, MU, 400.0
    ms = s.project.mesh_settings
    ms.element_shape, ms.element_order, ms.size_max = shape, 1, h
    f = lambda n, p: make_ref(b, "face", find_planar_face(b, n, p))  # noqa: E731
    s.project.loads = [Load("velocity_inlet", [f([0, 0, -1], [0, 0, 0])], {"magnitude": 0.01, "direction": "normal"},
                            name="Inlet"),
                       Load("pressure_outlet", [f([0, 0, 1], [0, 0, L])], {"magnitude": 0.0}, name="Outlet")]
    return s, b, f


def _solve(s):
    out, issues = s.run_solver()
    assert out and out.success, (out.errors if out else [str(i) for i in issues])
    return s.results


def _section_mean(mesh, w, z, extent, n=101):
    """Mean axial velocity on the cross-section z (project units) by interpolation."""
    gx = np.linspace(0, extent[0], n)
    gy = np.linspace(0, extent[1], n)
    X, Y = np.meshgrid(gx, gy, indexing="ij")
    ids, wt, inside = locate(mesh, np.c_[X.ravel(), Y.ravel(), np.full(X.size, z)])
    u = np.einsum("pk,pk->p", wt, w[ids]).reshape(n, n)
    return np.trapezoid(np.trapezoid(u, gy, axis=1), gx) / (extent[0] * extent[1])


def test_su2_selftest():
    from opencae.cfd.su2 import SU2Backend
    rep = SU2Backend().selftest()
    assert rep.ok, rep.summary()


def test_plane_poiseuille_channel():
    """Fully developed laminar flow between parallel plates (H = 10 mm), modelled as a slab with
    slip (symmetry) side faces: dp/dz = -12 mu U / H^2, u_max = 1.5 U."""
    H, W, L = 10.0, 2.0, 150.0
    s, b, f = _duct(W, H, L, 1.0)
    s.project.constraints = [Constraint("slip_wall", [f([-1, 0, 0], [0, 0, 0]), f([1, 0, 0], [W, 0, 0])],
                                        name="Symmetry")]
    s.generate_mesh()
    rs = _solve(s)
    sm = s.project.results.summary
    assert sm["converged"] and sm["mass_imbalance"] < 1e-3
    P = s.mesh.nodes * 1e-3
    Um = _section_mean(s.mesh, rs.velocity[:, 2], 100.0, (W, H))
    sel = (P[:, 2] > 0.07) & (P[:, 2] < 0.14) & (np.abs(P[:, 1] - H * 5e-4) < H * 6e-5)
    dpdz = np.polyfit(P[sel, 2], rs.pressure[sel], 1)[0]
    assert dpdz == pytest.approx(-12 * MU * Um / (H * 1e-3) ** 2, rel=0.02)  # 10 cells across: +1.1 %
    assert rs.velocity[sel, 2].max() / Um == pytest.approx(1.5, rel=0.015)
    # nominal inflow vs delivered: the inlet nodes on the walls are at rest (reported, not hidden)
    assert 0.85 < -sm["markers"]["Inlet"]["flow_m3s"] / sm["nominal_inflow_m3s"] < 1.0


def test_square_duct_poiseuille_number():
    """Square duct: -dp/dz = 28.44 mu U / a^2 (f Re = 56.91), u_max / U = 2.096 (Shah & London)."""
    a, L = 10.0, 150.0
    s, b, f = _duct(a, a, L, 1.0)
    s.generate_mesh()
    rs = _solve(s)
    P = s.mesh.nodes * 1e-3
    Um = _section_mean(s.mesh, rs.velocity[:, 2], 100.0, (a, a))
    c = (P[:, 2] > 0.07) & (P[:, 2] < 0.14) & (np.abs(P[:, 0] - a * 5e-4) + np.abs(P[:, 1] - a * 5e-4) < a * 6e-5)
    dpdz = np.polyfit(P[c, 2], rs.pressure[c], 1)[0]
    assert dpdz == pytest.approx(-28.438 * MU * Um / (a * 1e-3) ** 2, rel=0.04)  # 10 cells: +3.3 %
    assert rs.velocity[c, 2].max() / Um == pytest.approx(2.0962, rel=0.03)


def test_flow_validation_and_reload(tmp_path):
    s, b, f = _duct(10.0, 10.0, 60.0, 2.0)
    s.project.mesh_settings.element_order = 2
    s.generate_mesh()
    issues, _ = s.validate()
    assert any("linear elements" in i.message for i in issues)
    s.project.mesh_settings.element_order = 1
    s.generate_mesh()
    keep = s.project.loads
    s.project.loads = []
    assert any("no inlet or outlet" in i.message for i in s.validate()[0])
    s.project.loads = keep
    issues, _ = s.validate()
    assert not [i for i in issues if i.severity == "error"], issues
    assert any("no-slip walls" in i.message for i in issues)
    assert not any("material" in i.message.lower() and i.severity == "error" for i in issues)
    rs = _solve(s)
    path = s.save(tmp_path / "flow.ocae")
    s2 = Session()
    s2.open(path)
    assert s2.results.kind == "cfd" and np.allclose(s2.results.velocity, rs.velocity)
    assert s2.project.results_are_current()
