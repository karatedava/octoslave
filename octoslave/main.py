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
from prompt_toolkit.completion import Completer, Completion

from . import display
from . import __version__
from .agent import make_client, run_agent, continue_agent, load_session_memory, save_session_memory, memory_file
from .council import (
    resolve_council_roles, run_council_agent, continue_council_agent,
    council_available, print_roles as print_council_roles,
)
from .vault import run_vault_improve
from .config import (
    KNOWN_MODELS, DEFAULT_MODEL, BASE_URL, OLLAMA_BASE_URL,
    NIM_BASE_URL, NIM_DEFAULT_MODEL, NIM_KNOWN_MODELS,
    PIPELINE_ROLES, EINFRA_ROLE_MODELS, NIM_ROLE_MODELS,
    BUILTIN_BACKENDS,
    load_config, save_config,
    ollama_is_running, ollama_list_models, ollama_pull_model,
    nim_list_models, einfra_list_models, list_models,
    assign_local_models, sort_by_tool_calling,
    get_role_models, save_role_model, reset_role_models,
    resolve_backend, list_providers,
    get_custom_providers, get_custom_provider,
    add_custom_provider, update_custom_provider, remove_custom_provider,
    get_mcp_servers, add_mcp_server, remove_mcp_server, set_mcp_server_enabled,
    get_remotes, get_remote, add_remote, remove_remote,
)

# ---------------------------------------------------------------------------
# Prompt-toolkit style
# ---------------------------------------------------------------------------

_PT_STYLE = Style.from_dict(
    {
        "prompt":         "bold #fab283",
        "prompt-local":   "bold #7fd88f",   # green tint in local mode
        "prompt-nim":     "bold #5c9cf5",   # blue tint in nim mode
        "model-tag":      "#5c9cf5",
        "input":          "#d0d1d6",
        "bottom-toolbar": "bg:#16171d #7a7d86",
        "bottom-toolbar-local": "bg:#16171d #7fd88f",
        "bottom-toolbar-nim":   "bg:#16171d #5c9cf5",
    }
)

_HISTORY_FILE = Path.home() / ".octoslave" / "history"


# ---------------------------------------------------------------------------
# Main CLI group
# ---------------------------------------------------------------------------
# __version__ is single-sourced from octoslave/__init__.py (derived from the
# package metadata that pyproject.toml generates). Bump it in pyproject.toml.


@click.group(
    invoke_without_command=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)
@click.version_option(__version__, "-V", "--version", prog_name="octoslave")
@click.option("-m", "--model", default=None, help="Model to use")
@click.option("-d", "--dir", "working_dir", default=None, help="Working directory")
@click.option("--api-key", default=None, envvar="OCTOSLAVE_API_KEY")
@click.option("--base-url", default=None, envvar="OCTOSLAVE_BASE_URL")
@click.option("--local", is_flag=True, default=False, help="Use local Ollama models")
@click.option("-p", "--prompt-profile", default="base", help="Prompt profile to use (default: base, options: base, coder, analyst, cryouncle)")
@click.option("--permission-mode", default=None,
              type=click.Choice(["autonomous", "controlled", "supervised"]),
              help="Permission mode: autonomous (default), controlled (ask before all edits), or supervised (ask before file edits only)")
@click.option("-v", "--verbose", is_flag=True, default=False, help="Verbose mode: show full diffs, complete tool output, and bash commands live")
@click.option("--no-plan", "disable_plan", is_flag=True, default=False, help="Skip the upfront planning step")
@click.option("--verify/--no-verify", default=True,
              help="Self-check completion after each task and automatically fix anything found incomplete (on by default)")
@click.option("--no-memory", "disable_memory", is_flag=True, default=False, help="Do not load or save project memory")
@click.option("--remote", "remote_id", default=None, help="Run tools on a configured remote host over SSH (see `/remote`)")
@click.pass_context
def cli(ctx, model, working_dir, api_key, base_url, local, prompt_profile, permission_mode, verbose, disable_plan, verify, disable_memory, remote_id):
    """OctoSlave — autonomous AI research & coding assistant.

    Run without arguments to enter interactive mode.
    """
    ctx.ensure_object(dict)
    ctx.obj["model"] = model
    ctx.obj["remote_id"] = remote_id
    ctx.obj["working_dir"] = working_dir
    ctx.obj["api_key"] = api_key
    ctx.obj["base_url"] = base_url
    ctx.obj["local"] = local
    ctx.obj["prompt_profile"] = prompt_profile
    ctx.obj["permission_mode"] = permission_mode
    ctx.obj["verbose"] = verbose
    ctx.obj["enable_plan"] = not disable_plan
    ctx.obj["enable_verify"] = verify
    ctx.obj["enable_memory"] = not disable_memory
    if verbose:
        display.set_verbose(True)

    if ctx.invoked_subcommand is None:
        # First-run wizard — only in PyInstaller bundles, not pip installs
        from .wizard import needs_wizard, run_wizard
        if needs_wizard():
            run_wizard()
        _interactive(ctx.obj)


# ---------------------------------------------------------------------------
# `run` sub-command — one-shot task
# ---------------------------------------------------------------------------

@cli.command()
@click.argument("task")
@click.option("-m", "--model", default=None)
@click.option("-d", "--dir", "working_dir", default=None, help="Working directory (default: current directory)")
@click.option("--api-key", default=None, envvar="OCTOSLAVE_API_KEY")
@click.option("--base-url", default=None, envvar="OCTOSLAVE_BASE_URL")
@click.option("--local", is_flag=True, default=False, help="Use local Ollama models")
@click.option("-p", "--prompt-profile", default="base", help="Prompt profile to use (default: base, options: base, coder, analyst, cryouncle)")
@click.option("-i", "--interactive", is_flag=True, help="Stay interactive after task")
@click.option("--permission-mode", default=None,
              type=click.Choice(["autonomous", "controlled", "supervised"]),
              help="Permission mode: autonomous (default), controlled (ask before all edits), or supervised (ask before file edits only)")
@click.option("-v", "--verbose", is_flag=True, default=False, help="Verbose mode: show full diffs, complete tool output, and bash commands live")
@click.option("-n", "--new-project", is_flag=True, default=False, help="Create a new project dir in ~/octoslave/projects/ for output")
@click.option("--no-plan", "disable_plan", is_flag=True, default=False, help="Skip the upfront planning step")
@click.option("--verify/--no-verify", default=True,
              help="Self-check completion after the task and automatically fix anything found incomplete (on by default)")
@click.option("--no-memory", "disable_memory", is_flag=True, default=False, help="Do not load or save project memory")
@click.option("--parallel", "parallel_n", type=int, default=1,
              help="Run N agents on the same task in parallel and pick/merge a winner (default: 1)")
@click.option("--strategy", default="best",
              type=click.Choice(["best", "vote", "merge"]),
              help="How to combine parallel agents: best (judge picks), vote (peers grade), merge (synthesise)")
@click.option("--parallel-models", "parallel_models", default=None,
              help="Comma-separated models, one per parallel candidate (e.g. 'qwen3-coder-30b,deepseek-v3.2,kimi-k2.6'). "
                   "Shorter than --parallel? remaining slots reuse the default --model.")
@click.option("--parallel-profiles", "parallel_profiles", default=None,
              help="Comma-separated prompt profiles, one per parallel candidate (e.g. 'coder,analyst'). "
                   "Shorter than --parallel? rotates through the list.")
@click.option("--judge-model", "judge_model", default=None,
              help="Model used for the judge / vote-tally / merge step (defaults to --model).")
@click.option("--remote", "remote_id", default=None,
              help="Run tools on a configured remote host over SSH (see `/remote`).")
def run(task, model, working_dir, api_key, base_url, local, prompt_profile, interactive, permission_mode, verbose, new_project, disable_plan, verify, disable_memory, parallel_n, strategy, parallel_models, parallel_profiles, judge_model, remote_id):
    """Run a single TASK and exit (or continue interactively with -i).

    \b
    Default working directory is where you ran octoslave (current dir).
    Use -d to specify a directory, or -n to auto-create a project folder.

    \b
    Examples:
      ots run "build a REST API for a todo app"
      ots run "research recent papers on RAG" --model qwen3-coder
      ots run "add unit tests" -i
      ots run "explain this codebase" --local
      ots run "build a REST API" -p coder
      ots run "analyze this dataset" -p analyst -d ~/data
      ots run "write a report" -n                # auto-create project dir
      ots run "reorganize notes" -v
      ots run "refactor auth module" --no-plan   # skip planning step
      ots run "fix the bug" --no-verify          # skip the self-check/fix pass after
      ots run "quick task" --no-memory           # don't load/save project memory
    """
    if verbose:
        display.set_verbose(True)
    cfg = _resolve_config(model, working_dir, api_key, base_url, local=local)
    effective_profile = prompt_profile
    if cfg["backend"] == "ollama" and prompt_profile == "base":
        effective_profile = "local"
    cfg["prompt_profile"] = effective_profile

    enable_plan = not disable_plan
    enable_verify = verify
    enable_memory = not disable_memory

    # Resolve a --remote target: tools run on the remote host, starting in its dir.
    remote = None
    if remote_id:
        remote = get_remote(None, remote_id)
        if not remote:
            display.print_error(f"No remote with id '{remote_id}'. See `ots` then /remote list.")
            sys.exit(1)
        cfg["remote"] = remote
        # A remote path must NOT be resolved against the local filesystem
        # (_resolve_config already did that); use the raw path, or the remote home.
        if working_dir:
            cfg["working_dir"] = working_dir
        else:
            from .remote import resolve_start_dir
            cfg["working_dir"] = resolve_start_dir(remote)

    # Only create project dir if explicitly requested with -n
    if new_project and not working_dir:
        cfg["working_dir"] = _make_project_dir(task)
        display.console.print(f"[dim]📁 project dir:[/dim] [bold]{cfg['working_dir']}[/bold]")

    # Override permission mode if specified
    if permission_mode:
        cfg["permission_mode"] = permission_mode
    else:
        saved_cfg = load_config()
        cfg["permission_mode"] = saved_cfg.get("permission_mode", "autonomous")

    display.print_header(cfg["model"], cfg["working_dir"], backend=cfg["backend"])
    if remote:
        display.console.print(
            f"[dim]⇅ remote:[/dim] [bold]{remote.get('name')}[/bold] "
            f"[dim]({remote.get('user','') + '@' if remote.get('user') else ''}{remote.get('host')}:{cfg['working_dir']})[/dim]"
        )

    # Show permission mode in header
    if cfg["permission_mode"] == "autonomous":
        mode_tag = "[bold #7fd88f]autonomous[/bold #7fd88f]"
    elif cfg["permission_mode"] == "controlled":
        mode_tag = "[bold #f5a742]controlled[/bold #f5a742]"
    else:
        mode_tag = "[bold #5c9cf5]supervised[/bold #5c9cf5]"
    display.console.print(f"[dim #7a7d86]permission mode: {mode_tag}[/dim #7a7d86]")
    display.console.print()

    display.print_task(task)

    client = make_client(cfg["api_key"], cfg["base_url"])
    verify_out: list[str] = []

    if parallel_n > 1:
        from .parallel import run_parallel_agents
        if interactive:
            display.print_info(
                "Note: --parallel runs single-shot only; --interactive will start "
                "a fresh session after the parallel run completes."
            )

        # Parse the per-candidate overrides — comma-separated, trimmed.
        models_list = [m.strip() for m in parallel_models.split(",")] if parallel_models else None
        profiles_list = [p.strip() for p in parallel_profiles.split(",")] if parallel_profiles else None
        if models_list:
            display.print_info(
                "  models per candidate: " + ", ".join(
                    f"#{i}={m}" for i, m in enumerate(models_list)
                )
            )
        if profiles_list:
            display.print_info(
                "  profiles per candidate: " + ", ".join(
                    f"#{i}={p}" for i, p in enumerate(profiles_list)
                )
            )

        result = run_parallel_agents(
            task=task,
            model=cfg["model"],
            working_dir=cfg["working_dir"],
            client=client,
            n=parallel_n,
            strategy=strategy,
            permission_mode=cfg["permission_mode"],
            enable_plan=enable_plan,
            enable_memory=enable_memory,
            models=models_list,
            profiles=profiles_list,
            judge_model=judge_model,
        )
        # Build a minimal "messages" list so optional --interactive REPL has
        # context: system prompt is regenerated, but the user/assistant pair
        # records what just happened.
        winner = next(
            (c for c in result["candidates"] if c.index == result["winner"]),
            None,
        )
        messages = []
        if winner and winner.messages:
            messages = winner.messages
        # Memory: persist as one outcome rather than one per candidate.
        if enable_memory:
            note = f"parallel({parallel_n}, {strategy}) — {result.get('reason', '')[:160]}"
            save_session_memory(cfg["working_dir"], task, status="completed", note=note, remote=remote)
        if interactive:
            _repl_loop(client, cfg, messages)
        return

    messages = run_agent(
        task, cfg["model"], cfg["working_dir"], client,
        prompt_profile, cfg["permission_mode"],
        enable_plan=enable_plan,
        enable_verify=enable_verify,
        enable_memory=enable_memory,
        verify_out=verify_out,
        remote=remote,
    )

    if enable_memory:
        status = "completed"
        note = ""
        if verify_out:
            # verify_out may hold several grades if self-correction ran (e.g.
            # PARTIAL then DONE after a fix pass) — the LAST one is the final word.
            v = verify_out[-1].upper()
            if v.startswith("DONE"):
                status = "done"
            elif v.startswith("PARTIAL"):
                status = "partial"
            elif v.startswith("FAILED"):
                status = "failed"
            note = verify_out[-1][:200]
        save_session_memory(cfg["working_dir"], task, status=status, note=note, remote=remote)

    if interactive:
        _repl_loop(client, cfg, messages)


# ---------------------------------------------------------------------------
# `improved` — council single agent (Thinker/Worker/Verifier). A click GROUP:
#   ots improved              → interactive TUI
#   ots improved run "task"   → one-shot, non-interactive (mirrors `ots run`)
# ---------------------------------------------------------------------------

def _apply_remote_id(cfg: dict, remote_id: str | None, raw_dir: str | None = None) -> None:
    """Resolve a --remote id onto cfg (sets cfg['remote'] + remote working dir).

    ``raw_dir`` is the un-resolved ``-d`` path — a remote path must not be run
    through the local ``Path.resolve()`` that ``_resolve_config`` applies.
    """
    if not remote_id:
        return
    remote = get_remote(None, remote_id)
    if not remote:
        display.print_error(f"No remote with id '{remote_id}'. See `ots` then /remote list.")
        return
    from .remote import resolve_start_dir
    cfg["remote"] = remote
    if raw_dir:
        cfg["working_dir"] = raw_dir
    else:
        cfg["working_dir"] = resolve_start_dir(remote)


def _council_overrides(worker, thinker, verifier) -> dict:
    """Merge CLI flags with OCTOSLAVE_COUNCIL_* env into a role-override dict."""
    return {
        "worker":   worker   or os.environ.get("OCTOSLAVE_COUNCIL_WORKER"),
        "thinker":  thinker  or os.environ.get("OCTOSLAVE_COUNCIL_THINKER"),
        "verifier": verifier or os.environ.get("OCTOSLAVE_COUNCIL_VERIFIER"),
    }


def _improved_setup(model, working_dir, api_key, base_url, prompt_profile,
                    permission_mode, verbose, worker, thinker, verifier,
                    enable_plan: bool = True, enable_memory: bool = True,
                    ultra: bool = False) -> tuple[dict, object]:
    """Build cfg, resolve the council roles, and make a client.

    Shared by the Improved TUI and `improved run` so both resolve the council
    identically. On the Ollama backend (no cloud pool) council is disabled and
    ``cfg['council']`` is False — the caller falls back to the normal agent.
    ``ultra`` enables the deeper best-of-N debate orchestration. Returns
    ``(cfg, client)``.
    """
    if verbose:
        display.set_verbose(True)
    cfg = _resolve_config(model, working_dir, api_key, base_url, local=False)
    cfg["prompt_profile"] = prompt_profile
    cfg["verbose"] = verbose
    cfg["enable_plan"] = enable_plan
    cfg["enable_memory"] = enable_memory
    cfg["ultra"] = ultra
    if permission_mode:
        cfg["permission_mode"] = permission_mode
    else:
        cfg["permission_mode"] = load_config().get("permission_mode", "autonomous")

    if not council_available(cfg):
        display.print_error(
            "Improved (council) mode needs a cloud model pool (e-INFRA / NIM / custom).\n"
            "The local Ollama backend can't co-resident three large models.\n"
            "Switch with `ots config` (einfra), then `ots improved` again. "
            "Falling back to the normal single agent."
        )
        cfg["council"] = False
    else:
        if not cfg.get("api_key"):
            display.print_error(
                "No API key configured. Run `ots config` or set OCTOSLAVE_API_KEY."
            )
            sys.exit(1)
        client_probe = make_client(cfg["api_key"], cfg["base_url"])
        roles, notes = resolve_council_roles(
            client_probe, load_config(), _council_overrides(worker, thinker, verifier)
        )
        cfg["council"] = True
        cfg["council_roles"] = roles
        cfg["council_notes"] = notes
        # The "active model" in council mode is the Worker (the executor that
        # drives the tool loop) — surface that, not the stale single-model default.
        cfg["model"] = roles["worker"]

    client = make_client(cfg["api_key"], cfg["base_url"])
    return cfg, client


@cli.group("improved", invoke_without_command=True)
@click.option("-m", "--model", default=None, help="(unused in council; see --worker/--thinker/--verifier)")
@click.option("-d", "--dir", "working_dir", default=None, help="Working directory")
@click.option("--api-key", default=None, envvar="OCTOSLAVE_API_KEY")
@click.option("--base-url", default=None, envvar="OCTOSLAVE_BASE_URL")
@click.option("-p", "--prompt-profile", default="base", help="Prompt profile (default: base)")
@click.option("--permission-mode", default=None,
              type=click.Choice(["autonomous", "controlled", "supervised"]))
@click.option("-v", "--verbose", is_flag=True, default=False)
@click.option("--worker", default=None, help="Override the Worker model (executor / tool loop)")
@click.option("--thinker", default=None, help="Override the Thinker model (planner / reasoner)")
@click.option("--verifier", default=None, help="Override the Verifier model (critic / gate)")
@click.option("--ultra", is_flag=True, default=False,
              help="Deeper orchestration: a diverse panel debates & synthesizes the plan (slower, stronger)")
@click.pass_context
def improved(ctx, model, working_dir, api_key, base_url, prompt_profile,
             permission_mode, verbose, worker, thinker, verifier, ultra):
    """IMPROVED (council) mode — a unified single agent.

    One surface, but internally a coordinator routes each step between
    role-specialized e-INFRA models — Thinker / Worker / Verifier — so a diverse
    pool beats any single model. The plain `ots` command is unchanged.

    \b
    With no subcommand, launches the interactive TUI (use /improved on|off to
    toggle mid-session, /ultra on|off for deeper orchestration). One-shot via `run`:

    \b
      ots improved                              # interactive TUI
      ots improved --ultra                      # deeper orchestration
      ots improved run "analyse this dataset"   # one-shot, then exit
    """
    # Stash the group-level options so an `improved <subcommand>` can merge them
    # (subcommand-level flags take precedence over these).
    ctx.ensure_object(dict)
    ctx.obj["improved_opts"] = {
        "model": model, "working_dir": working_dir, "api_key": api_key,
        "base_url": base_url, "prompt_profile": prompt_profile,
        "permission_mode": permission_mode, "verbose": verbose,
        "worker": worker, "thinker": thinker, "verifier": verifier, "ultra": ultra,
    }
    if ctx.invoked_subcommand is not None:
        return  # a subcommand (e.g. `run`) handles it

    # No subcommand → interactive TUI (unchanged behavior).
    cfg, client = _improved_setup(
        model, working_dir, api_key, base_url, prompt_profile,
        permission_mode, verbose, worker, thinker, verifier,
        enable_plan=ctx.obj.get("enable_plan", True),
        enable_memory=ctx.obj.get("enable_memory", True),
        ultra=ultra,
    )
    _apply_remote_id(cfg, ctx.obj.get("remote_id"), raw_dir=working_dir)
    banner_model = "🐙 council" if cfg.get("council") else cfg["model"]
    display.print_welcome(banner_model, cfg["working_dir"], backend=cfg["backend"])
    if cfg.get("remote"):
        display.console.print(f"[dim]⇅ remote:[/dim] [bold]{cfg['remote'].get('name')}[/bold] [dim]({cfg['working_dir']})[/dim]")
    if cfg.get("council"):
        print_council_roles(cfg["council_roles"], cfg.get("council_notes"), ultra=cfg.get("ultra", False))
    display.console.print()
    _repl_loop(client, cfg, [])


@improved.command("run")
@click.argument("task")
@click.option("-d", "--dir", "working_dir", default=None, help="Working directory (default: current directory)")
@click.option("-p", "--prompt-profile", default=None, help="Prompt profile (base, coder, analyst, cryouncle)")
@click.option("-i", "--interactive", is_flag=True, help="Stay interactive after the task")
@click.option("--permission-mode", default=None,
              type=click.Choice(["autonomous", "controlled", "supervised"]))
@click.option("-v", "--verbose", is_flag=True, default=False)
@click.option("-n", "--new-project", is_flag=True, default=False, help="Create a new project dir in ~/octoslave/projects/ for output")
@click.option("--no-plan", "disable_plan", is_flag=True, default=False, help="Skip the upfront Thinker planning step")
@click.option("--no-memory", "disable_memory", is_flag=True, default=False, help="Do not load or save project memory")
@click.option("--worker", default=None, help="Override the Worker model (executor / tool loop)")
@click.option("--thinker", default=None, help="Override the Thinker model (planner / reasoner)")
@click.option("--verifier", default=None, help="Override the Verifier model (critic / gate)")
@click.option("--ultra", is_flag=True, default=False,
              help="Deeper orchestration: a diverse panel debates & synthesizes the plan (slower, stronger)")
@click.option("--api-key", default=None, envvar="OCTOSLAVE_API_KEY")
@click.option("--base-url", default=None, envvar="OCTOSLAVE_BASE_URL")
@click.pass_context
def improved_run(ctx, task, working_dir, prompt_profile, interactive, permission_mode,
                 verbose, new_project, disable_plan, disable_memory,
                 worker, thinker, verifier, ultra, api_key, base_url):
    """Run a single TASK through the council and exit (or continue with -i).

    The Improved counterpart of `ots run` — same one-shot UX, but driven by the
    Thinker / Worker / Verifier council instead of one model.

    \b
    Examples:
      ots improved run "build a REST API for a todo app"
      ots improved run "analyse this dataset" -p analyst -d ~/data
      ots improved run "add unit tests" -i
      ots improved run "explain this repo" --no-plan
    """
    # Merge: subcommand flag wins, else the group-level value (so options work
    # both before and after `run`).
    g = (ctx.obj or {}).get("improved_opts", {})
    prompt_profile = prompt_profile or g.get("prompt_profile") or "base"
    working_dir = working_dir or g.get("working_dir")
    permission_mode = permission_mode or g.get("permission_mode")
    api_key = api_key or g.get("api_key")
    base_url = base_url or g.get("base_url")
    verbose = verbose or g.get("verbose", False)
    worker = worker or g.get("worker")
    thinker = thinker or g.get("thinker")
    verifier = verifier or g.get("verifier")
    ultra = ultra or g.get("ultra", False)
    enable_plan = not disable_plan
    enable_memory = not disable_memory

    cfg, client = _improved_setup(
        g.get("model"), working_dir, api_key, base_url, prompt_profile,
        permission_mode, verbose, worker, thinker, verifier,
        enable_plan=enable_plan, enable_memory=enable_memory, ultra=ultra,
    )

    if working_dir:
        cfg["explicit_dir"] = True
    _apply_remote_id(cfg, (ctx.obj or {}).get("remote_id"), raw_dir=working_dir)

    if new_project and not working_dir:
        cfg["working_dir"] = _make_project_dir(task)
        display.console.print(f"[dim]📁 project dir:[/dim] [bold]{cfg['working_dir']}[/bold]")

    banner_model = "🐙 council" if cfg.get("council") else cfg["model"]
    display.print_header(banner_model, cfg["working_dir"], backend=cfg["backend"])
    if cfg.get("remote"):
        display.console.print(f"[dim]⇅ remote:[/dim] [bold]{cfg['remote'].get('name')}[/bold] [dim]({cfg['working_dir']})[/dim]")
    if cfg["permission_mode"] == "autonomous":
        mode_tag = "[bold #7fd88f]autonomous[/bold #7fd88f]"
    elif cfg["permission_mode"] == "controlled":
        mode_tag = "[bold #f5a742]controlled[/bold #f5a742]"
    else:
        mode_tag = "[bold #5c9cf5]supervised[/bold #5c9cf5]"
    display.console.print(f"[dim #7a7d86]permission mode: {mode_tag}[/dim #7a7d86]")
    if cfg.get("council"):
        print_council_roles(cfg["council_roles"], cfg.get("council_notes"), ultra=cfg.get("ultra", False))
    display.console.print()
    display.print_task(task)

    if cfg.get("council"):
        messages = run_council_agent(
            task, cfg["working_dir"], client, cfg["council_roles"],
            prompt_profile=cfg["prompt_profile"],
            permission_mode=cfg["permission_mode"],
            enable_plan=enable_plan,
            enable_memory=enable_memory,
            ultra=cfg.get("ultra", False),
            remote=cfg.get("remote"),
        )
    else:
        # Ollama fallback — council unavailable, drive the normal single agent.
        messages = run_agent(
            task, cfg["model"], cfg["working_dir"], client,
            cfg["prompt_profile"], cfg["permission_mode"],
            enable_plan=enable_plan, enable_verify=False,
            enable_memory=enable_memory,
            remote=cfg.get("remote"),
        )

    if enable_memory:
        save_session_memory(cfg["working_dir"], task, status="completed", note="", remote=cfg.get("remote"))

    if interactive:
        _repl_loop(client, cfg, messages)


# ---------------------------------------------------------------------------
# `config` sub-command
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--api-key", default=None)
@click.option("--nim-api-key", default=None, help="NVIDIA NIM API key")
@click.option("--model", default=None)
@click.option("--base-url", default=None)
@click.option("--ollama-url", default=None, help="Ollama base URL (default: http://localhost:11434/v1)")
@click.option("--nim-url", default=None, help="NVIDIA NIM base URL (default: https://integrate.api.nvidia.com/v1)")
@click.option("--permission-mode", default=None,
              type=click.Choice(["autonomous", "controlled", "supervised"]),
              help="Permission mode: autonomous (default), controlled (ask before all edits), or supervised (ask before file edits only)")
@click.option("--show", is_flag=True, help="Show current config")
def config(api_key, nim_api_key, model, base_url, ollama_url, nim_url, permission_mode, show):
    """Configure API key, default model, base URL, Ollama/NIM settings, and permission mode."""
    current = load_config()

    if show:
        key = current.get("api_key", "")
        masked = (key[:8] + "…" + key[-4:]) if len(key) > 12 else ("set" if key else "not set")
        nim_key = current.get("nim_api_key", "")
        nim_masked = (nim_key[:8] + "…" + nim_key[-4:]) if len(nim_key) > 12 else ("set" if nim_key else "not set")
        backend = current.get("backend", "einfra")
        perm_mode = current.get("permission_mode", "autonomous")
        display.console.print(f"[bold]backend[/bold]        : {backend}")
        display.console.print(f"[bold]api_key[/bold]        : {masked}")
        display.console.print(f"[bold]base_url[/bold]       : {current.get('base_url')}")
        display.console.print(f"[bold]default_model[/bold]  : {current.get('default_model')}")
        display.console.print(f"[bold]ollama_url[/bold]     : {current.get('ollama_url', OLLAMA_BASE_URL)}")
        display.console.print(f"[bold]nim_api_key[/bold]    : {nim_masked}")
        display.console.print(f"[bold]nim_url[/bold]        : {current.get('nim_url', NIM_BASE_URL)}")
        display.console.print(f"[bold]permission_mode[/bold]: {perm_mode}")
        if backend == "ollama":
            running = ollama_is_running(current.get("ollama_url", OLLAMA_BASE_URL))
            pulled = ollama_list_models(current.get("ollama_url", OLLAMA_BASE_URL))
            status = "[bold green]running[/bold green]" if running else "[bold red]not running[/bold red]"
            display.console.print(f"[bold]ollama status[/bold] : {status}")
            if pulled:
                display.console.print("[bold]pulled models[/bold] :")
                for m in pulled:
                    display.console.print(f"  {m}")
        return

    new_key = api_key or current.get("api_key", "")
    new_nim_key = nim_api_key or current.get("nim_api_key", "")
    new_url = base_url or current.get("base_url", BASE_URL)
    new_model = model or current.get("default_model", DEFAULT_MODEL)
    new_ollama = ollama_url or current.get("ollama_url", OLLAMA_BASE_URL)
    new_nim_url = nim_url or current.get("nim_url", NIM_BASE_URL)
    new_backend = current.get("backend", "einfra")
    new_perm_mode = permission_mode or current.get("permission_mode", "autonomous")

    if not any([api_key, nim_api_key, model, base_url, ollama_url, nim_url, permission_mode]):
        display.console.print("[bold]OctoSlave — setup[/bold]\n")
        display.console.print(
            "  [bold]einfra[/bold]  — e-INFRA CZ cloud API  "
            "(requires an API key; best model quality; recommended)\n"
            "  [bold]ollama[/bold]  — local models via Ollama "
            "(no API key; fully private; GPU strongly recommended)\n"
            "  [bold]nim[/bold]     — NVIDIA NIM cloud API    "
            "(requires an API key; access to NVIDIA-optimised models)\n"
        )
        new_backend = click.prompt(
            "Backend",
            default=new_backend,
            type=click.Choice(["einfra", "ollama", "nim"]),
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
        elif new_backend == "nim":
            display.console.print(
                "\n  Get an API key at [link=https://build.nvidia.com]build.nvidia.com[/link].\n"
            )
            new_nim_key = click.prompt(
                "API key (NVIDIA NIM)",
                default=new_nim_key,
                hide_input=True,
                show_default=False,
            )
            new_nim_url = click.prompt("NIM Base URL (leave default unless self-hosting)", default=new_nim_url)
            display.console.print(
                "\n  Suggested models:\n"
                "    [bold]meta/llama-3.3-70b-instruct[/bold]         — fast, strong all-round default\n"
                "    [bold]meta/llama-3.1-405b-instruct[/bold]        — largest Llama; best quality\n"
                "    [bold]nvidia/llama-3.1-nemotron-70b-instruct[/bold] — NVIDIA-tuned reasoning\n"
                "    [bold]deepseek-ai/deepseek-r1[/bold]             — extended chain-of-thought\n"
                "  Run [bold]ots models[/bold] to see the full list.\n"
            )
            new_model = click.prompt("Default model", default=current.get("default_model", NIM_DEFAULT_MODEL))
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

        # Ask about permission mode if not explicitly set
        display.console.print(
            "\n  [bold]Permission mode:[/bold]\n"
            "  [bold]autonomous[/bold]  — work without asking (default)\n"
            "  [bold]controlled[/bold]  — ask before file edits or commands\n"
            "  [bold]supervised[/bold]  — ask before file edits, auto-allow commands\n"
        )
        new_perm_mode = click.prompt(
            "Permission mode",
            default=new_perm_mode,
            type=click.Choice(["autonomous", "controlled", "supervised"]),
        )

    save_config(
        new_key, new_url, new_model,
        backend=new_backend,
        ollama_url=new_ollama,
        permission_mode=new_perm_mode,
        nim_api_key=new_nim_key,
        nim_url=new_nim_url,
    )
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

    if cfg.get("backend") == "nim":
        nim_models = nim_list_models(cfg.get("nim_url", NIM_BASE_URL), cfg.get("nim_api_key", ""))
        if nim_models:
            display.console.print("[bold]Available models on NVIDIA NIM[/bold] [dim](live from API)[/dim]\n")
        else:
            nim_models = list(NIM_KNOWN_MODELS)
            display.console.print("[bold]Available models on NVIDIA NIM[/bold] [dim](static fallback)[/dim]\n")
        default = cfg.get("default_model", NIM_DEFAULT_MODEL)
        for m in nim_models:
            marker = " [bold green]← default[/bold green]" if m == default else ""
            display.console.print(f"  {m}{marker}")
        display.console.print()
        display.console.print("[dim]Switch with: /model <name>  or  -m <name>[/dim]")
        display.console.print("[dim]Switch backend: /einfra · /local · /nim[/dim]")
        return

    # Custom user-defined provider — query its /v1/models or fall back to its
    # configured `models` list.
    backend = cfg.get("backend", "einfra")
    if backend not in BUILTIN_BACKENDS:
        provider = get_custom_provider(cfg, backend)
        if not provider:
            display.print_error(
                f"Configured backend '{backend}' is not registered. "
                "Run 'ots provider list' to see available providers."
            )
            return
        live = einfra_list_models(provider.get("base_url", ""), provider.get("api_key", "") or "x")
        if live:
            display.console.print(
                f"[bold]Available models on {provider.get('name', backend)}[/bold] [dim](live from API)[/dim]\n"
            )
            ms = live
        else:
            ms = provider.get("models") or [provider.get("default_model")] if provider.get("default_model") else []
            ms = [m for m in ms if m]
            display.console.print(
                f"[bold]Available models on {provider.get('name', backend)}[/bold] [dim](configured list)[/dim]\n"
            )
        default = cfg.get("default_model") or provider.get("default_model")
        for m in ms:
            marker = " [bold green]← default[/bold green]" if m == default else ""
            display.console.print(f"  {m}{marker}")
        display.console.print()
        display.console.print("[dim]Switch with: /model <name>  or  -m <name>[/dim]")
        display.console.print("[dim]Manage providers: ots provider list[/dim]")
        return

    einfra_models = einfra_list_models(cfg.get("base_url", BASE_URL), cfg.get("api_key", ""))
    if einfra_models:
        display.console.print("[bold]Available models on e-INFRA CZ[/bold] [dim](live from API)[/dim]\n")
    else:
        einfra_models = list(KNOWN_MODELS)
        display.console.print("[bold]Available models on e-INFRA CZ[/bold] [dim](static fallback)[/dim]\n")
    default = cfg.get("default_model", DEFAULT_MODEL)
    for m in einfra_models:
        marker = " [bold green]← default[/bold green]" if m == default else ""
        display.console.print(f"  {m}{marker}")
    display.console.print()
    display.console.print("[dim]Switch with: /model <name>  or  -m <name>[/dim]")
    display.console.print("[dim]Use local Ollama models: /local  or  --local flag[/dim]")
    display.console.print("[dim]Use NVIDIA NIM: /nim[/dim]")


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
# `provider` sub-command — manage custom OpenAI-compatible providers
# ---------------------------------------------------------------------------

@cli.group("provider", invoke_without_command=True)
@click.pass_context
def provider_grp(ctx):
    """Manage custom OpenAI-compatible providers (e.g. OpenAI, Together AI,
    Groq, self-hosted vLLM). Use 'ots provider list' to see registered
    providers, 'ots provider add' to register a new one, or 'ots provider
    use <id>' to switch the active backend."""
    if ctx.invoked_subcommand is None:
        ctx.invoke(provider_list)


@provider_grp.command("list")
def provider_list():
    """List all registered providers (built-in + custom)."""
    cfg = load_config()
    providers = list_providers(cfg)
    active = cfg.get("backend", "einfra")
    display.console.print("[bold]Providers[/bold]")
    for p in providers:
        mark = " [green]← active[/green]" if p["id"] == active else ""
        kind = "[dim](builtin)[/dim]" if p["kind"] == "builtin" else "[dim](custom)[/dim]"
        cfg_status = "" if p.get("configured", True) else " [yellow](needs config)[/yellow]"
        display.console.print(
            f"  [bold]{p['id']:<14}[/bold]  {p['name']:<28}  {kind}{cfg_status}{mark}"
        )
    display.console.print()
    display.console.print("[dim]Add:    ots provider add[/dim]")
    display.console.print("[dim]Use:    ots provider use <id>[/dim]")
    display.console.print("[dim]Remove: ots provider remove <id>[/dim]")


@provider_grp.command("add")
@click.option("--id", "pid", default=None, help="Provider id (lowercase slug)")
@click.option("--name", default=None, help="Display name")
@click.option("--base-url", default=None, help="OpenAI-compatible base URL (ending with /v1)")
@click.option("--api-key", default=None, help="API key (omit for unauthenticated endpoints)")
@click.option("--default-model", default=None, help="Default model id")
@click.option("--models", default=None,
              help="Comma-separated list of known model ids (optional)")
def provider_add(pid, name, base_url, api_key, default_model, models):
    """Register a new custom OpenAI-compatible provider. Runs interactively
    if any required field is missing."""
    if not pid:
        pid = click.prompt("Provider id (lowercase slug, e.g. 'openai')").strip().lower()
    if not name:
        name = click.prompt("Display name", default=pid).strip() or pid
    if not base_url:
        base_url = click.prompt("Base URL (ends with /v1)").strip().rstrip("/")
    if api_key is None:
        api_key = click.prompt(
            "API key (leave blank if none)",
            default="", hide_input=True, show_default=False,
        ).strip()
    if not default_model:
        default_model = click.prompt("Default model").strip()
    if models is None:
        models = click.prompt(
            "Known models (comma-separated, optional)", default=""
        ).strip()

    try:
        p = add_custom_provider({
            "id": pid,
            "name": name,
            "base_url": base_url,
            "api_key": api_key,
            "default_model": default_model,
            "models": models,
        })
    except ValueError as exc:
        display.print_error(str(exc))
        sys.exit(1)

    display.console.print(
        f"[bold green]✓ Provider '{p['id']}' added.[/bold green]"
    )
    display.console.print(
        f"[dim]  Switch with: ots provider use {p['id']}[/dim]"
    )


@provider_grp.command("remove")
@click.argument("provider_id")
def provider_remove(provider_id):
    """Remove a custom provider by id."""
    if remove_custom_provider(provider_id):
        display.console.print(f"[dim]Provider '{provider_id}' removed.[/dim]")
    else:
        display.print_error(f"No custom provider with id '{provider_id}'.")
        sys.exit(1)


@provider_grp.command("use")
@click.argument("provider_id")
@click.option("-m", "--model", default=None, help="Override default model")
def provider_use(provider_id, model):
    """Set <provider_id> as the active backend."""
    cfg = load_config()
    pid = provider_id.lower()
    if pid in BUILTIN_BACKENDS:
        # Built-in backends each have their own default model. When no
        # explicit -m override is given, fall back to the backend's
        # canonical default rather than carrying over whatever model was
        # active before — preserves NIM/EINFRA behaviour from before
        # the custom-provider system existed.
        if model:
            new_model = model
        elif pid == "nim":
            new_model = NIM_DEFAULT_MODEL
        elif pid == "ollama":
            new_model = cfg.get("default_model", DEFAULT_MODEL)  # stays — Ollama picks at switch time
        else:  # einfra
            new_model = DEFAULT_MODEL
        save_config(
            cfg.get("api_key", ""),
            cfg.get("base_url", BASE_URL),
            new_model,
            backend=pid,
            ollama_url=cfg.get("ollama_url", OLLAMA_BASE_URL),
            nim_api_key=cfg.get("nim_api_key", ""),
            nim_url=cfg.get("nim_url", NIM_BASE_URL),
        )
        display.console.print(f"[bold]✓ Active backend:[/bold] {pid} · model [bold]{new_model}[/bold]")
        return

    provider = get_custom_provider(cfg, pid)
    if not provider:
        display.print_error(f"Unknown provider '{pid}'. Run 'ots provider list' to see available providers.")
        sys.exit(1)
    if not provider.get("base_url"):
        display.print_error(f"Provider '{pid}' has no base_url configured.")
        sys.exit(1)

    chosen = model or provider.get("default_model") or cfg.get("default_model", DEFAULT_MODEL)
    save_config(
        cfg.get("api_key", ""),
        cfg.get("base_url", BASE_URL),
        chosen,
        backend=pid,
        ollama_url=cfg.get("ollama_url", OLLAMA_BASE_URL),
        nim_api_key=cfg.get("nim_api_key", ""),
        nim_url=cfg.get("nim_url", NIM_BASE_URL),
    )
    display.console.print(
        f"[bold green]✓ Active backend:[/bold green] {provider.get('name', pid)} "
        f"[dim]({pid})[/dim] · model [bold]{chosen}[/bold]"
    )


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
    explicit_profile = ctx_obj.get("prompt_profile", "base")
    if cfg["backend"] == "ollama" and explicit_profile == "base":
        explicit_profile = "local"
    cfg["prompt_profile"] = explicit_profile
    cfg["verbose"] = ctx_obj.get("verbose", False)
    cfg["explicit_dir"] = bool(ctx_obj.get("working_dir"))
    cfg["enable_plan"] = ctx_obj.get("enable_plan", True)
    cfg["enable_verify"] = ctx_obj.get("enable_verify", True)
    cfg["enable_memory"] = ctx_obj.get("enable_memory", True)

    # Resolve a --remote target: execute tools on the remote host and start in
    # its configured directory.
    if ctx_obj.get("remote_id"):
        remote = get_remote(None, ctx_obj["remote_id"])
        if remote:
            from .remote import resolve_start_dir
            cfg["remote"] = remote
            if cfg.get("explicit_dir"):
                # Raw remote path — don't use the locally-resolved value.
                cfg["working_dir"] = ctx_obj.get("working_dir")
            else:
                cfg["working_dir"] = resolve_start_dir(remote)
        else:
            display.print_error(f"No remote with id '{ctx_obj['remote_id']}'. See `ots` then /remote list.")

    # Handle permission mode from CLI or config
    if ctx_obj.get("permission_mode"):
        cfg["permission_mode"] = ctx_obj["permission_mode"]
    else:
        saved_cfg = load_config()
        cfg["permission_mode"] = saved_cfg.get("permission_mode", "autonomous")

    is_local = cfg["backend"] == "ollama"

    if not is_local and not cfg["api_key"]:
        display.print_error(
            "No API key configured. Run `ots config` or set OCTOSLAVE_API_KEY.\n"
            "For local models: `ots --local` or `/local` in session.\n"
            "For NVIDIA NIM: run `ots config` and choose the nim backend."
        )
        sys.exit(1)

    display.print_welcome(cfg["model"], cfg["working_dir"], backend=cfg["backend"])
    if cfg.get("remote"):
        _r = cfg["remote"]
        display.console.print(
            f"[dim]⇅ remote:[/dim] [bold]{_r.get('name')}[/bold] "
            f"[dim]({_r.get('user','') + '@' if _r.get('user') else ''}{_r.get('host')}:{cfg['working_dir']})[/dim]"
        )

    # Show permission mode
    if cfg["permission_mode"] == "autonomous":
        mode_tag = "[bold #7fd88f]autonomous[/bold #7fd88f]"
    elif cfg["permission_mode"] == "controlled":
        mode_tag = "[bold #f5a742]controlled[/bold #f5a742]"
    else:
        mode_tag = "[bold #5c9cf5]supervised[/bold #5c9cf5]"
    display.console.print(f"[dim #7a7d86]permission mode: {mode_tag}[/dim #7a7d86]")
    display.console.print()
    
    client = make_client(cfg["api_key"], cfg["base_url"])
    messages: list[dict] = []

    _repl_loop(client, cfg, messages)


def _repl_loop(client, cfg: dict, messages: list[dict]):
    """The main REPL: read input, handle slash commands, run agent."""
    _HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    state = {
        "model":        cfg["model"],
        "working_dir":  cfg["working_dir"],
        "backend":      cfg["backend"],
        "ollama_url":   cfg.get("ollama_url", OLLAMA_BASE_URL),
        "api_key":      cfg.get("api_key", ""),
        "base_url":     cfg.get("base_url", BASE_URL),
        "nim_api_key":  cfg.get("nim_api_key", ""),
        "nim_url":      cfg.get("nim_url", NIM_BASE_URL),
        "prompt_profile":  cfg.get("prompt_profile", "base"),
        "permission_mode": cfg.get("permission_mode", "autonomous"),
        "verbose": cfg.get("verbose", False),
        "enable_plan":   cfg.get("enable_plan", True),
        "enable_verify": cfg.get("enable_verify", True),
        "enable_memory": cfg.get("enable_memory", True),
        "current_plan":  "",   # last generated plan (shown by /show-plan)
        "council":       cfg.get("council", False),         # improved (council) mode on?
        "council_roles": cfg.get("council_roles") or {},    # {worker,thinker,verifier}
        "ultra":         cfg.get("ultra", False),            # deeper best-of-N orchestration?
        "remote":        cfg.get("remote") or None,          # remote SSH target dict, or None=local
    }
    if state["verbose"]:
        display.set_verbose(True)

    # Connect any user-configured MCP servers up front so their tools are ready
    # for the first task and /mcp shows live status.
    try:
        from .tools import init_mcp
        init_mcp()  # reads ~/.octoslave/config.json mcp_servers
    except Exception:
        pass

    session = PromptSession(
        history=FileHistory(str(_HISTORY_FILE)),
        style=_PT_STYLE,
        key_bindings=_make_keybindings(state),
        completer=_AtFileCompleter(state),
        complete_while_typing=True,
    )

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
                if state.get("council") and state.get("council_roles"):
                    messages = continue_council_agent(
                        messages, user_input, client, state["council_roles"],
                        state["working_dir"], state["permission_mode"],
                        ultra=state.get("ultra", False),
                        remote=state.get("remote"),
                    )
                else:
                    messages = continue_agent(
                        messages, user_input, state["model"],
                        state["working_dir"], client,
                        state["permission_mode"],
                        remote=state.get("remote"),
                    )
            else:
                plan_out: list[str] = []
                verify_out: list[str] = []
                if state.get("council") and state.get("council_roles"):
                    messages = run_council_agent(
                        user_input, state["working_dir"], client,
                        state["council_roles"],
                        prompt_profile=state["prompt_profile"],
                        permission_mode=state["permission_mode"],
                        enable_plan=state["enable_plan"],
                        enable_memory=state["enable_memory"],
                        plan_out=plan_out,
                        ultra=state.get("ultra", False),
                        remote=state.get("remote"),
                    )
                else:
                    messages = run_agent(
                        user_input, state["model"],
                        state["working_dir"], client,
                        state["prompt_profile"],
                        state["permission_mode"],
                        enable_plan=state["enable_plan"],
                        enable_verify=state["enable_verify"],
                        enable_memory=state["enable_memory"],
                        plan_out=plan_out,
                        verify_out=verify_out,
                        remote=state.get("remote"),
                    )
                if plan_out:
                    state["current_plan"] = plan_out[0]
                if state["enable_memory"]:
                    _status = "completed"
                    _note = ""
                    if verify_out:
                        # Last grade wins — self-correction may have run several
                        # (e.g. PARTIAL then DONE after a fix pass).
                        v = verify_out[-1].upper()
                        if v.startswith("DONE"):
                            _status = "done"
                        elif v.startswith("PARTIAL"):
                            _status = "partial"
                        elif v.startswith("FAILED"):
                            _status = "failed"
                        _note = verify_out[-1][:200]
                    save_session_memory(state["working_dir"], user_input, status=_status, note=_note, remote=state.get("remote"))
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
                               backend=state["backend"])
        return "clear"

    if name == "/verbose":
        new_state = not display.is_verbose()
        display.set_verbose(new_state)
        state["verbose"] = new_state
        status = "[bold green]ON[/bold green]" if new_state else "[bold red]OFF[/bold red]"
        display.console.print(f"[dim]Verbose mode:[/dim] {status}")
        return "ok"

    if name == "/model":
        if not arg:
            if state["backend"] == "ollama":
                _print_local_models(state["ollama_url"])
            elif state["backend"] == "nim":
                nim_models = nim_list_models(state.get("nim_url", NIM_BASE_URL), state.get("nim_api_key", ""))
                source = "[dim](live)[/dim]" if nim_models else "[dim](fallback)[/dim]"
                if not nim_models:
                    nim_models = list(NIM_KNOWN_MODELS)
                display.console.print(f"[bold]Available models on NVIDIA NIM:[/bold] {source}")
                for m in nim_models:
                    mark = " [green]←[/green]" if m == state["model"] else ""
                    display.console.print(f"  {m}{mark}")
            else:
                live = einfra_list_models(state.get("base_url", BASE_URL), state.get("api_key", ""))
                source = "[dim](live)[/dim]" if live else "[dim](fallback)[/dim]"
                models_to_show = live if live else list(KNOWN_MODELS)
                display.console.print(f"[bold]Available models on e-INFRA CZ:[/bold] {source}")
                for m in models_to_show:
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

    if name == "/nim":
        return _handle_nim_switch(arg, state, messages)

    if name == "/provider":
        return _handle_provider_command(arg, state, messages)

    if name == "/mcp":
        return _handle_mcp_command(arg, state, messages)

    if name == "/pull":
        if not arg:
            display.print_error("Usage: /pull <model-name>  e.g. /pull llama3.2")
            return "ok"
        _do_pull(arg, state)
        return "ok"

    if name == "/dir":
        if not arg:
            where = "remote dir" if state.get("remote") else "Working dir"
            display.console.print(f"[dim]{where}:[/dim] {state['working_dir']}")
        elif state.get("remote"):
            # Remote mode: the path lives on the remote host — no local validation.
            import posixpath as _pp
            new_dir = arg if _pp.isabs(arg) else _pp.normpath(_pp.join(state["working_dir"], arg))
            state["working_dir"] = new_dir
            display.console.print(f"[dim]Remote dir set to[/dim] {new_dir}")
            messages.clear()
        else:
            new_dir = str(Path(arg).expanduser().resolve())
            if not Path(new_dir).is_dir():
                display.print_error(f"Not a directory: {arg}")
            else:
                state["working_dir"] = new_dir
                display.console.print(f"[dim]Dir set to[/dim] {new_dir}")
                messages.clear()
        return "ok"

    if name == "/remote":
        return _handle_remote_command(arg, state, messages)

    if name == "/new-project":
        task_hint = arg if arg else "project"
        new_dir = _make_project_dir(task_hint)
        state["working_dir"] = new_dir
        display.console.print(f"[dim]📁 project dir:[/dim] [bold]{new_dir}[/bold]")
        messages.clear()
        return "ok"

    if name == "/profile":
        from .agent import load_system_prompt, list_prompt_profiles
        available = list_prompt_profiles() or ["base", "coder", "analyst", "cryouncle", "local"]
        if not arg:
            current = state.get("prompt_profile", "base")
            display.console.print(f"[dim]Current profile:[/dim] [bold #fab283]{current}[/bold #fab283]")
            display.console.print(f"[dim]Available profiles:[/dim] {', '.join(available)}")
            display.console.print("[dim]Usage: /profile <name>  e.g. /profile coder[/dim]")
        else:
            # Validate profile exists
            try:
                test_prompt = load_system_prompt(arg, state["working_dir"])
                state["prompt_profile"] = arg
                display.console.print(
                    f"[dim]Prompt profile set to[/dim] [bold #9d7cd8]{arg}[/bold #9d7cd8]"
                )
                display.console.print(
                    "[dim]Note: Profile will be used for the next task (new conversation).[/dim]"
                )
                messages.clear()
            except FileNotFoundError as e:
                display.print_error(str(e))
        return "ok"

    if name == "/permission":
        if not arg:
            current = state.get("permission_mode", "autonomous")
            available = ["autonomous", "controlled", "supervised"]
            display.console.print(f"[dim]Current permission mode:[/dim] [bold]{current}[/bold]")
            display.console.print(f"[dim]Available modes:[/dim] {', '.join(available)}")
            display.console.print(
                "[dim]Usage: /permission <mode>  e.g. /permission controlled[/dim]\n"
                "[dim]  autonomous — work without asking (default)[/dim]\n"
                "[dim]  controlled — ask before file edits or commands[/dim]\n"
                "[dim]  supervised — ask before file edits, auto-allow commands[/dim]"
            )
        else:
            arg = arg.lower()
            if arg not in ("autonomous", "controlled", "supervised"):
                display.print_error(
                    f"Invalid mode '{arg}'. Use 'autonomous', 'controlled', or 'supervised'."
                )
                return "ok"
            state["permission_mode"] = arg
            if arg == "autonomous":
                mode_tag = "[bold green]autonomous[/bold green]"
            elif arg == "controlled":
                mode_tag = "[bold yellow]controlled[/bold yellow]"
            else:
                mode_tag = "[bold cyan]supervised[/bold cyan]"
            display.console.print(
                f"[dim]Permission mode set to[/dim] {mode_tag}"
            )
            display.console.print(
                "[dim]Note: Mode will apply to the next tool execution.[/dim]"
            )
        return "ok"

    if name == "/show-plan":
        plan = state.get("current_plan", "")
        if plan:
            display.print_plan(plan)
        else:
            display.print_info("No plan for the current session. Plans are generated at task start.")
        return "ok"

    if name == "/memory":
        raw = arg.strip()
        sub = raw.lower()
        _rmt = state.get("remote")
        if sub == "clear":
            # Delete the memory file where the work runs (remote host or local).
            if _rmt:
                from .agent import _remote_memory_path
                from .remote import RemoteSession
                rp = _remote_memory_path(state["working_dir"])
                sess = RemoteSession.get(_rmt)
                if sess.exists(rp):
                    sess.run(f"rm -f {__import__('shlex').quote(rp)}", None, timeout=30)
                    display.print_info(f"Project memory cleared ({rp} on {_rmt.get('name')}).")
                else:
                    display.print_info("No project memory file to clear.")
            else:
                mf = memory_file(state["working_dir"])
                if mf.exists():
                    mf.unlink()
                    display.print_info(f"Project memory cleared ({mf}).")
                else:
                    display.print_info("No project memory file to clear.")
        elif sub.startswith("forget"):
            query = raw[len("forget"):].strip()
            if not query:
                display.print_info("Usage: /memory forget <description of the insight to remove>")
            else:
                from .agent import delete_memory_insight
                removed = delete_memory_insight(state["working_dir"], query, remote=_rmt)
                if removed:
                    display.print_info("Removed from memory:")
                    for r in removed:
                        display.console.print(f"  • {r[:120]}")
                else:
                    display.print_info("No matching insight found; nothing removed.")
        elif sub == "off":
            state["enable_memory"] = False
            display.print_info("Project memory disabled for this session.")
        elif sub == "on":
            state["enable_memory"] = True
            display.print_info("Project memory enabled.")
        else:
            # Show current memory
            mem = load_session_memory(state["working_dir"], remote=_rmt)
            if mem:
                display.console.print()
                display.console.print(mem)
                display.console.print()
            else:
                display.print_info(f"No project memory yet for {state['working_dir']}.")
        return "ok"

    if name == "/plan":
        sub = arg.strip().lower()
        if sub == "off":
            state["enable_plan"] = False
            display.print_info("Planning step disabled.")
        elif sub == "on":
            state["enable_plan"] = True
            display.print_info("Planning step enabled.")
        else:
            status = "[bold green]ON[/bold green]" if state.get("enable_plan", True) else "[bold red]OFF[/bold red]"
            display.console.print(f"[dim]Planning:[/dim] {status}  [dim]Use /plan on|off to toggle[/dim]")
        return "ok"

    if name == "/verify":
        sub = arg.strip().lower()
        if sub == "off":
            state["enable_verify"] = False
            display.print_info("Self-check/fix step disabled.")
        elif sub == "on":
            state["enable_verify"] = True
            display.print_info("Self-check/fix step enabled.")
        else:
            status = "[bold green]ON[/bold green]" if state.get("enable_verify", True) else "[bold red]OFF[/bold red]"
            display.console.print(f"[dim]Verify:[/dim] {status}  [dim]Use /verify on|off to toggle[/dim]")
        return "ok"

    if name == "/improved":
        sub = arg.strip().lower()
        if sub == "off":
            state["council"] = False
            display.print_info("Improved (council) mode OFF — using the normal single agent.")
            messages.clear()
            return "ok"
        if sub in ("on", "status", ""):
            if state.get("backend") == "ollama":
                display.print_error(
                    "Improved (council) mode needs a cloud pool (e-INFRA / NIM / custom). "
                    "Switch backend with /einfra, then /improved on."
                )
                return "ok"
            if sub == "status" or (sub == "" and state.get("council")):
                if state.get("council") and state.get("council_roles"):
                    print_council_roles(state["council_roles"])
                else:
                    display.print_info("Improved (council) mode is OFF. Enable with /improved on.")
                return "ok"
            # turn on (resolve roles fresh against the live catalog)
            roles, notes = resolve_council_roles(client, load_config(), _council_overrides(None, None, None))
            state["council"] = True
            state["council_roles"] = roles
            state["model"] = roles["worker"]  # Worker is the visible executor
            display.print_info("Improved (council) mode ON.")
            print_council_roles(roles, notes, ultra=state.get("ultra", False))
            messages.clear()
            return "ok"
        display.print_error("Usage: /improved on|off|status")
        return "ok"

    if name == "/ultra":
        sub = arg.strip().lower()
        if sub == "off":
            state["ultra"] = False
            display.print_info("Ultra orchestration OFF.")
            return "ok"
        if sub in ("on", "status", ""):
            if sub == "status" or sub == "":
                display.print_info(
                    f"Ultra orchestration is {'ON' if state.get('ultra') else 'OFF'}."
                    + ("" if state.get("council") else "  (needs Improved mode — /improved on)")
                )
                return "ok"
            if not state.get("council"):
                display.print_error("Ultra needs Improved mode. Enable it first with /improved on.")
                return "ok"
            state["ultra"] = True
            display.print_info(
                "Ultra orchestration ON — a diverse panel will debate & synthesize the plan."
            )
            if state.get("council_roles"):
                print_council_roles(state["council_roles"], ultra=True)
            return "ok"
        display.print_error("Usage: /ultra on|off|status")
        return "ok"

    if name == "/undo":
        if not messages:
            display.print_info("Nothing to undo.")
            return "ok"
        # Walk backwards and drop everything up to and including the most recent
        # user turn. This rewinds one user→assistant exchange.
        dropped = 0
        last_user_idx = None
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].get("role") == "user":
                last_user_idx = i
                break
        if last_user_idx is None:
            display.print_info("No user turn found in history to undo.")
            return "ok"
        dropped = len(messages) - last_user_idx
        del messages[last_user_idx:]
        display.print_info(
            f"⤺ Undid the last turn ({dropped} message(s) dropped from history).\n"
            "   File-system changes from that turn are NOT reverted — use [bold]git[/bold] "
            "or your editor to restore files if needed."
        )
        return "ok"

    if name == "/share":
        if not messages or len(messages) < 2:
            display.print_info("No conversation to share.")
            return "ok"
        try:
            from .agent import make_client as _mk  # noqa: F401
            shared_dir = Path.home() / ".octoslave" / "shared"
            shared_dir.mkdir(parents=True, exist_ok=True)
            import uuid as _uuid, json as _json
            from datetime import datetime as _dt
            sid = _uuid.uuid4().hex[:12]
            title = next((m["content"][:80] for m in messages
                          if m.get("role") == "user" and m.get("content")),
                         "OctoSlave conversation")
            (shared_dir / f"{sid}.json").write_text(_json.dumps({
                "id": sid, "title": title, "model": state.get("model", ""),
                "created_at": _dt.now().isoformat(timespec="seconds"),
                "messages": messages,
            }, indent=2))
            display.console.print(
                f"[dim]Snapshot saved to[/dim] [bold]{shared_dir / (sid + '.json')}[/bold]\n"
                f"[dim]Run [bold]ots web[/bold] and visit[/dim]  "
                f"[bold]http://127.0.0.1:7860/shared/{sid}[/bold]  [dim]to view it.[/dim]"
            )
        except Exception as exc:
            display.print_error(f"Could not share: {exc}")
        return "ok"

    if name == "/parallel":
        try:
            import shlex
            try:
                tokens = shlex.split(arg)
            except ValueError:
                tokens = arg.split()
            n = 3
            strategy = "best"
            models_list: list[str] | None = None
            profiles_list: list[str] | None = None
            judge: str | None = None
            task_tokens: list[str] = []
            for tok in tokens:
                low = tok.lower()
                if low.startswith("models="):
                    models_list = [m.strip() for m in tok.split("=", 1)[1].split(",") if m.strip()]
                elif low.startswith("profiles="):
                    profiles_list = [p.strip() for p in tok.split("=", 1)[1].split(",") if p.strip()]
                elif low.startswith("judge="):
                    judge = tok.split("=", 1)[1].strip() or None
                elif tok.isdigit() and not task_tokens:
                    n = max(1, min(8, int(tok)))
                elif low in ("best", "vote", "merge") and not task_tokens:
                    strategy = low
                else:
                    task_tokens.append(tok)
            task = " ".join(task_tokens).strip()
            if not task:
                display.print_error(
                    "Usage: /parallel [N] [best|vote|merge] "
                    "[models=A,B,C] [profiles=coder,analyst,base] [judge=MODEL] <task>"
                )
                return "ok"
            from .parallel import run_parallel_agents
            run_parallel_agents(
                task=task,
                model=state["model"],
                working_dir=state["working_dir"],
                client=client,
                n=n,
                strategy=strategy,
                permission_mode=state.get("permission_mode", "autonomous"),
                models=models_list,
                profiles=profiles_list,
                judge_model=judge,
            )
        except Exception as exc:
            display.print_error(f"Parallel run failed: {exc}")
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

    if name == "/research-roles":
        _handle_research_roles(arg, state, cfg)
        return "ok"

    if name == "/vault-improve":
        _handle_vault_improve(arg, state, client)
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
    state["model"] = DEFAULT_MODEL
    state["api_key"] = api_key
    state["base_url"] = saved.get("base_url", BASE_URL)

    save_config(
        api_key,
        state["base_url"],
        DEFAULT_MODEL,
        backend="einfra",
        ollama_url=state.get("ollama_url", OLLAMA_BASE_URL),
    )

    display.console.print(
        f"[bold bright_magenta]● e-INFRA CZ mode[/bold bright_magenta] — using [bold]{state['model']}[/bold]"
    )
    messages.clear()
    return "new_client"


def _handle_nim_switch(arg: str, state: dict, messages: list) -> str:
    """Switch to NVIDIA NIM backend. Optionally pass model name as arg."""
    saved = load_config()
    nim_api_key = saved.get("nim_api_key", "")
    if not nim_api_key:
        display.print_error(
            "No NVIDIA NIM API key configured.\n"
            "Run [bold]ots config[/bold] and choose the nim backend, "
            "or set OCTOSLAVE_NIM_API_KEY."
        )
        return "ok"

    nim_url = saved.get("nim_url", NIM_BASE_URL)
    chosen = arg if arg else NIM_DEFAULT_MODEL

    state["backend"] = "nim"
    state["model"] = chosen
    state["api_key"] = nim_api_key
    state["base_url"] = nim_url
    state["nim_api_key"] = nim_api_key
    state["nim_url"] = nim_url

    save_config(
        saved.get("api_key", ""),
        saved.get("base_url", BASE_URL),
        chosen,
        backend="nim",
        ollama_url=saved.get("ollama_url", OLLAMA_BASE_URL),
        nim_api_key=nim_api_key,
        nim_url=nim_url,
    )

    display.console.print(
        f"[bold bright_cyan]● NVIDIA NIM mode[/bold bright_cyan] — using [bold]{chosen}[/bold]"
    )
    display.console.print(
        "[dim]  Switch back: /einfra  or  /local[/dim]"
    )
    messages.clear()
    return "new_client"


def _handle_custom_switch(provider_id: str, state: dict, messages: list,
                          requested_model: str | None = None) -> str:
    """Switch to a custom user-defined provider by id."""
    cfg = load_config()
    provider = get_custom_provider(cfg, provider_id)
    if not provider:
        display.print_error(
            f"Unknown provider '{provider_id}'. "
            f"Run [bold]/provider list[/bold] to see registered providers."
        )
        return "ok"
    if not provider.get("base_url"):
        display.print_error(
            f"Provider '{provider.get('name', provider_id)}' has no base_url configured."
        )
        return "ok"

    chosen = (requested_model or provider.get("default_model")
              or cfg.get("default_model") or "")

    state["backend"] = provider_id
    state["model"] = chosen
    state["api_key"] = provider.get("api_key", "") or "x"
    state["base_url"] = provider["base_url"]

    save_config(
        cfg.get("api_key", ""),
        cfg.get("base_url", BASE_URL),
        chosen or cfg.get("default_model", DEFAULT_MODEL),
        backend=provider_id,
        ollama_url=cfg.get("ollama_url", OLLAMA_BASE_URL),
        nim_api_key=cfg.get("nim_api_key", ""),
        nim_url=cfg.get("nim_url", NIM_BASE_URL),
    )

    display.console.print(
        f"[bold bright_yellow]● {provider.get('name', provider_id)}[/bold bright_yellow]"
        + (f" — using [bold]{chosen}[/bold]" if chosen else "")
    )
    display.console.print("[dim]  Switch back: /einfra · /local · /nim[/dim]")
    messages.clear()
    return "new_client"


def _handle_provider_command(arg: str, state: dict, messages: list) -> str:
    """Manage custom providers from the interactive TUI.

    /provider                       — list registered providers
    /provider list                  — same
    /provider use <id> [model]      — switch to a registered custom provider
    /provider add                   — start interactive add wizard
    /provider remove <id>           — delete a custom provider
    """
    tokens = arg.split() if arg else []
    sub = tokens[0].lower() if tokens else "list"

    if sub in ("list", "ls", ""):
        cfg = load_config()
        providers = list_providers(cfg)
        active = cfg.get("backend", "einfra")
        display.console.print("[bold]Providers[/bold]")
        for p in providers:
            mark = " [green]←[/green]" if p["id"] == active else ""
            tag = "[dim](builtin)[/dim]" if p["kind"] == "builtin" else "[dim](custom)[/dim]"
            display.console.print(f"  [bold]{p['id']}[/bold]  {p['name']}  {tag}{mark}")
        display.console.print(
            "\n[dim]Switch: /provider use <id>   ·   Add: /provider add   ·   "
            "Remove: /provider remove <id>[/dim]"
        )
        return "ok"

    if sub == "use":
        if len(tokens) < 2:
            display.print_error("Usage: /provider use <id> [model]")
            return "ok"
        pid = tokens[1].lower()
        model_arg = tokens[2] if len(tokens) > 2 else None
        if pid == "einfra":
            return _handle_einfra_switch(state, messages)
        if pid == "ollama" or pid == "local":
            return _handle_local_switch(model_arg or "", state, messages)
        if pid == "nim":
            return _handle_nim_switch(model_arg or "", state, messages)
        return _handle_custom_switch(pid, state, messages, model_arg)

    if sub == "add":
        display.console.print("[bold]Add custom provider[/bold]")
        try:
            pid = click.prompt("Provider id (lowercase, e.g. 'openai')").strip().lower()
            name = click.prompt("Display name", default=pid).strip() or pid
            base_url = click.prompt("Base URL (OpenAI-compatible /v1)").strip().rstrip("/")
            api_key = click.prompt("API key (leave blank if none)",
                                   default="", hide_input=True, show_default=False).strip()
            default_model = click.prompt("Default model").strip()
            models_raw = click.prompt(
                "Known models (comma-separated, optional)", default=""
            ).strip()
            context_window = click.prompt(
                "Context window in tokens (optional — blank/0 = auto-detect from "
                "the provider, or fall back to a per-family default)",
                default=0, type=int, show_default=False,
            )
        except click.Abort:
            display.console.print("[dim]Cancelled.[/dim]")
            return "ok"

        try:
            p = add_custom_provider({
                "id": pid,
                "name": name,
                "base_url": base_url,
                "api_key": api_key,
                "default_model": default_model,
                "models": models_raw,
                "context_window": context_window,
            })
        except ValueError as exc:
            display.print_error(str(exc))
            return "ok"

        display.console.print(
            f"[bold green]✓ Provider '{p['id']}' added.[/bold green] "
            f"Switch with [bold]/provider use {p['id']}[/bold]."
        )
        return "ok"

    if sub in ("remove", "rm", "delete"):
        if len(tokens) < 2:
            display.print_error("Usage: /provider remove <id>")
            return "ok"
        pid = tokens[1].lower()
        if remove_custom_provider(pid):
            display.console.print(f"[dim]Provider '{pid}' removed.[/dim]")
            # If we just removed the active provider, fall back to einfra in state.
            if state.get("backend") == pid:
                return _handle_einfra_switch(state, messages)
        else:
            display.print_error(f"No custom provider with id '{pid}'.")
        return "ok"

    display.print_error(
        "Unknown subcommand. Try: /provider list · /provider use <id> · "
        "/provider add · /provider remove <id>"
    )
    return "ok"


def _remote_add_wizard() -> dict | None:
    """Interactive prompts to register a new remote SSH target."""
    display.console.print("[bold]Add remote SSH target[/bold]")
    try:
        host = click.prompt("Host (e.g. gpu.example.org)").strip()
        rid = click.prompt("Id (short handle)", default=host.split(".")[0]).strip().lower()
        name = click.prompt("Display name", default=rid).strip() or rid
        user = click.prompt("SSH user (blank = ssh default)", default="", show_default=False).strip()
        port = click.prompt("Port", default=22, type=int)
        identity = click.prompt("Identity file (private key path, optional)",
                                default="", show_default=False).strip()
    except click.Abort:
        display.console.print("[dim]Cancelled.[/dim]")
        return None
    try:
        r = add_remote({
            "id": rid, "name": name, "host": host, "user": user,
            "port": port, "identity_file": identity,
        })
    except ValueError as exc:
        display.print_error(str(exc))
        return None
    display.console.print(f"[bold green]✓ Remote '{r['id']}' added.[/bold green]")
    return r


def _handle_remote_command(arg: str, state: dict, messages: list) -> str:
    """Switch execution between the local machine and a remote host over SSH.

    /remote                  — show the current target + reachability
    /remote list             — list configured remotes
    /remote local            — switch back to local (default)
    /remote <id>             — switch to a configured remote (SSH)
    /remote add              — register a new remote (wizard)
    /remote remove <id>      — delete a remote
    """
    from .remote import RemoteSession, resolve_start_dir

    tokens = arg.split() if arg else []
    sub = tokens[0].lower() if tokens else ""

    if not sub:
        cur = state.get("remote")
        if cur:
            display.console.print(
                f"[dim]Execution:[/dim] [bold]remote[/bold] → {cur.get('name')} "
                f"({cur.get('user','') + '@' if cur.get('user') else ''}{cur.get('host')}:{state['working_dir']})"
            )
            ok, msg = RemoteSession.get(cur).check()
            display.console.print(("[green]✓ [/green]" if ok else "[red]✗ [/red]") + f"[dim]{msg}[/dim]")
        else:
            display.console.print("[dim]Execution:[/dim] [bold]local[/bold] (this machine)")
        display.console.print("[dim]Switch: /remote <id> · /remote local · /remote add · /remote list[/dim]")
        return "ok"

    if sub in ("list", "ls"):
        remotes = get_remotes()
        if not remotes:
            display.console.print("[dim]No remotes configured. Add one with /remote add.[/dim]")
            return "ok"
        active = (state.get("remote") or {}).get("id")
        display.console.print("[bold]Remotes[/bold]")
        for r in remotes:
            mark = " [green]←[/green]" if r["id"] == active else ""
            display.console.print(
                f"  [bold]{r['id']}[/bold]  {r.get('name','')}  "
                f"[dim]{r.get('user','') + '@' if r.get('user') else ''}{r.get('host')}:{r.get('remote_dir')}[/dim]{mark}"
            )
        return "ok"

    if sub == "local":
        state["remote"] = None
        if state.get("_local_dir"):
            state["working_dir"] = state.pop("_local_dir")
        display.console.print("[dim]Switched to[/dim] [bold]local[/bold] execution.")
        messages.clear()
        return "ok"

    if sub == "add":
        r = _remote_add_wizard()
        if r:
            display.console.print(f"[dim]Activate with[/dim] [bold]/remote {r['id']}[/bold].")
        return "ok"

    if sub in ("remove", "rm", "delete"):
        if len(tokens) < 2:
            display.print_error("Usage: /remote remove <id>")
            return "ok"
        rid = tokens[1].lower()
        if remove_remote(rid):
            display.console.print(f"[dim]Remote '{rid}' removed.[/dim]")
            if (state.get("remote") or {}).get("id") == rid:
                state["remote"] = None
                if state.get("_local_dir"):
                    state["working_dir"] = state.pop("_local_dir")
        else:
            display.print_error(f"No remote with id '{rid}'.")
        return "ok"

    # Otherwise treat the token as a remote id to switch to.
    rid = sub
    remote = get_remote(None, rid)
    if not remote:
        if not get_remotes():
            display.console.print(
                f"[yellow]No remotes configured yet.[/yellow] Let's add one now."
            )
            remote = _remote_add_wizard()
            if not remote:
                return "ok"
        else:
            display.print_error(f"No remote with id '{rid}'. See /remote list.")
            return "ok"

    ok, msg = RemoteSession.get(remote).check()
    if ok:
        display.console.print(f"[green]✓[/green] [dim]{msg}[/dim]")
    else:
        display.console.print(f"[yellow]⚠ could not verify:[/yellow] [dim]{msg}[/dim] (activating anyway)")
    # Remember the local dir so /remote local can restore it, then switch the
    # working dir to the remote home (or configured dir). Navigate with /dir.
    if not state.get("remote"):
        state["_local_dir"] = state["working_dir"]
    state["remote"] = remote
    state["working_dir"] = resolve_start_dir(remote)
    display.console.print(
        f"[dim]Switched to[/dim] [bold]remote[/bold] → {remote.get('name')} "
        f"[dim]({state['working_dir']})[/dim]  ·  change folder with [bold]/dir <path>[/bold]"
    )
    messages.clear()
    return "ok"


def _handle_mcp_command(arg: str, state: dict, messages: list) -> str:
    """Manage MCP (Model Context Protocol) servers — wire in custom tools.

    /mcp                          — list configured MCP servers + live status
    /mcp list                     — same
    /mcp registry                 — browse the catalog of known/recommended servers
    /mcp install <id> [k=v …]     — install a server from the catalog
    /mcp add                      — interactive add wizard (stdio or http)
    /mcp add <name> <command...>  — quick add a stdio server
    /mcp remove <name>            — delete a server
    /mcp enable|disable <name>    — toggle a server without deleting it
    /mcp reconnect                — re-read config and reconnect all servers
    """
    from .mcp_client import manager

    tokens = arg.split() if arg else []
    sub = tokens[0].lower() if tokens else "list"

    if sub in ("registry", "catalog", "store", "browse"):
        return _mcp_registry_list()

    if sub in ("install", "add-known"):
        if len(tokens) < 2:
            display.print_error("Usage: /mcp install <id> [key=value …]   (see /mcp registry)")
            return "ok"
        return _mcp_install(tokens[1], tokens[2:], state)

    if sub in ("list", "ls", ""):
        configured = get_mcp_servers()
        status = {s["name"]: s for s in manager.status()}
        display.console.print("[bold]MCP servers[/bold]")
        if not configured:
            display.console.print(
                "  [dim](none configured)[/dim]\n"
                "[dim]Browse the catalog:  /mcp registry   ·   "
                "Install:  /mcp install <id>   ·   Custom:  /mcp add[/dim]"
            )
            return "ok"
        for s in configured:
            nm = s["name"]
            enabled = s.get("enabled", True)
            transport = "http" if s.get("url") else "stdio"
            live = status.get(nm)
            if not enabled:
                state_str = "[dim]disabled[/dim]"
            elif live and live["connected"]:
                state_str = f"[green]connected[/green] [dim]({live['tool_count']} tools)[/dim]"
            elif live and live["error"]:
                state_str = f"[red]error[/red] [dim]{live['error']}[/dim]"
            else:
                state_str = "[yellow]not connected[/yellow]"
            target = s.get("url") or " ".join([s.get("command", ""), *s.get("args", [])]).strip()
            display.console.print(
                f"  [bold]{nm}[/bold]  [dim]{transport}[/dim]  {state_str}\n"
                f"      [dim]{target}[/dim]"
            )
        display.console.print(
            "\n[dim]Catalog: /mcp registry   ·   Install: /mcp install <id>   ·   "
            "Remove: /mcp remove <name>   ·   Toggle: /mcp enable|disable <name>   ·   "
            "Reconnect: /mcp reconnect[/dim]"
        )
        return "ok"

    if sub == "add":
        # Quick form:  /mcp add <name> <command> [args...]
        if len(tokens) >= 3:
            name = tokens[1]
            command = tokens[2]
            args = tokens[3:]
            entry = {"name": name, "command": command, "args": args, "enabled": True}
        else:
            display.console.print("[bold]Add MCP server[/bold]")
            try:
                name = click.prompt("Name (unique, e.g. 'filesystem')").strip()
                transport = click.prompt(
                    "Transport", type=click.Choice(["stdio", "http"]), default="stdio"
                ).strip()
                if transport == "http":
                    url = click.prompt("URL (e.g. https://host/mcp)").strip()
                    headers_raw = click.prompt(
                        "Headers as KEY=VALUE comma-separated (optional)", default=""
                    ).strip()
                    headers = {}
                    for pair in headers_raw.split(","):
                        if "=" in pair:
                            k, _, v = pair.partition("=")
                            headers[k.strip()] = v.strip()
                    entry = {"name": name, "url": url, "headers": headers, "enabled": True}
                else:
                    command = click.prompt("Command (e.g. 'npx' or 'uvx')").strip()
                    args_raw = click.prompt(
                        "Arguments (space-separated)", default=""
                    ).strip()
                    import shlex
                    env_raw = click.prompt(
                        "Env as KEY=VALUE comma-separated (optional)", default=""
                    ).strip()
                    env = {}
                    for pair in env_raw.split(","):
                        if "=" in pair:
                            k, _, v = pair.partition("=")
                            env[k.strip()] = v.strip()
                    entry = {
                        "name": name,
                        "command": command,
                        "args": shlex.split(args_raw),
                        "env": env,
                        "enabled": True,
                    }
            except click.Abort:
                display.console.print("[dim]Cancelled.[/dim]")
                return "ok"

        try:
            add_mcp_server(entry)
        except ValueError as exc:
            display.print_error(str(exc))
            return "ok"

        display.console.print(
            f"[bold green]✓ MCP server '{entry['name']}' added.[/bold green] Connecting…"
        )
        manager.init_from_config(load_config(), force=True)
        live = {s["name"]: s for s in manager.status()}.get(entry["name"])
        if live and live["connected"]:
            display.console.print(
                f"[green]Connected[/green] — {live['tool_count']} tool(s) available."
            )
        elif live and live["error"]:
            display.print_error(f"Connection failed: {live['error']}")
        return "ok"

    if sub in ("remove", "rm", "delete"):
        if len(tokens) < 2:
            display.print_error("Usage: /mcp remove <name>")
            return "ok"
        nm = tokens[1]
        if remove_mcp_server(nm):
            display.console.print(f"[dim]MCP server '{nm}' removed.[/dim]")
            manager.init_from_config(load_config(), force=True)
        else:
            display.print_error(f"No MCP server named '{nm}'.")
        return "ok"

    if sub in ("enable", "disable"):
        if len(tokens) < 2:
            display.print_error(f"Usage: /mcp {sub} <name>")
            return "ok"
        nm = tokens[1]
        if set_mcp_server_enabled(nm, sub == "enable"):
            display.console.print(f"[dim]MCP server '{nm}' {sub}d.[/dim]")
            manager.init_from_config(load_config(), force=True)
        else:
            display.print_error(f"No MCP server named '{nm}'.")
        return "ok"

    if sub in ("reconnect", "reload", "refresh"):
        display.console.print("[dim]Reconnecting MCP servers…[/dim]")
        manager.init_from_config(load_config(), force=True)
        for s in manager.status():
            if s["connected"]:
                display.console.print(
                    f"  [green]✓[/green] {s['name']} [dim]({s['tool_count']} tools)[/dim]"
                )
            else:
                display.console.print(
                    f"  [red]✗[/red] {s['name']} [dim]{s['error'] or 'not connected'}[/dim]"
                )
        return "ok"

    display.print_error(
        "Unknown subcommand. Try: /mcp list · /mcp registry · /mcp install <id> · "
        "/mcp add · /mcp remove <name> · /mcp enable|disable <name> · /mcp reconnect"
    )
    return "ok"


def _mcp_registry_list() -> str:
    """Render the curated MCP server catalog, grouped by category."""
    from . import mcp_registry as reg

    configured = {s["name"] for s in get_mcp_servers()}
    display.console.print("[bold]MCP catalog[/bold]  [dim](install with /mcp install <id>)[/dim]\n")
    for category, entries in reg.by_category().items():
        display.console.print(f"[bold #fab283]{category}[/bold #fab283]")
        for e in entries:
            runtime = e.get("runtime", "stdio")
            ok = reg.runtime_available(runtime)
            if e["id"] in configured:
                badge = "[green]installed[/green]"
            elif runtime == "http":
                badge = "[dim]remote[/dim]"
            elif ok:
                badge = f"[dim]{runtime}[/dim]"
            else:
                badge = f"[red]needs {runtime}[/red]"
            needs_key = any(i.get("secret") for i in e.get("inputs", []))
            key_tag = " [yellow]🔑[/yellow]" if needs_key else ""
            display.console.print(
                f"  [bold cyan]{e['id']:<20}[/bold cyan] {badge}{key_tag}\n"
                f"      [dim]{e['summary']}[/dim]"
            )
        display.console.print("")
    display.console.print(
        "[dim]🔑 = needs an API key/token.  "
        "Install: /mcp install <id>   ·   e.g. /mcp install filesystem[/dim]"
    )
    return "ok"


def _mcp_install(entry_id: str, inline_args: list[str], state: dict) -> str:
    """Install a server from the catalog. Collects required inputs (inline
    key=value or interactive prompt), then adds + connects it."""
    from . import mcp_registry as reg
    from .mcp_client import manager

    entry = reg.get_entry(entry_id)
    if entry is None:
        display.print_error(
            f"No catalog entry '{entry_id}'. Run /mcp registry to see available ids."
        )
        return "ok"

    runtime = entry.get("runtime", "stdio")
    if not reg.runtime_available(runtime):
        hint = reg.runtime_hint(runtime)
        display.print_error(
            f"'{entry['id']}' needs the [bold]{runtime}[/bold] runtime, which isn't on PATH."
            + (f"\n{hint}" if hint else "")
        )
        return "ok"

    # Parse inline key=value overrides.
    inline: dict = {}
    for tok in inline_args:
        if "=" in tok:
            k, _, v = tok.partition("=")
            inline[k.strip()] = v.strip()

    wd = state.get("working_dir")
    values: dict = {}
    for spec in reg.required_inputs(entry, wd):
        key = spec["key"]
        if key in inline:
            values[key] = inline[key]
            continue
        try:
            if spec["secret"]:
                val = click.prompt(spec["prompt"], hide_input=True, show_default=False, default="").strip()
            else:
                val = click.prompt(spec["prompt"], default=spec.get("default", "")).strip()
        except click.Abort:
            display.console.print("[dim]Cancelled.[/dim]")
            return "ok"
        if not val and spec["secret"]:
            display.print_error(f"'{key}' is required for {entry['id']}.")
            return "ok"
        values[key] = val

    cfg = reg.build_config(entry, values, wd)
    try:
        add_mcp_server(cfg)
    except ValueError as exc:
        # Most likely the name already exists — offer a hint.
        display.print_error(f"{exc}  (remove it first with /mcp remove {cfg['name']})")
        return "ok"

    display.console.print(
        f"[bold green]✓ Installed '{cfg['name']}' from the catalog.[/bold green] Connecting…"
    )
    manager.init_from_config(load_config(), force=True)
    live = {s["name"]: s for s in manager.status()}.get(cfg["name"])
    if live and live["connected"]:
        display.console.print(
            f"[green]Connected[/green] — {live['tool_count']} tool(s) available."
        )
    elif live and live["error"]:
        display.print_error(
            f"Connection failed: {live['error']}\n"
            f"[dim]First run may need to download the package — try /mcp reconnect in a moment.[/dim]"
        )
    return "ok"


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


def _handle_research_roles(arg: str, state: dict, cfg: dict):
    """View or modify per-role model assignments for the research pipeline."""
    backend = state.get("backend", "einfra")
    tokens = arg.split() if arg else []

    # /research-roles reset [backend]
    if tokens and tokens[0] == "reset":
        target_backend = tokens[1] if len(tokens) > 1 else backend
        if target_backend not in ("einfra", "nim", "ollama") and not get_custom_provider(load_config(), target_backend):
            display.print_error(
                f"Unknown backend '{target_backend}'. "
                "Use einfra, nim, ollama, or a custom provider id."
            )
            return
        reset_role_models(target_backend)
        cfg.update(load_config())
        display.console.print(
            f"[dim]Custom role models cleared for backend[/dim] [bold]{target_backend}[/bold]"
        )
        return

    # /research-roles <role> <model>
    if len(tokens) >= 2:
        role_name = tokens[0].lower()
        model_name = tokens[1]
        if role_name not in PIPELINE_ROLES:
            display.print_error(
                f"Unknown role '{role_name}'. Valid roles: {', '.join(PIPELINE_ROLES)}"
            )
            return
        save_role_model(role_name, model_name, backend)
        cfg.update(load_config())
        display.console.print(
            f"[dim]Role[/dim] [bold cyan]{role_name}[/bold cyan] "
            f"[dim]→[/dim] [bold magenta]{model_name}[/bold magenta] "
            f"[dim](backend: {backend})[/dim]"
        )
        return

    # /research-roles — show current assignments
    effective = get_role_models(cfg)
    display.print_role_models_config(backend, effective, cfg.get(f"role_models_{backend}") or {})


def _handle_long_research(arg: str, state: dict, cfg: dict, client):
    """Parse /long-research flags and launch the research pipeline."""
    import shlex

    try:
        tokens = shlex.split(arg)
    except ValueError:
        tokens = arg.split()

    topic_parts: list[str] = []
    max_rounds = 5
    min_rounds = 2
    all_model: str | None = None
    overseer_model: str | None = None
    role_flag_overrides: dict[str, str] = {}
    resume = False
    num_parallel = 1
    scrape_mode = False

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
        elif t == "--parallel" and i + 1 < len(tokens):
            try:
                num_parallel = int(tokens[i + 1])
                if num_parallel < 1:
                    raise ValueError
            except ValueError:
                display.print_error(f"--parallel expects a positive integer, got: {tokens[i+1]}")
                return
            i += 2
        elif t == "--all" and i + 1 < len(tokens):
            all_model = tokens[i + 1]
            i += 2
        elif t == "--overseer" and i + 1 < len(tokens):
            overseer_model = tokens[i + 1]
            i += 2
        elif t == "--role" and i + 2 < len(tokens):
            role_name = tokens[i + 1].lower()
            role_model = tokens[i + 2]
            if role_name not in PIPELINE_ROLES:
                display.print_error(
                    f"Unknown role '{role_name}'. Valid roles: {', '.join(PIPELINE_ROLES)}"
                )
                return
            role_flag_overrides[role_name] = role_model
            i += 3
        elif t == "--resume":
            resume = True
            i += 1
        elif t == "--min-rounds" and i + 1 < len(tokens):
            try:
                min_rounds = int(tokens[i + 1])
                if min_rounds < 1:
                    raise ValueError
            except ValueError:
                display.print_error(f"--min-rounds expects a positive integer, got: {tokens[i + 1]}")
                return
            i += 2
        elif t == "--scrape":
            scrape_mode = True
            i += 1
        else:
            topic_parts.append(t)
            i += 1

    topic = " ".join(topic_parts).strip()
    if not topic:
        display.print_error(
            "Usage: /long-research <topic> [--rounds N] [--parallel N] "
            "[--min-rounds N] [--all MODEL] [--overseer MODEL] [--role ROLE MODEL] "
            "[--resume] [--scrape]"
        )
        return

    overrides: dict[str, str] = {}

    if state["backend"] == "ollama" and not all_model:
        # Auto-assign local models across roles then apply any saved custom overrides
        pulled = ollama_list_models(state.get("ollama_url", OLLAMA_BASE_URL))
        if not pulled:
            display.print_error("No Ollama models available for local research.")
            return
        overrides = assign_local_models(pulled)
        # Apply saved custom ollama role overrides
        overrides.update(cfg.get("role_models_ollama") or {})
        display.print_local_research_assignment(overrides)
    elif all_model:
        for role in PIPELINE_ROLES:
            overrides[role] = all_model
    else:
        # Load per-backend defaults + any saved custom role overrides
        overrides = get_role_models(cfg)

    # The static role pipeline has been replaced by the dynamic Lab. The legacy
    # per-role / --parallel / --scrape flags no longer apply; warn if used.
    if role_flag_overrides or overseer_model or num_parallel > 1 or scrape_mode:
        display.print_info(
            "  [dim]Note: --role/--overseer/--parallel/--scrape are legacy flags "
            "from the old fixed pipeline and are ignored by the new Lab.[/dim]"
        )

    # Single model for all roles: --all wins, else the backend default (kimi-k2.7).
    lab_model = all_model or cfg.get("default_model")

    from .lab.runner import run_lab
    run_lab(
        task=topic,
        working_dir=state["working_dir"],
        client=client,
        model=lab_model,
        autonomous=True,
        max_rounds=max_rounds,
        resume=resume,
    )


def _handle_vault_improve(arg: str, state: dict, client):
    """Parse /vault-improve flags and launch the autonomous vault pipeline."""
    import shlex

    try:
        tokens = shlex.split(arg)
    except ValueError:
        tokens = arg.split()

    vault_path: str | None = None
    resume = False
    model: str | None = None

    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t == "--resume":
            resume = True
            i += 1
        elif t == "--model" and i + 1 < len(tokens):
            model = tokens[i + 1]
            i += 2
        elif not t.startswith("--"):
            vault_path = str(Path(t).expanduser().resolve())
            i += 1
        else:
            display.print_error(f"Unknown flag: {t}")
            return

    if not vault_path:
        # Default to current working dir
        vault_path = state["working_dir"]

    if not Path(vault_path).is_dir():
        display.print_error(f"Not a directory: {vault_path}")
        return

    profile = state.get("prompt_profile", "base")

    display.console.print()
    display.console.print(
        f"[bold bright_white]🐙 VAULT IMPROVE[/bold bright_white]\n"
        f"[dim]vault   :[/dim] {vault_path}\n"
        f"[dim]profile :[/dim] {profile}\n"
        f"[dim]model   :[/dim] {model or 'role defaults'}\n"
        f"[dim]resume  :[/dim] {resume}"
    )
    display.console.print()

    try:
        run_vault_improve(
            vault_path=vault_path,
            client=client,
            prompt_profile=profile,
            model=model,
            resume=resume,
        )
    except KeyboardInterrupt:
        display.console.print("\n[dim]Vault improve interrupted. Run again with --resume to continue.[/dim]")


# ---------------------------------------------------------------------------
# Prompt-toolkit helpers
# ---------------------------------------------------------------------------

def _make_prompt(state: dict):
    model_short = state["model"][:20]
    backend = state.get("backend", "einfra")
    if state.get("council"):
        # Unified-agent surface: the Worker is the visible executor.
        return HTML(f'<prompt>🐙</prompt> <model-tag>[council:{model_short}]</model-tag> ')
    if backend == "ollama":
        return HTML(f'<prompt-local>●</prompt-local> <model-tag>[local:{model_short}]</model-tag> ')
    if backend == "nim":
        return HTML(f'<prompt-nim>●</prompt-nim> <model-tag>[nim:{model_short}]</model-tag> ')
    return HTML(f'<prompt>●</prompt> <model-tag>[{model_short}]</model-tag> ')


def _make_toolbar(state: dict):
    wd = state["working_dir"]
    if len(wd) > 50:
        wd = "…" + wd[-48:]
    remote = state.get("remote")
    dir_icon = "🌐" if remote else "📁"
    remote_tag = f' ⇅{remote.get("name")}' if remote else ""
    backend = state.get("backend", "einfra")
    if backend == "ollama":
        toolbar_cls = "bottom-toolbar-local"
        backend_tag = " 🟢local"
    elif backend == "nim":
        toolbar_cls = "bottom-toolbar-nim"
        backend_tag = " 🔵nim"
    else:
        toolbar_cls = "bottom-toolbar"
        backend_tag = " 🟣einfra"
    profile = state.get("prompt_profile", "base")
    perm_mode = state.get("permission_mode", "autonomous")
    if perm_mode == "autonomous":
        perm_short = "auto"
    elif perm_mode == "controlled":
        perm_short = "ctrl"
    else:
        perm_short = "supv"
    plan_short = "plan:on" if state.get("enable_plan", True) else "plan:off"
    verify_short = "verify:on" if state.get("enable_verify", True) else "verify:off"
    mem_short = "mem:on" if state.get("enable_memory", True) else "mem:off"
    if state.get("council"):
        roles = state.get("council_roles") or {}
        council_tag = (
            f'  🐙council[W:{roles.get("worker","?")} '
            f'T:{roles.get("thinker","?")} V:{roles.get("verifier","?")}]'
        )
    else:
        council_tag = ""
    return HTML(
        f'<{toolbar_cls}>  {dir_icon} {wd}{remote_tag}{backend_tag}{council_tag}  ●{profile}  🛡{perm_short}'
        f'  {plan_short}  {verify_short}  {mem_short}'
        f'   /help · /improved · /remote · /model · /clear · /exit</{toolbar_cls}>'
    )


def _make_keybindings(state: dict | None = None) -> KeyBindings:
    kb = KeyBindings()

    @kb.add("c-l")
    def _clear_screen(event):
        event.app.renderer.clear()

    if state is not None:
        @kb.add("c-t")
        def _toggle_perm(event):
            """Ctrl+T flips between autonomous ('build') and controlled ('plan')."""
            cur = state.get("permission_mode", "autonomous")
            new = "controlled" if cur == "autonomous" else "autonomous"
            state["permission_mode"] = new
            display.console.print(
                f"\n[dim]permission mode →[/dim] [bold]{new}[/bold]"
            )

    return kb


# ---------------------------------------------------------------------------
# @-file completer for the interactive TUI
# ---------------------------------------------------------------------------

class _AtFileCompleter(Completer):
    """Suggest paths from the working directory after an `@` token.

    Triggers only when the cursor sits right after `@<query>` — leaves the
    rest of the prompt alone so it doesn't fight with slash-commands.
    """

    _SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__",
                  ".parallel", ".uploads", ".pytest_cache", "dist", "build"}
    _MAX = 40

    def __init__(self, state: dict):
        self.state = state
        self._cache: dict[str, list[str]] = {}

    def _index(self, root: Path) -> list[str]:
        key = str(root)
        if key in self._cache:
            return self._cache[key]
        out: list[str] = []
        try:
            for p in root.rglob("*"):
                if not p.is_file():
                    continue
                rel_parts = p.parts[len(root.parts):]
                if any(part in self._SKIP_DIRS for part in rel_parts):
                    continue
                if any(part.startswith(".") and len(part) > 1 for part in rel_parts):
                    continue
                out.append(str(p.relative_to(root)))
                if len(out) >= 2000:
                    break
        except Exception:
            pass
        out.sort(key=lambda s: (s.count("/"), len(s), s))
        self._cache[key] = out
        return out

    def get_completions(self, document, complete_event):
        text_before = document.text_before_cursor
        at_pos = text_before.rfind("@")
        if at_pos < 0:
            return
        # The @ must follow whitespace or be the very first char; otherwise this
        # is something like an email address.
        if at_pos > 0 and not text_before[at_pos - 1].isspace():
            return
        if any(c.isspace() for c in text_before[at_pos + 1:]):
            return
        query = text_before[at_pos + 1:].lower()

        wd = self.state.get("working_dir") or os.getcwd()
        try:
            root = Path(wd).expanduser().resolve()
        except Exception:
            return
        if not root.is_dir():
            return

        files = self._index(root)
        seen = 0
        for f in files:
            if query and query not in f.lower():
                continue
            yield Completion(
                text=f,
                start_position=-(len(text_before) - at_pos - 1),
                display=f,
            )
            seen += 1
            if seen >= self._MAX:
                break


# ---------------------------------------------------------------------------
# Directory helpers
# ---------------------------------------------------------------------------

def _make_task_dir() -> str:
    """
    Return the default working directory when the user does not specify one.
    If cwd is outside the octoslave repo, use cwd (so `ots` works in-place on
    any project the user is already in).  If cwd IS the octoslave repo dir,
    create ~/octoslave/tasks/YYYYMMDD_HHMMSS/ to avoid polluting the repo.
    """
    cwd = Path.cwd().resolve()
    octoslave_repo = (Path.home() / "octoslave").resolve()
    if cwd != octoslave_repo and not str(cwd).startswith(str(octoslave_repo) + "/"):
        return str(cwd)
    from datetime import datetime as _dt
    timestamp = _dt.now().strftime("%Y%m%d_%H%M%S")
    task_dir = octoslave_repo / "tasks" / timestamp
    task_dir.mkdir(parents=True, exist_ok=True)
    return str(task_dir)


def _make_project_dir(task: str) -> str:
    """
    Create ~/octoslave/projects/MMDD-word1-word2 from the task description.
    Returns the absolute path (already created).
    """
    import re
    from datetime import date as _date
    # Common stop words in English and Czech
    STOP_WORDS = {
        # English
        "i", "me", "my", "myself", "we", "our", "ours", "ourselves", "you", "your", "yours",
        "yourself", "yourselves", "he", "him", "his", "himself", "she", "her", "hers",
        "herself", "it", "its", "itself", "they", "them", "their", "theirs", "themselves",
        "what", "which", "who", "whom", "this", "that", "these", "those", "am", "is", "are",
        "was", "were", "be", "been", "being", "have", "has", "had", "having", "do", "does",
        "did", "doing", "a", "an", "the", "and", "but", "if", "or", "because", "as", "until",
        "while", "of", "at", "by", "for", "with", "about", "against", "between", "into",
        "through", "during", "before", "after", "above", "below", "to", "from", "up", "down",
        "in", "out", "on", "off", "over", "under", "again", "further", "then", "once",
        "here", "there", "when", "where", "why", "how", "all", "any", "both", "each",
        "few", "more", "most", "other", "some", "such", "no", "nor", "not", "only", "own",
        "same", "so", "than", "too", "very", "s", "t", "can", "will", "just", "don",
        "should", "now",
        # Czech common words
        "a", "aby", "ale", "ani", "ano", "asi", "během", "bez", "bude", "budeme", "budete",
        "budeš", "budou", "by", "byl", "byla", "byli", "bylo", "byly", "bys", "často", "či",
        "co", "což", "či", "článek", "článku", "články", "další", "dnes", "do", "ho", "i",
        "já", "je", "jeden", "jedna", "jedno", "jeho", "jej", "její", "jejich", "jen", "jenž",
        "ještě", "ji", "jiné", "již", "jsem", "jsi", "jsme", "jsou", "jste", "k", "kam",
        "každý", "kde", "ke", "kdo", "kdy", "když", "ke", "která", "které", "který", "kteří",
        "ku", "ma", "me", "mě", "mezi", "mi", "mne", "mně", "mno", "mou", "možná", "můj",
        "musí", "my", "na", "nad", "nám", "námi", "naproti", "nás", "náš", "naše", "naši",
        "ne", "nebo", "nebyl", "nebyla", "nebyli", "nebyly", "nechť", "ně", "něco", "nějak",
        "nejsi", "někdo", "některý", "nemá", "nemají", "neměl", "není", "nestačí", "nevím",
        "než", "nic", "nich", "ním", "nimi", "němu", "ní", "něj", "nyní", "od", "ode", "on",
        "ona", "oni", "ono", "ony", "o", "po", "pod", "podle", "pokud", "pouze", "pro",
        "proč", "proto", "protože", "před", "přes", "při", "roku", "s", "se", "si", "sice",
        "skoro", "sobě", "spolu", "sta", "své", "svůj", "svých", "svým", "svými", "ta",
        "tak", "také", "takže", "tam", "tamhle", "tamhleto", "tamto", "tě", "tebe", "tebou",
        "ted'", "tedy", "ten", "tento", "této", "ti", "tím", "tímto", "tip", "tipy", "to",
        "tobě", "tohle", "toho", "tohoto", "tom", "tomto", "tomu", "tomuto", "toto", "tu",
        "tuto", "tvá", "tvé", "tvoje", "tvůj", "ty", "tý", "tyto", "u", "už", "v", "váš",
        "vaše", "vaši", "ve", "více", "vlastně", "však", "všechen", "všechno", "všechny",
        "všichni", "vůbec", "vy", "vám", "vámi", "vás", "z", "za", "že",
        # Common programming/tech words that might not be informative
        "check", "code", "build", "create", "make", "write", "edit", "fix", "update",
        "add", "remove", "delete", "test", "run", "execute", "implement", "please",
        "need", "want", "could", "would", "should", "maybe", "perhaps", "help",
        "using", "via", "based", "like", "similar", "example", "etc", "eg", "ie",
        "vs", "ok", "yes", "no", "well", "also", "too", "very", "really", "quite",
        "actually", "basically", "literally", "seriously", "honestly", "probably",
        # Czech verbs and common words
        "přepiš", "přepisovat", "napiš", "udělej", "vytvoř", "zkontroluj", "oprav", "uprav",
        "změň", "přidej", "odeber", "smaž", "spusť", "testuj", "implementuj", "prosím",
        "potřebuji", "chci", "mohl", "by", "asi", "možná", "snad", "pomoc",
        # Generic nouns that might not be informative
        "notes", "note", "data", "file", "files", "folder", "directory", "project",
        "task", "work", "job", "thing", "stuff", "items", "element", "component",
    }
    
    # Clean and tokenize
    words = re.findall(r'[\wěščřžýáíéůúďťňó]+', task.lower().strip())

    # Filter stop words and short words, take 2 most meaningful
    filtered = [w for w in words if w not in STOP_WORDS and len(w) > 2]
    if not filtered:
        filtered = [w for w in words if len(w) > 2]
    selected = filtered[:2] if filtered else (words[:2] if words else ["project"])

    # Truncate each word to 12 chars, join with dash
    slug = "-".join(w[:12] for w in selected)

    # Prefix with MMDD date
    today = _date.today()
    slug = f"{today.month:02d}{today.day:02d}-{slug}"

    projects_root = Path.home() / "octoslave" / "projects"
    project_dir = projects_root / slug

    # If dir already exists (same task same day), add suffix
    if project_dir.exists():
        base = project_dir
        for n in range(2, 99):
            candidate = projects_root / f"{slug}-{n}"
            if not candidate.exists():
                project_dir = candidate
                break
            project_dir = base  # fallback: reuse existing

    project_dir.mkdir(parents=True, exist_ok=True)
    return str(project_dir)


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
                "Pull one with: [bold]ollama pull qwen2.5:7b[/bold]"
            )
            sys.exit(1)
        # When no model is explicitly requested, prefer models with better tool-calling support
        ranked = sort_by_tool_calling(pulled)
        chosen_model = model or saved.get("default_model") or ranked[0]
        if chosen_model not in pulled:
            display.console.print(
                f"[dim]Model '{chosen_model}' not found locally, "
                f"using '{ranked[0]}' instead.[/dim]"
            )
            chosen_model = ranked[0]
        return {
            "api_key":     "ollama",
            "base_url":    ollama_url,
            "model":       chosen_model,
            "working_dir": str(Path(working_dir).resolve()) if working_dir else _make_task_dir(),
            "backend":     "ollama",
            "ollama_url":  ollama_url,
            "nim_api_key": saved.get("nim_api_key", ""),
            "nim_url":     saved.get("nim_url", NIM_BASE_URL),
        }

    if backend == "nim":
        nim_api_key = saved.get("nim_api_key", "")
        if not nim_api_key:
            display.print_error(
                "No NVIDIA NIM API key configured.\n"
                "Run [bold]ots config[/bold] and choose the nim backend, "
                "or set OCTOSLAVE_NIM_API_KEY."
            )
            sys.exit(1)
        nim_url = saved.get("nim_url", NIM_BASE_URL)
        return {
            "api_key":     nim_api_key,
            "base_url":    nim_url,
            "model":       model or saved.get("default_model", NIM_DEFAULT_MODEL),
            "working_dir": str(Path(working_dir).resolve()) if working_dir else _make_task_dir(),
            "backend":     "nim",
            "ollama_url":  ollama_url,
            "nim_api_key": nim_api_key,
            "nim_url":     nim_url,
        }

    # Custom user-defined provider (anything that isn't a built-in backend).
    if backend not in BUILTIN_BACKENDS:
        provider = get_custom_provider(saved, backend)
        if not provider:
            display.print_error(
                f"Configured backend '{backend}' is not registered. "
                "Run [bold]ots provider list[/bold] to see available providers."
            )
            sys.exit(1)
        if not provider.get("base_url"):
            display.print_error(
                f"Custom provider '{provider.get('name', backend)}' has no base_url. "
                "Re-add it with [bold]ots provider add[/bold]."
            )
            sys.exit(1)
        return {
            "api_key":     provider.get("api_key", "") or "x",
            "base_url":    provider["base_url"],
            "model":       model or saved.get("default_model") or provider.get("default_model", ""),
            "working_dir": str(Path(working_dir).resolve()) if working_dir else _make_task_dir(),
            "backend":     backend,
            "ollama_url":  ollama_url,
            "nim_api_key": saved.get("nim_api_key", ""),
            "nim_url":     saved.get("nim_url", NIM_BASE_URL),
        }

    # e-INFRA CZ backend
    return {
        "api_key":     api_key or saved.get("api_key", ""),
        "base_url":    base_url or saved.get("base_url", BASE_URL),
        "model":       model or saved.get("default_model", DEFAULT_MODEL),
        "working_dir": str(Path(working_dir).resolve()) if working_dir else _make_task_dir(),
        "backend":     "einfra",
        "ollama_url":  ollama_url,
        "nim_api_key": saved.get("nim_api_key", ""),
        "nim_url":     saved.get("nim_url", NIM_BASE_URL),
    }


@cli.command("vault-improve")
@click.argument("vault_path", default=None, required=False)
@click.option("-p", "--profile", "prompt_profile", default="base",
              help="Prompt profile (default: base, options: base, coder, analyst, cryouncle)")
@click.option("-m", "--model", default=None, help="Model override for all vault agents")
@click.option("--resume", is_flag=True, default=False, help="Resume interrupted run")
@click.option("--api-key", default=None, envvar="OCTOSLAVE_API_KEY")
@click.option("--base-url", default=None, envvar="OCTOSLAVE_BASE_URL")
def vault_improve_cmd(vault_path, prompt_profile, model, resume, api_key, base_url):
    """Autonomously improve every note in a vault (Obsidian / markdown folder).

    \b
    Examples:
      octoslave vault-improve ~/Brain2 --profile base
      octoslave vault-improve ~/Brain2 --profile base --resume
      octoslave vault-improve ~/Brain2 --model deepseek-v3.2-thinking
    """
    from .vault import run_vault_improve
    from .agent import make_client

    cfg = _resolve_config(None, vault_path, api_key, base_url)
    vault = str(Path(vault_path).expanduser().resolve()) if vault_path else os.getcwd()

    if not Path(vault).is_dir():
        display.print_error(f"Not a directory: {vault}")
        sys.exit(1)

    client = make_client(cfg["api_key"], cfg["base_url"])

    display.console.print()
    display.console.print(
        f"[bold bright_white]🐙 VAULT IMPROVE[/bold bright_white]\n"
        f"[dim]vault  :[/dim] {vault}\n"
        f"[dim]profile:[/dim] {prompt_profile}\n"
        f"[dim]model  :[/dim] {model or 'role defaults'}\n"
        f"[dim]resume :[/dim] {resume}"
    )
    display.console.print()

    try:
        run_vault_improve(
            vault_path=vault,
            client=client,
            prompt_profile=prompt_profile,
            model=model,
            resume=resume,
        )
    except KeyboardInterrupt:
        display.console.print("\n[dim]Interrupted. Run with --resume to continue.[/dim]")
        sys.exit(0)


@cli.command("batch")
@click.argument("tasks_file")
@click.option("-m", "--model", default=None, help="Model to use for all tasks")
@click.option("-p", "--profile", "prompt_profile", default="base",
              help="Prompt profile (default: base)")
@click.option("--permission-mode", default=None,
              type=click.Choice(["autonomous", "controlled", "supervised"]))
@click.option("--resume", is_flag=True, default=False,
              help="Skip tasks already marked done in the state file")
@click.option("--output-dir", default=None,
              help="Root dir for task outputs (default: ~/octoslave/projects/)")
@click.option("--api-key", default=None, envvar="OCTOSLAVE_API_KEY")
@click.option("--base-url", default=None, envvar="OCTOSLAVE_BASE_URL")
def batch_cmd(tasks_file, model, prompt_profile, permission_mode, resume,
              output_dir, api_key, base_url):
    """Run a list of tasks from a file, one by one, with resume support.

    \b
    TASKS_FILE: plain text file, one task per line. Lines starting with #
    are treated as comments and skipped. Empty lines are skipped.

    \b
    Examples:
      octoslave batch tasks.txt
      octoslave batch tasks.txt --profile base --resume
      octoslave batch tasks.txt -m deepseek-v3.2-thinking --output-dir ~/results
    \b
    State is saved to TASKS_FILE.state.json after every completed task.
    Re-run with --resume to skip already completed tasks.
    """
    import json as _json
    from .agent import make_client, run_agent

    tasks_path = Path(tasks_file).expanduser().resolve()
    if not tasks_path.exists():
        display.print_error(f"Tasks file not found: {tasks_file}")
        sys.exit(1)

    # Parse tasks — skip comments and blank lines
    raw_lines = tasks_path.read_text(encoding="utf-8").splitlines()
    tasks = [ln.strip() for ln in raw_lines
             if ln.strip() and not ln.strip().startswith("#")]

    if not tasks:
        display.print_error("No tasks found in file.")
        sys.exit(1)

    # State file — tracks which tasks are done
    state_file = tasks_path.with_suffix(".state.json")
    state: dict = {}
    if resume and state_file.exists():
        try:
            state = _json.loads(state_file.read_text(encoding="utf-8"))
        except Exception:
            state = {}

    cfg = _resolve_config(None, None, api_key, base_url)
    if model:
        cfg["model"] = model
    if permission_mode:
        cfg["permission_mode"] = permission_mode
    else:
        saved_cfg = load_config()
        cfg["permission_mode"] = saved_cfg.get("permission_mode", "autonomous")

    client = make_client(cfg["api_key"], cfg["base_url"])

    root_dir = Path(output_dir).expanduser().resolve() if output_dir \
        else Path.home() / "octoslave" / "projects"
    root_dir.mkdir(parents=True, exist_ok=True)

    total = len(tasks)
    done = sum(1 for t in tasks if state.get(t) == "done")

    display.console.print()
    display.console.print(
        f"[bold bright_white]🐙 BATCH RUN[/bold bright_white]\n"
        f"[dim]tasks  :[/dim] {total} ({done} already done)\n"
        f"[dim]model  :[/dim] {cfg['model']}\n"
        f"[dim]profile:[/dim] {prompt_profile}\n"
        f"[dim]output :[/dim] {root_dir}\n"
        f"[dim]resume :[/dim] {resume}"
    )
    display.console.print()

    for i, task in enumerate(tasks, 1):
        if state.get(task) == "done":
            display.console.print(
                f"[dim]  [{i}/{total}] skipping (done): {task[:60]}[/dim]"
            )
            continue

        project_dir = _make_project_dir(task)
        display.console.print(
            f"\n[bold bright_white]  [{i}/{total}] {task[:80]}[/bold bright_white]\n"
            f"[dim]  → {project_dir}[/dim]\n"
        )
        display.print_task(task)

        try:
            run_agent(
                task, cfg["model"], project_dir, client,
                prompt_profile, cfg["permission_mode"]
            )
            state[task] = "done"
        except KeyboardInterrupt:
            display.console.print(
                "\n[bold yellow]Batch paused.[/bold yellow] "
                "Re-run with --resume to continue."
            )
            state_file.write_text(
                _json.dumps(state, indent=2, ensure_ascii=False),
                encoding="utf-8"
            )
            sys.exit(0)
        except Exception as e:
            display.print_error(f"Task failed: {e}")
            state[task] = f"failed: {e}"

        # Save state after every task
        state_file.write_text(
            _json.dumps(state, indent=2, ensure_ascii=False),
            encoding="utf-8"
        )

    done_count = sum(1 for v in state.values() if v == "done")
    failed_count = sum(1 for v in state.values() if str(v).startswith("failed"))
    display.console.print()
    display.console.print(
        f"[bold bright_green]✓ Batch complete[/bold bright_green]  "
        f"{done_count}/{total} done"
        + (f"  [bold red]{failed_count} failed[/bold red]" if failed_count else "")
    )
    display.console.print(f"[dim]State saved to: {state_file}[/dim]")


@cli.command()
@click.option("--host", default="127.0.0.1", show_default=True, help="Host to bind to")
@click.option("--port", default=7860, show_default=True, help="Port to listen on")
@click.option("--no-browser", is_flag=True, default=False, help="Do not open browser automatically")
def web(host, port, no_browser):
    """Launch the OctoSlave web UI in a browser."""
    try:
        import uvicorn
    except ImportError:
        display.print_error(
            "uvicorn is not installed. Run:  pip install 'octoslave[web]'  or  pip install uvicorn fastapi"
        )
        sys.exit(1)

    url = f"http://{host}:{port}"
    display.console.print()
    display.console.print(
        f"  [bold #fab283]🐙 OctoSlave Web UI[/bold #fab283]  "
        f"[dim #7a7d86]starting at[/dim #7a7d86]  [bold #5c9cf5]{url}[/bold #5c9cf5]"
    )
    display.console.print("  [dim #7a7d86]Press Ctrl+C to stop.[/dim #7a7d86]\n")

    if not no_browser:
        import threading, webbrowser
        # Open browser after a short delay so the server is ready
        def _open():
            import time; time.sleep(1.2)
            webbrowser.open(url)
        threading.Thread(target=_open, daemon=True).start()

    from .web.app import app as _web_app
    uvicorn.run(_web_app, host=host, port=port, log_level="warning")


def main():
    cli()


if __name__ == "__main__":
    main()
