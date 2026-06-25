"""
Shared research helpers — role metadata and HTML report post-processing.

The original fixed multi-agent research *pipeline* (``run_long_research`` and its
per-role specialists) has been retired and fully replaced by the dynamic Lab
(see :mod:`octoslave.lab`). What remains here is the small set of helpers that
are still imported by the rest of the codebase:

* :data:`ROLES` — role label/icon/colour metadata, used by ``display`` for the
  TUI and by anything that still wants a friendly name for a role.
* :func:`_postprocess_report_html` — repairs/embeds images and normalises styles
  on a generated HTML report; the Lab reporter calls this on its final report.

Keeping these in ``octoslave.research`` preserves the existing import paths
(``from .research import ROLES`` / ``from ..research import _postprocess_report_html``).
"""

from __future__ import annotations

import re
from pathlib import Path

# ---------------------------------------------------------------------------
# Role metadata
# ---------------------------------------------------------------------------
# Friendly label / icon / colour per role. ``display._get_role_cfg`` reads the
# label/icon/color fields; the remaining fields are retained for reference and
# for any caller that wants the historical defaults.

ROLES: dict[str, dict] = {
    "researcher": {
        "label": "Researcher",
        "icon": "🔬",
        "color": "bold cyan",
        "default_model": "deepseek-v3.2-thinking",
        "max_iter": 25,
        "tools": ["read_file", "write_file", "web_search", "web_fetch",
                  "list_dir", "glob",
                  "bio_inspect", "uniprot_lookup", "pubchem_lookup",
                  "chembl_lookup", "geo_search", "ena_fetch",
                  "pdb_fetch", "alphafold_fetch",
                  "pdf_ocr"],
    },
    "hypothesis": {
        "label": "Experiment Designer",
        "icon": "💡",
        "color": "bold bright_magenta",
        "default_model": "deepseek-v3.2-thinking",
        "max_iter": 8,
        "tools": ["read_file", "write_file", "list_dir", "glob",
                  "bio_inspect", "rdkit_describe"],
    },
    "skeptic": {
        "label": "Skeptic",
        "icon": "🤨",
        "color": "bold magenta",
        "default_model": "deepseek-v3.2-thinking",
        "max_iter": 5,
        "tools": ["read_file", "write_file"],
    },
    "coder": {
        "label": "Coder",
        "icon": "💻",
        "color": "bold green",
        "default_model": "qwen3-coder-30b",
        "max_iter": 80,
        "tools": ["read_file", "write_file", "edit_file", "apply_patch", "bash",
                  "todo_write", "run_background", "check_process", "stop_process",
                  "glob", "grep", "list_dir",
                  "bio_inspect", "rdkit_describe", "pdb_fetch",
                  "alphafold_fetch", "uniprot_lookup", "pubchem_lookup",
                  "chembl_lookup", "ena_fetch", "pdf_ocr"],
    },
    "debugger": {
        "label": "Debugger",
        "icon": "🐛",
        "color": "bold red",
        "default_model": "qwen3-coder-30b",
        "max_iter": 30,
        "tools": ["read_file", "write_file", "edit_file", "apply_patch", "bash",
                  "todo_write", "run_background", "check_process", "stop_process",
                  "glob", "grep", "list_dir",
                  "bio_inspect", "rdkit_describe"],
    },
    "evaluator": {
        "label": "Evaluator",
        "icon": "⚖️ ",
        "color": "bold yellow",
        "default_model": "deepseek-v3.2-thinking",
        "max_iter": 15,
        "tools": ["read_file", "bash", "write_file", "list_dir",
                  "web_search", "glob",
                  "bio_inspect", "rdkit_describe"],
    },
    "orchestrator": {
        "label": "Orchestrator",
        "icon": "🧠",
        "color": "bold bright_white",
        "default_model": "deepseek-v3.2",
        "max_iter": 8,
        "tools": ["read_file", "write_file", "list_dir", "glob"],
    },
    "reporter": {
        "label": "Reporter",
        "icon": "📊",
        "color": "bold bright_cyan",
        "default_model": "deepseek-v3.2",
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


# ---------------------------------------------------------------------------
# HTML report post-processing
# ---------------------------------------------------------------------------

_POSTPROCESSOR_CSS = """
<style id="ots-postprocessor-overrides">
  /* Fix white-on-white text in cards/abstracts (common 49B mistake). */
  .card, .abstract, .timeline-table, .visuals-gallery,
  .research-interpretation, .details {
      color: #0d1117;
  }
  .card *, .abstract *, .timeline-table *, .visuals-gallery *,
  .research-interpretation * { color: inherit; }
  .timeline-table table { width: 100%; border-collapse: collapse; background: #fff;
      border-radius: 8px; overflow: hidden; color: #0d1117; }
  .timeline-table th { background: #161b22; color: #fff; padding: 0.75rem 1rem;
      text-align: left; }
  .timeline-table td { padding: 0.75rem 1rem; border-top: 1px solid #e1e4e8; }
  .details { background: #fff !important; color: #0d1117; }
  .details summary { cursor: pointer; font-weight: 600; padding: 0.5rem 0; }
  .visuals-gallery img, .chart img { max-width: 100%; height: auto;
      border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,0.15); }
  body { line-height: 1.6; }
  h1, h2, h3 { letter-spacing: -0.01em; }
  /* Fallback box for images that failed to load (caught by onerror below) */
  .ots-img-missing { display: inline-block; padding: 1.5rem 2rem;
      background: #fff7d6; color: #5a4500; border: 1px dashed #c4a000;
      border-radius: 8px; font-style: italic; }
</style>
"""


def _wrap_details_divs(html: str) -> tuple[str, int]:
    """
    Convert <div class="details"><summary>...</summary>...</div> into
    <details class="details" open><summary>...</summary>...</details>,
    matching the OUTER </div> using a div-depth counter so nested <div>s
    (e.g. <div class="chart">) don't get mistaken for the close tag.
    Returns (new_html, n_fixes_applied). Idempotent — already-correct HTML
    passes through unchanged.
    """
    open_re = re.compile(
        r'<div\s+class=(["\'])details\1\s*>(\s*)<summary>([^<]+)</summary>',
        re.IGNORECASE,
    )
    out_parts: list[str] = []
    cursor = 0
    n_fixes = 0
    tag_re = re.compile(r'<\s*(/?)\s*(div|details)\b[^>]*>', re.IGNORECASE)

    for m in open_re.finditer(html):
        # Emit text up to the match unchanged
        out_parts.append(html[cursor:m.start()])
        # Walk forward from m.end() balancing div depth (we entered at depth 1)
        depth = 1
        i = m.end()
        close_match = None
        for sub in tag_re.finditer(html, i):
            slash, name = sub.group(1), sub.group(2).lower()
            if name == "div":
                if not slash:
                    depth += 1
                else:
                    depth -= 1
                    if depth == 0:
                        close_match = sub
                        break
            # Ignore nested details; if model already used <details>, we treat
            # them as opaque — we still only count <div> depth.
        if close_match is None:
            # Unbalanced — leave this block alone, copy original text and move on.
            out_parts.append(html[m.start():m.end()])
            cursor = m.end()
            continue

        # Replace open tag with <details ...> and the matched </div> with </details>
        out_parts.append(
            f'<details class="details" open>{m.group(2)}<summary>{m.group(3)}</summary>'
        )
        out_parts.append(html[m.end():close_match.start()])
        out_parts.append("</details>")
        cursor = close_match.end()
        n_fixes += 1

    out_parts.append(html[cursor:])
    return "".join(out_parts), n_fixes


def _postprocess_report_html(report_path: Path) -> int:
    """Repair common Reporter failures on the written HTML. Returns n fixes applied."""
    research_dir = report_path.parent
    html = report_path.read_text(encoding="utf-8", errors="replace")
    fixes = 0

    # Collect all real PNGs available on disk (relative to research_dir, sorted by round).
    available_pngs: list[Path] = sorted(research_dir.glob("round_*/03_code/results/*.png"))
    available_pngs += sorted(research_dir.glob("round_*/05_scores_chart.png"))
    available_pngs += sorted(research_dir.glob("*.png"))
    rel_pngs: list[str] = [str(p.relative_to(research_dir)) for p in available_pngs]

    # 1. Replace truncated base64 placeholders with relative paths to real PNGs.
    #    A real base64 PNG src is thousands of chars long; if we see one ending in
    #    `...` or `…` or shorter than 500 chars, it's a placeholder.
    placeholder_re = re.compile(
        r'src=(?P<q>["\'])data:image/[^;]+;base64,(?P<data>[^"\']*?)(?P=q)',
        re.IGNORECASE,
    )

    pool = list(rel_pngs)  # consume in order
    used: set[str] = set()

    def _replace_placeholder(m: re.Match) -> str:
        nonlocal fixes
        data = m.group("data")
        # Heuristic: real base64 of a chart PNG is usually > 5000 chars; ellipsis/truncation
        # markers are dead giveaways.
        if (len(data) >= 500 and not data.endswith("...") and not data.endswith("…")
                and "..." not in data[-10:]):
            return m.group(0)  # looks legit, keep it
        if not pool:
            # No real PNG to substitute — make the failure visible as a labelled box.
            fixes += 1
            return ('src="data:image/svg+xml;utf8,'
                    '<svg xmlns=%22http://www.w3.org/2000/svg%22 width=%22600%22 height=%22120%22>'
                    '<rect width=%22100%25%22 height=%22100%25%22 fill=%22%23fff7d6%22/>'
                    '<text x=%2250%25%22 y=%2255%25%22 font-family=%22sans-serif%22 '
                    'font-size=%2218%22 text-anchor=%22middle%22 fill=%22%235a4500%22>'
                    'Image missing — Reporter wrote a placeholder; no PNG available'
                    '</text></svg>"')
        path = pool.pop(0)
        used.add(path)
        fixes += 1
        return f'src="{path}"'

    new_html = placeholder_re.sub(_replace_placeholder, html)

    # 1b. Embed every <img src="<relative path>"> as base64 so the report is
    #     self-contained (shareable as a single .html file). Skips data: URIs
    #     and absolute http(s) URLs. Caps individual files at 8 MB; missing
    #     files fall through to the onerror box added in step 5.
    import base64, mimetypes
    embed_re = re.compile(r'<img\b([^>]*?)\bsrc=(["\'])(?P<src>[^"\']+)\2', re.IGNORECASE)
    MAX_EMBED_BYTES = 8 * 1024 * 1024

    def _embed(m: re.Match) -> str:
        nonlocal fixes
        src = m.group("src")
        if src.startswith(("data:", "http://", "https://", "//")):
            return m.group(0)
        # Resolve relative to the research_dir
        candidate = (research_dir / src).resolve()
        try:
            candidate.relative_to(research_dir.resolve())
        except ValueError:
            return m.group(0)  # outside research dir — leave alone
        if not candidate.is_file():
            return m.group(0)  # missing — onerror will show a labelled box
        size = candidate.stat().st_size
        if size > MAX_EMBED_BYTES:
            return m.group(0)  # too large to inline
        mime, _ = mimetypes.guess_type(candidate.name)
        if not mime or not mime.startswith("image/"):
            return m.group(0)
        try:
            b64 = base64.b64encode(candidate.read_bytes()).decode("ascii")
        except OSError:
            return m.group(0)
        fixes += 1
        # Preserve the original <img attrs> by reusing the captured prefix and quote.
        prefix = m.group(1)
        q = m.group(2)
        return f'<img{prefix}src={q}data:{mime};base64,{b64}{q}'

    new_html = embed_re.sub(_embed, new_html)

    # 2. Fix the stray `)>` HTML attribute syntax error this Reporter is fond of.
    new_html, n_paren = re.subn(r'(["\'])\)>', r'\1>', new_html)
    fixes += n_paren

    # 3. Wrap loose <summary> ... </summary> inside <div class="details"> ... </div>
    #    in proper <details> ... </details> elements. Depth-correct: closes on
    #    the OUTER </div>, not on a nested one (e.g. <div class="chart">...</div>).
    new_html, n_details = _wrap_details_divs(new_html)
    fixes += n_details

    # 4. Inject our overrides CSS so contrast / table / image styles are sane,
    #    regardless of what the model wrote.
    if "ots-postprocessor-overrides" not in new_html and "</head>" in new_html:
        new_html = new_html.replace("</head>", _POSTPROCESSOR_CSS + "</head>", 1)
        fixes += 1

    # 5. Add img onerror that converts a failed image to a labelled missing-box,
    #    so future runs degrade visibly instead of showing a broken-image icon.
    new_html, n_img = re.subn(
        r'<img\b(?![^>]*onerror=)([^>]*)>',
        r'<img\1 onerror="this.outerHTML=\'<div class=&quot;ots-img-missing&quot;>'
        r'image not found: \'+(this.alt||this.src)+\'</div>\'">',
        new_html,
    )
    if n_img:
        fixes += 1  # count as one logical fix

    if new_html != html:
        report_path.write_text(new_html, encoding="utf-8")
    return fixes
