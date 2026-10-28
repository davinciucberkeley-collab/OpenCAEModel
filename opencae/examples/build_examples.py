"""Build (and solve) the example projects with the OpenCAE scripting API.

    python -m opencae.examples.build_examples [--out DIR] [--no-solve]

This file doubles as a reference for scripting OpenCAE Studio: every step the
GUI performs is a call on :class:`opencae.app.session.Session`.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from opencae.app.session import Session
from opencae.geometry.references import find_planar_face, make_ref
from opencae.postprocessing.report import write_report
from opencae.project.model import Constraint, Feature, Load, MeshRefinement

DEFAULT_OUT = Path(__file__).parent / "projects"


def cantilever(solve: bool) -> Session:
    """First end-to-end demonstration: steel cantilever with a tip load."""
    s = Session()
    s.new("Cantilever beam", "mm-N-s")
    s.project.description = ("200 x 20 x 20 mm structural steel cantilever, root face fixed, 1000 N tip load in -Z. "
                             "Compare with Euler-Bernoulli: FL^3/(3EI) = 1.000 mm.")
    s.create_cantilever(length=200.0, width=20.0, height=20.0, force=1000.0)
    s.project.mesh_settings.size_max = 4.0
    s.project.mesh_settings.element_order = 2
    if solve:
        s.generate_mesh()
        out, issues = s.run_solver()
        assert out and out.success, issues
    return s


def plate_with_hole(solve: bool) -> Session:
    """Quarter model of a plate with a central hole under uniaxial tension."""
    s = Session()
    s.new("Plate with hole (quarter model)", "mm-N-s")
    s.project.description = (
        "Plate 200 x 100 mm with a 20 mm hole, 5 mm thick, nominal tension 100 MPa. Quarter model with half "
        "thickness and symmetry on x = 0, y = 0 and z = 0. Reference: Heywood Kt(net) = 2 + (1 - d/W)^3 = 2.512, "
        "net stress 125 MPa -> peak sigma_xx ~ 314 MPa at the hole edge (y = r).")
    plate = s.add_feature(Feature("primitive", {"shape": "box", "dx": 100.0, "dy": 50.0, "dz": 2.5}, name="Quarter plate"))
    hole = s.add_feature(Feature("primitive", {"shape": "cylinder", "radius": 10.0, "height": 10.0,
                                               "position": [0, 0, -5]}, name="Hole"))
    s.add_feature(Feature("boolean", {"target": plate.id, "tool": hole.id, "op": "subtract"}, name="Cut hole"))
    b = s.analysis_body()
    s.project.assign_material(b.id, "mat_steel")
    sx = find_planar_face(b, [-1, 0, 0], [0, 0, 0])
    sy = find_planar_face(b, [0, -1, 0], [0, 0, 0])
    sz = find_planar_face(b, [0, 0, -1], [0, 0, 0])
    end = find_planar_face(b, [1, 0, 0], [100, 0, 0])
    hole_face = next(f.index for f in b.faces if f.surface_type == "cylinder")
    s.project.constraints = [Constraint("symmetry", [make_ref(b, "face", sx)], name="Symmetry X"),
                             Constraint("symmetry", [make_ref(b, "face", sy)], name="Symmetry Y"),
                             Constraint("symmetry", [make_ref(b, "face", sz)], name="Symmetry Z (mid-plane)")]
    s.project.loads = [Load("pressure", [make_ref(b, "face", end)], {"magnitude": -100.0},
                            name="Tension 100 MPa (negative pressure)")]
    s.project.mesh_settings.size_max = 6.0
    s.project.mesh_settings.refinements = [MeshRefinement([make_ref(b, "face", hole_face)], size=1.0,
                                                          name="Hole refinement")]
    if solve:
        s.generate_mesh()
        out, issues = s.run_solver()
        assert out and out.success, issues
    return s


def bracket(solve: bool) -> Session:
    """L-bracket with fillet and bolt hole: fixed base, load on the upright, self-weight."""
    s = Session()
    s.new("L-bracket", "mm-N-s")
    s.project.description = "Aluminium L-bracket with an inner fillet and a bolt hole; fixed base, 500 N load and gravity."
    base = s.add_feature(Feature("primitive", {"shape": "box", "dx": 80.0, "dy": 40.0, "dz": 10.0}, name="Base"))
    up = s.add_feature(Feature("primitive", {"shape": "box", "dx": 10.0, "dy": 40.0, "dz": 70.0}, name="Upright"))
    s.add_feature(Feature("boolean", {"target": base.id, "tool": up.id, "op": "union"}, name="Union"))
    b = s.analysis_body()
    inner = [e.index for e in b.edges if e.curve_type == "line" and abs(e.centroid[0] - 10) < 1e-6
             and abs(e.centroid[2] - 10) < 1e-6]
    s.add_feature(Feature("fillet", {"body": base.id, "radius": 6.0, "edges": inner}, name="Inner fillet"))
    bolt = s.add_feature(Feature("primitive", {"shape": "cylinder", "radius": 5.0, "height": 20.0,
                                               "position": [55, 20, -5]}, name="Bolt hole"))
    s.add_feature(Feature("boolean", {"target": base.id, "tool": bolt.id, "op": "subtract"}, name="Cut bolt hole"))
    b = s.analysis_body()
    s.project.assign_material(b.id, "mat_al6061")
    bottom = find_planar_face(b, [0, 0, -1], [0, 0, 0])
    top = find_planar_face(b, [0, 0, 1], [0, 0, 70])
    s.project.constraints = [Constraint("fixed", [make_ref(b, "face", bottom)], name="Fixed base")]
    s.project.loads = [Load("force", [make_ref(b, "face", top)], {"magnitude": 500.0, "direction": [1, 0, 0],
                                                                  "coordinate_system": "global"}, name="Side load 500 N"),
                       Load("gravity", [], {"magnitude": 9806.65, "direction": [0, 0, -1]}, name="Gravity")]
    s.project.mesh_settings.size_max = 4.0
    if solve:
        s.generate_mesh()
        out, issues = s.run_solver()
        assert out and out.success, issues
    return s


EXAMPLES = {"cantilever_beam": cantilever, "plate_with_hole": plate_with_hole, "l_bracket": bracket}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--no-solve", action="store_true")
    args = ap.parse_args(argv)
    out = Path(args.out)
    for name, fn in EXAMPLES.items():
        s = fn(not args.no_solve)
        path = s.save(out / f"{name}.ocae")
        if s.results is not None:
            write_report(s, out / f"{name}_data" / "report" / f"{name}_report.html")
            sm = s.results.summary()
            print(f"{name}: {s.mesh.n_nodes} nodes, max |U| = {sm['max_displacement']:.5g} mm, "
                  f"max VM = {sm.get('max_von_mises_nodal', 0):.5g} MPa -> {path}")
        else:
            print(f"{name}: saved (unsolved) -> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
