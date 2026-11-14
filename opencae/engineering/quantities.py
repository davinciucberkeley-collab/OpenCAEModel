"""Engineering quantities with units, provenance and status (the common currency of the platform).

Every value the platform reports is a :class:`Q`: an SI value (or ``None`` when the data are missing),
its unit, the *class* of evidence it rests on, where it came from, an optional relative uncertainty and
a status. Comparisons and roll-ups keep the weakest class of their inputs, so a total built from one
provisional estimate is itself provisional.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

# evidence classes, strongest first (the order is used for "weakest input wins")
USER = "user_input"
SUPPLIER = "supplier_spec"
TEST = "test_measurement"
ANALYTICAL = "analytical"
SIMULATION = "simulation"
REDUCED = "reduced_order"
ESTIMATE = "provisional_estimate"
MISSING = "missing"

CLASSES = {
    TEST: "Physical test measurement",
    SUPPLIER: "Supplier specification",
    USER: "User-defined input",
    SIMULATION: "Numerical simulation",
    ANALYTICAL: "Analytical calculation",
    REDUCED: "Reduced-order prediction",
    ESTIMATE: "Provisional estimate",
    MISSING: "Missing or unsupported data",
}
_RANK = {TEST: 0, SUPPLIER: 1, USER: 1, SIMULATION: 2, ANALYTICAL: 2, REDUCED: 3, ESTIMATE: 4, MISSING: 5}
SHORT = {TEST: "TEST", SUPPLIER: "SUPPLIER", USER: "USER", SIMULATION: "SIM", ANALYTICAL: "CALC",
         REDUCED: "ROM", ESTIMATE: "EST", MISSING: "MISSING"}


@dataclass
class Q:
    value: Optional[float]
    unit: str = ""
    source: str = ESTIMATE  # one of CLASSES
    ref: str = ""  # where it came from (document, datasheet, calculation name)
    uncertainty: Optional[float] = None  # relative, 1 sigma
    note: str = ""
    inputs: list = field(default_factory=list)  # names of the inputs of a calculated value

    @property
    def missing(self) -> bool:
        return self.value is None or self.source == MISSING

    def fmt(self, digits: int = 3, scale: float = 1.0, unit: Optional[str] = None) -> str:
        if self.value is None:
            return "—"
        v = self.value * scale
        s = f"{v:.{digits}g}" if abs(v) < 1e5 else f"{v:.4g}"
        return f"{s} {unit if unit is not None else self.unit}".strip()

    def to_dict(self) -> dict:
        return {"value": self.value, "unit": self.unit, "source": self.source, "ref": self.ref,
                "uncertainty": self.uncertainty, "note": self.note, "inputs": list(self.inputs)}

    @classmethod
    def from_dict(cls, d: dict) -> "Q":
        return cls(d.get("value"), d.get("unit", ""), d.get("source", ESTIMATE), d.get("ref", ""),
                   d.get("uncertainty"), d.get("note", ""), list(d.get("inputs", [])))


def weakest(sources: Iterable[str]) -> str:
    """The weakest evidence class among ``sources`` (a result is no stronger than its weakest input)."""
    srcs = list(sources) or [ESTIMATE]
    return max(srcs, key=lambda s: _RANK.get(s, 4))


def derived(value: Optional[float], unit: str, ref: str, inputs: dict[str, "Q"], kind: str = ANALYTICAL,
            note: str = "") -> Q:
    """A calculated quantity: its class is the calculation kind, demoted to the weakest input class
    when an input is an estimate or missing."""
    ins = [q for q in inputs.values() if isinstance(q, Q)]
    if any(q.missing for q in ins):
        return Q(None, unit, MISSING, ref, note=note or "an input is missing: "
                 + ", ".join(k for k, q in inputs.items() if isinstance(q, Q) and q.missing), inputs=list(inputs))
    src = weakest([kind] + [q.source for q in ins if _RANK.get(q.source, 4) > _RANK[kind]])
    unc = [q.uncertainty for q in ins if q.uncertainty]
    return Q(value, unit, src, ref, uncertainty=(sum(u * u for u in unc) ** 0.5 if unc else None), note=note,
             inputs=list(inputs))
