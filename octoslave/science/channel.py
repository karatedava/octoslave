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

A session can run for weeks, so the timeline is compacted between turns
(:meth:`ScienceChannel.compact_if_needed`): beyond :data:`COMPACT_AT` records,
everything but the recent turns moves to ``science/archive/`` (gzipped, never
loaded by the page) and is replaced by one ``science_compacted`` record — a
digest of what was asked, answered, produced and run. Opening a session then
costs the same on day thirty as on day one; the results themselves live on in
the outputs, the provenance ledger and the files.
"""

from __future__ import annotations

import gzip
import json
import os
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

# Events worth keeping in the timeline. Token/reasoning deltas are folded into
# ``assistant_message`` records; transient UI signals (questions, done) are not
# replayed — a replayed question could never be answered.
_PERSIST = frozenset({
    "science_user", "science_steer", "science_reply", "science_specialist",
    "science_job", "science_artifact", "science_provenance", "tool_call",
    "tool_result", "todos", "plan", "info", "error", "science_wake",
    "science_settings", "science_artifact_removed",
})
_SPEAKER_KEYS = ("agent_id", "agent_name", "agent_role", "agent_icon")
_MAX_ARG_CHARS = 1500
HISTORY_LIMIT = 5000
#: Timeline records that trigger compaction, and roughly how many recent ones
#: stay (the cut is moved to the start of a turn, so no turn is split).
COMPACT_AT = 2500
KEEP_RECENT = 800
#: Earlier turns the digest lists one by one (older ones are only counted).
DIGEST_TURNS = 60


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
        # The question an agent is waiting on right now, if any. Not part of the
        # timeline (an old question cannot be answered), but a viewer that
        # reloads while it is open must still see it, or the agent sits blocked.
        self._question: dict | None = None

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
            # The last record alone is bigger than the tail read (a long
            # reply): read it all. Restarting the count at 0 would make every
            # viewer drop each new event as one it has already seen.
            best = 0
            with self.log_path.open(encoding="utf-8", errors="replace") as f:
                for line in f:
                    try:
                        best = max(best, int(json.loads(line).get("seq") or 0))
                    except Exception:
                        continue
            return best
        except Exception:
            pass
        return 0

    def _persist(self, ev: dict) -> None:
        # When it happened: the digest of compacted history needs dates, and a
        # replayed session can show how long things took.
        if "ts" not in ev:
            ev = {**ev, "ts": round(time.time(), 1)}
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def _track(self, ev: dict) -> None:
        """Maintain the in-flight stream and write timeline records (lock held)."""
        t = ev.get("type")
        if t == "user_question":
            self._question = ev
            return
        if t == "user_question_closed":
            self._question = None
            return
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
            return self._catchup_locked()

    def attach(self, loop, q, limit: int = HISTORY_LIMIT) -> list[dict]:
        """Subscribe and read the timeline in one step; returns the history.

        Done under the lock, so the history holds exactly the events published
        before this moment and the queue exactly the ones after — nothing
        missing, nothing twice. Reading the log separately let events land in
        between, and a viewer then threw away live text it had never seen. The
        catch-up for a reply in flight goes on the queue first, ahead of any
        live event (call_soon_threadsafe keeps the order).
        """
        with self._lock:
            self._subs.add((loop, q))
            for ev in self._catchup_locked():
                loop.call_soon_threadsafe(q.put_nowait, ev)
            return self.history(limit)

    def _catchup_locked(self) -> list[dict]:
        # No seq: the text is exactly what was streamed before this moment, so it
        # must not be filtered against the timeline (where it is not, yet).
        out = [{"type": "token", "text": p["text"], **p["tags"], "catchup": True}
               for p in self._partials.values() if p["text"]]
        if self._question is not None:
            out.append({k: v for k, v in self._question.items() if k != "seq"})
        return out

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

    # -- compaction ----------------------------------------------------------
    def compact_if_needed(self, *, force: bool = False) -> bool:
        """Fold all but the recent turns into one digest record. Call only
        between turns. Returns True when the timeline was compacted."""
        with self._lock:
            records = self.history(limit=10 ** 9)
            if not records or (len(records) <= COMPACT_AT and not force):
                return False
            prev = None
            if records[0].get("type") == "science_compacted":
                prev, records = records[0], records[1:]
            cut = _turn_boundary(records, max(1, len(records) - KEEP_RECENT))
            if cut <= 0:
                return False
            old, keep = records[:cut], records[cut:]
            digest = _merge_digest((prev or {}).get("digest"), _digest(old))
            arc_dir = self.log_path.parent / "archive"
            try:
                arc_dir.mkdir(parents=True, exist_ok=True)
                # Appending gzip members keeps one file readable with zcat.
                with gzip.open(arc_dir / "feed-archive.jsonl.gz", "at", encoding="utf-8") as f:
                    for r in old:
                        f.write(json.dumps(r, ensure_ascii=False) + "\n")
            except Exception:
                return False                 # never drop what could not be archived
            head = {"type": "science_compacted", "digest": digest,
                    "seq": old[-1].get("seq") or 0, "ts": round(time.time(), 1)}
            tmp = self.log_path.with_suffix(".jsonl.tmp")
            try:
                with tmp.open("w", encoding="utf-8") as f:
                    for r in [head, *keep]:
                        f.write(json.dumps(r, ensure_ascii=False) + "\n")
                os.replace(tmp, self.log_path)
            except Exception:
                return False
            return True


def _turn_boundary(records: list[dict], at: int) -> int:
    """The index of the first record of a turn at or after ``at`` (falling back
    to the nearest one before it), so compaction never splits a turn."""
    def starts_turn(r: dict) -> bool:
        return (r.get("type") == "science_user" and not r.get("queued")) \
            or r.get("type") == "science_wake"
    for i in range(at, len(records)):
        if starts_turn(records[i]):
            return i
    for i in range(min(at, len(records) - 1), 0, -1):
        if starts_turn(records[i]):
            return i
    return 0


def _clip(text, n: int) -> str:
    t = " ".join(str(text or "").split())
    return t if len(t) <= n else t[:n].rstrip() + "…"


def _digest(records: list[dict]) -> dict:
    """What a stretch of the timeline amounted to, without the step-by-step."""
    turns: list[dict] = []
    outputs: dict[str, str] = {}
    jobs: dict[str, str] = {}
    specialists: dict[str, str] = {}
    steps = 0
    first = last = None
    for r in records:
        ts = r.get("ts")
        if ts:
            first = first or ts
            last = ts
        t = r.get("type")
        if t in ("science_user", "science_wake") and not r.get("queued"):
            turns.append({"asked": _clip(r.get("text"), 300), "ts": ts,
                          "auto": t == "science_wake"})
        elif t == "science_user" and turns:
            turns[-1].setdefault("also", []).append(_clip(r.get("text"), 200))
        elif t == "science_reply" and turns and not r.get("stopped"):
            turns[-1]["answer"] = _clip(r.get("text"), 700)
        elif t == "assistant_message" and turns and not r.get("agent_name"):
            turns[-1].setdefault("_last", _clip(r.get("text"), 700))
            turns[-1]["_last"] = _clip(r.get("text"), 700)
        elif t == "science_artifact" and r.get("rel"):
            outputs[r["rel"]] = _clip(r.get("caption") or "", 120)
        elif t == "science_job" and r.get("name"):
            jobs[r["name"]] = r.get("status") or ""
        elif t == "science_specialist" and r.get("event") == "start" and r.get("name"):
            specialists[r["name"]] = r.get("role") or ""
        elif t == "tool_call":
            steps += 1
    for tr in turns:
        last_said = tr.pop("_last", "")
        if not tr.get("answer") and last_said:
            tr["answer"] = last_said
    return {"turns": turns, "earlier_turns": 0, "outputs": outputs, "jobs": jobs,
            "specialists": specialists, "steps": steps, "from": first, "to": last}


def _merge_digest(prev: dict | None, new: dict) -> dict:
    if not prev:
        out = dict(new)
    else:
        out = {
            "turns": (prev.get("turns") or []) + new["turns"],
            "earlier_turns": int(prev.get("earlier_turns") or 0),
            "outputs": {**(prev.get("outputs") or {}), **new["outputs"]},
            "jobs": {**(prev.get("jobs") or {}), **new["jobs"]},
            "specialists": {**(prev.get("specialists") or {}), **new["specialists"]},
            "steps": int(prev.get("steps") or 0) + new["steps"],
            "from": prev.get("from") or new["from"],
            "to": new["to"] or prev.get("to"),
        }
    extra = len(out["turns"]) - DIGEST_TURNS
    if extra > 0:
        out["turns"] = out["turns"][extra:]
        out["earlier_turns"] = int(out.get("earlier_turns") or 0) + extra
    return out


_CHANNELS: dict[str, ScienceChannel] = {}
_CH_LOCK = threading.Lock()


def get_channel(key: str) -> ScienceChannel:
    with _CH_LOCK:
        ch = _CHANNELS.get(key)
        if ch is None:
            ch = _CHANNELS[key] = ScienceChannel(key)
        return ch


def all_channels() -> list[ScienceChannel]:
    with _CH_LOCK:
        return list(_CHANNELS.values())


def running_keys() -> set[str]:
    with _CH_LOCK:
        return {k for k, ch in _CHANNELS.items() if ch.running}
