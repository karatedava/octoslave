"""
Meeting orchestration — the heart of the lab (mirrors Stanford's Virtual Lab).

Two meeting types:

  team_meeting        : the whole team DISCUSSES an agenda (no file work). Each
                        specialist contributes in turn, the Critic challenges,
                        the Director synthesizes into decisions + a next agenda.

  individual_meeting  : ONE specialist EXECUTES a concrete task with its tools
                        (via agent_runtime), then the Critic reviews the output;
                        on a "revise" verdict the specialist gets one more pass.

Transcripts are written under lab/meetings/ and indexed in the session.
"""

from __future__ import annotations

import time

from openai import OpenAI

from .. import display
from . import critic as _critic
from . import director as _director
from .agent_runtime import build_agent_system_prompt, run_agent_task
from .llm import complete_text
from .state import AgentSpec, LabSession


def _emit(emit, event: dict):
    if emit:
        try:
            emit(event)
        except Exception:
            pass


def _write_transcript(session: LabSession, n: int, mtype: str, agenda: str,
                      body: str) -> str:
    session.ensure_dirs()
    fname = f"{n:03d}_{mtype}.md"
    path = session.meetings_dir / fname
    header = f"# Meeting #{n:03d} — {mtype}\n\n**Agenda:** {agenda}\n\n"
    try:
        path.write_text(header + body, encoding="utf-8")
    except Exception:
        pass
    rel = f"lab/meetings/{fname}"
    session.add_meeting(n, mtype, agenda, rel)
    return rel


def _agent_opinion(session: LabSession, client: OpenAI, agent: AgentSpec,
                   agenda: str, prior: str) -> str:
    """One specialist's discussion contribution (tool-less)."""
    system = build_agent_system_prompt(agent, session.working_dir)
    user = f"""\
This is a TEAM MEETING (discussion only — do not produce final work yet).

Agenda: {agenda}

Discussion so far:
{prior[:14000] or '(you speak first)'}

Give your contribution as {agent.name} ({agent.role}). Be concrete and brief
(a few sentences to a short list). Focus on your expertise: propose the
approach you'd take, flag risks, or build on/disagree with others. No preamble.
This is discussion only — you have NO tools here; do not emit tool-call syntax,
just reason from the context above."""
    return complete_text(client, agent.model or session.model, system, user,
                         max_tokens=700)


def team_meeting(session: LabSession, client: OpenAI, agenda: str,
                 rounds: int = 2, emit=None) -> tuple[str, str]:
    """Run a team discussion. Returns (transcript, synthesis).

    NOTE: round accounting lives in the runner loop (one round = one
    implementation+review cycle); meetings do NOT bump session.round."""
    n = len(session.meetings) + 1
    _emit(emit, {"type": "meeting_start", "n": n, "meeting_type": "team",
                 "agenda": agenda, "round": session.round})
    display.print_info(f"\n🧠 Team meeting #{n} — {agenda}")

    transcript_parts: list[str] = []
    t0 = time.time()
    for r in range(1, rounds + 1):
        transcript_parts.append(f"\n### Discussion round {r}\n")
        for agent in session.active_team:
            prior = "\n".join(transcript_parts)
            display.print_info(f"  💬 {agent.icon} {agent.name}…")
            opinion = _agent_opinion(session, client, agent, agenda, prior)
            if not opinion:
                opinion = "_(no response)_"
            transcript_parts.append(f"\n**{agent.icon} {agent.name} ({agent.role}):** {opinion}\n")
            _emit(emit, {"type": "meeting_turn", "n": n, "agent": agent.name,
                         "agent_id": agent.id, "text": opinion})

    # Critic challenges the whole discussion.
    discussion = "\n".join(transcript_parts)
    verdict = _critic.critique(session, client, discussion, context=f"Agenda: {agenda}")
    crit_block = (
        f"\n### 🤨 Critic verdict: {verdict['verdict'].upper()}\n"
        f"{verdict['summary']}\n"
        + ("".join(f"- concern: {c}\n" for c in verdict["concerns"]))
        + ("".join(f"- suggestion: {s}\n" for s in verdict["suggestions"]))
    )
    transcript_parts.append(crit_block)
    _emit(emit, {"type": "critic_verdict", "n": n, "verdict": verdict["verdict"],
                 "summary": verdict["summary"], "concerns": verdict["concerns"]})

    # Director synthesizes.
    transcript = "\n".join(transcript_parts)
    synthesis = _director.synthesize(session, client, transcript, agenda)
    transcript_parts.append(f"\n### 🧠 Director synthesis\n{synthesis}\n")

    full = "\n".join(transcript_parts)
    rel = _write_transcript(session, n, "team", agenda, full)
    elapsed = time.time() - t0
    _emit(emit, {"type": "meeting_end", "n": n, "meeting_type": "team",
                 "path": rel, "synthesis": synthesis})
    display.print_info(f"  ✓ Team meeting #{n} done in {elapsed:.0f}s → {rel}")
    return full, synthesis


def individual_meeting(session: LabSession, client: OpenAI, agent: AgentSpec,
                       task: str, context: str = "", emit=None,
                       max_iter: int = 40) -> tuple[str, str, dict]:
    """One specialist executes ``task`` with its tools; the Critic reviews.

    Returns (final_text, transcript_rel_path, critic_verdict)."""
    n = len(session.meetings) + 1
    _emit(emit, {"type": "meeting_start", "n": n, "meeting_type": "individual",
                 "agent": agent.name, "agent_id": agent.id, "agenda": task})
    display.print_info(f"\n🔧 Individual meeting #{n} — {agent.icon} {agent.name}: {task[:80]}")

    messages, final_text = run_agent_task(
        agent, task, session.working_dir, client,
        max_iter=max_iter, context=context, emit=emit,
    )

    # Critic reviews the produced output.
    verdict = _critic.critique(
        session, client,
        proposal=f"Task given to {agent.name}: {task}\n\nResult summary:\n{final_text}",
        context=context,
    )
    _emit(emit, {"type": "critic_verdict", "n": n, "verdict": verdict["verdict"],
                 "summary": verdict["summary"], "concerns": verdict["concerns"]})

    # One revision pass if the critic wants changes (autonomous self-correction).
    if verdict["verdict"] == _critic.REVISE and verdict["concerns"]:
        fix = ("The Critic reviewed your work and asked for revisions:\n"
               + "\n".join(f"- {c}" for c in verdict["concerns"])
               + "\nAddress these now, then summarize what changed.")
        display.print_info(f"  ↻ {agent.name} revising per Critic feedback…")
        more, final_text2 = run_agent_task(
            agent, fix, session.working_dir, client,
            max_iter=max(8, max_iter // 2), context=final_text, emit=emit,
        )
        messages += more
        if final_text2:
            final_text = final_text2

    body = f"**Specialist:** {agent.icon} {agent.name} ({agent.role})\n\n"
    body += f"## Task\n{task}\n\n## Result\n{final_text}\n\n"
    body += (f"## 🤨 Critic verdict: {verdict['verdict'].upper()}\n{verdict['summary']}\n"
             + "".join(f"- {c}\n" for c in verdict["concerns"]))
    rel = _write_transcript(session, n, f"individual_{agent.name.replace(' ', '_')}",
                            task, body)
    _emit(emit, {"type": "meeting_end", "n": n, "meeting_type": "individual",
                 "agent": agent.name, "path": rel})
    return final_text, rel, verdict
