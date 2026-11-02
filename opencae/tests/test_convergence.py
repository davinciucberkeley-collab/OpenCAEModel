"""Mesh convergence tool."""
import numpy as np
import pytest

from opencae.postprocessing.convergence import assess, run, summary_html


def test_assess_recovers_known_order_and_limit():
    # q(N) = 1 - 2 h^2 with h ~ N^(-1/3): exact order 2, limit 1, unequal refinement ratios
    N = np.array([1e3, 5e3, 6e4])
    q = 1 - 2 * N ** (-2 / 3)
    a = assess(N, q)
    assert a["converging"] and a["order"] == pytest.approx(2.0, rel=1e-6)
    assert a["extrapolated"] == pytest.approx(1.0, rel=1e-9)
    # a diverging quantity (peak at a singularity grows with refinement) is flagged
    b = assess(N, [100.0, 130.0, 190.0])
    assert b["converging"] is False


@pytest.mark.solver
def test_cantilever_convergence_leaves_study_untouched(cantilever):
    s = cantilever
    s.generate_mesh()
    assert s.run_solver()[0].success
    before = (s.project.mesh.mesh_id, s.project.results.directory, len(s.project.studies))
    res = run(s, [20.0, 10.0, 6.0])
    assert (s.project.mesh.mesh_id, s.project.results.directory, len(s.project.studies)) == before
    assert s.project.results_are_current()
    assert res["elements"][0] < res["elements"][1] < res["elements"][2]
    q = res["quantities"]["Max displacement [mm]"]
    assert q[-1] == pytest.approx(1.0, rel=1e-2)  # Euler-Bernoulli 1.0 mm (+ small shear deflection)
    assert "Mesh convergence" in summary_html(res)
