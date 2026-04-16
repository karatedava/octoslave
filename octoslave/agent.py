"""Core agent loop for octoslave."""

import json
from datetime import date
from openai import OpenAI, BadRequestError

from . import display
from .tools import TOOL_DEFINITIONS, execute_tool

MAX_ITERATIONS = 80

# Hard cap on characters in a single tool result that goes into the message history.
# Prevents a single large file/page from blowing up the context window.
MAX_TOOL_RESULT_CHARS = 50_000

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
5. **Use `uv` as the Python package manager** (faster, safer than pip):
   - Install packages : `uv pip install <pkg>`
   - Run a script     : `uv run python script.py`
   - New project      : `uv init <name>` then `uv add <pkg>`
   - Virtual env      : `uv venv && source .venv/bin/activate`
   - If `uv` is not available, fall back to `pip` and note it.
   - Only switch away from `uv` if the user explicitly asks.
6. Complete the task fully — don't leave work half-done

### Research & scientific tasks
Follow this cycle when given a research topic:

1. **Literature survey** — use read_file on PDFs in the literature folder, or \
   web_search + web_fetch to find relevant papers, datasets, and prior work.
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
) -> list[dict]:
    messages: list[dict] = [
        {
            "role": "system",
            "content": SYSTEM_PROMPT.format(
                working_dir=working_dir,
                date=date.today().isoformat(),
            ),
        },
        {"role": "user", "content": task},
    ]
    return _agent_loop(messages, model, working_dir, client)


def continue_agent(
    messages: list[dict],
    follow_up: str,
    model: str,
    working_dir: str,
    client: OpenAI,
) -> list[dict]:
    messages.append({"role": "user", "content": follow_up})
    return _agent_loop(messages, model, working_dir, client)


def _agent_loop(messages: list[dict], model: str, working_dir: str, client: OpenAI) -> list[dict]:
    for iteration in range(1, MAX_ITERATIONS + 1):
        try:
            response = _stream_completion(client, model, messages)
        except BadRequestError as e:
            err_str = str(e)
            if "ContextWindowExceeded" in err_str or "context" in err_str.lower():
                display.print_error(
                    "Context window exceeded — the conversation history is too long.\n"
                    "Use /compact to summarise history, or /clear to start fresh."
                )
            else:
                display.print_error(f"API error: {e}")
            break
        except KeyboardInterrupt:
            display.stream_end(False)
            display.console.print("\n[dim]Interrupted.[/dim]")
            break
        except Exception as e:
            display.print_error(f"Unexpected error: {e}")
            break

        content = response["content"]
        tool_calls = response["tool_calls"]
        finish_reason = response["finish_reason"]

        assistant_msg: dict = {"role": "assistant", "content": content or None}
        if tool_calls:
            assistant_msg["tool_calls"] = tool_calls
        messages.append(assistant_msg)

        if not tool_calls or finish_reason == "stop":
            display.print_done(iteration)
            break

        # Execute tool calls
        display.print_separator()
        for tc in tool_calls:
            name = tc["function"]["name"]
            raw_args = tc["function"]["arguments"]

            try:
                args = json.loads(raw_args) if raw_args else {}
            except json.JSONDecodeError:
                args = {}

            display.print_tool_call(name, args)
            result, success = execute_tool(name, args, working_dir)

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
        display.print_separator()

    else:
        display.print_info(f"Reached max iterations ({MAX_ITERATIONS}).")

    return messages
