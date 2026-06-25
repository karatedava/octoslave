"""Core agent loop for octoslave."""

import json
import os
import re
import threading
from datetime import date, date as _date_cls
from pathlib import Path
import httpx
from openai import (
    OpenAI,
    BadRequestError,
    APIConnectionError,
    APITimeoutError,
    APIStatusError,
)

# Streaming guard: the client default timeout is 1 hour (long e-INFRA TTFT), but a
# stream that stalls mid-flight would otherwise hang the whole run for up to that
# hour. Cap the SILENCE between streamed chunks instead — long time-to-first-token
# is fine, but if no bytes arrive for this long we treat the call as stalled, raise
# APITimeoutError, and let the caller's retry path re-issue it.
_STREAM_READ_TIMEOUT = httpx.Timeout(300.0, connect=30.0)

from . import display
from . import logger
from .tools import TOOL_DEFINITIONS, execute_tool, all_tool_definitions, valid_tool_names
from .config import load_config, OLLAMA_BASE_URL


def _extract_text_tool_calls(content: str) -> tuple[list[dict], str]:
    """
    Fallback for models that output tool calls as JSON text instead of using
    the API's structured function-calling protocol (e.g. qwen2.5-coder:3b).

    Handles multiple formats:
      {"name": "tool", "arguments": {...}}
      {"name": "tool", "parameters": {...}}
      ["tool_name", {...args...}]
    Code fences (```json ... ```) are stripped before scanning.

    Returns (tool_calls, truncated_content) where truncated_content has
    everything from the first tool call onward removed — preventing hallucinated
    "results" that follow the tool call from polluting the message history.
    """
    calls: list[dict] = []
    call_idx = 0
    first_call_start: int | None = None  # position in original content
    _valid_names = valid_tool_names()  # built-in + MCP (recomputed each call)

    # Build a mapping from positions in stripped → original content.
    # Simple approach: track fence spans and map stripped indices back.
    # For our purposes, we only need the start position of the first tool call
    # in the original content so we can truncate there.

    # Find all code fence spans in original content to map positions
    fence_spans: list[tuple[int, int, str]] = []  # (orig_start, orig_end, inner_text)
    for m in re.finditer(r"```[a-z]*\n?(.*?)```", content, re.DOTALL):
        fence_spans.append((m.start(), m.end(), m.group(1)))

    def _orig_pos_of_first_call(stripped_pos: int) -> int:
        """Approximate original content position for a position in stripped text."""
        # Rebuild stripped with offset tracking
        pos_in_stripped = 0
        pos_in_orig = 0
        i = 0
        while i < len(content):
            # Check if we're at a fence start
            fence = next((f for f in fence_spans if f[0] == i), None)
            if fence:
                inner = fence[2]
                # The fence maps to inner text in stripped
                if pos_in_stripped + len(inner) > stripped_pos:
                    # target is inside this fence
                    offset_in_inner = stripped_pos - pos_in_stripped
                    return fence[0] + content[fence[0]:].index(inner) + offset_in_inner
                pos_in_stripped += len(inner)
                i = fence[1]
                continue
            # Not in a fence — direct mapping
            if pos_in_stripped == stripped_pos:
                return i
            pos_in_stripped += 1
            i += 1
        return len(content)

    stripped = re.sub(r"```[a-z]*\n?(.*?)```", r"\1", content, flags=re.DOTALL)

    def _make_call(name: str, args: dict) -> dict:
        nonlocal call_idx
        c = {
            "id": f"text_call_{call_idx}",
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)},
        }
        call_idx += 1
        return c

    i = 0
    while i < len(stripped):
        ch = stripped[i]
        if ch not in ("{", "["):
            i += 1
            continue

        depth = 0
        j = i
        while j < len(stripped):
            if stripped[j] in ("{", "["):
                depth += 1
            elif stripped[j] in ("}", "]"):
                depth -= 1
                if depth == 0:
                    break
            j += 1

        blob = stripped[i : j + 1]
        matched = False
        try:
            obj = json.loads(blob)
            if isinstance(obj, dict):
                name = obj.get("name") or obj.get("function", {}).get("name", "")
                args = obj.get("arguments") or obj.get("parameters") or {}
                if name in _valid_names and isinstance(args, dict):
                    calls.append(_make_call(name, args))
                    matched = True
            elif isinstance(obj, list) and len(obj) >= 2:
                name, args = obj[0], obj[1]
                if isinstance(name, str) and name in _valid_names and isinstance(args, dict):
                    calls.append(_make_call(name, args))
                    matched = True
        except (json.JSONDecodeError, AttributeError, TypeError):
            pass

        if matched and first_call_start is None:
            # Record the fence or raw start in original content
            # Walk back to find the opening ``` if any
            orig_i = _orig_pos_of_first_call(i)
            # If preceded by a code fence opening, include that in the truncation
            fence_start = next(
                (f[0] for f in fence_spans if f[0] < orig_i and f[1] > orig_i),
                orig_i,
            )
            first_call_start = fence_start

        i = j + 1

    if first_call_start is not None:
        truncated = content[:first_call_start].rstrip()
    else:
        truncated = content

    return calls, truncated


def _looks_like_tool_attempt(content: str, valid_names) -> bool:
    """True if a text-only response looks like a *failed* attempt to call a tool
    rather than a genuine "I'm done" message.

    Some endpoints (e.g. e-INFRA's gemma4) emit tool calls as broken custom text
    like ``<|tool_call>call:list_dir{path:<|"|>results/<|"|>}<tool_call|>`` that is
    neither the structured function-calling protocol nor valid JSON, so
    ``_extract_text_tool_calls`` cannot parse it. Such a turn must NOT be counted
    toward the "model declared done" text-only total — the model is trying to act,
    not finishing — otherwise three of them trigger a false completion."""
    if not content:
        return False
    low = content.lower()
    # Generic tool-call framing markers various templates emit.
    if "tool_call" in low or "tool▁call" in low or "<tool" in low:
        return True
    if '"name"' in low and ('"arguments"' in low or '"parameters"' in low):
        return True
    # A real tool name used in a call-ish position: call:NAME / NAME( / NAME{ .
    for n in valid_names:
        nl = n.lower()
        if f"call:{nl}" in low or f"{nl}(" in low or f"{nl}{{" in low:
            return True
    return False


MAX_ITERATIONS = 400


def _is_retryable_error(exc: Exception) -> bool:
    """True if the exception looks like a transient upstream gateway hiccup that
    we should retry rather than abort on.

    Covers connection drops mid-stream (``APIConnectionError`` →
    "peer closed connection without sending complete message body",
    "incomplete chunked read", "connection reset"), request timeouts, and
    gateway 502/503/504s. e-INFRA / NIM endpoints throw these on long-TTFT
    calls when a model "thinks" for tens of seconds before the first token."""
    if isinstance(exc, (APIConnectionError, APITimeoutError)):
        return True
    if isinstance(exc, APIStatusError):
        return exc.status_code in (502, 503, 504)
    s = str(exc).lower()
    return (
        "peer closed connection" in s
        or "incomplete chunked read" in s
        or "connection reset" in s
        or "connection error" in s
        or "server disconnected" in s
    )


# Hard cap on characters in a single tool result that goes into the message history.
# Prevents a single large file/page from blowing up the context window.
MAX_TOOL_RESULT_CHARS = 50_000

# Ollama allocates a KV cache sized to num_ctx at load time. Without this hint it
# uses the model's full trained context length, which drains GPU memory even for
# short conversations. This is only a *fallback* — at run time we auto-size from
# the model's advertised context_length (see _resolve_ollama_num_ctx), capped to
# keep the KV cache RAM-reasonable. Override the cap with
# OCTOSLAVE_OLLAMA_NUM_CTX_MAX, or force an exact value with OCTOSLAVE_OLLAMA_NUM_CTX.
OLLAMA_NUM_CTX = 8192
# Upper bound on auto-sized local context (KV-cache RAM guard). gemma4:12b et al.
# advertise 131072 — we don't want to allocate that much KV cache by default.
_OLLAMA_NUM_CTX_MAX = int(os.environ.get("OCTOSLAVE_OLLAMA_NUM_CTX_MAX", "32768"))

# Proactive context-budget. The agent estimates total tokens via chars/4 and
# compacts message history BEFORE sending when this threshold is exceeded —
# instead of waiting for the API to return a 400. Default ~96K tokens fits a
# Kimi K2 / 128K-window safely. Override with OCTOSLAVE_SOFT_CONTEXT_TOKENS.
# For local Ollama models this is overridden at run time to track the (much
# smaller) real window — see configure_runtime / _RT.
_SOFT_CONTEXT_BUDGET = int(os.environ.get("OCTOSLAVE_SOFT_CONTEXT_TOKENS", "96000"))

# Path to prompt profiles directory
PROMPT_PROFILES_DIR = Path(__file__).parent / "prompt_profiles"

# Per-thread runtime knobs, set once per run by configure_runtime() based on the
# backend. Lets the context/tool helpers adapt to small local models WITHOUT
# threading params through every call site, and WITHOUT touching the big-model
# (e-INFRA / NIM / custom) path. threading.local() keeps parallel runs isolated
# (mirrors display.py's _streaming_started pattern).
_RT = threading.local()


def _rt(attr: str, default):
    return getattr(_RT, attr, default)


# model → {"num_ctx": int, "thinking": bool}, populated from /api/show
_OLLAMA_INFO_CACHE: dict[str, dict] = {}


def _is_ollama_client(client) -> bool:
    try:
        return OLLAMA_BASE_URL.rstrip("/").rsplit("/v1", 1)[0] in str(client.base_url)
    except Exception:
        return False


def _ollama_model_info(client, model: str) -> dict:
    """Probe /api/show once per model: advertised context_length (capped to
    _OLLAMA_NUM_CTX_MAX; OCTOSLAVE_OLLAMA_NUM_CTX forces an exact value) and
    whether the model has the 'thinking' capability. Cached; safe on any error."""
    if model in _OLLAMA_INFO_CACHE:
        return _OLLAMA_INFO_CACHE[model]
    info = {"num_ctx": OLLAMA_NUM_CTX, "thinking": False}
    try:
        import urllib.request
        base = str(client.base_url).rstrip("/")
        if base.endswith("/v1"):
            base = base[:-3]
        req = urllib.request.Request(
            f"{base}/api/show",
            data=json.dumps({"name": model}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.load(resp)
        mi = data.get("model_info", {})
        ctx = next((v for k, v in mi.items()
                    if k.endswith("context_length") and isinstance(v, int)), None)
        if ctx and ctx > 0:
            info["num_ctx"] = min(ctx, _OLLAMA_NUM_CTX_MAX)
        info["thinking"] = "thinking" in (data.get("capabilities") or [])
    except Exception:
        pass
    forced = os.environ.get("OCTOSLAVE_OLLAMA_NUM_CTX")
    if forced and forced.isdigit():
        info["num_ctx"] = int(forced)
    _OLLAMA_INFO_CACHE[model] = info
    return info


def _ollama_extra_body() -> dict:
    """Ollama-specific request knobs (passed via the SDK's extra_body).

    - num_ctx: KV-cache size hint (honored by GGUF/llama.cpp models; ignored by
      the MLX runner, which always uses the model's full window — harmless).
    - think: thinking models (e.g. gemma3/gemma4, nemotron) reason internally
      before each turn. Kept ON by default so local models run at full power; the
      reasoning is streamed to the display (see stream_reason) so a long thinking
      turn shows visible progress instead of looking hung. Set
      OCTOSLAVE_OLLAMA_THINK=0 to disable thinking for much faster tool calls."""
    body: dict = {"options": {"num_ctx": _rt("num_ctx", None) or OLLAMA_NUM_CTX}}
    # Only touch `think` for models that actually support it (passing it to a
    # non-thinking model can be rejected by Ollama). Default: leave thinking on;
    # only send think:false when the user explicitly opts out for speed.
    think_env = os.environ.get("OCTOSLAVE_OLLAMA_THINK")
    if _rt("ollama_thinking", False) and think_env is not None \
            and think_env.lower() in ("0", "false", "no", "off"):
        body["think"] = False
    return body


def configure_runtime(client, model: str, prompt_profile: str) -> bool:
    """Set per-thread runtime knobs based on the backend. Returns True for local
    (Ollama) runs. For local: auto-size num_ctx, then derive a soft trim budget
    (75% of the real window, so the proactive trimmer fires BEFORE Ollama
    silently truncates) and a per-tool-result cap (~1/3 of the window, so one
    big read can't overflow). For everything else: keep the defaults verbatim."""
    if _is_ollama_client(client):
        meta = _ollama_model_info(client, model)
        num_ctx = meta["num_ctx"]
        _RT.num_ctx = num_ctx
        _RT.soft_budget = max(2048, int(num_ctx * 0.75))
        _RT.tool_result_chars = max(4000, num_ctx * 4 // 3)  # ~1/3 window, tokens→chars
        _RT.ollama_thinking = meta["thinking"]
        # Curate the tool surface for ANY local run, regardless of the prompt
        # profile name — the auto-"local" switch lives in a config dict that may
        # not reach this function, so we key off the backend, not the name.
        _RT.tool_profile = "local"
        return True
    _RT.num_ctx = None
    _RT.soft_budget = _SOFT_CONTEXT_BUDGET
    _RT.tool_result_chars = MAX_TOOL_RESULT_CHARS
    _RT.ollama_thinking = False
    _RT.tool_profile = None
    return False


def list_prompt_profiles() -> list[str]:
    """Return names of available prompt profiles (stems of .md files)."""
    if not PROMPT_PROFILES_DIR.exists():
        return []
    return sorted([f.stem for f in PROMPT_PROFILES_DIR.glob("*.md")])

# ---------------------------------------------------------------------------
# Cross-session memory
#
# One file, two kinds of entry:
#   • "session" — task outcomes recorded automatically at the end of a run
#                 (date / task / status / note).
#   • "insight" — facts the agent itself decided are worth keeping, written
#                 through the `remember` tool (date / kind:insight / tags / text).
#
# Recall is relevance-ranked against the current task rather than "dump the
# last N", so we can retain a larger pool without bloating the prompt — only
# the top lexical matches (plus a recency fallback) are injected.
# ---------------------------------------------------------------------------

MEMORY_FILE = Path.home() / ".octoslave" / "session_memory.md"
_SESSION_MAX_ENTRIES = 40   # session outcomes retained on disk
_INSIGHT_MAX_ENTRIES = 60   # agent-authored insights retained on disk
_RECALL_SESSIONS = 4        # session entries injected into a prompt
_RECALL_INSIGHTS = 6        # insight entries injected into a prompt

_MEMORY_STOPWORDS = frozenset(
    "the a an and or but for to of in on at by with from into is are was were be "
    "this that these those it its as do does did how what when where why which who "
    "use used using make made get got run ran add added fix fixed via your you we "
    "can will should would could not now then code file files task please help".split()
)


def _mem_tokenize(text: str) -> set[str]:
    """Lowercase alphanumeric tokens (len >= 3) minus common stopwords. A cheap
    lexical fingerprint — good enough to rank relevance without embeddings or
    tiktoken, consistent with how _estimated_tokens approximates token counts."""
    toks = re.findall(r"[a-z0-9_]{3,}", (text or "").lower())
    return {t for t in toks if t not in _MEMORY_STOPWORDS}


def _entry_match_text(e: dict) -> str:
    """The text of an entry used for relevance scoring."""
    if e.get("kind") == "insight":
        return f"{e.get('tags', '')} {e.get('text', '')}"
    return f"{e.get('task', '')} {e.get('note', '')}"


def _mem_relevance(query_tokens: set[str], entry: dict) -> float:
    """Token overlap of the query with an entry, penalised by entry length so a
    long entry can't win purely by containing more words. 0.0 with no query."""
    if not query_tokens:
        return 0.0
    et = _mem_tokenize(_entry_match_text(entry))
    if not et:
        return 0.0
    overlap = len(query_tokens & et)
    if overlap == 0:
        return 0.0
    return overlap / (len(et) ** 0.5 + 1.0)


def _parse_memory_entries() -> list[dict]:
    """Parse the memory file into a chronological list of entry dicts. Each has
    a 'kind' of 'session' or 'insight'. Tolerant of missing fields and of older
    files that predate the insight format. Never raises."""
    if not MEMORY_FILE.exists():
        return []
    try:
        text = MEMORY_FILE.read_text(encoding="utf-8")
    except OSError:
        return []
    entries: list[dict] = []
    for block in text.split("---"):
        block = block.strip()
        if not block or block.startswith("#"):
            continue
        parsed: dict = {}
        body_lines: list[str] = []
        in_text = False
        for line in block.splitlines():
            if in_text:
                body_lines.append(line)
                continue
            if ":" in line:
                k, _, v = line.partition(":")
                k = k.strip().lower()
                v = v.strip()
                if k == "text":
                    in_text = True
                    if v:
                        body_lines.append(v)
                else:
                    parsed[k] = v
        if body_lines:
            parsed["text"] = "\n".join(body_lines).strip()
        # Classify. An explicit kind wins; otherwise a `text` body with no
        # `task` is an insight, and anything with a `task` is a session entry.
        if parsed.get("kind") == "insight" or ("text" in parsed and "task" not in parsed):
            if parsed.get("text"):
                parsed["kind"] = "insight"
                entries.append(parsed)
        elif parsed.get("task"):
            parsed["kind"] = "session"
            entries.append(parsed)
    return entries


def _select_memory(pool: list[dict], query_tokens: set[str], k: int) -> list[dict]:
    """Pick up to k entries from pool. With a query, return the highest-scoring
    matches (most relevant first); with no query or no lexical hit, fall back to
    the k most recent (chronological order)."""
    if not pool:
        return []
    if query_tokens:
        scored = [(_mem_relevance(query_tokens, e), i, e) for i, e in enumerate(pool)]
        hits = sorted((s for s in scored if s[0] > 0.0),
                      key=lambda s: (s[0], s[1]), reverse=True)
        if hits:
            return [e for _, _, e in hits[:k]]
    return pool[-k:]


def load_session_memory(query: str | None = None) -> str:
    """
    Return a formatted block of relevant prior context for system-prompt
    injection. When `query` (the current task) is given, entries are ranked by
    lexical relevance to it; otherwise the most recent entries are used.
    Returns empty string when there is nothing to recall.
    """
    try:
        entries = _parse_memory_entries()
        if not entries:
            return ""
        insights = [e for e in entries if e["kind"] == "insight"]
        sessions = [e for e in entries if e["kind"] == "session"]

        qtok = _mem_tokenize(query) if query else set()
        sel_ins = _select_memory(insights, qtok, _RECALL_INSIGHTS)
        sel_ses = _select_memory(sessions, qtok, _RECALL_SESSIONS)
        if not sel_ins and not sel_ses:
            return ""

        parts = ["[MEMORY — relevant prior context; do not repeat completed work]"]
        if sel_ins:
            parts.append("\nThings you chose to remember:")
            for e in sel_ins:
                tags = e.get("tags", "").strip()
                suffix = f"  [{tags}]" if tags else ""
                parts.append(f"  • {e.get('text', '').strip()}{suffix}")
        if sel_ses:
            parts.append("\nPrior sessions:")
            for e in sel_ses:
                d = e.get("date", "?")
                task = e.get("task", "?")
                status = e.get("status", "?")
                note = e.get("note", "")
                line = f"  {d}: {task} — {status}"
                if note:
                    line += f" ({note[:100]})"
                parts.append(line)
        return "\n".join(parts)
    except Exception:
        return ""


def _serialize_memory_entry(e: dict) -> str:
    """Render one entry as a `---`-delimited block matching the parser."""
    if e.get("kind") == "insight":
        out = ["---", f"date: {e.get('date', '')}", "kind: insight"]
        if e.get("tags"):
            out.append(f"tags: {e['tags']}")
        out.append(f"text: {e.get('text', '')}")
        out.append("")
        return "\n".join(out)
    return "\n".join([
        "---",
        f"date: {e.get('date', '')}",
        f"task: {e.get('task', '')}",
        f"status: {e.get('status', '')}",
        f"note: {e.get('note', '')}",
        "",
    ])


def _write_memory(entries: list[dict]) -> None:
    """Trim each kind to its cap (keeping the most recent) and rewrite the file
    in chronological order. Rewriting wholesale keeps per-kind trimming correct
    — insights are never evicted to make room for session outcomes."""
    sessions = [e for e in entries if e.get("kind") == "session"][-_SESSION_MAX_ENTRIES:]
    insights = [e for e in entries if e.get("kind") == "insight"][-_INSIGHT_MAX_ENTRIES:]
    keep = {id(e) for e in sessions} | {id(e) for e in insights}
    ordered = [e for e in entries if id(e) in keep]

    MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(_serialize_memory_entry(e) for e in ordered)
    MEMORY_FILE.write_text(
        "# OctoSlave Session Memory\n\n" + body + ("\n" if body else ""),
        encoding="utf-8",
    )


def save_session_memory(task: str, status: str = "completed", note: str = "") -> None:
    """Record one task-outcome entry, trimming session history to its cap."""
    try:
        entries = _parse_memory_entries()
        entries.append({
            "kind": "session",
            "date": _date_cls.today().isoformat(),
            "task": task[:200].replace(chr(10), " "),
            "status": status,
            "note": note[:300].replace(chr(10), " "),
        })
        _write_memory(entries)
    except Exception:
        pass


def save_memory_insight(content: str, tags=None) -> bool:
    """Persist an agent-authored insight (the `remember` tool). Returns True on
    success. Multi-line content is allowed; a lone `---` line is defused so it
    can't be mistaken for a block separator on the next read."""
    content = (content or "").strip()
    if not content:
        return False
    content = content.replace("\r", "")
    content = re.sub(r"(?m)^---\s*$", "- - -", content)[:1000]
    if isinstance(tags, str):
        tag_str = tags.strip()
    elif tags:
        tag_str = ", ".join(str(t).strip() for t in tags if str(t).strip())
    else:
        tag_str = ""
    try:
        entries = _parse_memory_entries()
        entries.append({
            "kind": "insight",
            "date": _date_cls.today().isoformat(),
            "tags": tag_str[:120],
            "text": content,
        })
        _write_memory(entries)
        return True
    except Exception:
        return False


def load_system_prompt(profile: str = "base", working_dir: str = None) -> str:
    """
    Load a system prompt from a profile file in the prompt_profiles directory.
    
    Args:
        profile: Profile name without extension (e.g., "base" or "simple")
        working_dir: Current working directory to substitute in the prompt
    
    Returns:
        The system prompt string with working_dir and date substituted
    
    Raises:
        FileNotFoundError: If the profile file doesn't exist
    """
    profile_file = PROMPT_PROFILES_DIR / f"{profile}.md"
    
    if not profile_file.exists():
        available = [f.stem for f in PROMPT_PROFILES_DIR.glob("*.md")]
        raise FileNotFoundError(
            f"Prompt profile '{profile}' not found. Available profiles: {available}"
        )
    
    content = profile_file.read_text()
    
    # Strip the outer triple quotes and line continuation if present
    # Format is: """\ followed by newline at start, and """ at end
    content = content.strip()
    if content.startswith('"""'):
        content = content[3:]  # Remove opening """
        if content.startswith('\\\n'):
            content = content[2:]  # Remove \ and newline
        elif content.startswith('\\'):
            content = content[1:]  # Remove just \
    if content.endswith('"""'):
        content = content[:-3]  # Remove closing """
    content = content.strip()
    
    # Substitute placeholders
    wd = working_dir or Path.cwd().resolve()
    return content.format(working_dir=wd, date=date.today().isoformat())


# Substrings that, when present in a provider error string, mean "you sent too
# many tokens." Different providers phrase this very differently — Kimi via
# e-INFRA, OpenAI, vLLM, NIM, and Ollama all use distinct wording. This list
# is intentionally broad; false-positive trims are cheap, false-negative
# break-the-loop errors are not.
_CONTEXT_OVERFLOW_NEEDLES = (
    "contextwindow",          # OpenAI: ContextWindowExceeded
    "context window",         # human prose
    "context length",         # vLLM / many OSS hosts
    "context_length_exceeded",
    "maximum context",        # OpenAI legacy
    "max_tokens",             # some providers conflate
    "max tokens",
    "max_position_embeddings",
    "max_seq_len",
    "maximum_seq_length",
    "prompt is too long",     # Anthropic-style
    "input is too long",
    "input too long",
    "request too large",      # 413 prose
    "payload too large",      # 413 prose
    "too many tokens",
    "token limit",
    "exceeds the limit",
    "exceeds the maximum",
    "tokens in the messages",
    "string too long",        # some proxies
)


def _is_context_window_error(err_str: str) -> bool:
    """True if the error string looks like a context-window / too-many-tokens
    rejection from any supported provider. Lowercased substring match —
    intentionally broad."""
    if not err_str:
        return False
    s = err_str.lower()
    if any(needle in s for needle in _CONTEXT_OVERFLOW_NEEDLES):
        return True
    # HTTP 413 = payload too large; some hosts surface it bare.
    if "413" in s and ("payload" in s or "request" in s or "too large" in s):
        return True
    return False


def _estimated_tokens(messages: list[dict]) -> int:
    """Fast char/4 token estimate over a message list. Good enough to decide
    whether to compact proactively — actual tokenisation is provider-specific
    and not worth importing tiktoken for a soft-budget decision."""
    total = 0
    for m in messages:
        content = m.get("content")
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, list):
            # vision-style multimodal content blocks
            for block in content:
                if isinstance(block, dict):
                    total += len(json.dumps(block, ensure_ascii=False))
        tcs = m.get("tool_calls") or []
        for tc in tcs:
            try:
                total += len(tc.get("function", {}).get("name", ""))
                total += len(tc.get("function", {}).get("arguments", "") or "")
            except Exception:
                pass
        # tiny per-message envelope overhead
        total += 16
    return total // 4


def _proactive_trim(
    messages: list[dict],
    soft_budget: int | None = None,
    label: str = "",
) -> list[dict]:
    """If estimated tokens > soft_budget, compact in a loop until under budget
    or no more groups can be compacted. Returns the trimmed list (or original
    if no trim was needed / possible). Logs each round it actually trimmed.

    soft_budget defaults to the per-thread runtime budget (small for local
    Ollama models, _SOFT_CONTEXT_BUDGET for everything else)."""
    if soft_budget is None:
        soft_budget = _rt("soft_budget", _SOFT_CONTEXT_BUDGET)
    est = _estimated_tokens(messages)
    if est <= soft_budget:
        return messages
    rounds = 0
    while est > soft_budget:
        trimmed = _compact_and_trim(messages, groups=6)
        if len(trimmed) >= len(messages):
            # Nothing left to compact — surrender to the API and let the
            # reactive branch handle the hard failure.
            break
        messages = trimmed
        rounds += 1
        est = _estimated_tokens(messages)
        if rounds >= 5:
            # Safety: don't loop forever even if compaction keeps "working"
            # with diminishing returns.
            break
    if rounds > 0:
        tag = f"[{label}] " if label else ""
        msg = (
            f"{tag}Proactive context trim: {rounds} pass(es), "
            f"~{est:,} tokens remaining (soft budget {soft_budget:,})."
        )
        display.print_info(msg)
        try:
            logger.log_info(msg, rounds=rounds, est_tokens=est, soft_budget=soft_budget)
        except Exception:
            pass
    return messages


def _compact_and_trim(messages: list[dict], groups: int = 6) -> list[dict]:
    """
    Replace the oldest N complete assistant-turn groups with a compact text
    summary instead of silently discarding them.  Preserves messages[:2]
    (system prompt + first user message).

    Falls back to pure deletion when there is nothing to compact.
    """
    system = messages[:2]
    rest = list(messages[2:])

    turns_to_compact: list[dict] = []
    remaining = list(rest)
    removed = 0

    while removed < groups and remaining:
        start = next(
            (i for i, m in enumerate(remaining)
             if m.get("role") == "assistant" and m.get("tool_calls")),
            None,
        )
        if start is None:
            break
        end = start + 1
        while end < len(remaining) and remaining[end].get("role") == "tool":
            end += 1
        turns_to_compact.extend(remaining[start:end])
        remaining = remaining[:start] + remaining[end:]
        removed += 1

    if not turns_to_compact:
        return messages

    # Build a compact text log of the removed turns so information isn't lost.
    # Keep enough content that the model can still recall what it learned —
    # earlier versions of this summary lost too much (150 chars / 120 chars)
    # and the model would re-run the same searches after a compaction.
    lines = [f"[COMPACTED HISTORY — {removed} earlier turn(s) summarised to save context]"]
    for msg in turns_to_compact:
        if msg.get("role") == "assistant":
            txt = (msg.get("content") or "").strip()
            if txt:
                # 400 chars is enough to keep a short rationale / partial result
                snippet = txt if len(txt) <= 400 else txt[:400] + "…"
                lines.append(f"  [note: {snippet}]")
            for tc in msg.get("tool_calls") or []:
                name = tc["function"]["name"]
                try:
                    args = json.loads(tc["function"]["arguments"])
                    label = (
                        args.get("path") or args.get("file_path") or
                        args.get("command") or args.get("query") or
                        args.get("pattern") or args.get("url") or
                        json.dumps(args)[:60]
                    )
                    label = str(label)
                    if len(label) > 90:
                        label = label[:90] + "…"
                    lines.append(f"  called: {name}({label})")
                except Exception:
                    lines.append(f"  called: {name}(...)")
        elif msg.get("role") == "tool":
            content = (msg.get("content") or "").strip()
            if not content:
                continue
            # Keep the first ~3 lines (often the headline / summary) AND the
            # last ~3 non-empty lines (often the error / exit code / final
            # result) — same head-plus-tail strategy as the bash truncator,
            # because errors and result keys tend to live at the end.
            content_lines = [ln for ln in content.splitlines() if ln.strip()]
            head_lines = content_lines[:3]
            tail_lines = content_lines[-3:] if len(content_lines) > 6 else []
            head = " | ".join(ln.strip()[:160] for ln in head_lines)
            if head:
                lines.append(f"    → {head[:480]}")
            if tail_lines:
                tail = " | ".join(ln.strip()[:160] for ln in tail_lines)
                lines.append(f"    ⚠ {tail[:480]}")

    summary_msg = {"role": "user", "content": "\n".join(lines)}
    return system + [summary_msg] + remaining


def make_client(api_key: str, base_url: str) -> OpenAI:
    # Ollama doesn't require a real API key; use a placeholder so the SDK
    # doesn't raise a missing-key error.
    # 1-hour timeout: large models on e-INFRA CZ can have long TTFT
    return OpenAI(
        api_key=api_key if api_key else "ollama",
        base_url=base_url,
        timeout=3600.0,
    )


def _cap_result(result: str, tool_name: str) -> str:
    """Truncate oversized tool results before they enter the message history.

    The cap is the per-thread runtime value — much smaller for local Ollama
    models (~1/3 of their real window) so a single big read can't overflow the
    context; MAX_TOOL_RESULT_CHARS for everything else."""
    cap = _rt("tool_result_chars", MAX_TOOL_RESULT_CHARS)
    if len(result) <= cap:
        return result
    kept = result[:cap]
    omitted = len(result) - cap
    return (
        kept
        + f"\n\n[TRUNCATED — {omitted:,} more characters omitted. "
        f"Use read_file with offset/limit to read the next section if needed.]"
    )


def _simple_completion(client: OpenAI, model: str, messages: list, max_tokens: int = 600) -> str:
    """Non-streaming completion without tools — used for planning and verification."""
    extra = {"extra_body": _ollama_extra_body()} if _is_ollama_client(client) else {}
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=messages,
            **extra,
            max_tokens=max_tokens,
            timeout=120.0,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception:
        return ""


# Read-only tools the model may use while orienting itself before it plans.
# Deliberately excludes everything that writes, runs commands, or hits the
# network so the orientation pass can never mutate state or stall on a slow
# command — it just looks at the local workdir, code, and data. OCR is included
# (local, read-only) so the model can also orient on image/scanned files (PNG,
# JPG, scanned PDFs) instead of being blind to non-text inputs.
_ORIENT_TOOLS = frozenset({"read_file", "list_dir", "glob", "grep", "bio_inspect",
                           "image_ocr", "pdf_ocr"})

_ORIENT_PROMPT = (
    "Before you plan, ORIENT yourself in the working directory. Using ONLY "
    "read-only tools (list_dir, glob, grep, read_file, bio_inspect; and image_ocr / "
    "pdf_ocr to read text from image or scanned-PDF files): explore the directory "
    "layout, read the task and any files it references, and inspect the specific "
    "code and data you will need to change — including images/scans via OCR if "
    "those are what's present. Do NOT write, edit, or run anything yet. When you "
    "have seen enough to write a concrete, file-specific plan, stop calling tools "
    "and reply with just the word READY."
)


def _orientation_phase(
    client: OpenAI,
    model: str,
    messages: list[dict],
    working_dir: str,
    permission_mode: str,
    max_rounds: int = 5,
) -> list[dict]:
    """Let the model look around with read-only tools before it plans.

    Runs a short, bounded loop that offers ONLY ``_ORIENT_TOOLS`` and executes
    any read-only calls the model makes, so the subsequent plan is grounded in
    files it has actually read. All messages are appended, so the main loop
    inherits the gathered context and need not re-read. Best-effort: any error
    just ends orientation early and falls through to planning.
    """
    orient_tools = [td for td in all_tool_definitions(profile=_rt("tool_profile", None))
                    if td["function"]["name"] in _ORIENT_TOOLS]
    if not orient_tools:
        return messages

    display.print_info("Orienting…")
    messages = list(messages) + [{"role": "user", "content": _ORIENT_PROMPT}]

    for rnd in range(max_rounds):
        try:
            response = _stream_completion(
                client, model, messages,
                force_tool=(rnd == 0),  # ensure it actually starts looking
                tools=orient_tools,
            )
        except Exception as e:  # noqa: BLE001 — orientation is best-effort
            display.print_info(f"Orientation ended early ({type(e).__name__}).")
            break

        content = response.get("content") or ""
        tool_calls = response.get("tool_calls") or []
        if not tool_calls and content:
            tool_calls, content = _extract_text_tool_calls(content)

        assistant_msg: dict = {"role": "assistant", "content": content}
        if tool_calls:
            assistant_msg["tool_calls"] = tool_calls
        messages.append(assistant_msg)

        if not tool_calls:
            break  # model signalled READY — done looking

        for tc in tool_calls:
            name = tc["function"]["name"]
            raw_args = tc["function"]["arguments"]
            try:
                args = json.loads(raw_args) if raw_args else {}
            except json.JSONDecodeError:
                tc["function"]["arguments"] = "{}"
                messages.append({"role": "tool", "tool_call_id": tc["id"],
                                 "content": "Malformed JSON arguments — retry."})
                continue
            # Hard guard: never let a write/exec tool through during orientation.
            if name not in _ORIENT_TOOLS:
                messages.append({"role": "tool", "tool_call_id": tc["id"],
                                 "content": f"'{name}' is not available while orienting — "
                                            "use only read-only tools. Save it for after the plan."})
                continue
            display.print_tool_call(name, args)
            result, success = execute_tool(name, args, working_dir, permission_mode)
            result = _cap_result(result, name)
            display.print_tool_result(name, result, success)
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})

    return messages


def _planning_step(
    task: str,
    system_prompt: str,
    client: OpenAI,
    model: str,
    messages: list[dict],
    working_dir: str,
    permission_mode: str,
) -> tuple[list[dict], str]:
    """
    Run an orient-then-plan pass before the main loop.

    First the model orients itself with read-only tools (``_orientation_phase``),
    then it writes the plan grounded in what it actually saw. Appends the
    orientation, the plan request, and the plan response to messages, so the
    main loop inherits all of it. Returns (updated_messages, plan_text).
    """
    messages = _orientation_phase(client, model, messages, working_dir, permission_mode)

    display.print_info("Planning…")
    plan_request = (
        "Now that you have looked around, write a concise numbered plan (3–8 "
        "steps) of exactly what you will do to complete this task. Ground it in "
        "what you just saw — name the specific files/functions you will change "
        "and what you will verify when done. Do NOT call any tools — reply with "
        "the plan only."
    )
    plan_messages = list(messages) + [{"role": "user", "content": plan_request}]
    plan_text = _simple_completion(client, model, plan_messages, max_tokens=700)
    if not plan_text:
        return messages, ""

    display.print_plan(plan_text)
    # Inject into conversation so the main loop executes against the plan.
    messages = list(messages) + [
        {"role": "user", "content": plan_request},
        {"role": "assistant", "content": plan_text},
    ]
    return messages, plan_text


def _verify_completion(
    messages: list[dict],
    task: str,
    client: OpenAI,
    model: str,
) -> str:
    """
    Run a quick verification pass after the main loop exits.
    Returns a short grade string: DONE / PARTIAL / FAILED — reason.
    """
    verify_messages = list(messages) + [{
        "role": "user",
        "content": (
            f"Original task: {task}\n\n"
            "Grade your completion in one line using exactly one of these prefixes:\n"
            "  DONE     — task fully complete, deliverable is in place\n"
            "  PARTIAL  — real progress made but something remains unfinished\n"
            "  FAILED   — could not complete the task\n\n"
            "Reply format: DONE/PARTIAL/FAILED — one sentence reason."
        ),
    }]
    return _simple_completion(client, model, verify_messages, max_tokens=100)


def _stream_completion(client: OpenAI, model: str, messages: list, force_tool: bool = False,
                       tools: list | None = None) -> dict:
    """
    Stream one completion turn. Returns:
      {"content": str, "tool_calls": list[dict], "finish_reason": str}
    Raises BadRequestError on API errors (including context-window exceeded).

    ``tools`` overrides the tool surface offered for this turn (e.g. the
    read-only subset used during the orientation phase); defaults to the full
    profile-appropriate set.
    """
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_call_map: dict[int, dict] = {}
    finish_reason = "stop"

    display.stream_start()

    is_ollama = _is_ollama_client(client)
    extra = {"extra_body": _ollama_extra_body()} if is_ollama else {}

    if tools is None:
        tools = all_tool_definitions(profile=_rt("tool_profile", None))

    with client.chat.completions.create(
        model=model,
        messages=messages,
        tools=tools,
        tool_choice="required" if force_tool else "auto",
        stream=True,
        timeout=_STREAM_READ_TIMEOUT,
        **extra,
    ) as stream:
        for chunk in stream:
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            delta = choice.delta

            # Thinking models (Ollama) stream their reasoning in a separate delta
            # field before any content/tool call. Surface it so a long thinking
            # turn shows visible progress instead of a frozen "waiting" spinner.
            reasoning = getattr(delta, "reasoning_content", None) or getattr(delta, "reasoning", None)
            if reasoning:
                reasoning_parts.append(reasoning)
                display.stream_reason(reasoning)

            if delta.content:
                content_parts.append(delta.content)
                display.stream_chunk(delta.content)

            if delta.tool_calls:
                for tc in delta.tool_calls:
                    idx = tc.index
                    if idx not in tool_call_map:
                        tool_call_map[idx] = {
                            "id": "",
                            "type": "function",
                            "function": {"name": "", "arguments": ""},
                        }
                    slot = tool_call_map[idx]
                    if tc.id:
                        slot["id"] = tc.id
                    if tc.function:
                        if tc.function.name:
                            slot["function"]["name"] += tc.function.name
                        if tc.function.arguments:
                            slot["function"]["arguments"] += tc.function.arguments

            if choice.finish_reason:
                finish_reason = choice.finish_reason

    had_content = bool(content_parts)
    display.stream_end(had_content)

    # Ensure every tool call has a non-empty id (some vllm hosts omit it)
    for i, tc in enumerate(tool_call_map.values()):
        if not tc["id"]:
            tc["id"] = f"call_{i}"
    tool_calls = [tool_call_map[i] for i in sorted(tool_call_map)]
    return {
        "content": "".join(content_parts),
        "reasoning": "".join(reasoning_parts),
        "tool_calls": tool_calls,
        "finish_reason": finish_reason,
    }


def run_agent(
    task: str,
    model: str,
    working_dir: str,
    client: OpenAI,
    prompt_profile: str = "base",
    permission_mode: str = None,
    enable_plan: bool = True,
    enable_verify: bool = False,
    enable_memory: bool = True,
    plan_out: list | None = None,
    verify_out: list | None = None,
) -> list[dict]:
    if permission_mode is None:
        cfg = load_config()
        permission_mode = cfg.get("permission_mode", "autonomous")

    # Connect any user-configured MCP servers (idempotent across calls).
    from .tools import init_mcp
    init_mcp()

    # Adapt context/tool knobs to the backend (small-model accommodations for
    # local Ollama; no-op for e-INFRA / NIM / custom).
    is_local = configure_runtime(client, model, prompt_profile)
    if is_local:
        think_note = ""
        if _rt("ollama_thinking", False):
            think_env = (os.environ.get("OCTOSLAVE_OLLAMA_THINK") or "").lower()
            think_off = think_env in ("0", "false", "no", "off")
            think_note = f", thinking {'off' if think_off else 'on'}"
        display.print_info(
            f"Local model: context window {_rt('num_ctx', OLLAMA_NUM_CTX):,} tokens, "
            f"{len(all_tool_definitions(profile=_rt('tool_profile', None)))} tools{think_note}."
        )

    system_prompt = load_system_prompt(prompt_profile, working_dir)

    # Inject session memory into system prompt when available, ranked by
    # relevance to the current task.
    if enable_memory:
        memory_ctx = load_session_memory(query=task)
        if memory_ctx:
            system_prompt = system_prompt + f"\n\n{memory_ctx}"

    # Session logger bootstrap
    logger.log_session_start(
        model=model,
        working_dir=working_dir,
        backend=client.base_url.host if hasattr(client, "base_url") else "unknown",
        prompt_profile=prompt_profile,
        permission_mode=permission_mode,
        enable_plan=enable_plan,
        enable_verify=enable_verify,
        enable_memory=enable_memory,
    )

    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": task},
    ]

    # Upfront planning pass. Skipped for local models: the dedicated non-streaming
    # planning call adds a long blind stall (model load + up to 120s) and spends
    # ~900 tokens of an already-tiny context window on a plan small models follow
    # poorly. They plan inline in the main loop instead.
    if enable_plan and not is_local:
        messages, plan_text = _planning_step(task, system_prompt, client, model, messages, working_dir, permission_mode)
        if plan_text and plan_out is not None:
            plan_out.append(plan_text)
            logger.log_plan(plan_text)
    elif enable_plan and is_local:
        display.print_info("Local model: skipping separate planning step (plans inline).")

    # Main agent loop
    messages = _agent_loop(messages, model, working_dir, client, permission_mode)

    # Post-loop verification pass
    if enable_verify:
        display.print_info("Verifying…")
        verdict = _verify_completion(messages, task, client, model)
        if verdict:
            logger.log_verify(verdict)
            display.print_verify(verdict)
            if verify_out is not None:
                verify_out.append(verdict)

    return messages


def continue_agent(
    messages: list[dict],
    follow_up: str,
    model: str,
    working_dir: str,
    client: OpenAI,
    permission_mode: str = None,
) -> list[dict]:
    if permission_mode is None:
        cfg = load_config()
        permission_mode = cfg.get("permission_mode", "autonomous")

    from .tools import init_mcp
    init_mcp()

    # Re-apply backend-adaptive knobs (per-thread state may be stale/unset on a
    # fresh follow-up turn or a different thread).
    prompt_profile = "local" if _is_ollama_client(client) else "base"
    configure_runtime(client, model, prompt_profile)

    messages.append({"role": "user", "content": follow_up})
    return _agent_loop(messages, model, working_dir, client, permission_mode)


def _robust_stream(
    client: OpenAI,
    model: str,
    messages: list[dict],
    force_tool: bool,
    state: dict,
    iteration: int,
) -> tuple[dict | None, str, list[dict]]:
    """Run one ``_stream_completion`` with the full retry / trim / error policy.

    Returns ``(response, signal, messages)`` where ``signal`` is one of:
      ``"ok"``        — ``response`` is the completion dict.
      ``"retry"``     — transient; caller should ``iteration -= 1; continue``.
                        ``messages`` may have been compacted or rolled back, and any
                        backoff sleep has already happened inside this call.
      ``"interrupt"`` — KeyboardInterrupt; caller should stop the loop.
      ``"fatal"``     — unrecoverable; the error has already been printed and the
                        caller should break.

    Retry counters persist across calls in ``state`` (keys: ``rate``/``timeout``/``conn``).
    Extracted from ``_agent_loop`` so both the single-model loop and the council loop
    (council.py) share identical robustness. Behaviour is intentionally unchanged.
    """
    import time as _time
    messages = _proactive_trim(messages)
    try:
        response = _stream_completion(client, model, messages, force_tool=force_tool)
        state["rate"] = 0
        state["timeout"] = 0
        state["conn"] = 0
        return response, "ok", messages
    except BadRequestError as e:
        err_str = str(e)
        logger.log_error(f"BadRequestError on turn {iteration}", exc=e)
        if _is_context_window_error(err_str):
            compacted = _compact_and_trim(messages, groups=10)
            if len(compacted) < len(messages):
                display.print_info(
                    f"Context window exceeded — compacted oldest turns "
                    f"(~{_estimated_tokens(compacted):,} tokens left) and retrying."
                )
                logger.log_info(
                    "Context window exceeded — compacted and retrying.",
                    est_tokens=_estimated_tokens(compacted),
                )
                return None, "retry", compacted
            display.print_error(
                "Context window exceeded and cannot be compacted further.\n"
                "Use /compact to summarise history, or /clear to start fresh."
            )
            return None, "fatal", messages
        elif "Unterminated string" in err_str or "Extra data" in err_str:
            # The model's tool-call arguments were cut off mid-stream, leaving
            # invalid JSON in the message history.  Roll back the last assistant
            # turn (and any partial tool results) and ask the model to retry.
            while messages and messages[-1].get("role") in ("tool", "assistant"):
                messages.pop()
            display.print_info(
                "Tool call arguments were truncated — rolling back and retrying."
            )
            messages.append({
                "role": "user",
                "content": (
                    "Your previous response was cut off before the tool arguments "
                    "were complete. Please redo the last action from scratch, making "
                    "sure to produce a complete, valid response."
                ),
            })
            return None, "retry", messages
        else:
            display.print_error(f"API error: {e}")
            return None, "fatal", messages
    except KeyboardInterrupt:
        return None, "interrupt", messages
    except Exception as e:
        err_str = str(e)
        if "429" in err_str or "rate" in err_str.lower() or "RateLimit" in type(e).__name__:
            state["rate"] += 1
            wait = min(60, 5 * (2 ** (state["rate"] - 1)))
            display.print_info(f"Rate limit — waiting {wait}s ({state['rate']}/5).")
            if state["rate"] > 5:
                display.print_error("Rate limit persists after 5 retries.")
                return None, "fatal", messages
            _time.sleep(wait)
            return None, "retry", messages
        elif "timeout" in err_str.lower() or "timed out" in err_str.lower() or "Timeout" in type(e).__name__:
            state["timeout"] += 1
            wait = min(30, 5 * (2 ** (state["timeout"] - 1)))
            display.print_info(f"Request timeout — retrying in {wait}s ({state['timeout']}/3).")
            if state["timeout"] > 3:
                display.print_error("Request keeps timing out after 3 retries.")
                return None, "fatal", messages
            _time.sleep(wait)
            return None, "retry", messages
        elif _is_retryable_error(e):
            # Transient connection drop ("peer closed connection without sending
            # complete message body" / "incomplete chunked read") or gateway 5xx.
            state["conn"] += 1
            wait = min(30, 5 * (2 ** (state["conn"] - 1)))
            logger.log_error(f"Connection error on turn {iteration}", exc=e)
            display.print_info(
                f"Connection dropped — retrying in {wait}s ({state['conn']}/4)."
            )
            if state["conn"] > 4:
                display.print_error("Connection keeps dropping after 4 retries.")
                return None, "fatal", messages
            _time.sleep(wait)
            return None, "retry", messages
        elif "410" in err_str and ("end of life" in err_str.lower() or "Gone" in err_str):
            display.print_error(
                "Model has reached end of life and is no longer available.\n"
                "Switch to a different model with [bold]/model <name>[/bold]  "
                "or run [bold]/model[/bold] to list available models."
            )
            return None, "fatal", messages
        elif "404" in err_str:
            if "not found for account" in err_str.lower():
                display.print_error(
                    "Model is not accessible with your NVIDIA NIM account tier.\n"
                    "This model may require a paid plan or special access.\n"
                    "Switch to a different model with [bold]/model <name>[/bold]  "
                    "or run [bold]/model[/bold] to list models available to your account."
                )
            else:
                display.print_error(
                    "Model not found (404). The model ID may be incorrect or the model\n"
                    "is no longer available at this endpoint.\n"
                    "Switch to a different model with [bold]/model <name>[/bold]  "
                    "or run [bold]/model[/bold] to list available models."
                )
            return None, "fatal", messages
        logger.log_error(f"Unexpected error on turn {iteration}", exc=e)
        display.print_error(f"Unexpected error: {e}")
        return None, "fatal", messages


def _agent_loop(
    messages: list[dict],
    model: str,
    working_dir: str,
    client: OpenAI,
    permission_mode: str = "autonomous",
) -> list[dict]:
    from collections import Counter
    iteration = 0
    _retry_state = {"rate": 0, "timeout": 0, "conn": 0}  # persisted across _robust_stream calls
    _end_reason = "completed"  # how the loop terminated (for accurate session_end log)
    _tool_call_counts: Counter = Counter()  # (name, args_json) → call count
    _no_tool_nudges = 0  # consecutive text-only responses (resets on tool use)
    _text_only_total = 0  # total text-only responses across the whole run
    _botched_attempts = 0  # consecutive unparseable text-format tool-call attempts
    _redundant_calls = 0  # total tool calls whose (name, args) had been seen before
    _consecutive_error_turns = 0  # turns in a row where at least one tool failed

    while iteration < MAX_ITERATIONS:
        iteration += 1
        # Force a tool call only on the very first turn — kick-starts models that
        # would otherwise reply with chit-chat. After that, leave tool_choice="auto"
        # so a model that has finished can naturally return a text-only "done"
        # response instead of being pushed into redundant verification calls.
        _force = iteration == 1
        response, _signal, messages = _robust_stream(
            client, model, messages, _force, _retry_state, iteration
        )
        if _signal == "retry":
            iteration -= 1  # transient error — don't count this as a used turn
            continue
        if _signal == "interrupt":
            display.stream_end(False)
            display.console.print("\n[dim]Interrupted.[/dim]")
            logger.log_session_end(iteration, reason="interrupted")
            return messages
        if _signal == "fatal":
            _end_reason = "error"
            break

        content = response["content"]
        tool_calls = response["tool_calls"]
        finish_reason = response["finish_reason"]

        # Log every turn (model response summary)
        logger.log_turn(
            iteration,
            content_preview=content[:500] if content else "",
            finish_reason=finish_reason or "",
            tool_count=len(tool_calls),
        )

        # Some models (e.g. qwen2.5-coder:3b) output tool calls as JSON text instead
        # of using the API's structured function-calling protocol.  Extract and promote
        # them so the rest of the loop can execute them normally.
        # content is also truncated at the first tool call to drop hallucinated results.
        if not tool_calls and content:
            tool_calls, content = _extract_text_tool_calls(content)
            if tool_calls:
                display.print_info(
                    "Model used text-format tool calls — extracting and executing."
                )

        assistant_msg: dict = {"role": "assistant", "content": content if content else ""}
        if tool_calls:
            assistant_msg["tool_calls"] = tool_calls
        messages.append(assistant_msg)

        if not tool_calls:
            # Distinguish a genuine "I'm done" text reply from a model that TRIED
            # to call a tool but emitted an unparseable text format (e.g. gemma4's
            # <|tool_call>call:NAME{…}<tool_call|>). The latter must not count as a
            # finished-signal, or repeated formatting failures fake a completion.
            if content and _looks_like_tool_attempt(content, valid_tool_names()):
                _botched_attempts += 1
                logger.log_info(
                    "Unparseable text-format tool call — nudging for correct format.",
                    iteration=iteration,
                )
                if _botched_attempts >= 4:
                    display.print_error(
                        "Model keeps emitting tool calls in an unsupported text format "
                        "and cannot use the function-calling interface. Stopping."
                    )
                    _end_reason = "error"
                    break
                display.print_info(
                    "Model emitted a malformed text tool call — asking it to use the "
                    "proper format."
                )
                messages.append({
                    "role": "user",
                    "content": (
                        "Your last message looked like an attempt to call a tool, but it "
                        "was not in a valid format, so nothing ran. Do NOT wrap tool calls "
                        "in <tool_call> markers or any custom syntax. Call the tool through "
                        "the function-calling interface directly. Retry the action now."
                    ),
                })
                continue
            # Genuine text-only reply: a real "done" or chit-chat.
            _botched_attempts = 0
            _no_tool_nudges += 1
            _text_only_total += 1
            # If the model has declared "done" multiple times across the run
            # (text-only response 3+ times in total, even if interleaved with
            # redundant tool calls), accept that the task is finished and stop.
            if _text_only_total >= 3 or _no_tool_nudges >= 2:
                display.print_done(iteration)
                break
            # First text-only of a streak: invite the model to either continue
            # with a tool or end the turn cleanly. Phrased so a genuinely-finished
            # model can legitimately reply with another text-only message and
            # trigger the consecutive-text-only break above on the next turn.
            messages.append({
                "role": "user",
                "content": (
                    "If the task is complete and the deliverable is in place, you may end "
                    "here — do not re-read or re-validate files you have already produced. "
                    "If real work remains, call a tool now (write_file, edit_file, bash, …) "
                    "to make progress."
                ),
            })
            continue

        # Model used tools — reset *consecutive* text-only counter, but the global
        # _text_only_total still counts so we can detect alternating loops.
        _no_tool_nudges = 0

        # Execute tool calls
        display.print_separator()
        repeated_reads: list[str] = []
        _turn_had_error = False
        for tc in tool_calls:
            name = tc["function"]["name"]
            raw_args = tc["function"]["arguments"]

            try:
                args = json.loads(raw_args) if raw_args else {}
            except json.JSONDecodeError:
                # Arguments were truncated mid-stream — sanitize before they enter
                # message history (prevents "Unterminated string" on next API call).
                tc["function"]["arguments"] = "{}"
                err_msg = (
                    f"Tool call '{name}' had malformed JSON arguments "
                    f"(the model's response was truncated). Please retry with complete arguments."
                )
                display.print_tool_result(name, err_msg, False)
                messages.append({"role": "tool", "tool_call_id": tc["id"], "content": err_msg})
                continue

            display.print_tool_call(name, args)
            logger.log_tool_call(tc.get("id", ""), name, args)
            result, success = execute_tool(name, args, working_dir, permission_mode)

            if not success:
                _turn_had_error = True

            # Cap result size BEFORE it enters the message history
            result = _cap_result(result, name)

            display.print_tool_result(name, result, success)
            result_preview = result[:400] if isinstance(result, str) else str(result)[:400]
            logger.log_tool_result(tc.get("id", ""), name, success, preview=result_preview)

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result,
                }
            )

            # Track repeated tool calls. For named read tools and read-like
            # bash commands (cat / head / tail / ls / wc / file / stat), a
            # repeat with the same args is treated as redundant verification.
            tracked = name in ("read_file", "list_dir", "glob", "grep")
            if name == "bash":
                cmd = (args.get("command") or "").strip()
                first_word = cmd.split()[0] if cmd else ""
                first_word = first_word.rsplit("/", 1)[-1]  # /usr/bin/cat → cat
                if first_word in {"cat", "head", "tail", "ls", "wc", "file", "stat", "less", "more"}:
                    tracked = True
            if tracked:
                key = (name, raw_args)
                prior = _tool_call_counts[key]
                _tool_call_counts[key] += 1
                if prior >= 1:
                    _redundant_calls += 1
                    label = args.get("file_path") or args.get("path") or args.get("pattern") or args.get("command") or ""
                    repeated_reads.append(f"{name}({label})")

        # Track consecutive error turns and inject a recovery nudge when stuck.
        if _turn_had_error:
            _consecutive_error_turns += 1
        else:
            _consecutive_error_turns = 0

        if _consecutive_error_turns >= 2:
            messages.append({
                "role": "user",
                "content": (
                    "You have encountered errors in multiple consecutive turns. "
                    "Before your next action, answer these two questions:\n"
                    "1. Diagnosis — what do you think is causing these failures?\n"
                    "2. Strategy — what will you do differently this time?\n"
                    "State your answers, then proceed with a concrete fix."
                ),
            })
            _consecutive_error_turns = 0  # reset after nudging

        # Nudge the model if it is re-reading files it has already seen
        if repeated_reads:
            nudge = (
                "You have already read these: "
                + ", ".join(repeated_reads)
                + ". Stop re-reading — you already have the content. "
                "If the task is complete, end the turn. Otherwise make a productive change "
                "(write_file, edit_file, or run code that does new work)."
            )
            messages.append({"role": "user", "content": nudge})

        # Hard stop if the agent is making sustained redundant calls — strong
        # signal that the task is complete and the model is just looping.
        if _redundant_calls >= 5:
            display.print_info(
                "Stopping: agent has made repeated calls without new progress "
                "(task likely complete)."
            )
            display.print_done(iteration)
            _end_reason = "redundant_calls"
            break

        display.print_separator()
    else:
        display.print_info(f"Reached max iterations ({MAX_ITERATIONS}).")
        display.print_done(iteration)  # unblock web UI — done event must always fire
        logger.log_session_end(iteration, reason="max_iterations")
        return messages

    logger.log_session_end(iteration, reason=_end_reason)
    return messages
