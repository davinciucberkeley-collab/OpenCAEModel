"""Tests of the reduced-order K09-L flight simulation (opencae.engineering.flightsim)."""
import math

import numpy as np
import pytest

from opencae.engineering import calcs
from opencae.engineering import flightsim as fs
from opencae.engineering.model import EngineeringModel


@pytest.fixture(scope="module")
def model():
    return EngineeringModel()


@pytest.fixture(scope="module")
def hover10(model):
    return fs.simulate(model, "hover", duration=10.0)


def test_hover_trim_thrust_and_current(model, hover10):
    r = hover10
    W = r.meta["mass"] * model.params["g"].value
    late = r.t > 5.0
    T = r.thrust[late].sum(1)
    assert np.all(np.abs(T - W) / W < 0.02)
    prop = calcs.propulsion(model, calcs.mass_properties(model))
    i_ref = prop["i_hover_total"].value  # motor-side current (sum of the four motors) in calcs
    i_sim = r.motor_current[late].sum(1).mean()
    assert abs(i_sim - i_ref) / i_ref < 0.15
    # battery current is smaller than the motor current sum (ESC steps the voltage down)
    assert r.current[late].mean() < i_sim


def test_free_fall_matches_g_t2(model):
    r = fs.simulate(model, "hover", duration=1.0, altitude=100.0, controller=False, aero=False, ground=False,
                    log_every=5)
    g = model.params["g"].value
    z_exp = 100.0 - 0.5 * g * r.t ** 2
    assert np.max(np.abs(r.pos[:, 2] - z_exp)) < 1e-6
    assert np.allclose(r.thrust, 0.0)


def test_energy_accounting(model):
    r = fs.simulate(model, "takeoff_hover", duration=3.0, log_every=1)
    e_int = np.concatenate([[0.0], np.cumsum(0.5 * (r.power[1:] + r.power[:-1]) * np.diff(r.t))])
    assert r.energy_used[-1] > 100.0
    assert abs(e_int[-1] - r.energy_used[-1]) / r.energy_used[-1] < 5e-3
    assert np.max(np.abs(e_int - r.energy_used)) < 0.01 * r.energy_used[-1] + 1.0


def test_hover_holds_position(hover10):
    r = hover10
    sp = np.array([0.0, 0.0, 5.0])
    assert r.t[-1] >= 10.0 - 1e-9
    assert np.max(np.linalg.norm(r.pos - sp, axis=1)) < 0.5
    assert not r.warnings


def test_gust_bounded_and_recovers(model):
    gust = dict(amplitude=4.0, duration=1.5, start=1.0, direction=(1.0, 0.0, 0.0))
    r = fs.simulate(model, "gust_response", duration=8.0, gust=gust)
    sp = np.array([0.0, 0.0, 5.0])
    err = np.linalg.norm(r.pos - sp, axis=1)
    assert err.max() > 0.05  # the gust does disturb the aircraft
    assert err.max() < 2.0  # bounded
    assert np.all(err[r.t > 6.5] < 0.25)  # recovered
    tilt = np.degrees(np.arccos(np.clip([-fs._q2R(q)[2, 0] for q in r.quat], -1, 1)))
    assert tilt.max() < 45.0


def test_motor_degradation_warns(model):
    r = fs.simulate(model, "motor_degradation", duration=4.0, motor_failure=dict(motor=1, factor=0.7, time=1.0))
    texts = [w for _, w in r.warnings]
    assert any("failure injected" in w for w in texts)
    assert any("M1" in w and "detected" in w for w in texts)
    assert np.all(np.isfinite(r.pos))


def test_yaw_torque_sign(model):
    mx = fs.mixer_matrix(model)
    spin = mx["spin"]  # +1 for props CCW seen from the nose (reaction torque along +x body)
    assert [("CCW" in n) for n in mx["motors"]] == [s > 0 for s in spin]
    W = calcs.mass_properties(model)["weight"].value
    T = np.linalg.solve(mx["B"], [W, 0.05, 0.0, 0.0])  # positive M_x command (hover yaw)
    assert np.all(T[spin > 0] > T[spin < 0])  # CCW props work harder
    pl = fs._Plant(model, dict(fs.COMMON))
    u = pl.throttle_for(T, np.zeros(4), np.full(4, 80.0), pl.v_full)
    r = fs.simulate(model, "hover", duration=0.6, open_loop_throttle=u.tolist(), aero=False, ground=False,
                    log_every=5)
    w = r.omega[-1]
    assert w[0] > 0.05 and abs(w[0]) > 3 * max(abs(w[1]), abs(w[2]))
    # the opposite command turns the other way
    T2 = np.linalg.solve(mx["B"], [W, -0.05, 0.0, 0.0])
    u2 = pl.throttle_for(T2, np.zeros(4), np.full(4, 80.0), pl.v_full)
    r2 = fs.simulate(model, "hover", duration=0.6, open_loop_throttle=u2.tolist(), aero=False, ground=False,
                     log_every=5)
    assert r2.omega[-1][0] < -0.05


def test_telemetry_consistent(model, hover10, tmp_path):
    r = hover10
    n = len(r.t)
    for name in fs.CHANNELS:
        a = np.asarray(getattr(r, name))
        assert a.shape[0] == n, name
        assert np.all(np.isfinite(a.astype(float))), name
    assert r.out_of_envelope.all()  # no validated envelope exists
    assert "not evidence of flight stability" in r.meta["validity"].lower() or "NOT evidence" in r.meta["validity"]
    assert r.meta["config_hash"] == model.config_hash() and r.meta["assumptions"]
    p = tmp_path / "hover.csv"
    r.to_csv(p)
    assert p.read_text().count("\n") == n + 2
    d = fs.compare(r, r)
    assert max(d.values()) == 0.0


def test_motor_option_changes_hover_current():
    m = EngineeringModel()
    a = fs.simulate(m, "hover", duration=2.0)
    ok, _ = m.set_option("motor", "X2814V3-900")
    assert ok
    b = fs.simulate(m, "hover", duration=2.0)
    ia, ib = a.motor_current[-1].sum(), b.motor_current[-1].sum()
    assert a.meta["config_hash"] != b.meta["config_hash"]
    assert b.meta["mass"] > a.meta["mass"]
    assert abs(ib - ia) / ia > 0.03


def test_scenarios_registered(model):
    for k in ("takeoff_hover", "hover", "land", "gust_response", "payload", "motor_degradation",
              "battery_decline", "transition_forward", "transition_back", "waypoints"):
        assert k in fs.SCENARIOS
    with pytest.raises(KeyError):
        fs.simulate(model, "nope")
