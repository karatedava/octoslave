"""The live channel of one Science session: event fan-out + a persisted timeline.

Every event a Science turn emits goes through the session's channel, which

* stamps it with a per-session sequence number (``seq``), monotonic across
  turns and server restarts;
* appends the ones that matter for the timeline to ``science/feed.jsonl`` —
  messages, tool steps, specialists, jobs, outputs — condensing a token stream
  into one ``assistant_message`` record when it ends;
* fans it out to every connection currently viewing the session.

A viewer that opens the session — a reload, a second tab, coming back to it
from another session — subscribes FIRST, then reads the log, and drops any live
event whose ``seq`` the log already covered. So the timeline it rebuilds is the
real one (steps, specialists and outputs in place, not just chat bubbles), with
no gap and no duplicates. The only thing not in the log is the reply currently
being streamed; a new subscriber gets that as one catch-up ``token`` event.

The channel outlives individual turns (``running`` says whether one is in
flight), so a viewer can simply stay subscribed while the user keeps talking.
"""

from __future__ import annotations

import json
import threading
from collections import deque
from pathlib import Path
from typing import Any

# Events worth keeping in the timeline. Token/reasoning deltas are folded into
# ``assistant_message`` records; transient UI signals (questions, done) are not
# replayed — a replayed question could never be answered.
_PERSIST = frozenset({
    "science_user", "science_steer", "science_reply", "science_specialist",
    "science_job", "science_artifact", "science_provenance", "tool_call",
    "tool_result", "todos", "plan", "info", "error",
})
_SPEAKER_KEYS = ("agent_id", "agent_name", "agent_role", "agent_icon")
_MAX_ARG_CHARS = 1500
HISTORY_LIMIT = 5000


def _speaker(ev: dict) -> dict:
    return {k: ev[k] for k in _SPEAKER_KEYS if ev.get(k)}


def _stream_key(tags: dict) -> str:
    """Which stream an event belongs to: a specialist's id, or "" for the
    orchestrator. Several streams can be open at once."""
    return tags.get("agent_id") or ""


def _condense_tool_call(ev: dict) -> dict:
    """Keep a tool call's preview small on disk (a write_file can carry 4 KB)."""
    ap = ev.get("args_preview")
    if not isinstance(ap, dict):
        return ev
    small = {}
    for k, v in ap.items():
        small[k] = (v[:_MAX_ARG_CHARS] + "…") if isinstance(v, str) and len(v) > _MAX_ARG_CHARS else v
    return {**ev, "args_preview": small}


class ScienceChannel:
    def __init__(self, key: str):
        self.key = key
        self.log_path = Path(key) / "science" / "feed.jsonl"
        self.running = False
        self.agent_ident: int | None = None   # worker thread — so any viewer can Stop
        self.session: Any = None              # the live ScienceSession while running
        self._lock = threading.Lock()
        self._subs: set[tuple] = set()        # {(loop, queue)}
        self._seq = self._last_seq_on_disk()
        # Replies being streamed right now, keyed by speaker — the orchestrator
        # and several background specialists can stream AT THE SAME TIME, and one
        # shared slot would splice their text together (and lose whichever one
        # did not end last).
        self._partials: dict[str, dict] = {}
        self._last_text = ""                  # last assistant text persisted

    # -- sequence / persistence ------------------------------------------
    def _last_seq_on_disk(self) -> int:
        try:
            with self.log_path.open("rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 65536))
                tail = f.read().decode("utf-8", "replace").strip().splitlines()
            for line in reversed(tail):
                try:
                    return int(json.loads(line).get("seq") or 0)
                except Exception:
                    continue
        except Exception:
            pass
        return 0

    def _persist(self, ev: dict) -> None:
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def _track(self, ev: dict) -> None:
        """Maintain the in-flight stream and write timeline records (lock held)."""
        t = ev.get("type")
        if t == "stream_start":
            tags = _speaker(ev)
            self._partials[_stream_key(tags)] = {"text": "", "tags": tags, "seq": ev["seq"]}
            return
        if t == "token":
            tags = _speaker(ev)
            key = _stream_key(tags)
            p = self._partials.get(key)
            if p is None:
                p = self._partials[key] = {"text": "", "tags": tags, "seq": ev["seq"]}
            p["text"] += ev.get("text") or ""
            p["seq"] = ev["seq"]
            return
        if t == "stream_end":
            p = self._partials.pop(_stream_key(_speaker(ev)), None)
            text = (p or {}).get("text", "").strip()
            if text and not ev.get("aborted"):
                self._last_text = text
                self._persist({"type": "assistant_message", "text": text,
                               **(p or {}).get("tags", {}), "seq": ev["seq"]})
            return
        if t == "science_reply":
            text = (ev.get("text") or "").strip()
            if not text or (text == self._last_text and not ev.get("stopped")):
                return
            self._last_text = text
        if t in _PERSIST:
            self._persist(_condense_tool_call(ev) if t == "tool_call" else ev)

    # -- fan-out -----------------------------------------------------------
    def publish(self, event: dict) -> None:
        """Called from the turn's worker thread (and from the web loop)."""
        if not isinstance(event, dict) or event.get("type") == "_sentinel":
            return
        with self._lock:
            self._seq += 1
            ev = {**event, "seq": self._seq}
            self._track(ev)
            subs = list(self._subs)
        for loop, q in subs:
            try:
                loop.call_soon_threadsafe(q.put_nowait, ev)
            except RuntimeError:          # that viewer's event loop is gone
                with self._lock:
                    self._subs.discard((loop, q))

    def seed(self, events: list[dict]) -> None:
        """Write pre-existing history into an empty log (no fan-out)."""
        with self._lock:
            for ev in events:
                self._seq += 1
                self._persist({**ev, "seq": self._seq})

    def subscribe(self, loop, q) -> list[dict]:
        """Start receiving live events. Returns a catch-up event for the reply
        being streamed at this moment (it is not in the log yet), if any."""
        with self._lock:
            self._subs.add((loop, q))
            return [{"type": "token", "text": p["text"], **p["tags"], "seq": p["seq"]}
                    for p in self._partials.values() if p["text"]]

    def unsubscribe(self, loop, q) -> None:
        with self._lock:
            self._subs.discard((loop, q))

    def history(self, limit: int = HISTORY_LIMIT) -> list[dict]:
        """The persisted timeline (most recent ``limit`` records)."""
        out: deque = deque(maxlen=limit)
        try:
            with self.log_path.open(encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(json.loads(line))
                    except Exception:
                        continue
        except FileNotFoundError:
            return []
        except Exception:
            pass
        return list(out)

    def has_log(self) -> bool:
        return self.log_path.exists()


_CHANNELS: dict[str, ScienceChannel] = {}
_CH_LOCK = threading.Lock()


def get_channel(key: str) -> ScienceChannel:
    with _CH_LOCK:
        ch = _CHANNELS.get(key)
        if ch is None:
            ch = _CHANNELS[key] = ScienceChannel(key)
        return ch


def running_keys() -> set[str]:
    with _CH_LOCK:
        return {k for k, ch in _CHANNELS.items() if ch.running}
