"""K09-L design study and Design Monitor: variants measured with the same parameters, edits with range
checks, solver records that follow their variant, persistence, and the HTML dashboard report."""
import json
import os

import numpy as np
import pytest

from opencae.engineering import approval as AP
from opencae.engineering import revision_b as RB
from opencae.engineering import study as ST
from opencae.engineering.graph import CURRENT


@pytest.fixture()
def st():
    s = ST.Study()
    s.variants[0].update(name="Revision A")
    b = s.add_variant("Revision B", s.variants[0]["id"])
    RB.apply(s.engine)
    s._store(b["id"])
    return s


def test_variants_are_measured_with_the_same_parameters(st):
    snaps = st.snapshots()
    assert len(snaps) == 2
    for s in snaps.values():
        assert [r["id"] for r in s["requirements"]] == [f"R{i}" for i in range(1, 13)]
        assert [g["id"] for g in s["gates"]] == ["G1", "G2", "G3", "G4", "G5", "G6"]
        assert set(s["metrics"]) == {k for k, *_ in ST.METRICS}
        assert set(s["heavy"]) == {k for k, _ in ST.HEAVY}
    a, b = (snaps[r] for r in st.ids())
    assert a["counts"][AP.FAIL] == 3 and b["counts"][AP.FAIL] == 0


def test_new_variant_edit_diff_and_range_check(st):
    rb = st.ids()[1]
    c = st.add_variant("C wide feet", rb, "outrigger 30 mm")
    st.edit(c["id"], params={"foot_outrigger_mm": 30.0})
    d = dict((k, (x, y)) for k, x, y in st.diff(c["id"], rb))
    assert d == {"foot_outrigger_mm": ("22 mm", "30 mm")}
    s = st.snapshot(c["id"])
    assert s["metrics"]["tipover_deg"]["value"] > st.snapshot(rb)["metrics"]["tipover_deg"]["value"]
    assert next(r for r in s["requirements"] if r["id"] == "R5")["status"] == AP.FAIL  # too tall for the box now
    with pytest.raises(ValueError):
        st.edit(c["id"], params={"foot_outrigger_mm": 80.0})
    with pytest.raises(ValueError):
        st.edit(c["id"], options={"landing": "wheels"})  # incompatible option, with its reason
    # evidence class: kept on a plain edit, changed only when given
    st.edit(c["id"], params={"cd0": 0.04})
    assert st.engine.model.params["cd0"].source == "provisional_estimate"
    st.edit(c["id"], params={"cd0": (0.031, "test_measurement", "wind tunnel run 3")})
    assert st.engine.model.params["cd0"].source == "test_measurement"


def test_switching_variants_restores_geometry_exactly(st):
    ra, rb = st.ids()
    st.select(rb)
    assert st.engine.model.modified_bodies()
    st.select(ra)
    m = st.engine.model
    assert np.array_equal(m.points, m._points0) and not m.modified_bodies()
    assert m.params["throttle_max"].value == 1.0


def test_solver_records_follow_their_variant(st, tmp_path):
    ra, rb = st.ids()
    st.select(rb)
    st.engine.record_heavy("assembly_check", {"interferences": [], "electrical_gaps": []})
    assert st.snapshot(rb)["heavy"]["assembly_check"]["status"] == CURRENT
    assert st.snapshot(ra)["heavy"]["assembly_check"]["status"] != CURRENT
    c = st.add_variant("C", rb)
    st.edit(c["id"], params={"payload": 0.3})  # does not touch the assembly inputs
    assert st.snapshot(c["id"])["heavy"]["assembly_check"]["status"] == CURRENT
    st.edit(c["id"], params={"motor_inboard_mm": 3.0})  # geometry: the assembly check is no longer current
    assert st.snapshot(c["id"])["heavy"]["assembly_check"]["status"] != CURRENT
    # robustness is dropped when its inputs change
    v = st.variant(c["id"])
    st.select(c["id"])
    v["robustness"], v["robustness_hash"] = [{"case": "x", "altitude_loss": 0.1, "ground_contact": False}], \
        st._robust_hash()
    st._snap.clear()
    assert st.snapshot(c["id"])["robustness"]
    st.edit(c["id"], params={"transition_airspeed": 15.0})
    assert "robustness" not in st.variant(c["id"])


def test_study_round_trip(st, tmp_path):
    st.title, st.baseline = "my study", st.ids()[1]
    p = st.save(tmp_path / "s.json")
    s2 = ST.Study.open(p)
    assert s2.title == "my study" and s2.baseline == st.ids()[1]
    assert [v["name"] for v in s2.variants] == ["Revision A", "Revision B"]
    assert json.loads(p.read_text())["format"] == "opencae-k09l-study/1"
    s2.delete_variant(s2.ids()[0])
    assert s2.baseline == s2.ids()[0]
    with pytest.raises(ValueError):
        s2.delete_variant(s2.ids()[0])


def test_dashboard_report(st, tmp_path):
    c = st.add_variant("Variant <C> & co", st.ids()[1])
    st.edit(c["id"], params={"payload": 0.2})
    p = ST.write_dashboard(st, tmp_path / "d.html", renders=False)
    h = p.read_text(encoding="utf-8")
    assert "{{" not in h and "Variant &lt;C&gt; &amp; co" in h
    for r in [f"R{i}" for i in range(1, 13)] + ["G1", "G6"]:
        assert r in h
    assert h.count('<article class="card') == 3 and h.count("class=\"detail\"") == 3
    assert "Transport-box fit" in h and "not flight evidence" in h


def test_bundled_study_opens_with_current_analyses():
    if not ST.SEED.exists():
        pytest.skip("bundled study not generated")
    s = ST.Study.open(ST.SEED)
    names = [v["name"] for v in s.variants]
    assert names[:2] == ["Revision A", "Revision B"]
    for rid in s.ids():
        snap = s.snapshot(rid)
        assert all(h["status"] == CURRENT for h in snap["heavy"].values()), (rid, snap["heavy"])
        assert snap["robustness"], rid
    b = s.snapshot(s.ids()[1])
    assert b["counts"][AP.FAIL] == 0


@pytest.mark.skipif(not os.environ.get("DISPLAY"), reason="needs a display (xvfb-run)")
def test_monitor_window(tmp_path):
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from opencae.ui.platform.monitor import MonitorWindow, VariantEditor
    s = ST.Study()
    s.variants[0].update(name="Revision A")
    p = s.save(tmp_path / "study.json")
    w = MonitorWindow(study_path=p)
    w.show()
    app.processEvents()
    rid = w.study.add_variant("B-like", w.study.ids()[0])["id"]
    w.refresh(full=True)
    w.vlist.setCurrentRow(1)
    dlg = VariantEditor(w.study, rid, w)
    dlg.combos["landing"].setCurrentIndex(dlg.combos["landing"].findData("feet_b"))
    dlg.spins["foot_outrigger_mm"][0].setValue(25.0)
    dlg._ok()
    ch = dlg.result_changes
    assert ch["options"] == {"landing": "feet_b"} and ch["params"] == {"foot_outrigger_mm": 25.0}
    w.study.edit(rid, ch["options"], ch["params"])
    w.refresh(full=True)
    assert w.table.columnCount() == 3 and w.table.rowCount() > 40
    out = w.export_report(tmp_path / "dash.html")
    assert out and out.exists() and "B-like" in out.read_text(encoding="utf-8")
    w.close()
