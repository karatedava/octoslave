"""Live within an API key's request limit instead of tripping over it.

Gateways such as e-INFRA's (LiteLLM) cap how many requests one KEY may have
running at once ("max_parallel_requests"). The orchestrator, every background
specialist and any chat in this process share that key. A request over the
limit used to come back 429, and then:

* a specialist read the 429 as "this model is broken" and switched models —
  six times in a row, all on the same key, so all refused — and stopped;
* the orchestrator retried five times over about two minutes, then ended the
  turn with an error;
* before either saw it, the SDK had quietly sent the request twice more.

Two parts fix that.

1. A gate per (endpoint, key), shared by every client in the process, holds a
   request back until a slot is free, so OctoSlave stops tripping the limit
   itself. Its size follows what the server actually accepts. A refusal
   shrinks it to the number of OUR requests the server is running at that
   moment — the server can count more than ours: requests whose connection
   died without it noticing yet (a closed lid, a dropped network), or another
   program using the same key. After 20 quiet seconds with work waiting it grows
   back one slot at a time, up to the limit the server states ("Current
   limit: 4"). When the server refuses while none of ours is running, it is
   full with requests that are not ours: the gate then holds every request
   back on an escalating pause and lets one through at a time to find out when
   a slot frees. Waiting is Stop-aware and never counts as a failure.

2. ``wait_after(exc)`` — what an agent loop does with a 429 that reached it.
   A rate limit belongs to the key, not the model: never switch models over
   it, never count it as the model failing. Tell the researcher what is going
   on and send it again (the gate does the waiting); give up only once the
   server has refused everything for ``OCTOSLAVE_RATE_WAIT_MAX`` seconds
   (default 2 h).

The 429's "Limit resets at" and ``retry-after`` are deliberately ignored: for a
parallel-request limit LiteLLM sets both to now + its window size (30 min on
e-INFRA), which says nothing about when a slot frees — a slot frees the moment
a request finishes.
"""
from __future__ import annotations

import bisect
import hashlib
import itertools
import os
import re
import threading
import time
import weakref
from contextlib import contextmanager
from urllib.parse import urlsplit

import httpx
import openai

from . import display, interrupt
from .netwatch import _stopped, _took

FOREGROUND, BACKGROUND = 0, 1

_GROW_AFTER = 20.0                   # quiet seconds after a refusal before trying one slot more
_HOLD_STEPS = (5, 10, 15, 20, 30)   # pauses while the server is full with requests not ours
_QUEUE_NOTE_AFTER = 20.0             # a wait for a slot this long is worth a line in the feed…
_QUEUE_NOTE_EVERY = 600.0            # …at most this often per key
_STILL_EVERY = 300.0                 # while refused: a reminder this often
_GAVE_UP_FOR = 30.0                  # after giving up, refuse at once for this long, then start fresh
_BLOCK_STALE = 120.0                 # no refusal for this long: whatever block there was is over
RATE_WAIT_MAX = float(os.environ.get("OCTOSLAVE_RATE_WAIT_MAX") or 2 * 3600)


def _seed() -> int | None:
    try:
        n = int(os.environ.get("OCTOSLAVE_MAX_PARALLEL_REQUESTS") or 0)
    except ValueError:
        return None
    return n if n > 0 else None


class GaveUp(openai.OpenAIError):
    """The server has refused this key's requests for longer than
    RATE_WAIT_MAX. An OpenAIError so the SDK passes it through unwrapped
    (anything else raised inside a request it would report as a connection
    error — and a connection error triggers a model switch)."""


# Threads waiting for a slot right now: ident -> (since, why). Read by the
# Science specialist pool, so the orchestrator is told a quiet specialist is
# queued, not stuck.
_WAITING: dict[int, tuple[float, str]] = {}


def waiting(ident: int) -> tuple[float, str] | None:
    """(seconds waiting, why) when thread ``ident`` is waiting for a slot."""
    w = _WAITING.get(ident)
    return (time.monotonic() - w[0], w[1]) if w else None


class _Slot:
    __slots__ = ("_gate", "_owner", "_done", "__weakref__")

    def __init__(self, gate: "_Gate", owner: int):
        self._gate, self._owner, self._done = gate, owner, False

    def release(self) -> None:
        self._gate._give_back(self)


class _Gate:
    def __init__(self, host: str, cap: int | None = None):
        self.host = host or "the model server"
        self.cap = cap              # how many we let run at once; None = no limit known
        self.limit = cap            # the most the server says it allows
        self.in_flight = 0
        self._cond = threading.Condition()
        self._seq = itertools.count()
        self._waiters: list[tuple[int, int]] = []   # sorted (priority, ticket)
        self._held: dict[int, int] = {}             # thread ident -> slots it holds
        self.hold_until = 0.0       # nothing goes before this (monotonic)
        self._holds = 0             # refusals in a row while full with requests not ours
        self.blocked_since: float | None = None
        self.gave_up_until = 0.0
        self.gave_up_after = 0.0    # how long it had refused everything when we gave up
        self.told_gave_up = False
        self.last_refusal = 0.0
        self._last_grow = 0.0
        self.told_limit = False
        self.told_block = False
        self.last_still = 0.0
        self._noted_queue = -1e9

    # -- slots ---------------------------------------------------------------
    def _take(self, me: int) -> _Slot:
        self.in_flight += 1
        self._held[me] = self._held.get(me, 0) + 1
        return _Slot(self, me)

    def _give_back(self, slot: _Slot) -> None:
        with self._cond:
            if slot._done:
                return
            slot._done = True
            self.in_flight = max(0, self.in_flight - 1)
            n = self._held.get(slot._owner, 0) - 1
            if n > 0:
                self._held[slot._owner] = n
            else:
                self._held.pop(slot._owner, None)
            self._cond.notify_all()

    def _may_go(self, ticket, now: float) -> bool:
        if now < self.hold_until:
            return False
        if self.cap is None:
            return True
        free = self.cap - self.in_flight
        return free > 0 and ticket in self._waiters[:free]

    def _maybe_grow(self, now: float) -> None:
        if (self.cap is not None and self._waiters and self.in_flight >= self.cap
                and (self.limit is None or self.cap < self.limit)
                and self.blocked_since is None
                and now - self.last_refusal >= _GROW_AFTER
                and now - self._last_grow >= _GROW_AFTER):
            self.cap += 1
            self._last_grow = now

    def acquire(self, prio: int = FOREGROUND) -> _Slot:
        me = threading.get_ident()
        with self._cond:
            if self._held.get(me):
                # A request made while this thread already holds a slot (a
                # helper call nested inside one): never wait on ourselves.
                return self._take(me)
            ticket = (prio, next(self._seq))
            bisect.insort(self._waiters, ticket)
            since = time.monotonic()
            try:
                while True:
                    now = time.monotonic()
                    if now < self.gave_up_until:
                        raise GaveUp(f"{self.host} has refused every request from this "
                                     f"key for too long")
                    if self.blocked_since is not None and now - self.last_refusal > _BLOCK_STALE:
                        # Nobody has been refused for a while (everyone stopped,
                        # or the work ended mid-block): that block is history,
                        # not something to count a new request's patience from.
                        self.blocked_since = None
                        self._holds = 0
                        self.told_block = False
                    if self.blocked_since is not None and now - self.blocked_since > RATE_WAIT_MAX:
                        self._give_up(now)
                        continue
                    self._maybe_grow(now)
                    if self._may_go(ticket, now):
                        break
                    if _stopped():
                        raise interrupt.StopRequested()
                    _WAITING[me] = (since, "full" if now < self.hold_until else "queue")
                    if (now - since >= _QUEUE_NOTE_AFTER and now >= self.hold_until
                            and self.blocked_since is None
                            and now - self._noted_queue >= _QUEUE_NOTE_EVERY):
                        self._noted_queue = now
                        self._say(self._queue_note())
                    self._cond.wait(0.5)
            finally:
                self._waiters.remove(ticket)
                _WAITING.pop(me, None)
                self._cond.notify_all()
            return self._take(me)

    def _say(self, text: str, error: bool = False) -> None:
        """Print from inside the lock without holding it while the UI runs."""
        self._cond.release()
        try:
            (display.print_error if error else display.print_info)(text)
        finally:
            self._cond.acquire()

    def _queue_note(self) -> str:
        cap, limit = self.cap or 0, self.limit
        if limit and cap < limit:
            return (f"Requests are taking turns: {self.host} has room for only {cap} of "
                    f"this key's requests at once right now — it allows {limit}, but "
                    f"at the last check it counted {limit - cap} more as running that "
                    f"this OctoSlave does not have open (most likely connections that "
                    f"died without the server noticing; it releases those on its own). "
                    f"The agents wait for a slot in turn — nothing has failed.")
        return (f"Requests are taking turns: {self.host} runs {cap} requests at once "
                f"from this key, and with several agents working the others wait "
                f"for a slot — nothing has failed.")

    def _give_up(self, now: float) -> None:
        if now >= self.gave_up_until:           # a new give-up, not a repeat
            self.gave_up_after = now - (self.blocked_since or now)
            self.told_gave_up = False
        self.gave_up_until = now + _GAVE_UP_FOR
        self.blocked_since = None
        self.hold_until = 0.0
        self._holds = 0
        self.told_block = False
        self._cond.notify_all()

    # -- what the server said -------------------------------------------------
    def refused(self, limit: int | None) -> tuple[str, int]:
        """A 429 came back for a request that still holds its slot. Shrink to
        what the server accepts; returns ("ours" | "full", our other requests
        running)."""
        with self._cond:
            now = time.monotonic()
            self.last_refusal = now
            if limit:
                self.limit = limit
            ours = max(0, self.in_flight - 1)
            self.cap = max(1, ours if self.cap is None else min(self.cap, ours))
            if ours:
                situation = "ours"
            else:
                # Full with requests that are not ours: hold everything back,
                # longer each time, and let one through to test.
                situation = "full"
                step = _HOLD_STEPS[min(self._holds, len(_HOLD_STEPS) - 1)]
                self._holds += 1
                self.hold_until = max(self.hold_until, now + step)
                if self.blocked_since is None:
                    self.blocked_since = now
            self._cond.notify_all()
            return situation, ours

    def accepted(self) -> float | None:
        """A request got through. Returns how long the server had been
        refusing everything, if it had been."""
        with self._cond:
            was = self.blocked_since
            self.blocked_since = None
            self._holds = 0
            self.hold_until = 0.0
            self.told_block = False
            return None if was is None else time.monotonic() - was


_GATES: dict[tuple[str, str], _Gate] = {}
_GATES_LOCK = threading.Lock()


def gate_for(base_url: str, api_key: str) -> _Gate:
    parts = urlsplit(base_url or "")
    where = f"{parts.scheme}://{parts.netloc}{parts.path.rstrip('/')}".lower()
    who = hashlib.sha256((api_key or "").encode()).hexdigest()[:16]
    with _GATES_LOCK:
        g = _GATES.get((where, who))
        if g is None:
            g = _GATES[(where, who)] = _Gate(parts.hostname or "", _seed())
        return g


def _gate_of(client) -> _Gate | None:
    return getattr(getattr(client, "_client", None), "_octo_gate", None)


# -- priority: the researcher's conversation goes ahead of background work ----
_TL = threading.local()


def _prio() -> int:
    return getattr(_TL, "prio", FOREGROUND)


@contextmanager
def background():
    """Requests made inside this block queue behind foreground ones (the
    orchestrator answering the researcher, a chat turn)."""
    prev = _prio()
    _TL.prio = BACKGROUND
    try:
        yield
    finally:
        _TL.prio = prev


# -- reading a 429 -------------------------------------------------------------
_RE_TYPE = re.compile(r"Limit type:\s*([A-Za-z_]+)")
_RE_CURRENT = re.compile(r"Current limit:\s*(\d+)")
_RE_OLD = re.compile(r"(?<!current )max_parallel_requests['\"]?\s*[:=]\s*(\d+)", re.I)


def _parallel_limit(text: str, headers) -> int | None:
    """The server's stated limit on requests at once, if this 429 is about it."""
    kind = ""
    try:
        kind = headers.get("rate_limit_type") or ""
    except Exception:  # noqa: BLE001
        pass
    m = _RE_TYPE.search(text or "")
    kind = (kind or (m.group(1) if m else "")).lower()
    if "parallel" not in kind and "max_parallel_requests" not in (text or ""):
        return None
    m = _RE_CURRENT.search(text or "") or _RE_OLD.search(text or "")
    return int(m.group(1)) if m else None


def _quota_gone(text: str) -> bool:
    s = (text or "").lower()
    return ("insufficient_quota" in s or "exceeded your current quota" in s
            or "budget has been exceeded" in s or "exceededbudget" in s)


def is_rate_limit(exc: BaseException | None) -> bool:
    """A 429 / rate-limit refusal (or the gate giving up on one)."""
    if exc is None:
        return False
    if isinstance(exc, (GaveUp, openai.RateLimitError)):
        return True
    if getattr(exc, "status_code", None) == 429:
        return True
    s = str(exc).lower()
    return "rate limit" in s or "ratelimit" in s or "too many requests" in s


def install(client, api_key: str = "", base_url: str = "") -> bool:
    """Route ``client``'s requests through the gate for its (endpoint, key).
    Idempotent; False if the client isn't shaped as expected."""
    http = getattr(client, "_client", None)
    send = getattr(http, "send", None)
    if http is None or send is None or getattr(http, "_octo_gate", None) is not None:
        return False
    gate = gate_for(base_url or str(getattr(client, "base_url", "") or ""),
                    api_key or str(getattr(client, "api_key", "") or ""))

    def gated_send(request, *args, **kwargs):
        if request.method != "POST":          # listing models etc. is not limited
            return send(request, *args, **kwargs)
        slot = gate.acquire(_prio())
        try:
            resp = send(request, *args, **kwargs)
        except BaseException:
            slot.release()
            raise
        try:
            if resp.status_code == 429:
                _note_refusal(gate, resp)
                slot.release()
                return resp
            if resp.status_code < 400:
                blocked = gate.accepted()
                if blocked is not None:
                    display.print_info(f"{gate.host} is accepting requests again after "
                                       f"{_took(blocked)} — carrying on.")
                    display.net_status("ok", gate.host)
            if resp.is_closed:                 # body already read (not streamed)
                slot.release()
            else:                              # streamed: the slot is held until it closes
                resp.stream = _ReleasingStream(resp.stream, slot.release)
                weakref.finalize(resp, slot.release)
            return resp
        except BaseException:
            slot.release()
            raise

    http.send = gated_send
    http._octo_gate = gate
    return True


class _ReleasingStream(httpx.SyncByteStream):
    def __init__(self, inner, release):
        self._inner, self._release = inner, release

    def __iter__(self):
        yield from self._inner

    def close(self) -> None:
        try:
            self._inner.close()
        finally:
            self._release()


def _note_refusal(gate: _Gate, resp) -> None:
    try:
        resp.read()
        text = resp.text
    except Exception:  # noqa: BLE001
        text = ""
    situation, ours = gate.refused(_parallel_limit(text, resp.headers))
    # The SDK would re-send it at once, twice, unseen — and not Stop-aware.
    # The agent loops decide instead (see wait_after).
    resp.headers["x-should-retry"] = "false"
    resp.extensions["octoslave_rate"] = {"situation": situation, "ours": ours,
                                         "quota": _quota_gone(text)}


def wait_after(exc: BaseException, client=None, who: str = "") -> bool:
    """A rate-limit error reached an agent loop. True: send the request again
    now — the gate holds it until it may go. False: give up (the key's quota
    is gone, or the server has refused everything for longer than
    RATE_WAIT_MAX); the caller stops cleanly with the work so far saved.
    Raises interrupt.StopRequested on Stop."""
    tag = f"[{who}] " if who else ""
    if isinstance(exc, GaveUp):
        _tell_gave_up(_gate_of(client), tag)
        return False
    resp = getattr(exc, "response", None)
    note = (getattr(resp, "extensions", None) or {}).get("octoslave_rate") or {}
    if note.get("quota") or _quota_gone(str(exc)):
        display.print_error(f"{tag}The API key has used up its quota or budget — waiting "
                            f"will not help. Everything done so far is saved.")
        return False
    gate = _gate_of(client)
    if gate is None or not note:
        return _plain_backoff(tag)
    if note.get("situation") == "ours":
        with gate._cond:
            first, gate.told_limit = not gate.told_limit, True
        if first:
            lim = f"{gate.limit} requests" if gate.limit else "only so many requests"
            display.print_info(
                f"{gate.host} lets this key run {lim} at once — OctoSlave now queues "
                f"its requests to stay within that, so the agents take turns instead "
                f"of failing.")
        if _pause(0.2):
            raise interrupt.StopRequested()
        return True
    # The server is full with requests that are not ours.
    now = time.monotonic()
    with gate._cond:
        since = gate.blocked_since
        if since is None:
            # Another request got through in the meantime: no longer full.
            return True
        first, gate.told_block = not gate.told_block, True
        remind = not first and now - gate.last_still >= _STILL_EVERY
        if first or remind:
            gate.last_still = now
    if now - since > RATE_WAIT_MAX:
        with gate._cond:
            gate._give_up(now)        # every other request waiting on it stops too
        _tell_gave_up(gate, tag)
        return False
    if first:
        busy = (f"it allows {gate.limit} at once and counts that many as running"
                if gate.limit else "it counts the key as busy")
        display.print_info(
            f"{tag}{gate.host} is refusing new requests from this key: {busy}, "
            f"but this OctoSlave has none open. Most likely "
            f"those are requests whose connection died without the server noticing "
            f"(a closed lid, a dropped network) — it releases them on its own — or "
            f"another program is using the same key. Waiting for a free slot; the "
            f"work carries on by itself (Stop ends it).")
        display.net_status("limited", gate.host)
    elif remind:
        display.print_info(f"{tag}Still waiting for {gate.host} to accept a request "
                           f"({_took(now - since)} so far).")
    return True


def _tell_gave_up(gate: _Gate | None, tag: str) -> None:
    """Say once per give-up (not once per waiting agent) why the work stops."""
    if gate is None:
        return
    with gate._cond:
        first, gate.told_gave_up = not gate.told_gave_up, True
        after = gate.gave_up_after
    if first:
        display.print_error(
            f"{tag}{gate.host} has refused every request from this key for "
            f"{_took(after)} — giving up for now. Everything done so far is saved; "
            f"send a message (or resume the specialist) once it accepts requests "
            f"again.")
        display.net_status("ok", gate.host)


def _pause(seconds: float) -> bool:
    from .netwatch import _pause as pause
    return pause(seconds)


_PLAIN = threading.local()


def _plain_backoff(tag: str) -> bool:
    """No gate on this client: a Stop-aware escalating pause."""
    now = time.monotonic()
    if now - getattr(_PLAIN, "last", -1e9) > 600:      # a new run of refusals
        _PLAIN.n, _PLAIN.start = 0, now
    _PLAIN.last = now
    if now - _PLAIN.start > RATE_WAIT_MAX:
        _PLAIN.n, _PLAIN.start = 0, now
        return False
    n = _PLAIN.n
    wait = _HOLD_STEPS[min(n, len(_HOLD_STEPS) - 1)]
    _PLAIN.n = n + 1
    display.print_info(f"{tag}Rate limit — waiting {wait} s, then trying again.")
    if _pause(wait):
        raise interrupt.StopRequested()
    return True
