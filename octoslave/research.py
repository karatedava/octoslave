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
        "default_model": "qwen3.5-122b",           # large model — thorough web reading
        "max_iter": 30,
        "tools": ["read_file", "write_file", "web_search", "web_fetch",
                  "list_dir", "glob", "bash"],
    },
    "hypothesis": {
        "label": "Hypothesis Generator",
        "icon": "💡",
        "color": "bold bright_magenta",
        "default_model": "deepseek-v3.2-thinking",  # extended thinking — bold hypotheses
        "max_iter": 20,
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
        "default_model": "qwen3-coder-30b",         # same large coder — deep inspection
        "max_iter": 30,
        "tools": ["read_file", "write_file", "edit_file", "bash",
                  "glob", "grep", "list_dir"],
    },
    "evaluator": {
        "label": "Evaluator",
        "icon": "⚖️ ",
        "color": "bold yellow",
        "default_model": "deepseek-v3.2-thinking",  # extended thinking — rigorous critique
        "max_iter": 20,
        "tools": ["read_file", "bash", "write_file", "list_dir",
                  "web_search", "glob"],
    },
    "orchestrator": {
        "label": "Orchestrator",
        "icon": "🧠",
        "color": "bold bright_white",
        "default_model": "deepseek-v3.2",           # strong reasoning — solid synthesis
        "max_iter": 20,
        "tools": ["read_file", "write_file", "list_dir", "glob"],
    },
    "reporter": {
        "label": "Reporter",
        "icon": "📊",
        "color": "bold bright_cyan",
        "default_model": "gpt-oss-120b",            # large general — clean HTML/writing
        "max_iter": 25,
        "tools": ["read_file", "write_file", "bash", "list_dir", "glob"],
    },
}

# Execution order within each round
PIPELINE: list[str] = [
    "researcher",
    "hypothesis",
    "coder",
    "debugger",
    "evaluator",
    "orchestrator",
    "reporter",
]

# Expected output paths (relative to round_dir)
OUTPUT_FILES: dict[str, str] = {
    "researcher":    "01_literature.md",
    "hypothesis":    "02_hypotheses.md",
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
You are the {label} in OctoSlave's autonomous multi-agent research system.

RESEARCH TOPIC : {topic}
ROUND          : {round_num} / {max_rounds}
ROUND DIR      : {round_dir}
RESEARCH DIR   : {research_dir}
WORKING DIR    : {working_dir}

ROUND BRIEF:
{brief}

---
"""

_ROLE_PROMPTS: dict[str, str] = {

"researcher": """\
YOUR MISSION
Search the web and existing files to produce a comprehensive literature survey
for this round's brief. Collect papers, datasets, code repos, and key facts.

STEPS
1. Search the web (web_search, web_fetch) for the most relevant recent work.
   Focus on arXiv, GitHub, official docs, and benchmarks.
2. If there is a 'literature/' folder in the working dir, read any PDFs there.
3. Identify publicly available datasets — include direct download URLs / DOIs,
   file formats, and licensing. Attempt to fetch each dataset's landing page to
   confirm it is accessible. Flag each as: VERIFIED ACCESSIBLE | REQUIRES SIGNUP
   | PAYWALLED | UNAVAILABLE (with reason).
4. Note existing implementations or baselines.

DATA SOURCING RULES
- Only list datasets you have confirmed exist (fetched their landing page / DOI).
- Do NOT recommend datasets you cannot verify — scientists will not waste time
  chasing phantom links.
- Prefer datasets with programmatic access (direct URL, Zenodo, HuggingFace,
  figshare, UCI ML repo, NCBI, etc.).

OUTPUT — write ONE file:
  {round_dir}/01_literature.md

Structure it as:
  ## Overview
  ## Key Papers        (title, authors, year, URL, 3-sentence summary each)
  ## Datasets          (name, size, URL, licence, ACCESS STATUS, relevance)
  ## Existing Tools / Code  (repo URL, stars if known, relevance)
  ## Identified Gaps   (what is missing / unexplored)

Be thorough. Aim for ≥ 5 substantive sources.
""",

"hypothesis": """\
YOUR MISSION
Read the literature survey and generate bold, testable hypotheses.

STEPS
1. Read {round_dir}/01_literature.md
2. If round > 1, also read {research_dir}/findings.md and previous synthesis.
3. Generate 3–5 specific, falsifiable hypotheses ranked by (feasibility × impact).

OUTPUT — write ONE file:
  {round_dir}/02_hypotheses.md

For each hypothesis include:
  ### Hypothesis N: <short name>
  **Statement**: one clear, falsifiable claim
  **Motivation**: why this matters / what gap it addresses
  **Predicted outcome**: what we expect to observe
  **Experiment design**: concrete steps to test it
  **Success criteria**: measurable thresholds (e.g., accuracy > X%)
  **Feasibility**: HIGH / MEDIUM / LOW + reason
  **Priority rank**: 1 = highest

End the file with:
  ## RECOMMENDED EXPERIMENT
  Which hypothesis to tackle this round and why.
""",

"coder": """\
YOUR MISSION
Implement the recommended experiment from the hypotheses file.
Write real, working, runnable code. Produce concrete results from real data.

STEPS
1. Read {round_dir}/02_hypotheses.md — focus on ## RECOMMENDED EXPERIMENT.
2. Read {round_dir}/01_literature.md — note which datasets are VERIFIED ACCESSIBLE.
3. Read {research_dir}/hw_profile.json if it exists — this contains the detected
   hardware profile for this machine. Use it to configure batch sizes, device
   placement, and parallelism in every script you write.
4. Read any existing code in {round_dir}/03_code/ if this is a continuation.
5. HARDWARE PROBE (MANDATORY — do this before writing any experiment code):
   Run the following one-liner and save the output to
   {round_dir}/03_code/hw_profile.json AND {research_dir}/hw_profile.json:

   python3 - <<'HWPROBE'
   import json, platform, os, sys
   info = {{"python": sys.version, "platform": platform.platform(),
            "cpu_count": os.cpu_count()}}
   try:
       import psutil
       mem = psutil.virtual_memory()
       info["ram_total_gb"] = round(mem.total / 1e9, 1)
       info["ram_available_gb"] = round(mem.available / 1e9, 1)
   except ImportError:
       pass
   try:
       import torch
       info["torch_version"] = torch.__version__
       info["cuda_available"] = torch.cuda.is_available()
       if torch.cuda.is_available():
           info["cuda_device_count"] = torch.cuda.device_count()
           info["cuda_devices"] = [
               {{"name": torch.cuda.get_device_name(i),
                 "vram_gb": round(torch.cuda.get_device_properties(i).total_memory / 1e9, 1)}}
               for i in range(torch.cuda.device_count())
           ]
           info["cuda_version"] = torch.version.cuda
   except ImportError:
       info["torch_available"] = False
   try:
       result = __import__("subprocess").run(
           ["nvidia-smi", "--query-gpu=name,memory.total,memory.free",
            "--format=csv,noheader,nounits"],
           capture_output=True, text=True, timeout=5
       )
       if result.returncode == 0:
           info["nvidia_smi"] = result.stdout.strip()
   except Exception:
       pass
   print(json.dumps(info, indent=2))
   HWPROBE

   If psutil is not installed, install it first: pip install psutil -q

6. Plan the implementation using the hardware profile, then execute:
   a. Create {round_dir}/03_code/ directory.
   b. Attempt to download or access the verified dataset(s) from the literature.
   c. Write modular, well-commented Python (or other language if appropriate).
   d. Install required packages with pip/conda.
   e. Run the code. Fix any runtime errors.
   f. Save ALL output (logs, metrics, plots) to {round_dir}/03_code/results/.
7. Write {round_dir}/03_code/IMPLEMENTATION.md covering:
   - Detected hardware and how it was used
   - Approach taken and data sources used
   - Any steps that were skipped and why (see FAILURE PROTOCOL)
   - Key design decisions
   - How to run
   - Summary of results achieved

GPU / ACCELERATOR RULES (CRITICAL)
- After probing, if CUDA is available you MUST use it. There are no exceptions.
- Always use torch.device("cuda" if torch.cuda.is_available() else "cpu") and
  move models AND tensors to that device explicitly (.to(device) or .cuda()).
- For PyTorch training loops:
    * Use torch.amp.autocast("cuda") + GradScaler for mixed-precision training.
    * Set num_workers ≥ 2 in DataLoader (pin_memory=True when on CUDA).
    * Choose batch_size to fill ~70–80% of available VRAM (read from hw_profile).
- For scikit-learn / XGBoost: pass device="cuda" or tree_method="gpu_hist"
  where the library supports it.
- For HuggingFace Transformers: pass device_map="auto" or .to(device).
- For JAX / TensorFlow: confirm GPU backend and log it explicitly.
- Always log which device is actually being used at runtime:
    print(f"Using device: {{device}}")  # this must appear in the output
- Save GPU utilisation stats (peak VRAM used) to results/ using:
    torch.cuda.max_memory_allocated() / 1e9 → log as "peak_vram_gb"
- If CUDA is available but a library does not support it, document why in
  IMPLEMENTATION.md and ensure at minimum the data pipeline is vectorised.

VISUALISATION (REQUIRED)
- Generate plots for ALL key results using matplotlib or seaborn.
- Save every figure to {round_dir}/03_code/results/ as PNG at 150 dpi minimum.
- Each filename must be descriptive: e.g. results_accuracy_vs_epochs.png
- Every plot must have: title, axis labels with units, legend where applicable.
- Minimum required (adapt to the experiment):
    * Data overview / distribution plot
    * Main results plot (metric vs parameter, learning curve, scatter, etc.)
    * Model vs data comparison plot if fitting was performed
    * Baseline comparison plot if baselines are available
- Use tight_layout() and savefig() — do not rely on plt.show().
- Also save a {round_dir}/03_code/results/summary_figure.png that is a
  multi-panel overview (2–4 subplots) of the most important results.

PYTHON PACKAGE MANAGEMENT — USE UV
- Always use `uv` as the package manager unless the user specifies otherwise.
  uv is faster, reproducible, and isolates dependencies correctly.
  Commands:
    uv pip install <pkg>          # install into current env
    uv pip install -r requirements.txt
    uv run python script.py       # run with uv-managed env
    uv init <project>             # new project with pyproject.toml
    uv add <pkg>                  # add dep to pyproject.toml
    uv sync                       # install all deps from lockfile
- If uv is not installed: `pip install uv -q` first, then use uv.
- Fallback to pip ONLY if uv fails and document the reason in IMPLEMENTATION.md.

ABSOLUTE RULES — READ CAREFULLY
- NEVER generate synthetic or dummy data as a substitute for real data.
  Synthetic stand-ins are scientifically invalid and mislead future agents.
- NEVER fabricate results or outputs. Every number in results/ must come from
  real computation on real data.
- If a data source is unavailable (network error, API down, auth required):
    1. Log the failure clearly in IMPLEMENTATION.md under ## Skipped Steps.
    2. Do NOT proceed with that experiment using fake data.
    3. Instead: search for an alternative real dataset that tests the same
       hypothesis (web_search). Try at least 2–3 alternatives.
    4. If NO real data can be obtained for a given hypothesis, mark that
       experiment as BLOCKED in IMPLEMENTATION.md and pivot to a different
       hypothesis from {round_dir}/02_hypotheses.md that CAN use available data.
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
Independently verify that the code works correctly and that results are valid.
Your job is to be skeptical — find flaws before the evaluator does.

STEPS
1. List and read ALL files under {round_dir}/03_code/.
2. For each script: read it, then run it, inspect output.
3. Check for:
   - SYNTHETIC / DUMMY DATA — any use of generated, fabricated, or placeholder
     data instead of real sources is an AUTOMATIC CRITICAL BUG. Flag it
     immediately and mark it as UNFIXABLE unless real data is substituted.
   - GPU UNDERUTILISATION — read {research_dir}/hw_profile.json. If CUDA is
     available and the code does NOT move models/tensors to the GPU, this is a
     CRITICAL BUG. Check that:
       * "Using device: cuda" appears in the run output (not "cpu")
       * peak_vram_gb is logged and > 0 in results/
       * batch_size is appropriately sized for available VRAM
     Fix any CPU-only code by adding .to(device) and rerunning.
   - Runtime errors or silent failures
   - Off-by-one errors, data leakage, incorrect metrics
   - Results that seem too good / too bad to be true (may indicate fake data)
   - Hard-coded paths or missing dependencies
   - Skipped steps — verify each skip in IMPLEMENTATION.md is justified and
     that alternatives were genuinely attempted
4. Fix every bug you find (edit_file / bash).
5. Re-run after fixes to confirm they pass.
6. Write a structured report:

OUTPUT — write ONE file:
  {round_dir}/04_debug_report.md

Structure:
  ## Bugs Found and Fixed   (list each bug, fix applied, verification)
  ## Tests Run              (commands and outcomes)
  ## Verified Results       (copy key metrics here for the record)
  ## Outstanding Issues     (anything you could not fix — be honest)
  ## Confidence Score       (0–10: how trustworthy are the results?)
""",

"evaluator": """\
YOUR MISSION
Provide an INDEPENDENT, critical assessment of this round's work.
You have not been involved in producing the work — evaluate it with fresh eyes.

STEPS
1. Read in order:
   {round_dir}/01_literature.md
   {round_dir}/02_hypotheses.md
   All files under {round_dir}/03_code/
   {round_dir}/04_debug_report.md
2. Optionally run the code yourself to verify claims.
3. Cross-check results against literature benchmarks (web_search if needed).

OUTPUT — write ONE file:
  {round_dir}/05_evaluation.md

Structure:
  ## Literature Quality      (score 0–10 + commentary)
  ## Hypothesis Quality      (score 0–10 + commentary)
  ## Implementation Quality  (score 0–10 + commentary)
  ## Results Validity        (score 0–10 + commentary)
  ## Overall Score           (0–10 weighted average)
  ## Strengths               (what was done well)
  ## Critical Weaknesses     (what MUST be improved)
  ## Recommended Next Steps  (specific, actionable, prioritised)
  ## SOTA Comparison         (how does this compare to known state-of-the-art?)

VISUALISATION (REQUIRED)
- Write and run a short Python script that generates a bar chart of all your
  scores (0–10 per dimension) and saves it to {round_dir}/05_scores_chart.png.
- Use matplotlib with a clean style. Label every bar with its score.
- Colour bars: green (≥7), amber (4–6), red (≤3).

SCORING RULES
- If any results were produced from synthetic / generated data rather than a
  real source: Results Validity score is capped at 1/10. State this explicitly.
- A skipped step with a clear justification and documented alternatives is
  acceptable. A skipped step replaced by fake data is a critical failure.
- Be harsh. Mediocre work rated generously helps no one.
""",

"orchestrator": """\
YOUR MISSION
Synthesise this round's work, update the master findings log, and write the
precise brief that will drive the next round's specialist agents.

STEPS
1. Read all round outputs:
   {round_dir}/01_literature.md
   {round_dir}/02_hypotheses.md
   {round_dir}/03_code/IMPLEMENTATION.md  (if exists)
   {round_dir}/04_debug_report.md
   {round_dir}/05_evaluation.md
2. Read {research_dir}/findings.md (if it exists) for cumulative context.
3. Synthesise: what was learned, what worked, what failed, what to do next.
4. Write ONLY {round_dir}/06_synthesis.md with this exact structure.
   findings.md is updated automatically by the pipeline — do NOT touch it.

   ## Round Summary
   ## Key Findings
   ## What Worked
   ## What Failed / Gaps
   ## Updated Research Direction

   Then ONE of:

   {next_brief_marker}
   [A specific, detailed brief for the next round — concrete tasks,
    specific models/datasets to use, exact improvements to make.
    Build on what failed. Escalate ambition if things worked.]

   OR (only if the research has fully converged or max rounds reached):

   {complete_marker}
   [Final conclusion statement]

DECISION CRITERIA for COMPLETE:
- Hypotheses have been tested and results are solid (evaluator score ≥ 8/10)
- Findings are novel relative to the literature
- Code is reproducible and well-documented
- OR we have exhausted productive directions
""",

"reporter": """\
YOUR MISSION
Produce a polished, self-contained HTML progress report for this round.
This is the primary artifact scientists will open to quickly judge what was
done, what was found, and where the research is headed.

STEPS
1. Inventory all round outputs:
   - Read {round_dir}/01_literature.md
   - Read {round_dir}/02_hypotheses.md
   - Read {round_dir}/03_code/IMPLEMENTATION.md  (if exists)
   - Read {round_dir}/04_debug_report.md          (if exists)
   - Read {round_dir}/05_evaluation.md
   - Read {round_dir}/06_synthesis.md
   - List all *.png and *.svg files under {round_dir}/ and {round_dir}/03_code/results/
2. Write a Python script to {round_dir}/build_report.py that generates the
   HTML. Run it with bash. Verify {round_dir}/07_report.html is non-empty.

REPORT STRUCTURE (HTML sections in order)
  1. Sticky nav bar  — section anchors for quick jumping
  2. Header          — round N / max_rounds, topic, date, overall score badge
  3. Executive Summary — 4–6 bullet points drawn from the synthesis
  4. Literature Highlights — top 3 papers/datasets as cards with clickable links
  5. Hypotheses      — each hypothesis as a card (name, statement, feasibility
                       badge); recommended experiment card highlighted
  6. Implementation  — data sources used, approach, any skipped steps
  7. Results & Plots — ALL PNG/SVG files embedded inline (base64), laid out in
                       a 2-column responsive grid, each with a 1-sentence
                       caption derived from the filename / IMPLEMENTATION.md
  8. Evaluation      — embed 05_scores_chart.png; colour-coded score table
                       (green ≥7, amber 4–6, red ≤3)
  9. Debug Summary   — bugs found/fixed, confidence score badge
  10. Next Direction — NEXT_ROUND_BRIEF from synthesis, formatted as a callout

DESIGN REQUIREMENTS
- Fully self-contained: base64-encode every image; no external CSS/image URLs.
  External Google Fonts CDN link is OK.
- Academic style: dark (#1a1a2e) header/nav, white content cards with subtle
  box-shadow, readable 16px body font (Inter or system-ui), monospace for code.
- Responsive: max-width 1100px centred, 2-column plot grid that collapses to
  1 column on narrow viewports (use CSS flex/grid).
- Plots: full-width within their grid cell — never thumbnail-sized.
- Score badges: pill-shaped, colour-coded.
- Include a footer with: round number, topic, generation timestamp.

PYTHON SCRIPT REQUIREMENTS
- Use only stdlib + matplotlib (pip install if needed). No Jinja2 required —
  build the HTML as an f-string or concatenated string.
- Read markdown files with open(), base64-encode PNGs with base64.b64encode().
- Write the final HTML with open(output_path, 'w').
- Print "Report written to <path>" on success so bash output confirms it.
""",
}


def _build_system_prompt(
    role: str,
    topic: str,
    round_num: int,
    max_rounds: int,
    round_dir: str,
    research_dir: str,
    working_dir: str,
    brief: str,
) -> str:
    role_cfg = ROLES[role]
    header = _SHARED_HEADER.format(
        label=role_cfg["label"],
        topic=topic,
        round_num=round_num,
        max_rounds=max_rounds,
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

    for iteration in range(1, max_iter + 1):
        try:
            response = _stream_completion_with_tools(client, model, messages, tools)
        except BadRequestError as e:
            err = str(e)
            if "ContextWindow" in err or "context" in err.lower():
                display.print_error(
                    f"[{cfg['label']}] Context window exceeded — "
                    "trimming oldest tool results and retrying."
                )
                messages = _trim_messages(messages)
                continue
            display.print_error(f"[{cfg['label']}] API error: {e}")
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
    hypotheses  = _read(OUTPUT_FILES["hypothesis"])

    # Extract overall score from evaluation
    score_match = re.search(r"##\s*Overall Score[^\n]*\n+([^\n]+)", evaluation)
    score_str   = score_match.group(1).strip() if score_match else "N/A"

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

    # Extract recommended experiment from hypotheses
    rec_match = re.search(
        r"##\s*RECOMMENDED EXPERIMENT\s*\n(.*?)(?:\n##|\Z)", hypotheses, re.DOTALL
    )
    recommended = rec_match.group(1).strip() if rec_match else ""

    from datetime import datetime
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

def _trim_messages(messages: list[dict]) -> list[dict]:
    """
    Remove the oldest tool-result messages (pairs) to free context space.
    Always preserve system + first user message.
    """
    system = messages[:2]
    rest = messages[2:]

    # Drop oldest tool result
    for i, m in enumerate(rest):
        if m.get("role") == "tool":
            rest = rest[:max(0, i - 1)] + rest[i + 1:]
            break

    return system + rest


# ---------------------------------------------------------------------------
# Master HTML report (runs once after all rounds complete)
# ---------------------------------------------------------------------------

_MASTER_REPORTER_PROMPT = """\
You are the Master Reporter for an autonomous multi-agent research run.

RESEARCH TOPIC  : {topic}
ROUNDS COMPLETED: {rounds_done}
RESEARCH DIR    : {research_dir}

YOUR MISSION
Produce a single comprehensive, self-contained HTML report covering the entire
multi-round research run. This is the definitive deliverable — the document
a scientist will open to understand everything that was done.

STEPS
1. List all round directories under {research_dir}/.
2. For each round, read:
   - round_NNN/01_literature.md
   - round_NNN/02_hypotheses.md
   - round_NNN/03_code/IMPLEMENTATION.md  (if exists)
   - round_NNN/05_evaluation.md
   - round_NNN/06_synthesis.md
3. Read {research_dir}/findings.md.
4. Collect ALL PNG/SVG files from every round's 03_code/results/ directory
   and any *.png at the round level (score charts etc.).
5. Write a Python script to {research_dir}/build_master_report.py and run it.
   The script must produce {research_dir}/final_report.html.

MASTER REPORT STRUCTURE
  1. Sticky nav bar — jump links to each major section
  2. Title block    — topic, date, rounds completed, overall quality badge
  3. Abstract       — 1 paragraph summary of the entire research arc
  4. Research Timeline — visual round-by-round progress table showing:
       Round | Key Hypothesis Tested | Overall Score | Status
  5. Cumulative Findings — content from findings.md, formatted as cards
  6. Round-by-Round Deep Dives (one collapsible <details> block per round):
       - Literature highlights
       - Hypothesis tested
       - Implementation summary & data sources
       - ALL result plots from that round (2-column grid, base64 inline)
       - Evaluation scores chart + colour-coded score table
       - What worked / what failed
  7. Cross-Round Score Progression — a matplotlib line/bar chart showing
       overall evaluation score per round; generate this chart in the Python
       script and embed it inline.
  8. Key Visualisations Gallery — a curated gallery of the most informative
       plots across ALL rounds (the summary_figure.png from each round, if
       present), displayed prominently full-width.
  9. Methodology & Reproducibility — how to re-run each round's code
  10. Conclusions & Next Steps — drawn from the final synthesis

DESIGN REQUIREMENTS
- Fully self-contained (base64 all images, Google Fonts CDN OK).
- Dark header (#0d1117), white cards with subtle shadows, Inter font.
- Responsive max-width 1200px, 2-column plot grid.
- Collapsible round sections (HTML <details>/<summary>) so the document is
  scannable at the top level but full detail is one click away.
- Score progression chart: clean lines, round numbers on x-axis, score on y.
- Footer: topic, generation timestamp, "Generated by OctoSlave".
- Print the output path on success.
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
    cfg = ROLES["reporter"]
    tools = _tools_for_role("reporter")

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
    for iteration in range(1, cfg["max_iter"] + 1):
        try:
            response = _stream_completion_with_tools(client, model, messages, tools)
        except BadRequestError as e:
            err = str(e)
            if "ContextWindow" in err or "context" in err.lower():
                messages = _trim_messages(messages)
                continue
            display.print_error(f"[Master Reporter] API error: {e}")
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
        "import json, platform, os, sys\n"
        "info = {'python': sys.version.split()[0], 'platform': platform.platform(), "
        "'cpu_count': os.cpu_count()}\n"
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
        "    r = __import__('subprocess').run(['nvidia-smi','--query-gpu=name,memory.total,memory.free',"
        "'--format=csv,noheader,nounits'], capture_output=True, text=True, timeout=5)\n"
        "    if r.returncode==0: info['nvidia_smi'] = r.stdout.strip()\n"
        "except Exception: pass\n"
        "print(json.dumps(info))\n"
    )

    profile: dict = {}
    try:
        result = _sp.run(
            ["python3", "-c", script],
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

    if cuda and devices:
        gpu_str = ", ".join(f"{d['name']} ({d['vram_gb']} GB)" for d in devices)
        display.print_info(f"  Hardware: {cpus} CPU cores, {ram} GB RAM, "
                           f"[bold bright_green]CUDA ✓[/bold bright_green] {gpu_str}")
    else:
        display.print_info(f"  Hardware: {cpus} CPU cores, {ram} GB RAM, "
                           f"[dim]no CUDA GPU detected[/dim]")

    return profile


def run_long_research(
    topic: str,
    working_dir: str,
    client: OpenAI,
    max_rounds: int = 5,
    model_overrides: dict[str, str] | None = None,
    resume: bool = False,
) -> None:
    """
    Run the full autonomous multi-agent research pipeline.

    Args:
        topic:          The research topic / goal.
        working_dir:    The project working directory.
        client:         Authenticated OpenAI client.
        max_rounds:     Maximum number of research rounds.
        model_overrides: Per-role model overrides, e.g. {"coder": "qwen3-coder-30b"}.
        resume:         If True, skip rounds whose output files already exist.
    """
    overrides = model_overrides or {}
    research_dir = Path(working_dir) / "research"
    research_dir.mkdir(parents=True, exist_ok=True)

    display.print_research_start(topic, max_rounds, ROLES, overrides)

    # Probe hardware once; result is written to research_dir/hw_profile.json
    # and read by the coder/debugger agents in every subsequent round.
    _probe_hardware(str(research_dir))

    # Initial brief
    brief = (
        f"Initial research round. Conduct a broad literature survey on: {topic}\n"
        "Identify key papers, available datasets, existing methods, and open problems.\n"
        "Generate first hypotheses and implement the most promising experiment."
    )

    completed_early = False

    for round_num in range(1, max_rounds + 1):
        round_dir = research_dir / f"round_{round_num:03d}"
        round_dir.mkdir(parents=True, exist_ok=True)

        display.print_round_header(round_num, max_rounds, str(round_dir))

        for role in PIPELINE:
            model = overrides.get(role) or ROLES[role]["default_model"]

            # Resumability: skip if output already exists
            expected = OUTPUT_FILES[role]
            expected_path = round_dir / expected
            if resume and (expected_path.exists() or (expected_path.is_dir())):
                display.print_info(
                    f"  ↩  {ROLES[role]['label']} output found — skipping."
                )
                continue

            try:
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
