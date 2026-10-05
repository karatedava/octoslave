"""Workspace inspection: is the deliverable current, and is the tree tidy?

Two things went wrong repeatedly in real sessions, and neither is something an
agent notices on its own:

* **The deliverable drifts.** The researcher steers the work several times, each
  round produces new results, and the report that is supposed to show them keeps
  the state it had three rounds ago — until the researcher asks whether it is
  still up to date. The agent is not being careless; nothing ever tells it that
  the file it presented is now older than the work it describes. A timestamp
  comparison does tell it, exactly.

* **Half the directories are empty.** Agents pre-create scaffolding (``data/``,
  ``results/``, ``outputs/figures/``) and then write somewhere else, often
  inventing a second home for the same thing (``data/report/`` *and*
  ``outputs/report/``). What is left is a tree where the empty folders outnumber
  the full ones and nobody can tell where anything lives.

So at the end of a turn we look at the actual files and hand the orchestrator the
facts: which presented outputs are older than the work that followed, which it
has never looked at, and which directories it left empty. Facts only — what to
do about them is its decision.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path

#: Directory names never counted as project content (tooling, caches, envs, and
#: Science's own bookkeeping — feed.jsonl changes constantly and would make
#: every deliverable look stale).
SKIP_DIRS = frozenset({
    "science", "lab", ".git", ".hg", ".svn", ".venv", "venv", "env",
    "node_modules", "__pycache__", ".ipynb_checkpoints", ".octo", ".cache",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", "site-packages", ".idea",
    ".vscode", ".DS_Store", "wandb", ".snakemake", ".nextflow",
})

#: Files that change as a side effect of running, not as a result of work.
SKIP_SUFFIXES = (".log", ".out", ".err", ".pyc", ".pyo", ".tmp", ".swp",
                 ".lock", ".part", ".crdownload")
SKIP_NAMES = frozenset({".DS_Store", "Thumbs.db", "state.json", "feed.jsonl"})

#: Bounds so a scan over a big data directory stays cheap.
_MAX_ENTRIES = 40_000
_MAX_DEPTH = 6

#: Suffixes whose layout can be wrong in ways only looking at them reveals.
VISUAL_SUFFIXES = frozenset({".html", ".htm", ".png", ".jpg", ".jpeg", ".svg",
                             ".webp", ".pdf"})

#: Deliverable kinds worth tracking for staleness, most important first.
_KIND_ORDER = {"report": 0, "dataset": 1, "table": 2, "image": 3}


def _skip_file(name: str) -> bool:
    return name in SKIP_NAMES or name.endswith(SKIP_SUFFIXES) or name.startswith(".")


def scan(working_dir: str) -> tuple[list[tuple[str, float]], list[str]]:
    """Walk ``working_dir`` once. Returns ``(files, empty_dirs)`` where ``files``
    is ``[(abs_path, mtime)]`` of real project content and ``empty_dirs`` holds
    directories (relative, slash-suffixed) with no content anywhere beneath
    them."""
    root = Path(working_dir)
    files: list[tuple[str, float]] = []
    seen = 0

    def rel(d: Path) -> str:
        try:
            return str(d.relative_to(root)) + "/"
        except ValueError:
            return str(d) + "/"

    def walk(d: Path, depth: int) -> tuple[int, list[str]]:
        """``(content files in this subtree, empty dirs worth reporting)``.

        An empty branch is reported by its TOPMOST empty directory only: naming
        ``md/``, ``md/ff/`` and ``md/ff/parts/`` separately turns one mistake
        into three lines of noise.
        """
        nonlocal seen
        if depth > _MAX_DEPTH or seen >= _MAX_ENTRIES:
            return 1, []      # unexplored: assume content, never call it empty
        count = 0
        subdirs: list[Path] = []
        try:
            with os.scandir(d) as it:
                for e in it:
                    seen += 1
                    if seen >= _MAX_ENTRIES:
                        return count + 1, []
                    try:
                        if e.is_dir(follow_symlinks=False):
                            if e.name not in SKIP_DIRS and not e.name.startswith("."):
                                subdirs.append(Path(e.path))
                        elif e.is_file(follow_symlinks=False):
                            if not _skip_file(e.name):
                                count += 1
                                files.append((e.path, e.stat().st_mtime))
                    except OSError:
                        continue
        except OSError:
            return 1, []      # unreadable: not our business to call it empty
        reports: list[str] = []
        for sub in subdirs:
            n, sub_reports = walk(sub, depth + 1)
            count += n
            # An empty subtree collapses to its own name; a subtree with content
            # passes up whatever empty branches it found inside itself.
            if n == 0:
                reports.append(rel(sub))
            else:
                reports.extend(sub_reports)
        return count, reports

    _, empty = walk(root, 0)
    return files, empty


#: Files written beside a deliverable this soon after it are the tail of the same
#: build (a script that writes the page, then the CSV it links to), not newer work.
_BUILD_GRACE = 120.0


def stale_outputs(session, working_dir: str, files: list[tuple[str, float]],
                  viewed_at=None, limit: int = 3, since: float | None = None) -> list[dict]:
    """Presented outputs that the work has moved on from, or that were never
    looked at.

    Only a REPORT can be behind the work: it is the thing that summarises the
    rest. A plot or a table does not go stale because something else was written
    after it — flagging them made the report built FROM them look like it had
    outrun them (seen live). And a newer presented report is downstream of an
    older one, not new work. So for each non-interim report: the content files
    written after it, except other presented reports and the tail of its own
    build (see _BUILD_GRACE).

    Any visual output is also checked for having been looked at:
    ``viewed_at(path)`` — when given — returns when the agent last actually
    looked at that file, so a page or figure changed since it was last
    inspected is flagged.

    ``since`` limits both checks to what happened from that moment on (the start
    of the turn): newer work must be from this turn, and "never looked at" only
    applies to an output that changed this turn. Without it, a file the user
    edited, or an output the orchestrator already decided to leave alone, would
    be raised again on every turn.
    """
    out: list[dict] = []
    arts = list(getattr(session, "artifacts", []))
    downstream = {os.path.abspath(a.path) for a in arts if a.kind == "report"}
    for art in arts:
        if art.interim:
            continue
        p = Path(art.path)
        try:
            m_art = p.stat().st_mtime
        except OSError:
            continue              # presented then deleted/moved — not our call
        home = os.path.dirname(os.path.abspath(art.path))
        newer = []
        for fp, m in (files if art.kind == "report" else []):
            if m <= m_art + 1.0 or (since is not None and m < since):
                continue
            afp = os.path.abspath(fp)
            if afp == os.path.abspath(art.path) or afp in downstream:
                continue
            if afp.startswith(home + os.sep) and m - m_art < _BUILD_GRACE:
                continue          # written by the same build, just after it
            newer.append((fp, m))
        unseen = False
        if p.suffix.lower() in VISUAL_SUFFIXES and viewed_at is not None \
                and (since is None or m_art >= since):
            seen_at = viewed_at(str(p))
            unseen = seen_at is None or seen_at < m_art
        if not newer and not unseen:
            continue
        newer.sort(key=lambda t: t[1], reverse=True)
        out.append({
            "rel": art.rel or art.path,
            "kind": art.kind,
            "age": time.time() - m_art,
            "n_newer": len(newer),
            "newest": [os.path.relpath(fp, working_dir) for fp, _ in newer[:4]],
            "unseen": unseen,
            "visual": p.suffix.lower() in VISUAL_SUFFIXES,
        })
    out.sort(key=lambda d: (_KIND_ORDER.get(d["kind"], 4), -d["n_newer"]))
    return out[:limit]


#: Directory names that name a KIND of output. The same one holding files in two
#: places is the "two homes for the report" failure: ``data/report/`` and
#: ``outputs/report/`` both full, and nobody can say which is the real one.
#: Deliberately limited to output buckets — repeated ``src/`` or ``tests/`` under
#: several subprojects is normal structure, not confusion.
OUTPUT_BUCKETS = frozenset({
    "report", "reports", "results", "result", "outputs", "output", "figures",
    "figure", "figs", "plots", "tables", "data", "datasets", "analysis",
})


def bucket_homes(files: list[tuple[str, float]], working_dir: str) -> dict[str, frozenset[str]]:
    """{bucket name: the directories (relative, slash-suffixed) holding files}."""
    homes: dict[str, set[str]] = {}
    for fp, _ in files:
        d = os.path.dirname(fp)
        name = os.path.basename(d).lower()
        if name in OUTPUT_BUCKETS:
            homes.setdefault(name, set()).add(os.path.relpath(d, working_dir) + "/")
    return {k: frozenset(v) for k, v in homes.items()}


def split_buckets(files: list[tuple[str, float]], working_dir: str,
                  limit: int = 3, before: dict | None = None) -> list[tuple[str, list[str]]]:
    """Output buckets that hold content in more than one place.

    With ``before`` (bucket_homes at the start of the turn), only buckets that
    GAINED a home during the turn are returned: a layout that was already split —
    possibly the user's own — is not raised again and again.
    """
    out = []
    for name, dirs in bucket_homes(files, working_dir).items():
        if len(dirs) < 2:
            continue
        if before is not None and not (dirs - before.get(name, frozenset())):
            continue
        out.append((name, sorted(dirs)))
    out.sort(key=lambda t: -len(t[1]))
    return out[:limit]


@dataclass(frozen=True)
class Baseline:
    """The workspace as a turn found it, so the end of the turn reports only
    what the turn itself did."""
    at: float
    empty: frozenset
    buckets: dict


def baseline(working_dir: str) -> Baseline:
    files, empty = scan(working_dir)
    return Baseline(at=time.time(), empty=frozenset(empty),
                    buckets=bucket_homes(files, working_dir))


def _ago(seconds: float) -> str:
    if seconds < 90:
        return "just now"
    if seconds < 5400:
        return f"{seconds / 60:.0f} min ago"
    return f"{seconds / 3600:.1f} h ago"


#: The orchestrator's whole reply when the workspace check needs no action. The
#: orchestrator then keeps its real closing reply as the turn's answer and the
#: exchange is dropped — otherwise a reply to a note the researcher never saw
#: becomes their closing message (seen live).
NOTHING_TO_CHANGE = "NOTHING TO CHANGE"


def is_nothing_to_change(text: str) -> bool:
    return (text or "").strip().strip(".!*`_ ").upper() == NOTHING_TO_CHANGE


#: Fewer empty directories than this is untidiness, not confusion — not worth
#: spending a round on by themselves (they are still listed when the note goes
#: out for another reason).
EMPTY_DIR_FLOOR = 3


def wrap_up_note(session, working_dir: str, viewed_at=None,
                 since: Baseline | None = None) -> str:
    """What the orchestrator should settle before the turn ends, or "".

    Sent as the harness, not as the researcher, and only when there is something
    concrete: an output the work has outrun, a page nobody has looked at, or a
    tree littered with empty directories.

    ``since`` (the turn's Baseline) restricts every finding to what THIS turn
    did. Each problem is then raised once, in the turn that caused it — and never
    about the user's own pre-existing layout, which is not ours to rearrange.
    """
    files, empty = scan(working_dir)
    stale = stale_outputs(session, working_dir, files, viewed_at=viewed_at,
                          since=since.at if since else None)
    empty = [d for d in sorted(empty) if d.rstrip("/") not in SKIP_DIRS
             and (since is None or d not in since.empty)]
    split = split_buckets(files, working_dir, before=since.buckets if since else None)
    if not stale and not split and len(empty) < EMPTY_DIR_FLOOR:
        return ""

    parts = ["[Workspace check from the harness — not from the researcher. "
             "Facts about the files on disk; decide for yourself what matters.]"]
    for s in stale:
        bits = [f"- **{s['rel']}** ({s['kind']}), last written {_ago(s['age'])}."]
        if s["n_newer"]:
            bits.append(f" {s['n_newer']} file(s) have been written since, including: "
                        + ", ".join(s["newest"]) + ".")
        if s["unseen"]:
            bits.append(" You have not looked at it since it last changed.")
        parts.append("".join(bits))
    if stale:
        parts.append(
            "If one of these is the deliverable the researcher asked for, it is "
            "now behind the work: regenerate it from the current results, look at "
            "it (view_image — for a page that shows you the layout, its console "
            "errors and any broken images), then present_output it again.")
    if split:
        for name, dirs in split:
            parts.append(f"- '{name}' holds files in {len(dirs)} places: "
                         + ", ".join(dirs))
        parts.append(
            "Outputs of one kind living in several trees means neither the "
            "researcher nor you can say which is current. Pick the one that is "
            "real, move the rest into it, and use only that path from now on — "
            "including in the briefs you give specialists.")
    if empty:     # alone they must reach EMPTY_DIR_FLOOR; alongside, list any
        shown = ", ".join(empty[:8]) + (f" (+{len(empty) - 8} more)" if len(empty) > 8 else "")
        parts.append(
            f"Empty directories: {shown}\n"
            "A tree whose empty folders outnumber the full ones hides where the "
            "work actually is. Remove the ones nothing will use, and from here on "
            "create a directory only when you write the first file into it.")
    parts.append(
        "The researcher does not see this note, so do not answer it. If you change "
        "something, end with a short message telling them what you updated. If "
        f"nothing needs changing, reply with exactly: {NOTHING_TO_CHANGE}")
    return "\n".join(parts)
