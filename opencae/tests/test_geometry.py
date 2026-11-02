"""Geometry kernel tests: primitives, operations, IO, checks, references."""
import math

import numpy as np
import pytest

from opencae.geometry.occ_kernel import OccKernel, stl_topology_check
from opencae.geometry.references import find_planar_face, make_ref, resolve_ref
from opencae.project.model import Feature

K = OccKernel()


def regen(tmp_path, *features):
    r = K.regenerate(list(features), tmp_path)
    assert not r.errors, r.errors
    return r


@pytest.mark.parametrize("params,volume", [
    ({"shape": "box", "dx": 10, "dy": 20, "dz": 30}, 6000.0),
    ({"shape": "cylinder", "radius": 5, "height": 10}, math.pi * 25 * 10),
    ({"shape": "sphere", "radius": 3}, 4 / 3 * math.pi * 27),
    ({"shape": "cone", "radius1": 4, "radius2": 2, "height": 9}, math.pi * 9 / 3 * (16 + 8 + 4)),
    ({"shape": "plate", "length": 50, "width": 20, "thickness": 2}, 2000.0),
    ({"shape": "cantilever_beam", "length": 100, "width": 10, "height": 5}, 5000.0),
    ({"shape": "beam", "section": "i_section", "length": 100, "flange_width": 40, "height": 50,
      "flange_thickness": 5, "web_thickness": 4}, 100 * (2 * 40 * 5 + 4 * 40)),
    ({"shape": "beam", "section": "tube", "length": 100, "outer_diameter": 20, "wall_thickness": 2},
     100 * math.pi * (100 - 64)),
])
def test_primitive_volumes(tmp_path, params, volume):
    f = Feature("primitive", params)
    b = regen(tmp_path, f).bodies[f.id]
    assert b.volume == pytest.approx(volume, rel=1e-9)
    assert b.check.valid and b.check.closed and b.check.is_solid


def test_invalid_parameters_reported(tmp_path):
    f = Feature("primitive", {"shape": "box", "dx": -1, "dy": 1, "dz": 1})
    r = K.regenerate([f], tmp_path)
    assert f.id in r.errors and not r.bodies


def test_booleans_fillet_chamfer_extrude_transform(tmp_path):
    a = Feature("primitive", {"shape": "box", "dx": 100, "dy": 50, "dz": 30})
    hole = Feature("primitive", {"shape": "cylinder", "radius": 10, "height": 40, "position": [50, 25, -5]})
    cut = Feature("boolean", {"target": a.id, "tool": hole.id, "op": "subtract"})
    r = regen(tmp_path, a, hole, cut)
    v_cut = r.bodies[a.id].volume
    assert v_cut == pytest.approx(150000 - math.pi * 100 * 30, rel=1e-9)
    assert hole.id not in r.bodies
    fil = Feature("fillet", {"body": a.id, "radius": 3, "edges": [0]})
    assert regen(tmp_path, a, hole, cut, fil).bodies[a.id].volume < v_cut
    ch = Feature("chamfer", {"body": a.id, "distance": 2, "edges": "all"})
    assert regen(tmp_path, a, hole, cut, ch).bodies[a.id].volume < v_cut
    tr = Feature("transform", {"body": a.id, "translate": [10, 0, 0], "rotate_axis": [0, 0, 1], "rotate_angle": 90,
                               "scale": 2.0})
    b = regen(tmp_path, a, tr).bodies[a.id]
    assert b.volume == pytest.approx(150000 * 8, rel=1e-9)
    ex = Feature("extrude", {"profile": "rectangle", "width": 20, "height": 10, "origin": [50, 25, 30],
                             "normal": [0, 0, 1], "distance": 10, "fuse_with": a.id})
    assert regen(tmp_path, a, ex).bodies[a.id].volume == pytest.approx(150000 + 2000, rel=1e-9)
    u1 = Feature("primitive", {"shape": "box", "dx": 10, "dy": 10, "dz": 10})
    u2 = Feature("primitive", {"shape": "box", "dx": 10, "dy": 10, "dz": 10, "position": [5, 0, 0]})
    assert regen(tmp_path, u1, u2, Feature("boolean", {"target": u1.id, "tool": u2.id, "op": "union"})).bodies[
        u1.id].volume == pytest.approx(1500)
    assert regen(tmp_path, u1, u2, Feature("boolean", {"target": u1.id, "tool": u2.id, "op": "intersect"})).bodies[
        u1.id].volume == pytest.approx(500)


def test_disjoint_intersection_fails_cleanly(tmp_path):
    a = Feature("primitive", {"shape": "box", "dx": 1, "dy": 1, "dz": 1})
    b = Feature("primitive", {"shape": "box", "dx": 1, "dy": 1, "dz": 1, "position": [5, 5, 5]})
    op = Feature("boolean", {"target": a.id, "tool": b.id, "op": "intersect"})
    r = K.regenerate([a, b, op], tmp_path)
    assert op.id in r.errors


@pytest.mark.parametrize("fmt", ["step", "iges", "brep"])
def test_exact_export_import_roundtrip(tmp_path, fmt):
    a = Feature("primitive", {"shape": "box", "dx": 100, "dy": 50, "dz": 30})
    c = Feature("primitive", {"shape": "cylinder", "radius": 10, "height": 40, "position": [50, 25, -5]})
    r = regen(tmp_path, a, c, Feature("boolean", {"target": a.id, "tool": c.id, "op": "subtract"}))
    path = tmp_path / f"part.{fmt}"
    K.export([r.bodies[a.id]], path, fmt)
    imp = Feature("import", {"file": str(path)})
    b = regen(tmp_path, imp).bodies[imp.id]
    assert b.kind == "solid" and b.check.valid
    assert b.volume == pytest.approx(r.bodies[a.id].volume, rel=1e-6)


def test_step_metre_project_scaling(tmp_path):
    a = Feature("primitive", {"shape": "box", "dx": 100, "dy": 50, "dz": 30})
    r = regen(tmp_path, a)
    K.export([r.bodies[a.id]], tmp_path / "p.step", "step", length_unit="mm")
    imp = Feature("import", {"file": str(tmp_path / "p.step")})
    b = K.regenerate([imp], tmp_path, length_scale=1e-3).bodies[imp.id]
    assert b.volume == pytest.approx(150000e-9, rel=1e-6)  # m^3


def test_stl_is_not_treated_as_solid(tmp_path):
    a = Feature("primitive", {"shape": "box", "dx": 10, "dy": 10, "dz": 10})
    r = regen(tmp_path, a)
    K.export([r.bodies[a.id]], tmp_path / "b.stl", "stl")
    imp = Feature("import", {"file": str(tmp_path / "b.stl")})
    b = regen(tmp_path, imp).bodies[imp.id]
    assert b.kind == "tessellated" and not b.check.is_solid and b.shape is None
    rec = Feature("import", {"file": str(tmp_path / "b.stl"), "to_solid": True})
    s = regen(tmp_path, rec).bodies[rec.id]
    assert s.kind == "solid" and s.check.valid and s.volume == pytest.approx(1000, rel=1e-9)
    assert len(s.faces) == 6  # coplanar facets merged


def test_open_stl_detected():
    pts = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1.0]])
    tris = np.array([[0, 2, 1], [0, 1, 3], [1, 2, 3]])  # one face missing
    chk = stl_topology_check(pts, tris)
    assert not chk["watertight"] and chk["boundary_edges"] == 3


def test_references_validated_after_regeneration(tmp_path):
    a = Feature("primitive", {"shape": "cantilever_beam", "length": 100, "width": 10, "height": 10})
    r = regen(tmp_path, a)
    body = r.bodies[a.id]
    tip = find_planar_face(body, [1, 0, 0], [100, 0, 0])
    ref = make_ref(body, "face", tip)
    assert resolve_ref(ref, r.bodies).ok
    # unrelated parameter change keeps the face reference valid only if the face is unchanged
    a.params["width"] = 12
    st = resolve_ref(ref, regen(tmp_path, a).bodies)
    assert not st.ok and "re-select" in st.message
    # renumbering is detected and remapped, never guessed
    ref2 = make_ref(body, "face", tip)
    ref2.index = (tip + 1) % 6
    a.params["width"] = 10
    st2 = resolve_ref(ref2, regen(tmp_path, a).bodies)
    assert st2.ok and st2.remapped and st2.index == tip
