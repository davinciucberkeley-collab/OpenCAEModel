"""Consistent unit systems.

OpenCAE Studio works in a single *consistent* unit system per project. All user
inputs (geometry, loads, material properties shown in the UI) and all solver
inputs/outputs are expressed in that system, so no silent conversion happens
inside the solver deck.

Materials are stored in the library in base SI (Pa, kg/m^3, 1/K) and converted
to the project system when they are assigned / written to the solver.

Supported systems (both are consistent: F = m * a holds without factors):

=========  ======  =====  ======  =========  ==============
system     length  force  stress  density    acceleration
=========  ======  =====  ======  =========  ==============
mm-N-s     mm      N      MPa     t/mm^3     mm/s^2
m-N-s      m       N      Pa      kg/m^3     m/s^2
=========  ======  =====  ======  =========  ==============
"""
from __future__ import annotations

import math
from dataclasses import dataclass

# ---- the one table of conversion factors for typed quantities (console grammar, Motion dialogs and plots)
DEG_TO_RAD = math.pi / 180.0
RAD_TO_DEG = 180.0 / math.pi
RPM_TO_RAD_S = 2.0 * math.pi / 60.0
RAD_S_TO_RPM = 60.0 / (2.0 * math.pi)
DEG_S_TO_RAD_S = DEG_TO_RAD
MM_TO_M = 1e-3

# unit -> (dimension, factor to the SI base: m, rad, rad/s, N, N*m, s, Hz, Pa, kg, W)
UNIT_TABLE = {
    "mm": ("length", MM_TO_M), "cm": ("length", 1e-2), "m": ("length", 1.0), "um": ("length", 1e-6),
    "in": ("length", 0.0254), "inch": ("length", 0.0254),
    "deg": ("angle", DEG_TO_RAD), "rad": ("angle", 1.0),
    "rpm": ("speed", RPM_TO_RAD_S), "rad/s": ("speed", 1.0), "deg/s": ("speed", DEG_S_TO_RAD_S),
    "n": ("force", 1.0), "kn": ("force", 1e3), "mn": ("force", 1e-3),
    "nm": ("torque", 1.0), "nmm": ("torque", 1e-3),
    "s": ("time", 1.0), "ms": ("time", 1e-3), "min": ("time", 60.0),
    "hz": ("frequency", 1.0), "khz": ("frequency", 1e3),
    "pa": ("pressure", 1.0), "kpa": ("pressure", 1e3), "mpa": ("pressure", 1e6), "gpa": ("pressure", 1e9),
    "kg": ("mass", 1.0), "g": ("mass", 1e-3),
    "w": ("power", 1.0), "kw": ("power", 1e3),
    "c": ("temperature", 1.0), "k": ("temperature", 1.0),
    "mah": ("charge", 1.0), "v": ("voltage", 1.0), "%": ("ratio", 0.01),
}


@dataclass(frozen=True)
class UnitSystem:
    key: str
    length: str
    force: str
    stress: str
    density: str
    acceleration: str
    # multiply an SI value by these factors to obtain the value in this system
    length_from_si: float
    stress_from_si: float
    density_from_si: float
    accel_from_si: float
    # thermal (consistent units: energy = force * length)
    #   conductivity   W/(m K) = N/(s K)            -> identical number in both systems
    #   specific heat  J/(kg K) = m^2/(s^2 K)       -> mm^2/(s^2 K): x 1e6
    #   heat flux      W/m^2  = N/(m s)             -> N/(mm s):     x 1e-3
    #   film coeff.    W/(m^2 K)                    -> N/(mm s K):   x 1e-3
    #   volumetric     W/m^3  = N/(m^2 s)           -> N/(mm^2 s):   x 1e-6
    #   power          W = N m/s                    -> N mm/s:       x 1e3
    conductivity_from_si: float = 1.0
    specific_heat_from_si: float = 1.0
    heat_flux_from_si: float = 1.0
    film_from_si: float = 1.0
    heat_gen_from_si: float = 1.0
    power_from_si: float = 1.0

    @property
    def displacement(self) -> str:
        return self.length

    @property
    def pressure(self) -> str:
        return self.stress

    @property
    def strain(self) -> str:
        return "-"

    @property
    def force_per_volume(self) -> str:
        return f"{self.force}/{self.length}^3"


UNIT_SYSTEMS: dict[str, UnitSystem] = {
    "mm-N-s": UnitSystem(
        key="mm-N-s", length="mm", force="N", stress="MPa", density="t/mm^3",
        acceleration="mm/s^2",
        length_from_si=1e3, stress_from_si=1e-6, density_from_si=1e-12, accel_from_si=1e3,
        conductivity_from_si=1.0, specific_heat_from_si=1e6, heat_flux_from_si=1e-3, film_from_si=1e-3,
        heat_gen_from_si=1e-6, power_from_si=1e3,
    ),
    "m-N-s": UnitSystem(
        key="m-N-s", length="m", force="N", stress="Pa", density="kg/m^3",
        acceleration="m/s^2",
        length_from_si=1.0, stress_from_si=1.0, density_from_si=1.0, accel_from_si=1.0,
    ),
}

DEFAULT_UNIT_SYSTEM = "mm-N-s"
STANDARD_GRAVITY_SI = 9.80665  # m/s^2


def get_unit_system(key: str) -> UnitSystem:
    try:
        return UNIT_SYSTEMS[key]
    except KeyError as exc:  # pragma: no cover - defensive
        raise ValueError(f"Unknown unit system '{key}'. Valid: {sorted(UNIT_SYSTEMS)}") from exc


def check_thermal_consistency(us: UnitSystem) -> bool:
    """Energy balance must hold without factors: q [flux] = k [cond] * dT/dx [K/length];
    rho c dT/dt [energy/vol/time] = flux / length; power = flux * area."""
    L = us.length_from_si
    ok1 = abs(us.heat_flux_from_si - us.conductivity_from_si / L) < 1e-15
    ok2 = abs(us.density_from_si * us.specific_heat_from_si - us.heat_flux_from_si / L) < 1e-15
    ok3 = abs(us.power_from_si - us.heat_flux_from_si * L * L) < 1e-9
    ok4 = abs(us.heat_gen_from_si - us.heat_flux_from_si / L) < 1e-18
    ok5 = abs(us.film_from_si - us.heat_flux_from_si) < 1e-15
    return ok1 and ok2 and ok3 and ok4 and ok5


def check_consistency(us: UnitSystem) -> bool:
    """Verify F = m a consistency: stress*length^2 (force) == density*length^3*accel."""
    # 1 N expressed in the system must be 1 (force unit is N for both systems)
    force_from_stress = us.stress_from_si * us.length_from_si ** 2  # N -> system force
    force_from_inertia = us.density_from_si * us.length_from_si ** 3 * us.accel_from_si
    return abs(force_from_stress - 1.0) < 1e-12 and abs(force_from_inertia - 1.0) < 1e-12
