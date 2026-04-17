"""CLI entrypoint for octoslave — interactive TUI + one-shot run mode."""

import os
import sys
from pathlib import Path

import click
from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from prompt_toolkit.styles import Style
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.key_binding import KeyBindings

from . import display
from .agent import make_client, run_agent, continue_agent
from .research import run_long_research, ROLES as RESEARCH_ROLES
from .config import (
    KNOWN_MODELS, DEFAULT_MODEL, BASE_URL, OLLAMA_BASE_URL,
    load_config, save_config,
    ollama_is_running, ollama_list_models, ollama_pull_model,
    assign_local_models,
)

# ---------------------------------------------------------------------------
# Prompt-toolkit style
# ---------------------------------------------------------------------------

_PT_STYLE = Style.from_dict(
    {
        "prompt":         "bold #cc44ff",
        "prompt-local":   "bold #44ffaa",   # green tint in local mode
        "model-tag":      "#888888",
        "input":          "#ffffff",
        "bottom-toolbar": "bg:#1a001a #666666",
        "bottom-toolbar-local": "bg:#001a0a #666666",
    }
)

_HISTORY_FILE = Path.home() / ".octoslave" / "history"


# ---------------------------------------------------------------------------
# Main CLI group
# ---------------------------------------------------------------------------

@click.group(
    invoke_without_command=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)
@click.option("-m", "--model", default=None, help="Model to use")
@click.option("-d", "--dir", "working_dir", default=None, help="Working directory")
@click.option("--api-key", default=None, envvar="OCTOSLAVE_API_KEY")
@click.option("--base-url", default=None, envvar="OCTOSLAVE_BASE_URL")
@click.option("--local", is_flag=True, default=False, help="Use local Ollama models")
@click.pass_context
def cli(ctx, model, working_dir, api_key, base_url, local):
    """OctoSlave — autonomous AI research & coding assistant.

    Run without arguments to enter interactive mode.
    """
    ctx.ensure_object(dict)
    ctx.obj["model"] = model
    ctx.obj["working_dir"] = working_dir
    ctx.obj["api_key"] = api_key
    ctx.obj["base_url"] = base_url
    ctx.obj["local"] = local

    if ctx.invoked_subcommand is None:
        _interactive(ctx.obj)


# ---------------------------------------------------------------------------
# `run` sub-command — one-shot task
# ---------------------------------------------------------------------------

@cli.command()
@click.argument("task")
@click.option("-m", "--model", default=None)
@click.option("-d", "--dir", "working_dir", default=None)
@click.option("--api-key", default=None, envvar="OCTOSLAVE_API_KEY")
@click.option("--base-url", default=None, envvar="OCTOSLAVE_BASE_URL")
@click.option("--local", is_flag=True, default=False, help="Use local Ollama models")
@click.option("-i", "--interactive", is_flag=True, help="Stay interactive after task")
def run(task, model, working_dir, api_key, base_url, local, interactive):
    """Run a single TASK and exit (or continue interactively with -i).

    \b
    Examples:
      ots run "build a REST API for a todo app"
      ots run "research recent papers on RAG" --model qwen3-coder
      ots run "add unit tests" -i
      ots run "explain this codebase" --local
    """
    cfg = _resolve_config(model, working_dir, api_key, base_url, local=local)
    display.print_header(cfg["model"], cfg["working_dir"], local=cfg["backend"] == "ollama")
    display.print_task(task)

    client = make_client(cfg["api_key"], cfg["base_url"])
    messages = run_agent(task, cfg["model"], cfg["working_dir"], client)

    if interactive:
        _repl_loop(client, cfg, messages)


# ---------------------------------------------------------------------------
# `config` sub-command
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--api-key", default=None)
@click.option("--model", default=None)
@click.option("--base-url", default=None)
@click.option("--ollama-url", default=None, help="Ollama base URL (default: http://localhost:11434/v1)")
@click.option("--show", is_flag=True, help="Show current config")
def config(api_key, model, base_url, ollama_url, show):
    """Configure API key, default model, base URL, and Ollama settings."""
    current = load_config()

    if show:
        key = current.get("api_key", "")
        masked = (key[:8] + "…" + key[-4:]) if len(key) > 12 else ("set" if key else "not set")
        backend = current.get("backend", "einfra")
        display.console.print(f"[bold]backend[/bold]      : {backend}")
        display.console.print(f"[bold]api_key[/bold]      : {masked}")
        display.console.print(f"[bold]base_url[/bold]     : {current.get('base_url')}")
        display.console.print(f"[bold]default_model[/bold]: {current.get('default_model')}")
        display.console.print(f"[bold]ollama_url[/bold]   : {current.get('ollama_url', OLLAMA_BASE_URL)}")
        if backend == "ollama":
            running = ollama_is_running(current.get("ollama_url", OLLAMA_BASE_URL))
            pulled = ollama_list_models(current.get("ollama_url", OLLAMA_BASE_URL))
            status = "[bold green]running[/bold green]" if running else "[bold red]not running[/bold red]"
            display.console.print(f"[bold]ollama status[/bold]: {status}")
            if pulled:
                display.console.print("[bold]pulled models[/bold]:")
                for m in pulled:
                    display.console.print(f"  {m}")
        return

    new_key = api_key or current.get("api_key", "")
    new_url = base_url or current.get("base_url", BASE_URL)
    new_model = model or current.get("default_model", DEFAULT_MODEL)
    new_ollama = ollama_url or current.get("ollama_url", OLLAMA_BASE_URL)
    new_backend = current.get("backend", "einfra")

    if not any([api_key, model, base_url, ollama_url]):
        display.console.print("[bold]OctoSlave — setup[/bold]\n")
        display.console.print(
            "  [bold]einfra[/bold]  — e-INFRA CZ cloud API  "
            "(requires an API key; best model quality; recommended)\n"
            "  [bold]ollama[/bold]  — local models via Ollama "
            "(no API key; fully private; GPU strongly recommended)\n"
        )
        new_backend = click.prompt(
            "Backend",
            default=new_backend,
            type=click.Choice(["einfra", "ollama"]),
        )

        if new_backend == "einfra":
            display.console.print(
                "\n  Get an API key at [link=https://llm.ai.e-infra.cz]llm.ai.e-infra.cz[/link] "
                "(free for Czech academic institutions).\n"
            )
            new_key = click.prompt(
                "API key (e-INFRA CZ)",
                default=new_key,
                hide_input=True,
                show_default=False,
            )
            new_url = click.prompt("Base URL (leave default unless self-hosting)", default=new_url)
            display.console.print(
                "\n  Suggested models:\n"
                "    [bold]deepseek-v3.2[/bold]          — best all-round default (reasoning + coding)\n"
                "    [bold]deepseek-v3.2-thinking[/bold] — extended chain-of-thought; slower\n"
                "    [bold]qwen3-coder-30b[/bold]        — strongest at code generation\n"
                "    [bold]qwen3.5-122b[/bold]           — fast reader; good for research\n"
                "    [bold]gpt-oss-120b[/bold]           — large context; clean writing\n"
                "  Run [bold]ots models[/bold] to see the full list.\n"
            )
            new_model = click.prompt("Default model", default=new_model)
        else:
            new_ollama = click.prompt("Ollama URL", default=new_ollama)
            running = ollama_is_running(new_ollama)
            if not running:
                display.console.print(
                    "[yellow]  Ollama is not running — start it with: ollama serve[/yellow]\n"
                    "  Pull a model later with: ollama pull llama3.1:8b\n"
                )
                new_model = click.prompt("Default model (set now or update after pulling)", default=new_model)
            else:
                pulled = ollama_list_models(new_ollama)
                if pulled:
                    display.console.print(
                        "\n  Pulled models: " + ", ".join(pulled) + "\n"
                        "  Tip: pull a strong reasoning model for Tier A (orchestrator/evaluator)\n"
                        "       and a coder model for Tier B (coder/debugger).\n"
                    )
                    new_model = click.prompt("Default model", default=pulled[0], type=click.Choice(pulled))
                else:
                    display.console.print(
                        "\n  No models pulled yet. Recommended first pull:\n"
                        "    ollama pull llama3.1:8b   (5 GB — good all-round)\n"
                    )
                    new_model = click.prompt("Default model (set after pulling)", default="llama3.1:8b")

    save_config(new_key, new_url, new_model, backend=new_backend, ollama_url=new_ollama)
    display.console.print("[bold green]Config saved.[/bold green]")


# ---------------------------------------------------------------------------
# `models` sub-command
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--local", is_flag=True, default=False, help="List local Ollama models instead")
def models(local):
    """List available models."""
    cfg = load_config()

    if local or cfg.get("backend") == "ollama":
        _print_local_models(cfg.get("ollama_url", OLLAMA_BASE_URL))
        return

    display.console.print("[bold]Available models on e-INFRA CZ:[/bold]\n")
    default = cfg.get("default_model", DEFAULT_MODEL)
    for m in KNOWN_MODELS:
        marker = " [bold green]← default[/bold green]" if m == default else ""
        display.console.print(f"  {m}{marker}")
    display.console.print()
    display.console.print("[dim]Switch with: /model <name>  or  -m <name>[/dim]")
    display.console.print("[dim]Use local Ollama models: /local  or  --local flag[/dim]")


def _print_local_models(ollama_url: str):
    if not ollama_is_running(ollama_url):
        display.print_error(
            "Ollama is not running. Start it with: ollama serve"
        )
        return
    pulled = ollama_list_models(ollama_url)
    if not pulled:
        display.console.print("[dim]No models pulled yet.[/dim]")
        display.console.print("Pull a model with: [cyan]ollama pull mistral[/cyan]")
        return
    display.console.print("[bold]Pulled Ollama models:[/bold]\n")
    for m in pulled:
        display.console.print(f"  [bold bright_green]{m}[/bold bright_green]")
    display.console.print()
    display.console.print("[dim]Switch with: /model <name>[/dim]")
    display.console.print("[dim]Pull more with: /pull <model-name>[/dim]")


# ---------------------------------------------------------------------------
# Interactive TUI
# ---------------------------------------------------------------------------

def _interactive(ctx_obj: dict):
    cfg = _resolve_config(
        ctx_obj.get("model"),
        ctx_obj.get("working_dir"),
        ctx_obj.get("api_key"),
        ctx_obj.get("base_url"),
        local=ctx_obj.get("local", False),
    )

    is_local = cfg["backend"] == "ollama"

    if not is_local and not cfg["api_key"]:
        display.print_error(
            "No API key configured. Run `ots config` or set OCTOSLAVE_API_KEY.\n"
            "For local models: `ots --local` or `/local` in session."
        )
        sys.exit(1)

    display.print_welcome(cfg["model"], cfg["working_dir"], local=is_local)
    client = make_client(cfg["api_key"], cfg["base_url"])
    messages: list[dict] = []

    _repl_loop(client, cfg, messages)


def _repl_loop(client, cfg: dict, messages: list[dict]):
    """The main REPL: read input, handle slash commands, run agent."""
    _HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    session = PromptSession(
        history=FileHistory(str(_HISTORY_FILE)),
        style=_PT_STYLE,
        key_bindings=_make_keybindings(),
    )

    state = {
        "model":       cfg["model"],
        "working_dir": cfg["working_dir"],
        "backend":     cfg["backend"],
        "ollama_url":  cfg.get("ollama_url", OLLAMA_BASE_URL),
        "api_key":     cfg.get("api_key", ""),
        "base_url":    cfg.get("base_url", BASE_URL),
    }

    while True:
        try:
            user_input = session.prompt(
                _make_prompt(state),
                bottom_toolbar=_make_toolbar(state),
            ).strip()
        except KeyboardInterrupt:
            display.console.print("[dim]\n(Ctrl+C — use /exit or Ctrl+D to quit)[/dim]")
            messages = []
            continue
        except EOFError:
            display.console.print("[dim]\nBye.[/dim]")
            break

        if not user_input:
            continue

        if user_input.startswith("/"):
            handled = _handle_slash(user_input, state, cfg, messages, client)
            if handled == "exit":
                break
            if handled == "clear":
                messages = []
            if handled == "new_client":
                # Backend switched — rebuild client and clear history
                client = make_client(state["api_key"], state["base_url"])
                messages = []
            continue

        display.print_task(user_input)
        try:
            if messages:
                messages = continue_agent(messages, user_input, state["model"],
                                          state["working_dir"], client)
            else:
                messages = run_agent(user_input, state["model"],
                                     state["working_dir"], client)
        except KeyboardInterrupt:
            display.console.print("\n[dim]Interrupted.[/dim]")
            messages = []


def _handle_slash(cmd: str, state: dict, cfg: dict, messages: list, client) -> str | None:
    parts = cmd.split(None, 1)
    name = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    if name in ("/exit", "/quit", "/q"):
        display.console.print("[dim]Bye.[/dim]")
        return "exit"

    if name in ("/help", "/?"):
        display.print_help()
        return "ok"

    if name == "/clear":
        display.console.clear()
        display.print_welcome(state["model"], state["working_dir"],
                               local=state["backend"] == "ollama")
        return "clear"

    if name == "/model":
        if not arg:
            if state["backend"] == "ollama":
                _print_local_models(state["ollama_url"])
            else:
                display.console.print("[bold]Available models:[/bold]")
                for m in KNOWN_MODELS:
                    mark = " [green]←[/green]" if m == state["model"] else ""
                    display.console.print(f"  {m}{mark}")
        else:
            state["model"] = arg
            display.console.print(
                f"[dim]Model set to[/dim] [bold magenta]{arg}[/bold magenta]"
            )
            messages.clear()
        return "ok"

    if name == "/local":
        return _handle_local_switch(arg, state, messages)

    if name == "/einfra":
        return _handle_einfra_switch(state, messages)

    if name == "/pull":
        if not arg:
            display.print_error("Usage: /pull <model-name>  e.g. /pull llama3.2")
            return "ok"
        _do_pull(arg, state)
        return "ok"

    if name == "/dir":
        if not arg:
            display.console.print(f"[dim]Working dir:[/dim] {state['working_dir']}")
        else:
            new_dir = str(Path(arg).expanduser().resolve())
            if not Path(new_dir).is_dir():
                display.print_error(f"Not a directory: {arg}")
            else:
                state["working_dir"] = new_dir
                display.console.print(f"[dim]Dir set to[/dim] {new_dir}")
                messages.clear()
        return "ok"

    if name == "/compact":
        if not messages:
            display.print_info("No conversation to compact.")
            return "ok"
        summary_task = (
            "Summarise this conversation so far into a compact context block that preserves "
            "all key findings, code written, hypotheses, and decisions. Keep it under 400 words."
        )
        try:
            new_msgs = continue_agent(messages, summary_task, state["model"],
                                       state["working_dir"], client)
            # Keep: system prompt (index 0) + the assistant's summary reply (last
            # assistant message). This guarantees the system prompt is always present.
            system_msg = next((m for m in new_msgs if m.get("role") == "system"), None)
            summary_msg = next(
                (m for m in reversed(new_msgs) if m.get("role") == "assistant"), None
            )
            messages.clear()
            if system_msg:
                messages.append(system_msg)
            if summary_msg:
                messages.append(summary_msg)
            display.print_info("History compacted.")
        except Exception as e:
            display.print_error(str(e))
        return "ok"

    if name == "/long-research":
        _handle_long_research(arg, state, cfg, client)
        return "ok"

    display.print_error(f"Unknown command: {name}  (type /help)")
    return "ok"


def _handle_local_switch(arg: str, state: dict, messages: list) -> str:
    """Switch to local Ollama backend. Optionally pass model name as arg."""
    ollama_url = state.get("ollama_url", OLLAMA_BASE_URL)

    if not ollama_is_running(ollama_url):
        display.print_error(
            "Ollama is not running.\n"
            "Start it with:  [bold]ollama serve[/bold]\n"
            "Then try /local again."
        )
        return "ok"

    pulled = ollama_list_models(ollama_url)
    if not pulled:
        display.print_error(
            "No models are pulled yet.\n"
            "Pull one with:  [bold]/pull mistral[/bold]  or  [bold]ollama pull mistral[/bold]"
        )
        return "ok"

    chosen = arg if arg else pulled[0]
    if chosen not in pulled:
        display.print_error(
            f"Model '{chosen}' is not pulled. Available: {', '.join(pulled)}"
        )
        return "ok"

    state["backend"] = "ollama"
    state["model"] = chosen
    state["api_key"] = "ollama"
    state["base_url"] = ollama_url

    # Persist backend switch
    saved = load_config()
    save_config(
        saved.get("api_key", ""),
        saved.get("base_url", BASE_URL),
        chosen,
        backend="ollama",
        ollama_url=ollama_url,
    )

    display.console.print(
        f"[bold bright_green]● Local mode[/bold bright_green] — using [bold]{chosen}[/bold] via Ollama"
    )
    display.console.print(
        f"[dim]  {len(pulled)} model(s) available: {', '.join(pulled)}[/dim]"
    )
    display.console.print("[dim]  Switch back: /einfra[/dim]")
    messages.clear()
    return "new_client"


def _handle_einfra_switch(state: dict, messages: list) -> str:
    """Switch back to e-INFRA CZ backend."""
    saved = load_config()
    api_key = saved.get("api_key", "")
    if not api_key:
        display.print_error(
            "No e-INFRA CZ API key configured. Run `ots config` first."
        )
        return "ok"

    state["backend"] = "einfra"
    state["model"] = saved.get("default_model", DEFAULT_MODEL)
    state["api_key"] = api_key
    state["base_url"] = saved.get("base_url", BASE_URL)

    save_config(
        api_key,
        state["base_url"],
        state["model"],
        backend="einfra",
        ollama_url=state.get("ollama_url", OLLAMA_BASE_URL),
    )

    display.console.print(
        f"[bold bright_magenta]● e-INFRA CZ mode[/bold bright_magenta] — using [bold]{state['model']}[/bold]"
    )
    messages.clear()
    return "new_client"


def _do_pull(model_name: str, state: dict):
    """Pull a model via Ollama."""
    ollama_url = state.get("ollama_url", OLLAMA_BASE_URL)
    if not ollama_is_running(ollama_url):
        display.print_error("Ollama is not running. Start it with: ollama serve")
        return
    display.console.print(f"[dim]Pulling [bold]{model_name}[/bold] …[/dim]")
    ok = ollama_pull_model(model_name, ollama_url)
    if ok:
        display.console.print(f"[bold green]✓ {model_name} pulled successfully.[/bold green]")
        display.console.print(f"[dim]Use it with: /local {model_name}[/dim]")
    else:
        display.print_error(f"Failed to pull {model_name}.")


def _handle_long_research(arg: str, state: dict, cfg: dict, client):
    """Parse /long-research flags and launch the research pipeline."""
    import shlex

    try:
        tokens = shlex.split(arg)
    except ValueError:
        tokens = arg.split()

    topic_parts: list[str] = []
    max_rounds = 5
    all_model: str | None = None
    overseer_model: str | None = None
    resume = False

    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t == "--rounds" and i + 1 < len(tokens):
            try:
                max_rounds = int(tokens[i + 1])
            except ValueError:
                display.print_error(f"--rounds expects an integer, got: {tokens[i+1]}")
                return
            i += 2
        elif t == "--all" and i + 1 < len(tokens):
            all_model = tokens[i + 1]
            i += 2
        elif t == "--overseer" and i + 1 < len(tokens):
            overseer_model = tokens[i + 1]
            i += 2
        elif t == "--resume":
            resume = True
            i += 1
        else:
            topic_parts.append(t)
            i += 1

    topic = " ".join(topic_parts).strip()
    if not topic:
        display.print_error(
            "Usage: /long-research <topic> [--rounds N] [--all MODEL] "
            "[--overseer MODEL] [--resume]"
        )
        return

    overrides: dict[str, str] = {}

    if state["backend"] == "ollama" and not all_model:
        # Auto-assign local models across roles
        pulled = ollama_list_models(state.get("ollama_url", OLLAMA_BASE_URL))
        if not pulled:
            display.print_error("No Ollama models available for local research.")
            return
        overrides = assign_local_models(pulled)
        display.print_local_research_assignment(overrides)
    else:
        if all_model:
            for role in RESEARCH_ROLES:
                overrides[role] = all_model
        if overseer_model:
            overrides["orchestrator"] = overseer_model

    run_long_research(
        topic=topic,
        working_dir=state["working_dir"],
        client=client,
        max_rounds=max_rounds,
        model_overrides=overrides,
        resume=resume,
    )


# ---------------------------------------------------------------------------
# Prompt-toolkit helpers
# ---------------------------------------------------------------------------

def _make_prompt(state: dict):
    model_short = state["model"][:20]
    is_local = state.get("backend") == "ollama"
    if is_local:
        return HTML(f'<prompt-local>◆</prompt-local> <model-tag>[local:{model_short}]</model-tag> ')
    return HTML(f'<prompt>◆</prompt> <model-tag>[{model_short}]</model-tag> ')


def _make_toolbar(state: dict):
    wd = state["working_dir"]
    if len(wd) > 45:
        wd = "…" + wd[-43:]
    is_local = state.get("backend") == "ollama"
    backend_tag = " [local]" if is_local else ""
    return HTML(
        f'<bottom-toolbar>  dir: {wd}{backend_tag}'
        f'   /help · /model · /local · /einfra · /clear · /exit</bottom-toolbar>'
    )


def _make_keybindings() -> KeyBindings:
    kb = KeyBindings()

    @kb.add("c-l")
    def _clear_screen(event):
        event.app.renderer.clear()

    return kb


# ---------------------------------------------------------------------------
# Config resolution helper
# ---------------------------------------------------------------------------

def _resolve_config(model, working_dir, api_key, base_url, local: bool = False) -> dict:
    saved = load_config()

    # Decide backend
    backend = "ollama" if local else saved.get("backend", "einfra")
    ollama_url = saved.get("ollama_url", OLLAMA_BASE_URL)

    if backend == "ollama":
        # Validate Ollama is reachable
        if not ollama_is_running(ollama_url):
            display.print_error(
                f"Ollama is not running at {ollama_url}.\n"
                "Start it with: [bold]ollama serve[/bold]"
            )
            sys.exit(1)
        pulled = ollama_list_models(ollama_url)
        if not pulled:
            display.print_error(
                "No models pulled in Ollama.\n"
                "Pull one with: [bold]ollama pull mistral[/bold]"
            )
            sys.exit(1)
        chosen_model = model or saved.get("default_model") or pulled[0]
        if chosen_model not in pulled:
            display.console.print(
                f"[dim]Model '{chosen_model}' not found locally, "
                f"using '{pulled[0]}' instead.[/dim]"
            )
            chosen_model = pulled[0]
        return {
            "api_key":     "ollama",
            "base_url":    ollama_url,
            "model":       chosen_model,
            "working_dir": str(Path(working_dir).resolve()) if working_dir else os.getcwd(),
            "backend":     "ollama",
            "ollama_url":  ollama_url,
        }

    # e-INFRA CZ backend
    return {
        "api_key":     api_key or saved.get("api_key", ""),
        "base_url":    base_url or saved.get("base_url", BASE_URL),
        "model":       model or saved.get("default_model", DEFAULT_MODEL),
        "working_dir": str(Path(working_dir).resolve()) if working_dir else os.getcwd(),
        "backend":     "einfra",
        "ollama_url":  ollama_url,
    }


def main():
    cli()


if __name__ == "__main__":
    main()
