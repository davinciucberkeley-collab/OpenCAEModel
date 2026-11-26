"""Adapters: each project kind's stored results as the hub's JSON shapes.

``list_projects() / project_view() / scene() / plots() / audit() / compare() / motion()`` take a project id (see
``registry``) and a run id (or ``latest``) and return plain dicts and lists (see
``plans/2026-11-26-results-hub/contract.md``). Nothing is computed here that is not in a stored result: a missing
piece gives an empty list and a note that says what is missing.

Kinds: ``drone`` (a folder with drone.json; runs from results/runs.jsonl, snapshots in results/history/),
``studio`` (an .ocae project; one run per study result folder) and ``k09l-study`` (the K09-L engineering study).
"""
from __future__ import annotations

import base64
import json
import re
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

import numpy as np

from opencae.hub import HubError, registry
from opencae.hub.snapshots import snapshot_path

MAX_TRIANGLES = 200_000
STATUSES = ("ok", "watch", "conditional", "fail", "error", "not_evaluated")
_RANK_BASIS = {"estimate": 0, "datasheet": 1, "measured": 2, "user": 2}
LATEST_ONLY = "Fields are kept for the latest run only; this older run shows its numbers and audit."
_DISCIPLINE = {"mass": "mass", "propulsion": "propulsion", "structure": "structural", "cfd": "cfd",
               "flight": "flight"}
_HEADLINE = {"mass": "mass_kg", "propulsion": "tw_ratio", "structure": "safety_factor", "cfd": "ld",
             "flight": "settle_time_s"}
_ORDER = ("mass", "propulsion", "structure", "cfd", "flight")
_VERDICT = {"ready for physical tests": "pass", "conditional": "conditional", "not ready": "not ready",
            "pass": "pass", "fail": "fail"}
_CHECK_STATUS = {"pass": "ok", "conditional": "conditional", "fail": "fail", "not_evaluated": "not_evaluated"}


# ------------------------------------------------------------------------------------------------ helpers
def _b64(a, dtype="<f4") -> str:
    return base64.b64encode(np.ascontiguousarray(np.asarray(a), dtype=np.dtype(dtype)).tobytes()).decode()


def _num(v) -> Optional[float]:
    if isinstance(v, bool) or v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if np.isfinite(f) else None


def _iso(ts) -> str:
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(ts)))
    except (TypeError, ValueError, OverflowError):
        return ""


def _iso_text(s) -> str:
    """'2026-11-26 10:03:00' (UTC, as the pipeline writes it) -> '2026-11-26T10:03:00Z'; anything else unchanged."""
    s = str(s or "")
    return s.replace(" ", "T") + "Z" if re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d", s) else s


def _read_json(p: Path):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _weakest(bases) -> str:
    """The weakest evidence basis among the given strings (a string may list several, e.g. 'estimate datasheet')."""
    toks = []
    for b in bases:
        toks += [t for t in re.split(r"[\s,;+]+", str(b or "")) if t]
    known = [t for t in toks if t in _RANK_BASIS]
    if known:
        return min(known, key=lambda t: _RANK_BASIS[t])
    return toks[0] if toks else ""


def _value(v):
    """A metric value for a card: a number or string stays; a flat list of numbers becomes text; a nested list
    (a matrix) has no card form (None)."""
    if isinstance(v, (list, tuple)):
        if v and all(_num(x) is not None for x in v):
            return ", ".join(f"{float(x):.4g}" for x in v)
        return None
    if isinstance(v, bool):
        return v
    return _num(v) if isinstance(v, (int, float)) else v


def _metric_row(key, m) -> Optional[dict]:
    if not isinstance(m, dict):
        return None
    v = _value(m.get("value"))
    return {"label": m.get("label") or key.replace("_", " "), "value": v, "unit": m.get("unit") or "",
            "status": m.get("status") if m.get("status") in STATUSES else "ok", "basis": m.get("basis") or ""}


def _entry(pid: str) -> dict:
    if not pid:
        raise HubError("pid is required", 400)
    e = registry.find(pid)
    if e is None:
        raise HubError(f"Unknown project '{pid}'", 404)
    return e


def _decimate(P, T, budget: int):
    """(points, triangles) keeping about ``budget`` triangles; unused vertices dropped."""
    T = np.asarray(T)
    if len(T) > budget > 0:
        T = T[::int(np.ceil(len(T) / budget))]
    used, inv = np.unique(T, return_inverse=True)
    return np.asarray(P, dtype=np.float32)[used], inv.reshape(-1, 3).astype(np.int32)


def _range(v):
    v = np.asarray(v, dtype=np.float64)
    v = v[np.isfinite(v)]
    return (float(v.min()), float(v.max())) if len(v) else (None, None)


def _scale_hint(points, disp) -> Optional[float]:
    """A display factor that makes the largest displacement about 10 % of the part size (a viewing aid)."""
    if disp is None or not len(points):
        return None
    mx = float(np.linalg.norm(np.asarray(disp, np.float64), axis=1).max())
    diag = float(np.linalg.norm(np.ptp(np.asarray(points, np.float64), axis=0)))
    if mx <= 0 or diag <= 0:
        return None
    return float(f"{0.1 * diag / mx:.2g}")


def _plot(pid_, title, disc, xl, xu, yl, yu, series, note="", log=False) -> dict:
    return {"id": pid_, "title": title, "discipline": disc, "x": {"label": xl, "unit": xu},
            "y": {"label": yl, "unit": yu, "log": bool(log)}, "series": series, "note": note}


def _file_rows(dirpath: Path, names) -> list:
    out = []
    for n in names:
        p = Path(dirpath) / n
        if p.is_file():
            out.append({"name": n, "bytes": int(p.stat().st_size)})
    return out


def _motion_module():
    try:
        from opencae.hub import motion
        return motion
    except Exception:  # noqa: BLE001 - V3's module is optional here: the answer then says so
        return None


# ================================================================================================== drone
_HIST = "history"


def _drone_rows(d: Path) -> list:
    rows = []
    try:
        for line in (d / "results" / "runs.jsonl").read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if isinstance(r, dict) and r.get("run_id"):
                    rows.append(r)
    except OSError:
        pass
    return rows


def _drone_variants(d: Path) -> list:
    root = d / "results"
    if not root.is_dir():
        return []
    return sorted((p.name for p in root.iterdir() if p.is_dir() and p.name != _HIST and (p / "results.json").is_file()),
                  key=lambda n: (n != "baseline", n))


def _drone_run_list(d: Path) -> list:
    """Runs oldest first. Without a runs.jsonl, one synthetic run per variant that has a results.json."""
    rows = _drone_rows(d)
    if rows:
        return rows
    out = []
    for v in _drone_variants(d):
        f = d / "results" / v / "results.json"
        res = _read_json(f) or {}
        out.append({"run_id": f"current-{v}", "variant": v, "started": res.get("generated") or "",
                    "finished": res.get("generated") or "", "duration_s": None, "only": [],
                    "verdict": res.get("verdict"), "status": "done", "synthetic": True})
    return out


def _verdict_word(v) -> Optional[str]:
    return _VERDICT.get(str(v).lower()) if v else None


class _DroneRun:
    """One resolved run: its row, its results dict (or None), whether the variant folder is its live copy."""

    def __init__(self, d: Path, row: Optional[dict], rows: list):
        self.dir, self.row = d, row
        self.results, self.live, self.out_dir = None, False, None
        if row is None:
            return
        variant = row.get("variant") or "baseline"
        last = [r for r in rows if (r.get("variant") or "baseline") == variant][-1]
        live_file = d / "results" / variant / "results.json"
        if last is row and live_file.is_file():
            self.results, self.live, self.out_dir = _read_json(live_file), True, live_file.parent
        else:
            snap = snapshot_path(d, row["run_id"])
            if snap.is_file():
                self.results, self.out_dir = _read_json(snap), snap.parent
        self.variant = variant


def _drone_resolve(entry: dict, run: str) -> _DroneRun:
    d = Path(entry["path"])
    rows = _drone_run_list(d)
    if not rows:
        return _DroneRun(d, None, rows)
    if not run or run == "latest":
        row = rows[-1]
    else:
        row = next((r for r in rows if r["run_id"] == run), None)
        if row is None:
            raise HubError(f"No run '{run}' in this project", 404)
    return _DroneRun(d, row, rows)


def _drone_card(aid: str, res: dict) -> dict:
    ms = [r for r in (_metric_row(k, m) for k, m in (res.get("metrics") or {}).items()) if r and r["value"] is not None]
    status = res.get("status") if res.get("status") in STATUSES else "not_evaluated"
    head = None
    if status not in ("error", "not_evaluated"):
        key = _HEADLINE.get(aid)
        m = (res.get("metrics") or {}).get(key) if key else None
        r = _metric_row(key, m) if m else (ms[0] if ms else None)
        if r and r["value"] is not None:
            head = {"value": r["value"], "unit": r["unit"], "label": r["label"]}
    text = res.get("summary") or ""
    if res.get("missing"):
        text += " Missing: " + ", ".join(map(str, res["missing"])) + "."
    return {"id": aid, "title": res.get("title") or aid, "discipline": _DISCIPLINE.get(aid, aid), "status": status,
            "basis": _weakest(r["basis"] for r in ms), "headline": head, "text": text.strip(), "metrics": ms}


def _checks_card(results: dict) -> Optional[dict]:
    rows = results.get("checks") or []
    if not rows:
        return None
    verdict = results.get("verdict")
    status = {"pass": "ok", "conditional": "conditional", "not ready": "fail"}.get(_verdict_word(verdict) or "", "watch")
    ms = [{"label": r.get("label") or r.get("id"), "value": _value(r.get("value")), "unit": r.get("unit") or "",
           "status": _CHECK_STATUS.get(r.get("status"), "not_evaluated"), "basis": "",
           "note": r.get("why") or "", "limit": r.get("target"), "op": r.get("op")} for r in rows]
    met = sum(1 for r in rows if r.get("status") in ("pass", "conditional"))
    return {"id": "checks", "title": "Requirement checks", "discipline": "requirements", "status": status, "basis": "",
            "headline": {"value": str(verdict or ""), "unit": "", "label": "Verdict"},
            "text": f"{met} of {len(rows)} requirement checks are met or conditional on estimates; the verdict "
                    f"is '{verdict}'.", "metrics": ms}


def _drone_notice(r: _DroneRun) -> str:
    if r.row is None:
        return "No results yet: run the analyses of this project."
    parts = []
    if r.results is None:
        parts.append("The stored results of this run are not available.")
    elif not r.live:
        parts.append(LATEST_ONLY)
    row = r.row
    if row.get("status") == "error" and row.get("error"):
        parts.append(f"This run stopped with an error: {row['error']}")
    elif row.get("status") == "cancelled":
        parts.append("This run was stopped before it finished.")
    return " ".join(parts)


def _drone_runs_out(rows: list) -> list:
    return [{"run": r["run_id"], "started": _iso_text(r.get("started")), "verdict": _verdict_word(r.get("verdict")),
             "only": list(r.get("only") or []), "duration_s": r.get("duration_s"),
             "variant": r.get("variant") or "baseline", "status": r.get("status") or ""} for r in reversed(rows)]


def _drone_summary(entry: dict) -> dict:
    d = Path(entry["path"])
    rows = _drone_run_list(d)
    out = {"updated": "", "verdict": None, "runs": len(rows), "disciplines": []}
    if not rows:
        return out
    last = rows[-1]
    out["updated"] = _iso_text(last.get("finished") or last.get("started"))
    out["verdict"] = _verdict_word(last.get("verdict"))
    res = _DroneRun(d, last, rows).results or {}
    out["disciplines"] = [_DISCIPLINE.get(a, a) for a in _ORDER if (res.get("analyses") or {}).get(a, {}).get("status")
                          not in (None, "not_evaluated")]
    if not out["updated"]:
        f = d / "results" / (last.get("variant") or "baseline") / "results.json"
        out["updated"] = _iso(f.stat().st_mtime) if f.exists() else ""
    return out


def _drone_project(entry: dict, run: str) -> dict:
    r = _drone_resolve(entry, run)
    rows = _drone_run_list(Path(entry["path"]))
    out = {"id": entry["id"], "kind": "drone", "name": entry["name"], "run": r.row["run_id"] if r.row else None,
           "runs": _drone_runs_out(rows), "cards": [], "analyses": [], "notice": _drone_notice(r)}
    res = r.results
    if not res:
        return out
    an = res.get("analyses") or {}
    for aid in [a for a in _ORDER if a in an] + [a for a in an if a not in _ORDER]:
        c = _drone_card(aid, an[aid])
        out["cards"].append(c)
        out["analyses"].append({"id": aid, "title": c["title"], "discipline": c["discipline"], "status": c["status"],
                                "summary": an[aid].get("summary") or ""})
    cc = _checks_card(res)
    if cc:
        out["cards"].append(cc)
    return out


# ---- drone: geometry and scene
def _drone_parts(d: Path, variant: str, notes: list) -> list:
    for f in (d / "results" / variant / "geometry.npz", d / "results" / "baseline" / "geometry.npz",
              d / "results" / "geometry_cache.npz"):
        if not f.is_file():
            continue
        try:
            with np.load(f, allow_pickle=False) as z:
                names = [str(s) for s in z["names"]]
                roles = [str(s) for s in z["roles"]] if "roles" in z.files else ["other"] * len(names)
                P = [z[f"p{i}"] for i in range(len(names))]
                T = [z[f"t{i}"] for i in range(len(names))]
        except (OSError, KeyError, ValueError) as exc:
            notes.append(f"Aircraft geometry could not be read ({f.name}): {exc}")
            return []
        total = sum(len(t) for t in T) or 1
        out = []
        for n, ro, p, t in zip(names, roles, P, T):
            p2, t2 = _decimate(p, t, max(1, int(MAX_TRIANGLES * len(t) / total)))
            out.append({"name": n, "role": ro, "points": _b64(p2), "triangles": _b64(t2, "<i4")})
        return out
    notes.append("No aircraft geometry is stored for this project yet (run any analysis to write it).")
    return []


def _surface_field(fdir: Path, fname: str) -> dict:
    from opencae.drone import types as T
    z = T.load_field(fdir / fname)
    if "points" not in z and "mesh" in z:
        base = T.load_field(fdir / str(z["mesh"]))
        z["points"], z["triangles"] = base["points"], base["triangles"]
    return z


def _flow_arrays(z: dict) -> tuple:
    """(slices, streamlines) from a field dict that carries slice_* / stream_* arrays."""
    slices, sl = [], None
    if "slice_points" in z and "slice_offsets" in z and len(z["slice_points"]):
        P, V, Tr = z["slice_points"], z.get("slice_values"), np.asarray(z["slice_triangles"])
        off = np.asarray(z["slice_offsets"], dtype=np.int64)
        names = [str(s) for s in z.get("slice_names", [])]
        for i in range(len(off) - 1):
            lo, hi = int(off[i]), int(off[i + 1])
            sel = Tr[(Tr[:, 0] >= lo) & (Tr[:, 0] < hi)] - lo
            if hi <= lo or not len(sel):
                continue
            slices.append({"name": names[i] if i < len(names) else f"slice {i + 1}", "points": _b64(P[lo:hi]),
                           "triangles": _b64(sel, "<i4"), "values": _b64(V[lo:hi]), "label": "speed", "unit": "m/s"})
    if "stream_points" in z and len(z["stream_points"]):
        sl = {"points": _b64(z["stream_points"]), "offsets": _b64(z["stream_offsets"], "<i4"),
              "values": _b64(z["stream_speed"]), "unit": "m/s"}
    return slices, sl


def _drone_scene(entry: dict, run: str) -> dict:
    r = _drone_resolve(entry, run)
    out = {"units": "mm", "parts": [], "fields": [], "slices": [], "streamlines": None, "notes": []}
    notes = out["notes"]
    d = Path(entry["path"])
    if r.row is None or r.results is None:
        out["parts"] = _drone_parts(d, (r.row or {}).get("variant") or "baseline", notes) if r.row else []
        notes.append("No results yet: nothing to draw." if r.row is None else
                     "The stored results of this run are not available.")
        return out
    out["parts"] = _drone_parts(d, r.variant, notes)
    if not r.live:
        notes.append(LATEST_ONLY)
        return out
    first_cfd = None
    for aid in ("cfd", "structure"):
        res = (r.results.get("analyses") or {}).get(aid)
        if res is None:
            notes.append(f"{'CFD' if aid == 'cfd' else 'Structure'} was not run in this run.")
            continue
        flds = res.get("fields") or []
        if not flds:
            if res.get("status") in ("error", "not_evaluated"):
                notes.append(str(res.get("summary") or f"{aid} produced no result.")[:400])
            continue
        disc = _DISCIPLINE[aid]
        for f in flds:
            fname = f.get("file")
            if not fname or not (r.out_dir / fname).is_file():
                notes.append(f"Field file {fname} is missing from the results folder.")
                continue
            try:
                z = _surface_field(r.out_dir, fname)
            except Exception as exc:  # noqa: BLE001
                notes.append(f"Field file {fname} could not be read: {exc}")
                continue
            P, Tr, vals = z["points"], z["triangles"], z["values"]
            lo, hi = _range(vals)
            fid = Path(fname).stem
            label = str(f.get("quantity") or fid) + (f" ({f['part']})" if f.get("part") else "")
            item = {"id": fid, "label": label, "discipline": disc, "unit": f.get("unit") or "",
                    "quantity": f.get("key") or "", "mesh": "own", "points": _b64(P), "triangles": _b64(Tr, "<i4"),
                    "values": _b64(vals), "min": lo, "max": hi}
            if f.get("part"):
                item["part"] = f["part"]
            if z.get("displacement") is not None:
                item["displacement"] = _b64(z["displacement"])
                item["scale_hint"] = _scale_hint(P, z["displacement"])
            out["fields"].append(item)
            if aid == "cfd":
                if first_cfd is None:
                    first_cfd = fid
                elif "mesh" in z:  # a values-only file: it shares the surface of the first CFD field
                    item["shares_mesh_with"] = first_cfd
            if "slice_points" in z or "stream_points" in z:
                out["slices"], out["streamlines"] = _flow_arrays(z)
        probs = (res.get("extra") or {}).get("postprocess_problems") or []
        for p in probs:
            notes.append(f"CFD post-processing: {str(p)[:300]}")
        if aid == "cfd" and not out["slices"] and out["fields"] and not probs:
            notes.append("This CFD run stored no flow slices or streamlines.")
    return out


# ---- drone: plots
_RES_RE = re.compile(r"Solving for (\w+), Initial residual = ([-+0-9.eE]+)")


def _parse_residuals(log: Path) -> dict:
    """{field: ([step], [initial residual])} from a simpleFoam log (last solve of a field per step)."""
    out: dict = {}
    step = 0
    try:
        lines = log.read_text(errors="replace").splitlines()
    except OSError:
        return {}
    for ln in lines:
        if ln.startswith("Time = "):
            try:
                step = int(float(ln.split("=")[1].strip().rstrip("s")))
            except ValueError:
                step += 1
            continue
        m = _RES_RE.search(ln)
        if m and step:
            xs, ys = out.setdefault(m.group(1), ([], []))
            v = float(m.group(2))
            if xs and xs[-1] == step:
                ys[-1] = v
            else:
                xs.append(step)
                ys.append(v)
    return out


def _find_case_log(out_dir: Path) -> Optional[Path]:
    for p in sorted(Path(out_dir).rglob("log.simpleFoam")):
        return p
    return None


def _drone_plots(entry: dict, run: str) -> list:
    r = _drone_resolve(entry, run)
    out = []
    if r.results is None:
        return out
    an = r.results.get("analyses") or {}
    cfd = an.get("cfd") or {}
    ex = cfd.get("extra") or {}
    hist = ex.get("history")
    if hist and cfd.get("status") != "error":
        H = np.asarray(hist, dtype=float)
        if H.ndim == 2 and H.shape[1] >= 3:
            out.append(_plot("cfd_coefficients", "Lift and drag coefficients", "cfd", "Iteration", "", "Coefficient",
                             "-", [{"name": "C_L", "x": H[:, 0].tolist(), "y": H[:, 1].tolist()},
                                   {"name": "C_D", "x": H[:, 0].tolist(), "y": H[:, 2].tolist()}],
                             "Forces evaluated at each stored solver write."))
    sol = cfd.get("solver") or {}
    log = _find_case_log(r.out_dir) if (r.live and r.out_dir) else None
    full = _parse_residuals(log) if log else {}
    if full:
        out.append(_plot("cfd_residuals", "Solver residuals", "cfd", "Iteration", "", "Initial residual", "-",
                         [{"name": k, "x": v[0], "y": v[1]} for k, v in full.items()],
                         "Full history parsed from the case log kept with this run.", log=True))
    elif ex.get("residual_history") and cfd.get("status") != "error":
        out.append(_plot("cfd_residuals", "Solver residuals", "cfd", "Iteration", "", "Initial residual", "-",
                         [{"name": k, "x": v[0], "y": v[1]} for k, v in ex["residual_history"].items()],
                         "Residual history stored with the result (thinned to at most 400 points per field).",
                         log=True))
    elif sol.get("residuals_final"):
        n = sol.get("iterations") or 0
        out.append(_plot("cfd_residuals", "Solver residuals (final)", "cfd", "Iteration", "", "Initial residual", "-",
                         [{"name": k, "x": [n], "y": [v]} for k, v in sol["residuals_final"].items()],
                         "Only the final residuals were stored (the case log is deleted after a run): one point per "
                         "field at the last iteration.", log=True))
    fl = an.get("flight") or {}
    fx = fl.get("extra") or {}
    if fl.get("status") != "error" and fx.get("t"):
        t = np.asarray(fx["t"], float)
        att, pos, mot = (np.asarray(fx.get(k) or [], float) for k in ("att", "pos", "motor"))
        if att.ndim == 2 and len(att) == len(t):
            out.append(_plot("flight_attitude", "Hover: attitude after the kick", "flight", "Time", "s", "Angle", "deg",
                             [{"name": n, "x": t.tolist(), "y": att[:, i].tolist()}
                              for i, n in enumerate(("roll", "pitch", "yaw"))],
                             "Six-degree-of-freedom hover simulation; motor lag and rotor torque are estimates."))
        if pos.ndim == 2 and len(pos) == len(t):
            out.append(_plot("flight_position", "Hover: position", "flight", "Time", "s", "Position", "m",
                             [{"name": n, "x": t.tolist(), "y": pos[:, i].tolist()} for i, n in enumerate("xyz")], ""))
        if mot.ndim == 2 and len(mot) == len(t):
            out.append(_plot("flight_motors", "Hover: motor commands", "flight", "Time", "s", "Command", "-",
                             [{"name": f"motor {i + 1}", "x": t.tolist(), "y": mot[:, i].tolist()}
                              for i in range(mot.shape[1])], ""))
    return out


# ---- drone: audit
def _leaves(node, prefix, out, cap=60):
    if len(out) >= cap:
        return
    if isinstance(node, dict) and "value" in node and not isinstance(node["value"], dict):
        out.append({"name": prefix, "value": node.get("value"), "unit": node.get("unit") or "",
                    "status": node.get("status") or "", "source": node.get("source") or ""})
    elif isinstance(node, dict):
        for k, v in node.items():
            _leaves(v, f"{prefix}.{k}" if prefix else k, out, cap)
    elif isinstance(node, (list,)):
        out.append({"name": prefix, "value": json.dumps(node)[:200], "unit": "", "status": "", "source": ""})
    else:
        out.append({"name": prefix, "value": node, "unit": "", "status": "", "source": ""})


def _drone_inputs(spec: dict, aid: str) -> list:
    keys = [f"analyses.{aid}"]
    try:
        from opencae.drone import pipeline
        pipeline._refresh()
        mod = pipeline.REGISTRY.get(aid)
        if mod is not None:
            keys = list(getattr(mod, "NEEDS", keys))
    except Exception:  # noqa: BLE001 - the inputs table then lists the analysis' own settings
        pass
    out, seen = [], set()
    for k in keys:
        cur = spec
        for part in k.split("."):
            cur = cur.get(part) if isinstance(cur, dict) else None
            if cur is None:
                break
        if cur is None or k in seen:
            continue
        seen.add(k)
        _leaves(cur, k, out)
    return out


def _drone_audit(entry: dict, run: str) -> list:
    r = _drone_resolve(entry, run)
    if r.results is None:
        return []
    res_all = r.results
    spec = res_all.get("spec_used") or {}
    an = res_all.get("analyses") or {}
    rows = res_all.get("checks") or []
    out = []
    for aid in [a for a in _ORDER if a in an] + [a for a in an if a not in _ORDER]:
        res = an[aid]
        sol = dict(res.get("solver") or {})
        ex = res.get("extra") or {}
        resid = sol.pop("residuals_final", None)
        conv = {k: sol[k] for k in ("converged", "iterations", "iterations_requested", "converged_by_residuals",
                                    "force_change_last_10pct") if k in sol}
        if resid is not None:
            conv["residuals_final"] = resid
        mesh: dict = {}
        if isinstance(ex.get("mesh"), dict):
            mesh.update(ex["mesh"])
        if aid == "cfd":
            for k in ("y_plus", "separated_pct"):
                if ex.get(k) is not None:
                    mesh[k] = ex[k]
            if "checkMesh_ok" in sol:
                mesh["checkMesh_ok"] = sol["checkMesh_ok"]
            if sol.get("cells") and "cells" not in mesh:
                mesh["cells"] = sol["cells"]
        elif aid == "structure" and ex.get("parts"):
            mesh = {"elements": sol.get("cells"), "per_part": ex["parts"]}
        problems = [str(p) for p in (ex.get("postprocess_problems") or [])]
        if res.get("status") in ("error", "not_evaluated") and res.get("summary"):
            problems.insert(0, str(res["summary"]))
        problems += [f"Missing input: {m}" for m in (res.get("missing") or [])]
        checks = []
        for c in rows:
            if str(c.get("metric", "")).partition(".")[0] == aid:
                st = c.get("status")
                checks.append({"name": c.get("label") or c.get("id"), "value": c.get("value"),
                               "limit": c.get("target"), "unit": c.get("unit") or "", "op": c.get("op"),
                               "ok": True if st == "pass" else False if st == "fail" else None,
                               "status": st, "why": c.get("why") or ""})
        names = [f.get("file") for f in (res.get("fields") or []) if f.get("file")]
        files = _file_rows(r.out_dir, names + ["results.json"]) if r.out_dir else []
        if not r.live and r.out_dir:
            files = _file_rows(r.out_dir, ["results.json"])
        out.append({"analysis": aid, "title": res.get("title") or aid, "status": res.get("status"),
                    "solver": sol or ({"runtime_s": ex.get("runtime_s")} if ex.get("runtime_s") is not None else {}),
                    "mesh": mesh, "inputs": _drone_inputs(spec, aid), "checks": checks, "convergence": conv,
                    "assumptions": [str(a) for a in (ex.get("assumptions") or [])], "problems": problems,
                    "log_tail": res.get("log") or "", "files": files})
    return out


# ================================================================================================== studio
_STUDIO_CACHE: dict = {}
_STUDIO_LOCK = threading.Lock()
_FEA_KEYS = ("U_mag", "S_vm", "S_p1", "S_p3", "E_eqv")
_CFD_KEYS = ("V_mag", "P", "P_total", "mu_t")
_STUDIO_DISC = {"mbd": "mbd", "linear_static": "structural", "thermal_stress": "structural", "modal": "modal"}


def _studio_load(entry: dict):
    from opencae.project.io import data_dir_for, load_project
    p = Path(entry["path"])
    try:
        return load_project(p), data_dir_for(p)
    except Exception as exc:  # noqa: BLE001
        raise HubError(f"The project file could not be read: {exc}", 500)


def _study_disc(study) -> str:
    t = study.analysis.analysis_type
    if t.startswith("cfd"):
        return "cfd"
    return _STUDIO_DISC.get(t, "thermal" if t.startswith("thermal") else t)


def _studio_runs(project, dd: Path) -> list:
    rows = []
    for st in project.studies:
        rd = st.results.directory
        if not (st.results.completed and rd and (dd / rd).is_dir()):
            continue
        mt = max([(dd / rd).stat().st_mtime] + [f.stat().st_mtime for f in (dd / rd).glob("*") if f.is_file()])
        rows.append({"run": f"{st.id}@{int(mt)}", "study": st, "mtime": mt,
                     "started": st.results.finished_at and _iso_text(st.results.finished_at) or _iso(mt)})
    return sorted(rows, key=lambda r: -r["mtime"])


def _studio_resolve(entry: dict, run: str):
    project, dd = _studio_load(entry)
    rows = _studio_runs(project, dd)
    if not rows:
        return project, dd, rows, None
    if not run or run == "latest":
        return project, dd, rows, rows[0]
    row = next((r for r in rows if r["run"] == run or r["study"].id == run), None)
    if row is None:
        raise HubError(f"No run '{run}' in this project", 404)
    return project, dd, rows, row


def _studio_runs_out(rows: list) -> list:
    return [{"run": r["run"], "started": r["started"], "verdict": None, "only": [r["study"].name],
             "duration_s": r["study"].results.wall_time_s or None, "study": r["study"].name,
             "type": r["study"].analysis.analysis_type} for r in rows]


def _studio_summary(entry: dict) -> dict:
    project, dd = _studio_load(entry)
    rows = _studio_runs(project, dd)
    f = Path(entry["path"])
    upd = rows[0]["mtime"] if rows else f.stat().st_mtime
    discs = []
    for r in rows:
        dsc = _study_disc(r["study"])
        if dsc not in discs:
            discs.append(dsc)
    return {"updated": _iso(upd), "verdict": None, "runs": len(rows), "disciplines": discs}


def _mbd_load(dd: Path, study):
    from opencae.mbd.results import MBDResult
    return MBDResult.load(dd / study.results.directory)


def _mbd_card(study, res) -> dict:
    meta = res.meta
    units = meta.get("units") or {}
    lenu = units.get("length", "mm")
    viol = _num(meta.get("constraint_violation_max"))
    warns = list(meta.get("warnings") or [])
    ms = [{"label": "Simulated time", "value": float(res.t[-1]), "unit": "s", "status": "ok", "basis": ""},
          {"label": "Output samples", "value": int(len(res.t)), "unit": "", "status": "ok", "basis": ""},
          {"label": "Constraint violation (max)", "value": viol, "unit": lenu,
           "status": "ok" if viol is not None and viol < 1e-6 else "watch", "basis": ""}]
    for j in meta.get("joints", []):
        jid = j["id"]
        q = res.joint_q.get(jid)
        eff = res.joint_effort.get(jid)
        qunit = "rad" if j.get("kind") == "revolute" else lenu
        if q is not None and len(q):
            ms.append({"label": f"{j['name']}: peak coordinate", "value": float(np.abs(q).max()), "unit": qunit,
                       "status": "ok", "basis": ""})
        if eff is not None and len(eff) and float(np.abs(eff).max()) > 0:
            ms.append({"label": f"{j['name']}: peak actuator effort", "value": float(np.abs(eff).max()),
                       "unit": "N m" if j.get("kind") == "revolute" else "N", "status": "ok", "basis": ""})
    drivers = "; ".join(f"{d['name']} ({d['kind']})" for d in meta.get("drivers", [])) or "no drivers"
    text = (f"Multibody study '{study.name}' simulated {float(res.t[-1]):g} s with "
            f"{(meta.get('backend') or {}).get('name', 'MuJoCo')}; drivers: {drivers}.")
    if warns:
        text += " Solver warnings: " + "; ".join(map(str, warns[:3])) + "."
    return {"id": study.id, "title": study.name, "discipline": "mbd", "status": "watch" if (warns or ms[2]["status"]
                                                                                          == "watch") else "ok",
            "basis": "", "headline": {"value": float(res.t[-1]), "unit": "s", "label": "Simulated time"},
            "text": text, "metrics": ms}


_UNIT_BY_KEY = (("von_mises", "stress"), ("principal", "stress"), ("stress", "stress"), ("displacement", "length"),
                ("_ux", "length"), ("_uy", "length"), ("_uz", "length"), ("velocity", "m/s"), ("pressure", "Pa"))


def _fea_unit(key: str, us) -> str:
    k = key.lower()
    for frag, kind in _UNIT_BY_KEY:
        if frag in k:
            return us.stress if kind == "stress" else us.length if kind == "length" else kind
    return ""


def _fea_card(study, project) -> dict:
    from opencae.utilities.units import get_unit_system
    us = get_unit_system(project.unit_system)
    s = study.results.summary or {}
    ms = []
    for k, v in s.items():
        n = _num(v)
        if n is None or k.endswith("_node"):
            continue
        ms.append({"label": k.replace("_", " ").capitalize(), "value": n, "unit": _fea_unit(k, us), "status": "ok",
                   "basis": ""})
    head = next((m for m in ms if "von mises" in m["label"].lower()), ms[0] if ms else None)
    text = (f"{study.name}: {study.analysis.analysis_type.replace('_', ' ')} solved with "
            f"{study.results.solver_version or 'the study solver'}. No pass/fail limit is set in this study: compare "
            f"the numbers with the material allowable.")
    return {"id": study.id, "title": study.name, "discipline": _study_disc(study), "status": "ok", "basis": "",
            "headline": {"value": head["value"], "unit": head["unit"], "label": head["label"]} if head else None,
            "text": text, "metrics": ms}


def _studio_project(entry: dict, run: str) -> dict:
    project, dd, rows, row = _studio_resolve(entry, run)
    out = {"id": entry["id"], "kind": "studio", "name": entry["name"], "run": row["run"] if row else None,
           "runs": _studio_runs_out(rows), "cards": [], "analyses": [],
           "notice": "" if row else "No results yet: run a study of this project."}
    if row is None:
        return out
    st = row["study"]
    try:
        card = _mbd_card(st, _mbd_load(dd, st)) if st.analysis.analysis_type == "mbd" else _fea_card(st, project)
    except Exception as exc:  # noqa: BLE001 - shown, never hidden
        card = {"id": st.id, "title": st.name, "discipline": _study_disc(st), "status": "error", "basis": "",
                "headline": None, "text": f"The stored results could not be read: {exc}", "metrics": []}
    out["cards"].append(card)
    out["analyses"].append({"id": st.id, "title": st.name, "discipline": card["discipline"], "status": card["status"],
                            "summary": card["text"]})
    return out


def _studio_geometry(entry: dict, project) -> tuple:
    """(parts, note): body meshes of the project (mm). The project is opened from a temporary copy so the shipped
    example folders are never written to."""
    key = (entry["path"], Path(entry["path"]).stat().st_mtime_ns)
    with _STUDIO_LOCK:
        if key in _STUDIO_CACHE:
            return _STUDIO_CACHE[key]
    src = Path(entry["path"])
    tmp = Path(tempfile.mkdtemp(prefix="hubgeo_"))
    try:
        shutil.copy(src, tmp / src.name)
        sd = src.with_name(src.stem + "_data")
        if (sd / "geometry").is_dir():
            shutil.copytree(sd / "geometry", tmp / (src.stem + "_data") / "geometry")
        from opencae.app.session import Session
        s = Session()
        s.open(tmp / src.name)
        scale = 1000.0 if s.units.length == "m" else 1.0
        parts = []
        for b in s.bodies.values():
            t = b.tessellation
            if t is None or not len(t.triangles):
                continue
            P, Tr = _decimate(np.asarray(t.points) * scale, t.triangles, MAX_TRIANGLES)
            parts.append({"name": b.name, "role": "body", "points": _b64(P), "triangles": _b64(Tr, "<i4")})
        res = (parts, "" if parts else "The project has no solid bodies to draw.")
    except Exception as exc:  # noqa: BLE001
        res = ([], f"The project geometry could not be built: {exc}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    with _STUDIO_LOCK:
        _STUDIO_CACHE[key] = res
    return res


def _studio_fields(project, dd: Path, study, out: dict) -> None:
    from opencae.meshing.mesh_data import FEMesh
    from opencae.physics import physics_for
    from opencae.utilities.units import get_unit_system
    us = get_unit_system(project.unit_system)
    scale = 1000.0 if us.length == "m" else 1.0
    notes = out["notes"]
    if not (study.mesh.file and (dd / study.mesh.file).is_file()):
        notes.append(f"{study.name}: the mesh file is missing, so no contours can be drawn.")
        return
    mesh = FEMesh.load(dd / study.mesh.file)
    f = dd / study.results.directory / "results.npz"
    if not f.is_file():
        notes.append(f"{study.name}: results.npz is missing (the solver output was not converted), no contours.")
        return
    rs = physics_for(study.analysis.analysis_type).load_results(f, mesh)
    tris = mesh.boundary_tris if mesh.boundary_tris is not None else mesh.boundary_triangles()
    tris = np.asarray(tris)
    if not len(tris):
        notes.append(f"{study.name}: the mesh has no boundary faces to draw.")
        return
    used, inv = np.unique(tris, return_inverse=True)
    T2 = inv.reshape(-1, 3).astype(np.int32)
    P = (np.asarray(mesh.nodes)[used] * scale).astype(np.float32)
    avail = rs.available()
    for key in [k for k in (_CFD_KEYS if study.analysis.analysis_type.startswith("cfd") else _FEA_KEYS) if k in avail]:
        info = rs.info(key)
        vals = np.asarray(rs.quantity(key), dtype=np.float64)[used]
        lo, hi = _range(vals)
        unit = {"pressure_si": "Pa", "velocity": "m/s", "viscosity": "Pa s", "strain": "-"}.get(
            info.unit_kind, us.stress if info.unit_kind == "stress" else us.length if info.unit_kind == "displacement"
            else "")
        item = {"id": f"{study.id}_{key}", "label": f"{info.label} ({study.name})", "discipline": _study_disc(study),
                "unit": unit, "quantity": key, "mesh": "own", "points": _b64(P), "triangles": _b64(T2, "<i4"),
                "values": _b64(vals), "min": lo, "max": hi}
        disp = getattr(rs, "displacement", None)
        if disp is not None and np.any(disp) and not study.analysis.analysis_type.startswith("cfd"):
            d = np.asarray(disp, dtype=np.float64)[used] * scale
            item["displacement"] = _b64(d)
            item["scale_hint"] = _scale_hint(P, d)
        out["fields"].append(item)


def _studio_scene(entry: dict, run: str) -> dict:
    project, dd, rows, row = _studio_resolve(entry, run)
    out = {"units": "mm", "parts": [], "fields": [], "slices": [], "streamlines": None, "notes": []}
    out["parts"], note = _studio_geometry(entry, project)
    if note:
        out["notes"].append(note)
    if row is None:
        out["notes"].append("No results yet: run a study of this project.")
        return out
    st = row["study"]
    if st.analysis.analysis_type == "mbd":
        out["notes"].append("Motion study: there are no contour fields; open the Motion tab to play the result.")
        return out
    try:
        _studio_fields(project, dd, st, out)
    except Exception as exc:  # noqa: BLE001
        out["notes"].append(f"{st.name}: the stored fields could not be read: {exc}")
    return out


def _studio_audit(entry: dict, run: str) -> list:
    project, dd, rows, row = _studio_resolve(entry, run)
    if row is None:
        return []
    st = row["study"]
    rd = dd / st.results.directory
    if st.analysis.analysis_type == "mbd":
        try:
            res = _mbd_load(dd, st)
        except Exception as exc:  # noqa: BLE001
            return [{"analysis": st.id, "title": st.name, "status": "error", "solver": {}, "mesh": {}, "inputs": [],
                     "checks": [], "convergence": {}, "assumptions": [], "problems": [f"Stored results unreadable: {exc}"],
                     "log_tail": "", "files": _file_rows(rd, ["mbd.json", "mbd.npz"])}]
        meta = res.meta
        S = meta.get("settings") or {}
        inputs = [{"name": "gravity", "value": S.get("gravity"), "unit": "m/s^2", "status": "user",
                   "source": "study settings"}]
        for d in meta.get("drivers", []):
            inputs.append({"name": d["name"], "value": f"{d['mode']}: {json.dumps(d.get('profile'))[:160]}",
                           "unit": d.get("unit", ""), "status": "user", "source": d["kind"]})
        viol = _num(meta.get("constraint_violation_max"))
        return [{"analysis": st.id, "title": st.name, "status": "ok" if not meta.get("warnings") else "watch",
                 "solver": {"name": (meta.get("backend") or {}).get("name", "MuJoCo"),
                            "version": (meta.get("backend") or {}).get("version"), "dt_s": S.get("dt_s"),
                            "duration_s": S.get("duration_s"), "output_dt_s": S.get("output_dt_s"),
                            "integrator": S.get("integrator"), "wall_time_s": meta.get("wall_time_s")},
                 "mesh": {}, "inputs": inputs,
                 "checks": [{"name": "Constraint violation (max)", "value": viol, "limit": None,
                             "unit": (meta.get("units") or {}).get("length", ""), "ok": None}],
                 "convergence": {"constraint_violation_max": viol, "n_steps": meta.get("n_steps")},
                 "assumptions": [str(a) for a in meta.get("assumptions", [])],
                 "problems": [str(w) for w in meta.get("warnings", [])], "log_tail": "",
                 "files": _file_rows(rd, ["mbd.json", "mbd.npz"])}]
    # structural / thermal / modal / flow
    mesh_stats, problems = {}, []
    try:
        from opencae.meshing.mesh_data import FEMesh
        if st.mesh.file and (dd / st.mesh.file).is_file():
            mesh_stats = FEMesh.load(dd / st.mesh.file).stats()
    except Exception as exc:  # noqa: BLE001
        problems.append(f"The mesh could not be read: {exc}")
    inputs = []
    mats = {m.id: m for m in project.materials}
    for a in st.assignments:
        m = mats.get(a.material)
        if m is None:
            continue
        for prop, val, unit in (("youngs_modulus", m.youngs_modulus, "Pa"), ("poissons_ratio", m.poissons_ratio, ""),
                                ("density", m.density, "kg/m^3"), ("yield_strength", m.yield_strength, "Pa")):
            if val is None:
                continue
            status = "estimate" if prop in (m.estimated or []) else ("datasheet" if m.source else "user")
            inputs.append({"name": f"{m.name}.{prop}", "value": val, "unit": unit, "status": status,
                           "source": m.source or "project material"})
    for ld in st.loads:
        inputs.append({"name": f"load: {ld.name or ld.kind}", "value": getattr(ld, "magnitude", None), "unit": "",
                       "status": "user", "source": ld.kind})
    log_tail = ""
    for pat in ("*.sta", "*.log", "*.msg"):
        for lf in sorted(rd.glob(pat)):
            try:
                log_tail = lf.read_text(errors="replace")[-3000:]
            except OSError:
                pass
            break
        if log_tail:
            break
    try:
        from opencae.physics import physics_for
        assumptions = list(getattr(physics_for(st.analysis.analysis_type), "assumptions", []) or [])
    except Exception:  # noqa: BLE001
        assumptions = []
    sol = {"name": st.results.solver_version, "wall_time_s": st.results.wall_time_s,
           "analysis_type": st.analysis.analysis_type, "setup_hash": st.results.setup_hash}
    return [{"analysis": st.id, "title": st.name, "status": "ok", "solver": sol, "mesh": mesh_stats, "inputs": inputs,
             "checks": [], "convergence": {"completed": bool(st.results.completed)}, "assumptions": assumptions,
             "problems": problems, "log_tail": log_tail,
             "files": _file_rows(rd, [p.name for p in sorted(rd.glob("*")) if p.is_file()][:40])}]


# ================================================================================================== k09l study
_K09L_CACHE: dict = {}
_K09L_LOCK = threading.Lock()
_K09L_WARMING: set = set()
_ENGINE_STATUS = {"current": "ok", "stale": "watch", "failed": "error", "not run": "not_evaluated"}
_AP_STATUS = {"PASS": "ok", "FAIL": "fail", "NOT EVALUATED": "not_evaluated", "CONDITIONAL / REVIEW REQUIRED":
              "conditional"}
_PLAIN = {"pass": "ok", "ok": "ok", "watch": "watch", "fail": "fail", "nev": "not_evaluated", "cond": "conditional"}


def _k09l_bundle(entry: dict) -> dict:
    """The study's website data (summary, fields, aircraft, flow), computed once per file version."""
    p = Path(entry["path"])
    key = (str(p), p.stat().st_mtime_ns)
    with _K09L_LOCK:
        hit = _K09L_CACHE.get(key)
    if hit:
        return hit
    from opencae.engineering import study as ST
    from opencae.web import build as B
    study = ST.Study.open(p)
    rid = study.current
    fields, fsum = B.field_data(study)
    b = {"rid": rid, "fields": fields, "fsum": fsum, "summary": B.summary_data(study, rid, fsum), "errors": []}
    try:
        b["aircraft"] = B.aircraft_data(study, fsum)
    except Exception as exc:  # noqa: BLE001
        b["aircraft"], b["errors"] = None, b["errors"] + [f"Aircraft model: {exc}"]
    try:
        b["flow"] = B.flow_data(study)
    except Exception as exc:  # noqa: BLE001
        b["flow"], b["errors"] = {}, b["errors"] + [f"CFD flow: {exc}"]
    with _K09L_LOCK:
        _K09L_CACHE.clear()
        _K09L_CACHE[key] = b
    return b


def _k09l_cached(entry: dict) -> Optional[dict]:
    p = Path(entry["path"])
    with _K09L_LOCK:
        return _K09L_CACHE.get((str(p), p.stat().st_mtime_ns))


def _k09l_verdict(summ: dict) -> Optional[str]:
    v = summ.get("verdict") or {}
    if v.get("fail"):
        return "not ready"
    if v.get("cond") or v.get("nev"):
        return "conditional"
    return "pass" if v.get("pass") else None


def _k09l_summary(entry: dict) -> dict:
    p = Path(entry["path"])
    b = _k09l_cached(entry)
    if b is None:  # the first open takes seconds: warm it in the background, answer without the verdict
        key = str(p)
        if key not in _K09L_WARMING:
            _K09L_WARMING.add(key)

            def warm():
                try:
                    _k09l_bundle(entry)
                except Exception:  # noqa: BLE001
                    pass
                finally:
                    _K09L_WARMING.discard(key)
            threading.Thread(target=warm, name="hub-k09l-warm", daemon=True).start()
        return {"updated": _iso(p.stat().st_mtime), "verdict": None, "runs": 1, "disciplines": []}
    discs = []
    for a in b["summary"]["analyses"]:
        if a["status"] != "not run" and a["discipline"] not in discs:
            discs.append(a["discipline"])
    return {"updated": _iso(p.stat().st_mtime), "verdict": _k09l_verdict(b["summary"]), "runs": 1,
            "disciplines": discs or ["system"]}


def _k09l_runs(b: dict, entry: dict) -> list:
    return [{"run": "current", "started": _iso(Path(entry["path"]).stat().st_mtime),
             "verdict": _k09l_verdict(b["summary"]), "only": [], "duration_s": None,
             "variant": b["summary"].get("variant")}]


def _k09l_check_run(run: str) -> None:
    if run not in ("latest", "current", "", None):
        raise HubError(f"No run '{run}' in this project (the study has one current run)", 404)


def _k09l_project(entry: dict, run: str) -> dict:
    _k09l_check_run(run)
    b = _k09l_bundle(entry)
    s = b["summary"]
    cards = []
    kp = [{"label": k["label"], "value": k["value"], "unit": k["unit"], "status": _PLAIN.get(k["status"], "watch"),
           "basis": "", "note": k.get("limit", "")} for k in s["kpis"]]
    sev = ["fail", "watch", "conditional", "not_evaluated", "ok"]
    cards.append({"id": "kpis", "title": "Key numbers", "discipline": "system",
                  "status": min((m["status"] for m in kp), key=sev.index) if kp else "not_evaluated", "basis": "",
                  "headline": next(({"value": m["value"], "unit": m["unit"], "label": m["label"]} for m in kp
                                    if m["value"] is not None), None),
                  "text": f"Study '{s['study']}', variant '{s['variant']}': {s['verdict']['pass']} requirements pass, "
                          f"{s['verdict']['cond']} are conditional on estimates, {s['verdict']['fail']} fail, "
                          f"{s['verdict']['nev']} are not evaluated.", "metrics": kp})
    rq = [{"label": r.get("text") or r.get("id"), "value": _value(r.get("predicted")), "unit": r.get("unit") or "",
           "status": _AP_STATUS.get(str(r.get("status")), "conditional"), "basis": "", "note": str(r.get("why") or ""),
           "limit": r.get("threshold"), "op": r.get("op")} for r in s["requirements"]]
    cards.append({"id": "requirements", "title": "Requirements", "discipline": "requirements",
                  "status": ("fail" if any(m["status"] == "fail" for m in rq) else
                             "conditional" if any(m["status"] in ("conditional", "not_evaluated") for m in rq) else
                             "ok"), "basis": "", "headline": None, "text": f"{len(rq)} requirements of the study.",
                  "metrics": rq})
    groups: dict = {}
    for m in s["metrics"]:
        if m["value"] is None:
            continue
        groups.setdefault(m["group"], []).append({"label": m["label"], "value": m["value"], "unit": m["unit"] or "",
                                                  "status": "ok", "basis": str(m.get("source") or "")})
    for g, ms in groups.items():
        cards.append({"id": "metrics-" + re.sub(r"\W+", "-", g.lower()).strip("-"), "title": g,
                      "discipline": "system", "status": "ok", "basis": _weakest(m["basis"] for m in ms),
                      "headline": {"value": ms[0]["value"], "unit": ms[0]["unit"], "label": ms[0]["label"]},
                      "text": "Computed by the study's level-1 model; the basis is the source of each input.",
                      "metrics": ms})
    analyses = []
    for a in s["analyses"]:
        status = _AP_STATUS.get(str(a.get("verdict")), _ENGINE_STATUS.get(a["status"], "not_evaluated"))
        if a["status"] in ("not run", "failed"):
            status = _ENGINE_STATUS[a["status"]]
        sm = a.get("summary") or {}
        ms = [r for r in (_metric_row(k, {"value": v, "label": k.replace("_", " ")}) for k, v in sm.items()
                          if isinstance(v, (int, float)) and not isinstance(v, bool)) if r and r["value"] is not None]
        text = a.get("result") or a.get("reason") or ""
        cards.append({"id": a["id"], "title": a["label"], "discipline": a["discipline"], "status": status, "basis": "",
                      "headline": None, "text": f"{text} ({a['reason']})" if a.get("result") else text, "metrics": ms})
        analyses.append({"id": a["id"], "title": a["label"], "discipline": a["discipline"], "status": status,
                         "summary": a.get("result") or a["reason"]})
    notice = "; ".join(b["errors"])
    return {"id": entry["id"], "kind": "k09l-study", "name": entry["name"], "run": "current",
            "runs": _k09l_runs(b, entry), "cards": cards, "analyses": analyses, "notice": notice}


def _k09l_scene(entry: dict, run: str) -> dict:
    _k09l_check_run(run)
    b = _k09l_bundle(entry)
    out = {"units": "mm", "parts": [], "fields": [], "slices": [], "streamlines": None, "notes": list(b["errors"])}
    ac = b.get("aircraft")
    if ac:
        grouped: dict = {}
        for body in ac["bodies"]:
            P = np.frombuffer(base64.b64decode(body["pos"]), "<f4").reshape(-1, 3)
            T = np.frombuffer(base64.b64decode(body["idx"]), "<u4" if body.get("i32") else "<u2").reshape(-1, 3)
            g = grouped.setdefault(body["part"], {"P": [], "T": [], "n": 0})
            g["P"].append(P)
            g["T"].append(T.astype(np.int32) + g["n"])
            g["n"] += len(P)
        for pk, g in grouped.items():
            info = ac["parts"].get(pk, {})
            out["parts"].append({"name": info.get("name", pk), "role": info.get("group", "other"),
                                 "points": _b64(np.vstack(g["P"])), "triangles": _b64(np.vstack(g["T"]), "<i4")})
    else:
        out["notes"].append("The aircraft model could not be built.")
    for fid, arr in b["fields"].items():
        sm = b["fsum"].get(fid, {})
        vals = np.frombuffer(base64.b64decode(arr["val"]), "<f4")
        lo, hi = _range(vals)
        item = {"id": fid, "label": sm.get("title") or fid, "discipline": sm.get("discipline") or "system",
                "unit": sm.get("unit") or "", "quantity": sm.get("quantity") or "", "mesh": "own",
                "points": arr["pos"], "triangles": arr["idx"], "values": arr["val"], "min": lo, "max": hi}
        if arr.get("disp"):
            item["displacement"] = arr["disp"]
            item["scale_hint"] = _scale_hint(np.frombuffer(base64.b64decode(arr["pos"]), "<f4").reshape(-1, 3),
                                             np.frombuffer(base64.b64decode(arr["disp"]), "<f4").reshape(-1, 3))
        out["fields"].append(item)
    if not b["fields"]:
        out["notes"].append("The study holds no current contour fields (analyses not run or out of date).")
    fl = b.get("flow") or {}
    for sl in fl.get("slices", []):
        out["slices"].append({"name": sl["name"], "points": sl["pos"], "triangles": sl["idx"], "values": sl["speed"],
                              "label": "speed", "unit": "m/s"})
    if fl.get("streamlines"):
        s = fl["streamlines"]
        out["streamlines"] = {"points": s["pos"], "offsets": _b64(s["offsets"], "<i4"), "values": s["speed"],
                              "unit": "m/s"}
    return out


def _k09l_plots(entry: dict, run: str) -> list:
    _k09l_check_run(run)
    b = _k09l_bundle(entry)
    ex = (b["fsum"].get("cfd_cruise") or {}).get("extra") or {}
    H = np.asarray(ex.get("history") or [], dtype=float)
    if H.ndim == 2 and H.shape[1] >= 3 and len(H):
        return [_plot("cfd_coefficients", "Lift and drag coefficients", "fluid", "Iteration", "", "Coefficient", "-",
                      [{"name": "C_L", "x": H[:, 0].tolist(), "y": H[:, 1].tolist()},
                       {"name": "C_D", "x": H[:, 0].tolist(), "y": H[:, 2].tolist()}], "")]
    return []


def _k09l_audit(entry: dict, run: str) -> list:
    _k09l_check_run(run)
    b = _k09l_bundle(entry)
    out = []
    fsum = b["fsum"]
    for a in b["summary"]["analyses"]:
        f = fsum.get(a["id"]) or {}
        fail = f.get("failure") or {}
        status = _AP_STATUS.get(str(fail.get("status")), _ENGINE_STATUS.get(a["status"], "not_evaluated"))
        problems = []
        if a["status"] in ("failed",):
            problems.append(a["reason"])
        out.append({"analysis": a["id"], "title": a["label"], "status": status, "solver": dict(f.get("solver") or {}),
                    "mesh": dict(f.get("mesh") or {}), "inputs": [],
                    "checks": [],
                    "convergence": {"engine_status": a["status"], "reason": a["reason"]},
                    "assumptions": [str(x) for x in (f.get("assumptions") or [])], "problems": problems,
                    "log_tail": str(fail.get("text") or ""), "files": []})
    est = b["summary"].get("estimates") or []
    out.append({"analysis": "estimates", "title": "Provisional estimates and missing inputs", "status": "watch" if est
                else "ok", "solver": {}, "mesh": {}, "checks": [], "convergence": {}, "assumptions": [],
                "problems": [f"{u['item']}: {u['why']}" for u in (b["summary"].get("unresolved") or [])],
                "log_tail": "", "files": [],
                "inputs": [{"name": e["name"], "value": e["value"], "unit": e["unit"] or "",
                            "status": "estimate" if e["source"] == "provisional_estimate" else str(e["source"]),
                            "source": str(e.get("ref") or e["source"])} for e in est]})
    return out


# ================================================================================================== dispatch
_KINDS = {
    "drone": {"summary": _drone_summary, "project": _drone_project, "scene": _drone_scene, "plots": _drone_plots,
              "audit": _drone_audit},
    "studio": {"summary": _studio_summary, "project": _studio_project, "scene": _studio_scene,
               "plots": lambda e, r: [], "audit": _studio_audit},
    "k09l-study": {"summary": _k09l_summary, "project": _k09l_project, "scene": _k09l_scene,
                   "plots": _k09l_plots, "audit": _k09l_audit},
}


def list_projects() -> list:
    """Every project with its latest verdict, run count and disciplines. A project whose results cannot be read is
    still listed, with an ``error`` text."""
    out = []
    for e in registry.projects():
        item = {"id": e["id"], "kind": e["kind"], "name": e["name"], "path": e["path"], "updated": "",
                "verdict": None, "runs": 0, "disciplines": []}
        try:
            item.update(_KINDS[e["kind"]]["summary"](e))
        except HubError as exc:
            item["error"] = str(exc)
        except Exception as exc:  # noqa: BLE001 - one broken project never hides the others
            item["error"] = f"{type(exc).__name__}: {exc}"
        out.append(item)
    return out


def project_view(pid: str, run: str = "latest") -> dict:
    e = _entry(pid)
    return _KINDS[e["kind"]]["project"](e, run or "latest")


def scene(pid: str, run: str = "latest") -> dict:
    e = _entry(pid)
    return _KINDS[e["kind"]]["scene"](e, run or "latest")


def audit(pid: str, run: str = "latest") -> list:
    e = _entry(pid)
    return _KINDS[e["kind"]]["audit"](e, run or "latest")


def plots(pid: str, run: str = "latest") -> list:
    """The project's plots plus the motion plots of ``opencae.hub.motion.plots`` (when that module imports)."""
    e = _entry(pid)
    out = list(_KINDS[e["kind"]]["plots"](e, run or "latest"))
    mod = _motion_module()
    if mod is not None and hasattr(mod, "plots"):
        try:
            out += list(mod.plots(e, run or "latest") or [])
        except Exception:  # noqa: BLE001 - a motion failure must not hide the other plots
            pass
    return out


def motion(pid: str, run: str = "latest") -> dict:
    e = _entry(pid)
    mod = _motion_module()
    if mod is None or not hasattr(mod, "motion"):
        return {"kind": None, "title": "", "basis": "", "notes": ["motion module not installed"]}
    return mod.motion(e, run or "latest")


def _metric_table(pid: str, run: str) -> dict:
    out = {}
    for c in project_view(pid, run)["cards"]:
        for m in c["metrics"]:
            if isinstance(m.get("value"), (int, float)) and not isinstance(m["value"], bool):
                out.setdefault((c["id"], m["label"]), m)
    return out


def compare(pid: str, a: str, b: str) -> dict:
    """Numeric metrics of two runs side by side (``delta = b - a``; vectors and text are not compared)."""
    ta, tb = _metric_table(pid, a), _metric_table(pid, b)
    rows = []
    for key in list(ta) + [k for k in tb if k not in ta]:
        ma, mb = ta.get(key), tb.get(key)
        va = ma["value"] if ma else None
        vb = mb["value"] if mb else None
        delta = vb - va if va is not None and vb is not None else None
        rows.append({"analysis": key[0], "label": key[1], "unit": (ma or mb)["unit"], "a": va, "b": vb,
                     "delta": delta, "delta_pct": (100.0 * delta / abs(va)) if delta is not None and va else None})
    return {"metrics": rows}
