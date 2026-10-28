"""Multi-body (bonded assembly) analysis and multi-study projects."""
import json

import numpy as np
import pytest

from opencae.app.session import Session
from opencae.boundary_conditions.resolve import tri_areas
from opencae.geometry.references import find_planar_face, make_ref
from opencae.postprocessing.results import equilibrium_check
from opencae.project import io as pio
from opencae.project.model import Constraint, Feature, Load, Material


def two_bars(E2=68.9e9, nu2=None):
    s = Session()
    a = s.add_feature(Feature("primitive", {"shape": "box", "dx": 100, "dy": 10, "dz": 10}), "Bar A")
    b = s.add_feature(Feature("primitive", {"shape": "box", "dx": 60, "dy": 10, "dz": 10, "position": [100, 0, 0]}),
                      "Bar B")
    s.set_study_bodies([a.id, b.id])
    A, B = s.bodies[a.id], s.bodies[b.id]
    m1 = Material("E200", 200e9, 0.3, 7850, id="m1", is_example=False)
    m2 = Material("E2", E2, 0.3 * E2 / 200e9 if nu2 is None else nu2, 2700, id="m2", is_example=False)
    s.project.materials += [m1, m2]
    s.project.assign_material(a.id, "m1")
    s.project.assign_material(b.id, "m2")
    cons = []
    for body in (A, B):
        for n in ([0, -1, 0], [0, 0, -1]):
            cons.append(Constraint("symmetry", [make_ref(body, "face", find_planar_face(body, n, [0, 0, 0]))],
                                   name=f"sym{len(cons)}"))
    cons.append(Constraint("symmetry", [make_ref(A, "face", find_planar_face(A, [-1, 0, 0], [0, 0, 0]))], name="sx"))
    s.project.constraints = cons
    s.project.loads = [Load("force", [make_ref(B, "face", find_planar_face(B, [1, 0, 0], [160, 0, 0]))],
                            {"magnitude": 5000.0, "direction": [1, 0, 0]}, name="F")]
    s.project.mesh_settings.size_max = 5
    return s, a, b


def test_mesh_assigns_elements_to_bodies():
    s, a, b = two_bars()
    m = s.generate_mesh()
    assert m.body_ids == [a.id, b.id] and not m.unmapped_faces and len(m.face_tris) == 12
    c = m.nodes[m.elements[:, :4]].mean(axis=1)
    assert c[m.element_body == 0, 0].max() < 100 < c[m.element_body == 1, 0].min()
    shared = np.intersect1d(np.unique(m.elements[m.element_body == 0]), np.unique(m.elements[m.element_body == 1]))
    assert len(shared) > 10  # bonded: interface nodes are shared


@pytest.mark.solver
def test_bonded_two_material_bar_exact():
    """Series bar u = F L1/(E1 A) + F L2/(E2 A). Exact when nu/E is equal in both bodies
    (equal lateral strain, so the bonded interface carries no 3-D constraint stresses)."""
    s, a, b = two_bars()
    m = s.generate_mesh()
    out, issues = s.run_solver()
    assert out.success, [str(i) for i in issues]
    tip = np.isclose(m.nodes[:, 0], 160)
    u = s.results.displacement[tip, 0].mean()
    exact = 5000 * 100 / (100 * 200e3) + 5000 * 60 / (100 * 68.9e3)
    assert u == pytest.approx(exact, rel=2e-5)
    assert s.results.stress[:, 0] == pytest.approx(50.0, rel=1e-4)
    assert equilibrium_check(s.project.results.summary)["ok"]
    deck = (s.data_dir / s.project.results.directory / "job.inp").read_text()
    assert deck.count("*SOLID SECTION") == 2 and "ELSET=BODY2" in deck


@pytest.mark.solver
def test_bonded_different_lateral_strain_is_physical_not_exact():
    s, a, b = two_bars(nu2=0.3)  # nu/E differs -> interface constraint, 1-D formula only approximate
    s.generate_mesh()
    assert s.run_solver()[0].success
    exact_1d = 5000 * 100 / (100 * 200e3) + 5000 * 60 / (100 * 68.9e3)
    u = s.results.displacement[np.isclose(s.mesh.nodes[:, 0], 160), 0].mean()
    assert 0.0005 < abs(u - exact_1d) / exact_1d < 0.01
    assert equilibrium_check(s.project.results.summary)["ok"]


def test_split_face_and_floating_part():
    s = Session()
    p = s.add_feature(Feature("primitive", {"shape": "box", "dx": 100, "dy": 50, "dz": 5}), "Plate")
    k = s.add_feature(Feature("primitive", {"shape": "box", "dx": 20, "dy": 10, "dz": 30, "position": [40, 20, 5]}),
                      "Block")
    s.set_study_bodies([p.id, k.id])
    s.project.mesh_settings.size_max = 4
    m = s.generate_mesh()
    P = s.bodies[p.id]
    top = find_planar_face(P, [0, 0, 1], [0, 0, 5])
    # the plate top is split by the block footprint into two mesh surfaces; area is preserved
    assert tri_areas(m.nodes, m.face_tris[m.key(p.id, top)]).sum() == pytest.approx(P.faces[top].area, rel=1e-9)
    assert not m.unmapped_faces
    s2 = Session()
    f = s2.add_feature(Feature("primitive", {"shape": "box", "dx": 50, "dy": 10, "dz": 10}), "Fixed")
    g = s2.add_feature(Feature("primitive", {"shape": "box", "dx": 50, "dy": 10, "dz": 10, "position": [0, 30, 0]}),
                       "Floating arm")
    s2.set_study_bodies([f.id, g.id])
    for b in (f, g):
        s2.project.assign_material(b.id, "mat_steel")
    F = s2.bodies[f.id]
    s2.project.constraints = [Constraint("fixed", [make_ref(F, "face", find_planar_face(F, [-1, 0, 0], [0, 0, 0]))],
                                         name="fix")]
    s2.project.loads = [Load("gravity", [], {"magnitude": 9806.65, "direction": [0, 0, -1]}, name="g")]
    s2.project.mesh_settings.size_max = 5
    s2.generate_mesh()
    issues, _ = s2.validate()
    msg = " ".join(i.message for i in issues if i.severity == "error")
    assert "insufficiently constrained" in msg and "Floating arm" in msg


def test_overlapping_bodies_refused():
    from opencae.meshing.gmsh_mesher import MeshingError
    s = Session()
    a = s.add_feature(Feature("primitive", {"shape": "box", "dx": 10, "dy": 10, "dz": 10}))
    b = s.add_feature(Feature("primitive", {"shape": "box", "dx": 10, "dy": 10, "dz": 10, "position": [5, 0, 0]}))
    s.set_study_bodies([a.id, b.id])
    with pytest.raises(MeshingError, match="overlap"):
        s.generate_mesh()


@pytest.mark.solver
def test_studies_are_independent_and_persist(tmp_path):
    s = Session()
    s.create_cantilever()
    s.project.mesh_settings.size_max = 6
    s.generate_mesh()
    assert s.run_solver()[0].success
    st1 = s.study
    u_steel = s.results.summary()["max_displacement"]
    st2 = s.duplicate_study(name="Aluminium")
    s.project.assign_material(s.analysis_body().id, "mat_al6061")
    assert s.mesh is not None and s.project.mesh_is_current()  # mesh reused, results not
    assert s.results is None
    assert s.run_solver()[0].success
    u_al = s.results.summary()["max_displacement"]
    assert u_al / u_steel == pytest.approx(200 / 68.9, rel=5e-3)  # E ratio (nu differs slightly)
    # an unrelated new body does not invalidate either study
    s.add_feature(Feature("primitive", {"shape": "sphere", "position": [500, 0, 0]}))
    s.set_active_study(st2.id)
    assert s.project.results_are_current(st1) and s.project.results_are_current(st2)
    path = s.save(tmp_path / "studies.ocae")
    doc = json.loads(path.read_text())
    assert doc["schema_version"] == pio.SCHEMA_VERSION and len(doc["project"]["studies"]) == 2
    r = Session()
    r.open(path)
    assert [x.name for x in r.project.studies] == [st1.name, "Aluminium"]
    _, rs1 = r.load_study_results(st1.id)
    _, rs2 = r.load_study_results(st2.id)
    assert rs1.summary()["max_displacement"] == pytest.approx(u_steel)
    assert rs2.summary()["max_displacement"] == pytest.approx(u_al)
    # editing the beam invalidates both studies that use it
    r.project.geometry.features[0].params["length"] = 210.0
    assert not r.project.mesh_is_current(r.project.studies[0])


def test_v1_project_migrates(tmp_path):
    """Projects saved by 0.1 (schema 1) open as one study, with mesh/results still current."""
    from pathlib import Path
    src = Path(__file__).resolve().parents[1] / "examples" / "projects" / "cantilever_beam.ocae"
    doc = json.loads(src.read_text())
    if doc["schema_version"] != 1:
        pytest.skip("example already re-saved in schema 2")
    s = Session()
    s.open(src)
    assert len(s.project.studies) == 1 and s.project.results_are_current() and s.results is not None
