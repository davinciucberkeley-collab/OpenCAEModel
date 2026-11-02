"""Parser for CalculiX ASCII result files (.frd).

Fixed-width format: records start with a key (" -1" data, " -2" continuation,
" -3" end of block, " -4" result name, " -5" component). Node blocks are
introduced by "    2C", results by "  100C". The format flag in the block
header selects short (I5 ids) or long (I10 ids) records; values are E12.5 and
may touch each other (e.g. "1.0E+00-2.0E+00"), so fixed-width slicing is used.

Nodal STRESS / TOSTRAIN in .frd are values **extrapolated from integration
points to nodes and averaged** over adjacent elements by CalculiX.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np


@dataclass
class FrdBlock:
    name: str
    components: list[str]
    node_ids: np.ndarray
    values: np.ndarray  # (n, ncomp)
    step_time: float = 0.0


@dataclass
class FrdFile:
    node_ids: np.ndarray
    coords: np.ndarray
    blocks: list[FrdBlock] = field(default_factory=list)

    def get(self, name: str) -> FrdBlock | None:
        hits = [b for b in self.blocks if b.name == name]
        return hits[-1] if hits else None  # last increment


def _values(line: str, start: int, n: int) -> list[float]:
    out = []
    for k in range(n):
        s = line[start + 12 * k: start + 12 * (k + 1)]
        if not s.strip():
            break
        out.append(float(s))
    return out


def read_frd(path: Path) -> FrdFile:
    lines = Path(path).read_text(errors="replace").splitlines()
    i, n = 0, len(lines)
    node_ids, coords = [], []
    blocks: list[FrdBlock] = []
    while i < n:
        ln = lines[i]
        key = ln[:6]
        if key == "    2C":  # nodes
            fmt = int(ln[-1]) if ln.strip()[-1].isdigit() else 1
            w = 10 if fmt == 1 else 5
            i += 1
            while i < n and lines[i].startswith(" -1"):
                r = lines[i]
                node_ids.append(int(r[3:3 + w]))
                coords.append(_values(r, 3 + w, 3))
                i += 1
            continue
        if key == "  100C":  # result block
            hdr = ln
            fmt = int(hdr.split()[-1]) if hdr.split()[-1].isdigit() else 1
            try:
                step_time = float(hdr[12:24])
            except ValueError:
                step_time = 0.0
            w = 10 if fmt == 1 else 5
            i += 1
            name, ncomp = lines[i][5:13].strip(), int(lines[i][13:18])
            i += 1
            comps = []
            while i < n and lines[i].startswith(" -5"):
                comps.append(lines[i][5:13].strip())
                i += 1
            ids, vals = [], []
            cur: list[float] = []
            while i < n and not lines[i].startswith(" -3"):
                r = lines[i]
                if r.startswith(" -1"):
                    if cur:
                        vals.append(cur)
                    ids.append(int(r[3:3 + w]))
                    cur = _values(r, 3 + w, 6)
                elif r.startswith(" -2"):
                    cur += _values(r, 3 + w, 6)
                i += 1
            if cur:
                vals.append(cur)
            arr = np.array([v[:ncomp] for v in vals], dtype=float) if vals else np.zeros((0, ncomp))
            blocks.append(FrdBlock(name, comps[:ncomp], np.array(ids, dtype=np.int64), arr, step_time))
            i += 1
            continue
        if ln.startswith(" 9999"):
            break
        i += 1
    return FrdFile(np.array(node_ids, dtype=np.int64), np.array(coords, dtype=float), blocks)
