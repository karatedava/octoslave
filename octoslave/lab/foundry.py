"""
Tool foundry — runtime capability expansion (build + use, sandboxed).

When a specialist hits a capability it lacks, it calls one of three meta-tools
(registered into the live tool registry while a lab runs):

  request_tool(name, purpose, signature)  — a tool-engineer LLM writes a new
        Python tool to lab/tools/<name>.py; it is validated (import + shape) and
        registered into the live registry, callable immediately by any agent.
  request_agent(name, role, expertise, goal, tools)  — add a specialist to the
        team (capped at 10); it joins the next meeting/round.
  request_mcp(server_id, values)  — connect a known MCP server from the registry
        at runtime; its tools appear in the registry on reconnect.

A built tool runs in-process via ``tools.execute_tool`` (so the existing
permission flow still applies to anything it does through bash/file tools); its
source lives in lab/tools/ for human inspection.

Context (client/model/session/working_dir/emit) is set once per run by the
runner; the lab loop is single-threaded so a module-level context is safe.
"""

from __future__ import annotations

import importlib.util
import re
import threading

from .. import display
from .. import tools as _tools
from .llm import complete_text
from .state import AgentSpec

# --- per-run context ---------------------------------------------------------
_CTX = threading.local()


def set_context(client, model, working_dir, session, emit=None) -> None:
    _CTX.client = client
    _CTX.model = model
    _CTX.working_dir = working_dir
    _CTX.session = session
    _CTX.emit = emit


def set_current_agent(spec) -> None:
    """Tell the foundry which agent is currently running, so a tool it builds can
    be granted to it (and so per-agent request caps apply). Called by
    agent_runtime around each agent's loop."""
    _CTX.current_agent = spec
    if getattr(_CTX, "tool_req_counts", None) is None:
        _CTX.tool_req_counts = {}
    if getattr(_CTX, "agent_req_counts", None) is None:
        _CTX.agent_req_counts = {}


def clear_current_agent() -> None:
    if hasattr(_CTX, "current_agent"):
        _CTX.current_agent = None


# Cap on tools one agent may build in a single meeting (runaway guard).
_MAX_TOOL_REQUESTS_PER_AGENT = 3
# Cap on teammates one agent may request in a single meeting (runaway guard).
_MAX_AGENT_REQUESTS_PER_AGENT = 2
# System roles that already exist as built-ins — agents must NOT spawn duplicates.
_RESERVED_ROLE_WORDS = {"critic", "skeptic", "reviewer", "qa", "auditor",
                        "director", "orchestrator", "principal", "investigator", "pi"}
_ROLE_STOPWORDS = {"specialist", "engineer", "analyst", "scientist", "expert",
                   "agent", "lead", "assistant", "of", "and", "the", "for", "to"}


def _role_tokens(*parts: str) -> set:
    import re
    toks = set()
    for p in parts:
        toks |= {w for w in re.findall(r"[a-z]+", (p or "").lower())
                 if len(w) > 2 and w not in _ROLE_STOPWORDS}
    return toks


def _similar_active_agent(session, name: str, role: str):
    """Return an existing active agent that substantially overlaps the requested
    name/role (so we don't spawn near-duplicates like 3 'Python runner' agents)."""
    want = _role_tokens(name, role)
    nm = (name or "").strip().lower()
    for a in session.active_team:
        if nm and nm == (a.name or "").strip().lower():
            return a
        have = _role_tokens(a.name, a.role)
        if want and len(want & have) >= 2:
            return a
    return None


def current_session():
    """The active LabSession for this run (or None) — used e.g. by the heartbeat
    to keep state.json fresh during a long meeting."""
    return getattr(_CTX, "session", None)


def clear_context() -> None:
    for attr in ("client", "model", "working_dir", "session", "emit", "registered",
                 "current_agent", "tool_req_counts", "agent_req_counts"):
        if hasattr(_CTX, attr):
            delattr(_CTX, attr)


def disable() -> None:
    """Tear down a run: UNREGISTER every tool the foundry added (meta-tools and
    runtime-built tools) so a later single-agent / chat run in the same process
    sees the clean built-in surface, then clear the context."""
    for name in _ctx("registered", set()) or set():
        _tools.unregister_dynamic_tool(name)
    clear_context()


def _ctx(attr, default=None):
    return getattr(_CTX, attr, default)


def _track(name: str) -> None:
    """Record a dynamic-tool name registered this run (for teardown)."""
    reg = getattr(_CTX, "registered", None)
    if reg is None:
        reg = set()
        _CTX.registered = reg
    reg.add(name)


def _emit(event: dict):
    cb = _ctx("emit")
    if cb:
        try:
            cb(event)
        except Exception:
            pass


META_TOOL_NAMES = ("request_tool", "request_agent", "request_mcp")

_VALID_NAME = re.compile(r"^[a-z][a-z0-9_]{2,40}$")


# ---------------------------------------------------------------------------
# Tool-engineer: generate + validate + register a runtime tool
# ---------------------------------------------------------------------------

_ENGINEER_SYSTEM = (
    "You are a senior Python tool engineer for an autonomous lab. You write a "
    "single, self-contained Python tool module that other agents can call. The "
    "module MUST define exactly two top-level objects:\n"
    "  TOOL_DEFINITION: an OpenAI function-tool schema dict of the form "
    "{'type':'function','function':{'name','description','parameters':{...JSON "
    "schema...}}}.\n"
    "  def run(args: dict, working_dir: str) -> tuple[str, bool]: the "
    "implementation. It returns (result_text, ok). Catch your own exceptions and "
    "return (error_text, False) — never raise. Keep all file paths inside "
    "working_dir. Use only the Python standard library unless a dependency is "
    "clearly already available. No top-level side effects, no network unless the "
    "tool's purpose is fetching."
)


def _generate_tool_source(name: str, purpose: str, signature: str,
                          client, model: str) -> str:
    user = (
        f"Write the tool module for a tool named `{name}`.\n"
        f"Purpose: {purpose}\n"
        f"Desired arguments / signature: {signature}\n\n"
        f"TOOL_DEFINITION['function']['name'] MUST be exactly \"{name}\"."
    )
    text = complete_text(client, model, _ENGINEER_SYSTEM, user, max_tokens=2200)
    # Strip a ```python fence if present.
    m = re.search(r"```(?:python)?\s*(.*?)```", text, re.DOTALL)
    return (m.group(1) if m else text).strip()


def _load_and_validate(name: str, path) -> tuple[object | None, str]:
    """Import the module file and validate its shape. Returns (module, err)."""
    try:
        spec = importlib.util.spec_from_file_location(f"lab_tool_{name}", str(path))
        mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    except Exception as e:
        return None, f"import failed: {e}"
    td = getattr(mod, "TOOL_DEFINITION", None)
    run = getattr(mod, "run", None)
    if not isinstance(td, dict) or td.get("function", {}).get("name") != name:
        return None, "TOOL_DEFINITION missing or its function.name != tool name"
    if not callable(run):
        return None, "module has no callable run(args, working_dir)"
    return mod, ""


# Common capabilities the agent ALREADY has — reject rebuilds, redirect to the
# existing tool. Hints are matched as whole phrases (word-boundary), so e.g.
# "vowels" never matches "ls".  (purpose-keyword -> existing tool name)
_REDUNDANT_HINTS = {
    "read_file": ("read file", "read a file", "open file", "load file"),
    "write_file": ("write file", "write a file", "save file", "create file"),
    "edit_file": ("edit file", "modify file", "replace in file"),
    "list_dir": ("list dir", "list directory", "list files"),
    "glob": ("find files", "glob", "match files"),
    "grep": ("search files", "grep", "search text in"),
    "bash": ("run shell", "shell command", "run command", "execute command",
             "run bash", "subprocess", "run a script"),
    "web_search": ("web search", "search the web"),
    "web_fetch": ("fetch url", "download url", "http get", "fetch a web"),
}


def _redundant_with(name: str, purpose: str) -> str | None:
    """Return the existing tool this request duplicates, or None."""
    existing = _tools.valid_tool_names() - _tools.dynamic_tool_names()
    if name in existing:
        return name
    pl = (purpose or "").lower()
    for tool, hints in _REDUNDANT_HINTS.items():
        if tool not in existing:
            continue
        for h in hints:
            if re.search(r"\b" + re.escape(h) + r"\b", pl):
                return tool
    return None


def build_tool(name: str, purpose: str, signature: str) -> tuple[str, bool]:
    """Generate, validate, and register a runtime tool. Returns (message, ok)."""
    client, model = _ctx("client"), _ctx("model")
    session = _ctx("session")
    if client is None or session is None:
        return "Foundry not active.", False
    if not _VALID_NAME.match(name or ""):
        return (f"Invalid tool name '{name}'. Use snake_case, 3-40 chars, "
                "starting with a letter."), False

    # Don't rebuild capabilities the agent already has.
    dup = _redundant_with(name, purpose)
    if dup:
        return (f"You already have the `{dup}` tool for that — use it directly "
                "instead of building a new tool."), False

    # Per-agent runaway guard.
    agent = _ctx("current_agent")
    counts = _ctx("tool_req_counts", None)
    if agent is not None and isinstance(counts, dict):
        n = counts.get(agent.id, 0)
        if n >= _MAX_TOOL_REQUESTS_PER_AGENT:
            return (f"Tool-build limit reached ({_MAX_TOOL_REQUESTS_PER_AGENT}) for "
                    "this session — use the tools you have, or ask the Director for "
                    "a teammate."), False
        counts[agent.id] = n + 1

    session.ensure_dirs()
    path = session.tools_dir / f"{name}.py"
    source = _generate_tool_source(name, purpose, signature, client, model)
    if not source or "def run" not in source:
        return "Tool engineer did not produce a usable module.", False
    try:
        path.write_text(source, encoding="utf-8")
    except Exception as e:
        return f"Could not write tool file: {e}", False

    mod, err = _load_and_validate(name, path)
    if err:
        return f"Built tool failed validation ({err}). Source kept at {path} for review.", False

    _tools.register_dynamic_tool(mod.TOOL_DEFINITION, mod.run)  # type: ignore[union-attr]
    _track(name)
    # Grant the new tool to the agent that built it so it becomes callable on the
    # next turn (agent_runtime re-resolves the offered tool set each iteration).
    if agent is not None and name not in agent.tools:
        agent.tools.append(name)
    session.add_artifact(f"lab/tools/{name}.py", "tool", "Foundry")
    _emit({"type": "tool_built", "name": name, "purpose": purpose,
           "path": f"lab/tools/{name}.py"})
    display.print_info(f"  🛠  Foundry built & registered tool: {name}")
    return (f"Tool '{name}' built, validated and registered — it is now in YOUR "
            f"toolset; call it directly on your next step. Source: lab/tools/{name}.py"), True


def load_registered_tools(session) -> int:
    """Re-register any previously built tools in lab/tools/ (used on resume)."""
    n = 0
    if not session.tools_dir.exists():
        return 0
    for path in sorted(session.tools_dir.glob("*.py")):
        name = path.stem
        mod, err = _load_and_validate(name, path)
        if not err and mod is not None:
            _tools.register_dynamic_tool(mod.TOOL_DEFINITION, mod.run)
            _track(name)
            n += 1
    return n


# ---------------------------------------------------------------------------
# Meta-tool implementations (signature: func(args, working_dir) -> (text, ok))
# ---------------------------------------------------------------------------

def _meta_request_tool(args: dict, working_dir: str) -> tuple[str, bool]:
    return build_tool(
        str(args.get("name", "")).strip(),
        str(args.get("purpose", "")).strip(),
        str(args.get("signature", "")).strip(),
    )


def _meta_request_agent(args: dict, working_dir: str) -> tuple[str, bool]:
    session = _ctx("session")
    if session is None:
        return "Foundry not active.", False
    name = str(args.get("name", "Specialist"))[:60]
    role = str(args.get("role", ""))[:120]

    # Per-requester runaway guard (mirror the tool-build cap).
    agent = _ctx("current_agent")
    counts = _ctx("agent_req_counts", None)
    if agent is not None and isinstance(counts, dict):
        if counts.get(agent.id, 0) >= _MAX_AGENT_REQUESTS_PER_AGENT:
            return (f"You've already requested {_MAX_AGENT_REQUESTS_PER_AGENT} "
                    "teammates this meeting — work with the team you have, or ask the "
                    "Director to revise it."), False

    # Don't let agents spawn duplicates of the built-in system roles.
    if _role_tokens(name, role) & _RESERVED_ROLE_WORDS:
        return ("A Critic and a Director already exist as permanent roles — every "
                "output is already reviewed by the Critic. Do not create a "
                "critic/reviewer/QA/director agent."), False

    # Don't spawn a near-duplicate of an existing teammate.
    dup = _similar_active_agent(session, name, role)
    if dup is not None:
        return (f"A teammate already covers that: '{dup.name}' ({dup.role}). Hand the "
                "work to them (or ask the Director to adjust their tools/scope) instead "
                "of creating a duplicate."), False

    if len(session.active_team) >= 10:
        return "Team is already at the maximum of 10 members; discard one first.", False

    tools = [str(t) for t in (args.get("tools") or []) if isinstance(t, str)]
    spec = AgentSpec(
        name=name, role=role,
        expertise=str(args.get("expertise", ""))[:400],
        goal=str(args.get("goal", ""))[:400],
        tools=tools,
        icon=str(args.get("icon", "🧪"))[:4] or "🧪",
        model=session.model,
    )
    # Grant the meta-tools to the new agent too (so it can also expand).
    for mt in META_TOOL_NAMES:
        if mt not in spec.tools:
            spec.tools.append(mt)
    session.team.append(spec)
    if agent is not None and isinstance(counts, dict):
        counts[agent.id] = counts.get(agent.id, 0) + 1
    session.touch()
    _emit({"type": "team_update", "team": [a.to_dict() for a in session.active_team]})
    display.print_info(f"  👥 Foundry added agent: {spec.name}")
    return (f"Agent '{spec.name}' ({spec.role}) added to the team; they will join "
            "the next meeting/round."), True


def _meta_request_mcp(args: dict, working_dir: str) -> tuple[str, bool]:
    server_id = str(args.get("server_id", "")).strip()
    values = args.get("values") or {}
    if not server_id:
        return "Provide a server_id from the MCP registry.", False
    try:
        from .. import mcp_registry
        from ..config import add_mcp_server
    except Exception as e:
        return f"MCP unavailable: {e}", False
    entry = mcp_registry.get_entry(server_id)
    if entry is None:
        ids = ", ".join(e["id"] for e in mcp_registry.list_entries()[:30])
        return f"Unknown MCP server '{server_id}'. Known ids include: {ids}", False
    # Required inputs without a default and not supplied → ask the human instead.
    missing = [r["key"] for r in mcp_registry.required_inputs(entry, working_dir)
               if not r["default"] and not values.get(r["key"])]
    if missing:
        return (f"Server '{server_id}' needs inputs {missing} (e.g. API keys). "
                "Ask the human to provide them, then retry."), False
    try:
        cfg = mcp_registry.build_config(entry, dict(values), working_dir)
        add_mcp_server(cfg)
        _tools.init_mcp(force=True)
    except Exception as e:
        return f"Failed to connect MCP server '{server_id}': {e}", False
    _emit({"type": "mcp_connected", "server_id": server_id})
    display.print_info(f"  🔌 Foundry connected MCP server: {server_id}")
    return (f"MCP server '{server_id}' connected. Its tools are now available "
            "(named mcp__{server}__<tool>)."), True


# ---------------------------------------------------------------------------
# Meta-tool schemas + enable
# ---------------------------------------------------------------------------

_META_DEFS = {
    "request_tool": {
        "type": "function",
        "function": {
            "name": "request_tool",
            "description": ("Build a brand-new tool at runtime when you need a "
                            "capability no existing tool provides. A tool engineer "
                            "writes, validates and registers it; you can then call "
                            "it immediately by its name."),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "snake_case tool name (3-40 chars)"},
                    "purpose": {"type": "string", "description": "What the tool does and why you need it."},
                    "signature": {"type": "string", "description": "The arguments the tool should accept and what they mean."},
                },
                "required": ["name", "purpose", "signature"],
            },
        },
    },
    "request_agent": {
        "type": "function",
        "function": {
            "name": "request_agent",
            "description": ("Add a new specialist to the team (max 10) when the work "
                            "needs expertise NO current member has. They join the next "
                            "meeting/round. Do NOT request a duplicate of an existing "
                            "teammate, and do NOT request a critic/reviewer/QA/director "
                            "(those are permanent built-in roles). GRANT the new agent "
                            "the tools it needs to actually do its job — anything that "
                            "runs code/commands needs `bash`; an execution agent with no "
                            "execution tools is useless. If a teammate just lacks a "
                            "tool, prefer asking the Director to adjust them over "
                            "spawning a new agent."),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "role": {"type": "string"},
                    "expertise": {"type": "string"},
                    "goal": {"type": "string"},
                    "tools": {"type": "array", "items": {"type": "string"},
                              "description": "Tool names to grant — include execution tools (e.g. bash) if the agent must run code."},
                    "icon": {"type": "string"},
                },
                "required": ["name", "role", "goal"],
            },
        },
    },
    "request_mcp": {
        "type": "function",
        "function": {
            "name": "request_mcp",
            "description": ("Connect a known MCP server from the registry at runtime "
                            "to gain its tools."),
            "parameters": {
                "type": "object",
                "properties": {
                    "server_id": {"type": "string", "description": "Registry id of the MCP server."},
                    "values": {"type": "object", "description": "Optional input values (API keys, paths)."},
                },
                "required": ["server_id"],
            },
        },
    },
}

_META_FUNCS = {
    "request_tool": _meta_request_tool,
    "request_agent": _meta_request_agent,
    "request_mcp": _meta_request_mcp,
}


def enable(session, client, model, emit=None) -> None:
    """Activate the foundry for a run: set context, register the meta-tools, and
    re-load any previously built tools."""
    set_context(client, model, session.working_dir, session, emit=emit)
    _CTX.registered = set()
    for n in META_TOOL_NAMES:
        _tools.register_dynamic_tool(_META_DEFS[n], _META_FUNCS[n])
        _track(n)
    loaded = load_registered_tools(session)
    if loaded:
        display.print_info(f"  🛠  Foundry re-loaded {loaded} previously built tool(s).")


def grant_meta_tools(spec: AgentSpec) -> None:
    """Give an agent the meta-tools so it can request capabilities at runtime."""
    for mt in META_TOOL_NAMES:
        if mt not in spec.tools:
            spec.tools.append(mt)
