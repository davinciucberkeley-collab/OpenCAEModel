"""Separately meshed bodies connected by tie (bonded) and contact interactions."""
import numpy as np
import pytest

from opencae.geometry.references import find_planar_face, make_ref
from opencae.project.model import Constraint, Feature, Interaction, Load

pytestmark = pytest.mark.solver
E = 200e3


def _stack(s, kind, h_lo=2.5, h_hi=2.5, friction=0.0):
    """Two 10 mm cubes stacked in z, meshed separately (non-matching meshes), pressure on top."""
    s.add_feature(Feature("primitive", {"shape": "box", "dx": 10, "dy": 10, "dz": 10}))
    s.add_feature(Feature("primitive", {"shape": "box", "dx": 10, "dy": 10, "dz": 10, "position": [0, 0, 10]}))
    lo, hi = (s.bodies[b] for b in s.regen.order)
    s.set_study_bodies(s.regen.order)
    for b in (lo, hi):
        s.project.assign_material(b.id, "mat_steel")
    F = lambda b, n, p: make_ref(b, "face", find_planar_face(b, n, p))  # noqa: E731
    ms = s.project.mesh_settings
    ms.assembly_mesh, ms.size_max = "separate", h_lo
    s.project.constraints = [
        Constraint("symmetry", [F(lo, [0, 0, -1], [0, 0, 0])], name="base"),
        Constraint("symmetry", [F(lo, [-1, 0, 0], [0, 0, 0]), F(hi, [-1, 0, 0], [0, 0, 10])], name="sx"),
        Constraint("symmetry", [F(lo, [0, -1, 0], [0, 0, 0]), F(hi, [0, -1, 0], [0, 0, 10])], name="sy")]
    s.project.loads = [Load("pressure", [F(hi, [0, 0, 1], [0, 0, 20])], {"magnitude": 100.0}, name="p")]
    s.project.interactions = [Interaction(kind, [F(lo, [0, 0, 1], [0, 0, 10])], [F(hi, [0, 0, -1], [0, 0, 10])],
                                          {"friction": friction}, name="interface")]
    return lo, hi


def _solve(s):
    out, issues = s.run_solver()
    assert out and out.success, (out.errors if out else [str(i) for i in issues])
    return s.results


def test_separate_meshes_need_an_interaction(session):
    s = session
    _stack(s, "bonded")
    s.project.interactions = []
    s.generate_mesh()
    assert s.mesh.n_nodes > 0 and len(s.mesh.body_ids) == 2
    issues, _ = s.validate()
    assert any("insufficiently constrained" in i.message for i in issues)  # top block floats


@pytest.mark.parametrize("kind", ["bonded", "contact"])
def test_stacked_blocks_uniaxial_compression(session, kind):
    """Uniform compression: sigma_zz = -p in both blocks, top displacement p (h1 + h2) / E
    (contact adds the penalty penetration p / K)."""
    s = session
    _stack(s, kind)
    s.generate_mesh()
    rs = _solve(s)
    z = s.mesh.nodes[:, 2]
    assert rs.stress[:, 2] == pytest.approx(-100.0, rel=1e-3)
    u_top = rs.displacement[z > 19.999, 2].mean()
    exact = -100.0 * 20.0 / E
    if kind == "bonded":
        assert u_top == pytest.approx(exact, rel=1e-4)
    else:
        K = 10 * E / 2.5  # automatic penalty ~ 10 E / h (h = mean element edge)
        assert u_top == pytest.approx(exact, rel=0.03)
        assert -u_top - (-exact) < 3 * 100.0 / K
        sm = s.project.results.summary
        assert sm["max_contact_pressure"] == pytest.approx(100.0, rel=0.05)


def test_tied_bar_non_matching_meshes_in_tension(session):
    """Two bars of different mesh density tied end to end: u = F L / (E A) exactly."""
    s = session
    s.add_feature(Feature("primitive", {"shape": "box", "dx": 50, "dy": 10, "dz": 10}))
    s.add_feature(Feature("primitive", {"shape": "box", "dx": 50, "dy": 10, "dz": 10, "position": [50, 0, 0]}))
    a, b = (s.bodies[x] for x in s.regen.order)
    s.set_study_bodies(s.regen.order)
    for x in (a, b):
        s.project.assign_material(x.id, "mat_steel")
    F = lambda bb, n, p: make_ref(bb, "face", find_planar_face(bb, n, p))  # noqa: E731
    s.project.mesh_settings.assembly_mesh, s.project.mesh_settings.size_max = "separate", 4.0
    s.project.constraints = [Constraint("symmetry", [F(a, [-1, 0, 0], [0, 0, 0])], name="sx"),
                             Constraint("symmetry", [F(a, [0, -1, 0], [0, 0, 0]), F(b, [0, -1, 0], [50, 0, 0])],
                                        name="sy"),
                             Constraint("symmetry", [F(a, [0, 0, -1], [0, 0, 0]), F(b, [0, 0, -1], [50, 0, 0])],
                                        name="sz")]
    s.project.loads = [Load("force", [F(b, [1, 0, 0], [100, 0, 0])], {"magnitude": 5000.0, "direction": [1, 0, 0]},
                            name="F")]
    s.project.interactions = [Interaction("bonded", [F(a, [1, 0, 0], [50, 0, 0])], [F(b, [-1, 0, 0], [50, 0, 0])],
                                          name="tie")]
    s.generate_mesh()
    rs = _solve(s)
    x = s.mesh.nodes[:, 0]
    assert rs.displacement[x > 99.999, 0].mean() == pytest.approx(5000.0 * 100.0 / (E * 100.0), rel=1e-3)
    far = np.abs(x - 50.0) > 10.0  # away from the tied, non-matching interface
    assert rs.stress[far, 0] == pytest.approx(50.0, rel=1e-3)
    # next to the interface the tie constraint perturbs the stress locally (non-matching meshes)
    assert np.median(rs.stress[:, 0]) == pytest.approx(50.0, rel=1e-3)
    assert np.abs(rs.stress[:, 0] - 50.0).max() < 0.15 * 50.0


def _two_bars(s, analysis, h=(4.0,)):
    """Bar 0..100 split at x = 50 into two bodies, meshed separately and tied at the joint."""
    s.add_feature(Feature("primitive", {"shape": "box", "dx": 50, "dy": 10, "dz": 10}))
    s.add_feature(Feature("primitive", {"shape": "box", "dx": 50, "dy": 10, "dz": 10, "position": [50, 0, 0]}))
    a, b = (s.bodies[x] for x in s.regen.order)
    s.set_study_bodies(s.regen.order)
    for x in (a, b):
        s.project.assign_material(x.id, "mat_steel")
    s.project.analysis.analysis_type = analysis
    F = lambda bb, n, p: make_ref(bb, "face", find_planar_face(bb, n, p))  # noqa: E731
    s.project.mesh_settings.assembly_mesh, s.project.mesh_settings.size_max = "separate", h[0]
    s.project.interactions = [Interaction("bonded", [F(a, [1, 0, 0], [50, 0, 0])], [F(b, [-1, 0, 0], [50, 0, 0])],
                                          name="tie")]
    return a, b, F


def test_tied_bars_thermal_conduction(session):
    """Prescribed end temperatures across a tied joint: linear profile, exact for any mesh."""
    s = session
    a, b, F = _two_bars(s, "thermal_steady")
    s.project.constraints = [Constraint("temperature", [F(a, [-1, 0, 0], [0, 0, 0])], {"value": 0.0}, name="c"),
                             Constraint("temperature", [F(b, [1, 0, 0], [100, 0, 0])], {"value": 100.0}, name="h")]
    s.generate_mesh()
    rs = _solve(s)
    assert rs.quantity("T") == pytest.approx(s.mesh.nodes[:, 0], abs=1e-3 * 100)
    # without the tie the second bar has no path to the first: still determined (both ends fixed),
    # but a bar with only a flux load would float -> validation error
    s.project.interactions = []
    s.project.constraints = s.project.constraints[:1]
    s.project.loads = [Load("heat_flux", [F(b, [1, 0, 0], [100, 0, 0])], {"magnitude": 1e3}, name="q")]
    issues, _ = s.validate()
    assert any("not connected" in i.message for i in issues)


def test_tied_bars_modal_matches_single_body(session):
    """Cantilever natural frequencies: tied two-body model vs one body (same mesh size)."""
    from opencae.app.session import Session
    s = session
    a, b, F = _two_bars(s, "modal")
    s.project.analysis.n_modes = 3
    s.project.constraints = [Constraint("fixed", [F(a, [-1, 0, 0], [0, 0, 0])], name="root")]
    s.generate_mesh()
    f_tied = _solve(s).frequencies
    ref = Session()
    ref.add_feature(Feature("primitive", {"shape": "box", "dx": 100, "dy": 10, "dz": 10}))
    rb = ref.analysis_body()
    ref.project.assign_material(rb.id, "mat_steel")
    ref.project.analysis.analysis_type, ref.project.analysis.n_modes = "modal", 3
    ref.project.mesh_settings.size_max = 4.0
    ref.project.constraints = [Constraint("fixed", [make_ref(rb, "face", find_planar_face(rb, [-1, 0, 0],
                                                                                        [0, 0, 0]))], name="r")]
    ref.generate_mesh()
    f_one = _solve(ref).frequencies
    assert f_tied == pytest.approx(f_one, rel=5e-3)
    # contact is rejected in a linear eigenvalue analysis
    s.project.interactions[0].kind = "contact"
    issues, _ = s.validate()
    assert any("not available in a modal analysis" in i.message for i in issues)


def test_touching_face_detection():
    from opencae.app.session import Session
    from opencae.geometry.occ_kernel import touching_faces
    s = Session()
    s.add_feature(Feature("primitive", {"shape": "box", "dx": 10, "dy": 10, "dz": 10}))
    s.add_feature(Feature("primitive", {"shape": "box", "dx": 6, "dy": 6, "dz": 10, "position": [2, 2, 10]}))
    s.add_feature(Feature("primitive", {"shape": "box", "dx": 6, "dy": 6, "dz": 10, "position": [2, 2, 10.5]}))
    a, b, c = (s.bodies[x] for x in s.regen.order)
    pairs = touching_faces(a, b, 1e-6)
    assert len(pairs) == 1
    i, j, area = pairs[0]
    assert a.faces[i].normal == pytest.approx([0, 0, 1]) and b.faces[j].normal == pytest.approx([0, 0, -1])
    assert area == pytest.approx(36.0)
    assert touching_faces(a, c, 1e-6) == []  # 0.5 gap: never closed automatically
