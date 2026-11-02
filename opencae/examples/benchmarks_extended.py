"""Extended validation benchmarks (thermal, dynamics, stability, thermo-mechanics, flow).

Each benchmark runs the real pipeline (OCC geometry -> Gmsh -> CalculiX or SU2 -> parsed
results) and compares one or more quantities with a closed-form solution or an
established reference. Rows: name, quantity, fe, reference, err_pct, tol_pct, pass, note.
"""
from __future__ import annotations

import math
import time
from typing import Callable

import numpy as np

from opencae.app.session import Session
from opencae.geometry.references import find_planar_face, make_ref
from opencae.postprocessing.sampling import locate
from opencae.project.model import Constraint, Feature, Load, MeshRefinement

E, NU, RHO, ALPHA, K, C = 200e3, 0.3, 7850.0, 12e-6, 50.0, 470.0  # example steel (mm-N-s, SI thermal)


def _row(name, qty, fe, ref, tol_pct, note="", unit=""):
    err = (fe / ref - 1) * 100 if ref else float("nan")
    return {"name": name, "quantity": qty, "fe": float(fe), "reference": float(ref), "unit": unit,
            "err_pct": float(err), "tol_pct": tol_pct, "pass": bool(abs(err) <= tol_pct), "note": note}


def _solve(s):
    out, issues = s.run_solver()
    if out is None or not out.success:
        raise RuntimeError(f"solve failed: {[str(i) for i in issues if i.severity == 'error']} "
                           f"{out.errors if out else ''}")
    return s.results


def _bar(L=100.0, a=10.0, h=5.0, analysis="thermal_steady"):
    s = Session()
    s.new("bench")
    s.add_feature(Feature("primitive", {"shape": "box", "dx": L, "dy": a, "dz": a}))
    b = s.analysis_body()
    s.project.assign_material(b.id, "mat_steel")
    s.project.analysis.analysis_type = analysis
    s.project.mesh_settings.size_max = h
    f = lambda n, p: make_ref(b, "face", find_planar_face(b, n, p))  # noqa: E731
    return s, b, {"x0": f([-1, 0, 0], [0, 0, 0]), "xL": f([1, 0, 0], [L, 0, 0]), "y0": f([0, -1, 0], [0, 0, 0]),
                  "z0": f([0, 0, -1], [0, 0, 0])}


# ----------------------------------------------------------------------------- thermal
def thermal(log) -> list[dict]:
    rows = []
    s, b, f = _bar()
    s.project.constraints = [Constraint("temperature", [f["x0"]], {"value": 20.0}, name="cold")]
    s.project.loads = [Load("heat_flux", [f["xL"]], {"magnitude": 1e4}, name="q")]
    s.generate_mesh()
    rs = _solve(s)
    T = rs.quantity("T")
    rows.append(_row("T1 slab, flux + fixed T", "T(L) [°C]", T.max(), 20 + 1e4 * 0.1 / K, 0.01,
                     "T = T0 + q x / k", "°C"))
    sm = s.project.results.summary
    rows.append(_row("T1 slab, flux + fixed T", "heat through fixed face [W]",
                     -sm["heat_at_fixed_temperatures_W"]["T_ALL_FIXED"], 1.0, 0.01, "energy balance", "W"))
    s.project.constraints = []
    s.project.loads = [Load("heat_flux", [f["xL"]], {"magnitude": 1e4}, name="q"),
                       Load("convection", [f["x0"]], {"film": 100.0, "ambient": 20.0}, name="h")]
    rs = _solve(s)
    rows.append(_row("T2 slab, convection", "T(0) [°C]", rs.quantity("T").min(), 120.0, 0.01,
                     "q = h (T0 - T_inf)", "°C"))
    s.project.loads[1] = Load("radiation", [f["x0"]], {"emissivity": 0.8, "ambient": 20.0}, name="r")
    rs = _solve(s)
    T0 = (1e4 / (0.8 * 5.670374419e-8) + 293.15 ** 4) ** 0.25 - 273.15
    rows.append(_row("T3 slab, radiation", "T(0) [°C]", rs.quantity("T").min(), T0, 0.01,
                     "q = eps sigma (T0^4 - T_inf^4)", "°C"))
    s2, b2, f2 = _bar(h=4.0)
    s2.project.constraints = [Constraint("temperature", [f2["x0"], f2["xL"]], {"value": 0.0}, name="ends")]
    s2.project.loads = [Load("heat_generation", [], {"magnitude": 1e6}, name="qv")]
    s2.generate_mesh()
    rs = _solve(s2)
    rows.append(_row("T4 heat generation", "T max [°C]", rs.quantity("T").max(), 1e6 * 0.1 ** 2 / (8 * K), 0.1,
                     "T_max = q L^2 / (8 k)", "°C"))
    for dt in (10.0, 5.0):
        s3, b3, f3 = _bar(h=5.0, analysis="thermal_transient")
        a = s3.project.analysis
        a.initial_temperature, a.time_total, a.time_step = 20.0, 1000.0, dt
        s3.project.constraints = [Constraint("temperature", [f3["x0"]], {"value": 120.0}, name="hot")]
        s3.generate_mesh()
        rs = _solve(s3)
        L, al = 0.1, K / (RHO * C)
        k = len(rs.times) // 2
        t = rs.times[k]
        S = sum(4 / ((2 * n + 1) * math.pi) * math.sin((2 * n + 1) * math.pi / 2) *
                math.exp(-((2 * n + 1) * math.pi / (2 * L)) ** 2 * al * t) for n in range(200))
        ref = 120.0 - 100.0 * S
        fe = float(rs.temperature[k][np.isclose(s3.mesh.nodes[:, 0], 100.0)].mean())
        rows.append(_row(f"T5 transient conduction, dt = {dt:g} s", f"T(L, t = {t:g} s) [°C]", fe, ref, 1.5,
                         "Fourier series; backward Euler error ~ dt", "°C"))
    log("  thermal benchmarks done")
    return rows


# ----------------------------------------------------------------------------- dynamics / stability
def dynamics(log) -> list[dict]:
    rows = []
    s = Session()
    s.create_cantilever(200.0, 20.0, 20.0, 1000.0)
    s.project.analysis.analysis_type = "modal"
    s.project.analysis.n_modes = 6
    s.project.mesh_settings.size_max = 5.0
    s.generate_mesh()
    rs = _solve(s)
    Es, a, L = 200e9, 0.02, 0.2
    I, A = a ** 4 / 12, a * a
    f1 = 1.875104 ** 2 / (2 * math.pi) * math.sqrt(Es * I / (RHO * A * L ** 4))
    ft = math.sqrt(Es / 2.6 * 0.1406 * a ** 4 / (RHO * 2 * I)) / (4 * L)
    fa = math.sqrt(Es / RHO) / (4 * L)
    fr = rs.frequencies
    rows.append(_row("M1 cantilever modes", "f1 bending [Hz]", fr[0], f1, 1.0, "Euler-Bernoulli", "Hz"))
    rows.append(_row("M1 cantilever modes", "torsion [Hz]", fr[np.argmin(abs(fr / ft - 1))], ft, 1.0,
                     "square section, J = 0.1406 a^4", "Hz"))
    rows.append(_row("M1 cantilever modes", "axial [Hz]", fr[np.argmin(abs(fr / fa - 1))], fa, 1.0,
                     "quarter-wave rod", "Hz"))
    s, b, f = _bar(L=200.0, a=10.0, h=4.0, analysis="buckling")
    s.project.analysis.n_modes = 2
    s.project.constraints = [Constraint("fixed", [f["x0"]], name="base")]
    s.project.loads = [Load("force", [f["xL"]], {"magnitude": 1000.0, "direction": [-1, 0, 0]}, name="P")]
    s.generate_mesh()
    rs = _solve(s)
    pcr = math.pi ** 2 * E * (1e4 / 12) / (4 * 200.0 ** 2)
    rows.append(_row("B1 Euler column (fixed-free)", "P_cr [N]", rs.load_factors[0] * 1000.0, pcr, 1.0,
                     "pi^2 E I / (4 L^2)", "N"))
    log("  dynamics / stability benchmarks done")
    return rows


def statics(log) -> list[dict]:
    rows = []
    L, a, M = 200.0, 20.0, 1e5
    s, b, f = _bar(L=L, a=a, h=5.0, analysis="linear_static")
    s.project.constraints = [Constraint("fixed", [f["x0"]], name="root")]
    s.project.loads = [Load("moment", [f["xL"]], {"magnitude": M, "direction": [0, 1, 0]}, name="M")]
    s.generate_mesh()
    rs = _solve(s)
    x = s.mesh.nodes[:, 0]
    rows.append(_row("S1 end moment", "tip deflection [mm]", abs(rs.displacement[x > L - 1e-6, 2].mean()),
                     M * L ** 2 / (2 * E * a ** 4 / 12), 2.0, "M L^2 / (2 E I)", "mm"))
    L, a = 500.0, 10.0
    P = E * (a ** 4 / 12) / L ** 2
    s, b, f = _bar(L=L, a=a, h=5.0, analysis="linear_static")
    s.project.mesh_settings.element_shape, s.project.mesh_settings.element_order = "hex", 2
    s.project.analysis.nonlinear_geometry = True
    s.project.constraints = [Constraint("fixed", [f["x0"]], name="root")]
    s.project.loads = [Load("force", [f["xL"]], {"magnitude": P, "direction": [0, 0, -1]}, name="P")]
    s.generate_mesh()
    rs = _solve(s)
    tip = rs.displacement[s.mesh.nodes[:, 0] > L - 1e-6].mean(axis=0)
    rows.append(_row("S2 large deflection (NLGEOM)", "vertical tip deflection / L", -tip[2] / L, 0.3017, 1.0,
                     "elastica, P L^2 / EI = 1 (Bisshopp & Drucker)"))
    rows.append(_row("S2 large deflection (NLGEOM)", "horizontal shortening / L", -tip[0] / L, 0.0566, 3.0,
                     "elastica"))
    s, b, f = _bar(analysis="thermal_stress")
    s.project.analysis.reference_temperature = 20.0
    s.project.constraints = [Constraint("symmetry", [f[k]], name=k) for k in ("x0", "y0", "z0", "xL")]
    s.project.loads = [Load("uniform_temperature", [], {"value": 120.0}, name="T")]
    s.generate_mesh()
    rs = _solve(s)
    rows.append(_row("S3 restrained thermal expansion", "sigma_xx [MPa]", rs.stress[:, 0].mean(),
                     -E * ALPHA * 100.0, 0.1, "-E alpha dT", "MPa"))
    log("  statics benchmarks done")
    return rows


# ----------------------------------------------------------------------------- flow
def _flow_box(dx, dy, L, h, speed=0.01, mu=1e-3, rho=1000.0, shape="hex"):
    s = Session()
    s.new("flow")
    s.add_feature(Feature("primitive", {"shape": "box", "dx": dx, "dy": dy, "dz": L}))
    b = s.analysis_body()
    a = s.project.analysis
    a.analysis_type = "cfd_incompressible"
    a.fluid_density, a.fluid_viscosity, a.cfd_cfl = rho, mu, 400.0
    ms = s.project.mesh_settings
    ms.element_shape, ms.element_order, ms.size_max = shape, 1, h
    f = lambda n, p: make_ref(b, "face", find_planar_face(b, n, p))  # noqa: E731
    s.project.loads = [Load("velocity_inlet", [f([0, 0, -1], [0, 0, 0])], {"magnitude": speed, "direction": "normal"},
                            name="Inlet"),
                       Load("pressure_outlet", [f([0, 0, 1], [0, 0, L])], {"magnitude": 0.0}, name="Outlet")]
    return s, b, f


def _section_mean(mesh, w, z, ext, n=101):
    gx, gy = np.linspace(0, ext[0], n), np.linspace(0, ext[1], n)
    X, Y = np.meshgrid(gx, gy, indexing="ij")
    ids, wt, _ = locate(mesh, np.c_[X.ravel(), Y.ravel(), np.full(X.size, z)])
    u = np.einsum("pk,pk->p", wt, w[ids]).reshape(n, n)
    return float(np.trapezoid(np.trapezoid(u, gy, axis=1), gx) / (ext[0] * ext[1]))


def flow(log, quick: bool) -> list[dict]:
    rows = []
    mu = 1e-3
    # 2-D channel on quads
    for n in ((16,) if quick else (8, 16, 32)):
        H, L = 10.0, 200.0
        s = Session()
        s.new("ch2d")
        s.add_feature(Feature("primitive", {"shape": "box", "dx": L, "dy": H, "dz": 1.0}))
        b = s.analysis_body()
        a = s.project.analysis
        a.analysis_type, a.cfd_dimension = "cfd_incompressible", "2d"
        a.fluid_density, a.fluid_viscosity, a.cfd_cfl = 1000.0, mu, 400.0
        a.cfd_face = f"{b.id}|face:{find_planar_face(b, [0, 0, 1], [0, 0, 1.0])}"
        ms = s.project.mesh_settings
        ms.element_order, ms.element_shape, ms.size_max = 1, "hex", H / n
        e_in = next(e.index for e in b.edges if abs(e.centroid[2] - 1) < 1e-9 and abs(e.centroid[0]) < 1e-9)
        e_out = next(e.index for e in b.edges if abs(e.centroid[2] - 1) < 1e-9 and abs(e.centroid[0] - L) < 1e-9)
        s.project.loads = [Load("velocity_inlet", [make_ref(b, "edge", e_in)], {"magnitude": 0.01,
                                                                                "direction": "normal"}, name="Inlet"),
                           Load("pressure_outlet", [make_ref(b, "edge", e_out)], {"magnitude": 0.0}, name="Outlet")]
        s.generate_mesh()
        rs = _solve(s)
        sm = s.project.results.summary
        Um = sm["markers"]["Outlet"]["flow_m3s"] / (H * 1e-3)
        P = s.mesh.nodes * 1e-3
        sel = (P[:, 0] > 0.1) & (P[:, 0] < 0.19)
        dpdx = np.polyfit(P[sel, 0], rs.pressure[sel], 1)[0]
        rows.append(_row(f"F1 2-D channel, {n} quads across", "dp/dx [Pa/m]", dpdx, -12 * mu * Um / (H * 1e-3) ** 2,
                         2.0 if n <= 8 else 1.0, "plane Poiseuille, Re = 100", "Pa/m"))
    # square duct (3-D, hex)
    for h in ((1.0,) if quick else (1.0, 0.5)):
        a_ = 10.0
        s, b, f = _flow_box(a_, a_, 150.0, h, 0.005)
        s.generate_mesh()
        rs = _solve(s)
        P = s.mesh.nodes * 1e-3
        Um = _section_mean(s.mesh, rs.velocity[:, 2], 100.0, (a_, a_))
        c = (P[:, 2] > 0.07) & (P[:, 2] < 0.14) & (np.abs(P[:, 0] - 5e-3) + np.abs(P[:, 1] - 5e-3) < 6e-4)
        dpdz = np.polyfit(P[c, 2], rs.pressure[c], 1)[0]
        rows.append(_row(f"F2 square duct, {int(a_ / h)} hexes across", "dp/dz [Pa/m]", dpdz,
                         -28.438 * mu * Um / 1e-4, 4.0, "f Re = 56.91 (Shah & London)", "Pa/m"))
    # circular pipe (3-D, unstructured tets)
    if not quick:
        D, L = 10.0, 150.0
        s = Session()
        s.new("pipe")
        s.add_feature(Feature("primitive", {"shape": "cylinder", "radius": D / 2, "height": L}))
        b = s.analysis_body()
        a = s.project.analysis
        a.analysis_type = "cfd_incompressible"
        a.fluid_density, a.fluid_viscosity, a.cfd_cfl = 1000.0, mu, 400.0
        ms = s.project.mesh_settings
        ms.element_order, ms.size_max = 1, 0.5
        s.project.loads = [Load("velocity_inlet", [make_ref(b, "face", find_planar_face(b, [0, 0, -1], [0, 0, 0]))],
                                {"magnitude": 0.005, "direction": "normal"}, name="Inlet"),
                           Load("pressure_outlet", [make_ref(b, "face", find_planar_face(b, [0, 0, 1], [0, 0, L]))],
                                {"magnitude": 0.0}, name="Outlet")]
        t0 = time.time()
        s.generate_mesh()
        rs = _solve(s)
        sm = s.project.results.summary
        Um = sm["markers"]["Outlet"]["flow_m3s"] / (math.pi * (D * 5e-4) ** 2)
        P = s.mesh.nodes * 1e-3
        r = np.hypot(P[:, 0], P[:, 1])
        m = (P[:, 2] > 0.06) & (P[:, 2] < 0.14) & (r < 0.4 * D * 5e-4)
        dpdz = np.polyfit(P[m, 2], rs.pressure[m], 1)[0]
        rows.append(_row("F3 circular pipe (tets, D/h = 20)", "dp/dz [Pa/m]", dpdz, -32 * mu * Um / (D * 1e-3) ** 2,
                         4.0, f"Hagen-Poiseuille, Re = 50; {s.mesh.n_elements} cells, {time.time() - t0:.0f} s", "Pa/m"))
        c = (P[:, 2] > 0.09) & (P[:, 2] < 0.12) & (r < 0.15 * D * 5e-4)
        ue = 2 * Um * (1 - (r[c] / (D * 5e-4)) ** 2)
        rows.append(_row("F3 circular pipe (tets, D/h = 20)", "centre-line velocity [m/s]",
                         rs.velocity[c, 2].mean(), ue.mean(), 3.0, "u_max = 2 U_mean", "m/s"))
    # external flow: sphere drag
    s = Session()
    s.new("sphere")
    s.add_feature(Feature("primitive", {"shape": "sphere", "radius": 5.0}))
    sph = s.analysis_body()
    st = s.create_flow_domain([sph.id], "+x", 0.02, 4.0, 4.0, 4.0)
    a = st.analysis
    a.fluid_density, a.fluid_viscosity, a.cfd_cfl, a.ref_area = 1000.0, 0.01, 400.0, math.pi * 0.01 ** 2 / 4
    dom = s.bodies[st.body_ids[0]]
    st.mesh_settings.size_max, st.mesh_settings.growth_rate = 6.0, 1.3
    st.mesh_settings.refinements = [MeshRefinement(targets=[make_ref(dom, "face", fc.index) for fc in dom.faces
                                                            if fc.surface_type == "sphere"], size=0.6, name="sphere")]
    s.generate_mesh()
    _solve(s)
    cd = s.project.results.summary["force_coefficients"][0]
    Re = 20.0
    rows.append(_row("F4 sphere drag, Re = 20", "Cd", cd, 24 / Re * (1 + 0.15 * Re ** 0.687), 8.0,
                     "Schiller-Naumann correlation (±5 % vs experiments); side walls at 4 D"))
    log("  flow benchmarks done")
    return rows


def run_all(log: Callable[[str], None], quick: bool = False, with_flow: bool = True) -> list[dict]:
    rows = thermal(log) + dynamics(log) + statics(log)
    if with_flow:
        rows += flow(log, quick)
    return rows
