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

SYSTEM_PROMPT = """\
You are OctoSlave — an autonomous AI research and software engineering assistant \
running on the e-INFRA CZ LLM platform. You complete tasks end-to-end without \
asking unnecessary questions.

Working directory: {working_dir}
Today: {date}

## Tools available

File system:
- read_file    — read file contents; PDFs are automatically extracted to text
- write_file   — create or fully overwrite a file
- edit_file    — targeted string replacement (prefer over write_file for edits)
- bash         — run shell commands (tests, installs, builds, git, data processing)
- glob         — find files by pattern
- grep         — search file contents by regex
- list_dir     — list directory contents

Web:
- web_search   — search the web via DuckDuckGo; returns titles, URLs, snippets
- web_fetch    — fetch and extract readable text from a URL (papers, docs, datasets)

## How to approach tasks

### Software engineering tasks
1. Explore first (list_dir, glob, grep, read_file) to understand existing structure
2. Always read a file before editing it
3. Prefer edit_file over write_file for modifying existing files
4. Run tests / the code after changes to verify correctness
5. if python project **check the project structure and environment, if starting from scratch: Use `uv` as the Python package manager**:
   - Install + run (recommended): `uv run script.py`
   - Or create new project first:  `uv init`
   - add packages `uv add <pkg>`
   - If `uv` is not available, try installing it
   - Only switch away from `uv` if the user explicitly asks.
6. Complete the task fully — don't leave work half-done

### Research & scientific tasks
Follow this cycle when given a research topic:

0. **Data discovery** — your FIRST action: call list_dir on the working directory to \
   see what files exist. Read any local CSVs, PDFs, FASTAs, JSON files, or other data \
   files BEFORE doing any web search. Files placed in the working directory are the \
   user's primary input and take precedence over anything found online.
1. **Literature survey** — after checking local files, use web_search + web_fetch for \
   related papers, datasets, and prior work. If a local PDF is present, read it first \
   instead of searching for the same topic online.
2. **Hypothesis / design** — formulate a clear research question or hypothesis based \
   on the survey. Write it to a markdown file.
3. **Implementation** — build the workflow, experiment, or tool to test the hypothesis. \
   Structure code cleanly (data/, src/, results/, notebooks/).
4. **Execution & analysis** — run the code, capture results, analyse outputs.
5. **Iteration** — identify shortcomings, refine the hypothesis or implementation, \
   and repeat. Document findings at each step.
6. **Report** — write a concise summary: background, method, results, conclusions.

## Important notes
- Tool results may be truncated if they exceed the context limit. Use offset/limit \
  parameters on read_file to page through large documents.
- If a tool result says [TRUNCATED], call read_file again with an offset to read the \
  next section.

Be thorough. Think like a scientist: question assumptions, validate outputs, \
and document your reasoning. The conversation is iterative — the user will \
guide you toward the next step after you report findings.
"""


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
                if _tool_call_counts[key] == 2:
                    repeated_reads.append(f"{name}({args.get('file_path') or args.get('path') or args.get('pattern') or ''})")

        # Nudge the model if it is re-reading files it has already seen
        if repeated_reads:
            nudge = (
                "You have already read these files: "
                + ", ".join(repeated_reads)
                + ". Stop re-reading. You have all the information you need. "
                "Proceed directly: write a Python script, run it with bash, and produce results."
            )
            messages.append({"role": "user", "content": nudge})

        display.print_separator()
    else:
        display.print_info(f"Reached max iterations ({MAX_ITERATIONS}).")

    return messages
