"""
OctoSlave Lab — dynamic, self-organizing multi-agent research lab.

Replaces the static role pipeline (research.py) with a team that a Director
agent assembles and revises for the problem at hand. A Critic challenges plans
before implementation, specialists run on the shared agent engine, and a human
can inject insight live or let the team run autonomously.

Public entry point: ``run_lab`` (lab.runner).
"""

from .state import AgentSpec, LabSession, LabPhase  # noqa: F401

__all__ = ["AgentSpec", "LabSession", "LabPhase", "run_lab"]


def run_lab(*args, **kwargs):
    """Lazy import wrapper so importing the package is cheap."""
    from .runner import run_lab as _run_lab
    return _run_lab(*args, **kwargs)
