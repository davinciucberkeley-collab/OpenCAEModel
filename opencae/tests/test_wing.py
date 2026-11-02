"""Half-wing (S1223 section, 890 mm span) from a user STEP file: thin trailing edge meshing with
curved quadratic elements, and the external-flow domain with a symmetry plane at the root."""
from pathlib import Path

import numpy as np
import pytest

from opencae.app.session import Session

STEP = Path(__file__).parent / "data" / "geometry" / "wing_s1223_half.step"


@pytest.fixture
def wing():
    s = Session()
    s.import_geometry(str(STEP))
    return s, s.analysis_body()


def test_wing_imports_as_one_solid(wing):
    s, b = wing
    assert b.kind == "solid" and len(b.faces) == 4
    lo, hi = b.bbox
    assert hi[1] - lo[1] == pytest.approx(890.0, abs=0.01)  # span


def test_curved_c3d10_mesh_of_thin_trailing_edge_is_valid(wing):
    """Curving inverts a few trailing-edge elements; they are straightened (Gmsh's high-order
    optimiser aborted the process on this geometry and is not used)."""
    s, b = wing
    s.project.mesh_settings.size_max = 47.2  # the automatic size (body size / 20) that triggered the abort
    s.generate_mesh()
    st = s.mesh.stats()
    assert st["invalid_elements"] == 0
    assert s.mesh.element_type == "C3D10"
    log = "\n".join(getattr(s.mesh, "log", []) or [])
    assert "inverted after curving" in log or st.get("sj_min", 1.0) > 0


def test_flow_domain_with_symmetry_plane_at_root(wing):
    s, b = wing
    st = s.create_flow_domain([b.id], "+x", 15.0, upstream=1.0, downstream=2.0, lateral=1.0, symmetry="+y")
    dom = s.bodies[st.analysis_body_ids()[0]]
    lo, hi = dom.bbox
    assert hi[1] == pytest.approx(b.bbox[1][1], abs=1e-6)  # box ends at the root plane
    names = {c.name: c for c in s.project.constraints}
    assert "Symmetry plane" in names
    sym = dom.faces[names["Symmetry plane"].targets[0].index]
    assert sym.normal == pytest.approx([0, 1, 0])
    # the root face is not a wall: 6 box faces + upper, lower and tip surfaces of the wing
    assert len(dom.faces) == 9
    with pytest.raises(ValueError):
        s.create_flow_domain([b.id], "+x", 15.0, symmetry="-x")


def test_2d_root_section_mesh_with_refinement_and_trailing_edge_fan(wing):
    """2-D flow on the symmetry plane: the airfoil contour is refined locally and the boundary
    layer fans around the sharp trailing edge (without the fan Gmsh fails: 'Edge not recovered')."""
    from opencae.app.session import face_edge_indices
    from opencae.geometry.references import make_ref
    from opencae.project.model import MeshRefinement
    s, b = wing
    st = s.create_flow_domain([b.id], "+x", 15.0, upstream=1.0, downstream=2.0, lateral=1.0, symmetry="+y")
    dom = s.bodies[st.analysis_body_ids()[0]]
    sym = next(f.index for f in dom.faces if f.normal is not None and f.normal[1] > 0.99)
    a = s.project.analysis
    a.cfd_dimension, a.cfd_face = "2d", f"{dom.id}|face:{sym}"
    edges = face_edge_indices(dom, sym)
    foil = [i for i in edges if dom.edges[i].curve_type != "line"]
    assert len(foil) == 2
    from opencae.project.model import Constraint, Load
    x = {i: dom.edges[i].centroid[0] for i in edges if i not in foil}
    ein, eout = min(x, key=x.get), max(x, key=x.get)
    side = [i for i in x if i not in (ein, eout)]
    s.project.loads = [Load("velocity_inlet", [make_ref(dom, "edge", ein)], {"magnitude": 15.0,
                                                                            "direction": "normal"}, name="Inlet"),
                       Load("pressure_outlet", [make_ref(dom, "edge", eout)], {"magnitude": 0.0}, name="Outlet")]
    s.project.constraints = [Constraint("slip_wall", [make_ref(dom, "edge", i) for i in side], name="Far field")]
    ms = s.project.mesh_settings
    ms.element_order, ms.size_max = 1, 60.0
    ms.refinements = [MeshRefinement([make_ref(dom, "edge", i) for i in foil], 1.5, name="airfoil")]
    ms.bl_layers, ms.bl_first, ms.bl_growth = 10, 0.05, 1.2
    msgs = []
    m = s.generate_mesh(progress=msgs.append)
    assert m.element_type == "F2D3"
    text = "\n".join(msgs)
    assert "Local refinement 'airfoil'" in text and "fans at 1 sharp corner" in text
    assert sorted(ms.bl_edges) == sorted(foil)
    # cells next to the airfoil are about the requested size: 686 mm contour / 1.5 mm
    from scipy.spatial import cKDTree
    d, _ = cKDTree(m.nodes).query(m.nodes, k=2)
    assert m.n_nodes > 3000 and d[:, 1].min() <= 0.05 * 1.01  # first layer (fan nodes may be closer)
