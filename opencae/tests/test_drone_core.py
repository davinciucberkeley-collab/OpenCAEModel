"""Drone core: spec, geometry import, mass and propulsion, checks, pipeline (no heavy solver is run here)."""
import json
import shutil
import struct
from pathlib import Path

import numpy as np
import pytest

from opencae.drone import geometry as G
from opencae.drone import pipeline as P
from opencae.drone import spec as S
from opencae.drone.analyses import checks
from opencae.drone.types import Aircraft

EX = Path(__file__).resolve().parent.parent / "examples" / "drones"
LIGHT = ["mass", "propulsion"]  # never run the solver analyses in these tests


@pytest.fixture
def quad(tmp_path):
    d = tmp_path / "quad"
    shutil.copytree(EX / "quad250", d)
    return d


def _binary_stl(ascii_path: Path, out: Path):
    ac = G.load(ascii_path.parent, {"geometry": {"file": ascii_path.name}, "roles": {}})
    tris = []
    for p in ac.parts:
        tris.append(p.points[p.triangles])
    tris = np.concatenate(tris).astype("<f4")
    with open(out, "wb") as f:
        f.write(b"binary stl".ljust(80, b" "))
        f.write(struct.pack("<I", len(tris)))
        for t in tris:
            f.write(struct.pack("<3f", 0, 0, 0) + t.tobytes() + b"\0\0")


def test_spec_param_and_overrides(quad):
    spec = S.load(quad)
    p = S.param(spec, "propulsion.motor.kv")
    assert p.value == 2400 and p.status == "estimate"
    assert S.param(spec, "materials.carbon_plate.density_kg_m3").status == "estimate"
    assert S.param(spec, "propulsion.nothing") is None
    assert S.validate(spec) == []
    before = (quad / "drone.json").read_text()
    out = S.apply_overrides(spec, {"propulsion.battery.capacity_mah": 3000, "analyses.cfd.preset": "fine"})
    assert out["propulsion"]["battery"]["capacity_mah"]["value"] == 3000
    assert out["propulsion"]["battery"]["capacity_mah"]["status"] == "estimate"
    assert spec["propulsion"]["battery"]["capacity_mah"]["value"] == 1500  # copy, not edited in place
    with pytest.raises(KeyError):
        S.apply_overrides(spec, {"propulsion.battery.nope": 1})
    with pytest.raises(ValueError):
        S.apply_overrides(spec, {"propulsion.motor.kv": "fast"})
    with pytest.raises(ValueError):
        S.apply_overrides(spec, {"propulsion.motor.kv": True})
    assert (quad / "drone.json").read_text() == before
    assert S.input_hash(spec, ["propulsion.motor"]) != S.input_hash(out, ["propulsion.battery"])
    assert S.input_hash(spec, ["mass"]) == S.input_hash(out, ["mass"])


def test_geometry_units_and_roles(quad):
    spec = S.load(quad)
    ac = G.load(quad, spec)
    assert isinstance(ac, Aircraft) and len(ac.parts) >= 13
    assert [len(ac.role_parts(r)) for r in ("arm", "motor", "prop")] == [4, 4, 4]
    lo, hi = ac.bbox
    assert 200 < float(np.linalg.norm(hi - lo)) < 450  # 250 mm motor diagonal + prop discs -> 431 mm
    # the same file declared in metres: x1000
    spec_m = json.loads(json.dumps(spec))
    spec_m["geometry"]["units"] = "m"
    lo2, hi2 = G.load(quad, spec_m).bbox
    assert np.allclose(hi2 - lo2, (hi - lo) * 1000, rtol=1e-6)
    # axes: forward -x flips x
    spec_f = json.loads(json.dumps(spec))
    spec_f["geometry"]["forward"] = "-x"
    spec_f["geometry"]["up"] = "+z"
    p0 = ac.role_parts("battery")[0].centroid
    p1 = G.load(quad, spec_f).role_parts("battery")[0].centroid
    assert np.allclose(p1, [-p0[0], -p0[1], p0[2]], atol=1e-6)  # y flips with x: still right-handed
    # parts are closed solids
    from opencae.drone.analyses.mass import solid_moments
    assert all(solid_moments(p)[3] for p in ac.parts)


def test_guess_roles_and_obj(tmp_path):
    names = ["Arm_L", "motor_1", "motor_2", "PropellerA", "lipo battery", "left_wing", "elevon_r", "rudder",
             "skid", "main_body", "FC", "Payload", "widget"]
    g = G.guess_roles(names)
    role = G.assign_roles(names, g)
    assert role["Arm_L"] == "arm" and role["motor_2"] == "motor" and role["PropellerA"] == "prop"
    assert role["lipo battery"] == "battery" and role["left_wing"] == "wing"
    assert role["elevon_r"] == "control_surface" and role["rudder"] == "fin" and role["skid"] == "landing"
    assert role["main_body"] == "frame" and role["FC"] == "electronics" and role["Payload"] == "payload"
    assert role["widget"] == "other"
    assert "motor_*" in g["motor"]
    obj = tmp_path / "t.obj"
    obj.write_text("o box_a\nv 0 0 0\nv 1 0 0\nv 0 1 0\nv 0 0 1\nf 1 2 3\nf 1 2 4\nf 1 3 4\nf 2 3 4\n"
                   "o wing_b\nv 5 0 0\nv 6 0 0\nv 5 1 0\nv 5 0 1\nf 5 6 7\nf 5 6 8\nf 5 7 8\nf 6 7 8\n")
    ac = G.load(tmp_path, {"geometry": {"file": "t.obj", "units": "m"}})
    assert [p.name for p in ac.parts] == ["box_a", "wing_b"] and ac.parts[1].role == "wing"
    assert ac.bbox[1][0] == pytest.approx(6000)


def test_step_import(tmp_path):
    occ = pytest.importorskip("opencae.geometry.occ_kernel")
    from OCP.BRep import BRep_Builder
    from OCP.TopoDS import TopoDS_Compound
    shapes = [occ.make_primitive("box", {"dx": 10, "dy": 10, "dz": 10}) for _ in range(2)]
    try:
        comp, b = TopoDS_Compound(), BRep_Builder()
        b.MakeCompound(comp)
        for s in shapes:
            b.Add(comp, s)
        from OCP.IFSelect import IFSelect_RetDone
        from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer
        w = STEPControl_Writer()
        w.Transfer(comp, STEPControl_AsIs)
        assert w.Write(str(tmp_path / "x.step")) == IFSelect_RetDone
    except Exception as exc:  # pragma: no cover - environment specific API differences
        pytest.skip(f"cannot write a STEP file here: {exc}")
    ac = G.load(tmp_path, {"geometry": {"file": "x.step"}})
    assert len(ac.parts) == 2 and ac.parts[0].name == "solid_1"


def test_unnamed_parts(tmp_path, quad):
    _binary_stl(quad / "airframe.stl", tmp_path / "blob.stl")
    proj = tmp_path / "unnamed"
    P.new_project(proj, tmp_path / "blob.stl", "blob", "multirotor")
    spec = S.load(proj)
    ac = G.load(proj, spec)
    assert len(ac.parts) >= 13 and ac.parts[0].name.startswith("part_")
    assert {p.role for p in ac.parts} == {"other"}
    out = P.run_project(proj, only=LIGHT)
    assert out["analyses"]["mass"]["status"] == "not_evaluated"  # no masses for parts named part_N
    row = next(c for c in out["checks"] if c["metric"] == "structure.safety_factor")
    assert row["status"] == "not_evaluated" and "'arm'" in row["why"]
    assert out["verdict"] == "not ready"


def test_mass_propulsion_quad(quad):
    out = P.run_project(quad, only=LIGHT)
    m, p = out["analyses"]["mass"], out["analyses"]["propulsion"]
    assert m["metrics"]["mass_kg"]["value"] == pytest.approx(0.472, abs=0.02)
    assert len(m["metrics"]["cg_mm"]["value"]) == 3 and len(m["metrics"]["inertia_kgm2"]["value"]) == 3
    pm = p["metrics"]
    assert 3 < pm["tw_ratio"]["value"] < 15
    assert 0.1 < pm["hover_throttle"]["value"] < 0.6
    assert pm["hover_endurance_min"]["value"] > 0 and pm["rotor_count"]["value"] == 4
    assert pm["thrust_total_max_n"]["value"] == pytest.approx(4 * pm["thrust_max_n"]["value"])
    assert all(x["basis"] == "estimate" for k, x in {**m["metrics"], **pm}.items() if k != "rotor_count")
    assert len(p["extra"]["rotors"]) == 4
    # physics sanity: hover thrust equals weight
    W = m["metrics"]["mass_kg"]["value"] * 9.80665
    from opencae.drone.analyses import propulsion as PR
    h = p["extra"]["hover"]
    assert 4 * h["thrust"] == pytest.approx(W, rel=1e-6)
    n, i, t = PR.motor_prop(14.8, 2400, 0.07, 1.0, 0.11, 0.045, 0.1295, 1.225)
    assert (14.8 - n * 60 / 2400) / 0.07 == pytest.approx(i)
    assert (i - 1.0) * n * 60 / 2400 == pytest.approx(0.045 * 1.225 * n ** 3 * 0.1295 ** 5, rel=1e-6)


def test_checks_never_pass_on_estimates():
    spec = {"type": "multirotor", "requirements": [
        {"id": "A", "label": "a", "metric": "propulsion.tw_ratio", "op": ">=", "value": 2},
        {"id": "B", "label": "b", "metric": "propulsion.tw_ratio", "op": ">=", "value": 20},
        {"id": "C", "label": "c", "metric": "propulsion.hover_throttle", "op": "<=", "value": 0.6},
        {"id": "D", "label": "d", "metric": "propulsion.hover_current_a", "op": "<", "value": 99}]}
    mk = lambda v, b: {"value": v, "unit": "", "status": "ok", "basis": b, "label": ""}
    an = {"propulsion": {"status": "ok", "summary": "", "metrics": {
        "tw_ratio": mk(6.0, "estimate"), "hover_throttle": mk(0.3, "datasheet"),
        "hover_current_a": mk(5.0, "")}}}
    rows, verdict = checks.evaluate(spec, an)
    st = {r["id"]: r["status"] for r in rows}
    assert st == {"A": "conditional", "B": "fail", "C": "pass", "D": "conditional"}
    assert verdict == "not ready"
    rows, verdict = checks.evaluate({**spec, "requirements": [spec["requirements"][0], spec["requirements"][2]]}, an)
    assert verdict == "conditional"
    an["propulsion"]["metrics"]["tw_ratio"] = mk(6.0, "measured")
    rows, verdict = checks.evaluate({**spec, "requirements": [spec["requirements"][0], spec["requirements"][2]]}, an)
    assert verdict == "ready for physical tests"
    # the example (all estimates) never reaches a pass
    assert checks.verdict([{"status": "conditional"}, {"status": "pass"}]) == "conditional"


def test_checks_in_pipeline_never_pass(quad):
    out = P.run_project(quad, only=LIGHT)
    assert {c["status"] for c in out["checks"]} <= {"conditional", "not_evaluated", "fail"}
    assert out["verdict"] in ("conditional", "not ready")


def test_pipeline_cache_skips_unchanged(quad):
    first = P.run_project(quad, only=LIGHT)
    assert not first["analyses"]["mass"]["extra"]["cached"]
    second = P.run_project(quad, only=LIGHT)
    assert second["analyses"]["mass"]["extra"]["cached"] and second["analyses"]["propulsion"]["extra"]["cached"]
    assert P.status(quad)["variants"]["baseline"]["analyses"]["mass"]["stale"] is False
    # change a propulsion input: mass stays cached, propulsion reruns
    s = S.load(quad)
    s["propulsion"]["battery"]["capacity_mah"]["value"] = 2000
    S.save(s, quad)
    third = P.run_project(quad, only=LIGHT)
    assert third["analyses"]["mass"]["extra"]["cached"] and not third["analyses"]["propulsion"]["extra"]["cached"]
    forced = P.run_project(quad, only=LIGHT, force=True)
    assert not forced["analyses"]["mass"]["extra"]["cached"]
    # the stored result goes stale when drone.json changes afterwards
    s["propulsion"]["battery"]["capacity_mah"]["value"] = 2500
    S.save(s, quad)
    stt = P.status(quad)["variants"]["baseline"]["analyses"]
    assert stt["propulsion"]["stale"] is True and stt["mass"]["stale"] is False
    assert (quad / "results" / "baseline" / "geometry.npz").exists()
    assert not (quad / "results" / "baseline" / "_scratch").exists()
    assert P.clean(quad) >= 0
    assert P.clean(quad, results=True) > 0 and not (quad / "results").exists()


def test_new_project_and_iterate(tmp_path, quad):
    proj = tmp_path / "mine"
    p = P.new_project(proj, quad / "airframe.stl", "Mine", "multirotor")
    assert p == proj / "drone.json" and (proj / "airframe.stl").exists()
    spec = S.load(proj)
    assert S.validate(spec) == [] and spec["roles"]["arm"]
    # fill the template from the quad example and iterate
    full = S.load(quad)
    for k in ("materials", "role_materials", "mass", "propulsion", "aero", "mission"):
        spec[k] = full[k]
    S.save(spec, proj)
    before = (proj / "drone.json").read_text()
    res = P.iterate(proj, {"propulsion.battery.capacity_mah": 3000}, "big_battery", only=LIGHT)
    assert (proj / "drone.json").read_text() == before
    row = next(r for r in res["compare"] if r["metric"] == "propulsion.hover_endurance_min")
    assert row["variant"] > row["baseline"] * 1.9 and row["better"] == "higher"
    assert res["variant"] == "big_battery" and P.variants(proj) == ["baseline", "big_battery"]
    assert res["results"]["spec_used"]["propulsion"]["battery"]["capacity_mah"]["value"] == 3000
    with pytest.raises(KeyError):
        P.iterate(proj, {"propulsion.battery.bogus": 1}, "bad", only=LIGHT)
    assert (proj / "drone.json").read_text() == before and "bad" not in P.variants(proj)


def test_new_project_leaves_unknowns_missing(tmp_path, quad):
    proj = tmp_path / "blank"
    P.new_project(proj, quad / "airframe.stl", "Blank", "tailsitter")
    spec = S.load(proj)
    assert json.dumps(spec)  # valid JSON
    for k in ("kv", "r_ohm", "i0_a", "i_max_a", "mass_g"):
        assert spec["propulsion"]["motor"][k] is None
    assert all(v is None for v in spec["propulsion"]["battery"].values())
    assert all(v is None for v in spec["materials"]["my_material"].values() if not isinstance(v, str))
    assert all(v is None for v in spec["mass"]["parts"].values())
    out = P.run_project(proj, only=LIGHT)
    assert out["analyses"]["mass"]["status"] == "not_evaluated" and out["analyses"]["mass"]["missing"]
    pr = out["analyses"]["propulsion"]
    assert pr["status"] == "not_evaluated" and "propulsion.motor.kv" in pr["missing"] and not pr["metrics"]
    assert "propulsion.battery.capacity_mah" in pr["missing"]
    assert all(c["status"] == "not_evaluated" for c in out["checks"])
    assert out["verdict"] == "not ready"


def test_unavailable_module_is_not_evaluated(quad, monkeypatch):
    from opencae.drone import analyses
    monkeypatch.setitem(analyses.IMPORT_ERRORS, "flight", "ImportError: gone")
    monkeypatch.delitem(analyses.REGISTRY, "flight", raising=False)
    monkeypatch.setattr(analyses, "refresh", lambda: analyses.REGISTRY)
    monkeypatch.setattr(P, "_refresh", lambda: analyses.REGISTRY)
    out = P.run_project(quad, only=["flight"])
    assert out["analyses"]["flight"]["status"] == "not_evaluated"
    assert "analysis module not available" in out["analyses"]["flight"]["summary"]


def test_k09l_project_status(tmp_path, monkeypatch):
    proj = tmp_path / "k09l"
    shutil.copytree(EX / "k09l", proj)
    spec = S.load(proj)
    assert spec["engine"] == "k09l" and S.validate(spec) == []
    st = P.status(proj)
    assert st["engine"] == "k09l"
    an = st["variants"]["Revision B"]["analyses"]
    for nid in ("fea_motor_tube", "fea_spar", "fea_foot", "thermal_mount", "cfd_cruise", "assembly_check",
                "flight_hover", "flight_transition", "component_screening"):
        assert nid in an and "status" in an[nid] and "stale" in an[nid]
    # a mocked run: the heavy analyses are never started here
    from opencae.engineering import study as ST
    calls = []

    def fake_run(self, rid, workdir=None, only_missing=True, progress=None, cancelled=lambda: False,
                 keep_cases=False):
        calls.append((self.variant(rid)["name"], only_missing))
        if progress:
            progress("fea_foot", 0, 1)
        return {"done": [], "errors": {}, "workdir": ""}

    monkeypatch.setattr(ST.Study, "run_analyses", fake_run)
    seen = []
    out = P.run_project(proj, progress=lambda m, f: seen.append(m))
    assert calls == [("Revision B", True)] and "fea_foot" in seen
    assert out["engine"] == "k09l" and "fea_foot" in out["analyses"] and out["checks"]
    assert (proj / "results" / "Revision B" / "results.json").exists()
    assert P.variants(proj) == ["Revision B"]
    with pytest.raises(ValueError):
        P.run_project(proj, overrides={"x": 1})
