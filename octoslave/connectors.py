"""Agent self-service for MCP connectors — find and connect external software.

Two tools, offered wherever the full toolbox is (chat, council, Science, Lab):

  find_connector(query)    — search what is already connected, the curated
                             catalog (``mcp_registry``) and the official MCP
                             Registry, and say for each one whether it can run
                             here and what it would need.
  connect_connector(id=…)  — connect one. Its tools join the caller's toolset
                             from its next step, and it is saved to the user's
                             MCP servers so later sessions have it too.

Trust model
-----------
* A curated-catalog server is connected on request — it is what the user could
  install with one click in settings (in ``controlled`` permission mode the user
  is still asked first).
* Anything else — an MCP Registry listing (community-published, not reviewed)
  or a command / URL the agent supplies itself — runs third-party code or sends
  data to someone's service, so it is connected only after the user approves.
  With nobody there to approve, it is not connected.
* Secrets (API keys, tokens) are not collected through the model: the agent is
  told to have the user add them in settings, where input is hidden.
* Registry packages are pinned to the version that was listed (and shown to the
  user when approving), not left to float on ``@latest``.

Connecting goes through ``MCPManager.add_server``, which starts only the new
server — the servers other agents are calling at that moment keep running.
"""

from __future__ import annotations

import json
import re
import shutil
import urllib.parse

from . import mcp_registry

REGISTRY_API = "https://registry.modelcontextprotocol.io/v0.1"
_REGISTRY_META = "io.modelcontextprotocol.registry/official"
_HTTP_HEADERS = {"User-Agent": "octoslave-mcp-client", "Accept": "application/json"}
# A first npx/uvx run downloads the package before the server can answer.
_CONNECT_TIMEOUT = 150.0
_SETTINGS_HINT = "Settings → Tools (MCP) in the web app, or `/mcp` in the terminal"

CONNECTOR_TOOL_NAMES = ("find_connector", "connect_connector")

# How a registry package is launched here: registryType → command.
_PACKAGE_RUNNERS = {"npm": "npx", "pypi": "uvx", "oci": "docker"}
_RUNTIME_HINTS = {
    "npx": "Node.js (npx): `brew install node` on macOS, or https://nodejs.org",
    "uvx": "uv (uvx): `brew install uv`, or `curl -LsSf https://astral.sh/uv/install.sh | sh`",
    "docker": "Docker: install Docker Desktop and start it",
}

# Words that never help find a server by name.
_STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "for", "to", "of", "in", "on", "with", "from",
    "my", "some", "using", "use", "via", "mcp", "server", "servers", "tool",
    "tools", "connector", "connectors", "make", "create", "build", "generate",
    "app", "software", "something", "that", "can",
})


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _have(command: str) -> bool:
    """Whether ``command`` resolves on the PATH MCP servers are launched with."""
    if not command:
        return False
    try:
        from .mcp_client import _enriched_path
        return shutil.which(command, path=_enriched_path()) is not None
    except Exception:  # noqa: BLE001
        return shutil.which(command) is not None


def _query_words(query: str) -> list[str]:
    words = re.findall(r"[a-z0-9][a-z0-9+.#-]*", (query or "").lower())
    out: list[str] = []
    for w in words:
        if len(w) >= 2 and w not in _STOPWORDS and w not in out:
            out.append(w)
    return out


def _one_line(text: str, limit: int = 110) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _coerce_values(raw) -> dict:
    """``values`` as a dict of strings. Models sometimes send it as a JSON
    string, or as a list of {key/name, value} pairs."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:  # noqa: BLE001
            return {}
    if isinstance(raw, list):
        out = {}
        for item in raw:
            if isinstance(item, dict):
                k = item.get("key") or item.get("name")
                if k and item.get("value") is not None:
                    out[str(k)] = str(item["value"])
        return out
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items() if v is not None and str(v) != ""}
    return {}


def _coerce_args(raw) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(a) for a in parsed]
        except Exception:  # noqa: BLE001
            pass
        import shlex
        try:
            return shlex.split(raw)
        except ValueError:
            return raw.split()
    if isinstance(raw, (list, tuple)):
        return [str(a) for a in raw]
    return []


def _fill(template: str, values: dict) -> tuple[str, list[str]]:
    """Substitute ``{name}`` placeholders; return (text, unresolved names)."""
    missing: list[str] = []

    def sub(m):
        key = m.group(1)
        if key in values:
            return values[key]
        missing.append(key)
        return m.group(0)

    return re.sub(r"\{([A-Za-z0-9_.-]+)\}", sub, template or ""), missing


def _secretish(name: str) -> bool:
    return bool(re.search(r"key|token|secret|password|passwd|auth|bearer|credential",
                          name or "", re.I))


# ---------------------------------------------------------------------------
# The official MCP Registry (registry.modelcontextprotocol.io)
# ---------------------------------------------------------------------------

def _http_json(url: str, params: dict | None = None, timeout: float = 15.0):
    import requests
    resp = requests.get(url, params=params, headers=_HTTP_HEADERS, timeout=timeout)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


def _active(item: dict) -> bool:
    meta = (item.get("_meta") or {}).get(_REGISTRY_META) or {}
    return meta.get("status", "active") == "active"


def registry_search(query: str, limit: int = 8) -> tuple[list[dict], str | None]:
    """Search the MCP Registry. Returns (server records, error or None).

    The registry matches the search string against server NAMES only, as a
    substring, so a phrase rarely hits: the hyphenated phrase is tried first
    (``google slides`` → ``google-slides``), then each word, and the union is
    ranked by how many of the words the name / title / description contain.
    """
    words = _query_words(query)
    if not words:
        return [], None
    terms = ([words[0]] if len(words) == 1 else ["-".join(words)] + words)[:4]
    found: dict[str, dict] = {}
    error = None
    for term in terms:
        try:
            data = _http_json(f"{REGISTRY_API}/servers",
                              {"search": term, "limit": 30, "version": "latest"})
        except Exception as e:  # noqa: BLE001
            error = str(e)
            continue
        for item in (data or {}).get("servers") or []:
            srv = item.get("server") or {}
            name = srv.get("name")
            if name and name not in found and _active(item):
                found[name] = srv

    def score(srv: dict) -> int:
        hay = " ".join(str(srv.get(k) or "") for k in ("name", "title", "description")).lower()
        return sum(1 for w in words if w in hay)

    ranked = sorted(found.values(), key=score, reverse=True)  # stable: keeps registry order
    return ranked[:limit], (error if not found else None)


def registry_lookup(name: str) -> tuple[dict | None, str | None]:
    """The latest active version of one registry server, by its exact name."""
    try:
        item = _http_json(f"{REGISTRY_API}/servers/"
                          f"{urllib.parse.quote(name, safe='')}/versions/latest")
    except Exception as e:  # noqa: BLE001
        return None, f"the MCP Registry could not be reached ({e})"
    if not item:
        return None, None
    if not _active(item):
        return None, "that listing is no longer active in the MCP Registry"
    return item.get("server") or item, None


# ---------------------------------------------------------------------------
# Launch plans — how a server would run here, and what it still needs
# ---------------------------------------------------------------------------

def _empty_plan() -> dict:
    return {"cfg": None, "where": "local", "label": "", "runtime": None,
            "need_secret": [], "need_value": [], "unsupported": None}


def _registry_local_plan(pkg: dict, runner: str, values: dict) -> dict:
    plan = _empty_plan()
    ident = str(pkg.get("identifier") or "")
    version = str(pkg.get("version") or "")
    plan["runtime"] = runner
    env: dict[str, str] = {}
    for var in pkg.get("environmentVariables") or []:
        key = var.get("name")
        if not key:
            continue
        val = values.get(key)
        if val is None and var.get("value"):
            val, unresolved = _fill(str(var["value"]), values)
            if unresolved:
                val = None
        if val is None and var.get("isRequired") and var.get("default"):
            val = str(var["default"])
        if val is None:
            if var.get("isRequired"):
                bucket = "need_secret" if (var.get("isSecret") or _secretish(key)) else "need_value"
                plan[bucket].append((key, _one_line(var.get("description") or "", 90)))
            continue
        if var.get("format") == "filepath":
            import os
            val = os.path.expanduser(val)
        env[key] = val

    args: list[str] = []
    for a in pkg.get("packageArguments") or []:
        key = a.get("name") or a.get("valueHint") or ""
        val = values.get(key) if key else None
        if val is None and a.get("value"):
            val, unresolved = _fill(str(a["value"]), values)
            if unresolved:
                val = None
        if val is None and a.get("default"):
            val = str(a["default"])
        if val is None:
            if a.get("isRequired"):
                bucket = "need_secret" if a.get("isSecret") else "need_value"
                plan[bucket].append((key or "argument",
                                     _one_line(a.get("description") or "", 90)))
            continue
        if a.get("type") == "named":
            flag = key if key.startswith("-") else f"--{key}"
            args += [flag, str(val)]
        else:
            args.append(str(val))

    if runner == "npx":
        spec = f"{ident}@{version}" if version else ident
        argv = ["-y", spec, *args]
        plan["label"] = f"npm package {spec}, run with npx"
    elif runner == "uvx":
        spec = f"{ident}@{version}" if version else ident
        argv = [spec, *args]
        plan["label"] = f"Python package {spec}, run with uvx"
    else:  # docker
        image = ident if (":" in ident.rsplit("/", 1)[-1] or not version) else f"{ident}:{version}"
        argv = ["run", "-i", "--rm"]
        for key in env:
            argv += ["-e", key]
        argv += [image, *args]
        plan["label"] = f"container image {image}, run with docker"
    cfg = {"command": runner, "args": argv, "enabled": True}
    if env:
        cfg["env"] = env
    plan["cfg"] = cfg
    return plan


def _registry_remote_plan(remote: dict, values: dict) -> dict:
    plan = _empty_plan()
    plan["where"] = "remote"
    url, unresolved = _fill(str(remote.get("url") or ""), values)
    for key in unresolved:
        plan["need_value"].append((key, "part of the service URL"))
    headers: dict[str, str] = {}
    for h in remote.get("headers") or []:
        hname = h.get("name")
        if not hname:
            continue
        val = values.get(hname)
        if val is None and h.get("value"):
            val, missing = _fill(str(h["value"]), values)
            if missing:
                if h.get("isRequired") or h.get("isSecret"):
                    for key in missing:
                        bucket = ("need_secret" if (h.get("isSecret") or _secretish(key)
                                                    or _secretish(hname)) else "need_value")
                        plan[bucket].append((key, _one_line(h.get("description") or hname, 90)))
                val = None
        if val is None:
            if h.get("isRequired") and not h.get("value"):
                bucket = "need_secret" if (h.get("isSecret") or _secretish(hname)) else "need_value"
                plan[bucket].append((hname, _one_line(h.get("description") or "", 90)))
            continue
        headers[hname] = val
    host = urllib.parse.urlparse(url).netloc or url
    plan["label"] = f"remote service at {host}"
    cfg = {"url": url, "enabled": True}
    if headers:
        cfg["headers"] = headers
    plan["cfg"] = cfg
    return plan


def registry_plan(srv: dict, values: dict | None = None) -> dict:
    """Pick how a registry server would run here. Prefers a local package whose
    runtime is installed (the user's data stays on this machine), then a remote
    Streamable-HTTP endpoint, then a local package whose runtime is missing."""
    values = values or {}
    locals_: list[tuple[dict, str]] = []
    reasons: list[str] = []
    for pkg in srv.get("packages") or []:
        transport = (pkg.get("transport") or {}).get("type") or "stdio"
        rtype = pkg.get("registryType")
        runner = _PACKAGE_RUNNERS.get(rtype or "")
        if transport != "stdio":
            reasons.append(f"its {rtype} package serves over {transport}, which needs a separately started server")
        elif not runner:
            reasons.append(f"it ships as a {rtype} package, which OctoSlave cannot launch")
        else:
            locals_.append((pkg, runner))
    remotes = [r for r in (srv.get("remotes") or []) if r.get("type") == "streamable-http"]
    if any(r.get("type") == "sse" for r in (srv.get("remotes") or [])) and not remotes:
        reasons.append("its remote endpoint uses the legacy SSE transport, which OctoSlave does not speak")

    for pkg, runner in locals_:
        if _have(runner):
            return _registry_local_plan(pkg, runner, values)
    if remotes:
        return _registry_remote_plan(remotes[0], values)
    if locals_:
        pkg, runner = locals_[0]
        return _registry_local_plan(pkg, runner, values)
    plan = _empty_plan()
    plan["unsupported"] = "; ".join(reasons) or "it lists no package or endpoint OctoSlave can run"
    return plan


def catalog_plan(entry: dict, values: dict | None, working_dir: str | None) -> dict:
    values = values or {}
    plan = _empty_plan()
    runtime = entry.get("runtime") or "stdio"
    plan["where"] = "remote" if entry.get("transport") == "http" else "local"
    plan["runtime"] = None if runtime == "http" else (entry.get("command") or runtime)
    merged: dict[str, str] = {}
    for spec in entry.get("inputs") or []:
        key = spec["key"]
        val = values.get(key)
        if not val and spec.get("default_wd") and working_dir:
            val = working_dir
        if not val and spec.get("default"):
            val = spec["default"]
        if not val:
            bucket = "need_secret" if spec.get("secret") else "need_value"
            plan[bucket].append((key, spec.get("prompt") or key))
            continue
        merged[key] = val
    if plan["where"] == "remote":
        plan["label"] = f"remote service at {urllib.parse.urlparse(entry.get('url', '')).netloc}"
    else:
        plan["label"] = " ".join([entry.get("command", ""), *entry.get("args", [])]).strip()
    if not plan["need_secret"] and not plan["need_value"]:
        cfg = mcp_registry.build_config(entry, merged, working_dir)
        cfg.pop("name", None)
        plan["cfg"] = cfg
    return plan


def _runtime_problem(plan: dict) -> str | None:
    rt = plan.get("runtime")
    if plan.get("where") == "local" and rt and not _have(rt):
        hint = _RUNTIME_HINTS.get(rt) or mcp_registry.runtime_hint(rt) or ""
        return f"it needs `{rt}`, which is not installed here" + (f" — {hint}" if hint else "")
    return None


# ---------------------------------------------------------------------------
# Naming + config bookkeeping
# ---------------------------------------------------------------------------

def _short_name(raw: str) -> str:
    """A short connector name from a registry name / package / host —
    ``io.github.someone/ffmpeg-mcp-server`` → ``ffmpeg``."""
    tail = (raw or "").strip().lower().rstrip("/").split("/")[-1]
    tail = tail.split("@")[0] if not tail.startswith("@") else tail.lstrip("@").split("@")[0]
    for prefix in ("mcp-server-", "mcp_server_", "server-", "mcp-", "mcp_"):
        if tail.startswith(prefix) and len(tail) > len(prefix) + 1:
            tail = tail[len(prefix):]
    for suffix in ("-mcp-server", "_mcp_server", "-server-mcp", "-mcp", "_mcp", "-server"):
        if tail.endswith(suffix) and len(tail) > len(suffix) + 1:
            tail = tail[:-len(suffix)]
    tail = re.sub(r"[^a-z0-9_-]+", "-", tail).strip("-_")
    return (tail or "connector")[:24]


def _custom_name(command: str, args: list[str], url: str) -> str:
    if url:
        host = urllib.parse.urlparse(url).netloc or url
        parts = [p for p in host.split(".") if p not in ("www", "mcp", "api")]
        return _short_name(parts[0] if parts else host)
    for a in args:
        if a and not a.startswith("-") and a not in ("run", "x", "dlx", "exec"):
            return _short_name(a)
    return _short_name(command.rsplit("/", 1)[-1])


def _configured():
    from .config import get_mcp_servers
    try:
        return get_mcp_servers()
    except Exception:  # noqa: BLE001
        return []


def _configured_entry(key: str) -> dict | None:
    """A saved server matching ``key`` by name or by the source it came from."""
    if not key:
        return None
    k = key.strip().lower()
    for s in _configured():
        if str(s.get("name", "")).lower() == k or str(s.get("source", "")).lower() == k:
            return s
    return None


def _unique_name(base: str) -> str:
    from .mcp_client import manager, sanitize_server_name
    taken = {str(s.get("name", "")).lower() for s in _configured()}
    taken |= {s.raw_name.lower() for s in list(manager.servers.values())}
    taken_ns = {sanitize_server_name(t).lower() for t in taken}
    name, n = base, 2
    while name.lower() in taken or sanitize_server_name(name).lower() in taken_ns:
        name, n = f"{base}-{n}", n + 1
    return name


# ---------------------------------------------------------------------------
# Granting the new tools to the agent that asked
# ---------------------------------------------------------------------------

def _grant(server_name: str, tool_names: list[str]) -> None:
    """Lab and Science specialists run with an allowlist; add the connector's
    tools to it so they are offered on the agent's very next step (the main
    chat loop re-reads every connected tool each turn and needs nothing)."""
    try:
        from .lab import foundry
        foundry.connector_connected(server_name, tool_names)
    except Exception:  # noqa: BLE001
        pass


def _tool_listing(server, limit: int = 40) -> str:
    lines = []
    for td in server.tools[:limit]:
        fn = td.get("function") or {}
        desc = _one_line((fn.get("description") or "").split("\n")[0], 100)
        lines.append(f"  - {fn.get('name')} — {desc}" if desc else f"  - {fn.get('name')}")
    more = len(server.tools) - limit
    if more > 0:
        lines.append(f"  … and {more} more (all named mcp__{server.name}__…)")
    return "\n".join(lines)


def _connected_reply(server, *, saved: bool, note: str = "") -> str:
    head = (f"Connected '{server.raw_name}' — {len(server.tools)} tool(s), callable "
            f"from your next step:")
    tail = []
    if saved:
        tail.append("It is saved to the user's MCP servers, so later sessions have it too.")
    tail.append(f"To hand it to a specialist, grant `mcp__{server.name}` (all its tools).")
    try:
        from .tools import _remote
        if _remote() is not None:
            tail.append("Note: this session works on a remote host, but the connector "
                        "runs on THIS computer — it cannot see files on the remote.")
    except Exception:  # noqa: BLE001
        pass
    if note:
        tail.append(note)
    return f"{head}\n{_tool_listing(server)}\n" + " ".join(tail)


# ---------------------------------------------------------------------------
# Approval
# ---------------------------------------------------------------------------

_YES = {"connect", "yes", "y", "ok", "okay", "sure", "allow", "approve", "approved", "go", "go ahead"}


def _ask_approval(title: str, source_line: str, plan: dict, reason: str) -> tuple[bool, str, bool]:
    """Ask the user. Returns (approved, their answer, answered)."""
    lines = [f"OctoSlave wants to connect the MCP connector “{title}”"
             + (f" — {reason.rstrip('.')}" if reason else "") + "."]
    lines.append(source_line)
    if plan["where"] == "remote":
        lines.append(f"It is a {plan['label']}: whatever the agent sends to its tools "
                     "leaves this computer.")
    else:
        lines.append(f"It runs on this computer ({plan['label']}) and can do whatever "
                     "its tools are written to do with your files and network.")
    lines.append("It will be saved to your MCP servers for later sessions "
                 "(remove it any time in Settings → Tools (MCP)).")
    try:
        from . import display
        answer, answered = display.ask_user_question("\n".join(lines),
                                                     ["Connect", "Don't connect"])
    except Exception:  # noqa: BLE001
        return False, "", False
    if not answered:
        return False, "", False
    a = (answer or "").strip().lower().rstrip(".!")
    approved = a in _YES or (a.startswith("connect") and not a.startswith("connect not"))
    return approved, answer, True


# ---------------------------------------------------------------------------
# Tool: find_connector
# ---------------------------------------------------------------------------

def _catalog_matches(words: list[str]) -> list[tuple[int, dict]]:
    scored = []
    for e in mcp_registry.list_entries():
        hay = " ".join([e["id"], e.get("name", ""), e.get("summary", ""),
                        e.get("category", "")]).lower()
        s = sum(1 for w in words if w in hay)
        if s:
            scored.append((s, e))
    scored.sort(key=lambda t: -t[0])
    return scored


def _connected_matches(words: list[str]) -> list:
    from .mcp_client import manager
    out = []
    for server in list(manager.servers.values()):
        if not server.connected:
            continue
        hay = " ".join([server.raw_name] + [
            f"{(td.get('function') or {}).get('name', '')} "
            f"{(td.get('function') or {}).get('description', '')}" for td in server.tools
        ]).lower()
        if not words or any(w in hay for w in words):
            out.append(server)
    return out


def find_connector(query: str = "", working_dir: str = "", limit: int = 8, **_ignored) -> tuple[str, bool]:
    """Search connected servers, the curated catalog and the MCP Registry."""
    from . import tools as _tools
    _tools.init_mcp(working_dir=working_dir or None)
    words = _query_words(query)
    try:
        limit = max(1, min(int(limit or 8), 15))
    except (TypeError, ValueError):
        limit = 8
    out: list[str] = [f"Connectors for “{query.strip()}”:" if query.strip() else "Connectors:"]

    live = _connected_matches(words)
    if live:
        out.append("\nAlready connected (call their tools directly):")
        for s in live:
            out.append(f"  • {s.raw_name} — {len(s.tools)} tool(s), named mcp__{s.name}__…")

    cat = _catalog_matches(words) if words else [(0, e) for e in mcp_registry.list_entries()]
    if cat:
        out.append("\nCurated catalog (vetted; connect with connect_connector(id=…)):")
        configured = {str(s.get("name", "")).lower() for s in _configured()}
        for _, e in cat[:limit]:
            plan = catalog_plan(e, {}, working_dir)
            if e["id"].lower() in configured:
                state = "already saved in settings"
            elif _runtime_problem(plan):
                state = f"needs {plan['runtime']}"
            elif plan["need_secret"]:
                state = "needs an API key the user must add"
            elif plan["need_value"]:
                state = "needs: " + ", ".join(k for k, _ in plan["need_value"])
            else:
                state = "ready"
            out.append(f"  • {e['id']} — {_one_line(e.get('summary', ''), 95)} [{state}]")

    if words:
        reg, err = registry_search(query, limit=limit)
        if reg:
            out.append("\nOfficial MCP Registry (community-published, NOT reviewed — "
                       "connecting asks the user first):")
            for srv in reg:
                plan = registry_plan(srv)
                if plan["unsupported"]:
                    state = f"cannot run here: {plan['unsupported']}"
                else:
                    where = "remote service — data leaves this computer" if plan["where"] == "remote" else "local"
                    bits = [where, plan["label"]]
                    if _runtime_problem(plan):
                        bits.append(f"needs {plan['runtime']}")
                    if plan["need_secret"]:
                        bits.append("needs an API key (" + ", ".join(k for k, _ in plan["need_secret"]) + ")")
                    if plan["need_value"]:
                        bits.append("needs: " + ", ".join(k for k, _ in plan["need_value"]))
                    state = "; ".join(b for b in bits if b)
                desc = _one_line(srv.get("description") or srv.get("title") or "", 95)
                out.append(f"  • {srv.get('name')} — {desc} [{state}]")
        elif err:
            out.append(f"\n(The MCP Registry could not be searched: {_one_line(err, 120)})")
        else:
            out.append("\nOfficial MCP Registry: no match. It searches server names, so "
                       "try a single distinctive word (e.g. the app or file format).")

    if len(out) == 1:
        out.append("Nothing matched. Try one distinctive word, such as the application or file format.")
    out.append(
        "\nBefore connecting anything, check whether software already installed here does "
        "the job (`command -v <tool>`, an importable library) — call it with bash. A "
        "connector earns its place when it drives an application (a browser, a 3-D or "
        "office app, a game engine) or a service on the user's account. Prefer a curated "
        "entry, then a local registry package from a recognisable publisher; a remote "
        "service receives whatever you send it.")
    return "\n".join(out), True


# ---------------------------------------------------------------------------
# Tool: connect_connector
# ---------------------------------------------------------------------------

def _need_secret_reply(title: str, keys: list[tuple[str, str]], web_how: str, cli_how: str) -> str:
    listed = "; ".join(f"{k}" + (f" ({d})" if d else "") for k, d in keys)
    return (f"'{title}' needs a secret only the user can provide: {listed}. Do NOT ask "
            f"them to paste it into the chat. Ask them to add it in Settings → Tools "
            f"(MCP) in the web app ({web_how}), or with {cli_how} in the terminal, where "
            f"the key is typed hidden. Once added it connects, and its tools appear in "
            f"your toolset as mcp__<name>__… — if not, call connect_connector with the "
            f"name they gave it.")


def connect_connector(working_dir: str = "", permission_mode: str = "autonomous",
                      id: str = "", command: str = "", args=None, url: str = "",
                      values=None, name: str = "", reason: str = "",
                      **extra) -> tuple[str, bool]:
    """Connect a connector (see module docstring for the trust model)."""
    from . import display
    from . import tools as _tools
    from .mcp_client import manager

    sid = str(id or extra.get("server_id") or extra.get("server") or "").strip()
    command = str(command or "").strip()
    url = str(url or "").strip()
    argv = _coerce_args(args)
    vals = _coerce_values(values)
    reason = str(reason or "").strip()
    if not sid and not command and not url:
        return ("Say which connector: `id` (a catalog id or an MCP Registry name from "
                "find_connector), or `command`+`args` / `url` for one you know.", False)

    _tools.init_mcp(working_dir=working_dir or None)

    # 1. Already there? Live → hand it over; saved but down → reconnect it.
    if sid:
        live = manager.find_server(sid)
        saved = _configured_entry(sid)
        if live is None and saved is not None:
            live = manager.find_server(saved["name"])
        if live is not None and live.connected:
            _grant(live.name, list(live._tool_index))
            return _connected_reply(live, saved=False, note="(It was already connected.)"), True
        if saved is not None:
            if not saved.get("enabled", True):
                return (f"'{saved['name']}' is saved but the user has turned it off. Ask "
                        f"them to enable it in {_SETTINGS_HINT} if you need it.", False)
            server = manager.add_server(saved, timeout=_CONNECT_TIMEOUT)
            if server.connected:
                _grant(server.name, list(server._tool_index))
                return _connected_reply(server, saved=False), True
            return (f"'{saved['name']}' is saved in settings but did not start: "
                    f"{server.error}", False)

    # 2. Work out what it is and how it would run.
    entry = mcp_registry.get_entry(sid) if sid else None
    if entry is not None:
        kind, title = "catalog", entry["name"]
        plan = catalog_plan(entry, vals, working_dir)
        base_name, source = entry["id"], entry["id"]
        source_line = "Source: OctoSlave's curated catalog."
        secret_how = ("its entry in the catalog list", f"`/mcp install {entry['id']}`")
    elif sid and not command and not url:
        if "/" not in sid:
            return (f"No connector '{sid}' in the curated catalog. Run find_connector "
                    "to search the catalog and the MCP Registry, then pass the exact "
                    "id or registry name (registry names look like io.github.owner/name).",
                    False)
        srv, err = registry_lookup(sid)
        if srv is None:
            return (f"Could not use '{sid}' from the MCP Registry: "
                    f"{err or 'no such server'}. Check the exact name with find_connector.",
                    False)
        kind, title = "registry", srv.get("title") or srv.get("name") or sid
        plan = registry_plan(srv, vals)
        base_name, source = _short_name(srv.get("name") or sid), srv.get("name") or sid
        repo = ((srv.get("repository") or {}).get("url") or srv.get("websiteUrl") or "")
        source_line = (f"Source: the official MCP Registry ({source}) — published by its "
                       "author, not reviewed by OctoSlave." + (f" Code: {repo}" if repo else ""))
        secret_how = ("Add server — the key goes in as an environment variable or "
                      "header", "`/mcp add`")
    else:
        kind, title = "custom", name or sid or (url or " ".join([command, *argv]))
        plan = _empty_plan()
        if url:
            if not url.startswith(("https://", "http://localhost", "http://127.0.0.1")):
                return "A connector URL must be https:// (or a localhost address).", False
            plan.update(where="remote", label=f"remote service at {urllib.parse.urlparse(url).netloc}",
                        cfg={"url": url, "enabled": True})
        else:
            plan.update(runtime=command, label=" ".join([command, *argv]),
                        cfg={"command": command, "args": argv, "enabled": True})
            env = {k: v for k, v in vals.items() if re.fullmatch(r"[A-Z][A-Z0-9_]*", k)}
            if env:
                plan["cfg"]["env"] = env
        base_name = _short_name(name) if name else _custom_name(command, argv, url)
        source = sid or ""
        source_line = ("Source: supplied by the agent itself — not from the curated "
                       "catalog or the MCP Registry.")
        secret_how = ("Add server", "`/mcp add`")

    if plan["unsupported"]:
        return f"'{title}' cannot run here: {plan['unsupported']}. Pick another from find_connector.", False
    if plan["need_secret"]:
        return _need_secret_reply(title, plan["need_secret"], *secret_how), False
    if plan["need_value"]:
        listed = "; ".join(f"{k}" + (f" ({d})" if d else "") for k, d in plan["need_value"])
        return (f"'{title}' needs these settings: {listed}. Pass them as "
                f"values={{\"<name>\": \"<value>\"}} and call connect_connector again.", False)
    problem = _runtime_problem(plan)
    if problem:
        return (f"'{title}' can't start yet: {problem}. Install it (with the user's OK "
                "if that is not routine here), then call connect_connector again.", False)

    # 3. Approval: everything outside the curated catalog, and everything in
    #    controlled mode.
    if kind != "catalog" or permission_mode == "controlled":
        approved, answer, answered = _ask_approval(title, source_line, plan, reason)
        if not answered:
            return (f"Not connected: '{title}' needs the user's approval and no answer "
                    "came back (they may be away, or this run is non-interactive). Carry "
                    "on with the tools you have, and say in your report which connector "
                    "would help and why, so they can add it.", False)
        if not approved:
            return (f"Not connected — the user declined '{title}' (they said: "
                    f"{_one_line(answer, 160)}). Do not ask again; carry on without it.", False)

    # 4. Connect just this one, then save it.
    cfg = dict(plan["cfg"])
    cfg["name"] = _unique_name(base_name)
    if source:
        cfg["source"] = source
    display.print_info(f"  🔌 Connecting connector '{cfg['name']}' ({plan['label']})…")
    server = manager.add_server(cfg, timeout=_CONNECT_TIMEOUT)
    if not server.connected:
        return (f"'{title}' did not start: {server.error}. Nothing was saved. If the "
                "message points at a missing program or package, fix that and retry; "
                "otherwise try another connector.", False)
    saved_ok = True
    try:
        from .config import add_mcp_server
        add_mcp_server(cfg)
    except Exception as e:  # noqa: BLE001 — connected anyway; saving is a bonus
        saved_ok = False
        display.print_info(f"  (could not save connector '{cfg['name']}' to settings: {e})")
    _grant(server.name, list(server._tool_index))
    display.print_info(f"  🔌 Connected '{server.raw_name}' — {len(server.tools)} tool(s).")
    return _connected_reply(server, saved=saved_ok), True


# ---------------------------------------------------------------------------
# Tool schemas (flat — no anyOf; see tools.py)
# ---------------------------------------------------------------------------

CONNECTOR_TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "find_connector",
            "description": (
                "Find an MCP connector — external software exposed as tools — for a job "
                "your built-in tools do not cover well: driving an application (a "
                "browser, a 3-D, design or office app, a game engine), a service on the "
                "user's account (GitHub, Notion, Google Drive, Slack), or a specialised "
                "model or database. Searches what is already connected, OctoSlave's "
                "curated catalog and the official MCP Registry, and says whether each "
                "can run here and what it needs. Then use connect_connector. Check first "
                "whether a program or library already installed here does the job — "
                "then just use it with bash."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "What you need, in a few words — the application, service or file format works best (e.g. 'blender', 'powerpoint', 'notion')."},
                    "limit": {"type": "integer", "description": "Max results per source (default 8)."},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "connect_connector",
            "description": (
                "Connect an MCP connector so its tools (named mcp__<connector>__<tool>) "
                "are yours from your next step; it is also saved for later sessions. "
                "Pass `id`: a curated catalog id or an MCP Registry name from "
                "find_connector (or the name of one already saved). Curated ones connect "
                "directly; anything else asks the user to approve first. For a "
                "connector you know that is in neither, pass `command`+`args` (or `url`) "
                "instead — the user is asked too. Never ask the user to paste an API key "
                "into the chat: if one is needed, this tells you how they add it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Catalog id (e.g. 'playwright') or MCP Registry name (e.g. 'io.github.owner/name')."},
                    "reason": {"type": "string", "description": "One line on what you need it for — shown to the user when they are asked to approve."},
                    "values": {"type": "object", "description": "Non-secret settings it asks for, e.g. {\"path\": \"/data/project\"}."},
                    "command": {"type": "string", "description": "Only for a connector outside the catalog and registry: the executable (e.g. 'uvx', 'npx')."},
                    "args": {"type": "array", "items": {"type": "string"}, "description": "Arguments for `command`."},
                    "url": {"type": "string", "description": "Only for a remote connector outside the catalog and registry: its https Streamable-HTTP endpoint."},
                    "name": {"type": "string", "description": "Optional short name for a `command`/`url` connector."},
                },
                "required": [],
            },
        },
    },
]
