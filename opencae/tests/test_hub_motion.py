"""Results Hub motion simulator: backend payloads (opencae.hub.motion) and the front end (opencae/web/hub/motion.js).

Backend: the gripper and drone_rotors examples (their motion studies are run here, MuJoCo), a drone hover result, a
drone result without a time history, a synthetic K09-L website data folder (and the real one when present).
Front end: Playwright + Chromium load a small harness (served from a stub server) with WebGL on, with WebGL
disabled and with WebGL not requested; frames must advance and the canvases must not be blank.
"""
import base64
import json
import math
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("mujoco", reason="MuJoCo is not installed (python -m pip install mujoco)")

from opencae.app.session import Session
from opencae.examples.mbd_examples import GRIPPER as G
from opencae.hub import motion as M
from opencae.mbd import run_study

REPO = Path(__file__).resolve().parents[2]
EXAMPLES = REPO / "opencae" / "examples"
WEB = REPO / "opencae" / "web"
SHOTS = Path(os.environ.get("OPENCAE_MOTION_SHOTS",
                            "/tmp/claude-0/-home-claude/cca99fdc-b1aa-5a4c-a668-10c4bdf36bbb/scratchpad/motion_shots"))


def f32(b64, cols=None):
    a = np.frombuffer(base64.b64decode(b64), dtype="<f4")
    return a.reshape(-1, cols) if cols else a


def i32(b64):
    return np.frombuffer(base64.b64decode(b64), dtype="<i4").reshape(-1, 3)


def project(path, kind, name=None):
    return {"id": "t", "kind": kind, "path": str(path), "name": name or Path(path).stem}


# ------------------------------------------------------------------------------------------------ fixtures
def _copy_example(tmp, name):
    for suffix in (".ocae", "_data"):
        src = EXAMPLES / "projects" / (name + suffix)
        dst = tmp / (name + suffix)
        shutil.copytree(src, dst) if src.is_dir() else shutil.copy(src, dst)
    return tmp / (name + ".ocae")


def _run(path, letter_or_prefix):
    s = Session()
    s.open(path)
    st = next(x for x in s.project.studies if x.name.startswith(letter_or_prefix))
    run_study(s.project, st, data_dir=s.data_dir)
    return st


@pytest.fixture(scope="module")
def gripper(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("gripper_motion")
    path = _copy_example(tmp, "gripper")
    a = _run(path, "A ")
    b = _run(path, "B ")
    return {"path": path, "A": a, "B": b}


@pytest.fixture(scope="module")
def gripper_unrun(tmp_path_factory):
    return _copy_example(tmp_path_factory.mktemp("gripper_unrun"), "gripper")


@pytest.fixture(scope="module")
def rotors(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("rotors_motion")
    path = _copy_example(tmp, "drone_rotors")
    st = _run(path, "Rotor")
    return {"path": path, "study": st}


@pytest.fixture(scope="module")
def quad(tmp_path_factory):
    """quad250 with its flight analysis run (numpy only, about a second)."""
    from opencae.drone import pipeline
    d = tmp_path_factory.mktemp("quad_motion") / "quad"
    shutil.copytree(EXAMPLES / "drones" / "quad250", d)
    pipeline.run_project(d, only=["flight"])
    return d


@pytest.fixture
def quad_no_history(quad, tmp_path):
    d = tmp_path / "quad"
    shutil.copytree(quad, d)
    f = d / "results" / "baseline" / "results.json"
    data = json.loads(f.read_text())
    for k in ("t", "pos", "att", "motor"):
        data["analyses"]["flight"]["extra"].pop(k)
    f.write_text(json.dumps(data))
    return d


def _k09l_site(tmp, n=1500):
    """A small K09-L website data folder in the format opencae.web.build writes (aircraft.js, extra.js)."""
    data = tmp / "k09l_website" / "data"
    data.mkdir(parents=True)

    def box(c, s):
        x, y, z = c
        P = np.array([[x + dx * s, y + dy * s, z + dz * s] for dx in (-1, 1) for dy in (-1, 1) for dz in (-1, 1)], "<f4")
        T = np.array([[0, 1, 3], [0, 3, 2], [4, 6, 7], [4, 7, 5], [0, 4, 5], [0, 5, 1], [2, 3, 7], [2, 7, 6],
                      [0, 2, 6], [0, 6, 4], [1, 5, 7], [1, 7, 3]], "<u2")
        return {"pos": base64.b64encode(P.tobytes()).decode(), "idx": base64.b64encode(T.tobytes()).decode()}
    bodies = [dict(part="wing", name="Wing", rgb=[48, 59, 150], rotor=None, i32=False, modified=False,
                   **box((0, 0, 0), 300)),
              dict(part="prop", name="Prop", rgb=[220, 220, 220], rotor=0, i32=False, modified=False,
                   **box((0, 200, 0), 60))]
    air = {"parts": {"wing": {"name": "Wing core"}, "prop": {"name": "Prop"}}, "bodies": bodies,
           "rotors": [{"name": "Prop M1", "origin": [0, 200, 0], "axis": [0, 0, 1], "sense": 1.0}],
           "hover_rpm": [5000.0], "spin_display": 0.025, "cg_mm": [0, 0, 0]}
    t = np.arange(n) * 0.05
    th = np.linspace(0, 1.0, n)
    quat = np.stack([np.cos(th / 2), np.zeros(n), np.sin(th / 2), np.zeros(n)], 1)
    tr = {"scenario": "mission", "t": t.tolist(), "translation_mm": np.stack([t * 100, 0 * t, 250 + 10 * t], 1).tolist(),
          "quat": quat.tolist(), "rpm": (5000 + 0 * t).reshape(-1, 1).tolist(), "thrust_N": (3 + 0 * t).reshape(-1, 1).tolist(),
          "throttle": (0.5 + 0 * t).reshape(-1, 1).tolist(), "motor_current_A": (10 + 0 * t).reshape(-1, 1).tolist(),
          "battery_current_A": (10 + 0 * t).tolist(), "soc": (1 - t / 1000).tolist(), "airspeed": (t * 0.5).tolist(),
          "altitude_m": (0.25 + 0.01 * t).tolist(), "alpha_deg": (0 * t).tolist(),
          "elevon_deg": np.zeros((n, 2)).tolist(), "euler_deg": np.zeros((n, 3)).tolist(), "motors": ["Prop M1"],
          "events": [{"t": 0.5, "text": "armed", "kind": "event"}, {"t": 1.0, "text": "phase: climb", "kind": "event"},
                     {"t": 2.0, "text": "motor M1 current high", "kind": "warning"}],
          "phases": [{"id": "climb", "name": "Climb", "t0": 1.0, "t1": 5.0, "completed": True, "note": ""}],
          "validity": "reduced_order (provisional inputs)", "evidence_class": "reduced_order",
          "world": {"up": "z", "units": "mm", "ground_z_mm": 0.0, "extent_mm": [0, 1, 0, 1, 0, 1]},
          "convention": "v_W_mm = M v_B_mm"}
    (data / "aircraft.js").write_text("window.K09L_AIRCRAFT = " + json.dumps(air) + ";")
    (data / "extra.js").write_text("window.K09L_EXTRA = " + json.dumps({"flight": {"mission": tr, "note": "stub note"}}) + ";")
    return tmp / "k09l_study.json"


@pytest.fixture
def k09l(tmp_path):
    study = _k09l_site(tmp_path)
    study.write_text("{}")
    return study


# ------------------------------------------------------------------------------------------------ helpers (math)
def test_quaternion_roundtrip_and_euler_inverse():
    rng = np.random.default_rng(3)
    for _ in range(50):
        q = rng.normal(size=4)
        q /= np.linalg.norm(q)
        R = M.quat_to_mat(q)
        q2 = M.mat_to_quat(R)
        assert abs(abs(float(q @ q2)) - 1) < 1e-9
        assert np.abs(M.quat_to_mat(q2) - R).max() < 1e-9
    from opencae.drone.analyses import flight as FL
    for r, p, y in [(15, 0, 0), (-30, 20, 100), (5, -40, -170)]:
        back = FL._euler(M.euler_to_quat(r, p, y))
        assert np.allclose(np.degrees(back), [r, p, y], atol=1e-9)


def test_pose_array_unit_and_continuous():
    R = [M.quat_to_mat([np.cos(a / 2), 0, 0, np.sin(a / 2)]) for a in np.linspace(0, 6.0, 40)]
    P = M.pose_array(R, [np.zeros(3)] * 40)
    assert np.allclose(np.linalg.norm(P[:, 3:], axis=1), 1, atol=1e-12)
    assert (np.sum(P[1:, 3:] * P[:-1, 3:], axis=1) > 0).all()     # no sign flips between frames


def test_frame_indices_keep_ends_and_cap():
    idx = M.frame_indices(5000)
    assert idx[0] == 0 and idx[-1] == 4999 and len(idx) <= M.MAX_FRAMES
    assert len(M.frame_indices(100)) == 100


# ------------------------------------------------------------------------------------------------ studio / MBD
def _check_common(d, n_bodies):
    assert d["kind"] in ("mbd", "hover", "flight")
    assert set(["kind", "title", "basis", "notes", "t", "bodies", "poses", "joints", "channels", "rotors"]) <= set(d)
    n = len(d["t"])
    assert 2 <= n <= M.MAX_FRAMES
    assert all(b > a for a, b in zip(d["t"], d["t"][1:]))
    assert len(d["bodies"]) == n_bodies
    for b in d["bodies"]:
        P, T = f32(b["points"], 3), i32(b["triangles"])
        assert len(P) >= 3 and len(T) >= 1 and T.max() < len(P) and T.min() >= 0
        assert b["color"].startswith("#")
    for key, arr in d["poses"].items():
        a = f32(arr, 7)
        assert a.shape == (n, 7)
        assert np.allclose(np.linalg.norm(a[:, 3:], axis=1), 1, atol=1e-5)          # unit quaternions
    for ch in d["channels"]:
        assert len(ch["values"]) == n and ch["unit"] and ch["name"]


def test_gripper_prescribed_payload(gripper):
    p = project(gripper["path"], "studio", "gripper")
    d = M.motion(p, gripper["A"].id)
    assert d["kind"] == "mbd"
    _check_common(d, 3)
    assert d["disclaimer"] is None and "MuJoCo" in d["basis"]
    # joints with their modes
    modes = {j["name"]: j["mode"] for j in d["joints"]}
    assert modes == {"Left hinge": "prescribed", "Right hinge": None}
    right = next(j for j in d["joints"] if j["name"] == "Right hinge")
    assert "coupled" in right["label"]
    # the closing angle of the left finger ends at the closed angle of the example
    ang = next(c for c in d["channels"] if c["name"] == "Left hinge: angle")
    assert ang["unit"] == "deg" and abs(ang["values"][-1] - G["closed_deg"]) < 0.05 and abs(ang["values"][0]) < 1e-6
    names = {c["name"] for c in d["channels"]}
    assert {"Left hinge: speed", "Left hinge: torque", "Left hinge: reaction force", "Left hinge: reaction moment",
            "constraint error"} <= names
    assert next(c for c in d["channels"] if c["name"] == "Left hinge: torque")["unit"] == "N m"
    assert d["rotors"] is None and d["notes"]


def test_gripper_poses_at_t0_equal_component_placements(gripper):
    p = project(gripper["path"], "studio", "gripper")
    d = M.motion(p, gripper["A"].id)
    s = Session()
    s.open(gripper["path"])
    for c in s.project.assembly.components:
        pose = f32(d["poses"][c.id], 7)[0]
        Pm = np.asarray(c.placement, float).reshape(-1, 4)
        assert np.allclose(pose[:3], Pm[:3, 3], atol=1e-4)
        assert np.allclose(M.quat_to_mat(pose[3:]), Pm[:3, :3], atol=1e-5)
    # and the fingers do move
    left = next(c for c in s.project.assembly.components if c.name == "Left finger")
    a = f32(d["poses"][left.id], 7)
    assert np.abs(a[-1] - a[0]).max() > 1.0


def test_gripper_torque_driven_labels(gripper):
    d = M.motion(project(gripper["path"], "studio", "gripper"), gripper["B"].id)
    assert d["kind"] == "mbd"
    assert {j["name"]: j["mode"] for j in d["joints"]}["Left hinge"] == "torque-driven"
    _check_common(d, 3)


def test_latest_picks_the_newest_result(gripper):
    d = M.motion(project(gripper["path"], "studio", "gripper"), "latest")
    assert d["kind"] == "mbd" and d["study"]["id"] in (gripper["A"].id, gripper["B"].id)


def test_gripper_payload_is_small(gripper):
    d = M.motion(project(gripper["path"], "studio", "gripper"), gripper["A"].id)
    assert len(json.dumps(d)) < 150_000


def test_unrun_motion_study_says_so_and_runs_nothing(gripper_unrun):
    d = M.motion(project(gripper_unrun, "studio", "gripper"))
    assert d["kind"] is None
    assert any("run the motion study first" in n for n in d["notes"])
    assert d["bodies"] == [] and d["t"] == []
    assert M.plots(project(gripper_unrun, "studio", "gripper")) == []
    assert not (Path(str(gripper_unrun).replace(".ocae", "_data")) / "results").exists() or not any(
        (Path(str(gripper_unrun).replace(".ocae", "_data")) / "results").rglob("mbd.npz"))


def test_studio_without_motion_study_and_bad_inputs(tmp_path):
    d = M.motion(project(EXAMPLES / "projects" / "cantilever_beam.ocae", "studio"))
    assert d["kind"] is None and "no motion" in d["notes"][0].lower()
    assert M.motion(project(tmp_path / "missing.ocae", "studio"))["kind"] is None
    assert M.motion(project(tmp_path, "mystery"))["kind"] is None
    assert M.motion(project(EXAMPLES / "projects" / "gripper.ocae", "studio"), "nope")["kind"] is None


def test_drone_rotors_payload(rotors):
    d = M.motion(project(rotors["path"], "studio", "drone_rotors"))
    assert d["kind"] == "mbd"
    _check_common(d, 9)
    assert d["rotors"] and len(d["rotors"]) == 4
    body_ids = {b["id"] for b in d["bodies"]}
    for r in d["rotors"]:
        assert r["body_id"] in body_ids and r["in_pose"] is False and len(r["speed_rpm"]) == len(d["t"])
        assert abs(r["speed_rpm"][-1] - 6000.0) < 1.0               # the example spins to 6000 rpm (joint_qd)
        assert np.isclose(np.linalg.norm(r["axis"]), 1.0)
    assert all(j["mode"] in ("prescribed", None) for j in d["joints"])
    assert len(json.dumps(d)) < 400_000
    # the stored poses keep the rotors' position and drop the (aliased) spin about their axis
    a = f32(d["poses"][d["rotors"][0]["body_id"]], 7)
    assert np.abs(a[-1] - a[0]).max() < 1e-4


def test_mbd_plots_shape_and_units(gripper):
    pl = M.plots(project(gripper["path"], "studio", "gripper"), gripper["A"].id)
    assert pl
    d = M.motion(project(gripper["path"], "studio", "gripper"), gripper["A"].id)
    for item in pl:
        assert {"id", "title", "discipline", "x", "y", "series", "note"} <= set(item)
        assert item["discipline"] == "mbd" and item["x"]["unit"] == "s" and item["y"]["unit"]
        assert item["y"]["log"] is False
        for s in item["series"]:
            assert len(s["x"]) == len(s["y"]) == len(d["t"])
    assert len({i["id"] for i in pl}) == len(pl)
    ang = next(i for i in pl if i["y"]["label"] == "joint angle")
    assert ang["y"]["unit"] == "deg" and len(ang["series"]) == 2


def test_decimation_is_stated(gripper, monkeypatch):
    monkeypatch.setattr(M, "MAX_FRAMES", 40)
    d = M.motion(project(gripper["path"], "studio", "gripper"), gripper["A"].id)
    assert len(d["t"]) <= 40 and d["frames"]["decimated"] and d["frames"]["stored"] == 101
    assert d["t"][-1] == pytest.approx(1.0) and any("Shown at" in n for n in d["notes"])
    ang = next(c for c in d["channels"] if c["name"] == "Left hinge: angle")
    assert abs(ang["values"][-1] - G["closed_deg"]) < 0.05           # the last stored sample is kept


# ------------------------------------------------------------------------------------------------ drone hover
def test_hover_payload_from_stored_flight_result(quad):
    d = M.motion(project(quad, "drone", "quad250"))
    assert d["kind"] == "hover"
    _check_common(d, 16)
    assert d["disclaimer"] == "Animation of computed results; not a flight prediction"
    res = json.loads((quad / "results" / "baseline" / "results.json").read_text())
    ex = res["analyses"]["flight"]["extra"]
    assert len(d["t"]) == len(ex["t"]) == 400
    assert np.allclose(d["t"], ex["t"], atol=1e-4)
    # at the first sample the aircraft is level and its CG is at the hover point: mesh frame -> world = -R cg
    cg = np.array(res["analyses"]["mass"]["metrics"]["cg_mm"]["value"])
    pose = f32(d["poses"]["aircraft"], 7)
    assert np.allclose(pose[0, :3], -cg, atol=1.0)
    assert np.allclose(M.quat_to_mat(pose[0, 3:]), np.eye(3), atol=2e-3)
    assert set(d["pose_of"].values()) == {"aircraft"}
    # the largest tilt of the replay is the stored peak tilt
    tilt = next(c for c in d["channels"] if c["name"] == "tilt")
    assert max(tilt["values"]) == pytest.approx(res["analyses"]["flight"]["metrics"]["max_tilt_deg"]["value"], abs=0.1)
    att = {c["name"]: c["values"] for c in d["channels"] if c["quantity"] == "attitude"}
    assert max(att["roll"]) == pytest.approx(max(r[0] for r in ex["att"]), abs=1e-3)
    # rotors: one per stored rotor, speed = full-throttle rpm x sqrt(thrust fraction), props found near each motor
    assert len(d["rotors"]) == 4 and len({r["body_id"] for r in d["rotors"]}) == 4
    rpm_full = res["analyses"]["propulsion"]["extra"]["rpm_full"]
    r0 = d["rotors"][0]
    k = 123
    assert r0["speed_rpm"][k] == pytest.approx(rpm_full * math.sqrt(ex["motor"][k][0]), rel=1e-4)
    assert d["events"] and d["events"][0]["t"] == ex["kick_time_s"]
    assert len(json.dumps(d)) < 400_000


def test_hover_plots(quad):
    pl = M.plots(project(quad, "drone", "quad250"))
    qs = {i["y"]["label"] for i in pl}
    assert {"attitude", "tilt", "rotor speed"} <= qs
    assert all(i["discipline"] == "flight" for i in pl)


def test_hover_without_time_history_is_null_and_says_what_is_missing(quad_no_history):
    d = M.motion(project(quad_no_history, "drone", "quad250"))
    assert d["kind"] is None and d["poses"] == {} and d["bodies"] == []
    assert "extra.t" in d["notes"][0] and "extra.pos" in d["notes"][0] and "extra.att" in d["notes"][0]
    assert M.plots(project(quad_no_history, "drone", "quad250")) == []


def test_hover_without_flight_or_results(quad, tmp_path):
    d1 = tmp_path / "nores"
    shutil.copytree(EXAMPLES / "drones" / "quad250", d1)
    assert "run the drone analyses" in M.motion(project(d1, "drone"))["notes"][0]
    d2 = tmp_path / "noflight"
    shutil.copytree(quad, d2)
    f = d2 / "results" / "baseline" / "results.json"
    data = json.loads(f.read_text())
    del data["analyses"]["flight"]
    f.write_text(json.dumps(data))
    assert "flight analysis has not been run" in M.motion(project(d2, "drone"))["notes"][0]


def test_hover_run_ids(quad):
    p = project(quad, "drone", "quad250")
    rows = [json.loads(x) for x in (quad / "results" / "runs.jsonl").read_text().splitlines() if x.strip()]
    assert M.motion(p, rows[-1]["run_id"])["kind"] == "hover"       # the latest run of its variant
    assert M.motion(p, "20000101-000000-aaaa")["kind"] is None
    assert "not in the run history" in M.motion(p, "20000101-000000-aaaa")["notes"][0]


# ------------------------------------------------------------------------------------------------ K09-L replay
def test_k09l_replay_from_website_data(k09l):
    d = M.motion(project(k09l, "k09l-study", "K09-L"))
    assert d["kind"] == "flight"
    _check_common(d, 2)                                             # one body per part and per rotor group
    assert len(d["t"]) <= M.MAX_FRAMES and d["frames"] == {"shown": len(d["t"]), "stored": 1500, "decimated": True}
    assert d["disclaimer"] == "Animation of computed results; not a flight prediction"
    assert set(d["pose_of"]) == {"part-wing", "rotor-0"} and list(d["poses"]) == ["aircraft"]
    r = d["rotors"][0]
    assert r["body_id"] == "rotor-0" and r["axis"] == [0, 0, 1] and r["sense"] == 1.0 and r["in_pose"] is False
    assert not any(e["text"].startswith("phase:") for e in d["events"]) and d["phases"][0]["id"] == "climb"
    assert d["scene"]["ground_z_mm"] == 0.0
    assert any("stub note" in n for n in d["notes"])
    pose = f32(d["poses"]["aircraft"], 7)
    assert np.allclose(pose[0, :3], [0, 0, 250])                    # translation_mm of the first stored sample
    pl = M.plots(project(k09l, "k09l-study", "K09-L"))
    assert {"altitude (CG above ground)", "motor speed"} <= {i["y"]["label"] for i in pl}


def test_k09l_missing_data_names_what_to_build(tmp_path):
    study = tmp_path / "k09l_study.json"
    study.write_text("{}")
    d = M.motion(project(study, "k09l-study"))
    assert d["kind"] is None and "build the K09-L website" in d["notes"][0]


REAL_STUDY = Path.home() / ".opencae" / "k09l_study.json"


@pytest.mark.skipif(not (REAL_STUDY.parent / "k09l_website" / "data" / "extra.js").exists(),
                    reason="the K09-L website data is not built on this machine")
def test_real_k09l_payload_size_bound():
    d = M.motion(project(REAL_STUDY, "k09l-study", "K09-L"))
    assert d["kind"] == "flight"
    _check_common(d, len(d["bodies"]))
    assert len(d["t"]) <= 600 and len(json.dumps(d)) < 2_600_000
    assert sum(len(i32(b["triangles"])) for b in d["bodies"]) <= M.MAX_TRIANGLES * 1.3   # decimation keeps topology, so it lands near the budget
    assert len(d["rotors"]) == 4


# ------------------------------------------------------------------------------------------------ front end
def _python_with_playwright():
    cands = [sys.executable, "/usr/local/bin/python3", shutil.which("python3") or "", "/usr/bin/python3"]
    for c in cands:
        if c and Path(c).exists():
            r = subprocess.run([c, "-c", "import playwright.sync_api"], capture_output=True)
            if r.returncode == 0:
                return c
    return None


DRIVER = textwrap.dedent(r'''
import json, sys, threading, http.server, socketserver
from pathlib import Path
from playwright.sync_api import sync_playwright

cfg = json.load(open(sys.argv[1]))
WEB, DATA, SHOTS = Path(cfg["web"]), Path(cfg["data"]), Path(cfg["shots"])
HTML = """<!doctype html><html><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<style>:root{--panel:#151d24;--panel2:#1a242c;--ink:#e2e9ef;--ink2:#a7b4bf;--ink3:#7b8893;--rule:#283540;--accent:#f3864a;
--accent2:#79afe0;--stage:#18222a;--stage2:#0f171d}body{margin:0;background:#0e1419;color:#e2e9ef;font:14px system-ui;padding:12px}</style>
</head><body><div id=m></div><script src="/hub/motion.js"></script><script>
const q = new URLSearchParams(location.search);
fetch('/data/' + q.get('d') + '.json').then(r => r.json()).then(d => {
  window.H = HubMotion.mount(document.getElementById('m'), d, {webgl: q.get('gl') === '1'}); window.READY = true; });
</script></body></html>"""

class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        p = self.path.split('?')[0]
        ct = 'application/javascript'
        if p == '/': body, ct = HTML.encode(), 'text/html'
        elif p.startswith('/vendor/'): body = (WEB / 'vendor' / p[8:]).read_bytes()
        elif p == '/hub/motion.js': body = (WEB / 'hub' / 'motion.js').read_bytes()
        elif p.startswith('/data/'): body, ct = (DATA / p[6:]).read_bytes(), 'application/json'
        else:
            self.send_response(404); self.end_headers(); return
        self.send_response(200); self.send_header('Content-Type', ct); self.send_header('Content-Length', str(len(body)))
        self.end_headers(); self.wfile.write(body)

srv = socketserver.ThreadingTCPServer(('127.0.0.1', 0), H)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()

PIXELS = """() => {
  const out = [];
  for (const cv of document.querySelectorAll('.hm-stage canvas')) {
    const t = document.createElement('canvas'); t.width = cv.width; t.height = cv.height;
    const g = t.getContext('2d'); g.drawImage(cv, 0, 0);
    const d = g.getImageData(0, 0, t.width, t.height).data;
    let opaque = 0, hash = 0; const seen = new Set();
    for (let i = 0; i < d.length; i += 4) {
      if (d[i + 3] > 40) { opaque++; seen.add((d[i] >> 5) * 64 + (d[i + 1] >> 5) * 8 + (d[i + 2] >> 5)); }
      hash = (hash * 31 + d[i] + 7 * d[i + 1] + 13 * d[i + 2] + d[i + 3]) >>> 0;
    }
    out.push({w: t.width, h: t.height, frac: opaque / (d.length / 4), colours: seen.size, hash});
  }
  return out; }"""

results = []
with sync_playwright() as p:
    for job in cfg["jobs"]:
        args = ["--use-gl=swiftshader", "--enable-unsafe-swiftshader"]
        if not job.get("gpu", True):
            args = ["--disable-gpu", "--disable-3d-apis", "--disable-webgl"]
        b = p.chromium.launch(args=args)
        pg = b.new_page(viewport={'width': 1280, 'height': 1400}, device_scale_factor=1)
        msgs = []
        pg.on('console', lambda m: msgs.append(m.text))
        pg.on('pageerror', lambda e: msgs.append('PAGEERROR ' + str(e)))
        pg.goto(f"http://127.0.0.1:{port}/?d={job['data']}&gl={1 if job['webgl'] else 0}")
        pg.wait_for_function('window.READY === true', timeout=30000)
        r = {"name": job["name"]}
        if job.get("null"):
            r["text"] = pg.inner_text('.hm'); r["canvases"] = pg.locator('canvas').count()
            r["state"] = pg.evaluate('H.getState()')
        else:
            pg.wait_for_function('H.getState().renderer !== null', timeout=30000)
            pg.wait_for_timeout(600)
            r["state0"] = pg.evaluate('H.getState()')
            pg.evaluate('H.pause(); H.seek(H.getState().T * 0.1)'); pg.wait_for_timeout(500)
            r["pix_a"] = pg.evaluate(PIXELS); r["time_a"] = pg.inner_text('[data-hm=time]')
            pg.evaluate('H.seek(H.getState().T * 0.9)'); pg.wait_for_timeout(500)
            r["pix_b"] = pg.evaluate(PIXELS); r["time_b"] = pg.inner_text('[data-hm=time]')
            pg.evaluate('H.seek(0); H.play()'); t0 = pg.evaluate('H.getState()')
            pg.wait_for_timeout(2500)
            t1 = pg.evaluate('H.getState()'); r["play"] = [t0, t1]
            pg.evaluate('H.pause(); H.seek(H.getState().T * 0.45)'); pg.wait_for_timeout(600)
            r["state"] = pg.evaluate('H.getState()')
            r["note"] = pg.inner_text('.hm-rnote') if pg.locator('.hm-rnote').count() else ''
            r["disc"] = pg.inner_text('.hm-disc') if pg.locator('.hm-disc').count() else ''
            r["chips"] = pg.inner_text('.hm-live') if pg.locator('.hm-live').count() else ''
            r["controls"] = {"play": pg.locator('.hm-play').count(), "scrub": pg.locator('input[type=range]').count(),
                             "speed": pg.locator('select[aria-label="Playback speed"] option').all_inner_texts(),
                             "loop": pg.locator('input[type=checkbox]').count(), "reset": pg.get_by_text('Reset camera').count(),
                             "plot": pg.locator('.hm-plot canvas').count(), "follow": pg.locator('select[aria-label="Follow body"]').count()}
            # the plot's moving cursor: its canvas changes with time
            r["plot_hash"] = []
            for f in (0.2, 0.8):
                pg.evaluate(f'H.seek(H.getState().T * {f})'); pg.wait_for_timeout(300)
                r["plot_hash"].append(pg.evaluate("""() => { const c = document.querySelector('.hm-plot canvas'); const d = c.getContext('2d').getImageData(0,0,c.width,c.height).data; let h = 0; for (let i = 0; i < d.length; i += 4) h = (h * 31 + d[i] + 3 * d[i+1] + 5 * d[i+2]) >>> 0; return h; }"""))
            pg.evaluate('H.seek(H.getState().T * 0.45)'); pg.wait_for_timeout(400)
            # destroy() leaves nothing behind
            if job.get("shot"):
                pg.screenshot(path=str(SHOTS / job["shot"]), full_page=True)
            pg.evaluate('H.destroy()'); r["after_destroy"] = pg.evaluate("document.getElementById('m').children.length")
        r["errors"] = [m for m in msgs if 'GL Driver' not in m and 'GPU stall' not in m and 'willReadFrequently' not in m]
        results.append(r)
        b.close()
print("RESULTS:" + json.dumps(results))
''')


@pytest.fixture(scope="module")
def frontend_run(gripper, quad, tmp_path_factory):
    py = _python_with_playwright()
    if py is None:
        pytest.skip("python playwright is not installed")
    tmp = tmp_path_factory.mktemp("hm_front")
    data = tmp / "data"
    data.mkdir()
    for name, d in (("gripper", M.motion(project(gripper["path"], "studio", "gripper"), gripper["A"].id)),
                    ("quad", M.motion(project(quad, "drone", "quad250"))),
                    ("none", M.motion(project(gripper["path"].with_name("nothing.ocae"), "studio")))):
        (data / f"{name}.json").write_text(json.dumps(d))
    SHOTS.mkdir(parents=True, exist_ok=True)
    jobs = [
        {"name": "gripper_webgl", "data": "gripper", "webgl": True, "shot": "test_gripper_webgl.png"},
        {"name": "gripper_nowebgl_requested", "data": "gripper", "webgl": False, "shot": "test_gripper_2d.png"},
        {"name": "gripper_webgl_blocked", "data": "gripper", "webgl": True, "gpu": False, "shot": "test_gripper_blocked_fallback.png"},
        {"name": "quad_webgl", "data": "quad", "webgl": True, "shot": "test_quad_webgl.png"},
        {"name": "quad_2d", "data": "quad", "webgl": False, "shot": "test_quad_2d.png"},
        {"name": "none", "data": "none", "webgl": False, "null": True},
    ]
    cfg = tmp / "cfg.json"
    cfg.write_text(json.dumps({"web": str(WEB), "data": str(data), "shots": str(SHOTS), "jobs": jobs}))
    drv = tmp / "driver.py"
    drv.write_text(DRIVER)
    r = subprocess.run([py, str(drv), str(cfg)], capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr[-3000:]
    line = next(x for x in r.stdout.splitlines() if x.startswith("RESULTS:"))
    return {x["name"]: x for x in json.loads(line[len("RESULTS:"):])}


def _check_page(r, renderer, n_canvases, allowed=()):
    assert [e for e in r["errors"] if not any(a in e for a in allowed)] == [], r["errors"]
    assert r["state"]["renderer"] == renderer and r["state"]["canvases"] == n_canvases
    # not blank: every canvas draws something, with more than one colour
    for pix in (r["pix_a"], r["pix_b"]):
        assert len(pix) == n_canvases
        for c in pix:
            assert c["w"] > 100 and c["h"] > 100
            assert c["frac"] > 0.004 and c["colours"] >= 3, c
    # the picture changes with time (the bodies move) and so do the time readout and the plot cursor
    assert [c["hash"] for c in r["pix_a"]] != [c["hash"] for c in r["pix_b"]]
    assert r["time_a"] != r["time_b"] and r["time_a"].startswith("t = ")
    assert r["plot_hash"][0] != r["plot_hash"][1]
    # playing advances the playhead and renders frames
    t0, t1 = r["play"]
    assert t1["t"] > t0["t"] + 0.2 and t1["frames"] >= t0["frames"] + 2
    c = r["controls"]
    assert c["play"] == 1 and c["scrub"] >= 1 and c["loop"] >= 1 and c["reset"] == 1 and c["plot"] == 1
    assert c["speed"] == ["0.25×", "0.5×", "1×", "2×", "4×"]
    assert r["after_destroy"] == 0


def test_frontend_gripper_webgl(frontend_run):
    r = frontend_run["gripper_webgl"]
    _check_page(r, "webgl", 1)
    assert "prescribed" in r["chips"] and "Left hinge" in r["chips"]
    assert r["disc"] == ""                                          # the flight line is for drones only


def test_frontend_gripper_2d_views(frontend_run):
    r = frontend_run["gripper_nowebgl_requested"]
    _check_page(r, "2d", 3)
    assert "prescribed" in r["chips"] and "coupled" in r["chips"]


def test_frontend_webgl_blocked_falls_back_to_2d(frontend_run):
    r = frontend_run["gripper_webgl_blocked"]
    _check_page(r, "2d", 3, allowed=("Error creating WebGL context",))   # three.js logs why it gave up
    assert "2-D" in r["note"] and "WebGL" in r["note"]


def test_frontend_drone_hover_both_paths(frontend_run):
    for name, renderer, n in (("quad_webgl", "webgl", 1), ("quad_2d", "2d", 3)):
        r = frontend_run[name]
        _check_page(r, renderer, n)
        assert r["disc"] == "Animation of computed results; not a flight prediction"
        assert "rpm" in r["chips"] and r["controls"]["follow"] == 1


def test_frontend_null_data_shows_the_reason(frontend_run):
    r = frontend_run["none"]
    assert r["canvases"] == 0 and r["errors"] == []
    assert "No motion to show" in r["text"] and "does not exist" in r["text"]
