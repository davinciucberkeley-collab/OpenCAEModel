"""Starting drone.json files per aircraft type, and the default requirements.

The template never invents a property: motor, propeller, battery, ESC, masses and materials are ``null``
(missing) until the user fills them from a datasheet or a measurement. The analyses that need them report
``not_evaluated`` and name the key.
"""
from __future__ import annotations

from opencae.drone.types import TYPES

HELP = ("Fill every null with a number from a datasheet or a measurement, or as "
        "{\"value\": 2400, \"unit\": \"rpm/V\", \"status\": \"datasheet\", \"source\": \"maker's page\"}. "
        "status is estimate, datasheet, measured or user. Keys starting with _ are notes and are ignored. "
        "Guide: docs/DRONE_PROJECTS.md")


def default_requirements(type_: str) -> list[dict]:
    """The requirement rows used when drone.json has no ``requirements`` list."""
    if type_ not in TYPES:
        raise ValueError(f"unknown aircraft type '{type_}' (use {', '.join(TYPES)})")
    R = lambda i, label, metric, op, value: {"id": i, "label": label, "metric": metric, "op": op, "value": value}
    motors = [R("R-I", "Motor current stays within the motor rating at full throttle (margin of at least 1)",
                "propulsion.motor_current_margin", ">=", 1.0),
              R("R-ESC", "Motor current stays within the speed-controller rating at full throttle "
                         "(margin of at least 1)", "propulsion.esc_current_margin", ">=", 1.0),
              R("R-BAT", "Battery current at full throttle stays within the battery rating (margin of at least 1)",
                "propulsion.battery_current_margin", ">=", 1.0)]
    arms = R("R-STR", "Structure holds the load case with a safety factor of at least 2",
             "structure.safety_factor", ">=", 2.0)
    if type_ == "multirotor":
        return [R("R-TW", "Thrust is at least 2 times the weight", "propulsion.tw_ratio", ">=", 2.0),
                R("R-HT", "Hovering needs at most 60 % throttle", "propulsion.hover_throttle", "<=", 0.6),
                R("R-END", "Hover time of at least 5 minutes on the usable battery",
                  "propulsion.hover_endurance_min", ">=", 5.0)] + motors + [
                arms,
                R("R-FLT", "After a 15 degree disturbance the aircraft tilts by at most 30 degrees",
                  "flight.max_tilt_deg", "<=", 30.0)]
    if type_ == "fixed_wing":
        return [R("R-TW", "Static thrust is at least 0.4 times the weight", "propulsion.tw_ratio", ">=", 0.4)] \
            + motors + [arms,
                        R("R-LD", "Lift-to-drag ratio of at least 8 in cruise", "cfd.ld", ">=", 8.0)]
    return [R("R-TW", "Thrust is at least 1.5 times the weight", "propulsion.tw_ratio", ">=", 1.5),
            R("R-HT", "Hovering needs at most 70 % throttle", "propulsion.hover_throttle", "<=", 0.7),
            R("R-END", "Hover time of at least 3 minutes on the usable battery",
              "propulsion.hover_endurance_min", ">=", 3.0)] + motors + [
            arms,
            R("R-LD", "Lift-to-drag ratio of at least 5 in cruise", "cfd.ld", ">=", 5.0),
            R("R-FLT", "After a 15 degree disturbance the aircraft tilts by at most 30 degrees",
              "flight.max_tilt_deg", "<=", 30.0)]


def template(type_: str, name: str, cad_file: str, units: str = "mm", roles: dict | None = None,
             roles_present: list | None = None) -> dict:
    """A drone.json for ``type_`` with every property the user must supply set to null."""
    if type_ not in TYPES:
        raise ValueError(f"unknown aircraft type '{type_}' (use {', '.join(TYPES)})")
    present = list(roles_present or [])
    mass_parts = {r: None for r in present if r != "motor"}
    mats = {"my_material": {"density_kg_m3": None, "youngs_modulus_gpa": None, "poisson": None,
                            "yield_strength_mpa": None, "status": "estimate",
                            "source": "FILL IN: material datasheet or a measurement"}}
    structural = [r for r in ("frame", "arm", "wing", "fin") if r in present]
    return {
        "_help": HELP,
        "format": "opencae-drone/1",
        "name": name,
        "type": type_,
        "engine": "generic",
        "geometry": {"file": cad_file, "units": units, "forward": "+x", "up": "+z"},
        "roles": roles or {},
        "materials": mats,
        "role_materials": {r: "my_material" for r in structural},
        "mass": {"_note": "grams. A role name is the mass of EACH part of that role; a part name is that part. "
                          "null = not filled in yet. Parts with a material density need no entry.",
                 "parts": mass_parts, "payload_g": 0},
        "propulsion": {
            "_note": "Every null below must be filled before the propulsion analysis can run.",
            "rotors": "auto",
            "motor": {"kv": None, "r_ohm": None, "i0_a": None, "i_max_a": None, "mass_g": None},
            "prop": {"diameter_in": None, "pitch_in": None, "ct": None, "cp": None},
            "battery": {"cells": None, "capacity_mah": None, "c_rating": None,
                        "r_int_mohm_per_cell": None, "usable_fraction": None},
            "esc": {"i_cont_a": None, "efficiency": None}},
        "aero": {"cruise_speed_ms": None, "alpha_deg": 0},
        "mission": {"hover_s": None, "cruise_s": None, "payload_g": 0},
        "environment": {"rho": 1.225, "g": 9.80665},
        "analyses": {"cfd": {"enabled": True, "preset": "coarse", "iterations": 300},
                     "structure": {"enabled": True, "load_factor": 2},
                     "flight": {"enabled": type_ != "fixed_wing", "disturbance_deg": 15}},
    }
