"""Whole-aircraft cruise CFD: the record's secondary surface fields, the slice-plane list of the flow data
(save_field / load_flow) and the wall-field evaluation of _cfd_post on a synthetic patch. The OpenFOAM run itself
is an openfoam-marked slow test."""
import json
import math

import numpy as np
import pytest

from opencae.engineering import field_analyses as FA

SLICES = [("chordwise, centreline y = 0", [0.0, 1.0, 0.0], [0.0, 0.1, 0.0]),
          ("chordwise, y = +250 mm", [0.0, 1.0, 0.0], [0.0, 250.1, 0.0]),
          ("chordwise, y = -250 mm", [0.0, 1.0, 0.0], [0.0, -249.9, 0.0]),
          ("horizontal, wing mid-thickness z = 4.7 mm", [0.0, 0.0, 1.0], [0.0, 0.0, 4.7])]


def _slices(rng):
    out = []
    for k, (name, nrm, org) in enumerate(SLICES):
        n = 6 + k
        item = {"name": name, "normal": nrm, "origin_mm": org, "points_mm": rng.random((n, 3)) * 300,
                "tri": np.array([[0, 1, 2], [2, 3, 4], [3, 4, n - 1]]), "speed": rng.random(n) * 15,
                "cp": rng.random(n) - 0.6}
        if abs(nrm[1]) > 0.5:
            item["y_mm"] = org[1]
        out.append(item)
    return out


def _cfd_record(rng, n=40):
    """A tiny CFD-like field record built with field_record, with the CFD's secondary fields and slice list."""
    pts = rng.random((n, 3)) * 100
    tri = np.array([[i, i + 1, i + 2] for i in range(n - 2)])
    cp = rng.random(n) * 2 - 1.5
    fields = [{"key": k, "quantity": q, "unit": u, "values": rng.random(n) * 10, "cmap": cm, "symmetric": sy}
              for k, q, u, cm, sy in FA.CFD_SURFACE_FIELDS]
    rec = FA.field_record("cfd_cruise", "Aircraft - cruise aerodynamics", "fluid", "wing", pts, tri, cp, "-",
                          "pressure coefficient Cp",
                          critical={"threshold": -1.0, "higher_is_worse": False, "node_mask": cp < -1.0},
                          failure={"value": 0.3, "allowable": 0.59}, solver={}, mesh_info={"cells": 1},
                          load_case="", assumptions=[], evidence="test", fields=fields)
    sl = _slices(rng)
    streams = [{"points_mm": rng.random((m, 3)) * 500, "speed": rng.random(m) * 15} for m in (4, 150, 9)]
    rec["flow"] = {"slices": sl, "slice": sl[0], "streamlines": streams, "freestream": 12.5, "n_seeds": 100}
    return rec


def test_cfd_secondary_fields_saved(tmp_path):
    rec = _cfd_record(np.random.default_rng(3))
    keys = [f[0] for f in FA.CFD_SURFACE_FIELDS]
    assert keys == ["p_static", "u_near_wall", "wall_shear", "y_plus"]
    assert dict((f[0], f[2]) for f in FA.CFD_SURFACE_FIELDS) == {"p_static": "Pa", "u_near_wall": "m/s",
                                                                 "wall_shear": "Pa", "y_plus": "-"}
    s = FA.save_field(rec, tmp_path)
    json.dumps(s)
    assert [f["key"] for f in s["fields"]] == keys and all("values" not in f for f in s["fields"])
    assert all(f["range"][0] <= f["range"][1] for f in s["fields"])
    d = np.load(tmp_path / s["field_name"])
    for k in keys:
        assert "fv_" + k in d.files, k
    back = FA.load_field(s)
    assert set(back["fields"]) == set(keys)
    for f in rec["surface"]["fields"]:
        assert back["fields"][f["key"]].shape == back["values"].shape
        assert np.allclose(back["fields"][f["key"]], f["values"], atol=1e-5)


def test_save_and_load_flow_slice_list(tmp_path):
    rec = _cfd_record(np.random.default_rng(4))
    s = FA.save_field(rec, tmp_path)
    json.dumps(s)
    assert [m["name"] for m in s["flow"]["slices"]] == [x[0] for x in SLICES]
    assert s["flow"]["streamlines"] == 3 and s["flow"]["slice_y_mm"] == pytest.approx(0.1)
    d = np.load(tmp_path / s["field_name"])
    for i in range(len(SLICES)):
        for k in ("points", "tri", "speed", "cp"):
            assert f"slice{i}_{k}" in d.files, (i, k)
    for k in ("slice_points", "slice_tri", "slice_speed", "slice_cp", "slice_y_mm", "slices_meta"):
        assert k in d.files, k  # the older single-slice keys stay
    fl = FA.load_flow(s)
    assert fl is not None and fl["freestream"] == pytest.approx(12.5)
    assert len(fl["slices"]) == len(SLICES)
    for a, b, (name, nrm, org) in zip(fl["slices"], rec["flow"]["slices"], SLICES):
        assert a["name"] == name and a["normal"] == nrm and a["origin_mm"] == pytest.approx(org)
        assert np.allclose(a["points_mm"], b["points_mm"], atol=1e-3) and np.array_equal(a["tri"], b["tri"])
        assert np.allclose(a["speed"], b["speed"], atol=1e-5) and np.allclose(a["cp"], b["cp"], atol=1e-6)
        assert ("y_mm" in a) == (abs(nrm[1]) > 0.5)
    assert fl["slice"]["name"] == SLICES[0][0] and fl["slice"]["y_mm"] == pytest.approx(0.1)
    assert np.allclose(fl["slice"]["points_mm"], fl["slices"][0]["points_mm"])
    assert [len(x["speed"]) for x in fl["streamlines"]] == [4, 150, 9]


def test_load_flow_synthetic_npz(tmp_path):
    """load_flow on a hand-written npz with a slice list (no record, no save_field)."""
    rng = np.random.default_rng(5)
    arrs = {"points": np.zeros((3, 3), np.float32), "tri": np.array([[0, 1, 2]], np.int32),
            "values": np.zeros(3, np.float32), "critical": np.zeros(3, bool),
            "stream_points": rng.random((7, 3)).astype(np.float32), "stream_offsets": np.array([0, 3, 7], np.int32),
            "stream_speed": rng.random(7).astype(np.float32), "freestream": np.float32(12.0)}
    meta = []
    for i, (name, nrm, org) in enumerate(SLICES[:2] + SLICES[3:]):
        arrs[f"slice{i}_points"] = rng.random((5, 3)).astype(np.float32)
        arrs[f"slice{i}_tri"] = np.array([[0, 1, 2], [2, 3, 4]], np.int32)
        arrs[f"slice{i}_speed"] = np.full(5, 10.0 + i, np.float32)
        arrs[f"slice{i}_cp"] = np.full(5, -0.1 * i, np.float32)
        meta.append({"name": name, "normal": nrm, "origin_mm": org})
    arrs["slices_meta"] = np.array(json.dumps(meta))
    f = tmp_path / "cfd_cruise_test.npz"
    np.savez_compressed(f, **arrs)
    fl = FA.load_flow({"field_file": str(f), "field_name": f.name})
    assert [x["name"] for x in fl["slices"]] == [m["name"] for m in meta]
    assert [float(x["speed"][0]) for x in fl["slices"]] == [10.0, 11.0, 12.0]
    assert fl["slices"][2]["normal"] == [0.0, 0.0, 1.0] and "y_mm" not in fl["slices"][2]
    assert fl["slice"]["name"] == meta[0]["name"] and fl["slice"]["y_mm"] == pytest.approx(0.1)
    assert len(fl["streamlines"]) == 2 and fl["freestream"] == pytest.approx(12.0)


def test_load_flow_older_single_slice_file(tmp_path):
    """A file written before the slice list: one chordwise slice, named from its y."""
    rng = np.random.default_rng(6)
    f = tmp_path / "cfd_cruise_old.npz"
    np.savez_compressed(f, points=np.zeros((3, 3), np.float32), tri=np.array([[0, 1, 2]], np.int32),
                        values=np.zeros(3, np.float32), critical=np.zeros(3, bool),
                        slice_points=rng.random((4, 3)).astype(np.float32),
                        slice_tri=np.array([[0, 1, 2], [1, 2, 3]], np.int32),
                        slice_speed=rng.random(4).astype(np.float32), slice_cp=rng.random(4).astype(np.float32),
                        slice_y_mm=np.float32(250.0), stream_points=np.zeros((0, 3), np.float32),
                        stream_offsets=np.array([0], np.int32), stream_speed=np.zeros(0, np.float32),
                        freestream=np.float32(12.5))
    fl = FA.load_flow({"field_file": str(f), "field_name": f.name})
    assert len(fl["slices"]) == 1 and fl["slices"][0]["name"] == "chordwise, y = +250 mm"
    assert fl["slices"][0]["normal"] == [0.0, 1.0, 0.0] and fl["slices"][0]["origin_mm"] == [0.0, 250.0, 0.0]
    assert fl["slice"]["y_mm"] == pytest.approx(250.0) and fl["streamlines"] == []


def test_flow_slice_specs():
    full = FA._flow_slice_specs(False, 0.0047)
    assert len(full) == 4
    ys = [s["origin"][1] for s in full if s["normal"] == [0.0, 1.0, 0.0]]
    assert ys == pytest.approx([0.0, 0.25, -0.25], abs=2e-4) and all(abs(y - round(y, 2)) > 0 for y in ys)
    assert full[-1]["normal"] == [0.0, 0.0, 1.0] and full[-1]["origin"][2] == pytest.approx(0.0047)
    half = FA._flow_slice_specs(True, 0.0047)
    assert [s["origin"][1] for s in half[:-1]] == pytest.approx([0.25], abs=2e-4)


def test_cfd_post_wall_fields_on_a_plate(tmp_path):
    """_cfd_post on a synthetic aircraft patch (one 1 m x 1 m face, normal +z into the 'body'): pressure force,
    wall shear from the wall-adjacent cell and y+ against hand values."""
    import pyvista as pv
    rho, V, p_kin, nut, U, dn = 1.2, 10.0, 2.0, 3e-5, 3.0, 0.01
    face = pv.PolyData(np.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0]], float), np.array([4, 0, 1, 2, 3]))
    for name, data in (("VTK", {"p": [p_kin], "nut": [nut]}), ("VTKnear", {"U": [[U, 0.0, 0.0]]})):
        d = tmp_path / name / "case_10" / "boundary"
        d.mkdir(parents=True)
        f = face.copy()
        for k, v in data.items():
            f.cell_data[k] = np.asarray(v, float)
        f.save(str(d / "aircraft.vtp"))
    wall_cc = np.array([[0.5, 0.5, -dn]])  # the wall-adjacent cell sits on the fluid side
    post = FA._cfd_post(tmp_path, "10", rho, V, [0, 0, 1], [1, 0, 0], 1.0, 0.0, wall_cc)
    q = 0.5 * rho * V * V
    tau_kin = (FA.CFD_NU + nut) * U / dn
    assert post["force_N"] == pytest.approx([rho * tau_kin, 0.0, rho * p_kin], rel=1e-6)
    assert post["cl"] == pytest.approx(rho * p_kin / q) and post["cd"] == pytest.approx(rho * tau_kin / q)
    assert post["cy"] == pytest.approx(0.0, abs=1e-12)
    assert post["cl_by_side"]["y>=0"] == pytest.approx(post["cl"]) and post["cl_by_side"]["y<0"] == 0.0
    fd = post["fields"]
    assert set(fd) == {f[0] for f in FA.CFD_SURFACE_FIELDS}
    assert np.allclose(fd["p_static"], rho * p_kin) and np.allclose(fd["u_near_wall"], U)
    assert np.allclose(fd["wall_shear"], rho * tau_kin)
    assert np.allclose(fd["y_plus"], dn * math.sqrt(tau_kin) / FA.CFD_NU)
    assert np.allclose(post["cp"], p_kin / (0.5 * V * V)) and not post["separated"].any()
    assert post["separated_area_pct"] == 0.0 and post["y_plus_stats"]["max"] == pytest.approx(fd["y_plus"][0])


@pytest.mark.openfoam
@pytest.mark.slow
def test_cfd_cruise_whole_aircraft(tmp_path):
    """A short whole-aircraft run on a coarse mesh: both sides meshed, the wall fields finite, the four slices
    and streamlines on both sides of the centre plane."""
    from opencae.engineering import revision_b as RB
    from opencae.engineering.graph import Engine
    e = Engine()
    RB.apply(e)
    rec = FA.run_cfd_cruise(e, tmp_path / "cfd", iterations=20, write_every=10, level=(2, 3), layers=1)
    ms = rec["mesh"]
    assert ms["model"] == "full" and ms["domain_m"]["y"] == [-2.0, 2.0] and ms["cells"] > 0
    assert ms["body_count"] >= 11 and "coarse" in ms["note"]
    ex = rec["extra"]
    assert ex["model"] == "full" and ex["reference_area_m2"] > 0.3
    assert ex["cl_by_side"]["y>=0"] > 0 and ex["cl_by_side"]["y<0"] > 0
    assert [h[0] for h in ex["history"]] == [10.0, 20.0]
    assert any("temperature" in a for a in rec["assumptions"])
    keys = {f["key"] for f in rec["surface"]["fields"]}
    assert keys == {f[0] for f in FA.CFD_SURFACE_FIELDS}
    for f in rec["surface"]["fields"]:
        assert np.isfinite(f["values"]).all() and len(f["values"]) == len(rec["surface"]["values"]), f["key"]
    fl = rec["flow"]
    assert len(fl["slices"]) == 4 and all(len(s["tri"]) > 50 for s in fl["slices"])
    ys = np.concatenate([s["points_mm"][:, 1] for s in fl["streamlines"]])
    assert (ys > 100).any() and (ys < -100).any()
    assert all(len(s["speed"]) <= FA.FLOW_STREAM_MAX_PTS for s in fl["streamlines"])
    s = FA.save_field(rec, tmp_path / "fields")
    back = FA.load_flow({**s, "field_file": str(tmp_path / "fields" / s["field_name"])})
    assert [x["name"] for x in back["slices"]] == [x["name"] for x in fl["slices"]]
