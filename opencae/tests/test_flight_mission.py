"""The 'mission' flight scenario (ground -> climb -> transition -> cruise -> back-transition -> landing) and its
website track (opencae.web.data_extra). Reduced-order model on provisional data: these tests check that the
simulation reports what the model predicts, completely and honestly, not that the aircraft flies."""
from __future__ import annotations

import json
import math

import numpy as np
import pytest

from opencae.engineering import flightsim as fs
from opencae.engineering import revision_b as RB
from opencae.engineering.graph import Engine
from opencae.web import data_extra as DX


class _Ctx:
    """The parts of data_extra._Ctx the flight section uses."""

    def __init__(self, engine):
        self.m, self.r, self.snap = engine.model, engine.results, {}


@pytest.fixture(scope="module")
def ctx():
    e = Engine()
    RB.apply(e)
    return _Ctx(e)


@pytest.fixture(scope="module")
def sim(ctx):
    # exactly the run the website uses (dt = DX.SIM_DT = 0.004 s, logged every DX.MISSION_LOG_S)
    return DX._simulations(ctx, [("mission", None, DX.MISSION_LOG_S)])[0]


@pytest.fixture(scope="module")
def track(ctx, sim):
    tr = DX._track(ctx, "mission", sim, hz=20.0)
    tr.update(DX._mission_extras(sim))
    return DX._clean(tr)


@pytest.fixture(scope="module")
def failed(ctx):
    """A mission whose transition cannot converge (0.3 s timeout) and that hits its time limit before landing."""
    return fs.simulate(ctx.m, "mission", dt=0.004, log_every=6, altitude=8.0, transition_timeout=0.3, duration=13.0)


def _texts(sim):
    return [s for _, s in sim.meta["events"]]


def test_registered_and_uses_model_procedure(ctx, sim):
    assert "mission" in fs.SCENARIOS
    P = sim.meta["parameters"]
    assert sim.meta["scenario"] == "mission" and P["start"] == "ground"
    assert P["pitch_end_deg"] == ctx.m.params["transition_pitch_end_deg"].value
    assert P["pitch_rate_deg"] == ctx.m.params["transition_pitch_rate_deg"].value
    assert P["target_airspeed"] == ctx.m.params["transition_airspeed"].value
    assert sim.meta["dt"] == DX.SIM_DT


def test_starts_and_ends_on_the_ground(sim):
    z = sim.pos[:, 2]
    o = sim.meta["outcome"]
    assert z[0] < 0.5 and z[-1] < 0.5  # CG height standing on the feet
    assert abs(z[-1] - z[0]) < 0.1
    assert np.allclose(sim.rpm[0], 0.0) and np.allclose(sim.rpm[-1], 0.0, atol=50.0)  # disarmed / stopped
    assert sim.euler[0, 1] > 85 and sim.euler[-1, 1] > 80  # nose up on the feet at both ends
    texts = _texts(sim)
    assert any(s.startswith("lift-off") for s in texts)
    assert any(s.startswith("touchdown at vertical speed") for s in texts)
    assert any(s == "motors stopped after touchdown" for s in texts)
    assert o["landed"] and o["final_phase"] == "landed"
    assert -1.5 < o["touchdown_vertical_speed"] < 0.0
    assert 0.0 < o["liftoff_time"] < 2.0 and o["touchdown_time"] > o["liftoff_time"]
    assert sim.t[-1] < sim.meta["parameters"]["duration"]  # stopped after landing, not at the time limit
    assert 60.0 <= sim.t[-1] <= 90.0


def test_reaches_transition_altitude_and_cruises(sim):
    o = sim.meta["outcome"]
    assert 29.0 < o["max_altitude"] < 33.0
    assert abs(o["transition_altitude"] - 30.0) < 1.0
    assert 1.5 < o["max_climb_rate"] < 4.0
    assert o["transition_converged"] is True and o["back_transition_converged"] is True
    assert abs(o["cruise_airspeed_mean"] - sim.meta["parameters"]["target_airspeed"]) < 1.0
    assert o["cruise_duration"] >= 15.0 - 1e-6
    assert o["max_altitude_loss_from_transition"] < sim.meta["parameters"]["max_alt_loss"]
    assert o["landing_distance_from_takeoff"] > 50.0  # straight cruise; lands where hover is re-established
    assert any("landing site set where hover was re-established" in s for s in _texts(sim))
    assert o["energy_used_J"] > 0 and o["energy_used_Wh"] == pytest.approx(o["energy_used_J"] / 3600.0)


def test_phases_in_order(sim):
    ph = sim.meta["phases"]
    assert [p["name"] for p in ph] == list(fs.MISSION_PHASES)
    assert ph[0]["t0"] == 0.0 and ph[-1]["t1"] == pytest.approx(sim.t[-1])
    for a, b in zip(ph, ph[1:]):
        assert a["t1"] == pytest.approx(b["t0"]) and b["t0"] >= a["t0"]
    events = sim.meta["events"]
    for p in ph:  # every phase boundary is an event at its time
        assert any(abs(t - p["t0"]) < 1e-9 and s == f"phase: {p['label']}" for t, s in events)
    o = sim.meta["outcome"]
    assert o["mission_completed"] is True and o["phases_failed"] == []
    assert all(p["completed"] for p in ph)
    by = {p["name"]: p for p in ph}
    assert by["climb"]["altitude_end_m"] > 25.0 and by["descent"]["altitude_end_m"] < 5.0
    assert sum(p["energy_J"] for p in ph) == pytest.approx(o["energy_used_J"], rel=1e-6)


def test_states_finite_and_labelled(sim):
    n = len(sim.t)
    for name in fs.CHANNELS:
        a = np.asarray(getattr(sim, name), float)
        assert a.shape[0] == n and np.all(np.isfinite(a)), name
    assert np.all(np.diff(sim.t) > 0)
    assert sim.out_of_envelope.all()
    assert "NOT evidence" in sim.meta["validity"]
    # warnings that the model raised are kept (none is filtered out by the mission logic)
    for o in sim.meta["battery_oscillation"]:
        assert any("alternates step to step" in w and abs(t - o["t0"]) < 1e-9 for t, w in sim.warnings)


def test_failed_phase_reported(failed):
    """A transition that cannot converge and a time limit before landing are reported, not masked."""
    s = failed
    o = s.meta["outcome"]
    assert o["mission_completed"] is False and o["transition_converged"] is False
    assert "transition" in o["phases_failed"] and not o["landed"]
    assert o["final_phase"] != "landed" and o["final_phase"] in o["phases_failed"]
    w = [x for _, x in s.warnings]
    assert any("transition phase failed" in x for x in w)
    assert any(x.startswith("WARNING mission not completed") for x in w)
    tr = next(p for p in s.meta["phases"] if p["name"] == "transition")
    assert tr["completed"] is False and tr["note"]


def test_website_mission_track(ctx, sim, track):
    tr = track
    json.dumps(tr, allow_nan=False)
    t = tr["t"]
    n = len(t)
    assert n > 1000 and all(b > a for a, b in zip(t, t[1:]))
    assert 18.0 <= tr["sample_hz"] <= 22.0
    for k in ("pos_mm", "translation_mm", "quat", "m", "rpm", "airspeed", "altitude_m", "euler_deg",
              "battery_current_A", "throttle", "soc"):
        assert len(tr[k]) == n, k
    assert all(len(mm) == 16 and math.isfinite(sum(mm)) for mm in tr["m"])
    cg = tr["cg_mm"]
    for i in (0, n // 3, n // 2, n - 1):  # m maps the CG (model frame, mm) to pos_mm
        M = tr["m"][i]
        w = [sum(M[4 * r + c] * v for c, v in enumerate(cg + [1.0])) for r in range(3)]
        assert all(abs(a - b) < 0.5 for a, b in zip(w, tr["pos_mm"][i]))
    assert tr["altitude_m"][0] < 0.5 and tr["altitude_m"][-1] < 0.5 and max(tr["altitude_m"]) > 29.0
    ext = tr["world"]["extent_mm"]
    assert tr["world"]["up"] == "z" and len(ext) == 6 and ext[4] >= -50.0 and ext[5] > 29000.0
    assert any(e["text"].startswith("touchdown") for e in tr["events"])
    assert any(e["kind"] == "warning" for e in tr["events"]) == bool(sim.warnings)
    assert tr["outcome"]["mission_completed"] is True
    assert tr["validity"] and "NOT evidence" in tr["validity"]
    # phases (id + display name, in flight order) and per-phase numbers
    ids = [p["id"] for p in tr["phases"]]
    assert ids == list(fs.MISSION_PHASES)
    for p, q in zip(tr["phases"], tr["phases"][1:]):
        assert set(p) >= {"id", "name", "t0", "t1", "completed", "note"} and p["t1"] >= p["t0"]
        assert p["name"].lower() == fs.MISSION_PHASES[p["id"]] and q["t0"] == pytest.approx(p["t1"], abs=1e-3)
    assert [p["id"] for p in tr["phase_stats"]] == ids
    assert sum(p["energy_J"] for p in tr["phase_stats"]) == pytest.approx(sim.meta["outcome"]["energy_used_J"], abs=10)
    hl = tr["phase_summary"]  # flat scalars (the viewer lists them)
    assert all(not isinstance(v, (dict, list)) for v in hl.values())
    for k in ("max_climb_rate_ms", "max_altitude_m", "cruise_airspeed_ms", "touchdown_vertical_speed_ms",
              "energy_used_Wh", "min_battery_V"):
        assert isinstance(hl[k], (int, float)) and math.isfinite(hl[k]), k
    assert hl["mission_completed"] is True and hl["phases_failed"] == "none"
    sm = tr["summary"]
    assert sm["mission_completed"] is True and sm["phases_failed"] == []
    assert sm["min_battery_V_clean"] >= sm["min_battery_V"]
    assert bool(sm["battery_oscillation_intervals_s"]) == ("min_battery_V_incl_numerical_oscillation" in hl)
    assert sm["notes"]


def test_website_failed_phase_visible(failed):
    """A failed phase is flagged in the track's phase list and headline numbers."""
    ex = DX._clean(DX._mission_extras(failed))
    tr = next(p for p in ex["phases"] if p["id"] == "transition")
    assert tr["completed"] is False and tr["note"].startswith("FAILED:")
    assert "transition" in ex["phase_summary"]["phases_failed"] and ex["phase_summary"]["mission_completed"] is False


def test_flight_section_order(ctx, sim, monkeypatch):
    """flight_section puts 'mission' first and keeps 'hover' and 'transition' (simulations stubbed)."""
    seen = []

    def fake(_ctx, jobs):
        seen.extend(jobs)
        return [sim] * len(jobs)
    monkeypatch.setattr(DX, "_simulations", fake)
    d = DX.flight_section(ctx)
    assert list(d)[:3] == ["mission", "hover", "transition"]
    assert [j[0] for j in seen] == ["mission", "hover", "transition_forward"]
    assert d["mission"]["phases"] and d["mission"]["summary"]
