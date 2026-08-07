"""
Agent runtime — run ONE AgentSpec on the shared agent engine.

This is the atomic unit every higher layer (meetings, runner) composes. It is a
robust tool-using loop (transient-error retries, proactive context trimming,
truncated-args rollback, no-tool exit + nudge) driven by a *dynamic* AgentSpec
rather than a static role table.

Tool surface is restricted to the agent's allowlist by threading ``tools=`` into
``agent._stream_completion`` on every turn (not just turn 1), so a specialist
only ever sees the tools the Director granted it.
"""

from __future__ import annotations

import json
import time

from openai import OpenAI, BadRequestError

from .. import display
from .. import interrupt
from ..agent import (
    _RT,
    _cap_result,
    _compact_and_trim,
    _handle_context_overflow,
    _is_context_window_error,
    _is_malformed_history_error,
    _is_model_unavailable_error,
    _is_retryable_error,
    _proactive_trim,
    _stream_completion,
    independent_soft_budget,
    repair_messages,
    ModelRestorer,
)
from ..tools import all_tool_definitions, execute_tool
from .llm import strip_tool_markup
from .state import AgentSpec, AGENT_DONE, AGENT_WORKING

# Retry knobs (mirror research.py's specialist loop).
_MAX_TURN_RETRIES = 2
_RETRY_BACKOFF_SECS = 30
_DEFAULT_MAX_ITER = 40

# Failover budget. Each switch costs _MAX_TURN_RETRIES × _RETRY_BACKOFF_SECS of
# waiting, so an unbounded loop would churn for hours getting nowhere. A turn that
# succeeds resets the budget — these limit consecutive failures, not the run.
_MAX_FAILOVERS = 6
# Waits after a full sweep of every reachable model has failed. Escalating, since
# by then the problem is the backend, not the model choice. Exhausting this list
# ends the agent (with its partial work preserved) instead of looping forever.
_SWEEP_BACKOFF_SECS = (60, 180, 300)


def tools_for_agent(spec: AgentSpec) -> list[dict]:
    """Resolve the agent's tool allowlist against the live registry.

    Unknown names are silently dropped. An empty allowlist means "no tools"
    (discussion-only agent). MCP tools are matched by their full name too.
    """
    allowed = set(spec.tools or [])
    if not allowed:
        return []
    # If an agent can run shell commands, give it the background-process trio too,
    # so it can offload a long/expensive job instead of blocking the whole lab on a
    # synchronous bash call with an inflated timeout (the Director often omits these).
    if "bash" in allowed:
        allowed.update({"run_background", "check_process", "stop_process"})
    # When a compute node is configured for this lab run, give code-capable agents
    # the cluster-job tools so they can offload HEAVY steps to the node and fetch
    # lightweight results back (the lab itself stays local). Mirrors the bash →
    # run_background auto-grant. The tools are registered only while a node is active.
    try:
        from ..science.tools import active_compute_node, CLUSTER_TOOL_NAMES
        if active_compute_node() is not None and (allowed & {"bash", "write_file", "edit_file", "apply_patch"}):
            allowed.update(CLUSTER_TOOL_NAMES)
    except Exception:
        pass
    defs = [t for t in all_tool_definitions() if t["function"]["name"] in allowed]
    return defs


def _stream_turn(client: OpenAI, model: str, messages: list[dict],
                 tools: list[dict]) -> dict:
    """One streaming completion turn restricted to ``tools``, with transient
    upstream-error retries (504/502/503, connection/timeouts)."""
    last_exc: Exception | None = None
    for attempt in range(_MAX_TURN_RETRIES + 1):
        try:
            return _stream_completion(client, model, messages,
                                      force_tool=False, tools=tools)
        except BadRequestError:
            raise
        except Exception as e:  # noqa: BLE001 — classify below
            if not _is_retryable_error(e) or attempt >= _MAX_TURN_RETRIES:
                raise
            last_exc = e
            display.print_info(
                f"  ↻ Upstream {type(e).__name__}; retry "
                f"{attempt + 1}/{_MAX_TURN_RETRIES} in {_RETRY_BACKOFF_SECS}s…"
            )
            if interrupt.wait(_RETRY_BACKOFF_SECS):
                raise interrupt.StopRequested
    raise last_exc  # pragma: no cover


def _pick_alternate_model(failed: str, pool: list[str] | None,
                          tried: set[str], client: OpenAI) -> str | None:
    """An UNTRIED model to carry on with after ``failed`` gave up, or None.

    Order: a random untried member of the configured pool (random so a run
    doesn't deterministically pile onto the same second choice), then — once the
    pool is exhausted — any untried chat model in the provider catalog, by the
    usual family preference. Never returns a model already in ``tried``: bouncing
    between two models that are both down just burns retry timeouts. When this
    returns None the caller should back off rather than reuse something.
    """
    import random
    untried_pool = [m for m in (pool or []) if m and m != failed and m not in tried]
    if untried_pool:
        return random.choice(untried_pool)
    try:
        from ..config import is_chat_model, list_models, load_config, pick_fallback_model
        catalog = [m for m in (list_models(load_config()) or [])
                   if m and m != failed and m not in tried and is_chat_model(m)]
    except Exception:  # noqa: BLE001 — catalog unreachable
        return None
    if not catalog:
        return None
    return pick_fallback_model(failed, catalog) or random.choice(catalog)


def _resumable(history: list[dict]) -> list[dict]:
    """Trim a stored transcript to a state the API will accept as a prefix.

    A run that ended on an error can leave a trailing assistant turn whose
    tool_calls have no tool results — resuming on that is a 400. Drop such turns
    (a transcript ending in tool results is fine).
    """
    msgs = list(history)
    while msgs:
        last = msgs[-1]
        if last.get("role") == "assistant" and last.get("tool_calls"):
            answered = {m.get("tool_call_id") for m in msgs if m.get("role") == "tool"}
            if all(tc.get("id") in answered for tc in last["tool_calls"]):
                break
            msgs.pop()
            continue
        break
    return msgs


def run_agent_task(
    spec: AgentSpec,
    task: str,
    working_dir: str,
    client: OpenAI,
    *,
    model: str | None = None,
    max_iter: int = _DEFAULT_MAX_ITER,
    permission_mode: str = "autonomous",
    context: str = "",
    emit=None,
    history: list[dict] | None = None,
    model_pool: list[str] | None = None,
) -> tuple[list[dict], str]:
    """Run ``spec`` against ``task`` and return ``(transcript, final_text)``.

    ``context`` is extra shared-state material (prior findings, the agenda)
    appended to the user message. ``emit`` is an optional event callback used to
    surface per-agent activity to the web UI.

    ``history`` RESUMES a previous run of this agent: the transcript returned by
    an earlier call is carried in as the starting messages, so the agent keeps
    everything it already learned (and the dead ends it already hit) and ``task``
    becomes its next instruction. Without it the agent starts fresh.

    ``model_pool`` is the set of models this run may fall back to. When the active
    model stops being usable — retries exhausted on a dropping connection, or an
    endpoint that keeps rejecting what it produces — the agent picks a random
    OTHER pool member and carries on with the same transcript rather than failing.
    """
    model = model or spec.model
    spec.status = AGENT_WORKING
    # Register this agent with the foundry so a tool it builds is granted back to
    # it (and per-agent build caps apply). No-op if the foundry isn't active.
    try:
        from . import foundry as _foundry
        _foundry.set_current_agent(spec)
    except Exception:
        _foundry = None

    def _ev(kind: str, **kw):
        if emit:
            try:
                emit({"type": "agent_event", "agent_id": spec.id,
                      "agent": spec.name, "event": kind, **kw})
            except Exception:
                pass

    user_blocks = [task.strip()]
    if context.strip():
        user_blocks.append("\n\n## Shared context\n" + context.strip())
    messages: list[dict]
    if history:
        # Resume: keep the prior transcript (system prompt included) and append the
        # next instruction. A resumed agent must not redo what it already did.
        messages = _resumable(history) + [{"role": "user", "content": "\n".join(user_blocks)}]
    else:
        messages = [
            {"role": "system", "content": build_agent_system_prompt(spec, working_dir)},
            {"role": "user", "content": "\n".join(user_blocks)},
        ]

    display.print_agent_banner(spec.name, model, 0, 0)
    _ev("start", role=spec.role, tools=spec.tools)

    # Size the context-trim budget to THIS specialist's own model window. Lab and
    # Science never call configure_runtime, so without this the thread-local budget
    # is the module default (~96K) — throttling a large-window specialist (e.g. GLM
    # ~1M) to a fraction of its capacity. A specialist owns its message history, so
    # save/restore the ambient thread-local: in Science the orchestrator's own
    # run_agent loop calls this NESTED on the same thread, and must get its budget
    # back when the specialist returns.
    _had_budget = hasattr(_RT, "soft_budget")
    _prev_budget = getattr(_RT, "soft_budget", None)
    _sb = independent_soft_budget(client, model)
    if _sb:
        _RT.soft_budget = _sb

    def _restore_budget():
        if _had_budget:
            _RT.soft_budget = _prev_budget
        elif hasattr(_RT, "soft_budget"):
            try:
                delattr(_RT, "soft_budget")
            except Exception:
                pass

    # Model failover: when the active model can no longer be used, continue the
    # SAME transcript on another one instead of losing the agent's work. The
    # substitute is a stopgap — `restorer` watches for the chosen model coming
    # back and returns to it, so an outage doesn't demote the rest of the run.
    preferred_model = model
    restorer: ModelRestorer | None = None
    tried_models: set[str] = {model}
    bad_shape = 0        # consecutive "endpoint rejected the history" 400s
    stopped_reason = ""  # set when the loop breaks on an infrastructure failure
    switches = 0         # failovers since the last turn that actually succeeded
    sweeps = 0           # times we've been through every reachable model

    def _failover(reason: str) -> bool:
        """Move to another model, or return False to stop trying.

        Every reachable model is tried before any is retried. When a whole sweep
        fails, that is an outage rather than a bad model, so we wait — an
        escalating pause, not another immediate lap — before starting over.
        """
        nonlocal model, tried_models, switches, sweeps, restorer
        if switches >= _MAX_FAILOVERS:
            return False
        alt = _pick_alternate_model(model, model_pool, tried_models, client)
        if not alt:
            if sweeps >= len(_SWEEP_BACKOFF_SECS):
                return False
            wait = _SWEEP_BACKOFF_SECS[sweeps]
            sweeps += 1
            display.print_info(
                f"[{spec.name}] Every available model is failing — the backend "
                f"looks down. Waiting {wait}s before trying again."
            )
            _ev("backend_down", wait=wait, sweep=sweeps)
            if interrupt.wait(wait):
                raise interrupt.StopRequested
            tried_models = {model}      # fresh sweep; the current model failed
            alt = _pick_alternate_model(model, model_pool, tried_models, client)
            if not alt:
                return False
        display.print_info(
            f"[{spec.name}] {reason} on {model} — switching to {alt} to finish the work."
        )
        _ev("model_switch", from_model=model, to_model=alt, reason=reason)
        tried_models.add(alt)
        switches += 1
        model = alt
        sb = independent_soft_budget(client, alt)
        if sb:
            _RT.soft_budget = sb
        if alt != preferred_model and restorer is None:
            restorer = ModelRestorer(preferred_model)
        return True

    t0 = time.time()
    iteration = 0
    final_text = ""
    last_reasoning = ""   # kimi often answers in reasoning_content w/ empty content
    summary_nudged = False
    while iteration < max_iter:
        iteration += 1
        # A user Stop must end the specialist too, not just the orchestrator that
        # spawned it — otherwise Stop appears to hang until the specialist is done.
        if interrupt.should_stop():
            display.print_info(f"[{spec.name}] stopped by the user.")
            _restore_budget()
            # Carry the partial transcript on the exception so the caller can save
            # it — a stopped specialist should be resumable, not thrown away.
            stop = interrupt.StopRequested()
            stop.transcript = messages          # type: ignore[attr-defined]
            raise stop
        # Heartbeat: a single model turn can take minutes on a slow backend, and
        # state.json otherwise only updates at meeting boundaries — so a long
        # meeting LOOKS stuck. Emit a per-turn liveness event and refresh
        # state.json so the UI (and external monitors) see continuous progress.
        _ev("heartbeat", iteration=iteration, elapsed=int(time.time() - t0))
        if _foundry is not None:
            try:
                _sess = _foundry.current_session()
                if _sess is not None:
                    _sess.touch()
            except Exception:
                pass
        # Working on a substitute after a failover? Go back to the model this
        # agent was given as soon as it is serving again.
        if restorer is not None and restorer.due(model):
            if restorer.recovered(client):
                display.print_info(
                    f"[{spec.name}] {preferred_model} is responding again — "
                    f"switching back from {model}.")
                _ev("model_switch", from_model=model, to_model=preferred_model,
                    reason="preferred model recovered")
                model = preferred_model
                tried_models = {model}
                sb = independent_soft_budget(client, model)
                if sb:
                    _RT.soft_budget = sb
                restorer = None
        # Re-resolve the offered tools each turn so a tool the agent just built
        # via request_tool (granted back to spec.tools) becomes callable now.
        tools = tools_for_agent(spec)
        messages = _proactive_trim(messages, label=spec.name, client=client, model=model)
        # Strict OpenAI-compatible endpoints (vLLM behind litellm) 400 on shapes
        # some models emit — most often an assistant turn with no text and no
        # tool calls. Normalise before every request so it never reaches the wire.
        messages, _ = repair_messages(messages)
        try:
            response = _stream_turn(client, model, messages, tools)
            # Progress. Forgive the past: every model becomes a candidate again,
            # and the failover budget resets — these bound consecutive failures,
            # not the lifetime of the run.
            bad_shape = 0
            switches = 0
            sweeps = 0
            tried_models = {model}
        except BadRequestError as e:
            err = str(e)
            if _is_model_unavailable_error(err):
                # Dead deployment / stale model id — the conversation is fine.
                if _failover("Model is not available on this backend"):
                    iteration -= 1
                    continue
                display.print_error(f"[{spec.name}] {model} is not available and "
                                    f"no alternative model was found.")
                stopped_reason = "no reachable model"
                break
            if _is_malformed_history_error(err):
                # Repair what we can; if there's nothing left to repair, drop the
                # last exchange; if it STILL fails, this model and this endpoint
                # don't agree — carry on somewhere else.
                bad_shape += 1
                repaired, fixed = repair_messages(messages)
                if fixed:
                    display.print_info(
                        f"[{spec.name}] Endpoint rejected {fixed} message(s) — "
                        f"repaired and retrying.")
                    messages = repaired
                    iteration -= 1
                    continue
                if bad_shape <= 2 and len(repaired) > 2:
                    while len(repaired) > 2 and repaired[-1].get("role") in ("tool", "assistant"):
                        repaired.pop()
                    messages = repaired
                    display.print_info(
                        f"[{spec.name}] Rolling back the last exchange and retrying.")
                    iteration -= 1
                    continue
                if _failover("Endpoint keeps rejecting this model's messages"):
                    messages = repaired
                    bad_shape = 0
                    iteration -= 1
                    continue
                display.print_error(f"[{spec.name}] API error: {e}")
                stopped_reason = "the endpoint rejected this model's messages"
                break
            if _is_context_window_error(err):
                trimmed, progressed = _handle_context_overflow(messages, client, model)
                if progressed:
                    messages = trimmed
                    iteration -= 1
                    continue
                display.print_error(f"[{spec.name}] Context exhausted; stopping.")
                stopped_reason = "context exhausted"
                break
            if "Unterminated string" in err or "Extra data" in err:
                popped = 0
                while messages and messages[-1].get("role") in ("tool", "assistant"):
                    messages.pop()
                    popped += 1
                if popped == 0:
                    break
                messages.append({"role": "user", "content": (
                    "Your previous response was cut off before the tool arguments "
                    "were complete. Redo the last action from scratch with a "
                    "complete, valid response.")})
                iteration -= 1
                continue
            # Some other 400 this model keeps producing — try another model
            # before giving up on the agent's work.
            if _failover(f"{type(e).__name__}"):
                iteration -= 1
                continue
            display.print_error(f"[{spec.name}] API error: {e}")
            break
        except KeyboardInterrupt:
            display.stream_end(False)
            _restore_budget()
            raise
        except Exception as e:  # noqa: BLE001 — connection died for good
            # _stream_turn already retried transient failures with backoff. If we
            # land here the model is effectively unreachable: continue the same
            # transcript on another model rather than losing the whole run.
            display.stream_end(False)
            reason = ("Connection kept dropping" if _is_retryable_error(e)
                      else type(e).__name__)
            if _failover(reason):
                iteration -= 1
                continue
            # Out of options. Stop here rather than churning — the work done so
            # far is kept (transcript + summary), so this agent can be resumed
            # once the backend recovers instead of starting over.
            display.print_error(
                f"[{spec.name}] {reason} and no model is reachable — stopping with "
                f"the work done so far; resume this specialist once the backend is "
                f"back. ({e})")
            stopped_reason = reason.lower()
            break

        content = response["content"]
        tool_calls = response["tool_calls"]
        if content:
            final_text = content
        if response.get("reasoning", "").strip():
            last_reasoning = response["reasoning"]

        assistant_msg: dict = {"role": "assistant", "content": content or ""}
        if tool_calls:
            assistant_msg["tool_calls"] = tool_calls
        messages.append(assistant_msg)

        if not tool_calls:
            # Agent is done. But some models (e.g. kimi) narrate in
            # reasoning_content and return empty `content` on tool turns, so we
            # can reach here with no summary. Nudge ONCE for a closing summary so
            # the transcript and the reviewing Critic have something concrete.
            if not final_text.strip() and not summary_nudged and iteration < max_iter:
                summary_nudged = True
                messages.append({"role": "user", "content": (
                    "Before you finish, give a concise summary of what you did, what "
                    "you found, and the EXACT paths of every file you created or "
                    "modified.")})
                iteration -= 1  # the nudge doesn't consume a turn
                continue
            break

        display.print_separator()
        for tc in tool_calls:
            name = tc["function"]["name"]
            try:
                args = json.loads(tc["function"]["arguments"] or "{}")
            except json.JSONDecodeError:
                msg = (f"Tool call '{name}' had malformed JSON arguments. "
                       "Retry with complete arguments.")
                messages.append({"role": "tool", "tool_call_id": tc["id"], "content": msg})
                display.print_tool_result(name, msg, False)
                continue

            display.print_tool_call(name, args)
            _ev("tool_call", tool=name)
            try:
                result, success = execute_tool(name, args, working_dir, permission_mode)
            except interrupt.StopRequested as stop:
                # Carry the partial transcript out so the caller can save it and
                # this agent stays resumable after the user's stop.
                display.print_info(f"[{spec.name}] stopped by the user mid-tool.")
                _restore_budget()
                stop.transcript = messages      # type: ignore[attr-defined]
                raise
            result = _cap_result(result, name)
            display.print_tool_result(name, result, success)
            _ev("tool_result", tool=name, ok=success)
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
        display.print_separator()

    if not final_text.strip() and last_reasoning.strip():
        # kimi-k2.x frequently puts its entire answer in reasoning_content and
        # returns empty `content` on the closing (no-tool) turn — leaving the
        # orientation brief / agent summary blank. Recover it from the reasoning.
        salvaged = strip_tool_markup(last_reasoning).strip()
        if salvaged:
            final_text = salvaged[:8000]

    if not final_text.strip():
        # The model gave no prose at all — salvage a summary from the files it
        # wrote, so the transcript and the reviewing Critic aren't empty-handed.
        writes: list[str] = []
        for m in messages:
            if m.get("role") != "assistant":
                continue
            for tc in (m.get("tool_calls") or []):
                if tc["function"]["name"] in ("write_file", "edit_file", "apply_patch"):
                    try:
                        p = json.loads(tc["function"]["arguments"] or "{}").get("path", "")
                    except Exception:
                        p = ""
                    if p:
                        writes.append(p)
        final_text = ("(Agent returned no text summary.) Files written/modified: "
                      + (", ".join(sorted(set(writes))) if writes else "none recorded") + ".")

    if stopped_reason:
        # Say plainly that this is partial, so whoever reads the summary resumes
        # the agent instead of treating the work as finished (or redoing it).
        final_text = (
            f"⚠ INCOMPLETE — stopped early: {stopped_reason}. Everything done so "
            f"far is preserved; resume this agent to carry on rather than starting "
            f"the task over.\n\n{final_text}")

    spec.status = AGENT_DONE
    if _foundry is not None:
        try:
            _foundry.clear_current_agent()
        except Exception:
            pass
    display.print_agent_done(spec.name, time.time() - t0, iteration)
    _ev("done", iterations=iteration)
    _restore_budget()
    return messages, final_text


# ---------------------------------------------------------------------------
# System-prompt construction
# ---------------------------------------------------------------------------

# NOTE: any literal { } here would crash load_system_prompt-style formatting; we
# build this with f-strings and pass no further .format(), so braces are safe.
_AGENT_HEADER = """\
You are {name}, a specialist member of an autonomous research lab working on a \
shared goal with a small team of other agents and a human collaborator.

Your role: {role}
Your expertise: {expertise}
Your goal on this team: {goal}

Operating principles:
- You work inside a single project working directory: {working_dir}
- Keep the file system ORGANIZED. Put your work under lab/projects/<subproject>/ \
in clearly named subfolders. Never scatter files at the top level.
- Be concrete and rigorous. Prefer doing real work with your tools over \
describing what could be done. Do not fabricate results — run code, read files, \
verify.
- You only have the tools the Director granted you ({tool_list}). If you need a \
capability none of your tools provide, you can expand at runtime: call \
`request_tool` to have a new tool built and registered (then call it), \
`request_agent` to add a teammate, or `request_mcp` to connect a known data/service \
server. Use these sparingly and only when genuinely blocked.
- When you have finished your task, stop calling tools and give a concise final \
summary of what you did, what you found, and where you wrote outputs.

Today's date: {date}.
"""


def build_agent_system_prompt(spec: AgentSpec, working_dir: str) -> str:
    from datetime import datetime
    prompt = _AGENT_HEADER.format(
        name=spec.name,
        role=spec.role,
        expertise=spec.expertise,
        goal=spec.goal,
        working_dir=working_dir,
        tool_list=", ".join(spec.tools) or "none — you are a discussion-only advisor",
        date=datetime.now().strftime("%Y-%m-%d"),
    )
    # Compute-node awareness (hybrid model). The lab runs locally; when a node is
    # configured for the run, code-capable agents get the cluster-job tools —
    # tell them to offload HEAVY steps to it and fetch lightweight results back.
    # Only injected when a node is actually active, so local-only runs stay clean.
    if spec.tools:
        try:
            from ..config import remote_awareness_note
            from ..science.tools import active_compute_node
            node = active_compute_node()
            if node is not None:
                note = remote_awareness_note(active=node, job_submission=True)
                if note:
                    prompt = prompt + "\n\n" + note
        except Exception:
            pass
    return prompt
