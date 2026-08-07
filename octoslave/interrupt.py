"""Cooperative, cross-thread stop signalling for agent runs.

The web/UI layer runs each agent in its own worker thread, but a "stop" request
arrives on a *different* thread (the asyncio event loop). A plain thread-local
flag is therefore not enough: the requester and the worker never share a thread.

We keep a small registry keyed by the worker thread's identity. The worker calls
``should_stop()`` for its own id between turns and mid-stream; the requester (who
knows which thread it started) calls ``request_stop(ident)``. The shared
``threading.Event`` is what crosses the thread boundary.
"""

from __future__ import annotations

import threading


class StopRequested(KeyboardInterrupt):
    """Raised inside a worker thread when the user requests a stop.

    Subclasses ``KeyboardInterrupt`` (a ``BaseException``) on purpose: it is still
    caught by the main loop's existing ``except KeyboardInterrupt`` handler, yet it
    slips through any ``except Exception`` in the planning/orientation phases so it
    can propagate to a single top-level handler instead of crashing the thread.
    """


_lock = threading.Lock()
_events: dict[int, threading.Event] = {}


def register(ident: int | None = None) -> threading.Event:
    """Register (and return) a stop Event for ``ident`` (default: current thread)."""
    ident = threading.get_ident() if ident is None else ident
    ev = threading.Event()
    with _lock:
        _events[ident] = ev
    return ev


def unregister(ident: int | None = None) -> None:
    """Drop the stop Event for ``ident`` (default: current thread)."""
    ident = threading.get_ident() if ident is None else ident
    with _lock:
        _events.pop(ident, None)


def request_stop(ident: int) -> bool:
    """Signal the worker thread ``ident`` to stop. Returns True if it was registered."""
    with _lock:
        ev = _events.get(ident)
    if ev is not None:
        ev.set()
        return True
    return False


def should_stop() -> bool:
    """True if a stop has been requested for the *current* thread."""
    with _lock:
        ev = _events.get(threading.get_ident())
    return ev is not None and ev.is_set()


# What the agent (and any later session that reads the history) is told about why
# the run ended. Kept here so every layer reports a stop the same way.
STOP_NOTICE = (
    "[The user stopped this session.] Work was interrupted mid-task, so the last "
    "action may be incomplete and any command that was running was killed. Nothing "
    "here is a failure of the work itself. When the session resumes, check the "
    "current state of the files before continuing, and do not redo steps that "
    "already completed."
)


def wait(seconds: float, poll: float = 0.25) -> bool:
    """Sleep, but wake immediately when a stop is requested for this thread.

    Retry/backoff waits are the main reason a Stop used to take minutes to land:
    a plain ``time.sleep(30)`` cannot be interrupted. Returns True if the wait was
    cut short by a stop.
    """
    import time as _t
    with _lock:
        ev = _events.get(threading.get_ident())
    if ev is None:
        _t.sleep(seconds)
        return False
    return ev.wait(seconds)     # returns True as soon as the Event is set


def raise_if_stopped() -> None:
    """Raise ``StopRequested`` if a stop is pending for this thread."""
    if should_stop():
        raise StopRequested
