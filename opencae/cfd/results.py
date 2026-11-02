"""Flow results (velocity, pressure, turbulence) on the study mesh nodes."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np

from opencae.postprocessing.results import ResultsError


@dataclass
class FQuantity:
    key: str
    label: str
    unit_kind: str
    location: str


FLOW_QUANTITIES = {
    "V_mag": FQuantity("V_mag", "Velocity magnitude", "velocity", "nodal"),
    "V_x": FQuantity("V_x", "Velocity X", "velocity", "nodal"),
    "V_y": FQuantity("V_y", "Velocity Y", "velocity", "nodal"),
    "V_z": FQuantity("V_z", "Velocity Z", "velocity", "nodal"),
    "P": FQuantity("P", "Pressure (gauge)", "pressure_si", "nodal"),
    "P_total": FQuantity("P_total", "Total pressure p + ½ρ|V|²", "pressure_si", "nodal"),
    "mu_t": FQuantity("mu_t", "Eddy viscosity", "viscosity", "nodal"),
}


class CFDResultSet:
    kind = "cfd"
    frame = 0

    def __init__(self, mesh, velocity, pressure, eddy_viscosity=None, density: float = 1.0,
                 info: Optional[dict] = None):
        self.mesh = mesh
        self.velocity = np.asarray(velocity, float)
        self.pressure = np.asarray(pressure, float)
        self.eddy_viscosity = None if eddy_viscosity is None else np.asarray(eddy_viscosity, float)
        self.density = float(density)
        self.flow_info = info or {}
        self.displacement = np.zeros_like(mesh.nodes)
        self.stress = None
        self.ip_stress = None

    def frames(self) -> list[str]:
        return []

    def set_frame(self, i: int) -> None:
        pass

    def info(self, key):
        return FLOW_QUANTITIES[key]

    def available(self) -> list[str]:
        keys = ["V_mag", "V_x", "V_y", "V_z", "P", "P_total"]
        if self.eddy_viscosity is not None:
            keys.append("mu_t")
        return keys

    def quantity(self, key: str, frame=None) -> np.ndarray:
        v = self.velocity
        if key == "V_mag":
            return np.linalg.norm(v, axis=1)
        if key in ("V_x", "V_y", "V_z"):
            return v[:, "xyz".index(key[-1])]
        if key == "P":
            return self.pressure
        if key == "P_total":
            return self.pressure + 0.5 * self.density * np.einsum("ij,ij->i", v, v)
        if key == "mu_t":
            return self.eddy_viscosity
        raise KeyError(key)

    def summary(self) -> dict:
        vm = np.linalg.norm(self.velocity, axis=1)
        i = int(np.argmax(vm))
        s = {"kind": "cfd", "max_velocity": float(vm[i]), "max_velocity_node": i + 1,
             "min_pressure": float(self.pressure.min()), "max_pressure": float(self.pressure.max())}
        s.update(self.flow_info)
        return s

    def save(self, path: Path) -> None:
        d = {"kind": np.array("cfd"), "velocity": self.velocity, "pressure": self.pressure,
             "density": np.array(self.density), "info": np.array(json.dumps(self.flow_info))}
        if self.eddy_viscosity is not None:
            d["eddy_viscosity"] = self.eddy_viscosity
        np.savez_compressed(path, **d)

    @classmethod
    def load(cls, path: Path, mesh) -> "CFDResultSet":
        z = np.load(path, allow_pickle=False)
        if z["velocity"].shape != mesh.nodes.shape:
            raise ResultsError("Stored flow results do not match the mesh (node count differs).")
        return cls(mesh, z["velocity"], z["pressure"], z["eddy_viscosity"] if "eddy_viscosity" in z.files else None,
                   float(z["density"]), json.loads(str(z["info"])))
