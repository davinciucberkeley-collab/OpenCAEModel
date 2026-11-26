# Tutorial: structural and flow analysis of a half-wing (S1223, STEP file)

This tutorial uses `Wing_S1223_left_4face_smooth_solid.step` (left half-wing; a copy ships as
`opencae/tests/data/geometry/wing_s1223_half.step`). Every number below comes from running these steps with
OpenCAE 0.2 (CalculiX 2.21). The structural numbers are from those runs. Flow studies now run on OpenFOAM (Part C);
the flow figures that earlier versions of this page quoted came from a retired solver and have been removed, so
run the study and read your own numbers.

**The geometry**, as imported (millimetres):

| Item | Value |
|---|---|
| Span (root y = −110 → tip y = −1000) | 890 mm |
| Root chord / tip chord | 327 mm / 237 mm (tapered, straight trailing edge at x ≈ 993) |
| Planform area (half-wing) | 0.251 m² |
| Volume | 4.58 L (a **solid** wing) |
| Faces | **Face 1** lower surface · **Face 2** upper surface · **Face 3** root (plane, y = −110) · **Face 4** tip (plane) |
| Incidence | chord line at the root is level (leading and trailing edge both at z ≈ 84 mm): 0° geometric angle of attack |

Face numbers are those shown in OpenCAE's selection lists and model tree. In every list each face is also shown
with its centroid `c=(x, y, z)`, which you can use to check which face is which.

---

## Part A — Project and geometry

1. Start OpenCAE (`launch_opencae.bat`). **File → New project**. Keep the unit system **mm-N-s** (lengths mm,
   forces N, stresses MPa).
2. **Home/Geometry → Import geometry…** (Ctrl+I) → choose the STEP file. Accept the default (one body per solid).
3. **Geometry → Check geometry**. Expect one valid solid with 4 faces and 6 edges.
4. **File → Save project as…** (e.g. `wing.ocae`). Save after each part.

---

## Part B — Structural study (static: wing bending under lift)

### B1. Material
The wing is solid, so the material matters a lot. Built-in materials are only steel, aluminium and titanium
examples.

1. **Materials → New custom material…** Enter **your** material's data. The units are shown next to each field.
   Example used here (generic PLA, *not* measured data):
   * Young's modulus **3500 MPa**, Poisson's ratio **0.36**, density **1.24e-9 t/mm³** (= 1240 kg/m³), yield
     strength **50 MPa**.
   * For a printed part, use the values of your filament and print orientation (printed parts are weaker across
     layers). For a foam-core or composite-skin wing, a solid isotropic model is only a rough approximation.
2. **Materials → Assign material…** → assign it to the wing.

### B2. Supports
The root attaches to the fuselage, so the simplest model is a clamped root.

1. In face selection mode (**View → Face**, the default), click the **root face (Face 3, y = −110)**. Or tick it
   in the dialog list.
2. **Boundary Conditions → Fixed support…** → name it `root`.

### B3. Lift load
Lift L = ½ ρ V² S C_L × n (n = load factor). Example: sea-level air ρ = 1.225 kg/m³, V = 15 m/s, S = 0.251 m²,
C_L = 1.5 gives **L ≈ 52 N** per half-wing at n = 1. Use your own flight speed, C_L and design load factor
(manoeuvres and gusts, typically n = 2–4, plus a safety factor).

1. Select the **lower surface (Face 1)**.
2. **Boundary Conditions → Force…** → magnitude **52 N** (× n), direction **(0, 0, 1)** (upwards). The force is
   spread over the face in proportion to area. This is a simplification: real lift is concentrated forward
   (about quarter chord) and decreases towards the tip. For a more conservative bending moment, apply the force on
   the inner part only, or use **Remote force…** at the spanwise centre of lift (about 42 % of the half-span for
   an elliptic distribution).
3. Optional: **Boundary Conditions → Gravity…** (self-weight; it acts downwards and slightly reduces the net load).

### B4. Mesh
1. **Mesh → Mesh settings…** Element **C3D10** (quadratic tetrahedron, the default). Leave the size at 0
   (automatic ≈ 47 mm), or set **20 mm** for a better first result.
2. **Mesh → Generate mesh** (Ctrl+M). With the automatic size: about 100 000 nodes and 59 000 elements in about 20 s.
   * The mesh log may say "*N curved element(s) were inverted after curving … moved onto straight edges*". This
     happens at the very thin trailing edge and is handled automatically. All elements are valid.
   * A warning about a few "poor-quality elements (gamma < 0.1)" at the trailing edge is expected.

### B5. Solve and read the results
1. **Solve → Validate model** (F7) → no errors (the "example material" warning is fine). **Solve** (F5).
2. Results (example values: PLA, 52 N on Face 1, automatic mesh):
   * Tip deflection **≈ 2.0 mm** upwards; maximum displacement 2.2 mm. A simple tapered-beam hand estimate gives
     ≈ 1.8 mm, so the result is plausible.
   * Maximum von Mises stress **≈ 1.25 MPa**, at the root near the leading/trailing edges, far below the 50 MPa
     yield of PLA. Stresses at the clamped root edge are partly a *singularity* of the ideal clamp.
3. Useful views: **Results → Deformed shape**, **von Mises**, **Probe values** (click a point),
   the **Section plane** option in the *Results display* panel (cut through the root region), **Plot Explorer… → Along a line** (e.g. bending
   stress along the span on the upper surface).
4. **Check the mesh: Mesh → Mesh convergence study…** (3–4 refinement levels). Use the tip deflection and a
   stress *away from the root edge* as the monitored values. The study estimates the discretisation error.

### B6. Other structural checks (optional, new studies)
* **Modal**: **Physics & Setup → New study… → Modal** (same supports, no loads): natural frequencies of the
  clamped wing. Keep them away from motor/propeller frequencies.
* **Buckling**: only relevant for thin skins under compression; with a solid wing it is not critical.

---

## Part C — 3-D flow study (whole half-wing)

### C1. Install OpenFOAM (once)
Flow studies need OpenFOAM. On Windows it runs inside WSL: press **Drone → Solvers… → Install OpenFOAM** (it installs
OpenFOAM in WSL and tests it; see [WINDOWS_SETUP.md](WINDOWS_SETUP.md)). Then check **Tools → Settings → Test OpenFOAM**.

### C2. Create the flow domain with a symmetry plane
The STEP file is the **left half** of the wing with the root on the aircraft's plane of symmetry. Modelling it with
air all around would let air flow around the root, as if the half-wing were flying on its own. Use a symmetry
plane at the root instead.

1. Select the wing (or leave the selection empty: the study's bodies are used).
2. **Physics & Setup → External flow domain…**
   * Solid bodies inside the flow: the wing
   * Flow direction **+X** (the leading edge is at the smallest x)
   * Free-stream speed: your speed, e.g. **15 m/s**; Fluid **Air (ISA sea level, 15 °C)**
   * Upstream **1.0**, downstream **2.0**, side clearance **1.0** (× the largest body size, 890 mm). Larger values
     reduce blockage but cost more cells.
   * **Symmetry plane: "On the bodies' +Y side (e.g. half-wing root)"**
3. This creates a new study *External flow* with a box-shaped fluid body (the wing body itself is kept for the
   structural study). It is set up with:
   * velocity inlet on the upstream face, pressure outlet (0 Pa) downstream;
   * slip walls on the far-field sides and a slip wall named **Symmetry plane** at the root plane;
   * no-slip walls on the wing surfaces (faces without a condition are walls).

### C3. Flow settings
**Physics & Setup → Analysis settings…**
* Flow model **RANS Spalart–Allmaras** (Re = V c / ν ≈ 3×10⁵ at 15 m/s)
* Maximum iterations **3000**, CFL **20–100** (reduce it if the run diverges)
* **Reference area 0.251 m²** (half-wing planform), reference velocity = your speed → C_L and C_D are reported.

### C4. Mesh
1. OpenFOAM meshes the fluid itself (blockMesh + snappyHexMesh). **Mesh settings → Max element size 200 mm**; set
   **Boundary layers** (count, first-layer thickness) so the wing gets wall layers.
2. Select the wing faces of the **flow domain body** (upper, lower and tip surfaces; in the list they are the
   curved B-spline faces and the small planar tip face) → **Mesh → Local refinement on selection…** → size
   **10 mm** or smaller.
3. Generate. The mesh log reports the cell count and the wall-layer coverage; check both.

### C5. Solve and read the results
* The summary lists the wall force on the wing (x = drag, z = lift, in N), the force coefficients, the flow
  balance, iterations and whether the residuals converged.
* View velocity / pressure contours, **section plane** at mid-span, velocity vectors and streamlines.
* Check the reported y⁺ and the wall-layer coverage before you trust the forces: with wall cells much thicker than
  the turbulence model needs, lift and drag are not reliable. Use the 3-D study for the flow pattern (tip vortex,
  pressure distribution, blockage).

---

## Part D — 2-D section study (not available with OpenFOAM yet)

Earlier versions solved a 2-D section of the root airfoil. The 2-D planar mode is not available with OpenFOAM
yet: the study validation reports it as an error. For the section lift, read it from the 3-D study of Part C,
with wall layers and a mesh that resolves the airfoil, or from a mid-span section plane of its results.

---

## Part E — From flow to structure

OpenCAE does not map CFD pressures onto the structural mesh (no fluid–structure coupling). Transfer the load
by hand:

1. Section lift (from the 3-D study of Part C): L' = C_l · ½ ρ V² · c per metre of span. For the half-wing,
   L ≈ ½ ρ V² S C_L, with the 3-D C_L lower than the section C_l. For this aspect ratio, about 0.7–0.8 × C_l
   is a reasonable first estimate.
2. Multiply by the load factor and a safety factor. Use the result as the force (or pressure) in Part B, and
   re-solve the structural study.
3. Record the source and assumptions in the study's **Notes**. They are printed in the report
   (**Reports → Export report**).

## Summary of what is reliable

| Result | Reliability |
|---|---|
| Structural deflection and stress (given correct material and load) | good. Check with the mesh-convergence study; ignore the clamped-edge peak |
| Natural frequencies (modal) | good for the solid, clamped wing |
| 3-D flow pattern, pressure distribution | qualitative |
| 3-D lift and drag | depends on the wall layers: check the reported y⁺ and layer coverage; steady RANS cannot predict laminar separation bubbles |
