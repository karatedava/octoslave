"""
The Director (Principal Investigator) agent.

The Director never touches the file system directly. It reasons about the
problem and the lab's progress, and makes structural decisions:

  - ``assemble_team``  : design ≤10 specialists tailored to the task
  - ``revise_team``    : add / modify / discard agents mid-run
  - ``synthesize``     : turn a meeting transcript into decisions + next agenda
  - ``next_phase``     : decide what the lab should do next

All calls are tool-less, structured-JSON completions (see lab.llm).
"""

from __future__ import annotations

from openai import OpenAI

from ..tools import all_tool_definitions
from .llm import complete_json, complete_text
from .state import AgentSpec, LabSession


MAX_TEAM = 10


def tool_catalog() -> str:
    """A compact 'name — description' catalog of grantable tools for prompts."""
    seen, lines = set(), []
    for t in all_tool_definitions():
        fn = t.get("function", {})
        name = fn.get("name", "")
        if not name or name in seen:
            continue
        seen.add(name)
        desc = (fn.get("description", "") or "").strip().split("\n")[0][:110]
        lines.append(f"- {name}: {desc}")
    return "\n".join(sorted(lines))


_DIRECTOR_SYSTEM = (
    "You are the Director (Principal Investigator) of an autonomous research "
    "lab. You lead a small team of AI specialist agents and a human "
    "collaborator to deliver real, rigorous results on ANY problem — "
    "scientific or not. You are domain-agnostic: assemble whatever expertise "
    "the specific task demands. You are decisive, skeptical of hand-waving, and "
    "obsessed with producing verifiable deliverables, not plans about plans. "
    "You have NO tools in this step — do not call tools or emit any tool-call "
    "syntax; reason only from the information provided."
)


def assemble_team(session: LabSession, client: OpenAI,
                  files_summary: str = "") -> tuple[list[AgentSpec], str]:
    """Design the initial team for the task. Returns (team, agenda)."""
    catalog = tool_catalog()
    # If a specialist-model pool is configured, the Director assigns each agent a
    # model from it; otherwise every agent inherits the session model.
    pool = [m for m in (session.specialist_models or []) if m]
    model_field = (',\n      "model": "<one of the specialist models listed below>"'
                   if pool else "")
    user = f"""\
The human has given the lab this task:

\"\"\"{session.task}\"\"\"

Working directory: {session.working_dir}
{('Orientation brief (a survey of the working directory — data, prior work, the real objective):\\n' + files_summary) if files_summary else 'No orientation available yet.'}

Design the specialist team. Rules:
- 3 to {MAX_TEAM} specialists. Each must be genuinely necessary; no filler roles.
- Tailor roles to THIS task's domain (could be biology, ML, finance, law,
  writing, software, logistics — anything).
- Every specialist that needs to DO work (not just advise) must be granted the
  right tools from the catalog below. Discussion-only advisors get an empty
  tool list. Always include at least one agent able to write files and run code
  if the task needs implementation.
- Do NOT create a Director or a Critic — those exist separately.

Available tools you may grant (you can request more at runtime later):
{catalog}

Respond as JSON:
{{
  "agenda": "<2-4 sentence plan of attack for the first working session>",
  "team": [
    {{"name": "<short name>", "role": "<one-line title>",
      "expertise": "<what they're expert in>",
      "goal": "<what they will accomplish on this team>",
      "tools": ["tool_name", ...],
      "icon": "<one emoji>"{model_field}}}
  ]
}}"""

    if pool:
        user = user + (
            "\n\nSpecialist model pool — set each team member's \"model\" to one of "
            "these (match model strength to the role; you, the Director, run on "
            f"{session.model}):\n" + "\n".join(f"- {m}" for m in pool))

    # Make the Director aware of a configured compute node so it plans heavy steps
    # as offloaded jobs (and knows results come back locally for the report).
    try:
        from ..config import remote_awareness_note
        from ..science.tools import active_compute_node
        _node = active_compute_node()
        if _node is not None:
            _note = remote_awareness_note(active=_node, job_submission=True)
            if _note:
                user = user + "\n\n" + _note
    except Exception:
        pass

    data = complete_json(client, session.model, _DIRECTOR_SYSTEM, user, max_tokens=3000)
    team: list[AgentSpec] = []
    agenda = ""
    if isinstance(data, dict):
        agenda = str(data.get("agenda", "")).strip()
        for item in (data.get("team") or [])[:MAX_TEAM]:
            if not isinstance(item, dict):
                continue
            # Model from the pool if the Director picked a valid one; else default.
            chosen = str(item.get("model", "")).strip()
            spec_model = chosen if (pool and chosen in pool) else session.model
            team.append(AgentSpec(
                name=str(item.get("name", "Specialist"))[:60],
                role=str(item.get("role", ""))[:120],
                expertise=str(item.get("expertise", ""))[:400],
                goal=str(item.get("goal", ""))[:400],
                tools=[str(t) for t in (item.get("tools") or []) if isinstance(t, str)],
                icon=str(item.get("icon", "🧪"))[:4] or "🧪",
                model=spec_model,
            ))
    return team, agenda


def synthesize(session: LabSession, client: OpenAI, transcript: str,
               agenda: str) -> str:
    """Summarize a meeting transcript into decisions + the next agenda.

    Returns the synthesis text; also updates ``session.agenda`` with the
    director's chosen next focus.
    """
    user = f"""\
Task: {session.task}

The team just held a meeting on this agenda:
{agenda}

Transcript / contributions:
{transcript[:24000]}

As Director, write a concise synthesis:
1. Key decisions made.
2. What concretely should be implemented or done next (specific, assignable).
3. Open risks/unknowns.
End with a line beginning 'NEXT AGENDA:' giving the focused agenda for the next
working session."""
    text = complete_text(client, session.model, _DIRECTOR_SYSTEM, user, max_tokens=1500)
    for line in text.splitlines():
        if line.strip().upper().startswith("NEXT AGENDA:"):
            session.agenda = line.split(":", 1)[1].strip()
            break
    session.touch()
    return text


def next_phase(session: LabSession, client: OpenAI, progress: str,
               review: str = "") -> dict:
    """Decide what the lab should do next. Returns a decision dict:
    {"action": "continue"|"report"|"revise_team", "reason": str,
     "next_agenda": str}.

    ``review`` is the latest team-review/critic synthesis; the next agenda must
    explicitly close any unresolved BLOCKING concerns it raised (so the critic's
    feedback drives convergence instead of being re-discovered every round)."""
    review_block = (
        f"\nLatest review & critic synthesis (UNRESOLVED concerns here are the "
        f"priority — the next agenda must close them, not restate generic goals):\n"
        f"{review[:6000]}\n" if review.strip() else "")
    user = f"""\
Task: {session.task}
Completed round: {session.round}

Progress so far (artifacts, latest findings):
{progress[:14000]}
{review_block}
Decide the lab's next move. Options:
- "continue": another implementation round is worthwhile — give the next agenda.
- "report": enough has been achieved (or further rounds won't help); write the
  final report.
- "revise_team": the team is mis-matched to the work; explain how to change it.

If you "continue", the next_agenda MUST ADVANCE the project to the next concrete
stage based on what already exists above. Do NOT repeat work that is already
done (e.g. if data has been fetched and a schema/baseline exist, move on to full
curation, rule mining, validation, or the report — do not re-run data discovery,
schema drafting, or repo setup). Name the specific next deliverables.

Respond as JSON:
{{"action": "continue|report|revise_team",
  "reason": "<one or two sentences>",
  "next_agenda": "<the NEXT-STAGE agenda if continuing, else empty>"}}"""
    data = complete_json(client, session.model, _DIRECTOR_SYSTEM, user, max_tokens=800)
    if not isinstance(data, dict):
        return {"action": "report", "reason": "Could not parse decision; defaulting to report.",
                "next_agenda": ""}
    action = str(data.get("action", "report")).strip().lower()
    if action not in ("continue", "report", "revise_team"):
        action = "report"
    return {"action": action,
            "reason": str(data.get("reason", "")),
            "next_agenda": str(data.get("next_agenda", "")).strip()}


def classify_followup(session: LabSession, client: OpenAI, feedback: str,
                      progress: str) -> dict:
    """The lab has COMPLETED and the human sent follow-up feedback. Decide how to
    act on it. Returns:
    {"action": "report_fix"|"more_rounds", "reason": str, "agenda": str}.

    - report_fix: a small/cosmetic change to the report only → re-run the reporter.
    - more_rounds: substantive — needs new analysis/data/experiments → resume
      implementation rounds with a focused agenda.
    """
    user = f"""\
The lab already COMPLETED this task: {session.task}

Progress / artifacts so far:
{progress[:12000]}

The human reviewed the results and sent this follow-up:
\"{feedback}\"

Decide the smallest action that satisfies it:
- "report_fix": purely a change to the written report (wording, structure, adding
  a section/table from EXISTING results, formatting). No new analysis needed.
- "more_rounds": substantive — requires new data, analysis, experiments, or
  results before the report can change.

Respond as JSON:
{{"action": "report_fix|more_rounds",
  "reason": "<one sentence>",
  "agenda": "<focused agenda for the work if more_rounds, else the exact report change to make>"}}"""
    data = complete_json(client, session.model, _DIRECTOR_SYSTEM, user, max_tokens=600)
    if not isinstance(data, dict):
        return {"action": "report_fix", "reason": "Defaulting to a report fix.",
                "agenda": feedback}
    action = str(data.get("action", "report_fix")).strip().lower()
    if action not in ("report_fix", "more_rounds"):
        action = "report_fix"
    return {"action": action,
            "reason": str(data.get("reason", "")),
            "agenda": str(data.get("agenda", "")).strip() or feedback}


def revise_team(session: LabSession, client: OpenAI, reason: str) -> list[AgentSpec]:
    """Produce a revised team given a reason. Returns the new full team list.

    The director may keep, modify, add, or drop agents (capped at MAX_TEAM).
    """
    catalog = tool_catalog()
    current = "\n".join(
        f"- {a.name} ({a.role}); tools: {', '.join(a.tools) or 'none'}"
        for a in session.active_team
    ) or "(empty)"
    user = f"""\
Task: {session.task}
Reason to revise the team: {reason}

Current team:
{current}

Available tools:
{catalog}

Propose the REVISED full team (3..{MAX_TEAM} specialists). Keep what works, drop
what's unneeded, add what's missing.
Tooling rule (applies to every agent, especially ones you ADD now): each
specialist that needs to DO work must be granted the tools to actually do it —
anything that must run code/commands or produce computed outputs needs `bash`
(and other relevant tools); discussion-only advisors get an empty tool list.
Never create a do-er (engineer, runner, analyst, pipeline, "execution"
specialist) without the execution tools its job requires.
Same JSON schema as team assembly:
{{"agenda": "<updated agenda>", "team": [{{"name","role","expertise","goal","tools":[...],"icon"}}]}}"""
    data = complete_json(client, session.model, _DIRECTOR_SYSTEM, user, max_tokens=3000)
    if not isinstance(data, dict):
        return session.team
    new_team: list[AgentSpec] = []
    for item in (data.get("team") or [])[:MAX_TEAM]:
        if not isinstance(item, dict):
            continue
        new_team.append(AgentSpec(
            name=str(item.get("name", "Specialist"))[:60],
            role=str(item.get("role", ""))[:120],
            expertise=str(item.get("expertise", ""))[:400],
            goal=str(item.get("goal", ""))[:400],
            tools=[str(t) for t in (item.get("tools") or []) if isinstance(t, str)],
            icon=str(item.get("icon", "🧪"))[:4] or "🧪",
            model=session.model,
        ))
    if data.get("agenda"):
        session.agenda = str(data["agenda"]).strip()
    return new_team or session.team
