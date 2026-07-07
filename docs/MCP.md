# MCP — connecting external tools

> Plug external tools into OctoSlave via the
> [Model Context Protocol](https://modelcontextprotocol.io): git, GitHub, a real
> browser, databases, live docs, your own scripts. Installed tools sit right next
> to the built-in ones — in chat, research, the Lab and the web UI — namespaced
> `mcp__<server>__<tool>`.

The MCP client is **built in** (no extra dependency) and speaks both transports:

- **stdio** — OctoSlave launches a local subprocess (`npx …`, `uvx …`, `docker …`,
  or any command) and talks to it over stdin/stdout.
- **http** — OctoSlave talks to a remote URL (optionally with auth headers).

Servers are stored under `mcp_servers` in `~/.octoslave/config.json` and are
**shared between the TUI and the web UI** — add a server once, use it everywhere.

> **Pre-installed:** the installers (`install.sh` and the macOS / Windows /
> Linux GUI installers) auto-register the **Filesystem** server (sandboxed
> read/write/search under your home directory), installing Node.js for `npx`
> first when it's missing — plus **codag** when the codag CLI is installed.
> If Node can't be auto-installed, the entry is still written and the server
> connects automatically once you install Node 18+. Remove or disable them any
> time in **Settings → MCP Tools** or with `/mcp remove filesystem`; skip the
> auto-registration entirely with `OCTOSLAVE_NO_FS_MCP_REGISTER=1` (and skip
> the Node install in `install.sh` with `OCTOSLAVE_NO_NODE=1`).

---

## Quick setup (web UI, step by step)

Open **Settings → MCP Tools**. The card has everything:

### 1. The easy path — one-click catalog

Under **Catalog** you'll find ~21 curated, currently-maintained servers
(Filesystem, Git, GitHub, Playwright browser, Brave/Tavily search, SQLite,
Postgres, Slack, Notion, Google Drive, Context7 live docs, E2B sandbox, …).

1. Click **install** next to the one you want.
2. Fill in the one or two things that are genuinely *yours* — a directory to
   expose, a database path, an API token. Everything else (package name,
   transport, env-var conventions) is pre-configured.
3. That's it — the server connects immediately and shows up under
   **Your servers** with a live status dot and its tool count.

Each catalog entry declares its **runtime** (`npx` / `uvx` / `docker` / `http`)
so you can see up front whether you have what's needed. The first `npx`/`uvx`
install may take a moment while the package downloads.

### 2. Custom servers — the "+ Add a custom server" form

For anything not in the catalog:

- **Name** — a unique handle (becomes the `mcp__<name>__…` prefix).
- **Transport** — `stdio` or `http`.
- **stdio**: the **Command** (e.g. `npx`, `uvx`, `python`), space-separated
  **Arguments**, and optional **Env** (`KEY=VALUE`, comma-separated — API keys
  go here).
- **http**: the **URL** and optional **Headers**
  (e.g. `Authorization=Bearer xxx`).

Click **Add server** — it connects right away, no restart.

### 3. Managing servers

**Your servers** shows every configured server with its live connection state,
tool count, and any startup error. From there you can enable/disable or remove
a server, and expand it to see the individual tools. **Reconnect all** re-reads
the config file and reconnects everything — use it after editing
`~/.octoslave/config.json` by hand (or after the agent edits it for you, see
below).

A server that fails to start is reported and **skipped** — it never blocks the
rest of the agent.

---

## The lazy way: ask OctoSlave to add its own tools

OctoSlave can wire in MCP servers *for itself* — it just edits its own config
file. Paste something like this into the chat:

> Add an MCP server to your own toolbox: I want the **Playwright browser**.
> 1. Open `~/.octoslave/config.json` and append an entry to the top-level
>    `"mcp_servers"` list. The schema is
>    `{"name", "enabled": true, "command", "args": [...], "env": {...}}` for
>    stdio servers or `{"name", "enabled": true, "url", "headers": {...}}` for
>    http ones. Keep the rest of the file intact.
> 2. If you're unsure of the right package or command, consult your own curated
>    catalog first:
>    `python -c "from octoslave.mcp_registry import REGISTRY; import json; print(json.dumps(REGISTRY, indent=2))"`.
> 3. Verify the runtime exists (`which npx` / `uvx` / `docker`) before writing
>    the entry, and tell me if anything is missing.
>
> When you're done I'll click **Reconnect all** in Settings → MCP Tools.

The agent looks up the correct command in the built-in catalog, checks that the
runtime is available, writes the config entry, and reports back. Then one click
on **Reconnect all** (web UI) or `/mcp reconnect` (TUI) picks it up — the
reconnect re-reads the config from disk, so no restart is needed.

If the server needs an **API key or token**, the agent will ask you for it (or
tell you exactly which env value to fill in) — paste it in chat or edit the
`env`/`headers` value yourself if you'd rather not share it in the conversation.

> Inside the **Autonomous Lab**, agents can go one step further and connect
> catalog servers mid-run on their own via the `request_mcp` runtime-expansion
> tool. In normal chat the config-file route above is the way.

---

## Terminal (TUI / CLI)

```bash
/mcp                       # list servers + live status
/mcp registry              # browse the curated catalog
/mcp install filesystem    # install a catalog server (prompts for its inputs)
/mcp add NAME CMD [args…]  # quick-add a custom stdio server
/mcp remove NAME           # remove a server
/mcp reconnect             # re-read config and reconnect everything
```

---

## Where it's stored

Everything lives under `mcp_servers` in `~/.octoslave/config.json`:

```json
{
  "mcp_servers": [
    {
      "name": "filesystem",
      "enabled": true,
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/Users/me/data"]
    },
    {
      "name": "github",
      "enabled": true,
      "url": "https://api.githubcopilot.com/mcp/",
      "headers": {"Authorization": "Bearer ghp_…"}
    }
  ]
}
```

stdio entries may also carry `"env": {"API_KEY": "…"}` and an optional `"cwd"`.
`"enabled": false` keeps a server configured but disconnected.

---

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| Server shows an error / never connects | Expand it in **Your servers** to read the startup error; a broken server is skipped, the agent keeps working. |
| `npx` / `uvx` not found | Install Node.js (for `npx`) or `uv` (for `uvx`) — catalog entries state which runtime they need. |
| First install is slow | `npx -y` / `uvx` download the package on first run; later starts are fast. |
| Tool calls ask for confirmation | You're in `controlled` permission mode — MCP calls require a yes there. See [PERMISSION_MODE.md](PERMISSION_MODE.md). |
| Edited `config.json` by hand, nothing changed | Click **Reconnect all** (web UI) or run `/mcp reconnect` (TUI) — the config is only re-read on reconnect. |
| http server rejects requests | Check the `headers` auth value (most want `Authorization=Bearer <token>`). |

---

## Notes & limits

- MCP servers always run **locally**, even when tool execution is switched to a
  [remote host](REMOTE_EXECUTION.md) — they are not re-pointed at the remote.
- Tool names are namespaced `mcp__<server>__<tool>`, so two servers can expose
  tools with the same short name without clashing.
- Servers receive the current working directory as an MCP *root*, so
  filesystem-style servers know where you're working.
