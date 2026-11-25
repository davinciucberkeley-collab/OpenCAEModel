"""Mechanical assembly: placed components, rigid bodies, joints, couplings, drivers and applied loads.

``model`` is pure data; ``massprops`` computes mass properties from the OpenCASCADE B-rep. The motion solver
lives in :mod:`opencae.mbd`.
"""
from opencae.assembly.model import (GROUND, AppliedLoad, Assembly, Component, Coupling, Driver, Joint,  # noqa: F401
                                    MassProps, MBDSettings, Profile, RigidBody, ThrustModel, get_mbd, set_mbd)
