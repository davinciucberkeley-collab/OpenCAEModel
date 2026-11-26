"""``opencae`` command line: run a drone project, iterate a design, check the tools. Built for agents.

With ``--json`` a command prints exactly one JSON object on stdout; progress and messages go to stderr.
Exit codes: 0 done, 1 an analysis ended in status ``error`` (or an unexpected internal error), 2 bad input
(missing project, bad key/value, unknown analysis, unreadable CAD file). With ``--json`` every failure prints
``{"error": ...}`` on stdout; a raw traceback is never shown.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

EXIT_OK, EXIT_SOLVER, EXIT_BAD = 0, 1, 2


class BadInput(Exception):
    pass


# ------------------------------------------------------------------------------------------------ helpers
def parse_value(text: str):
    t = text.strip()
    if t.lower() == "true":
        return True
    if t.lower() == "false":
        return False
    if t.lower() == "null":
        return None
    try:
        return int(t)
    except ValueError:
        pass
    try:
        return float(t)
    except ValueError:
        return t


def parse_set(items: Optional[list]) -> dict:
    """['a.b=3', 'c=true'] -> {'a.b': 3, 'c': True}"""
    out = {}
    for it in items or []:
        key, sep, val = it.partition("=")
        if not sep or not key.strip():
            raise ValueError(f"--set expects key=value, got '{it}'")
        out[key.strip()] = parse_value(val)
    return out


def _err(*a):
    print(*a, file=sys.stderr, flush=True)


def _emit(args, obj: dict, human) -> None:
    if args.json:
        print(json.dumps(obj, default=str))
    else:
        human(obj)


def _project(path: str) -> Path:
    p = Path(path)
    if p.is_file() and p.name == "drone.json":
        p = p.parent
    if not p.exists():
        raise BadInput(f"project folder not found: {p}")
    if p.is_dir() and not (p / "drone.json").exists():
        raise BadInput(f"no drone.json in {p}")
    return p


def _progress(msg: str, frac: float = 0.0) -> None:
    _err(f"[{int(max(0.0, min(1.0, frac or 0.0)) * 100):3d}%] {msg}")


def _check_overrides(project: Path, overrides: dict) -> None:
    """Reject a bad key or value before anything runs (drone.json is never touched)."""
    if not overrides:
        return
    from opencae.drone import spec
    try:
        spec.apply_overrides(spec.load(project), overrides)
    except (KeyError, ValueError, TypeError) as e:
        raise BadInput(str(e).strip("'\"") or type(e).__name__)


def _check_only(only: Optional[list]) -> None:
    if not only:
        return
    from opencae.drone.types import ANALYSES
    bad = [o for o in only if o not in ANALYSES]
    if bad:
        raise BadInput(f"unknown analysis {', '.join(bad)}; choose from {', '.join(ANALYSES)}")


# ------------------------------------------------------------------------------------------------ summaries
def _human_results(res: dict) -> None:
    print(f"Verdict: {res.get('verdict', 'n/a')}")
    for aid, a in (res.get("analyses") or {}).items():
        print(f"  {aid:<11} {a.get('status', '?'):<14} {a.get('summary', '')}")
    checks = res.get("checks") or []
    if checks:
        counts: dict = {}
        for c in checks:
            counts[c.get("status", "?")] = counts.get(c.get("status", "?"), 0) + 1
        print("Requirement checks: " + ", ".join(f"{n} {s}" for s, n in sorted(counts.items())))
    print("Simulation results plan the tests; they are not flight approval.")


def _exit_for(res: dict) -> int:
    return EXIT_SOLVER if any(a.get("status") == "error" for a in (res.get("analyses") or {}).values()) \
        else EXIT_OK


# ------------------------------------------------------------------------------------------------ commands
def cmd_new(args) -> int:
    from opencae.drone import pipeline
    cad = Path(args.cad)
    if not cad.exists():
        raise BadInput(f"CAD file not found: {cad}")
    try:
        proj = pipeline.new_project(Path(args.dir), cad, args.name or Path(args.dir).name, args.type)
    except (ValueError, KeyError, OSError) as e:
        raise BadInput(str(e))
    out = {"project": str(proj), "roles": guess_roles_of(Path(proj))}

    def human(o):
        print(f"Created {o['project']}")
        for role, names in (o["roles"] or {}).items():
            print(f"  {role:<16} {len(names)} part(s): {', '.join(names[:6])}{' ...' if len(names) > 6 else ''}")
        print("Fill in drone.json (every property starts empty, null), then: opencae run " + o["project"])
    _emit(args, out, human)
    return EXIT_OK


def guess_roles_of(project: Path) -> dict:
    """{role: [part names]} of a project's imported geometry; {} when it cannot be read."""
    try:
        from opencae.drone import geometry, spec
        ac = geometry.load(project, spec.load(project))
        roles: dict = {}
        for p in ac.parts:
            roles.setdefault(p.role, []).append(p.name)
        return roles
    except Exception:
        return {}


def cmd_run(args) -> int:
    proj = _project(args.dir)
    from opencae.drone import pipeline
    only = [s for s in (args.only or "").split(",") if s] or None
    _check_only(only)
    overrides = parse_set(args.set)
    _check_overrides(proj, overrides)
    try:
        res = pipeline.run_project(proj, only=only, overrides=overrides or None, variant=args.variant,
                                   progress=_progress, keep_cases=args.keep_cases, force=args.force)
    except (KeyError, ValueError) as e:
        raise BadInput(str(e).strip("'\""))
    _emit(args, res, _human_results)
    return _exit_for(res)


def cmd_iterate(args) -> int:
    proj = _project(args.dir)
    from opencae.drone import pipeline
    overrides = parse_set(args.set)
    if not overrides:
        raise BadInput("iterate needs at least one --set key=value")
    only = [s for s in (args.only or "").split(",") if s] or None
    _check_only(only)
    _check_overrides(proj, overrides)
    try:
        out = pipeline.iterate(proj, overrides, args.name, only=only)
    except (KeyError, ValueError) as e:
        raise BadInput(str(e).strip("'\""))

    def human(o):
        print(f"Variant '{o.get('variant')}'")
        _human_results(o.get("results") or {})
        for row in o.get("compare") or []:
            print("  " + "  ".join(f"{k}={v}" for k, v in row.items()) if isinstance(row, dict) else f"  {row}")
    _emit(args, out, human)
    return _exit_for(out.get("results") or {})


def cmd_status(args) -> int:
    proj = _project(args.dir)
    from opencae.drone import pipeline
    st = pipeline.status(proj)

    def human(o):
        for k, v in o.items():
            print(f"{k}: {v}")
    _emit(args, st, human)
    return EXIT_OK


def cmd_clean(args) -> int:
    proj = _project(args.dir)
    from opencae.drone import pipeline
    n = pipeline.clean(proj, results=args.results)
    _emit(args, {"freed_bytes": int(n)}, lambda o: print(f"Freed {o['freed_bytes'] / 1e6:.1f} MB"))
    return EXIT_OK


def cmd_dashboard(args) -> int:
    proj = _project(args.dir)
    from opencae.web.server import DashboardServer
    srv = DashboardServer(proj, port=args.port)
    url = srv.start()
    try:
        if args.json:
            print(json.dumps({"url": url}))
        else:
            print(f"Dashboard: {url}   (Ctrl+C to stop)")
        sys.stdout.flush()
        if not args.no_browser:
            import webbrowser
            webbrowser.open(url)
        import time
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        srv.stop()
    return EXIT_OK


# ------------------------------------------------------------------------------------------------ setup
def cmd_setup(args) -> int:
    """``opencae setup [su2|openfoam|mujoco] [--yes]``: status, or (with --yes) install and test an open-source solver."""
    from opencae.drone import solver_setup as S
    if not args.target:
        st = S.status()
        _emit(args, st, lambda o: print(S.describe(o)))
        return EXIT_OK
    pl = S.plan(args.target)
    if not args.yes:
        pl["confirmed"] = False
        pl.setdefault("message", f"Would download {pl['size']} from {pl['source']} into {pl['into']}. "
                                 f"Run again with --yes to do it.")
        _emit(args, pl, lambda o: print(o["message"]))
        return EXIT_BAD
    res = S.install(args.target, progress=_progress)
    _emit(args, res, lambda o: print(o["message"] + ("\n" + o["log"] if not o["ok"] and o.get("log") else "")))
    return EXIT_OK if res.get("ok") else EXIT_SOLVER


# ------------------------------------------------------------------------------------------------ doctor
def _run_version(cmd: list, timeout=10) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        txt = (r.stdout or "") + (r.stderr or "")
        for line in txt.splitlines():
            if line.strip():
                return line.strip()[:120]
    except Exception:
        pass
    return ""


def doctor_report() -> dict:
    d: dict = {"python": {"available": True, "version": sys.version.split()[0], "path": sys.executable}}
    try:
        from opencae.solver.calculix.runner import find_ccx, ccx_version
        ccx = find_ccx()
        ccx_ver = ccx_version(ccx) if ccx else ""
    except Exception:
        ccx = os.environ.get("OPENCAE_CCX") or shutil.which("ccx")
        ccx_ver = _run_version([ccx, "-v"]) if ccx else ""
    d["ccx"] = {"available": bool(ccx), "path": ccx or "",
                "version": ccx_ver,
                "note": "" if ccx else "CalculiX not found: structure analysis will report an error naming ccx"}
    try:
        from opencae.cfd.openfoam.install import find_openfoam
        inst = find_openfoam()
    except Exception:
        inst = None
    d["openfoam"] = {"available": bool(inst), "path": getattr(inst, "path", "") if inst else "",
                     "version": getattr(inst, "version", "") if inst else "",
                     "note": "" if inst else "OpenFOAM not found: CFD will report an error naming it"}
    for key, mod, label in (("gmsh", "gmsh", "gmsh"), ("ocp", "OCP", "OCP (STEP import)"),
                            ("pyside6", "PySide6", "PySide6")):
        try:
            found = importlib.util.find_spec(mod) is not None
        except Exception:
            found = False
        ver = ""
        if found and key == "gmsh":
            try:
                import gmsh
                ver = getattr(gmsh, "__version__", "")
            except Exception:
                pass
        if found and key == "pyside6":
            try:
                import PySide6
                ver = PySide6.__version__
            except Exception:
                pass
        d[key] = {"available": found, "version": ver, "note": "" if found else f"{label} not installed"}
    try:
        from opencae.mbd import backend as _mbd
        mj_ok, mj_ver = _mbd.available()[0], _mbd.version()
    except Exception:
        mj_ok, mj_ver = False, ""
    d["mujoco"] = {"available": mj_ok, "version": mj_ver,
                   "note": "" if mj_ok else "not installed \u2014 Motion studies unavailable; install with: "
                                            "python -m pip install mujoco"}
    try:
        web = importlib.util.find_spec("PySide6.QtWebEngineWidgets") is not None
    except Exception:
        web = False
    d["qtwebengine"] = {"available": web, "note": "" if web else "the app will open the dashboard in your browser"}
    home = Path(os.environ.get("OPENCAE_HOME") or Path.home())
    try:
        d["free_disk_gb"] = round(shutil.disk_usage(home if home.exists() else Path.home()).free / 1e9, 1)
    except Exception:
        d["free_disk_gb"] = None
    return d


def cmd_doctor(args) -> int:
    rep = doctor_report()

    def human(o):
        for k, v in o.items():
            if k == "free_disk_gb":
                print(f"{'disk free':<12} {v} GB")
            else:
                s = "ok     " if v.get("available") else "MISSING"
                print(f"{k:<12} {s} {v.get('version', '')} {v.get('path', '')} {v.get('note', '')}".rstrip())
    _emit(args, rep, human)
    return EXIT_OK


# ------------------------------------------------------------------------------------------------ main
class _Parser(argparse.ArgumentParser):
    """Argument errors exit 2 and, with --json on the command line, print ``{"error": ...}`` on stdout."""
    argv: list = []

    def error(self, message):
        if "--json" in _Parser.argv:
            print(json.dumps({"error": message}))
        super().error(message)


def build_parser() -> argparse.ArgumentParser:
    ap = _Parser(prog="opencae", description="OpenCAE drone projects: simulate and iterate a "
                                 "design from a CAD file and drone.json. Results plan tests; they are not flight "
                                 "approval.")
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="COMMAND")

    def common(p, project=True):
        if project:
            p.add_argument("dir", help="project folder (contains drone.json)")
        p.add_argument("--json", action="store_true", help="print one JSON object on stdout (progress on stderr)")

    p = sub.add_parser("new", help="create a project from a CAD file (STEP, STL, OBJ)")
    p.add_argument("dir")
    p.add_argument("--cad", required=True)
    p.add_argument("--name", default="")
    p.add_argument("--type", default="multirotor", choices=["multirotor", "fixed_wing", "tailsitter", "vtol"])
    common(p, False)
    p.set_defaults(fn=cmd_new)

    p = sub.add_parser("run", help="run the analyses (cached ones are skipped)")
    common(p)
    p.add_argument("--only", default="", help="comma list: mass,propulsion,structure,cfd,flight")
    p.add_argument("--set", action="append", default=[], metavar="KEY=VALUE", help="override a parameter (repeatable)")
    p.add_argument("--variant", default="baseline")
    p.add_argument("--keep-cases", action="store_true", help="keep solver scratch folders")
    p.add_argument("--force", action="store_true", help="ignore the cache and recompute")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("iterate", help="run a named design variant with parameter changes")
    common(p)
    p.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    p.add_argument("--name", required=True)
    p.add_argument("--only", default="")
    p.set_defaults(fn=cmd_iterate)

    p = sub.add_parser("status", help="what has been run and what is stale")
    common(p)
    p.set_defaults(fn=cmd_status)

    p = sub.add_parser("dashboard", help="serve the dashboard and open it in the browser")
    common(p)
    p.add_argument("--port", type=int, default=0)
    p.add_argument("--no-browser", action="store_true")
    p.set_defaults(fn=cmd_dashboard)

    p = sub.add_parser("clean", help="delete solver scratch (and, with --results, results)")
    common(p)
    p.add_argument("--results", action="store_true")
    p.set_defaults(fn=cmd_clean)

    p = sub.add_parser("setup", help="show the solvers, or install SU2 / OpenFOAM / MuJoCo (open source)")
    p.add_argument("target", nargs="?", choices=["su2", "openfoam", "mujoco"], help="what to set up (omit for the status)")
    p.add_argument("--yes", action="store_true", help="really download it (without this only the plan is shown)")
    common(p, False)
    p.set_defaults(fn=cmd_setup)

    p = sub.add_parser("doctor", help="check which solvers and libraries are available")
    common(p, False)
    p.set_defaults(fn=cmd_doctor)
    return ap


def main(argv: Optional[list] = None) -> int:
    _Parser.argv = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(argv)
    as_json = getattr(args, "json", False)

    def fail(msg: str, code: int) -> int:
        if as_json:
            print(json.dumps({"error": msg}))
        _err(f"error: {msg}")
        return code

    try:
        return args.fn(args)
    except BadInput as e:
        return fail(str(e), EXIT_BAD)
    except ImportError as e:
        return fail(f"missing module: {e.name or e}", EXIT_BAD)
    except (ValueError, KeyError, OSError) as e:  # bad --set syntax, unreadable CAD, missing file, bad drone.json
        return fail(str(e).strip("'\"") or type(e).__name__, EXIT_BAD)
    except KeyboardInterrupt:
        raise
    except Exception as e:  # never a raw traceback: an unexpected failure is a solver-side error
        return fail(f"{type(e).__name__}: {e}", EXIT_SOLVER)


if __name__ == "__main__":
    sys.exit(main())
