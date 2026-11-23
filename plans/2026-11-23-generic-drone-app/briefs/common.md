# Common brief for B1–B5 (read before your own brief)

Repo `/home/claude/OpenCAE-Studio`. Python `/home/claude/venv/bin/python`. Test command form:
`cd /home/claude/OpenCAE-Studio && PYTHONPATH=$PWD QT_QPA_PLATFORM=offscreen xvfb-run -a /home/claude/venv/bin/python -m pytest <file> -q`

Read first: `AGENTS.md`, `plans/2026-11-23-generic-drone-app.md` (your Task section and Global Constraints),
`opencae/drone/types.py` (shared types; do not edit), `opencae/drone/runlock.py`.

Rules: edit only the files your brief names. Write tests first. Do not `git commit`. Do not run OpenFOAM or
CalculiX unless your brief says so (8 GB machine). Stop rule: if something is ambiguous or a check fails for a
reason the brief does not cover, stop and report the blocker with the exact output; do not improvise a design.
Report at the end: files written, the pass-check command and its exit status, anything another builder needs.
Other builders work at the same time on the other rows of the agent table; their modules may appear while you
work. Import them lazily and `pytest.importorskip` tests that need them; re-check before you finish.

## drone.json (format `opencae-drone/1`), the shared contract

Each numeric leaf is a bare number (status `estimate`, empty source) or `{"value", "unit", "status", "source"}`
with status `estimate | datasheet | measured | user`.

```
name, type (multirotor|fixed_wing|tailsitter|vtol), engine ("generic" default | "k09l")
geometry: {file, units: mm|m|in, forward: +x|-x|+y|-y|+z|-z (CAD axis pointing forward), up: same choices}
          -> aircraft frame: x forward, y left, z up (right-handed), millimetres
roles: {role: [glob patterns, case-insensitive, on part names]}   roles from types.ROLES; first match in ROLES
       order wins; unmatched -> "other"
materials: {name: {density_kg_m3, youngs_modulus_gpa, poisson, yield_strength_mpa, max_temp_c?, status, source}}
role_materials: {role: material name}
mass: {parts: {role or part name: grams}, payload_g}
      part mass = explicit grams, else density x closed volume when its role has a material with density,
      else missing -> the mass analysis is not_evaluated and lists the parts without a mass
propulsion: {rotors: "auto" | [{position_mm, axis, spin: cw|ccw}],      auto: one rotor per motor part, at
             motor: {kv, r_ohm, i0_a, i_max_a, mass_g},                  its centroid, axis +z (multirotor)
             prop: {diameter_in, pitch_in, ct, cp},                      or +x (tailsitter/fixed wing)
             battery: {cells, capacity_mah, c_rating, r_int_mohm_per_cell, usable_fraction},
             esc: {i_cont_a, efficiency}}
aero: {cruise_speed_ms, alpha_deg, ref_area_m2?, ref_chord_m?, cd0?}
mission: {hover_s, cruise_s, payload_g};  environment: {rho, g}
analyses: {cfd: {enabled, speed_ms?, alpha_deg?, preset: coarse|medium|fine, iterations},
           structure: {enabled, load_factor}, flight: {enabled, disturbance_deg}}
requirements (optional): [{id, label, metric: "analysis.metric", op: ">=|<=|>|<", value}]
                         absent -> defaults per type from opencae/drone/templates.py
```

## Results layout (written by the pipeline, read by server, CLI and app)

`<project>/results/<variant>/results.json`:
`{"project", "variant", "spec_used", "analyses": {id: result}, "checks": [...], "verdict", "generated",
"storage_bytes"}`; a result is `types.result(...)`. Checks rows: `{id, label, metric, op, target, value,
status: pass|conditional|fail|not_evaluated, why}`. Verdict: `not ready` | `conditional` |
`ready for physical tests` (never "approved").
`<project>/results/<variant>/geometry.npz`: `names` (str array), `roles` (str array), and per part i
`p{i}` float32 (n,3) mm, `t{i}` int32 (m,3). Field files `*.npz` via `types.save_field` in the same folder.
Scratch: `<project>/results/<variant>/_scratch/<analysis>/`, deleted after success unless keep_cases.

## Metric names (exact)

mass: `mass_kg`, `cg_mm` (list of 3), `inertia_kgm2` (3x3 nested list); extra.part_masses
propulsion: `thrust_max_n` (per rotor), `thrust_total_max_n`, `tw_ratio`, `hover_throttle`, `hover_current_a`,
`hover_power_w`, `hover_endurance_min`, `full_throttle_current_a`, `rotor_count`; extra.rotors
`[{position_mm, axis, spin}]`
structure: `safety_factor`, `max_stress_mpa`, `max_disp_mm`; extra.parts; fields `structure_<part>.npz`
cfd: `cl`, `cd`, `ld`, `lift_n`, `drag_n`, `converged`, `cells`, `iterations`; fields `cfd_cp.npz`
(+ slice_points, slice_values, stream_points, stream_offsets, stream_speed), `cfd_p.npz`, `cfd_u_wall.npz`,
`cfd_tau_wall.npz`
flight: `max_tilt_deg`, `settle_time_s`, `position_error_m`, `throttle_margin`; extra.t, extra.pos, extra.att,
extra.motor (each ≤ 400 samples)

## Pipeline API (B1 provides; others call)

`opencae.drone.pipeline`: `run_project(project_dir, only=None, overrides=None, variant="baseline",
progress=None, cancelled=None, keep_cases=False, force=False) -> dict`; `status(project_dir) -> dict`;
`clean(project_dir, results=False) -> int`; `new_project(dir, cad_file, name, type) -> Path`;
`iterate(project_dir, overrides, name, only=None) -> dict` (`{"variant", "results", "compare": [{metric,
label, baseline, variant, unit, better}]}`); `variants(project_dir) -> list[str]`.
`progress(msg: str, frac: float)`, `cancelled() -> bool`.
`opencae.drone.spec`: `load(path) -> dict` (project dir or drone.json), `save(spec, path)`,
`param(spec, dotted) -> Param | None`, `apply_overrides(spec, {dotted: value}) -> dict` (deep copy; raises
KeyError for an unknown key, ValueError for a non-number where a number is expected), `validate(spec) ->
list[str]`, `input_hash(spec, keys) -> str`.
`opencae.web.server.DashboardServer(project_dir, host="127.0.0.1", port=0)`: `.start() -> url`, `.stop()`,
`.url`, `.jobs.progress` `{running, step, frac, log, error, started, finished}`, `.jobs.start(only=None,
overrides=None, variant="baseline")`, `.jobs.stop()`.
