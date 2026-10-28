"""Parser for CalculiX .dat print output.

Extracts:
* integration-point stresses from ``*EL PRINT ... S``. The column order is
  read from the header ("sxx,syy,szz,sxy,sxz,syz") and normalised to
  (xx, yy, zz, xy, yz, zx).
* totals from ``*NODE PRINT ... TOTALS=ONLY`` (reaction force per set).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

_HDR = re.compile(r"^\s*(?P<what>[a-z ]+?)\s*\((?P<cols>[^)]*)\)\s*for set (?P<set>\S+)")


@dataclass
class DatFile:
    ip_stress: np.ndarray | None = None  # (n, 6) xx yy zz xy yz zx
    ip_elem: np.ndarray | None = None  # element id (1-based) per row
    ip_point: np.ndarray | None = None
    totals: dict[str, np.ndarray] = field(default_factory=dict)  # set -> (fx, fy, fz)


def read_dat(path: Path) -> DatFile:
    out = DatFile()
    lines = Path(path).read_text(errors="replace").splitlines()
    i, n = 0, len(lines)
    while i < n:
        m = _HDR.match(lines[i])
        if not m:
            i += 1
            continue
        what, cols, set_name = m["what"].strip(), [c.strip() for c in m["cols"].split(",")], m["set"]
        i += 1
        rows = []
        while i < n:
            s = lines[i].strip()
            if not s:
                if rows:
                    break
                i += 1
                continue
            if _HDR.match(lines[i]):
                break
            try:
                rows.append([float(v) for v in s.split()])
            except ValueError:
                break
            i += 1
        if what == "stresses" and rows:
            a = np.array(rows)
            idx = {c: k for k, c in enumerate(cols)}
            order = [idx[c] for c in ("sxx", "syy", "szz", "sxy", "syz", "sxz")]
            out.ip_elem = a[:, 0].astype(np.int64)
            out.ip_point = a[:, 1].astype(np.int64)
            out.ip_stress = a[:, order]
        elif what.startswith("total force") and rows:
            out.totals[set_name] = np.array(rows[0][:3])
    return out
