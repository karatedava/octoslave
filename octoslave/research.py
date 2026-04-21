"""
OctoSlave — autonomous multi-agent long-research pipeline.

Pipeline per round:
  Researcher → HypothesisGenerator → Coder → Debugger → Evaluator → Orchestrator

The Orchestrator synthesises each round and writes the brief for the next one.
Everything is persisted to disk so runs can be inspected or resumed.
"""

from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from openai import OpenAI, BadRequestError

from . import display
from .agent import _cap_result
from .tools import TOOL_DEFINITIONS, execute_tool

# ---------------------------------------------------------------------------
# Role registry
# ---------------------------------------------------------------------------

ROLES: dict[str, dict] = {
    "researcher": {
        "label": "Researcher",
        "icon": "🔬",
        "color": "bold cyan",
        "default_model": "qwen3.5-122b",           # large — fast reading + search
        "max_iter": 15,                             # 15 = budget for ~5 web ops + write
        "tools": ["read_file", "write_file", "web_search", "web_fetch",
                  "list_dir", "glob"],              # no bash — researcher surveys, never installs
    },
    "hypothesis": {
        "label": "Experiment Designer",
        "icon": "💡",
        "color": "bold bright_magenta",
        "default_model": "deepseek-v3.2-thinking",  # thinking — commit to the right experiment
        "max_iter": 8,
        "tools": ["read_file", "write_file", "list_dir", "glob"],
    },
    "coder": {
        "label": "Coder",
        "icon": "💻",
        "color": "bold green",
        "default_model": "qwen3-coder-30b",         # large code model — fewer mistakes
        "max_iter": 50,
        "tools": ["read_file", "write_file", "edit_file", "bash",
                  "glob", "grep", "list_dir"],
    },
    "debugger": {
        "label": "Debugger",
        "icon": "🐛",
        "color": "bold red",
        "default_model": "qwen3-coder-30b",         # same coder — knows the code
        "max_iter": 20,
        "tools": ["read_file", "write_file", "edit_file", "bash",
                  "glob", "grep", "list_dir"],
    },
    "evaluator": {
        "label": "Evaluator",
        "icon": "⚖️ ",
        "color": "bold yellow",
        "default_model": "deepseek-v3.2-thinking",  # thinking — rigorous scientific judgement
        "max_iter": 15,
        "tools": ["read_file", "bash", "write_file", "list_dir",
                  "web_search", "glob"],
    },
    "orchestrator": {
        "label": "Orchestrator",
        "icon": "🧠",
        "color": "bold bright_white",
        "default_model": "deepseek-v3.2",           # strong reasoning — synthesis + direction
        "max_iter": 8,
        "tools": ["read_file", "write_file", "list_dir", "glob"],
    },
    "reporter": {
        "label": "Reporter",
        "icon": "📊",
        "color": "bold bright_cyan",
        "default_model": "gpt-oss-120b",            # large general — clean HTML/writing
        "max_iter": 40,
        "tools": ["read_file", "write_file", "bash", "list_dir", "glob"],
    },
    "merger": {
        "label": "Merger",
        "icon": "🔀",
        "color": "bold bright_cyan",
        "default_model": "deepseek-v3.2",
        "max_iter": 12,
        "tools": ["read_file", "write_file"],
    },
}

# Roles that can run as N independent parallel copies (no intra-round dependencies)
PARALLEL_ROLES: frozenset[str] = frozenset({"researcher", "hypothesis", "evaluator"})

# Per-round pipeline — reporter runs ONCE at the very end, not each round
PIPELINE: list[str] = [
    "researcher",
    "hypothesis",
    "coder",
    "debugger",
    "evaluator",
    "orchestrator",
]

# Expected output paths (relative to round_dir)
OUTPUT_FILES: dict[str, str] = {
    "researcher":    "01_literature.md",
    "hypothesis":    "02_experiment.md",
    "coder":         "03_code/",          # directory
    "debugger":      "04_debug_report.md",
    "evaluator":     "05_evaluation.md",
    "orchestrator":  "06_synthesis.md",
    "reporter":      "07_report.html",
}

FINDINGS_FILE = "findings.md"
NEXT_BRIEF_MARKER = "## NEXT_ROUND_BRIEF"
COMPLETE_MARKER = "## STATUS: COMPLETE"


# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

_SHARED_HEADER = """\
You are the {label} in OctoSlave's multi-agent research pipeline.

TOPIC     : {topic}
ROUND     : {round_num} / {max_rounds}  {final_tag}
ROUND DIR : {round_dir}
RESEARCH  : {research_dir}
WORK DIR  : {working_dir}

BRIEF:
{brief}

EXECUTION RULES — non-negotiable:
- ACT, don't narrate. Zero preamble. "I will now..." costs a tool call. Skip it.
- Read ONLY the section you need (use offset/limit on read_file). Never read a whole file.
- Write each output file ONCE. No drafts, no re-reads, no polish passes.
- INTERMEDIATE FILES (01_literature.md … 06_synthesis.md) are terse HANDOFFS, not reports.
  They exist so the next agent can start fast — not to document your reasoning.
  If it fits in a bullet list, use a bullet list. Prose is waste.
- STOP the moment your required output file is written. Do not make additional tool calls
  to "verify", "review", or "summarise". The next agent will read it directly.
- LONG TASKS (training, data download) are expected to take hours. Do not abort them.
  Pass an appropriate timeout to bash (see PACKAGES / LONG-RUNNING JOBS below).
---
"""

_ROLE_PROMPTS: dict[str, str] = {

"researcher": """\
YOUR MISSION
Fast, targeted intelligence-gathering pass. Equip the Experiment Designer with
exactly what they need to commit to ONE concrete experiment. 3 sharp sources
beat 10 shallow ones. Total output: under 500 words.

HARD LIMITS — enforced by the system. Violating them means 01_literature.md is NOT written
and the entire round fails. Every limit below is a MAXIMUM, not a target:
  list_dir:    1 call   (step 0 only)
  read_file:   0–2 calls (local data files only — NOT task.md, NOT findings.md twice)
  web_search:  max 2 calls  ← STRICT. Stop searching after 2.
  web_fetch:   max 2 calls  ← STRICT. Stop fetching after 2.
  write_file:  1 call   (your LAST call — always)
  TOTAL: max 8 calls. After call 8, your next and only action is write_file.

RESEARCHER CONSTRAINTS — non-negotiable:
- Do NOT read task.md. The topic is already in your brief above — reading it again wastes a call.
- Do NOT install packages. You have no bash tool. Survey only.
- Do NOT run code. Do NOT validate datasets programmatically.
- Dataset accessibility: fetch the landing page once. If it loads and a download link is visible
  → ACCESSIBLE. Otherwise → REQUIRES_SIGNUP/PAYWALLED. That's the full check. Move on.
- After 2 web_search + 2 web_fetch calls, you have gathered enough. WRITE the file.

STEPS
0. LOCAL DATA FIRST (mandatory, round 1 and every round):
   Call list_dir on {working_dir}. If any PDFs, CSVs, FASTAs, TSVs, or JSON files
   exist there, read the most relevant ones NOW using read_file — they are the
   user's primary input. A local PDF is the paper you are extending; a local CSV
   is the dataset you must analyse. Do not web-search topics already covered by
   local files.
1. Round > 1: read {research_dir}/findings.md — ONLY the ## Key Findings section
   (use read_file with offset/limit). Round 1: skip this step entirely.
2. Run 2–3 targeted web searches to fill gaps NOT covered by local files. Fetch ONE
   page per search (the most useful one). Stop the moment you can answer:
   (a) best known result / method, (b) which dataset is accessible right now.
3. For each external dataset candidate: fetch its landing page. Label it:
   ACCESSIBLE | REQUIRES_SIGNUP | PAYWALLED | UNAVAILABLE. Only confirmed ones.
4. Write 01_literature.md. Stop. Do not re-read it. Do not add more searches.

OUTPUT — write EXACTLY ONE file: {round_dir}/01_literature.md
The filename MUST be exactly "01_literature.md". Do NOT write any other file (no HTML reports,
no final_report, no CSV, no summary). Any other file write is WRONG and wastes your only call.
Keep every section to bullet points — no prose paragraphs except the last one.

  ## SOTA Summary     (2–3 bullets: best result, method, benchmark)
  ## Available Datasets (name · path or URL · size · ACCESS STATUS)
    - LOCAL files from {working_dir} are always ACCESSIBLE — list their full
      absolute paths here so downstream agents can use them directly.
  ## Baselines        (concrete numbers only, e.g. "ResNet-50: 76.1% top-1")

  ## FOR THE EXPERIMENT DESIGNER
  [1 focused paragraph: which gap to target, which dataset to use, what
   baseline to beat, key gotcha. Be direct — the next agent reads ONLY this
   section. MUST include the absolute path(s) of any local data files so the
   Hypothesis Designer can pass them to the Coder verbatim.]
""",

"hypothesis": """\
YOUR MISSION
Design exactly ONE concrete, executable experiment. Be decisive.
Total output: under 400 words.

STEPS
1. Read ONLY the ## FOR THE EXPERIMENT DESIGNER section from
   {round_dir}/01_literature.md (use offset/limit — do not read the whole file).
2. Round > 1: read ONLY the ## What Failed section from {research_dir}/findings.md.
   Round 1: skip.
3. Think once, commit, write. No drafting, no iteration.

OUTPUT — write EXACTLY ONE file: {round_dir}/02_experiment.md
The filename MUST be exactly "02_experiment.md". Any other filename (e.g. 02_methodology.md)
is WRONG and will break the pipeline. No exceptions.

  ## Experiment: <short name>
  **Hypothesis**: one falsifiable claim
  **Success metric**: specific threshold (e.g. "F1 > 0.82 on test set")
  **Failure threshold**: below this = wrong approach

  ## Algorithm / Approach
  [Pseudocode or numbered steps. Precise enough that the Coder needs no guessing.
   Include: method, loss, key hyperparameters, eval protocol. Max 10 lines.]

  ## Data Plan
  **Primary**: <name> · <absolute path or download URL> · <format>
  **Fallback**: <alternative> · <path or URL>
  (Files in {working_dir} are always ACCESSIBLE — use their absolute paths.
   For external sources, only list those confirmed ACCESSIBLE in 01_literature.md.)

  ## Expected Output Files
  - results/key_results.json  → {{"metric": <name>, "value": <float>, "baseline": <float>}}
  - results/main_plot.png
  - results/summary_figure.png

  ## FOR THE CODER
  [2 sentences max: where to start, the single most critical implementation detail,
   what "done" looks like.]
""",

"coder": """\
YOUR MISSION
Implement the experiment. Write real, working, runnable code.
Produce concrete results from real data.

STEPS
1. Read ONLY ## FOR THE CODER and ## Data Plan from {round_dir}/02_experiment.md.
2. Read ONLY ## Available Datasets from {round_dir}/01_literature.md to confirm
   which dataset URLs are VERIFIED ACCESSIBLE.
3. Read {research_dir}/hw_profile.json — hardware is already probed by the
   pipeline. Use cuda_available, cuda_devices[].vram_gb, ram_total_gb, cpu_count
   to set batch sizes, device placement, and parallelism. Do NOT re-probe.
4. Read any existing code in {round_dir}/03_code/ if this is a continuation.
5. Execute:
   a. Create {round_dir}/03_code/ directory.
   b. Download / access the verified dataset(s).
   c. Write modular Python. Install packages with uv (see below).
   d. Run the code. Fix runtime errors.
   e. Save ALL output (metrics, plots) to {round_dir}/03_code/results/.
6. Write {round_dir}/03_code/IMPLEMENTATION.md — keep it SHORT (under 300 words).
   STOP after writing IMPLEMENTATION.md. Your output is EXACTLY:
     - {round_dir}/03_code/<script>.py     (the implementation)
     - {round_dir}/03_code/IMPLEMENTATION.md
     - {round_dir}/03_code/results/*.json and *.png
   FORBIDDEN files (writing these is an error):
     - final_report.html  (Master Reporter's job — wrong role, wrong path)
     - 01_literature.md / 02_experiment.md  (Researcher / Experiment Designer's job)
     - 04_debug_report.md  (Debugger's job)
     - 05_evaluation.md   (Evaluator's job)
     - 06_synthesis.md    (Orchestrator's job — writing this will SKIP the Orchestrator)
     - 04_findings.md, README.md, or any other round-level summaries
   If you find yourself writing anything other than the listed files, STOP.
   - Hardware used (device, batch size chosen)
   - Data source + how it was accessed
   - Approach in 3–5 bullet points
   - Results summary (key numbers)
   - Any skipped steps + reason (see FAILURE PROTOCOL)

GPU RULES (if CUDA available per hw_profile.json — no exceptions)
- device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
- Move models AND tensors: .to(device). Log "Using device: {{device}}" at runtime.
- PyTorch: use autocast("cuda") + GradScaler; num_workers≥2; pin_memory=True.
- Batch size: target 70–80% of vram_gb from hw_profile.
- HuggingFace: device_map="auto". scikit-learn/XGBoost: device="cuda".
- Log peak_vram_gb to results/ via torch.cuda.max_memory_allocated()/1e9.

RESULTS ORDER — CRITICAL:
1. Save key_results.json FIRST (before any visualisation).
2. Save main_plot.png, summary_figure.png.
3. Run any optional extras (UMAP, etc.) LAST — if they fail, the core results are already on disk.
Never put visualisation code before the JSON save — a plot error must not erase your results.

VISUALISATION (save to {round_dir}/03_code/results/)
- Main results plot + summary_figure.png (2–4 subplot overview). Both required.
- 150 dpi PNG. Title, axis labels, legend. Use tight_layout() + savefig().
- Wrap UMAP or other optional visualisations in try/except so a missing package doesn't crash.

PACKAGES — hw_profile.json contains `uv_available` (bool).
- If True  → ALWAYS use uv. No exceptions. Preferred patterns:
    Option A (isolated):  uv venv && uv pip install <pkgs> && .venv/bin/python script.py
    Option B (inline run): uv run --with <pkg1> --with <pkg2> python script.py
  CRITICAL: If you used Option A (uv venv + uv pip install), you MUST run with
  `.venv/bin/python script.py`. Do NOT mix Option A setup with `uv run` execution —
  `uv run` ignores the local .venv and uses the project-root environment where your
  packages are not installed. Mixing patterns = ModuleNotFoundError.
  Never call `uv pip install` without first creating a venv (Option A) or using `uv run` (Option B).
  `uv pip install --system` is acceptable if the working dir already has system Python in PATH.
- If False → use pip and add a one-line note in IMPLEMENTATION.md: "uv not found, used pip".
Never silently fall back to pip when uv is available.

LONG-RUNNING JOBS — training a model can take hours or days. This is expected and correct.
- Pass an explicit `timeout` to every bash training call: estimate duration × 1.5, in seconds.
  Example: expected 2 h → `timeout=10800`. Expected overnight → `timeout=86400`.
- Do NOT use the shell `timeout` command (e.g. `timeout 3600 python ...`) — it does NOT exist
  on macOS and will immediately fail with "timeout: command not found". Instead, pass the
  timeout as the tool parameter to the bash call itself (the tool enforces it at the OS level).
- Do NOT kill a training job because it is slow. Let it run.
- If a job genuinely fails (non-zero exit, OOM) document it and try alternatives.

ABSOLUTE RULES — READ CAREFULLY
- NEVER generate synthetic or dummy data as a substitute for real data.
  Synthetic stand-ins are scientifically invalid and mislead future agents.
- NEVER fabricate results or outputs. Every number in results/ must come from
  real computation on real data.
- NEVER hardcode paths to files from previous rounds (e.g. round_001/) as data
  fallbacks. Prior-round files may be artefacts, test files, or placeholders — not
  validated data sources. If your primary URL fails, download fresh data from a
  different public URL or report the failure in IMPLEMENTATION.md.
- If a data source is unavailable (network error, API down, auth required):
    1. Log the failure clearly in IMPLEMENTATION.md under ## Skipped Steps.
    2. Do NOT proceed with that experiment using fake data.
    3. Instead: search for an alternative real dataset that tests the same
       hypothesis (web_search). Try at least 2–3 alternatives.
    4. If NO real data can be obtained for a given hypothesis, mark that
       experiment as BLOCKED in IMPLEMENTATION.md and pivot to a different
       hypothesis from {round_dir}/02_experiment.md that CAN use available data.
    5. If ALL hypotheses are blocked due to data access, implement the
       methodological scaffolding (data loading, model, evaluation pipeline)
       using a small well-known public benchmark (e.g. UCI, HuggingFace, NCBI)
       that IS accessible — document the substitution clearly.
- Quantitative results MUST be saved (JSON / CSV / text).
- Every script that IS run must complete without error.
- If an approach fails after 3 debugging attempts, pivot and document why.
""",

"debugger": """\
YOUR MISSION
Verify code correctness and result validity. Be skeptical. Total report: under 350 words.

STEPS — focus ONLY on {round_dir}. Do NOT read files from other rounds.
1. Read {round_dir}/03_code/IMPLEMENTATION.md (the ## Results Summary section only).
   Use grep to scan the main script in {round_dir}/03_code/ — do NOT read every line.
2. Check {round_dir}/03_code/results/ with list_dir.
   - If results/ has key_results.json AND at least one .png → results exist. DO NOT re-run the
     full script. Proceed to step 3 with the existing files.
   - If results/ is MISSING or EMPTY → run the main script. To run, first check how the Coder
     ran it: read IMPLEMENTATION.md for the run command. If uv was used, run with:
     `cd {round_dir}/03_code && uv run --with <pkgs> python <script>.py`
     or use the existing .venv: `.venv/bin/python <script>.py`
     Never run bare `python <script>.py` — it won't have the packages.
3. Check — each is a potential one-line report entry:
   - SYNTHETIC DATA: any fabricated/placeholder data instead of real → CRITICAL
   - GPU UNDERUSE: if hw_profile.json shows CUDA available but "Using device: cpu"
     appears in output → CRITICAL (fix: add .to(device), rerun)
   - Runtime errors, off-by-one, data leakage, wrong metrics
   - Results implausibly good/bad (may indicate fake data)
4. Fix CRITICAL bugs only (edit_file / bash). Re-run once to confirm.
   Non-critical style issues: document in Outstanding Issues, do NOT fix now.
5. Write 04_debug_report.md. Stop immediately after writing.

OUTPUT — write ONE file: {round_dir}/04_debug_report.md

  ## Bugs Found and Fixed  (one line per bug: what · fix · verified ✓/✗)
  ## Tests Run             (command + pass/fail, one line each)
  ## Verified Results      (key metric values copied from results/)
  ## Outstanding Issues    (unfixable problems only)
  ## Confidence Score      (0–10)

If no bugs: "No bugs found. Results verified." — then the score. Done.
""",

"evaluator": """\
YOUR MISSION
Independent assessment of this round's work. Critical, concise. Total report: under 400 words.

STEPS — follow in ORDER, do NOT skip ahead:
1. Read {round_dir}/03_code/IMPLEMENTATION.md (primary input — always exists).
   If {round_dir}/04_debug_report.md exists, read it too (may be absent — that's OK).
   Read {round_dir}/02_experiment.md ONLY for the success metric (first 20 lines).
   Do NOT read 01_literature.md unless you need a specific SOTA number.
2. Read {round_dir}/03_code/results/key_results.json if it exists. Check numbers.
3. WRITE {round_dir}/05_evaluation.md NOW — do NOT wait. This is your primary output.
   Use the format below. Estimate scores from what you've read so far.
4. ONLY after writing 05_evaluation.md: optionally write + run a chart script.
   Skip the chart entirely if reading + writing has used more than 8 iterations.

OUTPUT — The filename MUST be exactly "05_evaluation.md". Write it at step 3, not later.
Format: score on the SAME line as the heading, then ONE sentence commentary.

  ## Literature Quality      X/10 — <one sentence>
  ## Hypothesis Quality      X/10 — <one sentence>
  ## Implementation Quality  X/10 — <one sentence>
  ## Results Validity        X/10 — <one sentence>
  ## Overall Score           X/10
  ## Critical Weaknesses     (bullet list, max 3 items)
  ## Recommended Next Steps  (bullet list, max 3 specific actionable items)

SCORES CHART (OPTIONAL — only after 05_evaluation.md is written)
- Write + run a minimal Python script → saves {round_dir}/05_scores_chart.png.
- Simple bar chart, 4 bars, labels, colour-coded (green≥7, amber4–6, red≤3).
- If no results exist, skip the chart entirely.

SCORING RULES
- Synthetic/fabricated data → Results Validity capped at 1/10.
- Be harsh. A generous score on mediocre work wastes the next round's effort.
- Missing 04_debug_report.md is NOT a reason to delay writing 05_evaluation.md —
  evaluate based on IMPLEMENTATION.md and key_results.json alone.
""",

"orchestrator": """\
YOUR MISSION
Synthesise this round. Write the brief that drives the next round.
Total output file: under 500 words.

STEPS
1. Read PRIMARILY {round_dir}/05_evaluation.md — it already summarises the work.
   Read {round_dir}/03_code/IMPLEMENTATION.md for specific technical details only
   if the evaluation references something you need to clarify.
2. Read {research_dir}/findings.md (## What Failed section only) if round > 1.
3. Write ONE file: {round_dir}/06_synthesis.md. Do NOT touch findings.md.

STRUCTURE — short bullets, not paragraphs:

  ## Round Summary        (2–3 bullets)
  ## Key Findings         (2–3 bullets with numbers where possible)
  ## What Worked          (1–3 bullets)
  ## What Failed / Gaps   (1–3 bullets)
  ## Updated Research Direction  (1–2 sentences)

  Then ONE of:

  {next_brief_marker}
  [HARD LIMIT: 150 words. Specific tasks only — no summaries of what happened.
   Format: numbered list of concrete actions for the next round's agents.
   Include: which dataset, which method, which metric to beat, what to fix.]

  OR (only if score ≥ 8/10 AND findings are solid OR all directions exhausted):

  {complete_marker}
  [One sentence conclusion.]
""",

"reporter": """\
YOUR MISSION
Produce a self-contained HTML report for this round. Scientists open this to
quickly judge what was done, what was found, and what comes next.

STEPS
1. Read: 05_evaluation.md, 06_synthesis.md, 03_code/IMPLEMENTATION.md.
   Skim 01_literature.md and 02_experiment.md for titles/metrics only.
2. List *.png in {round_dir}/ and {round_dir}/03_code/results/.
3. Write {round_dir}/build_report.py (stdlib + matplotlib only). Run it.
   Confirm {round_dir}/07_report.html is non-empty.

HTML SECTIONS (in order):
  1. Sticky nav · 2. Header (round, topic, date, score badge)
  3. Executive Summary (4–5 bullets from synthesis)
  4. Experiment (hypothesis, success metric, data used)
  5. Implementation (approach bullets, data source, any skipped steps)
  6. Results & Plots (ALL PNGs base64-embedded, 2-col grid, 1-line captions)
  7. Evaluation (scores table colour-coded ≥7 green / 4–6 amber / ≤3 red;
     embed 05_scores_chart.png if it exists)
  8. Next Direction (NEXT_ROUND_BRIEF as callout box)
  9. Footer (round, topic, timestamp)

DESIGN: dark (#1a1a2e) header, white cards, Inter font (CDN OK), max-width
1100px, responsive 2-col plot grid. All images base64 — no external URLs.
Script: read files with open(), base64.b64encode() for PNGs, write HTML as
string. Print output path on success.
""",
}


_BRIEF_CAP = 800  # chars — truncate round brief for roles that only need direction

def _build_system_prompt(
    role: str,
    topic: str,
    round_num: int,
    max_rounds: int,
    round_dir: str,
    research_dir: str,
    working_dir: str,
    brief: str,
    is_final: bool = False,
) -> str:
    role_cfg = ROLES[role]
    final_tag = "← FINAL ROUND — prioritise conclusions over exploration" if is_final else ""
    # Cap brief length for roles that only need the direction, not full synthesis prose
    if role not in ("orchestrator", "reporter") and len(brief) > _BRIEF_CAP:
        brief = brief[:_BRIEF_CAP].rstrip() + "\n…[brief truncated — read findings.md for full context]"
    header = _SHARED_HEADER.format(
        label=role_cfg["label"],
        topic=topic,
        round_num=round_num,
        max_rounds=max_rounds,
        final_tag=final_tag,
        round_dir=round_dir,
        research_dir=research_dir,
        working_dir=working_dir,
        brief=brief,
    )
    body = _ROLE_PROMPTS[role].format(
        round_dir=round_dir,
        research_dir=research_dir,
        working_dir=working_dir,
        next_brief_marker=NEXT_BRIEF_MARKER,
        complete_marker=COMPLETE_MARKER,
    )
    return header + body


# ---------------------------------------------------------------------------
# Filtered tool list per role
# ---------------------------------------------------------------------------

def _tools_for_role(role: str) -> list[dict]:
    allowed = set(ROLES[role]["tools"])
    return [t for t in TOOL_DEFINITIONS if t["function"]["name"] in allowed]


# ---------------------------------------------------------------------------
# Core specialist agent loop (mirrors agent._agent_loop with custom tools)
# ---------------------------------------------------------------------------

def _stream_completion_with_tools(
    client: OpenAI,
    model: str,
    messages: list[dict],
    tools: list[dict],
) -> dict:
    """Stream one turn. Returns {content, tool_calls, finish_reason}."""
    content_parts: list[str] = []
    tool_call_map: dict[int, dict] = {}
    finish_reason = "stop"

    display.stream_start()

    try:
        with client.chat.completions.create(
            model=model,
            messages=messages,
            tools=tools,
            tool_choice="auto",
            stream=True,
        ) as stream:
            for chunk in stream:
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                delta = choice.delta

                if delta.content:
                    content_parts.append(delta.content)
                    display.stream_chunk(delta.content)

                if delta.tool_calls:
                    for tc in delta.tool_calls:
                        idx = tc.index
                        if idx not in tool_call_map:
                            tool_call_map[idx] = {
                                "id": "", "type": "function",
                                "function": {"name": "", "arguments": ""},
                            }
                        slot = tool_call_map[idx]
                        if tc.id:
                            slot["id"] = tc.id
                        if tc.function:
                            if tc.function.name:
                                slot["function"]["name"] += tc.function.name
                            if tc.function.arguments:
                                slot["function"]["arguments"] += tc.function.arguments

                if choice.finish_reason:
                    finish_reason = choice.finish_reason
    except BadRequestError as e:
        display.stream_end(False)
        raise

    had_content = bool(content_parts)
    display.stream_end(had_content)

    return {
        "content": "".join(content_parts),
        "tool_calls": [tool_call_map[i] for i in sorted(tool_call_map)],
        "finish_reason": finish_reason,
    }


def _run_specialist(
    role: str,
    model: str,
    topic: str,
    round_num: int,
    max_rounds: int,
    round_dir: str,
    research_dir: str,
    working_dir: str,
    brief: str,
    client: OpenAI,
) -> bool:
    """
    Run one specialist agent for one round.
    Returns True on success, False if a fatal error occurred.
    """
    cfg = ROLES[role]
    tools = _tools_for_role(role)
    max_iter = cfg["max_iter"]

    display.print_agent_banner(role, model, round_num, max_rounds)

    system_prompt = _build_system_prompt(
        role, topic, round_num, max_rounds,
        round_dir, research_dir, working_dir, brief,
        is_final=(round_num == max_rounds),
    )

    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": (
            f"Round {round_num}: carry out your role. "
            f"Write all outputs to {round_dir}. "
            "When you are done, stop calling tools."
        )},
    ]

    t0 = time.time()
    iteration = 0
    _rate_limit_retries = 0

    while iteration < max_iter:
        iteration += 1
        try:
            response = _stream_completion_with_tools(client, model, messages, tools)
            _rate_limit_retries = 0  # reset on success
        except BadRequestError as e:
            err = str(e)
            if "ContextWindow" in err or "context" in err.lower():
                display.print_error(
                    f"[{cfg['label']}] Context window exceeded — "
                    "trimming oldest tool results and retrying."
                )
                messages = _trim_messages(messages)
                iteration -= 1  # context trim doesn't consume a turn
                continue
            display.print_error(f"[{cfg['label']}] API error: {e}")
            return False
        except Exception as e:
            err_str = str(e)
            if "429" in err_str or "rate" in err_str.lower() or "RateLimit" in type(e).__name__:
                _rate_limit_retries += 1
                wait = min(60, 5 * (2 ** (_rate_limit_retries - 1)))  # 5s, 10s, 20s, 40s, 60s cap
                display.print_info(
                    f"  [{cfg['label']}] Rate limit hit — waiting {wait}s before retry "
                    f"({_rate_limit_retries}/5)."
                )
                if _rate_limit_retries > 5:
                    display.print_error(f"[{cfg['label']}] Rate limit persists after 5 retries. Aborting.")
                    return False
                time.sleep(wait)
                iteration -= 1  # don't count this as a used iteration
                continue
            display.print_error(f"[{cfg['label']}] Unexpected error: {e}")
            return False
        except KeyboardInterrupt:
            display.stream_end(False)
            display.console.print("\n[dim]Interrupted.[/dim]")
            raise

        content = response["content"]
        tool_calls = response["tool_calls"]
        finish_reason = response["finish_reason"]

        assistant_msg: dict = {"role": "assistant", "content": content or None}
        if tool_calls:
            assistant_msg["tool_calls"] = tool_calls
        messages.append(assistant_msg)

        if not tool_calls:
            # Model returned text with no tool calls — only exit if expected output exists.
            import pathlib as _pl
            expected_rel = OUTPUT_FILES.get(role, "")
            if expected_rel:
                expected_abs = _pl.Path(round_dir) / expected_rel
                if expected_rel.endswith("/"):
                    output_done = (expected_abs / "IMPLEMENTATION.md").exists()
                else:
                    output_done = expected_abs.exists()
            else:
                output_done = True
            if output_done:
                break
            # Output not yet written — nudge the model to write it
            exact_path = str(_pl.Path(round_dir) / expected_rel)
            nudge = (
                f"REQUIRED ACTION: You have not yet written the output file for THIS round. "
                f"The EXACT path you must write is: {exact_path}\n"
                f"Call write_file with file_path=\"{exact_path}\" RIGHT NOW. "
                "Do not write to any other path. Do not write to a different round's directory."
            )
            messages.append({"role": "user", "content": nudge})
            iteration -= 1  # don't count the nudge against the iteration budget
            continue

        if finish_reason == "stop":
            break

        display.print_separator()
        for tc in tool_calls:
            name = tc["function"]["name"]
            try:
                args = json.loads(tc["function"]["arguments"] or "{}")
            except json.JSONDecodeError:
                args = {}

            display.print_tool_call(name, args)
            result, success = execute_tool(name, args, working_dir)
            result = _cap_result(result, name)
            display.print_tool_result(name, result, success)

            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": result,
            })
        display.print_separator()

    elapsed = time.time() - t0
    display.print_agent_done(role, elapsed, iteration)
    return True


# ---------------------------------------------------------------------------
# Parallel specialist runner + merger
# ---------------------------------------------------------------------------

def _run_parallel_specialists(
    role: str,
    n: int,
    models: list[str],
    topic: str,
    round_num: int,
    max_rounds: int,
    round_dir: Path,
    research_dir: str,
    working_dir: str,
    brief: str,
    client: OpenAI,
) -> list[Path]:
    """
    Run n independent copies of role in parallel, each writing to
    round_dir/{role}_{i}/. Returns paths to outputs that were written.
    """
    def _run_one(i: int) -> Path | None:
        sub_dir = round_dir / f"{role}_{i}"
        sub_dir.mkdir(exist_ok=True)
        model = models[(i - 1) % len(models)]
        _run_specialist(
            role=role,
            model=model,
            topic=topic,
            round_num=round_num,
            max_rounds=max_rounds,
            round_dir=str(sub_dir),
            research_dir=research_dir,
            working_dir=working_dir,
            brief=brief,
            client=client,
        )
        out = sub_dir / OUTPUT_FILES[role]
        return out if out.exists() else None

    results: list[Path] = []
    with ThreadPoolExecutor(max_workers=n) as pool:
        futures = {pool.submit(_run_one, i): i for i in range(1, n + 1)}
        for f in as_completed(futures):
            path = f.result()
            if path:
                results.append(path)
    return results


def _run_merger(
    role: str,
    parallel_outputs: list[Path],
    canonical_path: Path,
    round_num: int,
    max_rounds: int,
    round_dir: str,
    research_dir: str,
    working_dir: str,
    client: OpenAI,
) -> bool:
    """
    Reconcile parallel agent outputs into one canonical file.
    Returns True if canonical_path was written successfully.
    """
    cfg = ROLES["merger"]
    tools = _tools_for_role("merger")
    paths_block = "\n".join(f"  {i + 1}. {p}" for i, p in enumerate(parallel_outputs))
    role_label = ROLES[role]["label"]

    system_prompt = (
        f"You are the Merger in OctoSlave's autonomous research pipeline.\n\n"
        f"ROUND          : {round_num} / {max_rounds}\n"
        f"ROUND DIR      : {round_dir}\n"
        f"RESEARCH DIR   : {research_dir}\n"
        f"WORKING DIR    : {working_dir}\n\n"
        f"YOUR MISSION\n"
        f"Reconcile {len(parallel_outputs)} independent {role_label} outputs into one\n"
        f"authoritative canonical file. Read every parallel output, identify where\n"
        f"agents agree and diverge, and write the best synthesis.\n\n"
        f"PARALLEL OUTPUTS\n{paths_block}\n\n"
        f"CANONICAL OUTPUT: {canonical_path}\n\n"
        f"RECONCILIATION RULES\n"
        f"- Preserve all unique insights that appear in any single output.\n"
        f"- Where agents agree, state the consensus confidently.\n"
        f"- Where agents disagree, present the strongest position or note both.\n"
        f"- For EVALUATOR outputs: average numeric scores; flag any dimension where\n"
        f"  scores differ by more than 2 points as (DISPUTED).\n"
        f"- For RESEARCHER outputs: merge datasets and references without duplication;\n"
        f"  prefer sources that multiple agents identified independently.\n"
        f"- For HYPOTHESIS outputs: adopt the stronger design fully, or hybridise if\n"
        f"  both have complementary strengths. Produce one clear experiment spec.\n"
        f"- Use the same section structure as the individual outputs.\n"
        f"- Write ONLY the canonical output file — no other files.\n"
    )

    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": (
            f"Read all parallel outputs listed above, then write the merged result "
            f"to {canonical_path}. When done, stop calling tools."
        )},
    ]

    display.print_agent_banner("merger", cfg["default_model"], round_num, max_rounds)

    t0 = time.time()
    iteration = 0
    for iteration in range(1, cfg["max_iter"] + 1):
        try:
            response = _stream_completion_with_tools(client, cfg["default_model"], messages, tools)
        except BadRequestError as e:
            err = str(e)
            if "ContextWindow" in err or "context" in err.lower():
                messages = _trim_messages(messages)
                continue
            display.print_error(f"[Merger] API error: {e}")
            return False
        except KeyboardInterrupt:
            display.stream_end(False)
            raise

        content = response["content"]
        tool_calls = response["tool_calls"]
        finish_reason = response["finish_reason"]

        assistant_msg: dict = {"role": "assistant", "content": content or None}
        if tool_calls:
            assistant_msg["tool_calls"] = tool_calls
        messages.append(assistant_msg)

        if not tool_calls or finish_reason == "stop":
            break

        display.print_separator()
        for tc in tool_calls:
            name = tc["function"]["name"]
            try:
                args = json.loads(tc["function"]["arguments"] or "{}")
            except json.JSONDecodeError:
                args = {}
            display.print_tool_call(name, args)
            result, success = execute_tool(name, args, working_dir)
            result = _cap_result(result, name)
            display.print_tool_result(name, result, success)
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
        display.print_separator()

    elapsed = time.time() - t0
    display.print_agent_done("merger", elapsed, iteration)
    return canonical_path.exists()


# ---------------------------------------------------------------------------
# findings.md updater — called by the pipeline, not the LLM
# ---------------------------------------------------------------------------

def _update_findings(
    research_dir: str,
    round_num: int,
    round_dir: str,
    topic: str,
) -> None:
    """
    Append a structured entry for this round to findings.md.
    Reads from the round's output files directly — does not rely on the LLM.
    Called by the pipeline after the orchestrator finishes each round.
    """
    findings_path = Path(research_dir) / FINDINGS_FILE

    # Collect content from available round outputs
    def _read(rel: str) -> str:
        p = Path(round_dir) / rel
        if p.exists():
            try:
                return p.read_text(errors="replace").strip()
            except OSError:
                return ""
        return ""

    synthesis   = _read(OUTPUT_FILES["orchestrator"])
    evaluation  = _read(OUTPUT_FILES["evaluator"])
    experiment  = _read(OUTPUT_FILES["hypothesis"])

    # Extract overall score from evaluation.
    # Handles two formats:
    #   "## Overall Score           X/10"  (score on SAME line as heading)
    #   "## Overall Score\nX/10"           (score on NEXT line)
    score_match = re.search(
        r"##\s*Overall Score\s+(\d+(?:\.\d+)?/\d+|\d+(?:\.\d+)?\s*/\s*\d+)",
        evaluation,
    )
    if not score_match:
        score_match = re.search(r"##\s*Overall Score[^\n]*\n+([^\n]+)", evaluation)
    score_str = score_match.group(1).strip() if score_match else "N/A"

    # Extract key findings / summary block from synthesis (## Key Findings section)
    kf_match = re.search(
        r"##\s*Key Findings\s*\n(.*?)(?:\n##|\Z)", synthesis, re.DOTALL
    )
    key_findings = kf_match.group(1).strip() if kf_match else synthesis[:800].strip()

    # Extract what worked / what failed
    ww_match = re.search(r"##\s*What Worked\s*\n(.*?)(?:\n##|\Z)", synthesis, re.DOTALL)
    wf_match = re.search(r"##\s*What Failed[^\n]*\n(.*?)(?:\n##|\Z)", synthesis, re.DOTALL)
    what_worked = ww_match.group(1).strip() if ww_match else ""
    what_failed = wf_match.group(1).strip() if wf_match else ""

    # Extract experiment name + hypothesis from new-format experiment file
    # Supports: "## Experiment: <name>" with "**Hypothesis**: ..."
    exp_name_match = re.search(r"##\s*Experiment:\s*(.+)", experiment)
    hyp_match      = re.search(r"\*\*Hypothesis\*\*:\s*(.+)", experiment)
    if exp_name_match and hyp_match:
        recommended = f"{exp_name_match.group(1).strip()} — {hyp_match.group(1).strip()}"
    elif exp_name_match:
        recommended = exp_name_match.group(1).strip()
    else:
        recommended = experiment[:300].strip()

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    entry_lines = [
        f"\n\n---\n\n## Round {round_num}  ·  {timestamp}",
        f"\n**Overall score:** {score_str}",
    ]
    if recommended:
        entry_lines.append(f"\n**Experiment:** {recommended[:300]}")
    if key_findings:
        entry_lines.append(f"\n\n### Key Findings\n\n{key_findings}")
    if what_worked:
        entry_lines.append(f"\n\n### What Worked\n\n{what_worked}")
    if what_failed:
        entry_lines.append(f"\n\n### What Failed / Gaps\n\n{what_failed}")

    entry = "".join(entry_lines)

    # Create file with header if missing, otherwise append
    if not findings_path.exists():
        header = (
            f"# Research Findings: {topic}\n\n"
            f"_Automatically updated after each round by OctoSlave._\n"
        )
        findings_path.write_text(header + entry, encoding="utf-8")
    else:
        with open(findings_path, "a", encoding="utf-8") as f:
            f.write(entry)

    display.print_info(f"  findings.md updated (round {round_num})")


# ---------------------------------------------------------------------------
# Overseer: parse synthesis for next brief and completion signal
# ---------------------------------------------------------------------------

def _parse_synthesis(synthesis_path: str) -> tuple[str, bool]:
    """
    Read the orchestrator's synthesis file.
    Returns (next_brief: str, is_complete: bool).
    """
    path = Path(synthesis_path)
    if not path.exists():
        return "Continue the research with improvements based on previous round.", False

    text = path.read_text(errors="replace")

    if COMPLETE_MARKER in text:
        return "", True

    match = re.search(
        rf"{re.escape(NEXT_BRIEF_MARKER)}\s*(.*?)(?:\n## |\Z)",
        text,
        re.DOTALL,
    )
    if match:
        brief = match.group(1).strip()
        if brief:
            return brief, False

    # Fallback: use last 1500 chars of synthesis as implicit brief
    return text[-1500:].strip(), False


# ---------------------------------------------------------------------------
# Context trimmer (last-resort when context window fills up)
# ---------------------------------------------------------------------------

def _trim_messages(messages: list[dict], groups: int = 3) -> list[dict]:
    """
    Remove the oldest N complete assistant-turn groups (assistant message +
    all its tool results) to free context space.  Always preserves the system
    prompt and the first user message (messages[:2]).

    Removes `groups` turns per call so recovery from a deeply-full context is
    fast rather than requiring many retries.
    """
    system = messages[:2]
    rest   = list(messages[2:])

    removed = 0
    while removed < groups and rest:
        # Find the first assistant message that issued tool calls
        start = next(
            (i for i, m in enumerate(rest)
             if m.get("role") == "assistant" and m.get("tool_calls")),
            None,
        )
        if start is None:
            break  # no more tool-calling turns to trim

        # Collect the contiguous block of tool results that follow it
        end = start + 1
        while end < len(rest) and rest[end].get("role") == "tool":
            end += 1

        # Drop the assistant turn + its tool results
        rest = rest[:start] + rest[end:]
        removed += 1

    return system + rest


# ---------------------------------------------------------------------------
# Master HTML report (runs once after all rounds complete)
# ---------------------------------------------------------------------------

_MASTER_REPORTER_PROMPT = """\
You are the Master Reporter for an autonomous multi-agent research run.

TOPIC     : {topic}
ROUNDS    : {rounds_done}
RESEARCH  : {research_dir}

YOUR MISSION
One comprehensive, self-contained HTML report covering the full research run.
This is the definitive deliverable — spend your tokens here, not on intermediary prose.

STEPS
1. List round directories under {research_dir}/.
2. For each round read ONLY: round_NNN/05_evaluation.md, round_NNN/06_synthesis.md,
   round_NNN/03_code/IMPLEMENTATION.md (if exists).
   Read round_NNN/02_experiment.md only for the hypothesis name and success metric.
3. Read {research_dir}/findings.md.
4. Collect all summary_figure.png and 05_scores_chart.png from each round.
   Also list any other PNGs in round_NNN/03_code/results/.
5. Write {research_dir}/build_master_report.py. Run it.
   Must produce {research_dir}/final_report.html.

HTML SECTIONS:
  1. Sticky nav
  2. Title block (topic, date, rounds, quality badge)
  3. Abstract (1 paragraph — entire research arc)
  4. Research Timeline table: Round | Hypothesis | Score | Status
  5. Cumulative Findings (from findings.md, as cards)
  6. Round Deep Dives — one <details> per round:
       hypothesis · implementation summary · ALL result plots (2-col, base64)
       · scores chart · what worked / failed
  7. Score Progression chart (generate with matplotlib: round on x, score on y)
  8. Key Visualisations Gallery (summary_figure.png from each round, full-width)
  9. Conclusions & Next Steps (from final synthesis)
  Footer: topic · timestamp · "Generated by OctoSlave"

DESIGN: dark header (#0d1117), white cards, Inter (CDN OK), max-width 1200px,
base64 all images, collapsible rounds via <details>/<summary>.
Script: stdlib + matplotlib only. Print output path on success.
"""

_MASTER_REPORTER_SYSTEM = """\
You are an expert scientific report writer. You produce polished, self-contained
HTML research reports. You write clean Python scripts that generate these reports.
Working directory: {working_dir}
"""


def _run_master_reporter(
    topic: str,
    research_dir: str,
    rounds_done: int,
    working_dir: str,
    client: OpenAI,
    model: str,
) -> None:
    """Generate the final master HTML report covering all rounds."""
    import pathlib as _pl
    cfg = ROLES["reporter"]
    tools = _tools_for_role("reporter")

    # Remove stale report so the reporter always regenerates fresh
    stale = _pl.Path(research_dir) / "final_report.html"
    if stale.exists():
        stale.unlink()

    display.print_agent_banner("reporter", model, rounds_done, rounds_done)
    display.print_info("  Generating master report…")

    system = _MASTER_REPORTER_SYSTEM.format(working_dir=working_dir)
    user_task = _MASTER_REPORTER_PROMPT.format(
        topic=topic,
        rounds_done=rounds_done,
        research_dir=research_dir,
    )

    messages: list[dict] = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_task},
    ]

    t0 = time.time()
    iteration = 0
    _rate_limit_retries = 0

    while iteration < cfg["max_iter"]:
        iteration += 1
        try:
            response = _stream_completion_with_tools(client, model, messages, tools)
            _rate_limit_retries = 0
        except BadRequestError as e:
            err = str(e)
            if "ContextWindow" in err or "context" in err.lower():
                messages = _trim_messages(messages)
                iteration -= 1
                continue
            display.print_error(f"[Master Reporter] API error: {e}")
            return
        except Exception as e:
            err_str = str(e)
            if "429" in err_str or "rate" in err_str.lower() or "RateLimit" in type(e).__name__:
                _rate_limit_retries += 1
                wait = min(60, 5 * (2 ** (_rate_limit_retries - 1)))
                display.print_info(f"[Master Reporter] Rate limit — waiting {wait}s ({_rate_limit_retries}/5).")
                if _rate_limit_retries > 5:
                    display.print_error("[Master Reporter] Rate limit persists. Aborting.")
                    return
                time.sleep(wait)
                iteration -= 1
                continue
            display.print_error(f"[Master Reporter] Unexpected error: {e}")
            return
        except KeyboardInterrupt:
            display.stream_end(False)
            display.console.print("\n[dim]Master report interrupted.[/dim]")
            return

        content = response["content"]
        tool_calls = response["tool_calls"]
        finish_reason = response["finish_reason"]

        assistant_msg: dict = {"role": "assistant", "content": content or None}
        if tool_calls:
            assistant_msg["tool_calls"] = tool_calls
        messages.append(assistant_msg)

        if not tool_calls:
            # Only exit early if final_report.html has been produced.
            import pathlib as _pl2
            if (_pl2.Path(research_dir) / "final_report.html").exists():
                break
            # Not written yet — nudge the model
            nudge = (
                f"You have not yet written {research_dir}/final_report.html. "
                "Write build_master_report.py and run it, OR write final_report.html directly. "
                "Call write_file or bash now."
            )
            messages.append({"role": "user", "content": nudge})
            continue

        if finish_reason == "stop":
            break

        display.print_separator()
        for tc in tool_calls:
            name = tc["function"]["name"]
            try:
                args = json.loads(tc["function"]["arguments"] or "{}")
            except json.JSONDecodeError:
                args = {}
            display.print_tool_call(name, args)
            result, success = execute_tool(name, args, working_dir)
            result = _cap_result(result, name)
            display.print_tool_result(name, result, success)
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
        display.print_separator()

    elapsed = time.time() - t0
    display.print_agent_done("reporter", elapsed, iteration)

    final_report = Path(research_dir) / "final_report.html"
    if final_report.exists():
        display.print_info(
            f"  [bold bright_cyan]Master report → {final_report}[/bold bright_cyan]"
        )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def _probe_hardware(research_dir: str) -> dict:
    """
    Run a hardware probe and write hw_profile.json to research_dir.
    Returns the profile dict. Safe to call even if torch/psutil are absent.
    """
    import subprocess as _sp
    hw_path = Path(research_dir) / "hw_profile.json"

    script = (
        "import json, platform, os, sys, shutil, subprocess as _sp\n"
        "info = {'python': sys.version.split()[0], 'platform': platform.platform(), "
        "'cpu_count': os.cpu_count()}\n"
        # UV availability
        "uv_path = shutil.which('uv')\n"
        "if uv_path:\n"
        "    try:\n"
        "        v = _sp.run(['uv', '--version'], capture_output=True, text=True, timeout=5)\n"
        "        info['uv_available'] = True\n"
        "        info['uv_version'] = v.stdout.strip()\n"
        "    except Exception:\n"
        "        info['uv_available'] = True\n"
        "        info['uv_version'] = 'unknown'\n"
        "else:\n"
        "    info['uv_available'] = False\n"
        "    info['uv_version'] = None\n"
        "try:\n"
        "    import psutil; m = psutil.virtual_memory()\n"
        "    info['ram_total_gb'] = round(m.total/1e9,1)\n"
        "    info['ram_available_gb'] = round(m.available/1e9,1)\n"
        "except ImportError: pass\n"
        "try:\n"
        "    import torch\n"
        "    info['torch_version'] = torch.__version__\n"
        "    info['cuda_available'] = torch.cuda.is_available()\n"
        "    if torch.cuda.is_available():\n"
        "        info['cuda_device_count'] = torch.cuda.device_count()\n"
        "        info['cuda_devices'] = [{'name': torch.cuda.get_device_name(i), "
        "'vram_gb': round(torch.cuda.get_device_properties(i).total_memory/1e9,1)} "
        "for i in range(torch.cuda.device_count())]\n"
        "        info['cuda_version'] = torch.version.cuda\n"
        "except ImportError:\n"
        "    info['torch_available'] = False\n"
        "try:\n"
        "    r = _sp.run(['nvidia-smi','--query-gpu=name,memory.total,memory.free',"
        "'--format=csv,noheader,nounits'], capture_output=True, text=True, timeout=5)\n"
        "    if r.returncode==0: info['nvidia_smi'] = r.stdout.strip()\n"
        "except Exception: pass\n"
        "print(json.dumps(info))\n"
    )

    profile: dict = {}
    try:
        import shutil as _shutil
        _py = _shutil.which("python3") or _shutil.which("python") or "python3"
        result = _sp.run(
            [_py, "-c", script],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0 and result.stdout.strip():
            profile = json.loads(result.stdout.strip())
    except Exception:
        pass

    hw_path.write_text(json.dumps(profile, indent=2), encoding="utf-8")

    # Pretty-print hardware summary
    cuda = profile.get("cuda_available", False)
    devices = profile.get("cuda_devices", [])
    ram = profile.get("ram_total_gb", "?")
    cpus = profile.get("cpu_count", "?")
    uv_ok = profile.get("uv_available", False)
    uv_ver = profile.get("uv_version") or ""

    uv_tag = (
        f"[bold bright_green]uv ✓[/bold bright_green] ({uv_ver})"
        if uv_ok
        else "[bold red]uv ✗ — agents will fall back to pip[/bold red]"
    )

    if cuda and devices:
        gpu_str = ", ".join(f"{d['name']} ({d['vram_gb']} GB)" for d in devices)
        display.print_info(f"  Hardware: {cpus} CPU cores, {ram} GB RAM, "
                           f"[bold bright_green]CUDA ✓[/bold bright_green] {gpu_str}  |  {uv_tag}")
    else:
        display.print_info(f"  Hardware: {cpus} CPU cores, {ram} GB RAM, "
                           f"[dim]no CUDA GPU detected[/dim]  |  {uv_tag}")

    return profile


def run_long_research(
    topic: str,
    working_dir: str,
    client: OpenAI,
    max_rounds: int = 5,
    model_overrides: dict[str, str] | None = None,
    resume: bool = False,
    num_parallel: int = 1,
) -> None:
    """
    Run the full autonomous multi-agent research pipeline.

    Args:
        topic:           The research topic / goal.
        working_dir:     The project working directory.
        client:          Authenticated OpenAI client.
        max_rounds:      Maximum number of research rounds.
        model_overrides: Per-role model overrides, e.g. {"coder": "qwen3-coder-30b"}.
        resume:          If True, skip rounds whose output files already exist.
        num_parallel:    Number of independent agent copies to run for parallelisable
                         roles (researcher, hypothesis, evaluator). Default 1 = sequential.
    """
    overrides = model_overrides or {}
    research_dir = Path(working_dir) / "research"
    research_dir.mkdir(parents=True, exist_ok=True)

    display.print_research_start(topic, max_rounds, ROLES, overrides)

    # Probe hardware once; result is written to research_dir/hw_profile.json
    # and read by the coder/debugger agents in every subsequent round.
    _probe_hardware(str(research_dir))

    # Scan working directory for user-supplied local files (PDFs, CSVs, data, etc.)
    # Include them in the brief so every agent knows they exist from round 1.
    _LOCAL_DATA_EXTENSIONS = {
        ".pdf", ".csv", ".tsv", ".fasta", ".fa", ".fastq",
        ".json", ".jsonl", ".xlsx", ".xls", ".parquet", ".h5", ".hdf5",
        ".txt", ".bed", ".vcf", ".gff", ".gtf",
    }
    local_files = [
        p for p in Path(working_dir).iterdir()
        if p.is_file() and p.suffix.lower() in _LOCAL_DATA_EXTENSIONS
    ]
    local_files_block = ""
    if local_files:
        file_list = "\n".join(f"  - {p.name}  ({p.stat().st_size // 1024 or 1} KB)" for p in local_files)
        local_files_block = (
            f"\n\nLOCAL FILES IN WORKING DIR — provided by the user as primary input:\n"
            f"{file_list}\n"
            "Agents MUST read these files before doing any web searches. "
            "They take precedence over anything found online."
        )
        display.print_info(
            f"  Local data files detected: {', '.join(p.name for p in local_files)}"
        )

    # Initial brief
    brief = (
        f"Initial research round. Conduct a broad literature survey on: {topic}\n"
        "Identify key papers, available datasets, existing methods, and open problems.\n"
        "Generate first hypotheses and implement the most promising experiment."
        f"{local_files_block}"
    )

    completed_early = False

    for round_num in range(1, max_rounds + 1):
        round_dir = research_dir / f"round_{round_num:03d}"
        round_dir.mkdir(parents=True, exist_ok=True)

        display.print_round_header(round_num, max_rounds, str(round_dir))

        for role in PIPELINE:
            model = overrides.get(role) or ROLES[role]["default_model"]

            # Resumability: skip if canonical output already exists
            expected_path = round_dir / OUTPUT_FILES[role]
            if resume and expected_path.exists():
            # Resumability: skip if output already exists (and is non-empty for coder)
            expected = OUTPUT_FILES[role]
            expected_path = round_dir / expected
            output_complete = expected_path.exists()
            if output_complete and role == "coder":
                # For the coder, the directory must contain IMPLEMENTATION.md to be valid
                output_complete = (expected_path / "IMPLEMENTATION.md").exists()
            if resume and output_complete:
                display.print_info(
                    f"  ↩  {ROLES[role]['label']} output found — skipping."
                )
                continue

            try:
                if num_parallel > 1 and role in PARALLEL_ROLES:
                    display.print_info(
                        f"  ⚡ Spawning {num_parallel} parallel "
                        f"{ROLES[role]['label']} agents…"
                    )
                    parallel_outputs = _run_parallel_specialists(
                        role=role,
                        n=num_parallel,
                        models=[model] * num_parallel,
                        topic=topic,
                        round_num=round_num,
                        max_rounds=max_rounds,
                        round_dir=round_dir,
                        research_dir=str(research_dir),
                        working_dir=working_dir,
                        brief=brief,
                        client=client,
                    )
                    if parallel_outputs:
                        _run_merger(
                            role=role,
                            parallel_outputs=parallel_outputs,
                            canonical_path=expected_path,
                            round_num=round_num,
                            max_rounds=max_rounds,
                            round_dir=str(round_dir),
                            research_dir=str(research_dir),
                            working_dir=working_dir,
                            client=client,
                        )
                    else:
                        display.print_error(
                            f"All parallel {ROLES[role]['label']} agents failed "
                            f"in round {round_num}. Continuing."
                        )
                else:
                    ok = _run_specialist(
                        role=role,
                        model=model,
                        topic=topic,
                        round_num=round_num,
                        max_rounds=max_rounds,
                        round_dir=str(round_dir),
                        research_dir=str(research_dir),
                        working_dir=working_dir,
                        brief=brief,
                        client=client,
                    )
                    if not ok:
                        display.print_error(
                            f"{ROLES[role]['label']} failed in round {round_num}. "
                            "Continuing with next agent."
                        )
            except KeyboardInterrupt:
                display.console.print(
                    "\n[bold yellow]Research paused.[/bold yellow] "
                    f"Progress saved to [dim]{research_dir}[/dim]\n"
                    "Re-run with [cyan]/long-research ... --resume[/cyan] to continue."
                )
                return

            if not ok:
                display.print_error(
                    f"{ROLES[role]['label']} failed in round {round_num}. "
                    "Continuing with next agent."
                )

            # Structural integrity check: warn if expected output file is missing
            expected_out = round_dir / OUTPUT_FILES[role]
            if role != "coder":  # coder output is a directory, not a single file
                if not expected_out.exists():
                    display.print_info(
                        f"  [yellow]⚠ {ROLES[role]['label']}: expected output "
                        f"{OUTPUT_FILES[role]} not found after {ROLES[role]['max_iter']} "
                        f"iterations. Next role will proceed without it.[/yellow]"
                    )

        # Update findings.md from round outputs — pipeline-owned, not LLM-owned
        _update_findings(
            research_dir=str(research_dir),
            round_num=round_num,
            round_dir=str(round_dir),
            topic=topic,
        )

        # Parse orchestrator synthesis → next brief
        synthesis_path = round_dir / OUTPUT_FILES["orchestrator"]
        brief, is_complete = _parse_synthesis(str(synthesis_path))

        if is_complete:
            _run_master_reporter(
                topic=topic,
                research_dir=str(research_dir),
                rounds_done=round_num,
                working_dir=working_dir,
                client=client,
                model=overrides.get("reporter") or ROLES["reporter"]["default_model"],
            )
            display.print_research_complete(round_num, str(research_dir))
            completed_early = True
            break

        display.print_round_done(round_num, str(round_dir))

    if not completed_early:
        _run_master_reporter(
            topic=topic,
            research_dir=str(research_dir),
            rounds_done=max_rounds,
            working_dir=working_dir,
            client=client,
            model=overrides.get("reporter") or ROLES["reporter"]["default_model"],
        )
        display.print_research_complete(max_rounds, str(research_dir))
