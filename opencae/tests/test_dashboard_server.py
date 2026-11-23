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
    assert _get(srv.url + "api/field?file=../../drone.json")[0] in (400, 404)
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


# ------------------------------------------------------------------------------------------------ security
def _raw_post(srv, headers, body=b'{"overrides": {"propulsion.motor.kv": 3100}}', path="api/params"):
    req = urllib.request.Request(srv.url + path, body, headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def test_foreign_origin_post_refused_and_file_unchanged(srv):
    f = srv.project_dir / "drone.json"
    before = f.read_bytes()
    assert _raw_post(srv, {"Content-Type": "application/json", "Origin": "http://evil.example"}) == 403
    assert _raw_post(srv, {"Content-Type": "application/json", "Origin": "null"}) == 403
    assert _raw_post(srv, {"Content-Type": "application/json", "Host": "evil.example"}) == 403
    assert f.read_bytes() == before
    own = {"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{srv.port}"}
    assert _raw_post(srv, own) == 200  # the page's own origin is accepted


def test_post_needs_json_content_type(srv):
    f = srv.project_dir / "drone.json"
    before = f.read_bytes()
    assert _raw_post(srv, {"Content-Type": "text/plain"}) == 415
    assert _raw_post(srv, {}) == 415
    assert f.read_bytes() == before


def test_api_get_checks_host(srv):
    req = urllib.request.Request(srv.url + "api/project", headers={"Host": "evil.example"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req)
    assert e.value.code == 403
    assert _get(srv.url + "api/project")[0] == 200


def test_variant_and_file_names_are_checked(srv):
    for q in ("variant=../x", "variant=..%2F..%2Fetc", "variant=a.b", "variant=" + "a" * 41):
        assert _get(srv.url + "api/results?" + q)[0] == 400, q
        assert _get(srv.url + "api/geometry?" + q)[0] == 400, q
    for f in ("../../drone.json", "..%2F..%2Fdrone.json", "sub/x.npz", "geometry.txt", "..npz/../x.npz"):
        assert _get(srv.url + "api/field?file=" + f)[0] in (400, 404), f
    # a real .npz outside a variant folder is never served
    (srv.project_dir / "secret.npz").write_bytes(b"x")
    assert _get(srv.url + "api/field?file=..%2F..%2Fsecret.npz")[0] in (400, 404)


# ------------------------------------------------------------------------------------------------ model and fields
def test_geometry_before_first_run_loads_the_cad(tmp_path):
    pytest.importorskip("opencae.drone.geometry")
    if not QUAD.exists():
        pytest.skip("quad250 example not present")
    proj = tmp_path / "quad"
    shutil.copytree(QUAD, proj, ignore=shutil.ignore_patterns("results"))
    s = DashboardServer(proj)
    s.start()
    try:
        st, _, body = _get(s.url + "api/geometry")
        g = json.loads(body)
        assert st == 200 and len(g["parts"]) > 3 and g["parts"][0]["points"]
        assert not list(proj.glob("results/*/results.json"))  # no run was recorded
        assert _get(s.url + "api/results")[0] == 404
    finally:
        s.stop()


def test_field_with_shared_mesh(srv):
    from opencae.drone import types as T
    d = Path(srv.project_dir) / "results" / "baseline"
    p, t = _box((0, 0, 0), (1, 1, 1))
    T.save_field(d / "cfd_cp.npz", np.arange(8.0), p, t)
    np.savez_compressed(d / "cfd_p.npz", values=np.arange(8.0, dtype=np.float16), mesh=np.array("cfd_cp.npz"))
    f = json.loads(_get(srv.url + "api/field?file=cfd_p.npz")[2])
    assert f["points"]["shape"] == [8, 3] and f["triangles"]["shape"] == [12, 3] and "mesh" not in f


def test_project_serves_better_table(srv):
    d = json.loads(_get(srv.url + "api/project")[2])
    assert d["better"]["mass_kg"] == "lower"


# ------------------------------------------------------------------------------------------------ jobs
def test_iterate_passes_progress_and_cancelled(srv, monkeypatch):
    from opencae.drone import pipeline
    seen = {}

    def fake(project_dir, overrides, name, only=None, progress=None, cancelled=None, keep_cases=False):
        seen.update(overrides=overrides, name=name, progress=progress, cancelled=cancelled)
        progress("halfway", 0.5)
        return {"variant": name, "results": {"verdict": "conditional"}, "compare": []}

    monkeypatch.setattr(pipeline, "iterate", fake)
    st, body = _post(srv.url + "api/iterate", {"overrides": {"propulsion.motor.kv": 2500}, "name": "kv2500"})
    assert st == 200
    srv.jobs.join(10)
    assert callable(seen["progress"]) and callable(seen["cancelled"]) and seen["name"] == "kv2500"
    prog = json.loads(_get(srv.url + "api/progress")[2])
    assert prog["result"]["variant"] == "kv2500" and "halfway" in prog["log"] and not prog["error"]


def test_iterate_type_error_is_reported_not_rerun(srv, monkeypatch):
    from opencae.drone import pipeline
    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise TypeError("real failure")

    monkeypatch.setattr(pipeline, "iterate", boom)
    assert _post(srv.url + "api/iterate", {"overrides": {"propulsion.motor.kv": 2500}, "name": "x1"})[0] == 200
    srv.jobs.join(10)
    assert len(calls) == 1 and "real failure" in srv.jobs.progress["error"]
    assert _post(srv.url + "api/iterate", {"overrides": {"propulsion.motor.kv": 2500}, "name": "../x"})[0] == 400
