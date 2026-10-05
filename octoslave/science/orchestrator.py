"""The Science orchestrator turn — one exchange in the research conversation.

Reuses the standard agent loop (``octoslave.agent``): turn 1 boots it with the
``science`` prompt profile (which establishes the orchestrator persona and its
FAIR/HPC/specialist workflow); later turns continue the same message history, so
the persona and dynamic science tools persist. Science capability tools are
registered for the duration of the turn.

The user may keep messaging while a turn runs (live steering, see
``octoslave.steer``): the web layer queues those messages in the turn's
``inbox``, the agent loop folds them in at its next step, and this module keeps
the turn going until the inbox is empty — so nothing sent mid-run is dropped.
"""

from __future__ import annotations

from .. import display
from .. import steer
from .. import tools as _tools
from ..agent import continue_agent, run_agent
from . import workspace as _workspace
from .context import RunContext, clear_context, set_context
from .session import ScienceSession
from . import specialists as _specialists
from . import tools as _science_tools


#: How many times specialist reports may extend one turn. Generous — real work
#: runs in several waves — but finite, so a dispatch-on-every-report cycle ends.
SUPERVISE_ROUND_LIMIT = 40


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
    inbox: "steer.Inbox | None" = None,
) -> str:
    """Run one orchestrator turn against ``user_message`` and return its reply.

    ``refresh_artifact_id`` — set when the turn is a refinement of a specific
    presented output. After the turn we re-emit that artifact so the UI refreshes
    its (cache-busted) preview and scrolls to it, even if the model updated the
    file in place without calling present_output again.

    ``inbox`` — live messages the user sends while this turn runs. Each one is
    delivered at the orchestrator's next step (or relayed to a running specialist
    as an FYI first); the turn only ends once the inbox is empty and closed.
    """
    emit = emit or (lambda ev: None)
    pool = [m for m in (specialist_models or []) if m]
    crew = _specialists.Pool()
    solo = _specialists.SoloWatch()

    # While the orchestrator answers the end-of-turn workspace check, its text is
    # held back one message at a time: a bare "nothing to change" is about a note
    # the researcher never saw and must not appear in their feed. Anything else is
    # released when its message ends, before the tool calls that follow it.
    quiet: dict = {"on": False, "held": []}

    def watched(ev: dict) -> None:
        """Count the orchestrator's own tool calls on the way past (a
        specialist's events carry an agent tag, so they are not counted)."""
        if isinstance(ev, dict) and ev.get("type") == "tool_call" \
                and not ev.get("agent_name"):
            solo.note_tool(ev.get("name") or "")
        if quiet["on"] and isinstance(ev, dict) and not ev.get("agent_name") \
                and ev.get("type") in ("stream_start", "token", "reasoning", "stream_end"):
            quiet["held"].append(ev)
            if ev["type"] != "stream_end":
                return
            held, quiet["held"] = quiet["held"], []
            said = "".join(e.get("text") or "" for e in held if e["type"] == "token")
            if not _workspace.is_nothing_to_change(said):
                for e in held:
                    emit(e)
            return
        emit(ev)
    ctx = RunContext(session=session, client=client, model=model, emit=watched,
                     permission_mode=permission_mode, specialist_models=pool,
                     inbox=inbox, pool=crew)
    set_context(ctx)
    _science_tools.register()
    display.set_event_callback(watched)
    # Outputs the user commented on during this turn — re-emitted at the end so
    # their previews refresh even if the model edited the file in place.
    refresh_ids: list[str] = [refresh_artifact_id] if refresh_artifact_id else []

    def _on_deliver(items: list[dict], to: str, who: str) -> None:
        emit({"type": "science_steer", "ids": [it["id"] for it in items],
              "to": to, "to_name": who})
        for it in items:
            aid = (it.get("meta") or {}).get("artifact_id")
            if aid and aid not in refresh_ids:
                refresh_ids.append(aid)

    def _news_mid_loop() -> str:
        """News for the orchestrator at its next step, while its loop is running:
        a specialist's report, or a reminder that it has been working alone for a
        long stretch.

        Only reached from inside the agent loop (``steer.deliver``). The nudge
        must NOT be a reason to restart a finished turn — the restarted loop would
        work alone again, earn another nudge, and never stop — so the supervise
        loop uses ``_reports_only`` instead.
        """
        parts = []
        done = crew.collect()
        if done:
            parts.append(_specialists.format_reports(done, crew))
        elif not crew.any_running():
            # Only when nobody is working — if specialists are already running,
            # the orchestrator is doing the right thing by carrying on.
            nudge = solo.nudge()
            if nudge:
                parts.append(nudge)
        return "\n\n---\n\n".join(parts)

    if inbox is not None:
        inbox.on_deliver = _on_deliver
        # Specialist reports ride the same channel as live messages, so the
        # orchestrator hears about a finished specialist at its next step even
        # while it is busy with other work — not only once it goes idle.
        inbox.reports = _news_mid_loop
        steer.bind(inbox)
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
        # The workspace as this turn found it: the end-of-turn check reports only
        # what the turn itself changed, never the user's existing layout.
        try:
            start_state = _workspace.baseline(wd)
        except Exception:
            start_state = None
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
        from .. import interrupt as _interrupt
        # Supervise the rest of the turn. Two things can still need the
        # orchestrator after its own loop ends:
        #
        #   * a message the researcher sent in the gap (close_if_empty is atomic
        #     with the web layer's post(), so it is either delivered here or
        #     refused — and the web layer then starts a fresh turn with it);
        #   * a background specialist finishing, whose report the orchestrator
        #     must review and pass on.
        #
        # So the turn stays alive while specialists work: the orchestrator is
        # woken by whichever lands first, reacts, and only ends once nothing is
        # queued and nobody is still working.
        # Each report hands the orchestrator another round, and that round may
        # dispatch again — which is how real multi-stage work proceeds, but it is
        # also a cycle with nothing stopping it. Cap the rounds so a turn cannot
        # run forever, and spend the last one asking for a close rather than
        # cutting off silently.
        rounds = 0
        checked_workspace = False

        def workspace_note() -> str:
            """The end-of-turn workspace check (once per turn): a deliverable
            the work has outrun, a page nobody looked at, directories the turn
            left empty or split. Each is invisible from inside the conversation,
            and the researcher otherwise has to notice and ask. Reported only
            for what THIS turn changed (see start_state)."""
            if start_state is None or rounds >= SUPERVISE_ROUND_LIMIT:
                return ""
            try:
                return _workspace.wrap_up_note(session, wd, viewed_at=_tools.viewed_at,
                                               since=start_state)
            except Exception:
                return ""

        while not _interrupt.should_stop():
            # Look at who is still working BEFORE collecting: a specialist that
            # finishes in between is then picked up on the next pass. The other
            # order would see "none left running" after collecting nothing, and
            # end the turn with that report never delivered.
            busy = crew.any_running()
            reports = crew.collect()
            late: list[dict] = inbox.take() if inbox is not None else []
            note = ""
            if not reports and not late:
                if busy:
                    crew.wait(2.0)      # park until there is news, then re-check
                    continue
                # Nothing left to react to. Before ending, the workspace check —
                # INSIDE this loop, so a specialist the orchestrator dispatches
                # to fix something is still supervised, and live steering still
                # works during that round.
                if not checked_workspace:
                    checked_workspace = True
                    note = workspace_note()
                if not note:
                    # Closing the inbox ends live steering, so only now, when the
                    # turn genuinely ends. close_if_empty is atomic with the web
                    # layer's post(): a message that slipped in is handed back
                    # here rather than lost, and the turn carries on with it.
                    late = inbox.close_if_empty() if inbox is not None else []
                    if not late:
                        break
            parts = []
            if reports:
                parts.append(_specialists.format_reports(reports, crew))
            if late:
                _on_deliver(late, "orchestrator", "")
                parts.append(steer.format_for_orchestrator(late))
            if note:
                parts.append(note)
            rounds += 1
            before = len(messages)
            quiet["on"] = bool(note)
            if rounds > SUPERVISE_ROUND_LIMIT:
                crew.cancel()
                parts.append(
                    f"[This turn has been extended {SUPERVISE_ROUND_LIMIT} times by "
                    f"specialist reports — long enough that it is now being closed.] "
                    f"Stop dispatching, and give the researcher what you have: what "
                    f"was established, what each output shows, and what is still "
                    f"open. They can carry on in their next message.")
            messages = continue_agent(
                messages, "\n\n---\n\n".join(parts), model, wd, client,
                permission_mode=permission_mode, remote=None, model_pool=fallbacks,
                # A report or the workspace check is the harness speaking; a late
                # message alone is the researcher's own words.
                user_typed=bool(late) and not reports and not note,
            )
            quiet["on"] = False
            if note and _workspace.is_nothing_to_change(_last_assistant_text(messages,
                                                                              raw=True)):
                # Nothing needed doing. If the model only replied, drop the note
                # and the reply: the history (and the next turn) should not carry
                # an exchange about a note the researcher never saw. The closing
                # reply stays the turn's answer either way (_last_assistant_text
                # skips the dismissal).
                if not any(m.get("role") == "tool" for m in messages[before:]):
                    messages = messages[:before]
            session.messages = messages
            if rounds > SUPERVISE_ROUND_LIMIT:
                break
        # Outputs reach the chat only when the orchestrator explicitly calls
        # present_output — the model decides what's worth showing (not every
        # intermediate file). See prompt_profiles/science.md.
        # For a refinement, make sure the refined output's preview refreshes even
        # if the model edited the file in place and didn't re-present it.
        for aid in refresh_ids:
            art = session.get_artifact(aid)
            if art:
                emit({"type": "science_artifact", "id": art.id, "rel": art.rel,
                      "path": art.path, "caption": art.caption, "kind": art.kind,
                      "provenance": art.provenance, "interim": art.interim,
                      "refreshed": True})
        # A mid-tool stop unwinds the loop cleanly (history preserved and already
        # annotated), so the turn returns normally — check the flag rather than
        # relying on an exception, or a stop would be reported as a normal reply.
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
        # Nothing may keep running once the turn is over. On a Stop the
        # specialists already follow the orchestrator's stop signal; on an error
        # they would not, so end them explicitly (their own signal — this
        # thread is unaffected) and give them a moment to save their progress.
        if crew.any_running():
            crew.cancel()
            for r in crew.join(timeout=20.0):
                try:
                    session.update_specialist(
                        r.id, status="done",
                        summary="⚠ INCOMPLETE — still running when the turn ended. "
                                "Resume it with continue_specialist.")
                except Exception:
                    pass
                emit({"type": "science_specialist", "event": "done", "id": r.id,
                      "status": "done", "summary": "did not finish (resumable)"})
        if inbox is not None:
            # Stop / error: whatever never reached the agent is reported back so
            # the UI can say so (and offer the text again) instead of implying it
            # was acted on.
            dropped = inbox.close()
            if dropped:
                emit({"type": "science_steer", "ids": [it["id"] for it in dropped],
                      "dropped": True})
            inbox.on_deliver = None
            inbox.reports = None
            steer.unbind()
        _science_tools.unregister()
        clear_context()
        display.clear_event_callback()


def _last_assistant_text(messages: list[dict], raw: bool = False) -> str:
    """The orchestrator's last words — the turn's answer. A "nothing to change"
    reply to the workspace check is skipped (unless ``raw``): the researcher
    never saw that note, so the closing reply before it stays the answer."""
    for m in reversed(messages):
        if m.get("role") == "assistant":
            c = m.get("content")
            if isinstance(c, str) and c.strip():
                if not raw and _workspace.is_nothing_to_change(c):
                    continue
                return c.strip()
    return ""
