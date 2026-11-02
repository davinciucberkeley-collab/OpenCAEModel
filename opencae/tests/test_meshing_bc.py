"""Meshing, CAD-mesh mapping and boundary-condition resolution tests."""
import numpy as np
import pytest

from opencae.boundary_conditions.resolve import boundary_face_lookup, resolve_model, rigid_body_rank, tri_areas
from opencae.geometry.references import find_planar_face, make_ref
from opencae.meshing.gmsh_mesher import verify_c3d10_midsides
from opencae.meshing.mesh_data import FEMesh
from opencae.project.model import Constraint, Feature, Load, MeshRefinement


@pytest.mark.parametrize("order", [1, 2])
def test_mesh_valid_and_mapped(cantilever, order):
    s = cantilever
    s.project.mesh_settings.element_order = order
    m = s.generate_mesh()
    body = s.analysis_body()
    assert m.element_type == ("C3D10" if order == 2 else "C3D4")
    assert len(m.face_tris) == len(body.faces) and not m.unmapped_faces
    assert len(m.vertex_nodes) == len(body.vertices)
    assert np.all(m.quality["volume"] > 0)
    assert m.stats()["invalid_elements"] == 0
    assert m.quality["volume"].sum() == pytest.approx(body.volume, rel=1e-9)
    # every node is used by an element (no orphan nodes -> no singular DOFs)
    assert len(np.unique(m.elements)) == m.n_nodes
    for f in body.faces:  # mesh area of each mapped CAD face equals the CAD area (planar faces)
        assert tri_areas(m.nodes, m.face_tris[f.index][:, :3]).sum() == pytest.approx(f.area, rel=1e-9)
    if order == 2:
        assert verify_c3d10_midsides(m.nodes, m.elements, sample=len(m.elements)) < 1e-9


def test_curved_geometry_second_order_valid(session):
    f = session.add_feature(Feature("primitive", {"shape": "cylinder", "radius": 10, "height": 30}))
    session.project.mesh_settings.size_max = 4
    m = session.generate_mesh()
    assert m.stats()["invalid_elements"] == 0 and not m.unmapped_faces
    assert m.quality["sj"].min() > 0.2


def test_local_refinement_increases_density(cantilever):
    s = cantilever
    m0 = s.generate_mesh()
    body = s.analysis_body()
    fix = find_planar_face(body, [-1, 0, 0], [0, 0, 0])
    s.project.mesh_settings.refinements.append(MeshRefinement([make_ref(body, "face", fix)], size=1.5))
    m1 = s.generate_mesh()
    assert len(m1.face_tris[fix]) > 3 * len(m0.face_tris[fix])
    assert m1.n_elements > m0.n_elements


def test_mesh_save_load(cantilever, tmp_path):
    m = cantilever.generate_mesh()
    m2 = FEMesh.load(m.save(tmp_path / "m.npz"))
    assert np.array_equal(m.elements, m2.elements) and m2.face_tris.keys() == m.face_tris.keys()
    assert m2.vertex_nodes == m.vertex_nodes


def test_force_consistent_loads_sum_to_total(cantilever):
    s = cantilever
    for order in (1, 2):
        s.project.mesh_settings.element_order = order
        m = s.generate_mesh()
        rm = resolve_model(s.project, s.analysis_body(), m, s.bodies)
        assert rm.ok, [str(i) for i in rm.issues]
        assert rm.nodal_forces.sum(axis=0) == pytest.approx([0, 0, -1000.0], abs=1e-9)
        tip = s.project.loads[0].targets[0].index
        loaded = np.nonzero(np.abs(rm.nodal_forces).sum(axis=1))[0]
        assert set(loaded) <= set(m.face_nodes(tip))


def test_pressure_maps_to_element_faces(cantilever):
    s = cantilever
    m = s.generate_mesh()
    body = s.analysis_body()
    top = find_planar_face(body, [0, 0, 1], [0, 0, 10])
    s.project.loads = [Load("pressure", [make_ref(body, "face", top)], {"magnitude": 2.0}, name="p")]
    rm = resolve_model(s.project, body, m, s.bodies)
    assert rm.ok
    assert len(rm.pressures) == len(m.face_tris[top])
    assert rm.applied_force_total == pytest.approx([0, 0, -2.0 * body.faces[top].area], rel=1e-9)
    lookup = boundary_face_lookup(m)
    assert all(1 <= f <= 4 for _, f, _ in rm.pressures)
    assert len(lookup) == sum(len(t) for t in m.face_tris.values())  # boundary = union of CAD faces


def test_rigid_body_detection(cantilever):
    s = cantilever
    m = s.generate_mesh()
    body = s.analysis_body()
    fix = find_planar_face(body, [-1, 0, 0], [0, 0, 0])
    nodes = m.face_nodes(fix)
    full = {int(n): {1: 0.0, 2: 0.0, 3: 0.0} for n in nodes}
    assert rigid_body_rank(m, full)[0] == 6
    only_x = {int(n): {1: 0.0} for n in nodes}
    rank, free = rigid_body_rank(m, only_x)
    # u_x = 0 on the plane x = 0 restrains Tx, Ry, Rz; Ty, Tz and Rx remain free
    assert rank == 3 and len(free) == 3
    # no constraints at all
    s.project.constraints = []
    rm = resolve_model(s.project, body, m, s.bodies)
    assert not rm.ok and any("no constraints" in i.message for i in rm.issues)
    # x-only support -> reported as insufficiently constrained
    s.project.constraints = [Constraint("displacement", [make_ref(body, "face", fix)], {"ux": 0.0, "uy": None,
                                                                                       "uz": None}, name="ux")]
    rm = resolve_model(s.project, body, m, s.bodies)
    assert any("insufficiently constrained" in i.message for i in rm.issues)


def test_validation_messages(cantilever):
    s = cantilever
    issues, _ = s.validate()
    assert any("no mesh" in i.message for i in issues)
    s.generate_mesh()
    issues, rm = s.validate()
    assert not [i for i in issues if i.severity == "error"], [str(i) for i in issues]
    s.project.loads[0].targets = []
    issues, _ = s.validate()
    assert any("no geometry selected" in i.message for i in issues)
    # stale mesh is reported
    s.project.mesh_settings.size_max = 3.0
    issues, _ = s.validate()
    assert any("out of date" in i.message for i in issues)


def test_symmetry_requires_axis_aligned_plane(session):
    f = session.add_feature(Feature("primitive", {"shape": "box", "dx": 10, "dy": 10, "dz": 10}))
    session.add_feature(Feature("transform", {"body": f.id, "rotate_axis": [0, 0, 1], "rotate_angle": 30}))
    body = session.analysis_body()
    session.project.mesh_settings.size_max = 3
    m = session.generate_mesh()
    planar = next(x for x in body.faces if x.normal is not None and abs(x.normal[2]) < 0.5)
    session.project.constraints = [Constraint("symmetry", [make_ref(body, "face", planar.index)], name="sym")]
    rm = resolve_model(session.project, body, m, session.bodies)
    assert any("normal to a global axis" in i.message for i in rm.issues)


def test_consistent_volume_weights(session):
    """Exact integration of shape functions: sum = volume; curved C3D10 captures curvature."""
    import math
    from opencae.boundary_conditions.resolve import nodal_volume_weights
    session.add_feature(Feature("primitive", {"shape": "cylinder", "radius": 10, "height": 20}))
    session.project.mesh_settings.size_max = 5
    exact = math.pi * 100 * 20
    errs = {}
    for order in (1, 2):
        session.project.mesh_settings.element_order = order
        m = session.generate_mesh()
        w, vol = nodal_volume_weights(m)
        assert w.sum() == pytest.approx(vol, rel=1e-12)
        errs[order] = abs(vol - exact) / exact
    assert errs[2] < errs[1] / 10  # curved quadratic elements represent the cylinder far better
    assert errs[2] < 1e-4
