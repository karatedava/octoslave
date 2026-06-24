# Long Research Pipeline

> **⚠️ Superseded by the Autonomous Lab.** `/long-research` (and the web `/lab` UI) now launch the
> **dynamic Lab** ([octoslave/lab/](../octoslave/lab/)): a **Director** assembles a custom team of
> up to 10 specialists per task, a **Critic** vets the plan before implementation, agents can build
> tools / add teammates / connect MCP servers at runtime, and a human can inject guidance live.
> Outputs live under `<working_dir>/lab/` (not `research/`), and all roles default to `kimi-k2.6`.
> The fixed 8-role pipeline described below is retained for reference (still importable as
> `octoslave.research.run_long_research`) but is no longer the default.

OctoSlave's original research pipeline ran a **fixed** set of specialist agents in coordinated
rounds, each building on the previous.

---

## Agents

| Agent | Icon | Default model | Role |
|-------|------|---------------|------|
| Orchestrator | 🧠 | `deepseek-v3.2-thinking` | Plans each round, reads findings, directs specialists |
| Researcher | 🔬 | `deepseek-v3.2` | Web search, literature, data gathering |
| Hypothesis | 💡 | `deepseek-v3.2` | Generates hypotheses from current findings |
| Coder | 💻 | `qwen3-coder-30b` | Writes analysis code, plots, experiments |
| Debugger | 🐛 | `deepseek-v3.2` | Fixes code errors from coder output |
| Evaluator | ⚖️ | `deepseek-v3.2-thinking` | Critiques results, identifies gaps |
| Reporter | 📋 | `deepseek-v3.2` | Writes per-round and final HTML reports |
| Merger | 🔀 | `deepseek-v3.2` | Synthesises parallel agent outputs |

---

## Usage

### From interactive mode

```
/long-research TOPIC [FLAGS]
```

```bash
/long-research "Quantum computing in drug discovery"
/long-research "Státnicové okruhy z biochemie" --rounds 6
/long-research "Market analysis of EV batteries" --parallel 3
/long-research "Continue previous topic" --resume
```

### Flags

| Flag | Default | Description |
|------|---------|-------------|
| `--rounds N` | 5 | Number of research rounds |
| `--parallel N` | 1 | Parallel copies of researcher / hypothesis / evaluator |
| `--all MODEL` | | Use one model for all agents |
| `--overseer MODEL` | | Override model for orchestrator only |
| `--resume` | | Skip rounds whose output files already exist |

### Prompt profile

The active prompt profile (set with `/profile` or `-p`) is automatically passed to all agents. Set it before running:

```bash
octoslave -p biomedic
/long-research "Histologie jaterní tkáně" --rounds 5
```

Or as a one-liner from shell:
```bash
octoslave -p biomedic -v
# then: /long-research "topic" --rounds 5
```

---

## Output structure

```
research/
├── findings.md              ← cumulative findings (updated every round)
├── final_report.html        ← master HTML report (open in browser)
├── hw_profile.json          ← hardware probe (used by coder agent)
└── round_01/
    ├── 01_brief.md          ← orchestrator brief for this round
    ├── 02_research.md       ← researcher output
    ├── 03_hypothesis.md     ← hypothesis agent output
    ├── 04_code/             ← coder scripts + results/
    │   └── results/         ← plots, CSVs, data files
    ├── 05_evaluation.md     ← evaluator critique
    ├── 06_synthesis.md      ← orchestrator synthesis
    └── 07_report.html       ← per-round HTML report
```

---

## Model assignment examples

```bash
# Use stronger model only for orchestrator
/long-research "topic" --overseer deepseek-v3.2-thinking

# Use one model for everything (e.g. testing)
/long-research "topic" --all deepseek-v3.2

# Use specialist coder model
# (set via /model before running — coder uses the role's default unless overridden)
/long-research "topic" --all qwen3-coder-30b
```

---

## Parallel mode

`--parallel N` runs N independent copies of the researcher, hypothesis, and evaluator roles simultaneously, then merges their outputs with the Merger agent:

```bash
/long-research "Immunotherapy mechanisms" --parallel 3 --rounds 5
```

With 3 parallel researchers you get broader coverage of the topic in the same number of rounds. Uses 3× the tokens for those roles.

---

## Resuming an interrupted run

```bash
/long-research "same topic" --resume
```

The pipeline detects which round output files already exist and skips them. Safe to run after a crash, rate-limit failure, or manual Ctrl+C.

---

## Running as a one-shot CLI command

```bash
# Long-research isn't a direct CLI subcommand yet — use run with a task description instead
octoslave run "Do a 5-round deep research on quantum computing drug discovery, write HTML report" -p base -v
```

For true multi-agent long-research headlessly, use the interactive mode inside `screen` or pipe commands:

```bash
screen -S research
octoslave
# /long-research "topic" --rounds 8 --overseer deepseek-v3.2-thinking
```
