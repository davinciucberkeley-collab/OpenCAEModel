"""Run a drone project: analyses in order, requirement checks, results on disk.

``<project>/results/<variant>/results.json`` holds everything the dashboard, the CLI and the app read. Analyses
whose inputs did not change are skipped (cache key = hash of the drone.json keys the module NEEDS + geometry,
roles and role_materials (they shape every analysis) + the CAD file hash + the module version + the keys of the analyses it depends on). Engine ``"k09l"`` projects are routed to
``opencae.engineering.study.Study``.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import statistics
import time
import traceback
import uuid
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
SHARED_INPUTS = ("geometry", "roles", "role_materials")  # in every cache key: units, axes and roles change all
BETTER = {"mass_kg": "lower", "tw_ratio": "higher", "thrust_max_n": "higher", "thrust_total_max_n": "higher",
          "hover_throttle": "lower", "hover_current_a": "lower", "hover_power_w": "lower",
          "hover_endurance_min": "higher", "full_throttle_current_a": "lower", "motor_current_margin": "higher",
          "esc_current_margin": "higher", "battery_current_margin": "higher", "safety_factor": "higher",
          "max_stress_mpa": "lower", "max_disp_mm": "lower", "cl": None, "cd": "lower", "ld": "higher",
          "lift_n": None, "drag_n": "lower", "max_tilt_deg": "lower", "settle_time_s": "lower",
          "position_error_m": "lower", "throttle_margin": "higher"}


# ------------------------------------------------------------------------------------------------ helpers
VARIANT_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")


def variant_name(name: str) -> str:
    """A variant is a folder name: 1 to 40 letters, digits, '_' or '-'. Anything else is rejected."""
    if not isinstance(name, str) or not VARIANT_RE.match(name):
        raise ValueError(f"'{name}' is not a usable variant name (use 1 to 40 letters, digits, _ or -)")
    return name


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
    keys_in = list(mod.NEEDS) + [f"analyses.{aid}"] + list(SHARED_INPUTS)
    blob = json.dumps([S.input_hash(spec, keys_in), geom_hash, _version(mod), dep])
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


_RANK = {"ok": 0, "watch": 1, "fail": 2}


def _conditional_on_estimates(res: dict) -> None:
    """A computed result that rests on an estimate is reported ``conditional`` (never a plain ``ok``/``watch``);
    ``fail`` stays ``fail``. The numeric status stays in ``extra.numeric_status`` for colouring."""
    ex = res.setdefault("extra", {})
    st = res.get("status")
    if st in _RANK:
        ex["numeric_status"] = st
    elif st == "conditional" and "numeric_status" not in ex:
        sts = [m.get("status") for m in (res.get("metrics") or {}).values() if m.get("status") in _RANK]
        if sts:
            ex["numeric_status"] = max(sts, key=_RANK.get)
    if st in ("ok", "watch") and any("estimate" in str(m.get("basis") or "").replace(",", " ").split()
                                    for m in (res.get("metrics") or {}).values()):
        res["status"] = "conditional"


def _reusable(res: Optional[dict], key: str, out_dir: Path, source_dir: Path) -> bool:
    if not res or res.get("status") == ERROR or (res.get("extra") or {}).get("cache_key") != key:
        return False
    return all((source_dir / f["file"]).exists() for f in res.get("fields", []) if f.get("file"))


# ------------------------------------------------------------------------------------------------ run events / history
RUNS_FILE = "runs.jsonl"
# relative weights that shape the overall bar when there is no history yet (a CFD run takes far longer than mass)
_DEFAULT_WEIGHT = {"mass": 1.0, "propulsion": 1.0, "structure": 8.0, "cfd": 30.0, "flight": 3.0}


def run_history(project_dir) -> list:
    """The runs of this project, oldest first (newest last), read from ``results/runs.jsonl``. Unreadable lines
    are skipped."""
    p = Path(project_dir) / RESULTS / RUNS_FILE
    out: list = []
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                out.append(row)
    except OSError:
        pass
    return out


def typical_durations(project_dir=None, history: Optional[list] = None) -> dict:
    """``{analysis id: median seconds}`` of the earlier runs that really computed the analysis (cached,
    failed and zero-length entries are ignored)."""
    rows = history if history is not None else run_history(project_dir)
    seen: dict = {}
    for r in rows:
        for aid, a in ((r or {}).get("analyses") or {}).items():
            try:
                d = float(a.get("duration_s") or 0)
            except (TypeError, ValueError, AttributeError):
                continue
            if d > 0 and not a.get("cached") and a.get("status") not in ("error", "cancelled", "skipped"):
                seen.setdefault(aid, []).append(d)
    return {aid: float(statistics.median(v)) for aid, v in seen.items()}


def _append_run(project_dir: Path, row: dict) -> None:
    p = project_dir / RESULTS / RUNS_FILE
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, default=_jsonable) + "\n")
    except OSError:
        pass  # the history is a convenience; never fail a run because of it


class _RunTracker:
    """Emits the run events (see ``run_project``) and writes the run's line in ``runs.jsonl``."""

    def __init__(self, project_dir: Path, variant: str, only, events: Optional[Callable], plan: list):
        self.project_dir, self.variant, self.only, self.events = project_dir, variant, only, events
        self.plan = plan  # [(id, title)]
        self.run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]
        self.t0 = time.time()
        self.started = _now()
        self.records: dict = {}
        self.cur: Optional[str] = None
        self.frac = 0.0
        typ = typical_durations(project_dir)
        w = [max(typ.get(a) or _DEFAULT_WEIGHT.get(a, 1.0), 0.2) for a, _ in plan]
        tot = sum(w) or 1.0
        self.span: dict = {}
        acc = 0.05
        for (a, _), wi in zip(plan, w):
            self.span[a] = (acc, acc + 0.9 * wi / tot)
            acc += 0.9 * wi / tot
        self.finished = False

    def emit(self, kind: str, id: Optional[str] = None, title: str = "", message: str = "", status=None,
             frac: Optional[float] = None, **extra) -> None:
        if frac is not None:
            self.frac = max(self.frac, min(1.0, float(frac)))
        if self.events is None:
            return
        ev = {"t": time.time(), "kind": kind, "run_id": self.run_id, "id": id, "title": title, "status": status,
              "frac": self.frac, "message": message, "elapsed_s": round(time.time() - self.t0, 3)}
        ev.update(extra)
        try:
            self.events(ev)
        except Exception:  # a broken listener must not break the run
            pass

    def start(self, name: str) -> None:
        self.emit("run_start", title=name, message="Starting the run", frac=0.0,
                  plan=[{"id": a, "title": t} for a, t in self.plan], variant=self.variant)

    def analysis_start(self, aid: str, title: str) -> None:
        self.cur = aid
        self.emit("analysis_start", aid, title, f"{title}: starting", frac=self.span.get(aid, (0, 0))[0])

    def step(self, msg: str, local: Optional[float] = None) -> None:
        aid = self.cur
        lo, hi = self.span.get(aid, (self.frac, self.frac))
        try:
            lf = min(max(float(local), 0.0), 0.99)
        except (TypeError, ValueError):
            lf = 0.0
        title = dict(self.plan).get(aid, "") if aid else ""
        self.emit("step", aid, title, str(msg), frac=lo + (hi - lo) * lf)

    def analysis_done(self, aid: str, title: str, status: str, duration: float, cached: bool = False,
                      skipped: bool = False, message: str = "") -> None:
        self.records[aid] = {"status": status, "duration_s": round(duration, 3), "cached": bool(cached)}
        self.cur = None
        self.emit("analysis_done", aid, title, message or title, status=status, frac=self.span.get(aid, (0, 0))[1],
                  duration_s=round(duration, 3), cached=bool(cached), skipped=bool(skipped))

    def log(self, msg: str, aid: Optional[str] = None) -> None:
        self.emit("log", aid, "", msg)

    def finish(self, status: str, verdict=None, error: str = "") -> None:
        """status: done | cancelled | error. Appends the runs.jsonl line and emits ``run_done``."""
        if self.finished:
            return
        self.finished = True
        dur = round(time.time() - self.t0, 3)
        _append_run(self.project_dir, {"run_id": self.run_id, "variant": self.variant, "started": self.started,
                                       "finished": _now(), "duration_s": dur, "only": list(self.only or []),
                                       "verdict": verdict, "status": status, "analyses": self.records,
                                       "error": error or None})
        try:  # keep this run's numbers readable after a later run overwrites results/<variant>/
            if status != "error":  # a crashed run wrote nothing: do not file the previous run's numbers under it
                from opencae.hub.snapshots import snapshot_run
                snapshot_run(self.project_dir, self.run_id, self.variant)
        except Exception:  # noqa: BLE001 - the history is a convenience; never fail a run because of it
            pass
        self.emit("run_done", None, "", error or ("Stopped" if status == "cancelled" else "Run finished"),
                  status=status, frac=1.0 if status == "done" else None, verdict=verdict, error=error or None,
                  duration_s=dur)


# ------------------------------------------------------------------------------------------------ run
def run_project(project_dir, only=None, overrides: Optional[dict] = None, variant: str = BASELINE,
                progress: Optional[Callable] = None, cancelled: Optional[Callable] = None,
                keep_cases: bool = False, force: bool = False, events: Optional[Callable] = None) -> dict:
    """Run the analyses. ``progress(msg, frac)`` and ``cancelled()`` keep working as before. ``events(ev)`` gets
    ``{"t", "kind": run_start|analysis_start|step|analysis_done|log|run_done, "run_id", "id", "title", "status",
    "frac" (overall 0..1, never going back), "message", "elapsed_s"}`` plus kind specific keys (``plan``,
    ``duration_s``, ``cached``, ``skipped``, ``verdict``, ``error``). Every run appends a line to
    ``results/runs.jsonl``, also when an analysis fails or the run is stopped."""
    project_dir = Path(project_dir)
    spec0 = S.load(project_dir)
    if spec0.get("engine", "generic") == "k09l":
        return _run_k09l(project_dir, spec0, only, overrides, variant, progress, cancelled, keep_cases, force, events)
    progress = progress or (lambda msg, frac: None)
    cancelled = cancelled or (lambda: False)
    variant = variant_name(variant)
    spec = S.apply_overrides(spec0, overrides) if overrides else spec0
    problems = S.validate(spec)
    if problems:
        raise ValueError("drone.json has problems: " + "; ".join(problems))
    _refresh()
    todo = _closure(only, REGISTRY)
    plan = [(a, getattr(REGISTRY.get(a), "TITLE", a.capitalize())) for a in todo]
    T = _RunTracker(project_dir, variant, only, events, plan)
    T.start(spec.get("name", project_dir.name))
    try:
        out = _run_generic(project_dir, spec, only, variant, progress, cancelled, keep_cases, force, T, todo)
    except BaseException as exc:  # recorded in the history, then raised as before
        T.finish("error", error=f"{type(exc).__name__}: {exc}")
        raise
    T.finish("cancelled" if out.get("cancelled") else "done", verdict=out.get("verdict"))
    return out


def _run_generic(project_dir: Path, spec: dict, only, variant: str, progress, cancelled, keep_cases: bool,
                 force: bool, T: _RunTracker, todo: list) -> dict:
    def prog(msg, frac=None):  # the caller's progress callback is unchanged; the event carries the overall bar
        progress(msg, frac)
        T.step(msg, frac if T.cur else None)

    out_dir = project_dir / RESULTS / variant
    out_dir.mkdir(parents=True, exist_ok=True)
    progress("reading the CAD file", 0.02)
    T.emit("step", None, "", "Reading the CAD file", frac=0.02)
    aircraft = G.load(project_dir, spec)
    geom_hash = G.file_hash(project_dir / spec["geometry"]["file"])
    G.save_npz(aircraft, out_dir / "geometry.npz")
    prev = _read_results(project_dir, variant)
    base = _read_results(project_dir, BASELINE) if variant != BASELINE else None
    analyses = dict((prev or {}).get("analyses", {}))
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
            T.log("Stopped by the user before " + dict(T.plan).get(aid, aid))
            break
        frac = 0.05 + 0.9 * i / len(ANALYSES)
        title = getattr(mod, "TITLE", aid.capitalize())
        t_start = time.time()
        T.analysis_start(aid, title)
        if mod is None:
            why = "analysis module not available"
            if aid in IMPORT_ERRORS:
                why += f" ({IMPORT_ERRORS[aid]})"
            analyses[aid] = not_evaluated(aid, title, [], why)
            T.analysis_done(aid, title, NOT_EVALUATED, 0.0, skipped=True, message=f"{title}: {why}")
            continue
        key = _cache_key(spec, aid, mod, geom_hash, keys)
        keys[aid] = key
        progress(f"{title}", frac)
        if aid in SWITCHABLE and (spec.get("analyses") or {}).get(aid, {}).get("enabled") is False:
            analyses[aid] = not_evaluated(aid, title, [], f"Switched off in drone.json (analyses.{aid}.enabled).")
            analyses[aid]["extra"]["cache_key"] = key
            T.analysis_done(aid, title, NOT_EVALUATED, 0.0, skipped=True, message=f"{title}: switched off")
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
            T.analysis_done(aid, title, hit.get("status", ""), time.time() - t_start, cached=True,
                            message=f"{title}: reused (inputs unchanged)")
            continue
        work = out_dir / "_scratch" / aid
        ctx = RunContext(project_dir, spec, aircraft, work, out_dir,
                         {k: v for k, v in analyses.items() if k in ANALYSES[:i]}, prog, cancelled, keep_cases)
        t0 = time.time()
        try:
            res = mod.run(ctx)
        except Exception as exc:  # never hidden: recorded as an error result with the traceback tail
            res = result(aid, title, ERROR, f"The {title.lower()} analysis failed: {type(exc).__name__}: {exc}",
                         log=traceback.format_exc())
        res.setdefault("extra", {})
        _conditional_on_estimates(res)
        res["extra"].update(cached=False, cache_key=key, runtime_s=round(time.time() - t0, 3))
        analyses[aid] = res
        T.analysis_done(aid, title, res["status"], time.time() - t0, message=str(res.get("summary") or title))
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
    property placeholders. Returns the project folder (drone.json is inside it)."""
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
    S.save(spec, directory)
    return directory


def _flat(results: dict) -> dict:
    return {f"{aid}.{k}": m for aid, r in results.get("analyses", {}).items()
            for k, m in r.get("metrics", {}).items() if isinstance(m.get("value"), (int, float))}


def iterate(project_dir, overrides: dict, name: str, only=None, progress: Optional[Callable] = None,
            cancelled: Optional[Callable] = None, keep_cases: bool = False,
            events: Optional[Callable] = None) -> dict:
    """Run a variant with ``overrides`` applied to a copy of drone.json (the file itself is never changed) and
    compare it with the baseline (which is run first when it has no results yet). The events of both runs
    are one run for the listener: one ``run_start``, the bar goes 0..1 over both, one ``run_done``."""
    project_dir = Path(project_dir)
    name = variant_name(name)
    if name == BASELINE:
        raise ValueError("'baseline' is reserved for the unmodified drone.json; choose another name")
    S.apply_overrides(S.load(project_dir), overrides)  # reject bad keys before anything runs
    base = _read_results(project_dir, BASELINE)
    shared = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:4]

    def part(lo: float, hi: float, first: bool, last: bool):
        if events is None:
            return None

        def relay(ev: dict) -> None:
            if ev["kind"] == "run_start" and not first:
                return
            if ev["kind"] == "run_done" and not last:
                return
            ev = dict(ev, run_id=shared, frac=lo + (hi - lo) * float(ev.get("frac") or 0))
            if ev["kind"] == "run_start":
                ev["message"] = f"Starting variant '{name}'"
            events(ev)
        return relay

    if base is None:
        base = run_project(project_dir, only=only, variant=BASELINE, progress=progress, cancelled=cancelled,
                           keep_cases=keep_cases, events=part(0.0, 0.5, True, False))
        res = run_project(project_dir, only=only, overrides=overrides, variant=name, progress=progress,
                          cancelled=cancelled, keep_cases=keep_cases, events=part(0.5, 1.0, False, True))
    else:
        res = run_project(project_dir, only=only, overrides=overrides, variant=name, progress=progress,
                          cancelled=cancelled, keep_cases=keep_cases, events=part(0.0, 1.0, True, True))
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
_K09L_CHECK = {"PASS": "pass", "FAIL": "fail", "NOT EVALUATED": "not_evaluated",
               "CONDITIONAL / REVIEW REQUIRED": "conditional"}


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
        rows.append({"id": r.get("id"), "label": r.get("text") or r.get("id"), "metric": r.get("metric", ""),
                     "op": r.get("op", ""), "target": r.get("threshold"), "value": r.get("predicted"),
                     "unit": r.get("unit", ""), "status": _K09L_CHECK.get(r.get("status"), "conditional"),
                     "why": str(r.get("why") or "")})
    verdict = "not ready" if any(r["status"] == "fail" for r in rows) else "conditional"
    return {"project": spec.get("name", project_dir.name), "variant": name, "engine": "k09l", "spec_used": spec,
            "analyses": analyses, "checks": rows, "verdict": verdict, "generated": snap["time"],
            "storage_bytes": 0, "run": run_info}


def _run_k09l(project_dir, spec, only, overrides, variant, progress, cancelled, keep_cases, force,
              events=None) -> dict:
    if overrides:
        raise ValueError("overrides are not supported for the k09l engine; edit the variant in the K09-L "
                         "Design Monitor")
    progress = progress or (lambda msg, frac: None)
    cancelled = cancelled or (lambda: False)
    T = _RunTracker(project_dir, str(spec.get("variant") or "k09l"), only, events, [])
    T.start(spec.get("name", project_dir.name))
    try:
        st, work = _k09l_open(project_dir, spec)
        rid = _k09l_rid(st, spec)
        progress("K09-L heavy analyses", 0.02)
        T.emit("step", None, "", "K09-L heavy analyses", frac=0.02)

        def prog(nid, i, n):
            progress(nid, i / max(n, 1))
            T.emit("step", None, "", str(nid), frac=0.05 + 0.9 * i / max(n, 1))

        info = st.run_analyses(rid, workdir=project_dir / RESULTS / "_scratch", only_missing=not force,
                               progress=prog, cancelled=cancelled, keep_cases=keep_cases)
        st.save(work)
        out = _k09l_results(project_dir, spec, st, rid, info if isinstance(info, dict) else {})
        d = project_dir / RESULTS / (re.sub(r"[^A-Za-z0-9_.\- ]+", "_", str(out["variant"])).strip(" .") or "variant")
        _write_json(d / "results.json", out)
        out["storage_bytes"] = _dir_size(d)
        _write_json(d / "results.json", out)
    except BaseException as exc:
        T.finish("error", error=f"{type(exc).__name__}: {exc}")
        raise
    for nid, r in out["analyses"].items():
        T.records[nid] = {"status": r.get("status"), "duration_s": 0.0, "cached": False}
    progress("done", 1.0)
    T.finish("cancelled" if cancelled() else "done", verdict=out.get("verdict"))
    return out
