"""The shared job registry: one place that knows every long-running operation (Qt-free, thread-safe).

Everything that takes time (meshing, a solver run, a Drone analysis run, a multibody simulation, a parameter
study, a solver install) reports here: ``create_job`` when it starts, ``stage`` / ``set_progress`` / ``log`` while
it runs, ``finish`` when it ends. The Progress Monitor dock, the status-bar counter and (later) the command
console only read from here, so a run started from a button, the console or the CLI looks the same.

Rules the callers rely on:

* ``Job.progress`` stays ``None`` unless a caller sets it from something real (the backend reported it, or
  ``done / total`` of countable cases). Nothing here ever guesses a percentage from the clock.
* Events can come from any thread. Subscribers are called in the thread that made the change, after the lock is
  released; the Qt monitor re-queues them into the GUI thread.
* A job is persisted when it ends, to ``<project data folder>/jobs.jsonl`` (one JSON object per line), and
  loaded again when the project is registered. Callbacks (cancel, retry, ...) are never persisted.
"""
from __future__ import annotations

import itertools
import json
import logging
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

log = logging.getLogger("opencae.jobs")

KINDS = ("mesh", "structural", "thermal", "modal", "fluid", "motion", "drone", "parameter_study", "setup", "other")
SOURCES = ("gui", "console", "cli", "dashboard")
ACTIVE = ("queued", "running", "paused")
FINAL = ("done", "failed", "cancelled")
LOG_MAX = 500          # lines kept per job in memory
LOG_SAVED = 200        # lines written to jobs.jsonl
HISTORY_MAX = 300      # jobs loaded per project
WARNINGS_MAX = 100

# standard stage names for analyses (only the applicable ones per kind)
S_VALIDATE = "Input validation"
S_GEOMETRY = "Geometry preparation"
S_MESH = "Meshing"
S_SOLVER = "Solver"
S_POST = "Postprocessing"
S_READY = "Results ready"
ANALYSIS_STAGES = (S_VALIDATE, S_GEOMETRY, S_MESH, S_SOLVER, S_POST, S_READY)
STAGES_BY_KIND = {
    "mesh": (S_GEOMETRY, S_MESH, S_READY),
    "structural": ANALYSIS_STAGES, "thermal": ANALYSIS_STAGES, "modal": ANALYSIS_STAGES,
    "fluid": ANALYSIS_STAGES,
    "motion": (S_VALIDATE, S_SOLVER, S_POST, S_READY),
}


@dataclass
class Stage:
    name: str
    state: str = "pending"          # pending | running | done | failed | skipped
    started: Optional[float] = None
    finished: Optional[float] = None
    progress: Optional[float] = None
    detail: str = ""

    def to_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class Case:
    """One case of a parameter study."""
    name: str
    state: str = "queued"           # queued | running | done | failed | cancelled
    detail: str = ""
    error: str = ""
    started: Optional[float] = None
    finished: Optional[float] = None

    def to_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class Job:
    id: str
    project_path: str
    kind: str
    title: str
    target: str = ""
    analysis_type: str = ""
    source: str = "gui"
    state: str = "queued"
    stages: list = field(default_factory=list)
    progress: Optional[float] = None
    created: float = 0.0
    started: Optional[float] = None
    finished: Optional[float] = None
    log_lines: deque = field(default_factory=lambda: deque(maxlen=LOG_MAX))
    warnings: list = field(default_factory=list)
    inputs: dict = field(default_factory=dict)
    outputs: dict = field(default_factory=dict)
    children: list = field(default_factory=list)
    error: str = ""
    project_name: str = ""
    retry_of: Optional[str] = None
    retried_as: Optional[str] = None
    seq: int = 0
    _cb: dict = field(default_factory=dict, repr=False, compare=False)

    # -- derived
    @property
    def active(self) -> bool:
        return self.state in ACTIVE

    @property
    def log(self) -> list:
        """The log lines as text (levels: see :attr:`log_entries`)."""
        return [t for _, t in self.log_lines]

    @property
    def log_entries(self) -> list:
        return list(self.log_lines)

    def elapsed(self, now: Optional[float] = None) -> float:
        if self.started is None:
            return 0.0
        return max(0.0, (self.finished if self.finished is not None else (now or time.time())) - self.started)

    def stage_named(self, name: str) -> Optional[Stage]:
        return next((s for s in self.stages if s.name == name), None)

    def current_stage(self) -> Optional[Stage]:
        return next((s for s in self.stages if s.state == "running"), None)

    def case_counts(self) -> dict:
        c = {"total": len(self.children), "done": 0, "failed": 0, "running": 0, "queued": 0, "cancelled": 0}
        for ch in self.children:
            c[ch.state] = c.get(ch.state, 0) + 1
        return c

    def to_dict(self, persist: bool = False) -> dict:
        d = {"id": self.id, "project_path": self.project_path, "project_name": self.project_name, "kind": self.kind,
             "title": self.title, "target": self.target, "analysis_type": self.analysis_type,
             "source": self.source, "state": self.state, "stages": [s.to_dict() for s in self.stages],
             "progress": self.progress, "created": self.created, "started": self.started,
             "finished": self.finished,
             "log": [list(e) for e in self.log_entries[-(LOG_SAVED if persist else LOG_MAX):]],
             "warnings": list(self.warnings), "inputs": _clean(self.inputs), "outputs": _clean(self.outputs),
             "children": [c.to_dict() for c in self.children], "error": self.error,
             "retry_of": self.retry_of, "retried_as": self.retried_as, "seq": self.seq}
        if not persist:  # live view: what the buttons may do
            cb = self._cb
            d.update(can_cancel=self.active and "cancel" in cb, can_retry=self.state in ("failed", "cancelled") and
                     "retry" in cb, can_pause=self.state == "running" and "pause" in cb,
                     can_resume=self.state == "paused" and "resume" in cb,
                     has_opener=self.state == "done" and ("open" in cb or bool(self.outputs.get("opener"))))
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Job":
        j = cls(id=str(d["id"]), project_path=str(d.get("project_path", "")), kind=str(d["kind"]),
                title=str(d["title"]), target=str(d.get("target", "")),
                analysis_type=str(d.get("analysis_type", "")), source=str(d.get("source", "gui")),
                state=str(d.get("state", "failed")), progress=d.get("progress"), created=float(d.get("created") or 0),
                started=d.get("started"), finished=d.get("finished"), warnings=list(d.get("warnings") or []),
                inputs=dict(d.get("inputs") or {}), outputs=dict(d.get("outputs") or {}),
                error=str(d.get("error") or ""), project_name=str(d.get("project_name", "")),
                retry_of=d.get("retry_of"), retried_as=d.get("retried_as"), seq=int(d.get("seq") or 0))
        j.stages = [Stage(**{k: v for k, v in s.items() if k in Stage.__dataclass_fields__})
                    for s in d.get("stages") or []]
        j.children = [Case(**{k: v for k, v in c.items() if k in Case.__dataclass_fields__})
                      for c in d.get("children") or []]
        for e in d.get("log") or []:
            j.log_lines.append((str(e[0]), str(e[1])) if isinstance(e, (list, tuple)) and len(e) == 2
                               else ("info", str(e)))
        if j.state in ACTIVE:  # it was running when the app ended: say so instead of showing a ghost
            j.state, j.error = "failed", j.error or "The application closed while this was running."
            j.finished = j.finished or j.started or j.created
        return j


@dataclass
class Event:
    kind: str       # created | updated | log | finished
    job_id: str


def _clean(value: Any) -> Any:
    """JSON-safe copy (paths and other objects become strings)."""
    try:
        return json.loads(json.dumps(value, default=str))
    except (TypeError, ValueError):
        return {}


def standard_stages(kind: str) -> tuple:
    return STAGES_BY_KIND.get(kind, ())


class JobRegistry:
    def __init__(self, clock: Callable[[], float] = time.time):
        self._lock = threading.RLock()
        self._clock = clock
        self._jobs: dict[str, Job] = {}
        self._subs: list[Callable[[Event], None]] = []
        self._projects: dict[str, dict] = {}     # key -> {"name", "data_dir"}
        self._active_project: Optional[str] = None
        self._openers: dict[str, Callable] = {}
        self._seq = itertools.count(1)

    # ------------------------------------------------------------------ projects
    def register_project(self, key: str, name: Optional[str] = None, data_dir: Optional[Path] = None,
                         activate: bool = False) -> None:
        """Make a project known; loads its saved history (once per data folder)."""
        key = str(key)
        load = None
        with self._lock:
            info = self._projects.setdefault(key, {"name": name or Path(key).stem or key, "data_dir": None,
                                                   "loaded": None})
            if name:
                info["name"] = name
            if data_dir is not None and Path(data_dir) != info["data_dir"]:
                info["data_dir"] = Path(data_dir)
            dd = info["data_dir"]
            if dd is not None and info["loaded"] != dd:
                info["loaded"] = dd
                load = dd
            if activate:
                self._active_project = key
        if load is not None:
            self._load_history(key, load)

    def rename_project(self, old: str, new: str, name: Optional[str] = None, data_dir: Optional[Path] = None) -> None:
        """A project got a real path (first save): its jobs follow it."""
        old, new = str(old), str(new)
        if old == new:
            return
        with self._lock:
            moved = []
            for j in self._jobs.values():
                if j.project_path == old:
                    j.project_path = new
                    j.project_name = name or j.project_name
                    if j.state in FINAL:
                        moved.append(j)
            if self._active_project == old:
                self._active_project = new
            self._projects.pop(old, None)
        self.register_project(new, name, data_dir)
        if data_dir is not None:
            for j in sorted(moved, key=lambda x: x.seq):
                self._persist(j)

    def set_active_project(self, key: Optional[str]) -> None:
        with self._lock:
            self._active_project = None if key is None else str(key)

    @property
    def active_project(self) -> Optional[str]:
        return self._active_project

    def project_name(self, key: str) -> str:
        with self._lock:
            return self._projects.get(str(key), {}).get("name") or Path(str(key)).stem or str(key)

    def projects(self) -> list:
        with self._lock:
            return list(self._projects)

    def history_path(self, key: str) -> Optional[Path]:
        with self._lock:
            dd = self._projects.get(str(key), {}).get("data_dir")
        return Path(dd) / "jobs.jsonl" if dd else None

    def _load_history(self, key: str, data_dir: Path) -> None:
        f = Path(data_dir) / "jobs.jsonl"
        if not f.exists():
            return
        loaded: list[Job] = []
        try:
            for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
                if not line.strip():
                    continue
                try:
                    loaded.append(Job.from_dict(json.loads(line)))
                except (ValueError, KeyError, TypeError):
                    continue
        except OSError:
            return
        with self._lock:
            for j in loaded[-HISTORY_MAX:]:
                if j.id in self._jobs:
                    continue
                j.project_path = key
                j.project_name = j.project_name or self._projects[key]["name"]
                j.seq = next(self._seq)
                self._jobs[j.id] = j
        for j in loaded[-HISTORY_MAX:]:
            self._emit(Event("created", j.id))

    def _persist(self, job: Job) -> None:
        path = self.history_path(job.project_path)
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(job.to_dict(persist=True), default=str) + "\n")
        except OSError:
            log.exception("could not write %s", path)

    # ------------------------------------------------------------------ events
    def subscribe(self, callback: Callable[[Event], None]) -> Callable[[], None]:
        """Call ``callback(Event)`` on every change, in the thread that made it. Returns an unsubscribe function."""
        with self._lock:
            self._subs.append(callback)

        def off():
            with self._lock:
                if callback in self._subs:
                    self._subs.remove(callback)
        return off

    def _emit(self, ev: Event) -> None:
        with self._lock:
            subs = list(self._subs)
        for cb in subs:
            try:
                cb(ev)
            except Exception:  # noqa: BLE001 - a broken listener never stops a run
                log.exception("job event listener failed")

    # ------------------------------------------------------------------ create / update
    def create_job(self, project_path: str, kind: str, title: str, target: str = "", analysis_type: str = "",
                   source: str = "gui", *, stages=None, inputs: Optional[dict] = None, start: bool = True,
                   cancel=None, retry=None, pause=None, resume=None, opener=None,
                   project_name: Optional[str] = None, retry_of: Optional[str] = None) -> Job:
        """Register a job. ``stages`` None gives the standard stages of the kind; pass ``[]`` for none.
        ``start=False`` leaves it ``queued`` until :meth:`start`."""
        project_path = str(project_path)
        with self._lock:
            info = self._projects.get(project_path)
        if info is None:
            self.register_project(project_path, project_name)
        now = self._clock()
        names = standard_stages(kind) if stages is None else tuple(stages)
        job = Job(id=uuid.uuid4().hex[:12], project_path=project_path, kind=kind if kind in KINDS else "other",
                  title=title, target=target, analysis_type=analysis_type,
                  source=source if source in SOURCES else "gui", created=now, inputs=dict(inputs or {}),
                  stages=[Stage(n) for n in names], retry_of=retry_of)
        with self._lock:
            job.project_name = project_name or self.project_name(project_path)
            job.seq = next(self._seq)
            for k, fn in (("cancel", cancel), ("retry", retry), ("pause", pause), ("resume", resume),
                          ("open", opener)):
                if fn is not None:
                    job._cb[k] = fn
            self._jobs[job.id] = job
            if start:
                job.state, job.started = "running", now
        self._emit(Event("created", job.id))
        return job

    def set_callbacks(self, job, **callbacks) -> None:
        """Register cancel / retry / pause / resume / open callbacks (None removes one)."""
        j = self._job(job)
        with self._lock:
            for k, fn in callbacks.items():
                if fn is None:
                    j._cb.pop(k, None)
                else:
                    j._cb[k] = fn
        self._emit(Event("updated", j.id))

    def start(self, job) -> None:
        j = self._job(job)
        with self._lock:
            if j.state == "queued":
                j.state, j.started = "running", self._clock()
        self._emit(Event("updated", j.id))

    def stage(self, job, name: str, state: str, progress: Optional[float] = None,
              detail: Optional[str] = None) -> Stage:
        """Set a stage. Starting a stage closes the running one before it; an unknown name is appended."""
        j = self._job(job)
        now = self._clock()
        with self._lock:
            st = j.stage_named(name)
            if st is None:
                st = Stage(name)
                j.stages.append(st)
            if state == "running":
                for other in j.stages:
                    if other is not st and other.state == "running":
                        other.state, other.finished = "done", now
                if j.state == "queued":
                    j.state, j.started = "running", now
            if state in ("running", "done", "failed") and st.started is None:
                st.started = now
            if state in ("done", "failed", "skipped"):
                st.finished = now
                if state == "done" and progress is None and st.progress is not None:
                    st.progress = 1.0
            st.state = state
            if progress is not None:
                st.progress = max(0.0, min(1.0, float(progress)))
            if detail is not None:
                st.detail = detail
        self._emit(Event("updated", j.id))
        return st

    def drop_stage(self, job, name: str) -> None:
        """Remove a stage that turned out not to be part of this job."""
        j = self._job(job)
        with self._lock:
            j.stages = [s for s in j.stages if s.name != name]
        self._emit(Event("updated", j.id))

    def set_progress(self, job, fraction: Optional[float]) -> None:
        """Overall progress, only from a real source (backend report or done/total of countable cases)."""
        j = self._job(job)
        with self._lock:
            j.progress = None if fraction is None else max(0.0, min(1.0, float(fraction)))
        self._emit(Event("updated", j.id))

    def set_inputs(self, job, **inputs) -> None:
        j = self._job(job)
        with self._lock:
            j.inputs.update(inputs)
        self._emit(Event("updated", j.id))

    def log(self, job, line: str, level: str = "info") -> None:
        """Append a log line (bounded). ``level`` ``warning`` also lands in ``job.warnings``."""
        j = self._job(job)
        text = str(line).rstrip()
        if not text:
            return
        with self._lock:
            lv = {"warn": "warning", "warning": "warning", "error": "error", "err": "error"}.get(level, "info")
            j.log_lines.append((lv, text))
            if lv == "warning" and text not in j.warnings and len(j.warnings) < WARNINGS_MAX:
                j.warnings.append(text)
        self._emit(Event("log", j.id))

    # parameter studies --------------------------------------------------------------------
    def set_cases(self, job, names) -> None:
        j = self._job(job)
        with self._lock:
            j.children = [Case(str(n)) for n in names]
            self._case_progress(j)
        self._emit(Event("updated", j.id))

    def case(self, job, name: str, state: str, detail: Optional[str] = None, error: Optional[str] = None) -> None:
        """Set one case (added if new). Overall progress follows as finished cases / total cases."""
        j = self._job(job)
        now = self._clock()
        with self._lock:
            c = next((x for x in j.children if x.name == name), None)
            if c is None:
                c = Case(name)
                j.children.append(c)
            if state == "running" and c.started is None:
                c.started = now
            if state in ("done", "failed", "cancelled"):
                c.finished = now
            c.state = state
            if detail is not None:
                c.detail = detail
            if error is not None:
                c.error = error
            self._case_progress(j)
        self._emit(Event("updated", j.id))

    @staticmethod
    def _case_progress(j: Job) -> None:
        n = len(j.children)
        j.progress = (sum(1 for c in j.children if c.state in ("done", "failed", "cancelled")) / n) if n else None

    # ------------------------------------------------------------------ finish / cancel / retry
    def finish(self, job, ok: bool, error: Optional[str] = None, outputs: Optional[dict] = None,
               cancelled: bool = False) -> Job:
        """End a job. A job already cancelled stays cancelled (late results are still kept)."""
        j = self._job(job)
        now = self._clock()
        persist = False
        with self._lock:
            if outputs:
                j.outputs.update(outputs)
            if j.state in FINAL:
                pass
            else:
                state = "cancelled" if cancelled else ("done" if ok else "failed")
                j.state, j.finished = state, now
                if j.started is None:
                    j.started = now
                if error:
                    j.error = str(error)
                for s in j.stages:
                    if s.state == "running":
                        s.state, s.finished = ("done" if ok else "failed"), now
                    elif s.state == "pending" and ok:
                        s.state = "skipped"
                for c in j.children:
                    if c.state in ("queued", "running") and state != "done":
                        c.state = "cancelled" if cancelled else c.state
                if ok:
                    j.error = ""
                    if j.progress is not None:
                        j.progress = 1.0
                persist = True
        if persist:
            self._persist(j)
        self._emit(Event("finished", j.id))
        return j

    def cancel(self, job_id: str) -> bool:
        """Ask the job to stop (runs its cancel callback) and mark it cancelled. False if it cannot be cancelled."""
        j = self._jobs.get(job_id)
        if j is None or not j.active:
            return False
        cb = j._cb.get("cancel")
        if cb is None:
            return False
        try:
            cb()
        except Exception:  # noqa: BLE001
            log.exception("cancel callback failed")
        self.log(j, "Cancelled by you.", "warning")
        self.finish(j, False, error="Cancelled.", cancelled=True)
        return True

    def retry(self, job_id: str) -> Optional[Job]:
        """Start the same operation again. Returns the new job (linked to the old one) or None."""
        j = self._jobs.get(job_id)
        if j is None or j.state not in ("failed", "cancelled") or "retry" not in j._cb:
            return None
        before = set(self._jobs)
        try:
            new = j._cb["retry"]()
        except Exception:  # noqa: BLE001
            log.exception("retry callback failed")
            return None
        if not isinstance(new, Job):
            fresh = [x for x in self._jobs.values() if x.id not in before]
            new = fresh[-1] if fresh else None
        if new is not None:
            with self._lock:
                new.retry_of, j.retried_as = j.id, new.id
            self._emit(Event("updated", j.id))
            self._emit(Event("updated", new.id))
        return new

    def pause(self, job_id: str) -> bool:
        j = self._jobs.get(job_id)
        if j is None or j.state != "running" or "pause" not in j._cb:
            return False
        j._cb["pause"]()
        with self._lock:
            j.state = "paused"
        self._emit(Event("updated", j.id))
        return True

    def resume(self, job_id: str) -> bool:
        j = self._jobs.get(job_id)
        if j is None or j.state != "paused" or "resume" not in j._cb:
            return False
        j._cb["resume"]()
        with self._lock:
            j.state = "running"
        self._emit(Event("updated", j.id))
        return True

    # ------------------------------------------------------------------ openers
    def register_opener(self, key: str, fn: Callable[[Job], None]) -> None:
        """``fn(job)`` shows the results of a finished job whose ``outputs["opener"] == key``."""
        with self._lock:
            self._openers[key] = fn

    def can_open(self, job) -> bool:
        j = self._job(job)
        return j.state == "done" and ("open" in j._cb or j.outputs.get("opener") in self._openers)

    def open_results(self, job_id: str) -> bool:
        j = self._jobs.get(job_id)
        if j is None or j.state != "done":
            return False
        fn = j._cb.get("open") or self._openers.get(j.outputs.get("opener"))
        if fn is None:
            return False
        try:
            fn(j)
        except Exception:  # noqa: BLE001
            log.exception("open results failed")
            return False
        return True

    # ------------------------------------------------------------------ queries
    def _job(self, job) -> Job:
        if isinstance(job, Job):
            return job
        return self._jobs[job]

    def get(self, job_id: str) -> Optional[Job]:
        return self._jobs.get(job_id)

    def snapshot(self, job_id: str) -> Optional[dict]:
        """A consistent plain-dict copy of a job (safe to read from the GUI thread)."""
        with self._lock:
            j = self._jobs.get(job_id)
            return j.to_dict() if j is not None else None

    def jobs(self, project: Optional[str] = None) -> list:
        """Jobs of one project (``None``: all), newest first."""
        with self._lock:
            out = [j for j in self._jobs.values() if project is None or j.project_path == str(project)]
        return sorted(out, key=lambda j: j.seq, reverse=True)

    def active(self, project: Optional[str] = None) -> list:
        return [j for j in self.jobs(project) if j.active]

    def find_running(self, kind: Optional[str], target: str, project: Optional[str] = None) -> Optional[Job]:
        """The queued/running job for the same kind and target (in ``project``, default the active one)."""
        proj = str(project) if project is not None else self._active_project
        for j in self.jobs(proj):
            if j.active and (kind is None or j.kind == kind) and j.target == target:
                return j
        return None

    def counts(self, project: Optional[str] = None) -> dict:
        c = {"running": 0, "queued": 0, "paused": 0, "failed": 0, "done": 0, "cancelled": 0}
        for j in self.jobs(project):
            c[j.state] = c.get(j.state, 0) + 1
        return c


# ---------------------------------------------------------------------------------------------- default
_default: Optional[JobRegistry] = None
_default_lock = threading.Lock()


def default() -> JobRegistry:
    """The registry the whole application shares (created on first use)."""
    global _default
    with _default_lock:
        if _default is None:
            _default = JobRegistry()
        return _default


def set_default(reg: Optional[JobRegistry]) -> None:
    global _default
    with _default_lock:
        _default = reg
