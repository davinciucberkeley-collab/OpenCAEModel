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


def _states(session):
    return {k: v for k, v, _ in session.workflow_status()}


def test_workflow_never_green_without_inputs(session):
    """Regression: an empty project showed 'Analysis' as done with zero bodies, nodes, loads."""
    st = _states(session)
    assert list(st) == ["Geometry", "Materials", "Mesh", "Physics", "Constraints & Loads", "Validation", "Solve",
                        "Results", "Report"]
    assert all(v in ("not_started",) for v in st.values()), st
    session.add_feature(Feature("primitive", {"shape": "box"}))
    st = _states(session)
    assert st["Geometry"] == "completed" and st["Physics"] == "completed"
    assert st["Materials"] == "not_started" and st["Constraints & Loads"] == "not_started"
    assert st["Validation"] == "invalid" and st["Solve"] == "not_started"


def test_workflow_progression(cantilever):
    s = cantilever
    st = _states(s)
    assert st["Materials"] == "completed" and st["Constraints & Loads"] == "completed"
    assert st["Mesh"] == "not_started" and st["Validation"] == "invalid"
    s.project.loads[0].targets = []
    assert _states(s)["Constraints & Loads"] == "in_progress"
    s.project.loads = []
    assert _states(s)["Constraints & Loads"] == "in_progress"


@pytest.mark.parametrize("shape,target", [("tet", 2000), ("tet", 12000), ("hex", 500), ("hex", 5000)])
def test_target_element_count(cantilever, shape, target):
    ms = cantilever.project.mesh_settings
    ms.element_shape, ms.sizing_mode, ms.target_elements, ms.target_tolerance = shape, "target_count", target, 0.1
    m = cantilever.generate_mesh()
    assert abs(m.n_elements - target) <= 0.12 * target, m.n_elements
    assert m.stats()["target_elements"] == target
    assert any(line.startswith("Target element count") for line in m.log)


@pytest.mark.parametrize("order", [1, 2])
def test_hex_mesh_mapping_and_loads(cantilever, order):
    s = cantilever
    s.project.mesh_settings.element_shape, s.project.mesh_settings.element_order = "hex", order
    s.project.mesh_settings.size_max = 8
    m = s.generate_mesh()
    body = s.analysis_body()
    assert m.element_type == ("C3D20" if order == 2 else "C3D8")
    assert len(m.face_tris) == 6 and all(t.shape[1] == (8 if order == 2 else 4) for t in m.face_tris.values())
    assert m.quality["volume"].sum() == pytest.approx(body.volume, rel=1e-9)
    for f in body.faces:
        assert tri_areas(m.nodes, m.face_tris[f.index]).sum() == pytest.approx(f.area, rel=1e-9)
    rm = resolve_model(s.project, body, m, s.bodies)
    assert rm.ok and rm.nodal_forces.sum(axis=0) == pytest.approx([0, 0, -1000.0], abs=1e-9)
    top = find_planar_face(body, [0, 0, 1], [0, 0, 10])
    s.project.loads = [Load("pressure", [make_ref(body, "face", top)], {"magnitude": 2.0}, name="p")]
    rm = resolve_model(s.project, body, m, s.bodies)
    assert rm.ok and len(rm.pressures) == len(m.face_tris[top])
    assert rm.applied_force_total == pytest.approx([0, 0, -2.0 * body.faces[top].area], rel=1e-9)


def test_hex_refused_for_non_hexahedral_topology(session):
    from opencae.meshing.gmsh_mesher import MeshingError
    session.add_feature(Feature("primitive", {"shape": "cylinder", "radius": 5, "height": 20}))
    session.project.mesh_settings.element_shape = "hex"
    with pytest.raises(MeshingError, match="topologically hexahedral"):
        session.generate_mesh()


def test_mesh_settings_hash_backward_compatible():
    """Adding settings fields must not invalidate meshes saved by 0.1."""
    from opencae.project.model import MeshSettings, stable_hash
    from dataclasses import asdict
    ms = MeshSettings(body="b", size_max=4.0)
    old = {k: v for k, v in asdict(ms).items() if k not in MeshSettings._ADDED}
    assert ms.settings_hash() == stable_hash(old)
    ms.element_shape = "hex"
    assert ms.settings_hash() != stable_hash(old)
