"""Natural frequencies and mode shapes (linear modal analysis, CalculiX *FREQUENCY).

Solves K φ = ω² M φ with the supports of the study (free-free if there are none).
Loads are ignored (they do not change linear natural frequencies). Mode shapes are
mass-normalised by CalculiX (φᵀ M φ = 1), so their amplitude has no physical meaning;
only the shape and the relative values do.
"""
from __future__ import annotations

import io
import json
import re
from pathlib import Path
from typing import Optional

import numpy as np

from opencae.boundary_conditions.resolve import ERROR, WARNING, Issue, resolve_model
from opencae.physics import register
from opencae.postprocessing.results import QUANTITIES, Quantity, ResultSet, ResultsError
from opencae.solver.calculix.deck import _fmt, section_blocks, write_mesh_block

MODE_QUANTITIES = {k: Quantity(k, QUANTITIES[k].label.replace("Displacement", "Mode shape"), "mode_shape",
                               "nodal", QUANTITIES[k].func) for k in ("U_mag", "U_x", "U_y", "U_z")}
DIRS = ("X", "Y", "Z", "RX", "RY", "RZ")


class ModalResultSet(ResultSet):
    """Mode shapes as frames; ``displacement`` is the shape of the selected mode."""

    kind = "modal"

    def __init__(self, mesh, frequencies, modes, participation=None, effective_mass=None, total_mass=None):
        super().__init__(mesh=mesh, displacement=np.asarray(modes[0]))
        self.frequencies = np.asarray(frequencies, float)  # Hz
        self.modes = np.asarray(modes, float)  # (M, N, 3)
        self.participation = participation  # (M, 6)
        self.effective_mass = effective_mass  # (M, 6) in model mass units
        self.total_mass = total_mass  # (6,)
        self.frame = 0

    def frames(self) -> list[str]:
        return [f"Mode {i + 1}: {f:.6g} Hz" for i, f in enumerate(self.frequencies)]

    def set_frame(self, i: int) -> None:
        self.frame = int(i)
        self.displacement = self.modes[self.frame]
        self._cache.clear()

    def info(self, key):
        return MODE_QUANTITIES[key]

    def available(self) -> list[str]:
        return list(MODE_QUANTITIES)

    def mass_fractions(self) -> Optional[np.ndarray]:
        if self.effective_mass is None or self.total_mass is None:
            return None
        tm = np.where(np.abs(self.total_mass) > 0, self.total_mass, np.nan)
        return self.effective_mass / tm

    def summary(self) -> dict:
        s = {"kind": "modal", "frequencies_hz": self.frequencies.tolist(), "n_modes": len(self.frequencies)}
        fr = self.mass_fractions()
        if fr is not None:
            s["effective_mass_fraction"] = np.nan_to_num(fr).tolist()
            s["cumulative_mass_fraction"] = np.nan_to_num(fr[:, :3].sum(axis=0)).tolist()
        return s

    def save(self, path: Path) -> None:
        d = {"kind": np.array("modal"), "frequencies": self.frequencies, "modes": self.modes}
        for k in ("participation", "effective_mass", "total_mass"):
            v = getattr(self, k)
            if v is not None:
                d[k] = v
        np.savez_compressed(path, **d)

    @classmethod
    def load(cls, path: Path, mesh) -> "ModalResultSet":
        z = np.load(path, allow_pickle=False)
        if z["modes"].shape[1:] != mesh.nodes.shape:
            raise ResultsError("Stored mode shapes do not match the mesh (node count differs).")
        g = lambda k: z[k] if k in z.files else None  # noqa: E731
        return cls(mesh, z["frequencies"], z["modes"], g("participation"), g("effective_mass"), g("total_mass"))


def _table(text: str, title: str, ncol: int) -> list[list[float]]:
    i = text.find(title)
    if i < 0:
        return []
    rows = []
    for ln in text[i + len(title):].splitlines()[1:]:
        p = ln.split()
        if not p:
            if rows:
                break
            continue
        if p[0] == "TOTAL" or not re.match(r"^\d+$", p[0]):
            if rows:
                break
            continue
        try:
            rows.append([float(v) for v in p[1:1 + ncol]])
        except ValueError:
            break
    return rows


def read_modal(session, job_dir: Path, jobname: str) -> ModalResultSet:
    from opencae.solver.calculix.frd import read_frd
    mesh = session.mesh
    frd = read_frd(Path(job_dir) / f"{jobname}.frd")
    N = mesh.n_nodes
    if len(frd.node_ids) != N:
        raise ResultsError(f"Result file has {len(frd.node_ids)} nodes, mesh has {N}.")
    blocks = [b for b in frd.blocks if b.name == "DISP"]
    if not blocks:
        raise ResultsError("No mode shapes (DISP) in the result file - did the eigenvalue solver converge?")
    modes = np.zeros((len(blocks), N, 3))
    for k, b in enumerate(blocks):
        modes[k, b.node_ids - 1] = b.values[:, :3]
    text = (Path(job_dir) / f"{jobname}.dat").read_text(errors="replace")
    eig = _table(text, "E I G E N V A L U E   O U T P U T", 4)
    if len(eig) != len(blocks):
        raise ResultsError(f"{len(eig)} eigenvalues in .dat but {len(blocks)} mode shapes in .frd.")
    freqs = np.array([r[2] for r in eig])  # cycles/time = Hz (time unit is the second)
    part = _table(text, "P A R T I C I P A T I O N   F A C T O R S", 6)
    emm = _table(text, "E F F E C T I V E   M O D A L   M A S S", 6)
    tot = None
    i = text.find("T O T A L   E F F E C T I V E   M A S S")
    if i >= 0:
        for ln in text[i:].splitlines()[1:8]:
            try:
                vals = [float(v) for v in ln.split()[:6]]
            except ValueError:
                continue
            if len(vals) == 6:
                tot = np.array(vals)
                break
    ok = lambda r: np.array(r) if len(r) == len(blocks) else None  # noqa: E731
    return ModalResultSet(mesh, freqs, modes, ok(part), ok(emm), tot)


def write_modal_deck(session, rm) -> str:
    p = session.project
    a = p.analysis
    us = session.units
    mesh = session.mesh
    out = io.StringIO()
    out.write(f"*HEADING\nOpenCAE Studio - {p.name} - {a.title} - units {us.key}\n")
    out.write(write_mesh_block(mesh, p.mesh_settings.solver_element(mesh.element_type)))
    out.write(section_blocks(mesh, session.body_materials(), us))
    out.write("*STEP\n*FREQUENCY\n")
    n = max(1, int(a.n_modes))
    if a.freq_min and a.freq_min > 0:
        out.write(f"{n}, {_fmt(a.freq_min)}\n")
    else:
        out.write(f"{n}\n")
    if rm.spc:
        out.write("*BOUNDARY\n")
        for node in sorted(rm.spc):
            for d, v in sorted(rm.spc[node].items()):
                out.write(f"{node + 1}, {d}, {d}\n")
    out.write("*NODE FILE\nU\n*END STEP\n")
    return out.getvalue()


class ModalPhysics:
    name = "Modal"
    analysis_types = ("modal",)
    equations = [("Undamped free vibration", "M ü + K u = 0  →  (K − ω² M) φ = 0,  f = ω / 2π"),
                 ("Normalisation", "φᵀ M φ = 1 (mass-normalised mode shapes)")]
    assumptions = ["Linear elastic, small vibrations about the undeformed (unloaded) state; no pre-stress.",
                   "No damping; consistent mass matrix from the material densities.",
                   "Mode-shape amplitudes are arbitrary (mass-normalised); compare shapes, not magnitudes.",
                   "Supports are rigid. With no supports the first six modes are rigid-body modes (≈ 0 Hz)."]

    def resolve(self, session, bodies, mesh):
        rm = resolve_model(session.project, bodies, mesh, session.bodies)
        a = session.project.analysis
        if a.n_modes < 1 or a.n_modes > 500:
            rm.issues.append(Issue(ERROR, "number of modes must be between 1 and 500", "Analysis"))
        for b in bodies:
            mat = session.project.material_for_body(b.id)
            if mat is not None and not mat.density:
                rm.issues.append(Issue(ERROR, f"modal analysis needs a density for '{mat.name}'", "Material"))
        if not rm.spc:
            rm.issues.append(Issue(WARNING, "no supports: free-free analysis, the first six modes are rigid-body "
                                            "modes at ~0 Hz", "Constraints"))
        return rm

    def write_input(self, session, job_dir, jobname, rm) -> None:
        (Path(job_dir) / f"{jobname}.inp").write_text(write_modal_deck(session, rm))
        (Path(job_dir) / "resolved_loads.json").write_text(json.dumps({}))

    def read_results(self, session, job_dir, jobname):
        return read_modal(session, Path(job_dir), jobname)

    def load_results(self, path, mesh):
        return ModalResultSet.load(path, mesh)


register(ModalPhysics())
