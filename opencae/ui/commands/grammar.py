"""The command grammar of the console and the Ctrl+K palette: deterministic, Qt-free, no network, no language model.

``parse(text, ctx)`` turns one line into an :class:`Intent` (verb, analysis type, resolved component, parameter
range, ...). Names are resolved fuzzily (exact, then whole words, then ``difflib``) against the entities of the
CURRENT project that the caller passes in a :class:`Context`; several equally good matches are reported as
``candidates`` so the console can ask instead of guessing. Quantities carry units (mm, m, deg, rpm, N, N·m, s, ...).

Nothing here runs anything: ``opencae.ui.commands.actions`` executes an intent through the existing services.
"""
from __future__ import annotations

import difflib
import math
import re
from dataclasses import dataclass, field
from typing import Optional

from opencae.utilities.units import UNIT_TABLE

# ------------------------------------------------------------------------------------------------ vocabulary
# The CLI-style commands of the original console (they keep working); the natural verbs are added to them.
LEGACY_COMMANDS = ("help", "open", "new", "example", "run", "stop", "status", "results", "checks", "set", "get",
                   "params", "iterate", "compare", "variants", "history", "clean", "setup", "doctor", "mode",
                   "dashboard", "clear", "python")
LEGACY_ANALYSES = ("mass", "propulsion", "structure", "cfd", "flight")

# canonical verb -> (usage, one line, example)
VERBS: dict = {
    "open": ("open <example | project | dashboard>", "Open an example or a project, or the dashboard.", "open gripper"),
    "select": ("select <component>", "Select a component (same as clicking it).", "select left finger"),
    "run": ("run <structural|thermal|modal|fluid|motion> [on <component>]",
            "Run an analysis through the same services as the Run button.", "run structural on left finger"),
    "iterate": ("iterate <parameter> from <a> to <b> in <n> steps", "Run the analysis for a range of a parameter.",
                "iterate finger thickness from 2 mm to 5 mm in 10 steps"),
    "show": ("show <displacement|stress|temperature|mesh|table|plot|...>", "Show a result or a view.",
             "show displacement"),
    "stop": ("stop [job #N]", "Stop a running job.", "stop"),
    "retry": ("retry [job #N]", "Run a failed or cancelled job again.", "retry"),
    "status": ("status", "What is running, what failed, what finished.", "status"),
    "set": ("set <parameter> to <value> | set mesh coarse|medium|fine", "Change a parameter or the mesh density.",
            "set finger thickness to 3 mm"),
    "history": ("history", "Earlier commands (or earlier drone runs).", "history"),
    "clear": ("clear", "Clear this window.", "clear"),
    "help": ("help [command]", "List the commands.", "help"),
}
VERB_WORDS = {
    "open": "open", "load": "open", "launch": "open",
    "select": "select", "pick": "select", "choose": "select", "focus": "select",
    "run": "run", "start": "run", "simulate": "run", "analyze": "run", "analyse": "run", "solve": "run",
    "iterate": "iterate", "sweep": "iterate", "vary": "iterate",
    "show": "show", "display": "show", "view": "show", "plot": "show", "export": "show",
    "stop": "stop", "cancel": "stop", "abort": "stop", "halt": "stop", "kill": "stop",
    "retry": "retry", "rerun": "retry", "again": "retry",
    "status": "status", "jobs": "status", "progress": "status",
    "set": "set", "change": "set", "update": "set",
    "history": "history", "clear": "clear", "cls": "clear", "help": "help", "?": "help",
}
ANALYSIS_WORDS = {
    "structural": ("structural", "structure", "stress", "static", "statics", "strength", "stiffness", "fea"),
    "thermal": ("thermal", "heat", "temperature", "conduction"),
    "modal": ("modal", "modes", "mode", "vibration", "vibrations", "frequency", "frequencies", "natural"),
    "fluid": ("fluid", "cfd", "flow", "aero", "aerodynamic", "aerodynamics", "airflow", "air"),
    "motion": ("motion", "mbd", "kinematic", "kinematics", "dynamics", "multibody", "mechanism"),
}
ANALYSIS_LABEL = {"structural": "Static structural", "thermal": "Thermal (steady)", "modal": "Modal",
                  "fluid": "Fluid flow", "motion": "Motion"}
ANALYSIS_TYPE = {"structural": "linear_static", "thermal": "thermal_steady", "modal": "modal",
                 "fluid": "cfd_incompressible", "motion": "mbd"}
PREPOSITIONS = ("on", "for", "around", "of", "in", "at", "to", "over", "about")
ASSEMBLY_WORDS = {"assembly", "the assembly", "whole model", "everything", "all", "model", "whole assembly",
                  "all components", "entire assembly"}
STOP_WORDS = {"the", "a", "an", "my", "of", "please", "component", "body", "part", "piece", "this", "that"}

# what can be shown: canonical word -> synonyms
SHOW_WORDS = {
    "displacement": ("displacement", "displacements", "deflection", "deformation", "deformed", "movement", "u"),
    "stress": ("stress", "stresses", "von mises", "mises", "vonmises"),
    "principal": ("principal", "principal stress"),
    "strain": ("strain", "strains"),
    "temperature": ("temperature", "temperatures", "heat map", "thermal"),
    "frequency": ("frequency", "frequencies", "mode shape", "modes"),
    "velocity": ("velocity", "speed", "flow"),
    "pressure": ("pressure",),
    "mesh": ("mesh", "elements", "mesh quality"),
    "geometry": ("geometry", "model", "cad", "shape"),
    "results": ("results", "result", "solution", "output"),
    "table": ("table", "cases", "case table"),
    "plot": ("plot", "chart", "graph", "curve"),
    "csv": ("csv", "spreadsheet"),
    "jobs": ("jobs", "monitor", "progress", "queue"),
    "playback": ("playback", "animation", "motion", "movie"),
    "dashboard": ("dashboard", "site", "report page"),
}
MESH_LEVELS = {"coarse": "coarse", "rough": "coarse", "quick": "coarse", "medium": "medium", "normal": "medium",
               "fine": "fine", "auto": "auto", "automatic": "auto", "default": "auto"}

# ------------------------------------------------------------------------------------------------ quantities
UNITS = UNIT_TABLE  # one table, shared with the Motion dialogs and plots (opencae.utilities.units)
UNIT_ALIASES = {
    "millimeter": "mm", "millimeters": "mm", "millimetre": "mm", "millimetres": "mm", "centimeter": "cm",
    "centimeters": "cm", "meter": "m", "meters": "m", "metre": "m", "metres": "m", "inches": "in", "µm": "um",
    "degree": "deg", "degrees": "deg", "°": "deg", "radian": "rad", "radians": "rad", "newton": "n",
    "newtons": "n", "n·m": "nm", "n*m": "nm", "n.m": "nm", "n-m": "nm", "n·mm": "nmm", "n*mm": "nmm",
    "second": "s", "seconds": "s", "sec": "s", "secs": "s", "millisecond": "ms", "milliseconds": "ms",
    "minute": "min", "minutes": "min", "hertz": "hz", "watt": "w", "watts": "w", "°c": "c", "celsius": "c",
    "kelvin": "k", "kilogram": "kg", "kilograms": "kg", "gram": "g", "grams": "g", "pascal": "pa",
    "pascals": "pa", "newton-meter": "nm", "newton-meters": "nm", "volts": "v", "volt": "v",
}
_NUM = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"
_NUM_RE = re.compile(r"^" + _NUM + r"$")
_ATTACHED = re.compile(r"^(" + _NUM + r")([A-Za-zµ°%][A-Za-z0-9µ°·*./%-]*)$")


@dataclass
class Quantity:
    value: float
    unit: str = ""              # normalised unit text ("mm"); "" when the user gave a bare number
    dim: str = ""               # length | angle | ... ; "" for a bare number

    @property
    def si(self) -> float:
        return self.value * UNITS[self.unit][1] if self.unit in UNITS and self.unit not in ("c", "k") else self.value

    def to(self, unit: str) -> float:
        """The value in ``unit`` (same dimension), e.g. ``Quantity(0.004, 'm').to('mm') == 4``."""
        if not self.unit or unit == self.unit:
            return self.value
        u0, u1 = UNITS.get(self.unit), UNITS.get(unit)
        if u0 is None or u1 is None or u0[0] != u1[0]:
            raise ValueError(f"cannot convert {self.unit} to {unit}")
        return self.value * u0[1] / u1[1]

    def __str__(self) -> str:
        return f"{self.value:g} {self.unit}".strip()


def norm_unit(u: str) -> str:
    u = u.strip().lower().replace(" ", "")
    return UNIT_ALIASES.get(u, u)


def is_unit(word: str) -> bool:
    return norm_unit(word) in UNITS


def parse_quantity(text: str) -> Optional[Quantity]:
    """``"2 mm"``, ``"2mm"``, ``"6000 rpm"``, ``"5 N·m"``, ``"30deg"``, ``"0.5"`` -> Quantity (None if not one)."""
    t = text.strip()
    m = _ATTACHED.match(t)
    if m and is_unit(m.group(2)):
        u = norm_unit(m.group(2))
        return Quantity(float(m.group(1)), u, UNITS[u][0])
    parts = t.split()
    if parts and _NUM_RE.match(parts[0]):
        v = float(parts[0])
        if len(parts) == 1:
            return Quantity(v)
        u = norm_unit("".join(parts[1:]))
        if u in UNITS:
            return Quantity(v, u, UNITS[u][0])
    return None


def _take_quantity(words: list, i: int):
    """A quantity starting at ``words[i]``: returns (Quantity, next index) or (None, i)."""
    if i >= len(words):
        return None, i
    w = words[i]
    m = _ATTACHED.match(w)
    if m and is_unit(m.group(2)):
        u = norm_unit(m.group(2))
        return Quantity(float(m.group(1)), u, UNITS[u][0]), i + 1
    if _NUM_RE.match(w):
        v = float(w)
        if i + 1 < len(words):
            nxt = words[i + 1]
            # "5 in 10 steps": "in" is the word, not inches, when a number follows it
            follows_number = i + 2 < len(words) and bool(_NUM_RE.match(words[i + 2]))
            u = norm_unit(nxt)
            if u in UNITS and not (u == "in" and follows_number) and not (u == "s" and False):
                return Quantity(v, u, UNITS[u][0]), i + 2
        return Quantity(v), i + 1
    return None, i


# ------------------------------------------------------------------------------------------------ names
@dataclass
class Entity:
    """Something a command can name: a component/body, a study, a parameter, an example project."""
    name: str
    kind: str                   # component | study | parameter | example | job
    id: str = ""
    data: dict = field(default_factory=dict, compare=False)

    def __str__(self) -> str:
        return self.name


@dataclass
class Context:
    """What the current project offers (built by ``actions.build_context``; tests build it by hand)."""
    components: list = field(default_factory=list)      # Entity(kind="component")
    studies: list = field(default_factory=list)
    parameters: list = field(default_factory=list)      # Entity(kind="parameter", data={"unit": "mm", ...})
    projects: list = field(default_factory=list)        # Entity(kind="example")
    selection: list = field(default_factory=list)       # names of the selected components
    mode: str = "studio"                                # studio | drone | assembly
    drone: bool = False                                 # a drone project is open
    drone_keys: list = field(default_factory=list)
    jobs: list = field(default_factory=list)            # (number, title, state)
    project_name: str = ""                              # "run motion on gripper": the project is the assembly


@dataclass
class Resolution:
    entity: Optional[Entity] = None
    candidates: list = field(default_factory=list)      # equally good matches (more than one: ambiguous)
    score: float = 0.0
    exact: bool = False

    @property
    def ambiguous(self) -> bool:
        return self.entity is None and len(self.candidates) > 1


def _singular(t: str) -> str:
    if len(t) > 3 and t.endswith("ies"):
        return t[:-3] + "y"
    if len(t) > 3 and t.endswith("s") and not t.endswith("ss"):
        return t[:-1]
    return t


def tokens(s: str) -> list:
    t = re.sub(r"[^a-z0-9.]+", " ", s.lower().replace("_", " ")).split()
    return [_singular(w) for w in t if w not in STOP_WORDS]


def norm(s: str) -> str:
    return " ".join(tokens(s))


def _score(q: str, qt: list, name: str) -> float:
    n = norm(name)
    if not n or not q:
        return 0.0
    if q == n:
        return 1.0
    nt = n.split()
    if set(qt) <= set(nt):
        return 0.93 - 0.01 * min(len(nt) - len(qt), 5)
    if all(any(t.startswith(x) for t in nt) for x in qt):
        return 0.86 - 0.01 * min(len(nt) - len(qt), 5)
    whole = difflib.SequenceMatcher(None, q, n).ratio()
    per = [max(difflib.SequenceMatcher(None, x, t).ratio() for t in nt) for x in qt]
    return max(whole, 0.95 * sum(per) / len(per) if per else 0.0)


def rank(query: str, entities: list, cutoff: float = 0.55) -> list:
    """[(score, entity)] best first."""
    q = norm(query)
    qt = q.split()
    out = [(_score(q, qt, e.name), e) for e in entities]
    return sorted([x for x in out if x[0] >= cutoff], key=lambda x: (-x[0], x[1].name))


def resolve(query: str, entities: list, cutoff: float = 0.62) -> Resolution:
    """Best entity for ``query``; ``candidates`` holds the tied best matches when it cannot choose."""
    ranked = rank(query, entities, cutoff)
    if not ranked:
        return Resolution()
    top = ranked[0][0]
    if top >= 0.999:
        exact = [e for s, e in ranked if s >= 0.999]
        return Resolution(exact[0], exact[:1], top, True) if len(exact) == 1 else Resolution(None, exact, top, True)
    close = [e for s, e in ranked if s >= top - 0.03]
    if len(close) > 1:
        return Resolution(None, close, top)
    return Resolution(close[0], close, top)


def nearest(query: str, entities: list, n: int = 3) -> list:
    return [e for _, e in rank(query, entities, 0.4)[:n]]


# ------------------------------------------------------------------------------------------------ intents
@dataclass
class Intent:
    verb: str                                  # see VERBS + legacy | empty | python | unknown
    raw: str = ""
    analysis: str = ""                         # structural | thermal | modal | fluid | motion
    target: str = ""                           # the words after "on" / "select" / "open" ...
    entity: Optional[Entity] = None
    candidates: list = field(default_factory=list)
    assembly: bool = False                     # the target is the whole assembly
    what: str = ""                             # show: a key of SHOW_WORDS; set: "mesh"
    param: Optional[Entity] = None
    start: Optional[Quantity] = None
    stop: Optional[Quantity] = None
    step: Optional[Quantity] = None
    n: int = 0
    values: list = field(default_factory=list)  # the numbers of an iterate range, in the unit of ``start``
    unit: str = ""
    quantity: Optional[Quantity] = None
    level: str = ""                            # set mesh <level>
    job: Optional[int] = None                  # "job #3"
    legacy: str = ""                           # the line for the CLI-style handlers
    browser: bool = False
    message: str = ""                          # one sentence when the line cannot be used
    missing: list = field(default_factory=list)  # analysis | target | parameter | range | what
    suggestions: list = field(default_factory=list)   # complete command lines
    notes: list = field(default_factory=list)  # "Using 'Left finger' for 'left fingr'."

    @property
    def ok(self) -> bool:
        return not self.message and not self.missing


def range_values(start: float, stop: float, n: int = 0, step: float = 0.0) -> list:
    """The cases of a sweep: ``n`` values from start to stop inclusive, or every ``step`` from start to stop."""
    if step:
        if (stop - start) * step < 0:
            step = -step
        k = int(math.floor(abs((stop - start) / step) + 1e-9)) + 1
        return [round(start + i * step, 12) for i in range(k)]
    if n <= 1:
        return [start]
    return [round(start + (stop - start) * i / (n - 1), 12) for i in range(n)]


def _words(text: str) -> list:
    return text.replace(",", " ").split()


def _strip_noise(words: list) -> list:
    out = list(words)
    while out and out[0].lower() in ("please", "now", "just", "can", "you", "could", "pls"):
        out.pop(0)
    return out


def _looks_like_path(s: str) -> bool:
    import os
    s = s.strip().strip("\"'")
    return bool(s) and (any(c in s for c in "/\\~") or s.lower().endswith((".ocae", "drone.json")) or
                        (os.path.exists(s) and not s.isalpha()))


def parse(text: str, ctx: Optional[Context] = None) -> Intent:
    ctx = ctx or Context()
    raw = text.strip()
    if not raw:
        return Intent("empty", raw)
    if raw.startswith(">>>"):
        return Intent("python", raw)
    words = _strip_noise(_words(raw))
    if not words:
        return Intent("empty", raw)
    first = words[0].lower()
    rest = words[1:]
    low_all = " ".join(w.lower() for w in words)
    if first == "hub" or low_all in ("results hub", "open results hub", "show results hub"):
        return Intent("hub", raw, browser="browser" in low_all)
    verb = VERB_WORDS.get(first)
    if verb is None:
        if first in LEGACY_COMMANDS:
            return Intent("legacy", raw, legacy=raw)
        return _unknown(raw, first, rest, ctx)
    fn = {"open": _open, "select": _select, "run": _run, "iterate": _iterate, "show": _show, "stop": _stop,
          "retry": _retry, "status": _status, "set": _set, "history": _simple, "clear": _simple,
          "help": _help}[verb]
    it = fn(verb, raw, first, rest, ctx)
    return it


def _simple(verb, raw, first, rest, ctx) -> Intent:
    return Intent(verb, raw, target=" ".join(rest))


def _help(verb, raw, first, rest, ctx) -> Intent:
    return Intent("help", raw, target=" ".join(rest).lower())


def _unknown(raw: str, first: str, rest: list, ctx: Context) -> Intent:
    pool = list(dict.fromkeys(list(VERB_WORDS) + list(LEGACY_COMMANDS)))
    near = difflib.get_close_matches(first, pool, n=3, cutoff=0.6)
    sugg = [" ".join([VERB_WORDS.get(n, n)] + rest).strip() for n in near]
    sugg = list(dict.fromkeys(sugg))
    msg = f"I do not know '{first}'."
    if near:
        msg += " Did you mean " + " or ".join(f"'{s}'" for s in sugg[:2]) + "?"
    else:
        msg += " Try 'help'."
    # a bare component name: offer to select it
    r = resolve(raw, ctx.components, 0.7)
    if r.entity is not None and near and not r.exact and difflib.SequenceMatcher(
            None, norm(raw), norm(r.entity.name)).ratio() < 0.9:
        r = Resolution()                # a typo'd verb, not a component name
    if r.entity is not None:
        sugg.insert(0, f"select {r.entity.name}")
        msg = f"'{raw}' is a component. Select it?"
    return Intent("unknown", raw, message=msg, suggestions=sugg[:4])


# ---- open ---------------------------------------------------------------------------------------
def _open(verb, raw, first, rest, ctx) -> Intent:
    arg = " ".join(rest).strip()
    low = arg.lower()
    if not arg:
        return Intent("open", raw, missing=["target"], message="What should I open?",
                      suggestions=[f"open {e.name}" for e in ctx.projects[:4]] + ["open dashboard"])
    if _looks_like_path(arg):
        return Intent("legacy", raw, legacy="open " + arg)
    toks = tokens(low)
    if toks and toks[0] == "hub" or low in ("results hub", "the hub", "the results hub"):
        return Intent("hub", raw, browser="browser" in low)
    if toks and toks[0] in ("dashboard", "dash", "site", "website", "report") or low in ("the dashboard",):
        return Intent("dashboard", raw, browser="browser" in low)
    if toks and toks[0] in ("result", "results") and len(toks) == 1:
        return Intent("open", raw, what="results")          # the newest finished job, in its own viewer
    if toks and toks[0] in ("mesh", "geometry", "table", "plot", "playback"):
        return _show("show", raw, "show", rest, ctx)
    r = resolve(arg, ctx.projects, 0.62)
    if r.entity is not None:
        it = Intent("open", raw, target=arg, entity=r.entity)
        if not r.exact:
            it.notes.append(f"Using '{r.entity.name}' for '{arg}'.")
        return it
    if r.ambiguous:
        return Intent("open", raw, target=arg, candidates=r.candidates, missing=["target"],
                      message=f"Which one: {', '.join(e.name for e in r.candidates[:4])}?",
                      suggestions=[f"open {e.name}" for e in r.candidates[:4]])
    comp = resolve(arg, ctx.components, 0.7)
    if comp.entity is not None or comp.ambiguous:
        return Intent("unknown", raw, message=f"'{arg}' is a component, not a project. Select it?",
                      suggestions=[f"select {e.name}" for e in (comp.candidates or [comp.entity])[:3]])
    near = nearest(arg, ctx.projects, 3)
    return Intent("open", raw, target=arg, message=f"I cannot find a project or example called '{arg}'.",
                  suggestions=[f"open {e.name}" for e in near] or ["open dashboard"])


# ---- select -------------------------------------------------------------------------------------
def _select(verb, raw, first, rest, ctx) -> Intent:
    arg = " ".join(rest).strip()
    low = norm(arg)
    if low in ("none", "nothing", "all clear", "clear", "no"):
        return Intent("select", raw, what="none")
    if not arg:
        return Intent("select", raw, missing=["target"], message="Select which component?",
                      suggestions=[f"select {e.name}" for e in ctx.components[:6]])
    it = Intent("select", raw, target=arg)
    if arg.lower() in ASSEMBLY_WORDS:
        it.assembly = True
        return it
    _bind(it, arg, ctx.components, "select", ctx)
    return it


def _bind(it: Intent, query: str, pool: list, verb: str, ctx: Context) -> None:
    """Resolve ``query`` in ``pool`` and fill entity / candidates / message / suggestions of ``it``."""
    r = resolve(query, pool)
    if r.entity is not None:
        it.entity = r.entity
        if not r.exact and r.score < 0.99:
            it.notes.append(f"Using '{r.entity.name}' for '{query}'.")
    elif r.ambiguous:
        it.candidates = r.candidates
        it.missing.append("target")
        it.message = f"Which one: {', '.join(e.name for e in r.candidates[:5])}?"
        it.suggestions = [f"{verb} {e.name}" for e in r.candidates[:5]]
    else:
        near = nearest(query, pool, 3)
        it.message = f"I cannot find '{query}' in this project."
        if near:
            it.message += " Did you mean " + " or ".join(f"'{e.name}'" for e in near[:2]) + "?"
        it.suggestions = [f"{verb} {e.name}" for e in near]
        it.missing.append("target")


# ---- run ----------------------------------------------------------------------------------------
def analysis_of(word: str) -> str:
    w = word.lower()
    for a, syn in ANALYSIS_WORDS.items():
        if w in syn:
            return a
    return ""


def _run(verb, raw, first, rest, ctx) -> Intent:
    low = [w.lower() for w in rest]
    flags = [w for w in low if w.startswith("--")]
    legacy_tokens = [w for w in low if not w.startswith("--") and w not in ("on", "for", "around", "of")]
    if flags or (legacy_tokens and all(w in LEGACY_ANALYSES or w in ("baseline",) for w in legacy_tokens)
                 and not any(w in ("structure",) and ctx.mode != "drone" and not ctx.drone for w in legacy_tokens)
                 and (ctx.drone or ctx.mode == "drone" or any(w in ("mass", "propulsion", "flight") for w in legacy_tokens))):
        return Intent("legacy", raw, legacy=raw)
    if not rest and (ctx.mode == "drone" or ctx.drone):
        return Intent("legacy", raw, legacy=raw)
    it = Intent("run", raw)
    # in the Drone mode "run cfd" / "run structure" without a component is the drone analysis
    prep_i = next((i for i, w in enumerate(low) if w in PREPOSITIONS), -1)
    head = rest if prep_i < 0 else rest[:prep_i]
    tail = [] if prep_i < 0 else rest[prep_i + 1:]
    for w in head:
        a = analysis_of(w)
        if a:
            it.analysis = a
            break
    if not it.analysis:  # "run on left finger" or the analysis after the preposition ("run for structural")
        for w in tail:
            a = analysis_of(w)
            if a and prep_i >= 0 and not tail[:tail.index(w)]:
                it.analysis = a
                tail = tail[tail.index(w) + 1:]
                break
    unknown = [w for w in head if not analysis_of(w) and w.lower() not in STOP_WORDS and
               w.lower() not in ("analysis", "study", "simulation", "run", "model")]
    # "run structural left finger": no preposition, the words after the analysis name are the target
    if prep_i < 0 and it.analysis:
        k = next(i for i, w in enumerate(rest) if analysis_of(w) == it.analysis)
        tail = [w for w in rest[k + 1:] if w.lower() not in ("analysis", "study", "simulation")]
        unknown = []
    if prep_i < 0 and not it.analysis and unknown:
        # a lone word that is a component ("run left finger") or an unknown analysis ("run banana")
        r = resolve(" ".join(unknown), ctx.components, 0.8)
        if r.entity is not None or r.ambiguous:
            tail, unknown = unknown, []
    # quantities in the tail ("... with 20 N")
    qty, clean_tail = None, []
    i = 0
    while i < len(tail):
        w = tail[i]
        if w.lower() in ("with", "using", "at", "and") :
            i += 1
            continue
        q, j = _take_quantity(tail, i)
        if q is not None and q.unit:
            qty, i = q, j
            continue
        clean_tail.append(w)
        i += 1
    it.quantity = qty
    target = " ".join(clean_tail).strip()
    it.target = target
    if not it.analysis:
        it.missing.append("analysis")
        if unknown:
            guess = difflib.get_close_matches(unknown[0].lower(), [w for s in ANALYSIS_WORDS.values() for w in s],
                                              n=1, cutoff=0.6)
            it.message = f"I do not know the analysis '{unknown[0]}'."
            if guess:
                it.suggestions = [f"run {analysis_of(guess[0])}" + (f" on {target}" if target else "")]
                it.message += f" Did you mean '{analysis_of(guess[0])}'?"
        else:
            it.message = "Which analysis: structural, thermal, modal, fluid or motion?"
            it.suggestions = [f"run {a}" + (f" on {target}" if target else "") for a in ANALYSIS_WORDS]
        return it
    if not target:
        return it
    if target.lower() in ASSEMBLY_WORDS or norm(target) in ("assembly", "whole model", "everything"):
        it.assembly = True
        return it
    if ctx.project_name and not resolve(target, ctx.components, 0.8).entity and \
            _score(norm(target), norm(target).split(), ctx.project_name) >= 0.85:
        it.assembly = True                       # "run motion on gripper": the project's name is the assembly
        return it
    pool = ctx.components + (ctx.studies if it.analysis == "motion" else [])
    _bind(it, target, pool, f"run {it.analysis} on", ctx)
    return it


# ---- iterate ------------------------------------------------------------------------------------
_RANGE_END = ("in", "by", "step", "steps", "with", "using", "on", "for", "every", "stepping")


def _iterate(verb, raw, first, rest, ctx) -> Intent:
    if any("=" in w for w in rest[1:]) and not any(w.lower() in ("from", "to") for w in rest):
        return Intent("legacy", raw, legacy=raw)           # iterate <variant> key=value ...
    it = Intent("iterate", raw)
    low = [w.lower() for w in rest]
    if "from" not in low:
        it.missing.append("range")
    fi = low.index("from") if "from" in low else len(rest)
    pwords = [w for w in rest[:fi] if w.lower() not in ("over", "the", "a", "my", "of", "to")]
    pname = " ".join(pwords).strip()
    # "iterate finger thickness 2 to 5 mm": allow a missing "from"
    a = b = None
    after = rest[fi + 1:] if fi < len(rest) else []
    if fi >= len(rest):
        for i, w in enumerate(rest):
            if _NUM_RE.match(w.split("mm")[0]) or _ATTACHED.match(w):
                pname = " ".join(x for x in rest[:i] if x.lower() not in ("over", "the", "a", "my", "of"))
                after = rest[i:]
                it.missing = [m for m in it.missing if m != "range"]
                break
    q1, j = _take_quantity(after, 0)
    if q1 is not None:
        k = j
        if k < len(after) and after[k].lower() in ("to", "until", "through", "->", "-", "and"):
            q2, j2 = _take_quantity(after, k + 1)
            if q2 is not None:
                a, b, j = q1, q2, j2
        if a is None:
            it.missing.append("range")
    elif "range" not in it.missing:
        it.missing.append("range")
    if a is not None and b is not None:
        if not a.unit and b.unit:
            a = Quantity(a.value, b.unit, b.dim)
        if not b.unit and a.unit:
            b = Quantity(b.value, a.unit, a.dim)
        it.start, it.stop, it.unit = a, b, a.unit
        tailw = after[j:] if q1 is not None else []
        tl = [w.lower() for w in tailw]
        n, step = 0, None
        i = 0
        while i < len(tl):
            w = tl[i]
            if w in ("in", "with", "using") and i + 1 < len(tl) and tl[i + 1].isdigit():
                n = int(tl[i + 1])
                i += 2
                continue
            if w.isdigit() and i + 1 < len(tl) and tl[i + 1] in ("steps", "step", "cases", "points", "values",
                                                                 "runs", "samples"):
                n = int(w)
                i += 2
                continue
            if w in ("by", "step", "steps", "every", "stepping"):
                j2 = i + 1
                if j2 < len(tl) and tl[j2] in ("of", "size", "="):
                    j2 += 1
                q, j3 = _take_quantity(tailw, j2)
                if q is not None:
                    step = q if q.unit or not it.unit else Quantity(q.value, it.unit, a.dim)
                    i = j3
                    continue
            i += 1
        it.step = step
        it.n = n
        if step is not None and step.value:
            it.values = range_values(a.value, b.value, step=step.to(it.unit) if it.unit and step.unit else step.value)
            it.n = len(it.values)
        elif n:
            it.values = range_values(a.value, b.value, n=n)
        else:
            it.missing.append("steps")
            it.message = "How many steps? For example: in 5 steps."
        if it.values and len(it.values) > 50:
            it.message = f"{len(it.values)} cases is a lot for one study. Use at most 50 steps."
    if not pname:
        it.missing.append("parameter")
        it.message = it.message or "Which parameter should I vary?"
        it.suggestions = [f"iterate {p.name} from " for p in ctx.parameters[:4]]
        return it
    it.target = pname
    r = resolve(pname, ctx.parameters)
    if r.entity is not None:
        it.param = r.entity
        if not r.exact and r.score < 0.99:
            it.notes.append(f"Using '{r.entity.name}' for '{pname}'.")
    elif r.ambiguous:
        it.candidates = r.candidates
        it.missing.append("parameter")
        it.message = f"Which parameter: {', '.join(e.name for e in r.candidates[:5])}?"
        it.suggestions = [f"iterate {e.name} " + _tail(it) for e in r.candidates[:5]]
    else:
        near = nearest(pname, ctx.parameters, 3)
        it.missing.append("parameter")
        it.message = f"I cannot find a parameter called '{pname}' in this project."
        if near:
            it.message += " Did you mean " + " or ".join(f"'{e.name}'" for e in near[:2]) + "?"
        it.suggestions = [f"iterate {e.name} " + _tail(it) for e in near]
    if it.param is not None and it.start is not None:
        pu = it.param.data.get("unit", "")
        if not it.unit and pu:
            it.unit = pu
            it.start = Quantity(it.start.value, pu, UNITS.get(pu, ("", 1))[0])
            it.stop = Quantity(it.stop.value, pu, UNITS.get(pu, ("", 1))[0])
        elif it.unit and pu and it.unit != pu:
            try:
                f = Quantity(1.0, it.unit).to(pu)
            except ValueError:
                it.message = f"{it.param.name} is in {pu}, not {it.unit}."
            else:
                it.values = [round(v * f, 12) for v in it.values]
                it.start, it.stop = Quantity(it.start.value * f, pu, UNITS[pu][0]), Quantity(it.stop.value * f, pu, UNITS[pu][0])
                it.unit = pu
    return it


def _tail(it: Intent) -> str:
    if it.start is not None and it.stop is not None:
        return f"from {it.start} to {it.stop}" + (f" in {it.n} steps" if it.n else "")
    return "from "


# ---- show ---------------------------------------------------------------------------------------
def show_key(text: str) -> str:
    t = norm(text)
    if not t:
        return ""
    for key, syn in SHOW_WORDS.items():
        if t == key or t in [norm(s) for s in syn]:
            return key
    cand = {}
    for k, syn in SHOW_WORDS.items():
        for s in (k,) + tuple(syn):
            cand[norm(s)] = k
    best = difflib.get_close_matches(t, list(cand), n=1, cutoff=0.75)
    return cand[best[0]] if best else ""


def _show(verb, raw, first, rest, ctx) -> Intent:
    arg = " ".join(rest).strip()
    it = Intent("show", raw, target=arg)
    m = re.match(r"^(?:job\s*)?#?(\d+)$", arg.lower()) or re.match(r"^job\s*#?(\d+)", arg.lower())
    if m:
        it.what, it.job = "job", int(m.group(1))
        return it
    if first == "export" and not arg:
        it.what = "csv"
        return it
    if first == "plot" and not arg:
        it.what = "plot"
        return it
    if not arg:
        it.missing.append("what")
        it.message = "Show what?"
        it.suggestions = [f"show {w}" for w in ("displacement", "stress", "table", "plot", "mesh")]
        return it
    key = show_key(re.sub(r"^(?:the|me)\s+", "", arg.lower()))
    if not key and first == "plot":
        key = "plot"
    if key:
        it.what = key
        return it
    it.missing.append("what")
    near = difflib.get_close_matches(norm(arg), [w for s in SHOW_WORDS.values() for w in s], n=3, cutoff=0.5)
    it.message = f"I do not know how to show '{arg}'."
    it.suggestions = list(dict.fromkeys(f"show {show_key(n) or n}" for n in near)) or ["show results"]
    return it


# ---- stop / retry / status ------------------------------------------------------------------------
def _jobref(rest: list) -> Optional[int]:
    m = re.search(r"#?(\d+)", " ".join(rest))
    return int(m.group(1)) if m else None


def _stop(verb, raw, first, rest, ctx) -> Intent:
    if first in ("stop", "cancel") and rest and rest[0].lower() in ("python", "on", "off"):
        return Intent("legacy", raw, legacy=raw)
    it = Intent("stop", raw, job=_jobref(rest))
    if rest and rest[0].lower() == "all":
        it.what = "all"
    return it


def _retry(verb, raw, first, rest, ctx) -> Intent:
    return Intent("retry", raw, job=_jobref(rest))


def _status(verb, raw, first, rest, ctx) -> Intent:
    if rest and (_looks_like_path(" ".join(rest))):
        return Intent("legacy", raw, legacy=raw)
    return Intent("status", raw, job=_jobref(rest))


# ---- set ----------------------------------------------------------------------------------------
def _set(verb, raw, first, rest, ctx) -> Intent:
    if first == "set" and rest and ("." in rest[0] or rest[0] in ctx.drone_keys or
                                    (len(rest) >= 2 and rest[0].lower() == "python")):
        return Intent("legacy", raw, legacy=raw)             # set propulsion.battery.cells 4 [status]
    it = Intent("set", raw)
    low = [w.lower() for w in rest]
    if low and low[0] in ("mesh", "meshing", "mesh_density") or (low and low[0] == "mesh"):
        lvl = next((MESH_LEVELS[w] for w in low[1:] if w in MESH_LEVELS), "")
        it.what, it.level = "mesh", lvl
        if not lvl:
            it.missing.append("what")
            it.message = "Mesh density: coarse, medium or fine?"
            it.suggestions = [f"set mesh {x}" for x in ("coarse", "medium", "fine")]
        return it
    if len(low) >= 2 and low[0] in ("coarse", "medium", "fine") and low[1] == "mesh":
        it.what, it.level = "mesh", MESH_LEVELS[low[0]]
        return it
    # set <parameter words> [to|=|as] <quantity>
    k = len(rest)
    while k > 0:
        q, j = _take_quantity(rest, k - 1) if k - 1 >= 0 else (None, 0)
        if q is not None and j == len(rest):
            break
        k -= 1
    # the quantity may be 2 words ("3 mm"): search for the earliest index where a quantity runs to the end
    idx = next((i for i in range(len(rest)) if _take_quantity(rest, i)[0] is not None
                and _take_quantity(rest, i)[1] == len(rest)), -1)
    if idx < 0:
        it.missing.append("what")
        it.message = "Set what to which value? For example: set finger thickness to 3 mm"
        return it
    q, _ = _take_quantity(rest, idx)
    pw = [w for w in rest[:idx] if w.lower() not in ("to", "=", "as", "the", "a")]
    it.quantity = q
    it.target = " ".join(pw)
    it.what = "parameter"
    if not pw:
        it.missing.append("parameter")
        it.message = "Set which parameter?"
        return it
    r = resolve(it.target, ctx.parameters)
    if r.entity is not None:
        it.param = r.entity
        pu = r.entity.data.get("unit", "")
        if q.unit and pu and q.unit != pu:
            try:
                it.quantity = Quantity(q.to(pu), pu, UNITS[pu][0])
            except ValueError:
                it.message = f"{r.entity.name} is in {pu}, not {q.unit}."
        elif not q.unit and pu:
            it.quantity = Quantity(q.value, pu, UNITS[pu][0])
    elif r.ambiguous:
        it.candidates = r.candidates
        it.missing.append("parameter")
        it.message = f"Which parameter: {', '.join(e.name for e in r.candidates[:5])}?"
        it.suggestions = [f"set {e.name} to {q}" for e in r.candidates[:5]]
    else:
        near = nearest(it.target, ctx.parameters, 3)
        it.missing.append("parameter")
        it.message = f"I cannot find a parameter called '{it.target}'."
        it.suggestions = [f"set {e.name} to {q}" for e in near]
    return it


# ------------------------------------------------------------------------------------------------ completion
def _ranked_lines(prefix: str, frag: str, names: list, limit: int) -> list:
    """Full command lines ``prefix + name`` for the names that best match the fragment typed so far."""
    f = norm(frag)
    if not f:
        return [prefix + n for n in names[:limit]]
    scored = []
    for n in names:
        nn = norm(n)
        if nn.startswith(f):
            s = 1.0
        elif any(t.startswith(f) for t in nn.split()):
            s = 0.9
        elif f in nn:
            s = 0.8
        else:
            s = max(difflib.SequenceMatcher(None, f, nn[:max(len(f), 1) + 3]).ratio() * 0.75,
                    max(difflib.SequenceMatcher(None, f, t).ratio() for t in nn.split()) * 0.85)
            if s < 0.55:
                continue
        scored.append((-s, n))
    return [prefix + n for _, n in sorted(scored)[:limit]]


def complete(text: str, ctx: Optional[Context] = None, limit: int = 8) -> list:
    """Candidate command lines for what has been typed (verbs, analyses, components, parameters, ...)."""
    ctx = ctx or Context()
    if not text.strip() or text.lstrip().startswith(">>>"):
        return []
    trailing = text.endswith(" ")
    words = text.split()
    if len(words) == 1 and not trailing:
        verbs = list(dict.fromkeys(list(VERBS) + list(LEGACY_COMMANDS)))
        out = _ranked_lines("", words[0], verbs, limit)
        return [o + " " for o in out]
    first = words[0].lower()
    verb = VERB_WORDS.get(first, first)
    rest = words[1:]
    names_c = [e.name for e in ctx.components]
    head = " ".join(words) + (" " if trailing else "")
    base = text[: len(text) - len(words[-1])] if not trailing else text
    if verb == "run":
        low = [w.lower() for w in rest]
        pi = next((i for i, w in enumerate(low) if w in PREPOSITIONS), -1)
        if pi >= 0:
            frag = " ".join(rest[pi + 1:]) if not trailing or pi + 1 < len(rest) else ""
            pre = " ".join(words[: 1 + pi + 1]) + " "
            pool = names_c + ["assembly"]
            if low[0] and analysis_of(low[0]) == "motion":
                pool += [e.name for e in ctx.studies]
            return _ranked_lines(pre, frag, pool, limit)
        if len(rest) >= 1 and analysis_of(rest[0]) and (trailing or len(rest) > 1):
            return [f"{' '.join(words)} {p} " if trailing else f"{' '.join(words[:-1])} {p} "
                    for p in ("on",) if (trailing and not words[-1].lower() in PREPOSITIONS) or
                    (not trailing and p.startswith(words[-1].lower()))] or []
        frag = "" if trailing else rest[-1] if rest else ""
        pre = " ".join(words[:1] + (rest[:-1] if not trailing else rest)) + " "
        pool = list(ANALYSIS_WORDS) + [w for w in LEGACY_ANALYSES if w not in ("structure", "cfd")]
        return _ranked_lines(pre, frag, pool, limit)
    if verb == "select":
        frag = " ".join(rest) if not trailing or len(rest) else ""
        return _ranked_lines(words[0] + " ", frag, names_c + ["assembly", "none"], limit)
    if verb == "open":
        frag = " ".join(rest)
        pool = [e.name for e in ctx.projects] + ["dashboard", "results"]
        return _ranked_lines(words[0] + " ", frag, pool, limit)
    if verb == "show":
        frag = " ".join(rest)
        return _ranked_lines(words[0] + " ", frag, list(SHOW_WORDS), limit)
    if verb == "iterate":
        low = [w.lower() for w in rest]
        if "from" in low:
            tail = " ".join(rest[low.index("from") + 1:])
            nums = re.findall(_NUM, tail)
            if len(nums) >= 2 and " in " not in " " + tail.lower() + " ":
                return [text.rstrip() + " in 5 steps"]
            if len(nums) == 1 and " to " not in " " + tail.lower() + " " and trailing:
                return [text.rstrip() + " to "]
            return []
        frag = " ".join(rest)
        if trailing and rest:
            r = resolve(frag, ctx.parameters)
            if r.entity is not None:
                return [f"{words[0]} {r.entity.name} from "]
        names = [e.name for e in ctx.parameters]
        return [o if o.endswith(" ") else o for o in _ranked_lines(words[0] + " ", frag, names, limit)]
    if verb == "set":
        frag = " ".join(rest)
        return _ranked_lines(words[0] + " ", frag, [e.name for e in ctx.parameters] + ["mesh coarse", "mesh medium",
                                                                                         "mesh fine"], limit)
    if verb in ("stop", "retry"):
        return [f"{words[0]} job #{n}" for n, _, st in ctx.jobs
                if (verb == "stop") == (st in ("running", "queued", "paused"))][:limit]
    return []
