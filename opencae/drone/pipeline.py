"""Run a drone project: analyses in order, requirement checks, results on disk.

``<project>/results/<variant>/results.json`` holds everything the dashboard, the CLI and the app read. Analyses
whose inputs did not change are skipped (cache key = hash of the drone.json keys the module NEEDS + the CAD file
hash + the module version + the keys of the analyses it depends on). Engine ``"k09l"`` projects are routed to
``opencae.engineering.study.Study``.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
import traceback
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from opencae.drone import geometry as G
from opencae.drone import spec as S
from opencae.drone import templates as TPL
from opencae.drone.analyses import IMPORT_ERRORS, REGISTRY, checks
from opencae.drone.analyses import refresh as _refresh
from opencae.drone.types import (ANALYSES, ERROR, NOT_EVALUATED, RunContext, metric, not_evaluated, result)

RESULTS = "results"
BASELINE = "baseline"
DEFAULT_DEPENDS = {"propulsion": ["mass"], "structure": ["mass", "propulsion"], "flight": ["mass", "propulsion"],
                   "cfd": []}
SWITCHABLE = ("cfd", "structure", "flight")
BETTER = {"mass_kg": "lower", "tw_ratio": "higher", "thrust_max_n": "higher", "thrust_total_max_n": "higher",
          "hover_throttle": "lower", "hover_current_a": "lower", "hover_power_w": "lower",
          "hover_endurance_min": "higher", "full_throttle_current_a": "lower", "motor_current_margin": "higher",
          "esc_current_margin": "higher", "battery_current_margin": "higher", "safety_factor": "higher",
          "max_stress_mpa": "lower", "max_disp_mm": "lower", "cl": None, "cd": "lower", "ld": "higher",
          "lift_n": None, "drag_n": "lower", "max_tilt_deg": "lower", "settle_time_s": "lower",
          "position_error_m": "lower", "throttle_margin": "higher"}


# ------------------------------------------------------------------------------------------------ helpers
def variant_name(name: str) -> str:
    v = re.sub(r"[^A-Za-z0-9_.\- ]+", "_", str(name)).strip(" .")
    if not v or v.startswith("_"):
        raise ValueError(f"'{name}' is not a usable variant name")
    return v


def _dir_size(p: Path) -> int:
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) if p.exists() else 0


def _jsonable(o):
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, (np.floating, np.integer)):
        return o.item()
    if isinstance(o, Path):
        return str(o)
    raise TypeError(f"not JSON serialisable: {type(o).__name__}")


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, default=_jsonable), encoding="utf-8")
    tmp.replace(path)


def _read_results(project_dir: Path, variant: str) -> Optional[dict]:
    p = project_dir / RESULTS / variant / "results.json"
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
    except (OSError, ValueError):
        return None


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())


def _depends(mod, aid: str) -> list:
    return list(getattr(mod, "DEPENDS", DEFAULT_DEPENDS.get(aid, [])))


def _version(mod) -> str:
    return str(getattr(mod, "VERSION", "1"))


def _cache_key(spec: dict, aid: str, mod, geom_hash: str, keys: dict) -> str:
    dep = [keys.get(d, "") for d in _depends(mod, aid)]
    blob = json.dumps([S.input_hash(spec, list(mod.NEEDS) + [f"analyses.{aid}"]), geom_hash, _version(mod), dep])
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def _closure(only, registry) -> list:
    """The analyses to run: ``only`` plus what they depend on, in run order."""
    if not only:
        return list(ANALYSES)
    bad = [a for a in only if a not in ANALYSES]
    if bad:
        raise ValueError(f"unknown analysis {', '.join(bad)} (use {', '.join(ANALYSES)})")
    want = set(only)
    for a in only:
        mod = registry.get(a)
        want |= set(_depends(mod, a) if mod else DEFAULT_DEPENDS.get(a, []))
        for d in list(want):
            want |= set(DEFAULT_DEPENDS.get(d, []))
    return [a for a in ANALYSES if a in want]


def _reusable(res: Optional[dict], key: str, out_dir: Path, source_dir: Path) -> bool:
    if not res or res.get("status") == ERROR or (res.get("extra") or {}).get("cache_key") != key:
        return False
    return all((source_dir / f["file"]).exists() for f in res.get("fields", []) if f.get("file"))


# ------------------------------------------------------------------------------------------------ run
def run_project(project_dir, only=None, overrides: Optional[dict] = None, variant: str = BASELINE,
                progress: Optional[Callable] = None, cancelled: Optional[Callable] = None,
                keep_cases: bool = False, force: bool = False) -> dict:
    project_dir = Path(project_dir)
    spec0 = S.load(project_dir)
    if spec0.get("engine", "generic") == "k09l":
        return _run_k09l(project_dir, spec0, only, overrides, variant, progress, cancelled, keep_cases, force)
    progress = progress or (lambda msg, frac: None)
    cancelled = cancelled or (lambda: False)
    variant = variant_name(variant)
    spec = S.apply_overrides(spec0, overrides) if overrides else spec0
    problems = S.validate(spec)
    if problems:
        raise ValueError("drone.json has problems: " + "; ".join(problems))
    _refresh()
    out_dir = project_dir / RESULTS / variant
    out_dir.mkdir(parents=True, exist_ok=True)
    progress("reading the CAD file", 0.02)
    aircraft = G.load(project_dir, spec)
    geom_hash = G.file_hash(project_dir / spec["geometry"]["file"])
    G.save_npz(aircraft, out_dir / "geometry.npz")
    prev = _read_results(project_dir, variant)
    base = _read_results(project_dir, BASELINE) if variant != BASELINE else None
    analyses = dict((prev or {}).get("analyses", {}))
    todo = _closure(only, REGISTRY)
    keys: dict = {}
    was_cancelled = False
    for i, aid in enumerate(ANALYSES):
        mod = REGISTRY.get(aid)
        if aid not in todo:
            if aid in analyses:  # keep the key of what is on disk for dependants
                keys[aid] = (analyses[aid].get("extra") or {}).get("cache_key", "")
            continue
        if cancelled():
            was_cancelled = True
            break
        frac = 0.05 + 0.9 * i / len(ANALYSES)
        title = getattr(mod, "TITLE", aid.capitalize())
        if mod is None:
            why = "analysis module not available"
            if aid in IMPORT_ERRORS:
                why += f" ({IMPORT_ERRORS[aid]})"
            analyses[aid] = not_evaluated(aid, title, [], why)
            continue
        key = _cache_key(spec, aid, mod, geom_hash, keys)
        keys[aid] = key
        progress(f"{title}", frac)
        if aid in SWITCHABLE and (spec.get("analyses") or {}).get(aid, {}).get("enabled") is False:
            analyses[aid] = not_evaluated(aid, title, [], f"Switched off in drone.json (analyses.{aid}.enabled).")
            analyses[aid]["extra"]["cache_key"] = key
            continue
        hit = None
        if not force:
            for cand, src in ((analyses.get(aid), out_dir),
                              ((base or {}).get("analyses", {}).get(aid), project_dir / RESULTS / BASELINE)):
                if _reusable(cand, key, out_dir, src):
                    if src != out_dir:
                        for f in cand.get("fields", []):
                            if f.get("file"):
                                shutil.copyfile(src / f["file"], out_dir / f["file"])
                    hit = json.loads(json.dumps(cand))
                    hit["extra"]["cached"] = True
                    break
        if hit is not None:
            analyses[aid] = hit
            continue
        work = out_dir / "_scratch" / aid
        ctx = RunContext(project_dir, spec, aircraft, work, out_dir,
                         {k: v for k, v in analyses.items() if k in ANALYSES[:i]}, progress, cancelled, keep_cases)
        t0 = time.time()
        try:
            res = mod.run(ctx)
        except Exception as exc:  # never hidden: recorded as an error result with the traceback tail
            res = result(aid, title, ERROR, f"The {title.lower()} analysis failed: {type(exc).__name__}: {exc}",
                         log=traceback.format_exc())
        res.setdefault("extra", {})
        res["extra"].update(cached=False, cache_key=key, runtime_s=round(time.time() - t0, 3))
        analyses[aid] = res
        if res["status"] != ERROR and not keep_cases:
            shutil.rmtree(work, ignore_errors=True)
    scratch = out_dir / "_scratch"
    if scratch.exists() and not any(scratch.iterdir()):
        scratch.rmdir()
    rows, verdict = checks.evaluate(spec, analyses, aircraft)
    out = {"project": spec.get("name", project_dir.name), "variant": variant, "spec_used": spec,
           "analyses": analyses, "checks": rows, "verdict": verdict, "generated": _now(), "storage_bytes": 0}
    if was_cancelled:
        out["cancelled"] = True
    _write_json(out_dir / "results.json", out)
    out["storage_bytes"] = _dir_size(out_dir)
    _write_json(out_dir / "results.json", out)
    progress("done", 1.0)
    return out


# ------------------------------------------------------------------------------------------------ status / clean
def variants(project_dir) -> list[str]:
    project_dir = Path(project_dir)
    root = project_dir / RESULTS
    names = sorted(p.name for p in root.iterdir() if p.is_dir() and (p / "results.json").exists()) \
        if root.exists() else []
    return ([BASELINE] if BASELINE in names else []) + [n for n in names if n != BASELINE]


def status(project_dir) -> dict:
    """``{"project", "type", "engine", "variants": {name: {"verdict", "generated", "analyses": {id: {"status",
    "stale", ...}}}}}``. stale = the inputs changed since the stored result was computed."""
    project_dir = Path(project_dir)
    spec = S.load(project_dir)
    engine = spec.get("engine", "generic")
    head = {"project": spec.get("name", project_dir.name), "type": spec.get("type"), "engine": engine}
    if engine == "k09l":
        return {**head, "variants": _status_k09l(project_dir, spec)}
    _refresh()
    out: dict = {}
    try:
        geom_hash = G.file_hash(project_dir / spec["geometry"]["file"])
    except (OSError, KeyError):
        geom_hash = ""
    for name in variants(project_dir) or [BASELINE]:
        res = _read_results(project_dir, name)
        spec_used = (res or {}).get("spec_used") or spec
        cur = spec if name == BASELINE else spec_used
        keys, an = {}, {}
        for aid in ANALYSES:
            mod = REGISTRY.get(aid)
            r = ((res or {}).get("analyses") or {}).get(aid)
            if mod is not None:
                keys[aid] = _cache_key(cur, aid, mod, geom_hash, keys)
            if r is None:
                an[aid] = {"status": "not run", "stale": False}
            else:
                st = (r.get("extra") or {}).get("cache_key")
                an[aid] = {"status": r["status"], "stale": bool(mod is not None and st != keys.get(aid)),
                           "summary": r.get("summary", "")}
        out[name] = {"verdict": (res or {}).get("verdict"), "generated": (res or {}).get("generated"),
                     "analyses": an}
    return {**head, "variants": out}


def clean(project_dir, results: bool = False) -> int:
    """Delete solver scratch (and with ``results=True`` every result). Returns bytes freed."""
    root = Path(project_dir) / RESULTS
    freed = 0
    if not root.exists():
        return 0
    if results:
        freed = _dir_size(root)
        shutil.rmtree(root, ignore_errors=True)
        return freed
    for sc in list(root.glob("_scratch")) + list(root.glob("*/_scratch")):
        freed += _dir_size(sc)
        shutil.rmtree(sc, ignore_errors=True)
    return freed


# ------------------------------------------------------------------------------------------------ new / iterate
def new_project(directory, cad_file, name: str, type: str) -> Path:
    """Create a project folder: copy the CAD file, write a drone.json with guessed roles and empty (null)
    property placeholders. Returns the path of drone.json."""
    directory, cad_file = Path(directory), Path(cad_file)
    if type not in ("multirotor", "fixed_wing", "tailsitter", "vtol"):
        raise ValueError(f"unknown aircraft type '{type}'")
    if not cad_file.is_file():
        raise FileNotFoundError(f"CAD file not found: {cad_file}")
    if (directory / S.FILE_NAME).exists():
        raise FileExistsError(f"{directory / S.FILE_NAME} already exists")
    directory.mkdir(parents=True, exist_ok=True)
    dest = directory / cad_file.name
    if cad_file.resolve() != dest.resolve():
        shutil.copyfile(cad_file, dest)
    raw = G.read_raw(dest)
    names = [n for n, _, _ in raw]
    roles = G.guess_roles(names)
    allpts = np.vstack([p for _, p, _ in raw])
    diag = float(np.linalg.norm(allpts.max(0) - allpts.min(0)))
    units = "m" if dest.suffix.lower() == ".stl" or dest.suffix.lower() == ".obj" else "mm"
    units = units if (units == "m" and diag < 5.0) else "mm"
    present = [r for r in roles]
    spec = TPL.template(type, name, dest.name, units, roles, present)
    return S.save(spec, directory)


def _flat(results: dict) -> dict:
    return {f"{aid}.{k}": m for aid, r in results.get("analyses", {}).items()
            for k, m in r.get("metrics", {}).items() if isinstance(m.get("value"), (int, float))}


def iterate(project_dir, overrides: dict, name: str, only=None) -> dict:
    """Run a variant with ``overrides`` applied to a copy of drone.json (the file itself is never changed) and
    compare it with the baseline (which is run first when it has no results yet)."""
    project_dir = Path(project_dir)
    name = variant_name(name)
    if name == BASELINE:
        raise ValueError("'baseline' is reserved for the unmodified drone.json; choose another name")
    S.apply_overrides(S.load(project_dir), overrides)  # reject bad keys before anything runs
    base = _read_results(project_dir, BASELINE)
    if base is None:
        base = run_project(project_dir, only=only, variant=BASELINE)
    res = run_project(project_dir, only=only, overrides=overrides, variant=name)
    b, v = _flat(base), _flat(res)
    rows = []
    for key in b:
        if key in v:
            aid, mk = key.split(".", 1)
            mb, mv = b[key], v[key]
            rows.append({"metric": key, "label": mv.get("label") or mk, "baseline": mb["value"],
                         "variant": mv["value"], "unit": mv.get("unit", ""), "better": BETTER.get(mk)})
    return {"variant": name, "results": res, "compare": rows}


# ------------------------------------------------------------------------------------------------ K09-L engine
_K09L_STATUS = {"current": "ok", "stale": NOT_EVALUATED, "not run": NOT_EVALUATED, "failed": ERROR}
_K09L_CHECK = {"PASS": "pass", "FAIL": "fail", "NOT EVALUATED": "not_evaluated"}


def _k09l_open(project_dir: Path, spec: dict):
    from opencae.engineering import study as ST
    work = project_dir / RESULTS / "k09l_study.json"
    if work.exists():
        return ST.Study.open(work), work
    src = (project_dir / spec["study"]).resolve()
    return ST.Study.open(src if src.exists() else ST.SEED), work


def _k09l_rid(st, spec: dict) -> str:
    want = spec.get("variant")
    if not want:
        return st.current
    for v in st.variants:
        if v.get("name") == want:
            return v["id"]
    raise ValueError(f"variant '{want}' not found in the K09-L study (has: {', '.join(v.get('name', '') for v in st.variants)})")


def _status_k09l(project_dir: Path, spec: dict) -> dict:
    st, _ = _k09l_open(project_dir, spec)
    rid = _k09l_rid(st, spec)
    snap = st.snapshot(rid)
    an = {nid: {"status": _K09L_STATUS.get(h["status"], NOT_EVALUATED), "stale": h["status"] == "stale",
                "engine_status": h["status"], "summary": h.get("reason") or h["label"]}
          for nid, h in snap["heavy"].items()}
    return {spec.get("variant") or st.variant(rid).get("name", rid): {"verdict": None, "generated": snap["time"],
                                                                      "analyses": an}}


def _k09l_results(project_dir: Path, spec: dict, st, rid: str, run_info: dict) -> dict:
    from opencae.engineering import study as ST
    snap = st.snapshot(rid)
    name = spec.get("variant") or st.variant(rid).get("name", rid)
    analyses = {}
    for nid, h in snap["heavy"].items():
        s = h["summary"] or {}
        ms = {k: metric(v, "", "ok", "simulation", k) for k, v in s.items()
              if isinstance(v, (int, float)) and not isinstance(v, bool)}
        analyses[nid] = result(nid, h["label"], _K09L_STATUS.get(h["status"], NOT_EVALUATED),
                               s.get("text") or h.get("reason") or f"{h['label']}: {h['status']}", metrics=ms,
                               extra={"engine_status": h["status"], "stale": h["status"] == "stale"})
    km = {}
    for key, label, _g, scale, unit, _nd, _b in ST.METRICS:
        q = snap["metrics"].get(key)
        if q and isinstance(q.get("value"), (int, float)):
            km[key] = metric(q["value"] * scale, unit, "ok", str(q.get("source", "")), label)
    analyses["key_metrics"] = result("key_metrics", "Key metrics", "ok", "Key numbers of the K09-L model.", metrics=km)
    rows = []
    for r in snap["requirements"]:
        rows.append({"id": r.get("id"), "label": r.get("label") or r.get("name") or r.get("id"),
                     "metric": r.get("metric", ""), "op": r.get("op", ""), "target": r.get("target"),
                     "value": r.get("value"), "status": _K09L_CHECK.get(r.get("status"), "conditional"),
                     "why": str(r.get("why") or r.get("basis") or "")})
    c = snap["counts"]
    verdict = ("not ready" if c.get("FAIL") else "conditional")
    return {"project": spec.get("name", project_dir.name), "variant": name, "engine": "k09l", "spec_used": spec,
            "analyses": analyses, "checks": rows, "verdict": verdict, "generated": snap["time"],
            "storage_bytes": 0, "run": run_info}


def _run_k09l(project_dir, spec, only, overrides, variant, progress, cancelled, keep_cases, force) -> dict:
    if overrides:
        raise ValueError("overrides are not supported for the k09l engine; edit the variant in the K09-L "
                         "Design Monitor")
    progress = progress or (lambda msg, frac: None)
    cancelled = cancelled or (lambda: False)
    st, work = _k09l_open(project_dir, spec)
    rid = _k09l_rid(st, spec)
    progress("K09-L heavy analyses", 0.02)
    info = st.run_analyses(rid, workdir=project_dir / RESULTS / "_scratch", only_missing=not force,
                           progress=lambda nid, i, n: progress(nid, i / max(n, 1)), cancelled=cancelled,
                           keep_cases=keep_cases)
    st.save(work)
    out = _k09l_results(project_dir, spec, st, rid, info if isinstance(info, dict) else {})
    d = project_dir / RESULTS / variant_name(out["variant"])
    _write_json(d / "results.json", out)
    out["storage_bytes"] = _dir_size(d)
    _write_json(d / "results.json", out)
    progress("done", 1.0)
    return out
