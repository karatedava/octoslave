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


def _read() -> Optional[list[dict]]:
    """The index, [] when there is none yet — or None when a file exists but
    could not be read. Callers must not write after None: saving what they have
    would replace the user's whole session history with a near-empty list."""
    try:
        text = _INDEX.read_text()
    except FileNotFoundError:
        return []
    except Exception:
        return None
    try:
        data = json.loads(text)
    except Exception:
        return None
    return data if isinstance(data, list) else None


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
        if items is None:
            return
        prior = next((s for s in items if s.get("working_dir") == wd), None)
        created = (prior or {}).get("created_at") or _now()
        title = _title(task) if task else (prior or {}).get("title") or _title("")
        items = [s for s in items if s.get("working_dir") != wd]
        items.insert(0, {"working_dir": wd, "title": title,
                         "created_at": created, "updated_at": _now()})
        _write(items)


def list_sessions() -> list[dict]:
    """Return indexed sessions whose state.json can be found right now.

    Ones that can't are only HIDDEN, not deleted: a folder can be briefly out of
    reach (an unmounted drive, a network share, a folder being synced) and must
    reappear when it is back. Only an explicit remove() forgets a session.
    """
    with _LOCK:
        items = _read()
        if items is None:
            return []
        return [s for s in items
                if (Path(s.get("working_dir", "")) / "science" / "state.json").exists()]


def remove(working_dir: str) -> bool:
    wd = str(Path(working_dir).expanduser().resolve())
    with _LOCK:
        items = _read()
        if items is None:
            return False
        kept = [s for s in items if s.get("working_dir") != wd]
        _write(kept)
        return len(kept) != len(items)
