"""Per-run context for the Science orchestrator.

The science capability tools (spawn_specialist, cluster jobs, …) are plain
dynamic tools with signature ``run(args, working_dir) -> (text, ok)`` — they
can't take a client/model/session directly, so those are stashed here for the
duration of a run.

The context is PER THREAD. Several Science sessions can run at once (one turn per
session, but nothing stops two sessions in two tabs), and a single module-level
context let the second turn overwrite the first: the first session's outputs and
specialists then landed in the second session, and when either turn ended the
other's tools lost their context mid-run. A turn sets its context on its own
thread; each background specialist sets the same context on its thread (see
``science.tools._run_specialist``). A thread with no context — e.g. a chat agent
that happens to see the science tools — gets None, never another session's.

Specialists run on their OWN threads (see ``science.specialists``), so several
agents emit through this one context at the same time. Who is speaking is
therefore thread-local: :func:`emit` stamps each event with the tag belonging to
the thread it was called on, which is what lets the UI attribute two concurrent
specialists correctly.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .session import ScienceSession


@dataclass
class RunContext:
    session: ScienceSession
    client: Any
    model: str
    emit: Callable[[dict], None]
    permission_mode: str = "autonomous"
    # Pool of models the orchestrator may assign to spawned specialists. Empty →
    # specialists use the orchestrator ``model``.
    specialist_models: list = field(default_factory=list)
    # Live user messages for this turn (steer.Inbox) — bound on each specialist
    # thread so a specialist sees steering as an FYI while it works.
    inbox: Any = None
    # Background specialist runs (science.specialists.Pool), set by the
    # orchestrator for the duration of the turn.
    pool: Any = None


_tl = threading.local()


def set_context(ctx: RunContext | None) -> None:
    _tl.ctx = ctx


def clear_context() -> None:
    _tl.ctx = None


def current() -> Optional[RunContext]:
    return getattr(_tl, "ctx", None)


# -- who is speaking on THIS thread ----------------------------------------

def set_speaker(tag: dict | None) -> None:
    """Tag every event emitted from this thread as coming from one agent."""
    _tl.speaker = tag or None


def speaker() -> dict:
    return getattr(_tl, "speaker", None) or {}


def emit(event: dict) -> None:
    ctx = current()
    if not (ctx and ctx.emit):
        return
    tag = speaker()
    if tag and isinstance(event, dict) and "agent_name" not in event:
        event = {**event, **tag}
    try:
        ctx.emit(event)
    except Exception:
        pass
