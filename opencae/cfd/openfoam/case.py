"""OpenFOAM case writer (incompressible steady flow, simpleFoam) for a fluid body.

The fluid body's faces are grouped into patches by the study's flow conditions (inlet, outlet,
slip / symmetry, walls; faces without a condition are walls). Each patch is written as an STL
surface in metres; ``blockMesh`` builds a background box around the body, ``snappyHexMesh``
cuts out the fluid (the cell containing ``locationInMesh`` side), snaps to the surfaces and
feature edges, and adds prism layers on the walls. Works for internal flow (pipe, duct) and for
external flow (an enclosure body = box minus the parts).

Written for OpenFOAM v1912 (ESI), the version of Ubuntu 24.04's ``openfoam`` package; the
dictionary keywords used are also valid in later ESI versions (v2006-v2412).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np

HEADER = """/*--------------------------------*- C++ -*----------------------------------*\\
  Written by OpenCAE Studio
\\*---------------------------------------------------------------------------*/
FoamFile
{{
    version     2.0;
    format      ascii;
    class       {cls};
    object      {obj};
}}
"""


def _dict(cls: str, obj: str, body: str) -> str:
    return HEADER.format(cls=cls, obj=obj) + body.strip() + "\n"


@dataclass
class Patch:
    name: str
    kind: str  # inlet | outlet | wall | slip
    faces: list[int]
    velocity: Optional[list] = None  # inlet: velocity vector [m/s]
    pressure: float = 0.0  # outlet: gauge pressure [Pa]
    level: int = 0  # surface refinement level


@dataclass
class FoamSetup:
    """Everything the case needs, in SI units."""
    scale: float  # project length -> metres
    background_size: float  # m
    patches: list[Patch]
    location_in_mesh: list  # m
    bbox: tuple  # (lo, hi) of the fluid body, m
    nu: float  # kinematic viscosity m^2/s
    rho: float
    model: str = "laminar"  # laminar | sa | sst
    iterations: int = 2000
    residual: float = 1e-5
    turbulence_intensity: float = 0.05
    viscosity_ratio: float = 10.0
    layers: int = 0
    first_layer: float = 0.0  # m
    expansion: float = 1.2
    feature_level: int = 0
    ref_area: float = 0.0  # m^2 (0: no coefficients)
    ref_velocity: float = 0.0
    lift_dir: list = field(default_factory=lambda: [0.0, 0.0, 1.0])
    drag_dir: list = field(default_factory=lambda: [1.0, 0.0, 0.0])
    write_interval: int = 0  # 0: write the last iteration only
    threads: int = 1


# ------------------------------------------------------------------------- surfaces
def write_stl(path: Path, name: str, P: np.ndarray, T: np.ndarray) -> None:
    a, b, c = P[T[:, 0]], P[T[:, 1]], P[T[:, 2]]
    n = np.cross(b - a, c - a)
    ln = np.linalg.norm(n, axis=1)
    n = n / np.where(ln > 0, ln, 1.0)[:, None]
    lines = [f"solid {name}"]
    for k in range(len(T)):
        lines.append(f" facet normal {n[k, 0]:.9e} {n[k, 1]:.9e} {n[k, 2]:.9e}\n  outer loop\n"
                     f"   vertex {a[k, 0]:.12e} {a[k, 1]:.12e} {a[k, 2]:.12e}\n"
                     f"   vertex {b[k, 0]:.12e} {b[k, 1]:.12e} {b[k, 2]:.12e}\n"
                     f"   vertex {c[k, 0]:.12e} {c[k, 1]:.12e} {c[k, 2]:.12e}\n  endloop\n endfacet")
    lines.append(f"endsolid {name}")
    path.write_text("\n".join(lines) + "\n")


def fine_tessellation(shape, deflection: float):
    """(points, triangles, face index) of a shape meshed with a given chordal deflection
    (project units). Faces keep the outward orientation of the solid."""
    from opencae.geometry.occ_kernel import tessellate
    from OCP.BRepTools import BRepTools
    BRepTools.Clean_s(shape)
    t = tessellate(shape, deflection * 1e3)  # tessellate() uses size_hint * 1e-3 as deflection
    return np.asarray(t.points), np.asarray(t.triangles), np.asarray(t.tri_face)


# ------------------------------------------------------------------------- dictionaries
def block_mesh_dict(s: FoamSetup) -> tuple[str, list]:
    lo, hi = (np.asarray(x, float) for x in s.bbox)
    span = hi - lo
    pad = 0.02 * span.max() + 2 * s.background_size
    lo, hi = lo - pad, hi + pad
    n = np.maximum(np.ceil((hi - lo) / s.background_size).astype(int), 1)
    v = [(lo[0], lo[1], lo[2]), (hi[0], lo[1], lo[2]), (hi[0], hi[1], lo[2]), (lo[0], hi[1], lo[2]),
         (lo[0], lo[1], hi[2]), (hi[0], lo[1], hi[2]), (hi[0], hi[1], hi[2]), (lo[0], hi[1], hi[2])]
    verts = "\n".join(f"    ({x:.10g} {y:.10g} {z:.10g})" for x, y, z in v)
    body = f"""
scale 1;
vertices
(
{verts}
);
blocks
(
    hex (0 1 2 3 4 5 6 7) ({n[0]} {n[1]} {n[2]}) simpleGrading (1 1 1)
);
edges ();
boundary
(
    background
    {{
        type patch;
        faces
        (
            (0 4 7 3) (1 2 6 5) (0 1 5 4) (3 7 6 2) (0 3 2 1) (4 5 6 7)
        );
    }}
);
"""
    return _dict("dictionary", "blockMeshDict", body), n.tolist()


def snappy_dict(s: FoamSetup) -> str:
    geo = "\n".join(f"    {p.name}.stl {{ type triSurfaceMesh; name {p.name}; }}" for p in s.patches)
    feats = "\n".join(f"        {{ file \"{p.name}.eMesh\"; level {max(p.level, s.feature_level)}; }}"
                      for p in s.patches)
    ref = "\n".join(
        f"        {p.name} {{ level ({p.level} {p.level}); patchInfo {{ type "
        f"{'wall' if p.kind == 'wall' else 'patch'}; }} }}" for p in s.patches)
    walls = [p for p in s.patches if p.kind == "wall"]
    lay = "\n".join(f"        {p.name} {{ nSurfaceLayers {s.layers}; }}" for p in walls) if s.layers else ""
    x = np.asarray(s.location_in_mesh, float)
    body = f"""
castellatedMesh true;
snap            true;
addLayers       {'true' if s.layers else 'false'};

geometry
{{
{geo}
}}

castellatedMeshControls
{{
    maxLocalCells 2000000;
    maxGlobalCells 20000000;
    minRefinementCells 0;
    maxLoadUnbalance 0.10;
    nCellsBetweenLevels 3;
    features
    (
{feats}
    );
    refinementSurfaces
    {{
{ref}
    }}
    resolveFeatureAngle 30;
    refinementRegions {{}}
    locationInMesh ({x[0]:.10g} {x[1]:.10g} {x[2]:.10g});
    allowFreeStandingZoneFaces true;
}}

snapControls
{{
    nSmoothPatch 3;
    tolerance 2.0;
    nSolveIter 50;
    nRelaxIter 5;
    nFeatureSnapIter 10;
    implicitFeatureSnap false;
    explicitFeatureSnap true;
    multiRegionFeatureSnap true;
}}

addLayersControls
{{
    relativeSizes false;
    layers
    {{
{lay}
    }}
    expansionRatio {s.expansion:g};
    firstLayerThickness {max(s.first_layer, 1e-12):.6g};
    minThickness {max(0.1 * s.first_layer, 1e-12):.6g};
    nGrow 0;
    featureAngle 130;
    slipFeatureAngle 30;
    nRelaxIter 3;
    nSmoothSurfaceNormals 1;
    nSmoothNormals 3;
    nSmoothThickness 10;
    maxFaceThicknessRatio 0.5;
    maxThicknessToMedialRatio 0.3;
    minMedianAxisAngle 90;
    minMedialAxisAngle 90;
    nBufferCellsNoExtrude 0;
    nLayerIter 50;
}}

meshQualityControls
{{
    maxNonOrtho 65;
    maxBoundarySkewness 20;
    maxInternalSkewness 4;
    maxConcave 80;
    minVol 1e-30;
    minTetQuality 1e-15;
    minArea -1;
    minTwist 0.02;
    minDeterminant 0.001;
    minFaceWeight 0.05;
    minVolRatio 0.01;
    minTriangleTwist -1;
    nSmoothScale 4;
    errorReduction 0.75;
    relaxed {{ maxNonOrtho 75; }}
}}

mergeTolerance 1e-6;
"""
    return _dict("dictionary", "snappyHexMeshDict", body)


def feature_dict(s: FoamSetup) -> str:
    items = "\n".join(f"""{p.name}.stl
{{
    extractionMethod extractFromSurface;
    extractFromSurfaceCoeffs {{ includedAngle 150; }}
    writeObj no;
}}""" for p in s.patches)
    return _dict("dictionary", "surfaceFeatureExtractDict", items)


def control_dict(s: FoamSetup) -> str:
    walls = [p.name for p in s.patches if p.kind == "wall"]
    funcs = []
    if walls:
        pl = " ".join(walls)
        funcs.append(f"""    forces
    {{
        type forces;
        libs ("libforces.so");
        writeControl timeStep;
        writeInterval 1;
        patches ({pl});
        rho rhoInf;
        rhoInf {s.rho:.6g};
        CofR (0 0 0);
    }}""")
        for w in walls:
            funcs.append(f"""    forces_{w}
    {{
        type forces;
        libs ("libforces.so");
        writeControl timeStep;
        writeInterval 1;
        patches ({w});
        rho rhoInf;
        rhoInf {s.rho:.6g};
        CofR (0 0 0);
    }}""")
        if s.model != "laminar":
            funcs.append("""    yPlus
    {
        type yPlus;
        libs ("libfieldFunctionObjects.so");
        writeControl writeTime;
    }""")
    funcs.append("""    residuals
    {
        type solverInfo;
        libs ("libutilityFunctionObjects.so");
        fields (U p);
        writeControl timeStep;
        writeInterval 1;
    }""")
    wi = s.write_interval or s.iterations
    body = f"""
application     simpleFoam;
startFrom       startTime;
startTime       0;
stopAt          endTime;
endTime         {s.iterations};
deltaT          1;
writeControl    timeStep;
writeInterval   {wi};
purgeWrite      1;
writeFormat     ascii;
writePrecision  10;
writeCompression off;
timeFormat      general;
timePrecision   8;
runTimeModifiable false;
functions
{{
{chr(10).join(funcs)}
}}
"""
    return _dict("dictionary", "controlDict", body)


def fv_schemes(s: FoamSetup) -> str:
    return _dict("dictionary", "fvSchemes", """
ddtSchemes { default steadyState; }
gradSchemes { default Gauss linear; grad(U) cellLimited Gauss linear 1; }
divSchemes
{
    default none;
    div(phi,U) bounded Gauss linearUpwind grad(U);
    div(phi,k) bounded Gauss upwind;
    div(phi,omega) bounded Gauss upwind;
    div(phi,nuTilda) bounded Gauss upwind;
    div((nuEff*dev2(T(grad(U))))) Gauss linear;
}
laplacianSchemes { default Gauss linear limited corrected 0.5; }
interpolationSchemes { default linear; }
snGradSchemes { default limited corrected 0.5; }
wallDist { method meshWave; }
""")


def fv_solution(s: FoamSetup) -> str:
    r = s.residual
    turb = '"(k|omega|nuTilda)"' if s.model != "laminar" else ""
    rc = f"p {r:g}; U {r / 10:g};" + (f" {turb} {r / 10:g};" if turb else "")
    return _dict("dictionary", "fvSolution", f"""
solvers
{{
    p {{ solver GAMG; smoother GaussSeidel; tolerance 1e-8; relTol 0.05; }}
    "(U|k|omega|nuTilda)" {{ solver smoothSolver; smoother symGaussSeidel; tolerance 1e-9; relTol 0.1; }}
}}
SIMPLE
{{
    nNonOrthogonalCorrectors 1;
    consistent no;
    residualControl {{ {rc} }}
}}
relaxationFactors
{{
    fields {{ p 0.3; }}
    equations {{ U 0.7; ".*" 0.7; }}
}}
""")


def transport(s: FoamSetup) -> str:
    return _dict("dictionary", "transportProperties", f"""
transportModel Newtonian;
nu {s.nu:.8g};
""")


def turbulence(s: FoamSetup) -> str:
    if s.model == "laminar":
        return _dict("dictionary", "turbulenceProperties", "simulationType laminar;")
    m = {"sa": "SpalartAllmaras", "sst": "kOmegaSST"}[s.model]
    return _dict("dictionary", "turbulenceProperties", f"""
simulationType RAS;
RAS {{ RASModel {m}; turbulence on; printCoeffs on; }}
""")


def _inlet_turb(s: FoamSetup, speed: float) -> tuple[float, float, float]:
    k = 1.5 * (speed * s.turbulence_intensity) ** 2
    nut = s.viscosity_ratio * s.nu
    omega = k / nut if nut > 0 else 1.0
    return max(k, 1e-12), max(omega, 1e-6), nut


def field_files(s: FoamSetup) -> dict[str, str]:
    """0/ files. Walls: U noSlip; k, omega, nut wall functions valid from y+ ~ 1 (resolved) to the
    log layer (Spalding's law); slip patches: slip."""
    speeds = [np.linalg.norm(p.velocity) for p in s.patches if p.kind == "inlet" and p.velocity is not None]
    U0 = max(speeds) if speeds else 0.0
    k_in, w_in, nut_in = _inlet_turb(s, U0)
    out = {}

    def bc(fn):
        lines = []
        for p in s.patches:
            lines.append(f"    {p.name}\n    {{\n{fn(p)}\n    }}")
        lines.append("    background\n    {\n        type zeroGradient;\n    }")
        return "\n".join(lines)

    def U(p):
        if p.kind == "inlet":
            v = p.velocity
            return f"        type fixedValue;\n        value uniform ({v[0]:.8g} {v[1]:.8g} {v[2]:.8g});"
        if p.kind == "outlet":
            return "        type inletOutlet;\n        inletValue uniform (0 0 0);\n        value uniform (0 0 0);"
        if p.kind == "slip":
            return "        type slip;"
        return "        type noSlip;"

    def P(p):
        if p.kind == "outlet":
            return f"        type fixedValue;\n        value uniform {p.pressure / s.rho:.8g};"
        if p.kind == "slip":
            return "        type slip;"
        return "        type zeroGradient;"

    def scalar(name, inlet, wall, internal):
        def f(p):
            if p.kind == "inlet":
                return f"        type fixedValue;\n        value uniform {inlet:.8g};"
            if p.kind == "outlet":
                return f"        type inletOutlet;\n        inletValue uniform {inlet:.8g};\n        value uniform {inlet:.8g};"
            if p.kind == "slip":
                return "        type slip;"
            return wall
        return _dict("volScalarField", name, f"dimensions {DIMS[name]};\ninternalField uniform {internal:.8g};\n"
                     f"boundaryField\n{{\n{bc(f)}\n}}")

    out["U"] = _dict("volVectorField", "U", f"dimensions [0 1 -1 0 0 0 0];\ninternalField uniform (0 0 0);\n"
                     f"boundaryField\n{{\n{bc(U)}\n}}")
    out["p"] = _dict("volScalarField", "p", f"dimensions [0 2 -2 0 0 0 0];\ninternalField uniform 0;\n"
                     f"boundaryField\n{{\n{bc(P)}\n}}")
    if s.model == "sst":
        out["k"] = scalar("k", k_in, f"        type kqRWallFunction;\n        value uniform {k_in:.8g};", k_in)
        out["omega"] = scalar("omega", w_in, f"        type omegaWallFunction;\n        value uniform {w_in:.8g};",
                              w_in)
    if s.model == "sa":
        out["nuTilda"] = scalar("nuTilda", nut_in, "        type fixedValue;\n        value uniform 0;", nut_in)
    if s.model != "laminar":
        def nut(p):
            if p.kind == "wall":
                return "        type nutUSpaldingWallFunction;\n        value uniform 0;"
            if p.kind == "slip":
                return "        type slip;"
            return "        type calculated;\n        value uniform 0;"
        out["nut"] = _dict("volScalarField", "nut", "dimensions [0 2 -1 0 0 0 0];\ninternalField uniform 0;\n"
                           f"boundaryField\n{{\n{bc(nut)}\n}}")
    return out


DIMS = {"k": "[0 2 -2 0 0 0 0]", "omega": "[0 0 -1 0 0 0 0]", "nuTilda": "[0 2 -1 0 0 0 0]",
        "nut": "[0 2 -1 0 0 0 0]"}

MESH_SCRIPT = """#!/bin/bash
# OpenCAE Studio: mesh generation (blockMesh + surfaceFeatureExtract + snappyHexMesh + checkMesh)
set -e
set -o pipefail
cd "$(dirname "$0")"
blockMesh > log.blockMesh 2>&1
surfaceFeatureExtract > log.surfaceFeatureExtract 2>&1
snappyHexMesh -overwrite > log.snappyHexMesh 2>&1
checkMesh -latestTime > log.checkMesh 2>&1 || true
foamToVTK -latestTime -noFunctionObjects > log.foamToVTK.mesh 2>&1
echo "OPENCAE MESH DONE"
"""

SOLVE_SCRIPT = """#!/bin/bash
# OpenCAE Studio: steady incompressible solve and export (OPENCAE_NP > 1: parallel with MPI)
set -e
set -o pipefail
cd "$(dirname "$0")"
rm -rf processor* postProcessing VTK [1-9]*
NP=${OPENCAE_NP:-1}
if [ "$NP" -gt 1 ]; then
    sed -i "s/^numberOfSubdomains .*/numberOfSubdomains $NP;/" system/decomposeParDict
    decomposePar -force > log.decomposePar 2>&1
    mpirun -np $NP simpleFoam -parallel 2>&1 | tee log.simpleFoam
    reconstructPar -latestTime > log.reconstructPar 2>&1
else
    simpleFoam 2>&1 | tee log.simpleFoam
fi
foamToVTK -latestTime -noFunctionObjects > log.foamToVTK 2>&1
echo "OPENCAE SOLVE DONE"
"""

DECOMPOSE = """
numberOfSubdomains 2;
method scotch;
"""


def write_case(case: Path, s: FoamSetup, surfaces: dict[str, tuple[np.ndarray, np.ndarray]]) -> None:
    """Write a complete case. ``surfaces``: patch name -> (points [m], triangles)."""
    case = Path(case)
    for d in ("system", "constant/triSurface", "0"):
        (case / d).mkdir(parents=True, exist_ok=True)
    for name, (P, T) in surfaces.items():
        write_stl(case / "constant" / "triSurface" / f"{name}.stl", name, P, T)
    bm, _ = block_mesh_dict(s)
    (case / "system" / "blockMeshDict").write_text(bm)
    (case / "system" / "snappyHexMeshDict").write_text(snappy_dict(s))
    (case / "system" / "surfaceFeatureExtractDict").write_text(feature_dict(s))
    (case / "system" / "controlDict").write_text(control_dict(s))
    (case / "system" / "fvSchemes").write_text(fv_schemes(s))
    (case / "system" / "fvSolution").write_text(fv_solution(s))
    (case / "constant" / "transportProperties").write_text(transport(s))
    (case / "constant" / "turbulenceProperties").write_text(turbulence(s))
    (case / "system" / "decomposeParDict").write_text(_dict("dictionary", "decomposeParDict", DECOMPOSE))
    for old in (case / "0").glob("*"):
        old.unlink()
    for name, txt in field_files(s).items():
        (case / "0" / name).write_text(txt)
    for name, txt in (("Allmesh", MESH_SCRIPT), ("Allrun", SOLVE_SCRIPT)):
        (case / name).write_text(txt, newline="\n")
        (case / name).chmod(0o755)
    (case / "case.foam").write_text("")


def refinement_level(background: float, target: float) -> int:
    """Smallest snappy level n with background / 2^n <= target."""
    if target <= 0 or target >= background:
        return 0
    return int(math.ceil(math.log2(background / target) - 1e-9))
