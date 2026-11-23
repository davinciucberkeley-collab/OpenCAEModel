# Drone projects: the drone.json guide

A drone project is a folder with three things:

```
my_drone/
  drone.json        the parameter file (this guide)
  airframe.step     your CAD file (.step, .stp, .stl or .obj)
  results/          written by OpenCAE; safe to delete
```

Make one with `opencae new my_drone --cad airframe.step --type multirotor`, or from the app (Drone, New drone
project). OpenCAE copies the CAD file, guesses what each part is from its name and writes a `drone.json` in which
everything it cannot know is `null`. Your job is to replace each `null` with a number and say where it came from.
Open `opencae/examples/drones/quad250/drone.json` next to this guide: it is a complete, filled-in example.

**Simulation results plan the tests; they are not flight approval.** OpenCAE never says "approved".

## Numbers and where they come from

Every number can be written two ways:

```json
"kv": 2400
"kv": {"value": 2400, "unit": "rpm/V", "status": "datasheet", "source": "motor maker's page, 2026-11"}
```

A bare number counts as an **estimate**. The long form adds a status:

| status | meaning |
|---|---|
| `estimate` | a typical value or a guess. Results that depend on it can be at best **conditional**. |
| `datasheet` | from the maker's data. Can support a **pass**. |
| `measured` | you measured it on your hardware. Can support a **pass**. |
| `user` | a choice you made (mission time, load factor), not a property of the hardware. |

`null` means "not filled in yet". An analysis that needs a `null` does not guess: it reports **not evaluated**
and lists the key it is missing. Keys that start with `_` are notes and are ignored.

## Results and statuses

Each analysis ends in one of these:

| status | what it tells you |
|---|---|
| `ok` | computed, and every input was a datasheet or measured value |
| `conditional` | computed, but at least one input is an estimate (a result that would be `ok` or `watch` is shown as `conditional`; `extra.numeric_status` keeps `ok`, `watch` or `fail` for colouring) |
| `watch` / `fail` | computed from datasheet or measured inputs, and a number is close to or beyond a limit (`fail` also stays `fail` on estimates) |
| `not_evaluated` | an input is missing (the key is named), a part with the needed role does not exist, the analysis is switched off, or its program is not installed |
| `error` | a solver failed; the log tail is kept |

Every requirement check is `pass`, `conditional`, `fail` or `not_evaluated`. A missed target is always `fail`. A met
target is `pass` only when the numbers behind it are datasheet or measured; on estimates it is `conditional`.
The overall verdict is `not ready` (something fails, or nothing could be evaluated), `conditional`
(nothing fails, but something is estimated or missing) or `ready for physical tests` (everything passes).
That last one means "go build and test it", never "safe to fly".

## Every key

### Top level

| key | meaning |
|---|---|
| `format` | always `"opencae-drone/1"` |
| `name` | any name; shown in the app and in results |
| `type` | `multirotor`, `fixed_wing`, `tailsitter` or `vtol`. Decides default rotor direction, which requirements apply and what the structure analysis loads |
| `engine` | `"generic"` (default) or `"k09l"` (the K09-L design study; see the end) |

### `geometry`

| key | meaning |
|---|---|
| `file` | CAD file name, relative to the project folder |
| `units` | `mm`, `m` or `in`: the unit the STL or OBJ file is drawn in. A 0.25 m quad saved in metres needs `"m"`, otherwise it is read as 0.25 mm. STEP files carry their own units and ignore this |
| `forward` | which CAD axis points to the nose or front: `+x -x +y -y +z -z`. Default `+x` |
| `up` | which CAD axis points up. Default `+z` |

OpenCAE turns your file into its own frame: x forward, y left, z up (a right-handed frame, so a normal CAD export keeps its handedness), millimetres. Parts come from the file:
the solids of a STEP file, the named solids of an ASCII STL, the `o`/`g` groups of an OBJ. A binary STL (or any
file without names) is split into separate pieces called `part_1`, `part_2`, ... largest first.

### `roles`

`{"arm": ["arm_*"], "motor": ["motor_*", "mtr*"]}`: for each role, name patterns (`*` matches anything, upper or
lower case does not matter). A part gets the first role, in the order below, that has a matching pattern;
parts that match nothing are `other`.

Roles: `frame`, `arm`, `motor`, `prop`, `battery`, `wing`, `fin`, `control_surface`, `landing`, `payload`,
`electronics`, `other`.

If you leave out `roles` entirely, OpenCAE guesses from common words (arm, motor, prop, battery, wing, tail,
elevon, leg/skid, frame/body/plate, fc/esc/gps/camera). Parts called `part_1`, `part_2` get no role: rename them
in your CAD tool or write the patterns yourself.

### `materials` and `role_materials`

```json
"materials": {"carbon_plate": {"density_kg_m3": 1600, "youngs_modulus_gpa": 50, "poisson": 0.3,
                               "yield_strength_mpa": 600, "max_temp_c": 120,
                               "status": "datasheet", "source": "supplier sheet"}},
"role_materials": {"frame": "carbon_plate", "arm": "carbon_plate"}
```

`status` and `source` apply to the whole material. `role_materials` says which material each role is made of.
Density gives a part its mass when you have not typed a mass (the CAD solid must be closed). Stiffness,
Poisson ratio and yield strength feed the structure analysis. A missing property is never assumed.

### `mass`

| key | meaning |
|---|---|
| `parts` | grams. A **role** name (`"arm": 14`) is the mass of **each** part with that role; a **part** name (`"battery_pack": 185`) is that one part. A part's own name wins over its role. Parts with no mass here use `propulsion.motor.mass_g` (motors) or density times volume. A part with neither makes the mass analysis `not_evaluated` and lists it |
| `payload_g` | grams carried, counted in the total mass (0 if none) |

### `propulsion`

| key | unit | meaning |
|---|---|---|
| `rotors` | | `"auto"`: one rotor at the centre of every `motor` part, pointing up (multirotor, vtol) or forward (tailsitter, fixed wing), alternating spin. Or a list `[{"position_mm": [x, y, z], "axis": [0, 0, 1], "spin": "cw"}]` |
| `motor.kv` | rpm/V | motor constant |
| `motor.r_ohm` | ohm | winding resistance |
| `motor.i0_a` | A | no-load current |
| `motor.i_max_a` | A | highest current the motor is rated for |
| `motor.mass_g` | g | mass of one motor |
| `prop.diameter_in`, `prop.pitch_in` | inch | propeller size |
| `prop.ct`, `prop.cp` | | thrust and power coefficients (static): thrust = ct x air density x (rev/s)^2 x diameter^4, power = cp x air density x (rev/s)^3 x diameter^5. Take them from the maker's test data or your own thrust stand |
| `battery.cells` | | number of cells in series (nominal 3.7 V each) |
| `battery.capacity_mah` | mAh | capacity |
| `battery.c_rating` | | continuous discharge rating (C x capacity = allowed current) |
| `battery.r_int_mohm_per_cell` | milliohm | internal resistance of one cell |
| `battery.usable_fraction` | 0 to 1 | how much of the capacity you will use (0.8 keeps a reserve) |
| `esc.i_cont_a` | A | continuous current rating of the speed controller |
| `esc.efficiency` | 0 to 1 | speed controller efficiency |

The propulsion analysis finds where each motor and propeller settle, then reports thrust at full throttle, the
throttle at which total thrust equals the weight (hover), the battery current there and the hover time.

### `aero`, `mission`, `environment`

| key | meaning |
|---|---|
| `aero.cruise_speed_ms` | m/s, the speed for the air-flow (CFD) analysis |
| `aero.alpha_deg` | degrees nose-up angle of the aircraft in that flow |
| `aero.ref_area_m2`, `aero.ref_chord_m`, `aero.cd0` | optional; used when you want coefficients on your own reference area |
| `mission.hover_s`, `mission.cruise_s` | seconds you plan to hover / cruise |
| `mission.payload_g` | grams of payload for the mission |
| `environment.rho` | air density, kg/m3 (1.225 at sea level) |
| `environment.g` | gravity, m/s2 (9.80665) |

### `analyses`

Run in this order: mass, propulsion, structure, cfd, flight, then the requirement checks.

| key | meaning |
|---|---|
| `cfd.enabled` | `false` skips the air-flow run (which can take tens of minutes) |
| `cfd.speed_ms`, `cfd.alpha_deg` | optional; override `aero` for the air-flow run |
| `cfd.preset` | `coarse`, `medium` or `fine` (more cells, slower) |
| `cfd.iterations` | solver iterations |
| `structure.enabled` | `false` skips the strength run |
| `structure.load_factor` | multiple of the maximum thrust applied to the arms (2 = twice max thrust) |
| `flight.enabled` | `false` skips the hover simulation |
| `flight.disturbance_deg` | the tilt the simulated aircraft is knocked by, then must recover from |

Only one heavy solver runs at a time on a machine.

### `requirements` (optional)

```json
"requirements": [
  {"id": "R-END", "label": "Hover time of at least 8 minutes", "metric": "propulsion.hover_endurance_min",
   "op": ">=", "value": 8}
]
```

`metric` is `analysis.metric`; `op` is `>=`, `<=`, `>` or `<`. Metrics: `mass.mass_kg`, `propulsion.tw_ratio`,
`hover_throttle`, `hover_current_a`, `hover_power_w`, `hover_endurance_min`, `thrust_max_n`,
`thrust_total_max_n`, `full_throttle_current_a`, `motor_current_margin`, `esc_current_margin`,
`battery_current_margin` (rating divided by full-throttle current), `structure.safety_factor`,
`structure.max_stress_mpa`, `structure.max_disp_mm`, `cfd.cl`, `cfd.cd`, `cfd.ld`, `flight.max_tilt_deg`,
`flight.settle_time_s`, `flight.position_error_m`. If you leave `requirements` out, sensible defaults for the
aircraft type are used.

### Files in `results/<variant>/`

Besides `results.json` and `geometry.npz`, each analysis can write field files (`structure_<part>.npz`,
`cfd_*.npz`). A field file has `values` (float16) and, for a surface, `points` (float32, mm) and `triangles`.
The four CFD surface fields share one mesh to save space: `cfd_cp.npz` holds `points`, `triangles`, `values` (Cp)
and the flow slices and streamlines (`slice_*`, `stream_*`, float32 points); `cfd_p.npz`, `cfd_u_wall.npz` and
`cfd_tau_wall.npz` hold only `values` and `mesh` = `"cfd_cp.npz"`, the file (same folder) that has their points
and triangles. `opencae.drone.analyses.cfd.load_surface_field(path)` reads any of them with the mesh filled in.

The CFD case follows the aircraft frame: the air comes from ahead (towards -x), the inflow boundary lies ahead of the
nose, the wake refinement sits behind the tail, lift is +z and drag is along the flow. Variant names are 1 to 40
letters, digits, `_` or `-`.

## Running and changing

```
opencae run my_drone                     # everything; unchanged analyses are skipped
opencae run my_drone --only mass,propulsion
opencae iterate my_drone --set propulsion.battery.capacity_mah=2200 --name big_battery
opencae status my_drone
opencae clean my_drone                   # delete solver scratch
```

`iterate` tries a change without touching `drone.json`: the variant is stored in `results/<name>/` and compared
with the baseline. A key that does not exist, or a text where a number belongs, is refused and nothing changes.
An analysis is re-run only when something it reads has changed.

## The K09-L project

`opencae/examples/drones/k09l/drone.json` has `"engine": "k09l"`. It runs the K09-L design study instead of the
generic analyses: `study` is the path of the study file and `variant` the study variant to run (by name). Parameters
of that aircraft are edited in the K09-L Design Monitor, not in this file, so `iterate --set` is not available there.
