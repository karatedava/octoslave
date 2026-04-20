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

from fastapi import FastAPI, File, Form, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from .. import display
from ..agent import continue_agent, make_client, run_agent
from ..config import load_config
from ..research import PIPELINE, run_long_research

# ---------------------------------------------------------------------------

STATIC_DIR = Path(__file__).parent / "static"
CHATS_DIR  = Path.home() / ".octoslave" / "chats"

app = FastAPI(title="OctoSlave Web UI", docs_url=None, redoc_url=None)

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

_ALLOWED_EXT = {
    ".html", ".htm", ".md", ".txt", ".json", ".csv",
    ".png", ".jpg", ".jpeg", ".svg", ".gif", ".py", ".sh",
}

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


@app.get("/api/files/list")
async def list_files(working_dir: str = "."):
    """Recursive list of research output files under working_dir/research/."""
    root = Path(working_dir) / "research"
    if not root.exists():
        return {"items": [], "root": str(root), "exists": False}
    items = []
    try:
        for p in sorted(root.rglob("*")):
            if p.is_file() and p.suffix in _ALLOWED_EXT:
                rel = str(p.relative_to(working_dir))
                items.append({
                    "path": rel,
                    "abs": str(p),
                    "name": p.name,
                    "dir": str(p.parent.relative_to(working_dir)),
                    "size": p.stat().st_size,
                    "ext": p.suffix,
                    "mtime": p.stat().st_mtime,
                })
    except Exception:
        pass
    return {"items": items, "root": str(root), "exists": True}


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
        """Drain event_q and forward events to the WebSocket until sentinel."""
        while True:
            event = await event_q.get()
            if event.get("type") == "_sentinel":
                state["running"] = False
                return
            try:
                await websocket.send_json(event)
            except Exception:
                return

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
            "working_dir": ".",
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
                        "working_dir": state["working_dir"],
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

            # ---- chat ----
            elif mtype in ("chat", "chat_new", "chat_continue"):
                if state["running"]:
                    await send({"type": "error", "text": "A task is already running."})
                    continue

                cfg = load_config()
                model = msg.get("model") or state["model"] or cfg.get("default_model", "")
                working_dir = msg.get("working_dir") or state["working_dir"]
                message_text = msg.get("message", "").strip()
                if not message_text:
                    continue

                new_conv = (mtype in ("chat", "chat_new")) or (not state["messages"])
                state["model"] = model
                state["working_dir"] = working_dir
                state["running"] = True
                client = make_client(cfg.get("api_key", ""), cfg.get("base_url", ""))

                def chat_fn(txt=message_text, mdl=model, wd=working_dir, new=new_conv):
                    display.set_event_callback(make_emit())
                    try:
                        if new:
                            result = run_agent(txt, mdl, wd, client)
                        else:
                            result = continue_agent(state["messages"], txt, mdl, wd, client)
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

            # ---- research ----
            elif mtype == "research":
                if state["running"]:
                    await send({"type": "error", "text": "A task is already running."})
                    continue

                cfg = load_config()
                topic = msg.get("topic", "").strip()
                if not topic:
                    await send({"type": "error", "text": "Topic is required."})
                    continue

                rounds = max(1, min(20, int(msg.get("rounds", 3))))
                model_all = msg.get("model_all") or None
                resume = bool(msg.get("resume", False))
                working_dir = msg.get("working_dir") or state["working_dir"]
                state["working_dir"] = working_dir
                state["running"] = True

                model_overrides = {role: model_all for role in PIPELINE} if model_all else None
                client = make_client(cfg.get("api_key", ""), cfg.get("base_url", ""))

                def research_fn(t=topic, r=rounds, mo=model_overrides, wd=working_dir, res=resume):
                    display.set_event_callback(make_emit())
                    try:
                        run_long_research(
                            topic=t,
                            working_dir=wd,
                            client=client,
                            max_rounds=r,
                            model_overrides=mo,
                            resume=res,
                        )
                    except Exception as exc:
                        loop.call_soon_threadsafe(
                            event_q.put_nowait, {"type": "error", "text": str(exc)}
                        )
                    finally:
                        display.clear_event_callback()
                        loop.call_soon_threadsafe(event_q.put_nowait, {"type": "_sentinel"})

                threading.Thread(target=research_fn, daemon=True).start()
                await stream_events()

    except WebSocketDisconnect:
        pass
    except Exception as exc:
        try:
            await websocket.send_json({"type": "error", "text": f"Server error: {exc}"})
        except Exception:
            pass
