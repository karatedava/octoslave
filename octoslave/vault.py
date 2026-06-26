"""
OctoSlave — Vault improvement pipeline.

Autonomous, resumable pipeline that improves an entire Obsidian-style vault:

  Planning  →  [Editor → Verifier → (Editor fix)] × N batches  →  Reporter

The vault is split into batches of files (grouped by folder). Each batch goes
through an editor that rewrites and enriches the notes, then a verifier that
fact-checks via web search. Any confirmed errors are fixed before moving on.
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from openai import OpenAI, BadRequestError

from . import display
from .agent import _cap_result, _compact_and_trim as _trim_messages
from .tools import TOOL_DEFINITIONS, execute_tool

# ---------------------------------------------------------------------------
# Role registry
# ---------------------------------------------------------------------------

VAULT_ROLES: dict[str, dict] = {
    "editor": {
        "label": "Editor",
        "icon": "✏️ ",
        "color": "bold bright_green",
        "default_model": "deepseek-v3.2",
        "max_iter": 80,
        "tools": ["read_file", "write_file", "edit_file", "web_search",
                  "web_fetch", "glob", "grep", "list_dir"],
    },
    "verifier": {
        "label": "Verifier",
        "icon": "🔍",
        "color": "bold bright_magenta",
        "default_model": "deepseek-v3.2-thinking",
        "max_iter": 40,
        "tools": ["read_file", "web_search", "web_fetch", "write_file"],
    },
    "reporter": {
        "label": "Reporter",
        "icon": "📋",
        "color": "bold bright_cyan",
        "default_model": "deepseek-v3.2",
        "max_iter": 20,
        "tools": ["read_file", "write_file", "glob"],
    },
}

MAX_FILES_PER_BATCH = 8

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tools_for_role(role: str) -> list[dict]:
    allowed = set(VAULT_ROLES[role]["tools"])
    return [t for t in TOOL_DEFINITIONS if t["function"]["name"] in allowed]


def _save_plan(plan: dict, plan_file: Path) -> None:
    plan_file.write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")


def _load_profile_style(prompt_profile: str, vault_path: str) -> str:
    """Load style instructions from a prompt profile, if it exists."""
    try:
        from .agent import load_system_prompt
        return load_system_prompt(prompt_profile, vault_path)
    except Exception:
        return ""


def _discover_batches(vault_path: str) -> list[dict]:
    """
    Discover all .md files in the vault and group them into batches by folder.
    Batches are at most MAX_FILES_PER_BATCH files each.
    """
    vault = Path(vault_path)
    all_files = sorted(
        p for p in vault.rglob("*.md")
        if ".vault_work" not in p.parts and not p.name.startswith(".")
    )

    groups: dict[str, list[str]] = defaultdict(list)
    for f in all_files:
        rel = f.relative_to(vault)
        parent = str(rel.parent) if str(rel.parent) != "." else "(root)"
        groups[parent].append(str(rel))

    batches: list[dict] = []
    batch_id = 1
    for folder, files in sorted(groups.items()):
        for i in range(0, len(files), MAX_FILES_PER_BATCH):
            chunk = files[i:i + MAX_FILES_PER_BATCH]
            label = folder
            if len(files) > MAX_FILES_PER_BATCH:
                part = i // MAX_FILES_PER_BATCH + 1
                label = f"{folder} (part {part})"
            batches.append({
                "id": batch_id,
                "label": label,
                "folder": folder,
                "files": chunk,
                "file_count": len(chunk),
                "status": "pending",
            })
            batch_id += 1

    return batches


# ---------------------------------------------------------------------------
# Core agent loop
# ---------------------------------------------------------------------------

def _stream_with_tools(client: OpenAI, model: str, messages: list, tools: list) -> dict:
    """Stream a completion with tool calls. Returns {content, tool_calls, finish_reason}."""
    content_parts: list[str] = []
    tool_call_map: dict[int, dict] = {}
    finish_reason = "stop"

    display.stream_start()
    with client.chat.completions.create(
        model=model, messages=messages, tools=tools,
        tool_choice="auto", stream=True,
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
                            "id": "", "type": "function",
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

    display.stream_end(bool(content_parts))
    for i, tc in enumerate(tool_call_map.values()):
        if not tc["id"]:
            tc["id"] = f"call_{i}"
    return {
        "content": "".join(content_parts),
        "tool_calls": [tool_call_map[i] for i in sorted(tool_call_map)],
        "finish_reason": finish_reason,
    }


def _run_vault_agent(
    role: str,
    model: str,
    system_prompt: str,
    user_msg: str,
    vault_path: str,
    client: OpenAI,
    done_check=None,
) -> bool:
    """
    Run a vault agent to completion.
    done_check: optional callable() -> bool, checked when the model stops calling tools.
    Returns True on success, False on fatal error.
    """
    cfg = VAULT_ROLES[role]
    tools = _tools_for_role(role)
    max_iter = cfg["max_iter"]

    display.console.print(
        f"\n  [{cfg['color']}]{cfg['icon']} {cfg['label']}[/{cfg['color']}]"
        f"  [dim]→  {model}[/dim]"
    )
    display.console.print()

    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user",   "content": user_msg},
    ]

    iteration = 0
    _rl = 0
    _to = 0

    while iteration < max_iter:
        iteration += 1
        try:
            response = _stream_with_tools(client, model, messages, tools)
            _rl = _to = 0
        except BadRequestError as e:
            err = str(e)
            if "ContextWindow" in err or "context" in err.lower():
                display.print_info(f"  [{cfg['label']}] Context limit — trimming history.")
                messages = _trim_messages(messages)
                iteration -= 1
                continue
            if "Unterminated string" in err or "Extra data" in err:
                while messages and messages[-1].get("role") in ("tool", "assistant"):
                    messages.pop()
                messages.append({"role": "user", "content":
                    "Your previous response was cut off. Please redo the last action with complete arguments."})
                iteration -= 1
                continue
            display.print_error(f"[{cfg['label']}] API error: {e}")
            return False
        except Exception as e:
            err_str = str(e)
            if "429" in err_str or "rate" in err_str.lower() or "RateLimit" in type(e).__name__:
                _rl += 1
                wait = min(60, 5 * (2 ** (_rl - 1)))
                display.print_info(f"  [{cfg['label']}] Rate limit — waiting {wait}s ({_rl}/5).")
                if _rl > 5:
                    display.print_error(f"[{cfg['label']}] Rate limit persists. Aborting batch.")
                    return False
                time.sleep(wait)
                iteration -= 1
                continue
            elif "timeout" in err_str.lower() or "Timeout" in type(e).__name__:
                _to += 1
                wait = min(30, 5 * (2 ** (_to - 1)))
                display.print_info(f"  [{cfg['label']}] Timeout — retrying in {wait}s ({_to}/3).")
                if _to > 3:
                    display.print_error(f"[{cfg['label']}] Keeps timing out. Aborting.")
                    return False
                time.sleep(wait)
                iteration -= 1
                continue
            display.print_error(f"[{cfg['label']}] Unexpected error: {e}")
            return False
        except KeyboardInterrupt:
            display.stream_end(False)
            raise

        content  = response["content"]
        tool_calls = response["tool_calls"]
        finish_reason = response["finish_reason"]

        assistant_msg: dict = {"role": "assistant", "content": content or ""}
        if tool_calls:
            assistant_msg["tool_calls"] = tool_calls
        messages.append(assistant_msg)

        if not tool_calls:
            if done_check is None or done_check():
                break
            messages.append({"role": "user", "content":
                "You have not finished yet — continue until all files are processed."})
            continue

        if finish_reason == "stop":
            break

        display.print_separator()
        for tc in tool_calls:
            name = tc["function"]["name"]
            try:
                args = json.loads(tc["function"]["arguments"] or "{}")
            except json.JSONDecodeError:
                args = {}
            display.print_tool_call(name, args)
            result, success = execute_tool(name, args, vault_path)
            result = _cap_result(result, name)
            display.print_tool_result(name, result, success)
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})

    return True


# ---------------------------------------------------------------------------
# Phase prompts
# ---------------------------------------------------------------------------

_EDITOR_PROMPT = """\
You are the Editor in the OctoSlave Vault Improvement pipeline.
Your job is to rewrite and enrich a batch of notes, one file at a time.

VAULT     : {vault_path}
BATCH     : {batch_label}

FILES TO IMPROVE (process every single one, in order):
{file_list}

WRITING STYLE & RULES:
{profile_style}

WHAT TO DO FOR EACH FILE:
1. read_file — read the current content
2. write_file — write the complete, improved version (overwrite the file)

IMPROVEMENTS TO MAKE in every file:
- Standardize Markdown: consistent #/##/### hierarchy, **bold** key terms, tables where useful
- Add [[wikilinks]] for every important concept that deserves its own note
- Fill in missing important information (verify with web_search if uncertain)
- Add ## Testovací otázky at the bottom — 5-8 questions (mix of easy, hard, and tricky)
- Add ## Zajímavosti — 3-5 surprising or little-known facts related to the topic
- Add ## Kontext block at top listing [[parent topics]] and [[related topics]]
- Make it longer and deeper — a note that took 5 minutes to write should take 30 to improve

CONSTRAINTS:
- Process EVERY file in the list — skipping is not allowed
- Use write_file for each file with the FULL improved content
- Do not create files that are not in the list above
- If a claim is uncertain, use web_search before writing (max 2 searches per file)
- Stop calling tools only after the last file in the list is written
"""

_VERIFIER_PROMPT = """\
You are the Verifier in the OctoSlave Vault Improvement pipeline.
Your job is fact-checking: find and document any factually wrong claims.

VAULT      : {vault_path}
BATCH      : {batch_label}
ISSUES FILE: {issues_file}

FILES TO VERIFY:
{file_list}

YOUR TASK:
1. For each file: read_file → scan for factual claims
2. For any claim that seems questionable: web_search to verify (be targeted — 1-2 searches per file max)
3. After checking all files: write your findings as a JSON array to {issues_file}

JSON FORMAT:
[
  {{
    "file": "relative/path/to/file.md",
    "claim": "exact text that is wrong (copy verbatim from the file)",
    "issue": "what is actually correct",
    "suggested_fix": "exact replacement text to use in the file"
  }}
]

Write [] if no issues found — you MUST write the file regardless.

RULES:
- Only flag claims you have POSITIVELY CONFIRMED are wrong via web search
- Do NOT flag things you are uncertain about
- Do NOT flag style, formatting, or incomplete sections — only wrong facts
- Be specific: "claim" must be the exact substring from the file so it can be found
"""

_EDITOR_FIX_PROMPT = """\
You are the Editor in fix mode in the OctoSlave Vault Improvement pipeline.
Your job is to apply targeted factual corrections flagged by the Verifier.

VAULT      : {vault_path}
ISSUES:
{issues_json}

YOUR TASK:
For each issue in the list above:
1. read_file — read the file (use the "file" field for the path)
2. edit_file — replace the wrong claim (old_string = "claim" field) with the fix ("suggested_fix" field)

RULES:
- Use edit_file for targeted replacements — do NOT rewrite whole files
- Only fix exactly what is listed — do not make other changes
- old_string must match exactly — read the file first if unsure
- Process every issue in the list
"""

_REPORTER_PROMPT = """\
You are the Reporter in the OctoSlave Vault Improvement pipeline.
Write a concise summary report of everything that was done.

VAULT        : {vault_path}
WORK DIR     : {work_dir}
TOTAL BATCHES: {total_batches}
TOTAL FILES  : {total_files}
DATE         : {date}

YOUR TASK:
1. glob for all *_issues.json files in {work_dir}
2. read_file each issues file to count and categorize corrections
3. Write a report to {report_file}:

# Vault Improvement Report

**Date:** {date}
**Files improved:** {total_files}
**Batches:** {total_batches}

## What Was Improved
[High-level summary: what kinds of improvements were made across the vault]

## Factual Corrections
[List every correction made, grouped by file. If no issues were found, say so.]

## Notes
[Any patterns noticed, suggestions for future improvements]
"""


# ---------------------------------------------------------------------------
# Phase runners
# ---------------------------------------------------------------------------

def _run_editor_phase(
    batch: dict, vault_path: str, profile_style: str, client: OpenAI, model: str,
) -> None:
    file_list = "\n".join(f"  - {f}" for f in batch["files"])
    system = _EDITOR_PROMPT.format(
        vault_path=vault_path,
        batch_label=batch["label"],
        file_list=file_list,
        profile_style=profile_style or "(Use clear, thorough, well-structured Markdown.)",
    )
    user = (
        f"Improve all {batch['file_count']} files in batch '{batch['label']}'. "
        "Process them one by one. Do not stop until every file has been written."
    )
    files_set = set(batch["files"])
    _run_vault_agent(
        "editor", model, system, user, vault_path, client,
        done_check=lambda: all(
            (Path(vault_path) / f).exists() for f in files_set
        ),
    )


def _run_verifier_phase(
    batch: dict, vault_path: str, work_dir: Path, client: OpenAI, model: str,
) -> list[dict]:
    issues_file = work_dir / f"batch_{batch['id']:03d}_issues.json"
    file_list = "\n".join(f"  - {f}" for f in batch["files"])
    system = _VERIFIER_PROMPT.format(
        vault_path=vault_path,
        batch_label=batch["label"],
        file_list=file_list,
        issues_file=str(issues_file),
    )
    user = (
        f"Fact-check all {batch['file_count']} files in batch '{batch['label']}'. "
        f"Write your findings (even if empty []) to {issues_file}."
    )
    _run_vault_agent(
        "verifier", model, system, user, vault_path, client,
        done_check=lambda: issues_file.exists(),
    )

    if not issues_file.exists():
        display.print_info("  [dim]Verifier produced no issues file — assuming clean.[/dim]")
        return []
    try:
        issues = json.loads(issues_file.read_text(encoding="utf-8"))
        if issues:
            display.print_info(f"  [yellow]⚠  Verifier flagged {len(issues)} issue(s) — fixing.[/yellow]")
        else:
            display.print_info("  [green]✓  Verifier found no factual issues.[/green]")
        return issues
    except json.JSONDecodeError:
        display.print_info("  [dim]Verifier output was not valid JSON — skipping fix pass.[/dim]")
        return []


def _run_editor_fix_phase(
    batch: dict, issues: list[dict], vault_path: str, profile_style: str,
    client: OpenAI, model: str,
) -> None:
    issues_json = json.dumps(issues, indent=2, ensure_ascii=False)
    system = _EDITOR_FIX_PROMPT.format(
        vault_path=vault_path,
        issues_json=issues_json,
    )
    user = f"Apply {len(issues)} fix(es) to the files listed above using edit_file."
    _run_vault_agent("editor", model, system, user, vault_path, client)


def _run_reporter_phase(
    vault_path: str, work_dir: Path, plan: dict, client: OpenAI, model: str,
) -> None:
    report_file = work_dir / "report.md"
    total_files = sum(b["file_count"] for b in plan["batches"])
    system = _REPORTER_PROMPT.format(
        vault_path=vault_path,
        work_dir=str(work_dir),
        total_batches=len(plan["batches"]),
        total_files=total_files,
        date=datetime.now().strftime("%Y-%m-%d %H:%M"),
        report_file=str(report_file),
    )
    user = f"Write the vault improvement report to {report_file}."
    _run_vault_agent(
        "reporter", model, system, user, vault_path, client,
        done_check=lambda: report_file.exists(),
    )


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

def _print_vault_header(vault_path: str, profile: str, total_batches: int, total_files: int) -> None:
    from rich.text import Text
    from rich.panel import Panel
    lines = Text()
    lines.append("🗺  VAULT IMPROVEMENT PIPELINE\n\n", style="bold bright_white")
    lines.append("Vault    : ", style="dim"); lines.append(vault_path + "\n", style="bold white")
    lines.append("Profile  : ", style="dim"); lines.append(profile + "\n", style="bold white")
    lines.append("Files    : ", style="dim"); lines.append(f"{total_files}\n", style="bold white")
    lines.append("Batches  : ", style="dim"); lines.append(f"{total_batches}\n", style="bold white")
    lines.append("\nPipeline : ", style="dim")
    lines.append("[Editor → Verifier → Fix?] × batch", style="bold bright_green")
    lines.append("  →  ", style="dim")
    lines.append("Reporter\n", style="bold bright_cyan")
    display.console.print(Panel(lines, border_style="bright_blue", padding=(0, 2)))
    display.console.print()


def _print_batch_header(batch: dict, total: int) -> None:
    display.console.print()
    display.console.print(f"[bold bright_blue]{'─' * 60}[/bold bright_blue]")
    bar_filled = "█" * batch["id"]
    bar_empty  = "░" * (total - batch["id"])
    display.console.print(
        f"  [bold bright_white]BATCH {batch['id']} / {total}[/bold bright_white]"
        f"  [bright_blue]{bar_filled}[/bright_blue][dim]{bar_empty}[/dim]"
        f"  [dim]{batch['label']}[/dim]"
        f"  [dim]({batch['file_count']} files)[/dim]"
    )
    display.console.print(f"[bold bright_blue]{'─' * 60}[/bold bright_blue]")


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_vault_improve(
    vault_path: str,
    client: OpenAI,
    prompt_profile: str = "base",
    model: str | None = None,
    resume: bool = False,
) -> None:
    """
    Autonomously improve every note in vault_path.

    Args:
        vault_path:     Path to the Obsidian vault (or any folder of .md files).
        client:         Authenticated OpenAI client.
        prompt_profile: Writing style profile (e.g. 'base').
        model:          Model override for all agents (uses role defaults if None).
        resume:         If True, skip batches already marked done in plan.json.
    """
    vault = Path(vault_path).resolve()
    work_dir = vault / ".vault_work"
    work_dir.mkdir(parents=True, exist_ok=True)
    plan_file = work_dir / "plan.json"

    editor_model   = model or VAULT_ROLES["editor"]["default_model"]
    verifier_model = model or VAULT_ROLES["verifier"]["default_model"]
    reporter_model = model or VAULT_ROLES["reporter"]["default_model"]

    profile_style = _load_profile_style(prompt_profile, str(vault))

    # ── Planning ──────────────────────────────────────────────────────────
    if resume and plan_file.exists():
        plan = json.loads(plan_file.read_text(encoding="utf-8"))
        display.print_info(f"  ↩  Resuming — {len(plan['batches'])} batches in plan.")
    else:
        display.print_info("  🗺  Discovering files and building batch plan…")
        batches = _discover_batches(str(vault))
        if not batches:
            display.print_error(f"No .md files found in {vault_path}.")
            return
        plan = {
            "vault_path": str(vault),
            "created_at": datetime.now().isoformat(),
            "prompt_profile": prompt_profile,
            "batches": batches,
        }
        _save_plan(plan, plan_file)
        display.print_info(f"  Plan saved → {plan_file}")

    total_batches = len(plan["batches"])
    total_files   = sum(b["file_count"] for b in plan["batches"])
    done_count    = sum(1 for b in plan["batches"] if b["status"] == "done")

    _print_vault_header(str(vault), prompt_profile, total_batches, total_files)

    if done_count:
        display.print_info(f"  {done_count}/{total_batches} batches already done — skipping.")

    # ── Batch loop ────────────────────────────────────────────────────────
    for batch in plan["batches"]:
        if batch["status"] == "done":
            continue

        _print_batch_header(batch, total_batches)

        try:
            # 1. Editor
            batch["status"] = "editing"
            _save_plan(plan, plan_file)
            _run_editor_phase(batch, str(vault), profile_style, client, editor_model)

            # 2. Verifier
            batch["status"] = "verifying"
            _save_plan(plan, plan_file)
            issues = _run_verifier_phase(batch, str(vault), work_dir, client, verifier_model)

            # 3. Fix pass (only if issues found)
            if issues:
                batch["status"] = "fixing"
                _save_plan(plan, plan_file)
                _run_editor_fix_phase(batch, issues, str(vault), profile_style, client, editor_model)

            batch["status"] = "done"
            _save_plan(plan, plan_file)
            display.print_info(
                f"  [green]✓[/green]  Batch {batch['id']} / {total_batches} complete"
                f" — {batch['file_count']} files improved."
            )

        except KeyboardInterrupt:
            # Reset so this batch will be retried on resume
            batch["status"] = "pending"
            _save_plan(plan, plan_file)
            display.console.print(
                "\n[bold yellow]Vault improvement paused.[/bold yellow]\n"
                f"Progress saved to [dim]{plan_file}[/dim]\n"
                "Re-run with [cyan]--resume[/cyan] to continue from this batch."
            )
            return

    # ── Reporter ──────────────────────────────────────────────────────────
    display.console.print()
    display.console.print(f"[bold bright_blue]{'─' * 60}[/bold bright_blue]")
    display.console.print("  [bold bright_cyan]📋  Reporter[/bold bright_cyan]  [dim]— writing summary[/dim]")
    display.console.print(f"[bold bright_blue]{'─' * 60}[/bold bright_blue]")

    _run_reporter_phase(str(vault), work_dir, plan, client, reporter_model)

    report_file = work_dir / "report.md"
    display.console.print()
    display.console.print(
        f"[bold bright_green]✅  Vault improvement complete![/bold bright_green]\n"
        f"  [dim]{total_files} files improved across {total_batches} batches.[/dim]\n"
        f"  Report → [cyan]{report_file}[/cyan]"
    )
