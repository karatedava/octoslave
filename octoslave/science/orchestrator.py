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

import time as _time
from pathlib import Path

from .. import display
from .. import steer
from .. import tools as _tools
from ..agent import continue_agent, last_end_reason, run_agent
from . import workspace as _workspace
from .context import RunContext, clear_context, set_context
from .session import ScienceSession
from . import specialists as _specialists
from . import tools as _science_tools


#: How many times specialist reports may extend one turn. Generous — real work
#: runs in several waves — but finite, so a dispatch-on-every-report cycle ends.
SUPERVISE_ROUND_LIMIT = 40

#: Working memory of the orchestrator, in tokens. Older rounds beyond this are
#: compacted into summaries: what matters long-term lives in the outputs, the
#: provenance ledger and the summaries, and a days-long session stays as quick
#: on its hundredth turn as on its first.
SCIENCE_HISTORY_TOKENS = 200_000

#: Seconds between saves of the orchestrator's history while its loop runs, so
#: a crash or restart mid-turn loses at most this much of what it did.
_HISTORY_SAVE_INTERVAL = 60.0

#: Seconds between re-polls of the jobs still open on record (one round trip to
#: the node each, so not every agent step).
_JOB_POLL_INTERVAL = 120.0


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
    # Without a caller's inbox nobody can steer, but the turn still needs one:
    # specialist reports and the live-history saves ride on it.
    if inbox is None:
        inbox = steer.Inbox()
    from ..agent import set_budget_cap
    set_budget_cap(SCIENCE_HISTORY_TOKENS)
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
    # their previews refresh even if the model edited the file in place. Each is
    # noted with the file's mtime AS COMMENTED, so the end-of-turn pass can tell
    # a file that really changed from one the turn never touched: announcing an
    # update that did not happen is how a researcher comes to distrust the card.
    refresh_ids: dict[str, float] = {}

    def _note_refresh(aid: str) -> None:
        if not aid or aid in refresh_ids:
            return
        art = session.get_artifact(aid)
        try:
            refresh_ids[aid] = Path(art.path).stat().st_mtime if art else 0.0
        except (OSError, TypeError, ValueError):
            # A null/odd path in a hand-edited state.json raises TypeError from
            # Path(), not OSError — and this runs inside the inbox callback,
            # where an exception would take the turn down.
            refresh_ids[aid] = 0.0

    if refresh_artifact_id:
        _note_refresh(refresh_artifact_id)

    def _on_deliver(items: list[dict], to: str, who: str) -> None:
        emit({"type": "science_steer", "ids": [it["id"] for it in items],
              "to": to, "to_name": who})
        for it in items:
            _note_refresh((it.get("meta") or {}).get("artifact_id"))

    # Jobs are only re-polled on a timer: each poll is a round trip to the node,
    # and nothing here is worth a connection per agent step.
    jobs_polled: dict = {"at": 0.0}

    def _job_news() -> str:
        """Re-poll open jobs now and then; report the ones that have ended.

        The orchestrator submits a job and often never polls it again, so it can
        sit waiting for work that finished long ago — and the researcher's Jobs
        panel keeps saying "running". Checking on a timer fixes both.
        """
        now = _time.monotonic()
        if now - jobs_polled["at"] < _JOB_POLL_INTERVAL:
            return ""
        jobs_polled["at"] = now
        try:
            changed = _science_tools.reconcile_jobs(session, emit=emit)
        except Exception:  # noqa: BLE001
            return ""
        if not changed:
            return ""
        lines = [f"- '{j.name}' (id `{j.id}`) is **{j.status}**" for j in changed]
        return ("## Jobs you had not polled\n"
                "The node was asked about the jobs still open on record:\n"
                + "\n".join(lines)
                + "\n\nUse check_cluster_job for the output of any of these you "
                  "still need, and carry on — do not wait on them. A failed or "
                  "unknown one has not produced a result you can rely on: look "
                  "at its log before using anything it wrote.")

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
        # Jobs the orchestrator never polled: tell it the ones that have ended,
        # so it neither waits on finished work nor leaves the panel lying.
        jobs_news = _job_news()
        if jobs_news:
            parts.append(jobs_news)
        if not parts and not crew.any_running():
            # Only when nobody is working and there is no other news — if
            # specialists are running, the orchestrator is right to carry on.
            nudge = solo.nudge()
            if nudge:
                parts.append(nudge)
        return "\n\n---\n\n".join(parts)

    history_saved = {"at": _time.monotonic()}

    def _on_round(live: list[dict]) -> None:
        """Track the loop's live history (it rebinds its list as it goes), so
        every save made mid-turn — a spawn, a job, an output — writes what the
        orchestrator has actually done, and save it now and then on its own."""
        session.messages = live
        now = _time.monotonic()
        if now - history_saved["at"] >= _HISTORY_SAVE_INTERVAL:
            history_saved["at"] = now
            try:
                session.save()
            except Exception:  # noqa: BLE001
                pass

    if inbox is not None:
        inbox.on_deliver = _on_deliver
        inbox.on_round = _on_round
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
            # A resumed session carries the system prompt it was STARTED with —
            # for a project that runs for weeks, that is a persona frozen at
            # turn 1. Two things go stale in it:
            #
            #   * the orchestrator's own instructions, so every later improvement
            #     to how it works (how it delegates, what it presents, how it
            #     keeps the workspace) never reaches a session already running;
            #   * this turn's facts — which models it may give specialists, which
            #     compute node is selected — so it offers a model that is no
            #     longer in the pool, or submits to a node the researcher changed.
            #
            # Everything in it is regenerated from the profile, the config and
            # the session, so rebuilding it loses nothing.
            _refresh_system_prompt(session, wd, pool_note=pool_note,
                                   remote=remote, query=user_message)
            messages = continue_agent(
                session.messages, user_message, model, wd, client,
                permission_mode=permission_mode, remote=None,
                model_pool=fallbacks, prompt_profile="science",
            )
        ended = last_end_reason()
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
                prompt_profile="science",
                # A report or the workspace check is the harness speaking; a late
                # message alone is the researcher's own words.
                user_typed=bool(late) and not reports and not note,
            )
            ended = last_end_reason()
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
        # if the model edited the file in place and didn't re-present it — but
        # only when the file actually moved on. A comment the turn answered in
        # words ("no, the first one was right") changes nothing on disk, and
        # re-emitting then tells the researcher their output was updated when it
        # was not.
        for aid, was in refresh_ids.items():
            art = session.get_artifact(aid)
            if not art:
                continue
            try:
                now_mtime = Path(art.path).stat().st_mtime
            except (OSError, TypeError, ValueError):
                continue
            if now_mtime <= was:
                continue
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
        if ended in _CUT_SHORT:
            # The loop gave up mid-turn. Its last words are then a mid-task
            # remark, or — when it said nothing this turn — the PREVIOUS turn's
            # answer, which used to be shown again as if it were the new one.
            reply = (f"⚠ This turn was cut short: {_CUT_SHORT[ended]} (details "
                     f"above). Everything done so far is saved — send a message "
                     f"(for example “continue”) to pick up where it stopped.")
            emit({"type": "science_reply", "text": reply, "incomplete": True})
        else:
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
            inbox.on_round = None
            steer.unbind()
        # An error ends the turn without the saves above: keep what was done.
        try:
            session.save()
        except Exception:  # noqa: BLE001
            pass
        _science_tools.unregister()
        clear_context()
        display.clear_event_callback()
        set_budget_cap(None)


#: How an orchestrator loop that gave up is reported (see last_end_reason).
_CUT_SHORT = {
    "connection_lost": "the model server stopped responding, and no other model "
                       "could take over",
    "error": "the model endpoint returned an error that could not be worked around",
    "rate_limited": "the model server kept refusing this key's requests (rate limit) "
                    "for too long",
}

#: Most recent specialists named in the resumed orchestrator's team roster.
_ROSTER_LIMIT = 12


def _team_note(session: ScienceSession) -> str:
    """The specialists this project already has, for a RESUMED turn.

    The orchestrator only knows its team from its own conversation, and that is
    summarised as the session grows — so on a long project it eventually cannot
    name the agents it dispatched, and continue_specialist (which would hand work
    back to the one that already knows the ground) becomes unreachable. Then its
    only options are to spawn a fresh agent or do the job itself. The session
    knows the team for certain, so state it.
    """
    people = [s for s in (getattr(session, "specialists", None) or [])
              if (getattr(s, "role", "") or getattr(s, "name", ""))]
    if not people:
        return ""
    lines = []
    for s in people[-_ROSTER_LIMIT:]:
        outcome = " ".join((s.summary or "").split())[:220]
        lines.append(f"- `{s.id}` **{s.name}** ({s.role}) — {s.status}"
                     + (f": {outcome}" if outcome else "")
                     + f"\n  Remit: {' '.join((s.goal or '').split())[:200]}")
    earlier = len(people) - len(lines)
    head = ("## Your team on this project\n"
            "Specialists already dispatched here. To take any of their work "
            "further, continue_specialist(id=…) resumes that agent with its whole "
            "transcript — what it tried, what worked, the dead ends — instead of a "
            "fresh agent that would re-tread it. Dispatch a new specialist for work "
            "none of them covers.\n")
    if earlier > 0:
        head += f"(the {earlier} earlier one(s) are omitted)\n"
    return head + "\n".join(lines)


def _refresh_system_prompt(session: ScienceSession, wd: str, *, pool_note: str,
                           remote: dict | None, query: str) -> None:
    """Rebuild the orchestrator's system message for this turn, in place.

    Assembled exactly as ``run_agent`` assembles it for turn 1 (profile, then the
    compute-node note, then the caller's extra context, then project memory), so
    a session resumed next month runs the current orchestrator rather than the
    one it was created with. Does nothing if the history has no system message to
    replace, and never raises: a stale prompt is survivable, a lost turn is not.
    """
    msgs = session.messages
    if not msgs or msgs[0].get("role") != "system":
        return
    try:
        from ..agent import load_session_memory, load_system_prompt
        from ..config import remote_awareness_note

        prompt = load_system_prompt("science", wd)
        note = remote_awareness_note(active=remote, job_submission=True)
        if note:
            prompt += "\n\n" + note
        if pool_note.strip():
            prompt += "\n\n" + pool_note.strip()
        roster = _team_note(session)
        if roster:
            prompt += "\n\n" + roster
        try:
            memory = load_session_memory(wd, query=query, remote=None)
        except Exception:  # noqa: BLE001 — memory is an optional extra
            memory = ""
        if memory:
            prompt += f"\n\n{memory}"
    except Exception:  # noqa: BLE001
        return
    if prompt.strip():
        msgs[0] = {"role": "system", "content": prompt}


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
