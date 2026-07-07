"""
OctoSlave Science — a conversational research orchestrator.

Where the Lab (``octoslave.lab``) runs an autonomous team as a batch pipeline,
Science is *chat-first*: an orchestrator agent talks with the researcher, spins
up specialists on demand, submits and tracks jobs on remote clusters, presents
plots/tables inline for comment-driven refinement, curates messy data into FAIR
datasets, and searches the literature — all from the web UI ``/science`` tab.

It reuses the proven agent loop (``octoslave.agent``) driven by the ``science``
prompt profile, plus a small set of science capabilities registered as dynamic
tools while a session is active (the same mechanism the Lab foundry uses).
"""

from .session import ScienceSession
from .orchestrator import run_science_turn

__all__ = ["ScienceSession", "run_science_turn"]
