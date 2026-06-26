"""
Run N independent agents on the same task, then pick or merge the result.

Public surface:
    run_parallel_agents(task, model, working_dir, client, n=3, strategy="best", ...)

Three strategies:
    best   — Judge agent compares all N solutions and picks one winner
    vote   — Each candidate grades the others; majority winner is chosen
    merge  — Merger agent combines all N solutions into a synthesis written
             back into working_dir

The N runs each operate on an *isolated copy* of working_dir. Files are
materialised into ``working_dir/.parallel/run_{i}/`` and the chosen winner
(or merge result) is mirrored back into ``working_dir`` at the end so the
user sees a single, coherent result.

Diversity comes from per-run prompt-profile and (optionally) per-run model
overrides supplied by the caller.
"""

from __future__ import annotations

import shutil
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from openai import OpenAI

from . import display
from .agent import _simple_completion, run_agent

PARALLEL_SUBDIR = ".parallel"

# Per-candidate accent colours used to label CLI output and the web-UI cards.
_CAND_COLORS = [
    "#fab283",  # warm orange  (primary)
    "#5c9cf5",  # blue
    "#9d7cd8",  # purple
    "#7fd88f",  # green
    "#f5a742",  # amber
    "#e06c75",  # red
    "#5cd5c2",  # teal
    "#d8ad7c",  # tan
]


def _candidate_color(idx: int) -> str:
    return _CAND_COLORS[idx % len(_CAND_COLORS)]


@dataclass
class CandidateResult:
    index: int
    profile: str
    model: str
    workdir: Path
    summary: str = ""
    succeeded: bool = False
    error: Optional[str] = None
    messages: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------

def _prepare_workdir(parent: Path, index: int) -> Path:
    """Make ``parent/.parallel/run_{i}/`` and copy the project files into it.

    The copy is shallow on directory-tree level but recursive on file content.
    Hidden bookkeeping dirs (``.git``, ``.venv``, ``__pycache__``, ``.parallel``)
    are skipped so each run starts from a clean snapshot of the user's project.
    """
    runs_root = parent / PARALLEL_SUBDIR
    runs_root.mkdir(parents=True, exist_ok=True)
    target = runs_root / f"run_{index}"
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)

    SKIP = {".git", ".venv", "venv", "__pycache__", PARALLEL_SUBDIR, "node_modules", ".pytest_cache"}
    for child in parent.iterdir():
        if child.name in SKIP:
            continue
        try:
            if child.is_dir():
                shutil.copytree(child, target / child.name, symlinks=True,
                                ignore=shutil.ignore_patterns(*SKIP))
            else:
                shutil.copy2(child, target / child.name)
        except Exception as exc:
            display.print_info(f"[parallel] skipped {child.name}: {exc}")
    return target


def _last_assistant_text(messages: list[dict]) -> str:
    """Return the final assistant text content, or '' if none."""
    for m in reversed(messages):
        if m.get("role") == "assistant" and m.get("content"):
            return m["content"]
    return ""


def _judge_pick(
    task: str,
    candidates: list[CandidateResult],
    client: OpenAI,
    judge_model: str,
) -> tuple[int, str]:
    """Ask a judge model to pick the best candidate. Returns (winner_index, reasoning)."""
    if not candidates:
        return -1, "no candidates"

    sections = []
    for c in candidates:
        head = f"## Candidate {c.index} — profile={c.profile} model={c.model}"
        body = c.summary if c.succeeded else f"FAILED: {c.error or 'unknown error'}"
        files_listed = []
        try:
            for p in sorted(c.workdir.rglob("*"))[:40]:
                if p.is_file():
                    files_listed.append(str(p.relative_to(c.workdir)))
        except Exception:
            pass
        files_block = "\n".join(f"  - {f}" for f in files_listed) or "  (no files produced)"
        sections.append(f"{head}\n\nFiles produced:\n{files_block}\n\nFinal summary:\n{body}\n")

    prompt = (
        f"You are judging {len(candidates)} candidate solutions to the same task.\n\n"
        f"Original task:\n{task}\n\n"
        + "\n---\n".join(sections)
        + "\n\nRespond with EXACTLY two lines:\n"
        "  WINNER: <index>\n"
        "  REASON: <one sentence>\n"
        "Pick on technical correctness, completeness, and parsimony. "
        "Prefer candidates that actually produced working artefacts."
    )
    raw = _simple_completion(client, judge_model, [
        {"role": "system", "content": "You are a strict, impartial code reviewer."},
        {"role": "user", "content": prompt},
    ], max_tokens=200)

    winner = -1
    reason = raw.strip()
    for line in raw.splitlines():
        s = line.strip().upper()
        if s.startswith("WINNER:"):
            tail = line.split(":", 1)[1].strip()
            for tok in tail.split():
                tok = tok.strip(",.;[]()")
                if tok.isdigit():
                    winner = int(tok)
                    break
        elif s.startswith("REASON:"):
            reason = line.split(":", 1)[1].strip()

    if winner < 0 or winner >= len(candidates):
        # Fall back to the first successful candidate
        for c in candidates:
            if c.succeeded:
                return c.index, "judge response unparseable; falling back to first successful run"
        return 0, "judge response unparseable; falling back to first run"
    return winner, reason


def _vote_pick(
    task: str,
    candidates: list[CandidateResult],
    client: OpenAI,
    judge_model: str,
) -> tuple[int, str]:
    """Each candidate grades the others; majority winner wins."""
    if not candidates:
        return -1, "no candidates"
    if len(candidates) == 1:
        return candidates[0].index, "single candidate"

    votes: dict[int, int] = {c.index: 0 for c in candidates}
    rationales: list[str] = []
    for grader in candidates:
        peers = [c for c in candidates if c.index != grader.index]
        peer_block = "\n---\n".join(
            f"## Candidate {c.index}\n{c.summary or '(failed)'}" for c in peers
        )
        prompt = (
            f"Original task:\n{task}\n\n"
            f"You are Candidate {grader.index}. Pick the BEST peer below.\n\n"
            f"{peer_block}\n\n"
            "Respond with EXACTLY one line: VOTE: <index>"
        )
        raw = _simple_completion(client, judge_model, [
            {"role": "system", "content": "You are voting impartially."},
            {"role": "user", "content": prompt},
        ], max_tokens=50)
        rationales.append(f"  C{grader.index} → {raw.strip()[:80]}")
        for tok in raw.split():
            tok = tok.strip(",.;[]():")
            if tok.isdigit() and int(tok) in votes:
                votes[int(tok)] += 1
                break

    winner = max(votes, key=lambda k: votes[k])
    reason = f"votes={votes}; " + " · ".join(rationales)
    return winner, reason


def _merger_synthesize(
    task: str,
    candidates: list[CandidateResult],
    client: OpenAI,
    merger_model: str,
) -> str:
    """Synthesise an integrated answer from all candidates. Returns markdown text."""
    sections = []
    for c in candidates:
        body = c.summary if c.succeeded else f"FAILED: {c.error or 'unknown'}"
        sections.append(f"## Candidate {c.index} (profile={c.profile})\n\n{body}")
    prompt = (
        f"Original task:\n{task}\n\n"
        f"You are the merger. Below are {len(candidates)} candidate solutions.\n"
        "Produce a single integrated answer that takes the best of each — keep what is "
        "correct, merge complementary insights, drop contradictions in favour of the "
        "most defensible option, and explicitly note any disagreement that matters.\n\n"
        + "\n\n---\n\n".join(sections)
    )
    return _simple_completion(client, merger_model, [
        {"role": "system", "content": "You synthesise multi-agent outputs into one coherent answer."},
        {"role": "user", "content": prompt},
    ], max_tokens=1500)


def _promote_winner(winner: CandidateResult, target: Path) -> int:
    """Copy files from the winning run back into the user's working dir.

    Skips files identical to the source (no spam). Returns the count copied.
    The .parallel/ subdir itself is never copied back.
    """
    copied = 0
    for src in winner.workdir.rglob("*"):
        if not src.is_file():
            continue
        rel = src.relative_to(winner.workdir)
        if rel.parts and rel.parts[0] == PARALLEL_SUBDIR:
            continue
        dest = target / rel
        try:
            if dest.exists() and dest.read_bytes() == src.read_bytes():
                continue
        except Exception:
            pass
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(src, dest)
            copied += 1
        except Exception as exc:
            display.print_info(f"[parallel] could not promote {rel}: {exc}")
    return copied


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

# Default profile rotation gives lightweight diversity even when the caller
# supplies only one model. Order matters — first N are used.
_PROFILE_ROTATION = ["base", "coder", "analyst"]


def run_parallel_agents(
    task: str,
    model: str,
    working_dir: str,
    client: OpenAI,
    *,
    n: int = 3,
    strategy: str = "best",
    permission_mode: str = "autonomous",
    profiles: list[str] | None = None,
    models: list[str] | None = None,
    judge_model: str | None = None,
    enable_plan: bool = True,
    enable_memory: bool = False,
) -> dict:
    """Run ``n`` agents on ``task`` and pick or merge a result.

    Returns a dict with keys: winner, reason, candidates, strategy, merged_text.
    """
    if n < 1:
        raise ValueError("n must be >= 1")
    if strategy not in ("best", "vote", "merge"):
        raise ValueError(f"strategy must be best|vote|merge, got {strategy!r}")

    parent = Path(working_dir).resolve()
    parent.mkdir(parents=True, exist_ok=True)

    profile_pool = profiles or _PROFILE_ROTATION
    chosen_profiles = [(profile_pool * (n // len(profile_pool) + 1))[i] for i in range(n)]
    chosen_models = (models or [model] * n)
    if len(chosen_models) < n:
        chosen_models = (chosen_models * (n // len(chosen_models) + 1))[:n]

    candidates: list[CandidateResult] = [
        CandidateResult(
            index=i,
            profile=chosen_profiles[i],
            model=chosen_models[i],
            workdir=Path(),
        )
        for i in range(n)
    ]

    # Per-thread workdir prep happens up-front so failures here are visible.
    for c in candidates:
        c.workdir = _prepare_workdir(parent, c.index)

    # Capture the parent thread's emit callback (web-UI bridge) BEFORE we spawn
    # workers — workers run in their own threads where threading.local() is
    # blank, so we have to forward events explicitly.
    parent_emit = display.get_event_callback()

    # Tell the parent (web UI / future surfaces) the parallel run is starting.
    if parent_emit is not None:
        parent_emit({
            "type": "parallel_start",
            "n": n,
            "strategy": strategy,
            "candidates": [
                {
                    "index": c.index,
                    "profile": c.profile,
                    "model": c.model,
                    "color": _candidate_color(c.index),
                }
                for c in candidates
            ],
        })

    # Header for the CLI — shows the user N candidates and strategy at a glance.
    display.console.print()
    display.console.print(
        f"  [bold #fab283]🐙×{n}[/bold #fab283]  "
        f"[dim]parallel · strategy=[bold]{strategy}[/bold] · "
        f"workdir={parent.name}/.parallel/[/dim]"
    )
    for c in candidates:
        col = _candidate_color(c.index)
        display.console.print(
            f"  [bold {col}]#{c.index}[/bold {col}]  "
            f"[dim]profile=[/dim]{c.profile}  "
            f"[dim]model=[/dim][bold]{c.model}[/bold]  "
            f"[dim]→ run_{c.index}/[/dim]"
        )
    display.console.print()

    # Console output from worker threads must be serialised; otherwise lines
    # from different candidates collide mid-print.
    print_lock = threading.Lock()

    def _make_worker_emit(c: CandidateResult):
        """Build an emit callback installed in each worker thread.

        It does three things:
          1. forwards every event to the parent (web UI) with candidate metadata
          2. emits compact ``parallel_progress`` digest events for the panel
          3. prints colour-prefixed milestone lines to the CLI console
        """
        col = _candidate_color(c.index)
        label = f"[bold {col}]#{c.index}[/bold {col}]"
        state = {"iters": 0}

        def worker_emit(event: dict) -> None:
            t = event.get("type", "")

            # 1 + 2 — forward to parent for the web bridge.
            if parent_emit is not None:
                annotated = {
                    **event,
                    "candidate_index": c.index,
                    "candidate_profile": c.profile,
                    "candidate_model": c.model,
                    "candidate_color": col,
                }
                try:
                    parent_emit(annotated)
                except Exception:
                    pass

                # Per-candidate progress digest — easier for the UI to consume
                # than every raw token/tool_call event.
                progress = None
                if t == "tool_call":
                    state["iters"] += 1
                    progress = {
                        "type": "parallel_progress",
                        "index": c.index,
                        "status": "running",
                        "iter": state["iters"],
                        "action": (
                            (event.get("name") or "") + " " +
                            (event.get("summary") or "")
                        ).strip()[:120],
                    }
                elif t == "done":
                    progress = {
                        "type": "parallel_progress",
                        "index": c.index,
                        "status": "done",
                        "iter": event.get("iterations", state["iters"]),
                        "action": "completed",
                    }
                elif t == "error":
                    progress = {
                        "type": "parallel_progress",
                        "index": c.index,
                        "status": "error",
                        "iter": state["iters"],
                        "action": (event.get("text") or "")[:120],
                    }
                if progress is not None:
                    try:
                        parent_emit(progress)
                    except Exception:
                        pass

            # 3 — compact CLI output. Skip noisy events (token streaming, etc.)
            with print_lock:
                if t == "tool_call":
                    name = event.get("name", "")
                    summary = (event.get("summary") or "").strip()
                    display.console.print(
                        f"  {label} [dim]→[/dim] "
                        f"[bold #fab283]{name}[/bold #fab283] "
                        f"[dim]{summary[:80]}[/dim]"
                    )
                elif t == "tool_result":
                    ok = event.get("ok", True)
                    mark = "[bold #7fd88f]✓[/bold #7fd88f]" if ok \
                        else "[bold #e06c75]✗[/bold #e06c75]"
                    preview = (event.get("preview") or "").replace("\n", " ").strip()[:80]
                    if preview:
                        display.console.print(f"  {label}   {mark} [dim]{preview}[/dim]")
                elif t == "info":
                    text = (event.get("text") or "").strip()
                    if text:
                        display.console.print(f"  {label} [dim]ℹ {text[:120]}[/dim]")
                elif t == "error":
                    display.console.print(
                        f"  {label} [bold #e06c75]✗ error:[/bold #e06c75] "
                        f"[dim]{(event.get('text') or '')[:120]}[/dim]"
                    )
                elif t == "done":
                    display.console.print(
                        f"  {label} [bold #7fd88f]✓ done[/bold #7fd88f] "
                        f"[dim]({event.get('iterations', 0)} iter)[/dim]"
                    )
                elif t == "plan":
                    display.console.print(f"  {label} [dim]plan generated[/dim]")
                # token / stream_start / stream_end / tool_result with no
                # preview / separator are all suppressed.

        return worker_emit

    def _run_one(c: CandidateResult) -> CandidateResult:
        with print_lock:
            col = _candidate_color(c.index)
            display.console.print(
                f"  [bold {col}]#{c.index}[/bold {col}] "
                f"[bold #d0d1d6]▶ starting[/bold #d0d1d6]"
            )
        # Worker thread silences direct CLI output and installs the wrapping
        # emit; the wrapper prints the compact prefixed lines instead.
        display.set_silent_cli(True)
        display.set_event_callback(_make_worker_emit(c))
        try:
            msgs = run_agent(
                task=task,
                model=c.model,
                working_dir=str(c.workdir),
                client=client,
                prompt_profile=c.profile,
                permission_mode=permission_mode,
                enable_plan=enable_plan,
                enable_verify=False,
                enable_memory=enable_memory,
            )
            c.messages = msgs
            c.summary = _last_assistant_text(msgs) or "(no final summary)"
            c.succeeded = True
        except Exception as exc:
            c.error = str(exc)
            c.succeeded = False
            with print_lock:
                col = _candidate_color(c.index)
                display.console.print(
                    f"  [bold {col}]#{c.index}[/bold {col}] "
                    f"[bold #e06c75]✗ failed:[/bold #e06c75] {exc}"
                )
            if parent_emit is not None:
                try:
                    parent_emit({
                        "type": "parallel_progress",
                        "index": c.index,
                        "status": "error",
                        "action": str(exc)[:120],
                    })
                except Exception:
                    pass
        finally:
            display.clear_event_callback()
            display.set_silent_cli(False)
        return c

    # ThreadPoolExecutor — we cap workers at n; each agent loop is I/O-bound
    # against the LLM API so threading is the right primitive.
    with ThreadPoolExecutor(max_workers=n, thread_name_prefix="ots-par") as pool:
        futures = [pool.submit(_run_one, c) for c in candidates]
        for fut in as_completed(futures):
            try:
                fut.result()
            except Exception as exc:
                display.print_error(f"[parallel] candidate failed: {exc}")

    successes = [c for c in candidates if c.succeeded]
    if not successes:
        display.print_error("All parallel candidates failed.")
        return {
            "winner": None,
            "reason": "all candidates failed",
            "candidates": candidates,
            "strategy": strategy,
            "merged_text": "",
        }

    # Decide a winner / merge.
    judge = judge_model or model
    merged_text = ""
    if strategy == "merge":
        merged_text = _merger_synthesize(task, successes, client, judge)
        winner_idx = -1
        reason = "merged synthesis (no single winner)"
        # Write the merge to the parent working dir as PARALLEL_MERGE.md
        try:
            (parent / "PARALLEL_MERGE.md").write_text(merged_text, encoding="utf-8")
            display.print_info(f"📝 merge written to {parent / 'PARALLEL_MERGE.md'}")
        except Exception as exc:
            display.print_error(f"[parallel] could not write merge: {exc}")
    elif strategy == "vote":
        winner_idx, reason = _vote_pick(task, successes, client, judge)
    else:  # best
        winner_idx, reason = _judge_pick(task, successes, client, judge)

    winner = next((c for c in candidates if c.index == winner_idx), None)
    if winner and winner.succeeded:
        copied = _promote_winner(winner, parent)
        display.print_info(
            f"🏆 candidate {winner.index} wins — promoted {copied} file(s) "
            f"to {parent}.\n   reason: {reason}"
        )
    elif strategy != "merge":
        display.print_info("No winner promoted — see .parallel/run_*/ for raw outputs.")

    return {
        "winner": winner.index if winner else None,
        "reason": reason,
        "candidates": candidates,
        "strategy": strategy,
        "merged_text": merged_text,
    }
