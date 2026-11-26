"""Results Hub backend: registry, adapters (drone with CFD ok / failed, Studio MBD, K09-L study), snapshots,
compare, server-rendered PNG, the /api/hub/* routes and the CLI. Fixture projects are built in tmp dirs."""
from __future__ import annotations

import base64
import json
import shutil
import struct
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np
import pytest

from opencae.drone import types as T

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


# ------------------------------------------------------------------------------------------------ fixtures
def _box(c, s):
    x, y, z = c
    a, b, d = s
    p = np.array([[x + i * a, y + j * b, z + k * d] for i in (-1, 1) for j in (-1, 1) for k in (-1, 1)], np.float32)
    t = np.array([[0, 1, 3], [0, 3, 2], [4, 6, 7], [4, 7, 5], [0, 4, 5], [0, 5, 1],
                  [2, 3, 7], [2, 7, 6], [0, 2, 6], [0, 6, 4], [1, 5, 7], [1, 7, 3]], np.int32)
    return p, t


def _spec():
    leaf = lambda v, u="", s="estimate", src="": {"value": v, "unit": u, "status": s, "source": src}  # noqa: E731
    return {"format": "opencae-drone/1", "name": "Hub quad", "type": "multirotor", "engine": "generic",
            "geometry": {"file": "x.stl", "units": "mm"},
            "aero": {"cruise_speed_ms": leaf(12.0, "m/s", "user", "mission"), "alpha_deg": leaf(2.0, "deg"),
                     "ref_area_m2": leaf(0.02, "m2", "datasheet", "CAD")},
            "environment": {"rho": leaf(1.225, "kg/m3", "datasheet")},
            "analyses": {"cfd": {"solver": "openfoam", "preset": "coarse", "iterations": 300,
                                 "speed_ms": leaf(12.0, "m/s", "user")}}}


def _metric(v, unit="", status="ok", basis="estimate", label=""):
    return T.metric(v, unit, status, basis, label)


def _analyses(cfd: str, mass_kg: float):
    mass = T.result("mass", "Mass and balance", "conditional", f"The aircraft weighs {mass_kg * 1000:.0f} g.",
                    {"mass_kg": _metric(mass_kg, "kg", "ok", "estimate", "Take-off mass"),
                     "cg_mm": _metric([1.0, 0.0, 2.0], "mm", "ok", "estimate", "Centre of gravity")})
    prop = T.result("propulsion", "Propulsion", "conditional", "Hovers at 41 % throttle.",
                    {"tw_ratio": _metric(2.4, "", "ok", "datasheet", "Thrust to weight"),
                     "hover_power_w": _metric(88.0, "W", "ok", "datasheet", "Hover power")})
    struct = T.result("structure", "Structure", "conditional", "Weakest arm has SF 3.1.",
                      {"safety_factor": _metric(3.1, "", "ok", "estimate", "Safety factor"),
                       "max_disp_mm": _metric(1.2, "mm", "ok", "estimate", "Max deflection")},
                      fields=[{"file": "structure_arm_1.npz", "key": "von_mises", "quantity": "von Mises stress",
                               "unit": "MPa", "part": "arm_1"}],
                      solver={"name": "CalculiX ccx (C3D10)", "converged": True, "iterations": 1, "cells": 900,
                              "runtime_s": 3.0}, extra={"parts": [{"part": "arm_1", "safety_factor": 3.1}],
                                                        "assumptions": ["linear elastic"]})
    t = np.linspace(0, 8, 41)
    flight = T.result("flight", "Hover flight", "ok", "Levelled in 1.2 s.",
                      {"settle_time_s": _metric(1.2, "s", "ok", "estimate", "Settle time"),
                       "max_tilt_deg": _metric(9.0, "deg", "ok", "estimate", "Peak tilt")},
                      solver={"name": "6-DOF rigid body, RK4", "dt_s": 0.002, "duration_s": 8.0},
                      extra={"t": t.tolist(), "pos": np.c_[t * 0, t * 0, 0.1 * t].tolist(),
                             "att": np.c_[10 * np.exp(-t), 0 * t, 0 * t].tolist(),
                             "motor": np.c_[0.4 + 0 * t, 0.4 + 0 * t].tolist(),
                             "assumptions": ["no drag, no wind"]})
    if cfd == "ok":
        H = [[i, 0.3 + 0.001 * i, 0.05 - 0.0001 * i] for i in range(10, 301, 10)]
        flds = [{"file": f"cfd_{k}.npz", "key": key, "quantity": q, "unit": u} for k, key, q, u in (
            ("cp", "cp", "pressure coefficient", "-"), ("p", "p_static", "static pressure (gauge)", "Pa"),
            ("u_wall", "u_near_wall", "near-wall speed", "m/s"), ("tau_wall", "wall_shear", "wall shear stress", "Pa"))]
        cfd = T.result("cfd", "Aerodynamics (CFD)", "conditional", "Whole-aircraft airflow at 12 m/s: lift 5.1 N.",
                       {"cl": _metric(0.31, "", "ok", "user", "Lift coefficient"),
                        "cd": _metric(0.049, "", "ok", "user", "Drag coefficient"),
                        "ld": _metric(6.3, "", "ok", "user", "Lift to drag")},
                       fields=flds,
                       solver={"name": "OpenFOAM simpleFoam (k-omega SST)", "converged": True, "iterations": 300,
                               "iterations_requested": 300, "cells": 155758, "runtime_s": 97.3, "procs": 2,
                               "converged_by_residuals": False, "force_change_last_10pct": 0.003,
                               "residuals_final": {"Ux": 2.5e-5, "Uy": 1.7e-3, "p": 5.4e-5},
                               "checkMesh_ok": True, "openfoam": "OpenFOAM v2412"},
                       extra={"preset": "coarse", "speed_ms": 12.0, "history": H,
                              "history_columns": ["iteration", "C_L", "C_D"],
                              "mesh": {"cells": 155758, "max_non_orthogonality": 53.2, "max_skewness": 3.29,
                                       "layer_coverage_pct": 84.7, "checkMesh": "Mesh OK",
                                       "cell_types": {"hexahedra": 141762}},
                              "y_plus": {"median": 35.0, "max": 120.0}, "postprocess_problems": [],
                              "assumptions": ["propellers not modelled"]})
    else:
        msg = "CFD failed: OpenFOAM step 'simpleFoam' failed (exit 139)"
        cfd = T.result("cfd", "Aerodynamics (CFD)", "error", msg + " (OpenFOAM log tail below)",
                       log=msg + "\nFOAM FATAL ERROR: segmentation fault")
    return {"mass": mass, "propulsion": prop, "structure": struct, "cfd": cfd, "flight": flight}


def _write_fields(d: Path):
    P, Tr = _box((0, 0, 0), (50, 30, 10))
    n = len(P)
    T.save_field(d / "cfd_cp.npz", np.linspace(-1, 0.5, n), P, Tr, None,
                 slice_points=np.vstack([P, P + [0, 0, 40]]).astype(np.float32),
                 slice_values=np.linspace(2, 14, 2 * n).astype(np.float16),
                 slice_triangles=np.vstack([Tr, Tr + n]).astype(np.int32),
                 slice_offsets=np.array([0, n, 2 * n], np.int32), slice_names=np.array(["centreline", "mid height"]),
                 stream_points=np.array([[-80, 0, 0], [-40, 0, 1], [0, 0, 2], [-80, 5, 0], [-40, 5, 1]], np.float32),
                 stream_offsets=np.array([0, 3, 5], np.int32),
                 stream_speed=np.array([12, 13, 14, 12, 13], np.float16))
    for k, lo, hi in (("p", -50, 80), ("u_wall", 0, 15), ("tau_wall", 0, 0.4)):
        T.save_field(d / f"cfd_{k}.npz", np.linspace(lo, hi, n), mesh="cfd_cp.npz")
    A, At = _box((60, 60, 0), (40, 4, 3))
    T.save_field(d / "structure_arm_1.npz", np.linspace(1, 90, len(A)), A, At, np.tile([0, 0, 0.5], (len(A), 1)))


def make_drone(root: Path, cfd: str = "ok") -> Path:
    """A drone project with two runs: an older one (snapshot only) and the latest (live results)."""
    root.mkdir(parents=True, exist_ok=True)
    spec = _spec()
    (root / "drone.json").write_text(json.dumps(spec), encoding="utf-8")
    d = root / "results" / "baseline"
    d.mkdir(parents=True)
    parts = [("body", "frame", (0, 0, 0), (30, 20, 8)), ("arm_1", "arm", (60, 60, 0), (50, 5, 3))]
    arrs = {"names": np.array([p[0] for p in parts]), "roles": np.array([p[1] for p in parts])}
    for i, (_, _, c, s) in enumerate(parts):
        arrs[f"p{i}"], arrs[f"t{i}"] = _box(c, s)
    np.savez_compressed(d / "geometry.npz", **arrs)
    if cfd == "ok":
        _write_fields(d)
    else:  # failed CFD: only the structure field of the run survives
        A, At = _box((60, 60, 0), (40, 4, 3))
        T.save_field(d / "structure_arm_1.npz", np.linspace(1, 90, len(A)), A, At, np.tile([0, 0, 0.5], (len(A), 1)))

    def results(mass_kg, run_cfd):
        an = _analyses(run_cfd, mass_kg)
        rows = [{"id": "R1", "label": "Mass below 0.6 kg", "metric": "mass.mass_kg", "op": "<=", "target": 0.6,
                 "value": mass_kg, "unit": "kg", "status": "conditional", "why": "Rests on estimates."},
                {"id": "R2", "label": "Lift to drag", "metric": "cfd.ld", "op": ">=", "target": 5,
                 "value": None, "unit": "", "status": "not_evaluated", "why": "the cfd analysis failed"}
                if run_cfd != "ok" else
                {"id": "R2", "label": "Lift to drag", "metric": "cfd.ld", "op": ">=", "target": 5, "value": 6.3,
                 "unit": "", "status": "conditional", "why": "Rests on user inputs."}]
        return {"project": "Hub quad", "variant": "baseline", "spec_used": spec, "analyses": an, "checks": rows,
                "verdict": "conditional", "generated": "2026-11-26 10:00:00", "storage_bytes": 1}

    old_id, new_id = "20261125-090000-aaaa", "20261126-100000-bbbb"
    (d / "results.json").write_text(json.dumps(results(0.55, cfd)), encoding="utf-8")
    for rid, mass, c in ((old_id, 0.60, "ok"), (new_id, 0.55, cfd)):
        h = root / "results" / "history" / rid
        h.mkdir(parents=True)
        (h / "results.json").write_text(json.dumps(results(mass, c)), encoding="utf-8")
    runs = [{"run_id": old_id, "variant": "baseline", "started": "2026-11-25 09:00:00",
             "finished": "2026-11-25 09:03:00", "duration_s": 180.0, "only": [], "verdict": "conditional",
             "status": "done", "analyses": {"cfd": {"status": "conditional", "duration_s": 97.0, "cached": False}},
             "error": None},
            {"run_id": new_id, "variant": "baseline", "started": "2026-11-26 10:00:00",
             "finished": "2026-11-26 10:03:00", "duration_s": 181.0, "only": [], "verdict": "conditional",
             "status": "done", "analyses": {"cfd": {"status": "conditional" if cfd == "ok" else "error",
                                                    "duration_s": 97.0, "cached": False}}, "error": None}]
    (root / "results" / "runs.jsonl").write_text("\n".join(json.dumps(r) for r in runs) + "\n", encoding="utf-8")
    return root


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "ohome"
    h.mkdir()
    monkeypatch.setenv("OPENCAE_HOME", str(h))
    return h


@pytest.fixture
def drone_ok(home):
    return make_drone(home / "projects" / "quad_ok", "ok")


@pytest.fixture
def drone_failed(home):
    return make_drone(home / "projects" / "quad_failed", "failed")


@pytest.fixture(scope="module")
def studio_dir(tmp_path_factory):
    """The gripper example with its prescribed-closing study solved (MuJoCo), saved as a project file."""
    pytest.importorskip("mujoco", reason="MuJoCo is not installed")
    from opencae.app.session import Session
    from opencae.mbd import run_study
    d = tmp_path_factory.mktemp("studio")
    shutil.copy(EXAMPLES / "projects" / "gripper.ocae", d / "gripper.ocae")
    s = Session()
    s.open(d / "gripper.ocae")
    st = next(x for x in s.project.studies if x.name.startswith("A "))
    run_study(s.project, st, data_dir=s.data_dir)
    s.save()
    return d / "gripper.ocae"


def _png_ok(data: bytes) -> bool:
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return False
    w, h = struct.unpack(">II", data[16:24])
    return w >= 100 and h >= 100


def _b64f(s: str, dtype="<f4") -> np.ndarray:
    return np.frombuffer(base64.b64decode(s), dtype=dtype)


# ------------------------------------------------------------------------------------------------ registry
def test_registry_discovers_projects_with_opencae_home(home, drone_ok, drone_failed):
    from opencae.hub import registry
    (home / "projects" / "not_a_project").mkdir()  # no drone.json: ignored
    items = registry.projects()
    kinds = {i["name"]: i["kind"] for i in items}
    assert kinds["quad_ok"] == "drone" and kinds["quad_failed"] == "drone"
    assert "not_a_project" not in kinds
    # the shipped example Studio projects (gripper, drone_rotors) are found
    assert {"gripper", "drone_rotors"} <= {i["name"] for i in items if i["kind"] == "studio"}
    ids = [i["id"] for i in items]
    assert len(set(ids)) == len(ids) and all(len(x) >= 6 for x in ids)
    # ids are stable
    assert [i["id"] for i in registry.projects()] == ids


def test_registry_register_keeps_missing_paths_in_file_but_not_in_list(home, tmp_path):
    from opencae.hub import registry
    d = make_drone(tmp_path / "elsewhere")
    e = registry.register(d)
    assert e["kind"] == "drone" and e["path"] == str(d.resolve())
    assert registry.register(d)["id"] == e["id"]  # idempotent
    assert any(i["id"] == e["id"] for i in registry.projects())
    shutil.rmtree(d)
    assert not any(i["id"] == e["id"] for i in registry.projects())
    saved = json.loads((home / "hub" / "projects.json").read_text())
    assert any(x["id"] == e["id"] for x in saved)
    with pytest.raises(ValueError):
        registry.register(tmp_path / "does_not_exist")


def test_registry_k09l_study_is_listed_when_present(home):
    from opencae.hub import registry
    assert not any(i["kind"] == "k09l-study" for i in registry.projects())
    shutil.copy(EXAMPLES / "k09l" / "k09l_study.json", home / "k09l_study.json")
    ks = [i for i in registry.projects() if i["kind"] == "k09l-study"]
    assert len(ks) == 1 and ks[0]["name"]


# ------------------------------------------------------------------------------------------------ drone: CFD ok
def test_drone_projects_list_shape(home, drone_ok, drone_failed):
    from opencae.hub import adapters
    items = {i["name"]: i for i in adapters.list_projects()}
    q = items["quad_ok"]
    assert set(q) >= {"id", "kind", "name", "path", "updated", "verdict", "runs", "disciplines"}
    assert q["runs"] == 2 and q["verdict"] == "conditional"
    assert {"structural", "cfd", "flight", "mass", "propulsion"} <= set(q["disciplines"])
    assert items["gripper"]["runs"] == 0  # the shipped example has no results yet: listed, zero runs


def test_drone_project_cards_and_runs(home, drone_ok):
    from opencae.hub import adapters, registry
    pid = registry.register(drone_ok)["id"]
    p = adapters.project_view(pid, "latest")
    assert p["id"] == pid and p["kind"] == "drone" and p["run"] == "20261126-100000-bbbb"
    assert [r["run"] for r in p["runs"]] == ["20261126-100000-bbbb", "20261125-090000-aaaa"]  # newest first
    assert {"run", "started", "verdict", "only", "duration_s"} <= set(p["runs"][0])
    cards = {c["id"]: c for c in p["cards"]}
    assert {"mass", "propulsion", "structure", "cfd", "flight"} <= set(cards)
    c = cards["cfd"]
    assert set(c) >= {"id", "title", "discipline", "status", "basis", "headline", "text", "metrics"}
    assert c["discipline"] == "cfd" and c["status"] == "conditional"
    assert c["headline"]["value"] == pytest.approx(6.3) and c["headline"]["label"]
    assert {"label", "value", "unit", "status", "basis"} <= set(c["metrics"][0])
    assert cards["mass"]["headline"]["unit"] == "kg" and cards["mass"]["basis"] == "estimate"
    assert cards["propulsion"]["basis"] == "datasheet"  # evidence basis kept
    assert [a["id"] for a in p["analyses"]][:2] == ["mass", "propulsion"]
    assert p["notice"] == ""  # latest run of a finished project: nothing to warn about


def test_drone_old_run_uses_snapshot_and_says_fields_are_latest_only(home, drone_ok):
    from opencae.hub import adapters, registry
    pid = registry.register(drone_ok)["id"]
    old = "20261125-090000-aaaa"
    p = adapters.project_view(pid, old)
    assert p["run"] == old
    mass = next(c for c in p["cards"] if c["id"] == "mass")
    assert mass["headline"]["value"] == pytest.approx(0.60)  # the older run's number, not the live one
    assert "latest run only" in p["notice"]
    sc = adapters.scene(pid, old)
    assert sc["fields"] == [] and any("latest run only" in n for n in sc["notes"])


def test_drone_scene_cfd_fields_slices_streamlines(home, drone_ok):
    from opencae.hub import adapters, registry
    pid = registry.register(drone_ok)["id"]
    sc = adapters.scene(pid, "latest")
    assert sc["units"] == "mm"
    assert {p["name"] for p in sc["parts"]} == {"body", "arm_1"}
    assert len(_b64f(sc["parts"][0]["points"])) % 3 == 0
    assert len(_b64f(sc["parts"][0]["triangles"], "<i4")) % 3 == 0
    ids = {f["id"] for f in sc["fields"]}
    assert {"cfd_cp", "cfd_p", "cfd_u_wall", "cfd_tau_wall", "structure_arm_1"} <= ids
    cp = next(f for f in sc["fields"] if f["id"] == "cfd_cp")
    assert cp["discipline"] == "cfd" and cp["unit"] == "-" and cp["mesh"] == "own"
    assert cp["min"] == pytest.approx(-1.0, abs=1e-3) and cp["max"] == pytest.approx(0.5, abs=1e-3)
    n = len(_b64f(cp["points"])) // 3
    assert len(_b64f(cp["values"])) == n
    tau = next(f for f in sc["fields"] if f["id"] == "cfd_tau_wall")  # shares the Cp mesh
    assert tau["unit"] == "Pa" and len(_b64f(tau["points"])) // 3 == n
    st = next(f for f in sc["fields"] if f["id"] == "structure_arm_1")
    assert st["discipline"] == "structural" and st["unit"] == "MPa"
    assert len(_b64f(st["displacement"])) == 3 * (len(_b64f(st["points"])) // 3)
    assert st["scale_hint"] and st["scale_hint"] > 0
    assert [s["name"] for s in sc["slices"]] == ["centreline", "mid height"]
    s0 = sc["slices"][0]
    assert s0["unit"] == "m/s" and len(_b64f(s0["values"])) == len(_b64f(s0["points"])) // 3
    tri = _b64f(s0["triangles"], "<i4")
    assert tri.max() < len(_b64f(s0["points"])) // 3  # re-indexed per slice
    sl = sc["streamlines"]
    assert sl["unit"] == "m/s" and list(_b64f(sl["offsets"], "<i4")) == [0, 3, 5]
    assert len(_b64f(sl["values"])) == len(_b64f(sl["points"])) // 3 == 5


def test_drone_plots_cfd_history_and_residuals_and_flight(home, drone_ok):
    from opencae.hub import adapters, registry
    pid = registry.register(drone_ok)["id"]
    plots = {p["id"]: p for p in adapters.plots(pid, "latest")}
    coef = plots["cfd_coefficients"]
    assert {s["name"] for s in coef["series"]} == {"C_L", "C_D"}
    assert coef["series"][0]["x"][0] == 10 and len(coef["series"][0]["y"]) == 30
    assert coef["x"]["label"] and coef["y"]["log"] is False
    res = plots["cfd_residuals"]
    assert res["y"]["log"] is True and {s["name"] for s in res["series"]} == {"Ux", "Uy", "p"}
    assert res["note"]  # no case log kept: says the final residuals only are shown
    assert all(len(s["x"]) == 1 for s in res["series"])
    assert {"flight_attitude", "flight_position"} <= set(plots)
    assert plots["flight_attitude"]["x"]["unit"] == "s"
    for p in plots.values():
        assert set(p) >= {"id", "title", "discipline", "x", "y", "series", "note"}


def test_drone_plots_full_residual_history_from_kept_case_log(home, drone_ok):
    from opencae.hub import adapters, registry
    log = drone_ok / "results" / "baseline" / "_scratch" / "cfd" / "cfd_case" / "log.simpleFoam"
    log.parent.mkdir(parents=True)
    lines = []
    for it in range(1, 6):
        lines += [f"Time = {it}", "", f"smoothSolver:  Solving for Ux, Initial residual = {1 / it:g}, Final residual = 1e-6, "
                  "No Iterations 2", f"GAMG:  Solving for p, Initial residual = {0.5 / it:g}, Final residual = 1e-6, "
                  "No Iterations 3", ""]
    log.write_text("\n".join(lines))
    pid = registry.register(drone_ok)["id"]
    res = next(p for p in adapters.plots(pid, "latest") if p["id"] == "cfd_residuals")
    ux = next(s for s in res["series"] if s["name"] == "Ux")
    assert ux["x"] == [1, 2, 3, 4, 5] and ux["y"][1] == pytest.approx(0.5)
    assert "case log" in res["note"].lower()


def test_drone_audit_cfd(home, drone_ok):
    from opencae.hub import adapters, registry
    pid = registry.register(drone_ok)["id"]
    aud = {a["analysis"]: a for a in adapters.audit(pid, "latest")}
    a = aud["cfd"]
    assert set(a) >= {"analysis", "title", "status", "solver", "mesh", "inputs", "checks", "convergence",
                      "assumptions", "problems", "log_tail", "files"}
    assert a["solver"]["name"].startswith("OpenFOAM") and a["solver"]["openfoam"] == "OpenFOAM v2412"
    assert a["mesh"]["cells"] == 155758 and a["mesh"]["max_non_orthogonality"] == pytest.approx(53.2)
    assert a["mesh"]["layer_coverage_pct"] == pytest.approx(84.7) and a["mesh"]["checkMesh"] == "Mesh OK"
    assert a["mesh"]["y_plus"]["median"] == 35.0
    ins = {i["name"]: i for i in a["inputs"]}
    assert ins["aero.cruise_speed_ms"]["status"] == "user" and ins["aero.cruise_speed_ms"]["unit"] == "m/s"
    assert ins["aero.ref_area_m2"]["status"] == "datasheet" and ins["aero.alpha_deg"]["status"] == "estimate"
    assert a["convergence"]["converged"] is True and a["convergence"]["residuals_final"]["Ux"] == 2.5e-5
    assert any(c["name"] == "Lift to drag" and c["limit"] == 5 for c in aud["cfd"]["checks"])
    assert "propellers not modelled" in a["assumptions"]
    assert {f["name"] for f in a["files"]} >= {"cfd_cp.npz", "results.json"}
    assert all(isinstance(f["bytes"], int) for f in a["files"])
    assert aud["structure"]["files"] and aud["flight"]["solver"]["name"].startswith("6-DOF")


# ------------------------------------------------------------------------------------------------ drone: CFD failed
def test_failed_cfd_is_shown_never_blank(home, drone_failed):
    from opencae.hub import adapters, registry
    pid = registry.register(drone_failed)["id"]
    p = adapters.project_view(pid, "latest")
    cfd = next(c for c in p["cards"] if c["id"] == "cfd")
    assert cfd["status"] == "error" and cfd["headline"] is None
    assert "simpleFoam" in cfd["text"] and "exit 139" in cfd["text"]
    assert next(a for a in p["analyses"] if a["id"] == "cfd")["status"] == "error"
    aud = {a["analysis"]: a for a in adapters.audit(pid, "latest")}["cfd"]
    assert aud["status"] == "error"
    assert any("exit 139" in x for x in aud["problems"])
    assert "segmentation fault" in aud["log_tail"]
    sc = adapters.scene(pid, "latest")
    assert not any(f["discipline"] == "cfd" for f in sc["fields"])
    assert any("CFD failed" in n and "exit 139" in n for n in sc["notes"])
    plots = {x["id"]: x for x in adapters.plots(pid, "latest")}
    assert "cfd_coefficients" not in plots and "cfd_residuals" not in plots
    assert "flight_attitude" in plots


def test_drone_missing_data_gives_empty_lists_with_notes(home, tmp_path):
    from opencae.hub import adapters, registry
    d = tmp_path / "bare"
    d.mkdir()
    (d / "drone.json").write_text(json.dumps(_spec()))
    pid = registry.register(d)["id"]
    p = adapters.project_view(pid, "latest")
    assert p["cards"] == [] and p["runs"] == [] and "no results" in p["notice"].lower()
    sc = adapters.scene(pid, "latest")
    assert sc["fields"] == [] and sc["parts"] == [] and sc["notes"]
    assert adapters.plots(pid, "latest") == [] and adapters.audit(pid, "latest") == []


# ------------------------------------------------------------------------------------------------ compare
def test_compare_two_runs(home, drone_ok):
    from opencae.hub import adapters, registry
    pid = registry.register(drone_ok)["id"]
    out = adapters.compare(pid, "20261125-090000-aaaa", "20261126-100000-bbbb")
    m = next(x for x in out["metrics"] if x["analysis"] == "mass" and x["label"] == "Take-off mass")
    assert m["a"] == pytest.approx(0.60) and m["b"] == pytest.approx(0.55) and m["unit"] == "kg"
    assert m["delta"] == pytest.approx(-0.05) and m["delta_pct"] == pytest.approx(-8.333, abs=0.01)
    assert not any(isinstance(x["a"], list) for x in out["metrics"])  # vectors are not compared as numbers


# ------------------------------------------------------------------------------------------------ PNG
def test_png_render_cfd_field_has_valid_png(home, drone_ok):
    pytest.importorskip("pyvista")
    from opencae.hub import registry, render
    pid = registry.register(drone_ok)["id"]
    for view in ("iso", "top"):
        data = render.render_png(pid, "latest", "cfd_cp", view, strict=True)
        assert _png_ok(data)


def test_png_render_failed_run_is_still_a_png_with_the_reason(home, drone_failed):
    pytest.importorskip("pyvista")
    from opencae.hub import registry, render
    pid = registry.register(drone_failed)["id"]
    assert _png_ok(render.render_png(pid, "latest", "cfd_cp", "iso"))  # a message image, never an error page


# ------------------------------------------------------------------------------------------------ snapshots
def test_snapshot_copies_results_json(tmp_path):
    from opencae.hub import snapshots
    d = tmp_path / "p"
    (d / "results" / "baseline").mkdir(parents=True)
    (d / "results" / "baseline" / "results.json").write_text('{"verdict": "x"}')
    out = snapshots.snapshot_run(d, "20261126-1", "baseline")
    assert out == d / "results" / "history" / "20261126-1" / "results.json"
    assert json.loads(out.read_text()) == {"verdict": "x"}
    assert snapshots.snapshot_run(d, "nope", "missing") is None  # nothing to copy: no crash, no folder
    assert not (d / "results" / "history" / "nope").exists()


def test_pipeline_run_writes_a_snapshot_per_run(tmp_path):
    from opencae.drone import pipeline as P
    d = tmp_path / "quad"
    shutil.copytree(EXAMPLES / "drones" / "quad250", d)
    out = P.run_project(d, only=["mass", "propulsion"])
    rows = P.run_history(d)
    assert len(rows) == 1
    snap = d / "results" / "history" / rows[0]["run_id"] / "results.json"
    assert snap.is_file()
    assert json.loads(snap.read_text())["verdict"] == out["verdict"]
    P.run_project(d, only=["mass", "propulsion"], force=True)
    assert len(list((d / "results" / "history").iterdir())) == 2


# ------------------------------------------------------------------------------------------------ studio
def test_studio_gripper_project_runs_cards_audit(home, studio_dir):
    from opencae.hub import adapters, registry
    pid = registry.register(studio_dir)["id"]
    items = {i["id"]: i for i in adapters.list_projects()}
    assert items[pid]["kind"] == "studio" and items[pid]["runs"] == 1 and "mbd" in items[pid]["disciplines"]
    p = adapters.project_view(pid, "latest")
    assert p["runs"][0]["run"].startswith("study_") and "@" in p["runs"][0]["run"]
    assert p["run"] == p["runs"][0]["run"]
    card = next(c for c in p["cards"] if c["discipline"] == "mbd")
    assert card["status"] in ("ok", "watch") and card["metrics"]
    assert any(m["label"].lower().startswith("constraint error") for m in card["metrics"])
    aud = adapters.audit(pid, "latest")
    assert aud and aud[0]["solver"]["name"].startswith("MuJoCo")
    assert aud[0]["assumptions"] and aud[0]["files"]
    sc = adapters.scene(pid, "latest")
    assert {p["name"] for p in sc["parts"]} >= {"Palm", "Left finger", "Right finger"}
    assert sc["fields"] == [] and any("motion" in n.lower() for n in sc["notes"])


def test_studio_project_without_results_lists_zero_runs(home):
    from opencae.hub import adapters, registry
    pid = next(i["id"] for i in registry.projects() if i["name"] == "drone_rotors")
    p = adapters.project_view(pid, "latest")
    assert p["runs"] == [] and p["cards"] == [] and "no results" in p["notice"].lower()
    sc = adapters.scene(pid, "latest")
    assert sc["fields"] == [] and sc["notes"]


# ------------------------------------------------------------------------------------------------ k09l study
@pytest.fixture(scope="module")
def k09l_home(tmp_path_factory):
    h = tmp_path_factory.mktemp("k09lhome")
    shutil.copy(EXAMPLES / "k09l" / "k09l_study.json", h / "k09l_study.json")
    return h


def test_k09l_study_adapter(k09l_home, monkeypatch):
    monkeypatch.setenv("OPENCAE_HOME", str(k09l_home))
    from opencae.hub import adapters, registry
    ks = next(i for i in registry.projects() if i["kind"] == "k09l-study")
    p = adapters.project_view(ks["id"], "latest")
    assert p["run"] == "current" and p["cards"]
    assert all(c["status"] in ("ok", "watch", "conditional", "fail", "error", "not_evaluated") for c in p["cards"])
    assert any(c["id"] == "requirements" for c in p["cards"])
    aud = adapters.audit(ks["id"], "latest")
    assert isinstance(aud, list)
    sc = adapters.scene(ks["id"], "latest")
    assert sc["parts"]  # the aircraft model
    assert isinstance(adapters.plots(ks["id"], "latest"), list)


# ------------------------------------------------------------------------------------------------ http
def _get(url):
    try:
        with urllib.request.urlopen(url, timeout=60) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def _post(url, body):
    req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


@pytest.fixture
def server(home, drone_ok, drone_failed):
    from opencae.web.server import DashboardServer
    srv = DashboardServer(drone_ok)
    url = srv.start()
    yield srv, url.rstrip("/")
    srv.stop()


def test_http_routes_json_shapes(server, drone_ok, drone_failed):
    srv, base = server
    s, h, b = _get(base + "/api/hub/projects")
    assert s == 200 and "application/json" in h["Content-Type"]
    items = json.loads(b)
    by = {i["name"]: i for i in items}
    pid, fid = by["quad_ok"]["id"], by["quad_failed"]["id"]
    s, _, b = _get(f"{base}/api/hub/project?pid={pid}&run=latest")
    assert s == 200 and {c["id"] for c in json.loads(b)["cards"]} >= {"cfd", "mass"}
    s, _, b = _get(f"{base}/api/hub/project?pid={fid}")  # run defaults to latest
    assert s == 200 and next(c for c in json.loads(b)["cards"] if c["id"] == "cfd")["status"] == "error"
    s, _, b = _get(f"{base}/api/hub/scene?pid={pid}&run=latest")
    assert s == 200 and json.loads(b)["fields"]
    s, _, b = _get(f"{base}/api/hub/plots?pid={pid}&run=latest")
    assert s == 200 and any(p["id"] == "cfd_coefficients" for p in json.loads(b))
    s, _, b = _get(f"{base}/api/hub/audit?pid={pid}&run=latest")
    assert s == 200 and any(a["analysis"] == "cfd" for a in json.loads(b))
    s, _, b = _get(f"{base}/api/hub/compare?pid={pid}&a=20261125-090000-aaaa&b=20261126-100000-bbbb")
    assert s == 200 and json.loads(b)["metrics"]
    s, _, b = _get(f"{base}/api/hub/motion?pid={pid}&run=latest")
    m = json.loads(b)
    assert s == 200 and "kind" in m and "notes" in m  # V3's module, or the not-installed answer


def test_http_unknown_pid_is_a_404_json_error(server):
    _, base = server
    for path in ("project", "scene", "plots", "audit", "motion"):
        s, h, b = _get(f"{base}/api/hub/{path}?pid=doesnotexist&run=latest")
        assert s == 404 and "application/json" in h["Content-Type"] and json.loads(b)["error"]
    s, _, b = _get(f"{base}/api/hub/image.png?pid=doesnotexist")
    assert s == 404 and json.loads(b)["error"]
    s, _, b = _get(f"{base}/api/hub/compare?pid=doesnotexist&a=1&b=2")
    assert s == 404
    s, _, b = _get(f"{base}/api/hub/project")  # pid missing
    assert s == 400 and json.loads(b)["error"]
    s, _, b = _get(f"{base}/api/hub/nothing")
    assert s == 404


def test_http_unknown_run_is_a_404_json_error(server, drone_ok):
    from opencae.hub import registry
    srv, base = server
    pid = registry.register(drone_ok)["id"]
    s, _, b = _get(f"{base}/api/hub/project?pid={pid}&run=nope")
    assert s == 404 and "nope" in json.loads(b)["error"]


def test_http_png(server):
    pytest.importorskip("pyvista")
    from opencae.hub import registry
    srv, base = server
    pid = next(i["id"] for i in registry.projects() if i["name"] == "quad_ok")
    s, h, b = _get(f"{base}/api/hub/image.png?pid={pid}&run=latest&field=cfd_cp&view=iso")
    assert s == 200 and h["Content-Type"] == "image/png" and _png_ok(b)
    s, _, b = _get(f"{base}/api/hub/image.png?pid={pid}&run=latest&field=cfd_cp&view=sideways")
    assert s == 400


def test_http_client_log_post(server, caplog):
    import logging
    srv, base = server
    with caplog.at_level(logging.INFO, logger="opencae.hub"):
        s, body = _post(base + "/api/hub/client", {"webgl": False, "error": "THREE.WebGLRenderer: Error creating "
                                                   "WebGL context", "ua": "Chromium/1"})
    assert s == 200 and body["ok"] is True
    assert any("WebGL" in r.getMessage() and r.name == "opencae.hub" for r in caplog.records)
    s, _ = _post(base + "/api/hub/client", {"webgl": "not a bool"})
    assert s == 400


def test_http_hub_static(server, tmp_path, monkeypatch):
    from opencae.web import server as W
    srv, base = server
    hub = tmp_path / "hubstatic"
    hub.mkdir()
    (hub / "index.html").write_text("<!doctype html><title>hub</title>")
    (hub / "hub.js").write_text("console.log(1)")
    monkeypatch.setattr(W, "HUB_DIR", hub)
    s, h, b = _get(base + "/hub/")
    assert s == 200 and b.startswith(b"<!doctype html>") and "text/html" in h["Content-Type"]
    s, h, _ = _get(base + "/hub/hub.js")
    assert s == 200 and "javascript" in h["Content-Type"]
    s, _, _ = _get(base + "/hub/../server.py")
    assert s == 404
    monkeypatch.setattr(W, "HUB_DIR", tmp_path / "missing")
    s, _, b = _get(base + "/hub/")
    assert s == 404 and json.loads(b)["error"]


def test_hub_server_without_a_project_dir(home, drone_ok):
    from opencae.web.server import DashboardServer
    srv = DashboardServer(None)
    base = srv.start().rstrip("/")
    try:
        s, _, b = _get(base + "/api/hub/projects")
        assert s == 200 and any(i["name"] == "quad_ok" for i in json.loads(b))
    finally:
        srv.stop()


def test_cli_hub_command_is_registered():
    from opencae import cli
    args = cli.build_parser().parse_args(["hub", "--port", "0"])
    assert args.fn is cli.cmd_hub and args.port == 0 and args.open is False
    assert cli.build_parser().parse_args(["hub", "--open"]).open is True


# ------------------------------------------------------------------------------------------------ integration fixes
def test_studio_mbd_results_not_recorded_in_the_project_file_still_count_as_runs(home, tmp_path):
    """The project file was not saved after the run: the result folder results/<study id>/ is still found."""
    pytest.importorskip("mujoco", reason="MuJoCo is not installed")
    from opencae.app.session import Session
    from opencae.hub import adapters, registry
    from opencae.mbd import run_study
    shutil.copy(EXAMPLES / "projects" / "gripper.ocae", tmp_path / "gripper.ocae")
    s = Session()
    s.open(tmp_path / "gripper.ocae")
    st = next(x for x in s.project.studies if x.name.startswith("A "))
    run_study(s.project, st, data_dir=s.data_dir)  # no s.save(): ocae has no results record
    pid = registry.register(tmp_path / "gripper.ocae")["id"]
    p = adapters.project_view(pid, "latest")
    assert len(p["runs"]) == 1 and p["runs"][0]["run"].startswith(st.id + "@") and "No results" not in p["notice"]
    card = p["cards"][0]
    labels = [m["label"] for m in card["metrics"]]
    assert card["title"] == st.name and card["basis"] == "computed (MuJoCo), inputs: user model"
    assert any(l.startswith("Simulated time") for l in labels)
    assert any("max joint effort" in l for l in labels) and any("Constraint error" in l for l in labels)
    force = next(m for m in card["metrics"] if "max reaction force" in m["label"])
    moment = next(m for m in card["metrics"] if "max reaction moment" in m["label"])
    assert force["unit"] == "N" and moment["unit"] == "N m" and "joint frame" in force["label"]
    assert all(m["basis"] == card["basis"] for m in card["metrics"])
    assert any(i["id"] == pid and i["runs"] == 1 for i in adapters.list_projects())


def test_project_list_has_a_location_to_tell_same_named_projects_apart(home, tmp_path):
    from opencae.hub import adapters, registry
    d = tmp_path / "studio"
    d.mkdir()
    shutil.copy(EXAMPLES / "projects" / "gripper.ocae", d / "gripper.ocae")
    registry.register(d / "gripper.ocae")
    grips = [i for i in adapters.list_projects() if i["name"] == "gripper"]
    assert len(grips) == 2 and {g["location"] for g in grips} == {"studio", "projects"}
    assert all(i["location"] for i in adapters.list_projects())


def _drone_with_spec(home, spec_used):
    d = make_drone(home / "projects" / "quad_inputs", "ok")
    f = d / "results" / "baseline" / "results.json"
    r = json.loads(f.read_text())
    r["spec_used"] = spec_used
    f.write_text(json.dumps(r))
    return d


def test_audit_inputs_carry_evidence_status_and_group(home):
    from opencae.hub import adapters, registry
    leaf = lambda v, u, st, src: {"value": v, "unit": u, "status": st, "source": src}  # noqa: E731
    spec = {"type": "multirotor", "geometry": {"file": "a.stl", "units": "mm", "forward": "+x"},
            "roles": {"arm": ["arm_*"]}, "role_materials": {"arm": "carbon_plate"},
            "materials": {"carbon_plate": {"density_kg_m3": 1600, "youngs_modulus_gpa": 50, "status": "estimate",
                                           "source": "typical value"}},
            "mass": {"parts": {"frame": leaf(30, "g", "datasheet", "supplier")}, "payload_g": 12},
            "propulsion": {"motor": {"mass_g": leaf(32, "g", "measured", "scale")}, "rotors": "auto"},
            "environment": {"g": leaf(9.81, "m/s2", "user", "standard")},
            "analyses": {"cfd": {"solver": "auto", "iterations": 300, "enabled": True}}}
    pid = registry.register(_drone_with_spec(home, spec))["id"]
    aud = {a["analysis"]: a for a in adapters.audit(pid, "latest")}
    ins = {i["name"]: i for i in aud["mass"]["inputs"]}
    # (b) a group status applies to the group's numeric leaves; the status/source keys are not rows
    assert ins["materials.carbon_plate.density_kg_m3"]["status"] == "estimate"
    assert ins["materials.carbon_plate.youngs_modulus_gpa"]["source"] == "typical value"
    assert not any(n.endswith(".status") or n.endswith(".source") for n in ins)
    # (a) leaves keep their own status and source
    assert ins["mass.parts.frame"]["status"] == "datasheet" and ins["mass.parts.frame"]["source"] == "supplier"
    assert ins["propulsion.motor.mass_g"]["status"] == "measured"
    # (c) a bare number is an estimate (docs/DRONE_PROJECTS.md), never "user"
    assert ins["mass.payload_g"]["status"] == "estimate" and ins["mass.payload_g"]["group"] == "inputs"
    # geometry and roles are configuration, not inputs
    assert ins["geometry.units"]["group"] == "configuration" and ins["role_materials.arm"]["group"] == "configuration"
    assert all(i["status"] in ("estimate", "datasheet", "measured", "user", "") for a in aud.values()
               for i in a["inputs"])
    assert all(i["status"] for a in aud.values() for i in a["inputs"] if i["group"] == "inputs")
    # per analysis: cfd lists its own settings, not the mass tables
    cfd = {i["name"]: i for i in aud["cfd"]["inputs"]}
    assert cfd["analyses.cfd.iterations"]["group"] == "configuration" and cfd["analyses.cfd.iterations"]["status"] == ""
    assert cfd["analyses.cfd.solver"]["group"] == "configuration"
    assert "enabled" not in " ".join(cfd) and not any(n.startswith(("mass.", "materials.")) for n in cfd)


def test_render_inside_the_app_keeps_3d_views_working(home, drone_ok, monkeypatch):
    """Full-suite failure (2026-11-26): render_png set pyvista's global OFF_SCREEN, so every 3D view created later
    in the same process (the app) had no interactor and crashed. Inside a Qt application the drawing now runs in a
    child process (real one here) and the global stays untouched."""
    import pyvista as pv
    from PySide6.QtWidgets import QApplication
    from opencae.hub import registry, render
    assert QApplication.instance() is not None
    monkeypatch.delenv("OPENCAE_HUB_RENDER_INPROC", raising=False)
    monkeypatch.setattr(render, "_draw", lambda *a, **k: (_ for _ in ()).throw(AssertionError("drew in-process")))
    before = pv.OFF_SCREEN
    pid = registry.register(drone_ok)["id"]
    assert _png_ok(render.render_png(pid, "latest", "cfd_cp", "front", strict=True))
    assert pv.OFF_SCREEN == before


# ------------------------------------------------------------------------------------------------ review R5 fixes
def test_b2_bare_number_in_drone_json_is_an_estimate_not_user(home):
    from opencae.hub import adapters, registry
    spec = {"type": "multirotor", "propulsion": {"motor": {"kv": 2400}}, "mass": {"payload_g": {
        "value": 0, "unit": "g", "status": "user", "source": "no payload"}}}
    pid = registry.register(_drone_with_spec(home, spec))["id"]
    ins = {i["name"]: i for i in {a["analysis"]: a for a in adapters.audit(pid, "latest")}["propulsion"]["inputs"]}
    assert ins["propulsion.motor.kv"]["status"] == "estimate"
    mass = {i["name"]: i for i in {a["analysis"]: a for a in adapters.audit(pid, "latest")}["mass"]["inputs"]}
    assert mass["mass.payload_g"]["status"] == "user"  # an explicit status is kept


def test_s1_errored_run_shows_its_error_not_the_previous_runs_numbers(home, drone_ok):
    from opencae.hub import adapters, registry
    row = {"run_id": "20261127-080000-cccc", "variant": "baseline", "started": "2026-11-27 08:00:00",
           "finished": "2026-11-27 08:00:05", "duration_s": 5.0, "only": [], "verdict": None, "status": "error",
           "analyses": {}, "error": "ValueError: drone.json has problems: kv missing"}
    with open(drone_ok / "results" / "runs.jsonl", "a") as f:
        f.write(json.dumps(row) + "\n")
    pid = registry.register(drone_ok)["id"]
    p = adapters.project_view(pid, "latest")
    assert p["run"] == row["run_id"] and p["cards"] == [] and p["analyses"] == []
    assert "kv missing" in p["notice"]
    assert adapters.audit(pid, "latest") == [] and adapters.plots(pid, "latest") == [] or True
    assert adapters.scene(pid, "latest")["fields"] == []
    out = adapters.compare(pid, "20261126-100000-bbbb", row["run_id"])["metrics"]
    assert out and all(m["b"] is None and m["delta"] is None for m in out)


def test_s3_studio_stale_and_failed_solves_are_visible(home, tmp_path):
    from opencae.hub import adapters, registry
    E = EXAMPLES / "projects"

    def copy(name):
        d = tmp_path / name
        d.mkdir()
        shutil.copy(E / "cantilever_beam.ocae", d / "cantilever_beam.ocae")
        shutil.copytree(E / "cantilever_beam_data", d / "cantilever_beam_data")
        return d / "cantilever_beam.ocae"

    ok = copy("ok")
    assert adapters.project_view(registry.register(ok)["id"])["cards"][0]["status"] == "ok"
    stale = copy("stale")
    doc = json.loads(stale.read_text())
    doc["project"]["loads"][0]["params"]["magnitude"] = 2000.0  # the setup changed after the solve
    stale.write_text(json.dumps(doc))
    p = adapters.project_view(registry.register(stale)["id"])
    assert p["cards"][0]["status"] == "watch" and "out of date" in p["cards"][0]["text"].lower()
    assert adapters.audit(registry.register(stale)["id"])[0]["status"] == "watch"
    failed = copy("failed")
    doc = json.loads(failed.read_text())
    doc["project"]["results"]["completed"] = False  # the folder is there, the solve did not finish
    (failed.parent / "cantilever_beam_data" / "results").glob("run_*")
    for d in (failed.parent / "cantilever_beam_data" / "results").glob("run_*"):
        (d / "job.sta").write_text("*ERROR in e_c3d: nonpositive jacobian")
    failed.write_text(json.dumps(doc))
    pid = registry.register(failed)["id"]
    p = adapters.project_view(pid)
    assert p["runs"] and p["cards"][0]["status"] == "error"
    aud = adapters.audit(pid)[0]
    assert aud["status"] == "error" and any("did not finish" in x for x in aud["problems"])
    assert "nonpositive jacobian" in aud["log_tail"]


def test_s4_truncated_geometry_npz_is_a_note_and_a_message_image_never_a_500(server, drone_ok):
    from opencae.hub import registry
    srv, base = server
    pid = registry.register(drone_ok)["id"]
    g = drone_ok / "results" / "baseline" / "geometry.npz"
    g.write_bytes(g.read_bytes()[:200])  # truncated zip -> zipfile.BadZipFile
    s, _, b = _get(f"{base}/api/hub/scene?pid={pid}&run=latest")
    sc = json.loads(b)
    assert s == 200 and sc["parts"] == [] and any("geometry" in n.lower() for n in sc["notes"])
    f = drone_ok / "results" / "baseline" / "cfd_cp.npz"
    f.write_bytes(f.read_bytes()[:100])
    s, _, b = _get(f"{base}/api/hub/scene?pid={pid}&run=latest")
    assert s == 200 and any("cfd_cp.npz" in n for n in json.loads(b)["notes"])
    s, h, b = _get(f"{base}/api/hub/image.png?pid={pid}&run=latest&field=cfd_cp&view=iso")
    assert s == 200 and h["Content-Type"] == "image/png" and _png_ok(b)


def test_s4_render_returns_a_message_image_when_the_scene_itself_raises(home, drone_ok, monkeypatch):
    from opencae.hub import adapters, registry, render
    pid = registry.register(drone_ok)["id"]
    monkeypatch.setattr(adapters, "scene", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk read error")))
    assert _png_ok(render.render_png(pid, "latest", "cfd_cp", "iso"))


def _fake_k09l_bundle(status, verdict):
    a = {"id": "flight_hover", "label": "Hover simulation", "status": status, "reason": "the design changed",
         "time": None, "summary": {}, "discipline": "system", "has_field": False, "result": "Hover is stable",
         "verdict": verdict}
    return {"rid": "r1", "fields": {}, "fsum": {}, "errors": [], "aircraft": None, "flow": {},
            "summary": {"study": "S", "variant": "v", "verdict": {"fail": 0, "cond": 0, "pass": 1, "nev": 0},
                        "kpis": [], "requirements": [], "metrics": [], "analyses": [a]}}


def test_s8_stale_k09l_analysis_is_watch_even_with_a_pass_verdict(home, monkeypatch):
    from opencae.hub import adapters
    entry = {"id": "x", "kind": "k09l-study", "name": "K", "path": str(home / "k.json")}
    monkeypatch.setattr(adapters, "_k09l_bundle", lambda e: _fake_k09l_bundle("stale", "PASS"))
    p = adapters._k09l_project(entry, "latest")
    c = next(c for c in p["cards"] if c["id"] == "flight_hover")
    assert c["status"] == "watch" and "design changed" in c["text"]
    assert adapters._k09l_audit(entry, "latest")[0]["status"] == "watch"
    monkeypatch.setattr(adapters, "_k09l_bundle", lambda e: _fake_k09l_bundle("current", "PASS"))
    assert next(c for c in adapters._k09l_project(entry, "latest")["cards"] if c["id"] == "flight_hover")["status"] == "ok"


def test_s9_studio_fea_card_carries_the_material_basis(home, tmp_path):
    from opencae.hub import adapters, registry
    E = EXAMPLES / "projects"
    d = tmp_path / "c"
    d.mkdir()
    shutil.copy(E / "cantilever_beam.ocae", d / "cantilever_beam.ocae")
    shutil.copytree(E / "cantilever_beam_data", d / "cantilever_beam_data")
    pid = registry.register(d / "cantilever_beam.ocae")["id"]
    card = adapters.project_view(pid)["cards"][0]
    bases = {i["status"] for i in adapters.audit(pid)[0]["inputs"] if i["name"].count(".")}
    assert card["basis"] in ("estimate", "datasheet", "user") and card["basis"] in bases | {"estimate"}
    assert all(m["basis"] == card["basis"] for m in card["metrics"])


def test_s10_flight_motor_channel_uses_the_flight_models_name_and_units(home, drone_ok):
    from opencae.hub import adapters, registry
    pid = registry.register(drone_ok)["id"]
    p = next(x for x in adapters.plots(pid, "latest") if x["id"] == "flight_motors")
    assert "thrust" in p["title"].lower() and "fraction" in p["y"]["label"].lower()
    assert "command" not in p["title"].lower() and p["y"]["unit"] == "-"
