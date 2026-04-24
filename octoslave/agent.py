"""Core agent loop for octoslave."""

import json
from datetime import date
from pathlib import Path
from openai import OpenAI, BadRequestError

from . import display
from .tools import TOOL_DEFINITIONS, execute_tool
from .config import load_config

MAX_ITERATIONS = 100

# Hard cap on characters in a single tool result that goes into the message history.
# Prevents a single large file/page from blowing up the context window.
MAX_TOOL_RESULT_CHARS = 50_000

# Path to prompt profiles directory
PROMPT_PROFILES_DIR = Path(__file__).parent / "prompt_profiles"


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


def _trim_messages(messages: list[dict], groups: int = 3) -> list[dict]:
    """
    Remove the oldest N complete assistant-turn groups (assistant message +
    all its tool results) to free context space.  Always preserves the system
    prompt and the first user message (messages[:2]).
    """
    system = messages[:2]
    rest   = list(messages[2:])

    removed = 0
    while removed < groups and rest:
        start = next(
            (i for i, m in enumerate(rest)
             if m.get("role") == "assistant" and m.get("tool_calls")),
            None,
        )
        if start is None:
            break
        end = start + 1
        while end < len(rest) and rest[end].get("role") == "tool":
            end += 1
        rest = rest[:start] + rest[end:]
        removed += 1

    return system + rest


def make_client(api_key: str, base_url: str) -> OpenAI:
    # Ollama doesn't require a real API key; use a placeholder so the SDK
    # doesn't raise a missing-key error.
    return OpenAI(
        api_key=api_key if api_key else "ollama",
        base_url=base_url,
    )


def _cap_result(result: str, tool_name: str) -> str:
    """Truncate oversized tool results before they enter the message history."""
    if len(result) <= MAX_TOOL_RESULT_CHARS:
        return result
    kept = result[:MAX_TOOL_RESULT_CHARS]
    omitted = len(result) - MAX_TOOL_RESULT_CHARS
    return (
        kept
        + f"\n\n[TRUNCATED — {omitted:,} more characters omitted. "
        f"Use read_file with offset/limit to read the next section if needed.]"
    )


def _stream_completion(client: OpenAI, model: str, messages: list) -> dict:
    """
    Stream one completion turn. Returns:
      {"content": str, "tool_calls": list[dict], "finish_reason": str}
    Raises BadRequestError on API errors (including context-window exceeded).
    """
    content_parts: list[str] = []
    tool_call_map: dict[int, dict] = {}
    finish_reason = "stop"

    display.stream_start()

    with client.chat.completions.create(
        model=model,
        messages=messages,
        tools=TOOL_DEFINITIONS,
        tool_choice="auto",
        stream=True,
    ) as stream:
        for chunk in stream:
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            delta = choice.delta

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
) -> list[dict]:
    if permission_mode is None:
        cfg = load_config()
        permission_mode = cfg.get("permission_mode", "autonomous")
    
    system_prompt = load_system_prompt(prompt_profile, working_dir)
    messages: list[dict] = [
        {
            "role": "system",
            "content": system_prompt,
        },
        {"role": "user", "content": task},
    ]
    return _agent_loop(messages, model, working_dir, client, permission_mode)


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
    
    messages.append({"role": "user", "content": follow_up})
    return _agent_loop(messages, model, working_dir, client, permission_mode)


def _agent_loop(
    messages: list[dict], 
    model: str, 
    working_dir: str, 
    client: OpenAI,
    permission_mode: str = "autonomous",
) -> list[dict]:
    import time as _time
    from collections import Counter
    iteration = 0
    _rate_limit_retries = 0
    _tool_call_counts: Counter = Counter()  # (name, args_json) → call count

    while iteration < MAX_ITERATIONS:
        iteration += 1
        try:
            response = _stream_completion(client, model, messages)
            _rate_limit_retries = 0
        except BadRequestError as e:
            err_str = str(e)
            if "ContextWindowExceeded" in err_str or "context" in err_str.lower():
                trimmed = _trim_messages(messages)
                if len(trimmed) < len(messages):
                    display.print_info(
                        "Context window exceeded — trimming oldest tool results and retrying."
                    )
                    messages = trimmed
                    iteration -= 1  # context trim doesn't consume a turn
                    continue
                # Nothing left to trim
                display.print_error(
                    "Context window exceeded and cannot be trimmed further.\n"
                    "Use /compact to summarise history, or /clear to start fresh."
                )
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
                iteration -= 1
                continue
            else:
                display.print_error(f"API error: {e}")
            break
        except KeyboardInterrupt:
            display.stream_end(False)
            display.console.print("\n[dim]Interrupted.[/dim]")
            break
        except Exception as e:
            err_str = str(e)
            if "429" in err_str or "rate" in err_str.lower() or "RateLimit" in type(e).__name__:
                _rate_limit_retries += 1
                wait = min(60, 5 * (2 ** (_rate_limit_retries - 1)))
                display.print_info(f"Rate limit — waiting {wait}s ({_rate_limit_retries}/5).")
                if _rate_limit_retries > 5:
                    display.print_error("Rate limit persists after 5 retries.")
                    break
                _time.sleep(wait)
                iteration -= 1  # don't count this as a used iteration
                continue
            display.print_error(f"Unexpected error: {e}")
            break

        content = response["content"]
        tool_calls = response["tool_calls"]
        finish_reason = response["finish_reason"]

        assistant_msg: dict = {"role": "assistant", "content": content if content else ""}
        if tool_calls:
            assistant_msg["tool_calls"] = tool_calls
        messages.append(assistant_msg)

        if not tool_calls or finish_reason == "stop":
            display.print_done(iteration)
            break

        # Execute tool calls
        display.print_separator()
        repeated_reads: list[str] = []
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
            result, success = execute_tool(name, args, working_dir, permission_mode)

            # Cap result size BEFORE it enters the message history
            result = _cap_result(result, name)

            display.print_tool_result(name, result, success)

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result,
                }
            )

            # Track repeated read-only tool calls (only reads, not writes/bash)
            if name in ("read_file", "list_dir", "glob", "grep"):
                key = (name, raw_args)
                _tool_call_counts[key] += 1
                if _tool_call_counts[key] >= 2:
                    repeated_reads.append(f"{name}({args.get('file_path') or args.get('path') or args.get('pattern') or ''})")

        # Nudge the model if it is re-reading files it has already seen
        if repeated_reads:
            nudge = (
                "You have already read these files: "
                + ", ".join(repeated_reads)
                + ". Stop re-reading them — you already have the content. "
                "Proceed directly to the next step: make changes, run commands, or produce output."
            )
            messages.append({"role": "user", "content": nudge})

        display.print_separator()
    else:
        display.print_info(f"Reached max iterations ({MAX_ITERATIONS}).")

    return messages
