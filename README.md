<div align="center">

<img src="assets/logo.svg" alt="OctoSlave" width="220"/>

<h1>OctoSlave</h1>

<p><strong>Autonomous AI research &amp; coding assistant — powered by <a href="https://llm.ai.e-infra.cz">e-INFRA CZ</a> or your own local GPU</strong></p>

[![Python](https://img.shields.io/badge/python-3.10%2B-blue?style=flat-square&logo=python&logoColor=white)](https://www.python.org)
[![License: MIT](https://img.shields.io/badge/license-MIT-green?style=flat-square)](LICENSE)
[![Platform](https://img.shields.io/badge/e--INFRA%20CZ-LLM-7B2FBE?style=flat-square)](https://llm.ai.e-infra.cz)
[![Ollama](https://img.shields.io/badge/Ollama-local%20models-1A6B5C?style=flat-square)](https://ollama.com)

</div>

---

OctoSlave is a terminal-based autonomous agent built for scientists and engineers.
Give it a task or a research topic — it explores the web, writes and runs code, debugs, evaluates, and iterates until the job is done.

It ships two modes:

- **Interactive agent** — an always-on REPL that can do anything Claude Code can, using academic-grade LLMs
- **Long-research pipeline** (`/long-research`) — a population of 6 specialist agents that conduct rigorous, multi-round research with real data, reproducible code, and a polished HTML deliverable

---

## Contents

- [Features](#features)
- [Installation](#installation)
- [Quick start](#quick-start)
- [Interactive TUI](#interactive-tui)
- [Slash commands](#slash-commands)
- [One-shot mode](#one-shot-mode)
- [Long-research pipeline](#long-research-pipeline)
- [Available models](#available-models)
- [Local models (Ollama)](#local-models-ollama)
- [Tools reference](#tools-reference)
- [Configuration](#configuration)
- [Project structure](#project-structure)
- [License](#license)

---

## Features

<table>
<tr><td>🔁 <strong>Autonomous loop</strong></td><td>Runs up to 80 tool-call iterations end-to-end — no hand-holding required</td></tr>
<tr><td>🌐 <strong>Web research</strong></td><td>DuckDuckGo search + full-page text extraction from any URL or PDF</td></tr>
<tr><td>🖥️ <strong>Shell &amp; filesystem</strong></td><td>Read, write, edit files; run arbitrary shell commands; install packages via uv / pip</td></tr>
<tr><td>📡 <strong>Streaming output</strong></td><td>Reasoning and tool calls appear in real time with a Rich TUI</td></tr>
<tr><td>🔬 <strong>Multi-agent research</strong></td><td>6 specialist roles collaborate across multiple rounds; findings.md updated automatically</td></tr>
<tr><td>📊 <strong>Visual-first results</strong></td><td>Every round produces publication-quality plots; final HTML report with embedded figures</td></tr>
<tr><td>🛡️ <strong>Data integrity</strong></td><td>Synthetic data is forbidden — the pipeline skips unavailable sources and pivots to alternatives</td></tr>
<tr><td>⚡ <strong>GPU-aware</strong></td><td>Hardware probe at startup; CUDA utilisation enforced in all generated code</td></tr>
<tr><td>🏠 <strong>Local mode</strong></td><td>Full functionality via Ollama — no API key needed, complete privacy</td></tr>
<tr><td>💾 <strong>Resumable</strong></td><td>Research runs persist to disk and resume exactly where they left off</td></tr>
</table>

---

## Installation

**Requirements:** Python 3.10+, an [e-INFRA CZ LLM](https://llm.ai.e-infra.cz) API key *(or Ollama for local mode)*

```bash
git clone https://github.com/karatedava/octoslave.git
cd octoslave
pip install -e .
```

> **Recommended:** use [uv](https://github.com/astral-sh/uv) for faster, reproducible installs:
> ```bash
> uv pip install -e .
> ```

### Set your API key

```bash
ots config                        # interactive setup wizard
ots config --api-key sk-YOUR_KEY  # pass key directly
export OCTOSLAVE_API_KEY=sk-...   # or set env var for the session
```

Config is saved at `~/.octoslave/config.json`. Environment variables always take precedence.

---

## Quick start

```bash
# Interactive TUI (e-INFRA CZ)
ots

# Interactive TUI (local Ollama)
ots --local

# One-shot task
ots run "build a Flask REST API for a todo app"

# Research — 3 autonomous rounds
ots
◆ /long-research "calibration methods for large language models" --rounds 3
```

---

## Interactive TUI

Running `ots` opens the full TUI:

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
  │  model deepseek-v3.2   dir ~/project           │
  │  /help for commands                            │
  ╰────────────────────────────────────────────────╯

◆ [deepseek-v3.2] _
```

- Type any task in natural language — the agent streams its thinking and tool calls live
- Follow up freely; full conversation context is preserved across turns
- Use `/` commands to control the session (see below)

**Keyboard shortcuts**

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
| `/model [name]` | Switch model; lists available if no name given |
| `/dir [path]` | Change the active working directory |
| `/clear` | Clear screen and reset conversation history |
| `/compact` | Summarise history into a compact context block (saves tokens) |
| `/local [model]` | Switch to local Ollama backend |
| `/einfra` | Switch back to e-INFRA CZ backend |
| `/pull model` | Pull a new Ollama model without leaving the session |
| `/long-research TOPIC [flags]` | Launch the multi-agent research pipeline |
| `/help` | Show all commands and flags |
| `/exit` | Quit (also `Ctrl+D`) |

---

## One-shot mode

```bash
ots run "refactor the authentication module" \
  --model qwen3-coder-30b \
  --dir /path/to/project

# Stay interactive after the run completes
ots run "set up a data processing pipeline for CSV files" -i

ots run --help   # full flag reference
```

---

## Long-research pipeline

`/long-research` deploys **6 specialist agents** that collaborate over multiple fully autonomous rounds:

```
╔══════════════════════════════════════════════════════════════╗
║  Round N                                                     ║
╠══════════════════════════════════════════════════════════════╣
║  🔬 Researcher        Fast targeted scout — SOTA, datasets,  ║
║                       verified access status, handoff brief  ║
║     ↓                                                        ║
║  💡 Experiment        Commits to ONE concrete experiment:    ║
║     Designer          pseudocode, data plan, success metric  ║
║     ↓                                                        ║
║  💻 Coder             Implements on real data, GPU-aware,    ║
║                       produces plots + key_results.json      ║
║     ↓                                                        ║
║  🐛 Debugger          Independent verifier — runs code,      ║
║                       checks GPU use, validates numbers       ║
║     ↓                                                        ║
║  ⚖️  Evaluator         Critical scoring vs SOTA; generates   ║
║                       a colour-coded scores bar chart         ║
║     ↓                                                        ║
║  🧠 Orchestrator      Synthesises findings → writes precise  ║
║                       brief for the next round               ║
╚══════════════════════════════════════════════════════════════╝
  ↓  (after all rounds)
  📊 Master Reporter — comprehensive self-contained HTML report
                       with embedded plots, score progression,
                       and collapsible round deep-dives
```

**Data integrity guarantee:** agents are explicitly forbidden from generating synthetic or dummy data.
If a dataset is unavailable the failure is logged, alternatives are searched, and the pipeline pivots — it never fabricates results.

**GPU enforcement:** a hardware probe runs at startup; all generated code is required to use CUDA when available (mixed-precision, correct device placement, peak VRAM logging).

### Usage

```
/long-research TOPIC [--rounds N] [--all MODEL] [--overseer MODEL] [--resume]
```

| Flag | Default | Description |
|------|---------|-------------|
| `--rounds N` | `5` | Maximum number of research rounds |
| `--all MODEL` | *(per-role defaults)* | Use one model for all 6 agents |
| `--overseer MODEL` | `deepseek-v3.2` | Override the orchestrator model only |
| `--resume` | off | Resume an interrupted run (skips agents whose output already exists) |

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

Each run creates a self-contained directory tree under `research/` in your working directory:

```
research/
├── final_report.html          ← master HTML report — open in browser
├── findings.md                ← cumulative findings updated after each round
├── hw_profile.json            ← detected hardware (CPU, GPU, VRAM)
│
├── round_001/
│   ├── 01_literature.md       ← papers, datasets (with verified access status)
│   ├── 02_experiment.md       ← experiment design, pseudocode, data plan
│   ├── 03_code/
│   │   ├── *.py               ← experiment scripts
│   │   ├── IMPLEMENTATION.md  ← approach, skipped steps, results summary
│   │   └── results/           ← plots (PNG), key_results.json, logs
│   ├── 04_debug_report.md     ← bugs found/fixed, confidence score
│   ├── 05_evaluation.md       ← independent scoring against SOTA
│   ├── 05_scores_chart.png    ← colour-coded evaluation bar chart
│   └── 06_synthesis.md        ← round summary + brief for next round
│
├── round_002/
│   └── ...
```

---

## Available models (e-INFRA CZ)

Run `ots models` to see the live list. Default assignments in the research pipeline:

| Model | Research role | Strengths |
|-------|--------------|-----------|
| `deepseek-v3.2` | Orchestrator, single-agent default | Strong reasoning, synthesis |
| `deepseek-v3.2-thinking` | Evaluator, Experiment Designer | Extended chain-of-thought |
| `qwen3-coder-30b` | Coder, Debugger | Code generation, tool use |
| `qwen3.5-122b` | Researcher | Fast reading, web research |
| `gpt-oss-120b` | Master Reporter | Large context, clean writing |
| `qwen3-coder` | Lightweight coder | Faster, smaller tasks |
| `qwen3.5` | Balanced general | Good all-round |
| `kimi-k2.5` | Long-context tasks | Extended context window |
| `llama-4-scout-17b-16e-instruct` | — | Meta Llama 4 |
| `gemma4` | — | Google Gemma 4 |
| `thinker` / `coder` / `agentic` / `mini` | — | Alias shortcuts |

Switch mid-session: `/model qwen3-coder-30b` or pass `-m MODEL` to any command.

---

## Local models (Ollama)

OctoSlave runs fully offline via [Ollama](https://ollama.com). All functionality — chat, one-shot tasks, and the full research pipeline — works identically with local models.

### Setup

```bash
# 1. Install Ollama
#    macOS:   brew install ollama
#    Linux:   curl -fsSL https://ollama.com/install.sh | sh

# 2. Start the Ollama daemon
ollama serve

# 3. Pull a model (see hardware guide below)
ollama pull llama3.1:8b

# 4. Start OctoSlave in local mode
ots --local
```

### Backend switching

```bash
# In the TUI:
/local                    # switch to Ollama (first pulled model)
/local llama3.1:8b        # switch to a specific model
/pull qwen2.5-coder:14b   # pull a model without leaving the session
/einfra                   # switch back to e-INFRA CZ

# On the command line:
ots --local run "explain this code"
ots models --local        # list pulled Ollama models
```

### Long-research with local models

In local mode, `/long-research` automatically distributes up to **3 pulled models** across the 6 specialist roles by priority tier:

| Tier | Roles | Characteristic needed |
|------|-------|----------------------|
| **A** — model 1 | Orchestrator, Evaluator | Strong reasoning, synthesis |
| **B** — model 2 | Coder, Debugger, Reporter | Code generation, structured output |
| **C** — model 3 | Researcher, Experiment Designer | Document reading, writing |

If you only have 1 or 2 models pulled, tiers collapse automatically.

### Hardware recommendations

<details>
<summary><strong>8 GB VRAM / 16 GB RAM</strong> — minimum viable</summary>

```bash
ollama pull mistral          # 4 GB — fast, general
```
Good for interactive chat and simple coding tasks. Long-research will be slow and capability-limited.
</details>

<details>
<summary><strong>16 GB VRAM / 32 GB RAM</strong> — recommended starter</summary>

```bash
ollama pull llama3.1:8b      # 5 GB — best reasoning at this size
ollama pull qwen2.5-coder    # 4 GB — strong at code
```
Assign: `llama3.1:8b` → Tier A, `qwen2.5-coder` → Tier B.
</details>

<details>
<summary><strong>24 GB VRAM / 48 GB RAM</strong> — comfortable research</summary>

```bash
ollama pull llama3.1:8b        # 5 GB — Tier A
ollama pull qwen2.5-coder:14b  # 9 GB — Tier B
ollama pull mistral             # 4 GB — Tier C
```
This is the sweet spot for autonomous research runs.
</details>

<details>
<summary><strong>48 GB+ VRAM</strong> — full power</summary>

```bash
ollama pull llama3.3:70b       # 40 GB — Tier A
ollama pull qwen2.5-coder:32b  # 20 GB — Tier B
ollama pull qwen2.5:14b        # 9 GB  — Tier C
```
Approaches cloud model quality for most research tasks.
</details>

<details>
<summary><strong>CPU only (no GPU)</strong></summary>

```bash
ollama pull llama3.2:3b        # 2 GB — smallest capable model
ollama pull qwen2.5-coder:3b   # 2 GB — minimal coding capability
```
Usable for simple interactive tasks. Long-research not recommended on CPU only.
</details>

> Run `ots models --local` at any time to see what you have pulled.

---

## Tools reference

| Tool | Description |
|------|-------------|
| `read_file` | Read file contents (offset/limit for large files); PDFs auto-extracted to text |
| `write_file` | Create or fully overwrite a file |
| `edit_file` | Targeted string replacement — safer than rewriting whole files |
| `bash` | Run any shell command: builds, tests, git, data processing, package installs |
| `glob` | Find files by pattern, e.g. `**/*.py` |
| `grep` | Regex search across files with context lines |
| `list_dir` | Directory listing with sizes and modification times |
| `web_search` | DuckDuckGo search → titles, URLs, one-line snippets |
| `web_fetch` | Fetch URL → clean readable text (strips JS/CSS/ads/nav) |

---

## Configuration

### Which backend should I use?

```
Do you have access to e-INFRA CZ? ──yes──▶ use einfra  (best model quality, free for Czech academia)
         │
         no
         │
         ▼
Do you have a GPU (≥8 GB VRAM)?  ──yes──▶ use ollama  (fully local, private, no API key needed)
         │
         no
         │
         ▼
         use ollama on CPU  (interactive tasks only; long-research not recommended)
```

Run `ots config` — the interactive wizard will walk you through each choice.

### Which model should I set as default?

| Goal | Recommended default |
|------|-------------------|
| Best all-round (reasoning + coding) | `deepseek-v3.2` ← **start here** |
| Writing-heavy tasks | `gpt-oss-120b` |
| Code generation focus | `qwen3-coder-30b` |
| Chain-of-thought / hard problems | `deepseek-v3.2-thinking` |
| Fast general purpose | `qwen3.5-122b` |

The default model is only the starting point — switch any time with `/model NAME` inside the TUI.

### What about `base_url` and `ollama_url`?

- **`base_url`** — leave at the default (`https://llm.ai.e-infra.cz/v1`) unless you are self-hosting an OpenAI-compatible API.
- **`ollama_url`** — leave at the default (`http://localhost:11434/v1`) unless Ollama runs on a different machine or port.

### Precedence and environment variables

| Mechanism | Precedence | Notes |
|-----------|-----------|-------|
| Environment variable | **Highest** | Overrides everything |
| `~/.octoslave/config.json` | Medium | Written by `ots config` |
| Built-in default | Lowest | `deepseek-v3.2`, e-INFRA CZ endpoint |

| Variable | Description |
|----------|-------------|
| `OCTOSLAVE_API_KEY` | e-INFRA CZ API key |
| `OCTOSLAVE_BASE_URL` | API base URL (default: `https://llm.ai.e-infra.cz/v1`) |
| `OCTOSLAVE_MODEL` | Default model override |
| `OCTOSLAVE_BACKEND` | `einfra` (default) or `ollama` |
| `OCTOSLAVE_OLLAMA_URL` | Ollama base URL (default: `http://localhost:11434/v1`) |

```bash
ots config          # guided interactive setup
ots config --show   # print current config (key masked)
```

---

## Project structure

```
octoslave/
├── assets/
│   └── logo.svg              ← project logo (teal octopus, gold chain)
├── octoslave/
│   ├── agent.py              ← core agent loop, system prompt, context management
│   ├── config.py             ← config load/save, Ollama helpers, model list
│   ├── display.py            ← Rich TUI: mascot, banners, streaming, research display
│   ├── main.py               ← Click CLI, interactive REPL, slash-command handler
│   ├── research.py           ← multi-agent long-research pipeline
│   └── tools.py              ← all tool definitions and implementations
└── pyproject.toml
```

---

## License

MIT — see [LICENSE](LICENSE).

<div align="center">
<br/>
<img src="assets/logo.svg" alt="OctoSlave" width="80"/>
<br/>
<sub>Built for researchers who demand real results.</sub>
</div>
