"""Executors of the console commands: every command goes through the services the GUI already uses.

* ``open`` / ``select`` use the main window's own open and selection code (the tree and viewport paths);
* ``run structural|thermal|modal`` create or reuse a Studio study and call ``cmd_generate_mesh`` / ``cmd_solve``,
  the same handlers as the ribbon, so the Progress Monitor shows the same jobs (marked ``source = "console"``);
* ``run motion`` runs the multibody study through the Assembly workspace / ``track_mbd_run``;
* ``iterate`` is a parameter study: one registry job with one case per value, run sequentially through the same
  mesh and solve handlers, the original value restored at the end.

Nothing is faked: when something cannot run, the console says so in one sentence and offers the fix as a chip.
"""
from __future__ import annotations

import copy
import csv
import logging
import re
import time
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QObject, Qt, QTimer, Signal

from opencae.examples.example_copy import copy_example, examples_dir, projects_home  # noqa: F401 (shared helper)
from opencae.ui.commands import grammar as G
from opencae.ui.commands.grammar import ANALYSIS_LABEL, ANALYSIS_TYPE, Context, Entity, Intent

log = logging.getLogger("opencae.console")

JOBS_KEY = "opencae.ui.jobs"
ASSEMBLY_KEY = "opencae.ui.assembly"
SIDE_WORDS = {"left", "right", "front", "back", "rear", "upper", "lower", "top", "bottom", "inner", "outer", "fl",
              "fr", "rl", "rr", "first", "second", "third", "fourth"}
CONSOLE_OPENER = "console.study"
STUDY_PREFIX = {"structural": "Structural", "thermal": "Thermal", "modal": "Modal", "fluid": "Flow",
                "motion": "Motion"}


# ------------------------------------------------------------------------------------------------ small helpers
def esc(text) -> str:
    import html
    return html.escape(str(text))


def job_link(job) -> str:
    return f'<a href="job:{job.id}">job #{job.seq}</a>'


def cmd_link(label: str, command: str = "") -> str:
    return f'<a href="cmd:{esc(command or label)}">{esc(label)}</a>'


def _strip_side(name: str) -> str:
    toks = [t for t in re.split(r"[\s_\-]+", name.lower()) if t and t not in SIDE_WORDS and not t.isdigit()]
    return " ".join(toks)


# ------------------------------------------------------------------------------------------------ parameters
def discover_parameters(session) -> list:
    """The geometry parameters a user would vary, named like people say them ("finger thickness").

    Boxes get thickness (smallest side), width and length (largest); cylinders radius and height. Parts that differ
    only by a side or a number (Left / Right finger, Rotor 1..4) also appear as one group parameter that changes all
    of them together ("finger thickness"). Changing a thickness or width keeps the part centred; a length grows from
    its start edge.
    """
    p = session.project
    us = session.units
    ents: list = []
    rows: list = []                        # (feature, key, label, centred)
    names = p.geometry.body_names
    for f in p.geometry.features:
        if f.suppressed or not f.name:
            continue
        fname = names.get(f.id) or f.name          # the body's own name ("Left finger"), not "Box 1"
        pr = f.params
        if f.kind == "primitive":
            shape = pr.get("shape")
            if shape == "box" and all(k in pr for k in ("dx", "dy", "dz")):
                dims = sorted((("dx", float(pr["dx"])), ("dy", float(pr["dy"])), ("dz", float(pr["dz"]))),
                              key=lambda kv: (kv[1], kv[0]))
                labels = {dims[0][0]: "thickness", dims[1][0]: "width", dims[2][0]: "length"}
                for k, lab in labels.items():
                    rows.append((f, k, lab, lab in ("thickness", "width")))
            elif shape in ("cylinder", "cone", "sphere"):
                if "radius" in pr:
                    rows.append((f, "radius", "radius", False))
                if "height" in pr:
                    thin = float(pr["height"]) < 0.3 * float(pr.get("radius", 1e9))
                    rows.append((f, "height", "thickness" if thin else "height", False))
            elif shape == "cantilever_beam":
                for k, lab in (("length", "length"), ("width", "width"), ("height", "height")):
                    if k in pr:
                        rows.append((f, k, lab, False))
            elif shape in ("plate", "beam"):
                for k, v in pr.items():
                    if isinstance(v, (int, float)) and k not in ("position",):
                        rows.append((f, k, k.replace("_", " "), False))
        elif f.kind in ("fillet", "chamfer") and ("radius" in pr or "distance" in pr):
            k = "radius" if "radius" in pr else "distance"
            rows.append((f, k, k, False))
        elif f.kind == "extrude" and "distance" in pr:
            rows.append((f, "distance", "distance", False))
    by_group: dict = {}
    fname_of = {f.id: (names.get(f.id) or f.name) for f in p.geometry.features}
    for f, key, lab, centred in rows:
        member = {"feature": f.id, "key": key, "centred": centred, "shape": f.params.get("shape", f.kind)}
        ents.append(Entity(f"{fname_of[f.id]} {lab}", "parameter", f"{f.id}:{key}",
                           {"unit": us.length, "value": float(f.params[key]), "members": [member],
                            "feature": f.id, "label": lab, "group": False}))
        base = _strip_side(fname_of[f.id])
        if base:
            by_group.setdefault((base, lab, member["shape"]), []).append((f, key, member))
    for (base, lab, _shape), items in by_group.items():
        if len(items) < 2:
            continue
        vals = [float(f.params[k]) for f, k, _ in items]
        ents.append(Entity(f"{base} {lab}", "parameter", f"group:{base}:{lab}",
                           {"unit": us.length, "value": vals[0], "members": [m for _, _, m in items],
                            "feature": items[0][0].id, "label": lab, "group": True,
                            "same": max(vals) - min(vals) < 1e-9}))
    return ents


def set_parameter(session, ent: Entity, value: float) -> None:
    """Apply ``value`` to the parameter (every member of a group) and regenerate the geometry."""
    feats = {f.id: f for f in session.project.geometry.features}
    for m in ent.data["members"]:
        f = feats[m["feature"]]
        old = float(f.params[m["key"]])
        f.params[m["key"]] = float(value)
        if m.get("centred") and m.get("shape") == "box" and isinstance(f.params.get("position"), list):
            ax = {"dx": 0, "dy": 1, "dz": 2}[m["key"]]
            pos = list(f.params["position"])
            pos[ax] = float(pos[ax]) - (float(value) - old) / 2.0
            f.params["position"] = pos
    session.regenerate()


def snapshot_parameter(session, ent: Entity) -> list:
    feats = {f.id: f for f in session.project.geometry.features}
    return [(m["feature"], copy.deepcopy(feats[m["feature"]].params)) for m in ent.data["members"]]


def restore_parameter(session, snap: list) -> None:
    feats = {f.id: f for f in session.project.geometry.features}
    for fid, params in snap:
        if fid in feats:
            feats[fid].params = copy.deepcopy(params)
    session.regenerate()


def rebind_references(session, study) -> int:
    """After a parameter changed the geometry, point the study's face references at the same faces again.

    A reference stores a signature (area, centroid, normal) that no longer matches once a dimension changed. A face
    keeps its index when only a dimension changes, so the reference is renewed when the face at that index still has
    the same type and orientation. Anything else stays broken (and is reported by the validation, never guessed).
    """
    import numpy as np
    from opencae.geometry.references import make_ref
    n = 0
    for it in list(study.constraints) + list(study.loads):
        for r in it.targets:
            b = session.bodies.get(r.body)
            if b is None or r.kind != "face" or not (0 <= r.index < len(b.faces)):
                continue
            f, sig = b.faces[r.index], r.signature or {}
            if f.surface_type != sig.get("type") or (f.normal is None) != (sig.get("normal") is None):
                continue
            if f.normal is not None and float(np.dot(f.normal, sig["normal"])) < 1 - 1e-6:
                continue
            r.signature = make_ref(b, "face", r.index).signature
            n += 1
    return n


def metrics_of(session) -> dict:
    """Key results of the active study's current results: {name: (value, unit)}; empty when there are none."""
    rs = session.results
    if rs is None:
        return {}
    us = session.units
    kind = getattr(rs, "kind", "structural")
    out: dict = {}
    try:
        if kind == "modal":
            fr = rs.summary().get("frequencies_hz") or []
            if len(fr):
                out["First natural frequency"] = (float(fr[0]), "Hz")
        elif kind == "thermal":
            t = rs.quantity("T")
            out["Max temperature"] = (float(t.max()), "°C")
            out["Min temperature"] = (float(t.min()), "°C")
        elif kind == "structural":
            sm = rs.summary()
            out["Max displacement"] = (float(sm["max_displacement"]), us.length)
            if "max_von_mises_nodal" in sm:
                out["Max von Mises stress"] = (float(sm["max_von_mises_nodal"]), us.stress)
    except Exception:  # noqa: BLE001 - a missing number is reported as missing, never invented
        log.exception("could not read the key results")
    return out


# ------------------------------------------------------------------------------------------------ study record
class StudyRecord:
    """The finished parameter study: what 'show table', 'show plot' and 'export csv' use."""

    def __init__(self, title: str, param: str, unit: str, original: float):
        self.title, self.param, self.unit, self.original = title, param, unit, original
        self.rows: list = []         # {"value", "state", "metrics": {name: (v, unit)}, "error"}
        self.csv_path: Optional[Path] = None
        self.job_id = ""

    @property
    def metric_names(self) -> list:
        names: list = []
        for r in self.rows:
            for k in r["metrics"]:
                if k not in names:
                    names.append(k)
        return names

    def table_rows(self) -> list:
        names = self.metric_names
        head = [f"{self.param} [{self.unit}]" if self.unit else self.param] + \
               [f"{n} [{next((r['metrics'][n][1] for r in self.rows if n in r['metrics']), '')}]" for n in names] + \
               ["Status"]
        body = []
        for r in self.rows:
            body.append([f"{r['value']:g}"] + [f"{r['metrics'][n][0]:.4g}" if n in r["metrics"] else "-" for n in names]
                        + [r["state"] if r["state"] != "failed" else f"failed: {r.get('error', '')}"])
        return [head] + body

    def to_csv(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            for row in self.table_rows():
                w.writerow(row)
        self.csv_path = path
        return path


# ------------------------------------------------------------------------------------------------ job watching
class _Bridge(QObject):
    fin = Signal(str)


class _Chain:
    """One console run of a Studio study: mesh (if needed) and then solve."""

    def __init__(self, study_id: str, name: str, analysis: str):
        self.study_id, self.name, self.analysis = study_id, name, analysis
        self.stage = "start"
        self.job = None
        self.cancelled = False


# ------------------------------------------------------------------------------------------------ the actions
class Actions:
    """What each intent does. ``c`` is the console (UI sink): say_html, set_chips, ask, ask_form."""

    def __init__(self, c):
        self.c = c
        self.win = c.win
        self._chains: dict = {}
        self._watch: dict = {}
        self._study: Optional["ParameterStudy"] = None
        self.last_study: Optional[StudyRecord] = None
        self.bridge = _Bridge()
        self.bridge.fin.connect(self._on_finished, Qt.QueuedConnection)
        self._off = None
        reg = self.reg
        if reg is not None:
            self._off = reg.subscribe(lambda ev: self.bridge.fin.emit(ev.job_id) if ev.kind == "finished" else None)
            reg.register_opener(CONSOLE_OPENER, self._open_study_job)

    # ---------------------------------------------------------------- shared state
    @property
    def ext(self):
        return getattr(self.win, "extensions", {}).get(JOBS_KEY)

    @property
    def reg(self):
        ext = self.ext
        if ext is not None:
            return ext.reg
        from opencae.ui.jobs import registry as R
        return R.default()

    @property
    def session(self):
        return self.win.session

    def project_key(self) -> str:
        ext = self.ext
        return ext.sync_project() if ext is not None else ""

    def assembly_ext(self):
        return getattr(self.win, "extensions", {}).get(ASSEMBLY_KEY)

    def assembly_ws(self):
        ext = self.assembly_ext()
        return ext.workspace() if ext is not None else None

    def mode(self) -> str:
        w = self.win
        if w.mode == "drone":
            return "drone"
        ext = self.assembly_ext()
        if ext is not None and getattr(ext, "_ws", None) is not None and w.stack.currentWidget() is ext._ws:
            return "assembly"
        return "studio" if w.mode == "studio" else w.mode

    def go_studio(self) -> None:
        w = self.win
        if w.stack.currentWidget() is not w.viewport:
            w.ribbon.setCurrentIndex(0)
            w.stack.setCurrentWidget(w.viewport)

    # ---------------------------------------------------------------- context
    def context(self) -> Context:
        win, s = self.win, self.session
        comps: list = []
        asm = s.project.assembly
        rigid_of = {}
        for rb in asm.bodies:
            for cid in rb.component_ids:
                rigid_of[cid] = rb.id
        comp_of = {c.body_id: c for c in asm.components}
        for gid, b in s.bodies.items():
            if b.kind != "solid":
                continue
            c = comp_of.get(gid)
            comps.append(Entity(b.name, "component", gid, {"geometry": gid, "component": c.id if c else "",
                                                            "rigid": rigid_of.get(c.id, "") if c else ""}))
        studies = [Entity(st.name, "study", st.id, {"type": st.analysis.analysis_type}) for st in s.project.studies]
        params = discover_parameters(s)
        drone_keys: list = []
        ws = getattr(win, "_drone", None)
        drone = bool(ws is not None and getattr(ws, "project_dir", None))
        if drone:
            try:
                from opencae.drone import spec as S
                from opencae.ui.console import leaf_keys
                sp = S.load(Path(ws.project_dir))
                drone_keys = leaf_keys(sp)
                for k in drone_keys:
                    pm = S.param(sp, k)
                    if pm is not None and isinstance(pm.value, (int, float)) and not isinstance(pm.value, bool):
                        params.append(Entity(k.replace(".", " ").replace("_", " "), "parameter", "drone:" + k,
                                             {"unit": G.norm_unit(pm.unit or "") if G.is_unit(pm.unit or "") else "",
                                              "unit_text": pm.unit or "", "value": pm.value, "key": k,
                                              "drone": True}))
            except Exception:  # noqa: BLE001
                log.exception("drone parameters unavailable")
        sel_names = []
        for pk in win.selection:
            b = s.bodies.get(pk.body)
            if b is not None and b.name not in sel_names:
                sel_names.append(b.name)
        jobs = [(j.seq, j.title, j.state) for j in self.reg.jobs(self.project_key() or None)[:20]]
        return Context(components=comps, studies=studies, parameters=params, projects=self.project_entities(),
                       selection=sel_names, mode=self.mode(), drone=drone, drone_keys=drone_keys, jobs=jobs,
                       project_name=s.project.name if comps else "")

    def project_entities(self) -> list:
        ents: list = []
        try:
            for p in sorted(examples_dir().glob("*.ocae")):
                ents.append(Entity(p.stem.replace("_", " "), "example", p.stem, {"path": str(p)}))
        except OSError:
            pass
        for key, label in (("quad250", "quad250"), ("k09l", "k09l")):
            ents.append(Entity(label, "example", key, {"drone": True}))
        try:
            for r in (self.win.settings.value("recent", [], list) or [])[:10]:
                if Path(r).exists():
                    ents.append(Entity(Path(r).stem.replace("_", " "), "example", Path(r).stem, {"path": r}))
        except Exception:  # noqa: BLE001
            pass
        seen, out = set(), []
        for e in ents:
            if e.name.lower() not in seen:
                seen.add(e.name.lower())
                out.append(e)
        return out

    # ---------------------------------------------------------------- dispatch
    def execute(self, it: Intent) -> None:
        """Run a parsed intent (the legacy CLI-style ones are the console's own)."""
        c = self.c
        for n in it.notes:
            c.say(n, "dim")
        if it.verb in ("unknown",) or (it.message and it.verb not in ("run", "select", "iterate", "open", "show",
                                                                       "set")):
            c.reply(it.message, "err" if it.verb == "unknown" else "warn", chips=it.suggestions)
            return
        if it.verb == "run" and (it.missing or it.message):
            return self._clarify_intent(it)
        if it.verb in ("select", "open", "iterate", "show", "set") and (it.missing or it.message):
            return self._clarify_intent(it)
        fn = {"open": self.open_target, "select": self.select, "run": self.run, "iterate": self.iterate,
              "show": self.show, "dashboard": self.open_dashboard, "stop": self.stop, "retry": self.retry,
              "status": self.status, "set": self.set_value, "history": self.history}.get(it.verb)
        if fn is None:
            c.reply(f"I cannot do '{it.verb}' yet.", "warn")
            return
        fn(it)

    def _clarify_intent(self, it: Intent) -> None:
        """A line that lacks something: ask in the panel (chips) when there are clear choices, else say it once."""
        c = self.c
        if it.suggestions and (it.candidates or "analysis" in it.missing or "what" in it.missing or
                               it.verb in ("select", "open")):
            labels = [self._label_for(it, s) for s in it.suggestions]
            c.ask(it.message or "Which one?", list(zip(labels, it.suggestions)), lambda line: c.execute_command(line))
            return
        if it.verb == "select" and "target" in it.missing and not it.candidates and not it.message:
            c.ask("Select which component?", [(e.name, f"select {e.name}") for e in self.context().components[:8]],
                  lambda line: c.execute_command(line))
            return
        c.reply(it.message or "I need a bit more information.", "warn", chips=it.suggestions)

    @staticmethod
    def _label_for(it: Intent, line: str) -> str:
        if it.candidates:
            for cand in sorted(it.candidates, key=lambda e: -len(e.name)):
                if cand.name in line:
                    return cand.name
        m = re.match(r"^run (\w+)", line)
        if "analysis" in it.missing and m:
            return ANALYSIS_LABEL.get(m.group(1), m.group(1))
        return line.split(" ", 1)[1] if " " in line else line

    # ---------------------------------------------------------------- open
    def open_target(self, it: Intent) -> None:
        c, win = self.c, self.win
        ent = it.entity
        if it.what == "results":
            return self.open_results()
        if ent is None:
            c.reply(it.message or "What should I open?", "warn", chips=it.suggestions)
            return
        if ent.data.get("drone"):
            win.enter_drone_mode()
            ok = win.drone_workspace().open_example(ent.id)
            c.reply(f"Opened {ent.name} in Drone mode." if ok else f"Could not open {ent.name}.",
                    "ok" if ok else "err",
                    chips=["run mass propulsion", "open dashboard"] if ok else None)
            return
        key = ent.id
        path = Path(ent.data.get("path", ""))
        asm_ext = self.assembly_ext()
        try:
            if asm_ext is not None and key in ("gripper", "drone_rotors") and path.parent == examples_dir():
                ok = asm_ext.workspace().open_example(key)
            else:
                if path.parent == examples_dir():
                    path = copy_example(key)
                self.go_studio()
                before = self.session.path
                win.cmd_open(str(path))
                ok = self.session.path is not None and Path(self.session.path) == Path(path) or \
                    self.session.path != before
        except Exception as exc:  # noqa: BLE001
            c.reply(f"Could not open {ent.name}: {exc}", "err")
            return
        if not ok:
            c.reply(f"Could not open {ent.name}.", "err")
            return
        ctx = self.context()
        nstud = len(self.session.project.studies)
        c.reply(f"Opened {ent.name}: {len(ctx.components)} component(s), {nstud} stud{'y' if nstud == 1 else 'ies'}.",
                "ok", chips=self.next_steps(ctx))

    def next_steps(self, ctx: Context) -> list:
        steps: list = []
        parts = [e.name for e in ctx.components]
        asm = self.session.project.assembly
        movable = [e.name for e in ctx.components if e.data.get("rigid") and not
                   next((b.grounded for b in asm.bodies if b.id == e.data["rigid"]), False)]
        first = (movable or parts or [""])[0]
        if first:
            steps.append(f"select {first}")
        if asm.joints and ctx.project_name:
            steps.append(f"run motion on {ctx.project_name}")
        if first:
            steps.append(f"run structural on {first}")
        return steps[:4]

    # ---------------------------------------------------------------- select
    def select(self, it: Intent) -> None:
        c, win, s = self.c, self.win, self.session
        from opencae.ui.viewport import Pick
        if it.what == "none":
            win._select([])
            ws = self.assembly_ws() if self.mode() == "assembly" else None
            if ws is not None:
                ws.select("assembly", "")
            c.reply("Selection cleared.", "ok")
            return
        if it.assembly:
            ids = [g for g, b in s.bodies.items() if b.kind == "solid"]
            win._select([Pick("body", g) for g in ids])
            c.reply(f"Selected the whole assembly ({len(ids)} components).", "ok",
                    chips=["run motion", "run structural on assembly"])
            return
        ent = it.entity
        if ent is None:
            c.reply(it.message or "Select which component?", "warn", chips=it.suggestions)
            return
        self.select_entity(ent)
        c.reply(f"Selected {ent.name}.", "ok", chips=[f"run structural on {ent.name}", f"run modal on {ent.name}"])

    def select_entity(self, ent: Entity) -> None:
        """The same selection a click on the tree or in the viewport makes (main window state, tree, properties)."""
        win = self.win
        gid = ent.data.get("geometry", ent.id)
        if win.view_mode == "results":
            win.view_mode = "geometry"
        ws = self.assembly_ws() if self.mode() == "assembly" else None
        picked = False
        try:
            from opencae.ui.model_tree import ROLE
            from PySide6.QtWidgets import QTreeWidgetItemIterator
            it = QTreeWidgetItemIterator(win.tree)
            while it.value():
                item = it.value()
                d = item.data(0, ROLE)
                if d and d[0] == "body" and d[1] == gid:
                    win.tree.setCurrentItem(item)       # emits the same signal as a click on the tree
                    picked = True
                    break
                it += 1
        except Exception:  # noqa: BLE001
            log.exception("tree selection failed")
        if not picked:
            win.on_tree_selected("body", gid)
        from opencae.ui.viewport import Pick
        if [p.key() for p in win.selection] != [("body", gid, -1)]:
            win._select([Pick("body", gid)])
        if ws is not None:
            if ent.data.get("component"):
                ws.select("component", ent.data["component"])
            elif ent.data.get("rigid"):
                ws.select("body", ent.data["rigid"])

    # ---------------------------------------------------------------- targets
    def _targets(self, it: Intent) -> Optional[list]:
        """The components of a run: named, assembly, or the current selection; None when it has to ask first."""
        c, s = self.c, self.session
        ctx = self.context()
        if it.entity is not None:
            return [it.entity]
        if it.assembly:
            return [e for e in ctx.components]
        sel = [e for e in ctx.components if e.name in ctx.selection]
        if sel:
            return sel
        active = [e for e in ctx.components if e.id in s.study.analysis_body_ids()]
        if len(ctx.components) == 1:
            return ctx.components
        if active and s.study.analysis.analysis_type != "mbd" and len(active) < len(ctx.components):
            return active
        if not ctx.components:
            c.reply("This project has no solid component to analyse. Open a project first.", "warn",
                    chips=[f"open {e.name}" for e in ctx.projects[:3]])
            return None
        verb = f"run {it.analysis} on" if it.analysis else "run"
        c.ask(f"Run {ANALYSIS_LABEL.get(it.analysis, 'the analysis').lower()} on which component?",
              [(e.name, f"{verb} {e.name}") for e in ctx.components[:10]] + [("Whole assembly", f"{verb} assembly")],
              lambda line: c.execute_command(line))
        return None

    # ---------------------------------------------------------------- run
    def run(self, it: Intent) -> None:
        a = it.analysis
        if a == "motion":
            return self._run_motion(it)
        if a == "fluid" and self.mode() == "drone":
            return self.c.execute_command("run cfd")
        if a != "fluid" and not self._need_ccx():
            return
        targets = self._targets(it)
        if targets is None:
            return
        if a == "fluid":
            return self._run_fluid(targets, it)
        self._run_studio(a, targets, it, lambda study: self._launch(study, a))

    def _body_ids(self, targets: list) -> list:
        return [t.data.get("geometry", t.id) for t in targets]

    def _label_study(self, analysis: str, targets: list) -> str:
        names = [t.name for t in targets]
        what = names[0] if len(names) == 1 else (f"{len(names)} components" if len(names) < len(self.context().components)
                                                 else "assembly")
        return f"{STUDY_PREFIX[analysis]}: {what}"

    def _run_studio(self, analysis: str, targets: list, it: Optional[Intent], then: Callable) -> None:
        """Find or create the study, ask for what is missing, then call ``then(study)``."""
        c, win, s = self.c, self.win, self.session
        if self.mode() != "studio":
            self.go_studio()
        ids = self._body_ids(targets)
        atype = ANALYSIS_TYPE[analysis]
        study = self._find_study(atype, ids)
        if study is None:
            name = self._label_study(analysis, targets)
            made: dict = {}

            def make():
                st = s.new_study(name, atype, ids)
                made["st"] = st
                self._carry_material(st, ids)
            win.mutate("New study (console)", make, regen=False)
            study = made["st"]
        elif s.project.active_study != study.id:
            win.cmd_activate_study(study.id)
        missing = self._missing(study, analysis, ids)
        self._resolve_missing(study, analysis, ids, targets, it, missing, then)

    def _find_study(self, atype: str, ids: list):
        s = self.session
        cands = [st for st in s.project.studies if st.analysis.analysis_type == atype and
                 sorted(st.analysis_body_ids()) == sorted(ids)]
        cur = next((st for st in cands if st.id == s.project.active_study), None)
        return cur or (cands[0] if cands else None)

    def _carry_material(self, st, ids: list) -> None:
        """A new study keeps the material the part already has in another study."""
        p = self.session.project
        for bid in ids:
            if any(a.body == bid for a in st.assignments):
                continue
            for other in p.studies:
                a = next((x for x in other.assignments if x.body == bid), None)
                if a is not None:
                    p.assign_material(bid, a.material)
                    break

    def _missing(self, study, analysis: str, ids: list) -> list:
        p = self.session.project
        out: list = []
        if analysis != "fluid" and any(p.material_for_body(b, study) is None for b in ids):
            out.append("material")
        cons = [x for x in study.constraints if not x.suppressed]
        loads = [x for x in study.loads if not x.suppressed]
        if analysis == "structural":
            if not any(x.kind in ("fixed", "displacement", "symmetry") for x in cons):
                out.append("support")
            if not loads:
                out.append("load")
        elif analysis == "thermal":
            temps = [x for x in cons if x.kind == "temperature"]
            heat = [x for x in loads if x.kind in ("heat_flux", "heat_power", "heat_generation")]
            sink = [x for x in loads if x.kind in ("convection", "radiation")]
            if (not temps and not sink) or (not heat and len(temps) < 2):
                out.append("thermal")
        return out

    def _resolve_missing(self, study, analysis, ids, targets, it, missing: list, then: Callable) -> None:
        c, s = self.c, self.session
        if not missing:
            then(study)
            return
        name = targets[0].name if len(targets) == 1 else "the model"
        if "material" in missing:
            mats = [m for m in s.project.materials if not m.validate()]
            c.ask(f"{name} has no material. Which one?", [(m.name, m.id) for m in mats[:12]],
                  lambda mid: self._apply_material(study, analysis, ids, targets, it, mid, then))
            return
        body = s.bodies.get(ids[0])
        if body is None:
            c.reply("The component's geometry is not available.", "err")
            return
        if analysis == "structural":
            self._ask_structural(study, analysis, ids, targets, it, body, missing, then)
        elif analysis == "thermal":
            self._ask_thermal(study, analysis, ids, targets, it, body, then)
        else:
            then(study)

    def _apply_material(self, study, analysis, ids, targets, it, mid: str, then: Callable) -> None:
        win, s = self.win, self.session

        def fn():
            for b in ids:
                s.project.assign_material(b, mid)
        if s.project.active_study != study.id:
            win.cmd_activate_study(study.id)
        win.mutate("Assign material (console)", fn, regen=False)
        self._resolve_missing(study, analysis, ids, targets, it, self._missing(study, analysis, ids), then)

    # ---- the set-up forms
    def _face_options(self, body) -> list:
        """Planar faces as readable choices: '-Y end (area 40)' ..."""
        import numpy as np
        us = self.session.units
        opts = []
        lo, hi = body.bbox
        for f in body.faces:
            if f.normal is None:
                continue
            n = np.asarray(f.normal)
            ax = int(np.argmax(np.abs(n)))
            if abs(n[ax]) < 0.99:
                continue
            sign = "+" if n[ax] > 0 else "-"
            opts.append((f.index, f"{sign}{'XYZ'[ax]} side, face {f.index + 1} (area {f.area:.4g} {us.length}²)",
                         ax, sign, float(f.area)))
        return opts

    def _beam_axis(self, body) -> int:
        import numpy as np
        ext = np.asarray(body.bbox[1]) - np.asarray(body.bbox[0])
        return int(np.argmax(ext))

    def _ask_structural(self, study, analysis, ids, targets, it, body, missing, then) -> None:
        from opencae.ui.forms import Field
        c, s = self.c, self.session
        us = s.units
        opts = self._face_options(body)
        if not opts:
            c.reply(f"{body.name} has no flat faces to attach a support or a load to. "
                    "Use the Boundary Conditions tab.", "warn")
            return
        ax = self._beam_axis(body)
        ends = sorted([o for o in opts if o[2] == ax], key=lambda o: o[3])
        fixed_default = ends[0][0] if ends else opts[0][0]
        load_default = ends[-1][0] if ends else opts[-1][0]
        import numpy as np
        ext = np.asarray(body.bbox[1]) - np.asarray(body.bbox[0])
        cross = [a for a in range(3) if a != ax]
        thin = min(cross, key=lambda a: ext[a])
        dirs = [("-x", "-X"), ("+x", "+X"), ("-y", "-Y"), ("+y", "+Y"), ("-z", "-Z"), ("+z", "+Z")]
        dflt = "-" + "xyz"[thin]
        fields: list = []
        if "support" in missing:
            fields.append(Field("fixed", "Fixed support on", "choice", fixed_default,
                                options=[(o[0], o[1]) for o in opts]))
        if "load" in missing:
            fields.append(Field("load_face", "Load on", "choice", load_default, options=[(o[0], o[1]) for o in opts]))
            mag = it.quantity.to("n") if it is not None and it.quantity is not None and it.quantity.dim == "force" \
                else 10.0
            fields.append(Field("force", "Force", "float", float(mag), us.force, minimum=1e-12))
            fields.append(Field("direction", "Direction", "choice", dflt, options=dirs))
        what = " and ".join(x for x in ("a support" if "support" in missing else "",
                                        "a load" if "load" in missing else "") if x)
        note = (f"{body.name} has no {what.replace('a ', '')} yet. Nothing runs until you choose: pick the faces and "
                f"the force, then Start. The load is a total force spread over the face.")

        def submit(v):
            if "load_face" in v and "fixed" in v and v["load_face"] == v["fixed"]:
                return "The support and the load must be on different faces."
            self._apply_structural(study, body, v)
            self._resolve_missing(study, analysis, ids, targets, it, self._missing(study, analysis, ids), then)
            return ""
        c.ask_form(f"Set up {body.name}", fields, submit, note=note)

    def _apply_structural(self, study, body, v: dict) -> None:
        from opencae.geometry.references import make_ref
        from opencae.project.model import Constraint, Load
        win, s = self.win, self.session
        if s.project.active_study != study.id:
            win.cmd_activate_study(study.id)

        def fn():
            if "fixed" in v:
                s.project.constraints.append(Constraint("fixed", [make_ref(body, "face", int(v["fixed"]))],
                                                        name="Fixed support"))
            if "load_face" in v:
                sign = -1.0 if v["direction"][0] == "-" else 1.0
                d = [0.0, 0.0, 0.0]
                d["xyz".index(v["direction"][1])] = sign
                s.project.loads.append(Load("force", [make_ref(body, "face", int(v["load_face"]))],
                                            {"magnitude": float(v["force"]), "direction": d,
                                             "coordinate_system": "global"}, name="Load"))
        win.mutate("Add supports and loads (console)", fn, regen=False)

    def _ask_thermal(self, study, analysis, ids, targets, it, body, then) -> None:
        from opencae.ui.forms import Field
        c, s = self.c, self.session
        opts = self._face_options(body)
        if len(opts) < 2:
            c.reply(f"{body.name} needs two flat faces for a quick thermal set-up. Use the thermal tools in the "
                    "Boundary Conditions tab.", "warn")
            return
        ax = self._beam_axis(body)
        ends = sorted([o for o in opts if o[2] == ax], key=lambda o: o[3])
        cold = ends[0][0] if ends else opts[0][0]
        hot = ends[-1][0] if ends else opts[-1][0]
        fields = [Field("cold", "Held at a fixed temperature on", "choice", cold, options=[(o[0], o[1]) for o in opts]),
                  Field("temp", "Temperature", "float", 20.0, "°C"),
                  Field("hot", "Heat enters through", "choice", hot, options=[(o[0], o[1]) for o in opts]),
                  Field("power", "Heat power", "float", 5.0, "W", minimum=1e-12)]
        note = (f"{body.name} has no thermal conditions yet. Choose where the temperature is held and where the "
                "heat enters; nothing runs until you press Start.")

        def submit(v):
            if v["cold"] == v["hot"]:
                return "The fixed-temperature face and the heat face must differ."
            self._apply_thermal(study, body, v)
            self._resolve_missing(study, analysis, ids, targets, it, self._missing(study, analysis, ids), then)
            return ""
        c.ask_form(f"Thermal set-up for {body.name}", fields, submit, note=note)

    def _apply_thermal(self, study, body, v: dict) -> None:
        from opencae.geometry.references import make_ref
        from opencae.project.model import Constraint, Load
        win, s = self.win, self.session
        if s.project.active_study != study.id:
            win.cmd_activate_study(study.id)

        def fn():
            s.project.constraints.append(Constraint("temperature", [make_ref(body, "face", int(v["cold"]))],
                                                    {"value": float(v["temp"])}, name="Fixed temperature"))
            s.project.loads.append(Load("heat_power", [make_ref(body, "face", int(v["hot"]))],
                                        {"magnitude": float(v["power"])}, name="Heat power"))
        win.mutate("Add thermal conditions (console)", fn, regen=False)

    # ---- launching through the GUI services
    def _busy_reply(self) -> bool:
        """True (and a reply) when the GUI's mesh/solve process is already busy."""
        win, c = self.win, self.c
        if win.proc is None:
            return False
        reg = self.reg
        cur = None
        for kind in ("solve", "mesh", "convergence"):
            j = self.ext._cur.get(kind) if self.ext is not None else None
            if j is not None and j.active:
                cur = j
                break
        if cur is not None:
            c.reply(f"Already running — see {job_link(cur)}. Wait for it or stop it first.", "warn",
                    chips=[f"stop job #{cur.seq}"], html=True)
        else:
            c.reply("A mesh or solve is already running. Wait for it or stop it first.", "warn", chips=["stop"])
        return True

    def _existing(self, study) -> Optional[object]:
        ch = self._chains.get(study.id)
        if ch is not None and ch.job is not None and ch.job.active or ch is not None and ch.stage == "gap":
            return ch.job
        return self.reg.find_running(None, study.name, self.project_key())

    def _need_ccx(self) -> bool:
        """Structural, thermal and modal studies run in CalculiX: say so before creating anything."""
        be = self.session.backend
        if not be.executable():
            self.c.reply(f"{be.name} is not installed, so this cannot run.", "err", chips=["doctor"])
            return False
        if be.verified is False:
            self.c.reply(f"{be.name} failed its self-test (Tools > Settings > Test Solver), so I will not start it.",
                         "err", chips=["doctor"])
            return False
        return True

    def _need_solver(self, study) -> bool:
        s, c = self.session, self.c
        be = s.solver_backend()
        if be is None or not be.executable():
            what = be.name if be is not None else "The solver"
            c.reply(f"{what} is not installed, so this cannot run.", "err",
                    chips=["setup openfoam"] if s.study.analysis.analysis_type.startswith("cfd") else ["doctor"])
            return False
        if be.verified is False:
            c.reply(f"{be.name} failed its self-test (Tools > Settings > Test Solver), so I will not start it.", "err",
                    chips=["doctor"])
            return False
        return True

    def _launch(self, study, analysis: str, on_done: Optional[Callable] = None) -> None:
        c, win, s = self.c, self.win, self.session
        dup = self._existing(study)
        if dup is not None:
            c.reply(f"Already running — see {job_link(dup)}.", "warn", html=True)
            if self.ext is not None:
                self.ext.show_monitor(dup.id)
            return
        if not self._need_solver(study):
            return
        if self._busy_reply():
            return
        if s.project.active_study != study.id:
            win.cmd_activate_study(study.id)
        ch = _Chain(study.id, study.name, analysis)
        self._chains[study.id] = ch
        self._advance(ch)

    def _mesh_current(self) -> bool:
        s = self.session
        if s.mesh is None or not s.project.mesh_is_current():
            return False
        bodies = s.analysis_bodies()
        return not (len(bodies) == 1 and s.mesh.body_id != bodies[0].id)

    def _mark(self, job) -> None:
        if job is not None and job.source != "console":
            job.source = "console"
            self.reg.set_inputs(job, started_from="console")

    def _advance(self, ch: _Chain) -> None:
        c, win, s = self.c, self.win, self.session
        ext = self.ext
        if s.project.active_study != ch.study_id:
            win.cmd_activate_study(ch.study_id)
        if self._mesh_current():
            return self._start_solve(ch)
        before = ext._cur.get("mesh") if ext is not None else None
        win.cmd_generate_mesh()
        job = ext._cur.get("mesh") if ext is not None else None
        if job is before:
            job = None
        if job is not None:
            self._mark(job)
        ch.job, ch.stage = job, "mesh"
        if win.proc is None:       # refused: the monitor already holds a failed job with the reason
            self._chains.pop(ch.study_id, None)
            reason = job.error if job is not None else "The mesh could not be started."
            c.reply(f"Could not mesh {ch.name}: {reason} " + (f"({job_link(job)})" if job is not None else ""),
                    "err", html=True)
            return
        c.reply(f"Meshing {esc(ch.name)}… {job_link(job) if job is not None else ''}", "info", html=True)
        if job is not None:
            self.watch(job, lambda j, ch=ch: self._after_mesh(ch, j))

    def _after_mesh(self, ch: _Chain, job) -> None:
        c = self.c
        if ch.cancelled:
            return
        if job.state != "done":
            self._chains.pop(ch.study_id, None)
            c.reply(f"Meshing failed: {esc(job.error or job.state)} — {job_link(job)}", "err", html=True,
                    chips=["retry"] if job.state == "failed" else None)
            return
        n = job.outputs.get("elements")
        c.reply(f"Meshed {esc(ch.name)}" + (f" ({n} elements)" if n else "") + ". Solving…", "info")
        ch.stage = "gap"
        self._start_solve(ch)

    def _start_solve(self, ch: _Chain) -> None:
        c, win, s = self.c, self.win, self.session
        ext = self.ext
        issues, _ = s.validate()
        from opencae.boundary_conditions.resolve import ERROR
        errs = [str(i) for i in issues if i.severity == ERROR]
        if errs:
            self._chains.pop(ch.study_id, None)
            c.reply(f"Cannot solve: {esc(errs[0])}", "err", html=True,
                    chips=["show mesh"] if "mesh" in errs[0].lower() else None)
            return
        if self._busy_reply():
            self._chains.pop(ch.study_id, None)
            return
        before = ext._cur.get("solve") if ext is not None else None
        win.cmd_solve()
        job = ext._cur.get("solve") if ext is not None else None
        if job is before:
            job = None
        if job is not None:
            self._mark(job)
        ch.job, ch.stage = job, "solve"
        if win.proc is None:
            self._chains.pop(ch.study_id, None)
            c.reply(f"Could not start the solver: {esc(job.error if job is not None else 'see the Output panel')}",
                    "err", html=True)
            return
        c.reply(f"Solving {esc(ch.name)}… {job_link(job) if job is not None else ''}", "info", html=True)
        if job is not None:
            self.watch(job, lambda j, ch=ch: self._after_solve(ch, j))

    def _after_solve(self, ch: _Chain, job) -> None:
        c, s = self.c, self.session
        self._chains.pop(ch.study_id, None)
        if job.state == "cancelled":
            c.reply(f"{esc(ch.name)} was cancelled — {job_link(job)}", "warn", html=True, chips=["retry"])
            return
        if job.state != "done":
            c.reply(f"{esc(ch.name)} failed: {esc(job.error or 'see the log')} — {job_link(job)}", "err", html=True,
                    chips=["retry", "show status"])
            return
        m = metrics_of(s)
        txt = ", ".join(f"{k.lower()} {v:.4g} {u}" for k, (v, u) in m.items()) or esc(job.outputs.get("summary", "done"))
        chips = self.result_chips(ch.analysis)
        c.reply(f"{esc(ch.name)} done: {esc(txt)} · {job_link(job)}", "ok", html=True, chips=chips)

    @staticmethod
    def result_chips(analysis: str) -> list:
        return {"structural": ["show displacement", "show stress", "open results"],
                "thermal": ["show temperature", "open results"],
                "modal": ["show frequency", "open results"],
                "fluid": ["show velocity", "show pressure"]}.get(analysis, ["open results"])

    # ---- watching registry jobs
    def watch(self, job, cb: Callable) -> None:
        if job.state in ("done", "failed", "cancelled"):
            QTimer.singleShot(0, lambda: cb(job))
            return
        self._watch[job.id] = cb

    def _on_finished(self, job_id: str) -> None:
        cb = self._watch.pop(job_id, None)
        if cb is None:
            return
        job = self.reg.get(job_id)
        if job is not None:
            try:
                cb(job)
            except Exception:  # noqa: BLE001 - one broken follow-up never stops the console
                log.exception("console follow-up failed")

    # ---------------------------------------------------------------- motion
    def _mbd_study(self, it: Intent):
        s = self.session
        studies = [st for st in s.project.studies if st.analysis.analysis_type == "mbd"]
        if not studies:
            return None
        if it.entity is not None and it.entity.kind == "study":
            return s.project.study_by_id(it.entity.id)
        cur = s.project.study_by_id(s.project.active_study)
        return cur if cur in studies else studies[0]

    def _run_motion(self, it: Intent) -> None:
        c, s = self.c, self.session
        st = self._mbd_study(it)
        if st is None:
            c.reply("This project has no motion study. Open an assembly example first.", "warn",
                    chips=[f"open {e.name}" for e in self.context().projects if e.id in ("gripper", "drone_rotors")])
            return
        from opencae.mbd import backend as B
        ok, why = B.available()
        if not ok:
            c.reply(f"The motion solver is not available ({esc(str(why).splitlines()[0] if why else 'MuJoCo')}); "
                    "nothing was run.", "err", html=True, chips=["doctor"])
            return
        reg = self.reg
        dup = reg.find_running("motion", st.name, self.project_key())
        if dup is not None:
            c.reply(f"Already running — see {job_link(dup)}.", "warn", html=True)
            if self.ext is not None:
                self.ext.show_monitor(dup.id)
            return
        job = None
        ws = self.assembly_ws()
        if ws is not None:
            ws.enter()
            ws.set_study(st.id)
            if ws.study_problems():
                c.reply("The motion study is not ready: " + esc(ws.study_problems()[0]), "err", html=True)
                return
            job = ws.run_study()
        else:
            from opencae.ui.jobs import track_mbd_run
            job = track_mbd_run(self.win, s.project, st, data_dir=s.data_dir, source="console")
        if job is None:
            c.reply("The motion study could not be started.", "err")
            return
        self._mark(job)
        c.reply(f"Running {esc(st.name)}… {job_link(job)}", "info", html=True)
        self.watch(job, lambda j, st=st: self._after_motion(st, j))

    def _after_motion(self, st, job) -> None:
        c = self.c
        if job.state == "done":
            c.reply(f"Motion study {esc(st.name)} finished · {job_link(job)}", "ok", html=True,
                    chips=["open results"])
        elif job.state == "cancelled":
            c.reply(f"Motion study cancelled — {job_link(job)}", "warn", html=True, chips=["retry"])
        else:
            c.reply(f"Motion study failed: {esc(job.error)} — {job_link(job)}", "err", html=True, chips=["retry"])

    # ---------------------------------------------------------------- fluid
    def _run_fluid(self, targets: list, it: Intent) -> None:
        c, win, s = self.c, self.win, self.session
        from opencae.cfd.openfoam.install import find_openfoam
        from opencae.utilities.settings import get_setting
        if find_openfoam(get_setting("openfoam_env") or "", get_setting("openfoam_wsl_distro") or "") is None:
            c.reply("Fluid analysis needs the OpenFOAM solver, which is not installed, so nothing was run.", "err",
                    chips=["setup openfoam", "doctor"])
            return
        self.go_studio()
        ids = self._body_ids(targets)
        study = self._find_study(ANALYSIS_TYPE["fluid"], ids)
        if study is None:
            from opencae.ui.forms import Field
            from opencae.materials.library import FLUIDS
            fields = [Field("axis", "Flow direction", "choice", "+x",
                            options=[(a, a.upper()) for a in ("+x", "-x", "+y", "-y", "+z", "-z")]),
                      Field("speed", "Free-stream speed", "float", 10.0, "m/s", minimum=1e-9),
                      Field("fluid", "Fluid", "choice", "Air (20 °C)", options=[(k, k) for k in FLUIDS])]

            def submit(v):
                def fn():
                    st = s.create_flow_domain(ids, v["axis"], v["speed"], 3.0, 6.0, 2.0, "External flow",
                                              symmetry="")
                    st.analysis.fluid_name = v["fluid"]
                    st.analysis.fluid_density, st.analysis.fluid_viscosity = FLUIDS[v["fluid"]]
                try:
                    win.mutate("Create flow domain (console)", fn, regen=False)
                except ValueError as exc:
                    return str(exc)
                self._launch(s.study, "fluid")
                return ""
            c.ask_form("Flow around the model", fields, submit,
                       note="Creates a box of fluid around the parts with an inlet, an outlet and slip walls, then "
                            "meshes and solves it with OpenFOAM.")
            return
        if s.project.active_study != study.id:
            win.cmd_activate_study(study.id)
        self._launch(study, "fluid")

    # ---------------------------------------------------------------- iterate
    def iterate(self, it: Intent) -> None:
        c = self.c
        p = it.param
        if p.data.get("drone"):
            return self._iterate_drone(it)
        if self._study is not None and self._study.active:
            c.reply(f"A parameter study is already running — see {job_link(self._study.job)}.", "warn", html=True)
            return
        dup = self.reg.find_running("parameter_study", p.name, self.project_key())
        if dup is not None:
            c.reply(f"Already running — see {job_link(dup)}.", "warn", html=True)
            return
        ctx = self.context()
        body_id = p.data["members"][0]["feature"]
        ent = next((e for e in ctx.components if e.data.get("geometry") == body_id), None)
        if ent is None:
            c.reply(f"{esc(p.name)} belongs to a feature that is not a solid component, so there is nothing to "
                    "analyse for it.", "err", html=True)
            return
        analysis = it.analysis or "structural"
        if not self._need_ccx():
            return
        self._run_studio(analysis, [ent], None, lambda study: self._start_param_study(it, study, analysis))

    def _start_param_study(self, it: Intent, study, analysis: str) -> None:
        c = self.c
        if not self._need_solver(study):
            return
        if self._busy_reply():
            return
        self._study = ParameterStudy(self, it, study, analysis)
        self._study.start()

    # ---- drone parameter studies (drone.json copies with overrides: the file itself is never changed)
    def _iterate_drone(self, it: Intent) -> None:
        c, win = self.c, self.win
        ws = getattr(win, "_drone", None)
        if ws is None or not getattr(ws, "project_dir", None):
            c.reply("Open a drone project first.", "warn", chips=["open quad250"])
            return
        proj = Path(ws.project_dir)
        p = it.param
        key = p.data["key"]
        values = list(it.values)
        slug = re.sub(r"[^a-z0-9]+", "_", p.name.lower()).strip("_")[:24]
        title = f"Parameter study: {p.name} {values[0]:g} to {values[-1]:g}"
        reg = self.reg
        dup = reg.find_running("parameter_study", p.name, str(proj))
        if dup is not None:
            c.reply(f"Already running — see {job_link(dup)}.", "warn", html=True)
            return
        section = key.split(".")[0]
        only = [section] if section in ("mass", "propulsion", "structure", "cfd", "flight") else None
        holder: dict = {}
        ready = __import__("threading").Event()
        rec = StudyRecord(title, p.name, p.data.get("unit_text") or it.unit or "", float(p.data.get("value", 0)))
        names = [f"{p.name} = {v:g}" for v in values]

        def work(progress, cancelled):
            from opencae.drone import pipeline
            ready.wait(5)
            job = holder["job"]
            reg.set_cases(job, names)
            for i, v in enumerate(values):
                if cancelled():
                    break
                reg.case(job, names[i], "running")
                try:
                    res = pipeline.run_project(proj, only=only, overrides={key: v}, variant=f"{slug}_{i + 1}")
                    flat = pipeline._flat(res)
                    m = {(k.split(".", 1)[1] if "." in k else k): (float(x["value"]), x.get("unit", ""))
                         for k, x in flat.items()}
                    rec.rows.append({"value": v, "state": "done", "metrics": m, "error": "",
                                     "verdict": res.get("verdict")})
                    reg.case(job, names[i], "done", detail=str(res.get("verdict") or ""))
                except Exception as exc:  # noqa: BLE001 - this case failed; the others still run
                    rec.rows.append({"value": v, "state": "failed", "metrics": {}, "error": str(exc)[:200]})
                    reg.case(job, names[i], "failed", error=str(exc)[:300])
            keep = {}
            for r in rec.rows:
                for k, val in r["metrics"].items():
                    keep.setdefault(k, set()).add(round(val[0], 9))
            vary = [k for k, vs in keep.items() if len(vs) > 1][:5]
            for r in rec.rows:
                r["metrics"] = {k: r["metrics"][k] for k in vary if k in r["metrics"]}
            self.last_study = rec
            return {"opener": CONSOLE_OPENER, "cases": len(rec.rows), "restored": "drone.json was not changed"}

        from opencae.ui.jobs import track_callable
        job = track_callable(win, "parameter_study", title, p.name, work, stages=[], source="console",
                             analysis_type="drone", project_path=str(proj), allow_duplicate=False,
                             inputs={"parameter": key, "values": values, "analyses": only or "all"},
                             on_done=lambda res, rec=rec: self._study_done(rec, None),
                             opener=None)
        holder["job"] = job
        ready.set()
        c.reply(f"Running {len(values)} cases of {esc(p.name)}… {job_link(job)}", "info", html=True)

    def _study_done(self, rec: StudyRecord, job) -> None:
        c = self.c
        ok = [r for r in rec.rows if r["state"] == "done"]
        c.reply(f"{esc(rec.title)}: {len(ok)} of {len(rec.rows)} cases done. drone.json was not changed.", "ok",
                html=True, chips=["show table", "show plot", "export csv"])

    # ---------------------------------------------------------------- show
    RESULT_KEYS = {"displacement": ("U_mag",), "stress": ("S_vm",), "principal": ("S_p1",), "strain": ("E_eqv",),
                   "temperature": ("T",), "velocity": ("V_mag", "U_mag"), "pressure": ("p", "P"),
                   "frequency": ("U_mag",)}

    def show(self, it: Intent) -> None:
        c, win, s = self.c, self.win, self.session
        what = it.what
        if what == "job":
            j = self._job_by_number(it.job)
            if j is None:
                c.reply(f"There is no job #{it.job}.", "warn", chips=["status"])
            else:
                if self.ext is not None:
                    self.ext.show_monitor(j.id)
                c.reply(f"Showing {job_link(j)}.", "ok", html=True)
            return
        if what == "jobs":
            if self.ext is not None:
                self.ext.show_monitor()
            c.reply("Progress Monitor shown on the left.", "ok")
            return
        if what == "dashboard":
            return self.open_dashboard(it)
        if what in ("table", "plot", "csv"):
            return self._show_study(what)
        if what == "playback":
            ext = self.assembly_ext()
            if ext is None:
                c.reply("The motion view is not available.", "warn")
                return
            ext.show_mode()
            c.reply("Motion playback shown.", "ok")
            return
        if self.mode() == "drone" and what in ("results",):
            win.enter_drone_mode()
            c.reply("Drone results shown in the dashboard.", "ok", chips=["results"])
            return
        if self.mode() != "studio":
            self.go_studio()
        if what == "mesh":
            if s.mesh is None:
                c.reply("There is no mesh yet.", "warn", chips=self._run_chips())
                return
            win.set_view_mode("mesh")
            c.reply("Showing the mesh.", "ok")
            return
        if what == "geometry":
            win.set_view_mode("geometry")
            c.reply("Showing the geometry.", "ok")
            return
        rs = s.results
        if rs is None:
            c.reply("There are no results for the active study yet.", "warn", chips=self._run_chips())
            return
        if what == "results":
            win.set_view_mode("results")
            c.reply("Showing the results.", "ok", chips=["show displacement", "show stress"])
            return
        avail = rs.available()
        key = next((k for k in self.RESULT_KEYS.get(what, ()) if k in avail), None)
        if key is None:
            needle = what.lower()
            key = next((k for k in avail if needle in rs.info(k).label.lower()), None)
        if key is None:
            have = sorted({rs.info(k).label for k in avail})[:5]
            c.reply(f"This study has no {what} result. It has: {', '.join(have)}.", "warn",
                    chips=[f"show {x}" for x in ("displacement", "stress", "temperature") if
                           any(k in avail for k in self.RESULT_KEYS[x])])
            return
        win.cmd_result(key)
        c.reply(f"Showing {rs.info(key).label.lower()}.", "ok")

    def _run_chips(self) -> list:
        ctx = self.context()
        first = (ctx.selection or [e.name for e in ctx.components] or [""])[0]
        return [f"run structural on {first}"] if first else ["help"]

    def open_results(self) -> None:
        """'open results': the newest finished job of this project that has results, in its own viewer."""
        c, reg = self.c, self.reg
        for j in reg.jobs(self.project_key() or None):
            if j.state == "done" and reg.can_open(j):
                reg.open_results(j.id)
                c.reply(f"Opened the results of {job_link(j)}.", "ok", html=True)
                return
        c.reply("There are no finished results to open yet.", "warn", chips=self._run_chips())

    def _open_study_job(self, job) -> None:
        self._show_study("table")

    def _show_study(self, what: str) -> None:
        c = self.c
        rec = self.last_study
        if rec is None or not rec.rows:
            c.reply("There is no parameter study yet.", "warn",
                    chips=["iterate finger thickness from 2 mm to 5 mm in 5 steps"]
                    if any(p.name == "finger thickness" for p in self.context().parameters) else ["help"])
            return
        if what == "table":
            rows = rec.table_rows()
            h = ['<table cellspacing="0" cellpadding="3" style="margin:2px 0;">']
            for i, row in enumerate(rows):
                tag = "th" if i == 0 else "td"
                h.append("<tr>" + "".join(f'<{tag} align="right" style="border-bottom:1px solid #3a414d;">{esc(x)}'
                                          f"</{tag}>" for x in row) + "</tr>")
            h.append("</table>")
            c.say_html("".join(h))
            c.reply(f"{esc(rec.title)}", "info", html=True, chips=["show plot", "export csv"])
        elif what == "plot":
            self._plot(rec)
        else:
            base = self.session.data_dir / "studies" if self.session.path else projects_home() / "studies"
            path = base / (re.sub(r"[^a-z0-9]+", "_", rec.param.lower()) + time.strftime("_%Y%m%d_%H%M%S") + ".csv")
            rec.to_csv(path)
            c.reply(f'Saved <a href="folder:{esc(path.parent)}">{esc(path)}</a>', "ok", html=True)

    def _plot(self, rec: StudyRecord) -> None:
        c = self.c
        names = rec.metric_names
        if not names:
            c.reply("The study has no finished case to plot.", "warn")
            return
        from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
        from matplotlib.figure import Figure
        from PySide6.QtWidgets import QDialog, QVBoxLayout
        fig = Figure(figsize=(6.4, 2.6 * min(len(names), 2) + 0.6), dpi=100)
        done = [r for r in rec.rows if r["state"] == "done"]
        shown = names[:2]
        for i, n in enumerate(shown):
            ax = fig.add_subplot(len(shown), 1, i + 1)
            xs = [r["value"] for r in done if n in r["metrics"]]
            ys = [r["metrics"][n][0] for r in done if n in r["metrics"]]
            ax.plot(xs, ys, "o-", color="#4c9be8")
            ax.axvline(rec.original, color="#8b95a5", ls=":", lw=1)
            unit = next((r["metrics"][n][1] for r in done if n in r["metrics"]), "")
            ax.set_ylabel(f"{n} [{unit}]")
            ax.grid(True, alpha=0.3)
            if i == len(shown) - 1:
                ax.set_xlabel(f"{rec.param} [{rec.unit}]")
        fig.suptitle(rec.title, fontsize=10)
        fig.tight_layout()
        try:
            out = (self.session.data_dir / "studies") if self.session.path else projects_home() / "studies"
            out.mkdir(parents=True, exist_ok=True)
            png = out / (re.sub(r"[^a-z0-9]+", "_", rec.param.lower()) + ".png")
            fig.savefig(str(png), facecolor="white")
        except OSError:
            png = None
        dlg = QDialog(self.win)
        dlg.setWindowTitle(rec.title)
        lay = QVBoxLayout(dlg)
        lay.addWidget(FigureCanvasQTAgg(fig))
        dlg.resize(680, 300 * min(len(names), 2) + 40)
        dlg.setAttribute(Qt.WA_DeleteOnClose, True)
        self.win._console_plots = [d for d in getattr(self.win, "_console_plots", []) if d.isVisible()] + [dlg]
        dlg.show()
        c.reply("Plot opened" + (f" and saved to {esc(png)}" if png else "") + ".", "ok", html=True,
                chips=["show table", "export csv"])

    # ---------------------------------------------------------------- dashboard
    def open_dashboard(self, it: Optional[Intent] = None) -> None:
        c, win, s = self.c, self.win, self.session
        browser = bool(it is not None and it.browser)
        ws = getattr(win, "_drone", None)
        if ws is not None and getattr(ws, "project_dir", None):
            win.enter_drone_mode()
            if browser:
                ws.open_in_browser()
            c.reply("Dashboard open" + (" in your browser." if browser else " in the right-hand view."), "ok",
                    chips=[] if browser else ["open dashboard in browser"])
            return
        ext = self.assembly_ext()
        if ext is not None and any(st.analysis.analysis_type == "mbd" for st in s.project.studies):
            ext.show_mode()
            c.reply("Motion playback and plots are open (the dashboard of this project).", "ok",
                    chips=["run motion"] if self.assembly_ws() is not None and self.assembly_ws().result is None
                    else [])
            return
        if s.results is not None:
            self.go_studio()
            win.set_view_mode("results")
            c.reply("Showing the results (the dashboard of this project).", "ok",
                    chips=["show displacement", "show stress"])
            return
        c.reply("There is no dashboard to show yet: open a drone project or run an analysis first.", "warn",
                chips=["open quad250", "open gripper"])

    # ---------------------------------------------------------------- stop / retry / status
    def _job_by_number(self, n: Optional[int]):
        if n is None:
            return None
        return next((j for j in self.reg.jobs() if j.seq == n), None)

    def stop(self, it: Intent) -> None:
        c, reg = self.c, self.reg
        if it.job is not None:
            j = self._job_by_number(it.job)
            if j is None:
                c.reply(f"There is no job #{it.job}.", "warn", chips=["status"])
                return
            jobs = [j]
        else:
            jobs = reg.active(self.project_key() or None)
            if it.what != "all" and len(jobs) > 1:
                c.ask("Stop which job?", [(f"#{j.seq} {j.title}", f"stop job #{j.seq}") for j in jobs[:6]] +
                      [("All of them", "stop all")], lambda line: c.execute_command(line))
                return
        stopped = []
        for j in jobs:
            if j.active and reg.cancel(j.id):
                stopped.append(j)
        for ch in list(self._chains.values()):       # a run between mesh and solve has no job to cancel
            if ch.stage in ("gap", "start"):
                ch.cancelled = True
        if self._study is not None and self._study.active:
            pass
        ws = getattr(self.win, "_drone", None)
        srv = getattr(ws, "server", None) if ws is not None else None
        if not stopped and srv is not None and getattr(srv.jobs, "running", False):
            srv.stop_job()
            c.reply("Stopping the drone run after its current step.", "warn")
            return
        if stopped:
            c.reply("Stopped " + ", ".join(job_link(j) for j in stopped) +
                    (" — the parameter study ends after the current case." if any(
                        j.kind == "parameter_study" for j in stopped) else "."), "warn", html=True, chips=["retry"])
        else:
            c.reply("Nothing is running.", "dim")

    def retry(self, it: Intent) -> None:
        c, reg = self.c, self.reg
        if it.job is not None:
            j = self._job_by_number(it.job)
        else:
            j = next((x for x in reg.jobs(self.project_key() or None) if x.state in ("failed", "cancelled")
                      and "retry" in x._cb), None)
        if j is None:
            c.reply("There is no failed or cancelled job to retry.", "warn", chips=["status"])
            return
        new = reg.retry(j.id)
        if new is None:
            c.reply(f"{job_link(j)} cannot be retried from here.", "warn", html=True)
            return
        self._mark(new)
        c.reply(f"Retrying as {job_link(new)}.", "ok", html=True)

    def status(self, it: Intent) -> None:
        c, reg = self.c, self.reg
        key = self.project_key() or None
        counts = reg.counts(key)
        act = reg.active(key)
        parts = []
        if act:
            parts.append(f"{len(act)} running")
        for k in ("done", "failed", "cancelled"):
            if counts.get(k):
                parts.append(f"{counts[k]} {k}")
        if not parts:
            c.reply("No jobs yet in this project.", "dim", chips=self._run_chips())
        else:
            c.reply(" · ".join(parts), "info")
        for j in act[:4]:
            c.reply(f"  {job_link(j)} {esc(j.title)} ({j.state})", "info", html=True)
        failed = [j for j in reg.jobs(key) if j.state == "failed"][:2]
        for j in failed:
            c.reply(f"  {job_link(j)} failed: {esc(j.error)}", "err", html=True)
        if failed:
            c.set_chips(["retry"])

    def history(self, it: Intent) -> None:
        c = self.c
        lines = [h for h in c.history[-12:]]
        if not lines:
            c.reply("No earlier commands.", "dim")
            return
        for h in lines:
            c.say_html(f'<span style="color:#7f849c;">  {cmd_link(h)}</span>')

    # ---------------------------------------------------------------- set
    def set_value(self, it: Intent) -> None:
        c, win, s = self.c, self.win, self.session
        if it.what == "mesh":
            bodies = s.analysis_bodies()
            if not bodies:
                c.reply("There is no component in the active study to mesh.", "warn")
                return
            div = {"coarse": 10, "medium": 20, "fine": 40}.get(it.level)
            st = s.study
            before = st.mesh_settings.size_max
            size = 0.0 if it.level == "auto" else round(max(b.size for b in bodies) / div, 4)

            def fn():
                st.mesh_settings.size_max = size
            win.mutate("Mesh density (console)", fn, regen=False)
            c.reply(f"Mesh density of {esc(st.name)}: {it.level}" + (f" (element size {size:g} {s.units.length})"
                                                                     if size else " (automatic)") + ".", "ok",
                    html=True)
            return
        p = it.param
        if p is None:
            c.reply(it.message or "Set which parameter?", "warn", chips=it.suggestions)
            return
        if p.data.get("drone"):
            return c.execute_command(f"set {p.data['key']} {it.quantity.value:g}")
        q = it.quantity
        snap_before = p.data.get("value")
        win.mutate("Set parameter (console)", lambda: set_parameter(s, p, q.value), regen=True)
        c.reply(f"{esc(p.name)} = {q.value:g} {q.unit} (was {snap_before:g}). The mesh is out of date.", "ok",
                html=True)

    # ---------------------------------------------------------------- lifetime
    def close(self) -> None:
        if self._off is not None:
            try:
                self._off()
            except Exception:  # noqa: BLE001
                pass
            self._off = None


# ------------------------------------------------------------------------------------------------ parameter study
class ParameterStudy(QObject):
    """Runs the cases of a parameter study one after the other through the Studio mesh and solve handlers.

    The state machine polls the main window (its process ends), so it never blocks the GUI. Each case: set the
    value, regenerate, renew the face references, mesh, solve, record the key results. The original value is
    restored at the end, also when the study is cancelled (after the current case) or a case fails.
    """

    def __init__(self, actions: Actions, it: Intent, study, analysis: str):
        super().__init__(actions.win)
        self.a, self.it, self.study, self.analysis = actions, it, study, analysis
        self.win, self.s, self.reg = actions.win, actions.session, actions.reg
        self.param: Entity = it.param
        self.values = list(it.values)
        self.i = -1
        self.state = "idle"
        self.active = False
        self.cancel_flag = False
        self.job = None
        self.names = [f"{self.param.name} = {v:g} {it.unit}".strip() for v in self.values]
        self.snapshot = snapshot_parameter(self.s, self.param)
        self.original = float(self.param.data.get("value", 0.0))
        self.rec = StudyRecord(f"{self.param.name} {self.values[0]:g} to {self.values[-1]:g} {it.unit}".strip(),
                               self.param.name, it.unit, self.original)
        self.timer = QTimer(self)
        self.timer.setInterval(120)
        self.timer.timeout.connect(self._poll)
        self._dirty0 = self.win.dirty
        self._mesh_study, self._size_max0 = None, None
        self._t_case = 0.0
        self._error = ""

    # ---- lifecycle
    def start(self) -> None:
        a, win, s = self.a, self.win, self.s
        key = a.project_key()
        # a coarse mesh keeps the cases quick; the user can refine with "set mesh fine"
        bodies = s.analysis_bodies()
        self._mesh_study, self._size_max0 = s.study, s.study.mesh_settings.size_max   # given back in _finish
        if bodies and not s.study.mesh_settings.size_max:
            s.study.mesh_settings.size_max = round(max(b.size for b in bodies) / 10.0, 4)
            a.c.say("Using a coarse mesh for the cases (set mesh medium|fine to change it).", "dim")
        self.job = self.reg.create_job(
            key, "parameter_study", f"Parameter study: {self.rec.title}", self.param.name, self.study.analysis.analysis_type,
            "console", stages=[], inputs={"parameter": self.param.name, "values": self.values, "unit": self.it.unit,
                                          "study": self.study.name, "original": f"{self.original:g} {self.it.unit}"},
            cancel=self._cancel, retry=lambda: self._retry())
        self.reg.set_cases(self.job, self.names)
        self.reg.log(self.job, f"{len(self.values)} cases of {self.param.name}; response study: {self.study.name}")
        self.active = True
        win._jobs_filter = self._filter
        self.a.c.reply(f"Running {len(self.values)} cases of {esc(self.param.name)} "
                       f"({self.values[0]:g} to {self.values[-1]:g} {esc(self.it.unit)})… {job_link(self.job)}",
                       "info", html=True)
        try:
            self._next()
        except Exception as exc:  # noqa: BLE001
            log.exception("parameter study could not start a case")
            self._abort(exc)
            return
        self.timer.start()

    def _retry(self):
        self.a.iterate(self.it)
        return self.a._study.job if self.a._study is not None else None

    def _cancel(self) -> None:
        self.cancel_flag = True
        self.reg.log(self.job, "Cancel requested: the study stops after the current case.", "warning")

    def _filter(self, event: str, kw: dict) -> bool:
        """Swallow the per-case mesh and solve reports (the study job shows them as cases)."""
        if not self.active:
            return False
        if event == "line" and self.job is not None and self.i >= 0:
            line = kw.get("line", "")
            if line.startswith("PROGRESS:"):
                self.reg.case(self.job, self.names[self.i], "running", detail=line.replace("PROGRESS: ", "")[:70])
            elif "*WARNING" in line or "*ERROR" in line:
                self.reg.log(self.job, f"{self.names[self.i]}: {line.strip()}",
                             "warning" if "WARNING" in line else "error")
        if event == "finished" and kw.get("cancelled"):
            self.cancel_flag = True
        return True

    # ---- cases
    def _next(self) -> None:
        self.i += 1
        if self.i >= len(self.values) or self.cancel_flag:
            return self._finish()
        v = self.values[self.i]
        self._error = ""
        self.reg.case(self.job, self.names[self.i], "running", detail="changing the geometry")
        try:
            set_parameter(self.s, self.param, v)
            if self.s.regen.errors:
                raise ValueError("; ".join(self.s.regen.errors.values()))
            rebind_references(self.s, self.study)
            self.win.refresh_all()
        except Exception as exc:  # noqa: BLE001
            return self._case_failed(f"geometry: {exc}")
        self.reg.case(self.job, self.names[self.i], "running", detail="meshing")
        self.win.cmd_generate_mesh()
        if self.win.proc is None:
            return self._case_failed("the mesh could not be started")
        self.state = "mesh"

    def _poll(self) -> None:
        try:
            self._poll_step()
        except Exception as exc:  # noqa: BLE001 - a failing step must still restore the user's settings
            log.exception("parameter study step failed")
            self._abort(exc)

    def _abort(self, exc: Exception) -> None:
        if not self.active:
            return
        if 0 <= self.i < len(self.values):
            self.rec.rows.append({"value": self.values[self.i], "state": "failed", "metrics": {},
                                  "error": f"{type(exc).__name__}: {exc}"})
            self.reg.case(self.job, self.names[self.i], "failed", error=str(exc))
        self.reg.log(self.job, f"The study stopped: {type(exc).__name__}: {exc}", "error")
        self.cancel_flag = True
        self._finish()

    def _poll_step(self) -> None:
        if not self.active or self.win.proc is not None:
            return
        s, name = self.s, self.names[self.i] if 0 <= self.i < len(self.names) else ""
        if self.state == "mesh":
            if s.mesh is None or not s.project.mesh_is_current():
                return self._case_failed("meshing failed (see the Output panel)")
            issues, _ = s.validate()
            from opencae.boundary_conditions.resolve import ERROR
            errs = [str(x) for x in issues if x.severity == ERROR]
            if errs:
                return self._case_failed(errs[0])
            self.reg.case(self.job, name, "running", detail="solving")
            s.last_solve_error = ""
            self.win.cmd_solve()
            if self.win.proc is None:
                return self._case_failed("the solver could not be started")
            self.state = "solve"
        elif self.state == "solve":
            m = metrics_of(s)
            if s.last_solve_error or s.results is None or not m:
                return self._case_failed(s.last_solve_error or "no results were produced")
            self.rec.rows.append({"value": self.values[self.i], "state": "done", "metrics": m, "error": ""})
            detail = ", ".join(f"{k.lower()} {v:.4g} {u}" for k, (v, u) in m.items())
            self.reg.case(self.job, name, "done", detail=detail)
            self.reg.log(self.job, f"{name}: {detail}")
            self.state = "idle"
            self._next()

    def _case_failed(self, why: str) -> None:
        name = self.names[self.i]
        self.rec.rows.append({"value": self.values[self.i], "state": "failed", "metrics": {}, "error": why})
        self.reg.case(self.job, name, "failed", error=why)
        self.reg.log(self.job, f"{name}: {why}", "error")
        self.state = "idle"
        self._next()

    def _finish(self) -> None:
        self.timer.stop()
        self.active = False
        win, s = self.win, self.s
        win._jobs_filter = None
        if self._mesh_study is not None:          # the coarse case mesh was the study's choice, not the user's
            self._mesh_study.mesh_settings.size_max = self._size_max0
        restore_parameter(s, self.snapshot)
        rebind_references(s, self.study)
        try:
            win.refresh_all()
        except Exception:  # noqa: BLE001
            log.exception("refresh after the study failed")
        win.dirty = self._dirty0 or win.dirty
        rec = self.rec
        self.a.last_study = rec
        rec.job_id = self.job.id
        done = [r for r in rec.rows if r["state"] == "done"]
        restored = f"Restored {self.param.name} to {self.original:g} {self.it.unit}.".strip()
        self.reg.log(self.job, restored)
        if self.job.state in ("cancelled",):
            self.reg.finish(self.job, False, error="Cancelled.", cancelled=True,
                            outputs={"opener": CONSOLE_OPENER, "restored": restored})
        elif not done:
            self.reg.finish(self.job, False, error="Every case failed: " + (rec.rows[0]["error"] if rec.rows else ""),
                            outputs={"restored": restored})
        else:
            self.reg.finish(self.job, True, outputs={"opener": CONSOLE_OPENER, "cases": len(rec.rows),
                                                      "done": len(done), "restored": restored})
        csv_path = None
        try:
            base = (s.data_dir / "studies") if s.path else projects_home() / "studies"
            csv_path = rec.to_csv(base / (re.sub(r"[^a-z0-9]+", "_", self.param.name.lower()) + ".csv"))
            self.reg.set_inputs(self.job, csv=str(csv_path))
        except OSError:
            pass
        c = self.a.c
        failed = len(rec.rows) - len(done)
        if self.job.state == "cancelled":
            c.reply(f"Study cancelled after {len(rec.rows)} of {len(self.values)} cases. {esc(restored)} "
                    f"{job_link(self.job)}", "warn", html=True,
                    chips=["show table"] if done else None)
        elif not done:
            c.reply(f"No case finished: {esc(rec.rows[0]['error'] if rec.rows else '')}. {esc(restored)} "
                    f"{job_link(self.job)}", "err", html=True, chips=["retry"])
        else:
            c.reply(f"Study done: {len(done)} of {len(self.values)} cases" +
                    (f" ({failed} failed)" if failed else "") + f". {esc(restored)} {job_link(self.job)}", "ok",
                    html=True, chips=["show table", "show plot", "export csv"])
        self.a._study = None
