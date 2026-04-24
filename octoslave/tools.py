import json
import os
import subprocess
import glob as glob_module
from pathlib import Path

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
                "Make a targeted edit by replacing an exact unique string with a new string. "
                "old_string must appear exactly once in the file."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path to edit"},
                    "old_string": {"type": "string", "description": "Exact string to replace (must be unique in file)"},
                    "new_string": {"type": "string", "description": "Replacement string"},
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
                "Use for running tests, installing packages, building, git operations, etc."
                "Always use timeout on every bash command to avoid getting stuck"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to run"},
                    "timeout": {"type": "integer", "description": "Timeout in seconds (default 3600). For ML model training that may take hours pass a high value, e.g. 28800 (8 h) or 86400 (24 h). There is no hard cap — size it to the expected job duration."},
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
]


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
        else:
            return f"Unknown tool: {name}", False
    except TypeError as e:
        return f"Invalid arguments for {name}: {e}", False
    except Exception as e:
        return f"Tool error: {e}", False


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
        import pypdf
    except ImportError:
        return "pypdf not installed. Run: pip install pypdf", False

    try:
        reader = pypdf.PdfReader(str(resolved))
    except Exception as e:
        return f"Could not open PDF: {e}", False

    parts = []
    for i, page in enumerate(reader.pages):
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        if text.strip():
            parts.append(f"--- Page {i + 1} ---\n{text.strip()}")

    if not parts:
        return "PDF contains no extractable text (may be scanned/image-based).", False

    full_text = "\n\n".join(parts)
    lines = full_text.splitlines()
    total = len(lines)

    start = (offset - 1) if offset and offset > 0 else 0
    end = (start + limit) if limit else total
    selected = lines[start:end]

    header = f"PDF: {resolved.name} ({len(reader.pages)} pages, {total} lines extracted)\n\n"
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
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(content)
    lines = content.count("\n") + 1
    return f"Written {lines} lines to {path}", True


def _edit_file(path: str, old_string: str, new_string: str, working_dir: str) -> tuple[str, bool]:
    resolved = _resolve(path, working_dir)
    if not resolved.exists():
        return f"File not found: {path}", False

    content = resolved.read_text(errors="replace")
    count = content.count(old_string)

    if count == 0:
        return f"String not found in {path}:\n{old_string[:200]}", False
    if count > 1:
        return (
            f"old_string appears {count} times in {path} — make it more specific to ensure uniqueness.",
            False,
        )

    new_content = content.replace(old_string, new_string, 1)
    resolved.write_text(new_content)
    return f"Edited {path}", True


def _bash(command: str, working_dir: str, timeout: int = 3600) -> tuple[str, bool]:
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
        output = ""
        if result.stdout:
            output += result.stdout
        if result.stderr:
            output += result.stderr
        if not output:
            output = f"(exit code {result.returncode})"
        # Truncate very long outputs — keep tail-heavy since errors appear at the end
        if len(output) > 8000:
            output = output[:2000] + "\n\n... [output truncated] ...\n\n" + output[-5000:]
        return output, result.returncode == 0
    except subprocess.TimeoutExpired:
        return f"Command timed out after {timeout}s", False


def _glob(pattern: str, working_dir: str, path: str = None) -> tuple[str, bool]:
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
        with _DDGS() as ddgs:
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
        resp = _requests.get(url, headers=headers, timeout=30, allow_redirects=True)
        resp.raise_for_status()
    except Exception as e:
        return f"Fetch failed: {e}", False

    content_type = resp.headers.get("content-type", "")

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
