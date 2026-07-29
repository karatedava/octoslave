"""Per-run context for the Science orchestrator.

The science capability tools (spawn_specialist, cluster jobs, …) are plain
dynamic tools with signature ``run(args, working_dir) -> (text, ok)`` — they
can't take a client/model/session directly, so those are stashed here for the
duration of a run. The web layer runs Science turns one at a time (guarded by
``state["running"]``), so a single module-level context is safe.
"""

from __future__ import annotations

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


_CTX: Optional[RunContext] = None


def set_context(ctx: RunContext) -> None:
    global _CTX
    _CTX = ctx


def clear_context() -> None:
    global _CTX
    _CTX = None


def current() -> Optional[RunContext]:
    return _CTX


def emit(event: dict) -> None:
    ctx = _CTX
    if ctx and ctx.emit:
        try:
            ctx.emit(event)
        except Exception:
            pass
