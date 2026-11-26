"""Computational fluid dynamics: flow studies run on OpenFOAM (see :mod:`opencae.cfd.openfoam`).

CalculiX is a structural / thermal solver and is not used for flow. Flow studies are written as OpenFOAM cases
(snappyHexMesh mesh of the fluid, simpleFoam solution) and run by the OpenFOAM backend, which is found on Linux,
or inside WSL on Windows (the Solvers button / ``opencae setup openfoam`` installs it there).
"""
