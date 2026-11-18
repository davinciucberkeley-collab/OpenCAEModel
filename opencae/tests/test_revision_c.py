"""K09-L revision C (recommended): applied on top of revision B through the engine, its changes listed with the
numbers the engine computes, no status created by it, the measurement path that closes conditional requirements,
and the website recommendation data (opencae.web.data_recs) on a study holding revisions B and C."""
import csv
import json
import math

import pytest

from opencae.engineering import approval as AP
from opencae.engineering import manufacturing as MF
from opencae.engineering import revision_b as RB
from opencae.engineering import revision_c as RC
from opencae.engineering import study as ST
from opencae.engineering.graph import Engine
from opencae.engineering.quantities import ESTIMATE, SUPPLIER, USER

TEMPLATE_B = RC.DATA / "measurements_template_revB.csv"


@pytest.fixture(scope="module")
def engines():
    return RC.engines()


def _status(e):
    return {r["id"]: r["status"] for r in AP.requirements_table(e)}


def test_revision_c_applies_cleanly_on_top_of_b(engines):
    eb, ec = engines
    mb, mc = eb.model, ec.model
    assert mc.config == mb.config  # no catalog / geometry change: same hardware as B
    changed = {k for k in mc.params if mc.params[k].value != mb.params[k].value}
    expected = {"cla_factor", "cl_max"} | {k for k, v in RC.TRANSITION.items() if mb.params[k].value != v}
    if RC.THROTTLE_MAX != RB.THROTTLE_MAX:
        expected.add("throttle_max")
    assert changed == expected
    cal = RC.cfd_calibration(mc)
    assert mc.params["cla_factor"].value == pytest.approx(cal["cla_factor"], abs=1e-4)
    assert mc.params["cla_factor"].value == pytest.approx(0.5525, abs=1e-3)
    assert mc.params["cl_max"].value == pytest.approx(0.491, abs=1e-3)
    # CFD-calibrated inputs are estimates with an uncertainty and a reference to the CFD record
    for p in ("cla_factor", "cl_max"):
        q = mc.params[p]
        assert q.source == ESTIMATE and q.uncertainty == RC.CFD_UNCERTAINTY and RC.CFD_B["time"] in q.ref
    for k in RC.TRANSITION:
        assert mc.params[k].source == USER and "C2" in mc.params[k].ref
    # idempotent, and a fresh engine gets revision B first
    before = {k: q.value for k, q in mc.params.items()}
    RC.apply(ec)
    assert {k: q.value for k, q in mc.params.items()} == before
    e = Engine()
    RC.apply(e)
    assert RC.is_revision_b(e.model) and e.model.params["cla_factor"].value == before["cla_factor"]


def test_changes_are_listed_with_their_inputs(engines):
    _, ec = engines
    ids = [c["id"] for c in RC.CHANGES]
    assert ids[:2] == ["C1", "C2"] and len(ids) == len(set(ids))
    metric_keys = {k for k, *_ in ST.METRICS}
    for c in RC.CHANGES:
        for k in ("id", "title", "requirement", "analysis", "what", "why", "evidence", "params", "effects", "analyses"):
            assert c.get(k) is not None, (c["id"], k)
        assert any(ch.isdigit() for ch in c["why"]), c["id"]  # the why carries numbers
        for p in c["params"]:
            assert p in ec.model.params
        for key, *_ in c["effects"]:
            assert key in ec.results or key in metric_keys, key
        for nid in c["analyses"]:
            assert nid in ec.heavy
    assert {n["id"] for n in RC.NOT_APPLIED} >= {"N1", "N2", "N5"}
    assert all(n["why_not"] for n in RC.NOT_APPLIED)


def test_comparison_is_computed_by_the_engine(engines):
    eb, ec = engines
    rows = RC.comparison(eb, ec)
    rb = {x["id"]: x for x in AP.requirements_table(eb)}
    rc = {x["id"]: x for x in AP.requirements_table(ec)}
    req = [r for r in rows if r["kind"] == "requirement"]
    assert [r["id"] for r in req] == [f"R{i}" for i in range(1, 13)]
    for r in req:
        assert r["B"] == rb[r["id"]]["predicted"] and r["C"] == rc[r["id"]]["predicted"]
        assert r["status_B"] == rb[r["id"]]["status"] and r["status_C"] == rc[r["id"]]["status"]
    gates = [r for r in rows if r["kind"] == "gate"]
    assert [g["id"] for g in gates] == ["G1", "G2", "G3", "G4", "G5", "G6"]
    assert [g["status_C"] for g in gates] == [g["status"] for g in AP.gates(ec)]
    for r in (x for x in rows if x["kind"] == "metric"):
        scale = next(s for k, _l, s, _u in RC.KEY_METRICS if k == r["id"])
        assert r["C"] == pytest.approx(ec.results[r["id"]].value * scale)
        assert r["B"] == pytest.approx(eb.results[r["id"]].value * scale)
    # the numbers quoted in C1 are what the engine computes
    v = lambda e, k: e.results[k].value  # noqa: E731
    assert v(eb, "v_cruise") == pytest.approx(12.47, abs=0.01) and v(ec, "v_cruise") == pytest.approx(16.4, abs=0.05)
    assert v(eb, "cl_cruise") == pytest.approx(0.590, abs=1e-3) and v(ec, "cl_cruise") == pytest.approx(0.341, abs=1e-3)
    assert v(eb, "p_cruise_elec") == pytest.approx(62, abs=0.5) and v(ec, "p_cruise_elec") == pytest.approx(94, abs=0.5)
    assert v(eb, "endurance_cruise") / 60 == pytest.approx(45.6, abs=0.05)
    assert v(ec, "endurance_cruise") / 60 == pytest.approx(30.2, abs=0.05)
    assert v(eb, "mission_margin") == pytest.approx(3.54, abs=0.005)
    assert v(ec, "mission_margin") == pytest.approx(2.96, abs=0.005)
    # stall speed quoted in C2 / OPERATING_LIMITS: 1.2 V_stall = v_cruise
    assert v(ec, "v_cruise") / 1.2 == pytest.approx(13.7, abs=0.05)
    assert 16.0 / (v(ec, "v_cruise") / 1.2) == pytest.approx(1.17, abs=0.005)
    assert RC.TRANSITION["transition_airspeed"] / (v(ec, "v_cruise") / 1.2) == pytest.approx(1.24, abs=0.005)
    # trim angle of the new cruise point on the CFD slope (quoted in C1)
    cal = RC.cfd_calibration(ec.model)
    assert math.degrees((v(ec, "cl_cruise") - cal["cl0"]) / cal["cla_cfd"]) == pytest.approx(7.9, abs=0.05)
    # elevon authority scales with the lift slope in the flight model (quoted in OPERATING_LIMITS: 45 % less)
    assert 1 - ec.model.params["cla_factor"].value == pytest.approx(0.45, abs=0.005)


def _count(case_results: dict, cases) -> tuple[int, int]:
    within = sum(1 for c in cases if case_results[c] != "ground" and case_results[c] <= AP.TRANSITION_MAX_ALT_LOSS)
    ground = sum(1 for c in cases if case_results[c] == "ground")
    return within, ground


def test_transition_claims_match_the_robustness_reference():
    ref, cases = RC.ROBUSTNESS_REFERENCE, list(RB.ROBUSTNESS_CASES)
    assert _count(ref["C"], cases) == (8, 1)
    assert _count(ref["B (CFD slope)"], cases) == (6, 4)
    assert _count(ref["C at output limit 0.64"], cases) == (7, 3)
    assert ref["B (CFD slope)"]["nominal"] == pytest.approx(3.5, abs=0.05)
    assert ref["B (flight-model slope)"]["nominal"] == pytest.approx(0.4, abs=0.05)
    assert ref["C"]["battery 50 %"] <= 1.2 and ref["C"]["battery 35 %"] == "ground"
    assert ref["C"]["battery 20 % charge"] == pytest.approx(24, abs=0.5)
    assert 15 <= ref["C"]["side gust 4 m/s"] <= 17.5 and 15 <= ref["C"]["battery 90 % + side gust 4 m/s"] <= 17.5
    assert ref["C"]["battery 70 % + side gust 4 m/s"] == "ground"
    assert max(ref["C"][c] for c in ("head gust 4 m/s", "tailwind gust 4 m/s", "payload 0.3 kg",
                                       "motor M2 at 85 %")) <= 0.5
    assert ref["C at output limit 0.64"]["motor M2 at 85 %"] == pytest.approx(20, abs=0.5)


@pytest.mark.skipif(not __import__("os").environ.get("OPENCAE_SLOW"), reason="6-DOF runs (set OPENCAE_SLOW=1)")
def test_robustness_reference_reproduces():
    got = RC.robustness_reference(workers=2)
    bad = []
    for name, ref in RC.ROBUSTNESS_REFERENCE.items():
        for case, want in ref.items():
            x = got[name][case]
            if (want == "ground") != (x == "ground") or (want != "ground" and abs(x - want) > 0.05):
                bad.append((name, case, want, x))
    assert not bad, bad


# ------------------------------------------------------------------------------------- measurement closure path
def test_measurement_template_rows_match_the_test_matrix(engines):
    eb, _ = engines
    tm = {t["id"]: t for t in MF.test_matrix(eb.model, eb.results)}
    plan = MF.measurement_plan(eb, variant="Revision B")
    st = _status(eb)
    open_ids = {rid for rid, s in st.items() if s != AP.PASS}
    in_plan = {rid for r in plan for rid in r["requirement_ids"].split("; ")}
    assert in_plan == open_ids  # one set of measurements per requirement that is not PASS
    for r in plan:
        assert r["test_id"] in tm, r
        assert r["value"] is None and r["unit"] and r["pass_line"] and r["how_to_measure"]
        assert r["hardware_hash"] == MF.hardware_hash(eb.model)
    for rid in open_ids:  # every requirement is closed by at least one test that covers it in the matrix
        tests = {r["test_id"] for r in plan if rid in r["requirement_ids"].split("; ")}
        assert any(rid in tm[t]["requirement_ids"] for t in tests), rid
    keys = [(r["test_id"], r["quantity"]) for r in plan]
    assert len(keys) == len(set(keys))  # one row per measurement


def test_bundled_revision_b_template_file(engines):
    eb, _ = engines
    assert TEMPLATE_B.exists()
    with open(TEMPLATE_B, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert rows and set(MF.FIELDS[:4]) <= set(rows[0])
    assert all(r["value"] == "" for r in rows)  # a template: nothing filled in
    plan = MF.measurement_plan(eb)
    assert [(r["test_id"], r["quantity"], r["requirement_ids"]) for r in rows] == \
        [(r["test_id"], r["quantity"], r["requirement_ids"]) for r in plan]
    assert {r["hardware_hash"] for r in rows} == {MF.hardware_hash(eb.model)}
    back = MF.import_measurements(TEMPLATE_B)
    assert MF.requirement_measurements(eb.model, back) == {}  # an empty template closes nothing


def _filled(plan, values):
    return [dict(r, value=values[r["quantity"]]) for r in plan]


# synthetic readings for the logic test only (never written to a data file)
_GOOD = {"mass_total": 2.0, "thrust_low_per_motor": 9.0, "total_spend": 1450.0, "usable_energy": 45.0,
         "mission_energy": 20.0, "tipover_deg": 38.0, "gust_lean_deg": 30.0, "i_motor_full": 22.0,
         "i_pack_full": 85.0, "x_cg": 58.0, "x_np": 76.0, "proof_load_held": 19.0, "thrust_full_per_motor": 12.0}


def test_imported_measurements_turn_conditionals_into_pass_or_fail(tmp_path, engines):
    eb, _ = engines
    m = eb.model
    plan = MF.measurement_plan(eb, variant="Revision B")
    p = tmp_path / "meas.csv"
    with open(p, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=MF.TEMPLATE_FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in _filled(plan, _GOOD):
            w.writerow({k: ("" if r.get(k) is None else r[k]) for k in MF.TEMPLATE_FIELDS})
    rows = MF.import_measurements(p)
    assert all(r["class"] == "test_measurement" and r["hardware_hash"] for r in rows)
    meas = MF.requirement_measurements(m, rows)
    assert set(meas) == {rid for rid, s in _status(eb).items() if s != AP.PASS}
    st = {x["id"]: x for x in AP.requirements_table(eb, meas)}
    assert all(x["status"] == AP.PASS for x in st.values())
    assert st["R2"]["measured"] == pytest.approx(4 * 9.0 / (2.0 * 9.80665))
    assert st["R7"]["measured"] == pytest.approx(25.0 / 22.0) and st["R8"]["measured"] == pytest.approx(2.0)
    assert st["R10"]["measured"] == pytest.approx(0.018 / eb.results["chord"].value)
    assert st["R4"]["measured"] == pytest.approx(45.0 / 20.0)
    # G5 now has measured evidence, but G6 still needs flight-test records and a human decision
    G = {g["id"]: g for g in AP.gates(eb, meas)}
    assert G["G5"]["status"] == AP.PASS and G["G6"]["status"] != AP.PASS
    # a measurement that misses the line fails the requirement
    bad = MF.requirement_measurements(m, _filled(plan, {**_GOOD, "i_motor_full": 27.0}))
    assert next(x for x in AP.requirements_table(eb, bad) if x["id"] == "R7")["status"] == AP.FAIL
    # units are converted (g -> kg); a wrong unit, other hardware or a missing input never closes anything
    g_rows = [dict(r, value=2000.0, unit="g") if r["quantity"] == "mass_total" else r for r in rows]
    assert MF.requirement_measurements(m, g_rows)["R1"]["value"] == pytest.approx(2.0)
    wrong = [dict(r, unit="V") if r["quantity"] == "i_motor_full" else r for r in rows]
    cs = MF.closure_status(m, wrong)
    assert not cs["R7"]["closed"] and cs["R7"]["ignored"]
    other = [dict(r, hardware_hash="000000000000") for r in rows]
    assert MF.requirement_measurements(m, other) == {}
    partial = [r for r in rows if r["quantity"] != "x_np"]
    cs = MF.closure_status(m, partial)
    assert not cs["R10"]["closed"] and cs["R10"]["missing"] == ["T09 x_np [mm]"]
    assert next(x for x in AP.requirements_table(eb, MF.requirement_measurements(m, partial))
                if x["id"] == "R10")["status"] == AP.COND
    # an estimated constant blocks the closure (a measurement combined with an estimate is not a measurement)
    e2 = Engine()
    RB.apply(e2)
    e2.model.option("motor").data["imax"].source = ESTIMATE
    cs = MF.closure_status(e2.model, rows)
    assert not cs["R7"]["closed"] and cs["R7"]["blocked"]


def test_pass_line_states_the_supplier_limits(engines):
    eb, _ = engines
    assert "i_motor_full <= 25.0 A" in MF.pass_line(eb.model, "R7")
    assert "i_motor_full <= 50.0 A" in MF.pass_line(eb.model, "R8")
    assert "total_spend <= 1691 USD" in MF.pass_line(eb.model, "R3")
    assert "x_np - x_cg >= 15.0 mm" in MF.pass_line(eb.model, "R10")


def test_esc_ratings_come_from_the_supplier_datasheet():
    from opencae.engineering import catalog as C
    esc = C.option(C.catalog(), "esc", "Tekko32F4-50A")
    for k in ("i_cont", "i_burst", "cells_min", "cells_max", "channels", "mass"):
        q = esc.q(k)
        assert q.source == SUPPLIER and q.ref.startswith("supplier datasheet") and "https://" in q.ref
    assert esc.q("i_cont").value == 50.0 and esc.q("i_burst").value == 60.0
    prop = C.option(C.catalog(), "prop", "APC11x4.5MR")
    assert prop.q("ct0").source == ESTIMATE and prop.q("cp0").source == ESTIMATE  # not confirmed: stays estimate
    for d in RC.DATASHEET_UPGRADES:
        assert d["refs"] and all(u.startswith("https://") for u in d["refs"]) and d["status_change"]


# ------------------------------------------------------------------------------------- study + website data
@pytest.fixture(scope="module")
def study():
    from opencae.examples.k09l import build_study as BS
    st = ST.Study()
    ra, rb, rc = BS.build_variants(st)  # no solver runs
    return st, rb["id"], rc["id"]


def test_build_study_variants(study):
    st, rid_b, rid_c = study
    assert [v["name"] for v in st.variants] == ["Revision A", "Revision B", "Revision C (recommended)"]
    vb, vc = st.variant(rid_b), st.variant(rid_c)
    assert vc["base"] == rid_b and vc["config"] == vb["config"]
    diff = {k for k, _a, _b in st.diff(rid_c, rid_b)}
    assert {"cla_factor", "cl_max"} <= diff and "payload" not in diff


def test_site_recs_schema(study):
    from opencae.web import data_recs as DR
    st, rid_b, rid_c = study
    d = DR.site_recs(st, rid_b, rid_c)
    json.dumps(d)  # JSON-serialisable
    assert set(d) >= {"changes", "comparison", "evidence_needed", "datasheet_upgrades", "not_applied"}
    assert [c["id"] for c in d["changes"]] == [c["id"] for c in RC.CHANGES]
    c1 = next(c for c in d["changes"] if c["id"] == "C1")
    pv = {p["param"]: p for p in c1["params"]}
    assert pv["cla_factor"]["B"] == 1.0 and pv["cla_factor"]["C"] == pytest.approx(0.5525, abs=1e-3)
    ev = {x["metric"]: x for x in c1["effects"]}
    assert ev["v_cruise"]["B"] == pytest.approx(12.47, abs=0.01) and ev["v_cruise"]["C"] == pytest.approx(16.4, abs=0.05)
    assert ev["cfd_lift_ratio"]["C"] is None  # no CFD run for C in this study: never borrowed from B
    assert ev["p_cruise_elec"]["B"] == pytest.approx(62.4, abs=0.05) and ev["p_cruise_elec"]["C"] == pytest.approx(94.0, abs=0.05)
    assert ev["endurance_cruise"]["C"] == pytest.approx(30.2, abs=0.05) and ev["cl_cruise"]["C"] == pytest.approx(0.341, abs=1e-3)
    assert all(a["status_C"] != "current" for a in c1["analyses"])
    comp = d["comparison"]
    assert {r["id"] for r in comp if r["kind"] == "requirement"} == {f"R{i}" for i in range(1, 13)}
    assert {r["id"] for r in comp if r["kind"] == "gate"} == {f"G{i}" for i in range(1, 7)}
    snap_c = st.snapshot(rid_c)
    for r in (x for x in comp if x["kind"] == "requirement"):
        s = next(x for x in snap_c["requirements"] if x["id"] == r["id"])
        assert r["status_C"] == s["status"]
        if s["predicted"] is None:
            assert r["C"] is None
        else:
            assert r["C"] == pytest.approx(s["predicted"], abs=1e-4)
    open_c = [x["id"] for x in snap_c["requirements"] if x["status"] != AP.PASS]
    assert [x["id"] for x in d["evidence_needed"]] == open_c
    for x in d["evidence_needed"]:
        assert x["closes_by_test"] and x["pass_line"].startswith(x["id"]) and x["why_conditional"]
        assert {"value", "unit", "class", "criterion"} <= set(x["prediction"])
    r7 = next(x for x in d["evidence_needed"] if x["id"] == "R7")
    assert r7["closes_by_test"][0]["test_id"] == "T04" and "25.0 A" in r7["pass_line"]
    assert r7["margin"] == pytest.approx(snap_c["metrics"]["motor_current_margin"]["value"] - 1.0, abs=1e-3)
    r8 = next(x for x in d["evidence_needed"] if x["id"] == "R8")
    assert r8["closes_by_data"] and any("ct" in e["input"] or "C_T" in e.get("ref", "") or "ct0" in e["input"]
                                        for e in r8["estimated_inputs"])
    assert all(n["why_not"] for n in d["not_applied"])
    assert d["datasheet_upgrades"] and all(u["refs"] for u in d["datasheet_upgrades"])
