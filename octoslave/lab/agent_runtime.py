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
from ..agent import (
    _cap_result,
    _compact_and_trim,
    _is_context_window_error,
    _is_retryable_error,
    _proactive_trim,
    _stream_completion,
)
from ..tools import all_tool_definitions, execute_tool
from .llm import strip_tool_markup
from .state import AgentSpec, AGENT_DONE, AGENT_WORKING

# Retry knobs (mirror research.py's specialist loop).
_MAX_TURN_RETRIES = 2
_RETRY_BACKOFF_SECS = 30
_DEFAULT_MAX_ITER = 40


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
            time.sleep(_RETRY_BACKOFF_SECS)
    raise last_exc  # pragma: no cover


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
) -> tuple[list[dict], str]:
    """Run ``spec`` against ``task`` and return ``(transcript, final_text)``.

    ``context`` is extra shared-state material (prior findings, the agenda)
    appended to the user message. ``emit`` is an optional event callback used to
    surface per-agent activity to the web UI.
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

    system_prompt = build_agent_system_prompt(spec, working_dir)
    user_blocks = [task.strip()]
    if context.strip():
        user_blocks.append("\n\n## Shared context\n" + context.strip())
    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": "\n".join(user_blocks)},
    ]

    display.print_agent_banner(spec.name, model, 0, 0)
    _ev("start", role=spec.role, tools=spec.tools)

    t0 = time.time()
    iteration = 0
    final_text = ""
    last_reasoning = ""   # kimi often answers in reasoning_content w/ empty content
    summary_nudged = False
    while iteration < max_iter:
        iteration += 1
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
        # Re-resolve the offered tools each turn so a tool the agent just built
        # via request_tool (granted back to spec.tools) becomes callable now.
        tools = tools_for_agent(spec)
        messages = _proactive_trim(messages, label=spec.name, client=client, model=model)
        try:
            response = _stream_turn(client, model, messages, tools)
        except BadRequestError as e:
            err = str(e)
            if _is_context_window_error(err):
                trimmed = _compact_and_trim(messages, groups=10, client=client, model=model)
                if len(trimmed) < len(messages):
                    messages = trimmed
                    iteration -= 1
                    continue
                display.print_error(f"[{spec.name}] Context exhausted; stopping.")
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
            display.print_error(f"[{spec.name}] API error: {e}")
            break
        except KeyboardInterrupt:
            display.stream_end(False)
            raise

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
            result, success = execute_tool(name, args, working_dir, permission_mode)
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

    spec.status = AGENT_DONE
    if _foundry is not None:
        try:
            _foundry.clear_current_agent()
        except Exception:
            pass
    display.print_agent_done(spec.name, time.time() - t0, iteration)
    _ev("done", iterations=iteration)
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
    return _AGENT_HEADER.format(
        name=spec.name,
        role=spec.role,
        expertise=spec.expertise,
        goal=spec.goal,
        working_dir=working_dir,
        tool_list=", ".join(spec.tools) or "none — you are a discussion-only advisor",
        date=datetime.now().strftime("%Y-%m-%d"),
    )
