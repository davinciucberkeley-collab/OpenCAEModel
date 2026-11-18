"""K09-L revision C ("recommended"): revision B plus the changes that the analyses of revision B call for.

The strongest finding on revision B is the cruise CFD: at the flight model's cruise trim (12.5 m/s, alpha 8.1 deg)
the wing makes C_L 0.349 against the 0.590 the flight model needs (59 %), i.e. a lift slope of about 2.10/rad
against the 3.81/rad of the flight model's lifting-line slope. Revision C takes the CFD value as its design basis
and re-plans what depends on it. Every change goes through the engine (so the dependency graph marks what it
affects) and is listed with the requirement / analysis it addresses, why (with numbers from the engine) and the
evidence class behind it. Nothing here creates evidence: a provisional input stays provisional, requirements
verified by test stay CONDITIONAL until the measurements are imported (manufacturing.write_measurement_template /
requirement_measurements), and no gate is approved by this module.

Changes (see CHANGES):
  C1  Aerodynamic design basis from the cruise CFD: lift slope 2.10/rad, C_L,max 0.49 -> cruise 16.4 m/s  -> R4, CFD
  C2  Transition procedure: pitch over to 80 deg at 45 deg/s, wing-borne at 17 m/s                     -> G3 transition
The motor output limit stays at revision B's 0.66 (a lower limit was evaluated for the thin R7 margin and rejected,
see NOT_APPLIED N8). Recommendations the model cannot represent, or that do not pay off, are in NOT_APPLIED; the
flight-card limits that follow from the transition simulations of revision C are in OPERATING_LIMITS.
"""
from __future__ import annotations

import copy
import json
import math
from pathlib import Path
from typing import Optional

from opencae.engineering import approval as AP
from opencae.engineering import calcs
from opencae.engineering import catalog as C
from opencae.engineering import revision_b as RB
from opencae.engineering.flightsim import A_STALL
from opencae.engineering.graph import Engine
from opencae.engineering.quantities import ESTIMATE, USER

DATA = Path(__file__).resolve().parent.parent / "examples" / "k09l"
STUDY_FILE = DATA / "k09l_study.json"

# The cruise CFD of revision B the design basis is calibrated to: k09l_study.json, history entry cfd_cruise of
# 2026-11-17 16:56:35 (node hash 3725d8fe3bc6; OpenFOAM simpleFoam k-omega SST, half model, 564 106 cells, 500
# iterations, C_L varied by 0.005 over the last 25 %). An earlier, coarser run of the same inputs (349 517 cells)
# gave C_L 0.366 (2.23/rad): the spread is part of the uncertainty given to the calibrated inputs.
CFD_B = {"time": "2026-11-17 16:56:35", "node_hash": "3725d8fe3bc6", "cl": 0.348503421018928,
         "cd": 0.10490767604368395, "alpha_deg": 8.127213103276794, "V": 12.469326704902642,
         "cl_required": 0.5902777777777778, "cells": 564106, "iterations": 500, "separated_pct": 17.533593496314694}
CFD_UNCERTAINTY = 0.15  # relative, on the calibrated lift slope: mesh spread 2.10-2.23/rad + no transition model

TRANSITION = {"transition_pitch_end_deg": 80.0, "transition_pitch_rate_deg": 45.0, "transition_airspeed": 17.0}
THROTTLE_MAX = RB.THROTTLE_MAX  # kept: a lower limit costs transition robustness (NOT_APPLIED N8)

# metrics shown in the before / after tables: (result key, label, scale, unit)
KEY_METRICS = [
    ("mass_total", "Take-off mass", 1000.0, "g"), ("static_margin", "Static margin", 100.0, "%"),
    ("tipover_deg", "Tip-over angle standing", 1.0, "deg"), ("tipover_margin", "Margin over the gust lean", 1.0, "deg"),
    ("box_fit", "Transport-box fit (>= 1 fits)", 1.0, ""), ("tw_full", "Thrust-to-weight, full battery", 1.0, ""),
    ("tw_low", "Thrust-to-weight, end of battery", 1.0, ""), ("i_motor_max_full", "Motor current at full stick", 1.0, "A"),
    ("motor_current_margin", "Motor current margin", 1.0, ""), ("esc_margin", "ESC margin", 1.0, ""),
    ("battery_c_margin", "Battery C margin", 1.0, ""), ("throttle_headroom_low", "Control headroom, end of battery", 1.0, ""),
    ("hover_throttle", "Hover throttle", 100.0, "%"), ("endurance_hover", "Hover endurance", 1 / 60, "min"),
    ("cl_cruise", "Cruise C_L (1.2 V_stall)", 1.0, ""), ("v_cruise", "Cruise speed", 1.0, "m/s"),
    ("p_cruise_elec", "Cruise electrical power", 1.0, "W"), ("ld_cruise", "Cruise L/D (model polar)", 1.0, ""),
    ("endurance_cruise", "Cruise endurance", 1 / 60, "min"), ("mission_margin", "Mission energy margin", 1.0, ""),
    ("landing_n", "Landing load factor (1 m/s)", 1.0, "g"), ("foot_sf", "Foot outrigger safety factor", 1.0, ""),
    ("cost_total", "Total cost", 1.0, "USD"), ("reserve", "Budget reserve", 1.0, "USD"),
]

CHANGES = [
    {"id": "C1", "requirement": "R4", "analysis": "cfd_cruise",
     "title": "Aerodynamic design basis from the cruise CFD (lift slope 2.10/rad, C_L,max 0.49)",
     "what": "The wing lift slope of the flight model and the cruise calculation is taken from the cruise CFD of "
             "revision B instead of the lifting-line value: lift-slope factor 1.0 -> 0.5525 (3.81 -> 2.10/rad, "
             "C_L = 0.05 + 2.10 alpha) and C_L,max 0.85 -> 0.491 (same 12 deg stall angle as the flight model). "
             "The cruise point follows from the unchanged 1.2 V_stall rule: C_L 0.590 -> 0.341, 12.5 -> 16.4 m/s. "
             "No hardware changes; fly the cruise at about 16-17 m/s.",
     "why": "The CFD gives C_L 0.349 at the trim angle of 8.1 deg where the flight model needs 0.590 (59 %): the "
            "aircraft cannot cruise at 12.5 m/s at that angle, and every number that rests on the 3.81/rad slope "
            "(cruise speed, power, endurance, transition) is optimistic. With the CFD slope the cruise moves to "
            "16.4 m/s at 94 W (was 62 W): cruise endurance 45.6 -> 30.2 min and the mission energy margin "
            "3.54 -> 2.96 (R4 still met; 1.85 even with the coarse CFD's own drag, which it over-predicts). The "
            "new trim angle (7.9 deg) needs C_L 0.341, which the CFD gave at 8.1 deg: re-running the CFD at the "
            "revision-C trim point is the check.",
     "evidence": "ESTIMATE calibrated to a coarse RANS run (no transition model, one angle of attack, not mesh-"
                 "independent): +/- 15 %. Settled only by the wind-tunnel test T09 or the mission flight T16.",
     "params": ["cla_factor", "cl_max"],
     "effects": [("cl_cruise", "Cruise C_L", 1.0, ""), ("v_cruise", "Cruise speed", 1.0, "m/s"),
                 ("p_cruise_elec", "Cruise electrical power", 1.0, "W"),
                 ("endurance_cruise", "Cruise endurance", 1 / 60, "min"),
                 ("mission_margin", "Mission energy margin (R4)", 1.0, ""),
                 ("cfd_lift_ratio", "Cruise lift at trim / lift needed (CFD)", 100.0, "%")],
     "analyses": ["cfd_cruise"]},
    {"id": "C2", "requirement": "G3", "analysis": "flight_transition",
     "title": "Transition procedure: pitch over to 80 deg at 45 deg/s, wing-borne at 17 m/s",
     "what": "Forward transition pitches the nose over to 80 deg from vertical at 45 deg/s (was 60 deg/s) and "
             "declares wing-borne flight at 17 m/s (was 16 m/s); the back-transition enters from 17 m/s.",
     "why": "With the CFD lift slope (C1) the revision-B procedure loses 3.5 m in the nominal transition (0.4 m "
            "on the flight-model slope) and 4 of the 11 robustness cases end in ground contact. 16 m/s is only "
            "1.17 x the new stall speed (13.7 m/s); 17 m/s is 1.24 x. Swept in the 6-DOF model (pitch-over "
            "75/80/85 deg at 45/60/90 deg/s, 16/17/18 m/s): at 16 m/s every combination loses 2.9-4.8 m, at 17 m/s "
            "0.3-1.3 m. 80 deg at 45 deg/s and 17 m/s is the most robust: 8 of the 11 cases within the 5 m "
            "acceptance and one ground contact (B's procedure on the same slope: 6 and 4). 18 m/s survives a 4 m/s "
            "side gust on a full pack but ends in ground contact at 50 % charge, so 17 m/s is kept.",
     "evidence": "REDUCED-ORDER simulation on the CFD-calibrated (estimated) lift slope and the estimated prop "
                 "coefficients; verify in a high-altitude transition test before relying on it.",
     "params": ["transition_pitch_end_deg", "transition_pitch_rate_deg", "transition_airspeed"],
     "effects": [],
     "analyses": ["flight_transition"]},
]

# robustness of the forward transition (6-DOF reduced-order model, revision_b.ROBUSTNESS_CASES + EXTRA_CASES, dt
# 0.004 s, start 30 m) computed with robustness_reference() on 2026-11-17: altitude loss in m, 'ground' = ground
# contact. 'B (CFD slope)' is revision B's procedure with C1's lift slope.
ROBUSTNESS_REFERENCE = {
    "B (flight-model slope)": {"nominal": 0.38, "battery 20 % charge": "ground", "battery 50 % + side gust 4 m/s":
                               "ground", "side gust 4 m/s": 0.44, "head gust 4 m/s": 0.21, "tailwind gust 2 m/s":
                               0.43, "tailwind gust 4 m/s": "ground", "vertical gust 2 m/s": 0.57,
                               "payload 0.3 kg": 0.49, "steady headwind 4 m/s": 0.12, "motor M2 at 85 %": 0.31},
    "B (CFD slope)": {"nominal": 3.54, "battery 20 % charge": 5.53, "battery 50 % + side gust 4 m/s": "ground",
                      "side gust 4 m/s": "ground", "head gust 4 m/s": 0.42, "tailwind gust 2 m/s": 3.48,
                      "tailwind gust 4 m/s": "ground", "vertical gust 2 m/s": 3.70, "payload 0.3 kg": 4.39,
                      "steady headwind 4 m/s": 3.27, "motor M2 at 85 %": "ground"},
    "C": {"nominal": 0.35, "battery 20 % charge": 23.64, "battery 50 % + side gust 4 m/s": "ground",
          "side gust 4 m/s": 15.18, "head gust 4 m/s": 0.33, "tailwind gust 2 m/s": 0.34, "tailwind gust 4 m/s": 0.37,
          "vertical gust 2 m/s": 0.58, "payload 0.3 kg": 0.41, "steady headwind 4 m/s": 0.14,
          "motor M2 at 85 %": 0.45,
          # charge cases run to set the flight-card limits (same model, soc0 = usable charge at the start)
          "battery 35 %": "ground", "battery 50 %": 1.16, "battery 70 %": 0.60, "battery 50 % + tailwind 2 m/s": 0.87,
          "battery 70 % + side gust 4 m/s": "ground", "battery 90 % + side gust 4 m/s": 17.41},
    "C at output limit 0.64": {"nominal": 0.44, "battery 20 % charge": "ground", "battery 50 % + side gust 4 m/s":
                               "ground", "side gust 4 m/s": "ground", "head gust 4 m/s": 0.39, "tailwind gust 2 m/s":
                               0.43, "tailwind gust 4 m/s": 0.44, "vertical gust 2 m/s": 0.83, "payload 0.3 kg": 0.60,
                               "steady headwind 4 m/s": 0.25, "motor M2 at 85 %": 20.37},
}

OPERATING_LIMITS = [
    "Cruise at 16.4 m/s or faster (1.2 x the 13.7 m/s stall speed of the CFD-calibrated wing); do not trim below "
    "15 m/s in wing-borne flight until the wind-tunnel test (T09) or a flight log (T16) gives the real lift slope.",
    "Forward transition only with at least 50 % battery charge and gusts up to 2 m/s: at 50 % the model loses "
    "1.2 m (0.9 m with a 2 m/s tailwind gust); at 35 % it ends in ground contact and at 20 % it loses 24 m.",
    "Head or tail gusts up to 4 m/s, a 0.3 kg payload or a motor at 85 % thrust only with a fresh pack: 0.3-0.5 m "
    "at full charge (these cases were simulated at full charge only).",
    "No forward transition in side gusts above 2 m/s: a 4 m/s side gust loses 15-17 m at 90-100 % charge and ends "
    "in ground contact at 50-70 % (on the CFD lift slope the elevons have 45 % less roll authority). 2 m/s side "
    "gusts were not simulated.",
    "Transition at a safe height (30 m in the simulations) until test flights confirm the model.",
]

NOT_APPLIED = [
    {"id": "N1", "requirement": "R10", "title": "Battery further forward for static margin",
     "why_not": "Not possible in this airframe: the pack's front face (x 47 mm) already touches the inner face of "
                "the pod's front wall (x 45-47 mm, CAD). Moving it forward needs a longer pod (new CAD), which the "
                "parametric model cannot represent."},
    {"id": "N2", "requirement": "R10", "title": "Ballast at the motor mounts for an 8 % static margin",
     "why_not": "About 97 g (4 x 24 g at the motor-mount elbows, x = -114 mm) moves the CG 8.1 mm forward (static "
                "margin 5.3 -> 8.0 %) but costs 47 s of hover endurance (11.4 -> 10.6 min). The model has no ballast-"
                "position parameter (a new mass input needs a graph.py mapping so the heavy analyses are marked "
                "stale). Decide after T02 (CG) and T09 (neutral point): the 5.3 % rests on an estimated neutral "
                "point (+/- 5 % chord)."},
    {"id": "N3", "requirement": "R10, R6", "title": "Foot aft wall 4 mm thinner (foot_shorten_mm 6 -> 10)",
     "why_not": "Evaluated: static margin +0.43 % (5.31 -> 5.74 %), tip-over +0.29 deg, 10 g lighter. Not applied: "
                "the elevon trailing edge would stand only 10 mm above the ground (14 mm in B, 20 mm in A) and the aft "
                "wall that takes the touchdown drops from 9.8 to 5.8 mm, for a gain far below the uncertainty of "
                "the estimated neutral point."},
    {"id": "N4", "requirement": "R10", "title": "Neutral point from the CFD",
     "why_not": "Needs a second cruise CFD run at another angle of attack (e.g. 4 deg) and the pitching moment from "
                "the pressure field (dC_m/dalpha gives the neutral point). field_analyses.run_cfd_cruise does not "
                "output C_m yet; heavy CFD runs were not part of this change."},
    {"id": "N5", "requirement": "G3 (assembly)", "title": "Close the two open harness ends in the CAD",
     "why_not": "The assembly check of revision B finds 'Harness receiver CRSF' touching only the receiver (nearest "
                "other part: the receiver's foam-tape pad, 0.91 mm) and 'Harness servo R (S6)' touching only the "
                "servo extension plug (nearest other part: the wing core, 0.54 mm; the check needs 0.5 mm). Route "
                "the CRSF lead to the FC's ELRS header and the S6 lead to the servo connector in the Fusion model, "
                "re-export and re-run the assembly check; the parametric model has no harness-routing parameter, "
                "and moving wire geometry without the CAD would be invented geometry."},
    {"id": "N6", "requirement": "R4", "title": "Larger wing to restore a 12.5 m/s cruise",
     "why_not": "At the CFD lift slope the same weight and 1.2 V_stall margin at 12.5 m/s need about 1.73 x the "
                "wing area (0.345 -> 0.60 m^2). The wing planform is fixed in the imported CAD (no parametric wing "
                "option), the 1.25 m transport box (R5) allows at most 0.10 m more span (+9 % area at the same "
                "chord), and the mission margin stays 2.96 at 16.4 m/s, so the speed change (C1) is the "
                "proportionate answer."},
    {"id": "N7", "requirement": "cfd_cruise", "title": "Wing section / separated flow",
     "why_not": "17.5 % of the wetted surface shows reversed near-wall flow at alpha 8.1 deg in the CFD. A section "
                "with a better low-Reynolds behaviour (or turbulators) may recover lift, but the model cannot "
                "represent a section change and the coarse RANS run has no transition model to judge it."},
    {"id": "N8", "requirement": "R7, R2", "title": "Lower motor output limit (0.64) for the thin R7 margin",
     "why_not": "0.64 raises the R7 margin 1.07 -> 1.12 and keeps it >= 1 for a C_P 15 % above the estimate "
                "(0.66: 0.99), but costs thrust: T/W at the end of the battery 1.90 -> 1.81, and 1.597 - below the "
                "1.6 line of R2 - for a C_T 12 % below the estimate (0.66: 1.67). It also costs the C2 transition: "
                "7 of the 11 robustness cases within 5 m and 3 ground contacts (0.66: 8 and 1), the motor-at-85 % "
                "case now losing 20 m. Both margins rest on the estimated prop coefficients: measure the current "
                "and thrust at the limit on the thrust stand (T03 / T04) and set the limit on that data."},
]

# supplier data that would replace the estimated inputs of the requirements that are not verified by test
DATASHEETS_NEEDED = {
    "R3": ["real order totals / receipts for every order (replace the $14-per-order shipping estimate and the "
           "estimated BOM prices)"],
    "R6": ["measured part masses (T01: the volume-scaled masses of the modified parts)",
           "a measured gust lean (T11) instead of the decision-page wind-study estimate (36 deg)"],
    "R8": [f"APC performance file {C.APC_PER3_11x45MR} (C_T, C_P)", "measured pack internal resistance (T07)",
           "measured part masses (T01)", "or simply the full-stick current measurement T04"],
    "R11": ["CF tube laminate properties (DragonPlate publishes none for FDPT.625*.515*24; ask DragonPlate or test "
            "a coupon)", f"APC performance file {C.APC_PER3_11x45MR} for the thrust",
            "measured pack IR (T07) and part masses (T01)", "or the proof-load test T05 with the thrust from T03"],
    "R5": [], "R12": [],
}

DATASHEET_UPGRADES = [
    {"input": "ESC Holybro Tekko32 F4 4in1 50A: i_cont 50 A per channel, cells 4-6S, 4 channels",
     "was": "getfpv.com listing title (supplier class, ratings read from the product name)",
     "now": "supplier datasheet: Holybro spec sheet 'Continuous Current: 50A x4', 'Burst Current: 60A x4', "
            "'Supports 4-6S battery', 'Weight: 13.8g' (burst and mass added; values unchanged)",
     "refs": ["https://cdn.shopify.com/s/files/1/0604/5905/7341/files/Holybro_Tekko32_F4_4in1_50A_ESC_spec_"
              "7f900ab6-7c29-483f-86a9-260c69b669fd.pdf",
              "https://www.flyingtech.co.uk/product/holybro-tekko32-f4-4in1-50a-esc-am32/",
              "https://www.readymaderc.com/products/details/87187-holybro-tekko32-f4-4in1-50a-esc-am32"],
     "status_change": "none (R8 stays CONDITIONAL: its full-throttle current rests on the estimated prop "
                      "coefficients, battery IR and the volume-scaled masses; R12 was already PASS)"},
    {"input": "Motor SunnySky X3108S KV900: K_v 900, R_m 86 mOhm, I_0 0.9 A/10 V, 25 A/30 s, 370 W, 81 g, 3-4S",
     "was": "robu.in listing (supplier class), read 2026-11-14",
     "now": "re-confirmed on the same listing 2026-11-17, URL added; flagged: another reseller lists identical values "
            "for the KV720 version, so the table may not be KV-specific (SunnySky's own datasheet not reached)",
     "refs": ["https://robu.in/?p=1569817", "https://st.robu.in/?p=1569817",
              "https://rcdrone.top/es/products/sunnysky-x3108s-brushless-motor"],
     "status_change": "none"},
]

DATASHEET_NOT_CONFIRMED = [
    {"input": "APC 11x4.5MR static C_T 0.105 / C_P 0.045 (estimates)",
     "checked": "APC lists PER3_11x45MR.dat on its performance-data page; the file could not be fetched from this "
                "session (blocked). The UIUC propeller database has no 11x4.5MR.",
     "refs": [C.APC_PER3_11x45MR, "https://www.apcprop.com/performance-data-new/",
              "https://m-selig.ae.illinois.edu/props/propDB.html"],
     "stays": "provisional estimate - download the file or measure on the thrust stand (T03)"},
    {"input": "Ovonic 4S 4000 mAh 130C internal resistance (4 mOhm/cell estimate)",
     "checked": "No manufacturer page with IR reachable (the BOM's ovonicshop URL is not reachable from this "
                "session; marketplace listings are not primary sources).",
     "refs": [], "stays": "provisional estimate - measure with the charger's IR readout (T07)"},
    {"input": "CF motor tube DragonPlate FDPT.625*.515*24 laminate moduli / allowable",
     "checked": "Product page: 'Pultruded in an epoxy resin', 'Standard (33 Msi)' fibre, 'Solid CF (All Uni "
                "Directional)', 'UD 0', max temperature 200 F, 0.134 lb per 24 in; no laminate modulus or strength. "
                "The braided-tube spec PDF does not apply to this pultruded tube.",
     "refs": ["https://dragonplate.com/dragonplate-carbon-tube-16mm-od-x-515id-x-24"],
     "stays": "provisional estimate (component_analysis.cf_tube_material) - note for its owner: the tube is UD "
              "pultruded, so its hoop / clamping strength is matrix-dominated"},
    {"input": "CF spar tube Superstition CFT3K12101",
     "checked": "Product page: 3K woven, 12 x 10 x 1000 mm, 52.88 g; no mechanical properties.",
     "refs": ["https://superstitionhobbies.com/products/3k-carbon-fiber-tube-12mm-x-10mm-x-1000mm"],
     "stays": "provisional estimate"},
    {"input": "PETG / ASA / XPS / Depron / G10 strengths and limits",
     "checked": "The K09-L files name no filament, foam or laminate brand, so no brand datasheet can be applied "
                "without assuming one.", "refs": [],
     "stays": "provisional estimates - name the filament / foam actually bought, then its TDS applies"},
    {"input": "Shipping (R3)", "checked": "Order-specific; no datasheet exists.", "refs": [],
     "stays": "provisional estimate until the receipts are in (T15)"},
]


# --------------------------------------------------------------------------------------------- calibration
def cfd_calibration(model, record: Optional[dict] = None) -> dict:
    """The lift slope implied by a cruise CFD record (same zero-lift offset C_L0 as the flight model), as a factor on
    the lifting-line slope of the model's wing, and the C_L,max at the flight model's stall angle."""
    rec = record or CFD_B
    wr = calcs.wing_reference(model)
    AR = wr["span"].value ** 2 / wr["area"].value
    helm = 2 * math.pi * AR / (2 + math.sqrt(AR * AR + 4))
    cl0 = float(model.params["cl0"].value)
    a = math.radians(rec["alpha_deg"])
    cla = (rec["cl"] - cl0) / a
    return {"cla_cfd": cla, "cla_lifting_line": helm, "cla_factor": cla / helm, "cl_max": cl0 + cla * A_STALL,
            "cl0": cl0, "stall_deg": math.degrees(A_STALL), "lift_ratio": rec["cl"] / rec["cl_required"],
            "aspect_ratio": AR, "record": dict(rec)}


def cfd_record_from_study(path: Path = STUDY_FILE, node_hash: str = CFD_B["node_hash"]) -> Optional[dict]:
    """The latest cfd_cruise run with ``node_hash`` (revision B's inputs) in a saved study, in CFD_B's keys."""
    try:
        d = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    hits = [h for h in d.get("history", []) if h.get("node") == "cfd_cruise" and h.get("node_hash") == node_hash
            and not h.get("error")]
    if not hits:
        return None
    h = hits[-1]
    ex = h.get("extra") or {}
    return {"time": h.get("time"), "node_hash": node_hash, "cl": ex.get("cl"), "cd": ex.get("cd"),
            "alpha_deg": ex.get("alpha_deg"), "V": ex.get("V"), "cl_required": ex.get("cl_required"),
            "cells": (h.get("solver") or {}).get("cells"), "iterations": (h.get("solver") or {}).get("iterations"),
            "separated_pct": ex.get("separated_pct")}


# --------------------------------------------------------------------------------------------- apply
def is_revision_b(model) -> bool:
    return (model.config.get("landing") == "feet_b" and model.config.get("mount") == "inboard_b"
            and model.config.get("mount_material") == "asa" and not model.params["shipping"].missing)


def apply(engine: Engine) -> list[dict]:
    """Apply revision C on top of revision B (revision B is applied first if the engine is not on it); returns
    the change events."""
    m = engine.model
    evs = [] if is_revision_b(m) else RB.apply(engine)
    cal = cfd_calibration(m)
    r = CFD_B
    base = (f"Revision C (C1): cruise CFD of revision B ({r['time']}, coarse RANS, {r['cells']} cells, "
            f"{r['iterations']} iterations): C_L {r['cl']:.3f} at alpha {r['alpha_deg']:.1f} deg")
    evs.append(engine.set_param("cla_factor", round(cal["cla_factor"], 4), source=ESTIMATE,
                                ref=base + f" -> lift slope {cal['cla_cfd']:.2f}/rad = {cal['cla_factor']:.4f} x the "
                                           f"lifting-line slope {cal['cla_lifting_line']:.2f}/rad; provisional until "
                                           "T09 / T16"))
    m.params["cla_factor"].uncertainty = CFD_UNCERTAINTY
    evs.append(engine.set_param("cl_max", round(cal["cl_max"], 4), source=ESTIMATE,
                                ref=base + f" -> C_L,max = {cal['cl0']:.2f} + {cal['cla_cfd']:.2f} x "
                                           f"{cal['stall_deg']:.0f} deg (the flight model's stall angle)"))
    m.params["cl_max"].uncertainty = CFD_UNCERTAINTY
    for k, v in TRANSITION.items():
        evs.append(engine.set_param(k, v, source=USER, ref="Revision C transition procedure (change C2)"))
    engine.recompute()
    return evs


def engines() -> tuple[Engine, Engine]:
    """Fresh engines on revision B and revision C (light results only; no heavy analyses)."""
    eb = Engine()
    RB.apply(eb)
    ec = Engine()
    RB.apply(ec)
    apply(ec)
    return eb, ec


# --------------------------------------------------------------------------------------------- comparison
def _val(q):
    return None if q is None or not isinstance(q.value, (int, float)) else float(q.value)


def comparison(eb: Optional[Engine] = None, ec: Optional[Engine] = None) -> list[dict]:
    """Before / after of every requirement, gate and key metric, computed by the engine for revisions B and C
    (light results; gate statuses include whatever heavy records the engines hold)."""
    if eb is None or ec is None:
        eb, ec = engines()
    rows = []
    rb = {x["id"]: x for x in AP.requirements_table(eb)}
    for x in AP.requirements_table(ec):
        b = rb[x["id"]]
        rows.append({"kind": "requirement", "id": x["id"], "label": x["text"], "unit": x["unit"],
                     "criterion": f"{x['op']} {x['threshold']:g}", "B": b["predicted"], "C": x["predicted"],
                     "status_B": b["status"], "status_C": x["status"], "class_B": b["predicted_class"],
                     "class_C": x["predicted_class"]})
    gb = {g["id"]: g for g in AP.gates(eb)}
    for g in AP.gates(ec):
        rows.append({"kind": "gate", "id": g["id"], "label": g["name"], "unit": "", "criterion": "", "B": None,
                     "C": None, "status_B": gb[g["id"]]["status"], "status_C": g["status"]})
    for key, label, scale, unit in KEY_METRICS:
        vb, vc = _val(eb.results.get(key)), _val(ec.results.get(key))
        rows.append({"kind": "metric", "id": key, "label": label, "unit": unit, "criterion": "",
                     "B": None if vb is None else vb * scale, "C": None if vc is None else vc * scale,
                     "status_B": None, "status_C": None,
                     "class_B": (eb.results.get(key) or None) and eb.results[key].source,
                     "class_C": (ec.results.get(key) or None) and ec.results[key].source})
    for p in ("cla_factor", "cl_max", "throttle_max", *TRANSITION):
        rows.append({"kind": "input", "id": p, "label": p, "unit": ec.model.params[p].unit, "criterion": "",
                     "B": eb.model.params[p].value, "C": ec.model.params[p].value, "status_B": None,
                     "status_C": None, "class_B": eb.model.params[p].source, "class_C": ec.model.params[p].source})
    return rows


# --------------------------------------------------------------------------------------------- what-ifs (not applied)
def what_if(ec: Optional[Engine] = None) -> dict:
    """The numbers behind CHANGES / NOT_APPLIED that are not plain before / after values, each re-evaluated by the
    engine on a scratch copy of revision C (the configuration of ``ec`` is never changed)."""
    if ec is None:
        _, ec = engines()

    def scratch(params=None, prop_scale=None):
        e = Engine()
        e.model.config = dict(ec.model.config)
        e.model.params = copy.deepcopy(ec.model.params)
        for k, v in (params or {}).items():
            e.model.params[k].value = v
        if prop_scale:
            for key, f in prop_scale.items():
                e.model.option("prop").data[key].value *= f
        e.recompute()
        return e.results

    r = ec.results
    out = {}
    # C1: the coarse CFD's own drag at its C_L, as an equivalent C_D0 of the parabolic polar
    k_ind = 1.0 / (math.pi * r["aspect_ratio"].value * ec.model.params["oswald"].value)
    cd0_cfd = CFD_B["cd"] - k_ind * CFD_B["cl"] ** 2
    rc = scratch({"cd0": cd0_cfd})
    out["cfd_drag"] = {"cd0_equivalent": cd0_cfd, "p_cruise_elec": rc["p_cruise_elec"].value,
                       "mission_margin": rc["mission_margin"].value}
    # N2: ballast at the motor-mount elbows for an 8 % static margin
    cg, M = r["cg"].value[0], r["mass_total"].value
    x_b = min(j["origin_mm"][0] for n, j in ec.model.joints.items() if n.startswith("Prop M")) / 1000.0
    dx = (0.08 - r["static_margin"].value) * r["chord"].value
    mb = M * dx / (cg - x_b - dx)
    rb = scratch({"payload": mb})  # same mass (placed at the pack: only the endurance is read)
    out["ballast"] = {"mass": mb, "x": x_b, "cg_shift": dx, "endurance_hover": rb["endurance_hover"].value,
                      "endurance_hover_now": r["endurance_hover"].value}
    # N3: foot aft wall 6 -> 10 mm
    rf = scratch({"foot_shorten_mm": 10.0})
    out["foot_shorten_10"] = {k: rf[k].value for k in ("static_margin", "tipover_deg", "mass_total")}
    # N6: wing area for 12.5 m/s at the CFD slope (same weight, same 1.2 V_stall C_L)
    out["wing_area_factor"] = (r["v_cruise"].value / CFD_B["V"]) ** 2  # S x this flies C's C_L at B's speed
    # N8 / C3: prop-data sensitivity of R7 and R2 at 0.66 and 0.64
    for th in (0.66, 0.64):
        rp = scratch({"throttle_max": th})
        rcp = scratch({"throttle_max": th}, {"cp0": 1.15})
        rct = scratch({"throttle_max": th}, {"ct0": 0.88})
        out[f"throttle_{th:.2f}"] = {"motor_current_margin": rp["motor_current_margin"].value,
                                     "tw_low": rp["tw_low"].value,
                                     "motor_current_margin_cp_plus15": rcp["motor_current_margin"].value,
                                     "tw_low_ct_minus12": rct["tw_low"].value}
    return out


# off-nominal cases added for the flight-card limits of revision C (same form as revision_b.ROBUSTNESS_CASES)
EXTRA_CASES = {"battery 35 %": dict(soc0=0.35), "battery 50 %": dict(soc0=0.5), "battery 70 %": dict(soc0=0.7),
               "battery 50 % + tailwind 2 m/s": dict(soc0=0.5, gust=RB._G((1, 0, 0), 2.0)),
               "battery 70 % + side gust 4 m/s": dict(soc0=0.7, gust=RB._G((0, 1, 0))),
               "battery 90 % + side gust 4 m/s": dict(soc0=0.9, gust=RB._G((0, 1, 0)))}
# the configurations of ROBUSTNESS_REFERENCE: (base, extra parameters, with the extra cases)
REFERENCE_SETS = {"B (flight-model slope)": ("B", {}, False), "B (CFD slope)": ("B+C1", {}, False),
                  "C": ("C", {}, True), "C at output limit 0.64": ("C", {"throttle_max": 0.64}, False)}


def _engine_for(base: str, params: dict) -> Engine:
    e = Engine()
    RB.apply(e)
    if base == "C":
        apply(e)
    elif base == "B+C1":  # revision B's procedure on the CFD lift slope (C1 only)
        cal = cfd_calibration(e.model)
        e.set_param("cla_factor", round(cal["cla_factor"], 4), source=ESTIMATE, ref="C1 only")
        e.set_param("cl_max", round(cal["cl_max"], 4), source=ESTIMATE, ref="C1 only")
    for k, v in params.items():
        e.set_param(k, v)
    return e


def _robust_job(args):
    name, case = args
    import numpy as np

    from opencae.engineering import flightsim as FS
    base, params, _ = REFERENCE_SETS[name]
    e = _engine_for(base, params)
    s = FS.simulate(e.model, "transition_forward", dt=0.004, **{**RB.ROBUSTNESS_CASES, **EXTRA_CASES}[case])
    o = s.meta.get("outcome", {})
    zmin = float(s.pos[:, 2].min())
    im = np.asarray(s.motor_current)
    return {"set": name, "case": case, "converged": bool(o.get("transition_converged")),
            "altitude_loss": round(float(s.pos[0, 2]) - zmin, 2), "ground_contact": bool(zmin < 0.5),
            "saturation_warnings": sum("saturated" in w for _, w in s.warnings),
            "time_above_motor_rating_s": round(float(((im > e.model.option("motor").q("imax").value).any(1)
                                                      * np.diff(s.t, prepend=s.t[0])).sum()), 2)}


def robustness_reference(workers: int = 2, sets: Optional[list] = None) -> dict[str, dict]:
    """Re-run the forward-transition cases behind ROBUSTNESS_REFERENCE (reduced-order evidence): {set: {case:
    altitude loss in m, or 'ground' for ground contact}} plus the raw rows under '_rows'."""
    from concurrent.futures import ProcessPoolExecutor
    jobs = [(n, c) for n in (sets or list(REFERENCE_SETS))
            for c in list(RB.ROBUSTNESS_CASES) + (list(EXTRA_CASES) if REFERENCE_SETS[n][2] else [])]
    with ProcessPoolExecutor(workers) as ex:
        rows = list(ex.map(_robust_job, jobs))
    out: dict = {"_rows": rows}
    for x in rows:
        out.setdefault(x["set"], {})[x["case"]] = "ground" if x["ground_contact"] else x["altitude_loss"]
    return out
