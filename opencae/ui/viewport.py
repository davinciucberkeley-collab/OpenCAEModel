"""Interactive 3-D viewport (PyVista/VTK embedded in Qt).

Mouse: left-drag rotate, middle-drag (or shift+left) pan, wheel zoom,
left-click pick (in the active selection mode), ctrl+click add/remove from
the selection. Results mode: click probes the nodal value.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np
import pyvista as pv
import vtk
from pyvistaqt import QtInteractor
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget

from opencae.geometry.kernel import Body
from opencae.meshing.mesh_data import FEMesh
from opencae.ui import theme
from opencae.visualization import scene

BODY_COLORS = ["#8fb3d9", "#c9b37e", "#9cc9a1", "#c99a9a", "#a99cc9", "#9cc4c9"]
CONSTRAINT_COLORS = {"fixed": "#3d7fd1", "displacement": "#2fb5a8", "symmetry": "#9a6fe0", "temperature": "#e0457b",
                     "wall": "#8a8f98", "slip_wall": "#7fc8f8"}
LOAD_COLORS = {"force": "#ef5b55", "remote_force": "#ff8a65", "moment": "#d64fd1", "pressure": "#f0a03c", "gravity": "#5cc98a",
               "heat_flux": "#ff7a1a", "heat_power": "#ff7a1a", "convection": "#4aa8ff", "radiation": "#c9b11d",
               "heat_generation": "#ff3d3d", "uniform_temperature": "#e0457b", "velocity_inlet": "#3fb6ff",
               "pressure_outlet": "#2fd08a"}


def _surface(grid):
    try:  # pyvista >= 0.47 announces a default change for this filter
        return grid.extract_surface(nonlinear_subdivision=1, algorithm="dataset_surface")
    except TypeError:
        return grid.extract_surface(nonlinear_subdivision=1)


@dataclass
class Pick:
    kind: str  # body | face | edge | vertex | node | element
    body: str = ""
    index: int = -1
    point: Optional[np.ndarray] = None
    additive: bool = False

    def key(self):
        return (self.kind, self.body, self.index)


@dataclass
class _Pickable:
    kind: str  # faces | edges | vertices | mesh | results
    body: str
    data: pv.PolyData


class Viewport(QWidget):
    picked = Signal(object)  # Pick | None
    status = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.plotter = QtInteractor(self, auto_update=False)
        lay.addWidget(self.plotter.interactor)
        self.plotter.set_background(theme.VIEW_BG_BOTTOM, top=theme.VIEW_BG_TOP)
        try:
            self.plotter.enable_anti_aliasing("fxaa")
        except Exception:
            pass
        self.plotter.add_axes(line_width=3, color="white", xlabel="X", ylabel="Y", zlabel="Z")
        self._orientation_widget = None
        try:
            self._orientation_widget = self.plotter.add_camera_orientation_widget()
        except Exception:  # pragma: no cover - not available on some VTK builds
            pass
        self.pick_kind = "face"
        self.render_mode = "shaded_edges"
        self.display = "geometry"  # geometry | mesh | results
        self._groups: dict[str, list[str]] = {}
        self._pickables: dict[int, _Pickable] = {}
        self._press_pos: Optional[tuple[int, int]] = None
        self._press_ctrl = False
        self._bs_cache: tuple[int, scene.BoundarySurface] | None = None
        self._res_surface: Optional[pv.PolyData] = None
        self._res_base: Optional[np.ndarray] = None
        self._res_disp: Optional[np.ndarray] = None
        self._res_edges: Optional[pv.PolyData] = None
        self._anim_timer = QTimer(self)
        self._anim_timer.timeout.connect(self._anim_step)
        self._anim_phase = 0.0
        self._anim_scale = 1.0
        self._scalar_bar_title: Optional[str] = None
        self.model_size = 1.0
        iren = self.plotter.iren
        iren.add_observer("LeftButtonPressEvent", self._on_press)
        iren.add_observer("LeftButtonReleaseEvent", self._on_release)
        self.plotter.view_isometric()

    # ------------------------------------------------------------------ actor groups
    def _add(self, group: str, name: str, obj, **kw):
        actor = self.plotter.add_mesh(obj, name=name, reset_camera=False, **kw)
        self._groups.setdefault(group, []).append(name)
        return actor

    def _add_labels(self, group: str, name: str, points, labels, **kw):
        actor = self.plotter.add_point_labels(points, labels, name=name, reset_camera=False, **kw)
        self._groups.setdefault(group, []).append(name)
        return actor

    def clear_group(self, group: str) -> None:
        for name in self._groups.pop(group, []):
            self.plotter.remove_actor(name, reset_camera=False, render=False)
        if group in ("geometry", "mesh", "results"):
            self._pickables = {k: v for k, v in self._pickables.items() if v.kind != group}
            self._pickables = {k: v for k, v in self._pickables.items()
                               if not (group == "geometry" and v.kind in ("faces", "edges", "vertices"))}
        if group == "results":
            self._remove_scalar_bar()
            self._res_surface = None

    def _remove_scalar_bar(self) -> None:
        for title in list(self.plotter.scalar_bars.keys()):
            try:
                self.plotter.remove_scalar_bar(title, render=False)
            except Exception:
                pass
        self._scalar_bar_title = None

    def clear_all(self) -> None:
        self.stop_animation()
        for g in list(self._groups):
            self.clear_group(g)
        self._pickables.clear()
        self.plotter.render()

    def _register(self, actor, kind: str, body: str, data: pv.PolyData) -> None:
        self._pickables[id(actor)] = _Pickable(kind, body, data)
        actor._opencae_key = id(actor)  # keep a stable handle

    # ------------------------------------------------------------------ geometry
    def show_geometry(self, bodies: dict[str, Body], order: list[str], hidden: set[str]) -> None:
        self.stop_animation()
        for g in ("geometry", "mesh", "results", "highlight", "probe"):
            self.clear_group(g)
        self._pickables.clear()
        self.display = "geometry"
        sizes = []
        for i, bid in enumerate(order):
            b = bodies.get(bid)
            if b is None or bid in hidden:
                continue
            sizes.append(b.size)
            col = BODY_COLORS[i % len(BODY_COLORS)]
            fpd = scene.body_faces_polydata(b)
            if b.kind == "tessellated":
                self._add("geometry", f"g_{bid}_stl", fpd, color="#b8a6c9", show_edges=True, edge_color="#5b4e6b",
                          opacity=0.9, pickable=False)
                continue
            mode = self.render_mode
            face_opacity = 0.12 if mode == "wireframe" else 1.0
            a = self._add("geometry", f"g_{bid}_faces", fpd, color=col, smooth_shading=True, opacity=face_opacity,
                          specular=0.25, diffuse=0.85, ambient=0.15, pickable=True)
            self._register(a, "faces", bid, fpd)
            epd = scene.body_edges_polydata(b)
            if epd is not None and mode in ("shaded_edges", "wireframe"):
                a = self._add("geometry", f"g_{bid}_edges", epd, color="#101318" if mode != "wireframe" else "#d8e1ee",
                              line_width=2.0, pickable=True, render_lines_as_tubes=False)
                self._register(a, "edges", bid, epd)
            elif epd is not None:
                a = self._add("geometry", f"g_{bid}_edges", epd, color="#101318", line_width=1.0, opacity=0.0,
                              pickable=True)
                self._register(a, "edges", bid, epd)
            vpd = scene.body_vertices_polydata(b)
            if vpd is not None:
                show_v = self.pick_kind == "vertex"
                a = self._add("geometry", f"g_{bid}_verts", vpd, color="#f2d14c", point_size=9 if show_v else 1,
                              render_points_as_spheres=True, opacity=1.0 if show_v else 0.0, pickable=True)
                self._register(a, "vertices", bid, vpd)
        if sizes:
            self.model_size = max(sizes)
        self._apply_pickability()
        self.plotter.render()

    # ------------------------------------------------------------------ boundary condition glyphs
    def show_bc_glyphs(self, body: Optional[Body], constraints: list[tuple[str, list[int], list[int]]],
                       loads: list[dict], extra: Optional[list] = None) -> None:
        """Glyphs for supports / conditions and loads. ``extra``: further (body, constraints, loads)
        triples for multi-body studies. constraints: (kind, face indices, vertex indices);
        loads: dicts with kind, faces, vertices, dir, value, cs."""
        self.clear_group("bc")
        items = ([(body, constraints, loads)] if body is not None else []) + list(extra or [])
        items = [it for it in items if it[0] is not None and it[0].kind == "solid"]
        if not items:
            self.plotter.render()
            return
        lo = np.min([it[0].bbox[0] for it in items], axis=0)
        hi = np.max([it[0].bbox[1] for it in items], axis=0)
        L = float(np.linalg.norm(hi - lo)) or 1.0
        for bi, (body, cons, lds) in enumerate(items):
            self._bc_glyphs_for(f"b{bi}", body, cons, lds, L)
        self.plotter.render()

    def _edge_lines(self, tag: str, body: Body, edges: list[int], color: str) -> None:
        polys = [body.tessellation.edge_polylines[i] for i in edges
                 if i < len(body.tessellation.edge_polylines) and len(body.tessellation.edge_polylines[i]) > 1]
        if polys:
            self._add("bc", tag, pv.MultipleLines(np.vstack(polys)) if len(polys) == 1 else
                      pv.merge([pv.MultipleLines(p) for p in polys]), color=color, line_width=6, pickable=False)

    def _bc_glyphs_for(self, tag: str, body: Body, constraints, loads, L: float) -> None:
        for k, item in enumerate(constraints):
            kind, faces, verts = item[:3]
            col = CONSTRAINT_COLORS.get(kind, "#3d7fd1")
            if len(item) > 3 and item[3]:
                self._edge_lines(f"bc_ce{tag}{k}", body, item[3], col)
            sub = scene.face_subset(body, faces)
            if sub is not None:
                a = self._add("bc", f"bc_c{tag}{k}", sub, color=col, opacity=0.9, pickable=False)
                a.GetMapper().SetResolveCoincidentTopologyToPolygonOffset()
                a.GetMapper().SetRelativeCoincidentTopologyPolygonOffsetParameters(-2, -2)
                if kind not in ("temperature", "wall", "slip_wall"):
                    centers, normals = scene.face_glyph_points(sub, 24)
                    pts = pv.PolyData(centers + normals * 0.035 * L)
                    pts["vec"] = -normals
                    cone = pv.Cone(direction=(1, 0, 0), height=1.0, radius=0.45, resolution=12)
                    g = pts.glyph(orient="vec", scale=False, factor=0.035 * L, geom=cone)
                    self._add("bc", f"bc_cg{tag}{k}", g, color=col, pickable=False)
            for j, v in enumerate(verts):
                p = body.vertices[v].point
                self._add("bc", f"bc_cv{tag}{k}_{j}", pv.Sphere(radius=0.015 * L, center=p), color=col,
                          pickable=False)
        for k, ld in enumerate(loads):
            kind = ld["kind"]
            col = LOAD_COLORS.get(kind, "#ef5b55")
            if ld.get("edges"):
                self._edge_lines(f"bc_le{tag}{k}", body, ld["edges"], col)
            if kind in ("gravity", "heat_generation", "uniform_temperature"):
                c = 0.5 * (body.bbox[0] + body.bbox[1])
                if kind == "gravity":
                    d = np.asarray(ld["dir"], float)
                    arr = pv.Arrow(start=c - d * 0.25 * L, direction=d, scale=0.25 * L, tip_length=0.3,
                                   shaft_radius=0.04)
                    self._add("bc", f"bc_l{tag}{k}", arr, color=col, pickable=False)
                else:
                    self._add("bc", f"bc_l{tag}{k}", pv.Sphere(radius=0.03 * L, center=c), color=col, opacity=0.8,
                              pickable=False)
                continue
            sub = scene.face_subset(body, ld.get("faces", []))
            if kind == "remote_force":
                P = np.asarray(ld.get("point", [0, 0, 0]), float)
                d = np.asarray(ld["dir"], float)
                d = d / max(np.linalg.norm(d), 1e-30)
                self._add("bc", f"bc_l{tag}{k}", pv.Arrow(start=P - d * 0.15 * L, direction=d, scale=0.15 * L,
                                                         tip_length=0.3, shaft_radius=0.04), color=col, pickable=False)
                self._add("bc", f"bc_lp{tag}{k}", pv.Sphere(radius=0.012 * L, center=P), color=col, pickable=False)
                if sub is not None:
                    c = np.asarray(sub.points).mean(axis=0)
                    self._add("bc", f"bc_ll{tag}{k}", pv.Line(c, P), color=col, line_width=2, pickable=False)
                    a = self._add("bc", f"bc_lf{tag}{k}", sub, color=col, opacity=0.45, pickable=False)
                    a.GetMapper().SetResolveCoincidentTopologyToPolygonOffset()
                continue
            if kind == "moment":
                if sub is None:
                    continue
                c = np.asarray(sub.points).mean(axis=0)
                d = np.asarray(ld["dir"], float)
                d = d / max(np.linalg.norm(d), 1e-30) * (1 if ld.get("value", 1.0) >= 0 else -1)
                for j, off in enumerate((0.0, 0.07)):  # double-headed arrow = moment vector (right-hand rule)
                    arr = pv.Arrow(start=c + d * off * L, direction=d, scale=0.18 * L, tip_length=0.25,
                                   tip_radius=0.1, shaft_radius=0.03)
                    self._add("bc", f"bc_l{tag}{k}_{j}", arr, color=col, pickable=False)
                continue
            pts_list, dirs_list = [], []
            if sub is not None:
                centers, normals = scene.face_glyph_points(sub, 30)
                pts_list.append(centers)
                if kind == "pressure":
                    sgn = 1.0 if ld.get("value", 1.0) >= 0 else -1.0
                    dirs_list.append(-sgn * normals)
                elif kind in ("heat_flux", "heat_power"):
                    sgn = 1.0 if ld.get("value", 1.0) >= 0 else -1.0
                    dirs_list.append(-sgn * normals)  # into the body = heat input
                elif kind in ("convection", "radiation", "pressure_outlet"):
                    dirs_list.append(normals)  # heat exchange with the surroundings / outflow
                elif kind == "velocity_inlet":
                    d = ld.get("dir")
                    dirs_list.append(-normals if isinstance(d, str) or d is None else
                                     np.repeat(np.asarray(d, float)[None, :], len(centers), axis=0))
                elif ld.get("cs") == "face_normal":
                    dirs_list.append(normals * np.sign(ld.get("value", 1.0) or 1.0))
                else:
                    dirs_list.append(np.repeat(np.asarray(ld["dir"], float)[None, :], len(centers), axis=0))
            for v in ld.get("vertices", []):
                pts_list.append(body.vertices[v].point[None, :])
                dirs_list.append(np.asarray(ld["dir"], float)[None, :])
            if not pts_list:
                continue
            pts = np.concatenate(pts_list)
            dirs = np.concatenate(dirs_list)
            dirs = dirs / np.maximum(np.linalg.norm(dirs, axis=1, keepdims=True), 1e-30)
            alen = 0.12 * L if kind not in ("convection", "radiation") else 0.07 * L
            src = pv.PolyData(pts - dirs * alen if kind not in ("convection", "radiation") else pts)
            src["vec"] = dirs
            g = src.glyph(orient="vec", scale=False, factor=alen,
                          geom=pv.Arrow(tip_length=0.3, tip_radius=0.12, shaft_radius=0.04))
            self._add("bc", f"bc_l{tag}{k}", g, color=col, pickable=False)
            if sub is not None and kind in ("pressure", "heat_flux", "heat_power", "convection", "radiation",
                                            "velocity_inlet", "pressure_outlet"):
                a = self._add("bc", f"bc_lf{tag}{k}", sub, color=col, opacity=0.55, pickable=False)
                a.GetMapper().SetResolveCoincidentTopologyToPolygonOffset()

    # ------------------------------------------------------------------ mesh
    def _boundary(self, mesh: FEMesh) -> scene.BoundarySurface:
        if self._bs_cache is None or self._bs_cache[0] != id(mesh):
            self._bs_cache = (id(mesh), scene.boundary_surface(mesh))
        return self._bs_cache[1]

    def show_mesh(self, mesh: FEMesh, color_by: str = "none") -> None:
        self.stop_animation()
        for g in ("geometry", "mesh", "results", "highlight", "probe"):
            self.clear_group(g)
        self._pickables.clear()
        self.display = "mesh"
        bs = self._boundary(mesh)
        surf = bs.polydata(mesh.nodes)
        kw = dict(color="#7fa7cf")
        if color_by == "gamma" and "gamma" not in mesh.quality:
            color_by = "sicn"
        if color_by != "none" and color_by in mesh.quality:
            q = mesh.quality[color_by]
            surf.cell_data[color_by] = q[bs.owner]
            titles = {"gamma": "Element quality gamma (1 = ideal)", "sj": "Min scaled Jacobian",
                      "aspect_ratio": "Aspect ratio (1 = ideal)", "size": "Element size (mean edge)",
                      "sicn": "SICN quality"}
            cmap = "RdYlGn" if color_by in ("gamma", "sj", "sicn") else "viridis_r" if color_by == "aspect_ratio" else "viridis"
            kw = dict(scalars=color_by, cmap=cmap, show_scalar_bar=True,
                      scalar_bar_args={"title": titles.get(color_by, color_by), "vertical": True,
                                       "position_x": 0.88, "position_y": 0.25, "height": 0.5, "width": 0.06,
                                       "color": "white", "fmt": "%.3g"})
        a = self._add("mesh", "mesh_surf", surf, smooth_shading=False, pickable=True, **kw)
        self._register(a, "mesh", mesh.body_id, surf)
        if self.render_mode != "shaded":
            self._add("mesh", "mesh_edges", bs.edges_polydata(mesh.nodes), color="#0e1116", line_width=1.0,
                      pickable=False)
        self.model_size = float(np.ptp(mesh.nodes, axis=0).max())
        self._apply_pickability()
        self.plotter.render()

    # ------------------------------------------------------------------ results
    def show_results(self, mesh: FEMesh, values: np.ndarray, displacement: np.ndarray, title: str,
                     scale: float = 1.0, clim: Optional[tuple[float, float]] = None, show_undeformed: bool = True,
                     show_edges: bool = False, cmap: str = "jet", n_colors: int = 12, minmax: bool = True,
                     fmt: str = "%.4g", section: Optional[tuple] = None) -> None:
        """``section``: (origin, normal) - show only the part of the model behind a cutting plane.
        Elements cut by the plane are shown whole (no interpolation), so probed values on the
        section are true nodal values."""
        self.stop_animation()
        for g in ("geometry", "mesh", "results", "highlight", "probe"):
            self.clear_group(g)
        self._pickables.clear()
        self.display = "results"
        bs = self._boundary(mesh)
        if section is not None:
            surf, base, disp = self._section_surface(mesh, values, displacement, scale, *section)
        else:
            surf = bs.polydata(mesh.nodes, displacement, scale)
            surf.point_data["value"] = values[bs.point_ids]
            base, disp = mesh.nodes[bs.point_ids].copy(), displacement[bs.point_ids].copy()
        vmin, vmax = float(values.min()), float(values.max())
        lim = clim if clim is not None else (vmin, vmax)
        if lim[1] <= lim[0]:
            lim = (lim[0] - 1e-30, lim[0] + 1e-30) if lim[0] == 0 else (lim[0] - abs(lim[0]) * 1e-6, lim[0] + abs(lim[0]) * 1e-6)
        extra = {}
        if clim is not None:
            extra = {"above_color": "#d9d9d9", "below_color": "#3a3a3a"}
        a = self._add("results", "res_surf", surf, scalars="value", cmap=cmap, clim=lim, n_colors=n_colors,
                      show_edges=False, smooth_shading=False, pickable=True, show_scalar_bar=True,
                      scalar_bar_args={"title": title, "vertical": True, "position_x": 0.84, "position_y": 0.08,
                                       "height": 0.58, "width": 0.07, "color": "white", "fmt": fmt,
                                       "n_labels": min(n_colors + 1, 9), "title_font_size": 13,
                                       "label_font_size": 12}, **extra)
        self._scalar_bar_title = title
        self._register(a, "results", mesh.body_id, surf)
        self._res_surface = surf
        self._res_base = base
        self._res_disp = disp
        self._anim_scale = scale
        if section is not None:
            self._res_edges = None
            if show_edges:
                self._add("results", "res_edges", surf.extract_all_edges(), color="#111111", line_width=0.6,
                          opacity=0.6, pickable=False)
        elif show_edges:
            self._res_edges = bs.edges_polydata(mesh.nodes, displacement, scale)
            self._add("results", "res_edges", self._res_edges, color="#111111", line_width=0.6, opacity=0.6,
                      pickable=False)
        else:
            self._res_edges = None
        if show_undeformed and scale != 0:
            self._add("results", "res_undeformed", bs.polydata(mesh.nodes), style="wireframe", color="#cfd8e3",
                      opacity=0.12, pickable=False, line_width=0.5)
        if minmax:
            imax, imin = int(np.argmax(values)), int(np.argmin(values))
            P = mesh.nodes + scale * displacement
            self._add_labels("results", "res_minmax", P[[imax, imin]],
                             [f"MAX {values[imax]:.4g}", f"MIN {values[imin]:.4g}"], point_color="#ffffff",
                             point_size=10, text_color="#111111", font_size=13, shape_color="#f2d14c",
                             shape_opacity=0.85, render_points_as_spheres=True, always_visible=True)
        self.model_size = float(np.ptp(mesh.nodes, axis=0).max())
        self._apply_pickability()
        self.plotter.render()

    def _volume_grid(self, mesh: FEMesh) -> pv.UnstructuredGrid:
        key = id(mesh)
        if getattr(self, "_grid_cache", (None,))[0] != key:
            g = pv.from_meshio(mesh.to_meshio())
            g.point_data["node_id"] = np.arange(mesh.n_nodes)
            self._grid_cache = (key, g)
        return self._grid_cache[1]

    def add_flow_overlay(self, mesh: FEMesh, velocity: np.ndarray, vectors: bool = False, streamlines: bool = False,
                         n_vectors: int = 1500, n_seeds: int = 150) -> None:
        """Velocity arrows and/or streamlines (coloured by speed) on top of the current result view."""
        if not (vectors or streamlines):
            return
        g = self._volume_grid(mesh).copy(deep=False)
        g.point_data["V"] = velocity
        speed = np.linalg.norm(velocity, axis=1)
        vmax = float(speed.max()) or 1.0
        L = float(np.ptp(mesh.nodes, axis=0).max()) or 1.0
        rng = np.random.default_rng(0)
        if vectors:
            idx = np.nonzero(speed > 1e-6 * vmax)[0]
            if len(idx) > n_vectors:
                idx = rng.choice(idx, n_vectors, replace=False)
            pts = pv.PolyData(mesh.nodes[idx])
            pts["V"] = velocity[idx]
            pts["speed"] = speed[idx]
            arrows = pts.glyph(orient="V", scale="speed", factor=0.08 * L / vmax,
                               geom=pv.Arrow(tip_length=0.3, tip_radius=0.1, shaft_radius=0.03))
            self._add("results", "flow_vectors", arrows, scalars="speed", cmap="viridis", show_scalar_bar=False,
                      pickable=False)
        if streamlines:
            cells = mesh.corner_elements()
            pick = rng.choice(len(cells), min(n_seeds, len(cells)), replace=False)
            seeds = pv.PolyData(mesh.nodes[cells[pick]].mean(axis=1))
            try:
                sl = g.streamlines_from_source(seeds, vectors="V", integration_direction="both",
                                               max_length=4 * L, initial_step_length=0.01 * L)
            except Exception:  # pragma: no cover - VTK failures must not break the result view
                sl = None
            if sl is not None and sl.n_points:
                sl["speed"] = np.linalg.norm(sl["V"], axis=1)
                self._add("results", "flow_streamlines", sl, scalars="speed", cmap="viridis", line_width=2,
                          show_scalar_bar=False, pickable=False)
        self.plotter.render()

    def _section_surface(self, mesh, values, displacement, scale, origin, normal):
        g = self._volume_grid(mesh).copy(deep=False)
        g.point_data["value"] = values
        g.point_data["disp"] = displacement
        clipped = g.clip(normal=tuple(normal), origin=tuple(origin), crinkle=True)
        surf = _surface(clipped)
        if surf.n_points == 0:  # plane outside the model: show everything
            surf = _surface(g)
        ids = np.asarray(surf.point_data["node_id"]).astype(int)
        base = mesh.nodes[ids].copy()
        disp = displacement[ids].copy()
        surf.points = base + scale * disp
        surf.point_data["node_id"] = ids
        return surf, base, disp

    def set_deformation(self, s: float) -> None:
        if self._res_surface is None:
            return
        self._res_surface.points = self._res_base + s * self._res_disp
        if self._res_edges is not None:
            self._res_edges.points = self._res_base + s * self._res_disp
        self.plotter.render()

    def start_animation(self, scale: float, fps: int = 25) -> None:
        if self._res_surface is None:
            return
        self._anim_scale = scale
        self._anim_phase = 0.0
        self._anim_timer.start(int(1000 / fps))

    def stop_animation(self) -> None:
        if self._anim_timer.isActive():
            self._anim_timer.stop()
            self.set_deformation(self._anim_scale)

    def animating(self) -> bool:
        return self._anim_timer.isActive()

    def _anim_step(self) -> None:
        self._anim_phase += 2 * np.pi / 40
        self.set_deformation(self._anim_scale * 0.5 * (1 - np.cos(self._anim_phase)))

    def show_probe(self, point: np.ndarray, text: str) -> None:
        self.clear_group("probe")
        self._add_labels("probe", "probe_label", np.asarray(point)[None, :], [text], point_color="#4fd1d9",
                         point_size=12, text_color="#0d1015", font_size=13, shape_color="#4fd1d9",
                         shape_opacity=0.9, render_points_as_spheres=True, always_visible=True)
        self.plotter.render()

    # ------------------------------------------------------------------ highlight
    def highlight(self, picks: list[Pick], bodies: dict[str, Body], mesh: Optional[FEMesh] = None) -> None:
        self.clear_group("highlight")
        by_body: dict[str, list[int]] = {}
        for p in picks:
            b = bodies.get(p.body)
            if p.kind == "face" and b is not None:
                by_body.setdefault(p.body, []).append(p.index)
            elif p.kind == "body" and b is not None:
                sub = scene.body_faces_polydata(b)
                a = self._add("highlight", f"hl_body_{p.body}", sub, color=theme.ACCENT_2, opacity=0.45, pickable=False)
                a.GetMapper().SetResolveCoincidentTopologyToPolygonOffset()
            elif p.kind == "edge" and b is not None and p.index < len(b.tessellation.edge_polylines):
                pl = b.tessellation.edge_polylines[p.index]
                if len(pl) >= 2:
                    self._add("highlight", f"hl_e_{p.body}_{p.index}", pv.lines_from_points(pl),
                              color=theme.ACCENT_2, line_width=6, pickable=False)
            elif p.kind == "vertex" and b is not None:
                self._add("highlight", f"hl_v_{p.body}_{p.index}", pv.Sphere(radius=0.012 * b.size,
                          center=b.vertices[p.index].point), color=theme.ACCENT_2, pickable=False)
            elif p.kind == "node" and mesh is not None:
                pt = mesh.nodes[p.index]
                self._add_labels("highlight", f"hl_n_{p.index}", pt[None, :], [f"N{p.index + 1}"],
                                 point_color=theme.ACCENT_2, point_size=12, font_size=12, text_color="#111111",
                                 shape_color=theme.ACCENT_2, render_points_as_spheres=True, always_visible=True)
            elif p.kind == "element" and mesh is not None:
                from opencae.meshing.elements import boundary_faces, face_display_triangles
                el = mesh.elements[p.index]
                _, _, faces = boundary_faces(el[None, :], mesh.element_type)
                tris = face_display_triangles(np.searchsorted(np.unique(el), faces))
                pts = mesh.nodes[np.unique(el)]
                sub = pv.PolyData(pts, np.hstack([np.full((len(tris), 1), 3), tris]).ravel())
                self._add("highlight", f"hl_el_{p.index}", sub, color=theme.ACCENT_2, show_edges=True,
                          edge_color="#111111", line_width=2, pickable=False)
                c = mesh.nodes[el[:mesh.etype.n_corners]].mean(axis=0)
                self._add_labels("highlight", f"hl_ell_{p.index}", c[None, :], [f"E{p.index + 1}"],
                                 point_size=1, font_size=12, text_color="#111111", shape_color=theme.ACCENT_2,
                                 always_visible=True)
        for bid, faces in by_body.items():
            sub = scene.face_subset(bodies[bid], faces)
            if sub is not None:
                a = self._add("highlight", f"hl_f_{bid}", sub, color=theme.ACCENT_2, opacity=0.95, pickable=False)
                a.GetMapper().SetResolveCoincidentTopologyToPolygonOffset()
                a.GetMapper().SetRelativeCoincidentTopologyPolygonOffsetParameters(-4, -4)
        self.plotter.render()

    # ------------------------------------------------------------------ picking
    def set_pick_kind(self, kind: str) -> None:
        self.pick_kind = kind
        self._apply_pickability()
        for name in self._groups.get("geometry", []):
            if name.endswith("_verts"):
                actor = self.plotter.actors.get(name)
                if actor is not None:
                    actor.GetProperty().SetOpacity(1.0 if kind == "vertex" else 0.0)
                    actor.GetProperty().SetPointSize(9 if kind == "vertex" else 1)
        self.plotter.render()

    def _wanted_kinds(self) -> set[str]:
        if self.display == "results":
            return {"results"}
        if self.display == "mesh":
            return {"mesh"}
        return {"body": {"faces"}, "face": {"faces"}, "edge": {"edges"}, "vertex": {"vertices"},
                "node": {"mesh"}, "element": {"mesh"}}.get(self.pick_kind, {"faces"})

    def _apply_pickability(self) -> None:
        wanted = self._wanted_kinds()
        for actor in self.plotter.actors.values():
            key = getattr(actor, "_opencae_key", None)
            pk = self._pickables.get(key) if key is not None else None
            try:
                actor.SetPickable(bool(pk and pk.kind in wanted))
            except AttributeError:
                pass

    def _on_press(self, obj, event) -> None:
        self._press_pos = self.plotter.iren.get_event_position()
        self._press_ctrl = bool(QApplication.keyboardModifiers() & Qt.ControlModifier)

    def _on_release(self, obj, event) -> None:
        if self._press_pos is None:
            return
        x, y = self.plotter.iren.get_event_position()
        px, py = self._press_pos
        self._press_pos = None
        if abs(x - px) + abs(y - py) > 4:
            return  # was a drag (rotate)
        self.pick_at(x, y, additive=self._press_ctrl)

    def pick_at(self, x: int, y: int, additive: bool = False) -> Optional[Pick]:
        wanted = self._wanted_kinds()
        picker = vtk.vtkCellPicker()
        picker.SetTolerance(0.006 if wanted & {"edges", "vertices"} else 0.0005)
        picker.PickFromListOn()
        candidates = []
        for actor in self.plotter.actors.values():
            key = getattr(actor, "_opencae_key", None)
            pk = self._pickables.get(key) if key is not None else None
            if pk and pk.kind in wanted:
                picker.AddPickList(actor)
                candidates.append(actor)
        if not candidates:
            return None
        picker.Pick(x, y, 0, self.plotter.renderer)
        actor = picker.GetActor()
        cell = picker.GetCellId()
        if actor is None or cell < 0:
            self.picked.emit(None)
            return None
        pk = self._pickables.get(getattr(actor, "_opencae_key", None))
        if pk is None:
            return None
        pos = np.array(picker.GetPickPosition())
        pick: Optional[Pick] = None
        if pk.kind == "faces":
            fid = int(pk.data.cell_data["face_id"][cell])
            pick = Pick("body" if self.pick_kind == "body" else "face", pk.body, -1 if self.pick_kind == "body" else fid,
                        pos, additive)
        elif pk.kind == "edges":
            pick = Pick("edge", pk.body, int(pk.data.cell_data["edge_id"][cell]), pos, additive)
        elif pk.kind == "vertices":
            pick = Pick("vertex", pk.body, int(pk.data.cell_data["vertex_id"][cell]), pos, additive)
        elif pk.kind in ("mesh", "results"):
            c = pk.data.get_cell(cell)
            pids = np.asarray(c.point_ids)
            pts = pk.data.points[pids]
            local = pids[int(np.argmin(np.linalg.norm(pts - pos, axis=1)))]
            node = int(pk.data.point_data["node_id"][local])
            if pk.kind == "mesh" and self.pick_kind == "element":
                pick = Pick("element", pk.body, int(pk.data.cell_data["element_id"][cell]), pos, additive)
            else:
                pick = Pick("node", pk.body, node, pk.data.points[local], additive)
        if pick is not None:
            self.picked.emit(pick)
        return pick

    # ------------------------------------------------------------------ view controls
    def set_view(self, name: str) -> None:
        p = self.plotter
        {"front": lambda: p.view_xz(), "back": lambda: p.view_xz(negative=True), "top": lambda: p.view_xy(),
         "bottom": lambda: p.view_xy(negative=True), "right": lambda: p.view_yz(),
         "left": lambda: p.view_yz(negative=True), "iso": lambda: p.view_isometric()}[name]()
        p.render()

    def fit(self) -> None:
        self.plotter.reset_camera()
        self.plotter.render()

    def set_parallel(self, on: bool) -> None:
        if on:
            self.plotter.enable_parallel_projection()
        else:
            self.plotter.disable_parallel_projection()
        self.plotter.render()

    def set_background(self, color: str, top: Optional[str] = None) -> None:
        self.plotter.set_background(color, top=top)
        self.plotter.render()

    def screenshot(self, path: Path, scale: int = 1) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.plotter.render()
        self.plotter.screenshot(str(path), scale=scale)
        return path

    def close_plotter(self) -> None:
        self.stop_animation()
        try:
            self.plotter.close()
        except Exception:
            pass
