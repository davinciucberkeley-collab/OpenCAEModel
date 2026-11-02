"""Analysis-type-aware result summaries (GUI summary panel, console, reports)."""
from __future__ import annotations

import numpy as np

from opencae.postprocessing.results import equilibrium_check

UNIT_LABELS = {"temperature": "°C", "heat_flux": "W/m²", "temperature_gradient": "K/m",
               "mode_shape": "relative (mass-normalised)", "velocity": "m/s", "pressure_si": "Pa",
               "viscosity": "Pa·s"}


def unit_for(kind: str, us) -> str:
    """Display unit for a result quantity's unit kind."""
    return {"displacement": us.length, "stress": us.stress, "strain": "-"}.get(kind, UNIT_LABELS.get(kind, ""))


def summary_rows(session) -> tuple[list[tuple[str, str, str]], list[tuple[str, bool, str]]]:
    """(rows, checks). rows: (quantity, value, where); checks: (name, passed, detail)."""
    rs = session.results
    us = session.units
    if rs is None:
        return [], []
    sm = rs.summary()
    stored = session.project.results.summary or {}
    kind = getattr(rs, "kind", "structural")
    rows: list[tuple[str, str, str]] = []
    checks: list[tuple[str, bool, str]] = []
    if kind == "thermal":
        rows.append(("Max temperature", f"{sm['max_temperature']:.6g} °C", f"node {sm['max_temperature_node']}"))
        rows.append(("Min temperature", f"{sm['min_temperature']:.6g} °C", f"node {sm['min_temperature_node']}"))
        if "max_heat_flux_W_m2" in sm:
            rows.append(("Max heat flux (nodal avg.)", f"{sm['max_heat_flux_W_m2']:.6g} W/m²", ""))
        if sm.get("frames", 1) > 1:
            rows.append(("Stored time frames", str(sm["frames"]), f"end time {sm['final_time']:.6g} s"))
        if "heat_input_W" in stored:
            rows.append(("Prescribed heat input", f"{stored['heat_input_W']:.6g} W", "flux + power + generation"))
        fh = sm.get("heat_at_fixed_temperatures_W", {})
        if "T_ALL_FIXED" in fh:
            rows.append(("Heat at fixed-temperature faces", f"{fh['T_ALL_FIXED']:.6g} W", "negative = leaves the model"))
        from opencae.physics.thermal import heat_balance
        hb = heat_balance({**sm, **stored})
        if hb:
            checks.append(("Energy balance", hb["ok"], f"residual {hb['residual_W']:.3g} W "
                                                       f"(relative {hb['relative']:.1e})"))
        return rows, checks
    if kind == "cfd":
        rows.append(("Max velocity", f"{sm['max_velocity']:.6g} m/s", f"node {sm['max_velocity_node']}"))
        rows.append(("Pressure range (gauge)", f"{sm['min_pressure']:.6g} … {sm['max_pressure']:.6g} Pa", ""))
        for name, m in sm.get("markers", {}).items():
            what = "inflow" if m["flow_m3s"] < 0 else "outflow"
            rows.append((f"{m['source']} ({m['kind'].replace('_', ' ')})",
                         f"{abs(m['flow_m3s']):.6g} m³/s {what}, {abs(m['mass_flow_kgs']):.6g} kg/s",
                         f"mean p {m['mean_pressure_Pa']:.6g} Pa over {m['area_m2']:.4g} m²"))
        if "nominal_inflow_m3s" in sm and sm.get("inflow_m3s"):
            rows.append(("Delivered / nominal inflow", f"{sm['inflow_m3s'] / sm['nominal_inflow_m3s']:.4f}",
                         "below 1 when inlet nodes on walls are at rest (refine the mesh at the inlet edges)"))
        if "pressure_drop_Pa" in sm:
            rows.append(("Pressure drop inlet → outlet", f"{sm['pressure_drop_Pa']:.6g} Pa", "area-averaged"))
        if "wall_force_N" in sm:
            unit = " N/m (per metre of depth, 2-D)" if sm.get("force_per_unit_depth") else " N"
            rows.append(("Force of the fluid on the walls", " / ".join(f"{f:.5g}" for f in sm["wall_force_N"]) + unit,
                         "Fx / Fy / Fz, pressure + viscous (SU2 surface integration)"))
        if "force_coefficients" in sm:
            rows.append(("Force coefficients", " / ".join(f"{c:.5g}" for c in sm["force_coefficients"]),
                         f"F / (q A_ref), q = {sm['dynamic_pressure_Pa']:.4g} Pa"))
        if "iterations" in sm:
            rows.append(("Iterations", str(sm["iterations"]),
                         f"residual drop {sm['residual_drop_pressure']:.2f} (log10, pressure)"
                         if sm.get("residual_drop_pressure") is not None else ""))
        checks.append(("Solver convergence", bool(sm.get("converged")),
                       "residuals reached the requested drop" if sm.get("converged") else
                       "iteration limit reached before convergence - results are not reliable"))
        if sm.get("mass_imbalance") is not None:
            checks.append(("Inlet/outlet flow-rate balance", sm["mass_imbalance"] < 0.02,
                           f"imbalance {100 * sm['mass_imbalance']:.3f} % of the inflow (face integration of nodal "
                           "velocities; small deviations on coarse meshes are integration error)"))
        return rows, checks
    if kind == "buckling":
        for i, f in enumerate(sm["load_factors"]):
            rows.append((f"Buckling mode {i + 1}", f"load factor {f:.6g}",
                         "critical load = factor × applied loads" if i == 0 else ""))
        if sm.get("critical_load_factor") is not None:
            rows.append(("Lowest positive load factor", f"{sm['critical_load_factor']:.6g}",
                         "linear prediction; real structures with imperfections buckle earlier"))
        return rows, checks
    if kind == "modal":
        fr = sm["frequencies_hz"]
        mf = np.asarray(sm.get("effective_mass_fraction", []))
        for i, f in enumerate(fr):
            where = ""
            if mf.size:
                d = int(np.argmax(mf[i, :3]))
                where = f"dominant {'XYZ'[d]} translation, {100 * mf[i, d]:.1f} % of mass"
            rows.append((f"Mode {i + 1}", f"{f:.6g} Hz", where))
        if "cumulative_mass_fraction" in sm:
            c = sm["cumulative_mass_fraction"]
            rows.append(("Cumulative effective mass X / Y / Z",
                         " / ".join(f"{100 * v:.1f} %" for v in c), "aim for ≥ 80–90 % in excited directions"))
        return rows, checks
    # structural (static and thermal stress)
    rows.append(("Max displacement", f"{sm['max_displacement']:.6g} {us.length}", f"node {sm['max_displacement_node']}"))
    if "max_von_mises_nodal" in sm:
        rows.append(("Max von Mises (nodal avg.)", f"{sm['max_von_mises_nodal']:.6g} {us.stress}",
                     f"node {sm['max_von_mises_node']}"))
    if "max_von_mises_integration_point" in sm:
        rows.append(("Max von Mises (integration pts)", f"{sm['max_von_mises_integration_point']:.6g} {us.stress}",
                     "raw solver output"))
    if "max_principal" in sm:
        rows.append(("Principal stress max / min", f"{sm['max_principal']:.6g} / {sm['min_principal']:.6g} {us.stress}",
                     ""))
    if "max_contact_pressure" in sm:
        rows.append(("Max contact pressure", f"{sm['max_contact_pressure']:.6g} {us.stress}",
                     f"{sm['contact_nodes_closed']} slave nodes in contact; max penetration "
                     f"{sm['max_penetration']:.3g} {us.length} (penalty)"))
    if "temperature_min" in stored:
        rows.append(("Applied temperature range", f"{stored['temperature_min']:.6g} … {stored['temperature_max']:.6g} °C",
                     f"stress-free at {stored['reference_temperature']:.6g} °C"))
    e = equilibrium_check({**sm, **stored})
    if e:
        checks.append(("Global force equilibrium", e["ok"],
                       f"reactions {np.round(e['reactions'], 6).tolist()} {us.force}, applied "
                       f"{np.round(e['applied'], 6).tolist()} {us.force}, rel. residual {e['relative']:.1e}"))
    body = session.analysis_body()
    mat = session.project.material_for_body(body.id) if body else None
    if mat and mat.yield_strength and sm.get("max_von_mises_nodal"):
        rows.append(("Yield safety factor (nodal max)",
                     f"{mat.yield_strength * us.stress_from_si / sm['max_von_mises_nodal']:.3g}", mat.name))
    return rows, checks


def headline(session) -> str:
    """One-line description of a fresh result (console / status bar)."""
    rows, checks = summary_rows(session)
    if not rows:
        return "No results."
    txt = ", ".join(f"{a} {b}" for a, b, _ in rows[:2])
    bad = [c for c in checks if not c[1]]
    return txt + (f" — CHECK FAILED: {bad[0][0]}" if bad else "")
