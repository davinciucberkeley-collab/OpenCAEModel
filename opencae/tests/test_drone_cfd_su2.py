"""Drone CFD with SU2 (whole aircraft, tetrahedral gmsh mesh): solver choice, case writing, boolean failure, one real run."""
from __future__ import annotations

import copy
import math
import re
from pathlib import Path

import numpy as np
import pytest

from opencae.drone import types as T
from opencae.drone.analyses import cfd, cfd_su2
from opencae.tests.test_drone_solvers import box, ctx_for, quad_aircraft, quad_spec


class _Inst:
    exe = "/nonexistent/SU2_CFD"


def spec_with(solver=None, **cfd_keys):
    s = quad_spec()
    if solver is not None:
        s["analyses"]["cfd"]["solver"] = solver
    s["analyses"]["cfd"].update(cfd_keys)
    return s


# ------------------------------------------------------------------------------------- solver choice
def test_auto_prefers_openfoam_when_both(monkeypatch):
    monkeypatch.setattr(cfd, "_find_foam", lambda: "foam")
    monkeypatch.setattr(cfd, "_find_su2", lambda: _Inst())
    assert cfd.choose_solver(spec_with())[:2] == ("openfoam", "foam")
    assert cfd.choose_solver(spec_with("auto"))[0] == "openfoam"
    assert cfd.choose_solver(spec_with("su2"))[0] == "su2"


def test_auto_picks_su2_when_only_su2(monkeypatch):
    monkeypatch.setattr(cfd, "_find_foam", lambda: None)
    inst = _Inst()
    monkeypatch.setattr(cfd, "_find_su2", lambda: inst)
    assert cfd.choose_solver(spec_with()) == ("su2", inst, None)
    kind, _, err = cfd.choose_solver(spec_with("openfoam"))
    assert kind is None and "OpenFOAM" in err


def test_neither_solver_names_both(monkeypatch, tmp_path):
    monkeypatch.setattr(cfd, "_find_foam", lambda: None)
    monkeypatch.setattr(cfd, "_find_su2", lambda: None)
    r = cfd.run(ctx_for(tmp_path, quad_aircraft(), quad_spec()))
    assert r["status"] == T.ERROR and "OpenFOAM" in r["summary"] and "simpleFoam" in r["summary"]
    assert "SU2" in r["summary"]
    r = cfd.run(ctx_for(tmp_path, quad_aircraft(), spec_with("su2")))
    assert r["status"] == T.ERROR and "SU2" in r["summary"]


def test_unknown_solver_value_is_not_evaluated(monkeypatch, tmp_path):
    r = cfd.run(ctx_for(tmp_path, quad_aircraft(), spec_with("fluent")))
    assert r["status"] == T.NOT_EVALUATED and "analyses.cfd.solver" in r["missing"]


def test_su2_run_is_dispatched_with_the_inputs(monkeypatch, tmp_path):
    monkeypatch.setattr(cfd, "_find_foam", lambda: None)
    monkeypatch.setattr(cfd, "_find_su2", lambda: _Inst())
    seen = {}

    def fake(ctx, inst, V, v_st, alpha, a_st, alpha_default, rho, r_st, preset, iterations, aref, aref_txt, ar_st):
        seen.update(V=V, alpha=alpha, preset=preset, iterations=iterations, aref=aref)
        return T.result("cfd", "x", T.OK, "fake")
    monkeypatch.setattr(cfd_su2, "run_case", fake)
    s = spec_with(None, iterations=77)
    s["analyses"]["cfd"]["alpha_deg"] = 3.0
    r = cfd.run(ctx_for(tmp_path, quad_aircraft(), s))
    assert r["summary"] == "fake"
    assert seen["V"] == 12.0 and seen["alpha"] == 3.0 and seen["iterations"] == 77 and seen["aref"] > 0


# --------------------------------------------------------------------------------------- case writing
@pytest.fixture(scope="module")
def case(tmp_path_factory):
    ac = quad_aircraft()
    d = tmp_path_factory.mktemp("su2case") / "case"
    info = cfd_su2.write_case(d, ac, 15.0, 4.0, 1.225, 0.05, 0.22, "coarse", 50, target_cells=12000)
    return ac, d, info


def _read_su2_mesh(path):
    txt = Path(path).read_text().splitlines()
    pts, marks, cur = [], {}, None
    i = 0
    while i < len(txt):
        ln = txt[i]
        if ln.startswith("NPOIN="):
            n = int(ln.split("=")[1].split()[0])
            pts = np.array([[float(v) for v in txt[i + 1 + k].split()[:3]] for k in range(n)])
            i += n
        elif ln.startswith("MARKER_TAG="):
            cur = ln.split("=")[1].strip()
            marks[cur] = 0
        elif ln.startswith("MARKER_ELEMS="):
            marks[cur] = int(ln.split("=")[1])
        i += 1
    return pts, marks


def test_mesh_has_wall_and_farfield_markers(case):
    ac, d, info = case
    pts, marks = _read_su2_mesh(d / "mesh.su2")
    assert set(marks) == {"body", "farfield"} and marks["body"] > 100 and marks["farfield"] > 100
    assert 0.4 * 12000 < info["cells"] < 2.5 * 12000


def test_domain_contains_the_aircraft_and_props_are_left_out(case):
    ac, d, info = case
    pts, _ = _read_su2_mesh(d / "mesh.su2")
    dom = info["domain_m"]
    lo = np.array([dom["x"][0], dom["y"][0], dom["z"][0]])
    hi = np.array([dom["x"][1], dom["y"][1], dom["z"][1]])
    assert np.allclose(pts.min(0), lo, atol=1e-6) and np.allclose(pts.max(0), hi, atol=1e-6)
    b_lo, b_hi = (np.array(v) for v in info["bbox_m"])
    assert np.all(lo < b_lo) and np.all(hi > b_hi)
    # the body wall nodes lie inside the aircraft's box; props (r 62 mm discs at z 14-15.5) would reach further out
    body_ids = set()
    for ln in (d / "mesh.su2").read_text().split("MARKER_TAG= body")[1].split("MARKER_TAG=")[0].splitlines()[2:]:
        body_ids.update(int(v) for v in ln.split()[1:4])
    wall = pts[sorted(body_ids)]
    assert np.all(wall.min(0) >= b_lo - 1e-6) and np.all(wall.max(0) <= b_hi + 1e-6)
    assert info["omitted_parts"] == [f"prop_{i}" for i in range(1, 5)]
    assert not any(n.startswith("prop") for n in info["parts"])


def test_fused_body_is_one_watertight_volume(case):
    ac, d, info = case
    vols = sum(float(p) for p in [info["body"]["volume_mm3"]])
    parts_sum = 0.0
    for p in cfd._cfd_parts(ac):
        v = p.points[p.triangles]
        parts_sum += abs(np.einsum("ij,ij->", v[:, 0], np.cross(v[:, 1], v[:, 2])) / 6.0)
    assert 0.5 * parts_sum < vols <= parts_sum * 1.0001  # overlaps counted once


def test_config_freestream_is_towards_minus_x_and_references(case):
    ac, d, info = case
    txt = (d / "case.cfg").read_text()
    m = re.search(r"INC_VELOCITY_INIT= \( (\S+), (\S+), (\S+) \)", txt)
    u = np.array([float(m.group(i)) for i in (1, 2, 3)])
    al = math.radians(4.0)
    assert np.allclose(u, 15.0 * np.array([-math.cos(al), 0, math.sin(al)]), atol=1e-6)
    assert "REF_AREA= 0.05" in txt and "REF_LENGTH= 0.22" in txt
    assert "INC_DENSITY_INIT= 1.225" in txt and "MARKER_FAR= ( farfield )" in txt
    assert "MARKER_HEATFLUX= ( body, 0.0 )" in txt and "ITER= 50" in txt
    assert "SOLVER= INC_RANS" in txt and "KIND_TURB_MODEL= SA" in txt


def test_surface_force_integration_on_a_sphere_like_pressure(case):
    """Uniform pressure on a closed surface gives no net force: checks normals out of the fluid."""
    import gmsh  # noqa: F401  (availability)
    ac, d, info = case
    npz = None
    # rebuild a tiny mesh in memory
    from opencae.drone.analyses.cfd_su2 import build_body, choose_sizes, mesh_air, _dom_scaled
    parts = cfd._cfd_parts(ac)
    body, _ = build_body(parts)
    P = np.vstack([p.points for p in parts])
    dom = _dom_scaled(cfd.domain_for(P.min(0) * 1e-3, P.max(0) * 1e-3), 1e3)
    m = mesh_air(body, dom, choose_sizes(parts, dom, 8000, 0.35), d / "t.msh")
    nodes, tets, faces = m["nodes_mm"] * 1e-3, m["tets"], m["markers"]["body"]
    n = cfd_su2.fluid_normals(nodes, tets, faces)
    v = nodes[faces]
    area = 0.5 * np.linalg.norm(np.cross(v[:, 1] - v[:, 0], v[:, 2] - v[:, 0]), axis=1)
    assert np.linalg.norm((n * area[:, None]).sum(0)) < 1e-6 * area.sum()  # closed surface
    # normals point into the body: the centroid of the body wall lies on the side opposite to n
    c = v.mean(1)
    out = c - np.array(info["bbox_m"]).mean(0)
    assert (np.einsum("ij,ij->i", n, out) < 0).mean() > 0.6
    p = np.full(len(nodes), 10.0)
    F = cfd_su2.surface_forces(nodes, tets, faces, p, np.zeros((len(nodes), 3)), [-1, 0, 0], [0, 0, 1])
    assert abs(F["drag"]) < 1e-6 and abs(F["lift"]) < 1e-6


# ------------------------------------------------------------------------------------ boolean failure
def test_boolean_failure_is_an_error_result_not_a_number(monkeypatch, tmp_path):
    monkeypatch.setattr(cfd, "_find_foam", lambda: None)
    monkeypatch.setattr(cfd, "_find_su2", lambda: _Inst())

    def boom(parts):
        raise cfd_su2.MeshError("the boolean union of the parts failed")
    monkeypatch.setattr(cfd_su2, "build_body", boom)
    r = cfd.run(ctx_for(tmp_path, quad_aircraft(), quad_spec()))
    assert r["status"] == T.ERROR and "boolean union" in r["summary"]
    assert not r.get("metrics")


def test_open_part_is_reported(tmp_path):
    P, Tr = box((0, 0, 0), (10, 10, 10))
    parts = [T.Part("lid_missing", "frame", P.astype(float), Tr[:-2].astype(np.int64))]  # two triangles short
    with pytest.raises(cfd_su2.MeshError):
        cfd_su2.build_body(parts)


# ------------------------------------------------------------------------------------------ real run
@pytest.mark.cfd
@pytest.mark.slow
def test_real_coarse_su2_run(tmp_path):
    ac = quad_aircraft()
    s = spec_with("su2", iterations=25)
    ctx = ctx_for(tmp_path, ac, s)
    orig = cfd_su2.PRESETS["coarse"]["cells"]
    cfd_su2.PRESETS["coarse"]["cells"] = 25_000  # keep the unit test short
    try:
        r = cfd.run(ctx)
    finally:
        cfd_su2.PRESETS["coarse"]["cells"] = orig
    assert r["status"] in (T.OK, T.WATCH), r["summary"]
    assert "SU2" in r["solver"]["name"] and "SU2" in r["summary"] and "indicative" in r["summary"]
    m = r["metrics"]
    for k in ("cl", "cd", "ld", "lift_n", "drag_n", "converged", "cells", "iterations"):
        assert k in m, k
    assert m["drag_n"]["value"] > 0 and np.isfinite(m["cl"]["value"]) and m["iterations"]["value"] == 25
    assert 5_000 < m["cells"]["value"] < 80_000
    z = np.load(ctx.out_dir / "cfd_cp.npz")
    assert {"values", "points", "triangles"} <= set(z.files)
    assert len(z["values"]) == len(z["points"]) and z["triangles"].max() < len(z["points"])
    assert float(z["values"].astype(float).max()) > 0.5  # stagnation region on the nose-facing surfaces
    assert "slice_points" in z.files and len(z["slice_points"]) > 0 and "slice_values" in z.files
    for f in ("cfd_p.npz", "cfd_tau_wall.npz"):
        d = cfd.load_surface_field(ctx.out_dir / f)
        assert len(d["values"]) == len(d["points"])
    assert not (ctx.out_dir / "cfd_u_wall.npz").exists()
    fc = r["extra"]["force_check"]
    assert np.allclose(fc["integrated_N"], fc["su2_history_N"], rtol=0.15, atol=0.02)  # own sum vs SU2's


def test_case_is_written_from_a_worker_thread(tmp_path):
    """The app runs analyses in a background thread: meshing must not install signal handlers there
    (gmsh.initialize(interruptible=True) raised 'signal only works in main thread' on the user's PC)."""
    import threading
    from opencae.drone.analyses import cfd_su2
    ac = quad_aircraft()
    out = {}

    def work():
        try:
            out["info"] = cfd_su2.write_case(tmp_path / "case", ac, 15.0, 0.0, 1.225, 0.003, 0.2, "coarse", 10,
                                             target_cells=8000)
        except BaseException as exc:  # noqa: BLE001
            out["error"] = exc

    t = threading.Thread(target=work)
    t.start()
    t.join(300)
    assert "error" not in out, repr(out.get("error"))
    assert (tmp_path / "case" / "mesh.su2").is_file()
