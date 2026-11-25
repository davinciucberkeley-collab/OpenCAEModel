"""Multibody-dynamics backend: MuJoCo (Apache-2.0). Availability, version and the error type.

MuJoCo is used as a rigid-body dynamics engine only (tree of bodies, hinge / slide joints, equality
constraints, RNE for joint reactions). No contacts, no geometry, no actuator models: every force on a
body is an explicit assembly input (gravity, effort driver, applied load).
"""
from __future__ import annotations


class MBDModelError(Exception):
    """The assembly / study cannot be simulated. ``problems`` lists every issue found (readable sentences)."""

    def __init__(self, message: str, problems: list[str] | None = None) -> None:
        super().__init__(message)
        self.problems = list(problems) if problems else [message]


class MBDCancelled(Exception):
    """The run was cancelled by the caller."""


def available() -> tuple[bool, str]:
    """(True, "") when MuJoCo can be imported, else (False, reason)."""
    try:
        import mujoco  # noqa: F401
    except Exception as exc:  # ImportError, or a broken binary wheel
        return False, f"MuJoCo is not available ({type(exc).__name__}: {exc}); install it with: pip install mujoco"
    return True, ""


def version() -> str:
    try:
        import mujoco
        return str(mujoco.__version__)
    except Exception:
        return ""
