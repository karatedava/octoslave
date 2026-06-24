"""
Lab runner — the top-level orchestration loop.

Phases:
  assemble_team → planning (team meeting + critic gate) → [ implementation
  (individual meetings) → review (team meeting) → director decision ]* → report

Between phases the runner drains the human-injection queue (so a human can
steer mid-run) and, in step mode, blocks at gates until the human approves.

Events are emitted through the same callback the web layer registers via
``display.set_event_callback`` — so the existing token/tool stream AND the new
structured lab events both reach the UI.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from openai import OpenAI

from .. import display
from ..config import DEFAULT_MODEL, load_config
from . import director as _director
from . import foundry as _foundry
from . import meeting as _meeting
from .agent_runtime import run_agent_task
from .state import AgentSpec, LabPhase, LabSession


def _emit(emit, event: dict):
    if emit:
        try:
            emit(event)
        except Exception:
            pass


# --- step-mode approval gate -------------------------------------------------
# The web layer resolves these via resolve_lab_approval(session, action).

def _request_approval(session: LabSession, emit, kind: str, payload: dict) -> str:
    """Block (step mode) until the human approves/continues. Returns the action
    string ('continue' | 'stop'). Autonomous mode returns 'continue' at once."""
    if getattr(session, "autonomous", True):
        return "continue"
    ev = threading.Event()
    session._approval_event = ev          # type: ignore[attr-defined]
    session._approval_action = "continue"  # type: ignore[attr-defined]
    session.status = "paused"
    session.touch()
    _emit(emit, {"type": "lab_awaiting", "kind": kind, **payload})
    display.print_info(f"  ⏸  Awaiting human approval ({kind})…")
    ev.wait()
    session.status = "running"
    session.touch()
    return getattr(session, "_approval_action", "continue")


def resolve_lab_approval(session: LabSession, action: str = "continue") -> None:
    """Called by the web layer to unblock a step-mode gate."""
    session._approval_action = action      # type: ignore[attr-defined]
    ev = getattr(session, "_approval_event", None)
    if ev is not None:
        ev.set()


# --- helpers -----------------------------------------------------------------

# Files we treat as an out-of-band task brief when the task string is a pointer.
_TASK_FILES = ("task.md", "task.txt", "TASK.md", "TASK.txt")
_POINTER_HINTS = ("task.md", "task.txt", "defined in", "see the task",
                  "in the file", "as described in")


def resolve_task(task: str, working_dir: str) -> str:
    """If the task string is a thin pointer (e.g. "task is defined in task.md")
    or is very short while a task file exists in the working dir, load the file
    contents and use them as the real task. Returns the effective task text.

    This is what a human means when they drop a task.md in the folder and type a
    one-liner — the Director must design the team from the FULL brief, not the
    pointer."""
    t = (task or "").strip()
    tl = t.lower()
    is_pointer = (len(t.split()) <= 12) or any(h in tl for h in _POINTER_HINTS)
    if not is_pointer:
        return t
    for fname in _TASK_FILES:
        p = Path(working_dir) / fname
        if p.is_file():
            try:
                body = p.read_text(encoding="utf-8", errors="replace").strip()
            except Exception:
                continue
            if body:
                return body
    return t


def orient(session: LabSession, client: OpenAI, emit, max_iter: int = 12) -> str:
    """Ground the lab BEFORE assembling the team: an ephemeral read-only Scout
    surveys the actual working directory — any task brief, data files (schemas),
    prior outputs, and code — and returns a concise ORIENTATION BRIEF.

    This is more general than reading a fixed filename: the Director designs the
    team from what is genuinely present, whatever the task and layout."""
    scout = AgentSpec(
        name="Scout", role="Workspace orientation / surveyor",
        expertise="Rapidly reading a working directory — task briefs, datasets, "
                  "prior outputs, and code — to infer the real objective and the "
                  "resources available.",
        goal="Survey the working directory and report a grounded brief.",
        tools=["read_file", "list_dir", "glob", "grep"],  # read-only, universal
        icon="🧭", model=session.model,
    )
    survey_task = (
        "ORIENT the lab. This is a read-only survey — do NOT write, edit, or run "
        "anything.\n\n"
        f"The human's starting note was: \"{session.task}\"\n\n"
        "Look around the working directory: list the tree, read any task brief or "
        "README, inspect data files (names + headers/schemas + row counts), and "
        "skim any prior outputs or code already present. Then write a concise "
        "ORIENTATION BRIEF with:\n"
        "1. The REAL objective and success criteria (resolve the starting note "
        "against what you actually find).\n"
        "2. Available data / resources, each with a one-line schema or summary.\n"
        "3. Any prior progress already in the directory.\n"
        "4. Domain, constraints, and risks.\n"
        "Keep it tight and factual — this brief is what the Director uses to "
        "assemble the specialist team."
    )
    _emit(emit, {"type": "lab_phase", "phase": LabPhase.ORIENT})
    display.print_info("\n🧭 Orienting — surveying the working directory…")
    _messages, brief = run_agent_task(
        scout, survey_task, session.working_dir, client,
        max_iter=max_iter, permission_mode="autonomous", emit=emit,
    )
    brief = (brief or "").strip()
    if brief:
        try:
            (session.lab_dir / "orientation.md").write_text(
                f"# Orientation brief\n\n{brief}\n", encoding="utf-8")
            session.add_artifact("lab/orientation.md", "orientation", "Scout")
        except Exception:
            pass
        _emit(emit, {"type": "orientation", "brief": brief})
    return brief


def _files_summary(working_dir: str) -> str:
    """Quick top-level inventory of human-provided files."""
    try:
        items = []
        for p in sorted(Path(working_dir).iterdir()):
            if p.name == "lab" or p.name.startswith("."):
                continue
            if p.is_file():
                items.append(f"  - {p.name} ({p.stat().st_size // 1024 or 1} KB)")
            elif p.is_dir():
                items.append(f"  - {p.name}/ (dir)")
        return "\n".join(items[:60])
    except Exception:
        return ""


def _workspace_manifest(session: LabSession, limit: int = 80) -> str:
    """List the files already produced under lab/projects/ so each worker can
    BUILD ON them (and find prior agents' real data) instead of re-scaffolding or
    fabricating dummy inputs."""
    root = session.projects_dir
    if not root.exists():
        return "(workspace empty — no files produced yet)"
    out = []
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        parts = p.relative_to(session.working_dir).parts
        if "__MACOSX" in parts or "__pycache__" in parts or p.suffix == ".pyc":
            continue
        rel = p.relative_to(session.working_dir)
        out.append(f"  - {rel}  ({p.stat().st_size // 1024 or 1} KB)")
        if len(out) >= limit:
            out.append("  - … (more)")
            break
    return "\n".join(out) if out else "(workspace empty — no files produced yet)"


def _gather_progress(session: LabSession) -> str:
    parts = [f"Completed rounds: {session.round}.",
             "\nFILES ALREADY PRODUCED in the workspace (the project has progressed "
             "this far — do NOT repeat steps these represent):",
             _workspace_manifest(session),
             f"\nCurrent agenda was:\n{session.agenda}"]
    return "\n".join(parts)


def _drain_inbox(session: LabSession) -> list[str]:
    """Drain a file-based injection inbox at ``<wd>/lab/inbox/``.

    Any ``*.md`` / ``*.txt`` file dropped there is read as a human injection,
    then moved to ``inbox/applied/``. This lets a human (or a monitoring
    collaborator) steer a background/CLI run without the web socket — the web
    file picker can drop notes here too."""
    inbox = session.lab_dir / "inbox"
    if not inbox.is_dir():
        return []
    out: list[str] = []
    applied = inbox / "applied"
    for p in sorted(inbox.iterdir()):
        if not p.is_file() or p.suffix.lower() not in (".md", ".txt"):
            continue
        try:
            body = p.read_text(encoding="utf-8", errors="replace").strip()
        except Exception:
            continue
        if body:
            out.append(body)
        try:
            applied.mkdir(parents=True, exist_ok=True)
            p.replace(applied / f"{int(time.time())}_{p.name}")
        except Exception:
            pass
    return out


def _apply_injections(session: LabSession, emit) -> str:
    """Drain pending human injections; fold into the agenda; return their text."""
    injs = session.drain_injections() + _drain_inbox(session)
    if not injs:
        return ""
    block = "\n".join(f"- {t}" for t in injs)
    combined = (session.agenda + "\n\nHuman guidance:\n" + block).strip()
    # Keep the agenda bounded — favor the most recent guidance over old text.
    if len(combined) > 2500:
        combined = combined[-2500:]
    session.agenda = combined
    session.touch()
    _emit(emit, {"type": "injection_applied", "text": block})
    display.print_info(f"  💉 Applied {len(injs)} human injection(s).")
    return block


def _write_report(session: LabSession, client: OpenAI, emit, feedback: str = "") -> str:
    """Spawn an ephemeral reporter specialist to write/patch lab/report.html.

    When ``feedback`` is given (a post-completion follow-up), the reporter
    revises the EXISTING report to address it rather than rewriting from scratch.
    """
    reporter = AgentSpec(
        name="Reporter", role="Scientific writer / report builder",
        expertise="Synthesizing a project's findings into a clear, self-contained "
                  "HTML report with sections, tables, and honest caveats.",
        goal="Produce lab/report.html summarizing the whole project.",
        tools=["read_file", "write_file", "edit_file", "list_dir", "glob", "bash"],
        icon="📊", model=session.model,
    )
    if feedback:
        task = (
            "Revise the EXISTING report at lab/report.html to address this human "
            f"feedback:\n\"{feedback}\"\n\n"
            "Read the current lab/report.html first, then make the requested changes "
            "(edit in place where possible). Stay grounded in the real work under "
            "lab/projects/ and lab/meetings/ — do not fabricate. Keep it a single "
            "self-contained HTML file. When done, stop."
        )
    else:
        task = (
            f"Write the FINAL report for this lab as a single self-contained HTML file "
            f"at lab/report.html (relative to the working dir).\n\n"
            f"Original task: {session.task}\n\n"
            "Read the meeting transcripts under lab/meetings/ and the work under "
            "lab/projects/ to ground the report in what was actually done. Include: the "
            "goal, the team and approach, key results (with real numbers/outputs where "
            "they exist), limitations, and next steps. Be honest about what was and "
            "wasn't achieved — do not fabricate. Use clean inline CSS. When done, stop."
        )
    _emit(emit, {"type": "lab_phase", "phase": LabPhase.REPORT})
    display.print_info("\n📊 Writing final report…" if not feedback
                       else "\n📊 Revising report per feedback…")
    run_agent_task(reporter, task, session.working_dir, client,
                   max_iter=40, emit=emit)
    report_path = session.lab_dir / "report.html"
    rel = "lab/report.html"
    # Fallback: the reporter sometimes builds a rich report under
    # lab/projects/**/reports/ but never writes the canonical lab/report.html
    # (e.g. when a good report already exists there). Don't leave the lab with an
    # empty canonical path — adopt the best existing report HTML.
    if not report_path.exists():
        candidates = [p for p in session.projects_dir.rglob("*.html")
                      if p.is_file() and "report" in p.name.lower()
                      and "__MACOSX" not in p.parts]
        if candidates:
            best = max(candidates, key=lambda p: p.stat().st_size)
            try:
                import shutil
                shutil.copyfile(best, report_path)
                display.print_info(
                    f"  📊 Adopted {best.relative_to(session.working_dir)} as lab/report.html")
            except Exception:
                pass
    if report_path.exists():
        session.add_artifact(rel, "report", "Reporter")
        # Best-effort polish (reuse the existing pipeline post-processor).
        try:
            from ..research import _postprocess_report_html
            _postprocess_report_html(report_path)
        except Exception:
            pass
    return rel if report_path.exists() else ""


def _run_round(session: LabSession, client: OpenAI, synthesis: str, emit) -> str:
    """One implementation+review cycle: each tool-capable specialist executes its
    part (building on the shared workspace), then a team review meeting. Returns
    the review synthesis. Used by both the main loop and post-completion
    follow-ups."""
    session.phase = LabPhase.IMPLEMENTATION
    session.touch()
    _emit(emit, {"type": "lab_phase", "phase": LabPhase.IMPLEMENTATION, "round": session.round})

    _meta = set(_foundry.META_TOOL_NAMES)
    workers = [a for a in session.active_team if (set(a.tools) - _meta)]
    slug = Path(session.working_dir).name or "project"
    shared_dir = f"lab/projects/{slug}"
    contract_dir = f"{shared_dir}/shared"  # canonical shared inputs (data, ids, partitions) live here
    try:
        (session.projects_dir / slug / "shared").mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    for agent in workers:
        if session.status == "stopped":
            break
        # Drain fresh human guidance BEFORE each specialist runs, so injections
        # reach the very next worker instead of waiting for the next round.
        _apply_injections(session, emit)
        execute_note = (
            "The planning phase is over — this is EXECUTION. Do NOT re-draft the "
            "overall plan or re-derive the task; BUILD ON the existing files listed "
            "below and produce concrete NEW outputs."
        ) if session.round > 1 else "Produce concrete first outputs (don't just plan)."
        manifest = _workspace_manifest(session)
        others = [o for o in workers if o.id != agent.id]
        lanes = "\n".join(f"  - {o.name} ({o.role}) — owns: {o.goal}"
                          for o in others) or "  (you are the only worker this round)"
        task_i = (
            f"Round {session.round}. Current agenda (the WHOLE team's plan):\n{session.agenda}\n\n"
            f"You are {agent.name} ({agent.role}). Execute YOUR part now: "
            f"{agent.goal}.\n{execute_note}\n\n"
            "SCOPE — STAY IN YOUR LANE: deliver ONLY the slice of the agenda that "
            "matches your role/goal above. The agenda lists the whole team's work, "
            "but these teammates own the rest and will each deliver their part in "
            "this same round — do NOT run their analyses, produce their outputs, or "
            f"duplicate their work:\n{lanes}\n"
            "If your part depends on a teammate's output that doesn't exist yet, "
            "note the dependency and do what you can; don't do their job for them.\n\n"
            f"SHARED WORKSPACE: put all outputs under `{shared_dir}/` (one tree for "
            "the whole team — do NOT invent a new top-level folder).\n"
            "SHARED DATA CONTRACT: when several specialists rely on the same shared "
            "inputs (a dataset, a set of records, agreed reference values, or an agreed "
            "partition/split), there must be exactly ONE canonical version the whole "
            f"team shares — never a per-agent copy. Put those canonical shared artifacts "
            f"under `{contract_dir}/`.\n"
            "- Whoever owns the shared inputs PUBLISHES them there once and FREEZES them "
            "(state the version); after that, do not silently re-derive them.\n"
            "- STABLE IDs: if records are joined or partitioned across the team, give "
            "each a stable identifier derived from its CONTENT — never a row number, "
            "position, or anything that changes when a file is regenerated or re-sorted. "
            "All joins and any agreed partition reference this stable id so they don't "
            "drift across re-runs (volatile row-index ids are a classic cause of endless "
            "reconciliation).\n"
            "- Every other agent CONSUMES the canonical shared artifacts READ-ONLY: "
            "reference them directly; do NOT fork your own copy or re-derive them. If a "
            "canonical artifact is wrong or missing, FLAG it for the owner (or note the "
            "dependency) instead of forking your own version.\n"
            "Files that already exist (build on these — your teammates' real outputs "
            f"and data are here):\n{manifest}\n\n"
            "DATA INTEGRITY: use the REAL data already fetched/produced above. Do NOT "
            "create dummy, synthetic, placeholder, or fabricated data. If a required "
            "input is missing, fetch real data or explicitly flag it as blocked — "
            "never substitute made-up records.\n"
            "VALIDITY: before reporting a pattern or conclusion as a real finding, check "
            "it isn't an artifact of HOW the data was collected, grouped, or sampled "
            "(confounding, selection bias, leakage between the data used to find a "
            "pattern and the data used to confirm it). If the outcome you're explaining "
            "correlates with a nuisance factor (e.g. data source, time period, or how "
            "records were selected), control for it or state the caveat honestly — do "
            "not present conclusions built on an unaddressed confound.\n"
            "EFFICIENCY: do not loop over records/APIs one-by-one for hundreds of "
            "items — fetch in bulk (or a representative, clearly-stated sample) and "
            "STOP once you have a usable dataset. Acquisition is a means, not the "
            "goal; spend your turns on synthesis (integration, analysis, deliverables), "
            "not exhaustive crawling.\n"
            "HEAVY COMPUTE: a synchronous `bash` call blocks the ENTIRE lab until it "
            "returns. For anything that may run more than ~2 minutes (training, large "
            "simulations, resampling/Monte-Carlo runs, big data processing) do NOT just "
            "raise the bash timeout — prototype on a SUBSAMPLE first, vectorize instead "
            "of slow loops, and cap expensive iteration counts. If a job is genuinely "
            "long, launch it with `run_background` and poll with `check_process` so the "
            "rest of the team can keep working."
        )
        final, rel, verdict = _meeting.individual_meeting(
            session, client, agent, task_i, context=synthesis, emit=emit)
        session.add_artifact(rel, "meeting", agent.name)

    session.phase = LabPhase.REVIEW
    session.touch()
    _emit(emit, {"type": "lab_phase", "phase": LabPhase.REVIEW, "round": session.round})
    _, synthesis = _meeting.team_meeting(
        session, client, f"Review progress on: {session.agenda}", rounds=1, emit=emit)
    return synthesis


# --- main entry --------------------------------------------------------------

def run_lab(
    task: str,
    working_dir: str,
    client: OpenAI,
    *,
    model: str | None = None,
    autonomous: bool = True,
    max_rounds: int = 4,
    emit=None,
    resume: bool = False,
    files_summary: str = "",
    session: LabSession | None = None,
    enable_foundry: bool = True,
    orient_first: bool = True,
) -> LabSession:
    """Run the full autonomous lab loop. Returns the final LabSession.

    A pre-built ``session`` may be passed in (the web layer does this so live
    injections / approvals reach the same object the loop uses)."""
    cfg = load_config()
    model = model or cfg.get("default_model") or DEFAULT_MODEL
    emit = emit or display.get_event_callback()

    if session is None and resume:
        session = LabSession.load(working_dir)
    if session is None:
        # Expand a pointer task ("see task.md") into the real brief so the
        # Director designs the team from the full task, not the one-liner.
        effective_task = resolve_task(task, working_dir)
        if effective_task != task:
            display.print_info("  📄 Loaded task brief from task file.")
        session = LabSession(task=effective_task, working_dir=working_dir, model=model)
    session.model = model
    session.autonomous = autonomous
    session.status = "running"
    session.ensure_dirs()
    session.touch()

    # Activate the runtime tool/agent/MCP foundry for this run (single-threaded
    # loop → module-level context is safe). No-op if disabled.
    if enable_foundry:
        try:
            _foundry.enable(session, client, model, emit=emit)
        except Exception as exc:  # noqa: BLE001
            display.print_error(f"Foundry enable failed (continuing without it): {exc}")
            enable_foundry = False

    def _grant_foundry(specs):
        # Only working agents get the runtime-expansion meta-tools; discussion-
        # only advisors (no tools) stay advisors so they aren't mistaken for
        # implementation workers.
        if enable_foundry:
            for s in specs:
                if s.tools:
                    _foundry.grant_meta_tools(s)

    def phase(p: str):
        session.phase = p
        session.touch()
        _emit(emit, {"type": "lab_phase", "phase": p, "round": session.round})

    _emit(emit, {"type": "lab_start", "task": task, "working_dir": working_dir,
                 "model": model, "autonomous": autonomous})
    display.print_info(f"\n🧬 Lab starting — {task}\n")

    try:
        # 0) ORIENT — look around the working directory first, so the team is
        # assembled from what's genuinely there (not a hard-coded filename).
        orientation = ""
        if orient_first:
            orientation = orient(session, client, emit)

        # 1) ASSEMBLE TEAM (grounded in the orientation brief)
        phase(LabPhase.ASSEMBLE)
        fs = orientation or files_summary or _files_summary(working_dir)
        team, agenda = _director.assemble_team(session, client, files_summary=fs)
        if not team:
            # Fallback minimal team so the lab can still proceed.
            team = [AgentSpec(name="Generalist", role="General problem solver",
                              expertise="Broad analysis and implementation",
                              goal="Make concrete progress on the task",
                              tools=["read_file", "write_file", "edit_file", "bash",
                                     "glob", "grep", "list_dir", "web_search", "web_fetch"],
                              icon="🛠", model=model)]
            agenda = agenda or f"Make concrete progress on: {task}"
        session.set_team(team)
        _grant_foundry(session.team)
        session.agenda = agenda
        session.touch()
        _emit(emit, {"type": "team_update", "team": [a.to_dict() for a in session.active_team]})
        display.print_info(f"  👥 Team assembled: {', '.join(a.name for a in session.active_team)}")

        if _request_approval(session, emit, "team", {"team": [a.to_dict() for a in session.active_team]}) == "stop":
            return _finish(session, emit, stopped=True)

        # 2) PLANNING (team meeting → critic gate baked in)
        phase(LabPhase.PLANNING)
        _apply_injections(session, emit)
        _, synthesis = _meeting.team_meeting(session, client, session.agenda,
                                             rounds=2, emit=emit)
        _emit(emit, {"type": "plan_update", "agenda": session.agenda})

        if _request_approval(session, emit, "plan", {"agenda": session.agenda}) == "stop":
            return _finish(session, emit, stopped=True)

        # 3) IMPLEMENTATION / REVIEW ROUNDS — one round = one implement+review cycle
        for _round_i in range(max_rounds):
            if session.status == "stopped":
                break
            session.round = _round_i + 1
            _apply_injections(session, emit)

            synthesis = _run_round(session, client, synthesis, emit)

            # Director decides next move.
            decision = _director.next_phase(session, client, _gather_progress(session),
                                            review=synthesis)
            _emit(emit, {"type": "director_decision", **decision})
            display.print_info(f"  🧠 Director: {decision['action']} — {decision['reason']}")

            if _request_approval(session, emit, "round", {"decision": decision}) == "stop":
                break
            if decision["action"] == "report":
                break
            if decision["action"] == "revise_team":
                new_team = _director.revise_team(session, client, decision["reason"])
                session.set_team(new_team)
                _grant_foundry(session.team)
                _emit(emit, {"type": "team_update",
                             "team": [a.to_dict() for a in session.active_team]})
            if decision["next_agenda"]:
                session.agenda = decision["next_agenda"]
                session.touch()

        # 4) REPORT
        report_rel = _write_report(session, client, emit)
        _emit(emit, {"type": "report_ready", "path": report_rel})

        return _finish(session, emit, stopped=False)

    except KeyboardInterrupt:
        display.print_info("\n[dim]Lab interrupted.[/dim]")
        return _finish(session, emit, stopped=True)
    except Exception as exc:  # noqa: BLE001
        display.print_error(f"Lab error: {exc}")
        _emit(emit, {"type": "error", "text": f"Lab error: {exc}"})
        return _finish(session, emit, stopped=True)


def continue_lab(
    working_dir: str,
    client: OpenAI,
    feedback: str,
    *,
    model: str | None = None,
    emit=None,
    max_followup_rounds: int = 2,
    session: LabSession | None = None,
) -> LabSession:
    """Resume a COMPLETED lab to act on human follow-up feedback.

    The Director classifies the feedback: a small report change re-runs only the
    reporter; substantive feedback runs a bounded set of implementation/review
    rounds and then regenerates the report. Reuses the same engine as run_lab."""
    cfg = load_config()
    model = model or cfg.get("default_model") or DEFAULT_MODEL
    emit = emit or display.get_event_callback()

    if session is None:
        session = LabSession.load(working_dir)
    if session is None:
        _emit(emit, {"type": "error", "text": "No prior lab found to continue."})
        return LabSession(task="", working_dir=working_dir, model=model)
    session.model = model
    session.status = "running"
    session.touch()

    # Re-activate the foundry (re-loads any tools built previously).
    try:
        _foundry.enable(session, client, model, emit=emit)
    except Exception:
        pass

    _emit(emit, {"type": "lab_start", "task": session.task, "working_dir": working_dir,
                 "model": model, "autonomous": session.autonomous, "followup": True})
    display.print_info(f"\n↻ Continuing lab — human follow-up: {feedback}\n")

    try:
        decision = _director.classify_followup(
            session, client, feedback, _gather_progress(session))
        _emit(emit, {"type": "director_decision", **decision, "followup": True})
        display.print_info(f"  🧠 Director: {decision['action']} — {decision['reason']}")

        if decision["action"] == "more_rounds":
            session.agenda = decision["agenda"] or feedback
            session.touch()
            _emit(emit, {"type": "plan_update", "agenda": session.agenda})
            base_round = session.round
            synthesis = ""
            for i in range(max_followup_rounds):
                if session.status == "stopped":
                    break
                session.round = base_round + i + 1
                _apply_injections(session, emit)
                synthesis = _run_round(session, client, synthesis, emit)
                decision2 = _director.next_phase(session, client, _gather_progress(session),
                                                 review=synthesis)
                _emit(emit, {"type": "director_decision", **decision2})
                if decision2["action"] == "report":
                    break
                if decision2["next_agenda"]:
                    session.agenda = decision2["next_agenda"]
                    session.touch()
            report_rel = _write_report(session, client, emit)
        else:  # report_fix
            report_rel = _write_report(session, client, emit, feedback=decision["agenda"])

        _emit(emit, {"type": "report_ready", "path": report_rel})
        return _finish(session, emit, stopped=False)
    except Exception as exc:  # noqa: BLE001
        display.print_error(f"Lab follow-up error: {exc}")
        _emit(emit, {"type": "error", "text": f"Lab follow-up error: {exc}"})
        return _finish(session, emit, stopped=True)


def _finish(session: LabSession, emit, stopped: bool) -> LabSession:
    try:
        _foundry.disable()
    except Exception:
        pass
    session.phase = LabPhase.STOPPED if stopped else LabPhase.COMPLETE
    session.status = "stopped" if stopped else "complete"
    session.touch()
    _emit(emit, {"type": "lab_complete", "stopped": stopped,
                 "artifacts": session.artifacts,
                 "report": next((a["path"] for a in session.artifacts
                                 if a["kind"] == "report"), "")})
    display.print_info(f"\n{'🛑 Lab stopped.' if stopped else '✅ Lab complete.'} "
                       f"Outputs under {session.lab_dir}")
    return session
