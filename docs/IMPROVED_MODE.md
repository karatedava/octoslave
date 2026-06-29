# Improved (council) mode

> A unified single agent: one surface, an internal council of role-specialized models.

Rather than one model doing everything, a lightweight coordinator **dynamically routes each
step** between a small pool of role-specialized models — **Thinker / Worker / Verifier** —
so a *diverse pool beats any single model*. Different model families catch different
mistakes; the verifier turns that into fewer wrong actions and more reliable completions.

You still talk to **one agent**. The council is internal.

---

## How to use it

### Terminal

```bash
ots improved                       # launch the Improved TUI (interactive)
ots improved -d ~/project          # in a working directory
ots improved --thinker glm-5.2     # override a single role
```

One-shot, non-interactive (the Improved counterpart of `ots run`):

```bash
ots improved run "analyse this dataset" -p analyst -d ~/data
ots improved run "build a REST API for a todo app"
ots improved run "add unit tests" -i          # stay interactive after the task
ots improved run "explain this repo" --no-plan
```

`ots improved run` mirrors `ots run` (same flags: `-d`, `-p`, `-i`, `-v`,
`-n`, `--no-plan`, `--no-memory`, `--permission-mode`) but the work is driven by
the Thinker / Worker / Verifier council, plus the role overrides `--worker`,
`--thinker`, `--verifier`.

Inside any session:

```text
/improved on        # enable council mode (clears history, resolves roles live)
/improved off       # back to the normal single agent
/improved status    # show the resolved Worker / Thinker / Verifier models
```

The plain `ots` command is **unchanged** — Improved mode is purely additive.

### Web UI

```bash
ots web             # http://127.0.0.1:7860
```

The chat config bar has a **3-position mode selector** — **Standard · 🐙 Improved · ⚡ Ultra**
(defaults to **Improved**). Standard is a single-model chat; Improved runs the council; Ultra adds
the multi-model debate (see *Ultra orchestration* below). Improved and Ultra are disabled on the
local Ollama backend (it snaps back to Standard; see *Backends*).

---

## The three roles

| Role | Default model | Responsibility |
|------|---------------|----------------|
| 🔧 **Worker**  | `kimi-k2.7` | Drives the tool loop: reads, writes, edits, runs commands. The visible executor. |
| 🧠 **Thinker** | `kimi-k2.7` | Orients in the workspace (read-only), writes the execution plan, and course-corrects when the Worker stalls or errors repeatedly. |
| 🔍 **Verifier**| `glm-5.2`   | Independent critic: reviews **risky actions before they run** and grades **completion**, sending the Worker back when work is wrong or incomplete. |

Distinct model *families* for Worker/Thinker vs. Verifier are deliberate — an independent
critic from a different family catches mistakes a self-review would rationalize away.

---

## The coordinator (how a turn is routed)

The coordinator is a **fast heuristic** — it inspects the Worker's proposed tool calls and
its own signals, with *no extra routing LLM call*, so latency stays close to single-model:

```text
Thinker: orient (read-only) + write the plan         ── once, up front
loop:
  Worker proposes the next action ─┐
    read-only (read_file/grep/…)   ├─► execute directly            (easy)
    mutating  (write/edit/apply_   │
       patch/bash/run_background)  ├─► Verifier reviews BEFORE run  (risky)
                                   │     APPROVE → execute
                                   │     REVISE  → drop, Worker fixes & retries (≤2)
  repeated tool errors            ─┴─► Thinker injects a course-correction  (hard)
  Worker claims "done"             ──► Verifier completion gate
                                         DONE   → finish
                                         REVISE → Thinker re-strategizes,
                                                  Worker does another round (≤3)
```

Expensive reasoning/critic calls happen **only where they add value** (planning, risky
writes, completion) — not on every step.

### Diversity escalation (the ensemble lever)

A council of one model family isn't much more than that model. When the Worker gets **stuck**
— repeated execution errors, a verifier-deadlocked action, a rejected completion, or **re-running
the same action with no progress** — the loop switches the Worker to a **different model family**
(the *escalation worker*, e.g. `kimi-k2.7 → deepseek-v3.2-thinking`). A block one family can't clear
is often trivial for another; this is where an ensemble's gain over any single model actually comes
from. The escalation worker is resolved live from the catalog (a strong model whose family differs
from the primary Worker) and shown in the resolved roles panel; if no different-family model is
available, escalation is simply skipped.

Three guards keep the loop trustworthy:

- the Worker is **forced to call a tool until it has actually done something** — so a run can never
  "complete" having executed nothing;
- a completion claim is only graded **after** real work has happened;
- **re-running the same action** (same tool + args) with no edit in between is detected — the loop
  re-strategizes and switches family, and stops outright if it keeps repeating. Re-running a command
  *after* an edit is productive and never penalized.

### Ultra orchestration (`--ultra` / `/ultra on`)

For the hardest tasks, Improved mode has a deeper tier. It uses a **diverse panel** (one model per
family, e.g. `kimi-k2.7 · glm-5.2 · deepseek-v3.2-thinking`) for two debates:

- **Planning** — each panelist drafts a plan **in isolation**, then the aggregator (the Verifier
  model, a different family) **synthesizes** one stronger plan: keeps the best of each, drops what's
  wrong, resolves disagreements.
- **Completion** — when the Worker claims it's done, each panelist independently grades the finished
  work, and the aggregator resolves their verdicts. A deliverable only clears when independent
  critics from different families agree; a real defect any one spots is surfaced, while the
  aggregator dismisses nitpicks so a lone over-strict critic can't deadlock the run.

Isolated drafts/reviews preserve diversity; the synthesis harvests the union of their strengths —
how a non-frontier pool can exceed any single model. It costs more tokens and latency, so it is
**off by default**:

```bash
ots improved --ultra                       # interactive, ultra on
ots improved run "hard task" --ultra        # one-shot, ultra on
```

Inside a session: `/ultra on`, `/ultra off`, `/ultra status`. The resolved panel is shown in the
roles header.

---

## Model selection & overrides

Models are **probed live** against the backend's `/v1/models` at startup and resolved down
a per-role preference chain (so a not-yet-released id is honored *if/when* it appears, and
gracefully skipped otherwise). Defaults and chains live in
[`octoslave/config.py`](../octoslave/config.py) (`COUNCIL_ROLE_MODELS`, `COUNCIL_ROLE_PREFERENCES`).

Override precedence (highest first):

1. **CLI flags** — `--worker`, `--thinker`, `--verifier` (on `ots improved`)
2. **Environment** — `OCTOSLAVE_COUNCIL_WORKER`, `OCTOSLAVE_COUNCIL_THINKER`, `OCTOSLAVE_COUNCIL_VERIFIER`
3. **Live preference chain** — first preferred id present in the live catalog
4. **Safe defaults** — used when the catalog can't be probed

```bash
ots improved --worker kimi-k2.7 --thinker glm-5.2 --verifier deepseek-v3.2-thinking
OCTOSLAVE_COUNCIL_VERIFIER=glm-5.1 ots improved
```

When a role falls back, the resolved panel prints a short note (e.g.
`deepseek-v3.2-thinking (first available preference)`).

---

## Backends

Improved and Ultra need a **cloud pool** of large models — they run on **e-INFRA CZ** (default)
and **NVIDIA NIM**. On the **local Ollama** backend they automatically **fall back to the
normal single agent** (the large models can't be co-resident locally); the web selector disables
Improved/Ultra there (snapping back to Standard) and the TUI prints a notice.

---

## Cost & latency

A council turn can call the Thinker and/or Verifier in addition to the Worker, so it uses
**more tokens** and is somewhat slower than a single model — in exchange for stronger,
better-checked results. **Ultra** costs the most (a model panel debates the plan and completion).
Read-only steps stay single-model, and the gates are bounded (≤2 action revisions, ≤3 completion
rounds, with an automatic stop on a repeated-action loop) to avoid runaway loops. Switch to
**Standard** (`/improved off`, or the web selector) for quick, cheap chats.

---

## Implementation notes

- Orchestrator: [`octoslave/council.py`](../octoslave/council.py) —
  `run_council_agent` / `continue_council_agent`, `resolve_council_roles`, `_council_loop`,
  and the heuristic `_classify_turn`.
- The council loop reuses the normal agent's hardened primitives via the shared
  `agent._robust_stream` helper (identical retry / context-compaction / connection-drop
  handling), so Improved mode inherits the same robustness as normal mode.
- Wiring: `ots improved` / `--ultra`, `/improved`, and `/ultra` in
  [`octoslave/main.py`](../octoslave/main.py); the **Standard · Improved · Ultra** selector in
  [`octoslave/web/app.py`](../octoslave/web/app.py) and the static UI.
- Ultra debate: `_debate_drafts` (isolated panel) + `_aggregate` / `_debate_completion`
  (synthesis) in [`octoslave/council.py`](../octoslave/council.py); panel resolved by
  `resolve_debate_panel` in [`octoslave/config.py`](../octoslave/config.py).
- The coordinator is a fast heuristic (no extra routing LLM call): role specialization,
  dynamic per-step routing, verifier-driven iteration, and best-of-N debate, with no training pipeline.
