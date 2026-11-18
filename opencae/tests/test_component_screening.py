"""Component screening: every component gets a verdict (FEA of made parts, rating checks of the rest)."""
from __future__ import annotations

import numpy as np
import pytest

from opencae.engineering import approval as AP
from opencae.engineering import component_screening as CS
from opencae.engineering import field_analyses as FA
from opencae.engineering.graph import CURRENT, Engine

STATUSES = {CS.OK, CS.WATCH, CS.FAIL, CS.NEV}


def _rev_b() -> Engine:
    e = Engine()
    for k, v in (("landing", "feet_b"), ("mount", "inboard_b"), ("mount_material", "asa")):
        e.set_option(k, v)
    return e


@pytest.fixture(scope="module")
def screened(tmp_path_factory):
    """Revision-B screening without FE runs (only=[]): every component, analytical / rating / fastener checks."""
    e = _rev_b()
    d = tmp_path_factory.mktemp("cs")
    return e, CS.run_component_screening(e, d / "runs", d / "fields", only=[])


def test_every_component_has_a_verdict(screened):
    e, res = screened
    comps = res["components"]
    assert set(comps) == {c.key for c in e.model.active_components()}
    for k, c in comps.items():
        assert c["status"] in STATUSES, k
        assert {"title", "discipline", "part", "checks", "field", "status"} <= set(c), k
        prim = [x for x in c["checks"] if x.get("primary")]
        assert prim, f"{k}: no primary check"
        for x in c["checks"]:
            assert x["status"] in STATUSES and x.get("text"), (k, x)
        assert any(x["kind"] == "mass" for x in c["checks"]), k
    sm = res["summary"]
    assert sm["components"] == len(comps) and sum(sm["counts"].values()) == len(comps)
    assert res["load_cases"]["factors"]["n_land"] == pytest.approx(e.results["landing_n"].value)


def test_ratings_are_never_invented(screened):
    e, res = screened
    comps = res["components"]
    xt60 = next(c for k, c in comps.items() if "XT60" in k and k.startswith("Connector"))
    assert xt60["status"] == CS.NEV and "rating missing" in xt60["checks"][-1]["text"]
    mot = next(c for k, c in comps.items() if k.startswith("Motor SunnySky"))
    cur = next(x for x in mot["checks"] if "current" in x["name"])
    assert cur["rating"] == pytest.approx(e.model.option("motor").q("imax").value) and "supplier" in cur["source"]
    servo = next(c for k, c in comps.items() if k.startswith("Servo KST"))
    assert servo["status"] == CS.NEV and "rating missing" in servo["checks"][-1]["text"]
    assert "N mm" in servo["checks"][-1]["text"]  # the demand a rating has to cover is stated


def test_wire_ampacity_and_fasteners(screened):
    e, res = screened
    comps = res["components"]
    pig = next(c for k, c in comps.items() if k.startswith("Harness battery pigtail 12AWG"))
    amp = next(x for x in pig["checks"] if x["kind"] == "ampacity")
    assert amp["rating"] == CS.AWG_CHASSIS_A[12] and "powerstream" in amp["source"].lower()
    i_burst = e.results["i_battery_max"].value
    assert amp["status"] == (CS.WATCH if i_burst > 41.0 else CS.OK)
    screws = next(c for k, c in comps.items() if k.startswith("SHCS M3x8"))
    fx = next(x for x in screws["checks"] if x["kind"] == "fastener")
    assert fx["status"] in (CS.OK, CS.WATCH, CS.FAIL) and fx["sf"] > 0 and "A2-70" in fx["material"]["name"]
    assert fx["material"]["source"] == CS.EST  # the vendors' property class is not read: estimate


def test_materials_carry_sources():
    mats = CS.materials(_rev_b().model)
    for k, m in mats.items():
        assert m["source"] in (CS.SUP, CS.EST), k
        assert m.get("ref"), k
        for v in m.get("values", []):
            assert v["source"] in (CS.SUP, CS.EST) and v["ref"], (k, v)
    assert mats["mount"]["name"].startswith("ASA")  # revision B prints the mounts in ASA
    assert mats["xps"]["source"] == CS.EST and mats["petg"]["source"] == CS.SUP


def test_plate_outline_and_contact_distance():
    m = _rev_b().model
    plate = next(b for b in m.active_bodies() if b.component.startswith("Adapter plate"))
    d = CS._plate_outline(*m.body_mesh(plate))
    assert d is not None and d["thickness"] == pytest.approx(1.5, abs=0.01)
    core = next(b for b in m.active_bodies() if b.component.startswith("Wing core"))
    assert CS._plate_outline(*m.body_mesh(core)) is None
    P = np.array([[0.0, 0, 0], [1, 0, 0], [0, 1, 0]])
    T = np.array([[0, 1, 2]])
    dist, _ = CS._surface_distance(P, T, np.array([[0.2, 0.2, 0.3], [2.0, 0.0, 0.0]]), 2.0, False)
    assert dist == pytest.approx([0.3, 1.0])


def test_g3_criterion_maps_statuses():
    e = Engine()
    c = AP.component_screening_criterion(e)
    assert c["status"] == AP.NOT_EV and c["text"] == AP.SCREENING_TEXT

    def rec(statuses):
        return {"components": {f"part {i}": {"status": s} for i, s in enumerate(statuses)},
                "summary": {"solver_failures": []}}
    e.record_heavy("component_screening", rec([CS.OK, CS.OK]))
    assert e.heavy_status("component_screening")["status"] == CURRENT
    assert AP.component_screening_criterion(e)["status"] == AP.COND  # estimates: never a plain PASS
    e.record_heavy("component_screening", rec([CS.OK, CS.WATCH, CS.NEV]))
    c = AP.component_screening_criterion(e)
    assert c["status"] == AP.COND and "1 watch" in c["why"] and "1 not evaluated" in c["why"]
    e.record_heavy("component_screening", rec([CS.OK, CS.FAIL]))
    c = AP.component_screening_criterion(e)
    assert c["status"] == AP.FAIL and "part 1" in c["why"]
    g3 = next(g for g in AP.gates(e) if g["id"] == "G3")
    assert any(x["text"] == AP.SCREENING_TEXT and x["status"] == AP.FAIL for x in g3["criteria"])
    assert g3["status"] == AP.FAIL
    e.set_option("landing", "feet_b")  # an input of the screening changes: no longer current
    assert AP.component_screening_criterion(e)["status"] == AP.NOT_EV


@pytest.mark.solver
def test_fea_screening_of_small_parts(tmp_path):
    """Two small parts through CalculiX: the PETG control horn under the pushrod force and its G10 backplate
    loaded by the horn run's screw reactions (load-path chaining)."""
    e = _rev_b()
    res = CS.run_component_screening(e, tmp_path / "runs", tmp_path / "fields",
                                     only=["control_horn", "control_horn_backplate"])
    horn = next(c for k, c in res["components"].items() if k.startswith("Control horn"))
    fea = [x for x in horn["checks"] if x["kind"] == "fea"]
    assert len(fea) == 2 and not res["summary"]["solver_failures"]
    for x in fea:
        assert x["status"] in (CS.OK, CS.WATCH, CS.FAIL) and x["sf"] > 0 and x["value"] > 0
        assert x["mesh"]["elements"] > 100 and x["mesh"]["poor_pct"] <= CS.POOR_LIMIT_PCT
        assert all(v["equilibrium_residual"] < 1e-3 for v in x["cases"].values())
        assert x["material"]["source"] == CS.SUP and len(x["worst_point_mm"]) == 3
    assert horn["field"] is not None and horn["field"]["id"].startswith("cs_control_horn")
    arr = FA.load_field(horn["field"])
    assert {"disp_mag", "strain_eq", "s_max_principal"} <= set(arr["fields"])
    assert arr["disp"] is not None and arr["disp"].shape == (len(arr["points_mm"]), 3)
    assert float(np.abs(arr["disp"]).max()) > 0
    back = next(x for x in fea if "backplate" in x["name"])
    assert back["value"] > 0  # the chained screw reactions reached the backplate
    assert {f["key"] for f in horn["field"]["fields"]} >= {"disp_mag", "strain_eq", "s_max_principal"}
