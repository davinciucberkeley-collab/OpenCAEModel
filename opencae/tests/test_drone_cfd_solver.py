"""Drone CFD runs on OpenFOAM only: solver choice, a drone file that still names the retired solver, the error text."""
from __future__ import annotations

import pytest

from opencae.drone import types as T
from opencae.drone.analyses import cfd
from opencae.tests.test_drone_solvers import ctx_for, quad_aircraft, quad_spec


def spec_with(solver=None, **cfd_keys):
    s = quad_spec()
    if solver is not None:
        s["analyses"]["cfd"]["solver"] = solver
    s["analyses"]["cfd"].update(cfd_keys)
    return s


@pytest.mark.parametrize("value", [None, "auto", "openfoam", "OpenFOAM"], ids=["unset", "auto", "named", "mixed-case"])
def test_auto_and_openfoam_choose_openfoam(monkeypatch, value):
    monkeypatch.setattr(cfd, "_find_foam", lambda: "foam")
    assert cfd.choose_solver(spec_with(value)) == ("openfoam", "foam", None)


def test_missing_openfoam_says_how_to_install_it(monkeypatch, tmp_path):
    monkeypatch.setattr(cfd, "_find_foam", lambda: None)
    r = cfd.run(ctx_for(tmp_path, quad_aircraft(), quad_spec()))
    assert r["status"] == T.ERROR and "OpenFOAM" in r["summary"] and "simpleFoam" in r["summary"]
    assert "opencae setup openfoam" in r["summary"] and "Solvers" in r["summary"]


def test_unknown_solver_value_is_not_evaluated(tmp_path):
    r = cfd.run(ctx_for(tmp_path, quad_aircraft(), spec_with("fluent")))
    assert r["status"] == T.NOT_EVALUATED and "analyses.cfd.solver" in r["missing"]


def test_retired_solver_name_is_read_as_openfoam_with_a_note(monkeypatch, tmp_path):
    monkeypatch.setattr(cfd, "_find_foam", lambda: "foam")
    seen = {}

    def fake(ctx, inst, spec, V, v_st, alpha, a_st, alpha_default, rho, r_st, preset, iterations, aref, aref_txt,
             ar_st):
        seen.update(inst=inst, V=V, iterations=iterations, aref=aref)
        return T.result("cfd", "x", T.OK, "Airflow done.")
    monkeypatch.setattr(cfd, "_run_foam", fake)
    r = cfd.run(ctx_for(tmp_path, quad_aircraft(), spec_with("su2", iterations=77)))
    assert seen["inst"] == "foam" and seen["V"] == 12.0 and seen["iterations"] == 77 and seen["aref"] > 0
    assert r["status"] == T.OK and "OpenFOAM was used instead" in r["summary"] and r["summary"].endswith("Airflow done.")
    assert "solver_note" in r["extra"]


def test_a_failed_openfoam_run_is_reported_not_hidden_or_retried(monkeypatch, tmp_path):
    monkeypatch.setattr(cfd, "_find_foam", lambda: "foam")
    calls = []

    def fake(*a, **k):
        calls.append(1)
        return T.result("cfd", "x", T.ERROR, "CFD failed: snappyHexMesh failed", log="tail")
    monkeypatch.setattr(cfd, "_run_foam", fake)
    r = cfd.run(ctx_for(tmp_path, quad_aircraft(), quad_spec()))
    assert calls == [1] and r["status"] == T.ERROR and "snappyHexMesh failed" in r["summary"]
