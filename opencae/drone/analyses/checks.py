"""Requirement checks: every requirement row becomes pass / conditional / fail / not_evaluated.

Rules (opencae/drone/types.py): a missed target is ``fail`` whatever the evidence; a target met on inputs that
are estimates is at best ``conditional``; ``pass`` needs measured or datasheet inputs. A value that was not
computed is ``not_evaluated`` and says why. Nothing here is flight approval."""
from __future__ import annotations

import operator

from opencae.drone import spec as S
from opencae.drone.templates import default_requirements
from opencae.drone.types import ERROR, NOT_EVALUATED

ID = "checks"
TITLE = "Requirement checks"
OPS = {">=": operator.ge, "<=": operator.le, ">": operator.gt, "<": operator.lt}
# roles an analysis needs parts for (per aircraft type); named in the "why" when no part has the role
ROLE_NEEDS = {"structure": {"multirotor": ["arm"], "fixed_wing": ["wing"], "tailsitter": ["wing"],
                            "vtol": ["wing"]}}
PASS, CONDITIONAL, FAIL = "pass", "conditional", "fail"
VERDICTS = ("not ready", "conditional", "ready for physical tests")


def _fmt(x) -> str:
    return f"{x:.3g}" if isinstance(x, (int, float)) else str(x)


def _missing_roles(analysis: str, spec: dict, aircraft) -> list[str]:
    if aircraft is None:
        return []
    need = ROLE_NEEDS.get(analysis, {}).get(spec.get("type", "multirotor"), [])
    return [r for r in need if not aircraft.role_parts(r)]


def evaluate(spec: dict, analyses: dict, aircraft=None) -> tuple[list[dict], str]:
    """(rows, verdict) for the spec's requirements (or the defaults of its type)."""
    reqs = spec.get("requirements") or default_requirements(spec.get("type", "multirotor"))
    rows = []
    for r in reqs:
        aid, _, mkey = str(r["metric"]).partition(".")
        row = {"id": r["id"], "label": r.get("label", r["id"]), "metric": r["metric"], "op": r["op"],
               "target": r["value"], "value": None, "unit": "", "status": NOT_EVALUATED, "why": ""}
        res = analyses.get(aid)
        mt = (res or {}).get("metrics", {}).get(mkey) if res else None
        if res is None:
            why = f"the {aid} analysis has not been run"
        elif res["status"] == ERROR:
            why = f"the {aid} analysis failed: {res.get('summary', '')}"
        elif res["status"] == NOT_EVALUATED or mt is None or mt.get("value") is None:
            why = res.get("summary") or f"{r['metric']} was not computed"
            if mt is not None and mt.get("value") is None and res["status"] != NOT_EVALUATED:
                why = f"{r['metric']} could not be computed: {res.get('summary', '')}"
        else:
            why = ""
        if why:
            miss = _missing_roles(aid, spec, aircraft)
            if miss:
                why = ("no part has the role '" + "', '".join(miss) + "' (name the parts or set roles in "
                       "drone.json); " + why)
            row["why"] = why[:1].upper() + why[1:] if why else why
            rows.append(row)
            continue
        value, basis = mt["value"], (mt.get("basis") or "")
        row["value"], row["unit"] = value, mt.get("unit", "")
        ok = OPS[r["op"]](value, r["value"])
        weak = (not basis) or "estimate" in basis.replace(",", " ").split()
        txt = f"{_fmt(value)} {row['unit']}".strip() + f" against a target of {r['op']} {_fmt(r['value'])}"
        if not ok:
            row["status"], row["why"] = FAIL, f"Target missed: {txt}."
        elif weak:
            row["status"] = CONDITIONAL
            row["why"] = (f"Target met ({txt}), but it rests on estimated inputs, so it stays conditional until "
                          f"they are replaced by datasheet or measured values.")
        else:
            row["status"], row["why"] = PASS, f"Target met: {txt}."
        rows.append(row)
    return rows, verdict(rows)


def verdict(rows: list[dict]) -> str:
    st = [r["status"] for r in rows]
    if FAIL in st:
        return VERDICTS[0]
    if not any(s in (PASS, CONDITIONAL) for s in st):
        return VERDICTS[0]  # nothing was evaluated
    if CONDITIONAL in st or NOT_EVALUATED in st:
        return VERDICTS[1]
    return VERDICTS[2]
