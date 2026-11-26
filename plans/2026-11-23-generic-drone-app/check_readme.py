"""Check that everything README.md names exists in the code. Exit 0 when all is found, 1 otherwise.

Run from the repository root:  PYTHONPATH=$PWD python plans/2026-11-23-generic-drone-app/check_readme.py

Checks
  CLI           every ``opencae <cmd>`` line in the <!-- cli --> block: ``python -m opencae <cmd> --help`` works and
                every ``--flag`` on the line appears in that help; the command table's commands are real too
  console       first-column commands of the <!-- console-commands --> table are keys of ui/console.py COMMANDS
  ribbon        pages and button labels of the <!-- ribbon --> table exist in the ribbon / UI source
  shortcuts     every key combination of the <!-- shortcuts --> table is a string literal in the UI source
  metrics       every ``analysis.metric`` of the <!-- metrics --> block is a metric key of that analysis module
  file paths    every repo path in backticks exists; runtime paths (%USERPROFILE%, ~) name a file the code uses
  modules       every ``python -m pkg.mod`` has a module file; ``opencae-studio``/launcher options exist
  env vars      every OPENCAE_* name appears in the source or the launcher
  pytest        marker names in the README are declared in pyproject.toml
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
README = (ROOT / "README.md").read_text(encoding="utf-8")
failures: list = []
checked = 0


def ok(kind: str, what: str, good: bool, detail: str = "") -> None:
    global checked
    checked += 1
    if not good:
        failures.append(f"MISSING {kind}: {what} {detail}".rstrip())


def block(name: str) -> str:
    m = re.search(rf"<!-- {name} -->(.*?)<!-- /{name} -->", README, re.S)
    if not m:
        failures.append(f"README has no <!-- {name} --> block")
        return ""
    return m.group(1)


def ticks(text: str) -> list:
    """Backticked spans; double-backtick spans (for text containing a backtick) are handled."""
    out = []
    for m in re.finditer(r"``\s*(.+?)\s*``|`([^`\n]+)`", text):
        out.append((m.group(1) or m.group(2)).strip())
    return out


def src(*rel: str) -> str:
    return "\n".join((ROOT / r).read_text(encoding="utf-8", errors="replace") for r in rel)


def all_source() -> str:
    parts = []
    for p in list((ROOT / "opencae").rglob("*.py")) + list(ROOT.glob("*.bat")) + [ROOT / "pyproject.toml"]:
        if "tests" in p.parts:
            continue
        parts.append(p.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(parts)


def run(cmd: list, timeout=120) -> subprocess.CompletedProcess:
    import os
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=str(ROOT), env=env)


# ------------------------------------------------------------------------------------------------ CLI
top = run([sys.executable, "-m", "opencae", "--help"])
ok("CLI", "python -m opencae --help", top.returncode == 0, top.stderr[-200:])
help_cache: dict = {}


def cmd_help(cmd: str) -> str:
    if cmd not in help_cache:
        r = run([sys.executable, "-m", "opencae", cmd, "--help"])
        help_cache[cmd] = r.stdout if r.returncode == 0 else ""
    return help_cache[cmd]


cli_block = block("cli")
for line in cli_block.splitlines():
    m = re.match(r"\s*opencae\s+(\w+)(.*)", line)
    if not m:
        continue
    cmd, rest = m.group(1), m.group(2)
    h = cmd_help(cmd)
    ok("CLI command", f"opencae {cmd}", bool(h), "(`--help` failed)")
    for flag in re.findall(r"(--[a-z][a-z-]*)", rest):
        ok("CLI flag", f"opencae {cmd} {flag}", flag in h)
# the command table: | `new DIR` | `--cad FILE` ... |
CLI_SECTION = README.split("<!-- /cli -->", 1)[-1].split("Analysis names for", 1)[0]
for row in re.findall(r"^\| `(\w+)(?: DIR)?` \| (.*?) \|", CLI_SECTION, re.M):
    cmd, flags = row
    if cmd not in ("new", "run", "iterate", "status", "dashboard", "clean", "doctor"):
        continue
    h = cmd_help(cmd)
    ok("CLI command", f"table: opencae {cmd}", bool(h))
    for flag in re.findall(r"`(--[a-z][a-z-]*)", flags):
        ok("CLI flag", f"table: opencae {cmd} {flag}", flag in h)
doc_cmds = set(re.findall(r"^\| `(new|run|iterate|status|dashboard|clean|doctor)(?: DIR)?` \|", CLI_SECTION, re.M))
for cmd in ("new", "run", "iterate", "status", "dashboard", "clean", "doctor"):
    ok("CLI command", f"opencae {cmd} is documented in the command table", cmd in doc_cmds)
for code in ("0", "1", "2"):
    ok("exit code", code, f"| `{code}` |" in README)
ok("exit codes", "cli.py defines 0/1/2", "EXIT_OK, EXIT_SOLVER, EXIT_BAD = 0, 1, 2" in src("opencae/cli.py"))

# ------------------------------------------------------------------------------------------------ console
console_src = src("opencae/ui/console.py")
console_keys = set(re.findall(r'^    "(\w+)": \(', console_src, re.M))
documented = set()
for m in re.finditer(r"^\| `(\w+)` \|", block("console-commands"), re.M):
    documented.add(m.group(1))
    ok("console command", m.group(1), m.group(1) in console_keys)
for key in console_keys:
    ok("console command", f"{key} is documented", key in documented)
for ex in re.findall(r"^\| `\w+` \| .*? \| `(.+?)` \|$", block("console-commands"), re.M):
    first = ex.split()[0]
    ok("console example", ex, first in console_keys)
for name in ("app", "win", "project", "spec", "pipeline", "spec_mod", "geometry", "np", "results"):
    ok("python variable", name, f"{name}, " in console_src or f"{name}=" in console_src
       or f'ns["{name}"]' in console_src or f'("{name}",' in console_src)
    ok("python variable documented", name, f"| `{name}` |" in README)

# ------------------------------------------------------------------------------------------------ ribbon
mw = src("opencae/ui/main_window.py")
ui_text = src("opencae/ui/main_window.py", "opencae/ui/console.py", "opencae/ui/drone/ux.py",
              "opencae/ui/drone/tracker.py", "opencae/ui/drone/workspace.py")
pages_src = re.search(r"pages = \{(.*?)\n        \}\n", mw, re.S)
page_names = set(re.findall(r'^\s+"([^"]+)": \[', pages_src.group(1), re.M)) if pages_src else set()
for row in re.findall(r"^\| `([^`]+)` \| (.*) \|$", block("ribbon"), re.M):
    page, buttons = row
    ok("ribbon page", page, page in page_names)
    for label in ticks(buttons):
        ok("ribbon button", f"{page} > {label}", f'"{label}"' in ui_text, "(no such label in the UI source)")
for page in page_names:
    ok("ribbon page", f"{page} is documented", f"| `{page}` |" in block("ribbon"))
# the Drone page button order really holds those labels
drone_row = next((b for p, b in re.findall(r"^\| `([^`]+)` \| (.*) \|$", block("ribbon"), re.M) if p == "Drone"), "")
for label in ("Run all", "Run…", "Stop", "Report", "Folder", "Shortcuts", "Progress", "Console"):
    ok("ribbon button", f"Drone > {label} documented", f"`{label}`" in drone_row)

# ------------------------------------------------------------------------------------------------ shortcuts
for row in re.findall(r"^\| (.*?) \| (?:Drone|Studio|Everywhere) \|", block("shortcuts"), re.M):
    for combo in ticks(row):
        ok("shortcut", combo, f'"{combo}"' in ui_text or f"'{combo}'" in ui_text)
ok("shortcut", "console toggle F12 and Ctrl+`", 'QKeySequence("Ctrl+`")' in console_src and '"F12"' in console_src)
ux_text = src("opencae/ui/drone/ux.py")
for keys, _ in re.findall(r'^    \("([^"]+)", "([^"]+)"\),$', ux_text, re.M):
    for k in [x.strip() for x in keys.split("/")]:
        ok("shortcut documented", k, f"`{k}`" in README)

# ------------------------------------------------------------------------------------------------ metrics
for dotted in ticks(block("metrics")):
    aid, _, key = dotted.partition(".")
    path = ROOT / "opencae/drone/analyses" / f"{aid}.py"
    ok("metric", dotted, path.exists() and (f'"{key}"' in path.read_text(encoding="utf-8")),
       "(analysis module has no such metric)")

# ------------------------------------------------------------------------------------------------ files, modules, env, markers
everything = all_source()
runtime_re = re.compile(r"^(%USERPROFILE%|~|%)")
REPO_PREFIX = ("opencae/", "docs/", "plans/", "standards/", ".claude/", "opencae\\")
EXT = (".bat", ".md", ".py", ".toml", ".json", ".csv", ".jsonl", ".txt", ".log", ".npz", ".step", ".lock")
seen = set()
for tok in ticks(README):
    t = tok.strip()
    if t in seen or any(c in t for c in " <>*|{}$=(),;:") and not t.startswith("%USERPROFILE%"):
        if not t.startswith("%USERPROFILE%"):
            continue
    seen.add(t)
    t_norm = t.replace("\\", "/")
    if t.startswith("%USERPROFILE%"):
        parts = [p for p in t_norm.split("/") if p and p != "%USERPROFILE%"]
        last = parts[-1] if parts else ""
        if last.startswith("."):  # a folder like .opencae
            ok("runtime path", t, last in everything)
        else:
            ok("runtime path", t, last in everything or last.split(".")[0] in everything, f"('{last}' unused)")
        continue
    is_repo = t_norm.startswith(REPO_PREFIX) or (
        "/" not in t_norm and t_norm.endswith((".bat", ".md", ".toml")) and t_norm[0].isalpha())
    if is_repo:
        # a folder like opencae/drone/ or a file; a result file like results/... is not a repo path
        ok("file", t, (ROOT / t_norm.rstrip("/")).exists() or (ROOT / "docs" / t_norm).exists())
for t in ("README.md", "START_HERE.md", "AGENTS.md", "launch_opencae.bat", "open_website.bat", "LICENSE",
          "pyproject.toml"):
    ok("file", t, (ROOT / t).exists())
# runtime names that the README relies on
for name in ("crash.log", "opencae.log", "console_history.txt", "runs.jsonl", "k09l_website", "projects", "_scratch",
             "results.json", "geometry.npz", "session.lock"):
    ok("runtime name", name, name in everything)

for mod in set(re.findall(r"python -m ((?:opencae)[\w.]*)", README)):
    p = ROOT / (mod.replace(".", "/"))
    ok("module", mod, (p.with_suffix(".py")).exists() or (p / "__main__.py").exists())

app_help = run([sys.executable, "-m", "opencae.app.main", "--help"])
ok("app", "python -m opencae.app.main --help", app_help.returncode == 0, app_help.stderr[-200:])
for opt in ("--drone", "--example", "--safe", "--diagnose", "--selftest"):
    ok("app option", opt, opt in app_help.stdout)
for ex in ("cantilever", "tiltwing", "k09l", "k09l-revB", "k09l-monitor"):
    ok("app example", ex, ex in app_help.stdout)
bat = src("launch_opencae.bat")
for opt in ("--doctor", "--acceptance", "OPENCAE_GUI_ENV", "OPENCAE_CONDA_ROOT", "qt_test"):
    ok("launcher", opt, opt in bat)
ok("entry point", "opencae-studio", "opencae-studio" in src("pyproject.toml"))
ok("entry point", "opencae", re.search(r'^opencae = "opencae.cli:main"', src("pyproject.toml"), re.M) is not None)

for env in set(re.findall(r"\b(OPENCAE_[A-Z_]+)\b", README)):
    ok("env var", env, env in everything)

toml = src("pyproject.toml")
for marker in ("solver", "gui", "slow", "openfoam"):
    ok("pytest marker", marker, f'"{marker}:' in toml)
for t in ("test_cli.py",):
    ok("file", f"opencae/tests/{t}", (ROOT / "opencae/tests" / t).exists())

# pipeline facts the README states
pipe = src("opencae/drone/pipeline.py")
for fact in ('RUNS_FILE = "runs.jsonl"', '"_scratch"'):
    ok("pipeline", fact, fact in pipe)
ok("types", 'ANALYSES = ("mass", "propulsion", "structure", "cfd", "flight")',
   'ANALYSES = ("mass", "propulsion", "structure", "cfd", "flight")' in src("opencae/drone/types.py"))
for aid in ("mass", "propulsion", "structure", "cfd", "flight"):
    ok("analysis module", aid, (ROOT / f"opencae/drone/analyses/{aid}.py").exists())
srv = src("opencae/web/server.py")
for route in ("/api/project", "/api/results", "/api/geometry", "/api/field", "/api/progress", "/api/variants",
              "/api/history", "/api/run", "/api/params", "/api/iterate", "/api/stop"):
    ok("API route", route, route in srv)
dash = src("opencae/web/dashboard/index.html")
for tab in ("Results", "Contours", "Flight", "Parameters", "Iterations"):
    ok("dashboard tab", tab, f">{tab}</button>" in dash)
for status in ("ok", "conditional", "watch", "fail", "not_evaluated", "error"):
    ok("status", status, f'"{status}"' in src("opencae/drone/types.py") or status in src("opencae/drone/types.py"))

# ------------------------------------------------------------------------------------------------ Motion / MuJoCo install path
req, toml, win = src("requirements.txt"), src("pyproject.toml"), src("INSTALL_WINDOWS.md")
ok("install", "requirements.txt pins mujoco", re.search(r"^mujoco==\d", req, re.M) is not None)
ok("install", "pyproject.toml has the motion extra", re.search(r'^motion = \["mujoco', toml, re.M) is not None)
ok("install", "INSTALL_WINDOWS.md pip line has mujoco", re.search(r"^pip install .*\bmujoco==", win, re.M) is not None)
ok("install", "PyInstaller spec bundles mujoco", 'collect_all("mujoco")' in src("packaging/opencae_studio.spec"))
ok("install", "doctor reports mujoco", '"mujoco"' in src("opencae/cli.py") and "mujoco" in run([sys.executable, "-m", "opencae", "doctor"]).stdout)
ok("install", "opencae setup mujoco", "mujoco" in cmd_help("setup"))
ok("install", "Solvers dialog has a MuJoCo row", "mujoco_btn" in src("opencae/ui/drone/ux.py"))
ok("install", "README names the mujoco install line", "python -m pip install mujoco" in README)
ok("install", "README names the extra", '".[motion]"' in README)
motion = README.split("### Motion mode", 1)[-1].split("###", 1)[0]
ws_src = src("opencae/ui/assembly/workspace.py", "opencae/ui/assembly/plots.py", "opencae/ui/assembly/playback.py",
             "opencae/ui/assembly/panels.py")
for label in ("Gripper", "Edit…", "Run", "Play", "Load case"):
    ok("motion button", label, f"`{label}`" in motion and f'"{label}"' in ws_src, "(not in README or not in the UI)")
ok("motion", "plot check 'Fingertip gap'", "`Fingertip gap`" in motion and '"Fingertip gap"' in ws_src)
ok("motion", "tab 'Motion'", "`Motion`" in motion and 'PAGE = "Motion"' in ws_src)

# ------------------------------------------------------------------------------------------------ result
print(f"checked {checked} items")
if failures:
    print(f"{len(failures)} problem(s):")
    for f in failures:
        print("  " + f)
    sys.exit(1)
print("README check passed: every command, button, shortcut and path it names exists.")
