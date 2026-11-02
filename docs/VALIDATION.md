# Engineering validation

Run `python -m opencae.examples.run_validation --out DIR` (add `--quick` for fewer meshes). Every number below is
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
