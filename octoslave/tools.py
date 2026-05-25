import json
import os
import shutil
import subprocess
import glob as glob_module
from pathlib import Path

from .tools_bio import BIO_TOOL_DEFINITIONS, BIO_TOOL_NAMES, execute_bio_tool

# Optional web deps — imported lazily inside functions
try:
    import requests as _requests
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False

try:
    from ddgs import DDGS as _DDGS
    _HAS_DDG = True
except ImportError:
    try:
        from duckduckgo_search import DDGS as _DDGS
        _HAS_DDG = True
    except ImportError:
        _HAS_DDG = False

try:
    from bs4 import BeautifulSoup as _BS4
    _HAS_BS4 = True
except ImportError:
    _HAS_BS4 = False

try:
    from playwright.sync_api import sync_playwright as _sync_playwright
    _HAS_PLAYWRIGHT = True
except ImportError:
    _HAS_PLAYWRIGHT = False

# Tools that require permission in controlled mode
MODIFYING_TOOLS = {"write_file", "edit_file", "bash"}

# Tools that require permission only in controlled mode (not supervised)
FILE_MODIFYING_TOOLS = {"write_file", "edit_file"}

# ---------------------------------------------------------------------------
# Tool schemas (sent to the model)
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Read the contents of a file. Always read a file before editing it. "
                "Returns line-numbered content."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute or relative file path"},
                    "offset": {"type": "integer", "description": "Start line (1-indexed, optional)"},
                    "limit": {"type": "integer", "description": "Max lines to read (optional)"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "Create a new file or completely overwrite an existing file. "
                "Prefer edit_file for modifying existing files."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path to write"},
                    "content": {"type": "string", "description": "Full file content"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": (
                "Make a targeted edit by replacing an exact string with a new string. "
                "By default old_string must appear exactly once (uniquely). "
                "Set replace_all=true to replace every occurrence — useful for renames "
                "and global refactors. Preserve indentation and surrounding context exactly."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path to edit"},
                    "old_string": {"type": "string", "description": "Exact string to replace. Must be unique unless replace_all is true."},
                    "new_string": {"type": "string", "description": "Replacement string"},
                    "replace_all": {"type": "boolean", "description": "Replace every occurrence instead of requiring uniqueness (default false)"},
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": (
                "Execute a shell command and return stdout + stderr. "
                "Use for running tests, installing packages, building, git operations, etc. "
                "Do NOT start long-running servers or blocking processes (e.g. 'python app.py', 'flask run') — "
                "they will block until timeout. To verify a web app, use syntax checks or import tests instead. "
                "Default timeout is 300 s. "
                "For longer jobs set timeout explicitly: e.g. 600 for a slow test suite or large pip install, 28800 for ML training."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to run"},
                    "timeout": {"type": "integer", "description": "Timeout in seconds (default 300). Increase for slow installs (600), full test suites (600-3600), or ML training (28800-86400). No hard cap."},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "glob",
            "description": "Find files matching a glob pattern. Returns list of matching paths.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Glob pattern, e.g. '**/*.py' or 'src/**/*.ts'"},
                    "path": {"type": "string", "description": "Root directory (defaults to working dir)"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": "Search file contents with a regex pattern. Returns matching file:line:content entries.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Regex pattern to search"},
                    "path": {"type": "string", "description": "File or directory to search (defaults to working dir)"},
                    "glob": {"type": "string", "description": "File filter, e.g. '*.py'"},
                    "case_insensitive": {"type": "boolean", "description": "Case-insensitive search"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List the contents of a directory with file sizes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory path (defaults to working dir)"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Search the web using DuckDuckGo. Returns titles, URLs, and snippets. "
                "Use for finding research papers, documentation, current events, or any web information. "
                "Then use web_fetch to read the full content of promising URLs."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "max_results": {"type": "integer", "description": "Max results to return (default 10, max 20)"},
                    "region": {"type": "string", "description": "Region code e.g. 'us-en', 'wt-wt' (worldwide). Default 'wt-wt'"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": (
                "Fetch and extract readable text content from a URL. "
                "Strips HTML tags, navigation, and ads to return the main text. "
                "Use after web_search to read full articles, papers, or documentation pages."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to fetch"},
                    "max_chars": {"type": "integer", "description": "Max characters to return (default 8000)"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "crawl_tree",
            "description": (
                "Crawl a website tree starting from a root URL using BFS. "
                "Follows internal links up to max_depth levels deep and returns a JSON tree "
                "of {url: {title, text_snippet, children, depth}}. "
                "Uses Playwright (handles JS-rendered pages) if available, otherwise requests+BeautifulSoup. "
                "Ideal for scraping category trees, product hierarchies, documentation trees, or any "
                "multi-level site structure. Save results with output_path."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "root_url": {"type": "string", "description": "Starting URL to crawl from"},
                    "link_selector": {"type": "string", "description": "CSS selector for links to follow (default 'a')"},
                    "url_pattern": {"type": "string", "description": "Regex — only follow URLs matching this pattern (optional)"},
                    "max_depth": {"type": "integer", "description": "Maximum depth to crawl (default 3)"},
                    "max_pages": {"type": "integer", "description": "Maximum number of pages to visit (default 100)"},
                    "same_domain": {"type": "boolean", "description": "Only follow links on the same domain (default true)"},
                    "use_js": {"type": "boolean", "description": "Force Playwright even for static sites (default false — auto-selects best engine)"},
                    "output_path": {"type": "string", "description": "If given, save full JSON tree to this file path"},
                },
                "required": ["root_url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "compress_log",
            "description": (
                "Compress a long log, training output, or noisy command output into a compact "
                "templated summary using the `codag` CLI. Drastically reduces context cost "
                "(typically 95–99% token reduction) while preserving rare events like errors "
                "and tracebacks verbatim. "
                "USE THIS instead of read_file or bash whenever you would otherwise read a log "
                "longer than ~500 lines: ML training logs, kubectl/docker logs, CI output, "
                "stack traces from a failed run, anything tee'd to disk. "
                "Provide exactly ONE of `command` (codag runs and wraps it) or `path` "
                "(codag reads a local file). Default mode `compact` is plain text, free, "
                "no auth. Mode `capsule` returns structured JSON but requires `codag auth login`."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command whose output should be wrapped and compressed (e.g. 'python train.py --epochs 10'). Mutually exclusive with `path`."},
                    "path": {"type": "string", "description": "Path to an existing log file to compress (absolute or relative to working dir). Mutually exclusive with `command`."},
                    "service": {"type": "string", "description": "Optional tag for these lines (e.g. 'train', 'api'). Helps codag's template inference."},
                    "mode": {"type": "string", "description": "Output mode: 'compact' (default, plain text, no auth) or 'capsule' (JSON, requires auth)."},
                    "timeout": {"type": "integer", "description": "Max seconds for the wrapped command. Default 600 for compress_log of a file, matches the inner command's needs otherwise."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "image_ocr",
            "description": (
                "Extract text from an image file (PNG, JPG/JPEG, TIFF, BMP, GIF, WEBP) "
                "using tesseract OCR. Use for screenshots, scanned documents, photos of "
                "text, figures with embedded labels, plots with axis ticks, etc. "
                "For PDFs use pdf_ocr instead. "
                "Requires pytesseract + the `tesseract` binary "
                "(macOS: `brew install tesseract`, Debian: `apt install tesseract-ocr`)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to image file"},
                    "lang": {"type": "string", "description": "Tesseract language code (default 'eng'). Use 'eng+deu' for multilingual."},
                    "preprocess": {"type": "boolean", "description": "Apply grayscale + threshold for noisy/low-contrast images (default false)"},
                    "psm": {"type": "integer", "description": "Tesseract page segmentation mode (default 3 = auto). Try 6 for a single block of text, 11 for sparse text."},
                },
                "required": ["path"],
            },
        },
    },
] + BIO_TOOL_DEFINITIONS


# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------

def execute_tool(name: str, args: dict, working_dir: str, permission_mode: str = "autonomous") -> tuple[str, bool]:
    """Execute a tool. Returns (result_text, success)."""
    # Check permission for modifying tools
    # - controlled: ask for all modifying tools (file ops + bash)
    # - supervised: ask only for file operations (allow bash without asking)
    if permission_mode == "controlled" and name in MODIFYING_TOOLS:
        try:
            from . import display
            if not display.request_permission(name, args, working_dir, permission_mode):
                return f"Permission denied by user for {name}", False
        except Exception:
            # Fallback if display module has issues
            pass
    elif permission_mode == "supervised" and name in FILE_MODIFYING_TOOLS:
        try:
            from . import display
            if not display.request_permission(name, args, working_dir, permission_mode):
                return f"Permission denied by user for {name}", False
        except Exception:
            # Fallback if display module has issues
            pass
    
    # Reject empty / missing required string args BEFORE dispatch — some
    # models (notably nemotron-49b acting as Orchestrator) emit tool calls
    # with `path=""` etc. and a generic "File not found:" reply does not
    # help them recover. Return an actionable message instead.
    err = _validate_required_args(name, args)
    if err is not None:
        return err, False

    try:
        if name == "read_file":
            return _read_file(working_dir=working_dir, **args)
        elif name == "write_file":
            return _write_file(working_dir=working_dir, **args)
        elif name == "edit_file":
            return _edit_file(working_dir=working_dir, **args)
        elif name == "bash":
            return _bash(working_dir=working_dir, **args)
        elif name == "glob":
            return _glob(working_dir=working_dir, **args)
        elif name == "grep":
            return _grep(working_dir=working_dir, **args)
        elif name == "list_dir":
            return _list_dir(working_dir=working_dir, **args)
        elif name == "web_search":
            return _web_search(**args)
        elif name == "web_fetch":
            return _web_fetch(**args)
        elif name == "crawl_tree":
            return _crawl_tree(**args)
        elif name == "compress_log":
            return _compress_log(working_dir=working_dir, **args)
        elif name == "image_ocr":
            return _image_ocr(working_dir=working_dir, **args)
        elif name in BIO_TOOL_NAMES:
            return execute_bio_tool(name, args, working_dir)
        else:
            return f"Unknown tool: {name}", False
    except TypeError as e:
        return f"Invalid arguments for {name}: {e}", False
    except Exception as e:
        return f"Tool error: {e}", False


# Required string args per tool — used by _validate_required_args.
# Tools accepting `working_dir` as the only "input" (list_dir) intentionally
# allow path="" to mean "the working dir itself".
_REQUIRED_STR_ARGS: dict[str, tuple[str, ...]] = {
    "read_file": ("path",),
    "write_file": ("path", "content"),
    "edit_file": ("path", "old_string", "new_string"),
    "bash": ("command",),
    "glob": ("pattern",),
    "grep": ("pattern",),
    "web_search": ("query",),
    "web_fetch": ("url",),
    "crawl_tree": ("root_url",),
    "image_ocr": ("path",),
    # bio tools that need a string input
    "bio_inspect": ("path",),
    "rdkit_describe": ("smiles",),
    "pdb_fetch": ("pdb_id",),
    "alphafold_fetch": ("uniprot_id",),
    "ena_fetch": ("accession",),
    "pdf_ocr": ("path",),
}


def _validate_required_args(name: str, args: dict) -> str | None:
    """Return an actionable error string if a required string arg is missing/empty, else None."""
    if not isinstance(args, dict):
        return f"Tool `{name}` requires JSON object arguments, got {type(args).__name__}."
    required = _REQUIRED_STR_ARGS.get(name)
    if not required:
        return None
    missing: list[str] = []
    empty: list[str] = []
    for key in required:
        # `content` for write_file may legitimately be empty (creating empty file)
        if name == "write_file" and key == "content":
            if key not in args:
                missing.append(key)
            continue
        if key not in args:
            missing.append(key)
            continue
        v = args[key]
        if v is None or (isinstance(v, str) and v.strip() == ""):
            empty.append(key)
    if not missing and not empty:
        return None
    parts = []
    if missing:
        parts.append(f"missing argument(s): {', '.join(missing)}")
    if empty:
        parts.append(f"empty argument(s): {', '.join(empty)}")
    hint = _ARG_HINTS.get(name, "")
    return (
        f"Cannot run `{name}` — {'; '.join(parts)}. "
        f"{hint} "
        f"Re-issue the call with the required argument filled in."
    ).strip()


_ARG_HINTS: dict[str, str] = {
    "read_file": "Pass `path` as an absolute or working-dir-relative file path "
                 "(e.g. {\"path\": \"round_001/01_literature.md\"}).",
    "write_file": "Pass `path` and `content` (e.g. {\"path\": \"round_001/06_synthesis.md\", \"content\": \"...\"}).",
    "edit_file": "Pass `path`, `old_string`, `new_string`. Use replace_all=true for renames.",
    "bash": "Pass `command` as a shell string (e.g. {\"command\": \"ls\"}).",
    "glob": "Pass `pattern` (e.g. {\"pattern\": \"**/*.py\"}).",
    "grep": "Pass `pattern` as a regex (e.g. {\"pattern\": \"def \\\\w+\"}).",
    "web_search": "Pass `query`.",
    "web_fetch": "Pass `url` as a fully-qualified http(s) URL.",
    "crawl_tree": "Pass `root_url` as a fully-qualified http(s) URL.",
    "image_ocr": "Pass `path` to an image file (PNG/JPG/TIFF/BMP/GIF/WEBP).",
    "pdf_ocr": "Pass `path` to a PDF file. Use `pages` to limit page range.",
    "bio_inspect": "Pass `path` to a bio/chem data file.",
    "rdkit_describe": "Pass `smiles` (e.g. {\"smiles\": \"CC(=O)O\"}).",
    "pdb_fetch": "Pass `pdb_id` (4-char RCSB ID, e.g. \"1CRN\").",
    "alphafold_fetch": "Pass `uniprot_id` (e.g. \"P12345\").",
    "ena_fetch": "Pass `accession` (e.g. \"SRR000001\").",
}


def _resolve(path: str, working_dir: str) -> Path:
    p = Path(path)
    if not p.is_absolute():
        p = Path(working_dir) / p
    return p.resolve()


def _is_binary(path: Path) -> bool:
    """Quick check: read first 512 bytes and look for null bytes or high non-printable ratio."""
    try:
        chunk = path.read_bytes()[:512]
        if b"\x00" in chunk:
            return True
        non_printable = sum(1 for b in chunk if b < 9 or (14 <= b < 32))
        return non_printable / max(len(chunk), 1) > 0.30
    except OSError:
        return False


def _extract_pdf(resolved: Path, offset: int = None, limit: int = None) -> tuple[str, bool]:
    try:
        import fitz  # pymupdf
        doc = fitz.open(str(resolved))
        parts = [f"--- Page {i+1} ---\n{page.get_text()}" for i, page in enumerate(doc)]
        num_pages = len(doc)
    except ImportError:
        return "pymupdf not installed. Run: pip install pymupdf", False
    except Exception as e:
        return f"Could not open PDF: {e}", False

    if not any(p.strip() for p in parts):
        return "PDF contains no extractable text (may be scanned/image-based).", False

    full_text = "\n\n".join(parts)
    lines = full_text.splitlines()
    total = len(lines)

    start = (offset - 1) if offset and offset > 0 else 0
    end = (start + limit) if limit else total
    selected = lines[start:end]

    header = f"PDF: {resolved.name} ({num_pages} pages, {total} lines extracted)\n\n"
    return header + "\n".join(selected), True


def _extract_excel(resolved: Path, offset: int = None, limit: int = None) -> tuple[str, bool]:
    try:
        import openpyxl
    except ImportError:
        try:
            import subprocess, sys
            subprocess.check_call([sys.executable, "-m", "pip", "install", "openpyxl", "-q"])
            import openpyxl
        except Exception:
            return "openpyxl not installed. Run: pip install openpyxl", False

    try:
        wb = openpyxl.load_workbook(str(resolved), read_only=True, data_only=True)
    except Exception as e:
        return f"Could not open Excel file: {e}", False

    parts = []
    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        rows = []
        for row in ws.iter_rows(values_only=True):
            # Skip entirely empty rows
            if any(cell is not None for cell in row):
                rows.append("\t".join("" if v is None else str(v) for v in row))
        if rows:
            parts.append(f"=== Sheet: {sheet_name} ===\n" + "\n".join(rows))

    if not parts:
        return f"Excel file {resolved.name} appears empty.", False

    full_text = "\n\n".join(parts)
    lines = full_text.splitlines()
    total = len(lines)

    start = (offset - 1) if offset and offset > 0 else 0
    end = (start + limit) if limit else total
    selected = lines[start:end]

    header = f"Excel: {resolved.name} ({len(wb.sheetnames)} sheet(s), {total} lines)\n\n"
    return header + "\n".join(selected), True


def _extract_docx(resolved: Path, offset: int = None, limit: int = None) -> tuple[str, bool]:
    try:
        import docx as _docx
    except ImportError:
        try:
            import subprocess, sys
            subprocess.check_call([sys.executable, "-m", "pip", "install", "python-docx", "-q"])
            import docx as _docx
        except Exception:
            return "python-docx not installed. Run: pip install python-docx", False

    try:
        doc = _docx.Document(str(resolved))
    except Exception as e:
        return f"Could not open Word document: {e}", False

    parts = []
    for para in doc.paragraphs:
        if para.text.strip():
            parts.append(para.text)

    # Also extract tables
    for table in doc.tables:
        for row in table.rows:
            row_text = "\t".join(cell.text.strip() for cell in row.cells)
            if row_text.strip():
                parts.append(row_text)

    if not parts:
        return f"Word document {resolved.name} appears empty.", False

    full_text = "\n".join(parts)
    lines = full_text.splitlines()
    total = len(lines)

    start = (offset - 1) if offset and offset > 0 else 0
    end = (start + limit) if limit else total
    selected = lines[start:end]

    header = f"Word: {resolved.name} ({total} lines extracted)\n\n"
    return header + "\n".join(selected), True


def _read_file(path: str, working_dir: str, offset: int = None, limit: int = None) -> tuple[str, bool]:
    resolved = _resolve(path, working_dir)
    if not resolved.exists():
        return f"File not found: {path}", False
    if not resolved.is_file():
        return f"Not a file: {path}", False

    # PDF → extract text
    if resolved.suffix.lower() == ".pdf":
        return _extract_pdf(resolved, offset, limit)

    # Excel → convert to CSV-like text
    if resolved.suffix.lower() in (".xlsx", ".xls", ".xlsm", ".ods"):
        return _extract_excel(resolved, offset, limit)

    # Word → extract text
    if resolved.suffix.lower() in (".docx", ".doc"):
        return _extract_docx(resolved, offset, limit)

    # Other binary → reject early with a clear message
    if _is_binary(resolved):
        size = resolved.stat().st_size
        return (
            f"Binary file: {resolved.name} ({size:,} bytes). "
            "Cannot read as text. Use a dedicated tool or convert it first.",
            False,
        )

    try:
        lines = resolved.read_text(errors="replace").splitlines()
    except OSError as e:
        return str(e), False

    start = (offset - 1) if offset and offset > 0 else 0
    end = (start + limit) if limit else len(lines)
    selected = lines[start:end]

    numbered = "\n".join(f"{start + i + 1}\t{line}" for i, line in enumerate(selected))
    total = len(lines)
    header = f"File: {path} ({total} lines total)\n"
    return header + numbered, True


def _write_file(path: str, content: str, working_dir: str) -> tuple[str, bool]:
    resolved = _resolve(path, working_dir)
    size = len(content.encode("utf-8"))
    if size > _MAX_DOWNLOAD_BYTES:
        size_gb = size / (1024 ** 3)
        return (
            f"Refused to write {path} — content is {size_gb:.2f} GB, "
            f"exceeds the 1 GB limit.",
            False,
        )
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(content)
    lines = content.count("\n") + 1
    return f"Written {lines} lines to {path}", True


def _edit_file(
    path: str,
    old_string: str,
    new_string: str,
    working_dir: str,
    replace_all: bool = False,
) -> tuple[str, bool]:
    resolved = _resolve(path, working_dir)
    if not resolved.exists():
        return f"File not found: {path}", False
    if not resolved.is_file():
        return f"Not a file: {path}", False
    if old_string == "":
        return "old_string must not be empty. To create a file, use write_file.", False
    if old_string == new_string:
        return "old_string and new_string are identical — no change needed.", False
    if _is_binary(resolved):
        return f"Cannot edit binary file: {resolved.name}", False

    content = resolved.read_text(errors="replace")
    count = content.count(old_string)

    if count == 0:
        return f"String not found in {path}:\n{old_string[:200]}", False
    if count > 1 and not replace_all:
        return (
            f"old_string appears {count} times in {path} — "
            f"make it more specific to ensure uniqueness, or pass replace_all=true to replace every occurrence.",
            False,
        )

    new_content = content.replace(old_string, new_string)
    resolved.write_text(new_content)
    if replace_all and count > 1:
        return f"Edited {path} ({count} occurrences replaced)", True
    return f"Edited {path}", True


def _bash(command: str, working_dir: str, timeout: int = 300) -> tuple[str, bool]:
    # Unset VIRTUAL_ENV so uv doesn't emit a mismatch warning when the conda/system
    # venv doesn't match the project's .venv.
    env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            cwd=working_dir,
            timeout=timeout,
            env=env,
        )
        stdout = result.stdout or ""
        stderr = result.stderr or ""
        # Label streams when both have content so the model can tell them apart.
        # Most successful commands write only to stdout — keep that path lean.
        if stdout and stderr:
            output = f"--- stdout ---\n{stdout}\n--- stderr ---\n{stderr}"
        else:
            output = stdout or stderr
        if not output:
            output = f"(exit code {result.returncode})"
        elif result.returncode != 0:
            output += f"\n(exit code {result.returncode})"
        # Truncate very long outputs — keep tail-heavy since errors appear at the end
        if len(output) > 8000:
            output = output[:2000] + "\n\n... [output truncated] ...\n\n" + output[-5000:]
        return output, result.returncode == 0
    except subprocess.TimeoutExpired:
        return f"Command timed out after {timeout}s", False


def _find_codag() -> str | None:
    """Locate the codag binary. Returns absolute path, or None if not installed."""
    found = shutil.which("codag")
    if found:
        return found
    # install.sh default location
    fallback = Path.home() / ".local" / "bin" / "codag"
    return str(fallback) if fallback.is_file() and os.access(fallback, os.X_OK) else None


def _compress_log(
    working_dir: str,
    command: str = None,
    path: str = None,
    service: str = None,
    mode: str = "compact",
    timeout: int = 600,
) -> tuple[str, bool]:
    """Compress a log/command-output via the `codag` CLI."""
    if (command and path) or (not command and not path):
        return (
            "compress_log requires exactly one of `command` or `path` "
            "(got both, or neither). Pass `path` to compress a file, or `command` "
            "to wrap and compress a shell command's output.",
            False,
        )

    codag = _find_codag()
    if codag is None:
        return (
            "codag CLI not installed. Install with: "
            "`curl -fsSL https://codag.ai/install.sh | sh` "
            "(installs to ~/.local/bin/codag — make sure that dir is on PATH).",
            False,
        )

    if mode not in ("compact", "capsule", "parse"):
        return f"Invalid mode '{mode}'. Use 'compact' (default, no auth) or 'capsule' (JSON, needs auth).", False

    argv: list[str] = [codag, "wrap", "--mode", mode]
    if service:
        argv += ["--service", service]

    if path:
        resolved = _resolve(path, working_dir)
        if not resolved.exists():
            return f"compress_log: file not found: {resolved}", False
        if not resolved.is_file():
            return f"compress_log: not a regular file: {resolved}", False
        # `codag wrap -- cat <path>` is the documented file shape.
        argv += ["--", "cat", str(resolved)]
    else:
        # `command` may include shell metacharacters / pipes — let a shell parse it.
        argv += ["--", "sh", "-c", command]

    env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            cwd=working_dir,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return f"compress_log timed out after {timeout}s", False
    except FileNotFoundError:
        return "codag binary disappeared between lookup and exec. Reinstall codag.", False

    stdout = result.stdout or ""
    stderr = result.stderr or ""
    # codag's stderr carries the `[codag] N→M tok (X% reduction)` summary line.
    # Surface it to the agent so it can see compression actually happened.
    summary = ""
    for line in stderr.splitlines():
        if line.startswith("[codag]"):
            summary = line.strip()
            break

    if result.returncode != 0:
        err_body = stderr.strip() or stdout.strip() or "(no output)"
        return (
            f"codag failed (exit {result.returncode}):\n{err_body}\n"
            f"Tip: `capsule` and `parse` modes require `codag auth login`; "
            f"`compact` mode is free and works without auth.",
            False,
        )

    output = stdout.strip()
    if summary:
        output = f"{summary}\n{output}"
    return output or "(codag returned empty output)", True


_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".tiff", ".tif", ".bmp", ".gif", ".webp"}


def _image_ocr(
    path: str,
    working_dir: str,
    lang: str = "eng",
    preprocess: bool = False,
    psm: int = 3,
) -> tuple[str, bool]:
    try:
        from PIL import Image
    except ImportError:
        return "Pillow is not installed. Run: pip install pillow", False
    try:
        import pytesseract  # type: ignore
    except ImportError:
        return "pytesseract is not installed. Run: pip install pytesseract", False

    resolved = _resolve(path, working_dir)
    if not resolved.exists():
        return f"File not found: {path}", False
    if not resolved.is_file():
        return f"Not a file: {path}", False
    if resolved.suffix.lower() not in _IMAGE_EXTS:
        return (
            f"Unsupported image format: {resolved.suffix!r}. "
            f"Supported: {sorted(_IMAGE_EXTS)}. For PDFs use pdf_ocr.",
            False,
        )

    try:
        img = Image.open(str(resolved))
        img.load()
    except Exception as e:
        return f"Could not open image: {e}", False

    width, height = img.size

    if preprocess:
        img = img.convert("L")
        img = img.point(lambda x: 0 if x < 140 else 255, "1")

    config = f"--psm {int(psm)}"
    try:
        text = pytesseract.image_to_string(img, lang=lang, config=config)
    except pytesseract.TesseractNotFoundError:
        return (
            "tesseract binary not found on PATH. Install: "
            "macOS `brew install tesseract`, Debian `apt install tesseract-ocr`.",
            False,
        )
    except Exception as e:
        return f"OCR error: {e}", False

    header = f"OCR: {resolved.name} ({width}x{height}, lang={lang}, psm={psm})\n\n"
    if not text.strip():
        hint = "" if preprocess else " Try preprocess=true for noisy/low-contrast images."
        return header + f"(no text extracted —{hint} image may contain no readable text)", True
    return header + text, True


def _glob(pattern: str, working_dir: str, path: str = None) -> tuple[str, bool]:
    # If pattern is an absolute path, split into root + relative pattern
    p = Path(pattern)
    if p.is_absolute():
        # Find the first glob character to split root from pattern
        parts = p.parts
        root_parts = []
        glob_parts = []
        in_glob = False
        for part in parts:
            if in_glob or any(c in part for c in ("*", "?", "[")):
                in_glob = True
                glob_parts.append(part)
            else:
                root_parts.append(part)
        root = Path(*root_parts) if root_parts else Path("/")
        pattern = str(Path(*glob_parts)) if glob_parts else "**/*"
    else:
        root = _resolve(path, working_dir) if path else Path(working_dir)
    matches = sorted(str(p) for p in root.glob(pattern))
    if not matches:
        return f"No files matching '{pattern}'", True
    # Show relative paths when possible
    result = []
    for m in matches[:200]:
        try:
            result.append(str(Path(m).relative_to(working_dir)))
        except ValueError:
            result.append(m)
    output = "\n".join(result)
    if len(matches) > 200:
        output += f"\n... and {len(matches) - 200} more"
    return output, True


def _grep(pattern: str, working_dir: str, path: str = None, glob: str = None, case_insensitive: bool = False) -> tuple[str, bool]:
    cmd = ["grep", "-rn", "--include=" + (glob or "*")]
    if case_insensitive:
        cmd.append("-i")
    cmd.append(pattern)
    cmd.append(path if path else working_dir)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, cwd=working_dir, timeout=30)
        output = result.stdout or result.stderr or "No matches found"
        if len(output) > 8000:
            lines = output.splitlines()
            output = "\n".join(lines[:150]) + f"\n... ({len(lines)} total matches, showing first 150)"
        return output, True
    except subprocess.TimeoutExpired:
        return "grep timed out", False


def _list_dir(working_dir: str, path: str = None) -> tuple[str, bool]:
    target = _resolve(path, working_dir) if path else Path(working_dir)
    if not target.exists():
        return f"Directory not found: {path}", False
    if not target.is_dir():
        return f"Not a directory: {path}", False

    entries = sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    lines = []
    for entry in entries:
        if entry.is_dir():
            lines.append(f"  {entry.name}/")
        else:
            size = entry.stat().st_size
            if size < 1024:
                sz = f"{size}B"
            elif size < 1024 * 1024:
                sz = f"{size // 1024}KB"
            else:
                sz = f"{size // (1024*1024)}MB"
            lines.append(f"  {entry.name:<40} {sz:>8}")

    header = f"{target}/\n"
    return header + "\n".join(lines), True


def _web_search(query: str, max_results: int = 10, region: str = "wt-wt") -> tuple[str, bool]:
    if not _HAS_DDG:
        return "duckduckgo-search package not installed. Run: pip install duckduckgo-search", False

    max_results = min(max_results, 20)
    try:
        with _DDGS(timeout=20) as ddgs:
            results = list(ddgs.text(query, region=region, max_results=max_results))
    except Exception as e:
        return f"Search failed: {e}", False

    if not results:
        return "No results found.", True

    lines = [f"Search results for: {query}\n"]
    for i, r in enumerate(results, 1):
        title = r.get("title", "(no title)")
        url = r.get("href", "")
        body = r.get("body", "")
        lines.append(f"[{i}] {title}")
        lines.append(f"    URL: {url}")
        if body:
            snippet = body[:200] + ("…" if len(body) > 200 else "")
            lines.append(f"    {snippet}")
        lines.append("")

    return "\n".join(lines), True


_MAX_DOWNLOAD_BYTES = 1 * 1024 * 1024 * 1024  # 1 GB hard limit


def _web_fetch(url: str, max_chars: int = 8000) -> tuple[str, bool]:
    if not _HAS_REQUESTS:
        return "requests package not installed. Run: pip install requests", False

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    }
    try:
        # HEAD request first — check Content-Length before downloading anything
        try:
            head = _requests.head(url, headers=headers, timeout=10,
                                  allow_redirects=True)
            content_length = int(head.headers.get("content-length", 0))
            if content_length > _MAX_DOWNLOAD_BYTES:
                size_gb = content_length / (1024 ** 3)
                return (
                    f"Refused to download {url} — "
                    f"Content-Length is {size_gb:.2f} GB, exceeds the 1 GB limit.",
                    False,
                )
        except Exception:
            pass  # HEAD not supported by all servers — proceed with GET

        resp = _requests.get(url, headers=headers, timeout=30,
                             allow_redirects=True, stream=True)
        resp.raise_for_status()

        # Check Content-Length from GET response headers too
        content_length = int(resp.headers.get("content-length", 0))
        if content_length > _MAX_DOWNLOAD_BYTES:
            resp.close()
            size_gb = content_length / (1024 ** 3)
            return (
                f"Refused to download {url} — "
                f"Content-Length is {size_gb:.2f} GB, exceeds the 1 GB limit.",
                False,
            )

        # Read with a rolling size guard (handles chunked/no Content-Length)
        chunks = []
        downloaded = 0
        for chunk in resp.iter_content(chunk_size=1024 * 1024):  # 1 MB chunks
            downloaded += len(chunk)
            if downloaded > _MAX_DOWNLOAD_BYTES:
                resp.close()
                return (
                    f"Aborted download of {url} — "
                    f"exceeded 1 GB limit after {downloaded / (1024**3):.2f} GB.",
                    False,
                )
            chunks.append(chunk)
        resp._content = b"".join(chunks)
    except _requests.exceptions.HTTPError as e:
        status = getattr(e.response, "status_code", None)
        if status == 403:
            return (
                f"Fetch failed: 403 Forbidden — {url}\n"
                "The page requires login or is behind a paywall. "
                "Try: (1) searching for a preprint or open-access version, "
                "(2) fetching https://web.archive.org/web/*/" + url + " for a cached copy, "
                "or (3) using web_search to find a summary of the content.",
                False,
            )
        return f"Fetch failed: {e}", False
    except Exception as e:
        return f"Fetch failed: {e}", False

    content_type = resp.headers.get("content-type", "")

    # PDF — extract text with pymupdf if available, else warn
    if "application/pdf" in content_type or url.lower().endswith(".pdf"):
        try:
            import fitz  # pymupdf
            doc = fitz.open(stream=resp.content, filetype="pdf")
            pages = []
            for i, page in enumerate(doc):
                pages.append(f"--- Page {i+1} ---\n{page.get_text()}")
            text = "\n\n".join(pages)
            if len(text) > max_chars:
                text = text[:max_chars] + f"\n\n… [truncated, {len(text)} total chars]"
            return f"PDF: {url}\n\n{text}", True
        except ImportError:
            return (
                f"Skipped {url} — PDF detected but pymupdf is not installed. "
                "Run: pip install pymupdf",
                False,
            )
        except Exception as e:
            return f"Failed to extract PDF text from {url}: {e}", False

    # Reject other binary content types
    _BINARY_TYPES = ("application/octet-stream", "image/", "audio/",
                     "video/", "application/zip", "application/x-", "application/vnd.")
    if any(content_type.startswith(bt) for bt in _BINARY_TYPES):
        return (
            f"Skipped {url} — binary content ({content_type}), cannot read.",
            False,
        )

    # Plain text / markdown / code — return as-is
    if "text/plain" in content_type or url.endswith((".md", ".txt", ".rst", ".py", ".js", ".ts")):
        text = resp.text[:max_chars]
        if len(resp.text) > max_chars:
            text += f"\n\n… [truncated, {len(resp.text)} total chars]"
        return text, True

    # HTML — extract main text via BeautifulSoup
    if _HAS_BS4:
        soup = _BS4(resp.text, "html.parser")
        # Remove noise elements
        for tag in soup(["script", "style", "nav", "footer", "header", "aside",
                          "form", "noscript", "iframe", "svg", "button"]):
            tag.decompose()
        # Try to find main content block
        main = (
            soup.find("main")
            or soup.find("article")
            or soup.find(id="content")
            or soup.find(class_="content")
            or soup.find("body")
            or soup
        )
        text = main.get_text(separator="\n", strip=True)
        # Collapse excessive blank lines
        import re
        text = re.sub(r"\n{3,}", "\n\n", text)
    else:
        # Fallback: crude tag stripping
        import re
        text = re.sub(r"<[^>]+>", " ", resp.text)
        text = re.sub(r"\s+", " ", text).strip()

    if len(text) > max_chars:
        text = text[:max_chars] + f"\n\n… [truncated, {len(text)} total chars]"

    return f"URL: {url}\n\n{text}", True


def _ensure_playwright() -> bool:
    """Install playwright + chromium if not present. Returns True if available after attempt."""
    global _HAS_PLAYWRIGHT, _sync_playwright  # noqa: PLW0603
    if _HAS_PLAYWRIGHT:
        return True
    try:
        import sys
        print("[crawl_tree] Playwright not found — installing automatically…")
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "playwright", "-q"],
            check=True, capture_output=True,
        )
        subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium", "--with-deps"],
            check=True, capture_output=True,
        )
        from playwright.sync_api import sync_playwright as _sp
        _sync_playwright = _sp  # type: ignore[assignment]
        _HAS_PLAYWRIGHT = True
        print("[crawl_tree] Playwright installed successfully.")
        return True
    except Exception as e:
        print(f"[crawl_tree] Auto-install failed: {e} — falling back to requests+BS4.")
        return False


def _crawl_tree(
    root_url: str,
    link_selector: str = "a",
    url_pattern: str = "",
    max_depth: int = 3,
    max_pages: int = 100,
    same_domain: bool = True,
    use_js: bool = False,
    output_path: str = "",
) -> tuple[str, bool]:
    """BFS tree crawl. Returns JSON: {url: {title, text_snippet, children, depth}}"""
    import json
    import re
    from collections import deque
    from urllib.parse import urljoin, urlparse

    url_re = re.compile(url_pattern) if url_pattern else None
    origin = urlparse(root_url).netloc
    visited: dict[str, dict] = {}
    queue: deque[tuple[str, int]] = deque([(root_url, 0)])

    _HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    }

    # Auto-install Playwright on first use; fall back to requests if it fails
    playwright_ok = _ensure_playwright()
    use_playwright = playwright_ok and (use_js or not _HAS_REQUESTS)

    if use_playwright:
        browser_ctx = _sync_playwright().__enter__()
        browser = browser_ctx.chromium.launch(headless=True)
        page = browser.new_page()
    else:
        browser_ctx = browser = page = None  # type: ignore

    def _fetch_html(url: str) -> str | None:
        try:
            if use_playwright and page is not None:
                page.goto(url, timeout=15000, wait_until="domcontentloaded")
                try:
                    page.wait_for_load_state("networkidle", timeout=5000)
                except Exception:
                    pass
                return page.content()
            else:
                if not _HAS_REQUESTS:
                    return None
                r = _requests.get(url, headers=_HEADERS, timeout=15, allow_redirects=True)
                r.raise_for_status()
                return r.text
        except Exception:
            return None

    def _extract(html: str, base_url: str) -> tuple[str, str, list[str]]:
        """Returns (title, text_snippet, child_urls)."""
        if not _HAS_BS4:
            import re as _re
            title = (_re.search(r"<title[^>]*>(.*?)</title>", html, _re.I | _re.S) or ["", ""])[1]
            text = _re.sub(r"<[^>]+>", " ", html)
            text = " ".join(text.split())[:300]
            hrefs = _re.findall(r'href=["\']([^"\']+)["\']', html)
            return str(title), text, [urljoin(base_url, h) for h in hrefs]

        soup = _BS4(html, "html.parser")
        title = soup.title.get_text(strip=True) if soup.title else base_url
        for tag in soup(["script", "style", "nav", "footer", "header", "aside", "noscript"]):
            tag.decompose()
        body = soup.find("main") or soup.find("article") or soup.find("body") or soup
        snippet = " ".join(body.get_text(separator=" ", strip=True).split())[:300]
        links = []
        for a in soup.select(link_selector):
            href = a.get("href", "")
            if not href or href.startswith(("#", "mailto:", "javascript:", "tel:")):
                continue
            links.append(urljoin(base_url, href).split("#")[0])
        return title, snippet, links

    try:
        while queue and len(visited) < max_pages:
            url, depth = queue.popleft()
            if url in visited:
                continue

            html = _fetch_html(url)
            if html is None:
                visited[url] = {"title": "FETCH_ERROR", "text_snippet": "", "children": [], "depth": depth}
                continue

            title, snippet, raw_links = _extract(html, url)

            children: list[str] = []
            if depth < max_depth:
                seen_this_page: set[str] = set()
                for child in raw_links:
                    if child in visited or child in seen_this_page:
                        continue
                    if same_domain and urlparse(child).netloc != origin:
                        continue
                    if url_re and not url_re.search(child):
                        continue
                    seen_this_page.add(child)
                    children.append(child)
                    queue.append((child, depth + 1))

            visited[url] = {
                "title": title,
                "text_snippet": snippet,
                "children": children,
                "depth": depth,
            }
    finally:
        if use_playwright and browser_ctx is not None:
            try:
                browser.close()  # type: ignore
                browser_ctx.__exit__(None, None, None)
            except Exception:
                pass

    result_json = json.dumps(visited, indent=2, ensure_ascii=False)

    if output_path:
        try:
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            Path(output_path).write_text(result_json, encoding="utf-8")
            saved_msg = f"\n\nSaved to: {output_path}"
        except Exception as e:
            saved_msg = f"\n\nFailed to save: {e}"
    else:
        saved_msg = ""

    engine = "Playwright" if use_playwright else "requests+BS4"
    summary = (
        f"Crawled {len(visited)} pages from {root_url} "
        f"(depth≤{max_depth}, engine={engine}){saved_msg}\n\n"
        + result_json
    )
    return summary, True
