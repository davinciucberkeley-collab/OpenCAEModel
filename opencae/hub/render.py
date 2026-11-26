"""Server-rendered PNG contours: the fallback for a browser (or an app window) without WebGL.

``render_png(pid, run, field, view)`` draws one stored field of a run off-screen with pyvista (colour bar, title,
units). ``field`` is a field id of ``adapters.scene`` or ``slices`` (the flow slices, coloured by speed) or
``streamlines``. When there is nothing to draw (a failed solver run, a run whose fields were not kept) the image is
a plain message that says why, never an error page, so an ``<img>`` always shows something readable.
"""
from __future__ import annotations

import base64
import io
import textwrap
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Optional

import numpy as np

from opencae.hub import HubError, adapters, registry

VIEWS = ("iso", "top", "front", "side")
SIZE = (960, 600)
_CACHE: "OrderedDict[tuple, bytes]" = OrderedDict()
_LOCK = threading.Lock()  # one off-screen render at a time (VTK is not happy with parallel plotters)
# aircraft frame: x forward, y left, z up. (camera direction from the focal point, view-up)
_CAMERAS = {"iso": ((1.0, -1.0, 0.8), (0, 0, 1)), "top": ((0.0, 0.0, 1.0), (1, 0, 0)),
            "front": ((1.0, 0.0, 0.0), (0, 0, 1)), "side": ((0.0, -1.0, 0.0), (0, 0, 1))}
_DIVERGING = ("cp", "p_static", "pressure", "p", "p_total")


def _dec(s: str, dtype: str) -> np.ndarray:
    return np.frombuffer(base64.b64decode(s), dtype=dtype)


def _stamp(entry: dict) -> tuple:
    p = Path(entry["path"])
    out = []
    try:
        out.append(p.stat().st_mtime_ns)
        if p.is_dir() and (p / "results").is_dir():
            r = p / "results"
            out.append(r.stat().st_mtime_ns)
            out += [d.stat().st_mtime_ns for d in r.iterdir() if d.is_dir()]
    except OSError:
        pass
    return tuple(out)


def message_png(text: str, title: str = "Nothing to draw") -> bytes:
    """A PNG with a short message (used when a contour cannot be drawn)."""
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", SIZE, (245, 246, 248))
    d = ImageDraw.Draw(img)
    f = ImageFont.load_default()
    d.rectangle([0, 0, SIZE[0], 46], fill=(31, 41, 55))
    d.text((18, 16), "OpenCAE Results Hub  |  " + title, fill=(255, 255, 255), font=f)
    y = 76
    for para in str(text).splitlines() or [""]:
        for line in textwrap.wrap(para, 110) or [""]:
            d.text((24, y), line, fill=(31, 41, 55), font=f)
            y += 18
        if y > SIZE[1] - 40:
            break
    d.text((24, SIZE[1] - 28), "Simulation results plan the tests; they are not flight approval.",
           fill=(107, 114, 128), font=f)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _poly(pv, points, tris):
    tris = np.asarray(tris, dtype=np.int64).reshape(-1, 3)
    return pv.PolyData(np.asarray(points, dtype=np.float64).reshape(-1, 3),
                       np.hstack([np.full((len(tris), 1), 3, np.int64), tris]).ravel())


def _context_parts(sc: dict, pts: np.ndarray) -> list:
    """The parts worth drawing behind a field: not much larger than the field itself (a flow-domain box is not)."""
    diag = float(np.linalg.norm(np.ptp(pts, axis=0))) or 1.0
    out = []
    for part in sc["parts"]:
        P = _dec(part["points"], "<f4").reshape(-1, 3)
        if len(P) and float(np.linalg.norm(np.ptp(P, axis=0))) <= 4 * diag:
            out.append(part)
    return out


def _draw(sc: dict, field: str, view: str, title: str) -> bytes:
    import pyvista as pv
    # Never set pyvista's global OFF_SCREEN: inside the app it made every 3D view created afterwards come up
    # without an interactor (AttributeError on add_observer, a crashed window). The plotter alone is off-screen.
    pl = pv.Plotter(off_screen=True, window_size=list(SIZE))
    pl.set_background("white")
    label, unit, bounds_pts = field, "", []
    try:
        if field == "streamlines":
            sl = sc["streamlines"]
            P = _dec(sl["points"], "<f4").reshape(-1, 3).astype(np.float64)
            off = _dec(sl["offsets"], "<i4")
            v = _dec(sl["values"], "<f4")
            lines = []
            for a, b in zip(off[:-1], off[1:]):
                lines += [b - a, *range(a, b)]
            pd = pv.PolyData(P)
            pd.lines = np.asarray(lines, dtype=np.int64)
            pd.point_data["v"] = v
            label, unit = "Streamlines, speed", sl.get("unit", "m/s")
            for part in sc["parts"]:
                pl.add_mesh(_poly(pv, _dec(part["points"], "<f4"), _dec(part["triangles"], "<i4")),
                            color="#9aa5b1", opacity=0.55)
            pl.add_mesh(pd, scalars="v", cmap="turbo", line_width=2, render_lines_as_tubes=True,
                        scalar_bar_args={"title": f"{label} [{unit}]", "color": "black", "title_font_size": 12, "label_font_size": 11})
            bounds_pts = [P]
        elif field == "slices":
            if not sc["slices"]:
                raise LookupError("This run has no flow slices.")
            lo = min(float(_dec(s["values"], "<f4").min()) for s in sc["slices"])
            hi = max(float(_dec(s["values"], "<f4").max()) for s in sc["slices"])
            for part in sc["parts"]:
                pl.add_mesh(_poly(pv, _dec(part["points"], "<f4"), _dec(part["triangles"], "<i4")),
                            color="#9aa5b1", opacity=0.7)
            for i, s in enumerate(sc["slices"]):
                P = _dec(s["points"], "<f4").reshape(-1, 3)
                m = _poly(pv, P, _dec(s["triangles"], "<i4"))
                m.point_data["v"] = _dec(s["values"], "<f4")
                pl.add_mesh(m, scalars="v", cmap="turbo", clim=[lo, hi], show_scalar_bar=i == 0,
                            scalar_bar_args={"title": f"Flow speed [{s.get('unit', 'm/s')}]", "color": "black", "title_font_size": 12, "label_font_size": 11})
                bounds_pts.append(P)
            label, unit = "Flow slices, speed", "m/s"
        else:
            f = next((x for x in sc["fields"] if x["id"] == field), None)
            if f is None:
                raise LookupError(f"No field '{field}' in this run.")
            P = _dec(f["points"], "<f4").reshape(-1, 3)
            m = _poly(pv, P, _dec(f["triangles"], "<i4"))
            m.point_data["v"] = _dec(f["values"], "<f4")
            label, unit = f["label"], f["unit"]
            if f["discipline"] != "cfd":
                for part in _context_parts(sc, P):
                    pl.add_mesh(_poly(pv, _dec(part["points"], "<f4"), _dec(part["triangles"], "<i4")),
                                color="#c9d1d9", opacity=0.35)
            cmap = "coolwarm" if (f.get("quantity") in _DIVERGING or "pressure" in label.lower()) else "turbo"
            clim = [f["min"], f["max"]] if f["min"] is not None and f["max"] is not None and f["max"] > f["min"] else None
            pl.add_mesh(m, scalars="v", cmap=cmap, clim=clim, smooth_shading=True,
                        scalar_bar_args={"title": f"{label} [{unit}]" if unit else label, "color": "black", "title_font_size": 12, "label_font_size": 11,
                                         "vertical": False, "width": 0.5, "position_x": 0.25, "position_y": 0.04})
            bounds_pts = [P]
        direction, up = _CAMERAS[view]
        allp = np.vstack(bounds_pts)  # frame the drawn field, not a huge context body (a flow domain box)
        ctr = (allp.min(0) + allp.max(0)) / 2
        r = float(np.linalg.norm(np.ptp(allp, axis=0))) or 1.0
        d = np.asarray(direction, float)
        pl.camera.focal_point = ctr
        pl.camera.position = ctr + d / np.linalg.norm(d) * r * 2.0
        pl.camera.up = up
        lo, hi = allp.min(0), allp.max(0)
        pl.reset_camera(bounds=[lo[0], hi[0], lo[1], hi[1], lo[2], hi[2]])  # keeps the direction, fits the field
        pl.camera.up = up
        pl.reset_camera_clipping_range()
        pl.add_text(f"{title}\n{label}" + (f" [{unit}]" if unit else "") + f"  |  view: {view}", position="upper_left",
                    font_size=9, color="black")
        img = pl.screenshot(None, return_img=True)
    finally:
        pl.close()
    from PIL import Image
    buf = io.BytesIO()
    Image.fromarray(np.asarray(img)[:, :, :3]).save(buf, "PNG")
    return buf.getvalue()


def _inside_gui_process() -> bool:
    """True when this process runs the Qt application (the hub served by the app's own server). VTK off-screen
    rendering from the server thread would then share the OpenGL driver with the app's 3D views on another
    thread, which is not safe on Windows; the drawing goes to a short-lived child process instead."""
    import os
    import sys
    if os.environ.get("OPENCAE_HUB_RENDER_INPROC") == "1":
        return False
    qtw = sys.modules.get("PySide6.QtWidgets")
    return bool(qtw is not None and qtw.QApplication.instance() is not None)


def _draw_in_subprocess(pid: str, run: str, field: str, view: str, timeout: float = 180.0) -> bytes:
    import os
    import subprocess
    import sys
    import tempfile
    with tempfile.TemporaryDirectory(prefix="opencae-render-") as d:
        out = Path(d) / "out.png"
        env = dict(os.environ, OPENCAE_HUB_RENDER_INPROC="1")
        env.pop("QT_QPA_PLATFORM", None) if sys.platform == "win32" else None
        root = str(Path(__file__).resolve().parents[2])
        env["PYTHONPATH"] = root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        kw = {"creationflags": 0x08000000} if os.name == "nt" else {}  # CREATE_NO_WINDOW
        r = subprocess.run([sys.executable, "-m", "opencae.hub.render", pid, run, field, view, str(out)], env=env,
                           capture_output=True, text=True, timeout=timeout, **kw)
        if r.returncode != 0 or not out.exists():
            raise RuntimeError(f"renderer process failed ({r.returncode}): {(r.stderr or '')[-400:]}")
        return out.read_bytes()


def render_png(pid: str, run: str = "latest", field: str = "", view: str = "iso", strict: bool = False) -> bytes:
    """PNG bytes of one field of a run. ``strict=True`` raises instead of returning a message image when the
    drawing fails (the tests use it to be sure the real renderer ran)."""
    if view not in VIEWS:
        raise HubError(f"view must be one of {', '.join(VIEWS)}", 400)
    e = registry.find(pid)
    if e is None:
        raise HubError(f"Unknown project '{pid}'", 404)
    key = (pid, run or "latest", field, view, _stamp(e))
    with _LOCK:
        if key in _CACHE:
            _CACHE.move_to_end(key)
            return _CACHE[key]
    try:
        sc = adapters.scene(pid, run or "latest")
    except HubError:
        raise
    except Exception as exc:  # noqa: BLE001 - an <img> always gets a readable image
        if strict:
            raise
        return message_png(f"The stored results could not be read: {type(exc).__name__}: {exc}", "Render failed")
    notes = " ".join(sc.get("notes") or [])
    field = field or (sc["fields"][0]["id"] if sc["fields"] else "")
    if not field:
        return message_png("This run has no contour field to draw.\n" + notes, "No contours")
    try:
        with _LOCK:
            if _inside_gui_process():
                data = _draw_in_subprocess(pid, run or "latest", field, view)
            else:
                data = _draw(sc, field, view, f"{e['name']}  |  run {run or 'latest'}")
    except LookupError as exc:
        if strict:
            raise
        return message_png(f"{exc}\n{notes}", "Nothing to draw")
    except Exception as exc:  # noqa: BLE001 - the page still gets a readable image
        if strict:
            raise
        return message_png(f"The contour could not be drawn on the server: {type(exc).__name__}: {exc}",
                           "Render failed")
    with _LOCK:
        _CACHE[key] = data
        while len(_CACHE) > 24:
            _CACHE.popitem(last=False)
    return data


def _main(argv=None) -> int:
    """python -m opencae.hub.render PID RUN FIELD VIEW OUT.png (used by _draw_in_subprocess)."""
    import sys
    pid, run, field, view, out = (argv if argv is not None else sys.argv[1:])[:5]
    Path(out).write_bytes(render_png(pid, run, field, view, strict=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
