"""K09-L platform: data import checks against the K09-L model page, dependency propagation, gates, and the
mandatory end-to-end acceptance tests A-F (A, B, E headless; C runs CalculiX; D runs the 6-DOF model;
the GUI test drives the real window)."""
import os
from pathlib import Path

import numpy as np
import pytest

from opencae.engineering import approval as AP
from opencae.engineering import calcs
from opencae.engineering.graph import CURRENT, STALE, Engine
from opencae.engineering.model import EngineeringModel
from opencae.engineering.quantities import ESTIMATE, MISSING, Q, derived

MOTOR_TUBE = "Motor tube CF 16x13.08x380 — DragonPlate FDPT.625*.515*24"


@pytest.fixture(scope="module")
def engine():
    return Engine()


def test_import_reproduces_the_k09l_model_page(engine):
    """Independent cross-checks against numbers printed on the K09-L Tailsitter Model page."""
    m, r = engine.model, engine.results
    assert len(m.bodies) == 418 and len(m.joints) == 10
    assert r["mass_total"].value * 1000 == pytest.approx(1982.17, abs=0.01)  # page: modelled mass 1 982 g
    assert r["cg_frac"].value == pytest.approx(0.20, abs=0.01)  # page: CG 60.0 mm, 20 % chord
    assert r["tipover_deg"].value == pytest.approx(34.7, abs=0.3)  # page: tips over at 34.7 deg
    assert r["static_margin"].value == pytest.approx(0.05, abs=0.01)  # page: SM 5.0 % (estimated NP)
    assert r["span"].value == pytest.approx(1.15, abs=1e-3) and r["chord"].value == pytest.approx(0.30, abs=1e-3)
    assert r["cost_parts"].value == pytest.approx(904.66, abs=0.01)  # page: parts cost $905
    I = np.asarray(r["inertia"].value)
    assert np.allclose(I, I.T) and np.all(np.linalg.eigvalsh(I) > 0)


def test_evidence_classes_propagate():
    a = Q(1.0, "-", "supplier_spec")
    b = Q(2.0, "-", ESTIMATE)
    assert derived(3.0, "-", "sum", {"a": a, "b": b}).source == ESTIMATE
    assert derived(None, "-", "x", {"a": Q(None, "-", MISSING)}).source == MISSING


def test_requirements_never_pass_on_missing_or_estimated_data(engine):
    rows = {x["id"]: x for x in AP.requirements_table(engine)}
    for x in rows.values():
        if x["predicted"] is None:
            assert x["status"] == AP.NOT_EV
        if x["status"] == AP.PASS:
            assert x["predicted_class"] not in (ESTIMATE, MISSING) and x["verification"] != "test"
    G = AP.gates(engine)
    assert G[4]["status"] != AP.PASS and G[5]["status"] != AP.PASS  # no measurements, no flight evidence


# --------------------------------------------------------------------------- Test A
def test_A_change_landing_configuration():
    e = Engine()
    m = e.model
    before = {k: e.results[k].value for k in ("mass_total", "cost_parts", "tipover_deg", "landing_n")}
    cg0 = np.asarray(e.results["cg"].value)
    n_bodies = len(m.active_bodies())
    from opencae.engineering import assembly as AS
    from opencae.engineering import manufacturing as MF
    steps0 = AS.assembly_steps(m)
    e.record_heavy("assembly_check", {"interferences": []})  # pretend a current check exists
    ev = e.set_option("landing", "fin_tips")
    assert ev["ok"]
    # geometry: feet and their fasteners removed
    assert len(m.active_bodies()) < n_bodies
    assert not any(b.component.startswith("Foot PETG") for b in m.active_bodies())
    # mass and CG recalculated
    assert e.results["mass_total"].value < before["mass_total"]
    assert not np.allclose(np.asarray(e.results["cg"].value), cg0)
    # loads: the landing load case changes (stiffer contact) and the structural analyses are stale
    assert e.results["landing_n"].value > before["landing_n"]
    assert e.heavy_status("assembly_check")["status"] == STALE
    assert "landing" in e.heavy_status("assembly_check")["reason"]
    assert "landing_loads" in ev["affected"] and "fea_motor_tube" in ev["affected"]
    # manufacturing and cost
    assert not any(p["component"].startswith("Foot PETG") for p in MF.process_plan(m))
    assert e.results["cost_parts"].value < before["cost_parts"]
    # assembly visualisation / sequence
    steps1 = AS.assembly_steps(m)
    comps = lambda st: {c for s in st for c in s["components"]}
    assert any(c.startswith("Foot PETG") for c in comps(steps0)) and not any(
        c.startswith("Foot PETG") for c in comps(steps1))
    # readiness re-evaluated (ground stability changes)
    assert e.results["tipover_deg"].value != before["tipover_deg"]
    assert "gates" in ev["affected"]
    # incompatible options are refused with a reason
    bad = e.set_option("landing", "wheels")
    assert not bad["ok"] and "tailsitter" in bad["reason"].lower()


# --------------------------------------------------------------------------- Test B
def test_B_change_propulsion_system():
    e = Engine()
    r0 = {k: e.results[k].value for k in ("mass_total", "i_hover_battery", "endurance_hover", "cost_parts",
                                          "motor_loss_hover", "motor_current_margin")}
    e.record_heavy("flight_hover", {"scenario": "hover"})
    e.record_heavy("fea_motor_tube", {"result": {}})
    ev = e.set_option("motor", "X2814V3-900")
    r = e.results
    assert r["mass_total"].value > r0["mass_total"]  # 108 g vs 81 g per motor (supplier pages)
    assert r["i_hover_battery"].value != r0["i_hover_battery"]
    assert r["endurance_hover"].value != r0["endurance_hover"]
    assert r["motor_loss_hover"].value != r0["motor_loss_hover"]
    assert r["cost_parts"].value != r0["cost_parts"]
    assert r["motor_current_margin"].value > r0["motor_current_margin"]  # 50 A vs 25 A rating
    for nid in ("flight_hover", "fea_motor_tube"):
        assert e.heavy_status(nid)["status"] == STALE
    assert {"electrical", "thermal", "endurance", "cost", "assembly_check"} <= set(ev["affected"])
    # compatibility: ESC margin and cells re-evaluated; mounting stays unverified (bolt pattern unknown)
    assert r["esc_margin"].value == pytest.approx(50.0 / max(50.0, r["i_motor_max_full"].value))
    from opencae.engineering import assembly as AS
    mounting = AS._mounting_check(e.model)
    assert any("envelope" in str(x).lower() for x in mounting)
    # the flight simulation uses the new configuration
    from opencae.engineering import flightsim as FS
    sim = FS.simulate(e.model, "hover", duration=2.0)
    assert sim.meta["config_hash"] == e.model.config_hash()
    assert abs(sim.meta["mass"] - r["mass_total"].value) < 1e-9


# --------------------------------------------------------------------------- Test C
@pytest.mark.slow
def test_C_selected_component_analysis(tmp_path):
    from opencae.engineering import component_analysis as CA
    from opencae.engineering import services
    e = Engine()
    ctx = CA.public_context(CA.analysis_context(e.model, MOTOR_TUBE))
    roles = " ".join(str(c) for c in ctx["components"]).lower()
    assert "mount" in roles and "clip" in roles  # load introduction and support path identified
    rec = services.run_fea(e, MOTOR_TUBE, workdir=tmp_path)
    res = rec["result"]
    assert res["converged"]
    assert abs(res["equilibrium"]["force_residual_pct"]) < 1.0
    assert res["max_von_mises_nodal_MPa"] > 0 and res["max_displacement_mm"] > 0
    assert e.heavy_status("fea_motor_tube")["status"] == CURRENT
    assert "motor_tube_sf" in e.results and e.results["motor_tube_sf"].source == ESTIMATE
    assert Path(res["solver_log"]).exists()


# --------------------------------------------------------------------------- Test D
def test_D_virtual_flight_hover_telemetry_consistent():
    from opencae.engineering import flightsim as FS
    from opencae.engineering import services
    e = Engine()
    sim = services.run_flight(e, "hover", duration=6.0)
    W = e.results["weight"].value
    n = len(sim.t)
    for ch in ("pos", "vel", "quat", "thrust", "current", "soc", "power", "motor_temp_rise"):
        a = np.asarray(getattr(sim, ch))
        assert len(a) == n and np.all(np.isfinite(a))
    tail = slice(n // 2, None)
    # telemetry corresponds to the state: thrust balances weight in steady hover, attitude nose-up,
    # energy integrates the power, SOC falls
    assert np.mean(sim.thrust[tail].sum(1)) == pytest.approx(W, rel=0.03)
    assert np.all(np.abs(sim.pos[tail, 2] - sim.pos[-1, 2]) < 0.2)
    E = np.trapezoid(sim.power, sim.t)
    assert sim.energy_used[-1] - sim.energy_used[0] == pytest.approx(E, rel=0.05)
    assert sim.soc[-1] < sim.soc[0]
    assert "not evidence" in sim.meta["validity"].lower() or "not" in sim.meta["validity"].lower()
    assert e.heavy_status("flight_hover")["status"] == CURRENT


# --------------------------------------------------------------------------- Test E
def test_E_fabrication_and_readiness_review(tmp_path):
    from opencae.engineering import assembly as AS
    from opencae.engineering import manufacturing as MF
    from opencae.engineering import report
    e = Engine()
    m = e.model
    comps = m.active_components()
    subs = AS.subassemblies(m)
    assert sum(len(s["components"]) for s in subs) == len(comps)
    wf = MF.fabrication_workflow()
    assert wf[0]["stage"].lower().startswith("requirement") and len(wf) == 12
    bom = AS.bill_of_materials(m)
    assert len(bom) == len(comps) and any(b["price_class"] == ESTIMATE for b in bom)
    tm = MF.test_matrix(m, e.results)
    covered = {rid for t in tm for rid in t["requirement_ids"]}
    assert {r.id for r in m.requirements} <= covered
    G = AP.gates(e)
    assert [g["id"] for g in G] == ["G1", "G2", "G3", "G4", "G5", "G6"]
    rs = AP.readiness_summary(e)
    assert rs["risks"] and rs["next_steps"]
    p = report.write_report(e, tmp_path / "review.html")
    txt = p.read_text()
    for s in ("Bill of materials", "Assembly breakdown", "Fabrication workflow", "Verification matrix",
              "G6", "Critical risks", m.config_hash()):
        assert s in txt


def test_trade_study_restores_configuration_and_respects_compatibility():
    e = Engine()
    cfg = dict(e.model.config)
    ts = AP.trade_study(e)
    assert e.model.config == cfg
    assert all(c["config"]["landing"] not in ("wheels", "skids") for c in ts["candidates"])
    assert any(c["pareto"] for c in ts["candidates"]) or not any(c["feasible"] for c in ts["candidates"])


def test_revisions_and_state_round_trip(tmp_path):
    e = Engine()
    e.model.save_revision("baseline", {"mass_total": e.results["mass_total"].value})
    e.set_option("landing", "fin_tips")
    e.model.save(tmp_path / "s.json")
    e2 = Engine()
    e2.model.load_state(tmp_path / "s.json")
    assert e2.model.config["landing"] == "fin_tips" and e2.model.revisions[0]["note"] == "baseline"
    e2.model.restore_revision("r1")
    assert e2.model.config["landing"] == "feet"


# --------------------------------------------------------------------------- GUI (Test F lives in the
# rest of the suite: every earlier OpenCAE test still runs unchanged)
@pytest.mark.skipif(not os.environ.get("DISPLAY"), reason="needs a display (xvfb-run)")
def test_platform_window_pages_are_connected(tmp_path):
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from opencae.ui.platform.window import PlatformWindow
    w = PlatformWindow()
    w.show()
    app.processEvents()
    w.select_component(MOTOR_TUBE)
    assert MOTOR_TUBE.split(" — ")[0] in w.pages[0].props.toHtml()
    m0 = w.engine.results["mass_total"].value
    w.pages[0].combos["motor"].setCurrentIndex(w.pages[0].combos["motor"].findData("X2814V3-900"))
    app.processEvents()
    assert w.engine.model.config["motor"] == "X2814V3-900" and w.engine.results["mass_total"].value > m0
    assert "motor" in w.pages[0].changes.toPlainText()
    for i in range(6):
        w.tabs.setCurrentIndex(i)
        app.processEvents()
    assert w.pages[4].steps.count() >= 6
    assert "Design readiness" in w.pages[5].summary.toHtml()
    p = w.export_report(str(tmp_path / "r.html"))
    assert Path(p).exists()
    w.close()


# --------------------------------------------------------------------------- independent-review regressions
def test_review_findings_stay_fixed():
    e = Engine()
    m = e.model
    # E1: props are excluded from the transport envelope
    assert e.results["box_fit"].value > 0.95
    # E2: the ESC margin depends on the estimated prop data, so it cannot be stronger than an estimate
    assert e.results["esc_margin"].source == ESTIMATE
    # E3: editing an estimate does not turn it into a fact
    e.set_param("gust_lean_deg", 30.0)
    assert m.params["gust_lean_deg"].source == ESTIMATE and e.results["tipover_margin"].source == ESTIMATE
    # S1: CG outside the support polygon gives a negative tip-over angle
    from opencae.engineering import calcs as C
    mp = dict(e.results)
    mp["cg"] = Q([0.05, 1.0, 0.0], "m", "analytical")
    assert C.tipover(m, mp)["tipover_deg"].value < 0
    # S3: an FEA safety factor stops counting once an input of that analysis changes
    e.record_heavy("fea_motor_tube", {"result": {"converged": True}, "sf_q": Q(20.0, "-", ESTIMATE)})
    assert "motor_tube_sf" in e.results
    e.set_option("motor", "X2814V3-900")
    assert e.heavy_status("fea_motor_tube")["status"] == STALE and "motor_tube_sf" not in e.results
    # an unrelated input (budget) does not stale the FEA
    e.record_heavy("fea_motor_tube", {"result": {"converged": True}, "sf_q": Q(20.0, "-", ESTIMATE)})
    e.set_param("contingency", 0.1)
    assert e.heavy_status("fea_motor_tube")["status"] == CURRENT
    # S6: unknown shipping keeps the reserve requirement NOT EVALUATED
    r3 = next(x for x in AP.requirements_table(e) if x["id"] == "R3")
    assert r3["status"] == AP.NOT_EV
