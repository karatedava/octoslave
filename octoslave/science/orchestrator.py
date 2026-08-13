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
    specialist_models: list | None = None,
) -> str:
    """Run one orchestrator turn against ``user_message`` and return its reply.

    ``refresh_artifact_id`` — set when the turn is a refinement of a specific
    presented output. After the turn we re-emit that artifact so the UI refreshes
    its (cache-busted) preview and scrolls to it, even if the model updated the
    file in place without calling present_output again.
    """
    emit = emit or (lambda ev: None)
    pool = [m for m in (specialist_models or []) if m]
    ctx = RunContext(session=session, client=client, model=model, emit=emit,
                     permission_mode=permission_mode, specialist_models=pool)
    set_context(ctx)
    _science_tools.register()
    display.set_event_callback(emit)
    # Tell the orchestrator which models it may assign to specialists. Injected on
    # turn 1's system prompt; it persists in the message history for later turns.
    if pool:
        pool_note = (
            "## Specialist model pool\n"
            "When you spawn_specialist, you may set `model` to one of these "
            "configured models — pick per task (e.g. a strong reasoner for hard "
            "analysis, a fast/cheap model for routine work):\n"
            + "\n".join(f"- {m}" for m in pool)
            + f"\nOmit `model` to use the default ({pool[0]}). You run on {model}."
            + "\nThese are the ONLY valid values: a model id from anywhere else "
              "(however familiar) does not exist on this backend and will be refused."
        )
    else:
        # Without this, models tend to invent a plausible-sounding id ("gpt-4.1")
        # for spawn_specialist's `model`, which fails on the specialist's first call.
        pool_note = (
            "## Specialist models\n"
            f"No specialist pool is configured for this session: every specialist "
            f"you spawn runs on {model}, the same model as you. Do NOT pass `model` "
            f"to spawn_specialist — there is no other model to choose from."
        )
    try:
        wd = session.working_dir
        # The orchestrator itself runs LOCALLY (fast; artifacts render in the tab).
        # A selected remote is the HEAVY-COMPUTE cluster, reached only through the
        # cluster-job tools (submit_cluster_job uses session.remote_id) — big files
        # stay on the node, lightweight results are fetched back. So we do NOT route
        # the orchestrator's own tools to the remote (remote=None below); the
        # remote choice is carried on session.remote_id (set by the caller).
        # If the orchestrator's own model stops responding (or its endpoint starts
        # rejecting what it produces), finish the turn on another model from the
        # configured pool rather than dropping the conversation.
        fallbacks = [m for m in pool if m != model]
        if not session.messages:
            messages = run_agent(
                user_message, model, wd, client,
                prompt_profile="science", permission_mode=permission_mode,
                enable_plan=False, enable_verify=False, enable_memory=True,
                remote=None, compute_node=remote,
                extra_system=pool_note, model_pool=fallbacks,
            )
        else:
            messages = continue_agent(
                session.messages, user_message, model, wd, client,
                permission_mode=permission_mode, remote=None,
                model_pool=fallbacks,
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
        # A mid-tool stop unwinds the loop cleanly (history preserved and already
        # annotated), so the turn returns normally — check the flag rather than
        # relying on an exception, or a stop would be reported as a normal reply.
        from .. import interrupt as _interrupt
        if _interrupt.should_stop():
            session.messages = messages
            session.save()
            emit({"type": "science_reply", "stopped": True, "text": (
                "⏹ Stopped by you. Any running command was killed and the work so "
                "far is saved — send a message to carry on from here.")})
            return ""
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
