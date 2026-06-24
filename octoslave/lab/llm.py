"""
Small LLM helpers for the Director / Critic (structured, tool-less calls).

These do non-streaming completions with robust JSON extraction. They are kept
separate from the streaming, tool-using agent loop in agent_runtime.
"""

from __future__ import annotations

import json
import re
import time

from openai import OpenAI

from ..agent import _is_ollama_client, _ollama_extra_body, _is_retryable_error

# Transient upstream hiccups (502/503/504, connection drops, timeouts) are common
# on e-INFRA under load. Without a retry these tool-less calls fail open silently
# (e.g. the Critic's "Critic unavailable; proceeding"), so retry briefly first.
_MAX_RETRIES = 2
_RETRY_BACKOFF = 8.0


# Tool-call markup some models (notably kimi-k2.x) emit as TEXT when they want to
# call a tool but the call is tool-less (Director/Critic/discussion). Left in, it
# pollutes transcripts AND breaks the "NEXT AGENDA:" / JSON parsing the Lab relies
# on. We strip it so only the model's prose remains.
_KIMI_SECTION = re.compile(r"<\|tool_calls?_section_begin\|>.*?<\|tool_calls?_section_end\|>",
                           re.DOTALL)
_PIPE_TOKEN = re.compile(r"<\|tool_call[^>]*\|>")            # stray <|tool_call_begin|> etc.
_FUNCTIONS_CALL = re.compile(r"functions\.[A-Za-z0-9_]+:\d+")  # functions.read_file:2
_ANGLE_TOOL = re.compile(r"</?tool_call>", re.IGNORECASE)


def strip_tool_markup(text: str) -> str:
    """Remove tool-call markup a model emitted as text in a tool-less call."""
    if not text:
        return text
    text = _KIMI_SECTION.sub("", text)
    text = _PIPE_TOKEN.sub("", text)
    text = _FUNCTIONS_CALL.sub("", text)
    text = _ANGLE_TOOL.sub("", text)
    # collapse leftover argument-begin/end bare tokens
    text = text.replace("<|tool_call_argument_begin|>", "").replace("<|tool_call_end|>", "")
    return text.strip()


# Reasoning models (notably kimi-k2.x) emit a large `reasoning_content` that is
# billed against `max_tokens`. If the caller's budget is small, the reasoning can
# consume it entirely and the actual answer (`content`) comes back empty/truncated
# with finish_reason="length" — which silently breaks JSON parsing (the Critic's
# "Critic unavailable" fail-open, premature Director "report", etc.). Enforce a
# floor so there is always room for the answer ON TOP of the reasoning.
_REASONING_HEADROOM = 4000


def complete_text(client: OpenAI, model: str, system: str, user: str,
                  max_tokens: int = 2000, timeout: float = 240.0) -> str:
    """Non-streaming text completion. Returns "" on failure. Tool-call markup is
    stripped (these calls offer no tools; markup would only be noise)."""
    extra = {"extra_body": _ollama_extra_body()} if _is_ollama_client(client) else {}
    # Leave room for reasoning tokens so the answer isn't starved (see note above).
    max_tokens = max(max_tokens, _REASONING_HEADROOM)
    for attempt in range(_MAX_RETRIES + 1):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "system", "content": system},
                          {"role": "user", "content": user}],
                max_tokens=max_tokens,
                timeout=timeout,
                **extra,
            )
            return strip_tool_markup((resp.choices[0].message.content or "").strip())
        except Exception as exc:  # noqa: BLE001
            # Retry transient gateway/connection/timeout hiccups; give up otherwise.
            if _is_retryable_error(exc) and attempt < _MAX_RETRIES:
                time.sleep(_RETRY_BACKOFF * (attempt + 1))
                continue
            return ""
    return ""


_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def _extract_json(text: str):
    """Best-effort extraction of the first JSON object/array from model text."""
    if not text:
        return None
    # 1) fenced block
    m = _JSON_FENCE.search(text)
    candidates = [m.group(1)] if m else []
    candidates.append(text)
    # 2) first {...} or [...] span
    for opener, closer in (("{", "}"), ("[", "]")):
        i = text.find(opener)
        j = text.rfind(closer)
        if 0 <= i < j:
            candidates.append(text[i:j + 1])
    for c in candidates:
        c = c.strip()
        try:
            return json.loads(c)
        except Exception:
            continue
    return None


def complete_json(client: OpenAI, model: str, system: str, user: str,
                  max_tokens: int = 2500, retries: int = 1):
    """Complete and parse JSON. Returns the parsed object or None.

    Appends a strict-JSON instruction and retries once on parse failure with a
    sharper nudge.
    """
    sys_full = system + (
        "\n\nRespond with ONLY valid JSON — no prose, no markdown fences, no "
        "commentary before or after."
    )
    for attempt in range(retries + 1):
        nudge = "" if attempt == 0 else (
            "\n\nYour previous reply was not parseable JSON. Output ONLY the JSON value."
        )
        text = complete_text(client, model, sys_full, user + nudge, max_tokens=max_tokens)
        parsed = _extract_json(text)
        if parsed is not None:
            return parsed
    return None
