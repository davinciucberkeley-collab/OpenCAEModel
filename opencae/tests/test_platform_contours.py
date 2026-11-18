"""Contour panel of the platform's analysis page: every field analysis of revision B listed as current and
drawn with scalars, the failure parameter shown, critical-region / mesh toggles, the component jump, and
stale / not-run analyses reported without a plot."""
import pytest

from opencae.engineering import study as ST
from opencae.engineering.graph import CURRENT, NOT_RUN, Engine

pytestmark = pytest.mark.gui


def _app():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _revision_b():
    # the bundled study carries the revision-B solver runs; read it fresh (it may be regenerated)
    st = ST.Study.open(ST.SEED)
    rid = next(r for r in st.ids() if st.variant(r)["name"] == "Revision B")
    st.select(rid)
    return st.engine


@pytest.fixture(scope="module")
def panel():
    app = _app()
    from opencae.ui.platform.contours import ContourPanel
    p = ContourPanel(engine=_revision_b())
    p.resize(1400, 800)
    p.show()
    app.processEvents()
    yield p
    p.close_plotter()
    p.close()
    p.deleteLater()
    app.processEvents()


def _scalars_actor(p):
    a = p.plotter.actors.get("contour")
    assert a is not None
    m = a.GetMapper()
    return a, m


def test_all_five_analyses_listed_current(panel):
    rows = {r["id"]: r for r in panel.entries()}
    assert set(ST.FIELD_NODES) <= set(rows)  # plus one row per component screening contour
    assert all(r in ST.FIELD_NODES or r.startswith("cs:") for r in rows)
    assert all(r["status"] == CURRENT and r["summary"] is not None for r in rows.values())
    assert panel.list.count() == len(rows)
    assert all("current" in panel.list.item(i).text() for i in range(panel.list.count()))


@pytest.mark.parametrize("nid", ST.FIELD_NODES)
def test_each_analysis_draws_a_contour_with_failure_text(panel, nid):
    panel.select(nid)
    _app().processEvents()
    a, m = _scalars_actor(panel)
    ds = m.GetInput()
    assert ds.GetNumberOfPoints() > 100 and m.GetScalarVisibility()
    assert ds.GetPointData().GetScalars() is not None or ds.GetPointData().GetNumberOfArrays() > 0
    s = panel._summary
    assert s["id"] == nid
    txt = panel.info.toPlainText()
    assert "Failure parameter" in txt and s["failure"]["criterion"] in txt
    assert ("Safety factor" in txt) or ("Margin" in txt)
    assert "Load case" in txt and s["solver"]["name"] in txt
    assert panel.plotter.scalar_bars  # quantity + unit scalar bar
    assert s["unit"] in next(iter(panel.plotter.scalar_bars.keys()))
    assert "worst" in panel.plotter.actors  # worst point marker
    assert "type" in panel.mesh_text.text()  # mesh profile shows the record's mesh keys


def test_colormap_is_symmetric_for_cfd(panel):
    panel.select("cfd_cruise")
    _a, m = _scalars_actor(panel)
    lo, hi = m.GetScalarRange()
    assert lo == pytest.approx(-hi)


def test_critical_and_mesh_toggles(panel):
    import numpy as np
    panel.select("fea_spar")
    panel.crit.setChecked(True)
    panel.edges.setChecked(True)
    _app().processEvents()
    a, m = _scalars_actor(panel)
    v = np.asarray(panel._mesh.active_scalars)
    assert np.isnan(v).any() and not np.isnan(v).all()  # outside the critical region greyed out (NaN colour)
    assert a.GetProperty().GetEdgeVisibility()
    panel.crit.setChecked(False)
    panel.edges.setChecked(False)
    _app().processEvents()
    a, m = _scalars_actor(panel)
    assert not np.isnan(np.asarray(panel._mesh.active_scalars)).any()
    assert not a.GetProperty().GetEdgeVisibility()


def test_component_selector_jumps(panel):
    from opencae.ui.platform.contours import NO_ANALYSIS
    e = panel.e
    for nid in ST.FIELD_NODES:
        part = panel.part_of(nid)
        assert part in e.model.components
        assert panel.select_component(part) == nid
        assert panel.list.currentItem().data(0x0100) == nid and panel._summary["id"] == nid
    other = next(k for k in e.model.components if k not in {panel.part_of(n) for n in panel.all_ids()})
    assert panel.select_component(other) is None
    assert NO_ANALYSIS in panel.comp_note.text()


def test_histogram_mesh_keys_drawn_as_bars(panel):
    panel._set_mesh_profile({"type": "C3D10", "edge_length_mm": {"min": 0.4, "max": 3.1},
                             "quality_hist": {"counts": [1, 4, 9, 3], "edges": [0, .25, .5, .75, 1]},
                             "aspect_histogram": [5, 3, 2, 1, 0, 0]})
    names = sorted(c.objectName() for c in panel._hist_canvases)
    assert names == ["hist:aspect_histogram", "hist:quality_hist"]
    assert "edge_length_mm.min" in panel.mesh_text.text()


def test_not_run_analyses_are_not_plotted():
    app = _app()
    from opencae.ui.platform.contours import ContourPanel
    p = ContourPanel(engine=Engine())  # a fresh engine has no solver records
    p.show()
    app.processEvents()
    try:
        rows = p.entries()
        assert all(r["status"] != CURRENT and r["summary"] is None for r in rows)
        assert any(r["status"] == NOT_RUN for r in rows)
        p.select("fea_spar")
        assert p._summary is None
        assert p.plotter is None or "contour" not in p.plotter.actors
        assert "not run" in p.info.toPlainText().lower() or "out of date" in p.info.toPlainText().lower()
    finally:
        p.close_plotter()
        p.close()


def test_platform_window_has_contour_tab(tmp_path):
    app = _app()
    from opencae.ui.platform.window import PlatformWindow
    w = PlatformWindow()
    w.show()
    w.tabs.setCurrentIndex(1)
    pg = w.pages[1]
    pg.views.setCurrentIndex(1)
    app.processEvents()
    assert pg.views.tabText(1) == "Contours" and pg.contours.list.count() == 5
    w.close()
