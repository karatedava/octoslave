"""A lightweight index of Science sessions so the web UI can list past and
current sessions and reopen them after a refresh or tab switch.

Each Science session lives at ``<working_dir>/science/state.json``; this index
just records where they are plus a short title, in
``~/.octoslave/science_sessions.json`` (most-recent first).
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Optional

_INDEX = Path.home() / ".octoslave" / "science_sessions.json"
_LOCK = threading.Lock()
_MAX = 100


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def _read() -> list[dict]:
    try:
        data = json.loads(_INDEX.read_text())
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _write(items: list[dict]) -> None:
    _INDEX.parent.mkdir(parents=True, exist_ok=True)
    tmp = _INDEX.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(items[:_MAX], indent=2))
    tmp.replace(_INDEX)


def _title(task: str) -> str:
    t = (task or "").strip().splitlines()[0] if (task or "").strip() else ""
    t = t[:80]
    return t or "(untitled session)"


def record(working_dir: str, task: str = "") -> None:
    """Upsert a session, moving it to the top and refreshing updated_at."""
    wd = str(Path(working_dir).expanduser().resolve())
    with _LOCK:
        items = _read()
        prior = next((s for s in items if s.get("working_dir") == wd), None)
        created = (prior or {}).get("created_at") or _now()
        title = _title(task) if task else (prior or {}).get("title") or _title("")
        items = [s for s in items if s.get("working_dir") != wd]
        items.insert(0, {"working_dir": wd, "title": title,
                         "created_at": created, "updated_at": _now()})
        _write(items)


def list_sessions() -> list[dict]:
    """Return indexed sessions, dropping any whose state.json has vanished."""
    with _LOCK:
        items = _read()
        alive = [s for s in items
                 if (Path(s.get("working_dir", "")) / "science" / "state.json").exists()]
        if len(alive) != len(items):
            _write(alive)
        return alive


def remove(working_dir: str) -> bool:
    wd = str(Path(working_dir).expanduser().resolve())
    with _LOCK:
        items = _read()
        kept = [s for s in items if s.get("working_dir") != wd]
        _write(kept)
        return len(kept) != len(items)
