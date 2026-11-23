"""Generic drone solvers: structure (CalculiX), CFD (OpenFOAM, whole aircraft), hover flight simulation."""
from __future__ import annotations

import copy
import math
import shutil
from pathlib import Path

import numpy as np
import pytest

from opencae.drone import types as T
from opencae.drone.analyses import cfd, flight, structure


# ------------------------------------------------------------------------------------------ fixtures
def box(lo, hi):
    x0, y0, z0 = lo
    x1, y1, z1 = hi
    P = np.array([[x0, y0, z0], [x1, y0, z0], [x1, y1, z0], [x0, y1, z0],
                  [x0, y0, z1], [x1, y0, z1], [x1, y1, z1], [x0, y1, z1]], float)
    Tr = np.array([[0, 2, 1], [0, 3, 2], [4, 5, 6], [4, 6, 7], [0, 1, 5], [0, 5, 4], [1, 2, 6], [1, 6, 5],
                   [2, 3, 7], [2, 7, 6], [3, 0, 4], [3, 4, 7]])
    return P, Tr


def cylinder_z(cx, cy, z0, z1, r, n=24):
    a = np.linspace(0, 2 * math.pi, n, endpoint=False)
    ring = np.stack([cx + r * np.cos(a), cy + r * np.sin(a)], 1)
    P = np.vstack([np.c_[ring, np.full(n, z0)], np.c_[ring, np.full(n, z1)], [[cx, cy, z0]], [[cx, cy, z1]]])
    Tr = []
    for i in range(n):
        j = (i + 1) % n
        Tr += [[i, j, n + j], [i, n + j, n + i], [2 * n, j, i], [2 * n + 1, n + i, n + j]]
    return P, np.array(Tr)


def quad_aircraft(arm_len=110.0):
    parts = []

    def add(name, role, PT):
        parts.append(T.Part(name, role, PT[0].astype(float), PT[1].astype(np.int64)))
    add("frame", "frame", box((-30, -30, -5), (30, 30, 10)))
    add("battery", "battery", box((-25, -12, 10), (25, 12, 22)))
    for i, (sx, sy) in enumerate([(1, 1), (-1, 1), (-1, -1), (1, -1)]):
        c = arm_len / math.sqrt(2)
        lo = [min(0, sx * c), min(0, sy * c) - 4, -2]
        hi = [max(0, sx * c), max(0, sy * c) + 4, 6]
        # an arm along the diagonal is awkward as a box: use an x-aligned box then rotate
        P, Tr = box((0, -5, -2.5), (arm_len, 5, 2.5))
        ang = math.atan2(sy, sx)
        R = np.array([[math.cos(ang), -math.sin(ang), 0], [math.sin(ang), math.cos(ang), 0], [0, 0, 1]])
        add(f"arm_{i + 1}", "arm", (P @ R.T, Tr))
        add(f"motor_{i + 1}", "motor", cylinder_z(sx * c, sy * c, 2.5, 14, 7))
        add(f"prop_{i + 1}", "prop", cylinder_z(sx * c, sy * c, 14, 15.5, 62))
    return T.Aircraft("quad", parts)


def rotors_for(ac, spins=("ccw", "cw", "ccw", "cw")):
    out = []
    for i, p in enumerate(ac.role_parts("motor")):
        out.append({"position_mm": p.centroid.tolist(), "axis": [0, 0, 1], "spin": spins[i]})
    return out


def metric(v, unit="", basis="estimate"):
    return T.metric(v, unit, T.OK, basis)


def quad_spec():
    return {"name": "q", "type": "multirotor", "engine": "generic",
            "roles": {}, "materials": {"carbon": {"density_kg_m3": 1600, "youngs_modulus_gpa": 70, "poisson": 0.3,
                                                 "yield_strength_mpa": 600, "status": "estimate", "source": ""}},
            "role_materials": {"arm": "carbon"},
            "propulsion": {"prop": {"diameter_in": 5, "pitch_in": 4.3, "ct": 0.12, "cp": 0.06}},
            "aero": {"cruise_speed_ms": 12.0, "alpha_deg": 0.0},
            "environment": {"rho": 1.225, "g": 9.81},
            "analyses": {"cfd": {"enabled": True, "preset": "coarse", "iterations": 30},
                         "structure": {"enabled": True, "load_factor": 3.0},
                         "flight": {"enabled": True, "disturbance_deg": 15}}}


def previous_for(ac, mass=0.5):
    return {"mass": {"metrics": {"mass_kg": metric(mass, "kg"), "cg_mm": metric([0, 0, 5], "mm"),
                                 "inertia_kgm2": metric([[2.0e-3, 0, 0], [0, 2.0e-3, 0], [0, 0, 3.5e-3]])}},
            "propulsion": {"metrics": {"thrust_max_n": metric(mass * 9.81 / 4 * 3.5, "N")},
                           "extra": {"rotors": rotors_for(ac)}}}


def ctx_for(tmp_path, ac, spec, previous=None):
    wd, out = tmp_path / "scratch", tmp_path / "out"
    wd.mkdir(exist_ok=True)
    out.mkdir(exist_ok=True)
    return T.RunContext(project_dir=tmp_path, spec=spec, aircraft=ac, workdir=wd, out_dir=out,
                        previous=previous or {})


# ----------------------------------------------------------------------------------------------- structure
def test_structure_missing_material_is_not_evaluated(tmp_path):
    ac = quad_aircraft()
    spec = quad_spec()
    del spec["materials"]["carbon"]["yield_strength_mpa"]
    r = structure.run(ctx_for(tmp_path, ac, spec, previous_for(ac)))
    assert r["status"] == T.NOT_EVALUATED
    assert "materials.carbon.yield_strength_mpa" in r["missing"]
    spec2 = quad_spec()
    spec2["role_materials"] = {}
    r2 = structure.run(ctx_for(tmp_path, ac, spec2, previous_for(ac)))
    assert r2["status"] == T.NOT_EVALUATED and "role_materials.arm" in r2["missing"]
    # no arm part at all: names the role
    ac2 = T.Aircraft("x", [p for p in ac.parts if p.role != "arm"])
    r3 = structure.run(ctx_for(tmp_path, ac2, quad_spec(), previous_for(ac)))
    assert r3["status"] == T.NOT_EVALUATED and "arm" in " ".join(r3["missing"])


@pytest.mark.solver
def test_structure_arm_runs(tmp_path):
    ac = quad_aircraft()
    ac = T.Aircraft("one_arm", [p for p in ac.parts if p.name in ("frame", "arm_1", "motor_1")])
    prev = previous_for(quad_aircraft())
    prev["propulsion"]["extra"]["rotors"] = [prev["propulsion"]["extra"]["rotors"][0]]
    ctx = ctx_for(tmp_path, ac, quad_spec(), prev)
    r = structure.run(ctx)
    assert r["status"] in (T.OK, T.WATCH, T.FAIL), r["summary"] + r["log"]
    sf = r["metrics"]["safety_factor"]
    assert sf["value"] > 0 and sf["basis"] == "estimate"
    assert r["metrics"]["max_stress_mpa"]["value"] > 0 and r["metrics"]["max_disp_mm"]["value"] > 0
    f = ctx.out_dir / r["fields"][0]["file"]
    assert f.exists()
    with np.load(f) as z:
        assert z["values"].dtype == np.float16 and len(z["values"]) == len(z["points"])
    assert r["extra"]["parts"][0]["elements"] <= 40_000


# ----------------------------------------------------------------------------------------------- flight
def test_flight_hover_quad(tmp_path):
    ac = quad_aircraft()
    r = flight.run(ctx_for(tmp_path, ac, quad_spec(), previous_for(ac)))
    assert r["status"] in (T.OK, T.WATCH), r["summary"]
    m = r["metrics"]
    assert m["max_tilt_deg"]["value"] < 30
    assert m["settle_time_s"]["value"] is not None and m["settle_time_s"]["value"] < 5
    assert 0 < m["throttle_margin"]["value"] <= 1
    assert m["position_error_m"]["value"] < 1.0
    assert all(len(r["extra"][k]) <= 400 for k in ("t", "pos", "att", "motor"))
    assert r["extra"]["t"][-1] == pytest.approx(8.0, abs=0.05)


def test_flight_missing_inputs_and_underactuated(tmp_path):
    ac = quad_aircraft()
    r = flight.run(ctx_for(tmp_path, ac, quad_spec(), {}))
    assert r["status"] == T.NOT_EVALUATED and any("mass" in k or "propulsion" in k for k in r["missing"])
    prev = previous_for(ac)
    prev["propulsion"]["extra"]["rotors"] = prev["propulsion"]["extra"]["rotors"][:2]
    assert flight.run(ctx_for(tmp_path, ac, quad_spec(), prev))["status"] == T.NOT_EVALUATED


def test_flight_fixed_wing_not_evaluated(tmp_path):
    ac = quad_aircraft()
    spec = quad_spec()
    spec["type"] = "fixed_wing"
    r = flight.run(ctx_for(tmp_path, ac, spec, previous_for(ac)))
    assert r["status"] == T.NOT_EVALUATED and "fixed-wing flight is not simulated generically" in r["summary"]


# --------------------------------------------------------------------------------------------------- cfd
def test_cfd_case_dictionaries(tmp_path):
    ac = quad_aircraft()
    info = cfd.write_case(tmp_path / "case", ac, quad_spec(), "coarse", 30, 2, 12.0, 3.0)
    for f in ("system/blockMeshDict", "system/snappyHexMeshDict", "system/controlDict", "system/decomposeParDict",
              "0/U", "0/p", "0/k", "0/omega", "0/nut", "constant/triSurface/aircraft.stl"):
        assert (tmp_path / "case" / f).exists(), f
    d = info["domain_m"]
    lo, hi = np.array(info["bbox_m"][0]), np.array(info["bbox_m"][1])
    assert d["x"][0] < lo[0] - 2.9 * d["L"] and d["x"][1] > hi[0] + 5.9 * d["L"]
    for ax, i in (("y", 1), ("z", 2)):
        assert d[ax][0] < lo[i] - 2.4 * d["Lc"] and d[ax][1] > hi[i] + 2.4 * d["Lc"]
    loc = np.array(info["location_in_mesh_m"])
    assert all(d[a][0] < loc[i] < d[a][1] for a, i in (("x", 0), ("y", 1), ("z", 2)))
    for p in ac.parts:  # the mirrored geometry the solver sees
        P, Tt = cfd._mirror_y(p.points * 1e-3, p.triangles)
        assert not cfd.point_inside(P, Tt, loc), p.name
    assert "prop_1" in info["omitted_parts"] and "prop_1" not in info["parts"]
    txt = (tmp_path / "case/system/snappyHexMeshDict").read_text()
    assert "locationInMesh" in txt and "kOmegaSST" in (tmp_path / "case/constant/turbulenceProperties").read_text()
    # the whole aircraft: no symmetry plane
    assert "symmetry" not in (tmp_path / "case/system/blockMeshDict").read_text()
    u = (tmp_path / "case/0/U").read_text()
    assert "freestreamVelocity" in u


def test_cfd_reference_area_and_inputs(tmp_path):
    ac = quad_aircraft()
    spec = quad_spec()
    a, txt, _ = cfd.reference_area(ac, spec)
    assert a > 0 and "frontal" in txt
    spec["aero"]["ref_area_m2"] = 0.05
    assert cfd.reference_area(ac, spec)[0] == 0.05
    s2 = quad_spec()
    del s2["aero"]["cruise_speed_ms"]
    r = cfd.run(ctx_for(tmp_path, ac, s2))
    assert r["status"] == T.NOT_EVALUATED and "aero.cruise_speed_ms" in r["missing"]


def test_cfd_missing_openfoam_reports_error(tmp_path, monkeypatch):
    monkeypatch.setattr(cfd, "_find_foam", lambda: None)
    ac = quad_aircraft()
    r = cfd.run(ctx_for(tmp_path, ac, quad_spec()))
    assert r["status"] == T.ERROR and "OpenFOAM" in r["summary"] and "simpleFoam" in r["summary"]


def test_structure_missing_ccx_reports_error(tmp_path, monkeypatch):
    monkeypatch.setattr(structure, "_find_ccx", lambda: None)
    ac = quad_aircraft()
    r = structure.run(ctx_for(tmp_path, ac, quad_spec(), previous_for(ac)))
    assert r["status"] == T.ERROR and "ccx" in r["summary"]


def _foam_available() -> bool:
    return cfd._find_foam() is not None and shutil.which("simpleFoam") is not None


@pytest.mark.slow
@pytest.mark.skipif(not _foam_available(), reason="OpenFOAM (simpleFoam) not found")
def test_cfd_quad_runs(tmp_path):
    # NOTE: no @pytest.mark.openfoam here: conftest skips that marker for OpenFOAM < v2006 and this machine has
    # Ubuntu's v1912, which the case supports (it uses no function objects).
    spec, ac = quad_spec(), quad_aircraft()
    ex = Path(__file__).parent.parent / "examples" / "drones" / "quad250"
    if (ex / "drone.json").exists():
        from opencae.drone import geometry, spec as S
        spec = S.load(ex)
        ac = geometry.load(ex, spec)
        spec = S.apply_overrides(spec, {"analyses.cfd.preset": "coarse", "analyses.cfd.iterations": 30})
        if cfd._num(spec, "analyses.cfd.speed_ms")[0] is None and cfd._num(spec, "aero.cruise_speed_ms")[0] is None:
            spec = S.apply_overrides(spec, {"aero.cruise_speed_ms": 12.0})
    ctx = ctx_for(tmp_path, ac, spec)
    r = cfd.run(ctx)
    assert r["status"] in (T.OK, T.WATCH), r["summary"] + r["log"]
    m = r["metrics"]
    assert m["cells"]["value"] > 1000 and m["iterations"]["value"] >= 1
    assert math.isfinite(m["cd"]["value"]) and m["drag_n"]["value"] > 0
    assert "coarse mesh" in r["summary"].lower()
    for f in ("cfd_cp.npz", "cfd_p.npz", "cfd_u_wall.npz", "cfd_tau_wall.npz"):
        assert (ctx.out_dir / f).exists(), f
    with np.load(ctx.out_dir / "cfd_cp.npz") as z:
        assert z["values"].dtype == np.float16
        assert len(z["stream_points"]) > 0 and len(z["slice_points"]) > 0
    assert not list((ctx.workdir).rglob("processor*"))
    print("CELLS", m["cells"]["value"], "RUNTIME", r["solver"]["runtime_s"], r["solver"]["step_seconds"])
