"""Tilt-wing VTOL example: inputs taken from the Tilt-Wing Designer are used consistently (geometry,
load collection, beam reference), and the session no longer leaks temporary folders."""
import math
from pathlib import Path

import numpy as np
import pytest

from opencae.examples import tiltwing as T
from opencae.examples import tiltwing_geometry as TG


def test_designer_geometry_and_masses():
    d = T.designer()
    p, r = d["p"], d["r"]
    # designer equations G1-G3 at the paper's defaults
    S = p["span"] ** 2 / p["AR"]
    cr = 2 * S / (p["span"] * (1 + p["taper"]))
    assert r["S"] == pytest.approx(S) and r["cr"] == pytest.approx(cr)
    assert r["MAC"] == pytest.approx(2 / 3 * cr * (1 + p["taper"] + p["taper"] ** 2) / (1 + p["taper"]))
    assert r["m"] == pytest.approx((r["mStruct"] + p["elec"] + p["payload"]) / (1 - p["fProp"]))


def test_load_collection_conserves_the_designer_loads():
    d = T.designer()
    bins = T.wing_bins(d)
    for case, b in bins.items():
        F = np.asarray(d["fea"]["cases"][case]["F"])
        assert sum(b["segments"]) + sum(b["rings"].values()) == pytest.approx(F[1:].sum(), rel=1e-12)
        T_root = d["fea"]["cases"][case]["rootT"]
        assert sum(b["seg_torque"]) == pytest.approx(T_root, abs=1e-9)


def test_beam_reference_reproduces_the_designer_root_moment():
    d = T.designer()
    bins = T.wing_bins(d)
    for case in ("hover", "landing"):
        ref = T.beam_reference(d, bins, case)
        # the collected loads move forces by at most half a segment: root moment within a few percent
        assert ref["rootM_Nm"] == pytest.approx(d["fea"]["cases"][case]["rootM"], rel=0.06)
        assert ref["I_mm4"] == pytest.approx(math.pi / 64 * (20 ** 4 - 17 ** 4))


def test_geometry_is_valid_and_closed():
    from OCP.BRepCheck import BRepCheck_Analyzer
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps
    d = T.designer()
    af, g = TG.airframe_half(d)
    assert BRepCheck_Analyzer(af).IsValid()
    gp = GProp_GProps()
    BRepGProp.VolumeProperties_s(af, gp)
    assert gp.Mass() > 0
    assert g["span"] == pytest.approx(2000.0) and g["rotor_z"] == [400.0, 1000.0]
    parts = TG.spar_model(d)
    assert [n for n, _ in parts][:3] == ["Spar segment 1", "Spar segment 2", "Spar segment 3"]
    br, info = TG.bracket(d)
    assert BRepCheck_Analyzer(br).IsValid() and info["Dh"] == d["bracket"]["cruise"]["Dh"]


def test_unsaved_session_folder_is_removed(tmp_path):
    from opencae.app.session import Session
    s = Session()
    s.new("x")
    scratch = s.data_dir
    assert scratch.exists()
    s.save(tmp_path / "x.ocae")
    assert not scratch.exists() and (tmp_path / "x_data").exists()
    s.new("y")
    d2 = s.data_dir
    s.new("z")
    assert not d2.exists()


def test_bundled_example_opens_with_results_if_built():
    src = T.DEFAULT_OUT / f"{T.NAME}.ocae"
    if not src.exists():
        pytest.skip("example not built (python -m opencae.examples.tiltwing)")
    from opencae.app.session import Session
    s = Session()
    s.open(src)
    names = [st.name for st in s.project.studies]
    assert len(names) == 10
    for st in s.project.studies:
        assert st.results.completed, st.name
        s.set_active_study(st.id)
        assert s.results is not None, st.name
