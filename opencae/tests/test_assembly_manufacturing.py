"""Assembly (subassemblies, exploded view, sequence, checks, BOM) and manufacturing / validation
(process plan, test matrix, measurement import and comparison, workflow) on the K09-L model."""
import time

import numpy as np
import pytest

from opencae.engineering import assembly as A
from opencae.engineering import calcs
from opencae.engineering import manufacturing as MF
from opencae.engineering.model import EngineeringModel

FOOT = "Foot PETG + TPU pad (x4) — K09L-AF-FOOT"


@pytest.fixture(scope="module")
def model():
    return EngineeringModel()


@pytest.fixture(scope="module")
def fin_tips_model():
    m = EngineeringModel()
    ok, why = m.set_option("landing", "fin_tips")
    assert ok, why
    return m


@pytest.fixture(scope="module")
def results(model):
    return calcs.evaluate_all(model)


@pytest.fixture(scope="module")
def check(model):
    t0 = time.perf_counter()
    r = A.check_assembly(model, clearance_mm=0.5)
    r["_wall"] = time.perf_counter() - t0
    return r


# --------------------------------------------------------------------------- assembly

def test_every_active_component_in_exactly_one_subassembly(model, fin_tips_model):
    for m in (model, fin_tips_model):
        subs = A.subassemblies(m)
        keys = [k for s in subs for k in s["components"]]
        active = {c.key for c in m.active_components()}
        assert len(keys) == len(set(keys)), "a component is in two subassemblies"
        assert set(keys) == active
    ids = {s["id"] for s in A.subassemblies(model)}
    assert {"wing", "fins", "propulsion", "pod", "controls", "wiring", "fasteners"} <= ids
    sub = {k: s["id"] for s in A.subassemblies(model) for k in s["components"]}
    assert sub["Spar tube CF 12x10x560 — Superstition CFT3K12101"] == "wing"
    assert sub[FOOT] == "fins"
    assert sub["Motor SunnySky X3108S KV900 — X3108SK9"] == "propulsion"
    assert sub["Harness motor M1 lower-right CCW"] == "wiring"
    assert sub["SHCS M3x8 18-8 — McMaster 91292A112"] == "fasteners"


def test_exploded_points_are_a_copy(model):
    before = model.points.copy()
    vec = A.explode_vectors(model)
    assert set(vec) == {b.index for b in model.active_bodies()}
    E = A.exploded_points(model, 1.0)
    assert E is not model.points and E.shape == model.points.shape
    assert not np.allclose(E, model.points)
    np.testing.assert_array_equal(model.points, before)  # authoritative geometry unchanged
    # deterministic, and the directions follow the subassembly rules
    vec2 = A.explode_vectors(model)
    assert all(np.array_equal(vec[i], vec2[i]) for i in vec)
    by_name = {}
    for b in model.active_bodies():
        by_name.setdefault(b.component, []).append(b)
    spar = by_name["Spar tube CF 12x10x560 — Superstition CFT3K12101"][0]
    assert vec[spar.index][2] < 0  # spar down
    elevon = by_name["Elevon inboard XPS (L=R) — K09L-AF-ELEVON-IN"][0]
    assert vec[elevon.index][0] > 0  # elevons aft
    for b in by_name["Motor SunnySky X3108S KV900 — X3108SK9"]:
        v = vec[b.index]
        assert v[0] < 0 and np.sign(v[1]) == np.sign(b.centroid[1])  # forward and outward
    pod = by_name["Pod hatch PETG — K09L-AF-POD-HATCH"][0]
    assert vec[pod.index][2] > 0  # avionics up
    E2 = A.exploded_points(model, 2.0)
    i = spar
    np.testing.assert_allclose(E2[i.vo:i.vo + i.vc] - before[i.vo:i.vo + i.vc], np.broadcast_to(2 * vec[i.index], (i.vc, 3)),
                               atol=1e-6)


def test_assembly_steps_order_and_fasteners(model):
    steps = A.assembly_steps(model)
    titles = [s["title"].lower() for s in steps]
    assert "wing" in titles[0] and "prop" in titles[-1]
    for s in steps:
        for k in ("step", "title", "subassembly", "components", "fasteners", "tools", "checks", "tolerances_note"):
            assert k in s
        assert s["tolerances_note"].startswith("guidance")
    # fasteners are matched by geometry: the per-step counts add up to the BOM quantity
    act = {c.key: c for c in model.active_components()}
    for k, c in act.items():
        if A.is_fastener(c):
            used = sum(s["fastener_counts"].get(k, 0) for s in steps)
            assert used == c.qty, (k, used, c.qty)
    motors = next(s for s in steps if "motor" in s["title"].lower())
    assert motors["fastener_counts"]["SHCS M3x8 18-8 — McMaster 91292A112"] == 16
    props = steps[-1]
    assert "Prop locknut M5 — X3108S kit" in props["fasteners"]
    # every active non-fastener, installed component is in exactly one step
    comps = [k for s in steps for k in s["components"]]
    assert len(comps) == len(set(comps))


def test_fin_tips_removes_feet(model, fin_tips_model):
    assert FOOT in {r["key"] for r in A.bill_of_materials(model)}
    assert FOOT not in {r["key"] for r in A.bill_of_materials(fin_tips_model)}
    assert any(FOOT in s["components"] for s in A.assembly_steps(model))
    assert not any(FOOT in s["components"] for s in A.assembly_steps(fin_tips_model))
    assert FOOT in {r["component"] for r in MF.process_plan(model)}
    assert FOOT not in {r["component"] for r in MF.process_plan(fin_tips_model)}
    foot_bodies = set(model.components[FOOT].bodies)
    assert not foot_bodies & set(A.explode_vectors(fin_tips_model))


def test_check_assembly_keys_and_runtime(model, check):
    for k in ("missing", "not_modelled_by_design", "interferences", "expected_contacts", "tight_clearances",
              "unresolved_joints", "incompatible_mounting", "electrical_gaps", "orphan_fasteners", "rules", "stats"):
        assert k in check, k
    assert check["_wall"] < 60.0
    assert check["stats"]["bodies"] == len(model.active_bodies())
    # mass-only / consumable / spare lines are not "missing"
    nm = {r["component"] for r in check["not_modelled_by_design"]}
    assert any("(mass only)" in k for k in nm) and "(spare pack)" in nm
    assert not {r["component"] for r in check["missing"]} & nm
    # fasteners in holes are expected contact, never interference
    for r in check["interferences"]:
        assert not any(A.is_fastener(model.components[k]) for k in r["components"])
        assert r["penetration_mm"] > A.CONTACT_TOL_MM
    assert any(r["rule"] == "fastener" for r in check["expected_contacts"])
    for r in check["tight_clearances"]:
        assert r["min_gap_mm"] <= 0.5  # rounded to 0.1 um
    # catalog mounting: the bolt pattern is MISSING -> unverified; X2814 has no CAD
    st = {(r["item"], r["status"]) for r in check["incompatible_mounting"]}
    assert ("SunnySky X3108S KV900", "unverified") in st
    assert ("SunnySky X2814 V3 KV900", "CAD envelope not updated") in st
    # the scripted linkages have no origin (info), the revolute joints resolve
    uj = {r["joint"]: r for r in check["unresolved_joints"]}
    assert not any(r["severity"] == "error" for r in uj.values())
    # each motor has its harness
    assert not any("motor at" in r["interface"] for r in check["electrical_gaps"])


def test_expected_contact_rules(model):
    c = model.components
    assert A.expected_contact_rule(c["SHCS M3x8 18-8 — McMaster 91292A112"],
                                   c["Motor mount PETG elbow — K09L-PROP-MNT-01"])[0] == "fastener"
    assert A.expected_contact_rule(c["Spar joiner CF rod 9.8x100 — K09L-AF-SPAR-JOINER"],
                                   c["Spar tube CF 12x10x560 — Superstition CFT3K12101"])[0] == "fit"
    assert A.expected_contact_rule(c["Harness motor M1 lower-right CCW"],
                                   c["ESC Holybro Tekko32 F4 4in1 50A — Tekko32 F4 4in1 50A AM32"])[0] == "termination"
    # a wire through structure is NOT expected
    assert A.expected_contact_rule(c["Harness motor M1 lower-right CCW"], c["Fin Depron 6 mm (L=R) — K09L-AF-FIN"]) is None
    assert A.expected_contact_rule(c["Wing core XPS — K09L-AF-WING-CORE"], c["Pod shell PETG — K09L-AF-POD-SHELL"]) is None


def test_bill_of_materials(model):
    bom = A.bill_of_materials(model)
    assert len(bom) == len(model.active_components())
    r = next(x for x in bom if x["key"].startswith("SHCS M3x8"))
    assert r["ext_price_usd"] == pytest.approx(r["unit_price_usd"] * r["qty"])
    assert r["ext_mass_kg"] == pytest.approx(r["unit_mass_kg"] * r["qty"])
    assert {x["price_class"] for x in bom} <= {"supplier_spec", "provisional_estimate", "missing"}
    assert all(x["subassembly"] for x in bom)


# --------------------------------------------------------------------------- manufacturing

def test_process_plan(model):
    pp = MF.process_plan(model)
    keys = {r["component"] for r in pp}
    assert "Wing core XPS — K09L-AF-WING-CORE" in keys
    assert "Motor mount PETG elbow — K09L-PROP-MNT-01" in keys
    assert "Motor SunnySky X3108S KV900 — X3108SK9" not in keys  # purchased
    assert not any("USB extender" in k for k in keys)  # kit part (process keyword corrected)
    for r in pp:
        assert r["time_label"] == "ESTIMATE"
        assert r["time_per_unit"].source == "provisional_estimate"
        assert r["inspection"] and r["tools"]
        if r["process"].startswith("FDM"):
            assert "Z" in r["fdm"]["anisotropy"] and r["fdm"]["orientation"]
    mm = next(r for r in pp if r["component"].startswith("Motor mount"))
    assert mm["volume_cm3_per_unit"] > 0 and mm["time_per_unit"].value > 0.25
    hw = next(r for r in pp if r["component"].startswith("Wing core"))
    assert "hot-wire" in " ".join(hw["tools"]).lower()


def test_test_matrix_covers_requirements(model, results):
    tm = MF.test_matrix(model, results)
    covered = {rid for t in tm for rid in t["requirement_ids"]}
    assert {r.id for r in model.requirements} <= covered
    titles = " ".join(t["title"].lower() for t in tm)
    for w in ("mass", "cg", "thrust stand", "electrical", "structural", "temperature", "endurance", "vibration",
              "wind-tunnel", "control system", "tip-over", "hover", "transition"):
        assert w in titles, w
    for t in tm:
        assert t["measured"] is None and t["status"] == "NOT EVALUATED"
        for k in ("id", "title", "purpose", "requirement_ids", "equipment", "procedure", "data_to_record",
                  "acceptance", "safety", "expected"):
            assert k in t
    t1 = next(t for t in tm if t["id"] == "T01")
    assert t1["expected"]["mass_total"]["class"] == results["mass_total"].source
    crit = next(c for c in t1["acceptance"]["criteria"] if c["metric"] == "mass_total")
    assert crit["threshold"] == pytest.approx(2.6) and crit["op"] == "<="
    # without results there are no expectations
    assert all(t["expected"] is None for t in MF.test_matrix(model))


def test_compare_never_passes_without_measurement(model, results):
    tm = MF.test_matrix(model, results)
    rows = MF.compare(tm, [], results)
    assert rows and all(r["status"] != "PASS" and r["physical_validation"] == "NOT EVALUATED" for r in rows)
    assert any(r["simulation_verification"].startswith("PREDICTED") for r in rows)
    meas = [{"test_id": "T01", "quantity": "mass_total", "value": 2450.0, "unit": "g"},
            {"test_id": "T03", "quantity": "tw_low", "value": 1.5, "unit": "-"},
            {"test_id": "T14", "quantity": "box_fit", "value": None, "unit": "-"},
            {"test_id": "T15", "quantity": "reserve", "value": 400.0, "unit": "kg"}]
    rows = MF.compare(tm, meas, results)
    by = {(r["test_id"], r["quantity"]): r for r in rows}
    r = by[("T01", "mass_total")]
    assert r["status"] == "PASS" and r["measured_class"] == "test_measurement"
    assert r["error_pct"] == pytest.approx((2.45 - results["mass_total"].value) / results["mass_total"].value * 100)
    assert by[("T03", "tw_low")]["status"] == "FAIL"
    assert by[("T03", "tw_low")]["simulation_verification"].startswith("PREDICTED PASS")
    assert by[("T14", "box_fit")]["status"] == "NOT EVALUATED"
    assert by[("T15", "reserve")]["status"] == "UNIT MISMATCH"
    for r in rows:
        if r["status"] in ("PASS", "FAIL"):
            assert r["measured"] is not None


def test_csv_round_trip(tmp_path, model, results):
    tm = MF.test_matrix(model, results)
    rows = MF.measurement_template(tm)
    rows[0].update(value=2.31, unit="kg", date="2026-11-20", operator="QA", notes="with battery, props on")
    p = tmp_path / "meas.csv"
    MF.write_measurements(p, rows)
    back = MF.import_measurements(p)
    assert len(back) == len(rows)
    assert back[0]["value"] == pytest.approx(2.31) and back[0]["operator"] == "QA"
    assert back[0]["notes"] == "with battery, props on" and back[0]["class"] == "test_measurement"
    assert back[1]["value"] is None
    assert [b["test_id"] for b in back] == [r["test_id"] for r in rows]
    bad = tmp_path / "bad.csv"
    bad.write_text("a,b\n1,2\n")
    with pytest.raises(ValueError):
        MF.import_measurements(bad)


def test_fabrication_workflow():
    wf = MF.fabrication_workflow()
    names = [s["stage"].lower() for s in wf]
    assert names[0] == "requirements" and names[-1] == "design iteration"
    ids = [s["id"] for s in wf]
    for i, s in enumerate(wf):
        assert all(ids.index(d) < i for d in s["depends_on"])
        assert s["owner"] and s["checklist"] and s["gate"]
