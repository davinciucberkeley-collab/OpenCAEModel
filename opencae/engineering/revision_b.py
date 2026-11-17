"""K09-L revision B: the design changes that clear the failing and unevaluated requirements of revision A.

Every change is applied through the engine (so the dependency graph marks what it affects) and is listed
with the requirement it addresses, why, and the evidence class of the numbers behind it. Nothing here
creates evidence: requirements that rest on estimates stay CONDITIONAL until the physical tests in the
test matrix are done, and gates G5/G6 stay open until measurements are imported and a person decides.

Changes (see CHANGES):
  B1  Foot B (K09L-AF-FOOT-B): outrigger feet, parametric stretch of foot A        -> R6 tip-over
  B2  Motor sets 6 mm inboard: motor tube cut to 368 mm, fin notch 6 mm deeper     -> R5 transport box
  B3  Flight-controller motor output limit 0.66                                    -> R7 motor current
  B4  Shipping entered as a labelled provisional estimate                          -> R3 cost / reserve
  B5  Transition procedure: pitch over to 80 deg at 60 deg/s, wing-borne at 16 m/s -> G3 transition
  B6  Motor mounts printed in ASA instead of PETG (motor-base heat)                  -> G3 thermal
Operating limits that follow from the simulations are in OPERATING_LIMITS (flight card).
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path

from opencae.engineering.graph import Engine
from opencae.engineering.quantities import ESTIMATE, USER

DATA = Path(__file__).resolve().parent.parent / "examples" / "k09l"
STATE_FILE = DATA / "k09l_revB_state.json"

THROTTLE_MAX = 0.66
TRANSITION = {"transition_pitch_end_deg": 80.0, "transition_pitch_rate_deg": 60.0, "transition_airspeed": 16.0}
SHIPPING_PER_ORDER = 14.0  # USD, provisional

CHANGES = [
    {"id": "B1", "requirement": "R6", "title": "Outrigger feet (K09L-AF-FOOT-B)",
     "what": "Foot A stretched parametrically beyond its fin slot: outboard edge |z| 198 -> 220 mm (a solid 30 mm "
             "outrigger beyond the fin tip), aft wall 6 mm thinner (ground plane x 320 -> 314 mm), side walls 9 -> 5 "
             "mm with the clamp screw M3x30 -> M3x25. The slot and its fit on the fin are unchanged.",
     "why": "Revision A tips over at 34.7 deg, below the 36 deg lean the wind study gives for the 8 m/s flight-"
            "card gust. The governing support-polygon edge is the feet's outer edge; widening it and lowering "
            "the CG above the ground plane raises the tip-over angle. Thinner walls keep the feet mass-neutral, "
            "because mass at the feet (240 mm aft of the CG) costs static margin.",
     "evidence": "Geometry: CAD of foot A, modified parametrically (no new CAD exists yet). Mass: CAD mass x "
                 "volume ratio (ESTIMATE). Root bending check uses an estimated PETG strength."},
    {"id": "B2", "requirement": "R5", "title": "Motor sets 6 mm inboard (motor tube 368 mm, K09L-AF-FIN-B)",
     "what": "The CF motor tube is cut 368 mm long instead of 380 mm (same 24 in stock) and each motor set (elbow "
             "unchanged, motor, prop hardware, mount screws) sits 6 mm further inboard: motor axis |z| 206 -> 200 mm. "
             "The fin's leading-edge notch that receives the elbow is cut 6 mm deeper so the elbow keeps its "
             "clearance.",
     "why": "Revision A is 454 mm high (motor mounts at |z| 227 mm), 4 mm over the 450 mm side of the transport "
            "box. Revision B is 442 mm, an 8 mm margin. The elbow sits flush on the fin tip, so the fin notch has to "
            "follow; the assembly check confirms no new interference.",
     "evidence": "Geometry: rigid translation of the CAD bodies and the prop joints, shortened tube and deeper "
                 "notch (ANALYTICAL). Tube and fin masses scale with their volume (ESTIMATE)."},
    {"id": "B3", "requirement": "R7", "title": "Motor output limit 0.66 in the flight controller",
     "what": f"Full stick commands {THROTTLE_MAX:.0%} of the battery voltage to the motors (e.g. ArduPilot "
             "Q_M_SPIN_MAX / a motor output limit; set and verify on the thrust stand).",
     "why": "At full throttle on a full pack the X3108S would draw about 38.5 A against its 25 A (30 s) rating. "
            "The aircraft has thrust to spare (T/W 4.3), so a limit keeps the CAD-verified motor, adds no mass "
            "and keeps T/W >= 1.6 at the end of the battery. 0.66 is the highest limit that keeps a margin "
            "(1.07) on the rating; above 0.70 the full-throttle current exceeds it. The alternative, the X2814 V3 "
            "(50 A), has no CAD or published bolt pattern, adds 108 g, and its transition failed in the model.",
     "evidence": "Operating points use the supplier motor data and the ESTIMATED prop coefficients: "
                 "CONDITIONAL until the thrust-stand test (T-PROP-01) measures the current at the limit."},
    {"id": "B5", "requirement": "G3", "title": "Transition procedure: 80 deg pitch-over at 60 deg/s, 16 m/s",
     "what": "Forward transition pitches the nose over to 80 deg from vertical at 60 deg/s and declares wing-borne "
             "flight at 16 m/s (was 75 deg, 45 deg/s, 14 m/s); the back-transition enters from 16 m/s.",
     "why": "With the motor output limited (B3) the upper motors saturate during a 75 deg pitch-over and the "
            "aircraft loses about 23 m. Pitching further over puts the wing to work sooner; 16 m/s keeps the wing "
            "away from the stall. Swept in the 6-DOF model: 70 deg loses 22-25 m at any rate, 80 deg holds "
            "altitude, and 12 m/s loses about 24 m even without any output limit.",
     "evidence": "REDUCED-ORDER simulation on estimated aerodynamic and prop coefficients; verify in a high-"
                 "altitude transition test before relying on it."},
    {"id": "B6", "requirement": "G3", "title": "Motor mounts printed in ASA instead of PETG",
     "what": "The four motor-mount elbows are printed in ASA (glass transition about 100-105 °C) instead of PETG "
             "(about 80 °C). Same geometry.",
     "why": "The motor-mount thermal model (CalculiX, hover on a 35 °C day) puts the face against the motor base "
            "at about 67 °C, above the 65 °C limit set for PETG. ASA keeps about 20 K of margin for the same "
            "heat. It needs an enclosed printer, which the team's makerspace has.",
     "evidence": "Thermal simulation on estimated inputs (motor thermal resistance, convection, material "
                 "limits); the bench test T-E1 measures the motor temperature that drives it."},
    {"id": "B4", "requirement": "R3", "title": "Shipping as a labelled provisional estimate",
     "what": "Shipping = number of separate orders x $14 (ESTIMATE, +/- 50 %).",
     "why": "Revision A has no shipping figure, so the reserve cannot be evaluated (a missing cost is never "
            "counted as zero). The reserve stays above $309 even at +50 % on this estimate.",
     "evidence": "ESTIMATE: replace with the real order totals."},
]


OPERATING_LIMITS = [
    "Forward transition in calm air (gusts up to 2 m/s) only with at least 30 % battery charge: calm-air "
    "transitions lose 0.7-1.8 m at 30-60 % charge and end in ground contact at 20 % in the model.",
    "Forward transition in side or head gusts up to 4 m/s only with at least 90 % charge (fresh pack): a 4 m/s side "
    "gust ends in ground contact at 60-80 % charge and loses 0.5 m at 90 %. This is the price of the 0.66 output "
    "limit; a thrust-stand measurement of the real prop may allow a higher limit.",
    "No forward transition in tailwind gusts above 2 m/s: a 4 m/s tailwind gust during the pitch-over ends in "
    "ground contact in the model (revision A already fails at 2 m/s).",
    "Transition at a safe height (30 m in the simulations) until test flights confirm the model.",
]

# transition robustness cases run for the revision-B review (6-DOF model, see transition_robustness)
_G = lambda d, a=4.0: dict(amplitude=a, duration=1.5, start=3.0, direction=d)  # noqa: E731
ROBUSTNESS_CASES = {
    "nominal": {}, "battery 20 % charge": dict(soc0=0.2), "battery 50 % + side gust 4 m/s": dict(soc0=0.5, gust=_G((0, 1, 0))),
    "side gust 4 m/s": dict(gust=_G((0, 1, 0))), "head gust 4 m/s": dict(gust=_G((-1, 0, 0))),
    "tailwind gust 2 m/s": dict(gust=_G((1, 0, 0), 2.0)), "tailwind gust 4 m/s": dict(gust=_G((1, 0, 0))),
    "vertical gust 2 m/s": dict(gust=_G((0, 0, 1), 2.0)), "payload 0.3 kg": dict(payload=0.3),
    "steady headwind 4 m/s": dict(wind=(-4.0, 0, 0)), "motor M2 at 85 %": dict(motor_failure=dict(motor=2, factor=0.85, time=3.0)),
}


def _robust_case(args):
    rev, name = args
    import numpy as np
    from opencae.engineering import flightsim as FS
    e = Engine()
    if rev == "B":
        apply(e)
    s = FS.simulate(e.model, "transition_forward", dt=0.004, **ROBUSTNESS_CASES[name])
    o = s.meta.get("outcome", {})
    alt0 = float(s.pos[0, 2])
    im = np.asarray(s.motor_current)
    dt = np.diff(s.t, prepend=s.t[0])
    imax = e.model.option("motor").q("imax").value
    return {"revision": rev, "case": name, "converged": bool(o.get("transition_converged")),
            "altitude_loss": round(alt0 - float(s.pos[:, 2].min()), 2),
            "ground_contact": bool(float(s.pos[:, 2].min()) < 0.5),
            "time_above_motor_rating_s": round(float(((im > imax).any(1) * dt).sum()), 2),
            "saturation_warnings": sum("saturated" in w for _, w in s.warnings)}


def transition_robustness(workers: int = 4) -> list[dict]:
    """The forward transition of revisions A and B under the off-nominal cases (reduced-order evidence)."""
    from concurrent.futures import ProcessPoolExecutor
    jobs = [(r, n) for r in ("A", "B") for n in ROBUSTNESS_CASES]
    with ProcessPoolExecutor(workers) as ex:
        return list(ex.map(_robust_case, jobs))


def order_count(model) -> int:
    """Separate orders for the purchased parts: distinct BOM vendors (first name of a 'A / B' choice),
    counting a part supplied with another part ('included with', 'supplied', 'part of') as no order and
    all the lines without a vendor as one further order."""
    vend = Counter()
    for c in model.active_components():
        if not c.purchased:
            continue
        v = (c.vendor or "").strip()
        lv = v.lower()
        if lv.startswith(("included with", "part of")) or "(supplied" in lv:
            continue
        vend[v.split("(")[0].split("/")[0].strip().lower() or "(no vendor in the BOM)"] += 1
    return len(vend)  # the lines without a vendor count as one consolidated order


def apply(engine: Engine) -> list[dict]:
    """Apply revision B to the engine's configuration; returns the change events."""
    m = engine.model
    evs = [engine.set_option("landing", "feet_b"), engine.set_option("mount", "inboard_b"),
           engine.set_option("mount_material", "asa"),
           engine.set_param("throttle_max", THROTTLE_MAX, source=USER,
                            ref="Revision B: flight-controller motor output limit (verify on the thrust stand)")]
    for k, v in TRANSITION.items():
        evs.append(engine.set_param(k, v, source=USER, ref="Revision B transition procedure (change B5)"))
    n = order_count(m)
    evs.append(engine.set_param("shipping", n * SHIPPING_PER_ORDER, source=ESTIMATE,
                                ref=f"Revision B: {n} separate orders x ${SHIPPING_PER_ORDER:.0f} (provisional, "
                                    f"+/- 50 %; replace with real order totals)"))
    m.params["shipping"].uncertainty = 0.5
    engine.recompute()
    return evs


def run_analyses(engine: Engine, workdir: Path) -> dict:
    """Run every heavy analysis of the gates on the current configuration (solver failures are recorded and
    re-raised by the services, never hidden)."""
    from opencae.engineering import services as S
    out = {}
    workdir.mkdir(parents=True, exist_ok=True)
    out["fea_motor_tube"] = S.run_fea(engine, S.MOTOR_TUBE, workdir=workdir / "fea_motor_tube")
    out["fea_spar"] = S.run_fea(engine, S.SPAR_TUBE, workdir=workdir / "fea_spar")
    out["assembly_check"] = S.run_assembly_check(engine)
    out["flight_hover"] = S.run_flight(engine, "hover")
    out["flight_transition"] = S.run_flight(engine, "transition_forward")
    return out
