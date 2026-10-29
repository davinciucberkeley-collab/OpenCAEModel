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


def test_sphere_drag_external_flow():
    """External flow: sphere (D = 10 mm) at Re = 20 in an enclosure built by the flow-domain tool.
    Drag coefficient vs the Schiller-Naumann correlation Cd = 24/Re (1 + 0.15 Re^0.687) (itself
    within about 5 % of experiments); the side walls at 4 D add a small blockage effect."""
    from opencae.project.model import MeshRefinement
    s = Session()
    s.new("sphere")
    s.add_feature(Feature("primitive", {"shape": "sphere", "radius": 5.0}))
    sph = s.analysis_body()
    st = s.create_flow_domain([sph.id], "+x", 0.02, 4.0, 4.0, 4.0)
    assert sph.id in s.bodies  # the solid part is kept
    a = st.analysis
    a.fluid_density, a.fluid_viscosity, a.cfd_cfl = 1000.0, 0.01, 400.0
    D = 0.01
    a.ref_area = np.pi * D ** 2 / 4
    dom = s.bodies[st.body_ids[0]]
    assert dom.volume == pytest.approx(90.0 ** 3 - np.pi * 10.0 ** 3 / 6, rel=1e-3)  # 4 D clearance each side
    st.mesh_settings.size_max = 6.0
    st.mesh_settings.growth_rate = 1.3
    st.mesh_settings.refinements = [MeshRefinement(
        targets=[make_ref(dom, "face", f.index) for f in dom.faces if f.surface_type == "sphere"], size=0.6,
        name="sphere")]
    s.generate_mesh()
    _solve(s)
    sm = s.project.results.summary
    Re = 1000.0 * 0.02 * D / 0.01
    cd_ref = 24 / Re * (1 + 0.15 * Re ** 0.687)
    assert sm["converged"]
    assert sm["force_coefficients"][0] == pytest.approx(cd_ref, rel=0.08)
    assert abs(sm["force_coefficients"][1]) < 0.02 * cd_ref and abs(sm["force_coefficients"][2]) < 0.02 * cd_ref


@pytest.mark.parametrize("shape,tol", [("hex", 0.01), ("tet", 0.04)])
def test_plane_poiseuille_2d(shape, tol):
    """True 2-D flow on the top face of a plate (H = 10 mm, L = 200 mm), conditions on its edges:
    16 structured quadrilaterals (or triangles) across the channel."""
    H, L = 10.0, 200.0
    s = Session()
    s.new("ch2d")
    s.add_feature(Feature("primitive", {"shape": "box", "dx": L, "dy": H, "dz": 1.0}))
    b = s.analysis_body()
    a = s.project.analysis
    a.analysis_type, a.cfd_dimension = "cfd_incompressible", "2d"
    a.fluid_density, a.fluid_viscosity, a.cfd_cfl = RHO, MU, 400.0
    top = find_planar_face(b, [0, 0, 1], [0, 0, 1.0])
    a.cfd_face = f"{b.id}|face:{top}"
    ms = s.project.mesh_settings
    ms.element_order, ms.element_shape, ms.size_max = 1, shape, 0.625
    ein = next(e.index for e in b.edges if abs(e.centroid[2] - 1) < 1e-9 and abs(e.centroid[0]) < 1e-9)
    eout = next(e.index for e in b.edges if abs(e.centroid[2] - 1) < 1e-9 and abs(e.centroid[0] - L) < 1e-9)
    s.project.loads = [Load("velocity_inlet", [make_ref(b, "edge", ein)], {"magnitude": 0.01, "direction": "normal"},
                            name="Inlet"),
                       Load("pressure_outlet", [make_ref(b, "edge", eout)], {"magnitude": 0.0}, name="Outlet")]
    m = s.generate_mesh()
    assert m.element_type == ("F2D4" if shape == "hex" else "F2D3")
    rs = _solve(s)
    sm = s.project.results.summary
    assert sm["two_dimensional"] and sm["converged"]
    Um = sm["markers"]["Outlet"]["flow_m3s"] / (H * 1e-3)  # per metre of depth
    P = m.nodes * 1e-3
    sel = (P[:, 0] > 0.1) & (P[:, 0] < 0.19)
    dpdx = np.polyfit(P[sel, 0], rs.pressure[sel], 1)[0]
    assert dpdx == pytest.approx(-12 * MU * Um / (H * 1e-3) ** 2, rel=tol)
    # wall shear force per metre of depth balances the pressure drop (developed flow dominates)
    assert sm["force_per_unit_depth"] and sm["wall_force_N"][0] > 0
    # a structural study cannot use this planar mesh
    s.project.analysis.analysis_type = "linear_static"
    assert any("planar" in i.message for i in s.validate()[0])


def test_2d_boundary_layer_mesh_improves_wall_gradient():
    """Triangles with 5 boundary-layer rows on the walls: thin first cell at the wall and a smaller
    pressure-gradient error than the same triangle mesh without layers."""
    errs = {}
    for layers in (0, 5):
        H, L = 10.0, 200.0
        s = Session()
        s.new("bl")
        s.add_feature(Feature("primitive", {"shape": "box", "dx": L, "dy": H, "dz": 1.0}))
        b = s.analysis_body()
        a = s.project.analysis
        a.analysis_type, a.cfd_dimension = "cfd_incompressible", "2d"
        a.fluid_density, a.fluid_viscosity, a.cfd_cfl = RHO, MU, 400.0
        a.cfd_face = f"{b.id}|face:{find_planar_face(b, [0, 0, 1], [0, 0, 1.0])}"
        ms = s.project.mesh_settings
        ms.element_order, ms.size_max, ms.bl_layers, ms.bl_first = 1, 1.0, layers, 0.1
        ein = next(e.index for e in b.edges if abs(e.centroid[2] - 1) < 1e-9 and abs(e.centroid[0]) < 1e-9)
        eout = next(e.index for e in b.edges if abs(e.centroid[2] - 1) < 1e-9 and abs(e.centroid[0] - L) < 1e-9)
        s.project.loads = [Load("velocity_inlet", [make_ref(b, "edge", ein)], {"magnitude": 0.01,
                                                                               "direction": "normal"}, name="Inlet"),
                           Load("pressure_outlet", [make_ref(b, "edge", eout)], {"magnitude": 0.0}, name="Outlet")]
        m = s.generate_mesh()
        if layers:
            assert len(ms.bl_edges) == 2  # the two walls, not the inlet/outlet edges
            y = np.unique(np.round(m.nodes[:, 1], 6))
            assert y[1] == pytest.approx(0.1, rel=1e-3)
        rs = _solve(s)
        sm = s.project.results.summary
        Um = sm["markers"]["Outlet"]["flow_m3s"] / (H * 1e-3)
        P = m.nodes * 1e-3
        sel = (P[:, 0] > 0.1) & (P[:, 0] < 0.19)
        dpdx = np.polyfit(P[sel, 0], rs.pressure[sel], 1)[0]
        errs[layers] = abs(dpdx / (-12 * MU * Um / (H * 1e-3) ** 2) - 1)
    assert errs[5] < 0.6 * errs[0], errs
