# Engineering validation

Run `python -m opencae.examples.run_validation --out DIR` (add `--quick` for fewer meshes, `--no-extended` for
A–E only). Every number below is
from the real pipeline (OpenCascade → Gmsh → CalculiX 2.21 → parsed output) on the reference machine,
October 2026. The full report with plots is [validation/validation_report.html](validation/validation_report.html),
and the raw data is in [validation/validation_results.json](validation/validation_results.json).

Material for all cases: E = 200 000 MPa, ν = 0.30 (example structural steel). Units: mm-N-s.

## A. Axially loaded bar — PASS

100 × 10 × 10 mm bar, F = 5000 N on the end face, with symmetry constraints on x = 0, y = 0 and z = 0 (uniaxial
stress, free lateral contraction). Theory: u = FL/(AE) = 0.025 mm and σ = F/A = 50 MPa.

| Element | Nodes | u tip [mm] | u error | max σ deviation |
|---|---|---|---|---|
| C3D4 | 87 / 190 / 1073 | 2.500000e-02 | 0.00000 % | 0.00000 % |
| C3D10 | 458 / 998 / 6602 | 2.500000e-02 | 0.00000 % | 0.00100 % |

*Assumption / limitation*: the exact solution is linear, and both elements represent it exactly. This benchmark
therefore verifies the pipeline (load mapping, units, deck, parsing) rather than discretisation accuracy. The
0.001 % is the 6-significant-digit precision of `.frd` output.

## B. Cantilever beam — PASS

L = 200 mm, 20 × 20 mm section, root face fully fixed, F = 1000 N (−Z) as uniform traction on the tip face.
Tip deflection is the mean −Uz over the tip-face nodes. Euler–Bernoulli FL³/(3EI) = 1.000000 mm; Timoshenko with
Cowper's k = 1.007650 mm. Mid-span bending stress Mc/I = 75 MPa.

| Element | Nodes | Tip [mm] | vs EB | vs Timoshenko | σ mid-span [MPa] | Peak VM nodal / IP [MPa] |
|---|---|---|---|---|---|---|
| C3D10 | 998 | 0.99823 | −0.18 % | −0.94 % | 75.02 | 150.3 / 136.3 |
| C3D10 | 4437 | 0.99977 | −0.02 % | −0.78 % | 75.00 | 150.7 / 146.0 |
| C3D10 | 11370 | 1.00013 | +0.01 % | −0.75 % | 75.02 | 154.2 / 157.2 |
| C3D10 | 39598 | 1.00053 | +0.05 % | −0.71 % | 75.05 | 180.7 / 177.9 |
| C3D4 | 190 | 0.47556 | −52.4 % | −52.8 % | 22.42 | 84.5 / 120.5 |
| C3D4 | 1079 | 0.83819 | −16.2 % | −16.8 % | 55.03 | 113.3 / 141.9 |
| C3D4 | 5717 | 0.95228 | −4.8 % | −5.5 % | 66.54 | 135.7 / 149.3 |

Global equilibrium residual is below 1e-11 on every mesh.

*Assumptions and differences between beam theory and the 3-D solid model*

* Euler–Bernoulli neglects shear deformation. At L/h = 10, shear adds about 0.8 % (Timoshenko).
* The 3-D root face is fully clamped, which also suppresses Poisson contraction and slightly stiffens the root.
  Beam theory does not model how the support is applied. The converged 3-D result (1.0006 mm) therefore lies
  between the two theories, close to Euler–Bernoulli.
* The tip load is a uniform traction, not a point load at the centroid.
* Linear tetrahedra (C3D4) lock in bending and converge slowly. They are shown for completeness, not
  recommended for bending.

## C. Mesh refinement — PASS

| Element | Observed order p | Richardson tip deflection | GCI (fine) | Monotone |
|---|---|---|---|---|
| C3D10 | 3.64 | 1.000633 mm | 0.012 % | yes |
| C3D4 | 2.53 | 0.98603 mm | 4.43 % | yes |

* C3D10 mid-span bending stress stays within 0.1 % of Mc/I on all seven meshes.
* The **peak** von Mises stress (150 → 181 MPa) keeps growing with refinement. It sits at the corners of the
  clamped root face, which is a stress singularity, so it is not a convergent quantity
  (`validation/peak_stress_singularity.png`).
* The Richardson estimate is only meaningful in the asymptotic range. The C3D4 sequence is still dominated by
  locking, so its extrapolation is indicative only.

## D. Boundary-condition validation — PASS

| Case | Pre-check | CalculiX if pre-check bypassed |
|---|---|---|
| no constraints | blocked: "model has no constraints" | not run |
| Ux = 0 on root face only | blocked: rank 3/6, free Ty, Tz, Rx | reports success, max \|u\| = 5.3e10 mm |
| single fixed vertex | blocked: rank 3/6, rotations free | reports success, max \|u\| = 8.2e10 mm |
| fully fixed root (control) | accepted | success, max \|u\| = 1.0019 mm |

CalculiX does not reliably reject singular systems; it returns meaningless displacements. OpenCAE Studio
therefore checks the rigid-body restraint rank before solving and names the unrestrained motions.

## E. Units and serialization — PASS

* The same cantilever mesh, scaled, was solved in mm-N-s and m-N-s. The maximum relative difference is
  1.0e-6 in displacement and 6.6e-6 in von Mises stress (6-digit output precision). Gmsh tolerances are
  absolute, so meshing the metre-scaled CAD independently gives a slightly different mesh. The tests therefore
  compare identical meshes, plus a separate check that independently meshed models agree to 0.2 % in tip
  deflection.
* Save → reopen: project inputs, geometry volume, mesh connectivity and stored results are identical, results
  are recognised as current, and re-solving the reopened model reproduces the displacements exactly.

## Additional checks in the automated tests

* Gravity + pressure on a fixed block: the applied load equals ρgV + pA exactly. The reaction plus the load at
  constrained DOFs balances it (`test_gravity_and_pressure_equilibrium`).
* Tensor shear-strain convention: ε_xy = σ_xy / (2G) (`test_tensor_shear_strain_convention`).
* Curved C3D10 volume integration of a cylinder is accurate to < 1e-4 and at least 10× better than C3D4.
* Plate with hole example (quarter model, d/W = 0.2): σxx at the hole edge = 316 MPa vs Heywood
  Kt·σ_net ≈ 314 MPa (+0.7 %).
* Interactions between separately meshed bodies (`test_interactions.py`):
  * two stacked 10 mm steel cubes, 100 MPa compression: bonded (tie) σzz = −100 MPa everywhere (±0.1 %),
    top displacement exact (−0.0100 mm); penalty contact σzz exact, top displacement −0.01011 mm (extra
    1.1 % is the penalty penetration p/K), contact pressure 100 MPa within 5 %;
  * tied bar with non-matching meshes in tension: end displacement FL/(EA) within 0.1 %, σxx = 50 MPa within
    0.1 % away from the joint; within one element of the joint the tie disturbs σxx locally by up to ±10 %;
  * tied bar in heat conduction: linear temperature profile exact; tied cantilever modal frequencies equal the
    one-body model within 0.5 %;
  * separate, unconnected bodies are reported as insufficiently constrained before solving. (This also
    fixed a defect: with quadratic elements, mid-side nodes were not assigned to their mesh component, so
    supports of one body could hide a missing restraint of another.)

## X. Extended physics — PASS (23 / 23 quantities)

Added in 0.2. Run by `run_validation` after A–E (skip with `--no-extended`; flow benchmarks need SU2 and are
reported as skipped without it). Reference machine run, October 2026, CalculiX 2.21 and SU2 8.5.0 (Linux build;
the Windows `ccx.exe` 2.21 of the reference installation gives identical static/thermal results). Example steel:
k = 50 W/(m·K), c = 470 J/(kg·K), ρ = 7850 kg/m³, α = 12e-6 1/K.

| Benchmark | Quantity | FE / FV | Reference | Error | Tolerance | Reference / note |
|---|---|---|---|---|---|---|
| T1 slab, flux + fixed T (100 mm, q = 10 kW/m²) | T(L) | 40.000 °C | 40.000 °C | 0.000 % | 0.01 % | T0 + qL/k |
| T1 | heat through fixed face | 1.0000 W | 1.0000 W | 0.000 % | 0.01 % | energy balance |
| T2 slab, convection h = 100 | T(0) | 120.00 °C | 120.00 °C | 0.000 % | 0.01 % | q = h(T0 − T∞) |
| T3 slab, radiation ε = 0.8 | T(0) | 417.73 °C | 417.73 °C | 0.000 % | 0.01 % | q = εσ(T0⁴ − T∞⁴) |
| T4 heat generation 1 MW/m³, both ends 0 °C | T max | 25.000 °C | 25.000 °C | 0.000 % | 0.1 % | qL²/(8k) |
| T5 transient, step to 120 °C, dt = 10 s | T(L, 520 s) | 96.979 °C | 97.625 °C | −0.66 % | 1.5 % | Fourier series |
| T5 transient, dt = 5 s | T(L, 520 s) | 97.301 °C | 97.625 °C | −0.33 % | 1.5 % | error halves with dt (backward Euler) |
| M1 cantilever 200 × 20 × 20 | f1 bending | 406.62 Hz | 407.69 Hz | −0.26 % | 1 % | Euler–Bernoulli |
| M1 | torsion | 3608.1 Hz | 3593.9 Hz | +0.40 % | 1 % | J = 0.1406 a⁴ |
| M1 | axial | 6329.0 Hz | 6309.4 Hz | +0.31 % | 1 % | quarter-wave rod |
| B1 Euler column, fixed-free, 200 × 10 × 10 | P_cr | 10289 N | 10281 N | +0.08 % | 1 % | π²EI/(4L²) |
| S1 cantilever, end moment 100 N·m | tip deflection | 0.7461 mm | 0.7500 mm | −0.52 % | 2 % | ML²/(2EI) |
| S2 large deflection, PL²/EI = 1, C3D20R, NLGEOM | vertical tip / L | 0.30144 | 0.3017 | −0.09 % | 1 % | elastica (Bisshopp & Drucker) |
| S2 | horizontal shortening / L | 0.05636 | 0.0566 | −0.43 % | 3 % | elastica; linear theory gives 0.333 / 0 |
| S3 bar restrained axially, ΔT = 100 K | σxx | −239.998 MPa | −240.0 MPa | −0.001 % | 0.1 % | −EαΔT |
| F1 2-D plane channel, Re = 100, 8 quads across | dp/dx | −1.0667 Pa/m | −1.0499 Pa/m | +1.59 % | 2 % | −12μU/H² (U = delivered) |
| F1, 16 quads | dp/dx | −1.1294 | −1.1249 | +0.40 % | 1 % | second-order convergence |
| F1, 32 quads | dp/dx | −1.1636 | −1.1624 | +0.10 % | 1 % | |
| F2 square duct, 10 hexes across | dp/dz | −1.1897 | −1.1512 | +3.3 % | 4 % | f·Re = 56.91 (Shah & London) |
| F2, 20 hexes | dp/dz | −1.2940 | −1.2827 | +0.88 % | 4 % | |
| F3 circular pipe, tets, D/h = 20 (445 k cells) | dp/dz | −1.5040 | −1.4613 | +2.9 % | 4 % | Hagen–Poiseuille, Re = 50 |
| F3 | centre-line velocity | 9.156 mm/s | 9.027 mm/s | +1.4 % | 3 % | 2·U_mean |
| F4 sphere, Re = 20 (flow-domain tool, sides at 4 D) | Cd | 2.636 | 2.610 | +1.0 % | 8 % | Schiller–Naumann (±5 % vs experiments) |

Notes:

* The flow references use the flow rate actually delivered by the solver (measured at the outlet or on a cross
  section). With a uniform inlet, the inlet nodes on the walls are at rest, so the delivered rate is below the
  nominal one on coarse meshes (6 % at 16 cells across). The study summary reports both numbers.
* Flow convergence uses residual drops relative to the start of the run. An earlier criterion based on the
  absolute RMS pressure was found to stop dimensional runs long before convergence (18 % error on a fine mesh);
  it is no longer used.
* Unstructured tetrahedra (F3) converge more slowly than hexahedra and quadrilaterals; viscous wall forces on
  coarse wall meshes are underestimated by SU2's first-order wall gradients (19 % at 10 cells across a duct,
  10 % at 20, compared with a momentum balance).
* The sphere case is a correlation check, not an exact solution. The correlation is accurate to about ±5 %, and
  the side walls at 4 D add a small blockage effect.
