"""Component catalog for configuration changes: motors, propellers, ESCs, batteries, landing systems.

Only data with a stated source are entered. Supplier values cite the page they were read from; values
the supplier does not publish are registered as unresolved inputs (``MISSING``) or, where a working
number is needed to proceed, as provisional estimates with the reason. Mass and price of the parts that
are in the K09-L assembly come from its BOM (bom.json of the K09-L model).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from opencae.engineering.quantities import ESTIMATE, MISSING, SUPPLIER, USER, Q

ROBU_X3108S = "robu.in product page 'Sunny Sky X3108S KV900 Brushless Motors' (read 2026-11-14)"
RMRC_X2814 = "readymaderc.com 'SunnySky X Series V3 X2814 Brushless Motor 900KV' (read 2026-11-14)"
GETFPV_TEKKO = "getfpv.com 'Holybro Tekko32 50A 4-6S AM32 4-in-1 ESC' listing title (read 2026-11-14)"
K09L_BOM = "K09-L Tailsitter Model, bom.json (Fusion BOM, 2026-11-11)"
DECISION = "Tailsitter Decision page (config study, decided 2026-11-09)"


@dataclass
class Option:
    """One selectable catalog entry."""
    id: str
    kind: str  # motor | prop | esc | battery | landing | mount
    name: str
    data: dict = field(default_factory=dict)  # str -> Q
    bom_component: str = ""  # K09-L component key this option corresponds to ("" = not in the CAD)
    geometry: str = "cad"  # cad | mass_only (no CAD of this option: the K09-L envelope is kept) | suppress
    compatible: bool = True
    incompatible_reason: str = ""
    notes: str = ""
    # geometry == "modify": parametric changes of the CAD bodies (see EngineeringModel._sync_geometry).
    # Each mod is a dict: {"op": "stretch" | "translate", "components": [component keys], "axis": 0|1|2,
    # "beyond": mm (stretch: only vertices with coordinate beyond this value move; "mirror": True uses |coord|),
    # "delta": mm (outward when "mirror"), "hardware_within": mm (translate: also move the fasteners whose
    # centroid lies within this distance of the moved bodies), "joints": prefix of joints moved with them,
    # "mass": "volume" (scale the CAD mass by the new / old volume, labelled ESTIMATE) | "cad" (keep it)}
    mods: list = field(default_factory=list)
    pn: str = ""  # part number of the modified part (geometry == "modify")

    def q(self, key: str) -> Q:
        return self.data.get(key) or Q(None, "", MISSING, note=f"{self.name}: '{key}' not known")


def _motor_x3108s() -> Option:
    return Option("X3108S-900", "motor", "SunnySky X3108S KV900", {
        "kv": Q(900.0, "rpm/V", SUPPLIER, ROBU_X3108S),
        "rm": Q(0.086, "ohm", SUPPLIER, ROBU_X3108S),
        "i0": Q(0.9, "A", SUPPLIER, ROBU_X3108S, note="no-load current at 10 V"),
        "imax": Q(25.0, "A", SUPPLIER, ROBU_X3108S, note="'25A/30S': maximum for 30 s, not a continuous rating"),
        "pmax": Q(370.0, "W", SUPPLIER, ROBU_X3108S),
        "cells_max": Q(4.0, "S", SUPPLIER, ROBU_X3108S, note="3-4S"),
        "mass": Q(0.081, "kg", SUPPLIER, ROBU_X3108S, note="K09-L BOM carries its own motor mass"),
        "shaft_d": Q(0.004, "m", SUPPLIER, ROBU_X3108S),
        "bolt_pattern": Q(None, "mm", MISSING, "K09-L checks", note="modelled 4 x M3 on 19 x 25 mm (+16 x 19); "
                          "SunnySky drawing not reached - MEASURE before printing mounts"),
    }, bom_component="Motor SunnySky X3108S KV900 — X3108SK9", geometry="cad")


def _motor_x2814() -> Option:
    return Option("X2814V3-900", "motor", "SunnySky X2814 V3 KV900", {
        "kv": Q(900.0, "rpm/V", SUPPLIER, RMRC_X2814),
        "rm": Q(0.0336, "ohm", SUPPLIER, RMRC_X2814),
        "i0": Q(1.2, "A", SUPPLIER, RMRC_X2814, note="no-load current at 10 V"),
        "imax": Q(50.0, "A", SUPPLIER, RMRC_X2814, note="'50A/30s'"),
        "pmax": Q(680.0, "W", SUPPLIER, RMRC_X2814),
        "cells_max": Q(4.0, "S", SUPPLIER, RMRC_X2814, note="3-4S"),
        "mass": Q(0.108, "kg", SUPPLIER, RMRC_X2814),
        "price": Q(39.99, "USD", SUPPLIER, RMRC_X2814),
        "shaft_d": Q(None, "m", MISSING, RMRC_X2814, note="not listed ('shaft for collet mount: yes')"),
        "bolt_pattern": Q(None, "mm", MISSING, RMRC_X2814, note="not listed"),
    }, geometry="mass_only", notes="No CAD of this motor: the X3108S envelope stays in the model, its mass changes.")


def _prop_apc11() -> Option:
    return Option("APC11x4.5MR", "prop", "APC 11x4.5MR / MRP pair", {
        "diameter": Q(11 * 0.0254, "m", SUPPLIER, "APC part number LP11045MR (11 in)"),
        "pitch": Q(4.5 * 0.0254, "m", SUPPLIER, "APC part number LP11045MR (4.5 in)"),
        # no APC performance file could be reached from this session: working values, labelled
        "ct0": Q(0.105, "-", ESTIMATE, "typical static C_T of ~0.4 pitch/diameter APC multirotor props "
                 "(UIUC propeller database range 0.09-0.12)", uncertainty=0.12,
                 note="replace with APC PER3_11x45MR.dat or a thrust-stand measurement"),
        "cp0": Q(0.045, "-", ESTIMATE, "typical static C_P, same basis", uncertainty=0.15),
    }, bom_component="Prop APC 11x4.5MR (CCW) — LP11045MR")


def _esc_tekko() -> Option:
    return Option("Tekko32F4-50A", "esc", "Holybro Tekko32 F4 4in1 50A (AM32)", {
        "i_cont": Q(50.0, "A", SUPPLIER, GETFPV_TEKKO, note="per channel, as named; burst rating not read"),
        "cells_min": Q(4.0, "S", SUPPLIER, GETFPV_TEKKO),
        "cells_max": Q(6.0, "S", SUPPLIER, GETFPV_TEKKO),
        "channels": Q(4.0, "-", SUPPLIER, GETFPV_TEKKO),
    }, bom_component="ESC Holybro Tekko32 F4 4in1 50A — Tekko32 F4 4in1 50A AM32")


def _battery(id_, name, cells, mah, c_rate, mass, bom="", price=None, src=K09L_BOM) -> Option:
    d = {
        "cells": Q(float(cells), "S", SUPPLIER, name),
        "capacity": Q(mah / 1000.0 * 3600.0, "C", SUPPLIER, name, note=f"{mah} mAh"),
        "c_rate": Q(float(c_rate), "C", SUPPLIER, name, note="advertised; real sustained rating is lower"),
        "v_nom": Q(3.7 * cells, "V", SUPPLIER, "LiPo nominal 3.7 V/cell"),
        "v_full": Q(4.2 * cells, "V", SUPPLIER, "LiPo 4.2 V/cell"),
        "v_low": Q(3.5 * cells, "V", USER, "open-circuit voltage at the end of the usable capacity (working value "
                   "3.5 V/cell); load sag through r_int comes on top, in calcs and in the flight model alike"),
        "r_int": Q(0.004 * cells, "ohm", ESTIMATE, "about 4 mOhm/cell for a fresh 4000 mAh high-C pack",
                   uncertainty=0.5, note="measure with the charger's IR readout"),
        "usable": Q(0.80, "-", USER, "use 80 % of rated capacity"),
    }
    if mass is not None:
        d["mass"] = Q(mass, "kg", SUPPLIER if not bom else SUPPLIER, src)
    if price is not None:
        d["price"] = Q(price, "USD", SUPPLIER, src)
    return Option(id_, "battery", name, d, bom_component=bom, geometry="cad" if bom else "mass_only")


FOOT = "Foot PETG + TPU pad (x4) — K09L-AF-FOOT"
WALL_TRIM = 4.0  # mm per side wall, foot B
FOOT_SCREW = "SHCS M3x30 A2 — ISO 4762 M3x30 (vendor PN not verified)"


def _landing() -> list[Option]:
    return [
        Option("feet", "landing", "Four PETG feet with TPU pads (as modelled)", {
            "stroke": Q(0.003, "m", ESTIMATE, "TPU pad compression, about half of a ~6 mm pad", uncertainty=0.5),
        }, bom_component="Foot PETG + TPU pad (x4) — K09L-AF-FOOT", geometry="cad"),
        Option("fin_tips", "landing", "Land on the bare fin tips (feet removed)", {
            "stroke": Q(0.001, "m", ESTIMATE, "crush of the 6 mm Depron fin edge before load builds up",
                        uncertainty=0.7),
        }, bom_component="Foot PETG + TPU pad (x4) — K09L-AF-FOOT", geometry="suppress",
            notes="The feet are removed from the assembly; the fin tips become the contact points."),
        Option("feet_b", "landing", "Outrigger feet K09L-AF-FOOT-B (parametric: outrigger, shortening)", {
            "stroke": Q(0.003, "m", ESTIMATE, "TPU pad compression, about half of a ~6 mm pad (as feet A)",
                        uncertainty=0.5),
            "petg_strength": Q(25e6, "Pa", ESTIMATE, "FDM PETG across layers, 100 % walls at the root (open "
                               "literature 20-35 MPa; not tested)", uncertainty=0.3,
                               note="replace with a coupon test of the printer / settings used"),
        }, bom_component=FOOT, geometry="modify", pn="K09L-AF-FOOT-B",
            # the fin slot ends at |z| 190.2 and x 300.2 (fin tip 190, fin trailing edge 300): both stretches act
            # only on material beyond the slot, so the slot and its fit on the fin are unchanged
            mods=[{"op": "stretch", "components": [FOOT], "axis": 2, "beyond": 191.0, "mirror": True,
                   "delta": ["foot_outrigger_mm", 1.0], "mass": "volume"},
                  {"op": "stretch", "components": [FOOT], "axis": 0, "beyond": 301.0, "delta": ["foot_shorten_mm", -1.0],
                   "mass": "volume"},
                  # side walls 9 -> 5 mm (the 6 mm fin slot at |y| 317..323 is untouched)
                  {"op": "stretch", "components": [FOOT], "axis": 1, "beyond": 326.0, "mirror": True,
                   "delta": -WALL_TRIM, "mass": "volume"},
                  {"op": "stretch", "components": [FOOT], "axis": 1, "below": 314.0, "mirror": True,
                   "delta": WALL_TRIM, "mass": "volume"},
                  # the clamp stack is 8 mm thinner: nut and washer follow the inner wall, the screw head the
                  # outer wall, and the screw becomes an M3x25 (4.5 mm of thread past the nut, as before)
                  {"op": "translate", "components": [], "hardware_of": [FOOT], "within": 4.0, "axis": 1,
                   "mirror": True, "delta": WALL_TRIM, "where": {"abs_lt": 317.0}, "exclude": [FOOT_SCREW]},
                  {"op": "stretch", "components": [FOOT_SCREW], "axis": 1, "beyond": 320.0, "mirror": True,
                   "delta": -WALL_TRIM, "mass": "volume", "pn": "ISO 4762 M3x25 (vendor PN not verified)",
                   "name": "SHCS M3x25 A2 (foot B clamp)"},
                  {"op": "stretch", "components": [FOOT_SCREW], "axis": 1, "below": 310.0, "mirror": True,
                   "delta": 1.0, "mass": "volume"}],
            notes="Derived from foot A by a parametric stretch of its CAD, acting only on material beyond the fin "
                  "slot (the slot and its fit on the fin are unchanged). Parameters foot_outrigger_mm and "
                  "foot_shorten_mm; at the revision-B values (22, 6): the outboard end reaches 22 mm further out "
                  "(outer edge |z| 198 -> 220 mm, a solid 30 mm outrigger beyond the fin tip), the aft wall is 6 mm "
                  "thinner (ground plane x 320 -> 314 mm, elevon trailing edge clearance 20 -> 14 mm) and the side "
                  "walls are 9 -> 5 mm with an M3x25 clamp screw. Widens the support polygon and lowers the CG height above it."),
        Option("wheels", "landing", "Wheeled landing gear", {}, compatible=False,
               incompatible_reason="A tailsitter lands vertically on its tail; wheels give no benefit and the "
                                   "K09-L has no gear attachment points. Not offered."),
        Option("skids", "landing", "Fixed skids", {}, compatible=False,
               incompatible_reason="No skid geometry or attachment exists in the K09-L CAD; it would have to be "
                                   "designed first (register as a design task, not a selectable option)."),
    ]


MOUNT = "Motor mount PETG elbow — K09L-PROP-MNT-01"
MOTOR_TUBE = "Motor tube CF 16x13.08x380 — DragonPlate FDPT.625*.515*24"
FIN = "Fin Depron 6 mm (L=R) — K09L-AF-FIN"
MOTOR_SET = [MOUNT, "Motor SunnySky X3108S KV900 — X3108SK9", "Prop APC 11x4.5MR (CCW) — LP11045MR",
             "Prop APC 11x4.5MRP (CW) — LP11045MRP", "Prop adapter collet 4 mm to M5 — X3108S kit",
             "Prop adapter ring 5 mm — APC LPAR06E", "Prop locknut M5 — X3108S kit", "Washer M5 — X3108S kit"]


def _mount() -> list[Option]:
    return [
        Option("as_modelled", "mount", "Motor sets as modelled (motor tube 380 mm, motor axis |z| 206 mm)", {},
               bom_component=MOTOR_TUBE, geometry="cad"),
        Option("inboard_b", "mount", "Motor sets moved inboard, shorter motor tube (parametric: motor_inboard_mm)", {
        }, bom_component=MOTOR_TUBE, geometry="modify",
            mods=[{"op": "translate", "components": MOTOR_SET, "axis": 2, "mirror": True,
                   "delta": ["motor_inboard_mm", -1.0],
                   "hardware_within": 4.0, "joints": "Prop M", "mass": "cad"},
                  {"op": "stretch", "components": [MOTOR_TUBE], "axis": 2, "beyond": 180.0, "mirror": True,
                   "delta": ["motor_inboard_mm", -1.0], "mass": "volume",
                   "name": "Motor tube CF 16x13.08x{380 - 2 * motor_inboard_mm:.0f}",
                   "pn": "DragonPlate FDPT.625*.515*24 (cut to {380 - 2 * motor_inboard_mm:.0f} mm)"},
                  # the fin's leading-edge notch that receives the elbow (x < -79.5, |z| > 160.5 mm) is cut 6 mm
                  # deeper so the elbow keeps its clearance to the fin
                  {"op": "stretch", "components": [FIN], "axis": 2, "beyond": 160.0, "below": 161.0,
                   "mirror": True, "delta": ["motor_inboard_mm", -1.0], "mass": "volume", "pn": "K09L-AF-FIN-B"}],
            notes="Parameter motor_inboard_mm (revision B: 6 mm). At 6 mm: the motor tube is cut 12 mm shorter (380 -> 368 mm, from the same 24 in stock) and each motor set "
                  "(elbow A unchanged, motor, prop hardware, mount screws) sits 6 mm further inboard: motor axis "
                  "|z| 206 -> 200 mm. The fin's leading-edge notch that receives the elbow is cut 6 mm deeper "
                  "(K09L-AF-FIN-B). The aircraft's height drops from 454 to 442 mm, so it fits the 450 mm side of "
                  "the transport box with 8 mm to spare."),
    ]


def _mount_material() -> list[Option]:
    return [
        Option("petg", "mount_material", "Motor mounts printed in PETG (as modelled)", {
            "T_allow": Q(65.0, "degC", ESTIMATE, "PETG glass transition about 80 degC, 15 K margin for creep under "
                         "the motor screws", uncertainty=0.1),
            "k": Q(0.20, "W/mK", ESTIMATE, "PETG thermal conductivity (datasheet range 0.19-0.24)"),
        }, bom_component=MOUNT, geometry="cad"),
        Option("asa", "mount_material", "Motor mounts printed in ASA (revision B, change B6)", {
            "T_allow": Q(85.0, "degC", ESTIMATE, "ASA glass transition about 100-105 degC, 15 K margin for creep "
                         "under the motor screws", uncertainty=0.1),
            "k": Q(0.17, "W/mK", ESTIMATE, "ASA thermal conductivity (datasheet range 0.15-0.19)"),
        }, bom_component=MOUNT, geometry="cad",
            notes="Same part, printed in ASA instead of PETG: about 20 K more temperature margin next to the motor "
                  "base. ASA prints in an enclosed printer (the team's Jacobs Hall access has one). ASA is about "
                  "15 % less dense than PETG; the CAD (PETG) mass is kept, which is conservative."),
    ]


KINDS = ("motor", "prop", "esc", "battery", "landing", "mount", "mount_material")
# design parameters of the parametric geometry options (mm): default (= revision B), allowed range, option
GEOMETRY_PARAMS = {
    "foot_outrigger_mm": (22.0, 0.0, 40.0, "feet_b", "outboard extension of the foot beyond foot A's outer edge"),
    "foot_shorten_mm": (6.0, 0.0, 10.0, "feet_b", "aft wall thinning, raises the ground plane (the 15.8 mm aft wall "
                        "behind the fin slot limits it to 10 mm)"),
    "motor_inboard_mm": (6.0, 0.0, 12.0, "inboard_b", "motor sets moved inboard; the motor tube is cut 2x this shorter "
                         "and the fin notch deepened by it"),
}
GEOMETRY_KINDS = ("landing", "mount")


def catalog() -> dict[str, list[Option]]:
    batt = [
        _battery("Ovonic4S4000", "Ovonic 4S 4000 mAh 130C", 4, 4000, 130, None,
                 bom="LiPo Ovonic 4S 4000 mAh 130C — OVONIC 4000mAh 4S1P 130C XT60"),
    ]
    return {"motor": [_motor_x3108s(), _motor_x2814()], "prop": [_prop_apc11()], "esc": [_esc_tekko()],
            "battery": batt, "landing": _landing(), "mount": _mount(),
            "mount_material": _mount_material()}


def option(cat: dict, kind: str, oid: str) -> Option:
    for o in cat[kind]:
        if o.id == oid:
            return o
    raise KeyError(f"no {kind} option '{oid}'")


DEFAULT_CONFIG = {"motor": "X3108S-900", "prop": "APC11x4.5MR", "esc": "Tekko32F4-50A", "battery": "Ovonic4S4000",
                  "landing": "feet", "mount": "as_modelled", "mount_material": "petg"}
