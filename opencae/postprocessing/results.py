"""Result sets and derived quantities.

All quantities are computed from CalculiX output; nothing is synthesised.

Stress/strain tensors are stored in Voigt order (xx, yy, zz, xy, yz, zx).
CalculiX writes **tensor** shear strains (eps_xy, not engineering gamma_xy =
2 eps_xy); this is verified by an automated test against Hooke's law.

Location of data:

* ``nodal-averaged``: CalculiX .frd values, extrapolated from integration
  points to nodes and averaged across elements. Smooth, suitable for contours,
  but not raw solver data.
* ``integration-point``: raw values at Gauss points from the .dat file
  (used for the reported max-IP von Mises stress).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from opencae.meshing.mesh_data import FEMesh
from opencae.solver.calculix.dat import DatFile, read_dat
from opencae.solver.calculix.frd import read_frd


class ResultsError(Exception):
    pass


def von_mises(s: np.ndarray) -> np.ndarray:
    xx, yy, zz, xy, yz, zx = (s[:, k] for k in range(6))
    return np.sqrt(0.5 * ((xx - yy) ** 2 + (yy - zz) ** 2 + (zz - xx) ** 2) + 3.0 * (xy ** 2 + yz ** 2 + zx ** 2))


def tensor_matrix(s: np.ndarray) -> np.ndarray:
    xx, yy, zz, xy, yz, zx = (s[:, k] for k in range(6))
    return np.stack([np.stack([xx, xy, zx], -1), np.stack([xy, yy, yz], -1), np.stack([zx, yz, zz], -1)], -2)


def principal(s: np.ndarray) -> np.ndarray:
    """Principal values sorted descending: (n, 3) -> [s1 >= s2 >= s3]."""
    return np.linalg.eigvalsh(tensor_matrix(s))[:, ::-1]


@dataclass
class Quantity:
    key: str
    label: str
    unit_kind: str  # displacement | stress | strain
    location: str  # nodal | nodal-averaged
    func: Callable[["ResultSet"], np.ndarray]


@dataclass
class ResultSet:
    mesh: FEMesh
    displacement: np.ndarray  # (N,3)
    stress: Optional[np.ndarray] = None  # (N,6) nodal-averaged
    strain: Optional[np.ndarray] = None  # (N,6) tensor strains, nodal-averaged
    reaction: Optional[np.ndarray] = None  # (N,3) nodal reaction forces
    ip_stress: Optional[np.ndarray] = None  # (n_ip, 6)
    ip_elem: Optional[np.ndarray] = None
    reaction_totals: dict[str, np.ndarray] = field(default_factory=dict)
    contact: Optional[np.ndarray] = None  # (N,6) contact output of slave nodes (zeros elsewhere)
    _cache: dict[str, np.ndarray] = field(default_factory=dict, repr=False)

    kind = "structural"
    frame = 0

    # ------------------------------------------------------------- frames (modes / time steps)
    def frames(self) -> list[str]:
        """Labels of the stored frames; empty for a single static result."""
        return []

    def set_frame(self, i: int) -> None:
        pass

    def info(self, key: str) -> "Quantity":
        return QUANTITIES[key]

    # ------------------------------------------------------------- quantities
    def quantity(self, key: str) -> np.ndarray:
        if key not in self._cache:
            q = QUANTITIES.get(key)
            if q is None:
                raise KeyError(key)
            self._cache[key] = q.func(self)
        return self._cache[key]

    def available(self) -> list[str]:
        out = []
        for k, q in QUANTITIES.items():
            if q.unit_kind == "stress" and self.stress is None:
                continue
            if q.unit_kind == "strain" and self.strain is None:
                continue
            if k.startswith("C_") and getattr(self, "contact", None) is None:
                continue
            out.append(k)
        return out

    def ip_von_mises_max(self) -> Optional[float]:
        if self.ip_stress is None or not len(self.ip_stress):
            return None
        return float(von_mises(self.ip_stress).max())

    def summary(self) -> dict:
        u = self.quantity("U_mag")
        imax = int(np.argmax(u))
        s = {"max_displacement": float(u[imax]), "max_displacement_node": imax + 1,
             "max_displacement_location": self.mesh.nodes[imax].tolist(),
             "max_abs_ux": float(np.abs(self.displacement[:, 0]).max()),
             "max_abs_uy": float(np.abs(self.displacement[:, 1]).max()),
             "max_abs_uz": float(np.abs(self.displacement[:, 2]).max())}
        if self.stress is not None:
            vm = self.quantity("S_vm")
            j = int(np.argmax(vm))
            s.update({"max_von_mises_nodal": float(vm[j]), "max_von_mises_node": j + 1,
                      "max_von_mises_location": self.mesh.nodes[j].tolist(),
                      "max_principal": float(self.quantity("S_p1").max()),
                      "min_principal": float(self.quantity("S_p3").min())})
        ip = self.ip_von_mises_max()
        if ip is not None:
            s["max_von_mises_integration_point"] = ip
        if self.reaction_totals:
            s["reaction_totals"] = {k: v.tolist() for k, v in self.reaction_totals.items()}
        if self.contact is not None:
            cp = self.contact[:, 3]
            k = int(np.argmax(cp))
            s.update({"max_contact_pressure": float(cp[k]), "max_contact_pressure_node": k + 1,
                      "contact_nodes_closed": int(np.sum(cp > 0)), "max_penetration": float(-self.contact[:, 0].min())})
        return s

    # ------------------------------------------------------------- IO
    def save(self, path: Path) -> None:
        d = {"displacement": self.displacement}
        for k in ("stress", "strain", "reaction", "ip_stress", "ip_elem", "contact"):
            v = getattr(self, k, None)
            if v is not None:
                d[k] = v
        d["rt_keys"] = np.array(list(self.reaction_totals.keys()))
        for i, v in enumerate(self.reaction_totals.values()):
            d[f"rt_{i}"] = v
        np.savez_compressed(path, **d)

    @classmethod
    def load(cls, path: Path, mesh: FEMesh) -> "ResultSet":
        z = np.load(path, allow_pickle=False)
        rs = cls(mesh=mesh, displacement=z["displacement"])
        for k in ("stress", "strain", "reaction", "ip_stress", "ip_elem", "contact"):
            if k in z.files:
                setattr(rs, k, z[k])
        rs.reaction_totals = {str(k): z[f"rt_{i}"] for i, k in enumerate(z["rt_keys"])}
        if rs.displacement.shape != mesh.nodes.shape:
            raise ResultsError("Stored results do not match the mesh (node count differs).")
        return rs

    # ------------------------------------------------------------- from CalculiX
    @classmethod
    def from_calculix(cls, frd_path: Path, dat_path: Optional[Path], mesh: FEMesh) -> "ResultSet":
        frd = read_frd(frd_path)
        N = mesh.n_nodes
        if len(frd.node_ids) != N:
            raise ResultsError(f"Result file has {len(frd.node_ids)} nodes, mesh has {N}.")
        order = frd.node_ids - 1
        if order.min() < 0 or order.max() >= N:
            raise ResultsError("Result node ids outside mesh numbering.")
        xyz = np.empty_like(mesh.nodes)
        xyz[order] = frd.coords
        L = float(np.ptp(mesh.nodes, axis=0).max()) or 1.0
        dev = float(np.abs(xyz - mesh.nodes).max()) / L
        if dev > 1e-4:  # frd stores coordinates with 6 significant digits
            raise ResultsError(f"Result node coordinates do not match the mesh (rel. dev {dev:.2e}).")

        def nodal(name: str, ncomp: int) -> Optional[np.ndarray]:
            b = frd.get(name)
            if b is None:
                return None
            a = np.zeros((N, ncomp))
            a[b.node_ids - 1] = b.values[:, :ncomp]
            return a

        disp = nodal("DISP", 3)
        if disp is None:
            raise ResultsError("No displacement (DISP) block in result file.")
        rs = cls(mesh=mesh, displacement=disp, stress=nodal("STRESS", 6), strain=nodal("TOSTRAIN", 6),
                 reaction=nodal("FORC", 3))
        rs.contact = nodal("CONTACT", 6)  # COPEN, CSLIP1, CSLIP2, CPRESS, CSHEAR1, CSHEAR2 (last increment)
        if dat_path is not None and Path(dat_path).exists():
            dat: DatFile = read_dat(dat_path)
            rs.ip_stress, rs.ip_elem = dat.ip_stress, dat.ip_elem
            rs.reaction_totals = dat.totals
        return rs


def equilibrium_check(summary: dict, rel_tol: float = 1e-5) -> Optional[dict]:
    """Global force balance from solver reactions and the applied loads.

    CalculiX reaction forces (RF) are the internal forces at constrained DOFs and do not
    include external load applied directly on those DOFs, hence
    ``sum(RF) + F_applied - F_on_constrained = 0``. The tolerance reflects the 7
    significant digits of the .dat totals.
    """
    rt = summary.get("reaction_totals") or {}
    if not rt or "applied_force_total" not in summary:
        return None
    if "BC_ALL_SUPPORTS" in rt:  # union set: no double counting of nodes shared by several supports
        reactions = np.asarray(rt["BC_ALL_SUPPORTS"], float)
    else:  # projects solved before 0.2 (sets were disjoint in all shipped examples)
        reactions = np.sum([np.asarray(v, float) for v in rt.values()], axis=0)
    applied = np.asarray(summary["applied_force_total"], float)
    on_c = np.asarray(summary.get("applied_on_constrained", [0, 0, 0]), float)
    resid = reactions + applied - on_c
    scale = max(float(np.abs(applied).max()), float(np.abs(reactions).max()), 1e-300)
    rel = float(np.abs(resid).max() / scale)
    return {"reactions": reactions, "applied": applied, "on_constrained": on_c, "residual": resid,
            "relative": rel, "ok": rel <= rel_tol}


def _mag(v: np.ndarray) -> np.ndarray:
    return np.linalg.norm(v, axis=1)


QUANTITIES: dict[str, Quantity] = {
    "U_mag": Quantity("U_mag", "Displacement magnitude", "displacement", "nodal", lambda r: _mag(r.displacement)),
    "U_x": Quantity("U_x", "Displacement X", "displacement", "nodal", lambda r: r.displacement[:, 0]),
    "U_y": Quantity("U_y", "Displacement Y", "displacement", "nodal", lambda r: r.displacement[:, 1]),
    "U_z": Quantity("U_z", "Displacement Z", "displacement", "nodal", lambda r: r.displacement[:, 2]),
    "S_vm": Quantity("S_vm", "Von Mises stress", "stress", "nodal-averaged", lambda r: von_mises(r.stress)),
    "S_p1": Quantity("S_p1", "Max principal stress", "stress", "nodal-averaged", lambda r: principal(r.stress)[:, 0]),
    "S_p2": Quantity("S_p2", "Mid principal stress", "stress", "nodal-averaged", lambda r: principal(r.stress)[:, 1]),
    "S_p3": Quantity("S_p3", "Min principal stress", "stress", "nodal-averaged", lambda r: principal(r.stress)[:, 2]),
    "S_tresca": Quantity("S_tresca", "Tresca stress (s1 - s3)", "stress", "nodal-averaged",
                         lambda r: principal(r.stress)[:, 0] - principal(r.stress)[:, 2]),
    **{f"S_{c}": Quantity(f"S_{c}", f"Stress {c.upper()}", "stress", "nodal-averaged",
                          (lambda k: lambda r: r.stress[:, k])(i))
       for i, c in enumerate(("xx", "yy", "zz", "xy", "yz", "zx"))},
    **{f"E_{c}": Quantity(f"E_{c}", f"Strain {c.upper()}" + (" (tensor shear)" if c[0] != c[1] else ""),
                          "strain", "nodal-averaged", (lambda k: lambda r: r.strain[:, k])(i))
       for i, c in enumerate(("xx", "yy", "zz", "xy", "yz", "zx"))},
    "E_eqv": Quantity("E_eqv", "Equivalent (von Mises) strain", "strain", "nodal-averaged",
                      lambda r: (2.0 / 3.0) * np.sqrt(1.5 * np.einsum(
                          "nij,nij->n", *(2 * [tensor_matrix(r.strain) - np.einsum(
                              "n,ij->nij", np.trace(tensor_matrix(r.strain), axis1=1, axis2=2) / 3.0,
                              np.eye(3))])))),
    "C_press": Quantity("C_press", "Contact pressure", "stress", "nodal (slave surface)",
                        lambda r: r.contact[:, 3]),
    "C_open": Quantity("C_open", "Contact opening (gap > 0, penetration < 0)", "displacement",
                       "nodal (slave surface)", lambda r: r.contact[:, 0]),
}
