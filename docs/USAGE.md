# OctoSlave — Complete Usage Reference

Full reference for every CLI command, flag, and slash command.

---

## Table of Contents

1. [Installation & Setup](#installation--setup)
2. [CLI Commands](#cli-commands)
3. [Slash Commands (Interactive Mode)](#slash-commands-interactive-mode)
4. [Prompt Profiles](#prompt-profiles)
5. [Permission Modes](#permission-modes)
6. [Verbose Mode](#verbose-mode)
7. [Project Directories](#project-directories)
8. [Models](#models)

---

## Installation & Setup

```bash
git clone https://github.com/karatedava/octoslave ~/octoslave
cd ~/octoslave
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
octoslave config
```

---

## CLI Commands

### `octoslave` — Interactive mode

```bash
octoslave [OPTIONS]
```

Launches the interactive REPL. Type tasks directly, use slash commands to control behaviour.

| Flag | Short | Description |
|------|-------|-------------|
| `--model MODEL` | `-m` | Model to use (overrides config default) |
| `--dir PATH` | `-d` | Working directory (default: auto-created project dir) |
| `--prompt-profile NAME` | `-p` | Prompt profile: `base`, `coder`, `analyst` |
| `--permission-mode MODE` | | `autonomous` / `controlled` / `supervised` |
| `--verbose` | `-v` | Show full diffs, tool output, bash commands live |
| `--local` | | Use local Ollama models |
| `--api-key KEY` | | API key (or set `OCTOSLAVE_API_KEY`) |
| `--base-url URL` | | API base URL (or set `OCTOSLAVE_BASE_URL`) |

```bash
# Examples
octoslave
octoslave -p base
octoslave -v -p coder
octoslave -m deepseek-v3.2-thinking
octoslave --permission-mode controlled
octoslave --local
```

---

### `octoslave run` — One-shot task

```bash
octoslave run "TASK" [OPTIONS]
```

Runs a single task and exits. Add `-i` to stay in interactive mode after.

| Flag | Short | Description |
|------|-------|-------------|
| `--model MODEL` | `-m` | Model to use |
| `--dir PATH` | `-d` | Working directory (default: auto-created project dir) |
| `--prompt-profile NAME` | `-p` | Prompt profile |
| `--permission-mode MODE` | | Permission mode |
| `--verbose` | `-v` | Verbose output |
| `--interactive` | `-i` | Stay interactive after task completes |
| `--local` | | Use local Ollama models |

```bash
# Examples
octoslave run "build a REST API for a todo app"
octoslave run "research recent papers on RAG" -m qwen3-coder
octoslave run "add unit tests" -i
octoslave run "reorganize my notes" -p base -v
octoslave run "analyze sales data" -p analyst -d ~/data
octoslave run "fix the bug in main.py" --permission-mode controlled
```

---

### `octoslave vault-improve` — Vault pipeline

```bash
octoslave vault-improve [VAULT_PATH] [OPTIONS]
```

Autonomously improves every `.md` file in a vault. Runs headlessly — no terminal needed. Ideal for systemd / server use.

| Flag | Short | Description |
|------|-------|-------------|
| `VAULT_PATH` | | Path to vault directory (default: current working dir) |
| `--profile NAME` | `-p` | Prompt profile for writing style |
| `--model MODEL` | `-m` | Override model for all agents |
| `--resume` | | Resume interrupted run (skips completed batches) |
| `--api-key KEY` | | API key |
| `--base-url URL` | | API base URL |

```bash
# Examples
octoslave vault-improve ~/Brain2 --profile base
octoslave vault-improve ~/Brain2 --profile base --resume
octoslave vault-improve ~/Brain2 --model deepseek-v3.2-thinking
octoslave vault-improve  # uses current directory
```

See [VAULT_IMPROVE.md](VAULT_IMPROVE.md) for full pipeline documentation.

---

### `octoslave config` — Configuration

```bash
octoslave config [OPTIONS]
```

Set API key, default model, backend, and permission mode. Run without flags for interactive setup wizard.

| Flag | Description |
|------|-------------|
| `--api-key KEY` | Set API key |
| `--model MODEL` | Set default model |
| `--base-url URL` | Set API base URL |
| `--ollama-url URL` | Set Ollama URL (default: `http://localhost:11434/v1`) |
| `--permission-mode MODE` | Set default permission mode |
| `--show` | Print current config |

```bash
octoslave config          # interactive wizard
octoslave config --show   # show current config
```

Config is saved to `~/.octoslave/config.json`.

---

### `octoslave models` — List models

```bash
octoslave models [--local]
```

Lists available models on e-INFRA CZ, or pulled Ollama models with `--local`.

---

### `octoslave web` — Web UI

```bash
octoslave web [--host HOST] [--port PORT] [--no-browser]
```

Launches the web UI. Default: `http://127.0.0.1:7860`.

For remote access (phone, outside LAN) use Tailscale:

```bash
octoslave web --host 0.0.0.0 --port 7860
# Then access via Tailscale IP from anywhere
```

---

## Slash Commands (Interactive Mode)

Available inside `octoslave` interactive sessions.

### Session control

| Command | Description |
|---------|-------------|
| `/help` | Show command reference |
| `/exit` | Quit (also `Ctrl+D`) |
| `/clear` | Clear screen and reset conversation history |
| `/compact` | Summarise conversation history to save context window |

### Model & backend

| Command | Description |
|---------|-------------|
| `/model` | List available models |
| `/model NAME` | Switch to a different model |
| `/local [MODEL]` | Switch to local Ollama backend |
| `/einfra` | Switch back to e-INFRA CZ backend |
| `/pull MODEL` | Pull a new Ollama model |

### Working directory & profile

| Command | Description |
|---------|-------------|
| `/dir` | Show current working directory |
| `/dir PATH` | Change working directory |
| `/profile` | Show current profile and list available |
| `/profile NAME` | Switch prompt profile (`base`, `coder`, `analyst`) |

### Permission & verbosity

| Command | Description |
|---------|-------------|
| `/permission` | Show current permission mode |
| `/permission MODE` | Switch mode (`autonomous`, `controlled`, `supervised`) |
| `/verbose` | Toggle verbose mode on/off mid-session |

### Research & vault pipelines

| Command | Description |
|---------|-------------|
| `/long-research TOPIC [FLAGS]` | Launch multi-agent research pipeline |
| `/vault-improve [PATH] [FLAGS]` | Launch vault improvement pipeline |

#### `/long-research` flags

```
/long-research TOPIC [--rounds N] [--parallel N] [--all MODEL] [--overseer MODEL] [--resume]
```

| Flag | Default | Description |
|------|---------|-------------|
| `--rounds N` | 5 | Number of research rounds |
| `--parallel N` | 1 | Parallel agent copies for researcher/hypothesis/evaluator |
| `--all MODEL` | | Use one model for all agents |
| `--overseer MODEL` | | Override model for orchestrator only |
| `--resume` | | Skip rounds already completed |

The active prompt profile (`/profile`) is automatically passed to all agents.

```bash
# Examples
/long-research "Quantum computing in drug discovery" --rounds 8
/long-research "Státnicové okruhy z biochemie" --rounds 6 --overseer deepseek-v3.2-thinking
/long-research "Market analysis of EV batteries" --parallel 3 --rounds 5
/long-research "Continue previous topic" --resume
```

#### `/vault-improve` flags

```
/vault-improve [PATH] [--model MODEL] [--resume]
```

```bash
/vault-improve ~/Brain2 --model deepseek-v3.2-thinking
/vault-improve ~/Brain2 --resume
/vault-improve  # uses working directory
```

---

## Prompt Profiles

See [PROMPT_PROFILES.md](PROMPT_PROFILES.md) for full documentation.

| Profile | Language | Best for |
|---------|----------|----------|
| `base` | English | General tasks, coding, research |
| `coder` | English | Pure coding — no research preamble |
| `analyst` | English | Data analysis, statistics, plots |

```bash
octoslave run "analyze dataset" -p analyst
/profile coder
```

---

## Permission Modes

See [PERMISSION_MODE.md](PERMISSION_MODE.md) for full documentation.

| Mode | Behaviour |
|------|-----------|
| `autonomous` | Works without asking (default) |
| `controlled` | Asks before every file edit and bash command |
| `supervised` | Asks before file edits, auto-allows bash commands |

```bash
octoslave --permission-mode controlled
/permission supervised
```

---

## Verbose Mode

Shows full details of every tool call:

| What | Normal | Verbose |
|------|--------|---------|
| `edit_file` | filename only | red/green diff of every changed line |
| `write_file` | filename only | full file content with line count |
| `bash` | first 90 chars | full command |
| tool results | first 6 lines | all lines |

```bash
octoslave -v                       # verbose from start
octoslave run "task" -v            # one-shot verbose
/verbose                           # toggle on/off mid-session
```

---

## Project Directories

When no `-d / --dir` is given, OctoSlave automatically creates a project directory named after the task:

```
~/octoslave/projects/
├── analyze-sales-data-from-q1-2024/
├── research-quantum-computing-in-medicine/
├── build-a-rest-api-for-todo-app/
└── ...
```

This keeps every task's files isolated. Pass `-d PATH` to use a specific directory instead.

---

## Models

Recommended models on e-INFRA CZ:

| Model | Best for |
|-------|----------|
| `deepseek-v3.2` | Best all-round default (reasoning + coding) |
| `deepseek-v3.2-thinking` | Extended chain-of-thought; slower but more accurate |
| `qwen3-coder-30b` | Strongest at code generation |
| `qwen3.5-122b` | Fast reader; good for large research |
| `gpt-oss-120b` | Large context; clean writing |

```bash
octoslave models              # list all available
octoslave -m deepseek-v3.2-thinking
/model qwen3-coder-30b
```
