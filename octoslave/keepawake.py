"""Keep the computer awake while an agent is working.

Every run lives in this process. When the machine idle-sleeps, every thread
freezes: the model's stream dies, the SSH session to a remote drops, and nothing
moves until someone wakes the machine. ``netwatch`` makes a run *recover* after
a sleep; this module stops the idle sleep from happening in the first place.

Only *idle* sleep is blocked. The display still turns off on its own schedule
(screen off is fine), and a deliberate sleep (closing the lid, Apple menu →
Sleep) still happens: no unprivileged program can veto that.

Holds are counted across threads. The OS lock is taken when the first holder
arrives and dropped when the last one leaves, so nested holds (parallel agents
calling ``run_agent``, specialists inside a Science turn) are fine. There are
two kinds of hold:

  ``work``  an agent is actively working. Blocks idle sleep on battery too: a
            run is finite, and stopping it halfway wastes it.
  ``wait``  nothing is running, but a Science session waits for cluster jobs
            and will carry on by itself when they end. Jobs can run for days, so
            this holds only on mains power (macOS ``PreventSystemSleep`` is
            AC-only by design). On other platforms it does nothing.

macOS: an IOKit power assertion taken by THIS process, so ``pmset -g
assertions`` names us and the kernel drops it if the process dies. Holding it
in-process should also keep App Nap off the GUI app (not measured). Fallback:
``caffeinate -w <our pid>``.
Linux: ``systemd-inhibit``, tied to our pid. Windows: ``SetThreadExecutionState``.

Off switch: ``keep_awake: false`` in config.json, or ``OCTOSLAVE_KEEP_AWAKE=0``.
"""

from __future__ import annotations

import contextlib
import functools
import os
import shutil
import signal
import subprocess
import sys
import threading
from typing import ClassVar

_REASON = "OctoSlave: an agent is working"
_WAIT_REASON = "OctoSlave: waiting for cluster jobs to finish"

_lock = threading.Lock()
_work = 0                  # active "work" holders
_waiting = False           # level set by the Science job watcher
_held: _Lock | None = None


def enabled() -> bool:
    env = (os.environ.get("OCTOSLAVE_KEEP_AWAKE") or "").strip().lower()
    if env:
        return env not in ("0", "false", "no", "off")
    try:
        from .config import load_config
        return bool(load_config().get("keep_awake", True))
    except Exception:  # noqa: BLE001 — unreadable config must not block a run
        return True


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def hold():
    """Keep the machine awake for the duration of the ``with`` block."""
    global _work
    with _lock:
        _work += 1
        _sync()
    try:
        yield
    finally:
        with _lock:
            _work = max(0, _work - 1)
            _sync()


def while_working(fn):
    """Decorator form of :func:`hold` for a run's entry point."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with hold():
            return fn(*args, **kwargs)
    return wrapper


def set_waiting(waiting: bool) -> None:
    """Level-triggered ``wait`` hold (the Science job watcher calls this every
    tick with whether any session is waiting on jobs to continue by itself)."""
    global _waiting
    with _lock:
        if _waiting != bool(waiting):
            _waiting = bool(waiting)
            _sync()


def state() -> str:
    """"work", "wait" or "" — what is held right now (for tests/diagnostics)."""
    with _lock:
        return _held.kind if _held is not None else ""


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _sync() -> None:
    """Make the held OS lock match the counters. Caller holds ``_lock``."""
    global _held
    want = "work" if _work > 0 else ("wait" if _waiting else "")
    if want and not enabled():
        want = ""
    if (_held.kind if _held is not None else "") == want:
        return
    if _held is not None:
        _held.release()
        _held = None
    if want:
        _held = _acquire(want)


class _Lock:
    kind = ""

    def release(self) -> None:  # pragma: no cover — overridden
        pass


def _acquire(kind: str) -> _Lock | None:
    try:
        if sys.platform == "darwin":
            try:
                return _MacAssertion(kind)
            except Exception:  # noqa: BLE001 — IOKit unavailable → caffeinate
                return _Child(kind, ["caffeinate", "-i" if kind == "work" else "-s",
                                     "-w", str(os.getpid())])
        if kind != "work":
            return None   # no AC-only primitive here; never drain a battery for days
        if sys.platform.startswith("linux") and shutil.which("systemd-inhibit"):
            return _Child(kind, [
                "systemd-inhibit", "--what=idle:sleep", "--who=OctoSlave",
                f"--why={_REASON}", "--mode=block",
                "tail", f"--pid={os.getpid()}", "-f", "/dev/null"])
        if sys.platform == "win32":
            return _WinExecutionState(kind)
    except Exception:  # noqa: BLE001 — keeping awake is best-effort, never fatal
        return None
    return None


class _MacAssertion(_Lock):
    _TYPES: ClassVar[dict[str, bytes]] = {"work": b"PreventUserIdleSystemSleep",
                                         "wait": b"PreventSystemSleep"}
    _lib = None

    @classmethod
    def _load(cls):
        if cls._lib is None:
            import ctypes
            iokit = ctypes.CDLL("/System/Library/Frameworks/IOKit.framework/IOKit")
            cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
            cf.CFStringCreateWithCString.restype = ctypes.c_void_p
            cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                                     ctypes.c_uint32]
            cf.CFRelease.argtypes = [ctypes.c_void_p]
            iokit.IOPMAssertionCreateWithName.argtypes = [
                ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_uint32)]
            iokit.IOPMAssertionCreateWithName.restype = ctypes.c_int
            iokit.IOPMAssertionRelease.argtypes = [ctypes.c_uint32]
            iokit.IOPMAssertionRelease.restype = ctypes.c_int
            cls._lib = (ctypes, iokit, cf)
        return cls._lib

    def __init__(self, kind: str):
        ctypes, iokit, cf = self._load()
        utf8 = 0x08000100                     # kCFStringEncodingUTF8
        reason = (_REASON if kind == "work" else _WAIT_REASON).encode()
        t = cf.CFStringCreateWithCString(None, self._TYPES[kind], utf8)
        n = cf.CFStringCreateWithCString(None, reason, utf8)
        aid = ctypes.c_uint32(0)
        try:
            rc = iokit.IOPMAssertionCreateWithName(t, 255, n, ctypes.byref(aid))
        finally:
            cf.CFRelease(t)
            cf.CFRelease(n)
        if rc != 0:
            raise OSError(f"IOPMAssertionCreateWithName failed ({rc:#x})")
        self.kind, self._id = kind, aid.value

    def release(self) -> None:
        try:
            self._load()[1].IOPMAssertionRelease(self._id)
        except Exception:  # noqa: BLE001
            pass


class _Child(_Lock):
    """A helper process that holds the lock for as long as it lives. It is tied
    to our pid, so it exits on its own if this process dies without releasing."""

    def __init__(self, kind: str, argv: list[str]):
        self.kind = kind
        self._p = subprocess.Popen(argv, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL,
                                   start_new_session=True)

    def release(self) -> None:
        try:
            os.killpg(self._p.pid, signal.SIGTERM)   # the helper and its child
        except Exception:  # noqa: BLE001
            try:
                self._p.terminate()
            except Exception:  # noqa: BLE001
                pass
        try:
            self._p.wait(timeout=5)
        except Exception:  # noqa: BLE001
            pass


class _WinExecutionState(_Lock):
    """``SetThreadExecutionState`` is per thread, so a dedicated thread sets it
    and clears it again when released."""

    def __init__(self, kind: str):
        import ctypes
        self.kind = kind
        self._stop = threading.Event()
        es_continuous, es_system_required = 0x80000000, 0x00000001

        def keep() -> None:
            k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            k32.SetThreadExecutionState(es_continuous | es_system_required)
            self._stop.wait()
            k32.SetThreadExecutionState(es_continuous)

        self._t = threading.Thread(target=keep, name="keep-awake", daemon=True)
        self._t.start()

    def release(self) -> None:
        self._stop.set()
        self._t.join(timeout=5)
