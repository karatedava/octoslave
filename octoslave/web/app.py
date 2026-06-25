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
from datetime import datetime
from pathlib import Path
from queue import Empty, Queue
from typing import Any

from fastapi import FastAPI, File, Form, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from .. import display
from ..display import resolve_permission, resolve_user_response
from ..agent import continue_agent, make_client, run_agent, list_prompt_profiles
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
)

# ---------------------------------------------------------------------------

STATIC_DIR = Path(__file__).parent / "static"
CHATS_DIR  = Path.home() / ".octoslave" / "chats"
SHARED_DIR = Path.home() / ".octoslave" / "shared"

app = FastAPI(title="OctoSlave Web UI", docs_url=None, redoc_url=None)

class _NoCacheJS(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        if request.url.path.endswith('.js'):
            response.headers['Cache-Control'] = 'no-cache'
        return response

app.add_middleware(_NoCacheJS)

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# New Lab UI (Vite + React build). Served at /lab; index.html auto-served at the
# mount root via html=True. Built assets reference /lab/assets/* (vite base).
LAB_STATIC_DIR = Path(__file__).parent / "lab_static"
if LAB_STATIC_DIR.exists():
    app.mount("/lab", StaticFiles(directory=str(LAB_STATIC_DIR), html=True), name="lab")

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

def _save_chat(messages: list, model: str = "", chat_id: str = "") -> str:
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
        "created_at": created_at,
        "updated_at": now,
        "messages": messages,
    }
    (CHATS_DIR / f"{chat_id}.json").write_text(json.dumps(data, indent=2))
    return chat_id

def _safe_chat_id(chat_id: str) -> bool:
    return chat_id.startswith("chat_") and "/" not in chat_id and ".." not in chat_id

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
                state["running"] = True
                if state.get("backend"):
                    cfg["backend"] = state["backend"]
                _resolved = resolve_backend(cfg)
                client = make_client(_resolved["api_key"], _resolved["base_url"])

                # Improved (council) mode — opt-in per message; the web
                # UI ships it ON by default. Needs a cloud pool, so it auto-falls
                # back to the normal single agent on the Ollama backend.
                use_council = bool(msg.get("council", False))
                council_roles = None
                if use_council and council_available(cfg):
                    try:
                        council_roles, council_notes = resolve_council_roles(client, cfg, {})
                        await send({
                            "type": "info",
                            "text": (
                                "🐙 council — Worker: " + council_roles["worker"]
                                + " · Thinker: " + council_roles["thinker"]
                                + " · Verifier: " + council_roles["verifier"]
                            ),
                        })
                    except Exception as exc:
                        council_roles = None
                        await send({"type": "info", "text": f"council unavailable ({exc}); using single agent."})
                elif use_council and not council_available(cfg):
                    await send({
                        "type": "info",
                        "text": "Improved mode needs a cloud pool (e-INFRA / NIM); using the single local agent.",
                    })

                def chat_fn(txt=message_text, mdl=model, wd=working_dir, new=new_conv,
                           pp=prompt_profile, pm=permission_mode, roles=council_roles):
                    display.set_event_callback(make_emit())
                    try:
                        if roles:
                            if new:
                                result = run_council_agent(txt, wd, client, roles,
                                                           prompt_profile=pp, permission_mode=pm)
                            else:
                                result = continue_council_agent(state["messages"], txt, client,
                                                                roles, wd, pm)
                        elif new:
                            result = run_agent(txt, mdl, wd, client, pp, pm)
                        else:
                            result = continue_agent(state["messages"], txt, mdl, wd, client, pm)
                        state["messages"] = result
                    except Exception as exc:
                        loop.call_soon_threadsafe(
                            event_q.put_nowait, {"type": "error", "text": str(exc)}
                        )
                    finally:
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
                    chat_id = _save_chat(state["messages"], state.get("model", ""), existing_id)
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
                    await send({"type": "chat_loaded",
                                "id": chat_id,
                                "messages": data["messages"],
                                "model": data.get("model", "")})
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
                resume = bool(msg.get("resume", False))
                state["running"] = True

                if state.get("backend"):
                    cfg["backend"] = state["backend"]
                _resolved = resolve_backend(cfg)
                client = make_client(_resolved["api_key"], _resolved["base_url"])
                model = model_all or cfg.get("default_model")

                from ..lab.state import LabSession
                from ..lab.runner import run_lab
                lab_session = LabSession.load(working_dir) if resume else None
                if lab_session is None:
                    lab_session = LabSession(task=task, working_dir=working_dir,
                                             model=model or "")
                lab_session.autonomous = autonomous
                state["lab_session"] = lab_session

                def lab_fn(t=task, wd=working_dir, au=autonomous, mr=max_rounds,
                           md=model, res=resume, sess=lab_session):
                    display.set_event_callback(make_emit())
                    try:
                        run_lab(task=t, working_dir=wd, client=client, model=md,
                                autonomous=au, max_rounds=mr, resume=res,
                                emit=make_emit(), session=sess)
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

                from ..lab.state import LabSession
                from ..lab.runner import continue_lab
                lab_session = LabSession.load(working_dir)
                if lab_session is None:
                    await send({"type": "error", "text": "No completed lab found in this directory."})
                    continue
                state["running"] = True
                state["lab_session"] = lab_session

                def followup_fn(fb=feedback, wd=working_dir, md=model, sess=lab_session):
                    display.set_event_callback(make_emit())
                    try:
                        continue_lab(working_dir=wd, client=client, feedback=fb,
                                     model=md, emit=make_emit(), session=sess)
                    except Exception as exc:
                        loop.call_soon_threadsafe(
                            event_q.put_nowait, {"type": "error", "text": str(exc)})
                    finally:
                        display.clear_event_callback()
                        loop.call_soon_threadsafe(event_q.put_nowait, {"type": "_sentinel"})

                threading.Thread(target=followup_fn, daemon=True).start()
                await stream_events()
                state["lab_session"] = None

    except WebSocketDisconnect:
        pass
    except Exception as exc:
        try:
            await websocket.send_json({"type": "error", "text": f"Server error: {exc}"})
        except Exception:
            pass
