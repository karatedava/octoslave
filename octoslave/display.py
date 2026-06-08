"""Rich terminal display helpers for octoslave."""

import json
import sys
import time as _time
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.rule import Rule
from rich.text import Text
from rich.theme import Theme

import threading as _threading

# ---------------------------------------------------------------------------
# Web UI event bridge  (no-op when not in web mode)
# ---------------------------------------------------------------------------

_tl = _threading.local()  # thread-local storage for per-session callbacks

# Permission request state — shared across threads (agent sets, WS handler resolves)
_perm_lock = _threading.Lock()
_perm_event: "_threading.Event | None" = None
_perm_result: bool = False


def set_event_callback(cb) -> None:
    """Register a structured-event callback for the current thread (web mode)."""
    _tl.emit = cb


def clear_event_callback() -> None:
    """Remove the callback for the current thread."""
    _tl.emit = None


def get_event_callback():
    """Return the current thread's emit callback (used to propagate into worker threads)."""
    return getattr(_tl, "emit", None)


def set_silent_cli(silent: bool) -> None:
    """Suppress this thread's CLI/Rich output (events still flow through emit).

    Used by parallel-mode workers so their per-candidate output doesn't
    interleave with other workers — instead, the parent thread prints
    compact prefixed milestone lines.
    """
    _tl.silent_cli = silent


def _silent() -> bool:
    return bool(getattr(_tl, "silent_cli", False))


def resolve_permission(allow: bool) -> None:
    """Called from the web handler to unblock a pending permission request."""
    global _perm_result, _perm_event
    with _perm_lock:
        _perm_result = allow
        if _perm_event is not None:
            _perm_event.set()


def _emit(event: dict) -> None:
    """Fire a structured JSON event to the registered callback; no-op otherwise."""
    cb = getattr(_tl, "emit", None)
    if cb is not None:
        try:
            cb(event)
        except Exception:
            pass


_THEME = Theme(
    {
        "tool.name": "bold #fab283",
        "tool.arg": "dim #7a7d86",
        "tool.ok": "dim #7fd88f",
        "tool.err": "bold #e06c75",
        "info": "dim #7a7d86",
        "heading": "bold bright_white",
        "model": "bold #9d7cd8",
        "prompt": "bold #f5a742",
        "mascot": "#fab283",
    }
)

console = Console(theme=_THEME, highlight=False)
err_console = Console(stderr=True, theme=_THEME)

# ---------------------------------------------------------------------------
# Verbose mode
# ---------------------------------------------------------------------------

_verbose: bool = False


def set_verbose(v: bool) -> None:
    global _verbose
    _verbose = v


def is_verbose() -> bool:
    return _verbose


# ---------------------------------------------------------------------------
# Session header
#
# Aesthetic: open layout, no surrounding panel, a single accent line under
# the wordmark, all status info on one row separated by middle dots.
# ---------------------------------------------------------------------------

# 5-row block-letter "OCTOSLAVE" wordmark. 3-wide letters, 1-space gaps.
# Total width 53 — readable in any monospace terminal.
_WORDMARK = [
    " ███   ███  █████  ███   ███  █      ███  █   █ █████",
    "█   █ █   █   █   █   █ █     █     █   █ █   █ █    ",
    "█   █ █       █   █   █  ███  █     █████ █   █ █████",
    "█   █ █   █   █   █   █     █ █     █   █  █ █  █    ",
    " ███   ███    █    ███   ███  █████ █   █   █   █████",
]


def _backend_palette(backend: str) -> tuple[str, str, str]:
    """Return (primary_hex, label, accent_glyph_color) for a backend."""
    if backend == "ollama":
        return "#7fd88f", "local · ollama", "#7fd88f"
    if backend == "nim":
        return "#5c9cf5", "nvidia nim", "#5c9cf5"
    return "#fab283", "e-infra cz", "#fab283"


def print_welcome(model: str, working_dir: str, backend: str = "einfra"):
    """Render the session header: wordmark, status row, and a hint line."""
    primary, label, _ = _backend_palette(backend)

    wd = working_dir if len(working_dir) <= 60 else "…" + working_dir[-58:]

    rule_width = max(len(row) for row in _WORDMARK)

    console.print()
    for row in _WORDMARK:
        console.print(f"  [bold {primary}]{row}[/bold {primary}]")
    console.print(f"  [dim {primary}]{'─' * rule_width}[/dim {primary}]")
    console.print()

    status = Text("  ")
    status.append("●", style=f"bold {primary}")
    status.append(f"  {label}", style="bold #d0d1d6")
    status.append("  ·  ", style="dim #4a4d56")
    status.append(model, style="bold #9d7cd8")
    status.append("  ·  ", style="dim #4a4d56")
    status.append(wd, style="dim #7a7d86")
    console.print(status)

    hint = "  [dim #7a7d86]type[/dim #7a7d86] [bold #d0d1d6]/help[/bold #d0d1d6] " \
           "[dim #7a7d86]for commands · [/dim #7a7d86][dim #7a7d86]@[/dim #7a7d86]" \
           "[dim #7a7d86] for files · [/dim #7a7d86][dim #7a7d86]Ctrl+T[/dim #7a7d86]" \
           "[dim #7a7d86] toggles permission[/dim #7a7d86]"
    if backend == "ollama":
        hint = "  [dim #7a7d86]type[/dim #7a7d86] [bold #d0d1d6]/help[/bold #d0d1d6] " \
               "[dim #7a7d86]· [/dim #7a7d86][bold #d0d1d6]/pull <model>[/bold #d0d1d6] " \
               "[dim #7a7d86]· [/dim #7a7d86][bold #d0d1d6]/einfra[/bold #d0d1d6] " \
               "[dim #7a7d86]to switch back[/dim #7a7d86]"
    console.print(hint)
    console.print()


def print_header(model: str, working_dir: str, backend: str = "einfra"):
    """One-shot run header: same wordmark logic, even more compact."""
    primary, label, _ = _backend_palette(backend)
    wd = working_dir if len(working_dir) <= 60 else "…" + working_dir[-58:]
    console.print()
    console.print(
        f"  [bold {primary}]octoslave[/bold {primary}]  "
        f"[dim #4a4d56]·[/dim #4a4d56]  [bold #d0d1d6]{label}[/bold #d0d1d6]  "
        f"[dim #4a4d56]·[/dim #4a4d56]  [bold #9d7cd8]{model}[/bold #9d7cd8]  "
        f"[dim #4a4d56]·[/dim #4a4d56]  [dim #7a7d86]{wd}[/dim #7a7d86]"
    )
    console.print()


# ---------------------------------------------------------------------------
# Task display
# ---------------------------------------------------------------------------

def print_task(task: str):
    """Render the user's task as a quoted block — no bordered panel."""
    if _silent():
        return
    from rich.markup import escape as _escape
    console.print()
    # Left-side accent rule + the task text — a clean quote block.
    lines = task.splitlines() or [task]
    for ln in lines:
        console.print(f"  [bold #fab283]▌[/bold #fab283] [bold #d0d1d6]{_escape(ln)}[/bold #d0d1d6]")
    console.print()


# ---------------------------------------------------------------------------
# Streaming text
# ---------------------------------------------------------------------------

_stream_state = _threading.local()

_SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
# Carriage return + ANSI "erase entire line" — wipes the spinner regardless
# of how long the message grew (the elapsed-seconds counter expands forever).
_SPINNER_CLEAR  = "\r\x1b[2K"


def _spinning(stop_event: _threading.Event) -> None:
    """Background thread: animate a waiting indicator until stop_event is set."""
    i = 0
    while not stop_event.wait(timeout=0.1):
        frame = _SPINNER_FRAMES[i % len(_SPINNER_FRAMES)]
        elapsed = int(i * 0.1)
        sys.stdout.write(f"\r  {frame} waiting for model… {elapsed}s")
        sys.stdout.flush()
        i += 1
    sys.stdout.write(_SPINNER_CLEAR)
    sys.stdout.flush()


def _streaming_started() -> bool:
    return getattr(_stream_state, "started", False)


def stream_start():
    _stream_state.started = False
    _emit({"type": "stream_start"})
    # CLI-only spinner — skip when a web event callback is active OR this
    # thread is silenced (parallel-mode worker).
    if _silent() or getattr(_tl, "emit", None) is not None:
        _stream_state.stop_event = None
        _stream_state.spinner_thread = None
        return
    stop = _threading.Event()
    _stream_state.stop_event = stop
    t = _threading.Thread(target=_spinning, args=(stop,), daemon=True)
    _stream_state.spinner_thread = t
    t.start()


def stream_chunk(text: str):
    _emit({"type": "token", "text": text})
    if _silent():
        return
    if not _streaming_started():
        _stream_state.started = True
        _stream_state.buffer = ""
    _stream_state.buffer = getattr(_stream_state, "buffer", "") + text


def stream_end(had_content: bool):
    _emit({"type": "stream_end"})
    if _silent():
        _stream_state.started = False
        return
    # Stop the spinner
    stop: _threading.Event = getattr(_stream_state, "stop_event", None)
    if stop is not None:
        stop.set()
        t: _threading.Thread = getattr(_stream_state, "spinner_thread", None)
        if t is not None:
            t.join(timeout=0.3)
    if had_content:
        buf = getattr(_stream_state, "buffer", "")
        if buf:
            console.print("  [bold #fab283]◆[/bold #fab283]")
            console.print(Markdown(buf))
            console.print()
        _stream_state.buffer = ""
    _stream_state.started = False


# ---------------------------------------------------------------------------
# Tool display
# ---------------------------------------------------------------------------

# Icons per tool
_TOOL_ICONS = {
    "read_file":  "📄",
    "write_file": "✏️ ",
    "edit_file":  "🔧",
    "bash":       "⚡",
    "glob":       "🔍",
    "grep":       "🔎",
    "list_dir":   "📁",
    "web_search": "🌐",
    "web_fetch":  "🌍",
    "apply_patch": "🩹",
    "todo_write":  "✅",
    "run_background": "🚀",
    "check_process":  "📡",
    "stop_process":   "🛑",
    "ask_user":    "❓",
    "remember":    "🧠",
}


def print_tool_call(name: str, args: dict):
    """Emit a structured tool-call event and (in non-silent mode) print it.

    The web UI uses ``args_preview`` to render inline diffs and code previews
    for file-mutating tools. Args are clipped to keep the wire payload bounded
    — the raw values still hit disk via the actual tool call.
    """
    payload: dict = {
        "type": "tool_call",
        "name": name,
        "summary": _tool_summary(name, args),
    }

    # Per-tool rich preview for the web UI. Bash, write_file, edit_file all
    # benefit from showing the user the actual change rather than a one-line
    # summary buried inside a <details> block.
    _MAX_FIELD = 4000
    if name == "edit_file":
        payload["args_preview"] = {
            "path": args.get("path", "") or "",
            "old_string": (args.get("old_string", "") or "")[:_MAX_FIELD],
            "new_string": (args.get("new_string", "") or "")[:_MAX_FIELD],
            "replace_all": bool(args.get("replace_all", False)),
        }
    elif name == "write_file":
        content = args.get("content", "") or ""
        payload["args_preview"] = {
            "path": args.get("path", "") or "",
            "content": content[:_MAX_FIELD],
            "lines": content.count("\n") + (1 if content else 0),
            "truncated": len(content) > _MAX_FIELD,
        }
    elif name == "bash":
        payload["args_preview"] = {
            "command": (args.get("command", "") or "")[:1000],
        }
    elif name == "read_file":
        payload["args_preview"] = {"path": args.get("path", "") or ""}
    elif name == "list_dir":
        payload["args_preview"] = {"path": args.get("path", "") or "."}
    elif name == "glob":
        payload["args_preview"] = {
            "pattern": args.get("pattern", "") or "",
            "path": args.get("path", "") or "",
        }
    elif name == "grep":
        payload["args_preview"] = {
            "pattern": args.get("pattern", "") or "",
            "path": args.get("path", "") or "",
        }
    elif name == "web_search":
        payload["args_preview"] = {"query": args.get("query", "") or ""}
    elif name == "web_fetch":
        payload["args_preview"] = {"url": args.get("url", "") or ""}

    _emit(payload)
    if _silent():
        return
    icon = _TOOL_ICONS.get(name, "⚙")
    summary = _tool_summary(name, args)
    console.print(f"  [tool.name]{icon} {name}[/tool.name] [tool.arg]{summary}[/tool.arg]")

    from rich.markup import escape as _escape

    # Compact previews shown by default for the most informative tools.
    # In verbose mode we expand to the full content; otherwise we cap at a
    # few lines and append a "… N more" hint so the chat stays scannable.
    DEFAULT_LINES = 4

    if name == "edit_file":
        old = args.get("old_string", "") or ""
        new = args.get("new_string", "") or ""
        old_lines = old.splitlines()
        new_lines = new.splitlines()
        if _verbose:
            cap_old = old_lines
            cap_new = new_lines
        else:
            cap_old = old_lines[:DEFAULT_LINES]
            cap_new = new_lines[:DEFAULT_LINES]
        for ln in cap_old:
            console.print(f"    [#e06c75]- {_escape(ln[:120])}[/#e06c75]")
        if not _verbose and len(old_lines) > DEFAULT_LINES:
            console.print(f"    [dim]… {len(old_lines) - DEFAULT_LINES} more removed[/dim]")
        for ln in cap_new:
            console.print(f"    [#7fd88f]+ {_escape(ln[:120])}[/#7fd88f]")
        if not _verbose and len(new_lines) > DEFAULT_LINES:
            console.print(f"    [dim]… {len(new_lines) - DEFAULT_LINES} more inserted[/dim]")

    elif name == "write_file":
        content = args.get("content", "") or ""
        lines = content.splitlines()
        if _verbose:
            cap = lines
        else:
            cap = lines[:DEFAULT_LINES]
        for ln in cap:
            console.print(f"    [dim #d0d1d6]│ {_escape(ln[:120])}[/dim #d0d1d6]")
        if not _verbose and len(lines) > DEFAULT_LINES:
            console.print(f"    [dim]… {len(lines) - DEFAULT_LINES} more lines[/dim]")

    elif name == "bash":
        cmd = args.get("command", "") or ""
        # Always show the command — agents shouldn't run hidden bash.
        cap = cmd if _verbose or len(cmd) <= 200 else cmd[:200] + "…"
        console.print(f"    [bold #7fd88f]$[/bold #7fd88f] [#d0d1d6]{_escape(cap)}[/#d0d1d6]")

    elif name == "web_fetch" and _verbose:
        console.print(f"    [dim]↳ {_escape(args.get('url', ''))}[/dim]")

    elif name == "web_search" and _verbose:
        console.print(f"    [dim]↳ \"{_escape(args.get('query', ''))}\"[/dim]")


def print_tool_result(name: str, result: str, success: bool):
    from rich.markup import escape as _escape
    _emit({"type": "tool_result", "name": name, "ok": success,
           "preview": (result.strip()[:400] if result.strip() else "")})
    if _silent():
        return
    if not result.strip():
        return
    if not success:
        console.print(f"    [tool.err]✗ {_escape(result.strip())}[/tool.err]")
        return
    lines = result.splitlines()
    if _verbose:
        for ln in lines:
            console.print(f"    [tool.ok]{_escape(ln)}[/tool.ok]")
    else:
        show = lines[:6]
        preview = "\n".join(f"    {_escape(ln)}" for ln in show)
        if len(lines) > 6:
            preview += f"\n    [info]… {len(lines) - 6} more lines[/info]"
        console.print(f"[tool.ok]{preview}[/tool.ok]")


def _tool_summary(name: str, args: dict) -> str:
    if name == "read_file":
        return args.get("path", "")
    if name in ("write_file", "edit_file"):
        return args.get("path", "")
    if name == "bash":
        cmd = args.get("command", "")
        return (cmd[:90] + "…") if len(cmd) > 90 else cmd
    if name == "glob":
        return args.get("pattern", "") + (f"  in {args['path']}" if args.get("path") else "")
    if name == "grep":
        return args.get("pattern", "") + (f"  in {args['path']}" if args.get("path") else "")
    if name == "list_dir":
        return args.get("path", ".")
    if name == "web_search":
        return args.get("query", "")
    if name == "web_fetch":
        url = args.get("url", "")
        return (url[:80] + "…") if len(url) > 80 else url
    if name == "apply_patch":
        edits = args.get("edits") or []
        n = len(edits) if isinstance(edits, list) else 0
        return f"{args.get('path', '')}  ({n} edit{'s' if n != 1 else ''})"
    if name == "todo_write":
        todos = args.get("todos") or []
        n = len(todos) if isinstance(todos, list) else 0
        return f"{n} task{'s' if n != 1 else ''}"
    if name in ("run_background", "check_process", "stop_process"):
        return args.get("command", "") or args.get("id", "") or "(all)"
    if name == "ask_user":
        q = args.get("question", "")
        return (q[:90] + "…") if len(q) > 90 else q
    if name == "remember":
        c = args.get("content", "")
        return (c[:90] + "…") if len(c) > 90 else c
    if name.startswith("mcp__"):
        return name
    return json.dumps(args)[:80]


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------

def print_separator():
    if _silent():
        return
    console.print(Rule(style="dim"))


def print_info(msg: str):
    _emit({"type": "info", "text": msg})
    if _silent():
        return
    console.print(f"[info]{msg}[/info]")


def print_error(msg: str):
    _emit({"type": "error", "text": msg})
    if _silent():
        return
    err_console.print(f"[tool.err]Error:[/tool.err] {msg}")


def print_done(iterations: int):
    _emit({"type": "done", "iterations": iterations})
    if _silent():
        return
    console.print()
    console.print(
        f"  [bold #7fd88f]✓[/bold #7fd88f] [dim #7a7d86]done · "
        f"{iterations} iteration{'s' if iterations != 1 else ''}[/dim #7a7d86]"
    )
    console.print()


# ---------------------------------------------------------------------------
# Todo list (task tracking)
# ---------------------------------------------------------------------------

_TODO_GLYPHS = {
    "completed":   ("[#7fd88f]✓[/#7fd88f]", "#7a7d86", "s"),
    "in_progress": ("[#f5a742]▸[/#f5a742]", "#f5a742", ""),
    "pending":     ("[#7a7d86]○[/#7a7d86]", "#d0d1d6", ""),
}


def print_todos(todos: list[dict]):
    """Render a task checklist and emit a structured event for the web UI."""
    _emit({"type": "todos", "todos": todos})
    if _silent():
        return
    from rich.markup import escape as _escape
    if not todos:
        return
    console.print()
    done = sum(1 for t in todos if t.get("status") == "completed")
    console.print(
        f"  [bold]Tasks[/bold] [dim]· {done}/{len(todos)} done[/dim]"
    )
    for t in todos:
        status = t.get("status", "pending")
        glyph, text_color, _ = _TODO_GLYPHS.get(status, _TODO_GLYPHS["pending"])
        content = t.get("content", "")
        if status == "completed":
            console.print(f"    {glyph} [dim {text_color}]{_escape(content)}[/dim {text_color}]")
        else:
            console.print(f"    {glyph} [{text_color}]{_escape(content)}[/{text_color}]")
    console.print()


# ---------------------------------------------------------------------------
# ask_user — interactive question round-trip (CLI input / web event / fallback)
# ---------------------------------------------------------------------------

_ask_lock = _threading.Lock()
_ask_event: "_threading.Event | None" = None
_ask_answer: str = ""


def resolve_user_response(answer: str) -> None:
    """Called from the web handler to unblock a pending ask_user request."""
    global _ask_answer, _ask_event
    with _ask_lock:
        _ask_answer = answer or ""
        if _ask_event is not None:
            _ask_event.set()


def ask_user_question(question: str, options: list[str] | None = None) -> tuple[str, bool]:
    """Ask the user a question and return (answer, answered).

    Web mode  → emit a ``user_question`` event and block for the browser reply.
    CLI mode  → prompt on the console.
    Otherwise → not answerable (autonomous/non-interactive); caller should
    proceed with its own judgement.
    """
    options = [o for o in (options or []) if o]

    # Web mode: emit and wait for resolve_user_response.
    if getattr(_tl, "emit", None) is not None:
        global _ask_event, _ask_answer
        with _ask_lock:
            _ask_event = _threading.Event()
            _ask_answer = ""
        _emit({"type": "user_question", "question": question, "options": options})
        got = _ask_event.wait(timeout=600)  # 10-min window
        with _ask_lock:
            answer = _ask_answer
            _ask_event = None
        if not got:
            return "", False
        return answer, True

    # CLI mode: only if we have a real interactive stdin.
    if sys.stdin and sys.stdin.isatty() and not _silent():
        console.print()
        console.print(f"  [bold #f5a742]▌[/bold #f5a742] [bold]The agent is asking:[/bold]")
        console.print(f"  [bold #f5a742]▌[/bold #f5a742] {question}")
        if options:
            for i, opt in enumerate(options, 1):
                console.print(f"  [bold #f5a742]▌[/bold #f5a742]   [cyan]{i}.[/cyan] {opt}")
        try:
            resp = console.input("  [bold #f5a742]▌[/bold #f5a742] your answer: ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print()
            return "", False
        # Allow choosing an option by number.
        if options and resp.isdigit() and 1 <= int(resp) <= len(options):
            resp = options[int(resp) - 1]
        return resp, bool(resp)

    # Non-interactive (autonomous pipeline, no tty).
    return "", False


def print_local_research_assignment(assignment: dict[str, str]):
    """Show which local model was assigned to each research role."""
    console.print()
    console.print("[bold bright_green]● Local research model assignment:[/bold bright_green]")
    # Group by model to keep output compact
    model_to_roles: dict[str, list[str]] = {}
    for role, model in assignment.items():
        model_to_roles.setdefault(model, []).append(role)
    for model, roles in model_to_roles.items():
        console.print(f"  [bold]{model}[/bold]  →  {', '.join(roles)}")
    console.print(
        "[dim]  (up to 3 local models used; roles distributed by priority tier)[/dim]\n"
    )


def print_role_models_config(backend: str, effective: dict, custom: dict):
    """Show current per-role model assignments and which ones are customised."""
    from rich.table import Table
    console.print()
    console.print(
        f"[bold white]Research role models[/bold white]  "
        f"[dim](backend: {backend})[/dim]"
    )
    table = Table(show_header=True, header_style="bold dim", box=None, padding=(0, 2))
    table.add_column("Role", style="bold cyan", min_width=14)
    table.add_column("Model", style="")
    table.add_column("", style="dim")
    for role, model in effective.items():
        flag = "[yellow]custom[/yellow]" if role in custom else "[dim]default[/dim]"
        table.add_row(role, model, flag)
    console.print(table)
    console.print(
        "[dim]  /research-roles <role> <model>   — set a custom model for a role[/dim]\n"
        "[dim]  /research-roles reset             — clear all custom overrides[/dim]\n"
    )


def print_plan(plan_text: str):
    """Display the upfront execution plan as a quoted block (no panel border)."""
    from rich.markup import escape as _escape
    _emit({"type": "plan", "text": plan_text})
    if _silent():
        return
    console.print()
    console.print("  [bold #9d7cd8]◆ Plan[/bold #9d7cd8]")
    for ln in plan_text.splitlines() or [plan_text]:
        console.print(f"  [dim #9d7cd8]│[/dim #9d7cd8] [#d0d1d6]{_escape(ln)}[/#d0d1d6]")
    console.print()


def print_verify(result: str):
    """Display the post-loop verification verdict."""
    result = result.strip()
    upper = result.upper()
    if upper.startswith("DONE"):
        style = "bold #7fd88f"
        label = "✓ Verification"
    elif upper.startswith("PARTIAL"):
        style = "bold #f5a742"
        label = "⚠ Verification"
    else:
        style = "bold #e06c75"
        label = "✗ Verification"
    console.print()
    console.print(f"  [{style}]{label}:[/{style}] [dim]{result}[/dim]")
    console.print()


def print_help():
    console.print(Panel(
        "[bold white]Slash commands[/bold white]\n\n"
        "  [cyan]/model [NAME][/cyan]           Switch model (or list if no name given)\n"
        "  [cyan]/dir [PATH][/cyan]             Change working directory\n"
        "  [cyan]/profile [NAME][/cyan]         Switch prompt profile (base/coder/analyst/biomedic)\n"
        "  [cyan]/permission [MODE][/cyan]      Switch permission mode (autonomous/controlled/supervised)\n"
        "  [cyan]/verbose[/cyan]                Toggle verbose mode (show full diffs & output live)\n"
        "  [cyan]/clear[/cyan]                  Clear screen and conversation history\n"
        "  [cyan]/compact[/cyan]                Summarise history to save context\n"
        "  [cyan]/undo[/cyan]                   Rewind the last user/assistant exchange (history only)\n"
        "  [cyan]/share[/cyan]                  Save the conversation as a read-only share snapshot\n"
        "  [cyan]/parallel N task[/cyan]        Run N agents on the same task; pick best/vote/merge\n"
        "  [cyan]/long-research TOPIC[/cyan]    Launch multi-agent research pipeline\n"
        "  [cyan]/research-roles[/cyan]         View/set per-role models for research pipeline\n"
        "  [cyan]/vault-improve [PATH][/cyan]   Autonomously improve all notes in vault\n"
        "  [cyan]/help[/cyan]                   Show this help\n"
        "  [cyan]/exit[/cyan]                   Quit  (also Ctrl+D)\n\n"
        "[bold white]Agentic behaviour:[/bold white]\n"
        "  [cyan]/show-plan[/cyan]              Show the plan generated at the start of the last task\n"
        "  [cyan]/plan on|off[/cyan]            Enable/disable the upfront planning step (default: on)\n"
        "  [cyan]/verify on|off[/cyan]          Enable/disable post-task verification grade (default: off)\n"
        "  [cyan]/memory[/cyan]                 Show cross-session memory (remembered insights + prior task outcomes)\n"
        "  [cyan]/memory clear[/cyan]           Erase the session memory file\n"
        "  [cyan]/memory on|off[/cyan]          Enable/disable memory loading/saving (default: on)\n\n"
        "[bold white]Backend switching:[/bold white]\n"
        "  [cyan]/local [MODEL][/cyan]          Switch to local Ollama models\n"
        "  [cyan]/einfra[/cyan]                 Switch to e-INFRA CZ\n"
        "  [cyan]/nim [MODEL][/cyan]            Switch to NVIDIA NIM\n"
        "  [cyan]/pull MODEL[/cyan]             Pull a new Ollama model\n"
        "  [cyan]/provider …[/cyan]            Manage custom OpenAI-compatible providers\n\n"
        "[bold white]MCP — wire in custom tools:[/bold white]\n"
        "  [cyan]/mcp[/cyan]                    List configured MCP servers + live status\n"
        "  [cyan]/mcp registry[/cyan]           Browse the catalog of recommended servers\n"
        "  [cyan]/mcp install ID[/cyan]         Install a server from the catalog\n"
        "  [cyan]/mcp add[/cyan]                Add a custom server (stdio or http) — interactive\n"
        "  [cyan]/mcp add NAME CMD…[/cyan]      Quick-add a custom stdio server\n"
        "  [cyan]/mcp remove NAME[/cyan]        Delete a server\n"
        "  [cyan]/mcp enable|disable NAME[/cyan]  Toggle a server\n"
        "  [cyan]/mcp reconnect[/cyan]          Reconnect all servers\n\n"
        "[bold white]Permission modes:[/bold white]\n"
        "  [cyan]autonomous[/cyan]  — work without asking (default)\n"
        "  [cyan]controlled[/cyan]  — ask before file edits or commands\n"
        "  [cyan]supervised[/cyan]  — ask before file edits, auto-allow commands\n\n"
        "[bold white]/long-research flags:[/bold white]\n"
        "  [cyan]--rounds N[/cyan]              Number of research rounds (default 5)\n"
        "  [cyan]--parallel N[/cyan]            Run N parallel copies of researcher/hypothesis/evaluator\n"
        "  [cyan]--overseer MODEL[/cyan]        Model for orchestrator\n"
        "  [cyan]--all MODEL[/cyan]             Use one model for all agents\n"
        "  [cyan]--role ROLE MODEL[/cyan]       Override model for a specific role\n"
        "  [cyan]--resume[/cyan]                Resume an interrupted run\n"
        "  [cyan]--scrape[/cyan]                Scrape mode: crawl website tree\n"
        "  [dim](local mode: up to 3 pulled models auto-assigned across roles)[/dim]\n\n"
        "[bold white]/research-roles usage:[/bold white]\n"
        "  [cyan]/research-roles[/cyan]                   Show current role assignments\n"
        "  [cyan]/research-roles coder qwen3-coder[/cyan]  Set model for a specific role\n"
        "  [cyan]/research-roles reset[/cyan]              Reset to backend defaults\n\n"
        "[bold white]/vault-improve flags:[/bold white]\n"
        "  [cyan]PATH[/cyan]                  Vault directory (default: working dir)\n"
        "  [cyan]--model MODEL[/cyan]         Override model for all vault agents\n"
        "  [cyan]--resume[/cyan]              Resume interrupted pipeline\n"
        "  [dim]Pipeline: Planner → [Editor → Verifier → Fix] per batch → Reporter[/dim]\n\n"
        "[dim]Output per round: plots → 03_code/results/, HTML report → 07_report.html[/dim]\n"
        "[dim]Final master report: research/final_report.html  (open in browser)[/dim]\n\n"
        "[dim]Ctrl+C  pause current agent (progress saved)\n"
        "Ctrl+T  toggle permission mode (autonomous ↔ controlled)\n"
        "Type @ in the prompt to autocomplete a file from the working directory[/dim]",
        title="[prompt]Help[/prompt]",
        border_style="dim",
        padding=(0, 2),
    ))


# ---------------------------------------------------------------------------
# Multi-agent research display
# ---------------------------------------------------------------------------

def print_research_start(topic: str, max_rounds: int, roles: dict, overrides: dict):
    _emit({"type": "research_start", "topic": topic, "max_rounds": max_rounds})
    lines = Text()
    lines.append("🐙 AUTONOMOUS RESEARCH PIPELINE\n\n", style="bold bright_white")
    lines.append("Topic   : ", style="dim"); lines.append(topic + "\n", style="bold white")
    lines.append("Rounds  : ", style="dim"); lines.append(str(max_rounds) + "\n", style="bold white")
    lines.append("\nAgents  :\n", style="dim")
    for role, cfg in roles.items():
        model = overrides.get(role) or cfg["default_model"]
        lines.append(f"  {cfg['icon']}  ", style="")
        lines.append(f"{cfg['label']:<24}", style=cfg["color"])
        lines.append(f"→  {model}\n", style="dim")
    console.print(Panel(lines, border_style="bright_blue", padding=(0, 2)))
    console.print()


def print_round_header(round_num: int, max_rounds: int, round_dir: str):
    _emit({"type": "round_start", "round": round_num,
           "max_rounds": max_rounds, "dir": round_dir})
    bar_filled = "█" * round_num
    bar_empty  = "░" * (max_rounds - round_num)
    console.print()
    console.print(
        f"[bold bright_blue]{'─'*60}[/bold bright_blue]"
    )
    console.print(
        f"  [bold bright_white]ROUND {round_num} / {max_rounds}[/bold bright_white]  "
        f"[bright_blue]{bar_filled}[/bright_blue][dim]{bar_empty}[/dim]  "
        f"[dim]{round_dir}[/dim]"
    )
    console.print(
        f"[bold bright_blue]{'─'*60}[/bold bright_blue]"
    )
    console.print()


def print_agent_banner(role: str, model: str, round_num: int, max_rounds: int):
    cfg = _get_role_cfg(role)
    _emit({"type": "agent_start", "role": role, "label": cfg["label"],
           "icon": cfg["icon"], "model": model,
           "round": round_num, "max_rounds": max_rounds})
    console.print()
    console.print(
        Panel.fit(
            f"{cfg['icon']}  [{cfg['color']}]{cfg['label'].upper()}[/{cfg['color']}]"
            f"   [dim]model: {model}   round {round_num}/{max_rounds}[/dim]",
            border_style=cfg["color"].replace("bold ", ""),
            padding=(0, 2),
        )
    )
    console.print()


def print_agent_done(role: str, elapsed: float, iterations: int):
    _emit({"type": "agent_done", "role": role,
           "elapsed": f"{elapsed:.0f}s", "iterations": iterations})
    cfg = _get_role_cfg(role)
    console.print(
        f"\n  [{cfg['color']}]✓ {cfg['label']}[/{cfg['color']}] "
        f"[dim]done — {iterations} iter, {elapsed:.0f}s[/dim]"
    )
    console.print()


def print_round_done(round_num: int, round_dir: str):
    _emit({"type": "round_done", "round": round_num, "dir": round_dir})
    console.print(
        f"[dim]  Round {round_num} complete → {round_dir}[/dim]"
    )


def print_research_complete(rounds_done: int, research_dir: str):
    from pathlib import Path as _Path
    report_path = str(_Path(research_dir) / "final_report.html")
    _emit({"type": "research_complete", "rounds": rounds_done, "dir": research_dir, "report_path": report_path})
    console.print()
    console.print(Panel(
        f"[bold bright_white]🎉 Research complete[/bold bright_white]\n\n"
        f"[dim]Rounds completed : {rounds_done}[/dim]\n"
        f"[dim]Output directory : {research_dir}[/dim]\n\n"
        "[white]Key files:[/white]\n"
        f"  [bold cyan]{research_dir}/final_report.html[/bold cyan]    ← master HTML report (open in browser)\n"
        f"  [cyan]{research_dir}/findings.md[/cyan]              ← cumulative findings\n"
        f"  [cyan]{research_dir}/round_*/07_report.html[/cyan]   ← per-round HTML reports\n"
        f"  [cyan]{research_dir}/round_*/06_synthesis.md[/cyan]  ← per-round synthesis\n"
        f"  [cyan]{research_dir}/round_*/03_code/results/[/cyan] ← plots & data",
        border_style="bright_green",
        padding=(0, 2),
    ))
    console.print()


def request_permission(tool_name: str, args: dict, working_dir: str, permission_mode: str = "controlled") -> bool:
    """
    Request user permission for a tool that modifies state.
    Returns True if allowed, False if denied.
    
    In controlled mode, this displays a prompt and waits for user input.
    In supervised mode, the title reflects that bash commands are auto-approved.
    """
    # Build a descriptive message about what the tool will do
    icon = _TOOL_ICONS.get(tool_name, "⚙")
    
    if tool_name == "write_file":
        path = args.get("path", "")
        desc = f"create/overwrite file: {path}"
    elif tool_name == "edit_file":
        path = args.get("path", "")
        desc = f"edit file: {path}"
    elif tool_name == "bash":
        cmd = args.get("command", "")
        cmd_preview = (cmd[:60] + "...") if len(cmd) > 60 else cmd
        desc = f"run command: {cmd_preview}"
    else:
        desc = f"execute: {tool_name}"
    
    # Mode label + accent colour.
    if permission_mode == "supervised":
        mode_label = "supervised"
        accent = "#5c9cf5"
    else:
        mode_label = "controlled"
        accent = "#f5a742"

    # Compact, panel-free quote block — same visual language as the task line.
    console.print()
    console.print(
        f"  [bold {accent}]▌[/bold {accent}] "
        f"[bold]Permission required[/bold] "
        f"[dim]· {mode_label}[/dim]"
    )
    console.print(f"  [bold {accent}]▌[/bold {accent}] {icon} [bold]{desc}[/bold]")
    console.print(
        f"  [bold {accent}]▌[/bold {accent}] [dim]in {working_dir}[/dim]"
    )

    # Web mode: emit a permission_request event and wait for the browser response
    if getattr(_tl, "emit", None) is not None:
        global _perm_event, _perm_result
        with _perm_lock:
            _perm_event = _threading.Event()
            _perm_result = False
        _emit({
            "type": "permission_request",
            "tool": tool_name,
            "desc": desc,
            "icon": icon,
            "working_dir": working_dir,
            "mode": permission_mode,
        })
        _perm_event.wait(timeout=300)   # 5-minute timeout → default deny
        with _perm_lock:
            result = _perm_result
            _perm_event = None
        return result

    # Console mode: block on user input
    while True:
        try:
            response = console.input(
                "[bold green]Allow?[/bold green] [cyan](y)[/cyan]/[red](n)[/red] "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            console.print()
            return False

        if response in ("y", "yes", "ok", "allow"):
            return True
        elif response in ("n", "no", "deny"):
            return False
        else:
            console.print("[dim]Please enter 'y' for yes or 'n' for no.[/dim]")


def _get_role_cfg(role: str) -> dict:
    """Import ROLES lazily to avoid circular imports."""
    from .research import ROLES
    return ROLES.get(role, {"label": role, "icon": "⚙", "color": "white"})
