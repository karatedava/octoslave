"""The Science orchestrator turn — one exchange in the research conversation.

Reuses the standard agent loop (``octoslave.agent``): turn 1 boots it with the
``science`` prompt profile (which establishes the orchestrator persona and its
FAIR/HPC/specialist workflow); later turns continue the same message history, so
the persona and dynamic science tools persist. Science capability tools are
registered for the duration of the turn.
"""

from __future__ import annotations

from .. import display
from ..agent import continue_agent, run_agent
from .context import RunContext, clear_context, set_context
from .session import ScienceSession
from . import tools as _science_tools


def run_science_turn(
    session: ScienceSession,
    user_message: str,
    client,
    model: str,
    *,
    permission_mode: str = "autonomous",
    emit=None,
    remote: dict | None = None,
    refresh_artifact_id: str | None = None,
) -> str:
    """Run one orchestrator turn against ``user_message`` and return its reply.

    ``refresh_artifact_id`` — set when the turn is a refinement of a specific
    presented output. After the turn we re-emit that artifact so the UI refreshes
    its (cache-busted) preview and scrolls to it, even if the model updated the
    file in place without calling present_output again.
    """
    emit = emit or (lambda ev: None)
    ctx = RunContext(session=session, client=client, model=model, emit=emit,
                     permission_mode=permission_mode)
    set_context(ctx)
    _science_tools.register()
    display.set_event_callback(emit)
    try:
        wd = session.working_dir
        if not session.messages:
            messages = run_agent(
                user_message, model, wd, client,
                prompt_profile="science", permission_mode=permission_mode,
                enable_plan=False, enable_verify=False, enable_memory=True,
                remote=remote,
            )
        else:
            messages = continue_agent(
                session.messages, user_message, model, wd, client,
                permission_mode=permission_mode, remote=remote,
            )
        session.messages = messages
        # Outputs reach the chat only when the orchestrator explicitly calls
        # present_output — the model decides what's worth showing (not every
        # intermediate file). See prompt_profiles/science.md.
        # For a refinement, make sure the refined output's preview refreshes even
        # if the model edited the file in place and didn't re-present it.
        if refresh_artifact_id:
            art = session.get_artifact(refresh_artifact_id)
            if art:
                emit({"type": "science_artifact", "id": art.id, "rel": art.rel,
                      "path": art.path, "caption": art.caption, "kind": art.kind,
                      "provenance": art.provenance, "refreshed": True})
        reply = _last_assistant_text(messages)
        emit({"type": "science_reply", "text": reply})
        session.save()
        try:
            from . import index as _index
            _index.record(session.working_dir, session.task)
        except Exception:
            pass
        return reply
    finally:
        _science_tools.unregister()
        clear_context()
        display.clear_event_callback()


def _last_assistant_text(messages: list[dict]) -> str:
    for m in reversed(messages):
        if m.get("role") == "assistant":
            c = m.get("content")
            if isinstance(c, str) and c.strip():
                return c.strip()
    return ""
