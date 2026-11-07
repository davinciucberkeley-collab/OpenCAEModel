"""OpenFOAM solver backend: run, analyse, mesh conversion, results, self-test."""
from __future__ import annotations

import glob
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np

from opencae.cfd.openfoam.install import INSTALL_HELP, FoamInstall, find_openfoam, version_number


@dataclass
class FoamTestReport:
    ok: bool
    message: str
    details: list = field(default_factory=list)
    seconds: float = 0.0

    def summary(self) -> str:
        return "\n".join([f"OpenFOAM self-test: {self.message} ({self.seconds:.1f} s)"] + list(self.details))


class OpenFOAMBackend:
    name = "OpenFOAM"

    def __init__(self, setting: str = "", distro: str = "") -> None:
        self.install: Optional[FoamInstall] = find_openfoam(setting, distro)
        self.verified: Optional[bool] = None
        self.last_report: Optional[FoamTestReport] = None

    # ------------------------------------------------------------------ interface
    def executable(self) -> Optional[str]:
        return self.install.path if self.install else None

    def version(self) -> str:
        return self.install.version if self.install else ""

    def supported(self) -> bool:
        return bool(self.install) and version_number(self.install.version) >= 2006

    def status_text(self) -> str:
        if not self.install:
            return "OpenFOAM not found. " + INSTALL_HELP.splitlines()[0]
        if not self.supported():
            return (f"{self.install.describe()}: version too old or unusable (Ubuntu's v1912 package aborts in "
                    "function objects). Install ESI OpenFOAM v2006 or later.")
        v = {True: "self-test passed", False: "self-test FAILED", None: "not tested"}[self.verified]
        return f"{self.install.describe()} - {v}"

    def command(self, jobname: str, script: str = "./Allrun") -> list[str]:
        return self.install.argv(".", script)

    def environment(self, threads: int) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items()
               if k not in ("LD_LIBRARY_PATH", "PYTHONHOME", "PYTHONPATH", "QT_PLUGIN_PATH", "MPI_HOME")
               and not k.startswith(("FOAM_", "WM_"))}
        # parallel runs (MPI) only when enabled in the settings: MPI set-ups differ between installations
        from opencae.utilities.settings import get_setting
        env["OPENCAE_NP"] = str(max(1, int(get_setting("openfoam_processes") or 1)))
        return env

    def analyse(self, text: str, returncode: int, job_dir: Path, jobname: str):
        errs, warns = [], []
        logs = ""
        for name in ("log.simpleFoam", "log.snappyHexMesh", "log.blockMesh"):
            f = Path(job_dir) / name
            if f.exists():
                logs += f.read_text(errors="replace")[-20000:]
        both = text + logs
        if "FOAM FATAL" in both:
            m = re.search(r"FOAM FATAL[^\n]*\n(.*?)\n\n", both, re.S)
            errs.append("OpenFOAM error: " + (m.group(1).strip().replace("\n", " ")[:400] if m else "see solver.log"))
            if '"sha1"' in both:
                errs.append("This OpenFOAM build aborts in function objects (typically Ubuntu's own v1912 "
                            "package). Install ESI OpenFOAM v2006 or later (see Tools > Settings > OpenFOAM).")
        if re.search(r"Floating point exception \(core dumped\)|Foam::sigFpe::sigHandler", both):
            errs.append("the solver diverged (floating point exception): reduce the relaxation / refine the "
                        "mesh / check the boundary conditions")
        if "dummy Pstream" in both:
            errs.append("parallel run not possible with this OpenFOAM installation (no MPI): set OpenFOAM processes "
                        "to 1 in Tools > Settings")
        if returncode != 0 and not errs:
            errs.append(f"OpenFOAM exited with code {returncode}")
        ok = returncode == 0 and not errs
        return ok, errs, warns

    # ------------------------------------------------------------------ self-test
    def selftest(self, keep_dir: Optional[Path] = None) -> FoamTestReport:
        """Laminar plane channel (10 mm gap, slip side walls) meshed with snappyHexMesh and solved
        with simpleFoam *with function objects*; the developed pressure gradient must match
        dp/dx = 12 nu U / h^2 within 5 %."""
        t0 = time.time()
        if not self.install:
            self.verified = False
            self.last_report = FoamTestReport(False, "OpenFOAM not found.", INSTALL_HELP.splitlines())
            return self.last_report
        work = Path(keep_dir) if keep_dir else Path(tempfile.mkdtemp(prefix="opencae_foamtest_"))
        work.mkdir(parents=True, exist_ok=True)
        try:
            rep = self._selftest_in(work)
        except Exception as exc:  # noqa: BLE001 - reported to the user
            rep = FoamTestReport(False, f"self-test error: {exc}")
        rep.seconds = time.time() - t0
        self.verified = rep.ok
        self.last_report = rep
        if not keep_dir:
            shutil.rmtree(work, ignore_errors=True)
        return rep

    def _selftest_in(self, case: Path) -> FoamTestReport:
        from opencae.cfd.openfoam.case import FoamSetup, Patch, write_case
        L, h, w = 0.1, 0.01, 0.004
        U, nu = 0.01, 1e-5
        P = np.array([[0, 0, 0], [L, 0, 0], [L, h, 0], [0, h, 0], [0, 0, w], [L, 0, w], [L, h, w], [0, h, w]], float)
        quads = {"inlet": [0, 4, 7, 3], "outlet": [1, 2, 6, 5], "bottom": [0, 1, 5, 4], "top": [3, 7, 6, 2],
                 "front": [0, 3, 2, 1], "back": [4, 5, 6, 7]}

        def tri(q):
            return np.array([[q[0], q[1], q[2]], [q[0], q[2], q[3]]])
        surf = {"inlet": (P, tri(quads["inlet"])), "outlet": (P, tri(quads["outlet"])),
                "walls": (P, np.vstack([tri(quads["bottom"]), tri(quads["top"])])),
                "sides": (P, np.vstack([tri(quads["front"]), tri(quads["back"])]))}
        patches = [Patch("inlet", "inlet", [], velocity=[U, 0, 0]), Patch("outlet", "outlet", []),
                   Patch("walls", "wall", []), Patch("sides", "slip", [])]
        s = FoamSetup(scale=1.0, background_size=h / 10, patches=patches,
                      location_in_mesh=[0.05013, 0.00517, 0.00213], bbox=(P.min(0), P.max(0)), nu=nu, rho=1.0,
                      model="laminar", iterations=1500, residual=1e-6)
        write_case(case, s, surf)
        details = []
        for script, label in (("./Allmesh", "mesh"), ("./Allrun", "solve")):
            r = subprocess.run(self.command("selftest", script), cwd=case, capture_output=True, text=True,
                               env=self.environment(1), timeout=1800)
            ok, errs, _ = self.analyse(r.stdout + r.stderr, r.returncode, case, "selftest")
            if not ok:
                return FoamTestReport(False, f"{label} failed: " + "; ".join(errs), details)
            details.append(f"{label}: ok")
        res = read_case(case, rho=1.0)
        if not res.get("forces"):
            return FoamTestReport(False, "function objects produced no force output", details)
        import pyvista as pv
        g = res["grid"]
        line = pv.Line((0.05, h / 2, w / 2), (0.09, h / 2, w / 2), resolution=40).sample(g)
        k = np.polyfit(line.points[:, 0], line["p"], 1)[0]
        exact = -12 * nu * U / h ** 2
        err = k / exact - 1
        details.append(f"pressure gradient {k:.6g} vs exact {exact:.6g} ({100 * err:+.2f} %)")
        wall = res["forces"]["total"][0]
        details.append(f"wall shear force {wall:.4g} N (function objects work)")
        ok = abs(err) < 0.05
        return FoamTestReport(ok, ("passed" if ok else "FAILED") + f": channel pressure gradient {100 * err:+.2f} % "
                                                                    "from the exact solution", details)


# ---------------------------------------------------------------------- reading
def _latest_vtm(case: Path) -> Optional[Path]:
    files = glob.glob(str(Path(case) / "VTK" / "*.vtm"))
    if not files:
        return None
    return Path(max(files, key=lambda f: int(re.search(r"_(\d+)\.vtm$", f).group(1))
                    if re.search(r"_(\d+)\.vtm$", f) else -1))


def _dat(path: Path) -> np.ndarray:
    rows = []
    for line in path.read_text(errors="replace").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        nums = re.findall(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?", line.replace("(", " ").replace(")", " "))
        rows.append([float(x) for x in nums])
    return np.array(rows) if rows else np.zeros((0, 0))


def read_forces(case: Path, name: str = "forces") -> Optional[dict]:
    f = Path(case) / "postProcessing" / name / "0" / "force.dat"
    if not f.exists():
        return None
    d = _dat(f)
    if d.size == 0 or d.shape[1] < 10:
        return None
    last = d[-1]
    n = max(1, len(d) // 10)  # change over the last 10 % of the iterations
    ref = d[-n - 1] if len(d) > n else d[0]
    tot = last[1:4]
    change = float(np.linalg.norm(tot - ref[1:4]) / max(np.linalg.norm(tot), 1e-300))
    return {"total": last[1:4].tolist(), "pressure": last[4:7].tolist(), "viscous": last[7:10].tolist(),
            "change_last_10pct": change, "history_iterations": d[:, 0].tolist(), "history_total": d[:, 1:4].tolist()}


def read_yplus(case: Path) -> dict:
    f = sorted(glob.glob(str(Path(case) / "postProcessing" / "yPlus" / "*" / "yPlus.dat")))
    out = {}
    if not f:
        return out
    for line in Path(f[-1]).read_text(errors="replace").splitlines():
        if line.startswith("#") or not line.strip():
            continue
        parts = line.split()
        if len(parts) >= 5:
            out[parts[1]] = {"min": float(parts[2]), "max": float(parts[3]), "average": float(parts[4])}
    return out


def read_log(case: Path) -> dict:
    f = Path(case) / "log.simpleFoam"
    txt = f.read_text(errors="replace") if f.exists() else ""
    its = re.findall(r"^Time = (\d+)", txt, re.M)
    m = re.search(r"SIMPLE solution converged in (\d+) iterations", txt)
    res = {k: [float(x) for x in re.findall(rf"Solving for {k}, Initial residual = ([-+\deE.]+)", txt)]
           for k in ("Ux", "Uy", "Uz", "p", "k", "omega", "nuTilda")}
    return {"iterations": int(its[-1]) if its else 0, "converged": bool(m),
            "final_initial_residual": {k: v[-1] for k, v in res.items() if v}}


def read_case(case: Path, rho: float) -> dict:
    import pyvista as pv
    out = {"forces": read_forces(case), "yplus": read_yplus(case), "log": read_log(case)}
    v = _latest_vtm(case)
    if v is not None:
        mb = pv.read(str(v))
        out["grid"] = mb["internal"]
        out["boundary"] = {k: mb["boundary"][k] for k in mb["boundary"].keys()} if "boundary" in mb.keys() else {}
    walls = [p.name for p in Path(case, "postProcessing").glob("forces_*")] \
        if (Path(case) / "postProcessing").exists() else []
    out["wall_forces"] = {w[len("forces_"):]: read_forces(case, w) for w in walls}
    return out


def patch_flux(poly, rho: float) -> dict:
    """Volume flow (outward), area and mean pressure of a boundary patch (OpenFOAM face data)."""
    pd = poly.compute_cell_sizes(length=False, area=True, volume=False)
    A = np.asarray(pd.cell_data["Area"])
    n = np.asarray(poly.compute_normals(cell_normals=True, point_normals=False, auto_orient_normals=False,
                                        consistent_normals=False, split_vertices=False).cell_data["Normals"])
    U = np.asarray(poly.cell_data["U"])
    p = np.asarray(poly.cell_data["p"]) * rho
    q = float(np.sum(np.einsum("ij,ij->i", U, n) * A))
    return {"flow_m3s": q, "area_m2": float(A.sum()), "mean_pressure_Pa": float(np.sum(p * A) / max(A.sum(), 1e-300))}


def grid_to_femesh(grid, scale: float, body_id: str, boundary: Optional[dict] = None):
    """Display mesh: OpenFOAM cells split into tetrahedra (points unchanged, in project units), with the
    real boundary faces of each patch (``boundary``: patch name -> foamToVTK patch polydata) mapped onto
    the same points."""
    from opencae.meshing.mesh_data import FEMesh
    t = grid.triangulate()
    cells = np.asarray(t.cells_dict[10], dtype=np.int64) if hasattr(t, "cells_dict") else \
        np.asarray(t.cells).reshape(-1, 5)[:, 1:]
    m = FEMesh(nodes=np.asarray(grid.points, float) / scale, elements=cells, element_type="C3D4", body_id=body_id)
    m.source = "openfoam"
    if boundary:
        tris, pid = boundary_triangles(grid, boundary)
        if tris is not None:
            m.boundary_tris, m.boundary_patch = tris, pid
            m.meta["boundary_patches"] = list(boundary.keys())
    return m


def boundary_triangles(grid, boundary: dict):
    """Patch faces as triangles on the internal-mesh points (matched by coordinates)."""
    from scipy.spatial import cKDTree
    P = np.asarray(grid.points, float)
    tree = cKDTree(P)
    tol = 1e-9 * max(float(np.ptp(P, axis=0).max()), 1e-30)
    tris, pid = [], []
    for k, (name, poly) in enumerate(boundary.items()):
        if poly is None or poly.n_cells == 0:
            continue
        tp = poly.triangulate()
        faces = np.asarray(tp.faces).reshape(-1, 4)[:, 1:] if tp.n_cells else np.zeros((0, 3), int)
        d, idx = tree.query(np.asarray(tp.points, float))
        if np.any(d > tol):
            return None, None  # points do not match the internal mesh: keep the old display
        tris.append(idx[faces])
        pid.append(np.full(len(faces), k))
    if not tris:
        return None, None
    return np.concatenate(tris).astype(np.int64), np.concatenate(pid).astype(np.int64)


# ---------------------------------------------------------------------- meshing step
MESH_STEPS = (("blockMesh", "background mesh (blockMesh)"),
              ("surfaceFeatureExtract", "feature edges (surfaceFeatureExtract)"),
              ("snappyHexMesh -overwrite", "castellation, snapping, wall layers (snappyHexMesh)"),
              ("checkMesh -latestTime", "mesh check (checkMesh)"),
              ("foamToVTK -latestTime -noFunctionObjects", "export for display (foamToVTK)"))


def parse_check_mesh(text: str) -> dict:
    out = {"ok": "Mesh OK." in text, "failed": 0, "cells": None, "max_non_ortho": None, "max_skewness": None,
           "messages": []}
    m = re.search(r"Failed (\d+) mesh checks", text)
    if m:
        out["failed"] = int(m.group(1))
    m = re.search(r"^\s*cells:\s+(\d+)", text, re.M)
    if m:
        out["cells"] = int(m.group(1))
    m = re.search(r"Mesh non-orthogonality Max: ([\d.eE+-]+)", text)
    if m:
        out["max_non_ortho"] = float(m.group(1))
    m = re.search(r"Max skewness = ([\d.eE+-]+)", text)
    if m:
        out["max_skewness"] = float(m.group(1))
    out["messages"] = [l.strip(" *") for l in text.splitlines() if l.strip().startswith("***")]
    return out


def parse_layers(text: str) -> list[dict]:
    """Final snappyHexMesh layer summary: patch, faces, layers (average achieved), overall thickness
    [m] and [% of target]. Handles both table formats (v2006+: 'target mesh' columns; older:
    one 'layers' column)."""
    idx = [m.start() for m in re.finditer(r"\n\s*patch\s+faces\s+layers\s+overall thickness", text)]
    if not idx:
        return []
    lines = text[idx[-1]:].lstrip("\n").splitlines()[1:]
    two = len(lines) > 0 and "target" in lines[0]
    rows = []
    for line in lines[2 if two else 1:]:
        parts = line.split()
        if not parts or set(line.strip()) <= set("- "):
            if rows:
                break
            continue
        try:
            if two and len(parts) >= 6:
                rows.append({"patch": parts[0], "faces": int(parts[1]), "target": float(parts[2]),
                             "layers": float(parts[3]), "thickness_m": float(parts[4]),
                             "thickness_pct": float(parts[5])})
            elif len(parts) >= 5:
                rows.append({"patch": parts[0], "faces": int(parts[1]), "layers": float(parts[2]),
                             "thickness_m": float(parts[3]), "thickness_pct": float(parts[4])})
            else:
                break
        except ValueError:
            break
    return rows


def _final_mesh_to_constant(case: Path) -> None:
    """Make constant/polyMesh the final snappyHexMesh mesh (move a time-folder polyMesh there) and
    check its point count against snappyHexMesh's own report."""
    times = [d for d in case.iterdir() if d.is_dir() and re.match(r"^[0-9.eE+-]+$", d.name) and
             (d / "polyMesh").exists()]
    if times:
        newest = max(times, key=lambda d: float(d.name))
        dst = case / "constant" / "polyMesh"
        # a time folder holds only what changed (often just 'points'): overlay it on constant/polyMesh
        for f in (newest / "polyMesh").iterdir():
            target = dst / f.name
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists():
                target.unlink()
            shutil.move(str(f), str(target))
        for d in times:
            shutil.rmtree(d / "polyMesh", ignore_errors=True)
            if not any(d.iterdir()):
                d.rmdir()
    log = (case / "log.snappyHexMesh").read_text(errors="replace") if (case / "log.snappyHexMesh").exists() else ""
    m = re.findall(r"(?:Layer mesh|Snapped mesh|Mesh) : cells:(\d+)\s+faces:(\d+)\s+points:(\d+)", log)
    pts = case / "constant" / "polyMesh" / "points"
    if m and pts.exists():
        head = pts.read_text(errors="replace")[:4000]
        n = re.search(r"\n(\d+)\s*\n\(", head)
        if n and int(n.group(1)) != int(m[-1][2]):
            raise RuntimeError(f"OpenFOAM mesh files are inconsistent: constant/polyMesh has {n.group(1)} points, "
                               f"snappyHexMesh reported {m[-1][2]}")


def run_mesh(job: dict, progress=None):
    """Run the OpenFOAM meshing tools in the case folder and return the display mesh."""
    import pyvista as pv
    say = progress or (lambda m: None)
    inst = FoamInstall(**job["install"])
    case = Path(job["case"])
    env = {k: v for k, v in os.environ.items() if k not in ("LD_LIBRARY_PATH", "PYTHONHOME", "PYTHONPATH")}
    # with a '0' time folder present, 'snappyHexMesh -overwrite' writes the final mesh to 0/polyMesh and
    # leaves an intermediate mesh in constant/polyMesh: mesh without the field files
    if (case / "0").exists():
        shutil.rmtree(case / "0.fields", ignore_errors=True)
        (case / "0").rename(case / "0.fields")
    log = []
    for cmd, label in MESH_STEPS:
        if cmd.startswith("checkMesh"):
            _final_mesh_to_constant(case)
        say(label)
        t0 = time.time()
        tool = cmd.split()[0]
        r = subprocess.run(inst.argv(".", f"{cmd} > log.{tool} 2>&1"), cwd=case, capture_output=True, text=True,
                           env=env)
        txt = (case / f"log.{tool}").read_text(errors="replace") if (case / f"log.{tool}").exists() else ""
        if "FOAM FATAL" in txt or (r.returncode != 0 and tool != "checkMesh"):
            m = re.search(r"FOAM FATAL[^\n]*\n(.*?)\n\n", txt, re.S)
            raise RuntimeError(f"{tool} failed: " + (m.group(1).strip().replace("\n", " ")[:500] if m else
                                                     (r.stderr or txt[-500:]).strip()))
        log.append(f"{tool}: {time.time() - t0:.1f} s")
    if (case / "0.fields").exists():
        shutil.rmtree(case / "0", ignore_errors=True)
        (case / "0.fields").rename(case / "0")
    chk = parse_check_mesh((case / "log.checkMesh").read_text(errors="replace"))
    snappy_log = (case / "log.snappyHexMesh").read_text(errors="replace")
    layers = parse_layers(snappy_log)
    cov = re.findall(r"Extruding (\d+) out of (\d+) faces \(([\d.]+)%\)", snappy_log)
    coverage = float(cov[-1][2]) if cov else None
    v = _latest_vtm(case)
    if v is None:
        raise RuntimeError("foamToVTK wrote no output")
    mb = pv.read(str(v))
    grid = mb["internal"]
    bnd = {k: mb["boundary"][k] for k in mb["boundary"].keys()} if "boundary" in mb.keys() else None
    mesh = grid_to_femesh(grid, job["scale"], job["body_id"], bnd)
    warnings = []
    nl = int(job["settings"].get("bl_layers") or 0)
    if nl and coverage is not None and coverage < 80.0:
        warnings.append(f"wall layers were added on only {coverage:.0f} % of the wall faces (snappyHexMesh); "
                        "near-wall results (shear, y+, drag) are affected - refine less or change the layer settings")
    if not chk["ok"]:
        warnings.append(f"checkMesh: {chk['failed']} check(s) failed: " + "; ".join(chk["messages"][:3]))
    for row in layers:
        if row["layers"] < 0.8 * max(1, job["settings"].get("bl_layers") or 0) and (job["settings"].get("bl_layers")
                                                                                    or 0):
            warnings.append(f"wall layers on '{row['patch']}': {row['layers']:.2f} of "
                            f"{job['settings']['bl_layers']} layers on average ({row['thickness_pct']:.0f} % of "
                            "the requested thickness) - snappyHexMesh could not add them everywhere")
    mesh.meta = {**mesh.meta, "case_rel": job["case_rel"], "patches": sorted([[p["kind"], sorted(p["faces"])]
                                                                 for p in job["patches"]]),
                 "patch_names": {p["name"]: p["kind"] for p in job["patches"]}, "check": chk, "layers": layers,
                 "check_warnings": warnings, "cells": chk["cells"] or int(grid.n_cells),
                 "layer_coverage_pct": coverage}
    mesh.log = ["OpenFOAM mesh (blockMesh + snappyHexMesh) " + ", ".join(log),
                f"{mesh.meta['cells']} cells, {grid.n_points} points; display mesh: {mesh.n_elements} tetrahedra",
                "checkMesh: " + ("Mesh OK" if chk["ok"] else f"{chk['failed']} failed check(s)") +
                (f", max non-orthogonality {chk['max_non_ortho']:.1f}°" if chk["max_non_ortho"] is not None else "") +
                (f", max skewness {chk['max_skewness']:.2f}" if chk["max_skewness"] is not None else "")]
    if nl:
        mesh.log.append(f"wall layers: {coverage if coverage is not None else 0:.0f} % of wall faces extruded")
    for row in layers:
        mesh.log.append(f"layers on {row['patch']}: {row['layers']:.2f} average, overall {row['thickness_m']:.3g} m "
                        f"({row['thickness_pct']:.0f} % of target)")
    mesh.log += warnings
    say(f"OpenFOAM mesh: {mesh.meta['cells']} cells")
    return mesh


# ---------------------------------------------------------------------- solve step
def write_solve_case(session, job_dir: Path) -> None:
    """Write the solver case (current physics settings) and copy the mesh of the study."""
    from opencae.cfd.openfoam import study as foam_study
    mesh = session.mesh
    src = session.data_dir / mesh.meta["case_rel"] / "constant" / "polyMesh"
    setup, issues = foam_study.build(session, job_dir)
    errs = [str(i) for i in issues if i.severity == "error"]
    if errs or setup is None:
        raise RuntimeError("; ".join(errs) or "could not write the OpenFOAM case")
    dst = job_dir / "constant" / "polyMesh"
    shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(src, dst)
    names = {p.name for p in setup.patches}
    if names != set(mesh.meta.get("patch_names", {})):
        raise RuntimeError("the OpenFOAM mesh patches do not match the flow conditions - regenerate the mesh")
    (job_dir / "resolved_loads.json").write_text("{}")
    (job_dir / "foam_setup.json").write_text(json.dumps(
        {"rho": setup.rho, "nu": setup.nu, "model": setup.model, "ref_area": setup.ref_area,
         "ref_velocity": setup.ref_velocity, "scale": setup.scale,
         "patches": [{"name": p.name, "kind": p.kind, "faces": p.faces, "velocity": p.velocity,
                      "pressure": p.pressure} for p in setup.patches]}))


def read_results(session, job_dir: Path):
    from opencae.cfd.results import CFDResultSet
    from opencae.postprocessing.results import ResultsError
    meta = json.loads((job_dir / "foam_setup.json").read_text())
    rho = meta["rho"]
    res = read_case(job_dir, rho)
    mesh = session.mesh
    g = res.get("grid")
    if g is None:
        raise ResultsError("OpenFOAM wrote no field output (foamToVTK).")
    if g.n_points != mesh.n_nodes:
        raise ResultsError(f"OpenFOAM results have {g.n_points} points, the display mesh {mesh.n_nodes}: the mesh "
                           "changed - regenerate the mesh and solve again.")
    vel = np.asarray(g.point_data["U"], float)
    pres = np.asarray(g.point_data["p"], float) * rho  # kinematic -> Pa (gauge)
    mut = np.asarray(g.point_data["nut"], float) * rho if "nut" in g.point_data else None
    info = {"solver": f"OpenFOAM {session.solver_backend().version()} simpleFoam", "model": meta["model"]}
    lg = res["log"]
    info["iterations"] = lg["iterations"]
    info["converged"] = lg["converged"]
    info["final_initial_residuals"] = lg["final_initial_residual"]
    if res["forces"]:
        f = res["forces"]
        info["wall_force_N"] = f["total"]
        info["wall_force_pressure_N"] = f["pressure"]
        info["wall_force_viscous_N"] = f["viscous"]
        info["force_change_last_10pct"] = f["change_last_10pct"]
    info["wall_forces_by_patch_N"] = {k: v["total"] for k, v in res["wall_forces"].items() if v}
    V = meta["ref_velocity"]
    if meta["ref_area"] > 0 and V > 0 and "wall_force_N" in info:
        q = 0.5 * rho * V ** 2 * meta["ref_area"]
        info["force_coefficients"] = [x / q for x in info["wall_force_N"]]
        info["dynamic_pressure_Pa"] = 0.5 * rho * V ** 2
    if res["yplus"]:
        info["y_plus"] = res["yplus"]
    markers = {}
    kinds = {p["name"]: p["kind"] for p in meta["patches"]}
    for name, poly in (res.get("boundary") or {}).items():
        if kinds.get(name) in ("inlet", "outlet") and poly.n_cells:
            m = patch_flux(poly, rho)
            m["kind"] = {"inlet": "velocity_inlet", "outlet": "pressure_outlet"}[kinds[name]]
            m["source"] = name
            m["mass_flow_kgs"] = rho * m["flow_m3s"]
            markers[name] = m
    info["markers"] = markers
    ins = [m for m in markers.values() if m["kind"] == "velocity_inlet"]
    outs = [m for m in markers.values() if m["kind"] == "pressure_outlet"]
    if ins and outs:
        qin = -sum(m["flow_m3s"] for m in ins)
        qout = sum(m["flow_m3s"] for m in outs)
        info["inflow_m3s"], info["outflow_m3s"] = qin, qout
        info["flow_balance_rel"] = abs(qin - qout) / max(abs(qin), 1e-300)
        info["mass_imbalance"] = info["flow_balance_rel"]
        info["pressure_drop_Pa"] = (sum(m["mean_pressure_Pa"] * m["area_m2"] for m in ins) /
                                    sum(m["area_m2"] for m in ins) -
                                    sum(m["mean_pressure_Pa"] * m["area_m2"] for m in outs) /
                                    sum(m["area_m2"] for m in outs))
        info["nominal_inflow_m3s"] = sum(float(np.linalg.norm(p["velocity"])) * markers[p["name"]]["area_m2"]
                                         for p in meta["patches"] if p["kind"] == "inlet" and p["name"] in markers)
    info["mesh_cells"] = mesh.meta.get("cells")
    return CFDResultSet(mesh, vel, pres, mut, rho, info)
