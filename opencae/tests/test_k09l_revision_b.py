"""K09-L design revision B: the parametric design changes, their effect on the requirements, and that they
never alter the imported CAD, never create evidence, and are reversible through the revision mechanism."""
import numpy as np
import pytest

from opencae.engineering import approval as AP
from opencae.engineering import calcs
from opencae.engineering import revision_b as RB
from opencae.engineering.graph import CURRENT, STALE, Engine
from opencae.engineering.quantities import ESTIMATE

FOOT = "Foot PETG + TPU pad (x4) — K09L-AF-FOOT"
OK = {AP.PASS, AP.COND}


@pytest.fixture(scope="module")
def revb():
    e = Engine()
    rev_a = e.model.save_revision("Revision A")
    RB.apply(e)
    rev_b = e.model.save_revision("Revision B")
    return e, rev_a["id"], rev_b["id"]


def test_revision_b_clears_every_failing_requirement(revb):
    e = revb[0]
    st = {r["id"]: r["status"] for r in AP.requirements_table(e)}
    assert st["R5"] == AP.PASS and st["R12"] == AP.PASS
    for rid in ("R1", "R2", "R3", "R4", "R6", "R7", "R8", "R9", "R10"):
        assert st[rid] in OK, (rid, st[rid])
    # estimates stay estimates: nothing that rests on the prop / aero / strength estimates is a PASS
    for rid in ("R2", "R4", "R6", "R7", "R10"):
        assert st[rid] == AP.COND
    assert st["R11"] == AP.NOT_EV  # needs the FEA run (test_revision_b_fea in the acceptance set)


def test_geometry_changes_and_their_numbers(revb):
    e = revb[0]
    m, r = e.model, e.results
    assert max(r["envelope"].value) == pytest.approx(1.15, abs=1e-3)
    assert sorted(r["envelope"].value)[0] <= 0.45 - 0.005 and r["box_fit"].value >= 1.01
    for name, j in m.joints.items():
        if name.startswith("Prop M"):
            assert abs(j["origin_mm"][2]) == pytest.approx(200.0)  # 206 -> 200 mm
    tube = m.components[RB.C.MOTOR_TUBE] if hasattr(RB, "C") else m.components["Motor tube CF 16x13.08x380 — DragonPlate FDPT.625*.515*24"]
    assert "368" in tube.name and np.ptp(m.body_mesh(m.bodies[tube.bodies[0]])[0][:, 2]) == pytest.approx(368.0, abs=0.01)
    pts = calcs.contact_patch(m) * 1000
    assert np.abs(pts[:, 2]).max() == pytest.approx(220.0, abs=0.01)
    assert pts[:, 0].max() == pytest.approx(314.0, abs=0.01)
    assert r["tipover_deg"].value > 36.0 + 2.0  # beyond the 36 deg gust lean with margin
    assert r["static_margin"].value >= 0.05
    assert r["mass_total"].value <= 1.9822  # mass-neutral feet
    assert m.component_mass(m.components[FOOT]).source == ESTIMATE  # volume-scaled CAD mass
    assert m.components[FOOT].pn == "K09L-AF-FOOT-B"
    assert r["motor_current_margin"].value >= 1.05 and r["tw_low"].value >= 1.6
    assert r["throttle_headroom_low"].value >= 1.3
    assert r["foot_sf"].value >= 2.0 and r["foot_sf"].source != "analytical"
    assert r["reserve"].value >= 309 and r["reserve"].source == ESTIMATE
    # foot clamp hardware follows the thinner walls: M3x25 with 4.5 mm of thread past the locknut
    scr = m.components["SHCS M3x30 A2 — ISO 4762 M3x30 (vendor PN not verified)"]
    assert "M3x25" in scr.pn and m.component_mass(scr).source == ESTIMATE
    for i in scr.bodies:
        P = m.body_mesh(m.bodies[i])[0]
        assert np.ptp(P[:, 1]) == pytest.approx(28.0, abs=0.01)  # 25 mm shank + 3 mm head
        assert np.abs(P[:, 1]).max() == pytest.approx(331.0, abs=0.01)
    for b in m.bodies:
        if b.group == "hardware" and "Washer" in b.name and b.centroid[0] > 280:
            assert np.abs(m.body_mesh(b)[0][:, 1]).max() == pytest.approx(312.0, abs=0.01)  # on the inner wall


def test_revision_round_trip_restores_cad_and_parameters(revb):
    e, ra, rb = revb
    m = e.model
    e.restore_revision(ra)
    assert np.array_equal(m.points, m._points0)  # pristine CAD back, bit for bit
    assert m.config["landing"] == "feet" and m.config["mount"] == "as_modelled"
    assert m.params["throttle_max"].value == 1.0 and m.params["shipping"].missing
    assert e.results["tipover_deg"].value == pytest.approx(34.66, abs=0.05)
    assert m.components[FOOT].pn == "K09L-AF-FOOT"
    st = {r["id"]: r["status"] for r in AP.requirements_table(e)}
    assert st["R5"] == AP.FAIL and st["R6"] == AP.FAIL and st["R7"] == AP.FAIL
    e.restore_revision(rb)
    assert m.config["landing"] == "feet_b" and m.params["throttle_max"].value == RB.THROTTLE_MAX


def test_flight_model_honours_the_output_limit(revb):
    from opencae.engineering import flightsim as FS
    e = revb[0]
    pl = FS._Plant(e.model, FS._params("hover", None, {})[1])
    assert pl.u_max == RB.THROTTLE_MAX
    tm = pl.thrust_max(np.zeros(4), np.full(4, 100.0), 16.8)
    assert tm.sum() / (pl.m * pl.g) == pytest.approx(e.results["tw_full"].value, rel=0.15)
    assert pl.throttle_for(np.full(4, 1e3), np.zeros(4), np.zeros(4), 16.8).max() <= RB.THROTTLE_MAX


def test_heavy_records_follow_their_inputs_across_revisions(tmp_path):
    e = Engine()
    ra = e.model.save_revision("A")["id"]
    e.record_heavy("assembly_check", {"summary": "fake A run for the hash test"})
    RB.apply(e)
    rb = e.model.save_revision("B")["id"]
    assert e.heavy_status("assembly_check")["status"] == STALE
    e.restore_revision(ra)
    assert e.heavy_status("assembly_check")["status"] == CURRENT  # same inputs as the A run (history)
    p = tmp_path / "s.json"
    e.save_state(p)
    e2 = Engine()
    e2.load_state(p)
    assert e2.model.config["landing"] == "feet" and e2.heavy_status("assembly_check")["status"] == CURRENT
    e2.restore_revision(rb)
    assert e2.heavy_status("assembly_check")["status"] != CURRENT  # never run for B


def test_bundled_revision_b_state(tmp_path):
    """The shipped state holds revisions A and B and the solver runs of both (when present)."""
    if not RB.STATE_FILE.exists():
        pytest.skip("bundled state not generated")
    e = Engine()
    e.load_state(RB.STATE_FILE)
    notes = [r["note"] for r in e.model.revisions]
    assert any(n.startswith("Revision A") for n in notes) and any(n.startswith("Revision B") for n in notes)
    assert e.model.config["landing"] == "feet_b"
    for nid in ("fea_motor_tube", "fea_spar", "assembly_check", "flight_hover", "flight_transition"):
        assert e.heavy_status(nid)["status"] == CURRENT, nid
    st = {r["id"]: r["status"] for r in AP.requirements_table(e)}
    assert all(s in OK for s in st.values()), st


def test_transition_procedure_is_a_design_input(revb):
    from opencae.engineering import flightsim as FS
    e = revb[0]
    assert "flight_procedure" in e.upstream("flight_transition")
    sim = FS.simulate(e.model, "transition_forward", duration=0.05)
    P = sim.meta["parameters"]
    assert (P["pitch_end_deg"], P["pitch_rate_deg"], P["target_airspeed"]) == (80.0, 60.0, 16.0)
    sim = FS.simulate(e.model, "transition_forward", duration=0.05, pitch_end_deg=70.0)
    assert sim.meta["parameters"]["pitch_end_deg"] == 70.0  # explicit override wins


def test_gate_g3_rejects_a_transition_that_loses_altitude():
    e = Engine()
    base = {"scenario": "transition_forward", "altitude_start": 30.0, "warnings": [],
            "outcome": {"transition_converged": True}}
    e.record_heavy("flight_transition", {**base, "min_altitude": 6.0})
    row = next(c for c in AP.gates(e)[2]["criteria"] if c["text"].startswith("Transition"))
    assert row["status"] == AP.FAIL and "altitude loss 24.0 m" in row["why"]
    e.record_heavy("flight_transition", {**base, "min_altitude": 29.6,
                                         "warnings": [[1.0, "motor M2 current 25.2 A above the catalog 30-s rating"]]})
    row = next(c for c in AP.gates(e)[2]["criteria"] if c["text"].startswith("Transition"))
    assert row["status"] == AP.COND and "altitude loss 0.4 m" in row["why"]
    e.record_heavy("flight_transition", {**base, "min_altitude": 29.9,
                                         "warnings": [[2.0, "hard ground contact at -7 m/s"]]})
    row = next(c for c in AP.gates(e)[2]["criteria"] if c["text"].startswith("Transition"))
    assert row["status"] == AP.FAIL


def test_revision_b_adds_no_interference():
    """The parametric changes must not create clashes: B has exactly the interferences of A (the 0.1 mm
    wing-core / pod-shell contact that the imported CAD already has)."""
    from opencae.engineering import assembly as AS
    e = Engine()
    a = {tuple(sorted(i["names"])) for i in AS.check_assembly(e.model)["interferences"]}
    RB.apply(e)
    b = {tuple(sorted(i["names"])) for i in AS.check_assembly(e.model)["interferences"]}
    assert b == a
