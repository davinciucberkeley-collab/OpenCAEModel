"""Component screening: every component of the configuration gets a documented verdict.

The aircraft-level analyses (foot FEA, motor-tube and spar FEA, motor-mount thermal, cruise CFD) cover a few
parts. This module evaluates *every* active component of the configuration:

* **Made or load-carrying parts** (wing core, elevons, fins, pod shell / hatch, plates, doubler, control horn,
  fin bearing, fin clips, spar joiner, torque rod, motor mounts, GPS mast, battery saddle): a linear static
  CalculiX run (C3D10) of the part's own CAD body under screening load cases derived from the model, held where
  it touches its neighbours (contact detection: part surface nodes within ``CONTACT_TOL`` of a neighbour body,
  or inside it). Output per part: von Mises stress (primary field), displacement, equivalent strain and maximum
  principal stress fields with the nodal displacement vectors (deformed shape / animation), safety factor
  against the material strength (with its source), status, worst point, critical region, mesh profile, load
  cases and assumptions. Parts already covered by an aircraft-level run (feet, motor tubes, spar tubes, and the
  mount temperature) reuse that record. A body the mesher cannot turn into an acceptable tetra mesh is not run
  on a bad mesh: the reason is recorded and an average-contact-stress check is made instead (status at best
  ``watch``, because local peaks are unknown).
* **Electronics, motors, battery, connectors, wires, fasteners and bought mechanical parts**: analytical checks
  in the same output - current against the supplier rating held in the model, wire ampacity against the
  current (cited AWG table), fastener stress under the screening loads, mass against the BOM. A rating the
  model does not hold is never invented: the check is ``nev`` ("not evaluated - rating missing") and states the
  demand a rating has to cover.

Screening load cases (from the model; units N, mm, MPa)
-------------------------------------------------------
landing   1 m/s touchdown standing on the tail. Standing nose-up the tail points to the ground (aircraft frame:
          x aft; ground-plane normal -x), so every part is decelerated by the landing load factor n_land
          (results 'landing_n', energy method over the pad stroke): body force n_land g along +x, plus the
          masses a part carries entering through its contact with them. The fin takes the whole touchdown force
          (results 'landing_force_total') through one foot (tilted touchdown, as the foot FEA).
flight    ``N_AERO`` = 4 g pull-up (the design load factor of the spar FEA; there is no flight-envelope
          requirement in the K09-L files: estimate): uniform lift pressure n W / S on the wing's lower surface,
          inertia n g along -z.
control   elevons: lower-surface pressure n W / S + q_D C_N; fins: side pressure q_D C_N; q_D at the screening
          design speed V_D = ``VD_FACTOR`` x the cruise speed of the flight model, C_N = ``CN_MAX`` (flat-plate
          maximum normal-force coefficient: an upper bound for full elevon deflection or ~15 deg sideslip). Hinge
          moments from the CAD (elevon planform about the 'Elevon R' joint axis) set the pushrod force, the
          torque-rod torque and the servo torque demand.
thrust    full-throttle thrust per motor (calcs.propulsion thrust_max_full / 4) along the propeller joint axis
          and the motor reaction torque (calcs.motor_point at the same operating point).

Status: ``fail`` SF < 1; ``watch`` SF < line (2.0 printed / foam parts, 1.5 CF / G10 / metal); ``ok``
otherwise; ``nev`` not evaluated (reason given). A component's status is the worst of its primary checks
(fail > watch > nev > ok). Every result rests on provisional or supplier-datasheet material data and on
screening loads: a successful run is not proof of safe operation and not test evidence.
"""
from __future__ import annotations

import math
import re
import time
from collections import defaultdict
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from opencae.engineering import calcs
from opencae.engineering import field_analyses as FA
from opencae.utilities import crash

OK, WATCH, FAIL, NEV = "ok", "watch", "fail", "nev"
_RANK = {FAIL: 3, WATCH: 2, NEV: 1, OK: 0}
SUP, EST, STD = "supplier datasheet", "provisional estimate", "standard"

G_MM = 9806.65  # mm/s^2
N_AERO = 4.0  # flight screening load factor (same as the spar FEA): estimate
VD_FACTOR = 1.5  # screening design speed / cruise speed of the flight model: estimate
CN_MAX = 1.2  # flat-plate maximum normal-force coefficient (upper bound for full deflection / sideslip): estimate
CONTACT_TOL = 0.5  # mm: part surface nodes this close to a neighbour body (or inside it) are in contact
MAX_ELEMENTS = 40_000  # screening runs stay small: CalculiX peak memory well under ~1.5 GB (shared machine)
POOR_LIMIT_PCT = 10.0  # a mesh with more poorly shaped tetrahedra than this is not used
RATING_LINE = 1.2  # rating checks: margin below this is 'watch' (demands rest on provisional prop data)

# --------------------------------------------------------------------------------------------- sources
FOAMULAR = ("Owens Corning FOAMULAR 150 Product Data Sheet, Pub. No. 23513-E (2007): compressive strength >= 15 psi "
            "(103 kPa, ASTM D1621, at yield or 10 % deflection), flexural strength >= 60 psi (414 kPa, ASTM C203). "
            "https://www2.owenscorning.com/comminsul/documents/foamular150.pdf")
EVAZOTE = ("Zotefoams Evazote EV50 data (Polyformes data page): compression stress at 25 % strain 50 kPa, tensile "
           "strength 749 kPa (BS EN ISO 7214). https://poly.gwp.co.uk/foam-materials/evazote/evazote-ev50/")
G10_GUND = ("The Gund Company, NEMA G-10 Glass Epoxy Laminate data sheet (TGCR 10/2025): flexural strength 435 MPa "
            "lengthwise / 373 MPa crosswise (3 mm), density 1.85 g/cm3; typical values, not specification values. "
            "https://thegundcompany.com/wp-content/uploads/2024/03/NEMA-G-10-Glass-Epoxy-Laminate-by-The-Gund-Co.pdf")
MUSIC_WIRE = ("Master Spring & Wire Form Co., properties of materials: music wire ASTM A228, minimum tensile strength "
              "230-399 ksi (1586-2751 MPa, falling with diameter), E 30e6 psi (207 GPa), G 11.5e6 psi (79.3 GPa), "
              "design torsional stress 45 % of the minimum tensile strength. "
              "https://www.masterspring.com/wp-content/uploads/2021/12/properties_of_materials.pdf")
A2_70 = ("ISO 3506-1 property class A2-70: Rm >= 700 MPa, Rp0.2 >= 450 MPa (Fastenal, Mechanical Properties of "
         "Metric Fasteners, Jan 2016, https://www.fastenal.com/content/feds/pdf/2016/01/"
         "Mechanical%20Properties%20Metric%20Fasteners%20Jan%202016.pdf)")
AWG_REF = ("PowerStream 'Wire Gauge and Current Limits' table, column 'maximum amps for chassis wiring' (single wire "
           "in air, not bundled; a conservative rule of thumb from the Handbook of Electronic Tables and Formulas). "
           "https://www.powerstream.com/Wire_Size.htm")
AWG_CHASSIS_A = {10: 55.0, 12: 41.0, 14: 32.0, 16: 22.0, 18: 16.0, 20: 11.0, 22: 7.0, 24: 3.5, 26: 2.2, 28: 1.4}
STRESS_AREA_MM2 = {2.0: 2.07, 2.5: 3.39, 3.0: 5.03, 4.0: 8.78, 5.0: 14.2}  # ISO 898-1 tensile stress area, coarse
STRESS_AREA_REF = "ISO 898-1 tensile stress area of the coarse thread (M2 2.07, M3 5.03, M5 14.2 mm2)"


def _v(prop, value, unit, source, ref):
    return {"property": prop, "value": value, "unit": unit, "source": source, "ref": ref}


def materials(model) -> dict:
    """Screening materials. Every value carries its source class: 'supplier datasheet' (with the reference) or
    'provisional estimate' (with the basis). 'strength' is the allowable compared with the governing stress."""
    from opencae.materials.database import by_category, min_strength

    def printed(cat):
        mm = by_category(cat)[0]
        s, k = min_strength(mm)
        E = mm.youngs_modulus / 1e6
        xy = max((v for k_, v in (mm.strengths or {}).items() if k_.endswith("_xy") and v), default=None)
        return {"name": mm.name, "E": E, "nu": mm.poissons_ratio, "strength": s / 1e6, "kind": "iso", "line": 2.0,
                "xy_strength": xy / 1e6 if xy else None,
                "source": SUP, "ref": mm.source,
                "strength_basis": f"weakest tensile strength of the supplier's printed specimens ({k}; interlayer "
                                  f"direction), compared with von Mises",
                "values": [_v("E (flat XY specimens)", E, "MPa", SUP, mm.source),
                           _v("nu", mm.poissons_ratio, "-", EST, "not given by the TDS (OpenCAE database estimate)"),
                           _v(f"strength ({k})", s / 1e6, "MPa", SUP, mm.source)],
                "note": "supplier test prints (100 % infill), not this printer and settings"}

    xps_basis = ("the K09-L XPS grade is not specified: values of a representative building-grade XPS data sheet; "
                 "E and nu estimated (no data-sheet modulus read; typical XPS 5-20 MPa)")
    xps = {"name": "XPS foam (hot-wire cut)", "E": 10.0, "nu": 0.2, "strength": 0.103, "kind": "iso", "line": 2.0,
           "source": EST, "ref": FOAMULAR,
           "strength_basis": "compressive strength 103 kPa (lowest of compressive 103 / flexural 414 kPa), compared "
                             "with von Mises: conservative in tension and bending",
           "values": [_v("E", 10.0, "MPa", EST, xps_basis), _v("nu", 0.2, "-", EST, xps_basis),
                      _v("compressive strength", 0.103, "MPa", EST, "FOAMULAR 150 data sheet; " + xps_basis),
                      _v("flexural strength", 0.414, "MPa", EST, "FOAMULAR 150 data sheet; " + xps_basis)],
           "note": "stresses of a one-material part with fixed supports do not depend on E; displacements and "
                   "strains scale with 1/E"}
    depron = dict(xps, name="Depron 6 mm sheet (extruded polystyrene)",
                  values=[dict(x, ref="no Depron data sheet read: Depron is an extruded polystyrene sheet; XPS values "
                                      "(FOAMULAR 150) used") for x in xps["values"]])
    eva_basis = "K09-L EVA grade not specified; Evazote EV50 used as representative (closed-cell EVA, 50 kg/m3)"
    eva = {"name": "EVA foam 6 mm", "E": 0.2, "nu": 0.1, "strength": 0.050, "kind": "iso", "line": 2.0,
           "source": EST, "ref": EVAZOTE,
           "strength_basis": "compression stress at 25 % strain (50 kPa): beyond it the saddle squashes by a quarter",
           "values": [_v("E", 0.2, "MPa", EST, "secant modulus 50 kPa / 0.25 from the EV50 data; " + eva_basis),
                      _v("nu", 0.1, "-", EST, "closed-cell foam, estimate"),
                      _v("compression stress 25 %", 0.050, "MPa", EST, eva_basis)],
           "note": "linear analysis of a soft foam: indicative only"}
    g10 = {"name": "G10 glass-epoxy laminate", "E": 18000.0, "nu": 0.13, "strength": 373.0, "kind": "iso",
           "line": 1.5, "source": SUP, "ref": G10_GUND,
           "strength_basis": "crosswise flexural strength 373 MPa (the lower direction), compared with von Mises",
           "values": [_v("E", 18000.0, "MPa", EST, "typical G10 modulus 16-20 GPa; not on the data sheets read"),
                      _v("nu", 0.13, "-", EST, "typical glass-epoxy laminate"),
                      _v("flexural strength (crosswise)", 373.0, "MPa", SUP, G10_GUND)],
           "note": "laminate treated as isotropic for the screening"}
    wire = {"name": "Music wire ASTM A228", "E": 207000.0, "nu": 207.0 / (2 * 79.3) - 1, "strength": 1586.0,
            "kind": "iso", "line": 1.5, "source": SUP, "ref": MUSIC_WIRE,
            "strength_basis": "lower bound of the A228 minimum tensile strength range (largest diameters), compared "
                              "with von Mises",
            "values": [_v("E", 207000.0, "MPa", SUP, MUSIC_WIRE), _v("G", 79300.0, "MPa", SUP, MUSIC_WIRE),
                       _v("minimum tensile strength (lower bound)", 1586.0, "MPa", SUP, MUSIC_WIRE)],
            "note": "K&S 506 is sold as music wire; its own certificate was not read"}
    from opencae.engineering.component_analysis import cf_tube_material
    cfm = cf_tube_material()
    o = cfm.orthotropic
    cf = {"name": "CF rod / tube laminate (transversely isotropic)", "E": o["E3"] / 1e6, "nu": 0.30,
          "strength": cfm.yield_strength / 1e6, "kind": "cf", "line": 1.5, "source": EST, "ref": cfm.source,
          "ortho": {k: (v / 1e6 if k.startswith(("E", "G")) else v) for k, v in o.items()},
          "strength_basis": "estimated axial allowable compared with the axial (fibre-direction) stress; von Mises "
                            "is shown for reference only",
          "values": [_v("E axial", o["E3"] / 1e6, "MPa", EST, cfm.source),
                     _v("E transverse", o["E1"] / 1e6, "MPa", EST, cfm.source),
                     _v("axial allowable", cfm.yield_strength / 1e6, "MPa", EST, cfm.source)],
          "note": "same provisional laminate as the motor-tube and spar FEA"}
    steel = {"name": "Steel (grade not stated)", "E": 200000.0, "nu": 0.3, "strength": 250.0, "kind": "iso",
             "line": 1.5, "source": EST, "ref": "OpenCAE library example 'Structural steel (S235/ASTM A36-like)'",
             "strength_basis": "yield 250 MPa of a mild steel: the supplier does not state the grade",
             "values": [_v("E", 200000.0, "MPa", EST, "handbook steel"), _v("yield", 250.0, "MPa", EST,
                                                                             "mild steel, grade not stated")]}
    a2 = {"name": "Stainless 18-8 (A2) screws, class A2-70 assumed", "strength": 450.0, "Rm": 700.0, "line": 1.5,
          "source": EST, "ref": A2_70,
          "strength_basis": "Rp0.2 450 MPa of class A2-70 vs the von Mises equivalent of tension + shear on the "
                            "stress area; the vendors' property class was not read (18-8 = A2, class 70 assumed)",
          "values": [_v("Rp0.2", 450.0, "MPa", EST, A2_70), _v("Rm", 700.0, "MPa", EST, A2_70)]}
    nylon = {"name": "Nylon 6/6 (moulded screws / standoffs)", "strength": 50.0, "line": 2.0, "source": EST,
             "ref": "Essentra lists the material only (Nylon 6/6); moulded PA66 tensile strength is typically 60-85 "
                    "MPa dry and lower when moisture-conditioned: 50 MPa used",
             "strength_basis": "estimated PA66 strength vs the von Mises equivalent on the stress area",
             "values": [_v("strength", 50.0, "MPa", EST, "moulded PA66, conditioned (estimate)")]}
    mount = printed("ASA" if model.config.get("mount_material") == "asa" else "PETG")
    return {"petg": printed("PETG"), "asa": printed("ASA"), "mount": mount, "xps": xps, "depron": depron, "eva": eva,
            "g10": g10, "music_wire": wire, "cf": cf, "steel": steel, "a2_70": a2, "nylon66": nylon}


# --------------------------------------------------------------------------------------------- what is screened
POD_GROUP = ("Pod shell", "Pod hatch", "LiPo", "Battery saddle", "Battery strap", "ESC deck", "Adapter plate",
             "ESC Holybro", "FC CoreWing", "Capacitor", "Receiver", "GPS Holybro", "GPS mast", "Airspeed sensor", "Pitot",
             "Silicone pressure", "USB extender", "Foam tape", "Connector pair", "Plug ", "Standoff", "Screw M3x14",
             "Screw M2x5", "Screw M3x10", "Harness ESC", "Harness GPS", "Harness PDB", "Harness USB", "Harness XT60",
             "Harness airspeed", "Harness battery", "Harness receiver")
WING_SMEAR = ("Elevon", "Hinge tape", "Control horn", "Torque rod", "Fin bearing", "Servo KST", "Servo arm", "Pushrod",
              "Clevis", "Servo doubler", "SHCS M2x16", "Nut M2", "Screw M2x8", "Harness servo", "Servo extension",
              "Harness motor", "Laminating film")
MOTOR_SET_GROUP = ("Motor tube", "Motor mount", "Motor SunnySky", "Prop APC", "Prop adapter", "Prop locknut", "Washer M5",
                   "SHCS M3x8", "SHCS M3x16", "Washer M3", "Locknut M3", "Bullet connector")
_LAND, _FLY = "land", "flight"
_SUP_FIN = ("Fin clip", "Wing core")  # + the motor tube for the fin itself


def _clip_spec(prefix: str, sid: str) -> dict:
    chain = {"type": "chain", "source": "fin", "at": ("Fin Depron",)}
    return {"id": sid, "prefix": prefix, "pick": "right", "material": "petg", "size": 1.5,
            "cases": {"landing": {"supports": ("Wing core",), "inertia": _LAND, "loads": [chain]},
                      "sideslip": {"supports": ("Wing core",), "loads": [chain]}},
            "notes": ["load: the reaction this clip gives the fin in the fin run, applied back on the clip's fin "
                      "contact (load-path chaining); held where it seats on the wing core",
                      "clamp screw preload not modelled"]}


FEA_SPECS = [
    {"id": "wing_core", "prefix": "Wing core", "pick": "first", "material": "xps", "size": 12.0,
     "smeared": WING_SMEAR,
     "carried": [{"at": ("Pod shell",), "masses": POD_GROUP, "reach": 60.0},
                 {"at": ("Spar tube",), "masses": ("Spar tube", "Spar joiner"), "reach": 10.0}],
     "cases": {"landing": {"supports": ("Fin Depron", "Fin clip"), "inertia": _LAND},
               "flight": {"supports": ("Spar tube",), "inertia": _FLY, "pressure": ("-z", "wing")}},
     "notes": ["landing: held by the fins and fin clips; the pod and everything in it enter at the pod contact, the "
               "spar at the spar slot, elevons / controls / servos / harness smeared over the core",
               "flight: held at the spar slot (the CF spar carries the wing bending: see the spar FEA)"]},
    {"id": "elevon_inboard", "prefix": "Elevon inboard", "pick": "right", "material": "xps", "size": 4.0,
     "cases": {"landing": {"supports": ("Hinge tape", "Control horn", "SHCS M2x16", "Torque rod"), "inertia": _LAND},
               "control": {"supports": ("Hinge tape", "Control horn", "SHCS M2x16", "Torque rod"), "inertia": _FLY,
                           "pressure": ("-z", "elevon")}},
     "notes": ["held along the hinge tape and at its drive (control horn and its screws, torque rod)",
               "the outboard elevon's hinge moment passing through the torque rod is checked on the torque rod"]},
    {"id": "elevon_outboard", "prefix": "Elevon outboard", "pick": "right", "material": "xps", "size": 4.0,
     "cases": {"landing": {"supports": ("Hinge tape", "Torque rod"), "inertia": _LAND},
               "control": {"supports": ("Hinge tape", "Torque rod"), "inertia": _FLY, "pressure": ("-z", "elevon")}},
     "notes": ["held along the hinge tape and at the torque rod that drives it"]},
    {"id": "fin", "prefix": "Fin Depron", "pick": "right", "material": "depron", "size": 12.0,
     "cases": {"landing": {"supports": _SUP_FIN + ("Motor tube",), "inertia": _LAND, "loads": [{"type": "ground"}]},
               "sideslip": {"supports": _SUP_FIN + ("Motor tube",), "pressure": ("+y", "fin")}},
     "notes": ["held at the fin clips, its contact with the wing core and along the CF motor tube bonded to its "
               "leading edge (the tube is two orders of magnitude stiffer than the Depron: taken as rigid; its own "
               "loads are in the motor-tube FEA)",
               "landing: the whole touchdown force through the upper foot (tilted touchdown, as the foot FEA), spread "
               "over the fin surface the foot clamps",
               "sideslip: side pressure q_D C_N on one face; Depron modulus estimated, so the deflection is "
               "indicative only"]},
    _clip_spec("Fin clip top A", "fin_clip_top_a"), _clip_spec("Fin clip top B", "fin_clip_top_b"),
    _clip_spec("Fin clip bottom A", "fin_clip_bottom_a"), _clip_spec("Fin clip bottom B", "fin_clip_bottom_b"),
    {"id": "pod_shell", "prefix": "Pod shell", "pick": "first", "material": "petg", "size": 2.5,
     "carried": [{"at": ("Battery saddle",), "masses": ("LiPo", "Battery saddle", "Battery strap", "Harness battery",
                                                        "Plug JST-XH", "Connector pair"), "reach": 40.0},
                 {"at": ("ESC deck",), "masses": ("ESC deck", "ESC Holybro", "Adapter plate", "FC CoreWing", "Capacitor",
                                                  "Standoff", "Screw M3x14", "Screw M2x5", "Harness ESC", "Harness PDB",
                                                  "Harness XT60", "Plug JST-SH", "Plug JST-GH"), "reach": 25.0},
                 {"at": ("Pod hatch",), "masses": ("Pod hatch", "GPS mast", "GPS Holybro", "Foam tape pad 1 mm, GPS",
                                                   "Harness GPS", "Screw M3x10"), "reach": 60.0},
                 {"at": ("Foam tape pad, ASPD", "Pitot"), "masses": ("Foam tape pad, ASPD", "Airspeed sensor", "Pitot",
                                                                     "Silicone pressure", "Harness airspeed"),
                  "reach": 40.0},
                 {"at": ("Foam tape pad, USB", "USB extender"), "masses": ("Foam tape pad, USB", "USB extender",
                                                                           "Harness USB"), "reach": 20.0},
                 {"at": ("Receiver",), "masses": ("Receiver", "Foam tape pad 1 mm, Rx", "Harness receiver"),
                  "reach": 20.0}],
     "cases": {"landing": {"supports": ("Wing core",), "inertia": _LAND},
               "flight": {"supports": ("Wing core",), "inertia": _FLY}},
     "notes": ["held where it is bonded to the wing core; battery, ESC / FC stack, hatch, sensors enter at their "
               "contacts"]},
    {"id": "pod_hatch", "prefix": "Pod hatch", "pick": "first", "material": "petg", "size": 2.5,
     "carried": [{"at": ("GPS mast",), "masses": ("GPS mast", "GPS Holybro", "Foam tape pad 1 mm, GPS", "Harness GPS"),
                  "reach": 40.0}],
     "cases": {"landing": {"supports": ("Pod shell", "Screw M3x10"), "inertia": _LAND},
               "flight": {"supports": ("Pod shell", "Screw M3x10"), "inertia": _FLY}},
     "notes": ["held on the shell lip and its four nylon screws; the GPS mast with the GPS enters at the mast"]},
    {"id": "gps_mast", "prefix": "GPS mast", "pick": "first", "material": "petg", "size": 2.0,
     "carried": [{"at": ("Foam tape pad 1 mm, GPS", "GPS Holybro"), "masses": ("GPS Holybro", "Foam tape pad 1 mm, GPS",
                                                                             "Harness GPS"), "reach": 30.0}],
     "cases": {"landing": {"supports": ("Pod hatch",), "inertia": _LAND},
               "flight": {"supports": ("Pod hatch",), "inertia": _FLY}},
     "notes": ["bonded on the hatch; the GPS on its foam pad enters at the pad"]},
    {"id": "battery_saddle", "prefix": "Battery saddle", "pick": "first", "material": "eva", "size": 4.0,
     "carried": [{"at": ("LiPo",), "masses": ("LiPo", "Battery strap", "Harness battery", "Connector pair",
                                              "Plug JST-XH"), "reach": 40.0}],
     "cases": {"landing": {"supports": ("Pod shell",), "inertia": _LAND},
               "flight": {"supports": ("Pod shell",), "inertia": _FLY}},
     "notes": ["the whole battery inertia through the saddle (strap and friction share neglected: conservative)"]},
    {"id": "adapter_plate", "prefix": "Adapter plate", "pick": "first", "material": "g10", "size": 1.2,
     "carried": [{"at": ("Standoff M2", "Screw M2x5"), "masses": ("FC CoreWing", "Standoff M2", "Screw M2x5"),
                  "reach": 15.0}],
     "cases": {"landing": {"supports": ("Standoff nylon", "Screw M3x14"), "inertia": _LAND},
               "flight": {"supports": ("Standoff nylon", "Screw M3x14"), "inertia": _FLY}},
     "notes": ["held on the nylon standoffs / screws of the ESC; the FC stack enters at its M2 standoffs"]},
    {"id": "servo_doubler", "prefix": "Servo doubler", "pick": "right", "material": "g10", "size": 0.6,
     "carried": [{"at": ("Servo KST", "Screw M2x8"), "masses": ("Servo KST", "Servo arm", "Screw M2x8"), "reach": 15.0}],
     "cases": {"landing": {"supports": ("Wing core",), "inertia": _LAND},
               "flight": {"supports": ("Wing core",), "inertia": _FLY}},
     "notes": ["bonded on the core; the whole servo inertia through the doubler (the pocket walls' share neglected)"]},
    {"id": "control_horn", "prefix": "Control horn", "body": "Horn", "pick": "right", "material": "petg", "size": 0.8,
     "cases": {"hinge": {"supports": ("Elevon inboard", "SHCS M2x16", "Nut M2"),
                         "loads": [{"type": "pushrod", "at": ("Clevis",)}]}},
     "notes": ["pushrod force = (inboard + outboard hinge moment) / horn arm, at the clevis; held on the elevon and its "
               "two M2 screws"]},
    {"id": "control_horn_backplate", "prefix": "Control horn", "body": "Backplate", "pick": "right", "material": "g10",
     "size": 0.6,
     "cases": {"hinge": {"supports": ("Elevon inboard",),
                         "loads": [{"type": "chain", "source": "control_horn", "via": ("SHCS M2x16", "Nut M2"),
                                    "at": ("SHCS M2x16",)}]}},
     "notes": ["load: the screw reactions of the horn run, passed through the screws into the backplate"]},
    {"id": "fin_bearing", "prefix": "Fin bearing", "pick": "right", "material": "petg", "size": 0.7,
     "cases": {"control": {"supports": ("Fin Depron",), "loads": [{"type": "bearing", "at": ("Torque rod",)}]},
               "landing": {"supports": ("Fin Depron",), "inertia": _LAND,
                           "loads": [{"type": "rod_inertia", "at": ("Torque rod",)}]}},
     "notes": ["control: half the outboard elevon's normal force through the torque rod (the hinge tape takes the "
               "rest)"]},
    {"id": "spar_joiner", "prefix": "Spar joiner", "pick": "first", "material": "cf", "size": 1.5,
     "cases": {"flight": {"supports": (("Spar tube", "y<0"),),
                          "loads": [{"type": "spar_root", "at": (("Spar tube", "y>0"),)}]}},
     "notes": ["the right spar half's root shear and bending moment (half-wing lift n W / 2 at a quarter span) "
               "through its overlap; the left half's overlap held"]},
    {"id": "torque_rod", "prefix": "Torque rod", "pick": "right", "material": "music_wire", "size": 1.5,
     "cases": {"control": {"supports": ("Elevon inboard",), "loads": [{"type": "rod_torque", "at": ("Elevon outboard",)}]}},
     "notes": ["torsion: the outboard elevon's hinge moment from its contact, held in the inboard elevon"]},
    {"id": "motor_mount", "prefix": "Motor mount", "pick": "right_upper", "material": "mount", "size": 2.0,
     "carried": [{"at": ("Motor SunnySky", "SHCS M3x8"), "masses": ("Motor SunnySky", "Prop APC", "Prop adapter",
                                                                   "Prop locknut", "Washer M5", "SHCS M3x8"),
                  "reach": 60.0}],
     "cases": {"thrust": {"supports": ("Motor tube",), "loads": [{"type": "motor", "at": ("Motor SunnySky", "SHCS M3x8")}]},
               "landing": {"supports": ("Motor tube",), "inertia": _LAND}},
     "notes": ["held on the motor tube (clamp preload not modelled); thrust and the motor reaction torque at the "
               "motor base and its screws"]},
]
REUSE = (("Motor tube", "fea_motor_tube"), ("Spar tube", "fea_spar"))  # + the landing component -> fea_foot


# ============================================================================================ small helpers
def _first(key: str) -> str:
    return key.split(" — ")[0]


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def _worst(statuses) -> str:
    st = [s for s in statuses if s in _RANK]
    return max(st, key=lambda s: _RANK[s]) if st else NEV


def _status(sf: Optional[float], line: float) -> str:
    if sf is None or not np.isfinite(sf):
        return NEV
    return FAIL if sf < 1.0 else (WATCH if sf < line else OK)


def _f(x, nd=3):
    try:
        return None if x is None else round(float(x), nd)
    except (TypeError, ValueError):
        return None


def _match(key: str, prefixes) -> bool:
    return any(key.startswith(p) for p in prefixes)


class _Ctx:
    """Shared data of one screening run (model, results, loads, materials, contacts, finished runs)."""

    def __init__(self, engine, workdir: Path, fields_dir: Path):
        self.engine, self.m, self.r = engine, engine.model, engine.results
        self.workdir, self.fields_dir = Path(workdir), Path(fields_dir)
        self.mats = materials(self.m)
        self.bm = calcs.body_masses(self.m)
        self.active = self.m.active_bodies()
        self.runs: dict = {}
        self._surf: dict = {}
        self.loads = self._loads()
        self._hinge = None

    # ---------------------------------------------------------------- loads from the model
    def _loads(self) -> dict:
        m, r = self.m, self.r
        g = float(m.params["g"].value)
        W = float(r["weight"].value)
        S = float(r["area"].value)
        q = lambda k: (r[k].value if k in r and r[k].value is not None else None)  # noqa: E731
        n_land, F_touch = q("landing_n"), q("landing_force_total")
        Vc = float(r["v_cruise"].value)
        rho = float(m.params["rho"].value)
        VD = VD_FACTOR * Vc
        qD = 0.5 * rho * VD * VD  # Pa
        p_wing = N_AERO * W / S  # Pa
        T = float(r["thrust_max_full"].value) / 4.0
        cap = m.params["throttle_max"].value or 1.0
        op = calcs.motor_point(m, float(r["v_batt_max_full"].value), cap)
        omega = op["rpm"] * 2 * math.pi / 60
        Qm = op["p_shaft"] / max(omega, 1e-9) * 1e3  # N mm
        return {"g": g, "W": W, "S": S, "n_land": n_land, "F_touch": F_touch, "V_cruise": Vc, "V_D": VD, "q_D": qD,
                "rho": rho, "p_wing_MPa": p_wing * 1e-6, "p_ctrl_MPa": qD * CN_MAX * 1e-6,
                "p_elevon_MPa": (p_wing + qD * CN_MAX) * 1e-6, "T_motor": T, "Q_motor_Nmm": Qm, "rpm": op["rpm"],
                "n_aero": N_AERO,
                "text": {
                    "landing": (f"1 m/s touchdown on the tail: n_land {n_land:.1f} g along +x (towards the ground "
                                f"standing nose-up); touchdown force {F_touch:.0f} N" if n_land else
                                "landing load factor missing (no pad stroke for this landing option)"),
                    "flight": f"{N_AERO:g} g pull-up: lift pressure n W / S = {p_wing:.0f} Pa on the wing's lower "
                              f"surface, inertia {N_AERO:g} g along -z (W {W:.1f} N, S {S:.3f} m²)",
                    "control": f"elevon pressure n W / S + q_D C_N = {p_wing + qD * CN_MAX:.0f} Pa; fin side pressure "
                               f"q_D C_N = {qD * CN_MAX:.0f} Pa (V_D = {VD_FACTOR:g} x V_cruise = {VD:.1f} m/s, q_D "
                               f"{qD:.0f} Pa, C_N {CN_MAX:g})",
                    "thrust": f"full-throttle thrust {T:.1f} N per motor along the propeller axis and the motor "
                              f"reaction torque {Qm:.0f} N mm ({op['rpm']:.0f} rpm)"}}

    def inertia(self, kind: Optional[str]):
        """(acceleration magnitude in mm/s^2, unit direction) of a case's inertia, or (0, None)."""
        if kind == _LAND and self.loads["n_land"]:
            return self.loads["n_land"] * G_MM, np.array([1.0, 0.0, 0.0])
        if kind == _FLY:
            return N_AERO * G_MM, np.array([0.0, 0.0, -1.0])
        return 0.0, None

    # ---------------------------------------------------------------- geometry
    def bodies(self, prefix: str, name: Optional[str] = None) -> list:
        return [b for b in self.active if b.component.startswith(prefix) and (name is None or b.name.startswith(name))]

    def pick(self, spec: dict):
        bs = self.bodies(spec["prefix"], spec.get("body"))
        if not bs:
            return None
        rule = spec.get("pick", "first")
        if rule == "right":
            return max(bs, key=lambda b: b.centroid[1])
        if rule == "right_upper":
            return max(bs, key=lambda b: (b.centroid[1], b.centroid[2]))
        return bs[0]

    def box(self, b):
        if b.index not in self._surf:
            P = self.m.body_mesh(b)[0]
            self._surf[b.index] = (P.min(0), P.max(0))
        return self._surf[b.index]

    def touching(self, a, b, tol: float = CONTACT_TOL) -> bool:
        """Do bodies a and b touch (gap <= tol) or overlap? Sample points of each surface (vertices and four
        points per triangle) against the other surface, both ways (exact point-triangle distance, NumPy)."""
        la, ha = self.box(a)
        lb, hb = self.box(b)
        if np.any(la > hb + tol) or np.any(lb > ha + tol):
            return False
        for x, y in ((a, b), (b, a)):
            P, T = self.m.body_mesh(x)
            S = np.vstack([P, np.einsum("sk,tkd->tsd", _BARY, P[T]).reshape(-1, 3)])
            ly, hy = self.box(y)
            S = S[np.all((S >= ly - tol) & (S <= hy + tol), axis=1)]
            if not len(S):
                continue
            d, ins = _surface_distance(*self.m.body_mesh(y), S, tol, bool(y.closed))
            if (d <= tol).any() or ins.any():
                return True
        return False

    def neighbours(self, body) -> list:
        """Active bodies of other components touching ``body`` (gap <= CONTACT_TOL or overlapping)."""
        memo = self.__dict__.setdefault("_nb_memo", {})
        if body.index not in memo:
            memo[body.index] = [b for b in self.active if b.component != body.component and self.touching(body, b)]
        return memo[body.index]

    def fastener_hosts(self) -> dict:
        """For every active fastener body, the non-fastener components it joins (touching, propagated through
        fastener stacks screw - washer - nut); the rule of assembly._fastener_hosts with the NumPy contact test."""
        if getattr(self, "_hosts", None) is not None:
            return self._hosts
        from opencae.engineering.assembly import is_fastener
        comps = self.m.components
        fb = [b for b in self.active if is_fastener(comps[b.component])]
        others = [b for b in self.active if not is_fastener(comps[b.component])]
        hosts = {b.index: {o.component for o in others if self.touching(b, o)} for b in fb}
        adj = {b.index: {o.index for o in fb if o.component != b.component and self.touching(b, o)} for b in fb}
        seen: set = set()
        for b in fb:
            if b.index in seen:
                continue
            stack, grp = [b.index], []
            seen.add(b.index)
            while stack:
                u = stack.pop()
                grp.append(u)
                for v in adj[u] - seen:
                    seen.add(v)
                    stack.append(v)
            joined = set().union(*(hosts[u] for u in grp))
            for u in grp:
                hosts[u] = set(joined)
        self._hosts = hosts
        return hosts

    def mass(self, prefixes, near=None, reach: float = 0.0, exclude=()) -> tuple[float, list]:
        """Mass (kg) of the active bodies of components matching ``prefixes`` (optionally only those whose centroid
        lies in the bounding box of the ``near`` bodies grown by ``reach``) plus mass-only BOM lines."""
        if near:
            P = np.vstack([self.m.body_mesh(b)[0] for b in near])
            lo, hi = P.min(0) - reach, P.max(0) + reach
        out, tot = [], 0.0
        for b in self.active:
            if b.index in exclude or not _match(b.component, prefixes):
                continue
            if near and not (np.all(b.centroid >= lo) and np.all(b.centroid <= hi)):
                continue
            out.append(b.index)
            tot += self.bm.get(b.index, 0.0)
        if not near:
            for c in self.m.active_components():
                if not c.bodies and _match(c.key, prefixes):
                    tot += (self.m.component_mass(c).value or 0.0) * self.m.effective_qty(c)
        return tot, out

    def unit_mass(self, prefix: str) -> float:
        c = next((c for k, c in self.m.components.items() if k.startswith(prefix)), None)
        return 0.0 if c is None else float(self.m.component_mass(c).value or 0.0)

    # ---------------------------------------------------------------- control-surface geometry
    def hinge(self) -> dict:
        """Hinge moments, pushrod force and servo torque demand of the right elevon set (control case)."""
        if self._hinge is not None:
            return self._hinge
        from scipy.spatial import cKDTree
        m, L = self.m, self.loads
        j = m.joints.get("Elevon R")
        js = m.joints.get("Servo horn R")
        out = {"ok": False}
        ein = max(self.bodies("Elevon inboard"), key=lambda b: b.centroid[1], default=None)
        eout = max(self.bodies("Elevon outboard"), key=lambda b: b.centroid[1], default=None)
        horn = max(self.bodies("Control horn", "Horn"), key=lambda b: b.centroid[1], default=None)
        clevis = max(self.bodies("Clevis"), key=lambda b: b.centroid[1], default=None)
        rod = max(self.bodies("Pushrod"), key=lambda b: b.centroid[1], default=None)
        arm = max(self.bodies("Servo arm"), key=lambda b: b.centroid[1], default=None)
        if not (j and ein and eout):
            out["why"] = "elevon hinge joint or elevon bodies not in the model"
            self._hinge = out
            return out
        xh, zh = float(j["origin_mm"][0]), float(j["origin_mm"][2])
        p = L["p_elevon_MPa"]

        def planform(b):
            P, T = m.body_mesh(b)
            n = np.cross(P[T[:, 1]] - P[T[:, 0]], P[T[:, 2]] - P[T[:, 0]]) * 0.5
            low = n[:, 2] < -0.5 * np.linalg.norm(n, axis=1)
            A = -n[low, 2]
            xc = P[T[low]].mean(1)[:, 0]
            return float(A.sum()), float((A * xc).sum() / max(A.sum(), 1e-9)), float(np.ptp(P[:, 1]))
        Ai, xi, si = planform(ein)
        Ao, xo, so = planform(eout)
        out.update(ok=True, x_hinge=xh, z_hinge=zh, A_in=Ai, A_out=Ao, span_in=si, span_out=so,
                   N_in=p * Ai, N_out=p * Ao, H_in=p * Ai * (xi - xh), H_out=p * Ao * (xo - xh))
        if horn and clevis:
            Ph, Pc = m.body_mesh(horn)[0], m.body_mesh(clevis)[0]
            d, k = cKDTree(Ph).query(Pc)
            pc = Pc[int(np.argmin(d))]
            out["horn_point_mm"] = pc.tolist()
            out["r_horn"] = float(math.hypot(pc[0] - xh, pc[2] - zh))
            out["F_push"] = (out["H_in"] + out["H_out"]) / max(out["r_horn"], 1e-6)
            if rod is not None:
                Pr = m.body_mesh(rod)[0]
                c0 = Pr.mean(0)
                ax = np.linalg.svd(Pr - c0, full_matrices=False)[2][0]
                if arm is not None:
                    Pa = m.body_mesh(arm)[0]
                    d2, _ = cKDTree(Pa).query(Pr)
                    pa = Pr[int(np.argmin(d2))]
                    out["arm_point_mm"] = pa.tolist()
                    if np.dot(pa - pc, ax) < 0:
                        ax = -ax
                    if js:
                        o, a = np.asarray(js["origin_mm"], float), np.asarray(js["axis"], float)
                        rv = pa - o
                        out["r_servo_arm"] = float(np.linalg.norm(rv - np.dot(rv, a) * a))
                        out["servo_torque_Nmm"] = out["F_push"] * out["r_servo_arm"]
                out["push_dir"] = (ax / np.linalg.norm(ax)).tolist()
                ext = np.sort(np.ptp(Pr, axis=0))
                out["rod_d"] = float(ext[0])
                out["rod_L"] = float(m.joints.get("Linkage R", {}).get("rod_length_mm") or ext[-1])
        self._hinge = out
        return out


# ============================================================================================ meshing
def _plate_outline(P, T, weld=1e-3, flat_tol=0.02) -> Optional[dict]:
    """Flat-plate description of a closed CAD body - (normal, thickness, outline loops of the top face) - when the
    body is a prism over a planar outline (two opposite planar faces, face area x thickness = volume); else None."""
    q = np.round(np.asarray(P, float) / weld).astype(np.int64)
    _, idx, inv = np.unique(q, axis=0, return_index=True, return_inverse=True)
    V = np.asarray(P, float)[idx]
    F = inv.ravel()[np.asarray(T)]
    F = F[(F[:, 0] != F[:, 1]) & (F[:, 1] != F[:, 2]) & (F[:, 0] != F[:, 2])]
    n = np.cross(V[F[:, 1]] - V[F[:, 0]], V[F[:, 2]] - V[F[:, 0]])
    A = 0.5 * np.linalg.norm(n, axis=1)
    if not len(A):
        return None
    ok = A > 1e-6 * A.max()
    nh = np.zeros_like(n)
    nh[ok] = n[ok] / (2 * A[ok, None])
    d = nh[np.argmax(A)]

    def lev(g):
        return np.array([V[f] @ d for f in F[g]])
    ga0, gb0 = ok & ((nh @ d) > 0.999), ok & ((nh @ d) < -0.999)
    if not ga0.any() or not gb0.any():
        return None
    top, bot = lev(ga0).max(), lev(gb0).min()
    ga, gb = ga0.copy(), gb0.copy()
    ga[ga0] = np.all(np.abs(lev(ga0) - top) <= flat_tol, axis=1)
    gb[gb0] = np.all(np.abs(lev(gb0) - bot) <= flat_tol, axis=1)
    t = float(top - bot)
    vol = abs(np.einsum("ij,ij->i", V[F[:, 0]], np.cross(V[F[:, 1]], V[F[:, 2]])).sum() / 6)
    if t <= 0 or abs(A[ga].sum() * t - vol) > 0.03 * vol or abs(A[ga].sum() - A[gb].sum()) > 0.02 * A[ga].sum():
        return None
    e = np.concatenate([F[ga][:, [0, 1]], F[ga][:, [1, 2]], F[ga][:, [2, 0]]])
    _, iu, cnt = np.unique(np.sort(e, axis=1), axis=0, return_index=True, return_counts=True)
    nxt: dict = {}
    for a, b in e[iu[cnt == 1]]:
        nxt.setdefault(int(a), []).append(int(b))
    if any(len(v) != 1 for v in nxt.values()):
        return None  # the outline touches itself
    e1 = np.cross(d, [1.0, 0, 0] if abs(d[0]) < 0.9 else [0, 1.0, 0])
    e1 /= np.linalg.norm(e1)
    e2 = np.cross(d, e1)

    def area2d(L):
        x, y = L @ e1, L @ e2
        return 0.5 * abs(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))
    loops, seen = [], set()
    for s in list(nxt):
        if s in seen:
            continue
        lp, c = [], s
        while c not in seen:
            seen.add(c)
            lp.append(c)
            c = nxt[c][0]
        loops.append(V[lp])
    loops = [L for L in loops if len(L) >= 3 and area2d(L) > 1e-3]
    loops.sort(key=area2d, reverse=True)

    def inside(pt, L):  # ray casting in the plane of the face
        x, y = L @ e1, L @ e2
        px, py = pt @ e1, pt @ e2
        c = False
        for i in range(len(x)):
            j = i - 1
            if (y[i] > py) != (y[j] > py) and px < (x[j] - x[i]) * (py - y[i]) / (y[j] - y[i] + 1e-300) + x[i]:
                c = not c
        return c
    depth = [sum(inside(L[0], O) for O in loops if O is not L) for L in loops]
    faces = []
    for L, dp in zip(loops, depth):
        if dp % 2 == 0:
            faces.append([L])
    for L, dp in zip(loops, depth):
        if dp % 2 == 1:
            host = next((f for f in faces if inside(L[0], f[0])), None)
            if host is None:
                return None
            host.append(L)
    return {"normal": d, "thickness": t, "faces": faces, "volume": float(vol)} if faces else None


def _mesh_plate(desc: dict, size: float):
    """Quadratic tetra mesh (C3D10 node order) of a flat plate: a planar OCC face from the outline loops, extruded
    by the thickness (gmsh OpenCASCADE kernel)."""
    import gmsh
    crash.gmsh_initialize(interruptible=False)
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.model.add("plate")
        occ = gmsh.model.occ
        surfs = []
        for face in desc["faces"]:
            cl = []
            for L in face:
                keep = []
                for i in range(len(L)):  # drop collinear outline points
                    a, b, c = L[i - 1], L[i], L[(i + 1) % len(L)]
                    u, w = b - a, c - b
                    if np.linalg.norm(np.cross(u, w)) > 1e-6 * np.linalg.norm(u) * np.linalg.norm(w) + 1e-12:
                        keep.append(b)
                pts = [occ.addPoint(*map(float, p)) for p in keep]
                cl.append(occ.addCurveLoop([occ.addLine(pts[i], pts[(i + 1) % len(pts)]) for i in range(len(pts))]))
            surfs.append((2, occ.addPlaneSurface(cl)))
        occ.extrude(surfs, *map(float, -desc["thickness"] * desc["normal"]))
        occ.synchronize()
        return _occ_tets(size)
    finally:
        gmsh.finalize()


def _mesh_rod(fit: dict, r: float, size: float):
    """Quadratic tetra mesh of a solid cylinder (radius r) on a fitted rod axis (gmsh OpenCASCADE kernel)."""
    import gmsh
    crash.gmsh_initialize(interruptible=False)
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.model.add("rod")
        base = fit["centre"] + fit["axis"] * fit["s_min"]
        gmsh.model.occ.addCylinder(*map(float, base), *map(float, fit["axis"] * fit["length"]), float(r))
        gmsh.model.occ.synchronize()
        gmsh.option.setNumber("Mesh.MeshSizeFromCurvature", 7)  # about 7 curved elements around the circumference
        return _occ_tets(size, curved=True)
    finally:
        gmsh.finalize()


def _occ_tets(size: float, curved: bool = False):
    """Mesh the current gmsh model (OCC geometry) with quadratic tetrahedra; nodes / C3D10 connectivity.
    ``curved``: mid-side nodes on the true (curved) geometry instead of on straight edges."""
    import gmsh
    gmsh.option.setNumber("Mesh.MeshSizeMax", size)
    gmsh.option.setNumber("Mesh.MeshSizeMin", size / 4)
    gmsh.option.setNumber("Mesh.ElementOrder", 2)
    gmsh.option.setNumber("Mesh.SecondOrderLinear", 0 if curved else 1)
    gmsh.option.setNumber("Mesh.Optimize", 1)
    gmsh.model.mesh.generate(3)
    tags, xyz, _ = gmsh.model.mesh.getNodes()
    nodes = xyz.reshape(-1, 3)
    idx = {int(t): i for i, t in enumerate(tags)}
    et, _, en = gmsh.model.mesh.getElements(3)
    k = [int(t) for t in et].index(11)
    el = np.array([idx[int(t)] for t in en[k]]).reshape(-1, 10)
    el = el[:, [0, 1, 2, 3, 4, 5, 6, 7, 9, 8]]  # gmsh -> CalculiX C3D10 edge order
    a, b, c, d = (nodes[el[:, i]] for i in range(4))
    neg = np.einsum("ij,ij->i", np.cross(b - a, c - a), d - a) < 0
    if neg.any():
        el[neg] = el[neg][:, [0, 2, 1, 3, 6, 5, 4, 7, 9, 8]]
    used = np.unique(el)
    remap = -np.ones(len(nodes), int)
    remap[used] = np.arange(len(used))
    return nodes[used], remap[el]


def _mesh_volume(nodes, el) -> float:
    a, b, c, d = (nodes[el[:, i]] for i in range(4))
    return float(np.abs(np.einsum("ij,ij->i", np.cross(b - a, c - a), d - a)).sum() / 6)


class MeshRejected(RuntimeError):
    pass


def _mesh_part(m, body, size: float, wd: Path) -> tuple:
    """Tetra mesh of one CAD body: gmsh remesh of the CAD surface (field_analyses._tet_mesh), and for flat plates a
    solid rebuilt from the outline; the candidate with the fewest poorly shaped elements is used. Raises
    MeshRejected (with the reasons) when no candidate is acceptable - a bad mesh is never solved."""
    tried, cands = [], []
    try:
        n1, e1 = FA._tet_mesh(m, [body], size, wd)
        cands.append(("gmsh remesh of the CAD surface (or its facets when the remesh is refused)", n1, e1))
    except Exception as exc:
        tried.append(f"gmsh tetra mesh of the CAD surface failed: {exc}")
    P, T = m.body_mesh(body)
    desc = _plate_outline(P, T)
    if desc is not None:
        try:
            n2, e2 = _mesh_plate(desc, size)
            cands.append((f"flat plate rebuilt from the CAD outline ({desc['thickness']:.2f} mm, extruded)", n2, e2))
        except Exception as exc:
            tried.append(f"flat-plate rebuild failed: {exc}")
    if body.closed and body.volume > 0:
        from opencae.engineering.component_analysis import fit_tube
        fit = fit_tube(m, body)
        solid = body.volume > 0.6 * math.pi * fit["r_out"] ** 2 * fit["length"]
        if fit["pca_ratio"] < 0.05 and solid and fit["length"] > 20 * fit["r_out"]:
            r_eq = math.sqrt(body.volume / (math.pi * fit["length"]))
            try:
                n3, e3 = _mesh_rod(fit, r_eq, size)
                cands.append((f"solid rod rebuilt on the fitted axis: length {fit['length']:.1f} mm, diameter "
                              f"{2 * r_eq:.2f} mm (CAD volume equivalent)", n3, e3))
            except Exception as exc:
                tried.append(f"rod rebuild failed: {exc}")
    best = None
    for how, nodes, el in cands:
        vol = _mesh_volume(nodes, el)
        # the rebuilt rod has the CAD volume by construction (its curved elements are understated by the
        # corner-tetrahedra volume used here, so the test is skipped for it)
        if body.volume > 0 and not how.startswith("solid rod") and abs(vol / body.volume - 1) > 0.03:
            tried.append(f"{how}: mesh volume {vol:.0f} mm³ vs CAD {body.volume:.0f} mm³ (rejected)")
            continue
        if len(el) > MAX_ELEMENTS:
            tried.append(f"{how}: {len(el)} elements (> {MAX_ELEMENTS}, too large for a screening run)")
            continue
        q = FA.tet_mesh_quality(nodes, el, size)
        if q["poor_pct"] > POOR_LIMIT_PCT:
            tried.append(f"{how}: {q['poor_pct']:.0f} % of the elements poorly shaped (minSICN < {FA.TET_POOR_SICN}; "
                         f"limit {POOR_LIMIT_PCT:.0f} %): the CAD tessellation has sliver facets")
            continue
        if best is None or q["poor_pct"] < best[3]["poor_pct"]:
            best = (how, nodes, el, q)
    if best is None:
        raise MeshRejected("; ".join(tried) or "no mesh candidate")
    return best


# ============================================================================================ FE helpers
_FACES = ((0, 1, 2, 3), (0, 3, 1, 2), (1, 3, 2, 0), (2, 3, 0, 1))  # corners of each face + the opposite corner
_MID = {(0, 1): 4, (1, 2): 5, (0, 2): 6, (0, 3): 7, (1, 3): 8, (2, 3): 9}  # C3D10 edge -> mid-side node


def _boundary_faces(nodes, el) -> dict:
    """Boundary faces of a C3D10 mesh: corner and mid-side nodes, area, outward unit normal."""
    allf = np.concatenate([np.sort(el[:, list(f[:3])], axis=1) for f in _FACES])
    _, inv, cnt = np.unique(allf, axis=0, return_inverse=True, return_counts=True)
    bnd = cnt[inv.ravel()] == 1
    E = np.tile(np.arange(len(el)), 4)[bnd]
    Fi = np.repeat(np.arange(4), len(el))[bnd]
    tri = np.empty((len(E), 3), int)
    mid = np.empty((len(E), 3), int)
    opp = np.empty(len(E), int)
    for f, (a, b, c, d) in enumerate(_FACES):
        s = Fi == f
        tri[s] = el[E[s]][:, [a, b, c]]
        mid[s] = el[E[s]][:, [_MID[tuple(sorted(p))] for p in ((a, b), (b, c), (a, c))]]
        opp[s] = el[E[s], d]
    P = nodes
    n = np.cross(P[tri[:, 1]] - P[tri[:, 0]], P[tri[:, 2]] - P[tri[:, 0]])
    A = 0.5 * np.linalg.norm(n, axis=1)
    nh = n / np.maximum(2 * A[:, None], 1e-30)
    inward = np.einsum("ij,ij->i", nh, P[opp] - P[tri[:, 0]]) > 0
    nh[inward] *= -1
    return {"tri": tri, "mid": mid, "area": A, "normal": nh}


def _distribute(nodes, faces, nodeset, F, M=None, point=None) -> dict:
    """Consistent nodal forces of a resultant force F (and moment M about ``point``, default the patch centroid) over
    the surface patch ``nodeset``: uniform traction on the complete faces (6-node triangles: 1/3 of the face force
    at each mid-side node), rigid-patch distribution of the moment."""
    ns = np.asarray(sorted(set(int(i) for i in nodeset)), int)
    F = np.asarray(F, float)
    if not len(ns):
        return {}
    inset = np.isin(faces["tri"], ns).all(1)
    if inset.any():
        ids = faces["mid"][inset].ravel()
        w = np.repeat(faces["area"][inset] / 3.0, 3)
        u, inv = np.unique(ids, return_inverse=True)
        ww = np.zeros(len(u))
        np.add.at(ww, inv, w)
        ids, w = u, ww
    else:
        ids, w = ns, np.ones(len(ns))
    w = w / w.sum()
    X = nodes[ids]
    c = (w[:, None] * X).sum(0)
    f = w[:, None] * F[None, :]
    Mt = np.zeros(3) if M is None else np.asarray(M, float)
    if point is not None:
        Mt = Mt + np.cross(np.asarray(point, float) - c, F)
    if np.linalg.norm(Mt) > 0:
        r = X - c
        J = (w[:, None, None] * (np.einsum("ij,ij->i", r, r)[:, None, None] * np.eye(3)[None] -
                                 np.einsum("ij,ik->ijk", r, r))).sum(0)
        om = np.linalg.lstsq(J, Mt, rcond=None)[0]
        f = f + w[:, None] * np.cross(om[None, :], r)
    return {int(i): v for i, v in zip(ids, f)}


def _grav_nodal(nodes, el, rho: float, a: np.ndarray) -> np.ndarray:
    """Consistent nodal loads of a uniform body force on C3D10 elements (corners -1/20, mid-sides +1/5)."""
    vol = np.abs(np.einsum("ij,ij->i", np.cross(nodes[el[:, 1]] - nodes[el[:, 0]], nodes[el[:, 2]] - nodes[el[:, 0]]),
                           nodes[el[:, 3]] - nodes[el[:, 0]])) / 6
    fe = rho * vol[:, None] * a[None, :]
    out = np.zeros((len(nodes), 3))
    for k in range(4):
        np.add.at(out, el[:, k], -fe / 20)
    for k in range(4, 10):
        np.add.at(out, el[:, k], fe / 5)
    return out


_BARY = np.array([[1 / 3, 1 / 3, 1 / 3], [2 / 3, 1 / 6, 1 / 6], [1 / 6, 2 / 3, 1 / 6], [1 / 6, 1 / 6, 2 / 3]])


def _seg_dist(X, a, b):
    ab = b - a
    t = np.clip(((X - a) @ ab) / max(float(ab @ ab), 1e-30), 0.0, 1.0)
    return np.linalg.norm(X - (a + t[:, None] * ab), axis=1)


def _surface_distance(P, T, X, tol: float, inside_test: bool) -> tuple:
    """Exact unsigned distance (capped at tol + 1) from points X to a triangle surface (P, T), and for a closed
    surface whether each point lies inside it (ray parity). NumPy only: a triangle at a time, points pre-filtered
    by the triangle's bounding box (VTK's implicit-distance filter crashed this process intermittently)."""
    d = np.full(len(X), tol + 1.0)
    hits = np.zeros(len(X), int)
    ray = np.array([1.0, 1.37e-4, 2.71e-4])
    ray /= np.linalg.norm(ray)
    if not len(X):
        return d, np.zeros(0, bool)
    T = np.asarray(T)
    tlo, thi = P[T].min(1), P[T].max(1)
    xlo, xhi = X.min(0), X.max(0)
    near = np.all((tlo <= xhi + tol) & (thi >= xlo - tol), axis=1)  # triangles that can be within tol
    if inside_test:  # triangles a +x ray from some point can cross
        near |= (tlo[:, 1] <= xhi[1] + 1) & (thi[:, 1] >= xlo[1] - 1) & (tlo[:, 2] <= xhi[2] + 1) \
            & (thi[:, 2] >= xlo[2] - 1) & (thi[:, 0] >= xlo[0])  # 1 mm: the ray is slightly skewed
    for t in T[near]:
        A, B, C = P[t[0]], P[t[1]], P[t[2]]
        lo, hi = np.minimum(np.minimum(A, B), C) - tol, np.maximum(np.maximum(A, B), C) + tol
        k = np.nonzero(np.all((X >= lo) & (X <= hi), axis=1))[0]
        if len(k):
            Y = X[k]
            n = np.cross(B - A, C - A)
            nn = float(np.linalg.norm(n))
            dist = np.minimum(np.minimum(_seg_dist(Y, A, B), _seg_dist(Y, B, C)), _seg_dist(Y, C, A))
            if nn > 1e-14:
                nh = n / nn
                dp = (Y - A) @ nh
                Q = Y - dp[:, None] * nh
                v0, v1, v2 = B - A, C - A, Q - A
                d00, d01, d11 = v0 @ v0, v0 @ v1, v1 @ v1
                d20, d21 = v2 @ v0, v2 @ v1
                den = d00 * d11 - d01 * d01
                v = (d11 * d20 - d01 * d21) / den
                w = (d00 * d21 - d01 * d20) / den
                ins = (v >= 0) & (w >= 0) & (v + w <= 1)
                dist = np.where(ins, np.abs(dp), dist)
            d[k] = np.minimum(d[k], dist)
        if inside_test:  # Moller-Trumbore along a fixed, slightly skewed +x ray
            m_ = np.nonzero((X[:, 1] >= min(A[1], B[1], C[1]) - 1) & (X[:, 1] <= max(A[1], B[1], C[1]) + 1)
                            & (X[:, 2] >= min(A[2], B[2], C[2]) - 1) & (X[:, 2] <= max(A[2], B[2], C[2]) + 1)
                            & (X[:, 0] <= max(A[0], B[0], C[0]) + 1e-9))[0]  # prefilter; the exact test follows
            if len(m_):
                e1, e2 = B - A, C - A
                pv_ = np.cross(ray, e2)
                det = float(e1 @ pv_)
                if abs(det) > 1e-14:
                    s = X[m_] - A
                    u = (s @ pv_) / det
                    q = np.cross(s, e1)
                    vv = (q @ ray) / det
                    tt = (q @ e2) / det
                    hits[m_] += ((u >= 0) & (vv >= 0) & (u + vv <= 1) & (tt > 0)).astype(int)
    return d, (hits % 2 == 1) if inside_test else np.zeros(len(X), bool)


def _contact_nodes(ctx: _Ctx, nb, X, ids) -> np.ndarray:
    """Surface nodes (ids into X) within CONTACT_TOL of body ``nb`` or inside it."""
    P, T = ctx.m.body_mesh(nb)
    lo, hi = P.min(0) - CONTACT_TOL, P.max(0) + CONTACT_TOL
    sel = ids[np.all((X[ids] >= lo) & (X[ids] <= hi), axis=1)]
    if not len(sel):
        return sel
    d, ins = _surface_distance(P, T, X[sel], CONTACT_TOL, bool(nb.closed))
    return sel[(d <= CONTACT_TOL) | ins]


def _where(b, cond: Optional[str]) -> bool:
    if cond == "y<0":
        return b.centroid[1] < 0
    if cond == "y>0":
        return b.centroid[1] > 0
    return True


def _select(nbs, items) -> list:
    """Neighbour bodies matching prefixes; an item may be (prefix, 'y<0' | 'y>0')."""
    out = []
    for it in items:
        pre, cond = (it, None) if isinstance(it, str) else it
        out += [b for b in nbs if b.component.startswith(pre) and _where(b, cond) and b not in out]
    return out


# ============================================================================================ one FE screening run
def _run_fea(ctx: _Ctx, spec: dict, body, wd: Path) -> dict:
    """Mesh, load, solve and post-process one part. Returns {'check', 'record', 'reactions', ...}."""
    m, L = ctx.m, ctx.loads
    mat = ctx.mats[spec["material"]]
    wd.mkdir(parents=True, exist_ok=True)
    t_start = time.time()
    how, nodes, el, mq = _mesh_part(m, body, spec["size"], wd)
    faces = _boundary_faces(nodes, el)
    surf_ids = np.unique(np.concatenate([faces["tri"].ravel(), faces["mid"].ravel()]))
    tri = FA._surface(nodes, el)
    nbs = ctx.neighbours(body)
    contact = {b.index: _contact_nodes(ctx, b, nodes, surf_ids) for b in nbs}
    contact = {k: v for k, v in contact.items() if len(v)}
    nb_found = [b for b in nbs if b.index in contact]
    vol = _mesh_volume(nodes, el)
    own = float(ctx.bm.get(body.index, 0.0))
    smeared_m, _ = ctx.mass(spec.get("smeared", ()), exclude={body.index}) if spec.get("smeared") else (0.0, [])
    rho = (own + smeared_m) / vol * 1e-3  # t/mm^3
    notes = list(spec.get("notes", []))
    steps = []
    for cname, case in spec["cases"].items():
        sup_b = _select(nb_found, case["supports"])
        fixed = np.unique(np.concatenate([contact[b.index] for b in sup_b])) if sup_b else np.zeros(0, int)
        sup_note = ""
        if len(fixed) < 3:  # fall back to the faces nearest the attachment
            want = [b for b in ctx.active if b.index != body.index and _select([b], case["supports"])]
            if not want:
                raise RuntimeError(f"case '{cname}': no support body ({case['supports']}) in this configuration")
            d = np.min([_surface_distance(*m.body_mesh(b), nodes[surf_ids], 50.0, False)[0] for b in want], axis=0)
            if d.min() > 50.0:
                raise RuntimeError(f"case '{cname}': support bodies ({case['supports']}) are more than 50 mm away")
            fixed = surf_ids[d <= d.min() + 1.0]
            sup_b = want
            sup_note = (f"no contact (<= {CONTACT_TOL} mm) found with {', '.join(_first(b.component) for b in want[:3])}: "
                        f"fixed the surface nearest to it (within 1 mm of the closest point, {d.min():.2f} mm away)")
            notes.append(f"{cname}: {sup_note}")
        cload: dict = defaultdict(lambda: np.zeros(3))
        texts = []

        def add(fd):
            for i, v in fd.items():
                cload[i] = cload[i] + v
        acc, adir = ctx.inertia(case.get("inertia"))
        avec = np.zeros(3) if adir is None else acc * adir
        # carried masses through their contact
        assigned: set = set(sup.index for sup in sup_b) | {body.index}
        for cr in spec.get("carried", []):
            if adir is None:
                break
            at = [b for b in _select(nb_found, cr["at"]) if b not in sup_b]
            if not at:
                continue
            mass, idx = ctx.mass(cr["masses"], near=at, reach=cr["reach"], exclude=assigned)
            assigned |= set(idx)
            if mass <= 0:
                continue
            ns = np.unique(np.concatenate([contact[b.index] for b in at]))
            F = mass * acc * adir * 1e-3  # kg -> t
            add(_distribute(nodes, faces, ns, F))
            texts.append(f"{mass * 1000:.0f} g carried through {', '.join(sorted({_first(b.component) for b in at}))}")
        # pressure
        if case.get("pressure"):
            sel, key = case["pressure"]
            p = {"wing": L["p_wing_MPa"], "elevon": L["p_elevon_MPa"], "fin": L["p_ctrl_MPa"]}[key]
            ax = {"-z": (2, -1), "+y": (1, 1), "-y": (1, -1), "+z": (2, 1)}[sel]
            on = faces["normal"][:, ax[0]] * ax[1] > 0.5
            fF = -p * faces["area"][on, None] * faces["normal"][on]
            for k3 in range(3):
                for row, mid_ in zip(fF / 3.0, faces["mid"][on][:, k3]):
                    cload[int(mid_)] = cload[int(mid_)] + row
            texts.append(f"pressure {p * 1e6:.0f} Pa on the {sel} faces (resultant {np.linalg.norm(fF.sum(0)):.1f} N)")
        for ld in case.get("loads", []):
            fd, txt = _special_load(ctx, spec, body, nodes, faces, surf_ids, nb_found, contact, ld, cname)
            add(fd)
            if txt:
                texts.append(txt)
        if adir is not None:
            texts.insert(0, f"inertia {acc / G_MM:.1f} g along {'+x' if adir[0] > 0 else '-z'} on "
                            f"{(own + smeared_m) * 1000:.1f} g" + (f" (incl. {smeared_m * 1000:.0f} g smeared)"
                                                                  if smeared_m else ""))
        steps.append({"name": cname, "fixed": fixed, "cload": dict(cload), "avec": avec,
                      "supports": [b.index for b in sup_b], "text": "; ".join(texts), "sup_note": sup_note})
    # ---------------------------------------------------------------- deck
    deck = FA._ccx_deck_header(nodes, el, f"K09-L screening: {spec['id']}")
    for i, st in enumerate(steps):
        deck += FA._nset(f"FIX{i}", st["fixed"])
    if mat["kind"] == "cf":
        o = mat["ortho"]
        P = m.body_mesh(body)[0]
        axis = np.linalg.svd(P - P.mean(0), full_matrices=False)[2][0]
        e1 = np.cross(axis, [1.0, 0, 0] if abs(axis[0]) < 0.9 else [0, 1.0, 0])
        e1 /= np.linalg.norm(e1)
        e2 = np.cross(axis, e1)
        deck += ["*MATERIAL, NAME=M1", "*ELASTIC, TYPE=ENGINEERING CONSTANTS",
                 f"{o['E1']:.6g}, {o['E2']:.6g}, {o['E3']:.6g}, {o['nu12']:.6g}, {o['nu13']:.6g}, {o['nu23']:.6g}, "
                 f"{o['G12']:.6g}, {o['G13']:.6g},", f"{o['G23']:.6g}, 0.0",
                 "*DENSITY", f"{rho:.6e}", "*ORIENTATION, NAME=OR1, SYSTEM=RECTANGULAR",
                 ", ".join(f"{v:.6f}" for v in list(e1) + list(e2)),
                 "*SOLID SECTION, ELSET=EALL, MATERIAL=M1, ORIENTATION=OR1"]
    else:
        axis = None
        deck += ["*MATERIAL, NAME=M1", "*ELASTIC", f"{mat['E']:.6g}, {mat['nu']:.6g}", "*DENSITY", f"{rho:.6e}",
                 "*SOLID SECTION, ELSET=EALL, MATERIAL=M1"]
    for i, st in enumerate(steps):
        deck += ["*STEP", "*STATIC", "*BOUNDARY, OP=NEW", f"FIX{i}, 1, 3, 0.0", "*CLOAD, OP=NEW"]
        for n_, v in sorted(st["cload"].items()):
            for k3 in range(3):
                if abs(v[k3]) > 1e-12:
                    deck.append(f"{n_ + 1}, {k3 + 1}, {v[k3]:.8e}")
        deck.append("*DLOAD, OP=NEW")
        a = st["avec"]
        if np.linalg.norm(a) > 0:
            deck.append(f"EALL, GRAV, {np.linalg.norm(a):.6e}, " + ", ".join(f"{x:.6f}" for x in a / np.linalg.norm(a)))
        deck += ["*NODE FILE", "U, RF", "*EL FILE", "S, E", "*END STEP"]
    (wd / "part.inp").write_text("\n".join(deck) + "\n")
    t0 = time.time()
    rc = FA._run([FA.CCX, "-i", "part"], wd, wd / "ccx.log", env=FA._ccx_env(wd), timeout=900)
    t_solve = time.time() - t0
    if rc != 0 or not (wd / "part.frd").exists():
        raise RuntimeError(f"CalculiX failed (return code {rc}); see {wd / 'ccx.log'}")
    from opencae.solver.calculix.frd import read_frd
    frd = read_frd(wd / "part.frd")
    n = len(nodes)
    Us, Ss, Es, Rs = (FA._read_frd_steps(wd / "part.frd", k, n, frd) for k in ("DISP", "STRESS", "TOSTRAIN", "FORC"))
    if not (len(Us) == len(Ss) == len(steps)):
        raise RuntimeError(f"CalculiX wrote {len(Ss)} stress blocks for {len(steps)} steps; see {wd / 'ccx.log'}")
    # ---------------------------------------------------------------- post-processing per case
    from scipy.spatial import cKDTree
    res_cases, reactions = {}, {}
    for i, st in enumerate(steps):
        S, U = Ss[i], Us[i][:, :3]
        vm = FA._von_mises(S)
        if axis is not None:
            crit_v = np.abs(np.einsum("j,njk,k->n", axis, FA._sym_tensor(S), axis))
        else:
            crit_v = vm
        dist = cKDTree(nodes[st["fixed"]]).query(nodes)[0]
        on_s = np.zeros(n, bool)
        on_s[np.unique(tri)] = True  # surface corner nodes: the nodes the contour field shows
        away = (dist >= spec["size"]) & on_s
        gov_i = int(np.argmax(np.where(away, crit_v, -1.0))) if away.any() else int(np.argmax(np.where(on_s, crit_v, -1)))
        gov, peak = float(crit_v[gov_i]), float(crit_v[on_s].max())
        sf = mat["strength"] / gov if gov > 0 else float("inf")
        # reactions per support body and equilibrium
        fg = _grav_nodal(nodes, el, rho, st["avec"])
        cl = np.zeros((n, 3))
        for n_, v in st["cload"].items():
            cl[n_] += v
        R_node = Rs[i][:, :3] - cl - fg if i < len(Rs) else np.zeros((n, 3))
        applied = cl.sum(0) + fg.sum(0)
        R_tot = R_node[st["fixed"]].sum(0)
        scale = float(np.linalg.norm(cl, axis=1).sum() + np.linalg.norm(fg.sum(0)))  # a pure couple has ~0 resultant
        resid = float(np.linalg.norm(R_tot + applied) / max(scale, 1e-9))
        share = np.zeros(n)  # a node touching two supports gives each half of its reaction
        for b in st["supports"]:
            share[np.asarray(contact.get(b, ()), int)] += 1
        is_fixed = np.zeros(n, bool)
        is_fixed[st["fixed"]] = True
        rb = {}
        for b in st["supports"]:
            ids = np.asarray(contact.get(b, ()), int)
            ids = ids[is_fixed[ids]] if len(ids) else ids
            rb[int(b)] = (R_node[ids] / np.maximum(share[ids], 1)[:, None]).sum(0).tolist() if len(ids) else [0.0] * 3
        reactions[st["name"]] = rb
        res_cases[st["name"]] = {"gov": gov, "peak": peak, "sf": sf, "gov_node": gov_i, "vm": vm, "S": S, "U": U,
                                 "E": Es[i] if i < len(Es) else None, "residual": resid,
                                 "applied_N": applied.tolist(), "reaction_N": R_tot.tolist(),
                                 "max_disp": float(np.linalg.norm(U, axis=1).max())}
    gname = min(res_cases, key=lambda k: res_cases[k]["sf"])
    g = res_cases[gname]
    status = _status(g["sf"], mat["line"])
    sec, strain_src = FA.structural_fields(g["S"], g["U"], g["E"], nu=mat["nu"], E=mat["E"])
    crit = FA._critical(g["vm"], tri, 0.6)
    crit["text"] = f"surface where von Mises >= {crit['threshold']:.3g} MPa (60 % of the peak), {gname} case"
    worst = nodes[g["gov_node"]].round(2).tolist()
    what = "axial (fibre-direction) stress" if axis is not None else "von Mises"
    peak_sf = mat["strength"] / g["peak"] if g["peak"] > 0 else float("inf")
    text = (f"{gname}: {what} {g['gov']:.3g} MPa vs {mat['strength']:.3g} MPa ({mat['source']}) -> SF {g['sf']:.2f} "
            f"(line {mat['line']:g}); peak {g['peak']:.3g} MPa at a support edge (SF {peak_sf:.2f}, rigid-contact "
            f"singularity); max displacement {g['max_disp']:.3g} mm")
    if mat is ctx.mats["xps"] or mat is ctx.mats["depron"]:
        text += f"; against the flexural strength 0.414 MPa the SF would be {0.414 / max(g['gov'], 1e-12):.2f}"
    if mat.get("xy_strength"):
        text += (f"; against the in-layer (XY) strength {mat['xy_strength']:.1f} MPa the SF would be "
                 f"{mat['xy_strength'] / max(g['gov'], 1e-12):.2f} (print orientation not specified)")
    short = _first(body.component)
    cases_txt = "; ".join(f"{st['name']}: {st['text']}" for st in steps)
    assumptions = [f"material: {mat['name']} - {mat['strength_basis']} ({mat['source']})", *notes,
                   f"governing stress read at least one element size ({spec['size']:g} mm) away from the fixed "
                   f"contact nodes (rigid supports make singular peaks at their edges; the peak is reported too)",
                   "linear static, small displacements; contacts idealised as rigid fixed supports or as uniform "
                   "tractions on the contact patch", f"strain field: {strain_src}",
                   "screening loads, not a certified load envelope; not test evidence"]
    evidence = ("simulation on supplier-datasheet material data" if mat["source"] == SUP
                else "simulation on estimated material data")
    rec = FA.field_record(
        f"cs_{spec['id']}", f"{short} - screening ({gname})", "structural", body.component, nodes, tri, g["vm"], "MPa",
        "von Mises stress", critical=crit,
        failure={"criterion": f"{what} vs {mat['name']} strength", "value": g["gov"], "allowable": mat["strength"],
                 "unit": "MPa", "safety_factor": g["sf"], "status": status, "line": mat["line"], "peak": g["peak"],
                 "peak_safety_factor": peak_sf, "text": text},
        solver={"name": "CalculiX ccx", "seconds": round(t_solve, 1), "returncode": rc, "steps": len(steps),
                "equilibrium_residual": {k: v["residual"] for k, v in res_cases.items()},
                "max_displacement_mm": g["max_disp"]},
        mesh_info={"elements": int(len(el)), "nodes": int(len(nodes)), "type": "C3D10", "size_mm": spec["size"],
                   "method": how, **mq},
        load_case=cases_txt, assumptions=assumptions, evidence=evidence,
        extra={"governing_case": gname, "governing_location_mm": worst, "strain_source": strain_src,
               "cases": {k: {"governing_MPa": v["gov"], "peak_MPa": v["peak"], "safety_factor": v["sf"],
                             "max_displacement_mm": v["max_disp"], "equilibrium_residual": v["residual"],
                             "applied_N": v["applied_N"], "reaction_N": v["reaction_N"]} for k, v in res_cases.items()},
               "load_cases": {st["name"]: st["text"] for st in steps},
               "support_notes": [st["sup_note"] for st in steps if st["sup_note"]],
               "neighbours_in_contact": sorted({_first(b.component) for b in nb_found}),
               "material": {k: mat.get(k) for k in ("name", "source", "ref", "strength", "strength_basis", "values")}},
        fields=sec, displacement=g["U"])
    summ = FA.save_field(rec, ctx.fields_dir)
    check = {"kind": "fea", "name": "Linear static FEA screening (CalculiX C3D10)", "primary": True, "status": status,
             "value": _f(g["gov"], 5), "allowable": _f(mat["strength"], 5), "unit": "MPa", "sf": _f(g["sf"], 3),
             "line": mat["line"], "peak_MPa": _f(g["peak"], 5), "peak_sf": _f(peak_sf, 3), "text": text,
             "governing_case": gname, "worst_point_mm": worst, "critical_region": crit["text"],
             "max_displacement_mm": _f(g["max_disp"], 5),
             "cases": {k: {"sf": _f(v["sf"], 3), "governing_MPa": _f(v["gov"], 5), "peak_MPa": _f(v["peak"], 5),
                           "max_displacement_mm": _f(v["max_disp"], 5), "equilibrium_residual": _f(v["residual"], 5)}
                       for k, v in res_cases.items()},
             "load_case": cases_txt, "assumptions": assumptions,
             "mesh": {"elements": int(len(el)), "nodes": int(len(nodes)), "size_mm": spec["size"], "method": how,
                      "poor_pct": _f(mq["poor_pct"], 2), "quality_min": _f(mq["quality"]["min"], 3),
                      "quality_p5": _f(mq["quality"]["p5"], 3)},
             "material": {"name": mat["name"], "source": mat["source"], "ref": mat["ref"],
                          "strength_MPa": mat["strength"], "basis": mat["strength_basis"]},
             "field_id": summ["id"], "body": int(body.index), "seconds": round(time.time() - t_start, 1)}
    return {"check": check, "field": summ, "reactions": reactions, "status": status}


def _special_load(ctx, spec, body, nodes, faces, surf_ids, nbs, contact, ld, cname) -> tuple:
    """Nodal forces of a named load of a case (ground contact, thrust, pushrod, torque, chained reaction, ...)."""
    m, L = ctx.m, ctx.loads
    typ = ld["type"]
    at = _select(nbs, ld.get("at", ()))
    ns = np.unique(np.concatenate([contact[b.index] for b in at])) if at else np.zeros(0, int)
    if typ == "ground":
        land = m.option("landing")
        feet = [b for b in at] or [b for b in nbs if b.component == land.bom_component]
        F = np.array([-L["F_touch"], 0.0, 0.0])
        if feet:
            foot = max(feet, key=lambda b: b.centroid[2])
            return (_distribute(nodes, faces, contact[foot.index], F),
                    f"touchdown force {L['F_touch']:.0f} N (-x) through the {foot.name or 'foot'} contact")
        tip = surf_ids[(nodes[surf_ids, 0] >= nodes[:, 0].max() - 1.0) & (nodes[surf_ids, 2] > 0)]
        return (_distribute(nodes, faces, tip, F),
                f"touchdown force {L['F_touch']:.0f} N (-x) on the upper fin tip (no foot in contact)")
    if typ == "tube_thrust":
        if not len(ns):
            return {}, "no motor-tube contact found: thrust not applied"
        tube = at[0]
        js = [j for j in m.joints.values() if j["name"].startswith("Prop ")
              and abs(j["origin_mm"][1] - tube.centroid[1]) < 40]
        F = sum((L["T_motor"] * np.asarray(j["axis"], float) for j in js), np.zeros(3))
        return _distribute(nodes, faces, ns, F), f"thrust of {len(js)} motor(s) {np.linalg.norm(F):.1f} N via the motor tube"
    if typ == "motor":
        if not len(ns):
            return {}, "no motor contact found: thrust not applied"
        c0 = body.centroid
        j = min((j for j in m.joints.values() if j["name"].startswith("Prop ")),
                key=lambda jj: float(np.linalg.norm(np.asarray(jj["origin_mm"]) - c0)))
        ax = np.asarray(j["axis"], float)
        ax /= np.linalg.norm(ax)
        ccw = "CCW" in j["name"]
        tdir = -(ax if ccw else -ax)
        F = L["T_motor"] * ax
        return (_distribute(nodes, faces, ns, F, M=L["Q_motor_Nmm"] * tdir, point=j["origin_mm"]),
                f"{j['name'].split()[1]} thrust {L['T_motor']:.1f} N at the propeller joint + reaction torque "
                f"{L['Q_motor_Nmm']:.0f} N mm")
    if typ == "pushrod":
        h = ctx.hinge()
        if not h.get("F_push") or not len(ns):
            return {}, "pushrod force not available (linkage geometry missing)"
        F = h["F_push"] * np.asarray(h["push_dir"])  # push_dir points from the horn to the servo arm: tension
        return _distribute(nodes, faces, ns, F), f"pushrod force {h['F_push']:.1f} N at the clevis"
    if typ == "rod_torque":
        h = ctx.hinge()
        if not h.get("ok") or not len(ns):
            return {}, "hinge moment not available"
        P = m.body_mesh(body)[0]
        axis = np.linalg.svd(P - P.mean(0), full_matrices=False)[2][0]
        Mv = abs(h["H_out"]) * axis
        return _distribute(nodes, faces, ns, np.zeros(3), M=Mv), f"torque {abs(h['H_out']):.0f} N mm (outboard hinge moment)"
    if typ == "bearing":
        h = ctx.hinge()
        if not h.get("ok") or not len(ns):
            return {}, "elevon normal force not available"
        F = np.array([0.0, 0.0, 0.5 * h["N_out"]])
        return _distribute(nodes, faces, ns, F), f"half the outboard elevon normal force {0.5 * h['N_out']:.2f} N (+z)"
    if typ == "rod_inertia":
        rod = at[0] if at else None
        mass = ctx.bm.get(rod.index, 0.0) if rod is not None else 0.0
        if not len(ns) or not L["n_land"]:
            return {}, ""
        F = np.array([mass * L["n_land"] * G_MM * 1e-3, 0.0, 0.0])
        return _distribute(nodes, faces, ns, F), f"torque-rod inertia {np.linalg.norm(F):.2f} N"
    if typ == "spar_root":
        if not len(ns):
            return {}, "no spar contact: root load not applied"
        Lh = N_AERO * L["W"] / 2.0
        X = nodes[ns]
        pt = np.array([X[:, 0].mean(), float(ctx.r["span"].value) * 1000 / 4.0, X[:, 2].mean()])
        return (_distribute(nodes, faces, ns, np.array([0.0, 0.0, Lh]), point=pt),
                f"right half-wing lift {Lh:.1f} N at y = {pt[1]:.0f} mm (root moment {Lh * (pt[1] - X[:, 1].mean()):.0f} "
                f"N mm at the overlap)")
    if typ == "chain":
        src = ctx.runs.get(ld["source"])
        R = None
        if src is not None and cname in src["reactions"]:
            rd = src["reactions"][cname]
            if ld.get("via"):
                R = sum((np.asarray(v) for k, v in rd.items() if _match(m.bodies[int(k)].component, ld["via"])), np.zeros(3))
            elif body.index in rd or str(body.index) in rd:
                R = np.asarray(rd.get(body.index, rd.get(str(body.index))))
        if R is None:
            if ld["source"] == "fin" and cname == "landing" and len(ns):
                n_clips = 4
                F = np.array([-L["F_touch"] / n_clips, 0.0, 0.0])
                return (_distribute(nodes, faces, ns, F),
                        f"fin run unavailable: touchdown force / {n_clips} clips = {L['F_touch'] / n_clips:.0f} N (-x)")
            return {}, f"no reaction from the {ld['source']} run for this case: not applied"
        if not len(ns):
            return {}, f"no contact with {ld.get('at')}: chained load not applied"
        return _distribute(nodes, faces, ns, -R), f"{np.linalg.norm(R):.1f} N from the {ld['source']} run (reaction passed on)"
    raise ValueError(f"unknown load type {typ}")


_SUBGRID: dict = {}


def _subgrid(k: int) -> np.ndarray:
    """Barycentric centroids of the k x k uniform subdivision of a triangle (k^2 sub-triangles of equal area)."""
    if k not in _SUBGRID:
        up = [((i + 1 / 3) / k, (j + 1 / 3) / k) for i in range(k) for j in range(k - i)]
        dn = [((i + 2 / 3) / k, (j + 2 / 3) / k) for i in range(k) for j in range(k - i - 1)]
        uv = np.array(up + dn)
        _SUBGRID[k] = np.column_stack([1 - uv.sum(1), uv])
    return _SUBGRID[k]


def _contact_area(ctx: _Ctx, part, nb, h: float = 3.0) -> float:
    """Area (mm²) of the surface of ``nb`` within CONTACT_TOL of ``part`` (or inside it): the triangles of nb near
    the part, uniformly subdivided to about h, sub-triangle centroids tested against the part surface."""
    m = ctx.m
    P, T = m.body_mesh(nb)
    lo, hi = ctx.box(part)
    lo, hi = lo - CONTACT_TOL, hi + CONTACT_TOL
    tlo, thi = P[T].min(1), P[T].max(1)
    pts, ws = [], []
    for t in T[np.all((tlo <= hi) & (thi >= lo), axis=1)]:
        V = P[t]
        a = 0.5 * float(np.linalg.norm(np.cross(V[1] - V[0], V[2] - V[0])))
        if a <= 0:
            continue
        L = max(float(np.linalg.norm(V[i] - V[i - 1])) for i in range(3))
        k = int(min(max(1, math.ceil(L / h)), 60))  # long CAD strips: about L / 60
        X = _subgrid(k) @ V
        keep = np.all((X >= lo) & (X <= hi), axis=1)
        if keep.any():
            pts.append(X[keep])
            ws.append(np.full(int(keep.sum()), a / (k * k)))
    if not pts:
        return 0.0
    X, w = np.vstack(pts), np.concatenate(ws)
    d, ins = _surface_distance(*m.body_mesh(part), X, CONTACT_TOL, bool(part.closed))
    return float(w[(d <= CONTACT_TOL) | ins].sum())


def _bearing_fallback(ctx: _Ctx, spec: dict, body, reason: str) -> dict:
    """When a body cannot be meshed acceptably: average stress over the support contact area from the CAD
    triangles, per case (own inertia + carried masses + pressure). Status at best 'watch' (peaks unknown)."""
    m, L = ctx.m, ctx.loads
    mat = ctx.mats[spec["material"]]
    P, T = m.body_mesh(body)
    n = np.cross(P[T[:, 1]] - P[T[:, 0]], P[T[:, 2]] - P[T[:, 0]]) * 0.5
    A = np.linalg.norm(n, axis=1)
    nbs = ctx.neighbours(body)
    own = float(ctx.bm.get(body.index, 0.0))
    sm, _ = ctx.mass(spec.get("smeared", ()), exclude={body.index}) if spec.get("smeared") else (0.0, [])
    cases = {}
    for cname, case in spec["cases"].items():
        sup = _select(nbs, case["supports"])
        if not sup:
            continue
        a_sup = float(sum(_contact_area(ctx, body, b) for b in sup))
        acc, adir = ctx.inertia(case.get("inertia"))
        F = np.zeros(3)
        if adir is not None:
            F += (own + sm) * acc * adir * 1e-3
            ex = {body.index} | {b.index for b in sup}
            for cr in spec.get("carried", []):
                at = [b for b in _select(nbs, cr["at"]) if b not in sup]
                if at:
                    mass, idx = ctx.mass(cr["masses"], near=at, reach=cr["reach"], exclude=ex)
                    ex |= set(idx)
                    F += mass * acc * adir * 1e-3
        if case.get("pressure"):
            sel, key = case["pressure"]
            p = {"wing": L["p_wing_MPa"], "elevon": L["p_elevon_MPa"], "fin": L["p_ctrl_MPa"]}[key]
            ax = {"-z": (2, -1), "+y": (1, 1)}[sel]
            on = n[:, ax[0]] * ax[1] > 0.5 * A
            F += -p * n[on].sum(0)
        s = float(np.linalg.norm(F)) / max(a_sup, 1e-9)
        cases[cname] = {"force_N": float(np.linalg.norm(F)), "support_area_mm2": a_sup, "stress_MPa": s,
                        "sf": mat["strength"] / s if s > 0 else float("inf")}
    if not cases:
        return {"kind": "fea", "name": "Linear static FEA screening", "primary": True, "status": NEV,
                "text": f"not evaluated: {reason}; no support contact found for a fallback check"}
    g = min(cases, key=lambda k: cases[k]["sf"])
    c = cases[g]
    st = _status(c["sf"], mat["line"])
    st = WATCH if st == OK else st
    return {"kind": "contact_stress", "name": "Average contact stress at the supports (FE mesh not possible)",
            "primary": True, "status": st, "value": _f(c["stress_MPa"], 5), "allowable": mat["strength"], "unit": "MPa",
            "sf": _f(c["sf"], 3), "line": mat["line"], "governing_case": g,
            "cases": {k: {kk: _f(vv, 4) for kk, vv in v.items()} for k, v in cases.items()},
            "text": (f"FEA not run: {reason}. Fallback: {g} load {c['force_N']:.1f} N over the "
                     f"{c['support_area_mm2']:.0f} mm² support contact = {c['stress_MPa']:.3g} MPa average vs "
                     f"{mat['strength']:.3g} MPa ({mat['source']}) -> SF {c['sf']:.2f}; an average hides local "
                     f"peaks, so the status is at best 'watch'"),
            "material": {"name": mat["name"], "source": mat["source"], "ref": mat["ref"]},
            "load_case": "; ".join(f"{k}: {L['text'].get(k, k)}" for k in spec["cases"])}


# ============================================================================================ analytical checks
def _rating(name, demand, rating, unit, rating_q=None, demand_src="", line=RATING_LINE, primary=True,
            kind="rating") -> dict:
    if rating is None:
        return {"kind": kind, "name": name, "primary": primary, "status": NEV, "demand": _f(demand, 4), "unit": unit,
                "text": f"not evaluated - rating missing (the model holds no rating for this); demand "
                        f"{demand:.3g} {unit}" + (f" ({demand_src})" if demand_src else "") if demand is not None
                else "not evaluated - rating missing"}
    margin = rating / demand if demand else float("inf")
    st = _status(margin, line)
    src = ""
    if rating_q is not None:
        src = f"{rating_q.source.replace('_', ' ')}: {rating_q.ref}" + (f" ({rating_q.note})" if rating_q.note else "")
    return {"kind": kind, "name": name, "primary": primary, "status": st, "demand": _f(demand, 4),
            "rating": _f(rating, 4), "unit": unit, "margin": _f(margin, 3), "line": line,
            "text": f"{demand:.3g} {unit} vs rating {rating:.3g} {unit}: margin {margin:.2f} (line {line:g})"
                    + (f"; demand: {demand_src}" if demand_src else ""), "source": src}


def _nev(name, why, demand_text="", kind="rating") -> dict:
    return {"kind": kind, "name": name, "primary": True, "status": NEV,
            "text": why + (f"; demand: {demand_text}" if demand_text else "")}


def _awg(m, c) -> Optional[int]:
    s = re.search(r"(\d+)\s*AWG", c.key)
    if s:
        return int(s.group(1))
    for i in c.bodies:
        s = re.search(r"(\d+)\s*AWG", m.bodies[i].name)
        if s:
            return int(s.group(1))
    return None


def _ampacity(ctx, c, cont, burst, what) -> dict:
    awg = _awg(ctx.m, c)
    if awg is None or awg not in AWG_CHASSIS_A:
        return _nev("Wire ampacity vs current", "not evaluated - wire gauge not in the model (name or body names)",
                    f"{what}: {cont:.1f} A continuous, {burst:.1f} A full throttle")
    amp = AWG_CHASSIS_A[awg]
    st = FAIL if cont > amp else (WATCH if burst > amp else OK)
    return {"kind": "ampacity", "name": f"Wire ampacity ({awg} AWG) vs current", "primary": True, "status": st,
            "rating": amp, "demand": _f(burst, 3), "continuous_A": _f(cont, 3), "unit": "A",
            "margin": _f(amp / burst, 3), "source": AWG_REF,
            "text": (f"{what}: {cont:.1f} A continuous (hover) and {burst:.1f} A at full throttle vs {amp:g} A "
                     f"chassis-wiring ampacity of {awg} AWG (continuous rating). "
                     + ("Continuous current exceeds it." if st == FAIL else
                        "The full-throttle current exceeds the continuous rating: acceptable only as a short burst - "
                        "measure the wire temperature on the thrust stand." if st == WATCH else "Within the rating."))}


def _fastener_checks(ctx: _Ctx) -> dict:
    """Fastener stresses under the screening loads, per joint (fastener bodies grouped by the parts they join)."""
    m, L = ctx.m, ctx.loads
    hosts = ctx.fastener_hosts()
    groups: dict = defaultdict(list)
    for bi, hs in hosts.items():
        groups[(m.bodies[bi].component, frozenset(hs))].append(bi)
    out: dict = defaultdict(list)
    joint_status: dict = {}
    n_land = L["n_land"] or 0.0
    g = L["g"]
    fin_run = ctx.runs.get("fin")
    h = ctx.hinge()

    def kind_of(ck):
        f = _first(ck).lower()
        if f.startswith(("washer",)):
            return "washer"
        if f.startswith(("nut", "locknut", "prop locknut")):
            return "nut"
        if f.startswith("standoff"):
            return "standoff"
        return "screw"

    def clusters(bodies):
        cs = [[b] for b in bodies]
        merged = True
        while merged:
            merged = False
            for i in range(len(cs)):
                for j in range(i + 1, len(cs)):
                    if min(np.linalg.norm(m.bodies[a].centroid - m.bodies[b].centroid) for a in cs[i] for b in cs[j]) < 40:
                        cs[i] += cs.pop(j)
                        merged = True
                        break
                if merged:
                    break
        return cs
    order = sorted(groups.items(), key=lambda kv: kind_of(kv[0][0]) != "screw")
    for (ck, hs), bodies in order:
        hl = sorted(hs)
        has = lambda p: any(x.startswith(p) for x in hl)  # noqa: E731
        kd = kind_of(ck)
        cl = clusters(bodies)
        n_per = int(np.median([len(x) for x in cl]))
        host_txt = " + ".join(_first(x) for x in hl) or "no host found"
        name = f"Joint {host_txt}"
        size = re.search(r"M(\d+(?:\.\d+)?)", _first(ck))
        d = float(size.group(1)) if size else None
        As = STRESS_AREA_MM2.get(d) if d else None
        if kd in ("nut", "washer"):
            continue  # judged after the screws of the same joint
        # material
        low = ck.lower()
        if "nylon" in low:
            mat = ctx.mats["nylon66"]
        elif "18-8" in low or " a2" in low:
            mat = ctx.mats["a2_70"]
        else:
            mat = None
        # joint load per joint (tension, shear), N
        Ft = Fs = 0.0
        basis = ""
        if has("Prop ") or has("Prop adapter"):
            out[ck].append(_nev(name, "not evaluated - rating missing (kit part: material and proof load not in the "
                                       "model)", f"propeller thrust {L['T_motor']:.1f} N pulls the prop against the nut, "
                                                 f"torque {L['Q_motor_Nmm']:.0f} N mm", kind="fastener"))
            joint_status[hs] = NEV
            continue
        if has("Motor SunnySky") and has("Motor mount"):
            js = [j for j in m.joints.values() if j["name"].startswith("Prop ")]
            rb = []
            for grp in cl:
                c0 = np.mean([m.bodies[b].centroid for b in grp], axis=0)
                j = min(js, key=lambda jj: float(np.linalg.norm(np.asarray(jj["origin_mm"]) - c0)))
                o, a = np.asarray(j["origin_mm"], float), np.asarray(j["axis"], float)
                for b in grp:
                    r_ = m.bodies[b].centroid - o
                    rb.append(float(np.linalg.norm(r_ - np.dot(r_, a) * a)))
            r_b = float(np.mean(rb)) if rb else 10.0
            Ft, Fs = L["T_motor"], L["Q_motor_Nmm"] / r_b
            basis = (f"thrust {L['T_motor']:.1f} N in tension, reaction torque {L['Q_motor_Nmm']:.0f} N mm as shear at "
                     f"the bolt circle r {r_b:.1f} mm")
        elif has("Motor mount") and len(hl) == 1:
            mset = sum(ctx.unit_mass(p) for p in ("Motor SunnySky", "Prop APC", "Prop adapter collet", "Prop adapter ring",
                                                  "Prop locknut", "Washer M5", "Motor mount"))
            Fs = L["T_motor"] + mset * n_land * g
            basis = (f"clamp of the mount on the tube: thrust {L['T_motor']:.1f} N + motor-set inertia "
                     f"{mset * 1000:.0f} g x {n_land:.1f} g taken in bolt shear (upper bound of a friction joint)")
        elif has("Fin clip"):
            R = None
            if fin_run:
                R = max(sum(float(np.linalg.norm(v)) for k, v in rd.items() if m.bodies[int(k)].component.startswith("Fin clip"))
                        for rd in fin_run["reactions"].values())
                Fs = R / max(len(bodies) / 2, 1)
                basis = (f"fin-clip reactions of the fin run ({R:.0f} N on one fin's clips) over its "
                         f"{len(bodies) // 2} clip screws")
            else:
                Fs = (L["F_touch"] or 0.0) / max(len(bodies) / 2, 1)
                basis = "touchdown force over one fin's clip screws (fin run unavailable)"
        elif has("Foot") or any(x == m.option("landing").bom_component for x in hl):
            Fs = L["F_touch"] or 0.0
            basis = (f"whole touchdown force {Fs:.0f} N on one foot clamp screw (bearing of the foot on the fin end "
                     f"neglected: conservative)")
        elif has("Control horn"):
            Fs = h.get("F_push") or 0.0
            basis = f"pushrod force {Fs:.1f} N into the horn screws"
        elif has("Servo KST"):
            ms = ctx.unit_mass("Servo KST") + ctx.unit_mass("Servo arm")
            Fs = ms * n_land * g + (h.get("F_push") or 0.0)
            basis = f"servo inertia {ms * 1000:.0f} g x {n_land:.1f} g + pushrod reaction {h.get('F_push') or 0:.1f} N"
        elif has("FC CoreWing") and not has("ESC"):
            ms = ctx.unit_mass("FC CoreWing")
            Fs = ms * n_land * g
            basis = f"FC stack inertia {ms * 1000:.0f} g x {n_land:.1f} g"
        elif has("ESC Holybro") or has("ESC deck"):
            ms = sum(ctx.unit_mass(p) for p in ("ESC Holybro", "FC CoreWing", "Adapter plate", "Capacitor"))
            Fs = ms * n_land * g
            basis = f"ESC + adapter + FC + capacitor inertia {ms * 1000:.0f} g x {n_land:.1f} g"
        elif has("Pod hatch"):
            ms = sum(ctx.unit_mass(p) for p in ("Pod hatch", "GPS mast", "GPS Holybro", "Foam tape pad 1 mm, GPS"))
            Fs, Ft = ms * n_land * g, ms * N_AERO * g
            basis = (f"hatch + GPS mast + GPS {ms * 1000:.0f} g: {n_land:.1f} g landing in shear, {N_AERO:g} g pull-up "
                     f"in tension")
        else:
            ms = min((ctx.unit_mass(_first(x)) for x in hl), default=0.0)
            Fs = ms * n_land * g
            basis = f"generic: inertia of the lighter joined part {ms * 1000:.0f} g x {n_land:.1f} g"
        ft, fs = Ft / n_per, Fs / n_per
        if mat is None or As is None:
            out[ck].append(_nev(name, "not evaluated - rating missing (fastener material / size not stated in the "
                                       "model)", f"{ft:.1f} N tension, {fs:.1f} N shear per fastener ({basis})",
                                kind="fastener"))
            joint_status[hs] = NEV
            continue
        if kd == "standoff":  # threaded standoff: same stress-area check
            pass
        sig, tau = ft / As, fs / As
        seq = math.sqrt(sig * sig + 3 * tau * tau)
        sf = mat["strength"] / seq if seq > 0 else float("inf")
        st = _status(sf, mat["line"])
        joint_status[hs] = st
        out[ck].append({"kind": "fastener", "name": name, "primary": True, "status": st, "value": _f(seq, 4),
                        "allowable": mat["strength"], "unit": "MPa", "sf": _f(sf, 3), "line": mat["line"],
                        "per_fastener_N": {"tension": _f(ft, 3), "shear": _f(fs, 3)}, "fasteners_per_joint": n_per,
                        "joints": len(cl),
                        "text": (f"{ft:.1f} N tension + {fs:.1f} N shear per fastener on M{d:g} (A_s {As} mm²): "
                                 f"equivalent {seq:.3g} MPa vs {mat['strength']:g} MPa -> SF {sf:.1f} (line "
                                 f"{mat['line']:g}); load: {basis}. Hole bearing in the joined parts is not checked "
                                 f"here (it is in the parts' FEA where they are screened)"),
                        "material": {"name": mat["name"], "source": mat["source"], "ref": mat["ref"]},
                        "source": STRESS_AREA_REF})
    for (ck, hs), bodies in order:
        kd = kind_of(ck)
        if kd not in ("nut", "washer"):
            continue
        st = joint_status.get(hs)
        host_txt = " + ".join(_first(x) for x in sorted(hs)) or "no host found"
        if st is None:
            out[ck].append(_nev(f"Joint {host_txt}", "not evaluated - no screw check for this joint", kind="fastener"))
        else:
            out[ck].append({"kind": "fastener", "name": f"Joint {host_txt}", "primary": True, "status": st,
                            "text": f"{'nut' if kd == 'nut' else 'washer'} of the joint: follows its screw check "
                                    f"({st}). Basis: a nut of the matching property class is proof-load rated above "
                                    f"the screw (ISO 898-2 / ISO 3506-2 design rule); a washer spreads the bearing"})
    return out


def _mass_check(ctx: _Ctx, c) -> dict:
    m = ctx.m
    q = m.component_mass(c)
    bom = (c.unit_mass.value or 0.0) * c.qty
    mod = (q.value or 0.0) * m.effective_qty(c)
    if c.key in m.spare_keys or c.key.lower().startswith("(spare"):
        return {"kind": "mass", "name": "Mass vs BOM", "primary": False, "status": OK,
                "text": "spare, not installed: excluded from the take-off mass"}
    if bom <= 0 and mod <= 0:
        return {"kind": "mass", "name": "Mass vs BOM", "primary": False, "status": WATCH,
                "text": "the BOM line has no mass: this installed item is missing from the mass model"}
    txt = f"mass model {mod * 1000:.1f} g vs BOM line {bom * 1000:.1f} g ({c.qty} x {c.unit_mass.value * 1000:.2f} g)"
    if abs(mod - bom) > 1e-9:
        txt += f"; differs: {q.note or q.ref or 'configuration'}" + (
            f", {m.effective_qty(c)} of {c.qty} installed" if m.effective_qty(c) != c.qty else "")
    vol = sum(m.bodies[i].volume for i in c.bodies if m.bodies[i].closed)
    if vol > 0 and c.pn.startswith("K09L-") and q.value:
        txt += f"; implied density {q.value / (vol / max(c.qty, 1)) * 1e9:.0f} kg/m³ (unit mass / CAD volume)"
    return {"kind": "mass", "name": "Mass vs BOM", "primary": False, "status": OK, "value": _f(mod * 1000, 3),
            "unit": "g", "text": txt}


def _analytical(ctx: _Ctx, c) -> list:
    """Rating / ampacity / mechanical checks of a component without an FE run (rules on the part name)."""
    m, r, L = ctx.m, ctx.r, ctx.loads
    f = _first(c.key).lower()
    rq = lambda k: r[k].value if k in r and r[k].value is not None else None  # noqa: E731
    h = ctx.hinge()
    land_n = L["n_land"] or 0.0
    if f.startswith("harness") or f.startswith("servo extension"):
        return _wire_rules(ctx, c, f)
    if f.startswith("esc deck"):
        return [_esc_deck(ctx, c)]
    if f.startswith("motor sunnysky"):
        mot = m.option("motor")
        out = [_rating("Full-throttle current vs motor 30 s maximum current", rq("i_motor_max_full"),
                       mot.q("imax").value, "A", mot.q("imax"), "calcs.propulsion i_motor_max_full (provisional prop "
                                                                 "coefficients)"),
               _rating("Full-throttle electrical power vs motor maximum power",
                       mot.q("pmax").value / rq("motor_power_margin") if rq("motor_power_margin") else None,
                       mot.q("pmax").value, "W", mot.q("pmax"), "calcs.propulsion")]
        out.append({"kind": "rating", "name": "Cell count vs motor rating", "primary": True,
                    "status": OK if rq("cells_ok") == 1.0 else FAIL, "text": r["cells_ok"].ref})
        return out
    if f.startswith("esc "):
        esc = m.option("esc")
        dem = max(rq("i_motor_max_full") or 0, m.option("motor").q("imax").value or 0)
        return [_rating("Per-channel current rating vs max(motor rating, full-throttle current)", dem,
                        esc.q("i_cont").value, "A", esc.q("i_cont"), "the ESC must survive the motor's own maximum"),
                {"kind": "rating", "name": "Cell count within the ESC range", "primary": True,
                 "status": OK if rq("cells_ok") == 1.0 else FAIL, "text": r["cells_ok"].ref}]
    if f.startswith("lipo"):
        bat = m.option("battery")
        cmax = bat.q("c_rate").value * bat.q("capacity").value / 3600.0
        return [_rating("Full-throttle battery current vs advertised C rating", rq("i_battery_max"), cmax, "A",
                        bat.q("c_rate"), "calcs.propulsion i_battery_max (ESC efficiency 0.95)"),
                {"kind": "rating", "name": "Cell count vs motor / ESC", "primary": True,
                 "status": OK if rq("cells_ok") == 1.0 else FAIL, "text": r["cells_ok"].ref}]
    if f.startswith("capacitor"):
        v = re.search(r"(\d+(?:\.\d+)?)\s*V\b", c.key)
        vf = m.option("battery").q("v_full")
        ck = _rating("Voltage rating vs full battery voltage", vf.value, float(v.group(1)) if v else None, "V",
                     None, "4.2 V/cell, full pack")
        if v:
            ck["source"] = f"voltage rating from the BOM part name ('{c.key}')"
            ck["text"] += "; ripple-current rating not in the model (not checked)"
        return [ck]
    if f.startswith("connector pair") or "xt60" in f:
        return [_nev("Connector current rating", "not evaluated - rating missing (the model holds no current rating "
                                                 "for the XT60H)",
                     f"{rq('i_hover_battery'):.1f} A hover, {rq('i_battery_max'):.1f} A full throttle")]
    if f.startswith("bullet connector"):
        return [_nev("Connector current rating", "not evaluated - rating missing (no rating for the 3.5 mm bullets in "
                                                 "the model)",
                     f"{rq('i_hover_motor'):.1f} A hover, {rq('i_motor_max_full'):.1f} A full throttle per motor")]
    if f.startswith("fc "):
        return [_nev("Power-board / regulator rating", "not evaluated - rating missing (PDB current and BEC ratings of "
                                                       "the stack are not in the model)",
                     f"battery current up to {rq('i_battery_max'):.1f} A passes the stack's power board")]
    if f.startswith(("receiver", "gps holybro", "airspeed sensor", "usb extender")):
        return [_nev("Electrical rating", "not evaluated: low-power device on the FC's regulated rail - the model "
                                          "holds neither its current draw nor the rail rating, and there is no "
                                          "load-carrying function to screen")]
    if f.startswith("plug jst"):
        return [_nev("Connector rating", "not evaluated: signal connector; no current rating or signal current in the "
                                         "model")]
    if f.startswith(("pitot", "silicone pressure")):
        return [_nev("Screening", "not evaluated: pneumatic line of the airspeed sensor; no structural or electrical "
                                  "rating check is meaningful in this model", kind="none")]
    if f.startswith("servo kst"):
        dem = (f"servo torque {h['servo_torque_Nmm']:.0f} N mm (pushrod {h['F_push']:.1f} N on a {h['r_servo_arm']:.1f} "
               f"mm arm; hinge moments {h['H_in']:.0f} + {h['H_out']:.0f} N mm)" if h.get("servo_torque_Nmm") else "")
        return [_nev("Servo torque vs rating", "not evaluated - rating missing (the KST X10 torque is not in the model)",
                     dem)]
    if f.startswith("servo arm"):
        dem = (f"pushrod force {h['F_push']:.1f} N at {h['r_servo_arm']:.1f} mm: {h['servo_torque_Nmm']:.0f} N mm"
               if h.get("servo_torque_Nmm") else "")
        return [_nev("Servo arm strength", "not evaluated - rating missing (aftermarket aluminium arm: alloy and "
                                           "rating not stated)", dem, kind="mechanical")]
    if f.startswith("clevis"):
        return [_nev("Clevis strength", "not evaluated - rating missing (Du-Bro nylon Kwik-Link: no load rating in the "
                                        "model)", f"pushrod force {h.get('F_push', 0):.1f} N", kind="mechanical")]
    if f.startswith("pushrod"):
        if not h.get("F_push") or not h.get("rod_d"):
            return [_nev("Pushrod tension / buckling", "not evaluated: linkage geometry missing", kind="mechanical")]
        mat = ctx.mats["steel"]
        d, Lr, F = h["rod_d"], h["rod_L"], h["F_push"]
        A, I = math.pi * d * d / 4, math.pi * d ** 4 / 64
        sig = F / A
        pcr = math.pi ** 2 * mat["E"] * I / Lr ** 2
        sf = min(mat["strength"] / sig, pcr / F)
        return [{"kind": "mechanical", "name": "Pushrod stress and Euler buckling", "primary": True,
                 "status": _status(sf, mat["line"]), "sf": _f(sf, 3), "line": mat["line"], "value": _f(sig, 4),
                 "unit": "MPa",
                 "text": (f"pushrod force {F:.1f} N (both elevon hinge moments, control case): {sig:.1f} MPa on "
                          f"d {d:.2f} mm (CAD) vs {mat['strength']:g} MPa; pinned buckling load {pcr:.0f} N over "
                          f"{Lr:.1f} mm -> SF {sf:.1f}"),
                 "material": {"name": mat["name"], "source": mat["source"], "ref": mat["ref"]}}]
    if f.startswith("hinge tape"):
        tot = (h.get("N_in", 0) / max(h.get("span_in", 1), 1)) if h.get("ok") else None
        return [_nev("Hinge tape strength", "not evaluated - rating missing (tape peel / shear strength not in the "
                                            "model)",
                     f"hinge-line load about {tot:.3f} N/mm (inboard elevon normal force over its span)" if tot else "",
                     kind="mechanical")]
    if f.startswith("battery strap"):
        mb = ctx.unit_mass("LiPo")
        return [_nev("Strap holding force", "not evaluated - rating missing (hook-and-loop shear / peel strength not "
                                            "in the model)",
                     f"battery inertia {mb * land_n * L['g']:.0f} N at the {land_n:.1f} g touchdown", kind="mechanical")]
    if f.startswith("foam tape"):
        b = next((ctx.m.bodies[i] for i in c.bodies), None)
        held = {"gps": "GPS Holybro", "rx": "Receiver", "aspd": "Airspeed sensor", "usb": "USB extender"}
        key = next((v for k, v in held.items() if k in f), None)
        mh = ctx.unit_mass(key) if key else 0.0
        area = b.area / 2 if b is not None else None
        dem = f"{mh * land_n * L['g']:.1f} N shear at {land_n:.1f} g" + (
            f" = {mh * land_n * L['g'] / area * 1e3:.1f} kPa over about {area:.0f} mm²" if area else "")
        return [_nev("Adhesive pad holding force", "not evaluated - rating missing (3M foam-tape strength not in the "
                                                   "model; PN not verified)", dem, kind="mechanical")]
    if f.startswith("prop apc"):
        return [_nev("Propeller RPM limit", "not evaluated - rating missing (APC RPM limit not in the model)",
                     f"{rq('rpm_max_full'):.0f} rpm at full throttle", kind="rating")]
    if f.startswith("prop adapter"):
        return [_nev("Adapter torque / thrust capacity", "not evaluated - rating missing (kit part)",
                     f"torque {L['Q_motor_Nmm']:.0f} N mm, thrust {L['T_motor']:.1f} N per motor", kind="mechanical")]
    if "laminating film" in f:
        return [_nev("Screening", "not evaluated: mass-only BOM line without geometry - a 1.5 mil film cannot be "
                                  "meshed as a solid; its skin stiffness is not credited anywhere (conservative)",
                     kind="none")]
    if f.startswith("(consumable)"):
        return [_nev("Screening", "not evaluated: consumable (adhesive) without geometry; bonded joints are idealised "
                                  "as rigid in the part screenings", kind="none")]
    if f.startswith("(spare"):
        return [_nev("Screening", "not evaluated: spare pack, not installed", kind="none")]
    if f.startswith("esc deck"):
        return [_esc_deck(ctx, c)]
    return [_nev("Screening", "not evaluated: no screening rule for this component (add one)", kind="none")]


def _wire_rules(ctx: _Ctx, c, f: str) -> list:
    """Ampacity of the power harnesses; signal / sense / servo leads are not evaluated (no current in the model)."""
    r = ctx.r
    rq = lambda k: r[k].value if k in r and r[k].value is not None else None  # noqa: E731
    if any(k in f for k in ("xt60", "battery pigtail", "pdb")):
        return [_ampacity(ctx, c, rq("i_hover_battery"), rq("i_battery_max"), "battery current")]
    if "harness motor" in f:
        return [_ampacity(ctx, c, rq("i_hover_motor"), rq("i_motor_max_full"),
                          "motor current (DC-side value, conservative for the phase RMS)")]
    if "balance" in f:
        return [_nev("Wire ampacity", "not evaluated: cell-voltage sense / balance lead - no load current in flight; "
                                      "the charger's balance current is not in the model")]
    if "servo" in f:
        return [_nev("Wire ampacity", "not evaluated - servo current draw not in the model (rating missing)")]
    return [_nev("Wire ampacity", "not evaluated: signal harness, no load current in the model")]


def _esc_deck(ctx: _Ctx, c) -> dict:
    """ESC deck: the CAD body is an open surface (no solid to mesh): plate bending / shear by hand."""
    m, L = ctx.m, ctx.loads
    bs = [m.bodies[i] for i in c.bodies]
    if not bs:
        return _nev("Plate check", "not evaluated: no geometry", kind="mechanical")
    P = np.vstack([m.body_mesh(b)[0] for b in bs])
    ext = np.sort(np.ptp(P, axis=0))[::-1]
    t = float(re.search(r"(\d+(?:\.\d+)?)\s*mm", c.key).group(1)) if re.search(r"(\d+(?:\.\d+)?)\s*mm", c.key) else None
    mat = ctx.mats["petg"]
    ms = sum(ctx.unit_mass(p) for p in ("ESC Holybro", "FC CoreWing", "Adapter plate", "Capacitor"))
    if t is None:
        return _nev("Plate check", "not evaluated: open CAD surface and no thickness in the name", kind="mechanical")
    span, b = float(ext[0]), float(ext[1])
    F_out = ms * N_AERO * L["g"]  # out of plane (pull-up, the deck lies in the wing plane)
    F_in = ms * (L["n_land"] or 0.0) * L["g"]  # in plane (landing along x)
    sig = 6 * (F_out * span / 4) / (b * t * t)
    tau = F_in / (b * t)
    seq = math.sqrt(sig * sig + 3 * tau * tau)
    sf = mat["strength"] / seq if seq > 0 else float("inf")
    return {"kind": "mechanical", "name": "Plate bending / shear by hand (CAD body is an open surface)",
            "primary": True, "status": _status(sf, mat["line"]), "sf": _f(sf, 3), "line": mat["line"],
            "value": _f(seq, 4), "unit": "MPa",
            "text": (f"FEA not possible: the deck's CAD body is an open (non-watertight) surface. Hand check: ESC + FC "
                     f"+ adapter + capacitor {ms * 1000:.0f} g; {N_AERO:g} g out of plane as a centre load on a simply "
                     f"supported {span:.0f} x {b:.0f} x {t:g} mm plate strip -> {sig:.2f} MPa; {L['n_land'] or 0:.1f} g "
                     f"in plane over the {b:.0f} x {t:g} mm section -> {tau:.2f} MPa shear; equivalent {seq:.2f} MPa vs "
                     f"{mat['strength']:g} MPa -> SF {sf:.1f}"),
            "material": {"name": mat["name"], "source": mat["source"], "ref": mat["ref"]}}


def _reuse_check(engine, nid: str, label: str) -> dict:
    """Status of an aircraft-level record that already covers a component (not re-run here)."""
    from opencae.engineering.graph import CURRENT
    h = engine.heavy_status(nid) if nid in engine.heavy else {"status": "not run", "reason": "", "record": None}
    rec = h.get("record") or {}
    if h["status"] != CURRENT:
        return {"kind": "reuse", "name": label, "primary": True, "status": NEV, "record": nid,
                "text": f"covered by the '{nid}' analysis, which is {h['status']} for this configuration"
                        + (f" ({h.get('reason')})" if h.get("reason") else "") + ": run it"}
    f = rec.get("failure") or {}
    if f:
        return {"kind": "reuse", "name": label, "primary": True, "status": f.get("status") or NEV, "record": nid,
                "sf": _f(f.get("safety_factor"), 3), "value": _f(f.get("value"), 4), "unit": f.get("unit"),
                "text": f"from the '{nid}' record: {f.get('text', '')}"}
    res = rec.get("result") or {}
    sf = (res.get("safety_factor") or {}).get("value")
    st = _status(sf, 1.5) if res.get("converged", True) else FAIL
    return {"kind": "reuse", "name": label, "primary": True, "status": st, "record": nid, "sf": _f(sf, 3),
            "value": _f(res.get("governing_axial_stress_MPa"), 4), "unit": "MPa",
            "text": f"from the '{nid}' record: governing axial stress {res.get('governing_axial_stress_MPa') or 0:.1f} "
                    f"MPa, SF {sf or 0:.2f} vs the estimated CF allowable (line 1.5)"}


# ============================================================================================ the screening
def run_component_screening(engine, workdir, fields_dir, progress: Optional[Callable[[str, int, int], None]] = None,
                            only: Optional[list] = None) -> dict:
    """Screen every active component of the engine's configuration (see the module docstring).

    Returns {"components": {component key: {"status", "checks": [...], "field": <save_field summary or None>,
    "title", "discipline", "part", "group", "process", "material", "reuses"}}, "summary": {...}, "materials",
    "load_cases", "seconds"}. Contour fields are written to ``fields_dir`` (record ids 'cs_<part>'). ``only``:
    restrict the FE runs to these FEA_SPECS ids (tests); every component still gets its other checks."""
    from opencae.engineering.manufacturing import process_of
    t0 = time.time()
    workdir, fields_dir = Path(workdir), Path(fields_dir)
    workdir.mkdir(parents=True, exist_ok=True)
    ctx = _Ctx(engine, workdir, fields_dir)
    m = ctx.m
    comps: dict = {}
    for c in m.active_components():
        proc, mat, _ = process_of(m, c)
        comps[c.key] = {"title": _first(c.key), "part": c.key, "group": c.group, "process": proc, "material": mat,
                        "discipline": "structural", "status": NEV, "checks": [_mass_check(ctx, c)], "field": None,
                        "reuses": None}
    errors = []
    specs = [s for s in FEA_SPECS if only is None or s["id"] in only]
    fea_done: set = set()

    for i, spec in enumerate(specs):
        body = ctx.pick(spec)
        if body is None or body.component not in comps:
            continue
        if progress:
            progress(spec["id"], i, len(specs))
        ck = body.component
        fea_done.add(ck)
        try:
            out = _run_fea(ctx, spec, body, workdir / spec["id"])
            ctx.runs[spec["id"]] = out
            check = out["check"]
            if spec.get("body"):
                check["name"] += f" - {spec['body'].lower()} ({_first(body.name)})"
            comps[ck]["checks"].append(check)
            cur = comps[ck]["field"]
            if cur is None or (check.get("sf") or 9e9) < (cur.get("failure", {}).get("safety_factor") or 9e9):
                comps[ck]["field"] = out["field"]
        except MeshRejected as exc:
            comps[ck]["checks"].append(_bearing_fallback(ctx, spec, body, f"no acceptable tetra mesh of the CAD body "
                                                                          f"({exc})"))
        except Exception as exc:  # a solver or setup failure is recorded, never hidden
            msg = f"{type(exc).__name__}: {exc}"
            errors.append({"part": spec["id"], "component": ck, "error": msg})
            comps[ck]["checks"].append({"kind": "fea", "name": "Linear static FEA screening", "primary": True,
                                        "status": NEV, "error": msg,
                                        "text": f"not evaluated: the screening run failed - {msg}"})
    # aircraft-level records that already cover parts
    land = m.option("landing")
    reuse = list(REUSE) + ([(land.bom_component, "fea_foot")] if land.geometry != "suppress" else [])
    for pre, nid in reuse:
        for ck in comps:
            if ck.startswith(pre):
                comps[ck]["checks"].append(_reuse_check(engine, nid, f"Aircraft-level FEA '{nid}'"))
                comps[ck]["reuses"] = nid
                fea_done.add(ck)
    for ck in comps:
        if ck.startswith("Motor mount"):
            comps[ck]["checks"].append(_reuse_check(engine, "thermal_mount", "Temperature next to the motor "
                                                                             "('thermal_mount')"))
    # analytical checks for the rest
    fast = None
    from opencae.engineering.assembly import is_fastener
    for c in m.active_components():
        e = comps[c.key]
        if c.key in fea_done:
            if c.key.startswith("Torque rod") and ctx.hinge().get("ok"):
                h = ctx.hinge()
                mat = ctx.mats["music_wire"]
                d = float(np.sort(np.ptp(np.vstack([m.body_mesh(m.bodies[i])[0] for i in c.bodies[:1]]), axis=0))[0])
                tau = 16 * abs(h["H_out"]) / (math.pi * d ** 3)
                allow = 0.45 * mat["strength"]
                sf = allow / tau if tau > 0 else float("inf")
                e["checks"].append({"kind": "mechanical", "name": "Torsion by hand (cross-check)", "primary": False,
                                    "status": _status(sf, mat["line"]), "sf": _f(sf, 3), "value": _f(tau, 4),
                                    "unit": "MPa", "text": f"tau = 16 T / (pi d³) = {tau:.1f} MPa (T {abs(h['H_out']):.0f} "
                                                           f"N mm, d {d:.2f} mm) vs 45 % of {mat['strength']:g} MPa = "
                                                           f"{allow:.0f} MPa -> SF {sf:.1f}"})
            continue
        if is_fastener(c):
            if fast is None:
                fast = _fastener_checks(ctx)
            chk = fast.get(c.key) or [_nev("Fastener", "not evaluated: no joined parts found for this fastener",
                                           kind="fastener")]
            e["checks"] += chk
            continue
        e["checks"] += _analytical(ctx, c)
    # statuses and discipline
    for ck, e in comps.items():
        prim = [x for x in e["checks"] if x.get("primary")]
        e["status"] = _worst([x["status"] for x in prim]) if prim else NEV
        kinds = {x["kind"] for x in prim}
        e["discipline"] = ("structural" if kinds & {"fea", "reuse", "contact_stress", "mechanical", "fastener"} else
                           "electrical" if kinds & {"rating", "ampacity"} else "none")
        e["covered_by"] = ("fea" if kinds & {"fea"} and any(x.get("field_id") for x in prim) else
                           "reuse" if "reuse" in kinds else
                           "analytical" if any(x["status"] != NEV for x in prim) else "not evaluated")
    counts = {s: sum(1 for e in comps.values() if e["status"] == s) for s in (OK, WATCH, FAIL, NEV)}
    cov = {k: sum(1 for e in comps.values() if e["covered_by"] == k)
           for k in ("fea", "reuse", "analytical", "not evaluated")}
    est = sorted({x["material"]["name"] for e in comps.values() for x in e["checks"]
                  if isinstance(x.get("material"), dict) and x["material"].get("source") == EST})
    summary = {"components": len(comps), "counts": counts, "coverage": cov, "fea_runs": len(ctx.runs),
               "fields": sum(1 for e in comps.values() if e["field"]), "solver_failures": errors,
               "failed": sorted(k for k, e in comps.items() if e["status"] == FAIL),
               "watch": sorted(k for k, e in comps.items() if e["status"] == WATCH),
               "not_evaluated": sorted(k for k, e in comps.items() if e["status"] == NEV),
               "estimated_materials": est, "seconds": round(time.time() - t0, 1),
               "note": "screening on provisional / supplier-datasheet material data and screening loads: not proof of "
                       "safe operation, not test evidence"}
    if progress:
        progress("done", len(specs), len(specs))
    return {"components": comps, "summary": summary,
            "materials": {k: {kk: v.get(kk) for kk in ("name", "source", "ref", "strength", "strength_basis", "line",
                                                       "values", "note")} for k, v in ctx.mats.items()},
            "load_cases": {**ctx.loads["text"], "factors": {"n_land": ctx.loads["n_land"], "n_aero": N_AERO,
                                                            "V_D_m_s": ctx.loads["V_D"], "q_D_Pa": ctx.loads["q_D"],
                                                            "C_N": CN_MAX}},
            "hinge": {k: v for k, v in ctx.hinge().items() if isinstance(v, (int, float, str, bool, list))},
            "seconds": round(time.time() - t0, 1)}
