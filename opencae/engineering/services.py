"""Heavy analyses as services: each runs one solver/simulation on the current configuration and records
the result in the engine with its configuration and geometry hashes. They are called from worker
threads by the GUI and directly by the acceptance tests; none of them is run implicitly."""
from __future__ import annotations

import tempfile
import time
from pathlib import Path
from typing import Optional

from opencae.engineering.graph import Engine
from opencae.engineering.quantities import Q

MOTOR_TUBE = "Motor tube CF 16x13.08x380 — DragonPlate FDPT.625*.515*24"
SPAR_TUBE = "Spar tube CF 12x10x560 — Superstition CFT3K12101"


def run_fea(engine: Engine, component: str = MOTOR_TUBE, case: Optional[str] = None,
            workdir: Optional[Path] = None) -> dict:
    """Structural analysis of one component in its load-path context (CalculiX)."""
    from opencae.engineering import component_analysis as CA
    node = "fea_motor_tube" if "Motor tube" in component else "fea_spar"
    wd = Path(workdir or tempfile.mkdtemp(prefix="k09l_fea_"))
    t = time.time()
    try:
        res = CA.run_component_analysis(engine.model, component, wd, case=case)
    except Exception as exc:  # recorded, never hidden
        engine.record_heavy(node, {"error": f"{type(exc).__name__}: {exc}", "workdir": str(wd)}, ok=False)
        raise
    rec = {"component": component, "case": res.get("case"), "result": res, "workdir": str(wd),
           "seconds": round(time.time() - t, 1)}
    sf = res.get("safety_factor")
    if sf:
        rec["sf_q"] = Q.from_dict(sf)
    engine.record_heavy(node, rec, ok=bool(res.get("converged", True)))
    return rec


def run_assembly_check(engine: Engine) -> dict:
    from opencae.engineering import assembly as AS
    t = time.time()
    res = AS.check_assembly(engine.model)
    rec = {**res, "seconds": round(time.time() - t, 1)}
    engine.record_heavy("assembly_check", rec)
    return rec


def run_flight(engine: Engine, scenario: str = "hover", duration: Optional[float] = None, **overrides):
    from opencae.engineering import flightsim as FS
    sim = FS.simulate(engine.model, scenario, duration=duration, **overrides)
    node = "flight_transition" if scenario.startswith("transition") else "flight_hover"
    alt0 = float(sim.pos[0, 2]) if len(sim.t) else None
    engine.record_heavy(node, {"scenario": scenario, "outcome": sim.meta.get("outcome"), "altitude_start": alt0,
                               "min_altitude": float(sim.pos[:, 2].min()) if len(sim.t) else None,
                               "params": {k: v for k, v in (sim.meta.get("parameters") or {}).items()
                                          if isinstance(v, (int, float, str, bool))},
                               "warnings": sim.warnings[:50], "duration": float(sim.t[-1]) if len(sim.t) else 0.0,
                               "max_current": float(sim.current.max()) if len(sim.current) else None})
    return sim


# ------------------------------------------------------------------------------------------- field analyses
FIELD_RUNS = {"fea_foot": "run_foot_fea", "thermal_mount": "run_mount_thermal", "cfd_cruise": "run_cfd_cruise"}


def run_field(engine, nid: str, workdir, fields_dir) -> dict:
    """Run one field analysis (foot FEA, mount thermal, cruise CFD), keep its contour arrays in fields_dir and
    record the summary as the heavy result of node ``nid``. A solver failure is recorded as FAILED and
    re-raised, never hidden."""
    from pathlib import Path

    from opencae.engineering import field_analyses as FA
    t0 = time.time()
    try:
        rec = getattr(FA, FIELD_RUNS[nid])(engine, Path(workdir))
    except Exception as exc:
        engine.record_heavy(nid, {"error": f"{type(exc).__name__}: {exc}", "seconds": time.time() - t0}, ok=False)
        raise
    summ = FA.save_field(rec, fields_dir)
    summ["seconds"] = round(time.time() - t0, 1)
    engine.record_heavy(nid, summ, ok=True)
    return summ


def run_component_screening(engine, workdir, fields_dir, progress=None) -> dict:
    """Screen every component of the current configuration (component_screening.run_component_screening): FE
    runs of the made / load-carrying parts (contour fields kept in fields_dir), rating, ampacity, fastener and
    mass checks of the rest. Recorded as the heavy result 'component_screening'; an exception is recorded as
    FAILED and re-raised, never hidden (single-part solver failures are listed inside the record)."""
    from pathlib import Path

    from opencae.engineering import component_screening as CS
    t0 = time.time()
    try:
        rec = CS.run_component_screening(engine, Path(workdir), Path(fields_dir), progress=progress)
    except Exception as exc:
        engine.record_heavy("component_screening", {"error": f"{type(exc).__name__}: {exc}",
                                                    "seconds": round(time.time() - t0, 1)}, ok=False)
        raise
    rec["workdir"] = str(workdir)
    engine.record_heavy("component_screening", rec, ok=True)
    return rec


def attach_fea_field(engine, nid: str, fields_dir) -> Optional[dict]:
    """Add the surface stress field of a finished component FEA (motor tube / spar) to its current record
    (and the matching history entry), so the website can show its contours."""
    from opencae.engineering import field_analyses as FA
    rec = FA.fea_from_component_run(engine, nid)
    if rec is None:
        return None
    summ = FA.save_field(rec, fields_dir)
    cur = engine.heavy[nid]["record"]
    cur["field"] = summ
    for h in reversed(engine.history):
        if h.get("node") == nid and h.get("node_hash") == cur.get("node_hash"):
            h["field"] = summ
            break
    return summ
