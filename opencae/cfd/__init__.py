"""Computational fluid dynamics through an external solver adapter (SU2).

CalculiX is a structural / thermal solver and is not used for flow. Flow studies are
written for SU2's incompressible finite-volume solver (INC_NAVIER_STOKES / INC_RANS),
which runs as a separate executable discovered and launched in an isolated
environment like CalculiX.
"""
