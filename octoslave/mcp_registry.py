"""Curated catalog of well-known MCP servers — octoslave's "app store".

Lets users install competitive, high-value external toolsets in one command
(``/mcp install <id>``) instead of memorising package names, transports, and
env-var conventions. Each entry is a template: the only things the user must
supply are the bits that are genuinely theirs — a directory to expose, a DB
path, or an API token.

The registry is intentionally curated for currently-maintained servers. Entries
declare a ``runtime`` (npx / uvx / docker / http) so the UI can tell the user up
front whether they have what's needed to run it.

This module is pure data + helpers — no I/O, no prompting. The TUI layer
(``main.py``) collects user input and calls :func:`build_config`, which returns
a dict ready for :func:`octoslave.config.add_mcp_server`.
"""

import shutil


# ---------------------------------------------------------------------------
# Input descriptors
# ---------------------------------------------------------------------------
# An entry may need user-supplied values. Two flavours:
#   * params  — substituted into command args via "{key}" placeholders
#   * secrets — sensitive values injected as an env var or an HTTP header
#
# Each descriptor: {"key", "prompt", "default"?, "default_wd"?, "secret"?,
#                   "as": "arg"|"env"|"header", "header"?, "format"?}


REGISTRY: list[dict] = [
    # -- Files & code ------------------------------------------------------
    {
        "id": "filesystem",
        "name": "Filesystem",
        "category": "Files & code",
        "summary": "Sandboxed read/write/search/move over one or more allowed directories.",
        "transport": "stdio",
        "runtime": "npx",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "{path}"],
        "inputs": [
            {"key": "path", "prompt": "Directory to expose", "default_wd": True, "as": "arg"},
        ],
        "homepage": "https://github.com/modelcontextprotocol/servers/tree/main/src/filesystem",
    },
    {
        "id": "git",
        "name": "Git",
        "category": "Files & code",
        "summary": "Inspect and operate a local git repo — status, diff, log, blame, commit.",
        "transport": "stdio",
        "runtime": "uvx",
        "command": "uvx",
        "args": ["mcp-server-git", "--repository", "{path}"],
        "inputs": [
            {"key": "path", "prompt": "Repository path", "default_wd": True, "as": "arg"},
        ],
        "homepage": "https://github.com/modelcontextprotocol/servers/tree/main/src/git",
    },
    {
        "id": "github",
        "name": "GitHub",
        "category": "Files & code",
        "summary": "Official GitHub remote server — issues, PRs, code search, Actions, releases.",
        "transport": "http",
        "runtime": "http",
        "url": "https://api.githubcopilot.com/mcp/",
        "inputs": [
            {
                "key": "token",
                "prompt": "GitHub Personal Access Token (repo scope)",
                "secret": True,
                "as": "header",
                "header": "Authorization",
                "format": "Bearer {value}",
            },
        ],
        "homepage": "https://github.com/github/github-mcp-server",
    },
    {
        "id": "context7",
        "name": "Context7 (live docs)",
        "category": "Files & code",
        "summary": "Pulls up-to-date, version-correct docs & code examples for any library.",
        "transport": "stdio",
        "runtime": "npx",
        "command": "npx",
        "args": ["-y", "@upstash/context7-mcp"],
        "inputs": [],
        "homepage": "https://github.com/upstash/context7",
    },

    # -- Web & browser -----------------------------------------------------
    {
        "id": "fetch",
        "name": "Fetch",
        "category": "Web & browser",
        "summary": "Fetch a URL and return clean markdown — lighter than a full browser.",
        "transport": "stdio",
        "runtime": "uvx",
        "command": "uvx",
        "args": ["mcp-server-fetch"],
        "inputs": [],
        "homepage": "https://github.com/modelcontextprotocol/servers/tree/main/src/fetch",
    },
    {
        "id": "playwright",
        "name": "Playwright (browser)",
        "category": "Web & browser",
        "summary": "Drive a real browser — click, type, navigate, snapshot, fill forms.",
        "transport": "stdio",
        "runtime": "npx",
        "command": "npx",
        "args": ["-y", "@playwright/mcp@latest"],
        "inputs": [],
        "homepage": "https://github.com/microsoft/playwright-mcp",
    },
    {
        "id": "brave-search",
        "name": "Brave Search",
        "category": "Web & browser",
        "summary": "Web & local search via the Brave Search API (independent index).",
        "transport": "stdio",
        "runtime": "npx",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-brave-search"],
        "inputs": [
            {"key": "BRAVE_API_KEY", "prompt": "Brave Search API key", "secret": True, "as": "env"},
        ],
        "homepage": "https://github.com/modelcontextprotocol/servers/tree/main/src/brave-search",
    },
    {
        "id": "tavily",
        "name": "Tavily (AI search)",
        "category": "Web & browser",
        "summary": "Search + extract optimised for LLMs — concise, sourced answers.",
        "transport": "stdio",
        "runtime": "npx",
        "command": "npx",
        "args": ["-y", "tavily-mcp@latest"],
        "inputs": [
            {"key": "TAVILY_API_KEY", "prompt": "Tavily API key (tvly-…)", "secret": True, "as": "env"},
        ],
        "homepage": "https://github.com/tavily-ai/tavily-mcp",
    },
    {
        "id": "puppeteer",
        "name": "Puppeteer (browser)",
        "category": "Web & browser",
        "summary": "Headless Chrome automation — navigate, click, screenshot, scrape.",
        "transport": "stdio",
        "runtime": "npx",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-puppeteer"],
        "inputs": [],
        "homepage": "https://github.com/modelcontextprotocol/servers-archived/tree/main/src/puppeteer",
    },

    # -- Data & databases --------------------------------------------------
    {
        "id": "sqlite",
        "name": "SQLite",
        "category": "Data & databases",
        "summary": "Query and explore a local SQLite database; safe read + write tools.",
        "transport": "stdio",
        "runtime": "uvx",
        "command": "uvx",
        "args": ["mcp-server-sqlite", "--db-path", "{path}"],
        "inputs": [
            {"key": "path", "prompt": "Path to .sqlite/.db file", "as": "arg"},
        ],
        "homepage": "https://github.com/modelcontextprotocol/servers-archived/tree/main/src/sqlite",
    },
    {
        "id": "postgres",
        "name": "PostgreSQL",
        "category": "Data & databases",
        "summary": "Read-only schema inspection and SQL queries against a Postgres database.",
        "transport": "stdio",
        "runtime": "npx",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-postgres", "{conn}"],
        "inputs": [
            {"key": "conn", "prompt": "Connection URL (postgresql://user:pass@host/db)", "as": "arg"},
        ],
        "homepage": "https://github.com/modelcontextprotocol/servers-archived/tree/main/src/postgres",
    },

    # -- Reasoning & memory ------------------------------------------------
    {
        "id": "memory",
        "name": "Memory (knowledge graph)",
        "category": "Reasoning & memory",
        "summary": "Persistent knowledge-graph memory across sessions — entities & relations.",
        "transport": "stdio",
        "runtime": "npx",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-memory"],
        "inputs": [],
        "homepage": "https://github.com/modelcontextprotocol/servers/tree/main/src/memory",
    },
    {
        "id": "sequential-thinking",
        "name": "Sequential Thinking",
        "category": "Reasoning & memory",
        "summary": "A scratchpad tool for structured, revisable step-by-step reasoning.",
        "transport": "stdio",
        "runtime": "npx",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-sequential-thinking"],
        "inputs": [],
        "homepage": "https://github.com/modelcontextprotocol/servers/tree/main/src/sequentialthinking",
    },
    {
        "id": "time",
        "name": "Time & timezones",
        "category": "Reasoning & memory",
        "summary": "Current time and timezone conversions — grounds the model in real time.",
        "transport": "stdio",
        "runtime": "uvx",
        "command": "uvx",
        "args": ["mcp-server-time"],
        "inputs": [],
        "homepage": "https://github.com/modelcontextprotocol/servers/tree/main/src/time",
    },

    # -- Productivity & cloud ----------------------------------------------
    {
        "id": "slack",
        "name": "Slack",
        "category": "Productivity & cloud",
        "summary": "Read & post Slack messages, list channels, fetch threads.",
        "transport": "stdio",
        "runtime": "npx",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-slack"],
        "inputs": [
            {"key": "SLACK_BOT_TOKEN", "prompt": "Slack bot token (xoxb-…)", "secret": True, "as": "env"},
            {"key": "SLACK_TEAM_ID", "prompt": "Slack team id (T…)", "as": "env"},
        ],
        "homepage": "https://github.com/modelcontextprotocol/servers-archived/tree/main/src/slack",
    },
    {
        "id": "notion",
        "name": "Notion",
        "category": "Productivity & cloud",
        "summary": "Official Notion server — query & update pages and databases.",
        "transport": "stdio",
        "runtime": "npx",
        "command": "npx",
        "args": ["-y", "@notionhq/notion-mcp-server"],
        "inputs": [
            {"key": "NOTION_TOKEN", "prompt": "Notion integration token (ntn_… / secret_…)", "secret": True, "as": "env"},
        ],
        "homepage": "https://github.com/makenotion/notion-mcp-server",
    },
    {
        "id": "google-drive",
        "name": "Google Drive",
        "category": "Productivity & cloud",
        "summary": "Search and read files from Google Drive (needs OAuth setup).",
        "transport": "stdio",
        "runtime": "npx",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-gdrive"],
        "inputs": [
            {"key": "GDRIVE_CREDENTIALS_PATH", "prompt": "Path to OAuth credentials .json", "as": "env"},
        ],
        "homepage": "https://github.com/modelcontextprotocol/servers-archived/tree/main/src/gdrive",
    },
    {
        "id": "sentry",
        "name": "Sentry",
        "category": "Productivity & cloud",
        "summary": "Official Sentry remote server — inspect issues, errors, and traces.",
        "transport": "http",
        "runtime": "http",
        "url": "https://mcp.sentry.dev/mcp",
        "inputs": [],
        "homepage": "https://docs.sentry.io/product/sentry-mcp/",
    },
    {
        "id": "aws-docs",
        "name": "AWS Documentation",
        "category": "Productivity & cloud",
        "summary": "Search and read official AWS docs — services, APIs, best practices.",
        "transport": "stdio",
        "runtime": "uvx",
        "command": "uvx",
        "args": ["awslabs.aws-documentation-mcp-server@latest"],
        "inputs": [],
        "homepage": "https://github.com/awslabs/mcp",
    },
    {
        "id": "e2b",
        "name": "E2B (code sandbox)",
        "category": "Productivity & cloud",
        "summary": "Run code in a secure remote cloud sandbox — isolated from your machine.",
        "transport": "stdio",
        "runtime": "npx",
        "command": "npx",
        "args": ["-y", "@e2b/mcp-server"],
        "inputs": [
            {"key": "E2B_API_KEY", "prompt": "E2B API key (e2b_…)", "secret": True, "as": "env"},
        ],
        "homepage": "https://github.com/e2b-dev/mcp-server",
    },
    # -- Observability ----------------------------------------------------
    {
        "id": "codag",
        "name": "Codag (log compression)",
        "category": "Observability",
        "summary": (
            "Tail and compress logs from kubectl / docker / aws / vercel / gh-actions and "
            "arbitrary CLI log sources — typically 95–99% token reduction with errors preserved."
        ),
        "transport": "stdio",
        "runtime": "codag",
        "command": "codag",
        "args": ["mcp", "serve"],
        "inputs": [],
        # Each tail_* tool shells out to a provider CLI. If that CLI isn't on
        # PATH the tool can only ever fail ("executable file not found"), so we
        # prune it at load time. Self-healing: install the CLI and the tool
        # reappears on the next reconnect. health/compact/wrap have no single
        # hard dependency (wrap validates its own allowlist) and are kept.
        "tool_deps": {
            "tail_docker": "docker",
            "tail_kubernetes": "kubectl",
            "tail_aws_logs": "aws",
            "tail_gh_actions": "gh",
            "tail_vercel": "vercel",
        },
        "homepage": "https://codag.ai",
    },
]


_REGISTRY_INDEX = {e["id"]: e for e in REGISTRY}


# ---------------------------------------------------------------------------
# Lookup helpers
# ---------------------------------------------------------------------------

def get_entry(entry_id: str) -> dict | None:
    return _REGISTRY_INDEX.get((entry_id or "").strip().lower())


def list_entries() -> list[dict]:
    return list(REGISTRY)


def by_category() -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for e in REGISTRY:
        out.setdefault(e.get("category", "Other"), []).append(e)
    return out


# ---------------------------------------------------------------------------
# Runtime availability
# ---------------------------------------------------------------------------

_RUNTIME_HINTS = {
    "npx": "Node.js / npx — install Node 18+ (https://nodejs.org) or `brew install node`.",
    "uvx": "uv / uvx — install with `curl -LsSf https://astral.sh/uv/install.sh | sh`.",
    "docker": "Docker — install Docker Desktop or the docker CLI.",
    "codag": "codag CLI — install with `curl -fsSL https://codag.ai/install.sh | sh`.",
    "http": "",  # remote, nothing to install
}


def runtime_available(runtime: str) -> bool:
    if runtime in ("http", "", None):
        return True
    return shutil.which(runtime) is not None


def runtime_hint(runtime: str) -> str:
    return _RUNTIME_HINTS.get(runtime, "")


def unsupported_tools(server_id: str) -> dict[str, str]:
    """Tools from a known registry server whose required CLI is missing.

    Returns a mapping ``{tool_name: missing_binary}`` for every tool the entry
    declares in ``tool_deps`` whose binary is not on PATH. Servers not in the
    registry (or with no ``tool_deps``) yield an empty mapping, so this is a
    no-op for user-defined servers. Used to prune dead tools at MCP load time.
    """
    entry = get_entry(server_id)
    if not entry:
        return {}
    deps = entry.get("tool_deps") or {}
    return {tool: binary for tool, binary in deps.items()
            if shutil.which(binary) is None}


# ---------------------------------------------------------------------------
# Building a concrete server config from a registry entry
# ---------------------------------------------------------------------------

def required_inputs(entry: dict, working_dir: str | None = None) -> list[dict]:
    """Return the user-supplied values this entry needs, with resolved defaults.

    Each item: {"key", "prompt", "default", "secret"} — ``main.py`` prompts for
    these (hiding input when ``secret``) or accepts them as inline key=value.
    """
    out: list[dict] = []
    for spec in entry.get("inputs", []):
        default = spec.get("default", "")
        if spec.get("default_wd") and working_dir:
            default = working_dir
        out.append({
            "key": spec["key"],
            "prompt": spec.get("prompt", spec["key"]),
            "default": default,
            "secret": bool(spec.get("secret")),
        })
    return out


def build_config(
    entry: dict,
    values: dict,
    working_dir: str | None = None,
    name: str | None = None,
) -> dict:
    """Turn a registry entry + resolved user values into an mcp_servers config
    dict ready for ``config.add_mcp_server``.

    ``values`` maps each input ``key`` to its string value.
    """
    server_name = (name or entry["id"]).strip()
    transport = entry.get("transport", "stdio")

    # Resolve a default working-dir for any unspecified default_wd args.
    def _val(spec: dict) -> str:
        v = values.get(spec["key"])
        if v is None or v == "":
            if spec.get("default_wd") and working_dir:
                return working_dir
            return spec.get("default", "")
        return v

    specs = {s["key"]: s for s in entry.get("inputs", [])}

    if transport == "http":
        cfg: dict = {"name": server_name, "url": entry["url"], "enabled": True}
        headers = dict(entry.get("headers") or {})
        for spec in entry.get("inputs", []):
            if spec.get("as") == "header":
                raw = _val(spec)
                fmt = spec.get("format", "{value}")
                headers[spec.get("header", "Authorization")] = fmt.format(value=raw)
        if headers:
            cfg["headers"] = headers
        return cfg

    # stdio
    args: list[str] = []
    for tok in entry.get("args", []):
        if isinstance(tok, str) and "{" in tok and "}" in tok:
            # Substitute every {key} placeholder from values/defaults.
            rendered = tok
            for key, spec in specs.items():
                placeholder = "{" + key + "}"
                if placeholder in rendered:
                    rendered = rendered.replace(placeholder, _val(spec))
            args.append(rendered)
        else:
            args.append(tok)

    env = dict(entry.get("env") or {})
    for spec in entry.get("inputs", []):
        if spec.get("as") == "env":
            env[spec["key"]] = _val(spec)

    cfg = {"name": server_name, "command": entry["command"], "args": args, "enabled": True}
    if env:
        cfg["env"] = env
    return cfg
