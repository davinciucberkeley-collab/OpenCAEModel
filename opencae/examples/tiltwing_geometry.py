"""Geometry of the Tilt-Wing Designer aircraft (Cakir & Kurtulus 2022 configuration) for OpenCAE.

Built from the designer's own construction (``buildAircraft`` / ``size()`` in the Tilt-Wing Designer
artifact), at the paper's default values stored in ``tiltwing_designer.json``. Axes as in the
designer's CAD: X aft along the fuselage (nose at 0), Y up, Z to starboard. Lengths in mm.

* airframe (cruise, tilt 0): lathe-turned fuselage, lofted S1223 wing halves on top of the fuselage
  (inner end 0.55 d_f from the centre line, clear of the fuselage as in the paper's Fig. 3), lofted
  NACA 0009 tail halves, spar stubs through the fuselage, nacelles with spinners at the rotor stations.
  Canopy, landing legs and propellers are left out (the designer marks the first two as placeholders;
  propellers are not modelled in an unpowered cruise CFD).
* wing spar (structural model): CF tube from the tilt bearing to the tip in three segments, with
  aluminium motor clamp rings at the two rotor stations.
* tilt-bearing bracket: the designer's F8 plate (W x H x t) with the flanged-bearing bore; the bore is
  made of two half-cylinder faces so a bearing load can act on one half.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from OCP.BRep import BRep_Builder
from OCP.BRepAlgoAPI import BRepAlgoAPI_Common, BRepAlgoAPI_Cut, BRepAlgoAPI_Fuse
from OCP.BRepBuilderAPI import (BRepBuilderAPI_MakeEdge, BRepBuilderAPI_MakeFace, BRepBuilderAPI_MakePolygon,
                                BRepBuilderAPI_MakeWire)
from OCP.BRepOffsetAPI import BRepOffsetAPI_ThruSections
from OCP.BRepPrimAPI import (BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCone, BRepPrimAPI_MakeCylinder,
                             BRepPrimAPI_MakePrism, BRepPrimAPI_MakeRevol)
from OCP.GC import GC_MakeArcOfCircle
from OCP.GeomAPI import GeomAPI_PointsToBSpline
from OCP.IFSelect import IFSelect_RetDone
from OCP.Interface import Interface_Static
from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer
from OCP.TopoDS import TopoDS_Compound
from OCP.gp import gp_Ax1, gp_Ax2, gp_Dir, gp_Pnt, gp_Vec

try:  # OCP 8
    from OCP.OCP.collections import Array1_gp_Pnt as TColgp_Array1OfPnt  # type: ignore
except ImportError:  # pragma: no cover - OCP 7.x
    from OCP.TColgp import TColgp_Array1OfPnt  # type: ignore

DATA_FILE = Path(__file__).with_name("tiltwing_designer.json")
MM = 1000.0


def designer_data() -> dict:
    return json.loads(DATA_FILE.read_text())


def fuselage_profile(Lf: float, R: float, n: int = 64) -> list[tuple[float, float]]:
    """Designer's fuselageProfile: elliptic nose to 32 %, cylinder to 58 %, tapered tail cone."""
    pts = []
    for i in range(n + 1):
        t = i / n
        if t < 0.32:
            r = R * np.sqrt(max(0.0, 1 - ((0.32 - t) / 0.32) ** 2))
        elif t < 0.58:
            r = R
        else:
            r = R * (1 - 0.72 * ((t - 0.58) / 0.42) ** 1.3)
        pts.append((t * Lf, max(r, 0.0)))
    return pts


def _bspline(points) -> object:
    arr = TColgp_Array1OfPnt(1, len(points))
    for i, p in enumerate(points):
        arr.SetValue(i + 1, gp_Pnt(*map(float, p)))
    return BRepBuilderAPI_MakeEdge(GeomAPI_PointsToBSpline(arr, 3, 8).Curve()).Edge()


def _section_wire(coords, xle: float, y0: float, z: float, c: float):
    """Airfoil section (unit coords, TE -> upper -> LE -> lower -> TE) as a wire at span station z."""
    c2 = np.asarray(coords, float)
    if np.linalg.norm(c2[0] - c2[-1]) < 1e-9:
        c2 = c2[:-1]
    ile = int(np.argmin(c2[:, 0]))
    P = np.column_stack([xle + c * c2[:, 0], y0 + c * c2[:, 1], np.full(len(c2), z)])
    upper, lower = P[:ile + 1], P[ile:]
    mk = BRepBuilderAPI_MakeWire()
    mk.Add(_bspline(upper))
    mk.Add(_bspline(lower))
    if np.linalg.norm(P[-1] - P[0]) > 1e-9:  # open trailing edge (NACA 0009 tables): close it
        mk.Add(BRepBuilderAPI_MakeEdge(gp_Pnt(*P[-1]), gp_Pnt(*P[0])).Edge())
    return mk.Wire()


def lofted_half(coords, z_in, z_tip, xle_root, cr, ct, sweep_deg, y0, side=1):
    """Ruled loft between the inner and tip sections (linear taper and straight leading edge)."""
    t = np.tan(np.radians(sweep_deg))
    half = z_tip

    def chord(a):
        return cr - (cr - ct) * a / half

    ts = BRepOffsetAPI_ThruSections(True, True, 1e-6)
    for a in (z_in, z_tip):
        ts.AddWire(_section_wire(coords, xle_root + a * t, y0, side * a, chord(a)))
    ts.Build()
    return ts.Shape()


def _cyl(p0, axis, r, h):
    return BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(*p0), gp_Dir(*axis)), r, h).Shape()


def _fuse(shapes):
    s = shapes[0]
    for t in shapes[1:]:
        s = BRepAlgoAPI_Fuse(s, t).Shape()
    return s


def _nacelle(x_spinner_end, y, z, length, rad):
    """Designer's addRotor: tapered cylinder (rad at the front, 0.8 rad at the back) + spinner cone."""
    body = BRepPrimAPI_MakeCone(gp_Ax2(gp_Pnt(x_spinner_end, y, z), gp_Dir(1, 0, 0)), rad, 0.8 * rad, length).Shape()
    spin = BRepPrimAPI_MakeCone(gp_Ax2(gp_Pnt(x_spinner_end, y, z), gp_Dir(-1, 0, 0)), rad, 1e-3 * rad,
                                2.2 * rad).Shape()
    return _fuse([body, spin])


def geometry(d: dict | None = None) -> dict:
    """All dimensions (mm) of the airframe, from the designer's sizing output."""
    d = d or designer_data()
    p, r = d["p"], d["r"]
    g = {k: r[k] * MM for k in ("cr", "ct", "xw", "xLEroot", "xt", "bh", "crh", "cth", "xcg", "MAC")}
    g.update(span=p["span"] * MM, Lf=p["Lf"] * MM, df=p["df"] * MM, sweep=r["sweepLE"], Df=r["Df"] * MM,
             Dr=r["Dr"] * MM)
    g["yw"], g["yt"] = 0.42 * g["df"], 0.12 * g["df"]
    g["z_in"] = 0.55 * g["df"]
    g["z_tail_in"] = 0.25 * g["df"]
    g["xth"] = g["xt"] - 0.3 * g["crh"]
    g["rotor_z"] = [0.2 * g["span"], 0.5 * g["span"]]
    return g


def airframe_half(d: dict | None = None):
    """Starboard half (z >= 0) of the cruise airframe as one solid, for the symmetry-plane CFD model."""
    d = d or designer_data()
    g = geometry(d)
    prof = fuselage_profile(g["Lf"], g["df"] / 2, 96)
    # fuselage: revolve the profile (closed along the axis) about X
    poly = BRepBuilderAPI_MakePolygon()
    for x, rr in [(0.0, 0.0)] + prof[1:] + [(g["Lf"], 0.0)]:
        poly.Add(gp_Pnt(x, max(rr, 0.0) if x > 0 else 0.0, 0.0))
    poly.Close()
    face = BRepBuilderAPI_MakeFace(poly.Wire(), True).Face()
    fus = BRepPrimAPI_MakeRevol(face, gp_Ax1(gp_Pnt(0, 0, 0), gp_Dir(1, 0, 0)), 2 * np.pi).Shape()
    wing = lofted_half(d["airfoils"]["s1223"], g["z_in"], g["span"] / 2, g["xLEroot"], g["cr"], g["ct"],
                       g["sweep"], g["yw"])
    tail = lofted_half(d["airfoils"]["naca0009"], g["z_tail_in"], g["bh"] / 2, g["xth"], g["crh"], g["cth"], 5.0,
                       g["yt"])
    spar = _cyl((g["xw"], g["yw"], -1.0), (0, 0, 1), 12.0, 0.6 * g["df"] + 1.0)  # designer: r 12 mm, 1.2 d_f
    tspar = _cyl((g["xt"], g["yt"], -1.0), (0, 0, 1), 8.0, 0.3 * g["df"] + 1.0)  # r 8 mm, 0.6 d_f
    parts = [fus, wing, tail, spar, tspar]
    tw = np.tan(np.radians(g["sweep"]))
    hb = g["span"] / 2
    for z in g["rotor_z"]:  # wing rotors: nacelle length 0.55 c_r + 0.1 m, radius 28 mm, spinner end 0.45 L ahead of LE
        L = 0.55 * g["cr"] + 100.0
        parts.append(_nacelle(g["xLEroot"] + z * tw - 0.45 * L, g["yw"], z, L, 28.0))
    L = 0.55 * g["crh"] + 60.0  # tail rotor nacelles at the tail tips, radius 20 mm
    zt = g["bh"] / 2
    parts.append(_nacelle(g["xth"] + zt * np.tan(np.radians(5.0)) - 0.45 * L, g["yt"], zt, L, 20.0))
    whole = _fuse(parts)
    big = 5 * g["Lf"]
    keep = BRepPrimAPI_MakeBox(gp_Pnt(-big, -big, 0.0), gp_Pnt(big, big, big)).Shape()
    return BRepAlgoAPI_Common(whole, keep).Shape(), g


def spar_model(d: dict | None = None):
    """Wing spar (one half, starboard) with motor clamp rings: list of (name, solid)."""
    d = d or designer_data()
    g = geometry(d)
    sp = d["sp"]
    D, t = sp["sparD"], sp["sparT"]
    x, y = g["xw"], g["yw"]
    z0, z1 = g["z_in"], g["span"] / 2
    cuts = [z0, 400.0, 700.0, z1]
    out = []
    for i in range(3):
        tube = BRepAlgoAPI_Cut(_cyl((x, y, cuts[i]), (0, 0, 1), D / 2, cuts[i + 1] - cuts[i]),
                               _cyl((x, y, cuts[i] - 1), (0, 0, 1), D / 2 - t, cuts[i + 1] - cuts[i] + 2)).Shape()
        out.append((f"Spar segment {i + 1}", tube))
    w = 30.0
    for k, z in enumerate(g["rotor_z"]):
        zs = min(z - w / 2, z1 - w)
        ring = BRepAlgoAPI_Cut(_cyl((x, y, zs), (0, 0, 1), D / 2 + 5.0, w),
                               _cyl((x, y, zs - 1), (0, 0, 1), D / 2, w + 2)).Shape()
        out.append(("Motor clamp " + ("inboard" if k == 0 else "tip"), ring))
    return out


def bracket(d: dict | None = None, W: float = 90.0, H: float = 70.0, t: float = 4.0):
    """Tilt-bearing plate (designer F8): bottom edge bolted, bore of the flanged bearing at 0.58 H."""
    d = d or designer_data()
    g = geometry(d)
    Dh = d["bracket"]["cruise"]["Dh"]
    R = Dh / 2
    xc, yc = g["xw"], g["yw"]
    y0 = yc - 0.58 * H
    z0 = 0.4 * g["df"] - t / 2  # two plates 0.8 d_f apart (designer layout estimate): starboard one
    pts = [(xc - W / 2, y0), (xc + W / 2, y0), (xc + W / 2, y0 + H), (xc - W / 2, y0 + H)]
    poly = BRepBuilderAPI_MakePolygon()
    for a, b in pts:
        poly.Add(gp_Pnt(a, b, z0))
    poly.Close()
    outer = BRepBuilderAPI_MakeFace(poly.Wire(), True).Face()
    plate = BRepPrimAPI_MakePrism(outer, gp_Vec(0, 0, t)).Shape()
    # bore from two half arcs -> two half-cylinder faces (upper / lower)
    a1 = GC_MakeArcOfCircle(gp_Pnt(xc + R, yc, z0 - 1), gp_Pnt(xc, yc + R, z0 - 1), gp_Pnt(xc - R, yc, z0 - 1)).Value()
    a2 = GC_MakeArcOfCircle(gp_Pnt(xc - R, yc, z0 - 1), gp_Pnt(xc, yc - R, z0 - 1), gp_Pnt(xc + R, yc, z0 - 1)).Value()
    wire = BRepBuilderAPI_MakeWire(BRepBuilderAPI_MakeEdge(a1).Edge(), BRepBuilderAPI_MakeEdge(a2).Edge()).Wire()
    disk = BRepBuilderAPI_MakeFace(wire, True).Face()
    plug = BRepPrimAPI_MakePrism(disk, gp_Vec(0, 0, t + 2)).Shape()
    return BRepAlgoAPI_Cut(plate, plug).Shape(), {"W": W, "H": H, "t": t, "Dh": Dh, "xc": xc, "yc": yc, "z0": z0,
                                                    "y0": y0}


def write_step(shapes: list, path: Path) -> Path:
    comp = TopoDS_Compound()
    bld = BRep_Builder()
    bld.MakeCompound(comp)
    for s in shapes:
        bld.Add(comp, s)
    Interface_Static.SetCVal_s("write.step.unit", "MM")
    w = STEPControl_Writer()
    w.Transfer(comp if len(shapes) > 1 else shapes[0], STEPControl_AsIs)
    if w.Write(str(path)) != IFSelect_RetDone:
        raise RuntimeError(f"STEP export to {path} failed")
    return Path(path)
