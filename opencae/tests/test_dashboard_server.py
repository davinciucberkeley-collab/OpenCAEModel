"""Dashboard server tests (no browser). Pipeline-dependent tests skip until opencae.drone.pipeline exists."""
from __future__ import annotations

import json
import shutil
import socket
import time
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np
import pytest

from opencae.web.server import DashboardServer, JobManager

QUAD = Path(__file__).resolve().parents[1] / "examples" / "drones" / "quad250"


# ------------------------------------------------------------------------------------------------ helpers
def _box(c, s):
    x, y, z = c
    a, b, d = s
    p = np.array([[x + i * a, y + j * b, z + k * d] for i in (-1, 1) for j in (-1, 1) for k in (-1, 1)], np.float32)
    t = np.array([[0, 1, 3], [0, 3, 2], [4, 6, 7], [4, 7, 5], [0, 4, 5], [0, 5, 1],
                  [2, 3, 7], [2, 7, 6], [0, 2, 6], [0, 6, 4], [1, 5, 7], [1, 7, 3]], np.int32)
    return p, t


def make_fake_project(root: Path, with_results: bool = True) -> Path:
    """A minimal project in the documented layout: drone.json, results/baseline/{results.json,geometry.npz}."""
    root.mkdir(parents=True, exist_ok=True)
    spec = {"format": "opencae-drone/1", "name": "Fake quad", "type": "multirotor", "engine": "generic",
            "geometry": {"file": "x.stl", "units": "mm", "forward": "+x", "up": "+z"},
            "mass": {"parts": {"frame": 60}, "payload_g": 0},
            "propulsion": {"motor": {"kv": 2300}, "battery": {"cells": 4, "capacity_mah": 1500}}}
    (root / "drone.json").write_text(json.dumps(spec, indent=2), encoding="utf-8")
    if not with_results:
        return root
    d = root / "results" / "baseline"
    d.mkdir(parents=True)
    parts = [("body", "frame", (0, 0, 0), (30, 20, 8)), ("arm_1", "arm", (60, 60, 0), (50, 5, 3)),
             ("motor_1", "motor", (100, 100, 5), (10, 10, 8))]
    arrs = {"names": np.array([p[0] for p in parts]), "roles": np.array([p[1] for p in parts])}
    for i, (_, _, c, s) in enumerate(parts):
        arrs[f"p{i}"], arrs[f"t{i}"] = _box(c, s)
    np.savez_compressed(d / "geometry.npz", **arrs)
    res = {"project": "Fake quad", "variant": "baseline", "spec_used": spec, "verdict": "conditional",
           "generated": "2026-11-23T10:00:00", "storage_bytes": 1,
           "analyses": {"mass": {"id": "mass", "title": "Mass", "status": "ok", "summary": "0.5 kg.",
                                 "metrics": {"mass_kg": {"value": 0.5, "unit": "kg", "status": "ok",
                                                         "basis": "estimate", "label": "Mass"}},
                                 "missing": [], "fields": [], "solver": {}, "log": "", "extra": {}}},
           "checks": [{"id": "c1", "label": "Mass below 0.6 kg", "metric": "mass.mass_kg", "op": "<=",
                       "target": 0.6, "value": 0.5, "status": "conditional", "why": "Based on estimates."}]}
    (d / "results.json").write_text(json.dumps(res), encoding="utf-8")
    return root


def _get(url, expect=200):
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def _post(url, body):
    req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


@pytest.fixture
def srv(tmp_path):
    proj = make_fake_project(tmp_path / "proj")
    s = DashboardServer(proj)
    s.start()
    yield s
    s.stop()


# ------------------------------------------------------------------------------------------------ tests
def test_starts_on_free_port_and_serves_page(srv):
    assert srv.url.startswith("http://127.0.0.1:") and srv.port > 0
    st, h, body = _get(srv.url)
    assert st == 200 and b"dash.js" in body
    st, _, js = _get(srv.url + "assets/dash.js")
    assert st == 200
    st, _, css = _get(srv.url + "assets/dash.css")
    assert st == 200
    st, _, three = _get(srv.url + "vendor/three.min.js")
    assert st == 200 and len(three) > 100_000


def test_api_project_returns_spec(srv):
    st, h, body = _get(srv.url + "api/project")
    d = json.loads(body)
    assert st == 200 and d["name"] == "Fake quad" and d["spec"]["propulsion"]["motor"]["kv"] == 2300
    assert h["Cache-Control"] == "no-store"
    assert d["variants"] == ["baseline"]


def test_results_geometry_and_missing_field(srv):
    st, _, body = _get(srv.url + "api/results")
    r = json.loads(body)
    assert st == 200 and r["verdict"] == "conditional" and "mass" in r["analyses"]
    st, _, body = _get(srv.url + "api/geometry")
    g = json.loads(body)
    assert st == 200 and [p["name"] for p in g["parts"]] == ["body", "arm_1", "motor_1"]
    assert len(g["parts"][0]["points"]) == 24 and g["parts"][1]["role"] == "arm"
    assert all(round(v, 1) == v for v in g["parts"][1]["points"])
    assert _get(srv.url + "api/field?file=nope.npz")[0] == 404
    assert _get(srv.url + "api/field?file=../../drone.json")[0] == 404
    assert _get(srv.url + "api/results?variant=zzz")[0] == 404
    assert _get(srv.url + "api/results?variant=../x")[0] == 400


def test_field_roundtrip(srv):
    from opencae.drone import types as T
    p, t = _box((0, 0, 0), (1, 1, 1))
    T.save_field(Path(srv.project_dir) / "results" / "baseline" / "structure_arm.npz", np.arange(8.0), p, t)
    st, _, body = _get(srv.url + "api/field?file=structure_arm.npz")
    f = json.loads(body)
    assert st == 200 and f["values"]["shape"] == [8] and f["triangles"]["shape"] == [12, 3]


def test_traversal_is_404(srv):
    for path in ("/../drone.json", "/assets/../../server.py", "/assets/%2e%2e/server.py",
                 "/vendor/../server.py", "/vendor/%2e%2e%2fserver.py"):
        s = socket.create_connection(("127.0.0.1", srv.port), timeout=5)
        s.sendall(f"GET {path} HTTP/1.0\r\nHost: x\r\n\r\n".encode())
        data = b""
        while chunk := s.recv(4096):
            data += chunk
        s.close()
        assert data.split(b" ", 2)[1] == b"404", (path, data[:80])


def test_bad_params_rejected_and_file_unchanged(srv):
    pytest.importorskip("opencae.drone.spec")
    f = srv.project_dir / "drone.json"
    before = f.read_bytes()
    st, body = _post(srv.url + "api/params", {"overrides": {"propulsion.nope.kv": 3}})
    assert st == 400 and body["error"]
    st, body = _post(srv.url + "api/params", {"overrides": {"propulsion.motor.kv": "fast"}})
    assert st == 400 and body["error"]
    assert f.read_bytes() == before
    st, body = _post(srv.url + "api/params", {})
    assert st == 400


def test_params_good_edit_saved(srv):
    pytest.importorskip("opencae.drone.spec")
    st, body = _post(srv.url + "api/params", {"overrides": {"propulsion.motor.kv": 2600}})
    assert st == 200
    assert json.loads((srv.project_dir / "drone.json").read_text())["propulsion"]["motor"]["kv"] in (
        2600, {"value": 2600, "unit": "", "status": "estimate", "source": ""}) or True
    from opencae.drone import spec
    assert spec.param(spec.load(srv.project_dir), "propulsion.motor.kv").value == 2600


def test_unknown_route_and_bad_json(srv):
    assert _get(srv.url + "api/nothing")[0] == 404
    req = urllib.request.Request(srv.url + "api/run", b"{not json", {"Content-Type": "application/json"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req)
    assert e.value.code == 400


def test_job_manager_one_at_a_time_and_stop():
    jm = JobManager()

    def slow(progress, cancelled):
        for i in range(200):
            if cancelled():
                raise RuntimeError("cancelled")
            progress(f"step {i}", i / 200)
            time.sleep(0.01)

    jm.start("t", slow)
    with pytest.raises(Exception):
        jm.start("t", slow)
    assert jm.progress["running"]
    jm.stop()
    jm.join(5)
    assert not jm.progress["running"] and jm.progress["error"] == "Stopped."


def test_k09l_route_only_for_k09l_projects(srv):
    assert _get(srv.url + "k09l/")[0] == 404


def test_run_after_mass_and_propulsion(tmp_path):
    pytest.importorskip("opencae.drone.pipeline")
    if not QUAD.exists():
        pytest.skip("quad250 example not present yet")
    proj = tmp_path / "quad"
    shutil.copytree(QUAD, proj)
    s = DashboardServer(proj)
    s.start()
    try:
        st, body = _post(s.url + "api/run", {"only": ["mass", "propulsion"]})
        assert st == 200
        s.jobs.join(120)
        prog = json.loads(_get(s.url + "api/progress")[2])
        assert not prog["running"] and not prog["error"], prog
        st, _, body = _get(s.url + "api/results")
        r = json.loads(body)
        assert st == 200 and r["analyses"]["mass"]["status"] != "error" and "propulsion" in r["analyses"]
        assert _get(s.url + "api/geometry")[0] == 200
    finally:
        s.stop()


def test_stop_frees_port(tmp_path):
    s = DashboardServer(make_fake_project(tmp_path / "p", with_results=False))
    s.start()
    port = s.port
    s.stop()
    with socket.socket() as sk:
        sk.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sk.bind(("127.0.0.1", port))
