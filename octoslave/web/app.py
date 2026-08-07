"""
OctoSlave web UI — FastAPI backend.

Architecture:
- WebSocket /ws handles all communication (chat, research, cancel, config)
- Agent/research loops run in daemon threads
- Structured events flow back via asyncio.Queue (thread-safe bridge)
- File serving allows viewing HTML reports, plots, and markdown outputs
"""

from __future__ import annotations

import asyncio
import json
import threading
import uuid
from collections import deque
from datetime import datetime
from pathlib import Path
from queue import Empty, Queue
from typing import Any

from fastapi import FastAPI, File, Form, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from .. import display
from .. import interrupt
from ..display import resolve_permission, resolve_user_response
from ..agent import (
    continue_agent, make_client, run_agent, list_prompt_profiles,
    save_session_memory,
)
from ..council import (
    resolve_council_roles, run_council_agent, continue_council_agent, council_available,
)
from ..parallel import run_parallel_agents
from ..config import (
    load_config,
    resolve_backend, list_providers,
    get_custom_provider,
    add_custom_provider, update_custom_provider, remove_custom_provider,
    get_mcp_servers, add_mcp_server, remove_mcp_server, set_mcp_server_enabled,
    get_remotes, get_remote, add_remote, remove_remote,
)

# ---------------------------------------------------------------------------

STATIC_DIR = Path(__file__).parent / "static"
CHATS_DIR  = Path.home() / ".octoslave" / "chats"
SHARED_DIR = Path.home() / ".octoslave" / "shared"

app = FastAPI(title="OctoSlave Web UI", docs_url=None, redoc_url=None)

class _NoCacheAssets(BaseHTTPMiddleware):
    """Force browsers to revalidate JS/CSS/HTML instead of serving stale copies.

    ES-module imports (import './components.js') don't carry the ?v= cache-buster
    on the entry point, and CSS edits can ship without a version bump — so without
    this, an edit can be invisible until a hard refresh. Crucially, the SPA
    ``index.html`` (served for /lab and /science) references hashed asset files; if
    a browser caches a stale index after a rebuild it points at deleted asset
    hashes → a half-broken app (e.g. outputs never render). No-caching HTML too
    makes rebuilds always take effect. ``no-cache`` still allows 304s via
    StaticFiles' ETag/Last-Modified, so it's cheap.
    """
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        path = request.url.path
        ctype = response.headers.get("content-type", "")
        if (path.endswith('.js') or path.endswith('.css') or path.endswith('.html')
                or ctype.startswith("text/html")):
            response.headers['Cache-Control'] = 'no-cache'
        return response

app.add_middleware(_NoCacheAssets)

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# New Lab UI (Vite + React build). Served at /lab; index.html auto-served at the
# mount root via html=True. Built assets reference /lab/assets/* (vite base).
LAB_STATIC_DIR = Path(__file__).parent / "lab_static"
if LAB_STATIC_DIR.exists():
    app.mount("/lab", StaticFiles(directory=str(LAB_STATIC_DIR), html=True), name="lab")
    # The Science tab is the same React bundle (built assets live under /lab/);
    # index.html here just picks the Science view by pathname. Serving it at
    # /science needs no separate build.
    app.mount("/science", StaticFiles(directory=str(LAB_STATIC_DIR), html=True), name="science")

_ALLOWED_EXT = {
    ".html", ".htm", ".md", ".txt", ".json", ".csv",
    ".png", ".jpg", ".jpeg", ".svg", ".gif", ".py", ".sh",
}

# ---------------------------------------------------------------------------
# MCP helpers (shared by the websocket handlers)
# ---------------------------------------------------------------------------

def _reconnect_mcp() -> None:
    """Force-reconnect all configured MCP servers. Runs in a worker thread
    (subprocess launch + handshake can block, and npx may download on first run)."""
    from ..mcp_client import manager
    manager.init_from_config(load_config(), force=True)


def _mcp_snapshot() -> dict:
    """Merge configured servers with live connection status for the UI."""
    from ..mcp_client import manager
    live = {s["name"]: s for s in manager.status()}
    servers = []
    for s in get_mcp_servers():
        nm = s["name"]
        st = live.get(nm)
        servers.append({
            "name": nm,
            "enabled": s.get("enabled", True),
            "transport": "http" if s.get("url") else "stdio",
            "target": s.get("url") or " ".join([s.get("command", ""), *s.get("args", [])]).strip(),
            "connected": bool(st and st["connected"]),
            "error": (st or {}).get("error"),
            "tool_count": (st or {}).get("tool_count", 0),
            "tools": (st or {}).get("tools", []),
        })
    return {"type": "mcp_servers", "servers": servers}


# ---------------------------------------------------------------------------
# Static routes
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Chat persistence helpers
# ---------------------------------------------------------------------------

def _chat_title(messages: list) -> str:
    for m in messages:
        if m.get("role") == "user" and m.get("content"):
            return m["content"][:80]
    return "Untitled"

def _save_chat(messages: list, model: str = "", chat_id: str = "",
               working_dir: str = "", remote_id: str | None = None) -> str:
    CHATS_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now().isoformat(timespec="seconds")
    if chat_id and _safe_chat_id(chat_id):
        existing = CHATS_DIR / f"{chat_id}.json"
        try:
            existing_data = json.loads(existing.read_text())
            created_at = existing_data.get("created_at", now)
        except Exception:
            created_at = now
    else:
        chat_id = f"chat_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        created_at = now
    data = {
        "id": chat_id,
        "title": _chat_title(messages),
        "model": model,
        # Persist the execution context so reopening the chat restores where it
        # ran — the working directory and, for a remote (SSH) session, which host
        # so it reconnects to the same directory instead of falling back to local.
        "working_dir": working_dir or ".",
        "remote_id": remote_id,
        "created_at": created_at,
        "updated_at": now,
        "messages": messages,
    }
    (CHATS_DIR / f"{chat_id}.json").write_text(json.dumps(data, indent=2))
    return chat_id

def _safe_chat_id(chat_id: str) -> bool:
    return chat_id.startswith("chat_") and "/" not in chat_id and ".." not in chat_id


def _science_history(messages: list) -> list[dict]:
    """Flatten an orchestrator message list into chat bubbles for rehydration
    (user + assistant text only; system/tool/tool-call turns are dropped)."""
    out = []
    for m in messages or []:
        role = m.get("role")
        content = m.get("content")
        if role not in ("user", "assistant") or not isinstance(content, str):
            continue
        if not content.strip():
            continue
        out.append({"role": role, "text": content.strip()})
    return out

# ---------------------------------------------------------------------------
# Chat REST endpoints
# ---------------------------------------------------------------------------

@app.get("/api/profiles")
async def list_profiles():
    return {"profiles": list_prompt_profiles()}


@app.get("/api/chats")
async def list_chats():
    CHATS_DIR.mkdir(parents=True, exist_ok=True)
    chats = []
    for f in sorted(CHATS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            data = json.loads(f.read_text())
            chats.append({
                "id":            data["id"],
                "title":         data.get("title", "Untitled"),
                "model":         data.get("model", ""),
                "created_at":    data.get("created_at", ""),
                "updated_at":    data.get("updated_at", ""),
                "message_count": sum(1 for m in data.get("messages", [])
                                     if m.get("role") in ("user", "assistant")),
            })
        except Exception:
            pass
    return {"chats": chats}

@app.delete("/api/chats/{chat_id}")
async def delete_chat(chat_id: str):
    if not _safe_chat_id(chat_id):
        return {"error": "Invalid chat id"}
    f = CHATS_DIR / f"{chat_id}.json"
    if f.exists():
        f.unlink()
    return {"deleted": chat_id}


@app.post("/api/share")
async def share_chat(payload: dict):
    """Persist a conversation as a read-only shared snapshot. Returns {id, url}."""
    SHARED_DIR.mkdir(parents=True, exist_ok=True)
    messages = payload.get("messages") or []
    title = (payload.get("title") or "OctoSlave conversation").strip()[:120]
    model = payload.get("model", "")
    if not messages:
        return {"error": "no messages"}
    sid = uuid.uuid4().hex[:12]
    (SHARED_DIR / f"{sid}.json").write_text(json.dumps({
        "id": sid,
        "title": title,
        "model": model,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "messages": messages,
    }, indent=2))
    return {"id": sid, "url": f"/shared/{sid}"}


@app.get("/shared/{share_id}", response_class=HTMLResponse)
async def view_shared(share_id: str):
    """Render a shared conversation as a read-only HTML page."""
    if not share_id.isalnum() or len(share_id) > 24:
        return HTMLResponse("<p>Invalid share id.</p>", status_code=400)
    f = SHARED_DIR / f"{share_id}.json"
    if not f.exists():
        return HTMLResponse("<p>Shared conversation not found.</p>", status_code=404)
    try:
        data = json.loads(f.read_text())
    except Exception:
        return HTMLResponse("<p>Failed to load.</p>", status_code=500)

    def _esc(s: str) -> str:
        return (s.replace("&", "&amp;").replace("<", "&lt;")
                 .replace(">", "&gt;").replace('"', "&quot;"))

    parts = []
    for m in data.get("messages", []):
        role = m.get("role", "")
        content = (m.get("content") or "").strip()
        if not content or role == "system":
            continue
        klass = "u" if role == "user" else "a"
        parts.append(f'<div class="msg {klass}"><pre>{_esc(content)}</pre></div>')

    html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{_esc(data.get('title', 'Shared'))} — OctoSlave</title>
<style>
body{{font-family:Inter,system-ui,sans-serif;background:#111216;color:#d0d1d6;
     margin:0;padding:32px 16px;line-height:1.55}}
.wrap{{max-width:780px;margin:0 auto}}
h1{{font-size:18px;margin:0 0 4px;color:#fab283}}
.meta{{color:#7a7d86;font-size:12px;margin-bottom:24px}}
.msg{{margin:14px 0;padding:14px 16px;border-radius:12px;border:1px solid #2a2b35}}
.msg.u{{background:#1a1c25;border-color:#363846}}
.msg.a{{background:#16171d}}
.msg.u::before{{content:"You";display:block;font-size:11px;color:#5c9cf5;
                font-weight:600;margin-bottom:6px;letter-spacing:.04em}}
.msg.a::before{{content:"OctoSlave";display:block;font-size:11px;color:#fab283;
                font-weight:600;margin-bottom:6px;letter-spacing:.04em}}
pre{{margin:0;white-space:pre-wrap;word-break:break-word;font-family:inherit}}
.foot{{color:#4a4d56;font-size:11px;margin-top:32px;text-align:center}}
</style></head><body>
<div class="wrap">
<h1>{_esc(data.get('title', 'Shared conversation'))}</h1>
<div class="meta">model: {_esc(data.get('model', '—'))} · {_esc(data.get('created_at', ''))}</div>
{''.join(parts) or '<p>(empty)</p>'}
<div class="foot">Shared via OctoSlave · read-only snapshot</div>
</div></body></html>"""
    return HTMLResponse(html)


@app.get("/api/picker")
async def file_picker(working_dir: str = ".", q: str = ""):
    """Return up to 60 files matching ``q`` under ``working_dir`` for @ autocomplete."""
    root = Path(working_dir).expanduser().resolve()
    if not root.is_dir():
        return {"items": []}
    SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__",
                 ".parallel", ".uploads", ".pytest_cache", "dist", "build"}
    needle = q.lower().strip()
    out = []
    try:
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            if any(part in SKIP_DIRS or part.startswith(".") and len(part) > 1
                   for part in p.parts[len(root.parts):]):
                continue
            rel = str(p.relative_to(root))
            if needle and needle not in rel.lower():
                continue
            out.append(rel)
            if len(out) >= 60:
                break
    except Exception:
        pass
    out.sort(key=lambda s: (s.count("/"), len(s), s))
    return {"items": out[:60]}

# ---------------------------------------------------------------------------

@app.get("/")
async def serve_index():
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.post("/api/upload")
async def upload_file(
    file: UploadFile = File(...),
    working_dir: str = Form("."),
):
    """Save an uploaded file into <working_dir>/.uploads/ and return its path."""
    upload_dir = Path(working_dir) / ".uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)

    filename = Path(file.filename).name  # strip any client-side path components
    dest = upload_dir / filename
    # avoid clobbering existing files
    if dest.exists():
        stem, suffix, i = dest.stem, dest.suffix, 1
        while dest.exists():
            dest = upload_dir / f"{stem}_{i}{suffix}"
            i += 1

    content = await file.read()
    dest.write_bytes(content)
    return {"path": str(dest.resolve()), "name": filename, "size": len(content)}



@app.get("/api/pick-dir")
async def pick_directory():
    """Open a native OS directory-picker dialog and return the selected path."""
    import asyncio
    import concurrent.futures
    import platform
    import subprocess

    def _open_dialog() -> str:
        system = platform.system()
        try:
            if system == "Darwin":
                result = subprocess.run(
                    ["osascript", "-e",
                     'POSIX path of (choose folder with prompt "Select working directory")'],
                    capture_output=True, text=True, timeout=120,
                )
                if result.returncode == 0:
                    return result.stdout.strip()
            elif system == "Linux":
                for cmd in (
                    ["zenity", "--file-selection", "--directory",
                     "--title=Select working directory"],
                    ["kdialog", "--getexistingdirectory", "--title",
                     "Select working directory"],
                ):
                    try:
                        result = subprocess.run(
                            cmd, capture_output=True, text=True, timeout=120)
                        if result.returncode == 0:
                            return result.stdout.strip()
                    except FileNotFoundError:
                        continue
        except Exception:
            pass
        return ""

    loop = asyncio.get_running_loop()
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        path = await loop.run_in_executor(pool, _open_dialog)
    return {"path": path}


@app.get("/api/remotes")
async def list_remotes():
    """Return configured remote (SSH) targets (secrets are just file paths)."""
    return {"remotes": get_remotes()}


# Substrings marking non-chat models (embeddings/rerankers/audio) to hide from the
# model pickers. Shared with the agent's fallback picker, so a failover can never
# land on an embedding model either.
from ..config import NON_CHAT_MODEL_HINTS as _NON_CHAT_MODEL_HINTS


def _mark_stopped(messages: list[dict]) -> list[dict]:
    """Append the user-stop notice to a message history.

    A stop leaves the transcript ending mid-action (a tool call with no result, a
    half-finished edit). Without a note, the next turn reads that as its own
    failure and often redoes work that already succeeded — so state plainly what
    happened. Any dangling tool call is repaired first, or the history won't be
    accepted back by the API.
    """
    from ..agent import repair_messages
    from ..interrupt import STOP_NOTICE
    msgs, _ = repair_messages(list(messages or []))
    while msgs and msgs[-1].get("role") == "assistant" and msgs[-1].get("tool_calls"):
        msgs.pop()
    if msgs and msgs[-1].get("role") == "user" and STOP_NOTICE in str(msgs[-1].get("content", "")):
        return msgs
    msgs.append({"role": "user", "content": STOP_NOTICE})
    return msgs


def _healthy_model(model: str, cfg: dict) -> tuple[str, str]:
    """Check ``model`` against the backend's live catalog before a run starts.

    A model id that the backend no longer serves fails deep inside the run with
    an opaque 400 ("no healthy deployments for this model") — most often a stale
    ``default_model`` in config.json. Returns ``(model_to_use, replaced_id)``,
    where ``replaced_id`` is empty when nothing was swapped. Unreachable catalog
    ⇒ no opinion: the caller's model is used unchanged.
    """
    try:
        from ..config import list_models as _list_models, pick_fallback_model
        available = [m for m in (_list_models(cfg) or []) if m]
    except Exception:
        return model, ""
    if not available or not model or model in available:
        return model, ""
    chat = [m for m in available
            if not any(h in (m or "").lower() for h in _NON_CHAT_MODEL_HINTS)]
    alt = pick_fallback_model(model, chat or available)
    return (alt, model) if alt else (model, "")


@app.get("/api/models")
async def api_models():
    """Chat models available on the active backend — for the orchestrator/pool
    selectors in Science and Lab. Falls back to the known list on any error."""
    try:
        from ..config import list_models as _list_models
        cfg = load_config()
        models = _list_models(cfg)
    except Exception:
        models = []
    chat = [m for m in models
            if not any(h in (m or "").lower() for h in _NON_CHAT_MODEL_HINTS)]
    return {"models": chat, "default": load_config().get("default_model", "")}


@app.post("/api/remotes")
async def create_remote(payload: dict):
    """Add a new remote SSH target."""
    try:
        r = add_remote(payload or {})
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "remote": r}


@app.delete("/api/remotes/{remote_id}")
async def delete_remote(remote_id: str):
    ok = remove_remote(remote_id)
    return {"ok": ok}


@app.post("/api/remotes/test")
async def test_remote(payload: dict):
    """Probe SSH reachability for an existing (id) or ad-hoc remote config."""
    import asyncio
    import concurrent.futures
    from ..remote import RemoteSession

    remote = None
    if payload.get("id"):
        remote = get_remote(None, payload["id"])
    if remote is None:
        remote = payload or {}
    if not remote.get("host"):
        return {"ok": False, "message": "host is required"}

    def _check():
        try:
            return RemoteSession.get(remote).check()
        except Exception as exc:  # pragma: no cover - defensive
            return False, str(exc)

    loop = asyncio.get_running_loop()
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        ok, message = await loop.run_in_executor(pool, _check)
    return {"ok": ok, "message": message}


@app.get("/api/remote-dirs")
async def remote_dirs(remote_id: str, path: str = ""):
    """Browse directories on a remote host (for the working-dir picker)."""
    import asyncio
    import concurrent.futures
    from ..remote import RemoteSession

    remote = get_remote(None, remote_id)
    if not remote:
        return {"ok": False, "error": f"No remote with id '{remote_id}'."}

    def _ls():
        try:
            ok, cwd, dirs, err = RemoteSession.get(remote).list_dirs(path)
            return {"ok": ok, "path": cwd, "dirs": dirs, "error": err}
        except Exception as exc:  # pragma: no cover - defensive
            return {"ok": False, "error": str(exc)}

    loop = asyncio.get_running_loop()
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return await loop.run_in_executor(pool, _ls)


@app.get("/api/files/view/{file_path:path}")
async def view_file(file_path: str):
    """Serve a file for inline viewing in the browser."""
    path = Path(file_path)
    if not path.exists() or not path.is_file():
        return HTMLResponse("<p>File not found.</p>", status_code=404)
    if path.suffix not in _ALLOWED_EXT:
        return HTMLResponse("<p>File type not allowed.</p>", status_code=403)
    media = {
        ".html": "text/html", ".htm": "text/html",
        ".md": "text/plain; charset=utf-8",
        ".txt": "text/plain; charset=utf-8",
        ".json": "application/json", ".csv": "text/csv",
        ".png": "image/png", ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg", ".svg": "image/svg+xml",
        ".gif": "image/gif",
        ".py": "text/plain; charset=utf-8",
        ".sh": "text/plain; charset=utf-8",
    }
    return FileResponse(str(path), media_type=media.get(path.suffix, "application/octet-stream"))


# ---------------------------------------------------------------------------
# Science endpoints (edit a presented output inline)
# ---------------------------------------------------------------------------

_EDITABLE_EXT = {".md", ".txt", ".csv", ".tsv", ".json", ".html", ".htm",
                 ".py", ".sh", ".yaml", ".yml", ".svg"}

# Working dirs with an in-flight Science turn (process-wide). Lets the UI show a
# "running" badge and guards against two connections driving the same session.
_ACTIVE_SCIENCE: set[str] = set()

# How many recent events a re-attaching viewer gets replayed. Enough to cover the
# tool calls and streamed text of a long turn without holding a whole run in RAM.
_HUB_BACKLOG = 600


class _ScienceHub:
    """Event fan-out for one running Science turn.

    A turn's events used to go straight to the websocket connection that STARTED
    it. Reloading the page therefore orphaned the run: the turn kept working, but
    everything it emitted went to a socket nobody was reading, and the new page
    could only sit on restored history with a locked composer.

    The hub belongs to the SESSION instead, so any connection can attach — a
    reload, a second tab, a browser reopened an hour later — and a late joiner
    gets the recent backlog replayed before the live stream continues. Publishing
    happens on the turn's worker thread, so each subscriber carries the event loop
    it belongs to and is woken through ``call_soon_threadsafe``.
    """

    def __init__(self, key: str):
        self.key = key
        self.agent_ident: int | None = None   # thread id, so ANY viewer can Stop
        self._subs: set[tuple] = set()        # {(loop, queue)}
        self._backlog: "deque[dict]" = deque(maxlen=_HUB_BACKLOG)
        self._lock = threading.Lock()
        self._closed = False

    def publish(self, event: dict) -> None:
        with self._lock:
            # The sentinel is a control signal for the drain loops, not content —
            # replaying it to a later viewer would end its stream immediately.
            if event.get("type") != "_sentinel":
                self._backlog.append(event)
            subs = list(self._subs)
        for loop, q in subs:
            try:
                loop.call_soon_threadsafe(q.put_nowait, event)
            except RuntimeError:      # that viewer's loop is gone
                with self._lock:
                    self._subs.discard((loop, q))

    def attach(self, loop, q) -> list[dict]:
        """Subscribe and return what has already happened this turn.

        A turn that ALREADY finished hands back a trailing sentinel: the live
        sentinel was published before this subscriber existed, and without it here
        the caller's drain loop would wait for an event that can never come.
        """
        with self._lock:
            self._subs.add((loop, q))
            out = list(self._backlog)
            if self._closed:
                out.append({"type": "_sentinel"})
            return out

    def detach(self, loop, q) -> None:
        with self._lock:
            self._subs.discard((loop, q))

    def close(self) -> None:
        with self._lock:
            self._closed = True
        self.publish({"type": "_sentinel"})


_SCIENCE_HUBS: dict[str, _ScienceHub] = {}
_HUBS_LOCK = threading.Lock()


def _open_hub(key: str) -> _ScienceHub:
    hub = _ScienceHub(key)
    with _HUBS_LOCK:
        _SCIENCE_HUBS[key] = hub
    return hub


def _get_hub(key: str) -> "_ScienceHub | None":
    with _HUBS_LOCK:
        return _SCIENCE_HUBS.get(key)


def _close_hub(key: str) -> None:
    with _HUBS_LOCK:
        hub = _SCIENCE_HUBS.pop(key, None)
    if hub is not None:
        hub.close()


@app.get("/api/science/sessions")
async def science_sessions():
    """List past/current Science sessions (most recent first) for the history UI."""
    from ..science import index as _index
    out = []
    for s in _index.list_sessions():
        out.append({**s, "running": s.get("working_dir") in _ACTIVE_SCIENCE})
    return {"sessions": out}


@app.delete("/api/science/sessions")
async def science_forget(working_dir: str):
    """Drop a session from the history index (does not delete its files)."""
    from ..science import index as _index
    return {"ok": _index.remove(working_dir)}


@app.post("/api/science/save")
async def science_save(payload: dict):
    """Write edited text content back to a presented artifact file on disk.

    Only text-like files are editable; images/PDFs are refined by commenting.
    """
    raw = (payload or {}).get("path", "")
    content = (payload or {}).get("content")
    if not raw or content is None:
        return {"ok": False, "error": "path and content are required"}
    path = Path(raw)
    if path.suffix.lower() not in _EDITABLE_EXT:
        return {"ok": False, "error": f"{path.suffix} is not editable in place"}
    if not path.exists() or not path.is_file():
        return {"ok": False, "error": "file not found"}
    try:
        path.write_text(content)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "path": str(path.resolve()), "size": len(content)}


# ---------------------------------------------------------------------------
# WebSocket handler
# ---------------------------------------------------------------------------


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    loop = asyncio.get_running_loop()

    # Per-connection mutable state
    state: dict[str, Any] = {
        "messages": [],       # conversation history (chat mode)
        "working_dir": ".",
        "model": None,
        "running": False,
        "remote_id": None,    # active remote (SSH) target id, or None = local
    }

    # Async queue bridged from sync threads via loop.call_soon_threadsafe
    event_q: asyncio.Queue = asyncio.Queue()

    def make_emit():
        """Return a thread-safe emit callback that feeds the async queue."""
        def emit(event: dict) -> None:
            loop.call_soon_threadsafe(event_q.put_nowait, event)
        return emit

    async def stream_events() -> None:
        """Forward agent events to WS; concurrently handle permission_response."""
        async def _drain():
            while True:
                event = await event_q.get()
                if event.get("type") == "_sentinel":
                    return
                try:
                    await websocket.send_json(event)
                except Exception:
                    return

        async def _recv_perm():
            while True:
                try:
                    raw = await websocket.receive_text()
                    msg = json.loads(raw)
                    mt = msg.get("type")
                    if mt == "permission_response":
                        resolve_permission(bool(msg.get("allow", False)))
                    elif mt == "user_response":
                        resolve_user_response(str(msg.get("answer", "")))
                    # ---- stop the running chat/council agent ----
                    elif mt in ("stop", "stop_chat"):
                        ident = state.get("agent_ident")
                        if ident and interrupt.request_stop(ident):
                            event_q.put_nowait({
                                "type": "info",
                                "text": "⏹ Stopping — killing the running command "
                                        "and ending the turn.",
                            })
                    # ---- live lab controls (handled while a lab is running) ----
                    elif mt == "inject":
                        sess = state.get("lab_session")
                        if sess is not None:
                            sess.add_injection(str(msg.get("text", "")))
                    elif mt == "set_autonomous":
                        sess = state.get("lab_session")
                        if sess is not None:
                            sess.autonomous = bool(msg.get("autonomous", True))
                            sess.touch()
                    elif mt in ("approve", "lab_continue"):
                        sess = state.get("lab_session")
                        if sess is not None:
                            from ..lab.runner import resolve_lab_approval
                            resolve_lab_approval(sess, "continue")
                    elif mt == "stop_lab":
                        sess = state.get("lab_session")
                        if sess is not None:
                            from ..lab.runner import resolve_lab_approval
                            sess.status = "stopped"
                            resolve_lab_approval(sess, "stop")
                except Exception:
                    break

        drain_task = asyncio.create_task(_drain())
        recv_task  = asyncio.create_task(_recv_perm())
        await drain_task
        state["running"] = False
        recv_task.cancel()
        try:
            await recv_task
        except asyncio.CancelledError:
            pass

    async def stream_science(hub: "_ScienceHub") -> None:
        """Forward one Science turn's events to THIS socket until the turn ends.

        Used by the connection that started the turn and by any that attach later
        (a reload, a second tab). Each viewer gets its own queue, seeded with the
        backlog, so re-attaching mid-run shows what has already happened instead
        of a frozen page. Stop is routed through the hub's recorded thread id, so
        it works from a viewer that did not start the run.
        """
        q: asyncio.Queue = asyncio.Queue()
        for ev in hub.attach(loop, q):
            q.put_nowait(ev)

        async def _drain():
            while True:
                event = await q.get()
                if event.get("type") == "_sentinel":
                    return
                try:
                    await websocket.send_json(event)
                except Exception:
                    return

        async def _recv():
            while True:
                try:
                    raw = await websocket.receive_text()
                except Exception:
                    return          # this viewer's socket is gone
                try:
                    msg = json.loads(raw)
                    mt = msg.get("type")
                    if mt == "permission_response":
                        resolve_permission(bool(msg.get("allow", False)))
                    elif mt == "user_response":
                        resolve_user_response(str(msg.get("answer", "")))
                    elif mt in ("stop", "stop_chat"):
                        ident = hub.agent_ident or state.get("agent_ident")
                        if ident and interrupt.request_stop(ident):
                            hub.publish({
                                "type": "info",
                                "text": "⏹ Stopping — killing the running command "
                                        "and ending the turn.",
                            })
                except Exception:
                    continue        # bad message — ignore it, keep watching

        drain_task = asyncio.create_task(_drain())
        recv_task = asyncio.create_task(_recv())
        try:
            # Whichever happens first: the turn ends (drain hits the sentinel) or
            # this viewer disconnects (recv returns). Waiting only on the drain
            # meant a viewer that closed mid-turn left its handler parked on an
            # empty queue until the run happened to emit again.
            await asyncio.wait({drain_task, recv_task},
                               return_when=asyncio.FIRST_COMPLETED)
        finally:
            hub.detach(loop, q)
            for t in (drain_task, recv_task):
                t.cancel()
            for t in (drain_task, recv_task):
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass

    async def send(event: dict) -> None:
        try:
            await websocket.send_json(event)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Send initial config on connect
    # ------------------------------------------------------------------
    try:
        cfg = load_config()
        await send({"type": "config", "data": {
            "model": cfg.get("default_model", ""),
            "base_url": cfg.get("base_url", ""),
            "backend": cfg.get("backend", "einfra"),
            "has_api_key": bool(cfg.get("api_key", "")),
            "has_nim_key": bool(cfg.get("nim_api_key", "")),
            "working_dir": ".",
            "prompt_profile": cfg.get("prompt_profile", "base"),
            "remotes": get_remotes(cfg),
            "remote_id": state.get("remote_id"),
        }})
    except Exception as exc:
        await send({"type": "error", "text": f"Config load error: {exc}"})

    # ------------------------------------------------------------------
    # Main receive loop
    # ------------------------------------------------------------------
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            mtype = msg.get("type", "")

            # ---- config / meta ----
            if mtype == "get_config":
                try:
                    cfg = load_config()
                    await send({"type": "config", "data": {
                        "model": cfg.get("default_model", ""),
                        "base_url": cfg.get("base_url", ""),
                        "backend": cfg.get("backend", "einfra"),
                        "has_api_key": bool(cfg.get("api_key", "")),
                        "has_nim_key": bool(cfg.get("nim_api_key", "")),
                        "working_dir": state["working_dir"],
                        "prompt_profile": cfg.get("prompt_profile", "base"),
                        "remotes": get_remotes(cfg),
                        "remote_id": state.get("remote_id"),
                    }})
                except Exception as exc:
                    await send({"type": "error", "text": str(exc)})

            elif mtype == "list_models":
                try:
                    cfg = load_config()
                    # Try to fetch model list; fall back gracefully
                    try:
                        from ..config import list_models as _list_models
                        models = _list_models(cfg)
                    except Exception:
                        models = []
                    await send({"type": "models", "list": models})
                except Exception as exc:
                    await send({"type": "models", "list": [], "error": str(exc)})

            elif mtype == "set_working_dir":
                wd = msg.get("working_dir", ".")
                state["working_dir"] = wd
                await send({"type": "ok", "working_dir": wd})

            elif mtype == "set_remote":
                # remote_id "" / null → back to local execution.
                rid = msg.get("remote_id") or None
                if rid and get_remote(None, rid) is None:
                    await send({"type": "error", "text": f"No remote with id '{rid}'."})
                else:
                    state["remote_id"] = rid
                    remote = get_remote(None, rid) if rid else None
                    # Switching to a remote → start in its home directory (or the
                    # optional configured dir). Resolved over SSH off the loop.
                    if remote and msg.get("adopt_dir", True):
                        import asyncio as _asyncio
                        import concurrent.futures as _cf
                        from ..remote import resolve_start_dir
                        _loop = _asyncio.get_running_loop()
                        with _cf.ThreadPoolExecutor(max_workers=1) as _pool:
                            state["working_dir"] = await _loop.run_in_executor(
                                _pool, resolve_start_dir, remote)
                    await send({"type": "ok", "remote_id": rid, "set_remote": True,
                                "working_dir": state["working_dir"]})

            elif mtype == "switch_backend":
                """Handle backend switching (ollama / einfra / nim / <custom-id>)."""
                try:
                    from ..config import (
                        save_config, ollama_is_running, ollama_list_models,
                        NIM_BASE_URL, NIM_DEFAULT_MODEL, DEFAULT_MODEL,
                    )
                    backend = msg.get("backend", "einfra")
                    requested_model = msg.get("model")

                    cfg = load_config()

                    if backend == "ollama":
                        # Switch to Ollama
                        ollama_url = cfg.get("ollama_url", "http://localhost:11434/v1")
                        if not ollama_is_running(ollama_url):
                            await send({"type": "error", "text": "Ollama is not running. Start it with: ollama serve"})
                        else:
                            pulled = ollama_list_models(ollama_url)
                            if not pulled:
                                await send({"type": "error", "text": "No models pulled yet. Use /pull <model> first."})
                            else:
                                chosen = requested_model if requested_model and requested_model in pulled else pulled[0]
                                save_config(
                                    cfg.get("api_key", ""),
                                    cfg.get("base_url", ""),
                                    chosen,
                                    backend="ollama",
                                    ollama_url=ollama_url,
                                )
                                state["backend"] = "ollama"
                                state["model"] = chosen
                                await send({"type": "config_updated", "backend": "ollama", "model": chosen})
                                await send({"type": "info", "text": f"Switched to local mode with {chosen}"})
                    elif backend == "nim":
                        # Switch to NVIDIA NIM
                        nim_api_key = cfg.get("nim_api_key", "")
                        if not nim_api_key:
                            await send({"type": "error", "text": "No NVIDIA NIM API key configured. Run 'ots config' first."})
                        else:
                            nim_url = cfg.get("nim_url", NIM_BASE_URL)
                            chosen = requested_model or NIM_DEFAULT_MODEL
                            save_config(
                                cfg.get("api_key", ""),
                                cfg.get("base_url", ""),
                                chosen,
                                backend="nim",
                                ollama_url=cfg.get("ollama_url", ""),
                                nim_api_key=nim_api_key,
                                nim_url=nim_url,
                            )
                            state["backend"] = "nim"
                            state["model"] = chosen
                            await send({"type": "config_updated", "backend": "nim", "model": chosen})
                            await send({"type": "info", "text": f"Switched to NVIDIA NIM with {chosen}"})
                    elif backend == "einfra":
                        # Switch to e-INFRA CZ
                        api_key = cfg.get("api_key", "")
                        if not api_key:
                            await send({"type": "error", "text": "No e-INFRA CZ API key configured. Run 'ots config' first."})
                        else:
                            save_config(
                                api_key,
                                cfg.get("base_url", ""),
                                DEFAULT_MODEL,
                                backend="einfra",
                                ollama_url=cfg.get("ollama_url", ""),
                            )
                            state["backend"] = "einfra"
                            state["model"] = DEFAULT_MODEL
                            await send({"type": "config_updated", "backend": "einfra", "model": DEFAULT_MODEL})
                            await send({"type": "info", "text": f"Switched to e-INFRA CZ mode with {DEFAULT_MODEL}"})
                    else:
                        # Custom user-defined provider
                        provider = get_custom_provider(cfg, backend)
                        if not provider:
                            await send({"type": "error", "text": f"Unknown provider '{backend}'."})
                        elif not provider.get("base_url"):
                            await send({"type": "error", "text": f"Provider '{provider.get('name')}' has no base_url configured."})
                        else:
                            chosen = requested_model or provider.get("default_model") or ""
                            save_config(
                                cfg.get("api_key", ""),
                                cfg.get("base_url", ""),
                                chosen or cfg.get("default_model", DEFAULT_MODEL),
                                backend=backend,
                                ollama_url=cfg.get("ollama_url", ""),
                                nim_api_key=cfg.get("nim_api_key", ""),
                                nim_url=cfg.get("nim_url", ""),
                            )
                            state["backend"] = backend
                            state["model"] = chosen
                            await send({"type": "config_updated", "backend": backend, "model": chosen, "name": provider.get("name", backend)})
                            await send({"type": "info", "text": f"Switched to {provider.get('name', backend)}" + (f" with {chosen}" if chosen else "")})
                except Exception as exc:
                    await send({"type": "error", "text": f"Backend switch failed: {exc}"})

            elif mtype == "list_providers":
                try:
                    cfg = load_config()
                    await send({
                        "type": "providers",
                        "providers": list_providers(cfg),
                        "active": cfg.get("backend", "einfra"),
                    })
                except Exception as exc:
                    await send({"type": "error", "text": f"List providers failed: {exc}"})

            elif mtype == "add_provider":
                try:
                    p = add_custom_provider(msg.get("provider") or {})
                    cfg = load_config()
                    await send({
                        "type": "providers",
                        "providers": list_providers(cfg),
                        "active": cfg.get("backend", "einfra"),
                        "added": p["id"],
                    })
                    await send({"type": "info", "text": f"✓ Provider '{p['name']}' added"})
                except ValueError as exc:
                    await send({"type": "error", "text": str(exc)})
                except Exception as exc:
                    await send({"type": "error", "text": f"Add provider failed: {exc}"})

            elif mtype == "update_provider":
                try:
                    pid = msg.get("id") or ""
                    fields = msg.get("provider") or {}
                    p = update_custom_provider(pid, fields)
                    cfg = load_config()
                    await send({
                        "type": "providers",
                        "providers": list_providers(cfg),
                        "active": cfg.get("backend", "einfra"),
                        "updated": p["id"],
                    })
                    await send({"type": "info", "text": f"✓ Provider '{p['name']}' updated"})
                except ValueError as exc:
                    await send({"type": "error", "text": str(exc)})
                except Exception as exc:
                    await send({"type": "error", "text": f"Update provider failed: {exc}"})

            elif mtype == "remove_provider":
                try:
                    pid = msg.get("id") or ""
                    ok = remove_custom_provider(pid)
                    cfg = load_config()
                    if ok:
                        # If the active provider was removed, the config's backend
                        # was reset to einfra by remove_custom_provider; reflect
                        # that in this connection's state too.
                        if state.get("backend") == pid:
                            state["backend"] = cfg.get("backend", "einfra")
                            state["model"] = cfg.get("default_model", "")
                            await send({"type": "config_updated",
                                        "backend": state["backend"],
                                        "model": state["model"]})
                        await send({"type": "info", "text": f"Provider '{pid}' removed"})
                    else:
                        await send({"type": "error", "text": f"Provider '{pid}' not found"})
                    await send({
                        "type": "providers",
                        "providers": list_providers(cfg),
                        "active": cfg.get("backend", "einfra"),
                    })
                except Exception as exc:
                    await send({"type": "error", "text": f"Remove provider failed: {exc}"})

            elif mtype == "test_provider":
                """Probe a provider's /v1/models endpoint to verify connectivity.
                Accepts {provider: {base_url, api_key}} OR {id}."""
                try:
                    p = msg.get("provider") or {}
                    if not p and msg.get("id"):
                        cfg = load_config()
                        p = get_custom_provider(cfg, msg["id"]) or {}
                    base_url = (p.get("base_url") or "").strip().rstrip("/")
                    api_key = (p.get("api_key") or "").strip()
                    if not base_url:
                        await send({"type": "provider_test",
                                    "ok": False, "error": "base_url is required"})
                    else:
                        from ..config import einfra_list_models as _live_models
                        models = _live_models(base_url, api_key or "x")
                        await send({
                            "type": "provider_test",
                            "ok": bool(models),
                            "models": models,
                            "count": len(models),
                            "error": "" if models else "No models returned (check URL/key).",
                        })
                except Exception as exc:
                    await send({"type": "provider_test", "ok": False, "error": str(exc)})

            # ---- MCP servers (wire in external tools) ----
            elif mtype == "mcp_registry":
                try:
                    from .. import mcp_registry as _reg
                    configured = {s["name"] for s in get_mcp_servers()}
                    entries = []
                    for e in _reg.list_entries():
                        entries.append({
                            "id": e["id"],
                            "name": e["name"],
                            "category": e.get("category", "Other"),
                            "summary": e.get("summary", ""),
                            "transport": e.get("transport", "stdio"),
                            "runtime": e.get("runtime", "stdio"),
                            "runtime_available": _reg.runtime_available(e.get("runtime", "stdio")),
                            "runtime_hint": _reg.runtime_hint(e.get("runtime", "stdio")),
                            "homepage": e.get("homepage", ""),
                            "installed": e["id"] in configured,
                            "inputs": [
                                {"key": i["key"],
                                 "prompt": i.get("prompt", i["key"]),
                                 "secret": bool(i.get("secret")),
                                 "default_wd": bool(i.get("default_wd"))}
                                for i in e.get("inputs", [])
                            ],
                        })
                    await send({"type": "mcp_registry", "entries": entries})
                except Exception as exc:
                    await send({"type": "error", "text": f"MCP registry failed: {exc}"})

            elif mtype == "list_mcp":
                try:
                    await send(_mcp_snapshot())
                except Exception as exc:
                    await send({"type": "error", "text": f"List MCP failed: {exc}"})

            elif mtype == "install_mcp":
                try:
                    from .. import mcp_registry as _reg
                    entry = _reg.get_entry(msg.get("id", ""))
                    if entry is None:
                        await send({"type": "error", "text": f"No catalog entry '{msg.get('id')}'."})
                    elif not _reg.runtime_available(entry.get("runtime", "stdio")):
                        rt = entry.get("runtime")
                        await send({"type": "error",
                                    "text": f"'{entry['id']}' needs the {rt} runtime. {_reg.runtime_hint(rt)}"})
                    else:
                        wd = state.get("working_dir") or "."
                        cfg = _reg.build_config(entry, msg.get("values") or {}, wd)
                        add_mcp_server(cfg)
                        await send({"type": "info", "text": f"Installed '{cfg['name']}' — connecting…"})
                        await asyncio.to_thread(_reconnect_mcp)
                        await send(_mcp_snapshot())
                except ValueError as exc:
                    await send({"type": "error", "text": str(exc)})
                except Exception as exc:
                    await send({"type": "error", "text": f"Install failed: {exc}"})

            elif mtype == "add_mcp":
                try:
                    add_mcp_server(msg.get("server") or {})
                    await send({"type": "info", "text": "MCP server added — connecting…"})
                    await asyncio.to_thread(_reconnect_mcp)
                    await send(_mcp_snapshot())
                except ValueError as exc:
                    await send({"type": "error", "text": str(exc)})
                except Exception as exc:
                    await send({"type": "error", "text": f"Add MCP failed: {exc}"})

            elif mtype == "remove_mcp":
                try:
                    if remove_mcp_server(msg.get("name", "")):
                        await send({"type": "info", "text": f"Removed '{msg.get('name')}'."})
                        await asyncio.to_thread(_reconnect_mcp)
                    else:
                        await send({"type": "error", "text": f"No MCP server '{msg.get('name')}'."})
                    await send(_mcp_snapshot())
                except Exception as exc:
                    await send({"type": "error", "text": f"Remove MCP failed: {exc}"})

            elif mtype == "toggle_mcp":
                try:
                    enabled = bool(msg.get("enabled", True))
                    if set_mcp_server_enabled(msg.get("name", ""), enabled):
                        await asyncio.to_thread(_reconnect_mcp)
                    await send(_mcp_snapshot())
                except Exception as exc:
                    await send({"type": "error", "text": f"Toggle MCP failed: {exc}"})

            elif mtype == "reconnect_mcp":
                try:
                    await send({"type": "info", "text": "Reconnecting MCP servers…"})
                    await asyncio.to_thread(_reconnect_mcp)
                    await send(_mcp_snapshot())
                except Exception as exc:
                    await send({"type": "error", "text": f"Reconnect MCP failed: {exc}"})

            elif mtype == "pull_model":
                """Handle pulling a model from Ollama."""
                try:
                    from ..config import ollama_is_running, ollama_pull_model
                    model_name = msg.get("model", "")
                    if not model_name:
                        await send({"type": "error", "text": "Model name required."})
                        continue
                    
                    ollama_url = load_config().get("ollama_url", "http://localhost:11434/v1")
                    if not ollama_is_running(ollama_url):
                        await send({"type": "error", "text": "Ollama is not running. Start it with: ollama serve"})
                    else:
                        await send({"type": "info", "text": f"Pulling {model_name}..."})
                        ok = ollama_pull_model(model_name, ollama_url)
                        if ok:
                            await send({"type": "info", "text": f"✓ {model_name} pulled successfully. Use /local {model_name} to switch to it."})
                        else:
                            await send({"type": "error", "text": f"Failed to pull {model_name}."})
                except Exception as exc:
                    await send({"type": "error", "text": f"Pull failed: {exc}"})

            # ---- chat ----
            elif mtype in ("chat", "chat_new", "chat_continue"):
                if state["running"]:
                    await send({"type": "error", "text": "A task is already running."})
                    continue

                cfg = load_config()
                model = msg.get("model") or state["model"] or cfg.get("default_model", "")
                working_dir = msg.get("working_dir") or state["working_dir"]
                message_text = msg.get("message", "").strip()
                prompt_profile = msg.get("prompt_profile") or cfg.get("prompt_profile", "base")
                permission_mode = msg.get("permission_mode") or cfg.get("permission_mode", "autonomous")

                if not message_text:
                    continue

                new_conv = (mtype in ("chat", "chat_new")) or (not state["messages"])
                state["model"] = model
                state["working_dir"] = working_dir
                # Remote execution target: explicit per-message id wins, else the
                # session's current selection. None → local (default).
                remote_id = msg.get("remote_id") if "remote_id" in msg else state.get("remote_id")
                state["remote_id"] = remote_id
                remote = get_remote(None, remote_id) if remote_id else None
                if remote:
                    await send({"type": "info",
                                "text": f"⇅ remote: {remote.get('name')} ({remote.get('host')}:{working_dir})"})
                state["running"] = True
                if state.get("backend"):
                    cfg["backend"] = state["backend"]
                _resolved = resolve_backend(cfg)
                client = make_client(_resolved["api_key"], _resolved["base_url"])

                # Agent mode — standard | improved | ultra, opt-in per message
                # (web UI defaults to improved). Improved/Ultra run the council and
                # need a cloud pool, so they auto-fall back to the single agent on
                # Ollama. Ultra adds the multi-model debate on plan + completion.
                mode = msg.get("mode") or ("improved" if msg.get("council") else "standard")
                use_council = mode in ("improved", "ultra")
                use_ultra = mode == "ultra" or bool(msg.get("ultra", False))
                council_roles = None
                # Per-role model overrides from the UI (worker/thinker/verifier/
                # worker_alt). Empty values mean "auto" — resolve_council_roles
                # falls back to its preference chains for those roles.
                council_overrides = msg.get("council_models") or {}
                if not isinstance(council_overrides, dict):
                    council_overrides = {}
                council_overrides = {
                    k: v.strip() for k, v in council_overrides.items()
                    if k in ("worker", "thinker", "verifier", "worker_alt")
                    and isinstance(v, str) and v.strip()
                }
                if use_council and council_available(cfg):
                    try:
                        council_roles, council_notes = resolve_council_roles(client, cfg, council_overrides)
                        tag = "⚡ ultra council" if use_ultra else "🐙 council"
                        extra = ""
                        if council_roles.get("worker_alt"):
                            extra += " · escalation: " + council_roles["worker_alt"]
                        if use_ultra and council_roles.get("debate"):
                            extra += " · debate: " + ", ".join(council_roles["debate"])
                        await send({
                            "type": "info",
                            "text": (
                                tag + " — Worker: " + council_roles["worker"]
                                + " · Thinker: " + council_roles["thinker"]
                                + " · Verifier: " + council_roles["verifier"] + extra
                            ),
                        })
                    except Exception as exc:
                        council_roles = None
                        await send({"type": "info", "text": f"council unavailable ({exc}); using single agent."})
                elif use_council and not council_available(cfg):
                    await send({
                        "type": "info",
                        "text": "Improved / Ultra need a cloud pool (e-INFRA / NIM); using the single local agent.",
                    })

                def chat_fn(txt=message_text, mdl=model, wd=working_dir, new=new_conv,
                           pp=prompt_profile, pm=permission_mode, roles=council_roles, ultra=use_ultra,
                           rmt=remote):
                    display.set_event_callback(make_emit())
                    # Allow this run to be interrupted from the UI (Stop button).
                    interrupt.register()
                    state["agent_ident"] = threading.get_ident()
                    try:
                        if roles:
                            if new:
                                result = run_council_agent(txt, wd, client, roles,
                                                           prompt_profile=pp, permission_mode=pm, ultra=ultra,
                                                           remote=rmt)
                            else:
                                result = continue_council_agent(state["messages"], txt, client,
                                                                roles, wd, pm, ultra=ultra, remote=rmt)
                        elif new:
                            result = run_agent(txt, mdl, wd, client, pp, pm, remote=rmt)
                        else:
                            result = continue_agent(state["messages"], txt, mdl, wd, client, pm, remote=rmt)
                        state["messages"] = result
                        # Persist a project-scoped task-outcome record so a later
                        # chat in the same working directory recalls it (the TUI
                        # does this after each turn; the web layer previously only
                        # loaded memory, never saved it — so council mode, whose
                        # worker rarely calls `remember`, left no memory file).
                        # run_agent / run_council_agent both LOAD this on the next
                        # new task. Best-effort; never fail the run over it.
                        try:
                            if load_config().get("enable_memory", True):
                                # Pass the remote explicitly: run_agent/run_council
                                # already reset this thread's execution target in
                                # their finally, so memory must be told where the
                                # work ran to land in the same working directory.
                                save_session_memory(wd, txt, status="completed", remote=rmt)
                        except Exception:
                            pass
                    except interrupt.StopRequested:
                        # Stop requested during a phase that calls the model
                        # directly (orientation / planning), or mid-tool. Keep the
                        # history and note WHY it ends here, so the next turn
                        # continues instead of re-deriving the interrupted work.
                        state["messages"] = _mark_stopped(state.get("messages") or [])
                        loop.call_soon_threadsafe(
                            event_q.put_nowait,
                            {"type": "done", "iterations": 0, "stopped": True},
                        )
                    except Exception as exc:
                        loop.call_soon_threadsafe(
                            event_q.put_nowait, {"type": "error", "text": str(exc)}
                        )
                    finally:
                        interrupt.unregister()
                        state["agent_ident"] = None
                        display.clear_event_callback()
                        loop.call_soon_threadsafe(event_q.put_nowait, {"type": "_sentinel"})

                threading.Thread(target=chat_fn, daemon=True).start()
                await stream_events()

            elif mtype == "chat_parallel":
                if state["running"]:
                    await send({"type": "error", "text": "A task is already running."})
                    continue
                cfg = load_config()
                model = msg.get("model") or state["model"] or cfg.get("default_model", "")
                working_dir = msg.get("working_dir") or state["working_dir"]
                message_text = msg.get("message", "").strip()
                n = max(1, min(8, int(msg.get("n", 3))))
                strategy = msg.get("strategy", "best")
                if strategy not in ("best", "vote", "merge"):
                    strategy = "best"
                permission_mode = msg.get("permission_mode") or cfg.get("permission_mode", "autonomous")
                models_list = msg.get("models") or None
                profiles_list = msg.get("profiles") or None
                judge_model = msg.get("judge_model") or None
                # Accept either an array or a comma-separated string for friendliness
                if isinstance(models_list, str):
                    models_list = [s.strip() for s in models_list.split(",") if s.strip()]
                if isinstance(profiles_list, str):
                    profiles_list = [s.strip() for s in profiles_list.split(",") if s.strip()]
                if not message_text:
                    continue
                state["model"] = model
                state["working_dir"] = working_dir
                state["running"] = True
                if state.get("backend"):
                    cfg["backend"] = state["backend"]
                _resolved = resolve_backend(cfg)
                _api_key = _resolved["api_key"]
                _base_url = _resolved["base_url"]
                client = make_client(_api_key, _base_url)

                def parallel_fn(txt=message_text, mdl=model, wd=working_dir,
                                cnt=n, strat=strategy, pm=permission_mode,
                                ml=models_list, pl=profiles_list, jm=judge_model):
                    display.set_event_callback(make_emit())
                    try:
                        result = run_parallel_agents(
                            task=txt,
                            model=mdl,
                            working_dir=wd,
                            client=client,
                            n=cnt,
                            strategy=strat,
                            permission_mode=pm,
                            models=ml,
                            profiles=pl,
                            judge_model=jm,
                        )
                        loop.call_soon_threadsafe(
                            event_q.put_nowait,
                            {
                                "type": "parallel_result",
                                "winner": result.get("winner"),
                                "reason": result.get("reason", ""),
                                "strategy": strat,
                                "candidates": [
                                    {
                                        "index": c.index,
                                        "profile": c.profile,
                                        "model": c.model,
                                        "succeeded": c.succeeded,
                                        "summary": c.summary[:2000],
                                        "error": c.error,
                                        "workdir": str(c.workdir),
                                    }
                                    for c in result.get("candidates", [])
                                ],
                                "merged_text": result.get("merged_text", ""),
                            },
                        )
                        if result.get("winner") is not None:
                            winner = next(
                                (c for c in result["candidates"]
                                 if c.index == result["winner"]),
                                None,
                            )
                            if winner and winner.messages:
                                state["messages"] = winner.messages
                    except Exception as exc:
                        loop.call_soon_threadsafe(
                            event_q.put_nowait, {"type": "error", "text": str(exc)}
                        )
                    finally:
                        display.clear_event_callback()
                        loop.call_soon_threadsafe(event_q.put_nowait, {"type": "_sentinel"})

                threading.Thread(target=parallel_fn, daemon=True).start()
                await stream_events()

            elif mtype == "chat_clear":
                state["messages"] = []
                await send({"type": "cleared"})

            elif mtype == "save_chat":
                if state["messages"]:
                    existing_id = msg.get("chat_id", "")
                    chat_id = _save_chat(
                        state["messages"], state.get("model", ""), existing_id,
                        working_dir=state.get("working_dir", "."),
                        remote_id=state.get("remote_id"),
                    )
                    await send({"type": "chat_saved", "id": chat_id})
                else:
                    await send({"type": "chat_saved", "id": None})

            elif mtype == "load_chat":
                chat_id = msg.get("chat_id", "")
                if not _safe_chat_id(chat_id):
                    await send({"type": "error", "text": "Invalid chat id"})
                    continue
                f = CHATS_DIR / f"{chat_id}.json"
                if not f.exists():
                    await send({"type": "error", "text": "Chat not found"})
                    continue
                try:
                    data = json.loads(f.read_text())
                    state["messages"] = data["messages"]
                    state["model"]    = data.get("model", state.get("model", ""))

                    # Restore the execution context this chat ran in: working
                    # directory and, if it was a remote (SSH) session, the same
                    # host — so a follow-up continues where it left off instead of
                    # silently running local in the wrong directory.
                    saved_wd = data.get("working_dir") or "."
                    saved_remote = data.get("remote_id")
                    effective_remote = saved_remote
                    if saved_remote and get_remote(None, saved_remote) is None:
                        # The remote was removed since this chat was saved.
                        effective_remote = None
                        await send({
                            "type": "info",
                            "text": (
                                f"⚠ This chat ran on remote '{saved_remote}', which is no "
                                "longer configured — reopened in local mode. Re-add the "
                                "remote to continue on the same host."
                            ),
                        })
                    state["working_dir"] = saved_wd
                    state["remote_id"] = effective_remote
                    if effective_remote:
                        remote = get_remote(None, effective_remote)
                        await send({
                            "type": "info",
                            "text": (
                                f"⇅ reconnected to remote {remote.get('name')} "
                                f"({remote.get('host')}:{saved_wd})"
                            ),
                        })

                    await send({"type": "chat_loaded",
                                "id": chat_id,
                                "messages": data["messages"],
                                "model": data.get("model", ""),
                                "working_dir": saved_wd,
                                "remote_id": effective_remote})
                except Exception as exc:
                    await send({"type": "error", "text": f"Failed to load chat: {exc}"})

            # ---- lab (dynamic autonomous team) ----
            elif mtype in ("start_lab", "lab"):
                if state["running"]:
                    await send({"type": "error", "text": "A task is already running."})
                    continue

                cfg = load_config()
                task = (msg.get("task") or msg.get("topic") or "").strip()
                if not task:
                    await send({"type": "error", "text": "Task is required."})
                    continue

                working_dir = msg.get("working_dir") or state["working_dir"]
                state["working_dir"] = working_dir
                autonomous = bool(msg.get("autonomous", True))
                max_rounds = max(1, min(12, int(msg.get("rounds", 4))))
                model_all = msg.get("model_all") or None
                # Pool of models the Director may assign to specialists.
                specialist_models = [m for m in (msg.get("specialist_models") or [])
                                     if isinstance(m, str) and m.strip()]
                resume = bool(msg.get("resume", False))
                state["running"] = True

                if state.get("backend"):
                    cfg["backend"] = state["backend"]
                _resolved = resolve_backend(cfg)
                client = make_client(_resolved["api_key"], _resolved["base_url"])
                model = model_all or cfg.get("default_model")

                # Remote compute target for the whole lab (bash/file/background
                # tools run on this host). Explicit per-message id wins, else the
                # session's current selection; None → local.
                remote_id = msg.get("remote_id") if "remote_id" in msg else state.get("remote_id")
                state["remote_id"] = remote_id
                remote = get_remote(None, remote_id) if remote_id else None

                from ..lab.state import LabSession
                from ..lab.runner import run_lab
                lab_session = LabSession.load(working_dir) if resume else None
                if lab_session is None:
                    lab_session = LabSession(task=task, working_dir=working_dir,
                                             model=model or "")
                lab_session.autonomous = autonomous
                if specialist_models:
                    lab_session.specialist_models = specialist_models
                state["lab_session"] = lab_session

                def lab_fn(t=task, wd=working_dir, au=autonomous, mr=max_rounds,
                           md=model, res=resume, sess=lab_session, rmt=remote):
                    display.set_event_callback(make_emit())
                    try:
                        run_lab(task=t, working_dir=wd, client=client, model=md,
                                autonomous=au, max_rounds=mr, resume=res,
                                emit=make_emit(), session=sess, remote=rmt)
                    except Exception as exc:
                        loop.call_soon_threadsafe(
                            event_q.put_nowait, {"type": "error", "text": str(exc)}
                        )
                    finally:
                        display.clear_event_callback()
                        loop.call_soon_threadsafe(event_q.put_nowait, {"type": "_sentinel"})

                threading.Thread(target=lab_fn, daemon=True).start()
                await stream_events()
                state["lab_session"] = None

            # ---- lab follow-up (act on feedback after completion) ----
            elif mtype == "lab_followup":
                if state["running"]:
                    await send({"type": "error", "text": "A task is already running."})
                    continue
                feedback = (msg.get("text") or "").strip()
                working_dir = msg.get("working_dir") or state["working_dir"]
                if not feedback:
                    await send({"type": "error", "text": "Feedback text is required."})
                    continue
                state["working_dir"] = working_dir

                cfg = load_config()
                if state.get("backend"):
                    cfg["backend"] = state["backend"]
                _resolved = resolve_backend(cfg)
                client = make_client(_resolved["api_key"], _resolved["base_url"])
                model = cfg.get("default_model")

                remote_id = msg.get("remote_id") if "remote_id" in msg else state.get("remote_id")
                state["remote_id"] = remote_id
                remote = get_remote(None, remote_id) if remote_id else None

                from ..lab.state import LabSession
                from ..lab.runner import continue_lab
                lab_session = LabSession.load(working_dir)
                if lab_session is None:
                    await send({"type": "error", "text": "No completed lab found in this directory."})
                    continue
                state["running"] = True
                state["lab_session"] = lab_session

                def followup_fn(fb=feedback, wd=working_dir, md=model, sess=lab_session, rmt=remote):
                    display.set_event_callback(make_emit())
                    try:
                        continue_lab(working_dir=wd, client=client, feedback=fb,
                                     model=md, emit=make_emit(), session=sess, remote=rmt)
                    except Exception as exc:
                        loop.call_soon_threadsafe(
                            event_q.put_nowait, {"type": "error", "text": str(exc)})
                    finally:
                        display.clear_event_callback()
                        loop.call_soon_threadsafe(event_q.put_nowait, {"type": "_sentinel"})

                threading.Thread(target=followup_fn, daemon=True).start()
                await stream_events()
                state["lab_session"] = None

            # ---- science (conversational research orchestrator) ----
            elif mtype == "science_load":
                working_dir = msg.get("working_dir") or state["working_dir"]
                state["working_dir"] = working_dir
                from ..science.session import ScienceSession
                # Same key science_message registers under — the client may send an
                # unresolved path, and a hub lookup that misses would silently put
                # us back to the old "frozen page" behaviour.
                key = str(Path(working_dir).expanduser().resolve())
                live = _get_hub(key) if key in _ACTIVE_SCIENCE else None
                sess = ScienceSession.load(working_dir)
                if sess is None and live is None:
                    await send({"type": "science_state", "exists": False,
                                "working_dir": working_dir})
                else:
                    # A session whose FIRST turn is still running has nothing on
                    # disk yet (it saves at end of turn) — but there is a live run
                    # to watch, so this is very much an existing session.
                    if sess is not None:
                        state["science_session"] = sess
                    await send({"type": "science_state", "exists": True,
                                "working_dir": working_dir,
                                "task": sess.task if sess else "",
                                "running": key in _ACTIVE_SCIENCE,
                                "attached": live is not None,
                                "history": _science_history(sess.messages) if sess else [],
                                "snapshot": sess.snapshot() if sess else {}})
                    # A turn is in flight for this session — attach to it. Without
                    # this the page just sat on restored history while the run kept
                    # going somewhere it could no longer be seen. The backlog is
                    # replayed first, so re-attaching mid-run shows what was missed.
                    if live is not None:
                        await send({"type": "info",
                                    "text": "▶ This session has a turn in progress — "
                                            "reconnected to it."})
                        state["running"] = True
                        try:
                            await stream_science(live)
                        finally:
                            state["running"] = False
                        await send({"type": "science_done"})

            elif mtype in ("science_message", "science_comment"):
                # The UI marks itself busy the moment it sends a turn, and only a
                # "science_done" clears that. So EVERY path out of this branch —
                # including the rejections below, which never start a turn — has
                # to send one, or the tab stays stuck in a running state.
                if state["running"]:
                    await send({"type": "error", "text": "A task is already running."})
                    await send({"type": "science_done"})
                    continue

                working_dir = msg.get("working_dir") or state["working_dir"]
                state["working_dir"] = working_dir

                cfg = load_config()
                if state.get("backend"):
                    cfg["backend"] = state["backend"]
                _resolved = resolve_backend(cfg)
                client = make_client(_resolved["api_key"], _resolved["base_url"])
                from ..science.session import ScienceSession
                from ..science.orchestrator import run_science_turn

                sess = state.get("science_session")
                want = str(Path(working_dir).expanduser().resolve())
                if want in _ACTIVE_SCIENCE:
                    await send({"type": "info",
                                "text": "This session is still working — wait for the "
                                        "current turn to finish before sending more."})
                    await send({"type": "science_done"})
                    continue
                if sess is None or sess.working_dir != want:
                    sess = ScienceSession.load(working_dir)

                # Model resolution, most specific first: what this message asked
                # for → what this SESSION was started with → the socket's model →
                # config's default. The session tier matters: a refine comment and
                # a reload both arrive without a model, and before this they fell
                # through to default_model — silently moving the session onto a
                # different (possibly dead) model than the one the user picked.
                model = (msg.get("model") or getattr(sess, "model", "")
                         or state.get("model") or cfg.get("default_model"))
                # Pool of models the orchestrator may assign to spawned specialists.
                specialist_models = [m for m in (msg.get("specialist_models") or [])
                                     if isinstance(m, str) and m.strip()]
                if not specialist_models:
                    specialist_models = (list(getattr(sess, "specialist_models", []) or [])
                                         or state.get("specialist_models") or [])
                state["specialist_models"] = specialist_models

                model, _swapped = _healthy_model(model, cfg)
                if _swapped:
                    await send({"type": "info", "text": (
                        f"'{_swapped}' isn't available on this backend — running on "
                        f"{model} instead.")})
                state["model"] = model

                remote_id = msg.get("remote_id") if "remote_id" in msg else state.get("remote_id")
                state["remote_id"] = remote_id
                remote = get_remote(None, remote_id) if remote_id else None

                # Build the turn's user message (a comment refines a specific output).
                refine_id = None
                if mtype == "science_comment":
                    if sess is None:
                        await send({"type": "error", "text": "No science session for this directory."})
                        await send({"type": "science_done"})
                        continue
                    comment = (msg.get("text") or "").strip()
                    art = sess.get_artifact(msg.get("artifact_id", ""))
                    if not comment:
                        await send({"type": "science_done"})
                        continue
                    if art:
                        refine_id = art.id
                        sess.comment_artifact(art.id, comment)
                        user_message = (
                            f"The user commented on the presented output `{art.rel}` "
                            f"(kind: {art.kind}): \"{comment}\". Refine that specific "
                            f"output accordingly, then call present_output on the "
                            f"updated file so they see the new version.")
                    else:
                        user_message = comment
                    echo = comment
                else:
                    user_message = (msg.get("message") or "").strip()
                    if not user_message:
                        await send({"type": "science_done"})
                        continue
                    if sess is None:
                        sess = ScienceSession(task=user_message,
                                              working_dir=working_dir,
                                              model=model or "")
                    elif not sess.task:
                        sess.task = user_message
                    echo = user_message

                sess.remote_id = remote_id
                # Remember the models this session runs on, so a refine comment, a
                # reload, or reopening it later all stay on the user's choice.
                sess.model = model or sess.model
                sess.specialist_models = specialist_models
                state["science_session"] = sess
                state["running"] = True
                _ACTIVE_SCIENCE.add(want)
                # Events go to the SESSION's hub, not to this socket, so a reload
                # or a second tab can attach to the turn instead of losing it.
                hub = _open_hub(want)
                # Record in the history index up front, so the session is
                # reopenable even mid-run (before the first turn saves).
                try:
                    from ..science import index as _science_index
                    _science_index.record(sess.working_dir, sess.task or echo)
                except Exception:
                    pass
                # Into the backlog too, so a viewer attaching later sees the
                # message that started the turn.
                hub.publish({"type": "science_user", "text": echo,
                             "artifact_id": msg.get("artifact_id")})

                def science_fn(um=user_message, sc=sess, cl=client, md=model,
                               rmt=remote, wd_key=want, rid=refine_id,
                               pool=specialist_models):
                    display.set_event_callback(hub.publish)
                    interrupt.register()
                    state["agent_ident"] = threading.get_ident()
                    # On the hub as well: a viewer that attached later must be able
                    # to Stop the run it is watching.
                    hub.agent_ident = threading.get_ident()
                    try:
                        run_science_turn(sc, um, cl, md,
                                         permission_mode="autonomous",
                                         emit=hub.publish, remote=rmt,
                                         refresh_artifact_id=rid,
                                         specialist_models=pool)
                    except interrupt.StopRequested:
                        # Record the stop in the session's own history, so the next
                        # turn (and any later session) knows the work was cut short
                        # by the user rather than finished or failed.
                        try:
                            sc.messages = _mark_stopped(sc.messages)
                            sc.save()
                        except Exception:
                            pass
                        hub.publish(
                            {"type": "science_reply",
                             "text": "⏹ Stopped by you. Any running command was killed; "
                                     "the work so far is saved. Send a message to carry on.",
                             "stopped": True})
                    except Exception as exc:
                        hub.publish({"type": "error", "text": str(exc)})
                    finally:
                        _ACTIVE_SCIENCE.discard(wd_key)
                        interrupt.unregister()
                        state["agent_ident"] = None
                        display.clear_event_callback()
                        # Ends the stream for EVERY attached viewer, not just the
                        # one that started the turn.
                        _close_hub(wd_key)

                threading.Thread(target=science_fn, daemon=True).start()
                try:
                    await stream_science(hub)
                finally:
                    state["running"] = False
                # Authoritative end-of-turn: the turn thread has finished (the
                # sentinel drained). Recoverable mid-run errors (a model that
                # stopped responding and got swapped for a fallback, a failed
                # tool) must NOT be what puts the tab back to idle.
                await send({"type": "science_done"})

    except WebSocketDisconnect:
        pass
    except Exception as exc:
        try:
            await websocket.send_json({"type": "error", "text": f"Server error: {exc}"})
        except Exception:
            pass
