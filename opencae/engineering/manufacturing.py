"""Manufacturing and validation on the common model: process plans for the made parts, the physical
test matrix linked to the requirements, measurement import and the predicted-vs-measured comparison,
and the fabrication workflow (stage gates).

Evidence discipline (see quantities.py): fabrication times are labelled ESTIMATE with their basis;
print settings, tools and checks that are not from a K09-L source are labelled guidance; a test is
only PASS with a physical measurement - a prediction never passes a test ("simulation verification"
and "physical validation" are reported separately).
"""
from __future__ import annotations

import csv
import operator
from pathlib import Path
from typing import Optional

import numpy as np

from opencae.engineering.assembly import is_fastener, subassembly_of
from opencae.engineering.model import Component, EngineeringModel
from opencae.engineering.quantities import ESTIMATE, MISSING, SHORT, TEST, Q

GUIDANCE = "guidance (not from a K09-L source document)"

# Team constraint C4: fabrication at Jacobs Hall with these resources only.
JACOBS_HALL = ["FDM printers", "laser cutter", "hot-wire foam cutter", "hand tools"]

# --------------------------------------------------------------------------- process classification


def _first(c: Component) -> str:
    return c.key.split(" — ")[0].lower()


def process_of(m: EngineeringModel, c: Component) -> tuple[str, str, str]:
    """(process, material, provenance). Starts from the model's keyword classification and corrects
    the cases where the keywords mislead (stated in the provenance)."""
    f = _first(c)
    names = " ".join(m.bodies[i].name.lower() for i in c.bodies)
    if "music wire" in f or "torque rod" in f:
        return ("Cut and bend music wire", "Spring steel (music wire)",
                f"corrected: model process '{c.process}' (keyword 'wire' matched 'music wire')")
    if "usb extender" in f:
        return ("Purchase", "", f"corrected: model process '{c.process}' (keyword 'wire' matched 'Wireless'); "
                                "kit part of the F405 Wing V2")
    if c.process == "Purchase" and c.pn.startswith("K09L-"):
        if "fin clip" in f or ("petg" in names and "tpu" not in names):
            return "FDM print", "PETG", "corrected: K09L part with PETG bodies (model process 'Purchase')"
        if "hook-and-loop" in f or "strap" in f:
            return "Cut hook-and-loop strap to length", "Hook-and-loop tape", \
                "corrected: K09L part made from bought tape (model process 'Purchase')"
    if "control horn" in f:
        return "FDM print (PETG horn) + cut / drill G10 backplate", "PETG + G10", \
            "refined: horn is PETG, backplate G10 (body names)"
    return c.process, c.material, "model"


def _is_made(m: EngineeringModel, c: Component) -> bool:
    p = process_of(m, c)[0]
    return p != "Purchase" and not is_fastener(c)


def classification_notes(m: EngineeringModel) -> list[dict]:
    """Components whose model process string was corrected here (for the process-plan review)."""
    out = []
    for c in m.active_components():
        p, mat, prov = process_of(m, c)
        if prov != "model":
            out.append({"component": c.key, "model_process": c.process, "process": p, "reason": prov})
    return out


# --------------------------------------------------------------------------- geometry of a part

def _part_geometry(m: EngineeringModel, c: Component) -> dict:
    """Per-unit volume (cm^3) and size from the mesh. Open bodies (no volume) use area/2 x the
    thickness in the name when there is one (ESTIMATE)."""
    act = {b.index for b in m.active_bodies()}
    bs = [m.bodies[i] for i in c.bodies if i in act]
    if not bs:
        return {"volume_cm3": None, "volume_basis": "no geometry", "extents_mm": None, "bodies": 0}
    vol, basis = 0.0, "closed-mesh volume"
    for b in bs:
        if b.closed and b.volume > 0:
            vol += b.volume
        else:
            import re
            t = re.search(r"(\d+(?:\.\d+)?)\s*mm", c.key)
            if t:
                vol += b.area / 2.0 * float(t.group(1))
                basis = f"ESTIMATE: open surface, area/2 x {t.group(1)} mm thickness from the name"
            else:
                basis = "open surface: volume unknown"
    n_inst = max(c.qty, 1)
    # extents of one instance: the bodies of the first instance (bodies sorted by centroid y, z)
    per = max(1, round(len(bs) / n_inst))
    first = sorted(bs, key=lambda b: (round(b.centroid[1], 0), round(b.centroid[2], 0)))[:per]
    P = np.vstack([m.body_mesh(b)[0] for b in first])
    ext = np.ptp(P, axis=0)
    return {"volume_cm3": vol / 1000.0 / n_inst, "volume_basis": basis, "extents_mm": [round(float(e), 1) for e in ext],
            "bodies": len(bs)}


# --------------------------------------------------------------------------- FDM guidance

FDM_RATE_CM3_H = (10.0, 15.0)  # effective deposition rate, PETG 0.4 mm nozzle, 0.2 mm layers (basis)
TPU_RATE_CM3_H = (4.0, 7.0)


def _fdm_guidance(f: str) -> dict:
    """Orientation / supports / infill / layer guidance for one PETG/TPU part (guidance)."""
    base = {"layer_mm": 0.2, "nozzle_mm": 0.4, "perimeters": 4, "infill": "40 % gyroid",
            "material_notes": "PETG: dry filament (65 C, 4 h) if it strings; 235-245 C nozzle, 75-80 C bed",
            "anisotropy": "Layer adhesion is the weakest direction (Z, across layers): orient so the main "
                          "load path (bending / tension) runs in the XY plane along the extrusion lines; "
                          "avoid tensile or peel loads across layers."}
    rules = [
        ("motor mount", "Print with the motor face vertical and the tube socket axis in XY, so the thrust "
                        "bending of the elbow runs along the layers.",
         "tree supports under the socket overhang only", "5 perimeters, 60 % gyroid", "high: carries thrust "
         "and torque; bolt holes drilled / reamed to size after printing"),
        ("pod shell", "Floor on the bed, walls vertical (layer lines around the shell); loads are low "
                      "except at the wing seat.", "supports for the hatch lip / overhangs > 50 deg",
         "3 perimeters, 20 % gyroid", "medium"),
        ("pod hatch", "Outer skin down / flat side on the bed.", "none expected", "3 perimeters, 15 %", "low"),
        ("fin clip", "Lay flat so the clamping jaws and the screw boss are in XY (jaw bending along the "
                     "layers).", "none or minimal", "5 perimeters, 60 % gyroid", "high: clamps the fin and "
                     "motor tube; check screw boss for layer cracks"),
        ("foot", "Print with the landing-load axis in XY (impact bending along the layers); TPU pad printed "
                 "separately, flat.", "none for the pad; minimal for the PETG body",
         "PETG body 5 perimeters, 50 %; TPU 95A pad 3 perimeters, 30 %, 20-30 mm/s", "medium: landing impact"),
        ("control horn", "Horn arm flat on the bed (pushrod load in XY).", "none", "100 % (small part)",
         "high: control-critical; pin hole drilled to size"),
        ("gps mast", "Print lying on its side so the mast bending (gusts, handling) is along the layers.",
         "supports under the GPS platform", "4 perimeters, 30 %", "low-medium"),
        ("fin bearing", "Bore axis vertical (Z) for a round bore; low load.", "none",
         "100 % (small part)", "medium: ream bore for a free running fit on the 3/32 in torque rod"),
        ("esc deck", "Flat on the bed (2 mm plate).", "none", "100 % (thin plate)", "low"),
    ]
    for key, orient, supports, infill, crit in rules:
        if key in f:
            return {**base, "orientation": orient, "supports": supports, "infill": infill, "criticality": crit,
                    "class": GUIDANCE}
    return {**base, "orientation": "Largest flat face on the bed; main load path in XY.",
            "supports": "as needed for overhangs > 50 deg", "criticality": "review", "class": GUIDANCE}


# --------------------------------------------------------------------------- per-process data

def _time_estimate(proc: str, f: str, geo: dict, c: Component, m: EngineeringModel) -> Q:
    """Fabrication time per unit (h), ESTIMATE with its basis."""
    v = geo.get("volume_cm3") or 0.0
    ext = geo.get("extents_mm") or [0, 0, 0]
    if proc.startswith("FDM"):
        tpu = "tpu" in proc.lower() or "tpu" in f
        lo, hi = FDM_RATE_CM3_H
        if tpu:  # split by body volume between PETG and TPU
            act = {b.index for b in m.active_bodies()}
            vt = sum(m.bodies[i].volume for i in c.bodies if i in act and "tpu" in m.bodies[i].name.lower())
            vt = vt / 1000.0 / max(c.qty, 1)
            t_mid = (v - vt) / 12.5 + vt / 5.5 + 0.25
            basis = (f"PETG {v - vt:.1f} cm^3 at 10-15 cm^3/h + TPU {vt:.1f} cm^3 at 4-7 cm^3/h + 0.25 h setup "
                     "(solid CAD volume; effective rates include infill and perimeters)")
            unc = 0.3
        else:
            t_mid = v / ((lo + hi) / 2) + 0.25
            basis = (f"{v:.1f} cm^3 (solid CAD volume) at an effective 10-15 cm^3/h + 0.25 h setup / removal; "
                     "machine time, mostly unattended")
            unc = 0.25
        if "g10" in proc.lower():
            t_mid += 0.4
            basis += "; + 0.4 h G10 backplate cut and drill"
        return Q(t_mid, "h", ESTIMATE, "manufacturing.process_plan", uncertainty=unc, note=basis)
    if proc.startswith("Hot-wire"):
        area_m2 = ext[0] * ext[1] / 1e6
        t = 0.5 + 0.5 * area_m2 / 0.25
        return Q(t, "h", ESTIMATE, "manufacturing.process_plan", uncertainty=0.5,
                 note=f"0.5 h templates / setup + 0.5 h per 0.25 m^2 planform (planform {area_m2:.3f} m^2); "
                      "includes spar / hinge bevel cuts where present")
    if proc.startswith("Laser"):
        per = 2 * (ext[0] + ext[2]) if ext else 0
        t = 0.25 + per / 10.0 / 3600.0 * 2
        return Q(t, "h", ESTIMATE, "manufacturing.process_plan", uncertainty=0.5,
                 note=f"0.25 h setup + perimeter ~{per:.0f} mm at ~10 mm/s, 2 passes (Depron); knife cut ~3x longer")
    if "g10" in proc.lower():
        return Q(0.4, "h", ESTIMATE, "manufacturing.process_plan", uncertainty=0.5,
                 note="0.3 h hand cut / file + ~0.1 h drilling per small plate")
    if proc.startswith("Cut bought CF"):
        return Q(0.15, "h", ESTIMATE, "manufacturing.process_plan", uncertainty=0.5,
                 note="one cut + deburr / scuff for bonding per piece")
    if "music wire" in proc:
        return Q(0.4, "h", ESTIMATE, "manufacturing.process_plan", uncertainty=0.5,
                 note="cut + two bends in a vice, check against the CAD")
    if proc.startswith("Make harness"):
        act = {b.index for b in m.active_bodies()}
        n = len([i for i in c.bodies if i in act])
        if "bullet" in f:
            return Q(0.1, "h", ESTIMATE, "manufacturing.process_plan", uncertainty=0.5,
                     note="solder one bullet pair + heat-shrink")
        return Q(0.3 + 0.1 * 2 * n / max(c.qty, 1), "h", ESTIMATE, "manufacturing.process_plan", uncertainty=0.5,
                 note=f"0.3 h + 0.1 h per conductor end ({n} conductors modelled x 2 ends)")
    if "eva" in proc.lower() or "hook-and-loop" in proc.lower():
        return Q(0.15, "h", ESTIMATE, "manufacturing.process_plan", uncertainty=0.5, note="mark and knife-cut")
    return Q(None, "h", MISSING, "manufacturing.process_plan", note="no rate for this process")


def _tools(proc: str) -> list[str]:
    p = proc.lower()
    t = []
    if "fdm" in p:
        t += ["FDM printer (Jacobs Hall)", "deburring tool", "drill bits / reamer for holes"]
    if "hot-wire" in p:
        t += ["hot-wire foam cutter (Jacobs Hall)", "profile templates (laser-cut from MDF / acrylic)",
              "sanding block"]
    if "laser" in p:
        t += ["laser cutter (Jacobs Hall)", "steel rule + sharp knife (alternative)"]
    if "g10" in p:
        t += ["hacksaw / rotary tool (hand tools)", "drill", "files", "dust mask / respirator + vacuum: "
              "G10 dust is an irritant; do NOT laser-cut G10 (epoxy-glass)"]
    if "cf" in p:
        t += ["fine-tooth saw or rotary cut-off (hand tools)", "sandpaper", "dust mask; wet-cut or vacuum "
              "(carbon dust is conductive and an irritant); do NOT laser-cut"]
    if "music wire" in p:
        t += ["wire cutters (hardened)", "vice", "pliers"]
    if "harness" in p:
        t += ["temperature-controlled soldering iron", "wire strippers", "heat gun", "multimeter"]
    if "eva" in p or "hook-and-loop" in p:
        t += ["knife", "steel rule"]
    return t or ["hand tools"]


def _post(proc: str, f: str) -> list[str]:
    p = proc.lower()
    out = []
    if "fdm" in p:
        out += ["remove supports, deburr", "drill / ream holes to size (printed holes run undersize)"]
        if "tpu" in p:
            out.append("bond TPU pad to the PETG foot (CA + primer or mechanical)")
    if "hot-wire" in p:
        out += ["sand to template", "laminating film (iron on) - wing / elevons"]
    if "laser" in p:
        out += ["clean edges", "check flatness"]
    if "cf" in p:
        out += ["scuff bonding areas", "clean with IPA"]
    if "harness" in p:
        out += ["heat-shrink every joint", "label both ends", "continuity / short test"]
    return out


def _risks(proc: str, f: str) -> list[str]:
    p = proc.lower()
    r = []
    if "fdm" in p:
        r += ["layer delamination under loads across Z", "PETG creep under sustained clamp load (fin clips, "
              "screw bosses)", "printed-part strength not tested (K09-L unresolved input)"]
        if "motor mount" in f:
            r.append("bolt pattern MISSING in the catalog: measure the motor before printing")
    if "hot-wire" in p:
        r += ["wire lag / kerf errors at the leading / trailing edge", "warping from heat"]
    if "laser" in p:
        r += ["Depron melts / shrinks at high power: test cut first"]
    if "g10" in p or "cf" in p:
        r += ["dust hazard", "edge delamination when drilling without backing"]
    if "harness" in p:
        r += ["cold joints", "wrong motor phase order (rotation direction)", "polarity reversal"]
    return r


def _inspection(m: EngineeringModel, c: Component, proc: str, geo: dict) -> list[str]:
    um = m.component_mass(c)
    out = [f"weigh: compare with BOM unit mass {um.fmt(3, 1000, 'g')} ({SHORT.get(um.source, um.source)})"]
    if geo.get("extents_mm"):
        e = geo["extents_mm"]
        out.append(f"overall size vs CAD ~{e[0]:.0f} x {e[1]:.0f} x {e[2]:.0f} mm (x, y, z aircraft frame)")
    if "fdm" in proc.lower():
        out += ["visual: layer adhesion, no cracks at bosses", "fit check with the mating part / fastener"]
    if "harness" in proc.lower():
        out += ["continuity per conductor, no short between conductors", "pull test on every crimp / solder joint"]
    if "hot-wire" in proc.lower():
        out += ["profile against template; spar slot / hinge line straightness"]
    return out


def process_plan(model: EngineeringModel) -> list[dict]:
    """Process plan per made component (pn 'K09L-...' or a process other than Purchase, after the
    corrections in :func:`process_of`; fasteners and kit parts excluded)."""
    rows = []
    for c in model.active_components():
        if not (c.pn.startswith("K09L-") or c.process != "Purchase"):
            continue
        if not _is_made(model, c):
            continue
        proc, mat, prov = process_of(model, c)
        f = _first(c)
        geo = _part_geometry(model, c)
        t = _time_estimate(proc, f, geo, c, model)
        row = {
            "component": c.key, "pn": c.pn, "qty": model.effective_qty(c), "subassembly": subassembly_of(c),
            "process": proc, "material": mat, "process_source": prov,
            "volume_cm3_per_unit": None if geo["volume_cm3"] is None else round(geo["volume_cm3"], 2),
            "volume_basis": geo["volume_basis"], "extents_mm": geo["extents_mm"],
            "tools": _tools(proc), "facility": "Jacobs Hall (C4): " + ", ".join(JACOBS_HALL),
            "time_per_unit": t, "time_total": Q(None if t.value is None else t.value * model.effective_qty(c), "h",
                                                t.source,
                                                t.ref, t.uncertainty, "ESTIMATE: " + t.note),
            "time_label": "ESTIMATE", "inspection": _inspection(model, c, proc, geo),
            "post_processing": _post(proc, f), "risks": _risks(proc, f),
        }
        if proc.startswith("FDM"):
            row["fdm"] = _fdm_guidance(f)
        elif proc.startswith("Hot-wire"):
            row["notes"] = ("XPS: hot-wire with root / tip templates; cut slightly oversize and sand; keep wire "
                            "temperature low enough to avoid a wide kerf (" + GUIDANCE + ")")
        elif proc.startswith("Laser"):
            row["notes"] = ("Depron 6 mm: low power, high speed, multiple passes, or a knife along a steel rule ("
                            + GUIDANCE + ")")
        elif "g10" in proc.lower():
            row["notes"] = "G10: cut by hand / rotary tool with dust extraction; never laser (" + GUIDANCE + ")"
        rows.append(row)
    return rows


# --------------------------------------------------------------------------- test matrix

# (id, title, purpose, metrics verified, components, equipment, procedure, data, safety, extra criteria)
_TESTS = [
    ("T01", "Component and take-off mass measurement",
     "Measure every part and the assembled aircraft; replace BOM masses by measurements.",
     ["mass_total"], ["all"],
     ["digital scale 0.1 g (parts)", "scale 1 g / 5 kg (aircraft)"],
     ["tare the scale; weigh each part / subassembly as built", "record against the BOM line",
      "weigh the ready-to-fly aircraft with battery (no payload, C2)"],
     ["part masses (g)", "take-off mass (kg)"], ["none special"],
     [("part_mass_error", "<=", 0.10, "-", "each part within 10 % of its BOM mass (guidance)")]),
    ("T02", "CG verification",
     "Locate the centre of gravity and confirm the static margin.",
     ["static_margin", "cg_frac"], ["LiPo", "Pod"],
     ["CG balancer / two scales", "steel rule"],
     ["hang / balance the aircraft about y at the spar line; find the balance point",
      "or weigh on two supports and compute x_cg", "convert to % chord from the leading edge"],
     ["x_cg (mm from LE)", "CG fraction of chord", "static margin with the estimated NP"],
     ["battery secured; props off"], []),
    ("T03", "Motor thrust stand test",
     "Measure static thrust, current, rpm vs throttle for one motor-prop-ESC set at full and low battery voltage.",
     ["tw_low", "thrust_max_full", "thrust_max_low", "i_motor_max_full", "rpm_max_full"],
     ["Motor", "Prop", "ESC"],
     ["thrust stand with load cell", "power analyser / current sensor", "optical tachometer",
      "bench supply or 4S packs at 16.8 V and 14.0 V"],
     ["mount motor on the stand with the K09-L mount", "throttle sweep 0-100 % in 10 % steps, 3 s each",
      "repeat at full and low battery voltage", "T/W (low) = 4 x thrust(low) / weight"],
     ["thrust (N)", "current (A)", "voltage (V)", "rpm", "throttle (%)"],
     ["prop guard / blast shield; stand bolted; nobody in the prop plane", "LiPo charging bag"], []),
    ("T04", "Propulsion electrical test",
     "Confirm current draw against motor, ESC and battery ratings and the cell-count compatibility.",
     ["motor_current_margin", "esc_margin", "battery_c_margin", "cells_ok", "v_batt_max_low"],
     ["Motor", "ESC", "LiPo", "Harness"],
     ["current clamp / power analyser", "multimeter", "ESC telemetry log"],
     ["check cell count vs motor (3-4S) and ESC (4-6S) labels", "full-throttle 5 s burst on the stand, log "
      "per-motor current and pack voltage", "margins = rating / measured current"],
     ["per-motor current (A)", "pack current (A)", "pack voltage under load (V)"],
     ["current-limited first power-up", "fire-safe area"], []),
    ("T05", "Structural load test (motor tube, spar, fin clips)",
     "Proof-load the primary load paths to the design load x safety factor.",
     ["motor_tube_sf"], ["Motor tube", "Spar tube", "Fin clip", "Motor mount"],
     ["weights / spring scale", "dial gauge", "fixture"],
     ["fix the tube at its clamp", "apply max thrust x 1.5 at the motor mount in steps", "measure tip deflection, "
      "hold 30 s, check for cracking / permanent set", "repeat for the spar (wing tip load) and fin clips"],
     ["load (N)", "deflection (mm)", "permanent set (mm)", "failure mode"],
     ["eye protection (CF splinters)", "load in steps"],
     [("permanent_set_mm", "<=", 0.1, "mm", "no permanent set after proof load (guidance)")]),
    ("T06", "Motor / ESC temperature test",
     "Verify motor and ESC temperatures after a hover-duration run.",
     ["motor_loss_hover", "motor_loss_max", "motor_current_margin"], ["Motor", "ESC"],
     ["IR thermometer / thermocouples", "thrust stand"],
     ["run at hover throttle for 2 min, then 10 s full throttle", "measure motor case and ESC MOSFET "
      "temperatures immediately"],
     ["motor case temperature (C)", "ESC temperature (C)", "ambient (C)"],
     ["hot surfaces", "prop guard"],
     [("motor_temp_c", "<=", 80.0, "degC", "motor case <= 80 C (guidance; no supplier limit in the files)")]),
    ("T07", "Battery endurance test",
     "Measure the usable energy at the hover current and the mission energy margin.",
     ["endurance_hover", "mission_margin"], ["LiPo"],
     ["electronic load or the thrust stand", "charger with IR readout", "logger"],
     ["discharge at the predicted hover current to 3.5 V/cell under load", "record capacity and time",
      "measure internal resistance (charger)"],
     ["time to 3.5 V/cell (s)", "capacity used (mAh)", "IR per cell (mOhm)"],
     ["fire-safe area / LiPo bag; never below 3.3 V/cell"], []),
    ("T08", "Vibration measurement",
     "Check FC vibration levels from the motors / props before flight.",
     [], ["FC", "Motor", "Prop"],
     ["FC blackbox log", "prop balancer"],
     ["balance props", "restrained run-up 0-100 %", "inspect gyro / accelerometer spectra"],
     ["accelerometer RMS per axis", "dominant frequencies"], ["aircraft restrained", "prop guard"],
     [("vibe_rms_g", "<=", 0.3, "g", "accelerometer RMS <= 0.3 g (guidance)")]),
    ("T09", "Wind-tunnel test",
     "Measure C_D0, lift slope and neutral point (provisional estimates today).",
     ["ld_cruise", "p_cruise_elec", "v_cruise", "static_margin"], ["Wing core", "Pod", "Fin"],
     ["wind tunnel with 3-component balance (if available)", "or car-top / tow test rig"],
     ["mount at the CG", "alpha sweep at cruise speed", "derive C_D0, e, dC_m/dalpha -> neutral point"],
     ["lift, drag, pitching moment vs alpha", "speed"], ["model secured; props off"], []),
    ("T10", "Control system verification (servo direction / throw)",
     "Check servo direction, centring, elevon throw and failsafe.",
     [], ["Servo", "Control horn", "Pushrod", "FC"],
     ["throw meter / protractor", "transmitter", "servo tester"],
     ["centre servos, fit arms, set neutral", "check elevon direction for pitch and roll inputs in each FC "
      "mode", "measure throw (+/-30 deg design), check no binding at end points", "test failsafe"],
     ["elevon throw (deg) per input", "direction OK", "servo current at end points"],
     ["props off"], [("elevon_throw_deg", ">=", 30.0, "deg", "+/-30 deg reachable (joint limits in the CAD)")]),
    ("T11", "Ground-contact / tip-over test",
     "Measure the static tip-over angle standing on the landing contacts and the drop behaviour.",
     ["tipover_margin", "tipover_deg", "landing_n"], ["Foot", "Fin"],
     ["tilt table or inclinometer", "drop rig 1 m/s"],
     ["stand the aircraft on its contacts", "tilt until it tips; record the angle in the worst direction",
      "drop test at ~1 m/s touchdown speed; inspect contacts"],
     ["tip-over angle (deg)", "damage after drop"], ["props off, battery dummy"], []),
    ("T12", "Hover test (tethered first)",
     "Confirm hover throttle, current and stability.",
     ["hover_throttle", "i_hover_total", "p_hover_elec", "tw_low"], ["all"],
     ["tether", "FC log", "current sensor"],
     ["tethered hover 30 s, then free hover 2 min within the flight card", "log throttle, current, attitude"],
     ["hover throttle (%)", "current (A)", "attitude error (deg)"],
     ["flight card limits (gust <= 8 m/s)", "safety pilot, clear area", "failsafe tested (T10)"], []),
    ("T13", "Transition readiness assessment",
     "Gate review before the first transition: all hard requirements validated by test.",
     [], ["all"], ["test records T01-T12"],
     ["review every requirement status", "no FAIL, no hard requirement NOT EVALUATED"],
     ["gate decision"], ["flight card"], []),
    ("T14", "Transport box fit check", "Fit the aircraft (props off) in the transport box.",
     ["box_fit"], ["all"], ["the transport box", "tape measure"],
     ["remove props", "place the aircraft in the box"], ["fits (yes/no)", "smallest clearance (mm)"], [], []),
    ("T15", "Cost and reserve audit", "Audit actual spend against the budget and crash reserve.",
     ["reserve"], ["all"], ["receipts", "BOM"], ["sum receipts incl. tax and shipping"],
     ["total spend (USD)"], [], []),
    ("T16", "Mission endurance flight (2 min hover + 5 min cruise)",
     "Measure the energy the mission really takes and the cruise speed / power at the wing-borne trim (the cruise "
     "CFD shows less lift than the flight model; only a flight settles it). Flown after G6.",
     ["mission_margin", "v_cruise", "p_cruise_elec"], ["LiPo", "Wing core"],
     ["flight controller log with the power module (voltage, current, airspeed)", "charger (energy put back)"],
     ["fresh, fully charged pack; flight card limits", "2 min hover, transition, 5 min level cruise at the "
      "planned speed, back-transition, land", "integrate V x I over the log for the mission energy; read the "
      "cruise airspeed and power from the level segment", "cross-check with the energy put back by the charger"],
     ["mission energy (Wh)", "cruise airspeed (m/s)", "cruise electrical power (W)"],
     ["G6 approved; safety pilot; flight card"], []),
]

_OPS = {"<=": operator.le, ">=": operator.ge, "==": lambda a, b: abs(a - b) < 1e-9, "<": operator.lt,
        ">": operator.gt}


def _qdict(q: Optional[Q]) -> Optional[dict]:
    if q is None:
        return None
    return {"value": q.value, "unit": q.unit, "class": q.source, "ref": q.ref}


def test_matrix(model: EngineeringModel, results: Optional[dict] = None) -> list[dict]:
    """Physical test matrix linking every model requirement and the key components to tests. ``results``
    (name -> Q, e.g. calcs.evaluate_all) gives the expected values with their evidence class; measured
    values start as None and the status as NOT EVALUATED."""
    reqs = model.requirements
    by_metric: dict[str, list] = {}
    for r in reqs:
        by_metric.setdefault(r.metric, []).append(r)
    comp_keys = [c.key for c in model.active_components()]
    out = []
    covered: set[str] = set()
    for tid, title, purpose, metrics, comps, equip, proc, data, safety, extra in _TESTS:
        if tid == "T13":
            rids = [r.id for r in reqs]
            crit_reqs = [r for r in reqs if r.kind in ("hard", "working")]
        else:
            crit_reqs = [r for mt in metrics for r in by_metric.get(mt, [])]
            rids = [r.id for r in crit_reqs]
        covered.update(rids)
        criteria = [{"requirement_id": r.id, "metric": r.metric, "op": r.op, "threshold": r.threshold.value,
                     "unit": r.threshold.unit, "source": r.source, "kind": r.kind,
                     "text": f"{r.text}: {r.metric} {r.op} {r.threshold.fmt()}"} for r in crit_reqs]
        criteria += [{"requirement_id": None, "metric": mt, "op": op, "threshold": thr, "unit": u, "source": GUIDANCE,
                      "kind": "guidance", "text": txt} for mt, op, thr, u, txt in extra]
        if comps == ["all"]:
            keys = []
        else:
            keys = [k for k in comp_keys if any(k.lower().startswith(w.lower()) for w in comps)]
        exp = {}
        if results is not None:
            for mt in dict.fromkeys(metrics + [c["metric"] for c in criteria]):
                if mt in results:
                    exp[mt] = _qdict(results[mt])
        out.append({
            "id": tid, "title": title, "purpose": purpose, "requirement_ids": rids,
            "components": keys or ["(whole aircraft)"], "equipment": equip, "procedure": proc,
            "data_to_record": data,
            "acceptance": {"text": "; ".join(c["text"] for c in criteria) or "recorded for information "
                           "(no numeric requirement)", "criteria": criteria},
            "safety": safety, "expected": exp or None, "measured": None, "status": "NOT EVALUATED",
            "verification_kind": "physical validation (test)",
        })
    for r in reqs:  # anything a future requirement adds gets its own verification line
        if r.id not in covered:
            out.append({"id": f"T-{r.id}", "title": f"Verification of {r.id} ({r.verification})",
                        "purpose": r.text, "requirement_ids": [r.id], "components": ["(whole aircraft)"],
                        "equipment": [], "procedure": [f"verify by {r.verification}"],
                        "data_to_record": [r.metric],
                        "acceptance": {"text": f"{r.metric} {r.op} {r.threshold.fmt()}",
                                       "criteria": [{"requirement_id": r.id, "metric": r.metric, "op": r.op,
                                                     "threshold": r.threshold.value, "unit": r.threshold.unit,
                                                     "source": r.source, "kind": r.kind,
                                                     "text": f"{r.metric} {r.op} {r.threshold.fmt()}"}]},
                        "safety": [], "expected": {r.metric: _qdict(results.get(r.metric))}
                        if results and r.metric in results else None,
                        "measured": None, "status": "NOT EVALUATED",
                        "verification_kind": "physical validation (test)"})
    return out


# --------------------------------------------------------------------------- measurements

FIELDS = ["test_id", "quantity", "value", "unit", "date", "operator", "notes"]

# unit -> (dimension, factor to the SI / base unit used by the model)
_UNITS = {
    "kg": ("mass", 1.0), "g": ("mass", 1e-3),
    "m": ("length", 1.0), "mm": ("length", 1e-3), "cm": ("length", 1e-2),
    "N": ("force", 1.0), "kgf": ("force", 9.80665), "gf": ("force", 9.80665e-3),
    "s": ("time", 1.0), "min": ("time", 60.0), "h": ("time", 3600.0),
    "A": ("current", 1.0), "V": ("voltage", 1.0), "W": ("power", 1.0), "rpm": ("speed", 1.0),
    "deg": ("angle", 1.0), "degC": ("temperature", 1.0), "C": ("temperature", 1.0),
    "-": ("ratio", 1.0), "": ("ratio", 1.0), "%": ("ratio", 0.01), "g_accel": ("accel", 1.0),
    "USD": ("money", 1.0), "ohm": ("resistance", 1.0), "m/s": ("velocity", 1.0),
    "J": ("energy", 1.0), "Wh": ("energy", 3600.0), "kWh": ("energy", 3.6e6),
}


def _to_base(value: float, unit: str) -> Optional[tuple[str, float]]:
    u = _UNITS.get(unit.strip()) if unit is not None else None
    return None if u is None else (u[0], value * u[1])


def import_measurements(path_csv) -> list[dict]:
    """Read measurements (columns test_id, quantity, value, unit, date, operator, notes). Values are
    floats (blank -> None); every row is a physical test measurement."""
    out = []
    with open(Path(path_csv), newline="", encoding="utf-8") as fh:
        rd = csv.DictReader(fh)
        missing = [f for f in FIELDS[:4] if f not in (rd.fieldnames or [])]
        if missing:
            raise ValueError(f"measurement CSV lacks columns {missing}")
        for row in rd:
            v = (row.get("value") or "").strip()
            rec = {"test_id": (row.get("test_id") or "").strip(), "quantity": (row.get("quantity") or "").strip(),
                   "value": float(v) if v else None, "unit": (row.get("unit") or "").strip(),
                   "date": (row.get("date") or "").strip(), "operator": (row.get("operator") or "").strip(),
                   "notes": (row.get("notes") or "").strip(), "class": TEST}
            for k in ("hardware_hash", "variant", "requirement_ids"):  # written by write_measurement_template
                if (row.get(k) or "").strip():
                    rec[k] = row[k].strip()
            out.append(rec)
    return out


def write_measurements(path_csv, rows: list[dict]) -> None:
    """Write measurement rows (or an empty template when ``rows`` has no values) in the import format."""
    with open(Path(path_csv), "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in FIELDS})


def measurement_template(matrix: list[dict]) -> list[dict]:
    """One empty row per (test, criterion metric) of a test matrix."""
    return [{"test_id": t["id"], "quantity": c["metric"], "value": None, "unit": c["unit"], "date": "",
             "operator": "", "notes": ""} for t in matrix for c in t["acceptance"]["criteria"]]


def _judge(op: str, value: float, thr: float) -> bool:
    return bool(_OPS[op](value, thr))


def compare(test_matrix: list[dict], measurements: list[dict], results: Optional[dict] = None) -> list[dict]:
    """Predicted vs measured per test criterion. ``simulation_verification`` judges the *prediction*
    against the criterion (it never makes a test pass); ``physical_validation`` judges the
    *measurement*: PASS / FAIL only with a measured value in a compatible unit, otherwise NOT
    EVALUATED (or UNIT MISMATCH). ``status`` is the physical validation result."""
    results = results or {}
    rows = []
    used: set[int] = set()
    for t in test_matrix:
        for c in t["acceptance"]["criteria"]:
            mt = c["metric"]
            ms = [(k, x) for k, x in enumerate(measurements) if x["test_id"] == t["id"] and x["quantity"] == mt]
            used.update(k for k, _ in ms)
            pq = results.get(mt)
            pred = None if pq is None or pq.missing or not isinstance(pq.value, (int, float)) else float(pq.value)
            thr = c["threshold"]
            base_thr = _to_base(thr, c["unit"]) if thr is not None else None
            # simulation verification (prediction vs criterion)
            if pred is None or base_thr is None:
                sim = "NO PREDICTION"
                pbase = None
            else:
                pb = _to_base(pred, pq.unit)
                pbase = pb[1] if pb and pb[0] == base_thr[0] else None
                sim = ("NO PREDICTION" if pbase is None else
                       ("PREDICTED PASS" if _judge(c["op"], pbase, base_thr[1]) else "PREDICTED FAIL")
                       + f" ({SHORT.get(pq.source, pq.source)}; simulation verification only)")
            for _, x in (ms or [(None, None)]):
                row = {"test_id": t["id"], "title": t["title"], "requirement_id": c["requirement_id"],
                       "quantity": mt, "criterion": f"{mt} {c['op']} {thr} {c['unit']}".strip(),
                       "predicted": pred, "predicted_unit": None if pq is None else pq.unit,
                       "predicted_class": None if pq is None else pq.source,
                       "measured": None, "measured_unit": None, "measured_class": None,
                       "error_pct": None, "simulation_verification": sim, "physical_validation": "NOT EVALUATED",
                       "date": None, "operator": None}
                if x is not None and x.get("value") is not None:
                    row.update(measured=x["value"], measured_unit=x["unit"], measured_class=TEST,
                               date=x.get("date"), operator=x.get("operator"))
                    mb = _to_base(x["value"], x["unit"])
                    if base_thr is None:
                        row["physical_validation"] = "RECORDED (no numeric criterion)"
                    elif mb is None or mb[0] != base_thr[0]:
                        row["physical_validation"] = "UNIT MISMATCH"
                    else:
                        row["physical_validation"] = "PASS" if _judge(c["op"], mb[1], base_thr[1]) else "FAIL"
                        if pbase is not None and abs(pbase) > 1e-12:
                            row["error_pct"] = (mb[1] - pbase) / abs(pbase) * 100.0
                row["status"] = row["physical_validation"]
                rows.append(row)
    for k, x in enumerate(measurements):  # measurements with no criterion are recorded, never judged
        if k in used:
            continue
        pq = results.get(x["quantity"])
        rows.append({"test_id": x["test_id"], "title": None, "requirement_id": None, "quantity": x["quantity"],
                     "criterion": None, "predicted": None if pq is None else pq.value,
                     "predicted_unit": None if pq is None else pq.unit,
                     "predicted_class": None if pq is None else pq.source,
                     "measured": x["value"], "measured_unit": x["unit"],
                     "measured_class": TEST if x["value"] is not None else None, "error_pct": None,
                     "simulation_verification": "n/a",
                     "physical_validation": "RECORDED (no criterion)" if x["value"] is not None else "NOT EVALUATED",
                     "status": "RECORDED (no criterion)" if x["value"] is not None else "NOT EVALUATED",
                     "date": x.get("date"), "operator": x.get("operator")})
    return rows


# --------------------------------------------------------------------------- closing requirements by measurement
#
# How a CONDITIONAL requirement becomes PASS (the only way: physical evidence):
#   1. write_measurement_template(path, engine, variant)  -> CSV, one row per measurement still needed, with the
#      requirement(s) it closes, the unit and the pass line; value column empty.
#   2. the team measures and fills value / date / operator (never a predicted value).
#   3. rows = import_measurements(path); meas = requirement_measurements(engine.model, rows)
#      -> {requirement id: {"value", "unit", "test_id", ...}} in the requirement's own unit, only for requirements
#      whose every input was measured on this hardware (rows with another hardware_hash are ignored).
#   4. approval.requirements_table(engine, meas) / approval.gates(engine, meas): evaluate_requirement judges the
#      measured value against the unchanged threshold -> PASS or FAIL.
#
# Each closure combines measured quantities with model constants that are supplier, user or CAD data; a constant
# that is an estimate blocks the closure (a measurement plus an estimate is not a measured result).

def _cons(m: EngineeringModel, r: dict) -> dict[str, Q]:
    """Model constants a closure may use (each must not be an estimate)."""
    from opencae.engineering import calcs
    wr = calcs.wing_reference(m)
    bat, mot, esc = m.option("battery"), m.option("motor"), m.option("esc")
    cap = bat.q("capacity")
    return {"g": m.params["g"], "budget": m.params["budget"], "imax": mot.q("imax"), "i_cont": esc.q("i_cont"),
            "c_rate": bat.q("c_rate"), "capacity_ah": Q(None if cap.value is None else cap.value / 3600.0, "Ah",
                                                        cap.source, cap.ref),
            "chord": wr["chord"]}


def _pred(r: dict, key: str, scale: float = 1.0, unit: Optional[str] = None) -> Optional[dict]:
    q = r.get(key)
    if q is None or q.value is None or not isinstance(q.value, (int, float)):
        return None
    return {"value": q.value * scale, "unit": unit or q.unit, "class": q.source}


def _pred_energy(m, r, which):
    bat = m.option("battery")
    if which == "usable":
        v = bat.q("capacity").value * bat.q("usable").value * bat.q("v_nom").value
        src = "supplier_spec"
    else:
        if r.get("p_hover_battery") is None or r.get("p_cruise_elec") is None:
            return None
        v = r["p_hover_battery"].value * m.params["hover_min"].value + r["p_cruise_elec"].value * m.params["cruise_min"].value
        src = r["p_cruise_elec"].source
    return {"value": v / 3600.0, "unit": "Wh", "class": src}


# requirement id -> closure. inputs: (test id, quantity, unit of the template, how to measure, predicted(model,
# results) -> dict | None). value(x, k): x = measured inputs in base units (kg, m, N, A, J, deg, USD, -),
# k = constants (floats) -> value in the requirement's unit. constants: keys of _cons that the value uses.
CLOSURE: dict[str, dict] = {
    "R1": {"inputs": [("T01", "mass_total", "kg", "ready-to-fly aircraft with battery, no payload (C2), props on",
                       lambda m, r: _pred(r, "mass_total"))],
           "constants": [], "formula": "mass_total", "value": lambda x, k: x["mass_total"]},
    "R2": {"inputs": [("T03", "thrust_low_per_motor", "N",
                       "static thrust of one motor-prop-ESC set (K09-L mount) at full stick with the flight-controller "
                       "output limit set, pack at 3.5 V/cell (14.0 V) open circuit",
                       lambda m, r: _pred(r, "thrust_max_low", 0.25)),
                      ("T01", "mass_total", "kg", "as for R1", lambda m, r: _pred(r, "mass_total"))],
           "constants": ["g"], "formula": "4 x thrust_low_per_motor / (mass_total x g)",
           "value": lambda x, k: 4.0 * x["thrust_low_per_motor"] / (x["mass_total"] * k["g"])},
    "R3": {"inputs": [("T15", "total_spend", "USD", "sum of all receipts incl. tax and shipping (both packs, every "
                       "order)", lambda m, r: _pred(r, "cost_total"))],
           "constants": ["budget"], "formula": "budget - total_spend",
           "value": lambda x, k: k["budget"] - x["total_spend"],
           "bound": lambda k, thr: f"total_spend <= {k['budget'] - thr:.0f} USD"},
    "R4": {"inputs": [("T07", "usable_energy", "Wh", "energy delivered by a fresh pack at the predicted hover current "
                       "down to the usable limit (80 % of rated capacity, never below 3.5 V/cell under load)",
                       lambda m, r: _pred_energy(m, r, "usable")),
                      ("T16", "mission_energy", "Wh", "energy drawn over the mission flight: 2 min hover + 5 min "
                       "cruise, transitions as flown (power-module log)", lambda m, r: _pred_energy(m, r, "mission"))],
           "constants": [], "formula": "usable_energy / mission_energy",
           "value": lambda x, k: x["usable_energy"] / x["mission_energy"]},
    "R5": {"inputs": [("T14", "box_fit", "-", "1 if the props-off aircraft fits the 1.25 x 0.55 x 0.45 m box in one "
                       "piece, else 0", lambda m, r: {"value": 1.0 if (r["box_fit"].value or 0) >= 1 else 0.0,
                                                     "unit": "-", "class": r["box_fit"].source})],
           "constants": [], "formula": "box_fit (1 = fits)", "value": lambda x, k: x["box_fit"]},
    "R6": {"inputs": [("T11", "tipover_deg", "deg", "tilt table: angle at which the standing aircraft (battery in, "
                       "props off) tips, worst direction", lambda m, r: _pred(r, "tipover_deg")),
                      ("T11", "gust_lean_deg", "deg", "lean equivalent of the 8 m/s gust: stand the aircraft in an "
                       "8 m/s airflow (anemometer) from the worst direction and measure the overturning tilt "
                       "(or the tilt-table angle with the measured gust moment)",
                       lambda m, r: {"value": m.params["gust_lean_deg"].value, "unit": "deg",
                                     "class": m.params["gust_lean_deg"].source})],
           "constants": [], "formula": "tipover_deg - gust_lean_deg",
           "value": lambda x, k: x["tipover_deg"] - x["gust_lean_deg"]},
    "R7": {"inputs": [("T04", "i_motor_full", "A", "phase-side current of one motor at full stick with the output "
                       "limit set, fresh pack (16.8 V), 5 s burst (ESC telemetry or DC clamp on its feed)",
                       lambda m, r: _pred(r, "i_motor_max_full"))],
           "constants": ["imax"], "formula": "imax / i_motor_full", "value": lambda x, k: k["imax"] / x["i_motor_full"],
           "bound": lambda k, thr: f"i_motor_full <= {k['imax'] / thr:.1f} A"},
    "R8": {"inputs": [("T04", "i_motor_full", "A", "as for R7", lambda m, r: _pred(r, "i_motor_max_full"))],
           "constants": ["i_cont", "imax"], "formula": "i_cont / max(imax, i_motor_full)",
           "value": lambda x, k: k["i_cont"] / max(k["imax"], x["i_motor_full"]),
           "bound": lambda k, thr: f"i_motor_full <= {k['i_cont'] / thr:.1f} A"},
    "R9": {"inputs": [("T04", "i_pack_full", "A", "pack current at full stick with the output limit set, fresh "
                       "pack, all four motors (power module / DC clamp)", lambda m, r: _pred(r, "i_battery_max"))],
           "constants": ["c_rate", "capacity_ah"], "formula": "c_rate x capacity_Ah / i_pack_full",
           "value": lambda x, k: k["c_rate"] * k["capacity_ah"] / x["i_pack_full"],
           "bound": lambda k, thr: f"i_pack_full <= {k['c_rate'] * k['capacity_ah'] / thr:.0f} A"},
    "R10": {"inputs": [("T02", "x_cg", "mm", "balance point aft of the wing leading edge at the root, flight-ready",
                        lambda m, r: None if r.get("cg") is None else
                        {"value": (r["cg"].value[0] - r["x_le"].value) * 1000.0, "unit": "mm", "class": r["cg"].source}),
                       ("T09", "x_np", "mm", "neutral point aft of the wing leading edge from the measured "
                        "dC_m/dalpha (wind tunnel / tow rig) or a glide test",
                        lambda m, r: {"value": m.params["np_frac"].value * r["chord"].value * 1000.0, "unit": "mm",
                                      "class": m.params["np_frac"].source})],
            "constants": ["chord"], "formula": "(x_np - x_cg) / chord (CAD)",
            "value": lambda x, k: (x["x_np"] - x["x_cg"]) / k["chord"],
            "bound": lambda k, thr: f"x_np - x_cg >= {thr * k['chord'] * 1000:.1f} mm"},
    "R11": {"inputs": [("T05", "proof_load_held", "N", "largest load at the motor-mount position held 30 s by the "
                        "motor tube without cracking or permanent set (apply in steps up to 1.5 x the measured "
                        "full-stick thrust)", lambda m, r: _pred(r, "thrust_max_full", 0.25 * 1.5)),
                       ("T03", "thrust_full_per_motor", "N", "static thrust of one set at full stick with the output "
                        "limit set, fresh pack (16.8 V)", lambda m, r: _pred(r, "thrust_max_full", 0.25))],
            "constants": [], "formula": "proof_load_held / thrust_full_per_motor (proof-test lower bound of the SF)",
            "value": lambda x, k: x["proof_load_held"] / x["thrust_full_per_motor"]},
    "R12": {"inputs": [("T04", "cells_ok", "-", "1 if the labels agree: pack cell count within motor (3-4S) and ESC "
                        "(4-6S) ratings, else 0", lambda m, r: _pred(r, "cells_ok"))],
            "constants": [], "formula": "cells_ok", "value": lambda x, k: x["cells_ok"]},
}


def closure_spec(model: EngineeringModel, rid: str) -> dict:
    """The measurement closure of a requirement; a requirement without an entry in CLOSURE is closed by a direct
    measurement of its metric in its own unit (in the first test of the matrix that covers it)."""
    if rid in CLOSURE:
        return CLOSURE[rid]
    req = next(r for r in model.requirements if r.id == rid)
    tid = next((t["id"] for t in test_matrix(model) if rid in t["requirement_ids"] and t["id"] != "T13"), f"T-{rid}")
    return {"inputs": [(tid, req.metric, req.threshold.unit, f"measure {req.metric} directly",
                        lambda m, r, k=req.metric: _pred(r, k))],
            "constants": [], "formula": req.metric, "value": lambda x, k, q=req.metric: x[q]}


def pass_line(model: EngineeringModel, rid: str, results: Optional[dict] = None) -> str:
    """The pass line of a requirement in terms of the measured quantities (with the model constants it uses)."""
    req = next(r for r in model.requirements if r.id == rid)
    sp = closure_spec(model, rid)
    k = {n: q.value for n, q in _cons(model, results or {}).items()}
    thr = req.threshold.value
    txt = f"{rid}: {sp['formula']} {req.op} {thr:g} {req.threshold.unit if req.threshold.unit != '-' else ''}".strip()
    if sp.get("bound") and all(k.get(c) is not None for c in sp["constants"]):
        txt += f"  ->  {sp['bound'](k, thr)}"
    if sp["constants"]:
        txt += " (" + ", ".join(f"{c} = {k[c]:.4g}" for c in sp["constants"] if k.get(c) is not None) + ")"
    return txt


TEMPLATE_FIELDS = FIELDS + ["requirement_ids", "pass_line", "predicted", "predicted_unit", "predicted_class",
                            "how_to_measure", "variant", "hardware_hash"]
# parameters that change the built aircraft or what it does on the test stand (besides the catalog selections)
HARDWARE_PARAMS = ("payload", "throttle_max", "foot_outrigger_mm", "foot_shorten_mm", "motor_inboard_mm")


def hardware_hash(model: EngineeringModel) -> str:
    """Hash of the physical configuration a measurement belongs to: catalog selections, geometry parameters,
    payload and the flight-controller output limit. Estimates, costs and procedures are not part of it, so
    entering e.g. the real shipping cost does not invalidate the measurements already recorded."""
    import hashlib
    import json
    blob = json.dumps({"config": model.config, "params": {k: model.params[k].value for k in HARDWARE_PARAMS
                                                          if k in model.params}}, sort_keys=True)
    return hashlib.sha1(blob.encode()).hexdigest()[:12]


def measurement_plan(engine, measurements: Optional[list] = None, only_open: bool = True,
                     variant: str = "") -> list[dict]:
    """One row per measurement still needed to close the requirements of the engine's configuration: test id,
    quantity, unit, the requirement(s) it closes, their pass lines, the prediction (with its evidence class) and how
    to measure. ``only_open`` keeps the requirements that are not PASS (CONDITIONAL, NOT EVALUATED, FAIL);
    requirements already closed by ``measurements`` drop out."""
    from opencae.engineering import approval as AP
    m, r = engine.model, engine.results
    meas = requirement_measurements(m, measurements or []) if measurements else {}
    rows: dict[tuple, dict] = {}
    for x in AP.requirements_table(engine, meas):
        if only_open and x["status"] == AP.PASS:
            continue
        sp = closure_spec(m, x["id"])
        for tid, qty, unit, how, pred in sp["inputs"]:
            key = (tid, qty)
            p = pred(m, r) if r else None
            row = rows.setdefault(key, {"test_id": tid, "quantity": qty, "value": None, "unit": unit, "date": "",
                                        "operator": "", "notes": "", "requirement_ids": [], "pass_line": [],
                                        "predicted": None if p is None else round(float(p["value"]), 4),
                                        "predicted_unit": None if p is None else p["unit"],
                                        "predicted_class": None if p is None else p["class"],
                                        "how_to_measure": how, "variant": variant,
                                        "hardware_hash": hardware_hash(m), "status_now": {}})
            row["requirement_ids"].append(x["id"])
            row["pass_line"].append(pass_line(m, x["id"], r))
            row["status_now"][x["id"]] = x["status"]
    out = []
    for row in rows.values():
        row["requirement_ids"] = "; ".join(row["requirement_ids"])
        row["pass_line"] = " | ".join(row["pass_line"])
        out.append(row)
    return out


def write_measurement_template(path, engine, variant: str = "", only_open: bool = True) -> Path:
    """CSV measurement template for the engine's configuration (``variant`` names it): one row per measurement
    needed, value empty. Fill value / date / operator / notes and import with import_measurements."""
    path = Path(path)
    rows = measurement_plan(engine, only_open=only_open, variant=variant)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=TEMPLATE_FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in TEMPLATE_FIELDS})
    return path


def closure_status(model: EngineeringModel, measurements: list[dict], hw_hash: Optional[str] = None) -> dict:
    """Per requirement: whether the measurements close it, its measured value (requirement unit), the inputs used,
    what is missing, and what was ignored (rows measured on other hardware, unknown or incompatible units).
    A row without a hardware_hash is accepted (the importer cannot tell); the template always writes one."""
    cfg = hw_hash or hardware_hash(model)
    cons = _cons(model, {})
    out = {}
    for req in model.requirements:
        sp = closure_spec(model, req.id)
        x, used, missing, ignored, blocked = {}, [], [], [], []
        for tid, qty, unit, _how, _p in sp["inputs"]:
            cand = [rw for rw in measurements if rw.get("quantity") == qty and rw.get("test_id") == tid
                    and rw.get("value") is not None]
            if not cand and qty == req.metric:  # a direct measurement of the metric from any test
                cand = [rw for rw in measurements if rw.get("quantity") == qty and rw.get("value") is not None]
            ok = []
            for rw in cand:
                if rw.get("hardware_hash") and rw["hardware_hash"] != cfg:
                    ignored.append(f"{rw['test_id']} {qty}: measured on hardware {rw['hardware_hash']}, not {cfg}")
                    continue
                b, want = _to_base(float(rw["value"]), rw.get("unit") or ""), _to_base(1.0, unit)
                if b is None or want is None or b[0] != want[0]:
                    ignored.append(f"{rw['test_id']} {qty}: unit '{rw.get('unit')}' is not a {unit} quantity")
                    continue
                ok.append((rw, b[1]))
            if not ok:
                missing.append(f"{tid} {qty} [{unit}]")
                continue
            rw, v = ok[-1]  # the last reading in the file
            x[qty] = v
            used.append({"test_id": rw["test_id"], "quantity": qty, "value": rw["value"], "unit": rw.get("unit"),
                         "date": rw.get("date"), "operator": rw.get("operator"), "readings": len(ok)})
        for c in sp["constants"]:
            q = cons[c]
            if q.missing or q.source not in ("supplier_spec", "user_input", "analytical", "test_measurement"):
                blocked.append(f"{c} is {q.source} ({q.ref})")
        rec = {"requirement_id": req.id, "closed": False, "value": None, "unit": req.threshold.unit,
               "inputs": used, "missing": missing, "ignored": ignored, "blocked": blocked, "formula": sp["formula"]}
        if not missing and not blocked:
            k = {c: cons[c].value for c in sp["constants"]}
            rec.update(closed=True, value=float(sp["value"](x, k)),
                       test_id="+".join(dict.fromkeys(u["test_id"] for u in used)))
        out[req.id] = rec
    return out


def requirement_measurements(model: EngineeringModel, measurements: list[dict],
                             hw_hash: Optional[str] = None) -> dict:
    """Measurement records for approval.requirements_table / approval.gates: {requirement id: {"value", "unit",
    "test_id", "class", "inputs"}} for the requirements whose every input was measured on this hardware."""
    return {rid: {"value": c["value"], "unit": c["unit"], "test_id": c["test_id"], "class": TEST,
                  "inputs": c["inputs"], "formula": c["formula"]}
            for rid, c in closure_status(model, measurements, hw_hash).items() if c["closed"]}


# --------------------------------------------------------------------------- workflow

def fabrication_workflow() -> list[dict]:
    """Stage-gated workflow from requirements to design iteration (owner roles and gates are
    guidance for the K09-L team)."""
    S = [
        ("requirements", "Requirements", [], "Systems lead",
         ["constraints C1-C4 and goals recorded", "requirements R1.. with metric, threshold, source, "
          "verification method"], "Requirements baselined and traceable to sources"),
        ("preliminary_design", "Preliminary design", ["requirements"], "Design lead",
         ["configuration options listed", "level-1 sizing (mass, T/W, endurance)"], "Feasible options exist"),
        ("configuration_selection", "Configuration selection", ["preliminary_design"], "Team",
         ["trade study recorded (decision page)", "catalog options selected"], "Decision recorded with rationale"),
        ("detailed_design", "Detailed design", ["configuration_selection"], "CAD / assembly engineer",
         ["CAD complete, BOM linked", "joints and linkages defined", "drawings issued"], "Design review passed"),
        ("engineering_verification", "Engineering verification", ["detailed_design"], "Analysis engineer",
         ["level-1 results evaluated, requirement status reviewed", "assembly check: no unexplained "
          "interference, joints resolved", "FEA of motor tube / mounts"],
         "No predicted FAIL on a hard requirement; open items registered"),
        ("manufacturing_preparation", "Manufacturing preparation", ["engineering_verification"],
         "Manufacturing engineer",
         ["process plans per made part", "missing data measured (motor bolt pattern)", "Jacobs Hall bookings "
          "(C4)", "purchased parts ordered"], "All inputs for fabrication available"),
        ("fabrication", "Fabrication", ["manufacturing_preparation"], "Fabrication team",
         ["parts made per process plan", "traveller record per part"], "All parts made"),
        ("inspection", "Inspection", ["fabrication"], "Quality / test engineer",
         ["masses and key dimensions recorded (T01)", "nonconformances dispositioned"], "Parts accepted"),
        ("assembly", "Assembly", ["inspection"], "Assembly engineer",
         ["assembly steps followed", "fasteners per step", "CG check (T02)"], "Aircraft assembled, CG in range"),
        ("ground_testing", "Ground testing", ["assembly"], "Test engineer",
         ["T03-T08, T10, T11 executed", "measurements imported, compared with predictions"],
         "No FAIL; hard requirements validated by test"),
        ("flight_testing", "Controlled flight testing", ["ground_testing"], "Flight test lead / safety pilot",
         ["tethered then free hover (T12)", "transition readiness (T13)", "flight card"], "Flight card signed"),
        ("design_iteration", "Design iteration", ["flight_testing"], "Team",
         ["model updated with measurements (evidence class test)", "revision saved", "lessons learned"],
         "Next revision baselined"),
    ]
    return [{"id": i, "stage": n, "depends_on": d, "owner": o, "checklist": c, "gate": g, "class": GUIDANCE}
            for i, n, d, o, c, g in S]
