"""Background specialist runs for the Science orchestrator.

A specialist used to run nested on the orchestrator's own thread, which froze
the conversation for as long as it worked: the orchestrator could not answer the
researcher, could not check on the specialist, and could not run two pieces of
independent work at once. This module runs each one on its own thread instead,
so the orchestrator stays free to talk, supervise and dispatch more work.

What a background thread needs to behave like the nested run did:

* **Stop** — it adopts the orchestrator thread's stop Event
  (``interrupt.adopt``), so one Stop from the user reaches every specialist.
* **Attribution** — every event it emits is stamped with its identity, on its
  own thread (``context.set_speaker``), so the UI can tell two concurrent
  specialists apart.
* **Steering** — the turn's inbox is bound on the thread, so a message the
  researcher sends mid-run still reaches a working specialist as an FYI.

The orchestrator observes the pool through :meth:`Pool.snapshot` (what each one
is doing right now) and :meth:`Pool.collect` (reports it has not been told about
yet). :meth:`Pool.wait` parks it cheaply until there is something to react to.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Optional


def _short(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


@dataclass
class Run:
    """One dispatched specialist: its identity, its thread, and its progress."""
    id: str
    name: str
    role: str
    goal: str
    icon: str = "🔬"
    model: str = ""
    started: float = field(default_factory=time.monotonic)
    status: str = "working"          # working | done | failed | stopped
    summary: str = ""
    ok: bool = True
    reported: bool = False           # has the orchestrator been told the result?
    steps: int = 0
    activity: str = ""               # the tool it is running right now
    thread: Optional[threading.Thread] = None
    ident: Optional[int] = None      # its thread id, set once it starts (for cancel)
    ended: Optional[float] = None
    # Files it proposed showing the researcher. A specialist does not present
    # anything itself: the orchestrator checks these and decides.
    proposed: list = field(default_factory=list)

    @property
    def elapsed(self) -> float:
        return (self.ended or time.monotonic()) - self.started

    def line(self) -> str:
        mins = self.elapsed / 60
        when = f"{self.elapsed:.0f}s" if mins < 1 else f"{mins:.0f} min"
        if self.status == "working":
            doing = f" — {self.activity}" if self.activity else ""
            return (f"- **{self.name}** ({self.role}) — working for {when}, "
                    f"{self.steps} tool call{'s' if self.steps != 1 else ''}"
                    f"{doing}\n  Goal: {_short(self.goal, 200)}")
        return f"- **{self.name}** ({self.role}) — {self.status} after {when}"


class Pool:
    """The specialists dispatched during one orchestrator turn."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._runs: dict[str, Run] = {}
        self._news = threading.Event()   # set whenever a specialist reports back
        self._cancelled = False
        # Files each working specialist has written, so two running in parallel
        # cannot silently overwrite each other's (see claim()).
        self._claims: dict[str, str] = {}

    # -- dispatch ---------------------------------------------------------
    def start(self, run: Run, target, args: tuple) -> None:
        """Register ``run`` and start ``target(*args)`` on its own thread."""
        with self._lock:
            self._runs[run.id] = run
        t = threading.Thread(target=target, args=args,
                             name=f"science-specialist-{run.id}", daemon=True)
        run.thread = t
        t.start()

    def get(self, sid: str) -> Optional[Run]:
        with self._lock:
            return self._runs.get(sid)

    # -- progress (called from the specialist's own thread) ---------------
    def note_step(self, sid: str, activity: str) -> None:
        with self._lock:
            r = self._runs.get(sid)
            if r is not None and r.status == "working":
                r.steps += 1
                r.activity = _short(activity, 90)

    def attach(self, sid: str) -> bool:
        """Called first thing on the specialist's thread: record its id so the
        run can be cancelled. Returns False if the pool was cancelled before the
        thread got this far (it should then stop straight away)."""
        with self._lock:
            r = self._runs.get(sid)
            if r is not None:
                r.ident = threading.get_ident()
            return not self._cancelled

    def claim(self, path: str, sid: str) -> Optional[Run]:
        """Record that specialist ``sid`` writes ``path``. Returns the OTHER
        specialist already writing it while still working (a conflict), or
        None. A claim lasts until its owner finishes: handing a finished
        specialist's file on to another one is normal and allowed."""
        with self._lock:
            owner = self._claims.get(path)
            if owner and owner != sid:
                other = self._runs.get(owner)
                if other is not None and other.status == "working":
                    return other
            self._claims[path] = sid
            return None

    def propose(self, sid: str, item: dict) -> bool:
        """Record an output specialist ``sid`` suggests showing (one per path)."""
        with self._lock:
            r = self._runs.get(sid)
            if r is None:
                return False
            r.proposed = [p for p in r.proposed if p.get("rel") != item.get("rel")] + [item]
            return True

    def finish(self, sid: str, status: str, summary: str, ok: bool = True) -> None:
        with self._lock:
            for p in [p for p, o in self._claims.items() if o == sid]:
                del self._claims[p]
            r = self._runs.get(sid)
            if r is None or r.status != "working":
                return
            r.status, r.summary, r.ok, r.activity = status, summary, ok, ""
            r.ended = time.monotonic()
            self._news.set()

    # -- observation (called from the orchestrator's thread) --------------
    def running(self) -> list[Run]:
        # A thread that is gone but never reported (it died on an error the
        # run's own handling did not see) must not count as working, or the
        # orchestrator would wait for it forever.
        for r in [r for r in self._all() if r.status == "working"]:
            t = r.thread
            if t is not None and t.ident is not None and not t.is_alive():
                self.finish(r.id, "failed", f"Specialist '{r.name}' ended "
                            "unexpectedly without reporting back.", ok=False)
        with self._lock:
            return [r for r in self._runs.values() if r.status == "working"]

    def _all(self) -> list[Run]:
        with self._lock:
            return list(self._runs.values())

    def any_running(self) -> bool:
        return bool(self.running())

    def collect(self) -> list[Run]:
        """Finished specialists the orchestrator has not been told about yet.

        Marks them reported, so each result is handed over exactly once.
        """
        self.running()                      # settle any that died silently
        with self._lock:
            out = [r for r in self._runs.values()
                   if r.status != "working" and not r.reported]
            for r in out:
                r.reported = True
            self._news.clear()
        return out

    def wait(self, timeout: float) -> bool:
        """Block until a specialist reports back (True) or ``timeout`` (False)."""
        return self._news.wait(timeout)

    def cancel(self) -> None:
        """Ask every specialist still working to stop (its own stop signal —
        the orchestrator's thread is not affected)."""
        from .. import interrupt
        with self._lock:
            self._cancelled = True
        for r in self.running():
            if r.ident is not None:
                interrupt.request_stop(r.ident)

    def snapshot(self) -> str:
        """What every specialist is doing right now, for the orchestrator."""
        with self._lock:
            runs = list(self._runs.values())
        if not runs:
            return "No specialists have been dispatched in this turn."
        work = [r for r in runs if r.status == "working"]
        done = [r for r in runs if r.status != "working"]
        out = []
        if work:
            out.append("### Still working\n" + "\n".join(r.line() for r in work))
        if done:
            out.append("### Finished\n" + "\n".join(r.line() for r in done))
        return "\n\n".join(out)

    def join(self, timeout: float = 30.0) -> list[Run]:
        """Wait for every specialist to come back. Returns those still running
        when ``timeout`` expired (they are daemon threads, so they die with the
        process, but their partial work is already persisted)."""
        deadline = time.monotonic() + timeout
        for r in list(self._runs.values()):
            t = r.thread
            if t is not None and t.is_alive():
                t.join(max(0.0, deadline - time.monotonic()))
        return self.running()


# ---------------------------------------------------------------------------
# Handing results to the orchestrator
# ---------------------------------------------------------------------------

_REPORT_GUIDANCE = (
    "(Verify what it claims against the files it says it produced before you "
    "relay any of it, then tell the researcher in the chat what came back and "
    "what you are doing with it. Present only the proposed files that are real "
    "findings or deliverables — skip anything that is working material. If "
    "specialists are still working, carry on with what you can do meanwhile "
    "rather than waiting in silence.)"
)


def format_reports(runs: list[Run], pool: "Pool") -> str:
    """The message handed to the orchestrator when specialists report back."""
    parts = []
    for r in runs:
        took = f" after {r.elapsed / 60:.0f} min" if r.elapsed >= 60 else ""
        head = f"**{r.name}** ({r.role}) {r.status}{took}"
        proposed = ""
        if r.proposed:
            proposed = ("\n\n**Proposed for the researcher** (not shown to them — "
                        "your call):\n" + "\n".join(
                            f"- `{p['rel']}` — {p.get('caption') or '(no caption)'}"
                            + (" (interim)" if p.get("interim") else "")
                            for p in r.proposed))
        parts.append(f"{head}.\nIts id is `{r.id}` — use continue_specialist to "
                     f"take its work further rather than spawning another agent "
                     f"for the same area.\n\n{r.summary or '(no summary returned)'}"
                     f"{proposed}")
    body = "\n\n---\n\n".join(parts)
    still = pool.running()
    tail = ""
    if still:
        tail = ("\n\nStill working: "
                + ", ".join(f"{r.name} ({r.elapsed / 60:.0f} min)" for r in still))
    return (f"[Specialist report — {len(runs)} specialist"
            f"{'s' if len(runs) != 1 else ''} reported back while you worked]\n\n"
            f"{body}{tail}\n\n{_REPORT_GUIDANCE}")


# ---------------------------------------------------------------------------
# Keeping the orchestrator from quietly doing everything itself
#
# Delegation is the whole point of the role, but it is the easy thing to skip:
# each next step always looks small enough to just do, and a session ends with
# the orchestrator having ground through the lot on its own context while the
# specialists sat idle. A streak of its own tool calls with nothing dispatched is
# a reliable sign of that, so it is counted and pointed out — once per streak, so
# a genuinely solo stretch is not nagged, and never while specialists are busy.
# ---------------------------------------------------------------------------

#: Own tool calls in a row before the orchestrator is asked to reconsider.
SOLO_STREAK_LIMIT = 30


class SoloWatch:
    """Counts the orchestrator's consecutive own tool calls."""

    def __init__(self, limit: int = SOLO_STREAK_LIMIT) -> None:
        self.limit = limit
        self._lock = threading.Lock()
        self._streak = 0
        self._warned_at = 0
        self._warnings = 0
        self._by_tool: dict[str, int] = {}

    def note_tool(self, name: str) -> None:
        """One tool call BY THE ORCHESTRATOR (never a specialist's)."""
        with self._lock:
            if name in ("spawn_specialist", "continue_specialist"):
                self._streak = 0
                self._warned_at = 0
                self._warnings = 0
                self._by_tool = {}
            else:
                self._streak += 1
                self._by_tool[name] = self._by_tool.get(name, 0) + 1

    def nudge(self) -> str:
        """A reminder to delegate, or "" when none is due.

        The first comes after ``limit`` calls; each one that goes unheeded
        brings the next sooner (down to half the limit). A single
        reminder was easy to answer with "this is indivisible" and then ignore
        for hundreds of calls — the streak kept growing with nobody dispatched.
        """
        with self._lock:
            gap = max(self.limit // 2, self.limit // (1 + self._warnings))
            if self._streak < self.limit or self._streak < self._warned_at + gap:
                return ""
            n, self._warned_at = self._streak, self._streak
            self._warnings += 1
            top = sorted(self._by_tool.items(), key=lambda kv: -kv[1])[:3]
            again = self._warnings > 1
        mix = ", ".join(f"{k} ×{v}" for k, v in top)
        node = any(k.startswith("cluster_") or k in ("write_cluster_file",
                   "fetch_cluster_file", "submit_cluster_job") for k, _ in top)
        return (
            f"[Note to you, the orchestrator — not from the researcher]\n\n"
            f"You have made {n} tool calls in a row yourself without dispatching "
            f"anyone ({mix})"
            + (" — and this is not the first reminder" if again else "")
            + ". That is the pattern this role exists to avoid: your context "
            f"fills with detail, the researcher waits on a single thread of work, "
            f"and the specialists you could have had running in parallel are idle.\n"
            f"Look at what is left and split off whatever is a bounded piece with a "
            f"clear deliverable — then dispatch those together and supervise."
            + (" Work on the compute node delegates well: a specialist given the "
               "cluster tools can build, run and inspect it while you supervise."
               if node else "")
            + " If the remaining work genuinely cannot be handed over (it is one "
            f"indivisible thread, or it depends on what the researcher just told "
            f"you), carry on and say so in one line so they know it is deliberate."
        )
