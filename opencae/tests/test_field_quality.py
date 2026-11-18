"""Mesh-quality profiles of the field analyses, the CFD flow-visualisation data and the CFD setup."""
import json
import math

import numpy as np
import pytest

from opencae.engineering import field_analyses as FA


# ------------------------------------------------------------------------------------------- mesh quality
def _gmsh_box_tets(size=4.0):
    import gmsh
    gmsh.initialize(interruptible=False)
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.model.occ.addBox(0, 0, 0, 20, 10, 5)
        gmsh.model.occ.synchronize()
        gmsh.option.setNumber("Mesh.MeshSizeMax", size)
        gmsh.option.setNumber("Mesh.MeshSizeMin", size / 3)
        gmsh.model.mesh.generate(3)
        tags, xyz, _ = gmsh.model.mesh.getNodes()
        idx = {int(t): i for i, t in enumerate(tags)}
        et, etags, en = gmsh.model.mesh.getElements(3)
        k = [int(t) for t in et].index(4)
        el = np.array([idx[int(t)] for t in en[k]]).reshape(-1, 4)
        ref = np.asarray(gmsh.model.mesh.getElementQualities(etags[k], "minSICN"))
    finally:
        gmsh.finalize()
    return xyz.reshape(-1, 3), el, ref


def test_tet_mesh_quality_matches_gmsh():
    nodes, el, ref = _gmsh_box_tets()
    q = FA.tet_mesh_quality(nodes, el, 4.0)
    assert q["target_size_mm"] == 4.0
    assert "minSICN" in q["quality"]["metric"]
    assert q["quality"]["min"] == pytest.approx(ref.min(), rel=1e-6)
    assert q["quality"]["mean"] == pytest.approx(ref.mean(), rel=1e-6)
    assert q["quality"]["p5"] == pytest.approx(np.percentile(ref, 5), rel=1e-6)
    h = q["quality"]["histogram"]
    assert len(h) == 10 and sum(h) == len(el) and len(q["quality"]["bins"]) == 11
    assert q["poor_elements"] == int((ref < FA.TET_POOR_SICN).sum()) == q["quality"]["poor_elements"]
    e = q["edge_length_mm"]
    assert 0 < e["min"] <= e["mean"] <= e["max"] <= math.sqrt(20 ** 2 + 10 ** 2 + 5 ** 2)
    json.dumps(q)


def test_tet_quality_of_a_regular_and_a_sliver_tet():
    reg = np.array([[1, 1, 1], [1, -1, -1], [-1, 1, -1], [-1, -1, 1]], float)
    q = FA.tet_mesh_quality(reg, np.array([[0, 2, 1, 3]]))  # positive orientation (CalculiX C3D10 order)
    assert q["quality"]["min"] == pytest.approx(1.0, abs=1e-6) and q["poor_elements"] == 0
    q = FA.tet_mesh_quality(reg, np.array([[0, 1, 2, 3]]))  # inverted: signed metric, counted as inverted
    assert q["quality"]["min"] < 0 and q["inverted_elements"] == 1
    sliver = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0], [0.5, 0.5, 1e-3]], float)
    q = FA.tet_mesh_quality(sliver, np.array([[0, 1, 2, 3]]))
    assert q["quality"]["min"] < FA.TET_POOR_SICN and q["poor_elements"] == 1


# ------------------------------------------------------------------------------------------- flow data
def _flow_record():
    rng = np.random.default_rng(1)
    pts = rng.random((30, 3)) * 100
    tri = np.array([[0, 1, 2], [2, 3, 4], [5, 6, 7]])
    streams = [{"points_mm": rng.random((n, 3)) * 500, "speed": rng.random(n) * 15} for n in (5, 150, 17)]
    flow = {"slice": {"points_mm": rng.random((8, 3)) * 300, "tri": np.array([[0, 1, 2], [2, 3, 4], [4, 5, 7]]),
                      "speed": rng.random(8) * 14, "cp": rng.random(8) - 0.5, "y_mm": 250.0},
            "streamlines": streams, "freestream": 12.5, "n_seeds": 3}
    return {"id": "cfd_cruise", "surface": {"points_mm": pts, "tri": tri, "values": rng.random(30)},
            "critical": {"node_mask": np.zeros(30, bool), "threshold": 0.0}, "mesh": {"cells": 1}, "flow": flow}


def test_load_flow_roundtrip(tmp_path):
    rec = _flow_record()
    s = FA.save_field(rec, tmp_path)
    json.dumps(s)
    assert "flow" in s and s["flow"]["streamlines"] == 3 and s["flow"]["slice_y_mm"] == 250.0
    d = np.load(tmp_path / s["field_name"])
    for k in ("slice_points", "slice_tri", "slice_speed", "slice_cp", "stream_points", "stream_offsets",
              "stream_speed", "freestream"):
        assert k in d.files, k
    fl = FA.load_flow(s)
    assert fl is not None and fl["freestream"] == pytest.approx(12.5)
    src = rec["flow"]
    assert np.allclose(fl["slice"]["points_mm"], src["slice"]["points_mm"], atol=1e-3)
    assert np.array_equal(fl["slice"]["tri"], src["slice"]["tri"])
    assert np.allclose(fl["slice"]["cp"], src["slice"]["cp"], atol=1e-6)
    assert [len(x["speed"]) for x in fl["streamlines"]] == [5, 150, 17]
    for a, b in zip(fl["streamlines"], src["streamlines"]):
        assert a["points_mm"].shape == b["points_mm"].shape
        assert np.allclose(a["points_mm"], b["points_mm"], atol=1e-3)
        assert np.allclose(a["speed"], b["speed"], atol=1e-5)
    surf = FA.load_field(s)  # the surface field is unchanged by the extra keys
    assert len(surf["values"]) == 30


def test_load_flow_absent_for_records_without_flow(tmp_path):
    rec = _flow_record()
    rec.pop("flow")
    s = FA.save_field(rec, tmp_path)
    assert "flow" not in s and FA.load_flow(s) is None
    assert FA.load_flow({"field_file": "", "field_name": "missing_0000.npz"}) is None


# ------------------------------------------------------------------------------------------- CFD log parsing
def test_snappy_layer_and_checkmesh_parsing(tmp_path):
    (tmp_path / "log.snappy").write_text(
        "Extruding 1000 out of 2000 faces (50%). Removed extrusion at 0 faces.\n"
        "Extruding 1800 out of 2000 faces (90%). Removed extrusion at 0 faces.\n"
        "patch      faces    layers   overall thickness\n"
        "                             [m]       [%]\n"
        "-----      -----    ------   ---       ---\n"
        "aircraft   2000     2.7      0.0031    81.2\n")
    (tmp_path / "log.checkMesh").write_text(
        "    points:           5000\n    faces:            20000\n    cells:            4000\n"
        "    hexahedra:     3000\n    prisms:        500\n    polyhedra:     500\n"
        "    Max aspect ratio = 9.7 OK.\n    Min volume = 1e-09. Max volume = 0.001.  Total volume = 22.\n"
        "    Mesh non-orthogonality Max: 64.9 average: 9.1\n"
        " ***Max skewness = 7.07, 115 highly skew faces detected which may impair the quality of the results\n"
        "\nFailed 1 mesh checks.\n")
    m = FA._foam_mesh_profile(tmp_path, (4, 5), 3)
    assert m["cells"] == m["elements"] == 4000 and m["nodes"] == 5000
    assert m["layer_coverage_pct"] == pytest.approx(90.0) and m["layers_avg"] == pytest.approx(2.7)
    assert m["layer_thickness_pct"] == pytest.approx(81.2)
    assert m["max_non_orthogonality"] == pytest.approx(64.9) and m["max_skewness"] == pytest.approx(7.07)
    assert not m["checkMesh_ok"] and m["checkMesh_failed_checks"] == 1 and "skewness" in m["checkMesh"]
    assert m["cell_types"]["prisms"] == 500
    assert m["target_size_mm"] == pytest.approx(100 / 32) and m["refinement_levels"]["surface"] == [4, 5]


# ------------------------------------------------------------------------------------------- CFD run
@pytest.mark.openfoam
@pytest.mark.slow
def test_cfd_cruise_layers_history_and_flow(tmp_path):
    from opencae.engineering import revision_b as RB
    from opencae.engineering.graph import Engine
    e = Engine()
    RB.apply(e)
    rec = FA.run_cfd_cruise(e, tmp_path / "cfd", iterations=40, write_every=20, procs=1)
    ms = rec["mesh"]
    assert ms["cells"] > 0 and ms["layers_requested"] == 3 and ms["layer_coverage_pct"] is not None
    assert ms["max_non_orthogonality"] is not None and "coarse" in ms["note"]
    hist = rec["extra"]["history"]
    assert [h[0] for h in hist] == [20.0, 40.0]
    assert rec["solver"]["cl_oscillation_last_25pct"] is not None or len(hist) < 2
    fl = rec["flow"]
    assert len(fl["slice"]["tri"]) > 100 and len(fl["streamlines"]) >= 20
    assert all(len(s["speed"]) <= FA.FLOW_STREAM_MAX_PTS for s in fl["streamlines"])
    s = FA.save_field(rec, tmp_path / "fields")
    back = FA.load_flow({**s, "field_file": str(tmp_path / "fields" / s["field_name"])})
    assert back is not None and len(back["streamlines"]) == len(fl["streamlines"])
