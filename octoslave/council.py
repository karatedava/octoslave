"""
Council ("improved") single-agent mode.

Public surface — drop-in replacements for the agent.py entry points, so the
REPL's memory/continue plumbing is untouched:

    resolve_council_roles(client, cfg, overrides)   -> (roles, notes)
    run_council_agent(task, working_dir, client, roles, ...)      -> messages
    continue_council_agent(messages, follow_up, client, roles, ...) -> messages

The idea: rather than one model doing everything, a lightweight coordinator routes
each action-turn between three role-specialized e-INFRA models — **Thinker / Worker /
Verifier** — and lets a diverse pool beat any single model. The user sees ONE agent;
the council is internal.

We reuse agent.py's hardened primitives (``_robust_stream``, ``_stream_completion``,
``_simple_completion``, ``_orientation_phase``, ``execute_tool``, ``_cap_result``,
``_extract_text_tool_calls``, ``configure_runtime``, …) so the worker loop inherits
the same retry / trim / tool-format robustness as normal mode.

Coordinator policy (heuristic — fast, no extra routing LLM call):
  * easy   (read-only / informational tool calls)      -> execute directly
  * risky  (mutating: write/edit/apply_patch/bash/bg)  -> Verifier reviews BEFORE
            execution; on REVISE the action is dropped and the Worker revises
  * hard   (Worker stalls / repeats / signals doubt)   -> Thinker injects a
            course-correction note
At completion a Verifier gate grades against the task and can send the Worker
back for another round.

Diversity escalation (the ensemble lever): when the Worker gets stuck — repeated
execution errors, a verifier-deadlocked action, or rejected completion — the loop
switches the Worker to a different model FAMILY (``worker_alt``). A block one model
family can't clear is often trivial for another, which is where the ensemble's gain
over any single model comes from. On rejected completion the Thinker also
re-strategizes (the highest-value place for it). Until the first real action runs,
the Worker is forced to call a tool, so the run can never "complete" having done
nothing.
"""

from __future__ import annotations

import json

from openai import OpenAI

from . import display
from . import logger
from . import interrupt
from .agent import (
    MAX_ITERATIONS,
    _robust_stream,
    _simple_completion,
    _orientation_phase,
    _extract_text_tool_calls,
    _looks_like_tool_attempt,
    _cap_result,
    _proactive_trim,
    configure_runtime,
    load_system_prompt,
    load_session_memory,
    _rt,
)
from .tools import (
    execute_tool,
    all_tool_definitions,
    valid_tool_names,
    init_mcp,
    configure_execution,
    running_background_processes,
)
from .config import (
    load_config,
    resolve_backend,
    resolve_council_models,
    einfra_list_models,
    list_models,
    COUNCIL_ROLES,
)

# Tools that mutate the workspace / run commands — these trigger Verifier review.
MUTATING_TOOLS = frozenset({
    "write_file", "edit_file", "apply_patch", "bash", "run_background",
})

# Role display tags.
_TAG = {
    "thinker":  "[bold #9d7cd8]🧠 Thinker[/bold #9d7cd8]",
    "worker":   "[bold #fab283]🔧 Worker[/bold #fab283]",
    "verifier": "[bold #5cd5c2]🔍 Verifier[/bold #5cd5c2]",
}

# Bound how often the Verifier can bounce a single action-turn before we let the
# Worker proceed anyway (prevents a critic/worker deadlock).
_MAX_VERIFIER_REVISIONS = 2
# Bound how many full completion-gate rounds the Verifier can demand.
_MAX_COMPLETION_ROUNDS = 3
# Re-running the SAME action (same tool + args) with no edit in between is the
# classic stuck loop (e.g. running `tsc`/tests repeatedly without changing code).
# Soft = re-strategize + switch family; hard = stop the loop. Re-runs that follow
# a real edit are productive and never counted (the streak resets on each edit).
_REPEAT_LOOP_SOFT = 3    # nudge the CURRENT worker to stop repeating (no switch yet)
_REPEAT_LOOP_SWITCH = 5  # only if it ignored the nudge and kept repeating → switch family
_REPEAT_LOOP_HARD = 7    # still stuck after the nudge AND the switch → verify, then stop
# Re-reading files it already has, accumulated across the session, at/above which
# the worker is probably spinning. This does NOT end the task — it triggers a
# verifier check, and continues unless completion is confirmed.
_REDUNDANT_LIMIT = 8
# How many times the stuck-loop guard may verify-and-recover before it gives up
# and stops honestly (with a wrap-up), so a genuinely stuck run still terminates.
_MAX_STUCK_RECOVERIES = 5
# Tools whose successful execution counts as a real state change (resets streaks).
_EDIT_TOOLS = frozenset({"write_file", "edit_file", "apply_patch"})


# ---------------------------------------------------------------------------
# Role resolution
# ---------------------------------------------------------------------------

def resolve_council_roles(
    client: OpenAI,
    cfg: dict | None = None,
    overrides: dict | None = None,
) -> tuple[dict[str, str], dict[str, str]]:
    """Resolve worker/thinker/verifier -> concrete model ids for the active backend.

    Probes the live catalog (so the user's requested ``deepseek-v4`` is used when
    present, else falls back down the preference chain). ``overrides`` (CLI flags
    or ``OCTOSLAVE_COUNCIL_*`` env, merged by the caller) always win. Returns
    ``(roles, notes)`` where ``notes`` explains any fallback for display.
    """
    cfg = cfg or load_config()
    # Live model list for the active backend; einfra/custom go through the
    # generic list_models, which already handles provider catalogs.
    try:
        available = list_models(cfg)
    except Exception:
        available = []
    return resolve_council_models(available, overrides)


def council_available(cfg: dict | None = None) -> bool:
    """Council needs a cloud pool of large models. Local Ollama can't co-resident
    three big models, so council is disabled there (caller falls back to normal)."""
    cfg = cfg or load_config()
    return cfg.get("backend") != "ollama"


# ---------------------------------------------------------------------------
# Verifier / Thinker consults (thin wrappers over _simple_completion)
# ---------------------------------------------------------------------------

def _format_action(tool_calls: list[dict]) -> str:
    """Render the Worker's proposed tool calls for the Verifier to review."""
    lines = []
    for tc in tool_calls:
        name = tc["function"]["name"]
        raw = tc["function"].get("arguments") or "{}"
        try:
            args = json.loads(raw)
        except Exception:
            args = {"_raw": raw[:800]}
        # Keep diffs/commands readable but bounded.
        rendered = {}
        for k, v in args.items():
            sv = v if isinstance(v, str) else json.dumps(v)
            rendered[k] = sv if len(sv) <= 1200 else sv[:1200] + " …[truncated]"
        lines.append(f"- {name}({json.dumps(rendered, ensure_ascii=False)[:1500]})")
    return "\n".join(lines)


def _verifier_review_action(
    client: OpenAI,
    verifier_model: str,
    task: str,
    plan: str,
    tool_calls: list[dict],
    recent: str,
) -> tuple[bool, str]:
    """Verifier reviews a PROPOSED mutating action before it runs.

    Returns ``(approved, note)``. Defaults to APPROVE on any parse failure so the
    critic can never hard-block progress."""
    prompt = (
        f"You are the VERIFIER in a multi-model agent. A WORKER model is about to "
        f"run the action(s) below. Decide if they are correct and safe for the task.\n\n"
        f"TASK:\n{task}\n\n"
        f"PLAN:\n{plan or '(none)'}\n\n"
        f"RECENT CONTEXT (last worker reasoning / tool results):\n{recent[:2500]}\n\n"
        f"PROPOSED ACTION:\n{_format_action(tool_calls)}\n\n"
        "Reply with EXACTLY one of:\n"
        "  APPROVE\n"
        "  REVISE: <one specific, actionable defect to fix>\n"
        "Approve unless there is a concrete correctness, safety, or task-mismatch "
        "problem (wrong file, destructive command, code bug, ignores the task). "
        "Do NOT nitpick style. Be terse."
    )
    raw = _simple_completion(client, verifier_model, [
        {"role": "system", "content": "You are a strict but pragmatic reviewer. One verdict, no preamble."},
        {"role": "user", "content": prompt},
    ], max_tokens=200).strip()
    if not raw:
        return True, ""
    head = raw.splitlines()[0].strip()
    if head.upper().startswith("REVISE"):
        reason = head.split(":", 1)[1].strip() if ":" in head else raw[len("REVISE"):].strip()
        return False, reason or "unspecified concern"
    return True, ""


def _verifier_gate_completion(
    client: OpenAI,
    verifier_model: str,
    task: str,
    messages: list[dict],
    evidence: str = "",
    plan: str = "",
) -> tuple[bool, str]:
    """Verifier grades the finished work. Returns ``(done, note)``.

    DONE -> finish. REVISE -> Worker gets another round. ``evidence`` summarises
    what the Worker actually did this session (e.g. that it only *inspected*
    files and produced nothing) — the transcript alone can be talked past by a
    confident but premature "done", so this hard signal anchors the grade.
    ``plan`` is the concrete step list the worker committed to up front, so the
    grader can check that EVERY step actually happened, not just some."""
    transcript = _recent_text(messages, n=10)
    prompt = (
        f"You are the VERIFIER. The WORKER says it is finished. Independently grade "
        f"completion against the task.\n\n"
        f"TASK:\n{task}\n\n"
        + (f"THE PLAN THE WORKER COMMITTED TO (every step must be done):\n{plan[:1500]}\n\n" if plan else "")
        + (f"SESSION ACTIVITY (ground truth, not the worker's claim):\n{evidence}\n\n" if evidence else "")
        + f"WHAT THE WORKER DID (recent transcript):\n{transcript[:4000]}\n\n"
        "Reply with EXACTLY one of:\n"
        "  DONE\n"
        "  REVISE: <the single most important thing still missing or wrong>\n"
        "Grade the END STATE against the task, not which session produced it. Say DONE "
        "if the task's deliverable now EXISTS and demonstrably satisfies the task — "
        "INCLUDING when it was produced in an earlier session — as long as the "
        "transcript actually confirms that state (the worker READ the files and their "
        "contents meet the requirements, or ran/tested them and they pass). Do NOT "
        "force the worker to redo edits that are already present and correct: re-doing "
        "already-correct work is waste, not completion.\n"
        "But an UNVERIFIED claim is NOT completion: a bare assertion that something "
        "'already works' or 'was done before', with nothing in the transcript showing "
        "the deliverable actually meets the task, is not enough — answer REVISE and "
        "tell the worker to CONFIRM the real state (read / run / test it), not to "
        "blindly redo it.\n"
        "Grade the WHOLE task, not part of it. If the task (or the plan above) has "
        "several parts or steps and only SOME are done, answer REVISE and name the "
        "first unfinished part — partial progress is NOT completion.\n"
        "The following are NOT completion — answer REVISE if the worker did only these:\n"
        "  - oriented, inspected, or set up the environment (checked which tools/"
        "binaries/GPUs exist, read files, printed versions) without carrying out the task;\n"
        "  - launched or scheduled work and left it unfinished — a job that is still "
        "running, or output not yet collected and used, is not a delivered result;\n"
        "  - announced FUTURE work instead of doing it. Treat forward-looking phrasing "
        "('I'll check on it', 'next check in a few minutes', 'will start X once it "
        "completes', 'then I will…') as an admission the task is NOT finished.\n"
        "When unsure, answer REVISE — it only costs another round.\n"
        "Be terse."
    )
    grader_msgs = [
        {"role": "system", "content": "You are a strict completion grader. One verdict, no preamble."},
        {"role": "user", "content": prompt},
    ]
    # The completion decision is critical, so a transient drop shouldn't decide
    # it — retry once before giving up.
    raw = _simple_completion(client, verifier_model, grader_msgs, max_tokens=200).strip()
    if not raw:
        raw = _simple_completion(client, verifier_model, grader_msgs, max_tokens=200).strip()
    if not raw:
        # Fail SAFE, not open: an empty verdict means the grader call dropped
        # (common on a flaky connection) — do NOT read that as approval, or the
        # task ends unverified mid-work. Ask for another round; the completion-
        # round cap still bounds this so a persistent outage can't loop forever.
        return False, "completion could not be verified (grader did not respond) — continue the task"
    head = raw.splitlines()[0].strip()
    if head.upper().startswith("REVISE"):
        reason = head.split(":", 1)[1].strip() if ":" in head else raw[len("REVISE"):].strip()
        return False, reason or "work appears incomplete"
    return True, ""


def _debate_completion(
    client: OpenAI,
    panel: list[str],
    aggregator_model: str,
    task: str,
    messages: list[dict],
    evidence: str = "",
    plan: str = "",
) -> tuple[bool, str]:
    """Completion gate as a debate: a diverse panel of critics each grades the
    finished work in ISOLATION, then the aggregator resolves their verdicts.

    Different model families catch different defects, so a deliverable only clears
    when independent critics agree — and a real issue any one of them spots is
    surfaced. The aggregator makes the final call (and dismisses nitpicks), so a
    lone over-strict critic can't deadlock progress. Returns ``(done, note)``."""
    verdicts: list[tuple[str, bool, str]] = []
    for m in panel:
        done, note = _verifier_gate_completion(client, m, task, messages, evidence=evidence, plan=plan)
        verdicts.append((m, done, note))
    # Unanimous DONE → finish without spending an aggregator call.
    if all(d for _, d, _ in verdicts):
        return True, ""
    concerns = [f"- {m}: {note}" for m, d, note in verdicts if not d and note]
    if not concerns:  # a REVISE with no stated reason — treat as a minor, pass it
        return True, ""
    transcript = _recent_text(messages, n=10)
    prompt = (
        f"You are the AGGREGATOR resolving a completion review. Independent critics "
        f"graded the WORKER's finished work; some raised concerns. Decide the FINAL "
        f"verdict: is the deliverable actually complete and correct for the task?\n\n"
        f"TASK:\n{task}\n\n"
        + (f"THE PLAN THE WORKER COMMITTED TO (every step must be done):\n{plan[:1500]}\n\n" if plan else "")
        + (f"SESSION ACTIVITY (ground truth):\n{evidence}\n\n" if evidence else "")
        + f"WHAT THE WORKER DID (recent transcript):\n{transcript[:3500]}\n\n"
        f"CRITIC CONCERNS:\n" + "\n".join(concerns) + "\n\n"
        "Dismiss concerns that are nitpicks or already satisfied. Reply with EXACTLY one of:\n"
        "  DONE\n"
        "  REVISE: <the single most important REAL defect to fix>\n"
        "Be terse."
    )
    raw = _simple_completion(client, aggregator_model, [
        {"role": "system", "content": "You make the final completion call from several reviews. Decisive, no preamble."},
        {"role": "user", "content": prompt},
    ], max_tokens=200).strip()
    if not raw:
        # Aggregator silent — fall back to the strongest critic concern (stay safe).
        return False, concerns[0].split(": ", 1)[-1]
    head = raw.splitlines()[0].strip()
    if head.upper().startswith("REVISE"):
        reason = head.split(":", 1)[1].strip() if ":" in head else raw[len("REVISE"):].strip()
        return False, reason or "work appears incomplete"
    return True, ""


def _run_completion_gate(
    client: OpenAI,
    roles: dict[str, str],
    completion_panel: list[str] | None,
    task: str,
    messages: list[dict],
    evidence: str,
    plan: str,
) -> tuple[bool, str]:
    """Grade completion, using the ultra debate panel when available (>=2 critics)
    else the single Verifier. Shared by the worker's own 'done' claim and by the
    stuck-loop guards, so neither can END the task without the verifier's sign-off."""
    if completion_panel and len(completion_panel) >= 2:
        display.print_info(
            f"{_TAG['verifier']} debating completion across "
            f"[bold]{', '.join(completion_panel)}[/bold]…"
        )
        return _debate_completion(
            client, completion_panel, roles["verifier"], task, messages, evidence=evidence, plan=plan
        )
    display.print_info(f"{_TAG['verifier']} reviewing completion…")
    return _verifier_gate_completion(
        client, roles["verifier"], task, messages, evidence=evidence, plan=plan
    )


def _final_summary(client: OpenAI, model: str, task: str, messages: list[dict]) -> None:
    """Print a short wrap-up of what was actually accomplished when the loop is
    forced to stop WITHOUT a clean verifier-approved completion (a stuck loop or
    an exhausted retry budget). Leaves the user with an honest overview instead of
    a bare 'Done', which matters most for long unattended (e.g. overnight) runs."""
    try:
        summary = _simple_completion(client, model, messages + [{"role": "user", "content": (
            "We are stopping the session here. In 3-6 sentences, summarise for the user: "
            "what you actually accomplished on this task, and what (if anything) is still "
            "unfinished or would need another session. Be honest and concrete — this is the "
            f"only wrap-up the user will see.\n\nTask: {task[:400]}"
        )}], max_tokens=450).strip()
    except Exception:
        summary = ""
    if summary:
        display.print_summary(summary)


def _thinker_consult(
    client: OpenAI,
    thinker_model: str,
    task: str,
    plan: str,
    messages: list[dict],
) -> str:
    """Thinker produces a short course-correction note when the Worker is stuck."""
    transcript = _recent_text(messages, n=8)
    prompt = (
        f"You are the THINKER in a multi-model agent. The WORKER seems stuck or "
        f"uncertain. Give a brief, concrete course correction (2-4 sentences): the "
        f"most likely cause and the next single best move. Name specific files/steps.\n\n"
        f"TASK:\n{task}\n\nPLAN:\n{plan or '(none)'}\n\n"
        f"RECENT CONTEXT:\n{transcript[:3000]}"
    )
    return _simple_completion(client, thinker_model, [
        {"role": "system", "content": "You are a sharp planning strategist. Be concrete and brief."},
        {"role": "user", "content": prompt},
    ], max_tokens=300).strip()


# ---------------------------------------------------------------------------
# Thinker plan (orient with worker's tools, then plan with the reasoning model)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Ultra tier — best-of-N debate (isolated panel + aggregator)
# ---------------------------------------------------------------------------

def _debate_drafts(
    client: OpenAI,
    models: list[str],
    system: str,
    prompt: str,
    max_tokens: int = 800,
) -> list[tuple[str, str]]:
    """Ask each panel model the SAME prompt in ISOLATION (none sees another's
    answer) and return ``[(model, text), …]`` for the non-empty replies.

    Isolation is deliberate — it preserves the diversity the aggregator harvests
    and prevents the panel from collapsing onto the first model's framing."""
    drafts: list[tuple[str, str]] = []
    for m in models:
        txt = _simple_completion(client, m, [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ], max_tokens=max_tokens).strip()
        if txt:
            drafts.append((m, txt))
    return drafts


def _aggregate(
    client: OpenAI,
    aggregator_model: str,
    task: str,
    instruction: str,
    drafts: list[tuple[str, str]],
    max_tokens: int = 900,
) -> str:
    """Synthesize the isolated panel drafts into one stronger answer.

    The aggregator is told to keep the best of each, drop what's wrong, and resolve
    disagreements — not to vote, but to compose. Returns the synthesized text (or
    the single draft when only one is present)."""
    if not drafts:
        return ""
    if len(drafts) == 1:
        return drafts[0][1]
    panel = "\n\n".join(
        f"--- CANDIDATE {i+1} (from {m}) ---\n{txt}" for i, (m, txt) in enumerate(drafts)
    )
    prompt = (
        f"You are the AGGREGATOR. {len(drafts)} models independently produced the "
        f"candidates below for this task. Synthesize ONE superior result: keep the "
        f"strongest, most correct ideas from each, drop anything wrong or redundant, "
        f"and resolve disagreements on the merits. Output only the final result, in "
        f"the same format the task asks for — do not mention the candidates.\n\n"
        f"TASK:\n{task}\n\nWHAT TO PRODUCE:\n{instruction}\n\nCANDIDATES:\n{panel}"
    )
    out = _simple_completion(client, aggregator_model, [
        {"role": "system", "content": "You compose the best unified answer from several drafts. Be decisive and concrete."},
        {"role": "user", "content": prompt},
    ], max_tokens=max_tokens).strip()
    return out or drafts[0][1]


def _thinker_plan(
    client: OpenAI,
    roles: dict[str, str],
    task: str,
    messages: list[dict],
    working_dir: str,
    permission_mode: str,
    ultra: bool = False,
) -> tuple[list[dict], str]:
    """Orient (read-only, via Worker) then author a strategic plan.

    Normal: the Thinker writes the plan. Ultra: a diverse panel each drafts a plan
    in isolation and the aggregator (Verifier model — a different family, good at
    judging) synthesizes them into one stronger plan (multi-model debate). Returns
    ``(messages, plan_text)`` with the plan injected so the Worker shares it."""
    # Orientation uses the worker model (good tool-caller) with read-only tools.
    messages = _orientation_phase(client, roles["worker"], messages, working_dir, permission_mode)

    plan_request = (
        "You are the THINKER. Based on the task and what was just observed in the "
        "working directory, write a concise numbered plan (3-8 steps) the WORKER will "
        "execute. Name specific files/functions to change and what to verify when done. "
        "Do not call tools — output the plan only."
    )
    plan_msgs = list(messages) + [{"role": "user", "content": plan_request}]

    panel = roles.get("debate") if ultra else None
    if panel and len(panel) >= 2:
        digest = _recent_text(messages, n=6)
        draft_prompt = (
            f"{plan_request}\n\nTASK:\n{task}\n\n"
            f"WHAT WAS OBSERVED IN THE WORKING DIRECTORY:\n{digest[:3000]}"
        )
        display.print_info(
            f"{_TAG['thinker']} debating the plan across "
            f"[bold]{', '.join(panel)}[/bold]…"
        )
        drafts = _debate_drafts(client, panel, "You are a sharp planning strategist. Be concrete and brief.", draft_prompt)
        plan_text = _aggregate(
            client, roles["verifier"], task,
            "a single numbered execution plan (3-8 steps)", drafts,
        ).strip()
        if not plan_text:  # debate produced nothing usable — fall back to the solo planner
            plan_text = _simple_completion(client, roles["thinker"], plan_msgs, max_tokens=800).strip()
    else:
        display.print_info(f"{_TAG['thinker']} planning…")
        plan_text = _simple_completion(client, roles["thinker"], plan_msgs, max_tokens=800).strip()

    if not plan_text:
        return messages, ""

    display.print_plan(plan_text)
    logger.log_plan(plan_text)
    # Inject as a user directive + assistant ack so the Worker treats it as the plan.
    messages = list(messages) + [
        {"role": "user", "content": "Plan to execute (authored by the planner):\n" + plan_text},
        {"role": "assistant", "content": "Understood. I will execute this plan now."},
    ]
    return messages, plan_text


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _recent_text(messages: list[dict], n: int = 8) -> str:
    """Compact textual digest of the last ``n`` non-system messages."""
    parts = []
    for m in messages[-n:]:
        role = m.get("role", "")
        if role == "system":
            continue
        content = m.get("content") or ""
        if role == "assistant" and m.get("tool_calls"):
            names = ", ".join(tc["function"]["name"] for tc in m["tool_calls"])
            content = (content + f"  [calls: {names}]").strip()
        if content:
            parts.append(f"{role}: {content[:600]}")
    return "\n".join(parts)


def _classify_turn(tool_calls: list[dict]) -> str:
    """Heuristic coordinator: 'risky' if any proposed call mutates state, else 'easy'."""
    for tc in tool_calls:
        if tc["function"]["name"] in MUTATING_TOOLS:
            return "risky"
    return "easy"


# Substrings that mark a tool as state-changing even when it's MCP-namespaced
# (e.g. mcp__filesystem__write_file / __edit_file / __create_directory / __move_file).
_WORK_TOOL_HINTS = ("write", "edit", "create", "patch", "delete", "remove",
                    "run", "exec", "move", "rename", "mkdir", "append")


def _is_work_tool(name: str) -> bool:
    """True if executing ``name`` changes state / runs something (vs read-only
    inspection). Errs toward read-only: a misclassified reader just softens the
    'did no real work' guard, never wrongly blocks a genuine deliverable."""
    if name in MUTATING_TOOLS:
        return True
    low = name.lower()
    return any(h in low for h in _WORK_TOOL_HINTS)


# ---------------------------------------------------------------------------
# Council loop — Worker-driven, with adaptive Thinker/Verifier routing
# ---------------------------------------------------------------------------

def _council_loop(
    messages: list[dict],
    roles: dict[str, str],
    task: str,
    plan: str,
    working_dir: str,
    client: OpenAI,
    permission_mode: str,
    ultra: bool = False,
) -> list[dict]:
    worker = roles["worker"]
    worker_alt = roles.get("worker_alt")  # diverse second-opinion model (may be None)
    # Ultra: a diverse critic panel debates the completion gate (else single Verifier).
    completion_panel = roles.get("debate") if ultra else None
    active_worker = worker
    retry_state = {"rate": 0, "timeout": 0, "conn": 0}
    iteration = 0
    completion_rounds = 0
    stall = 0  # consecutive non-productive worker turns (text-only / botched)
    seen_calls: dict[tuple, int] = {}
    redundant = 0
    stuck_recoveries = 0  # verify-and-recover attempts from re-read / loop spinning
    actions_taken = 0  # tool calls actually executed in this loop (NOT orientation)
    work_actions = 0   # of those, state-changing ones (writes/edits/commands/bg runs)
    inspect_actions = 0  # of those, read-only ones (reads/greps/lists/searches/…)
    nudged_no_work = False  # pushed back on a "done" claim that did zero real work yet
    bg_wait_nudges = 0  # times we've told the worker to wait on an in-flight background job
    empty_start = 0    # consecutive no-tool turns before any work has begun
    edit_epoch = 0           # ++ on each successful edit; identical actions across the
    sig_epoch: dict = {}     # same epoch (no edit between) are a stuck repeat
    sig_streak: dict = {}
    repeat_warned = False     # nudged the current repeat-streak already
    repeat_switched = False   # already switched family for the current repeat-streak
    end_reason = "completed"

    def _escalate_worker(reason: str) -> None:
        """Switch the Worker to a different-family model when the current one is
        stuck. Flips between the primary and the diverse alternate, so a block one
        model can't clear is handed to another family (the core ensemble lever).
        No-op when no alternate is available."""
        nonlocal active_worker
        if not worker_alt or worker_alt == worker:
            return
        new = worker_alt if active_worker != worker_alt else worker
        if new == active_worker:
            return
        active_worker = new
        display.print_info(
            f"{_TAG['worker']} switching model to [bold]{active_worker}[/bold] "
            f"[dim]({reason} — trying a different family)[/dim]"
        )

    while iteration < MAX_ITERATIONS:
        iteration += 1
        # User-requested stop (web UI) — caught between turns; the mid-stream
        # check in _stream_completion handles aborts during generation.
        if interrupt.should_stop():
            display.print_interrupted(iteration - 1)
            logger.log_session_end(iteration, reason="interrupted")
            return messages
        # Force a tool call until the worker has actually DONE something. The
        # orient+plan preamble ends with the worker promising to execute, and
        # some models then narrate ("I will now…") or falsely claim completion
        # instead of acting — which would otherwise trip the completion gate with
        # nothing executed. Requiring a tool call until the first real action
        # guarantees the task actually starts.
        force = actions_taken == 0
        response, signal, messages = _robust_stream(
            client, active_worker, messages, force, retry_state, iteration
        )
        if signal == "retry":
            iteration -= 1
            continue
        if signal == "interrupt":
            display.stream_end(False)
            display.print_interrupted(iteration)
            logger.log_session_end(iteration, reason="interrupted")
            return messages
        if signal == "fatal":
            end_reason = "error"
            break

        content = response["content"]
        tool_calls = response["tool_calls"]
        finish_reason = response["finish_reason"]
        logger.log_turn(iteration, content_preview=(content or "")[:500],
                        finish_reason=finish_reason or "", tool_count=len(tool_calls))

        if not tool_calls and content:
            tool_calls, content = _extract_text_tool_calls(content)
            if tool_calls:
                display.print_info("Model used text-format tool calls — extracting and executing.")

        assistant_msg: dict = {"role": "assistant", "content": content or ""}
        if tool_calls:
            assistant_msg["tool_calls"] = tool_calls
        messages.append(assistant_msg)

        # ---- Worker produced no tool call: a "done" claim or a stall ----------
        if not tool_calls:
            if content and _looks_like_tool_attempt(content, valid_tool_names()):
                stall += 1
                if stall >= 4:
                    display.print_error(
                        "Worker keeps emitting tool calls in an unsupported text format. Stopping."
                    )
                    end_reason = "error"
                    break
                messages.append({"role": "user", "content": (
                    "Your last message looked like a tool call but was not in a valid format, so "
                    "nothing ran. Call the tool through the function-calling interface directly. "
                    "Retry the action now."
                )})
                continue

            # Nothing has actually been executed yet — a "done" claim here is
            # bogus (the worker only oriented/planned). Don't let it reach the
            # completion gate; push it to start executing. Bounded so a model
            # that genuinely refuses can't spin forever.
            if actions_taken == 0:
                empty_start += 1
                if empty_start >= 4:
                    display.print_error(
                        "Worker never started executing after planning. Stopping."
                    )
                    end_reason = "no_progress"
                    break
                display.print_info(
                    f"{_TAG['worker']} produced no action yet — directing it to begin execution."
                )
                messages.append({"role": "user", "content": (
                    "You have not executed any step of the plan yet — no tool has run, so the task "
                    "is NOT done. Do not summarize or claim completion. Begin now: call the first "
                    "concrete tool (read the data, write the script, run it, …) to carry out the plan."
                )})
                continue

            # A completion claim with zero state-changing work is usually premature:
            # the worker only inspected existing files (read/grep/list) and ASSUMED
            # the task was already satisfied. Push back ONCE — but ask it to CONFIRM
            # the end state, not to blindly redo work. If the deliverable already
            # exists and demonstrably meets the task (e.g. a resumed session), citing
            # that evidence is completion; only genuinely-missing work must be done.
            # Bounded (one nudge) so a genuine read-only / answer-only task, or an
            # already-satisfied deliverable, can still finish — the verifier gate
            # below makes the final call.
            if work_actions == 0 and not nudged_no_work:
                nudged_no_work = True
                display.print_info(
                    f"{_TAG['worker']} claims completion without changing anything — "
                    f"directing it to confirm the deliverable actually meets the task."
                )
                messages.append({"role": "user", "content": (
                    "You are claiming the task is done, but this session you have only INSPECTED "
                    "files (reads/greps/lists) — you have not run, built, edited, or tested anything. "
                    "A pre-existing file only counts if you CONFIRM it actually satisfies the task. "
                    "Verify the END STATE now: check that the required deliverable exists and meets "
                    "EVERY part of the task — read it and point to the specific content that satisfies "
                    "each requirement, and run the app/tests if the task involves running or producing "
                    "something. If it ALREADY fully meets the task, say so and cite that evidence — do "
                    "NOT redo, rewrite, or regenerate work that is already correct (that is wasted "
                    "effort, not completion). Only carry out the parts that are genuinely missing or "
                    "wrong. If the task was truly only to read or answer, state that and give the answer."
                )})
                continue

            # A background job the worker launched is still running — the task's
            # result depends on it, so "done" here is premature (the classic
            # "I'll check on it later" stop that ends the turn-based loop before
            # the deliverable exists). Don't complete: make the worker actually
            # wait for the job to finish and consume its output. Bounded so a job
            # that never terminates can't trap the loop forever.
            bg_running = running_background_processes()
            if bg_running and bg_wait_nudges < 3:
                bg_wait_nudges += 1
                listing = "\n".join(
                    f"  - {b['id']} (running {b['elapsed']}s): {b['command'][:80]}"
                    for b in bg_running
                )
                display.print_info(
                    f"{_TAG['worker']} claims completion, but a background job is still "
                    f"running — directing it to wait for the result."
                )
                messages.append({"role": "user", "content": (
                    "You cannot declare the task complete: a background job you started is "
                    "STILL RUNNING and its output is part of the deliverable:\n" + listing + "\n\n"
                    "Do NOT poll once and stop, and do NOT promise to 'check later' — this loop "
                    "will not resume on its own. Actually WAIT for it to finish in a single "
                    "blocking step, e.g. run a bash command that blocks until the process exits "
                    "(a `while kill -0 <pid> 2>/dev/null; do sleep <n>; done` loop, or `wait`, "
                    "with a timeout large enough to cover the whole run). Then read its output "
                    "with check_process, do the downstream analysis, and produce the final "
                    "deliverable. Only report completion once the job has finished and you have "
                    "used its results."
                )})
                continue

            # Genuine completion claim -> Verifier gate.
            completion_rounds += 1
            if completion_rounds > _MAX_COMPLETION_ROUNDS:
                display.print_done(iteration)
                break
            evidence = (
                f"This session the worker ran {inspect_actions} inspection action(s) "
                f"(reads/greps/lists/searches) and {work_actions} state-changing action(s) "
                f"(writes/edits/commands/background runs)."
            )
            if work_actions == 0:
                evidence += (
                    " It changed and ran NOTHING this session — it only inspected existing files. "
                    "That is acceptable ONLY if those files already fully satisfy the task AND the "
                    "inspection above confirms it (a resumed session whose deliverable already "
                    "exists is complete — do not demand redundant re-writes). Otherwise, if the "
                    "task requires building, running, testing, fixing, or producing something and "
                    "the current files do not meet it, it is NOT complete."
                )
            if bg_running:
                evidence += (
                    " A background job it launched is STILL RUNNING (" +
                    ", ".join(b["id"] for b in bg_running) +
                    "); any deliverable that depends on that job's output does not exist yet."
                )
            done, note = _run_completion_gate(
                client, roles, completion_panel, task, messages, evidence, plan
            )
            if done:
                display.print_info(f"{_TAG['verifier']} [bold #7fd88f]approved[/bold #7fd88f] — task complete.")
                display.print_done(iteration)
                break
            display.print_info(f"{_TAG['verifier']} requests revision: [dim]{note[:160]}[/dim]")
            # Rejected completion means the work itself is wrong/incomplete (not a
            # transient error) — the highest-value place for the Thinker. Turn the
            # defect into a concrete corrective strategy and hand the retry to a
            # different family.
            display.print_info(f"{_TAG['thinker']} re-strategizing on the rejected work…")
            strat = _thinker_consult(
                client, roles["thinker"], task, plan,
                messages + [{"role": "user", "content": f"The verifier rejected completion: {note}"}],
            )
            guidance = note
            if strat:
                display.print_plan(strat)
                guidance = f"{note}\n\nPlanner's correction:\n{strat}"
            # Give the current worker the correction first; only switch family if
            # it has now had completion rejected more than once — the same model
            # with concrete feedback usually fixes its own gap without a swap.
            if completion_rounds >= 2:
                _escalate_worker("completion rejected repeatedly")
            messages.append({"role": "user", "content": (
                f"The verifier reviewed your work and it is NOT done yet. Fix this and continue:\n"
                f"{guidance}\n\nMake the concrete change now using tools."
            )})
            continue

        # ---- Worker proposed tool calls: coordinator routes the turn ----------
        stall = 0
        klass = _classify_turn(tool_calls)

        if klass == "risky":
            revisions = 0
            approved = True
            while revisions < _MAX_VERIFIER_REVISIONS:
                display.print_info(f"{_TAG['verifier']} reviewing proposed action…")
                approved, note = _verifier_review_action(
                    client, roles["verifier"], task, plan, tool_calls, _recent_text(messages, 6)
                )
                if approved:
                    break
                revisions += 1
                display.print_info(f"{_TAG['verifier']} requests revision: [dim]{note[:160]}[/dim]")
                # Drop the unexecuted proposal: answer each tool_call_id with the
                # critique so the history stays valid, then let the Worker redo.
                for tc in tool_calls:
                    messages.append({"role": "tool", "tool_call_id": tc["id"], "content": (
                        f"Action NOT executed — verifier flagged a problem: {note}. "
                        "Revise and re-issue the corrected action."
                    )})
                # Re-prompt the Worker for a corrected action this same iteration.
                response, signal, messages = _robust_stream(
                    client, active_worker, messages, False, retry_state, iteration
                )
                if signal != "ok":
                    tool_calls = []
                    break
                content = response["content"]
                tool_calls = response["tool_calls"]
                if not tool_calls and content:
                    tool_calls, content = _extract_text_tool_calls(content)
                am: dict = {"role": "assistant", "content": content or ""}
                if tool_calls:
                    am["tool_calls"] = tool_calls
                messages.append(am)
                if not tool_calls:
                    break  # worker chose to stop / replan; fall through to next loop
            # The verifier kept rejecting this worker's action — hand the next
            # turn to a different family, which often clears the block.
            if not approved:
                _escalate_worker("verifier kept rejecting the action")
            if not tool_calls:
                continue

        # ---- Execute the (approved) tool calls --------------------------------
        display.print_separator()
        turn_had_error = False
        turn_repeat = 0  # longest identical-action streak (no edit between) this turn
        actions_taken += len(tool_calls)  # the task has actually begun
        for tc in tool_calls:
            name = tc["function"]["name"]
            raw_args = tc["function"]["arguments"]
            try:
                args = json.loads(raw_args) if raw_args else {}
            except json.JSONDecodeError:
                tc["function"]["arguments"] = "{}"
                err = (f"Tool call '{name}' had malformed JSON arguments "
                       f"(response truncated). Retry with complete arguments.")
                display.print_tool_result(name, err, False)
                messages.append({"role": "tool", "tool_call_id": tc["id"], "content": err})
                continue
            display.print_tool_call(name, args)
            logger.log_tool_call(tc.get("id", ""), name, args)
            display.tool_activity_start(name, args, permission_mode)
            try:
                result, success = execute_tool(name, args, working_dir, permission_mode)
            finally:
                display.tool_activity_end()
            # Track substantive vs read-only activity so the completion gate knows
            # whether the worker actually DID the task or merely inspected files.
            if _is_work_tool(name):
                work_actions += 1
            else:
                inspect_actions += 1
            if not success:
                turn_had_error = True
            result = _cap_result(result, name)
            display.print_tool_result(name, result, success)
            logger.log_tool_result(tc.get("id", ""), name, success,
                                   preview=(result[:400] if isinstance(result, str) else str(result)[:400]))
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})

            # Redundant read tracking (mirrors _agent_loop intent).
            if name in ("read_file", "list_dir", "glob", "grep"):
                key = (name, raw_args)
                if seen_calls.get(key, 0) >= 1:
                    redundant += 1
                seen_calls[key] = seen_calls.get(key, 0) + 1

            # Repeated-identical-action streak (covers bash/tests/builds too). A real
            # edit advances the epoch, so re-running a command AFTER a change resets
            # its streak; re-running it with nothing changed grows the streak.
            if name in _EDIT_TOOLS and success:
                edit_epoch += 1
            sig = (name, raw_args)
            sig_streak[sig] = (sig_streak.get(sig, 0) + 1) if sig_epoch.get(sig) == edit_epoch else 1
            sig_epoch[sig] = edit_epoch
            turn_repeat = max(turn_repeat, sig_streak[sig])

        # A Stop clicked *during* tool execution (e.g. a long bash) should abort
        # before we spend more model calls on error/stall consults below.
        if interrupt.should_stop():
            display.print_interrupted(iteration)
            logger.log_session_end(iteration, reason="interrupted")
            return messages

        # ---- Hard/stalled escalation to the Thinker ---------------------------
        if turn_had_error:
            stall += 1
            if stall >= 2:
                display.print_info(f"{_TAG['thinker']} consulting after repeated errors…")
                note = _thinker_consult(client, roles["thinker"], task, plan, messages)
                if note:
                    display.print_plan(note)
                    messages.append({"role": "user", "content": (
                        "Strategic guidance from the planner — apply it:\n" + note
                    )})
                # A different family often clears an error the current one keeps hitting.
                _escalate_worker("repeated execution errors")
                stall = 0

        # ---- Stuck re-running the same action with no edits between ------------
        if turn_repeat >= _REPEAT_LOOP_HARD:
            # A tight loop (soft-loop re-strategize + escalation already fired). Do
            # NOT claim the task is done — verify first; if the verifier agrees it is
            # complete, finish cleanly, otherwise stop HONESTLY with a wrap-up of
            # what was actually accomplished rather than a misleading "done".
            display.print_info(
                f"{_TAG['worker']} repeated the same action {turn_repeat}× with no change "
                f"— checking whether the task is actually complete before stopping."
            )
            evidence = (
                f"The worker is stuck repeating the same action {turn_repeat}× with nothing "
                f"changing between runs. It ran {work_actions} state-changing action(s) this session."
            )
            done, _note = _run_completion_gate(
                client, roles, completion_panel, task, messages, evidence, plan
            )
            if done:
                display.print_info(f"{_TAG['verifier']} [bold #7fd88f]approved[/bold #7fd88f] — task complete.")
                end_reason = "completed"
            else:
                display.print_error(
                    "Stopping: the worker is stuck in a loop and the task is not complete. "
                    "Send another message to continue from here."
                )
                _final_summary(client, active_worker, task, messages)
                end_reason = "looping"
            display.print_done(iteration)
            break
        # First: tell the CURRENT worker to stop repeating and re-strategize —
        # give it a real chance to correct itself before changing anything else.
        if turn_repeat >= _REPEAT_LOOP_SOFT and not repeat_warned:
            repeat_warned = True
            display.print_info(
                f"{_TAG['worker']} is repeating the same action without progress — re-strategizing."
            )
            note = _thinker_consult(client, roles["thinker"], task, plan, messages)
            if note:
                display.print_plan(note)
            messages.append({"role": "user", "content": (
                f"You have run the SAME action {turn_repeat} times with nothing changed in between and "
                f"got the same result — repeating it will not help. STOP repeating it. Either make a "
                f"concrete edit to fix the underlying problem, or take a genuinely different approach"
                + (f". Planner's guidance: {note}" if note else "")
                + ". If the task is already complete, stop and say so."
            )})
        # Only if it IGNORED that nudge and kept repeating do we switch family —
        # switching mid-thought loses the current model's context, so it's a last
        # resort, not the first response to a repeat.
        elif turn_repeat >= _REPEAT_LOOP_SWITCH and not repeat_switched:
            repeat_switched = True
            _escalate_worker("kept repeating after being asked to stop")
        elif turn_repeat <= 1:
            repeat_warned = False     # streak broken by a real edit / new action — re-arm
            repeat_switched = False

        if redundant >= _REDUNDANT_LIMIT:
            # Re-reading things it already has is NOT proof the task is done — this
            # was the old false "task likely complete" stop. Verify first; only end
            # if the verifier confirms completion. Otherwise reset the counter and
            # push the worker to make real progress, so a long, legitimately busy
            # session is never cut short. Bounded by _MAX_STUCK_RECOVERIES so a
            # genuinely stuck run still terminates — honestly, with a wrap-up.
            redundant = 0
            seen_calls.clear()
            evidence = (
                f"This session the worker ran {inspect_actions} inspection action(s) and "
                f"{work_actions} state-changing action(s), and keeps re-reading files it "
                f"already has without making new changes."
            )
            done, note = _run_completion_gate(
                client, roles, completion_panel, task, messages, evidence, plan
            )
            if done:
                display.print_info(f"{_TAG['verifier']} [bold #7fd88f]approved[/bold #7fd88f] — task complete.")
                display.print_done(iteration)
                end_reason = "completed"
                break
            stuck_recoveries += 1
            if stuck_recoveries > _MAX_STUCK_RECOVERIES:
                display.print_error(
                    "Stopping: the worker keeps repeating work without making new progress "
                    "and the task is not verified complete. Send another message to continue."
                )
                _final_summary(client, active_worker, task, messages)
                display.print_done(iteration)
                end_reason = "no_progress"
                break
            display.print_info(f"{_TAG['verifier']} says work remains: [dim]{note[:160]}[/dim]")
            # Nudge the current worker first; only switch family if a nudge already
            # failed to break the spin (2nd recovery onward).
            if stuck_recoveries >= 2:
                _escalate_worker("still stuck re-reading after being redirected")
            messages.append({"role": "user", "content": (
                "You keep re-reading files you already have without changing anything — that will "
                "not advance the task, and the verifier confirms it is NOT done: " + note + "\n"
                "Stop re-reading. Make the next CONCRETE change now (write/edit a file, run the "
                "code, produce or fix the deliverable). Do not inspect what you have already seen."
            )})
            continue

        display.print_separator()
    else:
        display.print_info(f"Reached max iterations ({MAX_ITERATIONS}).")
        display.print_done(iteration)
        logger.log_session_end(iteration, reason="max_iterations")
        return messages

    logger.log_session_end(iteration, reason=end_reason)
    return messages


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def print_roles(roles: dict[str, str], notes: dict[str, str] | None = None, ultra: bool = False) -> None:
    """Show the resolved council assignment (and any live-fallback notes)."""
    notes = notes or {}
    display.console.print("  [bold #fab283]🐙 council[/bold #fab283] [dim]— unified single agent[/dim]")
    for role in COUNCIL_ROLES:
        tag = _TAG.get(role, role)
        note = f"  [dim]({notes[role]})[/dim]" if role in notes else ""
        display.console.print(f"    {tag}  [bold]{roles[role]}[/bold]{note}")
    if roles.get("worker_alt"):
        display.console.print(
            f"    [dim]↳ escalation worker:[/dim] [bold]{roles['worker_alt']}[/bold] "
            f"[dim](used when the Worker gets stuck)[/dim]"
        )
    if ultra and roles.get("debate"):
        display.console.print(
            f"    [dim]↳ ⚡ ultra debate panel:[/dim] [bold]{', '.join(roles['debate'])}[/bold]"
        )
    display.console.print()


def run_council_agent(
    task: str,
    working_dir: str,
    client: OpenAI,
    roles: dict[str, str],
    prompt_profile: str = "base",
    permission_mode: str | None = None,
    enable_plan: bool = True,
    enable_memory: bool = True,
    plan_out: list | None = None,
    ultra: bool = False,
    remote: dict | None = None,
) -> list[dict]:
    """Run one task through the council. Same return shape as ``agent.run_agent``.

    ``ultra`` turns on deeper multi-model orchestration (currently: best-of-N
    debate planning across a diverse panel, synthesized by the aggregator)."""
    if permission_mode is None:
        permission_mode = load_config().get("permission_mode", "autonomous")

    configure_execution(remote)
    init_mcp(working_dir=working_dir)
    # Size the runtime context budget from the WORKER model's real window (it
    # drives the accumulating tool-loop history). Thinker/Verifier/aggregator
    # consults build their own bounded prompts, so they need no per-call sizing;
    # worker escalation to a different-family model is handled inside
    # _proactive_trim, which caps the budget at the active model's window.
    configure_runtime(client, roles["worker"], prompt_profile)

    system_prompt = load_system_prompt(prompt_profile, working_dir)
    if enable_memory:
        mem = load_session_memory(working_dir, query=task, remote=remote)
        if mem:
            system_prompt = system_prompt + f"\n\n{mem}"

    logger.log_session_start(
        model=f"council[worker={roles['worker']},thinker={roles['thinker']},verifier={roles['verifier']}]",
        working_dir=working_dir,
        backend=client.base_url.host if hasattr(client, "base_url") else "unknown",
        prompt_profile=prompt_profile,
        permission_mode=permission_mode,
        enable_plan=enable_plan,
        enable_verify=True,
        enable_memory=enable_memory,
    )

    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": task},
    ]

    plan_text = ""
    try:
        if enable_plan:
            messages, plan_text = _thinker_plan(
                client, roles, task, messages, working_dir, permission_mode, ultra=ultra
            )
            if plan_text and plan_out is not None:
                plan_out.append(plan_text)

        return _council_loop(messages, roles, task, plan_text, working_dir, client, permission_mode, ultra=ultra)
    finally:
        configure_execution(None)


def continue_council_agent(
    messages: list[dict],
    follow_up: str,
    client: OpenAI,
    roles: dict[str, str],
    working_dir: str,
    permission_mode: str | None = None,
    ultra: bool = False,
    remote: dict | None = None,
) -> list[dict]:
    """Follow-up turn in council mode (no re-plan; Worker continues with gating)."""
    if permission_mode is None:
        permission_mode = load_config().get("permission_mode", "autonomous")
    configure_execution(remote)
    init_mcp(working_dir=working_dir)
    configure_runtime(client, roles["worker"], "base")
    messages.append({"role": "user", "content": follow_up})
    try:
        return _council_loop(messages, roles, follow_up, "", working_dir, client, permission_mode, ultra=ultra)
    finally:
        configure_execution(None)
