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
from .tools import execute_tool, all_tool_definitions, valid_tool_names, init_mcp
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
) -> tuple[bool, str]:
    """Verifier grades the finished work. Returns ``(done, note)``.

    DONE -> finish. REVISE -> Worker gets another round."""
    transcript = _recent_text(messages, n=10)
    prompt = (
        f"You are the VERIFIER. The WORKER says it is finished. Independently grade "
        f"completion against the task.\n\n"
        f"TASK:\n{task}\n\n"
        f"WHAT THE WORKER DID (recent transcript):\n{transcript[:4000]}\n\n"
        "Reply with EXACTLY one of:\n"
        "  DONE\n"
        "  REVISE: <the single most important thing still missing or wrong>\n"
        "Only say DONE if the deliverable actually exists and satisfies the task. "
        "Be terse."
    )
    raw = _simple_completion(client, verifier_model, [
        {"role": "system", "content": "You are a strict completion grader. One verdict, no preamble."},
        {"role": "user", "content": prompt},
    ], max_tokens=200).strip()
    if not raw:
        return True, ""
    head = raw.splitlines()[0].strip()
    if head.upper().startswith("REVISE"):
        reason = head.split(":", 1)[1].strip() if ":" in head else raw[len("REVISE"):].strip()
        return False, reason or "work appears incomplete"
    return True, ""


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

def _thinker_plan(
    client: OpenAI,
    roles: dict[str, str],
    task: str,
    messages: list[dict],
    working_dir: str,
    permission_mode: str,
) -> tuple[list[dict], str]:
    """Orient (read-only, via Worker) then ask the Thinker for a strategic plan.

    Returns ``(messages, plan_text)`` with the plan injected so the Worker shares it."""
    # Orientation uses the worker model (good tool-caller) with read-only tools.
    messages = _orientation_phase(client, roles["worker"], messages, working_dir, permission_mode)

    display.print_info(f"{_TAG['thinker']} planning…")
    plan_request = (
        "You are the THINKER. Based on the task and what was just observed in the "
        "working directory, write a concise numbered plan (3-8 steps) the WORKER will "
        "execute. Name specific files/functions to change and what to verify when done. "
        "Do not call tools — output the plan only."
    )
    plan_msgs = list(messages) + [{"role": "user", "content": plan_request}]
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
) -> list[dict]:
    worker = roles["worker"]
    worker_alt = roles.get("worker_alt")  # diverse second-opinion model (may be None)
    active_worker = worker
    retry_state = {"rate": 0, "timeout": 0, "conn": 0}
    iteration = 0
    completion_rounds = 0
    stall = 0  # consecutive non-productive worker turns (text-only / botched)
    seen_calls: dict[tuple, int] = {}
    redundant = 0
    actions_taken = 0  # tool calls actually executed in this loop (NOT orientation)
    empty_start = 0    # consecutive no-tool turns before any work has begun
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
            display.console.print("\n[dim]Interrupted.[/dim]")
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

            # Genuine completion claim -> Verifier gate.
            completion_rounds += 1
            if completion_rounds > _MAX_COMPLETION_ROUNDS:
                display.print_done(iteration)
                break
            display.print_info(f"{_TAG['verifier']} reviewing completion…")
            done, note = _verifier_gate_completion(client, roles["verifier"], task, messages)
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
            _escalate_worker("completion was rejected")
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
            result, success = execute_tool(name, args, working_dir, permission_mode)
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

        if redundant >= 5:
            display.print_info("Stopping: repeated calls without new progress (task likely complete).")
            display.print_done(iteration)
            end_reason = "redundant_calls"
            break

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

def print_roles(roles: dict[str, str], notes: dict[str, str] | None = None) -> None:
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
) -> list[dict]:
    """Run one task through the council. Same return shape as ``agent.run_agent``."""
    if permission_mode is None:
        permission_mode = load_config().get("permission_mode", "autonomous")

    init_mcp()
    # Worker model drives tool knobs; council only runs on cloud backends so this
    # is a no-op for context sizing but keeps _rt() consistent.
    configure_runtime(client, roles["worker"], prompt_profile)

    system_prompt = load_system_prompt(prompt_profile, working_dir)
    if enable_memory:
        mem = load_session_memory(working_dir, query=task)
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
    if enable_plan:
        messages, plan_text = _thinker_plan(client, roles, task, messages, working_dir, permission_mode)
        if plan_text and plan_out is not None:
            plan_out.append(plan_text)

    return _council_loop(messages, roles, task, plan_text, working_dir, client, permission_mode)


def continue_council_agent(
    messages: list[dict],
    follow_up: str,
    client: OpenAI,
    roles: dict[str, str],
    working_dir: str,
    permission_mode: str | None = None,
) -> list[dict]:
    """Follow-up turn in council mode (no re-plan; Worker continues with gating)."""
    if permission_mode is None:
        permission_mode = load_config().get("permission_mode", "autonomous")
    init_mcp()
    configure_runtime(client, roles["worker"], "base")
    messages.append({"role": "user", "content": follow_up})
    return _council_loop(messages, roles, follow_up, "", working_dir, client, permission_mode)
