<div align="center">

<img src="assets/logo.png" alt="OctoSlave" width="220"/>

<h1>OctoSlave</h1>
<a href="https://octoslave.karamazov.website">Official Octoslave webside</a>

<p><strong>Autonomous AI research &amp; coding assistant — powered by <a href="https://llm.ai.e-infra.cz">e-INFRA CZ</a>, <a href="https://build.nvidia.com">NVIDIA NIM</a>, or your own local GPU</strong></p>

[![Python](https://img.shields.io/badge/python-3.10%2B-blue?style=flat-square&logo=python&logoColor=white)](https://www.python.org)
[![License: MIT](https://img.shields.io/badge/license-MIT-green?style=flat-square)](LICENSE)
[![Platform](https://img.shields.io/badge/e--INFRA%20CZ-LLM-7B2FBE?style=flat-square)](https://llm.ai.e-infra.cz)
[![Ollama](https://img.shields.io/badge/Ollama-local%20models-1A6B5C?style=flat-square)](https://ollama.com)
[![NVIDIA NIM](https://img.shields.io/badge/NVIDIA%20NIM-API-76B900?style=flat-square&logo=nvidia&logoColor=white)](https://build.nvidia.com)

</div>

---

OctoSlave is an autonomous agent built for scientists and engineers.
Give it a task or a research topic — it explores the web, writes and runs code, debugs, evaluates, and iterates until the job is done.

It ships several modes:

- **Interactive agent** — a chat-style assistant that can work on entire projects or assist with a single task
- **One-shot mode** (`ots run "..."`) — run a task, then exit (or stay interactive with `-i`)
- **Parallel agents** (`ots run "..." --parallel N`) — run N agents on the same task and have a Judge pick the best, peers vote, or a Merger synthesise the results
- **Long-research pipeline** (`/long-research`) — 8 specialist agents conduct rigorous, multi-round research with real data, reproducible code, and a self-contained HTML report
- **Vault improve** (`ots vault-improve`) — autonomous note-by-note improvement of an Obsidian / markdown vault
- **Batch mode** (`ots batch tasks.txt`) — run a list of tasks sequentially with resume support

---

## Contents

- [Features](#features)
- [Installation](#installation)
- [Quick start](#quick-start)
- [Agentic behaviour](#agentic-behaviour)
- [Web UI](#web-ui)
- [Interactive TUI](#interactive-tui)
- [Slash commands](#slash-commands)
- [Parallel agents](#parallel-agents)
- [MCP — wire in custom tools](#mcp--wire-in-custom-tools)
- [CLI commands](#cli-commands)
- [Long-research pipeline](#long-research-pipeline)
- [Vault improve](#vault-improve)
- [Batch mode](#batch-mode)
- [Backends and models](#backends-and-models)
  - [e-INFRA CZ](#e-infra-cz)
  - [NVIDIA NIM](#nvidia-nim)
  - [Ollama (local)](#ollama-local)
- [Tools reference](#tools-reference)
- [Prompt profiles](#prompt-profiles)
- [Permission modes](#permission-modes)
- [Configuration](#configuration)
- [Project structure](#project-structure)
- [License](#license)

---

## Features

<table>
<tr><td>🔁 <strong>Autonomous loop</strong></td><td>Runs many tool-call iterations end-to-end (per-role caps from 8 to 80) — no hand-holding required</td></tr>
<tr><td>🐙 <strong>Parallel agents</strong></td><td>Run N agents on the same task in isolated workdirs; a Judge / vote / Merger picks the winner. <code>--parallel 3 --strategy best</code></td></tr>
<tr><td>🧠 <strong>Upfront planning</strong></td><td>Before touching any files, the agent writes a numbered execution plan — making intent explicit and reducing aimless iteration</td></tr>
<tr><td>✅ <strong>Post-task verification</strong></td><td>Optional grade pass after each task: DONE / PARTIAL / FAILED with a one-sentence reason (<code>--verify</code>)</td></tr>
<tr><td>💾 <strong>Cross-session memory</strong></td><td>Outcomes of prior sessions are persisted to <code>~/.octoslave/session_memory.md</code> and injected as context on the next run</td></tr>
<tr><td>🩹 <strong>Error recovery nudge</strong></td><td>When the same operation fails across two consecutive turns, the agent is asked to diagnose and state an explicit new strategy</td></tr>
<tr><td>📦 <strong>Smart context compaction</strong></td><td>On context overflow, oldest turns are summarised (tool names, args, first result line) instead of silently dropped</td></tr>
<tr><td>🌐 <strong>Web research</strong></td><td>DuckDuckGo search, full-page extraction from any URL or PDF, BFS website crawler</td></tr>
<tr><td>🖥️ <strong>Shell &amp; filesystem</strong></td><td>Read, write, edit files; run arbitrary shell commands; install packages via uv / pip</td></tr>
<tr><td>📡 <strong>Streaming output</strong></td><td>Reasoning and tool calls appear in real time in a Rich TUI or web UI</td></tr>
<tr><td>🔬 <strong>Multi-agent research</strong></td><td>8 specialist roles collaborate over multiple rounds; cumulative <code>findings.md</code> updated each round</td></tr>
<tr><td>📊 <strong>Self-contained reports</strong></td><td>Every round produces plots; final HTML report has all images embedded as base64 — shareable as a single file</td></tr>
<tr><td>🛡️ <strong>Data integrity</strong></td><td>Synthetic data forbidden — pre-hoc Skeptic catches bad plans before Coder burns tokens</td></tr>
<tr><td>🧮 <strong>Resource inventory</strong></td><td>Deterministic pipeline-built file catalog (inventory IDs R001…); stops repeated schema re-discovery</td></tr>
<tr><td>⚡ <strong>GPU-aware</strong></td><td>Hardware probe at startup; CUDA utilisation enforced in all generated code</td></tr>
<tr><td>🎯 <strong>Convergence detection</strong></td><td>Auto-early-stop when scores plateau or publishable threshold (≥8/10) is reached two rounds in a row</td></tr>
<tr><td>🔒 <strong>Anti-regression memory</strong></td><td>Failed approaches from prior rounds are locked in <code>forbidden_approaches.md</code>; Designer must justify any overlap</td></tr>
<tr><td>🏠 <strong>Local mode</strong></td><td>Full functionality via Ollama — no API key needed, complete privacy</td></tr>
<tr><td>🔄 <strong>Resumable</strong></td><td>Research, vault, and batch runs persist to disk and resume exactly where they left off</td></tr>
<tr><td>🔒 <strong>Permission modes</strong></td><td><code>autonomous</code> (default), <code>controlled</code> (ask for everything), or <code>supervised</code> (ask before file edits only)</td></tr>
<tr><td>🧬 <strong>Bio &amp; chem connectors</strong></td><td>Direct REST access to UniProt, PubChem, ChEMBL, RCSB PDB, AlphaFold, NCBI GEO, ENA — plus RDKit and FASTA/PDB inspection</td></tr>
</table>

---

## Installation

**Requirements:** Python 3.10+ and an [e-INFRA CZ LLM](https://llm.ai.e-infra.cz) or [NVIDIA NIM](https://build.nvidia.com) API key — *or* Ollama for fully local mode.

### Download installer — no Python required

The easiest way to install OctoSlave. Each package bundles Python and all dependencies — just download and run.

| Platform | Download | Notes |
|----------|----------|-------|
| **macOS** | [OctoSlave-macOS.dmg](https://github.com/karatedava/octoslave/releases/latest/download/OctoSlave-macOS.dmg) | Double-click → drag to Applications → open the app |
| **Windows** | [OctoSlave-Windows-Installer.exe](https://github.com/karatedava/octoslave/releases/latest/download/OctoSlave-Windows-Installer.exe) | Run the installer wizard → follow the prompts |
| **Linux** | [OctoSlave-x86_64.AppImage](https://github.com/karatedava/octoslave/releases/latest/download/OctoSlave-x86_64.AppImage) | `chmod +x OctoSlave-x86_64.AppImage && ./OctoSlave-x86_64.AppImage` |

> All three installers launch a **setup wizard** on first run that guides you through choosing a backend, entering your API key, and picking a default model. No prior configuration needed.

---

### One-shot installer (macOS / Linux)

The included `scripts/install.sh` picks a Python ≥ 3.10, sets up `pipx` if it's missing, and installs OctoSlave into an isolated environment in one go:

```bash
git clone https://github.com/karatedava/octoslave.git
bash octoslave/scripts/install.sh
```

> The script's git/PyPI-aware version (so you can run it without cloning first) isn't yet hosted on a public URL. For now, fetch the script via the clone above; a `curl | bash` form will land once the package is published.

### pipx (any platform with Python 3.10+)

`pipx` keeps OctoSlave in its own virtualenv so it doesn't pollute your system Python:

```bash
pipx install "git+https://github.com/karatedava/octoslave.git#egg=octoslave[all]"
```

### pip

```bash
git clone https://github.com/karatedava/octoslave.git
cd octoslave
python -m venv .venv && source .venv/bin/activate
pip install -e ".[all]"
```

`uv` works too:

```bash
uv pip install -e ".[all]"
```

### Optional: codag (powers `compress_log`)

The `compress_log` tool drops noisy log streams (ML training output, kubectl logs, CI runs) into a templated summary — typically 95–99% token reduction with errors and tracebacks preserved verbatim. It shells out to the [`codag`](https://codag.ai) CLI.

You don't usually need to do anything: `scripts/install.sh` installs codag at the end, and the platform installers (DMG / EXE / AppImage) auto-install codag the first time the agent calls `compress_log` — a one-time ~5-second fetch. The free `compact` mode needs no account.

To install manually (or pre-warm a CI image):

```bash
curl -fsSL https://codag.ai/install.sh | sh
```

Opt out of all codag installs with `OCTOSLAVE_NO_CODAG=1` (during `scripts/install.sh`) or `OCTOSLAVE_NO_CODAG_AUTOINSTALL=1` (at runtime for the lazy path).

<details>
<summary><strong>Need help installing Python or pipx first?</strong></summary>

```bash
# macOS
brew install python pipx

# Linux (Debian / Ubuntu)
sudo apt update && sudo apt install python3 python3-pip pipx
pipx ensurepath

# Windows: download from https://www.python.org/downloads/ (tick "Add Python to PATH"),
# then in PowerShell:  python -m pip install --user pipx; python -m pipx ensurepath
```

</details>

### Configure your API key

```bash
ots config                                     # interactive wizard (einfra / nim / ollama)
ots config --api-key sk-YOUR_KEY               # e-INFRA CZ key directly
ots config --nim-api-key nvapi-YOUR_KEY        # NVIDIA NIM key
ots config --model kimi-k2.6                   # default model
ots config --show                              # print current config (keys masked)
```

Config is saved at `~/.octoslave/config.json`. Environment variables always take precedence:

```bash
export OCTOSLAVE_API_KEY=sk-...               # e-INFRA CZ
export OCTOSLAVE_NIM_API_KEY=nvapi-...        # NVIDIA NIM
```

---

## Quick start

```bash
ots                                            # interactive TUI (default backend)
ots --local                                    # interactive TUI, local Ollama
ots --nim                                      # interactive TUI, NVIDIA NIM
ots web                                        # browser UI at http://127.0.0.1:7860
ots run "build a Flask REST API for a todo app"
ots run "summarise this paper" -i              # one-shot, then stay interactive

# 3 agents on the same task — Judge picks the winner
ots run "refactor the auth module" --parallel 3 --strategy best

# Research — 3 autonomous rounds
ots
◆ /long-research "calibration methods for large language models" --rounds 3
```

---

## Agentic behaviour

OctoSlave ships five behaviours that make it more deliberate and self-aware — inspired by how experienced engineers approach complex tasks.

### Upfront planning (default: on)

Before calling any tool, the agent writes a numbered execution plan:

```
╭─────── ◆ Plan ──────────────────────────────────────────╮
│ 1. Read the existing auth module to understand structure  │
│ 2. Identify all call sites using grep                    │
│ 3. Write the new middleware with backward-compatible API  │
│ 4. Update imports in each call site                      │
│ 5. Run tests and verify no regressions                   │
╰──────────────────────────────────────────────────────────╯
```

Disable with `--no-plan` or `/plan off` in the TUI. View the last plan again with `/show-plan`.

### Post-task verification (default: off)

After the loop exits, the agent grades its own work:

```
  ✓ Verification: DONE — REST API created in api.py with all 4 endpoints passing tests.
```

Enable with `--verify` or `/verify on`.

### Cross-session memory (default: on)

At the end of each session, the outcome is appended to `~/.octoslave/session_memory.md`. The next session injects the last 3 entries as context so the agent doesn't repeat completed work:

```
[PRIOR SESSIONS]
  2025-01-15: build a REST API — done (api.py created, tests pass)
  2025-01-14: research RAG methods — partial (literature.md done, no code yet)
```

Commands: `/memory` (show), `/memory clear` (erase), `/memory on|off` (toggle).
Disable for a run with `--no-memory`.

### Error recovery nudge (always on)

When a tool fails across two consecutive turns, the agent is interrupted with a structured prompt:

```
You have encountered errors in multiple consecutive turns.
1. Diagnosis — what do you think is causing these failures?
2. Strategy — what will you do differently this time?
```

This prevents silent retry loops and forces an explicit change of approach.

### Smart context compaction (always on)

Two layers of automatic context management:

**Proactive trim** estimates total tokens before each API call (char/4 heuristic) and compacts oldest tool-call groups whenever the conversation exceeds the soft budget (`OCTOSLAVE_SOFT_CONTEXT_TOKENS`, default `96000`). This catches provider error-string mismatches and saves a wasted round-trip on hosts that 400 without a parseable error message.

**Reactive recovery** detects 20+ context-window error phrasings across providers (OpenAI, Anthropic, vLLM, Kimi via e-INFRA, HTTP 413) and compacts up to 10 groups per pass — usually one retry recovers from a deeply-overflowed conversation.

The compacted summary keeps head + tail of each tool result so errors and tracebacks survive:

```
[COMPACTED HISTORY — 3 earlier turn(s) summarised to save context]
  called: bash(python train.py 2>&1 | tee train.log)
    → INFO worker 0 batch loss=0.5 | INFO worker 1 batch loss=0.5 | INFO worker 2 batch loss=0.5
    ⚠ ERROR: CUDA out of memory at batch 142 | RuntimeError: out of memory | Traceback (most recent call last):
  called: read_file(train.log)
    ...
```

The model can also call **`compress_log`** explicitly to drop a noisy log into a templated summary via [codag](https://codag.ai) (≈95–99% token reduction with rare errors preserved). Use for ML training logs, kubectl/docker logs, CI output. See [debug/compress_log_demo/](debug/compress_log_demo/) for a worked example.

Manual compaction: `/compact` (summarises via the model).

---

### Flags reference

| Flag | Command | Default | Description |
|------|---------|---------|-------------|
| `--no-plan` | `ots`, `ots run` | plan ON | Skip the upfront planning step |
| `--verify` | `ots`, `ots run` | verify OFF | Grade completion after the task |
| `--no-memory` | `ots`, `ots run` | memory ON | Don't load or save session memory |

TUI toggles: `/plan on\|off`, `/verify on\|off`, `/memory on\|off`, `/memory clear`, `/show-plan`

---

## Web UI

```bash
pip install -e ".[web]"
ots web                                        # auto-opens browser
ots web --port 8080                            # custom port
ots web --host 0.0.0.0                         # expose on the network
ots web --no-browser                           # don't auto-open
```

| Tab | What it does |
|-----|-------------|
| **Chat** | Full conversational agent — streaming responses, tool-call inspector, conversation history, file attachments. `@` in the composer autocompletes a file from the working directory. |
| **Research** | Launch `/long-research` with live round progress, agent status, and streaming console |
| **Files** | Browse research outputs — view HTML reports inline, preview plots and markdown |
| **Settings** | Inspect / refresh current configuration (API key, model, backend) |

**Slash commands in the web UI:** all of the TUI's slash commands are also accepted in the chat composer, including `/parallel 3 task` (renders side-by-side candidate cards with the winner highlighted) and `/share` (creates a public read-only URL for the conversation).

All research outputs (HTML reports, plots, markdown) are accessible in the Files tab without leaving the browser.

---

## Interactive TUI

```
  ╭────────────────────────────────────────────────╮
  │                  ██████████                    │
  │               ██████████████                   │
  │              ████████████████                  │
  │            ██████████████████                  │
  │            ████◉███████◉█████                  │
  │            ██████████████████                  │
  │               ████ ▄▄▄▄▄ ████                  │
  │            ◆─◆─◆─◆─◆─◆─◆─◆─◆─                  │
  │                █████ ◈ █████                   │
  │             ╰██╯ ╰██╯ ╰██╯ ╰██╯                │
  │                                                │
  │               OCTOSLAVE                        │
  │  model kimi-k2.6   dir ~/project                │
  │  /help for commands                            │
  ╰────────────────────────────────────────────────╯

◆ [kimi-k2.6] _
```

- Type any task in natural language — the agent streams its thinking and tool calls live
- Follow up freely; full conversation context is preserved across turns
- Use `/` commands to control the session

| Key | Action |
|-----|--------|
| `↑` / `↓` | Cycle through prompt history |
| `Ctrl+C` | Cancel current generation (history kept) |
| `Ctrl+D` | Exit |
| `Ctrl+L` | Clear terminal screen |

---

## Slash commands

| Command | Description |
|---------|-------------|
| `/help` | Show all commands |
| `/model [name]` | Switch model; lists available if no name given |
| `/dir [path]` | Change the active working directory |
| `/new-project [hint]` | Create a fresh `~/octoslave/projects/<hint>/` directory and switch to it |
| `/profile [name]` | Switch prompt profile (`base` / `coder` / `analyst` / `biomedic`) |
| `/permission [mode]` | Show or change permission mode (`autonomous` / `controlled` / `supervised`) |
| `/plan on\|off` | Enable / disable the upfront planning step (default: on) |
| `/verify on\|off` | Enable / disable post-task verification grade (default: off) |
| `/show-plan` | Re-display the plan from the current task |
| `/memory` | Show cross-session memory (prior tasks and outcomes) |
| `/memory clear` | Erase the session memory file |
| `/memory on\|off` | Enable / disable memory loading/saving (default: on) |
| `/parallel N [strategy] task` | Run N agents on the same task; pick `best` / `vote` / `merge` |
| `/share` | Save the current conversation as a read-only share snapshot |
| `/undo` | Rewind the last user/assistant exchange (history only — does not revert files) |
| `/clear` | Clear screen and reset conversation history |
| `/compact` | Summarise history into a compact context block (saves tokens) |
| `/verbose` | Toggle verbose mode (show full diffs and output) |
| `/local [model]` | Switch to local Ollama backend |
| `/einfra` | Switch back to e-INFRA CZ backend |
| `/nim [model]` | Switch to NVIDIA NIM backend |
| `/pull MODEL` | Pull a new Ollama model without leaving the session |
| `/provider …` | Manage custom OpenAI-compatible providers |
| `/mcp …` | Wire in external tools via MCP servers (see below) |
| `/long-research TOPIC [flags]` | Launch the multi-agent research pipeline (see below) |
| `/research-roles` | Inspect or override per-role models for `/long-research` |
| `/vault-improve [path]` | Launch autonomous vault-wide note improvement |
| `/exit` (`/quit`, `/q`) | Quit (also `Ctrl+D`) |

**TUI shortcuts:** type `@` at the prompt to autocomplete a file from the working directory. `Ctrl+T` toggles permission mode (`autonomous` ↔ `controlled`). `Ctrl+L` clears the screen.

---

## Parallel agents

Run multiple agents on the same task and let OctoSlave pick the winner.

```bash
# 3 agents, judge model picks the best implementation
ots run "refactor auth.py for testability" --parallel 3 --strategy best

# 4 agents, each peer-reviews the others; majority wins
ots run "write a sorting benchmark" --parallel 4 --strategy vote

# 3 agents, results are merged into a single synthesis (PARALLEL_MERGE.md)
ots run "compare React vs Solid for our use-case" --parallel 3 --strategy merge
```

Each agent runs in an isolated copy of the working directory under
`.parallel/run_{i}/`. The winning candidate's files are promoted back into
the working directory; losing runs stay on disk for inspection. Diversity
between agents comes from rotating prompt profiles
(`base` / `coder` / `analyst` / `biomedic`).

| `--strategy` | Behaviour |
|---|---|
| `best` *(default)* | A judge model compares all candidates and picks one |
| `vote` | Each candidate grades the others; majority winner is promoted |
| `merge` | A merger synthesises all candidates into one combined answer (no winner promoted; merge written to `PARALLEL_MERGE.md`) |

In the web UI, `/parallel 3 task description` runs the same flow and shows
each candidate as a side-by-side card with the winner highlighted.

---

## MCP — wire in custom tools

OctoSlave speaks the [Model Context Protocol](https://modelcontextprotocol.io),
so you can plug in external tools — git, GitHub, a real browser, databases, live
docs, your own scripts — and the agent uses them right alongside the built-in
tools. MCP tools are available everywhere: single-agent runs, the research
pipeline, and the web UI.

The MCP client is **built in** — no extra Python dependency. It supports both
MCP transports:

- **stdio** — OctoSlave launches the server as a local subprocess and exchanges
  JSON-RPC over its stdin/stdout. The common case for local tool servers
  (run via `npx` or `uvx`).
- **http** — OctoSlave POSTs JSON-RPC to a remote URL (the "Streamable HTTP"
  transport) with optional auth headers; handles both JSON and SSE responses.

### How MCP tools appear to the agent

Once a server is connected, each of its tools is exposed to the model as
`mcp__<server>__<tool>` (e.g. `mcp__git__git_log`). The namespacing means MCP
tools never collide with the built-in tool names, and you can always tell which
server a call went to. In the research pipeline, MCP tools are offered to the
doer/reader roles (researcher, coder, debugger, evaluator, reporter) and kept
off the review-only roles to keep them lean.

### Quickest path: install from the catalog

OctoSlave ships a curated catalog of well-known servers so you get competitive
capabilities out of the box — you only supply the bits that are yours (a
directory, a DB path, a token):

```bash
/mcp registry            # browse the catalog, grouped by category
/mcp install filesystem  # prompts for a directory, then connects
/mcp install github      # 🔑 prompts for a token, wired as an auth header
/mcp install git sqlite  # (install several, one at a time)
```

You can also pre-fill inputs inline so it never prompts:

```bash
/mcp install filesystem path=/Users/me/project
/mcp install brave-search BRAVE_API_KEY=...
```

**Catalog (20 servers):**

| category | ids |
|---|---|
| Files & code | `filesystem`, `git`, `github` 🔑, `context7` |
| Web & browser | `fetch`, `playwright`, `brave-search` 🔑, `tavily` 🔑, `puppeteer` |
| Data & databases | `sqlite`, `postgres` |
| Reasoning & memory | `memory`, `sequential-thinking`, `time` |
| Productivity & cloud | `slack` 🔑, `notion` 🔑, `google-drive` 🔑, `sentry`, `aws-docs`, `e2b` 🔑 |

🔑 = needs an API key/token (you'll be prompted; it's stored locally and, for
remote servers, sent only as an auth header). stdio servers run via `npx` (Node)
or `uvx` (uv); the catalog shows whether a required runtime is on your `PATH` and
how to install it if not.

### Managing servers from the TUI

```bash
/mcp                       # list configured servers + live status (tool counts)
/mcp registry              # browse the catalog
/mcp install <id> [k=v…]   # install a catalog server
/mcp add                   # interactive wizard for a custom server (stdio/http)
/mcp add NAME CMD [args…]   # quick-add a custom stdio server
/mcp disable NAME           # turn a server off without deleting it
/mcp enable  NAME           # turn it back on
/mcp remove  NAME           # delete a server
/mcp reconnect              # re-read config and reconnect everything
```

Quick-add example (any server not in the catalog):

```bash
/mcp add my-tool npx -y @scope/some-mcp-server --flag value
```

### Managing servers from the Web UI

Open the **Settings** tab → **MCP Tools** card. There you can:

- see every configured server with a live status dot (connected / disabled /
  error) and its discovered tool count;
- browse the **Catalog** and click **install** on any server (you'll be prompted
  for any required path/token);
- enable / disable / remove servers, or **Reconnect all**;
- add a custom server (stdio or http) via the **+ Add a custom server** form.

Installs and reconnects run server-side and the panel refreshes with live
status. The same `~/.octoslave/config.json` is used, so servers added in the TUI
show up in the web UI and vice-versa.

### Config format

Servers are persisted in `~/.octoslave/config.json` under `mcp_servers` and
connected automatically at startup:

```json
{
  "mcp_servers": [
    {
      "name": "filesystem",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/path/to/project"],
      "env": { "SOME_VAR": "value" },
      "enabled": true
    },
    {
      "name": "internal-api",
      "url": "https://tools.example.com/mcp",
      "headers": { "Authorization": "Bearer YOUR_TOKEN" },
      "enabled": true
    }
  ]
}
```

A server entry needs either a `command` (stdio) or a `url` (http). Anything you
add via `/mcp add` or the catalog ends up here.

### Safety & failure handling

- In `controlled` [permission mode](#permission-modes), MCP tool calls require
  confirmation just like file edits and shell commands.
- A server that fails to start (missing runtime, bad command, network error) is
  reported and **skipped** — it never blocks the rest of the toolbox. Fix it and
  run `/mcp reconnect`.
- On first use a stdio server may need to download its package (e.g. `npx`
  fetching from npm); if a freshly-installed server shows "not connected", give
  it a moment and `/mcp reconnect`.

---

## CLI commands

```bash
ots                              # interactive TUI
ots run TASK [options]           # one-shot task; -i to stay interactive afterwards
ots web [options]                # launch the browser UI
ots config [options]             # interactive setup wizard, or pass flags directly
ots models [--local]             # list available models (live for cloud backends)
ots vault-improve PATH [options] # autonomous vault improvement (see below)
ots batch TASKS_FILE [options]   # run tasks one-per-line from a file with resume

# Examples with agentic flags
ots run "refactor the authentication module" --model qwen3-coder-30b --dir /path/to/project
ots run "set up a data processing pipeline for CSV files" -i   # stay interactive after run
ots run "rename variable x to count" --no-plan                 # skip planning for trivial tasks
ots run "migrate the database schema" --verify                 # grade completion after task
ots run "throwaway experiment" --no-memory                     # skip cross-session memory
ots run "refactor auth module" --parallel 3                    # 3 agents, judge picks winner
ots run "explore design options" --parallel 4 --strategy vote  # peer-vote majority winner
ots run "compare A vs B vs C" --parallel 3 --strategy merge    # synthesised answer in PARALLEL_MERGE.md

ots run --help   # full flag reference
```

Run `ots <command> --help` for the full flag reference for any command.

Common flags accepted by `ots` and `ots run`:

| Flag | Description |
|------|-------------|
| `-m`, `--model` | Model override |
| `-d`, `--dir` | Working directory |
| `-p`, `--prompt-profile` | `base` / `coder` / `analyst` / `biomedic` |
| `--local` / `--nim` | Force backend for this session |
| `--permission-mode` | `autonomous` / `controlled` / `supervised` |
| `-v`, `--verbose` | Show full diffs, complete tool output, live bash |
| `-i` (run only) | Stay interactive after the task completes |
| `-n`, `--new-project` (run only) | Create a fresh project directory under `~/octoslave/projects/` |
| `--parallel N` (run only) | Run N agents on the same task in parallel (default: 1) |
| `--strategy` (run only) | How to combine parallel agents: `best` / `vote` / `merge` |

---

## Long-research pipeline

`/long-research` deploys **8 specialist agents** that collaborate over multiple fully autonomous rounds:

```
╔══════════════════════════════════════════════════════════════╗
║  Round N                                                     ║
╠══════════════════════════════════════════════════════════════╣
║  🔬 Researcher       Reads inventory.md first, then scouts   ║
║                      SOTA papers, datasets, verified access  ║
║     ↓                                                        ║
║  💡 Designer         Commits to ONE concrete experiment:     ║
║                      pseudocode, data plan, success metric   ║
║     ↓                                                        ║
║  🤨 Skeptic          Pre-hoc PI review — catches circular    ║
║                      eval, missing inventory IDs, simulator  ║
║                      without earned failure BEFORE the Coder ║
║     ↓                                                        ║
║  💻 Coder            Implements on real data, GPU-aware,     ║
║                      produces plots + key_results.json       ║
║     ↓                                                        ║
║  🐛 Debugger         Independent verifier — runs code,       ║
║                      checks GPU use, validates numbers       ║
║     ↓                                                        ║
║  ⚖️  Evaluator        Critical scoring vs SOTA; generates   ║
║                      a colour-coded scores bar chart         ║
║     ↓                                                        ║
║  🧠 Orchestrator     Synthesises findings → writes precise   ║
║                      brief for the next round. Early stop    ║
║                      if convergence detected (≥8/10 or       ║
║                      plateaued failed approach)              ║
╚══════════════════════════════════════════════════════════════╝
  ↓  (after all rounds)
  📊 Master Reporter — self-contained HTML report with EMBEDDED
                       plots (base64), score progression, and
                       collapsible round deep-dives
```

**Data integrity guarantee:** agents are explicitly forbidden from generating synthetic, dummy, or "same-feature-range random" data. If a primary dataset is unreachable, the pipeline searches alternatives or marks the round BLOCKED — it never fabricates results.

**GPU enforcement:** a hardware probe runs at startup; all generated code is required to use CUDA when available (mixed-precision, correct device placement, peak VRAM logging).

**Self-contained reports:** the final HTML report has every plot inlined as a base64 data URI, so a single file can be emailed or shared without an asset folder.

### Usage

```
/long-research TOPIC [--rounds N] [--all MODEL] [--overseer MODEL]
                     [--role ROLE MODEL] [--parallel N] [--resume] [--scrape]
```

| Flag | Default | Description |
|------|---------|-------------|
| `--rounds N` | `5` | Maximum number of research rounds |
| `--all MODEL` | *per-role defaults* | Use one model for all 8 agents |
| `--overseer MODEL` | *per-role default* | Override the orchestrator model only |
| `--role ROLE MODEL` | — | Override a single role (e.g. `--role coder qwen3-coder-30b`) |
| `--parallel N` | `1` | Run multiple independent copies of researcher / designer / evaluator in parallel |
| `--min-rounds N` | `2` | Never auto-terminate before this many rounds (protects against premature convergence) |
| `--resume` | off | Resume an interrupted run (skips agents whose output already exists) |
| `--scrape` | off | Enable Playwright-backed website crawling for the Researcher |

### Examples

```
/long-research "effect of batch size on transformer generalisation" --rounds 3

/long-research "protein folding accuracy of ESMFold vs AlphaFold2" \
  --rounds 5 \
  --all qwen3-coder-30b \
  --overseer deepseek-v3.2-thinking

/long-research "RAG retrieval strategies for long documents" --resume
```

### Output structure

Each run creates a self-contained tree under `research/` in the working directory:

```
research/
├── final_report.html          ← master HTML report (open in browser, fully self-contained)
├── findings.md                ← cumulative findings updated after each round
├── hw_profile.json            ← detected hardware (CPU, GPU, VRAM)
│
├── round_001/
│   ├── 01_literature.md       ← papers, datasets (with verified access status)
│   ├── 02_experiment.md       ← experiment design, pseudocode, data plan
│   ├── 02b_skeptic_review.md  ← pre-hoc PI review: PASS / OBJECT verdict + issues
│   ├── 03_code/
│   │   ├── *.py               ← experiment scripts
│   │   ├── IMPLEMENTATION.md  ← approach, skipped steps, results summary
│   │   └── results/           ← plots (PNG), key_results.json, logs
│   ├── 04_debug_report.md     ← bugs found/fixed, confidence score
│   ├── 05_evaluation.md       ← independent scoring against SOTA
│   ├── 05_scores_chart.png    ← colour-coded evaluation bar chart
│   └── 06_synthesis.md        ← round summary + brief for next round
│
└── round_002/ ...
```

See [docs/RESEARCH.md](docs/RESEARCH.md) for the full pipeline contract.

---

## Vault improve

Autonomous note-by-note improvement of an Obsidian / markdown vault — fact-check, expand, fix structure, and link related notes.

```bash
ots vault-improve ~/Brain --profile biomedic
ots vault-improve ~/Brain --profile biomedic --resume
ots vault-improve ~/Brain --model deepseek-v3.2-thinking
```

| Flag | Description |
|------|-------------|
| `-p`, `--profile` | Prompt profile (`base` / `coder` / `analyst` / `biomedic`) |
| `-m`, `--model` | Model override for all vault agents |
| `--resume` | Resume an interrupted run |

State is persisted under the vault — re-running with `--resume` skips notes already processed. See [docs/VAULT_IMPROVE.md](docs/VAULT_IMPROVE.md) for details.

---

## Batch mode

Run a list of tasks from a plain text file, one per line, with resume support.

```bash
ots batch tasks.txt
ots batch tasks.txt --profile biomedic --resume
ots batch tasks.txt -m deepseek-v3.2-thinking --output-dir ~/results
```

- Lines starting with `#` are treated as comments and skipped.
- State is saved to `tasks.txt.state.json` after every completed task.
- Re-run with `--resume` to skip already-completed tasks.

---

## Backends and models

```
Do you have access to e-INFRA CZ? ──yes──▶ use einfra  (best model quality, free for Masaryk University)
         │
         no
         │
         ▼
Do you have an NVIDIA NIM key?   ──yes──▶ use nim      (good models, no local GPU needed)
         │
         no
         │
         ▼
Do you have a GPU (≥8 GB VRAM)?  ──yes──▶ use ollama   (fully local, private, no API key needed)
         │
         no
         │
         ▼
         use ollama on CPU (interactive tasks only; long-research not recommended)
```

Run `ots config` to launch the interactive wizard.

### e-INFRA CZ

The default backend. Run `ots models` for the live list. Recommended defaults:

| Goal | Model |
|------|-------|
| Best all-round (reasoning + coding, long-context) | `kimi-k2.6` ← **start here / default** |
| Chain-of-thought / hard problems | `deepseek-v3.2-thinking` |
| Code generation focus | `qwen3-coder-30b` |
| General reasoning | `deepseek-v3.2` |
| Writing-heavy tasks | `gpt-oss-120b` |

Common available models on e-INFRA CZ: `deepseek-v3.2`, `deepseek-v3.2-thinking`, `qwen3.5`, `qwen3.5-122b`, `qwen3-coder`, `qwen3-coder-30b`, `qwen3-coder-next`, `gpt-oss-120b`, `kimi-k2.5`, `kimi-k2.6`, `mistral-medium-3.5`, `llama-4-scout-17b-16e-instruct`, `gemma4`, `glm-4.7`, `glm-5`, `glm-5.1`.

Default per-role assignments for the long-research pipeline (override with `--all` / `--overseer` / `--role`):

| Role | Default model |
|------|---------------|
| Researcher | `deepseek-v3.2-thinking` |
| Designer (hypothesis) | `deepseek-v3.2-thinking` |
| Skeptic | `deepseek-v3.2-thinking` |
| Coder | `kimi-k2.6` |
| Debugger | `qwen3-coder-30b` |
| Evaluator | `kimi-k2.6` |
| Orchestrator | `kimi-k2.6` |
| Reporter | `kimi-k2.6` |
| Merger (parallel mode) | `deepseek-v3.2` |

### NVIDIA NIM

[NVIDIA NIM](https://build.nvidia.com) gives you cloud-hosted inference for frontier open-weight models (Llama 4, Nemotron, Qwen, etc.) via an OpenAI-compatible API.

**Get a key:** sign in at [build.nvidia.com](https://build.nvidia.com), open any model card, click **Get API Key**. The key starts with `nvapi-`. Free-tier accounts get monthly credits.

**Configure:**

```bash
ots config --backend nim --nim-api-key nvapi-YOUR_KEY \
           --model nvidia/nemotron-3-super-120b-a12b
```

**Use:**

```bash
ots --nim                                                  # force NIM for this session
ots --nim --model meta/llama-4-maverick-17b-128e-instruct

# In the TUI:
/nim                                                       # switch to NIM (keeps current model)
/nim nvidia/nemotron-3-super-120b-a12b                     # switch to NIM with a specific model
/model                                                     # list available NIM models
```

Default model: `nvidia/nemotron-3-super-120b-a12b` (used for **all** roles in the long-research pipeline by default — chosen because it handles long contexts reliably without hitting NIM gateway timeouts).

Commonly available NIM models (run `ots models` with NIM configured for your live list):

| Model | Notes |
|-------|-------|
| `nvidia/nemotron-3-super-120b-a12b` | Default — strong reasoning, stable at long contexts |
| `nvidia/llama-3.3-nemotron-super-49b-v1.5` | NVIDIA-tuned, smaller |
| `nvidia/llama-3.1-nemotron-nano-8b-v1` | Smallest, fast |
| `meta/llama-4-maverick-17b-128e-instruct` | Llama 4, balanced |
| `meta/llama-3.3-70b-instruct` | Reliable, widely available |
| `meta/llama-3.1-405b-instruct` | Largest Llama 3 |
| `qwen/qwen3-coder-480b-a35b-instruct` | Strong code generation |
| `deepseek-ai/deepseek-v3.2` | DeepSeek on NIM |
| `google/gemma-3-27b-it` | Compact, efficient |
| `mistralai/mistral-large-2-instruct` | Strong reasoning (paid tier) |

> If a model returns a 404 "not found for account" error, your tier doesn't have access. Use `/model` to list what your key can actually reach.

### Ollama (local)

OctoSlave runs fully offline via [Ollama](https://ollama.com). All functionality — chat, one-shot, vault, and `/long-research` — works identically with local models.

```bash
# 1. Install
brew install ollama                            # macOS
curl -fsSL https://ollama.com/install.sh | sh  # Linux

# 2. Run the daemon
ollama serve

# 3. Pull a model
ollama pull llama3.1:8b

# 4. Start OctoSlave in local mode
ots --local
```

In `/long-research` mode with Ollama, OctoSlave automatically distributes up to **3 pulled models** across the 7 specialist roles by tier:

| Tier | Roles | Characteristic needed |
|------|-------|----------------------|
| **A** — model 1 | Orchestrator, Evaluator | Strong reasoning, synthesis |
| **B** — model 2 | Coder, Debugger, Reporter | Code generation, structured output |
| **C** — model 3 | Researcher, Designer | Document reading, writing |

If you have only 1 or 2 models pulled, tiers collapse automatically.

<details>
<summary><strong>Hardware recommendations</strong></summary>

| VRAM | Recommended models | Use case |
|------|-------------------|----------|
| 8 GB | `mistral` (4 GB) | Chat + simple coding only |
| 16 GB | `llama3.1:8b` + `qwen2.5-coder` | Recommended starter for research |
| 24 GB | `llama3.1:8b` + `qwen2.5-coder:14b` + `mistral` | Sweet spot for autonomous research |
| 48 GB+ | `llama3.3:70b` + `qwen2.5-coder:32b` + `qwen2.5:14b` | Approaches cloud quality |
| CPU only | `llama3.2:3b` + `qwen2.5-coder:3b` | Interactive tasks only — `/long-research` not recommended |

Run `ots models --local` at any time to see what you have pulled.
</details>

---

## Tools reference

**Filesystem &amp; shell**

| Tool | Description |
|------|-------------|
| `read_file` | Read file contents (offset/limit for large files); PDFs auto-extracted to text |
| `write_file` | Create or fully overwrite a file |
| `edit_file` | Targeted string replacement (use `replace_all=true` for renames) |
| `apply_patch` | Apply several string-replacement edits to one file in a single atomic call |
| `bash` | Run any shell command: builds, tests, git, data processing, package installs |
| `glob` | Find files by pattern (e.g. `**/*.py`) |
| `grep` | Regex search across files with context lines |
| `list_dir` | Directory listing with sizes and modification times |

**Process &amp; workflow**

| Tool | Description |
|------|-------------|
| `run_background` | Start a long-running/blocking command (dev server, training job) in the background, returns a process id |
| `check_process` | Read a background process's status, exit code, and recent output (or list all) |
| `stop_process` | Stop a background process (SIGTERM → SIGKILL) |
| `todo_write` | Maintain a live task checklist for multi-step work — renders in the TUI and web UI |
| `ask_user` | Ask the human a clarifying question and wait for the answer (web round-trip / CLI prompt; no-ops in non-interactive runs) |

**Web**

| Tool | Description |
|------|-------------|
| `web_search` | DuckDuckGo search → titles, URLs, one-line snippets |
| `web_fetch` | Fetch URL → clean readable text (strips JS/CSS/ads/nav); PDFs auto-extracted |
| `crawl_tree` | BFS-crawl a website tree (Playwright-aware) — for documentation, catalogues, hierarchies |

**Biology &amp; chemistry** *(install with `pip install -e ".[bio]"`)*

| Tool | Description |
|------|-------------|
| `bio_inspect` | Schema-aware preview for FASTA / FASTQ / VCF / GFF / GTF / PDB / mmCIF / MTX / h5ad / SMI / SDF — counts, schema, head |
| `rdkit_describe` | SMILES → canonical SMILES, MW, logP, TPSA, HBD/HBA, rings, QED, Lipinski violations |
| `uniprot_lookup` | UniProtKB protein record (by accession) or search (by query) — name, organism, GO, PDB cross-refs |
| `pubchem_lookup` | PubChem compound by name / CID / SMILES — formula, MW, XLogP, TPSA, HBD/HBA |
| `chembl_lookup` | ChEMBL bioactive molecule (by ID or name) — max phase, RO5, indications |
| `pdb_fetch` | Download RCSB PDB / mmCIF structure by 4-char ID; returns header summary |
| `alphafold_fetch` | Download AlphaFold DB predicted structure by UniProt accession; reports mean pLDDT |
| `geo_search` | NCBI GEO / SRA dataset search (E-utilities) — accessions, sample counts, platforms |
| `ena_fetch` | EBI ENA file report — FASTQ download URLs, read counts, library layout |
| `pdf_ocr` | Render PDF pages and OCR them — recovers numbers/labels embedded in figures (axis ticks, EC50/IC50 values, heat-map legends) that `read_file` cannot reach |

> The bio/chem connectors call public REST APIs directly. They are preferred over `web_fetch` for any UniProt / PubChem / ChEMBL / GEO / ENA / RCSB / AlphaFold lookup — the agent gets parsed JSON instead of HTML and avoids burning the per-round web budget.

---

## Prompt profiles

A prompt profile is the system prompt used to seed the agent. Switch with `-p NAME` on the CLI or `/profile NAME` in the TUI.

| Profile | Best for |
|---------|----------|
| `base` | General-purpose engineering and research (default) |
| `coder` | Pure software engineering — file edits, tests, refactors |
| `analyst` | Data analysis, exploration, plotting, statistical inference |
| `biomedic` | Bio / chem research — uses bio tools by preference, follows literature conventions |

See [docs/PROMPT_PROFILES.md](docs/PROMPT_PROFILES.md) for details and examples.

---

## Permission modes

| Mode | Behaviour |
|------|-----------|
| `autonomous` *(default)* | Agent works without asking. Best for trusted workflows. |
| `controlled` | Agent asks before any modifying action (file edits, writes, shell). Best for production code. |
| `supervised` | Agent asks before file edits/writes; shell commands run automatically. Best for "watch the diffs but don't approve every test". |

```bash
ots --permission-mode supervised
ots run "edit files" --permission-mode supervised
export OCTOSLAVE_PERMISSION_MODE=supervised

# In the TUI:
/permission supervised
```

In `controlled` / `supervised` mode you'll see a prompt before modifying actions:

```
┌────── Controlled Mode ──────┐     ┌────── Supervised Mode ───────┐
│  ⚠ Permission Required      │     │  ⚠ Permission Required       │
│  ✏️  write_file             │     │  🔧 edit_file                │
│  OctoSlave wants to:        │     │  OctoSlave wants to:         │
│  create/overwrite file:     │     │  edit file: src/main.py      │
│  src/main.py                │     │                              │
└─────────────────────────────┘     └──────────────────────────────┘
Allow? (y)/n                            Allow? (y)/n
```

Full details: [docs/PERMISSION_MODE.md](docs/PERMISSION_MODE.md).

---

## Configuration

### Precedence

| Mechanism | Precedence | Notes |
|-----------|-----------|-------|
| Environment variable | **Highest** | Overrides everything |
| `~/.octoslave/config.json` | Medium | Written by `ots config` |
| Built-in default | Lowest | `deepseek-v3.2`, e-INFRA CZ endpoint |

### Environment variables

| Variable | Description |
|----------|-------------|
| `OCTOSLAVE_API_KEY` | e-INFRA CZ API key |
| `OCTOSLAVE_BASE_URL` | e-INFRA CZ base URL (default: `https://llm.ai.e-infra.cz/v1`) |
| `OCTOSLAVE_MODEL` | Default model override |
| `OCTOSLAVE_BACKEND` | `einfra` (default), `ollama`, or `nim` |
| `OCTOSLAVE_OLLAMA_URL` | Ollama base URL (default: `http://localhost:11434/v1`) |
| `OCTOSLAVE_NIM_API_KEY` | NVIDIA NIM API key (`nvapi-...`) |
| `OCTOSLAVE_NIM_URL` | NIM base URL (default: `https://integrate.api.nvidia.com/v1`) |
| `OCTOSLAVE_PERMISSION_MODE` | `autonomous` / `controlled` / `supervised` |

```bash
ots config            # interactive wizard
ots config --show     # print current config (keys masked)
```

---

## Project structure

```
octoslave/
├── assets/
│   └── logo.png
├── docs/
│   ├── DEPLOYMENT.md         ← deployment guide
│   ├── PERMISSION_MODE.md    ← permission mode reference
│   ├── PROMPT_PROFILES.md    ← prompt profile reference
│   ├── RESEARCH.md           ← long-research pipeline contract
│   ├── SCRAPING.md           ← web scraping / crawl_tree details
│   ├── USAGE.md              ← extended usage examples
│   └── VAULT_IMPROVE.md      ← vault-improve pipeline
├── octoslave/
│   ├── agent.py              ← core agent loop, system prompt, context management
│   ├── config.py             ← config load/save, model lists, role-model maps
│   ├── display.py            ← Rich TUI + web event bridge (thread-safe emit)
│   ├── main.py               ← Click CLI, interactive REPL, slash-command handler, @-completer
│   ├── parallel.py           ← parallel-agent runner (best / vote / merge strategies)
│   ├── prompt_profiles/      ← system prompts: base, coder, analyst, biomedic, local
│   ├── research.py           ← multi-agent long-research pipeline
│   ├── tools.py              ← filesystem, shell, web tool definitions
│   ├── tools_bio.py          ← biology / chemistry connectors (UniProt, PubChem, …)
│   ├── vault.py              ← vault-improve pipeline
│   └── web/
│       ├── app.py            ← FastAPI backend: WebSocket, /share, /api/picker, file serving
│       └── static/
│           ├── index.html    ← single-page UI (Chat / Research / Files / Settings)
│           ├── css/styles.css
│           └── js/
│               ├── app.js                ← message router, parallel panel, @-picker
│               ├── components.js
│               ├── slash-commands.js     ← /parallel, /share, /undo, etc.
│               ├── utils.js
│               └── websocket.js
├── scripts/
│   ├── install.sh            ← one-line installer (curl | bash)
│   └── release.md            ← maintainer release checklist
├── Formula/
│   └── octoslave.rb          ← Homebrew formula (lives in karatedava/homebrew-tap)
├── run_research.py           ← CLI helper: run long-research without the TUI
└── pyproject.toml
```

---

## License

MIT — see [LICENSE](LICENSE).

<div align="center">
<br/>
<img src="assets/logo.png" alt="OctoSlave" width="80"/>
<br/>
<sub>Built for researchers who demand real results.</sub>
</div>
