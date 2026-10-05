"""Live steering: messages a user sends while an agent is already working.

The web Science tab lets the researcher keep talking while the orchestrator
works. Those messages cannot start a new turn (one is in flight) and must not
wait for it to end either — the whole point is to change what the agent is
doing *now*. They land in an :class:`Inbox` bound to the worker thread, and the
agent loop folds them into the conversation at the next safe point: after a
round of tool results (never between two of them — strict servers require an
assistant turn's tool messages to stay contiguous), or instead of ending the
turn when the model was about to stop.

Delivery is two-tier, because Science specialists run on their own threads
alongside the orchestrator, each in its own loop:

* the orchestrator's loop CONSUMES messages (:func:`deliver`) — they become
  ordinary user turns in its history;
* every working specialist gets an FYI copy (:func:`relay`), so a long
  specialist run can react to "stop, that's the wrong dataset" straight away
  without waiting for the orchestrator to pass it on.

An Inbox is shared by those threads, and each item tracks which specialists have
already seen it, so a relay to one never hides it from another or from the
orchestrator.

With no inbox bound to the thread every function here is a no-op, so the chat,
the Lab and the CLI are unaffected.
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Callable, Optional

from . import interrupt


class Inbox:
    """Thread-safe queue of user messages for one running turn."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: list[dict] = []
        # Messages the orchestrator has already taken, kept a little longer so a
        # specialist working in parallel still gets its FYI copy: whichever loop
        # reaches a safe point first must not hide a message from the others.
        self._recent: list[dict] = []
        self._closed = False
        # Called as on_deliver(items, to, who) whenever messages reach an agent.
        # ``to`` is "orchestrator" or "specialist" (then ``who`` is its name).
        self.on_deliver: Optional[Callable[[list[dict], str, str], None]] = None
        # Optional: returns news for the orchestrator that is not from the user
        # (e.g. a background worker reporting back) as ready-to-send text, or "".
        # Folded in at the same safe points as user messages.
        self.reports: Optional[Callable[[], str]] = None

    def post(self, text: str, *, prompt: str = "", meta: dict | None = None) -> str | None:
        """Queue a message. Returns its id, or None when the turn is already
        wrapping up (the caller should then start a fresh turn with it)."""
        with self._lock:
            if self._closed:
                return None
            item = {"id": uuid.uuid4().hex[:10], "text": text,
                    "prompt": prompt or text, "meta": dict(meta or {}),
                    "at": time.time(), "_relayed": set()}
            self._items.append(item)
            return item["id"]

    def pending(self) -> bool:
        with self._lock:
            return bool(self._items)

    def take(self) -> list[dict]:
        with self._lock:
            out, self._items = self._items, []
            self._recent = (self._recent + out)[-_RECENT_KEEP:]
            return out

    def unrelayed(self, who: str) -> list[dict]:
        """Items ``who`` has not been shown yet (queued ones stay queued)."""
        with self._lock:
            fresh = [it for it in self._recent + self._items
                     if who not in it["_relayed"]]
            for it in fresh:
                it["_relayed"].add(who)
            return fresh

    def mark_seen(self, who: str) -> None:
        """A worker starting now does not need messages the orchestrator has
        already acted on — its instructions were written with them in mind."""
        with self._lock:
            for it in self._recent:
                it["_relayed"].add(who)

    def close_if_empty(self) -> list[dict]:
        """Atomically: hand back anything still queued, or — if nothing is —
        close the inbox so later posts are refused rather than silently lost."""
        with self._lock:
            if self._items:
                out, self._items = self._items, []
                return out
            self._closed = True
            return []

    def close(self) -> list[dict]:
        """Close for good; returns whatever was never delivered."""
        with self._lock:
            self._closed = True
            out, self._items = self._items, []
            self._recent = []
            return out

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed


# How many already-delivered messages stay available for FYI relay.
_RECENT_KEEP = 20


# ---------------------------------------------------------------------------
# Registry (keyed by session) and thread binding
# ---------------------------------------------------------------------------

_REG: dict[str, Inbox] = {}
_REG_LOCK = threading.Lock()
_tl = threading.local()


def open_inbox(key: str) -> Inbox:
    inbox = Inbox()
    with _REG_LOCK:
        _REG[key] = inbox
    return inbox


def get_inbox(key: str) -> Inbox | None:
    with _REG_LOCK:
        return _REG.get(key)


def drop_inbox(key: str, inbox: Inbox | None = None) -> None:
    with _REG_LOCK:
        if inbox is None or _REG.get(key) is inbox:
            _REG.pop(key, None)


def bind(inbox: Inbox | None) -> None:
    _tl.inbox = inbox


def unbind() -> None:
    _tl.inbox = None


def current() -> Inbox | None:
    return getattr(_tl, "inbox", None)


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------

STEER_HEADER = "[Live message from the user — sent while you were working]"

_ORCH_GUIDANCE = (
    "(The user can message you at any time while you work. Take this into account "
    "right now: if it changes direction, adjust your plan and todo list before "
    "continuing; if it is a question, answer it briefly and carry on; if it asks "
    "you to stop or drop something, do so. Acknowledge it in one short line in "
    "your next message so they know it landed.)"
)

_SPEC_GUIDANCE = (
    "(The orchestrator will also get this when you report back. If it changes or "
    "cancels what you are doing, adapt now — or, if your task no longer makes "
    "sense, stop and report what you have. Otherwise carry on.)"
)


def format_for_orchestrator(items: list[dict]) -> str:
    if len(items) == 1:
        body = items[0]["prompt"]
    else:
        body = "\n\n".join(f"{i}. {it['prompt']}" for i, it in enumerate(items, 1))
    return f"{STEER_HEADER}\n\n{body}\n\n{_ORCH_GUIDANCE}"


def _format_for_specialist(items: list[dict]) -> str:
    body = "\n\n".join(it["prompt"] for it in items)
    return ("[FYI — the user just sent this to the orchestrator while you were "
            f"working]\n\n{body}\n\n{_SPEC_GUIDANCE}")


def _notify(inbox: Inbox, items: list[dict], to: str, who: str = "") -> None:
    cb = inbox.on_deliver
    if cb is None:
        return
    try:
        cb(items, to, who)
    except Exception:
        pass


def deliver(messages: list[dict]) -> int:
    """Orchestrator loop: fold every queued message — and any report from a
    background worker — into ``messages`` as one user turn. Returns how many
    items were delivered (0 when there was nothing, or no inbox is bound to this
    thread); a non-zero result means the loop should keep going."""
    inbox = current()
    # A stop is ending this turn: leave the message queued so it is reported as
    # undelivered, rather than slipped into a history nothing will act on.
    if inbox is None or interrupt.should_stop():
        return 0
    items = inbox.take()
    news = ""
    if inbox.reports is not None:
        try:
            news = inbox.reports() or ""
        except Exception:
            news = ""
    if not items and not news:
        return 0
    parts = [news] if news else []
    if items:
        parts.append(format_for_orchestrator(items))
    msg = {"role": "user", "content": "\n\n---\n\n".join(parts)}
    if items:
        # Only the researcher's own words are shown back as their message.
        msg["_user"] = True
        msg["_display"] = "\n\n".join(it["text"] for it in items)
    messages.append(msg)
    if items:
        _notify(inbox, items, "orchestrator")
    return len(items) + (1 if news else 0)


def relay(messages: list[dict], who: str) -> int:
    """Specialist loop: show queued messages as an FYI without consuming them."""
    inbox = current()
    if inbox is None or interrupt.should_stop():
        return 0
    items = inbox.unrelayed(who)
    if not items:
        return 0
    messages.append({"role": "user", "content": _format_for_specialist(items)})
    _notify(inbox, items, "specialist", who)
    return len(items)
